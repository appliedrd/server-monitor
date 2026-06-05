#!/usr/bin/env python3
"""
Server Monitor — self-hosted uptime monitoring (replaces NodePing).

Runs from cron every 5 min on a Debian 12 GCE box. For each target in
servers.yaml it does an HTTP(S) or TCP check, tracks consecutive failures in a
small state file, and sends a Twilio SMS when a host goes DOWN (after N
consecutive failures) and again when it RECOVERS. At the end of every run it
pings a healthchecks.io URL as a dead-man's switch, so if this script or cron
ever stops, healthchecks.io emails you.

Each run also tallies per-site daily counters (checks / fails / incidents).
Run once a day with --summary to text a digest and reset those counters:
    venv/bin/python monitor.py --summary

Deps (install into a venv — Debian 12 enforces PEP 668):
    python3 -m venv venv
    venv/bin/pip install twilio requests pyyaml

Config: see config.yaml (secrets + defaults) and servers.yaml (targets).
"""

import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.yaml"
SERVERS_FILE = HERE / "servers.yaml"
STATE_FILE = HERE / "state.json"

DISPLAY_TZ = timezone.utc  # overridden by config defaults.timezone (display only)


def now_label() -> str:
    """Current time as 'YYYY-MM-DD HH:MM TZ' in the configured display timezone."""
    return datetime.now(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M %Z")


def log(msg: str) -> None:
    """Timestamped line to stdout (cron redirects this to monitor.log)."""
    print(f"{now_label()}  {msg}", flush=True)


def set_display_tz(cfg: dict) -> None:
    """Set the display timezone from config (does not touch the host clock)."""
    global DISPLAY_TZ
    name = (cfg.get("defaults") or {}).get("timezone")
    if not name:
        return
    if ZoneInfo is None:
        log("WARN: zoneinfo unavailable; using UTC for display")
        return
    try:
        DISPLAY_TZ = ZoneInfo(name)
    except Exception:  # noqa: BLE001
        log(f"WARN: unknown timezone '{name}'; using UTC for display")


def load_yaml(path: Path) -> dict:
    if not path.exists():
        log(f"FATAL: missing config file {path}")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            log("WARN: state file unreadable, starting fresh")
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    tmp.replace(STATE_FILE)  # atomic


def target_name(target: dict) -> str:
    return target.get("name") or target.get("url") or target.get("host", "?")


# ---- checks ---------------------------------------------------------------

def check_http(target: dict, timeout: float) -> tuple[bool, str]:
    url = target["url"]
    expected = target.get("expect_status", [200, 301, 302])
    try:
        r = requests.get(url, timeout=timeout, allow_redirects=False)
        # "any" = reachability check: any HTTP response under 500 means the
        # service answered (up). Use for third-party deps that return 401/403/
        # 404/302 to bare requests. Only 5xx / connection failures are "down".
        if isinstance(expected, str) and expected.lower() == "any":
            if r.status_code < 500:
                return True, f"HTTP {r.status_code} (reachable)"
            return False, f"HTTP {r.status_code} (server error)"
        if isinstance(expected, int):
            expected = [expected]
        if r.status_code in expected:
            return True, f"HTTP {r.status_code}"
        return False, f"HTTP {r.status_code} (expected {expected})"
    except requests.RequestException as e:
        return False, f"unreachable ({e.__class__.__name__})"


def check_tcp(target: dict, timeout: float) -> tuple[bool, str]:
    host = target["host"]
    port = int(target["port"])
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"TCP {host}:{port} open"
    except OSError as e:
        return False, f"TCP {host}:{port} failed ({e.__class__.__name__})"


def run_check(target: dict, timeout: float) -> tuple[bool, str]:
    kind = target.get("type", "http").lower()
    if kind == "http":
        return check_http(target, timeout)
    if kind == "tcp":
        return check_tcp(target, timeout)
    return False, f"unknown check type '{kind}'"


# ---- alerting -------------------------------------------------------------

def send_sms(cfg: dict, body: str, max_len: int = 700) -> None:
    tw = cfg.get("twilio", {})
    if len(body) > max_len:             # safety ceiling; full detail stays in the log
        body = body[:max_len - 3] + "..."
    try:
        from twilio.rest import Client
        client = Client(tw["account_sid"], tw["auth_token"])
        client.messages.create(body=body, from_=tw["from_number"], to=tw["to_number"])
        log(f"SMS sent: {body!r}")
    except Exception as e:  # noqa: BLE001 — never let alerting crash the run
        log(f"ERROR sending SMS: {e.__class__.__name__}: {e}")


def blank_counters() -> dict:
    return {"fails": 0, "alerted": False, "day_checks": 0, "day_fails": 0, "day_incidents": 0}


# ---- run modes ------------------------------------------------------------

def run_checks(cfg: dict, servers: list, state: dict) -> None:
    defaults = cfg.get("defaults", {})
    timeout = float(defaults.get("timeout_seconds", 10))
    threshold = int(defaults.get("failure_threshold", 2))

    state.setdefault("_summary", {"since": now_label()})

    if not servers:
        log("WARN: no servers configured in servers.yaml")

    for target in servers:
        name = target_name(target)
        ok, detail = run_check(target, timeout)

        st = state.setdefault(name, blank_counters())
        st["day_checks"] = st.get("day_checks", 0) + 1

        if ok:
            if st.get("alerted"):
                send_sms(cfg, f"RECOVERED: {name} is back up ({detail}).")
            st["fails"] = 0
            st["alerted"] = False
            log(f"UP   {name} — {detail}")
        else:
            st["fails"] = st.get("fails", 0) + 1
            st["day_fails"] = st.get("day_fails", 0) + 1
            log(f"DOWN {name} — {detail} (consecutive fails: {st['fails']})")
            if st["fails"] >= threshold and not st.get("alerted"):
                send_sms(cfg, f"DOWN: {name} failed {st['fails']}x — {detail}")
                st["alerted"] = True
                st["day_incidents"] = st.get("day_incidents", 0) + 1

    save_state(state)

    # Dead-man's switch: tell healthchecks.io we completed a run.
    ping_url = cfg.get("healthcheck", {}).get("ping_url")
    if ping_url:
        try:
            requests.get(ping_url, timeout=10)
            log("healthcheck ping sent")
        except requests.RequestException as e:
            log(f"WARN: healthcheck ping failed: {e}")


def send_summary(cfg: dict, servers: list, state: dict) -> None:
    since = state.get("_summary", {}).get("since", "?")
    lines, total_incidents, all_ok = [], 0, True

    for target in servers:
        name = target_name(target)
        st = state.get(name, {})
        checks = st.get("day_checks", 0)
        fails = st.get("day_fails", 0)
        incidents = st.get("day_incidents", 0)
        total_incidents += incidents
        up = checks - fails
        pct = (up / checks * 100) if checks else 0.0

        if incidents or fails or st.get("alerted"):
            all_ok = False
        flag = " DOWN NOW" if st.get("alerted") else ""
        extra = f", {incidents} incident(s)" if incidents else ""
        lines.append(f"- {name}: {pct:.1f}% ({up}/{checks}){extra}{flag}")

    header = ("Server Monitor daily: all systems healthy"
              if all_ok else
              f"Server Monitor daily: {total_incidents} incident(s) in last 24h")
    body = header + f"\n(since {since})\n" + "\n".join(lines)
    send_sms(cfg, body)
    log(f"daily summary sent ({total_incidents} incidents)")

    # Reset the daily window.
    for target in servers:
        st = state.get(target_name(target))
        if st:
            st["day_checks"] = st["day_fails"] = st["day_incidents"] = 0
    state["_summary"] = {"since": now_label()}
    save_state(state)


# ---- main -----------------------------------------------------------------

def main() -> None:
    cfg = load_yaml(CONFIG_FILE)
    set_display_tz(cfg)
    servers = load_yaml(SERVERS_FILE).get("servers", [])
    state = load_state()

    if "--summary" in sys.argv[1:]:
        send_summary(cfg, servers, state)
    else:
        run_checks(cfg, servers, state)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        # stdout was piped to a reader that closed early (e.g. `| head`).
        # Harmless; never happens under cron (output goes to a file). Exit quietly.
        try:
            sys.stdout.close()
        except Exception:  # noqa: BLE001
            pass
        os._exit(0)

#!/usr/bin/env python3
"""
Server Monitor — self-hosted uptime monitoring (replaces NodePing).

Runs from cron every 5 min on a Debian 12 GCE box. For each target in
servers.yaml it does an HTTP(S) or TCP check, tracks consecutive failures in a
small state file, and sends a Twilio SMS when a host goes DOWN (after N
consecutive failures) and again when it RECOVERS. At the end of every run it
pings a healthchecks.io URL as a dead-man's switch, so if this script or cron
ever stops, healthchecks.io emails you.

False alarms are kept down three ways:
  - a failed check is retried once, 20 s later, before it counts;
  - a site that connects but answers slowly (ReadTimeout) is "slow", not
    "down", and only texts if it stays that way (slow_threshold);
  - timeouts and thresholds can be set per target in servers.yaml.

Each run also tallies per-site daily counters (checks / fails / slow /
incidents). Run once a day with --summary to text a digest and reset them:
    venv/bin/python monitor.py --summary

Deps (install into a venv — Debian 12 enforces PEP 668):
    python3 -m venv venv
    venv/bin/pip install -r requirements.txt

Config: see config.yaml (secrets + defaults) and servers.yaml (targets).
"""

import json
import os
import socket
import sys
import time
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
LOG_FILE = HERE / "monitor.log"

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


def trim_log(max_bytes: int) -> None:
    """Keep monitor.log bounded without needing root for logrotate: past the
    limit, move it to monitor.log.1 (replacing the old one). cron's >> opens a
    fresh monitor.log on the next run."""
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > max_bytes:
            LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
    except OSError:
        pass


def target_name(target: dict) -> str:
    return target.get("name") or target.get("url") or target.get("host", "?")


def setting(target: dict, defaults: dict, key: str, fallback):
    """Per-target value, else the config default, else the built-in fallback."""
    return target.get(key, defaults.get(key, fallback))


# ---- checks ---------------------------------------------------------------
# Each check returns (status, detail); status is "up", "slow" or "down".

def check_http(target: dict, timeout: float) -> tuple[str, str]:
    url = target["url"]
    expected = target.get("expect_status", [200, 301, 302])
    try:
        r = requests.get(url, timeout=timeout, allow_redirects=False)
        # "any" = reachability check: any HTTP response under 500 means the
        # service answered (up). Use for third-party deps that return 401/403/
        # 404/302 to bare requests. Only 5xx / connection failures are "down".
        if isinstance(expected, str) and expected.lower() == "any":
            if r.status_code < 500:
                return "up", f"HTTP {r.status_code} (reachable)"
            return "down", f"HTTP {r.status_code} (server error)"
        if isinstance(expected, int):
            expected = [expected]
        if r.status_code in expected:
            return "up", f"HTTP {r.status_code}"
        return "down", f"HTTP {r.status_code} (expected {expected})"
    except requests.ReadTimeout:
        # Connected, but no answer in time: a struggling site, not a dead one.
        return "slow", f"no answer within {timeout:.0f}s (ReadTimeout)"
    except requests.RequestException as e:
        return "down", f"unreachable ({e.__class__.__name__})"


def check_tcp(target: dict, timeout: float) -> tuple[str, str]:
    host = target["host"]
    port = int(target["port"])
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "up", f"TCP {host}:{port} open"
    except OSError as e:
        return "down", f"TCP {host}:{port} failed ({e.__class__.__name__})"


def check_once(target: dict, timeout: float) -> tuple[str, str]:
    kind = target.get("type", "http").lower()
    if kind == "http":
        return check_http(target, timeout)
    if kind == "tcp":
        return check_tcp(target, timeout)
    return "down", f"unknown check type '{kind}'"


def run_check(target: dict, timeout: float, retries: int, delay: float) -> tuple[str, str]:
    """Check, and on failure retry after `delay` seconds before believing it.
    Most false alarms are a single blip that is gone 20 s later."""
    status, detail = check_once(target, timeout)
    for _ in range(retries):
        if status == "up":
            break
        time.sleep(delay)
        status, detail = check_once(target, timeout)
        if status == "up":
            detail += " (after retry)"
    return status, detail


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
    return {"fails": 0, "slow": 0, "alerted": False,
            "day_checks": 0, "day_fails": 0, "day_slow": 0, "day_incidents": 0}


# ---- run modes ------------------------------------------------------------

def run_checks(cfg: dict, servers: list, state: dict) -> None:
    defaults = cfg.get("defaults", {})
    retries = int(defaults.get("retries", 1))
    delay = float(defaults.get("retry_delay_seconds", 20))

    state.setdefault("_summary", {"since": now_label()})

    if not servers:
        log("WARN: no servers configured in servers.yaml")

    for target in servers:
        name = target_name(target)
        timeout = float(setting(target, defaults, "timeout_seconds", 10))
        threshold = int(setting(target, defaults, "failure_threshold", 2))
        slow_threshold = int(setting(target, defaults, "slow_threshold", 6))
        status, detail = run_check(target, timeout, retries, delay)

        st = state.setdefault(name, blank_counters())
        st["day_checks"] = st.get("day_checks", 0) + 1

        if status == "up":
            if st.get("alerted"):
                send_sms(cfg, f"RECOVERED: {name} is back up ({detail}).")
            st["fails"] = st["slow"] = 0
            st["alerted"] = False
            log(f"UP   {name} — {detail}")
            continue

        # Slow and down both count as "not answering"; down alerts sooner.
        st["fails"] = st.get("fails", 0) + 1
        if status == "slow":
            st["slow"] = st.get("slow", 0) + 1
            st["day_slow"] = st.get("day_slow", 0) + 1
            log(f"SLOW {name} — {detail} (consecutive: {st['fails']})")
        else:
            st["day_fails"] = st.get("day_fails", 0) + 1
            log(f"DOWN {name} — {detail} (consecutive fails: {st['fails']})")

        # A streak of only-slow checks alerts at slow_threshold; any hard
        # failure in the streak brings it down to failure_threshold.
        only_slow = st.get("slow", 0) == st["fails"]
        limit = slow_threshold if only_slow else threshold
        if st["fails"] >= limit and not st.get("alerted"):
            if only_slow:
                send_sms(cfg, f"SLOW: {name} has not answered for ~{st['fails'] * 5} min — {detail}")
            else:
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
        slow = st.get("day_slow", 0)
        incidents = st.get("day_incidents", 0)
        total_incidents += incidents
        up = checks - fails - slow
        pct = (up / checks * 100) if checks else 0.0

        if incidents or fails or st.get("alerted"):
            all_ok = False
        flag = " DOWN NOW" if st.get("alerted") else ""
        extra = f", {incidents} incident(s)" if incidents else ""
        extra += f", slow {slow}x" if slow else ""
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
            st["day_checks"] = st["day_fails"] = st["day_slow"] = st["day_incidents"] = 0
    state["_summary"] = {"since": now_label()}
    save_state(state)


# ---- main -----------------------------------------------------------------

def single_instance():
    """Hold an exclusive lock for the whole run. With retries and long timeouts
    a bad run can outlast the 5-min cron interval; an overlapping run would
    clobber state.json, so it exits instead. Returns the lock handle, or None
    if another run holds it. (No-op where fcntl doesn't exist, e.g. Windows.)"""
    try:
        import fcntl
    except ImportError:
        return True
    fh = open(HERE / ".monitor.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def main() -> None:
    lock = single_instance()
    if lock is None:
        log("previous run still in progress; skipping this one")
        return
    cfg = load_yaml(CONFIG_FILE)
    set_display_tz(cfg)
    defaults = cfg.get("defaults", {})
    trim_log(int(defaults.get("max_log_mb", 5)) * 1024 * 1024)
    servers = load_yaml(SERVERS_FILE).get("servers", [])
    state = load_state()

    if "--summary" in sys.argv[1:]:
        # With defaults.summary_hour set, cron calls --summary at both UTC
        # hours it could be, and only the one matching local time sends, so
        # the digest stays at the same local hour across DST changes.
        hour = defaults.get("summary_hour")
        if (hour is not None and "--force" not in sys.argv[1:]
                and datetime.now(DISPLAY_TZ).hour != int(hour)):
            return
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

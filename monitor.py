#!/usr/bin/env python3
"""
Server Monitor — self-hosted uptime monitoring (replaces NodePing).

Runs from cron every 5 min on a Debian 12 GCE box. For each target in
servers.yaml it does an HTTP(S) or TCP check, tracks consecutive failures in a
small state file, and sends a Twilio SMS when a host goes DOWN (after N
consecutive failures) and again when it RECOVERS. At the end of every run it
pings a healthchecks.io URL as a dead-man's switch, so if this script or cron
ever stops, healthchecks.io emails you.

Deps (install into a venv — Debian 12 enforces PEP 668):
    python3 -m venv venv
    venv/bin/pip install twilio requests pyyaml

Config: see config.yaml (secrets + defaults) and servers.yaml (targets).
"""

import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.yaml"
SERVERS_FILE = HERE / "servers.yaml"
STATE_FILE = HERE / "state.json"


def log(msg: str) -> None:
    """Timestamped line to stdout (cron redirects this to monitor.log)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"{ts}  {msg}", flush=True)


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


# ---- checks ---------------------------------------------------------------

def check_http(target: dict, timeout: float) -> tuple[bool, str]:
    url = target["url"]
    expected = target.get("expect_status", [200, 301, 302])
    if isinstance(expected, int):
        expected = [expected]
    try:
        r = requests.get(url, timeout=timeout, allow_redirects=False)
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

def send_sms(cfg: dict, body: str) -> None:
    tw = cfg.get("twilio", {})
    if len(body) > 300:                 # keep SMS to ~2 segments; full detail stays in the log
        body = body[:297] + "..."
    try:
        from twilio.rest import Client
        client = Client(tw["account_sid"], tw["auth_token"])
        client.messages.create(body=body, from_=tw["from_number"], to=tw["to_number"])
        log(f"SMS sent: {body!r}")
    except Exception as e:  # noqa: BLE001 — never let alerting crash the run
        log(f"ERROR sending SMS: {e.__class__.__name__}: {e}")


# ---- main -----------------------------------------------------------------

def main() -> None:
    cfg = load_yaml(CONFIG_FILE)
    servers = load_yaml(SERVERS_FILE).get("servers", [])
    state = load_state()

    defaults = cfg.get("defaults", {})
    timeout = float(defaults.get("timeout_seconds", 10))
    threshold = int(defaults.get("failure_threshold", 2))

    if not servers:
        log("WARN: no servers configured in servers.yaml")

    for target in servers:
        name = target.get("name") or target.get("url") or target.get("host", "?")
        ok, detail = run_check(target, timeout)

        st = state.setdefault(name, {"fails": 0, "alerted": False})

        if ok:
            if st["alerted"]:
                send_sms(cfg, f"RECOVERED: {name} is back up ({detail}).")
            st["fails"] = 0
            st["alerted"] = False
            log(f"UP   {name} — {detail}")
        else:
            st["fails"] += 1
            log(f"DOWN {name} — {detail} (consecutive fails: {st['fails']})")
            if st["fails"] >= threshold and not st["alerted"]:
                send_sms(cfg, f"DOWN: {name} failed {st['fails']}x — {detail}")
                st["alerted"] = True

    save_state(state)

    # Dead-man's switch: tell healthchecks.io we completed a run.
    ping_url = cfg.get("healthcheck", {}).get("ping_url")
    if ping_url:
        try:
            requests.get(ping_url, timeout=10)
            log("healthcheck ping sent")
        except requests.RequestException as e:
            log(f"WARN: healthcheck ping failed: {e}")


if __name__ == "__main__":
    main()

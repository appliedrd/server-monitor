# Server Monitor

Self-hosted uptime monitoring — a small Python script that checks a list of
servers (HTTP and TCP), texts you via Twilio when one goes down (and when it
recovers), and uses a [healthchecks.io](https://healthchecks.io) dead-man's
switch so you're alerted if the monitor itself stops. Runs from cron on a
Debian 12 box. Built to replace NodePing.

## Files
| File | Purpose |
|------|---------|
| `monitor.py` | The monitor: HTTP/TCP checks, anti-flapping, Twilio SMS, healthcheck ping |
| `servers.example.yaml` | Template for `servers.yaml` — your list of targets |
| `config.example.yaml` | Template for `config.yaml` — Twilio creds + healthchecks.io URL |
| `requirements.txt` | Python deps (`twilio`, `requests`, `pyyaml`) |
| `DEPLOY.md` | Step-by-step deploy runbook |

> **`config.yaml` and `servers.yaml` are gitignored** — `config.yaml` holds your
> Twilio auth token, and `servers.yaml` is your private target list. Create both
> on the host from the `.example` templates; never commit them.

## Quick start
```bash
git clone https://github.com/appliedrd/server-monitor.git && cd server-monitor
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml     # edit + chmod 600 config.yaml
cp servers.example.yaml servers.yaml   # edit your targets
venv/bin/python monitor.py             # test
```
See `DEPLOY.md` for the full runbook (cron, healthchecks.io).

## How it works
- Each run checks every target (HTTP status or TCP connect) and records consecutive
  failures in `state.json`.
- An SMS fires only after `failure_threshold` consecutive failures (anti-flapping),
  and a RECOVERED text is sent when a host comes back.
- At the end of each run it pings a [healthchecks.io](https://healthchecks.io) URL —
  a dead-man's switch so you're alerted if the monitor itself stops.
- Designed to run from cron on an always-on host.
- A daily digest (`monitor.py --summary`, scheduled separately) texts per-site
  uptime % and any incidents over the last 24h, then resets the counters.

## License
MIT — see [LICENSE](LICENSE).

# Deploy runbook (Debian/Ubuntu host)

Assumes a Linux box that's always on and ideally on a different network than the
things you're monitoring (a cheap VPS or cloud VM is ideal). Paths below use
`~/server-monitor`.

## 1. Clone the repo on the host
Public repo — a plain clone works:
```bash
cd ~
git clone https://github.com/appliedrd/server-monitor.git
cd server-monitor
```
> For unattended `git pull` updates you can instead use a read-only **deploy key**
> (`Settings → Deploy keys` on the repo). Note a deploy key is bound to a single
> repo, so generate a dedicated key if the host already uses one elsewhere.

## 2. Python env + deps (Debian 12 enforces PEP 668 — use a venv)
```bash
sudo apt update && sudo apt install -y python3-venv python3-pip
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

## 3. Configure (these two files are gitignored — they're yours, per-host)
```bash
cp config.example.yaml config.yaml      # Twilio creds + healthchecks.io URL
cp servers.example.yaml servers.yaml     # your list of targets
nano config.yaml
nano servers.yaml
chmod 600 config.yaml                     # holds your Twilio auth token
```

## 4. Test before scheduling
```bash
venv/bin/python monitor.py               # healthy targets print "UP", no SMS
```
To prove the SMS path, point one target at a bad URL and run twice (default
`failure_threshold: 2`) — the second run should text you; restore it and the next
run sends a RECOVERED text.

## 5. Schedule with cron (every 5 min)
```bash
( crontab -l 2>/dev/null; echo "*/5 * * * * $HOME/server-monitor/venv/bin/python $HOME/server-monitor/monitor.py >> $HOME/server-monitor/monitor.log 2>&1" ) | crontab -
crontab -l
```
Use absolute paths in cron (as above) — cron has a minimal environment.

### Daily summary (optional)
Add a second cron line to text yourself a once-a-day digest (per-site uptime %
and any incidents over the last 24h). Example at 07:00:
```bash
( crontab -l 2>/dev/null; echo "0 7 * * * $HOME/server-monitor/venv/bin/python $HOME/server-monitor/monitor.py --summary >> $HOME/server-monitor/monitor.log 2>&1" ) | crontab -
```
Note cron fires in the **host's timezone** (GCE default is UTC). Check with
`date`; to get 07:00 local, adjust the hour (e.g. America/Montreal EDT = UTC-4,
so use `0 11 * * *`). The summary reads counters accumulated by the 5-min runs,
then resets them — so keep the regular `*/5` job running too.

## 6. Dead-man's switch (recommended)
Create a free check at https://healthchecks.io (period ~5 min, grace ~3–5 min),
paste its ping URL into `config.yaml` → `healthcheck.ping_url`. The monitor pings
it at the end of every run; if the monitor or the host dies, healthchecks.io
emails you. Without this, a dead monitor fails silently.

## Updating later
```bash
cd ~/server-monitor && git pull          # config.yaml / servers.yaml are gitignored, never touched
```

---
### Notes
- State lives in `state.json` (auto-created) — consecutive-fail counts persist between cron runs.
- Logs append to `monitor.log`; add a logrotate rule if it grows.
- Anti-flapping: an SMS fires only after `failure_threshold` consecutive fails, plus a RECOVERED text when a host comes back.

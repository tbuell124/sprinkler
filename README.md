GPIO Sprinkler Controller (Raspberry Pi)

Lightweight, single-file backend that drives sprinkler/valve relays on a Raspberry Pi. Ships with:

Web UI (single HTML file served by Flask) for pins, schedules, groups, and rain-delay status. 

sprinkler

 

sprinkler

Command-line controls (turn zones on/off, manage schedules, automation, rain-delay, NTP/time). 

sprinkler

 

sprinkler

 

sprinkler

Persistent JSON config next to the script (config.json) with safe atomic writes. 

sprinkler

 

sprinkler

 

sprinkler

APScheduler cron-style automation with a fallback polling loop if APScheduler isn’t available. 

sprinkler

Optional rain-delay that inspects local forecast probability before each ON event (threshold in %). 

sprinkler

 

sprinkler

Hardware

By default, the controller manages these GPIOs (BCM numbering): 5, 6, 11, 12, 16, 19, 20, 21, 26 (plus many labeled spare slots you can enable). 

sprinkler

 

sprinkler

Tip: use a relay board that includes flyback diodes; solenoids will kick back when powered off.

Requirements

Raspberry Pi running Linux (systemd is used for the optional service).

Python 3.x. The app stubs out dependencies if missing, but for full functionality install:

Flask, APScheduler, requests, and gpiozero. (If APScheduler isn’t present, the code’s fallback poller keeps schedules working.) 

sprinkler

Installation
# on the Pi
cd /srv   # or any directory you prefer
sudo apt-get update
sudo apt-get install -y python3 python3-pip
# (optional) python3 -m venv .venv && source .venv/bin/activate
pip3 install flask apscheduler requests gpiozero


Copy sprinkler.py into this directory. On first run it will create config.json beside the script. 

sprinkler

Quick start

Run the web UI + scheduler:

sudo ./sprinkler.py web
# then open http://<pi-ip>:8000


The default bind is 0.0.0.0:8000 (overridable via CLI). 

sprinkler

 

sprinkler

Turn a zone on/off from the CLI:

sudo ./sprinkler.py on 19 --seconds 10   # pulse for testing
sudo ./sprinkler.py off 19


(These map to PinManager.on/off and cancel any pending timer cleanly.) 

sprinkler

Configuration (persisted config.json)

Key sections:

pins: BCM → {name, mode, active_high, initial, section}. New default pins are automatically merged into older configs while preserving your entries. 

sprinkler

schedules: list of entries {id, pin, on, off, days[], enabled}. 

sprinkler

automation_enabled, system_enabled (global switches). 

sprinkler

web: {host, port} defaults to 0.0.0.0:8000. 

sprinkler

rain_delay: {enabled, threshold} with default threshold 50% chance of precip. 

sprinkler

Changes are saved atomically. 

sprinkler

CLI reference

General:

./sprinkler.py status
./sprinkler.py on <PIN> [--seconds N]
./sprinkler.py off <PIN>


Schedules:

./sprinkler.py schedule add --pin <PIN> --on HH:MM --off HH:MM --days <daily|weekdays|weekends|mon,tue|0,2,4>
./sprinkler.py schedule list [--pin <PIN>]
./sprinkler.py schedule delete --id <UUID>
./sprinkler.py schedule enable --id <UUID>
./sprinkler.py schedule disable --id <UUID>


(Validation ensures HH:MM and resolves day shorthands to integers 0=Mon..6=Sun.) 

sprinkler

 

sprinkler

 

sprinkler

Automation & time/NTP:

./sprinkler.py automation on|off
./sprinkler.py time set "YYYY-MM-DD HH:MM:SS"
./sprinkler.py ntp enable [--server pool.ntp.org]
./sprinkler.py ntp disable


sprinkler

Rain-delay:

./sprinkler.py raindelay status
./sprinkler.py raindelay enable|disable
./sprinkler.py raindelay threshold <PERCENT>


(Returns current chance and whether delay is active.) 

sprinkler

 

sprinkler

 

sprinkler

Systemd service (optional):

sudo ./sprinkler.py service install
sudo systemctl enable --now sprinkler
# …
sudo ./sprinkler.py service uninstall


The installer writes /etc/systemd/system/sprinkler.service with ExecStart=/usr/bin/python3 <here> web … and runs as root so GPIO works reliably. 

sprinkler

 

sprinkler

 

sprinkler

HTTP API
Overview

Status
GET /api/status → JSON snapshot of pins, schedules, order, and flags (powers the UI). 

sprinkler

Pin control
POST /api/pin/<int:pin>/on {seconds?: int}
POST /api/pin/<int:pin>/off
POST /api/pin/<int:pin>/name {name: string}
Reorder visible pins: POST /api/pins/reorder {order: [pin,…]}. 

sprinkler

 

sprinkler

Global switches
POST /api/system {enabled: bool} (emergency global kill/enable)
POST /api/automation {enabled: bool} (disable schedules without changing them). 

sprinkler

 

sprinkler

Schedules
Create: POST /api/schedule {pin, on, off, days[], enabled}
Update enable flag: POST /api/schedule/<id> {enabled: bool}
Delete: DELETE /api/schedule/<id>. 

sprinkler

Schedule groups
Grouped collections of schedules for UX convenience.
Create group: POST /api/schedule-groups → {id}
Add a schedule: POST /api/schedule-groups/<gid>/schedules
Update: POST /api/schedule-groups/<gid>/schedules/<sid>
Delete: DELETE /api/schedule-groups/<gid> / …/<sid>
Bulk “add all active pins” sequencing:
POST /api/schedule-groups/<gid>/add-all with {start, duration_minutes, gap_minutes, days, order} to auto-generate sequential zone runs across your active pins. 

sprinkler

 

sprinkler

Rain-delay
GET /api/rain → {enabled, threshold, probability, active}
POST /api/rain {enabled?: bool, threshold?: number}. 

sprinkler

Routes / and /settings serve the UI and pin-settings page (drag-reorder, rename). 

sprinkler

 

sprinkler

How scheduling & rain-delay work

Each schedule creates ON/OFF jobs (cron-like) via APScheduler. If APScheduler isn’t available, the code switches to a background polling loop to honor times. 

sprinkler

Before an ON event, if rain-delay is enabled and the max precipitation probability in the look-ahead window meets/exceeds your threshold, that run is skipped. 

sprinkler

 

sprinkler

Security & operations notes

The provided systemd unit runs as root (GPIO access). If you prefer a non-root user, you’ll need to grant GPIO device permissions accordingly. 

sprinkler

Config path is colocated with the script; back it up if you care about names/order/schedules. 

sprinkler

The API is unauthenticated by default; bind to LAN only and/or put it behind your own reverse proxy with auth if exposing beyond your home network. (You can change host/port in the config or via CLI.) 

sprinkler

Troubleshooting

Web UI opens but schedules don’t fire → install APScheduler: pip3 install apscheduler (fallback poller also exists, but APScheduler is preferred). 

sprinkler

Pins don’t switch → check your BCM numbers vs relay board inputs and active_high in config.json. Edits are hot-reloaded by the app’s save/merge logic. 

sprinkler

 

sprinkler

Service won’t start → journalctl -u sprinkler -e. The installer prints exactly how it writes the unit and how to enable it. 

sprinkler

Contributing

The file is deliberately self-contained: GPIO abstraction (PinManager), schedule engine (SprinklerScheduler), rain module, Flask app factory, and CLI. PRs that keep that composable structure (and don’t introduce heavy deps) are welcome. See print_info() for an at-a-glance user manual exposed on -info. 

sprinkler

 

sprinkler

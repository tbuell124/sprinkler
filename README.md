# GPIO Sprinkler Controller (Raspberry Pi)
Single-file backend (`sprinkler.py`) that drives sprinkler/valve relays on a Raspberry Pi with:
- A modern **web UI** (served by the script) to view/rename pins (zones), drag-reorder active vs spare pins, add/edit schedules, and manage rain delay.
- A **CLI** for pin control, schedules, automation, NTP/time, and a systemd **service installer**.
- A **scheduler** (APScheduler when available; safe in-process fallback otherwise).
- Optional **Rain Delay** that checks the National Weather Service **hourly** forecast and skips runs when chance of precipitation exceeds your threshold.

---

## 0) What you need
- Raspberry Pi with 64-bit Raspberry Pi OS (Bookworm or newer), networked.
- 24 VAC valves + relay hat/board (one channel per zone). Use boards with flyback diodes.
- Python 3 (preinstalled on Raspberry Pi OS).
- (Recommended) A dedicated working directory: `/srv/sprinkler-controller`.

---

## 1) Fresh install (end-to-end commands)

### A) Prepare the SD card (Windows or macOS)
1. Download **Raspberry Pi Imager**.
2. Choose **Raspberry Pi OS Lite (64-bit)**.
3. Click the ⚙️ (Advanced):
   - **Set hostname**: `sprinkler`
   - **Username**: `tybuell` (or your user)
   - **Password**: set one
   - **Enable SSH**: yes
   - **Configure Wi-Fi**: SSID, password, country
   - **Timezone**: `America/New_York`
4. Write the card, insert in Pi, power up.

> If the Pi is already running and networked, skip to B.

### B) Log into the Pi (Windows PowerShell or macOS Terminal)
```bash
ssh tybuell@sprinkler    # or ssh tybuell@<pi-ip>
C) Create the app directory and install Python deps
bash
Copy code
sudo mkdir -p /srv/sprinkler-controller
sudo chown $USER:$USER /srv/sprinkler-controller
cd /srv/sprinkler-controller

sudo apt-get update
sudo apt-get install -y python3-pip
pip3 install --break-system-packages flask apscheduler gpiozero requests
gpiozero will use real GPIO on a Pi; if it’s missing or you’re not on a Pi, the app runs in a safe mock mode.

D) Get the app code
Clone your repo or copy the single file:

Option 1 — clone

bash
Copy code
git clone https://github.com/tbuell124/sprinkler .
Option 2 — copy a single file from your computer

bash
Copy code
# from Windows PowerShell, replace <PI-IP> and path to sprinkler.py
scp .\sprinkler.py tybuell@<PI-IP>:/srv/sprinkler-controller/sprinkler.py
E) First run (creates config.json)
bash
Copy code
sudo ./sprinkler.py web --host 0.0.0.0 --port 8000
Open a browser to: http://<pi-ip>:8000
You’ll see pins (zones), schedules, and rain delay settings. Stop with Ctrl+C.

2) Configure pins, schedules, and rain delay
A) Pin layout (BCM numbering)
The default config defines 16 active slots, left-to-right to match a typical 16-channel relay board:

yaml
Copy code
Active pins: 12,16,20,21,26,19,13,6,5,11,9,10,22,27,17,4
Spare pins : 7,8,14,15,18,23,24,25
Rename pins in the UI (click a name), or edit config.json (created beside sprinkler.py) and restart.

B) Quick CLI control (useful for wiring tests)
bash
Copy code
# See states & schedules
./sprinkler.py status

# Toggle a valve (pulse test for 3 seconds)
sudo ./sprinkler.py on 19 --seconds 3
sudo ./sprinkler.py off 19
C) Add a schedule from the CLI
bash
Copy code
# Run GPIO 19 from 06:30 to 06:45 on Mon/Wed/Fri
./sprinkler.py schedule add --pin 19 --on 06:30 --off 06:45 --days mon,wed,fri

# See them (optionally filter a pin)
./sprinkler.py schedule list
./sprinkler.py schedule list --pin 19

# Enable/disable or delete
./sprinkler.py schedule disable --id <uuid>
./sprinkler.py schedule enable  --id <uuid>
./sprinkler.py schedule delete  --id <uuid>
The UI also supports building sequences across all active pins (bulk add “in order”).

D) Automation & time / NTP
bash
Copy code
# Globally pause or resume automation (schedules won’t fire when off)
./sprinkler.py automation off
./sprinkler.py automation on

# Set system time (requires sudo) and manage NTP
sudo ./sprinkler.py time set "2025-09-24 07:00:00"
sudo ./sprinkler.py ntp enable  --server pool.ntp.org
sudo ./sprinkler.py ntp disable
E) Rain delay (skip watering when rain chance high)
bash
Copy code
# Turn on rain delay and set threshold (percent chance of precip)
./sprinkler.py raindelay enable
./sprinkler.py raindelay threshold 60
./sprinkler.py raindelay status
By default the controller uses the NWS Glen Allen, VA hourly endpoint. To change your location, set "forecast_url" under "rain_delay" in config.json to your local api.weather.gov/gridpoints/.../forecast/hourly URL, then restart.

3) Run on boot with systemd (one command)
Install the service; it will use your current web.host/web.port from config:

bash
Copy code
sudo ./sprinkler.py service install
sudo systemctl enable --now sprinkler
sudo systemctl status sprinkler --no-pager
Stop/start: sudo systemctl stop|start sprinkler

Logs: sudo journalctl -u sprinkler -e --no-pager

Uninstall later:

bash
Copy code
sudo ./sprinkler.py service uninstall
4) HTTP API (for integrations)
No auth by default; keep it on your LAN or front with a reverse proxy.

Status
GET /api/status → snapshot of pins, schedules, groups, flags

Pins
POST /api/pin/<int:pin>/on?seconds=10 → "OK"
POST /api/pin/<int:pin>/off → "OK"
POST /api/pin/<int:pin>/name {"name":"Backyard"}
POST /api/pins/reorder {"active":[...], "spare":[...]}

Schedules
POST /api/schedule {"pin":19,"on":"06:30","off":"06:45","days":"mon,wed,fri","enabled":true} → { "id":"<uuid>" }
POST /api/schedule/<id> (partial update)
DELETE /api/schedule/<id>
POST /api/schedules/reorder {"order":["<id1>","<id2>",...]}

Schedule groups (UI convenience)
GET /api/schedule-groups
POST /api/schedule-groups {"name":"Front Yard"} → { "id":"<uuid>" }
POST /api/schedule-groups/<gid>/add-all (bulk sequence across active pins)

Rain Delay
GET /api/rain → {enabled,threshold,probability,active,last_checked}
POST /api/rain {"enabled":true,"threshold":60}

5) Config file (config.json) cheat-sheet
Created on first run (atomic saves). The important parts:

json
Copy code
{
  "pins": {
    "19": {"name": "Garden", "mode": "out", "active_high": true, "initial": false, "section": "active"}
  },
  "schedules": [
    {"id": "…", "pin": 19, "on": "06:30", "off": "06:45", "days": [1,3,5], "enabled": true}
  ],
  "schedule_groups": {
    "current_group_id": "<uuid>",
    "schedule_groups": [ { "id": "<uuid>", "name": "Front Yard", "schedules": [ ... ] } ]
  },
  "system_enabled": true,
  "automation_enabled": true,
  "ntp": { "enabled": true, "server": "pool.ntp.org" },
  "web": { "host": "0.0.0.0", "port": 8000 },
  "rain_delay": { "enabled": false, "threshold": 50, "forecast_url": "https://api.weather.gov/gridpoints/AKQ/42,82/forecast/hourly" }
}
Advanced: You can override the default web host without editing the file by exporting SPRINKLER_DOMAIN before running web.

6) Troubleshooting
Web UI loads but schedules don’t fire → pip3 install apscheduler (fallback exists but APScheduler is preferred).

GPIO doesn’t toggle → verify BCM pin numbers vs your relay channels; confirm active_high and wiring; test with sudo ./sprinkler.py on <pin> --seconds 2.

Service not listening on 8000 → sudo journalctl -u sprinkler -e; confirm your WorkingDirectory and ExecStart in the unit match /srv/sprinkler-controller.

Rain delay stuck inactive → pip3 install requests, ensure the Pi has outbound internet, and set a valid "forecast_url" for your location.

7) Uninstall / reset (keep OS)
bash
Copy code
# Stop service and remove it
sudo systemctl disable --now sprinkler || true
sudo rm -f /etc/systemd/system/sprinkler.service
sudo systemctl daemon-reload

# Remove app directory (and config)
sudo rm -rf /srv/sprinkler-controller

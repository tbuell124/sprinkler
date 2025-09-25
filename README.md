# GPIO Sprinkler Controller (Raspberry Pi)
A single-file backend (`sprinkler.py`) that lets you run and schedule your yard sprinkler zones from a **web page** on your home network. It drives relay channels via Raspberry Pi **GPIO** and includes a built-in scheduler, a rain-delay feature, and a simple CLI. This README is *end-to-end*—follow it exactly and you’ll be clicking buttons in your browser to water zones.

---

## ⚠️ Safety & Wiring (read first)
- You are switching **24 VAC** irrigation valves using a **relay board** (one channel per zone). Use a relay board designed for inductive loads (e.g., has **flyback diodes**).
- The app uses **BCM** pin numbering (not board numbering). Default pins = a 16-zone layout (shown below).
- Many relay boards are **active-LOW** (energize when GPIO goes **LOW**). If your relays click “on” when the app says “off”, set `"active_high": false` for those pins in `config.json`.

---

## 0) What you need
- Raspberry Pi (64-bit Raspberry Pi OS **Bookworm** or newer), on your home network.
- Power for the relay board and 24 VAC valves.
- A computer (Windows/macOS/Linux) to SSH into the Pi.
- Git installed on the Pi (usually preinstalled; if not: `sudo apt-get install -y git`).

---

## 1) Prepare or re-image the Pi (only if needed)
**New setup:** Use Raspberry Pi Imager → *Raspberry Pi OS Lite (64-bit)* → ⚙️ Advanced:
- Hostname: `sprinkler`
- Username: `tybuell` (or your user)
- Password: set one
- SSH: **Enabled**
- Wi-Fi: set SSID/password and **Timezone: America/New_York**
Boot the Pi with this card.

**Existing Pi:** skip to step 2.

---

## 2) Log in and install prerequisites
Open **Windows PowerShell** (or Terminal on macOS/Linux), then:

```bash
# SSH into the Pi (replace with your hostname or IP if needed)
ssh tybuell@sprinkler
# or: ssh tybuell@<pi-ip-address>
On the Pi:

bash
Copy code
# Create working directory
sudo mkdir -p /srv/sprinkler-controller
sudo chown $USER:$USER /srv/sprinkler-controller
cd /srv/sprinkler-controller

# System packages
sudo apt-get update
sudo apt-get install -y python3-pip

# Python libraries (safe to re-run; Flask usually comes with Raspberry Pi OS)
pip3 install --break-system-packages apscheduler gpiozero requests
# (Flask is typically in /usr/lib/python3/dist-packages on Pi OS; if not: pip3 install --break-system-packages flask)

# Let your user use GPIO without sudo (takes effect after next login)
sudo usermod -aG gpio $USER
Optional: pigpio daemon isn’t required. If you later want it:
sudo apt-get install -y pigpio && sudo systemctl enable --now pigpiod

3) Get the app
bash
Copy code
cd /srv/sprinkler-controller
git clone https://github.com/tbuell124/sprinkler .
If you ever edited the file on Windows and see “Permission denied,” strip Windows line endings and add execute bit:

bash
Copy code
sed -i 's/\r$//' sprinkler.py
chmod +x sprinkler.py
4) First run (creates config.json) and open the web app
bash
Copy code
# Easiest, always works:
sudo python3 sprinkler.py web --host 0.0.0.0 --port 8000
Now, on your computer, open a browser to http://sprinkler.local:8000.
If mDNS isn’t working: run hostname -I on the Pi and open http://<pi-ip>:8000.

You’ll see:

Settings page with 16 active “Slot” pins and spare pins

Buttons to Run/Stop zones, rename zones, drag-reorder active vs spare pins

Schedules list and Schedule Groups (for organizing schedules)

Rain Delay controls (enable & adjust threshold)

Stop the server with Ctrl+C in the SSH session.

5) Configure your zones (GPIO → relay channels)
Default BCM pin map (left-to-right for a 16-channel relay board):

java
Copy code
Active pins (Slots 1–16):
12, 16, 20, 21, 26, 19, 13, 6, 5, 11, 9, 10, 22, 27, 17, 4

Spare pins:
7, 8, 14, 15, 18, 23, 24, 25
You can rename and reorder in the UI. All data is persisted in config.json (auto-created next to sprinkler.py the first time you run).

Tip: If your relay board is active-LOW, set "active_high": false per pin in config.json, then restart the app.

6) Quick smoke tests (CLI)
From the Pi:

bash
Copy code
cd /srv/sprinkler-controller

# Status snapshot
python3 sprinkler.py status

# Pulse a zone for 3 seconds (change 19 to your wired pin)
sudo python3 sprinkler.py on 19 --seconds 3
sudo python3 sprinkler.py off 19
If you hear the relay click and the valve reacts, you’re good.

7) Scheduling & the web UI
You can add schedules in the UI or via CLI. In the UI:

Add Schedule: choose a pin, On and Off times (24h), pick days, set Enabled.

Schedule Groups: create a group per yard area; you can Add All active pins to a group to build a sequential run:

Provide start time, duration per zone, gap minutes, and days. It will auto-create a chain (e.g., Slot 1 for 10 min, Slot 2 for 10 min, …).

CLI equivalents:

bash
Copy code
# Add a schedule (Mon/Wed/Fri 06:30–06:45 on pin 19)
python3 sprinkler.py schedule add --pin 19 --on 06:30 --off 06:45 --days mon,wed,fri

# See and manage
python3 sprinkler.py schedule list
python3 sprinkler.py schedule disable --id <uuid>
python3 sprinkler.py schedule enable  --id <uuid>
python3 sprinkler.py schedule delete  --id <uuid>
Automation switch (global on/off for schedules):

bash
Copy code
python3 sprinkler.py automation off
python3 sprinkler.py automation on
Device time / NTP (recommended to keep time correct):

bash
Copy code
sudo python3 sprinkler.py time set "2025-09-24 07:00:00"
sudo python3 sprinkler.py ntp enable  --server pool.ntp.org
# or: sudo python3 sprinkler.py ntp disable
8) Rain Delay (skip watering when rain is likely)
The app can skip ON events when the forecasted chance of precipitation meets/exceeds your threshold.

Enable & tune:

bash
Copy code
python3 sprinkler.py raindelay enable
python3 sprinkler.py raindelay threshold 60   # 60% chance required to skip
python3 sprinkler.py raindelay status
By default it checks Glen Allen, VA. To use your location:

Find your lat,lon and hit:

php-template
Copy code
https://api.weather.gov/points/<lat>,<lon>
In the JSON response, copy properties.forecastHourly (it looks like /gridpoints/<OFFICE>/<X>,<Y>/forecast/hourly).

Put that URL into config.json under:

json
Copy code
"rain_delay": {
  "enabled": true,
  "threshold": 60,
  "forecast_url": "https://api.weather.gov/gridpoints/<OFFICE>/<X>,<Y>/forecast/hourly"
}
Restart the app (or the service) after editing.

9) Run on boot with systemd (recommended)
Once you’ve proven it works in the foreground:

bash
Copy code
# Install the service (writes /etc/systemd/system/sprinkler.service)
sudo python3 sprinkler.py service install

# Enable and start
sudo systemctl enable --now sprinkler
sudo systemctl status sprinkler --no-pager

# Verify the port is listening
ss -lntp | grep ':8000' || echo "Not listening on :8000 yet"
It will run:

swift
Copy code
/usr/bin/python3 /srv/sprinkler-controller/sprinkler.py web --host 0.0.0.0 --port 8000
Stop/Start/Logs:

bash
Copy code
sudo systemctl stop sprinkler
sudo systemctl start sprinkler
sudo journalctl -u sprinkler -e --no-pager
Uninstall later:

bash
Copy code
sudo python3 sprinkler.py service uninstall
10) Open the web app from your browser
http://sprinkler.local:8000 (mDNS) or http://<pi-ip>:8000

Control zones (Run/Stop), rename zones, drag-reorder, add/edit schedules, configure Rain Delay.

Security: There is no built-in auth. Keep it on your LAN. If you expose it, front it with a reverse proxy that adds TLS + auth. (You can also bind to a specific LAN IP instead of 0.0.0.0.)

11) Updating the app
bash
Copy code
cd /srv/sprinkler-controller
git pull
# If running as a service:
sudo systemctl restart sprinkler
Your config.json (pins, names, schedules) lives next to sprinkler.py. Back it up if you care about it.

12) Troubleshooting
“sudo: ./sprinkler.py: command not found”
Use sudo python3 sprinkler.py … (doesn’t require execute bit) or:

bash
Copy code
sed -i 's/\r$//' sprinkler.py
chmod +x sprinkler.py
“Permission denied” when ./sprinkler.py
Run chmod +x sprinkler.py or call with python3 as shown.

Web UI loads but schedules don’t fire
pip3 install --break-system-packages apscheduler

GPIO doesn’t toggle relays
Confirm BCM pin numbers match your relay channels, and set "active_high": false if your relays are active-LOW. Pulse test:

bash
Copy code
sudo python3 sprinkler.py on <pin> --seconds 2
Service running but no UI
sudo journalctl -u sprinkler -e --no-pager then confirm the paths in the unit and that sprinkler.py exists in /srv/sprinkler-controller.

Something else already on port 8000
ss -lntp | grep ':8000' to see the process. Stop or change the port (--port 8001 and update the service if needed).

Old dev server on :5000 (Node/PM2) won’t die
Kill it and disable any PM2 user services that resurrect on boot:

bash
Copy code
ss -lntp | grep ':5000'
# If it's node/npm under PM2:
su - tybuell -c 'pm2 delete all || true'
sudo systemctl disable --now pm2-tybuell.service 2>/dev/null || true
sudo systemctl daemon-reload
13) Full CLI reference
text
Copy code
# Web UI + scheduler
sudo python3 sprinkler.py web [--host 0.0.0.0] [--port 8000]

# Status
python3 sprinkler.py status

# Pin control
sudo python3 sprinkler.py on <PIN> [--seconds N]
sudo python3 sprinkler.py off <PIN>

# Schedules
python3 sprinkler.py schedule add --pin <PIN> --on HH:MM --off HH:MM --days <daily|weekdays|weekends|mon,tue|0,2,4>
python3 sprinkler.py schedule list [--pin <PIN>]
python3 sprinkler.py schedule delete   --id <UUID>
python3 sprinkler.py schedule enable   --id <UUID>
python3 sprinkler.py schedule disable  --id <UUID>
python3 sprinkler.py schedules reorder --order <id1,id2,...>

# Automation toggle
python3 sprinkler.py automation on|off

# Time & NTP
sudo python3 sprinkler.py time set "YYYY-MM-DD HH:MM:SS"
sudo python3 sprinkler.py ntp enable  [--server pool.ntp.org]
sudo python3 sprinkler.py ntp disable

# Rain Delay
python3 sprinkler.py raindelay status
python3 sprinkler.py raindelay enable|disable
python3 sprinkler.py raindelay threshold <PERCENT>

# Systemd service
sudo python3 sprinkler.py service install
sudo python3 sprinkler.py service uninstall
14) Appendix: Default config.json (auto-generated on first run)
json
Copy code
{
  "pins": {
    "12": {"name":"Slot 1 - Driveway","mode":"out","active_high":true,"initial":false,"section":"active"},
    "16": {"name":"Slot 2 - Back Middle","mode":"out","active_high":true,"initial":false,"section":"active"},
    "20": {"name":"Slot 3 - Deck Corner","mode":"out","active_high":true,"initial":false,"section":"active"},
    "21": {"name":"Slot 4 - House Corner","mode":"out","active_high":true,"initial":false,"section":"active"},
    "26": {"name":"Slot 5 - Front Middle","mode":"out","active_high":true,"initial":false,"section":"active"},
    "19": {"name":"Slot 6 - Garden","mode":"out","active_high":true,"initial":false,"section":"active"},
    "13": {"name":"Slot 7 - Side Back","mode":"out","active_high":true,"initial":false,"section":"active"},
    "6":  {"name":"Slot 8 - Walkway","mode":"out","active_high":true,"initial":false,"section":"active"},
    "5":  {"name":"Slot 9 - Front Right","mode":"out","active_high":true,"initial":false,"section":"active"},
    "11": {"name":"Slot 10 - Side Front","mode":"out","active_high":true,"initial":false,"section":"active"},
    "9":  {"name":"Slot 11 - Side Middle","mode":"out","active_high":true,"initial":false,"section":"active"},
    "10": {"name":"Slot 12","mode":"out","active_high":true,"initial":false,"section":"active"},
    "22": {"name":"Slot 13","mode":"out","active_high":true,"initial":false,"section":"active"},
    "27": {"name":"Slot 14","mode":"out","active_high":true,"initial":false,"section":"active"},
    "17": {"name":"Slot 15","mode":"out","active_high":true,"initial":false,"section":"active"},
    "4":  {"name":"Slot 16","mode":"out","active_high":true,"initial":false,"section":"active"},
    "7":  {"name":"Spare 3","mode":"out","active_high":true,"initial":false,"section":"spare"},
    "8":  {"name":"Spare 4","mode":"out","active_high":true,"initial":false,"section":"spare"},
    "14": {"name":"Spare 5","mode":"out","active_high":true,"initial":false,"section":"spare"},
    "15": {"name":"Spare 6","mode":"out","active_high":true,"initial":false,"section":"spare"},
    "18": {"name":"Spare 7","mode":"out","active_high":true,"initial":false,"section":"spare"},
    "23": {"name":"Spare 8","mode":"out","active_high":true,"initial":false,"section":"spare"},
    "24": {"name":"Spare 9","mode":"out","active_high":true,"initial":false,"section":"spare"},
    "25": {"name":"Spare 10","mode":"out","active_high":true,"initial":false,"section":"spare"}
  },
  "schedules": [],
  "automation_enabled": true,
  "system_enabled": true,
  "ntp": { "enabled": true, "server": "pool.ntp.org" },
  "web": { "host": "0.0.0.0", "port": 8000 },
  "rain_delay": { "enabled": false, "threshold": 50 }
}

#!/usr/bin/env python3
"""
sprinkler.py

This script provides a fully‑automated GPIO sprinkler controller designed for
Raspberry Pi.  It builds upon the earlier example by adding a preset
schedule and accommodating the specific set of pins used by the
requester. It also exposes a simple CLI and optional web UI for monitoring
and adjusting state at runtime.

Key features:

  • Pins 5, 6, 11, 12, 16, 19, 20, 21 and 26 are defined in the default
    configuration.  Pin 19 is designated as the garden zone.
  • A `preset season` command builds a schedule where each active zone
    runs sequentially for 30 minutes every day, except the garden zone
    (19) which only runs on Tuesday, Thursday and Saturday.
  • Existing CLI commands remain available for manual schedule
    management, on/off control, automation toggling and time/NTP
    configuration.
  • A built in web UI (Flask) can be launched via `web` to view and
    adjust pins, schedules and time settings in a browser.

See the bottom of this file for a concise usage manual.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import uuid
import re
from datetime import datetime, timedelta
from typing import Dict, List

import subprocess

# We attempt to import the popular `requests` library for HTTP
# fetching.  If it isn’t available, a fallback using urllib will be
# used instead.  This allows the rain delay feature to retrieve
# weather data without requiring additional dependencies on the
# Raspberry Pi.  Should both approaches fail, rain delay checks will
# gracefully return None.
try:
    import requests  # type: ignore
except Exception:
    requests = None  # type: ignore

# =========================
# GPIO backend (gpiozero)
# =========================
USE_MOCK = False
try:
    # Attempt to import the real gpiozero classes and configure the
    # native pin factory.  This will succeed only on a Raspberry Pi
    # with the gpiozero library installed.
    from gpiozero import Device, DigitalOutputDevice  # type: ignore
    from gpiozero.pins.native import NativeFactory  # type: ignore
    Device.pin_factory = NativeFactory()
except Exception:
    try:
        # Fallback to the gpiozero mock factory.  This will succeed if
        # gpiozero is installed but you're not on a Pi.
        from gpiozero import Device, DigitalOutputDevice  # type: ignore
        from gpiozero.pins.mock import MockFactory  # type: ignore
        Device.pin_factory = MockFactory()
        USE_MOCK = True
    except Exception:
        # As a last resort, define lightweight dummy classes.  These
        # implement the minimal interface used by this script (on/off
        # methods and a value attribute) so that the code remains
        # importable and testable even if gpiozero is entirely absent.
        class DummyDevice:
            pin_factory = None

        class DummyDigitalOutputDevice(DummyDevice):
            def __init__(self, pin, active_high=True, initial_value=False):
                self.pin = pin
                self.active_high = active_high
                # Represent internal state as a boolean; gpiozero uses
                # value (1/0) but we map to bool in get_state().
                self.value = 1 if initial_value else 0
            def on(self):
                # Set output high respecting active_high flag
                self.value = 1
            def off(self):
                # Set output low respecting active_high flag
                self.value = 0

        Device = DummyDevice  # type: ignore
        DigitalOutputDevice = DummyDigitalOutputDevice  # type: ignore
        USE_MOCK = True

# =========================
# Scheduling (APScheduler)
# =========================
try:
    from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore
    from apscheduler.triggers.cron import CronTrigger  # type: ignore
    # Indicate that real APScheduler is available
    HAS_APSCHEDULER = True
except Exception:
    # If apscheduler is not available (e.g., in certain development
    # environments), define minimal stub classes so the module can be
    # imported without errors.  These stubs do nothing but satisfy the
    # interface used by this script.  We also mark that we are using a
    # fallback scheduler.
    class BackgroundScheduler:
        def __init__(self, daemon: bool = True):
            self._jobs = []
        def start(self):
            # stub does nothing
            pass
        def shutdown(self, wait: bool = False):
            pass
        def add_job(self, func, args=None, trigger=None, id=None, replace_existing=False, misfire_grace_time=None):
            # record jobs for introspection if needed
            self._jobs.append({"id": id, "func": func, "args": args, "trigger": trigger})
            return None
        def get_jobs(self):
            return self._jobs
        def remove_job(self, job_id):
            self._jobs = [j for j in self._jobs if j.get("id") != job_id]
    class CronTrigger:
        def __init__(self, day_of_week=None, hour=None, minute=None):
            self.day_of_week = day_of_week
            self.hour = hour
            self.minute = minute
    # Real APScheduler not available; we will use fallback scheduling
    HAS_APSCHEDULER = False

# =========================
# Web (Flask)
# =========================
try:
    from flask import Flask, jsonify, request, Response, redirect  # type: ignore
except Exception:
    # Provide minimal stubs when Flask is not installed.  The web
    # interface will not be available, but the CLI can still be used
    # without import errors.
    class Flask:
        def __init__(self, name):
            self.name = name
        # Decorators for route, get and post simply wrap the function and
        # return it unchanged.  No routing is performed.
        def route(self, *args, **kwargs):
            def decorator(fn):
                return fn
            return decorator
        def get(self, *args, **kwargs):
            return self.route(*args, **kwargs)
        def post(self, *args, **kwargs):
            return self.route(*args, **kwargs)
        def run(self, host=None, port=None):
            print("Flask app would run on", host, port)
    def jsonify(obj):
        return obj
    def redirect(location, code=302):
        return location
    # Simulate Flask request with args and get_json method
    class _Request:
        def __init__(self):
            self.args = {}
        def get_json(self, force=False):
            return {}
    request = _Request()
    class Response(str):
        def __new__(cls, data, mimetype=None):
            return str.__new__(cls, data)

# Path to our persistent configuration.  It lives next to this script.
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
# Re‑entrant lock used to guard shared state.
LOCK = threading.RLock()

# Simple settings page for editing pin names
SETTINGS_HTML = """<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\">
<title>Sprinkler Settings</title>
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0b0f14;color:#e6edf3;padding:16px}
a{color:#9fb1c1}
.list{display:flex;flex-direction:column;gap:8px}
.pin-row{display:flex;align-items:center;gap:10px;background:#10161d;border:1px solid #1e2936;border-radius:8px;padding:8px}
.pin-row span{min-width:80px}
.pin-row input{flex:1;background:transparent;color:#e6edf3;border:1px solid #1e2936;border-radius:6px;padding:4px 6px}
.cols{display:flex;gap:24px;flex-wrap:wrap}
.col{flex:1;min-width:200px}
</style>
</head>
<body>
<h1>Pin Settings</h1>
<div class=\"cols\">
  <div class=\"col\">
    <h2>Active</h2>
    <div id=\"activeList\" class=\"list\"></div>
  </div>
  <div class=\"col\">
    <h2>Inactive</h2>
    <div id=\"inactiveList\" class=\"list\"></div>
  </div>
</div>
<p><a href=\"/\">Back</a></p>
<script>
let dragEl;
function makeRow(p){
  const row=document.createElement('div'); row.className='pin-row'; row.draggable=true; row.dataset.pin=p.pin;
  const label=document.createElement('span'); label.textContent='GPIO '+p.pin; row.appendChild(label);
  const input=document.createElement('input'); input.value=p.name || ('Pin '+p.pin);
  input.addEventListener('change',()=>{ const name=input.value.trim(); fetch('/api/pin/'+p.pin+'/name',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})}); });
  row.appendChild(input);
  return row;
}
function sendPinOrder(){
  const active=[...document.querySelectorAll('#activeList .pin-row')].map(r=>parseInt(r.dataset.pin,10));
  const spare=[...document.querySelectorAll('#inactiveList .pin-row')].map(r=>parseInt(r.dataset.pin,10));
  fetch('/api/pins/reorder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({active,spare})});
}
function setupDrag(list){
  list.addEventListener('dragstart',e=>{ dragEl=e.target.closest('.pin-row'); e.dataTransfer.effectAllowed='move'; });
  list.addEventListener('dragover',e=>{ e.preventDefault(); const target=e.target.closest('.pin-row'); if(!target){ list.appendChild(dragEl); return;} if(target===dragEl) return; const rect=target.getBoundingClientRect(); const next=(e.clientY-rect.top)/(rect.bottom-rect.top)>0.5; target.parentElement.insertBefore(dragEl,next?target.nextSibling:target); });
  list.addEventListener('drop',e=>{ e.preventDefault(); sendPinOrder(); });
}
fetch('/api/status').then(r=>r.json()).then(data=>{
  const active=document.getElementById('activeList');
  const inactive=document.getElementById('inactiveList');
  data.pins_slots.forEach(p=>active.appendChild(makeRow(p)));
  data.pins_spares.forEach(p=>inactive.appendChild(makeRow(p)));
});
setupDrag(document.getElementById('activeList'));
setupDrag(document.getElementById('inactiveList'));
</script>
</body>
</html>
"""

# ---------- Default Config ----------
#
# All pins are defined with BCM numbers.  Each entry can be customised
# later by editing the generated config.json.  `active_high` determines
# whether writing a logical 1 energises the relay.  `initial` controls
# whether the zone is active at startup (all zones are off by default).

DEFAULT_CONFIG = {
"pins": {
    # ---- Slots 1–16 (left -> right exactly as on the board) ----
    "12": {"name": "Slot 1 - Driveway",  "mode": "out", "active_high": True, "initial": False, "section": "active"},
    "16": {"name": "Slot 2 - Back Middle",  "mode": "out", "active_high": True, "initial": False, "section": "active"},
    "20": {"name": "Slot 3 - Deck Corner",  "mode": "out", "active_high": True, "initial": False, "section": "active"},
    "21": {"name": "Slot 4 - House Corner",  "mode": "out", "active_high": True, "initial": False, "section": "active"},
    "26": {"name": "Slot 5 - Front Middle",  "mode": "out", "active_high": True, "initial": False, "section": "active"},
    "19": {"name": "Slot 6 - Garden",  "mode": "out", "active_high": True, "initial": False, "section": "active"},
    "13": {"name": "Slot 7 - Side Back",  "mode": "out", "active_high": True, "initial": False, "section": "active"},
    "6":  {"name": "Slot 8 - Walkway",  "mode": "out", "active_high": True, "initial": False, "section": "active"},
    "5":  {"name": "Slot 9 - Front Right",  "mode": "out", "active_high": True, "initial": False, "section": "active"},
    "11": {"name": "Slot 10 - Side Front", "mode": "out", "active_high": True, "initial": False, "section": "active"},
    "9":  {"name": "Slot 11 - Side Middle", "mode": "out", "active_high": True, "initial": False, "section": "active"},
    "10": {"name": "Slot 12", "mode": "out", "active_high": True, "initial": False, "section": "active"},
    "22": {"name": "Slot 13", "mode": "out", "active_high": True, "initial": False, "section": "active"},
    "27": {"name": "Slot 14", "mode": "out", "active_high": True, "initial": False, "section": "active"},
    "17": {"name": "Slot 15", "mode": "out", "active_high": True, "initial": False, "section": "active"},
    "4":  {"name": "Slot 16", "mode": "out", "active_high": True, "initial": False, "section": "active"},

    # ---- Remaining controllable GPIOs as sequential Spares ----
    # (All BCMs commonly available on the 40-pin header except those already used above)
    "7":  {"name": "Spare 3",  "mode": "out", "active_high": True, "initial": False, "section": "spare"},
    "8":  {"name": "Spare 4",  "mode": "out", "active_high": True, "initial": False, "section": "spare"},
    "14": {"name": "Spare 5",  "mode": "out", "active_high": True, "initial": False, "section": "spare"},
    "15": {"name": "Spare 6",  "mode": "out", "active_high": True, "initial": False, "section": "spare"},
    "18": {"name": "Spare 7",  "mode": "out", "active_high": True, "initial": False, "section": "spare"},
    "23": {"name": "Spare 8",  "mode": "out", "active_high": True, "initial": False, "section": "spare"},
    "24": {"name": "Spare 9",  "mode": "out", "active_high": True, "initial": False, "section": "spare"},
    "25": {"name": "Spare 10", "mode": "out", "active_high": True, "initial": False, "section": "spare"}
},
    # Schedule definitions live in this list.  Preset commands will
    # repopulate this list.  Each entry contains: id, pin, on, off,
    # days (list of weekday numbers) and enabled.
    "schedules": [],
    "automation_enabled": True,
    "system_enabled": True,
    # NTP options for timedatectl integration.  Enabled controls
    # whether NTP is active; server selects the pool or host for
    # synchronisation.
    "ntp": {"enabled": True, "server": "pool.ntp.org"},
    # Web server defaults.  You may override at runtime via CLI.
    "web": {"host": "0.0.0.0", "port": 8000},

    # Rain delay configuration.  When enabled, the controller will check
    # the National Weather Service hourly forecast for Glen Allen, VA
    # prior to each scheduled ON event.  If the maximum chance of
    # precipitation over the next 24 hours meets or exceeds the
    # configured threshold, the zone will be skipped for that event.
    # You can adjust these values via the CLI (raindelay commands) or
    # through the web interface.  See the usage manual below for more
    # details.
    "rain_delay": {
        "enabled": False,
        "threshold": 50  # percent chance of precipitation
    },
}

# Weekday names for human friendly display.  Monday=0..Sunday=6
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class PinManager:
    """Wraps gpiozero DigitalOutputDevice for each configured pin."""
    def __init__(self, config: dict):
        self.cfg = config
        self.devices: Dict[int, DigitalOutputDevice] = {}
        self.pending_timers: Dict[int, threading.Timer] = {}
        self._build_from_config(config)

    def _build_from_config(self, config: dict):
        for pin_str, meta in config.get("pins", {}).items():
            pin = int(pin_str)
            if meta.get("mode", "out") != "out":
                continue
            dev = DigitalOutputDevice(
                pin,
                active_high=bool(meta.get("active_high", True)),
                initial_value=bool(meta.get("initial", False)),
            )
            self.devices[pin] = dev

    def list_pins(self):
        out = []
        for pin, dev in self.devices.items():
            out.append(
                {
                    "pin": pin,
                    "is_active": bool(dev.value == (1 if dev.active_high else 0)),
                    "value": int(dev.value),
                    "active_high": dev.active_high,
                }
            )
        return out

    def on(self, pin: int, seconds: int = None):
        """Activate a pin and optionally schedule it to turn off."""
        with LOCK:
            if not self.cfg.get("system_enabled", True):
                return
            dev = self.devices[int(pin)]
            # Cancel any existing timer for this pin before activating it.
            # Otherwise a previously scheduled callback could turn the pin
            # off immediately after we turn it on, causing overlapping timers
            # to fight each other.
            self._cancel_timer(pin)
            dev.on()
            if seconds is not None and seconds > 0:
                t = threading.Timer(seconds, self.off, args=(pin,))
                t.daemon = True
                # Store the timer before starting it so a very short timer
                # can't fire before being recorded.
                self.pending_timers[pin] = t
                t.start()

    def off(self, pin: int):
        """Deactivate a pin immediately."""
        with LOCK:
            dev = self.devices[int(pin)]
            dev.off()
            self._cancel_timer(pin)

    def _cancel_timer(self, pin: int):
        t = self.pending_timers.pop(pin, None)
        if t:
            try:
                t.cancel()
            except Exception:
                pass

    def get_state(self, pin: int) -> bool:
        dev = self.devices[int(pin)]
        return bool(dev.value)


def load_config() -> dict:
    """Load the JSON config from disk, creating it with defaults if needed."""
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    with open(CONFIG_PATH, "r") as f:
        cfg = json.load(f)
    # Merge missing top level keys
    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = v
    # Merge any new pins from the default configuration.  This allows
    # additional GPIO pins to appear in the UI even if the config.json
    # pre-dates their introduction.  Existing entries are preserved.
    for pin, meta in DEFAULT_CONFIG.get("pins", {}).items():
        if pin not in cfg.get("pins", {}):
            cfg.setdefault("pins", {})[pin] = meta
    return cfg


def save_config(cfg: dict):
    """Atomically save the configuration to disk."""
    with LOCK:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_PATH)


class SprinklerScheduler:
    """Manage cron jobs for each schedule entry with rain delay support."""

    def __init__(self, pinman: PinManager, cfg: dict, rain: RainDelayManager | None = None):
        self.pinman = pinman
        self.cfg = cfg
        # Optional RainDelayManager; if provided, ON jobs will consult
        # this manager before activating a pin.  If None, no rain
        # checking occurs.
        self.rain = rain
        # Use the system's local timezone for all scheduled jobs where
        # supported.  Some older versions or stub implementations of
        # BackgroundScheduler do not accept a `timezone` keyword argument.
        # Attempt to supply the local timezone; if unsupported, fall back
        # to the default behaviour (which should use local time by default)【559723678597216†L42-L46】.
        local_tz = None
        try:
            local_tz = datetime.now().astimezone().tzinfo
        except Exception:
            local_tz = None
        try:
            self.sched = BackgroundScheduler(daemon=True)
        except TypeError:
            # fallback for environments where BackgroundScheduler
            # doesn't accept a timezone parameter
            self.sched = BackgroundScheduler(daemon=True)

        # Determine whether we're running with the real APScheduler or a
        # fallback stub.  When the stub is used, scheduled jobs are not
        # executed, so we implement our own polling loop to turn pins
        # on and off at the appropriate times.  See issue raised in
        # user feedback regarding schedules not firing.
        self.use_fallback = not HAS_APSCHEDULER
        # Polling thread state for fallback scheduler
        self._poll_thread: threading.Thread | None = None
        self._stop_poll = False

    def start(self):
        self.sched.start()
        self.reload_jobs()
        # If using fallback scheduling, start a background thread to
        # poll for schedule events every minute.  Without this, jobs
        # defined via the stub BackgroundScheduler will never run.
        if self.use_fallback:
            self._start_poll_thread()

    def shutdown(self):
        self.sched.shutdown(wait=False)
        # Stop polling thread if running
        if self.use_fallback:
            self._stop_poll = True
            # Wait briefly for thread to exit
            t = self._poll_thread
            if t and t.is_alive():
                try:
                    t.join(timeout=1.0)
                except Exception:
                    pass

    def _start_poll_thread(self) -> None:
        """Spawn a thread that polls the schedule configuration every minute.

        When APScheduler is unavailable (fallback mode), this thread
        periodically checks the current day of week and time against the
        configured schedules.  If a schedule is enabled and the current
        time matches its on or off time, the corresponding action is
        executed (respecting rain delay and automation settings).
        """
        if self._poll_thread and self._poll_thread.is_alive():
            return
        self._stop_poll = False
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    @staticmethod
    def _minutes_since_midnight(t: str) -> int:
        """Return minutes since midnight for HH:MM strings."""
        parts = t.split(":")
        if len(parts) != 2:
            raise ValueError("Bad time format")
        h, m = map(int, parts)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError("Bad time value")
        return h * 60 + m

    def _poll_loop(self) -> None:
        """Fallback scheduling loop.

        Runs in a background thread to turn pins on or off at the
        configured times when APScheduler isn't available.  The loop
        wakes up roughly every 30 seconds and checks the current
        hour:minute and weekday against each active schedule.  It
        respects the global automation_enabled flag and rain delay via
        the _maybe_on() wrapper.  Off events directly call the
        PinManager.off() method.
        """
        import time
        while not self._stop_poll:
            try:
                now = datetime.now()
                # Python weekday(): Monday=0, Sunday=6 which matches
                # our backend's convention.
                day_idx = now.weekday()
                current_time = now.strftime("%H:%M")
                # Acquire lock to safely read config and state
                with LOCK:
                    # Skip all events when automation or system is disabled
                    if not self.cfg.get("automation_enabled", True) or not self.cfg.get("system_enabled", True):
                        pass
                    else:
                        for entry in self.cfg.get("schedules", []):
                            if not entry.get("enabled", True):
                                continue
                            try:
                                pin = int(entry["pin"])
                            except Exception:
                                continue
                            days = entry.get("days")
                            if days is None:
                                days = list(range(7))
                            try:
                                # Coerce days to ints; ignore invalid
                                day_list = [int(d) for d in days]
                            except Exception:
                                day_list = []
                            on_time = str(entry.get("on", "")).strip()
                            off_time = str(entry.get("off", "")).strip()
                            try:
                                on_min = self._minutes_since_midnight(on_time)
                                off_min = self._minutes_since_midnight(off_time)
                            except Exception:
                                continue
                            for di in day_list:
                                off_day = di if off_min > on_min else (di + 1) % 7
                                if day_idx == di and on_time == current_time:
                                    # Attempt to turn on the pin; handle rain delay
                                    try:
                                        self._maybe_on(pin)
                                    except Exception:
                                        pass
                                if day_idx == off_day and off_time == current_time:
                                    # Turn off without rain delay
                                    try:
                                        self.pinman.off(pin)
                                    except Exception:
                                        pass
            except Exception:
                # Log any unexpected error but continue looping
                try:
                    print("[PollLoopError]", sys.exc_info()[1])
                except Exception:
                    pass
            # Sleep until approximately the next minute to reduce CPU usage.
            # Sleep 30 seconds as a compromise between precision and overhead.
            for _ in range(30):
                if self._stop_poll:
                    break
                time.sleep(1)

    def _maybe_on(self, pin: int):
        """Called by scheduled ON jobs; respects rain delay."""
        if not self.cfg.get("system_enabled", True):
            return
        try:
            if self.rain and self.rain.should_delay():
                # Skip watering due to rain delay; log to stdout for
                # debugging but do not raise exceptions.
                print(f"[RAIN DELAY] Skipping watering for pin {pin}")
                return
        except Exception as e:
            # On unexpected errors, proceed with watering rather than
            # silently skipping.  Log the exception for visibility.
            print(f"[RainDelayError] {e}; proceeding with watering")
        # No delay; activate the pin
        self.pinman.on(pin)

    def reload_jobs(self):
        """Rebuild all APScheduler jobs based on the current schedule config.

        This method first attempts to remove all existing jobs using the
        scheduler's remove_job() API.  In case the job object itself
        provides a remove() method (e.g., on older APScheduler versions), we
        fall back to that.  When rebuilding the jobs, any invalid
        schedule entry (malformed times or days) is skipped gracefully
        with an error message to stdout instead of raising an
        exception.  If automation is disabled, no jobs are added.
        """
        # Remove all existing jobs using the scheduler API; fall back to the
        # job object's own remove() method if remove_job() is unavailable.
        for job in list(self.sched.get_jobs()):
            try:
                # Prefer the scheduler's remove_job() method
                if hasattr(self.sched, "remove_job"):
                    self.sched.remove_job(job.id)
                else:
                    job.remove()
            except Exception:
                # Ignore any errors during job removal to avoid
                # disrupting the reload process.
                try:
                    job.remove()
                except Exception:
                    pass
        if not self.cfg.get("automation_enabled", True) or not self.cfg.get("system_enabled", True):
            return
        for entry in self.cfg.get("schedules", []):
            if not entry.get("enabled", True):
                continue
            try:
                pin = int(entry["pin"])
                on_parts = entry["on"].strip().split(":")
                off_parts = entry["off"].strip().split(":")
                if len(on_parts) != 2 or len(off_parts) != 2:
                    raise ValueError("Bad time format")
                on_hour, on_minute = map(int, on_parts)
                off_hour, off_minute = map(int, off_parts)
                on_min_total = on_hour * 60 + on_minute
                off_min_total = off_hour * 60 + off_minute
                days = entry.get("days", list(range(7)))
                if days is None:
                    days = list(range(7))
                for d in days:
                    di = int(d)
                    # ON job; use _maybe_on wrapper if rain manager provided
                    trig_on = CronTrigger(day_of_week=di, hour=on_hour, minute=on_minute)
                    self.sched.add_job(
                        self._maybe_on,
                        args=[pin],
                        trigger=trig_on,
                        id=f"{entry['id']}-on-{di}",
                        replace_existing=True,
                        misfire_grace_time=300,
                    )
                    # OFF job; if off time precedes or equals on time,
                    # schedule for the following day.
                    off_day = di if off_min_total > on_min_total else (di + 1) % 7
                    trig_off = CronTrigger(day_of_week=off_day, hour=off_hour, minute=off_minute)
                    self.sched.add_job(
                        self.pinman.off,
                        args=[pin],
                        trigger=trig_off,
                        id=f"{entry['id']}-off-{off_day}",
                        replace_existing=True,
                        misfire_grace_time=300,
                    )
            except Exception as e:
                # Log and skip invalid entries; do not raise
                try:
                    print(f"[SchedulerError] Skipping schedule {entry.get('id','?')}: {e}")
                except Exception:
                    pass


# Utility functions to build preset schedules

def build_season_schedules() -> List[dict]:
    """Construct the regular season schedule.

    For pins 5,6,11,12,16,20,21 and 26, each runs for 30 minutes in
    sequence starting at 06:00.  Pin 19 (the garden) runs for 30 minutes
    starting at 10:00 but only on Tuesday, Thursday and Saturday.
    """
    order = [5, 6, 11, 12, 16, 20, 21, 26]
    base_start = datetime.strptime("06:00", "%H:%M")
    dur = timedelta(minutes=30)
    schedules = []
    # Main zones on all days
    for i, p in enumerate(order):
        on_time = (base_start + i * dur).strftime("%H:%M")
        off_time = (base_start + (i + 1) * dur).strftime("%H:%M")
        schedules.append(
            {
                "id": str(uuid.uuid4()),
                "pin": p,
                "on": on_time,
                "off": off_time,
                "days": list(range(7)),
                "enabled": True,
            }
        )
    # Garden zone (19) only Tue/Thu/Sat after the others
    garden_on = (base_start + len(order) * dur).strftime("%H:%M")
    garden_off = (base_start + (len(order) + 1) * dur).strftime("%H:%M")
    schedules.append(
        {
            "id": str(uuid.uuid4()),
            "pin": 19,
            "on": garden_on,
            "off": garden_off,
            "days": [1, 3, 5],  # Tuesday=1, Thursday=3, Saturday=5
            "enabled": True,
        }
    )
# Helpers to run system commands (timedatectl, etc.)
def run_cmd(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


class RainDelayManager:
    """Encapsulate logic for fetching rain probability and deciding delays.

    The controller uses the National Weather Service (weather.gov) hourly
    forecast for Glen Allen, VA to determine whether scheduled runs
    should be skipped due to high chance of rain.  At construction
    time this manager reads the forecast URL from a constant.  You can
    change the location by editing the `forecast_url` attribute below.
    """

    # Pre‑determined forecast endpoint for Glen Allen, VA (AKQ grid
    # point).  See README/manual for details on locating this URL.
    DEFAULT_FORECAST_URL = "https://api.weather.gov/gridpoints/AKQ/42,82/forecast/hourly"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        # Use the default forecast URL; advanced users may override this
        # by adding a `forecast_url` key under rain_delay in config.json.
        self.forecast_url = cfg.get("rain_delay", {}).get("forecast_url", self.DEFAULT_FORECAST_URL)
        self._last_prob: float | None = None
        self._last_checked: str | None = None

    def _http_get(self, url: str) -> str:
        """Fetch a URL and return the body as text.

        Tries requests first, then falls back to urllib.  Raises on
        failure so callers can handle exceptions.
        """
        # Prefer requests if available
        if requests:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "sprinkler-controller/1.0"})  # type: ignore
            resp.raise_for_status()
            return resp.text
        # Fallback to urllib
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "sprinkler-controller/1.0"})
        with urllib.request.urlopen(req, timeout=10) as f:  # type: ignore
            return f.read().decode("utf-8")

    def check_probability(self) -> float | None:
        """Return the maximum chance of precipitation (%) in the next 24 h.

        If the forecast cannot be retrieved or parsed, None is returned
        and rain delay will be ignored.  This method caches the last
        result and timestamp for inspection via the API.
        """
        try:
            body = self._http_get(self.forecast_url)
            data = json.loads(body)
        except Exception:
            return None
        periods = data.get("properties", {}).get("periods", [])
        max_prob = 0.0
        # Consider the first 24 forecast hours
        for p in periods[:24]:
            prob = p.get("probabilityOfPrecipitation", {}).get("value")
            if prob is None:
                prob_val = 0.0
            else:
                try:
                    prob_val = float(prob)
                except Exception:
                    prob_val = 0.0
            if prob_val > max_prob:
                max_prob = prob_val
        self._last_prob = max_prob
        self._last_checked = datetime.now().isoformat()
        return max_prob

    def should_delay(self) -> bool:
        """Decide whether to skip watering based on current settings."""
        rd = self.cfg.get("rain_delay", {})
        if not rd.get("enabled", False):
            return False
        threshold = float(rd.get("threshold", 50))
        prob = self.check_probability()
        if prob is None:
            # On failure to fetch/parse, do not delay
            return False
        return prob >= threshold

    def status(self) -> dict:
        """Return a status dict for API and CLI display."""
        # Ensure we have a current probability reading
        prob = self.check_probability()
        rd = self.cfg.get("rain_delay", {})
        return {
            "enabled": bool(rd.get("enabled", False)),
            "threshold": float(rd.get("threshold", 50)),
            "probability": prob,
            "active": (prob is not None and rd.get("enabled", False) and prob >= rd.get("threshold", 50)),
            "last_checked": self._last_checked,
        }


def build_app(cfg: dict, pinman: PinManager, sched: SprinklerScheduler, rain: RainDelayManager | None = None) -> Flask:
    """Construct the Flask application for the web UI.

    A RainDelayManager may be passed to expose rain status and allow
    adjustments via the web interface.  When rain is None, rain
    endpoints return empty values and controls are disabled.
    """
    app = Flask(__name__)
    # Inline HTML for the modernised UI.  The page uses a dark theme with
    # white text and organises controls for each pin (zone), allows
    # renaming, manual run timers and schedule management.  Schedule IDs
    # are hidden from view but used internally for updates and deletion.

    # ------------------------------------------------------------------
    # Redesigned UI
    #
    # The original inline HTML above provided a basic list of pins and
    # schedules.  In response to the user's request, we override the
    # HTML variable with a much improved web interface.  This new UI
    # groups active sprinkler zones and spare pins into separate
    # sections, adds color‑coded toggle switches and manual run
    # shortcuts with a countdown timer, reorganises the schedule editor
    # into a grid, includes quick preset buttons for common seasonal
    # schedules, displays rain delay status prominently, warns when
    # multiple zones are active concurrently, and uses a modern dark
    # theme with responsive layout for mobile devices.  All existing
    # API endpoints are reused: pin control, renaming, schedule
    # management and rain delay queries.
    
    HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Sprinkler Controller</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{
  --bg:#0b0f14; --card:#10161d; --border:#1e2936; --text:#e6edf3; --muted:#9fb1c1; --green:#2ecc71; --red:#e74c3c; --accent:#3399ff;
}
*{box-sizing:border-box}
body{font-family:system-ui, -apple-system, Segoe UI, Roboto, sans-serif; background:var(--bg); color:var(--text); margin:0}
header{position:sticky; top:0; background:var(--card); padding:10px 16px; display:flex; flex-wrap:wrap; gap:12px 16px; align-items:center; border-bottom:1px solid var(--border); z-index:10}
header .status{font-size:.9rem}
header a{color:var(--text); text-decoration:none}
.logo{font-size:1.2rem; font-weight:600; color:var(--accent); margin-right:auto}
.master-toggle{display:flex; align-items:center; gap:6px; font-size:.9rem}
.badge{padding:4px 8px; border-radius:8px; font-size:.8rem; display:inline-block}
.badge.rain-active{background:var(--red); color:#fff}
.badge.rain-inactive{background:var(--green); color:#000}

main{padding:16px; display:flex; flex-direction:column; gap:16px}

/* Cards & collapsibles */
.card{background:var(--card); border:1px solid var(--border); border-radius:12px; overflow:hidden}
.collap-head{display:flex; align-items:center; gap:10px; padding:12px 14px; cursor:pointer; user-select:none; background:var(--bg)}
.collap-head h2{margin:0; font-size:1.05rem}
.collap-head .sub{margin-left:auto; font-size:.85rem; color:var(--muted)}
.chev{margin-left:6px; transition:transform .2s ease}
.collap.open .chev{transform:rotate(90deg)}
.collap-body{max-height:0; overflow:hidden; transition:max-height .25s ease}
.collap.open .collap-body{max-height:none} /* allow unlimited content */
.section-pad{padding:12px}

.warning{color:var(--red); font-size:.9rem; display:none; margin:8px 0}

/* Single-column lists with compact, horizontal controls */
.list{display:flex; flex-direction:column; gap:8px}
.pin-row{
  display:flex; align-items:center; gap:10px;
  background:var(--bg); border:1px solid var(--border); border-radius:10px;
  padding:8px 10px;
}
.pin-row .dot{width:8px; height:8px; border-radius:50%; background:var(--muted)}
.pin-row .dot.on{background:var(--green)}
.pin-row .pin-name{min-width:140px; flex:1; color:var(--green); font-weight:600}
.pin-row .mins{width:64px; background:transparent; border:1px solid var(--border); border-radius:6px; padding:4px 6px; color:var(--text); font-size:.9rem; text-align:center}
button.btn, a.btn{padding:6px 10px; font-size:.85rem; border:0; border-radius:8px; cursor:pointer; background:var(--border); color:var(--text); text-decoration:none; display:inline-block}
button.primary, a.primary{background:var(--green); color:#000}
button.ghost, a.ghost{background:transparent; border:1px solid var(--border)}
.countdown{font-size:.8rem; color:var(--muted); margin-left:auto}

/* switch */
.switch{position:relative; width:42px; height:24px; flex:0 0 auto}
.switch input{opacity:0; width:0; height:0}
.slider{position:absolute; inset:0; background:var(--muted); border-radius:34px; transition:.2s}
.slider:before{content:""; position:absolute; height:18px; width:18px; left:3px; bottom:3px; background:#fff; border-radius:50%; transition:.2s}
.switch input:checked + .slider{background:var(--green)}
.switch input:checked + .slider:before{transform:translateX(18px)}

/* Schedules */
table.sched-grid{width:100%; border-collapse:collapse}
.sched-grid th,.sched-grid td{border-bottom:1px solid var(--border); padding:6px; font-size:.8rem; text-align:left; vertical-align:top}
.sched-grid th{background:var(--bg)}
.sched-grid tr.disabled{opacity:.55}
.sched-grid .next-run{color:var(--green); font-weight:600}
.sched-wrapper{overflow-x:auto}

/* Rain */
.row{display:flex; flex-wrap:wrap; gap:10px; align-items:center}
label.inline{display:flex; align-items:center; gap:8px; font-size:.9rem}
input[type="number"], input[type="text"], select{background:transparent; color:var(--text); border:1px solid var(--border); border-radius:8px; padding:6px 8px; font-size:.9rem}
input[type="number"]{width:90px}

@media (max-width:600px){
  .sched-grid th,.sched-grid td{font-size:.72rem}
  .sched-grid{min-width:480px}
}
</style>
</head>
<body>
  <header>
    <h1 class="logo">Sprinkler</h1>
    <div class="status" id="timeBar"></div>
    <div class="status" id="automationBar"></div>
    <div class="master-toggle">
      <span>Master</span>
      <label class="switch"><input type="checkbox" id="masterToggle"><span class="slider"></span></label>
    </div>
    <a href="/pin-settings" class="btn primary">Pin Settings</a>
    <span class="badge" id="rainBadge">Rain Delay</span>
  </header>

<main>

  <!-- Active Pins (collapsible) -->
  <section class="card collap open" id="activeCard">
    <div class="collap-head" data-target="activeBody">
      <h2>Active Pins</h2>
      <div class="sub" id="activeSummary"></div>
      <div class="chev">▶</div>
    </div>
    <div class="collap-body" id="activeBody">
      <div class="section-pad">
        <div id="activeWarn" class="warning"></div>
        <div id="activeList" class="list"></div>
      </div>
    </div>
  </section>

  <!-- Schedules (collapsible) -->
  <section class="card collap open" id="schedulesCard">
    <div class="collap-head" data-target="schedulesBody">
      <h2>Schedules</h2>
      <div class="sub" id="schedSummary"></div>
      <div class="chev">▶</div>
    </div>
    <div class="collap-body" id="schedulesBody">
      <div class="section-pad">
        <div class="add-sched" style="margin-bottom:12px;">
          <h3 style="margin:12px 0 8px; font-size:.95rem; color:var(--muted)">Add Schedule</h3>
          <label>Pin <select id="newSchedPin"></select></label>
          <label>On <input id="newSchedOn" type="text" placeholder="HH:MM" size="5"></label>
          <label>Off <input id="newSchedOff" type="text" placeholder="HH:MM" size="5"></label>
          <label>Duration <input id="newSchedDur" type="text" placeholder="HH:MM" size="5"></label>
          <div id="newSchedDays" class="row" style="margin-top:6px;">
            <label><input type="checkbox" value="0">Mon</label>
            <label><input type="checkbox" value="1">Tue</label>
            <label><input type="checkbox" value="2">Wed</label>
            <label><input type="checkbox" value="3">Thu</label>
            <label><input type="checkbox" value="4">Fri</label>
            <label><input type="checkbox" value="5">Sat</label>
            <label><input type="checkbox" value="6">Sun</label>
          </div>
          <div style="margin-top:8px; display:flex; gap:8px">
            <button id="addScheduleBtn" class="btn primary">Add</button>
            <button id="addActiveBtn" class="btn">Add Active</button>
            <button id="disableAllSchedules" class="btn">Disable All</button>
            <button id="deleteAllSchedules" class="btn">Delete All</button>
          </div>
        </div>
        <div class="sched-wrapper">
          <table class="sched-grid">
            <thead>
              <tr><th>Zone</th><th>On</th><th>Off</th><th>Days</th><th>Enabled</th><th>Next</th><th>Action</th></tr>
            </thead>
            <tbody id="schedBody"></tbody>
          </table>
        </div>
        <div class="seq-sched" style="margin-top:20px;">
          <h3 style="margin:12px 0 8px; font-size:.95rem; color:var(--muted)">Sequence Schedules</h3>
          <label>Start <input id="seqStart" type="text" placeholder="HH:MM" size="5"></label>
          <label>Duration <input id="seqDur" type="text" placeholder="HH:MM" size="5"></label>
          <button id="runSeqBtn" class="btn">Run Sequence</button>
        </div>
      </div>
    </div>
  </section>

  <!-- Rain Delay (collapsible) -->
  <section class="card collap" id="rainCard">
    <div class="collap-head" data-target="rainBody">
      <h2>Rain Delay</h2>
      <div class="sub" id="rainSummary">Tap to expand</div>
      <div class="chev">▶</div>
    </div>
    <div class="collap-body" id="rainBody">
      <div class="section-pad">
        <div class="row" style="margin-bottom:8px">
          <label class="inline"><input type="checkbox" id="rainEnabled"> Enable rain delay</label>
          <label class="inline">Threshold (%) <input type="number" min="0" max="100" id="rainThreshold"></label>
          <button class="btn primary" id="rainSave">Save</button>
          <button class="btn ghost" id="rainRefresh">Refresh</button>
        </div>
        <div id="rainStatus" style="color:var(--muted)"></div>
      </div>
    </div>
  </section>

</main>

<script>
/* ========= Collapsible behavior ========= */
document.addEventListener('click', (e)=>{
  const head = e.target.closest('.collap-head'); if(!head) return;
  head.parentElement.classList.toggle('open');
});

/* ========= Helpers ========= */
const DAY_NAMES = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
const slotRe = /^Slot\s+(\d+)\b/i;
function bySlotThenGpio(pins){
  const slots=[], spares=[];
  pins.forEach(p=>{
    const m = p.name ? p.name.match(slotRe) : null;
    if(m){ const n = parseInt(m[1],10); slots.push({...p, _slot: Number.isFinite(n)?n:999}); }
    else { spares.push(p); }
  });
  slots.sort((a,b)=>a._slot - b._slot);
  spares.sort((a,b)=>a.pin - b.pin);
  return {slots, spares};
}
var countdownTimers = {};
var currentSchedules = [];
function fmtTime(s){
  var m = Math.floor(s/60), r = s % 60;
  return String(m) + ":" + String(r).padStart(2,'0');
}
// New helpers for parsing and formatting HH:MM times
function parseHHMM(s){
  const m = /^\s*(\d{1,2}):(\d{2})\s*$/.exec(s || "");
  if(!m) return null;
  const h = parseInt(m[1],10), mi = parseInt(m[2],10);
  if(h < 0 || h > 23 || mi < 0 || mi > 59) return null;
  return h*60 + mi;  // total minutes from midnight
}

function toHHMM(total){
  const h = Math.floor(total / 60) % 24;
  const m = total % 60;
  return String(h).padStart(2,'0') + ':' + String(m).padStart(2,'0');
}
/* ========= Data flow ========= */
function fetchStatus(){
  fetch('/api/status').then(r=>r.json()).then(data=>{
    updateHeader(data);
    renderPins(data.pins_slots, data.automation_enabled, data.system_enabled);
    updateSchedules(data.schedules);
    hydratePinSelect(data.pins);
  });
  fetch('/api/rain').then(r=>r.json()).then(updateRain).catch(()=>{});
}

/* ========= Header ========= */
function updateHeader(data){
  var server = new Date(data.server_time_iso);
  var drift = Math.round((Date.now() - server.getTime())/1000);
  document.getElementById('timeBar').textContent =
    'Pi Time: ' + server.toLocaleString() + ' (drift ' + (drift>=0?'+':'') + drift + 's)';
  document.getElementById('automationBar').textContent =
    data.automation_enabled ? 'Automation: ENABLED' : 'Automation: DISABLED';
  document.getElementById('masterToggle').checked = !!data.system_enabled;
}
/* ========= Pins ========= */
function renderPins(slots, automationEnabled, systemEnabled){
  const activeList = document.getElementById('activeList');
  activeList.innerHTML='';

  slots.forEach(p => activeList.appendChild(makePinRow(p, automationEnabled, systemEnabled)));

  document.getElementById('activeSummary').textContent =
  slots.length + ' slot' + (slots.length!==1 ? 's' : '');

var running = slots.filter(function(p){ return p.is_active; }).length;
var warn = document.getElementById('activeWarn');
if(running>1){
  warn.style.display='block';
  warn.textContent='Warning: ' + running + ' zones are active simultaneously';
}else{
  warn.style.display='none';
}
function makePinRow(p, automationEnabled, systemEnabled){
  const row = document.createElement('div'); row.className='pin-row'; row.draggable=true; row.dataset.pin = p.pin;

  const dot = document.createElement('span'); dot.className='dot'+(p.is_active?' on':''); row.appendChild(dot);
  var name = document.createElement('span'); name.className='pin-name'; name.textContent = p.name || ('Pin ' + p.pin); row.appendChild(name);

  const sw = document.createElement('label'); sw.className='switch';
  const sb = document.createElement('input'); sb.type='checkbox'; sb.checked=!!p.is_active; sb.disabled=!(automationEnabled && systemEnabled);
  sb.addEventListener('change', function(){
  fetch('/api/pin/' + p.pin + '/' + (sb.checked ? 'on' : 'off'), {method:'POST'});
});
  const slider = document.createElement('span'); slider.className='slider';
  sw.appendChild(sb); sw.appendChild(slider); row.appendChild(sw);

  const mins = document.createElement('input'); mins.type='text'; mins.value='10'; mins.className='mins'; mins.title='Minutes'; mins.disabled=!systemEnabled; row.appendChild(mins);

  const run = document.createElement('button'); run.className='btn primary'; run.textContent='RUN'; run.disabled=!systemEnabled;
  run.addEventListener('click', ()=>{
    const m = parseInt(mins.value,10); if(!Number.isFinite(m)||m<=0) return alert('Enter minutes > 0');
    var secs = m*60;
fetch('/api/pin/' + p.pin + '/on?seconds=' + secs, {method:'POST'}).then(function(){ startCountdown(p.pin, secs); });
  });
  row.appendChild(run);

  const stop = document.createElement('button'); stop.className='btn'; stop.textContent='STOP'; stop.disabled=!systemEnabled;
  stop.addEventListener('click', ()=>{
    fetch(`/api/pin/${p.pin}/off`, {method:'POST'}).then(()=> stopCountdown(p.pin));
    stopCountdown(p.pin);
  });
  row.appendChild(stop);

  var cd = document.createElement('span'); cd.id='countdown-' + p.pin; cd.className='countdown'; row.appendChild(cd);

  return row;
}
function startCountdown(pin, seconds){
  if(countdownTimers[pin]){ clearInterval(countdownTimers[pin]); }
  var remaining = seconds;
  var el = document.getElementById('countdown-' + pin);
  if(!el) return;
  el.textContent = fmtTime(remaining);
  countdownTimers[pin] = setInterval(function(){
    remaining--;
    if(remaining<=0){
      clearInterval(countdownTimers[pin]);
      delete countdownTimers[pin];
      el.textContent = '';
    }else{
      el.textContent = fmtTime(remaining);
    }
  }, 1000);
}

function stopCountdown(pin){
  if(countdownTimers[pin]){
    clearInterval(countdownTimers[pin]);
    delete countdownTimers[pin];
  }
  var el = document.getElementById('countdown-' + pin);
  if(el){
    el.textContent = '';
  }
}

  }
let dragEl;
function sendPinOrder(){
  const active = [...document.querySelectorAll('#activeList .pin-row')].map(r=>parseInt(r.dataset.pin,10));
  const spare  = [...document.querySelectorAll('#spareList .pin-row')].map(r=>parseInt(r.dataset.pin,10));
  fetch('/api/pins/reorder', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({active, spare})})
    .then(fetchStatus);
}

function setupPinDrag(list){
  list.addEventListener('dragstart', e=>{
    dragEl = e.target.closest('.pin-row');
    e.dataTransfer.effectAllowed='move';
  });
  list.addEventListener('dragover', e=>{
    e.preventDefault();
    const target = e.target.closest('.pin-row');
    if(!target){
      list.appendChild(dragEl);
      return;
    }
    if(target===dragEl) return;
    const rect = target.getBoundingClientRect();
    const next = (e.clientY - rect.top)/(rect.bottom-rect.top) > 0.5;
    target.parentElement.insertBefore(dragEl, next ? target.nextSibling : target);
  });
  list.addEventListener('drop', e=>{ e.preventDefault(); sendPinOrder(); });
}

  /* ========= Schedules ========= */
  function updateSchedules(schedules){
  currentSchedules = schedules.slice();
  var tbody = document.getElementById('schedBody');
  tbody.innerHTML = '';

  for (var i = 0; i < schedules.length; i++){
    var s = schedules[i];

    var tr = document.createElement('tr');
    tr.draggable = true; tr.dataset.id = s.id;
    if(!s.enabled){ tr.classList.add('disabled'); }

    // Zone
    var tdZone = document.createElement('td');
    tdZone.textContent = s.pin_name;
    tr.appendChild(tdZone);

    // On
    var tdOn = document.createElement('td');
    var inpOn = document.createElement('input');
    inpOn.type = 'text';
    inpOn.value = s.on;
    inpOn.size = 5;
    inpOn.addEventListener('change', function(idRef, el){
      return function(){ updateSchedule(idRef, {on: el.value}); };
    }(s.id, inpOn));
    tdOn.appendChild(inpOn);
    tr.appendChild(tdOn);

    // Off
    var tdOff = document.createElement('td');
    var inpOff = document.createElement('input');
    inpOff.type = 'text';
    inpOff.value = s.off;
    inpOff.size = 5;
    inpOff.addEventListener('change', function(idRef, el){
      return function(){ updateSchedule(idRef, {off: el.value}); };
    }(s.id, inpOff));
    tdOff.appendChild(inpOff);
    tr.appendChild(tdOff);

    // Days
    var tdDays = document.createElement('td');
    for (var d = 0; d < DAY_NAMES.length; d++){
      var lb = document.createElement('label');
      lb.style.display = 'inline-flex';
      lb.style.gap = '6px';
      lb.style.marginRight = '8px';

      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = s.days.indexOf(d) !== -1;
      cb.addEventListener('change', (function(idRef, container){
        return function(){
          var boxes = container.querySelectorAll('input[type=checkbox]');
          var arr = [];
          for (var k = 0; k < boxes.length; k++){
            if (boxes[k].checked){ arr.push(k); }
          }
          updateSchedule(idRef, {days: arr});
        };
      })(s.id, tdDays));

      lb.appendChild(cb);
      lb.append(DAY_NAMES[d]);
      tdDays.appendChild(lb);
    }
    tr.appendChild(tdDays);

    // Enabled
    var tdEn = document.createElement('td');
    var cbEn = document.createElement('input');
    cbEn.type = 'checkbox';
    cbEn.checked = s.enabled;
    cbEn.addEventListener('change', (function(idRef, el){
      return function(){ updateSchedule(idRef, {enabled: el.checked}); };
    })(s.id, cbEn));
    tdEn.appendChild(cbEn);
    tr.appendChild(tdEn);

    // Next
    var tdNext = document.createElement('td');
    tdNext.textContent = s.next_run ? s.next_run : '';
    if (s.next_run){ tdNext.classList.add('next-run'); }
    tr.appendChild(tdNext);

    // Action
    var tdAct = document.createElement('td');

    // Delete
    var del = document.createElement('button');
    del.className = 'btn';
    del.textContent = 'Delete';
    del.addEventListener('click', (function(idRef){
      return function(){
        if (confirm('Delete this schedule?')){
          fetch('/api/schedule/' + idRef, {method:'DELETE'}).then(fetchStatus);
        }
      };
    })(s.id));
    tdAct.appendChild(del);

    // Duplicate
    var dup = document.createElement('button');
    dup.className = 'btn';
    dup.textContent = 'Duplicate';
    dup.style.marginLeft = '8px';
    dup.title = "Make a copy that starts at this schedule's OFF time with the same duration";
    dup.addEventListener('click', (function(sRef){
      return function(){
        var onM  = parseHHMM(sRef.on);
        var offM = parseHHMM(sRef.off);
        if (onM == null || offM == null){
          alert('Cannot duplicate: bad time format.');
          return;
        }
        var dur = offM - onM;
        if (dur <= 0){
          alert('This schedule ends at/before it starts. Duplication would cross midnight. Please adjust this row first.');
          return;
        }
        var newOn  = offM;
        var newOff = offM + dur;
        if (newOff >= 24*60){
          alert('Duplication would cross midnight. Shorten the duration or move the original earlier.');
          return;
        }
        var payload = {
          pin: sRef.pin,
          on:  toHHMM(newOn),
          off: toHHMM(newOff),
          days: sRef.days.slice()
        };
        fetch('/api/schedule', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload)
        }).then(function(r){ return r.ok ? fetchStatus() : r.text().then(function(t){ alert('Error: ' + t); }); });
      };
    })(s));
    tdAct.appendChild(dup);

    tr.appendChild(tdAct);
    tbody.appendChild(tr);
  }

  var summary = schedules.length + ' schedule' + (schedules.length !== 1 ? 's' : '');
  document.getElementById('schedSummary').textContent =
  schedules.length + ' schedule' + (schedules.length!==1 ? 's' : '');
}
function hydratePinSelect(_){
  fetch('/api/status').then(r=>r.json()).then(data=>{
    const sel = document.getElementById('newSchedPin'); sel.innerHTML='';
    [...data.pins_slots, ...data.pins_spares].forEach(p=>{
      var o=document.createElement('option');
o.value = p.pin;
o.textContent = 'GPIO ' + p.pin + ' — ' + (p.name || ('GPIO ' + p.pin));
sel.appendChild(o);
    });
  });
}
  function updateSchedule(id, obj){
    fetch(`/api/schedule/${id}`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(obj)}).then(fetchStatus);
  }

function sendScheduleOrder(){
  const order = [...document.querySelectorAll('#schedBody tr')].map(r=>r.dataset.id);
  fetch('/api/schedules/reorder', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({order})}).then(fetchStatus);
}

function setupScheduleDrag(){
  const tbody = document.getElementById('schedBody');
  let dragEl;
  tbody.addEventListener('dragstart', e=>{
    dragEl = e.target.closest('tr');
    e.dataTransfer.effectAllowed='move';
  });
  tbody.addEventListener('dragover', e=>{
    e.preventDefault();
    const target = e.target.closest('tr');
    if(!target || target===dragEl) return;
    const rect = target.getBoundingClientRect();
    const next = (e.clientY - rect.top)/(rect.bottom-rect.top) > 0.5;
    tbody.insertBefore(dragEl, next ? target.nextSibling : target);
  });
  tbody.addEventListener('drop', e=>{ e.preventDefault(); sendScheduleOrder(); });
}

function sequenceSchedules(){
  const startVal = document.getElementById('seqStart').value.trim();
  const durVal = document.getElementById('seqDur').value.trim();
  const start = parseHHMM(startVal);
  const dur = parseHHMM(durVal);
  if(start == null || dur == null){
    alert('Times must be HH:MM');
    return;
  }
  let cur = start;
  const promises = [];
  for (const s of currentSchedules) {
    const on = cur % (24 * 60);
    const off = on + dur;
    cur += dur;
    promises.push(
      fetch(`/api/schedule/${s.id}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ on: toHHMM(on), off: toHHMM(off % (24 * 60)) })
      })
    );
  }
  Promise.all(promises).then(fetchStatus);
}

  /* ========= Rain ========= */
  function updateRain(rd){
  var badge = document.getElementById('rainBadge');
  if(!rd || !('enabled' in rd)){ badge.style.display='none'; return; }
  badge.style.display='inline-block';

  var prob = rd.probability!=null ? Math.round(rd.probability) : null;
  var thr = rd.threshold;

  badge.textContent = rd.active
    ? 'Rain Delay: Active (' + (prob!=null ? (prob + '%') : '') + ' \u2265 ' + thr + '%)'
    : 'Rain Delay: Inactive (' + (prob!=null ? (prob + '%') : '') + ' < ' + thr + '%)';

  // hydrate panel controls and summary
  var en = document.getElementById('rainEnabled');
  var th = document.getElementById('rainThreshold');
  if (en) { en.checked = !!rd.enabled; }
  if (th) { th.value   = (thr!=null ? thr : 50); }
  var sum = document.getElementById('rainSummary');
  if (sum) {
    sum.textContent = 'Enabled: ' + (en && en.checked ? 'Yes' : 'No') + ' • Threshold: ' + (th ? th.value : thr) + '%';
  }
}
function init(){
  setupPinDrag(document.getElementById('activeList'));
  setupScheduleDrag();

  document.getElementById('masterToggle').addEventListener('change', function(){
    fetch('/api/system', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({enabled:this.checked})})
      .then(fetchStatus);
  });

  // Add Schedule
  document.getElementById('addScheduleBtn').addEventListener('click', ()=>{
    const pin = parseInt(document.getElementById('newSchedPin').value,10);
    const onVal = document.getElementById('newSchedOn').value.trim();
    const offVal= document.getElementById('newSchedOff').value.trim();
    const days = [...document.querySelectorAll('#newSchedDays input[type=checkbox]')].filter(cb=>cb.checked).map(cb=>parseInt(cb.value,10));
    if(!/^\d{1,2}:\d{2}$/.test(onVal) || !/^\d{1,2}:\d{2}$/.test(offVal)) return alert('Times must be HH:MM');
    if(days.length===0) return alert('Select at least one day');
    fetch('/api/schedule', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({pin:pin,on:onVal,off:offVal,days:days})})
      .then(r=>r.ok?fetchStatus():r.text().then(t=>alert(t)));
  });

  document.getElementById('addActiveBtn').addEventListener('click', ()=>{
    const onVal = document.getElementById('newSchedOn').value.trim();
    const durVal = document.getElementById('newSchedDur').value.trim();
    const days = [...document.querySelectorAll('#newSchedDays input[type=checkbox]')].filter(cb=>cb.checked).map(cb=>parseInt(cb.value,10));
    const onM = parseHHMM(onVal);
    const durM = parseHHMM(durVal);
    if(onM==null || durM==null) return alert('Times must be HH:MM');
    if(days.length===0) return alert('Select at least one day');
    const off = toHHMM((onM + durM) % (24 * 60));
    const on = toHHMM(onM);
    const pins = [...document.querySelectorAll('#activeList .pin-row')].map(r=>parseInt(r.dataset.pin,10));
    Promise.all(pins.map(pin => fetch('/api/schedule', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({pin,on,off,days})})))
      .then(fetchStatus);
  });

  // Sequence schedules
  document.getElementById('runSeqBtn').addEventListener('click', sequenceSchedules);
  document.getElementById('disableAllSchedules')?.addEventListener('click', ()=>{
    fetch('/api/status').then(r=>r.json()).then(data=>{
      Promise.all(data.schedules.map(s=> fetch(`/api/schedule/${s.id}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:false})}) )).then(fetchStatus);
    });
  });
document.getElementById('deleteAllSchedules')?.addEventListener('click', ()=>{
    if(!confirm('Delete all schedules?')) return;
    fetch('/api/status').then(r=>r.json()).then(data=>{
      Promise.all(data.schedules.map(s=> fetch(`/api/schedule/${s.id}`,{method:'DELETE'}) )).then(fetchStatus);
    });
  });

  // Rain controls
  document.getElementById('rainSave').addEventListener('click', ()=>{
    const payload = {
      enabled: document.getElementById('rainEnabled').checked,
      threshold: parseInt(document.getElementById('rainThreshold').value,10)
    };
    fetch('/api/rain', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)})
      .then(()=> fetch('/api/rain').then(r=>r.json()).then(updateRain));
  });
  document.getElementById('rainRefresh').addEventListener('click', ()=> fetch('/api/rain').then(r=>r.json()).then(updateRain));

  // Kick off
  fetchStatus();
  setInterval(fetchStatus, 60000);
}

if(document.readyState === 'loading'){
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
</script>
</body>
</html>
"""

    @app.get("/")
    def index():
        return redirect("/settings")

    @app.get("/settings")
    def settings_page():
        return Response(HTML, mimetype="text/html")

    @app.get("/pin-settings")
    def pin_settings_page():
        return Response(SETTINGS_HTML, mimetype="text/html")


    def _split_and_sort_pins_for_view(cfg, pinman):
        """Return (slots_sorted, spares_sorted, combined_sorted) for UI."""
        slots, spares = [], []
        for pin_str, meta in cfg["pins"].items():
            pin = int(pin_str)
            name = meta.get("name", f"Pin {pin}")
            is_active = pinman.get_state(pin)
            item = {
                "pin": pin,
                "name": name,
                "is_active": is_active,
                "_order": meta.get("order"),
            }

            section = meta.get("section")
            if section == "active":
                slots.append(item)
                continue
            if section == "spare":
                spares.append(item)
                continue

            # Fallback for legacy configs without explicit sections
            m_slot = re.match(r"\s*Slot\s+(\d+)\b", name, re.IGNORECASE)
            if m_slot:
                item["_slot_num"] = int(m_slot.group(1))
                slots.append(item)
            else:
                m_spare = re.search(r"\bSpare\s+(\d+)\b", name, re.IGNORECASE)
                item["_spare_num"] = int(m_spare.group(1)) if m_spare else 10_000
                spares.append(item)

        if any(p.get("_order") is not None for p in slots):
            slots.sort(key=lambda x: x.get("_order", 0))
        else:
            slots.sort(key=lambda x: x.get("_slot_num", 10_000))
        if any(p.get("_order") is not None for p in spares):
            spares.sort(key=lambda x: x.get("_order", 0))
        else:
            spares.sort(key=lambda x: (x.get("_spare_num", 10_000), x["pin"]))
        return slots, spares, (slots + spares)


    @app.get("/api/status")
    def api_status():
        with LOCK:
            # Sorted pins (Slots 1–16 first, then Spares)
            slots_sorted, spares_sorted, pins_sorted = _split_and_sort_pins_for_view(cfg, pinman)
            pins_view = pins_sorted

            # Build a mapping of schedule id -> earliest next ON run time
            next_run_map = {}
            try:
                jobs = sched.sched.get_jobs()
                for job in jobs:
                    jid = getattr(job, 'id', '')
                    # id format: <schedule_id>-on-<day> or <schedule_id>-off-<day>
                    if '-on-' in jid:
                        sched_id = jid.split('-on-')[0]
                        nr = getattr(job, 'next_run_time', None)
                        if nr is not None:
                            existing = next_run_map.get(sched_id)
                            if existing is None or nr < existing:
                                next_run_map[sched_id] = nr
            except Exception:
                next_run_map = {}

            # Build schedules view
            sch_view = []
            for s in cfg.get("schedules", []):
                nr_dt = next_run_map.get(s["id"])
                if nr_dt:
                    try:
                        nr_str = nr_dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        nr_str = str(nr_dt)
                else:
                    nr_str = None

                sch_view.append({
                    "id": s["id"],
                    "pin": int(s["pin"]),
                    "pin_name": cfg["pins"].get(str(s["pin"]), {}).get("name", f"Pin {s['pin']}"),
                    "on": s["on"],
                    "off": s["off"],
                    "day_names": [DAY_NAMES[d] for d in s.get("days", [])],
                    "days": s.get("days", []),
                    "enabled": bool(s.get("enabled", True)),
                    "next_run": nr_str,
                })

            # Time/meta fields
            now_dt = datetime.now().astimezone()
            server_time_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
            server_time_iso = now_dt.isoformat()
            server_weekday = now_dt.weekday()
            server_tz = (now_dt.tzinfo.tzname(now_dt) if now_dt.tzinfo else '')
            server_utc_offset = now_dt.strftime('%z')

            return jsonify({
                "pins": pins_view,                 # combined, sorted
                "pins_slots": slots_sorted,        # slots only
                "pins_spares": spares_sorted,      # spares only
                "schedules": sch_view,
                "automation_enabled": bool(cfg.get("automation_enabled", True)),
                "system_enabled": bool(cfg.get("system_enabled", True)),
                "ntp": cfg.get("ntp", {}),
                "now": server_time_str,            # legacy
                "server_time": server_time_str,    # legacy
                "server_time_iso": server_time_iso,
                "server_time_str": server_time_str,
                "server_weekday": server_weekday,
                "server_tz": server_tz,
                "server_utc_offset": server_utc_offset,
                "mock_mode": USE_MOCK,
            })


    @app.post("/api/pin/<int:pin>/on")
    def api_pin_on(pin: int):
        if not cfg.get("system_enabled", True):
            return ("System disabled", 403)
        seconds = request.args.get("seconds", default=None, type=int)
        pinman.on(pin, seconds)
        return "OK"

    @app.post("/api/pin/<int:pin>/off")
    def api_pin_off(pin: int):
        pinman.off(pin)
        return "OK"

    @app.post("/api/system")
    def api_system_toggle():
        data = request.get_json(force=True) or {}
        enabled = bool(data.get("enabled", True))
        with LOCK:
            cfg["system_enabled"] = enabled
            save_config(cfg)
            if not enabled:
                for p in list(pinman.devices.keys()):
                    try:
                        pinman.off(p)
                    except Exception:
                        pass
        return jsonify({"system_enabled": enabled})

    @app.post("/api/pin/<int:pin>/name")
    def api_pin_rename(pin: int):
        """Update the friendly name of a pin.

        Expects JSON body {"name": "New Name"}.  After updating,
        configuration is persisted and scheduler is reloaded.  Returns
        the new name.
        """
        data = request.get_json(force=True)
        new_name = data.get("name", "").strip() if isinstance(data, dict) else ""
        if not new_name:
            return ("Invalid name", 400)
        with LOCK:
            pmeta = cfg.setdefault("pins", {}).get(str(pin))
            if not pmeta:
                return ("Unknown pin", 404)
            pmeta["name"] = new_name
            save_config(cfg)
        return jsonify({"pin": pin, "name": new_name})

    @app.post("/api/pins/reorder")
    def api_pins_reorder():
        """Update section membership and order of pins.

        Expects JSON like {"active": [5,6], "spare": [7]} representing the
        pins in each section.  All pins must be present exactly once across the
        two lists.  Each pin will have its "section" set to either "active" or
        "spare" and an "order" relative to others in that section.
        """
        data = request.get_json(force=True) or {}
        active = data.get("active")
        spare = data.get("spare")
        if not isinstance(active, list) or not isinstance(spare, list):
            return ("Invalid order", 400)
        try:
            active_int = [int(p) for p in active]
            spare_int = [int(p) for p in spare]
        except Exception:
            return ("Invalid order", 400)
        combined = active_int + spare_int
        with LOCK:
            pins_cfg = cfg.setdefault("pins", {})
            if set(map(str, combined)) != set(pins_cfg.keys()):
                return ("Order must contain all pins", 400)
            for idx, pin in enumerate(active_int):
                meta = pins_cfg[str(pin)]
                meta["section"] = "active"
                meta["order"] = idx
            for idx, pin in enumerate(spare_int):
                meta = pins_cfg[str(pin)]
                meta["section"] = "spare"
                meta["order"] = idx
            cfg["pins"] = {str(pin): pins_cfg[str(pin)] for pin in combined}
            save_config(cfg)
        return "OK"

    @app.post("/api/schedule")
    def api_schedule_add():
        """Add a new schedule entry via POST.

        Accepts JSON with keys: pin (int), on (HH:MM), off (HH:MM),
        days (string such as 'mon,wed,fri', 'weekdays', 'daily', or a list of
        integers 0-6).  On success returns the schedule id.
        """
        data = request.get_json(force=True) or {}
        try:
            pin_val = int(data.get("pin"))
        except Exception:
            return ("Missing or invalid pin", 400)
        on_val = data.get("on")
        off_val = data.get("off")
        # Ensure the on/off values are provided as strings
        if not on_val or not off_val:
            return ("Missing on/off times", 400)
        if not isinstance(on_val, str) or not isinstance(off_val, str):
            return ("Times must be HH:MM (24h)", 400)
        # Validate time format manually.  Accept one or two digit hours
        # and two digit minutes.  Example valid: 6:30, 06:30, 18:05.  Reject
        # invalid formats or out of range values.
        def _parse_time_str(t: str) -> tuple[int,int]:
            parts = t.strip().split(":")
            if len(parts) != 2:
                raise ValueError
            try:
                h = int(parts[0])
                m = int(parts[1])
            except Exception:
                raise ValueError
            if h < 0 or h > 23 or m < 0 or m > 59:
                raise ValueError
            return h, m
        try:
            _ = _parse_time_str(on_val)
            _ = _parse_time_str(off_val)
        except Exception:
            return ("Times must be HH:MM (24h)", 400)
        # Parse days
        days_raw = data.get("days", "daily")
        days: List[int] = []
        if isinstance(days_raw, list):
            # assume list of ints or strings
            for d in days_raw:
                try:
                    di = int(d)
                except Exception:
                    # attempt to parse abbreviations
                    try:
                        di = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(str(d)[:3].lower())
                    except Exception:
                        return (f"Invalid day token: {d}", 400)
                if 0 <= di <= 6:
                    days.append(di)
        elif isinstance(days_raw, str):
            try:
                days = parse_days(days_raw)
            except Exception as e:
                return (str(e), 400)
        else:
            return ("Invalid days", 400)
        # Reject when no days selected.  A schedule with no days would
        # never execute and could confuse the user.  Require at least
        # one day entry.
        if isinstance(days, list) and len(days) == 0:
            return ("No days selected", 400)
        with LOCK:
            entry = {
                "id": str(uuid.uuid4()),
                "pin": pin_val,
                "on": on_val,
                "off": off_val,
                "days": days,
                "enabled": True,
            }
            cfg.setdefault("schedules", []).append(entry)
            save_config(cfg)
        sched.reload_jobs()
        return jsonify({"id": entry["id"]})

    @app.post("/api/schedule/<id>")
    def api_schedule_update(id: str):
        """Update an existing schedule entry.

        Expects JSON with any of the keys: on, off, days, enabled.  days
        can be a list or string (same semantics as in add).  On success
        returns OK.
        """
        data = request.get_json(force=True) or {}
        with LOCK:
            entry = None
            for s in cfg.get("schedules", []):
                if s["id"] == id:
                    entry = s
                    break
            if entry is None:
                return ("Schedule not found", 404)
            # Update on/off.  Accept strings in HH:MM 24h.  Use
            # manual parsing to allow one or two digit hours.
            def _parse_time_str(t: str) -> tuple[int, int]:
                parts = t.strip().split(":")
                if len(parts) != 2:
                    raise ValueError
                try:
                    h = int(parts[0])
                    m = int(parts[1])
                except Exception:
                    raise ValueError
                if h < 0 or h > 23 or m < 0 or m > 59:
                    raise ValueError
                return h, m
            if "on" in data:
                val = data["on"]
                if not isinstance(val, str):
                    return ("Invalid on time", 400)
                try:
                    _parse_time_str(val)
                except Exception:
                    return ("Invalid on time", 400)
                entry["on"] = val
            if "off" in data:
                val = data["off"]
                if not isinstance(val, str):
                    return ("Invalid off time", 400)
                try:
                    _parse_time_str(val)
                except Exception:
                    return ("Invalid off time", 400)
                entry["off"] = val
            if "enabled" in data:
                entry["enabled"] = bool(data["enabled"])
            if "days" in data:
                days_raw = data["days"]
                newdays: List[int] = []
                if isinstance(days_raw, list):
                    for d in days_raw:
                        try:
                            di = int(d)
                        except Exception:
                            try:
                                di = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(str(d)[:3].lower())
                            except Exception:
                                return (f"Invalid day token: {d}", 400)
                        if 0 <= di <= 6:
                            newdays.append(di)
                elif isinstance(days_raw, str):
                    try:
                        newdays = parse_days(days_raw)
                    except Exception as e:
                        return (str(e), 400)
                else:
                    return ("Invalid days", 400)
                # Reject empty list of days to prevent schedules that
                # never run.  Require at least one day when updating.
                if len(newdays) == 0:
                    return ("No days selected", 400)
                entry["days"] = newdays
            save_config(cfg)
        sched.reload_jobs()
        return "OK"

    @app.route("/api/schedule/<id>", methods=["DELETE"])
    def api_schedule_delete(id: str):
        """Remove a schedule by its id."""
        with LOCK:
            before = len(cfg.get("schedules", []))
            cfg["schedules"] = [s for s in cfg.get("schedules", []) if s.get("id") != id]
            save_config(cfg)
        sched.reload_jobs()
        return jsonify({"deleted": before - len(cfg.get("schedules", []))})

    @app.post("/api/schedules/reorder")
    def api_schedules_reorder():
        """Update the order of schedules based on a list of IDs."""
        data = request.get_json(force=True) or {}
        order = data.get("order")
        if not isinstance(order, list):
            return ("Invalid order", 400)
        with LOCK:
            sched_map = {s["id"]: s for s in cfg.get("schedules", [])}
            if set(order) != set(sched_map.keys()):
                return ("Order must contain all schedules", 400)
            cfg["schedules"] = [sched_map[i] for i in order]
            save_config(cfg)
        sched.reload_jobs()
        return "OK"

    @app.get("/api/rain")
    def api_rain_status():
        """Return rain delay status for the UI.

        If no RainDelayManager is provided, return an empty object so
        the UI can hide controls.
        """
        if rain is None:
            return jsonify({})
        return jsonify(rain.status())

    @app.post("/api/rain")
    def api_rain_update():
        """Update rain delay settings (enabled/threshold) via POST."""
        if rain is None:
            return "Rain delay unsupported", 400
        data = request.get_json(force=True)
        with LOCK:
            rd = cfg.setdefault("rain_delay", {})
            if "enabled" in data:
                rd["enabled"] = bool(data["enabled"])
            if "threshold" in data:
                try:
                    rd["threshold"] = float(data["threshold"])
                except Exception:
                    pass
            save_config(cfg)
        return jsonify(rain.status())

    # Remove preset API.  Presets are not exposed via the web UI in the
    # redesigned interface.  Manual schedule editing is encouraged instead.

    return app


def print_info():
    """Print a friendly command reference to stdout."""
    print(r"""
GPIO SPRINKLER CONTROLLER
-------------------------

USAGE OVERVIEW
===============

This program controls sprinkler valves connected to the following GPIO
pins: 5, 6, 11, 12, 16, 19, 20, 21 and 26.  Pin 19 is designated
specifically as the garden zone and follows a different schedule in the
regular season preset.

Running the web UI
------------------
Launch the web interface and background scheduler with:

  sudo ./sprinkler.py web

Navigate to http://<pi‑ip>:8000 to view current pin states, existing
schedules, and to apply presets.  Use the “Season” button to apply
your regular 30‑minute sequential schedule.

Pin control from the command line
---------------------------------
Turn a zone on:

  sudo ./sprinkler.py on <PIN>

Turn it off:

  sudo ./sprinkler.py off <PIN>

Pulse a zone for N seconds (for testing):

  sudo ./sprinkler.py on <PIN> --seconds N

Working with schedules
----------------------
Add a manual schedule (HH:MM times, 24‑hour clock):

  ./sprinkler.py schedule add --pin <PIN> --on 06:00 --off 06:30 --days mon,wed,fri

List schedules:

  ./sprinkler.py schedule list

Delete a schedule by its id:

  ./sprinkler.py schedule delete --id <UUID>

Enable/disable a schedule:

  ./sprinkler.py schedule enable  --id <UUID>
  ./sprinkler.py schedule disable --id <UUID>

Switching presets
-----------------
Apply the regular season preset (30‑minute sequential, garden on Tue/Thu/Sat):

  ./sprinkler.py preset season

Viewing status
--------------
Print current pin states and schedules:

  ./sprinkler.py status

Automation toggle
-----------------
Disable automation (schedules won’t automatically turn valves on/off):

  ./sprinkler.py automation off

Re‑enable automation:

  ./sprinkler.py automation on

Time management
---------------
Set the device’s system time using timedatectl (requires sudo):

  sudo ./sprinkler.py time set "2025-08-29 06:30:00"

Enable or disable NTP synchronisation and choose a server:

  sudo ./sprinkler.py ntp enable  --server pool.ntp.org
  sudo ./sprinkler.py ntp disable

Installing as a service
-----------------------
To run the controller automatically at boot, install the systemd
service:

  sudo ./sprinkler.py service install
  sudo systemctl enable --now sprinkler

To remove it:

  sudo ./sprinkler.py service uninstall

Editing the configuration
-------------------------
The configuration is stored in config.json next to this script.  You
can edit pin definitions (names, active_high, etc.), adjust default
web host/port and tweak the NTP server.  Manual schedules and
presets are stored here too.  After editing, restart the controller
or reapply the relevant preset.

An alternate host can be supplied at runtime by setting the
``SPRINKLER_DOMAIN`` environment variable.  For example:

  export SPRINKLER_DOMAIN=sprinkler.buell

The legacy ``SPRINKLER_HOME`` variable is still honoured.  Either one
overrides the host used when running ``web`` without editing the
configuration file.

""")


def main():
    cfg = load_config()
    pinman = PinManager(cfg)
    # Initialise the rain delay manager before creating the scheduler.
    rain_manager = RainDelayManager(cfg)
    # Pass rain_manager into the scheduler so ON jobs can consult it.
    scheduler = SprinklerScheduler(pinman, cfg, rain_manager)

    parser = argparse.ArgumentParser(description="GPIO sprinkler controller")
    sub = parser.add_subparsers(dest="cmd")
    parser.add_argument("-info", action="store_true", help="Show usage manual")

    # web command
    p_web = sub.add_parser("web", help="Run web UI and scheduler")
    default_host = os.environ.get("SPRINKLER_DOMAIN") or os.environ.get(
        "SPRINKLER_HOME", cfg.get("web", {}).get("host", "0.0.0.0")
    )
    p_web.add_argument("--host", default=default_host)
    p_web.add_argument("--port", type=int, default=int(cfg.get("web", {}).get("port", 8000)))

    # status command
    sub.add_parser("status", help="Show pin states and schedules")

    # on/off commands
    p_on = sub.add_parser("on", help="Turn pin ON")
    p_on.add_argument("pin", type=int)
    p_on.add_argument("--seconds", type=int, default=None, help="Pulse on for N seconds")
    p_off = sub.add_parser("off", help="Turn pin OFF")
    p_off.add_argument("pin", type=int)

    # schedule management
    p_sch = sub.add_parser("schedule", help="Manage schedules")
    sch_sub = p_sch.add_subparsers(dest="sch_cmd")
    p_add = sch_sub.add_parser("add", help="Add a schedule")
    p_add.add_argument("--pin", type=int, required=True)
    p_add.add_argument("--on", required=True, help="HH:MM 24h")
    p_add.add_argument("--off", required=True, help="HH:MM 24h")
    p_add.add_argument("--days", default="daily", help="mon,tue,...|weekdays|weekends|daily|0,2,4")

    p_list = sch_sub.add_parser("list", help="List schedules")
    p_list.add_argument("--pin", type=int, default=None)

    p_del = sch_sub.add_parser("delete", help="Delete a schedule by id")
    p_del.add_argument("--id", required=True)

    p_en = sch_sub.add_parser("enable", help="Enable schedule by id")
    p_en.add_argument("--id", required=True)
    p_dis = sch_sub.add_parser("disable", help="Disable schedule by id")
    p_dis.add_argument("--id", required=True)

    # automation toggle
    p_auto = sub.add_parser("automation", help="Enable or disable automation")
    p_auto.add_argument("state", choices=["on", "off"])

    # time / ntp
    p_time = sub.add_parser("time", help="Set system time")
    time_sub = p_time.add_subparsers(dest="time_cmd")
    p_set = time_sub.add_parser("set", help="Set time via timedatectl")
    p_set.add_argument("datetime_str", help="YYYY-MM-DD HH:MM:SS")

    p_ntp = sub.add_parser("ntp", help="Enable/disable NTP")
    ntp_sub = p_ntp.add_subparsers(dest="ntp_cmd")
    p_ntp_en = ntp_sub.add_parser("enable", help="Enable NTP")
    p_ntp_en.add_argument("--server", default=cfg["ntp"].get("server", "pool.ntp.org"))
    ntp_sub.add_parser("disable", help="Disable NTP")

    # Note: preset command removed in redesigned UI; manual scheduling encouraged

    # rain delay commands
    p_rd = sub.add_parser("raindelay", help="Manage rain delay settings")
    rd_sub = p_rd.add_subparsers(dest="rd_cmd")
    rd_sub.add_parser("status", help="Show rain delay status")
    rd_sub.add_parser("enable", help="Enable rain delay")
    rd_sub.add_parser("disable", help="Disable rain delay")
    p_thresh = rd_sub.add_parser("threshold", help="Set precipitation threshold (percent)")
    p_thresh.add_argument("value", type=float, help="Percent chance of precipitation to trigger delay")

    # service install/uninstall
    p_srv = sub.add_parser("service", help="Manage systemd service")
    srv_sub = p_srv.add_subparsers(dest="srv_cmd")
    srv_sub.add_parser("install")
    srv_sub.add_parser("uninstall")

    args = parser.parse_args()

    # Show manual if -info or no command provided
    if args.info or (len(sys.argv) == 1):
        print_info()
        return

    # Web UI
    if args.cmd == "web":
        scheduler.start()
        # Pass rain_manager into the web app so rain status appears
        app = build_app(cfg, pinman, scheduler, rain_manager)
        # Override host/port if provided via command line
        host = args.host
        port = args.port
        app.run(host=host, port=port)
        return

    # Status
    if args.cmd == "status":
        print(f"Automation: {'ENABLED' if cfg.get('automation_enabled', True) else 'DISABLED'}")
        print("Pins:")
        for pin_str, meta in cfg["pins"].items():
            pin = int(pin_str)
            name = meta.get("name", f"Pin {pin}")
            state = "ON" if pinman.get_state(pin) else "OFF"
            print(f"  GPIO {pin:2}  {name:<12}  state={state}")
        print("\nSchedules:")
        if not cfg.get("schedules"):
            print("  (none)")
        else:
            for s in cfg["schedules"]:
                days = ",".join(DAY_NAMES[d] for d in s.get("days", []))
                status = "EN" if s.get("enabled", True) else "DIS"
                print(f"  {s['id']} | pin {s['pin']:2} | {s['on']}->{s['off']} | {days} | {status}")
        return

    # Pin on
    if args.cmd == "on":
        pinman.on(args.pin, seconds=args.seconds)
        print("OK")
        return

    # Pin off
    if args.cmd == "off":
        pinman.off(args.pin)
        print("OK")
        return

    # Schedule management
    if args.cmd == "schedule":
        if args.sch_cmd == "add":
            # Validate times
            try:
                datetime.strptime(args.on, "%H:%M")
                datetime.strptime(args.off, "%H:%M")
            except ValueError:
                print("Time must be HH:MM (24h).", file=sys.stderr)
                sys.exit(1)
            # Parse days
            days = parse_days(args.days)
            entry = {
                "id": str(uuid.uuid4()),
                "pin": int(args.pin),
                "on": args.on,
                "off": args.off,
                "days": days,
                "enabled": True,
            }
            with LOCK:
                cfg["schedules"].append(entry)
                save_config(cfg)
            scheduler.reload_jobs()
            print("Added schedule:", entry["id"])
            return
        if args.sch_cmd == "list":
            for s in cfg.get("schedules", []):
                if args.pin is not None and int(s["pin"]) != args.pin:
                    continue
                days_str = ",".join(DAY_NAMES[d] for d in s.get("days", []))
                status = "EN" if s.get("enabled", True) else "DIS"
                print(f"{s['id']} | pin {s['pin']:2} | {s['on']}->{s['off']} | {days_str} | {status}")
            return
        if args.sch_cmd == "delete":
            with LOCK:
                before = len(cfg["schedules"])
                cfg["schedules"] = [s for s in cfg["schedules"] if s["id"] != args.id]
                save_config(cfg)
            scheduler.reload_jobs()
            print(f"Deleted {before - len(cfg['schedules'])}")
            return
        if args.sch_cmd in ("enable", "disable"):
            enable = args.sch_cmd == "enable"
            with LOCK:
                for s in cfg["schedules"]:
                    if s["id"] == args.id:
                        s["enabled"] = enable
                save_config(cfg)
            scheduler.reload_jobs()
            print("OK")
            return

    # Automation toggle
    if args.cmd == "automation":
        state = (args.state == "on")
        with LOCK:
            cfg["automation_enabled"] = state
            save_config(cfg)
        scheduler.reload_jobs()
        print(f"Automation {'ENABLED' if state else 'DISABLED'}")
        return

    # Time set
    if args.cmd == "time" and args.time_cmd == "set":
        res = run_cmd(["sudo", "timedatectl", "set-time", args.datetime_str])
        if res.returncode != 0:
            print(res.stderr or res.stdout, file=sys.stderr)
            sys.exit(res.returncode)
        print("Time updated")
        return

    # NTP enable/disable
    if args.cmd == "ntp":
        if args.ntp_cmd == "enable":
            with LOCK:
                cfg["ntp"]["enabled"] = True
                cfg["ntp"]["server"] = args.server
                save_config(cfg)
            # Write timesyncd config
            conf = f"[Time]\nNTP={args.server}\n"
            try:
                tmp = "/tmp/timesyncd.conf.tmp"
                with open(tmp, "w") as f:
                    f.write(conf)
                run_cmd(["sudo", "cp", tmp, "/etc/systemd/timesyncd.conf"])
                run_cmd(["sudo", "systemctl", "restart", "systemd-timesyncd"])
            except Exception:
                pass
            res = run_cmd(["sudo", "timedatectl", "set-ntp", "true"])
            print(res.stderr or "NTP enabled")
            return
        if args.ntp_cmd == "disable":
            with LOCK:
                cfg["ntp"]["enabled"] = False
                save_config(cfg)
            res = run_cmd(["sudo", "timedatectl", "set-ntp", "false"])
            print(res.stderr or "NTP disabled")
            return

    # Preset command removed; presets cannot be applied via CLI

    # Rain delay management
    if args.cmd == "raindelay":
        # When no subcommand or 'status' is provided, display current status
        if getattr(args, 'rd_cmd', None) is None or args.rd_cmd == "status":
            st = rain_manager.status()
            enabled_str = "ENABLED" if st["enabled"] else "DISABLED"
            prob = st["probability"]
            prob_str = f"{prob:.0f}%" if prob is not None else "N/A"
            active_str = "YES" if st.get("active") else "NO"
            print(f"Rain delay {enabled_str}\n  Threshold: {st['threshold']}%\n  Current chance: {prob_str}\n  Active: {active_str}")
            return
        # Enable rain delay
        if args.rd_cmd == "enable":
            with LOCK:
                cfg.setdefault("rain_delay", {})["enabled"] = True
                save_config(cfg)
            print("Rain delay enabled")
            return
        # Disable rain delay
        if args.rd_cmd == "disable":
            with LOCK:
                cfg.setdefault("rain_delay", {})["enabled"] = False
                save_config(cfg)
            print("Rain delay disabled")
            return
        # Set threshold
        if args.rd_cmd == "threshold":
            with LOCK:
                cfg.setdefault("rain_delay", {})["threshold"] = float(args.value)
                save_config(cfg)
            print(f"Rain delay threshold set to {args.value}%")
            return

    # Service install/uninstall
    if args.cmd == "service":
        service_name = "sprinkler"
        service_path = f"/etc/systemd/system/{service_name}.service"
        if args.srv_cmd == "install":
            here = os.path.abspath(__file__)
            unit = f"""[Unit]
Description=GPIO Sprinkler Controller
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 {here} web --host {cfg['web']['host']} --port {cfg['web']['port']}
WorkingDirectory={os.path.dirname(here)}
Restart=on-failure
User=root

[Install]
WantedBy=multi-user.target
"""
            tmp = "/tmp/sprinkler.service.tmp"
            with open(tmp, "w") as f:
                f.write(unit)
            run_cmd(["sudo", "cp", tmp, service_path])
            run_cmd(["sudo", "systemctl", "daemon-reload"])
            print("Service installed. Enable with: sudo systemctl enable --now sprinkler")
            return
        if args.srv_cmd == "uninstall":
            run_cmd(["sudo", "systemctl", "disable", "--now", service_name])
            run_cmd(["sudo", "rm", "-f", service_path])
            run_cmd(["sudo", "systemctl", "daemon-reload"])
            print("Service removed.")
            return

    # Fallback: if we reach here, show manual
    print_info()


def parse_days(day_str: str) -> List[int]:
    """Convert day strings into a list of integers (0=Mon .. 6=Sun)."""
    s = day_str.strip().lower()
    if s in ("daily", "everyday", "all"):
        return list(range(7))
    if s in ("weekdays",):
        return [0, 1, 2, 3, 4]
    if s in ("weekends",):
        return [5, 6]
    parts = [p.strip() for p in s.split(",") if p.strip()]
    out = []
    for p in parts:
        if p.isdigit():
            v = int(p)
            if 0 <= v <= 6:
                out.append(v)
            else:
                raise ValueError(f"Unknown day token: {p}")
        else:
            try:
                idx = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(p[:3])
                out.append(idx)
            except ValueError:
                raise ValueError(f"Unknown day token: {p}")
    return sorted(set(out))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
sprinkler.py

This script provides a fully‑automated GPIO sprinkler controller designed for
Raspberry Pi.  It builds upon the earlier example by adding two preset
schedules and accommodating the specific set of pins used by the
requester.  The presets allow you to switch between a “regular season”
schedule and a “high‑frequency” schedule with a single command.  It also
exposes a simple CLI and optional web UI for monitoring and adjusting
state at runtime.

Key features:

  • Pins 5, 6, 11, 12, 16, 19, 20, 21 and 26 are defined in the default
    configuration.  Pin 19 is designated as the garden zone.
  • A `preset season` command builds a schedule where each active zone
    runs sequentially for 30 minutes every day, except the garden zone
    (19) which only runs on Tuesday, Thursday and Saturday.
  • A `preset highfreq` command creates a high‑frequency schedule where
    each non‑garden zone runs for 20 minutes at four evenly spaced
    intervals (midnight, 6 am, noon and 6 pm).  The garden zone is
    excluded from this preset.
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
except Exception:
    # If apscheduler is not available (e.g., in certain development
    # environments), define minimal stub classes so the module can be
    # imported without errors.  These stubs do nothing but satisfy the
    # interface used by this script.
    class BackgroundScheduler:
        def __init__(self, daemon: bool = True):
            self._jobs = []
        def start(self):
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

# =========================
# Web (Flask)
# =========================
try:
    from flask import Flask, jsonify, request, Response  # type: ignore
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

# ---------- Default Config ----------
#
# All pins are defined with BCM numbers.  Each entry can be customised
# later by editing the generated config.json.  `active_high` determines
# whether writing a logical 1 energises the relay.  `initial` controls
# whether the zone is active at startup (all zones are off by default).

DEFAULT_CONFIG = {
    "pins": {
        "12": {"name": "Slot 1", "mode": "out", "active_high": True, "initial": False},
        "16": {"name": "Slot 2", "mode": "out", "active_high": True, "initial": False},
        "20": {"name": "Slot 3", "mode": "out", "active_high": True, "initial": False},
        "21": {"name": "Slot 4", "mode": "out", "active_high": True, "initial": False},
        "26": {"name": "Slot 5", "mode": "out", "active_high": True, "initial": False},
        "19": {"name": "Slot 6", "mode": "out", "active_high": True, "initial": False},    
        "13": {"name": "Slot 7", "mode": "out", "active_high": True, "initial": False},
        "6":  {"name": "Slot 8", "mode": "out", "active_high": True, "initial": False},
        "5":  {"name": "Slot 9", "mode": "out", "active_high": True, "initial": False}, 
        "11": {"name": "Slot 10", "mode": "out", "active_high": True, "initial": False},
        "9":  {"name": "Slot 11", "mode": "out", "active_high": True, "initial": False},
        "10": {"name": "Slot 12", "mode": "out", "active_high": True, "initial": False},
        "22": {"name": "Slot 13", "mode": "out", "active_high": True, "initial": False},
        "27": {"name": "Slot 14", "mode": "out", "active_high": True, "initial": False},
        "17": {"name": "Slot 15", "mode": "out", "active_high": True, "initial": False},
        "4":  {"name": "Slot 16",  "mode": "out", "active_high": True, "initial": False},
        "18": {"name": "Spare 1", "mode": "out", "active_high": True, "initial": False},  
        "23": {"name": "Spare 2", "mode": "out", "active_high": True, "initial": False},
        "24": {"name": "Spare 3", "mode": "out", "active_high": True, "initial": False},
        "25": {"name": "Spare 4", "mode": "out", "active_high": True, "initial": False},
        
    },
    # Schedule definitions live in this list.  Preset commands will
    # repopulate this list.  Each entry contains: id, pin, on, off,
    # days (list of weekday numbers) and enabled.
    "schedules": [],
    "automation_enabled": True,
    # Global system master switch.  When set to False the controller
    # disables all automation, refuses new manual ON commands and
    # immediately turns every zone off.  A value of True re‑enables
    # automation and restores normal operation.  Toggling the system
    # also updates automation_enabled accordingly.
    "system_enabled": True,
    # Order of pins displayed in the UI.  When missing, pins are
    # sorted numerically.  This list holds the BCM numbers as strings
    # corresponding to keys in the "pins" mapping.  Drag‑and‑drop
    # reordering in the UI updates this list.
    "pin_order": [],
    # Order of schedule IDs.  When missing the order in the
    # "schedules" list is used.  Drag‑and‑drop reordering updates this
    # list.  Each value corresponds to the "id" of a schedule entry.
    "schedule_order": [],
    # Run log storing history of ON/OFF events.  Each element is a dict
    # with keys: time (ISO8601 string), pin (int), action ('on'/'off'),
    # trigger (manual, schedule, timer, system, etc.).  The log is
    # truncated to the most recent log_max entries on every update.
    "run_log": [],
    # Maximum number of entries to retain in the run_log.  Older
    # entries are dropped when the log grows beyond this size.
    "log_max": 200,
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
    """Wraps gpiozero DigitalOutputDevice for each configured pin.

    A RunLog may be supplied to record every activation and
    deactivation of a zone.  When provided, on() and off() will
    create log entries capturing the pin number, the action ('on' or
    'off') and the trigger string used to invoke the call (manual,
    schedule, timer, system, etc.).
    """
    def __init__(self, config: dict, run_log: 'RunLog | None' = None):
        self.devices: Dict[int, DigitalOutputDevice] = {}
        # timers keyed by pin number for manual pulses
        self.pending_timers: Dict[int, threading.Timer] = {}
        self.run_log: RunLog | None = run_log
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

    def on(self, pin: int, seconds: int | None = None, trigger: str = "manual"):
        """Activate a pin and optionally schedule it to turn off.

        Parameters:
          pin: BCM pin number to activate.
          seconds: Optional duration after which the pin will be turned
            off automatically.  If provided, the same trigger string is
            forwarded to the off() call when the timer expires.
          trigger: Human readable source of the call ('manual', 'schedule',
            'timer', 'system', etc.) which is recorded in the run log.
        """
        with LOCK:
            dev = self.devices[int(pin)]
            dev.on()
            # record log
            if self.run_log:
                try:
                    self.run_log.add_event(pin, "on", trigger)
                except Exception:
                    pass
            self._cancel_timer(pin)
            if seconds is not None and seconds > 0:
                # schedule off() with same trigger; in this context the
                # trigger is likely manual (manual-run), so we propagate
                def _off_wrapper():
                    try:
                        self.off(pin, trigger=trigger)
                    except Exception:
                        pass
                t = threading.Timer(seconds, _off_wrapper)
                t.daemon = True
                t.start()
                self.pending_timers[pin] = t

    def off(self, pin: int, trigger: str = "manual"):
        """Deactivate a pin immediately.

        Parameters:
          trigger: Human readable source of the call ('manual', 'schedule',
            'timer', 'system', etc.) recorded in the run log.
        """
        with LOCK:
            dev = self.devices[int(pin)]
            dev.off()
            if self.run_log:
                try:
                    self.run_log.add_event(pin, "off", trigger)
                except Exception:
                    pass
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

    # Provide sensible defaults for newly introduced fields.  Pin order
    # defaults to all configured pins sorted numerically when not
    # present or empty.  Schedule order defaults to the order of
    # schedules in the list.  The run_log is always initialised to an
    # empty list if absent.  The system master flag defaults to True.
    # log_max defaults to the configured default value.
    if not cfg.get("pin_order"):
        # Convert keys to ints for correct numerical sort then back to
        # strings to match stored key type
        cfg["pin_order"] = [str(p) for p in sorted([int(x) for x in cfg.get("pins", {}).keys()])]
    # Ensure schedule_order exists and contains all known schedule ids
    if not cfg.get("schedule_order"):
        cfg["schedule_order"] = [s.get("id") for s in cfg.get("schedules", []) if s.get("id")]
    else:
        # Remove ids no longer present (e.g. after deletion) and append
        # new ids at the end
        existing_ids = {s.get("id") for s in cfg.get("schedules", [])}
        new_order = [i for i in cfg["schedule_order"] if i in existing_ids]
        # Append any schedules not already in the order
        for s in cfg.get("schedules", []):
            sid = s.get("id")
            if sid and sid not in new_order:
                new_order.append(sid)
        cfg["schedule_order"] = new_order
    if "run_log" not in cfg:
        cfg["run_log"] = []
    if "system_enabled" not in cfg:
        cfg["system_enabled"] = True
    if "log_max" not in cfg:
        cfg["log_max"] = DEFAULT_CONFIG.get("log_max", 200)
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

    def __init__(self, pinman: PinManager, cfg: dict, rain: RainDelayManager | None = None, run_log: 'RunLog | None' = None):
        self.pinman = pinman
        self.cfg = cfg
        self.rain = rain
        # Optional run log used by schedule wrappers to attribute
        # activations/deactivations.  Currently unused directly
        # because PinManager handles logging itself when called with
        # appropriate trigger arguments.
        self.run_log = run_log
        # Initialise underlying APScheduler.  When timezone support is
        # available we rely on the scheduler default; older versions or
        # stub implementations ignore timezone arguments.  See docs for
        # details.
        try:
            self.sched = BackgroundScheduler(daemon=True)
        except Exception:
            # fallback to stub scheduler
            self.sched = BackgroundScheduler(daemon=True)

    def start(self):
        self.sched.start()
        self.reload_jobs()

    def shutdown(self):
        self.sched.shutdown(wait=False)

    def _maybe_on(self, pin: int):
        """Called by scheduled ON jobs; respects rain delay."""
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
        # Use a 'schedule' trigger so the run log records the origin
        self.pinman.on(pin, trigger="schedule")

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
        if not self.cfg.get("automation_enabled", True):
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
                    # OFF job
                    trig_off = CronTrigger(day_of_week=di, hour=off_hour, minute=off_minute)
                    # schedule a wrapper that calls off() with the
                    # appropriate schedule trigger.  Using a lambda
                    # ensures the trigger string is bound correctly.
                    def _off_wrapper(p=pin):
                        try:
                            self.pinman.off(p, trigger="schedule")
                        except Exception:
                            pass
                    self.sched.add_job(
                        _off_wrapper,
                        trigger=trig_off,
                        id=f"{entry['id']}-off-{di}",
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
    return schedules


def build_highfreq_schedules() -> List[dict]:
    """Construct the high‑frequency schedule.

    Pins 5,6,11,12,16,20,21 and 26 run for 20 minutes each,
    sequentially at four times per day: 00:00, 06:00, 12:00 and 18:00.
    The garden zone (19) is excluded from this preset.
    """
    order = [5, 6, 11, 12, 16, 20, 21, 26]
    cycle_starts = ["00:00", "06:00", "12:00", "18:00"]
    dur = timedelta(minutes=20)
    schedules = []
    for start_str in cycle_starts:
        base_start = datetime.strptime(start_str, "%H:%M")
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
    return schedules


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


# =========================
# Run log
# =========================
class RunLog:
    """Maintain a rolling log of zone activations and deactivations.

    The log persists into the config dictionary under the 'run_log'
    key.  Each entry is a dictionary with the following keys:

      • time: ISO8601 timestamp when the event occurred (local time)
      • pin: GPIO pin (int)
      • action: 'on' or 'off'
      • trigger: a short string describing the origin (e.g. 'manual',
        'schedule', 'timer', 'system')

    The size of the log is capped to cfg['log_max'] entries.  Older
    entries are dropped on insert.  All updates persist the config via
    save_config() so logs survive restarts.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        # ensure keys exist
        cfg.setdefault("run_log", [])
        cfg.setdefault("log_max", DEFAULT_CONFIG.get("log_max", 200))

    def add_event(self, pin: int, action: str, trigger: str = "manual"):
        now = datetime.now().astimezone().isoformat()
        entry = {
            "time": now,
            "pin": int(pin),
            "action": str(action),
            "trigger": str(trigger),
        }
        with LOCK:
            log = self.cfg.setdefault("run_log", [])
            log.append(entry)
            # Truncate old entries if log exceeds max
            max_len = int(self.cfg.get("log_max", DEFAULT_CONFIG.get("log_max", 200)))
            if len(log) > max_len:
                # remove oldest entries
                del log[0 : len(log) - max_len]
            save_config(self.cfg)

    def get_events(self, n: int | None = None, pin: int | None = None, since: str | None = None):
        """Return a slice of log entries.

        Parameters:
          n: Maximum number of entries to return (latest first).  If None,
             returns all available.  Negative values behave like None.
          pin: If provided, filter entries to the specified pin.
          since: ISO8601 timestamp; if provided, only include entries
             after this moment.  Parsing errors fallback to including all.
        Returns a list sorted from most recent to oldest.
        """
        with LOCK:
            events = list(self.cfg.get("run_log", []))
        # Filter by pin
        if pin is not None:
            events = [e for e in events if int(e.get("pin")) == int(pin)]
        # Filter by since
        if since:
            try:
                since_dt = datetime.fromisoformat(since)
                events = [e for e in events if datetime.fromisoformat(e.get("time")) > since_dt]
            except Exception:
                pass
        # Return newest first
        events = list(reversed(events))
        if n and n > 0:
            events = events[:n]
        return events


def build_app(cfg: dict, pinman: PinManager, sched: SprinklerScheduler, rain: RainDelayManager | None = None, run_log: 'RunLog | None' = None) -> Flask:
    """Construct the Flask application for the web UI.

    A RainDelayManager may be passed to expose rain status and allow
    adjustments via the web interface.  When rain is None, rain
    endpoints return empty values and controls are disabled.

    The run_log instance, when provided, exposes a rolling history of
    activations and deactivations via the /api/log endpoint and the
    /run-log page.  If omitted, the log endpoints return empty lists.
    """
    app = Flask(__name__)
    # Inline HTML for the modernised UI.  The page uses a dark theme with
    # white text and organises controls for each pin (zone), allows
    # renaming, manual run timers and schedule management.  Schedule IDs
    # are hidden from view but used internally for updates and deletion.
    # Modernised UI HTML.  See above for explanation.
    HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Sprinkler Controller</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body { font-family: system-ui,sans-serif; background:#0b0f14; color:#e6edf3; margin:0; padding:0; }
    .nav { padding:10px 20px; background:#10161d; display:flex; gap:20px; align-items:center; }
    .nav a { color:#58a6ff; text-decoration:none; font-weight:bold; }
    .nav .spacer { flex:1; }
    .badge { padding:4px 8px; border-radius:6px; font-size:0.8rem; margin-right:8px; }
    .badge.on { background:#2ecc71; color:#0b0f14; }
    .badge.off { background:#e74c3c; color:white; }
    .badge.disabled { background:#7f8c8d; color:#0b0f14; }
    .badge.active { background:#f1c40f; color:#0b0f14; }
    .card { background:#10161d; margin:20px; padding:16px; border-radius:12px; box-shadow:0 0 0 1px #1e2936; }
    details summary { cursor:pointer; font-size:1.2rem; font-weight:bold; outline:none; }
    button { padding:8px 14px; border:0; border-radius:8px; cursor:pointer; font-size:0.9rem; }
    button.on { background:#2ecc71; color:#0b0f14; }
    button.off { background:#e74c3c; color:white; }
    button.muted { background:#233040; color:#9fb1c1; }
    button.gray { background:#555; color:#ddd; }
    input, select { background:#0b0f14; color:#e6edf3; border:1px solid #1e2936; border-radius:6px; padding:6px; font-size:0.9rem; }
    table { width:100%; border-collapse:collapse; margin-top:10px; }
    th, td { padding:6px; border-bottom:1px solid #1e2936; font-size:0.85rem; text-align:left; }
    tr.dragging { opacity:0.5; }
    .pin-row { border:1px solid #1e2936; border-radius:8px; padding:10px; margin-bottom:12px; background:#0f141b; }
    .pin-row.dragging { opacity:0.6; }
    .pin-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:6px; }
    .schedule-controls { display:flex; align-items:center; gap:8px; margin-bottom:10px; flex-wrap:wrap; }
    .preset-buttons { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
    @media (max-width:600px) {
      .pin-header, .schedule-controls, .preset-buttons { flex-direction:column; align-items:flex-start; }
      button { width:100%; margin-top:6px; }
    }
  </style>
</head>
<body>
  <div class="nav">
    <span style="font-size:1.2rem;font-weight:bold;color:#e6edf3;">Sprinkler Controller</span>
    <div class="spacer"></div>
    <a href="/">Home</a>
    <a href="/run-log">Run Log</a>
  </div>
  <div style="padding:0 20px;">
    <div id="statusBadges" style="margin-top:10px;"></div>
  </div>
  <details open>
    <summary>Pins</summary>
    <div id="pinsContainer" style="padding:0 20px;"></div>
  </details>
  <details open>
    <summary>Schedules</summary>
    <div style="padding:0 20px;">
      <div class="schedule-controls">
        <label>Filter <input id="schedFilter" placeholder="Start time filter e.g. 22:" style="width:130px;"></label>
        <button class="muted" onclick="clearFilter()">Clear</button>
      </div>
      <div style="margin-bottom:10px;">
        <strong>Add Schedule:</strong><br/>
        <label>Pin <select id="addPin"></select></label>
        <label>On <input id="addOn" type="text" placeholder="HH:MM" style="width:80px;"></label>
        <label>Off <input id="addOff" type="text" placeholder="HH:MM" style="width:80px;"></label>
        <label>Days</label>
        <div id="addDaysContainer" style="display:flex;gap:4px;margin-top:4px;flex-wrap:wrap;"></div>
        <button class="muted" onclick="addSchedule()">Add</button>
      </div>
      <table id="schedTable"></table>
      <div style="margin-top:8px;">
        <button class="muted" onclick="saveAll()">Save All</button>
      </div>
      <div class="preset-buttons">
        <button class="muted" onclick="applyStandard()">Standard schedule</button>
        <button class="off" onclick="deleteAllSchedules()">Delete All</button>
        <button class="muted" onclick="disableAllSchedules()">Disable All</button>
      </div>
    </div>
  </details>
  <details open>
    <summary>Rain Delay</summary>
    <div id="rainSection" style="padding:0 20px;">
      <div id="rainInfo">Loading...</div>
      <div style="margin-top:10px;" id="rainControls">
        <label>Threshold (%) <input id="rainThreshold" style="width:60px" /></label>
        <button class="muted" onclick="setRain()">Set Threshold</button>
        <button class="on" onclick="enableRain()">Enable</button>
        <button class="off" onclick="disableRain()">Disable</button>
      </div>
    </div>
  </details>
</body>
<script>
// Modern Sprinkler UI JavaScript
const DAY_NAMES = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
let editingCount = 0;
function incEditing(){ editingCount++; }
function decEditing(){ if(editingCount>0) editingCount--; }
let schedSortKey = null;
let schedSortAsc = true;
let schedFilterStr = '';
async function fetchJson(url, opts={}) {
  const resp = await fetch(url, opts);
  if(!resp.ok) {
    const txt = await resp.text();
    throw new Error(txt || 'Request failed');
  }
  return await resp.json();
}
async function refresh() {
  try {
    const status = await fetchJson('/api/status');
    const rainData = await fetchJson('/api/rain');
    renderStatus(status, rainData);
    renderPins(status.pins, status.system_enabled, status.automation_enabled);
    renderSchedules(status.schedules);
    renderRain(rainData);
  } catch(e) {
    console.error(e);
  }
}
function renderStatus(status, rain) {
  const cont = document.getElementById('statusBadges');
  cont.innerHTML = '';
  const sys = document.createElement('span');
  sys.className = 'badge ' + (status.system_enabled ? 'on' : 'off');
  sys.textContent = 'System: ' + (status.system_enabled ? 'ON' : 'OFF');
  cont.appendChild(sys);
  const auto = document.createElement('span');
  auto.className = 'badge ' + (status.automation_enabled ? 'on' : 'off');
  auto.textContent = 'Automation: ' + (status.automation_enabled ? 'ENABLED' : 'DISABLED');
  cont.appendChild(auto);
  if (rain && typeof rain.enabled !== 'undefined') {
    const rb = document.createElement('span');
    if(!rain.enabled) {
      rb.className = 'badge disabled';
      rb.textContent = 'Rain Delay: OFF';
    } else if (rain.active) {
      rb.className = 'badge active';
      rb.textContent = 'Rain Delay: ACTIVE';
    } else {
      rb.className = 'badge on';
      rb.textContent = 'Rain Delay: ON';
    }
    cont.appendChild(rb);
  }
}
function renderPins(pins, systemEnabled, autoEnabled) {
  const container = document.getElementById('pinsContainer');
  container.innerHTML = '';
  const addPinSel = document.getElementById('addPin');
  addPinSel.innerHTML = '';
  pins.forEach((p, idx) => {
    const opt = document.createElement('option');
    opt.value = p.pin;
    opt.textContent = p.pin + ' (' + p.name + ')';
    addPinSel.appendChild(opt);
    const row = document.createElement('div');
    row.className = 'pin-row';
    row.setAttribute('draggable','true');
    row.dataset.pin = p.pin;
    const header = document.createElement('div');
    header.className = 'pin-header';
    const title = document.createElement('div');
    title.innerHTML = '<strong>GPIO ' + p.pin + '</strong>';
    header.appendChild(title);
    if(idx === 0) {
      const sysToggle = document.createElement('div');
      sysToggle.style.marginLeft = 'auto';
      const btn = document.createElement('button');
      if(systemEnabled) {
        btn.className = 'off';
        btn.textContent = 'Turn System OFF';
        btn.onclick = () => setSystem(false);
      } else {
        btn.className = 'on';
        btn.textContent = 'Turn System ON';
        btn.onclick = () => setSystem(true);
      }
      sysToggle.appendChild(btn);
      header.appendChild(sysToggle);
    }
    row.appendChild(header);
    const nameRow = document.createElement('div');
    nameRow.style.marginBottom = '6px';
    const nameLabel = document.createElement('span');
    nameLabel.textContent = 'Name: ';
    nameRow.appendChild(nameLabel);
    const nameInput = document.createElement('input');
    nameInput.value = p.name;
    nameInput.style.width = '160px';
    nameInput.id = 'name-' + p.pin;
    nameInput.onfocus = incEditing;
    nameInput.onblur = decEditing;
    nameRow.appendChild(nameInput);
    const saveBtn = document.createElement('button');
    saveBtn.className = 'muted';
    saveBtn.style.marginLeft = '4px';
    saveBtn.textContent = 'Save';
    saveBtn.onclick = () => { saveName(p.pin); };
    nameRow.appendChild(saveBtn);
    row.appendChild(nameRow);
    const stateRow = document.createElement('div');
    stateRow.style.display = 'flex';
    stateRow.style.alignItems = 'center';
    stateRow.style.flexWrap = 'wrap';
    const stateTxt = document.createElement('span');
    stateTxt.textContent = 'State: ' + (p.is_active ? 'ON' : 'OFF');
    stateTxt.style.marginRight = '10px';
    stateRow.appendChild(stateTxt);
    const btnOn = document.createElement('button');
    btnOn.className = 'on';
    btnOn.textContent = 'On';
    btnOn.disabled = !systemEnabled;
    btnOn.onclick = () => { fetch('/api/pin/' + p.pin + '/on', {method:'POST'}).then(refresh).catch(()=>{}); };
    stateRow.appendChild(btnOn);
    const btnOff = document.createElement('button');
    btnOff.className = 'off';
    btnOff.textContent = 'Off';
    btnOff.style.marginLeft = '4px';
    btnOff.onclick = () => { fetch('/api/pin/' + p.pin + '/off', {method:'POST'}).then(refresh).catch(()=>{}); };
    stateRow.appendChild(btnOff);
    const manLabel = document.createElement('span');
    manLabel.style.marginLeft = '12px';
    manLabel.textContent = 'Minutes:';
    stateRow.appendChild(manLabel);
    const manInput = document.createElement('input');
    manInput.type = 'number';
    manInput.min = '1';
    manInput.max = '180';
    manInput.step = '1';
    manInput.style.width = '60px';
    manInput.value = '10';
    manInput.id = 'manual-' + p.pin;
    manInput.onfocus = incEditing;
    manInput.onblur = decEditing;
    stateRow.appendChild(manInput);
    const manBtn = document.createElement('button');
    manBtn.className = 'muted';
    manBtn.style.marginLeft = '4px';
    manBtn.textContent = 'Start';
    manBtn.disabled = !systemEnabled;
    manBtn.onclick = () => {
      const mins = parseFloat(document.getElementById('manual-' + p.pin).value);
      if(!isFinite(mins) || mins <= 0) { alert('Enter minutes > 0'); return; }
      const secs = Math.round(mins * 60);
      fetch('/api/pin/' + p.pin + '/on?seconds=' + secs, {method:'POST'}).then(refresh).catch(()=>{});
    };
    stateRow.appendChild(manBtn);
    row.appendChild(stateRow);
    container.appendChild(row);
  });
  enableDragOrdering(container, '.pin-row', (order) => {
    fetch('/api/reorder', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({pin_order: order})});
  });
}
function renderSchedules(schedules) {
  const table = document.getElementById('schedTable');
  table.innerHTML = '';
  const headerRow = document.createElement('tr');
  const cols = [
    {key:'pin', label:'Pin (Name)'},
    {key:'on', label:'On'},
    {key:'off', label:'Off'},
    {key:'next_run', label:'Next'},
    {key:'days', label:'Days'},
    {key:'enabled', label:'Enabled'},
    {key:'actions', label:'Actions'}
  ];
  cols.forEach(col => {
    const th = document.createElement('th');
    th.textContent = col.label;
    if(['pin','on','off','next_run'].includes(col.key)) {
      th.style.cursor = 'pointer';
      th.onclick = () => {
        if(schedSortKey === col.key) {
          schedSortAsc = !schedSortAsc;
        } else {
          schedSortKey = col.key;
          schedSortAsc = true;
        }
        renderSchedules(schedules);
      };
    }
    headerRow.appendChild(th);
  });
  table.appendChild(headerRow);
  let filtered = schedules;
  if(schedFilterStr && schedFilterStr.trim() !== '') {
    const f = schedFilterStr.trim().toLowerCase();
    filtered = schedules.filter(s => s.on.toLowerCase().startsWith(f));
  }
  if(schedSortKey) {
    filtered = filtered.slice().sort((a,b) => {
      let av = a[schedSortKey];
      let bv = b[schedSortKey];
      if(schedSortKey === 'pin') { av = a.pin; bv = b.pin; }
      if(av < bv) return schedSortAsc ? -1 : 1;
      if(av > bv) return schedSortAsc ? 1 : -1;
      return 0;
    });
  }
  filtered.forEach(s => {
    const tr = document.createElement('tr');
    tr.setAttribute('draggable','true');
    tr.dataset.id = s.id;
    const tdPin = document.createElement('td');
    tdPin.textContent = s.pin + ' (' + s.pin_name + ')';
    tr.appendChild(tdPin);
    const tdOn = document.createElement('td');
    const inpOn = document.createElement('input');
    inpOn.value = s.on;
    inpOn.style.width = '70px';
    inpOn.id = 'on-' + s.id;
    inpOn.onfocus = incEditing;
    inpOn.onblur = decEditing;
    tdOn.appendChild(inpOn);
    tr.appendChild(tdOn);
    const tdOff = document.createElement('td');
    const inpOff = document.createElement('input');
    inpOff.value = s.off;
    inpOff.style.width = '70px';
    inpOff.id = 'off-' + s.id;
    inpOff.onfocus = incEditing;
    inpOff.onblur = decEditing;
    tdOff.appendChild(inpOff);
    tr.appendChild(tdOff);
    const tdNext = document.createElement('td');
    tdNext.textContent = s.next_run || '';
    tr.appendChild(tdNext);
    const tdDays = document.createElement('td');
    const daysDiv = document.createElement('div');
    daysDiv.style.display = 'flex';
    daysDiv.style.flexWrap = 'wrap';
    daysDiv.style.gap = '4px';
    for(let d=0; d<7; d++) {
      const lbl = document.createElement('label');
      lbl.style.display='flex'; lbl.style.alignItems='center'; lbl.style.marginRight='4px';
      const cb = document.createElement('input');
      cb.type='checkbox';
      cb.value=d;
      cb.id = 'days-' + s.id + '-' + d;
      if(Array.isArray(s.days) && s.days.includes(d)) cb.checked=true;
      cb.onfocus = incEditing;
      cb.onblur = decEditing;
      lbl.appendChild(cb);
      lbl.appendChild(document.createTextNode(DAY_NAMES[d]));
      daysDiv.appendChild(lbl);
    }
    tdDays.appendChild(daysDiv);
    tr.appendChild(tdDays);
    const tdEn = document.createElement('td');
    const chk = document.createElement('input');
    chk.type='checkbox';
    chk.id = 'enabled-' + s.id;
    chk.checked = s.enabled;
    chk.onfocus = incEditing;
    chk.onblur = decEditing;
    tdEn.appendChild(chk);
    tr.appendChild(tdEn);
    const tdAct = document.createElement('td');
    const saveBtn = document.createElement('button');
    saveBtn.className = 'muted';
    saveBtn.textContent = 'Save';
    saveBtn.onclick = () => {
      const newOn = document.getElementById('on-' + s.id).value;
      const newOff = document.getElementById('off-' + s.id).value;
      const enVal = document.getElementById('enabled-' + s.id).checked;
      const daysArr = [];
      for(let d=0; d<7; d++) {
        const cb = document.getElementById('days-' + s.id + '-' + d);
        if(cb && cb.checked) daysArr.push(d);
      }
      fetch('/api/schedule/' + s.id, {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({on:newOn, off:newOff, days:daysArr, enabled:enVal})
      }).then(resp => {
        if(!resp.ok) {
          resp.text().then(t => alert(t));
        }
        refresh();
      });
    };
    tdAct.appendChild(saveBtn);
    const delBtn = document.createElement('button');
    delBtn.className = 'off';
    delBtn.style.marginLeft='4px';
    delBtn.textContent = 'Delete';
    delBtn.onclick = () => {
      if(confirm('Delete this schedule?')) {
        fetch('/api/schedule/' + s.id, {method:'DELETE'}).then(refresh).catch(()=>{});
      }
    };
    tdAct.appendChild(delBtn);
    tr.appendChild(tdAct);
    table.appendChild(tr);
  });
  enableDragOrdering(table, 'tr[data-id]', (order) => {
    fetch('/api/reorder', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({schedule_order: order})});
  });
}
function enableDragOrdering(container, selector, callback) {
  let dragged;
  container.querySelectorAll(selector).forEach(item => {
    item.addEventListener('dragstart', function(e){ dragged=this; this.classList.add('dragging'); e.dataTransfer.effectAllowed='move'; });
    item.addEventListener('dragend', function(){ this.classList.remove('dragging'); });
    item.addEventListener('dragover', function(e){ if(e.preventDefault) e.preventDefault(); return false; });
    item.addEventListener('drop', function(e){ e.stopPropagation(); if(dragged && dragged !== this){
      const items = Array.from(container.querySelectorAll(selector));
      const draggedIndex = items.indexOf(dragged);
      const dropIndex = items.indexOf(this);
      if(draggedIndex < dropIndex) {
        container.insertBefore(dragged, this.nextSibling);
      } else {
        container.insertBefore(dragged, this);
      }
      const order = Array.from(container.querySelectorAll(selector)).map(el => el.dataset.pin || el.dataset.id);
      if(callback) callback(order);
    }
    return false; });
  });
}
function saveAll() {
  const pinRows = document.querySelectorAll('.pin-row');
  const pinsData = [];
  pinRows.forEach(row => {
    const pin = parseInt(row.dataset.pin);
    const name = document.getElementById('name-' + pin).value.trim();
    pinsData.push({pin: pin, name: name});
  });
  const scheduleRows = document.querySelectorAll('#schedTable tr[data-id]');
  const schedulesData = [];
  scheduleRows.forEach(tr => {
    const id = tr.dataset.id;
    const onVal = document.getElementById('on-' + id).value;
    const offVal = document.getElementById('off-' + id).value;
    const enVal = document.getElementById('enabled-' + id).checked;
    const daysArr = [];
    for(let d=0; d<7; d++) {
      const cb = document.getElementById('days-' + id + '-' + d);
      if(cb && cb.checked) daysArr.push(d);
    }
    schedulesData.push({id: id, on: onVal, off: offVal, days: daysArr, enabled: enVal});
  });
  const pinOrder = Array.from(document.querySelectorAll('.pin-row')).map(el => el.dataset.pin);
  const scheduleOrder = Array.from(document.querySelectorAll('#schedTable tr[data-id]')).map(el => el.dataset.id);
  fetch('/api/save_all', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({pins: pinsData, schedules: schedulesData, pin_order: pinOrder, schedule_order: scheduleOrder})
  }).then(resp => {
    if(!resp.ok) resp.text().then(t => alert(t));
    refresh();
  }).catch(err => alert(err));
}
function applyStandard() {
  if(!confirm('Apply standard schedule? This will replace all existing schedules.')) return;
  fetch('/api/preset/standard', {method:'POST'}).then(refresh).catch(()=>{});
}
function deleteAllSchedules() {
  if(!confirm('Delete all schedules?')) return;
  fetch('/api/schedules/delete_all', {method:'POST'}).then(refresh).catch(()=>{});
}
function disableAllSchedules() {
  if(!confirm('Disable all schedules?')) return;
  fetch('/api/schedules/disable_all', {method:'POST'}).then(refresh).catch(()=>{});
}
const filterInput = document.getElementById('schedFilter');
if(filterInput) {
  filterInput.addEventListener('input', () => {
    schedFilterStr = filterInput.value;
    refresh();
  });
}
function clearFilter() {
  schedFilterStr = '';
  const fi = document.getElementById('schedFilter');
  if(fi) fi.value = '';
  refresh();
}
function addSchedule() {
  const pin = parseInt(document.getElementById('addPin').value);
  const onVal = document.getElementById('addOn').value;
  const offVal = document.getElementById('addOff').value;
  const daysCbs = document.querySelectorAll('#addDaysContainer input[type="checkbox"]');
  const days = [];
  daysCbs.forEach(cb => { if(cb.checked) days.push(parseInt(cb.value)); });
  const timeRegex = /^\d{1,2}:\d{2}$/;
  if(!onVal || !offVal || !timeRegex.test(onVal) || !timeRegex.test(offVal)) { alert('Please provide times in HH:MM'); return; }
  if(days.length === 0) { alert('Select at least one day'); return; }
  fetch('/api/schedule', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({pin: pin, on: onVal, off: offVal, days: days})}).then(resp => {
    if(!resp.ok) resp.text().then(t => alert(t));
    document.getElementById('addOn').value = '';
    document.getElementById('addOff').value = '';
    daysCbs.forEach(cb => { cb.checked = false; });
    refresh();
  });
}
function saveName(pin) {
  const newName = document.getElementById('name-' + pin).value.trim();
  if(!newName) { alert('Name cannot be empty'); return; }
  fetch('/api/pin/' + pin + '/name', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name:newName})}).then(refresh).catch(()=>{});
}
function renderRain(data) {
  const info = document.getElementById('rainInfo');
  const controls = document.getElementById('rainControls');
  if(!data || typeof data.enabled === 'undefined') {
    info.textContent = 'Rain delay: not supported';
    controls.style.display='none';
    return;
  }
  const enabled = data.enabled;
  const prob = data.probability;
  const active = data.active;
  document.getElementById('rainThreshold').value = data.threshold;
  info.textContent = `Rain delay ${enabled? 'ENABLED':'DISABLED'} | Threshold: ${data.threshold}% | Chance: ${prob !== null ? prob.toFixed(0)+'%' : 'N/A'} | Active: ${active ? 'YES' : 'NO'}`;
}
async function enableRain() { await fetch('/api/rain', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({enabled:true})}); refresh(); }
async function disableRain() { await fetch('/api/rain', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({enabled:false})}); refresh(); }
async function setRain() { const val = parseFloat(document.getElementById('rainThreshold').value); if(!isFinite(val) || val<0 || val>100){ alert('Enter valid threshold 0-100'); return; } await fetch('/api/rain', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({threshold:val})}); refresh(); }
function setSystem(on) {
  fetch('/api/system', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({enabled: !!on})}).then(resp => {
    if(!resp.ok) resp.text().then(t => alert(t));
    refresh();
  });
}
(function initDays(){
  const cont = document.getElementById('addDaysContainer');
  if(cont && cont.children.length === 0) {
    for(let d=0; d<7; d++) {
      const lbl = document.createElement('label');
      lbl.style.display='flex';
      lbl.style.alignItems='center';
      lbl.style.marginRight='4px';
      const cb = document.createElement('input');
      cb.type='checkbox';
      cb.value = d;
      lbl.appendChild(cb);
      lbl.appendChild(document.createTextNode(DAY_NAMES[d]));
      cont.appendChild(lbl);
    }
  }
})();
refresh();
setInterval(() => { if(editingCount === 0) refresh(); }, 5000);
</script>
</html>"""

    # ------------------------------------------------------------------
    # Override the legacy HTML with a modernised interface implementing
    # drag‑and‑drop reordering, batch save, global system toggle, presets,
    # filtering and sorting, and run log navigation.  This new HTML
    # completely replaces the earlier embedded template but is defined
    # here after the original string so that index() will return this
    # updated content.  Any references to the old HTML remain unused.
    HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Sprinkler Controller</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body { font-family: system-ui,sans-serif; background:#0b0f14; color:#e6edf3; margin:0; padding:0; }
    .nav { padding:10px 20px; background:#10161d; display:flex; gap:20px; align-items:center; }
    .nav a { color:#58a6ff; text-decoration:none; font-weight:bold; }
    .nav .spacer { flex:1; }
    h1 { margin:20px; font-size:1.6rem; }
    .badge { padding:4px 8px; border-radius:6px; font-size:0.8rem; margin-right:8px; }
    .badge.on { background:#2ecc71; color:#0b0f14; }
    .badge.off { background:#e74c3c; color:white; }
    .badge.disabled { background:#7f8c8d; color:#0b0f14; }
    .badge.active { background:#f1c40f; color:#0b0f14; }
    .card { background:#10161d; margin:20px; padding:16px; border-radius:12px; box-shadow:0 0 0 1px #1e2936; }
    details summary { cursor:pointer; font-size:1.2rem; font-weight:bold; outline:none; }
    button { padding:8px 14px; border:0; border-radius:8px; cursor:pointer; font-size:0.9rem; }
    button.on { background:#2ecc71; color:#0b0f14; }
    button.off { background:#e74c3c; color:white; }
    button.muted { background:#233040; color:#9fb1c1; }
    button.gray { background:#555; color:#ddd; }
    input, select { background:#0b0f14; color:#e6edf3; border:1px solid #1e2936; border-radius:6px; padding:6px; font-size:0.9rem; }
    table { width:100%; border-collapse:collapse; margin-top:10px; }
    th, td { padding:6px; border-bottom:1px solid #1e2936; font-size:0.85rem; text-align:left; }
    tr.dragging { opacity:0.5; }
    .pin-row { border:1px solid #1e2936; border-radius:8px; padding:10px; margin-bottom:12px; background:#0f141b; }
    .pin-row.dragging { opacity:0.6; }
    .pin-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:6px; }
    .schedule-controls { display:flex; align-items:center; gap:8px; margin-bottom:10px; flex-wrap:wrap; }
    .preset-buttons { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
    @media (max-width:600px) {
      .pin-header, .schedule-controls, .preset-buttons { flex-direction:column; align-items:flex-start; }
      button { width:100%; margin-top:6px; }
    }
  </style>
</head>
<body>
  <div class="nav">
    <span style="font-size:1.2rem;font-weight:bold;color:#e6edf3;">Sprinkler Controller</span>
    <div class="spacer"></div>
    <a href="/">Home</a>
    <a href="/run-log">Run Log</a>
  </div>
  <div style="padding:0 20px;">
    <div id="statusBadges" style="margin-top:10px;"></div>
  </div>
  <details open>
    <summary>Pins</summary>
    <div id="pinsContainer" style="padding:0 20px;"></div>
  </details>
  <details open>
    <summary>Schedules</summary>
    <div style="padding:0 20px;">
      <div class="schedule-controls">
        <label>Filter <input id="schedFilter" placeholder="Start time filter e.g. 22:" style="width:130px;"></label>
        <button class="muted" onclick="clearFilter()">Clear</button>
      </div>
      <div style="margin-bottom:10px;">
        <strong>Add Schedule:</strong><br/>
        <label>Pin <select id="addPin"></select></label>
        <label>On <input id="addOn" type="text" placeholder="HH:MM" style="width:80px;"></label>
        <label>Off <input id="addOff" type="text" placeholder="HH:MM" style="width:80px;"></label>
        <label>Days</label>
        <div id="addDaysContainer" style="display:flex;gap:4px;margin-top:4px;flex-wrap:wrap;"></div>
        <button class="muted" onclick="addSchedule()">Add</button>
      </div>
      <table id="schedTable"></table>
      <div style="margin-top:8px;">
        <button class="muted" onclick="saveAll()">Save All</button>
      </div>
      <div class="preset-buttons">
        <button class="muted" onclick="applyStandard()">Standard schedule</button>
        <button class="off" onclick="deleteAllSchedules()">Delete All</button>
        <button class="muted" onclick="disableAllSchedules()">Disable All</button>
      </div>
    </div>
  </details>
  <details open>
    <summary>Rain Delay</summary>
    <div id="rainSection" style="padding:0 20px;">
      <div id="rainInfo">Loading...</div>
      <div style="margin-top:10px;" id="rainControls">
        <label>Threshold (%) <input id="rainThreshold" style="width:60px" /></label>
        <button class="muted" onclick="setRain()">Set Threshold</button>
        <button class="on" onclick="enableRain()">Enable</button>
        <button class="off" onclick="disableRain()">Disable</button>
      </div>
    </div>
  </details>
</body>
<script>
// Modern Sprinkler UI JavaScript
const DAY_NAMES = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
let editingCount = 0;
function incEditing(){ editingCount++; }
function decEditing(){ if(editingCount>0) editingCount--; }
let schedSortKey = null;
let schedSortAsc = true;
let schedFilterStr = '';
async function fetchJson(url, opts={}) {
  const resp = await fetch(url, opts);
  if(!resp.ok) {
    const txt = await resp.text();
    throw new Error(txt || 'Request failed');
  }
  return await resp.json();
}
async function refresh() {
  try {
    const status = await fetchJson('/api/status');
    const rainData = await fetchJson('/api/rain');
    renderStatus(status, rainData);
    renderPins(status.pins, status.system_enabled, status.automation_enabled);
    renderSchedules(status.schedules);
    renderRain(rainData);
  } catch(e) {
    console.error(e);
  }
}
function renderStatus(status, rain) {
  const cont = document.getElementById('statusBadges');
  cont.innerHTML = '';
  const sys = document.createElement('span');
  sys.className = 'badge ' + (status.system_enabled ? 'on' : 'off');
  sys.textContent = 'System: ' + (status.system_enabled ? 'ON' : 'OFF');
  cont.appendChild(sys);
  const auto = document.createElement('span');
  auto.className = 'badge ' + (status.automation_enabled ? 'on' : 'off');
  auto.textContent = 'Automation: ' + (status.automation_enabled ? 'ENABLED' : 'DISABLED');
  cont.appendChild(auto);
  if (rain && typeof rain.enabled !== 'undefined') {
    const rb = document.createElement('span');
    if(!rain.enabled) {
      rb.className = 'badge disabled';
      rb.textContent = 'Rain Delay: OFF';
    } else if (rain.active) {
      rb.className = 'badge active';
      rb.textContent = 'Rain Delay: ACTIVE';
    } else {
      rb.className = 'badge on';
      rb.textContent = 'Rain Delay: ON';
    }
    cont.appendChild(rb);
  }
}
function renderPins(pins, systemEnabled, autoEnabled) {
  const container = document.getElementById('pinsContainer');
  container.innerHTML = '';
  const addPinSel = document.getElementById('addPin');
  addPinSel.innerHTML = '';
  pins.forEach((p, idx) => {
    const opt = document.createElement('option');
    opt.value = p.pin;
    opt.textContent = p.pin + ' (' + p.name + ')';
    addPinSel.appendChild(opt);
    const row = document.createElement('div');
    row.className = 'pin-row';
    row.setAttribute('draggable','true');
    row.dataset.pin = p.pin;
    const header = document.createElement('div');
    header.className = 'pin-header';
    const title = document.createElement('div');
    title.innerHTML = '<strong>GPIO ' + p.pin + '</strong>';
    header.appendChild(title);
    if(idx === 0) {
      const sysToggle = document.createElement('div');
      sysToggle.style.marginLeft = 'auto';
      const btn = document.createElement('button');
      if(systemEnabled) {
        btn.className = 'off';
        btn.textContent = 'Turn System OFF';
        btn.onclick = () => setSystem(false);
      } else {
        btn.className = 'on';
        btn.textContent = 'Turn System ON';
        btn.onclick = () => setSystem(true);
      }
      sysToggle.appendChild(btn);
      header.appendChild(sysToggle);
    }
    row.appendChild(header);
    const nameRow = document.createElement('div');
    nameRow.style.marginBottom = '6px';
    const nameLabel = document.createElement('span');
    nameLabel.textContent = 'Name: ';
    nameRow.appendChild(nameLabel);
    const nameInput = document.createElement('input');
    nameInput.value = p.name;
    nameInput.style.width = '160px';
    nameInput.id = 'name-' + p.pin;
    nameInput.onfocus = incEditing;
    nameInput.onblur = decEditing;
    nameRow.appendChild(nameInput);
    const saveBtn = document.createElement('button');
    saveBtn.className = 'muted';
    saveBtn.style.marginLeft = '4px';
    saveBtn.textContent = 'Save';
    saveBtn.onclick = () => { saveName(p.pin); };
    nameRow.appendChild(saveBtn);
    row.appendChild(nameRow);
    const stateRow = document.createElement('div');
    stateRow.style.display = 'flex';
    stateRow.style.alignItems = 'center';
    stateRow.style.flexWrap = 'wrap';
    const stateTxt = document.createElement('span');
    stateTxt.textContent = 'State: ' + (p.is_active ? 'ON' : 'OFF');
    stateTxt.style.marginRight = '10px';
    stateRow.appendChild(stateTxt);
    const btnOn = document.createElement('button');
    btnOn.className = 'on';
    btnOn.textContent = 'On';
    btnOn.disabled = !systemEnabled;
    btnOn.onclick = () => { fetch('/api/pin/' + p.pin + '/on', {method:'POST'}).then(refresh).catch(()=>{}); };
    stateRow.appendChild(btnOn);
    const btnOff = document.createElement('button');
    btnOff.className = 'off';
    btnOff.textContent = 'Off';
    btnOff.style.marginLeft = '4px';
    btnOff.onclick = () => { fetch('/api/pin/' + p.pin + '/off', {method:'POST'}).then(refresh).catch(()=>{}); };
    stateRow.appendChild(btnOff);
    const manLabel = document.createElement('span');
    manLabel.style.marginLeft = '12px';
    manLabel.textContent = 'Minutes:';
    stateRow.appendChild(manLabel);
    const manInput = document.createElement('input');
    manInput.type = 'number';
    manInput.min = '1';
    manInput.max = '180';
    manInput.step = '1';
    manInput.style.width = '60px';
    manInput.value = '10';
    manInput.id = 'manual-' + p.pin;
    manInput.onfocus = incEditing;
    manInput.onblur = decEditing;
    stateRow.appendChild(manInput);
    const manBtn = document.createElement('button');
    manBtn.className = 'muted';
    manBtn.style.marginLeft = '4px';
    manBtn.textContent = 'Start';
    manBtn.disabled = !systemEnabled;
    manBtn.onclick = () => {
      const mins = parseFloat(document.getElementById('manual-' + p.pin).value);
      if(!isFinite(mins) || mins <= 0) { alert('Enter minutes > 0'); return; }
      const secs = Math.round(mins * 60);
      fetch('/api/pin/' + p.pin + '/on?seconds=' + secs, {method:'POST'}).then(refresh).catch(()=>{});
    };
    stateRow.appendChild(manBtn);
    row.appendChild(stateRow);
    container.appendChild(row);
  });
  enableDragOrdering(container, '.pin-row', (order) => {
    fetch('/api/reorder', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({pin_order: order})});
  });
}
function renderSchedules(schedules) {
  const table = document.getElementById('schedTable');
  table.innerHTML = '';
  const headerRow = document.createElement('tr');
  const cols = [
    {key:'pin', label:'Pin (Name)'},
    {key:'on', label:'On'},
    {key:'off', label:'Off'},
    {key:'next_run', label:'Next'},
    {key:'days', label:'Days'},
    {key:'enabled', label:'Enabled'},
    {key:'actions', label:'Actions'}
  ];
  cols.forEach(col => {
    const th = document.createElement('th');
    th.textContent = col.label;
    if(['pin','on','off','next_run'].includes(col.key)) {
      th.style.cursor = 'pointer';
      th.onclick = () => {
        if(schedSortKey === col.key) {
          schedSortAsc = !schedSortAsc;
        } else {
          schedSortKey = col.key;
          schedSortAsc = true;
        }
        renderSchedules(schedules);
      };
    }
    headerRow.appendChild(th);
  });
  table.appendChild(headerRow);
  let filtered = schedules;
  if(schedFilterStr && schedFilterStr.trim() !== '') {
    const f = schedFilterStr.trim().toLowerCase();
    filtered = schedules.filter(s => s.on.toLowerCase().startsWith(f));
  }
  if(schedSortKey) {
    filtered = filtered.slice().sort((a,b) => {
      let av = a[schedSortKey];
      let bv = b[schedSortKey];
      if(schedSortKey === 'pin') { av = a.pin; bv = b.pin; }
      if(av < bv) return schedSortAsc ? -1 : 1;
      if(av > bv) return schedSortAsc ? 1 : -1;
      return 0;
    });
  }
  filtered.forEach(s => {
    const tr = document.createElement('tr');
    tr.setAttribute('draggable','true');
    tr.dataset.id = s.id;
    const tdPin = document.createElement('td');
    tdPin.textContent = s.pin + ' (' + s.pin_name + ')';
    tr.appendChild(tdPin);
    const tdOn = document.createElement('td');
    const inpOn = document.createElement('input');
    inpOn.value = s.on;
    inpOn.style.width = '70px';
    inpOn.id = 'on-' + s.id;
    inpOn.onfocus = incEditing;
    inpOn.onblur = decEditing;
    tdOn.appendChild(inpOn);
    tr.appendChild(tdOn);
    const tdOff = document.createElement('td');
    const inpOff = document.createElement('input');
    inpOff.value = s.off;
    inpOff.style.width = '70px';
    inpOff.id = 'off-' + s.id;
    inpOff.onfocus = incEditing;
    inpOff.onblur = decEditing;
    tdOff.appendChild(inpOff);
    tr.appendChild(tdOff);
    const tdNext = document.createElement('td');
    tdNext.textContent = s.next_run || '';
    tr.appendChild(tdNext);
    const tdDays = document.createElement('td');
    const daysDiv = document.createElement('div');
    daysDiv.style.display = 'flex';
    daysDiv.style.flexWrap = 'wrap';
    daysDiv.style.gap = '4px';
    for(let d=0; d<7; d++) {
      const lbl = document.createElement('label');
      lbl.style.display='flex'; lbl.style.alignItems='center'; lbl.style.marginRight='4px';
      const cb = document.createElement('input');
      cb.type='checkbox';
      cb.value=d;
      cb.id = 'days-' + s.id + '-' + d;
      if(Array.isArray(s.days) && s.days.includes(d)) cb.checked=true;
      cb.onfocus = incEditing;
      cb.onblur = decEditing;
      lbl.appendChild(cb);
      lbl.appendChild(document.createTextNode(DAY_NAMES[d]));
      daysDiv.appendChild(lbl);
    }
    tdDays.appendChild(daysDiv);
    tr.appendChild(tdDays);
    const tdEn = document.createElement('td');
    const chk = document.createElement('input');
    chk.type='checkbox';
    chk.id = 'enabled-' + s.id;
    chk.checked = s.enabled;
    chk.onfocus = incEditing;
    chk.onblur = decEditing;
    tdEn.appendChild(chk);
    tr.appendChild(tdEn);
    const tdAct = document.createElement('td');
    const saveBtn = document.createElement('button');
    saveBtn.className = 'muted';
    saveBtn.textContent = 'Save';
    saveBtn.onclick = () => {
      const newOn = document.getElementById('on-' + s.id).value;
      const newOff = document.getElementById('off-' + s.id).value;
      const enVal = document.getElementById('enabled-' + s.id).checked;
      const daysArr = [];
      for(let d=0; d<7; d++) {
        const cb = document.getElementById('days-' + s.id + '-' + d);
        if(cb && cb.checked) daysArr.push(d);
      }
      fetch('/api/schedule/' + s.id, {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({on:newOn, off:newOff, days:daysArr, enabled:enVal})
      }).then(resp => {
        if(!resp.ok) {
          resp.text().then(t => alert(t));
        }
        refresh();
      });
    };
    tdAct.appendChild(saveBtn);
    const delBtn = document.createElement('button');
    delBtn.className = 'off';
    delBtn.style.marginLeft='4px';
    delBtn.textContent = 'Delete';
    delBtn.onclick = () => {
      if(confirm('Delete this schedule?')) {
        fetch('/api/schedule/' + s.id, {method:'DELETE'}).then(refresh).catch(()=>{});
      }
    };
    tdAct.appendChild(delBtn);
    tr.appendChild(tdAct);
    table.appendChild(tr);
  });
  enableDragOrdering(table, 'tr[data-id]', (order) => {
    fetch('/api/reorder', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({schedule_order: order})});
  });
}
function enableDragOrdering(container, selector, callback) {
  let dragged;
  container.querySelectorAll(selector).forEach(item => {
    item.addEventListener('dragstart', function(e){ dragged=this; this.classList.add('dragging'); e.dataTransfer.effectAllowed='move'; });
    item.addEventListener('dragend', function(){ this.classList.remove('dragging'); });
    item.addEventListener('dragover', function(e){ if(e.preventDefault) e.preventDefault(); return false; });
    item.addEventListener('drop', function(e){ e.stopPropagation(); if(dragged && dragged !== this){
      const items = Array.from(container.querySelectorAll(selector));
      const draggedIndex = items.indexOf(dragged);
      const dropIndex = items.indexOf(this);
      if(draggedIndex < dropIndex) {
        container.insertBefore(dragged, this.nextSibling);
      } else {
        container.insertBefore(dragged, this);
      }
      const order = Array.from(container.querySelectorAll(selector)).map(el => el.dataset.pin || el.dataset.id);
      if(callback) callback(order);
    }
    return false; });
  });
}
function saveAll() {
  const pinRows = document.querySelectorAll('.pin-row');
  const pinsData = [];
  pinRows.forEach(row => {
    const pin = parseInt(row.dataset.pin);
    const name = document.getElementById('name-' + pin).value.trim();
    pinsData.push({pin: pin, name: name});
  });
  const scheduleRows = document.querySelectorAll('#schedTable tr[data-id]');
  const schedulesData = [];
  scheduleRows.forEach(tr => {
    const id = tr.dataset.id;
    const onVal = document.getElementById('on-' + id).value;
    const offVal = document.getElementById('off-' + id).value;
    const enVal = document.getElementById('enabled-' + id).checked;
    const daysArr = [];
    for(let d=0; d<7; d++) {
      const cb = document.getElementById('days-' + id + '-' + d);
      if(cb && cb.checked) daysArr.push(d);
    }
    schedulesData.push({id: id, on: onVal, off: offVal, days: daysArr, enabled: enVal});
  });
  const pinOrder = Array.from(document.querySelectorAll('.pin-row')).map(el => el.dataset.pin);
  const scheduleOrder = Array.from(document.querySelectorAll('#schedTable tr[data-id]')).map(el => el.dataset.id);
  fetch('/api/save_all', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({pins: pinsData, schedules: schedulesData, pin_order: pinOrder, schedule_order: scheduleOrder})
  }).then(resp => {
    if(!resp.ok) resp.text().then(t => alert(t));
    refresh();
  }).catch(err => alert(err));
}
function applyStandard() {
  if(!confirm('Apply standard schedule? This will replace all existing schedules.')) return;
  fetch('/api/preset/standard', {method:'POST'}).then(refresh).catch(()=>{});
}
function deleteAllSchedules() {
  if(!confirm('Delete all schedules?')) return;
  fetch('/api/schedules/delete_all', {method:'POST'}).then(refresh).catch(()=>{});
}
function disableAllSchedules() {
  if(!confirm('Disable all schedules?')) return;
  fetch('/api/schedules/disable_all', {method:'POST'}).then(refresh).catch(()=>{});
}
const filterInput = document.getElementById('schedFilter');
if(filterInput) {
  filterInput.addEventListener('input', () => {
    schedFilterStr = filterInput.value;
    refresh();
  });
}
function clearFilter() {
  schedFilterStr = '';
  const fi = document.getElementById('schedFilter');
  if(fi) fi.value = '';
  refresh();
}
function addSchedule() {
  const pin = parseInt(document.getElementById('addPin').value);
  const onVal = document.getElementById('addOn').value;
  const offVal = document.getElementById('addOff').value;
  const daysCbs = document.querySelectorAll('#addDaysContainer input[type="checkbox"]');
  const days = [];
  daysCbs.forEach(cb => { if(cb.checked) days.push(parseInt(cb.value)); });
  const timeRegex = /^\d{1,2}:\d{2}$/;
  if(!onVal || !offVal || !timeRegex.test(onVal) || !timeRegex.test(offVal)) { alert('Please provide times in HH:MM'); return; }
  if(days.length === 0) { alert('Select at least one day'); return; }
  fetch('/api/schedule', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({pin: pin, on: onVal, off: offVal, days: days})}).then(resp => {
    if(!resp.ok) resp.text().then(t => alert(t));
    document.getElementById('addOn').value = '';
    document.getElementById('addOff').value = '';
    daysCbs.forEach(cb => { cb.checked = false; });
    refresh();
  });
}
function saveName(pin) {
  const newName = document.getElementById('name-' + pin).value.trim();
  if(!newName) { alert('Name cannot be empty'); return; }
  fetch('/api/pin/' + pin + '/name', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name:newName})}).then(refresh).catch(()=>{});
}
function renderRain(data) {
  const info = document.getElementById('rainInfo');
  const controls = document.getElementById('rainControls');
  if(!data || typeof data.enabled === 'undefined') {
    info.textContent = 'Rain delay: not supported';
    controls.style.display='none';
    return;
  }
  const enabled = data.enabled;
  const prob = data.probability;
  const active = data.active;
  document.getElementById('rainThreshold').value = data.threshold;
  info.textContent = `Rain delay ${enabled? 'ENABLED':'DISABLED'} | Threshold: ${data.threshold}% | Chance: ${prob !== null ? prob.toFixed(0)+'%' : 'N/A'} | Active: ${active ? 'YES' : 'NO'}`;
}
async function enableRain() { await fetch('/api/rain', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({enabled:true})}); refresh(); }
async function disableRain() { await fetch('/api/rain', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({enabled:false})}); refresh(); }
async function setRain() { const val = parseFloat(document.getElementById('rainThreshold').value); if(!isFinite(val) || val<0 || val>100){ alert('Enter valid threshold 0-100'); return; } await fetch('/api/rain', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({threshold:val})}); refresh(); }
function setSystem(on) {
  fetch('/api/system', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({enabled: !!on})}).then(resp => {
    if(!resp.ok) resp.text().then(t => alert(t));
    refresh();
  });
}
(function initDays(){
  const cont = document.getElementById('addDaysContainer');
  if(cont && cont.children.length === 0) {
    for(let d=0; d<7; d++) {
      const lbl = document.createElement('label');
      lbl.style.display='flex';
      lbl.style.alignItems='center';
      lbl.style.marginRight='4px';
      const cb = document.createElement('input');
      cb.type='checkbox';
      cb.value = d;
      lbl.appendChild(cb);
      lbl.appendChild(document.createTextNode(DAY_NAMES[d]));
      cont.appendChild(lbl);
    }
  }
})();
refresh();
setInterval(() => { if(editingCount === 0) refresh(); }, 5000);
</script>
</html>"""

    @app.get("/")
    def index():
        return Response(HTML, mimetype="text/html")

    @app.get("/api/status")
    def api_status():
        with LOCK:
            # Build pin view objects keyed by BCM number
            pin_entries = {}
            for pin_str, meta in cfg["pins"].items():
                pin = int(pin_str)
                is_active = pinman.get_state(pin)
                pin_entries[pin] = {
                    "pin": pin,
                    "name": meta.get("name", f"Pin {pin}"),
                    "is_active": is_active,
                }
            # Order pins according to configured pin_order; fall back to
            # numerical ordering for any missing entries
            pins_view = []
            order_list = []
            # Convert stored order to ints for comparison
            for p in cfg.get("pin_order", []):
                try:
                    order_list.append(int(p))
                except Exception:
                    pass
            for p in order_list:
                if p in pin_entries:
                    pins_view.append(pin_entries.pop(p))
            # Append any remaining pins not listed in pin_order
            for p in sorted(pin_entries.keys()):
                pins_view.append(pin_entries[p])
            # Build a mapping of schedule id -> next run time (as aware datetime)
            # by inspecting the internal scheduler jobs.  We only consider
            # the ON jobs (jobs whose id ends with '-on-<day>').  The
            # earliest next_run_time across all selected days is used.
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
                            # Keep the earliest next run
                            existing = next_run_map.get(sched_id)
                            if existing is None or nr < existing:
                                next_run_map[sched_id] = nr
            except Exception:
                # If scheduler introspection fails, just leave next_run_map empty
                next_run_map = {}
            # Build schedule view entries keyed by id
            sched_entries = {}
            for s in cfg.get("schedules", []):
                # Determine next run time string if available
                nr_dt = next_run_map.get(s["id"])
                if nr_dt:
                    try:
                        # Convert to local timezone for display
                        nr_str = nr_dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        nr_str = str(nr_dt)
                else:
                    nr_str = None
                sched_entries[s["id"]] = {
                    "id": s["id"],
                    "pin": int(s["pin"]),
                    "pin_name": cfg["pins"].get(str(s["pin"]), {}).get("name", f"Pin {s['pin']}") ,
                    "on": s["on"],
                    "off": s["off"],
                    "day_names": [DAY_NAMES[d] for d in s.get("days", [])],
                    "days": s.get("days", []),
                    "enabled": bool(s.get("enabled", True)),
                    "next_run": nr_str,
                }
            # Order schedules according to schedule_order
            sch_view = []
            for sid in cfg.get("schedule_order", []):
                if sid in sched_entries:
                    sch_view.append(sched_entries.pop(sid))
            # Append any remaining schedules not listed
            for sid in sched_entries:
                sch_view.append(sched_entries[sid])
            now_dt = datetime.now().astimezone()
            now = now_dt.strftime("%Y-%m-%d %H:%M:%S")
            server_time = now
            server_weekday = now_dt.weekday()
            server_tz = (now_dt.tzinfo.tzname(now_dt) if now_dt.tzinfo else '')
            server_utc_offset = now_dt.strftime('%z')
            # Include system master state in the status payload.  The
            # system_enabled flag controls whether the controller is
            # permitted to activate zones.  When false, manual ON
            # commands are refused and scheduled jobs are suppressed.
            return jsonify({
                "pins": pins_view,
                "schedules": sch_view,
                "automation_enabled": bool(cfg.get("automation_enabled", True)),
                "system_enabled": bool(cfg.get("system_enabled", True)),
                "ntp": cfg.get("ntp", {}),
                "now": now,
                "mock_mode": USE_MOCK,
                "server_time": server_time,
                "server_weekday": server_weekday,
                "server_tz": server_tz,
                "server_utc_offset": server_utc_offset,
            })

    @app.post("/api/pin/<int:pin>/on")
    def api_pin_on(pin: int):
        # Disallow manual activations when the system master switch is
        # disabled.  Return a 400 with a human‑readable message so the
        # front‑end can alert the user.  When enabled, the call is
        # forwarded to PinManager; the trigger defaults to "manual".
        if not bool(cfg.get("system_enabled", True)):
            return ("System is OFF – enable system to run zones", 400)
        seconds = request.args.get("seconds", default=None, type=int)
        # If a seconds parameter is provided via querystring we pass it on;
        # otherwise PinManager will leave the valve on indefinitely until
        # manually turned off by the user.
        pinman.on(pin, seconds, trigger="manual")
        return "OK"

    @app.post("/api/pin/<int:pin>/off")
    def api_pin_off(pin: int):
        # Always allow manual OFF requests, regardless of system state.
        pinman.off(pin, trigger="manual")
        return "OK"

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

    # ------------------------------------------------------------------
    # System master toggle
    # ------------------------------------------------------------------
    @app.post("/api/system")
    def api_system_toggle():
        """Enable or disable the entire sprinkler system.

        When disabled (enabled=false) automation is turned off, all
        valves are immediately deactivated and the PinManager refuses
        subsequent manual ON commands.  When re‑enabled, automation is
        restored to its previous state (stored in cfg["previous_automation_enabled"]).
        Returns the new system_enabled state.
        """
        data = request.get_json(force=True) or {}
        new_enabled = bool(data.get("enabled", False))
        with LOCK:
            current_enabled = bool(cfg.get("system_enabled", True))
            # No change; return current state
            if new_enabled == current_enabled:
                return jsonify({"system_enabled": current_enabled})
            if not new_enabled:
                # Turning system off: remember automation state and disable
                cfg["previous_automation_enabled"] = cfg.get("automation_enabled", True)
                cfg["system_enabled"] = False
                cfg["automation_enabled"] = False
                # Immediately switch all zones off and cancel pending timers
                for p in list(pinman.devices.keys()):
                    try:
                        pinman.off(p, trigger="system")
                    except Exception:
                        pass
                save_config(cfg)
                # Reload scheduler with automation disabled
                sched.reload_jobs()
                return jsonify({"system_enabled": False})
            else:
                # Turning system on: restore previous automation state if present
                cfg["system_enabled"] = True
                prev_auto = cfg.pop("previous_automation_enabled", cfg.get("automation_enabled", True))
                cfg["automation_enabled"] = bool(prev_auto)
                save_config(cfg)
                # Reload scheduler now that automation may have been reenabled
                sched.reload_jobs()
                return jsonify({"system_enabled": True})

    # ------------------------------------------------------------------
    # Batch reorder handler
    # ------------------------------------------------------------------
    @app.post("/api/reorder")
    def api_reorder():
        """Update the UI ordering of pins and schedules.

        Expects JSON with optional `pin_order` and `schedule_order`
        lists.  `pin_order` should contain BCM numbers (strings or
        integers) present in cfg["pins"].  `schedule_order` should
        contain schedule ids present in cfg["schedules"].  Missing or
        invalid entries are ignored.  Persists the new order in
        config.json.
        """
        data = request.get_json(force=True) or {}
        with LOCK:
            changed = False
            if isinstance(data.get("pin_order"), list):
                # Normalise to strings and filter out unknown pins
                new_pin_order = []
                for val in data["pin_order"]:
                    sval = str(val)
                    if sval in cfg.get("pins", {}):
                        new_pin_order.append(sval)
                if new_pin_order:
                    cfg["pin_order"] = new_pin_order
                    changed = True
            if isinstance(data.get("schedule_order"), list):
                # Build set of valid ids
                valid_ids = {s.get("id") for s in cfg.get("schedules", [])}
                new_sched_order = [sid for sid in data["schedule_order"] if sid in valid_ids]
                if new_sched_order:
                    cfg["schedule_order"] = new_sched_order
                    changed = True
            if changed:
                save_config(cfg)
        return "OK"

    # ------------------------------------------------------------------
    # Mass save handler
    # ------------------------------------------------------------------
    @app.post("/api/save_all")
    def api_save_all():
        """Persist all edited pins and schedules in one batch.

        Accepts JSON with keys:
          pins: list of {pin: int, name: str}
          schedules: list of {id: str, on: str, off: str, days: list[int], enabled: bool}
          pin_order: list of pin identifiers (string/int)
          schedule_order: list of schedule ids
        Invalid entries are ignored.  After updating, the
        scheduler is reloaded.
        """
        data = request.get_json(force=True) or {}
        pins_data = data.get("pins") or []
        sch_data = data.get("schedules") or []
        new_pin_order = data.get("pin_order")
        new_sched_order = data.get("schedule_order")
        with LOCK:
            # Update pin names
            if isinstance(pins_data, list):
                for item in pins_data:
                    try:
                        pnum = int(item.get("pin"))
                    except Exception:
                        continue
                    name = str(item.get("name", "")).strip()
                    if not name:
                        continue
                    pmeta = cfg.get("pins", {}).get(str(pnum))
                    if pmeta is not None:
                        pmeta["name"] = name
            # Update schedules
            if isinstance(sch_data, list):
                # Build map of id -> entry for quick lookup
                id_map = {s.get("id"): s for s in cfg.get("schedules", [])}
                for item in sch_data:
                    sid = item.get("id")
                    if sid not in id_map:
                        continue
                    entry = id_map[sid]
                    # on time
                    if "on" in item:
                        val = item.get("on")
                        if isinstance(val, str):
                            entry["on"] = val
                    # off time
                    if "off" in item:
                        val = item.get("off")
                        if isinstance(val, str):
                            entry["off"] = val
                    # enabled flag
                    if "enabled" in item:
                        entry["enabled"] = bool(item.get("enabled"))
                    # days
                    if "days" in item:
                        days_val = item.get("days")
                        if isinstance(days_val, list) and days_val:
                            # ensure ints and within 0-6
                            new_days: List[int] = []
                            for d in days_val:
                                try:
                                    di = int(d)
                                except Exception:
                                    continue
                                if 0 <= di <= 6:
                                    new_days.append(di)
                            if new_days:
                                entry["days"] = new_days
            # Update orders
            if isinstance(new_pin_order, list):
                # Filter unknown pins
                valid_pin_keys = set(cfg.get("pins", {}).keys())
                cfg["pin_order"] = [str(x) for x in new_pin_order if str(x) in valid_pin_keys]
            if isinstance(new_sched_order, list):
                valid_ids = {s.get("id") for s in cfg.get("schedules", [])}
                cfg["schedule_order"] = [sid for sid in new_sched_order if sid in valid_ids]
            save_config(cfg)
        # Reload scheduler to apply changes to times/days/enabled
        sched.reload_jobs()
        return "OK"

    # ------------------------------------------------------------------
    # Standard preset schedule handler
    # ------------------------------------------------------------------
    @app.post("/api/preset/standard")
    def api_preset_standard():
        """Replace all schedules with the example standard schedule.

        This schedule mirrors the arrangement shown in the provided
        screenshot: zones 6,5,11,12,16,20,21,26,19 and 9 run
        sequentially with specific on/off times.  All schedules are
        enabled by default.  Pin 19 (garden) runs only on Tuesday
        through Sunday.  All others run daily.
        """
        # Define the standard configuration: list of tuples of (pin,on,off,days)
        standard = [
            (6,  "22:00", "22:30", list(range(7))),
            (5,  "22:30", "23:00", list(range(7))),
            (11, "23:00", "23:30", list(range(7))),
            (12, "23:30", "00:00", list(range(7))),
            (16, "00:00", "00:30", list(range(7))),
            (20, "00:30", "1:30",  list(range(7))),
            (21, "1:30",  "2:00",  list(range(7))),
            (26, "2:00",  "2:30",  list(range(7))),
            (19, "4:00",  "4:20",  [1,2,3,4,5,6]),  # Tue–Sun
            (9,  "2:30",  "3:00",  list(range(7))),
        ]
        with LOCK:
            cfg["schedules"] = []
            cfg["schedule_order"] = []
            for pin_id, on_time, off_time, days in standard:
                sched_id = str(uuid.uuid4())
                cfg["schedules"].append({
                    "id": sched_id,
                    "pin": pin_id,
                    "on": on_time,
                    "off": off_time,
                    "days": days,
                    "enabled": True,
                })
                cfg["schedule_order"].append(sched_id)
            save_config(cfg)
        # Reload scheduler with new entries
        sched.reload_jobs()
        return "OK"

    # ------------------------------------------------------------------
    # Delete all schedules
    # ------------------------------------------------------------------
    @app.post("/api/schedules/delete_all")
    def api_schedules_delete_all():
        """Remove all schedule entries from the configuration."""
        with LOCK:
            cfg["schedules"] = []
            cfg["schedule_order"] = []
            save_config(cfg)
        sched.reload_jobs()
        return "OK"

    # ------------------------------------------------------------------
    # Disable all schedules
    # ------------------------------------------------------------------
    @app.post("/api/schedules/disable_all")
    def api_schedules_disable_all():
        """Disable every schedule in the configuration without removing it."""
        with LOCK:
            for entry in cfg.get("schedules", []):
                entry["enabled"] = False
            save_config(cfg)
        sched.reload_jobs()
        return "OK"

    # ------------------------------------------------------------------
    # Run log retrieval endpoint
    # ------------------------------------------------------------------
    @app.get("/api/log")
    def api_log_get():
        """Return run log entries.

        Query parameters:
          n (int): maximum number of entries to return (most recent first)
          pin (int): optional pin number to filter on
          since (str): ISO8601 timestamp; include only entries after this time
        Returns a JSON list of log entries sorted newest first.
        """
        if run_log is None:
            return jsonify([])
        n = request.args.get("n", default=None, type=int)
        pin_val = request.args.get("pin", default=None, type=int)
        since = request.args.get("since", default=None, type=str)
        events = run_log.get_events(n, pin_val, since)
        return jsonify(events)

    # ------------------------------------------------------------------
    # Run log page
    # ------------------------------------------------------------------
    @app.get("/run-log")
    def run_log_page():
        """Serve the run log page with dynamic loading via /api/log."""
        runlog_html = """<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <title>Sprinkler Run Log</title>
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <style>
    body { font-family: system-ui,sans-serif; background:#0b0f14; color:#e6edf3; margin:0; padding:0; }
    .nav { padding:10px 20px; background:#10161d; display:flex; gap:20px; align-items:center; }
    .nav .spacer { flex:1; }
    .nav a { color:#58a6ff; text-decoration:none; font-weight:bold; }
    table { width:100%; border-collapse:collapse; margin-top:20px; }
    th, td { padding:6px; border-bottom:1px solid #1e2936; text-align:left; font-size:0.9rem; }
    input { background:#0b0f14; color:#e6edf3; border:1px solid #1e2936; border-radius:6px; padding:4px; }
    button { padding:6px 10px; border-radius:6px; border:0; cursor:pointer; background:#233040; color:#9fb1c1; }
  </style>
</head>
<body>
  <div class=\"nav\">
    <span style=\"font-size:1.2rem;font-weight:bold;color:#e6edf3;\">Sprinkler Run Log</span>
    <div class=\"spacer\"></div>
    <a href=\"/\">Home</a>
  </div>
  <div style=\"padding:20px;\">
    <div style=\"margin-bottom:10px;\">
      <label>Max entries <input id=\"logN\" type=\"number\" value=\"50\" min=\"1\" max=\"1000\" style=\"width:80px;\"></label>
      <label style=\"margin-left:10px;\">Pin <input id=\"logPin\" type=\"number\" style=\"width:60px;\" placeholder=\"all\"></label>
      <label style=\"margin-left:10px;\">Since (ISO) <input id=\"logSince\" type=\"text\" style=\"width:180px;\" placeholder=\"\"></label>
      <button onclick=\"loadLog()\" style=\"margin-left:10px;\">Refresh</button>
    </div>
    <table id=\"logTable\">
      <thead><tr><th>Time</th><th>Pin</th><th>Action</th><th>Trigger</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>
  <script>
  async function loadLog() {
    const nVal = document.getElementById('logN').value;
    const n = nVal ? parseInt(nVal) : null;
    const pinVal = document.getElementById('logPin').value;
    const pin = pinVal ? parseInt(pinVal) : null;
    const since = document.getElementById('logSince').value.trim();
    let url = '/api/log?';
    if(n) url += 'n=' + n + '&';
    if(pinVal) url += 'pin=' + pin + '&';
    if(since) url += 'since=' + encodeURIComponent(since) + '&';
    const resp = await fetch(url);
    const data = await resp.json();
    const tbody = document.getElementById('logTable').querySelector('tbody');
    tbody.innerHTML = '';
    data.forEach(ev => {
      const tr = document.createElement('tr');
      const cells = [ev.time, ev.pin, ev.action, ev.trigger];
      cells.forEach(txt => {
        const td = document.createElement('td');
        td.textContent = txt;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
  }
  loadLog();
  </script>
</body>
</html>
"""
        return Response(runlog_html, mimetype="text/html")

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
your regular 30‑minute sequential schedule, or “High‑frequency” to
apply the 20‑minute repeated schedule.

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

Apply the high‑frequency preset (20‑minute sequential runs at 00:00, 06:00, 12:00 and 18:00):

  ./sprinkler.py preset highfreq

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

""")


def main():
    cfg = load_config()
    # Initialise the run log which persists activation history.  Pass
    # this into PinManager and scheduler so they can record events.
    runlog = RunLog(cfg)
    pinman = PinManager(cfg, runlog)
    # Initialise the rain delay manager before creating the scheduler.
    rain_manager = RainDelayManager(cfg)
    # Pass runlog into the scheduler so scheduled events are logged via
    # PinManager.  The scheduler calls PinManager which already
    # records events, but we provide runlog in case future logic needs
    # it directly.
    scheduler = SprinklerScheduler(pinman, cfg, rain_manager, runlog)

    parser = argparse.ArgumentParser(description="GPIO sprinkler controller")
    sub = parser.add_subparsers(dest="cmd")
    parser.add_argument("-info", action="store_true", help="Show usage manual")

    # web command
    p_web = sub.add_parser("web", help="Run web UI and scheduler")
    p_web.add_argument("--host", default=cfg.get("web", {}).get("host", "0.0.0.0"))
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
        # Pass rain_manager and runlog into the web app so rain status
        # and run log appear on the UI
        app = build_app(cfg, pinman, scheduler, rain_manager, runlog)
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
        # Respect the system master switch on CLI as well.  If the
        # system is disabled, refuse manual activations.
        if not bool(cfg.get("system_enabled", True)):
            print("System is OFF – enable system to run zones")
            return
        pinman.on(args.pin, seconds=args.seconds, trigger="manual")
        print("OK")
        return

    # Pin off
    if args.cmd == "off":
        pinman.off(args.pin, trigger="manual")
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
            try:
                idx = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(p[:3])
                out.append(idx)
            except ValueError:
                raise ValueError(f"Unknown day token: {p}")
    return sorted(set(out))


if __name__ == "__main__":
    main()

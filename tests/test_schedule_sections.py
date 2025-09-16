import json
from pathlib import Path

import sprinkler


def build_app(tmp_path):
    cfg = {
        "pins": {
            "5": {"name": "A", "section": "active", "order": 0},
        },
        "schedules": [],
        "automation_enabled": True,
        "system_enabled": True,
        "config_path": str(tmp_path / "cfg.json"),
    }
    p = Path(cfg["config_path"])
    p.write_text(json.dumps(cfg))
    cfg_obj = json.loads(p.read_text())
    pinman = sprinkler.PinManager(cfg_obj)
    sched = sprinkler.SprinklerScheduler(pinman, cfg_obj)
    app = sprinkler.build_app(cfg_obj, pinman, sched, rain=None)
    app.testing = True
    return app


def test_schedule_section_creation_and_status(tmp_path):
    app = build_app(tmp_path)
    c = app.test_client()

    gid = c.post("/api/schedule-groups", json={"name": "G"}).get_json()["id"]
    assert gid

    r = c.post("/api/schedule", json={"pin": 5, "on": "06:00", "off": "06:10", "days": [0]})
    assert r.status_code == 200

    data = c.get("/api/status").get_json()
    assert "schedule_sections" in data
    sections = data["schedule_sections"]
    assert len(sections) == 1
    assert len(sections[0]["schedules"]) == 1
    assert sections[0]["schedules"][0]["pin"] == 5


def test_overlap_merging_in_scheduler(tmp_path):
    app = build_app(tmp_path)
    c = app.test_client()

    gid = c.post("/api/schedule-groups", json={"name": "G"}).get_json()["id"]
    assert gid

    c.post("/api/schedule", json={"pin": 5, "on": "06:00", "off": "06:20", "days": [1]})
    c.post("/api/schedule", json={"pin": 5, "on": "06:10", "off": "06:30", "days": [1]})

    c.get("/api/status")

    jobs = app.sched.sched.get_jobs()
    on_ids = [j.get("id") if isinstance(j, dict) else getattr(j, "id", "") for j in jobs if ("merged-5-on-1" in (j.get("id") if isinstance(j, dict) else getattr(j, "id", "")))]
    off_ids = [j.get("id") if isinstance(j, dict) else getattr(j, "id", "") for j in jobs if ("merged-5-off-1" in (j.get("id") if isinstance(j, dict) else getattr(j, "id", "")))]
    assert len(on_ids) == 1
    assert len(off_ids) == 1

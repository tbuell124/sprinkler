import json
from pathlib import Path

import sprinkler


def build_app(tmp_path):
    cfg = {
        "pins": {
            "5": {"name": "A", "section": "active", "order": 0},
            "6": {"name": "B", "section": "active", "order": 1},
            "7": {"name": "C", "section": "spare", "order": 0},
        },
        "schedules": [
            {"id": "s1", "pin": 5, "on": "06:00", "off": "06:05", "days": [0, 1, 2], "enabled": True}
        ],
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


def test_status_includes_groups_when_present(tmp_path):
    app = build_app(tmp_path)
    c = app.test_client()
    r = c.get("/api/status")
    assert r.status_code == 200
    data = r.get_json()
    assert "schedule_groups" in data
    assert data["schedule_groups"]["groups"] == []

    r = c.post("/api/schedule-groups", json={"name": "Summer"})
    assert r.status_code == 200
    gid = r.get_json()["id"]

    r = c.get("/api/status")
    data = r.get_json()
    assert data["schedule_groups"]["current"] == gid
    assert len(data["schedule_groups"]["groups"]) == 1
    assert data["schedule_groups"]["groups"][0]["name"] == "Summer"


def test_group_crud_and_select(tmp_path):
    app = build_app(tmp_path)
    c = app.test_client()

    g1 = c.post("/api/schedule-groups", json={"name": "G1"}).get_json()["id"]
    g2 = c.post("/api/schedule-groups", json={"name": "G2"}).get_json()["id"]

    r = c.post("/api/schedule-groups/select", json={"id": g2})
    assert r.status_code == 200

    r = c.delete(f"/api/schedule-groups/{g2}")
    assert r.status_code == 200

    r = c.get("/api/schedule-groups")
    data = r.get_json()
    assert data["current"] == g1
    assert len(data["groups"]) == 1


def test_add_all_active_pins_creates_sequential_schedules(tmp_path):
    app = build_app(tmp_path)
    c = app.test_client()

    gid = c.post("/api/schedule-groups", json={"name": "AllPins"}).get_json()["id"]

    payload = {
        "order": [5, 6],
        "on": "06:00",
        "duration_minutes": 10,
        "gap_minutes": 5,
        "days": "daily",
    }
    r = c.post(f"/api/schedule-groups/{gid}/add-all", json=payload)
    assert r.status_code == 200

    r = c.get("/api/status")
    data = r.get_json()
    sch = data["schedules"]
    assert len(sch) == 2
    assert sch[0]["pin"] == 5
    assert sch[0]["on"] == "06:00"
    assert sch[0]["off"] == "06:10"
    assert sch[1]["pin"] == 6
    assert sch[1]["on"] == "06:15"
    assert sch[1]["off"] == "06:25"


def test_legacy_endpoints_operate_on_current_group_when_present(tmp_path):
    app = build_app(tmp_path)
    c = app.test_client()

    gid = c.post("/api/schedule-groups", json={"name": "LegacyCompat"}).get_json()["id"]
    assert gid

    r = c.post(
        "/api/schedule",
        json={"pin": 5, "on": "07:00", "off": "07:10", "days": "weekdays"},
    )
    assert r.status_code == 200

    r = c.get("/api/status")
    sch = r.get_json()["schedules"]
    order = [s["id"] for s in sch][::-1]
    r = c.post("/api/schedules/reorder", json={"order": order})
    assert r.status_code == 200

    r = c.post("/api/schedule", json={"pin": 6, "on": "08:00", "off": "08:05", "days": [0]})
    sid = r.get_json()["id"]
    r = c.delete(f"/api/schedule/{sid}")
    assert r.status_code == 200

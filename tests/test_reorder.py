import tempfile
import sprinkler
import pytest

pytest.importorskip("flask")

sprinkler.CONFIG_PATH = tempfile.NamedTemporaryFile(delete=False).name


def build_app():
    cfg = {
        "pins": {
            "5": {"name": "Slot 1"},
            "6": {"name": "Slot 2"},
        },
        "schedules": [
            {"id": "a", "pin": 5, "on": "06:00", "off": "06:30", "days": [0], "enabled": True},
            {"id": "b", "pin": 6, "on": "07:00", "off": "07:30", "days": [1], "enabled": True},
        ],
        "automation_enabled": True,
    }
    pinman = sprinkler.PinManager(cfg)
    sched = sprinkler.SprinklerScheduler(pinman, cfg)
    sched.reload_jobs = lambda: None
    app = sprinkler.build_app(cfg, pinman, sched)
    app.testing = True
    return app, cfg


def test_reorder_schedules():
    app, cfg = build_app()
    client = app.test_client()
    resp = client.post('/api/schedules/reorder', json={"order": ["b", "a"]})
    assert resp.status_code == 200
    assert [s["id"] for s in cfg["schedules"]] == ["b", "a"]


def test_reorder_pins():
    app, cfg = build_app()
    client = app.test_client()
    resp = client.post('/api/pins/reorder', json={"active": [6], "spare": [5]})
    assert resp.status_code == 200
    assert list(cfg["pins"].keys()) == ["6", "5"]
    assert cfg["pins"]["6"]["order"] == 0
    assert cfg["pins"]["6"]["section"] == "active"
    assert cfg["pins"]["5"]["order"] == 0
    assert cfg["pins"]["5"]["section"] == "spare"

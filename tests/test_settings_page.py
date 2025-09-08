import tempfile

import sprinkler
import pytest


pytest.importorskip("flask")


sprinkler.CONFIG_PATH = tempfile.NamedTemporaryFile(delete=False).name


def build_app():
    cfg = {
        "pins": {
            "5": {"name": "Front"},
            "6": {"name": "Back"},
        },
        "schedules": [],
        "automation_enabled": True,
    }
    pinman = sprinkler.PinManager(cfg)
    sched = sprinkler.SprinklerScheduler(pinman, cfg)
    sched.reload_jobs = lambda: None
    app = sprinkler.build_app(cfg, pinman, sched)
    app.testing = True
    return app, cfg


def test_settings_lists_pins():
    app, _ = build_app()
    client = app.test_client()
    resp = client.get('/settings')
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Pin Names" in body


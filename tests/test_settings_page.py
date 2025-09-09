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
        "schedules": [],
        "automation_enabled": True,
    }
    pinman = sprinkler.PinManager(cfg)
    sched = sprinkler.SprinklerScheduler(pinman, cfg)
    sched.reload_jobs = lambda: None
    app = sprinkler.build_app(cfg, pinman, sched)
    app.testing = True
    return app


def test_settings_serves_main_ui():
    app = build_app()
    client = app.test_client()
    resp = client.get('/settings')
    assert resp.status_code == 200
    text = resp.data.decode()
    assert 'Sprinkler Controller' in text


def test_root_redirects_to_settings():
    app = build_app()
    client = app.test_client()
    resp = client.get('/')
    assert resp.status_code == 302
    # Flask may include full URL in Location; ensure it ends with /settings
    assert resp.headers['Location'].endswith('/settings')


def test_pin_settings_lists_pins():
    app = build_app()
    client = app.test_client()
    resp = client.get('/pin-settings')
    assert resp.status_code == 200
    text = resp.data.decode()
    assert 'Pin Settings' in text

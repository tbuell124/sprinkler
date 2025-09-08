import sys
import sprinkler


def test_env_host_overrides_default(monkeypatch, tmp_path):
    # ensure config path is temp
    sprinkler.CONFIG_PATH = str(tmp_path / 'config.json')
    # environment variable specifying host
    monkeypatch.setenv("SPRINKLER_HOME", "sprinkler.home")

    captured = {}

    class DummyApp:
        def run(self, host=None, port=None):
            captured['host'] = host
            captured['port'] = port

    # use dummy app
    monkeypatch.setattr(sprinkler, "build_app", lambda cfg, p, s, r: DummyApp())
    # prevent scheduler from starting threads
    monkeypatch.setattr(sprinkler.SprinklerScheduler, "start", lambda self: None)

    # run main with web command
    monkeypatch.setattr(sys, "argv", ["sprinkler.py", "web"])
    sprinkler.main()

    assert captured['host'] == "sprinkler.home"

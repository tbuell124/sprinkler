import sprinkler

class FakeScheduler:
    def __init__(self):
        self.jobs = []
    def start(self):
        pass
    def shutdown(self, wait=False):
        pass
    def get_jobs(self):
        return self.jobs
    def add_job(self, func, args=None, trigger=None, id=None, replace_existing=False, misfire_grace_time=None):
        self.jobs.append({"func": func, "args": args, "trigger": trigger, "id": id})
    def remove_job(self, job_id):
        pass

class DummyCronTrigger:
    def __init__(self, day_of_week=None, hour=None, minute=None):
        self.day_of_week = day_of_week
        self.hour = hour
        self.minute = minute


def test_reload_jobs_crosses_midnight(monkeypatch):
    fake_sched = FakeScheduler()
    monkeypatch.setattr(sprinkler, "BackgroundScheduler", lambda daemon=True: fake_sched)
    monkeypatch.setattr(sprinkler, "CronTrigger", DummyCronTrigger)
    cfg = {
        "pins": {"1": {}},
        "schedules": [
            {
                "id": "mid", "pin": 1,
                "on": "23:50", "off": "00:10",
                "days": [0], "enabled": True,
            }
        ],
        "automation_enabled": True,
    }
    pinman = sprinkler.PinManager(cfg)
    sched = sprinkler.SprinklerScheduler(pinman, cfg)
    sched.reload_jobs()
    jobs = {j["id"]: j for j in fake_sched.get_jobs()}
    assert "mid-on-0" in jobs
    assert "mid-off-1" in jobs
    assert jobs["mid-on-0"]["trigger"].day_of_week == 0
    assert jobs["mid-off-1"]["trigger"].day_of_week == 1

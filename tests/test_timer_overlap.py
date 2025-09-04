import time

import sprinkler


class FakeDevice:
    """Simple stand-in for gpiozero's DigitalOutputDevice."""

    def __init__(self, pin, active_high=True, initial_value=False):
        self.pin = pin
        self.active_high = active_high
        self.value = 1 if initial_value else 0

    def on(self):
        # Turn on and pause so a nearly-expired prior timer can run and
        # switch the device off before cancellation occurs.
        self.value = 1
        time.sleep(0.1)

    def off(self):
        self.value = 0


def test_new_timer_replaces_old_timer(monkeypatch):
    """Starting a new timer should cancel any existing timer for the pin."""

    # Use the fake device so the test does not require GPIO hardware.
    monkeypatch.setattr(sprinkler, "DigitalOutputDevice", FakeDevice)

    pm = sprinkler.PinManager({"pins": {"1": {}}})

    # Start a short timer and wait until it is nearly due.
    pm.on(1, seconds=0.2)
    time.sleep(0.18)

    # Starting another timer should cancel the first before it fires.
    pm.on(1, seconds=0.2)

    # Give the original timer a chance to fire if it wasn't cancelled.
    time.sleep(0.05)

    assert pm.get_state(1) is True

    # Clean up any remaining timer threads.
    pm.off(1)


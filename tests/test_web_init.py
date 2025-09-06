from pathlib import Path

import sprinkler


def test_js_ready_state_init():
    """The embedded UI should bootstrap even if the DOM is already loaded."""
    text = Path(sprinkler.__file__).read_text()
    assert "document.readyState" in text
    assert "DOMContentLoaded', init" in text


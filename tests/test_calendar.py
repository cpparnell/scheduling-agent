import subprocess
from types import SimpleNamespace

import pytest

from scheduling_agent import calendar


@pytest.fixture
def capture_osascript(monkeypatch):
    """Replace subprocess.run with a stub that records the AppleScript and
    returns a configurable result."""
    state = {"script": None, "returncode": 0, "stderr": "", "raise": None}

    def fake_run(args, **kwargs):
        # args == ["osascript", "-e", script]
        state["script"] = args[2]
        if state["raise"] is not None:
            raise state["raise"]
        return SimpleNamespace(returncode=state["returncode"], stderr=state["stderr"])

    monkeypatch.setattr(calendar.subprocess, "run", fake_run)
    return state


def test_success_returns_true(capture_osascript):
    ok = calendar.create_event("Dinner", "2026-06-13", "19:00", 90, None, "Home")
    assert ok is True
    assert "Dinner" in capture_osascript["script"]
    assert 'first calendar whose name is "Home"' in capture_osascript["script"]


def test_nonzero_returncode_returns_false(capture_osascript):
    capture_osascript["returncode"] = 1
    capture_osascript["stderr"] = "no such calendar"
    assert calendar.create_event("Dinner", "2026-06-13", "19:00", 60, None) is False


def test_timeout_returns_false(capture_osascript):
    capture_osascript["raise"] = subprocess.TimeoutExpired(cmd="osascript", timeout=15)
    assert calendar.create_event("Dinner", "2026-06-13", "19:00", 60, None) is False


def test_quotes_in_title_and_location_are_escaped(capture_osascript):
    calendar.create_event('Sam\'s "party"', "2026-06-13", "19:00", 60, 'The "Spot"')
    script = capture_osascript["script"]
    assert '\\"party\\"' in script
    assert '\\"Spot\\"' in script


def test_date_and_time_render_expected_applescript_literal(capture_osascript):
    calendar.create_event("Dinner", "2026-06-13", "19:00", 60, None)
    script = capture_osascript["script"]
    # 19:00 + 60min -> start 7:00:00 PM, end 8:00:00 PM on June 13, 2026.
    assert 'date "June 13, 2026 at 07:00:00 PM"' in script
    assert 'date "June 13, 2026 at 08:00:00 PM"' in script


def test_no_time_defaults_to_noon(capture_osascript):
    calendar.create_event("All day thing", "2026-06-13", None, 60, None)
    assert 'date "June 13, 2026 at 12:00:00 PM"' in capture_osascript["script"]


def test_none_duration_defaults_to_60_minutes(capture_osascript):
    calendar.create_event("Dinner", "2026-06-13", "19:00", None, None)
    script = capture_osascript["script"]
    assert 'date "June 13, 2026 at 07:00:00 PM"' in script
    assert 'date "June 13, 2026 at 08:00:00 PM"' in script


def test_location_omitted_when_none(capture_osascript):
    calendar.create_event("Dinner", "2026-06-13", "19:00", 60, None)
    assert "location:" not in capture_osascript["script"]

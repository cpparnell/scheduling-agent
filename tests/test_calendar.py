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


def test_weekly_recurrence_sets_rrule(capture_osascript):
    calendar.create_event("Standup", "2026-06-13", "10:00", 30, None, recurrence="weekly")
    script = capture_osascript["script"]
    assert 'set recurrence of newEvent to "FREQ=WEEKLY;INTERVAL=1"' in script


def test_biweekly_recurrence_sets_rrule(capture_osascript):
    calendar.create_event("Standup", "2026-06-13", "10:00", 30, None, recurrence="biweekly")
    script = capture_osascript["script"]
    assert 'set recurrence of newEvent to "FREQ=WEEKLY;INTERVAL=2"' in script


def test_no_recurrence_omits_rrule_line(capture_osascript):
    calendar.create_event("Dinner", "2026-06-13", "19:00", 60, None, recurrence=None)
    assert "recurrence" not in capture_osascript["script"]


def test_end_date_overrides_duration_based_end(capture_osascript):
    calendar.create_event("NYC Trip", "2026-06-13", None, 60, None, end_date="2026-06-16")
    script = capture_osascript["script"]
    # Start: June 13 at noon; end: June 16 at noon (same time, end_date day)
    assert 'date "June 13, 2026 at 12:00:00 PM"' in script
    assert 'date "June 16, 2026 at 12:00:00 PM"' in script


def test_end_date_with_time_start(capture_osascript):
    calendar.create_event("Conference", "2026-06-10", "09:00", 60, None, end_date="2026-06-12")
    script = capture_osascript["script"]
    assert 'date "June 10, 2026 at 09:00:00 AM"' in script
    assert 'date "June 12, 2026 at 09:00:00 AM"' in script

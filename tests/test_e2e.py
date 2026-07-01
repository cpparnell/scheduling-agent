"""End-to-end smoke test: chat.db -> reader -> detector -> calendar -> state.

Unlike test_main.py (which stubs calendar.create_event wholesale), this test
lets the real reader, detector, calendar, and state code run together. Only the
two true external boundaries are stubbed: the Anthropic client (fake_anthropic)
and the osascript subprocess (so no real Calendar event is created).
"""

import time
from types import SimpleNamespace

import pytest

from scheduling_agent import calendar, main, reader, state


@pytest.fixture
def spy_osascript(monkeypatch):
    """Replace the osascript subprocess boundary, recording each invocation and
    returning a successful result."""
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(calendar.subprocess, "run", fake_run)
    return calls


def _cfg(**overrides):
    cfg = {
        "lookback_days": 7,
        "blocked_contacts": [],
        "confidence_threshold": 0.85,
        "tentative_confidence_threshold": 0.6,
        "target_calendar": "Work",
    }
    cfg.update(overrides)
    return cfg


def _event():
    return {
        "has_event": True,
        "status": "confirmed",
        "title": "Dinner at Lucia's",
        "date": "2099-01-15",
        "time_start": "19:00",
        "duration_minutes": 90,
        "location": "Lucia's",
        "confidence": 0.95,
        "recurrence": None,
        "end_date": None,
    }


def test_full_pipeline_creates_event_then_is_idempotent(
    fake_chat_db, fake_anthropic, spy_osascript, monkeypatch
):
    newest = time.time() - 3600
    fake_chat_db([
        {
            "participants": ["+15551234567"],
            "messages": [
                {"text": "dinner friday at lucia's?", "from_me": False,
                 "unix_ts": time.time() - 7200},
                {"text": "yes! 7pm", "from_me": True, "unix_ts": newest},
            ],
        }
    ])
    # Same event payload is returned for every detector call.
    fake_anthropic([_event()])

    # --- First run: detect + create through the real calendar path ---
    main.process_new_messages(_cfg())

    assert len(spy_osascript) == 1
    script = spy_osascript[0][2]  # ["osascript", "-e", <script>]
    assert "Dinner at Lucia's" in script
    assert 'name is "Work"' in script
    assert "Lucia's" in script  # location made it into the AppleScript

    # State recorded the dedup hash and advanced the timestamp.
    assert state.is_duplicate(1, "2099-01-15", "19:00", "Dinner at Lucia's") is True
    assert state.get_last_timestamp() == reader.unix_to_apple(newest)

    # --- Second run: same thread reappears, but dedup must suppress it ---
    # Rescan the same window by pretending no timestamp checkpoint exists, so the
    # idempotency comes from the dedup guard rather than the timestamp shortcut.
    monkeypatch.setattr(state, "get_last_timestamp", lambda: None)
    main.process_new_messages(_cfg())

    # No second osascript call — the event was not created twice.
    assert len(spy_osascript) == 1

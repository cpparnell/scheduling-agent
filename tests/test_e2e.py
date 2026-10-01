"""End-to-end smoke test: chat.db -> reader -> detector -> calendar -> state.

Unlike test_main.py (which stubs calendar.create_event wholesale), this test
lets the real reader, detector, calendar, and state code run together. Only the
two true external boundaries are stubbed: the Anthropic client (fake_anthropic)
and the osascript subprocess (so no real Calendar event is created).
"""

import time
from types import SimpleNamespace

import pytest

from scheduling_agent import calendar, config, main, reader, state


@pytest.fixture
def spy_osascript(monkeypatch):
    """Replace the osascript subprocess boundary, recording each invocation and
    returning a successful result."""
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="FAKE-EVENT-UID", stderr="")

    monkeypatch.setattr(calendar.subprocess, "run", fake_run)
    return calls


def _cfg(**overrides):
    cfg = {
        **config.DEFAULTS,
        "target_calendar": "Work",
        "dedup_enabled": False,
        "calendar_query_enabled": False,
        # These tests script the Claude detector via fake_anthropic; the Jev
        # path has its own tests (test_jev_detector.py).
        "detector_backend": "claude",
    }
    cfg.update(overrides)
    return cfg


def _event():
    return {
        "events": [{
            "status": "confirmed",
            "title": "Dinner at Lucia's",
            "date": "2099-01-15",
            "time_start": "19:00",
            "time_confidence": 0.95,
            "duration_minutes": 90,
            "location": "Lucia's",
            "confidence": 0.95,
            "user_is_participant": True,
            "participation_evidence": "Me accepted the invitation",
            "recurrence": None,
            "end_date": None,
            "evidence": "yes! 7pm",
            "date_evidence": "dinner friday at lucia's?",
        }]
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


def test_default_jev_backend_creates_event_without_calling_claude(
    fake_chat_db, spy_osascript, monkeypatch
):
    """The default config (detector_backend="jev") end to end: a confident
    Jev answer creates the event on the fast path with no Claude call."""
    from datetime import date, timedelta
    from types import SimpleNamespace

    from scheduling_agent import detector, jev_client

    day = date.today() + timedelta(days=10)
    fake_chat_db([
        {
            "participants": ["+15551234567"],
            "messages": [
                {"text": f"dinner {day:%B} {day.day} at 7pm?", "from_me": False,
                 "unix_ts": time.time() - 7200},
                {"text": "yes!", "from_me": True, "unix_ts": time.time() - 3600},
            ],
        }
    ])

    def confident(state, questions):
        # Every choice picks its first offered option (status lists
        # "confirmed" first); every yes/no is decisive in the plan's favor.
        nouls = {"has_plan": 0.97, "multiple_plans": 0.02, "participant": 0.98, "new_info": 0.9}
        return SimpleNamespace(
            nouls={k: SimpleNamespace(noul=v) for k, v in nouls.items() if k in questions},
            choices={
                k: SimpleNamespace(choice=next(iter(q.criteria)), confidence=0.95)
                for k, q in questions.items() if hasattr(q, "criteria") and q.type == "choice"
            },
        )

    def no_claude(*args, **kwargs):
        raise AssertionError("fast path should not call the Claude detector")

    monkeypatch.setattr(jev_client, "ask", confident)
    monkeypatch.setattr(detector, "_detect_plans_claude", no_claude)

    cfg = {**_cfg(), "detector_backend": config.DEFAULTS["detector_backend"]}
    assert cfg["detector_backend"] == "jev"
    main.process_new_messages(cfg)

    assert len(spy_osascript) == 1
    script = spy_osascript[0][2]
    assert "Dinner" in script
    assert state.is_duplicate(1, day.isoformat(), "19:00", "Dinner") is True

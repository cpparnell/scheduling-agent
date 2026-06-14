import time

import pytest

from scheduling_agent import calendar, main, reader, state


def _cfg(**overrides):
    cfg = {
        "lookback_days": 7,
        "blocked_contacts": [],
        "confidence_threshold": 0.85,
        "target_calendar": "Work",
    }
    cfg.update(overrides)
    return cfg


def _event(**overrides):
    base = {
        "has_event": True,
        "status": "confirmed",
        "title": "Dinner",
        "date": "2026-06-13",
        "time_start": "19:00",
        "duration_minutes": 60,
        "location": None,
        "confidence": 0.95,
    }
    base.update(overrides)
    return base


@pytest.fixture
def spy_create_event(monkeypatch):
    """Replace calendar.create_event with a spy returning a configurable bool."""
    state_ = {"calls": [], "return_value": True}

    def fake(**kwargs):
        state_["calls"].append(kwargs)
        return state_["return_value"]

    monkeypatch.setattr(calendar, "create_event", fake)
    return state_


def _tentative_cfg(**overrides):
    cfg = _cfg(**overrides)
    cfg["tentative_confidence_threshold"] = 0.6
    return cfg


@pytest.fixture
def one_chat_db(fake_chat_db):
    """A single chat (chat_id == 1) with one recent message; returns the newest
    message's stored apple timestamp for assertions."""
    newest = time.time() - 3600
    fake_chat_db([
        {
            "participants": ["+15551234567"],
            "messages": [
                {"text": "dinner friday?", "from_me": False, "unix_ts": time.time() - 7200},
                {"text": "yes 7pm", "from_me": True, "unix_ts": newest},
            ],
        }
    ])
    return reader.unix_to_apple(newest)


def test_happy_path_creates_event_records_and_advances_timestamp(
    one_chat_db, fake_anthropic, spy_create_event
):
    newest_apple = one_chat_db
    fake_anthropic([_event()])

    main.process_new_messages(_cfg())

    assert len(spy_create_event["calls"]) == 1
    call = spy_create_event["calls"][0]
    assert call["title"] == "Dinner"
    assert call["calendar_name"] == "Work"
    # Dedup hash recorded (chat_id 1 from the fixture).
    assert state.is_duplicate(1, "2026-06-13", "19:00", "Dinner") is True
    # Timestamp advanced to the newest message seen.
    assert state.get_last_timestamp() == newest_apple


def test_low_confidence_event_is_skipped(one_chat_db, fake_anthropic, spy_create_event):
    fake_anthropic([_event(confidence=0.5)])

    main.process_new_messages(_cfg(confidence_threshold=0.85))

    assert spy_create_event["calls"] == []
    assert state.is_duplicate(1, "2026-06-13", "19:00", "Dinner") is False


def test_duplicate_event_is_skipped(one_chat_db, fake_anthropic, spy_create_event):
    # Pre-seed the dedup hash for chat 1.
    state.record_event(1, "2026-06-13", "19:00", "Dinner")
    fake_anthropic([_event()])

    main.process_new_messages(_cfg())

    assert spy_create_event["calls"] == []


def test_failed_create_does_not_record_hash(one_chat_db, fake_anthropic, spy_create_event):
    spy_create_event["return_value"] = False
    fake_anthropic([_event()])

    main.process_new_messages(_cfg())

    assert len(spy_create_event["calls"]) == 1
    # Not recorded -> will be retried on the next run.
    assert state.is_duplicate(1, "2026-06-13", "19:00", "Dinner") is False


def test_same_time_different_title_is_duplicate(
    one_chat_db, fake_anthropic, spy_create_event
):
    # Simulate the first event already having been created (e.g. from a prior run).
    state.record_event(1, "2026-06-13", "17:30", "Pizza at Dicey's")

    # The detector now returns a differently-described event at the exact same time.
    fake_anthropic([_event(title="Drinks", time_start="17:30")])

    main.process_new_messages(_cfg())

    # Must be suppressed — same chat/date/time is the same real-world event.
    assert spy_create_event["calls"] == []


def test_no_threads_does_nothing(fake_chat_db, fake_anthropic, spy_create_event):
    fake_chat_db([])  # empty db, no messages

    main.process_new_messages(_cfg())

    assert spy_create_event["calls"] == []
    assert state.get_last_timestamp() is None


def test_tentative_event_created_for_unanswered_invite(
    one_chat_db, fake_anthropic, spy_create_event
):
    fake_anthropic([_event(status="tentative", confidence=0.7)])

    main.process_new_messages(_tentative_cfg())

    assert len(spy_create_event["calls"]) == 1
    assert spy_create_event["calls"][0]["tentative"] is True
    assert state.is_duplicate(1, "2026-06-13", "19:00", "Dinner") is True


def test_tentative_below_threshold_is_skipped(
    one_chat_db, fake_anthropic, spy_create_event
):
    fake_anthropic([_event(status="tentative", confidence=0.4)])

    main.process_new_messages(_tentative_cfg())

    assert spy_create_event["calls"] == []
    assert state.is_duplicate(1, "2026-06-13", "19:00", "Dinner") is False

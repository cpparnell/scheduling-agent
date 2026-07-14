import time

import pytest

from scheduling_agent import calendar, config, main, reader, state


def _cfg(**overrides):
    cfg = {
        "lookback_days": 7,
        "blocked_contacts": [],
        "confidence_threshold": 0.85,
        "target_calendar": "Work",
        "time_confidence_threshold": config.DEFAULTS["time_confidence_threshold"],
        "dedup_enabled": False,
        "dedup_model": config.DEFAULTS["dedup_model"],
        "dedup_day_window": config.DEFAULTS["dedup_day_window"],
        "dedup_fail_open": config.DEFAULTS["dedup_fail_open"],
        "max_watermark_retries": config.DEFAULTS["max_watermark_retries"],
    }
    cfg.update(overrides)
    return cfg


FUTURE_DATE = "2099-01-15"


def _event(**overrides):
    base = {
        "status": "confirmed",
        "title": "Dinner",
        "date": FUTURE_DATE,
        "time_start": "19:00",
        "time_confidence": 0.95,
        "duration_minutes": 60,
        "location": None,
        "confidence": 0.95,
        "recurrence": None,
        "end_date": None,
        "evidence": "yes 7pm",
    }
    base.update(overrides)
    return base


def _response(*events):
    return {"events": list(events)}


@pytest.fixture
def spy_create_event(monkeypatch):
    """Replace calendar.create_event with a spy returning a configurable UID
    (or None to simulate a failed creation)."""
    state_ = {"calls": [], "return_value": "FAKE-UID"}

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
    fake_anthropic([_response(_event())])

    main.process_new_messages(_cfg())

    assert len(spy_create_event["calls"]) == 1
    call = spy_create_event["calls"][0]
    assert call["title"] == "Dinner"
    assert call["calendar_name"] == "Work"
    # Dedup hash recorded (chat_id 1 from the fixture).
    assert state.is_duplicate(1, FUTURE_DATE, "19:00", "Dinner") is True
    # Timestamp advanced to the newest message seen.
    assert state.get_last_timestamp() == newest_apple


def test_low_confidence_event_is_skipped(one_chat_db, fake_anthropic, spy_create_event):
    fake_anthropic([_response(_event(confidence=0.5))])

    main.process_new_messages(_cfg(confidence_threshold=0.85))

    assert spy_create_event["calls"] == []
    assert state.is_duplicate(1, FUTURE_DATE, "19:00", "Dinner") is False


def test_duplicate_event_is_skipped(one_chat_db, fake_anthropic, spy_create_event):
    # Pre-seed the dedup hash for chat 1.
    state.record_event(1, FUTURE_DATE, "19:00", "Dinner")
    fake_anthropic([_response(_event())])

    main.process_new_messages(_cfg())

    assert spy_create_event["calls"] == []


def test_failed_create_does_not_record_hash(one_chat_db, fake_anthropic, spy_create_event):
    spy_create_event["return_value"] = None
    fake_anthropic([_response(_event())])

    main.process_new_messages(_cfg())

    assert len(spy_create_event["calls"]) == 1
    # Not recorded -> will be retried on the next run.
    assert state.is_duplicate(1, FUTURE_DATE, "19:00", "Dinner") is False


def test_same_time_different_title_is_duplicate(
    one_chat_db, fake_anthropic, spy_create_event
):
    # Simulate the first event already having been created (e.g. from a prior run).
    state.record_event(1, FUTURE_DATE, "17:30", "Pizza at Dicey's")

    # The detector now returns a differently-described event at the exact same time.
    fake_anthropic([_response(_event(title="Drinks", time_start="17:30"))])

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
    fake_anthropic([_response(_event(status="tentative", confidence=0.7))])

    main.process_new_messages(_tentative_cfg())

    assert len(spy_create_event["calls"]) == 1
    assert spy_create_event["calls"][0]["tentative"] is True
    assert state.is_duplicate(1, FUTURE_DATE, "19:00", "Dinner") is True


def test_tentative_below_threshold_is_skipped(
    one_chat_db, fake_anthropic, spy_create_event
):
    fake_anthropic([_response(_event(status="tentative", confidence=0.4))])

    main.process_new_messages(_tentative_cfg())

    assert spy_create_event["calls"] == []


def test_past_event_is_skipped(one_chat_db, fake_anthropic, spy_create_event):
    fake_anthropic([_response(_event(date="2020-01-01"))])

    main.process_new_messages(_cfg())

    assert spy_create_event["calls"] == []
    assert state.is_duplicate(1, "2020-01-01", "19:00", "Dinner") is False


def test_today_event_is_not_skipped(one_chat_db, fake_anthropic, spy_create_event):
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    fake_anthropic([_response(_event(date=today))])

    main.process_new_messages(_cfg())

    assert len(spy_create_event["calls"]) == 1
    assert state.is_duplicate(1, FUTURE_DATE, "19:00", "Dinner") is False


def test_low_time_confidence_demotes_to_allday(one_chat_db, fake_anthropic, spy_create_event):
    fake_anthropic([_response(_event(time_confidence=0.5))])

    main.process_new_messages(_cfg(time_confidence_threshold=0.9))

    assert len(spy_create_event["calls"]) == 1
    assert spy_create_event["calls"][0]["time_start"] is None


def test_high_time_confidence_keeps_specific_time(one_chat_db, fake_anthropic, spy_create_event):
    fake_anthropic([_response(_event(time_confidence=0.95))])

    main.process_new_messages(_cfg(time_confidence_threshold=0.9))

    assert len(spy_create_event["calls"]) == 1
    assert spy_create_event["calls"][0]["time_start"] == "19:00"


def test_null_time_start_is_unaffected_by_time_confidence_gate(
    one_chat_db, fake_anthropic, spy_create_event
):
    fake_anthropic([_response(_event(time_start=None, time_confidence=None))])

    main.process_new_messages(_cfg(time_confidence_threshold=0.9))

    assert len(spy_create_event["calls"]) == 1
    assert spy_create_event["calls"][0]["time_start"] is None


def test_created_event_records_calendar_uid(one_chat_db, fake_anthropic, spy_create_event):
    spy_create_event["return_value"] = "REAL-UID-99"
    fake_anthropic([_response(_event())])

    main.process_new_messages(_cfg())

    events = state._load()["events"]
    assert len(events) == 1
    assert events[0]["calendar_uid"] == "REAL-UID-99"


def test_two_events_from_one_thread_both_created(one_chat_db, fake_anthropic, spy_create_event):
    fake_anthropic([_response(
        _event(title="Dinner", date=FUTURE_DATE),
        _event(title="The Game", date="2099-01-16", evidence="game tomorrow?"),
    )])

    main.process_new_messages(_cfg())

    assert len(spy_create_event["calls"]) == 2
    titles = {c["title"] for c in spy_create_event["calls"]}
    assert titles == {"Dinner", "The Game"}


def test_dedup_adjudicator_duplicate_suppresses_creation(
    one_chat_db, fake_anthropic, fake_dedup_anthropic, spy_create_event
):
    # A pre-existing event on a nearby day, described differently.
    state.record_event(1, "2099-01-14", "19:00", "Dinner with Sam", status="confirmed")
    fake_anthropic([_response(_event(title="Dinner w/ Samantha"))])
    fake_dedup_anthropic([{"is_duplicate": True, "duplicate_of": 0, "reasoning": "same plan reworded"}])

    main.process_new_messages(_cfg(dedup_enabled=True))

    assert spy_create_event["calls"] == []
    events = state._load()["events"]
    suppressed = [e for e in events if e["title"] == "Dinner w/ Samantha"]
    assert len(suppressed) == 1
    assert suppressed[0]["suppressed"] is True


def test_dedup_adjudicator_records_duplicate_of_uid(
    one_chat_db, fake_anthropic, fake_dedup_anthropic, spy_create_event
):
    state.record_event(
        1, "2099-01-14", "19:00", "Dinner with Sam",
        status="confirmed", calendar_uid="uid-123",
    )
    fake_anthropic([_response(_event(title="Dinner w/ Samantha"))])
    fake_dedup_anthropic([{"is_duplicate": True, "duplicate_of": 0, "reasoning": "same plan reworded"}])

    main.process_new_messages(_cfg(dedup_enabled=True))

    events = state._load()["events"]
    suppressed = [e for e in events if e["title"] == "Dinner w/ Samantha"]
    assert len(suppressed) == 1
    assert suppressed[0]["duplicate_of_uid"] == "uid-123"


def test_dedup_adjudicator_out_of_range_duplicate_of_still_suppresses(
    one_chat_db, fake_anthropic, fake_dedup_anthropic, spy_create_event, caplog
):
    state.record_event(
        1, "2099-01-14", "19:00", "Dinner with Sam",
        status="confirmed", calendar_uid="uid-123",
    )
    fake_anthropic([_response(_event(title="Dinner w/ Samantha"))])
    fake_dedup_anthropic([{"is_duplicate": True, "duplicate_of": 7, "reasoning": "same plan reworded"}])

    with caplog.at_level("WARNING"):
        main.process_new_messages(_cfg(dedup_enabled=True))

    assert spy_create_event["calls"] == []
    events = state._load()["events"]
    suppressed = [e for e in events if e["title"] == "Dinner w/ Samantha"]
    assert len(suppressed) == 1
    assert suppressed[0]["duplicate_of_uid"] is None
    assert "out-of-range duplicate_of" in caplog.text


def test_dedup_adjudicator_not_duplicate_creates_event(
    one_chat_db, fake_anthropic, fake_dedup_anthropic, spy_create_event
):
    state.record_event(1, "2099-01-14", "12:00", "Work Call", status="confirmed")
    fake_anthropic([_response(_event())])
    fake_dedup_anthropic([{"is_duplicate": False, "duplicate_of": None, "reasoning": "different activity"}])

    main.process_new_messages(_cfg(dedup_enabled=True))

    assert len(spy_create_event["calls"]) == 1


def test_dedup_adjudicator_error_fails_open_creates_event(
    one_chat_db, fake_anthropic, fake_dedup_anthropic, spy_create_event
):
    state.record_event(1, "2099-01-14", "12:00", "Work Call", status="confirmed")
    fake_anthropic([_response(_event())])
    fake_dedup_anthropic(["not json"])

    main.process_new_messages(_cfg(dedup_enabled=True, dedup_fail_open=True))

    assert len(spy_create_event["calls"]) == 1


def test_dedup_adjudicator_error_fails_closed_skips_event(
    one_chat_db, fake_anthropic, fake_dedup_anthropic, spy_create_event
):
    state.record_event(1, "2099-01-14", "12:00", "Work Call", status="confirmed")
    fake_anthropic([_response(_event())])
    fake_dedup_anthropic(["not json"])

    main.process_new_messages(_cfg(dedup_enabled=True, dedup_fail_open=False))

    assert spy_create_event["calls"] == []


def test_dedup_disabled_bypasses_adjudicator_entirely(
    one_chat_db, fake_anthropic, fake_dedup_anthropic, spy_create_event
):
    state.record_event(1, "2099-01-14", "19:00", "Dinner with Sam", status="confirmed")
    fake_anthropic([_response(_event(title="Dinner w/ Samantha"))])
    client = fake_dedup_anthropic([{"is_duplicate": True, "duplicate_of": 0, "reasoning": "n/a"}])

    main.process_new_messages(_cfg(dedup_enabled=False))

    assert len(spy_create_event["calls"]) == 1
    assert client.messages.calls == []


def test_dedup_no_nearby_candidates_never_calls_adjudicator(
    one_chat_db, fake_anthropic, fake_dedup_anthropic, spy_create_event
):
    # Existing event is far outside the dedup window.
    state.record_event(1, "2099-02-14", "19:00", "Unrelated Dinner", status="confirmed")
    fake_anthropic([_response(_event())])
    client = fake_dedup_anthropic([{"is_duplicate": True, "duplicate_of": 0, "reasoning": "n/a"}])

    main.process_new_messages(_cfg(dedup_enabled=True))

    assert len(spy_create_event["calls"]) == 1
    assert client.messages.calls == []


def test_watermark_held_when_thread_fails_then_advanced_after_retries(
    one_chat_db, fake_anthropic, spy_create_event
):
    newest_apple = one_chat_db
    fake_anthropic([RuntimeError("boom")])
    cfg = _cfg(max_watermark_retries=3)

    main.process_new_messages(cfg)
    assert state.get_last_timestamp() is None
    assert state.get_watermark_hold()["count"] == 1

    main.process_new_messages(cfg)
    assert state.get_last_timestamp() is None
    assert state.get_watermark_hold()["count"] == 2

    # Third consecutive failure hits the retry cap -> advance anyway.
    main.process_new_messages(cfg)
    assert state.get_last_timestamp() == newest_apple
    assert state.get_watermark_hold() == {"ts": None, "count": 0}

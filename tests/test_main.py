import json
import threading
import time
from datetime import datetime, timedelta

import pytest

from scheduling_agent import calendar, config, main, reader, state


def _cfg(**overrides):
    cfg = {
        **config.DEFAULTS,
        "target_calendar": "Work",
        "dedup_enabled": False,
        "calendar_query_enabled": False,
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
        "user_is_participant": True,
        "participation_evidence": "Me accepted the invitation",
        "recurrence": None,
        "end_date": None,
        "evidence": "yes 7pm",
        "date_evidence": "yes 7pm",
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


@pytest.fixture
def spy_update_event(monkeypatch):
    """Replace calendar.update_event with a spy returning a configurable bool."""
    state_ = {"calls": [], "return_value": True}

    def fake(uid, **kwargs):
        state_["calls"].append({"uid": uid, **kwargs})
        return state_["return_value"]

    monkeypatch.setattr(calendar, "update_event", fake)
    return state_


@pytest.fixture
def spy_delete_event(monkeypatch):
    """Replace calendar.delete_event with a spy returning a configurable bool."""
    state_ = {"calls": [], "return_value": True}

    def fake(uid, **kwargs):
        state_["calls"].append({"uid": uid, **kwargs})
        return state_["return_value"]

    monkeypatch.setattr(calendar, "delete_event", fake)
    return state_


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


def test_reworded_title_same_time_is_duplicate(
    one_chat_db, fake_anthropic, spy_create_event
):
    """Title normalization is a sorted token set (F2a), so a verb-order
    reshuffle of the same title still hits the exact-hash layer."""
    state.record_event(1, FUTURE_DATE, "19:00", "Dinner with Sam")

    fake_anthropic([_response(_event(title="Sam with Dinner", time_start="19:00"))])

    main.process_new_messages(_cfg())

    assert spy_create_event["calls"] == []


def test_same_time_different_title_is_not_merged_without_dedup(
    one_chat_db, fake_anthropic, spy_create_event
):
    """Same chat/date/time alone is not enough to call two events the same
    plan — event_hash keys on date + normalized title (F2b), not time_start,
    and with dedup disabled there's no fuzzy/LLM layer to catch a genuine
    coincidence (two different plans discussed in one chat landing on the
    same slot). Over-merge guard: this must create both."""
    state.record_event(1, FUTURE_DATE, "17:30", "Pizza at Dicey's")

    fake_anthropic([_response(_event(title="Drinks", time_start="17:30"))])

    main.process_new_messages(_cfg())

    assert len(spy_create_event["calls"]) == 1


def test_no_threads_does_nothing(fake_chat_db, fake_anthropic, spy_create_event):
    fake_chat_db([])  # empty db, no messages

    main.process_new_messages(_cfg())

    assert spy_create_event["calls"] == []
    assert state.get_last_timestamp() is None


def test_tentative_event_created_when_user_hedged(
    one_chat_db, fake_anthropic, spy_create_event
):
    # Tentative is a high-confidence classification (the user said "maybe"),
    # judged against the single confidence threshold.
    fake_anthropic([_response(_event(status="tentative", confidence=0.95))])

    main.process_new_messages(_cfg())

    assert len(spy_create_event["calls"]) == 1
    assert spy_create_event["calls"][0]["tentative"] is True
    assert state.is_duplicate(1, FUTURE_DATE, "19:00", "Dinner") is True


def test_tentative_uses_single_confidence_threshold(
    one_chat_db, fake_anthropic, spy_create_event
):
    # The old 0.6 tentative threshold is gone: a 0.7 tentative event is now
    # below the one 0.85 bar and must be skipped.
    fake_anthropic([_response(_event(status="tentative", confidence=0.7))])

    main.process_new_messages(_cfg())

    assert spy_create_event["calls"] == []


def test_unanswered_invitation_is_skipped_and_not_recorded(
    one_chat_db, fake_anthropic, spy_create_event
):
    fake_anthropic([_response(_event(status="unanswered", confidence=0.95))])

    main.process_new_messages(_cfg())

    assert spy_create_event["calls"] == []
    # Not recorded: a later acceptance must be able to create it.
    assert state.is_duplicate(1, FUTURE_DATE, "19:00", "Dinner") is False


def test_non_participant_event_is_skipped_silently(
    one_chat_db, fake_anthropic, spy_create_event, caplog
):
    fake_anthropic([_response(_event(
        user_is_participant=False,
        participation_evidence="The friend is going to the lake house, not the user",
    ))])

    with caplog.at_level("INFO"):
        main.process_new_messages(_cfg())

    assert spy_create_event["calls"] == []
    assert state.is_duplicate(1, FUTURE_DATE, "19:00", "Dinner") is False
    assert "Skipping non-participant plan" in caplog.text


def test_non_participant_gate_beats_confirmed_status(
    one_chat_db, fake_anthropic, spy_create_event
):
    # Even a fully confirmed, high-confidence plan is skipped when the user
    # isn't part of it.
    fake_anthropic([_response(_event(
        status="confirmed", confidence=1.0, user_is_participant=False,
    ))])

    main.process_new_messages(_cfg())

    assert spy_create_event["calls"] == []


# --- F7: observations + anti-flap guard -------------------------------------


def test_unanswered_skip_records_an_observation():
    result = main.process_event(_event(chat_id=1, status="unanswered"), _cfg())

    assert result == "skipped:unanswered"
    obs = state.get_observation(1, FUTURE_DATE, "19:00", "Dinner")
    assert obs is not None
    assert obs["last_status"] == "unanswered"
    assert obs["count"] == 1


def test_not_participant_skip_records_an_observation():
    event = _event(
        chat_id=1, user_is_participant=False, participation_evidence="not the user's plan"
    )
    result = main.process_event(event, _cfg())

    assert result == "skipped:not-participant"
    obs = state.get_observation(1, FUTURE_DATE, "19:00", "Dinner")
    assert obs is not None
    assert obs["last_status"] == "not-participant"


def test_low_confidence_skip_records_an_observation():
    event = _event(chat_id=1, confidence=0.5)
    result = main.process_event(event, _cfg(confidence_threshold=0.85))

    assert result == "skipped:low-confidence"
    obs = state.get_observation(1, FUTURE_DATE, "19:00", "Dinner")
    assert obs is not None
    assert obs["last_status"] == "low-confidence"


def test_observation_count_increments_across_repeated_skips():
    main.process_event(_event(chat_id=1, status="unanswered"), _cfg())
    main.process_event(_event(chat_id=1, status="unanswered"), _cfg())
    main.process_event(_event(chat_id=1, status="unanswered"), _cfg())

    obs = state.get_observation(1, FUTURE_DATE, "19:00", "Dinner")
    assert obs["count"] == 3


def test_anti_flap_reverts_confirmed_to_unanswered_with_no_new_user_message(
    spy_create_event,
):
    # First poll: unanswered, correctly skipped and observed.
    main.process_event(_event(chat_id=1, status="unanswered"), _cfg())

    # A later poll flips to confirmed but the event carries no new message
    # from the user (stale context re-analysis, not a real acceptance).
    flipped = _event(chat_id=1, status="confirmed", **{"_new_msg_from_user": False})
    result = main.process_event(flipped, _cfg())

    assert result == "skipped:unanswered"
    assert spy_create_event["calls"] == []


def test_anti_flap_does_not_block_a_genuine_new_acceptance(spy_create_event):
    main.process_event(_event(chat_id=1, status="unanswered"), _cfg())

    # This time the flip DOES carry a new message from the user — a real
    # acceptance — and must be allowed through normally.
    accepted = _event(chat_id=1, status="confirmed", **{"_new_msg_from_user": True})
    result = main.process_event(accepted, _cfg())

    assert result == "created"
    assert len(spy_create_event["calls"]) == 1


def test_anti_flap_does_not_trigger_without_prior_unanswered_observation(
    spy_create_event,
):
    # No observation history at all (e.g. state was purged, or this is the
    # first time this hash has ever been seen) — confirmed with no new user
    # message still creates normally; the guard only fires against a known
    # prior "unanswered" observation, it isn't a general-purpose block.
    event = _event(chat_id=1, status="confirmed", **{"_new_msg_from_user": False})
    result = main.process_event(event, _cfg())

    assert result == "created"
    assert len(spy_create_event["calls"]) == 1


def test_new_msg_from_user_missing_defaults_to_true_preserving_old_behavior(
    spy_create_event,
):
    # An event dict with no "_new_msg_from_user" key at all (e.g. constructed
    # by code that predates F7) must not be treated as guard-triggering.
    main.process_event(_event(chat_id=1, status="unanswered"), _cfg())
    event = _event(chat_id=1, status="confirmed")
    assert "_new_msg_from_user" not in event

    result = main.process_event(event, _cfg())

    assert result == "created"


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
        _event(title="The Game", date="2099-01-16", evidence="dinner friday?"),
    )])

    main.process_new_messages(_cfg())

    assert len(spy_create_event["calls"]) == 2
    titles = {c["title"] for c in spy_create_event["calls"]}
    assert titles == {"Dinner", "The Game"}


def test_dedup_adjudicator_duplicate_suppresses_creation(
    one_chat_db, fake_anthropic, fake_dedup_anthropic, spy_create_event
):
    # The same plan already recorded from a DIFFERENT chat, described
    # differently — only the LLM layer can match it.
    state.record_event(2, FUTURE_DATE, "19:00", "Dinner with Sam", status="confirmed", confidence=0.95)
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
        2, FUTURE_DATE, "19:00", "Dinner with Sam",
        status="confirmed", confidence=0.95, calendar_uid="uid-123",
    )
    fake_anthropic([_response(_event(title="Dinner w/ Samantha"))])
    fake_dedup_anthropic([{"is_duplicate": True, "duplicate_of": 0, "reasoning": "same plan reworded"}])

    main.process_new_messages(_cfg(dedup_enabled=True))

    events = state._load()["events"]
    suppressed = [e for e in events if e["title"] == "Dinner w/ Samantha"]
    assert len(suppressed) == 1
    assert suppressed[0]["duplicate_of_uid"] == "uid-123"


def test_reconcile_match_with_time_drift_updates_calendar_event(
    one_chat_db, fake_anthropic, spy_create_event, spy_update_event
):
    # Same plan re-detected from another chat at a slightly later time: the
    # existing calendar event moves instead of a duplicate being created.
    state.record_event(
        2, FUTURE_DATE, "19:00", "Dinner",
        status="confirmed", confidence=0.9, calendar_uid="uid-123",
    )
    fake_anthropic([_response(_event(time_start="19:30"))])

    main.process_new_messages(_cfg())

    assert spy_create_event["calls"] == []
    assert len(spy_update_event["calls"]) == 1
    call = spy_update_event["calls"][0]
    assert call["uid"] == "uid-123"
    assert call["time_start"] == "19:30"

    record = next(e for e in state._load()["events"] if e["calendar_uid"] == "uid-123")
    assert record["time_start"] == "19:30"
    assert record["chat_ids"] == [2, 1]
    assert len(record["revisions"]) == 1
    assert state.get_pending_journal() == []


def test_reconcile_update_disabled_treats_update_as_duplicate(
    one_chat_db, fake_anthropic, spy_create_event, spy_update_event
):
    state.record_event(
        2, FUTURE_DATE, "19:00", "Dinner",
        status="confirmed", confidence=0.9, calendar_uid="uid-123",
    )
    fake_anthropic([_response(_event(time_start="19:30"))])

    main.process_new_messages(_cfg(reconcile_update_enabled=False))

    assert spy_create_event["calls"] == []
    assert spy_update_event["calls"] == []
    # Original record untouched; new wording recorded as suppressed.
    record = next(e for e in state._load()["events"] if e["calendar_uid"] == "uid-123")
    assert record["time_start"] == "19:00"
    suppressed = [e for e in state._load()["events"] if e["suppressed"]]
    assert len(suppressed) == 1
    assert suppressed[0]["duplicate_of_uid"] == "uid-123"


# --- cancellation (F4) --------------------------------------------------------


def test_cancelled_event_deletes_agent_owned_calendar_event(
    one_chat_db, fake_anthropic, spy_create_event, spy_delete_event
):
    state.record_event(
        1, FUTURE_DATE, "19:00", "Dinner",
        status="confirmed", confidence=0.9, calendar_uid="uid-123",
    )
    fake_anthropic([_response(_event(status="cancelled"))])

    result = main.process_new_messages(_cfg())

    assert spy_create_event["calls"] == []
    assert spy_delete_event["calls"] == [{"uid": "uid-123", "calendar_name": "Work"}]
    record = next(e for e in state._load()["events"] if e["calendar_uid"] == "uid-123")
    assert record["status"] == "cancelled"
    assert state.get_pending_journal() == []


def test_cancelled_event_with_no_match_is_noop(
    one_chat_db, fake_anthropic, spy_create_event, spy_delete_event
):
    fake_anthropic([_response(_event(status="cancelled"))])

    main.process_new_messages(_cfg())

    assert spy_create_event["calls"] == []
    assert spy_delete_event["calls"] == []


def test_cancelled_event_never_deletes_unowned_calendar_match(
    monkeypatch, one_chat_db, fake_anthropic, spy_create_event, spy_delete_event
):
    monkeypatch.setattr(calendar, "get_events_near", lambda *a, **k: [{
        "title": "Dinner", "date": FUTURE_DATE, "time_start": "19:00",
        "location": None, "calendar_uid": "UID-7", "source": "calendar",
    }])
    fake_anthropic([_response(_event(status="cancelled"))])

    main.process_new_messages(_cfg(calendar_query_enabled=True))

    assert spy_delete_event["calls"] == []


def test_cancellation_disabled_config_skips_delete(
    one_chat_db, fake_anthropic, spy_create_event, spy_delete_event
):
    state.record_event(
        1, FUTURE_DATE, "19:00", "Dinner",
        status="confirmed", confidence=0.9, calendar_uid="uid-123",
    )
    fake_anthropic([_response(_event(status="cancelled"))])

    main.process_new_messages(_cfg(cancellation_enabled=False))

    assert spy_delete_event["calls"] == []
    record = next(e for e in state._load()["events"] if e["calendar_uid"] == "uid-123")
    assert record["status"] == "confirmed"


def test_failed_calendar_delete_rolls_back_journal_and_state(
    one_chat_db, fake_anthropic, spy_create_event, spy_delete_event
):
    spy_delete_event["return_value"] = False
    state.record_event(
        1, FUTURE_DATE, "19:00", "Dinner",
        status="confirmed", confidence=0.9, calendar_uid="uid-123",
    )
    fake_anthropic([_response(_event(status="cancelled"))])

    main.process_new_messages(_cfg())

    record = next(e for e in state._load()["events"] if e["calendar_uid"] == "uid-123")
    assert record["status"] == "confirmed"  # unchanged, will retry next detection
    assert state.get_pending_journal() == []


def test_failed_calendar_update_rolls_back_journal_and_state(
    one_chat_db, fake_anthropic, spy_create_event, spy_update_event
):
    spy_update_event["return_value"] = False
    state.record_event(
        2, FUTURE_DATE, "19:00", "Dinner",
        status="confirmed", confidence=0.9, calendar_uid="uid-123",
    )
    fake_anthropic([_response(_event(time_start="19:30"))])

    main.process_new_messages(_cfg())

    record = next(e for e in state._load()["events"] if e["calendar_uid"] == "uid-123")
    assert record["time_start"] == "19:00"  # unchanged, will retry next detection
    assert state.get_pending_journal() == []


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
    # Existing event is far outside the dedup window AND dissimilar in title,
    # so neither the near layer nor the far-date layer produces candidates.
    state.record_event(1, "2099-02-14", "19:00", "Dentist Checkup", status="confirmed")
    fake_anthropic([_response(_event())])
    client = fake_dedup_anthropic([{"is_duplicate": True, "duplicate_of": 0, "reasoning": "n/a"}])

    main.process_new_messages(_cfg(dedup_enabled=True))

    assert len(spy_create_event["calls"]) == 1
    assert client.messages.calls == []


def test_crash_between_calendar_and_state_write_cannot_duplicate(
    one_chat_db, fake_anthropic, spy_create_event, monkeypatch
):
    fake_anthropic([_response(_event())])
    real_commit = state.journal_commit

    def dying_commit(*args, **kwargs):
        raise RuntimeError("simulated crash before state write")

    monkeypatch.setattr(state, "journal_commit", dying_commit)
    with pytest.raises(RuntimeError):
        main.process_new_messages(_cfg())

    # The calendar write happened but state never recorded it — the classic
    # crash-duplicate window. The intent survives in the journal.
    assert len(spy_create_event["calls"]) == 1
    assert len(state.get_pending_journal()) == 1

    # Restart and reprocess the same messages (the watermark never advanced):
    # the pending journal entry must block a second calendar write.
    monkeypatch.setattr(state, "journal_commit", real_commit)
    main.process_new_messages(_cfg())

    assert len(spy_create_event["calls"]) == 1


def test_recover_journal_commits_when_event_found_on_calendar(monkeypatch):
    record = state.make_record(1, FUTURE_DATE, "19:00", "Dinner", confidence=0.9)
    state.journal_intent(record)
    monkeypatch.setattr(
        calendar, "get_events_near",
        lambda *a, **k: [{"title": "Dinner", "date": FUTURE_DATE, "time_start": "19:00",
                          "location": None, "calendar_uid": "UID-R", "source": "calendar"}],
    )

    main.recover_journal(_cfg(calendar_query_enabled=True))

    assert state.get_pending_journal() == []
    events = state._load()["events"]
    assert len(events) == 1
    assert events[0]["calendar_uid"] == "UID-R"


def test_recover_journal_drops_create_missing_from_calendar(monkeypatch):
    record = state.make_record(1, FUTURE_DATE, "19:00", "Dinner", confidence=0.9)
    state.journal_intent(record)
    monkeypatch.setattr(calendar, "get_events_near", lambda *a, **k: [])

    main.recover_journal(_cfg(calendar_query_enabled=True))

    # The write never happened: entry dropped so re-detection recreates it.
    assert state.get_pending_journal() == []
    assert state._load()["events"] == []
    assert state.is_duplicate(1, FUTURE_DATE, "19:00", "Dinner") is False


def test_recover_journal_commits_without_uid_when_query_disabled():
    record = state.make_record(1, FUTURE_DATE, "19:00", "Dinner", confidence=0.9)
    state.journal_intent(record)

    main.recover_journal(_cfg(calendar_query_enabled=False))

    assert state.get_pending_journal() == []
    events = state._load()["events"]
    assert len(events) == 1
    assert events[0]["calendar_uid"] is None
    # Conservative: the plan stays deduplicated rather than risking a duplicate.
    assert state.is_duplicate(1, FUTURE_DATE, "19:00", "Dinner") is True


def test_recover_journal_drops_interrupted_update():
    state.journal_intent({"canonical_id": "abc", "changes": {"time_start": "20:00"}}, op="update")

    main.recover_journal(_cfg())

    assert state.get_pending_journal() == []


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


class TestRunGate:
    """The watcher (blocking) and poll-fallback timer (skip_if_busy) both
    drive the same run function; a shared _RunGate must ensure they never
    execute it concurrently, since state._save() has no locking of its own."""

    def test_skip_if_busy_does_not_run_while_blocking_holds_the_lock(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def run():
            calls.append("start")
            started.set()
            release.wait(timeout=5)
            calls.append("end")

        gate = main._RunGate(run)
        t = threading.Thread(target=gate.blocking)
        t.start()
        started.wait(timeout=5)

        # A concurrent poll tick must skip rather than run or block.
        gate.skip_if_busy()
        assert calls == ["start"]

        release.set()
        t.join(timeout=5)
        assert calls == ["start", "end"]

    def test_skip_if_busy_runs_when_lock_is_free(self):
        calls = []
        gate = main._RunGate(lambda: calls.append("ran"))

        gate.skip_if_busy()

        assert calls == ["ran"]

    def test_blocking_runs_are_serialized_not_concurrent(self):
        order = []
        lock_held = threading.Lock()

        def run():
            # A non-blocking acquire fails if another `run` is mid-flight,
            # proving the gate never lets two runs overlap.
            assert lock_held.acquire(blocking=False)
            order.append("enter")
            time.sleep(0.05)
            order.append("exit")
            lock_held.release()

        gate = main._RunGate(run)
        threads = [threading.Thread(target=gate.blocking) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert order.count("enter") == 5
        assert order.count("exit") == 5
        # Each enter is immediately followed by its own exit.
        for i in range(0, len(order), 2):
            assert order[i] == "enter"
            assert order[i + 1] == "exit"


class TestParseBackfillSince:
    """--since accepts a non-negative integer number of days ago, or an ISO
    date. A negative integer must be rejected, not silently interpreted as a
    future date (see PR #15 review comment #2)."""

    def test_integer_days_ago(self):
        now = datetime(2026, 6, 15)
        assert main.parse_backfill_since("10", now=now) == now - timedelta(days=10)

    def test_iso_date(self):
        assert main.parse_backfill_since("2026-01-01") == datetime(2026, 1, 1)

    def test_negative_integer_is_rejected(self):
        with pytest.raises(ValueError):
            main.parse_backfill_since("-5")

    def test_garbage_is_rejected(self):
        with pytest.raises(ValueError):
            main.parse_backfill_since("not-a-date")


class TestBackfill:
    """main.backfill() replays history through the real detector/reconcile
    pipeline in dry-run mode — no calendar or state.json writes."""

    def test_creates_decision_log_without_touching_calendar_or_state(
        self, fake_chat_db, fake_anthropic, spy_create_event, tmp_path
    ):
        send_time = time.time() - 10 * 86400
        fake_chat_db([
            {
                "participants": ["+15551234567"],
                "messages": [
                    {"text": "dinner friday?", "from_me": False, "unix_ts": send_time - 3600},
                    {"text": "yes 7pm", "from_me": True, "unix_ts": send_time},
                ],
            }
        ])
        fake_anthropic([_response(_event())])
        out_path = tmp_path / "backfill.jsonl"

        result = main.backfill(
            _cfg(),
            since=datetime.now() - timedelta(days=15),
            until=datetime.now(),
            window_days=30,  # one window covers the whole range
            out_path=out_path,
        )

        # The real calendar.create_event (spied on) was never called — only
        # backfill's own dry-run stub ran.
        assert spy_create_event["calls"] == []
        # Nothing was written to the real (isolated-by-fixture) state file.
        assert not state.STATE_FILE.exists()

        assert result.out_path == out_path
        assert result.n_events == 1
        assert result.n_failed == 0
        lines = [json.loads(l) for l in out_path.read_text().splitlines()]
        assert len(lines) == 1
        assert lines[0]["title"] == "Dinner"
        assert lines[0]["result"] == "created"

    def test_window_end_is_used_as_today_not_the_live_clock(
        self, fake_chat_db, fake_anthropic, spy_create_event, tmp_path
    ):
        # A message from 10 days ago proposing a plan for "that same day" —
        # in the past relative to the live clock, but not relative to the
        # window it was sent in.
        send_dt = datetime.now() - timedelta(days=10)
        fake_chat_db([
            {
                "participants": ["+15551234567"],
                "messages": [
                    {"text": "dinner today?", "from_me": False, "unix_ts": send_dt.timestamp() - 3600},
                    {"text": "yes 7pm", "from_me": True, "unix_ts": send_dt.timestamp()},
                ],
            }
        ])
        fake_anthropic([_response(_event(date=send_dt.strftime("%Y-%m-%d")))])

        main.backfill(
            _cfg(),
            since=send_dt - timedelta(hours=2),
            until=send_dt + timedelta(days=1),
            window_days=1,
            out_path=tmp_path / "backfill.jsonl",
        )

        assert spy_create_event["calls"] == []  # backfill never calls the real one
        assert len(spy_create_event["calls"]) == 0

    def test_empty_history_writes_an_empty_log(self, fake_chat_db, fake_anthropic, tmp_path):
        fake_chat_db([])
        out_path = tmp_path / "backfill.jsonl"

        result = main.backfill(
            _cfg(),
            since=datetime.now() - timedelta(days=5),
            until=datetime.now(),
            out_path=out_path,
        )

        assert result.out_path == out_path
        assert result.n_events == 0
        assert result.n_failed == 0
        assert out_path.read_text() == ""

    def test_failed_detection_call_is_surfaced_not_swallowed(
        self, fake_chat_db, fake_anthropic, tmp_path
    ):
        """A detection call failing (bad API key, exhausted credits, transport
        error) must never look identical to "no plan in this thread" — the
        caller needs to know coverage for that window is incomplete."""
        send_time = time.time() - 10 * 86400
        fake_chat_db([
            {
                "participants": ["+15551234567"],
                "messages": [
                    {"text": "dinner friday?", "from_me": False, "unix_ts": send_time - 3600},
                    {"text": "yes 7pm", "from_me": True, "unix_ts": send_time},
                ],
            }
        ])
        fake_anthropic([RuntimeError("simulated API failure")])
        out_path = tmp_path / "backfill.jsonl"

        result = main.backfill(
            _cfg(),
            since=datetime.now() - timedelta(days=15),
            until=datetime.now(),
            window_days=30,
            out_path=out_path,
        )

        assert result.n_events == 0
        assert result.n_failed == 1
        assert result.first_failed_window is not None
        lines = [json.loads(l) for l in out_path.read_text().splitlines()]
        assert len(lines) == 1
        assert lines[0]["result"] == "detection_failed"
        assert lines[0]["chat_id"] == 1


@pytest.fixture
def fake_mock_calendar(monkeypatch):
    """Stand-in for Calendar.app's calendar-level operations. `calendars`
    maps name -> {"description", "events"}; tweak it before calling the code
    under test."""
    fake = {"calendars": {}, "calls": [], "fail": set()}

    def get_info(name):
        if "info" in fake["fail"]:
            return None
        matches = [c for n, c in fake["calendars"].items() if n == name]
        if not matches:
            return {"count": 0, "description": ""}
        return {"count": len(matches), "description": matches[0]["description"]}

    def create(name):
        fake["calls"].append(("create", name))
        if "create" in fake["fail"]:
            return False
        fake["calendars"][name] = {"description": calendar.MOCK_CALENDAR_MARKER, "events": 0}
        return True

    def count(name):
        return fake["calendars"][name]["events"]

    def clear(name):
        fake["calls"].append(("clear", name))
        fake["calendars"][name]["events"] = 0
        return True

    def delete(name):
        fake["calls"].append(("delete", name))
        del fake["calendars"][name]
        return True

    monkeypatch.setattr(calendar, "get_calendar_info", get_info)
    monkeypatch.setattr(calendar, "create_mock_calendar", create)
    monkeypatch.setattr(calendar, "count_events", count)
    monkeypatch.setattr(calendar, "clear_calendar", clear)
    monkeypatch.setattr(calendar, "delete_calendar", delete)
    monkeypatch.setattr(calendar, "get_events_near", lambda *a, **k: [])
    return fake


class TestPrepareMockCalendar:
    """--backfill --calendar writes to a real calendar, so every path into
    it must refuse anything that could be a calendar the user cares about."""

    def test_creates_missing_calendar(self, fake_mock_calendar):
        main.prepare_mock_calendar("SA test", _cfg())
        assert fake_mock_calendar["calls"] == [("create", "SA test")]

    def test_refuses_real_target_calendar_case_insensitively(self, fake_mock_calendar):
        with pytest.raises(main.MockCalendarError, match="target_calendar"):
            main.prepare_mock_calendar(" work ", _cfg(target_calendar="Work"))
        assert fake_mock_calendar["calls"] == []

    def test_refuses_empty_name(self, fake_mock_calendar):
        with pytest.raises(main.MockCalendarError):
            main.prepare_mock_calendar("  ", _cfg())

    def test_refuses_existing_calendar_without_marker(self, fake_mock_calendar):
        fake_mock_calendar["calendars"]["Family"] = {"description": "", "events": 12}
        with pytest.raises(main.MockCalendarError, match="wasn't created by scheduling-agent"):
            main.prepare_mock_calendar("Family", _cfg(), clear=True)
        assert fake_mock_calendar["calls"] == []

    def test_existing_empty_mock_is_reused(self, fake_mock_calendar):
        fake_mock_calendar["calendars"]["SA test"] = {
            "description": calendar.MOCK_CALENDAR_MARKER, "events": 0,
        }
        main.prepare_mock_calendar("SA test", _cfg())
        assert fake_mock_calendar["calls"] == []

    def test_non_empty_mock_requires_clear(self, fake_mock_calendar):
        fake_mock_calendar["calendars"]["SA test"] = {
            "description": calendar.MOCK_CALENDAR_MARKER, "events": 5,
        }
        with pytest.raises(main.MockCalendarError, match="--clear-calendar"):
            main.prepare_mock_calendar("SA test", _cfg())
        assert fake_mock_calendar["calls"] == []

    def test_non_empty_mock_is_cleared_when_asked(self, fake_mock_calendar):
        fake_mock_calendar["calendars"]["SA test"] = {
            "description": calendar.MOCK_CALENDAR_MARKER, "events": 5,
        }
        main.prepare_mock_calendar("SA test", _cfg(), clear=True)
        assert fake_mock_calendar["calls"] == [("clear", "SA test")]

    def test_calendar_query_failure_is_an_error(self, fake_mock_calendar):
        fake_mock_calendar["fail"].add("info")
        with pytest.raises(main.MockCalendarError, match="couldn't query"):
            main.prepare_mock_calendar("SA test", _cfg())

    def test_create_failure_is_an_error(self, fake_mock_calendar):
        fake_mock_calendar["fail"].add("create")
        with pytest.raises(main.MockCalendarError, match="couldn't create"):
            main.prepare_mock_calendar("SA test", _cfg())


class TestDeleteMockCalendar:
    def test_deletes_tagged_mock(self, fake_mock_calendar):
        fake_mock_calendar["calendars"]["SA test"] = {
            "description": calendar.MOCK_CALENDAR_MARKER, "events": 3,
        }
        main.delete_mock_calendar("SA test", _cfg())
        assert fake_mock_calendar["calls"] == [("delete", "SA test")]

    def test_refuses_untagged_calendar(self, fake_mock_calendar):
        fake_mock_calendar["calendars"]["Family"] = {"description": "", "events": 3}
        with pytest.raises(main.MockCalendarError):
            main.delete_mock_calendar("Family", _cfg())
        assert fake_mock_calendar["calls"] == []

    def test_refuses_real_target_calendar(self, fake_mock_calendar):
        with pytest.raises(main.MockCalendarError):
            main.delete_mock_calendar("Work", _cfg(target_calendar="Work"))

    def test_missing_calendar_is_an_error(self, fake_mock_calendar):
        with pytest.raises(main.MockCalendarError, match="no calendar named"):
            main.delete_mock_calendar("SA test", _cfg())


class TestBackfillToMockCalendar:
    def test_writes_events_to_mock_calendar_only(
        self, fake_chat_db, fake_anthropic, spy_create_event, fake_mock_calendar, tmp_path
    ):
        send_time = time.time() - 10 * 86400
        fake_chat_db([
            {
                "participants": ["+15551234567"],
                "messages": [
                    {"text": "dinner friday?", "from_me": False, "unix_ts": send_time - 3600},
                    {"text": "yes 7pm", "from_me": True, "unix_ts": send_time},
                ],
            }
        ])
        fake_anthropic([_response(_event())])

        result = main.backfill(
            _cfg(),
            since=datetime.now() - timedelta(days=15),
            until=datetime.now(),
            window_days=30,
            out_path=tmp_path / "backfill.jsonl",
            mock_calendar="SA test",
        )

        assert fake_mock_calendar["calls"] == [("create", "SA test")]
        assert len(spy_create_event["calls"]) == 1
        assert spy_create_event["calls"][0]["calendar_name"] == "SA test"
        assert result.n_events == 1
        # Real state.json still untouched; and the calendar functions are
        # restored once the run ends.
        assert not state.STATE_FILE.exists()
        assert calendar.create_event.__name__ == "fake"

    def test_safety_failure_aborts_before_any_api_call(
        self, fake_chat_db, fake_anthropic, spy_create_event, fake_mock_calendar, tmp_path
    ):
        fake_chat_db([])
        client = fake_anthropic([_response(_event())])
        with pytest.raises(main.MockCalendarError):
            main.backfill(
                _cfg(target_calendar="Work"),
                since=datetime.now() - timedelta(days=5),
                out_path=tmp_path / "backfill.jsonl",
                mock_calendar="Work",
            )
        assert client.messages.calls == []
        assert spy_create_event["calls"] == []

    def test_guard_refuses_any_other_calendar(self, spy_create_event, spy_delete_event):
        with main._mock_calendar_environment("SA test"):
            with pytest.raises(RuntimeError, match="refusing"):
                calendar.create_event(
                    title="x", date_str="2099-01-01", time_start=None,
                    duration_minutes=None, location=None, calendar_name="Work",
                )
            with pytest.raises(RuntimeError, match="refusing"):
                calendar.delete_event("uid", calendar_name="Work")
            calendar.delete_event("uid", calendar_name="SA test")
        assert spy_create_event["calls"] == []
        assert spy_delete_event["calls"] == [{"uid": "uid", "calendar_name": "SA test"}]

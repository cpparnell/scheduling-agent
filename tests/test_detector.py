from datetime import datetime

import httpx

import anthropic
from scheduling_agent import detector


def _thread(chat_id=1, messages=None, participants=("+15551234567",)):
    return {
        "chat_id": chat_id,
        "participants": list(participants),
        "messages": messages or [
            {"sender": "+15551234567", "text": "dinner friday?", "from_me": False, "unix_ts": 1700000000.0},
            {"sender": "me", "text": "yes 7pm", "from_me": True, "unix_ts": 1700000100.0},
        ],
    }


def _event(**overrides):
    base = {
        "title": "Dinner",
        "date": "2026-06-13",
        "time_start": "19:00",
        "time_confidence": 0.95,
        "duration_minutes": 60,
        "location": None,
        "confidence": 0.95,
        "status": "confirmed",
        "user_is_participant": True,
        "participation_evidence": "Me accepted the invitation",
        "recurrence": None,
        "end_date": None,
        "evidence": "yes 7pm",
    }
    base.update(overrides)
    return base


def _response(*events):
    return {"events": list(events)}


def test_confirmed_plan_returns_event_with_chat_id(fake_anthropic):
    fake_anthropic([_response(_event())])

    results, failed = detector.detect_plans([_thread(chat_id=42)])

    assert len(results) == 1
    assert results[0]["title"] == "Dinner"
    assert results[0]["chat_id"] == 42
    assert failed == set()


def test_empty_events_array_is_filtered(fake_anthropic):
    fake_anthropic([_response()])

    results, failed = detector.detect_plans([_thread()])

    assert results == []
    assert failed == set()


def test_null_date_event_is_filtered(fake_anthropic):
    fake_anthropic([_response(_event(date=None))])

    results, failed = detector.detect_plans([_thread()])

    assert results == []


def test_multiple_events_in_one_thread(fake_anthropic):
    fake_anthropic([_response(
        _event(title="Dinner", evidence="dinner friday?"),
        _event(title="The Game", date="2026-06-14", evidence="game saturday?"),
    )])
    thread = _thread(chat_id=7, messages=[
        {"sender": "+15551234567", "text": "dinner friday? and game saturday?", "from_me": False, "unix_ts": 1700000000.0},
        {"sender": "me", "text": "yes to both", "from_me": True, "unix_ts": 1700000100.0},
    ])

    results, failed = detector.detect_plans([thread])

    assert len(results) == 2
    titles = {r["title"] for r in results}
    assert titles == {"Dinner", "The Game"}
    assert all(r["chat_id"] == 7 for r in results)


def test_legacy_single_object_shape_still_parsed(fake_anthropic):
    legacy_payload = {
        "has_event": True,
        "title": "Dinner",
        "date": "2026-06-13",
        "time_start": "19:00",
        "duration_minutes": 60,
        "location": None,
        "confidence": 0.95,
        "status": "confirmed",
        "recurrence": None,
        "end_date": None,
    }
    fake_anthropic([legacy_payload])

    results, failed = detector.detect_plans([_thread()])

    assert len(results) == 1
    assert results[0]["title"] == "Dinner"


def test_legacy_has_event_false_is_filtered(fake_anthropic):
    fake_anthropic([{"has_event": False, "date": None}])

    results, failed = detector.detect_plans([_thread()])

    assert results == []


def test_malformed_json_skips_thread_but_continues(fake_anthropic):
    # First thread returns junk, second returns a valid event.
    fake_anthropic(["this is not json", _response(_event())])

    results, failed = detector.detect_plans([_thread(chat_id=1), _thread(chat_id=2)])

    assert len(results) == 1
    assert results[0]["chat_id"] == 2
    assert failed == {1}


def test_api_error_skips_thread_but_continues(fake_anthropic):
    err = anthropic.APIConnectionError(
        message="boom", request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    fake_anthropic([err, _response(_event())])

    results, failed = detector.detect_plans([_thread(chat_id=1), _thread(chat_id=2)])

    assert len(results) == 1
    assert results[0]["chat_id"] == 2
    assert failed == {1}


def test_message_missing_text_key_is_formatted_as_blank_not_a_crash(fake_anthropic):
    # A message with no "text" key (e.g. an unsupported attachment-only
    # message) must not raise a KeyError out of _format_thread.
    thread = _thread(chat_id=1, messages=[
        {"sender": "+15551234567", "from_me": False, "unix_ts": 1700000000.0},
    ])
    # evidence=None so the evidence gate can't trip on a text-less thread; this
    # test is only about _format_thread not raising.
    fake_anthropic([_response(_event(evidence=None))])

    results, failed = detector.detect_plans([thread])

    assert failed == set()
    assert len(results) == 1


def test_format_thread_failure_skips_thread_but_continues(fake_anthropic, monkeypatch):
    # Simulate a malformed thread that blows up inside _format_thread itself
    # (e.g. a non-numeric timestamp). Formatting now happens inside the
    # per-thread try block, so this must fail in isolation rather than
    # aborting the whole batch.
    bad_thread = _thread(chat_id=1, messages=[
        {"sender": "+15551234567", "text": "hi", "from_me": False, "unix_ts": "not-a-number"},
    ])
    client = fake_anthropic([_response(_event())])

    results, failed = detector.detect_plans([bad_thread, _thread(chat_id=2)])

    assert failed == {1}
    assert len(results) == 1
    assert results[0]["chat_id"] == 2
    # Only the healthy second thread reached the API call.
    assert len(client.messages.calls) == 1


def test_evidence_not_found_drops_event_by_default(fake_anthropic, caplog):
    fake_anthropic([_response(_event(evidence="this text is nowhere in the thread"))])

    with caplog.at_level("WARNING"):
        results, failed = detector.detect_plans([_thread()])

    assert results == []  # hallucination guard: unverifiable evidence drops the event
    assert failed == set()  # a gated drop is not a thread failure
    assert any("Evidence not found verbatim" in r.message for r in caplog.records)


def test_evidence_not_found_kept_when_gate_disabled(fake_anthropic, caplog):
    fake_anthropic([_response(_event(evidence="this text is nowhere in the thread"))])

    with caplog.at_level("WARNING"):
        results, failed = detector.detect_plans([_thread()], evidence_gate=False)

    assert len(results) == 1
    assert any("Evidence not found verbatim" in r.message for r in caplog.records)


def test_verbatim_evidence_passes_gate(fake_anthropic):
    fake_anthropic([_response(_event(evidence="yes 7pm"))])

    results, failed = detector.detect_plans([_thread()])

    assert len(results) == 1


def test_new_schema_fields_pass_through(fake_anthropic):
    fake_anthropic([_response(_event(user_is_participant=False, status="unanswered"))])

    results, failed = detector.detect_plans([_thread()])

    assert len(results) == 1
    assert results[0]["user_is_participant"] is False
    assert results[0]["status"] == "unanswered"


def test_format_thread_is_deterministic_with_injected_today():
    today = datetime(2026, 6, 10, 9, 0, 0)
    thread = _thread(messages=[
        {"sender": "+15551234567", "text": "lunch?", "from_me": False, "unix_ts": 1700000000.0},
        {"sender": "me", "text": "sure", "from_me": True, "unix_ts": 1700000100.0},
    ])

    out = detector._format_thread(thread, today=today)

    assert out.startswith("[Today is Wednesday, June 10, 2026]")
    assert "[Participants: +15551234567]" in out
    assert "Me (" in out  # sent message labeled Me
    assert ": lunch?" in out


def test_format_thread_annotates_stale_messages_with_age():
    today = datetime(2026, 6, 13, 9, 0, 0)
    three_days_ago = datetime(2026, 6, 10, 18, 0, 0).timestamp()
    thread = _thread(messages=[
        {"sender": "+15551234567", "text": "dinner tomorrow?", "from_me": False, "unix_ts": three_days_ago},
    ])

    out = detector._format_thread(thread, today=today)

    assert "sent 3 days ago" in out


def test_format_thread_omits_age_for_recent_messages():
    today = datetime(2026, 6, 13, 9, 0, 0)
    same_day = datetime(2026, 6, 13, 8, 0, 0).timestamp()
    thread = _thread(messages=[
        {"sender": "+15551234567", "text": "lunch?", "from_me": False, "unix_ts": same_day},
    ])

    out = detector._format_thread(thread, today=today)

    assert "sent" not in out


def test_create_call_uses_expected_model_system_and_schema(fake_anthropic):
    client = fake_anthropic([_response(_event())])

    detector.detect_plans([_thread()])

    call = client.messages.calls[0]
    assert call["model"] == detector.MODEL
    assert call["system"] == detector.SYSTEM_PROMPT
    assert call["output_config"]["format"]["schema"] is detector.RESPONSE_SCHEMA


def test_model_override_is_passed_through(fake_anthropic):
    client = fake_anthropic([_response(_event())])

    detector.detect_plans([_thread()], model="claude-sonnet-4-6")

    assert client.messages.calls[0]["model"] == "claude-sonnet-4-6"

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

    results, failed = detector.detect_plans([_thread(chat_id=7)])

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


def test_evidence_not_found_logs_warning_but_keeps_event(fake_anthropic, caplog):
    fake_anthropic([_response(_event(evidence="this text is nowhere in the thread"))])

    with caplog.at_level("WARNING"):
        results, failed = detector.detect_plans([_thread()])

    assert len(results) == 1  # not dropped
    assert any("Evidence not found verbatim" in r.message for r in caplog.records)


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

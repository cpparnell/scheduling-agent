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
        "has_event": True,
        "title": "Dinner",
        "date": "2026-06-13",
        "time_start": "19:00",
        "duration_minutes": 60,
        "location": None,
        "confidence": 0.95,
    }
    base.update(overrides)
    return base


def test_confirmed_plan_returns_event_with_chat_id(fake_anthropic):
    fake_anthropic([_event()])

    results = detector.detect_plans([_thread(chat_id=42)])

    assert len(results) == 1
    assert results[0]["title"] == "Dinner"
    assert results[0]["chat_id"] == 42


def test_has_event_false_is_filtered(fake_anthropic):
    fake_anthropic([_event(has_event=False, date=None)])

    assert detector.detect_plans([_thread()]) == []


def test_has_event_true_but_null_date_is_filtered(fake_anthropic):
    fake_anthropic([_event(has_event=True, date=None)])

    assert detector.detect_plans([_thread()]) == []


def test_malformed_json_skips_thread_but_continues(fake_anthropic):
    # First thread returns junk, second returns a valid event.
    fake_anthropic(["this is not json", _event()])

    results = detector.detect_plans([_thread(chat_id=1), _thread(chat_id=2)])

    assert len(results) == 1
    assert results[0]["chat_id"] == 2


def test_api_error_skips_thread_but_continues(fake_anthropic):
    err = anthropic.APIConnectionError(
        message="boom", request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    fake_anthropic([err, _event()])

    results = detector.detect_plans([_thread(chat_id=1), _thread(chat_id=2)])

    assert len(results) == 1
    assert results[0]["chat_id"] == 2


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


def test_create_call_uses_expected_model_system_and_schema(fake_anthropic):
    client = fake_anthropic([_event()])

    detector.detect_plans([_thread()])

    call = client.messages.calls[0]
    assert call["model"] == detector.MODEL
    assert call["system"] == detector.SYSTEM_PROMPT
    assert call["output_config"]["format"]["schema"] is detector.EVENT_SCHEMA


def test_model_override_is_passed_through(fake_anthropic):
    client = fake_anthropic([_event()])

    detector.detect_plans([_thread()], model="claude-sonnet-4-6")

    assert client.messages.calls[0]["model"] == "claude-sonnet-4-6"

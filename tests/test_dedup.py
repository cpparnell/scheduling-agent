import httpx
import pytest

import anthropic
from scheduling_agent import dedup


def _record(**overrides):
    base = {
        "hash": "abc123",
        "chat_id": 1,
        "date": "2026-06-13",
        "time_start": "19:00",
        "title": "Dinner",
        "location": None,
        "status": "confirmed",
        "evidence": "dinner at 7?",
        "calendar_uid": "UID-1",
        "created_at": "2026-06-01T10:00:00",
        "suppressed": False,
    }
    base.update(overrides)
    return base


def _new_event(**overrides):
    base = {
        "date": "2026-06-13",
        "time_start": "19:00",
        "title": "Dinner with Sam",
        "location": None,
        "chat_id": 1,
        "status": "confirmed",
        "evidence": "dinner tonight?",
        "_hash": "different-hash",
    }
    base.update(overrides)
    return base


# --- find_candidates (pure, no LLM) ----------------------------------------


def test_find_candidates_same_day():
    candidates = dedup.find_candidates(_new_event(), [_record()])
    assert len(candidates) == 1


def test_find_candidates_within_window():
    existing = [_record(date="2026-06-12"), _record(date="2026-06-14")]
    candidates = dedup.find_candidates(_new_event(date="2026-06-13"), existing, day_window=1)
    assert len(candidates) == 2


def test_find_candidates_outside_window_excluded():
    existing = [_record(date="2026-06-01")]
    candidates = dedup.find_candidates(_new_event(date="2026-06-13"), existing, day_window=1)
    assert candidates == []


def test_find_candidates_excludes_exact_hash_match():
    existing = [_record(hash="same-hash")]
    event = _new_event(**{"_hash": "same-hash"})
    candidates = dedup.find_candidates(event, existing)
    assert candidates == []


def test_find_candidates_caps_at_five_newest():
    existing = [
        _record(hash=f"h{i}", created_at=f"2026-06-01T{i:02d}:00:00")
        for i in range(8)
    ]
    candidates = dedup.find_candidates(_new_event(), existing)
    assert len(candidates) == 5
    # Newest (highest hour) first.
    assert candidates[0]["hash"] == "h7"


def test_find_candidates_unparseable_date_returns_empty():
    candidates = dedup.find_candidates(_new_event(date="not-a-date"), [_record()])
    assert candidates == []


# --- adjudicate --------------------------------------------------------


def test_adjudicate_returns_none_when_no_candidates():
    assert dedup.adjudicate(_new_event(), [], model="claude-haiku-4-5") is None


def test_adjudicate_happy_path_duplicate(fake_dedup_anthropic):
    fake_dedup_anthropic([{"is_duplicate": True, "duplicate_of": 0, "reasoning": "same plan reworded"}])

    verdict = dedup.adjudicate(_new_event(), [_record()], model="claude-haiku-4-5")

    assert verdict["is_duplicate"] is True
    assert verdict["duplicate_of"] == 0


def test_adjudicate_happy_path_not_duplicate(fake_dedup_anthropic):
    fake_dedup_anthropic([{"is_duplicate": False, "duplicate_of": None, "reasoning": "different activity"}])

    verdict = dedup.adjudicate(_new_event(), [_record()], model="claude-haiku-4-5")

    assert verdict["is_duplicate"] is False


def test_adjudicate_malformed_json_returns_none(fake_dedup_anthropic):
    fake_dedup_anthropic(["not json"])

    assert dedup.adjudicate(_new_event(), [_record()], model="claude-haiku-4-5") is None


def test_adjudicate_api_error_returns_none(fake_dedup_anthropic):
    err = anthropic.APIConnectionError(
        message="boom", request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    fake_dedup_anthropic([err])

    assert dedup.adjudicate(_new_event(), [_record()], model="claude-haiku-4-5") is None

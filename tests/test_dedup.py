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


def test_adjudicate_happy_path_reschedule_relationship(fake_dedup_anthropic):
    fake_dedup_anthropic(
        [{"is_duplicate": True, "duplicate_of": 0, "relationship": "reschedule", "reasoning": "moved to Sat"}]
    )

    verdict = dedup.adjudicate(_new_event(), [_record()], model="claude-haiku-4-5")

    assert verdict["relationship"] == "reschedule"


def test_adjudicator_schema_requires_relationship_field():
    assert "relationship" in dedup.ADJUDICATOR_SCHEMA["required"]
    assert set(dedup.ADJUDICATOR_SCHEMA["properties"]["relationship"]["enum"]) == {
        "duplicate", "reschedule", "new_occurrence",
    }


def test_adjudicator_schema_requires_confidence_field():
    # Observability only — never gates a decision (Haiku's confidences are
    # poorly calibrated), but it is what makes a wrong verdict diagnosable.
    assert "confidence" in dedup.ADJUDICATOR_SCHEMA["required"]
    assert dedup.ADJUDICATOR_SCHEMA["properties"]["confidence"]["type"] == "number"


def test_system_prompt_has_one_uncertainty_sink_for_relationship():
    """Regression on the contradiction that made verdicts flap: the prompt used
    to say 'prefer new_occurrence' when unsure about the relationship while also
    saying 'prefer is_duplicate=true' when unsure about sameness. The
    relationship sink is now 'duplicate' — the non-destructive answer."""
    prompt = dedup.SYSTEM_PROMPT
    assert "prefer \"new_occurrence\"" not in prompt
    assert "choose \"duplicate\"" in prompt


def test_adjudicate_passes_confidence_through(fake_dedup_anthropic):
    fake_dedup_anthropic([{
        "is_duplicate": True, "duplicate_of": 0, "relationship": "reschedule",
        "confidence": 0.82, "reasoning": "moved a week",
    }])

    verdict = dedup.adjudicate(_new_event(), [_record()], model="claude-haiku-4-5")

    assert verdict["confidence"] == 0.82


def test_adjudicate_happy_path_not_duplicate(fake_dedup_anthropic):
    fake_dedup_anthropic([{"is_duplicate": False, "duplicate_of": None, "reasoning": "different activity"}])

    verdict = dedup.adjudicate(_new_event(), [_record()], model="claude-haiku-4-5")

    assert verdict["is_duplicate"] is False


def test_adjudicate_malformed_json_returns_none(fake_dedup_anthropic):
    fake_dedup_anthropic(["not json"])

    assert dedup.adjudicate(_new_event(), [_record()], model="claude-haiku-4-5") is None


def test_adjudicate_malformed_json_does_not_double_count_usage(fake_dedup_anthropic):
    # Each malformed-JSON attempt is one API call that failed to parse, not
    # one call recorded as a success (usage_tracker.record) *and* a second
    # failure (usage_tracker.record_failure) — that would double-count it in
    # summary()'s total_calls/failed_calls (PR #15 review comment #3).
    from scheduling_agent import usage_tracker
    usage_tracker.reset()

    fake_dedup_anthropic(["not json"])  # reused for both the initial attempt and the retry
    dedup.adjudicate(_new_event(), [_record()], model="claude-haiku-4-5")

    summary = usage_tracker.summary()
    assert summary["total_calls"] == 0
    assert summary["failed_calls"] == 2  # initial attempt + one retry
    assert summary["call_failure_rate"] == 1.0


def test_adjudicate_api_error_returns_none(fake_dedup_anthropic):
    err = anthropic.APIConnectionError(
        message="boom", request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    fake_dedup_anthropic([err])

    assert dedup.adjudicate(_new_event(), [_record()], model="claude-haiku-4-5") is None


# --- sampling_kwargs / temperature ------------------------------------------


def _api_error():
    return anthropic.APIConnectionError(
        message="boom", request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )


def test_sampling_kwargs_passes_temperature_for_accepting_models():
    assert dedup.sampling_kwargs("claude-haiku-4-5", 0.0) == {"temperature": 0.0}
    assert dedup.sampling_kwargs("claude-sonnet-4-6", 0.3) == {"temperature": 0.3}


def test_sampling_kwargs_drops_temperature_for_rejecting_models():
    # These 400 on an explicit temperature — the kwarg must not be sent.
    for model in ("claude-opus-5", "claude-sonnet-5", "claude-opus-4-8", "claude-fable-5"):
        assert dedup.sampling_kwargs(model, 0.0) == {}, model


def test_adjudicate_sends_temperature_zero(fake_dedup_anthropic):
    client = fake_dedup_anthropic([{"is_duplicate": False, "duplicate_of": None, "reasoning": "x"}])

    dedup.adjudicate(_new_event(), [_record()], model="claude-haiku-4-5")

    assert client.messages.calls[0]["temperature"] == 0.0


def test_adjudicate_omits_temperature_for_five_family_model(fake_dedup_anthropic):
    client = fake_dedup_anthropic([{"is_duplicate": False, "duplicate_of": None, "reasoning": "x"}])

    dedup.adjudicate(_new_event(), [_record()], model="claude-opus-5")

    assert "temperature" not in client.messages.calls[0]


# --- request timeout --------------------------------------------------------


def test_client_is_constructed_with_a_request_timeout(monkeypatch):
    """A hung request with no timeout stalls the single-threaded poll loop
    indefinitely — observed as a 33-minute blocked socket read during an eval
    run."""
    captured = {}

    class _Recorder:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(dedup.anthropic, "Anthropic", _Recorder)
    monkeypatch.setattr(dedup, "_client", None)

    dedup._get_client()

    assert captured["timeout"] == dedup.REQUEST_TIMEOUT_SECONDS
    assert 0 < dedup.REQUEST_TIMEOUT_SECONDS <= 600


def test_detector_client_shares_the_same_timeout(monkeypatch):
    from scheduling_agent import detector

    captured = {}

    class _Recorder:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(detector.anthropic, "Anthropic", _Recorder)
    monkeypatch.setattr(detector, "_client", None)

    detector._get_client()

    assert captured["timeout"] == dedup.REQUEST_TIMEOUT_SECONDS


# --- retry ------------------------------------------------------------------


def test_adjudicate_retries_once_after_transient_error(fake_dedup_anthropic):
    client = fake_dedup_anthropic([
        _api_error(),
        {"is_duplicate": True, "duplicate_of": 0, "relationship": "duplicate", "reasoning": "same"},
    ])

    verdict = dedup.adjudicate(_new_event(), [_record()], model="claude-haiku-4-5")

    assert verdict["is_duplicate"] is True
    assert len(client.messages.calls) == 2


def test_adjudicate_retries_once_after_malformed_json(fake_dedup_anthropic):
    client = fake_dedup_anthropic([
        "not json",
        {"is_duplicate": False, "duplicate_of": None, "relationship": "duplicate", "reasoning": "no"},
    ])

    verdict = dedup.adjudicate(_new_event(), [_record()], model="claude-haiku-4-5")

    assert verdict["is_duplicate"] is False
    assert len(client.messages.calls) == 2


def test_adjudicate_gives_up_after_two_failures(fake_dedup_anthropic):
    client = fake_dedup_anthropic([_api_error(), _api_error()])

    assert dedup.adjudicate(_new_event(), [_record()], model="claude-haiku-4-5") is None
    # Exactly one retry — not an unbounded loop.
    assert len(client.messages.calls) == 2


def test_adjudicate_does_not_retry_on_success(fake_dedup_anthropic):
    client = fake_dedup_anthropic([
        {"is_duplicate": False, "duplicate_of": None, "relationship": "duplicate", "reasoning": "x"},
    ])

    dedup.adjudicate(_new_event(), [_record()], model="claude-haiku-4-5")

    assert len(client.messages.calls) == 1


# --- adjudication logging ---------------------------------------------------


def test_adjudicate_logs_structured_verdict_line(fake_dedup_anthropic, caplog):
    fake_dedup_anthropic([
        {"is_duplicate": True, "duplicate_of": 0, "relationship": "reschedule", "reasoning": "moved"},
    ])

    with caplog.at_level("INFO", logger="scheduling_agent.dedup"):
        dedup.adjudicate(
            _new_event(), [_record()], model="claude-haiku-4-5", source="far_exact"
        )

    line = next(r.message for r in caplog.records if "dedup_adjudication" in r.message)
    assert '"source": "far_exact"' in line
    assert '"relationship": "reschedule"' in line
    assert '"retried": false' in line
    # Candidate identity, not list index — survives reordering across calls.
    assert '"abc123"' in line


def test_adjudicate_logs_failure_and_retry_flag(fake_dedup_anthropic, caplog):
    fake_dedup_anthropic([_api_error(), _api_error()])

    with caplog.at_level("INFO", logger="scheduling_agent.dedup"):
        dedup.adjudicate(_new_event(), [_record()], model="claude-haiku-4-5")

    line = next(r.message for r in caplog.records if "dedup_adjudication" in r.message)
    assert '"failed": true' in line
    assert '"retried": true' in line

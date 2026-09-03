"""Offline tests for the eval harness plumbing (loader poll materialization and
the pipeline scorer) with the LLM faked — no API key needed."""

from datetime import date

from evals import loader
from evals.run import _FakeCalendar, _default_eval_today, score_case, score_pipeline_case


def _poll_case(**overrides):
    case = {
        "id": "pipe_test",
        "category": "pipeline",
        "participants": ["+15551234567"],
        "polls": [
            {"messages": [
                {"from_me": False, "text": "dinner {day+3} at 7pm?", "hours_ago": 8},
                {"from_me": True, "text": "yes!", "hours_ago": 7},
            ]},
            {"messages": [
                {"from_me": False, "text": "see you then!", "hours_ago": 2},
            ]},
        ],
        "expected_pipeline": {"creates": 1, "updates": 0},
    }
    case.update(overrides)
    return case


def test_materialize_polls_accumulates_context():
    threads = loader.materialize_polls(_poll_case(), today=date(2026, 6, 10), now=1_700_000_000.0)

    assert len(threads) == 2
    assert len(threads[0]["messages"]) == 2
    # Poll 2 re-feeds poll 1's messages plus its own — the production replay.
    assert len(threads[1]["messages"]) == 3
    assert threads[1]["messages"][0]["text"] == threads[0]["messages"][0]["text"]
    assert threads[0]["chat_id"] == threads[1]["chat_id"] == "pipe_test"


def test_materialize_polls_cross_chat_isolates_messages():
    case = _poll_case()
    case["polls"][1]["chat_id"] = "other_chat"
    threads = loader.materialize_polls(case, today=date(2026, 6, 10), now=1_700_000_000.0)

    assert threads[1]["chat_id"] == "other_chat"
    # A different chat does not inherit the first chat's messages.
    assert len(threads[1]["messages"]) == 1


def test_fake_calendar_round_trip():
    cal = _FakeCalendar()
    uid = cal.create_event("Dinner", "2099-01-15", "19:00", 60, None)
    assert cal.get_events_near("2099-01-15")[0]["calendar_uid"] == uid
    assert cal.update_event(uid, "Dinner", "2099-01-15", "20:00", 60, None) is True
    assert cal.get_events_near("2099-01-15")[0]["time_start"] == "20:00"
    assert cal.get_events_near("2099-03-01") == []


def _detector_event(**overrides):
    base = {
        "title": "Dinner",
        "date": (date.today().replace(year=date.today().year + 1)).isoformat(),
        "time_start": "19:00",
        "time_confidence": 0.95,
        "duration_minutes": 60,
        "location": None,
        "confidence": 0.95,
        "status": "confirmed",
        "user_is_participant": True,
        "participation_evidence": "Me accepted",
        "recurrence": None,
        "end_date": None,
        # Literal (no placeholder substitution) so it's verbatim in the poll
        # messages regardless of which {day+N} resolution is in play.
        "evidence": "yes!",
        "date_evidence": "yes!",
    }
    base.update(overrides)
    return base


def test_score_pipeline_case_replay_dedupes(fake_anthropic):
    # Both polls re-detect the same event; the journal-aware exact layer must
    # keep the second poll from creating a duplicate.
    fake_anthropic([{"events": [_detector_event()]}])

    result = score_pipeline_case(_poll_case(), model="fake", dedup_model="fake")

    assert result["creates"] == 1
    assert result["passed"] is True, result["failures"]


def test_score_pipeline_case_flags_duplicate_creation(fake_anthropic):
    # Second poll re-detects the plan under a different title/time, and the
    # (disabled-here) LLM layer can't save us — the scorer must FAIL the case.
    fake_anthropic([
        {"events": [_detector_event()]},
        {"events": [_detector_event(title="Totally different words", time_start="12:00")]},
    ])

    result = score_pipeline_case(_poll_case(), model="fake", dedup_model="fake")

    assert result["creates"] == 2
    assert result["passed"] is False


def test_weekday_of_day_placeholder_matches_day_offset():
    today = date(2026, 7, 16)
    text = loader._substitute("reunion is {day+45}, dinner on {weekday_of_day+45}", today)
    # 2026-08-30 is a Sunday; both placeholders must land on the same real date.
    assert text == "reunion is Sunday, August 30, dinner on Sunday"


def test_weekday_of_day_placeholder_unknown_token_left_alone():
    assert loader._substitute("{weekday_of_day+x}", date(2026, 7, 16)) == "{weekday_of_day+x}"


def test_days_until_weekday_same_day_is_zero():
    # 2026-07-16 is a Thursday: "this Thursday" said on a Thursday means today.
    today = date(2026, 7, 16)
    assert loader._days_until_weekday(today, "thursday") == 0
    assert loader._substitute("{thursday}", today) == "Thursday"


# --- Eval clock pinning ------------------------------------------------------


def test_default_eval_today_is_a_wednesday():
    # Regardless of which real weekday the test suite runs on, the pinned eval
    # clock always lands on a Wednesday so "this <weekday>" cases never flake.
    assert _default_eval_today().weekday() == 2


def test_default_eval_today_respects_env_override(monkeypatch):
    monkeypatch.setenv("EVAL_TODAY", "2026-01-05")  # a Monday
    assert _default_eval_today() == date(2026, 1, 5)


def test_score_case_sends_pinned_wednesday_header_to_the_model(fake_anthropic):
    client = fake_anthropic([{"events": []}])
    case = {
        "id": "pin_test", "category": "positive",
        "participants": ["+15551234567"],
        "messages": [{"from_me": False, "text": "dinner friday?", "hours_ago": 2}],
        "expected": {"has_event": False},
    }

    score_case(case, model="fake")

    sent = client.messages.calls[0]["messages"][0]["content"]
    assert "[Today is Wednesday" in sent

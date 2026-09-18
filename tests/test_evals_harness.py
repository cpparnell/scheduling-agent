"""Offline tests for the eval harness plumbing (loader poll materialization,
the pipeline scorer, and the --repeat/--diff variance instrumentation) with the
LLM faked — no API key needed."""

from datetime import date

from evals import loader
from evals.run import (
    _FakeCalendar,
    _default_eval_today,
    aggregate_repeats,
    diff_reports,
    failure_sets,
    known_failure_ids,
    score_case,
    score_pipeline_case,
    summarize,
)


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


# --- Variance instrumentation: gating, --repeat, --diff -----------------------


def _det(case_id, passed, *, known_failure=False, category="positive"):
    """Minimal detector-result shape as summarize() consumes it."""
    return {
        "id": case_id, "category": category, "known_failure": known_failure,
        "passed": passed, "expected_has_event": True, "predicted_has_event": passed,
        "confidence": 0.9,
    }


def _pipe(case_id, passed, *, known_failure=False):
    return {"id": case_id, "category": "pipeline", "known_failure": known_failure,
            "passed": passed, "creates": 1, "updates": 0, "failures": []}


def _dedup(case_id, passed, *, known_failure=False, expected="same"):
    return {"id": case_id, "dedup_with": f"{case_id}_a", "expected_verdict": expected,
            "got_verdict": expected if passed else "different", "passed": passed,
            "known_failure": known_failure}


def _report(detector=(), pipeline=(), dedup=()):
    return {
        "summary": summarize(list(detector), list(dedup), list(pipeline)),
        "results": list(detector),
        "pipeline_results": list(pipeline),
        "dedup_results": list(dedup),
    }


def test_known_failure_pipeline_case_does_not_count_against_accuracy():
    # The regression this gating fix exists for: an aspirational pipeline case
    # must not drag pipeline_accuracy down, exactly as for detector accuracy.
    summary = summarize(
        [_det("d1", True)],
        pipeline_results=[_pipe("p_ok", True), _pipe("p_aspirational", False, known_failure=True)],
    )

    assert summary["pipeline_accuracy"] == 1.0
    assert summary["n_pipeline_gated"] == 1
    assert summary["pipeline_failed"] == []
    # ...but the ungated list still records it, so --diff can see it flip.
    assert summary["pipeline_failed_all"] == ["p_aspirational"]


def test_known_failure_dedup_pair_does_not_count_against_accuracy():
    summary = summarize(
        [_det("d1", True)],
        dedup_results=[_dedup("dd_ok", True), _dedup("dd_aspirational", False, known_failure=True)],
    )

    assert summary["dedup_accuracy"] == 1.0
    assert summary["n_dedup_gated"] == 1
    assert summary["dedup_same_missed"] == []
    assert summary["dedup_failed_all"] == ["dd_aspirational"]


def test_score_dedup_pairs_stamps_pair_aware_known_failure(fake_anthropic):
    """Either half being flagged makes the pair aspirational — a flagged
    reference detection makes the pair's verdict meaningless."""
    from evals.run import score_dedup_pairs

    fake_anthropic([{"is_duplicate": True, "duplicate_of": 0,
                     "relationship": "duplicate", "reasoning": "same"}])
    cases = [
        {"id": "a", "known_failure": True},
        {"id": "b", "dedup_with": "a", "dedup_verdict": "same"},
    ]
    event = {"title": "Dinner", "date": "2099-01-15", "time_start": "19:00"}
    results_by_id = {"a": {"events": [event]}, "b": {"events": [event]}}

    out = score_dedup_pairs(cases, results_by_id, model="fake", day_window=7)

    assert len(out) == 1
    # Flag came from the REFERENCE case, not this one.
    assert out[0]["known_failure"] is True


def test_score_dedup_pairs_stamps_flag_on_missing_detection_path():
    from evals.run import score_dedup_pairs

    cases = [
        {"id": "a"},
        {"id": "b", "dedup_with": "a", "dedup_verdict": "same", "known_failure": True},
    ]
    results_by_id = {"a": {"events": []}, "b": {"events": [{"title": "x"}]}}

    out = score_dedup_pairs(cases, results_by_id, model="fake", day_window=7)

    assert out[0]["note"].startswith("missing detection")
    assert out[0]["known_failure"] is True


def test_failure_sets_splits_by_suite():
    report = _report(
        detector=[_det("d_ok", True), _det("d_bad", False)],
        pipeline=[_pipe("p_bad", False)],
        dedup=[_dedup("dd_bad", False)],
    )

    sets = failure_sets(report)

    assert sets == {"detector": {"d_bad"}, "pipeline": {"p_bad"}, "dedup": {"dd_bad"}}


def test_failure_sets_includes_known_failures():
    # A known-failure case must appear in the diff's failure set, otherwise a
    # flip from failing to passing would be invisible to flag hygiene.
    report = _report(pipeline=[_pipe("p_aspirational", False, known_failure=True)])

    assert failure_sets(report)["pipeline"] == {"p_aspirational"}


def test_failure_sets_tolerates_pre_pr1_report_shape():
    """Pre-PR1 reports have no *_failed_all keys and may omit the per-case
    lists entirely — every baseline report on disk predates this change."""
    legacy = {
        "summary": {"pipeline_failed": ["p_old"], "dedup_same_missed": ["dd_old"]},
        "results": [{"id": "d_old", "passed": False}],
    }

    sets = failure_sets(legacy)

    assert sets == {"detector": {"d_old"}, "pipeline": {"p_old"}, "dedup": {"dd_old"}}


def test_aggregate_repeats_splits_always_failed_from_flaky():
    run1 = _report(pipeline=[_pipe("always", False), _pipe("sometimes", False), _pipe("never", True)])
    run2 = _report(pipeline=[_pipe("always", False), _pipe("sometimes", True), _pipe("never", True)])
    run3 = _report(pipeline=[_pipe("always", False), _pipe("sometimes", False), _pipe("never", True)])

    agg = aggregate_repeats([run1, run2, run3])

    assert agg["n_runs"] == 3
    assert agg["suites"]["pipeline"]["always_failed"] == ["always"]
    assert agg["suites"]["pipeline"]["flaky"] == ["sometimes"]
    assert agg["always_failed"] == ["always"]
    assert agg["flaky"] == ["sometimes"]


def test_aggregate_repeats_single_run_has_no_flaky_set():
    agg = aggregate_repeats([_report(pipeline=[_pipe("bad", False)])])

    assert agg["suites"]["pipeline"]["always_failed"] == ["bad"]
    assert agg["suites"]["pipeline"]["flaky"] == []


def test_diff_reports_classifies_newly_failing_and_passing():
    before = _report(pipeline=[_pipe("fixed", False), _pipe("stuck", False), _pipe("clean", True)])
    after = _report(pipeline=[_pipe("fixed", True), _pipe("stuck", False), _pipe("clean", False)])

    diff = diff_reports(before, after)

    assert diff["suites"]["pipeline"]["newly_failing"] == ["clean"]
    assert diff["suites"]["pipeline"]["newly_passing"] == ["fixed"]
    assert diff["suites"]["pipeline"]["still_failing"] == ["stuck"]
    assert diff["regressions"] == ["clean"]


def test_diff_reports_newly_failing_known_failure_is_not_a_regression():
    # A flagged case flipping back to failing is tracked, not a merge blocker.
    before = _report(pipeline=[_pipe("aspirational", True, known_failure=True)])
    after = _report(pipeline=[_pipe("aspirational", False, known_failure=True)])

    diff = diff_reports(before, after)

    assert diff["suites"]["pipeline"]["newly_failing"] == ["aspirational"]
    assert diff["regressions"] == []


def test_diff_reports_dedupes_a_regression_failing_in_two_suites():
    """A detector miss also sinks that case's dedup pair, so the same id shows
    up as newly-failing in both suites — it is one regression, not two."""
    before = _report(detector=[_det("both", True)], dedup=[_dedup("both", True)])
    after = _report(detector=[_det("both", False)], dedup=[_dedup("both", False)])

    diff = diff_reports(before, after)

    assert diff["suites"]["detector"]["newly_failing"] == ["both"]
    assert diff["suites"]["dedup"]["newly_failing"] == ["both"]
    assert diff["regressions"] == ["both"]


# --- invalid-run detection ----------------------------------------------------
#
# Regression on a real incident: a --repeat 3 run exhausted the account's API
# credit partway through. Run 3 made ZERO successful calls, every case reported
# "expected an event, none produced", and the harness printed "28% accuracy,
# 0% false-positive rate" as if it were a measurement of the model.


def _invalid_report(reason="no API call succeeded"):
    r = _report(detector=[_det("d1", False)])
    r["summary"]["run_valid"] = False
    r["summary"]["invalid_reason"] = reason
    return r


def test_run_validity_flags_a_run_with_no_successful_calls():
    from evals.run import run_validity

    verdict = run_validity({"total_calls": 0, "failed_calls": 196, "call_failure_rate": 1.0})

    assert verdict["valid"] is False
    assert "never reached the model" in verdict["reason"]


def test_run_validity_flags_a_high_failure_rate():
    from evals.run import run_validity

    verdict = run_validity({"total_calls": 50, "failed_calls": 50, "call_failure_rate": 0.5})

    assert verdict["valid"] is False


def test_run_validity_tolerates_a_few_stray_failures():
    from evals.run import run_validity

    verdict = run_validity({"total_calls": 196, "failed_calls": 2, "call_failure_rate": 0.0101})

    assert verdict["valid"] is True


def test_run_validity_of_a_fully_successful_run():
    from evals.run import run_validity

    assert run_validity({"total_calls": 196, "failed_calls": 0, "call_failure_rate": 0.0})["valid"]


def test_aggregate_repeats_excludes_invalid_runs():
    good1 = _report(pipeline=[_pipe("a", False), _pipe("b", True)])
    good2 = _report(pipeline=[_pipe("a", False), _pipe("b", True)])
    dead = _invalid_report()

    agg = aggregate_repeats([good1, good2, dead])

    assert agg["n_runs"] == 2
    assert agg["n_invalid_runs"] == 1
    assert agg["invalid_runs"] == [3]
    # "b" must not be dragged into always_failed by the dead run.
    assert agg["suites"]["pipeline"]["always_failed"] == ["a"]
    assert agg["suites"]["pipeline"]["flaky"] == []


def test_aggregate_repeats_with_only_invalid_runs_measures_nothing():
    agg = aggregate_repeats([_invalid_report(), _invalid_report()])

    assert agg["n_runs"] == 0
    assert agg["always_failed"] == []
    assert agg["flaky"] == []


def test_diff_reports_refuses_to_compare_an_invalid_run():
    good = _report(pipeline=[_pipe("a", True)])

    diff = diff_reports(good, _invalid_report())

    assert diff["invalid"]
    # Crucially: no regressions are reported, so an outage can never be
    # mistaken for a code regression.
    assert diff["regressions"] == []


def test_diff_reports_refuses_when_the_baseline_is_invalid():
    diff = diff_reports(_invalid_report(), _report(pipeline=[_pipe("a", True)]))

    assert diff["invalid"]


def test_usage_tracker_reports_failed_calls():
    from scheduling_agent import usage_tracker

    usage_tracker.reset()
    usage_tracker.record_failure("boom")
    usage_tracker.record_failure("boom")

    summary = usage_tracker.summary()

    assert summary["total_calls"] == 0
    assert summary["failed_calls"] == 2
    assert summary["call_failure_rate"] == 1.0
    usage_tracker.reset()


# --- whole-run wall-clock budget ----------------------------------------------
#
# REQUEST_TIMEOUT_SECONDS bounds one request; nothing bounded a run. Observed: a
# run sat at 0.2s of CPU per 90s of wall clock for 1.5 hours against a degraded
# API — every individual request was within its timeout, the run was not.


def _expired_deadline():
    from evals.run import Deadline
    d = Deadline(minutes=1)
    d.started -= 3600  # pretend an hour has passed
    return d


def test_deadline_disabled_never_expires():
    from evals.run import Deadline

    d = Deadline(minutes=0)
    d.started -= 10_000

    assert d.expired() is False


def test_deadline_expires_past_its_limit():
    assert _expired_deadline().expired() is True


def test_deadline_within_limit_does_not_expire():
    from evals.run import Deadline

    assert Deadline(minutes=60).expired() is False


def test_run_validity_marks_a_budget_exceeded_run_invalid():
    from evals.run import run_validity

    d = _expired_deadline()
    d.skip(42)
    # Calls all succeeded — only the clock ran out, so the failure-rate checks
    # would happily call this a good run.
    verdict = run_validity({"total_calls": 100, "failed_calls": 0, "call_failure_rate": 0.0}, d)

    assert verdict["valid"] is False
    assert "wall-clock budget exceeded" in verdict["reason"]
    assert "42 case(s) not run" in verdict["reason"]


def test_run_validity_ignores_an_unexpired_deadline():
    from evals.run import Deadline, run_validity

    verdict = run_validity(
        {"total_calls": 100, "failed_calls": 0, "call_failure_rate": 0.0}, Deadline(minutes=60)
    )

    assert verdict["valid"] is True


def test_run_stops_scoring_detector_cases_once_the_budget_is_blown(fake_anthropic):
    from evals.run import run

    fake_anthropic([{"events": []}])
    cases = [
        {"id": f"c{i}", "category": "positive", "participants": ["+15551234567"],
         "messages": [{"from_me": False, "text": "hi", "hours_ago": 1}],
         "expected": {"has_event": False}}
        for i in range(5)
    ]
    d = _expired_deadline()

    results = run(cases, model="fake", deadline=d)

    assert results == []           # stopped at the first case boundary
    assert d.skipped == 5


def test_run_pipeline_stops_once_the_budget_is_blown():
    from evals.run import run_pipeline

    d = _expired_deadline()

    results = run_pipeline([_poll_case()], model="fake", dedup_model="fake", deadline=d)

    assert results == []
    assert d.skipped == 1


def test_score_dedup_pairs_skips_a_pair_whose_half_was_never_scored():
    """A truncated detector phase leaves ids unscored — the pair must be
    skipped, not raise KeyError."""
    from evals.run import score_dedup_pairs

    cases = [{"id": "a"}, {"id": "b", "dedup_with": "a", "dedup_verdict": "same"}]
    # "a" never got scored.
    out = score_dedup_pairs(cases, {"b": {"events": [{"title": "x"}]}}, model="fake", day_window=7)

    assert out == []


def test_known_failure_ids_backfills_from_legacy_summary():
    legacy = {"summary": {"known_failures": ["old_flagged"]}, "results": []}

    assert known_failure_ids(legacy) == {"old_flagged"}


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

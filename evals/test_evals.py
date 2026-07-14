"""Pytest entry point for the paid eval suite. Excluded from the default run;
invoke with ``pytest -m eval`` (needs ANTHROPIC_API_KEY)."""

import pytest

from evals import loader
from evals.run import run, run_pipeline, score_dedup_pairs, summarize
from scheduling_agent import detector

pytestmark = pytest.mark.eval

DEDUP_MODEL = "claude-haiku-4-5"


@pytest.fixture(scope="module")
def golden_cases():
    return loader.load_golden()


@pytest.fixture(scope="module")
def detector_results(golden_cases):
    return run(golden_cases, model=detector.MODEL)


def test_detector_baseline_accuracy_and_no_false_positives(golden_cases, detector_results):
    summary = summarize(detector_results)

    false_positives = [
        r["id"] for r in detector_results
        if r["category"] == "hard_negative" and r["predicted_has_event"]
    ]
    # A false positive silently drops a junk event onto the user's real calendar.
    assert summary["false_positive_rate"] == 0.0, f"false positives: {false_positives}"
    assert summary["accuracy"] >= 0.8, summary


def test_bystander_plans_never_reach_the_calendar(detector_results):
    """The ownership-hallucination gate: a third party's plan ("SHE is going to
    the lake house") must never produce an event that would pass the
    participation/status gates. Zero tolerance, like the false-positive rate."""
    summary = summarize(detector_results)
    assert summary["bystander_leaks"] == [], (
        f"third-party plans would reach the calendar: {summary['bystander_leaks']}"
    )


def test_bystander_controls_still_detected(detector_results):
    """The flip side: when the user IS included, the plan must still be
    detected with user_is_participant=true — the gate must not eat real plans."""
    controls = [r for r in detector_results if r["category"] == "bystander_control"]
    assert controls, "no bystander_control cases found"
    failed = [r["id"] for r in controls if not r["passed"]]
    assert failed == [], f"participant plans wrongly filtered or misdetected: {failed}"


def test_dedup_adjudicator_verdicts(golden_cases, detector_results):
    """Hard gate on the pairwise adjudicator: every known-duplicate pair must
    be caught, and no genuinely-different control may be merged (the flipped
    uncertainty bias's failure mode)."""
    results_by_id = {r["id"]: r for r in detector_results}
    dedup_results = score_dedup_pairs(golden_cases, results_by_id, model=DEDUP_MODEL)

    assert dedup_results, "no dedup pairs found"
    missed = [r["id"] for r in dedup_results if r["expected_verdict"] == "same" and not r["passed"]]
    merged = [r["id"] for r in dedup_results if r["expected_verdict"] == "different" and not r["passed"]]
    assert missed == [], f"duplicate plans not caught: {missed}"
    assert merged == [], f"distinct plans wrongly merged: {merged}"


def test_pipeline_multi_poll_scenarios(golden_cases):
    """Hard gate on the end-to-end duplication scenarios: growing-context
    replay, reworded re-mentions, cross-chat duplicates, reschedules-as-updates,
    and distinct-plans controls, each scored on exact create/update counts."""
    pipeline_results = run_pipeline(golden_cases, model=detector.MODEL, dedup_model=DEDUP_MODEL)

    assert pipeline_results, "no pipeline cases found"
    failed = {
        r["id"]: r["failures"] for r in pipeline_results
        if not r["passed"] and not r["known_failure"]
    }
    assert failed == {}, f"pipeline scenarios failed: {failed}"

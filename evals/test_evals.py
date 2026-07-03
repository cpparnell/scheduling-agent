"""Pytest entry point for the paid eval suite. Excluded from the default run;
invoke with ``pytest -m eval`` (needs ANTHROPIC_API_KEY)."""

import pytest

from evals import loader
from evals.run import run, score_dedup_pairs, summarize
from scheduling_agent import detector

pytestmark = pytest.mark.eval


def test_detector_baseline_accuracy_and_no_false_positives():
    cases = loader.load_golden()
    results = run(cases, model=detector.MODEL)
    summary = summarize(results)

    false_positives = [
        r["id"] for r in results
        if r["category"] == "hard_negative" and r["predicted_has_event"]
    ]
    # A false positive silently drops a junk event onto the user's real calendar.
    assert summary["false_positive_rate"] == 0.0, f"false positives: {false_positives}"
    assert summary["accuracy"] >= 0.8, summary


def test_dedup_adjudicator_catches_known_duplicates():
    """Report-only for now: run the real adjudicator over the golden dedup
    pairs and print the accuracy. Once a baseline run confirms the model
    reliably catches the "same"-verdict pairs, tighten this to a hard assert
    (see PLAN.md Step 6)."""
    cases = loader.load_golden()
    results = run(cases, model=detector.MODEL)
    results_by_id = {r["id"]: r for r in results}
    dedup_results = score_dedup_pairs(cases, results_by_id, model="claude-haiku-4-5")

    missed = [r for r in dedup_results if r["expected_verdict"] == "same" and not r["passed"]]
    accuracy = sum(r["passed"] for r in dedup_results) / len(dedup_results) if dedup_results else 0.0
    print(f"\ndedup_accuracy={accuracy:.0%} missed_duplicates={[r['id'] for r in missed]}")

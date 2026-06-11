"""Pytest entry point for the paid eval suite. Excluded from the default run;
invoke with ``pytest -m eval`` (needs ANTHROPIC_API_KEY)."""

import pytest

from evals import loader
from evals.run import run, summarize
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

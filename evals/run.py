"""Detector eval harness.

Runs each golden case through the real ``detector.detect_plans`` and scores the
structured output programmatically (exact-match on has_event/date/time, substring
on title/location). Prints a per-case table + aggregate metrics and writes a
diffable JSON report so prompt/model changes can be compared.

Usage:
    python -m evals.run                          # baseline on the default model
    python -m evals.run --model claude-sonnet-4-6
    python -m evals.run --judge                  # + LLM title-quality score
    python -m evals.run -k dinner                # only cases whose id contains 'dinner'
"""

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from evals import loader
from scheduling_agent import detector

REPORTS_DIR = Path(__file__).parent / "reports"

_GOT_FIELDS = ("title", "date", "time_start", "location", "confidence", "status")


def score_case(case: dict, model: str) -> dict:
    thread, expected = loader.materialize_case(case)
    events = detector.detect_plans([thread], model=model)
    got = events[0] if events else None

    failures: list[str] = []
    predicted_has_event = got is not None

    if predicted_has_event != expected["has_event"]:
        failures.append(
            "expected an event, none produced" if expected["has_event"]
            else "false positive: event produced for a non-plan"
        )

    if expected["has_event"] and got is not None:
        if "date" in expected and got.get("date") != expected["date"]:
            failures.append(f"date {got.get('date')} != {expected['date']}")
        exp_time = expected.get("time_start")
        if exp_time and got.get("time_start") != exp_time:
            failures.append(f"time_start {got.get('time_start')} != {exp_time}")
        if "status" in expected and got.get("status") != expected["status"]:
            failures.append(f"status {got.get('status')!r} != {expected['status']!r}")
        if "title_contains_any" in expected:
            title = (got.get("title") or "").lower()
            if not any(s.lower() in title for s in expected["title_contains_any"]):
                failures.append(
                    f"title {got.get('title')!r} missing any of {expected['title_contains_any']}"
                )
        if "location_contains_any" in expected:
            loc = (got.get("location") or "").lower()
            if not any(s.lower() in loc for s in expected["location_contains_any"]):
                failures.append(
                    f"location {got.get('location')!r} missing any of {expected['location_contains_any']}"
                )

    return {
        "id": case["id"],
        "category": case.get("category", "positive"),
        "known_failure": case.get("known_failure", False),
        "passed": not failures,
        "predicted_has_event": predicted_has_event,
        "expected_has_event": expected["has_event"],
        "got": None if got is None else {k: got.get(k) for k in _GOT_FIELDS},
        "confidence": None if got is None else got.get("confidence"),
        "failures": failures,
    }


def run(cases: list[dict], model: str = detector.MODEL, judge: bool = False) -> list[dict]:
    results = [score_case(c, model) for c in cases]
    if judge:
        from evals import judge as judge_mod
        for result, case in zip(results, cases):
            if result["passed"] and result["expected_has_event"] and result["got"]:
                thread, _ = loader.materialize_case(case)
                result["title_quality"] = judge_mod.score_title(thread, result["got"]["title"])
    return results


def summarize(results: list[dict]) -> dict:
    gated = [r for r in results if not r["known_failure"]]
    negatives = [r for r in results if r["category"] == "hard_negative"]
    tentatives = [r for r in gated if r["category"] == "tentative"]
    positives = [r for r in gated if r["expected_has_event"] and r["category"] != "tentative"]
    fps = sum(1 for r in negatives if r["predicted_has_event"])

    conf: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r["confidence"] is not None:
            conf[r["category"]].append(r["confidence"])

    return {
        "accuracy": (sum(r["passed"] for r in gated) / len(gated)) if gated else 0.0,
        "positive_recall": (sum(r["passed"] for r in positives) / len(positives)) if positives else 0.0,
        "tentative_recall": (sum(r["passed"] for r in tentatives) / len(tentatives)) if tentatives else 0.0,
        "false_positive_rate": (fps / len(negatives)) if negatives else 0.0,
        "false_positives": fps,
        "n_total": len(results),
        "n_gated": len(gated),
        "n_passed_gated": sum(r["passed"] for r in gated),
        "known_failures": [r["id"] for r in results if r["known_failure"]],
        "mean_confidence_by_category": {k: round(sum(v) / len(v), 3) for k, v in conf.items()},
    }


def print_report(results: list[dict], summary: dict, model: str) -> None:
    print(f"\n=== Detector eval — model={model} ===")
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        flag = " (known-fail)" if r["known_failure"] else ""
        line = f"  [{status}] {r['id']}{flag}"
        if r["failures"]:
            line += "  — " + "; ".join(r["failures"])
        if "title_quality" in r:
            line += f"  [title q={r['title_quality']}]"
        print(line)
    print(
        f"\n  accuracy (excl. known-fail): {summary['accuracy']:.0%} "
        f"({summary['n_passed_gated']}/{summary['n_gated']})"
    )
    print(f"  positive recall:             {summary['positive_recall']:.0%}")
    print(f"  tentative recall:            {summary['tentative_recall']:.0%}")
    print(
        f"  false-positive rate (neg):   {summary['false_positive_rate']:.0%} "
        f"({summary['false_positives']} hard-negative(s) produced an event)"
    )
    print(f"  mean confidence by category: {summary['mean_confidence_by_category']}")
    if summary["known_failures"]:
        print(f"  known failures (tracked):    {', '.join(summary['known_failures'])}")


def write_report(results: list[dict], summary: dict, model: str) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = REPORTS_DIR / f"{ts}_{model.replace('/', '_')}.json"
    path.write_text(json.dumps({"model": model, "summary": summary, "results": results}, indent=2))
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the detector eval suite.")
    ap.add_argument("--model", default=detector.MODEL)
    ap.add_argument("--judge", action="store_true", help="add LLM title-quality scoring")
    ap.add_argument("-k", "--filter", default=None, help="only run cases whose id contains this")
    ap.add_argument("--golden", default=str(loader.GOLDEN_PATH))
    args = ap.parse_args()

    cases = loader.load_golden(Path(args.golden))
    if args.filter:
        cases = [c for c in cases if args.filter in c["id"]]
    if not cases:
        print("No cases matched.")
        return

    results = run(cases, model=args.model, judge=args.judge)
    summary = summarize(results)
    print_report(results, summary, args.model)
    path = write_report(results, summary, args.model)
    print(f"\n  report: {path}")


if __name__ == "__main__":
    main()

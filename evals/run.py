"""Detector eval harness.

Runs each golden case through the real ``detector.detect_plans`` and scores the
structured output programmatically (exact-match on has_event/date/time, substring
on title/location). A separate phase adjudicates the "dedup" pairs through the
real ``dedup.adjudicate`` and scores same/different verdicts. Prints a per-case
table + aggregate metrics and writes a diffable JSON report so prompt/model
changes can be compared.

Usage:
    python -m evals.run                          # baseline on the default model
    python -m evals.run --model claude-sonnet-4-6
    python -m evals.run --judge                  # + LLM title-quality score
    python -m evals.run -k dinner                # only cases whose id contains 'dinner'
"""

import argparse
import json
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from evals import loader
from scheduling_agent import dedup, detector

LOGS_DIR = Path(__file__).parent.parent / "logs"
REPORTS_DIR = LOGS_DIR / "evals"

_GOT_FIELDS = (
    "title", "date", "time_start", "time_confidence", "location",
    "confidence", "status", "user_is_participant", "participation_evidence",
    "recurrence", "end_date", "evidence",
)


def _check_event_fields(expected: dict, got: dict) -> list[str]:
    """Field-level checks shared by single- and multi-event scoring. `expected`
    may omit any key to skip that check (matches the existing golden.jsonl
    convention of only asserting what matters for a given case)."""
    failures: list[str] = []
    if "date" in expected and got.get("date") != expected["date"]:
        failures.append(f"date {got.get('date')} != {expected['date']}")
    if "time_start" in expected and got.get("time_start") != expected["time_start"]:
        failures.append(f"time_start {got.get('time_start')!r} != {expected['time_start']!r}")
    if "status" in expected and got.get("status") != expected["status"]:
        failures.append(f"status {got.get('status')!r} != {expected['status']!r}")
    if "user_is_participant" in expected and got.get("user_is_participant") != expected["user_is_participant"]:
        failures.append(
            f"user_is_participant {got.get('user_is_participant')!r} != {expected['user_is_participant']!r}"
        )
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
    if "recurrence" in expected and got.get("recurrence") != expected["recurrence"]:
        failures.append(f"recurrence {got.get('recurrence')!r} != {expected['recurrence']!r}")
    if "end_date" in expected and got.get("end_date") != expected["end_date"]:
        failures.append(f"end_date {got.get('end_date')!r} != {expected['end_date']!r}")
    return failures


def _matches_loosely(expected: dict, got: dict) -> bool:
    """Cheap candidate-matching key for greedy multi-event pairing: same date
    (when asserted) and at least one expected title substring (when asserted)."""
    if "date" in expected and got.get("date") != expected["date"]:
        return False
    if "title_contains_any" in expected:
        title = (got.get("title") or "").lower()
        if not any(s.lower() in title for s in expected["title_contains_any"]):
            return False
    return True


def _score_multi_event(expected_events: list[dict], got_events: list[dict]) -> list[str]:
    failures: list[str] = []
    remaining = list(got_events)
    for i, exp_ev in enumerate(expected_events):
        match = next((g for g in remaining if _matches_loosely(exp_ev, g)), None)
        if match is None:
            failures.append(f"expected event #{i} not detected: {exp_ev}")
            continue
        remaining.remove(match)
        failures.extend(_check_event_fields(exp_ev, match))
    for g in remaining:
        failures.append(f"hallucinated extra event: {g.get('title')!r} on {g.get('date')}")
    return failures


def _would_reach_calendar(event: dict) -> bool:
    """Mirror the production ownership/status gates: an event only reaches the
    calendar when the user participates and the invitation isn't unanswered."""
    return bool(event.get("user_is_participant")) and event.get("status") != "unanswered"


def score_case(case: dict, model: str) -> dict:
    thread, expected = loader.materialize_case(case)
    events, failed = detector.detect_plans([thread], model=model)

    failures: list[str] = []

    if expected.get("no_calendar_event"):
        # Bystander cases: the detector may either not emit the third-party
        # plan at all, or emit it flagged so the participation/status gates
        # stop it. Both are safe; an event that would reach the calendar fails.
        leaks = [e for e in events if _would_reach_calendar(e)]
        for e in leaks:
            failures.append(
                f"would reach calendar: {e.get('title')!r} on {e.get('date')} "
                f"(user_is_participant={e.get('user_is_participant')}, status={e.get('status')!r})"
            )
        got = events[0] if events else None
        return {
            "id": case["id"],
            "category": case.get("category", "bystander"),
            "known_failure": case.get("known_failure", False),
            "passed": not failures,
            "predicted_has_event": bool(leaks),
            "expected_has_event": False,
            "got": None if got is None else {k: got.get(k) for k in _GOT_FIELDS},
            "events": events,
            "confidence": None if got is None else got.get("confidence"),
            "failures": failures,
        }

    if "events" in expected:
        predicted_has_event = len(events) > 0
        expected_has_event = True
        if not events:
            failures.append("expected event(s), none produced")
        else:
            failures.extend(_score_multi_event(expected["events"], events))
        got = events[0] if events else None
    else:
        got = events[0] if events else None
        predicted_has_event = got is not None
        expected_has_event = expected["has_event"]

        if predicted_has_event != expected_has_event:
            failures.append(
                "expected an event, none produced" if expected_has_event
                else "false positive: event produced for a non-plan"
            )
        if expected_has_event and got is not None:
            failures.extend(_check_event_fields(expected, got))
        if len(events) > 1:
            failures.append(f"hallucinated {len(events) - 1} extra event(s) beyond the expected one")

    return {
        "id": case["id"],
        "category": case.get("category", "positive"),
        "known_failure": case.get("known_failure", False),
        "passed": not failures,
        "predicted_has_event": predicted_has_event,
        "expected_has_event": expected_has_event,
        "got": None if got is None else {k: got.get(k) for k in _GOT_FIELDS},
        "events": events,  # full event dicts, used by the dedup-scoring phase
        "confidence": None if got is None else got.get("confidence"),
        "failures": failures,
    }


def score_dedup_pairs(cases: list[dict], results_by_id: dict, model: str) -> list[dict]:
    """For golden cases annotated with dedup_with/dedup_verdict, treat the
    referenced case's detected event as an "existing calendar event" and run
    the real dedup.find_candidates + dedup.adjudicate against this case's
    detected event(s), scoring the resulting same/different verdict."""
    dedup_results = []

    for case in cases:
        if "dedup_with" not in case:
            continue

        ref_id = case["dedup_with"]
        expected_verdict = case["dedup_verdict"]
        b_events = results_by_id[case["id"]]["events"]
        a_events = results_by_id[ref_id]["events"]

        if not a_events or not b_events:
            dedup_results.append({
                "id": case["id"], "dedup_with": ref_id, "expected_verdict": expected_verdict,
                "got_verdict": None, "passed": False,
                "note": "missing detection on one side of the pair",
            })
            continue

        existing_records = [{**a, "hash": f"eval-{ref_id}", "created_at": "2020-01-01T00:00:00"}
                             for a in a_events]

        called_llm = False
        any_duplicate = False
        reasoning = None
        for b in b_events:
            candidates = dedup.find_candidates(b, existing_records)
            if not candidates:
                continue
            called_llm = True
            verdict = dedup.adjudicate(b, candidates, model=model)
            if verdict and verdict.get("is_duplicate"):
                any_duplicate = True
                reasoning = verdict.get("reasoning")
                break

        got_verdict = "same" if any_duplicate else "different"
        dedup_results.append({
            "id": case["id"],
            "dedup_with": ref_id,
            "expected_verdict": expected_verdict,
            "got_verdict": got_verdict,
            "called_llm": called_llm,
            "reasoning": reasoning,
            "passed": got_verdict == expected_verdict,
        })

    return dedup_results


class _FakeCalendar:
    """In-memory stand-in for calendar.py used by the pipeline phase: records
    creates/updates and serves get_events_near from what has been created, so
    reconciliation's calendar-query layer works against it."""

    def __init__(self):
        self.events: dict[str, dict] = {}
        self.creates = 0
        self.updates = 0

    def create_event(self, title, date_str, time_start, duration_minutes, location,
                     calendar_name="Calendar", tentative=False, recurrence=None, end_date=None):
        uid = f"uid-{self.creates}"
        self.creates += 1
        self.events[uid] = {
            "title": title, "date": date_str, "time_start": time_start,
            "location": location, "tentative": tentative,
        }
        return uid

    def update_event(self, uid, title, date_str, time_start, duration_minutes, location,
                     calendar_name="Calendar", tentative=False, end_date=None):
        self.updates += 1
        if uid not in self.events:
            return False
        self.events[uid].update({
            "title": title, "date": date_str, "time_start": time_start,
            "location": location, "tentative": tentative,
        })
        return True

    def get_events_near(self, date_str, window_days=1, calendar_name="Calendar"):
        try:
            target = date.fromisoformat(date_str)
        except ValueError:
            return []
        out = []
        for uid, e in self.events.items():
            try:
                if abs((date.fromisoformat(e["date"]) - target).days) <= window_days:
                    out.append({**e, "calendar_uid": uid, "source": "calendar"})
            except ValueError:
                continue
        return out


def score_pipeline_case(case: dict, model: str, dedup_model: str) -> dict:
    """Run a multi-poll golden case through the REAL pipeline gates: detection
    (real LLM) -> main.process_event -> reconcile (real adjudicator), against
    isolated state and a fake calendar. Scores final create/update counts and,
    optionally, the final calendar event's fields."""
    from scheduling_agent import calendar as calendar_mod, config, main, state as state_mod

    threads = loader.materialize_polls(case)
    expected = loader._resolve_offsets(dict(case["expected_pipeline"]), date.today())
    final_expected = expected.get("final")
    if final_expected:
        final_expected = loader._resolve_offsets(dict(final_expected), date.today())

    fake_calendar = _FakeCalendar()
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"eval-state-{case['id']}-"))
    saved = {
        "STATE_DIR": state_mod.STATE_DIR,
        "STATE_FILE": state_mod.STATE_FILE,
        "create_event": calendar_mod.create_event,
        "update_event": calendar_mod.update_event,
        "get_events_near": calendar_mod.get_events_near,
    }
    state_mod.STATE_DIR = tmp_dir
    state_mod.STATE_FILE = tmp_dir / "state.json"
    calendar_mod.create_event = fake_calendar.create_event
    calendar_mod.update_event = fake_calendar.update_event
    calendar_mod.get_events_near = fake_calendar.get_events_near

    cfg = {**config.DEFAULTS, "dedup_model": dedup_model}
    outcomes: list[str] = []
    try:
        for thread in threads:
            events, _failed = detector.detect_plans(
                [thread], model=model, evidence_gate=cfg["evidence_gate_enabled"]
            )
            for event in events:
                outcomes.append(main.process_event(event, cfg))
    finally:
        state_mod.STATE_DIR = saved["STATE_DIR"]
        state_mod.STATE_FILE = saved["STATE_FILE"]
        calendar_mod.create_event = saved["create_event"]
        calendar_mod.update_event = saved["update_event"]
        calendar_mod.get_events_near = saved["get_events_near"]

    failures: list[str] = []
    if fake_calendar.creates != expected.get("creates", 0):
        failures.append(f"creates {fake_calendar.creates} != {expected.get('creates', 0)}")
    if fake_calendar.updates != expected.get("updates", 0):
        failures.append(f"updates {fake_calendar.updates} != {expected.get('updates', 0)}")
    if final_expected and not failures:
        finals = list(fake_calendar.events.values())
        if not any(not _check_event_fields(final_expected, e) for e in finals):
            failures.append(f"no final calendar event matches {final_expected}; got {finals}")

    return {
        "id": case["id"],
        "category": case.get("category", "pipeline"),
        "known_failure": case.get("known_failure", False),
        "passed": not failures,
        "outcomes": outcomes,
        "creates": fake_calendar.creates,
        "updates": fake_calendar.updates,
        "calendar_events": list(fake_calendar.events.values()),
        "failures": failures,
    }


def run(cases: list[dict], model: str = detector.MODEL, judge: bool = False) -> list[dict]:
    cases = [c for c in cases if "polls" not in c]  # pipeline cases score separately
    results = [score_case(c, model) for c in cases]
    if judge:
        from evals import judge as judge_mod
        for result, case in zip(results, cases):
            if result["passed"] and result["expected_has_event"] and result["got"]:
                thread, _ = loader.materialize_case(case)
                result["title_quality"] = judge_mod.score_title(thread, result["got"]["title"])
    return results


def run_pipeline(cases: list[dict], model: str, dedup_model: str) -> list[dict]:
    return [score_pipeline_case(c, model, dedup_model) for c in cases if "polls" in c]


def summarize(
    results: list[dict],
    dedup_results: list[dict] | None = None,
    pipeline_results: list[dict] | None = None,
) -> dict:
    gated = [r for r in results if not r["known_failure"]]
    negatives = [r for r in results if r["category"] == "hard_negative"]
    tentatives = [r for r in gated if r["category"] == "tentative"]
    bystanders = [r for r in results if r["category"] == "bystander"]
    positives = [r for r in gated if r["expected_has_event"] and r["category"] != "tentative"]
    fps = sum(1 for r in negatives if r["predicted_has_event"])

    conf: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r["confidence"] is not None:
            conf[r["category"]].append(r["confidence"])

    summary = {
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
        # Bystander leaks are third-party plans that would have reached the
        # user's calendar — the ownership-hallucination bug. Gate at zero.
        "bystander_leaks": [r["id"] for r in bystanders if r["predicted_has_event"]],
    }

    if pipeline_results is not None:
        summary["pipeline_accuracy"] = (
            sum(r["passed"] for r in pipeline_results) / len(pipeline_results)
            if pipeline_results else 0.0
        )
        summary["pipeline_failed"] = [r["id"] for r in pipeline_results if not r["passed"]]

    if dedup_results is not None:
        summary["dedup_accuracy"] = (
            sum(r["passed"] for r in dedup_results) / len(dedup_results) if dedup_results else 0.0
        )
        summary["dedup_same_missed"] = [
            r["id"] for r in dedup_results if r["expected_verdict"] == "same" and not r["passed"]
        ]

    return summary


def print_report(
    results: list[dict], summary: dict, model: str,
    dedup_results: list[dict] | None = None,
    pipeline_results: list[dict] | None = None,
) -> None:
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
    if summary.get("bystander_leaks"):
        print(f"  BYSTANDER LEAKS:             {', '.join(summary['bystander_leaks'])}")
    if pipeline_results is not None:
        print(f"\n=== Pipeline eval (multi-poll, real reconcile) ===")
        for r in pipeline_results:
            status = "PASS" if r["passed"] else "FAIL"
            line = f"  [{status}] {r['id']}  creates={r['creates']} updates={r['updates']}"
            if r["failures"]:
                line += "  — " + "; ".join(r["failures"])
            print(line)
        print(f"\n  pipeline accuracy:            {summary['pipeline_accuracy']:.0%}")
    if dedup_results is not None:
        print(f"\n  dedup accuracy:               {summary['dedup_accuracy']:.0%}")
        if summary["dedup_same_missed"]:
            print(f"  dedup 'same' missed:          {', '.join(summary['dedup_same_missed'])}")


def write_report(
    results: list[dict], summary: dict, model: str, run_dir: Path,
    dedup_results: list[dict] | None = None,
    pipeline_results: list[dict] | None = None,
) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "report.json"
    report = {"model": model, "summary": summary, "results": results}
    if dedup_results is not None:
        report["dedup_results"] = dedup_results
    if pipeline_results is not None:
        report["pipeline_results"] = pipeline_results
    path.write_text(json.dumps(report, indent=2))
    return path


class _Tee:
    """Mirrors writes to stdout into a log file for the duration of the run."""

    def __init__(self, log_file):
        self._log_file = log_file
        self._real_stdout = None

    def __enter__(self) -> "_Tee":
        self._real_stdout = sys.stdout
        sys.stdout = self
        return self

    def __exit__(self, *exc_info) -> None:
        sys.stdout = self._real_stdout

    def write(self, data: str) -> None:
        self._real_stdout.write(data)
        self._log_file.write(data)

    def flush(self) -> None:
        self._real_stdout.flush()
        self._log_file.flush()


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the detector eval suite.")
    ap.add_argument("--model", default=detector.MODEL)
    ap.add_argument("--dedup-model", default="claude-haiku-4-5")
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

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = REPORTS_DIR / f"{ts}_{args.model.replace('/', '_')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = run_dir / "stdout.log"

    with stdout_path.open("w") as log_file, _Tee(log_file):
        results = run(cases, model=args.model, judge=args.judge)
        results_by_id = {r["id"]: r for r in results}
        dedup_results = score_dedup_pairs(cases, results_by_id, model=args.dedup_model)
        pipeline_results = run_pipeline(cases, model=args.model, dedup_model=args.dedup_model)
        summary = summarize(results, dedup_results, pipeline_results)
        print_report(results, summary, args.model, dedup_results, pipeline_results)
        path = write_report(results, summary, args.model, run_dir, dedup_results, pipeline_results)
        print(f"\n  report: {path}")
        print(f"  stdout log: {stdout_path}")


if __name__ == "__main__":
    main()

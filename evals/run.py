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
    python -m evals.run --repeat 3               # 3 runs + always_failed/flaky split
    python -m evals.run --diff RUN_A RUN_B       # failure-set diff of two finished runs
    python -m evals.run --run-budget-minutes 30  # bound each run's wall clock
"""

import argparse
import json
import os
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path

from time import monotonic  # `time` itself is datetime.time here

from evals import loader
from scheduling_agent import config, dedup, detector, usage_tracker

LOGS_DIR = Path(__file__).parent.parent / "logs"
REPORTS_DIR = LOGS_DIR / "evals"

# The detector's own eval-clock pin. Pass-fail must not depend on which real
# weekday the run happens on ("this Thursday" resolves differently depending
# on today's weekday — see detect_plans's weekday-reconciliation and
# loader._days_until_weekday), so every run pins "today" to a deterministic
# Wednesday rather than using live wall-clock time. This MUST stay relative to
# the real clock (never a hardcoded calendar date): main.process_event drops
# any event dated before datetime.now(), so a fixed pin would eventually make
# every pipeline case dated relative to it fall into the past and start
# failing. Override with EVAL_TODAY=YYYY-MM-DD for a specific date (e.g. to
# reproduce a bug seen on a particular real day).
_PINNED_WEEKDAY = 2  # Wednesday (date.weekday(): Mon=0 ... Sun=6)


def _default_eval_today() -> date:
    override = os.environ.get("EVAL_TODAY")
    if override:
        return date.fromisoformat(override)
    today = date.today()
    return today + timedelta(days=(_PINNED_WEEKDAY - today.weekday()) % 7)


def _eval_clock(today: date | None = None) -> tuple[date, float]:
    """Returns (today, now) where `now` is a unix timestamp at noon on `today`
    — used both to materialize golden-case message timestamps and as the
    "today" the detector prompt shows the model, so both stay consistent."""
    today = today or _default_eval_today()
    now = datetime.combine(today, time(12, 0)).timestamp()
    return today, now

_GOT_FIELDS = (
    "title", "date", "time_start", "time_confidence", "location",
    "confidence", "status", "user_is_participant", "participation_evidence",
    "recurrence", "end_date", "evidence", "date_evidence",
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


# Statuses that never reach the calendar as a create, mirroring
# main.process_event's gates. "cancelled" isn't a detector output yet (added
# by a later fix in this plan) — included here pre-emptively so scoring
# doesn't need to change again once it is, and so an unrecognized status
# never crashes this check.
_NON_CREATING_STATUSES = ("unanswered", "cancelled")


def _would_reach_calendar(event: dict) -> bool:
    """Mirror the production ownership/status gates: an event only reaches the
    calendar when the user participates and the invitation isn't unanswered
    (or, once implemented, cancelled)."""
    return (
        bool(event.get("user_is_participant"))
        and event.get("status") not in _NON_CREATING_STATUSES
    )


def score_case(case: dict, model: str, today: date | None = None) -> dict:
    today, now = _eval_clock(today)
    thread, expected = loader.materialize_case(case, today=today, now=now)
    events, failed = detector.detect_plans(
        [thread], model=model, today=datetime.combine(today, time(12, 0))
    )

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
        expected_has_event = expected["has_event"]

        if expected_has_event:
            predicted_has_event = got is not None
        else:
            # For a hard_negative, only an event that would actually reach the
            # calendar counts as a false positive. A "cancelled" classification
            # (F4) recognizing that a previously-agreed plan was called off is
            # the correct detection for that case, not junk — it only ever
            # deletes an existing agent-owned record, never creates one.
            reaching = [e for e in events if _would_reach_calendar(e)]
            predicted_has_event = bool(reaching)
            got = reaching[0] if reaching else got

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


def score_dedup_pairs(
    cases: list[dict], results_by_id: dict, model: str, day_window: int = 1,
    deadline: "Deadline | None" = None,
) -> list[dict]:
    """For golden cases annotated with dedup_with/dedup_verdict, treat the
    referenced case's detected event as an "existing calendar event" and run
    the real dedup.find_candidates + dedup.adjudicate against this case's
    detected event(s), scoring the resulting same/different verdict.

    ``day_window`` is threaded through to ``dedup.find_candidates`` so a wider
    (or narrower) candidate window than production's default can be exercised
    without duplicating this function — used by dedup pairs whose two halves
    are deliberately more than a day apart (e.g. a reschedule mention).

    Every result carries a pair-aware ``known_failure``: a dedup pair is
    aspirational if EITHER half is flagged, since a flagged detection on the
    reference side makes the pair's verdict meaningless. Stamping it here (the
    only place with access to `cases`) is what lets `summarize` and
    `test_evals` gate on it without re-deriving the pairing."""
    cases_by_id = {c["id"]: c for c in cases}

    def _pair_known_failure(case: dict, ref_id: str) -> bool:
        ref = cases_by_id.get(ref_id, {})
        return bool(case.get("known_failure") or ref.get("known_failure"))

    dedup_results = []

    pair_cases = [c for c in cases if "dedup_with" in c]
    for i, case in enumerate(pair_cases):
        if deadline is not None and deadline.expired():
            deadline.skip(len(pair_cases) - i)
            print(f"  !! run budget exceeded — skipping {len(pair_cases) - i} dedup pair(s)")
            break

        ref_id = case["dedup_with"]
        expected_verdict = case["dedup_verdict"]
        known_failure = _pair_known_failure(case, ref_id)
        # A truncated detector phase (budget exceeded, or a -k filter that
        # caught one half of a pair) leaves an id unscored — skip the pair
        # rather than KeyError. The run is already being marked invalid.
        if case["id"] not in results_by_id or ref_id not in results_by_id:
            continue
        b_events = results_by_id[case["id"]]["events"]
        a_events = results_by_id[ref_id]["events"]

        if not a_events or not b_events:
            dedup_results.append({
                "id": case["id"], "dedup_with": ref_id, "expected_verdict": expected_verdict,
                "got_verdict": None, "passed": False, "known_failure": known_failure,
                "note": "missing detection on one side of the pair",
            })
            continue

        existing_records = [{**a, "hash": f"eval-{ref_id}", "created_at": "2020-01-01T00:00:00"}
                             for a in a_events]

        called_llm = False
        any_duplicate = False
        reasoning = None
        for b in b_events:
            candidates = dedup.find_candidates(b, existing_records, day_window=day_window)
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
            "known_failure": known_failure,
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
        self.deletes = 0

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

    def delete_event(self, uid, calendar_name="Calendar"):
        self.deletes += 1
        if uid not in self.events:
            return False
        del self.events[uid]
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


def score_pipeline_case(
    case: dict, model: str, dedup_model: str, today: date | None = None
) -> dict:
    """Run a multi-poll golden case through the REAL pipeline gates: detection
    (real LLM) -> main.process_event -> reconcile (real adjudicator), against
    isolated state and a fake calendar. Scores final create/update counts and,
    optionally, the final calendar event's fields."""
    from scheduling_agent import calendar as calendar_mod, config, main, state as state_mod

    today, now = _eval_clock(today)
    threads = loader.materialize_polls(case, today=today, now=now)
    expected = loader._resolve_offsets(dict(case["expected_pipeline"]), today)
    final_expected = expected.get("final")
    if final_expected:
        final_expected = loader._resolve_offsets(dict(final_expected), today)

    fake_calendar = _FakeCalendar()
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"eval-state-{case['id']}-"))
    # calendar.delete_event does not exist yet (added by a later fix) — track
    # whether we're adding a new attribute or overriding one so it can be torn
    # down symmetrically either way.
    had_delete_event = hasattr(calendar_mod, "delete_event")
    saved = {
        "STATE_DIR": state_mod.STATE_DIR,
        "STATE_FILE": state_mod.STATE_FILE,
        "create_event": calendar_mod.create_event,
        "update_event": calendar_mod.update_event,
        "get_events_near": calendar_mod.get_events_near,
        "delete_event": getattr(calendar_mod, "delete_event", None),
    }
    state_mod.STATE_DIR = tmp_dir
    state_mod.STATE_FILE = tmp_dir / "state.json"
    calendar_mod.create_event = fake_calendar.create_event
    calendar_mod.update_event = fake_calendar.update_event
    calendar_mod.get_events_near = fake_calendar.get_events_near
    calendar_mod.delete_event = fake_calendar.delete_event

    cfg = {**config.DEFAULTS, "dedup_model": dedup_model}
    outcomes: list[str] = []
    try:
        for thread in threads:
            events, _failed = detector.detect_plans(
                [thread], model=model, evidence_gate=cfg["evidence_gate_enabled"],
                today=datetime.combine(today, time(12, 0)),
                context_marking_enabled=cfg["context_marking_enabled"],
                date_resolver_enabled=cfg["date_resolver_enabled"],
            )
            for event in events:
                outcomes.append(main.process_event(event, cfg))
    finally:
        state_mod.STATE_DIR = saved["STATE_DIR"]
        state_mod.STATE_FILE = saved["STATE_FILE"]
        calendar_mod.create_event = saved["create_event"]
        calendar_mod.update_event = saved["update_event"]
        calendar_mod.get_events_near = saved["get_events_near"]
        if had_delete_event:
            calendar_mod.delete_event = saved["delete_event"]
        else:
            del calendar_mod.delete_event

    failures: list[str] = []
    if fake_calendar.creates != expected.get("creates", 0):
        failures.append(f"creates {fake_calendar.creates} != {expected.get('creates', 0)}")
    if fake_calendar.updates != expected.get("updates", 0):
        failures.append(f"updates {fake_calendar.updates} != {expected.get('updates', 0)}")
    if fake_calendar.deletes != expected.get("deletes", 0):
        failures.append(f"deletes {fake_calendar.deletes} != {expected.get('deletes', 0)}")
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


def run(
    cases: list[dict], model: str = detector.MODEL, judge: bool = False,
    today: date | None = None, deadline: "Deadline | None" = None,
) -> list[dict]:
    today, now = _eval_clock(today)
    cases = [c for c in cases if "polls" not in c]  # pipeline cases score separately
    results = []
    for i, case in enumerate(cases):
        if deadline is not None and deadline.expired():
            deadline.skip(len(cases) - i)
            print(f"  !! run budget exceeded — skipping {len(cases) - i} detector case(s)")
            break
        results.append(score_case(case, model, today=today))
    cases = cases[:len(results)]  # keep judge's zip() aligned with what ran
    if judge:
        from evals import judge as judge_mod
        for result, case in zip(results, cases):
            if result["passed"] and result["expected_has_event"] and result["got"]:
                thread, _ = loader.materialize_case(case, today=today, now=now)
                result["title_quality"] = judge_mod.score_title(thread, result["got"]["title"])
    return results


def run_pipeline(
    cases: list[dict], model: str, dedup_model: str, today: date | None = None,
    deadline: "Deadline | None" = None,
) -> list[dict]:
    today, _ = _eval_clock(today)
    poll_cases = [c for c in cases if "polls" in c]
    results = []
    for i, case in enumerate(poll_cases):
        if deadline is not None and deadline.expired():
            deadline.skip(len(poll_cases) - i)
            print(f"  !! run budget exceeded — skipping {len(poll_cases) - i} pipeline case(s)")
            break
        results.append(score_pipeline_case(case, model, dedup_model, today=today))
    return results


def summarize(
    results: list[dict],
    dedup_results: list[dict] | None = None,
    pipeline_results: list[dict] | None = None,
) -> dict:
    gated = [r for r in results if not r["known_failure"]]
    # A known_failure hard_negative is an aspirational case tracking a fix not
    # yet implemented (e.g. an ambiguous-anchor case the model isn't expected
    # to resolve correctly yet) — it must not fail the zero-FP gate, so it's
    # excluded the same way it's excluded from `gated` accuracy.
    negatives = [r for r in results if r["category"] == "hard_negative" and not r["known_failure"]]
    known_failure_negatives = [r for r in results if r["category"] == "hard_negative" and r["known_failure"]]
    tentatives = [r for r in gated if r["category"] == "tentative"]
    bystanders = [r for r in results if r["category"] == "bystander"]
    positives = [r for r in gated if r["expected_has_event"] and r["category"] != "tentative"]
    fps = sum(1 for r in negatives if r["predicted_has_event"])
    known_failure_fps = [r["id"] for r in known_failure_negatives if r["predicted_has_event"]]

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
        "known_failure_false_positives": known_failure_fps,
        "n_total": len(results),
        "n_gated": len(gated),
        "n_passed_gated": sum(r["passed"] for r in gated),
        "known_failures": [r["id"] for r in results if r["known_failure"]],
        "mean_confidence_by_category": {k: round(sum(v) / len(v), 3) for k, v in conf.items()},
        # Bystander leaks are third-party plans that would have reached the
        # user's calendar — the ownership-hallucination bug. Gate at zero.
        "bystander_leaks": [r["id"] for r in bystanders if r["predicted_has_event"]],
    }

    # Pipeline and dedup accuracy gate on known_failure exactly the way detector
    # `accuracy` does above: an aspirational case tracking an unimplemented fix
    # must not drag the headline number down. The ungated `*_failed_all` lists
    # stay alongside so --diff can still see a known-failure case flip.
    if pipeline_results is not None:
        pipeline_gated = [r for r in pipeline_results if not r.get("known_failure")]
        summary["pipeline_accuracy"] = (
            sum(r["passed"] for r in pipeline_gated) / len(pipeline_gated)
            if pipeline_gated else 0.0
        )
        summary["n_pipeline_gated"] = len(pipeline_gated)
        summary["pipeline_failed"] = [r["id"] for r in pipeline_gated if not r["passed"]]
        summary["pipeline_failed_all"] = [r["id"] for r in pipeline_results if not r["passed"]]

    if dedup_results is not None:
        dedup_gated = [r for r in dedup_results if not r.get("known_failure")]
        summary["dedup_accuracy"] = (
            sum(r["passed"] for r in dedup_gated) / len(dedup_gated) if dedup_gated else 0.0
        )
        summary["n_dedup_gated"] = len(dedup_gated)
        summary["dedup_same_missed"] = [
            r["id"] for r in dedup_gated if r["expected_verdict"] == "same" and not r["passed"]
        ]
        summary["dedup_failed_all"] = [r["id"] for r in dedup_results if not r["passed"]]

    return summary


def cost_summary(n_cases: int) -> dict:
    """Snapshot of `usage_tracker`'s accumulated cost for this run (call after
    all phases finish, with the tracker reset at the start of the run so it
    covers only this invocation). `cost_per_eval_usd` averages the run's total
    cost over `n_cases` — the golden cases actually run (post -k filtering) —
    not the number of raw API calls, since one case can drive detector +
    dedup + pipeline calls."""
    summary = usage_tracker.summary()
    summary["cost_per_run_usd"] = summary["total_cost_usd"]
    summary["cost_per_eval_usd"] = (
        round(summary["total_cost_usd"] / n_cases, 6) if n_cases else None
    )
    return summary


# A run whose API calls mostly failed measured nothing. Above this share of
# failed calls the numbers are an artifact of the outage, not of the model, and
# must not be compared against anything.
MAX_TOLERABLE_CALL_FAILURE_RATE = 0.10

# Wall-clock ceiling for ONE full pass of the suite. dedup.REQUEST_TIMEOUT_SECONDS
# bounds a single request, but not a run: the SDK retries each call twice by
# default, so one bad call can burn 3x that, and ~200 calls have no collective
# bound at all. Observed: a run sat at 0.2s of CPU per 90s of wall clock for
# 1.5 hours against a degraded API — every individual request was "fine", the
# run as a whole was not. A run that blows this budget is marked invalid rather
# than left to hang, which is the same treatment an outage already gets.
DEFAULT_RUN_BUDGET_MINUTES = 60


class Deadline:
    """Wall-clock budget for one pass of the suite, polled between cases.

    Checked at case boundaries rather than enforced mid-request: a case in
    flight is allowed to finish, so results are never half-written. `minutes=0`
    (or None) disables the budget entirely."""

    def __init__(self, minutes: float | None = DEFAULT_RUN_BUDGET_MINUTES):
        self.limit_seconds = minutes * 60 if minutes else None
        self.started = monotonic()
        self.skipped = 0

    @property
    def elapsed(self) -> float:
        return monotonic() - self.started

    def expired(self) -> bool:
        return self.limit_seconds is not None and self.elapsed > self.limit_seconds

    def skip(self, n: int = 1) -> None:
        self.skipped += n

    @staticmethod
    def _fmt(seconds: float) -> str:
        # Sub-minute budgets are used in tests and quick checks; rounding them
        # to "0 min" makes the message useless.
        return f"{seconds:.0f}s" if seconds < 60 else f"{seconds / 60:.1f} min"

    def reason(self) -> str:
        return (
            f"wall-clock budget exceeded ({self._fmt(self.elapsed)} > "
            f"{self._fmt(self.limit_seconds)}); {self.skipped} case(s) not run"
        )


def run_validity(cost: dict, deadline: "Deadline | None" = None) -> dict:
    """Whether a finished run's numbers mean anything.

    Motivating incident: a `--repeat 3` run exhausted the account's credit
    balance partway through. Its third run made ZERO successful API calls, so
    every case reported "expected an event, none produced" — and the harness
    printed "28% accuracy, 0% false-positive rate" as though that were a
    measurement. Read as a diff against the previous run it looks exactly like
    a catastrophic regression, which is the most expensive way to be wrong."""
    total = cost.get("total_calls", 0)
    failed = cost.get("failed_calls", 0)
    rate = cost.get("call_failure_rate", 0.0)

    # A truncated run scored only part of the suite; its "failures" are just
    # the cases that never ran.
    if deadline is not None and deadline.expired():
        return {"valid": False, "reason": deadline.reason()}
    if total == 0:
        return {"valid": False, "reason":
                f"no API call succeeded ({failed} failed) — the suite never reached the model"}
    if rate > MAX_TOLERABLE_CALL_FAILURE_RATE:
        return {"valid": False, "reason":
                f"{failed} of {total + failed} API calls failed ({rate:.0%}) — "
                "results reflect the outage, not the model"}
    return {"valid": True, "reason": None}


def print_cost_summary(cost: dict) -> None:
    print(f"\n=== Cost ===")
    print(f"  total cost (this run):        ${cost['cost_per_run_usd']:.4f}")
    if cost["cost_per_eval_usd"] is not None:
        print(f"  average cost per eval case:   ${cost['cost_per_eval_usd']:.4f}")
    print(
        f"  total calls: {cost['total_calls']}  "
        f"(input tokens: {cost['total_input_tokens']}, output tokens: {cost['total_output_tokens']})"
    )
    if cost["unpriced_calls"]:
        print(
            f"  WARNING: {cost['unpriced_calls']} call(s) used a model with no known "
            "pricing — total cost is a floor, not exact"
        )
    for model, m in cost["by_model"].items():
        print(f"    {model}: {m['calls']} call(s), ${m['cost_usd']:.4f}")


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
    if summary.get("known_failure_false_positives"):
        print(
            "  known-failure FPs (not gated): "
            f"{', '.join(summary['known_failure_false_positives'])}"
        )
    if summary.get("bystander_leaks"):
        print(f"  BYSTANDER LEAKS:             {', '.join(summary['bystander_leaks'])}")
    if pipeline_results is not None:
        print(f"\n=== Pipeline eval (multi-poll, real reconcile) ===")
        for r in pipeline_results:
            status = "PASS" if r["passed"] else "FAIL"
            flag = " (known-fail)" if r.get("known_failure") else ""
            line = f"  [{status}] {r['id']}{flag}  creates={r['creates']} updates={r['updates']}"
            if r["failures"]:
                line += "  — " + "; ".join(r["failures"])
            print(line)
        print(
            f"\n  pipeline accuracy (excl. known-fail): {summary['pipeline_accuracy']:.0%} "
            f"({summary['n_pipeline_gated'] - len(summary['pipeline_failed'])}/"
            f"{summary['n_pipeline_gated']})"
        )
    if dedup_results is not None:
        print(
            f"\n  dedup accuracy (excl. known-fail):    {summary['dedup_accuracy']:.0%} "
            f"({summary['n_dedup_gated'] - len([r for r in dedup_results if not r.get('known_failure') and not r['passed']])}/"
            f"{summary['n_dedup_gated']})"
        )
        if summary["dedup_same_missed"]:
            print(f"  dedup 'same' missed:          {', '.join(summary['dedup_same_missed'])}")


def write_report(
    results: list[dict], summary: dict, model: str, run_dir: Path,
    dedup_results: list[dict] | None = None,
    pipeline_results: list[dict] | None = None,
    cost: dict | None = None,
) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "report.json"
    report = {"model": model, "summary": summary, "results": results}
    if dedup_results is not None:
        report["dedup_results"] = dedup_results
    if pipeline_results is not None:
        report["pipeline_results"] = pipeline_results
    if cost is not None:
        report["cost"] = cost
    path.write_text(json.dumps(report, indent=2))
    return path


SUITES = ("detector", "pipeline", "dedup")


def load_report(path: Path) -> dict:
    """Load a run's report.json. Accepts either the run directory or the
    report.json path itself, so `--diff` can take the paths printed by a run."""
    path = Path(path)
    if path.is_dir():
        path = path / "report.json"
    return json.loads(path.read_text())


def failure_sets(report: dict) -> dict[str, set[str]]:
    """Per-suite sets of failing case ids, INCLUDING known failures — a diff
    must be able to show a known-failure case flipping to passing, which is the
    whole point of the flag-hygiene rule.

    Tolerant of pre-PR1 reports, which have no `*_failed_all` summary keys: the
    per-case result lists are authoritative when present, and the older summary
    keys are the fallback."""
    summary = report.get("summary", {})
    out: dict[str, set[str]] = {
        "detector": {r["id"] for r in report.get("results", []) if not r["passed"]},
    }
    if "pipeline_results" in report:
        out["pipeline"] = {r["id"] for r in report["pipeline_results"] if not r["passed"]}
    else:
        out["pipeline"] = set(
            summary.get("pipeline_failed_all") or summary.get("pipeline_failed") or []
        )
    if "dedup_results" in report:
        out["dedup"] = {r["id"] for r in report["dedup_results"] if not r["passed"]}
    else:
        out["dedup"] = set(
            summary.get("dedup_failed_all") or summary.get("dedup_same_missed") or []
        )
    return out


def known_failure_ids(report: dict) -> set[str]:
    """Every case id flagged known_failure anywhere in a report. Pre-PR1 dedup
    results carry no flag, so the summary's `known_failures` list backfills."""
    ids = {r["id"] for r in report.get("results", []) if r.get("known_failure")}
    for key in ("pipeline_results", "dedup_results"):
        ids |= {r["id"] for r in report.get(key, []) if r.get("known_failure")}
    ids |= set(report.get("summary", {}).get("known_failures") or [])
    return ids


def aggregate_repeats(reports: list[dict]) -> dict:
    """Split N runs' failures into `always_failed` (the intersection — real
    defects) and `flaky` (union minus intersection — sampling variance). This
    is the yardstick the whole v0.10 plan is judged against; per
    plans/v0.10, compare failure SETS across runs, never single-run accuracy."""
    # Runs that never reached the model measured nothing — folding them in
    # would put every case into `always_failed` and read as total collapse.
    invalid = [i for i, r in enumerate(reports, 1)
               if r.get("summary", {}).get("run_valid") is False]
    valid_reports = [r for r in reports
                     if r.get("summary", {}).get("run_valid") is not False]

    per_run = [failure_sets(r) for r in valid_reports]
    out: dict = {
        "n_runs": len(valid_reports),
        "n_invalid_runs": len(invalid),
        "invalid_runs": invalid,
        "suites": {},
    }
    if not per_run:
        out["suites"] = {s: {"always_failed": [], "flaky": [], "per_run": []} for s in SUITES}
        out["always_failed"] = []
        out["flaky"] = []
        return out

    for suite in SUITES:
        sets = [fs[suite] for fs in per_run]
        inter = set.intersection(*sets) if sets else set()
        union = set().union(*sets) if sets else set()
        out["suites"][suite] = {
            "always_failed": sorted(inter),
            "flaky": sorted(union - inter),
            "per_run": [sorted(s) for s in sets],
        }

    overall = [set().union(*[fs[s] for s in SUITES]) for fs in per_run]
    inter = set.intersection(*overall) if overall else set()
    union = set().union(*overall) if overall else set()
    out["always_failed"] = sorted(inter)
    out["flaky"] = sorted(union - inter)
    return out


def print_repeat_summary(agg: dict) -> None:
    print(f"\n=== Repeat summary ({agg['n_runs']} valid runs) ===")
    if agg.get("n_invalid_runs"):
        print(
            f"  *** {agg['n_invalid_runs']} run(s) EXCLUDED as invalid "
            f"(run{', run'.join(str(i) for i in agg['invalid_runs'])}) — API calls failed; "
            "their numbers measured nothing."
        )
        if not agg["n_runs"]:
            print("  No valid runs — nothing was measured. Fix API access and re-run.")
            return
    for suite in SUITES:
        s = agg["suites"][suite]
        print(f"  {suite}:")
        print(f"    always failed: {', '.join(s['always_failed']) or '(none)'}")
        print(f"    flaky:         {', '.join(s['flaky']) or '(none)'}")
    print(f"\n  ALWAYS FAILED (all suites): {', '.join(agg['always_failed']) or '(none)'}")
    print(f"  FLAKY (all suites):        {', '.join(agg['flaky']) or '(none)'}")


def diff_reports(report_a: dict, report_b: dict) -> dict:
    """Failure-set diff B against A (A = before, B = after). `newly_failing`
    entries that are NOT flagged known_failure in B are regressions and make
    the CLI exit nonzero."""
    for label, report in (("A", report_a), ("B", report_b)):
        if report.get("summary", {}).get("run_valid") is False:
            return {
                "suites": {s: {"newly_failing": [], "newly_passing": [], "still_failing": []}
                           for s in SUITES},
                "regressions": [],
                "invalid": (
                    f"run {label} is an INVALID run "
                    f"({report['summary'].get('invalid_reason')}) — nothing to compare"
                ),
            }

    fa, fb = failure_sets(report_a), failure_sets(report_b)
    known_b = known_failure_ids(report_b)
    out: dict = {"suites": {}, "regressions": []}
    regressions: set[str] = set()
    for suite in SUITES:
        newly_failing = sorted(fb[suite] - fa[suite])
        out["suites"][suite] = {
            "newly_failing": newly_failing,
            "newly_passing": sorted(fa[suite] - fb[suite]),
            "still_failing": sorted(fa[suite] & fb[suite]),
        }
        # A case can fail in two suites at once (a detector miss also sinks its
        # dedup pair) — count it as one regression, not two.
        regressions |= {i for i in newly_failing if i not in known_b}
    out["regressions"] = sorted(regressions)
    return out


def print_diff(diff: dict, label_a: str, label_b: str) -> None:
    print(f"\n=== Failure-set diff ===\n  A (before): {label_a}\n  B (after):  {label_b}")
    if diff.get("invalid"):
        print(f"\n  *** CANNOT DIFF — {diff['invalid']}.")
        return
    for suite in SUITES:
        s = diff["suites"][suite]
        if not any(s.values()):
            continue
        print(f"\n  {suite}:")
        if s["newly_failing"]:
            print(f"    NEWLY FAILING: {', '.join(s['newly_failing'])}")
        if s["newly_passing"]:
            print(f"    newly passing: {', '.join(s['newly_passing'])}")
        if s["still_failing"]:
            print(f"    still failing: {', '.join(s['still_failing'])}")
    if diff["regressions"]:
        print(f"\n  REGRESSIONS (newly failing, not known_failure): {', '.join(diff['regressions'])}")
    else:
        print("\n  No regressions (no newly-failing unflagged cases).")


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


def execute_run(args, cases: list[dict], eval_today: date, run_dir: Path) -> dict:
    """One full pass of every phase, written to `run_dir`. Returns the report
    dict (also persisted as run_dir/report.json)."""
    deadline = Deadline(getattr(args, "run_budget_minutes", DEFAULT_RUN_BUDGET_MINUTES))
    run_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = run_dir / "stdout.log"

    usage_tracker.reset()  # so the run's cost totals don't include prior calls
    with stdout_path.open("w") as log_file, _Tee(log_file):
        print(f"  eval clock pinned to: {eval_today.isoformat()} ({eval_today.strftime('%A')})")
        results = run(cases, model=args.model, judge=args.judge, today=eval_today,
                      deadline=deadline)
        results_by_id = {r["id"]: r for r in results}
        dedup_results = score_dedup_pairs(
            cases, results_by_id, model=args.dedup_model, day_window=args.dedup_day_window,
            deadline=deadline,
        )
        pipeline_results = run_pipeline(
            cases, model=args.model, dedup_model=args.dedup_model, today=eval_today,
            deadline=deadline,
        )
        summary = summarize(results, dedup_results, pipeline_results)
        summary["eval_today"] = eval_today.isoformat()
        print_report(results, summary, args.model, dedup_results, pipeline_results)
        cost = cost_summary(len(cases))
        print_cost_summary(cost)
        validity = run_validity(cost, deadline)
        summary["elapsed_minutes"] = round(deadline.elapsed / 60, 2)
        summary["run_valid"] = validity["valid"]
        summary["invalid_reason"] = validity["reason"]
        if not validity["valid"]:
            print(
                f"\n  *** INVALID RUN — {validity['reason']}.\n"
                "      Accuracy figures above are meaningless; do not diff or "
                "unflag against this run. Fix the API access and re-run."
            )
        path = write_report(
            results, summary, args.model, run_dir, dedup_results, pipeline_results, cost
        )
        print(f"\n  report: {path}")
        print(f"  stdout log: {stdout_path}")

    return {
        "model": args.model, "summary": summary, "results": results,
        "dedup_results": dedup_results, "pipeline_results": pipeline_results, "cost": cost,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the detector eval suite.")
    ap.add_argument("--model", default=detector.MODEL)
    ap.add_argument("--dedup-model", default=config.DEFAULTS["dedup_model"])
    ap.add_argument(
        "--dedup-day-window", type=int, default=config.DEFAULTS["dedup_candidate_day_window"],
        help="candidate window (days) for dedup-pair scoring; matches production's "
             "dedup_candidate_day_window by default",
    )
    ap.add_argument("--judge", action="store_true", help="add LLM title-quality scoring")
    ap.add_argument("-k", "--filter", default=None, help="only run cases whose id contains this")
    ap.add_argument("--golden", default=str(loader.GOLDEN_PATH))
    ap.add_argument(
        "--today", default=None,
        help="pin the eval clock to this ISO date (YYYY-MM-DD) instead of the "
             "default next-Wednesday-from-now; same effect as EVAL_TODAY"
    )
    ap.add_argument(
        "--repeat", type=int, default=1, metavar="N",
        help="run the whole suite N times and write a repeat_summary.json splitting "
             "failures into always_failed (real defects) vs flaky (sampling variance). "
             "N=3 is the plan's baseline protocol — a 2-run baseline misclassifies "
             "1-in-3 flaky cases too often to trust for flag hygiene.",
    )
    ap.add_argument(
        "--run-budget-minutes", type=float, default=DEFAULT_RUN_BUDGET_MINUTES,
        metavar="N",
        help=f"wall-clock ceiling for ONE pass of the suite (default {DEFAULT_RUN_BUDGET_MINUTES}; "
             "0 disables). A run that exceeds it stops at the next case boundary and is "
             "marked invalid, so a degraded API fails in bounded time instead of hanging.",
    )
    ap.add_argument(
        "--diff", nargs=2, metavar=("RUN_A", "RUN_B"), default=None,
        help="compare two finished runs (dir or report.json each) instead of running "
             "the suite; exits nonzero if B has newly-failing cases not flagged "
             "known_failure",
    )
    args = ap.parse_args()

    if args.diff:
        report_a, report_b = load_report(args.diff[0]), load_report(args.diff[1])
        diff = diff_reports(report_a, report_b)
        print_diff(diff, args.diff[0], args.diff[1])
        sys.exit(1 if diff["regressions"] else 0)

    if args.repeat < 1:
        ap.error("--repeat must be >= 1")

    eval_today, _ = _eval_clock(date.fromisoformat(args.today) if args.today else None)

    cases = loader.load_golden(Path(args.golden))
    if args.filter:
        cases = [c for c in cases if args.filter in c["id"]]
    if not cases:
        print("No cases matched.")
        return

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    base_dir = REPORTS_DIR / f"{ts}_{args.model.replace('/', '_')}"

    if args.repeat == 1:
        execute_run(args, cases, eval_today, base_dir)
        return

    reports = []
    for i in range(1, args.repeat + 1):
        print(f"\n########## repeat {i}/{args.repeat} ##########")
        report = execute_run(args, cases, eval_today, base_dir / f"run{i}")
        reports.append(report)
        # An invalid run means the API is down, out of credit, or degraded —
        # none of which the next repeat will fix. Stop rather than spend the
        # remaining runs' time and money producing more unusable reports.
        if report["summary"].get("run_valid") is False and i < args.repeat:
            print(
                f"\n  *** Aborting after repeat {i}/{args.repeat}: "
                f"{report['summary'].get('invalid_reason')}.\n"
                "      Remaining repeats skipped — fix the API access and re-run."
            )
            break

    agg = aggregate_repeats(reports)
    agg["run_dirs"] = [str(base_dir / f"run{i}") for i in range(1, args.repeat + 1)]
    agg["total_cost_usd"] = round(sum(r["cost"]["cost_per_run_usd"] for r in reports), 6)
    print_repeat_summary(agg)
    print(f"\n  total cost across {args.repeat} runs: ${agg['total_cost_usd']:.4f}")
    summary_path = base_dir / "repeat_summary.json"
    summary_path.write_text(json.dumps(agg, indent=2))
    print(f"  repeat summary: {summary_path}")


if __name__ == "__main__":
    main()

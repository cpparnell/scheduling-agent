"""Reconcile newly detected plans against the canonical event store.

The store (plus, optionally, the actual Apple Calendar) is the source of truth:
every detection is matched against known events BEFORE anything touches the
calendar. Matching runs in three layers, cheapest first:

1. exact  — state.is_duplicate (dedup hash + per-chat title window, journal-aware)
2. fuzzy  — deterministic chat-agnostic match: normalized-title token overlap +
            compatible date/time. No LLM call.
3. llm    — dedup.adjudicate over the remaining nearby candidates, including
            events read back from the calendar itself when enabled.

A match yields either "update" (the detection carries material new information:
a reschedule, a newly stated location, a tentative plan now confirmed) or
"skip_duplicate" (nothing new). Only unmatched detections yield "create".
"""

import logging
from dataclasses import dataclass, field
from datetime import date as date_type
from typing import Literal

from . import calendar, dedup, state

logger = logging.getLogger(__name__)

# Timed events whose start times are within this many minutes are considered
# plausibly the same slot by the fuzzy layer (covers small reschedule drift).
TIME_COMPAT_MINUTES = 120

# A matched detection may update the stored record even when slightly less
# confident than it; only a clearly weaker detection is blocked from updating.
UPDATE_CONFIDENCE_TOLERANCE = 0.1


@dataclass
class Decision:
    action: Literal["create", "update", "skip_duplicate", "skip_error"]
    matched: dict | None = None
    changes: dict = field(default_factory=dict)
    source: Literal["exact", "fuzzy", "llm"] | None = None
    reasoning: str | None = None


def _title_tokens(title: str) -> set[str]:
    return set(state._normalize_title(title or "").split())


def _title_similarity(a: str, b: str) -> float:
    """Jaccard overlap of normalized title tokens."""
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _minutes(time_start: str) -> int:
    hours, minutes = time_start.split(":")
    return int(hours) * 60 + int(minutes)


def _time_compatible(a: str | None, b: str | None) -> bool:
    """Same slot heuristic: all-day matches anything; timed events must be
    within TIME_COMPAT_MINUTES of each other."""
    if a is None or b is None:
        return True
    try:
        return abs(_minutes(a) - _minutes(b)) <= TIME_COMPAT_MINUTES
    except ValueError:
        return False


def _dates_within(a: str, b: str, days: int) -> bool:
    try:
        return abs((date_type.fromisoformat(a) - date_type.fromisoformat(b)).days) <= days
    except (TypeError, ValueError):
        return False


def fuzzy_match(event: dict, candidates: list[dict], title_threshold: float) -> dict | None:
    """Deterministic cross-chat match: close date, compatible time, and strong
    normalized-title overlap. Returns the best candidate or None."""
    best = None
    best_score = 0.0
    for cand in candidates:
        if not _dates_within(event["date"], cand.get("date", ""), 1):
            continue
        if not _time_compatible(event.get("time_start"), cand.get("time_start")):
            continue
        score = _title_similarity(event.get("title", ""), cand.get("title", ""))
        if score >= title_threshold and score > best_score:
            best = cand
            best_score = score
    return best


def _assemble_candidates(event: dict, cfg: dict) -> list[dict]:
    """Nearby candidates from the canonical store (incl. pending journal
    records) plus, when enabled, events read back from the target calendar.
    Calendar rows duplicating a store record (same calendar_uid) are dropped."""
    candidates = state.get_events_near(event["date"], cfg["dedup_day_window"])

    if cfg.get("calendar_query_enabled"):
        known_uids = {c.get("calendar_uid") for c in candidates if c.get("calendar_uid")}
        for cal_event in calendar.get_events_near(
            event["date"], cfg["dedup_day_window"], calendar_name=cfg["target_calendar"]
        ):
            if cal_event["calendar_uid"] not in known_uids:
                candidates.append(cal_event)

    return candidates


def _disposition(event: dict, matched: dict, source: str, reasoning: str | None) -> Decision:
    """Decide update vs skip for a matched detection.

    Only canonical store records can be updated; a match against a calendar-only
    row (manually created or from lost state) is always a skip — rewriting an
    event the agent doesn't own based on a text message is too aggressive.
    """
    if matched.get("source") == "calendar" or "canonical_id" not in matched:
        return Decision("skip_duplicate", matched=matched, source=source, reasoning=reasoning)

    # A mention far from the stored date (e.g. a title-window match on a
    # recurring plan weeks out) is a duplicate mention, not a reschedule.
    if not _dates_within(event["date"], matched.get("date", ""), 1):
        return Decision("skip_duplicate", matched=matched, source=source, reasoning=reasoning)

    # A clearly weaker detection never overwrites a stronger record.
    stored_confidence = matched.get("confidence") or 0
    new_confidence = event.get("confidence") or 0
    if new_confidence + UPDATE_CONFIDENCE_TOLERANCE < stored_confidence:
        return Decision("skip_duplicate", matched=matched, source=source, reasoning=reasoning)

    changes: dict = {}
    if event["date"] != matched.get("date") and _dates_within(event["date"], matched.get("date", ""), 1):
        changes["date"] = event["date"]
    if event.get("time_start") is not None and event.get("time_start") != matched.get("time_start"):
        changes["time_start"] = event["time_start"]
    if event.get("location") and not matched.get("location"):
        changes["location"] = event["location"]
    # Status can only move toward confirmed, never away from it.
    if event.get("status") == "confirmed" and matched.get("status") == "tentative":
        changes["status"] = "confirmed"

    if changes:
        return Decision("update", matched=matched, changes=changes, source=source, reasoning=reasoning)
    return Decision("skip_duplicate", matched=matched, source=source, reasoning=reasoning)


def reconcile(event: dict, cfg: dict) -> Decision:
    """Match a detected event (post-gating, post-time-demotion) against the
    canonical store and the calendar. Returns what the caller should do."""
    chat_id = event["chat_id"]
    date = event["date"]
    time_start = event.get("time_start")
    title = event["title"]

    if state.is_duplicate(chat_id, date, time_start, title):
        # Fetch the matched record so material changes (reschedule, status
        # upgrade, new location) can still flow through as updates.
        matched = state.find_record(chat_id, date, time_start, title)
        if matched is not None:
            return _disposition(event, matched, "exact", "exact hash/title-window match")
        return Decision("skip_duplicate", source="exact", reasoning="exact hash/title-window match")

    candidates = _assemble_candidates(event, cfg)
    if not candidates:
        return Decision("create")

    matched = fuzzy_match(event, candidates, cfg["fuzzy_title_threshold"])
    if matched is not None:
        return _disposition(
            event, matched, "fuzzy",
            f"title similarity >= {cfg['fuzzy_title_threshold']} with compatible date/time",
        )

    if not cfg["dedup_enabled"]:
        return Decision("create")

    llm_candidates = dedup.find_candidates(
        {**event, "_hash": state.event_hash(chat_id, date, time_start, title)},
        candidates,
        day_window=cfg["dedup_day_window"],
    )
    if not llm_candidates:
        return Decision("create")

    verdict = dedup.adjudicate(event, llm_candidates, model=cfg["dedup_model"])
    if verdict is None:
        if cfg["dedup_fail_open"]:
            return Decision("create", reasoning="adjudicator failed; fail-open")
        return Decision("skip_error", source="llm", reasoning="adjudicator failed; fail-closed")

    if verdict.get("is_duplicate"):
        duplicate_of = verdict.get("duplicate_of")
        matched = None
        if isinstance(duplicate_of, int) and 0 <= duplicate_of < len(llm_candidates):
            matched = llm_candidates[duplicate_of]
        else:
            logger.warning(
                "Adjudicator returned out-of-range duplicate_of=%r for %d candidates: %s",
                duplicate_of, len(llm_candidates), title,
            )
        if matched is None:
            return Decision("skip_duplicate", source="llm", reasoning=verdict.get("reasoning"))
        return _disposition(event, matched, "llm", verdict.get("reasoning"))

    return Decision("create")

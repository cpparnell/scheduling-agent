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
    relationship: Literal["duplicate", "reschedule", "new_occurrence"] | None = None


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


def fuzzy_match(
    event: dict, candidates: list[dict], title_threshold: float, day_window: int = 1
) -> dict | None:
    """Deterministic cross-chat match: close date, compatible time, and strong
    normalized-title overlap. Returns the best candidate or None. Kept to a
    narrow day_window even when the LLM-facing candidate window is wider —
    this layer has no adjudicator to catch a wrong call."""
    best = None
    best_score = 0.0
    for cand in candidates:
        if not _dates_within(event["date"], cand.get("date", ""), day_window):
            continue
        if not _time_compatible(event.get("time_start"), cand.get("time_start")):
            continue
        score = _title_similarity(event.get("title", ""), cand.get("title", ""))
        if score >= title_threshold and score > best_score:
            best = cand
            best_score = score
    return best


def far_candidates(event: dict, cfg: dict) -> list[dict]:
    """Records whose titles resemble the detection but whose dates are far
    away. Catches a bare weekday mis-resolved to a near-term date when the
    real plan is already recorded months out ("on Friday we..." about the
    October trip). Same-chat records need only weak title overlap; a
    different-chat record needs a stronger tie since title alone is a weaker
    signal across conversations (catches a plan set in a group chat then
    rescheduled or re-mentioned in a 1:1). The verdict is always left to the
    LLM adjudicator — a same-title far date can also be a genuinely new
    occurrence of a recurring plan."""
    event_hash = state.event_hash(
        event["chat_id"], event["date"], event.get("time_start"), event["title"]
    )
    scored = []
    for record in state.get_active_events():
        if record.get("hash") == event_hash:
            continue
        if _dates_within(event["date"], record.get("date", ""), cfg["dedup_candidate_day_window"]):
            continue  # near dates are the near layer's job
        same_chat = record.get("chat_id") == event["chat_id"]
        threshold = cfg["far_title_similarity"] if same_chat else cfg["far_title_similarity_cross_chat"]
        score = _title_similarity(event.get("title", ""), record.get("title", ""))
        if score >= threshold:
            scored.append((score, record))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [record for _, record in scored[:dedup.MAX_CANDIDATES]]


def _assemble_candidates(event: dict, cfg: dict) -> list[dict]:
    """Nearby candidates from the canonical store (incl. pending journal
    records) plus, when enabled, events read back from the target calendar.
    Calendar rows duplicating a store record (same calendar_uid) are dropped."""
    candidates = state.get_events_near(event["date"], cfg["dedup_candidate_day_window"])

    if cfg.get("calendar_query_enabled"):
        known_uids = {c.get("calendar_uid") for c in candidates if c.get("calendar_uid")}
        for cal_event in calendar.get_events_near(
            event["date"], cfg["dedup_candidate_day_window"], calendar_name=cfg["target_calendar"]
        ):
            if cal_event["calendar_uid"] not in known_uids:
                candidates.append(cal_event)

    return candidates


def _disposition(
    event: dict, matched: dict, source: str, reasoning: str | None,
    relationship: str = "duplicate", cfg: dict | None = None,
) -> Decision:
    """Decide create/update/skip for a matched detection.

    Only canonical store records can be updated; a match against a calendar-only
    row (manually created or from lost state) is always a skip — rewriting an
    event the agent doesn't own based on a text message is too aggressive.

    `relationship` (set by the LLM adjudicator; deterministic layers only ever
    see near-date matches and stay at the default "duplicate") distinguishes
    three cases the matched record can stand in for:
    - "duplicate": the same occurrence, possibly re-worded or mis-dated —
      never move the record's date more than a day for this relationship
      (a far "duplicate" is a mis-dated re-mention; the stored date is the
      authoritative one, so don't let the wrong new date overwrite it).
    - "reschedule": the group explicitly moved the plan to a new date/time —
      the record's date is deliberately updated, bounded by
      reschedule_max_days and (for moves >1 day) requiring the new detection
      to be status=confirmed, so a tentative maybe-reschedule can't silently
      relocate a confirmed plan.
    - "new_occurrence": same recurring activity, but a genuinely distinct
      future instance — never merged into the matched record; create.
    """
    if matched.get("source") == "calendar" or "canonical_id" not in matched:
        return Decision("skip_duplicate", matched=matched, source=source, reasoning=reasoning)

    if relationship == "new_occurrence":
        return Decision("create", reasoning=reasoning)

    # A clearly weaker detection never overwrites a stronger record.
    stored_confidence = matched.get("confidence") or 0
    new_confidence = event.get("confidence") or 0
    if new_confidence + UPDATE_CONFIDENCE_TOLERANCE < stored_confidence:
        return Decision("skip_duplicate", matched=matched, source=source, reasoning=reasoning)

    try:
        delta_days = abs(
            (date_type.fromisoformat(event["date"]) - date_type.fromisoformat(matched.get("date", ""))).days
        )
    except (TypeError, ValueError):
        delta_days = None

    if relationship == "reschedule":
        cfg = cfg or {}
        max_days = cfg.get("reschedule_max_days", 30)
        if delta_days is None or delta_days > max_days:
            return Decision("skip_duplicate", matched=matched, source=source, reasoning=reasoning)
        if delta_days > 1 and event.get("status") != "confirmed":
            return Decision("skip_duplicate", matched=matched, source=source, reasoning=reasoning)
        if delta_days > 1:
            logger.warning(
                "Reschedule moving %r by %d day(s): %s -> %s (%s)",
                matched.get("title"), delta_days, matched.get("date"), event["date"], reasoning,
            )
        changes: dict = {}
        if event["date"] != matched.get("date"):
            changes["date"] = event["date"]
    else:  # "duplicate"
        # A mention far from the stored date (e.g. a title-window or
        # far-candidate match) is a duplicate mention, not a reschedule —
        # the stored date stands.
        near_days = (cfg or {}).get("dedup_day_window", 1)
        if delta_days is None or delta_days > near_days:
            return Decision("skip_duplicate", matched=matched, source=source, reasoning=reasoning)
        changes = {}
        if event["date"] != matched.get("date"):
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
            if _dates_within(date, matched.get("date", ""), cfg["dedup_day_window"]):
                return _disposition(event, matched, "exact", "exact hash/title-window match", cfg=cfg)
            # A title-window match whose date is more than a day off the
            # matched record isn't necessarily the same mention — it could be
            # a genuine reschedule, a new occurrence of a recurring plan, or a
            # mis-dated re-mention. Deterministic logic can't tell those
            # apart; let the adjudicator decide instead of silently skipping
            # (previously this always skipped with no chance for the record
            # to be recognized as a reschedule, and could shadow a genuinely
            # new occurrence of a recurring plan).
            return _reconcile_far_exact_match(event, matched, cfg)
        # is_duplicate() said yes (a hash/title-window key exists) but no
        # committed record matches it — the record was pruned, or the key
        # belongs to a suppressed duplicate. Don't silently drop the
        # detection with nothing to reconcile against; fall through and let
        # the normal candidate assembly (fuzzy/LLM) have a shot at it.
        logger.info(
            "is_duplicate matched no record for %r on %s (chat %s) — falling through to candidates",
            title, date, chat_id,
        )

    candidates = _assemble_candidates(event, cfg)

    if candidates:
        matched = fuzzy_match(event, candidates, cfg["fuzzy_title_threshold"], day_window=cfg["dedup_day_window"])
        if matched is not None:
            return _disposition(
                event, matched, "fuzzy",
                f"title similarity >= {cfg['fuzzy_title_threshold']} with compatible date/time",
                cfg=cfg,
            )

    if not cfg["dedup_enabled"]:
        return Decision("create")

    llm_candidates = dedup.find_candidates(
        {**event, "_hash": state.event_hash(chat_id, date, time_start, title)},
        candidates,
        day_window=cfg["dedup_candidate_day_window"],
    )
    if not llm_candidates:
        # Nothing near the detected date — check for a record with a similar
        # title far away (a mis-resolved bare weekday lands here).
        llm_candidates = far_candidates(event, cfg)
    if not llm_candidates:
        return Decision("create")

    return _adjudicate(event, llm_candidates, cfg)


def _reconcile_far_exact_match(event: dict, matched: dict, cfg: dict) -> Decision:
    """An exact title-window match whose date is far from the new detection.
    Deterministic logic can't tell a mis-dated re-mention from a genuine
    reschedule from a new occurrence of a recurring plan — send it to the
    adjudicator with the single matched record as the candidate."""
    if not cfg["dedup_enabled"]:
        return Decision(
            "skip_duplicate", matched=matched, source="exact",
            reasoning="far title-window match (dedup disabled; treated as duplicate)",
        )
    return _adjudicate(event, [matched], cfg)


def _adjudicate(event: dict, llm_candidates: list[dict], cfg: dict) -> Decision:
    verdict = dedup.adjudicate(event, llm_candidates, model=cfg["dedup_model"])
    if verdict is None:
        if cfg["dedup_fail_open"]:
            return Decision("create", reasoning="adjudicator failed; fail-open")
        return Decision("skip_error", source="llm", reasoning="adjudicator failed; fail-closed")

    if not verdict.get("is_duplicate"):
        return Decision("create")

    duplicate_of = verdict.get("duplicate_of")
    matched = None
    if isinstance(duplicate_of, int) and 0 <= duplicate_of < len(llm_candidates):
        matched = llm_candidates[duplicate_of]
    else:
        logger.warning(
            "Adjudicator returned out-of-range duplicate_of=%r for %d candidates: %s",
            duplicate_of, len(llm_candidates), event.get("title"),
        )
    if matched is None:
        return Decision("skip_duplicate", source="llm", reasoning=verdict.get("reasoning"))

    relationship = verdict.get("relationship") or "duplicate"
    if relationship not in ("duplicate", "reschedule", "new_occurrence"):
        relationship = "duplicate"
    return _disposition(event, matched, "llm", verdict.get("reasoning"), relationship=relationship, cfg=cfg)

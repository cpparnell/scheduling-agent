"""Jev detector backend: plan detection driven by TypeSafe's System One model,
with Claude Haiku as a fallback extractor.

Per thread, one Jev request answers every judgement in parallel:

  gate     has_plan, new_info, multiple_plans, participant (noul)
           status (choice: confirmed/tentative/unanswered/declined/cancelled/no_plan)
  slots    date / time / location / activity (choice over the deterministic
           candidates from candidates.py, plus "none")

Then one of three paths:

  skip      Jev is confident there's no new plan here. No LLM call at all.
            This is the common case for ordinary chatter.
  fast      Every judgement clears its threshold, so the event is assembled
            in code from Jev's picks. No LLM call.
  fallback  Anything uncertain or out of the fast path's scope (several
            plans, a multi-day span, a cancellation, a date no candidate
            covers) goes to the existing Haiku detector for that one thread.

The returned events have the same shape as detector.detect_plans, so the
downstream gates in main.py treat both backends identically.
"""

import json
import logging
import re
from collections import Counter
from datetime import date as _date, datetime

from typesafe_sdk import Choice, Noul

from scheduling_agent import candidates as candidates_mod
from scheduling_agent import detector, jev_client

logger = logging.getLogger(__name__)

# Defaults for config.DEFAULTS["jev_thresholds"]; a user config may override
# any subset.
DEFAULT_THRESHOLDS = {
    # Skip (no event, no LLM) when P(a plan is proposed) is below this. Kept
    # low: a wrong skip silently loses a real plan, while a wrong pass only
    # costs one Haiku call.
    "skip_plan_below": 0.2,
    # Same, for P(a NEW message adds scheduling info) on threads that replay
    # already-processed context.
    "skip_new_info_below": 0.2,
    # Fast path requires at least this P(a plan is proposed).
    "fast_plan_min": 0.85,
    # Fast path requires each choice (status, date, time) to be at least this
    # confident; below it, the thread falls back to Haiku.
    "fast_choice_min": 0.75,
    # Fast path requires P(participant) to be this decisive either way.
    "fast_participant_margin": 0.85,
    # Fast path is single-plan only.
    "fast_multi_plan_max": 0.3,
    # A low-confidence activity pick only makes a worse title, never a wrong
    # event, so the bar is lower than for the other slots.
    "fast_title_min": 0.4,
    # Below this, a location pick is dropped (event created without one)
    # rather than forcing a fallback.
    "location_min": 0.6,
}

# Per-path counters for observability. The eval harness resets these at the
# start of a run and reports them, so a high fallback rate (the candidate
# generator is the bottleneck) is visible in every report.
STATS: Counter = Counter()


def reset_stats() -> None:
    STATS.clear()


_STATUS_CRITERIA = {
    "confirmed": "The user ('Me') is attending with clear agreement: Me accepted an "
                 "invitation (a reply or a Loved/Liked tapback), or Me proposed the plan "
                 "and someone accepted.",
    "tentative": "Me was invited and explicitly hedged ('maybe', 'I'll try', 'let me "
                 "check'), or Me proposed the plan and every reply so far is a hedge.",
    "unanswered": "A specific invitation Me has not responded to at all, or a plan "
                  "others arranged among themselves while Me sent nothing about it.",
    "declined": "Me turned down an invitation they had never agreed to.",
    "cancelled": "A plan Me had already agreed to is explicitly called off in a new "
                 "message.",
    "no_plan": "There is no specific invitation: vague talk ('we should hang out "
               "sometime'), only a past event, or a proposal superseded by a reschedule "
               "request with no new date agreed.",
}

_HAS_PLAN = Noul(
    instructions="Do these messages propose a specific plan: an activity on a reasonably "
                 "specific date or day?",
    criteria={
        "true": "Someone explicitly invites or proposes an activity for a specific day "
                "('dinner Friday?', 'coffee tomorrow at 10').",
        "false": "Vague intentions ('we should hang out sometime'), only past events, "
                 "or no plan at all.",
    },
)
_NEW_INFO = Noul(
    instructions="Does at least one message with already_processed=false add "
                 "scheduling-relevant information: a proposal, an acceptance or decline, "
                 "a change to the date, time or place, or a cancellation?",
    criteria={
        "true": "A new message proposes, answers, changes, or cancels a plan.",
        "false": "New messages only react ('so excited!!', 'lol', an emoji) or are "
                 "unrelated; everything about the plan is in already-processed messages.",
    },
)
_MULTIPLE_PLANS = Noul(
    instructions="Do these messages arrange more than one distinct plan (for example "
                 "'dinner then the game' is two plans)?",
)
_PARTICIPANT = Noul(
    instructions="Is the user ('Me') personally expected to attend the plan: Me proposed "
                 "it, was invited or addressed, or clearly included themselves?",
    criteria={
        "true": "Me proposed it, was invited or addressed ('you in?'), or included "
                "themselves, even if Me hasn't replied yet.",
        "false": "The plan belongs to someone else: a friend describing their own plans, "
                 "someone else's event, a group plan Me was never addressed in, or one "
                 "Me declined.",
    },
)
_STATUS = Choice(
    instructions="What is the user's ('Me') status for the plan?",
    criteria=_STATUS_CRITERIA,
)


def _sender(msg: dict) -> str:
    return "Me" if msg.get("from_me") else (msg.get("sender") or "Them")


def build_state(thread: dict, today: datetime | None, context_marking_enabled: bool) -> dict:
    now = today or datetime.now()
    messages = thread.get("messages", [])
    mark = context_marking_enabled and any(m.get("is_context") for m in messages)
    rendered = []
    for i, m in enumerate(messages):
        entry = {
            "index": i,
            "sender": _sender(m),
            "sent": datetime.fromtimestamp(m.get("unix_ts", 0)).strftime("%a %m/%d/%Y %I:%M%p"),
            "text": m.get("text", ""),
        }
        if mark:
            entry["already_processed"] = bool(m.get("is_context"))
        rendered.append(entry)
    return {
        "today": now.strftime("%A, %B %d, %Y"),
        "user": "Me",
        "participants": thread.get("participants", []),
        "messages": rendered,
    }


def _date_label(c: dict) -> str:
    d = _date.fromisoformat(c["value"])
    return f"{d.strftime('%A, %B')} {d.day}, {d.year}: from '{c['phrase']}' in message {c['msg_index']}"


def _slot_question(instructions: str, cands: list[dict], describe) -> Choice:
    criteria = {c["value"]: describe(c) for c in cands}
    criteria["none"] = "None of the other options."
    return Choice(instructions=instructions, criteria=criteria)


def build_questions(cands: dict, has_context: bool) -> dict:
    questions = {
        "has_plan": _HAS_PLAN,
        "multiple_plans": _MULTIPLE_PLANS,
        "participant": _PARTICIPANT,
        "status": _STATUS,
    }
    if has_context:
        questions["new_info"] = _NEW_INFO
    if cands["dates"]:
        questions["date"] = _slot_question(
            "On which date does the plan happen? For a plan being called off, the date "
            "it was scheduled for. 'none' if no option is the plan's date.",
            cands["dates"], _date_label,
        )
    if cands["times"]:
        questions["time"] = _slot_question(
            "At what clock time does the plan start? 'none' if no specific start time "
            "was stated and agreed.",
            cands["times"],
            lambda c: f"{c['value']} (24h): from '{c['phrase']}' in message {c['msg_index']}",
        )
    if cands["locations"]:
        questions["location"] = _slot_question(
            "Where does the plan take place? 'none' if no venue or place is given for it.",
            cands["locations"],
            lambda c: f"'{c['value']}': from '{c['phrase']}' in message {c['msg_index']}",
        )
    if cands["activities"]:
        questions["activity"] = _slot_question(
            "Which phrase best names the planned activity?",
            cands["activities"],
            lambda c: f"'{c['value']}': from message {c['msg_index']}",
        )
    return questions


def _choice(resp, name: str) -> tuple[str | None, float]:
    answer = resp.choices.get(name)
    return (answer.choice, answer.confidence) if answer else (None, 0.0)


def _noul(resp, name: str, default: float = 0.0) -> float:
    answer = resp.nouls.get(name)
    return answer.noul if answer else default


# Cancel or change-of-plan language in a NEW message. Either one alters an
# existing event, the most expensive thing to get wrong, so Jev never settles
# such a thread alone, neither by skipping it nor on the fast path. Skipping
# is the one unrecoverable path (no second look), and Jev has been seen
# calling a cancellation buried in chatter "confirmed" (0.48-0.65).
# Over-inclusive on purpose: a false match costs one Haiku call.
_CHANGE_RE = re.compile(
    r"\b(?:instead|reschedule|push(?:ed)? (?:it|back|to)|move(?:d)? (?:it|to)|change of plans|"
    r"switch(?:ed)? to|different (?:day|time)|"
    r"cancel(?:led|ing|ling)?|call(?:ing)? it off|(?:can['’]?t|cannot|won['’]?t|not gonna|not going to) "
    r"(?:make it|do|go|come)|can no longer|never ?mind|nvm|bail(?:ing)?|rain ?check|"
    r"something came up|(?:it['’]?s|is) off|not happening|have to skip|gonna skip)\b",
    re.IGNORECASE,
)


def _new_message_changes_plan(thread: dict) -> bool:
    return any(_CHANGE_RE.search(m.get("text") or "") for m in detector._new_messages(thread))

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'-]{0,40}$")


def _title(activity: str, location: str | None, thread: dict) -> str:
    title = activity[0].upper() + activity[1:]
    participants = thread.get("participants", [])
    # Only a 1:1 with a real contact name gets "with X"; phone numbers and
    # emails make worse titles than none.
    if len(participants) == 1 and _NAME_RE.match(participants[0] or ""):
        return f"{title} with {participants[0].split()[0]}"
    if location:
        return f"{title} at {location}"
    return title


def _find(cands: list[dict], value: str) -> dict | None:
    return next((c for c in cands if c["value"] == value), None)


def decide(resp, cands: dict, thread: dict, th: dict) -> tuple[str, dict | None, str]:
    """Map Jev's answers to (path, event, reason). `path` is "skip", "fast",
    or "fallback"; `event` is set only for "fast"."""
    # Before any skip: a new message that cancels or changes a plan set in
    # earlier context always gets a Haiku look (see _CHANGE_RE).
    has_context = any(m.get("is_context") for m in thread.get("messages", []))
    if has_context and _new_message_changes_plan(thread):
        return "fallback", None, "cancel/change language in a new message"

    has_plan = _noul(resp, "has_plan")
    if has_plan < th["skip_plan_below"]:
        return "skip", None, f"has_plan={has_plan:.2f}"
    if "new_info" in resp.nouls and resp.nouls["new_info"].noul < th["skip_new_info_below"]:
        return "skip", None, f"new_info={resp.nouls['new_info'].noul:.2f}"

    status, status_conf = _choice(resp, "status")
    if status in ("declined", "no_plan") and status_conf >= th["fast_choice_min"]:
        return "skip", None, f"status={status} ({status_conf:.2f})"

    if has_plan < th["fast_plan_min"]:
        return "fallback", None, f"has_plan={has_plan:.2f} below fast bar"
    if _noul(resp, "multiple_plans") > th["fast_multi_plan_max"]:
        return "fallback", None, "multiple plans"
    if status not in ("confirmed", "tentative", "unanswered") or status_conf < th["fast_choice_min"]:
        return "fallback", None, f"status={status} ({status_conf:.2f})"
    participant = _noul(resp, "participant", 0.5)
    if 1 - th["fast_participant_margin"] < participant < th["fast_participant_margin"]:
        return "fallback", None, f"participant={participant:.2f} undecided"

    date_value, date_conf = _choice(resp, "date")
    if not date_value or date_value == "none" or date_conf < th["fast_choice_min"]:
        return "fallback", None, f"date={date_value} ({date_conf:.2f})"
    time_value, time_conf = _choice(resp, "time")
    if time_value is not None and time_conf < th["fast_choice_min"]:
        return "fallback", None, f"time={time_value} ({time_conf:.2f})"
    activity, activity_conf = _choice(resp, "activity")
    if not activity or activity == "none" or activity_conf < th["fast_title_min"]:
        return "fallback", None, f"activity={activity} ({activity_conf:.2f})"

    messages = thread.get("messages", [])
    if any(detector._MULTI_DAY_SIGNAL_RE.search(m.get("text") or "") for m in messages):
        return "fallback", None, "multi-day language"

    if _new_message_changes_plan(thread):
        return "fallback", None, "cancel/change language"

    location, location_conf = _choice(resp, "location")
    if location == "none" or location_conf < th["location_min"]:
        location = None
    time_start = None if time_value in (None, "none") else time_value

    date_cand = _find(cands["dates"], date_value)
    evidence = messages[date_cand["msg_index"]].get("text", "") if date_cand else ""
    event = {
        "title": _title(activity, location, thread),
        "date": date_value,
        "time_start": time_start,
        "time_confidence": time_conf if time_start else None,
        "duration_minutes": None,
        "location": location,
        "confidence": has_plan,
        "status": status,
        "user_is_participant": participant >= 0.5,
        "participation_evidence": f"Jev P(participant)={participant:.2f}",
        "recurrence": cands["recurrence"],
        "end_date": None,
        "evidence": evidence,
        "date_evidence": evidence,
    }
    return "fast", event, "all judgements cleared thresholds"


def _summary(resp) -> dict:
    out = {name: round(a.noul, 3) for name, a in resp.nouls.items()}
    out.update({name: [a.choice, round(a.confidence, 3)] for name, a in resp.choices.items()})
    return out


def _fallback(thread: dict, **kwargs) -> tuple[list[dict], set]:
    events, failed = detector._detect_plans_claude([thread], **kwargs)
    for e in events:
        e["_backend"] = "jev+haiku"
    return events, failed


def detect_plans(
    threads: list[dict],
    model: str = detector.MODEL,
    evidence_gate: bool = True,
    today: datetime | None = None,
    context_marking_enabled: bool = True,
    date_resolver_enabled: bool = True,
    thresholds: dict | None = None,
) -> tuple[list[dict], set]:
    """Same contract as detector.detect_plans: (events, failed_chat_ids).
    `model` is the Haiku model used on the fallback path."""
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    fallback_kwargs = dict(
        model=model, evidence_gate=evidence_gate, today=today,
        context_marking_enabled=context_marking_enabled,
        date_resolver_enabled=date_resolver_enabled,
    )
    results: list[dict] = []
    failed_chat_ids: set = set()

    for thread in threads:
        chat_id = thread.get("chat_id")
        STATS["threads"] += 1
        cands = candidates_mod.generate(thread)
        has_context = context_marking_enabled and any(
            m.get("is_context") for m in thread.get("messages", [])
        )
        try:
            resp = jev_client.ask(
                build_state(thread, today, context_marking_enabled),
                build_questions(cands, has_context),
            )
        except Exception as e:
            # Degrade to the Claude detector rather than dropping the thread.
            # jev_client already recorded the failure for run-validity checks.
            STATS["jev_errors"] += 1
            logger.warning("Jev request failed for thread %s (%s); falling back to Haiku", chat_id, e)
            events, failed = _fallback(thread, **fallback_kwargs)
            results.extend(events)
            failed_chat_ids |= failed
            continue

        path, event, reason = decide(resp, cands, thread, th)
        STATS[path] += 1
        logger.info(
            "jev_decision %s",
            json.dumps({"chat_id": chat_id, "path": path, "reason": reason,
                        "answers": _summary(resp)}, default=str),
        )

        if path == "skip":
            logger.info("  -> No plan detected (Jev: %s)", reason)
            continue
        if path == "fallback":
            events, failed = _fallback(thread, **fallback_kwargs)
            results.extend(events)
            failed_chat_ids |= failed
            continue

        detector._demote_if_user_silent(event, thread)
        event["chat_id"] = chat_id
        event["_new_msg_from_user"] = detector._new_message_from_user(thread)
        event["_backend"] = "jev"
        logger.info(
            "  -> Detected %s plan: %s on %s (Jev, confidence %.2f)",
            event["status"], event["title"], event["date"], event["confidence"],
        )
        results.append(event)

    return results, failed_chat_ids

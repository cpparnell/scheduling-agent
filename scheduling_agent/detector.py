import json
import logging
import re
from datetime import datetime

import anthropic

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"

_client = None


def _get_client() -> "anthropic.Anthropic":
    """Lazily construct the Anthropic client so importing this module does not
    require ANTHROPIC_API_KEY (and so tests can swap in a fake)."""
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


SYSTEM_PROMPT = """You are an assistant that analyzes iMessage conversation threads to identify scheduled plans.

A thread may contain zero, one, or several DISTINCT plans (e.g. "dinner then the game"
is two plans). Return one entry in `events` for each distinct plan, and an empty
`events` array when there are none. Never split a single plan into multiple entries,
and never invent a plan that no message explicitly proposes.

Plans fall into two categories — set `status` accordingly:

**confirmed**: An explicit invitation with a specific date AND all responding parties have explicitly accepted.
Acceptance includes: "yes!", "sounds good", "I'll be there", "see you then", "k", "I'm down", "sure", "why not", "!!", 👍, or similar clear agreement.
Tapback reactions also count: "❤️ Loved your message" or "👍 Liked your message" or "‼️ Emphasized your message" in response to a scheduling message signals acceptance. "👎 Disliked your message" signals rejection.

**tentative**: An explicit invitation with a specific date, but acceptance is incomplete or uncertain.
This includes: no response yet, mixed responses (some yes, some maybe), "maybe", "I'll try", "hopefully", "we'll see", or any hedged/conditional reply from any party.

Do NOT emit a plan when:
- No specific invitation exists ("we should hang out sometime")
- The user explicitly declined or the plan was cancelled
- No reasonably specific date is mentioned
- The thread only references a past event

**Evidence**: For every plan, set `evidence` to a verbatim quote of the single message
that most clearly establishes it (the invitation or the agreement). If you cannot
point to a specific message, do not emit the plan.

**Times**: Set `time_start` ONLY when a specific clock time is stated in the messages
("7pm", "at 5:30", "noon"). If the time is vague ("morning", "after work", "evening")
or absent, set `time_start` to null — the event will be created as an all-day event.
Set `time_confidence` to how certain you are the plan starts exactly at `time_start`
(1.0 = explicitly stated and agreed; lower if inferred). Null when time_start is null.

**Relative dates**: Each message is prefixed with the date/time it was SENT. Resolve
"tomorrow", "tonight", "this Saturday" etc. relative to the SEND time of the message
containing them, NOT relative to today. A message sent 3 days ago saying "tomorrow"
means 2 days ago.

**Recurring events**: If the plan repeats on a pattern, set `recurrence`:
- "every Monday", "weekly standup", "every week" → "weekly"
- "every day", "daily" → "daily"
- "every other week", "biweekly" → "biweekly"
- "every month", "monthly" → "monthly"
- One-time event → null

**Multi-day events**: If the plan spans multiple days (trips, conferences, festivals), set `end_date` to the ISO 8601 last day of the event. For single-day events, set `end_date` to null.

Respond with a JSON object only. No prose.
"""

EVENT_ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "date": {
            "type": ["string", "null"],
            "description": "ISO 8601 date (YYYY-MM-DD) or null if no specific date"
        },
        "time_start": {
            "type": ["string", "null"],
            "description": "HH:MM in 24h format, or null if no specific time"
        },
        "time_confidence": {
            "type": ["number", "null"],
            "description": "0.0-1.0 confidence that the plan starts exactly at time_start; null when time_start is null"
        },
        "duration_minutes": {
            "type": ["integer", "null"],
            "description": "Duration in minutes, or null if unknown (default to 60)"
        },
        "location": {
            "type": ["string", "null"],
            "description": "Location or venue, or null if unspecified"
        },
        "confidence": {
            "type": "number",
            "description": "Confidence score 0.0-1.0 that this is a genuine plan"
        },
        "status": {
            "type": "string",
            "enum": ["confirmed", "tentative"],
            "description": "confirmed if the user explicitly accepted; tentative if the invite exists but user hasn't clearly responded"
        },
        "recurrence": {
            "anyOf": [
                {"type": "string", "enum": ["daily", "weekly", "biweekly", "monthly"]},
                {"type": "null"}
            ],
            "description": "Recurrence pattern for repeating events, or null for one-time events"
        },
        "end_date": {
            "type": ["string", "null"],
            "description": "ISO 8601 last date (YYYY-MM-DD) for multi-day events, or null for single-day"
        },
        "evidence": {
            "type": "string",
            "description": "Verbatim quote of the single message that most clearly establishes this plan"
        },
    },
    "required": [
        "title", "date", "time_start", "time_confidence", "duration_minutes",
        "location", "confidence", "status", "recurrence", "end_date", "evidence",
    ],
}

RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "events": {"type": "array", "items": EVENT_ITEM_SCHEMA},
    },
    "required": ["events"],
}

# Kept for backward compatibility with anything still referencing the old
# single-object schema name.
EVENT_SCHEMA = RESPONSE_SCHEMA


def _format_thread(thread: dict, today: datetime | None = None) -> str:
    now = today or datetime.now()
    today_str = now.strftime("%A, %B %d, %Y")
    participants = ", ".join(thread.get("participants", ["unknown"]))
    lines = [f"[Today is {today_str}]", f"[Participants: {participants}]", ""]
    for msg in thread.get("messages", []):
        sender = "Me" if msg.get("from_me") else msg.get("sender", "Them")
        sent_at = datetime.fromtimestamp(msg.get("unix_ts", 0))
        ts = sent_at.strftime("%m/%d %I:%M%p")
        age_days = (now.date() - sent_at.date()).days
        age_suffix = f", sent {age_days} day{'s' if age_days != 1 else ''} ago" if age_days >= 1 else ""
        lines.append(f"{sender} ({ts}{age_suffix}): {msg['text']}")
    return "\n".join(lines)


def _normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _evidence_found(evidence: str, thread: dict) -> bool:
    haystack = _normalize_for_match(
        " ".join(msg.get("text", "") for msg in thread.get("messages", []))
    )
    return _normalize_for_match(evidence) in haystack


def detect_plans(threads: list[dict], model: str = MODEL) -> tuple[list[dict], set]:
    """
    Analyze a list of conversation threads for plans.

    Returns (events, failed_chat_ids): events is a list of event dicts across
    all threads (a single thread may contribute zero, one, or several), and
    failed_chat_ids is the set of chat_ids whose API call errored or returned
    an unparseable response, so the caller can hold the watermark back.
    """
    results = []
    failed_chat_ids = set()

    for thread in threads:
        formatted = _format_thread(thread)
        participants = ", ".join(thread.get("participants", ["unknown"]))
        n_msgs = len(thread.get("messages", []))
        logger.info(
            "Analyzing thread %s with %s (%d message%s)",
            thread["chat_id"],
            participants,
            n_msgs,
            "s" if n_msgs != 1 else "",
        )

        try:
            response = _get_client().messages.create(
                model=model,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": f"Analyze this iMessage thread for plans:\n\n{formatted}"
                }],
                output_config={"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}}
            )

            text = next(
                (b.text for b in response.content if b.type == "text"),
                None
            )
            if not text:
                continue

            payload = json.loads(text)

            # Legacy single-object shape (has_event/date at the top level)
            # from an older cached payload or an off-spec model response.
            if "events" not in payload and "has_event" in payload:
                events = [payload] if payload.get("has_event") and payload.get("date") else []
            else:
                events = payload.get("events", [])

            if not events:
                logger.info("  -> No plan detected")
                continue

            for event in events:
                if not event.get("date"):
                    continue

                evidence = event.get("evidence")
                if evidence and not _evidence_found(evidence, thread):
                    logger.warning(
                        "  -> Evidence not found verbatim in thread %s: %r",
                        thread["chat_id"], evidence,
                    )

                logger.info(
                    "  -> Detected %s plan: %s on %s (confidence %.2f)",
                    event.get("status", "confirmed"),
                    event.get("title"),
                    event.get("date"),
                    event.get("confidence", 0),
                )
                event["chat_id"] = thread["chat_id"]
                results.append(event)

        except Exception as e:
            # One malformed response, API error, or unexpected payload must not
            # abort the whole batch — log, remember the failure, and move on.
            logger.warning("Error detecting plans in thread %s: %s", thread.get("chat_id"), e)
            failed_chat_ids.add(thread.get("chat_id"))

    return results, failed_chat_ids

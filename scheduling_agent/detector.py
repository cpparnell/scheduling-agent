import json
import logging
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

Plans fall into two categories — set `status` accordingly:

**confirmed**: An explicit invitation with a specific date AND all responding parties have explicitly accepted.
Acceptance includes: "yes!", "sounds good", "I'll be there", "see you then", "k", "I'm down", "sure", "why not", "!!", 👍, or similar clear agreement.
Tapback reactions also count: "❤️ Loved your message" or "👍 Liked your message" or "‼️ Emphasized your message" in response to a scheduling message signals acceptance. "👎 Disliked your message" signals rejection.

**tentative**: An explicit invitation with a specific date, but acceptance is incomplete or uncertain.
This includes: no response yet, mixed responses (some yes, some maybe), "maybe", "I'll try", "hopefully", "we'll see", or any hedged/conditional reply from any party.

Set `has_event: true` for BOTH confirmed and tentative plans.
Set `has_event: false` when:
- No specific invitation exists ("we should hang out sometime")
- The user explicitly declined or the plan was cancelled
- No reasonably specific date is mentioned
- The thread only references a past event

**Recurring events**: If the plan repeats on a pattern, set `recurrence`:
- "every Monday", "weekly standup", "every week" → "weekly"
- "every day", "daily" → "daily"
- "every other week", "biweekly" → "biweekly"
- "every month", "monthly" → "monthly"
- One-time event → null

**Multi-day events**: If the plan spans multiple days (trips, conferences, festivals), set `end_date` to the ISO 8601 last day of the event. For single-day events, set `end_date` to null.

Respond with a JSON object only. No prose.
"""

EVENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "has_event": {"type": "boolean"},
        "title": {"type": "string"},
        "date": {
            "type": ["string", "null"],
            "description": "ISO 8601 date (YYYY-MM-DD) or null if no specific date"
        },
        "time_start": {
            "type": ["string", "null"],
            "description": "HH:MM in 24h format, or null if no specific time"
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
        }
    },
    "required": ["has_event", "title", "date", "time_start", "duration_minutes", "location", "confidence", "status", "recurrence", "end_date"]
}


def _format_thread(thread: dict, today: datetime | None = None) -> str:
    today = (today or datetime.now()).strftime("%A, %B %d, %Y")
def _format_thread(thread: dict, today: datetime | None = None) -> str:
    today = (today or datetime.now()).strftime("%A, %B %d, %Y")
    participants = ", ".join(thread.get("participants", ["unknown"]))
    lines = [f"[Today is {today}]", f"[Participants: {participants}]", ""]
    for msg in thread.get("messages", []):
        sender = "Me" if msg.get("from_me") else msg.get("sender", "Them")
        ts = datetime.fromtimestamp(msg.get("unix_ts", 0)).strftime("%m/%d %I:%M%p")
        lines.append(f"{sender} ({ts}): {msg['text']}")
    return "\n".join(lines)


def detect_plans(threads: list[dict], model: str = MODEL) -> list[dict]:
def detect_plans(threads: list[dict], model: str = MODEL) -> list[dict]:
    """
    Analyze a list of conversation threads for confirmed plans.
    Returns a list of event dicts for threads that have confirmed plans.
    """
    results = []

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
                max_tokens=512,
                system=SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": f"Analyze this iMessage thread for confirmed plans:\n\n{formatted}"
                }],
                output_config={"format": {"type": "json_schema", "schema": EVENT_SCHEMA}}
            )

            text = next(
                (b.text for b in response.content if b.type == "text"),
                None
            )
            if not text:
                continue

            event = json.loads(text)
            if event.get("has_event") and event.get("date"):
                logger.info(
                    "  -> Detected %s plan: %s on %s (confidence %.2f)",
                    event.get("status", "confirmed"),
                    event.get("title"),
                    event.get("date"),
                    event.get("confidence", 0),
                )
                event["chat_id"] = thread["chat_id"]
                results.append(event)
            else:
                logger.info("  -> No plan detected")

        except Exception as e:
            # One malformed response, API error, or unexpected payload must not
            # abort the whole batch — log and move on to the next thread.
            logger.warning("Error detecting plans in thread %s: %s", thread.get("chat_id"), e)

    return results

import json
import logging
from datetime import datetime

import anthropic

logger = logging.getLogger(__name__)

_client = anthropic.Anthropic()

SYSTEM_PROMPT = """You are an assistant that analyzes iMessage conversation threads to determine if confirmed plans have been made.

A "confirmed plan" requires ALL of the following:
1. An explicit invitation to do something at a specific time or date (e.g. "want to grab dinner Friday?", "meet at 3pm?", "let's do lunch Tuesday")
2. An explicit acceptance from the other party (e.g. "yes!", "sounds good", "I'll be there", "see you then")
3. A reasonably specific date (day of week, date, or relative like "this Saturday")

Do NOT create events for:
- Vague expressions of interest ("we should hang out sometime")
- Unconfirmed invitations with no response yet
- Plans that were cancelled or rescheduled without confirmation
- Casual references to past events
- Plans where acceptance is ambiguous

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
            "description": "Confidence score 0.0-1.0 that this is a genuine confirmed plan"
        }
    },
    "required": ["has_event", "title", "date", "time_start", "duration_minutes", "location", "confidence"]
}


def _format_thread(thread: dict) -> str:
    today = datetime.now().strftime("%A, %B %d, %Y")
    participants = ", ".join(thread.get("participants", ["unknown"]))
    lines = [f"[Today is {today}]", f"[Participants: {participants}]", ""]
    for msg in thread.get("messages", []):
        sender = "Me" if msg.get("from_me") else msg.get("sender", "Them")
        ts = datetime.fromtimestamp(msg.get("unix_ts", 0)).strftime("%m/%d %I:%M%p")
        lines.append(f"{sender} ({ts}): {msg['text']}")
    return "\n".join(lines)


def detect_plans(threads: list[dict]) -> list[dict]:
    """
    Analyze a list of conversation threads for confirmed plans.
    Returns a list of event dicts for threads that have confirmed plans.
    """
    results = []

    for thread in threads:
        formatted = _format_thread(thread)

        try:
            response = _client.messages.create(
                model="claude-haiku-4-5-20251001",
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
                event["chat_id"] = thread["chat_id"]
                results.append(event)

        except (json.JSONDecodeError, anthropic.APIError) as e:
            logger.warning("Error detecting plans in thread %s: %s", thread.get("chat_id"), e)

    return results

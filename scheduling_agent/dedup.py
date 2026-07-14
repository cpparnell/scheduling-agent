import json
import logging
from datetime import date as date_type

import anthropic

logger = logging.getLogger(__name__)

_client = None


def _get_client() -> "anthropic.Anthropic":
    """Lazily construct the Anthropic client so importing this module does not
    require ANTHROPIC_API_KEY (and so tests can swap in a fake)."""
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


SYSTEM_PROMPT = """You decide whether a newly detected plan is the SAME real-world plan as an event
already on the calendar, or a different plan.

The same plan often appears twice with different wording ("gym at 7am" vs "morning
workout session at 7"), sometimes in different conversations, sometimes with small
time drift after a reschedule (7:00 vs 7:30). Different plans can legitimately share
a date and even a time — lunch with mom and a work call can both be at noon on the
same day, in different conversations.

Judge by: whether the conversations/participants overlap, whether titles and
locations describe the same activity, whether the times are identical or plausibly
the same slot, and what the quoted evidence messages say. When genuinely uncertain,
answer is_duplicate=false — an occasional duplicate on the calendar is safer than
silently dropping a real plan.

Respond with JSON only.
"""

ADJUDICATOR_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "is_duplicate": {"type": "boolean"},
        "duplicate_of": {
            "type": ["integer", "null"],
            "description": "index (0-based) of the matching existing event in the list provided, or null",
        },
        "reasoning": {"type": "string"},
    },
    "required": ["is_duplicate", "duplicate_of", "reasoning"],
}

# Never adjudicate against more than this many candidates in one call.
MAX_CANDIDATES = 5


def find_candidates(event: dict, existing: list[dict], day_window: int = 1) -> list[dict]:
    """Code-side filter: existing records within +/- day_window of the new
    event's date, excluding exact-hash matches (already handled upstream by
    state.is_duplicate). Returns at most MAX_CANDIDATES, newest first."""
    try:
        target = date_type.fromisoformat(event["date"])
    except (KeyError, ValueError, TypeError):
        return []

    event_hash = event.get("_hash")

    matches = []
    for record in existing:
        if event_hash is not None and record.get("hash") == event_hash:
            continue
        try:
            record_date = date_type.fromisoformat(record["date"])
        except (KeyError, ValueError, TypeError):
            continue
        if abs((record_date - target).days) <= day_window:
            matches.append(record)

    matches.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return matches[:MAX_CANDIDATES]


def _format_candidates(candidates: list[dict]) -> str:
    lines = []
    for i, c in enumerate(candidates):
        lines.append(
            f"{i}. title={c.get('title')!r} date={c.get('date')} time={c.get('time_start')} "
            f"location={c.get('location')!r} chat_id={c.get('chat_id')} status={c.get('status')} "
            f"evidence={c.get('evidence')!r}"
        )
    return "\n".join(lines)


def _format_new_plan(event: dict) -> str:
    return (
        f"title={event.get('title')!r} date={event.get('date')} time={event.get('time_start')} "
        f"location={event.get('location')!r} chat_id={event.get('chat_id')} status={event.get('status')} "
        f"evidence={event.get('evidence')!r}"
    )


def adjudicate(event: dict, candidates: list[dict], model: str) -> dict | None:
    """One structured-output call deciding whether `event` duplicates one of
    `candidates`. Returns the parsed verdict dict, or None on any error (the
    caller applies its own fail-open/fail-closed policy)."""
    if not candidates:
        return None

    prompt = (
        f"NEW PLAN:\n{_format_new_plan(event)}\n\n"
        f"EXISTING CALENDAR EVENTS:\n{_format_candidates(candidates)}"
    )

    try:
        response = _get_client().messages.create(
            model=model,
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": ADJUDICATOR_SCHEMA}},
        )
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            return None
        return json.loads(text)
    except Exception as e:
        logger.warning("Dedup adjudication failed for %r: %s", event.get("title"), e)
        return None

"""Optional LLM-as-judge for the one fuzzy field the programmatic scorer can't
check well: calendar-title quality. Returns an integer 1-5. Reported, never
gating. The judge model defaults to a stronger model than the system under test.
"""

import json

import anthropic

JUDGE_MODEL = "claude-opus-4-8"

_client = None

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "score": {"type": "integer", "description": "1 (poor) to 5 (excellent)"},
        "reason": {"type": "string"},
    },
    "required": ["score", "reason"],
}


def _get_client() -> "anthropic.Anthropic":
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def score_title(thread: dict, title: str, model: str = JUDGE_MODEL) -> int | None:
    convo = "\n".join(
        f"{'Me' if m.get('from_me') else 'Them'}: {m['text']}" for m in thread["messages"]
    )
    prompt = (
        "A scheduling assistant extracted a calendar event title from a text conversation.\n\n"
        f"Conversation:\n{convo}\n\n"
        f"Proposed title: {title!r}\n\n"
        "Rate 1-5 how good this title is for a calendar event a glance weeks later "
        "(5 = clear and specific; 1 = vague, wrong, or unhelpful)."
    )
    try:
        resp = _get_client().messages.create(
            model=model,
            max_tokens=256,
            system="You grade calendar event titles. Respond with JSON only.",
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        )
        text = next((b.text for b in resp.content if b.type == "text"), None)
        return json.loads(text)["score"] if text else None
    except (json.JSONDecodeError, KeyError, anthropic.APIError):
        return None

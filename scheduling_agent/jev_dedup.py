"""Jev dedup adjudicator: the System One counterpart to dedup's Sonnet call.

Adjudication is already a pure classification over known options (which
existing event, if any, is this new plan, and how it relates), which is
exactly Jev's shape: one `same_as` choice over the candidates plus "new", and
one `relationship` choice. Returns the same verdict dict as
dedup._call_adjudicator, so reconcile.py handles both backends identically.
"""

from typesafe_sdk import Choice

from scheduling_agent import jev_client

# Create a new event only when Jev is at least this sure the plan is new.
# Mirrors the Claude prompt's bias: when uncertain, call it the same plan. A
# missed duplicate spams the calendar; a wrong merge only folds a mention into
# an existing event.
NEW_MIN = 0.6
# A reschedule or new occurrence changes when an event happens, so it needs
# this much confidence; anything less defaults to "duplicate", the answer
# that never moves an event.
RELATIONSHIP_MIN = 0.6

_FIELDS = ("title", "date", "time_start", "location", "chat_id", "status", "evidence")

_RELATIONSHIP = Choice(
    instructions="If the new plan is the same real-world plan as an existing event, how "
                 "does it relate to that event?",
    criteria={
        "duplicate": "The same occurrence: a rewording, a mis-dated mention, or a plain "
                     "re-mention with no material change.",
        "reschedule": "The group explicitly moved this same plan to a new date or time "
                      "('can we push it to Saturday?' / 'sure').",
        "new_occurrence": "A recurring activity's next, distinct instance ('next month's "
                          "book club'), not a change to the existing event.",
    },
)


def _describe(event: dict) -> dict:
    return {k: event.get(k) for k in _FIELDS}


def adjudicate_once(event: dict, candidates: list[dict]) -> dict:
    """One Jev request. Raises on transport errors (dedup.adjudicate owns the
    retry and fail-open policy)."""
    labels = [f"event_{i}" for i in range(len(candidates))]
    criteria = {
        label: f"Existing event {i}: {c.get('title')!r} on {c.get('date')}"
        for i, (label, c) in enumerate(zip(labels, candidates))
    }
    criteria["new"] = "None of them: the new plan is a different real-world plan."
    questions = {
        "same_as": Choice(
            instructions="Is the new plan the same real-world plan as one of the existing "
                         "calendar events? The same plan is often worded differently, "
                         "mentioned in a different conversation, or shifted in time. "
                         "Different plans can share a date and even a time.",
            criteria=criteria,
        ),
        "relationship": _RELATIONSHIP,
    }
    state = {
        "new_plan": _describe(event),
        "existing_events": [_describe(c) for c in candidates],
    }
    resp = jev_client.ask(state, questions)

    same = resp.choices["same_as"]
    p_new = same.probabilities.get("new", 0.0)
    is_duplicate = p_new < NEW_MIN
    duplicate_of = None
    if is_duplicate:
        best = max(labels, key=lambda label: same.probabilities.get(label, 0.0))
        duplicate_of = labels.index(best)

    rel = resp.choices["relationship"]
    relationship = rel.choice if rel.confidence >= RELATIONSHIP_MIN else "duplicate"

    return {
        "is_duplicate": is_duplicate,
        "duplicate_of": duplicate_of,
        "relationship": relationship,
        "confidence": round(1 - p_new if is_duplicate else p_new, 3),
        "reasoning": f"Jev P(new)={p_new:.2f}, relationship={rel.choice} ({rel.confidence:.2f})",
    }

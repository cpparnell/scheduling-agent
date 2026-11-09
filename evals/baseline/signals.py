"""Cheap, deliberately over-inclusive "this message might be about a plan"
signal. It decides which chat-days without any v0.14 detection still get
queued for labeling, so the corpus can measure *missed* Commitments. False
positives only cost labeling time (the labeler marks the episode empty);
false negatives make misses invisible, so err toward matching."""

import re

from scheduling_agent.datepatterns import ANCHOR_PATTERN

_WEEKDAY = re.compile(
    r"(?i)\b(?:mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?|sun)(?:day)?s?\b"
)
_TIME = re.compile(
    r"(?i)\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b|\b\d{1,2}:\d{2}\b|\b(?:noon|midnight)\b"
)
_RELATIVE = re.compile(
    r"(?i)\b(?:tonight|tmrw|tmr|tomorrow|this weekend|next week(?:end)?|later today|"
    r"this (?:morning|afternoon|evening)|in (?:a|two|three|\d+) (?:days?|weeks?))\b"
)
_PLAN_WORDS = re.compile(
    r"(?i)\b(?:dinner|lunch|brunch|breakfast|drinks?|coffee|party|tickets?|"
    r"reservations?|reserved|booked|booking|confirmed|confirmation|appointment|appt|"
    r"concert|show|game|trip|flight|hotel|airbnb|wedding|birthday|bday|hang(?:out)?|"
    r"come over|wanna|want to|let'?s|down to|you down|are you free|free on|rsvp|"
    r"meet(?:ing)? up|see you|pick you up|plans?)\b"
)


def plan_signal(text: str) -> bool:
    """True when the text names a date/time or plan vocabulary."""
    return bool(
        ANCHOR_PATTERN.search(text)
        or _WEEKDAY.search(text)
        or _TIME.search(text)
        or _RELATIVE.search(text)
        or _PLAN_WORDS.search(text)
    )

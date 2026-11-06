"""Deterministic resolution of relative date phrases against a message's send date.

The detector's model writes a plan's date itself, and its calendar arithmetic is
unreliable even when told the date (see detector._reconcile_weekday). Temporal-
normalization research finds the same thing for LLMs generally: letting the
model *name* the expression and code *compute* the date beats having the model
output the date directly. So the model also reports the verbatim phrase it
resolved (`date_phrase`), and this module turns that phrase into the dates it
can mean, anchored to the send date of the message it appeared in.

A phrase can have one reading ("tomorrow"), or several that people genuinely
disagree on (a weekday named on that same weekday, "next weekend" said
mid-week). `Resolution.ambiguous` marks the second kind, so the event can carry a
note instead of committing silently. "next <weekday>" is deliberately NOT treated
as ambiguous: people split on it in studies, but in this user's texts it means
next week's, so it resolves deterministically (see next_weekday).

Only phrases whose meaning depends on nothing but the send date are handled. A
bare weekday ("Friday") is deliberately NOT given a single reading: it may be
anchored to an event weeks away (see the detector's bare-weekday rules), and
overriding it from the send date alone would undo that.
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta

from scheduling_agent.datepatterns import EXPLICIT_DATE_RE

_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
_WEEKDAY_ALT = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))

_DAY_AFTER_TOMORROW_RE = re.compile(r"\bday after (?:tomorrow|tmrw|tmr)\b", re.IGNORECASE)
_TOMORROW_RE = re.compile(r"\b(?:tomorrow|tmrw|tmr|tmrow|tomorow)\b", re.IGNORECASE)
_TODAY_RE = re.compile(
    r"\b(?:today|tonight|tonite|this (?:morning|afternoon|evening)|later today)\b", re.IGNORECASE
)
_IN_DAYS_RE = re.compile(r"\bin (\d{1,2}) days?\b", re.IGNORECASE)
_NEXT_WEEKEND_RE = re.compile(r"\bnext weekend\b", re.IGNORECASE)
_THIS_WEEKEND_RE = re.compile(r"\b(?:this|the) weekend\b", re.IGNORECASE)
_WEEKDAY_RE = re.compile(
    rf"\b(?:(this coming|this|next|coming|on|last)\s+)?({_WEEKDAY_ALT})\b", re.IGNORECASE
)


@dataclass
class Resolution:
    """The dates a phrase can mean. `ambiguous` is True when those readings are
    genuinely different plans' dates (not just the two days of one weekend);
    `overridable` is True only when there is exactly one reading and it depends
    on nothing but the send date, so a model date outside it is safe to correct."""
    readings: list[date]
    ambiguous: bool = False
    overridable: bool = False
    phrase: str = ""


def _weekend_of(d: date) -> date:
    """Saturday of the weekend on or after `d` (`d` itself when it's a Saturday,
    the day before when it's a Sunday)."""
    if d.weekday() == 6:
        return d - timedelta(days=1)
    return d + timedelta(days=(5 - d.weekday()) % 7)


def next_weekday(sent: date, weekday: int) -> date:
    """What "next <weekday>" means when said on `sent`: that weekday in the
    following week, with weeks starting on Sunday (the US convention) —
    "next Saturday" said on a Sunday is 13 days out, since "this Saturday"
    is the one 6 days out. `weekday` uses date.weekday() numbering
    (Mon=0 ... Sun=6)."""
    this_sunday = sent - timedelta(days=(sent.weekday() + 1) % 7)
    return this_sunday + timedelta(days=7 + (weekday + 1) % 7)


def resolve_phrase(phrase: str, sent: date) -> Resolution | None:
    """Resolve a relative date phrase against `sent`. Returns None for phrases
    this module doesn't handle (explicit dates, "last Friday", vague phrases
    like "sometime next week"), so the caller leaves the model's date alone."""
    if not phrase or EXPLICIT_DATE_RE.search(phrase):
        return None

    if _DAY_AFTER_TOMORROW_RE.search(phrase):
        return Resolution([sent + timedelta(days=2)], overridable=True, phrase=phrase)
    if _TOMORROW_RE.search(phrase):
        return Resolution([sent + timedelta(days=1)], overridable=True, phrase=phrase)
    if _TODAY_RE.search(phrase):
        return Resolution([sent], overridable=True, phrase=phrase)
    m = _IN_DAYS_RE.search(phrase)
    if m:
        return Resolution([sent + timedelta(days=int(m.group(1)))], overridable=True, phrase=phrase)

    if _NEXT_WEEKEND_RE.search(phrase):
        sat = _weekend_of(sent)
        upcoming = [sat, sat + timedelta(days=1)]
        following = [sat + timedelta(days=7), sat + timedelta(days=8)]
        # Said mid-week, "next weekend" may mean the coming one or the one
        # after; said ON a weekend it can only mean the following one.
        if sent.weekday() >= 5:
            return Resolution(following, phrase=phrase)
        return Resolution(following + upcoming, ambiguous=True, phrase=phrase)
    if _THIS_WEEKEND_RE.search(phrase):
        sat = _weekend_of(sent)
        days = [d for d in (sat, sat + timedelta(days=1)) if d >= sent]
        return Resolution(days, phrase=phrase)

    m = _WEEKDAY_RE.search(phrase)
    if m:
        qualifier = (m.group(1) or "").lower()
        if qualifier == "last":
            return None
        target = _WEEKDAYS[m.group(2).lower()]
        ahead = (target - sent.weekday()) % 7
        nearest = sent + timedelta(days=ahead)
        if qualifier == "next":
            # Deterministic, never ambiguous: "next <weekday>" is that weekday
            # in the week after the one the message was sent in, even when
            # this week's is still ahead ("next Saturday" said on a Wednesday
            # is 10 days out, not 3). See next_weekday.
            return Resolution([next_weekday(sent, target)], overridable=True, phrase=phrase)
        if qualifier in ("this", "this coming", "coming"):
            # "this Thursday" said on a Thursday is that day (README:
            # bare-weekday anchoring); otherwise the coming occurrence.
            return Resolution([nearest], overridable=True, phrase=phrase)
        # Bare or "on <weekday>": may be anchored to a far-off event, so never
        # overridable. Said on that same weekday it is genuinely two-way.
        if ahead == 0:
            return Resolution([nearest, nearest + timedelta(days=7)], ambiguous=True, phrase=phrase)
        return Resolution([nearest], phrase=phrase)

    return None


def ambiguity_note(res: Resolution, chosen: date, sent: date) -> str | None:
    """A one-line calendar note explaining an ambiguous date, or None when the
    resolution isn't ambiguous or offers no alternative to the chosen date."""
    if not res.ambiguous:
        return None
    alternatives = {d for d in res.readings if d != chosen}
    if chosen.weekday() >= 5:
        # A weekend phrase: the other day of the chosen weekend isn't a
        # different plan, and the other weekend is named by its Saturday.
        alternatives = {
            _weekend_of(d) for d in alternatives if _weekend_of(d) != _weekend_of(chosen)
        }
    alternatives = sorted(alternatives)
    if not alternatives:
        return None
    alts = " or ".join(d.strftime("%a %b %-d") for d in alternatives)
    return (
        f"Date inferred from “{res.phrase.strip()}” (sent {sent.strftime('%a %b %-d')}); "
        f"could also be {alts}."
    )

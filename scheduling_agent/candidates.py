"""Deterministic candidate generation for the Jev detector backend.

Jev (TypeSafe's System One model) can't write a value — it can only pick one
of the options it's offered. So this module does the reading: it scans a
thread and proposes every date, time, location, and activity a plan could
plausibly be about, and jev_detector asks Jev which one applies.

Generation is deliberately over-inclusive. An extra candidate costs Jev a few
tokens and gets a low probability; a missing one caps recall, since Jev can't
choose an answer it was never offered. Where a phrase is genuinely ambiguous
("Friday" said on a Friday, a bare "at 7") every reading is offered and Jev
resolves it from context.

Each candidate is a dict: {"value": str, "phrase": str, "msg_index": int}.
Candidates are de-duplicated by value, keeping the first mention.
"""

import re
from datetime import date, datetime, timedelta

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}

_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WEEKDAY_ALT = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))

# "October 10", "Oct 10th", "Tuesday, June 17". Lowercase "may" is excluded
# ("you may want to") — only a capitalized May is read as the month.
_MONTH_DAY_RE = re.compile(
    rf"\b(?:(?i:{_WEEKDAY_ALT}),?\s+)?((?i:(?!may\b)(?:{_MONTH_ALT}))|May)\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b"
)
# "10th of October"
_DAY_OF_MONTH_RE = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+of\s+((?i:(?!may\b)(?:{_MONTH_ALT}))|May)\b"
)
# "10/14", "10/14/26", "10/14/2026" — US month/day. Slash only: a dash is too
# often a range ("3-5pm") and a dot too often a decimal.
_NUMERIC_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?\b")
# "the 14th" — an ordinal day with no month.
_ORDINAL_RE = re.compile(r"\b(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b", re.IGNORECASE)
_RELATIVE_RE = re.compile(
    r"\b(day after tomorrow|tomorrow|tmrw|tmr|today|tonight|this (?:morning|afternoon|evening)"
    r"|this weekend|next weekend|in (\d{1,2}) days)\b",
    re.IGNORECASE,
)
_WEEKDAY_RE = re.compile(rf"\b(?:(this|next|last|on)\s+)?({_WEEKDAY_ALT})\b", re.IGNORECASE)

_CLOCK_RE = re.compile(
    r"\b(\d{1,2})(?::([0-5]\d))?\s*(a\.?m\.?|p\.?m\.?|a|p)(?![a-z])", re.IGNORECASE
)
_NAMED_TIME_RE = re.compile(r"\b(noon|midday|midnight)\b", re.IGNORECASE)
# "at 7", "@ 7:30", "around 8" — a clock time with no am/pm. Not followed by a
# slash or more digits, so a date ("at 10/14") isn't misread as a time.
_BARE_TIME_RE = re.compile(
    r"(?:\bat|@|\baround|\bby|\buntil|\btill)\s+(\d{1,2})(?::([0-5]\d))?(?![\d/:]|\s*(?:a\.?m|p\.?m|a\b|p\b))",
    re.IGNORECASE,
)

# A bare hour in change-of-time phrasing: "can we do 8 instead?", "make it
# 8", "push to 8:30", "8 works". Without these, a reschedule's new time is
# never offered and Jev can only re-pick the old one.
_CHANGE_TIME_RE = re.compile(
    r"(?:\b(?:do|make it|push(?: it)? to|move(?: it)? to|say|how about|what about|change(?: it)? to)\s+"
    r"(\d{1,2})(?::([0-5]\d))?\b(?![\d/:]|\s*(?:a\.?m|p\.?m|a\b|p\b))"
    r"|\b(\d{1,2})(?::([0-5]\d))?\s+(?:instead|works|work|is better|is fine|then)\b)",
    re.IGNORECASE,
)

# "5:30" with no am/pm and no "at" — only with a colon, so a bare number
# ("2 of us") is never read as a time.
_COLON_TIME_RE = re.compile(r"(?<![\d/:])\b(\d{1,2}):([0-5]\d)\b(?!\s*(?:a\.?m|p\.?m|a\b|p\b))", re.IGNORECASE)

_PLACE_WORD = r"[A-Z][\w'’&.-]*"
_LOCATION_RE = re.compile(
    rf"(?:\bat|@|\bin)\s+((?:the\s+)?{_PLACE_WORD}(?:\s+(?:{_PLACE_WORD}|of|on|the|&))*)"
)
_PRIVATE_PLACE_RE = re.compile(
    r"(?:\bat|@)\s+((?:my|our|your|his|her|their)\s+(?:place|house|apartment|apt|parents'?)"
    r"|[a-z]+'s\s+(?:place|house|apartment|apt))\b",
    re.IGNORECASE,
)
# Lowercase venue names ("pizza at diceys") — one word, optionally after
# "the". Over-inclusive on purpose; Jev answers "none" for "at least".
_LOWER_PLACE_RE = re.compile(r"(?:\bat|@)\s+((?:the\s+)?[a-z][\w'’]+)\b")
_LOWER_PLACE_STOPWORDS = {
    "least", "all", "most", "first", "last", "some", "any", "night", "noon", "midnight",
    "home", "work", "it", "this", "that", "my", "our", "your", "his", "her", "their",
    "a", "an", "around", "about", "like", "once", "best", "worst", "times", "lunch",
    "dinner", "breakfast", "brunch", "tonight", "tomorrow", "today", "school",
}
_LOCATION_STOPWORDS = (
    set(_MONTHS) | set(_WEEKDAYS)
    | {"i", "me", "the", "noon", "midnight", "tonight", "tomorrow", "today", "lol", "ok", "omg"}
)

# Keyword -> canonical activity label used as the event title stem.
_ACTIVITIES = {
    "dinner": "dinner", "lunch": "lunch", "brunch": "brunch", "breakfast": "breakfast",
    "coffee": "coffee", "drinks": "drinks", "drink": "drinks", "beers": "drinks",
    "beer": "drinks", "happy hour": "happy hour", "movie": "movie", "movies": "movie",
    "hike": "hike", "hiking": "hike", "concert": "concert", "show": "show",
    "party": "party", "bbq": "BBQ", "barbecue": "BBQ", "game night": "game night",
    "game": "game", "run": "run", "gym": "gym", "workout": "workout", "yoga": "yoga",
    "climbing": "climbing", "golf": "golf", "tennis": "tennis", "pickleball": "pickleball",
    "basketball": "basketball", "soccer": "soccer", "sync": "sync", "meeting": "meeting",
    "call": "call", "standup": "standup", "interview": "interview", "trip": "trip",
    "wedding": "wedding", "birthday": "birthday", "book club": "book club",
    "shopping": "shopping", "appointment": "appointment", "haircut": "haircut",
    "dentist": "dentist", "doctor": "doctor's appointment", "picnic": "picnic",
    "beach": "beach", "museum": "museum", "karaoke": "karaoke", "trivia": "trivia",
    "bowling": "bowling", "date": "date", "hang": "hang out", "hangout": "hang out",
    "playdate": "playdate", "practice": "practice", "class": "class", "brewery": "brewery",
    "running club": "running club", "conference": "conference", "festival": "festival",
    "marathon": "marathon", "bonfire": "bonfire", "poker": "poker", "pizza": "pizza",
    "food": "food", "escape room": "escape room",
}
_ACTIVITY_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(_ACTIVITIES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

_RECURRENCE_PATTERNS = (
    (re.compile(r"\b(?:every other week|biweekly|every two weeks)\b", re.IGNORECASE), "biweekly"),
    (re.compile(rf"\b(?:every (?:week|{_WEEKDAY_ALT})|weekly)\b", re.IGNORECASE), "weekly"),
    (re.compile(r"\b(?:every day|daily)\b", re.IGNORECASE), "daily"),
    (re.compile(r"\b(?:every month|monthly)\b", re.IGNORECASE), "monthly"),
)


def _sent_date(msg: dict) -> date:
    return datetime.fromtimestamp(msg.get("unix_ts", 0)).date()


def _resolve_month_day(month: int, day: int, sent: date, year: int | None = None) -> date | None:
    """Month/day with no year means the nearest such date that isn't well in
    the past: a month-old date rolls forward a year ("Jan 5" said in December),
    a date a few days back stays put (it's a past reference, and the
    past-event gate downstream handles it)."""
    try:
        if year is not None:
            return date(year if year > 100 else 2000 + year, month, day)
        d = date(sent.year, month, day)
    except ValueError:
        return None
    if d < sent - timedelta(days=30):
        try:
            d = date(sent.year + 1, month, day)
        except ValueError:
            return None
    return d


def _add(out: list[dict], seen: set, value: str, phrase: str, idx: int) -> None:
    if value not in seen:
        seen.add(value)
        out.append({"value": value, "phrase": phrase.strip(), "msg_index": idx})


def _mask(text: str, spans: list[tuple[int, int]]) -> str:
    """Blank out already-consumed spans so a later, looser pattern can't
    re-read them (the "17" in "June 17" is not also "the 17th")."""
    chars = list(text)
    for start, end in spans:
        for k in range(start, end):
            chars[k] = " "
    return "".join(chars)


def date_candidates(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set = set()
    for idx, msg in enumerate(messages):
        text = msg.get("text") or ""
        sent = _sent_date(msg)
        consumed: list[tuple[int, int]] = []

        for m in _MONTH_DAY_RE.finditer(text):
            d = _resolve_month_day(_MONTHS[m.group(1).lower()], int(m.group(2)), sent)
            if d:
                _add(out, seen, d.isoformat(), m.group(0), idx)
            consumed.append(m.span())
        for m in _DAY_OF_MONTH_RE.finditer(text):
            d = _resolve_month_day(_MONTHS[m.group(2).lower()], int(m.group(1)), sent)
            if d:
                _add(out, seen, d.isoformat(), m.group(0), idx)
            consumed.append(m.span())
        masked = _mask(text, consumed)

        for m in _NUMERIC_RE.finditer(masked):
            year = int(m.group(3)) if m.group(3) else None
            d = _resolve_month_day(int(m.group(1)), int(m.group(2)), sent, year)
            if d:
                _add(out, seen, d.isoformat(), m.group(0), idx)
            consumed.append(m.span())
        masked = _mask(text, consumed)

        for m in _ORDINAL_RE.finditer(masked):
            day = int(m.group(1))
            try:
                d = date(sent.year, sent.month, day)
                if d < sent:
                    d = date(sent.year + (sent.month == 12), sent.month % 12 + 1, day)
            except ValueError:
                continue
            _add(out, seen, d.isoformat(), m.group(0), idx)

        for m in _RELATIVE_RE.finditer(masked):
            word = m.group(1).lower()
            if word == "day after tomorrow":
                ds = [sent + timedelta(days=2)]
            elif word in ("tomorrow", "tmrw", "tmr"):
                ds = [sent + timedelta(days=1)]
            elif word.startswith("in "):
                ds = [sent + timedelta(days=int(m.group(2)))]
            elif word == "this weekend":
                ds = [sent + timedelta(days=(5 - sent.weekday()) % 7)]
            elif word == "next weekend":
                sat = sent + timedelta(days=(5 - sent.weekday()) % 7)
                ds = [sat + timedelta(days=7), sat]
            else:  # today / tonight / this morning|afternoon|evening
                ds = [sent]
            for d in ds:
                _add(out, seen, d.isoformat(), m.group(0), idx)

        for m in _WEEKDAY_RE.finditer(masked):
            qualifier = (m.group(1) or "").lower()
            if qualifier == "last":
                continue
            target = _WEEKDAYS[m.group(2).lower()]
            ahead = (target - sent.weekday()) % 7
            nearest = sent + timedelta(days=ahead)
            # "next Friday" follows the project convention (golden
            # anchor_next_friday): the Friday of next week, never the coming
            # one. A convention isn't something to infer from context, so only
            # that reading is offered. A weekday named on that same weekday
            # is genuinely ambiguous (today, or a week out), so both readings
            # are offered for Jev to pick between.
            if qualifier == "next":
                ds = [nearest + timedelta(days=7)]
            elif ahead == 0:
                ds = [nearest, nearest + timedelta(days=7)]
            else:
                ds = [nearest]
            for d in ds:
                _add(out, seen, d.isoformat(), m.group(0), idx)
    return out


def _hhmm(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


def time_candidates(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set = set()
    for idx, msg in enumerate(messages):
        text = msg.get("text") or ""
        for m in _CLOCK_RE.finditer(text):
            hour, minute = int(m.group(1)), int(m.group(2) or 0)
            if not 1 <= hour <= 12:
                continue
            pm = m.group(3).lower().startswith("p")
            hour = (hour % 12) + (12 if pm else 0)
            _add(out, seen, _hhmm(hour, minute), m.group(0), idx)
        for m in _NAMED_TIME_RE.finditer(text):
            _add(out, seen, "00:00" if m.group(1).lower() == "midnight" else "12:00", m.group(0), idx)
        for m in _BARE_TIME_RE.finditer(text):
            hour, minute = int(m.group(1)), int(m.group(2) or 0)
            if not 1 <= hour <= 12:
                continue
            # Social plans skew evening: offer pm first, and am only where a
            # morning reading is plausible.
            if hour == 12:
                readings = [12]
            elif hour <= 6:
                readings = [hour + 12]
            else:
                readings = [hour + 12, hour]
            for h in readings:
                _add(out, seen, _hhmm(h, minute), m.group(0), idx)
        for m in _CHANGE_TIME_RE.finditer(text):
            hour = int(m.group(1) or m.group(3))
            minute = int(m.group(2) or m.group(4) or 0)
            if not 1 <= hour <= 12:
                continue
            readings = [12] if hour == 12 else [hour + 12] if hour <= 6 else [hour + 12, hour]
            for h in readings:
                _add(out, seen, _hhmm(h, minute), m.group(0), idx)
        for m in _COLON_TIME_RE.finditer(text):
            hour, minute = int(m.group(1)), int(m.group(2))
            if not 1 <= hour <= 12:
                continue
            readings = [12] if hour == 12 else [hour + 12] if hour <= 6 else [hour + 12, hour]
            for h in readings:
                _add(out, seen, _hhmm(h, minute), m.group(0), idx)
    return out


def location_candidates(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set = set()
    for idx, msg in enumerate(messages):
        text = msg.get("text") or ""
        for m in _PRIVATE_PLACE_RE.finditer(text):
            _add(out, seen, m.group(1), m.group(0), idx)
        for m in _LOCATION_RE.finditer(text):
            # Stop at a date word: "Blue Bottle on Friday" is "Blue Bottle".
            words = m.group(1).split()
            cut = next((k for k, w in enumerate(words) if k and w.lower().strip("'’.,") in _LOCATION_STOPWORDS),
                       len(words))
            value = re.sub(r"(?:\s+(?:of|on|the|&))+$", "", " ".join(words[:cut])).rstrip(".")
            first = value.split()[0].lower().strip("'’.")
            if first == "the" and len(value.split()) > 1:
                first = value.split()[1].lower().strip("'’.")
            if first in _LOCATION_STOPWORDS:
                continue
            _add(out, seen, value, m.group(0), idx)
        for m in _LOWER_PLACE_RE.finditer(text):
            word = m.group(1).split()[-1].lower()
            if word in _LOWER_PLACE_STOPWORDS or word in _LOCATION_STOPWORDS or word in _ACTIVITIES:
                continue
            _add(out, seen, m.group(1), m.group(0), idx)
    return out


# Where a plan's noun phrase ends: the first date, time, place, or clause
# boundary after it ("bonfire | Saturday", "escape room | at 6pm", "poker | —").
_LEAD_STOP_RE = re.compile(
    rf"[?!,.;:—–(]|\s-\s|\b(?:at|@|on|in|this|next|tomorrow|tmrw|tonight|today|with|for|after|"
    rf"before|around|by|you|u|anyone|again|instead|{_WEEKDAY_ALT}|{_MONTH_ALT})\b|\d",
    re.IGNORECASE,
)
_LEAD_FILLER_RE = re.compile(
    r"^(?:(?:hey|so|ok|okay|also|and|still|are|we|is|it|on|for|want|wanna|to|do|you|"
    r"let'?s|lets|go|going|get|grab|grabbing|have|having|how|about|what|doing|up|"
    r"a|an|the|some|our|my|any|interest|in|i'?m|im|running|hosting|planning)\s+)+",
    re.IGNORECASE,
)
_LEAD_REJECT = {
    "yes", "yeah", "yep", "no", "nope", "maybe", "sure", "ok", "okay", "lol", "same",
    "sounds", "perfect", "great", "i", "we", "me", "count", "can't", "cant", "sorry",
    "love", "see", "works", "thanks", "def", "what", "when", "where", "who", "time",
}


def _lead_phrase(text: str) -> str | None:
    """The noun phrase an invitation opens with ("bonfire Saturday 7pm?" ->
    "bonfire", "lets go to pizza at diceys" -> "pizza"). Catches the long
    tail of activities no keyword list anticipates."""
    stop = _LEAD_STOP_RE.search(text)
    head = text[:stop.start()] if stop else text
    head = _LEAD_FILLER_RE.sub("", head.strip() + " ").strip()
    words = head.split()
    if not 1 <= len(words) <= 4 or words[0].lower().strip("'’") in _LEAD_REJECT:
        return None
    if not all(re.fullmatch(r"[\w'’&-]+", w) for w in words):
        return None
    return head


def activity_candidates(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set = set()
    for idx, msg in enumerate(messages):
        text = msg.get("text") or ""
        lead = _lead_phrase(text)
        if lead:
            _add(out, seen, lead.lower(), lead, idx)
        for m in _ACTIVITY_RE.finditer(text):
            _add(out, seen, _ACTIVITIES[m.group(1).lower()], m.group(0), idx)
    return out


def recurrence(messages: list[dict]) -> str | None:
    text = " ".join(m.get("text") or "" for m in messages)
    for pattern, label in _RECURRENCE_PATTERNS:
        if pattern.search(text):
            return label
    return None


def generate(thread: dict) -> dict:
    messages = thread.get("messages", [])
    return {
        "dates": date_candidates(messages),
        "times": time_candidates(messages),
        "locations": location_candidates(messages),
        "activities": activity_candidates(messages),
        "recurrence": recurrence(messages),
    }

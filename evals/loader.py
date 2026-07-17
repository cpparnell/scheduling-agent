"""Load golden eval cases and materialize their date placeholders relative to
the real ``today`` at runtime, so fixtures never go stale.

Placeholders in message text:
  {day+N}     -> "Weekday, Month D" of today+N  (e.g. "Tuesday, June 17")
  {date+N}    -> "Month D" of today+N           (e.g. "June 13")
  {saturday}  -> "Saturday" (next Saturday from today)
  {monday}, {tuesday}, ... -> next occurrence of that weekday's name, never today
                               (use this instead of {day+N} whenever the message text
                               also names a literal weekday, e.g. "every Monday" — a
                               fixed N-day offset will only coincidentally land on the
                               right weekday, silently making the case flaky)
  {tomorrow}  -> "tomorrow"
  {tonight}   -> "tonight"

Expectation fields:
  date_offset_days: N            ->  resolved to concrete "YYYY-MM-DD" (today+N)
  date_offset_saturday: true     ->  resolved to next Saturday's "YYYY-MM-DD"
  date_offset_weekday: "monday"  ->  resolved to next occurrence of that weekday
"""

import json
import re
import time
from datetime import date, timedelta
from pathlib import Path

GOLDEN_PATH = Path(__file__).parent / "golden.jsonl"

_PLACEHOLDER = re.compile(r"\{([^}]+)\}")

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _days_until_weekday(today: date, weekday_name: str) -> int:
    """Days from today until the next occurrence of weekday_name (never 0 —
    always at least 1, i.e. "next Monday" said today never means today)."""
    target = _WEEKDAYS.index(weekday_name.lower())
    return (target - today.weekday()) % 7 or 7


def _days_until_saturday(today: date) -> int:
    return _days_until_weekday(today, "saturday")


def _substitute(text: str, today: date) -> str:
    def repl(m: re.Match) -> str:
        token = m.group(1)
        if token in ("tomorrow", "tonight"):
            return token
        if token.lower() in _WEEKDAYS:
            return (today + timedelta(days=_days_until_weekday(today, token))).strftime("%A")
        rel = re.fullmatch(r"(day|date)\+(\d+)", token)
        if rel:
            kind, n = rel.group(1), int(rel.group(2))
            d = today + timedelta(days=n)
            if kind == "day":
                return f"{d.strftime('%A, %B')} {d.day}"
            return f"{d.strftime('%B')} {d.day}"
        return m.group(0)

    return _PLACEHOLDER.sub(repl, text)


def _materialize_messages(raw_messages: list[dict], participants: list[str], today: date, now: float) -> list[dict]:
    messages = []
    for msg in raw_messages:
        if msg.get("from_me"):
            sender = "me"
        else:
            # sender_index picks which participant sent the message (default 0).
            # Enables group-chat cases where multiple people speak.
            idx = msg.get("sender_index", 0)
            sender = participants[idx] if idx < len(participants) else participants[0]
        messages.append({
            "sender": sender,
            "text": _substitute(msg["text"], today),
            "from_me": msg.get("from_me", False),
            "unix_ts": now - msg.get("hours_ago", 1) * 3600,
        })
    return messages


def materialize_case(case: dict, today: date | None = None, now: float | None = None):
    """Return ``(thread, expected)`` ready for ``detector.detect_plans``."""
    today = today or date.today()
    now = time.time() if now is None else now

    participants = case.get("participants", ["+15550000000"])
    thread = {
        "chat_id": case["id"],
        "participants": participants,
        "messages": _materialize_messages(case["messages"], participants, today, now),
    }

    expected = _resolve_offsets(dict(case["expected"]), today)
    if "events" in expected:
        expected["events"] = [_resolve_offsets(dict(e), today) for e in expected["events"]]

    return thread, expected


def materialize_polls(case: dict, today: date | None = None, now: float | None = None) -> list[dict]:
    """Materialize a multi-poll pipeline case (``"polls": [{...}, ...]``) into
    one thread per poll.

    Poll N's thread contains ALL messages from polls 1..N that belong to the
    same chat, mimicking reader._prepend_context re-feeding prior context on
    every incremental poll — the exact replay behavior that causes duplicate
    re-detections in production. A poll may set ``chat_id`` to simulate the
    same plan surfacing in a different conversation (default: the case id).
    """
    today = today or date.today()
    now = time.time() if now is None else now

    participants = case.get("participants", ["+15550000000"])
    threads = []
    for i, poll in enumerate(case["polls"]):
        chat_id = poll.get("chat_id", case["id"])
        raw = []
        for prior in case["polls"][: i + 1]:
            if prior.get("chat_id", case["id"]) == chat_id:
                raw.extend(prior["messages"])
        threads.append({
            "chat_id": chat_id,
            "participants": poll.get("participants", participants),
            "messages": _materialize_messages(raw, poll.get("participants", participants), today, now),
        })
    return threads


def _resolve_offsets(expected: dict, today: date) -> dict:
    if "date_offset_days" in expected:
        offset = expected.pop("date_offset_days")
        expected["date"] = (today + timedelta(days=offset)).isoformat()
    if expected.pop("date_offset_saturday", False):
        expected["date"] = (today + timedelta(days=_days_until_saturday(today))).isoformat()
    if "date_offset_weekday" in expected:
        weekday_name = expected.pop("date_offset_weekday")
        expected["date"] = (today + timedelta(days=_days_until_weekday(today, weekday_name))).isoformat()
    if "end_date_offset_days" in expected:
        offset = expected.pop("end_date_offset_days")
        expected["end_date"] = (today + timedelta(days=offset)).isoformat()
    return expected


def load_golden(path: Path = GOLDEN_PATH) -> list[dict]:
    cases = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("//"):
                cases.append(json.loads(line))
    return cases

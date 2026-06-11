"""Load golden eval cases and materialize their date placeholders relative to
the real ``today`` at runtime, so fixtures never go stale.

Placeholders in message text:
  {day+N}    -> weekday name of today+N        (keep N in 1..6 to stay unambiguous)
  {date+N}   -> "Month D" of today+N           (e.g. "June 13")
  {tomorrow} -> "tomorrow"
  {tonight}  -> "tonight"

Expectation field:
  date_offset_days: N  ->  resolved to concrete "YYYY-MM-DD" (today+N)
"""

import json
import re
import time
from datetime import date, timedelta
from pathlib import Path

GOLDEN_PATH = Path(__file__).parent / "golden.jsonl"

_PLACEHOLDER = re.compile(r"\{([^}]+)\}")


def _substitute(text: str, today: date) -> str:
    def repl(m: re.Match) -> str:
        token = m.group(1)
        if token in ("tomorrow", "tonight"):
            return token
        rel = re.fullmatch(r"(day|date)\+(\d+)", token)
        if rel:
            kind, n = rel.group(1), int(rel.group(2))
            d = today + timedelta(days=n)
            if kind == "day":
                return d.strftime("%A")
            return f"{d.strftime('%B')} {d.day}"
        return m.group(0)

    return _PLACEHOLDER.sub(repl, text)


def materialize_case(case: dict, today: date | None = None, now: float | None = None):
    """Return ``(thread, expected)`` ready for ``detector.detect_plans``."""
    today = today or date.today()
    now = time.time() if now is None else now

    participants = case.get("participants", ["+15550000000"])
    messages = []
    for msg in case["messages"]:
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

    thread = {
        "chat_id": case["id"],
        "participants": participants,
        "messages": messages,
    }

    expected = dict(case["expected"])
    if "date_offset_days" in expected:
        offset = expected.pop("date_offset_days")
        expected["date"] = (today + timedelta(days=offset)).isoformat()

    return thread, expected


def load_golden(path: Path = GOLDEN_PATH) -> list[dict]:
    cases = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("//"):
                cases.append(json.loads(line))
    return cases

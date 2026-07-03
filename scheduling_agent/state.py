import hashlib
import json
import re
import time
from datetime import date as date_type, datetime, timedelta
from pathlib import Path

STATE_DIR = Path.home() / ".scheduling-agent"
STATE_FILE = STATE_DIR / "state.json"

# Bump this whenever the on-disk state shape changes, and add a corresponding
# step in _migrate(). Files written before versioning are treated as version 0.
CURRENT_SCHEMA_VERSION = 3

# Events with the same chat + normalized title within this many days are treated
# as the same occurrence and deduplicated.
TITLE_DEDUP_WINDOW_DAYS = 28

# Descriptive event records older than this are pruned on write to keep
# state.json small; they're no longer useful for dedup/adjudication.
EVENT_RECORD_RETENTION_DAYS = 90


def _new_state() -> dict:
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "last_processed_timestamp": None,
        "created_events": [],
        # Maps "{chat_id}:{normalized_title}" -> most-recent ISO date recorded.
        # Used to catch the same event being detected with a different date.
        "title_events": {},
        # Descriptive records (title/date/time/evidence/calendar_uid/...) used
        # by the LLM dedup adjudicator to compare a new detection against
        # recently created events. suppressed=True means the adjudicator
        # ruled it a duplicate, so no calendar event was actually created.
        "events": [],
        # Tracks how many consecutive polls have failed to advance the
        # watermark past a bad thread, so main.py can cap retries.
        "watermark_hold": {"ts": None, "count": 0},
    }


def _migrate(data: dict) -> dict:
    """Bring an on-disk state dict up to CURRENT_SCHEMA_VERSION in place.

    Migrations are applied stepwise so a file several versions old upgrades
    cleanly. Pre-v1 files (no schema_version) used a title-based dedup hash
    that later changed to time_start; those old hashes can't be recomputed
    here, so v0->v1 keeps the existing created_events as-is and just stamps
    the version. A clean reset is `rm ~/.scheduling-agent/state.json`.
    """
    version = data.get("schema_version", 0)
    if version < 1:
        data.setdefault("last_processed_timestamp", None)
        data.setdefault("created_events", [])
        version = 1
    if version < 2:
        data.setdefault("title_events", {})
        version = 2
    if version < 3:
        data.setdefault("events", [])
        data.setdefault("watermark_hold", {"ts": None, "count": 0})
        version = 3
    data["schema_version"] = CURRENT_SCHEMA_VERSION
    return data


def _load() -> dict:
    STATE_DIR.mkdir(exist_ok=True)
    if not STATE_FILE.exists():
        return _new_state()
    with open(STATE_FILE) as f:
        data = json.load(f)
    on_disk_version = data.get("schema_version", 0)
    data = _migrate(data)
    if on_disk_version != data["schema_version"]:
        # Persist the upgrade once so subsequent reads are clean.
        _save(data)
    return data


def _save(data: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_last_timestamp() -> int | None:
    return _load().get("last_processed_timestamp")


def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation and leading month names for title dedup."""
    t = title.lower()
    t = re.sub(r'\b(january|february|march|april|may|june|july|august|september|october|november|december)\b', '', t)
    t = re.sub(r'[^\w\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def _title_key(chat_id: int, title: str) -> str:
    return f"{chat_id}:{_normalize_title(title)}"


# The dedup key uses time_start when a time is present, falling back to a
# normalized title otherwise. See _migrate() for how pre-versioning state files
# (which used a title-only key) are handled on upgrade.
def event_hash(chat_id: int, date: str, time_start: str | None, title: str) -> str:
    if time_start is not None:
        key = f"{chat_id}|{date}|{time_start}"
    else:
        key = f"{chat_id}|{date}|title:{title.strip().lower()}"
    return hashlib.sha256(key.encode()).hexdigest()


def is_duplicate(chat_id: int, date: str, time_start: str | None, title: str) -> bool:
    data = _load()
    h = event_hash(chat_id, date, time_start, title)
    if h in data.get("created_events", []):
        return True

    # Secondary check: same chat + same normalized title within the dedup window.
    key = _title_key(chat_id, title)
    existing_date_str = data.get("title_events", {}).get(key)
    if existing_date_str:
        try:
            existing = date_type.fromisoformat(existing_date_str)
            new = date_type.fromisoformat(date)
            if abs((new - existing).days) < TITLE_DEDUP_WINDOW_DAYS:
                return True
        except ValueError:
            pass

    return False


def record_event(
    chat_id: int,
    date: str,
    time_start: str | None,
    title: str,
    *,
    location: str | None = None,
    status: str | None = None,
    evidence: str | None = None,
    calendar_uid: str | None = None,
    suppressed: bool = False,
) -> None:
    """Record a created event's dedup hash, title key, and descriptive record.
    Timestamp advancement is handled separately by update_timestamp()."""
    data = _load()
    h = event_hash(chat_id, date, time_start, title)
    created = set(data.get("created_events", []))
    created.add(h)
    data["created_events"] = list(created)

    key = _title_key(chat_id, title)
    title_events = data.setdefault("title_events", {})
    # Keep the most recent date for this title key.
    existing = title_events.get(key)
    if not existing or date > existing:
        title_events[key] = date

    events = data.setdefault("events", [])
    events.append({
        "hash": h,
        "chat_id": chat_id,
        "date": date,
        "time_start": time_start,
        "title": title,
        "location": location,
        "status": status,
        "evidence": evidence,
        "calendar_uid": calendar_uid,
        "created_at": datetime.now().isoformat(),
        "suppressed": suppressed,
    })
    data["events"] = _prune_old_events(events)

    _save(data)


def _prune_old_events(events: list[dict]) -> list[dict]:
    cutoff = date_type.today() - timedelta(days=EVENT_RECORD_RETENTION_DAYS)
    kept = []
    for record in events:
        try:
            if date_type.fromisoformat(record["date"]) >= cutoff:
                kept.append(record)
        except (KeyError, ValueError, TypeError):
            kept.append(record)
    return kept


def get_events_near(date_str: str, window_days: int = 1) -> list[dict]:
    """Non-suppressed descriptive event records within window_days of date_str,
    across all chats. Used to build dedup-adjudication candidates."""
    try:
        target = date_type.fromisoformat(date_str)
    except ValueError:
        return []

    data = _load()
    matches = []
    for record in data.get("events", []):
        if record.get("suppressed"):
            continue
        try:
            record_date = date_type.fromisoformat(record["date"])
        except (KeyError, ValueError, TypeError):
            continue
        if abs((record_date - target).days) <= window_days:
            matches.append(record)
    return matches


def get_watermark_hold() -> dict:
    return _load().get("watermark_hold", {"ts": None, "count": 0})


def set_watermark_hold(ts: int | None, count: int) -> None:
    data = _load()
    data["watermark_hold"] = {"ts": ts, "count": count}
    _save(data)


def update_timestamp(ts: int) -> None:
    data = _load()
    if ts > (data.get("last_processed_timestamp") or 0):
        data["last_processed_timestamp"] = ts
    _save(data)

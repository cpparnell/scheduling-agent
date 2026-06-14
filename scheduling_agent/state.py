import hashlib
import json
import time
from pathlib import Path

STATE_DIR = Path.home() / ".scheduling-agent"
STATE_FILE = STATE_DIR / "state.json"


def _load() -> dict:
    STATE_DIR.mkdir(exist_ok=True)
    if not STATE_FILE.exists():
        return {"last_processed_timestamp": None, "created_events": []}
    with open(STATE_FILE) as f:
        data = json.load(f)
    return data


def _save(data: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_last_timestamp() -> int | None:
    return _load().get("last_processed_timestamp")


# MIGRATION NOTE: as of this version the dedup key uses time_start instead of
# title when a time is present. Existing state.json hashes (keyed on title) will
# not match the new formula, so events within lookback_days may be re-created
# once on the first run after upgrading. Delete ~/.scheduling-agent/state.json
# to reset cleanly.
def event_hash(chat_id: int, date: str, time_start: str | None, title: str) -> str:
    if time_start is not None:
        key = f"{chat_id}|{date}|{time_start}"
    else:
        key = f"{chat_id}|{date}|title:{title.strip().lower()}"
    return hashlib.sha256(key.encode()).hexdigest()


def is_duplicate(chat_id: int, date: str, time_start: str | None, title: str) -> bool:
    data = _load()
    h = event_hash(chat_id, date, time_start, title)
    return h in data.get("created_events", [])


def record_event(chat_id: int, date: str, time_start: str | None, title: str) -> None:
    """Record a created event's dedup hash. Timestamp advancement is handled
    separately by update_timestamp()."""
    data = _load()
    h = event_hash(chat_id, date, time_start, title)
    created = set(data.get("created_events", []))
    created.add(h)
    data["created_events"] = list(created)
    _save(data)


def update_timestamp(ts: int) -> None:
    data = _load()
    if ts > (data.get("last_processed_timestamp") or 0):
        data["last_processed_timestamp"] = ts
    _save(data)

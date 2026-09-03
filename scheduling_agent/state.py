import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import date as date_type, datetime, timedelta
from pathlib import Path

STATE_DIR = Path.home() / ".scheduling-agent"
STATE_FILE = STATE_DIR / "state.json"

# Bump this whenever the on-disk state shape changes, and add a corresponding
# step in _migrate(). Files written before versioning are treated as version 0.
CURRENT_SCHEMA_VERSION = 7

# Events with the same chat + normalized title within this many days are treated
# as the same occurrence and deduplicated.
TITLE_DEDUP_WINDOW_DAYS = 28

# Descriptive event records older than this are pruned on write to keep
# state.json small; they're no longer useful for dedup/adjudication.
EVENT_RECORD_RETENTION_DAYS = 90

# Verbatim message evidence is truncated to this length before it's ever
# written to disk. state.json otherwise accumulates an unbounded, unencrypted
# archive of quoted personal message text. dedup.py's LLM adjudicator still
# reads this field to compare candidate plans, so this is a real tradeoff —
# a short snippet keeps that comparison useful without keeping full quotes.
EVIDENCE_MAX_CHARS = 240


def _truncate_evidence(evidence: str | None) -> str | None:
    if evidence is None or len(evidence) <= EVIDENCE_MAX_CHARS:
        return evidence
    return evidence[:EVIDENCE_MAX_CHARS].rstrip() + "…"


def _new_state() -> dict:
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "last_processed_timestamp": None,
        # Maps dedup hash -> ISO date, so pruning can drop stale hashes in step
        # with the records they came from (see _prune_state).
        "created_events": {},
        # Maps "{chat_id}:{normalized_title}" -> most-recent ISO date recorded.
        # Used to catch the same event being detected with a different date.
        "title_events": {},
        # The canonical event store: one record per real-world plan the agent
        # knows about. Reconciliation matches new detections against these
        # records (and pending journal entries) before anything touches the
        # calendar. suppressed=True means the record was ruled a duplicate of
        # another, so no calendar event was created for it.
        "events": [],
        # Write-ahead journal: an entry is written BEFORE the corresponding
        # calendar write and removed once state has been updated to match, so
        # a crash between the two can be detected and recovered on startup.
        "journal": [],
        # Tracks how many consecutive polls have failed to advance the
        # watermark past a bad thread, so main.py can cap retries.
        "watermark_hold": {"ts": None, "count": 0},
        # Maps dedup hash -> {last_status, last_seen, date, count} for
        # detections main.process_event skipped (unanswered, not-participant,
        # low-confidence) — these previously left no record at all, so a
        # single flaky poll's confirmation of a hash last seen "unanswered"
        # had no history to check against. See record_observation/
        # get_observation and main.py's anti-flap guard.
        "observations": {},
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
    if version < 4:
        # v4: events become the canonical store (stable canonical_id, multi-chat
        # provenance, revision history) and the write-ahead journal appears.
        for record in data.get("events", []):
            record.setdefault("canonical_id", uuid.uuid4().hex)
            if "chat_ids" not in record:
                chat_id = record.get("chat_id")
                record["chat_ids"] = [chat_id] if chat_id is not None else []
            record.setdefault("confidence", None)
            record.setdefault("updated_at", record.get("created_at"))
            record.setdefault("revisions", [])
        data.setdefault("journal", [])
        version = 4
    if version < 5:
        # v5: created_events becomes hash -> date (was a bare list of hashes),
        # so it can be pruned in lockstep with `events` and `title_events"
        # instead of growing unbounded and silently outliving the records it
        # dedups against (is_duplicate() true, find_record() None).
        old_created = data.get("created_events", [])
        if isinstance(old_created, list):
            events_by_hash = {
                r.get("hash"): r.get("date")
                for r in data.get("events", [])
                if r.get("hash") and r.get("date")
            }
            today_str = date_type.today().isoformat()
            data["created_events"] = {
                h: events_by_hash.get(h, today_str) for h in old_created
            }
        version = 5
    if version < 6:
        # v6: observations map for skipped (not created) detections, so an
        # anti-flap check has history to consult instead of none at all.
        data.setdefault("observations", {})
        version = 6
    if version < 7:
        # v7: title normalization becomes token-set (order-insensitive) and
        # event_hash drops time_start (a time_confidence flap between polls
        # no longer produces two hashes for the same plan). Recompute each
        # canonical record's hash and title key under the new algorithm and
        # merge them in; stale old-algorithm entries in created_events/
        # title_events are left as-is (harmless — they just age out via the
        # normal retention prune, same as any other entry).
        for record in data.get("events", []):
            record["hash"] = event_hash(
                record.get("chat_id"), record.get("date"),
                record.get("time_start"), record.get("title", ""),
            )
        created = data.setdefault("created_events", {})
        title_events = data.setdefault("title_events", {})
        for record in data.get("events", []):
            if record.get("hash") and record.get("date"):
                created[record["hash"]] = record["date"]
            key = _title_key(record.get("chat_id"), record.get("title", ""))
            d = record.get("date")
            if d and (key not in title_events or d > title_events[key]):
                title_events[key] = d
        version = 7
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
    """Write state.json atomically: a crash or concurrent read mid-write can
    never observe a truncated/partial file, since the write lands on a temp
    file first and only becomes STATE_FILE via a single atomic rename."""
    STATE_DIR.mkdir(exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=STATE_DIR, prefix=".state-", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, STATE_FILE)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_last_timestamp() -> int | None:
    return _load().get("last_processed_timestamp")


def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation/month names, and collapse to a sorted set
    of unique tokens for title dedup — order-insensitive, so "dinner with Sam"
    and "Sam dinner" key identically. Matches the fuzzy reconciliation layer's
    Jaccard-overlap notion of title equality (reconcile._title_tokens) instead
    of the two disagreeing (a title reworded verb-first previously produced a
    different hash/title-key than the fuzzy layer would call a match)."""
    t = title.lower()
    t = re.sub(r'\b(january|february|march|april|may|june|july|august|september|october|november|december)\b', '', t)
    t = re.sub(r'[^\w\s]', '', t)
    tokens = sorted(set(t.split()))
    return ' '.join(tokens)


def _title_key(chat_id: int, title: str) -> str:
    return f"{chat_id}:{_normalize_title(title)}"


# The dedup key is chat + date + normalized title (order-insensitive token
# set — see _normalize_title). time_start deliberately does NOT factor in:
# a plan detected once at 19:00 and once all-day (time_confidence dipped
# below its threshold on one poll) is the same plan and must hash the same,
# not silently produce two canonical records. `time_start` is kept as a
# parameter for call-site compatibility even though unused here. See
# _migrate() for how hashes computed under the pre-v7 (time_start-keyed)
# algorithm are upgraded.
def event_hash(chat_id: int, date: str, time_start: str | None, title: str) -> str:
    key = f"{chat_id}|{date}|{_normalize_title(title)}"
    return hashlib.sha256(key.encode()).hexdigest()


def _pending_create_records(data: dict) -> list[dict]:
    """Records from pending create-journal entries. These count for dedup and
    candidate lookup exactly like committed records, so the window between
    journal_intent and journal_commit (or a crash inside it) can't produce a
    duplicate calendar event."""
    return [
        entry["record"]
        for entry in data.get("journal", [])
        if entry.get("op") == "create" and entry.get("record")
    ]


def is_duplicate(chat_id: int, date: str, time_start: str | None, title: str) -> bool:
    data = _load()
    h = event_hash(chat_id, date, time_start, title)
    if h in data.get("created_events", {}):
        return True

    pending = _pending_create_records(data)
    if any(r.get("hash") == h for r in pending):
        return True

    # Secondary check: same chat + same normalized title within the dedup window.
    key = _title_key(chat_id, title)
    existing_date_str = data.get("title_events", {}).get(key)
    if not existing_date_str:
        for r in pending:
            if r.get("chat_id") is not None and _title_key(r["chat_id"], r.get("title", "")) == key:
                existing_date_str = r.get("date")
                break
    if existing_date_str:
        try:
            existing = date_type.fromisoformat(existing_date_str)
            new = date_type.fromisoformat(date)
            if abs((new - existing).days) < TITLE_DEDUP_WINDOW_DAYS:
                return True
        except ValueError:
            pass

    return False


def find_record(chat_id: int, date: str, time_start: str | None, title: str) -> dict | None:
    """The committed, non-suppressed canonical record matching this event's
    dedup identity: exact hash first, then the per-chat title window. Lets the
    reconciler apply material changes (reschedule, status upgrade) to a record
    the exact dedup layer already matched."""
    data = _load()
    events = [r for r in data.get("events", []) if not r.get("suppressed")]

    h = event_hash(chat_id, date, time_start, title)
    for record in events:
        if record.get("hash") == h:
            return record

    key = _title_key(chat_id, title)
    for record in events:
        if record.get("chat_id") != chat_id or _title_key(chat_id, record.get("title", "")) != key:
            continue
        try:
            existing = date_type.fromisoformat(record["date"])
            new = date_type.fromisoformat(date)
        except (KeyError, TypeError, ValueError):
            continue
        if abs((new - existing).days) < TITLE_DEDUP_WINDOW_DAYS:
            return record
    return None


def make_record(
    chat_id: int,
    date: str,
    time_start: str | None,
    title: str,
    *,
    location: str | None = None,
    status: str | None = None,
    evidence: str | None = None,
    confidence: float | None = None,
    calendar_uid: str | None = None,
    suppressed: bool = False,
    duplicate_of_uid: str | None = None,
) -> dict:
    """Build a canonical event record (not yet persisted)."""
    now = datetime.now().isoformat()
    return {
        "canonical_id": uuid.uuid4().hex,
        "hash": event_hash(chat_id, date, time_start, title),
        "chat_id": chat_id,
        "chat_ids": [chat_id],
        "date": date,
        "time_start": time_start,
        "title": title,
        "location": location,
        "status": status,
        "evidence": _truncate_evidence(evidence),
        "confidence": confidence,
        "calendar_uid": calendar_uid,
        "created_at": now,
        "updated_at": now,
        "suppressed": suppressed,
        "duplicate_of_uid": duplicate_of_uid,
        "revisions": [],
    }


# --- Write-ahead journal -----------------------------------------------------


def journal_intent(record: dict, op: str = "create") -> str:
    """Persist the intent to perform a calendar write for `record` BEFORE the
    write happens. Returns the journal_id to pass to journal_commit/journal_drop
    once state has been brought in line (or the write abandoned)."""
    data = _load()
    journal_id = uuid.uuid4().hex
    data.setdefault("journal", []).append({
        "journal_id": journal_id,
        "op": op,
        "record": record,
        "started_at": datetime.now().isoformat(),
        "state": "pending",
    })
    _save(data)
    return journal_id


def journal_commit(journal_id: str, calendar_uid: str | None = None) -> None:
    """Land a pending create entry in the canonical store (hash, title key,
    record) and remove it from the journal, in a single save."""
    data = _load()
    journal = data.get("journal", [])
    entry = next((e for e in journal if e.get("journal_id") == journal_id), None)
    if entry is None:
        return
    journal.remove(entry)
    if entry.get("op") == "create":
        record = entry["record"]
        record["calendar_uid"] = calendar_uid
        _commit_record(data, record)
    _save(data)


def journal_drop(journal_id: str) -> None:
    """Abandon a pending journal entry without committing (e.g. the calendar
    write failed, so there is nothing to record)."""
    data = _load()
    data["journal"] = [
        e for e in data.get("journal", []) if e.get("journal_id") != journal_id
    ]
    _save(data)


def get_pending_journal() -> list[dict]:
    return list(_load().get("journal", []))


def _commit_record(data: dict, record: dict) -> None:
    """Add a record's hash, title key, and the record itself to `data` (caller saves)."""
    created = data.setdefault("created_events", {})
    created[record["hash"]] = record["date"]

    key = _title_key(record["chat_id"], record["title"])
    title_events = data.setdefault("title_events", {})
    existing = title_events.get(key)
    if not existing or record["date"] > existing:
        title_events[key] = record["date"]

    events = data.setdefault("events", [])
    events.append(record)
    _prune_state(data)


def record_event(
    chat_id: int,
    date: str,
    time_start: str | None,
    title: str,
    *,
    location: str | None = None,
    status: str | None = None,
    evidence: str | None = None,
    confidence: float | None = None,
    calendar_uid: str | None = None,
    suppressed: bool = False,
    duplicate_of_uid: str | None = None,
) -> None:
    """Record an event directly (intent + commit in one step). Used for paths
    with no calendar write to journal around, e.g. suppressed duplicates.
    Timestamp advancement is handled separately by update_timestamp()."""
    record = make_record(
        chat_id, date, time_start, title,
        location=location, status=status, evidence=evidence,
        confidence=confidence, calendar_uid=calendar_uid,
        suppressed=suppressed, duplicate_of_uid=duplicate_of_uid,
    )
    data = _load()
    _commit_record(data, record)
    _save(data)


def update_record(
    canonical_id: str,
    changes: dict,
    *,
    reason: str | None = None,
    chat_id: int | None = None,
) -> bool:
    """Apply reconciliation changes (date/time_start/title/location/status/
    confidence) to a canonical record. Appends a revision entry, merges chat
    provenance, and registers the new dedup hash while keeping the old one so
    prior wordings stay deduplicated. Returns False if no record matches."""
    data = _load()
    record = next(
        (r for r in data.get("events", []) if r.get("canonical_id") == canonical_id),
        None,
    )
    if record is None:
        return False

    changed = {}
    for field, new_value in changes.items():
        if field == "evidence":
            new_value = _truncate_evidence(new_value)
        old_value = record.get(field)
        if old_value != new_value:
            changed[field] = [old_value, new_value]
            record[field] = new_value

    if chat_id is not None and chat_id not in record.get("chat_ids", []):
        record.setdefault("chat_ids", []).append(chat_id)

    if changed:
        record["updated_at"] = datetime.now().isoformat()
        record.setdefault("revisions", []).append({
            "at": record["updated_at"],
            "changed": changed,
            "reason": reason,
        })
        new_hash = event_hash(
            record["chat_id"], record["date"], record.get("time_start"), record["title"]
        )
        record["hash"] = new_hash
        created = data.setdefault("created_events", {})
        created[new_hash] = record["date"]  # old hash stays: prior wording remains deduped

        key = _title_key(record["chat_id"], record["title"])
        title_events = data.setdefault("title_events", {})
        existing = title_events.get(key)
        if not existing or record["date"] > existing:
            title_events[key] = record["date"]

        _prune_state(data)

    _save(data)
    return True


def _prune_state(data: dict) -> None:
    """Prune `events`, `created_events`, `title_events`, and `observations`
    together against the same retention cutoff, in place. Keeping the first
    three in lockstep is what lets is_duplicate() and find_record() agree: a
    hash or title key that outlives the record it came from is what
    previously caused is_duplicate()=True / find_record()=None (a duplicate
    detection silently dropped with no record to reconcile against).
    `observations` never had this failure mode (it's read defensively via
    get_observation, not asserted against) but is pruned the same way so it
    doesn't grow unbounded either."""
    cutoff = date_type.today() - timedelta(days=EVENT_RECORD_RETENTION_DAYS)

    kept = []
    for record in data.get("events", []):
        try:
            if date_type.fromisoformat(record["date"]) >= cutoff:
                kept.append(record)
        except (KeyError, ValueError, TypeError):
            kept.append(record)
    data["events"] = kept

    created = data.get("created_events", {})
    pruned_created = {}
    for h, d in created.items():
        try:
            if date_type.fromisoformat(d) >= cutoff:
                pruned_created[h] = d
        except (ValueError, TypeError):
            pruned_created[h] = d  # malformed date: keep rather than silently un-dedup
    data["created_events"] = pruned_created

    title_events = data.get("title_events", {})
    pruned_title = {}
    for k, d in title_events.items():
        try:
            if date_type.fromisoformat(d) >= cutoff:
                pruned_title[k] = d
        except (ValueError, TypeError):
            pruned_title[k] = d
    data["title_events"] = pruned_title

    observations = data.get("observations", {})
    pruned_observations = {}
    for h, obs in observations.items():
        d = obs.get("date")
        try:
            if d is not None and date_type.fromisoformat(d) >= cutoff:
                pruned_observations[h] = obs
            elif d is None:
                pruned_observations[h] = obs  # malformed: keep rather than silently drop
        except (ValueError, TypeError):
            pruned_observations[h] = obs
    data["observations"] = pruned_observations


# --- Observations (F7 anti-flap guard) --------------------------------------


def record_observation(
    chat_id: int, date: str, time_start: str | None, title: str, status: str
) -> None:
    """Record that a detection was skipped (not created) with this status —
    unanswered, not-participant, or low-confidence. Written even though no
    calendar event resulted, so a later poll can tell a genuine acceptance
    apart from a single flaky re-classification of stale replayed context
    (see main.py's anti-flap guard, which consults get_observation)."""
    data = _load()
    h = event_hash(chat_id, date, time_start, title)
    observations = data.setdefault("observations", {})
    prev = observations.get(h)
    observations[h] = {
        "last_status": status,
        "last_seen": datetime.now().isoformat(),
        "date": date,
        "count": (prev.get("count", 0) + 1) if prev else 1,
    }
    _prune_state(data)
    _save(data)


def get_observation(chat_id: int, date: str, time_start: str | None, title: str) -> dict | None:
    data = _load()
    h = event_hash(chat_id, date, time_start, title)
    return data.get("observations", {}).get(h)


def get_events_near(date_str: str, window_days: int = 1) -> list[dict]:
    """Non-suppressed canonical records within window_days of date_str, across
    all chats, including records from pending create-journal entries. Used to
    build reconciliation candidates."""
    try:
        target = date_type.fromisoformat(date_str)
    except ValueError:
        return []

    data = _load()
    matches = []
    for record in data.get("events", []) + _pending_create_records(data):
        if record.get("suppressed"):
            continue
        try:
            record_date = date_type.fromisoformat(record["date"])
        except (KeyError, ValueError, TypeError):
            continue
        if abs((record_date - target).days) <= window_days:
            matches.append(record)
    return matches


def get_active_events() -> list[dict]:
    """All non-suppressed canonical records across all chats and dates,
    including records from pending create-journal entries. Used by the
    far-date reconciliation layer, which matches on title rather than date."""
    data = _load()
    return [
        record
        for record in data.get("events", []) + _pending_create_records(data)
        if not record.get("suppressed")
    ]


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

import argparse
import contextlib
import hashlib
import json
import logging
import logging.handlers
import shutil
import signal
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from . import calendar, config, detector, reader, reconcile, state, watcher

LOGS_DIR = Path(__file__).parent.parent / "logs" / "stdout"
BACKFILL_LOG_DIR = Path(__file__).parent.parent / "logs" / "backfill"

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"

    fmt = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    datefmt = "%H:%M:%S"
    formatter = logging.Formatter(fmt, datefmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    # Rotating rather than a plain FileHandler: under launchd this process runs
    # indefinitely instead of once per terminal session, so an unbounded
    # single file is no longer safe to assume.
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    logger.info("Logging to %s", log_file)


def process_event(event: dict, cfg: dict, reference_date: date | None = None) -> str:
    """Run one detected event through the gate sequence and reconciliation.

    Returns "created", "updated", or "skipped:<reason>". Shared by the polling
    loop, the eval harness, and backfill() so all three exercise the exact
    production gates.

    `reference_date` is what "today" means for the past-event gate below.
    Production and the eval harness leave it None (the live wall clock);
    backfill() pins it to each window's own end, since a message from last
    March describing a plan for "next week" must not be skipped as if that
    week were already in the past.
    """
    today = reference_date or datetime.now().date()
    chat_id = event["chat_id"]
    title = event["title"]
    date = event["date"]
    time_start = event.get("time_start")
    time_confidence = event.get("time_confidence") or 0
    location = event.get("location")
    confidence = event["confidence"]
    status = event.get("status", "confirmed")
    evidence = event.get("evidence")
    new_msg_from_user = event.get("_new_msg_from_user", True)

    try:
        event_date = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        logger.warning("Skipping event with unparseable date %r: %s", date, title)
        return "skipped:unparseable-date"

    if event_date < today:
        logger.info("Skipping past event: %s on %s", title, date)
        return "skipped:past"

    # Hard ownership gate: plans the user isn't personally part of never touch
    # the calendar, no matter how confident or confirmed they are.
    if not event.get("user_is_participant"):
        logger.info(
            "Skipping non-participant plan: %s on %s — %s",
            title, date, event.get("participation_evidence"),
        )
        state.record_observation(chat_id, date, time_start, title, "not-participant")
        return "skipped:not-participant"

    # Anti-flap guard (F7): a hash last observed unanswered flipping to
    # confirmed/tentative with no new message from "Me" is more likely stale
    # context re-analysis than a genuine acceptance — demote it back rather
    # than trust a single poll. A real acceptance always shows up as a new
    # message from the user, so this never blocks one.
    if status in ("confirmed", "tentative") and not new_msg_from_user:
        obs = state.get_observation(chat_id, date, time_start, title)
        if obs and obs.get("last_status") == "unanswered":
            logger.info(
                "Anti-flap: reverting %s->unanswered with no new user message: %s on %s",
                status, title, date,
            )
            status = "unanswered"

    # An invitation nobody has answered is not calendar-worthy yet. Its
    # observation IS recorded (unlike before F7), so a later flip to
    # confirmed/tentative with still no new user message can be caught above.
    if status == "unanswered":
        logger.info("Skipping unanswered invitation: %s on %s", title, date)
        state.record_observation(chat_id, date, time_start, title, "unanswered")
        return "skipped:unanswered"

    # Single confidence bar: tentative is a classification (the user explicitly
    # hedged), not a lower-confidence tier.
    if confidence < cfg["confidence_threshold"]:
        logger.info(
            "Skipping low-confidence %s event (%.2f < %.2f): %s",
            status, confidence, cfg["confidence_threshold"], title,
        )
        state.record_observation(chat_id, date, time_start, title, "low-confidence")
        return "skipped:low-confidence"

    # Cancellation (F4): a previously-established plan explicitly called off.
    # Never creates or updates through the normal reconcile action — it only
    # ever deletes an event the agent itself created (a calendar-only or
    # unowned match is left alone), and a detection with no match at all is a
    # no-op rather than a signal to create anything.
    if status == "cancelled":
        if not cfg.get("cancellation_enabled", True):
            logger.info("Cancellation detected but cancellation_enabled=False: %s on %s", title, date)
            return "skipped:cancellation-disabled"
        matched = reconcile.reconcile(event, cfg).matched
        if matched is None or "canonical_id" not in matched or not matched.get("calendar_uid"):
            logger.info("Cancellation detected but no agent-owned event matches: %s on %s", title, date)
            return "skipped:cancel-no-match"
        jid = state.journal_intent({"canonical_id": matched["canonical_id"]}, op="cancel")
        ok = calendar.delete_event(matched["calendar_uid"], calendar_name=cfg["target_calendar"])
        if not ok:
            logger.error("Failed to delete calendar event: %s", matched.get("title"))
            state.journal_drop(jid)
            return "skipped:cancel-failed"
        state.update_record(
            matched["canonical_id"], {"status": "cancelled"}, reason=evidence, chat_id=chat_id,
        )
        state.journal_commit(jid)
        logger.info(
            "Cancelled event %r (uid=%s): %s", matched.get("title"), matched["calendar_uid"], evidence,
        )
        return "cancelled"

    if time_start is not None and time_confidence < cfg["time_confidence_threshold"]:
        logger.info(
            "Demoting to all-day (time_confidence %.2f < %.2f): %s",
            time_confidence, cfg["time_confidence_threshold"], title,
        )
        time_start = None

    event = {**event, "time_start": time_start}
    decision = reconcile.reconcile(event, cfg)

    if decision.action == "skip_error":
        logger.warning("Skipping event after adjudicator failure (fail-closed): %s", title)
        return "skipped:reconcile-error"

    if decision.action == "skip_duplicate":
        matched_uid = decision.matched.get("calendar_uid") if decision.matched else None
        logger.info(
            "Reconcile (%s/%s): '%s' on %s duplicates existing event %r (uid=%s) — %s",
            decision.source, decision.relationship, title, date,
            decision.matched.get("title") if decision.matched else None,
            matched_uid, decision.reasoning,
        )
        # Exact-layer skips need no new record — the hash already covers this
        # wording. Fuzzy/LLM matches record a suppressed entry so the new
        # wording is exact-deduped next poll instead of re-adjudicated.
        if decision.source in ("fuzzy", "llm"):
            state.record_event(
                chat_id, date, time_start, title,
                location=location, status=status, evidence=evidence,
                confidence=confidence, suppressed=True, duplicate_of_uid=matched_uid,
            )
        return "skipped:duplicate"

    if decision.action == "update":
        matched = decision.matched
        if not cfg["reconcile_update_enabled"]:
            logger.info(
                "Reconcile update disabled; treating as duplicate of %r: %s",
                matched.get("title"), decision.changes,
            )
            state.record_event(
                chat_id, date, time_start, title,
                location=location, status=status, evidence=evidence,
                confidence=confidence, suppressed=True,
                duplicate_of_uid=matched.get("calendar_uid"),
            )
            return "skipped:duplicate"

        merged = {**matched, **decision.changes}
        jid = state.journal_intent(
            {"canonical_id": matched["canonical_id"], "changes": decision.changes},
            op="update",
        )
        if matched.get("calendar_uid"):
            ok = calendar.update_event(
                matched["calendar_uid"],
                title=merged["title"],
                date_str=merged["date"],
                time_start=merged.get("time_start"),
                duration_minutes=event.get("duration_minutes"),
                location=merged.get("location"),
                calendar_name=cfg["target_calendar"],
                tentative=merged.get("status") == "tentative",
                end_date=event.get("end_date"),
            )
            if not ok:
                logger.error("Failed to update calendar event: %s", merged["title"])
                state.journal_drop(jid)
                return "skipped:update-failed"
        state.update_record(
            matched["canonical_id"], decision.changes,
            reason=decision.reasoning, chat_id=chat_id,
        )
        state.journal_commit(jid)
        logger.info(
            "Updated event %r (uid=%s, %s): %s — %s",
            merged["title"], matched.get("calendar_uid"), decision.relationship,
            decision.changes, decision.reasoning,
        )
        return "updated"

    # decision.action == "create": journal the intent first so a crash between
    # the calendar write and the state write can't produce a duplicate.
    record = state.make_record(
        chat_id, date, time_start, title,
        location=location, status=status, evidence=evidence, confidence=confidence,
    )
    jid = state.journal_intent(record)
    uid = calendar.create_event(
        title=title,
        date_str=date,
        time_start=time_start,
        duration_minutes=event.get("duration_minutes"),
        location=location,
        calendar_name=cfg["target_calendar"],
        tentative=status == "tentative",
        recurrence=event.get("recurrence"),
        end_date=event.get("end_date"),
    )
    if uid is None:
        logger.error("Failed to create calendar event: %s", title)
        state.journal_drop(jid)
        return "skipped:create-failed"

    state.journal_commit(jid, uid)
    time_str = f" at {time_start}" if time_start else " (all-day)"
    loc_str = f" @ {location}" if location else ""
    logger.info(
        "Created %s event: %s — %s%s%s (confidence %.2f)",
        status, title, date, time_str, loc_str, confidence,
    )
    return "created"


def recover_journal(cfg: dict) -> None:
    """Resolve write-ahead journal entries left pending by a crash between the
    calendar write and the state write."""
    for entry in state.get_pending_journal():
        jid = entry.get("journal_id")
        if entry.get("op") != "create":
            # An interrupted update never landed in state; the next detection
            # re-reconciles and re-issues it (calendar updates are idempotent).
            logger.warning("Dropping interrupted journal %s entry", entry.get("op"))
            state.journal_drop(jid)
            continue

        record = entry.get("record") or {}
        title, date = record.get("title", ""), record.get("date")
        found_uid = None
        if cfg["calendar_query_enabled"] and date:
            wanted = state._normalize_title(title)
            for cal_event in calendar.get_events_near(date, 0, cfg["target_calendar"]):
                if state._normalize_title(cal_event["title"]) == wanted:
                    found_uid = cal_event["calendar_uid"]
                    break

        if found_uid is not None:
            logger.warning(
                "Recovered interrupted create: %r on %s exists on the calendar (uid=%s); committing",
                title, date, found_uid,
            )
            state.journal_commit(jid, found_uid)
        elif cfg["calendar_query_enabled"]:
            # Not on the calendar: the write never happened. Drop the entry so
            # re-detection recreates it through normal reconciliation.
            logger.warning("Dropping interrupted create that never reached the calendar: %r", title)
            state.journal_drop(jid)
        else:
            # Can't check the calendar — commit without a uid. Worst case a
            # failed write is suppressed, but a completed one can't duplicate.
            logger.warning(
                "Committing interrupted create without calendar verification: %r on %s",
                title, date,
            )
            state.journal_commit(jid, None)


def process_new_messages(cfg: dict) -> None:
    last_ts = state.get_last_timestamp()
    logger.info("─" * 60)
    logger.info("Polling for new messages (last_ts=%s)", last_ts)

    try:
        threads = reader.get_threads_since(
            last_apple_ts=last_ts,
            lookback_days=cfg["lookback_days"],
            blocked=cfg["blocked_contacts"],
            date_context_lookback_days=cfg["date_context_lookback_days"],
            date_context_max=cfg["date_context_max_messages"],
        )
    except RuntimeError as e:
        logger.error("%s", e)
        return

    if not threads:
        logger.info("No new threads to process")
        return

    logger.info("Found %d thread(s) with new messages", len(threads))

    events, failed_chats = detector.detect_plans(
        threads,
        evidence_gate=cfg["evidence_gate_enabled"],
        context_marking_enabled=cfg["context_marking_enabled"],
        date_resolver_enabled=cfg["date_resolver_enabled"],
    )

    counts = {"created": 0, "updated": 0, "cancelled": 0, "skipped": 0}
    for event in events:
        result = process_event(event, cfg)
        counts["skipped" if result.startswith("skipped") else result] += 1

    logger.info(
        "Done — %d created, %d updated, %d cancelled, %d skipped, %d threads processed",
        counts["created"],
        counts["updated"],
        counts["cancelled"],
        counts["skipped"],
        len(threads),
    )

    # Update the last-processed timestamp to the newest message we saw, unless
    # some thread's detection failed — hold the watermark so it's retried next
    # poll, up to a bounded number of retries to avoid looping on a poison thread.
    if threads:
        newest_ts = max(t["latest_apple_ts"] for t in threads)
        if not failed_chats:
            state.update_timestamp(newest_ts)
            state.set_watermark_hold(None, 0)
        else:
            hold = state.get_watermark_hold()
            same_position = hold.get("ts") == last_ts
            count = (hold.get("count", 0) + 1) if same_position else 1
            if count >= cfg["max_watermark_retries"]:
                logger.error(
                    "Giving up on failed thread(s) %s after %d retries; advancing watermark anyway",
                    failed_chats, count,
                )
                state.update_timestamp(newest_ts)
                state.set_watermark_hold(None, 0)
            else:
                logger.warning(
                    "Holding watermark for failed thread(s) %s (retry %d/%d)",
                    failed_chats, count, cfg["max_watermark_retries"],
                )
                state.set_watermark_hold(last_ts, count)


@contextlib.contextmanager
def _dry_run_environment():
    """Redirects state.py's persistence to a scratch temp directory (deleted
    on exit) and replaces calendar.py's AppleScript writes with logging
    no-ops, for the duration of the `with` block. Lets backfill() run the
    exact production code path (process_event / reconcile / state) against
    real historical messages with zero chance of touching the user's real
    state.json or Calendar.app.

    Reads (calendar.get_events_near) are stubbed to return nothing rather
    than routed to the real calendar — callers should also set
    cfg["calendar_query_enabled"] = False so this is belt-and-suspenders, not
    the only thing standing between backfill and a live calendar query.
    """
    scratch_dir = Path(tempfile.mkdtemp(prefix="scheduling-agent-backfill-"))
    real_state_dir = state.STATE_DIR
    real_state_file = state.STATE_FILE
    real_create = calendar.create_event
    real_update = calendar.update_event
    real_delete = calendar.delete_event
    real_get_near = calendar.get_events_near

    def fake_create(title, date_str, time_start, duration_minutes, location,
                     calendar_name="Calendar", tentative=False, recurrence=None, end_date=None):
        logger.info(
            "[DRY RUN] would create %s event: %s on %s%s%s",
            "tentative" if tentative else "confirmed", title, date_str,
            f" at {time_start}" if time_start else "",
            f" @ {location}" if location else "",
        )
        return f"dryrun-{_stable_id(title, date_str)}"

    def fake_update(uid, title, date_str, time_start, duration_minutes, location,
                     calendar_name="Calendar", tentative=False, end_date=None):
        logger.info("[DRY RUN] would update event %s: %s on %s", uid, title, date_str)
        return True

    def fake_delete(uid, calendar_name="Calendar"):
        logger.info("[DRY RUN] would delete event %s", uid)
        return True

    def fake_get_near(date_str, window_days=1, calendar_name="Calendar"):
        return []

    state.STATE_DIR = scratch_dir
    state.STATE_FILE = scratch_dir / "state.json"
    calendar.create_event = fake_create
    calendar.update_event = fake_update
    calendar.delete_event = fake_delete
    calendar.get_events_near = fake_get_near
    try:
        yield
    finally:
        state.STATE_DIR = real_state_dir
        state.STATE_FILE = real_state_file
        calendar.create_event = real_create
        calendar.update_event = real_update
        calendar.delete_event = real_delete
        calendar.get_events_near = real_get_near
        shutil.rmtree(scratch_dir, ignore_errors=True)


def _stable_id(*parts: str) -> str:
    """Deterministic fake calendar UID for dry-run creates, so the same event
    replayed across a re-run of the same backfill window gets the same id
    (harmless; nothing ever looks it up on a real calendar)."""
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


class BackfillResult(NamedTuple):
    """Return value of backfill(): out_path is the JSONL decision log;
    n_failed/first_failed_window flag whether coverage is incomplete (see
    backfill()'s docstring)."""
    out_path: Path
    n_events: int
    n_failed: int
    first_failed_window: date | None


def parse_backfill_since(value: str, now: datetime | None = None) -> datetime:
    """Parses --backfill's --since value: either a non-negative integer
    number of days ago, or an ISO date (YYYY-MM-DD). Raises ValueError on
    anything else, including a negative integer — `"-5".lstrip("-").isdigit()`
    is true, so without this check `int("-5")` silently computed a start
    date 5 days in the *future*, and since `until` defaults to now, the
    backfill loop's `while window_start < until` never ran, producing an
    empty log with no error explaining why."""
    now = now or datetime.now()
    if value.startswith("-"):
        raise ValueError(f"--since must be non-negative, got {value!r}")
    if value.isdigit():
        return now - timedelta(days=int(value))
    return datetime.strptime(value, "%Y-%m-%d")


def backfill(
    cfg: dict,
    since: datetime,
    until: datetime | None = None,
    window_days: float = 1.0,
    out_path: Path | None = None,
) -> BackfillResult:
    """Replay historical messages through the real detection + reconciliation
    pipeline in read-only dry-run mode, to sanity-check the agent's real-world
    behavior against message volume and diversity far beyond what shows up
    live in one person's inbox in a day.

    Walks [since, until) in `window_days`-sized windows, each anchored to its
    own end as "today" (both for the detector's relative-date resolution and
    process_event's past-event gate) so a message from months ago describing
    "next Tuesday" is judged as it would have been judged at the time, not
    against the real live wall clock. No calendar or state.json is touched —
    see _dry_run_environment.

    Returns a BackfillResult: out_path is the JSONL decision log written (one
    line per detected event, across all windows, plus one line per thread
    whose detection call itself failed); n_failed/first_failed_window flag
    whether coverage is incomplete.

    A detection call failing (a transport error, a bad API key, an exhausted
    credit balance) is NOT the same as "no plan in this thread," but
    detect_plans() can't tell the difference from inside a single window —
    it just returns fewer events. Left unchecked, a billing failure silently
    produces a normal-looking summary with a gap of zero events where a real
    stretch of history was in fact never analyzed. So every failed chat_id is
    logged, written to the JSONL as its own line (`"result": "detection_failed"`),
    and counted into `failed_threads` in the final summary — a nonzero count
    there means coverage is INCOMPLETE and the run should be repeated (e.g.
    `--since <the first failed window's date>`) once the underlying problem
    is fixed.
    """
    until = until or datetime.now()
    out_path = out_path or (
        BACKFILL_LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.jsonl"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    backfill_cfg = {**cfg, "calendar_query_enabled": False}

    counts: dict[str, int] = {}
    n_events = 0
    n_failed = 0
    first_failed_window: date | None = None
    window_start = since

    with _dry_run_environment():
        with open(out_path, "w") as f:
            while window_start < until:
                window_end = min(window_start + timedelta(days=window_days), until)
                start_ts = reader.unix_to_apple(window_start.timestamp())
                end_ts = reader.unix_to_apple(window_end.timestamp())

                try:
                    threads = reader.get_threads_since(
                        last_apple_ts=start_ts,
                        lookback_days=cfg["lookback_days"],
                        blocked=cfg["blocked_contacts"],
                        date_context_lookback_days=cfg["date_context_lookback_days"],
                        date_context_max=cfg["date_context_max_messages"],
                        end_apple_ts=end_ts,
                    )
                except RuntimeError as e:
                    logger.error("%s", e)
                    return BackfillResult(out_path, n_events, n_failed, first_failed_window)

                if threads:
                    logger.info(
                        "Backfill window %s..%s: %d thread(s)",
                        window_start.date(), window_end.date(), len(threads),
                    )
                    events, failed_chat_ids = detector.detect_plans(
                        threads,
                        today=window_end,
                        evidence_gate=cfg["evidence_gate_enabled"],
                        context_marking_enabled=cfg["context_marking_enabled"],
                        date_resolver_enabled=cfg["date_resolver_enabled"],
                    )
                    for event in events:
                        result = process_event(
                            event, backfill_cfg, reference_date=window_end.date()
                        )
                        counts[result] = counts.get(result, 0) + 1
                        n_events += 1
                        f.write(json.dumps({
                            "window_end": window_end.isoformat(),
                            "chat_id": event.get("chat_id"),
                            "title": event.get("title"),
                            "date": event.get("date"),
                            "status": event.get("status"),
                            "confidence": event.get("confidence"),
                            "evidence": event.get("evidence"),
                            "result": result,
                        }) + "\n")

                    if failed_chat_ids:
                        if first_failed_window is None:
                            first_failed_window = window_start.date()
                        logger.warning(
                            "Backfill window %s..%s: detection call FAILED for %d "
                            "thread(s) (%s) — coverage for this window is incomplete, "
                            "not \"no plan found\"",
                            window_start.date(), window_end.date(),
                            len(failed_chat_ids), sorted(failed_chat_ids),
                        )
                        for chat_id in failed_chat_ids:
                            n_failed += 1
                            f.write(json.dumps({
                                "window_end": window_end.isoformat(),
                                "chat_id": chat_id,
                                "result": "detection_failed",
                            }) + "\n")

                window_start = window_end

    logger.info(
        "Backfill done — %d event(s), %d failed detection call(s) across %s: %s",
        n_events, n_failed, out_path, counts,
    )
    if n_failed:
        logger.warning(
            "%d detection call(s) failed starting at window %s — coverage from "
            "there onward is INCOMPLETE (a billing/API problem looks identical to "
            "\"no plans found\" unless you check this). Fix the underlying issue and "
            "re-run, e.g. --since %s",
            n_failed, first_failed_window, first_failed_window,
        )
    return BackfillResult(out_path, n_events, n_failed, first_failed_window)


LAUNCHD_LOG_DIR = Path.home() / "Library" / "Logs" / "scheduling-agent"


def purge() -> None:
    """Delete all local state (canonical event store, journal, watermark) and
    logs. Does not touch anything on the Calendar or in Messages."""
    removed = []
    if state.STATE_FILE.exists():
        state.STATE_FILE.unlink()
        removed.append(str(state.STATE_FILE))
    if LOGS_DIR.parent.exists():
        shutil.rmtree(LOGS_DIR.parent)
        removed.append(str(LOGS_DIR.parent))
    # launchd redirects raw stdout/stderr here (see install-launchagent.sh);
    # it's a separate, unrotated log containing the same truncated-but-still
    # evidence-bearing lines as logs/stdout/, so it must be purged too.
    if LAUNCHD_LOG_DIR.exists():
        shutil.rmtree(LAUNCHD_LOG_DIR)
        removed.append(str(LAUNCHD_LOG_DIR))

    if removed:
        print("Removed:")
        for path in removed:
            print(f"  {path}")
    else:
        print("Nothing to remove.")


class _RunGate:
    """Serializes runs of `run_fn` triggered by the filesystem watcher and the
    poll-fallback timer, since state._save() is a non-atomic write with no
    locking of its own — concurrent runs could corrupt state.json or double
    up a calendar event.

    `blocking()` waits for the lock (used by the watcher, which should never
    silently drop a detected change). `skip_if_busy()` does not block: if a
    run is already in progress, the tick is skipped entirely rather than
    queued, since the next scheduled tick will pick up any new messages.
    """

    def __init__(self, run_fn):
        self._run_fn = run_fn
        self._lock = threading.Lock()

    def blocking(self) -> None:
        with self._lock:
            self._run_fn()

    def skip_if_busy(self) -> None:
        if not self._lock.acquire(blocking=False):
            logger.info("Poll fallback skipping tick; a run is already in progress")
            return
        try:
            self._run_fn()
        finally:
            self._lock.release()


class _PollTimer:
    """Repeating backstop timer that re-runs `callback` every
    `interval_minutes`, independent of the filesystem watcher, in case a
    chat.db change event is ever missed."""

    def __init__(self, callback, interval_minutes: float):
        self._callback = callback
        self._interval = interval_minutes * 60
        self._timer: threading.Timer | None = None

    def start(self) -> None:
        self._schedule()

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()

    def _schedule(self) -> None:
        self._timer = threading.Timer(self._interval, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self) -> None:
        try:
            self._callback()
        except Exception:
            logger.exception("Error in poll-fallback callback")
        finally:
            self._schedule()


def main() -> None:
    parser = argparse.ArgumentParser(prog="scheduling-agent")
    parser.add_argument(
        "--purge", action="store_true",
        help="Delete local state and logs (~/.scheduling-agent/state.json and ./logs), then exit.",
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help=(
            "Replay historical messages through the real detector in "
            "read-only dry-run mode (no calendar or state.json writes) — for "
            "sanity-checking behavior against real message volume beyond "
            "what shows up live in one day. Requires --since."
        ),
    )
    parser.add_argument(
        "--since",
        help="Backfill start: an integer number of days ago, or an ISO date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--until",
        help="Backfill end: an ISO date (YYYY-MM-DD). Default: now.",
    )
    parser.add_argument(
        "--window-days", type=float, default=1.0,
        help=(
            "Size of each backfill window in days (default 1). Each window is "
            "anchored to its own end as \"today\" for relative-date resolution "
            "and the past-event gate — smaller windows judge old messages "
            "more like they'd have been judged at the time they were sent."
        ),
    )
    parser.add_argument(
        "--out",
        help="Path to write the backfill JSONL decision log (default logs/backfill/<timestamp>.jsonl).",
    )
    args = parser.parse_args()

    if args.purge:
        purge()
        return

    if args.backfill:
        if not args.since:
            parser.error("--backfill requires --since (an integer number of days, or an ISO date)")
        setup_logging()
        cfg = config.load()
        try:
            since_dt = parse_backfill_since(args.since)
        except ValueError:
            parser.error(f"--since must be a non-negative integer number of days or an ISO date, got {args.since!r}")
            return
        until_dt = None
        if args.until:
            try:
                until_dt = datetime.strptime(args.until, "%Y-%m-%d")
            except ValueError:
                parser.error(f"--until must be an ISO date (YYYY-MM-DD), got {args.until!r}")
                return
        out_path = Path(args.out) if args.out else None
        logger.info(
            "Backfilling from %s to %s in %s-day windows (dry run — no calendar/state writes)",
            since_dt.date(), (until_dt or datetime.now()).date(), args.window_days,
        )
        result = backfill(
            cfg, since=since_dt, until=until_dt, window_days=args.window_days, out_path=out_path,
        )
        print(f"Backfill decision log written to {result.out_path} ({result.n_events} event(s))")
        if result.n_failed:
            print(
                f"WARNING: {result.n_failed} detection call(s) failed starting at "
                f"{result.first_failed_window} — coverage from there onward is "
                f"INCOMPLETE (see 'detection_failed' lines in the log and the "
                f"WARNING lines above). Fix the underlying issue (e.g. an exhausted "
                f"API credit balance) and re-run with --since {result.first_failed_window}."
            )
        return

    setup_logging()
    cfg = config.load()
    logger.info("Scheduling agent starting (calendar=%s)", cfg["target_calendar"])

    # Resolve any calendar writes interrupted by a crash, then run once.
    recover_journal(cfg)
    process_new_messages(cfg)

    def run() -> None:
        cfg_fresh = config.load()
        process_new_messages(cfg_fresh)

    gate = _RunGate(run)
    observer = watcher.watch(gate.blocking, debounce_seconds=5.0)

    poll_timer = None
    poll_interval = cfg.get("poll_interval_minutes") or 0
    if poll_interval > 0:
        poll_timer = _PollTimer(gate.skip_if_busy, poll_interval)
        poll_timer.start()
        logger.info("Poll fallback active every %s minute(s)", poll_interval)

    def shutdown(sig, frame):
        logger.info("Shutting down...")
        observer.stop()
        if poll_timer is not None:
            poll_timer.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info("Watching for new iMessages. Press Ctrl+C to stop.")
    while observer.is_alive():
        time.sleep(1)


if __name__ == "__main__":
    main()

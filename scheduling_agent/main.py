import logging
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from . import calendar, config, detector, reader, reconcile, state, watcher

LOGS_DIR = Path(__file__).parent.parent / "logs" / "stdout"

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

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    logger.info("Logging to %s", log_file)


def process_event(event: dict, cfg: dict) -> str:
    """Run one detected event through the gate sequence and reconciliation.

    Returns "created", "updated", or "skipped:<reason>". Shared by the polling
    loop and the eval harness so both exercise the exact production gates.
    """
    chat_id = event["chat_id"]
    title = event["title"]
    date = event["date"]
    time_start = event.get("time_start")
    time_confidence = event.get("time_confidence") or 0
    location = event.get("location")
    confidence = event["confidence"]
    status = event.get("status", "confirmed")
    evidence = event.get("evidence")

    try:
        event_date = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        logger.warning("Skipping event with unparseable date %r: %s", date, title)
        return "skipped:unparseable-date"

    if event_date < datetime.now().date():
        logger.info("Skipping past event: %s on %s", title, date)
        return "skipped:past"

    # Hard ownership gate: plans the user isn't personally part of never touch
    # the calendar, no matter how confident or confirmed they are.
    if not event.get("user_is_participant"):
        logger.info(
            "Skipping non-participant plan: %s on %s — %s",
            title, date, event.get("participation_evidence"),
        )
        return "skipped:not-participant"

    # An invitation nobody has answered is not calendar-worthy yet. It is not
    # recorded either, so a later acceptance re-detects and creates it.
    if status == "unanswered":
        logger.info("Skipping unanswered invitation: %s on %s", title, date)
        return "skipped:unanswered"

    # Single confidence bar: tentative is a classification (the user explicitly
    # hedged), not a lower-confidence tier.
    if confidence < cfg["confidence_threshold"]:
        logger.info(
            "Skipping low-confidence %s event (%.2f < %.2f): %s",
            status, confidence, cfg["confidence_threshold"], title,
        )
        return "skipped:low-confidence"

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
            "Reconcile (%s): '%s' on %s duplicates existing event %r (uid=%s) — %s",
            decision.source, title, date,
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
            "Updated event %r (uid=%s): %s — %s",
            merged["title"], matched.get("calendar_uid"), decision.changes, decision.reasoning,
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
        )
    except RuntimeError as e:
        logger.error("%s", e)
        return

    if not threads:
        logger.info("No new threads to process")
        return

    logger.info("Found %d thread(s) with new messages", len(threads))

    events, failed_chats = detector.detect_plans(
        threads, evidence_gate=cfg["evidence_gate_enabled"]
    )

    counts = {"created": 0, "updated": 0, "skipped": 0}
    for event in events:
        result = process_event(event, cfg)
        counts["skipped" if result.startswith("skipped") else result] += 1

    logger.info(
        "Done — %d created, %d updated, %d skipped, %d threads processed",
        counts["created"],
        counts["updated"],
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


def main() -> None:
    setup_logging()
    cfg = config.load()
    logger.info("Scheduling agent starting (calendar=%s)", cfg["target_calendar"])

    # Resolve any calendar writes interrupted by a crash, then run once.
    recover_journal(cfg)
    process_new_messages(cfg)

    # Then watch for future changes
    def on_change():
        cfg_fresh = config.load()
        process_new_messages(cfg_fresh)

    observer = watcher.watch(on_change, debounce_seconds=5.0)

    def shutdown(sig, frame):
        logger.info("Shutting down...")
        observer.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info("Watching for new iMessages. Press Ctrl+C to stop.")
    while observer.is_alive():
        time.sleep(1)


if __name__ == "__main__":
    main()

import logging
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from . import calendar, config, dedup, detector, reader, state, watcher

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

    events, failed_chats = detector.detect_plans(threads)

    created_count = 0
    skipped_count = 0

    for event in events:
        chat_id = event["chat_id"]
        title = event["title"]
        date = event["date"]
        time_start = event.get("time_start")
        time_confidence = event.get("time_confidence") or 0
        location = event.get("location")
        confidence = event["confidence"]
        status = event.get("status", "confirmed")
        evidence = event.get("evidence")
        is_tentative = status == "tentative"

        threshold = (
            cfg["tentative_confidence_threshold"] if is_tentative
            else cfg["confidence_threshold"]
        )
        if confidence < threshold:
            logger.info(
                "Skipping low-confidence %s event (%.2f < %.2f): %s",
                status,
                confidence,
                threshold,
                title,
            )
            skipped_count += 1
            continue

        try:
            event_date = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            logger.warning("Skipping event with unparseable date %r: %s", date, title)
            skipped_count += 1
            continue

        if event_date < datetime.now().date():
            logger.info("Skipping past event: %s on %s", title, date)
            skipped_count += 1
            continue

        if time_start is not None and time_confidence < cfg["time_confidence_threshold"]:
            logger.info(
                "Demoting to all-day (time_confidence %.2f < %.2f): %s",
                time_confidence,
                cfg["time_confidence_threshold"],
                title,
            )
            time_start = None

        if state.is_duplicate(chat_id, date, time_start, title):
            logger.info("Skipping duplicate: %s on %s", title, date)
            skipped_count += 1
            continue

        if cfg["dedup_enabled"]:
            candidates = dedup.find_candidates(
                {"date": date, "_hash": state.event_hash(chat_id, date, time_start, title)},
                state.get_events_near(date, cfg["dedup_day_window"]),
                day_window=cfg["dedup_day_window"],
            )
            if candidates:
                event_for_adjudication = {**event, "date": date, "time_start": time_start}
                verdict = dedup.adjudicate(event_for_adjudication, candidates, model=cfg["dedup_model"])
                if verdict is None and not cfg["dedup_fail_open"]:
                    logger.warning("Skipping event after adjudicator failure (fail-closed): %s", title)
                    skipped_count += 1
                    continue
                if verdict and verdict.get("is_duplicate"):
                    logger.info(
                        "LLM dedup: '%s' on %s duplicates an existing event — %s",
                        title, date, verdict.get("reasoning"),
                    )
                    state.record_event(
                        chat_id, date, time_start, title,
                        location=location, status=status, evidence=evidence,
                        suppressed=True,
                    )
                    skipped_count += 1
                    continue

        uid = calendar.create_event(
            title=title,
            date_str=date,
            time_start=time_start,
            duration_minutes=event.get("duration_minutes"),
            location=location,
            calendar_name=cfg["target_calendar"],
            tentative=is_tentative,
            recurrence=event.get("recurrence"),
            end_date=event.get("end_date"),
        )

        if uid is not None:
            state.record_event(
                chat_id, date, time_start, title,
                location=location, status=status, evidence=evidence,
                calendar_uid=uid,
            )
            time_str = f" at {time_start}" if time_start else " (all-day)"
            loc_str = f" @ {location}" if location else ""
            logger.info(
                "Created %s event: %s — %s%s%s (confidence %.2f)",
                status,
                title,
                date,
                time_str,
                loc_str,
                confidence,
            )
            created_count += 1
        else:
            logger.error("Failed to create calendar event: %s", title)

    logger.info(
        "Done — %d created, %d skipped, %d threads processed",
        created_count,
        skipped_count,
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

    # Run once immediately on startup
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

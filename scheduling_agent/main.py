import logging
import signal
import sys
import time

from . import calendar, config, detector, reader, state, watcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def process_new_messages(cfg: dict) -> None:
    last_ts = state.get_last_timestamp()
    logger.info("Checking for new messages (last_ts=%s)", last_ts)

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

    events = detector.detect_plans(threads)

    for event in events:
        if event["confidence"] < cfg["confidence_threshold"]:
            logger.info(
                "Skipping low-confidence event (%.2f): %s",
                event["confidence"],
                event["title"],
            )
            continue

        chat_id = event["chat_id"]
        title = event["title"]
        date = event["date"]
        time_start = event.get("time_start")

        if state.is_duplicate(chat_id, date, time_start, title):
            logger.info("Skipping duplicate event: %s on %s", title, date)
            continue

        created = calendar.create_event(
            title=title,
            date_str=date,
            time_start=time_start,
            duration_minutes=event.get("duration_minutes"),
            location=event.get("location"),
            calendar_name=cfg["target_calendar"],
        )

        if created:
            state.record_event(chat_id, date, time_start, title)
            print(f"  ✓ Created: {title} on {date}")
        else:
            logger.error("Failed to create calendar event: %s", title)

    # Update the last-processed timestamp to the newest message we saw
    if threads:
        newest_ts = max(t["latest_apple_ts"] for t in threads)
        state.update_timestamp(newest_ts)


def main() -> None:
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

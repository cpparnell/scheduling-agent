import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".scheduling-agent"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS = {
    "blocked_contacts": [],
    "target_calendar": "Calendar",
    "lookback_days": 7,
    "confidence_threshold": 0.85,
    "tentative_confidence_threshold": 0.6,
    # Only keep a specific time_start when the detector's time_confidence
    # meets this bar; otherwise the event is demoted to all-day.
    "time_confidence_threshold": 0.9,
    # LLM-adjudicated dedup: before creating an event, check nearby existing
    # events (see dedup.py) and skip if the adjudicator judges it a duplicate.
    "dedup_enabled": True,
    "dedup_model": "claude-haiku-4-5",
    "dedup_day_window": 1,
    # If the adjudicator call itself fails, create the event rather than risk
    # silently dropping a real plan (a visible duplicate is easy to fix).
    "dedup_fail_open": True,
    # How many consecutive polls to hold the watermark back for a thread that
    # keeps failing detection, before giving up and advancing past it anyway.
    "max_watermark_retries": 3,
}


def load() -> dict:
    CONFIG_DIR.mkdir(exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULTS, indent=2))
        return dict(DEFAULTS)
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Could not read %s (%s); using defaults", CONFIG_FILE, e)
        return dict(DEFAULTS)
    if not isinstance(data, dict):
        logger.error("%s is not a JSON object; using defaults", CONFIG_FILE)
        return dict(DEFAULTS)
    return {**DEFAULTS, **data}

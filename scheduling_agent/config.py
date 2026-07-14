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
    # Only keep a specific time_start when the detector's time_confidence
    # meets this bar; otherwise the event is demoted to all-day.
    "time_confidence_threshold": 0.9,
    # LLM-adjudicated dedup: the last reconciliation layer (see reconcile.py)
    # for detections the deterministic layers couldn't match.
    "dedup_enabled": True,
    "dedup_model": "claude-haiku-4-5",
    "dedup_day_window": 1,
    # If the adjudicator call itself fails, create the event rather than risk
    # silently dropping a real plan (a visible duplicate is easy to fix).
    "dedup_fail_open": True,
    # Include events read back from the target calendar as reconciliation
    # candidates (catches manually created events and lost state).
    "calendar_query_enabled": True,
    # Minimum normalized-title token overlap for the deterministic fuzzy
    # reconciliation layer to declare a match without consulting the LLM.
    "fuzzy_title_threshold": 0.6,
    # Drop detected events whose quoted evidence isn't found verbatim in the
    # thread (hallucination guard). Disable to log-and-keep instead.
    "evidence_gate_enabled": True,
    # Allow reconciliation matches to update the existing calendar event
    # (reschedules, added locations). Disable to treat updates as skips.
    "reconcile_update_enabled": True,
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

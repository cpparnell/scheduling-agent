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

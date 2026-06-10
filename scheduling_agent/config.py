import json
import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".scheduling-agent"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS = {
    "blocked_contacts": [],
    "target_calendar": "Calendar",
    "lookback_days": 7,
    "confidence_threshold": 0.85,
}


def load() -> dict:
    CONFIG_DIR.mkdir(exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULTS, indent=2))
        return dict(DEFAULTS)
    with open(CONFIG_FILE) as f:
        data = json.load(f)
    return {**DEFAULTS, **data}

"""Where the private corpus lives. Nothing here is ever committed."""

import os
from pathlib import Path

# Override for tests and smoke runs, so they never touch the real corpus.
CORPUS_DIR = Path(os.environ.get("BASELINE_CORPUS_DIR") or Path.home() / ".scheduling-agent" / "corpus")
RUN_META = CORPUS_DIR / "run.json"            # backfill parameters, cost, validity
DETECTIONS = CORPUS_DIR / "v014_detections.jsonl"  # backfill decision log
EPISODES = CORPUS_DIR / "episodes.jsonl"      # labeling queue, fixed shuffled order
LABELS = CORPUS_DIR / "labels.jsonl"          # append-only; last line per episode wins
REPORT = CORPUS_DIR / "baseline_report.json"


def ensure() -> None:
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)

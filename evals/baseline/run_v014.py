"""Replay the last N days of chat.db through v0.14 as a dry run (nothing is
written to Calendar.app or state.json) and record what it would have done.

Windows are aligned to local midnight so each backfill window is one calendar
day, which is what makes "episode = chat x day" line up with detections.

The whole range runs as ONE backfill call: backfill keeps its reconciliation
state in a scratch dir that is deleted when the call returns, so splitting the
range into chunks would forget earlier events at every chunk boundary and
inflate duplicates.

Cost guard: a 7-day preflight runs first and its cost is projected to the full
range; the full run only starts when the projection is under the cap.
"""

import _thread
import json
import logging
import subprocess
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from scheduling_agent import config, usage_tracker
from scheduling_agent.main import backfill

from evals.baseline import paths

logger = logging.getLogger(__name__)

PREFLIGHT_DAYS = 7


@dataclass
class RunOutcome:
    valid: bool
    cost_usd: float
    n_events: int
    n_failed: int
    out_path: Path


class KeyCheckFailed(RuntimeError):
    pass


class RunAborted(RuntimeError):
    pass


class _Guard:
    """Stops a backfill early instead of letting it grind on. backfill() has no
    hook for this, so a watcher thread interrupts the main thread on the first
    failed API call (dead key, exhausted credit: the rest of the run would be
    invalid anyway) or once spend passes the budget. The interrupt unwinds
    through backfill's context managers, so its scratch state is still cleaned up."""

    def __init__(self, budget_usd: float) -> None:
        self.budget = budget_usd
        self.reason: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True)

    def _watch(self) -> None:
        while not self._stop.wait(1.0):
            s = usage_tracker.summary()
            if int(s.get("failed_calls", 0)) > 0:
                failures = usage_tracker.get_failures()
                first = failures[0] if failures else ""
                self.reason = f"an API call failed: {first[:300]}"
            elif float(s.get("total_cost_usd", 0.0)) > self.budget:
                self.reason = f"spend passed the ${self.budget:.2f} budget"
            if self.reason:
                _thread.interrupt_main()
                return

    def __enter__(self) -> "_Guard":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()


def check_keys(cfg: dict[str, object]) -> None:
    """Fail fast, before any spend, when a backend's key is rejected. The
    default config calls both Jev (detector) and Claude (second looks, dedup)."""
    import anthropic  # local: keep import cost off `score`/`label`

    from scheduling_agent import detector, jev_client

    try:
        anthropic.Anthropic().messages.create(
            model=detector.MODEL,
            max_tokens=1, messages=[{"role": "user", "content": "ping"}],
        )
    except anthropic.AuthenticationError as e:
        raise KeyCheckFailed(
            "ANTHROPIC_API_KEY in .env is rejected (401). Replace it with a valid key, then re-run."
        ) from e
    if cfg.get("detector_backend") == "jev" and not jev_client.has_api_key():
        raise KeyCheckFailed("TYPESAFE_API_KEY is not set in .env.")


def _midnight(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time())


def _one(cfg: dict[str, object], since: date, until: date, out: Path, budget_usd: float) -> RunOutcome:
    usage_tracker.reset()
    guard = _Guard(budget_usd)
    try:
        with guard:
            r = backfill(cfg, _midnight(since), _midnight(until), window_days=1.0, out_path=out)
    except KeyboardInterrupt:
        if guard.reason is None:
            raise
        spent = float(usage_tracker.summary().get("total_cost_usd", 0.0))
        raise RunAborted(f"Stopped early: {guard.reason} (spent ${spent:.3f}).") from None
    s = usage_tracker.summary()
    cost = float(s.get("total_cost_usd", 0.0))
    failed_calls = int(s.get("failed_calls", 0))
    return RunOutcome(r.n_failed == 0 and failed_calls == 0, cost, r.n_events, r.n_failed, out)


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def run(days: int, cap_usd: float, until: date | None = None) -> RunOutcome:
    paths.ensure()
    cfg = config.load()
    check_keys(cfg)
    until = until or date.today()
    since = until - timedelta(days=days)

    pre = _one(cfg, until - timedelta(days=PREFLIGHT_DAYS), until, paths.CORPUS_DIR / "preflight.jsonl", cap_usd)
    if not pre.valid:
        raise RuntimeError(
            f"Preflight had failed API calls ({pre.n_failed} failed threads); fix the backend and re-run."
        )
    projected = pre.cost_usd * days / PREFLIGHT_DAYS
    print(f"Preflight: {PREFLIGHT_DAYS} days cost ${pre.cost_usd:.3f}; projected {days} days ≈ ${projected:.2f}", flush=True)
    if projected > cap_usd:
        raise RuntimeError(
            f"Projected cost ${projected:.2f} exceeds the cap ${cap_usd:.2f}. "
            "Re-run with a higher --cap or fewer --days."
        )

    paths.RUN_META.unlink(missing_ok=True)  # a stale valid run must not survive a failed re-run
    full = _one(cfg, since, until, paths.DETECTIONS, cap_usd - pre.cost_usd)
    meta = {
        "since": since.isoformat(), "until": until.isoformat(), "days": days,
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "git_sha": _git_sha(), "detector_backend": cfg.get("detector_backend"),
        "valid": full.valid, "cost_usd": round(full.cost_usd + pre.cost_usd, 4),
        "n_events": full.n_events, "n_failed_threads": full.n_failed,
    }
    paths.RUN_META.write_text(json.dumps(meta, indent=1))
    return full


def load_meta() -> dict[str, object] | None:
    if not paths.RUN_META.exists():
        return None
    data: dict[str, object] = json.loads(paths.RUN_META.read_text())
    return data

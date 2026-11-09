"""Label schema. labels.jsonl is append-only; the last line for an episode
wins, so re-labeling never needs a rewrite and a crash loses one episode."""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

# What the labeler says about one v0.14 detection.
VERDICTS = (
    "correct",     # a real Commitment, right day (and time, if it had one)
    "wrong_date",  # a real Commitment on the wrong day
    "wrong_time",  # right day, wrong time
    "not_mine",    # a real plan, but not one the owner is in on
    "not_a_plan",  # nothing was planned (hallucinated or a non-plan)
    "duplicate",   # real and the owner's, but it repeats one already recorded on an earlier day
    "unsure",
)


@dataclass
class DetectionLabel:
    idx: int
    verdict: str
    correct_date: str | None = None
    correct_time: str | None = None
    note: str | None = None


@dataclass
class MissedCommitment:
    title: str
    date: str
    time: str | None = None
    note: str | None = None


@dataclass
class EpisodeLabel:
    episode_id: str
    detections: list[DetectionLabel] = field(default_factory=list)
    missed: list[MissedCommitment] = field(default_factory=list)
    note: str | None = None
    labeled_at: str = ""


class LabelError(ValueError):
    pass


def parse(raw: dict[str, object]) -> EpisodeLabel:
    """Validate a label posted by the UI."""
    eid = raw.get("episode_id")
    if not isinstance(eid, str) or ":" not in eid:
        raise LabelError("episode_id missing")
    dets: list[DetectionLabel] = []
    raw_dets = raw.get("detections", [])
    if not isinstance(raw_dets, list):
        raise LabelError("detections must be a list")
    for d in raw_dets:
        if not isinstance(d, dict):
            raise LabelError("bad detection label")
        idx, verdict = d.get("idx"), d.get("verdict")
        if not isinstance(idx, int) or verdict not in VERDICTS:
            raise LabelError(f"bad detection label: {d}")
        cd = _opt(d.get("correct_date"))
        if verdict == "wrong_date" and not cd:
            raise LabelError("wrong_date needs the correct date")
        dets.append(DetectionLabel(idx, str(verdict), cd, _opt(d.get("correct_time")), _opt(d.get("note"))))
    missed: list[MissedCommitment] = []
    raw_missed = raw.get("missed", [])
    if not isinstance(raw_missed, list):
        raise LabelError("missed must be a list")
    for m in raw_missed:
        if not isinstance(m, dict):
            raise LabelError("bad missed entry")
        title, dt = _opt(m.get("title")), _opt(m.get("date"))
        if not title or not dt:
            raise LabelError("a missed Commitment needs a title and a date")
        missed.append(MissedCommitment(title, dt, _opt(m.get("time")), _opt(m.get("note"))))
    return EpisodeLabel(eid, dets, missed, _opt(raw.get("note")))


def _opt(v: object) -> str | None:
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def append(label: EpisodeLabel, path: Path) -> None:
    label.labeled_at = datetime.now().isoformat(timespec="seconds")
    with open(path, "a") as f:
        f.write(json.dumps(asdict(label)) + "\n")


def load(path: Path) -> dict[str, EpisodeLabel]:
    out: dict[str, EpisodeLabel] = {}
    if not path.exists():
        return out
    with open(path) as f:
        for line in f:
            d = json.loads(line)
            out[d["episode_id"]] = EpisodeLabel(
                episode_id=d["episode_id"],
                detections=[DetectionLabel(**x) for x in d["detections"]],
                missed=[MissedCommitment(**x) for x in d["missed"]],
                note=d.get("note"),
                labeled_at=d.get("labeled_at", ""),
            )
    return out

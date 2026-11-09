"""Build the labeling queue.

The labeling unit is an **episode**: one chat on one calendar day. That matches
the backfill, which runs in midnight-aligned one-day windows (see run_v014.py),
so every v0.14 detection belongs to exactly one episode.

Every episode with messages falls into one stratum:
  D  v0.14 detected at least one plan in it (where false positives,
     wrong days and not-mine plans live)
  S  no detection, but a message carries a plan signal (signals.py), which is
     where most misses should be
  N  neither (assumed empty; a thin sample checks that assumption)

The queue interleaves the strata in a fixed seeded order, so any prefix the
labeler finishes is a stratified random sample. score.py extrapolates each
stratum by its population size, so labeling can stop at any point.
"""

import json
import random
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TypedDict

from scheduling_agent import reader

from evals.baseline.signals import plan_signal

SEED = 14
# Out of every 10 queue slots: 5 from D, 4 from S, 1 from N.
PATTERN = ["D", "S", "D", "S", "D", "S", "D", "S", "D", "N"]
USER_ACCEPT_TAPBACKS = (2000, 2001)  # love, like — sent by the user


class Detection(TypedDict):
    idx: int
    title: str | None
    date: str | None
    status: str | None
    confidence: float | None
    evidence: str | None
    result: str


@dataclass
class Episode:
    id: str
    chat_id: int
    day: str  # YYYY-MM-DD
    stratum: str
    n_messages: int
    n_signal: int
    detections: list[Detection] = field(default_factory=list)


@dataclass
class _DayStats:
    n_messages: int = 0
    n_signal: int = 0


def episode_id(chat_id: int, day: date) -> str:
    return f"{chat_id}:{day.isoformat()}"


def detection_day(window_end_iso: str) -> date:
    """The episode day for a backfill line. Windows are [day 00:00, day+1 00:00),
    and the backfill logs each line with its window's end."""
    return (datetime.fromisoformat(window_end_iso) - timedelta(days=1)).date()


def load_detections(path: Path) -> dict[str, list[Detection]]:
    """Group the backfill decision log by episode id. A `detection_failed` line
    is not a detection; run_v014 refuses to mark such a run valid anyway."""
    by_episode: dict[str, list[Detection]] = {}
    if not path.exists():
        return by_episode
    with open(path) as f:
        for line in f:
            row: dict[str, object] = json.loads(line)
            result = str(row.get("result"))
            if result == "detection_failed":
                continue
            chat_id = row.get("chat_id")
            window_end = row.get("window_end")
            if not isinstance(chat_id, int) or not isinstance(window_end, str):
                continue
            eid = episode_id(chat_id, detection_day(window_end))
            lst = by_episode.setdefault(eid, [])
            conf = row.get("confidence")
            lst.append(Detection(
                idx=len(lst),
                title=_opt_str(row.get("title")),
                date=_opt_str(row.get("date")),
                status=_opt_str(row.get("status")),
                confidence=float(conf) if isinstance(conf, (int, float)) else None,
                evidence=_opt_str(row.get("evidence")),
                result=result,
            ))
    return by_episode


def _opt_str(v: object) -> str | None:
    return v if isinstance(v, str) else None


def scan_days(since: date, until: date, blocked: Iterable[str]) -> dict[str, _DayStats]:
    """Message and signal counts per episode for [since, until), straight from
    chat.db (read-only). Day boundaries are local time, like the backfill's."""
    start = reader.unix_to_apple(datetime.combine(since, datetime.min.time()).timestamp())
    end = reader.unix_to_apple(datetime.combine(until, datetime.min.time()).timestamp())
    conn = sqlite3.connect(f"file:{reader.CHAT_DB}?mode=ro", uri=True, timeout=5)
    try:
        blocked_chats = _blocked_chat_ids(conn, set(blocked))
        rows = conn.execute(
            """
            SELECT cmj.chat_id, m.date, m.text, m.attributedBody, m.is_from_me,
                   COALESCE(m.associated_message_type, 0)
            FROM message m JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            WHERE m.date >= ? AND m.date < ?
            """,
            (start, end),
        ).fetchall()
    finally:
        conn.close()

    stats: dict[str, _DayStats] = {}
    for chat_id, apple_ts, text, body, from_me, tapback in rows:
        if chat_id in blocked_chats:
            continue
        day = datetime.fromtimestamp(reader.apple_to_unix(apple_ts)).date()
        is_accept = bool(from_me) and tapback in USER_ACCEPT_TAPBACKS
        if tapback and not is_accept:
            continue
        body_text = text or reader._decode_attributed_body(body)
        if not body_text and not is_accept:
            continue
        s = stats.setdefault(episode_id(chat_id, day), _DayStats())
        s.n_messages += 1
        if is_accept or (body_text and plan_signal(body_text)):
            s.n_signal += 1
    return stats


def _blocked_chat_ids(conn: sqlite3.Connection, blocked: set[str]) -> set[int]:
    if not blocked:
        return set()
    out: set[int] = set()
    for chat_id, handle in conn.execute(
        "SELECT chj.chat_id, h.id FROM chat_handle_join chj JOIN handle h ON chj.handle_id = h.ROWID"
    ):
        if handle in blocked:
            out.add(chat_id)
    return out


def build(
    stats: dict[str, _DayStats], detections: dict[str, list[Detection]], seed: int = SEED,
) -> list[Episode]:
    """Assign strata and return the queue in its fixed interleaved order."""
    strata: dict[str, list[Episode]] = {"D": [], "S": [], "N": []}
    for eid in sorted(set(stats) | set(detections)):
        chat, day = eid.split(":")
        s = stats.get(eid, _DayStats())
        dets = detections.get(eid, [])
        stratum = "D" if dets else "S" if s.n_signal else "N"
        strata[stratum].append(Episode(eid, int(chat), day, stratum, s.n_messages, s.n_signal, dets))
    rng = random.Random(seed)
    for lst in strata.values():
        rng.shuffle(lst)
    return list(interleave(strata))


def interleave(strata: dict[str, list[Episode]]) -> Iterator[Episode]:
    """Walk PATTERN repeatedly, taking the next episode from each named
    stratum; an exhausted stratum's slots are skipped, so the tail of the
    queue is whatever strata remain."""
    cursors = {k: 0 for k in strata}
    remaining = sum(len(v) for v in strata.values())
    while remaining:
        for k in PATTERN:
            if cursors[k] < len(strata[k]):
                yield strata[k][cursors[k]]
                cursors[k] += 1
                remaining -= 1


def write(episodes: list[Episode], path: Path) -> None:
    with open(path, "w") as f:
        for ep in episodes:
            f.write(json.dumps(asdict(ep)) + "\n")


def read(path: Path) -> list[Episode]:
    out: list[Episode] = []
    with open(path) as f:
        for line in f:
            d = json.loads(line)
            out.append(Episode(
                id=d["id"], chat_id=d["chat_id"], day=d["day"], stratum=d["stratum"],
                n_messages=d["n_messages"], n_signal=d["n_signal"],
                detections=[Detection(**x) for x in d["detections"]],
            ))
    return out


def stratum_sizes(episodes: Iterable[Episode]) -> dict[str, int]:
    sizes = {"D": 0, "S": 0, "N": 0}
    for ep in episodes:
        sizes[ep.stratum] += 1
    return sizes

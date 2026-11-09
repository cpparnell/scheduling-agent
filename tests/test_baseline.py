"""Unit tests for the baseline corpus tooling (evals/baseline). Offline: no
chat.db, no API."""

import json
from collections import Counter
from pathlib import Path

import pytest

from evals.baseline import episodes, labels, score
from evals.baseline.episodes import Detection, Episode, _DayStats
from evals.baseline.signals import plan_signal


@pytest.mark.parametrize("text", [
    "dinner friday?", "tickets are booked", "7pm works", "see you tmrw", "Oct 10 trip",
    "let's do it", "are you free this weekend", "the 14th?",
])
def test_plan_signal_matches_plan_talk(text: str) -> None:
    assert plan_signal(text)


@pytest.mark.parametrize("text", ["lol", "that's hilarious", "ok sounds good", "love you"])
def test_plan_signal_ignores_chatter(text: str) -> None:
    assert not plan_signal(text)


def test_detection_day_is_window_start() -> None:
    # Midnight-aligned window [Nov 7 00:00, Nov 8 00:00) is logged with its end.
    assert episodes.detection_day("2026-11-08T00:00:00").isoformat() == "2026-11-07"


def test_load_detections_groups_by_episode_and_skips_failures(tmp_path: Path) -> None:
    log = tmp_path / "d.jsonl"
    rows = [
        {"window_end": "2026-11-08T00:00:00", "chat_id": 5, "title": "Dinner", "date": "2026-11-13",
         "status": "confirmed", "confidence": 0.9, "evidence": "yes", "result": "created"},
        {"window_end": "2026-11-08T00:00:00", "chat_id": 5, "title": "Brunch", "date": "2026-11-15",
         "status": "confirmed", "confidence": 0.8, "evidence": "ok", "result": "skipped:unanswered"},
        {"window_end": "2026-11-08T00:00:00", "chat_id": 9, "result": "detection_failed"},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    got = episodes.load_detections(log)
    assert list(got) == ["5:2026-11-07"]
    assert [d["idx"] for d in got["5:2026-11-07"]] == [0, 1]
    assert got["5:2026-11-07"][1]["result"] == "skipped:unanswered"


def _det(idx: int = 0, result: str = "created", title: str = "Dinner") -> Detection:
    return Detection(idx=idx, title=title, date="2026-11-13", status="confirmed",
                     confidence=0.9, evidence="yes", result=result)


def test_build_assigns_strata_and_is_deterministic() -> None:
    stats = {f"{c}:2026-11-0{d}": _DayStats(5, 1 if c % 2 else 0) for c in range(1, 9) for d in range(1, 4)}
    dets = {"1:2026-11-01": [_det()], "2:2026-11-02": [_det()]}
    a = episodes.build(stats, dets)
    b = episodes.build(stats, dets)
    assert [e.id for e in a] == [e.id for e in b]
    strata = {e.id: e.stratum for e in a}
    assert strata["1:2026-11-01"] == "D"
    assert strata["2:2026-11-02"] == "D"      # detection wins even with no signal
    assert strata["3:2026-11-01"] == "S"
    assert strata["2:2026-11-01"] == "N"
    assert len(a) == len(stats)


def test_interleave_prefix_is_stratified() -> None:
    def eps(k: str, n: int) -> list[Episode]:
        return [Episode(f"{k}{i}:2026-01-01", i, "2026-01-01", k, 1, 0) for i in range(n)]
    order = list(episodes.interleave({"D": eps("D", 50), "S": eps("S", 50), "N": eps("N", 50)}))
    first10 = Counter(e.stratum for e in order[:10])
    assert first10 == Counter({"D": 5, "S": 4, "N": 1})
    assert len(order) == 150


def test_label_parse_validates() -> None:
    ok = labels.parse({"episode_id": "5:2026-11-07",
                       "detections": [{"idx": 0, "verdict": "wrong_date", "correct_date": "2027-01-15"}],
                       "missed": [{"title": "Coffee", "date": "2026-11-09", "time": ""}]})
    assert ok.detections[0].correct_date == "2027-01-15"
    assert ok.missed[0].time is None
    with pytest.raises(labels.LabelError):
        labels.parse({"episode_id": "5:2026-11-07", "detections": [{"idx": 0, "verdict": "wrong_date"}]})
    with pytest.raises(labels.LabelError):
        labels.parse({"episode_id": "5:2026-11-07", "detections": [{"idx": 0, "verdict": "meh"}]})
    with pytest.raises(labels.LabelError):
        labels.parse({"episode_id": "5:2026-11-07", "missed": [{"title": "", "date": "2026-11-09"}]})


def test_labels_last_line_wins(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    labels.append(labels.parse({"episode_id": "1:2026-11-01", "detections": [{"idx": 0, "verdict": "correct"}]}), path)
    labels.append(labels.parse({"episode_id": "1:2026-11-01", "detections": [{"idx": 0, "verdict": "not_mine"}]}), path)
    assert labels.load(path)["1:2026-11-01"].detections[0].verdict == "not_mine"


def test_score_episode_maps_outcomes() -> None:
    ep = Episode("1:2026-11-01", 1, "2026-11-01", "D", 10, 3, [
        _det(0, "created"), _det(1, "created", "Ski trip"), _det(2, "skipped:unanswered", "Brunch"),
        _det(3, "skipped:not-participant", "Their trip"), _det(4, "skipped:duplicate", "Dinner again"),
        _det(5, "updated", "Party"),
    ])
    lab = labels.parse({"episode_id": ep.id, "detections": [
        {"idx": 0, "verdict": "correct"},
        {"idx": 1, "verdict": "wrong_date", "correct_date": "2027-01-15"},
        {"idx": 2, "verdict": "correct"},
        {"idx": 3, "verdict": "not_mine"},
        {"idx": 4, "verdict": "correct"},
        {"idx": 5, "verdict": "not_a_plan"},
    ], "missed": [{"title": "Coffee", "date": "2026-11-09"}]})
    o = score.score_episode(ep, lab)
    assert o.counts == Counter({"true_positive": 1, "wrong_day": 1, "missed_by_gate": 1,
                                "gate_blocked_correctly": 1, "hallucinated": 1, "missed_by_detector": 1})
    assert o.gate_misses == Counter({"skipped:unanswered": 1})
    assert o.gate_saves == Counter({"skipped:not-participant": 1})
    assert any("2027-01-15" in text for mode, text in o.examples if mode == "wrong_day")


def test_stratified_estimate_scales_by_population() -> None:
    est = score.stratified_estimate({"D": [1, 0, 1, 0], "S": [0, 0], "N": []}, {"D": 40, "S": 100, "N": 10})
    assert est.sample == 2
    assert est.total == pytest.approx(20.0)
    assert est.low is not None and est.high is not None
    assert est.low <= est.total <= est.high


def test_stratified_estimate_single_episode_stratum_has_unknown_interval() -> None:
    est = score.stratified_estimate({"D": [1]}, {"D": 40})
    assert est.total == pytest.approx(40.0)
    assert est.low is None and est.high is None


def test_stratified_estimate_full_census_has_no_variance() -> None:
    est = score.stratified_estimate({"D": [1, 0, 2]}, {"D": 3})
    assert est.total == pytest.approx(3.0)
    assert est.high == pytest.approx(3.0)


def test_wilson_bounds() -> None:
    lo, hi = score.wilson(24, 30)
    assert 0.6 < lo < 0.65 and 0.9 < hi < 0.92
    assert score.wilson(0, 0) == (0.0, 1.0)

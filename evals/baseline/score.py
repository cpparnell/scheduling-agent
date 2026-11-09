"""Score v0.14 against the labels, per failure mode.

A detection only reaches the calendar when its result is `created` or
`updated`; anything `skipped:*` was stopped by a gate. That splits outcomes:

  written + correct        true positive
  written + wrong_date     WRONG DAY
  written + wrong_time     wrong time (day right)
  written + not_mine       NOT MINE
  written + not_a_plan     HALLUCINATED
  written + duplicate      duplicate calendar entry
  skipped + real           MISSED (a gate dropped a real Commitment; by gate)
  skipped + not real       a gate correctly blocked it (credited per gate)
  labeler-added            MISSED (detector never emitted it)

Counts in the labeled sample are extrapolated to the whole range per stratum
(N_h / n_h), with a stratified-variance 95% interval, so a partly labeled
queue still gives an unbiased estimate, just a wider one.
"""

import json
import math
from collections import Counter
from dataclasses import dataclass, field

from evals.baseline import episodes as ep_mod
from evals.baseline import labels as lab_mod
from evals.baseline import paths

WRITTEN = {"created", "updated"}
REAL = {"correct", "wrong_date", "wrong_time"}
NOT_REAL = {"not_mine", "not_a_plan"}

MODES = (
    "true_positive", "wrong_day", "wrong_time", "not_mine", "hallucinated",
    "duplicate_entry", "missed_by_gate", "missed_by_detector", "gate_blocked_correctly",
)


@dataclass
class EpisodeOutcome:
    counts: Counter[str] = field(default_factory=Counter)
    gate_misses: Counter[str] = field(default_factory=Counter)
    gate_saves: Counter[str] = field(default_factory=Counter)
    examples: list[tuple[str, str]] = field(default_factory=list)  # (mode, text)


def score_episode(ep: ep_mod.Episode, label: lab_mod.EpisodeLabel) -> EpisodeOutcome:
    out = EpisodeOutcome()
    by_idx = {d.idx: d for d in label.detections}
    for det in ep.detections:
        dl = by_idx.get(det["idx"])
        if dl is None or dl.verdict == "unsure":
            continue
        title = det["title"] or "(untitled)"
        result = det["result"]
        if result in WRITTEN:
            mode = {
                "correct": "true_positive", "wrong_date": "wrong_day", "wrong_time": "wrong_time",
                "not_mine": "not_mine", "not_a_plan": "hallucinated", "duplicate": "duplicate_entry",
            }[dl.verdict]
            out.counts[mode] += 1
            if mode != "true_positive":
                extra = f" → should be {dl.correct_date}" if dl.verdict == "wrong_date" else ""
                out.examples.append((mode, f"{ep.id} “{title}” on {det['date']}{extra}"
                                     + (f" ({dl.note})" if dl.note else "")))
        elif dl.verdict in REAL and result != "skipped:duplicate":
            out.counts["missed_by_gate"] += 1
            out.gate_misses[result] += 1
            out.examples.append(("missed_by_gate", f"{ep.id} “{title}” dropped by {result}"))
        elif dl.verdict in NOT_REAL:
            out.counts["gate_blocked_correctly"] += 1
            out.gate_saves[result] += 1
    for m in label.missed:
        out.counts["missed_by_detector"] += 1
        out.examples.append(("missed_by_detector", f"{ep.id} “{m.title}” on {m.date}"
                             + (f" ({m.note})" if m.note else "")))
    return out


@dataclass
class Estimate:
    sample: int
    total: float
    low: float | None  # None: some sampled stratum has a single labeled
    high: float | None  # episode, so its variance (and the interval) is unknown


def stratified_estimate(
    per_episode: dict[str, list[int]], sizes: dict[str, int],
) -> Estimate:
    """per_episode: stratum -> that mode's count for each labeled episode."""
    total = 0.0
    var = 0.0
    sample = 0
    unknown = False
    for h, xs in per_episode.items():
        n, big_n = len(xs), sizes.get(h, 0)
        if n == 0 or big_n == 0:
            continue
        sample += sum(xs)
        mean = sum(xs) / n
        total += big_n * mean
        if n > 1:
            s2 = sum((x - mean) ** 2 for x in xs) / (n - 1)
            var += big_n ** 2 * s2 / n * (1 - n / big_n)
        elif big_n > 1:
            unknown = True
    if unknown:
        return Estimate(sample, total, None, None)
    half = 1.96 * math.sqrt(var)
    return Estimate(sample, total, max(float(sample), total - half), total + half)


def wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    z = 1.96
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def report() -> dict[str, object]:
    eps = ep_mod.read(paths.EPISODES)
    labels = lab_mod.load(paths.LABELS)
    sizes = ep_mod.stratum_sizes(eps)
    per_mode: dict[str, dict[str, list[int]]] = {m: {"D": [], "S": [], "N": []} for m in MODES}
    gate_misses: Counter[str] = Counter()
    gate_saves: Counter[str] = Counter()
    examples: dict[str, list[str]] = {m: [] for m in MODES}
    labeled = {"D": 0, "S": 0, "N": 0}
    for ep in eps:
        lab = labels.get(ep.id)
        if lab is None:
            continue
        labeled[ep.stratum] += 1
        o = score_episode(ep, lab)
        for m in MODES:
            per_mode[m][ep.stratum].append(o.counts[m])
        gate_misses.update(o.gate_misses)
        gate_saves.update(o.gate_saves)
        for mode, text in o.examples:
            examples[mode].append(text)

    est = {m: stratified_estimate(per_mode[m], sizes) for m in MODES}
    written = sum(est[m].sample for m in ("true_positive", "wrong_day", "wrong_time",
                                          "not_mine", "hallucinated", "duplicate_entry"))
    tp = est["true_positive"].sample
    lo, hi = wilson(tp, written)
    real_est = sum(est[m].total for m in ("true_positive", "wrong_day", "wrong_time",
                                          "missed_by_gate", "missed_by_detector"))
    found_est = sum(est[m].total for m in ("true_positive", "wrong_day", "wrong_time"))
    out: dict[str, object] = {
        "run": json.loads(paths.RUN_META.read_text()) if paths.RUN_META.exists() else None,
        "episodes": {"population": sizes, "labeled": labeled},
        "modes": {m: {"in_sample": e.sample, "est_total": round(e.total, 1),
                      "ci95": None if e.low is None or e.high is None
                      else [round(e.low, 1), round(e.high, 1)]} for m, e in est.items()},
        "calendar_precision": {"correct": tp, "written": written,
                               "rate": round(tp / written, 3) if written else None,
                               "ci95": [round(lo, 3), round(hi, 3)]},
        "est_recall": round(found_est / real_est, 3) if real_est else None,
        "missed_by_gate": dict(gate_misses.most_common()),
        "gate_blocked_correctly": dict(gate_saves.most_common()),
        "examples": {m: xs[:20] for m, xs in examples.items() if xs},
    }
    return out


def print_report(r: dict[str, object]) -> None:
    eps = r["episodes"]
    assert isinstance(eps, dict)
    print(f"Episodes labeled: {eps['labeled']} of {eps['population']}")
    cp = r["calendar_precision"]
    assert isinstance(cp, dict)
    print(f"Calendar precision: {cp['correct']}/{cp['written']} = {cp['rate']}  95% CI {cp['ci95']}")
    print(f"Estimated recall: {r['est_recall']}")
    print("\nFailure mode            in sample   est. over full range (95% CI)")
    modes = r["modes"]
    assert isinstance(modes, dict)
    for m, v in modes.items():
        ci = v["ci95"] if v["ci95"] is not None else "(label more episodes)"
        print(f"  {m:<22}{v['in_sample']:>8}   {v['est_total']:>8}  {ci}")
    for k in ("missed_by_gate", "gate_blocked_correctly"):
        print(f"\n{k}: {r[k]}")
    ex = r["examples"]
    assert isinstance(ex, dict)
    for m, xs in ex.items():
        print(f"\n{m}:")
        for x in xs:
            print(f"  - {x}")


def main() -> None:
    r = report()
    paths.REPORT.write_text(json.dumps(r, indent=1))
    print_report(r)
    print(f"\nWritten to {paths.REPORT}")

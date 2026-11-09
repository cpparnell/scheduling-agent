# Baseline corpus

Measures how often v0.14 hits each failure mode (wrong day, missed,
hallucinated, not mine) on the owner's **real** message history. Wayfinder
ticket: *Baseline: how often does v0.14 hit each failure mode on real history?*
(cpparnell/scheduling-agent#21). The v1.0 core-architecture decision waits on it.

Everything it produces is private and lives in `~/.scheduling-agent/corpus/`
(override with `BASELINE_CORPUS_DIR`). Nothing is committed.

## Steps

```bash
source venv/bin/activate
python -m evals.baseline status     # what's done, what's next

# 1. Dry-run v0.14 over the last 90 days (costs API $, nothing touches Calendar)
python -m evals.baseline run        # --days 90 --cap 15
# 2. Label (free, local, resumable — stop any time)
python -m evals.baseline label      # opens http://127.0.0.1:8714
# 3. Report (free; re-run as labels accumulate)
python -m evals.baseline score
```

`run` checks both API keys first, runs a 7-day preflight, projects the cost to
the full range, and aborts if it exceeds `--cap`. The full range runs as one
backfill call (splitting it would reset reconciliation state at each chunk
boundary). A run with any failed API call is marked invalid and no queue is
built from it.

## How labeling works

The unit is an **episode**: one chat on one day. The labeler shows exactly
what v0.14's detector saw (the day's new messages, plus the faded context and
date anchors the reader prepended) next to v0.14's detections and what the
pipeline did with each (`created`, `skipped:unanswered`, …).

- Give every detection a verdict: correct / wrong day (+ right date) / wrong
  time / not mine / not a plan / duplicate / unsure.
- Add any Commitment this day's messages set up that v0.14 missed.
- Keys: `1`–`7` verdict, `j`/`k` move, `a` add missed, `c` toggle context,
  `Enter` save & next, `←`/`→` navigate.

Episodes are queued in three strata: **D** (v0.14 detected something), **S**
(plan-like words or dates, nothing detected — where misses hide), **N** (no
signal; a thin spot check). The queue interleaves them in a fixed seeded order
(5 D : 4 S : 1 N), so any prefix is a stratified random sample. `score`
extrapolates each stratum by its size, with 95% intervals. About 300 episodes
is enough; more just narrows the intervals.

## Known limits

- A tapback shows as "Liked your message" without saying which message (the
  reader doesn't resolve the target).
- Chat-days with no plan signal are only spot-checked (stratum N); a miss
  there is extrapolated from the spot check.
- A Commitment discussed over several days can be added as "missed" more than
  once; mark re-mentions as duplicates where you notice them.

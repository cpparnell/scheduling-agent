# things to work on next
- does not seem like it is handling new messages well; the past 7 days part is working decently though
- change detector model to sonnet — measure first with `python -m evals.run --model claude-sonnet-4-6` and diff the report against the Haiku baseline
- some events are getting put in the calendar twice ie. 'baseball game at mlb ballpark' and 'dinner with parents and baseball game' which were created at the same time at the same day. (golden cases `dedup_baseball_a/b` track this)
- ~~create tests~~ done — see `tests/` (offline, run `pytest`) and `evals/` (paid, run `pytest -m eval` or `python -m evals.run`)
- setup a sqlite client so I can see the messages database
- tapback reaction of love/like can count as agreeing to plans — eval case `pos_tapback_acceptance` is a tracked known-failure that will start passing once this is implemented

## Testing
- `pytest` — fast, free tier-1 unit/integration tests (chat.db reader, attributedBody decode, detector parsing with a stubbed client, state dedup, calendar AppleScript, main pipeline). No network.
- `python -m evals.run [--model M] [--judge] [-k id]` — runs the golden dataset (`evals/golden.jsonl`) against the real detector model; prints per-case + aggregate accuracy / false-positive rate and writes a diffable report to `evals/reports/`. Costs ~$0.05/run.
- `pytest -m eval` — same eval suite as a pass/fail gate (accuracy ≥ 80%, zero false positives on hard negatives).
- Install dev deps with `pip install -r requirements.dev.txt`.
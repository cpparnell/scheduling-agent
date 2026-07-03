# scheduling-agent

An iMessage-to-Calendar scheduling agent for macOS. It watches your iMessage database (`~/Library/Messages/chat.db`) for new messages, uses Claude Haiku to detect plans (an explicit invite, plus a specific date), and automatically creates Apple Calendar events — no confirmation step.

## How it works

```
chat.db changes
  → watcher.py   (watchdog, 5-second debounce)
  → reader.py    (read-only SQLite read of new messages)
  → detector.py  (Claude Haiku extracts zero or more plans per thread as structured JSON)
  → main.py      (confidence gate, past-event guard, time-confidence gate for all-day events)
  → state.py     (exact-hash + title-window dedup check)
  → dedup.py     (LLM adjudicator: is this the same plan as a nearby existing event?)
  → calendar.py  (creates the event — timed or all-day — via AppleScript)
  → state.py     (records the descriptive event record + calendar UID, updates the checkpoint)
```

Only plans that meet the confidence threshold are created. A specific start time is only kept
when the detector is highly confident about it (`time_confidence_threshold`); otherwise the event
is created as all-day. Before creating an event, a second Claude call checks nearby existing
events and skips creation if it judges the new detection to be the same real-world plan, reworded
— this is what catches the same plan being added twice with slightly different wording. If a
thread's detection fails (API error, malformed response), the watermark is held back and the
thread is retried on the next poll, up to a bounded number of retries, so a transient failure
doesn't silently and permanently drop a plan.

## Requirements

- macOS with iMessage and Apple Calendar
- Python 3
- An Anthropic API key

## Setup

1. **Grant Full Disk Access** so the agent can read the iMessage database: System Settings → Privacy & Security → Full Disk Access → add Terminal (or your Python binary).

2. **Create a virtual environment and install dependencies:**

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Set your API key:** copy the example env file and fill in your key.

   ```bash
   cp .env.example .env
   ```

   `.env` is gitignored. Alternatively, export `ANTHROPIC_API_KEY` in your shell.

## Usage

```bash
source venv/bin/activate
python main.py
```

On startup the agent scans the last 7 days of messages, then keeps running and processes new messages as they arrive. Press Ctrl+C to stop.

## Configuration

The config file lives at `~/.scheduling-agent/config.json` and is created with defaults on first run. Changes are picked up automatically — no restart needed.

| Key | Default | Description |
|-----|---------|-------------|
| `blocked_contacts` | `[]` | Phone numbers / emails to ignore |
| `target_calendar` | `"Calendar"` | Apple Calendar name to create events in |
| `lookback_days` | `7` | How far back to scan on first run |
| `confidence_threshold` | `0.85` | Minimum Claude confidence to auto-create a confirmed event |
| `tentative_confidence_threshold` | `0.6` | Minimum confidence for a tentative (unanswered/hedged) plan |
| `time_confidence_threshold` | `0.9` | Minimum confidence in the extracted clock time to keep it; below this the event is created all-day instead |
| `dedup_enabled` | `true` | Whether the LLM dedup adjudicator runs before creating an event |
| `dedup_model` | `"claude-haiku-4-5"` | Model used for dedup adjudication |
| `dedup_day_window` | `1` | How many days on either side of a new plan's date count as "nearby" for dedup candidates |
| `dedup_fail_open` | `true` | If the adjudicator call itself fails, create the event rather than risk dropping a real plan |
| `max_watermark_retries` | `3` | How many consecutive polls to retry a thread whose detection failed before giving up and advancing past it |

State (the last-processed message timestamp, dedup hashes, and descriptive records of created
events — including the calendar event UID — used for dedup adjudication) is stored in
`~/.scheduling-agent/state.json`.

## Testing

Install the dev dependencies first:

```bash
source venv/bin/activate
pip install -r requirements.dev.txt
```

There are two tiers:

**Unit / integration tests** — fast, offline, and free. No API key required.

```bash
pytest
```

These cover the deterministic plumbing: the chat.db reader and `attributedBody`
decoding (run against a temporary SQLite fixture database), detector parsing and
filtering (with the Anthropic client stubbed out), the dedup adjudicator's
candidate-filtering and verdict-handling logic (with its client stubbed out),
the state/dedup logic, and AppleScript event assembly (timed and all-day). All
on-disk state is redirected to a temp directory, so your real `~/.scheduling-agent`
is never touched. Every run's console output (pass/fail per file, the final
summary line) is also mirrored to a timestamped file in `logs/tests/`.

**Detection eval** — measures the Claude detector (and the dedup adjudicator)
against a golden dataset of synthetic threads: confirmed/tentative plans, hard
negatives like vague/cancelled/past-recap threads, multi-event threads, stale
relative-date resolution, all-day vs. timed extraction, and dedup pairs (the
same plan reworded, or two different plans sharing a date/time). This calls
the real model, so it needs `ANTHROPIC_API_KEY` and costs roughly $0.05 per run.

```bash
python -m evals.run                     # baseline on the default model
python -m evals.run --model claude-sonnet-4-6   # compare another model
python -m evals.run --judge             # add an LLM title-quality score
pytest -m eval                          # run it as a pass/fail gate
```

It prints per-case detection results plus a separate dedup-adjudication report
(same/different verdicts against the golden dedup pairs), aggregate accuracy,
dedup accuracy, and the false-positive rate on hard negatives. Each run writes
its two output files into its own timestamped folder under `logs/evals/`
(e.g. `logs/evals/20260702-162344_claude-haiku-4-5/`): `report.json` (for
diffing across prompt or model changes) and `stdout.log`, mirroring everything
printed to the console. The golden cases (`evals/golden.jsonl`) use date
placeholders that are resolved relative to the current day at runtime, so they
never go stale.

**Log directories** — three separate locations, one per entry point: `logs/stdout/`
(the live agent, `python main.py`), `logs/evals/` (`python -m evals.run`), and
`logs/tests/` (`pytest`).

## Project structure

```
main.py                # Thin entry point
scheduling_agent/
├── main.py            # process_new_messages() and the watcher loop
├── config.py          # Loads ~/.scheduling-agent/config.json
├── state.py           # Checkpoint timestamp, dedup hashes, descriptive event records
├── reader.py          # Reads iMessage threads from chat.db
├── detector.py        # Claude Haiku plan detection (JSON schema output, events array)
├── dedup.py           # LLM adjudicator: is a new detection the same plan as an existing event?
├── calendar.py        # Apple Calendar event creation via osascript (timed + all-day)
└── watcher.py         # Filesystem watcher with debounce
tests/                 # Offline unit/integration tests (pytest)
└── fixtures/chatdb.py # Builds a temp chat.db + encodes attributedBody blobs
evals/                 # Paid detection eval (golden dataset + runner)
├── golden.jsonl       # Labeled threads: positives, hard negatives, dedup pairs, etc.
├── loader.py          # Materializes runtime-relative dates
└── run.py             # Detection scorer + dedup-adjudication scorer + report writer
```

## Privacy notes

- The iMessage database is opened **read-only**; the agent never modifies your messages.
- Message text from new threads is sent to the Anthropic API for plan detection. Use `blocked_contacts` to exclude conversations you don't want processed.

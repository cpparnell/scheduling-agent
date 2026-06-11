# scheduling-agent

An iMessage-to-Calendar scheduling agent for macOS. It watches your iMessage database (`~/Library/Messages/chat.db`) for new messages, uses Claude Haiku to detect confirmed plans (an explicit invite, an explicit acceptance, and a specific date), and automatically creates Apple Calendar events — no confirmation step.

## How it works

```
chat.db changes
  → watcher.py   (watchdog, 5-second debounce)
  → reader.py    (read-only SQLite read of new messages)
  → detector.py  (Claude Haiku extracts confirmed plans as structured JSON)
  → state.py     (dedup check via SHA-256 hash)
  → calendar.py  (creates the event via AppleScript)
  → state.py     (records the event hash and updates the checkpoint)
```

Only plans that meet the confidence threshold are created, and each event is deduplicated by chat, date, and title so re-scans never create duplicates.

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
| `confidence_threshold` | `0.85` | Minimum Claude confidence to auto-create an event |

State (the last-processed message timestamp and hashes of created events) is stored in `~/.scheduling-agent/state.json`.

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
filtering (with the Anthropic client stubbed out), the dedup/state logic, and
AppleScript event assembly. All on-disk state is redirected to a temp directory,
so your real `~/.scheduling-agent` is never touched.

**Detection eval** — measures the Claude detector against a golden dataset of
~20 synthetic threads (confirmed plans plus hard negatives like vague or
cancelled invites). This calls the real model, so it needs `ANTHROPIC_API_KEY`
and costs roughly $0.05 per run.

```bash
python -m evals.run                     # baseline on the default model
python -m evals.run --model claude-sonnet-4-6   # compare another model
python -m evals.run --judge             # add an LLM title-quality score
pytest -m eval                          # run it as a pass/fail gate
```

It prints per-case results plus aggregate accuracy and the false-positive rate
on hard negatives, and writes a timestamped report to `evals/reports/` so runs
can be diffed across prompt or model changes. The golden cases (`evals/golden.jsonl`)
use date placeholders that are resolved relative to the current day at runtime,
so they never go stale.

## Project structure

```
main.py                # Thin entry point
scheduling_agent/
├── main.py            # process_new_messages() and the watcher loop
├── config.py          # Loads ~/.scheduling-agent/config.json
├── state.py           # Checkpoint timestamp + event dedup hashes
├── reader.py          # Reads iMessage threads from chat.db
├── detector.py        # Claude Haiku plan detection (JSON schema output)
├── calendar.py        # Apple Calendar event creation via osascript
└── watcher.py         # Filesystem watcher with debounce
tests/                 # Offline unit/integration tests (pytest)
└── fixtures/chatdb.py # Builds a temp chat.db + encodes attributedBody blobs
evals/                 # Paid detection eval (golden dataset + runner)
├── golden.jsonl       # ~20 labeled threads (positives + hard negatives)
├── loader.py          # Materializes runtime-relative dates
└── run.py             # Scorer + report writer
```

## Privacy notes

- The iMessage database is opened **read-only**; the agent never modifies your messages.
- Message text from new threads is sent to the Anthropic API for plan detection. Use `blocked_contacts` to exclude conversations you don't want processed.

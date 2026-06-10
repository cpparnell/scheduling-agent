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
```

## Privacy notes

- The iMessage database is opened **read-only**; the agent never modifies your messages.
- Message text from new threads is sent to the Anthropic API for plan detection. Use `blocked_contacts` to exclude conversations you don't want processed.

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

An iMessage-to-Calendar scheduling agent that runs continuously on macOS. It watches `~/Library/Messages/chat.db` for new messages, uses Claude Haiku to detect confirmed plans (explicit invite + explicit acceptance + specific date), and automatically creates Apple Calendar events via AppleScript — no confirmation step.

## Getting Started

### Prerequisites

1. **Full Disk Access**: System Settings → Privacy & Security → Full Disk Access → add Terminal (or Python)
2. **Virtual environment + dependencies**:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

3. **API key**: `cp .env.example .env` and fill in `ANTHROPIC_API_KEY` (loaded via python-dotenv in `scheduling_agent/__init__.py`), or export it in your shell.

### Running the agent

```bash
source venv/bin/activate
python main.py
```

The agent runs immediately on startup (scanning the last 7 days) then watches for new messages indefinitely. Press Ctrl+C to stop.

## Project Structure

```
scheduling_agent/
├── __init__.py
├── main.py        # Entry point: process_new_messages(), main() with watcher loop
├── config.py      # Loads ~/.scheduling-agent/config.json (blocked contacts, calendar, etc.)
├── state.py       # Persists last_processed_timestamp and SHA-256 event hashes
├── reader.py      # Reads iMessage threads from chat.db since last checkpoint
├── detector.py    # Calls Claude Haiku with JSON schema to extract confirmed plans
├── calendar.py    # Creates Apple Calendar events via osascript/AppleScript
└── watcher.py     # watchdog FileSystemEventHandler with 5-second debounce
```

## Configuration

Config file: `~/.scheduling-agent/config.json` (created with defaults on first run)

| Key | Default | Description |
|-----|---------|-------------|
| `blocked_contacts` | `[]` | Phone numbers / emails to ignore |
| `target_calendar` | `"Calendar"` | Apple Calendar name to create events in |
| `lookback_days` | `7` | How far back to scan on first run |
| `confidence_threshold` | `0.85` | Minimum Claude confidence to auto-create |

## Architecture Notes

### Pipeline

```
chat.db changes
  → watcher.py (debounce 5s)
  → reader.py (read-only SQLite, Apple epoch conversion)
  → detector.py (Claude Haiku, JSON schema output)
  → state.py (dedup check via SHA-256 hash)
  → calendar.py (osascript AppleScript)
  → state.py (record hash + update timestamp)
```

### Key technical details

- **Apple epoch**: iMessage timestamps are nanoseconds since 2001-01-01; offset = 978307200 seconds
- **Read-only DB**: `sqlite3.connect("file:chat.db?mode=ro", uri=True)`
- **Model**: `claude-haiku-4-5-20251001` — fast/cheap for continuous monitoring
- **Structured output**: `output_config={"format": {"type": "json_schema", "schema": ...}}`
- **Dedup key**: `SHA-256(f"{chat_id}|{date}|{title.strip().lower()}")`
- **State file**: `~/.scheduling-agent/state.json`

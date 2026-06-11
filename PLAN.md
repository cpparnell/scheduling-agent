# iMessage-to-Calendar Scheduling Agent

## Context

Build a tool that continuously watches iMessages on macOS, uses Claude to detect confirmed plans in conversations, and automatically creates Apple Calendar events — no confirmation step. Runs as a persistent background process. First run scans the last 7 days; subsequent runs process only new messages.

## Critique & Refinements to the Original Idea

**What's solid:**
- Core loop (watch messages → detect intent → create event) is well-defined and feasible
- macOS has native hooks: `chat.db` for iMessages, `osascript` for Calendar
- No exotic dependencies — Python stdlib handles both platform integrations

**Key risks to mitigate:**
- **Deduplication** is critical — chat.db triggers multiple file-change events per message; Claude must not create duplicate calendar events
- **Ambiguity** — "let's hang out soon" is not actionable; Claude must require both an invite AND a clear acceptance before creating an event
- **Partial event info** — "grab coffee Tuesday" often lacks time; must handle gracefully (create with TBD time, or skip)
- **File locking** — chat.db may be briefly locked by Messages app; use `?mode=ro` + retry logic

---

## Architecture

### File Structure

```
scheduling_agent/
├── main.py           # Entry point; starts the file watcher loop
├── watcher.py        # Monitors chat.db via watchdog; debounces rapid events
├── reader.py         # Reads new message threads from chat.db since last checkpoint
├── detector.py       # Sends threads to Claude API; returns structured plan events or None
├── calendar.py       # Creates Apple Calendar events via osascript
├── state.py          # Persists: last_processed_timestamp + set of created event hashes
└── config.py         # Loads config from ~/.scheduling-agent/config.json (blocked list, etc.)
```

### Data Flow

```
chat.db changes
    → watcher.py (debounce 5s, deduplicate rapid events)
    → reader.py (query new messages since last_processed_timestamp, grouped by thread)
    → filter out threads where any participant is in blocked list
    → detector.py (Claude API: classify each thread, extract event details)
    → if has_event AND confidence ≥ 0.85 AND date is present:
        → state.py check: skip if event hash already created
        → calendar.py: create event via osascript
        → state.py: save event hash + update timestamp
```

### Key Technical Details

**File Watcher** (`watcher.py`):
- Use `watchdog` library (`pip install watchdog`) to watch `~/Library/Messages/chat.db`
- Debounce: collect events for 5 seconds after first change, then process once (chat.db flushes in bursts)
- This avoids hammering Claude API on every individual write

**iMessage Reading** (`reader.py`):
- Connect read-only: `sqlite3.connect('file:chat.db?mode=ro', uri=True)`
- Query messages newer than `last_processed_timestamp`, grouped into conversation threads
- Thread structure: `{chat_id, participants: [phone/email], messages: [{sender, text, timestamp, from_me}]}`
- Exclude threads with any participant in blocked list (checked against `handle.id`)

**Claude Detection** (`detector.py`):
- Model: `claude-haiku-4-5-20251001` (fast, cheap for continuous processing)
- System prompt: strict criteria — requires explicit invite + explicit acceptance; reject vague expressions of interest
- Returns JSON: `{has_event, title, date, time_start, duration_minutes, location, confidence}`
- `date` is required; `time_start` is optional (if missing, mark as all-day event)
- Batch multiple threads in one API call to reduce latency/cost

**Calendar Creation** (`calendar.py`):
- `subprocess.run(['osascript', '-e', script], capture_output=True, timeout=10)`
- Default to "Calendar" unless user has configured a different target calendar
- If no time extracted → create as all-day event on the date
- If no duration → default to 1 hour

**Deduplication** (`state.py`):
- State file: `~/.scheduling-agent/state.json`
- `created_events`: set of SHA-256 hashes of `(chat_id + date + title_normalized)` — prevents duplicate events even if same conversation is reprocessed
- `last_processed_timestamp`: macOS iMessage timestamp of the newest message processed

**Config** (`config.py`):
- `~/.scheduling-agent/config.json`
- `blocked_contacts`: list of phone numbers or emails to exclude
- `target_calendar`: name of the Apple Calendar to create events in (default: "Calendar")
- `lookback_days`: how far back on first run (default: 7)
- `confidence_threshold`: minimum Claude confidence to auto-create (default: 0.85)

---

## Setup Requirements

1. **Full Disk Access**: System Settings → Privacy & Security → Full Disk Access → add `python3` (or Terminal)
2. **Install dependencies**: `pip install anthropic watchdog`
3. **Set env var**: `export ANTHROPIC_API_KEY=...`
4. **Run**: `python main.py` (Calendar access permission prompted by macOS on first event creation)

---

## Verification

1. Send yourself a test iMessage thread where someone invites you and you accept
2. Run `python main.py` and observe logs: should detect the thread and create event
3. Open Apple Calendar to confirm event appears with correct title and date
4. Re-run — event should NOT be created again (deduplication check)
5. Add a contact to the blocked list and confirm their threads are skipped
6. Send a vague message ("we should hang out") — confirm no event is created

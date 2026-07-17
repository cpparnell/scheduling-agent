# scheduling-agent

An iMessage-to-Calendar scheduling agent for macOS. It watches your iMessage database (`~/Library/Messages/chat.db`) for new messages, uses Claude Haiku to detect plans (an explicit invite, plus a specific date), and automatically creates Apple Calendar events — no confirmation step.

## How it works

```
chat.db changes
  → watcher.py   (watchdog, 5-second debounce)
  → reader.py    (read-only SQLite read of new messages)
  → detector.py  (Claude Haiku extracts zero or more plans per thread as structured JSON,
                  with a verbatim-evidence gate against hallucinated plans)
  → main.py      (gates: past-event, participation, unanswered-invite, confidence,
                  time-confidence demotion to all-day)
  → reconcile.py (matches the detection against the canonical event store — and optionally
                  the live calendar — before anything is written: exact hash → deterministic
                  fuzzy match → LLM adjudicator)
  → calendar.py  (creates the event, or updates the existing one on a reschedule/upgrade)
  → state.py     (write-ahead journal + canonical event record + checkpoint)
```

**Ownership gate.** The detector judges whether the user is personally part of each plan
(`user_is_participant`); plans that belong to someone else — a friend describing *their* trip,
a sibling's wedding, a group plan the user declined — are logged and skipped, never created.

**Status semantics.** `confirmed` means the user is attending with clear agreement. `tentative`
means the user was invited and *explicitly hedged* ("maybe", "I'll try") — it is a
classification, not a lower confidence tier, and is judged against the same confidence bar.
An invitation the user hasn't answered at all is `unanswered` and never creates an event;
when the user later replies, the plan is re-detected with its new status.

**Reconciliation instead of create-by-default.** Every detection is matched against the
canonical event store (`state.json`) before any calendar write: an exact hash/title-window
check, then a deterministic fuzzy match (normalized-title overlap + compatible date/time,
across chats), then an LLM adjudicator for the genuinely ambiguous cases — biased toward
"same" when uncertain, because a wrong merge just updates the existing event while a missed
duplicate spams the calendar. With `calendar_query_enabled`, events already on the target
calendar (created manually, or before a state reset) join the candidate set. A match with
material new information — a reschedule, a newly stated location, tentative → confirmed —
**updates** the existing calendar event; anything else is skipped as a duplicate.

**Crash safety.** Calendar writes are journaled: the intent is persisted before the
AppleScript call and committed after state is updated, and pending journal entries count
for dedup immediately. If the process dies between the calendar write and the state write,
startup recovery checks the calendar and either adopts the created event or drops the entry
— the classic restart-duplicate window is closed. If a thread's detection fails (API error,
malformed response), the watermark is held back and the thread is retried on the next poll,
up to a bounded number of retries, so a transient failure doesn't silently drop a plan.

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
| `confidence_threshold` | `0.85` | Minimum Claude confidence to auto-create an event (confirmed and tentative alike — tentative is a status, not a lower bar) |
| `time_confidence_threshold` | `0.9` | Minimum confidence in the extracted clock time to keep it; below this the event is created all-day instead |
| `dedup_enabled` | `true` | Whether the LLM adjudicator runs as reconciliation's last layer |
| `dedup_model` | `"claude-haiku-4-5"` | Model used for dedup adjudication |
| `dedup_day_window` | `1` | How many days on either side of a new plan's date count as "nearby" for reconciliation candidates |
| `dedup_fail_open` | `true` | If the adjudicator call itself fails, create the event rather than risk dropping a real plan |
| `calendar_query_enabled` | `true` | Read events back from the target calendar as reconciliation candidates (catches manually created events and lost state) |
| `fuzzy_title_threshold` | `0.6` | Minimum normalized-title token overlap for the deterministic fuzzy layer to match without the LLM |
| `evidence_gate_enabled` | `true` | Drop detected plans whose quoted evidence isn't found verbatim in the thread (hallucination guard) |
| `reconcile_update_enabled` | `true` | Let reconciliation matches update the existing calendar event (reschedules, added locations); off treats them as skips |
| `max_watermark_retries` | `3` | How many consecutive polls to retry a thread whose detection failed before giving up and advancing past it |

Upgrading from v0.4: `tentative_confidence_threshold` was removed (a stale key in an existing
config.json is ignored). The state file migrates automatically to schema v4; previously created
"(Tentative)" events from unanswered invites stay on the calendar and can be cleaned up by hand.

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

These run in CI on every push and pull request against `main` (see
`.github/workflows/tests.yml`), and must pass before a PR can be merged.

These cover the deterministic plumbing: the chat.db reader and `attributedBody`
decoding (run against a temporary SQLite fixture database), detector parsing and
filtering (with the Anthropic client stubbed out), the dedup adjudicator's
candidate-filtering and verdict-handling logic (with its client stubbed out),
the state/dedup logic, and AppleScript event assembly (timed and all-day). All
on-disk state is redirected to a temp directory, so your real `~/.scheduling-agent`
is never touched. Every run's console output (pass/fail per file, the final
summary line) is also mirrored to a timestamped file in `logs/tests/`.

**Detection eval** — measures the Claude detector (and the dedup adjudicator)
against a golden dataset of synthetic threads: confirmed/tentative/unanswered
plans, hard negatives like vague/cancelled/past-recap threads, bystander cases
(third-party plans that must never reach the calendar, plus participant-positive
controls), multi-event threads, stale relative-date resolution, all-day vs.
timed extraction, and dedup pairs (the same plan reworded, or two different
plans sharing a date/time — including "different" controls that guard against
over-merging). A separate **pipeline phase** replays multi-poll scenarios
(`"polls"` cases) through the real gates and reconciliation against isolated
state and a fake calendar: growing-context re-detection, reworded re-mentions,
the same plan across two chats, reschedules that must update rather than
duplicate, and cancellations. This calls the real model, so it needs
`ANTHROPIC_API_KEY` and costs roughly $0.10 per run.

`pytest -m eval` enforces hard gates: zero false positives on hard negatives,
zero bystander leaks, all known-duplicate pairs caught with no controls merged,
and exact create/update counts on every pipeline scenario.

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
├── main.py            # process_new_messages(), per-event gates (process_event), journal recovery
├── config.py          # Loads ~/.scheduling-agent/config.json
├── state.py           # Canonical event store, write-ahead journal, checkpoint, dedup hashes
├── reader.py          # Reads iMessage threads from chat.db
├── detector.py        # Claude Haiku plan detection (participation, status, evidence gate)
├── reconcile.py       # Matches detections against known events: exact → fuzzy → LLM
├── dedup.py           # LLM adjudicator: is a new detection the same plan as an existing event?
├── calendar.py        # Apple Calendar create/update/query via osascript (timed + all-day)
└── watcher.py         # Filesystem watcher with debounce
tests/                 # Offline unit/integration tests (pytest)
└── fixtures/chatdb.py # Builds a temp chat.db + encodes attributedBody blobs
evals/                 # Paid detection eval (golden dataset + runner)
├── golden.jsonl       # Labeled threads: positives, negatives, bystander, dedup, pipeline polls
├── loader.py          # Materializes runtime-relative dates (+ multi-poll threads)
└── run.py             # Detection, dedup-adjudication, and pipeline scorers + report writer
```

## Privacy notes

- The iMessage database is opened **read-only**; the agent never modifies your messages.
- Message text from new threads is sent to the Anthropic API for plan detection. Use `blocked_contacts` to exclude conversations you don't want processed.

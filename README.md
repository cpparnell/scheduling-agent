# scheduling-agent

An iMessage-to-Calendar scheduling agent for macOS. It watches your iMessage database (`~/Library/Messages/chat.db`) for new messages, uses Claude Haiku to detect plans (an explicit invite, plus a specific date), and automatically creates Apple Calendar events — no confirmation step.

## How it works

```
chat.db changes
  → watcher.py   (watchdog, 5-second debounce)
  → reader.py    (read-only SQLite read of new messages; prepends recent context plus
                  older date-bearing anchor messages, e.g. "the trip is Oct 10!")
  → detector.py  (Claude Haiku extracts zero or more plans per thread as structured JSON,
                  with verbatim-evidence gates against hallucinated plans and dates)
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

**Bare-weekday anchoring.** "On Friday we will…" usually means the next Friday — but not
when the chat established weeks earlier that the plan is months out. Four defenses: the
reader prepends older date-bearing messages (beyond the normal 30-message context window)
so the anchor stays visible; the detector must resolve a bare weekday against any anchor
in the thread and cite the anchoring message verbatim in `date_evidence` (dropped if the
quote isn't found — same hallucination guard as `evidence`); a deterministic post-check
(`_reconcile_weekday`) re-derives the weekday named in the model's own evidence and shifts
the emitted date by the minimal signed delta (at most 3 days) if the two disagree — Claude's
own weekday arithmetic is unreliable even when told the current date explicitly, so this
catches it rather than trusting the model's math; and reconciliation has a far-date layer
that catches a same-chat detection whose title matches an event already recorded far away,
sending it to the LLM adjudicator as a probable mis-dated re-mention instead of creating a
near-term duplicate. Relatedly, a weekday named on the same day it's said ("this Thursday"
sent on a Thursday) resolves to that day, not next week.

**Verbatim evidence matching.** The hallucination guard requires `evidence`/`date_evidence`
to appear in the thread, but the model doesn't always quote it byte-for-byte: it prepends a
sender/timestamp label ("Me (07/11 6:46PM, sent 3 days ago): …"), wraps it in quotes, joins
several messages with `/` or a newline, or re-encodes an emoji's invisible variation-selector
codepoint. The matcher normalizes (NFKC, curly→straight quotes, strips invisible codepoints),
strips a recognized sender/timestamp prefix, matches per-fragment across message joins, and
falls back to a bounded token-overlap check (≥3 tokens, ≥80% coverage against a single
message) — while still dropping genuine fabrications where no message covers the quote.

**Group-silence demotion.** In a multi-person thread, another participant accepting a plan
never confirms it *for the user* — if "Me" never sent a message about the plan, its status
is force-demoted to `unanswered` regardless of what the model classified it as, so the
unanswered-invite gate holds it back until the user actually responds.

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

**Quick install:**

```bash
./scripts/setup.sh
```

Creates the venv, installs the package, walks you through the API key, checks
whether Full Disk Access looks granted, and optionally installs a launchd
agent (see "Running in the background" below) so you don't have to leave a
terminal open. It's a thin wrapper around the manual steps below — read on if
you'd rather do it by hand or understand what it does.

1. **Grant Full Disk Access** so the agent can read the iMessage database: System Settings → Privacy & Security → Full Disk Access → add Terminal (or your Python binary).

2. **Create a virtual environment and install:**

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -e .
   ```

3. **Set your API key:** copy the example env file and fill in your key.

   ```bash
   cp .env.example .env
   ```

   `.env` is gitignored. Alternatively, export `ANTHROPIC_API_KEY` in your shell.

## Usage

```bash
source venv/bin/activate
scheduling-agent
```

(equivalently `python -m scheduling_agent`, or the older `python main.py`.)

On startup the agent scans the last 7 days of messages, then keeps running and processes new messages as they arrive: normally triggered by a `chat.db` filesystem event, with a `poll_interval_minutes`-based timer as a backstop in case an event is ever missed. Press Ctrl+C to stop.

### Running in the background

A foreground terminal isn't a great way to run something meant to stay up.
`scripts/install-launchagent.sh` installs a `launchd` agent that starts on
login and restarts automatically if the process crashes:

```bash
./scripts/install-launchagent.sh
```

Supervisor-level output goes to `~/Library/Logs/scheduling-agent/launchd.log`;
the agent's own detailed log still goes to `logs/stdout/` as described below.
`launchctl unload ~/Library/LaunchAgents/com.scheduling-agent.plist` stops and
disables it.

### Removing your data

```bash
scheduling-agent --purge
```

Deletes `~/.scheduling-agent/state.json` (the canonical event store, including
quoted message evidence) and everything under `logs/`, then exits. It does not
touch Calendar or Messages — events already created stay on your calendar.

## Configuration

The config file lives at `~/.scheduling-agent/config.json` and is created with defaults on first run. Changes are picked up automatically — no restart needed.

| Key | Default | Description |
|-----|---------|-------------|
| `blocked_contacts` | `[]` | Phone numbers / emails to ignore |
| `target_calendar` | `"Calendar"` | Apple Calendar name to create events in |
| `lookback_days` | `7` | How far back to scan on first run |
| `date_context_lookback_days` | `90` | How far before the watermark to scan for older date-bearing anchor messages to prepend as context |
| `date_context_max_messages` | `10` | Max older date-bearing messages prepended per chat, beyond the fixed 30-message context window |
| `confidence_threshold` | `0.85` | Minimum Claude confidence to auto-create an event (confirmed and tentative alike — tentative is a status, not a lower bar) |
| `time_confidence_threshold` | `0.9` | Minimum confidence in the extracted clock time to keep it; below this the event is created all-day instead |
| `dedup_enabled` | `true` | Whether the LLM adjudicator runs as reconciliation's last layer |
| `dedup_model` | `"claude-haiku-4-5"` | Model used for dedup adjudication |
| `dedup_day_window` | `1` | How many days on either side of a new plan's date count as "nearby" for reconciliation candidates |
| `dedup_fail_open` | `true` | If the adjudicator call itself fails, create the event rather than risk dropping a real plan |
| `calendar_query_enabled` | `true` | Read events back from the target calendar as reconciliation candidates (catches manually created events and lost state) |
| `fuzzy_title_threshold` | `0.6` | Minimum normalized-title token overlap for the deterministic fuzzy layer to match without the LLM |
| `far_title_similarity` | `0.4` | Screening bar for the far-date layer: same-chat records with at least this much title overlap but a distant date go to the LLM adjudicator (catches a bare weekday mis-resolved to a near-term date) |
| `evidence_gate_enabled` | `true` | Drop detected plans whose quoted evidence (or date evidence) isn't found verbatim in the thread (hallucination guard) |
| `reconcile_update_enabled` | `true` | Let reconciliation matches update the existing calendar event (reschedules, added locations); off treats them as skips |
| `max_watermark_retries` | `3` | How many consecutive polls to retry a thread whose detection failed before giving up and advancing past it |
| `poll_interval_minutes` | `15` | Backstop poll interval, independent of the filesystem watcher, in case a `chat.db` change event is ever missed. `0` disables it |

Upgrading from v0.4: `tentative_confidence_threshold` was removed (a stale key in an existing
config.json is ignored). The state file migrates automatically to schema v4; previously created
"(Tentative)" events from unanswered invites stay on the calendar and can be cleaned up by hand.

State (the last-processed message timestamp, dedup hashes, and descriptive records of created
events — including the calendar event UID — used for dedup adjudication) is stored in
`~/.scheduling-agent/state.json`. Quoted message `evidence` is truncated to
`state.EVIDENCE_MAX_CHARS` (240 characters) before being written, and log lines that would
otherwise include a verbatim quote are truncated the same way — enough context for the dedup
adjudicator to keep working, without keeping a full plaintext transcript on disk indefinitely.
Run `scheduling-agent --purge` to delete it (see "Removing your data" above).

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
controls), multi-event threads, stale relative-date resolution (including bare
weekdays anchored to a far-out event earlier in the thread), all-day vs.
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

**Eval clock pinning.** What the detector prompt shows as "today" affects
weekday-dependent cases ("this Thursday" resolves differently depending on the
real day of the week), so a run's pass/fail must not depend on which day it
happens to execute. `evals/run.py` pins the clock to the next Wednesday
on/after the real date (never a fixed calendar date — `main.process_event`
drops past-dated events against the live clock, so a fixed pin would eventually
rot every pipeline case). Override with `--today YYYY-MM-DD` or `EVAL_TODAY`
to reproduce a specific day.

```bash
python -m evals.run                     # baseline on the default model
python -m evals.run --model claude-sonnet-4-6   # compare another model
python -m evals.run --judge             # add an LLM title-quality score
python -m evals.run --today 2026-07-16  # reproduce a specific day's eval clock
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
(the live agent, `scheduling-agent`), `logs/evals/` (`python -m evals.run`), and
`logs/tests/` (`pytest`). The live agent's log file rotates at 10MB (5 backups
kept) so a long-running background process (see "Running in the background")
doesn't grow it unbounded.

## Project structure

```
main.py                # Thin entry point (python main.py); scheduling-agent console
                        # command and python -m scheduling_agent do the same thing
scheduling_agent/
├── __main__.py        # Enables `python -m scheduling_agent`
├── main.py            # process_new_messages(), per-event gates (process_event), journal
                        # recovery, poll-fallback timer, --purge flag
├── config.py          # Loads ~/.scheduling-agent/config.json
├── state.py           # Canonical event store, write-ahead journal, checkpoint, dedup hashes
├── reader.py          # Reads iMessage threads from chat.db
├── detector.py        # Claude Haiku plan detection (participation, status, evidence gate)
├── reconcile.py       # Matches detections against known events: exact → fuzzy → LLM
├── dedup.py           # LLM adjudicator: is a new detection the same plan as an existing event?
├── calendar.py        # Apple Calendar create/update/query via osascript (timed + all-day)
└── watcher.py         # Filesystem watcher with debounce
scripts/
├── setup.sh                    # venv + install + API key + launchd prompt
├── install-launchagent.sh      # Installs/reloads the launchd agent
└── com.scheduling-agent.plist  # launchd agent template (RunAtLoad, KeepAlive)
tests/                 # Offline unit/integration tests (pytest)
└── fixtures/chatdb.py # Builds a temp chat.db + encodes attributedBody blobs
evals/                 # Paid detection eval (golden dataset + runner)
├── golden.jsonl       # Labeled threads: positives, negatives, bystander, dedup, pipeline polls
├── loader.py          # Materializes runtime-relative dates (+ multi-poll threads)
└── run.py             # Detection, dedup-adjudication, and pipeline scorers + report writer
```

## Privacy notes

- The iMessage database is opened **read-only**; the agent never modifies your messages.
- Message text from new threads is sent to the Anthropic API for plan detection, including
  the phone numbers/emails of everyone in the thread as participant context — not just the
  device owner's. Use `blocked_contacts` to exclude conversations you don't want processed.
- Quoted message evidence stored in `~/.scheduling-agent/state.json` (and written to logs) is
  truncated, not stored/logged in full — see "Configuration" above. Run `scheduling-agent
  --purge` at any time to delete all local state and logs.

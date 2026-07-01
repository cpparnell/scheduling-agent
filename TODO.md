# Scheduling Agent — Roadmap

Status as of v0.3 (`feature/v0.3`): the core pipeline (watcher → reader → detector → dedup →
calendar) works, tentative/group/relative-date handling has landed, and detection quality is
strong (golden evals ~100% accuracy, 0% false positives). The work below hardens the
foundation, broadens recall, handles plans that change over time, and adds user visibility.

Priority order: **Robustness → Recall gaps → Conversation evolution → Trust & control**.

---

## Tier 1 — Robustness / hardening  **DONE**

Goal: solidify the foundation so the daemon runs unattended without surprises.

- **Test `watcher.py`** (currently 0 tests). Cover: debounce timer cancels/reschedules on
  rapid `on_modified`/`on_created` events; callback fires once after quiet period; an
  exception in the callback does not kill the observer thread.
- **Test `config.py`** (currently 0 tests). Cover: defaults written on first run; user config
  merged over defaults; malformed/partial `config.json` handled gracefully; unknown keys
  preserved.
- **State migration / versioning.** The dedup hash formula changed in v0.2
  (`chat_id|date|title` → `chat_id|date|time_start`), which can re-create events once on
  upgrade. Add a `schema_version` field to `state.json` and a migration path so upgrades are
  silent. (`scheduling_agent/state.py`)
- **End-to-end smoke test.** One integration test: fixture `chat.db` → `reader` →
  `detector` (stubbed client) → `calendar` (stubbed osascript) → `state`, asserting the full
  flow and idempotency on re-run. (`tests/`)
- **Failure-mode resilience review.** Ensure the daemon survives: chat.db locked/mid-write,
  Anthropic API errors/timeouts, repeated osascript failures. Add bounded retry/backoff where
  sensible; never let one bad thread crash the loop. (`scheduling_agent/main.py`,
  `detector.py`, `calendar.py`)
- **Logging foundation.** Replace ad-hoc `print()` with the `logging` module (level via
  config/env). This also underpins the Tier 4 audit log.

## Tier 2 — Recall gaps

Goal: catch real plans the detector currently misses.

- **Tapback acceptance** (closes `pos_tapback_acceptance` known-failure). `reader.py`
  currently drops reaction messages (`associated_message_type` 2000–2005 add, 3000–3005
  remove). Decode them and surface "X loved/liked your message" as an acceptance signal to the
  detector. Update the golden case to expect detection. (`reader.py`, `detector.py`,
  `evals/golden.jsonl`)
- **Past-event guard.** Don't auto-create events whose date is already in the past relative to
  "today" (can happen within the `lookback_days` window on first run). Guard before
  `calendar.create_event`. Add unit + golden cases. (`scheduling_agent/main.py`)
- **Recurring events.** Detect "every Thursday", "weekly standup". Extend the event schema
  with an optional recurrence field; emit an AppleScript recurrence rule. Add golden cases.
  (`detector.py`, `calendar.py`, `evals/golden.jsonl`)
- **Multi-day events.** Detect plans spanning days (trips, conferences). Add optional
  `end_date` to the schema and honor it in calendar creation. Add golden cases.

## Tier 3 — Conversation evolution

Goal: keep the calendar correct as a plan changes after it's first created.

- **Persist the calendar event identifier** in `state.json` alongside the dedup hash (today
  only hashes are stored) so events can later be updated or deleted. (`state.py`,
  `calendar.py`)
- **Reschedule handling.** When a thread changes the time/date/location of a previously
  detected plan, update the existing calendar event instead of leaving a stale one or creating
  a second. Requires the detector to signal "this updates a prior plan" and a way to map
  thread → existing event. (`detector.py`, `main.py`, `calendar.py`)
- **Cancellation handling.** When a thread cancels a confirmed plan, delete the corresponding
  calendar event. (`detector.py`, `calendar.py`)
- **Dedup rework for identity.** Current keying on `time_start` means a reschedule reads as a
  brand-new event. Anchor event identity to the source thread + plan, so updates replace
  rather than duplicate. (`state.py`)

## Tier 4 — Trust & control

Goal: make the silent daemon's actions visible, reviewable, and (optionally) gated.

- **Creation notifications.** Fire a macOS notification (`osascript display notification` or
  `terminal-notifier`) when an event is created, showing title / date / time / confidence /
  tentative status. (`calendar.py` or a new `notify.py`)
- **On-disk audit log.** Append a structured JSONL record for every thread processed:
  decision (created / skipped-low-confidence / duplicate / no-event), reason, confidence,
  status. Builds on Tier 1 logging. Lets the user see exactly what the agent did and why.
- **Optional confirmation mode.** Config flag to require user approval before creating
  (especially tentative / lower-confidence events) — e.g. via a notification action or a
  small review queue, rather than zero-touch.
- **Inspection CLI.** A command to show recent decisions, created events, and current config
  without tailing logs.

## Cross-cutting

- Keep the golden eval suite (`evals/golden.jsonl`) and unit tests green; add cases alongside
  each feature.
- Maintain privacy posture: read-only `chat.db`, no persistent message storage beyond what the
  audit log deliberately records.

# v0.4 Plan — Detection Hardening: Dedup Adjudicator, All-Day Events, Recall Fixes

## Context

v0.3 detects plans well on the golden set, but real-world use shows two failure classes:

- **Hallucination** — events created that already exist (the same plan added twice with slightly
  different wording, sometimes from re-processing or cross-thread discussion) or that were never
  actually made.
- **Omission** — real plans that never make it to the calendar.

v0.4 attacks both with four workstreams:

1. **LLM dedup adjudicator** — before creating an event, retrieve similar existing events
   (same day ±1, cross-chat) and have a second LLM call decide same-plan vs different-plan.
2. **All-day unless extremely confident about time** — a specific `time_start` survives only when
   the model is highly confident the plan starts at that time; otherwise the event is all-day.
   (Today a missing time silently becomes a 60-minute noon event — no all-day support exists.)
3. **Recall fixes** — the detector schema currently allows max one event per thread (a thread with
   "dinner then the game" loses one plan), and the watermark advances past threads whose API call
   errored (those messages are permanently lost).
4. **Anti-hallucination** — require the detector to quote its evidence, and annotate message age so
   stale relative dates ("tomorrow" sent 3 days ago) resolve against send time, not processing time.

**Decisions made:**
- Dedup compares against **app state only** (events this agent created, upgraded to descriptive
  records). Querying Calendar.app for manually-created events is out of scope (slow, extra failure
  surface).
- A dedup match at a different time (reschedule-shaped) → **skip + log**. Event updating ships in
  v0.5 using the calendar UIDs we start storing now.
- Adjudicator failure → **fail-open** (create the event). A rare visible duplicate beats a silently
  dropped plan; exact-hash and title-window dedup still catch verbatim repeats first.

---

## Step 1 — `state.py`: schema v3 with descriptive event records

Current state stores only SHA-256 hashes — an LLM adjudicator has nothing descriptive to compare
against. Upgrade to full records.

- `CURRENT_SCHEMA_VERSION = 3`. v2→v3 migration in `_migrate()`:
  `data.setdefault("events", [])` and `data.setdefault("watermark_hold", {"ts": None, "count": 0})`.
  Existing hashes in `created_events` / `title_events` stay valid — no recompute.
- New `events` list of records:

  ```json
  {"hash": "…", "chat_id": 123, "date": "2026-07-04", "time_start": "19:00",
   "title": "Dinner with Sam", "location": "Dicey's", "status": "confirmed",
   "evidence": "dinner friday at 7?", "calendar_uid": "1F2A…",
   "created_at": "2026-07-02T18:00:00", "suppressed": false}
  ```

  `suppressed: true` means the adjudicator ruled it a duplicate: the hash and record are stored so
  the same detection isn't re-adjudicated next cycle, but no calendar event was created.
- Extended signature:

  ```python
  def record_event(chat_id, date, time_start, title, *,
                   location=None, status=None, evidence=None,
                   calendar_uid=None, suppressed=False) -> None
  ```

- New query (cross-chat by design — the observed dup bug can span chats):

  ```python
  def get_events_near(date_str: str, window_days: int = 1) -> list[dict]
  ```

  Returns non-suppressed records within ±`window_days`; skips unparseable dates. Prune records
  whose date is >90 days past inside `record_event` to keep state.json small.

**Tests (`tests/test_state.py`):** v2→v3 and v0→v3 migration chains; record shape round-trip;
`get_events_near` window semantics (same day / ±1 / outside / suppressed excluded); a
`suppressed=True` record still trips `is_duplicate`.

## Step 2 — `calendar.py`: all-day events + UID capture

- Return type change: `create_event(...) -> str | None` — the event UID on success (empty string
  if Calendar returns none), `None` on failure. Callers switch from `if created:` to
  `if uid is not None:`.
- AppleScript: append `return uid of newEvent` inside the `tell` block; read `stdout.strip()`.
- **All-day branch** when `time_start is None` (replaces the noon fallback
  `time_part = time_start or "12:00"`):

  ```applescript
  make new event at targetCalendar with properties
      {summary:"…", start date:date "July 04, 2026 at 12:00:00 AM",
       end date:date "July 05, 2026 at 12:00:00 AM", allday event:true}
  ```

  Single-day: end = start + 1 day. Multi-day (`end_date` set): end = end_date + 1 day (Calendar's
  all-day end date is exclusive). `duration_minutes` is ignored for all-day. Tentative title prefix
  and location behavior unchanged. The timed path stays byte-identical.

**Tests (`tests/test_calendar.py`):** replace `test_no_time_defaults_to_noon` with
`test_no_time_creates_allday_event` (asserts `allday event:true`, midnight start, next-midnight
end); multi-day exclusive end; UID captured from the `capture_osascript` stub; failure → `None`.
Update `spy_create_event` in `tests/test_main.py` and the osascript stub in `tests/test_e2e.py` to
return a UID string.

⚠️ **Manual verification required once on the real Mac**: `allday event:true` end-exclusivity and
the `uid` return format vary subtly across macOS Calendar versions. Unit tests only assert script
text.

## Step 3 — `detector.py`: events array, evidence, time_confidence, stale-date fix

This is the single coordinated LLM-surface change. Land schema + prompt + parsing + test-payload
updates together, then **immediately run `python -m evals.run` to re-baseline** before stacking
Steps 4–5.

### Schema

Root becomes an object wrapping an array (structured outputs require an object root):

```python
RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"events": {"type": "array", "items": EVENT_ITEM_SCHEMA}},
    "required": ["events"],
}
```

`EVENT_ITEM_SCHEMA` = current `EVENT_SCHEMA` **minus `has_event`** (empty array ⇒ no plans),
**plus** two required fields:

```python
"evidence": {
    "type": "string",
    "description": "Verbatim quote of the single message that most clearly establishes this plan",
},
"time_confidence": {
    "type": ["number", "null"],
    "description": "0.0-1.0 confidence that the plan starts exactly at time_start; null when time_start is null",
},
```

`max_tokens`: 512 → 2048 (multiple events per thread). Model unchanged
(`claude-haiku-4-5-20251001`).

### Prompt additions (replace the `has_event` paragraphs with events-array wording)

```
A thread may contain zero, one, or several DISTINCT plans (e.g. "dinner then the game"
is two plans). Return one entry in `events` for each distinct plan, and an empty
`events` array when there are none. Never split a single plan into multiple entries,
and never invent a plan that no message explicitly proposes.

**Evidence**: For every plan, set `evidence` to a verbatim quote of the single message
that most clearly establishes it (the invitation or the agreement). If you cannot
point to a specific message, do not emit the plan.

**Times**: Set `time_start` ONLY when a specific clock time is stated in the messages
("7pm", "at 5:30", "noon"). If the time is vague ("morning", "after work", "evening")
or absent, set `time_start` to null — the event will be created as an all-day event.
Set `time_confidence` to how certain you are the plan starts exactly at `time_start`
(1.0 = explicitly stated and agreed; lower if inferred). Null when time_start is null.

**Relative dates**: Each message is prefixed with the date/time it was SENT. Resolve
"tomorrow", "tonight", "this Saturday" etc. relative to the SEND time of the message
containing them, NOT relative to today. A message sent 3 days ago saying "tomorrow"
means 2 days ago.
```

### Code changes

- `_format_thread()`: annotate messages older than 24h:
  `Them (06/29 7:02PM, sent 3 days ago): …` — computed against the same `today` param. Keep the
  `[Today is …]` header.
- Signature change:

  ```python
  def detect_plans(threads: list[dict], model: str = MODEL) -> tuple[list[dict], set]:
      """Returns (events, failed_chat_ids)."""
  ```

  Per-thread exceptions still log-and-continue, but the failed set is now reported so main.py can
  hold the watermark. Compat shim: if a parsed payload carries `has_event` (legacy single-object
  shape), wrap it as a one-element list.
- **Soft evidence check** (non-gating in v0.4): normalize whitespace/case and verify `evidence`
  appears in the concatenated thread text; log a warning on mismatch but do **not** drop the event
  (avoids trading hallucination for omission). The evidence string flows into the state record for
  the adjudicator either way.

**Tests:** mechanically migrate every `fake_anthropic` payload in `test_detector.py` /
`test_main.py` / `test_e2e.py` from `{…single…}` to `{"events": […]}`. New cases:
two-plans-one-thread; empty events array; legacy single-object payload still parsed; failed thread
reported in the failure set; evidence-mismatch logs warning but keeps the event.

## Step 4 — `main.py` + `config.py`: gates, multi-event loop, watermark hold

- `events, failed_chats = detector.detect_plans(threads)`.
- **Time-confidence gate** (per event, before calendar creation):

  ```python
  if time_start is not None and (event.get("time_confidence") or 0) < cfg["time_confidence_threshold"]:
      logger.info("Demoting to all-day (time_confidence %.2f < %.2f): %s", ...)
      time_start = None
  ```

  A specific time survives only when the model is extremely confident (default threshold **0.9**);
  otherwise all-day. The dedup hash then falls back to its title-based key — existing behavior.
- `uid = calendar.create_event(...)`; on success
  `state.record_event(..., location=location, status=status, evidence=event.get("evidence"), calendar_uid=uid)`.
- **Watermark hold with bounded retries** (fixes permanently-missed plans without poison-thread
  loops): if `failed_chats` is empty → advance to max `latest_apple_ts` as today and clear the
  hold. If non-empty → do **not** advance; increment `watermark_hold.count` keyed to the current
  position. Once `count >= cfg["max_watermark_retries"]` (default 3) → advance anyway and log an
  error naming the abandoned chat_ids. Holding re-sends successful threads next cycle, but
  `state.is_duplicate` makes re-detection idempotent — cost is a few redundant Haiku calls, no
  duplicate calendar events. (Per-chat watermarks noted in TODO.md as a future refinement.)
- New `config.py` DEFAULTS:

  ```python
  "time_confidence_threshold": 0.9,
  "dedup_enabled": True,
  "dedup_model": "claude-haiku-4-5",
  "dedup_day_window": 1,
  "dedup_fail_open": True,
  "max_watermark_retries": 3,
  ```

**Tests (`tests/test_main.py`):** low time_confidence → `create_event` called with
`time_start=None`; high confidence passes through; watermark held when a thread fails; advanced
after 3 consecutive failures; UID recorded in state; two events from one thread both created.

## Step 5 — `scheduling_agent/dedup.py` (new): LLM dedup adjudicator

**Design: cheap code filter first, LLM only when candidates exist.** In the common case (no other
event within ±1 day) this costs zero API calls.

```python
def find_candidates(event: dict, existing: list[dict], day_window: int = 1) -> list[dict]:
    """Code-side filter: existing records within ±day_window of event['date'],
    excluding exact-hash matches (handled upstream). Cap at 5 newest."""

def adjudicate(event: dict, candidates: list[dict], model: str) -> dict | None:
    """One structured-output call. Returns the parsed verdict dict, or None on
    any error (caller applies the fail-open policy)."""
```

Adjudicator system prompt (draft):

```
You decide whether a newly detected plan is the SAME real-world plan as an event
already on the calendar, or a different plan.

The same plan often appears twice with different wording ("gym at 7am" vs "morning
workout session at 7"), sometimes in different conversations, sometimes with small
time drift after a reschedule (7:00 vs 7:30). Different plans can legitimately share
a date and even a time — lunch with mom and a work call can both be at noon on the
same day, in different conversations.

Judge by: whether the conversations/participants overlap, whether titles and
locations describe the same activity, whether the times are identical or plausibly
the same slot, and what the quoted evidence messages say. When genuinely uncertain,
answer is_duplicate=false — an occasional duplicate on the calendar is safer than
silently dropping a real plan.

Respond with JSON only.
```

Output schema:

```python
ADJUDICATOR_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "is_duplicate": {"type": "boolean"},
        "duplicate_of": {"type": ["integer", "null"],
                         "description": "index of the matching existing event, or null"},
        "reasoning": {"type": "string"},
    },
    "required": ["is_duplicate", "duplicate_of", "reasoning"],
}
```

- Input (user message): a `NEW PLAN:` block (title / date / time / location / chat_id / status /
  evidence quote) followed by an indexed `EXISTING CALENDAR EVENTS:` list rendered from the state
  records — this is why Step 1 stores descriptive records instead of hashes.
- API style mirrors `detector.py`: module-level lazy `_client` / `_get_client()` (own copy per
  module so tests monkeypatch `dedup._client` exactly like `detector._client`),
  `output_config={"format": {"type": "json_schema", "schema": ADJUDICATOR_SCHEMA}}`,
  `max_tokens=300`.
- **Model:** Haiku via the `dedup_model` knob (~500 in / ~100 out tokens ≈ $0.001/call, fires
  rarely). If evals show mis-adjudication, bump to Sonnet via config — no code change.
- **main.py integration** (after `state.is_duplicate`, before `create_event`):

  ```python
  if cfg["dedup_enabled"]:
      candidates = dedup.find_candidates(event, state.get_events_near(date, cfg["dedup_day_window"]))
      if candidates:
          verdict = dedup.adjudicate(event, candidates, model=cfg["dedup_model"])
          if verdict is None and not cfg["dedup_fail_open"]:
              # fail-closed (non-default): skip
          elif verdict and verdict["is_duplicate"]:
              logger.info("LLM dedup: '%s' duplicates '%s' — %s", ...)
              state.record_event(..., suppressed=True)  # don't re-adjudicate next cycle
              # skip creation
  ```

  A duplicate verdict at a different time (reschedule-shaped) is also skip + log in v0.4; the
  stored `calendar_uid` makes v0.5 event-updating possible without another migration.

**Tests (new `tests/test_dedup.py`):** `find_candidates` window / cap / exclusion (pure, no LLM);
`adjudicate` happy path with a fake client; malformed JSON → `None`; exception → `None`.
Main-level: duplicate verdict suppresses creation and records a suppressed hash; non-duplicate
creates; adjudicator error + fail-open creates; `dedup_enabled: false` bypasses entirely; an event
with no nearby candidates never triggers an adjudicator call.

## Step 6 — Evals (interleaved with Steps 3–5, per CLAUDE.md)

- `evals/run.py`: adapt to the `(events, failed)` tuple return from `detect_plans`.
- **Fix the time_start scoring bug**: `score_case` currently skips the time check when the expected
  value is falsy (`exp_time = expected.get("time_start"); if exp_time and …`), so an eval cannot
  assert "no time / all-day". Change to `if "time_start" in expected:` with direct comparison —
  without this, the all-day goal is untestable.
- **Multi-event scoring**: support `"expected": {"events": [{…}, {…}]}`. Greedy-match each expected
  event to a prediction by date + `title_contains_any`; unmatched expected ⇒ omission failure,
  unmatched predicted ⇒ hallucination failure. Single-event cases keep the current shape (runner
  normalizes both to lists).
- **Score dedup pairs as dedup decisions**: add `"dedup_with": "<id_of_a_case>"` and
  `"dedup_verdict": "same" | "different"` to the `_b` cases. New phase in `run.py` after detection:
  shape the `_a` case's detected event as a state record, run `find_candidates` + `adjudicate`
  against the `_b` detection, score the verdict. Report `dedup_accuracy` in `summarize()`.
  Existing pairs map to: baseball = "same" (its `_b` becomes a multi-event case — dinner new,
  baseball dup); gym/workout = "same"; pizza-vs-drinks = "different" via the code filter alone
  (dates differ — assert no LLM call was made).
- **New golden cases**:
  - Dedup: same plan reworded, same day, no time ("same"); same plan discussed in two different
    chats ("same" — cross-chat); two genuinely different plans, same day + same time, different
    chats ("different" — the adjudicator's hard negative).
  - Stale relative dates: "tomorrow" sent 30h ago → expected `date_offset_days: 0`; "tonight" sent
    yesterday → expected has_event with yesterday's date (main's past-guard then drops it — the
    eval only checks the detector resolves it correctly).
  - All-day: "dinner Saturday" no time → `"time_start": null`; "let's hang Saturday morning"
    (vague) → `"time_start": null`; "7pm sharp" → `"time_start": "19:00"` (control).
  - Multi-event: one thread proposing two distinct plans → `expected.events` with two entries.
  - Hallucination hard negative: a thread vividly *recapping* a past event → no events.
- **Gates (`evals/test_evals.py`):** keep `false_positive_rate == 0` and `accuracy >= 0.8`. For
  `dedup_accuracy`: one report-only eval pass first to confirm Haiku clears the bar, then hard-gate
  that **all "same"-verdict pairs are caught** (that is the user-observed bug).

---

## Files

| Change | Files |
|---|---|
| Modified | `scheduling_agent/state.py`, `calendar.py`, `detector.py`, `main.py`, `config.py`; `evals/run.py`, `evals/golden.jsonl`, `evals/test_evals.py`; `tests/conftest.py`, `test_state.py`, `test_calendar.py`, `test_detector.py`, `test_main.py`, `test_e2e.py`; `README.md`; `TODO.md` |
| New | `scheduling_agent/dedup.py`, `tests/test_dedup.py`, this `PLAN.md` |

TODO.md updates: mark shipped Tier 3 slices (UID persistence, dedup-for-identity); note per-chat
watermarks and event-update-via-UID (reschedule handling) as v0.5.

## Risks

1. **Detector single-object → array re-baseline.** Switching Haiku's output shape can shift
   confidence calibration; the `false_positive_rate == 0` gate may trip. Mitigation: change only
   what Step 3 lists, run evals immediately, tune the prompt before stacking Steps 4–5.
2. **Adjudicator false "same" verdicts are a brand-new omission channel.** Mitigations: ±1-day
   candidate gating, "uncertain ⇒ different" prompt rule, fail-open on errors, every suppression
   logged with reasoning, suppressed records kept in state for audit, dedup eval gate.
3. **AppleScript all-day / UID quirks** across macOS Calendar versions — one manual real-Mac
   verification before trusting Step 2.
4. **Watermark hold** re-sends successful threads while any thread fails — bounded by the 3-retry
   cap, idempotent via dedup, cheap in the common case.

## Verification

1. `pytest` — full offline suite green (no API key required).
2. `python -m evals.run` after Step 3 (re-baseline) and after Step 6 (full run including the dedup
   phase). Confirm `accuracy >= 0.8`, `false_positive_rate == 0`, all "same" dedup pairs caught.
3. Manual end-to-end on the Mac: run `python main.py`; send a test iMessage plan **without a time**
   → verify a true all-day event appears in Calendar (not a noon event); send the **same plan
   reworded** from another thread → verify the log shows the adjudicator suppressing it with
   reasoning; inspect `~/.scheduling-agent/state.json` for the descriptive record with
   `calendar_uid` populated.

---

# v0.5 — Hardening: Canonical Event Store, Ownership Gating, Eval Coverage (SHIPPED)

Field-reported failures this iteration fixed:

1. **Duplication** — the same real-world plan landed multiple times with different wording.
   Root causes: every poll re-feeds up to 30 prior messages of context, so the same plan is
   re-detected on nearly every poll; the exact hash breaks on any rewording/time drift; the
   adjudicator was biased toward "not duplicate" when uncertain; a crash between the calendar
   write and the state write left no record to dedup against; dedup never looked at the
   actual calendar.
2. **Ownership hallucination** — a friend's own plan ("SHE is going to Patty's lake house")
   was created on the user's calendar. Nothing anywhere modeled whether the user participates.
3. **Evals caught neither** — no bystander cases, no multi-poll re-detection cases, and the
   dedup eval was report-only.

What shipped (see README for the user-facing description):

- **state.py schema v4** — events are a canonical store (`canonical_id`, `chat_ids`
  provenance, `confidence`, `revisions` audit trail) plus a **write-ahead journal**:
  intent persisted before every calendar write, committed after the state write, pending
  entries counted by the dedup lookups, and startup recovery (`main.recover_journal`)
  resolving interrupted writes against the calendar.
- **reconcile.py** — detections are matched before any calendar write: exact
  hash/title-window (now returning the matched record so material changes still apply) →
  deterministic fuzzy layer (normalized-title Jaccard + compatible date/time, cross-chat)
  → LLM adjudicator with the uncertainty bias flipped to "same" (the v0.4 Risk-2 tradeoff
  inverted deliberately: a wrong merge now updates the existing event rather than dropping
  data). Matches with material diffs (reschedule, new location, tentative→confirmed)
  **update** the calendar event via the stored UID; `calendar.get_events_near` feeds
  manually created / lost-state events into the candidate set (the v0.4 out-of-scope item).
- **detector.py** — `user_is_participant` + `participation_evidence` required fields with a
  hard silent gate in main.py; `status` gained `unanswered` (invitations with no response
  never create events); **tentative redefined** as "the user explicitly hedged" — a
  classification judged at the single confidence bar, `tentative_confidence_threshold`
  deleted; the verbatim-evidence check is now gating (config: `evidence_gate_enabled`).
- **Evals** — bystander category (zero-leak hard gate + participant-positive controls),
  pairwise "different" adjudicator controls against over-merging, and a multi-poll
  **pipeline phase** (`"polls"` golden cases) replaying growing context / rewording /
  cross-chat / reschedule / cancellation scenarios through the real gates + reconciliation,
  gated on exact create/update counts. The dedup gate is now a hard assert. Offline
  harness tests cover the plumbing with the LLM faked.

Verification: `pytest` green offline (197 tests); `python -m evals.run` + `pytest -m eval`
require `ANTHROPIC_API_KEY` (re-baseline needed after the prompt/schema changes). Manual
real-Mac checks before trusting the new AppleScript surface: time `get_events_near` on a
large calendar, verify `update_event` moves an event by UID, and kill -9 between the
"Created calendar event" log line and the next state write to watch recovery run.

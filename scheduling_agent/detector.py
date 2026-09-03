import json
import logging
import re
import unicodedata
from datetime import date as _date, datetime, timedelta

import anthropic

from scheduling_agent.datepatterns import EXPLICIT_DATE_RE
from scheduling_agent import usage_tracker

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"

_client = None


def _get_client() -> "anthropic.Anthropic":
    """Lazily construct the Anthropic client so importing this module does not
    require ANTHROPIC_API_KEY (and so tests can swap in a fake)."""
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


SYSTEM_PROMPT = """You are an assistant that analyzes iMessage conversation threads to identify scheduled plans.

The messages labeled "Me" are from the user whose calendar these plans go on.

A thread may contain zero, one, or several DISTINCT plans (e.g. "dinner then the game"
is two plans). Return one entry in `events` for each distinct plan, and an empty
`events` array when there are none. Never split a single plan into multiple entries,
and never invent a plan that no message explicitly proposes.

**Participation — set `user_is_participant`**: true ONLY if the user ("Me") is
personally expected to attend the plan: they proposed it, were invited to it, or
clearly included themselves. People constantly text about plans that are NOT the
user's — set `user_is_participant` to false for those, even when the plan itself is
specific and confirmed. Examples of NON-participation:
- A friend describing their own plans: "I'm going to Patty's lake house Saturday"
- Someone else's event mentioned in passing: "my sister's wedding is in June"
- A group chat where others arrange something and the user never engages or is
  addressed ("you two have fun!")
- The user explicitly declined but others are still going
Participation is about whether the user is INVITED, not whether they've replied yet:
if the user was directly addressed or invited ("you in?", "you two too?") and simply
hasn't responded, `user_is_participant` is still true — use `status: unanswered` for
the not-yet-replied part, a separate field. Only set it false when the plan is truly
someone else's and the user was never addressed at all.
Set `participation_evidence` to one sentence citing the message that shows the user
is (or is not) expected to attend. When in doubt whether "Me" is included, set
`user_is_participant` to false — a wrong event on the user's calendar is worse than
a missing one.

Plans fall into three categories — set `status` accordingly:

**confirmed**: An explicit invitation or proposal with a specific date, AND the user
is attending with clear agreement: the user accepted an invitation, or the user
proposed the plan and the others accepted. Acceptance includes: "yes!", "sounds good",
"I'll be there", "see you then", "k", "I'm down", "sure", "why not", "!!", 👍, or similar clear agreement.
Tapback reactions also count: "❤️ Loved your message" or "👍 Liked your message" or "‼️ Emphasized your message" in response to a scheduling message signals acceptance. "👎 Disliked your message" signals rejection.
If the user clearly accepted, someone ELSE hedging ("I'll try to make it") does not
make the plan tentative for the user — it is still confirmed.
Confirmed requires that "Me" is a genuine participant in the exchange: either "Me"
accepted (a message or tapback from "Me"), or "Me" proposed the plan and someone else
accepted. If "Me" proposed it but every reply is a HEDGE (not an acceptance), that is
tentative, not confirmed — see tentative below. If "Me" never proposed the plan AND
never replied to it at all — a group chat where OTHER people propose and accept a
plan entirely among themselves while "Me" sends nothing about it — that is unanswered
FOR THE USER; someone else's acceptance of someone else's proposal never confirms it.

**tentative**: The user was invited and the USER explicitly gave a hedged response:
"maybe", "I'll try", "hopefully", "we'll see", "let me check", or similar. Also
tentative: the user proposed the plan and every reply so far is a hedge (from anyone
who replied) rather than a clear acceptance — this still counts even though the hedge
came from someone other than "Me", because "Me" is the one who initiated the plan.
Tentative is NOT a lower-confidence guess — it is a definite invitation with a definite
hedged answer. Judge it with the same confidence you would a confirmed plan.

**unanswered**: An explicit invitation with a specific date that the user has not
responded to at all (or a plan the user proposed that nobody has answered yet). Emit
it with this status — do not guess it into tentative or confirmed. This also covers a
group chat where OTHER people propose and accept a plan entirely among themselves
while "Me" never proposed it and never sent anything about it — that is still
unanswered FOR THE USER, never confirmed or tentative. (This does NOT apply when "Me"
is the one who proposed the plan — see tentative above for that case.)

**cancelled**: A plan that was previously agreed to (confirmed or tentative — the user
had accepted it, in this thread) is explicitly called off in a NEW message: "actually
we have to cancel", "can't do Friday anymore, sorry", "trip's off". Emit it with
`status: "cancelled"`, using the ORIGINAL date/time/title of the plan being called off
(not a guessed replacement), and set `evidence` to the message that cancels it. This is
different from a plain decline below — cancelled means a plan the user had already
agreed to is now being undone, not an invitation being turned down for the first time.

Do NOT emit a plan when:
- No specific invitation exists ("we should hang out sometime")
- The user declines an invitation they had never previously agreed to — there is no
  existing plan to cancel, so this is simply not a plan at all, not a "cancelled" one
- No reasonably specific date is mentioned
- The thread only references a past event
- The proposal was superseded by a reschedule request ("can we push it?", "let's do
  next week instead") and no NEW specific date has been agreed — emit nothing: not
  the original date, and not a guessed replacement
- A vaguely floated date ("maybe Saturday?") was answered only with hedges ("maybe,
  I'll let you know") and never became a real invitation with a real answer
NEVER fabricate a date: `date` must come from a specific message. Do not compute one
by adding time to a vague phrase like "next week" or "sometime soon".

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
means 2 days ago. A weekday named on that same weekday ("this Thursday" sent on a
Thursday) means THAT DAY — the send date itself, not next week.

**Bare weekday names**: Before resolving a bare weekday ("Friday", "on Wed"), scan
the ENTIRE thread — including old messages — for an anchor: an explicit date, a
named month or event, or an earlier message that already pinned when the occasion
happens. Only when no anchor exists does a bare weekday mean the next occurrence
after the message's send time.
- If an earlier message states an explicit date falling on that same weekday, the
  bare weekday refers to THAT date. Example: a month-old message says "reminder:
  the reunion is Sunday, August 30" and someone now says "on Sunday we should all
  get dinner" — that dinner is August 30, NOT the upcoming Sunday. People routinely
  say "on Friday" about the Friday of an event months away.
- If the plan is anchored to a named event/timeframe ("the October trip"), resolve
  the weekday WITHIN that timeframe.
- If the weekday clearly belongs to an anchored event whose exact dates are never
  stated in the thread, set `date` to null rather than guessing the next occurrence.

**Date evidence**: For every plan, set `date_evidence` to a verbatim quote of the
single message that establishes the plan's DATE. When a bare weekday was resolved
against an anchor, `date_evidence` MUST quote the anchoring message (the one with
the explicit date or timeframe), not the weekday mention. It may be the same
message as `evidence`.

**Recurring events**: If the plan repeats on a pattern, set `recurrence`:
- "every Monday", "weekly standup", "every week" → "weekly"
- "every day", "daily" → "daily"
- "every other week", "biweekly" → "biweekly"
- "every month", "monthly" → "monthly"
- One-time event → null

**Multi-day events**: If the plan spans multiple days (trips, conferences, festivals), set `end_date` to the ISO 8601 last day of the event. For single-day events, set `end_date` to null.

Respond with a JSON object only. No prose.
"""

# Appended to SYSTEM_PROMPT (when context_marking_enabled) for threads that
# mix already-processed context with newly-arrived messages. Every poll
# re-sends the same context window, so without this rule the model would
# re-emit the same plan every time any new message arrives in the chat.
_CONTEXT_MARKING_ADDENDUM = """

**Already-processed context**: This thread mixes context — messages from a
previous analysis, marked "[--- earlier messages, already processed —
context only ---]" — with messages that just arrived, marked "[--- new
messages ---]". Only emit a plan if at least one NEW message carries
scheduling-relevant information about it: the proposal itself, an
acceptance or decline, a change to the date/time/location, or a
cancellation. If everything that establishes a plan sits in the earlier
context and the new message(s) don't add anything relevant, emit nothing —
it was already handled. A new message that merely reacts to an
already-established plan ("so excited!!", "lol", an emoji) is NOT
scheduling-relevant and must not cause you to re-emit it. This rule is
about withholding re-emission, not about ignoring context: still use the
context to fill in a plan's details (date, time, location) when a new
message's scheduling-relevant content refers back to it — e.g. a late
acceptance of an invitation that was proposed in the context, or a
reschedule/cancellation of a plan established there.
"""

EVENT_ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "date": {
            "type": ["string", "null"],
            "description": "ISO 8601 date (YYYY-MM-DD) or null if no specific date"
        },
        "time_start": {
            "type": ["string", "null"],
            "description": "HH:MM in 24h format, or null if no specific time"
        },
        "time_confidence": {
            "type": ["number", "null"],
            "description": "0.0-1.0 confidence that the plan starts exactly at time_start; null when time_start is null"
        },
        "duration_minutes": {
            "type": ["integer", "null"],
            "description": "Duration in minutes, or null if unknown (default to 60)"
        },
        "location": {
            "type": ["string", "null"],
            "description": "Location or venue, or null if unspecified"
        },
        "confidence": {
            "type": "number",
            "description": "Confidence score 0.0-1.0 that this is a genuine plan"
        },
        "status": {
            "type": "string",
            "enum": ["confirmed", "tentative", "unanswered", "cancelled"],
            "description": "confirmed if the user is attending with clear agreement; tentative if the user explicitly hedged ('maybe', 'I'll try'); unanswered if the user has not responded to the invitation; cancelled if a previously-agreed plan is explicitly called off in a new message"
        },
        "user_is_participant": {
            "type": "boolean",
            "description": "true only if the user ('Me') is personally expected to attend this plan"
        },
        "participation_evidence": {
            "type": "string",
            "description": "One sentence citing the message that shows whether the user is expected to attend"
        },
        "recurrence": {
            "anyOf": [
                {"type": "string", "enum": ["daily", "weekly", "biweekly", "monthly"]},
                {"type": "null"}
            ],
            "description": "Recurrence pattern for repeating events, or null for one-time events"
        },
        "end_date": {
            "type": ["string", "null"],
            "description": "ISO 8601 last date (YYYY-MM-DD) for multi-day events, or null for single-day"
        },
        "evidence": {
            "type": "string",
            "description": "Verbatim quote of the single message that most clearly establishes this plan"
        },
        "date_evidence": {
            "type": "string",
            "description": "Verbatim quote of the single message that establishes this plan's date (the anchoring message when a bare weekday was resolved against an earlier anchor)"
        },
    },
    "required": [
        "title", "date", "time_start", "time_confidence", "duration_minutes",
        "location", "confidence", "status", "user_is_participant",
        "participation_evidence", "recurrence", "end_date", "evidence",
        "date_evidence",
    ],
}

RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "events": {"type": "array", "items": EVENT_ITEM_SCHEMA},
    },
    "required": ["events"],
}

# Kept for backward compatibility with anything still referencing the old
# single-object schema name.
EVENT_SCHEMA = RESPONSE_SCHEMA


_CONTEXT_HEADER = "[--- earlier messages, already processed — context only ---]"
_NEW_HEADER = "[--- new messages ---]"


def _format_thread(
    thread: dict, today: datetime | None = None, context_marking_enabled: bool = True
) -> str:
    now = today or datetime.now()
    # A naive `now` (production's live wall clock, or the eval harness's
    # pinned date) is presumed to already be local time; attach the local
    # zone without shifting the wall-clock value. An already-aware `now`
    # (not currently produced by any caller) is left as-is.
    now = now if now.tzinfo else now.astimezone()
    local_tz = now.tzinfo

    offset = now.utcoffset() or timedelta(0)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hh, mm = divmod(abs(total_minutes), 60)
    offset_str = f"UTC{sign}{hh:02d}:{mm:02d}"
    tz_abbr = now.strftime("%Z") or offset_str

    today_str = now.strftime("%A, %B %d, %Y")
    participants = ", ".join(thread.get("participants", ["unknown"]))
    lines = [
        f"[Today is {today_str} ({tz_abbr}, {offset_str})]",
        f"[Participants: {participants}]",
        "",
    ]
    messages = thread.get("messages", [])
    # Only insert the context/new-message separators when the thread actually
    # mixes both (some message tagged is_context=True by reader._prepend_context
    # or evals/loader.materialize_polls) — a single-poll thread where every
    # message is "new" renders exactly as it did before this feature existed.
    mark_context = context_marking_enabled and any(m.get("is_context") for m in messages)
    context_header_shown = False
    new_header_shown = False
    for msg in messages:
        if mark_context:
            is_ctx = bool(msg.get("is_context"))
            if is_ctx and not context_header_shown:
                lines.append(_CONTEXT_HEADER)
                context_header_shown = True
            if not is_ctx and not new_header_shown:
                lines.append(_NEW_HEADER)
                new_header_shown = True
        sender = "Me" if msg.get("from_me") else msg.get("sender", "Them")
        sent_at = datetime.fromtimestamp(msg.get("unix_ts", 0), tz=local_tz)
        ts = sent_at.strftime("%a %m/%d/%Y %I:%M%p")
        age_days = (now.date() - sent_at.date()).days
        age_suffix = f", sent {age_days} day{'s' if age_days != 1 else ''} ago" if age_days >= 1 else ""
        lines.append(f"{sender} ({ts}{age_suffix}): {msg.get('text', '')}")
    return "\n".join(lines)


# Variation selectors (FE00-FE0F), emoji tag characters used in flag-sequence
# emoji (E0000-E007F), and zero-width formatting characters. The model quotes
# emoji verbatim from messages but sometimes drops or re-encodes these
# invisible codepoints, which would otherwise break an exact substring match.
_IGNORABLE_CHARS = re.compile(
    "[\uFE00-\uFE0F\U000E0000-\U000E007F\u200B\u200C\u200D\uFEFF]"
)

_QUOTE_TRANSLATION = str.maketrans({
    "‘": "'", "’": "'", "‚": "'",
    "“": '"', "”": '"', "„": '"',
})

# Strips a leading sender/timestamp label the model sometimes prepends when
# quoting evidence, e.g. "Me (07/11 06:46PM, sent 3 days ago): " or
# "+15551234567: ". Tightly anchored (bounded prefix length, requires a
# trailing colon) so real message content like "dinner at 7: ok?" is never
# mistaken for a label.
_SENDER_PREFIX = re.compile(
    r"^\s*(?:me|\+?\d[\d\-() ]{5,14})\s*(?:\([^)\n]{0,60}\))?\s*:\s*"
    r"|^\s*[^:\n()]{1,30}\(\d{1,2}/\d{1,2}[^)]*\)\s*:\s*",
    re.IGNORECASE,
)

_WRAPPING_QUOTES = re.compile(r'^([\'"])(.*)\1$', re.DOTALL)

_WORD_RE = re.compile(r"[a-z0-9']+")

_MIN_FUZZY_TOKENS = 3
_FUZZY_COVERAGE_THRESHOLD = 0.9

# Negation/change-of-plan markers. If the matched message contains one of
# these that the quoted evidence fragment does NOT, the fuzzy match is
# rejected — otherwise an evidence quote could validate its own negation
# ("dinner Friday, NOT at 7" would fuzzy-match evidence "dinner Friday at 7").
_NEGATION_RE = re.compile(
    r"\bnot\b|n't\b|\bno\b|\bnever\b|\bcancel(?:led)?\b|\binstead\b|\bmoved\b",
    re.IGNORECASE,
)

# Verbatim message text is never logged in full — only a short snippet, so
# logs/stdout/*.log doesn't accumulate an unbounded plaintext transcript of
# the user's messages. See state.EVIDENCE_MAX_CHARS for the same tradeoff
# applied to what's persisted to state.json.
_LOG_SNIPPET_CHARS = 80


def _log_snippet(text: str | None) -> str:
    if not text:
        return repr(text)
    if len(text) <= _LOG_SNIPPET_CHARS:
        return repr(text)
    return repr(text[:_LOG_SNIPPET_CHARS].rstrip() + "…")


def _normalize_for_match(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _IGNORABLE_CHARS.sub("", text)
    text = text.translate(_QUOTE_TRANSLATION)
    return re.sub(r"\s+", " ", text).strip().lower()


def _strip_quote_wrappers(fragment: str) -> str:
    """Strip a leading sender/timestamp label and one pair of wrapping quotes
    from a raw (not yet normalized) evidence fragment."""
    fragment = _SENDER_PREFIX.sub("", fragment, count=1)
    m = _WRAPPING_QUOTES.match(fragment.strip())
    if m:
        fragment = m.group(2)
    return fragment


def _ordered_coverage(fragment_tokens: list[str], message_tokens: list[str]) -> float:
    """Fraction of fragment_tokens found in message_tokens IN ORDER (a greedy
    subsequence match: each fragment token must occur at or after the
    position of the previous match). Unlike a token-SET overlap, this can't
    be fooled by a message whose words merely overlap the fragment in a
    different order or with a meaning-flipping word inserted mid-fragment."""
    if not fragment_tokens:
        return 0.0
    pos = 0
    matched = 0
    for tok in fragment_tokens:
        for k in range(pos, len(message_tokens)):
            if message_tokens[k] == tok:
                matched += 1
                pos = k + 1
                break
    return matched / len(fragment_tokens)


def _negation_mismatch(fragment_norm: str, message_norm: str) -> bool:
    """True if the message contains a negation/change-of-plan word the
    evidence fragment doesn't itself quote — a sign the fragment is quoting
    around a negation rather than an affirmative statement."""
    msg_negations = set(_NEGATION_RE.findall(message_norm))
    if not msg_negations:
        return False
    frag_negations = set(_NEGATION_RE.findall(fragment_norm))
    return not msg_negations.issubset(frag_negations)


def _fuzzy_covered(fragment_norm: str, message_norm: str) -> bool:
    ev_tokens = _WORD_RE.findall(fragment_norm)
    if len(ev_tokens) < _MIN_FUZZY_TOKENS:
        return False
    msg_tokens = _WORD_RE.findall(message_norm)
    if not msg_tokens:
        return False
    if _ordered_coverage(ev_tokens, msg_tokens) < _FUZZY_COVERAGE_THRESHOLD:
        return False
    if _negation_mismatch(fragment_norm, message_norm):
        return False
    return True


def _evidence_found(evidence: str, thread: dict) -> bool:
    messages = thread.get("messages", [])
    message_norms = [_normalize_for_match(m.get("text", "")) for m in messages]
    haystack = " ".join(message_norms)

    evidence_norm = _normalize_for_match(evidence)
    if not evidence_norm:
        # Empty/whitespace-only evidence fails the gate rather than bypassing
        # it — a required schema field left blank is not proof of anything.
        return False
    if evidence_norm in haystack:
        return True

    # The model sometimes quotes several messages joined together (a
    # tapback-style "question / answer" pairing, or a multi-line message
    # rendered with embedded newlines). Split on those joiners and require
    # every non-trivial fragment to match — exactly, or via the bounded fuzzy
    # fallback below — against SOME single message in the thread.
    raw_fragments = [f for f in re.split(r"\n+| / ", evidence) if f.strip()]
    if not raw_fragments:
        return False

    for raw_fragment in raw_fragments:
        stripped = _strip_quote_wrappers(raw_fragment)
        fragment_norm = _normalize_for_match(stripped)
        if not fragment_norm:
            continue
        if fragment_norm in haystack:
            continue
        if any(_fuzzy_covered(fragment_norm, msg_norm) for msg_norm in message_norms):
            logger.info(
                "  -> Evidence matched only via fuzzy fallback: %s", _log_snippet(raw_fragment)
            )
            continue
        return False

    return True


_WEEKDAY_INDEX = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

# Matches a bare weekday name/abbreviation, excluding ones preceded by "last"
# or "next" (both name a specific relative occurrence already resolved by the
# model's own date arithmetic — not a signal to shift the detected date via
# this deterministic heuristic). Each lookbehind is fixed-width ("last "/
# "next ", 5 chars) so both are valid in Python's re engine.
_WEEKDAY_MENTION_RE = re.compile(
    r"(?<!last\s)(?<!next\s)\b(" + "|".join(sorted(_WEEKDAY_INDEX, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def _named_weekdays(text: str) -> set[int]:
    return {_WEEKDAY_INDEX[m.lower()] for m in _WEEKDAY_MENTION_RE.findall(text)}


def _reconcile_weekday(event: dict, chat_id) -> None:
    """Deterministically correct a date whose weekday doesn't match the
    weekday named in the model's own evidence — the model computes weekday
    arithmetic unreliably even when the prompt states "today" explicitly.
    Shifts by the minimal signed delta (at most +/-3 days) so a date anchored
    far in the future only nudges within its anchor week; skips when no
    weekday is named, the date already matches, more than one distinct
    weekday is named (ambiguous — do not guess), or the model's own evidence
    already quotes an explicit date (a month name, numeric date, or ordinal
    day) — that anchor wins over weekday-shift heuristics rather than being
    overridden by an incidental weekday mentioned in the same quote."""
    date_str = event.get("date")
    if not date_str:
        return
    try:
        d = _date.fromisoformat(date_str)
    except ValueError:
        return

    evidence_text = event.get("evidence") or ""
    date_evidence_text = event.get("date_evidence") or ""
    if EXPLICIT_DATE_RE.search(evidence_text) or EXPLICIT_DATE_RE.search(date_evidence_text):
        return

    named = _named_weekdays(evidence_text) | _named_weekdays(date_evidence_text)
    if len(named) != 1:
        return
    target = next(iter(named))
    if d.weekday() == target:
        return

    fwd = (target - d.weekday()) % 7
    back = (d.weekday() - target) % 7
    delta = fwd if fwd <= back else -back

    new_date = d + timedelta(days=delta)
    logger.warning(
        "  -> Correcting weekday mismatch in thread %s: %s (%s) -> %s (delta=%+d)",
        chat_id, date_str, d.strftime("%A"), new_date.isoformat(), delta,
    )
    event["date"] = new_date.isoformat()

    end_date_str = event.get("end_date")
    if end_date_str:
        try:
            end_d = _date.fromisoformat(end_date_str)
            event["end_date"] = (end_d + timedelta(days=delta)).isoformat()
        except ValueError:
            pass


def _new_messages(thread: dict) -> list[dict]:
    """The thread's messages that aren't already-processed context — i.e. the
    ones from THIS poll. When the thread carries no context-marking info at
    all (single-poll cases, or context marking disabled), every message
    counts as new, matching pre-F1 behavior."""
    messages = thread.get("messages", [])
    if not any(m.get("is_context") for m in messages):
        return messages
    return [m for m in messages if not m.get("is_context")]


def _new_message_from_user(thread: dict) -> bool:
    """Whether any NEW (this-poll) message is from "Me". Used both by
    _demote_if_user_silent (within-poll group-silence check) and tagged onto
    each event as event["_new_msg_from_user"] for main.py's cross-poll
    anti-flap guard (F7), which has no access to the thread itself."""
    return any(m.get("from_me") for m in _new_messages(thread))


def _demote_if_user_silent(event: dict, thread: dict) -> None:
    """In a multi-participant thread where the user ("Me") never sent a
    message, another person's acceptance can't confirm or tentatively commit
    the plan FOR the user — demote to "unanswered" so the calendar's
    ownership/status gates hold it back until the user actually responds. A
    later real acceptance re-detects and creates the event normally.

    "Group" is judged by distinct senders seen in the thread, not just the
    queried `participants` list — chat_handle_join excludes the user, so a
    2-person group chat can show participants=[one other handle] if a handle
    row is ever missing, indistinguishable from a genuine 1:1 by list length
    alone. Distinct non-"Me" senders is a more reliable group signal.

    Only NEW (this-poll) messages are checked for user silence — an old
    acceptance from a prior poll doesn't retroactively justify a status this
    poll is otherwise not re-establishing from new content."""
    other_senders = {
        m.get("sender") for m in thread.get("messages", []) if not m.get("from_me")
    }
    is_group = len(thread.get("participants", [])) > 1 or len(other_senders) > 1
    if not is_group:
        return
    if event.get("status") not in ("confirmed", "tentative"):
        return
    if _new_message_from_user(thread):
        return
    logger.info(
        "  -> Demoting to unanswered: user never responded in group thread %s",
        thread.get("chat_id"),
    )
    event["status"] = "unanswered"


def detect_plans(
    threads: list[dict],
    model: str = MODEL,
    evidence_gate: bool = True,
    today: datetime | None = None,
    context_marking_enabled: bool = True,
) -> tuple[list[dict], set]:
    """
    Analyze a list of conversation threads for plans.

    Returns (events, failed_chat_ids): events is a list of event dicts across
    all threads (a single thread may contribute zero, one, or several), and
    failed_chat_ids is the set of chat_ids whose API call errored or returned
    an unparseable response, so the caller can hold the watermark back.

    When evidence_gate is true, an event whose quoted evidence cannot be found
    verbatim in the thread is dropped (hallucination guard) instead of merely
    logged.

    `today` is forwarded to `_format_thread` so callers (the eval harness) can
    pin what the model sees as "today"; production callers leave it None and
    get the live wall clock.

    `context_marking_enabled` (mirrors config.DEFAULTS) inserts the
    already-processed-context / new-messages separators (see
    _CONTEXT_MARKING_ADDENDUM) so the model doesn't re-emit a plan whose only
    trace is old context replayed by reader._prepend_context every poll. A
    thread with no is_context-tagged messages (any single-poll case) is
    unaffected either way.
    """
    results = []
    failed_chat_ids = set()
    system_prompt = SYSTEM_PROMPT + (_CONTEXT_MARKING_ADDENDUM if context_marking_enabled else "")

    for thread in threads:
        participants = ", ".join(thread.get("participants", ["unknown"]))
        n_msgs = len(thread.get("messages", []))
        logger.info(
            "Analyzing thread %s with %s (%d message%s)",
            thread["chat_id"],
            participants,
            n_msgs,
            "s" if n_msgs != 1 else "",
        )

        try:
            formatted = _format_thread(
                thread, today=today, context_marking_enabled=context_marking_enabled
            )
            response = _get_client().messages.create(
                model=model,
                max_tokens=2048,
                system=system_prompt,
                messages=[{
                    "role": "user",
                    "content": f"Analyze this iMessage thread for plans:\n\n{formatted}"
                }],
                output_config={"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}}
            )
            usage_tracker.record(model, getattr(response, "usage", None))

            text = next(
                (b.text for b in response.content if b.type == "text"),
                None
            )
            if not text:
                continue

            payload = json.loads(text)

            # Legacy single-object shape (has_event/date at the top level)
            # from an older cached payload or an off-spec model response.
            if "events" not in payload and "has_event" in payload:
                events = [payload] if payload.get("has_event") and payload.get("date") else []
            else:
                events = payload.get("events", [])

            if not events:
                logger.info("  -> No plan detected")
                continue

            for event in events:
                if not event.get("date"):
                    continue

                date_evidence = event.get("date_evidence") or ""
                if not _evidence_found(date_evidence, thread):
                    logger.warning(
                        "  -> Date evidence not found verbatim in thread %s: %s",
                        thread["chat_id"], _log_snippet(date_evidence),
                    )
                    if evidence_gate:
                        logger.warning(
                            "  -> Dropping event with missing/unverifiable date evidence: %s",
                            event.get("title"),
                        )
                        continue

                evidence = event.get("evidence") or ""
                if not _evidence_found(evidence, thread):
                    logger.warning(
                        "  -> Evidence not found verbatim in thread %s: %s",
                        thread["chat_id"], _log_snippet(evidence),
                    )
                    if evidence_gate:
                        logger.warning(
                            "  -> Dropping event with missing/unverifiable evidence: %s",
                            event.get("title"),
                        )
                        continue

                _reconcile_weekday(event, thread["chat_id"])
                _demote_if_user_silent(event, thread)

                logger.info(
                    "  -> Detected %s plan: %s on %s (confidence %.2f)",
                    event.get("status", "confirmed"),
                    event.get("title"),
                    event.get("date"),
                    event.get("confidence", 0),
                )
                event["chat_id"] = thread["chat_id"]
                # Consumed by main.py's anti-flap guard (F7), which has no
                # access to the thread itself — see _new_message_from_user.
                event["_new_msg_from_user"] = _new_message_from_user(thread)
                results.append(event)

        except Exception as e:
            # One malformed response, API error, or unexpected payload must not
            # abort the whole batch — log, remember the failure, and move on.
            logger.warning("Error detecting plans in thread %s: %s", thread.get("chat_id"), e)
            failed_chat_ids.add(thread.get("chat_id"))

    return results, failed_chat_ids

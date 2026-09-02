from datetime import datetime

import httpx

import anthropic
from scheduling_agent import detector


def _thread(chat_id=1, messages=None, participants=("+15551234567",)):
    return {
        "chat_id": chat_id,
        "participants": list(participants),
        "messages": messages or [
            {"sender": "+15551234567", "text": "dinner friday?", "from_me": False, "unix_ts": 1700000000.0},
            {"sender": "me", "text": "yes 7pm", "from_me": True, "unix_ts": 1700000100.0},
        ],
    }


def _event(**overrides):
    base = {
        "title": "Dinner",
        "date": "2026-06-13",
        "time_start": "19:00",
        "time_confidence": 0.95,
        "duration_minutes": 60,
        "location": None,
        "confidence": 0.95,
        "status": "confirmed",
        "user_is_participant": True,
        "participation_evidence": "Me accepted the invitation",
        "recurrence": None,
        "end_date": None,
        "evidence": "yes 7pm",
        # Deliberately neutral (no weekday name, no explicit-date token) so it
        # doesn't collide with tests exercising _reconcile_weekday or the
        # explicit-date-anchor skip via the `evidence` override alone.
        "date_evidence": "yes 7pm",
    }
    base.update(overrides)
    return base


def _response(*events):
    return {"events": list(events)}


def test_confirmed_plan_returns_event_with_chat_id(fake_anthropic):
    fake_anthropic([_response(_event())])

    results, failed = detector.detect_plans([_thread(chat_id=42)])

    assert len(results) == 1
    assert results[0]["title"] == "Dinner"
    assert results[0]["chat_id"] == 42
    assert failed == set()


def test_empty_events_array_is_filtered(fake_anthropic):
    fake_anthropic([_response()])

    results, failed = detector.detect_plans([_thread()])

    assert results == []
    assert failed == set()


def test_null_date_event_is_filtered(fake_anthropic):
    fake_anthropic([_response(_event(date=None))])

    results, failed = detector.detect_plans([_thread()])

    assert results == []


def test_multiple_events_in_one_thread(fake_anthropic):
    fake_anthropic([_response(
        _event(title="Dinner", evidence="dinner friday?", date_evidence="dinner friday?"),
        _event(title="The Game", date="2026-06-14", evidence="game saturday?", date_evidence="game saturday?"),
    )])
    thread = _thread(chat_id=7, messages=[
        {"sender": "+15551234567", "text": "dinner friday? and game saturday?", "from_me": False, "unix_ts": 1700000000.0},
        {"sender": "me", "text": "yes to both", "from_me": True, "unix_ts": 1700000100.0},
    ])

    results, failed = detector.detect_plans([thread])

    assert len(results) == 2
    titles = {r["title"] for r in results}
    assert titles == {"Dinner", "The Game"}
    assert all(r["chat_id"] == 7 for r in results)


def test_legacy_single_object_shape_still_parsed(fake_anthropic):
    legacy_payload = {
        "has_event": True,
        "title": "Dinner",
        "date": "2026-06-13",
        "time_start": "19:00",
        "duration_minutes": 60,
        "location": None,
        "confidence": 0.95,
        "status": "confirmed",
        "recurrence": None,
        "end_date": None,
        "evidence": "yes 7pm",
        "date_evidence": "dinner friday?",
    }
    fake_anthropic([legacy_payload])

    results, failed = detector.detect_plans([_thread()])

    assert len(results) == 1
    assert results[0]["title"] == "Dinner"


def test_legacy_has_event_false_is_filtered(fake_anthropic):
    fake_anthropic([{"has_event": False, "date": None}])

    results, failed = detector.detect_plans([_thread()])

    assert results == []


def test_malformed_json_skips_thread_but_continues(fake_anthropic):
    # First thread returns junk, second returns a valid event.
    fake_anthropic(["this is not json", _response(_event())])

    results, failed = detector.detect_plans([_thread(chat_id=1), _thread(chat_id=2)])

    assert len(results) == 1
    assert results[0]["chat_id"] == 2
    assert failed == {1}


def test_api_error_skips_thread_but_continues(fake_anthropic):
    err = anthropic.APIConnectionError(
        message="boom", request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    fake_anthropic([err, _response(_event())])

    results, failed = detector.detect_plans([_thread(chat_id=1), _thread(chat_id=2)])

    assert len(results) == 1
    assert results[0]["chat_id"] == 2
    assert failed == {1}


def test_message_missing_text_key_is_formatted_as_blank_not_a_crash(fake_anthropic):
    # A message with no "text" key (e.g. an unsupported attachment-only
    # message) must not raise a KeyError out of _format_thread.
    thread = _thread(chat_id=1, messages=[
        {"sender": "+15551234567", "from_me": False, "unix_ts": 1700000000.0},
    ])
    # Gate disabled: missing evidence is expected on a text-less thread, and
    # this test is only about _format_thread not raising, not the gate.
    fake_anthropic([_response(_event(evidence=None, date_evidence=None))])

    results, failed = detector.detect_plans([thread], evidence_gate=False)

    assert failed == set()
    assert len(results) == 1


def test_format_thread_failure_skips_thread_but_continues(fake_anthropic, monkeypatch):
    # Simulate a malformed thread that blows up inside _format_thread itself
    # (e.g. a non-numeric timestamp). Formatting now happens inside the
    # per-thread try block, so this must fail in isolation rather than
    # aborting the whole batch.
    bad_thread = _thread(chat_id=1, messages=[
        {"sender": "+15551234567", "text": "hi", "from_me": False, "unix_ts": "not-a-number"},
    ])
    client = fake_anthropic([_response(_event())])

    results, failed = detector.detect_plans([bad_thread, _thread(chat_id=2)])

    assert failed == {1}
    assert len(results) == 1
    assert results[0]["chat_id"] == 2
    # Only the healthy second thread reached the API call.
    assert len(client.messages.calls) == 1


def test_evidence_not_found_drops_event_by_default(fake_anthropic, caplog):
    fake_anthropic([_response(_event(evidence="this text is nowhere in the thread"))])

    with caplog.at_level("WARNING"):
        results, failed = detector.detect_plans([_thread()])

    assert results == []  # hallucination guard: unverifiable evidence drops the event
    assert failed == set()  # a gated drop is not a thread failure
    assert any("Evidence not found verbatim" in r.message for r in caplog.records)


def test_evidence_not_found_kept_when_gate_disabled(fake_anthropic, caplog):
    fake_anthropic([_response(_event(evidence="this text is nowhere in the thread"))])

    with caplog.at_level("WARNING"):
        results, failed = detector.detect_plans([_thread()], evidence_gate=False)

    assert len(results) == 1
    assert any("Evidence not found verbatim" in r.message for r in caplog.records)


def test_verbatim_evidence_passes_gate(fake_anthropic):
    fake_anthropic([_response(_event(evidence="yes 7pm"))])

    results, failed = detector.detect_plans([_thread()])

    assert len(results) == 1


def test_new_schema_fields_pass_through(fake_anthropic):
    fake_anthropic([_response(_event(user_is_participant=False, status="unanswered"))])

    results, failed = detector.detect_plans([_thread()])

    assert len(results) == 1
    assert results[0]["user_is_participant"] is False
    assert results[0]["status"] == "unanswered"


def test_format_thread_is_deterministic_with_injected_today():
    today = datetime(2026, 6, 10, 9, 0, 0)
    thread = _thread(messages=[
        {"sender": "+15551234567", "text": "lunch?", "from_me": False, "unix_ts": 1700000000.0},
        {"sender": "me", "text": "sure", "from_me": True, "unix_ts": 1700000100.0},
    ])

    out = detector._format_thread(thread, today=today)

    # Header now also carries local timezone info (F6a); the abbreviation
    # itself is system-dependent, so only check the date/UTC-offset parts.
    assert out.startswith("[Today is Wednesday, June 10, 2026 (")
    assert "UTC" in out.splitlines()[0]
    assert "[Participants: +15551234567]" in out
    assert "Me (" in out  # sent message labeled Me
    assert ": lunch?" in out


def test_format_thread_annotates_stale_messages_with_age():
    today = datetime(2026, 6, 13, 9, 0, 0)
    three_days_ago = datetime(2026, 6, 10, 18, 0, 0).timestamp()
    thread = _thread(messages=[
        {"sender": "+15551234567", "text": "dinner tomorrow?", "from_me": False, "unix_ts": three_days_ago},
    ])

    out = detector._format_thread(thread, today=today)

    assert "sent 3 days ago" in out


def test_format_thread_omits_age_for_recent_messages():
    today = datetime(2026, 6, 13, 9, 0, 0)
    same_day = datetime(2026, 6, 13, 8, 0, 0).timestamp()
    thread = _thread(messages=[
        {"sender": "+15551234567", "text": "lunch?", "from_me": False, "unix_ts": same_day},
    ])

    out = detector._format_thread(thread, today=today)

    assert "sent" not in out


def test_create_call_uses_expected_model_system_and_schema(fake_anthropic):
    client = fake_anthropic([_response(_event())])

    detector.detect_plans([_thread()])

    call = client.messages.calls[0]
    assert call["model"] == detector.MODEL
    assert call["system"] == detector.SYSTEM_PROMPT
    assert call["output_config"]["format"]["schema"] is detector.RESPONSE_SCHEMA


def test_model_override_is_passed_through(fake_anthropic):
    client = fake_anthropic([_response(_event())])

    detector.detect_plans([_thread()], model="claude-sonnet-4-6")

    assert client.messages.calls[0]["model"] == "claude-sonnet-4-6"


def test_detect_plans_forwards_today_to_the_prompt(fake_anthropic):
    client = fake_anthropic([_response(_event())])
    pinned = datetime(2026, 6, 10, 12, 0, 0)

    detector.detect_plans([_thread()], today=pinned)

    sent = client.messages.calls[0]["messages"][0]["content"]
    assert "[Today is Wednesday, June 10, 2026 (" in sent


def test_date_evidence_not_found_drops_event(fake_anthropic, caplog):
    fake_anthropic([_response(_event(date_evidence="this text is nowhere in the thread"))])

    with caplog.at_level("WARNING"):
        results, failed = detector.detect_plans([_thread()])

    assert results == []  # hallucinated date anchor: the event is dropped
    assert failed == set()  # a gated drop is not a thread failure
    assert any("Date evidence not found verbatim" in r.message for r in caplog.records)


def test_date_evidence_not_found_kept_when_gate_disabled(fake_anthropic, caplog):
    fake_anthropic([_response(_event(date_evidence="this text is nowhere in the thread"))])

    with caplog.at_level("WARNING"):
        results, failed = detector.detect_plans([_thread()], evidence_gate=False)

    assert len(results) == 1
    assert any("Date evidence not found verbatim" in r.message for r in caplog.records)


def test_verbatim_date_evidence_passes_gate(fake_anthropic):
    fake_anthropic([_response(_event(date_evidence="dinner friday?"))])

    results, failed = detector.detect_plans([_thread()])

    assert len(results) == 1


def test_missing_date_evidence_field_keeps_event(fake_anthropic):
    # Legacy payloads (or an off-spec response) without the field must pass.
    fake_anthropic([_response(_event())])

    results, failed = detector.detect_plans([_thread()])

    assert len(results) == 1


def test_schema_requires_date_evidence():
    assert "date_evidence" in detector.EVENT_ITEM_SCHEMA["properties"]
    assert "date_evidence" in detector.EVENT_ITEM_SCHEMA["required"]


def test_date_evidence_with_sender_timestamp_prefix_passes_gate(fake_anthropic):
    # The date_evidence gate calls the same _evidence_found as the evidence
    # gate, so it must inherit the same prefix/quote tolerance.
    thread = _thread(messages=[
        {"sender": "+15551234567", "text": "reminder: the reunion is Sunday, August 30", "from_me": False, "unix_ts": 1700000000.0},
    ])
    fake_anthropic([_response(_event(
        evidence="reminder: the reunion is Sunday, August 30",
        date_evidence="Me (07/28 09:00AM, sent 33 days ago): reminder: the reunion is Sunday, August 30",
    ))])

    results, failed = detector.detect_plans([thread])

    assert len(results) == 1


# --- Evidence matcher robustness (real failure modes from production logs) --


def test_evidence_sender_timestamp_prefix_passes(fake_anthropic):
    thread = _thread(messages=[
        {"sender": "+15551234567", "text": "9:30 or 10 start? Gonna book 2 hours", "from_me": False, "unix_ts": 1700000000.0},
    ])
    fake_anthropic([_response(_event(
        evidence="Me (07/11 06:46PM, sent 3 days ago): 9:30 or 10 start? Gonna book 2 hours",
        date_evidence="9:30 or 10 start? Gonna book 2 hours",
    ))])

    results, failed = detector.detect_plans([thread])

    assert len(results) == 1


def test_evidence_me_colon_prefix_with_wrapping_quotes_passes(fake_anthropic):
    thread = _thread(messages=[
        {"sender": "me", "text": "I'm locked in to going to the cubs game at 8", "from_me": True, "unix_ts": 1700000000.0},
    ])
    fake_anthropic([_response(_event(
        evidence="Me: 'I'm locked in to going to the cubs game at 8'",
        date_evidence="I'm locked in to going to the cubs game at 8",
    ))])

    results, failed = detector.detect_plans([thread])

    assert len(results) == 1


def test_evidence_phone_prefix_passes(fake_anthropic):
    thread = _thread(messages=[
        {"sender": "+15551234567", "text": "Yes for sure", "from_me": False, "unix_ts": 1700000000.0},
    ])
    fake_anthropic([_response(_event(
        evidence="+15551234567: 'Yes for sure'", date_evidence="Yes for sure",
    ))])

    results, failed = detector.detect_plans([thread])

    assert len(results) == 1


def test_evidence_curly_vs_straight_apostrophe_passes(fake_anthropic):
    thread = _thread(messages=[
        {"sender": "+15551234567", "text": "I'm in!", "from_me": False, "unix_ts": 1700000000.0},
    ])
    fake_anthropic([_response(_event(evidence="I’m in!", date_evidence="I’m in!"))])

    results, failed = detector.detect_plans([thread])

    assert len(results) == 1


def test_evidence_emoji_variation_selector_and_flag_tags_pass(fake_anthropic):
    thread = _thread(messages=[
        {"sender": "+15551234567", "text": "Gonna watch \U0001f1f3\U0001f1f4 game on wells", "from_me": False, "unix_ts": 1700000000.0},
    ])
    # Model quotes the same text but with a variation selector inserted.
    fake_anthropic([_response(_event(
        evidence="Gonna watch \U0001f1f3\U0001f1f4️ game on wells",
        date_evidence="Gonna watch \U0001f1f3\U0001f1f4️ game on wells",
    ))])

    results, failed = detector.detect_plans([thread])

    assert len(results) == 1


def test_evidence_slash_joined_multi_message_passes(fake_anthropic):
    thread = _thread(messages=[
        {"sender": "+15551234567", "text": "Do you wanna come?", "from_me": False, "unix_ts": 1700000000.0},
        {"sender": "me", "text": "Yes", "from_me": True, "unix_ts": 1700000100.0},
    ])
    fake_anthropic([_response(_event(
        evidence="Do you wanna come? / Yes", date_evidence="Do you wanna come? / Yes",
    ))])

    results, failed = detector.detect_plans([thread])

    assert len(results) == 1


def test_evidence_newline_joined_multiline_message_passes(fake_anthropic):
    thread = _thread(messages=[
        {"sender": "+15551234567", "text": "So I booked Ravisloe for the 22nd.", "from_me": False, "unix_ts": 1700000000.0},
        {"sender": "+15551234567", "text": "First tee time is 10:50.", "from_me": False, "unix_ts": 1700000100.0},
    ])
    fake_anthropic([_response(_event(
        evidence="So I booked Ravisloe for the 22nd.\nFirst tee time is 10:50.",
        date_evidence="So I booked Ravisloe for the 22nd.",
    ))])

    results, failed = detector.detect_plans([thread])

    assert len(results) == 1


def test_evidence_minor_paraphrase_within_overlap_passes(fake_anthropic):
    thread = _thread(messages=[
        {"sender": "+15551234567", "text": "want to grab dinner at the new taco place friday around 7", "from_me": False, "unix_ts": 1700000000.0},
    ])
    fake_anthropic([_response(_event(
        evidence="want to grab dinner at the taco place this friday around 7",
        date_evidence="want to grab dinner at the taco place this friday around 7",
    ))])

    results, failed = detector.detect_plans([thread])

    assert len(results) == 1


def test_evidence_fabricated_details_still_dropped(fake_anthropic, caplog):
    thread = _thread(messages=[
        {"sender": "+15551234567", "text": "dinner friday?", "from_me": False, "unix_ts": 1700000000.0},
    ])
    fake_anthropic([_response(_event(
        evidence="dinner at 9pm on friday at the fancy new steakhouse downtown"
    ))])

    with caplog.at_level("WARNING"):
        results, failed = detector.detect_plans([thread])

    assert results == []


def test_evidence_prefix_strip_does_not_eat_real_colon_content(fake_anthropic):
    thread = _thread(messages=[
        {"sender": "+15551234567", "text": "dinner at 7: does that work for you?", "from_me": False, "unix_ts": 1700000000.0},
    ])
    fake_anthropic([_response(_event(
        evidence="dinner at 7: does that work for you?",
        date_evidence="dinner at 7: does that work for you?",
    ))])

    results, failed = detector.detect_plans([thread])

    assert len(results) == 1


# --- Weekday math hardening (_reconcile_weekday) ----------------------------


def test_reconcile_weekday_shifts_back_one_day():
    # 2026-07-21 is a Tuesday; "this Monday" should be 2026-07-20.
    event = _event(date="2026-07-21", evidence="can we set up standup this Monday?")
    detector._reconcile_weekday(event, chat_id=1)
    assert event["date"] == "2026-07-20"


def test_reconcile_weekday_shifts_forward_when_nearer():
    # 2026-07-14 is a Tuesday; nearest Wednesday is forward by 1 day.
    event = _event(date="2026-07-14", evidence="dinner this Wednesday?")
    detector._reconcile_weekday(event, chat_id=1)
    assert event["date"] == "2026-07-15"


def test_reconcile_weekday_leaves_matching_date_untouched():
    # 2026-07-16 is already a Thursday.
    event = _event(date="2026-07-16", evidence="running club this Thursday?")
    detector._reconcile_weekday(event, chat_id=1)
    assert event["date"] == "2026-07-16"


def test_reconcile_weekday_leaves_ambiguous_multi_weekday_untouched():
    event = _event(date="2026-07-21", evidence="Monday or Tuesday works for the standup")
    detector._reconcile_weekday(event, chat_id=1)
    assert event["date"] == "2026-07-21"


def test_reconcile_weekday_ignores_last_weekday_mention():
    event = _event(date="2026-07-21", evidence="like we talked about last Friday, let's meet up")
    detector._reconcile_weekday(event, chat_id=1)
    assert event["date"] == "2026-07-21"


def test_reconcile_weekday_coshifts_end_date():
    event = _event(
        date="2026-07-21", end_date="2026-07-23", evidence="trip starts this Monday"
    )
    detector._reconcile_weekday(event, chat_id=1)
    assert event["date"] == "2026-07-20"
    assert event["end_date"] == "2026-07-22"


def test_reconcile_weekday_far_anchor_only_nudged_within_week():
    # A far-future date whose weekday is one day off should shift by 1 day,
    # never snap back to "next occurrence of that weekday from today".
    event = _event(date="2026-08-31", evidence="on Sunday we should all get dinner")
    detector._reconcile_weekday(event, chat_id=1)
    assert event["date"] == "2026-08-30"


def test_reconcile_weekday_checks_date_evidence_field_too():
    event = _event(date="2026-07-21", evidence="yes!", date_evidence="dinner this Monday?")
    detector._reconcile_weekday(event, chat_id=1)
    assert event["date"] == "2026-07-20"


def test_reconcile_weekday_ignores_next_weekday_mention():
    # "next Friday" already went through the model's own date arithmetic —
    # the deterministic weekday-shift heuristic must not second-guess it.
    event = _event(date="2026-07-21", evidence="let's do dinner next Friday")
    detector._reconcile_weekday(event, chat_id=1)
    assert event["date"] == "2026-07-21"


def test_reconcile_weekday_skips_when_explicit_date_in_evidence():
    # An incidental weekday ("Thursday") sits alongside an explicit date
    # (the 14th) in the same quote — the explicit date wins and the
    # deterministic weekday nudge must not override a correct date.
    event = _event(
        date="2026-06-13",  # a Saturday
        evidence="party's on the 13th",
        date_evidence="Jess's party is the 13th — I fly in that Thursday btw",
    )
    detector._reconcile_weekday(event, chat_id=1)
    assert event["date"] == "2026-06-13"


def test_reconcile_weekday_skips_when_explicit_month_date_in_evidence():
    event = _event(
        date="2026-06-13",  # a Saturday
        evidence="see you Thursday for the trip, dinner's June 13th though",
        date_evidence="see you Thursday for the trip, dinner's June 13th though",
    )
    detector._reconcile_weekday(event, chat_id=1)
    assert event["date"] == "2026-06-13"


# --- Evidence gate: empty/missing evidence (F5a) ----------------------------


def test_missing_evidence_is_dropped_not_bypassed(fake_anthropic, caplog):
    # A required schema field left blank/missing must fail the gate, not
    # silently pass it — this is the hallucination-guard bypass fix.
    fake_anthropic([_response(_event(evidence=""))])

    with caplog.at_level("WARNING"):
        results, failed = detector.detect_plans([_thread()])

    assert results == []
    assert failed == set()


def test_missing_date_evidence_is_dropped_not_bypassed(fake_anthropic, caplog):
    fake_anthropic([_response(_event(date_evidence=""))])

    with caplog.at_level("WARNING"):
        results, failed = detector.detect_plans([_thread()])

    assert results == []
    assert failed == set()


def test_evidence_found_rejects_empty_string_directly():
    assert detector._evidence_found("", _thread()) is False
    assert detector._evidence_found("   ", _thread()) is False


# --- Fuzzy evidence: ordered subsequence + negation guard (F5b) ------------


def test_fuzzy_evidence_rejects_reordered_tokens():
    # Same token SET as the message but in a different order — the old
    # set-overlap check would accept this; the ordered-subsequence check
    # must not, since word order changes what a quote actually claims.
    message_norm = detector._normalize_for_match("dinner friday at the new place around 7")
    reordered_norm = detector._normalize_for_match("7 around place new the at friday dinner")
    assert detector._fuzzy_covered(reordered_norm, message_norm) is False


def test_fuzzy_evidence_accepts_in_order_paraphrase_with_skipped_words():
    message_norm = detector._normalize_for_match("want to grab dinner at the new taco place friday around 7")
    fragment_norm = detector._normalize_for_match("want to grab dinner at the taco place this friday around 7")
    assert detector._fuzzy_covered(fragment_norm, message_norm) is True


def test_fuzzy_evidence_rejects_negation_not_quoted_in_fragment():
    # The message negates the plan; a fragment that drops the negation word
    # would otherwise fuzzy-match as if it were an affirmative statement.
    message_norm = detector._normalize_for_match("dinner friday at 7, actually not at 7 my bad")
    fragment_norm = detector._normalize_for_match("dinner friday at 7")
    assert detector._fuzzy_covered(fragment_norm, message_norm) is False


def test_fuzzy_evidence_allows_negation_when_fragment_quotes_it_too():
    message_norm = detector._normalize_for_match("we are not doing trivia friday anymore, sorry")
    fragment_norm = detector._normalize_for_match("we are not doing trivia friday anymore")
    assert detector._fuzzy_covered(fragment_norm, message_norm) is True


# --- Group-silence participation demotion (_demote_if_user_silent) ---------


def test_demote_group_thread_with_zero_from_me_messages():
    thread = _thread(participants=("+15551111111", "+15552222222"), messages=[
        {"sender": "+15551111111", "text": "gym at 6?", "from_me": False, "unix_ts": 1700000000.0},
        {"sender": "+15552222222", "text": "yes! see you there", "from_me": False, "unix_ts": 1700000100.0},
    ])
    event = _event(status="confirmed")
    detector._demote_if_user_silent(event, thread)
    assert event["status"] == "unanswered"


def test_no_demote_when_user_sent_a_message():
    thread = _thread(participants=("+15551111111", "+15552222222"), messages=[
        {"sender": "+15551111111", "text": "gym at 6?", "from_me": False, "unix_ts": 1700000000.0},
        {"sender": "me", "text": "yes!", "from_me": True, "unix_ts": 1700000100.0},
    ])
    event = _event(status="confirmed")
    detector._demote_if_user_silent(event, thread)
    assert event["status"] == "confirmed"


def test_no_demote_in_one_on_one_thread():
    thread = _thread(participants=("+15551234567",), messages=[
        {"sender": "+15551234567", "text": "dinner friday?", "from_me": False, "unix_ts": 1700000000.0},
    ])
    event = _event(status="confirmed")
    detector._demote_if_user_silent(event, thread)
    assert event["status"] == "confirmed"


def test_demote_leaves_already_unanswered_status_untouched():
    thread = _thread(participants=("+15551111111", "+15552222222"), messages=[
        {"sender": "+15551111111", "text": "gym at 6?", "from_me": False, "unix_ts": 1700000000.0},
    ])
    event = _event(status="unanswered")
    detector._demote_if_user_silent(event, thread)
    assert event["status"] == "unanswered"

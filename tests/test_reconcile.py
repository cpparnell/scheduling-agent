import pytest

from scheduling_agent import calendar, config, reconcile, state


def _cfg(**overrides):
    cfg = {
        "dedup_enabled": True,
        "dedup_model": config.DEFAULTS["dedup_model"],
        "dedup_day_window": 1,
        "dedup_fail_open": True,
        "calendar_query_enabled": False,
        "fuzzy_title_threshold": 0.6,
        "target_calendar": "Calendar",
    }
    cfg.update(overrides)
    return cfg


def _event(**overrides):
    base = {
        "chat_id": 1,
        "date": "2099-01-15",
        "time_start": "19:00",
        "title": "Dinner with Sam",
        "location": None,
        "status": "confirmed",
        "evidence": "dinner at 7?",
        "confidence": 0.95,
    }
    base.update(overrides)
    return base


# --- fuzzy matcher ------------------------------------------------------------


@pytest.mark.parametrize(
    "event_kw,candidate,should_match",
    [
        # Identical title, different punctuation/case
        ({"title": "Dinner with Sam"}, {"date": "2099-01-15", "time_start": "19:00", "title": "dinner with sam!"}, True),
        # Small time drift (within 120 min)
        ({"title": "Dinner with Sam", "time_start": "19:30"}, {"date": "2099-01-15", "time_start": "19:00", "title": "Dinner with Sam"}, True),
        # Large time gap — different slot
        ({"title": "Dinner with Sam", "time_start": "22:30"}, {"date": "2099-01-15", "time_start": "19:00", "title": "Dinner with Sam"}, False),
        # All-day candidate matches a timed detection
        ({"title": "Dinner with Sam"}, {"date": "2099-01-15", "time_start": None, "title": "Dinner with Sam"}, True),
        # Adjacent date (reschedule drift)
        ({"title": "Dinner with Sam"}, {"date": "2099-01-16", "time_start": "19:00", "title": "Dinner with Sam"}, True),
        # Date too far away
        ({"title": "Dinner with Sam"}, {"date": "2099-01-18", "time_start": "19:00", "title": "Dinner with Sam"}, False),
        # Same date+time but a clearly different activity
        ({"title": "Dinner with Sam"}, {"date": "2099-01-15", "time_start": "19:00", "title": "Work call"}, False),
        # Loosely reworded title — below threshold, left for the LLM layer
        ({"title": "Dinner with Sam"}, {"date": "2099-01-15", "time_start": "19:00", "title": "Dinner w/ Samantha"}, False),
    ],
)
def test_fuzzy_match_table(event_kw, candidate, should_match):
    got = reconcile.fuzzy_match(_event(**event_kw), [candidate], title_threshold=0.6)
    assert (got is not None) == should_match


def test_fuzzy_match_is_chat_agnostic():
    candidate = {"chat_id": 999, "date": "2099-01-15", "time_start": "19:00", "title": "Dinner with Sam"}
    assert reconcile.fuzzy_match(_event(chat_id=1), [candidate], 0.6) is candidate


def test_fuzzy_match_prefers_highest_similarity():
    weaker = {"date": "2099-01-15", "time_start": "19:00", "title": "dinner with sam and co"}
    exact = {"date": "2099-01-15", "time_start": "19:00", "title": "Dinner with Sam"}
    assert reconcile.fuzzy_match(_event(), [weaker, exact], 0.6) is exact


# --- candidate assembly -------------------------------------------------------


def test_assemble_candidates_merges_calendar_and_dedupes_by_uid(monkeypatch):
    state.record_event(1, "2099-01-15", "19:00", "Dinner", calendar_uid="UID-1")
    monkeypatch.setattr(
        calendar, "get_events_near",
        lambda *a, **k: [
            {"title": "Dinner", "date": "2099-01-15", "time_start": "19:00",
             "location": None, "calendar_uid": "UID-1", "source": "calendar"},
            {"title": "Manually added thing", "date": "2099-01-15", "time_start": None,
             "location": None, "calendar_uid": "UID-2", "source": "calendar"},
        ],
    )

    candidates = reconcile._assemble_candidates(_event(), _cfg(calendar_query_enabled=True))

    uids = [c.get("calendar_uid") for c in candidates]
    assert uids.count("UID-1") == 1  # own record not listed twice
    assert "UID-2" in uids


def test_assemble_candidates_skips_calendar_when_disabled(monkeypatch):
    called = []
    monkeypatch.setattr(calendar, "get_events_near", lambda *a, **k: called.append(1) or [])

    reconcile._assemble_candidates(_event(), _cfg(calendar_query_enabled=False))

    assert called == []


# --- disposition matrix -------------------------------------------------------


def _record(**overrides):
    base = state.make_record(2, "2099-01-15", "19:00", "Dinner with Sam", status="confirmed", confidence=0.9)
    base.update(overrides)
    return base


def test_disposition_calendar_only_match_never_updates():
    matched = {"title": "Dinner with Sam", "date": "2099-01-15", "time_start": "20:00",
               "location": None, "calendar_uid": "UID-9", "source": "calendar"}
    decision = reconcile._disposition(_event(), matched, "fuzzy", None)
    assert decision.action == "skip_duplicate"


def test_disposition_lower_confidence_detection_never_updates():
    decision = reconcile._disposition(
        _event(confidence=0.5, time_start="20:00"), _record(confidence=0.9), "llm", None
    )
    assert decision.action == "skip_duplicate"


def test_disposition_time_drift_updates():
    decision = reconcile._disposition(_event(time_start="20:00"), _record(), "fuzzy", None)
    assert decision.action == "update"
    assert decision.changes == {"time_start": "20:00"}


def test_disposition_new_location_updates():
    decision = reconcile._disposition(_event(location="Lucia's"), _record(location=None), "fuzzy", None)
    assert decision.action == "update"
    assert decision.changes == {"location": "Lucia's"}


def test_disposition_does_not_overwrite_existing_location():
    decision = reconcile._disposition(_event(location="Lucia's"), _record(location="Dicey's"), "fuzzy", None)
    assert decision.action == "skip_duplicate"


def test_disposition_tentative_to_confirmed_upgrades():
    decision = reconcile._disposition(_event(status="confirmed"), _record(status="tentative"), "llm", None)
    assert decision.action == "update"
    assert decision.changes == {"status": "confirmed"}


def test_disposition_never_downgrades_confirmed_to_tentative():
    decision = reconcile._disposition(_event(status="tentative"), _record(status="confirmed"), "llm", None)
    assert decision.action == "skip_duplicate"


def test_disposition_allday_detection_does_not_erase_time():
    # A vaguer re-mention (all-day) of a timed event must not strip the time.
    decision = reconcile._disposition(_event(time_start=None), _record(time_start="19:00"), "fuzzy", None)
    assert decision.action == "skip_duplicate"


def test_disposition_no_material_diff_skips():
    decision = reconcile._disposition(_event(), _record(), "fuzzy", None)
    assert decision.action == "skip_duplicate"


def test_disposition_date_drift_updates():
    decision = reconcile._disposition(_event(date="2099-01-16"), _record(), "fuzzy", None)
    assert decision.action == "update"
    assert decision.changes == {"date": "2099-01-16"}


# --- reconcile() wiring -------------------------------------------------------


def test_exact_duplicate_short_circuits():
    state.record_event(1, "2099-01-15", "19:00", "Dinner with Sam")
    decision = reconcile.reconcile(_event(), _cfg())
    assert decision.action == "skip_duplicate"
    assert decision.source == "exact"


def test_no_candidates_creates():
    decision = reconcile.reconcile(_event(), _cfg())
    assert decision.action == "create"


def test_fuzzy_match_skips_without_llm(fake_dedup_anthropic):
    client = fake_dedup_anthropic([{"is_duplicate": True, "duplicate_of": 0, "reasoning": "n/a"}])
    # Same plan recorded from a DIFFERENT chat — exact layer can't see it.
    state.record_event(2, "2099-01-15", "19:00", "Dinner with Sam", confidence=0.95)

    decision = reconcile.reconcile(_event(chat_id=1), _cfg())

    assert decision.action == "skip_duplicate"
    assert decision.source == "fuzzy"
    assert client.messages.calls == []


def test_fuzzy_match_with_time_drift_updates():
    state.record_event(2, "2099-01-15", "19:00", "Dinner with Sam", confidence=0.9)

    decision = reconcile.reconcile(_event(chat_id=1, time_start="19:30"), _cfg())

    assert decision.action == "update"
    assert decision.changes == {"time_start": "19:30"}
    assert decision.matched["canonical_id"]


def test_llm_layer_duplicate_verdict_skips(fake_dedup_anthropic):
    fake_dedup_anthropic([{"is_duplicate": True, "duplicate_of": 0, "reasoning": "same plan reworded"}])
    state.record_event(2, "2099-01-15", "19:00", "Sam bday dinner", confidence=0.95)

    decision = reconcile.reconcile(_event(chat_id=1), _cfg())

    assert decision.action == "skip_duplicate"
    assert decision.source == "llm"
    assert decision.reasoning == "same plan reworded"


def test_llm_layer_different_verdict_creates(fake_dedup_anthropic):
    fake_dedup_anthropic([{"is_duplicate": False, "duplicate_of": None, "reasoning": "different"}])
    state.record_event(2, "2099-01-15", "12:00", "Work call", confidence=0.95)

    decision = reconcile.reconcile(_event(chat_id=1), _cfg())

    assert decision.action == "create"


def test_llm_out_of_range_duplicate_of_still_skips(fake_dedup_anthropic, caplog):
    fake_dedup_anthropic([{"is_duplicate": True, "duplicate_of": 7, "reasoning": "same"}])
    state.record_event(2, "2099-01-15", "12:00", "Sam thing", confidence=0.95)

    with caplog.at_level("WARNING"):
        decision = reconcile.reconcile(_event(chat_id=1), _cfg())

    assert decision.action == "skip_duplicate"
    assert decision.matched is None
    assert "out-of-range duplicate_of" in caplog.text


def test_llm_failure_fail_open_creates(fake_dedup_anthropic):
    fake_dedup_anthropic(["not json"])
    state.record_event(2, "2099-01-15", "12:00", "Sam thing", confidence=0.95)

    decision = reconcile.reconcile(_event(chat_id=1), _cfg(dedup_fail_open=True))

    assert decision.action == "create"


def test_llm_failure_fail_closed_skips(fake_dedup_anthropic):
    fake_dedup_anthropic(["not json"])
    state.record_event(2, "2099-01-15", "12:00", "Sam thing", confidence=0.95)

    decision = reconcile.reconcile(_event(chat_id=1), _cfg(dedup_fail_open=False))

    assert decision.action == "skip_error"


def test_dedup_disabled_skips_llm_but_keeps_fuzzy(fake_dedup_anthropic):
    client = fake_dedup_anthropic([{"is_duplicate": True, "duplicate_of": 0, "reasoning": "n/a"}])
    state.record_event(2, "2099-01-15", "19:00", "Sam bday dinner", confidence=0.95)

    decision = reconcile.reconcile(_event(chat_id=1), _cfg(dedup_enabled=False))

    assert decision.action == "create"  # fuzzy can't match the reworded title; LLM never runs
    assert client.messages.calls == []


def test_pending_journal_record_is_visible_to_reconcile():
    # A create that has been journaled but not yet committed must still block
    # a rewording of the same plan from another chat.
    record = state.make_record(2, "2099-01-15", "19:00", "Dinner with Sam", confidence=0.95)
    state.journal_intent(record)

    decision = reconcile.reconcile(_event(chat_id=1), _cfg())

    assert decision.action == "skip_duplicate"
    assert decision.source == "fuzzy"


def test_calendar_only_candidate_reaches_llm(monkeypatch, fake_dedup_anthropic):
    fake_dedup_anthropic([{"is_duplicate": True, "duplicate_of": 0, "reasoning": "manually created"}])
    monkeypatch.setattr(
        calendar, "get_events_near",
        lambda *a, **k: [{"title": "Sam bday celebration", "date": "2099-01-15", "time_start": "19:00",
                          "location": None, "calendar_uid": "UID-7", "source": "calendar"}],
    )

    decision = reconcile.reconcile(_event(chat_id=1), _cfg(calendar_query_enabled=True))

    assert decision.action == "skip_duplicate"
    assert decision.matched["calendar_uid"] == "UID-7"

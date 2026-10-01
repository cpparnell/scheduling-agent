from datetime import datetime
from types import SimpleNamespace

import pytest

from scheduling_agent import detector, jev_client, jev_detector, usage_tracker

# Wednesday, September 30 2026, noon local.
WED = datetime(2026, 9, 30, 12, 0).timestamp()


def _thread(chat_id=1, messages=None, participants=("+15551234567",)):
    return {
        "chat_id": chat_id,
        "participants": list(participants),
        "messages": messages or [
            {"sender": "+15551234567", "text": "dinner saturday at 7pm?", "from_me": False, "unix_ts": WED},
            {"sender": "me", "text": "yes!", "from_me": True, "unix_ts": WED + 60},
        ],
    }


def _noul(p):
    return SimpleNamespace(noul=p)


def _choice(label, confidence=0.95):
    return SimpleNamespace(choice=label, confidence=confidence, probabilities={label: confidence})


def _resp(**overrides):
    """A confident, fast-path-eligible Jev answer for _thread()'s default
    messages; override any question's answer."""
    nouls = {"has_plan": 0.97, "multiple_plans": 0.02, "participant": 0.98}
    choices = {
        "status": _choice("confirmed"),
        "date": _choice("2026-10-03"),
        "time": _choice("19:00"),
        "activity": _choice("dinner"),
    }
    for name, value in overrides.items():
        if value is None:
            nouls.pop(name, None)
            choices.pop(name, None)
        elif isinstance(value, float):
            nouls[name] = value
        else:
            choices[name] = value
    return SimpleNamespace(
        nouls={k: _noul(v) for k, v in nouls.items()},
        choices=choices,
    )


@pytest.fixture
def fake_jev(monkeypatch):
    """Install a fake jev_client.ask returning `resp` (or raising it, if it's
    an exception). Returns the list of (state, questions) calls."""
    calls = []

    def install(resp):
        def ask(state, questions):
            calls.append((state, questions))
            if isinstance(resp, Exception):
                raise resp
            return resp
        monkeypatch.setattr(jev_client, "ask", ask)
        return calls

    return install


@pytest.fixture
def fake_haiku(monkeypatch):
    """Replace the Claude detector used on the fallback path; records the
    threads it was handed."""
    calls = []

    def install(events=(), failed=()):
        def detect(threads, **kwargs):
            calls.append((threads, kwargs))
            return [dict(e) for e in events], set(failed)
        monkeypatch.setattr(detector, "_detect_plans_claude", detect)
        return calls

    return install


@pytest.fixture(autouse=True)
def _reset_stats():
    jev_detector.reset_stats()


# --- fast path -------------------------------------------------------------


def test_fast_path_assembles_event_without_calling_haiku(fake_jev, fake_haiku):
    fake_jev(_resp())
    haiku = fake_haiku()
    events, failed = jev_detector.detect_plans([_thread(chat_id=42)])
    assert haiku == []
    assert failed == set()
    assert len(events) == 1
    e = events[0]
    assert e["date"] == "2026-10-03"
    assert e["time_start"] == "19:00"
    assert e["status"] == "confirmed"
    assert e["user_is_participant"] is True
    assert e["title"] == "Dinner"
    assert e["chat_id"] == 42
    assert e["_backend"] == "jev"
    # Evidence is the verbatim message the date came from, so the downstream
    # evidence checks hold for fast-path events too.
    assert e["evidence"] == "dinner saturday at 7pm?"
    assert jev_detector.STATS["fast"] == 1


def test_fast_path_event_has_every_field_the_claude_detector_emits(fake_jev, fake_haiku):
    fake_jev(_resp())
    fake_haiku()
    events, _ = jev_detector.detect_plans([_thread()])
    required = set(detector.EVENT_ITEM_SCHEMA["required"]) | {"chat_id", "_new_msg_from_user"}
    assert required <= set(events[0])


def test_no_time_answer_means_all_day(fake_jev, fake_haiku):
    fake_jev(_resp(time=_choice("none")))
    fake_haiku()
    events, _ = jev_detector.detect_plans([_thread()])
    assert events[0]["time_start"] is None
    assert events[0]["time_confidence"] is None


def test_low_confidence_location_is_dropped_not_a_fallback(fake_jev, fake_haiku):
    fake_jev(_resp(location=_choice("Nopa", 0.4)))
    haiku = fake_haiku()
    events, _ = jev_detector.detect_plans([_thread()])
    assert haiku == []
    assert events[0]["location"] is None


def test_named_contact_gets_with_in_title(fake_jev, fake_haiku):
    fake_jev(_resp())
    fake_haiku()
    events, _ = jev_detector.detect_plans([_thread(participants=("Sarah Chen",))])
    assert events[0]["title"] == "Dinner with Sarah"


def test_group_thread_where_user_is_silent_is_demoted(fake_jev, fake_haiku):
    thread = _thread(participants=("a", "b"), messages=[
        {"sender": "a", "text": "dinner saturday at 7pm?", "from_me": False, "unix_ts": WED},
        {"sender": "b", "text": "yes!", "from_me": False, "unix_ts": WED + 60},
    ])
    fake_jev(_resp())
    fake_haiku()
    events, _ = jev_detector.detect_plans([thread])
    assert events[0]["status"] == "unanswered"


# --- skip path ---------------------------------------------------------------


@pytest.mark.parametrize("overrides", [
    {"has_plan": 0.05},
    {"status": _choice("declined", 0.99)},
    {"status": _choice("no_plan", 0.9)},
])
def test_confident_no_plan_skips_without_any_llm_call(fake_jev, fake_haiku, overrides):
    fake_jev(_resp(**overrides))
    haiku = fake_haiku()
    events, failed = jev_detector.detect_plans([_thread()])
    assert events == [] and failed == set()
    assert haiku == []
    assert jev_detector.STATS["skip"] == 1


def test_context_thread_with_no_new_info_skips(fake_jev, fake_haiku):
    thread = _thread()
    thread["messages"][0]["is_context"] = True
    calls = fake_jev(_resp(new_info=0.05))
    fake_haiku()
    events, _ = jev_detector.detect_plans([thread])
    assert events == []
    assert "new_info" in calls[0][1]
    assert calls[0][0]["messages"][0]["already_processed"] is True


def test_new_info_question_only_asked_when_thread_has_context(fake_jev, fake_haiku):
    calls = fake_jev(_resp())
    fake_haiku()
    jev_detector.detect_plans([_thread()])
    assert "new_info" not in calls[0][1]


# --- fallback path -----------------------------------------------------------


@pytest.mark.parametrize("overrides", [
    {"has_plan": 0.6},                          # plausible but not sure
    {"multiple_plans": 0.8},
    {"status": _choice("cancelled", 0.99)},     # destructive: Haiku decides
    {"status": _choice("confirmed", 0.5)},
    {"participant": 0.6},
    {"date": _choice("none", 0.9)},
    {"date": _choice("2026-10-03", 0.5)},
    {"date": None},                             # no date candidates at all
    {"time": _choice("19:00", 0.5)},
    {"activity": None},
])
def test_uncertain_or_out_of_scope_falls_back_to_haiku(fake_jev, fake_haiku, overrides):
    fake_jev(_resp(**overrides))
    haiku = fake_haiku(events=[{"title": "Dinner", "date": "2026-10-03"}])
    events, _ = jev_detector.detect_plans([_thread(chat_id=7)])
    assert len(haiku) == 1
    assert haiku[0][0][0]["chat_id"] == 7
    assert events == [{"title": "Dinner", "date": "2026-10-03", "_backend": "jev+haiku"}]
    assert jev_detector.STATS["fallback"] == 1


def test_multi_day_language_falls_back(fake_jev, fake_haiku):
    thread = _thread(messages=[
        {"sender": "x", "text": "cabin trip saturday?", "from_me": False, "unix_ts": WED},
        {"sender": "me", "text": "yes!", "from_me": True, "unix_ts": WED},
    ])
    fake_jev(_resp())
    haiku = fake_haiku()
    jev_detector.detect_plans([thread])
    assert len(haiku) == 1


def test_fallback_forwards_haiku_failures(fake_jev, fake_haiku):
    fake_jev(_resp(has_plan=0.6))
    fake_haiku(failed=[1])
    _, failed = jev_detector.detect_plans([_thread(chat_id=1)])
    assert failed == {1}


def test_jev_error_degrades_to_haiku(fake_jev, fake_haiku):
    fake_jev(RuntimeError("boom"))
    haiku = fake_haiku(events=[{"title": "Dinner"}])
    events, failed = jev_detector.detect_plans([_thread()])
    assert len(haiku) == 1
    assert events[0]["_backend"] == "jev+haiku"
    assert failed == set()
    assert jev_detector.STATS["jev_errors"] == 1


def test_threshold_overrides_apply(fake_jev, fake_haiku):
    fake_jev(_resp(has_plan=0.6))
    haiku = fake_haiku()
    events, _ = jev_detector.detect_plans([_thread()], thresholds={"fast_plan_min": 0.5})
    assert haiku == []
    assert len(events) == 1


# --- questions and dispatch ---------------------------------------------------


def test_slot_questions_offer_candidates_plus_none(fake_jev, fake_haiku):
    calls = fake_jev(_resp())
    fake_haiku()
    jev_detector.detect_plans([_thread()])
    questions = calls[0][1]
    assert set(questions["date"].criteria) == {"2026-10-03", "none"}
    assert set(questions["time"].criteria) == {"19:00", "none"}
    assert "location" not in questions  # no location candidates in the thread


def test_detect_plans_dispatches_on_backend(fake_jev, fake_haiku):
    fake_jev(_resp())
    haiku = fake_haiku()
    events, _ = detector.detect_plans([_thread()], backend="jev")
    assert events[0]["_backend"] == "jev"
    assert haiku == []


def test_detect_plans_rejects_unknown_backend():
    with pytest.raises(ValueError):
        detector.detect_plans([_thread()], backend="gpt")


# --- client ------------------------------------------------------------------


def test_client_records_usage_and_failures(monkeypatch):
    usage_tracker.reset()
    ok = SimpleNamespace(model="jev-1.13.0", usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=5))

    class Fake:
        def __init__(self, result):
            self.result = result

        def system_one(self, state, questions):
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    monkeypatch.setattr(jev_client, "_get_client", lambda: Fake(ok))
    jev_client.ask({}, {})
    monkeypatch.setattr(jev_client, "_get_client", lambda: Fake(RuntimeError("down")))
    with pytest.raises(RuntimeError):
        jev_client.ask({}, {})

    summary = usage_tracker.summary()
    assert summary["total_calls"] == 1
    assert summary["failed_calls"] == 1
    assert summary["total_cost_usd"] == pytest.approx(0.042)
    assert summary["unpriced_calls"] == 0


def test_change_of_plan_in_new_message_falls_back(fake_jev, fake_haiku):
    thread = _thread(messages=[
        {"sender": "x", "text": "dinner saturday at 7pm?", "from_me": False, "unix_ts": WED, "is_context": True},
        {"sender": "me", "text": "yes!", "from_me": True, "unix_ts": WED, "is_context": True},
        {"sender": "x", "text": "actually can we do 8 instead?", "from_me": False, "unix_ts": WED + 60},
    ])
    fake_jev(_resp(new_info=0.9))
    haiku = fake_haiku()
    jev_detector.detect_plans([thread])
    assert len(haiku) == 1


def test_change_language_only_in_context_does_not_force_fallback(fake_jev, fake_haiku):
    thread = _thread(messages=[
        {"sender": "x", "text": "lunch instead of dinner saturday at 7pm?", "from_me": False, "unix_ts": WED, "is_context": True},
        {"sender": "me", "text": "yes!", "from_me": True, "unix_ts": WED + 60},
    ])
    fake_jev(_resp(new_info=0.9))
    haiku = fake_haiku()
    jev_detector.detect_plans([thread])
    assert haiku == []


def _context_thread(new_text, from_me=False):
    return _thread(messages=[
        {"sender": "x", "text": "dinner saturday at 7pm?", "from_me": False, "unix_ts": WED, "is_context": True},
        {"sender": "me", "text": "yes!", "from_me": True, "unix_ts": WED, "is_context": True},
        {"sender": "me" if from_me else "x", "text": new_text, "from_me": from_me, "unix_ts": WED + 60},
    ])


@pytest.mark.parametrize("text", [
    "ugh actually can't make it, sorry",
    "can’t do sat 😕",                       # curly apostrophe, as iMessage sends it
    "nvm, it's off",
    "something came up, gonna have to bail",
    "rain check?",
])
def test_cancel_language_in_new_message_is_never_skipped(fake_jev, fake_haiku, text):
    # Even when Jev says there's no plan here: a skip is the one path with no
    # second look, so a possible cancellation always reaches Haiku.
    fake_jev(_resp(has_plan=0.05, new_info=0.05))
    haiku = fake_haiku()
    jev_detector.detect_plans([_context_thread(text)])
    assert len(haiku) == 1
    assert jev_detector.STATS["fallback"] == 1


def test_cancel_language_overrides_a_confident_confirmed_answer(fake_jev, fake_haiku):
    # Observed: Jev called a cancellation buried in chatter "confirmed".
    fake_jev(_resp(new_info=0.9, status=_choice("confirmed", 0.95)))
    haiku = fake_haiku()
    jev_detector.detect_plans([_context_thread("oh also can't do tmrw")])
    assert len(haiku) == 1


def test_ordinary_new_message_can_still_be_skipped(fake_jev, fake_haiku):
    fake_jev(_resp(has_plan=0.9, new_info=0.05))
    haiku = fake_haiku()
    jev_detector.detect_plans([_context_thread("so excited!!")])
    assert haiku == []
    assert jev_detector.STATS["skip"] == 1


def test_cant_wait_is_not_cancel_language():
    assert not jev_detector._CHANGE_RE.search("can't wait!!")

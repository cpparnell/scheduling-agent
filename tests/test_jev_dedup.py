from types import SimpleNamespace

import pytest

from scheduling_agent import config, dedup, jev_client, jev_dedup, reconcile


def _choice(probabilities):
    label = max(probabilities, key=probabilities.get)
    return SimpleNamespace(choice=label, confidence=probabilities[label], probabilities=probabilities)


def _resp(same_as, relationship):
    return SimpleNamespace(
        nouls={},
        choices={"same_as": _choice(same_as), "relationship": _choice(relationship)},
    )


@pytest.fixture
def fake_jev(monkeypatch):
    calls = []

    def install(*responses):
        queue = list(responses)

        def ask(state, questions):
            calls.append((state, questions))
            r = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(r, Exception):
                raise r
            return r
        monkeypatch.setattr(jev_client, "ask", ask)
        return calls

    return install


EVENT = {"title": "Dinner", "date": "2026-10-03", "chat_id": 1, "evidence": "dinner sat?"}
CANDS = [
    {"title": "Lunch", "date": "2026-10-03", "hash": "a"},
    {"title": "Dinner w/ Sam", "date": "2026-10-03", "hash": "b"},
]


def test_confident_match_is_duplicate_of_the_most_likely_candidate(fake_jev):
    calls = fake_jev(_resp({"event_0": 0.05, "event_1": 0.9, "new": 0.05}, {"duplicate": 0.9, "reschedule": 0.1}))
    v = jev_dedup.adjudicate_once(EVENT, CANDS)
    assert v["is_duplicate"] is True
    assert v["duplicate_of"] == 1
    assert v["relationship"] == "duplicate"
    assert set(calls[0][1]["same_as"].criteria) == {"event_0", "event_1", "new"}


def test_confident_new_is_not_duplicate(fake_jev):
    fake_jev(_resp({"event_0": 0.1, "event_1": 0.1, "new": 0.8}, {"duplicate": 0.9, "reschedule": 0.1}))
    v = jev_dedup.adjudicate_once(EVENT, CANDS)
    assert v["is_duplicate"] is False
    assert v["duplicate_of"] is None


def test_uncertain_new_biases_toward_same(fake_jev):
    # P(new) = 0.5 isn't enough to create; a missed duplicate spams the calendar.
    fake_jev(_resp({"event_0": 0.2, "event_1": 0.3, "new": 0.5}, {"duplicate": 0.9, "reschedule": 0.1}))
    v = jev_dedup.adjudicate_once(EVENT, CANDS)
    assert v["is_duplicate"] is True
    assert v["duplicate_of"] == 1


def test_low_confidence_reschedule_defaults_to_duplicate(fake_jev):
    fake_jev(_resp({"event_0": 0.9, "event_1": 0.05, "new": 0.05}, {"reschedule": 0.5, "duplicate": 0.4, "new_occurrence": 0.1}))
    assert jev_dedup.adjudicate_once(EVENT, CANDS)["relationship"] == "duplicate"


def test_confident_reschedule_is_kept(fake_jev):
    fake_jev(_resp({"event_0": 0.9, "event_1": 0.05, "new": 0.05}, {"reschedule": 0.85, "duplicate": 0.15}))
    assert jev_dedup.adjudicate_once(EVENT, CANDS)["relationship"] == "reschedule"


def test_dedup_adjudicate_routes_to_jev_and_retries_once(fake_jev):
    good = _resp({"event_0": 0.9, "event_1": 0.05, "new": 0.05}, {"duplicate": 0.9, "reschedule": 0.1})
    calls = fake_jev(RuntimeError("blip"), good)
    v = dedup.adjudicate(EVENT, CANDS, model="unused", backend="jev")
    assert len(calls) == 2
    assert v["is_duplicate"] is True and v["duplicate_of"] == 0


def test_dedup_adjudicate_jev_returns_none_after_two_failures(fake_jev):
    fake_jev(RuntimeError("down"))
    assert dedup.adjudicate(EVENT, CANDS, model="unused", backend="jev") is None


def test_dedup_adjudicate_rejects_unknown_backend():
    with pytest.raises(ValueError):
        dedup.adjudicate(EVENT, CANDS, model="unused", backend="gpt")


def test_reconcile_passes_configured_dedup_backend(monkeypatch):
    seen = {}

    def fake_adjudicate(event, candidates, model, source=None, backend="claude"):
        seen["backend"] = backend
        return {"is_duplicate": False}

    monkeypatch.setattr(dedup, "adjudicate", fake_adjudicate)
    cfg = {**config.DEFAULTS, "dedup_backend": "jev"}
    decision = reconcile._adjudicate(EVENT, CANDS, cfg)
    assert seen["backend"] == "jev"
    assert decision.action == "create"

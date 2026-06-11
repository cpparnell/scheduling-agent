from scheduling_agent import state


def test_event_hash_normalizes_case_and_whitespace():
    a = state.event_hash(1, "2026-06-13", "Dinner With Sam")
    b = state.event_hash(1, "2026-06-13", "  dinner with sam  ")
    assert a == b


def test_event_hash_distinct_on_chat_date_title():
    base = state.event_hash(1, "2026-06-13", "Dinner")
    assert state.event_hash(2, "2026-06-13", "Dinner") != base
    assert state.event_hash(1, "2026-06-14", "Dinner") != base
    assert state.event_hash(1, "2026-06-13", "Lunch") != base


def test_record_event_then_is_duplicate():
    assert state.is_duplicate(1, "2026-06-13", "Dinner") is False
    state.record_event(1, "2026-06-13", "Dinner")
    assert state.is_duplicate(1, "2026-06-13", "Dinner") is True
    # Normalized variant is also a duplicate.
    assert state.is_duplicate(1, "2026-06-13", "  DINNER ") is True


def test_record_event_does_not_touch_timestamp():
    state.update_timestamp(500)
    state.record_event(1, "2026-06-13", "Dinner")
    assert state.get_last_timestamp() == 500


def test_update_timestamp_is_monotonic():
    state.update_timestamp(1000)
    assert state.get_last_timestamp() == 1000
    state.update_timestamp(500)  # older, ignored
    assert state.get_last_timestamp() == 1000
    state.update_timestamp(2000)
    assert state.get_last_timestamp() == 2000


def test_fresh_state_defaults():
    assert state.get_last_timestamp() is None
    assert state.is_duplicate(1, "2026-06-13", "Anything") is False

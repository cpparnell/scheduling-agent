from scheduling_agent import state


def test_event_hash_normalizes_case_and_whitespace():
    # title-based fallback (time_start=None) still normalizes case/whitespace
    a = state.event_hash(1, "2026-06-13", None, "Dinner With Sam")
    b = state.event_hash(1, "2026-06-13", None, "  dinner with sam  ")
    assert a == b


def test_event_hash_distinct_on_chat_date_title():
    base = state.event_hash(1, "2026-06-13", None, "Dinner")
    assert state.event_hash(2, "2026-06-13", None, "Dinner") != base
    assert state.event_hash(1, "2026-06-14", None, "Dinner") != base
    assert state.event_hash(1, "2026-06-13", None, "Lunch") != base


def test_event_hash_timed_collapses_different_titles():
    # Same chat + date + time_start → same hash regardless of title (the dedup fix)
    h = state.event_hash(1, "2026-06-13", "17:30", "Pizza at Dicey's")
    assert state.event_hash(1, "2026-06-13", "17:30", "Drinks") == h


def test_event_hash_timed_distinct_on_time():
    h = state.event_hash(1, "2026-06-13", "17:30", "Dinner")
    assert state.event_hash(1, "2026-06-13", "20:00", "Dinner") != h


def test_event_hash_timed_distinct_from_untimed():
    h_timed = state.event_hash(1, "2026-06-13", "17:30", "Dinner")
    h_untimed = state.event_hash(1, "2026-06-13", None, "Dinner")
    assert h_timed != h_untimed


def test_record_event_then_is_duplicate():
    assert state.is_duplicate(1, "2026-06-13", None, "Dinner") is False
    state.record_event(1, "2026-06-13", None, "Dinner")
    assert state.is_duplicate(1, "2026-06-13", None, "Dinner") is True
    # Normalized variant is also a duplicate.
    assert state.is_duplicate(1, "2026-06-13", None, "  DINNER ") is True


def test_record_event_timed_dedup():
    # Recording "Pizza at Dicey's" at 17:30 should block "Drinks" at the same time.
    state.record_event(1, "2026-06-14", "17:30", "Pizza at Dicey's")
    assert state.is_duplicate(1, "2026-06-14", "17:30", "Drinks") is True
    # Different time is not a duplicate.
    assert state.is_duplicate(1, "2026-06-14", "20:00", "Drinks") is False


def test_record_event_does_not_touch_timestamp():
    state.update_timestamp(500)
    state.record_event(1, "2026-06-13", None, "Dinner")
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
    assert state.is_duplicate(1, "2026-06-13", None, "Anything") is False

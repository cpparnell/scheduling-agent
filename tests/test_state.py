import json

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


def test_fresh_state_file_is_stamped_with_current_version():
    # Writing anything materializes the file at the current schema version.
    state.update_timestamp(123)
    data = json.loads(state.STATE_FILE.read_text())
    assert data["schema_version"] == state.CURRENT_SCHEMA_VERSION


def test_pre_versioning_state_migrates_and_persists_on_load():
    # Simulate a v0 file (no schema_version) written by an older build.
    state.STATE_DIR.mkdir(exist_ok=True)
    legacy = {"last_processed_timestamp": 999, "created_events": ["abc123"]}
    state.STATE_FILE.write_text(json.dumps(legacy))

    # Reading it migrates in memory and preserves existing data.
    assert state.get_last_timestamp() == 999
    assert "abc123" in state._load()["created_events"]

    # The upgrade is persisted to disk so later reads are clean.
    on_disk = json.loads(state.STATE_FILE.read_text())
    assert on_disk["schema_version"] == state.CURRENT_SCHEMA_VERSION
    assert on_disk["last_processed_timestamp"] == 999
    assert on_disk["created_events"] == ["abc123"]


def test_normalize_title_strips_month_names():
    # Month prefix stripped so "July Munch at Sinha" == "Munch at Sinha"
    assert state._normalize_title("July Munch at Sinha") == state._normalize_title("Munch at Sinha")
    assert state._normalize_title("August Munch at Sinha") == state._normalize_title("Munch at Sinha")


def test_title_dedup_blocks_same_title_within_window():
    state.record_event(300, "2026-07-09", "14:00", "July Munch at Sinha")
    # Different date but within 28 days, title normalizes to the same key → duplicate
    assert state.is_duplicate(300, "2026-07-16", "14:00", "Munch at Sinha") is True


def test_title_dedup_allows_same_title_outside_window():
    state.record_event(300, "2026-07-09", "14:00", "July Munch at Sinha")
    # 35 days later → outside the 28-day window → new occurrence allowed
    assert state.is_duplicate(300, "2026-08-13", "14:00", "August Munch at Sinha") is False


def test_title_dedup_isolated_by_chat():
    state.record_event(300, "2026-07-09", "14:00", "Munch at Sinha")
    # Same title and date range but different chat → not a duplicate
    assert state.is_duplicate(999, "2026-07-16", "14:00", "Munch at Sinha") is False


def test_title_dedup_matches_across_month_prefix_variants():
    state.record_event(300, "2026-07-09", "14:00", "July Munch at Sinha")
    # "August Munch" strips to same key as "July Munch"; close date → blocked
    assert state.is_duplicate(300, "2026-07-16", "14:00", "August Munch at Sinha") is True


def test_migrate_is_noop_for_current_version():
    data = {
        "schema_version": state.CURRENT_SCHEMA_VERSION,
        "last_processed_timestamp": 5,
        "created_events": ["x"],
    }
    assert state._migrate(dict(data)) == data

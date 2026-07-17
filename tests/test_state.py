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


def test_v2_to_v3_migration_preserves_existing_data_and_adds_events():
    legacy = {
        "schema_version": 2,
        "last_processed_timestamp": 999,
        "created_events": ["abc123"],
        "title_events": {"1:dinner": "2026-06-01"},
    }
    migrated = state._migrate(dict(legacy))
    assert migrated["schema_version"] == state.CURRENT_SCHEMA_VERSION
    assert migrated["last_processed_timestamp"] == 999
    assert migrated["created_events"] == ["abc123"]
    assert migrated["title_events"] == {"1:dinner": "2026-06-01"}
    assert migrated["events"] == []
    assert migrated["watermark_hold"] == {"ts": None, "count": 0}


def test_v0_to_v3_migration_chain():
    legacy = {"last_processed_timestamp": 42, "created_events": ["x"]}
    migrated = state._migrate(dict(legacy))
    assert migrated["schema_version"] == state.CURRENT_SCHEMA_VERSION
    assert migrated["last_processed_timestamp"] == 42
    assert migrated["created_events"] == ["x"]
    assert migrated["title_events"] == {}
    assert migrated["events"] == []
    assert migrated["watermark_hold"] == {"ts": None, "count": 0}


def test_record_event_stores_descriptive_record():
    state.record_event(
        1, "2026-06-13", "19:00", "Dinner with Sam",
        location="Dicey's", status="confirmed",
        evidence="dinner at 7?", calendar_uid="ABC-123",
    )
    events = state._load()["events"]
    assert len(events) == 1
    record = events[0]
    assert record["chat_id"] == 1
    assert record["date"] == "2026-06-13"
    assert record["time_start"] == "19:00"
    assert record["title"] == "Dinner with Sam"
    assert record["location"] == "Dicey's"
    assert record["status"] == "confirmed"
    assert record["evidence"] == "dinner at 7?"
    assert record["calendar_uid"] == "ABC-123"
    assert record["suppressed"] is False
    assert "created_at" in record


def test_get_events_near_same_day():
    state.record_event(1, "2026-06-13", "19:00", "Dinner")
    matches = state.get_events_near("2026-06-13", window_days=1)
    assert len(matches) == 1
    assert matches[0]["title"] == "Dinner"


def test_get_events_near_within_window():
    state.record_event(1, "2026-06-12", "19:00", "Dinner")
    state.record_event(1, "2026-06-14", "19:00", "Lunch")
    matches = state.get_events_near("2026-06-13", window_days=1)
    titles = {m["title"] for m in matches}
    assert titles == {"Dinner", "Lunch"}


def test_get_events_near_outside_window_excluded():
    state.record_event(1, "2026-06-01", "19:00", "Dinner")
    matches = state.get_events_near("2026-06-13", window_days=1)
    assert matches == []


def test_get_events_near_excludes_suppressed():
    state.record_event(1, "2026-06-13", "19:00", "Dinner", suppressed=True)
    matches = state.get_events_near("2026-06-13", window_days=1)
    assert matches == []


def test_suppressed_record_still_trips_is_duplicate():
    state.record_event(1, "2026-06-13", "19:00", "Dinner", suppressed=True)
    assert state.is_duplicate(1, "2026-06-13", "19:00", "Dinner") is True


def test_watermark_hold_default_and_round_trip():
    assert state.get_watermark_hold() == {"ts": None, "count": 0}
    state.set_watermark_hold(1000, 2)
    assert state.get_watermark_hold() == {"ts": 1000, "count": 2}


# --- v4: canonical store + write-ahead journal -------------------------------


def test_v3_to_v4_migration_upgrades_records_and_adds_journal():
    legacy = {
        "schema_version": 3,
        "last_processed_timestamp": 999,
        "created_events": ["abc123"],
        "title_events": {"1:dinner": "2026-06-01"},
        "events": [{
            "hash": "abc123",
            "chat_id": 1,
            "date": "2026-06-01",
            "time_start": "19:00",
            "title": "Dinner",
            "created_at": "2026-06-01T10:00:00",
            "suppressed": False,
        }],
        "watermark_hold": {"ts": None, "count": 0},
    }
    migrated = state._migrate(dict(legacy))
    assert migrated["schema_version"] == state.CURRENT_SCHEMA_VERSION
    assert migrated["journal"] == []
    record = migrated["events"][0]
    assert record["chat_id"] == 1
    assert record["chat_ids"] == [1]
    assert record["confidence"] is None
    assert record["updated_at"] == "2026-06-01T10:00:00"
    assert record["revisions"] == []
    assert len(record["canonical_id"]) == 32


def test_v0_to_v4_migration_chain():
    legacy = {"last_processed_timestamp": 42, "created_events": ["x"]}
    migrated = state._migrate(dict(legacy))
    assert migrated["schema_version"] == state.CURRENT_SCHEMA_VERSION
    assert migrated["created_events"] == ["x"]
    assert migrated["events"] == []
    assert migrated["journal"] == []


def test_pending_journal_entry_trips_is_duplicate():
    record = state.make_record(1, "2026-06-13", "19:00", "Dinner")
    state.journal_intent(record)
    # Exact hash match against the pending record.
    assert state.is_duplicate(1, "2026-06-13", "19:00", "Dinner") is True
    # Title-window match against the pending record.
    assert state.is_duplicate(1, "2026-06-20", None, "Dinner") is True
    # Unrelated event is not blocked.
    assert state.is_duplicate(2, "2026-06-13", "19:00", "Dinner") is False


def test_pending_journal_entry_appears_in_get_events_near():
    record = state.make_record(1, "2026-06-13", "19:00", "Dinner")
    state.journal_intent(record)
    matches = state.get_events_near("2026-06-13", window_days=1)
    assert [m["title"] for m in matches] == ["Dinner"]


def test_journal_commit_lands_record_and_clears_journal():
    record = state.make_record(1, "2026-06-13", "19:00", "Dinner")
    jid = state.journal_intent(record)
    state.journal_commit(jid, calendar_uid="UID-1")

    assert state.get_pending_journal() == []
    data = state._load()
    assert len(data["events"]) == 1
    assert data["events"][0]["calendar_uid"] == "UID-1"
    assert record["hash"] in data["created_events"]
    assert state.is_duplicate(1, "2026-06-13", "19:00", "Dinner") is True


def test_journal_drop_removes_entry_without_committing():
    record = state.make_record(1, "2026-06-13", "19:00", "Dinner")
    jid = state.journal_intent(record)
    state.journal_drop(jid)

    assert state.get_pending_journal() == []
    assert state._load()["events"] == []
    assert state.is_duplicate(1, "2026-06-13", "19:00", "Dinner") is False


def test_journal_commit_unknown_id_is_noop():
    state.journal_commit("nope", calendar_uid="UID-1")
    assert state._load()["events"] == []


def test_update_record_applies_changes_and_keeps_old_hash():
    state.record_event(1, "2026-06-13", "19:00", "Dinner", calendar_uid="UID-1")
    record = state._load()["events"][0]

    ok = state.update_record(
        record["canonical_id"],
        {"time_start": "20:00"},
        reason="rescheduled in chat",
        chat_id=2,
    )
    assert ok is True

    updated = state._load()["events"][0]
    assert updated["time_start"] == "20:00"
    assert updated["chat_ids"] == [1, 2]
    assert len(updated["revisions"]) == 1
    assert updated["revisions"][0]["changed"] == {"time_start": ["19:00", "20:00"]}
    assert updated["revisions"][0]["reason"] == "rescheduled in chat"
    # Both the old and new wording/time stay deduplicated.
    assert state.is_duplicate(1, "2026-06-13", "19:00", "Dinner") is True
    assert state.is_duplicate(1, "2026-06-13", "20:00", "Dinner") is True


def test_update_record_no_material_change_records_no_revision():
    state.record_event(1, "2026-06-13", "19:00", "Dinner")
    record = state._load()["events"][0]
    state.update_record(record["canonical_id"], {"time_start": "19:00"})
    assert state._load()["events"][0]["revisions"] == []


def test_update_record_unknown_id_returns_false():
    assert state.update_record("missing", {"time_start": "20:00"}) is False

import time

import pytest

from scheduling_agent import reader
from tests.fixtures import chatdb


def _recent(hours_ago: float) -> float:
    return time.time() - hours_ago * 3600


def test_text_column_message_round_trips(fake_chat_db):
    fake_chat_db([
        {
            "participants": ["+15551234567"],
            "messages": [
                {"text": "hey are we still on?", "from_me": False, "unix_ts": _recent(2)},
                {"text": "yes! see you at 7", "from_me": True, "unix_ts": _recent(1)},
            ],
        }
    ])

    threads = reader.get_threads_since(None, lookback_days=7, blocked=[])

    assert len(threads) == 1
    t = threads[0]
    assert t["participants"] == ["+15551234567"]
    assert [m["text"] for m in t["messages"]] == ["hey are we still on?", "yes! see you at 7"]
    assert [m["from_me"] for m in t["messages"]] == [False, True]
    # Sent message resolves to 'me' (handle_id 0 -> COALESCE), received to the handle id.
    assert t["messages"][0]["sender"] == "+15551234567"
    assert t["messages"][1]["sender"] == "me"


def test_attributed_body_only_message_short_payload(fake_chat_db):
    fake_chat_db([
        {
            "participants": ["+15551234567"],
            "messages": [
                {"attributed": "sent via attributedBody", "from_me": True, "unix_ts": _recent(1)},
            ],
        }
    ])

    threads = reader.get_threads_since(None, lookback_days=7, blocked=[])

    assert threads[0]["messages"][0]["text"] == "sent via attributedBody"


def test_attributed_body_long_payload_and_emoji(fake_chat_db):
    long_text = "dinner 🍕 " + "x" * 300
    fake_chat_db([
        {
            "participants": ["+15551234567"],
            "messages": [
                {"attributed": long_text, "from_me": True, "unix_ts": _recent(1)},
            ],
        }
    ])

    threads = reader.get_threads_since(None, lookback_days=7, blocked=[])

    assert threads[0]["messages"][0]["text"] == long_text


def test_garbage_blob_message_is_skipped_not_crash(fake_chat_db):
    fake_chat_db([
        {
            "participants": ["+15551234567"],
            "messages": [
                {"raw_attributed": b"\x00\x01 no NSString here \x02", "from_me": True, "unix_ts": _recent(2)},
                {"text": "real message", "from_me": False, "unix_ts": _recent(1)},
            ],
        }
    ])

    threads = reader.get_threads_since(None, lookback_days=7, blocked=[])

    # Garbage-only message dropped; the real one survives.
    assert len(threads) == 1
    assert [m["text"] for m in threads[0]["messages"]] == ["real message"]


def test_last_apple_ts_cutoff_excludes_older(fake_chat_db):
    old_ts = _recent(48)
    new_ts = _recent(1)
    fake_chat_db([
        {
            "participants": ["+15551234567"],
            "messages": [
                {"text": "old", "from_me": False, "unix_ts": old_ts},
                {"text": "new", "from_me": False, "unix_ts": new_ts},
            ],
        }
    ])

    cutoff = reader.unix_to_apple(_recent(24))
    threads = reader.get_threads_since(cutoff, lookback_days=7, blocked=[])

    assert len(threads) == 1
    # The new message must be present; the old message may also appear as
    # context (prepended by _prepend_context), but "new" must be last.
    msgs = [m["text"] for m in threads[0]["messages"]]
    assert "new" in msgs
    assert msgs.index("new") > msgs.index("old")


def test_lookback_days_fallback_when_no_timestamp(fake_chat_db):
    fake_chat_db([
        {
            "participants": ["+15551234567"],
            "messages": [
                {"text": "ancient", "from_me": False, "unix_ts": _recent(24 * 30)},  # 30 days ago
                {"text": "fresh", "from_me": False, "unix_ts": _recent(1)},
            ],
        }
    ])

    threads = reader.get_threads_since(None, lookback_days=7, blocked=[])

    assert [m["text"] for m in threads[0]["messages"]] == ["fresh"]


def test_blocked_contact_thread_excluded(fake_chat_db):
    fake_chat_db([
        {
            "participants": ["+15550000000"],
            "messages": [{"text": "from blocked", "from_me": False, "unix_ts": _recent(1)}],
        },
        {
            "participants": ["+15551234567"],
            "messages": [{"text": "from allowed", "from_me": False, "unix_ts": _recent(1)}],
        },
    ])

    threads = reader.get_threads_since(None, lookback_days=7, blocked=["+15550000000"])

    assert len(threads) == 1
    assert threads[0]["participants"] == ["+15551234567"]


def test_multi_chat_grouping_and_latest_ts(fake_chat_db):
    a1, a2, b1 = _recent(3), _recent(1), _recent(2)
    fake_chat_db([
        {
            "participants": ["+15551111111"],
            "messages": [
                {"text": "a1", "from_me": False, "unix_ts": a1},
                {"text": "a2", "from_me": True, "unix_ts": a2},
            ],
        },
        {
            "participants": ["+15552222222"],
            "messages": [
                {"text": "b1", "from_me": False, "unix_ts": b1},
            ],
        },
    ])

    threads = reader.get_threads_since(None, lookback_days=7, blocked=[])

    assert len(threads) == 2
    by_part = {t["participants"][0]: t for t in threads}
    a = by_part["+15551111111"]
    # Messages ordered ascending by date.
    assert [m["text"] for m in a["messages"]] == ["a1", "a2"]
    # latest_apple_ts is the newest message's stored apple timestamp.
    assert a["latest_apple_ts"] == reader.unix_to_apple(a2)


@pytest.mark.parametrize("unix_ts", [0.0, 978307200.0, 1700000000.5, time.time()])
def test_apple_unix_round_trip(unix_ts):
    apple = reader.unix_to_apple(unix_ts)
    assert reader.apple_to_unix(apple) == pytest.approx(unix_ts, abs=1e-3)


def test_tapback_null_text_synthesizes_label(fake_chat_db):
    fake_chat_db([
        {
            "participants": ["+15551234567"],
            "messages": [
                {"text": "dinner friday at 7?", "from_me": True, "unix_ts": _recent(2)},
                {"tapback": 2000, "from_me": False, "unix_ts": _recent(1)},
            ],
        }
    ])

    threads = reader.get_threads_since(None, lookback_days=7, blocked=[])

    assert len(threads) == 1
    texts = [m["text"] for m in threads[0]["messages"]]
    assert texts[0] == "dinner friday at 7?"
    assert "Loved" in texts[1]


def test_tapback_disliked_synthesizes_label(fake_chat_db):
    fake_chat_db([
        {
            "participants": ["+15551234567"],
            "messages": [
                {"tapback": 2002, "from_me": False, "unix_ts": _recent(1)},
            ],
        }
    ])

    threads = reader.get_threads_since(None, lookback_days=7, blocked=[])

    assert "Disliked" in threads[0]["messages"][0]["text"]


def test_tapback_with_existing_text_uses_text_column(fake_chat_db):
    fake_chat_db([
        {
            "participants": ["+15551234567"],
            "messages": [
                # Modern macOS writes "Loved '...'" directly into text column alongside the tapback type
                {"text": "Loved \"dinner friday at 7?\"", "tapback": 2000, "from_me": False, "unix_ts": _recent(1)},
            ],
        }
    ])

    threads = reader.get_threads_since(None, lookback_days=7, blocked=[])

    assert threads[0]["messages"][0]["text"] == 'Loved "dinner friday at 7?"'

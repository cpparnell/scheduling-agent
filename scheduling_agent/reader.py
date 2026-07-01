import sqlite3
import time
from pathlib import Path

CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"

# iMessage timestamps are nanoseconds since 2001-01-01 00:00:00 UTC
# Convert to/from Unix epoch (seconds since 1970-01-01)
APPLE_EPOCH_OFFSET = 978307200  # seconds between 1970-01-01 and 2001-01-01

# associated_message_type values for tapback reactions (add variants; remove = type + 1000)
TAPBACK_LABELS = {
    2000: "❤️ Loved your message",
    2001: "👍 Liked your message",
    2002: "👎 Disliked your message",
    2003: "😂 Laughed at your message",
    2004: "‼️ Emphasized your message",
    2005: "❓ Questioned your message",
}


def unix_to_apple(unix_ts: float) -> int:
    return int((unix_ts - APPLE_EPOCH_OFFSET) * 1e9)


def apple_to_unix(apple_ts: int) -> float:
    return apple_ts / 1e9 + APPLE_EPOCH_OFFSET


def _decode_attributed_body(data: bytes | None) -> str | None:
    """
    Extract the message text from the attributedBody typedstream BLOB.
    On modern macOS many messages (especially sent ones) have a NULL `text`
    column and store their content only here.
    """
    if not data:
        return None
    idx = data.find(b"NSString")
    if idx == -1:
        return None
    # The string payload follows a 0x2b ('+') token after the NSString class name
    idx = data.find(b"+", idx)
    if idx == -1:
        return None
    idx += 1
    if data[idx] == 0x81:
        # Lengths >= 128 are encoded as a 2-byte little-endian int after 0x81
        length = int.from_bytes(data[idx + 1:idx + 3], "little")
        idx += 3
    else:
        length = data[idx]
        idx += 1
    try:
        return data[idx:idx + length].decode("utf-8", errors="replace")
    except (IndexError, UnicodeDecodeError):
        return None


<<<<<<< HEAD
CONTEXT_WINDOW = 30  # prior messages to prepend per thread for context


=======
>>>>>>> origin
def get_threads_since(last_apple_ts: int | None, lookback_days: int, blocked: list[str]) -> list[dict]:
    if last_apple_ts is None:
        cutoff = unix_to_apple(time.time() - lookback_days * 86400)
    else:
        cutoff = last_apple_ts

    try:
        conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError as e:
        raise RuntimeError(f"Cannot open chat.db — ensure Full Disk Access is granted: {e}")

    try:
        cursor = conn.cursor()

        # Get all messages newer than cutoff, with participant info
        cursor.execute("""
            SELECT
                c.ROWID AS chat_id,
                m.ROWID AS msg_id,
                m.text,
                m.attributedBody,
                COALESCE(h.id, 'me') AS sender,
                m.is_from_me,
                m.date AS apple_ts,
                COALESCE(m.associated_message_type, 0) AS tapback_type
            FROM message m
            JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            JOIN chat c ON cmj.chat_id = c.ROWID
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.date > ?
<<<<<<< HEAD
              AND (
                (m.text IS NOT NULL AND m.text != '')
                OR m.attributedBody IS NOT NULL
                OR COALESCE(m.associated_message_type, 0) BETWEEN 2000 AND 2005
              )
=======
              AND ((m.text IS NOT NULL AND m.text != '') OR m.attributedBody IS NOT NULL)
>>>>>>> origin
            ORDER BY c.ROWID, m.date ASC
        """, (cutoff,))

        rows = cursor.fetchall()

        # Get all participants per chat
        cursor.execute("""
            SELECT chj.chat_id, h.id
            FROM chat_handle_join chj
            JOIN handle h ON chj.handle_id = h.ROWID
        """)
        participants_by_chat: dict[int, list[str]] = {}
        for chat_id, handle_id in cursor.fetchall():
            participants_by_chat.setdefault(chat_id, []).append(handle_id)

    finally:
        conn.close()

    # Group messages into threads, filter blocked contacts
    blocked_set = set(blocked)
    threads: dict[int, dict] = {}

<<<<<<< HEAD
    for chat_id, msg_id, text, attributed_body, sender, from_me, apple_ts, tapback_type in rows:
=======
    for chat_id, msg_id, text, attributed_body, sender, from_me, apple_ts in rows:
>>>>>>> origin
        participants = participants_by_chat.get(chat_id, [])
        if any(p in blocked_set for p in participants):
            continue

        if not text:
            text = _decode_attributed_body(attributed_body)
        if not text:
<<<<<<< HEAD
            text = TAPBACK_LABELS.get(tapback_type)
        if not text:
=======
>>>>>>> origin
            continue

        if chat_id not in threads:
            threads[chat_id] = {
                "chat_id": chat_id,
                "participants": participants,
                "messages": [],
                "latest_apple_ts": 0,
            }

        threads[chat_id]["messages"].append({
            "sender": sender,
            "text": text,
            "from_me": bool(from_me),
            "unix_ts": apple_to_unix(apple_ts),
        })
        if apple_ts > threads[chat_id]["latest_apple_ts"]:
            threads[chat_id]["latest_apple_ts"] = apple_ts

    # When only a slice of a thread's history was fetched (incremental poll),
    # prepend prior messages as context so the LLM can see what was actually
    # confirmed rather than inferring from a fragment.
    if threads and last_apple_ts is not None:
        _prepend_context(threads, cutoff, blocked_set, participants_by_chat)

    return list(threads.values())


def _prepend_context(
    threads: dict[int, dict],
    cutoff: int,
    blocked_set: set[str],
    participants_by_chat: dict[int, list[str]],
) -> None:
    """Fetch up to CONTEXT_WINDOW prior messages per thread and prepend them."""
    try:
        conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError:
        return

    try:
        cursor = conn.cursor()
        chat_ids = list(threads.keys())
        placeholders = ",".join("?" * len(chat_ids))
        cursor.execute(f"""
            SELECT
                c.ROWID AS chat_id,
                m.text,
                m.attributedBody,
                COALESCE(h.id, 'me') AS sender,
                m.is_from_me,
                m.date AS apple_ts,
                COALESCE(m.associated_message_type, 0) AS tapback_type
            FROM message m
            JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            JOIN chat c ON cmj.chat_id = c.ROWID
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE c.ROWID IN ({placeholders})
              AND m.date <= ?
              AND (
                (m.text IS NOT NULL AND m.text != '')
                OR m.attributedBody IS NOT NULL
                OR COALESCE(m.associated_message_type, 0) BETWEEN 2000 AND 2005
              )
            ORDER BY c.ROWID, m.date DESC
        """, (*chat_ids, cutoff))
        ctx_rows = cursor.fetchall()
    finally:
        conn.close()

    # Take the most recent CONTEXT_WINDOW messages per chat (rows are DESC)
    counts: dict[int, int] = {}
    context_by_chat: dict[int, list[dict]] = {}
    for chat_id, text, attributed_body, sender, from_me, apple_ts, tapback_type in ctx_rows:
        if any(p in blocked_set for p in participants_by_chat.get(chat_id, [])):
            continue
        if counts.get(chat_id, 0) >= CONTEXT_WINDOW:
            continue
        if not text:
            text = _decode_attributed_body(attributed_body)
        if not text:
            text = TAPBACK_LABELS.get(tapback_type)
        if not text:
            continue
        context_by_chat.setdefault(chat_id, []).append({
            "sender": sender,
            "text": text,
            "from_me": bool(from_me),
            "unix_ts": apple_to_unix(apple_ts),
        })
        counts[chat_id] = counts.get(chat_id, 0) + 1

    for chat_id, ctx_msgs in context_by_chat.items():
        ctx_msgs.reverse()  # restore chronological order
        threads[chat_id]["messages"] = ctx_msgs + threads[chat_id]["messages"]

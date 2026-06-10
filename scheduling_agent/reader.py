import sqlite3
import time
from pathlib import Path

CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"

# iMessage timestamps are nanoseconds since 2001-01-01 00:00:00 UTC
# Convert to/from Unix epoch (seconds since 1970-01-01)
APPLE_EPOCH_OFFSET = 978307200  # seconds between 1970-01-01 and 2001-01-01


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
                m.date AS apple_ts
            FROM message m
            JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            JOIN chat c ON cmj.chat_id = c.ROWID
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.date > ?
              AND ((m.text IS NOT NULL AND m.text != '') OR m.attributedBody IS NOT NULL)
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

    for chat_id, msg_id, text, attributed_body, sender, from_me, apple_ts in rows:
        participants = participants_by_chat.get(chat_id, [])
        if any(p in blocked_set for p in participants):
            continue

        if not text:
            text = _decode_attributed_body(attributed_body)
        if not text:
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

    return list(threads.values())

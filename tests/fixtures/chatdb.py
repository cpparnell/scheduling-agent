"""Build a temporary SQLite database that mirrors the minimal subset of the
real macOS ``chat.db`` schema that ``reader.get_threads_since`` queries, plus a
helper to encode message text into an ``attributedBody`` typedstream BLOB.

The encoder is the exact inverse of ``reader._decode_attributed_body`` so tests
can exercise the NULL-``text`` / attributedBody-only path that real sent
messages take on modern macOS.
"""

import sqlite3

from scheduling_agent import reader

# Only the columns reader.py actually SELECTs. Real chat.db has many more.
SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT);
CREATE TABLE message (
    ROWID INTEGER PRIMARY KEY,
    text TEXT,
    attributedBody BLOB,
    handle_id INTEGER,
    date INTEGER,
<<<<<<< HEAD
    is_from_me INTEGER,
    associated_message_type INTEGER DEFAULT 0
=======
    is_from_me INTEGER
>>>>>>> origin
);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
"""


def encode_attributed_body(text: str) -> bytes:
    """Inverse of reader._decode_attributed_body.

    Produces a typedstream-shaped blob: an ``NSString`` class marker, the ``+``
    token, then a length prefix (single byte for <128 payload bytes, else 0x81
    plus a 2-byte little-endian length), then the UTF-8 payload.
    """
    payload = text.encode("utf-8")
    n = len(payload)
    if n >= 128:
        length = b"\x81" + n.to_bytes(2, "little")
    else:
        length = bytes([n])
    return (
        b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84"
        b"\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84"
        b"\x08NSString\x01\x94\x84\x01+"
        + length
        + payload
        + b"\x86"
    )


def build_chat_db(path, chats: list[dict]):
    """Create a chat.db fixture at ``path`` from a declarative description.

    ``chats`` is a list of chat dicts::

        {
          "participants": ["+15551234567", ...],   # handle ids (no 'me')
          "messages": [
            {"text": "hi", "from_me": False, "unix_ts": 1700000000.0},
            {"attributed": "sent via blob", "from_me": True, "unix_ts": ...},
            {"raw_attributed": b"...garbage...", "from_me": False, "unix_ts": ...},
            {"text": "grp", "from_me": False, "sender": "+1555...", "unix_ts": ...},
          ],
        }

    A message provides its content via exactly one of ``text`` (text column),
    ``attributed`` (encoded into the attributedBody BLOB, text column NULL), or
    ``raw_attributed`` (raw bytes stored verbatim — for malformed-blob tests).
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(SCHEMA)
        cur = conn.cursor()

        handle_rowid: dict[str, int] = {}

        def get_handle(hid: str) -> int:
            if hid not in handle_rowid:
                rowid = len(handle_rowid) + 1
                handle_rowid[hid] = rowid
                cur.execute("INSERT INTO handle (ROWID, id) VALUES (?, ?)", (rowid, hid))
            return handle_rowid[hid]

        msg_rowid = 0
        for chat_idx, chat in enumerate(chats, start=1):
            cur.execute(
                "INSERT INTO chat (ROWID, guid) VALUES (?, ?)",
                (chat_idx, f"chat-{chat_idx}"),
            )
            participants = chat.get("participants", [])
            for p in participants:
                cur.execute(
                    "INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (?, ?)",
                    (chat_idx, get_handle(p)),
                )

            for msg in chat.get("messages", []):
                msg_rowid += 1
                from_me = msg.get("from_me", False)
                if from_me:
                    handle_id = 0  # real chat.db: sent messages have handle_id 0
                else:
                    sender = msg.get("sender") or (participants[0] if participants else None)
                    handle_id = get_handle(sender) if sender else 0

                text = msg.get("text")
                if "raw_attributed" in msg:
                    blob = msg["raw_attributed"]
                elif msg.get("attributed") is not None:
                    blob = encode_attributed_body(msg["attributed"])
                else:
                    blob = None

<<<<<<< HEAD
                tapback_type = msg.get("tapback", 0)

                cur.execute(
                    "INSERT INTO message "
                    "(ROWID, text, attributedBody, handle_id, date, is_from_me, associated_message_type) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
=======
                cur.execute(
                    "INSERT INTO message "
                    "(ROWID, text, attributedBody, handle_id, date, is_from_me) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
>>>>>>> origin
                    (
                        msg_rowid,
                        text,
                        blob,
                        handle_id,
                        reader.unix_to_apple(msg["unix_ts"]),
                        1 if from_me else 0,
<<<<<<< HEAD
                        tapback_type,
=======
>>>>>>> origin
                    ),
                )
                cur.execute(
                    "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)",
                    (chat_idx, msg_rowid),
                )

        conn.commit()
    finally:
        conn.close()
    return path

"""Best-effort handle -> name lookup from the macOS AddressBook, so the
labeler sees "Maya" instead of "+13125550123". Read-only; any failure
(schema change, no access) falls back to showing raw handles."""

import re
import sqlite3
from pathlib import Path

AB_SOURCES = Path.home() / "Library" / "Application Support" / "AddressBook" / "Sources"


def phone_key(handle: str) -> str | None:
    digits = re.sub(r"\D", "", handle)
    return digits[-10:] if len(digits) >= 7 else None


def load() -> dict[str, str]:
    """Map of normalized handle (last 10 phone digits, or lowercased email) to name."""
    names: dict[str, str] = {}
    for db in AB_SOURCES.glob("*/AddressBook-v22.abcddb"):
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        except sqlite3.Error:
            continue
        try:
            people: dict[int, str] = {}
            for pk, first, last, org, nick in conn.execute(
                "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION, ZNICKNAME FROM ZABCDRECORD"
            ):
                name = " ".join(p for p in (first, last) if p) or nick or org
                if name:
                    people[pk] = name
            for owner, number in conn.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
                key = phone_key(number or "")
                if key and owner in people:
                    names.setdefault(key, people[owner])
            for owner, addr in conn.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
                if addr and owner in people:
                    names.setdefault(addr.lower(), people[owner])
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return names


def name_for(handle: str, names: dict[str, str]) -> str:
    if handle == "me":
        return "You"
    key = handle.lower() if "@" in handle else phone_key(handle)
    return names.get(key or "", handle)

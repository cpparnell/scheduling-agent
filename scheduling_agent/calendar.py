import logging
import subprocess
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_RRULE = {
    "daily": "FREQ=DAILY;INTERVAL=1",
    "weekly": "FREQ=WEEKLY;INTERVAL=1",
    "biweekly": "FREQ=WEEKLY;INTERVAL=2",
    "monthly": "FREQ=MONTHLY;INTERVAL=1",
}


def _applescript_date(dt: datetime) -> str:
    """Convert a Python datetime to an AppleScript date literal."""
    return dt.strftime("%B %d, %Y at %I:%M:%S %p")


def _escape_as_string(value: str) -> str:
    """Escape a value for embedding in a double-quoted AppleScript string
    literal. Backslashes must be escaped BEFORE quotes, or a value ending in
    a backslash (e.g. from message-derived text) would escape the closing
    quote instead of itself, letting the rest of the string spill into the
    script."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _compute_span(
    date_str: str,
    time_start: str | None,
    duration_minutes: int | None,
    end_date: str | None,
) -> tuple[datetime, datetime, bool]:
    """Resolve an event's (start_dt, end_dt, is_allday) from its fields."""
    is_allday = time_start is None
    start_date = datetime.strptime(date_str, "%Y-%m-%d")

    if is_allday:
        start_dt = start_date
        last_day = datetime.strptime(end_date, "%Y-%m-%d") if end_date else start_date
        # Calendar's all-day end date is exclusive, so it must land on the
        # midnight *after* the last day of the event.
        end_dt = last_day + timedelta(days=1)
    else:
        start_dt = datetime.strptime(f"{date_str} {time_start}", "%Y-%m-%d %H:%M")
        if end_date:
            end_dt = datetime.strptime(f"{end_date} {time_start}", "%Y-%m-%d %H:%M")
        else:
            end_dt = start_dt + timedelta(minutes=duration_minutes or 60)
    return start_dt, end_dt, is_allday


def _run_osascript(script: str, timeout: float = 15) -> str | None:
    """Run an AppleScript, returning stdout on success or None on any failure."""
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        logger.error("osascript error: %s", result.stderr.strip())
        return None
    return result.stdout


def create_event(
    title: str,
    date_str: str,
    time_start: str | None,
    duration_minutes: int | None,
    location: str | None,
    calendar_name: str = "Calendar",
    tentative: bool = False,
    recurrence: str | None = None,
    end_date: str | None = None,
) -> str | None:
    """
    Create an Apple Calendar event via osascript.
    Returns the created event's UID on success (empty string if Calendar
    didn't return one), or None on failure.
    """
    try:
        start_dt, end_dt, is_allday = _compute_span(
            date_str, time_start, duration_minutes, end_date
        )

        start_str = _applescript_date(start_dt)
        end_str = _applescript_date(end_dt)

        display_title = f"(Tentative) {title}" if tentative else title
        safe_title = _escape_as_string(display_title)
        safe_calendar = _escape_as_string(calendar_name)

        props = f'{{summary:"{safe_title}", start date:date "{start_str}", end date:date "{end_str}"'
        if is_allday:
            props += ", allday event:true"
        if location:
            safe_location = _escape_as_string(location)
            props += f', location:"{safe_location}"'
        props += "}"

        recurrence_line = ""
        if recurrence and recurrence in _RRULE:
            recurrence_line = f'\n    set recurrence of newEvent to "{_RRULE[recurrence]}"'

        script = f"""
tell application "Calendar"
    set targetCalendar to first calendar whose name is "{safe_calendar}"
    set newEvent to make new event at targetCalendar with properties {props}{recurrence_line}
    return uid of newEvent
end tell
"""

        out = _run_osascript(script)
        if out is None:
            return None

        logger.info("Created calendar event: %s on %s", title, date_str)
        return out.strip()

    except subprocess.TimeoutExpired:
        logger.error("Calendar creation timed out for event: %s", title)
        return None
    except Exception as e:
        logger.error("Failed to create calendar event '%s': %s", title, e)
        return None


def update_event(
    uid: str,
    title: str,
    date_str: str,
    time_start: str | None,
    duration_minutes: int | None,
    location: str | None,
    calendar_name: str = "Calendar",
    tentative: bool = False,
    end_date: str | None = None,
) -> bool:
    """
    Rewrite an existing Apple Calendar event's properties (found by UID) to the
    given merged field values. Same field semantics as create_event.
    Returns True on success, False on any failure.
    """
    try:
        start_dt, end_dt, is_allday = _compute_span(
            date_str, time_start, duration_minutes, end_date
        )

        display_title = f"(Tentative) {title}" if tentative else title
        safe_title = _escape_as_string(display_title)
        safe_calendar = _escape_as_string(calendar_name)
        safe_uid = _escape_as_string(uid)

        location_line = ""
        if location:
            safe_location = _escape_as_string(location)
            location_line = f'\n    set location of theEvent to "{safe_location}"'

        script = f"""
tell application "Calendar"
    set targetCalendar to first calendar whose name is "{safe_calendar}"
    set theEvent to first event of targetCalendar whose uid is "{safe_uid}"
    set summary of theEvent to "{safe_title}"
    set newStart to date "{_applescript_date(start_dt)}"
    set newEnd to date "{_applescript_date(end_dt)}"
    -- Calendar saves after each property set and rejects start >= end, so
    -- moving an event past its old end must set the end first.
    if newStart is greater than or equal to (end date of theEvent) then
        set end date of theEvent to newEnd
        set start date of theEvent to newStart
    else
        set start date of theEvent to newStart
        set end date of theEvent to newEnd
    end if
    set allday event of theEvent to {"true" if is_allday else "false"}{location_line}
    return uid of theEvent
end tell
"""

        if _run_osascript(script) is None:
            return False

        logger.info("Updated calendar event %s: %s on %s", uid, title, date_str)
        return True

    except subprocess.TimeoutExpired:
        logger.error("Calendar update timed out for event: %s", title)
        return False
    except Exception as e:
        logger.error("Failed to update calendar event '%s': %s", title, e)
        return False


def delete_event(uid: str, calendar_name: str = "Calendar") -> bool:
    """
    Delete an existing Apple Calendar event by UID. Returns True on success
    (including when the event no longer exists — the end state matches
    intent), False on any other failure.
    """
    try:
        safe_calendar = _escape_as_string(calendar_name)
        safe_uid = _escape_as_string(uid)

        script = f"""
tell application "Calendar"
    set targetCalendar to first calendar whose name is "{safe_calendar}"
    set matched to (every event of targetCalendar whose uid is "{safe_uid}")
    repeat with theEvent in matched
        delete theEvent
    end repeat
end tell
"""

        if _run_osascript(script) is None:
            return False

        logger.info("Deleted calendar event %s", uid)
        return True

    except subprocess.TimeoutExpired:
        logger.error("Calendar deletion timed out for uid: %s", uid)
        return False
    except Exception as e:
        logger.error("Failed to delete calendar event %s: %s", uid, e)
        return False


# Field/row separators for the get_events_near AppleScript output. ASCII unit
# and record separators can't plausibly appear in event titles or locations.
_FIELD_SEP = "\x1f"
_ROW_SEP = "\x1e"


def get_events_near(
    date_str: str,
    window_days: int = 1,
    calendar_name: str = "Calendar",
) -> list[dict]:
    """
    Fetch events from the target calendar within +/- window_days of date_str,
    shaped like state records ({title, date, time_start, location,
    calendar_uid, source: "calendar"}). Fail-open: returns [] on any error so a
    broken calendar query can never block event creation.
    """
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d")
        window_start = target - timedelta(days=window_days)
        window_end = target + timedelta(days=window_days + 1)  # exclusive

        safe_calendar = _escape_as_string(calendar_name)
        script = f"""
tell application "Calendar"
    set targetCalendar to first calendar whose name is "{safe_calendar}"
    set fs to character id 31
    set rs to character id 30
    set windowStart to date "{_applescript_date(window_start)}"
    set windowEnd to date "{_applescript_date(window_end)}"
    set matched to (every event of targetCalendar whose start date is greater than or equal to windowStart and start date is less than windowEnd)
    set out to ""
    repeat with theEvent in matched
        set sd to start date of theEvent
        set loc to location of theEvent
        if loc is missing value then set loc to ""
        set rowText to (uid of theEvent) & fs & (summary of theEvent) & fs & (year of sd) & fs & ((month of sd) as integer) & fs & (day of sd) & fs & (hours of sd) & fs & (minutes of sd) & fs & (allday event of theEvent) & fs & loc
        set out to out & rowText & rs
    end repeat
    return out
end tell
"""

        out = _run_osascript(script)
        if out is None:
            return []
        return _parse_event_rows(out)

    except subprocess.TimeoutExpired:
        logger.error("Calendar query timed out for %s", date_str)
        return []
    except Exception as e:
        logger.error("Failed to query calendar near %s: %s", date_str, e)
        return []


def _parse_event_rows(out: str) -> list[dict]:
    events = []
    # NB: no strip() on the raw output — Python considers \x1e/\x1f whitespace,
    # so stripping would eat separators around empty trailing fields.
    for row in out.split(_ROW_SEP):
        if not row.strip():
            continue
        fields = row.split(_FIELD_SEP)
        if len(fields) != 9:
            logger.warning("Skipping malformed calendar query row: %r", row)
            continue
        uid, summary, year, month, day, hours, minutes, allday, location = fields
        try:
            date = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
            is_allday = allday.strip().lower() == "true"
            time_start = None if is_allday else f"{int(hours):02d}:{int(minutes):02d}"
        except ValueError:
            logger.warning("Skipping malformed calendar query row: %r", row)
            continue
        title = summary.strip()
        if title.startswith("(Tentative) "):
            title = title[len("(Tentative) "):]
        events.append({
            "title": title,
            "date": date,
            "time_start": time_start,
            "location": location.strip() or None,
            "calendar_uid": uid.strip(),
            "source": "calendar",
        })
    return events


# --- Mock calendars ----------------------------------------------------------
#
# Backfill can write into a throwaway calendar instead of no-op'ing its writes
# (see main.prepare_mock_calendar). Every calendar created that way carries
# this exact description, and clear/delete/write are refused on any calendar
# without it — so a typo'd name can never empty or delete a real calendar.

MOCK_CALENDAR_MARKER = "scheduling-agent mock calendar (safe to delete)"

# Clearing or deleting a calendar with months of backfilled events is far
# slower than a single-event write.
_BULK_TIMEOUT = 120


def get_calendar_info(calendar_name: str) -> dict | None:
    """Look up a calendar by name. Returns {"count": <calendars with that
    name>, "description": <first match's description, "" if none>}, with
    count == 0 when no such calendar exists, or None if Calendar couldn't be
    queried at all."""
    try:
        safe_calendar = _escape_as_string(calendar_name)
        script = f"""
tell application "Calendar"
    set matched to (every calendar whose name is "{safe_calendar}")
    set n to count of matched
    if n is 0 then return "0" & character id 31
    set d to description of item 1 of matched
    if d is missing value then set d to ""
    return (n as text) & character id 31 & d
end tell
"""
        out = _run_osascript(script)
        if out is None:
            return None
        count, _, description = out.rstrip("\n").partition(_FIELD_SEP)
        return {"count": int(count), "description": description}
    except subprocess.TimeoutExpired:
        logger.error("Calendar lookup timed out for %s", calendar_name)
        return None
    except Exception as e:
        logger.error("Failed to look up calendar %s: %s", calendar_name, e)
        return None


def list_calendars() -> list[dict] | None:
    """Every calendar as {"name", "description"} ("" if none), or None if
    Calendar couldn't be queried."""
    try:
        script = """
tell application "Calendar"
    set fs to character id 31
    set rs to character id 30
    set out to ""
    repeat with theCalendar in calendars
        set d to description of theCalendar
        if d is missing value then set d to ""
        set out to out & (name of theCalendar) & fs & d & rs
    end repeat
    return out
end tell
"""
        out = _run_osascript(script)
        if out is None:
            return None
        calendars = []
        for row in out.split(_ROW_SEP):
            if not row.strip():
                continue
            name, _, description = row.partition(_FIELD_SEP)
            calendars.append({"name": name.lstrip("\n"), "description": description.rstrip("\n")})
        return calendars
    except subprocess.TimeoutExpired:
        logger.error("Listing calendars timed out")
        return None
    except Exception as e:
        logger.error("Failed to list calendars: %s", e)
        return None


def create_mock_calendar(calendar_name: str) -> bool:
    """Create a new calendar tagged with MOCK_CALENDAR_MARKER. It lands in
    Calendar's default account (iCloud, if you use it). Returns True on
    success."""
    try:
        safe_calendar = _escape_as_string(calendar_name)
        safe_marker = _escape_as_string(MOCK_CALENDAR_MARKER)
        script = f"""
tell application "Calendar"
    set newCalendar to make new calendar with properties {{name:"{safe_calendar}"}}
    set description of newCalendar to "{safe_marker}"
end tell
"""
        if _run_osascript(script) is None:
            return False
        logger.info("Created mock calendar %s", calendar_name)
        return True
    except subprocess.TimeoutExpired:
        logger.error("Calendar creation timed out for %s", calendar_name)
        return False
    except Exception as e:
        logger.error("Failed to create calendar %s: %s", calendar_name, e)
        return False


def count_events(calendar_name: str) -> int | None:
    """Number of events on a calendar, or None on failure."""
    try:
        safe_calendar = _escape_as_string(calendar_name)
        script = f"""
tell application "Calendar"
    return count of events of (first calendar whose name is "{safe_calendar}")
end tell
"""
        out = _run_osascript(script, timeout=_BULK_TIMEOUT)
        if out is None:
            return None
        return int(out.strip())
    except subprocess.TimeoutExpired:
        logger.error("Event count timed out for %s", calendar_name)
        return None
    except Exception as e:
        logger.error("Failed to count events on %s: %s", calendar_name, e)
        return None


def clear_calendar(calendar_name: str) -> bool:
    """Delete every event on a calendar. Callers must have verified it's a
    mock calendar first (main.prepare_mock_calendar)."""
    try:
        safe_calendar = _escape_as_string(calendar_name)
        script = f"""
tell application "Calendar"
    delete every event of (first calendar whose name is "{safe_calendar}")
end tell
"""
        if _run_osascript(script, timeout=_BULK_TIMEOUT) is None:
            return False
        logger.info("Cleared all events from %s", calendar_name)
        return True
    except subprocess.TimeoutExpired:
        logger.error("Clearing calendar %s timed out", calendar_name)
        return False
    except Exception as e:
        logger.error("Failed to clear calendar %s: %s", calendar_name, e)
        return False


def delete_calendar(calendar_name: str) -> bool:
    """Delete a whole calendar. Callers must have verified it's a mock
    calendar first (main.delete_mock_calendar)."""
    try:
        safe_calendar = _escape_as_string(calendar_name)
        script = f"""
tell application "Calendar"
    delete (first calendar whose name is "{safe_calendar}")
end tell
"""
        if _run_osascript(script, timeout=_BULK_TIMEOUT) is None:
            return False
        logger.info("Deleted calendar %s", calendar_name)
        return True
    except subprocess.TimeoutExpired:
        logger.error("Deleting calendar %s timed out", calendar_name)
        return False
    except Exception as e:
        logger.error("Failed to delete calendar %s: %s", calendar_name, e)
        return False

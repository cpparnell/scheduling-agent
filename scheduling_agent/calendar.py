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


def _run_osascript(script: str) -> str | None:
    """Run an AppleScript, returning stdout on success or None on any failure."""
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=15,
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
        safe_title = display_title.replace('"', '\\"')
        safe_calendar = calendar_name.replace('"', '\\"')

        props = f'{{summary:"{safe_title}", start date:date "{start_str}", end date:date "{end_str}"'
        if is_allday:
            props += ", allday event:true"
        if location:
            safe_location = location.replace('"', '\\"')
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
        safe_title = display_title.replace('"', '\\"')
        safe_calendar = calendar_name.replace('"', '\\"')
        safe_uid = uid.replace('"', '\\"')

        location_line = ""
        if location:
            safe_location = location.replace('"', '\\"')
            location_line = f'\n    set location of theEvent to "{safe_location}"'

        script = f"""
tell application "Calendar"
    set targetCalendar to first calendar whose name is "{safe_calendar}"
    set theEvent to first event of targetCalendar whose uid is "{safe_uid}"
    set summary of theEvent to "{safe_title}"
    set start date of theEvent to date "{_applescript_date(start_dt)}"
    set end date of theEvent to date "{_applescript_date(end_dt)}"
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

        safe_calendar = calendar_name.replace('"', '\\"')
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

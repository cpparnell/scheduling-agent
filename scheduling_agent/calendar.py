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

        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=15,
        )

        if result.returncode != 0:
            logger.error("osascript error: %s", result.stderr.strip())
            return None

        logger.info("Created calendar event: %s on %s", title, date_str)
        return result.stdout.strip()

    except subprocess.TimeoutExpired:
        logger.error("Calendar creation timed out for event: %s", title)
        return None
    except Exception as e:
        logger.error("Failed to create calendar event '%s': %s", title, e)
        return None

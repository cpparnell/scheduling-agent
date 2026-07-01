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
) -> bool:
    """
    Create an Apple Calendar event via osascript.
    Returns True on success, False on failure.
    """
    try:
        time_part = time_start or "12:00"
        start_dt = datetime.strptime(f"{date_str} {time_part}", "%Y-%m-%d %H:%M")

        if end_date:
            end_dt = datetime.strptime(f"{end_date} {time_part}", "%Y-%m-%d %H:%M")
        else:
            end_dt = start_dt + timedelta(minutes=duration_minutes or 60)

        start_str = _applescript_date(start_dt)
        end_str = _applescript_date(end_dt)

        display_title = f"(Tentative) {title}" if tentative else title
        safe_title = display_title.replace('"', '\\"')
        safe_calendar = calendar_name.replace('"', '\\"')

        props = f'{{summary:"{safe_title}", start date:date "{start_str}", end date:date "{end_str}"'
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
            return False

        logger.info("Created calendar event: %s on %s", title, date_str)
        return True

    except subprocess.TimeoutExpired:
        logger.error("Calendar creation timed out for event: %s", title)
        return False
    except Exception as e:
        logger.error("Failed to create calendar event '%s': %s", title, e)
        return False

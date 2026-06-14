import logging
import subprocess
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


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
) -> bool:
    """
    Create an Apple Calendar event via osascript.
    Returns True on success, False on failure.
    """
    try:
        if time_start:
            start_dt = datetime.strptime(f"{date_str} {time_start}", "%Y-%m-%d %H:%M")
        else:
            # All-day: use noon so it doesn't bleed into adjacent days
            start_dt = datetime.strptime(f"{date_str} 12:00", "%Y-%m-%d %H:%M")

        end_dt = start_dt + timedelta(minutes=duration_minutes or 60)

        start_str = _applescript_date(start_dt)
        end_str = _applescript_date(end_dt)

        safe_title = title.replace('"', '\\"')
        safe_calendar = calendar_name.replace('"', '\\"')

        props = f'{{summary:"{safe_title}", start date:date "{start_str}", end date:date "{end_str}"'
        if location:
            safe_location = location.replace('"', '\\"')
            props += f', location:"{safe_location}"'
        if tentative:
            props += ", status:tentative"
        props += "}"

        script = f"""
tell application "Calendar"
    set targetCalendar to first calendar whose name is "{safe_calendar}"
    make new event at targetCalendar with properties {props}
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

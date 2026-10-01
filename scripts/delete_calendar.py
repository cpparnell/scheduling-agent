"""Delete a calendar from Apple Calendar, e.g. the "SA test: ..." calendars
that `--backfill --calendar` leaves behind.

Deliberately a separate, manual script: it is never called by the agent, and
it always asks you to type the calendar's name before deleting anything.

    python scripts/delete_calendar.py                  # list calendars
    python scripts/delete_calendar.py "SA test: main"  # delete a mock calendar
    python scripts/delete_calendar.py "Old" --any      # delete ANY calendar

Without --any, only calendars created by scheduling-agent as mocks (tagged in
their description) can be deleted. Deleting a calendar deletes all of its
events permanently, and on iCloud that syncs to every device.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scheduling_agent import calendar, config  # noqa: E402


def _list() -> int:
    calendars = calendar.list_calendars()
    if calendars is None:
        print("ERROR: couldn't query Calendar.app")
        return 1
    for c in calendars:
        tag = "  [scheduling-agent mock]" if c["description"] == calendar.MOCK_CALENDAR_MARKER else ""
        print(f"{c['name']}{tag}")
    return 0


def run(argv: list[str], input_fn=input) -> int:
    parser = argparse.ArgumentParser(
        prog="delete_calendar.py",
        description="Delete an Apple Calendar calendar. Run with no name to list calendars.",
    )
    parser.add_argument("name", nargs="?", help="Exact name of the calendar to delete.")
    parser.add_argument(
        "--any", action="store_true",
        help="Allow deleting a calendar that scheduling-agent didn't create as a mock.",
    )
    args = parser.parse_args(argv)

    if args.name is None:
        return _list()

    name = args.name
    info = calendar.get_calendar_info(name)
    if info is None:
        print("ERROR: couldn't query Calendar.app")
        return 1
    if info["count"] == 0:
        print(f"ERROR: no calendar named {name!r} (run with no arguments to list them)")
        return 1
    if info["count"] > 1:
        print(f"ERROR: {info['count']} calendars are named {name!r}; rename one in Calendar.app first")
        return 1

    is_mock = info["description"] == calendar.MOCK_CALENDAR_MARKER
    if not is_mock and not args.any:
        print(
            f"ERROR: {name!r} wasn't created by scheduling-agent as a mock calendar. "
            f"If you really want to delete it, re-run with --any."
        )
        return 1

    n_events = calendar.count_events(name)
    print(f"Calendar:  {name}")
    print(f"Events:    {n_events if n_events is not None else 'unknown'}")
    print(f"Type:      {'scheduling-agent mock' if is_mock else 'NOT a scheduling-agent mock'}")
    if name.strip().casefold() == config.load()["target_calendar"].strip().casefold():
        print("WARNING:   this is the target_calendar the live agent writes to.")
    print("Deleting it permanently removes all of its events (on every synced device).")

    if input_fn("Type the calendar name to confirm: ") != name:
        print("Aborted — nothing deleted.")
        return 1
    if not calendar.delete_calendar(name):
        print("ERROR: deletion failed (see the osascript error above)")
        return 1
    print(f"Deleted {name!r}.")
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))

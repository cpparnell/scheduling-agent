import importlib.util
from pathlib import Path

import pytest

from scheduling_agent import calendar

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "delete_calendar.py"
_spec = importlib.util.spec_from_file_location("delete_calendar", _SCRIPT)
delete_calendar = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(delete_calendar)

MOCK = calendar.MOCK_CALENDAR_MARKER


@pytest.fixture
def fake_calendar(monkeypatch):
    """`calendars` maps name -> description; `deleted` records deletions."""
    fake = {"calendars": {}, "deleted": []}

    def get_info(name):
        if name not in fake["calendars"]:
            return {"count": 0, "description": ""}
        return {"count": 1, "description": fake["calendars"][name]}

    def delete(name):
        fake["deleted"].append(name)
        del fake["calendars"][name]
        return True

    monkeypatch.setattr(calendar, "get_calendar_info", get_info)
    monkeypatch.setattr(calendar, "count_events", lambda name: 3)
    monkeypatch.setattr(calendar, "delete_calendar", delete)
    monkeypatch.setattr(
        calendar, "list_calendars",
        lambda: [{"name": n, "description": d} for n, d in fake["calendars"].items()],
    )
    return fake


def test_deletes_mock_after_typed_confirmation(fake_calendar):
    fake_calendar["calendars"]["SA test: main"] = MOCK
    assert delete_calendar.run(["SA test: main"], input_fn=lambda _: "SA test: main") == 0
    assert fake_calendar["deleted"] == ["SA test: main"]


def test_wrong_confirmation_aborts(fake_calendar):
    fake_calendar["calendars"]["SA test: main"] = MOCK
    assert delete_calendar.run(["SA test: main"], input_fn=lambda _: "y") == 1
    assert fake_calendar["deleted"] == []


def test_non_mock_refused_without_any(fake_calendar):
    fake_calendar["calendars"]["Family"] = ""
    prompted = []
    assert delete_calendar.run(["Family"], input_fn=prompted.append) == 1
    assert prompted == []  # refused before ever asking
    assert fake_calendar["deleted"] == []


def test_non_mock_deleted_with_any_and_confirmation(fake_calendar):
    fake_calendar["calendars"]["Old stuff"] = ""
    assert delete_calendar.run(["Old stuff", "--any"], input_fn=lambda _: "Old stuff") == 0
    assert fake_calendar["deleted"] == ["Old stuff"]


def test_missing_calendar_is_an_error(fake_calendar):
    assert delete_calendar.run(["Nope"], input_fn=lambda _: "Nope") == 1
    assert fake_calendar["deleted"] == []


def test_no_name_lists_calendars_and_marks_mocks(fake_calendar, capsys):
    fake_calendar["calendars"] = {"Home": "", "SA test: main": MOCK}
    assert delete_calendar.run([]) == 0
    out = capsys.readouterr().out
    assert "Home\n" in out
    assert "SA test: main  [scheduling-agent mock]" in out
    assert fake_calendar["deleted"] == []

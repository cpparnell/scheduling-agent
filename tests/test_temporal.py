from datetime import date

import pytest

from scheduling_agent import temporal

MON = date(2026, 10, 5)  # a Monday
WED = date(2026, 10, 7)
THU = date(2026, 10, 8)
SAT = date(2026, 10, 10)
SUN = date(2026, 10, 11)


def d(day: int, month: int = 10) -> date:
    return date(2026, month, day)


@pytest.mark.parametrize(
    "phrase,sent,readings",
    [
        ("tomorrow", WED, [d(8)]),
        ("tmrw at 7", WED, [d(8)]),
        ("the day after tomorrow", WED, [d(9)]),
        ("tonight", WED, [d(7)]),
        ("this afternoon", WED, [d(7)]),
        ("in 3 days", WED, [d(10)]),
        ("this Saturday", WED, [d(10)]),
        ("this thursday", THU, [d(8)]),  # same weekday with "this" = that day
        ("next Tuesday", THU, [d(13)]),  # this week's already passed: one reading
    ],
)
def test_single_reading_phrases_are_overridable(phrase, sent, readings):
    res = temporal.resolve_phrase(phrase, sent)
    assert res.readings == readings
    assert res.overridable is True
    assert res.ambiguous is False


def test_next_weekday_is_next_weeks_even_when_this_weeks_is_ahead():
    res = temporal.resolve_phrase("next Friday", WED)
    assert res.readings == [d(16)]
    assert res.overridable is True
    assert res.ambiguous is False


def test_next_weekday_said_on_that_weekday_is_next_week():
    res = temporal.resolve_phrase("next Wednesday", WED)
    assert res.readings == [d(14)]
    assert res.ambiguous is False


@pytest.mark.parametrize("sent", [d(4), d(5), d(6), d(7), d(8), d(9), d(10)])  # Sun..Sat
@pytest.mark.parametrize("weekday", range(7))
def test_next_weekday_lands_in_the_following_sunday_start_week(sent, weekday):
    got = temporal.next_weekday(sent, weekday)
    assert got.weekday() == weekday
    assert d(11) <= got <= d(17)  # the week of Sun Oct 11 - Sat Oct 17


def test_next_saturday_said_on_sunday_is_13_days_out():
    assert temporal.resolve_phrase("next saturday", SUN).readings == [d(24)]


def test_bare_weekday_is_never_overridable():
    # Could be anchored to an event weeks away — see detector bare-weekday rules.
    res = temporal.resolve_phrase("Friday", WED)
    assert res.readings == [d(9)]
    assert res.overridable is False
    assert res.ambiguous is False


def test_bare_weekday_said_on_that_weekday_is_ambiguous():
    res = temporal.resolve_phrase("on Thursday", THU)
    assert res.readings == [d(8), d(15)]
    assert res.ambiguous is True


def test_this_weekend_midweek_is_both_days_and_not_ambiguous():
    res = temporal.resolve_phrase("this weekend", WED)
    assert res.readings == [d(10), d(11)]
    assert res.ambiguous is False
    assert res.overridable is False


def test_this_weekend_said_on_sunday_is_only_today():
    res = temporal.resolve_phrase("this weekend", SUN)
    assert res.readings == [d(11)]


def test_next_weekend_midweek_is_ambiguous():
    res = temporal.resolve_phrase("next weekend", WED)
    assert set(res.readings) == {d(17), d(18), d(10), d(11)}
    assert res.ambiguous is True


def test_next_weekend_said_on_saturday_is_the_following_one():
    res = temporal.resolve_phrase("next weekend", SAT)
    assert res.readings == [d(17), d(18)]
    assert res.ambiguous is False


@pytest.mark.parametrize(
    "phrase",
    ["October 10th", "10/14", "the 14th", "last Friday", "sometime next week", "", "the day before the marathon"],
)
def test_unhandled_phrases_return_none(phrase):
    assert temporal.resolve_phrase(phrase, WED) is None


def test_ambiguity_note_names_the_alternative():
    res = temporal.resolve_phrase("on Thursday", THU)
    note = temporal.ambiguity_note(res, d(8), THU)
    assert "on Thursday" in note
    assert "Thu Oct 8" in note  # sent date
    assert "could also be Thu Oct 15" in note


def test_ambiguity_note_weekend_names_other_weekend_by_saturday():
    res = temporal.resolve_phrase("next weekend", WED)
    note = temporal.ambiguity_note(res, d(17), WED)
    assert "Sat Oct 10" in note
    assert "Oct 18" not in note and "Oct 11" not in note


def test_ambiguity_note_none_when_unambiguous():
    res = temporal.resolve_phrase("tomorrow", WED)
    assert temporal.ambiguity_note(res, d(8), WED) is None

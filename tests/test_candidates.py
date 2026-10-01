from datetime import datetime

from scheduling_agent import candidates

# Wednesday, September 30 2026, noon local.
WED = datetime(2026, 9, 30, 12, 0).timestamp()


def _msgs(*texts, ts=WED):
    return [{"text": t, "unix_ts": ts, "from_me": False} for t in texts]


def _values(cands):
    return [c["value"] for c in cands]


# --- dates -----------------------------------------------------------------


def test_month_day_resolves_in_current_year():
    assert _values(candidates.date_candidates(_msgs("dinner Friday, October 2?"))) == ["2026-10-02"]


def test_month_day_well_in_the_past_rolls_to_next_year():
    assert _values(candidates.date_candidates(_msgs("ski trip Jan 5th"))) == ["2027-01-05"]


def test_recent_past_month_day_stays_put():
    # A few days back is a past reference, not next year's date.
    assert _values(candidates.date_candidates(_msgs("that was Sept 27"))) == ["2026-09-27"]


def test_day_of_month_phrasing():
    assert _values(candidates.date_candidates(_msgs("party on the 10th of October"))) == ["2026-10-10"]


def test_lowercase_may_is_not_a_month():
    assert candidates.date_candidates(_msgs("you may want 5 of them")) == []


def test_numeric_date_with_and_without_year():
    assert _values(candidates.date_candidates(_msgs("10/14 works"))) == ["2026-10-14"]
    assert _values(candidates.date_candidates(_msgs("10/14/27 works"))) == ["2027-10-14"]


def test_ordinal_day_after_today_is_this_month_before_is_next():
    assert _values(candidates.date_candidates(_msgs("the 30th"))) == ["2026-09-30"]
    assert _values(candidates.date_candidates(_msgs("the 5th"))) == ["2026-10-05"]


def test_month_day_isnt_reread_as_ordinal_or_weekday():
    # "Friday, October 2" is one date; neither "Friday" nor "2" adds another.
    assert _values(candidates.date_candidates(_msgs("Friday, October 2nd"))) == ["2026-10-02"]


def test_relative_words_resolve_against_send_time_not_today():
    yesterday = WED - 86400  # sent Tuesday
    got = _values(candidates.date_candidates(_msgs("movie tomorrow?", ts=yesterday)))
    assert got == ["2026-09-30"]


def test_tonight_and_day_after_tomorrow():
    assert _values(candidates.date_candidates(_msgs("tonight"))) == ["2026-09-30"]
    assert _values(candidates.date_candidates(_msgs("day after tomorrow"))) == ["2026-10-02"]


def test_bare_weekday_is_next_occurrence():
    assert _values(candidates.date_candidates(_msgs("drinks saturday?"))) == ["2026-10-03"]


def test_same_weekday_offers_today_and_a_week_out():
    assert _values(candidates.date_candidates(_msgs("spin this Wednesday"))) == [
        "2026-09-30", "2026-10-07",
    ]


def test_next_weekday_means_next_weeks():
    # Regression (anchor_next_friday): offered both readings, Jev confidently
    # picked the coming Friday. Convention is next week's.
    assert _values(candidates.date_candidates(_msgs("next friday"))) == ["2026-10-09"]


def test_last_weekday_is_ignored():
    assert candidates.date_candidates(_msgs("last friday was fun")) == []


def test_this_weekend_is_the_coming_saturday():
    assert _values(candidates.date_candidates(_msgs("this weekend?"))) == ["2026-10-03"]


def test_candidates_dedupe_by_value_and_record_first_mention():
    got = candidates.date_candidates(_msgs("saturday?", "yes Saturday!"))
    assert _values(got) == ["2026-10-03"]
    assert got[0]["msg_index"] == 0


# --- times -----------------------------------------------------------------


def test_clock_times_with_meridiem():
    assert _values(candidates.time_candidates(_msgs("7pm", "10:30am", "at 6:30 p.m."))) == [
        "19:00", "10:30", "18:30",
    ]


def test_bare_at_time_offers_pm_then_am_when_morning_is_plausible():
    assert _values(candidates.time_candidates(_msgs("at 8"))) == ["20:00", "08:00"]
    assert _values(candidates.time_candidates(_msgs("at 5"))) == ["17:00"]


def test_colon_time_without_at():
    assert _values(candidates.time_candidates(_msgs("5:30 right?"))) == ["17:30"]


def test_noon_and_midnight():
    assert _values(candidates.time_candidates(_msgs("noon", "midnight"))) == ["12:00", "00:00"]


def test_date_is_not_read_as_time():
    assert candidates.time_candidates(_msgs("at 10/14")) == []


# --- locations -------------------------------------------------------------


def test_capitalized_venue():
    assert _values(candidates.location_candidates(_msgs("drinks at Blue Bottle on Friday"))) == ["Blue Bottle"]


def test_private_place():
    assert _values(candidates.location_candidates(_msgs("dinner at our place"))) == ["our place"]


def test_lowercase_venue_but_not_filler():
    assert _values(candidates.location_candidates(_msgs("pizza at diceys", "at least 5"))) == ["diceys"]


def test_weekday_after_at_is_not_a_location():
    assert candidates.location_candidates(_msgs("at Friday")) == []


# --- activities and recurrence ----------------------------------------------


def test_lead_phrase_catches_unlisted_activities():
    assert "escape room" in _values(candidates.activity_candidates(_msgs("escape room Saturday at 6pm?")))
    assert "bonfire" in _values(candidates.activity_candidates(_msgs("bonfire Saturday 7pm?")))


def test_lead_phrase_strips_filler():
    assert _values(candidates.activity_candidates(_msgs("lets go to pizza at diceys")))[0] == "pizza"


def test_replies_are_not_activities():
    assert candidates.activity_candidates(_msgs("yes sounds good!", "maybe")) == []


def test_keyword_activity_found_mid_message():
    assert "dinner" in _values(candidates.activity_candidates(_msgs("want to grab dinner friday?")))


def test_recurrence():
    assert candidates.recurrence(_msgs("standup every Monday at 10")) == "weekly"
    assert candidates.recurrence(_msgs("every other week")) == "biweekly"
    assert candidates.recurrence(_msgs("dinner friday")) is None


def test_bare_hour_in_change_of_time_phrasing():
    # Regression (pipe_time_drift_reschedule): "8" was never offered, so Jev
    # could only re-pick the original 7pm.
    assert "20:00" in _values(candidates.time_candidates(_msgs("actually can we do 8 instead?")))
    assert "20:00" in _values(candidates.time_candidates(_msgs("sure, 8 works")))
    assert "20:30" in _values(candidates.time_candidates(_msgs("push it to 8:30")))


def test_bare_number_without_time_context_is_not_a_time():
    assert candidates.time_candidates(_msgs("there are 8 of us")) == []

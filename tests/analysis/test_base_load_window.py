"""Base load over a chosen time-of-day window, including windows past midnight."""

import math
from datetime import date, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ovo_insights.analysis.half_hourly import (
    DEFAULT_WINDOW,
    TimeWindow,
    base_load,
    local_slot,
    night_starts,
    nightly_base_load,
)
from ovo_insights.sync.plan import DateRange
from ovo_insights.timeutil import london_half_hours


def all_half_hours(first: date, last: date, value: float = 0.1) -> dict[datetime, float]:
    return {
        start: value for day in DateRange(first, last).days() for start in london_half_hours(day)
    }


def varied(first: date, last: date) -> dict[datetime, float]:
    """A different value in every half-hour, so any mix-up of slots changes the answer."""
    found = all_half_hours(first, last)
    return {start: 0.05 + (i % 97) / 500 + (i % 7) / 3000 for i, start in enumerate(found)}


# -- the window ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "slots", "label", "wraps"),
    [
        ("00:00", "05:00", (0, 10), "00:00\u201305:00", False),
        ("22:30", "23:30", (45, 47), "22:30\u201323:30", False),
        ("22:00", "00:00", (44, 48), "22:00\u201300:00", False),  # 00:00 as an end is midnight
        ("00:00", "00:00", (0, 48), "00:00\u201300:00", False),  # the whole day
        ("22:00", "06:00", (44, 12), "22:00\u201306:00", True),
        ("23:30", "00:30", (47, 1), "23:30\u201300:30", True),
    ],
)
def test_parsing_times_into_a_window(
    start: str, end: str, slots: tuple[int, int], label: str, wraps: bool
) -> None:
    window = TimeWindow.parse(start, end)
    assert (window.start, window.end) == slots
    assert (window.label, window.wraps) == (label, wraps)
    assert TimeWindow.parse("00:00", "05:00") == DEFAULT_WINDOW


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("06:00", "06:00"),  # empty
        ("00:30", "00:30"),
        ("24:00", "05:00"),
        ("7:00", "08:00"),
        ("07:15", "08:00"),
        ("07:00", "08:00:00"),
        ("", "08:00"),
        ("07:00", "\u0660\u0668:\u0660\u0660"),
        ("07:00", "8am"),
    ],
)
def test_bad_windows_are_refused(start: str, end: str) -> None:
    with pytest.raises(ValueError, match=r"half hour|same time"):
        TimeWindow.parse(start, end)


def test_the_constructor_enforces_the_same_limits() -> None:
    for start, end in ((-1, 5), (0, 0), (48, 5), (3, 49), (5, 5)):
        with pytest.raises(ValueError, match=r"range|same time"):
            TimeWindow(start, end)


def test_which_slots_a_window_contains() -> None:
    day = TimeWindow(10, 14)
    assert [s for s in range(48) if day.contains(s)] == [10, 11, 12, 13]
    night = TimeWindow(44, 4)
    assert [s for s in range(48) if night.contains(s)] == [0, 1, 2, 3, 44, 45, 46, 47]
    assert all(TimeWindow(0, 48).contains(s) for s in range(48))


# -- the half-hours of a night ------------------------------------------------------------


def test_a_night_is_the_evening_and_the_next_morning() -> None:
    starts = night_starts(date(2026, 1, 14), TimeWindow.parse("22:00", "06:00"))
    clocks = [(local_slot(s)[0].day, local_slot(s)[1]) for s in starts]
    assert clocks == [(14, 44), (14, 45), (14, 46), (14, 47)] + [(15, i) for i in range(12)]
    assert starts == sorted(starts)


def test_clock_change_nights_have_the_slots_that_really_exist() -> None:
    wrapping = TimeWindow.parse("22:00", "06:00")
    assert len(night_starts(date(2026, 3, 28), wrapping)) == 4 + 10  # the 01:00-02:00 hour skipped
    assert len(night_starts(date(2025, 10, 25), wrapping)) == 4 + 14  # 01:00-02:00 happens twice
    assert len(night_starts(date(2026, 1, 14), wrapping)) == 4 + 12
    overnight = DEFAULT_WINDOW
    assert len(night_starts(date(2026, 3, 29), overnight)) == 8  # 00:00 00:30 then 02:00-04:30
    assert len(night_starts(date(2025, 10, 26), overnight)) == 12


@given(
    day=st.dates(min_value=date(2015, 1, 1), max_value=date(2035, 12, 20)),
    start=st.integers(min_value=0, max_value=47),
    end=st.integers(min_value=1, max_value=48),
)
def test_every_half_hour_in_the_window_belongs_to_exactly_one_night(
    day: date, start: int, end: int
) -> None:
    if start == end:
        return
    window = TimeWindow(start, end)
    nights = [day + timedelta(days=i) for i in range(3)]
    claimed = [s for night in nights for s in night_starts(night, window)]
    assert len(claimed) == len(set(claimed))  # no half-hour in two nights
    # Independently: every half-hour of the days around, in the window, whose night is one of ours.
    expected = set()
    for offset in range(-1, 5):
        for s in london_half_hours(day + timedelta(days=offset)):
            local_day, slot = local_slot(s)
            if window.contains(slot):
                night = local_day - timedelta(days=1) if window.wraps and slot < end else local_day
                if night in nights:
                    expected.add(s)
    assert set(claimed) == expected


# -- the figure ---------------------------------------------------------------------------


def nearest_rank_watts(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.1 * len(ordered)) - 1] * 2 * 1000


def test_the_default_window_gives_the_same_answer_as_before() -> None:
    data = varied(date(2026, 1, 10), date(2026, 1, 20))
    wall = [v for s, v in data.items() if local_slot(s)[1] < 10]
    load = base_load(data)
    assert load is not None
    assert load.watts == pytest.approx(nearest_rank_watts(wall))
    assert load.samples == len(wall)
    assert base_load(data, span=DateRange(date(2026, 1, 10), date(2026, 1, 20))) == load


def test_a_daytime_window() -> None:
    data = varied(date(2026, 1, 10), date(2026, 1, 12))
    window = TimeWindow.parse("12:00", "14:00")
    wall = [v for s, v in data.items() if 24 <= local_slot(s)[1] < 28]
    load = base_load(data, window, DateRange(date(2026, 1, 10), date(2026, 1, 12)))
    assert load is not None
    assert load.samples == 12
    assert load.watts == pytest.approx(nearest_rank_watts(wall))


def test_a_window_past_midnight_counts_each_morning_with_the_evening_before() -> None:
    data = varied(date(2026, 1, 9), date(2026, 1, 14))
    span = DateRange(date(2026, 1, 10), date(2026, 1, 12))
    window = TimeWindow.parse("22:00", "06:00")
    mine = []
    for s, v in data.items():
        local_day, slot = local_slot(s)
        if window.contains(slot):
            night = local_day - timedelta(days=1) if slot < window.end else local_day
            if night in span:
                mine.append(v)
    load = base_load(data, window, span)
    assert load is not None
    assert load.samples == len(mine) == 3 * 16  # three nights of 4 evening + 12 morning slots
    assert load.watts == pytest.approx(nearest_rank_watts(mine))
    # The morning of the first day belongs to the night before the range: not counted.
    low_morning = dict(data)
    for s in london_half_hours(date(2026, 1, 10)):
        if local_slot(s)[1] < 12:
            low_morning[s] = 0.0
    assert base_load(low_morning, window, span) == load


def test_nothing_in_the_window_is_no_figure() -> None:
    assert base_load({}) is None
    assert base_load({}, TimeWindow(10, 14), DateRange(date(2026, 1, 1), date(2026, 1, 2))) is None
    only_day = all_half_hours(date(2026, 1, 1), date(2026, 1, 1))
    assert base_load({s: v for s, v in only_day.items() if local_slot(s)[1] >= 20}) is None


# -- night by night -----------------------------------------------------------------------


def test_each_night_gets_its_own_figure_and_incomplete_nights_none() -> None:
    first, last = date(2026, 1, 10), date(2026, 1, 13)
    data = varied(first, last + timedelta(days=1))
    window = TimeWindow.parse("22:00", "06:00")
    # Night of the 11th loses one morning half-hour; the night of the 13th has no morning data.
    del data[night_starts(date(2026, 1, 11), window)[10]]
    for s in london_half_hours(last + timedelta(days=1)):
        del data[s]
    nights = nightly_base_load(data, window, DateRange(first, last))
    assert [n.night for n in nights] == DateRange(first, last).days()
    assert [n.watts is None for n in nights] == [False, True, False, True]
    fine = [data[s] for s in night_starts(first, window)]
    assert nights[0].watts == pytest.approx(nearest_rank_watts(fine))


def test_a_night_with_no_data_at_all_is_none_not_zero() -> None:
    nights = nightly_base_load({}, DEFAULT_WINDOW, DateRange(date(2026, 1, 1), date(2026, 1, 2)))
    assert [n.watts for n in nights] == [None, None]


def test_clock_change_nights_are_complete_when_every_real_slot_is_there() -> None:
    first, last = date(2026, 3, 27), date(2026, 3, 29)
    data = all_half_hours(first, last + timedelta(days=1), 0.2)
    nights = nightly_base_load(data, TimeWindow.parse("22:00", "06:00"), DateRange(first, last))
    assert [n.watts for n in nights] == [pytest.approx(400.0)] * 3  # 0.2 kWh per 30 min = 400 W

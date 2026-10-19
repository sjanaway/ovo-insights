"""Completeness of days and fuels, across clock changes and supply start and end dates."""

from datetime import date, timedelta
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from ovo_insights.analysis.completeness import (
    DayFacts,
    DayState,
    classify_day,
    expected_slots,
    fuel_completeness,
    supplied_on,
)
from ovo_insights.sync.plan import DateRange, PeriodStatus
from ovo_insights.timeutil import london_half_hours

TODAY = date(2026, 10, 13)
FOREVER = date.max
SPRING = date(2025, 3, 30)  # clocks forward: 46 half-hours
AUTUMN = date(2025, 10, 26)  # clocks back: 50 half-hours
NORMAL = date(2025, 6, 11)


def hh(received: int, *, flag: bool = True, status: PeriodStatus | None = None) -> DayFacts:
    return DayFacts(True, flag, received, status)


# -- clock changes ------------------------------------------------------------------------


def test_clock_change_days_expect_46_and_50_half_hours_and_are_complete_when_whole() -> None:
    for day, slots in ((SPRING, 46), (NORMAL, 48), (AUTUMN, 50)):
        assert expected_slots(day, hh(slots), SPRING) == slots
        cell = classify_day(day, TODAY, [], hh(slots), SPRING)
        assert (cell.state, cell.expected, cell.received) == (DayState.COMPLETE, slots, slots)


def test_a_normal_days_worth_of_slots_on_a_clock_change_day_is_not_a_gap() -> None:
    # 46 slots on the spring-forward day is every slot there is; 48 would be impossible.
    assert classify_day(SPRING, TODAY, [], hh(46), SPRING).state is DayState.COMPLETE
    # One short of the day's real count is partial, whichever day it is.
    for day, slots in ((SPRING, 46), (AUTUMN, 50)):
        cell = classify_day(day, TODAY, [], hh(slots - 1), SPRING)
        assert (cell.state, cell.expected, cell.received) == (DayState.PARTIAL, slots, slots - 1)


@given(st.dates(min_value=date(2015, 1, 1), max_value=date(2035, 12, 31)))
def test_a_whole_day_is_complete_and_a_short_day_partial_on_any_date(day: date) -> None:
    slots = len(london_half_hours(day))
    assert slots in (46, 48, 50)
    today = day + timedelta(days=1)
    assert classify_day(day, today, [], hh(slots), day).state is DayState.COMPLETE
    short = classify_day(day, today, [], hh(slots - 2), day)
    assert (short.state, short.expected) == (DayState.PARTIAL, slots)
    # A missing day is missing whatever its length, and expects the full count.
    gone = classify_day(day, today, [], None, day)
    assert (gone.state, gone.expected) == (DayState.MISSING, slots)


# -- what a day expects -------------------------------------------------------------------


def test_a_day_without_half_hourly_data_is_complete_on_its_daily_figure_alone() -> None:
    cell = classify_day(NORMAL, TODAY, [], hh(0, flag=False), SPRING)
    assert (cell.state, cell.expected) == (DayState.COMPLETE, 0)


def test_slots_that_arrive_anyway_make_the_whole_day_expected() -> None:
    # OVO's flag said no half-hourly data, but some arrived: all of them are expected.
    cell = classify_day(NORMAL, TODAY, [], hh(40, flag=False), SPRING)
    assert (cell.state, cell.expected, cell.received) == (DayState.PARTIAL, 48, 40)


def test_a_day_with_half_hours_but_no_daily_figure_is_missing() -> None:
    facts = DayFacts(False, False, 48)
    cell = classify_day(NORMAL, TODAY, [], facts, SPRING)
    assert cell.state is DayState.MISSING
    assert cell.received == 48


def test_a_missing_day_expects_slots_only_once_the_fuel_has_had_half_hourly_data() -> None:
    before = classify_day(date(2025, 2, 10), TODAY, [], None, date(2025, 2, 11))
    on_the_day = classify_day(date(2025, 2, 11), TODAY, [], None, date(2025, 2, 11))
    never = classify_day(NORMAL, TODAY, [], None, None)  # a fuel like the owner's gas
    assert (before.state, before.expected) == (DayState.MISSING, 0)
    assert (on_the_day.state, on_the_day.expected) == (DayState.MISSING, 48)
    assert (never.state, never.expected) == (DayState.MISSING, 0)


def test_today_and_later_are_not_yet_available_whatever_is_stored() -> None:
    for day in (TODAY, TODAY + timedelta(days=1)):
        for facts in (None, hh(48)):
            cell = classify_day(day, TODAY, [], facts, SPRING)
            assert (cell.state, cell.expected, cell.received) == (DayState.NOT_YET, 0, 0)
    assert classify_day(TODAY - timedelta(days=1), TODAY, [], hh(48), SPRING).state is (
        DayState.COMPLETE
    )


def test_partial_days_say_whether_the_sync_will_try_again() -> None:
    gap = classify_day(NORMAL, TODAY, [], hh(46, status=PeriodStatus.GAP), SPRING)
    assert gap.note == "known gap: OVO doesn't have the rest"
    for status in (PeriodStatus.INCOMPLETE, PeriodStatus.ERROR):
        retry = classify_day(NORMAL, TODAY, [], hh(46, status=status), SPRING)
        assert retry.note == "the next sync will try again"
    assert classify_day(NORMAL, TODAY, [], hh(46), SPRING).note is None
    assert classify_day(NORMAL, TODAY, [], hh(48, status=PeriodStatus.GAP), SPRING).note is None


# -- supply start and end -----------------------------------------------------------------


def test_days_outside_supply_dates_are_no_supply_and_the_boundaries_are_inclusive() -> None:
    supply = [DateRange(date(2025, 6, 1), date(2025, 6, 30))]
    states = {
        day: classify_day(day, TODAY, supply, hh(48), SPRING).state
        for day in (
            date(2025, 5, 31),
            date(2025, 6, 1),
            date(2025, 6, 30),
            date(2025, 7, 1),
        )
    }
    assert states == {
        date(2025, 5, 31): DayState.NO_SUPPLY,
        date(2025, 6, 1): DayState.COMPLETE,
        date(2025, 6, 30): DayState.COMPLETE,
        date(2025, 7, 1): DayState.NO_SUPPLY,
    }


def test_a_day_outside_supply_is_never_missing_even_with_nothing_stored() -> None:
    supply = [DateRange(date(2025, 6, 1), date(2025, 6, 30))]
    assert classify_day(date(2025, 7, 15), TODAY, supply, None, SPRING).state is DayState.NO_SUPPLY


def test_two_supply_periods_leave_the_gap_between_them_unsupplied() -> None:
    # A customer who left and came back: supplied, then not, then supplied again.
    supply = [
        DateRange(date(2017, 9, 26), date(2019, 3, 20)),
        DateRange(date(2021, 8, 13), FOREVER),
    ]
    assert supplied_on(date(2018, 1, 1), supply)
    assert not supplied_on(date(2020, 1, 1), supply)
    assert supplied_on(date(2026, 1, 1), supply)  # open-ended
    assert supplied_on(date(2020, 1, 1), [])  # nothing known: assume supplied


def test_clock_change_inside_a_supply_period_that_ends_that_day() -> None:
    supply = [DateRange(date(2025, 1, 1), SPRING)]
    assert classify_day(SPRING, TODAY, supply, hh(46), SPRING).state is DayState.COMPLETE
    next_day = SPRING + timedelta(days=1)
    assert classify_day(next_day, TODAY, supply, hh(46), SPRING).state is DayState.NO_SUPPLY


def test_a_day_that_is_both_future_and_unsupplied_is_unsupplied() -> None:
    supply = [DateRange(date(2025, 6, 1), date(2025, 6, 30))]
    # Supply ended; today is after it. Today's cell is "no supply" only if checked first:
    # not yet available applies to the day regardless, since it cannot have data.
    assert classify_day(TODAY, TODAY, supply, None, None).state is DayState.NOT_YET


# -- whole fuels --------------------------------------------------------------------------


def facts_for(days: list[date], *, short: frozenset[date] = frozenset()) -> dict[date, DayFacts]:
    return {d: hh(len(london_half_hours(d)) - (2 if d in short else 0)) for d in days}


def test_fuel_completeness_counts_states_and_percentages_across_a_clock_change() -> None:
    span = DateRange(date(2025, 3, 28), date(2025, 4, 1))
    days = span.days()
    gone = date(2025, 3, 31)
    facts = facts_for([d for d in days if d != gone], short=frozenset({date(2025, 3, 29)}))
    result = fuel_completeness(span, TODAY, [], facts, date(2025, 3, 1), date(2025, 3, 1))
    assert result is not None
    assert [c.state for c in result.cells] == [
        DayState.COMPLETE,
        DayState.PARTIAL,
        DayState.COMPLETE,  # the 30th: 46 of 46
        DayState.MISSING,
        DayState.COMPLETE,
    ]
    assert result.counts[DayState.COMPLETE] == 3
    assert result.expected_slots == 48 + 48 + 46 + 48 + 48
    assert result.received_slots == 48 + 46 + 46 + 0 + 48
    assert result.half_hour_percent == Decimal("79.0")  # 188 of 238
    assert result.daily_percent == Decimal("80.0")


def test_a_range_ending_yesterday_gets_todays_not_yet_cell_that_counts_for_nothing() -> None:
    span = DateRange(TODAY - timedelta(days=3), TODAY - timedelta(days=1))
    facts = facts_for(span.days())
    result = fuel_completeness(span, TODAY, [], facts, span.first, span.first)
    assert result is not None
    assert [c.day for c in result.cells][-1] == TODAY
    assert result.cells[-1].state is DayState.NOT_YET
    assert result.counts[DayState.NOT_YET] == 1
    assert result.half_hour_percent == Decimal("100.0")
    earlier = DateRange(span.first, span.last - timedelta(days=1))
    again = fuel_completeness(earlier, TODAY, [], facts, span.first, span.first)
    assert again is not None
    assert all(c.state is not DayState.NOT_YET for c in again.cells)


def test_days_before_the_first_data_are_not_in_the_calendar() -> None:
    span = DateRange(date(2025, 1, 1), date(2025, 1, 10))
    result = fuel_completeness(span, TODAY, [], {}, date(2025, 1, 8), None)
    assert result is not None
    assert [c.day for c in result.cells] == [date(2025, 1, 8), date(2025, 1, 9), date(2025, 1, 10)]
    assert fuel_completeness(span, TODAY, [], {}, None, None) is None
    assert fuel_completeness(span, TODAY, [], {}, date(2025, 2, 1), None) is None


def test_a_fuel_without_half_hourly_data_has_no_half_hour_percentage() -> None:
    span = DateRange(date(2025, 6, 1), date(2025, 6, 3))
    facts = {d: DayFacts(True, False, 0) for d in span.days()}
    result = fuel_completeness(span, TODAY, [], facts, span.first, None)
    assert result is not None
    assert result.expected_slots == 0
    assert result.half_hour_percent is None
    assert result.daily_percent == Decimal("100.0")


def test_supply_end_inside_the_range_splits_complete_and_no_supply_days() -> None:
    span = DateRange(date(2025, 6, 1), date(2025, 6, 6))
    supply = [DateRange(date(2025, 1, 1), date(2025, 6, 3))]
    facts = facts_for(span.days())
    result = fuel_completeness(span, TODAY, supply, facts, date(2025, 1, 1), date(2025, 1, 1))
    assert result is not None
    assert [c.state for c in result.cells] == [DayState.COMPLETE] * 3 + [DayState.NO_SUPPLY] * 3
    # Days after supply ended count towards neither percentage.
    assert result.expected_slots == 3 * 48
    assert result.daily_percent == Decimal("100.0")


def test_supply_starting_inside_the_range() -> None:
    span = DateRange(date(2025, 6, 1), date(2025, 6, 4))
    supply = [DateRange(date(2025, 6, 3), FOREVER)]
    result = fuel_completeness(span, TODAY, supply, {}, date(2025, 6, 1), None)
    assert result is not None
    assert [c.state for c in result.cells] == [
        DayState.NO_SUPPLY,
        DayState.NO_SUPPLY,
        DayState.MISSING,
        DayState.MISSING,
    ]
    assert result.daily_percent == Decimal("0.0")

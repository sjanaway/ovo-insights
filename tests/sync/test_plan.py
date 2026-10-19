import itertools
from datetime import date, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ovo_insights.domain import Fuel, SupplyPoint
from ovo_insights.sync.plan import (
    GAP_HORIZON_DAYS,
    REVISION_DAYS,
    DateRange,
    Granularity,
    Period,
    PeriodStatus,
    active_ranges,
    backfill_ranges,
    classify,
    expected_keys,
    history_start,
    month_end,
    period_last_day,
    period_starts,
    plan_account,
    union,
)

E, G = Fuel.ELECTRICITY, Fuel.GAS
MPAN, MPRN = "1900000000000", "9876543"
TODAY = date(2026, 10, 13)
YESTERDAY = TODAY - timedelta(days=1)

dates = st.dates(min_value=date(2015, 1, 1), max_value=date(2035, 12, 31))


@st.composite
def ranges(draw: st.DrawFn, max_days: int = 900) -> DateRange:
    first = draw(dates)
    return DateRange(first, first + timedelta(days=draw(st.integers(0, max_days))))


def sp(account: str, fuel: Fuel, start: date, end: date | None = None) -> SupplyPoint:
    return SupplyPoint(account, fuel, MPAN if fuel is E else MPRN, start, end)


# -- ranges and periods -------------------------------------------------------------------


def test_date_range_basics() -> None:
    r = DateRange(date(2026, 1, 30), date(2026, 2, 2))

    assert r.days() == [date(2026, 1, 30), date(2026, 1, 31), date(2026, 2, 1), date(2026, 2, 2)]
    assert date(2026, 2, 1) in r
    assert date(2026, 2, 3) not in r
    assert "2026-02-01" not in r
    assert r.clip(date(2026, 2, 1), date(2027, 1, 1)) == DateRange(date(2026, 2, 1), r.last)
    assert r.clip(date(2026, 3, 1), date(2027, 1, 1)) is None
    with pytest.raises(ValueError, match="before it starts"):
        DateRange(date(2026, 1, 2), date(2026, 1, 1))


def test_history_start_is_two_years_back_and_handles_leap_days() -> None:
    assert history_start(TODAY) == date(2024, 10, 13)
    assert history_start(date(2028, 2, 29)) == date(2026, 2, 28)


def test_period_validation_and_last_day() -> None:
    assert Period("a", Granularity.MONTHLY, date(2025, 1, 1)).last == date(2025, 12, 31)
    assert Period("a", Granularity.DAILY, date(2024, 2, 1)).last == date(2024, 2, 29)
    hh = Period("a", Granularity.HALF_HOURLY, date(2024, 2, 7))
    assert hh.range == DateRange(hh.start, hh.start)
    with pytest.raises(ValueError, match="1 January"):
        Period("a", Granularity.MONTHLY, date(2025, 2, 1))
    with pytest.raises(ValueError, match="the 1st"):
        Period("a", Granularity.DAILY, date(2025, 2, 2))


def test_period_starts_newest_first() -> None:
    span = DateRange(date(2024, 12, 30), date(2025, 2, 2))

    assert period_starts(Granularity.MONTHLY, span) == [date(2025, 1, 1), date(2024, 1, 1)]
    assert period_starts(Granularity.DAILY, span) == [
        date(2025, 2, 1),
        date(2025, 1, 1),
        date(2024, 12, 1),
    ]
    assert period_starts(Granularity.HALF_HOURLY, span)[:2] == [date(2025, 2, 2), date(2025, 2, 1)]


@given(ranges(), st.sampled_from(list(Granularity)))
def test_periods_tile_the_range_exactly(span: DateRange, granularity: Granularity) -> None:
    starts = period_starts(granularity, span)
    covered = [
        d
        for s in reversed(starts)
        for d in DateRange(s, period_last_day(granularity, s)).days()
        if d in span
    ]

    assert covered == span.days()  # every day once, in order, nothing outside
    assert starts == sorted(starts, reverse=True)


@given(dates)
def test_month_end(day: date) -> None:
    end = month_end(day)
    assert end.month == day.month
    assert (end + timedelta(days=1)).day == 1


def test_union() -> None:
    assert union([]) is None
    assert union(
        [
            DateRange(date(2025, 3, 1), date(2025, 3, 5)),
            DateRange(date(2025, 1, 1), date(2025, 1, 2)),
        ]
    ) == DateRange(date(2025, 1, 1), date(2025, 3, 5))


# -- active windows -----------------------------------------------------------------------


def test_owner_case_old_account_ends_the_day_before_the_new_one_starts() -> None:
    # Two accounts on the same supply points; Bootstrap gives no end dates.
    accounts = {
        "old": [sp("old", E, date(2017, 9, 26)), sp("old", G, date(2017, 9, 26))],
        "new": [sp("new", E, date(2021, 8, 13)), sp("new", G, date(2021, 8, 13))],
    }

    active = active_ranges(accounts, YESTERDAY)

    assert active["old"] == {
        E: DateRange(date(2017, 9, 26), date(2021, 8, 12)),
        G: DateRange(date(2017, 9, 26), date(2021, 8, 12)),
    }
    assert active["new"][E] == DateRange(date(2021, 8, 13), YESTERDAY)
    # The two-year cap leaves the old account with nothing to fetch.
    assert backfill_ranges(active["old"], TODAY) == {}
    assert backfill_ranges(active["new"], TODAY) == {
        E: DateRange(date(2024, 10, 13), YESTERDAY),
        G: DateRange(date(2024, 10, 13), YESTERDAY),
    }


def test_explicit_end_dates_win_and_are_clipped_to_yesterday() -> None:
    accounts = {
        "a": [sp("a", E, date(2020, 1, 1), date(2025, 6, 30))],
        "b": [sp("b", E, date(2025, 6, 1), date(2030, 1, 1))],
    }

    active = active_ranges(accounts, YESTERDAY)

    # An explicit end is used even though another account starts earlier than it.
    assert active["a"][E] == DateRange(date(2020, 1, 1), date(2025, 6, 30))
    assert active["b"][E] == DateRange(date(2025, 6, 1), YESTERDAY)


def test_accounts_not_started_yet_or_on_other_meters_are_not_clipped_by_each_other() -> None:
    accounts = {
        "future": [sp("future", E, TODAY)],
        "elec": [sp("elec", E, date(2025, 1, 1))],
        "gas": [sp("gas", G, date(2026, 1, 1))],
    }

    active = active_ranges(accounts, YESTERDAY)

    assert active["future"] == {}
    # "future" starts later on the same MPAN, so "elec" is taken to end the day before.
    assert active["elec"][E] == DateRange(date(2025, 1, 1), YESTERDAY)
    assert active["gas"][G] == DateRange(date(2026, 1, 1), YESTERDAY)


def test_several_supply_points_for_one_fuel_span_earliest_to_latest() -> None:
    points = [
        SupplyPoint("a", E, "1111111111111", date(2024, 1, 1), date(2024, 12, 31)),
        SupplyPoint("a", E, "2222222222222", date(2025, 1, 1)),
    ]

    assert active_ranges({"a": points}, YESTERDAY)["a"][E] == DateRange(date(2024, 1, 1), YESTERDAY)


@given(
    starts=st.lists(dates, min_size=1, max_size=4, unique=True),
    yesterday=dates,
)
def test_accounts_sharing_a_meter_never_overlap(starts: list[date], yesterday: date) -> None:
    accounts = {f"a{i}": [sp(f"a{i}", E, start)] for i, start in enumerate(starts)}

    active = active_ranges(accounts, yesterday)

    spans = sorted((r[E] for r in active.values() if E in r), key=lambda r: r.first)
    for earlier, later in itertools.pairwise(spans):
        assert earlier.last < later.first
    for span in spans:
        assert span.last <= yesterday
    # Every day up to yesterday from the first start is covered by exactly one account.
    if spans:
        assert sum(len(s.days()) for s in spans) == (spans[-1].last - spans[0].first).days + 1


# -- planning -----------------------------------------------------------------------------


def _range(first: date, last: date) -> dict[Fuel, DateRange]:
    return {E: DateRange(first, last), G: DateRange(first, last)}


def test_plan_order_and_skipping_final_periods() -> None:
    backfill = _range(date(2026, 9, 29), date(2026, 10, 2))
    states = {
        (Granularity.HALF_HOURLY, date(2026, 10, 1)): PeriodStatus.COMPLETE,
        (Granularity.HALF_HOURLY, date(2026, 9, 30)): PeriodStatus.GAP,
        (Granularity.HALF_HOURLY, date(2026, 9, 29)): PeriodStatus.ERROR,
        (Granularity.DAILY, date(2026, 10, 1)): PeriodStatus.PROVISIONAL,
        (Granularity.DAILY, date(2026, 9, 1)): PeriodStatus.INCOMPLETE,
    }

    plan = plan_account("a", backfill, states)

    assert [(p.granularity.value, p.start.isoformat()) for p in plan] == [
        ("monthly", "2026-01-01"),
        ("daily", "2026-10-01"),
        ("half_hourly", "2026-10-02"),
        ("daily", "2026-09-01"),
        ("half_hourly", "2026-09-29"),  # failed validation last time: retried last
    ]


def test_plan_for_nothing_to_fetch() -> None:
    assert plan_account("a", {}, {}) == []


@given(ranges(max_days=120))
def test_plan_covers_every_day_once_and_daily_comes_before_its_days(span: DateRange) -> None:
    plan = plan_account("a", {E: span}, {})

    days = [p.start for p in plan if p.granularity is Granularity.HALF_HOURLY]
    assert sorted(days) == span.days()
    seen_months: set[date] = set()
    for p in plan:
        if p.granularity is Granularity.DAILY:
            seen_months.add(p.start)
        elif p.granularity is Granularity.HALF_HOURLY:
            assert p.start.replace(day=1) in seen_months


def test_expected_keys() -> None:
    backfill = {
        E: DateRange(date(2026, 9, 29), YESTERDAY),
        G: DateRange(date(2026, 10, 1), YESTERDAY),
    }
    october = DateRange(date(2026, 10, 1), date(2026, 10, 31))

    daily = expected_keys(Granularity.DAILY, october, backfill)
    assert len(daily) == 2 * 12
    assert (E, date(2026, 10, 12)) in daily
    assert (E, date(2026, 10, 13)) not in daily  # today: not available yet

    year = DateRange(date(2026, 1, 1), date(2026, 12, 31))
    assert expected_keys(Granularity.MONTHLY, year, backfill) == {
        (E, date(2026, 9, 1)),
        (E, date(2026, 10, 1)),
        (G, date(2026, 10, 1)),
    }
    assert (
        expected_keys(Granularity.DAILY, DateRange(date(2025, 1, 1), date(2025, 1, 31)), backfill)
        == set()
    )
    with pytest.raises(ValueError, match="daily rows"):
        expected_keys(Granularity.HALF_HOURLY, october, backfill)


@pytest.mark.parametrize(
    ("complete", "days_after", "status"),
    [
        (True, REVISION_DAYS - 1, PeriodStatus.PROVISIONAL),
        (True, REVISION_DAYS, PeriodStatus.COMPLETE),
        (False, GAP_HORIZON_DAYS - 1, PeriodStatus.INCOMPLETE),
        (False, GAP_HORIZON_DAYS, PeriodStatus.GAP),
    ],
)
def test_classify(complete: bool, days_after: int, status: PeriodStatus) -> None:
    last = date(2026, 9, 30)

    assert classify(complete=complete, last_day=last, today=last + timedelta(days_after)) is status

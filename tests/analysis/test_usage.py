from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ovo_insights.analysis.usage import (
    Change,
    DayTotal,
    Grain,
    Preset,
    Summary,
    bucket_end,
    bucket_start,
    buckets,
    compare,
    grain_for,
    household_days,
    is_partial,
    missing_days,
    one_year_earlier,
    pounds_text,
    previous_year,
    recent_unit_rate,
    resolve_range,
    summarise,
    year_on_year,
)
from ovo_insights.domain import DailyUsage, Fuel
from ovo_insights.money import MilliPence
from ovo_insights.sync.plan import DateRange

E, G = Fuel.ELECTRICITY, Fuel.GAS
TODAY = date(2026, 10, 13)


def day_total(
    day: date, kwh: float = 1.0, cost: int | None = 250, standing: int | None = 50_000
) -> DayTotal:
    return DayTotal(
        day,
        kwh,
        None if cost is None else MilliPence(cost),
        None if standing is None else MilliPence(standing),
        MilliPence(25_000),
    )


@st.composite
def day_series(draw: st.DrawFn) -> list[DayTotal]:
    first = draw(st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 1, 1)))
    n = draw(st.integers(0, 120))  # still spans several weeks and months
    present = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    days = []
    for i, keep in enumerate(present):
        if keep:
            days.append(
                day_total(
                    first + timedelta(days=i),
                    draw(st.floats(0, 100, allow_nan=False)),
                    draw(st.one_of(st.none(), st.integers(0, 10**7))),
                    draw(st.one_of(st.none(), st.integers(0, 10**6))),
                )
            )
    return days


# -- ranges -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("preset", "first"),
    [
        (Preset.LAST_30_DAYS, date(2026, 9, 13)),
        (Preset.LAST_12_MONTHS, date(2025, 10, 13)),
        (Preset.THIS_YEAR, date(2026, 1, 1)),
        (Preset.ALL, date(2024, 10, 13)),
    ],
)
def test_presets_end_yesterday(preset: Preset, first: date) -> None:
    span = resolve_range(preset, TODAY, date(2024, 10, 13))

    assert span == DateRange(first, date(2026, 10, 12))


def test_presets_on_awkward_days() -> None:
    # 1 January: "this year" has no complete day yet, so it is just yesterday.
    assert resolve_range(Preset.THIS_YEAR, date(2027, 1, 1), None) == DateRange(
        date(2026, 12, 31), date(2026, 12, 31)
    )
    # No data at all: "all" is just yesterday.
    assert resolve_range(Preset.ALL, TODAY, None) == DateRange(
        date(2026, 10, 12), date(2026, 10, 12)
    )


def test_one_year_earlier_and_previous_year() -> None:
    assert one_year_earlier(date(2028, 2, 29)) == date(2027, 2, 28)
    span = DateRange(date(2028, 2, 28), date(2028, 3, 1))  # 3 days across a leap day
    assert previous_year(span) == DateRange(date(2027, 2, 28), date(2027, 3, 2))


@pytest.mark.parametrize(
    ("days", "grain"),
    [(1, Grain.DAY), (62, Grain.DAY), (63, Grain.WEEK), (400, Grain.WEEK), (401, Grain.MONTH)],
)
def test_grain_for(days: int, grain: Grain) -> None:
    first = date(2025, 1, 1)

    assert grain_for(DateRange(first, first + timedelta(days=days - 1))) is grain


# -- totals -------------------------------------------------------------------------------


def test_household_days_sum_accounts_and_keep_missing_costs_missing() -> None:
    day = date(2026, 10, 1)
    rows = [
        DailyUsage("a", E, day, 2.0, MilliPence(500), True, MilliPence(25_000), MilliPence(50_000)),
        DailyUsage(
            "b", E, day, 6.0, MilliPence(1500), True, MilliPence(29_000), MilliPence(50_000)
        ),
        DailyUsage("a", G, day, 9.0, MilliPence(700), False),
        DailyUsage("a", E, day + timedelta(1), 1.0, None, True),
    ]

    totals = household_days(rows, E)

    assert totals[0] == DayTotal(
        day, 8.0, MilliPence(2000), MilliPence(100_000), MilliPence(28_000)
    )
    assert totals[0].total_cost == 102_000
    assert totals[1].total_cost is None  # unknown, not zero
    assert totals[1].unit_rate is None
    assert [t.day for t in household_days(rows, G)] == [day]


def test_weighted_rate_with_zero_consumption() -> None:
    day = date(2026, 10, 1)
    rows = [DailyUsage("a", E, day, 0.0, MilliPence(0), True, MilliPence(25_000))]

    assert household_days(rows, E)[0].unit_rate == 25_000


@given(day_series(), st.sampled_from(list(Grain)))
def test_bucket_totals_equal_day_totals(days: list[DayTotal], grain: Grain) -> None:
    result = buckets(days, grain)

    assert sum(b.days for b in result) == len(days)
    assert sum(b.kwh for b in result) == pytest.approx(sum(d.kwh for d in days))
    costed = [d for d in days if d.usage_cost is not None]
    assert sum(b.usage_cost for b in result) == sum(d.usage_cost or 0 for d in costed)
    assert sum(b.standing for b in result) == sum(d.standing or 0 for d in costed)
    assert all(bucket_start(b.start, grain) == b.start for b in result)


@given(day_series(), st.integers(0, 120), st.sampled_from(list(Grain)))
def test_totals_are_invariant_to_slicing(days: list[DayTotal], cut: int, grain: Grain) -> None:
    whole = summarise(days)
    left, right = summarise(days[:cut]), summarise(days[cut:])

    assert whole.cost == left.cost + right.cost
    assert whole.days == left.days + right.days
    assert whole.costed_days == left.costed_days + right.costed_days
    assert whole.kwh == pytest.approx(left.kwh + right.kwh)


def test_summary_average_cost_per_day() -> None:
    days = [day_total(date(2026, 10, d), cost=1000, standing=500) for d in (1, 2)]
    days.append(day_total(date(2026, 10, 3), cost=None))

    summary = summarise(days)

    assert summary == Summary(3.0, MilliPence(3000), 3, 2)
    assert summary.average_cost_per_day == 1500
    assert summarise([]).average_cost_per_day is None


def test_compare_like_for_like() -> None:
    span = DateRange(date(2026, 10, 1), date(2026, 10, 3))
    current = [day_total(date(2026, 10, d), kwh=12.0, cost=1100, standing=0) for d in (1, 2, 3)]
    # Last year: 1 Oct missing, so only 2 days are compared.
    previous = [day_total(date(2025, 10, d), kwh=10.0, cost=1000, standing=0) for d in (2, 3)]

    outside = [*current, day_total(date(2026, 11, 2), kwh=99.0)]  # ignored: not in the span
    assert compare(outside, span, previous) == Change(Decimal("20.0"), Decimal("10.0"), 2)
    assert compare(current, span, []) == Change(None, None, 0)
    uncosted = [day_total(date(2025, 10, 2), kwh=10.0, cost=None)]
    assert compare(current, span, uncosted).cost_percent is None


# -- year on year -------------------------------------------------------------------------


def test_year_on_year_by_day() -> None:
    span = DateRange(date(2026, 10, 1), date(2026, 10, 3))
    current = [day_total(date(2026, 10, 1), 1.0), day_total(date(2026, 10, 3), 3.0)]
    previous = [day_total(date(2025, 10, 2), 5.0)]

    yoy = year_on_year(current, previous, span)

    assert yoy.grain is Grain.DAY
    assert yoy.labels == ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert yoy.current == [1.0, None, 3.0]
    assert yoy.previous == [None, 5.0, None]


def test_year_on_year_by_iso_week_including_week_53() -> None:
    # 2026 has an ISO week 53 (28 Dec 2026 - 3 Jan 2027); 2025 doesn't.
    span = DateRange(date(2026, 10, 1), date(2027, 1, 3))
    current = [day_total(span.first + timedelta(days=i), 1.0) for i in range(95)]
    previous = [day_total(date(2025, 9, 1) + timedelta(days=i), 2.0) for i in range(150)]

    yoy = year_on_year(current, previous, span)

    assert yoy.grain is Grain.WEEK
    # 1 Oct 2026 is a Thursday: week 40 is only partly in range, so it's left out.
    assert yoy.labels[0] == "2026-W41"
    assert yoy.labels[-1] == "2026-W53"
    assert yoy.current[0] == 7.0
    assert yoy.previous[0] == 14.0  # last year's ISO week 41
    assert yoy.previous[-1] is None  # no week 53 a year earlier


@pytest.mark.parametrize(
    ("start", "grain", "partial"),
    [
        (date(2026, 9, 28), Grain.WEEK, True),  # Mon 28 Sep: starts before the range
        (date(2026, 10, 5), Grain.WEEK, False),
        (date(2026, 10, 26), Grain.WEEK, True),  # runs past 31 Oct
        (date(2026, 10, 1), Grain.MONTH, False),
        (date(2026, 10, 15), Grain.DAY, False),
    ],
)
def test_is_partial(start: date, grain: Grain, partial: bool) -> None:
    span = DateRange(date(2026, 10, 1), date(2026, 10, 31))

    assert is_partial(start, grain, span) is partial
    assert bucket_end(date(2028, 2, 1), Grain.MONTH) == date(2028, 2, 29)


def test_pounds_text_is_exact() -> None:
    assert pounds_text(None) is None
    assert pounds_text(MilliPence(1_234_565)) == "12.35"  # half-even at the penny
    assert pounds_text(MilliPence(1_234_575)) == "12.35"
    assert pounds_text(MilliPence(5)) == "0.00"


@given(st.integers(-(10**12), 10**12))
def test_pounds_text_never_drifts(milli_pence: int) -> None:
    text = pounds_text(MilliPence(milli_pence))
    assert text is not None
    assert abs(Decimal(text) * 100_000 - milli_pence) <= 500


# -- rates and completeness ---------------------------------------------------------------


def test_recent_unit_rate() -> None:
    days = [
        DayTotal(date(2026, 1, 1), 10.0, None, None, MilliPence(20_000)),
        DayTotal(date(2026, 1, 2), 30.0, None, None, MilliPence(28_000)),
        DayTotal(date(2026, 1, 3), 5.0, None, None, None),
    ]

    assert recent_unit_rate(days) == 26_000  # kWh-weighted
    assert recent_unit_rate(days, window=1) == 28_000
    assert recent_unit_rate([]) is None
    assert recent_unit_rate([DayTotal(date(2026, 1, 1), 0.0, None, None, MilliPence(1))]) == 1


def test_missing_days() -> None:
    span = DateRange(date(2026, 10, 1), date(2026, 10, 10))
    present = {E: set(span.days()) - {date(2026, 10, 4)}, G: set(span.days()[5:])}
    earliest = {E: date(2024, 1, 1), G: date(2026, 10, 6)}  # gas supply began on the 6th

    missing = missing_days(span, present, earliest, [date(2026, 10, 7), date(2026, 9, 1)])

    assert missing == {date(2026, 10, 4), date(2026, 10, 7)}
    assert missing_days(span, {}, {E: date(2027, 1, 1)}) == set()


def test_empty_inputs() -> None:
    span = DateRange(date(2026, 10, 1), date(2026, 10, 1))

    assert buckets([], Grain.MONTH) == []
    assert summarise([]) == Summary(0, MilliPence(0), 0, 0)
    assert year_on_year([], [], span).current == [None]

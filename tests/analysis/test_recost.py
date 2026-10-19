"""Re-costing: hand-calculated golden cases, DST days, properties, and like-for-like totals."""

from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from hypothesis import given
from hypothesis import strategies as st

from ovo_insights.analysis.recost import (
    Column,
    CostingDay,
    DayCost,
    Skip,
    actual_cost,
    columns_for,
    compare,
    cost_in_column,
    penny_round,
    recost_day,
)
from ovo_insights.domain import Fuel
from ovo_insights.money import MilliPence
from ovo_insights.tariffs import ALL_WEEKDAYS, Band, BandDraft, Tariff, TariffDraft, build_tariff
from ovo_insights.timeutil import LONDON, london_half_hours

EVERY_DAY = tuple(range(7))
M = MilliPence


def whole_day(day: date, kwh_per_slot: float = 1.0) -> dict[datetime, float]:
    return {start: kwh_per_slot for start in london_half_hours(day)}


def costing(
    day: date,
    kwh: float | None = 10.0,
    usage: int | None = None,
    standing: int | None = 53_680,
    half_hours: dict[datetime, float] | None = None,
) -> CostingDay:
    return CostingDay(
        day,
        kwh,
        None if usage is None else M(usage),
        None if standing is None else M(standing),
        half_hours,
    )


def tariff(name: str, *bands: BandDraft, standing: str = "50", fuel: str = "electricity") -> Tariff:
    return build_tariff(TariffDraft(name, fuel, standing, "", "", bands))


ECONOMY7 = tariff(
    "Economy 7",
    BandDraft("00:30", "07:30", "10", EVERY_DAY),
    BandDraft("07:30", "00:30", "30", EVERY_DAY),
)
EV = tariff(
    "EV",
    BandDraft("00:00", "05:00", "7", (0, 1, 2, 3, 4)),
    BandDraft("05:00", "16:00", "28", (0, 1, 2, 3, 4)),
    BandDraft("16:00", "19:00", "40", (0, 1, 2, 3, 4)),
    BandDraft("19:00", "00:00", "28", (0, 1, 2, 3, 4)),
    BandDraft("00:00", "00:00", "15", (5, 6)),
)


def usage_of(result: DayCost | Skip) -> int:
    assert isinstance(result, DayCost)
    return result.usage


# -- golden cases (worked out by hand) ----------------------------------------------------


def test_flat_rate_is_daily_kwh_times_rate_rounded_to_the_penny_plus_exact_standing() -> None:
    flat = Tariff.flat("Flat", Fuel.ELECTRICITY, M(24_500), M(53_680))
    # 10 kWh x 24.5 p = 245 p exactly.
    assert recost_day(flat, costing(date(2026, 1, 14), 10.0)) == DayCost(M(245_000), M(53_680))
    # 7.777 kWh x 24.5 p = 190.5365 p -> 191 p (half-even only matters at exactly .5).
    assert recost_day(flat, costing(date(2026, 1, 14), 7.777)).usage == 191_000  # type: ignore[union-attr]
    # Exactly half a penny rounds to the even penny: 2 x 24.25 p = 48.5 p -> 48 p, and
    # 3 x 24.5 p = 73.5 p -> 74 p.
    half = Tariff.flat("Half", Fuel.ELECTRICITY, M(24_250), M(0))
    assert usage_of(recost_day(half, costing(date(2026, 1, 14), 2.0))) == 48_000
    assert (
        usage_of(
            recost_day(Tariff.flat("x", Fuel.GAS, M(24_500), M(0)), costing(date(2026, 1, 14), 3.0))
        )
        == 74_000
    )
    assert DayCost(M(245_000), M(53_680)).total == 298_680


def test_a_flat_tariff_uses_the_daily_figure_even_when_half_hours_exist() -> None:
    flat = Tariff.flat("Flat", Fuel.ELECTRICITY, M(10_000), M(0))
    day = costing(date(2026, 1, 14), 5.0, half_hours=whole_day(date(2026, 1, 14), 0.2))  # 9.6 kWh
    assert usage_of(recost_day(flat, day)) == 50_000


def test_gas_costs_from_daily_kwh_only() -> None:
    gas = tariff(
        "Gas", BandDraft("00:00", "00:00", "5.55", EVERY_DAY), standing="23.95", fuel="gas"
    )
    result = recost_day(gas, costing(date(2026, 1, 14), 40.0, half_hours=None))
    assert result == DayCost(M(222_000), M(23_950))  # 40 x 5.55 p = 222 p


def test_economy_7_two_bands() -> None:
    # Wednesday 14 Jan 2026 (UTC = UK time). Night 00:30-07:30 is 14 slots at 0.2 kWh and
    # 10 p: 2.8 kWh = 28 p. Day is the other 34 slots at 0.3 kWh and 30 p: 10.2 kWh = 306 p.
    day = date(2026, 1, 14)
    slots = {
        start: (0.2 if 1 <= i <= 14 else 0.3) for i, start in enumerate(london_half_hours(day))
    }
    result = recost_day(ECONOMY7, costing(day, 13.0, half_hours=slots))
    assert result == DayCost(M(334_000), M(50_000))
    assert result.total == 384_000


def test_four_band_ev_tariff_with_a_separate_weekend_rate() -> None:
    # 1 kWh in all 48 slots. Weekday: 10 slots x 7 p + 22 x 28 p + 6 x 40 p + 10 x 28 p = 1,206 p.
    weekday, weekend = date(2026, 1, 14), date(2026, 1, 17)  # Wednesday, Saturday
    assert (
        usage_of(recost_day(EV, costing(weekday, 48.0, half_hours=whole_day(weekday)))) == 1_206_000
    )
    # Weekend: 48 x 15 p = 720 p.
    assert (
        usage_of(recost_day(EV, costing(weekend, 48.0, half_hours=whole_day(weekend)))) == 720_000
    )


def test_spring_forward_day_has_46_slots_and_skips_the_missing_hour() -> None:
    # Sunday 29 March 2026: 01:00-02:00 does not exist. Present night slots (00:30-07:30) are
    # 00:30 plus 02:00..07:00, i.e. 1 + 11 = 12; the other 34 slots are the day band.
    day = date(2026, 3, 29)
    assert len(london_half_hours(day)) == 46
    result = recost_day(ECONOMY7, costing(day, 46.0, half_hours=whole_day(day)))
    assert usage_of(result) == 12 * 10_000 + 34 * 30_000  # 120 p + 1,020 p = 1,140 p


def test_clock_back_day_has_50_slots_and_both_01_00_hours_get_the_same_band() -> None:
    # Sunday 26 October 2025: 01:00-02:00 happens twice. Night slots are 14 + the repeated
    # 01:00 and 01:30 = 16; the other 34 are day.
    day = date(2025, 10, 26)
    assert len(london_half_hours(day)) == 50
    result = recost_day(ECONOMY7, costing(day, 50.0, half_hours=whole_day(day)))
    assert usage_of(result) == 16 * 10_000 + 34 * 30_000  # 160 p + 1,020 p = 1,180 p


def test_a_time_of_day_tariff_needs_every_half_hour() -> None:
    assert (
        recost_day(ECONOMY7, costing(date(2026, 1, 14), 10.0, half_hours=None))
        is Skip.NO_HALF_HOURS
    )
    assert recost_day(ECONOMY7, costing(date(2026, 1, 14), None)) is Skip.NO_DAILY
    flat = Tariff.flat("Flat", Fuel.GAS, M(1), M(0))
    assert recost_day(flat, costing(date(2026, 1, 14), None)) is Skip.NO_DAILY


def test_penny_rounding_is_half_even() -> None:
    assert [penny_round(Decimal(x)) for x in (499, 500, 501, 1_499, 1_500, 1_501, 2_500)] == [
        0, 0, 1_000, 1_000, 2_000, 2_000, 2_000,
    ]  # fmt: skip


# -- properties ---------------------------------------------------------------------------


kwh_values = st.floats(min_value=0, max_value=200, allow_nan=False, allow_infinity=False).map(
    lambda v: round(v, 3)
)


@given(
    days=st.lists(kwh_values, min_size=1, max_size=40),
    rate=st.integers(min_value=0, max_value=500_000),
    standing=st.integers(min_value=0, max_value=1_000_000),
)
def test_flat_rate_total_is_rate_times_kwh_plus_standing_times_days(
    days: list[float], rate: int, standing: int
) -> None:
    flat = Tariff.flat("Flat", Fuel.ELECTRICITY, M(rate), M(standing))
    start = date(2026, 1, 1)
    inputs = [costing(start + timedelta(days=i), kwh, 0, 0) for i, kwh in enumerate(days)]
    costs = [recost_day(flat, d) for d in inputs]
    assert all(isinstance(c, DayCost) for c in costs)
    usage = sum(c.usage for c in costs if isinstance(c, DayCost))
    total = sum(c.total for c in costs if isinstance(c, DayCost))
    # Each day is rounded to a whole penny as OVO does, so the exact formula
    # (rate x total kWh + standing x days) is matched to within half a penny a day.
    exact = sum(Decimal(str(k)) for k in days) * rate
    assert abs(Decimal(usage) - exact) <= 500 * len(days)
    assert total == usage + standing * len(days)
    assert all(c.usage % 1_000 == 0 for c in costs if isinstance(c, DayCost))
    # And exactly equal when each day is costed on its own.
    assert usage == sum(
        int((Decimal(str(k)) * rate / 1000).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)) * 1000
        for k in days
    )


@given(
    day=st.dates(min_value=date(2015, 1, 1), max_value=date(2035, 12, 31)),
    split=st.integers(min_value=1, max_value=47),
    values=st.lists(kwh_values, min_size=50, max_size=50),
)
def test_two_band_cost_matches_an_independent_wall_clock_calculation(
    day: date, split: int, values: list[float]
) -> None:
    """On any date, including both clock changes, a slot costs what the wall clock says."""
    cheap, dear = M(11_250), M(31_500)
    two_bands = Tariff(
        "T",
        Fuel.ELECTRICITY,
        M(0),
        (Band(0, split, cheap, ALL_WEEKDAYS), Band(split, 48, dear, ALL_WEEKDAYS)),
    )
    starts = london_half_hours(day)
    slots = {start: values[i] for i, start in enumerate(starts)}
    expected = Decimal(0)
    for start, kwh in slots.items():
        local = start.astimezone(LONDON)
        wall_slot = local.hour * 2 + local.minute // 30
        expected += Decimal(str(kwh)) * (cheap if wall_slot < split else dear)
    assert usage_of(recost_day(two_bands, costing(day, sum(slots.values()), half_hours=slots))) == (
        penny_round(expected)
    )


@given(st.dates(min_value=date(2015, 1, 1), max_value=date(2035, 12, 31)))
def test_the_weekday_comes_from_the_local_date_of_the_slot(day: date) -> None:
    """Only one weekday is priced differently: every slot of that day, and no other, gets it."""
    weekday = day.weekday()
    other = tuple(d for d in range(7) if d != weekday)
    special = tariff(
        "S",
        BandDraft("00:00", "00:00", "50", (weekday,)),
        BandDraft("00:00", "00:00", "1", other),
    )
    slots = whole_day(day)
    assert usage_of(recost_day(special, costing(day, 0.0, half_hours=slots))) == (
        len(slots) * 50_000
    )
    for shifted in (day - timedelta(days=1), day + timedelta(days=1)):
        slots = whole_day(shifted)
        assert usage_of(recost_day(special, costing(shifted, 0.0, half_hours=slots))) == (
            len(slots) * 1_000
        )


# -- columns and the baseline -------------------------------------------------------------


def version(name: str, rate: int, start: date | None, end: date | None) -> Tariff:
    return Tariff.flat(name, Fuel.ELECTRICITY, M(rate), M(50_000), start, end)


def test_versions_of_a_name_make_one_column_in_date_order() -> None:
    later = version("Cap", 30_000, date(2026, 4, 1), None)
    earlier = version("cap", 25_000, None, date(2026, 3, 31))
    other = version("Fixed", 20_000, None, None)
    gas = Tariff.flat("Cap", Fuel.GAS, M(5), M(5))
    columns = columns_for([later, other, gas, earlier], Fuel.ELECTRICITY)
    assert [(c.name, len(c.versions)) for c in columns] == [("Cap", 2), ("Fixed", 1)]
    assert columns[0].versions == (earlier, later)
    assert columns[0].version_on(date(2026, 3, 31)) is earlier
    assert columns[0].version_on(date(2026, 4, 1)) is later
    assert [c.name for c in columns_for([gas], Fuel.GAS)] == ["Cap"]
    assert columns_for([], Fuel.GAS) == []


def test_a_day_outside_every_version_cant_be_costed() -> None:
    column = Column(
        "Cap", Fuel.ELECTRICITY, (version("Cap", 25_000, date(2026, 1, 1), date(2026, 1, 31)),)
    )
    assert cost_in_column(column, costing(date(2026, 2, 1))) is Skip.OUTSIDE_TARIFF
    assert isinstance(cost_in_column(column, costing(date(2026, 1, 31))), DayCost)


def test_the_baseline_is_ovos_cost_plus_standing_and_unknowns_are_not_zero() -> None:
    day = date(2026, 1, 14)
    assert actual_cost(costing(day, 10.0, usage=245_000, standing=53_680)) == DayCost(
        M(245_000), M(53_680)
    )
    assert actual_cost(costing(day, 10.0, usage=245_000, standing=None)) is Skip.NO_ACTUAL
    assert actual_cost(costing(day, 10.0, usage=None, standing=53_680)) is Skip.NO_ACTUAL
    assert actual_cost(costing(day, None, usage=245_000)) is Skip.NO_DAILY


def test_ovos_own_rates_reproduce_ovos_own_figures_to_the_penny() -> None:
    rate, standing = 24_440, 47_840
    own = Tariff.flat("OVO", Fuel.ELECTRICITY, M(rate), M(standing))
    start = date(2026, 1, 1)
    days = []
    for i, kwh in enumerate([9.732, 12.014, 0.0, 31.5, 7.777, 15.265]):
        cost = (
            int((Decimal(str(kwh)) * rate / 1000).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
            * 1000
        )
        days.append(costing(start + timedelta(days=i), kwh, cost, standing))
    result = compare(days, [Column("OVO", Fuel.ELECTRICITY, (own,))])
    [column] = result.columns
    assert column.difference == 0
    assert column.difference_percent == Decimal("0.0")
    assert column.totals.total == result.actual.total


def test_comparison_totals_differences_percentages_and_months() -> None:
    # Actual: 10 kWh at 25 p = 250 p + 50 p standing, on two days in January and one in February.
    days = [
        costing(date(2026, 1, 30), 10.0, 250_000, 50_000),
        costing(date(2026, 1, 31), 10.0, 250_000, 50_000),
        costing(date(2026, 2, 1), 10.0, 250_000, 50_000),
    ]
    cheaper = Tariff.flat("Cheaper", Fuel.ELECTRICITY, M(20_000), M(40_000))  # 200 + 40 = 240 p/day
    dearer = Tariff.flat("Dearer", Fuel.ELECTRICITY, M(30_000), M(60_000))  # 300 + 60 = 360 p/day
    result = compare(
        days,
        [
            Column("Cheaper", Fuel.ELECTRICITY, (cheaper,)),
            Column("Dearer", Fuel.ELECTRICITY, (dearer,)),
        ],
    )
    assert result.days_considered == 3
    assert result.included == tuple(d.day for d in days)
    assert result.excluded == ()
    assert result.actual.total == 900_000
    assert result.actual.by_month == {date(2026, 1, 1): 600_000, date(2026, 2, 1): 300_000}
    low, high = result.columns
    assert (low.totals.usage, low.totals.standing, low.totals.total) == (600_000, 120_000, 720_000)
    assert (low.difference, low.difference_percent) == (-180_000, Decimal("-20.0"))
    assert (high.difference, high.difference_percent) == (180_000, Decimal("20.0"))
    assert low.totals.by_month == {date(2026, 1, 1): 480_000, date(2026, 2, 1): 240_000}
    assert low.totals.usage_by_month == {date(2026, 1, 1): 400_000, date(2026, 2, 1): 200_000}
    assert low.totals.standing_by_month == {date(2026, 1, 1): 80_000, date(2026, 2, 1): 40_000}
    assert result.actual.usage_by_month == {date(2026, 1, 1): 500_000, date(2026, 2, 1): 250_000}
    assert sum(high.totals.by_month.values()) == high.totals.total


def test_a_day_any_column_cant_cost_leaves_every_column_and_the_baseline() -> None:
    flat = Tariff.flat("Flat", Fuel.ELECTRICITY, M(20_000), M(40_000))
    day1, day2, day3, day4, day5, day6 = (date(2026, 1, d) for d in range(1, 7))
    days = [
        costing(day1, 10.0, 250_000, 50_000, whole_day(day1)),  # fine for everything
        costing(day2, 10.0, 250_000, 50_000, None),  # no half-hours: Economy 7 can't
        costing(day3, None, None, None),  # no daily figure
        costing(day4, 10.0, 250_000, None, whole_day(day4)),  # OVO gave no standing charge
        costing(day5, 10.0, 250_000, 50_000, whole_day(day5)),  # fine
        costing(day6, 10.0, 250_000, 50_000, whole_day(day6)),  # outside the dated tariff
    ]
    dated = version("Dated", 20_000, None, date(2026, 1, 5))
    result = compare(
        days,
        [
            Column("Flat", Fuel.ELECTRICITY, (flat,)),
            Column("Economy 7", Fuel.ELECTRICITY, (ECONOMY7,)),
            Column("Dated", Fuel.ELECTRICITY, (dated,)),
        ],
    )
    assert result.included == (day1, day5)
    assert result.excluded == (
        (day2, Skip.NO_HALF_HOURS),
        (day3, Skip.NO_DAILY),
        (day4, Skip.NO_ACTUAL),
        (day6, Skip.OUTSIDE_TARIFF),
    )
    assert result.excluded_counts == {
        Skip.NO_DAILY: 1,
        Skip.NO_ACTUAL: 1,
        Skip.NO_HALF_HOURS: 1,
        Skip.OUTSIDE_TARIFF: 1,
    }
    # Every column covers exactly the two included days: same days, so the totals compare.
    assert result.actual.total == 2 * 300_000
    assert result.columns[0].totals.total == 2 * 240_000
    assert all(c.totals.by_month.keys() == {date(2026, 1, 1)} for c in result.columns)


def test_the_reason_reported_is_the_first_in_the_order_checked() -> None:
    day = date(2026, 1, 1)
    dated = version("Dated", 1, None, date(2025, 12, 31))
    # No daily figure beats a half-hours or dates problem; no baseline beats those too.
    result = compare(
        [costing(day, None, None, None)],
        [Column("E7", Fuel.ELECTRICITY, (ECONOMY7,)), Column("Dated", Fuel.ELECTRICITY, (dated,))],
    )
    assert result.excluded == ((day, Skip.NO_DAILY),)
    result = compare(
        [costing(day, 5.0, 1_000, None)],
        [Column("E7", Fuel.ELECTRICITY, (ECONOMY7,))],
    )
    assert result.excluded == ((day, Skip.NO_ACTUAL),)
    result = compare(
        [costing(day, 5.0, 1_000, 1_000)],
        [Column("E7", Fuel.ELECTRICITY, (ECONOMY7,)), Column("Dated", Fuel.ELECTRICITY, (dated,))],
    )
    assert result.excluded == ((day, Skip.NO_HALF_HOURS),)


def test_a_price_change_mid_range_is_one_column() -> None:
    before = version("Cap", 20_000, None, date(2026, 1, 31))
    after = version("Cap", 30_000, date(2026, 2, 1), None)
    days = [
        costing(date(2026, 1, 31), 10.0, 250_000, 50_000),
        costing(date(2026, 2, 1), 10.0, 250_000, 50_000),
    ]
    result = compare(days, columns_for([after, before], Fuel.ELECTRICITY))
    [column] = result.columns
    assert column.totals.usage == 200_000 + 300_000
    assert column.totals.by_month == {date(2026, 1, 1): 250_000, date(2026, 2, 1): 350_000}


def test_no_columns_or_no_days_or_zero_actual() -> None:
    day = costing(date(2026, 1, 1), 10.0, 250_000, 50_000)
    only_actual = compare([day], [])
    assert only_actual.columns == ()
    assert only_actual.actual.total == 300_000
    nothing = compare([], [Column("x", Fuel.GAS, (Tariff.flat("x", Fuel.GAS, M(1), M(1)),))])
    assert (nothing.days_considered, nothing.included, nothing.actual.total) == (0, (), 0)
    assert nothing.columns[0].difference_percent is None  # nothing was actually paid
    assert nothing.excluded_counts == {}


def test_everything_excluded_leaves_zero_totals() -> None:
    flat = Tariff.flat("F", Fuel.GAS, M(1), M(1))
    result = compare([costing(date(2026, 1, 1), None)], [Column("F", Fuel.GAS, (flat,))])
    assert result.included == ()
    assert result.actual.total == 0
    assert result.columns[0].totals.total == 0
    assert result.excluded_counts == {Skip.NO_DAILY: 1}


def test_clock_change_days_count_as_whole_days_in_a_comparison() -> None:
    spring, normal, autumn = date(2026, 3, 29), date(2026, 3, 30), date(2025, 10, 26)
    days = [
        costing(d, float(len(london_half_hours(d))), 100_000, 50_000, whole_day(d))
        for d in (autumn, spring, normal)
    ]
    result = compare(days, [Column("E7", Fuel.ELECTRICITY, (ECONOMY7,))])
    assert result.excluded == ()
    assert set(result.columns[0].totals.by_month) == {date(2025, 10, 1), date(2026, 3, 1)}

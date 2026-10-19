"""Derived consumption between readings, and the checks against smart data."""

from datetime import date, timedelta
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from ovo_insights.analysis.readings import (
    CALORIFIC_RANGE,
    PROBLEM_VERDICTS,
    RegisterSeries,
    Verdict,
    series_order,
    sort_series,
    summarise_windows,
    windows_of,
)
from ovo_insights.domain import GAS_REGISTER_ID, Fuel, Reading

START = date(2026, 1, 1)
SERIAL = "TESTMETER01"


def reading(day: date, value: str, register: str = "1", serial: str = SERIAL) -> Reading:
    return Reading(serial, register, day, Decimal(value), "SMART_METER", "INLIFE", "ACTUAL")


def series(
    values: list[tuple[date, str]],
    *,
    fuel: Fuel = Fuel.ELECTRICITY,
    unit: str | None = "KWH",
    registers: int = 1,
    status: str | None = "Active",
) -> RegisterSeries:
    register = "1" if fuel is Fuel.ELECTRICITY else GAS_REGISTER_ID
    return RegisterSeries(
        fuel,
        SERIAL,
        register,
        status,
        unit,
        registers,
        tuple(reading(d, v, register) for d, v in values),
    )


def smart(first: date, kwh: list[float]) -> dict[date, float]:
    return {first + timedelta(days=i): v for i, v in enumerate(kwh)}


def one(
    s: RegisterSeries, data: dict[date, float], smart_from: date | None = START
) -> "tuple[Verdict, object]":
    [window] = windows_of(s, data, smart_from)
    return window.verdict, window


# -- electricity --------------------------------------------------------------------------


def test_electricity_window_matches_the_smart_total_for_the_same_days() -> None:
    s = series([(START, "100.000"), (START + timedelta(days=3), "130.500")])
    [w] = windows_of(s, smart(START, [10.0, 10.5, 10.0, 99.0]), START)
    assert (w.start, w.end, w.days) == (START, START + timedelta(days=3), 3)
    assert w.derived_kwh == Decimal("30.500")
    assert w.smart_kwh == Decimal("30.5")  # the 4th day is after the window
    assert w.smart_days == 3
    assert w.verdict is Verdict.MATCH
    assert w.difference_kwh == 0
    assert w.difference_percent == Decimal("0.0")


def test_difference_just_over_five_percent_is_flagged_and_just_under_is_not() -> None:
    day2 = START + timedelta(days=1)
    over = windows_of(series([(START, "0"), (day2, "10.6")]), smart(START, [10.0]), START)[0]
    assert over.verdict is Verdict.MISMATCH
    assert over.difference_percent == Decimal("6.0")
    exactly = windows_of(series([(START, "0"), (day2, "10.5")]), smart(START, [10.0]), START)[0]
    assert exactly.verdict is Verdict.MATCH  # exactly 5% is not "over 5%"
    under = windows_of(series([(START, "0"), (day2, "10.4")]), smart(START, [10.0]), START)[0]
    assert under.verdict is Verdict.MATCH


def test_a_big_percentage_on_a_tiny_window_is_not_flagged_below_the_floor() -> None:
    day2 = START + timedelta(days=1)
    [w] = windows_of(series([(START, "0"), (day2, "0.590")]), smart(START, [0.5]), START)
    assert w.difference_percent == Decimal("18.0")  # over 5%, but only 0.09 kWh
    assert w.verdict is Verdict.MATCH


def test_difference_under_the_floor_is_ignored() -> None:
    day2 = START + timedelta(days=1)
    [w] = windows_of(series([(START, "0"), (day2, "0.090")]), smart(START, [0.0]), START)
    assert w.smart_kwh == 0
    assert w.difference_percent is None  # nothing to take a percentage of
    assert w.verdict is Verdict.MATCH
    [bigger] = windows_of(series([(START, "0"), (day2, "0.100")]), smart(START, [0.0]), START)
    assert bigger.verdict is Verdict.MISMATCH


def test_a_reading_that_goes_down_is_never_compared() -> None:
    day2 = START + timedelta(days=1)
    [w] = windows_of(series([(START, "100"), (day2, "90")]), smart(START, [10.0]), START)
    assert w.verdict is Verdict.WENT_DOWN
    assert w.change == -10
    assert w.derived_kwh is None


def test_unknown_or_missing_units_are_unsupported() -> None:
    day2 = START + timedelta(days=1)
    for unit in (None, "", "MWH", "SCFM"):
        s = series([(START, "1"), (day2, "2")], unit=unit)
        assert windows_of(s, smart(START, [1.0]), START)[0].verdict is Verdict.UNSUPPORTED_UNIT
    # A kWh register on a gas meter (or the reverse) is not understood either.
    gas = series([(START, "1"), (day2, "2")], fuel=Fuel.GAS, unit="KWH")
    assert windows_of(gas, smart(START, [1.0]), START)[0].verdict is Verdict.UNSUPPORTED_UNIT


def test_units_are_case_and_space_insensitive() -> None:
    day2 = START + timedelta(days=1)
    s = series([(START, "0"), (day2, "5")], unit=" kwh ")
    assert windows_of(s, smart(START, [5.0]), START)[0].verdict is Verdict.MATCH


def test_a_meter_with_several_registers_is_not_compared() -> None:
    day2 = START + timedelta(days=1)
    s = series([(START, "0"), (day2, "5")], registers=2)
    [w] = windows_of(s, smart(START, [5.0]), START)
    assert w.verdict is Verdict.SEVERAL_REGISTERS
    assert w.derived_kwh == 5


def test_windows_before_smart_data_are_not_a_problem_but_gaps_after_it_are() -> None:
    d1, d2, d3 = START, START + timedelta(days=1), START + timedelta(days=3)
    s = series([(d1, "0"), (d2, "5"), (d3, "11")])
    # Smart data starts on d2: the first window ends on that day, so it predates the data.
    first, *_ = windows_of(s, smart(d2, [6.0, 0.0]), d2)
    assert first.verdict is Verdict.NO_SMART_DATA
    assert first.smart_kwh is None
    assert first.verdict not in PROBLEM_VERDICTS
    # Two days, one of them missing: a gap in data we should have.
    s2 = series([(d2, "5"), (d3, "11")])
    [partial] = windows_of(s2, smart(d2, [6.0]), d2)
    assert partial.verdict is Verdict.PARTIAL_SMART_DATA
    assert (partial.smart_days, partial.days) == (1, 2)
    assert partial.verdict in PROBLEM_VERDICTS
    # No smart data at all for a fuel: nothing could be compared, and nothing is missing.
    [none] = windows_of(s2, {}, None)
    assert none.verdict is Verdict.NO_SMART_DATA


def test_a_wholly_missing_day_inside_the_smart_era_is_a_gap_not_absence() -> None:
    day2 = START + timedelta(days=1)
    s = series([(START, "0"), (day2, "9")])
    [w] = windows_of(s, {}, START - timedelta(days=30))
    assert w.verdict is Verdict.PARTIAL_SMART_DATA
    assert w.smart_days == 0


def test_ends_in_selects_windows_by_their_end_date() -> None:
    days = [START + timedelta(days=i) for i in range(5)]
    s = series([(d, str(i * 10)) for i, d in enumerate(days)])
    data = smart(START, [10.0] * 4)
    found = windows_of(s, data, START, ends_in=(days[2], days[3]))
    assert [w.end for w in found] == [days[2], days[3]]
    assert len(windows_of(s, data, START)) == 4


def test_a_single_reading_has_no_windows() -> None:
    assert windows_of(series([(START, "5")]), {}, START) == []


@given(
    steps=st.lists(st.integers(min_value=0, max_value=50_000), min_size=1, max_size=40),
)
def test_windows_telescope_and_match_when_the_smart_data_agrees(steps: list[int]) -> None:
    """Consecutive windows partition the readings, so their changes sum to last - first, and
    smart data built from the same steps matches in every window."""
    days = [START + timedelta(days=i) for i in range(len(steps) + 1)]
    values = [Decimal(1000)]
    for step in steps:
        values.append(values[-1] + Decimal(step) / 1000)
    s = RegisterSeries(
        Fuel.ELECTRICITY,
        SERIAL,
        "1",
        "Active",
        "KWH",
        1,
        tuple(reading(d, format(v, "f")) for d, v in zip(days, values, strict=True)),
    )
    data = {d: step / 1000 for d, step in zip(days, steps, strict=False)}
    windows = windows_of(s, data, START)
    assert len(windows) == len(steps)
    assert sum(w.change for w in windows) == values[-1] - values[0]
    assert all(w.verdict is Verdict.MATCH for w in windows)
    assert [w.end for w in windows] == days[1:]


# -- gas ----------------------------------------------------------------------------------


def gas_series(unit: str = "SCMH") -> RegisterSeries:
    return series(
        [(START, "1000.000"), (START + timedelta(days=30), "1100.000")], fuel=Fuel.GAS, unit=unit
    )


def kwh_for(volume: str, calorific_value: str) -> float:
    return float(Decimal(volume) * Decimal("1.02264") * Decimal(calorific_value) / Decimal("3.6"))


def test_gas_implied_calorific_value_inverts_the_bills_formula() -> None:
    data = smart(START, [kwh_for("100", "39.5") / 30] * 30)
    [w] = windows_of(gas_series(), data, START)
    assert w.volume_m3 == Decimal("100")
    assert w.derived_kwh is None  # no single figure converts gas, so none is derived
    assert w.implied_calorific_value == Decimal("39.50")
    assert w.verdict is Verdict.MATCH
    assert w.difference_percent is None


def test_gas_is_flagged_outside_the_range_on_the_bill() -> None:
    low, high = CALORIFIC_RANGE
    for calorific_value, verdict in (
        (str(low), Verdict.MATCH),
        (str(high), Verdict.MATCH),
        ("37.4", Verdict.MISMATCH),
        ("43.1", Verdict.MISMATCH),
    ):
        data = smart(START, [kwh_for("100", calorific_value) / 30] * 30)
        [w] = windows_of(gas_series(), data, START)
        assert w.verdict is verdict, calorific_value


def test_imperial_gas_registers_use_the_bills_2_83_factor() -> None:
    # 100 register units of an imperial meter are 283 m3 of gas.
    data = smart(START, [kwh_for("283", "40.0") / 30] * 30)
    [w] = windows_of(gas_series("SCFH"), data, START)
    assert w.volume_m3 == Decimal("283.00")
    assert w.implied_calorific_value == Decimal("40.00")
    assert w.verdict is Verdict.MATCH


def test_gas_with_no_volume_matches_only_when_the_smart_data_is_also_nil() -> None:
    s = series([(START, "50"), (START + timedelta(days=2), "50")], fuel=Fuel.GAS, unit="SCMH")
    assert windows_of(s, smart(START, [0.0, 0.0]), START)[0].verdict is Verdict.MATCH
    flagged = windows_of(s, smart(START, [5.0, 5.0]), START)[0]
    assert flagged.verdict is Verdict.MISMATCH
    assert flagged.implied_calorific_value is None


def test_gas_reading_that_goes_down_and_missing_data_are_not_compared() -> None:
    down = series([(START, "50"), (START + timedelta(days=2), "40")], fuel=Fuel.GAS, unit="SCMH")
    assert windows_of(down, smart(START, [1.0, 1.0]), START)[0].verdict is Verdict.WENT_DOWN
    data = smart(START, [3.0] * 29)  # a day short
    assert windows_of(gas_series(), data, START)[0].verdict is Verdict.PARTIAL_SMART_DATA


# -- ordering and summary -----------------------------------------------------------------


def test_ordering_puts_electricity_first_and_active_meters_before_removed() -> None:
    old = series([(date(2019, 1, 1), "1")], status="Removed")
    new = series([(date(2024, 1, 1), "1")])
    gas = series([(date(2020, 1, 1), "1")], fuel=Fuel.GAS, unit="SCMH")
    empty = RegisterSeries(Fuel.ELECTRICITY, "EMPTYMETER", "1", None, "KWH", 1, ())
    assert sort_series([old, gas, empty, new]) == [new, old, gas]
    assert series_order(new) < series_order(old) < series_order(gas)


def test_summary_counts_compared_flagged_and_not_compared() -> None:
    days = [START + timedelta(days=i) for i in range(5)]
    s = series([(days[0], "0"), (days[1], "10"), (days[2], "30"), (days[3], "25"), (days[4], "35")])
    windows = windows_of(s, smart(START, [10.0, 10.0, 0.0, 10.0]), START)
    verdicts = [w.verdict for w in windows]
    assert verdicts == [Verdict.MATCH, Verdict.MISMATCH, Verdict.WENT_DOWN, Verdict.MATCH]
    summary = summarise_windows(windows)
    assert (summary.total, summary.compared, summary.mismatched, summary.not_compared) == (
        4,
        3,
        1,
        1,
    )

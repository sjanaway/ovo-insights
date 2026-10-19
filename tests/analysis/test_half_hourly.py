import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ovo_insights.analysis import queries
from ovo_insights.analysis.half_hourly import (
    SLOT_LABELS,
    annual_cost,
    base_load,
    heatmap,
    household,
    local_slot,
    nearest_rank,
    typical_day,
)
from ovo_insights.domain import DailyUsage, Fuel, HalfHourUsage
from ovo_insights.money import MilliPence
from ovo_insights.storage.repository import Repository
from ovo_insights.sync import store
from ovo_insights.sync.plan import DateRange, Granularity, Period, PeriodStatus
from ovo_insights.sync.store import Trigger
from ovo_insights.timeutil import london_half_hours

E = Fuel.ELECTRICITY
FETCHED = datetime(2026, 10, 13, tzinfo=UTC)


def day_of(day: date, kwh: float = 0.1) -> dict[datetime, float]:
    return dict.fromkeys(london_half_hours(day), kwh)


def test_slot_labels() -> None:
    assert (len(SLOT_LABELS), SLOT_LABELS[0], SLOT_LABELS[1], SLOT_LABELS[-1]) == (
        48,
        "00:00",
        "00:30",
        "23:30",
    )


def test_local_slot_uses_uk_wall_clock_time() -> None:
    # 23:00 UTC in summer is midnight in the UK, the next day.
    assert local_slot(datetime(2026, 7, 1, 23, 0, tzinfo=UTC)) == (date(2026, 7, 2), 0)
    assert local_slot(datetime(2026, 1, 1, 23, 30, tzinfo=UTC)) == (date(2026, 1, 1), 47)


def test_household_sums_accounts() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [HalfHourUsage("a", E, start, 0.25), HalfHourUsage("b", E, start, 0.5)]

    assert household(rows) == {start: 0.75}


@pytest.mark.parametrize(
    ("day", "cells", "doubled"),
    [
        (date(2026, 3, 29), 46, set()),  # clocks go forward: 01:00 and 01:30 don't exist
        (date(2026, 10, 25), 48, {2, 3}),  # clocks go back: 01:00 and 01:30 happen twice
        (date(2026, 6, 1), 48, set()),
    ],
)
def test_heatmap_on_dst_days(day: date, cells: int, doubled: set[int]) -> None:
    half_hours = {start: float(i) for i, start in enumerate(london_half_hours(day))}

    grid = heatmap(half_hours)

    assert len(grid) == cells
    assert {c.day for c in grid} == {day}
    assert {c.slot for c in grid if c.readings == 2} == doubled
    if doubled:
        first_one_am = next(c for c in grid if c.slot == 2)
        assert first_one_am.kwh == (2 + 4) / 2  # average of the two 01:00s (UTC 00:00, 01:00)
    if cells == 46:
        assert {2, 3}.isdisjoint(c.slot for c in grid)


@given(st.dates(min_value=date(2020, 1, 1), max_value=date(2035, 12, 31)))
def test_heatmap_preserves_energy_except_for_the_averaged_repeat(day: date) -> None:
    half_hours = day_of(day, 0.2)

    grid = heatmap(half_hours)

    assert sum(c.kwh * c.readings for c in grid) == pytest.approx(0.2 * len(half_hours))


def test_typical_day_splits_weekdays_and_weekends() -> None:
    half_hours = {**day_of(date(2026, 10, 9), 1.0), **day_of(date(2026, 10, 10), 3.0)}  # Fri, Sat
    half_hours.update(day_of(date(2026, 10, 12), 2.0))  # Mon

    weekday = typical_day(half_hours, weekend=False)
    weekend = typical_day(half_hours, weekend=True)

    assert (weekday[0].mean, weekday[0].median, weekday[0].samples) == (1.5, 1.5, 2)
    assert (weekend[0].mean, weekend[0].samples) == (3.0, 1)
    assert typical_day({}, weekend=True)[5].mean is None


def test_base_load_is_a_low_overnight_percentile() -> None:
    day = date(2026, 6, 1)
    half_hours = day_of(day, 2.0)  # a busy day...
    overnight = [s for s in london_half_hours(day) if local_slot(s)[1] < 10]
    for i, start in enumerate(overnight):
        half_hours[start] = 0.1 + i / 100  # ...with a quiet night: 0.10 .. 0.19 kWh

    load = base_load(half_hours)

    assert load is not None
    assert load.watts == pytest.approx(200.0)  # 0.10 kWh per 30 min = 200 W
    assert load.samples == 10
    assert base_load({}) is None


def test_nearest_rank() -> None:
    values = [float(v) for v in range(1, 11)]

    assert nearest_rank(values, 10) == 1.0
    assert nearest_rank(values, 11) == 2.0
    assert nearest_rank(values, 100) == 10.0
    with pytest.raises(ValueError, match="no values"):
        nearest_rank([], 10)
    with pytest.raises(ValueError, match="percentile"):
        nearest_rank(values, 0)


def test_annual_cost_of_a_constant_load() -> None:
    # 200 W all year is 1,752 kWh; at 25p/kWh that's £438.
    assert annual_cost(200.0, MilliPence(25_000)) == 43_800_000


# -- queries against a real database ------------------------------------------------------


def test_queries_read_household_rows_and_missing_days(conn: sqlite3.Connection) -> None:
    repo = Repository(conn)
    for account in ("a", "b"):
        repo.upsert_account(account)
    day = date(2026, 10, 1)
    repo.upsert_daily(
        [
            DailyUsage("a", E, day, 3.0, MilliPence(750), True),
            DailyUsage("b", E, day, 1.0, MilliPence(250), True),
            DailyUsage("a", E, day + timedelta(2), 2.0, MilliPence(500), True),
            DailyUsage("a", Fuel.GAS, day, 9.0, MilliPence(500), False),
        ],
        fetched_at=FETCHED,
    )
    starts = london_half_hours(day)
    repo.upsert_half_hourly(
        [HalfHourUsage("a", E, s, 0.1) for s in starts] + [HalfHourUsage("b", E, starts[0], 0.2)],
        fetched_at=FETCHED,
    )
    run_id = store.start_run(conn, Trigger.CLI, FETCHED)
    store.record_period(
        conn,
        Period("a", Granularity.HALF_HOURLY, day + timedelta(1)),
        PeriodStatus.GAP,
        expected=48,
        received=40,
        fetched_at=FETCHED,
        run_id=run_id,
    )
    span = DateRange(day, day + timedelta(2))

    totals = queries.daily(conn, E, span)
    half_hours = queries.half_hours(conn, E, DateRange(day, day))

    assert [(t.day, t.kwh, t.usage_cost) for t in totals] == [
        (day, 4.0, 1000),
        (day + timedelta(2), 2.0, 500),
    ]
    assert len(half_hours) == 48
    assert half_hours[starts[0]] == pytest.approx(0.3)
    assert queries.earliest_days(conn) == {E: day, Fuel.GAS: day}
    # 2 Oct: no daily rows at all (and a known gap); 3 Oct: no gas.
    assert queries.missing_in(conn, span) == {day + timedelta(1), day + timedelta(2)}

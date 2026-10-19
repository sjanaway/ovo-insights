"""Contract tests against real OVO response shapes, captured 2026-10-13.

The captures were redacted, then every kWh, cost, rate and meter reading was replaced with a
made-up value and account and meter dates were moved (`Synthesiser`), keeping the
relationships below. These pin down facts verified on a real account, so a future change
that contradicts them fails loudly:
- Both half-hourly endpoints send naive UTC timestamps. `half-hourly-local` covers the UK
  day and `half-hourly` the UTC day; values at the same timestamp are identical.
- Daily `cost` is kWh x `rates.anytime` (pounds per kWh), rounded to the penny; the
  standing charge (`rates.standing`, pounds per day) is not included.
- Monthly figures are exactly the sum of the daily figures.
- Gas has no half-hourly data on this account (`hasHhData: false`).
- Customers who left and returned have two accounts on the same supply points.
"""

import json
import sqlite3
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pytest

from ovo_insights.domain import Fuel
from ovo_insights.money import MilliPence
from ovo_insights.ovo import convert, models
from ovo_insights.storage.repository import Repository
from ovo_insights.timeutil import london_half_hours

CAPTURED = Path(__file__).resolve().parent.parent / "fixtures" / "captured"
FETCHED = datetime(2026, 10, 13, tzinfo=UTC)


def raw(name: str) -> Any:
    return json.loads((CAPTURED / f"{name}.json").read_text(), parse_float=Decimal)


def parsed[M: models.OvoModel](model: type[M], name: str) -> M:
    return model.model_validate(raw(name))


@pytest.fixture(scope="module")
def account_id() -> str:
    [current, _old] = convert.accounts(parsed(models.BootstrapResponse, "bootstrap"))
    return current.account_id


@pytest.mark.parametrize(
    ("model", "name"),
    [
        (models.BootstrapResponse, "bootstrap"),
        (models.MonthlyResponse, "monthly"),
        (models.DailyResponse, "daily"),
        (models.HalfHourResponse, "half_hourly"),
        (models.HalfHourResponse, "half_hourly_local"),
        (models.ReadsResponse, "meter_reads"),
    ],
)
def test_real_responses_have_no_unrecognised_fields(
    model: type[models.OvoModel], name: str
) -> None:
    assert models.unknown_fields(parsed(model, name)) == set()


def test_two_accounts_share_the_same_supply_points() -> None:
    summaries = convert.accounts(parsed(models.BootstrapResponse, "bootstrap"))

    assert len(summaries) == 2
    starts = sorted(min(p.start_date for p in s.supply_points) for s in summaries)
    assert starts == [date(2015, 9, 28), date(2019, 8, 15)]
    mpxns = [{p.mpxn for p in s.supply_points} for s in summaries]
    assert mpxns[0] == mpxns[1]
    assert all({p.fuel for p in s.supply_points} == {Fuel.ELECTRICITY, Fuel.GAS} for s in summaries)


def test_half_hourly_utc_endpoint(account_id: str) -> None:
    rows = convert.half_hours(
        account_id, parsed(models.HalfHourResponse, "half_hourly"), naive_is_utc=True
    )

    assert len(rows) == 48
    assert {r.fuel for r in rows} == {Fuel.ELECTRICITY}  # no gas half-hours on this account
    assert rows[0].interval_start == datetime(2026, 10, 11, 0, 0, tzinfo=UTC)
    assert rows[-1].interval_start == datetime(2026, 10, 11, 23, 30, tzinfo=UTC)


def test_half_hourly_local_endpoint_is_the_uk_day(account_id: str) -> None:
    rows = convert.half_hours(
        account_id, parsed(models.HalfHourResponse, "half_hourly_local"), naive_is_utc=True
    )

    # Exactly the UK day: 00:00 BST is 23:00 UTC the day before.
    assert [r.interval_start for r in rows] == london_half_hours(date(2026, 10, 11))


def test_both_half_hourly_endpoints_agree_at_the_same_timestamps(account_id: str) -> None:
    utc = convert.half_hours(
        account_id, parsed(models.HalfHourResponse, "half_hourly"), naive_is_utc=True
    )
    local = convert.half_hours(
        account_id, parsed(models.HalfHourResponse, "half_hourly_local"), naive_is_utc=True
    )
    by_instant = {r.interval_start: r.kwh for r in utc}
    overlap = [r for r in local if r.interval_start in by_instant]

    assert len(overlap) == 46
    assert all(by_instant[r.interval_start] == r.kwh for r in overlap)


def test_naive_half_hours_are_rejected_without_a_zone(account_id: str) -> None:
    from ovo_insights.ovo.errors import UpstreamSchemaChanged

    with pytest.raises(UpstreamSchemaChanged, match="timezone"):
        convert.half_hours(account_id, parsed(models.HalfHourResponse, "half_hourly"))


def test_daily_dates_rates_and_cost_semantics(account_id: str) -> None:
    rows = convert.daily(account_id, parsed(models.DailyResponse, "daily"))

    for fuel in (Fuel.ELECTRICITY, Fuel.GAS):
        days = [r for r in rows if r.fuel is fuel]
        assert [r.day for r in days] == [date(2026, 9, d) for d in range(1, 31)]
        for r in days:
            assert r.cost is not None
            assert r.unit_rate is not None
            assert r.standing_charge is not None
            # cost == kWh x unit rate, rounded to the penny; standing charge excluded.
            expected = (Decimal(str(r.kwh)) * r.unit_rate / 1000).quantize(
                Decimal(1), rounding=ROUND_HALF_UP
            ) * 1000
            assert abs(r.cost - expected) <= 1000, r.day  # within a penny of rounding
    elec = [r for r in rows if r.fuel is Fuel.ELECTRICITY]
    assert {r.has_half_hourly for r in elec} == {True}
    assert {r.has_half_hourly for r in rows if r.fuel is Fuel.GAS} == {False}


def test_monthly_equals_sum_of_daily_excluding_standing(account_id: str) -> None:
    months = convert.monthly(account_id, parsed(models.MonthlyResponse, "monthly"))
    days = convert.daily(account_id, parsed(models.DailyResponse, "daily"))

    assert len(months) == 20  # Jan-Oct 2026, two fuels
    for fuel in (Fuel.ELECTRICITY, Fuel.GAS):
        september = next(m for m in months if m.fuel is fuel and m.month == date(2026, 9, 1))
        sept_days = [d for d in days if d.fuel is fuel]
        assert september.cost == sum(d.cost or 0 for d in sept_days)
        assert september.kwh == pytest.approx(sum(d.kwh for d in sept_days), abs=1e-9)


def test_meter_data_with_history_and_estimates(account_id: str) -> None:
    data = convert.meter_data(account_id, parsed(models.ReadsResponse, "meter_reads"))

    assert len(data.supply_points) == 2
    assert len(data.meters) == 5  # current smart meters plus removed older ones
    assert data.skipped_registers == 1  # an old gas meter with status "Unknown"
    assert len(data.readings) == 717
    assert {r.meter_serial for r in data.readings} <= {m.serial for m in data.meters}
    assert max(len(format(r.value, "f").partition(".")[2]) for r in data.readings) == 8
    assert {r.lifecycle for r in data.readings} == {"INLIFE", "CHANGE_OF_SUPPLY_OPENING"}


def test_everything_stores_cleanly(account_id: str, conn: sqlite3.Connection) -> None:
    repo = Repository(conn)
    summaries = convert.accounts(parsed(models.BootstrapResponse, "bootstrap"))
    for summary in summaries:
        repo.upsert_account(summary.account_id)
        repo.upsert_supply_points(summary.supply_points)
    data = convert.meter_data(account_id, parsed(models.ReadsResponse, "meter_reads"))
    repo.upsert_supply_points(data.supply_points)
    repo.upsert_meters(data.meters)
    repo.upsert_registers(data.registers)
    repo.upsert_readings(data.readings)
    daily = convert.daily(account_id, parsed(models.DailyResponse, "daily"))
    repo.upsert_daily(daily, fetched_at=FETCHED)
    repo.upsert_monthly(
        convert.monthly(account_id, parsed(models.MonthlyResponse, "monthly")),
        fetched_at=FETCHED,
    )
    repo.upsert_half_hourly(
        convert.half_hours(
            account_id, parsed(models.HalfHourResponse, "half_hourly"), naive_is_utc=True
        ),
        fetched_at=FETCHED,
    )

    stored = repo.daily(account_id, Fuel.ELECTRICITY, date(2026, 9, 1), date(2026, 9, 30))
    assert stored == [d for d in daily if d.fuel is Fuel.ELECTRICITY]
    assert stored[0].standing_charge == MilliPence(44_360)  # £0.4436/day
    assert stored[0].unit_rate == MilliPence(21_700)  # £0.2170/kWh
    assert sum(len(repo.readings(m.serial)) for m in data.meters) == 717

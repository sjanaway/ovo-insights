"""The reads behind the Readings & gaps page, against a real (temporary) database."""

import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from ovo_insights.analysis import queries
from ovo_insights.domain import (
    GAS_REGISTER_ID,
    DailyUsage,
    Fuel,
    HalfHourUsage,
    Meter,
    Reading,
    Register,
    SupplyPoint,
)
from ovo_insights.storage.repository import Repository
from ovo_insights.sync import store
from ovo_insights.sync.plan import DateRange, Granularity, Period, PeriodStatus
from ovo_insights.sync.store import Trigger
from ovo_insights.timeutil import london_half_hours

NOW = datetime(2026, 10, 13, 6, tzinfo=UTC)
DAY = date(2025, 6, 11)


def add_accounts(repo: Repository, *ids: str) -> None:
    for account_id in ids:
        repo.upsert_account(account_id)


def test_register_series_merges_readings_across_accounts_by_meter(repo: Repository) -> None:
    add_accounts(repo, "acc1", "acc2")
    # The shared meter belongs to the current account; the old account's reads are on it too.
    repo.upsert_meters([Meter("SHARED01", "acc2", Fuel.ELECTRICITY, "S2AD", "Active")])
    repo.upsert_registers([Register("SHARED01", "1", date(2019, 4, 6), None, "ANYTIME", "KWH", 5)])
    repo.upsert_readings(
        [
            Reading("SHARED01", "1", date(2020, 1, 1), Decimal("10.5"), "MANUAL_READ_CUSTOMER"),
            Reading("SHARED01", "1", date(2019, 1, 1), Decimal("5.25")),
        ]
    )
    [series] = queries.register_series(repo._conn)
    assert (series.fuel, series.meter_status, series.unit) == (Fuel.ELECTRICITY, "Active", "KWH")
    assert [r.read_date for r in series.readings] == [date(2019, 1, 1), date(2020, 1, 1)]
    assert series.registers_on_meter == 1


def test_register_ids_gas_blank_and_padded_ones_stay_separate(repo: Repository) -> None:
    add_accounts(repo, "acc1")
    repo.upsert_meters(
        [
            Meter("GASMETER1", "acc1", Fuel.GAS),
            Meter("OLDELEC01", "acc1", Fuel.ELECTRICITY, status="Removed"),
        ]
    )
    repo.upsert_registers(
        [
            Register("GASMETER1", GAS_REGISTER_ID, date(2019, 4, 6), None, None, "SCMH", 5),
            Register("OLDELEC01", "01", date(1996, 12, 20), None, "ANYTIME", "KWH", 5),
        ]
    )
    repo.upsert_readings(
        [
            Reading("GASMETER1", GAS_REGISTER_ID, DAY, Decimal("7023.32")),
            Reading("OLDELEC01", "01", DAY, Decimal("1.00000001")),
        ]
    )
    found = {s.register_id: s for s in queries.register_series(repo._conn)}
    assert set(found) == {GAS_REGISTER_ID, "01"}
    assert found[GAS_REGISTER_ID].unit == "SCMH"
    assert found["01"].readings[0].value == Decimal("1.00000001")


def test_a_meter_with_two_register_ids_is_flagged_as_having_several(repo: Repository) -> None:
    add_accounts(repo, "acc1")
    repo.upsert_meters([Meter("TWOREG001", "acc1", Fuel.ELECTRICITY)])
    repo.upsert_registers(
        [
            Register("TWOREG001", "1", date(2020, 1, 1), None, "DAY", "KWH", 5),
            Register("TWOREG001", "2", date(2020, 1, 1), None, "NIGHT", "KWH", 5),
        ]
    )
    repo.upsert_readings(
        [Reading("TWOREG001", "1", DAY, Decimal(1)), Reading("TWOREG001", "2", DAY, Decimal(2))]
    )
    assert {s.registers_on_meter for s in queries.register_series(repo._conn)} == {2}


def test_conflicting_or_missing_register_units_become_unknown(repo: Repository) -> None:
    add_accounts(repo, "acc1")
    repo.upsert_meters([Meter("MIXED0001", "acc1", Fuel.GAS), Meter("NOUNIT001", "acc1", Fuel.GAS)])
    repo.upsert_registers(
        [
            Register(
                "MIXED0001", GAS_REGISTER_ID, date(2019, 1, 1), date(2020, 1, 1), None, "SCMH"
            ),
            Register("MIXED0001", GAS_REGISTER_ID, date(2020, 1, 1), None, None, "SCFH"),
            Register("NOUNIT001", GAS_REGISTER_ID, date(2019, 1, 1), None, None, None),
        ]
    )
    repo.upsert_readings(
        [
            Reading("MIXED0001", GAS_REGISTER_ID, DAY, Decimal(1)),
            Reading("NOUNIT001", GAS_REGISTER_ID, DAY, Decimal(1)),
        ]
    )
    assert {s.unit for s in queries.register_series(repo._conn)} == {None}


def test_a_meter_without_readings_has_no_series(repo: Repository) -> None:
    add_accounts(repo, "acc1")
    repo.upsert_meters([Meter("QUIET0001", "acc1", Fuel.GAS)])
    assert queries.register_series(repo._conn) == []
    assert queries.earliest_reading(repo._conn) is None
    repo.upsert_readings([Reading("QUIET0001", GAS_REGISTER_ID, DAY, Decimal(1))])
    assert queries.earliest_reading(repo._conn) == DAY


def test_supply_ranges_across_accounts_with_open_ends(repo: Repository) -> None:
    add_accounts(repo, "acc1", "acc2")
    repo.upsert_supply_points(
        [
            SupplyPoint(
                "acc1", Fuel.ELECTRICITY, "1900000000001", date(2017, 9, 26), date(2019, 3, 20)
            ),
            SupplyPoint("acc2", Fuel.ELECTRICITY, "1900000000001", date(2021, 8, 13)),
            SupplyPoint("acc2", Fuel.GAS, "9876543", date(2021, 8, 13)),
        ]
    )
    assert queries.supply_ranges(repo._conn, Fuel.ELECTRICITY) == [
        DateRange(date(2017, 9, 26), date(2019, 3, 20)),
        DateRange(date(2021, 8, 13), date.max),
    ]
    assert len(queries.supply_ranges(repo._conn, Fuel.GAS)) == 1


def seed_day(
    repo: Repository, day: date, *, slots: int, flag: bool = True, fuel: Fuel = Fuel.ELECTRICITY
) -> None:
    repo.upsert_daily([DailyUsage("acc1", fuel, day, 5.0, None, flag)], fetched_at=NOW)
    starts = london_half_hours(day)[:slots]
    repo.upsert_half_hourly([HalfHourUsage("acc1", fuel, s, 0.1) for s in starts], fetched_at=NOW)


def test_day_facts_count_distinct_uk_day_slots_across_clock_changes(repo: Repository) -> None:
    add_accounts(repo, "acc1")
    spring, autumn = date(2025, 3, 30), date(2025, 10, 26)
    seed_day(repo, spring, slots=46)
    seed_day(repo, autumn, slots=50)
    seed_day(repo, DAY, slots=40)
    facts = queries.day_facts(repo._conn, Fuel.ELECTRICITY, DateRange(spring, autumn))
    assert (facts[spring].received, facts[autumn].received, facts[DAY].received) == (46, 50, 40)
    assert all(f.has_daily and f.has_half_hourly_flag for f in facts.values())
    # Gas has stored nothing at all.
    assert queries.day_facts(repo._conn, Fuel.GAS, DateRange(spring, autumn)) == {}


def test_day_facts_slots_are_summed_distinctly_across_accounts(repo: Repository) -> None:
    add_accounts(repo, "acc1", "acc2")
    seed_day(repo, DAY, slots=20)
    repo.upsert_half_hourly(
        [HalfHourUsage("acc2", Fuel.ELECTRICITY, s, 0.1) for s in london_half_hours(DAY)[10:30]],
        fetched_at=NOW,
    )
    facts = queries.day_facts(repo._conn, Fuel.ELECTRICITY, DateRange(DAY, DAY))
    assert facts[DAY].received == 30  # slots 0-29, the overlap counted once


def test_day_facts_report_the_sync_state_needing_most_attention(repo: Repository) -> None:
    add_accounts(repo, "acc1", "acc2")
    seed_day(repo, DAY, slots=46)
    run = store.start_run(repo._conn, Trigger.CLI, NOW)
    for account, status in (("acc1", PeriodStatus.COMPLETE), ("acc2", PeriodStatus.GAP)):
        store.record_period(
            repo._conn,
            Period(account, Granularity.HALF_HOURLY, DAY),
            status,
            expected=48,
            received=46,
            fetched_at=NOW,
            run_id=run,
        )
    facts = queries.day_facts(repo._conn, Fuel.ELECTRICITY, DateRange(DAY, DAY))
    assert facts[DAY].sync_status is PeriodStatus.GAP
    other = queries.day_facts(repo._conn, Fuel.ELECTRICITY, DateRange(DAY - timedelta(days=1), DAY))
    assert other[DAY].sync_status is PeriodStatus.GAP


def test_first_half_hourly_day_uses_the_flag_or_the_stored_slots(repo: Repository) -> None:
    add_accounts(repo, "acc1")
    assert queries.first_half_hourly_day(repo._conn, Fuel.ELECTRICITY) is None
    # A day without the flag, but with slots in the first minutes of the UK day (23:00 UTC
    # the evening before in summer): the UK date counts, not the UTC date.
    seed_day(repo, date(2025, 7, 2), slots=2, flag=False)
    assert queries.first_half_hourly_day(repo._conn, Fuel.ELECTRICITY) == date(2025, 7, 2)
    seed_day(repo, date(2025, 8, 1), slots=0, flag=True)
    assert queries.first_half_hourly_day(repo._conn, Fuel.ELECTRICITY) == date(2025, 7, 2)
    seed_day(repo, date(2025, 6, 1), slots=0, flag=True)
    assert queries.first_half_hourly_day(repo._conn, Fuel.ELECTRICITY) == date(2025, 6, 1)
    assert queries.first_half_hourly_day(repo._conn, Fuel.GAS) is None


def test_reads_work_on_an_empty_database(conn: sqlite3.Connection) -> None:
    assert queries.register_series(conn) == []
    assert queries.supply_ranges(conn, Fuel.GAS) == []
    assert queries.day_facts(conn, Fuel.GAS, DateRange(DAY, DAY)) == {}

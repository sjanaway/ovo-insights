"""The reads that feed re-costing, against a real (temporary) database."""

import sqlite3
from datetime import UTC, date, datetime

from ovo_insights.analysis import queries
from ovo_insights.domain import DailyUsage, Fuel, HalfHourUsage
from ovo_insights.money import MilliPence
from ovo_insights.storage.repository import Repository
from ovo_insights.sync.plan import DateRange
from ovo_insights.timeutil import london_half_hours

NOW = datetime(2026, 10, 14, 9, tzinfo=UTC)


def seed(
    repo: Repository,
    account: str,
    fuel: Fuel,
    day: date,
    kwh: float,
    slots: int = 0,
    cost: int | None = 1_000,
    standing: int | None = 5_000,
) -> None:
    repo.upsert_daily(
        [
            DailyUsage(
                account,
                fuel,
                day,
                kwh,
                None if cost is None else MilliPence(cost),
                slots > 0,
                unit_rate=None if cost is None else MilliPence(100),
                standing_charge=None if standing is None else MilliPence(standing),
            )
        ],
        fetched_at=NOW,
    )
    repo.upsert_half_hourly(
        [HalfHourUsage(account, fuel, s, 0.5) for s in london_half_hours(day)[:slots]],
        fetched_at=NOW,
    )


def test_days_from_the_first_stored_day_with_half_hours_only_when_whole(
    repo: Repository, conn: sqlite3.Connection
) -> None:
    repo.upsert_account("acc1")
    spring, autumn = date(2026, 3, 29), date(2025, 10, 26)
    seed(repo, "acc1", Fuel.ELECTRICITY, date(2026, 3, 28), 24.0, 48)
    seed(repo, "acc1", Fuel.ELECTRICITY, spring, 23.0, 46)  # a whole (46-slot) day
    seed(repo, "acc1", Fuel.ELECTRICITY, date(2026, 3, 30), 20.0, 40)  # short of the day
    # 2026-03-31 has no daily figure at all; 2026-04-01 has no half-hours.
    seed(repo, "acc1", Fuel.ELECTRICITY, date(2026, 4, 1), 9.0, 0, cost=2_000, standing=None)
    days = queries.costing_days(
        conn, Fuel.ELECTRICITY, DateRange(date(2026, 3, 1), date(2026, 4, 1))
    )
    assert [d.day for d in days] == [
        date(2026, 3, 28), spring, date(2026, 3, 30), date(2026, 3, 31), date(2026, 4, 1),
    ]  # fmt: skip
    by_day = {d.day: d for d in days}
    assert by_day[date(2026, 3, 28)].half_hours is not None
    assert len(by_day[date(2026, 3, 28)].half_hours or {}) == 48
    assert len(by_day[spring].half_hours or {}) == 46
    assert by_day[date(2026, 3, 30)].half_hours is None  # 40 of 48
    assert (by_day[date(2026, 3, 31)].kwh, by_day[date(2026, 3, 31)].actual_usage) == (None, None)
    assert by_day[date(2026, 3, 31)].half_hours is None
    assert by_day[date(2026, 4, 1)].actual_standing is None
    assert by_day[date(2026, 4, 1)].actual_usage == 2_000
    assert autumn not in by_day


def test_the_autumn_clock_change_day_needs_all_50_slots(
    repo: Repository, conn: sqlite3.Connection
) -> None:
    repo.upsert_account("acc1")
    autumn = date(2025, 10, 26)
    seed(repo, "acc1", Fuel.ELECTRICITY, autumn, 25.0, 50)
    seed(repo, "acc1", Fuel.ELECTRICITY, date(2025, 10, 27), 24.0, 47)
    first, second = queries.costing_days(
        conn, Fuel.ELECTRICITY, DateRange(autumn, date(2025, 10, 27))
    )
    assert len(first.half_hours or {}) == 50
    assert second.half_hours is None


def test_accounts_are_summed_and_a_missing_cost_on_either_makes_it_unknown(
    repo: Repository, conn: sqlite3.Connection
) -> None:
    repo.upsert_account("acc1")
    repo.upsert_account("acc2")
    day = date(2026, 1, 14)
    seed(repo, "acc1", Fuel.GAS, day, 10.0, cost=1_000, standing=4_000)
    seed(repo, "acc2", Fuel.GAS, day, 5.0, cost=500, standing=2_000)
    other = date(2026, 1, 15)
    seed(repo, "acc1", Fuel.GAS, other, 10.0, cost=1_000, standing=4_000)
    seed(repo, "acc2", Fuel.GAS, other, 5.0, cost=None, standing=2_000)
    first, second = queries.costing_days(conn, Fuel.GAS, DateRange(day, other))
    assert (first.kwh, first.actual_usage, first.actual_standing) == (15.0, 1_500, 6_000)
    assert first.half_hours is None  # gas has none
    assert (second.kwh, second.actual_usage, second.actual_standing) == (15.0, None, 6_000)


def test_days_before_the_first_stored_day_and_other_fuels_are_left_out(
    repo: Repository, conn: sqlite3.Connection
) -> None:
    repo.upsert_account("acc1")
    seed(repo, "acc1", Fuel.GAS, date(2026, 1, 10), 5.0)
    span = DateRange(date(2026, 1, 1), date(2026, 1, 12))
    assert [d.day for d in queries.costing_days(conn, Fuel.GAS, span)] == [
        date(2026, 1, 10), date(2026, 1, 11), date(2026, 1, 12),
    ]  # fmt: skip
    assert queries.costing_days(conn, Fuel.ELECTRICITY, span) == []  # nothing stored for it
    assert queries.costing_days(conn, Fuel.GAS, DateRange(date(2025, 1, 1), date(2025, 6, 1))) == []

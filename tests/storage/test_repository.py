import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ovo_insights.domain import (
    GAS_REGISTER_ID,
    DailyUsage,
    DomainError,
    Fuel,
    HalfHourUsage,
    Meter,
    MonthlyUsage,
    Reading,
    Register,
    SupplyPoint,
)
from ovo_insights.money import MilliPence
from ovo_insights.storage.db import transaction
from ovo_insights.storage.repository import Repository
from ovo_insights.timeutil import HALF_HOUR, london_half_hours

E, G = Fuel.ELECTRICITY, Fuel.GAS
ACCOUNT = "1234567"
FETCHED = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
DAILY_INSERT = (
    "INSERT INTO usage_daily"
    " (account_id, fuel, day, kwh, cost_milli_pence, has_half_hourly, fetched_at_utc)"
)


@pytest.fixture
def account_repo(repo: Repository) -> Repository:
    repo.upsert_account(ACCOUNT)
    return repo


def test_account_upsert_is_idempotent(repo: Repository) -> None:
    repo.upsert_account(ACCOUNT)
    repo.upsert_account(ACCOUNT)

    assert repo.account_ids() == [ACCOUNT]


def test_account_id_is_validated(repo: Repository) -> None:
    with pytest.raises(DomainError):
        repo.upsert_account("bad id;")


def test_supply_points_round_trip_and_update(account_repo: Repository) -> None:
    point = SupplyPoint(ACCOUNT, E, "1234567890123", date(2023, 1, 1))
    account_repo.upsert_supply_points([point, SupplyPoint(ACCOUNT, G, "1234567", date(2023, 1, 1))])
    ended = SupplyPoint(ACCOUNT, E, "1234567890123", date(2023, 1, 1), date(2025, 1, 1))
    account_repo.upsert_supply_points([ended])

    points = account_repo.supply_points(ACCOUNT)

    assert ended in points
    assert point not in points
    assert len(points) == 2


def test_half_hourly_range_is_half_open_and_ordered(account_repo: Repository) -> None:
    slots = london_half_hours(date(2025, 3, 30))  # 46-slot DST day
    rows = [HalfHourUsage(ACCOUNT, E, s, i / 10) for i, s in enumerate(reversed(slots))]
    account_repo.upsert_half_hourly(rows, fetched_at=FETCHED)

    got = account_repo.half_hourly(ACCOUNT, E, slots[0], slots[-1])

    assert [u.interval_start for u in got] == slots[:-1]
    assert account_repo.half_hourly(ACCOUNT, G, slots[0], slots[-1] + HALF_HOUR) == []


def test_half_hourly_upsert_replaces_value(account_repo: Repository) -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    account_repo.upsert_half_hourly([HalfHourUsage(ACCOUNT, E, start, 0.1)], fetched_at=FETCHED)
    account_repo.upsert_half_hourly([HalfHourUsage(ACCOUNT, E, start, 0.2)], fetched_at=FETCHED)

    got = account_repo.half_hourly(ACCOUNT, E, start, start + HALF_HOUR)

    assert [u.kwh for u in got] == [0.2]


def test_daily_round_trip_including_null_cost(account_repo: Repository) -> None:
    rows = [
        DailyUsage(ACCOUNT, E, date(2025, 1, 1), 8.5, MilliPence(250_123), has_half_hourly=True),
        DailyUsage(ACCOUNT, E, date(2025, 1, 2), 9.0, None, has_half_hourly=False),
    ]
    account_repo.upsert_daily(rows, fetched_at=FETCHED)

    assert account_repo.daily(ACCOUNT, E, date(2025, 1, 1), date(2025, 1, 2)) == rows
    assert account_repo.daily(ACCOUNT, E, date(2025, 1, 2), date(2025, 1, 2)) == rows[1:]


def test_monthly_round_trip(account_repo: Repository) -> None:
    rows = [MonthlyUsage(ACCOUNT, G, date(2025, m, 1), 100.0 * m, None) for m in (1, 2, 3)]
    account_repo.upsert_monthly(rows, fetched_at=FETCHED)

    assert account_repo.monthly(ACCOUNT, G, date(2025, 2, 1), date(2025, 3, 1)) == rows[1:]


def test_meters_registers_readings_round_trip(account_repo: Repository) -> None:
    meter = Meter("E1", ACCOUNT, E, "SMETS2", "ACTIVE")
    gas_meter = Meter("G1", ACCOUNT, G)
    account_repo.upsert_meters([meter, gas_meter])
    registers = [
        Register("E1", "1", date(2023, 1, 1), None, "DAY", "KWH", 6),
        Register("E1", "2", date(2023, 1, 1), None, "NIGHT", "KWH", 6),
    ]
    account_repo.upsert_registers(registers)
    readings = [
        Reading("E1", "1", date(2025, 1, 1), Decimal("012345.6")),
        Reading("E1", "2", date(2025, 1, 1), Decimal("5432.1")),  # same day, other register
    ]
    gas_reading = Reading("G1", GAS_REGISTER_ID, date(2025, 1, 1), Decimal("789.123"), "SMART")
    account_repo.upsert_readings([*readings, gas_reading])

    assert account_repo.meters(ACCOUNT) == [meter, gas_meter]
    assert account_repo.registers("E1") == registers
    assert account_repo.readings("E1") == readings
    assert account_repo.readings("G1") == [gas_reading]
    # Exact decimal text is preserved, including leading zeros.
    assert account_repo.readings("E1")[0].value.as_tuple() == Decimal("012345.6").as_tuple()


def test_upserts_join_an_outer_transaction(
    conn: sqlite3.Connection, account_repo: Repository
) -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)

    def write_then_fail() -> None:
        with transaction(conn):
            account_repo.upsert_half_hourly(
                [HalfHourUsage(ACCOUNT, E, start, 0.1)], fetched_at=FETCHED
            )
            raise RuntimeError("fail the period")

    with pytest.raises(RuntimeError):
        write_then_fail()

    assert account_repo.half_hourly(ACCOUNT, E, start, start + HALF_HOUR) == []


def test_usage_for_unknown_account_is_rejected(repo: Repository) -> None:
    usage = HalfHourUsage("999", E, datetime(2025, 1, 1, tzinfo=UTC), 0.1)

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        repo.upsert_half_hourly([usage], fetched_at=FETCHED)


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        (
            "INSERT INTO usage_half_hourly VALUES (?, 'electricity', ?, 0.1, ?)",
            (ACCOUNT, "2025-01-01T00:15:00Z", "2025-01-01T00:00:00Z"),
        ),
        (
            "INSERT INTO usage_half_hourly VALUES (?, 'electricity', ?, 0.1, ?)",
            (ACCOUNT, "2025-01-01 00:00:00", "2025-01-01T00:00:00Z"),
        ),
        (
            "INSERT INTO usage_half_hourly VALUES (?, 'electricity', ?, -1, ?)",
            (ACCOUNT, "2025-01-01T00:00:00Z", "2025-01-01T00:00:00Z"),
        ),
        (
            "INSERT INTO usage_half_hourly VALUES (?, 'water', ?, 0.1, ?)",
            (ACCOUNT, "2025-01-01T00:00:00Z", "2025-01-01T00:00:00Z"),
        ),
        (
            DAILY_INSERT + " VALUES (?, 'gas', ?, 1, NULL, 0, ?)",
            (ACCOUNT, "2025-02-30", "2025-01-01T00:00:00Z"),
        ),
        (
            DAILY_INSERT + " VALUES (?, 'gas', ?, 1, 1.5, 0, ?)",
            (ACCOUNT, "2025-02-01", "2025-01-01T00:00:00Z"),
        ),
        (
            DAILY_INSERT + " VALUES (?, 'gas', ?, 1, NULL, 2, ?)",
            (ACCOUNT, "2025-02-01", "2025-01-01T00:00:00Z"),
        ),
        (
            "INSERT INTO usage_monthly VALUES (?, 'gas', ?, 1, NULL, NULL, ?)",
            (ACCOUNT, "2025-02-02", "2025-01-01T00:00:00Z"),
        ),
        (
            "INSERT INTO supply_points VALUES (?, 'gas', ?, '2025-01-01', NULL)",
            (ACCOUNT, "12a456"),
        ),
        (
            "INSERT INTO supply_points VALUES (?, 'gas', '123456', '2025-01-02', '2025-01-01')",
            (ACCOUNT,),
        ),
    ],
)
def test_schema_rejects_invalid_rows_written_directly(
    conn: sqlite3.Connection, account_repo: Repository, sql: str, params: tuple[object, ...]
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=r"CHECK|constraint|cannot store"):
        conn.execute(sql, params)


@pytest.mark.parametrize("value", ["1.2.3", "-1", "1e5", "", "1.", "abc"])
def test_schema_rejects_bad_reading_values(
    conn: sqlite3.Connection, account_repo: Repository, value: str
) -> None:
    account_repo.upsert_meters([Meter("E1", ACCOUNT, E)])

    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO readings (meter_serial, register_id, read_date, value)"
            " VALUES ('E1', '1', '2025-01-01', ?)",
            (value,),
        )


def test_corrupted_row_fails_loudly_on_read(
    conn: sqlite3.Connection, account_repo: Repository
) -> None:
    # A value the schema allows but the domain forbids (beyond the half-hour sanity ceiling).
    conn.execute(
        "INSERT INTO usage_half_hourly VALUES (?, 'electricity', ?, 9999, ?)",
        (ACCOUNT, "2025-01-01T00:00:00Z", "2025-01-01T00:00:00Z"),
    )
    start = datetime(2025, 1, 1, tzinfo=UTC)

    with pytest.raises(DomainError):
        account_repo.half_hourly(ACCOUNT, E, start, start + HALF_HOUR)


half_hour_rows = st.lists(
    st.tuples(st.integers(min_value=0, max_value=200), st.floats(min_value=0, max_value=50)),
    max_size=40,
)


@given(rows=half_hour_rows)
def test_upserting_twice_matches_upserting_once_last_write_wins(
    rows: list[tuple[int, float]],
) -> None:
    import tempfile
    from pathlib import Path

    from ovo_insights.storage import open_database

    base = datetime(2025, 1, 1, tzinfo=UTC)
    usage = [HalfHourUsage(ACCOUNT, E, base + i * HALF_HOUR, kwh) for i, kwh in rows]
    expected = {u.interval_start: u.kwh for u in usage}  # later duplicates win

    with tempfile.TemporaryDirectory() as tmp:
        conn = open_database(Path(tmp) / "d")
        try:
            repo = Repository(conn)
            repo.upsert_account(ACCOUNT)
            repo.upsert_half_hourly(usage, fetched_at=FETCHED)
            repo.upsert_half_hourly(usage, fetched_at=FETCHED + timedelta(hours=1))

            got = repo.half_hourly(ACCOUNT, E, base, base + 201 * HALF_HOUR)
        finally:
            conn.close()

    assert {u.interval_start: u.kwh for u in got} == expected
    assert [u.interval_start for u in got] == sorted(expected)


def test_had_half_hourly_before_looks_at_flags_and_stored_slots_of_earlier_days(
    account_repo: Repository, conn: sqlite3.Connection
) -> None:
    repo = account_repo
    day = date(2025, 7, 10)
    assert repo.had_half_hourly_before(ACCOUNT, E, day) is False  # nothing stored at all

    # A daily row saying there are no half-hours, however recent, doesn't count.
    conn.execute(
        f"{DAILY_INSERT} VALUES (?, 'electricity', '2025-07-09', 1.0, NULL, 0, ?)",
        (ACCOUNT, "2025-06-01T12:00:00Z"),
    )
    assert repo.had_half_hourly_before(ACCOUNT, E, day) is False

    # One that says there are, does, but only for earlier days (the day itself and later don't).
    conn.execute(
        f"{DAILY_INSERT} VALUES (?, 'electricity', '2025-07-10', 1.0, NULL, 1, ?)",
        (ACCOUNT, "2025-06-01T12:00:00Z"),
    )
    conn.execute(
        f"{DAILY_INSERT} VALUES (?, 'electricity', '2025-07-11', 1.0, NULL, 1, ?)",
        (ACCOUNT, "2025-06-01T12:00:00Z"),
    )
    assert repo.had_half_hourly_before(ACCOUNT, E, day) is False
    assert repo.had_half_hourly_before(ACCOUNT, E, date(2025, 7, 11)) is True

    # Stored half-hours count too, even with no daily row, by their UK day.
    spring = date(2025, 3, 30)
    repo.upsert_half_hourly(
        [HalfHourUsage(ACCOUNT, G, london_half_hours(spring)[0], 0.1)], fetched_at=FETCHED
    )
    assert repo.had_half_hourly_before(ACCOUNT, G, spring) is False  # the first slot is that day's
    assert repo.had_half_hourly_before(ACCOUNT, G, date(2025, 3, 31)) is True
    # Fuels and accounts are kept apart.
    assert repo.had_half_hourly_before(ACCOUNT, E, spring) is False
    repo.upsert_account("other")
    assert repo.had_half_hourly_before("other", G, date(2025, 3, 31)) is False

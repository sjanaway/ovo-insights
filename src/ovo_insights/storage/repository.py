"""Typed reads and idempotent writes.

Every write is an upsert keyed on the natural key, so re-syncing the same period is harmless.
Every write runs in a transaction (or joins the caller's). Every value read back goes through
the domain constructors, so a corrupted row raises instead of flowing into analysis.
"""

import sqlite3
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal

from ovo_insights.domain import (
    DailyUsage,
    Fuel,
    HalfHourUsage,
    Meter,
    MonthlyUsage,
    Reading,
    Register,
    SupplyPoint,
    validate_identifier,
)
from ovo_insights.money import MilliPence
from ovo_insights.storage.db import transaction
from ovo_insights.timeutil import format_utc, london_day_bounds, parse_utc, require_utc


class Repository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # -- writes ---------------------------------------------------------------------------

    def upsert_account(self, account_id: str) -> None:
        validate_identifier(account_id, "account id")
        with transaction(self._conn) as conn:
            conn.execute(
                "INSERT INTO accounts (account_id) VALUES (?) ON CONFLICT DO NOTHING",
                (account_id,),
            )

    def upsert_supply_points(self, points: Iterable[SupplyPoint]) -> None:
        rows = [
            (p.account_id, p.fuel.value, p.mpxn, p.start_date.isoformat(), _iso(p.end_date))
            for p in points
        ]
        with transaction(self._conn) as conn:
            conn.executemany(
                "INSERT INTO supply_points (account_id, fuel, mpxn, start_date, end_date)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (account_id, fuel, mpxn) DO UPDATE SET"
                " start_date = excluded.start_date, end_date = excluded.end_date",
                rows,
            )

    def upsert_half_hourly(self, usage: Iterable[HalfHourUsage], *, fetched_at: datetime) -> None:
        fetched = format_utc(fetched_at)
        rows = [
            (u.account_id, u.fuel.value, format_utc(u.interval_start), u.kwh, fetched)
            for u in usage
        ]
        with transaction(self._conn) as conn:
            conn.executemany(
                "INSERT INTO usage_half_hourly"
                " (account_id, fuel, interval_start_utc, kwh, fetched_at_utc)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (account_id, fuel, interval_start_utc) DO UPDATE SET"
                " kwh = excluded.kwh, fetched_at_utc = excluded.fetched_at_utc",
                rows,
            )

    def upsert_daily(self, usage: Iterable[DailyUsage], *, fetched_at: datetime) -> None:
        fetched = format_utc(fetched_at)
        rows = [
            (
                u.account_id,
                u.fuel.value,
                u.day.isoformat(),
                u.kwh,
                u.cost,
                int(u.has_half_hourly),
                u.unit_rate,
                u.standing_charge,
                fetched,
            )
            for u in usage
        ]
        with transaction(self._conn) as conn:
            conn.executemany(
                "INSERT INTO usage_daily"
                " (account_id, fuel, day, kwh, cost_milli_pence, has_half_hourly,"
                " unit_rate_milli_pence, standing_charge_milli_pence, fetched_at_utc)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (account_id, fuel, day) DO UPDATE SET"
                " kwh = excluded.kwh, cost_milli_pence = excluded.cost_milli_pence,"
                " has_half_hourly = excluded.has_half_hourly,"
                " unit_rate_milli_pence = excluded.unit_rate_milli_pence,"
                " standing_charge_milli_pence = excluded.standing_charge_milli_pence,"
                " fetched_at_utc = excluded.fetched_at_utc",
                rows,
            )

    def upsert_monthly(self, usage: Iterable[MonthlyUsage], *, fetched_at: datetime) -> None:
        fetched = format_utc(fetched_at)
        rows = [
            (u.account_id, u.fuel.value, u.month.isoformat(), u.kwh, u.cost, u.mpxn, fetched)
            for u in usage
        ]
        with transaction(self._conn) as conn:
            conn.executemany(
                "INSERT INTO usage_monthly"
                " (account_id, fuel, month, kwh, cost_milli_pence, mpxn, fetched_at_utc)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (account_id, fuel, month) DO UPDATE SET"
                " kwh = excluded.kwh, cost_milli_pence = excluded.cost_milli_pence,"
                " mpxn = excluded.mpxn, fetched_at_utc = excluded.fetched_at_utc",
                rows,
            )

    def upsert_meters(self, meters: Iterable[Meter]) -> None:
        rows = [(m.serial, m.account_id, m.fuel.value, m.meter_type, m.status) for m in meters]
        with transaction(self._conn) as conn:
            conn.executemany(
                "INSERT INTO meters (meter_serial, account_id, fuel, meter_type, status)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (meter_serial) DO UPDATE SET"
                " account_id = excluded.account_id, fuel = excluded.fuel,"
                " meter_type = excluded.meter_type, status = excluded.status",
                rows,
            )

    def upsert_registers(self, registers: Iterable[Register]) -> None:
        rows = [
            (
                r.meter_serial,
                r.register_id,
                r.start_date.isoformat(),
                _iso(r.end_date),
                r.timing_category,
                r.unit,
                r.digits,
            )
            for r in registers
        ]
        with transaction(self._conn) as conn:
            conn.executemany(
                "INSERT INTO registers (meter_serial, register_id, start_date, end_date,"
                " timing_category, unit, digits) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (meter_serial, register_id, start_date) DO UPDATE SET"
                " end_date = excluded.end_date, timing_category = excluded.timing_category,"
                " unit = excluded.unit, digits = excluded.digits",
                rows,
            )

    def upsert_readings(self, readings: Iterable[Reading]) -> None:
        rows = [
            (
                r.meter_serial,
                r.register_id,
                r.read_date.isoformat(),
                format(r.value, "f"),
                r.source,
                r.lifecycle,
                r.read_type,
            )
            for r in readings
        ]
        with transaction(self._conn) as conn:
            conn.executemany(
                "INSERT INTO readings (meter_serial, register_id, read_date, value, source,"
                " lifecycle, read_type) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (meter_serial, register_id, read_date) DO UPDATE SET"
                " value = excluded.value, source = excluded.source,"
                " lifecycle = excluded.lifecycle, read_type = excluded.read_type",
                rows,
            )

    # -- reads ----------------------------------------------------------------------------

    def account_ids(self) -> list[str]:
        rows = self._conn.execute("SELECT account_id FROM accounts ORDER BY account_id")
        return [validate_identifier(row[0], "account id") for row in rows]

    def supply_points(self, account_id: str) -> list[SupplyPoint]:
        rows = self._conn.execute(
            "SELECT account_id, fuel, mpxn, start_date, end_date FROM supply_points"
            " WHERE account_id = ? ORDER BY fuel, start_date, mpxn",
            (account_id,),
        )
        return [
            SupplyPoint(a, Fuel(f), m, date.fromisoformat(s), _date_or_none(e))
            for a, f, m, s, e in rows
        ]

    def half_hourly(
        self, account_id: str, fuel: Fuel, start: datetime, end: datetime
    ) -> list[HalfHourUsage]:
        """Half-hours with `start <= interval_start < end`, in time order."""
        rows = self._conn.execute(
            "SELECT interval_start_utc, kwh FROM usage_half_hourly"
            " WHERE account_id = ? AND fuel = ?"
            " AND interval_start_utc >= ? AND interval_start_utc < ?"
            " ORDER BY interval_start_utc",
            (account_id, fuel.value, format_utc(require_utc(start)), format_utc(end)),
        )
        return [HalfHourUsage(account_id, fuel, parse_utc(ts), kwh) for ts, kwh in rows]

    def daily(self, account_id: str, fuel: Fuel, first: date, last: date) -> list[DailyUsage]:
        """Days with `first <= day <= last`, in date order."""
        rows = self._conn.execute(
            "SELECT day, kwh, cost_milli_pence, has_half_hourly, unit_rate_milli_pence,"
            " standing_charge_milli_pence FROM usage_daily"
            " WHERE account_id = ? AND fuel = ? AND day BETWEEN ? AND ? ORDER BY day",
            (account_id, fuel.value, first.isoformat(), last.isoformat()),
        )
        return [
            DailyUsage(
                account_id,
                fuel,
                date.fromisoformat(d),
                kwh,
                _money(c),
                bool(hh),
                unit_rate=_money(rate),
                standing_charge=_money(standing),
            )
            for d, kwh, c, hh, rate, standing in rows
        ]

    def had_half_hourly_before(self, account_id: str, fuel: Fuel, day: date) -> bool:
        """Whether this account's fuel had half-hourly data on an earlier day: a daily row that
        says so, or stored half-hours."""
        if self._conn.execute(
            "SELECT 1 FROM usage_daily WHERE account_id = ? AND fuel = ?"
            " AND has_half_hourly = 1 AND day < ? LIMIT 1",
            (account_id, fuel.value, day.isoformat()),
        ).fetchone():
            return True
        return (
            self._conn.execute(
                "SELECT 1 FROM usage_half_hourly WHERE account_id = ? AND fuel = ?"
                " AND interval_start_utc < ? LIMIT 1",
                (account_id, fuel.value, format_utc(london_day_bounds(day)[0])),
            ).fetchone()
            is not None
        )

    def monthly(self, account_id: str, fuel: Fuel, first: date, last: date) -> list[MonthlyUsage]:
        """Months whose first day is within `first..last`, in order."""
        rows = self._conn.execute(
            "SELECT month, kwh, cost_milli_pence, mpxn FROM usage_monthly"
            " WHERE account_id = ? AND fuel = ? AND month BETWEEN ? AND ? ORDER BY month",
            (account_id, fuel.value, first.isoformat(), last.isoformat()),
        )
        return [
            MonthlyUsage(account_id, fuel, date.fromisoformat(m), kwh, _money(c), mpxn)
            for m, kwh, c, mpxn in rows
        ]

    def meters(self, account_id: str) -> list[Meter]:
        rows = self._conn.execute(
            "SELECT meter_serial, account_id, fuel, meter_type, status FROM meters"
            " WHERE account_id = ? ORDER BY meter_serial",
            (account_id,),
        )
        return [Meter(s, a, Fuel(f), t, st) for s, a, f, t, st in rows]

    def registers(self, meter_serial: str) -> list[Register]:
        rows = self._conn.execute(
            "SELECT meter_serial, register_id, start_date, end_date, timing_category, unit,"
            " digits FROM registers WHERE meter_serial = ? ORDER BY register_id, start_date",
            (meter_serial,),
        )
        return [
            Register(s, r, date.fromisoformat(sd), _date_or_none(ed), tc, u, dg)
            for s, r, sd, ed, tc, u, dg in rows
        ]

    def readings(self, meter_serial: str) -> list[Reading]:
        rows = self._conn.execute(
            "SELECT meter_serial, register_id, read_date, value, source, lifecycle, read_type"
            " FROM readings WHERE meter_serial = ? ORDER BY read_date, register_id",
            (meter_serial,),
        )
        return [
            Reading(s, r, date.fromisoformat(d), Decimal(v), src, lc, rt)
            for s, r, d, v, src, lc, rt in rows
        ]


def _iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _date_or_none(value: str | None) -> date | None:
    return None if value is None else date.fromisoformat(value)


def _money(value: int | None) -> MilliPence | None:
    return None if value is None else MilliPence(value)

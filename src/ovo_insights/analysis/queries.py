"""Reads for the analysis layer: household rows for a fuel and range, validated on read."""

import sqlite3
from datetime import date, datetime

from ovo_insights.analysis.completeness import DayFacts
from ovo_insights.analysis.half_hourly import household
from ovo_insights.analysis.readings import RegisterSeries
from ovo_insights.analysis.recost import CostingDay
from ovo_insights.analysis.usage import DayTotal, household_days, missing_days
from ovo_insights.domain import DailyUsage, Fuel, HalfHourUsage, Reading
from ovo_insights.storage.repository import Repository
from ovo_insights.sync.plan import DateRange, PeriodStatus
from ovo_insights.timeutil import (
    format_utc,
    london_date,
    london_day_bounds,
    london_half_hours,
    parse_utc,
)


def daily(conn: sqlite3.Connection, fuel: Fuel, span: DateRange) -> list[DayTotal]:
    repo = Repository(conn)
    rows: list[DailyUsage] = []
    for account_id in repo.account_ids():
        rows.extend(repo.daily(account_id, fuel, span.first, span.last))
    return household_days(rows, fuel)


def half_hours(conn: sqlite3.Connection, fuel: Fuel, span: DateRange) -> dict[datetime, float]:
    """Half-hours of the UK days in `span`, summed across accounts."""
    repo = Repository(conn)
    start = london_day_bounds(span.first)[0]
    end = london_day_bounds(span.last)[1]
    rows: list[HalfHourUsage] = []
    for account_id in repo.account_ids():
        rows.extend(repo.half_hourly(account_id, fuel, start, end))
    return household(rows)


def earliest_days(conn: sqlite3.Connection) -> dict[Fuel, date]:
    rows = conn.execute("SELECT fuel, min(day) FROM usage_daily GROUP BY fuel")
    return {Fuel(fuel): date.fromisoformat(day) for fuel, day in rows}


def earliest_reading(conn: sqlite3.Connection) -> date | None:
    """The oldest meter reading we hold (readings go back further than smart data)."""
    found = conn.execute("SELECT min(read_date) FROM readings").fetchone()[0]
    return date.fromisoformat(found) if found else None


def missing_in(conn: sqlite3.Connection, span: DateRange) -> set[date]:
    """Days in `span` missing daily data for any fuel, or with a known half-hourly gap."""
    present: dict[Fuel, set[date]] = {}
    for fuel, day in conn.execute(
        "SELECT DISTINCT fuel, day FROM usage_daily WHERE day BETWEEN ? AND ?",
        (span.first.isoformat(), span.last.isoformat()),
    ):
        present.setdefault(Fuel(fuel), set()).add(date.fromisoformat(day))
    gaps = {
        date.fromisoformat(day)
        for (day,) in conn.execute(
            "SELECT DISTINCT period_start FROM sync_state WHERE granularity = 'half_hourly'"
            " AND status IN ('gap', 'incomplete') AND period_start BETWEEN ? AND ?",
            (span.first.isoformat(), span.last.isoformat()),
        )
    }
    return missing_days(span, present, earliest_days(conn), gaps)


# -- readings and completeness ------------------------------------------------------------


def register_series(conn: sqlite3.Connection) -> list[RegisterSeries]:
    """Every register that has readings, merged across accounts (readings are keyed by meter,
    and the current account owns shared meters). Unordered; see `readings.sort_series`."""
    repo = Repository(conn)
    result = []
    for account_id in repo.account_ids():
        for meter in repo.meters(account_id):
            by_register: dict[str, list[Reading]] = {}
            for reading in repo.readings(meter.serial):
                by_register.setdefault(reading.register_id, []).append(reading)
            registers = repo.registers(meter.serial)
            for register_id, readings in by_register.items():
                units = {
                    r.unit.strip().upper()
                    for r in registers
                    if r.register_id == register_id and r.unit
                }
                result.append(
                    RegisterSeries(
                        meter.fuel,
                        meter.serial,
                        register_id,
                        meter.status,
                        next(iter(units)) if len(units) == 1 else None,
                        len(by_register),
                        tuple(sorted(readings, key=lambda r: r.read_date)),
                    )
                )
    return result


def supply_ranges(conn: sqlite3.Connection, fuel: Fuel) -> list[DateRange]:
    """Supply periods for a fuel, across accounts (an open end runs to the far future)."""
    rows = conn.execute(
        "SELECT start_date, end_date FROM supply_points WHERE fuel = ? ORDER BY start_date",
        (fuel.value,),
    )
    return [
        DateRange(date.fromisoformat(start), date.fromisoformat(end) if end else date.max)
        for start, end in rows
    ]


def first_half_hourly_day(conn: sqlite3.Connection, fuel: Fuel) -> date | None:
    """The first UK day a fuel had half-hourly data: flagged by OVO, or with slots stored."""
    flagged = conn.execute(
        "SELECT min(day) FROM usage_daily WHERE fuel = ? AND has_half_hourly = 1", (fuel.value,)
    ).fetchone()[0]
    slot = conn.execute(
        "SELECT min(interval_start_utc) FROM usage_half_hourly WHERE fuel = ?", (fuel.value,)
    ).fetchone()[0]
    found = [date.fromisoformat(flagged)] if flagged else []
    if slot:
        found.append(london_date(parse_utc(slot)))
    return min(found, default=None)


# When several accounts record a state for the same day, the one needing most attention wins.
_STATUS_PRIORITY = (
    PeriodStatus.GAP,
    PeriodStatus.INCOMPLETE,
    PeriodStatus.ERROR,
    PeriodStatus.PROVISIONAL,
    PeriodStatus.COMPLETE,
)


def day_facts(conn: sqlite3.Connection, fuel: Fuel, span: DateRange) -> dict[date, DayFacts]:
    """What is stored per UK day in `span` for one fuel, summed across accounts."""
    flagged: dict[date, bool] = {}
    for day, has_hh in conn.execute(
        "SELECT day, max(has_half_hourly) FROM usage_daily"
        " WHERE fuel = ? AND day BETWEEN ? AND ? GROUP BY day",
        (fuel.value, span.first.isoformat(), span.last.isoformat()),
    ):
        flagged[date.fromisoformat(day)] = bool(has_hh)

    start, end = london_day_bounds(span.first)[0], london_day_bounds(span.last)[1]
    slots: dict[date, set[str]] = {}
    for (stamp,) in conn.execute(
        "SELECT DISTINCT interval_start_utc FROM usage_half_hourly"
        " WHERE fuel = ? AND interval_start_utc >= ? AND interval_start_utc < ?",
        (fuel.value, format_utc(start), format_utc(end)),
    ):
        slots.setdefault(london_date(parse_utc(stamp)), set()).add(stamp)

    states: dict[date, set[PeriodStatus]] = {}
    for day, status in conn.execute(
        "SELECT period_start, status FROM sync_state WHERE granularity = 'half_hourly'"
        " AND period_start BETWEEN ? AND ?",
        (span.first.isoformat(), span.last.isoformat()),
    ):
        states.setdefault(date.fromisoformat(day), set()).add(PeriodStatus(status))

    return {
        day: DayFacts(
            has_daily=day in flagged,
            has_half_hourly_flag=flagged.get(day, False),
            received=len(slots.get(day, ())),
            sync_status=next((s for s in _STATUS_PRIORITY if s in states.get(day, ())), None),
        )
        for day in flagged.keys() | slots.keys()
    }


def costing_days(conn: sqlite3.Connection, fuel: Fuel, span: DateRange) -> list[CostingDay]:
    """One entry per UK day of `span` from the fuel's first stored day: its daily figures and,
    when every half-hour of the day is stored, those too. Days before the first stored day
    were never downloaded, so they aren't part of any comparison."""
    first = earliest_days(conn).get(fuel)
    clipped = None if first is None else span.clip(first, span.last)
    if clipped is None:
        return []
    totals = {d.day: d for d in daily(conn, fuel, clipped)}
    by_slot = half_hours(conn, fuel, clipped)
    days = []
    for day in clipped.days():
        total = totals.get(day)
        slots = london_half_hours(day)
        whole = all(start in by_slot for start in slots)
        days.append(
            CostingDay(
                day,
                None if total is None else total.kwh,
                None if total is None else total.usage_cost,
                None if total is None else total.standing,
                {start: by_slot[start] for start in slots} if whole else None,
            )
        )
    return days

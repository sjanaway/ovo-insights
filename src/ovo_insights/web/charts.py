"""Data for the charts and tiles: one JSON payload per chart.

Money is sent as exact decimal strings ("12.34"), never floats, and formatted in the
browser. kWh is rounded to 3 decimal places (meter resolution). Missing values are `null`,
never zero.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from ovo_insights.analysis import queries
from ovo_insights.analysis.half_hourly import (
    DEFAULT_WINDOW,
    SLOT_LABELS,
    BaseLoad,
    TimeWindow,
    annual_cost,
    base_load,
    heatmap,
    nightly_base_load,
    typical_day,
)
from ovo_insights.analysis.usage import (
    Change,
    Preset,
    Summary,
    buckets,
    compare,
    grain_for,
    is_partial,
    pounds_text,
    previous_year,
    recent_unit_rate,
    resolve_range,
    summarise,
    year_on_year,
)
from ovo_insights.domain import Fuel
from ovo_insights.money import MilliPence
from ovo_insights.sync.plan import DateRange


def span_for(
    conn: sqlite3.Connection, preset: Preset, today: date, *, with_readings: bool = False
) -> DateRange:
    """The range a preset means. `with_readings`: "all" starts at the oldest meter reading."""
    found = list(queries.earliest_days(conn).values())
    if with_readings and (reading := queries.earliest_reading(conn)) is not None:
        found.append(reading)
    return resolve_range(preset, today, min(found, default=None))


@dataclass(frozen=True, slots=True)
class FuelTiles:
    fuel: Fuel
    summary: Summary
    change: Change

    @property
    def cost_text(self) -> str | None:
        return pounds_text(self.summary.cost) if self.summary.costed_days else None

    @property
    def average_text(self) -> str | None:
        return pounds_text(self.summary.average_cost_per_day)


def tiles(conn: sqlite3.Connection, span: DateRange) -> list[FuelTiles]:
    result = []
    earlier = previous_year(span)
    for fuel in Fuel:
        days = queries.daily(conn, fuel, span)
        if not days:
            continue
        before = queries.daily(conn, fuel, earlier)
        result.append(FuelTiles(fuel, summarise(days), compare(days, span, before)))
    return result


def usage(conn: sqlite3.Connection, fuel: Fuel, span: DateRange) -> dict[str, Any]:
    grain = grain_for(span)
    rows = buckets(queries.daily(conn, fuel, span), grain)
    return {
        "fuel": fuel.value,
        "grain": grain.value,
        "labels": [b.start.isoformat() for b in rows],
        "kwh": [_kwh(b.kwh) for b in rows],
        "usage_cost": [pounds_text(b.usage_cost) if b.costed_days else None for b in rows],
        "standing": [pounds_text(b.standing) if b.costed_days else None for b in rows],
        "days": [b.days for b in rows],
        "costed_days": [b.costed_days for b in rows],
        # Periods cut off by the range (a week or month only partly inside it).
        "partial": [is_partial(b.start, grain, span) for b in rows],
    }


def yoy(conn: sqlite3.Connection, fuel: Fuel, span: DateRange) -> dict[str, Any]:
    earlier = previous_year(span)
    result = year_on_year(queries.daily(conn, fuel, span), queries.daily(conn, fuel, earlier), span)
    return {
        "fuel": fuel.value,
        "grain": result.grain.value,
        "labels": result.labels,
        "current": [None if v is None else _kwh(v) for v in result.current],
        "previous": [None if v is None else _kwh(v) for v in result.previous],
        "current_name": f"{span.first:%d %b %Y} to {span.last:%d %b %Y}",
        "previous_name": f"{earlier.first:%d %b %Y} to {earlier.last:%d %b %Y}",
    }


def heatmap_data(conn: sqlite3.Connection, span: DateRange) -> dict[str, Any]:
    cells = heatmap(queries.half_hours(conn, Fuel.ELECTRICITY, span))
    days = span.days()
    index = {day: i for i, day in enumerate(days)}
    return {
        "days": [d.isoformat() for d in days],
        "slots": list(SLOT_LABELS),
        "cells": [[index[c.day], c.slot, _kwh(c.kwh), c.readings] for c in cells if c.day in index],
        "max": _kwh(max((c.kwh for c in cells), default=0.0)),
    }


def profile(conn: sqlite3.Connection, span: DateRange) -> dict[str, Any]:
    half_hours = queries.half_hours(conn, Fuel.ELECTRICITY, span)
    weekday = typical_day(half_hours, weekend=False)
    weekend = typical_day(half_hours, weekend=True)

    def values(stats: list[Any], attr: str) -> list[float | None]:
        return [None if getattr(s, attr) is None else _kwh(getattr(s, attr)) for s in stats]

    return {
        "slots": list(SLOT_LABELS),
        "weekday_mean": values(weekday, "mean"),
        "weekday_median": values(weekday, "median"),
        "weekend_mean": values(weekend, "mean"),
        "weekend_median": values(weekend, "median"),
    }


def gas_daily(conn: sqlite3.Connection, span: DateRange) -> dict[str, Any]:
    days = queries.daily(conn, Fuel.GAS, span)
    return {"labels": [d.day.isoformat() for d in days], "kwh": [_kwh(d.kwh) for d in days]}


@dataclass(frozen=True, slots=True)
class BaseLoadTile:
    load: BaseLoad
    unit_rate: MilliPence | None
    window: TimeWindow = DEFAULT_WINDOW

    @property
    def watts_text(self) -> str:
        return f"{self.load.watts:,.0f}"

    @property
    def per_year_text(self) -> str | None:
        if self.unit_rate is None:
            return None
        return pounds_text(annual_cost(self.load.watts, self.unit_rate))

    @property
    def rate_text(self) -> str | None:
        if self.unit_rate is None:
            return None
        return f"{Decimal(self.unit_rate) / 1000:.2f}"  # pence per kWh


def _window_data(
    conn: sqlite3.Connection, span: DateRange, window: TimeWindow
) -> dict[datetime, float]:
    """Half-hours for the nights of `span`. A window past midnight needs the morning after the
    last night too."""
    last = span.last + timedelta(days=1) if window.wraps else span.last
    return queries.half_hours(conn, Fuel.ELECTRICITY, DateRange(span.first, last))


def base_load_tile(
    conn: sqlite3.Connection, span: DateRange, window: TimeWindow = DEFAULT_WINDOW
) -> BaseLoadTile | None:
    load = base_load(_window_data(conn, span, window), window, span)
    if load is None:
        return None
    rate = recent_unit_rate(queries.daily(conn, Fuel.ELECTRICITY, span))
    return BaseLoadTile(load, rate, window)


def base_load_nightly(
    conn: sqlite3.Connection, span: DateRange, window: TimeWindow = DEFAULT_WINDOW
) -> dict[str, Any]:
    """Base load for each night in `span`, in watts; `null` where the window isn't complete."""
    nights = nightly_base_load(_window_data(conn, span, window), window, span)
    return {
        "window": window.label,
        "labels": [n.night.isoformat() for n in nights],
        "watts": [None if n.watts is None else round(n.watts, 1) for n in nights],
        "wraps": window.wraps,
    }


def _kwh(value: float) -> float:
    return round(value, 3)

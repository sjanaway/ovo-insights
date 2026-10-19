"""Data for the Readings & gaps page: the readings table, derived consumption against smart
data, and the completeness calendar and summary.

Meters are shown as "Electricity meter 1", never by serial number, and registers by their id
(a short label such as `1`), so the page carries no identifiers.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ovo_insights.analysis import queries
from ovo_insights.analysis.completeness import (
    STATE_LABELS,
    DayCell,
    DayState,
    FuelCompleteness,
    fuel_completeness,
)
from ovo_insights.analysis.readings import (
    CALORIFIC_RANGE,
    PROBLEM_VERDICTS,
    RegisterSeries,
    Verdict,
    Window,
    WindowSummary,
    sort_series,
    summarise_windows,
    windows_of,
)
from ovo_insights.domain import Fuel, Reading
from ovo_insights.sync.plan import DateRange

PAGE_SIZE = 100
READING_COLUMNS = ("date", "value", "source", "lifecycle", "type")
STATE_CLASSES = {state: state.value.replace("_", "-") for state in DayState}


@dataclass(frozen=True, slots=True)
class SeriesChoice:
    """One register in the picker."""

    number: int  # 1-based position in the picker (the `r` URL parameter)
    label: str
    meter: str  # "Electricity meter 1": no serial, as everywhere on the pages
    series: RegisterSeries
    in_range: int  # readings in the chosen date range


def reading_span(span: DateRange, today: date) -> DateRange:
    """Readings are taken at the start of a day, so a range ending yesterday includes the
    reading dated today (it closes the last day's window)."""
    if span.last == today - timedelta(days=1):
        return DateRange(span.first, today)
    return span


def choices(conn: sqlite3.Connection, span: DateRange) -> list[SeriesChoice]:
    ordered = sort_series(queries.register_series(conn))
    per_fuel: dict[Fuel, int] = {}
    registers_of_meter: dict[str, int] = {}
    meter_numbers: dict[str, int] = {}
    result = []
    for number, series in enumerate(ordered, start=1):
        if series.meter_serial not in meter_numbers:
            per_fuel[series.fuel] = per_fuel.get(series.fuel, 0) + 1
            meter_numbers[series.meter_serial] = per_fuel[series.fuel]
        registers_of_meter[series.meter_serial] = registers_of_meter.get(series.meter_serial, 0) + 1
        status = (series.meter_status or "status unknown").lower()
        register = f", register {series.register_id}" if series.register_id else ""
        meter = f"{series.fuel.value.capitalize()} meter {meter_numbers[series.meter_serial]}"
        label = f"{meter}{register} ({status}, {series.unit or 'unit unknown'})"
        in_range = sum(1 for r in series.readings if r.read_date in span)
        result.append(SeriesChoice(number, label, meter, series, in_range))
    return result


# -- readings table -----------------------------------------------------------------------


def sorted_readings(readings: list[Reading], column: str, descending: bool) -> list[Reading]:
    """Sort by a column; readings without a value for it (source and so on) always go last."""
    keys: dict[str, Callable[[Reading], Any]] = {
        "date": lambda r: r.read_date,
        "value": lambda r: r.value,
        "source": lambda r: r.source or "",
        "lifecycle": lambda r: r.lifecycle or "",
        "type": lambda r: r.read_type or "",
    }
    key = keys[column]
    present = [r for r in readings if column in ("date", "value") or key(r)]
    absent = [r for r in readings if r not in present]
    present.sort(key=lambda r: (key(r), r.read_date), reverse=descending)
    return present + absent


def paginate[T](items: list[T], page: int) -> tuple[list[T], int]:
    """(the items of 1-based `page`, the number of pages)."""
    pages = max(1, -(-len(items) // PAGE_SIZE))
    page = min(max(page, 1), pages)
    return items[(page - 1) * PAGE_SIZE : page * PAGE_SIZE], pages


# -- derived consumption ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Comparison:
    windows: list[Window]
    summary: WindowSummary
    gas: bool  # gas windows show volume and implied calorific value, not kWh


def comparison(conn: sqlite3.Connection, series: RegisterSeries, span: DateRange) -> Comparison:
    ends = [r.read_date for r in series.readings if r.read_date in span]
    smart: dict[date, float] = {}
    if ends and len(series.readings) > 1:
        # Windows can start before the range, so fetch from the reading before the first end.
        first = max(
            (r.read_date for r in series.readings if r.read_date < min(ends)), default=min(ends)
        )
        days = queries.daily(conn, series.fuel, DateRange(first, max(ends)))
        smart = {d.day: d.kwh for d in days}
    first_smart = queries.earliest_days(conn).get(series.fuel)
    windows = windows_of(series, smart, first_smart, ends_in=(span.first, span.last))
    return Comparison(windows, summarise_windows(windows), series.fuel is Fuel.GAS)


def filter_windows(windows: list[Window], problems_only: bool) -> list[Window]:
    """Newest first; optionally only the windows that are flagged or couldn't be compared
    (windows before smart data was recorded aren't problems: nothing was ever missing)."""
    chosen = [w for w in windows if not problems_only or w.verdict in PROBLEM_VERDICTS]
    return sorted(chosen, key=lambda w: w.end, reverse=True)


def comparison_chart(windows: list[Window], *, gas: bool) -> dict[str, Any]:
    """Chart data for windows that could be compared. Electricity: derived against smart-data
    kWh. Gas: the implied calorific value against the range printed on the bill."""
    if gas:
        usable = [w for w in windows if w.implied_calorific_value is not None]
        return {
            "kind": "gas",
            "labels": [w.end.isoformat() for w in usable],
            "days": [w.days for w in usable],
            "implied": [float(w.implied_calorific_value or 0) for w in usable],
            "flagged": [w.verdict is Verdict.MISMATCH for w in usable],
            "low": float(CALORIFIC_RANGE[0]),
            "high": float(CALORIFIC_RANGE[1]),
        }
    kwh = [w for w in windows if w.derived_kwh is not None and w.smart_kwh is not None]
    return {
        "kind": "electricity",
        "labels": [w.end.isoformat() for w in kwh],
        "days": [w.days for w in kwh],
        "derived": [float(w.derived_kwh or 0) for w in kwh],
        "smart": [float(w.smart_kwh or 0) for w in kwh],
        "flagged": [w.verdict is Verdict.MISMATCH for w in kwh],
    }


# -- completeness -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CalendarCell:
    day: date
    css: str
    label: str  # the tooltip and screen-reader text


@dataclass(frozen=True, slots=True)
class Calendar:
    fuel: Fuel
    completeness: FuelCompleteness
    cells: list[CalendarCell]
    offset: int  # weekday of the first day (Monday = 0), so the grid starts in the right row
    first: date
    last: date
    clipped: bool  # the range began before this fuel's first data


def calendars(conn: sqlite3.Connection, span: DateRange, today: date) -> list[Calendar]:
    firsts = queries.earliest_days(conn)
    result = []
    for fuel in Fuel:
        built = fuel_completeness(
            span,
            today,
            queries.supply_ranges(conn, fuel),
            queries.day_facts(conn, fuel, span),
            firsts.get(fuel),
            queries.first_half_hourly_day(conn, fuel),
        )
        if built is None:
            continue
        first = built.cells[0].day
        result.append(
            Calendar(
                fuel,
                built,
                [CalendarCell(c.day, STATE_CLASSES[c.state], _describe(c)) for c in built.cells],
                first.weekday(),
                first,
                built.cells[-1].day,
                first > span.first,
            )
        )
    return result


def _describe(cell: DayCell) -> str:
    text = f"{cell.day.day} {cell.day:%b %Y}: {STATE_LABELS[cell.state].lower()}"
    if cell.state is DayState.PARTIAL:
        text += f" ({cell.received} of {cell.expected} half-hours)"
        if cell.note:
            text += f", {cell.note}"
    elif cell.state is DayState.MISSING and cell.expected:
        text += f" ({cell.received} of {cell.expected} half-hours)"
    return text

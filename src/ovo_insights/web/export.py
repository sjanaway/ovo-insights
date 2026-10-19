"""CSV export: one file per dataset for a date range, individually or as one zip.

The columns, units and rules are a stable contract, written up in `docs/csv-format.md`; a test
fails if the two disagree. Rules, in short:

- UTF-8 with a byte-order mark (so Excel on a Mac shows £ and accents properly), `\\r\\n` line
  endings, a header row, standard quoting (`csv.QUOTE_MINIMAL`).
- Amounts are pounds to exactly 2 decimal places (`..._gbp`), rates and standing charges are
  pence to exactly 3 (`..._pence`: milli-pence are exact), kWh are 3 decimal places. Nothing
  goes through a float, and an unknown value is an empty cell, never 0.
- Dates are UK calendar dates (`YYYY-MM-DD`). Half-hours carry both `utc_time` and
  `local_time` (UK time with its offset), so clock-change days are unambiguous.
- Text that came from outside (OVO's labels) or from the user (tariff names) and starts with
  `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading `'`, so a spreadsheet can't
  run it as a formula. Numbers are never changed.
- Usage is household totals (accounts summed, as on every page). Meters are "Electricity
  meter 1", never by serial number.
- Rows are produced one at a time and written in chunks, so even two years of half-hours
  never sit in memory, and a zip is written as a stream too.
"""

import csv
import io
import sqlite3
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum

from ovo_insights.analysis import queries
from ovo_insights.analysis.recost import Totals, columns_for, compare
from ovo_insights.analysis.usage import pounds_text
from ovo_insights.domain import Fuel
from ovo_insights.money import MilliPence, format_pence
from ovo_insights.storage import tariffs as tariff_store
from ovo_insights.storage.repository import Repository
from ovo_insights.sync.plan import DateRange
from ovo_insights.timeutil import LONDON, format_utc, london_day_bounds, parse_utc
from ovo_insights.web import readings

BYTE_ORDER_MARK = b"\xef\xbb\xbf"
CHUNK_ROWS = 2_000
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
BASELINE_NAME = "What OVO charged"


class Dataset(StrEnum):
    MONTHLY = "monthly"
    DAILY = "daily"
    HALF_HOURLY_ELECTRICITY = "half-hourly-electricity"
    HALF_HOURLY_GAS = "half-hourly-gas"
    READINGS = "readings"
    TARIFF_COMPARISON = "tariff-comparison"


# The contract. Changing a name, order or unit needs a note in docs/csv-format.md.
COLUMNS: dict[Dataset, tuple[str, ...]] = {
    Dataset.MONTHLY: ("month", "fuel", "kwh", "usage_cost_gbp"),
    Dataset.DAILY: (
        "date",
        "fuel",
        "kwh",
        "usage_cost_gbp",
        "standing_charge_pence",
        "unit_rate_pence",
        "has_half_hourly",
    ),
    Dataset.HALF_HOURLY_ELECTRICITY: ("utc_time", "local_time", "kwh"),
    Dataset.HALF_HOURLY_GAS: ("utc_time", "local_time", "kwh"),
    Dataset.READINGS: (
        "fuel",
        "meter",
        "register",
        "unit",
        "meter_status",
        "date",
        "value",
        "source",
        "lifecycle",
        "type",
    ),
    Dataset.TARIFF_COMPARISON: (
        "fuel",
        "tariff",
        "month",
        "days_compared",
        "usage_gbp",
        "standing_gbp",
        "total_gbp",
    ),
}
# Columns holding text from outside or from the user: these get the formula guard.
TEXT_COLUMNS: dict[Dataset, frozenset[str]] = {
    Dataset.READINGS: frozenset(
        {"register", "unit", "meter_status", "source", "lifecycle", "type"}
    ),
    Dataset.TARIFF_COMPARISON: frozenset({"tariff"}),
}

TITLES = {
    Dataset.MONTHLY: "Monthly usage",
    Dataset.DAILY: "Daily usage and rates",
    Dataset.HALF_HOURLY_ELECTRICITY: "Half-hourly electricity",
    Dataset.HALF_HOURLY_GAS: "Half-hourly gas",
    Dataset.READINGS: "Meter readings",
    Dataset.TARIFF_COMPARISON: "Tariff comparison",
}
DESCRIPTIONS = {
    Dataset.MONTHLY: "kWh and OVO's usage cost per month and fuel. Months overlapping the range.",
    Dataset.DAILY: "kWh, OVO's usage cost, standing charge and unit rate per day and fuel.",
    Dataset.HALF_HOURLY_ELECTRICITY: "Every half-hour, in UTC and UK time.",
    Dataset.HALF_HOURLY_GAS: "Every half-hour, in UTC and UK time (some accounts have none).",
    Dataset.READINGS: "Every meter reading in the range, by meter and register.",
    Dataset.TARIFF_COMPARISON: (
        "Each tariff against what OVO charged, per month, over the days every one can be costed."
    ),
}

HALF_HOURLY_FUEL = {
    Dataset.HALF_HOURLY_ELECTRICITY: Fuel.ELECTRICITY,
    Dataset.HALF_HOURLY_GAS: Fuel.GAS,
}


def filename(dataset: Dataset, span: DateRange) -> str:
    return f"ovo-insights-{dataset.value}-{span.first.isoformat()}_to_{span.last.isoformat()}.csv"


def zip_filename(span: DateRange) -> str:
    return f"ovo-insights-export-{span.first.isoformat()}_to_{span.last.isoformat()}.zip"


# -- cells --------------------------------------------------------------------------------


def defend(text: str) -> str:
    """Stop a spreadsheet reading text as a formula (OWASP's CSV-injection advice)."""
    return "'" + text if text.startswith(FORMULA_STARTS) else text


def kwh_text(value: float) -> str:
    return format(Decimal(str(value)).quantize(Decimal("0.001"), rounding=ROUND_HALF_EVEN), "f")


def _gbp(amount: MilliPence | None) -> str:
    return "" if amount is None else str(pounds_text(amount))


def _pence(amount: MilliPence | None) -> str:
    return "" if amount is None else format_pence(amount)


# -- rows ---------------------------------------------------------------------------------


def rows(
    conn: sqlite3.Connection, dataset: Dataset, span: DateRange, today: date
) -> Iterator[list[str]]:
    """The dataset's rows for `span`, as text cells in `COLUMNS[dataset]` order."""
    if dataset is Dataset.MONTHLY:
        return _monthly(conn, span)
    if dataset is Dataset.DAILY:
        return _daily(conn, span)
    if dataset in HALF_HOURLY_FUEL:
        return _half_hourly(conn, HALF_HOURLY_FUEL[dataset], span)
    if dataset is Dataset.READINGS:
        return _readings(conn, readings.reading_span(span, today))
    return _tariff_comparison(conn, span)


def count(conn: sqlite3.Connection, dataset: Dataset, span: DateRange, today: date) -> int:
    if dataset in HALF_HOURLY_FUEL:
        start, end = london_day_bounds(span.first)[0], london_day_bounds(span.last)[1]
        found = conn.execute(
            "SELECT count(DISTINCT interval_start_utc) FROM usage_half_hourly"
            " WHERE fuel = ? AND interval_start_utc >= ? AND interval_start_utc < ?",
            (HALF_HOURLY_FUEL[dataset].value, format_utc(start), format_utc(end)),
        ).fetchone()[0]
        return int(found)
    return sum(1 for _ in rows(conn, dataset, span, today))


def _monthly(conn: sqlite3.Connection, span: DateRange) -> Iterator[list[str]]:
    repo = Repository(conn)
    first = span.first.replace(day=1)
    totals: dict[tuple[date, Fuel], tuple[float, int | None]] = {}
    for account_id in repo.account_ids():
        for fuel in Fuel:
            for row in repo.monthly(account_id, fuel, first, span.last):
                kwh, cost = totals.get((row.month, fuel), (0.0, 0))
                new_cost = None if cost is None or row.cost is None else cost + row.cost
                totals[(row.month, fuel)] = (kwh + row.kwh, new_cost)
    order = list(Fuel)
    for (month, fuel), (kwh, cost) in sorted(
        totals.items(), key=lambda i: (i[0][0], order.index(i[0][1]))
    ):
        yield [
            month.isoformat(),
            fuel.value,
            kwh_text(kwh),
            _gbp(None if cost is None else MilliPence(cost)),
        ]


def _daily(conn: sqlite3.Connection, span: DateRange) -> Iterator[list[str]]:
    found = []
    for fuel in Fuel:
        flags = queries.day_facts(conn, fuel, span)
        for day in queries.daily(conn, fuel, span):
            facts = flags.get(day.day)
            flagged = facts is not None and facts.has_half_hourly_flag
            found.append(
                (
                    day.day,
                    list(Fuel).index(fuel),
                    [
                        day.day.isoformat(),
                        fuel.value,
                        kwh_text(day.kwh),
                        _gbp(day.usage_cost),
                        _pence(day.standing),
                        _pence(day.unit_rate),
                        "true" if flagged else "false",
                    ],
                )
            )
    for _, _, row in sorted(found, key=lambda item: item[:2]):
        yield row


def _half_hourly(conn: sqlite3.Connection, fuel: Fuel, span: DateRange) -> Iterator[list[str]]:
    start, end = london_day_bounds(span.first)[0], london_day_bounds(span.last)[1]
    cursor = conn.execute(
        "SELECT interval_start_utc, sum(kwh) FROM usage_half_hourly"
        " WHERE fuel = ? AND interval_start_utc >= ? AND interval_start_utc < ?"
        " GROUP BY interval_start_utc ORDER BY interval_start_utc",
        (fuel.value, format_utc(start), format_utc(end)),
    )
    for stamp, kwh in cursor:
        yield [stamp, parse_utc(stamp).astimezone(LONDON).isoformat(), kwh_text(kwh)]


def _readings(conn: sqlite3.Connection, span: DateRange) -> Iterator[list[str]]:
    for choice in readings.choices(conn, span):
        series = choice.series
        for reading in series.readings:
            if reading.read_date in span:
                yield [
                    series.fuel.value,
                    choice.meter,
                    series.register_id,
                    series.unit or "",
                    series.meter_status or "",
                    reading.read_date.isoformat(),
                    format(reading.value, "f"),
                    reading.source or "",
                    reading.lifecycle or "",
                    reading.read_type or "",
                ]


def _tariff_comparison(conn: sqlite3.Connection, span: DateRange) -> Iterator[list[str]]:
    stored = tariff_store.all_tariffs(conn)
    for fuel in Fuel:
        columns = columns_for(stored, fuel)
        if not columns:
            continue
        result = compare(queries.costing_days(conn, fuel, span), columns)
        if not result.included:
            continue
        per_month: dict[date, int] = {}
        for day in result.included:
            per_month[day.replace(day=1)] = per_month.get(day.replace(day=1), 0) + 1
        everything = [(BASELINE_NAME, result.actual), *((c.name, c.totals) for c in result.columns)]
        for name, totals in everything:
            yield from _month_rows(fuel, name, totals, per_month)


def _month_rows(
    fuel: Fuel, name: str, totals: Totals, days_by_month: dict[date, int]
) -> Iterator[list[str]]:
    for month, total in totals.by_month.items():
        yield [
            fuel.value,
            name,
            month.isoformat(),
            str(days_by_month[month]),
            _gbp(totals.usage_by_month[month]),
            _gbp(totals.standing_by_month[month]),
            _gbp(total),
        ]


# -- encoding -----------------------------------------------------------------------------


def csv_chunks(
    dataset: Dataset, data: Iterable[list[str]], chunk_rows: int = CHUNK_ROWS
) -> Iterator[bytes]:
    """The file's bytes, in pieces: the byte-order mark and header first, then the rows."""
    header = COLUMNS[dataset]
    guarded = {i for i, name in enumerate(header) if name in TEXT_COLUMNS.get(dataset, frozenset())}
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)

    def drain() -> bytes:
        piece = buffer.getvalue().encode("utf-8")
        buffer.seek(0)
        buffer.truncate()
        return piece

    writer.writerow(header)
    yield BYTE_ORDER_MARK + drain()
    for number, row in enumerate(data, start=1):
        writer.writerow([defend(c) if i in guarded else c for i, c in enumerate(row)])
        if number % chunk_rows == 0:
            yield drain()
    tail = drain()
    if tail:
        yield tail


class _Sink(io.RawIOBase):
    """Collects what a `ZipFile` writes, so it can be handed on piece by piece."""

    def __init__(self) -> None:
        self._pieces: list[bytes] = []

    def write(self, data: bytes | bytearray | memoryview, /) -> int:  # type: ignore[override]
        self._pieces.append(bytes(data))
        return len(data)

    def drain(self) -> Iterator[bytes]:
        pieces, self._pieces = self._pieces, []
        yield from (p for p in pieces if p)


def zip_chunks(
    conn: sqlite3.Connection, span: DateRange, today: date, chunk_rows: int = CHUNK_ROWS
) -> Iterator[bytes]:
    """Every dataset as a CSV in one zip, written as a stream (nothing is held back)."""
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED) as archive:
        for dataset in Dataset:
            with archive.open(filename(dataset, span), "w") as member:
                for chunk in csv_chunks(dataset, rows(conn, dataset, span, today), chunk_rows):
                    member.write(chunk)
                    yield from sink.drain()
        yield from sink.drain()
    yield from sink.drain()


@dataclass(frozen=True, slots=True)
class Listing:
    dataset: Dataset
    title: str
    description: str
    rows: int


def listing(conn: sqlite3.Connection, span: DateRange, today: date) -> list[Listing]:
    return [Listing(d, TITLES[d], DESCRIPTIONS[d], count(conn, d, span, today)) for d in Dataset]

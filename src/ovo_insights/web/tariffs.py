"""The Tariffs page: turning the editor's form into a tariff and back, and the comparison
shown beside it.

The editor is a plain form (no script). A band is a row of fields named `band<i>_start`,
`_end`, `_rate` and `_day<d>` (a checkbox, 0 = Monday), because the form parser keeps single
values only. A row with no times and no rate is ignored.
"""

import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from ovo_insights.analysis import queries
from ovo_insights.analysis.recost import (
    SKIP_LABELS,
    Comparison,
    Skip,
    columns_for,
    compare,
)
from ovo_insights.analysis.usage import pounds_text
from ovo_insights.domain import Fuel
from ovo_insights.money import MilliPence
from ovo_insights.storage import tariffs as stored
from ovo_insights.sync.plan import DateRange
from ovo_insights.tariffs import (
    SLOTS_PER_DAY,
    WEEKDAY_NAMES,
    BandDraft,
    FieldError,
    Tariff,
    TariffDraft,
    slot_label,
)

MAX_ROWS = 10  # band rows the form can hold
MIN_ROWS = 4
DAY_LABELS = tuple(name[:3] for name in WEEKDAY_NAMES)
_ROW_FIELD = re.compile(r"bands\[(\d+)\]\.(start|end|rate|weekdays)", re.ASCII)


def pence_text(amount: MilliPence) -> str:
    """Milli-pence as pence, trimmed: 24440 -> `24.44`, 5000 -> `5`."""
    return format((Decimal(amount) / 1000).normalize(), "f")


# -- the form -----------------------------------------------------------------------------

# Starting points with the rates left blank (the PRD asks for 2-3).
TEMPLATES: dict[str, tuple[str, Fuel, tuple[tuple[str, str, tuple[int, ...]], ...]]] = {
    "flat": ("Flat rate", Fuel.ELECTRICITY, (("00:00", "00:00", tuple(range(7))),)),
    "economy7": (
        "Economy 7",
        Fuel.ELECTRICITY,
        (
            ("00:30", "07:30", tuple(range(7))),  # night
            ("07:30", "00:30", tuple(range(7))),  # day, wrapping past midnight
        ),
    ),
    "ev": (
        "EV overnight",
        Fuel.ELECTRICITY,
        (
            ("00:00", "05:00", tuple(range(7))),  # cheap overnight charging
            ("05:00", "00:00", tuple(range(7))),  # everything else
        ),
    ),
    "gas": ("Gas", Fuel.GAS, (("00:00", "00:00", tuple(range(7))),)),
}


def _row_values(index: int, start: str, end: str, rate: str, days: Iterable[int]) -> dict[str, str]:
    values = {f"band{index}_start": start, f"band{index}_end": end, f"band{index}_rate": rate}
    values.update({f"band{index}_day{d}": "1" for d in days})
    return values


def template_values(key: str) -> dict[str, str]:
    name, fuel, rows = TEMPLATES[key]
    values = {
        "name": name,
        "fuel": fuel.value,
        "standing": "",
        "effective_from": "",
        "effective_to": "",
    }
    for index, (start, end, days) in enumerate(rows):
        values |= _row_values(index, start, end, "", days)
    return values


def tariff_values(tariff: Tariff) -> dict[str, str]:
    """The form's fields for a stored tariff. Bands that were split at midnight are joined
    again, so "07:30 to 00:30" shows as one row."""
    values = {
        "name": tariff.name,
        "fuel": tariff.fuel.value,
        "standing": pence_text(tariff.standing_charge),
        "effective_from": tariff.effective_from.isoformat() if tariff.effective_from else "",
        "effective_to": tariff.effective_to.isoformat() if tariff.effective_to else "",
    }
    for index, (start, end, rate, days) in enumerate(display_rows(tariff)):
        values |= _row_values(index, slot_label(start), _end_label(end), pence_text(rate), days)
    return values


def _end_label(slot: int) -> str:
    return slot_label(0 if slot == SLOTS_PER_DAY else slot)


def display_rows(tariff: Tariff) -> list[tuple[int, int, MilliPence, frozenset[int]]]:
    """Bands as (start slot, end slot, rate, weekdays), with touching bands of the same rate
    and days merged, and a band that ends at midnight joined to one that starts there."""
    groups: dict[tuple[MilliPence, frozenset[int]], list[tuple[int, int]]] = {}
    for band in tariff.bands:
        groups.setdefault((band.unit_rate, band.weekdays), []).append(
            (band.start_slot, band.end_slot)
        )
    rows = []
    for (rate, days), ranges in groups.items():
        merged: list[tuple[int, int]] = []
        for start, end in sorted(ranges):
            if merged and merged[-1][1] == start:
                merged[-1] = (merged[-1][0], end)
            else:
                merged.append((start, end))
        if len(merged) > 1 and merged[0][0] == 0 and merged[-1][1] == SLOTS_PER_DAY:
            first, last = merged[0], merged.pop()
            merged[0] = (last[0], first[1])  # wraps: starts late, ends early the next morning
        rows += [(start, end, rate, days) for start, end in merged]
    return sorted(rows, key=lambda r: (min(r[0], r[1]), r[2]))


def row_count(values: Mapping[str, str]) -> int:
    """Rows to show: those in use plus two spare, within the form's limits."""
    used = [i for i in range(MAX_ROWS) if _row_used(values, i)]
    return min(MAX_ROWS, max(MIN_ROWS, (max(used) + 3) if used else MIN_ROWS))


def _row_used(values: Mapping[str, str], index: int) -> bool:
    return any(values.get(f"band{index}_{key}", "").strip() for key in ("start", "end", "rate"))


def parse_form(values: Mapping[str, str]) -> tuple[TariffDraft, list[int]]:
    """The draft a submitted form describes, and which form row each draft band came from."""
    rows = [i for i in range(MAX_ROWS) if _row_used(values, i)]
    bands = tuple(
        BandDraft(
            values.get(f"band{i}_start", ""),
            values.get(f"band{i}_end", ""),
            values.get(f"band{i}_rate", ""),
            tuple(d for d in range(7) if values.get(f"band{i}_day{d}")),
        )
        for i in rows
    )
    draft = TariffDraft(
        values.get("name", ""),
        values.get("fuel", ""),
        values.get("standing", ""),
        values.get("effective_from", ""),
        values.get("effective_to", ""),
        bands,
    )
    return draft, rows


@dataclass(frozen=True, slots=True)
class FormErrors:
    by_field: dict[str, str]  # form field name -> first message
    general: tuple[str, ...]  # about the bands as a whole (gaps, overlaps)

    def __bool__(self) -> bool:
        return bool(self.by_field or self.general)


def form_errors(errors: Iterable[FieldError], rows: list[int]) -> FormErrors:
    """Map the rules' field paths (`bands[1].rate`) onto the form's fields (`band3_rate`)."""
    by_field: dict[str, str] = {}
    general: list[str] = []
    for error in errors:
        match = _ROW_FIELD.fullmatch(error.field)
        if match is not None:
            key = {"weekdays": "days"}.get(match.group(2), match.group(2))
            by_field.setdefault(f"band{rows[int(match.group(1))]}_{key}", error.message)
        elif error.field == "bands":
            general.append(error.message)
        else:
            by_field.setdefault(error.field, error.message)
    return FormErrors(by_field, tuple(dict.fromkeys(general)))


# -- the list and the comparison ----------------------------------------------------------


def describe_rates(tariff: Tariff) -> str:
    rates = sorted({b.unit_rate for b in tariff.bands})
    if len(rates) == 1:
        return f"{pence_text(rates[0])}p/kWh"
    return f"{len(rates)} rates, {pence_text(rates[0])}p to {pence_text(rates[-1])}p/kWh"


def describe_dates(tariff: Tariff) -> str:
    start, end = tariff.effective_from, tariff.effective_to
    if start is None and end is None:
        return "always"
    return f"{_day(start) if start else 'start'} to {_day(end) if end else 'now'}"


def _day(day: date) -> str:
    return f"{day.day} {day:%b %Y}"


@dataclass(frozen=True, slots=True)
class FuelView:
    fuel: Fuel
    comparison: Comparison
    tariffs: list[Tariff]
    excluded: list[tuple[str, int]]  # reason, days


def fuel_view(conn: sqlite3.Connection, fuel: Fuel, span: DateRange) -> FuelView | None:
    """The comparison for one fuel over `span`, or None if it has no tariffs to compare."""
    tariffs = stored.all_tariffs(conn, fuel)
    if not tariffs:
        return None
    days = queries.costing_days(conn, fuel, span)
    result = compare(days, columns_for(tariffs, fuel))
    return FuelView(fuel, result, tariffs, reason_counts(result.excluded_counts))


def reason_counts(counts: Mapping[Skip, int]) -> list[tuple[str, int]]:
    return [(SKIP_LABELS[reason], n) for reason, n in counts.items()]


def chart_data(view: FuelView) -> dict[str, Any]:
    """Totals per column as exact decimal strings, usage and standing stacked."""
    result = view.comparison
    names = ["What OVO charged", *(c.name for c in result.columns)]
    totals = [result.actual, *(c.totals for c in result.columns)]
    return {
        "fuel": view.fuel.value,
        "labels": names,
        "usage": [pounds_text(t.usage) for t in totals],
        "standing": [pounds_text(t.standing) for t in totals],
        "total": [pounds_text(t.total) for t in totals],
        "days": len(result.included),
    }

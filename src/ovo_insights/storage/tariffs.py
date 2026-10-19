"""Saving and loading tariffs. Writes check the rules that SQL can't, inside one transaction."""

import sqlite3
from datetime import date, datetime

from ovo_insights.domain import Fuel
from ovo_insights.money import MilliPence
from ovo_insights.storage.db import transaction
from ovo_insights.tariffs import Band, Tariff, TariffError, check_versions
from ovo_insights.timeutil import format_utc

_WEEKDAYS = range(7)


def _mask(weekdays: frozenset[int]) -> int:
    return sum(1 << d for d in weekdays)


def _days(mask: int) -> frozenset[int]:
    return frozenset(d for d in _WEEKDAYS if mask & (1 << d))


def add(conn: sqlite3.Connection, tariff: Tariff, now: datetime) -> int:
    """Store a new tariff and return its id. Raises `TariffError` if another version of the
    same tariff already covers some of its dates."""
    with transaction(conn):
        _require_free_dates(conn, tariff)
        cursor = conn.execute(
            "INSERT INTO tariffs (name, fuel, standing_charge_milli_pence, effective_from,"
            " effective_to, created_at_utc, updated_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                tariff.name,
                tariff.fuel.value,
                tariff.standing_charge,
                _iso(tariff.effective_from),
                _iso(tariff.effective_to),
                format_utc(now),
                format_utc(now),
            ),
        )
        tariff_id = cursor.lastrowid
        if tariff_id is None:  # pragma: no cover - sqlite always sets it after an INSERT
            raise RuntimeError("Could not store the tariff")
        _write_bands(conn, tariff_id, tariff)
    return tariff_id


def replace(conn: sqlite3.Connection, tariff_id: int, tariff: Tariff, now: datetime) -> None:
    """Overwrite a stored tariff (and all its bands). Raises `KeyError` if it doesn't exist."""
    with transaction(conn):
        if (
            conn.execute("SELECT 1 FROM tariffs WHERE tariff_id = ?", (tariff_id,)).fetchone()
            is None
        ):
            raise KeyError(tariff_id)
        _require_free_dates(conn, tariff, ignoring=tariff_id)
        conn.execute(
            "UPDATE tariffs SET name = ?, fuel = ?, standing_charge_milli_pence = ?,"
            " effective_from = ?, effective_to = ?, updated_at_utc = ? WHERE tariff_id = ?",
            (
                tariff.name,
                tariff.fuel.value,
                tariff.standing_charge,
                _iso(tariff.effective_from),
                _iso(tariff.effective_to),
                format_utc(now),
                tariff_id,
            ),
        )
        conn.execute("DELETE FROM tariff_bands WHERE tariff_id = ?", (tariff_id,))
        _write_bands(conn, tariff_id, tariff)


def remove(conn: sqlite3.Connection, tariff_id: int) -> bool:
    """Delete a tariff and its bands. False if there was no such tariff."""
    with transaction(conn):
        cursor = conn.execute("DELETE FROM tariffs WHERE tariff_id = ?", (tariff_id,))
    return cursor.rowcount > 0


def get(conn: sqlite3.Connection, tariff_id: int) -> Tariff | None:
    found = [t for t in all_tariffs(conn) if t.tariff_id == tariff_id]
    return found[0] if found else None


def all_tariffs(conn: sqlite3.Connection, fuel: Fuel | None = None) -> list[Tariff]:
    """Every stored tariff (or one fuel's), by fuel, name and start date. Validated on read."""
    bands: dict[int, list[Band]] = {}
    for tariff_id, start, end, rate, mask in conn.execute(
        "SELECT tariff_id, start_slot, end_slot, unit_rate_milli_pence, weekdays"
        " FROM tariff_bands ORDER BY tariff_id, band_no"
    ):
        bands.setdefault(tariff_id, []).append(Band(start, end, MilliPence(rate), _days(mask)))
    result = []
    for tariff_id, name, fuel_text, standing, start_date, end_date in conn.execute(
        "SELECT tariff_id, name, fuel, standing_charge_milli_pence, effective_from, effective_to"
        " FROM tariffs ORDER BY fuel, name COLLATE NOCASE, effective_from, tariff_id"
    ):
        if fuel is not None and fuel_text != fuel.value:
            continue
        result.append(
            Tariff(
                name,
                Fuel(fuel_text),
                MilliPence(standing),
                tuple(bands.get(tariff_id, [])),
                _date(start_date),
                _date(end_date),
                tariff_id,
            )
        )
    return result


def _require_free_dates(
    conn: sqlite3.Connection, tariff: Tariff, ignoring: int | None = None
) -> None:
    others = [t for t in all_tariffs(conn, tariff.fuel) if t.tariff_id != ignoring]
    errors = check_versions(others, tariff)
    if errors:
        raise TariffError(errors)


def _write_bands(conn: sqlite3.Connection, tariff_id: int, tariff: Tariff) -> None:
    conn.executemany(
        "INSERT INTO tariff_bands (tariff_id, band_no, start_slot, end_slot,"
        " unit_rate_milli_pence, weekdays) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (tariff_id, number, b.start_slot, b.end_slot, b.unit_rate, _mask(b.weekdays))
            for number, b in enumerate(tariff.bands)
        ],
    )


def _iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _date(value: str | None) -> date | None:
    return None if value is None else date.fromisoformat(value)

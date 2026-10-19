"""Storing tariffs: round-trips, the SQL constraints, and the checks SQL can't make."""

import sqlite3
from datetime import UTC, date, datetime

import pytest

from ovo_insights.domain import Fuel
from ovo_insights.money import MilliPence
from ovo_insights.storage import tariffs as stored
from ovo_insights.storage.migrator import apply_migrations
from ovo_insights.tariffs import BandDraft, Tariff, TariffDraft, TariffError, build_tariff

NOW = datetime(2026, 10, 14, 9, tzinfo=UTC)
LATER = datetime(2026, 10, 15, 9, tzinfo=UTC)


def economy7(name: str = "Economy 7", **kw: str) -> Tariff:
    every = tuple(range(7))
    return build_tariff(
        TariffDraft(
            name,
            kw.get("fuel", "electricity"),
            "47.84",
            kw.get("effective_from", ""),
            kw.get("effective_to", ""),
            (BandDraft("00:30", "07:30", "10.5", every), BandDraft("07:30", "00:30", "30", every)),
        )
    )


def flat(name: str, start: date | None = None, end: date | None = None) -> Tariff:
    return Tariff.flat(name, Fuel.GAS, MilliPence(5_550), MilliPence(23_950), start, end)


def test_a_tariff_round_trips_with_its_bands_weekdays_and_dates(conn: sqlite3.Connection) -> None:
    tariff = economy7(effective_from="2026-04-01", effective_to="2026-09-30")
    tariff_id = stored.add(conn, tariff, NOW)
    [found] = stored.all_tariffs(conn)
    assert found.tariff_id == tariff_id
    assert (found.name, found.fuel, found.standing_charge) == (
        "Economy 7",
        Fuel.ELECTRICITY,
        47_840,
    )
    assert (found.effective_from, found.effective_to) == (date(2026, 4, 1), date(2026, 9, 30))
    assert found.bands == tariff.bands  # three bands: the wrapping one was split
    assert [found.rate_at(2, s) for s in (0, 1, 14, 15)] == [30_000, 10_500, 10_500, 30_000]
    assert stored.get(conn, tariff_id) == found
    assert stored.get(conn, tariff_id + 1) is None


def test_weekday_sets_survive_the_bitmask(conn: sqlite3.Connection) -> None:
    tariff = build_tariff(
        TariffDraft(
            "Split week",
            "electricity",
            "0",
            "",
            "",
            (
                BandDraft("00:00", "00:00", "10", (0, 2, 4)),
                BandDraft("00:00", "00:00", "20", (1, 3)),
                BandDraft("00:00", "00:00", "30", (5, 6)),
            ),
        )
    )
    stored.add(conn, tariff, NOW)
    [found] = stored.all_tariffs(conn)
    assert [found.rate_at(d, 0) for d in range(7)] == [
        10_000,
        20_000,
        10_000,
        20_000,
        10_000,
        30_000,
        30_000,
    ]


def test_listing_by_fuel_is_ordered_and_filtered(conn: sqlite3.Connection) -> None:
    stored.add(conn, flat("b"), NOW)
    stored.add(conn, economy7("Zed"), NOW)
    stored.add(conn, flat("A", date(2026, 6, 1)), NOW)
    stored.add(conn, flat("A", None, date(2026, 5, 31)), NOW)
    assert [t.name for t in stored.all_tariffs(conn)] == ["Zed", "A", "A", "b"]  # electricity first
    gas = stored.all_tariffs(conn, Fuel.GAS)
    assert [(t.name, t.effective_from) for t in gas] == [
        ("A", None),
        ("A", date(2026, 6, 1)),
        ("b", None),
    ]
    assert [t.name for t in stored.all_tariffs(conn, Fuel.ELECTRICITY)] == ["Zed"]


def test_overlapping_versions_are_refused_and_nothing_is_written(conn: sqlite3.Connection) -> None:
    stored.add(conn, flat("Cap", date(2026, 1, 1), date(2026, 3, 31)), NOW)
    with pytest.raises(TariffError) as caught:
        stored.add(conn, flat("cap", date(2026, 3, 1), None), NOW)
    assert caught.value.errors[0].field == "effective_from"
    assert len(stored.all_tariffs(conn)) == 1
    assert conn.execute("SELECT count(*) FROM tariff_bands").fetchone()[0] == 1
    stored.add(conn, flat("Cap", date(2026, 4, 1), None), NOW)  # the next version is fine
    assert len(stored.all_tariffs(conn)) == 2


def test_replacing_overwrites_bands_and_ignores_its_own_old_dates(conn: sqlite3.Connection) -> None:
    first = stored.add(conn, flat("Cap", date(2026, 1, 1), date(2026, 3, 31)), NOW)
    stored.add(conn, flat("Cap", date(2026, 4, 1), None), NOW)
    stored.replace(conn, first, flat("Cap", date(2025, 6, 1), date(2026, 3, 31)), LATER)
    found = stored.get(conn, first)
    assert found is not None
    assert found.effective_from == date(2025, 6, 1)
    assert (
        conn.execute("SELECT count(*) FROM tariff_bands WHERE tariff_id = ?", (first,)).fetchone()[
            0
        ]
        == 1
    )
    # Moving it onto the other version's dates is refused, and leaves it as it was.
    with pytest.raises(TariffError):
        stored.replace(conn, first, flat("Cap", date(2026, 3, 1), None), LATER)
    again = stored.get(conn, first)
    assert again is not None
    assert again.effective_to == date(2026, 3, 31)
    # A tariff can change from one band to several.
    stored.replace(conn, first, economy7("Cap", fuel="electricity"), LATER)
    changed = stored.get(conn, first)
    assert changed is not None
    assert (changed.fuel, len(changed.bands)) == (Fuel.ELECTRICITY, 3)
    row = conn.execute(
        "SELECT created_at_utc, updated_at_utc FROM tariffs WHERE tariff_id = ?", (first,)
    ).fetchone()
    assert row == ("2026-10-14T09:00:00Z", "2026-10-15T09:00:00Z")


def test_replacing_a_missing_tariff_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(KeyError):
        stored.replace(conn, 99, flat("x"), NOW)


def test_removing_deletes_the_bands_too(conn: sqlite3.Connection) -> None:
    keep = stored.add(conn, flat("Keep"), NOW)
    gone = stored.add(conn, economy7(), NOW)
    assert stored.remove(conn, gone) is True
    assert stored.remove(conn, gone) is False
    assert [t.tariff_id for t in stored.all_tariffs(conn)] == [keep]
    assert conn.execute("SELECT count(*) FROM tariff_bands").fetchone()[0] == 1


def test_failed_write_rolls_back_inside_a_callers_transaction(conn: sqlite3.Connection) -> None:
    stored.add(conn, flat("Cap"), NOW)
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(TariffError):
        stored.add(conn, flat("Cap"), NOW)
    conn.execute("ROLLBACK")
    assert len(stored.all_tariffs(conn)) == 1


# -- constraints in SQL, and corrupt rows -------------------------------------------------


def insert_tariff(conn: sqlite3.Connection, **override: object) -> None:
    row = {
        "name": "t",
        "fuel": "gas",
        "standing": 0,
        "frm": None,
        "to": None,
        "created": "2026-10-14T09:00:00Z",
        **override,
    }
    conn.execute(
        "INSERT INTO tariffs (name, fuel, standing_charge_milli_pence, effective_from,"
        " effective_to, created_at_utc, updated_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            row["name"],
            row["fuel"],
            row["standing"],
            row["frm"],
            row["to"],
            row["created"],
            row["created"],
        ),
    )


@pytest.mark.parametrize(
    "override",
    [
        {"name": ""},
        {"name": "x" * 65},
        {"fuel": "steam"},
        {"standing": -1},
        {"standing": 1_000_001},
        {"frm": "2026-02-30"},
        {"frm": "2026-05-01", "to": "2026-04-30"},
        {"to": "not a date"},
        {"created": "2026-10-14 09:00"},
    ],
)
def test_the_tariffs_table_rejects_bad_rows(
    conn: sqlite3.Connection, override: dict[str, object]
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        insert_tariff(conn, **override)


@pytest.mark.parametrize(
    "values",
    [
        (0, 5, 5, 0, 127),  # empty
        (0, -1, 5, 0, 127),
        (0, 0, 49, 0, 127),
        (0, 0, 48, 500_001, 127),
        (0, 0, 48, -1, 127),
        (0, 0, 48, 0, 0),
        (0, 0, 48, 0, 128),
        (-1, 0, 48, 0, 127),
    ],
)
def test_the_bands_table_rejects_bad_rows(
    conn: sqlite3.Connection, values: tuple[int, ...]
) -> None:
    insert_tariff(conn)
    band_no, start, end, rate, days = values
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tariff_bands VALUES (1, ?, ?, ?, ?, ?)", (band_no, start, end, rate, days)
        )


def test_bands_need_a_tariff_and_follow_it_on_delete(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO tariff_bands VALUES (42, 0, 0, 48, 0, 127)")


def test_a_hand_edited_row_that_breaks_the_rules_fails_loudly_on_read(
    conn: sqlite3.Connection,
) -> None:
    tariff_id = stored.add(conn, flat("Cap"), NOW)
    conn.execute("UPDATE tariff_bands SET end_slot = 40 WHERE tariff_id = ?", (tariff_id,))
    with pytest.raises(TariffError, match="no rate"):
        stored.all_tariffs(conn)


def test_a_tariff_without_bands_is_refused_on_read(conn: sqlite3.Connection) -> None:
    insert_tariff(conn)
    with pytest.raises(TariffError, match="Add at least one band"):
        stored.all_tariffs(conn)


def test_the_migration_is_part_of_the_packaged_set_and_idempotent(conn: sqlite3.Connection) -> None:
    apply_migrations(conn)  # a second run changes nothing
    names = [r[0] for r in conn.execute("SELECT name FROM schema_migrations ORDER BY version")]
    assert "tariffs" in names[-1]

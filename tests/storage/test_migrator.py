import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from ovo_insights.paths import ensure_private_dir
from ovo_insights.storage.db import connect
from ovo_insights.storage.migrator import (
    Migration,
    MigrationError,
    apply_migrations,
    load_migrations,
    packaged_migrations,
    schema_version,
)

ONE = ("0001_one.sql", "CREATE TABLE one (x INTEGER) STRICT;")
TWO = ("0002_two.sql", "CREATE TABLE two (y INTEGER) STRICT;")


@pytest.fixture
def bare(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(ensure_private_dir(tmp_path / "d") / "m.sqlite3")
    try:
        yield conn
    finally:
        conn.close()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")}


def test_packaged_migrations_load_and_start_at_one() -> None:
    migrations = packaged_migrations()

    assert migrations[0].version == 1
    assert migrations[0].name == "initial"


def test_applies_pending_then_is_idempotent(bare: sqlite3.Connection) -> None:
    migrations = load_migrations([ONE, TWO])

    assert apply_migrations(bare, migrations) == [1, 2]
    assert apply_migrations(bare, migrations) == []
    assert schema_version(bare) == 2
    assert {"one", "two", "schema_migrations"} <= _tables(bare)


def test_applies_only_new_migrations(bare: sqlite3.Connection) -> None:
    apply_migrations(bare, load_migrations([ONE]))

    assert apply_migrations(bare, load_migrations([ONE, TWO])) == [2]


def test_failed_migration_rolls_back_completely(bare: sqlite3.Connection) -> None:
    broken = ("0002_broken.sql", "CREATE TABLE half (z INTEGER) STRICT;\nNOT VALID SQL;")

    with pytest.raises(sqlite3.OperationalError):
        apply_migrations(bare, load_migrations([ONE, broken]))

    assert "half" not in _tables(bare)
    assert schema_version(bare) == 1
    assert not bare.in_transaction


def test_edited_migration_is_detected(bare: sqlite3.Connection) -> None:
    apply_migrations(bare, load_migrations([ONE]))
    edited = ("0001_one.sql", ONE[1] + "\n-- sneaky edit")

    with pytest.raises(MigrationError, match="changed since it was applied"):
        apply_migrations(bare, load_migrations([edited]))


def test_renamed_migration_is_detected(bare: sqlite3.Connection) -> None:
    apply_migrations(bare, load_migrations([ONE]))

    with pytest.raises(MigrationError, match="changed since it was applied"):
        apply_migrations(bare, load_migrations([("0001_renamed.sql", ONE[1])]))


def test_database_from_newer_version_is_refused(bare: sqlite3.Connection) -> None:
    apply_migrations(bare, load_migrations([ONE, TWO]))

    with pytest.raises(MigrationError, match="newer than this version"):
        apply_migrations(bare, load_migrations([ONE]))


def test_refuses_to_run_inside_a_transaction(bare: sqlite3.Connection) -> None:
    bare.execute("BEGIN")

    with pytest.raises(MigrationError, match="inside a transaction"):
        apply_migrations(bare, load_migrations([ONE]))


@pytest.mark.parametrize(
    "entries",
    [
        [("1_one.sql", "")],
        [("0001_One.sql", "")],
        [("0001_one.txt", "")],
        [("0002_two.sql", "")],
        [ONE, ("0003_three.sql", "")],
        [ONE, ("0001_dup.sql", "")],
    ],
)
def test_bad_migration_sets_are_rejected(entries: list[tuple[str, str]]) -> None:
    with pytest.raises(MigrationError):
        load_migrations(entries)


@pytest.mark.parametrize("statement", ["COMMIT;", "begin transaction;", "  ROLLBACK;", "END;"])
def test_transaction_control_in_migration_is_rejected(statement: str) -> None:
    with pytest.raises(MigrationError, match="transaction control"):
        load_migrations([("0001_one.sql", f"CREATE TABLE t (x INTEGER);\n{statement}")])


def test_checksum_is_sha256_of_sql() -> None:
    assert len(Migration(1, "x", "SELECT 1").checksum) == 64
    assert Migration(1, "x", "a").checksum != Migration(1, "x", "b").checksum

import os
import sqlite3
import stat
import threading
from pathlib import Path

import pytest

from ovo_insights.paths import InsecurePathError, ensure_private_dir
from ovo_insights.storage import open_database
from ovo_insights.storage.db import DatabaseError, connect, connect_read_only, transaction


def _pragma(conn: sqlite3.Connection, name: str) -> object:
    return conn.execute(f"PRAGMA {name}").fetchone()[0]


def test_connect_applies_safe_pragmas(tmp_path: Path) -> None:
    conn = connect(ensure_private_dir(tmp_path / "d") / "x.sqlite3")
    try:
        assert _pragma(conn, "foreign_keys") == 1
        assert _pragma(conn, "journal_mode") == "wal"
        assert _pragma(conn, "trusted_schema") == 0
        assert _pragma(conn, "busy_timeout") == 5000
    finally:
        conn.close()


def test_connect_refuses_old_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 36, 0))

    with pytest.raises(DatabaseError, match="too old"):
        connect(tmp_path / "x.sqlite3")


def test_connect_refuses_world_readable_database(tmp_path: Path) -> None:
    path = ensure_private_dir(tmp_path / "d") / "x.sqlite3"
    path.touch()
    path.chmod(0o644)

    with pytest.raises(InsecurePathError):
        connect(path)


def test_wal_side_files_are_private(data_dir: Path) -> None:
    conn = open_database(data_dir)
    try:
        conn.execute("INSERT INTO accounts (account_id) VALUES ('1')")
        side_files = [p for p in data_dir.iterdir() if p.name.endswith(("-wal", "-shm"))]
        assert side_files, "expected WAL side files while the connection is open"
        for path in [*side_files, *data_dir.glob("*.sqlite3")]:
            assert stat.S_IMODE(os.stat(path).st_mode) & 0o077 == 0, path
    finally:
        conn.close()


def test_transaction_commits(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        conn.execute("INSERT INTO accounts (account_id) VALUES ('1')")

    assert not conn.in_transaction
    assert conn.execute("SELECT count(*) FROM accounts").fetchone()[0] == 1


def test_transaction_rolls_back_on_error(conn: sqlite3.Connection) -> None:
    def insert_then_fail() -> None:
        with transaction(conn):
            conn.execute("INSERT INTO accounts (account_id) VALUES ('1')")
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        insert_then_fail()

    assert not conn.in_transaction
    assert conn.execute("SELECT count(*) FROM accounts").fetchone()[0] == 0


def test_nested_transaction_joins_outer(conn: sqlite3.Connection) -> None:
    def inner_succeeds_outer_fails() -> None:
        with transaction(conn):
            with transaction(conn):
                conn.execute("INSERT INTO accounts (account_id) VALUES ('1')")
            raise RuntimeError("outer fails after inner succeeded")

    with pytest.raises(RuntimeError):
        inner_succeeds_outer_fails()

    # The inner block must not have committed on its own.
    assert conn.execute("SELECT count(*) FROM accounts").fetchone()[0] == 0


def test_foreign_keys_are_enforced(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            "INSERT INTO meters (meter_serial, account_id, fuel) VALUES ('S', 'nope', 'gas')"
        )


def test_open_database_closes_connection_if_migration_fails(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []
    real_connect = connect

    def tracking_connect(path: Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
        conn = real_connect(path, check_same_thread=check_same_thread)

        class Tracked:
            def __getattr__(self, name: str) -> object:
                return getattr(conn, name)

            def close(self) -> None:
                closed.append(True)
                conn.close()

        return Tracked()  # type: ignore[return-value]

    def failing_migrations(conn: sqlite3.Connection) -> list[int]:
        raise RuntimeError("migration failed")

    import ovo_insights.storage as storage

    monkeypatch.setattr(storage, "connect", tracking_connect)
    monkeypatch.setattr(storage, "apply_migrations", failing_migrations)

    with pytest.raises(RuntimeError, match="migration failed"):
        open_database(data_dir)
    assert closed == [True]


def test_read_only_connection_never_creates_or_writes(data_dir: Path) -> None:
    from ovo_insights.paths import DATABASE_FILENAME

    path = data_dir / DATABASE_FILENAME
    with pytest.raises(FileNotFoundError):
        connect_read_only(path)
    assert not path.exists()

    open_database(data_dir).close()
    conn = connect_read_only(path)
    try:
        assert conn.execute("SELECT count(*) FROM accounts").fetchone() == (0,)
        with pytest.raises(sqlite3.OperationalError, match=r"readonly|read-only"):
            conn.execute("INSERT INTO accounts (account_id) VALUES ('a')")
    finally:
        conn.close()


def test_a_connection_can_be_handed_between_threads_only_when_asked(tmp_path: Path) -> None:
    results: dict[str, object] = {}

    def use(name: str, conn: sqlite3.Connection) -> None:
        try:
            results[name] = conn.execute("SELECT 1").fetchone()[0]
        except sqlite3.ProgrammingError as exc:
            results[name] = type(exc).__name__

    strict = open_database(tmp_path / "strict")
    relaxed = open_database(tmp_path / "relaxed", check_same_thread=False)
    try:
        for name, conn in (("strict", strict), ("relaxed", relaxed)):
            thread = threading.Thread(target=use, args=(name, conn))
            thread.start()
            thread.join()
    finally:
        strict.close()
        relaxed.close()
    assert results == {"strict": "ProgrammingError", "relaxed": 1}

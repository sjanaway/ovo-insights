"""SQLite connection setup and explicit transactions."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ovo_insights.paths import ensure_private_file

# STRICT tables need 3.37.
MIN_SQLITE_VERSION = (3, 37, 0)


class DatabaseError(RuntimeError):
    """The database could not be opened or configured safely."""


def connect(path: Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open the database with safe settings. The file is created 0600 if missing.

    `check_same_thread=False` is for a connection that is used by one thread at a time but
    handed between threads, such as a streamed download."""
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        raise DatabaseError(
            f"SQLite {sqlite3.sqlite_version} is too old; "
            f"{'.'.join(map(str, MIN_SQLITE_VERSION))} or newer is required"
        )
    ensure_private_file(path)
    # autocommit=True: we issue BEGIN/COMMIT ourselves, and executescript never commits for us.
    conn = sqlite3.connect(path, autocommit=True, check_same_thread=check_same_thread)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA trusted_schema = OFF")
        conn.execute("PRAGMA busy_timeout = 5000")
        journal_mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        conn.execute("PRAGMA synchronous = NORMAL")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise DatabaseError("Could not enable foreign key enforcement")
        if journal_mode != "wal":
            raise DatabaseError(f"Could not enable WAL mode (got {journal_mode!r})")
    except BaseException:
        conn.close()
        raise
    return conn


def connect_read_only(path: Path) -> sqlite3.Connection:
    """Open an existing database for reading only (`sync --dry-run`). Never creates it."""
    if not path.exists():
        raise FileNotFoundError(path)
    ensure_private_file(path)  # verifies owner and mode; the file exists, so nothing is created
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, autocommit=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA trusted_schema = OFF")
        conn.execute("PRAGMA busy_timeout = 5000")
    except BaseException:  # pragma: no cover - PRAGMAs on a fresh connection don't fail
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in one write transaction, or join the caller's if one is already open."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")

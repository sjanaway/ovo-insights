"""Persistence: SQLite connection, migrations and repository."""

import sqlite3
from pathlib import Path

from ovo_insights.paths import DATABASE_FILENAME, ensure_private_dir
from ovo_insights.storage.db import connect
from ovo_insights.storage.migrator import apply_migrations


def open_database(data_dir: Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open (creating if needed) the private database in `data_dir`, fully migrated."""
    ensure_private_dir(data_dir)
    conn = connect(data_dir / DATABASE_FILENAME, check_same_thread=check_same_thread)
    try:
        apply_migrations(conn)
    except BaseException:
        conn.close()
        raise
    return conn

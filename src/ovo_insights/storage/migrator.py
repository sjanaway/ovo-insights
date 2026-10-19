"""Forward-only schema migrations with checksums.

Migrations live in `storage/migrations/NNNN_name.sql`, numbered from 0001 with no gaps. Each is
applied in its own transaction together with its `schema_migrations` row. The SHA-256 of every
applied migration is stored, so editing a migration after release fails loudly instead of
leaving databases in different shapes.
"""

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files

from ovo_insights.timeutil import format_utc

_FILENAME = re.compile(r"(\d{4})_([a-z0-9_]+)\.sql", re.ASCII)
# Transactions are managed by the runner; a migration must not end or nest one.
_TRANSACTION_CONTROL = re.compile(
    r"^\s*(BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b", re.IGNORECASE | re.MULTILINE | re.ASCII
)

_CREATE_LEDGER = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY NOT NULL CHECK (version > 0),
    name TEXT NOT NULL,
    checksum TEXT NOT NULL CHECK (length(checksum) = 64),
    applied_at_utc TEXT NOT NULL
) STRICT
"""


class MigrationError(RuntimeError):
    """The migration set or the database's migration history is inconsistent."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


def packaged_migrations() -> list[Migration]:
    package = files("ovo_insights.storage.migrations")
    entries = [
        (entry.name, entry.read_text(encoding="utf-8"))
        for entry in package.iterdir()
        if entry.name.endswith(".sql")
    ]
    return load_migrations(entries)


def load_migrations(entries: list[tuple[str, str]]) -> list[Migration]:
    """Validate and order (filename, sql) pairs."""
    migrations = []
    for filename, sql in entries:
        match = _FILENAME.fullmatch(filename)
        if not match:
            raise MigrationError(f"Bad migration filename: {filename!r}")
        if _TRANSACTION_CONTROL.search(sql):
            raise MigrationError(f"{filename} must not contain transaction control statements")
        migrations.append(Migration(int(match.group(1)), match.group(2), sql))
    migrations.sort(key=lambda m: m.version)
    versions = [m.version for m in migrations]
    if versions != list(range(1, len(migrations) + 1)):
        raise MigrationError(f"Migrations must be numbered 1..N with no gaps, got {versions}")
    return migrations


def apply_migrations(
    conn: sqlite3.Connection, migrations: list[Migration] | None = None
) -> list[int]:
    """Bring the schema up to date. Returns the versions applied by this call."""
    if migrations is None:
        migrations = packaged_migrations()
    if conn.in_transaction:
        raise MigrationError("apply_migrations must not be called inside a transaction")
    conn.execute(_CREATE_LEDGER)
    _check_history(conn, migrations)

    applied_now = []
    for migration in migrations:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-check under the write lock in case another process migrated meanwhile.
            already = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?", (migration.version,)
            ).fetchone()
            if already is None:
                conn.executescript(migration.sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, checksum, applied_at_utc)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        migration.version,
                        migration.name,
                        migration.checksum,
                        format_utc(datetime.now(UTC)),
                    ),
                )
                applied_now.append(migration.version)
        except BaseException:
            # A failed statement may or may not have ended the transaction already.
            if _in_transaction(conn):
                conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    return applied_now


def _in_transaction(conn: sqlite3.Connection) -> bool:
    return conn.in_transaction


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
    return int(row[0])


def _check_history(conn: sqlite3.Connection, migrations: list[Migration]) -> None:
    known = {m.version: m for m in migrations}
    for version, name, checksum in conn.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ):
        migration = known.get(version)
        if migration is None:
            raise MigrationError(
                f"Database is at schema version {version}, newer than this version of "
                "ovo-insights understands. Upgrade ovo-insights."
            )
        if migration.checksum != checksum or migration.name != name:
            raise MigrationError(
                f"Migration {version:04d}_{name} has changed since it was applied. "
                "Released migrations must never be edited; add a new one instead."
            )

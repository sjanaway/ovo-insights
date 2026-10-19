"""`ovo-insights backup`: a consistent copy of the database, private from the first byte.

The database is in write-ahead-log mode, so while the app runs some recent writes live in side
files next to it, and copying the main file alone can miss or tear them. `VACUUM INTO` writes a
complete, compact copy from one consistent snapshot, whether or not the app is running and
without stopping a sync.

The copy is created `0600` before SQLite writes to it (an empty file is allowed as the target),
so there is never a moment when it is readable by anyone else; it is checked afterwards; and
the backup folder must be private too, like the data folder. Nothing is overwritten and nothing
is deleted unless you ask to keep only the newest few.
"""

import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ovo_insights.paths import DATABASE_FILENAME, ensure_private_dir
from ovo_insights.storage.db import connect, connect_read_only
from ovo_insights.timeutil import require_utc

PREFIX = "ovo-insights-"
SUFFIX = ".sqlite3"
# Only files with exactly this shape are ever treated as ours (and pruned).
_BACKUP_NAME = re.compile(r"ovo-insights-(\d{8}T\d{6}Z)(?:-(\d{1,3}))?\.sqlite3", re.ASCII)


class BackupError(RuntimeError):
    """The backup couldn't be made safely. The message says why and what to do."""


@dataclass(frozen=True, slots=True)
class BackupResult:
    path: Path
    size: int
    rows: dict[str, int]  # what the copy holds, by table: counts only


def default_backup_dir() -> Path:
    return Path.home() / "Backups" / "ovo-insights"


def backup_name(now: datetime) -> str:
    return f"{PREFIX}{require_utc(now):%Y%m%dT%H%M%S}Z{SUFFIX}"


def make_backup(data_dir: Path, dest_dir: Path, now: datetime) -> BackupResult:
    """Copy the database in `data_dir` into a new file in `dest_dir`."""
    source = data_dir / DATABASE_FILENAME
    if not source.exists():
        raise BackupError(f"There is no database at {source} yet: run a sync first.")
    destination = _destination_dir(data_dir, dest_dir)
    target = _create_private_file(destination, backup_name(now))
    try:
        conn = connect(source)
        try:
            conn.execute("VACUUM INTO ?", (str(target),))
        finally:
            conn.close()
        rows = _verify(target)
    except BaseException:
        target.unlink(missing_ok=True)  # never leave a half-made or failed copy behind
        raise
    return BackupResult(target, target.stat().st_size, rows)


def _destination_dir(data_dir: Path, dest_dir: Path) -> Path:
    resolved = dest_dir.expanduser().resolve()
    if resolved == data_dir.resolve() or data_dir.resolve() in resolved.parents:
        raise BackupError(
            f"Put backups outside the data folder ({data_dir}): deleting the data folder "
            "would delete them too."
        )
    return ensure_private_dir(dest_dir.expanduser())


def _create_private_file(directory: Path, name: str) -> Path:
    """A new empty `0600` file, never overwriting one (a second backup in the same second gets
    `-2`, `-3`, ...), and never following a symlink."""
    stem = name.removesuffix(SUFFIX)
    for number in range(1, 1000):
        path = directory / (name if number == 1 else f"{stem}-{number}{SUFFIX}")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return path
    raise BackupError("Too many backups with the same name; try again in a moment.")


def _verify(copy: Path) -> dict[str, int]:
    """Open the copy on its own and make sure it is a whole, healthy database."""
    mode = stat.S_IMODE(copy.stat().st_mode)
    if mode & 0o077:
        raise BackupError(f"The copy ended up with mode {mode:04o}, not 0600; it was removed.")
    try:
        conn = connect_read_only(copy)
    except (sqlite3.Error, OSError) as exc:
        raise BackupError(f"The copy couldn't be opened ({type(exc).__name__}); removed.") from exc
    try:
        problems = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        if problems != ["ok"]:
            raise BackupError("The copy failed SQLite's integrity check; it was removed.")
        # What the copy holds: counts only, nothing identifying leaves this module.
        return {
            "monthly": conn.execute("SELECT count(*) FROM usage_monthly").fetchone()[0],
            "daily": conn.execute("SELECT count(*) FROM usage_daily").fetchone()[0],
            "half-hourly": conn.execute("SELECT count(*) FROM usage_half_hourly").fetchone()[0],
            "meter readings": conn.execute("SELECT count(*) FROM readings").fetchone()[0],
            "tariffs": conn.execute("SELECT count(*) FROM tariffs").fetchone()[0],
        }
    except sqlite3.Error as exc:
        raise BackupError(f"The copy couldn't be read ({type(exc).__name__}); removed.") from exc
    finally:
        conn.close()


def list_backups(directory: Path) -> list[Path]:
    """Our backup files in `directory`, newest first. Anything else is ignored: other names,
    folders, symlinks."""
    if not directory.is_dir():
        return []
    found = [
        p
        for p in directory.iterdir()
        if _BACKUP_NAME.fullmatch(p.name) and p.is_file() and not p.is_symlink()
    ]
    return sorted(found, key=_sort_key, reverse=True)


def _sort_key(path: Path) -> tuple[str, int]:
    """Oldest-to-newest by the timestamp in the name, then by the `-N` suffix."""
    match = _BACKUP_NAME.fullmatch(path.name)
    return (match.group(1), int(match.group(2) or 1)) if match else ("", 0)


def prune(directory: Path, keep: int) -> list[Path]:
    """Delete all but the newest `keep` of our backups; returns what was deleted."""
    if keep < 1:
        raise ValueError("keep must be at least 1")
    doomed = list_backups(directory)[keep:]
    for path in doomed:
        path.unlink()
    return doomed

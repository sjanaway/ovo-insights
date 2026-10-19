"""`ovo-insights backup`: a consistent, private, verified copy; nothing overwritten or deleted
unless asked."""

import os
import sqlite3
import stat
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ovo_insights import backup, demo
from ovo_insights.cli import app
from ovo_insights.paths import DATA_DIR_ENV, DATABASE_FILENAME, InsecurePathError
from ovo_insights.storage import open_database

NOW = datetime(2026, 10, 14, 11, 30, 5, tzinfo=UTC)
runner = CliRunner()


@pytest.fixture
def populated(data_dir: Path) -> Path:
    """A data folder with the demo data in it (a month's worth is plenty)."""
    conn = open_database(data_dir)
    try:
        demo.write(conn, demo.build(3, date(2026, 10, 13)), NOW)
    finally:
        conn.close()
    return data_dir


def counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            "monthly": conn.execute("SELECT count(*) FROM usage_monthly").fetchone()[0],
            "daily": conn.execute("SELECT count(*) FROM usage_daily").fetchone()[0],
            "half-hourly": conn.execute("SELECT count(*) FROM usage_half_hourly").fetchone()[0],
            "meter readings": conn.execute("SELECT count(*) FROM readings").fetchone()[0],
            "tariffs": conn.execute("SELECT count(*) FROM tariffs").fetchone()[0],
        }
    finally:
        conn.close()


# -- making a backup ----------------------------------------------------------------------


def test_a_backup_holds_everything_and_is_private(populated: Path, tmp_path: Path) -> None:
    dest = tmp_path / "backups"
    result = backup.make_backup(populated, dest, NOW)

    assert result.path == dest / "ovo-insights-20261014T113005Z.sqlite3"
    assert result.rows == counts(populated / DATABASE_FILENAME)
    assert result.rows["half-hourly"] > 30_000
    assert result.rows["tariffs"] == 7
    assert counts(result.path) == result.rows
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700
    assert result.size == result.path.stat().st_size > 1_000_000
    assert os.listdir(dest) == [result.path.name]  # no stray journal or temporary files


def test_the_copy_is_a_working_database_the_app_can_open(populated: Path, tmp_path: Path) -> None:
    result = backup.make_backup(populated, tmp_path / "b", NOW)
    restored = tmp_path / "restored"
    restored.mkdir(mode=0o700)
    (restored / DATABASE_FILENAME).write_bytes(result.path.read_bytes())
    (restored / DATABASE_FILENAME).chmod(0o600)
    conn = open_database(restored)  # runs the migrations check, then reads normally
    try:
        assert conn.execute("SELECT count(*) FROM tariffs").fetchone()[0] == 7
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_writes_still_in_the_write_ahead_log_are_in_the_backup(
    populated: Path, tmp_path: Path
) -> None:
    """The reason to use SQLite's own copy: a plain file copy would miss these."""
    writer = open_database(populated)
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")  # keep new writes in the -wal file
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO usage_daily (account_id, fuel, day, kwh, has_half_hourly,"
            " fetched_at_utc) VALUES ('demo1', 'gas', '2030-01-01', 1.5, 0, '2026-10-14T11:00:00Z')"
        )
        writer.execute("COMMIT")
        assert (populated / f"{DATABASE_FILENAME}-wal").stat().st_size > 0
        before = counts(populated / DATABASE_FILENAME)["daily"]  # sees it too, via the log
        result = backup.make_backup(populated, tmp_path / "b", NOW)
    finally:
        writer.close()
    assert result.rows["daily"] == before
    check = sqlite3.connect(result.path)
    try:
        found = check.execute("SELECT kwh FROM usage_daily WHERE day = '2030-01-01'").fetchone()
    finally:
        check.close()
    assert found == (1.5,)


def test_a_backup_while_another_connection_is_reading_and_writing(
    populated: Path, tmp_path: Path
) -> None:
    reader = open_database(populated)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM usage_half_hourly").fetchone()  # a read in flight
        result = backup.make_backup(populated, tmp_path / "b", NOW)
    finally:
        reader.close()
    assert result.rows["half-hourly"] > 0


def test_nothing_is_overwritten_and_the_same_second_gets_a_new_name(
    populated: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "b"
    first = backup.make_backup(populated, dest, NOW)
    second = backup.make_backup(populated, dest, NOW)
    third = backup.make_backup(populated, dest, NOW)
    assert [p.name for p in (first.path, second.path, third.path)] == [
        "ovo-insights-20261014T113005Z.sqlite3",
        "ovo-insights-20261014T113005Z-2.sqlite3",
        "ovo-insights-20261014T113005Z-3.sqlite3",
    ]
    assert first.rows == second.rows == third.rows  # three whole, separate copies
    assert backup.list_backups(dest) == [third.path, second.path, first.path]


def test_no_database_is_a_clear_error_and_creates_nothing(data_dir: Path, tmp_path: Path) -> None:
    with pytest.raises(backup.BackupError, match="run a sync first"):
        backup.make_backup(data_dir, tmp_path / "b", NOW)
    assert not (tmp_path / "b").exists()
    assert not (data_dir / DATABASE_FILENAME).exists()  # and the database wasn't created either


def test_backups_may_not_go_inside_the_data_folder(populated: Path) -> None:
    for inside in (populated, populated / "backups", populated / "logs" / "x"):
        with pytest.raises(backup.BackupError, match="outside the data folder"):
            backup.make_backup(populated, inside, NOW)
    assert not (populated / "backups").exists()


def test_a_loose_or_symlinked_destination_is_refused(populated: Path, tmp_path: Path) -> None:
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    loose.chmod(0o755)
    with pytest.raises(InsecurePathError, match="chmod 700"):
        backup.make_backup(populated, loose, NOW)
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(InsecurePathError, match="symlink"):
        backup.make_backup(populated, link, NOW)
    assert os.listdir(real) == []


def test_a_failed_copy_is_removed_not_left_behind(
    populated: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_path: Path) -> dict[str, int]:
        raise backup.BackupError("The copy failed SQLite's integrity check; it was removed.")

    monkeypatch.setattr(backup, "_verify", broken)
    with pytest.raises(backup.BackupError, match="integrity"):
        backup.make_backup(populated, tmp_path / "b", NOW)
    assert os.listdir(tmp_path / "b") == []


def test_verification_catches_a_loose_mode_a_corrupt_file_and_a_foreign_one(
    tmp_path: Path,
) -> None:
    loose = tmp_path / "loose.sqlite3"
    loose.write_bytes(b"")
    loose.chmod(0o644)
    with pytest.raises(backup.BackupError, match="0600"):
        backup._verify(loose)
    junk = tmp_path / "junk.sqlite3"
    junk.write_bytes(b"this is not a database at all" * 100)
    junk.chmod(0o600)
    with pytest.raises(backup.BackupError, match="removed"):
        backup._verify(junk)
    other = tmp_path / "other.sqlite3"
    other.write_bytes(b"")
    other.chmod(0o600)
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE unrelated (x)")
    conn.commit()
    conn.close()
    with pytest.raises(backup.BackupError, match="couldn't be read"):
        backup._verify(other)


def test_default_folder_and_names() -> None:
    assert backup.default_backup_dir() == Path.home() / "Backups" / "ovo-insights"
    assert backup.backup_name(NOW) == "ovo-insights-20261014T113005Z.sqlite3"
    # The name is always in UTC, whatever zone the time was given in.
    assert backup.backup_name(NOW.astimezone(timezone(timedelta(hours=1)))) == backup.backup_name(
        NOW
    )
    with pytest.raises(ValueError, match="aware"):
        backup.backup_name(datetime(2026, 10, 14, 11, 30))  # noqa: DTZ001 - the point


# -- keeping only the newest --------------------------------------------------------------


def test_prune_keeps_the_newest_and_touches_nothing_else(tmp_path: Path) -> None:
    folder = tmp_path / "b"
    folder.mkdir(mode=0o700)
    names = [backup.backup_name(NOW + timedelta(days=d)) for d in range(5)]
    for name in names:
        (folder / name).write_bytes(b"x")
    twin = folder / "ovo-insights-20261016T113005Z-2.sqlite3"
    twin.write_bytes(b"x")
    others = [
        folder / "notes.txt",
        folder / "ovo-insights-latest.sqlite3",
        folder / "ovo-insights-20261014T113005Z.sqlite3.bak",
        folder / "OVO-INSIGHTS-20250101T000000Z.sqlite3",  # wrong case: not ours
    ]
    for path in others:
        path.write_bytes(b"x")
    (folder / "ovo-insights-20260101T000000Z.sqlite3").mkdir()  # a folder, not a file
    outside = tmp_path / "precious.sqlite3"
    outside.write_bytes(b"x")
    (folder / "ovo-insights-20260102T000000Z.sqlite3").symlink_to(outside)

    removed = backup.prune(folder, keep=3)

    # Newest first: the last two days, then the second backup of the third day.
    assert sorted(p.name for p in removed) == sorted(names[:3])
    kept = [p.name for p in backup.list_backups(folder)]
    assert kept == [names[4], names[3], twin.name]
    assert all(path.exists() for path in others)
    assert outside.exists()
    assert (folder / "ovo-insights-20260102T000000Z.sqlite3").is_symlink()
    assert (folder / "ovo-insights-20260101T000000Z.sqlite3").is_dir()


def test_prune_with_fewer_than_keep_deletes_nothing_and_rejects_zero(tmp_path: Path) -> None:
    folder = tmp_path / "b"
    folder.mkdir(mode=0o700)
    (folder / backup.backup_name(NOW)).write_bytes(b"x")
    assert backup.prune(folder, keep=5) == []
    with pytest.raises(ValueError, match="at least 1"):
        backup.prune(folder, keep=0)
    assert backup.list_backups(tmp_path / "missing") == []


# -- the command --------------------------------------------------------------------------


def run(populated: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> tuple[int, str]:
    monkeypatch.setenv(DATA_DIR_ENV, str(populated))
    result = runner.invoke(app, ["backup", *args])
    return result.exit_code, result.output


def test_the_command_reports_what_it_made(
    populated: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, out = run(populated, monkeypatch, "--to", str(tmp_path / "b"))
    assert code == 0, out
    [made] = os.listdir(tmp_path / "b")
    assert f"Backed up to {tmp_path / 'b' / made} (" in out
    assert "mode 0600" in out
    assert "passed SQLite's integrity check" in out
    assert "half-hourly" in out
    assert "tariffs         7" in out
    assert "Deleted" not in out


def test_keep_deletes_only_older_backups_and_says_so(
    populated: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "b"
    for _ in range(4):
        assert run(populated, monkeypatch, "--to", str(folder))[0] == 0
    assert len(os.listdir(folder)) == 4
    code, out = run(populated, monkeypatch, "--to", str(folder), "--keep", "2")
    assert code == 0, out
    assert len(os.listdir(folder)) == 2
    assert out.count("Deleted the older backup") == 3
    assert "Keeping the newest 2." in out
    assert run(populated, monkeypatch, "--to", str(folder), "--keep", "0")[0] == 2  # usage error


def test_the_command_refuses_clearly_without_a_database_or_inside_the_data_folder(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, out = run(data_dir, monkeypatch, "--to", str(tmp_path / "b"))
    assert code == 2
    assert "run a sync first" in out
    conn = open_database(data_dir)
    conn.close()
    code, out = run(data_dir, monkeypatch, "--to", str(data_dir / "backups"))
    assert code == 2
    assert "outside the data folder" in out
    loose = tmp_path / "loose"
    loose.mkdir()
    loose.chmod(0o755)
    code, out = run(data_dir, monkeypatch, "--to", str(loose))
    assert code == 1
    assert "chmod 700" in out


def test_the_default_folder_is_under_the_home_folder(
    populated: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    code, out = run(populated, monkeypatch)
    assert code == 0, out
    assert len(os.listdir(tmp_path / "Backups" / "ovo-insights")) == 1
    leaf = tmp_path / "Backups" / "ovo-insights"
    assert stat.S_IMODE(leaf.stat().st_mode) == 0o700  # the folder holding the files is private


def test_a_database_error_is_reported_without_a_traceback(
    populated: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(backup, "make_backup", fail)
    code, out = run(populated, monkeypatch, "--to", str(tmp_path / "b"))
    assert code == 1
    assert "the database couldn't be copied (OperationalError)" in out
    assert "Traceback" not in out

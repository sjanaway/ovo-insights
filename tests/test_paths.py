import os
import stat
from pathlib import Path

import pytest

from ovo_insights.paths import (
    DATA_DIR_ENV,
    InsecurePathError,
    default_data_dir,
    ensure_private_dir,
    ensure_private_file,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_default_data_dir_uses_platform_location(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)

    assert default_data_dir().name == "ovo-insights"


def test_default_data_dir_honours_absolute_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path / "custom"))

    assert default_data_dir() == tmp_path / "custom"


def test_default_data_dir_rejects_relative_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DATA_DIR_ENV, "relative/dir")

    with pytest.raises(InsecurePathError, match="absolute"):
        default_data_dir()


def test_creates_private_dir(tmp_path: Path) -> None:
    created = ensure_private_dir(tmp_path / "a" / "b")

    assert created.is_dir()
    assert _mode(created) == 0o700


def test_rejects_group_readable_dir_with_fix(tmp_path: Path) -> None:
    target = tmp_path / "loose"
    target.mkdir(mode=0o755)
    target.chmod(0o755)

    with pytest.raises(InsecurePathError, match="chmod 700"):
        ensure_private_dir(target)


def test_rejects_symlinked_dir(tmp_path: Path) -> None:
    real = ensure_private_dir(tmp_path / "real")
    link = tmp_path / "link"
    link.symlink_to(real)

    with pytest.raises(InsecurePathError, match="symlink"):
        ensure_private_dir(link)


def test_rejects_file_where_dir_expected(tmp_path: Path) -> None:
    target = tmp_path / "file"
    target.touch(mode=0o600)

    with pytest.raises(FileExistsError):
        ensure_private_dir(target)


def test_creates_private_file(tmp_path: Path) -> None:
    created = ensure_private_file(tmp_path / "db")

    assert created.is_file()
    assert _mode(created) == 0o600


def test_existing_private_file_is_accepted(tmp_path: Path) -> None:
    target = tmp_path / "db"
    target.touch(mode=0o600)
    target.chmod(0o600)

    assert ensure_private_file(target) == target


def test_rejects_world_readable_file(tmp_path: Path) -> None:
    target = tmp_path / "db"
    target.touch()
    target.chmod(0o644)

    with pytest.raises(InsecurePathError, match="chmod 600"):
        ensure_private_file(target)


def test_rejects_symlinked_file(tmp_path: Path) -> None:
    real = ensure_private_file(tmp_path / "real")
    link = tmp_path / "link"
    link.symlink_to(real)

    with pytest.raises(InsecurePathError, match="symlink"):
        ensure_private_file(link)


def test_rejects_dir_where_file_expected(tmp_path: Path) -> None:
    target = ensure_private_dir(tmp_path / "dir")

    with pytest.raises(InsecurePathError, match="regular file"):
        ensure_private_file(target)


def test_rejects_dir_owned_by_someone_else(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = ensure_private_dir(tmp_path / "theirs")
    someone_else = os.geteuid() + 1
    monkeypatch.setattr(os, "geteuid", lambda: someone_else)

    with pytest.raises(InsecurePathError, match="another user"):
        ensure_private_dir(target)

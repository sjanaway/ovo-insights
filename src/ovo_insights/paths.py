"""Where data lives, and making sure nobody else can read it.

The data directory must be mode 0700 and the database 0600, owned by the current user. If
permissions are looser the app refuses to start and says how to fix it, rather than silently
"correcting" a directory the user may have set up deliberately.
"""

import os
import stat
import sys
from pathlib import Path

import platformdirs

APP_NAME = "ovo-insights"
DATA_DIR_ENV = "OVO_INSIGHTS_DATA_DIR"
DATABASE_FILENAME = "ovo-insights.sqlite3"


class InsecurePathError(RuntimeError):
    """A data path is a symlink, has the wrong owner, or is readable by other users."""


def default_data_dir() -> Path:
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise InsecurePathError(f"{DATA_DIR_ENV} must be an absolute path, got {override!r}")
        return path
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))


def ensure_private_dir(path: Path) -> Path:
    """Create `path` (mode 0700) if missing, then verify it is private."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _verify_private(path, expect_dir=True)
    return path


def ensure_private_file(path: Path) -> Path:
    """Create `path` (mode 0600) if missing, without following symlinks, then verify it."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    _verify_private(path, expect_dir=False)
    return path


def _verify_private(path: Path, *, expect_dir: bool) -> None:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise InsecurePathError(f"Refusing to use a symlink: {path}")
    if expect_dir and not stat.S_ISDIR(info.st_mode):
        raise InsecurePathError(f"Expected a directory: {path}")
    if not expect_dir and not stat.S_ISREG(info.st_mode):
        raise InsecurePathError(f"Expected a regular file: {path}")
    if sys.platform == "win32":  # pragma: no cover - POSIX permission bits don't apply
        return
    if info.st_uid != os.geteuid():
        raise InsecurePathError(f"{path} is owned by another user (uid {info.st_uid})")
    if info.st_mode & 0o077:
        wanted = "700" if expect_dir else "600"
        raise InsecurePathError(
            f"{path} is accessible by other users (mode {stat.S_IMODE(info.st_mode):o}). "
            f"Fix with: chmod {wanted} '{path}'"
        )

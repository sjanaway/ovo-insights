"""Single-flight lock: at most one sync at a time, across processes (CLI and server).

An OS file lock on `<data dir>/sync.lock`. The OS releases it when the holder exits, even on
a crash or `kill -9`, so a stale lock can never block syncing. The lock is taken without
waiting: a second sync is refused ("already running"), never queued.
"""

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ovo_insights.paths import ensure_private_dir, ensure_private_file

LOCK_FILENAME = "sync.lock"


class SyncAlreadyRunning(RuntimeError):
    """Another sync holds the lock."""


if sys.platform != "win32":
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

else:  # pragma: no cover - Windows is best-effort and not run in CI
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


class SyncLock:
    """The lock as an object, for holders that release it from another thread (the web
    server takes it when a sync is requested and its worker thread releases it)."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._fd: int | None = None

    def acquire(self) -> None:
        """Take the lock or raise SyncAlreadyRunning. Never waits."""
        if self._fd is not None:
            raise SyncAlreadyRunning("A sync is already running")
        ensure_private_dir(self._data_dir)
        path = ensure_private_file(self._data_dir / LOCK_FILENAME)
        fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        if not _try_lock(fd):
            os.close(fd)
            raise SyncAlreadyRunning("A sync is already running")
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                _unlock(fd)
            finally:
                os.close(fd)


@contextmanager
def sync_lock(data_dir: Path) -> Iterator[None]:
    """Hold the sync lock for the duration of the block, or raise SyncAlreadyRunning."""
    lock = SyncLock(data_dir)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()

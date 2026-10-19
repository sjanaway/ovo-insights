import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from ovo_insights.sync.lock import LOCK_FILENAME, SyncAlreadyRunning, sync_lock


def test_lock_is_exclusive_and_released(data_dir: Path) -> None:
    with sync_lock(data_dir), pytest.raises(SyncAlreadyRunning), sync_lock(data_dir):
        pass  # pragma: no cover - never entered
    with sync_lock(data_dir):  # released on exit
        pass


def test_lock_is_released_after_an_error(data_dir: Path) -> None:
    with pytest.raises(RuntimeError), sync_lock(data_dir):
        raise RuntimeError("boom")

    with sync_lock(data_dir):
        pass


def test_lock_file_is_private(data_dir: Path) -> None:
    with sync_lock(data_dir):
        pass

    mode = stat.S_IMODE(os.stat(data_dir / LOCK_FILENAME).st_mode)
    assert mode == 0o600


def test_lock_excludes_other_processes(data_dir: Path) -> None:
    holder = textwrap.dedent(
        f"""
        import sys, time
        from pathlib import Path
        from ovo_insights.sync.lock import sync_lock
        with sync_lock(Path({str(data_dir)!r})):
            print("locked", flush=True)
            sys.stdin.readline()
        """
    )
    with subprocess.Popen(
        [sys.executable, "-c", holder], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    ) as proc:
        assert proc.stdout is not None
        assert proc.stdin is not None
        try:
            assert proc.stdout.readline().strip() == "locked"
            with pytest.raises(SyncAlreadyRunning), sync_lock(data_dir):
                pass  # pragma: no cover - never entered
            proc.stdin.write("\n")
            proc.stdin.flush()
            assert proc.wait(timeout=30) == 0
        finally:
            proc.kill()

    with sync_lock(data_dir):  # the OS released it when the holder exited
        pass

"""Running syncs from the web server: one at a time, off the event loop.

A sync runs in its own worker thread with its own event loop and SQLite connection, so the
server stays responsive and SQLite connections never cross threads. The single-flight lock
is taken before `start` returns, so "already running" is answered immediately and also
covers a sync started from the terminal.
"""

import asyncio
import enum
import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ovo_insights.ovo.auth import Credentials
from ovo_insights.ovo.client import OvoClient
from ovo_insights.storage import open_database
from ovo_insights.sync import store
from ovo_insights.sync.engine import SyncEngine, SyncProgress, SyncResult
from ovo_insights.sync.lock import SyncAlreadyRunning, SyncLock
from ovo_insights.sync.store import Trigger

log = logging.getLogger(__name__)


class StartOutcome(enum.Enum):
    STARTED = "started"
    ALREADY_RUNNING = "already_running"
    NO_LOGIN = "no_login"
    LOGIN_UNREADABLE = "login_unreadable"


@dataclass(frozen=True, slots=True)
class LastOutcome:
    finished_at: datetime
    result: SyncResult | None  # None when the run crashed before recording a result
    error_class: str | None


class SyncService:
    def __init__(
        self,
        data_dir: Path,
        *,
        load_credentials: Callable[[], Credentials | None],
        client_factory: Callable[[Credentials], OvoClient] = OvoClient,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._data_dir = data_dir
        self._load_credentials = load_credentials
        self._client_factory = client_factory
        self._now = now
        self._guard = threading.Lock()
        self._thread: threading.Thread | None = None
        self.progress = SyncProgress()
        self.last: LastOutcome | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, trigger: Trigger) -> StartOutcome:
        from ovo_insights.credentials import CredentialError

        with self._guard:
            if self.running:
                return StartOutcome.ALREADY_RUNNING
            try:
                credentials = self._load_credentials()
            except CredentialError:
                return StartOutcome.LOGIN_UNREADABLE
            if credentials is None:
                return StartOutcome.NO_LOGIN
            lock = SyncLock(self._data_dir)
            try:
                lock.acquire()
            except SyncAlreadyRunning:
                return StartOutcome.ALREADY_RUNNING  # e.g. `ovo-insights sync` in a terminal
            self.progress = SyncProgress(running=True, phase="starting")
            self._thread = threading.Thread(
                target=self._run, args=(trigger, credentials, lock), name="ovo-sync", daemon=True
            )
            self._thread.start()
            return StartOutcome.STARTED

    def wait(self, timeout: float | None = None) -> None:
        """Block until the current sync (if any) finishes. For shutdown and tests."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self, trigger: Trigger, credentials: Credentials, lock: SyncLock) -> None:
        result: SyncResult | None = None
        error: str | None = None
        try:
            conn = open_database(self._data_dir)
            try:
                result = asyncio.run(self._sync(conn, credentials, trigger))
            finally:
                conn.close()
        except Exception as exc:
            # The engine has recorded what it could in sync_runs; this is for the log.
            log.exception("Sync failed")
            error = store.error_class_name(exc)
        finally:
            lock.release()
            self.progress.running = False
            self.last = LastOutcome(self._now(), result, result.error_class if result else error)

    async def _sync(
        self, conn: sqlite3.Connection, credentials: Credentials, trigger: Trigger
    ) -> SyncResult:
        async with self._client_factory(credentials) as client:
            engine = SyncEngine(client, conn, now=self._now, progress=self.progress)
            return await engine.run(trigger)

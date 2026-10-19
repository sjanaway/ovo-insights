"""Daily automatic sync while the server runs.

Once a day at a random minute between 06:00 and 08:00 UK time, so that installs don't all
call OVO at the same moment. Also once at startup if nothing has been saved for over 24
hours. Skipped while the circuit breaker is open (OVO said no, or syncs keep failing):
the user then decides when to try again with "Sync now".
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from ovo_insights.storage import open_database
from ovo_insights.sync import store
from ovo_insights.sync.store import Breaker, Trigger
from ovo_insights.timeutil import LONDON
from ovo_insights.web.sync_service import StartOutcome, SyncService

log = logging.getLogger(__name__)

WINDOW_START = time(6, 0)
WINDOW_MINUTES = 120
STALE_AFTER = timedelta(hours=24)


def next_run(now: datetime, rng: random.Random) -> datetime:
    """The next daily run: a random minute in the window, today if still ahead, else
    tomorrow. Returned in UTC."""
    local = now.astimezone(LONDON)
    minute = rng.randrange(WINDOW_MINUTES)
    for day in (local.date(), local.date() + timedelta(days=1)):
        start = datetime.combine(day, WINDOW_START, tzinfo=LONDON)
        candidate = (start + timedelta(minutes=minute)).astimezone(UTC)
        if candidate > now:
            return candidate
    raise AssertionError("unreachable")  # pragma: no cover - tomorrow is always ahead


def due_at_startup(last_productive: datetime | None, now: datetime) -> bool:
    return last_productive is None or now - last_productive > STALE_AFTER


class DailyScheduler:
    def __init__(
        self,
        service: SyncService,
        data_dir: Path,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._service = service
        self._data_dir = data_dir
        self._now = now
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311 - spreading load, not security

    async def run(self) -> None:
        """Run until cancelled."""
        breaker, last = await asyncio.to_thread(self._history)
        if due_at_startup(last, self._now()):
            self._trigger(breaker, "startup")
        while True:
            when = next_run(self._now(), self._rng)
            log.info("Next automatic sync at %s", when.astimezone(LONDON).strftime("%H:%M"))
            await self._sleep(max(0.0, (when - self._now()).total_seconds()))
            breaker, _ = await asyncio.to_thread(self._history)
            self._trigger(breaker, "daily")

    def _history(self) -> tuple[Breaker, datetime | None]:
        conn = open_database(self._data_dir)
        try:
            return store.breaker(conn), store.last_productive_run(conn)
        finally:
            conn.close()

    def _trigger(self, breaker: Breaker, why: str) -> None:
        if breaker.open:
            log.warning("Automatic sync skipped: paused because %s", breaker.reason)
            return
        outcome = self._service.start(Trigger.SCHEDULED)
        if outcome is not StartOutcome.STARTED:
            log.info("Automatic sync (%s) not started: %s", why, outcome.value)

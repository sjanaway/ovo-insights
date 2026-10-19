import asyncio
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ovo_insights.storage import open_database
from ovo_insights.sync import store as sync_store
from ovo_insights.sync.store import RunCounts, RunStatus, Trigger
from ovo_insights.timeutil import LONDON
from ovo_insights.web.scheduler import DailyScheduler, due_at_startup, next_run
from ovo_insights.web.sync_service import StartOutcome

pytestmark = pytest.mark.anyio


def at(day: int, hour: int, minute: int = 0, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=LONDON).astimezone(UTC)


class Fixed(random.Random):
    def __init__(self, minute: int) -> None:
        super().__init__(0)
        self.minute = minute

    def randrange(self, *args: object, **kwargs: object) -> int:
        return self.minute


@pytest.mark.parametrize(
    ("now", "minute", "expected"),
    [
        (at(13, 5, 0), 30, at(13, 6, 30)),  # before the window: today
        (at(13, 6, 45), 90, at(13, 7, 30)),  # inside, still ahead: today
        (at(13, 7, 30), 10, at(14, 6, 10)),  # inside, already passed: tomorrow
        (at(13, 22, 0), 0, at(14, 6, 0)),  # evening: tomorrow
        (at(24, 23, 0), 0, at(25, 6, 0)),  # across the clocks going back (BST -> GMT)
    ],
)
def test_next_run(now: datetime, minute: int, expected: datetime) -> None:
    assert next_run(now, Fixed(minute)) == expected


@given(
    st.datetimes(
        # Hypothesis takes naive bounds and attaches the zone itself.
        min_value=datetime(2020, 1, 1),  # noqa: DTZ001
        max_value=datetime(2035, 1, 1),  # noqa: DTZ001
        timezones=st.just(UTC),
    ),
    st.integers(0, 2**32),
)
def test_next_run_is_always_ahead_in_the_window_and_within_a_day(now: datetime, seed: int) -> None:
    when = next_run(now, random.Random(seed))  # noqa: S311 - test data

    local = when.astimezone(LONDON)
    assert now < when <= now + timedelta(days=1, hours=3)
    assert 6 <= local.hour < 8


def test_due_at_startup() -> None:
    now = datetime(2026, 10, 13, 12, tzinfo=UTC)

    assert due_at_startup(None, now)
    assert due_at_startup(now - timedelta(hours=25), now)
    assert not due_at_startup(now - timedelta(hours=23), now)


class FakeService:
    def __init__(self) -> None:
        self.starts: list[Trigger] = []

    def start(self, trigger: Trigger) -> StartOutcome:
        self.starts.append(trigger)
        return StartOutcome.ALREADY_RUNNING if len(self.starts) > 1 else StartOutcome.STARTED


def record(data_dir: Path, status: RunStatus, error: str | None, at_: datetime) -> None:
    conn = open_database(data_dir)
    try:
        run_id = sync_store.start_run(conn, Trigger.SCHEDULED, at_)
        sync_store.finish_run(conn, run_id, status, error, RunCounts(), at_)
    finally:
        conn.close()


async def run_scheduler(
    data_dir: Path, now: datetime, *, days: int
) -> tuple[FakeService, list[float]]:
    service = FakeService()
    sleeps: list[float] = []
    clock = [now]

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) > days:
            raise asyncio.CancelledError
        clock[0] += timedelta(seconds=seconds)

    scheduler = DailyScheduler(
        service,  # type: ignore[arg-type]
        data_dir,
        now=lambda: clock[0],
        sleep=sleep,
        rng=Fixed(15),
    )
    with pytest.raises(asyncio.CancelledError):
        await scheduler.run()
    return service, sleeps


async def test_syncs_at_startup_when_stale_then_daily(data_dir: Path) -> None:
    now = at(13, 12, 0)
    record(data_dir, RunStatus.OK, None, now - timedelta(days=2))

    service, sleeps = await run_scheduler(data_dir, now, days=2)

    assert service.starts == [Trigger.SCHEDULED] * 3  # startup, then two mornings
    assert sleeps[0] == (at(14, 6, 15) - now).total_seconds()
    assert sleeps[1] == timedelta(days=1).total_seconds()


async def test_no_startup_sync_when_recent(data_dir: Path) -> None:
    now = at(13, 12, 0)
    record(data_dir, RunStatus.PARTIAL, "UpstreamUnavailable", now - timedelta(hours=2))

    service, _ = await run_scheduler(data_dir, now, days=0)

    assert service.starts == []


async def test_open_breaker_skips_automatic_syncs(
    data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    now = at(13, 12, 0)
    record(data_dir, RunStatus.BLOCKED, "BlockedByUpstream", now - timedelta(days=2))

    service, _ = await run_scheduler(data_dir, now, days=1)

    assert service.starts == []
    assert "Automatic sync skipped: paused because OVO blocked the last sync" in caplog.text


def test_default_rng_and_clock(data_dir: Path) -> None:
    scheduler = DailyScheduler(FakeService(), data_dir)  # type: ignore[arg-type]

    assert scheduler._now().tzinfo is UTC
    assert isinstance(scheduler._rng, random.Random)

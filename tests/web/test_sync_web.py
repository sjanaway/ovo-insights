"""Sync now, progress polling and the background service, against the fake OVO."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from ovo_insights.credentials import CredentialError, MemoryCredentialStore
from ovo_insights.ovo.auth import Credentials
from ovo_insights.storage import open_database
from ovo_insights.sync import store as sync_store
from ovo_insights.sync.lock import sync_lock
from ovo_insights.sync.store import RunCounts, RunStatus, Trigger
from ovo_insights.timeutil import london_date
from ovo_insights.web.app import WebContext
from ovo_insights.web.sync_service import StartOutcome, SyncService
from tests.sync.fake_ovo import FakeOvo, cloudflare_block, dual_fuel
from tests.web.conftest import CREDS, csrf, fast_client

pytestmark = pytest.mark.anyio

TODAY = london_date(datetime.now(UTC))


@pytest.fixture
def fake(mock: respx.MockRouter) -> FakeOvo:
    return FakeOvo([dual_fuel("300003", TODAY - timedelta(days=3))], today=TODAY).install(mock)


async def test_sync_now_runs_in_the_background_and_reports_progress(
    ctx: WebContext, browser: httpx.AsyncClient, store: MemoryCredentialStore, fake: FakeOvo
) -> None:
    store.save(CREDS)

    started = await browser.post("/sync", headers=csrf(ctx))
    assert started.status_code == 200
    assert 'id="sync-progress"' in started.text
    assert 'hx-trigger="every 2s"' in started.text  # polling while it runs

    ctx.sync.wait(30)
    done = await browser.get("/sync/progress")
    assert 'hx-trigger="every 2s"' not in done.text  # polling stops when it's finished
    assert "The sync you started finished: <strong>ok</strong>" in done.text
    assert ctx.sync.last is not None
    assert ctx.sync.last.result is not None
    assert ctx.sync.last.result.status is RunStatus.OK
    assert len(fake.usage_calls("half-hourly-local")) == 3

    page = (await browser.get("/sync")).text
    assert "<td>manual</td>" in page
    assert "<td>ok</td>" in page
    assert "300003" not in page


async def test_sync_now_without_a_login(ctx: WebContext, browser: httpx.AsyncClient) -> None:
    assert "Nothing yet." in (await browser.get("/sync")).text
    response = await browser.post("/sync", headers=csrf(ctx))

    assert "Save your OVO login on the Setup page first." in response.text
    assert not ctx.sync.running


async def test_sync_now_with_an_unreadable_login(
    ctx: WebContext, browser: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable() -> Credentials:
        raise CredentialError("unreadable")

    monkeypatch.setattr(ctx.sync, "_load_credentials", unreadable)

    response = await browser.post("/sync", headers=csrf(ctx))

    assert "can&#39;t be read" in response.text


async def test_sync_now_while_the_terminal_is_syncing(
    ctx: WebContext, browser: httpx.AsyncClient, store: MemoryCredentialStore
) -> None:
    store.save(CREDS)

    with sync_lock(ctx.data_dir):
        response = await browser.post("/sync", headers=csrf(ctx))

    assert "A sync is already running." in response.text


async def test_breaker_reason_is_shown(ctx: WebContext, browser: httpx.AsyncClient) -> None:
    conn = open_database(ctx.data_dir)
    try:
        now = datetime.now(UTC)
        run_id = sync_store.start_run(conn, Trigger.SCHEDULED, now)
        sync_store.finish_run(
            conn, run_id, RunStatus.BLOCKED, "BlockedByUpstream", RunCounts(), now
        )
    finally:
        conn.close()

    page = (await browser.get("/sync")).text

    assert "Automatic syncing is paused because OVO blocked the last sync." in page
    assert "<td>BlockedByUpstream</td>" in page


# -- the service on its own ---------------------------------------------------------------


def service(data_dir: Path, store: MemoryCredentialStore) -> SyncService:
    return SyncService(data_dir, load_credentials=store.load, client_factory=fast_client)


def test_only_one_sync_at_a_time(
    data_dir: Path, store: MemoryCredentialStore, fake: FakeOvo
) -> None:
    store.save(CREDS)
    svc = service(data_dir, store)

    first = svc.start(Trigger.MANUAL)
    second = svc.start(Trigger.MANUAL)
    svc.wait(30)

    assert (first, second) == (StartOutcome.STARTED, StartOutcome.ALREADY_RUNNING)
    assert not svc.running
    with sync_lock(data_dir):  # released when the worker finished
        pass


def test_a_blocked_sync_is_recorded(
    data_dir: Path, store: MemoryCredentialStore, fake: FakeOvo
) -> None:
    store.save(CREDS)
    fake.intercept = lambda kind, account, param: (
        cloudflare_block() if kind == "half-hourly-local" else None
    )
    svc = service(data_dir, store)

    svc.start(Trigger.SCHEDULED)
    svc.wait(30)

    assert svc.last is not None
    assert svc.last.error_class == "BlockedByUpstream"
    assert svc.progress.phase == "blocked"


def test_a_crash_in_the_worker_is_logged_and_releases_the_lock(
    data_dir: Path, store: MemoryCredentialStore, caplog: pytest.LogCaptureFixture
) -> None:
    store.save(CREDS)

    def broken(credentials: Credentials) -> object:
        raise RuntimeError("bug")

    svc = SyncService(data_dir, load_credentials=store.load, client_factory=broken)  # type: ignore[arg-type]

    svc.start(Trigger.MANUAL)
    svc.wait(30)

    assert svc.last is not None
    assert (svc.last.result, svc.last.error_class) == (None, "RuntimeError")
    assert "Sync failed" in caplog.text
    assert not svc.progress.running
    with sync_lock(data_dir):
        pass


def test_wait_without_a_sync_returns_at_once(data_dir: Path, store: MemoryCredentialStore) -> None:
    service(data_dir, store).wait(0)


async def test_the_sync_page_explains_how_the_last_run_stopped(
    ctx: WebContext, browser: httpx.AsyncClient
) -> None:
    conn = open_database(ctx.data_dir)
    try:
        for status, error_class in (
            (RunStatus.OK, None),
            (RunStatus.PARTIAL, "UpstreamUnavailable"),
        ):
            run_id = sync_store.start_run(conn, Trigger.MANUAL, datetime.now(UTC))
            sync_store.finish_run(conn, run_id, status, error_class, RunCounts(), datetime.now(UTC))
        page = (await browser.get("/sync")).text
    finally:
        conn.close()

    assert "stopped early." in page
    assert "OVO couldn&#39;t be reached, or kept failing" in page
    assert "Everything it saved is kept" in page
    assert "Sync now” carries on from where it stopped" in page
    assert 'class="notice warn" role="status"' in page
    assert "<td>UpstreamUnavailable</td>" in page  # the class name stays in the table


@pytest.mark.parametrize(
    ("status", "error_class", "headline", "advice", "css"),
    [
        (RunStatus.FAILED, "AuthFailed", "failed.", "Save it again on the Setup page", "bad"),
        (RunStatus.BLOCKED, "BlockedByUpstream", "was blocked.", "Wait several hours", "bad"),
        (RunStatus.PARTIAL, "Interrupted", "stopped early.", "closed or stopped", "warn"),
        (RunStatus.PARTIAL, "SomethingElse", "stopped early.", "(SomethingElse)", "warn"),
    ],
)
async def test_each_way_a_sync_can_stop_gets_its_own_message(
    ctx: WebContext,
    browser: httpx.AsyncClient,
    status: RunStatus,
    error_class: str,
    headline: str,
    advice: str,
    css: str,
) -> None:
    conn = open_database(ctx.data_dir)
    try:
        run_id = sync_store.start_run(conn, Trigger.SCHEDULED, datetime.now(UTC))
        sync_store.finish_run(conn, run_id, status, error_class, RunCounts(), datetime.now(UTC))
    finally:
        conn.close()
    page = (await browser.get("/sync")).text
    assert headline in page
    assert advice in page
    assert f'class="notice {css}" role="status"' in page


async def test_no_message_when_the_last_sync_was_fine_or_there_were_none(
    ctx: WebContext, browser: httpx.AsyncClient
) -> None:
    assert 'role="status"' not in (await browser.get("/sync")).text
    conn = open_database(ctx.data_dir)
    try:
        failed = sync_store.start_run(conn, Trigger.CLI, datetime.now(UTC))
        sync_store.finish_run(
            conn, failed, RunStatus.FAILED, "RateLimited", RunCounts(), datetime.now(UTC)
        )
        ok = sync_store.start_run(conn, Trigger.CLI, datetime.now(UTC))
        sync_store.finish_run(conn, ok, RunStatus.OK, None, RunCounts(), datetime.now(UTC))
        sync_store.start_run(conn, Trigger.MANUAL, datetime.now(UTC))  # still running
    finally:
        conn.close()
    assert 'role="status"' not in (await browser.get("/sync")).text  # nothing to report yet


async def test_whats_downloaded_describes_the_data_held_not_the_latest_syncs_window(
    ctx: WebContext, browser: httpx.AsyncClient
) -> None:
    import re

    from ovo_insights import demo

    today = date(2026, 10, 13)
    data = demo.build(7, today)
    conn = open_database(ctx.data_dir)
    try:
        demo.write(conn, data, datetime(2026, 10, 13, 6, tzinfo=UTC))
        # A later sync's window starts later, so the older days' bookkeeping is gone ...
        conn.execute("DELETE FROM sync_state WHERE period_start < '2026-01-01'")
    finally:
        conn.close()
    ctx.today = lambda: today

    page = (await browser.get("/sync")).text

    # ... but the table still says the data goes back to where it does.
    row = re.search(r"<td>half-hourly</td>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>", page)
    assert row is not None
    assert row.groups() == (data.first.isoformat(), data.last.isoformat())
    daily = re.search(r"<td>daily</td>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>", page)
    assert daily is not None
    assert daily.groups() == (data.first.isoformat(), data.last.isoformat())
    assert "first and last days of data held" in page

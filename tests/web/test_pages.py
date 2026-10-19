import html
import logging
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from ovo_insights.credentials import CredentialError, MemoryCredentialStore
from ovo_insights.domain import Fuel, SupplyPoint
from ovo_insights.logs import LOG_FILENAME, add_file_logging, log_dir
from ovo_insights.ovo.errors import AuthFailed, UpstreamUnavailable
from ovo_insights.paths import DATABASE_FILENAME
from ovo_insights.storage import open_database
from ovo_insights.storage.repository import Repository
from ovo_insights.sync import store as sync_store
from ovo_insights.sync.lock import sync_lock
from ovo_insights.sync.plan import Granularity, Period, PeriodStatus
from ovo_insights.sync.store import RunCounts, RunStatus, Trigger
from ovo_insights.web.app import WebContext
from tests.web.conftest import CREDS, PASSWORD, LoginChecks, form

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 10, 13, 7, 0, tzinfo=UTC)


def seed(data_dir: Path) -> None:
    conn = open_database(data_dir)
    try:
        repo = Repository(conn)
        for account, start in (("100001", date(2017, 9, 26)), ("200002", date(2021, 8, 13))):
            repo.upsert_account(account)
            repo.upsert_supply_points(
                [SupplyPoint(account, Fuel.ELECTRICITY, "1900000000000", start)]
            )
        run_id = sync_store.start_run(conn, Trigger.CLI, T0)
        sync_store.record_period(
            conn,
            Period("200002", Granularity.HALF_HOURLY, date(2026, 10, 1)),
            PeriodStatus.COMPLETE,
            expected=48,
            received=48,
            fetched_at=T0,
            run_id=run_id,
        )
        sync_store.finish_run(conn, run_id, RunStatus.OK, None, RunCounts(requests=9), T0)
    finally:
        conn.close()


# -- dashboard and placeholders -----------------------------------------------------------


async def test_dashboard_empty_state(browser: httpx.AsyncClient) -> None:
    page = (await browser.get("/")).text

    assert "No data yet — run a sync" in page
    assert "No OVO login is saved yet" in page
    assert "Unofficial — not affiliated with or endorsed by OVO Energy" in page


async def test_dashboard_summarises_without_identifiers(
    ctx: WebContext, browser: httpx.AsyncClient, store: MemoryCredentialStore
) -> None:
    seed(ctx.data_dir)
    store.save(CREDS)

    page = (await browser.get("/")).text

    assert "No OVO login" not in page
    assert "Last sync: ok, 13 Oct 2026 08:00." in page  # shown in UK time
    assert "Account 1" in page
    assert "(supply since 2017-09-26)" in page
    assert "No usage data in the last two years." in page
    assert "<td>half-hourly</td>" in page
    for identifier in ("100001", "200002", "1900000000000"):
        assert identifier not in page


async def test_tariffs_page_is_in_the_navigation_and_empty_at_first(
    browser: httpx.AsyncClient,
) -> None:
    page = (await browser.get("/tariffs")).text

    assert '<a href="/tariffs" aria-current="page">Tariffs</a>' in page
    assert "No tariffs yet. Add one to start comparing." in page
    assert "arrives in milestone" not in page


async def test_readings_page_is_in_the_navigation(browser: httpx.AsyncClient) -> None:
    page = (await browser.get("/readings")).text

    assert '<a href="/readings" aria-current="page">Readings &amp; gaps</a>' in page
    assert "arrives in milestone" not in page


async def test_not_found_page(browser: httpx.AsyncClient) -> None:
    response = await browser.get("/nope")

    assert response.status_code == 404
    assert "Page not found" in response.text


async def test_favicon_redirects_to_the_static_icon(browser: httpx.AsyncClient) -> None:
    response = await browser.get("/favicon.ico")

    assert (response.status_code, response.headers["location"]) == (301, "/static/favicon.svg")


async def test_unexpected_errors_show_an_id_and_log_the_details(
    ctx: WebContext,
    browser: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(*args: object) -> None:
        raise RuntimeError("detail-only-for-the-log")

    monkeypatch.setattr(sync_store, "recent_runs", broken)

    response = await browser.get("/")

    assert response.status_code == 500
    assert "detail-only-for-the-log" not in response.text
    error_id = response.text.split("error ID <code>")[1].split("</code>")[0]
    assert f"Unhandled error {error_id}" in caplog.text
    assert "detail-only-for-the-log" in caplog.text


# -- setup --------------------------------------------------------------------------------


async def test_setup_checks_with_ovo_then_saves(
    ctx: WebContext,
    browser: httpx.AsyncClient,
    store: MemoryCredentialStore,
    login_checks: LoginChecks,
) -> None:
    assert "A login is saved" not in (await browser.get("/setup")).text

    response = await browser.post(
        "/setup",
        headers=form(ctx),
        content=f"email=someone%40example.com&password={PASSWORD}",
    )

    assert "Saved in your Keychain" in response.text
    assert login_checks.calls == [CREDS]
    assert store.load() == CREDS
    assert "A login is saved" in (await browser.get("/setup")).text


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (AuthFailed("no"), "OVO didn't accept that email and password. Nothing was saved."),
        (UpstreamUnavailable("down"), "Couldn't check the login with OVO (UpstreamUnavailable)"),
    ],
)
async def test_setup_saves_nothing_when_ovo_says_no(
    ctx: WebContext,
    browser: httpx.AsyncClient,
    store: MemoryCredentialStore,
    login_checks: LoginChecks,
    error: Exception,
    message: str,
) -> None:
    login_checks.error = error

    response = await browser.post(
        "/setup", headers=form(ctx), content=f"email=a%40example.com&password={PASSWORD}"
    )

    assert message in html.unescape(response.text)  # autoescaped: ' arrives as &#39;
    assert store.load() is None


@pytest.mark.parametrize("body", ["email=&password=x", "email=a%40example.com&password=", ""])
async def test_setup_needs_both_fields(
    ctx: WebContext, browser: httpx.AsyncClient, login_checks: LoginChecks, body: str
) -> None:
    response = await browser.post("/setup", headers=form(ctx), content=body)

    assert "Enter your OVO email address and password." in response.text
    assert login_checks.calls == []


async def test_setup_throttles_login_checks(
    ctx: WebContext, browser: httpx.AsyncClient, login_checks: LoginChecks
) -> None:
    clock = [100.0]
    ctx.monotonic = lambda: clock[0]
    login_checks.error = AuthFailed("no")
    body = f"email=a%40example.com&password={PASSWORD}"

    await browser.post("/setup", headers=form(ctx), content=body)
    clock[0] += 1
    second = await browser.post("/setup", headers=form(ctx), content=body)
    clock[0] += 5
    await browser.post("/setup", headers=form(ctx), content=body)

    assert "Please wait a few seconds" in second.text
    assert len(login_checks.calls) == 2


async def test_setup_reports_keychain_failures(
    ctx: WebContext, browser: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(credentials: object) -> None:
        raise CredentialError("No secure keychain is available")

    monkeypatch.setattr(ctx.store, "save", refuse)

    response = await browser.post(
        "/setup", headers=form(ctx), content=f"email=a%40example.com&password={PASSWORD}"
    )

    assert "Not saved: No secure keychain is available" in response.text


@pytest.mark.parametrize(
    ("headers", "body", "status"),
    [
        ({"Content-Type": "application/json"}, '{"email": "a"}', 415),
        ({}, "x=" + "a" * 5000, 413),
        ({}, b"email=\xff", 400),
    ],
)
async def test_form_parsing_is_strict(
    ctx: WebContext,
    browser: httpx.AsyncClient,
    headers: dict[str, str],
    body: str | bytes,
    status: int,
) -> None:
    response = await browser.post("/setup", headers=form(ctx, **headers), content=body)

    assert response.status_code == status


async def test_the_password_never_appears_in_pages_or_logs(
    ctx: WebContext,
    browser: httpx.AsyncClient,
    login_checks: LoginChecks,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    handler = add_file_logging(ctx.data_dir)
    try:
        bodies = []
        for error in (None, AuthFailed("no")):
            login_checks.error = error
            ctx.last_login_attempt = None
            bodies.append(
                (
                    await browser.post(
                        "/setup",
                        headers=form(ctx),
                        content=f"email=someone%40example.com&password={PASSWORD}",
                    )
                ).text
            )
        for path in ("/", "/setup", "/settings", "/sync"):
            bodies.append((await browser.get(path)).text)
        logging.getLogger("ovo_insights").warning("setup done for %s", CREDS)
    finally:
        handler.close()
        logging.getLogger().removeHandler(handler)

    log_file = (log_dir(ctx.data_dir) / LOG_FILENAME).read_text()
    for text in (*bodies, caplog.text, log_file):
        assert PASSWORD not in text


# -- settings -----------------------------------------------------------------------------


async def test_forget_login(
    ctx: WebContext, browser: httpx.AsyncClient, store: MemoryCredentialStore
) -> None:
    store.save(CREDS)
    assert "Forget my login" in (await browser.get("/settings")).text

    first = await browser.post("/settings/forget", headers=form(ctx))
    second = await browser.post("/settings/forget", headers=form(ctx))

    assert "Your saved OVO login was deleted." in first.text
    assert "No login was saved." in second.text
    assert store.load() is None
    assert "No login is saved." in (await browser.get("/settings")).text


async def test_forget_login_reports_keychain_failures(
    ctx: WebContext, browser: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse() -> bool:
        raise CredentialError("Could not remove the login from the keychain: KeyringError")

    monkeypatch.setattr(ctx.store, "forget", refuse)
    monkeypatch.setattr(ctx.store, "load", refuse)  # the page copes with an unreadable login

    response = await browser.post("/settings/forget", headers=form(ctx))

    assert "Not removed:" in response.text
    assert "No login is saved." in (await browser.get("/settings")).text


async def test_delete_all_data_needs_the_typed_confirmation(
    ctx: WebContext, browser: httpx.AsyncClient
) -> None:
    seed(ctx.data_dir)

    for typed in ("", "delete", "DELETE "):
        response = await browser.post(
            "/settings/delete", headers=form(ctx), content=f"confirm={typed}"
        )
        assert "Type DELETE (in capitals) to confirm." in response.text
    assert (ctx.data_dir / DATABASE_FILENAME).exists()


async def test_delete_all_data_removes_the_database_and_logs_but_keeps_the_login(
    ctx: WebContext, browser: httpx.AsyncClient, store: MemoryCredentialStore
) -> None:
    seed(ctx.data_dir)
    store.save(CREDS)
    handler = add_file_logging(ctx.data_dir)
    ctx.log_handler = handler
    try:
        logging.getLogger("ovo_insights").warning("something worth logging")
        (log_dir(ctx.data_dir) / f"{LOG_FILENAME}.1").write_text("old")
        assert (log_dir(ctx.data_dir) / LOG_FILENAME).exists()

        response = await browser.post(
            "/settings/delete", headers=form(ctx), content="confirm=DELETE"
        )

        assert "All downloaded data and logs were deleted" in response.text
        assert sorted(p.name for p in log_dir(ctx.data_dir).iterdir()) == []
        assert store.load() == CREDS
        # The app carries on: a fresh, empty database and a fresh log file.
        assert "No data yet" in (await browser.get("/")).text
        logging.getLogger("ovo_insights").warning("after deletion")
        assert "after deletion" in (log_dir(ctx.data_dir) / LOG_FILENAME).read_text()
    finally:
        handler.close()
        logging.getLogger().removeHandler(handler)
    conn = sqlite3.connect(ctx.data_dir / DATABASE_FILENAME)
    try:
        assert conn.execute("SELECT count(*) FROM accounts").fetchone() == (0,)
    finally:
        conn.close()


async def test_delete_all_data_waits_for_a_running_sync(
    ctx: WebContext, browser: httpx.AsyncClient
) -> None:
    seed(ctx.data_dir)

    with sync_lock(ctx.data_dir):
        response = await browser.post(
            "/settings/delete", headers=form(ctx), content="confirm=DELETE"
        )

    assert "A sync is running." in response.text
    assert (ctx.data_dir / DATABASE_FILENAME).exists()


async def test_delete_all_data_without_any_logs(
    ctx: WebContext, browser: httpx.AsyncClient
) -> None:
    seed(ctx.data_dir)
    assert not log_dir(ctx.data_dir).exists()

    response = await browser.post("/settings/delete", headers=form(ctx), content="confirm=DELETE")

    assert "were deleted" in response.text
    assert not log_dir(ctx.data_dir).exists()


async def test_the_half_hourly_page_is_called_details_and_says_its_data_is_half_hourly(
    browser: httpx.AsyncClient,
) -> None:
    page = (await browser.get("/half-hourly")).text

    assert '<a href="/half-hourly" aria-current="page">Details</a>' in page
    assert "<title>Details · ovo-insights</title>" in page
    assert "<h1>Details</h1>" in page
    assert "The data shown on this page is half-hourly" in page
    assert ">Half-hourly</a>" not in page  # the old menu name is gone everywhere
    assert ">Half-hourly</a>" not in (await browser.get("/")).text

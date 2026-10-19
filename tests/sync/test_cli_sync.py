"""`ovo-insights sync` end to end, against the fake OVO (never the real one or the Keychain)."""

import os
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner, Result

from ovo_insights import cli
from ovo_insights.cli import app
from ovo_insights.credentials import MemoryCredentialStore
from ovo_insights.ovo.auth import Credentials, Secret
from ovo_insights.ovo.client import LOGIN_URL
from ovo_insights.paths import DATA_DIR_ENV, DATABASE_FILENAME
from ovo_insights.storage import open_database
from ovo_insights.sync.lock import sync_lock
from ovo_insights.sync.plan import REVISION_DAYS, Granularity, period_last_day
from ovo_insights.timeutil import london_date
from tests.sync.fake_ovo import ELEC_MPAN, FakeOvo, bad_json, cloudflare_block, dual_fuel

PASSWORD = "pw-SENTINEL-do-not-print"
ACCOUNT = "300003"
OLD_ACCOUNT = "100001"
TODAY = london_date(datetime.now(UTC))
WINDOW = [TODAY - timedelta(days=d) for d in range(1, 11)]
# A first sync's requests: one per year, per month and per day in the window (which may
# cross a month or year boundary depending on when the tests run).
PERIODS = [
    *((Granularity.MONTHLY, s) for s in {date(d.year, 1, 1) for d in WINDOW}),
    *((Granularity.DAILY, s) for s in {d.replace(day=1) for d in WINDOW}),
    *((Granularity.HALF_HOURLY, d) for d in WINDOW),
]
FIRST_SYNC = len(PERIODS)
# Periods still within OVO's revision window after a complete sync today.
STILL_PROVISIONAL = sum(
    1 for g, s in PERIODS if period_last_day(g, s) + timedelta(days=REVISION_DAYS) > TODAY
)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "data"
    monkeypatch.setenv(DATA_DIR_ENV, str(path))
    return path


@pytest.fixture(autouse=True)
def saved_login(monkeypatch: pytest.MonkeyPatch) -> MemoryCredentialStore:
    store = MemoryCredentialStore()
    store.save(Credentials("someone@example.com", Secret(PASSWORD)))
    monkeypatch.setattr(cli, "_store", lambda: store)
    monkeypatch.setattr("ovo_insights.ovo.client.DEFAULT_MIN_INTERVAL", 0)
    monkeypatch.setattr("getpass.getpass", lambda prompt: pytest.fail("should not prompt"))
    return store


@pytest.fixture
def fake(mock: respx.MockRouter) -> FakeOvo:
    old = dual_fuel(OLD_ACCOUNT, date(2017, 9, 26), date(2021, 8, 12))
    return FakeOvo([old, dual_fuel(ACCOUNT, TODAY - timedelta(days=10))], today=TODAY).install(mock)


def sync(*args: str) -> Result:
    result = CliRunner().invoke(app, ["sync", *args])
    for secret in (PASSWORD, ACCOUNT, OLD_ACCOUNT, ELEC_MPAN):
        assert secret not in result.output
    return result


def test_sync_downloads_and_summarises(fake: FakeOvo, data_dir: Path) -> None:
    result = sync()

    assert result.exit_code == cli.EXIT_OK, result.output
    assert "Logging in and finding your accounts..." in result.output
    assert "Account 1 of 2 (supply since 2017-09-26): nothing to fetch" in result.output
    assert "day(s) of half-hourly data to fetch" in result.output
    assert "periods (100%)" in result.output
    assert "Sync ok:" in result.output
    assert "Account 1 (supply since 2017-09-26):\n  no usage data in the last two years" in (
        result.output
    )
    assert "  half_hourly  " in result.output
    assert len(fake.usage_calls("half-hourly-local")) == 10
    assert (data_dir / DATABASE_FILENAME).exists()


def test_second_sync_while_one_is_running_is_refused(fake: FakeOvo, data_dir: Path) -> None:
    with sync_lock(data_dir):
        result = sync()

    assert result.exit_code == cli.EXIT_ALREADY_RUNNING
    assert "already running" in result.output
    assert fake.calls == []


def test_blocked_sync_exits_4_and_pauses_automatic_sync(fake: FakeOvo, data_dir: Path) -> None:
    fake.intercept = lambda kind, account, param: (
        cloudflare_block() if kind == "half-hourly-local" else None
    )

    result = sync()

    assert result.exit_code == cli.EXIT_BLOCKED
    assert "Stopped by: BlockedByUpstream" in result.output
    assert "hours, not minutes" in result.output

    fake.intercept = None
    again = sync()
    assert "automatic syncing is paused because OVO blocked the last sync" in again.output
    assert again.exit_code == cli.EXIT_OK


def test_refused_login_exits_1_and_says_how_to_fix_it(
    mock: respx.MockRouter, data_dir: Path
) -> None:
    mock.post(LOGIN_URL).respond(401)

    result = sync()

    assert result.exit_code == cli.EXIT_FAILED
    assert "Run `ovo-insights login`" in result.output


def test_partial_sync_exits_3(fake: FakeOvo, data_dir: Path) -> None:
    fake.intercept = lambda kind, account, param: (
        bad_json() if param == (TODAY - timedelta(days=5)).isoformat() else None
    )

    result = sync()

    assert result.exit_code == cli.EXIT_PARTIAL
    assert "1 response(s) failed validation" in result.output
    assert "Run `ovo-insights sync` again to resume." in result.output


def test_rate_limited_sync_says_to_wait(fake: FakeOvo, data_dir: Path) -> None:
    fake.intercept = lambda kind, account, param: (
        httpx.Response(429, headers={"retry-after": "3600"}) if param == "Bootstrap" else None
    )

    result = sync()

    assert result.exit_code == cli.EXIT_FAILED
    assert "OVO asked us to slow down." in result.output


def test_summary_shows_when_nothing_is_complete_yet(fake: FakeOvo, data_dir: Path) -> None:
    fake.missing_daily = set(WINDOW)

    result = sync()

    assert "daily        no data yet" in result.output


def test_insecure_data_dir_is_refused(fake: FakeOvo, data_dir: Path) -> None:
    data_dir.mkdir(mode=0o755)
    os.chmod(data_dir, 0o755)  # noqa: S103 - deliberately too open

    result = sync()

    assert result.exit_code == cli.EXIT_FAILED
    assert "chmod 700" in result.output


def test_dry_run_writes_nothing(fake: FakeOvo, data_dir: Path) -> None:
    result = sync("--dry-run")

    assert result.exit_code == 0, result.output
    assert f"Would make {FIRST_SYNC} data request(s)" in result.output
    assert "Dry run: nothing was written." in result.output
    assert f"window {TODAY - timedelta(days=10)} to {TODAY - timedelta(days=1)}" in result.output
    assert not (data_dir / DATABASE_FILENAME).exists()
    assert fake.usage_calls() == []


def test_dry_run_after_a_sync_plans_only_what_is_left(fake: FakeOvo, data_dir: Path) -> None:
    sync()
    db = data_dir / DATABASE_FILENAME
    before = db.stat().st_mtime_ns

    result = sync("--dry-run")

    assert f"Would make {STILL_PROVISIONAL} data request(s)" in result.output
    assert db.stat().st_mtime_ns == before


def test_dry_run_with_an_old_schema_plans_from_scratch(
    fake: FakeOvo, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    open_database(data_dir).close()
    from ovo_insights.storage import migrator

    real = migrator.packaged_migrations()
    monkeypatch.setattr(migrator, "packaged_migrations", lambda: [*real, real[0]])

    result = sync("--dry-run")

    assert "needs upgrading" in result.output
    assert f"Would make {FIRST_SYNC} data request(s)" in result.output


def test_dry_run_reports_bad_meter_reads(fake: FakeOvo, data_dir: Path) -> None:
    fake.intercept = lambda kind, account, param: (
        bad_json() if param == "MeterReads_nextV1" else None
    )

    result = sync("--dry-run")

    assert "2 meter-reads response(s) failed validation." in result.output


def test_sync_help_documents_exit_codes() -> None:
    result = CliRunner().invoke(app, ["sync", "--help"])
    # CI runners force colour, and the styling codes split option names apart.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)

    assert "Exit codes" in plain
    assert "--dry-run" in plain


def test_failed_sync_says_nothing_was_saved(fake: FakeOvo, data_dir: Path) -> None:
    fake.accounts.append(fake.accounts[0])  # an account listed twice fails validation

    result = sync()

    assert result.exit_code == cli.EXIT_FAILED
    assert "Nothing new was saved." in result.output

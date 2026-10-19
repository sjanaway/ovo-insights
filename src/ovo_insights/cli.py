"""Command-line entry point."""

import asyncio
import getpass
import logging
import secrets
import sqlite3
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from ovo_insights import __version__
from ovo_insights.logs import quiet_http_logs
from ovo_insights.ovo.auth import Credentials, Secret
from ovo_insights.ovo.redact import install_redaction

if TYPE_CHECKING:
    import sqlite3

    from ovo_insights.credentials import CredentialStore
    from ovo_insights.sync.engine import AccountPlan, SyncProgress, SyncResult

app = typer.Typer(
    name="ovo-insights",
    help="Unofficial, local-only analysis of your OVO Energy smart-meter data.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
dev_app = typer.Typer(help="Developer tools. Not needed for normal use.", no_args_is_help=True)
app.add_typer(dev_app, name="dev")

REPO_ROOT_MARKER = "pyproject.toml"


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"ovo-insights {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the version and exit.",
            callback=_print_version,
            is_eager=True,
        ),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log progress.")] = False,
) -> None:
    """Unofficial, local-only analysis of your OVO Energy smart-meter data."""
    _configure_logging(verbose)


def _configure_logging(verbose: bool) -> None:
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(logging.INFO if verbose else logging.WARNING)
    install_redaction(root)
    quiet_http_logs()


def _store() -> "CredentialStore":
    from ovo_insights.credentials import KeyringCredentialStore

    return KeyringCredentialStore()


def _credentials_for_run(*, prompt: bool) -> Credentials:
    """The saved login, or a prompt if there isn't one (or `--prompt` was given)."""
    from ovo_insights.credentials import CredentialError

    if not prompt:
        try:
            saved = _store().load()
        except CredentialError as exc:
            typer.echo(f"{exc}", err=True)
            saved = None
        if saved is not None:
            return saved
        typer.echo("No saved OVO login (run `ovo-insights login` to save one).", err=True)
    return _prompt_credentials()


def _prompt_credentials() -> Credentials:
    """Ask for the OVO login. The password is read without echo and never accepted as an
    argument or environment variable, so it can't end up in shell history."""
    email = typer.prompt("OVO email").strip()
    password = getpass.getpass("OVO password (not shown): ")
    try:
        return Credentials(email, Secret(password))
    except (ValueError, TypeError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(2) from None


PromptOption = Annotated[
    bool, typer.Option("--prompt", help="Ask for the login even if one is saved.")
]


@app.command()
def login(
    verify: Annotated[
        bool, typer.Option("--verify/--no-verify", help="Check the login with OVO first.")
    ] = True,
) -> None:
    """Save your OVO login in the macOS Keychain.

    The password is read without echo and is never accepted as an argument, so it can't end
    up in shell history. By default it is checked with OVO before it's saved.
    """
    from ovo_insights.credentials import CredentialError, secure_backend

    try:
        secure_backend()
    except CredentialError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None

    credentials = _prompt_credentials()
    if verify:
        from ovo_insights.ovo.client import OvoClient
        from ovo_insights.ovo.errors import OvoError

        async def check() -> None:
            async with OvoClient(credentials) as client:
                await client.login()

        try:
            asyncio.run(check())
        except OvoError as exc:
            typer.echo(f"Login not saved: {exc}", err=True)
            raise typer.Exit(1) from None
    try:
        _store().save(credentials)
    except CredentialError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    typer.echo("Saved your OVO login in the Keychain.")


@app.command()
def logout() -> None:
    """Delete the saved OVO login from the Keychain."""
    from ovo_insights.credentials import CredentialError

    try:
        removed = _store().forget()
    except CredentialError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    typer.echo("Deleted your saved OVO login." if removed else "No saved OVO login to delete.")


@app.command()
def doctor(prompt: PromptOption = False) -> None:
    """Check that logging in and every OVO endpoint work for your account.

    Prints pass/fail per check with counts only: no account numbers, meter details or
    amounts are shown.
    """
    from ovo_insights.ovo.client import OvoClient
    from ovo_insights.ovo.doctor import run_checks

    credentials = _credentials_for_run(prompt=prompt)

    async def run() -> list[tuple[str, bool, str]]:
        async with OvoClient(credentials) as client:
            return [(r.name, r.ok, r.detail) for r in await run_checks(client)]

    results = asyncio.run(run())
    width = max(len(name) for name, _, _ in results)
    for name, ok, detail in results:
        typer.echo(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
    if not all(ok for _, ok, _ in results):
        typer.echo("\nSome checks failed. If you report this, paste only the lines above.")
        raise typer.Exit(1)


@app.command()
def serve(
    port: Annotated[
        int,
        typer.Option(min=0, max=65535, help="Port on 127.0.0.1 (default: any free port)."),
    ] = 0,
    no_open: Annotated[
        bool, typer.Option("--no-open", help="Don't open the browser; just print the link.")
    ] = False,
) -> None:
    """Start the web UI on this Mac only (127.0.0.1) and open it in your browser.

    The printed link signs you in and works once; restart the server for a new one. While it
    runs, it syncs automatically once a day between 06:00 and 08:00.
    """
    from ovo_insights.paths import InsecurePathError, default_data_dir
    from ovo_insights.web.server import prepare, run

    try:
        prepared = prepare(default_data_dir(), _store(), port=port)
    except InsecurePathError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    except OSError as exc:
        typer.echo(f"Could not listen on port {port}: {exc.strerror or exc}", err=True)
        raise typer.Exit(1) from None
    run(prepared, open_browser=not no_open, announce=typer.echo)


@app.command()
def demo(
    seed: Annotated[int, typer.Option(help="Different seeds give different made-up data.")] = 1,
    port: Annotated[
        int,
        typer.Option(min=0, max=65535, help="Port on 127.0.0.1 (default: any free port)."),
    ] = 0,
    no_open: Annotated[
        bool, typer.Option("--no-open", help="Don't open the browser; just print the link.")
    ] = False,
) -> None:
    """Try the app with two years of made-up data.

    The data goes in a temporary folder that is deleted when you stop the server. Your
    real data, your saved login and OVO are never touched.
    """
    import shutil
    import tempfile

    from ovo_insights.credentials import MemoryCredentialStore
    from ovo_insights.demo import build, write
    from ovo_insights.storage import open_database
    from ovo_insights.timeutil import LONDON
    from ovo_insights.web.server import prepare, run

    data_dir = Path(tempfile.mkdtemp(prefix="ovo-insights-demo-"))  # created 0700
    try:
        conn = open_database(data_dir)
        try:
            write(conn, build(seed, datetime.now(LONDON).date()))
        finally:
            conn.close()
        try:
            prepared = prepare(
                data_dir, MemoryCredentialStore(), port=port, schedule=False, demo=True
            )
        except OSError as exc:
            typer.echo(f"Could not listen on port {port}: {exc.strerror or exc}", err=True)
            raise typer.Exit(1) from None
        typer.echo(f"Demo mode: made-up data (seed {seed}) in a temporary folder.")
        run(prepared, open_browser=not no_open, announce=typer.echo)
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


@app.command()
def backup(
    to: Annotated[
        Path | None,
        typer.Option(
            "--to",
            help="Folder for the backup (made private if new). Default: ~/Backups/ovo-insights.",
        ),
    ] = None,
    keep: Annotated[
        int | None,
        typer.Option(
            "--keep",
            min=1,
            help="Afterwards, delete all but the newest N backups here. Default: keep all.",
        ),
    ] = None,
) -> None:
    """Make a safe copy of your downloaded data, even while the app is running.

    The copy is a single file, private to you, checked after it is made. Nothing is overwritten,
    and nothing is deleted unless you give --keep (which only ever deletes this command's own
    backups). It holds your usage, readings and tariffs, but not your OVO login (that is in the
    Keychain). Keep it somewhere private: it shows when you're home.
    """
    from ovo_insights import backup as backups
    from ovo_insights.paths import InsecurePathError, default_data_dir

    try:
        destination = to if to is not None else backups.default_backup_dir()
        result = backups.make_backup(default_data_dir(), destination, datetime.now(UTC))
        removed = backups.prune(result.path.parent, keep) if keep is not None else []
    except (backups.BackupError, InsecurePathError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(2 if isinstance(exc, backups.BackupError) else 1) from None
    except sqlite3.Error as exc:
        typer.echo(f"Error: the database couldn't be copied ({type(exc).__name__}).", err=True)
        raise typer.Exit(1) from None

    typer.echo(f"Backed up to {result.path} ({result.size / 1_000_000:.1f} MB, mode 0600).")
    typer.echo("It passed SQLite's integrity check and holds:")
    for name, count in result.rows.items():
        typer.echo(f"  {name:<15} {count:,}")
    for path in removed:
        typer.echo(f"Deleted the older backup {path.name}.")
    if keep is not None:
        typer.echo(f"Keeping the newest {keep}.")


# `sync` exit codes, so it can be scripted (2 is Typer's usage error).
EXIT_OK, EXIT_FAILED, EXIT_PARTIAL, EXIT_BLOCKED, EXIT_ALREADY_RUNNING = 0, 1, 3, 4, 5


@app.command()
def sync(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Log in and show what would be fetched; write nothing."),
    ] = False,
    prompt: PromptOption = False,
) -> None:
    """Download your OVO data into the local database.

    The first run backfills up to two years of monthly, daily and half-hourly usage, plus
    your meter readings. Later runs fetch only what is new, recent (OVO may revise the last
    few days) or still incomplete. It is safe to stop at any point: the next run resumes.

    Exit codes: 0 ok, 1 failed, 3 partial (run again to resume), 4 blocked by OVO,
    5 another sync is already running.
    """
    from ovo_insights.paths import InsecurePathError, default_data_dir
    from ovo_insights.sync.lock import SyncAlreadyRunning, sync_lock

    try:
        data_dir = default_data_dir()
        if dry_run:
            _sync_dry_run(data_dir, prompt=prompt)
            return
        with sync_lock(data_dir):
            code = _sync_now(data_dir, prompt=prompt)
    except SyncAlreadyRunning:
        typer.echo("Another sync is already running; not starting a second one.", err=True)
        raise typer.Exit(EXIT_ALREADY_RUNNING) from None
    except InsecurePathError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(EXIT_FAILED) from None
    if code != EXIT_OK:
        raise typer.Exit(code)


def _sync_now(data_dir: Path, *, prompt: bool) -> int:
    from ovo_insights.ovo.client import OvoClient
    from ovo_insights.storage import open_database
    from ovo_insights.sync import store
    from ovo_insights.sync.engine import SyncEngine, SyncResult
    from ovo_insights.sync.store import RunStatus, Trigger

    conn = open_database(data_dir)
    try:
        paused = store.breaker(conn)
        if paused.open:
            typer.echo(
                f"Note: automatic syncing is paused because {paused.reason}. "
                "Running now because you asked.",
                err=True,
            )
        credentials = _credentials_for_run(prompt=prompt)

        async def run() -> SyncResult:
            async with OvoClient(credentials) as client:
                engine = SyncEngine(client, conn, on_progress=_ProgressPrinter())
                return await engine.run(Trigger.CLI)

        result = asyncio.run(run())
        _print_sync_summary(result, conn)
    finally:
        conn.close()
    return {
        RunStatus.OK: EXIT_OK,
        RunStatus.PARTIAL: EXIT_PARTIAL,
        RunStatus.BLOCKED: EXIT_BLOCKED,
    }.get(result.status, EXIT_FAILED)


class _ProgressPrinter:
    """Prints progress to stderr: one line per account, then every PROGRESS_EVERY periods."""

    PROGRESS_EVERY = 25

    def __init__(self) -> None:
        self._phase = ""
        self._account: int | None = None
        self._printed_done = 0

    def __call__(self, progress: "SyncProgress") -> None:
        if progress.phase != self._phase:
            self._phase = progress.phase
            if progress.phase == "accounts":
                typer.echo("Logging in and finding your accounts...", err=True)
        account = progress.account
        if account is not None and account.number != self._account:
            self._account = account.number
            typer.echo(
                f"Account {account.number} of {progress.accounts_total} "
                f"({_since(account.first_supply)}): {_period_counts(account)}",
                err=True,
            )
        done, total = progress.periods_done, progress.periods_total
        if done != self._printed_done and (done % self.PROGRESS_EVERY == 0 or done == total):
            typer.echo(f"  {done}/{total} periods ({done * 100 // total}%)", err=True)
            self._printed_done = done


def _since(first: date | None) -> str:
    return f"supply since {first}" if first else "no supply period"


def _period_counts(account: "AccountPlan") -> str:
    from ovo_insights.sync.plan import Granularity

    if not account.periods:
        return "nothing to fetch"
    monthly = account.count(Granularity.MONTHLY)
    daily = account.count(Granularity.DAILY)
    half_hourly = account.count(Granularity.HALF_HOURLY)
    return (
        f"{monthly} year(s) of monthly, {daily} month(s) of daily and "
        f"{half_hourly} day(s) of half-hourly data to fetch"
    )


def _print_sync_summary(result: "SyncResult", conn: "sqlite3.Connection") -> None:
    from ovo_insights.sync import store
    from ovo_insights.sync.store import RunStatus

    c = result.counts
    typer.echo(
        f"\nSync {result.status.value}: {c.periods_fetched} period(s) fetched "
        f"with {c.requests} request(s)."
    )
    typer.echo(
        f"Rows written: {c.half_hours_written} half-hour(s), {c.days_written} day(s), "
        f"{c.months_written} month(s), {c.readings_written} meter reading(s)."
    )
    typer.echo(f"New known gaps: {c.gaps_found}. Warnings: {c.warnings}.")
    for account in result.plan.accounts if result.plan else ():
        typer.echo(f"Account {account.number} ({_since(account.first_supply)}):")
        covered = store.coverage(conn, account.account_id)
        if not covered:
            typer.echo("  no usage data in the last two years")
        for cov in covered:
            span = f"{cov.earliest} to {cov.latest}" if cov.earliest else "no data yet"
            typer.echo(
                f"  {cov.granularity.value:<11}  {span}  ({cov.complete} final, "
                f"{cov.provisional} provisional, {cov.gaps} gap(s), {cov.pending} to retry)"
            )

    advice = {
        RunStatus.PARTIAL: "Some data is missing. Run `ovo-insights sync` again to resume.",
        RunStatus.FAILED: "Nothing new was saved.",
        RunStatus.BLOCKED: (
            "OVO blocked the request. Automatic syncing is paused; wait a while (hours, not "
            "minutes) before running `ovo-insights sync` again."
        ),
    }.get(result.status)
    if result.error_class:
        typer.echo(f"Stopped by: {result.error_class}")
    if result.error_class == "RateLimited":
        advice = (
            "OVO asked us to slow down. Wait a few minutes, then run `ovo-insights sync` "
            "again; it resumes where it stopped."
        )
    if result.error_class in ("AuthFailed", "Unauthorized"):
        advice = "OVO refused the saved login. Run `ovo-insights login` to update it."
    if result.period_errors:
        typer.echo(
            f"{len(result.period_errors)} response(s) failed validation; OVO may have changed "
            "its API. `ovo-insights doctor` checks each endpoint."
        )
    if advice:
        typer.echo(advice)


def _sync_dry_run(data_dir: Path, *, prompt: bool) -> None:
    from ovo_insights.ovo.client import DEFAULT_MIN_INTERVAL, OvoClient
    from ovo_insights.paths import DATABASE_FILENAME
    from ovo_insights.storage.db import connect_read_only
    from ovo_insights.storage.migrator import packaged_migrations, schema_version
    from ovo_insights.sync.engine import SyncPlan, plan_sync

    conn = None
    db_path = data_dir / DATABASE_FILENAME
    if db_path.exists():
        conn = connect_read_only(db_path)
        if schema_version(conn) != len(packaged_migrations()):
            typer.echo("The database needs upgrading; planning as if it were empty.", err=True)
            conn.close()
            conn = None
    credentials = _credentials_for_run(prompt=prompt)

    async def run() -> SyncPlan:
        async with OvoClient(credentials) as client:
            return await plan_sync(client, conn)

    try:
        plan = asyncio.run(run())
    finally:
        if conn is not None:
            conn.close()
    for account in plan.accounts:
        backfill = sorted(account.backfill.values(), key=lambda r: r.first)
        window = f"; window {backfill[0].first} to {backfill[-1].last}" if backfill else ""
        typer.echo(
            f"Account {account.number} of {len(plan.accounts)} "
            f"({_since(account.first_supply)}{window}): {_period_counts(account)}"
        )
    minutes = plan.periods * DEFAULT_MIN_INTERVAL / 60
    typer.echo(
        f"\nWould make {plan.periods} data request(s) (about {minutes:.0f} minute(s) at the "
        "polite request rate). Dry run: nothing was written."
    )
    if plan.discovery_errors:
        typer.echo(f"{len(plan.discovery_errors)} meter-reads response(s) failed validation.")


PROGRESS_SECONDS = 5.0  # how often `dev verify` says it is still searching


@dev_app.command("verify")
def verify(
    caches: Annotated[
        bool,
        typer.Option(
            "--caches/--no-caches",
            help="Also search ~/Library/Caches for your password (can take a few minutes).",
        ),
    ] = True,
) -> None:
    """Check that your data folder is private and holds no password or login token.

    Run it after logging in and syncing. It reads your saved password from the Keychain only to
    search for it, and prints file names and counts, never what it finds. Exit code 0 means
    everything passed, 1 that something didn't.
    """
    from ovo_insights import verify as checks
    from ovo_insights.credentials import CredentialError
    from ovo_insights.paths import default_data_dir

    data_dir = default_data_dir()
    results = checks.check_permissions(data_dir)
    for item in results:
        typer.echo(f"{'PASS' if item.ok else 'FAIL'}  permissions: {item.name} ({item.detail})")
    ok = all(item.ok for item in results)

    password: str | None = None
    typer.echo("Reading your saved login from the Keychain (macOS may ask you to allow it)...")
    try:
        saved = _store().load()
        password = saved.password.reveal() if saved else None
    except CredentialError:
        typer.echo("NOTE  the saved login can't be read, so only token shapes are searched.")
    if password is None:
        typer.echo("NOTE  no saved login: your password can't be searched for.")
    elif checks.short_password(password):
        typer.echo("NOTE  your password is short, so a match may be a coincidence.")

    caches_dir = checks.default_caches_dir() if caches else None
    if caches and caches_dir is None:
        typer.echo("NOTE  there is no Caches folder here, so only the data folder is searched.")

    last_report = time.monotonic()

    def show_progress(label: str, files: int) -> None:
        nonlocal last_report
        if time.monotonic() - last_report >= PROGRESS_SECONDS:
            last_report = time.monotonic()
            typer.echo(f"      still searching the {label}: {files:,} files so far...")

    typer.echo("Searching the data folder...")
    for label, scan in checks.scan_roots(
        data_dir, caches_dir, password=password, progress=show_progress
    ):
        for hit in scan.hits:
            typer.echo(f"FAIL  {label}: {hit.path} contains {hit.what}")
        extras = f"{scan.skipped_large} too large, {scan.skipped_unreadable} unreadable"
        if scan.interrupted:
            outcome, what = (
                "SKIP",
                f"stopped after {scan.files_scanned:,} files, so not fully searched",
            )
        else:
            outcome = "PASS" if not scan.hits else "FAIL"
            what = f"searched {scan.files_scanned:,} files ({extras})"
        typer.echo(f"{outcome}  {label}: {what}")
        ok = ok and not scan.hits and not scan.interrupted
        if label == "data folder" and caches_dir is not None:
            typer.echo(
                f"Searching {caches_dir} for your password. This can take several minutes; "
                "press Ctrl-C to skip it."
            )
    typer.echo("All checks passed." if ok else "Something failed or was skipped: see above.")
    raise typer.Exit(0 if ok else 1)


@dev_app.command("capture-fixtures")
def capture_fixtures(
    raw_dir: Annotated[
        Path, typer.Option(help="Where unredacted responses go (must be gitignored).")
    ] = Path("fixtures-raw"),
    out_dir: Annotated[Path, typer.Option(help="Where safe fixtures go.")] = Path(
        "tests/fixtures/captured"
    ),
    prompt: PromptOption = False,
) -> None:
    """Capture one response per OVO endpoint for test fixtures.

    Raw responses are written to RAW_DIR (0600 files). Safe copies go to OUT_DIR: every id,
    MPAN/MPRN, serial, name, email and address is replaced by a consistent fake, every kWh,
    cost, rate and meter reading by a made-up value, and account and meter dates are moved.
    Review them before committing; CI also scans them.
    """
    from ovo_insights.ovo.client import GRAPHQL_URL, HALF_HOURLY_VARIANTS, USAGE_BASE, OvoClient
    from ovo_insights.ovo.client import _query as query
    from ovo_insights.ovo.fixtures import Redactor, Synthesiser, dumps, write_private

    if not Path(REPO_ROOT_MARKER).exists():
        typer.echo("Run this from the repository root.", err=True)
        raise typer.Exit(2)
    if raw_dir.resolve().is_relative_to(Path("tests").resolve()):
        typer.echo("RAW_DIR must not be inside tests/.", err=True)
        raise typer.Exit(2)

    credentials = _credentials_for_run(prompt=prompt)

    async def run() -> dict[str, object]:
        async with OvoClient(credentials) as client:
            await client.login()
            summaries = await client.accounts()
            if not summaries:
                raise typer.Exit(1)
            account = summaries[0].account_id
            today = datetime.now(UTC).date()
            last_month = today.replace(day=1) - timedelta(days=1)
            probe = today - timedelta(days=2)
            captured: dict[str, object] = {
                "bootstrap": await client.raw_json(
                    "POST",
                    GRAPHQL_URL,
                    json={
                        "operationName": "Bootstrap",
                        "query": query("bootstrap.graphql"),
                        "variables": {"customerId": client.customer_id},
                    },
                ),
                "meter_reads": await client.raw_json(
                    "POST",
                    GRAPHQL_URL,
                    json={
                        "operationName": "MeterReads_nextV1",
                        "query": query("meter_reads.graphql"),
                        "variables": {
                            "accountId": account,
                            "query": {"includeReads": "TOP_VALID_ONLY"},
                        },
                    },
                ),
                "monthly": await client.raw_json(
                    "GET", f"{USAGE_BASE}/monthly/{account}", params={"date": f"{today.year}"}
                ),
                "daily": await client.raw_json(
                    "GET",
                    f"{USAGE_BASE}/daily/{account}",
                    params={"date": f"{last_month:%Y-%m}"},
                ),
            }
            for variant in HALF_HOURLY_VARIANTS:
                captured[variant.replace("-", "_")] = await client.raw_json(
                    "GET", f"{USAGE_BASE}/{variant}/{account}", params={"date": probe.isoformat()}
                )
            captured["_sensitive"] = [credentials.email, client.customer_id or "", account]
            return captured

    captured = asyncio.run(run())
    sensitive = [str(v) for v in captured.pop("_sensitive")]  # type: ignore[attr-defined]
    redactor = Redactor(salt=secrets.token_hex(16), extra_sensitive=sensitive)
    for name, payload in captured.items():
        write_private(raw_dir / f"{name}.json", dumps(payload))
    safe = Synthesiser(salt=secrets.token_hex(16)).synthesise(
        {name: redactor.redact(payload) for name, payload in captured.items()}
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in safe.items():
        (out_dir / f"{name}.json").write_text(dumps(payload), encoding="utf-8")
    typer.echo(f"Raw responses: {raw_dir}/ (never commit these)")
    typer.echo(
        f"Safe fixtures: {out_dir}/ (review, run the tests, then update the expected "
        "values in tests/ovo/test_captured.py)"
    )

"""The local web app: pages, forms and the sync controls.

Every request passes through `SecurityMiddleware` first (Host check, session cookie, CSRF,
security headers). Routes that touch SQLite are plain functions, which FastAPI runs in a
worker thread, so the event loop never blocks on the database. Each request opens its own
connection.
"""

import logging
import re
import secrets
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.telemetry import TelemetryConfig
from jinja2 import Environment, PackageLoader, StrictUndefined
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp

from ovo_insights import __version__
from ovo_insights.analysis import queries
from ovo_insights.analysis.completeness import STATE_LABELS
from ovo_insights.analysis.half_hourly import DEFAULT_WINDOW, SLOT_LABELS, TimeWindow
from ovo_insights.analysis.readings import CALORIFIC_RANGE, VERDICT_LABELS, Window
from ovo_insights.analysis.usage import PRESET_LABELS, Preset
from ovo_insights.credentials import CredentialError, CredentialStore
from ovo_insights.domain import Fuel, Reading
from ovo_insights.logs import PrivateRotatingFileHandler, log_dir
from ovo_insights.money import MilliPence, format_pounds
from ovo_insights.ovo.auth import Credentials, Secret
from ovo_insights.ovo.errors import AuthFailed, OvoError
from ovo_insights.paths import DATABASE_FILENAME
from ovo_insights.storage import open_database
from ovo_insights.storage import tariffs as tariff_store
from ovo_insights.storage.repository import Repository
from ovo_insights.sync import store
from ovo_insights.sync.lock import SyncAlreadyRunning, SyncLock
from ovo_insights.sync.plan import DateRange
from ovo_insights.sync.store import Trigger
from ovo_insights.tariffs import TariffError, build_tariff
from ovo_insights.timeutil import LONDON
from ovo_insights.web import charts, export, readings, tariffs
from ovo_insights.web.security import SecurityMiddleware, Sessions
from ovo_insights.web.sync_service import StartOutcome, SyncService

log = logging.getLogger(__name__)

MAX_FORM_BYTES = 4096
DELETE_CONFIRMATION = "DELETE"
LOGIN_ATTEMPT_INTERVAL = 5.0  # seconds between login checks against OVO from the setup page
DEMO_REFUSAL = "Not available in demo mode: it never touches your login or OVO."

# FastAPI >= 0.143 instruments every app with OpenTelemetry by default. Nothing can be
# exported without the OpenTelemetry SDK (not installed), but the PRD rules out telemetry,
# so every switch is off explicitly, including environment-driven auto-configuration.
TELEMETRY_OFF: TelemetryConfig = {
    "tracing": False,
    "metrics": False,
    "logs": False,
    "operation_spans": False,
    "auto_configure": False,
}

NAV = (
    ("/", "Dashboard"),
    ("/half-hourly", "Details"),
    ("/tariffs", "Tariffs"),
    ("/readings", "Readings & gaps"),
    ("/export", "Export"),
    ("/sync", "Sync"),
    ("/setup", "Setup"),
    ("/settings", "Settings"),
)


@dataclass
class WebContext:
    data_dir: Path
    port: int
    store: CredentialStore
    sync: SyncService
    verify_login: Callable[[Credentials], Awaitable[None]]
    sessions: Sessions = field(default_factory=Sessions)
    log_handler: PrivateRotatingFileHandler | None = None
    scheduler: Callable[[], Coroutine[Any, Any, None]] | None = None
    monotonic: Callable[[], float] = field(default_factory=lambda: time.monotonic)
    today: Callable[[], date] = field(default_factory=lambda: _london_today)
    demo: bool = False  # synthetic data: no login, no syncing, a banner on every page
    last_login_attempt: float | None = field(default=None, init=False)


def templates() -> Environment:
    env = Environment(
        loader=PackageLoader("ovo_insights.web", "templates"),
        autoescape=True,  # always, for every template; never `|safe`
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["uk_date"] = uk_date
    env.filters["gbp"] = gbp
    env.filters["money"] = format_pounds
    env.filters["signed_money"] = signed_pounds
    return env


def gbp(text: str) -> str:
    """`1412.26` -> `1,412.26` (exact: the input is already a decimal string)."""
    return f"{Decimal(text):,.2f}"


def signed_pounds(amount: MilliPence) -> str:
    """`+£1.20`, `-£0.05` or `£0.00`: a difference, with its direction."""
    text = format_pounds(amount)
    return f"+{text}" if amount > 0 else text


def uk_date(day: date) -> str:
    """`13 Oct 2026` (strftime's `%-d` isn't portable)."""
    return f"{day.day} {day:%b %Y}"


def create_app(ctx: WebContext) -> ASGIApp:
    env = templates()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        import asyncio

        task = asyncio.create_task(ctx.scheduler()) if ctx.scheduler else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()

    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
        telemetry=TELEMETRY_OFF,
    )
    app.mount("/static", StaticFiles(packages=[("ovo_insights.web", "static")]), name="static")

    def render(request: Request, name: str, status: int = 200, **context: Any) -> HTMLResponse:
        context.setdefault("carry", {})  # extra query fields the range bar must keep
        page = env.get_template(name).render(
            csrf_token=request.scope.get("state", {}).get("csrf_token", ""),
            nav=NAV,
            demo=ctx.demo,
            path=request.url.path,
            version=__version__,
            **context,
        )
        return HTMLResponse(page, status_code=status)

    @contextmanager
    def database() -> Iterator[sqlite3.Connection]:
        conn = open_database(ctx.data_dir)
        try:
            yield conn
        finally:
            conn.close()

    # -- errors -------------------------------------------------------------------------

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        titles = {
            400: "Bad request",
            404: "Page not found",
            405: "Not allowed",
            413: "Too large",
            415: "Not supported",
        }
        return render(
            request,
            "error.html",
            status=exc.status_code,
            title=titles.get(exc.status_code, "Request refused"),
            error_id=None,
        )

    @app.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception) -> Response:
        error_id = secrets.token_hex(4)
        log.error("Unhandled error %s", error_id, exc_info=exc)  # redacted by the log filter
        return render(
            request, "error.html", status=500, title="Something went wrong", error_id=error_id
        )

    # -- pages --------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        range_: RangeParam = Preset.LAST_12_MONTHS.value,
        from_: FromParam = None,
        to: ToParam = None,
    ) -> Response:
        with database() as conn:
            accounts = _account_summaries(conn)
            runs = store.recent_runs(conn, 1)
            choice = _choose(conn, ctx.today(), range_, from_, to)
            span = choice.span
            fuel_tiles = charts.tiles(conn, span)
            missing = len(queries.missing_in(conn, span))
        return render(
            request,
            "dashboard.html",
            accounts=accounts,
            last_run=runs[0] if runs else None,
            has_login=_has_login(ctx),
            fmt=_london,
            presets=PRESET_LABELS,
            choice=choice,
            span=span,
            tiles=fuel_tiles,
            missing=missing,
        )

    @app.get("/half-hourly", response_class=HTMLResponse)
    def half_hourly_page(
        request: Request,
        range_: RangeParam = Preset.LAST_30_DAYS.value,
        from_: FromParam = None,
        to: ToParam = None,
        bl_from: BaseLoadFromParam = None,
        bl_to: BaseLoadToParam = None,
    ) -> Response:
        window = _window(bl_from, bl_to)
        with database() as conn:
            choice = _choose(conn, ctx.today(), range_, from_, to)
            span = choice.span
            load = charts.base_load_tile(conn, span, window)
            missing = len(queries.missing_in(conn, span))
            has_gas = bool(queries.daily(conn, Fuel.GAS, span))
        chosen = bl_from is not None
        return render(
            request,
            "half_hourly.html",
            presets=PRESET_LABELS,
            choice=choice,
            span=span,
            base_load=load,
            window=window,
            window_start=SLOT_LABELS[window.start],
            window_end=SLOT_LABELS[window.end % 48],
            window_times=SLOT_LABELS,
            window_query=urlencode({"bl_from": bl_from, "bl_to": bl_to}) if chosen else "",
            carry={"bl_from": bl_from, "bl_to": bl_to} if chosen else {},
            missing=missing,
            has_gas=has_gas,
        )

    def tariff_form_page(
        request: Request,
        values: dict[str, str],
        errors: tariffs.FormErrors,
        action: str,
        heading: str,
        submit: str,
    ) -> Response:
        return render(
            request,
            "_tariff_form.html",
            values=values,
            errors=errors,
            action=action,
            heading=heading,
            submit=submit,
            rows=tariffs.row_count(values),
            day_labels=tariffs.DAY_LABELS,
        )

    @app.get("/tariffs", response_class=HTMLResponse)
    def tariffs_page(
        request: Request,
        range_: RangeParam = Preset.LAST_12_MONTHS.value,
        from_: FromParam = None,
        to: ToParam = None,
        new: str | None = None,
        edit: str | None = None,
    ) -> Response:
        if new is not None and edit is not None:
            raise HTTPException(400)
        if new is not None and new not in tariffs.TEMPLATES:
            raise HTTPException(400)
        if edit is not None and not _DIGITS.fullmatch(edit):
            raise HTTPException(400)
        with database() as conn:
            choice = _choose(conn, ctx.today(), range_, from_, to)
            listed = tariff_store.all_tariffs(conn)
            editing = None
            if edit is not None:
                editing = next((t for t in listed if t.tariff_id == int(edit)), None)
                if editing is None:
                    raise HTTPException(404)
            views = [v for fuel in Fuel if (v := tariffs.fuel_view(conn, fuel, choice.span))]
        form: dict[str, Any] | None = None
        if new is not None:
            form = {
                "values": tariffs.template_values(new),
                "action": "/tariffs",
                "heading": "New tariff",
                "submit": "Save tariff",
            }
        elif editing is not None:
            form = {
                "values": tariffs.tariff_values(editing),
                "action": f"/tariffs/{editing.tariff_id}",
                "heading": f"Edit {editing.name}",
                "submit": "Save changes",
            }
        if form is not None:
            form |= {
                "errors": tariffs.FormErrors({}, ()),
                "rows": tariffs.row_count(form["values"]),
                "day_labels": tariffs.DAY_LABELS,
            }
        return render(
            request,
            "tariffs.html",
            presets=PRESET_LABELS,
            choice=choice,
            span=choice.span,
            missing=0,
            query=choice.query,
            tariffs=listed,
            views=views,
            form=form is not None,
            templates=tariffs.TEMPLATES,
            describe_rates=tariffs.describe_rates,
            describe_dates=tariffs.describe_dates,
            pence_text=tariffs.pence_text,
            **(
                form
                or {
                    "values": {},
                    "errors": None,
                    "action": "",
                    "heading": "",
                    "submit": "",
                    "rows": 0,
                    "day_labels": (),
                }
            ),
        )

    async def save_tariff(request: Request, tariff_id: int | None) -> Response:
        values = await _form(request, max_fields=120, max_bytes=8192)
        draft, rows = tariffs.parse_form(values)
        action = "/tariffs" if tariff_id is None else f"/tariffs/{tariff_id}"
        heading = "New tariff" if tariff_id is None else "Edit tariff"
        submit = "Save tariff" if tariff_id is None else "Save changes"
        try:
            tariff = build_tariff(draft, tariff_id)
            with database() as conn:
                if tariff_id is None:
                    tariff_store.add(conn, tariff, datetime.now(UTC))
                else:
                    tariff_store.replace(conn, tariff_id, tariff, datetime.now(UTC))
        except TariffError as exc:
            errors = tariffs.form_errors(exc.errors, rows)
            return tariff_form_page(request, values, errors, action, heading, submit)
        except KeyError:
            raise HTTPException(404) from None
        response = Response(status_code=200)
        response.headers["HX-Redirect"] = "/tariffs"
        return response

    @app.post("/tariffs", response_class=HTMLResponse)
    async def create_tariff(request: Request) -> Response:
        return await save_tariff(request, None)

    @app.post("/tariffs/{tariff_id}", response_class=HTMLResponse)
    async def update_tariff(request: Request, tariff_id: str) -> Response:
        if not _DIGITS.fullmatch(tariff_id):
            raise HTTPException(404)
        return await save_tariff(request, int(tariff_id))

    @app.post("/tariffs/{tariff_id}/delete", response_class=HTMLResponse)
    def delete_tariff(request: Request, tariff_id: str) -> Response:
        if not _DIGITS.fullmatch(tariff_id):
            raise HTTPException(404)
        with database() as conn:
            removed = tariff_store.remove(conn, int(tariff_id))
        if not removed:
            return render(
                request, "_message.html", message="That tariff was already deleted.", ok=False
            )
        response = Response(status_code=200)
        response.headers["HX-Redirect"] = "/tariffs"
        return response

    @app.get("/readings", response_class=HTMLResponse)
    def readings_page(
        request: Request,
        range_: RangeParam = Preset.LAST_12_MONTHS.value,
        from_: FromParam = None,
        to: ToParam = None,
        r: str | None = None,
        sort: str = "date",
        dir_: Annotated[str, Query(alias="dir")] = "desc",
        rpage: str = "1",
        show: str = "all",
        cpage: str = "1",
    ) -> Response:
        if sort not in readings.READING_COLUMNS or dir_ not in ("asc", "desc"):
            raise HTTPException(400)
        if show not in ("all", "problems"):
            raise HTTPException(400)
        reading_page, window_page = _page_number(rpage), _page_number(cpage)
        today = ctx.today()
        with database() as conn:
            choice = _choose(conn, today, range_, from_, to, with_readings=True)
            span = choice.span
            reading_span = readings.reading_span(span, today)
            picker = readings.choices(conn, reading_span)
            chosen = _pick(picker, r)
            calendars = readings.calendars(conn, span, today)
            missing = len(queries.missing_in(conn, span))
            compared = readings.comparison(conn, chosen.series, reading_span) if chosen else None
        table: list[Reading] = []
        pages = 1
        if chosen:
            inside = [rd for rd in chosen.series.readings if rd.read_date in reading_span]
            table, pages = readings.paginate(
                readings.sorted_readings(inside, sort, dir_ == "desc"), reading_page
            )
        windows: list[Window] = []
        window_pages = 1
        if compared:
            windows, window_pages = readings.paginate(
                readings.filter_windows(compared.windows, show == "problems"), window_page
            )
        params = {
            k: v
            for k, v in (
                ("range", None if from_ or to else choice.query.partition("=")[2]),
                ("from", from_),
                ("to", to),
                ("r", str(chosen.number) if chosen else None),
                ("sort", sort),
                ("dir", dir_),
                ("rpage", str(reading_page)),
                ("show", show),
                ("cpage", str(window_page)),
            )
            if v
        }

        def link(**changes: str) -> str:
            merged = {**params, **{k: v for k, v in changes.items()}}
            return "?" + urlencode(merged)

        return render(
            request,
            "readings.html",
            presets=PRESET_LABELS,
            choice=choice,
            span=span,
            missing=0,
            gaps=missing,
            picker=picker,
            chosen=chosen,
            compared=compared,
            table=table,
            pages=pages,
            reading_page=min(reading_page, pages),
            windows=windows,
            window_pages=window_pages,
            window_page=min(window_page, window_pages),
            calendars=calendars,
            sort=sort,
            descending=dir_ == "desc",
            show=show,
            link=link,
            columns=readings.READING_COLUMNS,
            verdict_labels=VERDICT_LABELS,
            calorific_range=CALORIFIC_RANGE,
            state_labels=STATE_LABELS,
            today=today,
            query=choice.query,
        )

    # -- chart data (JSON) ----------------------------------------------------------------

    def chart(
        build: Callable[[sqlite3.Connection, DateRange], dict[str, Any]],
        range_: str,
        from_: str | None,
        to: str | None,
    ) -> JSONResponse:
        with database() as conn:
            return JSONResponse(build(conn, _choose(conn, ctx.today(), range_, from_, to).span))

    @app.get("/api/charts/usage")
    def usage_chart(
        fuel: str,
        range_: RangeParam = Preset.LAST_12_MONTHS.value,
        from_: FromParam = None,
        to: ToParam = None,
    ) -> JSONResponse:
        chosen = _fuel(fuel)
        return chart(lambda conn, span: charts.usage(conn, chosen, span), range_, from_, to)

    @app.get("/api/charts/yoy")
    def yoy_chart(
        fuel: str,
        range_: RangeParam = Preset.LAST_12_MONTHS.value,
        from_: FromParam = None,
        to: ToParam = None,
    ) -> JSONResponse:
        chosen = _fuel(fuel)
        return chart(lambda conn, span: charts.yoy(conn, chosen, span), range_, from_, to)

    @app.get("/api/charts/heatmap")
    def heatmap_chart(
        range_: RangeParam = Preset.LAST_30_DAYS.value, from_: FromParam = None, to: ToParam = None
    ) -> JSONResponse:
        return chart(charts.heatmap_data, range_, from_, to)

    @app.get("/api/charts/profile")
    def profile_chart(
        range_: RangeParam = Preset.LAST_30_DAYS.value, from_: FromParam = None, to: ToParam = None
    ) -> JSONResponse:
        return chart(charts.profile, range_, from_, to)

    @app.get("/api/charts/base-load")
    def base_load_chart(
        range_: RangeParam = Preset.LAST_30_DAYS.value,
        from_: FromParam = None,
        to: ToParam = None,
        bl_from: BaseLoadFromParam = None,
        bl_to: BaseLoadToParam = None,
    ) -> JSONResponse:
        window = _window(bl_from, bl_to)
        return chart(
            lambda conn, span: charts.base_load_nightly(conn, span, window), range_, from_, to
        )

    @app.get("/api/charts/gas-daily")
    def gas_chart(
        range_: RangeParam = Preset.LAST_30_DAYS.value, from_: FromParam = None, to: ToParam = None
    ) -> JSONResponse:
        return chart(charts.gas_daily, range_, from_, to)

    @app.get("/api/charts/tariffs")
    def tariffs_chart(
        fuel: str,
        range_: RangeParam = Preset.LAST_12_MONTHS.value,
        from_: FromParam = None,
        to: ToParam = None,
    ) -> JSONResponse:
        chosen = _fuel(fuel)
        with database() as conn:
            span = _choose(conn, ctx.today(), range_, from_, to).span
            view = tariffs.fuel_view(conn, chosen, span)
        if view is None:
            raise HTTPException(404)
        return JSONResponse(tariffs.chart_data(view))

    @app.get("/api/charts/readings")
    def readings_chart(
        r: str,
        range_: RangeParam = Preset.LAST_12_MONTHS.value,
        from_: FromParam = None,
        to: ToParam = None,
    ) -> JSONResponse:
        with database() as conn:
            today = ctx.today()
            span = readings.reading_span(
                _choose(conn, today, range_, from_, to, with_readings=True).span, today
            )
            chosen = _pick(readings.choices(conn, span), r)
            if chosen is None:
                raise HTTPException(404)
            found = readings.comparison(conn, chosen.series, span)
        return JSONResponse(readings.comparison_chart(found.windows, gas=found.gas))

    # -- export (downloads) -----------------------------------------------------------------

    @app.get("/export", response_class=HTMLResponse)
    def export_page(
        request: Request,
        range_: RangeParam = Preset.LAST_12_MONTHS.value,
        from_: FromParam = None,
        to: ToParam = None,
    ) -> Response:
        today = ctx.today()
        with database() as conn:
            choice = _choose(conn, today, range_, from_, to, with_readings=True)
            listed = export.listing(conn, choice.span, today)
        return render(
            request,
            "export.html",
            presets=PRESET_LABELS,
            choice=choice,
            span=choice.span,
            missing=0,
            query=choice.query,
            listed=listed,
            zip_name=export.zip_filename(choice.span),
        )

    def download(
        name: Callable[[DateRange], str],
        media_type: str,
        range_: str,
        from_: str | None,
        to: str | None,
        chunks: Callable[[sqlite3.Connection, DateRange, date], Iterator[bytes]],
    ) -> Response:
        today = ctx.today()
        with database() as conn:
            span = _choose(conn, today, range_, from_, to, with_readings=True).span

        def generate() -> Iterator[bytes]:
            # Opened here, in whichever worker thread starts the stream, and used one chunk at
            # a time, so the connection may move between threads. Read-only for safety.
            stream_conn = open_database(ctx.data_dir, check_same_thread=False)
            try:
                stream_conn.execute("PRAGMA query_only = ON")
                yield from chunks(stream_conn, span, today)
            finally:
                stream_conn.close()

        return StreamingResponse(
            generate(),
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{name(span)}"'},
        )

    @app.get("/export/ovo-insights.zip")
    def export_zip(
        range_: RangeParam = Preset.LAST_12_MONTHS.value,
        from_: FromParam = None,
        to: ToParam = None,
    ) -> Response:
        return download(
            export.zip_filename,
            "application/zip",
            range_,
            from_,
            to,
            lambda conn, span, today: export.zip_chunks(conn, span, today),
        )

    @app.get("/export/{dataset}.csv")
    def export_csv(
        dataset: str,
        range_: RangeParam = Preset.LAST_12_MONTHS.value,
        from_: FromParam = None,
        to: ToParam = None,
    ) -> Response:
        try:
            chosen = export.Dataset(dataset)
        except ValueError:
            raise HTTPException(404) from None
        return download(
            lambda span: export.filename(chosen, span),
            "text/csv; charset=utf-8",
            range_,
            from_,
            to,
            lambda conn, span, today: export.csv_chunks(
                chosen, export.rows(conn, chosen, span, today)
            ),
        )

    @app.get("/sync", response_class=HTMLResponse)
    def sync_page(request: Request) -> Response:
        with database() as conn:
            runs = store.recent_runs(conn, 10)
            breaker = store.breaker(conn)
            accounts = _account_summaries(conn)
        return render(
            request,
            "sync.html",
            runs=runs,
            breaker=breaker,
            accounts=accounts,
            progress=ctx.sync.progress,
            message=None,
            fmt=_london,
            explain=store.explain,
        )

    @app.get("/sync/progress", response_class=HTMLResponse)
    def sync_progress(request: Request) -> Response:
        return render(request, "_progress.html", progress=ctx.sync.progress, message=None)

    @app.post("/sync", response_class=HTMLResponse)
    def sync_now(request: Request) -> Response:
        if ctx.demo:
            return render(
                request, "_progress.html", progress=ctx.sync.progress, message=DEMO_REFUSAL
            )
        outcome = ctx.sync.start(Trigger.MANUAL)
        messages = {
            StartOutcome.STARTED: None,
            StartOutcome.ALREADY_RUNNING: "A sync is already running.",
            StartOutcome.NO_LOGIN: "Save your OVO login on the Setup page first.",
            StartOutcome.LOGIN_UNREADABLE: (
                "The saved OVO login can't be read. Save it again on the Setup page."
            ),
        }
        return render(
            request, "_progress.html", progress=ctx.sync.progress, message=messages[outcome]
        )

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request) -> Response:
        return render(request, "setup.html", has_login=_has_login(ctx), message=None, ok=False)

    @app.post("/setup", response_class=HTMLResponse)
    async def setup_save(request: Request) -> Response:
        form = await _form(request)
        email = form.get("email", "").strip()
        password = form.get("password", "")

        def reply(message: str, ok: bool = False) -> Response:
            # Never echoes the password, or anything derived from it.
            return render(request, "_setup_result.html", message=message, ok=ok)

        if ctx.demo:
            return reply(DEMO_REFUSAL)

        try:
            credentials = Credentials(email, Secret(password))
        except (ValueError, TypeError):
            return reply("Enter your OVO email address and password.")
        now = ctx.monotonic()
        last = ctx.last_login_attempt
        if last is not None and now - last < LOGIN_ATTEMPT_INTERVAL:
            return reply("Please wait a few seconds before trying again.")
        ctx.last_login_attempt = now
        try:
            await ctx.verify_login(credentials)
        except AuthFailed:
            return reply("OVO didn't accept that email and password. Nothing was saved.")
        except OvoError as exc:
            return reply(f"Couldn't check the login with OVO ({type(exc).__name__}). Try later.")
        try:
            ctx.store.save(credentials)
        except CredentialError as exc:
            return reply(f"Not saved: {exc}")
        return reply("Saved in your Keychain. You can now sync.", ok=True)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> Response:
        return render(
            request,
            "settings.html",
            has_login=_has_login(ctx),
            data_dir=str(ctx.data_dir),
            message=None,
        )

    @app.post("/settings/forget", response_class=HTMLResponse)
    def forget_login(request: Request) -> Response:
        if ctx.demo:
            return render(request, "_message.html", message=DEMO_REFUSAL, ok=False)
        try:
            removed = ctx.store.forget()
        except CredentialError as exc:
            return render(request, "_message.html", message=f"Not removed: {exc}", ok=False)
        message = "Your saved OVO login was deleted." if removed else "No login was saved."
        return render(request, "_message.html", message=message, ok=True)

    @app.post("/settings/delete", response_class=HTMLResponse)
    async def delete_everything(request: Request) -> Response:
        form = await _form(request)
        if form.get("confirm", "") != DELETE_CONFIRMATION:
            return render(
                request,
                "_message.html",
                message=f"Type {DELETE_CONFIRMATION} (in capitals) to confirm.",
                ok=False,
            )
        lock = SyncLock(ctx.data_dir)
        try:
            lock.acquire()
        except SyncAlreadyRunning:
            return render(
                request,
                "_message.html",
                message="A sync is running. Try again when it has finished.",
                ok=False,
            )
        try:
            _delete_data(ctx)
        finally:
            lock.release()
        return render(
            request,
            "_message.html",
            message="All downloaded data and logs were deleted. Your saved login was kept.",
            ok=True,
        )

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return RedirectResponse("/static/favicon.svg", status_code=301)

    return SecurityMiddleware(app, port=ctx.port, sessions=ctx.sessions)


# -- helpers ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccountSummary:
    number: int
    first_supply: str | None
    coverage: list[store.Coverage]


def _account_summaries(conn: sqlite3.Connection) -> list[AccountSummary]:
    """Accounts numbered oldest first, as the CLI does. Ids are never shown."""
    repo = Repository(conn)
    found = []
    for account_id in repo.account_ids():
        starts = [p.start_date for p in repo.supply_points(account_id)]
        found.append((min(starts) if starts else None, account_id))
    found.sort(key=lambda item: (item[0] is None, item[0]))
    return [
        AccountSummary(
            number, first.isoformat() if first else None, store.coverage(conn, account_id)
        )
        for number, (first, account_id) in enumerate(found, start=1)
    ]


BaseLoadFromParam = Annotated[str | None, Query()]
BaseLoadToParam = Annotated[str | None, Query()]
RangeParam = Annotated[str, Query(alias="range")]
FromParam = Annotated[str | None, Query(alias="from")]
ToParam = Annotated[str | None, Query()]
EARLIEST_DATE = date(2000, 1, 1)
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_DIGITS = re.compile(r"\d+", re.ASCII)


@dataclass(frozen=True, slots=True)
class RangeChoice:
    """The dates being shown: a preset, or custom from/to dates (preset None)."""

    span: DateRange
    preset: Preset | None
    earliest: date | None  # first day with any data, for the date pickers
    yesterday: date

    @property
    def query(self) -> str:
        """The query string that reproduces this choice, for chart data URLs."""
        if self.preset is not None:
            return urlencode({"range": self.preset.value})
        return urlencode({"from": self.span.first.isoformat(), "to": self.span.last.isoformat()})


def _choose(
    conn: sqlite3.Connection,
    today: date,
    range_: str,
    from_: str | None,
    to: str | None,
    *,
    with_readings: bool = False,
) -> RangeChoice:
    """Custom dates win over a preset. Both must be given. `to` is capped at yesterday and
    `from` raised to the first day with data (nothing exists outside those). With
    `with_readings`, meter readings count as data too: they go back years before smart data."""
    found = list(queries.earliest_days(conn).values())
    if with_readings and (reading := queries.earliest_reading(conn)) is not None:
        found.append(reading)
    earliest = min(found, default=None)
    yesterday = today - timedelta(days=1)
    if from_ is None and to is None:
        preset = _preset(range_)
        span = charts.span_for(conn, preset, today, with_readings=with_readings)
        return RangeChoice(span, preset, earliest, yesterday)
    first, last = _date_param(from_), _date_param(to)
    if first > last:
        raise HTTPException(400)
    last = min(last, yesterday)
    if earliest is not None:
        first = max(first, min(earliest, last))
    if first > last:
        raise HTTPException(400)
    return RangeChoice(DateRange(first, last), None, earliest, yesterday)


def _page_number(value: str) -> int:
    if not _DIGITS.fullmatch(value) or len(value) > 6:
        raise HTTPException(400)
    return max(int(value), 1)


def _pick(picker: list[readings.SeriesChoice], value: str | None) -> readings.SeriesChoice | None:
    """The register chosen by `r` (its number in the picker); the first if none was given."""
    if not picker:
        return None
    if value is None:
        return picker[0]
    if not _DIGITS.fullmatch(value) or not 1 <= int(value) <= len(picker):
        raise HTTPException(400)
    return picker[int(value) - 1]


def _window(start: str | None, end: str | None) -> TimeWindow:
    """The base-load time window from `bl_from` and `bl_to` (both or neither)."""
    if start is None and end is None:
        return DEFAULT_WINDOW
    if start is None or end is None:
        raise HTTPException(400)
    try:
        return TimeWindow.parse(start, end)
    except ValueError:
        raise HTTPException(400) from None


def _date_param(value: str | None) -> date:
    if value is None or not _ISO_DATE.fullmatch(value):
        raise HTTPException(400)
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400) from None
    if day < EARLIEST_DATE:
        raise HTTPException(400)
    return day


def _preset(value: str) -> Preset:
    try:
        return Preset(value)
    except ValueError:
        raise HTTPException(400) from None


def _fuel(value: str) -> Fuel:
    try:
        return Fuel(value)
    except ValueError:
        raise HTTPException(400) from None


def _london_today() -> date:
    return datetime.now(LONDON).date()


def _has_login(ctx: WebContext) -> bool:
    try:
        return ctx.store.load() is not None
    except CredentialError:
        return False


def _london(instant: datetime | None) -> str:
    return "" if instant is None else instant.astimezone(LONDON).strftime("%d %b %Y %H:%M")


async def _form(
    request: Request, max_fields: int = 20, max_bytes: int = MAX_FORM_BYTES
) -> dict[str, str]:
    """A small urlencoded form (single values only). Avoids the python-multipart dependency."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(415)
    body = await request.body()
    if len(body) > max_bytes:
        raise HTTPException(413)
    try:
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=max_fields)
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(400) from None
    return {name: values[0] for name, values in parsed.items() if len(values) == 1}


def _delete_data(ctx: WebContext) -> None:
    """Delete the database (and its WAL files) and every log file. Call with the sync lock."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        (ctx.data_dir / f"{DATABASE_FILENAME}{suffix}").unlink(missing_ok=True)
    if ctx.log_handler is not None:
        ctx.log_handler.reset()
    directory = log_dir(ctx.data_dir)
    if directory.is_dir():
        for path in directory.iterdir():
            if path.is_file() and not path.is_symlink():
                path.unlink()
    log.info("All data deleted at the user's request")

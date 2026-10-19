"""The PRD's M6 security tests: Host, session, CSRF, Origin, headers, token handling."""

import importlib.util
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.types import Message

from ovo_insights.web.app import WebContext, create_app
from ovo_insights.web.security import (
    CONTENT_SECURITY_POLICY,
    SESSION_COOKIE,
    SecurityMiddleware,
    Sessions,
)
from tests.web.conftest import ORIGIN, PORT, csrf, http

pytestmark = pytest.mark.anyio


def assert_security_headers(response: httpx.Response) -> None:
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert "server" not in response.headers


@pytest.mark.parametrize("host", ["evil.example", f"evil.example:{PORT}", "127.0.0.1:1", ""])
async def test_wrong_host_is_refused_with_400(ctx: WebContext, host: str) -> None:
    async with http(ctx) as client:
        response = await client.get(f"/?token={ctx.sessions.launch_token}", headers={"Host": host})

    assert response.status_code == 400
    assert_security_headers(response)
    assert not ctx.sessions.launch_token_used  # refused before the token was looked at


async def test_localhost_is_an_accepted_host(ctx: WebContext) -> None:
    async with http(ctx, base_url=f"http://localhost:{PORT}") as client:
        response = await client.get("/")

    assert response.status_code == 403  # past the Host check, stopped by the session check


async def test_duplicate_host_headers_are_refused(ctx: WebContext) -> None:
    app = create_app(ctx)
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    host = f"127.0.0.1:{PORT}".encode()
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [(b"host", host), (b"host", host)],
    }
    await app(scope, receive, send)

    assert sent[0]["status"] == 400


async def test_requests_without_a_session_are_refused(anonymous: httpx.AsyncClient) -> None:
    for path in ("/", "/sync", "/settings", "/static/app.css", "/nope"):
        response = await anonymous.get(path)
        assert response.status_code == 403, path
        assert "Open the link printed by `ovo-insights serve`" in response.text
        assert_security_headers(response)


async def test_a_forged_session_cookie_is_refused(ctx: WebContext) -> None:
    async with http(ctx) as client:
        response = await client.get("/", headers={"Cookie": f"{SESSION_COOKIE}=guess"})
        broken = await client.get("/", headers={"Cookie": 'bad"cookie'})
        unparseable = await client.get("/", headers={"Cookie": "a=b; \x00=c"})

    assert response.status_code == broken.status_code == unparseable.status_code == 403


async def test_launch_token_becomes_a_strict_http_only_cookie_and_leaves_the_url(
    ctx: WebContext,
) -> None:
    async with http(ctx) as client:
        response = await client.get(f"/?token={ctx.sessions.launch_token}")

        assert response.status_code == 303
        assert response.headers["location"] == "/"  # the token is gone from the URL
        cookie = response.headers["set-cookie"]
        assert cookie.startswith(f"{SESSION_COOKIE}=")
        for attribute in ("HttpOnly", "SameSite=Strict", "Path=/"):
            assert attribute in cookie
        assert ctx.sessions.launch_token not in cookie  # the session id is a different secret
        assert_security_headers(response)

        followed = await client.get("/")
        assert followed.status_code == 200
        assert ctx.sessions.launch_token not in str(followed.url)


async def test_launch_token_works_once(ctx: WebContext) -> None:
    link = f"/?token={ctx.sessions.launch_token}"
    async with http(ctx) as first, http(ctx) as second:
        assert (await first.get(link)).status_code == 303
        again = await second.get(link)

    assert again.status_code == 403
    assert "already used" in again.text


@pytest.mark.parametrize("query", ["token=wrong", "token=", "token=a&token=b"])
async def test_bad_launch_tokens_are_refused(ctx: WebContext, query: str) -> None:
    async with http(ctx) as client:
        response = await client.get(f"/?{query}")

    assert response.status_code == 403
    assert not ctx.sessions.launch_token_used


async def test_post_without_csrf_token_is_refused(
    ctx: WebContext, browser: httpx.AsyncClient
) -> None:
    response = await browser.post("/sync", headers={"Origin": ORIGIN})

    assert response.status_code == 403
    assert_security_headers(response)


async def test_post_with_wrong_csrf_token_is_refused(
    ctx: WebContext, browser: httpx.AsyncClient
) -> None:
    response = await browser.post("/sync", headers={"Origin": ORIGIN, "X-CSRF-Token": "x"})

    assert response.status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://evil.example"},
        {"Origin": f"http://127.0.0.1:{PORT + 1}"},
        {"Origin": "null"},
        {},  # neither Origin nor Sec-Fetch-Site
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
async def test_cross_origin_posts_are_refused_even_with_the_csrf_token(
    ctx: WebContext, browser: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    token = csrf(ctx)["X-CSRF-Token"]

    response = await browser.post("/sync", headers={"X-CSRF-Token": token, **headers})

    assert response.status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": ORIGIN},
        {"Origin": f"http://localhost:{PORT}"},
        {"Sec-Fetch-Site": "same-origin"},
    ],
)
async def test_same_origin_posts_with_the_csrf_token_are_accepted(
    ctx: WebContext, browser: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    token = csrf(ctx)["X-CSRF-Token"]

    response = await browser.post("/sync", headers={"X-CSRF-Token": token, **headers})

    assert response.status_code == 200


async def test_every_page_carries_the_security_headers(browser: httpx.AsyncClient) -> None:
    for path in (
        "/",
        "/sync",
        "/sync/progress",
        "/setup",
        "/settings",
        "/half-hourly",
        "/tariffs",
        "/readings",
        "/static/app.css",
        "/static/vendor/htmx-2.0.11.min.js",
        "/nope",
    ):
        response = await browser.get(path)
        assert response.status_code in (200, 404), path
        assert_security_headers(response)


async def test_api_docs_and_schema_are_disabled(browser: httpx.AsyncClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await browser.get(path)).status_code == 404


async def test_websockets_are_never_accepted() -> None:
    called: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        called.append(scope["type"])

    middleware = SecurityMiddleware(app, port=PORT, sessions=Sessions())
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await middleware({"type": "websocket", "headers": []}, receive, send)
    await middleware({"type": "lifespan"}, receive, send)

    assert called == ["lifespan"]
    assert sent == []


def test_telemetry_is_switched_off(ctx: WebContext) -> None:
    app = create_app(ctx)
    assert isinstance(app, SecurityMiddleware)
    inner = app._app
    assert isinstance(inner, FastAPI)

    config = inner._telemetry
    assert not any(
        config[k] for k in ("tracing", "metrics", "logs", "operation_spans", "auto_configure")
    )
    assert not inner._native_telemetry.enabled()
    # Nothing could export telemetry even if it were on.
    for module in ("opentelemetry.sdk", "opentelemetry.exporter"):
        assert importlib.util.find_spec(module.split(".")[0]) is not None
        assert _missing(module)


def _missing(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is None
    except ModuleNotFoundError:
        return True


async def test_lifespan_runs_the_scheduler_and_cancels_it_on_shutdown(ctx: WebContext) -> None:
    import asyncio

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def scheduler() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    ctx.scheduler = scheduler
    app = create_app(ctx)
    messages: asyncio.Queue[Message] = asyncio.Queue()
    for kind in ("lifespan.startup", "lifespan.shutdown"):
        messages.put_nowait({"type": kind})
    sent: list[Message] = []

    async def receive() -> Message:
        message = await messages.get()
        if message["type"] == "lifespan.shutdown":
            await started.wait()
        return message

    async def send(message: Message) -> None:
        sent.append(message)

    await app({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}, receive, send)
    await asyncio.sleep(0)

    assert [m["type"] for m in sent] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]
    assert started.is_set()
    assert cancelled.is_set()

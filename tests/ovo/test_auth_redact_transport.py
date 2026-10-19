import logging
import pickle
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ovo_insights.ovo.auth import (
    REFRESH_MARGIN,
    Credentials,
    Secret,
    TokenState,
    decode_access_token,
)
from ovo_insights.ovo.errors import AuthFailed, DisallowedHostError
from ovo_insights.ovo.redact import RedactingFilter, install_redaction, redact
from ovo_insights.ovo.transport import AllowlistTransport, RateLimiter, make_client
from tests.ovo.conftest import b64, make_jwt

pytestmark = pytest.mark.anyio

# -- Secret / Credentials -----------------------------------------------------------------


def test_secret_never_shows_its_value() -> None:
    secret = Secret("hunter2")

    assert "hunter2" not in repr(secret)
    assert "hunter2" not in str(secret)
    assert "hunter2" not in f"{secret}"
    assert "hunter2" not in repr(Credentials("a@b.co", secret))
    assert secret.reveal() == "hunter2"


def test_secret_cannot_be_pickled() -> None:
    with pytest.raises(TypeError, match="pickled"):
        pickle.dumps(Secret("hunter2"))


def test_secret_equality_and_hash() -> None:
    assert Secret("a") == Secret("a")
    assert Secret("a") != Secret("b")
    assert Secret("a") != "a"
    assert len({Secret("a"), Secret("a")}) == 1


@pytest.mark.parametrize("value", ["", 123, None])
def test_secret_rejects_empty_or_non_strings(value: object) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        Secret(value)  # type: ignore[arg-type]


@pytest.mark.parametrize("email", ["", "no-at-sign", "a" * 250 + "@b.co"])
def test_credentials_validate_email(email: str) -> None:
    with pytest.raises(ValueError, match="email"):
        Credentials(email, Secret("x"))


def test_credentials_require_secret() -> None:
    with pytest.raises(TypeError, match="Secret"):
        Credentials("a@b.co", "plain")  # type: ignore[arg-type]


def test_token_state_clear() -> None:
    state = TokenState(login_cookies={"a": Secret("b")}, customer_id="c")
    state.access = decode_access_token(make_jwt(datetime(2030, 1, 1, tzinfo=UTC)))

    state.clear()

    assert state == TokenState()
    assert "b" not in repr(TokenState(login_cookies={"a": Secret("bbbb")}))


# -- JWT ----------------------------------------------------------------------------------


def test_decode_access_token() -> None:
    exp = datetime(2030, 1, 1, tzinfo=UTC)
    token = decode_access_token(
        make_jwt(exp, sub="cust", permissions=["account::123", "account::abc", "other", 5])  # type: ignore[list-item]
    )

    assert token.expires_at == exp
    assert token.subject == "cust"
    assert token.account_ids == ("123",)
    assert not token.needs_refresh(exp - REFRESH_MARGIN - timedelta(seconds=1))
    assert token.needs_refresh(exp - REFRESH_MARGIN)


@pytest.mark.parametrize(
    ("lifetime", "margin"),
    [
        (timedelta(hours=1), REFRESH_MARGIN),  # long-lived: the usual minute
        (timedelta(seconds=60), timedelta(seconds=15)),  # short-lived: a quarter
        (timedelta(seconds=-5), timedelta(0)),  # already expired (clock skew): no margin
    ],
)
def test_short_lived_tokens_are_reused_until_near_expiry(
    lifetime: timedelta, margin: timedelta
) -> None:
    now = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    token = decode_access_token(make_jwt(now + lifetime), now)

    assert token.refresh_margin == margin
    if lifetime > timedelta(0):
        assert not token.needs_refresh(now)  # usable straight away
    assert token.needs_refresh(now + lifetime - margin)


@pytest.mark.parametrize(
    "jwt",
    [
        "not-a-jwt",
        "a.b",
        "a.!!!.c",
        f"h.{b64({'sub': 'x'})}.s",
        f"h.{b64({'exp': True})}.s",
        f"h.{b64({'exp': 10**20})}.s",
        "h." + "eyJ" + "x" * 20000 + ".s",
        "h.WzFd.s",  # payload is a JSON list
    ],
)
def test_decode_rejects_bad_tokens(jwt: str) -> None:
    with pytest.raises(AuthFailed):
        decode_access_token(jwt)


# -- redaction ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("token eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl here", "eyJhbGciOi"),
        ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
        ("headers={'authorization': 'Bearer xyz'}", "xyz"),
        ("Cookie: restricted_refresh_token=SEKRIT; other=1", "SEKRIT"),
        ("set-cookie=foo=bar", "foo=bar"),
        ('{"password": "hunter2"}', "hunter2"),
        ("password=hunter2 next", "hunter2"),
        ("contact someone@realmail.co.uk now", "someone@realmail.co.uk"),
    ],
)
def test_redact_masks_secrets(text: str, secret: str) -> None:
    assert secret not in redact(text)


def test_redact_leaves_ordinary_text() -> None:
    assert redact("Fetched 48 half-hours for 2025-01-01") == "Fetched 48 half-hours for 2025-01-01"


def test_filter_redacts_args_and_tracebacks() -> None:
    record = logging.LogRecord(
        "x", logging.ERROR, __file__, 1, "login %s", ("password=hunter2",), None
    )
    try:
        raise RuntimeError("Bearer abc123")
    except RuntimeError:
        import sys

        record.exc_info = sys.exc_info()

    assert RedactingFilter().filter(record) is True
    assert "hunter2" not in record.getMessage()
    assert "abc123" not in record.getMessage()
    assert "RuntimeError" in record.getMessage()
    assert record.exc_info is None


def test_install_redaction_is_idempotent() -> None:
    logger = logging.getLogger("ovo-test-install")
    handler = logging.StreamHandler()
    logger.addHandler(handler)
    try:
        install_redaction(logger)
        install_redaction(logger)
        assert sum(isinstance(f, RedactingFilter) for f in handler.filters) == 1
    finally:
        logger.removeHandler(handler)


# -- transport ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/",
        "http://my.ovoenergy.com/api",
        "https://my.ovoenergy.com:8443/api",
        "https://my.ovoenergy.com.evil.example/",
        "https://ovoenergy.com/",
    ],
)
async def test_allowlist_refuses_other_destinations(url: str) -> None:
    async with make_client(httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        with pytest.raises(DisallowedHostError):
            await client.get(url)


async def test_allowlist_permits_ovo_hosts() -> None:
    async with make_client(httpx.MockTransport(lambda r: httpx.Response(204))) as client:
        for host in ("my.ovoenergy.com", "smartpaymapi.ovoenergy.com", "api.eu1.prod.kaluza.com"):
            assert (await client.get(f"https://{host}/x")).status_code == 204


async def test_client_never_stores_cookies() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"set-cookie": "sid=abc; Domain=.ovoenergy.com"})

    async with make_client(httpx.MockTransport(handler)) as client:
        response = await client.get("https://my.ovoenergy.com/x")
        assert response.cookies.get("sid") == "abc"  # readable from the response...
        assert not list(client.cookies.jar)  # ...but never kept by the client


async def test_allowlist_transport_closes_inner() -> None:
    closed = []

    class Inner(httpx.AsyncBaseTransport):
        async def aclose(self) -> None:
            closed.append(True)

    await AllowlistTransport(Inner()).aclose()
    assert closed == [True]


async def test_rate_limiter_spaces_requests() -> None:
    clock = [100.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    limiter = RateLimiter(1.0, clock=lambda: clock[0], sleep=sleep)
    await limiter.wait()
    clock[0] += 0.25
    await limiter.wait()
    clock[0] += 5
    await limiter.wait()

    assert sleeps == [0.75]


async def test_rate_limiter_awaits_async_sleep() -> None:
    clock = [0.0]
    awaited: list[float] = []

    async def sleep(seconds: float) -> None:
        awaited.append(seconds)

    limiter = RateLimiter(2.0, clock=lambda: clock[0], sleep=sleep)
    await limiter.wait()
    await limiter.wait()

    assert awaited == [2.0]


def test_rate_limiter_slows_down_and_never_speeds_up_again() -> None:
    limiter = RateLimiter(0)

    assert limiter.slow_down() == 1.0  # from nothing to at least a second
    assert [limiter.slow_down() for _ in range(5)] == [2.0, 4.0, 8.0, 16.0, 30.0]
    assert limiter.slow_down() == RateLimiter.MAX_INTERVAL
    assert limiter.min_interval == RateLimiter.MAX_INTERVAL

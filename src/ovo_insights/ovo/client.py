"""Async client for OVO's unofficial API.

Flow (from My-Ovo-Data v1.0.9 and timmo001/ovoenergy, to be confirmed by `doctor`):
  1. POST my.ovoenergy.com/api/v2/auth/login  -> userId + cookies (incl. restricted_refresh_token)
  2. GET  my.ovoenergy.com/api/v2/auth/token  with those cookies -> access-token JWT
  3. Bearer JWT on Kaluza GraphQL (accounts, meter reads) and smartpaymapi usage endpoints.

Defensive rules: one shared rate limiter; only 5xx/timeouts are retried (with jittered
backoff); 401 triggers at most one token refresh and one re-login per call; Cloudflare/403
and failed logins are never retried; bodies over the size cap are refused; JSON numbers are
parsed as Decimal; unknown response fields are logged once by name.
"""

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from importlib.resources import files
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from ovo_insights.domain import DailyUsage, HalfHourUsage, MonthlyUsage
from ovo_insights.ovo import convert, models
from ovo_insights.ovo.auth import Credentials, Secret, TokenState, decode_access_token
from ovo_insights.ovo.errors import (
    AuthFailed,
    BlockedByUpstream,
    RateLimited,
    ResponseTooLarge,
    Unauthorized,
    UpstreamSchemaChanged,
    UpstreamUnavailable,
)
from ovo_insights.ovo.transport import RateLimiter, make_client

log = logging.getLogger(__name__)

LOGIN_URL = "https://my.ovoenergy.com/api/v2/auth/login"
TOKEN_URL = "https://my.ovoenergy.com/api/v2/auth/token"  # noqa: S105 - a URL, not a secret
GRAPHQL_URL = "https://api.eu1.prod.kaluza.com/graphql/1"
USAGE_BASE = "https://smartpaymapi.ovoenergy.com/usage/api"

MAX_BODY_BYTES = 20 * 1024 * 1024
MAX_ATTEMPTS = 3
# OVO has been seen asking for 60 s. Waits up to this long are honoured and the request
# retried; anything longer stops the run.
MAX_RETRY_AFTER_SECONDS = 120.0
# OVO's usage API answered about every other request with 429 at one request a second
# (first real sync, 2026-10-13), so start at one every two seconds; 429s widen it further.
DEFAULT_MIN_INTERVAL = 2.0
REFRESH_COOKIE = "restricted_refresh_token"

# Verified against a real account on 2026-10-11 (a BST day): both half-hourly endpoints send
# naive UTC timestamps. `half-hourly-local` covers the UK day (23:00 to 22:30 UTC), and
# `half-hourly` the UTC day (00:00 to 23:30 UTC); values at the same timestamp are identical.
# The local variant is the default because it returns exactly one UK day (46/48/50 slots).
HALF_HOURLY_VARIANTS = ("half-hourly-local", "half-hourly")


def _query(name: str) -> str:
    """A GraphQL document with our comment lines removed (they are for us, not OVO)."""
    text = files("ovo_insights.ovo.queries").joinpath(name).read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


class OvoClient:
    def __init__(
        self,
        credentials: Credentials,
        *,
        http: httpx.AsyncClient | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        half_hourly_variant: str = HALF_HOURLY_VARIANTS[0],
    ) -> None:
        if half_hourly_variant not in HALF_HOURLY_VARIANTS:
            raise ValueError(f"Unknown half-hourly variant: {half_hourly_variant!r}")
        self._credentials = credentials
        self._http = http or make_client()
        self._limiter = limiter or RateLimiter(DEFAULT_MIN_INTERVAL)
        self._sleep = sleep
        self._now = now
        self._state = TokenState()
        self._half_hourly_variant = half_hourly_variant
        self._reported_unknown: set[str] = set()
        self.request_count = 0

    async def __aenter__(self) -> "OvoClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._state.clear()
        await self._http.aclose()

    @property
    def customer_id(self) -> str | None:
        return self._state.customer_id

    @property
    def token_account_ids(self) -> tuple[str, ...]:
        """Account ids listed in the access token's permissions (fallback discovery)."""
        return self._state.access.account_ids if self._state.access else ()

    # -- auth -----------------------------------------------------------------------------

    async def login(self) -> None:
        """Log in and obtain an access token. Raises AuthFailed; never retried."""
        self._state.clear()
        body = {
            "username": self._credentials.email,
            "password": self._credentials.password.reveal(),
            "rememberMe": True,
        }
        response = await self._send("POST", LOGIN_URL, json=body, auth=False, retry=False)
        if response.status_code in (400, 401, 403):
            raise AuthFailed(f"Login refused (HTTP {response.status_code})")
        if response.status_code != 200:
            raise AuthFailed(f"Unexpected login response (HTTP {response.status_code})")
        login = self._parse(response, models.LoginResponse, "login")
        if login.code is not None and login.code.lower() == "unknown":
            # OVO answers a bad password with HTTP 200 and {"code": "Unknown"}.
            raise AuthFailed("Login refused (check email and password)")

        cookies = {name: Secret(value) for name, value in response.cookies.items() if value}
        if REFRESH_COOKIE not in cookies:
            raise AuthFailed("Login succeeded but no refresh cookie was issued")
        self._state.login_cookies = cookies
        await self._refresh_access_token()
        self._state.customer_id = login.user_id or (
            self._state.access.subject if self._state.access else None
        )
        if not self._state.customer_id:
            raise AuthFailed("Could not determine the customer id after login")

    async def _refresh_access_token(self) -> None:
        if not self._state.login_cookies:
            raise AuthFailed("Not logged in")
        cookie_header = "; ".join(
            f"{name}={secret.reveal()}" for name, secret in self._state.login_cookies.items()
        )
        refresh = self._state.login_cookies[REFRESH_COOKIE].reveal()
        response = await self._send(
            "GET",
            TOKEN_URL,
            # Sent both ways: as cookies (timmo001) and as the header My-Ovo-Data uses.
            headers={"Cookie": cookie_header, REFRESH_COOKIE: refresh},
            auth=False,
            retry=True,
        )
        if response.status_code in (400, 401, 403):
            raise AuthFailed(f"Token request refused (HTTP {response.status_code})")
        if response.status_code != 200:
            raise AuthFailed(f"Unexpected token response (HTTP {response.status_code})")
        token = self._parse(response, models.TokenResponse, "token")
        now = self._now()
        self._state.access = decode_access_token(token.access_token.value, now)
        log.info(
            "Got an access token valid for %.0f s",
            (self._state.access.expires_at - now).total_seconds(),
        )

    async def _ensure_token(self) -> None:
        access = self._state.access
        if access is None:
            await self.login()
        elif access.needs_refresh(self._now()):
            try:
                await self._refresh_access_token()
            except AuthFailed:
                log.info("Token refresh failed; logging in again")
                await self.login()

    # -- data -----------------------------------------------------------------------------

    async def accounts(self) -> list[convert.AccountSummary]:
        await self._ensure_token()
        payload = {
            "operationName": "Bootstrap",
            "query": _query("bootstrap.graphql"),
            "variables": {"customerId": self._state.customer_id},
        }
        response = await self._authorized("POST", GRAPHQL_URL, json=payload)
        return convert.accounts(self._parse(response, models.BootstrapResponse, "accounts"))

    async def meter_data(self, account_id: str) -> convert.MeterData:
        payload = {
            "operationName": "MeterReads_nextV1",
            "query": _query("meter_reads.graphql"),
            "variables": {"accountId": account_id, "query": {"includeReads": "TOP_VALID_ONLY"}},
        }
        response = await self._authorized("POST", GRAPHQL_URL, json=payload)
        return convert.meter_data(
            account_id, self._parse(response, models.ReadsResponse, "meter reads")
        )

    async def monthly(self, account_id: str, year: int) -> list[MonthlyUsage]:
        _check_year(year)
        response = await self._authorized(
            "GET", f"{USAGE_BASE}/monthly/{_path_id(account_id)}", params={"date": f"{year:04d}"}
        )
        return convert.monthly(account_id, self._parse(response, models.MonthlyResponse, "monthly"))

    async def daily(self, account_id: str, year: int, month: int) -> list[DailyUsage]:
        _check_year(year)
        if not 1 <= month <= 12:
            raise ValueError(f"Invalid month: {month}")
        response = await self._authorized(
            "GET",
            f"{USAGE_BASE}/daily/{_path_id(account_id)}",
            params={"date": f"{year:04d}-{month:02d}"},
        )
        return convert.daily(account_id, self._parse(response, models.DailyResponse, "daily"))

    async def half_hourly(
        self, account_id: str, day: date, *, variant: str | None = None
    ) -> list[HalfHourUsage]:
        """Half-hours for one day: the UK day for `half-hourly-local` (the default), the UTC
        day for `half-hourly`. Timestamps are UTC either way."""
        variant = variant or self._half_hourly_variant
        if variant not in HALF_HOURLY_VARIANTS:
            raise ValueError(f"Unknown half-hourly variant: {variant!r}")
        if isinstance(day, datetime):
            raise TypeError("day must be a date")
        _check_year(day.year)
        response = await self._authorized(
            "GET",
            f"{USAGE_BASE}/{variant}/{_path_id(account_id)}",
            params={"date": day.isoformat()},
        )
        return convert.half_hours(
            account_id,
            self._parse(response, models.HalfHourResponse, "half-hourly"),
            naive_is_utc=True,
        )

    async def raw_json(self, method: str, url: str, **kwargs: Any) -> object:
        """Authorized request returning parsed JSON, for `capture-fixtures` only."""
        response = await self._authorized(method, url, **kwargs)
        return _load_json(response)

    # -- plumbing -------------------------------------------------------------------------

    async def _authorized(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Send with a bearer token. On 401: refresh the token once; if still refused, log in
        again once; if still refused, give up. Never more than one re-login per call."""
        await self._ensure_token()
        response = await self._send(method, url, **kwargs)
        if response.status_code != 401:
            return self._check_status(response)

        log.info("Access token rejected; refreshing")
        relogged = False
        try:
            await self._refresh_access_token()
        except AuthFailed:
            await self.login()
            relogged = True
        response = await self._send(method, url, **kwargs)
        if response.status_code == 401 and not relogged:
            await self.login()
            response = await self._send(method, url, **kwargs)
        if response.status_code == 401:
            raise Unauthorized("OVO refused the request even after logging in again")
        return self._check_status(response)

    async def _send(
        self,
        method: str,
        url: str,
        *,
        auth: bool = True,
        retry: bool = True,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        request_headers = dict(headers or {})
        if auth:
            access = self._state.access
            if access is None:
                raise AuthFailed("Not logged in")
            request_headers["Authorization"] = f"Bearer {access.value.reveal()}"

        attempts = MAX_ATTEMPTS if retry else 1
        for attempt in range(1, attempts + 1):
            await self._limiter.wait()
            self.request_count += 1
            try:
                response = await self._http.request(method, url, headers=request_headers, **kwargs)
                await self._read_capped(response)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                error: Exception = UpstreamUnavailable(f"{type(exc).__name__} contacting OVO")
            else:
                if _is_cloudflare_block(response):
                    raise BlockedByUpstream(
                        f"OVO's edge blocked the request (HTTP {response.status_code})"
                    )
                if response.status_code == 429:
                    retry_after = _retry_after(response)
                    interval = self._limiter.slow_down()
                    log.warning(
                        "OVO asked us to slow down (HTTP 429, Retry-After: %s); now at most "
                        "one request every %.0f s",
                        "none" if retry_after is None else f"{retry_after:.0f} s",
                        interval,
                    )
                    if (
                        attempt < attempts
                        and retry_after is not None
                        and retry_after <= MAX_RETRY_AFTER_SECONDS
                    ):
                        await self._sleep(retry_after)
                        continue
                    wait = "" if retry_after is None else f" (asked to wait {retry_after:.0f} s)"
                    raise RateLimited(f"OVO rate-limited the request{wait}", retry_after)
                if response.status_code < 500:
                    return response
                error = UpstreamUnavailable(f"OVO returned HTTP {response.status_code}")
            if attempt == attempts:
                raise error
            # Full jitter: up to 1s, 2s, 4s...
            await self._sleep(random.uniform(0, 2 ** (attempt - 1)))  # noqa: S311 - not crypto
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    async def _read_capped(response: httpx.Response) -> None:
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            raise ResponseTooLarge(f"Response of {declared} bytes exceeds the cap")
        await response.aread()
        if len(response.content) > MAX_BODY_BYTES:
            raise ResponseTooLarge("Response exceeds the size cap")

    @staticmethod
    def _check_status(response: httpx.Response) -> httpx.Response:
        if response.status_code == 403:
            raise BlockedByUpstream("OVO refused access (HTTP 403)")
        if response.status_code == 404:
            raise UpstreamSchemaChanged("Endpoint not found (HTTP 404); OVO may have moved it")
        if response.status_code != 200:
            raise UpstreamSchemaChanged(f"Unexpected HTTP {response.status_code}")
        return response

    def _parse[M: BaseModel](self, response: httpx.Response, model: type[M], what: str) -> M:
        payload = _load_json(response)
        try:
            parsed = model.model_validate(payload)
        except ValidationError as exc:
            # Report field locations and error types only; never input values.
            problems = sorted(
                {".".join(map(str, e["loc"])) + f" ({e['type']})" for e in exc.errors()}
            )
            raise UpstreamSchemaChanged(
                f"{what} response did not match the expected shape: {', '.join(problems[:10])}"
            ) from None
        for field in sorted(models.unknown_fields(parsed) - self._reported_unknown):
            log.warning("OVO %s response has an unrecognised field: %s", what, field)
            self._reported_unknown.add(field)
        return parsed


def _load_json(response: httpx.Response) -> object:
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type.lower():
        raise UpstreamSchemaChanged(f"Expected JSON, got {content_type.split(';')[0] or 'nothing'}")
    try:
        return json.loads(response.content, parse_float=Decimal, parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError) as exc:
        raise UpstreamSchemaChanged("Response is not valid JSON") from exc


def _reject_constant(name: str) -> object:
    raise ValueError(f"Non-standard JSON constant {name}")


def _is_cloudflare_block(response: httpx.Response) -> bool:
    if response.status_code not in (403, 429, 503):
        return False
    is_html = "text/html" in response.headers.get("content-type", "").lower()
    from_cloudflare = "cf-ray" in response.headers or "cf-mitigated" in response.headers
    return from_cloudflare and (is_html or "cf-mitigated" in response.headers)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after", "").strip()
    if value.isascii() and value.isdigit():
        return float(value)
    return None


def _check_year(year: int) -> None:
    if isinstance(year, bool) or not isinstance(year, int) or not 2000 <= year <= 2100:
        raise ValueError(f"Implausible year: {year!r}")


def _path_id(account_id: str) -> str:
    # Account ids go into URL paths, so only plain ASCII digits and letters are allowed.
    if not (account_id.isascii() and account_id.isalnum() and len(account_id) <= 32):
        raise ValueError("Account id must be ASCII letters/digits only")
    return account_id

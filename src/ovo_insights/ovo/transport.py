"""HTTP plumbing: host allowlist, rate limiting and the shared client."""

import asyncio
import time
from collections.abc import Callable
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx

from ovo_insights import __version__
from ovo_insights.ovo.errors import DisallowedHostError

ALLOWED_HOSTS = frozenset(
    {"my.ovoenergy.com", "smartpaymapi.ovoenergy.com", "api.eu1.prod.kaluza.com"}
)
USER_AGENT = f"ovo-insights/{__version__} (+https://github.com/sjanaway/ovo-insights)"
TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


class AllowlistTransport(httpx.AsyncBaseTransport):
    """Refuses any request whose host isn't an OVO host, or that isn't HTTPS on port 443."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.scheme != "https" or url.host not in ALLOWED_HOSTS or url.port not in (None, 443):
            raise DisallowedHostError(f"Refusing request to {url.scheme}://{url.host}")
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class RateLimiter:
    """At most one request per `min_interval` seconds, across all endpoints.

    `slow_down` widens the interval after OVO answers 429. It never narrows again for the
    life of the limiter (one client, so one sync run), so a run settles at a pace OVO accepts.
    """

    MAX_INTERVAL = 30.0

    def __init__(
        self,
        min_interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] = asyncio.sleep,
    ) -> None:
        self._min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last: float | None = None

    @property
    def min_interval(self) -> float:
        return self._min_interval

    def slow_down(self) -> float:
        """Double the interval (at least 1 s, at most MAX_INTERVAL). A 429's Retry-After is
        a one-off wait, which the client sleeps separately; it isn't the new pace."""
        self._min_interval = min(self.MAX_INTERVAL, max(self._min_interval * 2, 1.0))
        return self._min_interval

    async def wait(self) -> None:
        async with self._lock:
            now = self._clock()
            if self._last is not None:
                delay = self._last + self._min_interval - now
                if delay > 0:
                    result = self._sleep(delay)
                    if asyncio.iscoroutine(result):
                        await result
                    now = self._clock()
            self._last = now


def make_client(inner: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """The one HTTP client. TLS verification is always on; redirects are never followed."""
    return httpx.AsyncClient(
        transport=AllowlistTransport(inner or httpx.AsyncHTTPTransport(verify=True, retries=0)),
        timeout=TIMEOUT,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        # A jar that accepts nothing: cookies are read from the login response and sent only
        # to the token endpoint, explicitly. They must never ride along to other hosts.
        cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
        trust_env=False,
    )

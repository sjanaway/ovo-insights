from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
import respx

from ovo_insights.credentials import MemoryCredentialStore
from ovo_insights.ovo.auth import Credentials, Secret
from ovo_insights.ovo.client import OvoClient
from ovo_insights.ovo.transport import RateLimiter, make_client
from ovo_insights.web.app import WebContext, create_app
from ovo_insights.web.sync_service import SyncService

PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"
PASSWORD = "pw-SENTINEL-never-shown"
CREDS = Credentials("someone@example.com", Secret(PASSWORD))


async def _no_sleep(seconds: float) -> None:
    return None


def fast_client(credentials: Credentials) -> OvoClient:
    return OvoClient(
        credentials, http=make_client(), limiter=RateLimiter(0, sleep=_no_sleep), sleep=_no_sleep
    )


@dataclass
class LoginChecks:
    """Stands in for checking a login with OVO."""

    calls: list[Credentials] = field(default_factory=list)
    error: Exception | None = None

    async def __call__(self, credentials: Credentials) -> None:
        self.calls.append(credentials)
        if self.error is not None:
            raise self.error


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


@pytest.fixture
def store() -> MemoryCredentialStore:
    return MemoryCredentialStore()


@pytest.fixture
def login_checks() -> LoginChecks:
    return LoginChecks()


@pytest.fixture
def ctx(data_dir: Path, store: MemoryCredentialStore, login_checks: LoginChecks) -> WebContext:
    service = SyncService(data_dir, load_credentials=store.load, client_factory=fast_client)
    return WebContext(data_dir, PORT, store, service, login_checks)


def http(ctx: WebContext, *, base_url: str = ORIGIN) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(ctx), raise_app_exceptions=False),
        base_url=base_url,
    )


@pytest.fixture
async def anonymous(ctx: WebContext) -> AsyncIterator[httpx.AsyncClient]:
    async with http(ctx) as client:
        yield client


@pytest.fixture
async def browser(ctx: WebContext) -> AsyncIterator[httpx.AsyncClient]:
    """A signed-in browser: the launch link exchanged for a session cookie."""
    async with http(ctx) as client:
        response = await client.get(f"/?token={ctx.sessions.launch_token}")
        assert response.status_code == 303
        yield client


def csrf(ctx: WebContext) -> dict[str, str]:
    assert ctx.sessions._session is not None
    token = ctx.sessions._session[1]
    return {"X-CSRF-Token": token, "Origin": ORIGIN}


def form(ctx: WebContext, **extra: str) -> dict[str, str]:
    return {**csrf(ctx), "Content-Type": "application/x-www-form-urlencoded", **extra}

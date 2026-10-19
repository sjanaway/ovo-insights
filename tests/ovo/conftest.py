import base64
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import respx

from ovo_insights.ovo.auth import Credentials, Secret
from ovo_insights.ovo.client import GRAPHQL_URL, LOGIN_URL, TOKEN_URL, USAGE_BASE, OvoClient
from ovo_insights.ovo.transport import RateLimiter, make_client

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "synthetic"
PASSWORD = "correct-horse-battery-staple-SENTINEL"
EMAIL = "someone@example.com"
NOW = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
ACCOUNT = "7654321"


def b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def make_jwt(exp: datetime, sub: str = "cust-1", permissions: list[str] | None = None) -> str:
    payload = {"exp": int(exp.timestamp()), "sub": sub, "permissions": permissions or []}
    return f"{b64({'alg': 'RS256'})}.{b64(payload)}.signature"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def token_body(exp: datetime) -> dict[str, Any]:
    body: dict[str, Any] = fixture("token")
    body["accessToken"]["value"] = make_jwt(exp, permissions=[f"account::{ACCOUNT}"])
    return body


class Sleeps(list[float]):
    async def __call__(self, seconds: float) -> None:
        self.append(seconds)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


def mock_login(mock: respx.MockRouter, exp: datetime | None = None) -> None:
    mock.post(LOGIN_URL).respond(
        200,
        json=fixture("login"),
        headers=[
            ("set-cookie", "restricted_refresh_token=REFRESH-SECRET; Path=/; Secure; HttpOnly"),
            ("set-cookie", "other=OTHER-SECRET; Path=/"),
        ],
    )
    mock.get(TOKEN_URL).respond(200, json=token_body(exp or datetime(2030, 1, 1, tzinfo=UTC)))


async def _no_wait(seconds: float) -> None:
    return None


@pytest.fixture
async def client(sleeps: Sleeps) -> AsyncIterator[OvoClient]:
    c = OvoClient(
        Credentials(EMAIL, Secret(PASSWORD)),
        http=make_client(),
        # The limiter's own waits are not under test here (a 429 widens its interval).
        limiter=RateLimiter(0, sleep=_no_wait),
        sleep=sleeps,
        now=lambda: NOW,
    )
    try:
        yield c
    finally:
        await c.aclose()


__all__ = ["ACCOUNT", "GRAPHQL_URL", "USAGE_BASE", "fixture", "mock_login"]

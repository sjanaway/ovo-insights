from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
import respx

from ovo_insights.ovo.auth import Credentials, Secret
from ovo_insights.ovo.client import OvoClient
from ovo_insights.ovo.transport import RateLimiter, make_client

# A Tuesday morning in BST.
START = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def _no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


def make_ovo_client(clock: Clock) -> OvoClient:
    return OvoClient(
        Credentials("someone@example.com", Secret("pw-SENTINEL")),
        http=make_client(),
        limiter=RateLimiter(0, sleep=_no_sleep),
        sleep=_no_sleep,
        now=clock,
    )


@pytest.fixture
async def client(clock: Clock) -> AsyncIterator[OvoClient]:
    c = make_ovo_client(clock)
    try:
        yield c
    finally:
        await c.aclose()

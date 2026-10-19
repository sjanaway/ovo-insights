"""Everything that can go wrong talking to OVO, as distinct types.

Callers decide what to do by type: only `UpstreamUnavailable` is retried automatically.
`BlockedByUpstream` and `AuthFailed` must stop all further requests (the M5 circuit breaker),
so the app never hammers OVO when OVO has said no.
"""


class OvoError(Exception):
    """Base class. Messages never contain credentials, tokens or response bodies."""


class AuthFailed(OvoError):
    """Login was refused (bad credentials, or OVO's login flow changed). Never retried."""


class Unauthorized(OvoError):
    """A data request was refused even after refreshing the token and logging in again."""


class RateLimited(OvoError):
    """OVO returned 429."""

    def __init__(self, message: str, retry_after: float | None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class BlockedByUpstream(OvoError):
    """A Cloudflare challenge or a 403 from OVO's edge. Never retried; stops syncing."""


class UpstreamSchemaChanged(OvoError):
    """The response didn't match what we expect. OVO probably changed its API."""


class UpstreamUnavailable(OvoError):
    """A 5xx, timeout or connection error. Retried with backoff."""


class DisallowedHostError(OvoError):
    """A request (or redirect) targeted a host outside the allowlist."""


class ResponseTooLarge(OvoError):
    """A response exceeded the size cap."""

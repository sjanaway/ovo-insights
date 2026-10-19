"""Credentials and tokens, held in memory only and masked in every repr."""

import base64
import binascii
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ovo_insights.ovo.errors import AuthFailed

# Refresh this long before the access token's stated expiry, or a quarter of the token's
# lifetime if that is shorter. OVO's access tokens appear to be short-lived: with a flat
# 60 s margin every token was "due" on arrival, so every data request cost two requests.
REFRESH_MARGIN = timedelta(seconds=60)


class Secret:
    """A string that never shows itself in repr, str, logs or tracebacks."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise ValueError("Secret must be a non-empty string")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(("Secret", self._value))

    def __reduce__(self) -> tuple[object, ...]:
        raise TypeError("Secrets cannot be pickled")


@dataclass(frozen=True, slots=True)
class Credentials:
    email: str
    password: Secret

    def __post_init__(self) -> None:
        if not isinstance(self.email, str) or "@" not in self.email or len(self.email) > 254:
            raise ValueError("A valid email address is required")
        if not isinstance(self.password, Secret):
            raise TypeError("password must be a Secret")


@dataclass(frozen=True, slots=True)
class AccessToken:
    value: Secret
    expires_at: datetime
    subject: str | None
    account_ids: tuple[str, ...]
    refresh_margin: timedelta = REFRESH_MARGIN

    def needs_refresh(self, now: datetime) -> bool:
        return now >= self.expires_at - self.refresh_margin


@dataclass(slots=True)
class TokenState:
    """The live session. Cookies from login are sent only to the token endpoint."""

    login_cookies: dict[str, Secret] = field(default_factory=dict, repr=False)
    customer_id: str | None = None
    access: AccessToken | None = field(default=None, repr=False)

    def clear(self) -> None:
        self.login_cookies = {}
        self.customer_id = None
        self.access = None


def decode_access_token(jwt: str, now: datetime | None = None) -> AccessToken:
    """Read expiry, subject and account permissions from a JWT's payload.

    Given `now` (when the token was received), the refresh margin is limited to a quarter
    of the token's remaining lifetime, so short-lived tokens are still reused.

    The signature is not verified: we are the bearer, not the verifier, and nothing here is
    trusted for security. It only schedules refreshes and offers fallback account ids.
    """
    parts = jwt.split(".")
    if len(parts) != 3 or len(jwt) > 16_384:
        raise AuthFailed("Access token is not a JWT")
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (binascii.Error, UnicodeError, ValueError) as exc:
        raise AuthFailed("Access token payload is unreadable") from exc
    if not isinstance(payload, dict):
        raise AuthFailed("Access token payload is not an object")
    exp = payload.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, int | float):
        raise AuthFailed("Access token has no expiry")
    try:
        expires_at = datetime.fromtimestamp(exp, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise AuthFailed("Access token expiry is out of range") from exc
    subject = payload.get("sub") if isinstance(payload.get("sub"), str) else None
    permissions = payload.get("permissions")
    account_ids: list[str] = []
    if isinstance(permissions, list):
        for permission in permissions:
            if isinstance(permission, str) and permission.startswith("account::"):
                candidate = permission.removeprefix("account::")
                if candidate.isascii() and candidate.isdigit():
                    account_ids.append(candidate)
    margin = REFRESH_MARGIN
    if now is not None:
        margin = max(timedelta(0), min(REFRESH_MARGIN, (expires_at - now) / 4))
    return AccessToken(Secret(jwt), expires_at, subject, tuple(account_ids), margin)

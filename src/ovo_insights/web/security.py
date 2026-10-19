"""The security shell around every request (pure ASGI, so nothing can bypass it).

In order, for every HTTP request:
  1. Host header must be exactly `127.0.0.1:PORT` or `localhost:PORT` (DNS-rebinding
     defence), else 400.
  2. `/?token=…` with the per-launch token is exchanged, once, for a session cookie
     (`HttpOnly; SameSite=Strict; Path=/`) and a redirect that drops the token from the URL.
  3. Every other request needs that session cookie, else 403.
  4. State-changing requests (anything but GET/HEAD) need a same-origin `Origin` (or, without
     one, `Sec-Fetch-Site: same-origin`) and the session's CSRF token in `X-CSRF-Token`,
     else 403.
  5. Every response, including the refusals above, carries the security headers.
"""

import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qs

from starlette.types import ASGIApp, Message, Receive, Scope, Send

SESSION_COOKIE = "ovo_session"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD"})

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
    (b"cache-control", b"no-store"),
)


@dataclass
class Sessions:
    """The launch token and the one session it can be exchanged for (in memory only).

    The token works once, so there is at most one session per launch.
    """

    launch_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    _session: tuple[str, str] | None = field(default=None, repr=False)  # (id, CSRF token)

    @property
    def launch_token_used(self) -> bool:
        return self._session is not None

    def exchange(self, token: str) -> str | None:
        """A new session id for a valid, unused launch token; None otherwise."""
        if self.launch_token_used or not secrets.compare_digest(
            token.encode(), self.launch_token.encode()
        ):
            return None
        session_id = secrets.token_urlsafe(32)
        self._session = (session_id, secrets.token_urlsafe(32))
        return session_id

    def csrf_token(self, session_id: str | None) -> str | None:
        """The session's CSRF token if `session_id` is the live session."""
        if not session_id or self._session is None:
            return None
        known, csrf = self._session
        return csrf if secrets.compare_digest(known.encode(), session_id.encode()) else None


class SecurityMiddleware:
    def __init__(self, app: ASGIApp, *, port: int, sessions: Sessions) -> None:
        self._app = app
        self._sessions = sessions
        self._hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self._origins = {f"http://{host}" for host in self._hosts}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope["type"] != "http":
            return  # no websockets: close without accepting

        send = _with_security_headers(send)
        headers = _headers(scope["headers"])
        if headers.get("host") not in self._hosts:
            await _plain(send, 400, "Bad Host header.")
            return

        method = scope["method"]
        if scope["path"] == "/" and "token" in parse_qs(scope["query_string"].decode("latin-1")):
            await self._exchange(scope, send)
            return

        session_id = _cookie(headers.get("cookie"), SESSION_COOKIE)
        csrf = self._sessions.csrf_token(session_id)
        if csrf is None:
            await _plain(
                send,
                403,
                "Not signed in. Open the link printed by `ovo-insights serve` "
                "(each link works once; restart the server for a new one).",
            )
            return

        if method not in SAFE_METHODS:
            origin = headers.get("origin")
            if origin is not None:
                same_origin = origin in self._origins
            else:
                same_origin = headers.get("sec-fetch-site") == "same-origin"
            token = headers.get(CSRF_HEADER, "")
            if not same_origin or not secrets.compare_digest(token.encode(), csrf.encode()):
                await _plain(send, 403, "Request refused (cross-site or missing CSRF token).")
                return

        scope.setdefault("state", {})["csrf_token"] = csrf
        await self._app(scope, receive, send)

    async def _exchange(self, scope: Scope, send: Send) -> None:
        tokens = parse_qs(scope["query_string"].decode("latin-1")).get("token", [""])
        session_id = self._sessions.exchange(tokens[0]) if len(tokens) == 1 else None
        if session_id is None:
            await _plain(
                send,
                403,
                "This link has expired or was already used. Restart `ovo-insights serve` "
                "for a new one.",
            )
            return
        cookie = f"{SESSION_COOKIE}={session_id}; HttpOnly; SameSite=Strict; Path=/"
        await send(
            {
                "type": "http.response.start",
                "status": 303,
                "headers": [(b"location", b"/"), (b"set-cookie", cookie.encode())],
            }
        )
        await send({"type": "http.response.body", "body": b""})


def _with_security_headers(send: Send) -> Send:
    names = {name for name, _ in SECURITY_HEADERS}

    async def wrapped(message: Message) -> None:
        if message["type"] == "http.response.start":
            kept = [(n, v) for n, v in message.get("headers", []) if n.lower() not in names]
            message = {**message, "headers": [*kept, *SECURITY_HEADERS]}
        await send(message)

    return wrapped


def _headers(raw: Iterable[tuple[bytes, bytes]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in raw:
        key = name.decode("latin-1").lower()
        if key in result:
            # A repeated Host, Origin or CSRF header is never legitimate; make it fail.
            result[key] = "\x00duplicate"
        else:
            result[key] = value.decode("latin-1")
    return result


def _cookie(header: str | None, name: str) -> str | None:
    if not header:
        return None
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(header)
    except CookieError:  # pragma: no cover - SimpleCookie drops malformed input silently
        return None
    morsel = jar.get(name)
    return morsel.value if morsel else None


async def _plain(send: Send, status: int, text: str) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": text.encode()})

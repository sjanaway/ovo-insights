"""Security smoke test against a real server on a real socket (the M12 scripted check).

Everything here goes over actual TCP to a running uvicorn, the way a browser or another
program on the machine would, not through the in-process test client.
"""

import logging
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from ovo_insights.credentials import MemoryCredentialStore
from ovo_insights.web.server import Prepared, prepare

EVIL_ORIGIN = "http://evil.example"


@pytest.fixture
def live(data_dir: Path) -> Iterator[Prepared]:
    prepared = prepare(data_dir, MemoryCredentialStore(), schedule=False, demo=True)
    thread = threading.Thread(target=prepared.server.run, kwargs={"sockets": [prepared.sock]})
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not prepared.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert prepared.server.started
        yield prepared
    finally:
        prepared.server.should_exit = True
        thread.join(10)
        prepared.log_handler.close()
        logging.getLogger().removeHandler(prepared.log_handler)


def client(live: Prepared, **kwargs: object) -> httpx.Client:
    return httpx.Client(base_url=f"http://127.0.0.1:{live.port}", trust_env=False, **kwargs)  # type: ignore[arg-type]


@contextmanager
def signed_in(live: Prepared) -> Iterator[tuple[httpx.Client, str]]:
    """A browser that has used the launch link (it works once per server), and the CSRF token
    its pages carry."""
    with client(live, follow_redirects=True) as browser:
        page = browser.get(live.url.removeprefix(f"http://127.0.0.1:{live.port}"))
        assert page.status_code == 200
        yield browser, page.text.split('"X-CSRF-Token": "')[1].split('"')[0]


def test_a_request_with_a_foreign_host_header_gets_400(live: Prepared) -> None:
    with client(live) as plain:
        for host in ("evil.example", f"evil.example:{live.port}", "127.0.0.1.evil.example", ""):
            assert plain.get("/", headers={"Host": host}).status_code == 400, host
        # Even with a valid session: the cookie doesn't make a rebinding host acceptable.
        with signed_in(live) as (browser, _):
            assert browser.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_the_session_cookie_is_http_only_and_same_site_strict(live: Prepared) -> None:
    with client(live) as plain:
        response = plain.get(live.url.removeprefix(f"http://127.0.0.1:{live.port}"))
    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert "Path=/" in cookie
    assert "Domain" not in cookie  # host-only: never shared with a sibling name
    assert live.url.split("token=")[1] not in response.headers["location"]


def test_a_page_on_another_origin_cannot_start_a_sync(live: Prepared) -> None:
    """What `fetch('http://127.0.0.1:PORT/sync', {method: 'POST'})` from another site can do:
    the browser sends its Origin, never sends our SameSite=Strict cookie, and cannot read the
    CSRF token. Even if every one of those protections failed except one, the request fails."""
    with signed_in(live) as (browser, csrf):
        stolen = next(iter(browser.cookies.jar))
        cookie = {"Cookie": f"{stolen.name}={stolen.value}"}
        with client(live) as cross_site:
            cases = {
                "no cookie, foreign origin": {"Origin": EVIL_ORIGIN},
                "foreign origin and a stolen cookie": {**cookie, "Origin": EVIL_ORIGIN},
                "stolen cookie, no origin, cross-site fetch": {
                    **cookie,
                    "Sec-Fetch-Site": "cross-site",
                },
                "stolen cookie and a guessed token": {
                    **cookie,
                    "Origin": EVIL_ORIGIN,
                    "X-CSRF-Token": "x" * 43,
                },
                "stolen cookie and the real token, foreign origin": {
                    **cookie,
                    "Origin": EVIL_ORIGIN,
                    "X-CSRF-Token": csrf,
                },
            }
            for name, headers in cases.items():
                response = cross_site.post("/sync", headers=headers)
                assert response.status_code == 403, name
        # The same request from the app's own origin, with its token, is accepted.
        ok = browser.post(
            "/sync",
            headers={"Origin": f"http://127.0.0.1:{live.port}", "X-CSRF-Token": csrf},
        )
        assert ok.status_code == 200


def test_no_cross_origin_reads_are_allowed(live: Prepared) -> None:
    with signed_in(live) as (browser, _), client(live) as plain:
        for session in (browser, plain):
            preflight = session.options(
                "/api/charts/usage?fuel=electricity",
                headers={"Origin": EVIL_ORIGIN, "Access-Control-Request-Method": "GET"},
            )
            assert preflight.status_code in (403, 405)
            read = session.get(
                "/api/charts/usage?fuel=electricity", headers={"Origin": EVIL_ORIGIN}
            )
            assert not any(h.lower().startswith("access-control-") for h in read.headers)


def test_the_listener_is_on_127_0_0_1_only(live: Prepared) -> None:
    assert live.sock.getsockname() == ("127.0.0.1", live.port)

    # Nothing answers on this machine's other addresses.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # a documentation address; nothing is sent
        local_address = probe.getsockname()[0]
    except OSError:
        local_address = ""
    finally:
        probe.close()
    if local_address and not local_address.startswith("127."):
        with (
            pytest.raises(ConnectionRefusedError),
            socket.create_connection((local_address, live.port), 2),
        ):
            pass
    else:
        pytest.skip("this machine has no address other than loopback")


def test_lsof_shows_one_listener_on_loopback(live: Prepared) -> None:
    lsof = shutil.which("lsof")
    if lsof is None:
        pytest.skip("lsof isn't installed")
    found = subprocess.run(
        [lsof, "-nP", f"-iTCP:{live.port}", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    listeners = [line for line in found.stdout.splitlines()[1:] if line.strip()]
    assert listeners, found.stderr
    for line in listeners:
        assert f"127.0.0.1:{live.port}" in line
        assert f"*:{live.port}" not in line

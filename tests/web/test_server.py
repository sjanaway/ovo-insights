"""The real server: loopback-only socket, uvicorn settings, and the `serve` command."""

import logging
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from ovo_insights import cli
from ovo_insights.cli import app
from ovo_insights.credentials import MemoryCredentialStore
from ovo_insights.logs import LOG_FILENAME, PrivateRotatingFileHandler, log_dir
from ovo_insights.paths import DATA_DIR_ENV
from ovo_insights.web import server
from ovo_insights.web.server import Prepared, bind_loopback, prepare, run


@pytest.fixture
def prepared(data_dir: Path) -> Iterator[Prepared]:
    p = prepare(data_dir, MemoryCredentialStore(), schedule=False)
    try:
        yield p
    finally:
        p.sock.close()
        p.log_handler.close()
        logging.getLogger().removeHandler(p.log_handler)


def test_bind_loopback_listens_on_127_0_0_1_only() -> None:
    sock = bind_loopback(0)
    try:
        host, port = sock.getsockname()
        assert host == "127.0.0.1"
        assert port > 0
    finally:
        sock.close()
    with pytest.raises(ValueError, match="Invalid port"):
        bind_loopback(70000)


def test_bind_loopback_closes_the_socket_if_the_port_is_taken() -> None:
    taken = bind_loopback(0)
    try:
        with pytest.raises(OSError, match=r"(?i)address"):  # "Address already in use"
            bind_loopback(taken.getsockname()[1])
    finally:
        taken.close()


def test_prepare_never_logs_requests_or_announces_itself(prepared: Prepared) -> None:
    assert logging.getLogger("uvicorn.access").disabled
    config = prepared.server.config
    assert config.access_log is False
    assert config.log_config is None
    assert config.server_header is False
    assert config.proxy_headers is False
    assert prepared.url == (
        f"http://127.0.0.1:{prepared.port}/?token={prepared.url.split('token=')[1]}"
    )


def test_real_server_end_to_end(
    prepared: Prepared, data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    thread = threading.Thread(target=prepared.server.run, kwargs={"sockets": [prepared.sock]})
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not prepared.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert prepared.server.started
        base = f"http://127.0.0.1:{prepared.port}"
        with httpx.Client(base_url=base, trust_env=False, follow_redirects=True) as browser:
            signed_in = browser.get(prepared.url.removeprefix(base))
            assert signed_in.status_code == 200  # followed the redirect, with the cookie
            assert "Dashboard" in signed_in.text
            assert "token=" not in str(signed_in.url)
            assert browser.get("/", headers={"Host": "evil.example"}).status_code == 400
            assert "server" not in signed_in.headers
    finally:
        prepared.server.should_exit = True
        thread.join(10)
    assert not thread.is_alive()

    token = prepared.url.split("token=")[1]
    # The test's own HTTP client logs the URLs it requests; everything else is the server.
    server_side = [
        r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))
    ]
    assert any("Application startup complete" in m for m in server_side)
    leaked = [
        (r.name, r.getMessage()[:120])
        for r in caplog.records
        if not r.name.startswith(("httpx", "httpcore")) and token in r.getMessage()
    ]
    assert leaked == []
    log_file = log_dir(data_dir) / LOG_FILENAME
    assert not log_file.exists() or token not in log_file.read_text()


def test_run_announces_opens_the_browser_and_closes_the_socket(prepared: Prepared) -> None:
    lines: list[str] = []
    opened: list[str] = []
    prepared.server.run = lambda sockets=None: None  # type: ignore[method-assign]

    run(prepared, open_browser=True, announce=lines.append, browser=opened.append)

    assert opened == [prepared.url]
    assert lines[1] == f"Open this link to sign in (it works once): {prepared.url}"
    assert prepared.sock.fileno() == -1


def test_run_without_opening_the_browser(prepared: Prepared) -> None:
    opened: list[str] = []
    prepared.server.run = lambda sockets=None: None  # type: ignore[method-assign]

    run(prepared, open_browser=False, announce=lambda line: None, browser=opened.append)

    assert opened == []


async def test_verify_login_uses_the_ovo_client(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class FakeClient:
        def __init__(self, credentials: object) -> None:
            calls.append("init")

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *exc: object) -> None:
            calls.append("closed")

        async def login(self) -> None:
            calls.append("login")

    monkeypatch.setattr(server, "OvoClient", FakeClient)

    await server.verify_login(object())  # type: ignore[arg-type]

    assert calls == ["init", "login", "closed"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


pytestmark = pytest.mark.anyio


# -- CLI ----------------------------------------------------------------------------------


def test_serve_command_wires_everything(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path / "data"))
    store = MemoryCredentialStore()
    monkeypatch.setattr(cli, "_store", lambda: store)
    seen: dict[str, object] = {}

    def fake_prepare(data_dir: Path, credential_store: object, *, port: int) -> str:
        seen.update(data_dir=data_dir, store=credential_store, port=port)
        return "prepared"

    def fake_run(p: object, *, open_browser: bool, announce: object) -> None:
        seen.update(prepared=p, open_browser=open_browser)

    monkeypatch.setattr(server, "prepare", fake_prepare)
    monkeypatch.setattr(server, "run", fake_run)

    result = CliRunner().invoke(app, ["serve", "--port", "8123", "--no-open"])

    assert result.exit_code == 0, result.output
    assert seen == {
        "data_dir": tmp_path / "data",
        "store": store,
        "port": 8123,
        "prepared": "prepared",
        "open_browser": False,
    }


def test_serve_reports_a_busy_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path / "data"))
    monkeypatch.setattr(cli, "_store", MemoryCredentialStore)
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    port = taken.getsockname()[1]
    try:
        result = CliRunner().invoke(app, ["serve", "--port", str(port), "--no-open"])
    finally:
        taken.close()
        for handler in list(logging.getLogger().handlers):
            if isinstance(handler, PrivateRotatingFileHandler):
                handler.close()
                logging.getLogger().removeHandler(handler)

    assert result.exit_code == 1
    assert f"Could not listen on port {port}" in result.output


def test_serve_refuses_an_insecure_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    data.chmod(0o755)
    monkeypatch.setenv(DATA_DIR_ENV, str(data))
    monkeypatch.setattr(cli, "_store", MemoryCredentialStore)

    result = CliRunner().invoke(app, ["serve", "--no-open"])

    assert result.exit_code == 1
    assert "chmod 700" in result.output


def test_serve_has_no_host_option() -> None:
    result = CliRunner().invoke(app, ["serve", "--host", "0.0.0.0", "--no-open"])  # noqa: S104

    assert result.exit_code == 2
    assert "No such option" in result.output

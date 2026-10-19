"""`ovo-insights serve`: run the web UI on 127.0.0.1 only.

The listening socket is bound here, to the loopback address, before uvicorn sees it. There
is no host option anywhere, so the server can't be exposed to the network by a flag or a
config file. The port is chosen by the OS unless one is given; the per-launch link is what
the user opens, so a fixed port would buy nothing.

The launch link is printed to the terminal and opened in the browser. It is never logged:
uvicorn's access log, which would record it, is switched off.
"""

import logging
import socket
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import uvicorn

from ovo_insights.credentials import CredentialStore
from ovo_insights.logs import PrivateRotatingFileHandler, add_file_logging
from ovo_insights.ovo.auth import Credentials
from ovo_insights.ovo.client import OvoClient
from ovo_insights.storage import open_database
from ovo_insights.web.app import WebContext, create_app
from ovo_insights.web.scheduler import DailyScheduler
from ovo_insights.web.security import Sessions
from ovo_insights.web.sync_service import SyncService

LOOPBACK = "127.0.0.1"


def bind_loopback(port: int) -> socket.socket:
    """A listening TCP socket on 127.0.0.1 (port 0 = any free port)."""
    if not 0 <= port <= 65535:
        raise ValueError(f"Invalid port: {port}")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((LOOPBACK, port))
        sock.listen(64)
    except BaseException:
        sock.close()
        raise
    return sock


async def verify_login(credentials: Credentials) -> None:
    async with OvoClient(credentials) as client:
        await client.login()


@dataclass
class Prepared:
    sock: socket.socket
    port: int
    url: str
    server: uvicorn.Server
    log_handler: PrivateRotatingFileHandler


def prepare(
    data_dir: Path,
    store: CredentialStore,
    *,
    port: int = 0,
    schedule: bool = True,
    demo: bool = False,
) -> Prepared:
    open_database(data_dir).close()  # create and migrate before serving, failing early
    sock = bind_loopback(port)
    actual_port = sock.getsockname()[1]
    sessions = Sessions()
    log_handler = add_file_logging(data_dir)
    service = SyncService(data_dir, load_credentials=store.load)
    scheduler = DailyScheduler(service, data_dir) if schedule else None
    ctx = WebContext(
        data_dir=data_dir,
        port=actual_port,
        store=store,
        sync=service,
        verify_login=verify_login,
        sessions=sessions,
        log_handler=log_handler,
        scheduler=scheduler.run if scheduler else None,
        demo=demo,
    )
    config = uvicorn.Config(
        create_app(ctx),
        log_config=None,  # use our (redacting) root handlers
        access_log=False,  # would log the launch token
        server_header=False,
        date_header=False,
        proxy_headers=False,
        lifespan="on",
        ws="none",
    )
    # Belt and braces: the access log line would contain the launch token. `access_log=False`
    # stops uvicorn adding handlers, but anything else that attached one (pytest does) could
    # still receive it, so the logger itself is switched off.
    logging.getLogger("uvicorn.access").disabled = True
    url = f"http://{LOOPBACK}:{actual_port}/?token={sessions.launch_token}"
    return Prepared(sock, actual_port, url, uvicorn.Server(config), log_handler)


def run(
    prepared: Prepared,
    *,
    open_browser: bool,
    announce: Callable[[str], None],
    browser: Callable[[str], object] = webbrowser.open,
) -> None:
    announce(f"ovo-insights is running at http://{LOOPBACK}:{prepared.port}/")
    announce(f"Open this link to sign in (it works once): {prepared.url}")
    announce("Press Ctrl-C to stop.")
    if open_browser:
        browser(prepared.url)
    try:
        prepared.server.run(sockets=[prepared.sock])
    finally:
        prepared.sock.close()

"""The local log file: `<data dir>/logs/ovo-insights.log`, rotated 5 x 1 MB, mode 0600.

Every handler gets the redaction filter, so tokens, passwords and emails never reach the
file. There is deliberately no way to dump raw OVO responses into the log.
"""

import io
import logging
import logging.handlers
import os
from pathlib import Path

from ovo_insights.ovo.redact import install_redaction
from ovo_insights.paths import ensure_private_dir

LOG_DIRNAME = "logs"
LOG_FILENAME = "ovo-insights.log"
MAX_BYTES = 1_000_000
BACKUP_COUNT = 5
# HTTP libraries log every request URL at INFO/DEBUG. OVO's URLs contain account numbers,
# and the server's contain the launch token, so these never log below WARNING.
QUIET_LOGGERS = ("httpx", "httpcore")


def quiet_http_logs() -> None:
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


class PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Creates every log file (including rotated ones) as 0600 and never follows symlinks."""

    def _open(self) -> io.TextIOWrapper:
        fd = os.open(
            self.baseFilename,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(fd, 0o600)  # also tightens a file that already existed
        stream = os.fdopen(fd, "a", encoding=self.encoding or "utf-8")
        if not isinstance(stream, io.TextIOWrapper):  # pragma: no cover - text mode always is
            raise TypeError("expected a text stream")
        return stream

    def reset(self) -> None:
        """Close the current file; the next record reopens (recreates) it."""
        self.acquire()
        try:
            if self.stream is not None:
                self.stream.close()
                self.stream = None
        finally:
            self.release()


def log_dir(data_dir: Path) -> Path:
    return data_dir / LOG_DIRNAME


def add_file_logging(data_dir: Path) -> PrivateRotatingFileHandler:
    """Attach the private log file to the root logger (redacted) and return its handler."""
    directory = ensure_private_dir(log_dir(data_dir))
    handler = PrivateRotatingFileHandler(
        directory / LOG_FILENAME,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
        delay=True,
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    install_redaction(root)
    quiet_http_logs()
    return handler

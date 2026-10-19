import hashlib
import logging
import os
import re
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from ovo_insights import logs
from ovo_insights.logs import LOG_FILENAME, PrivateRotatingFileHandler, add_file_logging, log_dir

WEB = Path(__file__).resolve().parents[2] / "src" / "ovo_insights" / "web"
VENDOR = WEB / "static" / "vendor"


@pytest.fixture
def handler(data_dir: Path) -> Iterator[PrivateRotatingFileHandler]:
    h = add_file_logging(data_dir)
    try:
        yield h
    finally:
        h.close()
        logging.getLogger().removeHandler(h)


def mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_log_file_is_private_and_redacted(
    data_dir: Path, handler: PrivateRotatingFileHandler
) -> None:
    jwt = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ4In0.sig"
    logging.getLogger("ovo_insights.test").warning("user someone@example.com token %s", jwt)

    path = log_dir(data_dir) / LOG_FILENAME
    text = path.read_text()
    assert "[EMAIL]" in text
    assert "[JWT]" in text
    assert "someone@example.com" not in text
    assert jwt not in text
    assert mode(log_dir(data_dir)) == 0o700
    assert mode(path) == 0o600


def test_http_libraries_never_log_request_urls(
    data_dir: Path, handler: PrivateRotatingFileHandler, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    for name in logs.QUIET_LOGGERS:
        logging.getLogger(name).info("HTTP Request: GET https://example.invalid/account/123")

    assert "account/123" not in caplog.text
    assert not (log_dir(data_dir) / LOG_FILENAME).exists()


def test_rotated_files_are_private_too(
    data_dir: Path, handler: PrivateRotatingFileHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler.maxBytes = 200
    for i in range(20):
        logging.getLogger("ovo_insights.test").warning("line %d %s", i, "x" * 40)

    files = sorted(log_dir(data_dir).iterdir())
    assert len(files) > 1
    assert {mode(f) for f in files} == {0o600}
    assert len(files) <= logs.BACKUP_COUNT + 1


def test_an_existing_loose_log_file_is_tightened(data_dir: Path) -> None:
    directory = log_dir(data_dir)
    directory.mkdir(parents=True, mode=0o700)
    loose = directory / LOG_FILENAME
    loose.write_text("old\n")
    loose.chmod(0o644)

    h = add_file_logging(data_dir)
    try:
        logging.getLogger("ovo_insights.test").warning("new")
    finally:
        h.close()
        logging.getLogger().removeHandler(h)

    assert mode(loose) == 0o600


def test_reset_reopens_on_the_next_record(
    data_dir: Path, handler: PrivateRotatingFileHandler
) -> None:
    logging.getLogger("ovo_insights.test").warning("before")
    path = log_dir(data_dir) / LOG_FILENAME
    path.unlink()

    handler.reset()
    handler.reset()  # harmless when already closed
    logging.getLogger("ovo_insights.test").warning("after")

    assert path.read_text().strip().endswith("after")


# -- vendored files -----------------------------------------------------------------------


def manifest() -> list[tuple[str, str, str]]:
    entries = []
    for line in (VENDOR / "MANIFEST.txt").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            name, version, digest, _source = line.split()
            entries.append((name, version, digest))
    return entries


def test_vendored_files_match_the_manifest() -> None:
    entries = manifest()
    assert entries
    for name, _version, digest in entries:
        assert hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() == digest, name


def test_every_vendored_file_is_listed() -> None:
    listed = {name for name, _, _ in manifest()}
    present = {p.name for p in VENDOR.iterdir()} - {"MANIFEST.txt"}

    assert {n for n in present if not n.startswith(("LICENSE", "NOTICE"))} == listed
    # Every vendored library ships with its licence (and Apache's NOTICE).
    assert {"LICENSE-htmx.txt", "LICENSE-echarts.txt", "NOTICE-echarts.txt"} <= present


def test_pages_load_exactly_the_vendored_htmx() -> None:
    base = (WEB / "templates" / "base.html").read_text()
    scripts = re.findall(r'<script src="/static/vendor/([^"]+)"', base, re.ASCII)

    assert scripts == [name for name, _, _ in manifest() if name.startswith("htmx-")]

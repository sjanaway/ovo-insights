import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import keyring
import keyring.backend
import pytest
from hypothesis import HealthCheck, settings

from ovo_insights.storage import open_database
from ovo_insights.storage.repository import Repository

settings.register_profile("ci", max_examples=300, deadline=None)
settings.register_profile(
    "default", max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
# CI sets HYPOTHESIS_PROFILE=ci for a deeper search than local runs.
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def conn(data_dir: Path) -> Iterator[sqlite3.Connection]:
    connection = open_database(data_dir)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def repo(conn: sqlite3.Connection) -> Repository:
    return Repository(conn)


class _ForbiddenKeyring(keyring.backend.KeyringBackend):
    """Installed for every test: touching the real OS keychain from a test is a bug."""

    priority = 1

    def __init__(self) -> None:  # keyring's own __init__ is untyped and reads the env
        pass

    def get_password(self, service: str, username: str) -> str | None:
        raise AssertionError("A test tried to read the real keychain")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise AssertionError("A test tried to write the real keychain")

    def delete_password(self, service: str, username: str) -> None:
        raise AssertionError("A test tried to delete from the real keychain")


@pytest.fixture(autouse=True)
def _no_real_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keyring, "get_keyring", lambda: _ForbiddenKeyring())

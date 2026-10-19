import json
import logging
from collections.abc import Iterator

import keyring
import keyring.backend
import keyring.errors
import pytest
import respx
from typer.testing import CliRunner

from ovo_insights import cli, credentials
from ovo_insights.cli import app
from ovo_insights.credentials import (
    ACCOUNT,
    SERVICE,
    CredentialError,
    InsecureKeyringError,
    KeyringCredentialStore,
    MemoryCredentialStore,
    backend_name,
    secure_backend,
)
from ovo_insights.ovo.auth import Credentials, Secret
from tests.ovo.conftest import mock_login

PASSWORD = "sentinel-password-for-tests"
CREDS = Credentials("someone@example.com", Secret(PASSWORD))


class FakeSecureKeyring(keyring.backend.KeyringBackend):
    priority = 1

    def __init__(self) -> None:  # keyring's own __init__ is untyped and reads the env
        self.items: dict[tuple[str, str], str] = {}
        self.fail: type[Exception] | None = None

    def _maybe_fail(self) -> None:
        if self.fail is not None:
            raise self.fail("boom")

    def get_password(self, service: str, username: str) -> str | None:
        self._maybe_fail()
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._maybe_fail()
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._maybe_fail()
        if (service, username) not in self.items:
            raise keyring.errors.PasswordDeleteError("missing")
        del self.items[(service, username)]


class PlaintextKeyring(FakeSecureKeyring):
    """Stands in for keyrings.alt's plaintext file backend."""


class Chainer(keyring.backend.KeyringBackend):
    priority = 1

    def __init__(self, backends: list[keyring.backend.KeyringBackend]) -> None:
        self.backends = backends

    def get_password(self, service: str, username: str) -> str | None:
        return None

    def set_password(self, service: str, username: str, password: str) -> None:
        raise NotImplementedError

    def delete_password(self, service: str, username: str) -> None:
        raise NotImplementedError


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> FakeSecureKeyring:
    fake = FakeSecureKeyring()
    monkeypatch.setattr(
        credentials, "SECURE_BACKENDS", credentials.SECURE_BACKENDS | {backend_name(fake)}
    )
    monkeypatch.setattr(keyring, "get_keyring", lambda: fake)
    return fake


# -- backend allowlist --------------------------------------------------------------------


def test_macos_keychain_is_on_the_allowlist() -> None:
    assert "keyring.backends.macOS.Keyring" in credentials.SECURE_BACKENDS


def test_unknown_or_plaintext_backends_are_refused() -> None:
    with pytest.raises(InsecureKeyringError, match="PlaintextKeyring"):
        secure_backend(PlaintextKeyring())


def test_chainer_is_secure_only_if_every_member_is(backend: FakeSecureKeyring) -> None:
    assert secure_backend(Chainer([backend])) is not None
    with pytest.raises(InsecureKeyringError, match="PlaintextKeyring"):
        secure_backend(Chainer([backend, PlaintextKeyring()]))
    with pytest.raises(InsecureKeyringError, match="no backends"):
        secure_backend(Chainer([]))


def test_store_refuses_insecure_backend() -> None:
    store = KeyringCredentialStore(PlaintextKeyring())

    with pytest.raises(InsecureKeyringError):
        store.save(CREDS)
    with pytest.raises(InsecureKeyringError):
        store.load()


# -- store ---------------------------------------------------------------------------------


def test_round_trip_in_one_item(backend: FakeSecureKeyring) -> None:
    store = KeyringCredentialStore()

    assert store.load() is None
    store.save(CREDS)

    assert list(backend.items) == [(SERVICE, ACCOUNT)]
    assert store.load() == CREDS
    assert store.forget() is True
    assert store.forget() is False
    assert store.load() is None


@pytest.mark.parametrize(
    "stored",
    [
        "not json",
        "[]",
        json.dumps({"version": 2, "email": "a@b.co", "password": "x"}),
        json.dumps({"version": 1, "email": "a@b.co"}),
        json.dumps({"version": 1, "email": "no-at", "password": "x"}),
        json.dumps({"version": 1, "email": "a@b.co", "password": ""}),
        json.dumps({"version": 1, "email": "a@b.co", "password": "x" * 5000}),
    ],
)
def test_corrupt_items_are_refused_with_a_fix(backend: FakeSecureKeyring, stored: str) -> None:
    backend.items[(SERVICE, ACCOUNT)] = stored

    with pytest.raises(CredentialError, match="logout") as exc_info:
        KeyringCredentialStore().load()
    assert "x" * 50 not in str(exc_info.value)


@pytest.mark.parametrize("method", ["load", "save", "forget"])
def test_keyring_failures_are_reported_without_detail(
    backend: FakeSecureKeyring, method: str
) -> None:
    backend.fail = keyring.errors.KeyringLocked
    store = KeyringCredentialStore()
    calls = {"load": store.load, "save": lambda: store.save(CREDS), "forget": store.forget}

    with pytest.raises(CredentialError, match="KeyringLocked"):
        calls[method]()


def test_memory_store() -> None:
    store = MemoryCredentialStore()

    assert store.load() is None
    store.save(CREDS)
    assert store.load() == CREDS
    assert store.forget() is True
    assert store.forget() is False


# -- CLI ----------------------------------------------------------------------------------


@pytest.fixture
def fast_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ovo_insights.ovo.client.DEFAULT_MIN_INTERVAL", 0)


def test_login_verifies_then_saves(
    backend: FakeSecureKeyring,
    mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    fast_client: None,
) -> None:
    mock_login(mock)
    monkeypatch.setattr("getpass.getpass", lambda prompt: PASSWORD)

    result = CliRunner().invoke(app, ["login"], input="someone@example.com\n")

    assert result.exit_code == 0, result.output
    assert "Saved" in result.output
    assert KeyringCredentialStore().load() == CREDS
    assert mock.routes[0].call_count == 1


def test_login_with_wrong_password_saves_nothing(
    backend: FakeSecureKeyring,
    mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    fast_client: None,
) -> None:
    mock.post("https://my.ovoenergy.com/api/v2/auth/login").respond(401)
    monkeypatch.setattr("getpass.getpass", lambda prompt: PASSWORD)

    result = CliRunner().invoke(app, ["login"], input="someone@example.com\n")

    assert result.exit_code == 1
    assert "Login not saved" in result.output
    assert backend.items == {}


def test_login_no_verify_skips_network(
    backend: FakeSecureKeyring, mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("getpass.getpass", lambda prompt: PASSWORD)

    result = CliRunner().invoke(app, ["login", "--no-verify"], input="someone@example.com\n")

    assert result.exit_code == 0
    assert not mock.calls
    assert KeyringCredentialStore().load() == CREDS


def test_login_refuses_without_secure_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keyring, "get_keyring", lambda: PlaintextKeyring())
    prompted: list[str] = []

    def fake_getpass(prompt: str) -> str:
        prompted.append(prompt)
        return "x"

    monkeypatch.setattr("getpass.getpass", fake_getpass)

    result = CliRunner().invoke(app, ["login"])

    assert result.exit_code == 1
    assert "No secure keychain" in result.output
    assert prompted == []  # refused before asking for the password


def test_login_reports_save_failure(
    backend: FakeSecureKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("getpass.getpass", lambda prompt: PASSWORD)
    backend.fail = keyring.errors.PasswordSetError

    result = CliRunner().invoke(app, ["login", "--no-verify"], input="someone@example.com\n")

    assert result.exit_code == 1
    assert "PasswordSetError" in result.output


def test_logout(backend: FakeSecureKeyring) -> None:
    KeyringCredentialStore().save(CREDS)

    first = CliRunner().invoke(app, ["logout"])
    second = CliRunner().invoke(app, ["logout"])

    assert "Deleted" in first.output
    assert "No saved OVO login" in second.output
    assert first.exit_code == second.exit_code == 0


def test_logout_reports_keychain_failure(backend: FakeSecureKeyring) -> None:
    backend.fail = keyring.errors.KeyringLocked

    result = CliRunner().invoke(app, ["logout"])

    assert result.exit_code == 1


def test_saved_login_is_used_without_prompting(
    backend: FakeSecureKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    KeyringCredentialStore().save(CREDS)
    monkeypatch.setattr("getpass.getpass", lambda prompt: pytest.fail("should not prompt"))

    assert cli._credentials_for_run(prompt=False) == CREDS


def test_prompt_flag_ignores_saved_login(
    backend: FakeSecureKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    KeyringCredentialStore().save(CREDS)
    monkeypatch.setattr("getpass.getpass", lambda prompt: "other-password")
    monkeypatch.setattr("typer.prompt", lambda text: "other@example.com")

    assert cli._credentials_for_run(prompt=True).email == "other@example.com"


def test_unreadable_saved_login_falls_back_to_prompt(
    backend: FakeSecureKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend.items[(SERVICE, ACCOUNT)] = "garbage"
    monkeypatch.setattr("getpass.getpass", lambda prompt: PASSWORD)
    monkeypatch.setattr("typer.prompt", lambda text: "someone@example.com")

    assert cli._credentials_for_run(prompt=False) == CREDS


def test_password_only_ever_lands_in_the_keychain(
    backend: FakeSecureKeyring,
    mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fast_client: None,
) -> None:
    """Capture-everything check across login, doctor and logout."""
    from tests.ovo.test_fixtures_and_doctor import _mock_everything

    _mock_everything(mock)
    monkeypatch.setattr("getpass.getpass", lambda prompt: PASSWORD)
    runner = CliRunner()

    with caplog.at_level(logging.DEBUG):
        outputs = [
            runner.invoke(app, ["-v", "login"], input="someone@example.com\n").output,
            runner.invoke(app, ["-v", "doctor"]).output,
        ]
        stored = dict(backend.items)
        outputs.append(runner.invoke(app, ["-v", "logout"]).output)

    for text in [*outputs, caplog.text]:
        assert PASSWORD not in text
    assert any(PASSWORD in value for value in stored.values())  # it is in the keychain
    assert backend.items == {}  # and logout removed it

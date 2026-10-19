"""Where the OVO login lives: the OS keychain, and nowhere else.

One keychain item (service `ovo-insights`, account `ovo-login`) holds the email and password
as JSON, so loading and forgetting are each a single atomic operation. The email is stored
in the secret too, so nothing about the login is written to disk outside the keychain.

Only backends known to encrypt at rest are accepted. On a system without one (a headless
Linux box, or a plaintext backend from `keyrings.alt`), storing is refused and the caller
falls back to asking for the password each time.
"""

import json
from typing import Protocol

import keyring
import keyring.backend
import keyring.errors

from ovo_insights.ovo.auth import Credentials, Secret

SERVICE = "ovo-insights"
ACCOUNT = "ovo-login"

# Fully qualified class names of backends that store secrets encrypted by the OS.
SECURE_BACKENDS = frozenset(
    {
        "keyring.backends.macOS.Keyring",
        "keyring.backends.Windows.WinVaultKeyring",
        "keyring.backends.SecretService.Keyring",
        "keyring.backends.libsecret.Keyring",
        "keyring.backends.kwallet.DBusKeyring",
    }
)
_MAX_STORED_LENGTH = 4096


class CredentialError(RuntimeError):
    """Credentials could not be stored or read safely."""


class InsecureKeyringError(CredentialError):
    """No keyring backend that encrypts at rest is available."""


class CredentialStore(Protocol):
    def load(self) -> Credentials | None: ...

    def save(self, credentials: Credentials) -> None: ...

    def forget(self) -> bool: ...


def backend_name(backend: keyring.backend.KeyringBackend) -> str:
    cls = type(backend)
    return f"{cls.__module__}.{cls.__qualname__}"


def secure_backend(
    backend: keyring.backend.KeyringBackend | None = None,
) -> keyring.backend.KeyringBackend:
    """The active keyring backend, or InsecureKeyringError if it isn't on the allowlist.

    A chainer backend counts as secure only if every backend it chains is secure, because
    keyring writes to the first one that accepts the item.
    """
    backend = backend or keyring.get_keyring()
    chained = getattr(backend, "backends", None)
    members = list(chained) if chained is not None else [backend]
    insecure = [backend_name(b) for b in members if backend_name(b) not in SECURE_BACKENDS]
    if not members or insecure:
        raise InsecureKeyringError(
            "No secure keychain is available "
            f"({', '.join(insecure) or 'no backends'}); "
            "your OVO password won't be saved and you'll be asked for it each time."
        )
    return backend


class KeyringCredentialStore:
    def __init__(self, backend: keyring.backend.KeyringBackend | None = None) -> None:
        self._backend = backend

    def _keyring(self) -> keyring.backend.KeyringBackend:
        return secure_backend(self._backend)

    def load(self) -> Credentials | None:
        try:
            stored = self._keyring().get_password(SERVICE, ACCOUNT)
        except keyring.errors.KeyringError as exc:
            raise CredentialError(
                f"Could not read from the keychain: {type(exc).__name__}"
            ) from None
        if stored is None:
            return None
        return _decode(stored)

    def save(self, credentials: Credentials) -> None:
        payload = json.dumps(
            {"version": 1, "email": credentials.email, "password": credentials.password.reveal()}
        )
        try:
            self._keyring().set_password(SERVICE, ACCOUNT, payload)
        except keyring.errors.KeyringError as exc:
            raise CredentialError(f"Could not save to the keychain: {type(exc).__name__}") from None

    def forget(self) -> bool:
        """Delete the stored login. Returns False if there was nothing to delete."""
        try:
            self._keyring().delete_password(SERVICE, ACCOUNT)
        except keyring.errors.PasswordDeleteError:
            return False
        except keyring.errors.KeyringError as exc:
            raise CredentialError(
                f"Could not remove the login from the keychain: {type(exc).__name__}"
            ) from None
        return True


class MemoryCredentialStore:
    """For tests, and for runs without a secure keychain (nothing outlives the process)."""

    def __init__(self, credentials: Credentials | None = None) -> None:
        self._credentials = credentials

    def load(self) -> Credentials | None:
        return self._credentials

    def save(self, credentials: Credentials) -> None:
        self._credentials = credentials

    def forget(self) -> bool:
        had = self._credentials is not None
        self._credentials = None
        return had


def _decode(stored: str) -> Credentials:
    problem = "The saved OVO login is unreadable; run `ovo-insights logout` then `login` again."
    if len(stored) > _MAX_STORED_LENGTH:
        raise CredentialError(problem)
    try:
        data = json.loads(stored)
    except ValueError:
        raise CredentialError(problem) from None
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(data.get("email"), str)
        or not isinstance(data.get("password"), str)
    ):
        raise CredentialError(problem)
    try:
        return Credentials(data["email"], Secret(data["password"]))
    except (ValueError, TypeError):
        raise CredentialError(problem) from None

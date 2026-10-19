"""`dev verify`: permissions, and the scan for secrets that must never be written down."""

import base64
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ovo_insights import cli, verify
from ovo_insights.cli import app
from ovo_insights.credentials import CredentialError, MemoryCredentialStore
from ovo_insights.ovo.auth import Credentials, Secret
from ovo_insights.ovo.redact import redact
from ovo_insights.paths import DATA_DIR_ENV

PASSWORD = "sentinel-password-for-the-verify-tests"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


# Built at run time, so no complete secret-shaped string sits in the source for a secret
# scanner to (rightly) object to.
JWT = ".".join(
    (
        _b64(b'{"alg":"RS256"}'),
        _b64(b'{"sub":"test","exp":1700000000}'),
        _b64(b"not-a-real-signature"),
    )
)
REFRESH = "A1" * 16
runner = CliRunner()


def private_tree(root: Path) -> Path:
    """A data folder as the app makes it: 0700 folders, 0600 files."""
    root.mkdir(mode=0o700, parents=True)
    root.chmod(0o700)
    (root / "logs").mkdir(mode=0o700)
    (root / "logs").chmod(0o700)
    for name in ("ovo-insights.sqlite3", "sync.lock", "logs/ovo-insights.log"):
        path = root / name
        path.write_bytes(b"nothing secret here")
        path.chmod(0o600)
    return root


# -- permissions --------------------------------------------------------------------------


def test_a_private_folder_passes_every_check(tmp_path: Path) -> None:
    checks = verify.check_permissions(private_tree(tmp_path / "data"))
    assert [c.name for c in checks] == [
        "data folder",
        "logs",
        "ovo-insights.sqlite3",
        "sync.lock",
        "logs/ovo-insights.log",
    ]  # sorted per folder, folders first as the walk meets them
    assert all(c.ok for c in checks)
    assert checks[0].detail == "mode 0700"


def test_tighter_than_required_is_fine(tmp_path: Path) -> None:
    data = private_tree(tmp_path / "data")
    (data / "sync.lock").chmod(0o400)
    assert all(c.ok for c in verify.check_permissions(data))


@pytest.mark.parametrize(
    ("what", "mode"),
    [("ovo-insights.sqlite3", 0o644), ("logs/ovo-insights.log", 0o640), ("logs", 0o755)],
)
def test_loose_modes_fail_and_say_which(tmp_path: Path, what: str, mode: int) -> None:
    data = private_tree(tmp_path / "data")
    (data / what).chmod(mode)
    failed = [c for c in verify.check_permissions(data) if not c.ok]
    assert [c.name for c in failed] == [what]
    assert f"mode {mode:04o}" in failed[0].detail


def test_the_data_folder_itself_must_be_exactly_0700(tmp_path: Path) -> None:
    data = private_tree(tmp_path / "data")
    data.chmod(0o750)
    [failed] = [c for c in verify.check_permissions(data) if not c.ok]
    assert failed.name == "data folder"
    assert "should be 0700" in failed.detail
    data.chmod(0o500)  # read-only is "tighter", but not what the app creates
    assert not verify.check_permissions(data)[0].ok
    data.chmod(0o700)


def test_symlinks_are_failures_and_never_followed(tmp_path: Path) -> None:
    data = private_tree(tmp_path / "data")
    outside = tmp_path / "elsewhere"
    outside.write_bytes(b"x")
    (data / "link").symlink_to(outside)
    [failed] = [c for c in verify.check_permissions(data) if not c.ok]
    assert (failed.name, failed.detail) == ("link", "is a symlink")


def test_a_missing_folder_is_reported_not_a_crash(tmp_path: Path) -> None:
    [check] = verify.check_permissions(tmp_path / "nope")
    assert not check.ok
    assert "run a sync first" in check.detail


def test_files_owned_by_someone_else_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = private_tree(tmp_path / "data")
    real = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real + 1)
    assert all("owned by someone else" in c.detail for c in verify.check_permissions(data))


# -- the leak scan ------------------------------------------------------------------------

NEEDLES = {"your password": PASSWORD.encode()}


def test_a_clean_tree_has_no_hits(tmp_path: Path) -> None:
    data = private_tree(tmp_path / "data")
    result = verify.scan_tree(data, NEEDLES, verify.TOKEN_PATTERNS)
    assert result.hits == []
    assert result.files_scanned == 3


def test_the_password_is_found_by_value_and_never_repeated(tmp_path: Path) -> None:
    data = private_tree(tmp_path / "data")
    (data / "logs" / "ovo-insights.log").write_text(f"login ok for pw={PASSWORD} done")
    result = verify.scan_tree(data, NEEDLES, verify.TOKEN_PATTERNS)
    assert [(h.path.name, h.what) for h in result.hits] == [("ovo-insights.log", "your password")]
    assert PASSWORD not in repr(result)


@pytest.mark.parametrize(
    ("text", "what"),
    [
        (f"token={JWT}", "a login token (JWT)"),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789", "a bearer token"),
        (f"Cookie: restricted_refresh_token={REFRESH}", "a refresh-token cookie"),
    ],
)
def test_token_shapes_are_found_wherever_they_sit(tmp_path: Path, text: str, what: str) -> None:
    data = private_tree(tmp_path / "data")
    (data / "ovo-insights.sqlite3").write_bytes(b"\x00\x01binary " + text.encode() + b"\xff")
    hits = verify.scan_tree(data, {}, verify.TOKEN_PATTERNS).hits
    assert [(h.path.name, h.what) for h in hits] == [("ovo-insights.sqlite3", what)]


def test_what_the_redaction_filter_writes_never_counts_as_a_leak(tmp_path: Path) -> None:
    """The log filter and this scan agree: after redaction, nothing is left to find."""
    data = private_tree(tmp_path / "data")
    raw = f"Authorization: Bearer {JWT}\nCookie: restricted_refresh_token={REFRESH}\ntoken {JWT}\n"
    assert verify.scan_tree(private_tree(tmp_path / "raw"), {}, verify.TOKEN_PATTERNS).hits == []
    (tmp_path / "raw" / "sync.lock").write_text(raw)
    assert len(verify.scan_tree(tmp_path / "raw", {}, verify.TOKEN_PATTERNS).hits) == 3
    (data / "logs" / "ovo-insights.log").write_text(redact(raw))
    assert verify.scan_tree(data, NEEDLES, verify.TOKEN_PATTERNS).hits == []


def test_a_secret_split_across_two_chunks_is_still_found(tmp_path: Path) -> None:
    data = private_tree(tmp_path / "data")
    (data / "ovo-insights.sqlite3").write_bytes(b"A" * 1000 + PASSWORD.encode() + b"B" * 1000)
    for chunk in (7, 100, 1013, 1020, 4096):  # the password straddles the boundary somewhere
        hits = verify.scan_tree(data, NEEDLES, {}, chunk_bytes=chunk).hits
        assert len(hits) == 1, chunk


def test_one_hit_per_kind_per_file_however_often_it_appears(tmp_path: Path) -> None:
    data = private_tree(tmp_path / "data")
    (data / "sync.lock").write_text((PASSWORD + "\n") * 500 + JWT)
    hits = verify.scan_tree(data, NEEDLES, verify.TOKEN_PATTERNS).hits
    assert sorted(h.what for h in hits) == ["a login token (JWT)", "your password"]


def test_big_unreadable_and_skipped_files_are_counted_not_fatal(tmp_path: Path) -> None:
    root = tmp_path / "caches"
    (root / "ms-playwright").mkdir(parents=True)
    (root / "ms-playwright" / "browser.bin").write_bytes(PASSWORD.encode())  # skipped folder
    (root / "big").write_bytes(b"x" * 2_000)
    (root / "locked").write_bytes(PASSWORD.encode())
    (root / "locked").chmod(0)
    (root / "fine").write_bytes(b"ok")
    result = verify.scan_tree(root, NEEDLES, {}, max_file_bytes=1_000)
    (root / "locked").chmod(0o600)
    assert result.skipped_large == 1
    assert result.skipped_unreadable == 1
    assert result.files_scanned == 1
    assert result.hits == []


def test_symlinks_are_not_followed_when_scanning(tmp_path: Path) -> None:
    root = tmp_path / "caches"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text(PASSWORD)
    (root / "link").symlink_to(outside)
    (root / "loop").symlink_to(root)
    result = verify.scan_tree(root, NEEDLES, {})
    assert result.hits == []
    assert result.files_scanned == 0


def test_a_missing_root_scans_nothing(tmp_path: Path) -> None:
    result = verify.scan_tree(tmp_path / "nope", NEEDLES, verify.TOKEN_PATTERNS)
    assert (result.files_scanned, result.hits) == (0, [])


def test_the_caches_are_searched_for_the_password_only(tmp_path: Path) -> None:
    data = private_tree(tmp_path / "data")
    caches = tmp_path / "caches"
    caches.mkdir()
    (caches / "other-app").write_text(f"their own token {JWT}")  # not ours: ignored there
    assert verify.summarise(list(verify.scan_roots(data, caches, password=PASSWORD)))
    (caches / "leaky").write_text(PASSWORD)
    scans = dict(verify.scan_roots(data, caches, password=PASSWORD))
    assert [h.path.name for h in scans["caches"].hits] == ["leaky"]
    assert scans["data folder"].hits == []
    assert not verify.summarise(list(scans.items()))


def test_without_a_password_only_shapes_are_searched_and_no_caches_means_none(
    tmp_path: Path,
) -> None:
    data = private_tree(tmp_path / "data")
    (data / "sync.lock").write_text(PASSWORD)
    scans = list(verify.scan_roots(data, None, password=None))
    assert [label for label, _ in scans] == ["data folder"]
    assert verify.summarise(scans)


def test_short_passwords_are_flagged_as_unreliable() -> None:
    assert verify.short_password("abc1234")
    assert not verify.short_password("abcd12345")
    assert not verify.short_password(None)


def test_the_default_caches_folder_is_only_returned_when_it_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert verify.default_caches_dir() is None
    (tmp_path / "Library" / "Caches").mkdir(parents=True)
    assert verify.default_caches_dir() == tmp_path / "Library" / "Caches"


# -- the command --------------------------------------------------------------------------


@pytest.fixture
def saved_login(monkeypatch: pytest.MonkeyPatch) -> MemoryCredentialStore:
    store = MemoryCredentialStore()
    store.save(Credentials("someone@example.com", Secret(PASSWORD)))
    monkeypatch.setattr(cli, "_store", lambda: store)
    return store


def run_verify(data: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> tuple[int, str]:
    monkeypatch.setenv(DATA_DIR_ENV, str(data))
    result = runner.invoke(app, ["dev", "verify", *args])
    return result.exit_code, result.output


def test_verify_passes_on_a_clean_private_folder(
    tmp_path: Path, saved_login: MemoryCredentialStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = private_tree(tmp_path / "data")
    code, out = run_verify(data, monkeypatch, "--no-caches")
    assert code == 0, out
    assert "PASS  permissions: data folder (mode 0700)" in out
    assert "PASS  data folder: searched 3 files (0 too large, 0 unreadable)" in out
    assert "All checks passed." in out
    assert "caches" not in out
    assert PASSWORD not in out


def test_verify_fails_loudly_without_ever_printing_the_secret(
    tmp_path: Path, saved_login: MemoryCredentialStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = private_tree(tmp_path / "data")
    (data / "logs" / "ovo-insights.log").write_text(f"oops {PASSWORD} {JWT}")
    (data / "sync.lock").chmod(0o644)
    code, out = run_verify(data, monkeypatch, "--no-caches")
    assert code == 1
    assert "FAIL  permissions: sync.lock (mode 0644, should be 0600 or tighter)" in out
    assert f"FAIL  data folder: {data / 'logs' / 'ovo-insights.log'} contains your password" in out
    assert "contains a login token (JWT)" in out
    assert "Something failed" in out
    assert PASSWORD not in out
    assert JWT not in out


def test_verify_searches_the_caches_when_asked(
    tmp_path: Path,
    saved_login: MemoryCredentialStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = private_tree(tmp_path / "data")
    caches = tmp_path / "Library" / "Caches"
    caches.mkdir(parents=True)
    (caches / "leaky").write_text(PASSWORD)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    code, out = run_verify(data, monkeypatch)
    assert code == 1
    assert f"FAIL  caches: {caches / 'leaky'} contains your password" in out
    assert "PASS  data folder" in out
    assert PASSWORD not in out


def test_verify_notes_when_there_is_no_saved_login_or_no_caches_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_store", lambda: MemoryCredentialStore())
    monkeypatch.setattr(Path, "home", lambda: tmp_path)  # no Library/Caches here
    code, out = run_verify(private_tree(tmp_path / "data"), monkeypatch)
    assert code == 0
    assert "NOTE  no saved login: your password can't be searched for." in out
    assert "NOTE  there is no Caches folder here" in out


def test_verify_copes_with_an_unreadable_keychain_and_a_short_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken:
        def load(self) -> Credentials | None:
            raise CredentialError("keychain locked")

    monkeypatch.setattr(cli, "_store", lambda: Broken())
    code, out = run_verify(private_tree(tmp_path / "data"), monkeypatch, "--no-caches")
    assert code == 0
    assert "NOTE  the saved login can't be read" in out
    short = MemoryCredentialStore()
    short.save(Credentials("someone@example.com", Secret("abc12")))
    monkeypatch.setattr(cli, "_store", lambda: short)
    code, out = run_verify(tmp_path / "data", monkeypatch, "--no-caches")
    assert "NOTE  your password is short" in out


def test_verify_without_a_data_folder_says_so(
    tmp_path: Path, saved_login: MemoryCredentialStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, out = run_verify(tmp_path / "missing", monkeypatch, "--no-caches")
    assert code == 1
    assert "FAIL  permissions: data folder (doesn't exist yet: run a sync first)" in out


# -- progress, skipping and streaming -----------------------------------------------------


def test_progress_is_reported_every_so_many_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "PROGRESS_EVERY_FILES", 2)
    root = tmp_path / "caches"
    root.mkdir()
    for i in range(5):
        (root / f"f{i}").write_bytes(b"x")
    seen: list[int] = []
    verify.scan_tree(root, NEEDLES, {}, progress=seen.append)
    assert seen == [2, 4]


def test_ctrl_c_stops_a_search_early_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "PROGRESS_EVERY_FILES", 2)
    root = tmp_path / "caches"
    root.mkdir()
    for i in range(6):
        (root / f"f{i}").write_bytes(PASSWORD.encode() if i == 0 else b"x")

    def stop(_files: int) -> None:
        raise KeyboardInterrupt

    result = verify.scan_tree(root, NEEDLES, {}, progress=stop)
    assert result.interrupted
    assert result.files_scanned == 2  # what was searched before it stopped is kept
    assert not verify.summarise([("caches", result)])  # a skipped search never counts as a pass
    assert verify.summarise([("caches", verify.ScanResult())])


def test_each_folder_is_reported_as_soon_as_it_is_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "PROGRESS_EVERY_FILES", 1)
    data = private_tree(tmp_path / "data")
    caches = tmp_path / "caches"
    caches.mkdir()
    (caches / "one").write_bytes(b"x")
    labels: list[str] = []
    scans = verify.scan_roots(
        data, caches, password=PASSWORD, progress=lambda name, _n: labels.append(name)
    )
    label, first = next(scans)
    assert label == "data folder"
    assert first.files_scanned == 3
    assert set(labels) == {"data folder"}  # the caches haven't been touched yet
    label, _ = next(scans)
    assert label == "caches"
    assert "caches" in labels


def test_big_files_in_the_caches_are_skipped_but_not_in_the_data_folder(tmp_path: Path) -> None:
    data = private_tree(tmp_path / "data")
    caches = tmp_path / "caches"
    caches.mkdir()
    size = verify.MAX_CACHE_FILE_BYTES + 1
    for folder in (data, caches):
        with (folder / "big").open("wb") as handle:
            handle.truncate(size)  # a sparse file: costs nothing to make
    (data / "logs" / "ovo-insights.log").write_text("fine")
    scans = dict(verify.scan_roots(data, caches, password=PASSWORD))
    assert scans["caches"].skipped_large == 1
    assert scans["data folder"].skipped_large == 0
    assert scans["data folder"].files_scanned == 4


def test_verify_reports_the_data_folder_before_it_starts_on_the_caches(
    tmp_path: Path, saved_login: MemoryCredentialStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = private_tree(tmp_path / "data")
    caches = tmp_path / "Library" / "Caches"
    caches.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    code, out = run_verify(data, monkeypatch)
    assert code == 0
    order = [
        out.index("Reading your saved login from the Keychain"),
        out.index("Searching the data folder..."),
        out.index("PASS  data folder: searched 3 files"),
        out.index(f"Searching {caches} for your password. This can take several minutes"),
        out.index("PASS  caches: searched 0 files"),
    ]
    assert order == sorted(order)
    assert "press Ctrl-C to skip it" in out


def test_verify_says_it_is_still_working_during_a_long_search(
    tmp_path: Path, saved_login: MemoryCredentialStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "PROGRESS_SECONDS", 0.0)
    monkeypatch.setattr(verify, "PROGRESS_EVERY_FILES", 1)
    code, out = run_verify(private_tree(tmp_path / "data"), monkeypatch, "--no-caches")
    assert code == 0
    assert "still searching the data folder: 1 files so far..." in out
    assert "still searching the data folder: 3 files so far..." in out


def test_a_skipped_caches_search_is_reported_and_is_not_a_pass(
    tmp_path: Path, saved_login: MemoryCredentialStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = private_tree(tmp_path / "data")
    (tmp_path / "Library" / "Caches").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    real = verify.scan_tree

    def interrupted_in_caches(root: Path, *args: object, **kwargs: object) -> verify.ScanResult:
        if root.name == "Caches":
            return verify.ScanResult(files_scanned=1234, interrupted=True)
        return real(root, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(verify, "scan_tree", interrupted_in_caches)
    code, out = run_verify(data, monkeypatch)
    assert code == 1
    assert "PASS  data folder" in out
    assert "SKIP  caches: stopped after 1,234 files, so not fully searched" in out
    assert "Something failed or was skipped" in out
    assert "All checks passed" not in out

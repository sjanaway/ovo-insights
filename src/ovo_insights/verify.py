"""`ovo-insights dev verify`: are the files private, and is anything secret where it shouldn't be?

Two checks, for the M12 verification pass:

- *Permissions:* the data folder is `0700`, and every file and folder in it is `0600` or
  `0700`, owned by you, and not a symlink.
- *Leak scan:* your saved password, and anything shaped like a login token, must not appear in
  the data folder (database and logs), and your password must not appear in `~/Library/Caches`.

The scan never prints what it finds, only which file and what kind of thing. Login tokens live
only in memory, so they can't be searched for by value; instead the data folder is searched for
their *shape* (a JWT, a bearer token, a refresh-token cookie). That is noisy in the system
caches, where other apps keep their own tokens, so the caches are searched for the password only.
"""

import os
import re
import stat
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

CHUNK_BYTES = 4 * 1024 * 1024
# Longer than any needle or token, so a match can't hide across the boundary between two chunks.
OVERLAP_BYTES = 16 * 1024
MAX_FILE_BYTES = 100 * 1024 * 1024
# Caches hold other apps' media and binaries, and anything of ours that leaked is small text or
# a small database, so larger files there are skipped (and counted) to keep the search quick.
MAX_CACHE_FILE_BYTES = 25 * 1024 * 1024
# How often the progress callback is told how many files have been searched.
PROGRESS_EVERY_FILES = 500
# Big binaries with nothing of ours in them (a downloaded test browser, say).
SKIP_DIRECTORIES = frozenset({"ms-playwright"})

# Shapes of things that must never be written down. The redaction filter replaces these with
# `[REDACTED]`, which none of them match.
TOKEN_PATTERNS: dict[str, "re.Pattern[bytes]"] = {
    "a login token (JWT)": re.compile(
        rb"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    ),
    "a bearer token": re.compile(rb"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}", re.ASCII),
    "a refresh-token cookie": re.compile(
        rb"(?i)restricted_refresh_token[\"'\s:=]+[A-Za-z0-9._~+/=-]{20,}", re.ASCII
    ),
}


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


# -- permissions --------------------------------------------------------------------------


def _mode(info: os.stat_result) -> int:
    return stat.S_IMODE(info.st_mode)


def _entries(root: Path) -> Iterator[Path]:
    yield root
    for directory, folders, files in os.walk(root, followlinks=False):
        for name in sorted(folders + files):
            yield Path(directory) / name


def check_permissions(data_dir: Path) -> list[Check]:
    """One check for the folder itself, then one per file or folder inside it."""
    if not data_dir.exists():
        return [Check("data folder", False, "doesn't exist yet: run a sync first")]
    checks = []
    for path in _entries(data_dir):
        label = "data folder" if path == data_dir else str(path.relative_to(data_dir))
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            checks.append(Check(label, False, "is a symlink"))
            continue
        wanted = 0o700 if stat.S_ISDIR(info.st_mode) else 0o600
        problems = []
        if info.st_uid != os.getuid():
            problems.append("owned by someone else")
        if path == data_dir and _mode(info) != 0o700:
            problems.append(f"mode {_mode(info):04o}, should be 0700")
        elif path != data_dir and _mode(info) & 0o077:
            problems.append(f"mode {_mode(info):04o}, should be {wanted:04o} or tighter")
        checks.append(Check(label, not problems, ", ".join(problems) or f"mode {_mode(info):04o}"))
    return checks


# -- the leak scan ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Hit:
    path: Path
    what: str  # "your password", "a login token (JWT)": never the thing itself


@dataclass(slots=True)
class ScanResult:
    files_scanned: int = 0
    skipped_large: int = 0
    skipped_unreadable: int = 0
    interrupted: bool = False  # stopped early (Ctrl-C), so not everything was searched
    hits: list[Hit] = field(default_factory=list)


def scan_tree(
    root: Path,
    needles: dict[str, bytes],
    patterns: dict[str, "re.Pattern[bytes]"],
    *,
    chunk_bytes: int = CHUNK_BYTES,
    max_file_bytes: int = MAX_FILE_BYTES,
    skip_directories: frozenset[str] = SKIP_DIRECTORIES,
    progress: Callable[[int], None] | None = None,
) -> ScanResult:
    """Search every regular file under `root` (symlinks are not followed) for the needles, by
    value, and the patterns, by shape.

    `progress` is called with the number of files searched so far, every few hundred files.
    Ctrl-C (or anything `progress` raises as `KeyboardInterrupt`) stops the search early rather
    than the program: what was found so far is returned, with `interrupted` set."""
    result = ScanResult()
    if not root.is_dir():
        return result
    visited = 0
    try:
        for directory, folders, files in os.walk(root, followlinks=False):
            folders[:] = [f for f in folders if f not in skip_directories]
            for name in files:
                _search_one(
                    Path(directory) / name, needles, patterns, chunk_bytes, max_file_bytes, result
                )
                visited += 1
                if progress is not None and visited % PROGRESS_EVERY_FILES == 0:
                    progress(result.files_scanned)
    except KeyboardInterrupt:
        result.interrupted = True
    return result


def _search_one(
    path: Path,
    needles: dict[str, bytes],
    patterns: dict[str, "re.Pattern[bytes]"],
    chunk_bytes: int,
    max_file_bytes: int,
    result: ScanResult,
) -> None:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            return
        if info.st_size > max_file_bytes:
            result.skipped_large += 1
            return
        found = _search_file(path, needles, patterns, chunk_bytes)
    except OSError:
        result.skipped_unreadable += 1
        return
    result.files_scanned += 1
    result.hits.extend(Hit(path, what) for what in found)


def _search_file(
    path: Path,
    needles: dict[str, bytes],
    patterns: dict[str, "re.Pattern[bytes]"],
    chunk_bytes: int,
) -> list[str]:
    found: dict[str, None] = {}
    carry = b""
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_bytes):
            window = carry + chunk
            for what, needle in needles.items():
                if needle in window:
                    found.setdefault(what)
            for what, pattern in patterns.items():
                if pattern.search(window):
                    found.setdefault(what)
            carry = window[-OVERLAP_BYTES:]
    return list(found)


def scan_roots(
    data_dir: Path,
    caches_dir: Path | None,
    *,
    password: str | None,
    chunk_bytes: int = CHUNK_BYTES,
    progress: Callable[[str, int], None] | None = None,
) -> Iterator[tuple[str, ScanResult]]:
    """The data folder (the password and token shapes), then, if given, the caches (the
    password only), as `(label, result)` pairs, each yielded as soon as it is finished."""
    needles = {"your password": password.encode()} if password else {}

    def report(label: str) -> Callable[[int], None] | None:
        return None if progress is None else lambda files: progress(label, files)

    yield (
        "data folder",
        scan_tree(
            data_dir,
            needles,
            TOKEN_PATTERNS,
            chunk_bytes=chunk_bytes,
            progress=report("data folder"),
        ),
    )
    if caches_dir is not None:
        yield (
            "caches",
            scan_tree(
                caches_dir,
                needles,
                {},
                chunk_bytes=chunk_bytes,
                max_file_bytes=MAX_CACHE_FILE_BYTES,
                progress=report("caches"),
            ),
        )


def default_caches_dir() -> Path | None:
    caches = Path.home() / "Library" / "Caches"
    return caches if caches.is_dir() else None


def short_password(password: str | None) -> bool:
    """A password this short may turn up in files by coincidence."""
    return password is not None and len(password) < 8


def summarise(scans: Sequence[tuple[str, ScanResult]]) -> bool:
    """True only if nothing was found and every search ran to the end."""
    return all(not result.hits and not result.interrupted for _, result in scans)

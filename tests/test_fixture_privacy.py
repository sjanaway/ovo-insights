"""CI guard: nothing under tests/fixtures may look like real personal data.

Fails on 13-digit numbers that pass the MPAN check digit, UK postcodes, email addresses
outside example.com, JWTs, and password fields. Redacted captures are generated so they pass.
"""

import re
from pathlib import Path

import pytest

from ovo_insights.ovo.fixtures import is_valid_mpan

FIXTURES = Path(__file__).resolve().parent / "fixtures"

_THIRTEEN_DIGITS = re.compile(r"(?<!\d)\d{13}(?!\d)", re.ASCII)
_POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", re.ASCII)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", re.ASCII)
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.", re.ASCII)
_PASSWORD = re.compile(r"(?i)\"password\"\s*:\s*\"[^\"]+\"", re.ASCII)


def problems_in(text: str) -> list[str]:
    found = [f"valid MPAN {m}" for m in _THIRTEEN_DIGITS.findall(text) if is_valid_mpan(m)]
    found += [f"postcode {m}" for m in _POSTCODE.findall(text)]
    found += [f"email domain {d}" for d in _EMAIL.findall(text) if d.lower() not in {"example.com"}]
    found += ["JWT" for _ in _JWT.findall(text)]
    found += ["password field" for _ in _PASSWORD.findall(text)]
    return found


def _fixture_files() -> list[Path]:
    return sorted(p for p in FIXTURES.rglob("*") if p.is_file())


@pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: str(p.relative_to(FIXTURES)))
def test_fixture_contains_no_personal_data(path: Path) -> None:
    assert problems_in(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "text",
    [
        "mpan 1234567890126",  # valid check digit
        "postcode SW1A 1AA",
        "mail jo@realmail.co.uk",
        "token eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.sig",
        '{"password": "hunter2"}',
    ],
)
def test_scanner_catches_examples(text: str) -> None:
    assert problems_in(text)


def test_scanner_allows_safe_content() -> None:
    assert problems_in('{"sprn": "1900000000000", "email": "someone@example.com"}') == []

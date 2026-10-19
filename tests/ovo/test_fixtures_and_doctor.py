import json
import os
import re
import stat
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from ovo_insights.cli import app
from ovo_insights.ovo import convert, models
from ovo_insights.ovo.client import GRAPHQL_URL, USAGE_BASE, OvoClient
from ovo_insights.ovo.doctor import run_checks
from ovo_insights.ovo.fixtures import (
    Redactor,
    Synthesiser,
    dumps,
    is_valid_mpan,
    mpan_check_digit,
    write_private,
)
from ovo_insights.timeutil import london_date
from tests.ovo.conftest import ACCOUNT, fixture, mock_login

# -- MPAN check digit ---------------------------------------------------------------------


def test_mpan_check_digit_known_example() -> None:
    # Worked example from the MPAN check-digit algorithm (primes 3..43, mod 11, mod 10).
    digits = "123456789012"
    primes = (3, 5, 7, 13, 17, 19, 23, 29, 31, 37, 41, 43)
    expected = sum(int(d) * p for d, p in zip(digits, primes, strict=True)) % 11 % 10

    assert mpan_check_digit(digits) == expected
    assert is_valid_mpan(digits + str(expected))
    assert not is_valid_mpan(digits + str((expected + 1) % 10))
    assert not is_valid_mpan("12345")


# -- redactor -----------------------------------------------------------------------------


def test_redactor_replaces_sensitive_values_everywhere_consistently() -> None:
    payload = {
        "data": {
            "account": {
                "id": "7654321",
                "note": "account 7654321 belongs to jo@realmail.com",
                "accountSupplyPoints": [
                    {"supplyPoint": {"sprn": "1234567890123", "meterSerialNumber": "23E1234567"}},
                    {"supplyPoint": {"sprn": "1234567890123", "postCode": "SW1A 1AA"}},
                ],
                "consumption": Decimal("1.25"),
            }
        }
    }

    redacted = Redactor("salt", extra_sensitive=["cust-guid-1234"]).redact(payload)
    text = json.dumps(redacted, default=str)

    for real in ("7654321", "1234567890123", "23E1234567", "SW1A", "jo@realmail.com"):
        assert real not in text
    account = redacted["data"]["account"]
    assert account["id"] in account["note"]  # same fake everywhere
    sprns = [p["supplyPoint"]["sprn"] for p in account["accountSupplyPoints"]]
    assert sprns[0] == sprns[1]
    assert len(sprns[0]) == 13
    assert sprns[0].isdigit()
    assert not is_valid_mpan(sprns[0])
    assert account["consumption"] == Decimal("1.25")


def test_redactor_sweeps_extra_sensitive_values() -> None:
    redacted = Redactor("s", extra_sensitive=["cust-guid-1234", ""]).redact(
        {"message": "for cust-guid-1234"}
    )

    assert "cust-guid-1234" not in redacted["message"]


def test_redactor_fakes_emails_and_unicode() -> None:
    redacted = Redactor("s").redact({"email": "jo@realmail.com", "name": "Zoë"})

    assert redacted["email"] == "someone@example.com"
    assert redacted["name"].isascii()


# -- synthesiser --------------------------------------------------------------------------

CAPTURED = Path(__file__).resolve().parent.parent / "fixtures" / "captured"
CAPTURE_NAMES = ("bootstrap", "meter_reads", "monthly", "daily", "half_hourly", "half_hourly_local")


def _capture() -> dict[str, Any]:
    return {
        name: json.loads((CAPTURED / f"{name}.json").read_text(), parse_float=Decimal)
        for name in CAPTURE_NAMES
    }


def _values(payload: Any, key: str) -> list[str]:
    return re.findall(rf'"{key}": "?([0-9.]+)', dumps(payload))


def test_synthesiser_replaces_every_number_and_moves_account_dates() -> None:
    before = _capture()
    after = Synthesiser("salt").synthesise(_capture())

    for name in CAPTURE_NAMES:
        for key in ("consumption", "amount", "value", "anytime", "standing"):
            old, new = _values(before[name], key), _values(after[name], key)
            assert len(old) == len(new), (name, key)
            assert sum(o == n for o, n in zip(old, new, strict=True)) <= len(old) // 20
    old_starts = re.findall(r'"startDate": "([^"]+)"', dumps(before["bootstrap"]))
    new_starts = re.findall(r'"startDate": "([^"]+)"', dumps(after["bootstrap"]))
    assert len(old_starts) == len(new_starts)
    assert not set(old_starts) & set(new_starts)


def test_synthesiser_keeps_the_relationships_between_endpoints() -> None:
    out = Synthesiser("another salt").synthesise(_capture())
    account = "acct"

    daily = convert.daily(account, models.DailyResponse.model_validate(out["daily"]))
    for row in daily:
        assert row.cost is not None
        assert row.unit_rate is not None
        expected = (Decimal(str(row.kwh)) * row.unit_rate / 1000).quantize(
            Decimal(1), rounding=ROUND_HALF_UP
        ) * 1000
        assert row.cost == expected
    months = convert.monthly(account, models.MonthlyResponse.model_validate(out["monthly"]))
    september = [m for m in months if m.month == date(2026, 9, 1)]
    assert september
    for month in september:
        days = [d for d in daily if d.fuel is month.fuel]
        assert month.cost == sum(d.cost or 0 for d in days)
        assert month.kwh == pytest.approx(sum(d.kwh for d in days), abs=1e-9)
    utc, local = (
        {
            r.interval_start: r.kwh
            for r in convert.half_hours(
                account, models.HalfHourResponse.model_validate(out[n]), naive_is_utc=True
            )
        }
        for n in ("half_hourly", "half_hourly_local")
    )
    overlap = utc.keys() & local.keys()
    assert overlap
    assert all(utc[t] == local[t] for t in overlap)
    data = convert.meter_data(account, models.ReadsResponse.model_validate(out["meter_reads"]))
    assert len(data.readings) == 717


def test_synthesiser_is_repeatable_per_salt() -> None:
    assert dumps(Synthesiser("a").synthesise(_capture())) == dumps(
        Synthesiser("a").synthesise(_capture())
    )
    assert dumps(Synthesiser("a").synthesise(_capture())) != dumps(
        Synthesiser("b").synthesise(_capture())
    )


@pytest.mark.parametrize(
    "stamp",
    [
        "2019-04-05T23:00:00.000Z",  # midnight BST
        "1996-12-20T00:00:00.000Z",  # midnight GMT
        "2021-03-27T23:30:00Z",  # the night the clocks go forward, no fraction
    ],
)
def test_synthesiser_moves_the_uk_date_by_exactly_the_shift(stamp: str) -> None:
    synthesiser = Synthesiser("salt")
    shift = synthesiser._shift
    moved = synthesiser._shift_dates({"date": stamp})["date"]

    def uk_day(text: str) -> date:
        return london_date(datetime.fromisoformat(text.replace("Z", "+00:00")))

    assert uk_day(moved) == uk_day(stamp) - shift
    assert moved.endswith("Z")
    assert ("." in moved) == ("." in stamp)
    assert (
        synthesiser._shift_dates({"startDate": "2021-08-13"})["startDate"]
        == (date(2021, 8, 13) - shift).isoformat()
    )


def test_synthesiser_ignores_missing_endpoints_and_empty_values() -> None:
    synthesiser = Synthesiser("s")

    assert synthesiser.synthesise({}) == {}
    out = synthesiser.synthesise(
        {
            "daily": {"electricity": {"data": [{"consumption": None}]}, "gas": None},
            "monthly": {
                "electricity": {"data": [{"consumption": Decimal("1.5"), "month": 1, "year": 2026}]}
            },
        }
    )
    assert out["daily"]["electricity"]["data"] == [{"consumption": None}]
    assert out["monthly"]["electricity"]["data"][0]["consumption"] != Decimal("1.5")


def test_dumps_preserves_exact_decimals() -> None:
    text = dumps({"a": Decimal("0.10"), "b": Decimal("12345678901234567890.123456789"), "c": 1})

    assert '"a": 0.10' in text
    assert '"b": 12345678901234567890.123456789' in text
    assert json.loads(text, parse_float=Decimal)["a"] == Decimal("0.10")


def test_write_private_creates_owner_only_files(tmp_path: Path) -> None:
    target = tmp_path / "raw" / "x.json"

    write_private(target, "{}")

    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(target.parent).st_mode) == 0o700


# -- doctor -------------------------------------------------------------------------------


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _mock_everything(mock: respx.MockRouter) -> None:
    mock_login(mock)
    mock.post(GRAPHQL_URL).side_effect = lambda request: (
        httpx.Response(200, json=fixture("meter_reads"))
        if b"MeterReads" in request.content
        else httpx.Response(200, json=fixture("bootstrap"))
    )
    mock.get(f"{USAGE_BASE}/monthly/{ACCOUNT}").respond(200, json=fixture("monthly"))
    mock.get(f"{USAGE_BASE}/daily/{ACCOUNT}").respond(200, json=fixture("daily"))
    mock.get(f"{USAGE_BASE}/half-hourly-local/{ACCOUNT}").respond(200, json=fixture("half_hourly"))
    mock.get(f"{USAGE_BASE}/half-hourly/{ACCOUNT}").respond(404)


@pytest.mark.anyio
async def test_doctor_reports_each_check(client: OvoClient, mock: respx.MockRouter) -> None:
    _mock_everything(mock)

    results = await run_checks(client, today=date(2025, 2, 10))

    by_name = {r.name: r for r in results}
    assert by_name["Log in and get access token"].ok
    assert "1 account(s)" in by_name["Accounts (GraphQL)"].detail
    assert by_name["Half-hourly usage (half-hourly-local)"].ok
    assert not by_name["Half-hourly usage (half-hourly)"].ok
    assert "UpstreamSchemaChanged" in by_name["Half-hourly usage (half-hourly)"].detail
    assert "3 reading(s)" in by_name["Meters and readings (GraphQL)"].detail
    # Details never contain identifiers.
    joined = " ".join(r.detail for r in results)
    for secret in (ACCOUNT, "1900000000000", "23E0000000"):
        assert secret not in joined


@pytest.mark.anyio
async def test_doctor_stops_after_failed_login(client: OvoClient, mock: respx.MockRouter) -> None:
    mock.post("https://my.ovoenergy.com/api/v2/auth/login").respond(401)

    results = await run_checks(client)

    assert [(r.name, r.ok) for r in results] == [("Log in and get access token", False)]


@pytest.mark.anyio
async def test_doctor_stops_when_no_accounts(client: OvoClient, mock: respx.MockRouter) -> None:
    mock_login(mock)
    mock.post(GRAPHQL_URL).respond(
        200, json={"data": {"customer_nextV1": {"customerAccountRelationships": {"edges": []}}}}
    )

    results = await run_checks(client)

    assert [r.name for r in results] == ["Log in and get access token", "Accounts (GraphQL)"]


def test_doctor_cli_prints_table_and_exit_code(
    monkeypatch: pytest.MonkeyPatch, mock: respx.MockRouter
) -> None:
    _mock_everything(mock)
    monkeypatch.setattr("getpass.getpass", lambda prompt: "pw-SENTINEL")
    monkeypatch.setattr("ovo_insights.ovo.client.DEFAULT_MIN_INTERVAL", 0)

    result = CliRunner().invoke(app, ["doctor"], input="someone@example.com\n")

    assert "PASS  Log in and get access token" in result.output
    assert "FAIL  Half-hourly usage (half-hourly)" in result.output
    assert "pw-SENTINEL" not in result.output
    assert result.exit_code == 1


def test_cli_rejects_invalid_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("getpass.getpass", lambda prompt: "pw")

    result = CliRunner().invoke(app, ["doctor"], input="not-an-email\n")

    assert result.exit_code == 2
    assert "email" in result.output


# -- capture-fixtures ---------------------------------------------------------------------


def test_capture_fixtures_writes_raw_privately_and_redacted_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mock: respx.MockRouter
) -> None:
    _mock_everything(mock)
    mock.get(f"{USAGE_BASE}/half-hourly/{ACCOUNT}").respond(200, json=fixture("half_hourly"))
    monkeypatch.setattr("getpass.getpass", lambda prompt: "pw")
    monkeypatch.setattr("ovo_insights.ovo.client.DEFAULT_MIN_INTERVAL", 0)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").touch()

    result = CliRunner().invoke(app, ["dev", "capture-fixtures"], input="someone@example.com\n")

    assert result.exit_code == 0, result.output
    raw = tmp_path / "fixtures-raw"
    out = tmp_path / "tests" / "fixtures" / "captured"
    assert stat.S_IMODE(os.stat(raw / "bootstrap.json").st_mode) == 0o600
    assert ACCOUNT in (raw / "bootstrap.json").read_text()
    for path in out.glob("*.json"):
        text = path.read_text()
        assert ACCOUNT not in text, path.name
        assert "1900000000000" not in text, path.name
        assert "23E0000000" not in text, path.name
    assert {p.stem for p in out.glob("*.json")} == {
        "bootstrap",
        "meter_reads",
        "monthly",
        "daily",
        "half_hourly_local",
        "half_hourly",
    }


def test_capture_fixtures_refuses_outside_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(app, ["dev", "capture-fixtures"])

    assert result.exit_code == 2


def test_capture_fixtures_refuses_raw_dir_inside_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").touch()

    result = CliRunner().invoke(app, ["dev", "capture-fixtures", "--raw-dir", "tests/raw"])

    assert result.exit_code == 2


def test_now_is_used() -> None:
    assert datetime.now(UTC).year >= 2025

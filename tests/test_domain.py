from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ovo_insights.domain import (
    GAS_REGISTER_ID,
    DailyUsage,
    DomainError,
    Fuel,
    HalfHourUsage,
    Meter,
    MonthlyUsage,
    Reading,
    Register,
    SupplyPoint,
    UnknownFuelError,
    validate_identifier,
)
from ovo_insights.money import MilliPence
from ovo_insights.timeutil import NaiveDatetimeError

E = Fuel.ELECTRICITY
T0 = datetime(2025, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "fuel"),
    [
        ("ELECTRICITY", Fuel.ELECTRICITY),
        ("ELECTRIC", Fuel.ELECTRICITY),
        ("electricity", Fuel.ELECTRICITY),
        (" Gas ", Fuel.GAS),
    ],
)
def test_fuel_parse_accepts_known_spellings(text: str, fuel: Fuel) -> None:
    assert Fuel.parse(text) is fuel


@pytest.mark.parametrize("text", ["", "WATER", "ELEC", "gas2"])
def test_fuel_parse_rejects_unknown(text: str) -> None:
    with pytest.raises(UnknownFuelError):
        Fuel.parse(text)


@pytest.mark.parametrize("value", ["1234567", "abc-DEF_1.2/3", "A"])
def test_valid_identifiers(value: str) -> None:
    assert validate_identifier(value, "x") == value


@pytest.mark.parametrize(
    "value", ["", " leading", "-dash", "a" * 65, "semi;colon", "new\nline", "é", 123]
)
def test_invalid_identifiers(value: object) -> None:
    with pytest.raises(DomainError):
        validate_identifier(value, "x")  # type: ignore[arg-type]


def test_supply_point_validation() -> None:
    SupplyPoint("1", E, "1234567890123", date(2024, 1, 1), date(2024, 1, 1))

    with pytest.raises(DomainError, match="MPAN"):
        SupplyPoint("1", E, "12345", date(2024, 1, 1))
    with pytest.raises(DomainError, match="before it starts"):
        SupplyPoint("1", E, "123456", date(2024, 1, 2), date(2024, 1, 1))
    with pytest.raises(DomainError, match="fuel"):
        SupplyPoint("1", "electricity", "123456", date(2024, 1, 1))  # type: ignore[arg-type]
    with pytest.raises(DomainError, match="date"):
        SupplyPoint("1", E, "123456", T0)


def test_half_hour_normalises_to_utc() -> None:
    plus_one = timezone(timedelta(hours=1))
    usage = HalfHourUsage("1", E, datetime(2025, 7, 1, 1, 30, tzinfo=plus_one), 0.5)

    assert usage.interval_start == datetime(2025, 7, 1, 0, 30, tzinfo=UTC)
    assert usage.interval_start.tzinfo is UTC


@pytest.mark.parametrize("kwh", [-0.001, float("nan"), float("inf"), 500.1, True, "1", None])
def test_half_hour_rejects_bad_kwh(kwh: object) -> None:
    with pytest.raises(DomainError):
        HalfHourUsage("1", E, T0, kwh)  # type: ignore[arg-type]


def test_half_hour_integer_kwh_becomes_float() -> None:
    assert HalfHourUsage("1", E, T0, 2).kwh == 2.0
    assert isinstance(HalfHourUsage("1", E, T0, 2).kwh, float)


def test_half_hour_rejects_misaligned_and_naive() -> None:
    with pytest.raises(DomainError, match="aligned"):
        HalfHourUsage("1", E, T0.replace(minute=15), 0.1)
    with pytest.raises(NaiveDatetimeError):
        HalfHourUsage("1", E, datetime(2025, 1, 1), 0.1)  # noqa: DTZ001
    with pytest.raises(DomainError, match="fuel"):
        HalfHourUsage("1", "gas", T0, 0.1)  # type: ignore[arg-type]


def test_daily_validation() -> None:
    DailyUsage("1", E, date(2025, 1, 1), 10.0, MilliPence(500_000), has_half_hourly=True)
    DailyUsage("1", E, date(2025, 1, 1), 10.0, None, has_half_hourly=False)

    with pytest.raises(DomainError, match="cost"):
        DailyUsage("1", E, date(2025, 1, 1), 1.0, 1.5, has_half_hourly=True)  # type: ignore[arg-type]
    with pytest.raises(DomainError, match="cost"):
        DailyUsage("1", E, date(2025, 1, 1), 1.0, True, has_half_hourly=True)  # type: ignore[arg-type]
    with pytest.raises(DomainError, match="has_half_hourly"):
        DailyUsage("1", E, date(2025, 1, 1), 1.0, None, has_half_hourly=1)  # type: ignore[arg-type]
    with pytest.raises(DomainError, match="fuel"):
        DailyUsage("1", "gas", date(2025, 1, 1), 1.0, None, has_half_hourly=True)  # type: ignore[arg-type]


def test_monthly_validation() -> None:
    MonthlyUsage("1", E, date(2025, 1, 1), 300.0, None, "1234567890123")

    with pytest.raises(DomainError, match="1st"):
        MonthlyUsage("1", E, date(2025, 1, 2), 1.0, None)
    with pytest.raises(DomainError, match="MPAN"):
        MonthlyUsage("1", E, date(2025, 1, 1), 1.0, None, "x")
    with pytest.raises(DomainError, match="fuel"):
        MonthlyUsage("1", "gas", date(2025, 1, 1), 1.0, None)  # type: ignore[arg-type]


def test_meter_validation() -> None:
    Meter("S1", "1", E, "SMETS2", "ACTIVE")

    with pytest.raises(DomainError, match="meter type"):
        Meter("S1", "1", E, "x" * 65)
    with pytest.raises(DomainError, match="status"):
        Meter("S1", "1", E, None, "bad\x00")
    with pytest.raises(DomainError, match="fuel"):
        Meter("S1", "1", "gas")  # type: ignore[arg-type]


def test_register_validation() -> None:
    Register("S1", "1", date(2024, 1, 1), None, "STANDARD", "KWH", 5)
    Register("G1", GAS_REGISTER_ID, date(2024, 1, 1))

    with pytest.raises(DomainError, match="digit"):
        Register("S1", "1", date(2024, 1, 1), digits=0)
    with pytest.raises(DomainError, match="digit"):
        Register("S1", "1", date(2024, 1, 1), digits=True)
    with pytest.raises(DomainError, match="register id"):
        Register("S1", " bad", date(2024, 1, 1))


@pytest.mark.parametrize("value", ["0", "12345.678", "000123", "999999999999999.123456"])
def test_reading_accepts_plain_values(value: str) -> None:
    Reading("S1", "1", date(2025, 1, 1), Decimal(value))
    Reading("G1", GAS_REGISTER_ID, date(2025, 1, 1), Decimal(value))


@pytest.mark.parametrize(
    "value", [Decimal(-1), Decimal("NaN"), Decimal("Infinity"), Decimal("1.12345678901"), "12", 12]
)
def test_reading_rejects_bad_values(value: object) -> None:
    with pytest.raises(DomainError, match="reading value"):
        Reading("S1", "1", date(2025, 1, 1), value)  # type: ignore[arg-type]


def test_reading_rejects_bad_register() -> None:
    with pytest.raises(DomainError, match="register id"):
        Reading("S1", "bad;", date(2025, 1, 1), Decimal(1))


def test_types_are_immutable() -> None:
    usage = HalfHourUsage("1", E, T0, 0.5)

    with pytest.raises(AttributeError):
        usage.kwh = 1.0  # type: ignore[misc]

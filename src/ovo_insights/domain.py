"""Validated domain types.

Every type checks its invariants on construction, so an invalid value cannot exist in memory.
Values read back from the database go through the same constructors, which also guards against
a hand-edited or corrupted database.
"""

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self

from ovo_insights.money import MilliPence
from ovo_insights.timeutil import is_half_hour_aligned, require_utc

# Sanity ceilings for a domestic supply. Values above these indicate bad data, not big usage.
MAX_KWH_PER_HALF_HOUR = 500.0
MAX_KWH_PER_DAY = 10_000.0
MAX_KWH_PER_MONTH = 300_000.0

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._/-]{0,63}", re.ASCII)
_MPXN = re.compile(r"\d{6,13}", re.ASCII)
# Real estimated reads carry up to 8 decimal places.
_READING_VALUE = re.compile(r"\d{1,15}(?:\.\d{1,10})?", re.ASCII)

# OVO/Kaluza spell electricity two ways (seen in My-Ovo-Data's Constants.cs).
_FUEL_ALIASES = {"ELECTRICITY": "electricity", "ELECTRIC": "electricity", "GAS": "gas"}

# Gas meters have a single register; readings use this register id.
GAS_REGISTER_ID = ""


class DomainError(ValueError):
    """A value violated a domain invariant."""


class UnknownFuelError(DomainError):
    """The API reported a fuel type we don't understand. Never coerced to a guess."""


class Fuel(StrEnum):
    ELECTRICITY = "electricity"
    GAS = "gas"

    @classmethod
    def parse(cls, text: str) -> Self:
        normalised = _FUEL_ALIASES.get(text.strip().upper())
        if normalised is None:
            raise UnknownFuelError(f"Unknown fuel type: {text!r}")
        return cls(normalised)


def validate_identifier(value: str, what: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise DomainError(f"Invalid {what}: {value!r}")
    return value


def _validate_kwh(value: float, ceiling: float, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DomainError(f"{what} must be a number, not {type(value).__name__}")
    if not math.isfinite(value) or value < 0 or value > ceiling:
        raise DomainError(f"{what} out of range (0..{ceiling}): {value!r}")
    return float(value)


def _validate_date(value: date, what: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise DomainError(f"{what} must be a date, not {type(value).__name__}")
    return value


def _validate_date_range(start: date, end: date | None, what: str) -> None:
    _validate_date(start, f"{what} start")
    if end is not None and _validate_date(end, f"{what} end") < start:
        raise DomainError(f"{what} ends ({end}) before it starts ({start})")


def _validate_optional_money(value: object, what: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise DomainError(f"{what} must be integer milli-pence, not {type(value).__name__}")


def _validate_optional_text(value: str | None, what: str, max_length: int = 64) -> None:
    if value is None:
        return
    if not isinstance(value, str) or len(value) > max_length or not value.isprintable():
        raise DomainError(f"Invalid {what}: {value!r}")


@dataclass(frozen=True, slots=True)
class SupplyPoint:
    account_id: str
    fuel: Fuel
    mpxn: str
    start_date: date
    end_date: date | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.account_id, "account id")
        if not isinstance(self.fuel, Fuel):
            raise DomainError(f"fuel must be a Fuel, not {type(self.fuel).__name__}")
        if not isinstance(self.mpxn, str) or not _MPXN.fullmatch(self.mpxn):
            raise DomainError(f"Invalid MPAN/MPRN: {self.mpxn!r}")
        _validate_date_range(self.start_date, self.end_date, "Supply")


@dataclass(frozen=True, slots=True)
class HalfHourUsage:
    account_id: str
    fuel: Fuel
    interval_start: datetime
    kwh: float

    def __post_init__(self) -> None:
        validate_identifier(self.account_id, "account id")
        if not isinstance(self.fuel, Fuel):
            raise DomainError(f"fuel must be a Fuel, not {type(self.fuel).__name__}")
        utc = require_utc(self.interval_start)
        if not is_half_hour_aligned(utc):
            raise DomainError(f"Half-hour interval not aligned: {self.interval_start!r}")
        object.__setattr__(self, "interval_start", utc)
        object.__setattr__(
            self, "kwh", _validate_kwh(self.kwh, MAX_KWH_PER_HALF_HOUR, "Half-hourly kWh")
        )


@dataclass(frozen=True, slots=True)
class DailyUsage:
    account_id: str
    fuel: Fuel
    day: date
    kwh: float
    cost: MilliPence | None
    has_half_hourly: bool
    # OVO's `cost` is kWh x unit rate only; the standing charge is separate (confirmed on
    # real data). Both rates are milli-pence: per kWh and per day respectively.
    unit_rate: MilliPence | None = None
    standing_charge: MilliPence | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.account_id, "account id")
        if not isinstance(self.fuel, Fuel):
            raise DomainError(f"fuel must be a Fuel, not {type(self.fuel).__name__}")
        _validate_date(self.day, "day")
        object.__setattr__(self, "kwh", _validate_kwh(self.kwh, MAX_KWH_PER_DAY, "Daily kWh"))
        _validate_optional_money(self.cost, "Daily cost")
        _validate_optional_money(self.unit_rate, "Unit rate")
        _validate_optional_money(self.standing_charge, "Standing charge")
        if not isinstance(self.has_half_hourly, bool):
            raise DomainError("has_half_hourly must be a bool")


@dataclass(frozen=True, slots=True)
class MonthlyUsage:
    account_id: str
    fuel: Fuel
    month: date  # always the 1st of the month
    kwh: float
    cost: MilliPence | None
    mpxn: str | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.account_id, "account id")
        if not isinstance(self.fuel, Fuel):
            raise DomainError(f"fuel must be a Fuel, not {type(self.fuel).__name__}")
        if _validate_date(self.month, "month").day != 1:
            raise DomainError(f"month must be the 1st of the month: {self.month}")
        object.__setattr__(self, "kwh", _validate_kwh(self.kwh, MAX_KWH_PER_MONTH, "Monthly kWh"))
        _validate_optional_money(self.cost, "Monthly cost")
        if self.mpxn is not None and (
            not isinstance(self.mpxn, str) or not _MPXN.fullmatch(self.mpxn)
        ):
            raise DomainError(f"Invalid MPAN/MPRN: {self.mpxn!r}")


@dataclass(frozen=True, slots=True)
class Meter:
    serial: str
    account_id: str
    fuel: Fuel
    meter_type: str | None = None
    status: str | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.serial, "meter serial")
        validate_identifier(self.account_id, "account id")
        if not isinstance(self.fuel, Fuel):
            raise DomainError(f"fuel must be a Fuel, not {type(self.fuel).__name__}")
        _validate_optional_text(self.meter_type, "meter type")
        _validate_optional_text(self.status, "meter status")


@dataclass(frozen=True, slots=True)
class Register:
    meter_serial: str
    register_id: str
    start_date: date
    end_date: date | None = None
    timing_category: str | None = None
    unit: str | None = None
    digits: int | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.meter_serial, "meter serial")
        if self.register_id != GAS_REGISTER_ID:
            validate_identifier(self.register_id, "register id")
        _validate_date_range(self.start_date, self.end_date, "Register")
        _validate_optional_text(self.timing_category, "timing category")
        _validate_optional_text(self.unit, "unit")
        if self.digits is not None and (
            isinstance(self.digits, bool)
            or not isinstance(self.digits, int)
            or not 1 <= self.digits <= 20
        ):
            raise DomainError(f"Invalid register digit count: {self.digits!r}")


@dataclass(frozen=True, slots=True)
class Reading:
    meter_serial: str
    register_id: str  # GAS_REGISTER_ID for gas
    read_date: date
    value: Decimal
    source: str | None = None
    lifecycle: str | None = None
    read_type: str | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.meter_serial, "meter serial")
        if self.register_id != GAS_REGISTER_ID:
            validate_identifier(self.register_id, "register id")
        _validate_date(self.read_date, "read date")
        if not isinstance(self.value, Decimal) or not _READING_VALUE.fullmatch(_plain(self.value)):
            raise DomainError(f"Invalid meter reading value: {self.value!r}")
        _validate_optional_text(self.source, "reading source")
        _validate_optional_text(self.lifecycle, "reading lifecycle")
        _validate_optional_text(self.read_type, "reading type")


def _plain(value: Decimal) -> str:
    return format(value, "f") if value.is_finite() else str(value)

"""Turn validated OVO responses into domain objects.

Anything unexpected (a timestamp without a timezone, a unit other than kWh, an unknown fuel,
an invalid identifier) raises `UpstreamSchemaChanged` with a description that contains no
personal data. Nothing is guessed or coerced.
"""

import functools
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

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
    validate_identifier,
)
from ovo_insights.money import MilliPence, MoneyError, parse_pounds
from ovo_insights.ovo import models
from ovo_insights.ovo.errors import UpstreamSchemaChanged
from ovo_insights.timeutil import london_date

log = logging.getLogger(__name__)

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
# ISO-8601 with a mandatory zone designator.
_AWARE_INSTANT = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:\d{2})", re.ASCII
)
# Real responses carry naive timestamps such as "2026-10-11T00:00:00.000".
_NAIVE_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?", re.ASCII)
_NAIVE_MIDNIGHT = re.compile(r"(\d{4}-\d{2}-\d{2})T00:00(?::00(?:\.0{1,9})?)?", re.ASCII)
# Real estimated reads carry up to 8 decimal places.
_READING = re.compile(r"\d{1,15}(?:\.\d{1,10})?", re.ASCII)
_KWH_UNITS = frozenset({"kwh"})


@dataclass(frozen=True, slots=True)
class AccountSummary:
    account_id: str
    supply_points: tuple[SupplyPoint, ...]


@dataclass(frozen=True, slots=True)
class MeterData:
    supply_points: tuple[SupplyPoint, ...]
    meters: tuple[Meter, ...]
    registers: tuple[Register, ...]
    readings: tuple[Reading, ...]
    skipped_registers: int = 0


def _reports_invalid_values[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Report domain validation failures as schema changes, without echoing the values."""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except (DomainError, ValueError, TypeError) as exc:
            raise UpstreamSchemaChanged(
                f"{func.__name__}: value failed validation ({type(exc).__name__})"
            ) from exc

    return wrapper


def parse_instant(text: str, what: str, *, naive_is_utc: bool = False) -> datetime:
    """Parse an ISO timestamp to UTC.

    Timestamps with a zone designator are always accepted. Naive ones are accepted only when
    the caller knows the endpoint sends UTC (`naive_is_utc`); otherwise they are rejected
    rather than guessed.
    """
    if not isinstance(text, str):
        raise UpstreamSchemaChanged(f"{what}: expected a timestamp string")
    aware = bool(_AWARE_INSTANT.fullmatch(text))
    if not aware and not (naive_is_utc and _NAIVE_INSTANT.fullmatch(text)):
        raise UpstreamSchemaChanged(f"{what}: expected an ISO timestamp with a timezone")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise UpstreamSchemaChanged(f"{what}: invalid timestamp") from exc
    return parsed.astimezone(UTC) if aware else parsed.replace(tzinfo=UTC)


def calendar_day(text: str, what: str) -> date:
    """The date an OVO daily interval refers to.

    Real responses label each day with a naive midnight ("2026-09-01T00:00:00.000"): a
    calendar date, not an instant. Aware timestamps are mapped to the UK date.
    """
    match = _NAIVE_MIDNIGHT.fullmatch(text) if isinstance(text, str) else None
    if match:
        try:
            return date.fromisoformat(match.group(1))
        except ValueError as exc:
            raise UpstreamSchemaChanged(f"{what}: invalid date") from exc
    return london_date(parse_instant(text, what))


def parse_day(text: str, what: str) -> date:
    """A calendar date given either as YYYY-MM-DD or as an aware timestamp (UK date taken)."""
    if isinstance(text, str) and _DATE.fullmatch(text):
        try:
            return date.fromisoformat(text)
        except ValueError as exc:
            raise UpstreamSchemaChanged(f"{what}: invalid date") from exc
    return london_date(parse_instant(text, what))


@_reports_invalid_values
def half_hours(
    account_id: str, response: models.HalfHourResponse, *, naive_is_utc: bool = False
) -> list[HalfHourUsage]:
    result = []
    for fuel, block in _fuels(response.electricity, response.gas):
        for item in _present(block.data):
            if item.unit is not None and item.unit.strip().lower() not in _KWH_UNITS:
                raise UpstreamSchemaChanged(
                    f"{fuel.value} half-hourly data is in {item.unit!r}, not kWh; "
                    "unit conversion isn't supported yet"
                )
            start = parse_instant(item.interval.start, "half-hour start", naive_is_utc=naive_is_utc)
            result.append(HalfHourUsage(account_id, fuel, start, _kwh(item.consumption)))
    return result


@_reports_invalid_values
def daily(account_id: str, response: models.DailyResponse) -> list[DailyUsage]:
    result = []
    for fuel, block in _fuels(response.electricity, response.gas):
        for item in _present(block.data):
            day = calendar_day(item.interval.start, "daily interval start")
            has_hh = item.has_hh_data if item.has_hh_data is not None else item.has_half_hour_data
            rates = item.rates
            result.append(
                DailyUsage(
                    account_id,
                    fuel,
                    day,
                    _kwh(item.consumption),
                    _cost(item.cost),
                    bool(has_hh),
                    unit_rate=_rate(rates.anytime if rates else None, "unit rate"),
                    standing_charge=_rate(rates.standing if rates else None, "standing charge"),
                )
            )
    return result


@_reports_invalid_values
def monthly(account_id: str, response: models.MonthlyResponse) -> list[MonthlyUsage]:
    result = []
    for fuel, block in _fuels(response.electricity, response.gas):
        for item in _present(block.data):
            month = date(item.year, item.month, 1)
            cost = _cost(item.cost)
            mpxn = item.mpxn or None
            result.append(MonthlyUsage(account_id, fuel, month, _kwh(item.consumption), cost, mpxn))
    return result


@_reports_invalid_values
def accounts(response: models.BootstrapResponse) -> list[AccountSummary]:
    data = _graphql_data(response.data, response.errors, "accounts")
    result = []
    for edge in data.customer.customer_account_relationships.edges:
        account = edge.node.account
        points = []
        for asp in account.account_supply_points:
            fuel = _fuel(asp.supply_point.fuel_type)
            start = parse_day(asp.start_date, "supply start date")
            sprn = asp.supply_point.sprn
            if sprn:
                points.append(SupplyPoint(account.id, fuel, sprn, start))
        result.append(AccountSummary((_account_id(account.id)), tuple(points)))
    return result


@_reports_invalid_values
def meter_data(account_id: str, response: models.ReadsResponse) -> MeterData:
    data = _graphql_data(response.data, response.errors, "meter reads")
    points: list[SupplyPoint] = []
    meters: dict[str, Meter] = {}
    registers: list[Register] = []
    readings: dict[tuple[str, str, date], Reading] = {}
    skipped_registers = 0

    for asp in data.account.account_supply_points:
        sp = asp.supply_point
        fuel = _fuel(sp.fuel_type)
        start = parse_day(asp.start_date, "supply start date")
        end = parse_day(asp.end.date, "supply end date") if asp.end and asp.end.date else None
        if sp.sprn:
            sprn = sp.sprn
            points.append(SupplyPoint(account_id, fuel, sprn, start, end))

        for detail in sp.meter_technical_details or []:
            serial = detail.meter_serial_number
            meters[serial] = Meter(serial, account_id, fuel, detail.type, detail.status)
            for reg in detail.registers or []:
                if not reg.register_start_date:
                    # Seen on an old meter with status "Unknown". A register can't be stored
                    # without a start date, and it carries no readings we need.
                    skipped_registers += 1
                    continue
                registers.append(_register(serial, fuel, reg))

        for edge in asp.meter_reads.edges if asp.meter_reads else []:
            read = edge.node.reading
            read_date = parse_day(read.date, "meter read date")
            if fuel is Fuel.GAS:
                values = [(GAS_REGISTER_ID, read.value)]
            else:
                values = [(r.register_id, r.value) for r in read.registers or []]
            for register_id, raw in values:
                if raw is None:
                    raise UpstreamSchemaChanged("meter read has no value")
                value = _reading_value(raw)
                serial = read.meter_serial_number
                reading = Reading(
                    serial, register_id, read_date, value, read.source, read.lifecycle, read.type
                )
                key = (reading.meter_serial, reading.register_id, reading.read_date)
                existing = readings.get(key)
                if existing is not None and existing.value != reading.value:
                    raise UpstreamSchemaChanged("two different readings for one register and day")
                readings[key] = reading

    if skipped_registers:
        # Expected for some old meters (seen in real data), so informational, not a warning.
        # Callers get the count in MeterData.skipped_registers.
        log.info("Skipped %d meter register(s) with no start date", skipped_registers)
    return MeterData(
        tuple(points),
        tuple(meters.values()),
        tuple(registers),
        tuple(readings.values()),
        skipped_registers,
    )


# -- helpers ------------------------------------------------------------------------------


def _fuels[T](electricity: T | None, gas: T | None) -> list[tuple[Fuel, T]]:
    return [(f, b) for f, b in ((Fuel.ELECTRICITY, electricity), (Fuel.GAS, gas)) if b is not None]


def _present[T](items: list[T | None] | None) -> list[T]:
    return [item for item in items or [] if item is not None]


def _kwh(value: Decimal) -> float:
    if value < 0:
        raise UpstreamSchemaChanged("negative consumption")
    return float(value)


def _cost(cost: models.Cost | None) -> MilliPence | None:
    if cost is None:
        return None
    if cost.currency_unit is not None and cost.currency_unit.upper() != "GBP":
        raise UpstreamSchemaChanged(f"cost in unexpected currency {cost.currency_unit!r}")
    amount = cost.amount
    if isinstance(amount, str) and not amount.strip():
        return None
    try:
        return parse_pounds(amount if isinstance(amount, str) else Decimal(amount))
    except MoneyError as exc:
        raise UpstreamSchemaChanged(f"invalid cost amount: {exc}") from exc


def _fuel(text: str) -> Fuel:
    try:
        return Fuel.parse(text)
    except DomainError as exc:
        raise UpstreamSchemaChanged(str(exc)) from exc


def _account_id(value: str) -> str:
    return validate_identifier(value, "account id")


@_reports_invalid_values
def _register(serial: str, fuel: Fuel, reg: models.RegisterDetail) -> Register:
    digits: int | None = None
    if reg.number_of_digits not in (None, ""):
        raw = str(reg.number_of_digits)
        if not (raw.isascii() and raw.isdigit()):
            raise UpstreamSchemaChanged("register digit count is not a number")
        digits = int(raw)
    if not reg.register_start_date:  # pragma: no cover - callers skip these first
        raise UpstreamSchemaChanged("register has no start date")
    start = parse_day(reg.register_start_date, "register start date")
    end = parse_day(reg.register_end_date, "register end date") if reg.register_end_date else None
    register_id = GAS_REGISTER_ID if fuel is Fuel.GAS else reg.register_id
    return Register(
        serial, register_id, start, end, reg.timing_category, reg.unit_measurement, digits
    )


def _reading_value(raw: str | Decimal) -> Decimal:
    text = raw.strip() if isinstance(raw, str) else format(raw, "f")
    if not _READING.fullmatch(text):
        raise UpstreamSchemaChanged("meter read value is not a plain non-negative number")
    return Decimal(text)


def _rate(value: Decimal | None, what: str) -> MilliPence | None:
    """A rate in pounds (per kWh or per day) as milli-pence. Confirmed against real data:
    daily cost == kWh x rates.anytime, rounded to the penny; standing is extra."""
    if value is None:
        return None
    try:
        return parse_pounds(value)
    except MoneyError as exc:
        raise UpstreamSchemaChanged(f"invalid {what}: {exc}") from exc


def _graphql_data[T](data: T | None, errors: list[models.GraphQLError] | None, what: str) -> T:
    if errors:
        # GraphQL error messages can echo ids; report only how many there were.
        raise UpstreamSchemaChanged(f"{what} query returned {len(errors)} GraphQL error(s)")
    if data is None:
        raise UpstreamSchemaChanged(f"{what} query returned no data")
    return data

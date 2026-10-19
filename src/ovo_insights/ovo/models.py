"""Pydantic models of OVO's (undocumented) responses.

Shapes come from My-Ovo-Data v1.0.9 (Dec 2025) and timmo001/ovoenergy, and are confirmed or
corrected by `ovo-insights doctor` against a real account. Models accept unknown fields but
record them (`unknown_fields`), so schema drift produces a warning instead of silence or a
crash. Missing *required* fields fail validation, which the client reports as
`UpstreamSchemaChanged`.

Numbers arrive as `Decimal` (the client parses JSON with `parse_float=Decimal`).
"""

from decimal import Decimal
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _number(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        raise ValueError(f"expected a JSON number, got {type(value).__name__}")
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError("number is not finite")
    return Decimal(value)


Number = Annotated[Decimal, BeforeValidator(_number)]


class OvoModel(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True, strict=True, populate_by_name=True)

    # Fields OVO is known to send that we deliberately don't use. They don't count as drift.
    ignored_fields: ClassVar[frozenset[str]] = frozenset()


# -- auth ---------------------------------------------------------------------------------


class LoginResponse(OvoModel):
    ignored_fields = frozenset({"authenticationPlatform", "identifiers", "roles", "username"})

    user_id: str | None = Field(default=None, alias="userId")
    code: str | None = None


class AccessTokenValue(OvoModel):
    value: str


class TokenResponse(OvoModel):
    # Lifetimes are read from the JWT's own `exp` claim instead.
    ignored_fields = frozenset({"expiresIn", "refreshExpiresIn"})

    access_token: AccessTokenValue = Field(alias="accessToken")


# -- usage --------------------------------------------------------------------------------


class Interval(OvoModel):
    start: str
    end: str | None = None


class Cost(OvoModel):
    amount: str | Number
    currency_unit: str | None = Field(default=None, alias="currencyUnit")


class HalfHourItem(OvoModel):
    consumption: Number
    interval: Interval
    unit: str | None = None


class Rates(OvoModel):
    """Pounds per kWh (`anytime`) and pounds per day (`standing`), confirmed against real data."""

    anytime: Number | None = None
    standing: Number | None = None


class DailyItem(OvoModel):
    consumption: Number
    interval: Interval
    cost: Cost | None = None
    has_hh_data: bool | None = Field(default=None, alias="hasHhData")
    has_half_hour_data: bool | None = Field(default=None, alias="hasHalfHourData")
    rates: Rates | None = None


class MonthlyItem(OvoModel):
    year: int
    month: int = Field(ge=1, le=12)
    consumption: Number
    cost: Cost | None = None
    mpxn: str | None = None


_PAGING = frozenset({"prev", "next", "prevYear"})


class HalfHourFuel(OvoModel):
    ignored_fields = _PAGING

    data: list[HalfHourItem | None] | None = None


class DailyFuel(OvoModel):
    ignored_fields = _PAGING

    data: list[DailyItem | None] | None = None


class MonthlyFuel(OvoModel):
    ignored_fields = _PAGING

    data: list[MonthlyItem | None] | None = None


class HalfHourResponse(OvoModel):
    electricity: HalfHourFuel | None = None
    gas: HalfHourFuel | None = None


class DailyResponse(OvoModel):
    electricity: DailyFuel | None = None
    gas: DailyFuel | None = None


class MonthlyResponse(OvoModel):
    electricity: MonthlyFuel | None = None
    gas: MonthlyFuel | None = None


# -- GraphQL: accounts --------------------------------------------------------------------


class GraphQLError(OvoModel):
    message: str | None = None


class SupplyPointDetail(OvoModel):
    fuel_type: str = Field(alias="fuelType")
    sprn: str | None = None


class AccountSupplyPoint(OvoModel):
    start_date: str = Field(alias="startDate")
    supply_point: SupplyPointDetail = Field(alias="supplyPoint")


class Account(OvoModel):
    id: str
    account_supply_points: list[AccountSupplyPoint] = Field(alias="accountSupplyPoints")


class AccountNode(OvoModel):
    account: Account


class AccountEdge(OvoModel):
    node: AccountNode


class AccountEdges(OvoModel):
    edges: list[AccountEdge]


class Customer(OvoModel):
    ignored_fields = frozenset({"id"})

    customer_account_relationships: AccountEdges = Field(alias="customerAccountRelationships")


class BootstrapData(OvoModel):
    customer: Customer = Field(alias="customer_nextV1")


class BootstrapResponse(OvoModel):
    data: BootstrapData | None = None
    errors: list[GraphQLError] | None = None


# -- GraphQL: meter reads -----------------------------------------------------------------


class RegisterDetail(OvoModel):
    register_id: str = Field(alias="registerId")
    timing_category: str | None = Field(default=None, alias="timingCategory")
    number_of_digits: str | int | None = Field(default=None, alias="numberOfDigits")
    unit_measurement: str | None = Field(default=None, alias="unitMeasurement")
    register_start_date: str | None = Field(default=None, alias="registerStartDate")
    register_end_date: str | None = Field(default=None, alias="registerEndDate")


class MeterTechnicalDetail(OvoModel):
    meter_serial_number: str = Field(alias="meterSerialNumber")
    type: str | None = None
    status: str | None = None
    registers: list[RegisterDetail] | None = None


class ReadsSupplyPoint(OvoModel):
    fuel_type: str = Field(alias="fuelType")
    sprn: str | None = None
    meter_technical_details: list[MeterTechnicalDetail] | None = Field(
        default=None, alias="meterTechnicalDetails"
    )


class ReadRegister(OvoModel):
    register_id: str = Field(alias="registerId")
    timing_category: str | None = Field(default=None, alias="timingCategory")
    value: str | Number


class MeterRead(OvoModel):
    date: str
    meter_serial_number: str = Field(alias="meterSerialNumber")
    type: str | None = None
    lifecycle: str | None = None
    source: str | None = None
    value: str | Number | None = None  # gas
    registers: list[ReadRegister] | None = None  # electricity


class ReadNode(OvoModel):
    reading: MeterRead


class ReadEdge(OvoModel):
    node: ReadNode


class ReadEdges(OvoModel):
    edges: list[ReadEdge]


class SupplyEnd(OvoModel):
    date: str | None = None


class ReadsAccountSupplyPoint(OvoModel):
    start_date: str = Field(alias="startDate")
    end: SupplyEnd | None = None
    supply_point: ReadsSupplyPoint = Field(alias="supplyPoint")
    meter_reads: ReadEdges | None = Field(default=None, alias="meterReads_nextV1")


class ReadsAccount(OvoModel):
    id: str
    account_supply_points: list[ReadsAccountSupplyPoint] = Field(alias="accountSupplyPoints")


class ReadsData(OvoModel):
    account: ReadsAccount


class ReadsResponse(OvoModel):
    data: ReadsData | None = None
    errors: list[GraphQLError] | None = None


# -- drift detection ----------------------------------------------------------------------


def unknown_fields(model: BaseModel, prefix: str = "") -> set[str]:
    """Dotted paths of every field OVO sent that our models don't declare."""
    found: set[str] = set()
    ignored: frozenset[str] = getattr(type(model), "ignored_fields", frozenset())
    for name in model.model_extra or {}:
        if name not in ignored:
            found.add(f"{prefix}{name}")
    for name in type(model).model_fields:
        found |= _unknown_in(getattr(model, name), f"{prefix}{name}.")
    return found


def _unknown_in(value: Any, prefix: str) -> set[str]:
    if isinstance(value, BaseModel):
        return unknown_fields(value, prefix)
    if isinstance(value, list):
        found: set[str] = set()
        for item in value:
            found |= _unknown_in(item, f"{prefix.rstrip('.')}[].")
        return found
    return set()

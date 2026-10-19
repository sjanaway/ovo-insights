import copy
import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from ovo_insights.domain import GAS_REGISTER_ID, Fuel
from ovo_insights.ovo import convert, models
from ovo_insights.ovo.errors import UpstreamSchemaChanged
from tests.ovo.conftest import fixture

ACCOUNT = "7654321"


def as_decimals(node: Any) -> Any:
    """Mimic the client's json.loads(parse_float=Decimal)."""
    if isinstance(node, dict):
        return {k: as_decimals(v) for k, v in node.items()}
    if isinstance(node, list):
        return [as_decimals(v) for v in node]
    if isinstance(node, float):
        return Decimal(repr(node))
    return node


def load[M: models.OvoModel](model: type[M], name: str, mutate: Any = None) -> M:
    payload = as_decimals(fixture(name))
    if mutate is not None:
        payload = copy.deepcopy(payload)
        mutate(payload)
    return model.model_validate(payload)


def test_half_hours_from_fixture() -> None:
    rows = convert.half_hours(ACCOUNT, load(models.HalfHourResponse, "half_hourly"))

    assert [(r.fuel, r.interval_start, r.kwh) for r in rows] == [
        (Fuel.ELECTRICITY, datetime(2025, 1, 1, 0, 0, tzinfo=UTC), 0.5),
        (Fuel.ELECTRICITY, datetime(2025, 1, 1, 0, 30, tzinfo=UTC), 0.25),
        (Fuel.GAS, datetime(2025, 1, 1, 0, 0, tzinfo=UTC), 0.2),
    ]


def test_half_hours_reject_naive_timestamps() -> None:
    def naive(p: Any) -> None:
        p["electricity"]["data"][0]["interval"]["start"] = "2025-01-01T00:00:00"

    with pytest.raises(UpstreamSchemaChanged, match="timezone"):
        convert.half_hours(ACCOUNT, load(models.HalfHourResponse, "half_hourly", naive))


def test_half_hours_reject_non_kwh_units() -> None:
    def cubic_metres(p: Any) -> None:
        p["gas"]["data"][0]["unit"] = "m³"

    with pytest.raises(UpstreamSchemaChanged, match="not kWh"):
        convert.half_hours(ACCOUNT, load(models.HalfHourResponse, "half_hourly", cubic_metres))


def test_half_hours_reject_negative_and_misaligned() -> None:
    def negative(p: Any) -> None:
        p["electricity"]["data"][0]["consumption"] = Decimal("-1")

    def misaligned(p: Any) -> None:
        p["electricity"]["data"][0]["interval"]["start"] = "2025-01-01T00:10:00Z"

    with pytest.raises(UpstreamSchemaChanged, match="negative"):
        convert.half_hours(ACCOUNT, load(models.HalfHourResponse, "half_hourly", negative))
    with pytest.raises(UpstreamSchemaChanged, match="validation"):
        convert.half_hours(ACCOUNT, load(models.HalfHourResponse, "half_hourly", misaligned))


def test_null_items_and_missing_fuels_are_skipped() -> None:
    response = models.HalfHourResponse.model_validate(
        {"electricity": {"data": [None]}, "gas": None}
    )

    assert convert.half_hours(ACCOUNT, response) == []


def test_daily_from_fixture_uses_uk_dates() -> None:
    rows = convert.daily(ACCOUNT, load(models.DailyResponse, "daily"))

    elec = [r for r in rows if r.fuel is Fuel.ELECTRICITY]
    assert [r.day for r in elec] == [date(2025, 1, 1), date(2025, 7, 2)]  # 23:00Z in BST
    assert elec[0].cost == 294_000
    assert elec[0].has_half_hourly is True
    assert elec[1].cost is None
    gas = [r for r in rows if r.fuel is Fuel.GAS]
    assert gas[0].has_half_hourly is False


@pytest.mark.parametrize(
    ("cost", "expected"),
    [({"amount": "", "currencyUnit": "GBP"}, None), ({"amount": Decimal("1.5")}, 150_000)],
)
def test_daily_cost_variants(cost: dict[str, Any], expected: int | None) -> None:
    def set_cost(p: Any) -> None:
        p["electricity"]["data"][0]["cost"] = cost

    rows = convert.daily(ACCOUNT, load(models.DailyResponse, "daily", set_cost))

    assert rows[0].cost == expected


@pytest.mark.parametrize(
    ("cost", "match"),
    [
        ({"amount": "1.00", "currencyUnit": "EUR"}, "currency"),
        ({"amount": "£1.00"}, "invalid cost"),
        ({"amount": "-1.00"}, "invalid cost"),
    ],
)
def test_daily_bad_costs(cost: dict[str, Any], match: str) -> None:
    def set_cost(p: Any) -> None:
        p["electricity"]["data"][0]["cost"] = cost

    with pytest.raises(UpstreamSchemaChanged, match=match):
        convert.daily(ACCOUNT, load(models.DailyResponse, "daily", set_cost))


def test_daily_accepts_alternative_half_hour_flag() -> None:
    def alt(p: Any) -> None:
        item = p["electricity"]["data"][0]
        del item["hasHhData"]
        item["hasHalfHourData"] = True

    rows = convert.daily(ACCOUNT, load(models.DailyResponse, "daily", alt))
    assert rows[0].has_half_hourly is True


def test_monthly_from_fixture() -> None:
    rows = convert.monthly(ACCOUNT, load(models.MonthlyResponse, "monthly"))

    assert [(r.fuel, r.month, r.cost) for r in rows] == [
        (Fuel.ELECTRICITY, date(2025, 1, 1), 9_876_000),
        (Fuel.ELECTRICITY, date(2025, 2, 1), 8_810_000),
        (Fuel.GAS, date(2025, 1, 1), None),
    ]
    assert rows[0].mpxn == "1900000000000"


def test_monthly_rejects_bad_month() -> None:
    def bad(p: Any) -> None:
        p["electricity"]["data"][0]["month"] = 13

    with pytest.raises(Exception, match="month"):
        load(models.MonthlyResponse, "monthly", bad)


def test_accounts_from_fixture() -> None:
    [account] = convert.accounts(load(models.BootstrapResponse, "bootstrap"))

    assert account.account_id == ACCOUNT
    assert {(p.fuel, p.mpxn, p.start_date) for p in account.supply_points} == {
        (Fuel.ELECTRICITY, "1900000000000", date(2022, 3, 15)),
        (Fuel.GAS, "9876543", date(2022, 3, 15)),
    }


def test_accounts_reject_unknown_fuel() -> None:
    def water(p: Any) -> None:
        edges = p["data"]["customer_nextV1"]["customerAccountRelationships"]["edges"]
        edges[0]["node"]["account"]["accountSupplyPoints"][0]["supplyPoint"]["fuelType"] = "WATER"

    with pytest.raises(UpstreamSchemaChanged, match="Unknown fuel"):
        convert.accounts(load(models.BootstrapResponse, "bootstrap", water))


def test_accounts_reject_bad_account_id() -> None:
    def bad_id(p: Any) -> None:
        edges = p["data"]["customer_nextV1"]["customerAccountRelationships"]["edges"]
        edges[0]["node"]["account"]["id"] = "../../x"
        edges[0]["node"]["account"]["accountSupplyPoints"] = []

    with pytest.raises(UpstreamSchemaChanged, match="validation"):
        convert.accounts(load(models.BootstrapResponse, "bootstrap", bad_id))


@pytest.mark.parametrize(
    "payload",
    [{"errors": [{"message": "secret account 123 not found"}]}, {"data": None}],
)
def test_graphql_errors_and_missing_data(payload: dict[str, Any]) -> None:
    with pytest.raises(UpstreamSchemaChanged) as exc_info:
        convert.accounts(models.BootstrapResponse.model_validate(payload))
    assert "123" not in str(exc_info.value)


def test_meter_data_from_fixture() -> None:
    data = convert.meter_data(ACCOUNT, load(models.ReadsResponse, "meter_reads"))

    assert {m.serial for m in data.meters} == {"23E0000000", "G4A0000000"}
    assert {(r.meter_serial, r.register_id) for r in data.registers} == {
        ("23E0000000", "1"),
        ("23E0000000", "2"),
        ("G4A0000000", GAS_REGISTER_ID),
    }
    values = {(r.meter_serial, r.register_id): r.value for r in data.readings}
    assert values == {
        ("23E0000000", "1"): Decimal("012345.6"),
        ("23E0000000", "2"): Decimal("5432.1"),
        ("G4A0000000", GAS_REGISTER_ID): Decimal("789.123"),
    }
    assert all(r.read_date == date(2025, 1, 1) for r in data.readings)
    assert len(data.supply_points) == 2


@pytest.mark.parametrize("value", ["-1", "1e5", "12.3.4", "", None])
def test_meter_data_rejects_bad_reading_values(value: object) -> None:
    def bad(p: Any) -> None:
        reads = p["data"]["account"]["accountSupplyPoints"][1]["meterReads_nextV1"]["edges"]
        reads[0]["node"]["reading"]["value"] = value

    with pytest.raises(UpstreamSchemaChanged):
        convert.meter_data(ACCOUNT, load(models.ReadsResponse, "meter_reads", bad))


def test_meter_data_rejects_bad_register_digits_and_missing_start() -> None:
    def digits(p: Any) -> None:
        sp = p["data"]["account"]["accountSupplyPoints"][0]["supplyPoint"]
        sp["meterTechnicalDetails"][0]["registers"][0]["numberOfDigits"] = "six"

    def no_start(p: Any) -> None:
        sp = p["data"]["account"]["accountSupplyPoints"][0]["supplyPoint"]
        sp["meterTechnicalDetails"][0]["registers"][0]["registerStartDate"] = None

    with pytest.raises(UpstreamSchemaChanged, match="digit"):
        convert.meter_data(ACCOUNT, load(models.ReadsResponse, "meter_reads", digits))
    # Seen in real data on an old meter: undated registers are skipped, not fatal.
    data = convert.meter_data(ACCOUNT, load(models.ReadsResponse, "meter_reads", no_start))
    assert data.skipped_registers == 1
    assert len(data.registers) == 2


def test_meter_data_handles_supply_end_and_integer_values() -> None:
    def tweak(p: Any) -> None:
        asp = p["data"]["account"]["accountSupplyPoints"][1]
        asp["end"] = {"date": "2025-06-01"}
        asp["meterReads_nextV1"]["edges"][0]["node"]["reading"]["value"] = 789

    data = convert.meter_data(ACCOUNT, load(models.ReadsResponse, "meter_reads", tweak))

    gas = next(p for p in data.supply_points if p.fuel is Fuel.GAS)
    assert gas.end_date == date(2025, 6, 1)
    assert Decimal(789) in {r.value for r in data.readings}


@pytest.mark.parametrize(
    "text",
    ["2025-01-01T00:00:00", "2025-01-01", "01/01/2025", "2025-13-01T00:00:00Z", "", 5],
)
def test_parse_instant_is_strict(text: object) -> None:
    with pytest.raises(UpstreamSchemaChanged):
        convert.parse_instant(text, "x")  # type: ignore[arg-type]


def test_parse_instant_accepts_offsets_and_fractions() -> None:
    assert convert.parse_instant("2025-07-01T00:00:00.123+01:00", "x") == datetime(
        2025, 6, 30, 23, 0, 0, 123000, tzinfo=UTC
    )


def test_parse_day_forms() -> None:
    assert convert.parse_day("2025-07-01", "x") == date(2025, 7, 1)
    assert convert.parse_day("2025-06-30T23:00:00Z", "x") == date(2025, 7, 1)
    with pytest.raises(UpstreamSchemaChanged):
        convert.parse_day("2025-02-30", "x")


def test_strict_models_reject_strings_for_numbers_and_bools() -> None:
    with pytest.raises(Exception, match="JSON number"):
        models.HalfHourItem.model_validate({"consumption": "1", "interval": {"start": "x"}})
    with pytest.raises(Exception, match="JSON number"):
        models.HalfHourItem.model_validate({"consumption": True, "interval": {"start": "x"}})
    with pytest.raises(Exception, match="finite"):
        models.HalfHourItem.model_validate(
            {"consumption": Decimal("NaN"), "interval": {"start": "x"}}
        )


def test_unknown_fields_are_reported_with_paths() -> None:
    parsed = load(models.ReadsResponse, "meter_reads")

    unknown = models.unknown_fields(parsed)

    assert "data.account.account_supply_points[].supply_point.region" in unknown
    assert "data.account.account_supply_points[].supply_point.timezone" in unknown


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-09-01T00:00:00.000", date(2026, 9, 1)),
        ("2026-09-01T00:00", date(2026, 9, 1)),
        ("2026-06-30T23:00:00Z", date(2026, 7, 1)),
    ],
)
def test_calendar_day(text: str, expected: date) -> None:
    assert convert.calendar_day(text, "x") == expected


@pytest.mark.parametrize("text", ["2026-02-30T00:00:00.000", "2026-09-01T01:00:00.000", 7])
def test_calendar_day_rejects_bad_labels(text: object) -> None:
    with pytest.raises(UpstreamSchemaChanged):
        convert.calendar_day(text, "x")  # type: ignore[arg-type]


def test_naive_instants_need_explicit_utc() -> None:
    assert convert.parse_instant("2026-10-11T00:00:00.000", "x", naive_is_utc=True) == datetime(
        2026, 10, 11, tzinfo=UTC
    )
    with pytest.raises(UpstreamSchemaChanged):
        convert.parse_instant("2026-10-11T25:00:00.000", "x", naive_is_utc=True)
    with pytest.raises(UpstreamSchemaChanged):
        convert.parse_instant("2026-10-11", "x", naive_is_utc=True)


def test_daily_rates_are_parsed_and_validated() -> None:
    rows = convert.daily(ACCOUNT, load(models.DailyResponse, "daily"))
    assert rows[0].unit_rate == 25_000  # £0.25/kWh
    assert rows[0].standing_charge == 45_000  # £0.45/day

    def negative(p: Any) -> None:
        p["electricity"]["data"][0]["rates"]["standing"] = Decimal("-0.1")

    with pytest.raises(UpstreamSchemaChanged, match="standing charge"):
        convert.daily(ACCOUNT, load(models.DailyResponse, "daily", negative))


def test_conflicting_duplicate_readings_are_rejected() -> None:
    def duplicate(p: Any) -> None:
        edges = p["data"]["account"]["accountSupplyPoints"][1]["meterReads_nextV1"]["edges"]
        twin = copy.deepcopy(edges[0])
        twin["node"]["reading"]["value"] = "790.000"
        edges.append(twin)

    with pytest.raises(UpstreamSchemaChanged, match="two different readings"):
        convert.meter_data(ACCOUNT, load(models.ReadsResponse, "meter_reads", duplicate))


def test_identical_duplicate_readings_are_merged() -> None:
    def duplicate(p: Any) -> None:
        edges = p["data"]["account"]["accountSupplyPoints"][1]["meterReads_nextV1"]["edges"]
        edges.append(copy.deepcopy(edges[0]))

    data = convert.meter_data(ACCOUNT, load(models.ReadsResponse, "meter_reads", duplicate))
    assert len(data.readings) == 3


def test_skipped_registers_log_at_info_not_warning(caplog: pytest.LogCaptureFixture) -> None:
    def no_start(p: Any) -> None:
        sp = p["data"]["account"]["accountSupplyPoints"][0]["supplyPoint"]
        sp["meterTechnicalDetails"][0]["registers"][0]["registerStartDate"] = None

    with caplog.at_level(logging.INFO, logger="ovo_insights.ovo.convert"):
        convert.meter_data(ACCOUNT, load(models.ReadsResponse, "meter_reads", no_start))

    [record] = [r for r in caplog.records if "no start date" in r.getMessage()]
    assert record.levelno == logging.INFO

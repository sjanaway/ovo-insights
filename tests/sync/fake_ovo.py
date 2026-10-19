"""A fake OVO for sync tests: serves consistent synthetic data for any date via respx.

Shapes follow the captured (redacted) real responses: naive UTC half-hour timestamps covering
exactly one UK day, naive-midnight daily intervals, money as strings, `hasHhData` true for
electricity and false for gas, and monthly = sum of daily = sum of half-hours. All ids are
fake (13-digit numbers fail the MPAN check digit).
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx
import respx

from ovo_insights.timeutil import london_date, london_half_hours
from tests.ovo.conftest import GRAPHQL_URL, mock_login

USAGE_HOST = "smartpaymapi.ovoenergy.com"
_USAGE_PATH = re.compile(r"/usage/api/(monthly|daily|half-hourly-local)/([A-Za-z0-9]+)", re.ASCII)
ELEC_MPAN = "1900000000000"
GAS_MPRN = "9876543"
UNIT_RATE = Decimal("0.25")


@dataclass
class FakeSupply:
    fuel: str  # "ELECTRICITY" or "GAS"
    mpxn: str
    start: date
    end: date | None = None
    end_in_reads: bool = True  # whether meter reads report the end date


@dataclass
class FakeAccount:
    account_id: str
    supplies: list[FakeSupply]


def dual_fuel(account_id: str, start: date, end: date | None = None) -> FakeAccount:
    return FakeAccount(
        account_id,
        [
            FakeSupply("ELECTRICITY", ELEC_MPAN, start, end),
            FakeSupply("GAS", GAS_MPRN, start, end),
        ],
    )


Interceptor = Callable[[str, str, str], httpx.Response | None]


@dataclass
class FakeOvo:
    accounts: list[FakeAccount]
    today: date
    gas_half_hourly: bool = False
    # Days whose half-hourly response is missing this many trailing slots.
    short_days: dict[date, int] = field(default_factory=dict)
    # Days whose daily total is reported this many kWh off the half-hourly sum.
    daily_offset: dict[date, Decimal] = field(default_factory=dict)
    # Days with no daily row at all.
    missing_daily: set[date] = field(default_factory=set)
    # Days whose daily row says hasHhData false although half-hours are served.
    hh_flag_off: set[date] = field(default_factory=set)
    # Serve rows for the whole requested period, ignoring the supply period.
    ignore_supply_period: bool = False
    revision: Decimal = Decimal(0)
    # (kind, account id, date parameter) for every data request, in order.
    calls: list[tuple[str, str, str]] = field(default_factory=list)
    intercept: Interceptor | None = None

    def install(self, mock: respx.MockRouter) -> "FakeOvo":
        mock_login(mock)
        mock.post(GRAPHQL_URL).mock(side_effect=self._graphql)
        mock.get(host=USAGE_HOST, path__regex=_USAGE_PATH.pattern).mock(side_effect=self._usage)
        return self

    def usage_calls(self, kind: str | None = None) -> list[tuple[str, str, str]]:
        return [c for c in self.calls if c[0] != "graphql" and (kind is None or c[0] == kind)]

    # -- data model -------------------------------------------------------------------

    def _supplied(self, account_id: str, fuel: str, day: date) -> bool:
        if day >= self.today:
            return False  # OVO's data runs to the end of the previous day
        if self.ignore_supply_period:
            return any(s.fuel == fuel for s in self._account(account_id).supplies)
        return any(
            s.fuel == fuel and s.start <= day and (s.end is None or day <= s.end)
            for s in self._account(account_id).supplies
        )

    def _account(self, account_id: str) -> FakeAccount:
        return next(a for a in self.accounts if a.account_id == account_id)

    def half_hours(self, day: date) -> list[Decimal]:
        slots = london_half_hours(day)
        return [
            Decimal("0.050") + Decimal((day.toordinal() + i) % 7) / 100 + self.revision
            for i in range(len(slots))
        ]

    def day_kwh(self, fuel: str, day: date) -> Decimal:
        if fuel == "ELECTRICITY":
            return sum(self.half_hours(day), Decimal(0)) + self.daily_offset.get(day, Decimal(0))
        return Decimal(20 + day.toordinal() % 5) + self.revision

    # -- handlers ---------------------------------------------------------------------

    def _graphql(self, request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        operation = body["operationName"]
        account_id = body["variables"].get("accountId", "")
        self.calls.append(("graphql", account_id, operation))
        if self.intercept and (hit := self.intercept("graphql", account_id, operation)):
            return hit
        if operation == "Bootstrap":
            return httpx.Response(200, json=self._bootstrap())
        return httpx.Response(200, json=self._meter_reads(account_id))

    def _usage(self, request: httpx.Request) -> httpx.Response:
        match = _USAGE_PATH.fullmatch(request.url.path)
        assert match is not None
        kind, account_id = match.groups()
        param = request.url.params["date"]
        self.calls.append((kind, account_id, param))
        if self.intercept and (hit := self.intercept(kind, account_id, param)):
            return hit
        if kind == "monthly":
            return httpx.Response(200, json=self._monthly(account_id, int(param)))
        if kind == "daily":
            year, month = map(int, param.split("-"))
            return httpx.Response(200, json=self._daily(account_id, year, month))
        return httpx.Response(200, json=self._half_hourly(account_id, date.fromisoformat(param)))

    def _bootstrap(self) -> dict[str, Any]:
        edges = [
            {
                "node": {
                    "account": {
                        "id": a.account_id,
                        "accountSupplyPoints": [
                            {
                                "startDate": s.start.isoformat(),
                                "supplyPoint": {"fuelType": s.fuel, "sprn": s.mpxn},
                            }
                            for s in a.supplies
                        ],
                    }
                }
            }
            for a in self.accounts
        ]
        return {"data": {"customer_nextV1": {"customerAccountRelationships": {"edges": edges}}}}

    def _meter_reads(self, account_id: str) -> dict[str, Any]:
        points = []
        for s in self._account(account_id).supplies:
            serial = f"{s.fuel[0]}{s.mpxn[-4:]}"
            register = {"registerId": "1", "registerStartDate": s.start.isoformat()}
            reading: dict[str, Any] = {
                "type": s.fuel,
                "date": s.start.isoformat(),
                "lifecycle": "INLIFE",
                "source": "SMART",
                "meterSerialNumber": serial,
            }
            if s.fuel == "GAS":
                reading["value"] = "200.25"
            else:
                reading["registers"] = [{"registerId": "1", "value": "100.5"}]
            end = s.end.isoformat() if s.end and s.end_in_reads else None
            points.append(
                {
                    "startDate": s.start.isoformat(),
                    "end": {"date": end},
                    "supplyPoint": {
                        "sprn": s.mpxn,
                        "fuelType": s.fuel,
                        "meterTechnicalDetails": [
                            {"meterSerialNumber": serial, "type": "S2", "registers": [register]}
                        ],
                    },
                    "meterReads_nextV1": {"edges": [{"node": {"reading": reading}}]},
                }
            )
        return {"data": {"account": {"id": account_id, "accountSupplyPoints": points}}}

    def _days(self, account_id: str, fuel: str, first: date, last: date) -> list[date]:
        days = []
        day = first
        while day <= last:
            if self._supplied(account_id, fuel, day):
                days.append(day)
            day += timedelta(days=1)
        return days

    def _daily(self, account_id: str, year: int, month: int) -> dict[str, Any]:
        first = date(year, month, 1)
        last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        result: dict[str, Any] = {}
        for fuel, key in (("ELECTRICITY", "electricity"), ("GAS", "gas")):
            rows = []
            for day in self._days(account_id, fuel, first, last):
                if day in self.missing_daily:
                    continue
                kwh = self.day_kwh(fuel, day)
                rows.append(
                    {
                        "consumption": float(kwh),
                        "cost": {"amount": _pounds(kwh * UNIT_RATE), "currencyUnit": "GBP"},
                        "hasHhData": (fuel == "ELECTRICITY" or self.gas_half_hourly)
                        and day not in self.hh_flag_off,
                        "interval": {
                            "start": f"{day}T00:00:00.000",
                            "end": f"{day}T23:59:59.999",
                        },
                        "rates": {"anytime": float(UNIT_RATE), "standing": 0.5},
                    }
                )
            result[key] = {"data": rows}
        return result

    def _monthly(self, account_id: str, year: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for fuel, key in (("ELECTRICITY", "electricity"), ("GAS", "gas")):
            rows = []
            for month in range(1, 13):
                first = date(year, month, 1)
                last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
                days = self._days(account_id, fuel, first, last)
                if not days:
                    continue
                kwh = sum((self.day_kwh(fuel, d) for d in days), Decimal(0))
                rows.append(
                    {
                        "year": year,
                        "month": month,
                        "consumption": float(kwh),
                        "cost": {"amount": _pounds(kwh * UNIT_RATE), "currencyUnit": "GBP"},
                    }
                )
            result[key] = {"data": rows}
        return result

    def _half_hourly(self, account_id: str, day: date) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for fuel, key in (("ELECTRICITY", "electricity"), ("GAS", "gas")):
            rows = []
            has_hh = fuel == "ELECTRICITY" or self.gas_half_hourly
            if has_hh and self._supplied(account_id, fuel, day):
                slots = london_half_hours(day)
                values = self.half_hours(day)
                keep = len(slots) - self.short_days.get(day, 0)
                for start, kwh in list(zip(slots, values, strict=True))[:keep]:
                    assert london_date(start) == day
                    rows.append(
                        {
                            "consumption": float(kwh),
                            "interval": {"start": start.strftime("%Y-%m-%dT%H:%M:%S.000")},
                            "unit": "kwh",
                        }
                    )
            result[key] = {"data": rows}
        return result


def _pounds(amount: Decimal) -> str:
    return str(amount.quantize(Decimal("0.01")))


def cloudflare_block() -> httpx.Response:
    return httpx.Response(
        403, headers={"content-type": "text/html", "cf-ray": "abc123"}, text="<html>blocked</html>"
    )


def unavailable() -> httpx.Response:
    return httpx.Response(503, text="down")


def bad_json() -> httpx.Response:
    return httpx.Response(200, json={"electricity": {"data": [{"consumption": "lots"}]}})

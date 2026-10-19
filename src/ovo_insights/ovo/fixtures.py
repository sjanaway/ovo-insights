"""Turn real OVO responses into test fixtures that are safe to commit.

Raw captures go to a gitignored directory (mode 0700, files 0600). Redaction then replaces
every identifying value (ids, MPANs/MPRNs, meter serials, names, emails, addresses) with a
stable fake of the same shape, everywhere it appears, including inside other strings.
Fake 13-digit numbers are given a *wrong* MPAN check digit so they can never be real.

Synthesis then replaces the household's own numbers (usage, costs, rates, meter readings)
with made-up ones, and moves account and meter dates by a random number of days, keeping
the response shapes and the relationships between endpoints that the contract tests check.
"""

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ovo_insights.paths import ensure_private_dir

SENSITIVE_KEYS = frozenset(
    {
        "id",
        "accountno",
        "accountid",
        "customerid",
        "userid",
        "username",
        "sprn",
        "mpxn",
        "mpan",
        "mprn",
        "meterserialnumber",
        "serialnumber",
        "email",
        "identifiers",
        "addresslines",
        "postcode",
        "name",
        "firstname",
        "lastname",
        "phone",
    }
)

MPAN_PRIMES = (3, 5, 7, 13, 17, 19, 23, 29, 31, 37, 41, 43)


def mpan_check_digit(first_twelve: str) -> int:
    total = sum(int(d) * p for d, p in zip(first_twelve, MPAN_PRIMES, strict=True))
    return total % 11 % 10


def is_valid_mpan(text: str) -> bool:
    return (
        len(text) == 13
        and text.isascii()
        and text.isdigit()
        and mpan_check_digit(text[:12]) == int(text[12])
    )


class Redactor:
    """Consistent fake for every sensitive value seen across a whole capture."""

    def __init__(self, salt: str, extra_sensitive: Iterable[str] = ()) -> None:
        self._salt = salt
        self._fakes: dict[str, str] = {}
        for value in extra_sensitive:
            if value:
                self._fake(value)

    def redact(self, payload: Any) -> Any:
        collected = self._collect(payload)
        for value in collected:
            self._fake(value)
        return self._rewrite(payload, key=None)

    def _collect(self, node: Any, key: str | None = None) -> set[str]:
        found: set[str] = set()
        if isinstance(node, dict):
            for k, v in node.items():
                found |= self._collect(v, k)
        elif isinstance(node, list):
            for item in node:
                found |= self._collect(item, key)
        elif isinstance(node, str) and node and key is not None and key.lower() in SENSITIVE_KEYS:
            found.add(node)
        return found

    def _rewrite(self, node: Any, key: str | None) -> Any:
        if isinstance(node, dict):
            return {k: self._rewrite(v, k) for k, v in node.items()}
        if isinstance(node, list):
            return [self._rewrite(item, key) for item in node]
        if isinstance(node, str):
            if key is not None and key.lower() in SENSITIVE_KEYS:
                return self._fake(node)
            return self._sweep(node)
        return node

    def _sweep(self, text: str) -> str:
        # Longest first so a value that contains another is replaced whole.
        for real in sorted(self._fakes, key=len, reverse=True):
            if len(real) >= 4 and real in text:
                text = text.replace(real, self._fakes[real])
        return re.sub(
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
            "someone@example.com",
            text,
            flags=re.ASCII,
        )

    def _fake(self, real: str) -> str:
        if real in self._fakes:
            return self._fakes[real]
        digest = hashlib.sha256(f"{self._salt}:{real}".encode()).digest()
        stream = iter(digest * (len(real) // len(digest) + 1))
        chars = []
        for ch in real:
            b = next(stream)
            if ch.isascii() and ch.isdigit():
                chars.append(str(b % 10))
            elif ch.isascii() and ch.isupper():
                chars.append(chr(ord("A") + b % 26))
            elif ch.isascii() and ch.islower():
                chars.append(chr(ord("a") + b % 26))
            elif ch == "@" or not ch.isascii():
                chars.append("x")
            else:
                chars.append(ch)
        fake = "".join(chars)
        if "@" in real:
            fake = "someone@example.com"
        if len(fake) == 13 and fake.isdigit() and is_valid_mpan(fake):
            fake = fake[:12] + str((int(fake[12]) + 1) % 10)
        self._fakes[real] = fake
        return fake


_LONDON = ZoneInfo("Europe/London")
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_UTC_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?Z", re.ASCII)
_PENNY = Decimal("0.01")


class Synthesiser:
    """Made-up values for a whole (already redacted) capture, keyed by endpoint name.

    Kept from the real responses: shapes, field types, decimal places, usage dates and
    times, which days have data, and how endpoints relate. Daily cost is kWh x unit rate,
    a month with daily data in the capture is exactly the sum of those days, and the two
    half-hourly endpoints agree at the same timestamps.
    Replaced: every kWh, cost, rate and meter reading. Account and meter dates (supply
    starts, meter fits and removals, readings) move back by the same random number of days.
    """

    def __init__(self, salt: str) -> None:
        self._salt = salt
        self._shift = timedelta(days=200 + int(self._uniform("shift", 0, 600)))
        self._rate_factor = self._uniform("rates", Decimal("0.8"), Decimal("1.2"))

    def synthesise(self, captured: dict[str, Any]) -> dict[str, Any]:
        out = dict(captured)
        if "daily" in out:
            out["daily"] = self._daily(out["daily"])
        if "monthly" in out:
            out["monthly"] = self._monthly(out["monthly"], out.get("daily"))
        for name in ("half_hourly", "half_hourly_local"):
            if name in out:
                out[name] = self._half_hourly(out[name])
        if "bootstrap" in out:
            out["bootstrap"] = self._shift_dates(out["bootstrap"])
        if "meter_reads" in out:
            out["meter_reads"] = self._meter_reads(self._shift_dates(out["meter_reads"]))
        return out

    # -- usage ----------------------------------------------------------------------------

    def _half_hourly(self, payload: Any) -> Any:
        for fuel, item in _usage_items(payload):
            start = str(item["interval"]["start"])
            hour = int(start[11:13])
            evening = Decimal("0.25") if 17 <= hour < 22 else Decimal(0)
            daytime = Decimal("0.08") if 7 <= hour < 17 else Decimal(0)
            noise = self._uniform(f"hh:{fuel}:{start}", 0, Decimal("0.05"))
            item["consumption"] = _like(
                item["consumption"], Decimal("0.05") + daytime + evening + noise
            )
        return payload

    def _daily(self, payload: Any) -> Any:
        for fuel, item in _usage_items(payload):
            start = str(item["interval"]["start"])
            low, high = (5, 11) if fuel == "electricity" else (2, 30)
            kwh = _like(item["consumption"], self._uniform(f"day:{fuel}:{start}", low, high))
            item["consumption"] = kwh
            rates = item.get("rates")
            if isinstance(rates, dict):
                for key, value in rates.items():
                    if value is not None:
                        rates[key] = _like(value, Decimal(str(value)) * self._rate_factor)
                anytime = rates.get("anytime")
                if anytime is not None and isinstance(item.get("cost"), dict):
                    cost = Decimal(str(kwh)) * Decimal(str(anytime))
                    item["cost"]["amount"] = _pounds_like(item["cost"]["amount"], cost)
        return payload

    def _monthly(self, payload: Any, daily: Any) -> Any:
        sums: dict[tuple[str, int, int], tuple[Decimal, Decimal]] = {}
        rate: dict[str, Decimal] = {}
        for fuel, item in _usage_items(daily) if daily is not None else []:
            day = date.fromisoformat(str(item["interval"]["start"])[:10])
            key = (fuel, day.year, day.month)
            kwh, cost = sums.get(key, (Decimal(0), Decimal(0)))
            amount = (item.get("cost") or {}).get("amount")
            sums[key] = (
                kwh + Decimal(str(item["consumption"])),
                cost + (Decimal(str(amount)) if amount not in (None, "") else Decimal(0)),
            )
            anytime = (item.get("rates") or {}).get("anytime")
            if anytime is not None:
                rate.setdefault(fuel, Decimal(str(anytime)))
        for fuel, item in _usage_items(payload):
            key = (fuel, int(item["year"]), int(item["month"]))
            if key in sums:
                kwh, cost = sums[key]
                places = _places(kwh)  # exactly the sum of the days
            else:
                low, high = (180, 320) if fuel == "electricity" else (60, 900)
                kwh = self._uniform(f"month:{fuel}:{key[1]}-{key[2]}", low, high)
                cost = kwh * rate.get(fuel, Decimal("0.25"))
                places = 2
            item["consumption"] = _like(item["consumption"], kwh, places)
            if isinstance(item.get("cost"), dict):
                item["cost"]["amount"] = _pounds_like(item["cost"]["amount"], cost)
        return payload

    # -- meter readings -------------------------------------------------------------------

    def _meter_reads(self, payload: Any) -> Any:
        account = payload["data"]["account"]
        for asp in account.get("accountSupplyPoints") or []:
            digits: dict[str, int] = {}
            for detail in asp["supplyPoint"].get("meterTechnicalDetails") or []:
                counts = [
                    int(r["numberOfDigits"])
                    for r in detail.get("registers") or []
                    if str(r.get("numberOfDigits") or "").isdigit()
                ]
                digits[detail["meterSerialNumber"]] = max(counts, default=5)
            series: dict[tuple[str, str], list[tuple[date, dict[str, Any], str]]] = {}
            for edge in (asp.get("meterReads_nextV1") or {}).get("edges") or []:
                read = edge["node"]["reading"]
                serial = read["meterSerialNumber"]
                day = date.fromisoformat(str(read["date"])[:10])
                if read.get("value") is not None:
                    series.setdefault((serial, ""), []).append((day, read, "value"))
                for reg in read.get("registers") or []:
                    if reg.get("value") is not None:
                        series.setdefault((serial, reg["registerId"]), []).append(
                            (day, reg, "value")
                        )
            for (serial, register), points in series.items():
                modulus = Decimal(10) ** digits.get(serial, 5)
                total = self._uniform(f"base:{serial}:{register}", 1000, 9000)
                values: dict[date, Decimal] = {}
                previous: date | None = None
                for day, _node, _key in sorted(points, key=lambda p: p[0]):
                    if day not in values:
                        if previous is not None:
                            step = self._uniform(f"step:{serial}:{register}:{day}", 4, 14)
                            total += step * (day - previous).days
                        values[day] = total % modulus
                        previous = day
                for day, node, key in points:
                    node[key] = _like(node[key], values[day])
        return payload

    def _shift_dates(self, node: Any) -> Any:
        if isinstance(node, dict):
            return {k: self._shift_dates(v) for k, v in node.items()}
        if isinstance(node, list):
            return [self._shift_dates(v) for v in node]
        if isinstance(node, str):
            if _DATE_ONLY.fullmatch(node):
                return (date.fromisoformat(node) - self._shift).isoformat()
            match = _UTC_STAMP.fullmatch(node)
            if match:
                # Move the UK wall-clock time, so the UK date (what the app stores) moves by
                # exactly the shift even when the result lands on the other side of a
                # clock change.
                instant = datetime.fromisoformat(match.group(1) + "+00:00")
                local = instant.astimezone(_LONDON).replace(tzinfo=None) - self._shift
                moved = local.replace(tzinfo=_LONDON).astimezone(ZoneInfo("UTC"))
                return moved.strftime("%Y-%m-%dT%H:%M:%S") + (match.group(2) or "") + "Z"
        return node

    # -- helpers --------------------------------------------------------------------------

    def _uniform(self, label: str, low: Decimal | int, high: Decimal | int) -> Decimal:
        digest = hashlib.sha256(f"{self._salt}:{label}".encode()).digest()
        fraction = Decimal(int.from_bytes(digest[:8], "big")) / Decimal(2**64)
        return Decimal(low) + (Decimal(high) - Decimal(low)) * fraction


def _usage_items(payload: Any) -> list[tuple[str, dict[str, Any]]]:
    """(fuel, item) for every usage row in a monthly, daily or half-hourly response."""
    items = []
    for fuel in ("electricity", "gas"):
        block = payload.get(fuel) if isinstance(payload, dict) else None
        for item in (block or {}).get("data") or []:
            if isinstance(item, dict) and item.get("consumption") is not None:
                items.append((fuel, item))
    return items


def _places(value: object) -> int:
    text = format(value, "f") if isinstance(value, Decimal) else str(value)
    return len(text.partition(".")[2])


def _like(original: object, value: Decimal, places: int | None = None) -> Any:
    """`value` with the original's type and, unless given, number of decimal places (at
    least three for floats, which lose their trailing zeros)."""
    if places is None:
        places = _places(original)
        if isinstance(original, float):
            places = max(places, 3)
    rounded = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    if isinstance(original, str):
        return format(rounded, "f")
    if isinstance(original, float):
        return float(rounded)
    if isinstance(original, int):
        return int(rounded)
    return rounded


def _pounds_like(original: object, value: Decimal) -> Any:
    pounds = value.quantize(_PENNY, rounding=ROUND_HALF_UP)
    return format(pounds, "f") if isinstance(original, str) else pounds


_DECIMAL_MARK = "__ovo_decimal__"
_DECIMAL_PLACEHOLDER = re.compile(rf'"{_DECIMAL_MARK}(-?\d+(?:\.\d+)?)"', re.ASCII)


def dumps(payload: Any) -> str:
    """Pretty JSON that writes Decimals back out with exactly the digits OVO sent."""
    text = json.dumps(_mark_decimals(payload), indent=2, sort_keys=True, ensure_ascii=False)
    return _DECIMAL_PLACEHOLDER.sub(r"\1", text) + "\n"


def _mark_decimals(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _mark_decimals(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_mark_decimals(v) for v in node]
    if isinstance(node, Decimal):
        return f"{_DECIMAL_MARK}{format(node, 'f')}"
    return node


def write_private(path: Path, text: str) -> None:
    ensure_private_dir(path.parent)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(text, encoding="utf-8")

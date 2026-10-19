"""Exact money handling.

All money is held as integer milli-pence (thousandths of a penny). That is fine enough to sum
per-half-hour costs (e.g. 0.137 kWh x 24.567 p/kWh) without drift, and integers never suffer
float rounding. Floats are rejected outright: callers must parse JSON numbers as `Decimal`.
"""

import re
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from typing import NewType

MilliPence = NewType("MilliPence", int)

MILLI_PENCE_PER_PENNY = 1_000
MILLI_PENCE_PER_POUND = 100 * MILLI_PENCE_PER_PENNY

# Sanity ceiling: no single domestic energy figure comes anywhere near £1m.
MAX_ABS_POUNDS = Decimal(1_000_000)

# Plain decimal notation only: no exponents, NaN, infinity, currency symbols or separators.
_DECIMAL_TEXT = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", re.ASCII)
_MAX_TEXT_LENGTH = 40


class MoneyError(ValueError):
    """A money value was malformed, non-finite, out of range or of the wrong sign."""


def parse_pounds(value: str | int | Decimal, *, allow_negative: bool = False) -> MilliPence:
    """Convert an amount in pounds to milli-pence, rounding half-even to the nearest milli-penny."""
    return _to_milli_pence(value, MILLI_PENCE_PER_POUND, allow_negative=allow_negative)


def parse_pence(value: str | int | Decimal, *, allow_negative: bool = False) -> MilliPence:
    """Convert an amount in pence (e.g. a unit rate in p/kWh) to milli-pence."""
    return _to_milli_pence(value, MILLI_PENCE_PER_PENNY, allow_negative=allow_negative)


def to_pounds(amount: MilliPence) -> Decimal:
    """Exact conversion from milli-pence to pounds."""
    return Decimal(amount).scaleb(-5)


def format_pounds(amount: MilliPence) -> str:
    """Human-readable pounds rounded half-even to the penny, e.g. `£1,234.57` or `-£0.05`."""
    pounds = to_pounds(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    sign = "-" if pounds < 0 else ""
    return f"{sign}£{abs(pounds):,.2f}"


def format_pence(amount: MilliPence) -> str:
    """Pence to exactly 3 decimal places (milli-pence are exact): `24440` -> `24.440`."""
    return f"{Decimal(amount).scaleb(-3):.3f}"


def _to_milli_pence(value: str | int | Decimal, factor: int, *, allow_negative: bool) -> MilliPence:
    amount = _to_decimal(value)
    # Compare before scaling so absurd exponents can't overflow the decimal context.
    if abs(amount) > MAX_ABS_POUNDS * MILLI_PENCE_PER_POUND / factor:
        raise MoneyError(f"Amount out of range: {value!r}")
    if amount < 0 and not allow_negative:
        raise MoneyError(f"Negative amount not allowed here: {value!r}")
    with localcontext() as ctx:
        ctx.prec = 50
        scaled = (amount * factor).to_integral_value(rounding=ROUND_HALF_EVEN)
    return MilliPence(int(scaled))


def _to_decimal(value: object) -> Decimal:
    # bool is a subclass of int and float is lossy; both are programming errors here.
    if isinstance(value, bool | float):
        raise TypeError(f"Money must be str, int or Decimal, not {type(value).__name__}")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise MoneyError(f"Amount is not finite: {value!r}")
        return value
    if isinstance(value, str):
        text = value.strip()
        if len(text) > _MAX_TEXT_LENGTH or not _DECIMAL_TEXT.fullmatch(text):
            raise MoneyError(f"Not a plain decimal amount: {value!r}")
        try:
            return Decimal(text)
        except InvalidOperation as exc:  # pragma: no cover - the regex admits only valid input
            raise MoneyError(f"Not a plain decimal amount: {value!r}") from exc
    raise TypeError(f"Money must be str, int or Decimal, not {type(value).__name__}")

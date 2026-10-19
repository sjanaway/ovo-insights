"""Meter readings: consumption derived from successive readings, checked against smart data.

A meter register is a counter. The difference between two successive readings is what went
through it in between, so each pair of readings gives a *window*, which can be compared with
the smart-data (daily) total for the same days.

A window from reading date `a` to reading date `b` covers the days `a` up to but not
including `b`: the reading dated `a` is the counter at the start of day `a` (confirmed on the
owner's account, where the daily electricity differences equal the daily smart-data totals).

Electricity registers count kWh, so the derived kWh is compared directly: flagged when it
differs from the smart-data total by more than 5%.

Gas registers count volume, and OVO's bill converts it to kWh with a calorific value that
changes every day (37.5 to 43.0), so there is no single figure to convert with, and none is
guessed. Instead the bill's formula is run backwards: from the smart-data kWh and the volume
the register recorded, the window's *implied* calorific value. A window is flagged when that
falls outside the range on the bill.

Nothing else is guessed. A window is only compared when every input is known: the register's
unit is understood, the counter went up (a drop could be a rollover or a replaced meter;
either way the difference means nothing), the meter has one register (several registers need
summing, which isn't done), and the smart data covers every day of the window. Otherwise the
window says why it wasn't compared.

All arithmetic is exact `Decimal`. kWh from the smart data are floats, converted through `str`
(the way they're stored and displayed).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Protocol

from ovo_insights.domain import Fuel, Reading

# A window is flagged when derived and smart-data kWh differ by more than this share of the
# smart-data total (PRD: "mismatches over 5%")...
MISMATCH_PERCENT = Decimal(5)
# ...and by at least this much, so rounding on tiny windows (0.001 steps on the meter) can't
# raise a flag. Same floor as the sync engine's daily-vs-half-hourly check.
MIN_DIFFERENCE_KWH = Decimal("0.1")

KWH_UNIT = "KWH"
# Gas, from the owner's OVO bill: kWh = metered volume x metric conversion factor x
# calorific value x 1.02264 (volume correction) / 3.6. The conversion factor is 1 for a
# metric meter and 2.83 for an imperial one; the calorific value "can range from 37.5 to
# 43.0". OVO labels the owner's metric gas registers "SCMH" and the old imperial one "SCFH".
VOLUME_CORRECTION = Decimal("1.02264")
MJ_PER_KWH = Decimal("3.6")
GAS_CONVERSION_FACTORS = {"SCMH": Decimal(1), "SCFH": Decimal("2.83")}
CALORIFIC_RANGE = (Decimal("37.5"), Decimal("43.0"))


class Verdict(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"
    NO_SMART_DATA = "no_smart_data"
    PARTIAL_SMART_DATA = "partial_smart_data"
    WENT_DOWN = "went_down"
    UNSUPPORTED_UNIT = "unsupported_unit"
    SEVERAL_REGISTERS = "several_registers"


VERDICT_LABELS = {
    Verdict.MATCH: "Matches",
    Verdict.MISMATCH: "Differs",
    Verdict.NO_SMART_DATA: "No smart data for these days",
    Verdict.PARTIAL_SMART_DATA: "Smart data has gaps",
    Verdict.WENT_DOWN: "Reading went down",
    Verdict.UNSUPPORTED_UNIT: "Unit not supported",
    Verdict.SEVERAL_REGISTERS: "Meter has several registers",
}
# Windows that aren't comparable or that disagree: what "show only problems" lists.
PROBLEM_VERDICTS = frozenset(Verdict) - {Verdict.MATCH, Verdict.NO_SMART_DATA}


@dataclass(frozen=True, slots=True)
class RegisterSeries:
    """All readings of one register of one meter. Identifiers stay internal: the web layer
    shows a number ("Electricity meter 1"), never the serial."""

    fuel: Fuel
    meter_serial: str
    register_id: str
    meter_status: str | None
    unit: str | None  # None when the register rows disagree or give no unit
    registers_on_meter: int  # distinct register ids with readings on this meter
    readings: tuple[Reading, ...]  # oldest first, one per date

    @property
    def first_read(self) -> date | None:
        return self.readings[0].read_date if self.readings else None


@dataclass(frozen=True, slots=True)
class Window:
    start: date  # reading date at the start
    end: date  # reading date at the end (the window excludes this day)
    change: Decimal  # end reading minus start reading, in register units (may be negative)
    derived_kwh: Decimal | None  # electricity only: gas needs a calorific value that varies
    volume_m3: Decimal | None  # gas only: the change in metric units
    smart_kwh: Decimal | None
    smart_days: int  # days in the window that have smart data
    days: int
    difference_percent: Decimal | None  # electricity: of the smart-data total (None if zero)
    implied_calorific_value: Decimal | None  # gas only, MJ/m3
    verdict: Verdict

    @property
    def difference_kwh(self) -> Decimal | None:
        if self.derived_kwh is None or self.smart_kwh is None:
            return None
        return self.derived_kwh - self.smart_kwh


def series_order(series: RegisterSeries) -> tuple[int, bool, date, str, str]:
    """Electricity before gas, active meters first, then oldest first."""
    return (
        list(Fuel).index(series.fuel),
        series.meter_status != "Active",
        series.first_read or date.max,
        series.meter_serial,
        series.register_id,
    )


def sort_series(items: Sequence[RegisterSeries]) -> list[RegisterSeries]:
    return sorted((s for s in items if s.readings), key=series_order)


def windows_of(
    series: RegisterSeries,
    smart_kwh: Mapping[date, float],
    smart_from: date | None,
    *,
    ends_in: tuple[date, date] | None = None,
) -> list[Window]:
    """The windows between successive readings, oldest first. `smart_from` is the first day
    we hold smart data for this fuel: windows ending on or before it are "no smart data" (it
    wasn't downloaded or didn't exist), while days missing after it are gaps. With `ends_in`
    (first, last), only windows whose end date lies in that range."""
    result = []
    for before, after in zip(series.readings, series.readings[1:], strict=False):
        if ends_in is not None and not ends_in[0] <= after.read_date <= ends_in[1]:
            continue
        result.append(_window(series, before, after, smart_kwh, smart_from))
    return result


def _window(
    series: RegisterSeries,
    before: Reading,
    after: Reading,
    smart_kwh: Mapping[date, float],
    smart_from: date | None,
) -> Window:
    days = (after.read_date - before.read_date).days
    change = after.value - before.value
    present = [
        smart_kwh[d]
        for d in (before.read_date + timedelta(days=i) for i in range(days))
        if d in smart_kwh
    ]
    smart = _exact_sum(present) if present else None
    unit = series.unit.strip().upper() if series.unit else None
    gas_factor = GAS_CONVERSION_FACTORS.get(unit or "") if series.fuel is Fuel.GAS else None
    electricity = series.fuel is Fuel.ELECTRICITY and unit == KWH_UNIT

    def result(
        verdict: Verdict,
        *,
        derived: Decimal | None = None,
        volume: Decimal | None = None,
        percent: Decimal | None = None,
        implied: Decimal | None = None,
    ) -> Window:
        return Window(
            before.read_date, after.read_date, change, derived, volume, smart, len(present),
            days, percent, implied, verdict,
        )  # fmt: skip

    if change < 0:
        return result(Verdict.WENT_DOWN)
    if not electricity and gas_factor is None:
        return result(Verdict.UNSUPPORTED_UNIT)
    volume = None if gas_factor is None else change * gas_factor
    derived = change if electricity else None
    if series.registers_on_meter > 1:
        return result(Verdict.SEVERAL_REGISTERS, derived=derived, volume=volume)
    if smart is None and (smart_from is None or after.read_date <= smart_from):
        return result(Verdict.NO_SMART_DATA, derived=derived, volume=volume)
    if smart is None or len(present) < days:
        return result(Verdict.PARTIAL_SMART_DATA, derived=derived, volume=volume)
    if derived is not None:
        return _compare_kwh(derived, smart, result)
    return _check_calorific_value(volume or Decimal(0), smart, result)


def _compare_kwh(derived: Decimal, smart: Decimal, result: "ResultFactory") -> Window:
    difference = abs(derived - smart)
    percent = (
        (difference / smart * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_EVEN)
        if smart > 0
        else None
    )
    # Over the percentage (or, with nothing in the smart data, anything over the floor) and
    # at least the floor in kWh.
    over_share = difference * 100 > MISMATCH_PERCENT * smart
    flagged = over_share and difference >= MIN_DIFFERENCE_KWH
    return result(Verdict.MISMATCH if flagged else Verdict.MATCH, derived=derived, percent=percent)


def _check_calorific_value(volume: Decimal, smart: Decimal, result: "ResultFactory") -> Window:
    """Run the bill's formula backwards: the calorific value that turns this volume into the
    smart-data kWh. Plausible if it lies within the range printed on the bill."""
    if volume == 0:
        # Nothing went through the meter: the smart data should agree (to within the floor).
        agrees = smart < MIN_DIFFERENCE_KWH
        return result(Verdict.MATCH if agrees else Verdict.MISMATCH, volume=volume)
    implied = (smart * MJ_PER_KWH / (volume * VOLUME_CORRECTION)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_EVEN
    )
    low, high = CALORIFIC_RANGE
    verdict = Verdict.MATCH if low <= implied <= high else Verdict.MISMATCH
    return result(verdict, volume=volume, implied=implied)


class ResultFactory(Protocol):
    def __call__(
        self,
        verdict: Verdict,
        *,
        derived: Decimal | None = None,
        volume: Decimal | None = None,
        percent: Decimal | None = None,
        implied: Decimal | None = None,
    ) -> Window: ...


def _exact_sum(values: Sequence[float]) -> Decimal:
    return sum((Decimal(str(v)) for v in values), Decimal(0))


@dataclass(frozen=True, slots=True)
class WindowSummary:
    total: int
    compared: int  # matched or mismatched
    mismatched: int
    not_compared: int  # everything else, including windows before smart data


def summarise_windows(windows: Sequence[Window]) -> WindowSummary:
    mismatched = sum(1 for w in windows if w.verdict is Verdict.MISMATCH)
    compared = mismatched + sum(1 for w in windows if w.verdict is Verdict.MATCH)
    return WindowSummary(len(windows), compared, mismatched, len(windows) - compared)

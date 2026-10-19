"""Re-costing usage under a tariff, and comparing tariffs with what OVO charged.

*Costing a day.* OVO charges `round-to-the-penny(kWh x unit rate)` for each day, with the
standing charge added on top, exact (confirmed on the owner's data: every stored daily cost
equals that). The engine does the same, so a tariff with OVO's own rates reproduces OVO's own
figures. A flat tariff (one rate all day) is costed from the day's total kWh, as OVO does. A
tariff with several rates needs the day's half-hours, matched to bands by UK wall-clock time,
and only costs days whose half-hours are all there.

*Like for like.* A comparison only counts a day that every column can cost, plus the baseline
("actually paid": OVO's cost and standing charge). Any other day is left out of every column,
whatever the reason, and the page can say how many and why. A tariff with a different name is
a different column; versions of one tariff (same name and fuel, different effective dates)
make one column, so a price change part-way through the range is one column.

All money is integer milli-pence; kWh are floats converted through `str` to `Decimal`.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum

from ovo_insights.analysis.half_hourly import local_slot
from ovo_insights.domain import Fuel
from ovo_insights.money import MILLI_PENCE_PER_PENNY, MilliPence
from ovo_insights.tariffs import Tariff


class Skip(StrEnum):
    """Why a day can't be costed, in the order they are checked."""

    NO_DAILY = "no_daily"  # no daily figure stored for the day
    NO_ACTUAL = "no_actual"  # OVO gave no cost or standing charge for it
    NO_HALF_HOURS = "no_half_hours"  # a multi-rate tariff needs every half-hour of the day
    OUTSIDE_TARIFF = "outside_tariff"  # no version of the tariff applies on that day


SKIP_LABELS = {
    Skip.NO_DAILY: "no daily figure stored",
    Skip.NO_ACTUAL: "OVO gave no cost for the day",
    Skip.NO_HALF_HOURS: "half-hours incomplete or not available (needed for a time-of-day tariff)",
    Skip.OUTSIDE_TARIFF: "outside the tariff's dates",
}


@dataclass(frozen=True, slots=True)
class CostingDay:
    """What is stored for one UK day of one fuel, summed across accounts."""

    day: date
    kwh: float | None  # None: no daily figure
    actual_usage: MilliPence | None  # OVO's cost: kWh x unit rate, to the penny
    actual_standing: MilliPence | None
    half_hours: Mapping[datetime, float] | None  # every half-hour of the day, or None


@dataclass(frozen=True, slots=True)
class DayCost:
    usage: MilliPence
    standing: MilliPence

    @property
    def total(self) -> MilliPence:
        return MilliPence(self.usage + self.standing)


def penny_round(milli_pence: Decimal) -> MilliPence:
    """Round to a whole penny (half-even), the way OVO rounds a day's usage cost."""
    pennies = (milli_pence / MILLI_PENCE_PER_PENNY).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
    return MilliPence(int(pennies) * MILLI_PENCE_PER_PENNY)


def recost_day(tariff: Tariff, day: CostingDay) -> DayCost | Skip:
    """The day's cost under one tariff, or why it can't be costed."""
    if day.kwh is None:
        return Skip.NO_DAILY
    if tariff.is_flat:
        rate = tariff.rate_at(0, 0)
        return DayCost(penny_round(Decimal(str(day.kwh)) * rate), tariff.standing_charge)
    if day.half_hours is None:
        return Skip.NO_HALF_HOURS
    total = Decimal(0)
    for start, kwh in day.half_hours.items():
        local_day, slot = local_slot(start)
        total += Decimal(str(kwh)) * tariff.rate_at(local_day.weekday(), slot)
    return DayCost(penny_round(total), tariff.standing_charge)


# -- columns ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Column:
    """One thing to compare: a tariff, or all the dated versions of it."""

    name: str
    fuel: Fuel
    versions: tuple[Tariff, ...]

    def version_on(self, day: date) -> Tariff | None:
        for version in self.versions:
            if version.applies_on(day):
                return version
        return None


def columns_for(tariffs: Iterable[Tariff], fuel: Fuel) -> list[Column]:
    """One column per tariff name (case-insensitive) for a fuel; versions in date order."""
    groups: dict[str, list[Tariff]] = {}
    for tariff in tariffs:
        if tariff.fuel is fuel:
            groups.setdefault(tariff.name.casefold(), []).append(tariff)
    return [
        Column(
            versions[0].name,
            fuel,
            tuple(sorted(versions, key=lambda t: t.effective_from or date.min)),
        )
        for versions in groups.values()
    ]


def cost_in_column(column: Column, day: CostingDay) -> DayCost | Skip:
    version = column.version_on(day.day)
    if version is None:
        return Skip.OUTSIDE_TARIFF
    return recost_day(version, day)


# -- comparison ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Totals:
    usage: MilliPence
    standing: MilliPence
    by_month: dict[date, MilliPence]  # first of the month -> total cost
    usage_by_month: dict[date, MilliPence] = field(default_factory=dict)
    standing_by_month: dict[date, MilliPence] = field(default_factory=dict)

    @property
    def total(self) -> MilliPence:
        return MilliPence(self.usage + self.standing)


@dataclass(frozen=True, slots=True)
class ColumnResult:
    name: str
    totals: Totals
    difference: MilliPence  # this column minus what was actually paid
    difference_percent: Decimal | None  # of what was actually paid; None if that is zero


@dataclass(frozen=True, slots=True)
class Comparison:
    days_considered: int
    included: tuple[date, ...]
    excluded: tuple[tuple[date, Skip], ...]  # each excluded day with its first reason
    actual: Totals
    columns: tuple[ColumnResult, ...]

    @property
    def excluded_counts(self) -> dict[Skip, int]:
        counts = dict.fromkeys(Skip, 0)
        for _, reason in self.excluded:
            counts[reason] += 1
        return {reason: n for reason, n in counts.items() if n}


def actual_cost(day: CostingDay) -> DayCost | Skip:
    """What OVO charged for the day: its usage cost plus its standing charge. A missing
    standing charge is "not known", not zero."""
    if day.kwh is None:
        return Skip.NO_DAILY
    if day.actual_usage is None or day.actual_standing is None:
        return Skip.NO_ACTUAL
    return DayCost(day.actual_usage, day.actual_standing)


def compare(days: Sequence[CostingDay], columns: Sequence[Column]) -> Comparison:
    """Total each column and the baseline over the days all of them can cost."""
    included: list[date] = []
    excluded: list[tuple[date, Skip]] = []
    actual_days: list[tuple[date, DayCost]] = []
    column_days: list[list[tuple[date, DayCost]]] = [[] for _ in columns]
    for day in days:
        costs: list[DayCost | Skip] = [actual_cost(day), *(cost_in_column(c, day) for c in columns)]
        reason = next((c for c in costs if isinstance(c, Skip)), None)
        if reason is not None:
            excluded.append((day.day, reason))
            continue
        included.append(day.day)
        for target, cost in zip([actual_days, *column_days], costs, strict=True):
            if isinstance(cost, DayCost):
                target.append((day.day, cost))
    actual = _totals(actual_days)
    results = tuple(
        _result(column.name, _totals(found), actual)
        for column, found in zip(columns, column_days, strict=True)
    )
    return Comparison(len(days), tuple(included), tuple(excluded), actual, results)


def _totals(days: Iterable[tuple[date, DayCost]]) -> Totals:
    usage = standing = 0
    by_month: dict[date, int] = defaultdict(int)
    usage_by_month: dict[date, int] = defaultdict(int)
    standing_by_month: dict[date, int] = defaultdict(int)
    for day, cost in days:
        usage += cost.usage
        standing += cost.standing
        month = day.replace(day=1)
        by_month[month] += cost.total
        usage_by_month[month] += cost.usage
        standing_by_month[month] += cost.standing

    def milli(found: dict[date, int]) -> dict[date, MilliPence]:
        return {month: MilliPence(amount) for month, amount in sorted(found.items())}

    return Totals(
        MilliPence(usage),
        MilliPence(standing),
        milli(by_month),
        milli(usage_by_month),
        milli(standing_by_month),
    )


def _result(name: str, totals: Totals, actual: Totals) -> ColumnResult:
    difference = MilliPence(totals.total - actual.total)
    percent = (
        None
        if actual.total == 0
        else (Decimal(difference) / Decimal(actual.total) * 100).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_EVEN
        )
    )
    return ColumnResult(name, totals, difference, percent)

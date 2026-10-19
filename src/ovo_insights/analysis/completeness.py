"""Data completeness: one cell per day per fuel, and coverage percentages.

States of a day (for one fuel):
- complete: the daily figure is stored and every expected half-hour is there
- partial: the daily figure is stored but half-hours are short (n of the expected slots)
- missing: no daily figure is stored for a day the fuel was supplied
- not yet available: today or later (OVO's data runs to the end of yesterday)
- no supply: outside the fuel's supply dates (before it started or after it ended)

Half-hours expected on a day are the UK day's 46, 48 or 50 slots (clock changes are correct,
not gaps), as the sync engine does it: a day expects slots when its daily row says OVO has
half-hourly data (`hasHhData`), or when slots arrived anyway. A day with no daily row at all
has no flag to read, so it expects a full day once the fuel has ever had half-hourly data
(a fuel that never had any, such as the owner's gas, expects none).

Days before the first day we hold data for a fuel are not part of the calendar at all: the
two-year sync window means they were never downloaded, which isn't the same as missing.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum

from ovo_insights.sync.plan import DateRange, PeriodStatus
from ovo_insights.timeutil import london_half_hours


class DayState(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    MISSING = "missing"
    NOT_YET = "not_yet"
    NO_SUPPLY = "no_supply"


STATE_LABELS = {
    DayState.COMPLETE: "Complete",
    DayState.PARTIAL: "Partial",
    DayState.MISSING: "Missing",
    DayState.NOT_YET: "Not yet available",
    DayState.NO_SUPPLY: "No supply",
}


@dataclass(frozen=True, slots=True)
class DayFacts:
    """What is stored for one fuel on one UK day."""

    has_daily: bool
    has_half_hourly_flag: bool  # OVO's `hasHhData` on the daily row
    received: int  # distinct half-hour slots stored
    sync_status: PeriodStatus | None = None  # the half-hourly period's state, if recorded


@dataclass(frozen=True, slots=True)
class DayCell:
    day: date
    state: DayState
    expected: int  # half-hour slots expected (0 if none)
    received: int
    sync_status: PeriodStatus | None = None

    @property
    def note(self) -> str | None:
        """For a partial day: whether the sync will try again."""
        if self.state is not DayState.PARTIAL:
            return None
        if self.sync_status is PeriodStatus.GAP:
            return "known gap: OVO doesn't have the rest"
        if self.sync_status in (PeriodStatus.INCOMPLETE, PeriodStatus.ERROR):
            return "the next sync will try again"
        return None


@dataclass(frozen=True, slots=True)
class FuelCompleteness:
    cells: list[DayCell]
    counts: dict[DayState, int]
    expected_slots: int
    received_slots: int

    @property
    def half_hour_percent(self) -> Decimal | None:
        """Share of expected half-hours that are stored; None if none were expected."""
        return _percent(self.received_slots, self.expected_slots)

    @property
    def daily_percent(self) -> Decimal | None:
        """Share of supplied, past days that have a daily figure."""
        have = self.counts[DayState.COMPLETE] + self.counts[DayState.PARTIAL]
        return _percent(have, have + self.counts[DayState.MISSING])


def supplied_on(day: date, supply: Sequence[DateRange]) -> bool:
    """Whether any supply period covers `day`. No supply periods known: assume supplied."""
    return not supply or any(day in period for period in supply)


def expected_slots(day: date, facts: DayFacts | None, first_half_hourly: date | None) -> int:
    full = len(london_half_hours(day))
    if facts is None or not facts.has_daily:
        return full if first_half_hourly is not None and day >= first_half_hourly else 0
    return full if facts.has_half_hourly_flag or facts.received else 0


def classify_day(
    day: date,
    today: date,
    supply: Sequence[DateRange],
    facts: DayFacts | None,
    first_half_hourly: date | None,
) -> DayCell:
    if day >= today:
        return DayCell(day, DayState.NOT_YET, 0, 0)
    if not supplied_on(day, supply):
        return DayCell(day, DayState.NO_SUPPLY, 0, 0)
    expected = expected_slots(day, facts, first_half_hourly)
    received = facts.received if facts is not None else 0
    status = facts.sync_status if facts is not None else None
    if facts is None or not facts.has_daily:
        return DayCell(day, DayState.MISSING, expected, received, status)
    state = DayState.COMPLETE if received >= expected else DayState.PARTIAL
    return DayCell(day, state, expected, received, status)


def fuel_completeness(
    span: DateRange,
    today: date,
    supply: Sequence[DateRange],
    facts: Mapping[date, DayFacts],
    first_data: date | None,
    first_half_hourly: date | None,
) -> FuelCompleteness | None:
    """The calendar cells for `span` (plus today, when `span` ends yesterday) and totals.

    Days before `first_data` are left out. None if nothing remains. Today's cell is shown
    as "not yet available" but never counts towards a percentage.
    """
    if first_data is None:
        return None
    clipped = span.clip(first_data, span.last)
    if clipped is None:
        return None
    days: Iterable[date] = clipped.days()
    if span.last == today - timedelta(days=1):
        days = [*clipped.days(), today]
    cells = [classify_day(d, today, supply, facts.get(d), first_half_hourly) for d in days]
    counts = dict.fromkeys(DayState, 0)
    expected = received = 0
    for cell in cells:
        counts[cell.state] += 1
        if cell.state in (DayState.COMPLETE, DayState.PARTIAL, DayState.MISSING):
            expected += cell.expected
            received += cell.received
    return FuelCompleteness(cells, counts, expected, received)


def _percent(part: int, whole: int) -> Decimal | None:
    if whole == 0:
        return None
    return (Decimal(part) * 100 / whole).quantize(Decimal("0.1"), rounding=ROUND_HALF_EVEN)

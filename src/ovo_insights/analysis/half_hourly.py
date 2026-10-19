"""Half-hourly patterns: the heatmap grid, typical-day profiles and base load.

Half-hours are stored in UTC and shown by UK wall-clock time. A "slot" is the wall-clock
half-hour of the day, 0 (00:00) to 47 (23:30). On the day the clocks go back, 01:00 and
01:30 happen twice: the heatmap shows the average of the two and flags it, and profiles
count both as separate observations. On the day they go forward those slots don't exist.
"""

import re
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from ovo_insights.analysis.usage import round_half_even
from ovo_insights.domain import HalfHourUsage
from ovo_insights.money import MilliPence
from ovo_insights.sync.plan import DateRange
from ovo_insights.timeutil import LONDON, london_half_hours

SLOTS_PER_DAY = 48
SLOT_LABELS = tuple(f"{i // 2:02d}:{30 * (i % 2):02d}" for i in range(SLOTS_PER_DAY))

# Base load: a low percentile of overnight half-hours, when little but always-on devices run.
BASE_LOAD_PERCENTILE = 10
HOURS_PER_YEAR = Decimal(8760)
_CLOCK = re.compile(r"([01]\d|2[0-3]):([03]0)", re.ASCII)


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """A span of the UK wall-clock day, in half-hour slots: `start` (0-47) up to but not
    including `end` (1-48; 48 is midnight). An `end` at or before `start` runs past midnight
    into the next morning (22:00 to 06:00), and the morning part belongs to the evening's
    "night". A window can't be empty: from 06:00 to 06:00 is refused."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if not (0 <= self.start < SLOTS_PER_DAY and 1 <= self.end <= SLOTS_PER_DAY):
            raise ValueError(f"Time window out of range: {self.start}-{self.end}")
        if self.start == self.end:
            raise ValueError("A time window can't start and end at the same time.")

    @property
    def wraps(self) -> bool:
        return self.end <= self.start

    def contains(self, slot: int) -> bool:
        if self.wraps:
            return slot >= self.start or slot < self.end
        return self.start <= slot < self.end

    @property
    def label(self) -> str:
        return f"{SLOT_LABELS[self.start]}\u2013{SLOT_LABELS[self.end % SLOTS_PER_DAY]}"

    @classmethod
    def parse(cls, start: str, end: str) -> "TimeWindow":
        """From two `HH:MM` times on the hour or half hour. An end of `00:00` is midnight."""
        slots = []
        for text in (start, end):
            match = _CLOCK.fullmatch(text)
            if match is None:
                raise ValueError(f"Use a time on the hour or half hour, like 22:30: {text!r}")
            slots.append(int(match.group(1)) * 2 + int(match.group(2)) // 30)
        return cls(slots[0], slots[1] or SLOTS_PER_DAY)


# Base load: a low percentile of overnight half-hours, when little but always-on devices run.
DEFAULT_WINDOW = TimeWindow(0, 10)  # 00:00-05:00


@dataclass(frozen=True, slots=True)
class Cell:
    day: date
    slot: int
    kwh: float
    readings: int  # 2 where the clocks went back and the slot happened twice


@dataclass(frozen=True, slots=True)
class SlotStat:
    slot: int
    mean: float | None
    median: float | None
    samples: int


@dataclass(frozen=True, slots=True)
class BaseLoad:
    watts: float
    samples: int


def local_slot(start: datetime) -> tuple[date, int]:
    """UK date and wall-clock slot of a half-hour that starts at `start` (UTC)."""
    local = start.astimezone(LONDON)
    return local.date(), local.hour * 2 + local.minute // 30


def household(rows: Iterable[HalfHourUsage]) -> dict[datetime, float]:
    """kWh per half-hour start, summed across accounts."""
    totals: dict[datetime, float] = {}
    for row in rows:
        totals[row.interval_start] = totals.get(row.interval_start, 0.0) + row.kwh
    return dict(sorted(totals.items()))


def heatmap(half_hours: dict[datetime, float]) -> list[Cell]:
    grouped: dict[tuple[date, int], list[float]] = {}
    for start, kwh in half_hours.items():
        grouped.setdefault(local_slot(start), []).append(kwh)
    return [
        Cell(day, slot, sum(values) / len(values), len(values))
        for (day, slot), values in sorted(grouped.items())
    ]


def typical_day(half_hours: dict[datetime, float], *, weekend: bool) -> list[SlotStat]:
    """Mean and median kWh for each wall-clock slot over weekdays (or weekends)."""
    by_slot: list[list[float]] = [[] for _ in range(SLOTS_PER_DAY)]
    for start, kwh in half_hours.items():
        day, slot = local_slot(start)
        if (day.weekday() >= 5) == weekend:
            by_slot[slot].append(kwh)
    return [
        SlotStat(
            slot,
            statistics.fmean(values) if values else None,
            statistics.median(values) if values else None,
            len(values),
        )
        for slot, values in enumerate(by_slot)
    ]


def night_starts(night: date, window: TimeWindow) -> list[datetime]:
    """The UTC start of every half-hour of `night`'s window, by UK wall-clock time. For a window
    past midnight that's the evening of `night` and the morning after it. On a clock-change
    night that is 46 or 50 slots' worth, which is correct."""
    if not window.wraps:
        return [s for s in london_half_hours(night) if window.contains(local_slot(s)[1])]
    evening = [s for s in london_half_hours(night) if local_slot(s)[1] >= window.start]
    morning = [
        s for s in london_half_hours(night + timedelta(days=1)) if local_slot(s)[1] < window.end
    ]
    return evening + morning


def base_load(
    half_hours: Mapping[datetime, float],
    window: TimeWindow = DEFAULT_WINDOW,
    span: DateRange | None = None,
) -> BaseLoad | None:
    """The BASE_LOAD_PERCENTILE-th percentile (nearest rank) of the half-hours in `window` over
    the nights of `span` (default: the days present), as watts."""
    values = sorted(
        kwh
        for night in _nights(half_hours, span)
        for start in night_starts(night, window)
        if (kwh := half_hours.get(start)) is not None
    )
    if not values:
        return None
    return BaseLoad(
        watts=nearest_rank(values, BASE_LOAD_PERCENTILE) * 2 * 1000, samples=len(values)
    )


@dataclass(frozen=True, slots=True)
class NightBaseLoad:
    night: date
    watts: float | None  # None unless every half-hour of the window is present that night


def nightly_base_load(
    half_hours: Mapping[datetime, float], window: TimeWindow, span: DateRange
) -> list[NightBaseLoad]:
    """Base load for each night of `span`. A night with any half-hour of the window missing is
    shown as unknown, not computed from what's left."""
    result = []
    for night in span.days():
        starts = night_starts(night, window)
        values = sorted(kwh for s in starts if (kwh := half_hours.get(s)) is not None)
        complete = len(values) == len(starts)
        watts = (
            nearest_rank(values, BASE_LOAD_PERCENTILE) * 2 * 1000 if complete and values else None
        )
        result.append(NightBaseLoad(night, watts))
    return result


def _nights(half_hours: Mapping[datetime, float], span: DateRange | None) -> list[date]:
    if span is not None:
        return span.days()
    days = sorted({local_slot(start)[0] for start in half_hours})
    return DateRange(days[0], days[-1]).days() if days else []


def nearest_rank(sorted_values: Sequence[float], percentile: int) -> float:
    if not sorted_values:
        raise ValueError("no values")
    if not 0 < percentile <= 100:
        raise ValueError(f"percentile must be in (0, 100], not {percentile}")
    rank = -(-percentile * len(sorted_values) // 100)  # ceil without floats
    return sorted_values[rank - 1]


def annual_cost(watts: float, unit_rate: MilliPence) -> MilliPence:
    """What a constant load costs over a year at `unit_rate` (milli-pence per kWh)."""
    kwh_per_year = Decimal(str(watts)) / 1000 * HOURS_PER_YEAR
    return MilliPence(round_half_even(kwh_per_year * unit_rate))

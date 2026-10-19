"""What to fetch: active windows, periods and the rules for when a period is final.

Pure functions only (no I/O), so every rule here is unit- and property-tested.

Terms:
- *Active range*: the days an account supplied a fuel, from its supply points. Customers who
  left OVO and came back have several accounts on the same supply points, so each account is
  limited to its own active period.
- *Backfill range*: the active range clipped to the last two years, ending yesterday (OVO's
  data runs to the end of the previous day).
- *Period*: the unit of one request: a year (monthly endpoint), a month (daily endpoint) or a
  UK day (half-hourly endpoint).
- A period is *final* once it is `complete` (data matched expectations and was fetched after
  the revision window) or a known `gap` (still incomplete after the gap horizon). Final
  periods are never fetched again.
"""

import calendar
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from ovo_insights.domain import Fuel, SupplyPoint

HISTORY_YEARS = 2
# OVO may revise recent data, so a period is re-fetched until a fetch made this many days
# after it ends (in practice: each sync re-fetches the last three days).
REVISION_DAYS = 3
# An incomplete period is retried until this many days after it ends, then marked a gap.
GAP_HORIZON_DAYS = 14


class Granularity(StrEnum):
    MONTHLY = "monthly"
    DAILY = "daily"
    HALF_HOURLY = "half_hourly"


class PeriodStatus(StrEnum):
    COMPLETE = "complete"
    PROVISIONAL = "provisional"
    INCOMPLETE = "incomplete"
    GAP = "gap"
    ERROR = "error"


FINAL_STATUSES = frozenset({PeriodStatus.COMPLETE, PeriodStatus.GAP})


@dataclass(frozen=True, slots=True)
class DateRange:
    """Inclusive range of calendar dates."""

    first: date
    last: date

    def __post_init__(self) -> None:
        if self.last < self.first:
            raise ValueError(f"Range ends ({self.last}) before it starts ({self.first})")

    def __contains__(self, day: object) -> bool:
        return isinstance(day, date) and self.first <= day <= self.last

    def clip(self, first: date, last: date) -> "DateRange | None":
        start, end = max(self.first, first), min(self.last, last)
        return DateRange(start, end) if start <= end else None

    def days(self) -> list[date]:
        return [self.first + timedelta(days=i) for i in range((self.last - self.first).days + 1)]


@dataclass(frozen=True, slots=True)
class Period:
    account_id: str
    granularity: Granularity
    start: date

    def __post_init__(self) -> None:
        if self.granularity is Granularity.MONTHLY and (self.start.month, self.start.day) != (1, 1):
            raise ValueError(f"A monthly-endpoint period starts on 1 January, not {self.start}")
        if self.granularity is Granularity.DAILY and self.start.day != 1:
            raise ValueError(f"A daily-endpoint period starts on the 1st, not {self.start}")

    @property
    def last(self) -> date:
        return period_last_day(self.granularity, self.start)

    @property
    def range(self) -> DateRange:
        return DateRange(self.start, self.last)


def period_last_day(granularity: Granularity, start: date) -> date:
    if granularity is Granularity.MONTHLY:
        return date(start.year, 12, 31)
    if granularity is Granularity.DAILY:
        return month_end(start)
    return start


def month_end(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def history_start(today: date) -> date:
    """The same calendar day HISTORY_YEARS ago (29 February maps to 28 February)."""
    year = today.year - HISTORY_YEARS
    return date(year, today.month, min(today.day, calendar.monthrange(year, today.month)[1]))


def period_starts(granularity: Granularity, span: DateRange) -> list[date]:
    """Starts of every period overlapping `span`, newest first."""
    if granularity is Granularity.HALF_HOURLY:
        return span.days()[::-1]
    if granularity is Granularity.MONTHLY:
        return [date(y, 1, 1) for y in range(span.last.year, span.first.year - 1, -1)]
    starts = []
    month = span.last.replace(day=1)
    while month >= span.first.replace(day=1):
        starts.append(month)
        month = (month - timedelta(days=1)).replace(day=1)
    return starts


# -- active windows -----------------------------------------------------------------------


def active_ranges(
    accounts: Mapping[str, Sequence[SupplyPoint]], yesterday: date
) -> dict[str, dict[Fuel, DateRange]]:
    """Days each account supplied each fuel, ending no later than `yesterday`.

    A supply point's end is its explicit end date if OVO gave one. Otherwise, if another
    account starts later on the same meter point (fuel and MPxN), this one is taken to end the
    day before. Otherwise it is still open. A fuel with several supply points spans from the
    earliest start to the latest end.
    """
    later_starts: dict[tuple[Fuel, str], list[date]] = {}
    for points in accounts.values():
        for p in points:
            later_starts.setdefault((p.fuel, p.mpxn), []).append(p.start_date)

    result: dict[str, dict[Fuel, DateRange]] = {}
    for account_id, points in accounts.items():
        spans: dict[Fuel, DateRange] = {}
        for p in points:
            end = p.end_date
            if end is None:
                successors = [s for s in later_starts[(p.fuel, p.mpxn)] if s > p.start_date]
                if successors:
                    end = min(successors) - timedelta(days=1)
            end = yesterday if end is None else min(end, yesterday)
            if end < p.start_date:
                continue
            existing = spans.get(p.fuel)
            if existing is None:
                spans[p.fuel] = DateRange(p.start_date, end)
            else:
                spans[p.fuel] = DateRange(
                    min(existing.first, p.start_date), max(existing.last, end)
                )
        result[account_id] = spans
    return result


def backfill_ranges(active: Mapping[Fuel, DateRange], today: date) -> dict[Fuel, DateRange]:
    """Active ranges clipped to the last HISTORY_YEARS years, ending yesterday."""
    first, last = history_start(today), today - timedelta(days=1)
    clipped = {fuel: span.clip(first, last) for fuel, span in active.items()}
    return {fuel: span for fuel, span in clipped.items() if span is not None}


def union(spans: Iterable[DateRange]) -> DateRange | None:
    """The smallest range covering all of `spans` (None if there are none)."""
    spans = list(spans)
    if not spans:
        return None
    return DateRange(min(s.first for s in spans), max(s.last for s in spans))


# -- planning -----------------------------------------------------------------------------


def plan_account(
    account_id: str,
    backfill: Mapping[Fuel, DateRange],
    states: Mapping[tuple[Granularity, date], PeriodStatus],
) -> list[Period]:
    """Periods still to fetch for one account, newest first.

    Within each month the daily request comes before that month's half-hourly days, because
    half-hourly completeness depends on the daily rows (which say whether each fuel has
    half-hourly data at all). Periods whose last fetch failed validation go last, so one bad
    period can't starve the rest of the backfill.
    """
    span = union(backfill.values())
    if span is None:
        return []
    days_by_month: dict[date, list[date]] = {}
    for day in period_starts(Granularity.HALF_HOURLY, span):
        days_by_month.setdefault(day.replace(day=1), []).append(day)

    ordered = [
        Period(account_id, Granularity.MONTHLY, s) for s in period_starts(Granularity.MONTHLY, span)
    ]
    for month, days in days_by_month.items():  # newest first, as inserted
        ordered.append(Period(account_id, Granularity.DAILY, month))
        ordered.extend(Period(account_id, Granularity.HALF_HOURLY, day) for day in days)
    todo = [p for p in ordered if states.get((p.granularity, p.start)) not in FINAL_STATUSES]
    return sorted(todo, key=lambda p: states.get((p.granularity, p.start)) is PeriodStatus.ERROR)


def expected_keys(
    granularity: Granularity, period: DateRange, backfill: Mapping[Fuel, DateRange]
) -> set[tuple[Fuel, date]]:
    """(fuel, day-or-month) rows a monthly or daily response must contain to be complete."""
    if granularity is Granularity.HALF_HOURLY:
        raise ValueError("Half-hourly expectations depend on the daily rows; see the engine")
    keys: set[tuple[Fuel, date]] = set()
    for fuel, span in backfill.items():
        overlap = span.clip(period.first, period.last)
        if overlap is None:
            continue
        if granularity is Granularity.DAILY:
            keys |= {(fuel, d) for d in overlap.days()}
        else:
            keys |= {(fuel, m) for m in period_starts(Granularity.DAILY, overlap)}
    return keys


def classify(*, complete: bool, last_day: date, today: date) -> PeriodStatus:
    """Status of a period that was just fetched."""
    if complete:
        if today >= last_day + timedelta(days=REVISION_DAYS):
            return PeriodStatus.COMPLETE
        return PeriodStatus.PROVISIONAL
    if today >= last_day + timedelta(days=GAP_HORIZON_DAYS):
        return PeriodStatus.GAP
    return PeriodStatus.INCOMPLETE

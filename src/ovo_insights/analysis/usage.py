"""Usage and cost totals, date ranges and year-on-year alignment.

Pure functions over validated domain rows. Accounts are summed per fuel: a customer who left
OVO and came back is one household. Money stays integer milli-pence; a day without a cost
contributes to kWh but not to money, and is counted, so nothing missing turns into a zero.

OVO's daily `cost` is kWh x unit rate only; the standing charge is separate. "Total cost"
here is both, which is what the customer pays.
"""

import calendar
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum

from ovo_insights.domain import DailyUsage, Fuel
from ovo_insights.money import MilliPence, to_pounds
from ovo_insights.sync.plan import DateRange


class Preset(StrEnum):
    LAST_30_DAYS = "30d"
    LAST_12_MONTHS = "12m"
    THIS_YEAR = "year"
    ALL = "all"


PRESET_LABELS = {
    Preset.LAST_30_DAYS: "Last 30 days",
    Preset.LAST_12_MONTHS: "Last 12 months",
    Preset.THIS_YEAR: "This year",
    Preset.ALL: "All",
}


class Grain(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


@dataclass(frozen=True, slots=True)
class DayTotal:
    """One fuel's household total for one day."""

    day: date
    kwh: float
    usage_cost: MilliPence | None  # kWh x unit rate, as OVO reports it
    standing: MilliPence | None
    unit_rate: MilliPence | None  # per kWh; kWh-weighted if several accounts

    @property
    def total_cost(self) -> MilliPence | None:
        if self.usage_cost is None:
            return None
        return MilliPence(self.usage_cost + (self.standing or 0))


@dataclass(frozen=True, slots=True)
class Bucket:
    start: date
    kwh: float
    usage_cost: MilliPence
    standing: MilliPence
    days: int  # days with data
    costed_days: int  # days whose cost is known (and included above)


@dataclass(frozen=True, slots=True)
class Summary:
    kwh: float
    cost: MilliPence  # usage + standing, over costed days only
    days: int
    costed_days: int

    @property
    def average_cost_per_day(self) -> MilliPence | None:
        if not self.costed_days:
            return None
        return MilliPence(round_half_even(Decimal(self.cost) / self.costed_days))


@dataclass(frozen=True, slots=True)
class Change:
    """Like-for-like change against the same days a year earlier."""

    kwh_percent: Decimal | None
    cost_percent: Decimal | None
    days_compared: int


# -- ranges -------------------------------------------------------------------------------


def one_year_earlier(day: date) -> date:
    """The same calendar day a year earlier (29 February maps to 28 February)."""
    year = day.year - 1
    return date(year, day.month, min(day.day, calendar.monthrange(year, day.month)[1]))


def resolve_range(preset: Preset, today: date, earliest: date | None) -> DateRange:
    """The range a preset means, ending yesterday (OVO's data runs to yesterday)."""
    last = today - timedelta(days=1)
    if preset is Preset.LAST_30_DAYS:
        first = last - timedelta(days=29)
    elif preset is Preset.LAST_12_MONTHS:
        first = one_year_earlier(last) + timedelta(days=1)
    elif preset is Preset.THIS_YEAR:
        first = min(date(today.year, 1, 1), last)
    else:
        first = min(earliest or last, last)
    return DateRange(first, last)


def previous_year(span: DateRange) -> DateRange:
    """The same number of days, starting a year earlier (day-of-year alignment)."""
    first = one_year_earlier(span.first)
    return DateRange(first, first + (span.last - span.first))


def grain_for(span: DateRange) -> Grain:
    days = (span.last - span.first).days + 1
    if days <= 62:
        return Grain.DAY
    if days <= 400:
        return Grain.WEEK
    return Grain.MONTH


# -- totals -------------------------------------------------------------------------------


def household_days(rows: Iterable[DailyUsage], fuel: Fuel) -> list[DayTotal]:
    """Sum one fuel's daily rows across accounts, in date order."""
    by_day: dict[date, list[DailyUsage]] = {}
    for row in rows:
        if row.fuel is fuel:
            by_day.setdefault(row.day, []).append(row)
    result = []
    for day in sorted(by_day):
        group = by_day[day]
        kwh = sum(r.kwh for r in group)
        result.append(
            DayTotal(
                day,
                kwh,
                _sum_or_none([r.cost for r in group]),
                _sum_or_none([r.standing_charge for r in group]),
                _weighted_rate(group),
            )
        )
    return result


def bucket_start(day: date, grain: Grain) -> date:
    if grain is Grain.WEEK:
        return day - timedelta(days=day.weekday())  # ISO weeks start on Monday
    if grain is Grain.MONTH:
        return day.replace(day=1)
    return day


def buckets(days: Iterable[DayTotal], grain: Grain) -> list[Bucket]:
    groups: dict[date, list[DayTotal]] = {}
    for d in days:
        groups.setdefault(bucket_start(d.day, grain), []).append(d)
    return [_bucket(start, groups[start]) for start in sorted(groups)]


def summarise(days: Sequence[DayTotal]) -> Summary:
    costed = [d for d in days if d.total_cost is not None]
    return Summary(
        kwh=sum(d.kwh for d in days),
        cost=MilliPence(sum(d.total_cost or 0 for d in costed)),
        days=len(days),
        costed_days=len(costed),
    )


def compare(current: Sequence[DayTotal], span: DateRange, previous: Sequence[DayTotal]) -> Change:
    """Change against the same period a year earlier, over days present in both."""
    earlier = previous_year(span)
    prior = {d.day: d for d in previous}
    pairs = []
    for d in current:
        if d.day in span:
            match = prior.get(earlier.first + (d.day - span.first))
            if match is not None:
                pairs.append((d, match))
    now_kwh = sum(c.kwh for c, _ in pairs)
    then_kwh = sum(p.kwh for _, p in pairs)
    costed = [(c, p) for c, p in pairs if c.total_cost is not None and p.total_cost is not None]
    now_cost = sum(c.total_cost or 0 for c, _ in costed)
    then_cost = sum(p.total_cost or 0 for _, p in costed)
    return Change(
        _percent(Decimal(str(now_kwh)), Decimal(str(then_kwh))),
        _percent(Decimal(now_cost), Decimal(then_cost)) if costed else None,
        len(pairs),
    )


# -- year on year -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class YearOnYear:
    labels: list[str]
    current: list[float | None]
    previous: list[float | None]
    grain: Grain


def year_on_year(
    current: Sequence[DayTotal], previous: Sequence[DayTotal], span: DateRange
) -> YearOnYear:
    """kWh this period against the same period a year earlier.

    Up to 62 days: day by day, aligned by day offset (the same day of the year). Longer:
    ISO week by ISO week (week 23 against last year's week 23), complete weeks only, since a
    part-week against a whole one would look like a collapse; a week 53 has no partner.
    """
    if grain_for(span) is Grain.DAY:
        earlier = previous_year(span)
        now = {d.day: d.kwh for d in current}
        then = {d.day: d.kwh for d in previous}
        labels, cur, prev = [], [], []
        for offset, day in enumerate(span.days()):
            labels.append(day.isoformat())
            cur.append(now.get(day))
            prev.append(then.get(earlier.first + timedelta(days=offset)))
        return YearOnYear(labels, cur, prev, Grain.DAY)

    now_weeks = _iso_weeks(current)
    then_weeks = _iso_weeks(previous)
    keys = sorted({week for week in now_weeks if _week_in(week, span)})
    labels = [f"{year}-W{week:02d}" for year, week in keys]
    return YearOnYear(
        labels,
        [now_weeks.get(k) for k in keys],
        [then_weeks.get((k[0] - 1, k[1])) for k in keys],
        Grain.WEEK,
    )


# -- formatting ---------------------------------------------------------------------------


def pounds_text(amount: MilliPence | None) -> str | None:
    """Exact decimal pounds to the penny, as a string (charts and tiles never use floats)."""
    if amount is None:
        return None
    return str(to_pounds(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))


def round_half_even(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


# -- helpers ------------------------------------------------------------------------------


def _sum_or_none(values: list[MilliPence | None]) -> MilliPence | None:
    if any(v is None for v in values):
        return None
    return MilliPence(sum(v or 0 for v in values))


def _weighted_rate(group: list[DailyUsage]) -> MilliPence | None:
    rated = [r for r in group if r.unit_rate is not None]
    if not rated:
        return None
    total_kwh = sum(Decimal(str(r.kwh)) for r in rated)
    if total_kwh == 0:
        return rated[0].unit_rate
    weighted = sum(Decimal(str(r.kwh)) * (r.unit_rate or 0) for r in rated)
    return MilliPence(round_half_even(weighted / total_kwh))


def _bucket(start: date, group: list[DayTotal]) -> Bucket:
    costed = [d for d in group if d.usage_cost is not None]
    return Bucket(
        start,
        sum(d.kwh for d in group),
        MilliPence(sum(d.usage_cost or 0 for d in costed)),
        MilliPence(sum(d.standing or 0 for d in costed)),
        len(group),
        len(costed),
    )


def _percent(now: Decimal, then: Decimal) -> Decimal | None:
    if then == 0:
        return None
    return ((now - then) / then * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_EVEN)


def _iso_weeks(days: Sequence[DayTotal]) -> dict[tuple[int, int], float]:
    weeks: dict[tuple[int, int], float] = {}
    for d in days:
        year, week, _ = d.day.isocalendar()
        weeks[(year, week)] = weeks.get((year, week), 0.0) + d.kwh
    return weeks


def _week_in(week: tuple[int, int], span: DateRange) -> bool:
    """Whether the whole ISO week lies inside `span`."""
    monday = date.fromisocalendar(week[0], week[1], 1)
    return monday >= span.first and monday + timedelta(days=6) <= span.last


def bucket_end(start: date, grain: Grain) -> date:
    if grain is Grain.WEEK:
        return start + timedelta(days=6)
    if grain is Grain.MONTH:
        return start.replace(day=calendar.monthrange(start.year, start.month)[1])
    return start


def is_partial(start: date, grain: Grain, span: DateRange) -> bool:
    """Whether a bucket's period extends outside `span` (so it can't be a full period)."""
    return start < span.first or bucket_end(start, grain) > span.last


# -- rates and completeness ---------------------------------------------------------------


def recent_unit_rate(days: Sequence[DayTotal], window: int = 30) -> MilliPence | None:
    """kWh-weighted unit rate over the last `window` days that have one ("current" rate)."""
    rated = [d for d in days if d.unit_rate is not None][-window:]
    total_kwh = sum(Decimal(str(d.kwh)) for d in rated)
    if not rated or total_kwh == 0:
        return rated[-1].unit_rate if rated else None
    weighted = sum(Decimal(str(d.kwh)) * (d.unit_rate or 0) for d in rated)
    return MilliPence(round_half_even(weighted / total_kwh))


def missing_days(
    span: DateRange,
    present: dict[Fuel, set[date]],
    earliest: dict[Fuel, date],
    known_gaps: Iterable[date] = (),
) -> set[date]:
    """Days in `span` with no daily row for a fuel that had data by then, plus known
    half-hourly gaps. Days before a fuel's first data are "not yet supplied", not missing."""
    missing = {d for d in known_gaps if d in span}
    for fuel, first in earliest.items():
        clipped = span.clip(first, span.last)
        if clipped is not None:
            missing |= set(clipped.days()) - present.get(fuel, set())
    return missing

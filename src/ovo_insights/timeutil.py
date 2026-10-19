"""Time handling: UTC for storage, Europe/London for anything a person sees.

Half-hourly data is aligned to UK local half-hours. A local day has 48 of them, except the
spring-forward Sunday (46) and the fall-back Sunday (50). Those are correct days, not gaps.
"""

import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
HALF_HOUR = timedelta(minutes=30)

_UTC_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", re.ASCII)
_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class NaiveDatetimeError(ValueError):
    """A datetime without a timezone was supplied where an instant is required."""


def require_utc(instant: datetime) -> datetime:
    """Return `instant` normalised to UTC; refuse naive datetimes rather than guess a zone."""
    if instant.tzinfo is None or instant.tzinfo.utcoffset(instant) is None:
        raise NaiveDatetimeError(f"Datetime must be timezone-aware: {instant!r}")
    return instant.astimezone(UTC)


def format_utc(instant: datetime) -> str:
    """Canonical storage form: `YYYY-MM-DDTHH:MM:SSZ` (sub-second precision is dropped)."""
    return require_utc(instant).strftime(_UTC_FORMAT)


def parse_utc(text: str) -> datetime:
    """Strict inverse of `format_utc`."""
    if not _UTC_TEXT.fullmatch(text):
        raise ValueError(f"Not a canonical UTC timestamp: {text!r}")
    return datetime.strptime(text, _UTC_FORMAT).replace(tzinfo=UTC)


def london_date(instant: datetime) -> date:
    """The UK calendar date on which `instant` falls."""
    return require_utc(instant).astimezone(LONDON).date()


def london_day_bounds(day: date) -> tuple[datetime, datetime]:
    """UTC start (inclusive) and end (exclusive) of a UK local calendar day."""
    _require_plain_date(day)
    start = datetime.combine(day, time.min, tzinfo=LONDON).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=LONDON).astimezone(UTC)
    return start, end


def london_half_hours(day: date) -> list[datetime]:
    """UTC start of every half-hour in a UK local day: 46, 48 or 50 of them."""
    start, end = london_day_bounds(day)
    count = (end - start) // HALF_HOUR
    return [start + i * HALF_HOUR for i in range(count)]


def is_half_hour_aligned(instant: datetime) -> bool:
    utc = require_utc(instant)
    return utc.minute in (0, 30) and utc.second == 0 and utc.microsecond == 0


def _require_plain_date(day: date) -> None:
    # datetime is a subclass of date; passing one here is almost always a bug.
    if isinstance(day, datetime) or not isinstance(day, date):
        raise TypeError(f"Expected a date, not {type(day).__name__}")

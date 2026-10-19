from datetime import UTC, date, datetime, timedelta, timezone
from itertools import pairwise

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ovo_insights.timeutil import (
    HALF_HOUR,
    LONDON,
    NaiveDatetimeError,
    format_utc,
    is_half_hour_aligned,
    london_date,
    london_day_bounds,
    london_half_hours,
    parse_utc,
    require_utc,
)

dates = st.dates(min_value=date(2015, 1, 1), max_value=date(2035, 12, 31))
aware_instants = st.datetimes(
    # Hypothesis takes naive bounds and attaches the timezone itself.
    min_value=datetime(2015, 1, 1),  # noqa: DTZ001
    max_value=datetime(2035, 12, 31),  # noqa: DTZ001
    timezones=st.just(UTC),
)


def _last_sunday(year: int, month: int) -> date:
    day = date(year, month + 1, 1) - timedelta(days=1)
    return day - timedelta(days=(day.weekday() - 6) % 7)


@pytest.mark.parametrize(
    ("day", "count"),
    [
        (date(2025, 3, 30), 46),  # clocks go forward
        (date(2025, 10, 26), 50),  # clocks go back
        (date(2025, 6, 1), 48),
        (date(2024, 2, 29), 48),
    ],
)
def test_half_hours_per_day(day: date, count: int) -> None:
    assert len(london_half_hours(day)) == count


def test_bst_day_starts_at_2300_utc_previous_day() -> None:
    slots = london_half_hours(date(2025, 7, 1))

    assert slots[0] == datetime(2025, 6, 30, 23, 0, tzinfo=UTC)
    assert slots[-1] == datetime(2025, 7, 1, 22, 30, tzinfo=UTC)


@given(dates)
def test_half_hour_properties(day: date) -> None:
    slots = london_half_hours(day)
    start, end = london_day_bounds(day)

    expected = 48
    if day == _last_sunday(day.year, 3):
        expected = 46
    elif day == _last_sunday(day.year, 10):
        expected = 50
    assert len(slots) == expected
    assert slots[0] == start
    assert slots[-1] + HALF_HOUR == end
    assert all(b - a == HALF_HOUR for a, b in pairwise(slots))
    assert all(is_half_hour_aligned(s) for s in slots)
    assert all(london_date(s) == day for s in slots)
    assert start.astimezone(LONDON).time().hour == 0


@given(dates)
def test_consecutive_days_tile_without_gaps(day: date) -> None:
    _, end = london_day_bounds(day)
    next_start, _ = london_day_bounds(day + timedelta(days=1))

    assert end == next_start


def test_half_hours_rejects_datetime() -> None:
    with pytest.raises(TypeError):
        london_half_hours(datetime(2025, 1, 1, tzinfo=UTC))


def test_half_hours_rejects_non_date() -> None:
    with pytest.raises(TypeError):
        london_half_hours("2025-01-01")  # type: ignore[arg-type]


def test_require_utc_rejects_naive() -> None:
    with pytest.raises(NaiveDatetimeError):
        require_utc(datetime(2025, 1, 1))  # noqa: DTZ001 - deliberately naive


def test_require_utc_normalises_other_zones() -> None:
    plus_one = timezone(timedelta(hours=1))

    assert require_utc(datetime(2025, 1, 1, 1, 0, tzinfo=plus_one)) == datetime(
        2025, 1, 1, tzinfo=UTC
    )
    assert require_utc(datetime(2025, 1, 1, 1, 0, tzinfo=plus_one)).tzinfo is UTC


@given(aware_instants)
def test_format_parse_round_trip(instant: datetime) -> None:
    truncated = instant.replace(microsecond=0)

    assert parse_utc(format_utc(instant)) == truncated


@pytest.mark.parametrize(
    "text",
    [
        "2025-01-01 00:00:00Z",
        "2025-01-01T00:00:00",
        "2025-01-01T00:00:00+00:00",
        "2025-01-01T00:00:00.000Z",
        "2025-13-01T00:00:00Z",
        "",
    ],
)
def test_parse_utc_is_strict(text: str) -> None:
    with pytest.raises(ValueError, match=r"timestamp|does not match|out of range"):
        parse_utc(text)


@pytest.mark.parametrize(
    ("instant", "aligned"),
    [
        (datetime(2025, 1, 1, 0, 0, tzinfo=UTC), True),
        (datetime(2025, 1, 1, 0, 30, tzinfo=UTC), True),
        (datetime(2025, 1, 1, 0, 15, tzinfo=UTC), False),
        (datetime(2025, 1, 1, 0, 0, 1, tzinfo=UTC), False),
        (datetime(2025, 1, 1, 0, 0, 0, 1, tzinfo=UTC), False),
    ],
)
def test_is_half_hour_aligned(instant: datetime, aligned: bool) -> None:
    assert is_half_hour_aligned(instant) is aligned

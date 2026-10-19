"""The base-load window on the Details page, and its per-night chart."""

import math
import re
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from ovo_insights import demo
from ovo_insights.analysis.half_hourly import local_slot
from ovo_insights.storage import open_database
from ovo_insights.timeutil import parse_utc
from ovo_insights.web.app import WebContext

pytestmark = pytest.mark.anyio

TODAY = date(2026, 10, 13)
RANGE = "from=2026-09-01&to=2026-09-30"


@pytest.fixture
def seeded(ctx: WebContext) -> demo.DemoData:
    data = demo.build(7, TODAY)
    conn = open_database(ctx.data_dir)
    try:
        demo.write(conn, data, datetime(2026, 10, 13, 6, tzinfo=UTC))
    finally:
        conn.close()
    ctx.today = lambda: TODAY
    return data


def stored(ctx: WebContext) -> dict[datetime, float]:
    """Every stored electricity half-hour, read straight from the database."""
    conn = open_database(ctx.data_dir)
    try:
        rows = conn.execute(
            "SELECT interval_start_utc, sum(kwh) FROM usage_half_hourly"
            " WHERE fuel = 'electricity' GROUP BY 1"
        ).fetchall()
    finally:
        conn.close()
    return {parse_utc(stamp): kwh for stamp, kwh in rows}


def expected_watts(
    half_hours: dict[datetime, float], first: date, last: date, start: int, end: int
) -> float:
    """Independent of the app: the 10th percentile (nearest rank) over the nights in range."""
    wraps = end <= start
    values = []
    for stamp, kwh in half_hours.items():
        day, slot = local_slot(stamp)
        inside = (slot >= start or slot < end) if wraps else start <= slot < end
        if not inside:
            continue
        night = day - timedelta(days=1) if wraps and slot < end else day
        if first <= night <= last:
            values.append(kwh)
    ordered = sorted(values)
    return ordered[math.ceil(0.1 * len(ordered)) - 1] * 2000


def tile_watts(page: str) -> int:
    match = re.search(r'<span class="label">Base load</span><span class="value">([\d,]+) W', page)
    assert match is not None
    return int(match.group(1).replace(",", ""))


async def test_the_default_window_is_midnight_to_five_and_nothing_extra_is_carried(
    seeded: demo.DemoData, ctx: WebContext, browser: httpx.AsyncClient
) -> None:
    page = (await browser.get(f"/half-hourly?{RANGE}")).text
    assert "lowest 10% of half-hours between 00:00 and 05:00" in page
    assert "(past midnight)" not in page
    assert '<option value="00:00" selected>00:00</option>' in page
    assert '<option value="05:00" selected>05:00</option>' in page
    assert 'data-src="/api/charts/base-load?from=2026-09-01&amp;to=2026-09-30"' in page
    assert 'name="bl_from"' in page
    assert 'name="bl_to"' in page
    assert 'type="hidden" name="bl_' not in page  # nothing chosen, nothing carried
    watts = expected_watts(stored(ctx), date(2026, 9, 1), date(2026, 9, 30), 0, 10)
    assert tile_watts(page) == round(watts)


@pytest.mark.parametrize(
    ("bl_from", "bl_to", "start", "end"),
    [
        ("22:00", "00:00", 44, 48),  # evening, up to midnight
        ("17:00", "22:00", 34, 44),  # the evening peak
        ("10:30", "11:30", 21, 23),  # two half-hours
        ("22:00", "06:00", 44, 12),  # a night past midnight
        ("23:30", "00:30", 47, 1),  # one slot either side of midnight
        ("00:00", "00:00", 0, 48),  # the whole day
    ],
)
async def test_the_figure_for_any_window_matches_an_independent_calculation(
    seeded: demo.DemoData,
    ctx: WebContext,
    browser: httpx.AsyncClient,
    bl_from: str,
    bl_to: str,
    start: int,
    end: int,
) -> None:
    page = (await browser.get(f"/half-hourly?{RANGE}&bl_from={bl_from}&bl_to={bl_to}")).text
    watts = expected_watts(stored(ctx), date(2026, 9, 1), date(2026, 9, 30), start, end)
    assert tile_watts(page) == round(watts)
    assert f"between {bl_from} and {bl_to}" in page
    assert ("(past midnight)" in page) == (end <= start and not (start == 0 and end == 48))


async def test_a_window_past_midnight_is_flagged_and_its_last_night_has_no_figure_yet(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    query = "range=30d&bl_from=22:00&bl_to=06:00"
    page = (await browser.get(f"/half-hourly?{query}")).text
    assert "(past midnight)" in page
    body = (await browser.get(f"/api/charts/base-load?{query}")).json()
    assert body["wraps"] is True
    assert body["window"] == "22:00\u201306:00"
    assert len(body["labels"]) == 30
    assert body["labels"][-1] == "2026-10-12"
    assert body["watts"][-1] is None  # the morning after the last night isn't here yet
    assert all(w is not None for w in body["watts"][:-1] if w is not None)
    default = (await browser.get("/api/charts/base-load?range=30d")).json()
    assert default["wraps"] is False
    assert default["watts"][-1] is not None  # an overnight 00:00-05:00 window is whole


async def test_chart_data_matches_the_database_night_by_night(
    seeded: demo.DemoData, ctx: WebContext, browser: httpx.AsyncClient
) -> None:
    body = (await browser.get(f"/api/charts/base-load?{RANGE}&bl_from=01:00&bl_to=04:00")).json()
    half_hours = stored(ctx)
    assert len(body["watts"]) == 30
    for label, watts in zip(body["labels"], body["watts"], strict=True):
        night = date.fromisoformat(label)
        expected = expected_watts(half_hours, night, night, 2, 8)
        assert watts == pytest.approx(round(expected, 1))


async def test_nights_with_missing_data_are_gaps_not_zeros(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    gone = min(seeded.missing_days)
    body = (await browser.get(f"/api/charts/base-load?from={gone}&to={gone}")).json()
    assert body["watts"] == [None]
    assert body["labels"] == [gone.isoformat()]


async def test_the_chosen_window_survives_changing_the_dates(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    page = (await browser.get("/half-hourly?range=12m&bl_from=22:00&bl_to=06:00")).text
    for preset in ("30d", "12m", "year", "all"):
        assert f'href="?range={preset}&amp;bl_from=22%3A00&amp;bl_to=06%3A00"' in page
    assert '<input type="hidden" name="bl_from" value="22:00">' in page
    assert '<input type="hidden" name="bl_to" value="06:00">' in page
    # And the dates survive changing the window.
    assert '<input type="hidden" name="range" value="12m">' in page
    assert (
        'data-src="/api/charts/base-load?range=12m&amp;bl_from=22%3A00&amp;bl_to=06%3A00"' in page
    )
    custom = (await browser.get(f"/half-hourly?{RANGE}&bl_from=22:00&bl_to=06:00")).text
    assert '<input type="hidden" name="from" value="2026-09-01">' in custom
    assert '<input type="hidden" name="to" value="2026-09-30">' in custom
    assert '<option value="22:00" selected>22:00</option>' in custom
    assert '<option value="06:00" selected>06:00</option>' in custom


async def test_the_chart_heading_is_baseload_and_the_help_text_is_gone(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    page = (await browser.get(f"/half-hourly?{RANGE}")).text
    assert "<h2>Baseload</h2>" in page
    assert "Base load each night" not in page
    assert "Pick any times" not in page
    assert "left blank in the chart" not in page


async def test_the_midnight_end_is_labelled_and_selectable(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    page = (await browser.get(f"/half-hourly?{RANGE}&bl_from=22:00&bl_to=00:00")).text
    assert '<option value="00:00" selected>00:00 (midnight)</option>' in page
    assert "between 22:00 and 00:00" in page


@pytest.mark.parametrize(
    "query",
    [
        "bl_from=22:00",  # one without the other
        "bl_to=06:00",
        "bl_from=06:00&bl_to=06:00",  # empty
        "bl_from=22:15&bl_to=06:00",  # not on the half hour
        "bl_from=24:00&bl_to=06:00",
        "bl_from=7:00&bl_to=08:00",
        "bl_from=&bl_to=06:00",
        "bl_from=22:00&bl_to=late",
        "bl_from=22:00&bl_to=06:00:00",
    ],
)
async def test_bad_windows_are_refused_on_the_page_and_the_chart(
    seeded: demo.DemoData, browser: httpx.AsyncClient, query: str
) -> None:
    assert (await browser.get(f"/half-hourly?{query}")).status_code == 400
    assert (await browser.get(f"/api/charts/base-load?{query}")).status_code == 400


async def test_the_window_form_is_there_even_with_no_data_and_the_page_still_works(
    ctx: WebContext, browser: httpx.AsyncClient
) -> None:
    ctx.today = lambda: TODAY
    page = (await browser.get("/half-hourly?bl_from=22:00&bl_to=06:00")).text
    assert "No half-hourly electricity data in this range." in page
    assert 'name="bl_from"' in page
    assert "<h2>Baseload</h2>" not in page  # nothing to chart
    assert (await browser.get("/api/charts/base-load?bl_from=22:00&bl_to=06:00")).status_code == 200


async def test_the_chart_script_has_a_builder_for_the_new_chart(
    browser: httpx.AsyncClient,
) -> None:
    source = (await browser.get("/static/charts.js")).text
    assert "baseload: function" in source
    assert "no figure: half-hours missing in the window" in source

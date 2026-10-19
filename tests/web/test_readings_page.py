"""The Readings & gaps page and its chart data, over the demo dataset with a fixed date."""

import re
from datetime import UTC, date, datetime

import httpx
import pytest

from ovo_insights import demo
from ovo_insights.analysis import queries
from ovo_insights.analysis.completeness import DayState
from ovo_insights.storage import open_database
from ovo_insights.sync.plan import DateRange
from ovo_insights.timeutil import london_half_hours
from ovo_insights.web import readings
from ovo_insights.web.app import WebContext

pytestmark = pytest.mark.anyio

TODAY = date(2026, 10, 13)
ELECTRICITY, GAS, OLD_ELECTRICITY, OLD_GAS = 1, 3, 2, 4  # picker numbers (see choices())


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


def rows(page: str, table: int = 0) -> list[list[str]]:
    """The cell texts of every body row of the n-th table on the page."""
    body = re.findall(r"<tbody>(.*?)</tbody>", page, re.DOTALL)[table]
    return [
        [
            re.sub(r"<[^>]+>|\s+", " ", cell).strip()
            for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
        ]
        for row in re.findall(r"<tr>(.*?)</tr>", body, re.DOTALL)
    ]


async def test_page_shows_calendar_summary_and_readings(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    response = await browser.get("/readings")
    page = response.text
    assert response.status_code == 200
    assert "<h1>Readings &amp; gaps</h1>" in page
    assert "<h2>Data completeness</h2>" in page
    assert 'aria-label="Electricity completeness by day"' in page
    assert 'aria-label="Gas completeness by day"' in page
    assert "Electricity: half-hour coverage" in page
    assert "no half-hourly data expected for this fuel" not in page.split("Gas: half-hour")[0]
    assert "Gas: half-hour coverage" in page
    assert "Electricity meter 1, register 1 (active, KWH)" in page
    assert "Gas meter 1 (active, SCMH)" in page
    assert "Electricity meter 2, register 01 (removed, KWH)" in page
    assert 'data-chart="readings" data-src="/api/charts/readings?r=1&amp;range=12m"' in page


async def test_page_never_shows_meter_serials_or_supply_numbers(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    for url in ("/readings", "/readings?r=3", "/readings?range=all&r=4"):
        page = (await browser.get(url)).text
        for identifier in (
            demo.ELECTRIC_METER,
            demo.GAS_METER,
            demo.OLD_ELECTRIC_METER,
            demo.OLD_GAS_METER,
            demo.MPAN,
            demo.MPRN,
            demo.ACCOUNT,
        ):
            assert identifier not in page


async def test_calendar_cells_match_the_dashboards_missing_days(
    seeded: demo.DemoData, ctx: WebContext
) -> None:
    """The calendar and the "N days missing" banners agree about which days have gaps."""
    conn = open_database(ctx.data_dir)
    try:
        span = DateRange(date(2024, 10, 13), date(2026, 10, 12))
        calendars = readings.calendars(conn, span, TODAY)
        flagged = {
            cell.day
            for calendar in calendars
            for cell in calendar.completeness.cells
            if cell.state in (DayState.MISSING, DayState.PARTIAL)
        }
        assert flagged == queries.missing_in(conn, span)
        assert flagged == seeded.missing_days | seeded.short_days
    finally:
        conn.close()


async def test_calendar_has_a_cell_per_day_per_fuel_plus_today(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    page = (await browser.get("/readings?range=30d")).text
    calendar = re.findall(r'<div class="calendar".*?</div>', page, re.DOTALL)
    assert len(calendar) == 2
    for fuel in calendar:
        assert fuel.count('role="listitem"') == 31  # 30 days and today
        assert "13 Oct 2026: not yet available" in fuel
        assert "cell not-yet" in fuel


async def test_partial_and_missing_cells_say_what_is_wrong(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    page = (await browser.get("/readings?range=all")).text
    short = min(seeded.short_days)
    slots = len(london_half_hours(short))
    assert f"{short.day} {short:%b %Y}: partial ({slots - 2} of {slots} half-hours)" in page
    gone = min(seeded.missing_days)
    assert f"{gone.day} {gone:%b %Y}: missing" in page
    assert "cell partial" in page
    assert "cell missing" in page


async def test_the_first_calendar_cell_is_offset_to_its_weekday(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    page = (await browser.get("/readings?from=2026-10-07&to=2026-10-12")).text  # a Wednesday
    assert "cell complete start-2" in page
    assert page.count("start-2") == 2  # one per fuel, and only on the first cell


async def test_the_calendar_says_when_data_starts_after_the_range_begins(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    page = (await browser.get("/readings?from=2020-01-01&to=2025-01-31")).text
    assert "earlier days were never downloaded and aren't shown" in page
    assert "13 Oct 2024" in page


async def test_range_with_no_usage_data_still_shows_readings(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    page = (await browser.get("/readings?from=2020-01-01&to=2021-12-31&r=2")).text
    assert "No usage data in this range yet" in page
    assert "Electricity meter 2, register 01" in page
    assert "MANUAL READ CUSTOMER" not in page  # shown lower-case
    assert "manual read customer" in page


async def test_readings_table_is_sortable_both_ways(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    newest = rows((await browser.get("/readings?r=1")).text)
    assert newest[0][0] == "13 Oct 2026"  # the range ends yesterday, so today's reading closes it
    page = (await browser.get("/readings?r=1&sort=date&dir=asc")).text
    assert rows(page)[0][0] == "13 Oct 2025"
    assert 'aria-sort="ascending"' in page
    page = (await browser.get("/readings?r=1&sort=value&dir=desc")).text
    values = [float(r[1]) for r in rows(page)]
    assert values == sorted(values, reverse=True)
    assert 'aria-sort="descending"' in page
    # The header link for the sorted column toggles; other columns start sensibly.
    assert (
        'href="?range=12m&amp;r=1&amp;sort=date&amp;dir=desc'
        in (await browser.get("/readings?r=1&sort=date&dir=asc")).text
    )


async def test_sorting_by_text_columns_puts_blanks_last(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    page = (await browser.get("/readings?range=all&r=1&sort=source&dir=asc")).text
    sources = [r[2] for r in rows(page)]
    assert sources[0] == "manual read agent"
    assert sources[-1] == "smart meter"
    page = (await browser.get("/readings?range=all&r=1&sort=type&dir=desc")).text
    assert next(r[4] for r in rows(page)) == "estimate"


async def test_readings_are_paginated_and_pages_clamp(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    first = (await browser.get("/readings?r=1")).text
    assert len(rows(first)) == readings.PAGE_SIZE
    assert "Page 1 of 4" in first  # 366 readings
    assert "Previous" not in first.split("Readings against smart data")[0]
    second = (await browser.get("/readings?r=1&rpage=2")).text
    assert "Page 2 of 4" in second
    assert rows(second)[0] != rows(first)[0]
    last = (await browser.get("/readings?r=1&rpage=999")).text
    assert "Page 4 of 4" in last
    assert (await browser.get("/readings?r=1&rpage=0")).status_code == 200


@pytest.mark.parametrize(
    "query",
    [
        "r=0",
        "r=9",
        "r=abc",
        "r=-1",
        "sort=nope",
        "dir=sideways",
        "show=everything",
        "rpage=abc",
        "cpage=-1",
        "rpage=1234567",
        "range=forever",
        "from=2025-01-01",
        "from=2025-02-30&to=2025-03-01",
    ],
)
async def test_bad_parameters_are_refused(
    seeded: demo.DemoData, browser: httpx.AsyncClient, query: str
) -> None:
    assert (await browser.get(f"/readings?{query}")).status_code == 400


async def test_electricity_comparison_flags_the_bad_estimate_and_the_reading_that_went_down(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    page = (await browser.get("/readings?range=all&r=1&show=problems")).text
    problems = rows(page, 1)
    results = [r[-1] for r in problems]
    assert sum(1 for r in results if r == "Differs") == 1
    assert sum(1 for r in results if r.startswith("Reading went down")) == 1
    assert sum(1 for r in results if r == "Smart data has gaps") == len(seeded.missing_days)
    flagged = next(r for r in problems if r[-1] == "Differs")
    assert "+25.000 kWh" in flagged[5]
    assert "Flagged when they differ by more than 5%" in page


async def test_all_periods_lists_matches_with_both_figures(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    page = (await browser.get("/readings?r=1&range=30d")).text
    table = rows(page, 1)
    assert len(table) == 31  # windows ending 30 days ago to today (the range ends today)
    assert table[0][1] == "13 Oct 2026"  # newest first
    kwh = [r for r in table if r[-1] == "Matches"]
    assert len(kwh) == len(table)
    assert all(r[3] and r[4] and "+0.000 kWh" in r[5] for r in kwh)


async def test_comparison_can_be_paged_and_filtered(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    page = (await browser.get("/readings?range=all&r=1&cpage=2")).text
    assert "Page 2 of 8" in page.split("Readings against smart data")[1]
    problems = (await browser.get("/readings?range=all&r=1&show=problems")).text
    assert 'aria-current="true">Flagged and not comparable' in problems
    assert "Page 1 of" not in problems.split("Readings against smart data")[1]


async def test_gas_comparison_shows_volume_and_implied_calorific_value(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    page = (await browser.get(f"/readings?range=all&r={GAS}")).text
    assert "Implied CV (MJ/m³)" in page
    assert "37.5 to 43.0 MJ/m³" in page
    table = rows(page, 1)
    ok = [r for r in table if r[-1] == "Matches"]
    assert ok
    assert all(37.5 <= float(r[5]) <= 43.0 for r in ok)
    flagged = [r for r in table if r[-1] == "Differs"]
    assert len(flagged) in (1, 2)  # both sides of the bad estimate (one may hit a gap)
    assert all(not 37.5 <= float(r[5]) <= 43.0 for r in flagged)
    assert "unit is" not in page


async def test_removed_meters_have_no_smart_data_to_compare(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    page = (await browser.get(f"/readings?range=all&r={OLD_ELECTRICITY}")).text
    results = {r[-1] for r in rows(page, 1)}
    assert results == {"No smart data for these days"}
    assert "Compared" in page
    assert 'data-chart="readings"' not in page  # nothing to draw
    page = (await browser.get(f"/readings?range=all&r={OLD_GAS}")).text
    assert {r[-1] for r in rows(page, 1)} == {"No smart data for these days"}
    only_problems = (await browser.get(f"/readings?range=all&r={OLD_GAS}&show=problems")).text
    assert "No periods to show." in only_problems


async def test_chart_data_for_electricity_and_gas(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    electricity = (await browser.get("/api/charts/readings?r=1&range=all")).json()
    assert electricity["kind"] == "electricity"
    n = len(electricity["labels"])
    assert (
        n == len(electricity["derived"]) == len(electricity["smart"]) == len(electricity["flagged"])
    )
    # Every window except those spanning a day missing from the smart data, or whose reading
    # went down, has both figures: 730 windows, 3 gaps, 1 drop.
    assert n == 730 - len(seeded.missing_days) - 1
    assert sum(electricity["flagged"]) == 1
    gas = (await browser.get(f"/api/charts/readings?r={GAS}&range=all")).json()
    assert gas["kind"] == "gas"
    assert (gas["low"], gas["high"]) == (37.5, 43.0)
    assert len(gas["implied"]) == len(gas["labels"]) == len(gas["flagged"])
    assert all(isinstance(v, float) for v in gas["implied"])


async def test_chart_data_refusals(seeded: demo.DemoData, browser: httpx.AsyncClient) -> None:
    assert (await browser.get("/api/charts/readings?r=9")).status_code == 400
    assert (await browser.get("/api/charts/readings?r=x")).status_code == 400
    assert (await browser.get("/api/charts/readings?r=1&range=bad")).status_code == 400
    assert (await browser.get("/api/charts/readings")).status_code == 422  # r is required


async def test_chart_is_empty_for_a_register_without_comparable_windows(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    body = (await browser.get(f"/api/charts/readings?r={OLD_ELECTRICITY}&range=all")).json()
    assert body["labels"] == []
    assert body["derived"] == []


async def test_page_and_chart_with_nothing_stored(
    ctx: WebContext, browser: httpx.AsyncClient
) -> None:
    ctx.today = lambda: TODAY
    page = (await browser.get("/readings")).text
    assert "No usage data in this range yet. Run a sync first." in page
    assert "No meter readings are stored yet. Run a sync first." in page
    assert (await browser.get("/api/charts/readings?r=1")).status_code == 404


async def test_a_range_ending_yesterday_includes_todays_reading_and_others_do_not(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    today = rows((await browser.get("/readings?r=1&range=30d&sort=date&dir=desc")).text)
    assert today[0][0] == "13 Oct 2026"
    earlier = rows((await browser.get("/readings?r=1&from=2026-09-01&to=2026-10-01")).text)
    assert earlier[0][0] == "1 Oct 2026"


async def test_calendar_squares_show_their_details_on_hover_without_relying_on_title(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    page = (await browser.get("/readings?range=30d")).text
    assert '<script src="/static/calendar.js" defer></script>' in page
    for fuel in ("electricity", "gas"):
        assert f'data-detail-id="detail-{fuel}"' in page
        assert f'id="detail-{fuel}"' in page
    cells = re.findall(r'<span class="cell [^"]*"[^>]*role="listitem"[^>]*>', page)
    assert len(cells) == 62  # 31 days for each fuel
    for cell in cells:
        title = re.search(r'title="([^"]*)"', cell)
        detail = re.search(r'data-detail="([^"]*)"', cell)
        assert title is not None
        assert detail is not None
        assert title.group(1) == detail.group(1)  # the same words, shown two ways
    assert "Hover over or tap a square" in page
    assert "Hover a square for details" not in page


async def test_the_calendar_script_is_served_and_uses_no_unsafe_apis(
    browser: httpx.AsyncClient,
) -> None:
    response = await browser.get("/static/calendar.js")
    assert response.status_code == 200
    source = response.text
    assert "textContent" in source
    for unsafe in ("innerHTML", "eval(", "document.write", "outerHTML", "insertAdjacentHTML"):
        assert unsafe not in source

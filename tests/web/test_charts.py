"""Dashboard, half-hourly page and chart JSON, over the demo dataset with a fixed date."""

from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from ovo_insights import demo
from ovo_insights.storage import open_database
from ovo_insights.web.app import WebContext, gbp, uk_date
from tests.web.conftest import form

pytestmark = pytest.mark.anyio

TODAY = date(2026, 10, 13)


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


async def test_dashboard_tiles_and_charts(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    page = (await browser.get("/?range=12m")).text

    assert '<a href="?range=12m" aria-current="true">Last 12 months</a>' in page
    assert "13 Oct 2025 to 12 Oct 2026" in page
    assert "<h2>Electricity</h2>" in page
    assert "<h2>Gas</h2>" in page
    assert "incl. standing charge" in page
    assert 'data-chart="usage" data-src="/api/charts/usage?fuel=electricity&amp;range=12m"' in page
    assert '<script src="/static/vendor/echarts-6.1.0.min.js" defer></script>' in page
    missing = len({d for d in seeded.missing_days | seeded.short_days if d >= date(2025, 10, 13)})
    if missing:
        assert f"{missing} day" in page
        assert "missing in this range" in page


async def test_usage_json_sends_money_as_exact_strings(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    data = (await browser.get("/api/charts/usage?fuel=electricity&range=12m")).json()

    assert data["grain"] == "week"
    # 13 Oct 2025 is a Monday, so the first week is whole; the last runs past 12 Oct 2026.
    assert (data["partial"][0], data["partial"][-1]) == (False, True)
    assert all(isinstance(v, str) for v in data["usage_cost"])
    assert all(Decimal(v).as_tuple().exponent == -2 for v in data["usage_cost"])
    assert isinstance(data["kwh"][0], float)
    # The chart's money adds up to the rows it came from, to the penny.
    rows = [
        r
        for r in seeded.daily
        if r.fuel.value == "electricity" and date(2025, 10, 13) <= r.day <= date(2026, 10, 12)
    ]
    assert sum(Decimal(v) for v in data["usage_cost"]) == sum(
        Decimal(r.cost or 0) / 100_000 for r in rows
    )


async def test_usage_json_by_day_and_by_month(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    by_day = (await browser.get("/api/charts/usage?fuel=gas&range=30d")).json()
    by_month = (await browser.get("/api/charts/usage?fuel=gas&range=all")).json()

    assert by_day["grain"] == "day"
    assert len(by_day["labels"]) == 30
    assert not any(by_day["partial"])
    assert by_month["grain"] == "month"
    assert by_month["labels"][0] == "2024-10-01"
    assert by_month["partial"][0] is True  # starts on 13 Oct


async def test_year_on_year_json(seeded: demo.DemoData, browser: httpx.AsyncClient) -> None:
    data = (await browser.get("/api/charts/yoy?fuel=electricity&range=12m")).json()

    assert data["grain"] == "week"
    assert data["labels"][0] == "2025-W42"  # the first whole ISO week in range
    assert data["current_name"] == "13 Oct 2025 to 12 Oct 2026"
    assert data["previous_name"] == "13 Oct 2024 to 12 Oct 2025"
    assert data["previous"][1] is not None


async def test_half_hourly_page_and_json(seeded: demo.DemoData, browser: httpx.AsyncClient) -> None:
    page = (await browser.get("/half-hourly")).text  # default: last 30 days
    heat = (await browser.get("/api/charts/heatmap?range=12m")).json()
    profile = (await browser.get("/api/charts/profile?range=30d")).json()
    gas = (await browser.get("/api/charts/gas-daily?range=30d")).json()

    assert '<a href="?range=30d" aria-current="true">' in page
    assert "Base load" in page
    assert " W</span>" in page
    assert "at your recent 27.03p/kWh" in page  # the demo's rate after its price rise
    assert 'data-chart="heatmap"' in page
    assert 'data-chart="gas"' in page
    assert len(heat["days"]) == 365
    assert len(heat["slots"]) == 48
    by_day: dict[int, int] = {}
    for x, _slot, _kwh, _readings in heat["cells"]:
        by_day[x] = by_day.get(x, 0) + 1
    spring = heat["days"].index("2026-03-29")
    autumn = heat["days"].index("2025-10-26")
    assert by_day[spring] == 46  # 01:00 and 01:30 never happened
    assert by_day[autumn] == 48  # they happened twice: averaged into 48 cells
    assert {c[3] for c in heat["cells"] if c[0] == autumn} == {1, 2}
    assert profile["weekday_mean"][0] is not None
    assert len(gas["labels"]) == 30


@pytest.mark.parametrize(
    "path",
    [
        "/?range=forever",
        "/half-hourly?range=",
        "/api/charts/usage?fuel=water&range=12m",
        "/api/charts/heatmap?range=1y",
    ],
)
async def test_bad_parameters_are_refused(
    ctx: WebContext, browser: httpx.AsyncClient, path: str
) -> None:
    response = await browser.get(path)

    assert response.status_code == 400
    if not path.startswith("/api"):
        assert "Bad request" in response.text


async def test_pages_without_data(browser: httpx.AsyncClient) -> None:
    dashboard = (await browser.get("/")).text
    half_hourly = (await browser.get("/half-hourly")).text
    usage = (await browser.get("/api/charts/usage?fuel=electricity")).json()

    assert "No data yet — run a sync" in dashboard
    assert "No half-hourly electricity data in this range." in half_hourly
    assert 'data-chart="gas"' not in half_hourly
    assert usage["labels"] == []


async def test_a_range_without_usage_says_so(
    ctx: WebContext, seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    ctx.today = lambda: date(2030, 1, 1)  # long after the demo data ends

    page = (await browser.get("/?range=30d")).text

    assert "No usage in this range." in page


# -- demo mode ----------------------------------------------------------------------------


async def test_demo_mode_refuses_login_and_sync(
    ctx: WebContext, browser: httpx.AsyncClient
) -> None:
    ctx.demo = True

    page = (await browser.get("/")).text
    setup = await browser.post("/setup", headers=form(ctx), content="email=a%40b.com&password=x")
    sync = await browser.post("/sync", headers=form(ctx))
    forget = await browser.post("/settings/forget", headers=form(ctx))

    assert "Demo mode: this is made-up data" in page
    assert "No OVO login is saved yet" not in page
    for response in (setup, sync, forget):
        assert "Not available in demo mode" in response.text
    assert not ctx.sync.running


def test_template_filters() -> None:
    assert uk_date(date(2026, 3, 9)) == "9 Mar 2026"
    assert gbp("1412.26") == "1,412.26"
    assert gbp("-0.05") == "-0.05"


def test_base_load_tile_without_a_unit_rate() -> None:
    from ovo_insights.analysis.half_hourly import BaseLoad
    from ovo_insights.money import MilliPence
    from ovo_insights.web.charts import BaseLoadTile

    unknown = BaseLoadTile(BaseLoad(206.4, 300), None)
    known = BaseLoadTile(BaseLoad(206.4, 300), MilliPence(27_030))

    assert (unknown.watts_text, unknown.per_year_text, unknown.rate_text) == ("206", None, None)
    assert (known.per_year_text, known.rate_text) == ("488.72", "27.03")


# -- custom dates -------------------------------------------------------------------------


async def test_custom_dates_on_the_dashboard(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    page = (await browser.get("/?from=2026-02-01&to=2026-02-28")).text

    assert "Showing 1 Feb 2026 to 28 Feb 2026 (your dates)." in page
    assert 'aria-current="true"' not in page.split('class="ranges"')[1].split("</nav>")[0]
    assert 'class="secondary current">Show these dates' in page
    assert 'name="from" value="2026-02-01" min="2024-10-13" max="2026-10-12"' in page
    assert (
        'data-src="/api/charts/usage?fuel=electricity&amp;from=2026-02-01&amp;to=2026-02-28"'
        in page
    )
    assert '<a href="?range=12m">Last 12 months</a>' in page  # presets still one click away


async def test_custom_dates_drive_the_chart_data(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    usage = (await browser.get("/api/charts/usage?fuel=gas&from=2026-02-01&to=2026-02-28")).json()
    yoy = (await browser.get("/api/charts/yoy?fuel=gas&from=2026-02-01&to=2026-02-28")).json()
    heat = (await browser.get("/api/charts/heatmap?from=2026-03-28&to=2026-03-30")).json()

    assert usage["grain"] == "day"
    assert usage["labels"][0] == "2026-02-01"
    assert usage["labels"][-1] == "2026-02-28"
    assert yoy["previous_name"] == "01 Feb 2025 to 28 Feb 2025"
    assert heat["days"] == ["2026-03-28", "2026-03-29", "2026-03-30"]


async def test_custom_dates_on_the_half_hourly_page(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    page = (await browser.get("/half-hourly?from=2026-07-01&to=2026-07-14")).text

    assert "Showing 1 Jul 2026 to 14 Jul 2026 (your dates)." in page
    assert 'data-src="/api/charts/heatmap?from=2026-07-01&amp;to=2026-07-14"' in page
    assert 'action="/half-hourly"' in page


@pytest.mark.parametrize(
    ("query", "shown"),
    [
        # Nothing exists after yesterday or before the first data, so the range is trimmed.
        ("from=2026-10-01&to=2027-01-01", "1 Oct 2026 to 12 Oct 2026"),
        ("from=2001-01-01&to=2024-10-20", "13 Oct 2024 to 20 Oct 2024"),
        ("from=2026-05-05&to=2026-05-05", "5 May 2026 to 5 May 2026"),
    ],
)
async def test_custom_dates_are_trimmed_to_the_data(
    seeded: demo.DemoData, browser: httpx.AsyncClient, query: str, shown: str
) -> None:
    page = (await browser.get(f"/?{query}")).text

    assert f"Showing {shown} (your dates)." in page


@pytest.mark.parametrize(
    "query",
    [
        "from=2026-02-01",  # both are needed
        "to=2026-02-01",
        "from=2026-2-1&to=2026-02-28",  # not YYYY-MM-DD
        "from=2026-02-30&to=2026-03-01",  # no such day
        "from=\uff12\uff10\uff12\uff16-02-01&to=2026-02-28",  # full-width digits
        "from=2026-03-01&to=2026-02-01",  # backwards
        "from=1999-12-31&to=2026-02-01",  # implausibly early
        "from=2027-01-01&to=2027-02-01",  # entirely in the future
    ],
)
async def test_bad_custom_dates_are_refused(
    seeded: demo.DemoData, browser: httpx.AsyncClient, query: str
) -> None:
    page = await browser.get(f"/?{query}")
    data = await browser.get(f"/api/charts/usage?fuel=gas&{query}")

    assert page.status_code == data.status_code == 400


async def test_custom_dates_without_any_data(ctx: WebContext, browser: httpx.AsyncClient) -> None:
    ctx.today = lambda: TODAY

    page = (await browser.get("/half-hourly?from=2026-01-01&to=2026-01-31")).text

    assert "Showing 1 Jan 2026 to 31 Jan 2026 (your dates)." in page
    assert 'name="from" value="2026-01-01" max="2026-10-12"' in page  # no min without data
    assert "No half-hourly electricity data in this range." in page

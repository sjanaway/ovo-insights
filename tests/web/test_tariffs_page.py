"""The Tariffs page: the editor, saving, and the comparison, over the demo dataset."""

import itertools
import re
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from ovo_insights import demo
from ovo_insights.analysis.recost import Skip, columns_for, compare
from ovo_insights.domain import Fuel
from ovo_insights.money import MilliPence, format_pounds
from ovo_insights.storage import open_database
from ovo_insights.storage import tariffs as stored
from ovo_insights.sync.plan import DateRange
from ovo_insights.tariffs import Band, Tariff, build_tariff
from ovo_insights.web import tariffs as view
from ovo_insights.web.app import WebContext
from tests.web.conftest import ORIGIN, csrf, form

pytestmark = pytest.mark.anyio

TODAY = date(2026, 10, 13)
WHOLE_RANGE = DateRange(date(2024, 10, 13), date(2026, 10, 12))
EVERY_DAY = {f"band0_day{d}": "1" for d in range(7)}


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


def flat_form(name: str = "Mine", rate: str = "25.5", **extra: str) -> dict[str, str]:
    return {
        "name": name,
        "fuel": "electricity",
        "standing": "50",
        "effective_from": "",
        "effective_to": "",
        "band0_start": "00:00",
        "band0_end": "00:00",
        "band0_rate": rate,
        **EVERY_DAY,
        **extra,
    }


async def post(
    browser: httpx.AsyncClient, ctx: WebContext, url: str, data: dict[str, str]
) -> httpx.Response:
    return await browser.post(url, data=data, headers=form(ctx))


def stored_tariffs(ctx: WebContext) -> list[Tariff]:
    conn = open_database(ctx.data_dir)
    try:
        return stored.all_tariffs(conn)
    finally:
        conn.close()


# -- the page and the comparison ----------------------------------------------------------


async def test_empty_page_offers_templates_and_no_comparison(browser: httpx.AsyncClient) -> None:
    page = (await browser.get("/tariffs")).text
    assert "No tariffs yet. Add one to start comparing." in page
    assert "Add a tariff to compare it with what you actually paid." in page
    for key in view.TEMPLATES:
        assert f'href="/tariffs?new={key}"' in page
    assert "Economy 7 (example)" in page
    assert "Gas</a>" in page
    assert 'data-chart="tariffs"' not in page


async def test_demo_tariffs_are_listed_with_their_rates_and_dates(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    page = (await browser.get("/tariffs")).text
    assert page.count("<td>OVO standard</td>") == 4  # two versions for each fuel
    assert "<td>Economy 7 (example)</td>" in page
    assert "2 rates, 11.5p to 31p/kWh" in page
    assert "3 rates, 8p to 41p/kWh" not in page  # the EV tariff has four rates
    assert "4 rates, 8p to 41p/kWh" in page
    assert "always" in page
    assert "24.5p/kWh" in page
    assert 'href="/tariffs?edit=' in page
    assert 'hx-confirm="Delete the tariff OVO standard?"' in page


async def test_a_tariff_with_ovos_own_rates_costs_exactly_what_ovo_charged(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    page = (await browser.get("/tariffs?from=2024-10-13&to=2026-10-12")).text
    assert page.count('<th scope="row">OVO standard</th>') == 2  # one per fuel
    # The sample "OVO standard" versions have the demo's own rates, so the difference is nil.
    assert page.count("£0.00 (+0.0%)") == 2
    assert "cheaper" in page  # the example gas tariff is cheaper


async def test_page_figures_equal_the_engines(
    seeded: demo.DemoData, ctx: WebContext, browser: httpx.AsyncClient
) -> None:
    from ovo_insights.analysis import queries

    conn = open_database(ctx.data_dir)
    try:
        days = queries.costing_days(conn, Fuel.ELECTRICITY, WHOLE_RANGE)
        result = compare(days, columns_for(stored.all_tariffs(conn), Fuel.ELECTRICITY))
    finally:
        conn.close()
    page = (await browser.get("/tariffs?from=2024-10-13&to=2026-10-12")).text
    assert format_pounds(result.actual.total) in page
    for column in result.columns:
        assert format_pounds(column.totals.total) in page
    assert f"Compared over {len(result.included)} of {result.days_considered} days" in page


async def test_days_a_tariff_cant_cost_are_left_out_of_every_figure_with_reasons(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    page = (await browser.get("/tariffs?from=2024-10-13&to=2026-10-12")).text
    electricity = page.split("<h2>Electricity</h2>")[1].split("<h2>Gas</h2>")[0]
    # Missing days have no daily figure; short days lack half-hours for the time-of-day tariffs.
    assert "left out of every figure" in electricity
    assert f"{len(seeded.missing_days)}: no daily figure stored" in electricity
    assert f"{len(seeded.short_days)}: half-hours incomplete or not available" in electricity
    gas = page.split("<h2>Gas</h2>")[1]
    assert f"{len(seeded.missing_days)}: no daily figure stored" in gas
    assert "half-hours incomplete" not in gas  # gas tariffs never need them


async def test_a_range_nothing_can_be_costed_in_says_so(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    day = min(seeded.missing_days)
    page = (await browser.get(f"/tariffs?from={day}&to={day}")).text
    assert "No day in this range can be costed by all of these tariffs" in page
    assert "no daily figure stored" in page


async def test_month_by_month_table_lists_the_months_of_the_range(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    page = (await browser.get("/tariffs?from=2026-08-01&to=2026-09-30")).text
    months = re.findall(r'<th scope="row">((?:Aug|Sep) 2026)</th>', page)
    assert months == ["Aug 2026", "Sep 2026"] * 2  # electricity and gas


async def test_chart_data_has_exact_strings_and_a_baseline_first(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    body = (await browser.get("/api/charts/tariffs?fuel=electricity&range=12m")).json()
    assert body["labels"][0] == "What OVO charged"
    assert body["labels"][1:] == ["Economy 7 (example)", "EV overnight (example)", "OVO standard"]
    for usage, standing, total in zip(body["usage"], body["standing"], body["total"], strict=True):
        assert Decimal(usage) + Decimal(standing) == Decimal(total)
        assert re.fullmatch(r"\d+\.\d\d", total)
    assert body["fuel"] == "electricity"
    assert body["days"] > 300
    gas = (await browser.get("/api/charts/tariffs?fuel=gas&from=2026-01-01&to=2026-01-31")).json()
    assert gas["labels"] == ["What OVO charged", "Cheaper gas (example)", "OVO standard"]


async def test_chart_data_refusals(
    seeded: demo.DemoData, ctx: WebContext, browser: httpx.AsyncClient
) -> None:
    assert (await browser.get("/api/charts/tariffs?fuel=steam")).status_code == 400
    assert (await browser.get("/api/charts/tariffs?fuel=gas&range=forever")).status_code == 400
    conn = open_database(ctx.data_dir)
    try:
        for tariff in stored.all_tariffs(conn, Fuel.GAS):
            assert tariff.tariff_id is not None
            stored.remove(conn, tariff.tariff_id)
    finally:
        conn.close()
    assert (await browser.get("/api/charts/tariffs?fuel=gas")).status_code == 404


# -- the editor ---------------------------------------------------------------------------


async def test_new_tariff_form_comes_from_a_template_with_blank_rates(
    browser: httpx.AsyncClient,
) -> None:
    page = (await browser.get("/tariffs?new=economy7")).text
    assert 'hx-post="/tariffs"' in page
    assert 'name="name" value="Economy 7"' in page
    assert 'name="band0_start" value="00:30"' in page
    assert 'name="band1_end" value="00:30"' in page
    assert 'name="band0_rate" value=""' in page
    assert "<h2>New tariff</h2>" in page
    assert "inc. VAT" in page
    assert page.count('type="checkbox"') == 4 * 7  # four rows
    assert "Your tariffs" not in page  # the list gives way to the form
    gas = (await browser.get("/tariffs?new=gas")).text
    assert '<option value="gas" selected>' in gas


@pytest.mark.parametrize(
    ("query", "status"),
    [
        ("new=nope", 400),
        ("edit=abc", 400),
        ("edit=999", 404),
        ("new=flat&edit=1", 400),
        ("range=forever", 400),
    ],
)
async def test_bad_page_parameters(browser: httpx.AsyncClient, query: str, status: int) -> None:
    assert (await browser.get(f"/tariffs?{query}")).status_code == status


async def test_creating_a_tariff_stores_it_and_redirects(
    browser: httpx.AsyncClient, ctx: WebContext
) -> None:
    response = await post(browser, ctx, "/tariffs", flat_form("Mine", "25.5"))
    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/tariffs"
    [tariff] = stored_tariffs(ctx)
    assert (tariff.name, tariff.fuel, tariff.standing_charge) == ("Mine", Fuel.ELECTRICITY, 50_000)
    assert tariff.rate_at(3, 20) == 25_500
    assert "Mine" in (await browser.get("/tariffs")).text


async def test_a_wrapping_band_form_is_saved_split_and_edited_joined(
    browser: httpx.AsyncClient, ctx: WebContext
) -> None:
    data = flat_form("E7", "10", band0_start="00:30", band0_end="07:30")
    data |= {"band1_start": "07:30", "band1_end": "00:30", "band1_rate": "30"}
    data |= {f"band1_day{d}": "1" for d in range(7)}
    assert (await post(browser, ctx, "/tariffs", data)).headers["HX-Redirect"] == "/tariffs"
    [tariff] = stored_tariffs(ctx)
    assert len(tariff.bands) == 3  # the 07:30-00:30 band was split at midnight
    page = (await browser.get(f"/tariffs?edit={tariff.tariff_id}")).text
    assert 'name="band0_start" value="00:30"' in page
    assert 'name="band0_rate" value="10"' in page
    assert 'name="band1_start" value="07:30"' in page
    assert 'name="band1_end" value="00:30"' in page  # joined again: one row, not two
    assert 'name="band2_start" value=""' in page
    assert "<h2>Edit E7</h2>" in page
    assert f'hx-post="/tariffs/{tariff.tariff_id}"' in page


async def test_errors_come_back_with_the_form_filled_in_and_nothing_saved(
    browser: httpx.AsyncClient, ctx: WebContext
) -> None:
    data = flat_form("", "abc", standing="-5", fuel="steam", effective_from="soon")
    response = await post(browser, ctx, "/tariffs", data)
    page = response.text
    assert response.status_code == 200
    assert "HX-Redirect" not in response.headers
    assert 'class="tariff-form"' in page
    assert "Please fix the highlighted fields." in page
    assert page.count('class="field-error"') >= 5
    assert "Enter a name." in page
    assert "Choose electricity or gas." in page
    assert "Enter a date as YYYY-MM-DD, or leave it empty." in page
    assert 'name="band0_rate" value="abc"' in page  # what was typed is kept
    assert 'name="standing" value="-5"' in page
    assert 'aria-invalid="true"' in page
    assert stored_tariffs(ctx) == []


async def test_a_bad_rate_in_a_later_row_is_reported_on_that_row(
    browser: httpx.AsyncClient, ctx: WebContext
) -> None:
    data = flat_form("Rows", "10", band0_start="00:00", band0_end="12:00")
    data |= {"band3_start": "12:00", "band3_end": "00:00", "band3_rate": "x1"}
    data |= {f"band3_day{d}": "1" for d in range(7)}
    page = (await post(browser, ctx, "/tariffs", data)).text
    assert "Band 4: Enter a plain number such as 24.44 (pence)." in page
    assert 'aria-label="Band 4 pence per kWh" aria-invalid="true"' in page
    assert 'name="band0_rate" value="10" size="8" aria-label="Band 1 pence per kWh">' in page


async def test_gaps_and_overlaps_are_listed_in_plain_words(
    browser: httpx.AsyncClient, ctx: WebContext
) -> None:
    data = flat_form("Gappy", "10", band0_end="12:00", band1_start="11:00", band1_end="23:00")
    data |= {"band1_rate": "20"} | {f"band1_day{d}": "1" for d in range(7)}
    page = (await post(browser, ctx, "/tariffs", data)).text
    assert "Monday has no rate for 23:00-24:00." in page
    assert "Monday has two rates for 11:00-12:00." in page
    assert 'role="alert"' in page


async def test_weekday_boxes_matter(browser: httpx.AsyncClient, ctx: WebContext) -> None:
    data = flat_form("Weekdays only") | {f"band0_day{d}": "" for d in (5, 6)}
    data = {k: v for k, v in data.items() if v != ""} | {"band0_rate": "25.5"}
    page = (await post(browser, ctx, "/tariffs", data)).text
    assert "Saturday has no rate for 00:00-24:00." in page
    none = {k: v for k, v in flat_form("None").items() if not k.startswith("band0_day")}
    assert "Choose at least one day." in (await post(browser, ctx, "/tariffs", none)).text


async def test_the_same_name_and_overlapping_dates_are_refused(
    browser: httpx.AsyncClient, ctx: WebContext
) -> None:
    first = flat_form("Cap", "20", effective_to="2026-03-31")
    assert (await post(browser, ctx, "/tariffs", first)).headers["HX-Redirect"] == "/tariffs"
    clash = await post(
        browser, ctx, "/tariffs", flat_form("cap", "21", effective_from="2026-03-01")
    )
    assert "Another version of Cap already covers some of these dates." in clash.text
    assert len(stored_tariffs(ctx)) == 1
    later = flat_form("Cap", "21", effective_from="2026-04-01")
    assert (await post(browser, ctx, "/tariffs", later)).headers["HX-Redirect"] == "/tariffs"
    assert len(stored_tariffs(ctx)) == 2


async def test_editing_replaces_the_tariff_and_keeps_its_id(
    browser: httpx.AsyncClient, ctx: WebContext
) -> None:
    await post(browser, ctx, "/tariffs", flat_form("Cap", "20"))
    [before] = stored_tariffs(ctx)
    url = f"/tariffs/{before.tariff_id}"
    response = await post(browser, ctx, url, flat_form("Cap", "22.25", standing="40"))
    assert response.headers["HX-Redirect"] == "/tariffs"
    [after] = stored_tariffs(ctx)
    assert after.tariff_id == before.tariff_id
    assert (after.rate_at(0, 0), after.standing_charge) == (22_250, 40_000)
    bad = await post(browser, ctx, url, flat_form("Cap", "-1"))
    assert f'hx-post="{url}"' in bad.text
    assert "Save changes" in bad.text
    assert stored_tariffs(ctx)[0].rate_at(0, 0) == 22_250  # unchanged


async def test_editing_something_that_isnt_there_or_isnt_an_id(
    browser: httpx.AsyncClient, ctx: WebContext
) -> None:
    assert (await post(browser, ctx, "/tariffs/99", flat_form())).status_code == 404
    assert (await post(browser, ctx, "/tariffs/abc", flat_form())).status_code == 404
    assert (await post(browser, ctx, "/tariffs/abc/delete", {})).status_code == 404


async def test_deleting(browser: httpx.AsyncClient, ctx: WebContext) -> None:
    await post(browser, ctx, "/tariffs", flat_form("Gone", "20"))
    [tariff] = stored_tariffs(ctx)
    url = f"/tariffs/{tariff.tariff_id}/delete"
    assert (await browser.post(url, headers=csrf(ctx))).headers["HX-Redirect"] == "/tariffs"
    assert stored_tariffs(ctx) == []
    again = await browser.post(url, headers=csrf(ctx))
    assert "already deleted" in again.text


async def test_posts_need_the_csrf_token_and_a_form_body(
    browser: httpx.AsyncClient, ctx: WebContext
) -> None:
    no_token = await browser.post("/tariffs", data=flat_form(), headers={"Origin": ORIGIN})
    assert no_token.status_code == 403
    wrong_type = await browser.post("/tariffs", content=b"{}", headers={**csrf(ctx)})
    assert wrong_type.status_code == 415
    assert stored_tariffs(ctx) == []


async def test_a_huge_form_is_refused(browser: httpx.AsyncClient, ctx: WebContext) -> None:
    big = flat_form(name="x" * 9000)
    assert (await post(browser, ctx, "/tariffs", big)).status_code == 413
    many = {f"junk{i}": "1" for i in range(150)}
    assert (await post(browser, ctx, "/tariffs", many)).status_code == 400


# -- the form helpers ---------------------------------------------------------------------


def round_trip(tariff: Tariff) -> Tariff:
    draft, _ = view.parse_form(view.tariff_values(tariff))
    return build_tariff(draft)


def rate_table(tariff: Tariff) -> list[list[int]]:
    return [[tariff.rate_at(d, s) for s in range(48)] for d in range(7)]


def test_every_example_tariff_survives_form_and_back() -> None:
    conn_tariffs = demo._tariffs(date(2026, 1, 1))
    assert len(conn_tariffs) == 7
    for tariff in conn_tariffs:
        assert rate_table(round_trip(tariff)) == rate_table(tariff)
        assert round_trip(tariff).standing_charge == tariff.standing_charge
        assert (round_trip(tariff).effective_from, round_trip(tariff).effective_to) == (
            tariff.effective_from,
            tariff.effective_to,
        )


@given(
    cuts=st.lists(st.integers(min_value=1, max_value=47), min_size=1, max_size=6, unique=True),
    rates=st.lists(st.integers(min_value=0, max_value=500_000), min_size=7, max_size=7),
    shift=st.integers(min_value=0, max_value=47),
)
def test_any_banding_survives_the_form_even_when_rotated_past_midnight(
    cuts: list[int], rates: list[int], shift: int
) -> None:
    edges = [0, *sorted(cuts), 48]
    bands = []
    for i, (start, end) in enumerate(itertools.pairwise(edges)):
        # Rotate the whole day by `shift` slots so some bands straddle midnight.
        first, last = (start + shift) % 48, (end + shift) % 48 or 48
        rate = MilliPence(rates[i % 7])
        if first < last:
            bands.append(Band(first, last, rate, frozenset(range(7))))
        else:
            bands.append(Band(first, 48, rate, frozenset(range(7))))
            if last < 48:
                bands.append(Band(0, last, rate, frozenset(range(7))))
    tariff = Tariff("T", Fuel.ELECTRICITY, MilliPence(0), tuple(bands))
    assert rate_table(round_trip(tariff)) == rate_table(tariff)


def test_row_count_shows_used_rows_and_two_spare() -> None:
    assert view.row_count({}) == 4
    assert view.row_count(view.template_values("economy7")) == 4  # two used, two spare
    assert view.row_count({"band2_rate": "1"}) == 5
    assert view.row_count({"band5_rate": "1"}) == 8
    assert view.row_count({"band9_rate": "1"}) == 10
    assert view.row_count({"band12_rate": "1"}) == 4  # beyond the form's rows: ignored


def test_unused_rows_and_days_are_ignored_when_reading_the_form() -> None:
    draft, rows = view.parse_form(
        {"name": "n", "band2_rate": "5", "band2_day3": "1", "band2_day6": "1", "band5_day0": "1"}
    )
    assert rows == [2]
    assert draft.bands[0].weekdays == (3, 6)


def test_form_errors_are_mapped_to_form_fields() -> None:
    from ovo_insights.tariffs import FieldError

    errors = view.form_errors(
        [
            FieldError("bands[1].rate", "bad rate"),
            FieldError("bands[0].weekdays", "no days"),
            FieldError("bands[1].end", "bad end"),
            FieldError("bands", "gap"),
            FieldError("bands", "gap"),
            FieldError("name", "needs a name"),
            FieldError("name", "second message ignored"),
        ],
        rows=[2, 5],
    )
    assert errors.by_field == {
        "band5_rate": "bad rate",
        "band2_days": "no days",
        "band5_end": "bad end",
        "name": "needs a name",
    }
    assert errors.general == ("gap",)
    assert errors
    assert not view.FormErrors({}, ())


def test_descriptions_and_pence_text() -> None:
    assert [view.pence_text(MilliPence(v)) for v in (24_440, 5_000, 0, 500_000, 1)] == [
        "24.44", "5", "0", "500", "0.001",
    ]  # fmt: skip
    flat = Tariff.flat("F", Fuel.GAS, MilliPence(5_550), MilliPence(0), date(2026, 4, 1))
    assert view.describe_rates(flat) == "5.55p/kWh"
    assert view.describe_dates(flat) == "1 Apr 2026 to now"
    ended = Tariff.flat("F", Fuel.GAS, MilliPence(1), MilliPence(0), None, date(2026, 3, 31))
    assert view.describe_dates(ended) == "start to 31 Mar 2026"
    both = Tariff.flat(
        "F", Fuel.GAS, MilliPence(1), MilliPence(0), date(2026, 1, 1), date(2026, 1, 31)
    )
    assert view.describe_dates(both) == "1 Jan 2026 to 31 Jan 2026"


def test_reason_labels_cover_every_skip() -> None:
    labels = view.reason_counts(dict.fromkeys(Skip, 1))
    assert len(labels) == len(Skip)
    assert all(count == 1 for _, count in labels)

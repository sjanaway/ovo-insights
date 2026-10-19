"""CSV export: contract, formats, clock-change days, injection guard, streaming and the zip."""

import csv
import io
import re
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from ovo_insights import demo
from ovo_insights.analysis import queries
from ovo_insights.analysis.recost import columns_for, compare
from ovo_insights.analysis.usage import pounds_text
from ovo_insights.domain import Fuel, Reading
from ovo_insights.money import MilliPence, format_pence
from ovo_insights.storage import open_database
from ovo_insights.storage import tariffs as tariff_store
from ovo_insights.storage.repository import Repository
from ovo_insights.sync.plan import DateRange
from ovo_insights.tariffs import Tariff
from ovo_insights.timeutil import LONDON, london_half_hours
from ovo_insights.web import export
from ovo_insights.web.app import WebContext

pytestmark = pytest.mark.anyio

TODAY = date(2026, 10, 13)
RANGE = "from=2025-10-24&to=2025-10-28"  # includes the clock-change day with 50 half-hours
SPRING = "from=2026-03-28&to=2026-03-30"  # includes the 46-half-hour day
DOCS = Path(__file__).resolve().parents[2] / "docs" / "csv-format.md"
SENTINEL = '=HYPERLINK("http://example.com","click")'


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


def parse(response: httpx.Response) -> list[dict[str, str]]:
    assert response.content.startswith(export.BYTE_ORDER_MARK)
    text = response.content.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text, newline="")))


async def get(browser: httpx.AsyncClient, dataset: str, query: str = RANGE) -> httpx.Response:
    response = await browser.get(f"/export/{dataset}.csv?{query}")
    assert response.status_code == 200
    return response


# -- the files ----------------------------------------------------------------------------


@pytest.mark.parametrize("dataset", list(export.Dataset))
async def test_every_file_has_the_contract_header_bom_crlf_and_download_headers(
    seeded: demo.DemoData, browser: httpx.AsyncClient, dataset: export.Dataset
) -> None:
    response = await get(browser, dataset.value, "from=2025-10-24&to=2025-10-28")
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="ovo-insights-{dataset.value}-2025-10-24_to_2025-10-28.csv"'
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    body = response.content
    assert body.startswith(export.BYTE_ORDER_MARK)
    header = ",".join(export.COLUMNS[dataset]) + "\r\n"
    assert body[3:].decode().startswith(header)
    assert b"\r\n" in body
    assert b"\n" not in body.replace(b"\r\n", b"")  # never a bare line feed


async def test_daily_matches_the_database_figure_for_figure(
    seeded: demo.DemoData, ctx: WebContext, browser: httpx.AsyncClient
) -> None:
    rows = parse(await get(browser, "daily"))
    conn = open_database(ctx.data_dir)
    try:
        span = DateRange(date(2025, 10, 24), date(2025, 10, 28))
        expected = {
            (day.day.isoformat(), fuel.value): day
            for fuel in Fuel
            for day in queries.daily(conn, fuel, span)
        }
    finally:
        conn.close()
    assert len(rows) == len(expected) == 10
    assert [(r["date"], r["fuel"]) for r in rows] == sorted(
        expected, key=lambda k: (k[0], list(Fuel).index(Fuel(k[1])))
    )
    for row in rows:
        day = expected[(row["date"], row["fuel"])]
        assert row["usage_cost_gbp"] == pounds_text(day.usage_cost)
        assert re.fullmatch(r"\d+\.\d\d", row["usage_cost_gbp"])
        assert row["standing_charge_pence"] == format_pence(day.standing or MilliPence(0))
        assert re.fullmatch(r"\d+\.\d{3}", row["standing_charge_pence"])
        assert row["unit_rate_pence"] == format_pence(day.unit_rate or MilliPence(0))
        assert Decimal(row["kwh"]) == Decimal(str(day.kwh)).quantize(Decimal("0.001"))
        assert row["has_half_hourly"] == ("true" if row["fuel"] == "electricity" else "false")


async def test_daily_rates_keep_the_precision_two_decimal_pounds_would_lose(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    row = next(r for r in parse(await get(browser, "daily")) if r["fuel"] == "electricity")
    assert row["standing_charge_pence"] == "53.680"  # £0.5368 a day, not £0.54
    assert row["unit_rate_pence"] == "24.500"


async def test_monthly_covers_months_overlapping_the_range(
    seeded: demo.DemoData, ctx: WebContext, browser: httpx.AsyncClient
) -> None:
    rows = parse(await get(browser, "monthly"))
    assert [(r["month"], r["fuel"]) for r in rows] == [
        ("2025-10-01", "electricity"),
        ("2025-10-01", "gas"),
    ]
    conn = open_database(ctx.data_dir)
    try:
        repo = Repository(conn)
        [account] = repo.account_ids()
        month = repo.monthly(account, Fuel.ELECTRICITY, date(2025, 10, 1), date(2025, 10, 1))[0]
    finally:
        conn.close()
    assert Decimal(rows[0]["kwh"]) == Decimal(str(month.kwh)).quantize(Decimal("0.001"))
    assert rows[0]["usage_cost_gbp"] == pounds_text(month.cost)


def stored_slots(day: date, data: demo.DemoData) -> int:
    """Half-hours the demo stored for a day: all of them, two fewer on a short day, none if lost."""
    whole = len(london_half_hours(day))
    if day in data.missing_days:
        return 0
    return whole - 2 if day in data.short_days else whole


async def test_half_hourly_autumn_clock_change_day_has_50_rows_and_the_repeated_hour(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    rows = parse(await get(browser, "half-hourly-electricity"))
    autumn = [r for r in rows if r["local_time"].startswith("2025-10-26")]
    assert len(autumn) == stored_slots(date(2025, 10, 26), seeded)
    if len(autumn) == 50:  # the demo's seed leaves this day whole
        assert [r["local_time"][-6:] for r in autumn] == ["+01:00"] * 4 + ["+00:00"] * 46
        # The UK clock reads 01:00 and 01:30 twice: first in BST, then in GMT.
        repeated = [r["local_time"] for r in autumn if r["local_time"][11:16] in ("01:00", "01:30")]
        assert [(t[11:16], t[-6:]) for t in repeated] == [
            ("01:00", "+01:00"),
            ("01:30", "+01:00"),
            ("01:00", "+00:00"),
            ("01:30", "+00:00"),
        ]
    assert [r["utc_time"] for r in autumn] == sorted({r["utc_time"] for r in autumn})
    for row in rows:
        instant = datetime.strptime(row["utc_time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        assert datetime.fromisoformat(row["local_time"]) == instant
        assert instant.astimezone(LONDON).isoformat() == row["local_time"]
    days = [date(2025, 10, d) for d in range(24, 29)]
    assert len(rows) == sum(stored_slots(d, seeded) for d in days)


async def test_half_hourly_spring_forward_day_has_46_rows_and_no_01_00_hour(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    rows = parse(await get(browser, "half-hourly-electricity", SPRING))
    spring = [r["local_time"] for r in rows if r["local_time"].startswith("2026-03-29")]
    assert len(spring) == stored_slots(date(2026, 3, 29), seeded)
    clocks = [t[11:16] for t in spring]
    assert "01:00" not in clocks
    assert "01:30" not in clocks
    assert clocks[:3] == ["00:00", "00:30", "02:00"]
    assert [t[-6:] for t in spring[:3]] == ["+00:00", "+00:00", "+01:00"]
    assert spring[-1].endswith("+01:00")


async def test_row_counts_equal_the_page_and_the_database(
    seeded: demo.DemoData, ctx: WebContext, browser: httpx.AsyncClient
) -> None:
    page = (await browser.get(f"/export?{RANGE}")).text
    conn = open_database(ctx.data_dir)
    try:
        span = DateRange(date(2025, 10, 24), date(2025, 10, 28))
        for dataset in export.Dataset:
            expected = sum(1 for _ in export.rows(conn, dataset, span, TODAY))
            assert export.count(conn, dataset, span, TODAY) == expected
            body = await get(browser, dataset.value)
            assert len(parse(body)) == expected
    finally:
        conn.close()
    assert "Meter readings" in page
    assert "none (header only)" in page  # gas half-hours


async def test_gas_half_hourly_is_a_header_only_file_when_there_is_none(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    response = await get(browser, "half-hourly-gas")
    assert response.content == export.BYTE_ORDER_MARK + b"utc_time,local_time,kwh\r\n"


async def test_readings_use_numbered_meters_exact_values_and_include_todays(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    rows = parse(await get(browser, "readings", "from=2026-10-01&to=2026-10-12"))
    electricity = [r for r in rows if r["fuel"] == "electricity"]
    assert electricity[-1]["date"] == "2026-10-13"  # closes the last day, as on the page
    assert {r["meter"] for r in rows} == {"Electricity meter 1", "Gas meter 1"}
    assert {r["register"] for r in electricity} == {"1"}
    assert {r["register"] for r in rows if r["fuel"] == "gas"} == {""}
    assert {r["unit"] for r in rows} == {"KWH", "SCMH"}
    assert all(re.fullmatch(r"\d+\.\d+", r["value"]) for r in rows)
    text = "".join(",".join(r.values()) for r in rows)
    for identifier in (demo.ELECTRIC_METER, demo.GAS_METER, demo.MPAN, demo.MPRN, demo.ACCOUNT):
        assert identifier not in text


async def test_readings_include_old_meters_when_the_range_reaches_them(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    rows = parse(await get(browser, "readings", "range=all"))
    meters = {r["meter"] for r in rows}
    assert meters == {"Electricity meter 1", "Electricity meter 2", "Gas meter 1", "Gas meter 2"}
    old = [r for r in rows if r["meter"] == "Electricity meter 2"]
    assert {r["register"] for r in old} == {"01"}  # the leading zero survives
    assert {r["meter_status"] for r in old} == {"Removed"}
    assert {r["source"] for r in old} == {"MANUAL_READ_CUSTOMER"}


async def test_formula_text_is_defused_and_numbers_are_left_alone(
    seeded: demo.DemoData, ctx: WebContext, browser: httpx.AsyncClient
) -> None:
    conn = open_database(ctx.data_dir)
    try:
        Repository(conn).upsert_readings(
            [
                Reading(
                    demo.ELECTRIC_METER,
                    "1",
                    date(2026, 10, 1),
                    Decimal("50125.561"),
                    source=SENTINEL[:60],
                    lifecycle="+cmd",
                    read_type="-1+1",
                )
            ]
        )
        sentinel = Tariff.flat('=2+2" ,x', Fuel.GAS, MilliPence(5_000), MilliPence(25_000))
        tariff_store.add(conn, sentinel, datetime(2026, 10, 13, tzinfo=UTC))
    finally:
        conn.close()
    rows = parse(await get(browser, "readings", "from=2026-10-01&to=2026-10-02"))
    changed = next(r for r in rows if r["fuel"] == "electricity" and r["date"] == "2026-10-01")
    assert changed["source"].startswith("'=HYPERLINK(")
    assert changed["lifecycle"] == "'+cmd"
    assert changed["type"] == "'-1+1"
    assert changed["value"] == "50125.561"  # a number: not prefixed
    assert changed["date"] == "2026-10-01"
    comparison = parse(await get(browser, "tariff-comparison", "range=12m"))
    names = {r["tariff"] for r in comparison}
    assert "'=2+2\" ,x" in names  # quoting and the guard together survive a round trip


async def test_tariff_comparison_months_and_amounts_match_the_engine(
    seeded: demo.DemoData, ctx: WebContext, browser: httpx.AsyncClient
) -> None:
    rows = parse(await get(browser, "tariff-comparison", "from=2025-10-01&to=2025-12-31"))
    conn = open_database(ctx.data_dir)
    try:
        span = DateRange(date(2025, 10, 1), date(2025, 12, 31))
        result = compare(
            queries.costing_days(conn, Fuel.ELECTRICITY, span),
            columns_for(tariff_store.all_tariffs(conn), Fuel.ELECTRICITY),
        )
    finally:
        conn.close()
    electricity = [r for r in rows if r["fuel"] == "electricity"]
    assert {r["tariff"] for r in electricity} == {
        "What OVO charged",
        "Economy 7 (example)",
        "EV overnight (example)",
        "OVO standard",
    }
    actual = {r["month"]: r for r in electricity if r["tariff"] == "What OVO charged"}
    assert list(actual) == ["2025-10-01", "2025-11-01", "2025-12-01"]
    for month, row in actual.items():
        key = date.fromisoformat(month)
        assert row["total_gbp"] == pounds_text(result.actual.by_month[key])
        assert row["usage_gbp"] == pounds_text(result.actual.usage_by_month[key])
        assert row["standing_gbp"] == pounds_text(result.actual.standing_by_month[key])
        assert int(row["days_compared"]) == sum(
            1 for d in result.included if d.replace(day=1) == key
        )
    # OVO's own rates are the baseline to the penny.
    standard = {r["month"]: r["total_gbp"] for r in electricity if r["tariff"] == "OVO standard"}
    assert standard == {m: r["total_gbp"] for m, r in actual.items()}
    assert sum(1 for r in rows if r["fuel"] == "gas") == 3 * 3  # baseline, two tariffs


async def test_tariff_comparison_is_header_only_without_tariffs(
    ctx: WebContext, browser: httpx.AsyncClient
) -> None:
    ctx.today = lambda: TODAY
    response = await browser.get("/export/tariff-comparison.csv?range=12m")
    assert (
        response.content
        == export.BYTE_ORDER_MARK
        + (",".join(export.COLUMNS[export.Dataset.TARIFF_COMPARISON]) + "\r\n").encode()
    )


async def test_tariff_comparison_is_header_only_when_no_day_can_be_costed(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    day = min(seeded.missing_days)  # no daily figure, so nothing to compare
    response = await get(browser, "tariff-comparison", f"from={day}&to={day}")
    assert parse(response) == []


# -- the zip ------------------------------------------------------------------------------


async def test_the_zip_holds_every_file_identical_to_the_single_downloads(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    response = await browser.get(f"/export/ovo-insights.zip?{RANGE}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == (
        'attachment; filename="ovo-insights-export-2025-10-24_to_2025-10-28.zip"'
    )
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert archive.testzip() is None
    expected = {f"ovo-insights-{d.value}-2025-10-24_to_2025-10-28.csv" for d in export.Dataset}
    assert set(archive.namelist()) == expected
    for dataset in export.Dataset:
        single = await get(browser, dataset.value)
        name = f"ovo-insights-{dataset.value}-2025-10-24_to_2025-10-28.csv"
        assert archive.read(name) == single.content


async def test_the_zip_works_with_nothing_stored(
    ctx: WebContext, browser: httpx.AsyncClient
) -> None:
    ctx.today = lambda: TODAY
    response = await browser.get("/export/ovo-insights.zip")
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert len(archive.namelist()) == 6
    assert all(archive.read(n).startswith(export.BYTE_ORDER_MARK) for n in archive.namelist())


# -- the page and refusals ----------------------------------------------------------------


async def test_the_export_page_lists_files_with_links_that_keep_the_dates(
    seeded: demo.DemoData, browser: httpx.AsyncClient
) -> None:
    page = (await browser.get(f"/export?{RANGE}")).text
    assert '<a href="/export" aria-current="page">Export</a>' in page
    for dataset in export.Dataset:
        assert f'href="/export/{dataset.value}.csv?from=2025-10-24&amp;to=2025-10-28"' in page
    assert 'href="/export/ovo-insights.zip?from=2025-10-24&amp;to=2025-10-28"' in page
    assert "ovo-insights-export-2025-10-24_to_2025-10-28.zip" in page
    assert "outside this app's protection" in page
    preset = (await browser.get("/export")).text
    assert 'href="/export/daily.csv?range=12m"' in preset


async def test_refusals(
    seeded: demo.DemoData, anonymous: httpx.AsyncClient, ctx: WebContext
) -> None:
    assert (await anonymous.get("/export/daily.csv")).status_code == 403  # no session
    assert (await anonymous.get("/export/ovo-insights.zip")).status_code == 403


async def test_bad_requests(seeded: demo.DemoData, browser: httpx.AsyncClient) -> None:
    assert (await browser.get("/export/nope.csv")).status_code == 404
    assert (await browser.get("/export/daily.csv?range=forever")).status_code == 400
    assert (await browser.get("/export/daily.csv?from=2025-01-01")).status_code == 400
    assert (await browser.get("/export/ovo-insights.zip?from=x&to=y")).status_code == 400
    assert (await browser.get("/export?range=forever")).status_code == 400
    assert (await browser.post("/export/daily.csv")).status_code in (403, 405)


# -- encoding -----------------------------------------------------------------------------


def test_chunks_are_bounded_and_start_with_the_mark_and_header() -> None:
    def many() -> Iterator[list[str]]:
        for i in range(10_500):
            yield ["2025-01-01", "electricity", f"{i}.000", "1.00"]

    chunks = list(export.csv_chunks(export.Dataset.MONTHLY, many(), chunk_rows=1_000))
    assert chunks[0] == export.BYTE_ORDER_MARK + b"month,fuel,kwh,usage_cost_gbp\r\n"
    assert len(chunks) == 1 + 10 + 1  # header, ten full chunks, the remainder
    assert max(len(c) for c in chunks) < 1_000 * 40
    text = b"".join(chunks).decode("utf-8-sig")
    assert len(list(csv.reader(io.StringIO(text, newline="")))) == 10_501


def test_a_zip_is_written_as_a_stream_of_many_small_pieces(tmp_path: Path) -> None:
    conn = open_database(tmp_path / "data")
    try:
        data = demo.build(3, TODAY)
        demo.write(conn, data, datetime(2026, 10, 13, 6, tzinfo=UTC))
        span = DateRange(data.first, data.last)  # two years: about 35,000 half-hours
        pieces = list(export.zip_chunks(conn, span, TODAY, chunk_rows=500))
    finally:
        conn.close()
    whole = b"".join(pieces)
    assert len(pieces) > 20
    assert max(len(p) for p in pieces) < 64_000  # never the whole archive at once
    assert max(len(p) for p in pieces) < len(whole) / 5
    archive = zipfile.ZipFile(io.BytesIO(whole))
    assert archive.testzip() is None
    assert len(archive.namelist()) == 6
    half_hours = archive.read(
        f"ovo-insights-half-hourly-electricity-{span.first}_to_{span.last}.csv"
    )
    assert half_hours.count(b"\r\n") > 30_000


@given(st.text(alphabet=st.characters(blacklist_categories=["Cs"], blacklist_characters="\x00")))
def test_defend_prefixes_only_formula_starts(text: str) -> None:
    out = export.defend(text)
    if text.startswith(export.FORMULA_STARTS):
        assert out == "'" + text
    else:
        assert out == text


@given(st.text(alphabet=st.characters(blacklist_categories=["Cs"], blacklist_characters="\x00")))
def test_any_text_survives_the_csv_round_trip_once_defended(text: str) -> None:
    row = [
        "electricity",
        "Electricity meter 1",
        "1",
        "KWH",
        "Active",
        "2026-01-01",
        "1.0",
        text,
        "",
        "",
    ]
    chunks = export.csv_chunks(export.Dataset.READINGS, [row], chunk_rows=1)
    body = b"".join(chunks).decode("utf-8-sig")
    parsed = list(csv.reader(io.StringIO(body, newline="")))
    assert parsed[1][7] == export.defend(text)
    assert parsed[1][6] == "1.0"
    assert len(parsed) == 2


@given(st.floats(min_value=0, max_value=100_000, allow_nan=False, allow_infinity=False))
def test_kwh_text_is_three_decimals_and_close_to_the_value(value: float) -> None:
    text = export.kwh_text(value)
    assert re.fullmatch(r"\d+\.\d{3}", text)
    assert abs(Decimal(text) - Decimal(str(value))) <= Decimal("0.0005")


def test_kwh_text_rounds_half_even_on_the_decimal_not_the_binary_float() -> None:
    assert [export.kwh_text(v) for v in (2.0005, 2.0015, 0.0, 223.29300000000003)] == [
        "2.000",
        "2.002",
        "0.000",
        "223.293",
    ]


def test_filenames() -> None:
    span = DateRange(date(2025, 1, 2), date(2025, 12, 31))
    assert export.filename(export.Dataset.DAILY, span) == (
        "ovo-insights-daily-2025-01-02_to_2025-12-31.csv"
    )
    assert export.zip_filename(span) == "ovo-insights-export-2025-01-02_to_2025-12-31.zip"


# -- the documented contract --------------------------------------------------------------


def documented_columns() -> dict[str, list[str]]:
    """Column names in each `## ` section's first table, keyed by the section's dataset names."""
    found: dict[str, list[str]] = {}
    for section in re.split(r"^## ", DOCS.read_text(), flags=re.MULTILINE)[1:]:
        title = section.splitlines()[0]
        names = re.findall(r"`([a-z-]+)`", title)
        columns = re.findall(r"^\| `([a-z_]+)` \|", section, flags=re.MULTILINE)
        for name in names:
            found[name] = columns
    return found


def test_the_documented_columns_are_exactly_the_exported_columns_in_order() -> None:
    documented = documented_columns()
    for dataset in export.Dataset:
        assert documented[dataset.value] == list(export.COLUMNS[dataset]), dataset
    assert set(documented) == {d.value for d in export.Dataset}


def test_text_columns_exist_and_numbers_are_never_guarded() -> None:
    for dataset, guarded in export.TEXT_COLUMNS.items():
        assert guarded <= set(export.COLUMNS[dataset])
    numeric = {"kwh", "value", "usage_cost_gbp", "standing_charge_pence", "unit_rate_pence"}
    assert all(not (numeric & guarded) for guarded in export.TEXT_COLUMNS.values())


def test_slot_counts_used_in_the_clock_change_tests_are_what_london_says() -> None:
    assert len(london_half_hours(date(2025, 10, 26))) == 50
    assert len(london_half_hours(date(2026, 3, 29))) == 46

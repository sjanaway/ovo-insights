import logging
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ovo_insights import demo
from ovo_insights.analysis import queries
from ovo_insights.analysis.half_hourly import base_load, household
from ovo_insights.analysis.readings import Verdict, windows_of
from ovo_insights.analysis.recost import columns_for, compare
from ovo_insights.cli import app
from ovo_insights.domain import Fuel
from ovo_insights.logs import PrivateRotatingFileHandler
from ovo_insights.paths import DATABASE_FILENAME
from ovo_insights.storage import open_database
from ovo_insights.storage import tariffs as tariff_store
from ovo_insights.sync import store
from ovo_insights.sync.plan import DateRange
from ovo_insights.timeutil import london_half_hours
from ovo_insights.web import server

TODAY = date(2026, 10, 13)


@pytest.fixture(scope="module")
def data() -> demo.DemoData:
    return demo.build(1, TODAY)


def test_deterministic_for_a_seed(data: demo.DemoData) -> None:
    assert demo.build(1, TODAY) == data
    assert demo.build(2, TODAY) != data


def test_two_years_ending_yesterday_with_deliberate_gaps(data: demo.DemoData) -> None:
    assert (data.first, data.last) == (date(2024, 10, 13), date(2026, 10, 12))
    assert len(data.missing_days) == demo.WHOLE_DAYS_MISSING
    assert len(data.short_days) == demo.SHORT_DAYS
    assert not data.missing_days & data.short_days
    days = {r.day for r in data.daily}
    assert not days & data.missing_days
    assert len(days) == 730 - demo.WHOLE_DAYS_MISSING


def test_dst_days_have_46_and_50_half_hours(data: demo.DemoData) -> None:
    for day, slots in ((date(2025, 3, 30), 46), (date(2025, 10, 26), 50)):
        starts = set(london_half_hours(day))
        assert sum(1 for h in data.half_hours if h.interval_start in starts) == slots


def test_shape_is_plausible(data: demo.DemoData) -> None:
    gas = {r.day: r.kwh for r in data.daily if r.fuel is Fuel.GAS}
    january = sum(v for d, v in gas.items() if d.month == 1) / 62
    july = sum(v for d, v in gas.items() if d.month == 7) / 62
    assert january > 4 * july  # winter heating
    load = base_load(household(data.half_hours))
    assert load is not None
    assert 180 < load.watts < 260  # the 220 W base load comes through
    # Daily electricity is the sum of the day's half-hours (where the day is whole).
    day = date(2026, 6, 1)
    starts = set(london_half_hours(day))
    row = next(r for r in data.daily if r.fuel is Fuel.ELECTRICITY and r.day == day)
    assert row.kwh == pytest.approx(
        sum(h.kwh for h in data.half_hours if h.interval_start in starts), abs=1e-6
    )


def test_costs_follow_ovo_semantics(data: demo.DemoData) -> None:
    for row in data.daily[:20]:
        assert row.cost is not None
        assert row.cost % 1000 == 0
        assert row.standing_charge is not None
        assert row.unit_rate is not None
    months = {(m.fuel, m.month): m for m in data.monthly}
    september = [
        r for r in data.daily if r.fuel is Fuel.GAS and r.day.replace(day=1) == date(2026, 9, 1)
    ]
    assert months[(Fuel.GAS, date(2026, 9, 1))].cost == sum(r.cost or 0 for r in september)


def test_write_stores_data_and_a_sync_history(
    conn: sqlite3.Connection, data: demo.DemoData
) -> None:
    demo.write(conn, data, datetime(2026, 10, 13, 6, tzinfo=UTC))

    assert conn.execute("SELECT count(*) FROM usage_half_hourly").fetchone()[0] == len(
        data.half_hours
    )
    [run] = store.recent_runs(conn)
    assert run.status is store.RunStatus.OK
    gaps = {
        row[0]
        for row in conn.execute(
            "SELECT period_start FROM sync_state"
            " WHERE granularity = 'half_hourly' AND status = 'gap'"
        )
    }
    assert gaps == {d.isoformat() for d in data.missing_days | data.short_days}
    statuses = {r[0] for r in conn.execute("SELECT DISTINCT status FROM sync_state")}
    assert statuses <= {"complete", "provisional", "gap"}


# -- CLI ----------------------------------------------------------------------------------


@pytest.fixture
def no_server(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    seen: dict[str, object] = {}

    def fake_prepare(data_dir: Path, store_: object, **kwargs: object) -> str:
        seen.update(data_dir=data_dir, kwargs=kwargs)
        assert (data_dir / DATABASE_FILENAME).exists()
        return "prepared"

    def fake_run(prepared: object, *, open_browser: bool, announce: object) -> None:
        seen.update(open_browser=open_browser)
        raise KeyboardInterrupt  # the user pressed Ctrl-C

    monkeypatch.setattr(server, "prepare", fake_prepare)
    monkeypatch.setattr(server, "run", fake_run)
    return seen


def test_demo_command_uses_a_throwaway_folder_and_cleans_up(
    no_server: dict[str, object],
) -> None:
    result = CliRunner().invoke(app, ["demo", "--seed", "3", "--port", "8123", "--no-open"])

    data_dir = no_server["data_dir"]
    assert isinstance(data_dir, Path)
    assert data_dir.name.startswith("ovo-insights-demo-")
    assert not data_dir.exists()  # deleted on the way out, even after Ctrl-C
    assert no_server["kwargs"] == {"port": 8123, "schedule": False, "demo": True}
    assert no_server["open_browser"] is False
    assert "Demo mode: made-up data (seed 3)" in result.output
    assert isinstance(result.exception, KeyboardInterrupt) or result.exit_code != 0


def test_demo_command_reports_a_busy_port(monkeypatch: pytest.MonkeyPatch) -> None:
    def busy(*args: object, **kwargs: object) -> None:
        raise OSError(48, "Address already in use")

    monkeypatch.setattr(server, "prepare", busy)

    result = CliRunner().invoke(app, ["demo", "--port", "8123", "--no-open"])

    assert result.exit_code == 1
    assert "Could not listen on port 8123: Address already in use" in result.output


def test_demo_never_touches_the_keychain_or_real_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real = tmp_path / "real-data"
    monkeypatch.setenv("OVO_INSIGHTS_DATA_DIR", str(real))
    captured: dict[str, object] = {}

    def fake_run(prepared: server.Prepared, *, open_browser: bool, announce: object) -> None:
        captured["store"] = type(prepared.server.config.app)
        prepared.sock.close()
        prepared.log_handler.close()
        logging.getLogger().removeHandler(prepared.log_handler)

    monkeypatch.setattr(server, "run", fake_run)

    result = CliRunner().invoke(app, ["demo", "--no-open"])

    assert result.exit_code == 0, result.output
    assert not real.exists()  # the real data dir was never created
    for handler in logging.getLogger().handlers:
        assert not isinstance(handler, PrivateRotatingFileHandler)


# -- meter readings -----------------------------------------------------------------------


def test_demo_has_four_meters_with_readings_and_known_units(data: demo.DemoData) -> None:
    assert {(m.fuel, m.status) for m in data.meters} == {
        (Fuel.ELECTRICITY, "Active"),
        (Fuel.ELECTRICITY, "Removed"),
        (Fuel.GAS, "Active"),
        (Fuel.GAS, "Removed"),
    }
    assert {r.unit for r in data.registers} == {"KWH", "SCMH", "SCFH"}
    meters = {m.serial for m in data.meters}
    assert {r.meter_serial for r in data.readings} == meters
    assert {r.meter_serial for r in data.registers} == meters
    keys = [(r.meter_serial, r.register_id, r.read_date) for r in data.readings]
    assert len(keys) == len(set(keys))  # the key readings are stored under


def test_demo_electricity_readings_run_daily_from_the_first_day_to_today(
    data: demo.DemoData,
) -> None:
    days = [r.read_date for r in data.readings if r.meter_serial == demo.ELECTRIC_METER]
    assert days[0] == data.first
    assert days[-1] == TODAY
    assert len(days) == (TODAY - data.first).days + 1


def test_demo_faults_are_found_by_the_comparison(tmp_path: Path, data: demo.DemoData) -> None:
    """Clean windows match; the deliberate bad estimates are flagged and nothing else is."""
    conn = open_database(tmp_path / "data")
    try:
        demo.write(conn, data, datetime(2026, 10, 13, 6, tzinfo=UTC))
        found: dict[str, list[Verdict]] = {}
        for series in queries.register_series(conn):
            days = queries.daily(conn, series.fuel, DateRange(date(2020, 1, 1), TODAY))
            smart = {d.day: d.kwh for d in days}
            first = queries.earliest_days(conn).get(series.fuel)
            found[f"{series.fuel.value}-{series.unit}"] = [
                w.verdict for w in windows_of(series, smart, first)
            ]
    finally:
        conn.close()
    electricity = found["electricity-KWH"]
    assert electricity.count(Verdict.MISMATCH) == 1
    assert electricity.count(Verdict.WENT_DOWN) == 1  # the reading after the bad estimate
    assert electricity.count(Verdict.PARTIAL_SMART_DATA) == len(data.missing_days)
    assert set(electricity) - {Verdict.MISMATCH, Verdict.WENT_DOWN} == {
        Verdict.MATCH,
        Verdict.PARTIAL_SMART_DATA,
    }
    gas = found["gas-SCMH"]
    assert gas.count(Verdict.MISMATCH) == 2  # either side of the bad estimate
    assert Verdict.WENT_DOWN not in gas
    assert set(found["gas-SCFH"]) == {Verdict.NO_SMART_DATA}


def test_demo_tariffs_include_the_demos_own_rates_for_both_fuels(data: demo.DemoData) -> None:
    names = {(t.fuel, t.name) for t in data.tariffs}
    assert (Fuel.ELECTRICITY, "OVO standard") in names
    assert (Fuel.GAS, "OVO standard") in names
    assert {t.name for t in data.tariffs if not t.is_flat} == {
        "Economy 7 (example)",
        "EV overnight (example)",
    }
    standard = [t for t in data.tariffs if t.name == "OVO standard" and t.fuel is Fuel.GAS]
    assert len(standard) == 2  # before and after the price rise
    assert standard[0].effective_to is not None
    assert standard[1].effective_from == standard[0].effective_to + timedelta(days=1)


def test_demo_standard_tariff_reproduces_what_was_charged_to_the_penny(
    tmp_path: Path, data: demo.DemoData
) -> None:
    conn = open_database(tmp_path / "data")
    try:
        demo.write(conn, data, datetime(2026, 10, 13, 6, tzinfo=UTC))
        stored = tariff_store.all_tariffs(conn)
        assert len(stored) == len(data.tariffs)
        for fuel in Fuel:
            span = DateRange(data.first, data.last)
            days = queries.costing_days(conn, fuel, span)
            result = compare(
                days, [c for c in columns_for(stored, fuel) if c.name == "OVO standard"]
            )
            [column] = result.columns
            assert column.difference == 0
            assert result.included
    finally:
        conn.close()

import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from ovo_insights.domain import Fuel
from ovo_insights.ovo.client import LOGIN_URL, OvoClient
from ovo_insights.storage import open_database
from ovo_insights.storage.repository import Repository
from ovo_insights.sync import store
from ovo_insights.sync.engine import (
    MAX_PERIOD_ERRORS,
    SyncEngine,
    SyncProgress,
    SyncResult,
    plan_sync,
)
from ovo_insights.sync.plan import Granularity
from ovo_insights.sync.store import RunStatus, Trigger
from ovo_insights.timeutil import london_half_hours
from tests.sync.conftest import Clock, make_ovo_client
from tests.sync.fake_ovo import (
    ELEC_MPAN,
    GAS_MPRN,
    FakeAccount,
    FakeOvo,
    FakeSupply,
    bad_json,
    cloudflare_block,
    dual_fuel,
    unavailable,
)

pytestmark = pytest.mark.anyio

TODAY = date(2026, 10, 13)
SMALL_START = date(2026, 9, 20)  # 23 days to yesterday: 1 year, 2 months, 23 days
SMALL = "300003"


async def run(
    client: OvoClient,
    conn: sqlite3.Connection,
    clock: Clock,
    on_progress: Callable[[SyncProgress], None] | None = None,
) -> SyncResult:
    return await SyncEngine(client, conn, now=clock, on_progress=on_progress).run(Trigger.CLI)


def data(conn: sqlite3.Connection) -> dict[str, list[tuple[Any, ...]]]:
    """Every stored usage row and reading, without fetch times."""
    return {
        "half_hourly": conn.execute(
            "SELECT account_id, fuel, interval_start_utc, kwh FROM usage_half_hourly"
            " ORDER BY 1, 2, 3"
        ).fetchall(),
        "daily": conn.execute(
            "SELECT account_id, fuel, day, kwh, cost_milli_pence, has_half_hourly,"
            " unit_rate_milli_pence, standing_charge_milli_pence FROM usage_daily ORDER BY 1, 2, 3"
        ).fetchall(),
        "monthly": conn.execute(
            "SELECT account_id, fuel, month, kwh, cost_milli_pence FROM usage_monthly"
            " ORDER BY 1, 2, 3"
        ).fetchall(),
        "readings": conn.execute("SELECT * FROM readings ORDER BY 1, 2, 3").fetchall(),
    }


def states(conn: sqlite3.Connection, account: str = SMALL) -> dict[tuple[str, str], str]:
    return {
        (g, s): status
        for g, s, status in conn.execute(
            "SELECT granularity, period_start, status FROM sync_state WHERE account_id = ?",
            (account,),
        )
    }


def hh_days(fake: FakeOvo) -> list[str]:
    return [param for _, _, param in fake.usage_calls("half-hourly-local")]


@pytest.fixture
def small(mock: respx.MockRouter) -> FakeOvo:
    return FakeOvo([dual_fuel(SMALL, SMALL_START)], today=TODAY).install(mock)


# -- full backfill ------------------------------------------------------------------------


async def test_owner_shaped_backfill_of_two_accounts_on_shared_supply_points(
    mock: respx.MockRouter, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    # The old account's meter reads give no end date: it is inferred from the new one.
    old = FakeAccount(
        "100001",
        [
            FakeSupply("ELECTRICITY", ELEC_MPAN, date(2017, 9, 26), date(2021, 7, 1), False),
            FakeSupply("GAS", GAS_MPRN, date(2017, 9, 26), date(2021, 7, 1), False),
        ],
    )
    fake = FakeOvo([dual_fuel("200002", date(2021, 8, 13)), old], today=TODAY).install(mock)

    result = await run(client, conn, clock)

    assert (result.status, result.error_class, result.period_errors) == (RunStatus.OK, None, ())
    # The old account is outside the two-year window: only its meter reads are fetched.
    assert {acct for _, acct, _ in fake.usage_calls()} == {"200002"}
    assert [p for _, _, p in fake.usage_calls("monthly")] == ["2026", "2025", "2024"]
    assert len(fake.usage_calls("daily")) == 25  # Oct 2024 .. Oct 2026
    days = hh_days(fake)
    assert (days[0], days[-1], len(days)) == ("2026-10-12", "2024-10-13", 730)
    meter_calls = [c for c in fake.calls if c[2] == "MeterReads_nextV1"]
    assert [acct for _, acct, _ in meter_calls] == ["100001", "200002"]  # oldest first
    assert result.counts.requests == 2 + 1 + 2 + 3 + 25 + 730  # login, accounts, reads, data

    window = [date(2024, 10, 13) + timedelta(days=i) for i in range(730)]
    slots = sum(len(london_half_hours(d)) for d in window)
    assert result.counts.half_hours_written == slots
    assert conn.execute("SELECT count(*) FROM usage_half_hourly").fetchone()[0] == slots
    assert result.counts.gaps_found == result.counts.warnings == 0

    ledger = states(conn, "200002")
    assert ledger[("half_hourly", "2026-10-12")] == "provisional"  # OVO may still revise it
    assert ledger[("half_hourly", "2026-10-10")] == "complete"
    assert ledger[("daily", "2026-10-01")] == "provisional"
    assert ledger[("daily", "2026-09-01")] == "complete"
    assert ledger[("monthly", "2025-01-01")] == "complete"
    assert set(ledger.values()) == {"complete", "provisional"}
    # DST days are complete with 46 and 50 half-hours, not gaps.
    for day, expected in (("2025-03-30", 46), ("2025-10-26", 50)):
        row = conn.execute(
            "SELECT status, expected, received FROM sync_state"
            " WHERE granularity = 'half_hourly' AND period_start = ?",
            (day,),
        ).fetchone()
        assert row == ("complete", expected, expected)

    # Shared meters belong to the current account; readings from both accounts are kept.
    assert {m.account_id for m in Repository(conn).meters("200002")} == {"200002"}
    assert Repository(conn).meters("100001") == []
    assert result.counts.readings_written == 4
    assert conn.execute("SELECT count(*) FROM readings").fetchone()[0] == 4

    # A second sync the same day re-fetches only what OVO may still revise.
    fake.calls.clear()
    before = data(conn)
    second = await run(client, conn, clock)

    assert second.status is RunStatus.OK
    assert [(k, p) for k, _, p in fake.usage_calls()] == [
        ("monthly", "2026"),
        ("daily", "2026-10"),
        ("half-hourly-local", "2026-10-12"),
        ("half-hourly-local", "2026-10-11"),
    ]
    assert data(conn) == before


async def test_incremental_sync_next_day(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    await run(client, conn, clock)
    small.calls.clear()
    clock.now += timedelta(days=1)
    small.today += timedelta(days=1)

    await run(client, conn, clock)

    assert hh_days(small) == ["2026-10-13", "2026-10-12", "2026-10-11"]
    assert states(conn)[("half_hourly", "2026-10-11")] == "complete"


async def test_two_syncs_give_an_identical_database(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    await run(client, conn, clock)
    first = data(conn)
    first_ledger = states(conn)

    await run(client, conn, clock)

    assert data(conn) == first
    assert states(conn) == first_ledger


async def test_revised_recent_data_replaces_the_old_values(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    await run(client, conn, clock)
    small.revision = Decimal("0.001")

    await run(client, conn, clock)

    repo = Repository(conn)
    start, end = datetime(2026, 10, 11, 23, tzinfo=UTC), datetime(2026, 10, 12, 23, tzinfo=UTC)
    revised = repo.half_hourly(SMALL, Fuel.ELECTRICITY, start, end)
    expected = [float(v) for v in small.half_hours(date(2026, 10, 12))]
    assert [r.kwh for r in revised] == expected
    # Final days keep the values they were fetched with.
    old = repo.half_hourly(SMALL, Fuel.ELECTRICITY, datetime(2026, 9, 30, 23, tzinfo=UTC), start)
    assert old[0].kwh != float(small.half_hours(date(2026, 10, 1))[0])


# -- resuming -----------------------------------------------------------------------------


async def test_resume_after_a_failure_at_period_n(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock, tmp_path: Path
) -> None:
    def fail_after_ten(kind: str, account: str, param: str) -> httpx.Response | None:
        if kind == "half-hourly-local" and len(small.usage_calls(kind)) > 10:
            return unavailable()
        return None

    small.intercept = fail_after_ten
    first = await run(client, conn, clock)

    assert (first.status, first.error_class) == (RunStatus.PARTIAL, "UpstreamUnavailable")
    assert first.counts.periods_fetched == 1 + 1 + 10  # monthly, daily Oct, 10 days
    assert len(hh_days(small)) == 10 + 3  # the 11th was tried three times, then the run stopped

    small.intercept = None
    small.calls.clear()
    second = await run(client, conn, clock)

    assert second.status is RunStatus.OK
    fetched = hh_days(small)
    # Days that were final after the first run were not fetched again.
    assert not {f"2026-10-{d:02d}" for d in range(3, 11)} & set(fetched)
    assert len(fetched) == 2 + 2 + 11  # provisional Oct 11-12, then Oct 2-1, Sep 30-20

    other = open_database(tmp_path / "uninterrupted")
    try:
        fresh = make_ovo_client(clock)
        try:
            await run(fresh, other, clock)
        finally:
            await fresh.aclose()
        assert data(conn) == data(other)
    finally:
        other.close()


def network_down() -> httpx.Response | None:
    """What a dropped connection looks like to the client: the request itself fails."""
    raise httpx.ConnectError("network is unreachable")


async def test_losing_the_network_mid_sync_is_a_partial_run_that_resumes_cleanly(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock, tmp_path: Path
) -> None:
    def drops_after_ten(kind: str, account: str, param: str) -> httpx.Response | None:
        if kind == "half-hourly-local" and len(small.usage_calls(kind)) > 10:
            return network_down()
        return None

    small.intercept = drops_after_ten
    first = await run(client, conn, clock)

    assert (first.status, first.error_class) == (RunStatus.PARTIAL, "UpstreamUnavailable")
    assert first.counts.periods_fetched == 1 + 1 + 10  # what arrived before the drop is kept
    [latest] = store.recent_runs(conn, 1)
    assert (latest.status, latest.error_class) == (RunStatus.PARTIAL, "UpstreamUnavailable")
    assert store.breaker(conn).open is False  # a flaky connection isn't OVO saying no

    small.intercept = None  # the network is back
    second = await run(client, conn, clock)
    assert second.status is RunStatus.OK

    other = open_database(tmp_path / "never-dropped")
    try:
        fresh = make_ovo_client(clock)
        try:
            await run(fresh, other, clock)
        finally:
            await fresh.aclose()
        assert data(conn) == data(other)  # no duplicates, no holes
    finally:
        other.close()


async def test_no_network_from_the_start_is_a_failed_run_that_saves_nothing(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    def down(kind: str, account: str, param: str) -> httpx.Response | None:
        return network_down()

    small.intercept = down
    result = await run(client, conn, clock)

    assert (result.status, result.error_class) == (RunStatus.FAILED, "UpstreamUnavailable")
    assert conn.execute("SELECT count(*) FROM usage_half_hourly").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM sync_state").fetchone()[0] == 0


async def test_a_crashed_run_is_closed_by_the_next(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    crashed = store.start_run(conn, Trigger.SCHEDULED, clock.now - timedelta(hours=1))

    await run(client, conn, clock)

    runs = {r.run_id: r for r in store.recent_runs(conn)}
    assert runs[crashed].status is RunStatus.PARTIAL
    assert runs[crashed].error_class == store.INTERRUPTED


async def test_ctrl_c_is_recorded_and_data_so_far_is_kept(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    def interrupt(progress: SyncProgress) -> None:
        if progress.periods_done == 3:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        await run(client, conn, clock, on_progress=interrupt)

    [last] = store.recent_runs(conn, 1)
    assert (last.status, last.error_class) == (RunStatus.PARTIAL, store.INTERRUPTED)
    assert last.counts.periods_fetched == 3
    assert len(states(conn)) == 3


async def test_a_bug_is_recorded_as_failed_and_raised(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    def bug(progress: SyncProgress) -> None:
        if progress.phase == "accounts":
            raise RuntimeError("bug")

    with pytest.raises(RuntimeError, match="bug"):
        await run(client, conn, clock, on_progress=bug)

    [last] = store.recent_runs(conn, 1)
    assert (last.status, last.error_class) == (RunStatus.FAILED, "RuntimeError")


# -- circuit breaker ----------------------------------------------------------------------


async def test_cloudflare_block_stops_at_once_and_opens_the_breaker(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    def block_third_day(kind: str, account: str, param: str) -> httpx.Response | None:
        if kind == "half-hourly-local" and len(small.usage_calls(kind)) == 3:
            return cloudflare_block()
        return None

    small.intercept = block_third_day

    result = await run(client, conn, clock)

    assert (result.status, result.error_class) == (RunStatus.BLOCKED, "BlockedByUpstream")
    assert small.calls[-1] == ("half-hourly-local", SMALL, "2026-10-10")  # nothing after it
    assert len(hh_days(small)) == 3  # never retried
    assert store.breaker(conn) == store.Breaker(open=True, reason="OVO blocked the last sync")
    assert result.counts.half_hours_written == 2 * 48  # what came before is kept


async def test_refused_login_fails_the_run_and_opens_the_breaker(
    mock: respx.MockRouter, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    mock.post(LOGIN_URL).respond(401)

    result = await run(client, conn, clock)

    assert (result.status, result.error_class) == (RunStatus.FAILED, "AuthFailed")
    assert result.counts.requests == 1
    assert store.breaker(conn).reason == "OVO refused the saved login"


async def test_three_failed_runs_open_the_breaker(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    small.intercept = lambda kind, account, param: unavailable() if param == "Bootstrap" else None

    for _ in range(3):
        result = await run(client, conn, clock)
        assert (result.status, result.error_class) == (RunStatus.FAILED, "UpstreamUnavailable")

    assert store.breaker(conn).reason == "the last 3 syncs failed"


async def test_rate_limiting_stops_the_run(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    small.intercept = lambda kind, account, param: (
        httpx.Response(429, headers={"retry-after": "3600"})
        if kind == "half-hourly-local"
        else None
    )

    result = await run(client, conn, clock)

    assert (result.status, result.error_class) == (RunStatus.PARTIAL, "RateLimited")
    assert len(hh_days(small)) == 1


# -- completeness -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "today", "dst_day", "slots"),
    [
        (date(2025, 3, 25), date(2025, 4, 10), "2025-03-30", 46),
        (date(2025, 10, 20), date(2025, 11, 10), "2025-10-26", 50),
    ],
)
async def test_dst_days_are_complete_with_46_or_50_half_hours(
    mock: respx.MockRouter,
    client: OvoClient,
    conn: sqlite3.Connection,
    clock: Clock,
    start: date,
    today: date,
    dst_day: str,
    slots: int,
) -> None:
    FakeOvo([dual_fuel(SMALL, start)], today=today).install(mock)
    clock.now = datetime(today.year, today.month, today.day, 9, tzinfo=UTC)

    await run(client, conn, clock)

    assert conn.execute(
        "SELECT status, expected, received FROM sync_state WHERE period_start = ?"
        " AND granularity = 'half_hourly'",
        (dst_day,),
    ).fetchone() == ("complete", slots, slots)


async def test_an_incomplete_day_is_retried_then_becomes_a_known_gap(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    small.short_days[date(2026, 10, 5)] = 1
    await run(client, conn, clock)
    assert states(conn)[("half_hourly", "2026-10-05")] == "incomplete"

    small.calls.clear()
    clock.now += timedelta(days=1)
    await run(client, conn, clock)
    assert "2026-10-05" in hh_days(small)

    clock.now = datetime(2026, 10, 20, 9, tzinfo=UTC)  # 15 days after: past the horizon
    result = await run(client, conn, clock)
    assert states(conn)[("half_hourly", "2026-10-05")] == "gap"
    assert result.counts.gaps_found == 1
    assert result.status is RunStatus.OK  # missing data at OVO's end isn't a failed sync

    small.calls.clear()
    await run(client, conn, clock)
    assert "2026-10-05" not in hh_days(small)  # known gaps are never fetched again


async def test_an_old_incomplete_day_is_a_gap_straight_away(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    small.short_days[date(2026, 9, 21)] = 48  # nothing at all

    result = await run(client, conn, clock)

    assert states(conn)[("half_hourly", "2026-09-21")] == "gap"
    assert result.counts.gaps_found == 1


async def test_gas_half_hours_are_expected_only_when_ovo_says_it_has_them(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    small.gas_half_hourly = True

    await run(client, conn, clock)

    row = conn.execute(
        "SELECT status, expected FROM sync_state WHERE period_start = '2026-10-01'"
        " AND granularity = 'half_hourly'"
    ).fetchone()
    assert row == ("complete", 96)
    assert (
        conn.execute("SELECT count(*) FROM usage_half_hourly WHERE fuel = 'gas'").fetchone()[0]
        == 23 * 48
    )


async def test_half_hours_sent_despite_the_daily_flag_are_expected_in_full(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    # Seen on the real account: hasHhData false on days that had all 48 slots.
    small.hh_flag_off |= {date(2026, 10, 4), date(2026, 10, 5)}
    small.short_days[date(2026, 10, 5)] = 2

    await run(client, conn, clock)

    assert conn.execute(
        "SELECT status, expected, received FROM sync_state WHERE granularity = 'half_hourly'"
        " AND period_start = '2026-10-04'"
    ).fetchone() == ("complete", 48, 48)
    assert conn.execute(
        "SELECT status, expected, received FROM sync_state WHERE granularity = 'half_hourly'"
        " AND period_start = '2026-10-05'"
    ).fetchone() == ("incomplete", 48, 46)


def hh_row(conn: sqlite3.Connection, day: str) -> tuple[str, int, int]:
    status, expected, received = conn.execute(
        "SELECT status, expected, received FROM sync_state"
        " WHERE granularity = 'half_hourly' AND period_start = ? AND account_id = ?",
        (day, SMALL),
    ).fetchone()
    return status, expected, received


async def test_a_day_without_a_daily_row_has_complete_half_hours_but_an_incomplete_daily_month(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    small.missing_daily.add(date(2026, 10, 5))

    await run(client, conn, clock)

    ledger = states(conn)
    assert ledger[("daily", "2026-10-01")] == "incomplete"  # the daily row is still awaited
    assert ledger[("half_hourly", "2026-10-04")] == "complete"
    # Every half-hour of the electricity is there, and gas has never had any, so the day is
    # whole: it isn't held back by a daily row that hasn't been published.
    assert hh_row(conn, "2026-10-05") == ("complete", 48, 48)


async def test_yesterday_is_covered_as_soon_as_its_half_hours_are_in_before_its_daily_row(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    """The reported bug: OVO had published all of yesterday's half-hours but not yet its daily
    row. The day was expected to have 96 half-hours (48 for gas, which has none) so it stayed
    `incomplete`, and the Sync page's "What's downloaded" stopped a day short."""
    yesterday = date(2026, 10, 12)
    small.missing_daily.add(yesterday)

    await run(client, conn, clock)

    assert hh_row(conn, "2026-10-12") == ("provisional", 48, 48)  # still revisable, but covered
    [coverage] = [
        c for c in store.coverage(conn, SMALL) if c.granularity is Granularity.HALF_HOURLY
    ]
    assert coverage.latest == yesterday
    assert coverage.pending == 0
    # When the daily row turns up, nothing changes for the half-hours.
    small.missing_daily.clear()
    await run(client, conn, clock)
    assert hh_row(conn, "2026-10-12") == ("provisional", 48, 48)


async def test_a_short_day_without_a_daily_row_is_still_incomplete(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    day = date(2026, 10, 12)
    small.missing_daily.add(day)
    small.short_days[day] = 46  # only the first two half-hours have been published

    await run(client, conn, clock)

    assert hh_row(conn, "2026-10-12") == ("incomplete", 48, 2)
    [coverage] = [
        c for c in store.coverage(conn, SMALL) if c.granularity is Granularity.HALF_HOURLY
    ]
    # The two half-hours that have been published are data we hold, so the dates cover the day,
    # but it is still counted as one to retry rather than as complete.
    assert coverage.latest == date(2026, 10, 12)
    assert coverage.pending == 1


async def test_a_day_with_no_half_hours_and_no_daily_row_is_never_called_whole(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    day = date(2026, 10, 12)
    small.missing_daily.add(day)
    small.short_days[day] = 48  # nothing published yet

    await run(client, conn, clock)

    # Electricity had half-hours on earlier days, so a full day is expected of it.
    assert hh_row(conn, "2026-10-12") == ("incomplete", 48, 0)


async def test_with_no_history_and_nothing_published_the_day_is_not_called_whole(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    """The first day of an account, no daily row, no half-hours: there is nothing to say what
    to expect, so it is not called whole (and is expected to have both fuels' slots). It is old
    enough that it is a known gap straight away, as any incomplete day that old is."""
    first = SMALL_START
    small.missing_daily.add(first)
    small.short_days[first] = 48

    await run(client, conn, clock)

    assert hh_row(conn, first.isoformat()) == ("gap", 96, 0)


async def test_a_fuel_that_has_half_hours_is_waited_for_even_before_its_daily_row(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    small.gas_half_hourly = True
    day = date(2026, 10, 12)
    small.missing_daily.add(day)

    await run(client, conn, clock)
    assert hh_row(conn, "2026-10-12") == ("provisional", 96, 96)  # both fuels, all there

    small.short_days[day] = 4  # now the last two hours are missing for both
    small.revision = Decimal("0.001")  # (so the day is re-fetched and re-judged)
    await run(client, conn, clock)
    assert hh_row(conn, "2026-10-12") == ("incomplete", 96, 88)


async def test_daily_and_half_hourly_totals_that_disagree_are_flagged_not_rejected(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    small.daily_offset[date(2026, 10, 5)] = Decimal(2)

    result = await run(client, conn, clock)

    assert result.counts.warnings == 1
    status, note = conn.execute(
        "SELECT status, note FROM sync_state WHERE period_start = '2026-10-05'"
        " AND granularity = 'half_hourly'"
    ).fetchone()
    assert status == "complete"
    assert note.startswith("electricity: daily ")


async def test_rows_outside_an_accounts_supply_period_are_not_stored(
    mock: respx.MockRouter, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    old = dual_fuel("100001", date(2026, 9, 20), date(2026, 10, 1))
    new = dual_fuel("200002", date(2026, 10, 2))
    fake = FakeOvo([old, new], today=TODAY, ignore_supply_period=True).install(mock)

    await run(client, conn, clock)

    old_days = conn.execute(
        "SELECT max(day) FROM usage_daily WHERE account_id = '100001'"
    ).fetchone()[0]
    assert old_days == "2026-10-01"
    old_hh = [p for _, acct, p in fake.usage_calls("half-hourly-local") if acct == "100001"]
    assert (min(old_hh), max(old_hh)) == ("2026-09-20", "2026-10-01")


# -- validation failures ------------------------------------------------------------------


async def test_one_bad_response_marks_that_period_and_is_retried_last(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    small.intercept = lambda kind, account, param: bad_json() if param == "2026-10-05" else None

    result = await run(client, conn, clock)

    assert (result.status, result.error_class) == (RunStatus.PARTIAL, "UpstreamSchemaChanged")
    assert result.period_errors == ("UpstreamSchemaChanged",)
    assert states(conn)[("half_hourly", "2026-10-05")] == "error"
    assert states(conn)[("half_hourly", "2026-10-04")] == "complete"

    small.intercept = None
    small.calls.clear()
    clock.now += timedelta(days=4)  # Oct 11-12 are now final too
    second = await run(client, conn, clock)

    assert second.status is RunStatus.OK
    assert hh_days(small)[-1] == "2026-10-05"
    assert states(conn)[("half_hourly", "2026-10-05")] == "complete"


async def test_many_bad_responses_stop_the_run(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    small.intercept = lambda kind, account, param: (
        bad_json() if kind == "half-hourly-local" else None
    )

    result = await run(client, conn, clock)

    assert (result.status, result.error_class) == (RunStatus.PARTIAL, "UpstreamSchemaChanged")
    assert len(hh_days(small)) == MAX_PERIOD_ERRORS


def _one_slot(start: str, repeat: int = 1) -> httpx.Response:
    item = {"consumption": 0.1, "interval": {"start": start}, "unit": "kwh"}
    return httpx.Response(200, json={"electricity": {"data": [item] * repeat}})


@pytest.mark.parametrize(
    "response",
    [
        _one_slot("2026-10-06T12:00:00.000"),  # a slot from another day
        _one_slot("2026-10-05T12:00:00.000", repeat=2),  # the same slot twice
    ],
)
async def test_half_hourly_responses_must_be_exactly_the_requested_day(
    small: FakeOvo,
    client: OvoClient,
    conn: sqlite3.Connection,
    clock: Clock,
    response: httpx.Response,
) -> None:
    small.intercept = lambda kind, account, param: response if param == "2026-10-05" else None

    await run(client, conn, clock)

    assert states(conn)[("half_hourly", "2026-10-05")] == "error"


async def test_repeated_daily_rows_are_refused(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    def doubled(kind: str, account: str, param: str) -> httpx.Response | None:
        if kind != "daily" or param != "2026-10":
            return None
        body = small._daily(account, 2026, 10)
        body["electricity"]["data"] *= 2
        return httpx.Response(200, json=body)

    small.intercept = doubled

    await run(client, conn, clock)

    assert states(conn)[("daily", "2026-10-01")] == "error"
    assert (
        conn.execute("SELECT count(*) FROM usage_daily WHERE day >= '2026-10-01'").fetchone()[0]
        == 0
    )


async def test_bad_meter_reads_fall_back_to_bootstrap_supply_points(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    small.intercept = lambda kind, account, param: (
        httpx.Response(200, json={"data": {"account": {"id": SMALL}}})
        if param == "MeterReads_nextV1"
        else None
    )

    result = await run(client, conn, clock)

    assert result.status is RunStatus.PARTIAL
    assert result.counts.half_hours_written == 23 * 48  # usage still synced
    assert result.counts.readings_written == 0


async def test_inconsistent_meter_reads_are_not_stored(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    def orphan_reading(kind: str, account: str, param: str) -> httpx.Response | None:
        if param != "MeterReads_nextV1":
            return None
        body = small._meter_reads(account)
        body["data"]["account"]["accountSupplyPoints"][1]["meterReads_nextV1"]["edges"][0]["node"][
            "reading"
        ]["meterSerialNumber"] = "UNKNOWN1"
        return httpx.Response(200, json=body)

    small.intercept = orphan_reading

    result = await run(client, conn, clock)

    assert result.status is RunStatus.PARTIAL
    assert result.period_errors == ("UpstreamSchemaChanged",)
    assert conn.execute("SELECT count(*) FROM meters").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM readings").fetchone()[0] == 0


async def test_an_account_listed_twice_fails_the_run(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    small.accounts.append(small.accounts[0])

    result = await run(client, conn, clock)

    assert (result.status, result.error_class) == (RunStatus.FAILED, "UpstreamSchemaChanged")


# -- progress and dry run -----------------------------------------------------------------


async def test_progress_is_reported(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    seen: list[tuple[bool, str, int, int]] = []
    engine = SyncEngine(
        client,
        conn,
        now=clock,
        on_progress=lambda p: seen.append((p.running, p.phase, p.periods_done, p.periods_total)),
    )

    await engine.run(Trigger.MANUAL)

    assert seen[0] == (True, "starting", 0, 0)
    assert (True, "fetching", 0, 26) in seen
    assert seen[-1] == (False, "ok", 26, 26)
    assert engine.progress.account is not None
    assert engine.progress.account.count(Granularity.HALF_HOURLY) == 23
    assert store.recent_runs(conn, 1)[0].trigger is Trigger.MANUAL

    seen.clear()
    await engine.run(Trigger.MANUAL)  # a reused progress object starts from zero again
    assert seen[0] == (True, "starting", 0, 0)


async def test_engine_works_without_a_progress_listener(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    result = await SyncEngine(client, conn, now=clock).run(Trigger.SCHEDULED)

    assert result.status is RunStatus.OK


async def test_dry_run_plans_without_writing(
    small: FakeOvo, client: OvoClient, conn: sqlite3.Connection, clock: Clock
) -> None:
    empty = await plan_sync(client, None, now=clock)
    assert empty.periods == 26
    [account] = empty.accounts
    assert account.first_supply == SMALL_START
    assert conn.execute("SELECT count(*) FROM sync_runs").fetchone()[0] == 0

    await run(client, conn, clock)
    before = data(conn)
    later = await plan_sync(client, conn, now=clock)

    assert later.periods == 1 + 1 + 2  # this year, this month, the last two days
    assert data(conn) == before


async def test_dry_run_reports_bad_meter_reads(
    small: FakeOvo, client: OvoClient, clock: Clock
) -> None:
    small.intercept = lambda kind, account, param: (
        bad_json() if param == "MeterReads_nextV1" else None
    )

    plan = await plan_sync(client, None, now=clock)

    assert plan.discovery_errors == ("UpstreamSchemaChanged",)
    assert plan.periods == 26


def test_default_clocks_are_utc() -> None:
    engine_defaults = SyncEngine.__init__.__kwdefaults__
    plan_defaults = plan_sync.__kwdefaults__
    assert engine_defaults is not None
    assert plan_defaults is not None
    assert engine_defaults["now"]().tzinfo is UTC
    assert plan_defaults["now"]().tzinfo is UTC

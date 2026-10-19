import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from ovo_insights.domain import DailyUsage, Fuel, HalfHourUsage, MonthlyUsage
from ovo_insights.storage.repository import Repository
from ovo_insights.sync import store
from ovo_insights.sync.plan import Granularity, Period, PeriodStatus
from ovo_insights.sync.store import Breaker, RunCounts, RunStatus, Trigger
from ovo_insights.timeutil import london_half_hours

T0 = datetime(2026, 10, 13, 7, 0, tzinfo=UTC)


def finished_run(
    conn: sqlite3.Connection, status: RunStatus, error: str | None = None, *, at: int = 0
) -> int:
    started = T0 + timedelta(hours=at)
    run_id = store.start_run(conn, Trigger.SCHEDULED, started)
    store.finish_run(conn, run_id, status, error, RunCounts(requests=3), started)
    return run_id


def test_run_round_trip(conn: sqlite3.Connection) -> None:
    run_id = store.start_run(conn, Trigger.CLI, T0)
    [running] = store.recent_runs(conn)
    assert (running.status, running.finished_at) == (RunStatus.RUNNING, None)

    counts = RunCounts(5, 4, 1, 2, 96, 30, 2, 7)
    store.finish_run(conn, run_id, RunStatus.PARTIAL, "UpstreamUnavailable", counts, T0)

    [run] = store.recent_runs(conn)
    assert run == store.SyncRun(
        run_id, Trigger.CLI, T0, T0, RunStatus.PARTIAL, "UpstreamUnavailable", counts
    )


def test_finish_run_validation(conn: sqlite3.Connection) -> None:
    run_id = store.start_run(conn, Trigger.CLI, T0)

    with pytest.raises(ValueError, match="final status"):
        store.finish_run(conn, run_id, RunStatus.RUNNING, None, RunCounts(), T0)
    with pytest.raises(ValueError, match="error class"):
        store.finish_run(conn, run_id, RunStatus.FAILED, "Bad class!", RunCounts(), T0)
    with pytest.raises(ValueError, match="non-negative"):
        RunCounts(requests=-1)
    with pytest.raises(ValueError, match="non-negative"):
        RunCounts(warnings=True)


def test_schema_rejects_inconsistent_runs(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sync_runs (trigger, started_at_utc, status) VALUES ('cli', ?, 'ok')",
            ("2026-10-13T07:00:00Z",),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sync_runs (trigger, started_at_utc, finished_at_utc, status,"
            " error_class) VALUES ('cli', ?, ?, 'ok', 'AuthFailed')",
            ("2026-10-13T07:00:00Z", "2026-10-13T07:01:00Z"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sync_runs (trigger, started_at_utc, finished_at_utc, status)"
            " VALUES ('cli', ?, ?, 'ok')",
            ("2026-10-13T07:00:00Z", "2026-10-13T06:00:00Z"),
        )


def test_corrupt_error_class_is_refused_on_read(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO sync_runs (trigger, started_at_utc, finished_at_utc, status, error_class)"
        " VALUES ('cli', ?, ?, 'failed', 'AuthFailed')",
        ("2026-10-13T07:00:00Z", "2026-10-13T07:01:00Z"),
    )
    # Simulate a hand-edited database: the schema's own CHECK would refuse this value.
    conn.execute("PRAGMA ignore_check_constraints = ON")
    conn.execute("UPDATE sync_runs SET error_class = 'Auth Failed'")

    with pytest.raises(ValueError, match="invalid error class"):
        store.recent_runs(conn)


def test_interrupted_runs_are_closed_as_partial(conn: sqlite3.Connection) -> None:
    store.start_run(conn, Trigger.CLI, T0)
    finished_run(conn, RunStatus.OK)

    assert store.close_interrupted_runs(conn, T0 + timedelta(minutes=5)) == 1
    assert store.close_interrupted_runs(conn, T0) == 0
    interrupted = store.recent_runs(conn)[-1]
    assert interrupted.status is RunStatus.PARTIAL
    assert interrupted.error_class == store.INTERRUPTED
    assert interrupted.finished_at == T0 + timedelta(minutes=5)


def test_error_class_name_never_includes_the_message() -> None:
    class Odd_Error1(Exception):
        pass

    assert store.error_class_name(ValueError("secret detail")) == "ValueError"
    assert store.error_class_name(Odd_Error1()) == "Error"


# -- circuit breaker ----------------------------------------------------------------------


def test_breaker_closed_with_no_history(conn: sqlite3.Connection) -> None:
    store.start_run(conn, Trigger.CLI, T0)  # still running: not counted

    assert store.breaker(conn) == Breaker(open=False)


@pytest.mark.parametrize(
    ("status", "error", "reason"),
    [
        (RunStatus.BLOCKED, "BlockedByUpstream", "OVO blocked the last sync"),
        (RunStatus.FAILED, "AuthFailed", "OVO refused the saved login"),
        (RunStatus.PARTIAL, "Unauthorized", "OVO refused access to the account data"),
    ],
)
def test_breaker_opens_when_ovo_says_no(
    conn: sqlite3.Connection, status: RunStatus, error: str, reason: str
) -> None:
    finished_run(conn, RunStatus.OK)
    finished_run(conn, status, error, at=1)

    assert store.breaker(conn) == Breaker(open=True, reason=reason)


def test_breaker_opens_after_three_failed_runs_and_a_success_closes_it(
    conn: sqlite3.Connection,
) -> None:
    for hour in range(2):
        finished_run(conn, RunStatus.FAILED, "UpstreamUnavailable", at=hour)
    assert not store.breaker(conn).open

    finished_run(conn, RunStatus.FAILED, "UpstreamUnavailable", at=2)
    assert store.breaker(conn) == Breaker(open=True, reason="the last 3 syncs failed")

    finished_run(conn, RunStatus.OK, at=3)  # e.g. the user pressed "Sync now"
    assert not store.breaker(conn).open


def test_partial_runs_do_not_count_as_failures(conn: sqlite3.Connection) -> None:
    for hour in range(3):
        finished_run(conn, RunStatus.PARTIAL, "UpstreamSchemaChanged", at=hour)

    assert not store.breaker(conn).open


# -- period ledger ------------------------------------------------------------------------


def test_record_period_counts_attempts_and_truncates_notes(
    conn: sqlite3.Connection, repo: Repository
) -> None:
    repo.upsert_account("acct1")
    run_id = store.start_run(conn, Trigger.CLI, T0)
    period = Period("acct1", Granularity.HALF_HOURLY, date(2026, 10, 10))

    for status in (PeriodStatus.INCOMPLETE, PeriodStatus.PROVISIONAL):
        store.record_period(
            conn,
            period,
            status,
            expected=48,
            received=47,
            fetched_at=T0,
            run_id=run_id,
            note="n" * 500,
        )

    attempts, note = conn.execute("SELECT attempts, note FROM sync_state").fetchone()
    assert (attempts, len(note)) == (2, 200)
    assert store.period_states(conn, "acct1") == {
        (Granularity.HALF_HOURLY, date(2026, 10, 10)): PeriodStatus.PROVISIONAL
    }
    assert store.period_states(conn, "other") == {}


def test_schema_rejects_misaligned_periods(conn: sqlite3.Connection, repo: Repository) -> None:
    repo.upsert_account("acct1")
    for granularity, start in (("monthly", "2026-02-01"), ("daily", "2026-02-02")):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sync_state (account_id, granularity, period_start, status,"
                " expected, received, attempts, last_fetched_at_utc)"
                " VALUES ('acct1', ?, ?, 'complete', 1, 1, 1, '2026-10-13T07:00:00Z')",
                (granularity, start),
            )


def put_data(repo: Repository, account: str, days: list[date]) -> None:
    """Store a daily row, a month's total and every half-hour for each day."""
    repo.upsert_daily(
        [DailyUsage(account, Fuel.ELECTRICITY, d, 5.0, None, True) for d in days], fetched_at=T0
    )
    repo.upsert_monthly(
        [MonthlyUsage(account, Fuel.ELECTRICITY, d.replace(day=1), 100.0, None) for d in days],
        fetched_at=T0,
    )
    repo.upsert_half_hourly(
        [
            HalfHourUsage(account, Fuel.ELECTRICITY, start, 0.1)
            for d in days
            for start in london_half_hours(d)
        ],
        fetched_at=T0,
    )


def test_coverage_dates_are_the_data_held_and_the_counts_are_the_syncs(
    conn: sqlite3.Connection, repo: Repository
) -> None:
    repo.upsert_account("acct1")
    run_id = store.start_run(conn, Trigger.CLI, T0)

    def record(granularity: Granularity, start: date, status: PeriodStatus) -> None:
        store.record_period(
            conn,
            Period("acct1", granularity, start),
            status,
            expected=1,
            received=1,
            fetched_at=T0,
            run_id=run_id,
        )

    # The data held reaches back to the 2nd and forward to the 11th ...
    put_data(repo, "acct1", [date(2026, 10, 2), date(2026, 10, 11)])
    # ... while the sync's bookkeeping says other things about other days.
    record(Granularity.HALF_HOURLY, date(2026, 10, 1), PeriodStatus.GAP)
    record(Granularity.HALF_HOURLY, date(2026, 10, 2), PeriodStatus.COMPLETE)
    record(Granularity.HALF_HOURLY, date(2026, 10, 11), PeriodStatus.PROVISIONAL)
    record(Granularity.HALF_HOURLY, date(2026, 10, 12), PeriodStatus.ERROR)
    record(Granularity.DAILY, date(2026, 10, 1), PeriodStatus.INCOMPLETE)
    record(Granularity.MONTHLY, date(2026, 1, 1), PeriodStatus.PROVISIONAL)

    assert store.coverage(conn, "acct1") == [
        store.Coverage(Granularity.MONTHLY, date(2026, 10, 1), date(2026, 10, 1), 0, 1, 0, 0),
        store.Coverage(Granularity.DAILY, date(2026, 10, 2), date(2026, 10, 11), 0, 0, 0, 1),
        store.Coverage(Granularity.HALF_HOURLY, date(2026, 10, 2), date(2026, 10, 11), 1, 1, 1, 1),
    ]


def test_coverage_only_widens_as_data_is_added_whatever_the_syncs_window(
    conn: sqlite3.Connection, repo: Repository
) -> None:
    """A later sync looks at a window that starts later, but the dates shown keep the old
    data: they describe what is held, not what the latest sync touched."""
    repo.upsert_account("acct1")
    run_id = store.start_run(conn, Trigger.CLI, T0)
    put_data(repo, "acct1", [date(2024, 10, 13), date(2025, 2, 11), date(2026, 10, 12)])
    # Only recent days are in the ledger now (older ones aged out of the window).
    store.record_period(
        conn,
        Period("acct1", Granularity.HALF_HOURLY, date(2026, 10, 12)),
        PeriodStatus.PROVISIONAL,
        expected=48,
        received=48,
        fetched_at=T0,
        run_id=run_id,
    )
    [half_hourly] = store.coverage(conn, "acct1")
    assert (half_hourly.earliest, half_hourly.latest) == (date(2024, 10, 13), date(2026, 10, 12))

    put_data(repo, "acct1", [date(2026, 10, 13)])  # the next sync adds a day
    [half_hourly] = store.coverage(conn, "acct1")
    assert (half_hourly.earliest, half_hourly.latest) == (date(2024, 10, 13), date(2026, 10, 13))


def test_coverage_gives_uk_days_for_half_hours_and_keeps_accounts_apart(
    conn: sqlite3.Connection, repo: Repository
) -> None:
    repo.upsert_account("acct1")
    repo.upsert_account("acct2")
    run_id = store.start_run(conn, Trigger.CLI, T0)
    for account in ("acct1", "acct2"):
        store.record_period(
            conn,
            Period(account, Granularity.HALF_HOURLY, date(2026, 7, 2)),
            PeriodStatus.COMPLETE,
            expected=1,
            received=1,
            fetched_at=T0,
            run_id=run_id,
        )
    # In summer, 23:00 UTC is midnight in the UK: the first half-hour of the *next* UK day.
    repo.upsert_half_hourly(
        [
            HalfHourUsage("acct1", Fuel.ELECTRICITY, datetime(2026, 7, 1, 23, 0, tzinfo=UTC), 0.1),
            HalfHourUsage("acct1", Fuel.ELECTRICITY, datetime(2026, 7, 2, 22, 30, tzinfo=UTC), 0.1),
        ],
        fetched_at=T0,
    )
    [first] = store.coverage(conn, "acct1")
    assert (first.earliest, first.latest) == (date(2026, 7, 2), date(2026, 7, 2))
    assert (first.complete, first.provisional) == (1, 0)
    [second] = store.coverage(conn, "acct2")  # a ledger with no data held
    assert (second.earliest, second.latest) == (None, None)
    assert second.complete == 1


def test_every_way_a_run_can_stop_has_a_plain_explanation() -> None:
    from ovo_insights.ovo.errors import OvoError

    def subclasses(cls: type[Exception]) -> set[type[Exception]]:
        return {sub for child in cls.__subclasses__() for sub in {child, *subclasses(child)}}

    names = {c.__name__ for c in subclasses(OvoError)} | {store.INTERRUPTED}
    assert names <= set(store.EXPLANATIONS)  # a new error class must say what it means
    assert all(store.explain(name) for name in names)
    for text in store.EXPLANATIONS.values():
        assert text.endswith(".")
        assert "Exception" not in text


def test_explain_has_nothing_to_say_for_a_clean_run_and_names_the_unexpected() -> None:
    assert store.explain(None) is None
    assert store.explain("WeirdError") == "Something unexpected stopped it (WeirdError)."

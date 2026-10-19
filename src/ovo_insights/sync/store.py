"""Sync bookkeeping: the `sync_runs` history and the per-period `sync_state` ledger.

The circuit breaker is derived from the run history rather than stored separately, so it
can't drift out of step with what actually happened.
"""

import re
import sqlite3
from dataclasses import dataclass, fields
from datetime import date, datetime
from enum import StrEnum

from ovo_insights.domain import validate_identifier
from ovo_insights.storage.db import transaction
from ovo_insights.sync.plan import Granularity, Period, PeriodStatus
from ovo_insights.timeutil import format_utc, london_date, parse_utc

# Errors after which automatic syncing stops until the user asks for a sync: OVO has said no,
# and asking again on a timer would be impolite (and could get the user blocked).
BREAKER_ERRORS = frozenset({"BlockedByUpstream", "AuthFailed", "Unauthorized"})
BREAKER_FAILED_RUNS = 3
INTERRUPTED = "Interrupted"

_ERROR_CLASS = re.compile(r"[A-Za-z]{1,64}", re.ASCII)
_NOTE_LIMIT = 200


# What each way a run can stop means, in plain words (the class name alone isn't one). Shown on
# the Sync page; the key is always an exception class name, never a message from OVO.
EXPLANATIONS = {
    "UpstreamUnavailable": (
        "OVO couldn't be reached, or kept failing: a network problem, or OVO being down."
    ),
    "RateLimited": "OVO asked the app to slow down by more than it was willing to wait.",
    "BlockedByUpstream": "OVO blocked the request. Wait several hours before trying again.",
    "AuthFailed": "OVO didn't accept the saved login. Save it again on the Setup page.",
    "Unauthorized": "OVO refused access to the account data, even after logging in again.",
    "UpstreamSchemaChanged": (
        "OVO sent data in a shape the app doesn't understand, so its API may have changed. "
        "Run `ovo-insights doctor` to see which part."
    ),
    "DisallowedHostError": "A request to an address the app doesn't allow was refused.",
    "ResponseTooLarge": "OVO sent a much larger response than expected, so it was refused.",
    INTERRUPTED: "The app was closed or stopped while it was syncing.",
}


def explain(error_class: str | None) -> str | None:
    """A plain-language reason a run stopped, or None for a run that didn't stop early."""
    if error_class is None:
        return None
    return EXPLANATIONS.get(error_class, f"Something unexpected stopped it ({error_class}).")


class Trigger(StrEnum):
    CLI = "cli"
    MANUAL = "manual"
    SCHEDULED = "scheduled"


class RunStatus(StrEnum):
    RUNNING = "running"
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass(slots=True)
class RunCounts:
    requests: int = 0
    periods_fetched: int = 0
    gaps_found: int = 0
    warnings: int = 0
    half_hours_written: int = 0
    days_written: int = 0
    months_written: int = 0
    readings_written: int = 0

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{f.name} must be a non-negative int, not {value!r}")


@dataclass(frozen=True, slots=True)
class SyncRun:
    run_id: int
    trigger: Trigger
    started_at: datetime
    finished_at: datetime | None
    status: RunStatus
    error_class: str | None
    counts: RunCounts


@dataclass(frozen=True, slots=True)
class Breaker:
    open: bool
    reason: str | None = None


def error_class_name(exc: BaseException) -> str:
    """The exception's class name, if it is a safe identifier; never its message."""
    name = type(exc).__name__
    return name if _ERROR_CLASS.fullmatch(name) else "Error"


# -- runs ---------------------------------------------------------------------------------


def start_run(conn: sqlite3.Connection, trigger: Trigger, now: datetime) -> int:
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO sync_runs (trigger, started_at_utc, status) VALUES (?, ?, 'running')",
            (Trigger(trigger).value, format_utc(now)),
        )
    run_id = cursor.lastrowid
    if run_id is None:  # pragma: no cover - sqlite always sets it after an INSERT
        raise RuntimeError("Could not record the sync run")
    return run_id


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: RunStatus,
    error_class: str | None,
    counts: RunCounts,
    now: datetime,
) -> None:
    if status is RunStatus.RUNNING:
        raise ValueError("A finished run needs a final status")
    if error_class is not None and not _ERROR_CLASS.fullmatch(error_class):
        raise ValueError(f"Invalid error class: {error_class!r}")
    with transaction(conn):
        conn.execute(
            "UPDATE sync_runs SET finished_at_utc = ?, status = ?, error_class = ?,"
            " requests = ?, periods_fetched = ?, gaps_found = ?, warnings = ?,"
            " half_hours_written = ?, days_written = ?, months_written = ?,"
            " readings_written = ? WHERE run_id = ? AND status = 'running'",
            (
                format_utc(now),
                status.value,
                error_class,
                counts.requests,
                counts.periods_fetched,
                counts.gaps_found,
                counts.warnings,
                counts.half_hours_written,
                counts.days_written,
                counts.months_written,
                counts.readings_written,
                run_id,
            ),
        )


def close_interrupted_runs(conn: sqlite3.Connection, now: datetime) -> int:
    """Close runs left 'running' by a crash or kill. Call only while holding the sync lock."""
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE sync_runs SET status = 'partial', error_class = 'Interrupted',"
            " finished_at_utc = max(started_at_utc, ?) WHERE status = 'running'",
            (format_utc(now),),
        )
    return cursor.rowcount


def recent_runs(conn: sqlite3.Connection, limit: int = 10) -> list[SyncRun]:
    rows = conn.execute(
        "SELECT run_id, trigger, started_at_utc, finished_at_utc, status, error_class,"
        " requests, periods_fetched, gaps_found, warnings, half_hours_written, days_written,"
        " months_written, readings_written FROM sync_runs ORDER BY run_id DESC LIMIT ?",
        (limit,),
    )
    result = []
    for run_id, trig, started, finished, status, error, *counts in rows:
        if error is not None and not _ERROR_CLASS.fullmatch(error):
            raise ValueError(f"Corrupt sync run {run_id}: invalid error class")
        result.append(
            SyncRun(
                run_id,
                Trigger(trig),
                parse_utc(started),
                None if finished is None else parse_utc(finished),
                RunStatus(status),
                error,
                RunCounts(*counts),
            )
        )
    return result


def breaker(conn: sqlite3.Connection) -> Breaker:
    """Whether automatic syncing is paused, and why. A manual sync always runs."""
    finished = [r for r in recent_runs(conn, BREAKER_FAILED_RUNS + 5) if r.finished_at]
    if not finished:
        return Breaker(open=False)
    latest = finished[0]
    if latest.error_class in BREAKER_ERRORS:
        reasons = {
            "BlockedByUpstream": "OVO blocked the last sync",
            "AuthFailed": "OVO refused the saved login",
            "Unauthorized": "OVO refused access to the account data",
        }
        return Breaker(open=True, reason=reasons[latest.error_class])
    window = finished[:BREAKER_FAILED_RUNS]
    if len(window) == BREAKER_FAILED_RUNS and all(
        r.status in (RunStatus.FAILED, RunStatus.BLOCKED) for r in window
    ):
        return Breaker(open=True, reason=f"the last {BREAKER_FAILED_RUNS} syncs failed")
    return Breaker(open=False)


# -- periods ------------------------------------------------------------------------------


def period_states(
    conn: sqlite3.Connection, account_id: str
) -> dict[tuple[Granularity, date], PeriodStatus]:
    validate_identifier(account_id, "account id")
    rows = conn.execute(
        "SELECT granularity, period_start, status FROM sync_state WHERE account_id = ?",
        (account_id,),
    )
    return {
        (Granularity(g), date.fromisoformat(start)): PeriodStatus(status)
        for g, start, status in rows
    }


def record_period(
    conn: sqlite3.Connection,
    period: Period,
    status: PeriodStatus,
    *,
    expected: int,
    received: int,
    fetched_at: datetime,
    run_id: int,
    note: str | None = None,
) -> None:
    """Upsert a period's state; joins the caller's transaction so it commits with the data."""
    if note is not None:
        note = note[:_NOTE_LIMIT]
    with transaction(conn):
        conn.execute(
            "INSERT INTO sync_state (account_id, granularity, period_start, status, expected,"
            " received, attempts, last_fetched_at_utc, last_run_id, note)"
            " VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)"
            " ON CONFLICT (account_id, granularity, period_start) DO UPDATE SET"
            " status = excluded.status, expected = excluded.expected,"
            " received = excluded.received, attempts = sync_state.attempts + 1,"
            " last_fetched_at_utc = excluded.last_fetched_at_utc,"
            " last_run_id = excluded.last_run_id, note = excluded.note",
            (
                period.account_id,
                period.granularity.value,
                period.start.isoformat(),
                PeriodStatus(status).value,
                expected,
                received,
                format_utc(fetched_at),
                run_id,
                note,
            ),
        )


@dataclass(frozen=True, slots=True)
class Coverage:
    granularity: Granularity
    earliest: date | None  # first day (or month) we hold data for, whenever it was fetched
    latest: date | None  # last day (or month) we hold data for
    complete: int  # periods final
    provisional: int  # periods that matched, but may still be revised
    gaps: int  # periods that are final, known gaps
    pending: int  # periods incomplete or failed validation; retried on later syncs


def coverage(conn: sqlite3.Connection, account_id: str) -> list[Coverage]:
    """Per granularity: the first and last day of data held for the account, and how many sync
    periods are in each state.

    The dates describe the data in the database, not the latest sync: a sync only ever adds to
    it, so they only widen. The counts describe the sync's own bookkeeping.
    """
    validate_identifier(account_id, "account id")
    rows = conn.execute(
        "SELECT granularity,"
        " sum(status = 'complete'), sum(status = 'provisional'), sum(status = 'gap'),"
        " sum(status IN ('incomplete', 'error'))"
        " FROM sync_state WHERE account_id = ? GROUP BY granularity",
        (account_id,),
    )
    held = _data_held(conn, account_id)
    order = list(Granularity)
    result = [
        Coverage(Granularity(g), *held.get(Granularity(g), (None, None)), c, p, gaps, pending)
        for g, c, p, gaps, pending in rows
    ]
    return sorted(result, key=lambda c: order.index(c.granularity))


def _data_held(
    conn: sqlite3.Connection, account_id: str
) -> dict[Granularity, tuple[date | None, date | None]]:
    """The first and last day (month for monthly) with any stored data, per granularity."""
    held: dict[Granularity, tuple[date | None, date | None]] = {}
    monthly = conn.execute(
        "SELECT min(month), max(month) FROM usage_monthly WHERE account_id = ?", (account_id,)
    ).fetchone()
    held[Granularity.MONTHLY] = (_date_or_none(monthly[0]), _date_or_none(monthly[1]))
    daily = conn.execute(
        "SELECT min(day), max(day) FROM usage_daily WHERE account_id = ?", (account_id,)
    ).fetchone()
    held[Granularity.DAILY] = (_date_or_none(daily[0]), _date_or_none(daily[1]))
    slots = conn.execute(
        "SELECT min(interval_start_utc), max(interval_start_utc) FROM usage_half_hourly"
        " WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    held[Granularity.HALF_HOURLY] = (_uk_day(slots[0]), _uk_day(slots[1]))
    return held


def _uk_day(stamp: str | None) -> date | None:
    return None if stamp is None else london_date(parse_utc(stamp))


def _date_or_none(value: str | None) -> date | None:
    return None if value is None else date.fromisoformat(value)


def last_productive_run(conn: sqlite3.Connection) -> datetime | None:
    """When the most recent run that saved data (`ok` or `partial`) finished."""
    row = conn.execute(
        "SELECT max(finished_at_utc) FROM sync_runs WHERE status IN ('ok', 'partial')"
    ).fetchone()
    return None if row[0] is None else parse_utc(row[0])

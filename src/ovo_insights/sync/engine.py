"""The sync engine: fetch, validate and store, one period at a time.

A run:
  1. Closes any run a crash left 'running', and records a new one.
  2. Discovers every account (Bootstrap) and each account's meters, readings and supply
     end dates (one meter-reads request per account), oldest account first.
  3. Plans the periods still to fetch per account (see `plan.py`), newest data first.
  4. Fetches each period, checks it against what a complete period must contain, and writes
     the rows together with the period's `sync_state` in one transaction. A crash loses at
     most the period in flight, and the next run resumes from the ledger.

Errors: anything meaning "OVO said no" or "OVO isn't answering" ends the run at once, with
no retries beyond the client's own (see `ovo.client`). A response that fails validation
only marks that period as `error`; after MAX_PERIOD_ERRORS of those the run stops too, so a
changed API can't trigger hundreds of pointless requests.
"""

import asyncio
import logging
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from ovo_insights.domain import DailyUsage, Fuel, HalfHourUsage, SupplyPoint
from ovo_insights.ovo.client import OvoClient
from ovo_insights.ovo.convert import MeterData
from ovo_insights.ovo.errors import (
    AuthFailed,
    BlockedByUpstream,
    DisallowedHostError,
    RateLimited,
    ResponseTooLarge,
    Unauthorized,
    UpstreamSchemaChanged,
    UpstreamUnavailable,
)
from ovo_insights.storage.db import transaction
from ovo_insights.storage.repository import Repository
from ovo_insights.sync import store
from ovo_insights.sync.plan import (
    DateRange,
    Granularity,
    Period,
    PeriodStatus,
    active_ranges,
    backfill_ranges,
    classify,
    expected_keys,
    month_end,
    plan_account,
)
from ovo_insights.sync.store import INTERRUPTED, RunCounts, RunStatus, Trigger
from ovo_insights.timeutil import london_date, london_half_hours

log = logging.getLogger(__name__)

MAX_PERIOD_ERRORS = 5
# Sanity check: a day's total should match the sum of its half-hours. A larger difference is
# stored as a warning on the half-hourly day (the data is kept, not rejected).
HH_MISMATCH_RATIO = 0.10
HH_MISMATCH_MIN_KWH = 0.1

# End the run at once: continuing would only make more requests that fail.
_STOP_ERRORS: tuple[type[Exception], ...] = (
    BlockedByUpstream,
    AuthFailed,
    Unauthorized,
    RateLimited,
    UpstreamUnavailable,
    DisallowedHostError,
)
# Confined to one response: mark the period and carry on (up to MAX_PERIOD_ERRORS).
_PERIOD_ERRORS: tuple[type[Exception], ...] = (UpstreamSchemaChanged, ResponseTooLarge)


@dataclass(frozen=True, slots=True)
class AccountPlan:
    number: int  # 1 = oldest account. Account ids are never printed.
    account_id: str
    active: Mapping[Fuel, DateRange]
    backfill: Mapping[Fuel, DateRange]
    periods: tuple[Period, ...]

    @property
    def first_supply(self) -> date | None:
        return min((r.first for r in self.active.values()), default=None)

    def count(self, granularity: Granularity) -> int:
        return sum(1 for p in self.periods if p.granularity is granularity)


@dataclass(frozen=True, slots=True)
class SyncPlan:
    accounts: tuple[AccountPlan, ...]
    discovery_errors: tuple[str, ...] = ()

    @property
    def periods(self) -> int:
        return sum(len(a.periods) for a in self.accounts)


@dataclass(slots=True)
class SyncProgress:
    """Live status: printed by the CLI, and polled by the UI from M6."""

    running: bool = False
    phase: str = "idle"
    accounts_total: int = 0
    account: AccountPlan | None = None
    period: Period | None = None
    periods_done: int = 0
    periods_total: int = 0


@dataclass(frozen=True, slots=True)
class SyncResult:
    run_id: int
    status: RunStatus
    error_class: str | None
    counts: RunCounts
    plan: SyncPlan | None
    period_errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Discovered:
    account_id: str
    points: tuple[SupplyPoint, ...]
    meter: MeterData | None


# -- discovery and planning (shared with --dry-run) ---------------------------------------


async def discover(client: OvoClient) -> tuple[list[_Discovered], list[str]]:
    """Every account with its supply points (end dates from meter reads), oldest first.

    Returns the accounts and the class names of per-account errors that were tolerated (a
    meter-reads response that failed validation leaves that account with the Bootstrap
    supply points only).
    """
    summaries = await client.accounts()
    ids = [s.account_id for s in summaries]
    if len(set(ids)) != len(ids):
        raise UpstreamSchemaChanged("accounts query listed the same account twice")
    summaries.sort(key=lambda s: min((p.start_date for p in s.supply_points), default=date.max))

    found: list[_Discovered] = []
    errors: list[str] = []
    for summary in summaries:
        meter: MeterData | None = None
        try:
            meter = await client.meter_data(summary.account_id)
        except _PERIOD_ERRORS as exc:
            log.warning("Meter reads for one account failed validation: %s", exc)
            errors.append(store.error_class_name(exc))
        # Meter reads carry supply end dates, which Bootstrap doesn't; they win on a clash.
        points = {(p.fuel, p.mpxn): p for p in summary.supply_points}
        for p in meter.supply_points if meter else ():
            points[(p.fuel, p.mpxn)] = p
        found.append(_Discovered(summary.account_id, tuple(points.values()), meter))
    return found, errors


def build_plan(
    discovered: Sequence[_Discovered],
    states_for: Callable[[str], Mapping[tuple[Granularity, date], PeriodStatus]],
    today: date,
    discovery_errors: Sequence[str] = (),
) -> SyncPlan:
    active = active_ranges({d.account_id: d.points for d in discovered}, today - timedelta(1))
    accounts = []
    for number, d in enumerate(discovered, start=1):
        backfill = backfill_ranges(active[d.account_id], today)
        periods = plan_account(d.account_id, backfill, states_for(d.account_id))
        accounts.append(
            AccountPlan(number, d.account_id, active[d.account_id], backfill, tuple(periods))
        )
    return SyncPlan(tuple(accounts), tuple(discovery_errors))


async def plan_sync(
    client: OvoClient,
    conn: sqlite3.Connection | None,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SyncPlan:
    """What a sync would fetch, without writing anything (`sync --dry-run`)."""
    discovered, errors = await discover(client)
    if conn is None:
        return build_plan(discovered, lambda _: {}, london_date(now()), errors)
    return build_plan(
        discovered, lambda a: store.period_states(conn, a), london_date(now()), errors
    )


# -- the run ------------------------------------------------------------------------------


@dataclass(slots=True)
class _RunState:
    run_id: int
    counts: RunCounts = field(default_factory=RunCounts)
    period_errors: list[str] = field(default_factory=list)

    @property
    def wrote_anything(self) -> bool:
        return self.counts.periods_fetched > 0 or self.counts.readings_written > 0


class SyncEngine:
    """Runs one sync. The caller must hold the sync lock (`lock.sync_lock`)."""

    def __init__(
        self,
        client: OvoClient,
        conn: sqlite3.Connection,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        progress: SyncProgress | None = None,
        on_progress: Callable[[SyncProgress], None] | None = None,
    ) -> None:
        self._client = client
        self._conn = conn
        self._repo = Repository(conn)
        self._now = now
        self.progress = progress or SyncProgress()
        self._on_progress = on_progress

    async def run(self, trigger: Trigger) -> SyncResult:
        started = self._now()
        closed = store.close_interrupted_runs(self._conn, started)
        if closed:
            log.warning("Marked %d interrupted sync run(s) as partial", closed)
        state = _RunState(store.start_run(self._conn, trigger, started))
        first_request = self._client.request_count
        plan: SyncPlan | None = None
        status, error_class = RunStatus.OK, None
        self._report(
            running=True,
            phase="starting",
            account=None,
            period=None,
            periods_done=0,
            periods_total=0,
            accounts_total=0,
        )
        try:
            plan = await self._run(state)
        except BlockedByUpstream as exc:
            status, error_class = RunStatus.BLOCKED, store.error_class_name(exc)
        except _STOP_ERRORS + _PERIOD_ERRORS as exc:
            status = RunStatus.PARTIAL if state.wrote_anything else RunStatus.FAILED
            error_class = store.error_class_name(exc)
        except BaseException as exc:
            # A bug, Ctrl-C or cancellation: record what happened, then let it propagate.
            interrupted = isinstance(exc, KeyboardInterrupt | asyncio.CancelledError)
            status = RunStatus.PARTIAL if state.wrote_anything or interrupted else RunStatus.FAILED
            error_class = INTERRUPTED if interrupted else store.error_class_name(exc)
            self._finish(state, status, error_class, first_request)
            raise
        else:
            if state.period_errors:
                status, error_class = RunStatus.PARTIAL, state.period_errors[0]
        self._finish(state, status, error_class, first_request)
        return SyncResult(
            state.run_id, status, error_class, state.counts, plan, tuple(state.period_errors)
        )

    def _finish(
        self, state: _RunState, status: RunStatus, error_class: str | None, first_request: int
    ) -> None:
        state.counts.requests = self._client.request_count - first_request
        store.finish_run(self._conn, state.run_id, status, error_class, state.counts, self._now())
        self._report(running=False, phase=status.value, period=None)

    async def _run(self, state: _RunState) -> SyncPlan:
        self._report(phase="accounts")
        discovered, errors = await discover(self._client)
        state.period_errors.extend(errors)
        for d in discovered:  # oldest first, so the current account owns shared meters
            self._store_account(d, state)

        today = london_date(self._now())
        plan = build_plan(discovered, lambda a: store.period_states(self._conn, a), today, errors)
        self._report(
            phase="fetching", accounts_total=len(plan.accounts), periods_total=plan.periods
        )
        for account in reversed(plan.accounts):  # newest account (current data) first
            self._report(account=account)
            for period in account.periods:
                self._report(period=period)
                await self._sync_period(account, period, state)
                self.progress.periods_done += 1
        self._report(period=None)
        return plan

    def _store_account(self, d: _Discovered, state: _RunState) -> None:
        with transaction(self._conn):
            self._repo.upsert_account(d.account_id)
            self._repo.upsert_supply_points(d.points)
        if d.meter is None:
            return
        try:
            with transaction(self._conn):
                self._repo.upsert_meters(d.meter.meters)
                self._repo.upsert_registers(d.meter.registers)
                self._repo.upsert_readings(d.meter.readings)
        except sqlite3.IntegrityError:
            # e.g. a reading for a meter the response didn't list. Nothing was written.
            log.warning("Meter reads for one account were inconsistent; not stored")
            state.period_errors.append(UpstreamSchemaChanged.__name__)
            return
        state.counts.readings_written += len(d.meter.readings)

    async def _sync_period(self, account: AccountPlan, period: Period, state: _RunState) -> None:
        try:
            if period.granularity is Granularity.MONTHLY:
                await self._monthly(account, period, state)
            elif period.granularity is Granularity.DAILY:
                await self._daily(account, period, state)
            else:
                await self._half_hourly(account, period, state)
        except _PERIOD_ERRORS as exc:
            name = store.error_class_name(exc)
            log.warning(
                "%s data for %s failed validation: %s", period.granularity, period.start, exc
            )
            state.period_errors.append(name)
            store.record_period(
                self._conn,
                period,
                PeriodStatus.ERROR,
                expected=0,
                received=0,
                fetched_at=self._now(),
                run_id=state.run_id,
                note=f"{name}: {exc}",
            )
            if len(state.period_errors) >= MAX_PERIOD_ERRORS:
                raise

    async def _monthly(self, account: AccountPlan, period: Period, state: _RunState) -> None:
        fetched = await self._client.monthly(account.account_id, period.start.year)
        fetched_at = self._now()
        rows = [
            r
            for r in fetched
            if r.fuel in account.active
            and account.active[r.fuel].clip(r.month, month_end(r.month)) is not None
        ]
        keys = _unique_keys([(r.fuel, r.month) for r in rows], "monthly")
        expected = expected_keys(Granularity.MONTHLY, period.range, account.backfill)
        status = self._write(
            period,
            state,
            fetched_at,
            expected=len(expected),
            received=len(expected & keys),
            complete=expected <= keys,
            write=lambda: self._repo.upsert_monthly(rows, fetched_at=fetched_at),
        )
        state.counts.months_written += len(rows)
        _log_dropped(len(fetched) - len(rows), period, status)

    async def _daily(self, account: AccountPlan, period: Period, state: _RunState) -> None:
        fetched = await self._client.daily(
            account.account_id, period.start.year, period.start.month
        )
        fetched_at = self._now()
        rows = [r for r in fetched if r.fuel in account.active and r.day in account.active[r.fuel]]
        keys = _unique_keys([(r.fuel, r.day) for r in rows], "daily")
        expected = expected_keys(Granularity.DAILY, period.range, account.backfill)
        status = self._write(
            period,
            state,
            fetched_at,
            expected=len(expected),
            received=len(expected & keys),
            complete=expected <= keys,
            write=lambda: self._repo.upsert_daily(rows, fetched_at=fetched_at),
        )
        state.counts.days_written += len(rows)
        _log_dropped(len(fetched) - len(rows), period, status)

    async def _half_hourly(self, account: AccountPlan, period: Period, state: _RunState) -> None:
        day = period.start
        fetched = await self._client.half_hourly(account.account_id, day)
        fetched_at = self._now()
        slots = london_half_hours(day)
        slot_set = set(slots)
        if any(r.interval_start not in slot_set for r in fetched):
            raise UpstreamSchemaChanged("half-hourly response included a slot outside the UK day")
        _unique_keys([(r.fuel, r.interval_start) for r in fetched], "half-hourly")
        rows = [r for r in fetched if r.fuel in account.active and day in account.active[r.fuel]]
        by_fuel: dict[Fuel, list[HalfHourUsage]] = {}
        for r in rows:
            by_fuel.setdefault(r.fuel, []).append(r)

        # Which fuels should have half-hours that day? The daily rows say (`hasHhData`): gas
        # has none on some accounts. If OVO sent half-hours for a fuel anyway, a full day is
        # expected for it: on the real account `hasHhData` was false on a few days that had all
        # 48 slots. OVO publishes a day's half-hours before its daily row, so for a fuel with no
        # daily row yet we go by history: it is expected to have them if this account's fuel had
        # half-hourly data on an earlier day. (Gas with none, ever, isn't waited for.) If that
        # leaves nothing we can say is expected, we can't tell a whole day from an empty one, so
        # the day stays incomplete and is fetched again.
        fuels = sorted(f for f, span in account.backfill.items() if day in span)
        daily: dict[Fuel, DailyUsage | None] = {}
        for fuel in fuels:
            found = self._repo.daily(account.account_id, fuel, day, day)
            daily[fuel] = found[0] if found else None
        known = all(daily[f] is not None for f in fuels)

        def expected_to_have_half_hours(fuel: Fuel) -> bool:
            row = daily[fuel]
            if fuel in by_fuel:
                return True
            if row is not None:
                return row.has_half_hourly
            return self._repo.had_half_hourly_before(account.account_id, fuel, day)

        wanted = [f for f in fuels if expected_to_have_half_hours(f)]
        decided = known or bool(wanted)
        considered = wanted if decided else fuels
        received = {f: {r.interval_start for r in by_fuel.get(f, [])} for f in considered}
        complete = decided and all(received[f] == slot_set for f in wanted)

        notes = []
        for fuel in wanted:
            day_row = daily[fuel]
            if day_row is None or received[fuel] != slot_set:
                continue
            total = sum(r.kwh for r in by_fuel[fuel])
            difference = abs(day_row.kwh - total)
            if difference > HH_MISMATCH_MIN_KWH and difference > HH_MISMATCH_RATIO * max(
                day_row.kwh, total
            ):
                notes.append(
                    f"{fuel.value}: daily {day_row.kwh:.3f} kWh vs half-hourly {total:.3f} kWh"
                )
                log.warning(
                    "%s on %s: the daily total and the half-hourly sum differ by over %d%%",
                    fuel.value,
                    day,
                    round(HH_MISMATCH_RATIO * 100),
                )
        state.counts.warnings += len(notes)
        status = self._write(
            period,
            state,
            fetched_at,
            expected=len(slots) * len(considered),
            received=sum(len(times) for times in received.values()),
            complete=complete,
            write=lambda: self._repo.upsert_half_hourly(rows, fetched_at=fetched_at),
            note="; ".join(notes) or None,
        )
        state.counts.half_hours_written += len(rows)
        _log_dropped(len(fetched) - len(rows), period, status)

    def _write(
        self,
        period: Period,
        state: _RunState,
        fetched_at: datetime,
        *,
        expected: int,
        received: int,
        complete: bool,
        write: Callable[[], None],
        note: str | None = None,
    ) -> PeriodStatus:
        """Store a period's rows and its state in one transaction."""
        status = classify(complete=complete, last_day=period.last, today=london_date(fetched_at))
        with transaction(self._conn):
            write()
            store.record_period(
                self._conn,
                period,
                status,
                expected=expected,
                received=received,
                fetched_at=fetched_at,
                run_id=state.run_id,
                note=note,
            )
        state.counts.periods_fetched += 1
        if status is PeriodStatus.GAP:
            state.counts.gaps_found += 1
        return status

    def _report(self, **changes: object) -> None:
        for name, value in changes.items():
            setattr(self.progress, name, value)
        if self._on_progress is not None:
            self._on_progress(self.progress)


def _unique_keys[K](keys: list[K], what: str) -> set[K]:
    unique = set(keys)
    if len(unique) != len(keys):
        raise UpstreamSchemaChanged(f"{what} response repeated a period")
    return unique


def _log_dropped(dropped: int, period: Period, status: PeriodStatus) -> None:
    if dropped:
        log.info(
            "%s %s: ignored %d row(s) outside the account's supply period (%s)",
            period.granularity,
            period.start,
            dropped,
            status,
        )

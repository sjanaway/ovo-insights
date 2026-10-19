"""`ovo-insights doctor`: prove every OVO call works against a real account.

Each check reports pass/fail and a short detail. Details are counts and formats only: no
ids, MPANs, serials or amounts are ever printed.
"""

import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from ovo_insights.domain import DailyUsage, MonthlyUsage
from ovo_insights.ovo.client import HALF_HOURLY_VARIANTS, OvoClient
from ovo_insights.ovo.errors import OvoError


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


async def run_checks(client: OvoClient, today: date | None = None) -> list[CheckResult]:
    today = today or datetime.now(UTC).date()
    results: list[CheckResult] = []

    async def check(name: str, action: Callable[[], Awaitable[str]]) -> bool:
        started = time.monotonic()
        try:
            detail = await action()
        except OvoError as exc:
            results.append(CheckResult(name, ok=False, detail=f"{type(exc).__name__}: {exc}"))
            return False
        elapsed = time.monotonic() - started
        results.append(CheckResult(name, ok=True, detail=f"{detail} ({elapsed:.1f}s)"))
        return True

    async def login() -> str:
        await client.login()
        n = len(client.token_account_ids)
        return f"logged in; token lists {n} account(s)"

    if not await check("Log in and get access token", login):
        return results  # Nothing else can work, and retrying login would be impolite.

    found: list[str] = []

    async def accounts() -> str:
        summaries = await client.accounts()
        # Customers who left and came back have several accounts; check the current one
        # (latest supply start date).
        newest_first = sorted(
            summaries,
            key=lambda s: max((p.start_date for p in s.supply_points), default=date.min),
            reverse=True,
        )
        found.extend(s.account_id for s in newest_first)
        fuels = sorted({p.fuel.value for s in summaries for p in s.supply_points})
        return f"{len(summaries)} account(s); fuels: {', '.join(fuels) or 'none'}"

    if not await check("Accounts (GraphQL)", accounts) or not found:
        return results
    account = found[0]

    last_month = today.replace(day=1) - timedelta(days=1)
    probe_day = today - timedelta(days=2)

    async def monthly() -> str:
        rows = await client.monthly(account, today.year)
        return f"{len(rows)} month row(s) for {today.year}; costs present: {_any_cost(rows)}"

    async def daily() -> str:
        rows = await client.daily(account, last_month.year, last_month.month)
        hh = sum(1 for r in rows if r.has_half_hourly)
        rates = "yes" if any(r.unit_rate is not None for r in rows) else "no"
        return (
            f"{len(rows)} day row(s) for {last_month:%Y-%m}; {hh} with half-hourly data; "
            f"costs present: {_any_cost(rows)}; rates present: {rates}"
        )

    await check("Monthly usage", monthly)
    await check("Daily usage", daily)

    for variant in HALF_HOURLY_VARIANTS:

        async def half_hourly(variant: str = variant) -> str:
            rows = await client.half_hourly(account, probe_day, variant=variant)
            fuels = sorted({r.fuel.value for r in rows})
            return f"{len(rows)} slot(s) for {probe_day}; fuels: {', '.join(fuels) or 'none'}"

        await check(f"Half-hourly usage ({variant})", half_hourly)

    async def reads() -> str:
        data = await client.meter_data(account)
        skipped = (
            f", {data.skipped_registers} undated register(s) skipped"
            if data.skipped_registers
            else ""
        )
        return (
            f"{len(data.meters)} meter(s), {len(data.registers)} register(s), "
            f"{len(data.readings)} reading(s){skipped}"
        )

    await check("Meters and readings (GraphQL)", reads)
    results.append(
        CheckResult("Requests made", ok=True, detail=f"{client.request_count} HTTP request(s)")
    )
    return results


def _any_cost(rows: Sequence[DailyUsage | MonthlyUsage]) -> str:
    return "yes" if any(r.cost is not None for r in rows) else "no"

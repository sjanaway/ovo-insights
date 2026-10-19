"""Synthetic data for `ovo-insights demo`: two years of plausible dual-fuel usage.

Electricity: an overnight base load, weekday and weekend routines, a little more in winter,
and random variation. Gas: hot water all year plus winter heating. One price rise part-way
through. A few deliberate gaps (whole days missing, and days short of two half-hours), and
the real DST days, so every banner and edge case has something to show.

Deterministic for a given seed and date. Never touches the real data dir or the Keychain;
the identifiers are fake (the MPAN fails its check digit).
"""

import math
import random
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from ovo_insights.analysis.half_hourly import local_slot
from ovo_insights.analysis.usage import round_half_even
from ovo_insights.domain import (
    GAS_REGISTER_ID,
    DailyUsage,
    Fuel,
    HalfHourUsage,
    Meter,
    MonthlyUsage,
    Reading,
    Register,
    SupplyPoint,
)
from ovo_insights.money import MilliPence
from ovo_insights.storage import tariffs as tariff_store
from ovo_insights.storage.db import transaction
from ovo_insights.storage.repository import Repository
from ovo_insights.sync import store
from ovo_insights.sync.plan import (
    Granularity,
    Period,
    PeriodStatus,
    classify,
    history_start,
)
from ovo_insights.sync.store import RunCounts, RunStatus, Trigger
from ovo_insights.tariffs import ALL_WEEKDAYS, Band, Tariff
from ovo_insights.timeutil import london_half_hours

ACCOUNT = "demo1"
MPAN = "1900000000000"
MPRN = "9876543"

BASE_LOAD_KWH = 0.11
ELECTRIC_METER = "DEMOELEC01"
GAS_METER = "DEMOGAS01"
OLD_ELECTRIC_METER = "DEMOELEC00"
OLD_GAS_METER = "DEMOGAS00"
# Synthetic calorific values (MJ/m3), inside the 37.5-43.0 range OVO's bill gives.
DEMO_CALORIFIC_VALUE = 39.5  # per half-hour: 220 W always on
# (unit rate per kWh, standing charge per day) in milli-pence, before and after the rise.
RATES = {
    Fuel.ELECTRICITY: ((24_500, 53_680), (27_030, 60_120)),
    Fuel.GAS: ((6_240, 31_430), (6_890, 33_160)),
}
WHOLE_DAYS_MISSING = 3
SHORT_DAYS = 3


@dataclass(frozen=True, slots=True)
class DemoData:
    first: date
    last: date
    supply_points: tuple[SupplyPoint, ...]
    half_hours: tuple[HalfHourUsage, ...]
    daily: tuple[DailyUsage, ...]
    monthly: tuple[MonthlyUsage, ...]
    missing_days: frozenset[date]
    short_days: frozenset[date]
    meters: tuple[Meter, ...] = ()
    registers: tuple[Register, ...] = ()
    readings: tuple[Reading, ...] = ()
    tariffs: tuple[Tariff, ...] = ()


def build(seed: int, today: date) -> DemoData:
    rng = random.Random(seed)  # noqa: S311 - synthetic data, not security
    first, last = history_start(today), today - timedelta(days=1)
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    rise = first + timedelta(days=len(days) * 3 // 5)
    candidates = days[5:-10]  # keep the newest days whole, so "last 30 days" looks normal
    picked = rng.sample(candidates, WHOLE_DAYS_MISSING + SHORT_DAYS)
    missing, short = frozenset(picked[:WHOLE_DAYS_MISSING]), frozenset(picked[WHOLE_DAYS_MISSING:])

    half_hours: list[HalfHourUsage] = []
    daily: list[DailyUsage] = []
    for day in days:
        if day in missing:
            continue
        slots = london_half_hours(day)
        values = [_electricity(rng, day, local_slot(start)[1]) for start in slots]
        kept = list(zip(slots, values, strict=True))
        if day in short:
            kept = kept[:-2]  # OVO sometimes loses a reading or two
        half_hours.extend(HalfHourUsage(ACCOUNT, Fuel.ELECTRICITY, s, v) for s, v in kept)
        daily.append(_day(Fuel.ELECTRICITY, day, round(sum(values), 3), day >= rise, True))
        daily.append(_day(Fuel.GAS, day, _gas(rng, day), day >= rise, False))

    return DemoData(
        first,
        last,
        (
            SupplyPoint(ACCOUNT, Fuel.ELECTRICITY, MPAN, first - timedelta(days=400)),
            SupplyPoint(ACCOUNT, Fuel.GAS, MPRN, first - timedelta(days=400)),
        ),
        tuple(half_hours),
        tuple(daily),
        tuple(_monthly(daily)),
        missing,
        short,
        *_meter_data(seed, first, today, daily, missing),
        _tariffs(rise),
    )


def write(conn: sqlite3.Connection, data: DemoData, now: datetime | None = None) -> None:
    """Store the demo data and a matching sync history, as a real sync would."""
    now = now or datetime.now(UTC)
    today = data.last + timedelta(days=1)
    repo = Repository(conn)
    run_id = store.start_run(conn, Trigger.CLI, now)
    with transaction(conn):
        repo.upsert_account(ACCOUNT)
        repo.upsert_supply_points(data.supply_points)
        repo.upsert_half_hourly(data.half_hours, fetched_at=now)
        repo.upsert_daily(data.daily, fetched_at=now)
        repo.upsert_monthly(data.monthly, fetched_at=now)
        repo.upsert_meters(data.meters)
        repo.upsert_registers(data.registers)
        repo.upsert_readings(data.readings)
        for tariff in data.tariffs:
            tariff_store.add(conn, tariff, now)
        for day in (
            data.first + timedelta(days=i) for i in range((data.last - data.first).days + 1)
        ):
            complete = day not in data.missing_days and day not in data.short_days
            _record(conn, run_id, Granularity.HALF_HOURLY, day, complete, today, now)
        months = sorted({d.day.replace(day=1) for d in data.daily})
        for month in months:
            _record(conn, run_id, Granularity.DAILY, month, True, today, now)
        for year in sorted({m.year for m in months}):
            _record(conn, run_id, Granularity.MONTHLY, date(year, 1, 1), True, today, now)
    counts = RunCounts(
        periods_fetched=len(data.daily) // 2,
        gaps_found=len(data.missing_days) + len(data.short_days),
        half_hours_written=len(data.half_hours),
        days_written=len(data.daily),
        months_written=len(data.monthly),
    )
    store.finish_run(conn, run_id, RunStatus.OK, None, counts, now)


# -- tariffs ------------------------------------------------------------------------------


def _tariffs(rise: date) -> tuple[Tariff, ...]:
    """Sample tariffs: the demo's own rates (so they match what was "charged" to the penny),
    and three alternatives. All made up."""
    before, after = RATES[Fuel.ELECTRICITY]
    gas_before, gas_after = RATES[Fuel.GAS]
    day = rise - timedelta(days=1)

    def versions(name: str, fuel: Fuel, old: tuple[int, int], new: tuple[int, int]) -> list[Tariff]:
        return [
            Tariff.flat(name, fuel, MilliPence(old[0]), MilliPence(old[1]), None, day),
            Tariff.flat(name, fuel, MilliPence(new[0]), MilliPence(new[1]), rise, None),
        ]

    everyday = ALL_WEEKDAYS
    cheap, dear = MilliPence(11_500), MilliPence(31_000)
    economy7 = Tariff(
        "Economy 7 (example)",
        Fuel.ELECTRICITY,
        MilliPence(58_000),
        (Band(1, 15, cheap, everyday), Band(15, 48, dear, everyday), Band(0, 1, dear, everyday)),
    )
    weekdays, weekend = frozenset(range(5)), frozenset({5, 6})
    ev = Tariff(
        "EV overnight (example)",
        Fuel.ELECTRICITY,
        MilliPence(54_000),
        (
            Band(0, 10, MilliPence(8_000), weekdays),
            Band(10, 32, MilliPence(29_500), weekdays),
            Band(32, 38, MilliPence(41_000), weekdays),
            Band(38, 48, MilliPence(29_500), weekdays),
            Band(0, 48, MilliPence(26_000), weekend),
        ),
    )
    return (
        *versions("OVO standard", Fuel.ELECTRICITY, before, after),
        economy7,
        ev,
        *versions("OVO standard", Fuel.GAS, gas_before, gas_after),
        Tariff.flat("Cheaper gas (example)", Fuel.GAS, MilliPence(5_400), MilliPence(30_000)),
    )


# -- meters and readings ------------------------------------------------------------------


def _meter_data(
    seed: int, first: date, today: date, daily: list[DailyUsage], missing: frozenset[date]
) -> tuple[tuple[Meter, ...], tuple[Register, ...], tuple[Reading, ...]]:
    """Two current meters with readings that agree with the smart data (so the comparison
    page has matches), two removed meters from before it, and a few deliberate faults: a bad
    estimated electricity reading, a bad estimated gas reading, and the days missing from the
    smart data (their consumption still went through the meters)."""
    rng = random.Random(f"readings-{seed}")  # noqa: S311 - synthetic data, not security
    kwh = {(r.fuel, r.day): r.kwh for r in daily}
    lost = {
        fuel: {
            d: round(rng.uniform(6, 14) if fuel is Fuel.ELECTRICITY else rng.uniform(8, 40), 3)
            for d in sorted(missing)
        }
        for fuel in Fuel
    }
    since = first - timedelta(days=400)
    meters = (
        Meter(ELECTRIC_METER, ACCOUNT, Fuel.ELECTRICITY, "DEMO", "Active"),
        Meter(GAS_METER, ACCOUNT, Fuel.GAS, "DEMO", "Active"),
        Meter(OLD_ELECTRIC_METER, ACCOUNT, Fuel.ELECTRICITY, "DEMO", "Removed"),
        Meter(OLD_GAS_METER, ACCOUNT, Fuel.GAS, "DEMO", "Removed"),
    )
    registers = (
        Register(ELECTRIC_METER, "1", since, None, "ANYTIME", "KWH", 5),
        Register(GAS_METER, GAS_REGISTER_ID, since, None, None, "SCMH", 5),
        Register(
            OLD_ELECTRIC_METER, "01", since - timedelta(days=2000), since, "ANYTIME", "KWH", 5
        ),
        Register(
            OLD_GAS_METER, GAS_REGISTER_ID, since - timedelta(days=2000), since, None, "SCFH", 4
        ),
    )

    readings: list[Reading] = []
    total = Decimal("41230.500")
    days = [first + timedelta(days=i) for i in range((today - first).days + 1)]
    # Faults go where the smart data is whole, so each is reported as exactly what it is.
    bad_electricity = next(d for d in days[400:] if not missing & {d - timedelta(days=1), d})
    for day in days:
        value = total
        if day == bad_electricity:  # an estimated read 25 kWh too high; the next one is right
            readings.append(
                Reading(
                    ELECTRIC_METER, "1", day, value + 25, "MANUAL_READ_AGENT", "INLIFE", "ESTIMATE"
                )
            )
        else:
            readings.append(
                Reading(ELECTRIC_METER, "1", day, value, "SMART_METER", "INLIFE", "ACTUAL")
            )
        used = kwh.get((Fuel.ELECTRICITY, day), lost[Fuel.ELECTRICITY].get(day, 0.0))
        total += Decimal(str(used))

    # Gas: one read on the first of each month (and on the first and last day), in m3.
    volume = Decimal("1200.000")
    gas_days = {first, today, *(d for d in days if d.day == 1)}
    read_days = sorted(gas_days)
    december = next(
        (
            d
            for before, d, after in zip(read_days[2:], read_days[3:], read_days[4:], strict=False)
            if d.month in (11, 12, 1, 2) and not any(before <= m < after for m in missing)
        ),
        None,
    )
    cv = Decimal(str(DEMO_CALORIFIC_VALUE))
    totals: dict[date, Decimal] = {}
    for day in days:
        if day in gas_days:
            totals[day] = volume
        used = kwh.get((Fuel.GAS, day), lost[Fuel.GAS].get(day, 0.0))
        daily_cv = cv * Decimal(str(round(rng.uniform(0.96, 1.04), 3)))
        volume += (Decimal(str(used)) * Decimal("3.6") / (daily_cv * Decimal("1.02264"))).quantize(
            Decimal("0.001")
        )
    for day in read_days:
        bad = day == december  # an estimated read 15 m3 too high
        readings.append(
            Reading(
                GAS_METER,
                GAS_REGISTER_ID,
                day,
                totals[day] + (15 if bad else 0),
                "INDUSTRY" if bad else "SMART_METER",
                "INLIFE",
                "ESTIMATE" if bad else "ACTUAL",
            )
        )

    # Before smart data: quarterly manual reads off the meters that were later removed.
    for i, base in enumerate(("20110.000", "20640.500", "21102.250", "21688.000")):
        day = since - timedelta(days=900 - 200 * i)
        readings.append(
            Reading(
                OLD_ELECTRIC_METER,
                "01",
                day,
                Decimal(base),
                "MANUAL_READ_CUSTOMER",
                "INLIFE",
                "ACTUAL",
            )
        )
    for i, base in enumerate(("5521.00", "5630.00", "5702.00")):
        day = since - timedelta(days=700 - 200 * i)
        readings.append(
            Reading(
                OLD_GAS_METER,
                GAS_REGISTER_ID,
                day,
                Decimal(base),
                "MANUAL_READ_CUSTOMER",
                "INLIFE",
                "ACTUAL",
            )
        )
    return meters, registers, tuple(readings)


# -- shapes -------------------------------------------------------------------------------


def _winter(day: date) -> float:
    """1 in mid-January, 0 in mid-July."""
    return (1 + math.cos(2 * math.pi * (day.timetuple().tm_yday - 15) / 365.25)) / 2


def _electricity(rng: random.Random, day: date, slot: int) -> float:
    hour = slot / 2
    if day.weekday() < 5:
        routine = (
            0.28
            if 6.5 <= hour < 8
            else 0.07
            if 8 <= hour < 17
            else 0.36
            if 17 <= hour < 22
            else 0.0
        )
    else:
        routine = (
            0.32
            if 8 <= hour < 11
            else 0.16
            if 11 <= hour < 17
            else 0.30
            if 17 <= hour < 22.5
            else 0.0
        )
    seasonal = 1 + 0.3 * _winter(day)
    value = BASE_LOAD_KWH * rng.uniform(0.92, 1.12) + routine * seasonal * rng.uniform(0.7, 1.3)
    return round(value, 3)


def _gas(rng: random.Random, day: date) -> float:
    hot_water = 7.5 * rng.uniform(0.8, 1.2)
    heating = 52 * math.pow(_winter(day), 1.6) * rng.uniform(0.75, 1.25)
    return round(hot_water + heating, 3)


def _day(fuel: Fuel, day: date, kwh: float, risen: bool, has_hh: bool) -> DailyUsage:
    rate, standing = RATES[fuel][1 if risen else 0]
    # As OVO does: cost is kWh x unit rate, rounded to the penny; standing charge separate.
    cost = round_half_even(Decimal(str(kwh)) * rate / 1000) * 1000
    return DailyUsage(
        ACCOUNT,
        fuel,
        day,
        kwh,
        MilliPence(cost),
        has_hh,
        unit_rate=MilliPence(rate),
        standing_charge=MilliPence(standing),
    )


def _monthly(daily: list[DailyUsage]) -> list[MonthlyUsage]:
    totals: dict[tuple[Fuel, date], list[DailyUsage]] = defaultdict(list)
    for row in daily:
        totals[(row.fuel, row.day.replace(day=1))].append(row)
    return [
        MonthlyUsage(
            ACCOUNT,
            fuel,
            month,
            round(sum(r.kwh for r in rows), 3),
            MilliPence(sum(r.cost or 0 for r in rows)),
            MPAN if fuel is Fuel.ELECTRICITY else MPRN,
        )
        for (fuel, month), rows in sorted(totals.items())
    ]


def _record(
    conn: sqlite3.Connection,
    run_id: int,
    granularity: Granularity,
    start: date,
    complete: bool,
    today: date,
    now: datetime,
) -> None:
    period = Period(ACCOUNT, granularity, start)
    # Demo gaps are deliberate, so they are final straight away.
    status = (
        classify(complete=True, last_day=period.last, today=today) if complete else PeriodStatus.GAP
    )
    expected = len(london_half_hours(start)) if granularity is Granularity.HALF_HOURLY else 1
    store.record_period(
        conn,
        period,
        status,
        expected=expected,
        received=expected if complete else max(0, expected - 2),
        fetched_at=now,
        run_id=run_id,
    )

"""View-building for the Readings page: the parts a page test doesn't reach."""

import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from ovo_insights.analysis.completeness import DayCell, DayState
from ovo_insights.analysis.readings import Verdict
from ovo_insights.domain import (
    GAS_REGISTER_ID,
    DailyUsage,
    Fuel,
    Meter,
    Reading,
    Register,
)
from ovo_insights.storage.repository import Repository
from ovo_insights.sync.plan import DateRange, PeriodStatus
from ovo_insights.web import readings

DAY = date(2026, 6, 10)
TODAY = date(2026, 10, 13)
SPAN = DateRange(date(2026, 6, 1), date(2026, 6, 30))


def test_choices_label_registers_and_number_meters_within_each_fuel(repo: Repository) -> None:
    repo.upsert_account("acc1")
    repo.upsert_meters(
        [
            Meter("ELECDUAL1", "acc1", Fuel.ELECTRICITY, status="Active"),
            Meter("ELECOLD01", "acc1", Fuel.ELECTRICITY, status="Removed"),
            Meter("GASONLY01", "acc1", Fuel.GAS, status=None),
        ]
    )
    repo.upsert_registers(
        [
            Register("ELECDUAL1", "1", date(2020, 1, 1), None, "DAY", "KWH", 5),
            Register("ELECDUAL1", "2", date(2020, 1, 1), None, "NIGHT", "KWH", 5),
        ]
    )
    repo.upsert_readings(
        [
            Reading("ELECDUAL1", "1", DAY, Decimal(1)),
            Reading("ELECDUAL1", "2", DAY, Decimal(1)),
            Reading("ELECOLD01", "1", DAY - timedelta(days=900), Decimal(1)),
            Reading("GASONLY01", GAS_REGISTER_ID, DAY, Decimal(1)),
        ]
    )
    labels = [c.label for c in readings.choices(repo._conn, SPAN)]
    assert labels == [
        "Electricity meter 1, register 1 (active, KWH)",
        "Electricity meter 1, register 2 (active, KWH)",
        "Electricity meter 2, register 1 (removed, unit unknown)",
        "Gas meter 1 (status unknown, unit unknown)",
    ]
    assert [c.in_range for c in readings.choices(repo._conn, SPAN)] == [1, 1, 0, 1]
    assert [c.number for c in readings.choices(repo._conn, SPAN)] == [1, 2, 3, 4]
    assert [c.meter for c in readings.choices(repo._conn, SPAN)] == [
        "Electricity meter 1",
        "Electricity meter 1",
        "Electricity meter 2",
        "Gas meter 1",
    ]


def test_comparison_without_two_readings_in_range_has_no_windows(
    repo: Repository, conn: sqlite3.Connection
) -> None:
    repo.upsert_account("acc1")
    repo.upsert_meters([Meter("ELECONE01", "acc1", Fuel.ELECTRICITY, status="Active")])
    repo.upsert_registers([Register("ELECONE01", "1", date(2020, 1, 1), None, "ANYTIME", "KWH", 5)])
    repo.upsert_readings([Reading("ELECONE01", "1", DAY, Decimal(5))])
    [choice] = readings.choices(conn, SPAN)
    for span in (SPAN, DateRange(date(2020, 1, 1), date(2020, 1, 31))):
        found = readings.comparison(conn, choice.series, span)
        assert found.windows == []
        assert (found.summary.total, found.summary.compared) == (0, 0)
        assert not found.gas


def test_comparison_fetches_smart_data_from_before_the_range(
    repo: Repository, conn: sqlite3.Connection
) -> None:
    """A window ending inside the range can start before it; its smart total must still be
    the whole window's, not just the part inside the range."""
    repo.upsert_account("acc1")
    repo.upsert_meters([Meter("ELECTWO01", "acc1", Fuel.ELECTRICITY, status="Active")])
    repo.upsert_registers([Register("ELECTWO01", "1", date(2020, 1, 1), None, "ANYTIME", "KWH", 5)])
    repo.upsert_readings(
        [
            Reading("ELECTWO01", "1", date(2026, 5, 30), Decimal("100")),
            Reading("ELECTWO01", "1", date(2026, 6, 2), Decimal("130")),
        ]
    )
    repo.upsert_daily(
        [
            DailyUsage(
                "acc1", Fuel.ELECTRICITY, date(2026, 5, 30) + timedelta(days=i), 10.0, None, False
            )
            for i in range(3)
        ],
        fetched_at=datetime(2026, 6, 3, tzinfo=UTC),
    )
    [choice] = readings.choices(conn, SPAN)
    [window] = readings.comparison(conn, choice.series, SPAN).windows
    assert (window.start, window.end) == (date(2026, 5, 30), date(2026, 6, 2))
    assert window.smart_kwh == Decimal(30)
    assert window.verdict is Verdict.MATCH


def test_calendars_skip_a_fuel_with_no_data(repo: Repository, conn: sqlite3.Connection) -> None:
    repo.upsert_account("acc1")
    repo.upsert_daily(
        [DailyUsage("acc1", Fuel.GAS, DAY, 5.0, None, False)],
        fetched_at=datetime(2026, 6, 11, tzinfo=UTC),
    )
    found = readings.calendars(conn, SPAN, TODAY)
    assert [c.fuel for c in found] == [Fuel.GAS]
    assert found[0].offset == DAY.weekday()
    assert found[0].clipped  # the range began on 1 June; the data on the 10th
    assert found[0].cells[0].css == "complete"
    assert found[0].cells[0].label == "10 Jun 2026: complete"


def test_cell_descriptions_for_every_state() -> None:
    def say(state: DayState, expected: int, received: int, status: PeriodStatus | None) -> str:
        return readings._describe(DayCell(DAY, state, expected, received, status))

    assert say(DayState.COMPLETE, 48, 48, None) == "10 Jun 2026: complete"
    assert say(DayState.NOT_YET, 0, 0, None) == "10 Jun 2026: not yet available"
    assert say(DayState.NO_SUPPLY, 0, 0, None) == "10 Jun 2026: no supply"
    assert say(DayState.MISSING, 0, 0, None) == "10 Jun 2026: missing"
    assert say(DayState.MISSING, 48, 0, None) == "10 Jun 2026: missing (0 of 48 half-hours)"
    assert say(DayState.MISSING, 48, 12, None).endswith("(12 of 48 half-hours)")
    assert say(DayState.PARTIAL, 48, 46, None) == "10 Jun 2026: partial (46 of 48 half-hours)"
    assert say(DayState.PARTIAL, 48, 46, PeriodStatus.GAP).endswith(
        "(46 of 48 half-hours), known gap: OVO doesn't have the rest"
    )

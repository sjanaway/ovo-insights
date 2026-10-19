"""Tariff rules: coverage of every slot of every weekday, field-level errors, versions."""

from collections.abc import Callable
from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ovo_insights.domain import Fuel
from ovo_insights.money import MilliPence
from ovo_insights.tariffs import (
    ALL_WEEKDAYS,
    Band,
    BandDraft,
    FieldError,
    Tariff,
    TariffDraft,
    TariffError,
    build_tariff,
    check_versions,
    slot_label,
)

EVERY_DAY = tuple(range(7))


def draft(*bands: BandDraft, **overrides: str) -> TariffDraft:
    fields = {
        "name": "Test",
        "fuel": "electricity",
        "standing": "50",
        "effective_from": "",
        "effective_to": "",
    }
    return TariffDraft(bands=bands, **{**fields, **overrides})


def band(start: str, end: str, rate: str, days: tuple[int, ...] = EVERY_DAY) -> BandDraft:
    return BandDraft(start, end, rate, days)


def errors_of(d: TariffDraft) -> list[tuple[str, str]]:
    with pytest.raises(TariffError) as caught:
        build_tariff(d)
    return [(e.field, e.message) for e in caught.value.errors]


def fields_of(d: TariffDraft) -> set[str]:
    return {field for field, _ in errors_of(d)}


# -- building -----------------------------------------------------------------------------


def test_a_whole_day_band_builds_a_flat_tariff_with_exact_milli_pence() -> None:
    tariff = build_tariff(draft(band("00:00", "00:00", "24.44"), standing="53.68"))
    assert tariff.is_flat
    assert tariff.standing_charge == MilliPence(53_680)
    assert tariff.rate_at(0, 0) == MilliPence(24_440)
    assert tariff.rate_at(6, 47) == MilliPence(24_440)
    assert tariff.bands == (Band(0, 48, MilliPence(24_440), ALL_WEEKDAYS),)


def test_a_band_that_ends_before_it_starts_wraps_past_midnight_into_two() -> None:
    tariff = build_tariff(
        draft(band("00:30", "07:30", "10"), band("07:30", "00:30", "30"), name="Economy 7")
    )
    assert not tariff.is_flat
    assert [(b.start_slot, b.end_slot) for b in tariff.bands] == [(1, 15), (15, 48), (0, 1)]
    assert tariff.rate_at(2, 0) == MilliPence(30_000)  # 00:00 is the end of the day band
    assert tariff.rate_at(2, 1) == MilliPence(10_000)  # 00:30
    assert tariff.rate_at(2, 14) == MilliPence(10_000)  # 07:00
    assert tariff.rate_at(2, 15) == MilliPence(30_000)  # 07:30


def test_weekday_bands_and_a_weekend_band() -> None:
    tariff = build_tariff(
        draft(
            band("00:00", "05:00", "7", (0, 1, 2, 3, 4)),
            band("05:00", "00:00", "28", (0, 1, 2, 3, 4)),
            band("00:00", "00:00", "15", (5, 6)),
        )
    )
    assert tariff.rate_at(0, 9) == MilliPence(7_000)
    assert tariff.rate_at(4, 10) == MilliPence(28_000)
    assert tariff.rate_at(5, 9) == MilliPence(15_000)
    assert tariff.rate_at(6, 47) == MilliPence(15_000)


def test_a_wrapping_band_belongs_to_the_day_each_part_falls_on() -> None:
    # "Monday 23:30 to 05:30" is Monday's last slot and Monday's first 11: nothing is carried
    # into Tuesday, so Tuesday's early morning (not covered) is a gap.
    errors = errors_of(
        draft(band("05:30", "23:30", "30"), band("23:30", "05:30", "10", (0,)), standing="0")
    )
    messages = {m for f, m in errors if f == "bands"}
    assert "Tuesday has no rate for 00:00-05:30, 23:30-24:00." in messages
    assert not any(m.startswith("Monday") for m in messages)


def test_names_are_trimmed_and_fuel_and_dates_parse() -> None:
    tariff = build_tariff(
        draft(
            band("00:00", "00:00", "5.5"),
            name="  Gas 2026 ",
            fuel="GAS",
            effective_from="2026-04-01",
            effective_to="2026-09-30",
        ),
        tariff_id=7,
    )
    assert (tariff.name, tariff.fuel, tariff.tariff_id) == ("Gas 2026", Fuel.GAS, 7)
    assert (tariff.effective_from, tariff.effective_to) == (date(2026, 4, 1), date(2026, 9, 30))
    assert tariff.applies_on(date(2026, 4, 1))
    assert tariff.applies_on(date(2026, 9, 30))
    assert not tariff.applies_on(date(2026, 3, 31))
    assert not tariff.applies_on(date(2026, 10, 1))


def test_open_ended_dates_apply_either_side() -> None:
    always = Tariff.flat("A", Fuel.GAS, MilliPence(1), MilliPence(0))
    assert always.applies_on(date.min)
    assert always.applies_on(date.max)
    from_only = Tariff.flat("A", Fuel.GAS, MilliPence(1), MilliPence(0), date(2026, 1, 1))
    assert not from_only.applies_on(date(2025, 12, 31))
    assert from_only.applies_on(date(2099, 1, 1))
    to_only = Tariff.flat("A", Fuel.GAS, MilliPence(1), MilliPence(0), None, date(2026, 1, 1))
    assert to_only.applies_on(date(1999, 1, 1))
    assert not to_only.applies_on(date(2026, 1, 2))


# -- field-level errors -------------------------------------------------------------------


def test_every_problem_is_reported_at_once_with_its_field() -> None:
    found = fields_of(
        draft(
            band("7:30", "08:00", "abc"),
            band("00:15", "00:00", "600", ()),
            name="",
            fuel="steam",
            standing="-1",
            effective_from="01/04/2026",
            effective_to="2026-02-30",
        )
    )
    assert found >= {
        "name",
        "fuel",
        "standing",
        "effective_from",
        "effective_to",
        "bands[0].start",
        "bands[0].rate",
        "bands[1].start",
        "bands[1].rate",
        "bands[1].weekdays",
    }


@pytest.mark.parametrize(
    ("rate", "ok"),
    [("0", True), ("500", True), ("500.0005", True), ("500.0006", False), ("-0.1", False)],
)
def test_unit_rate_limits(rate: str, ok: bool) -> None:
    d = draft(band("00:00", "00:00", rate))
    if ok:
        assert build_tariff(d)
    else:
        assert "bands[0].rate" in fields_of(d)


@pytest.mark.parametrize(("standing", "ok"), [("0", True), ("1000", True), ("1000.1", False)])
def test_standing_charge_limits(standing: str, ok: bool) -> None:
    d = draft(band("00:00", "00:00", "10"), standing=standing)
    if ok:
        assert build_tariff(d)
    else:
        assert fields_of(d) == {"standing"}


@pytest.mark.parametrize(
    "text", ["", "abc", "1e2", "£10", "10p", "1,000", "NaN", "inf", "٣", "10 20", "1" * 50]
)
def test_rates_must_be_plain_decimals(text: str) -> None:
    assert "bands[0].rate" in fields_of(draft(band("00:00", "00:00", text)))
    assert "standing" in fields_of(draft(band("00:00", "00:00", "5"), standing=text))


@pytest.mark.parametrize(
    "text", ["", "7", "07:15", "24:00", "7:30", "07:30:00", "ab:cd", "\u0660\u0667:\u0663\u0660"]
)
def test_band_times_must_be_on_the_hour_or_half_hour(text: str) -> None:
    found = fields_of(draft(band(text, "08:00", "5")))
    assert "bands[0].start" in found


def test_an_empty_band_is_refused_and_the_whole_day_is_00_to_00() -> None:
    assert errors_of(draft(band("06:00", "06:00", "5"))) == [
        ("bands[0].end", "A band can't be empty; use 00:00 to 00:00 for a whole day.")
    ]
    assert build_tariff(draft(band("00:00", "00:00", "5"))).is_flat


def test_no_bands_and_bad_weekdays() -> None:
    assert errors_of(draft()) == [("bands", "Add at least one band.")]
    assert "bands[0].weekdays" in fields_of(draft(band("00:00", "00:00", "5", (7,))))
    assert "bands[0].weekdays" in fields_of(draft(band("00:00", "00:00", "5", (-1, 0))))


def test_gaps_and_overlaps_name_the_day_and_times() -> None:
    errors = errors_of(draft(band("00:00", "12:00", "5"), band("11:00", "23:00", "6")))
    messages = {m for _, m in errors}
    assert "Monday has no rate for 23:00-24:00." in messages
    assert "Monday has two rates for 11:00-12:00." in messages
    assert "Sunday has no rate for 23:00-24:00." in messages
    assert len(messages) == 14  # one of each, for every weekday


def test_a_weekday_with_no_band_at_all_is_a_gap() -> None:
    errors = errors_of(draft(band("00:00", "00:00", "5", (0, 1, 2, 3, 4))))
    assert {m for _, m in errors} == {
        "Saturday has no rate for 00:00-24:00.",
        "Sunday has no rate for 00:00-24:00.",
    }


def test_many_gaps_are_summarised() -> None:
    bands = [band(f"{h:02d}:00", f"{h:02d}:30", "5") for h in range(0, 16, 2)]
    messages = {m for _, m in errors_of(draft(*bands))}
    assert any("and 5 more" in m or ("and" in m and "more" in m) for m in messages)


def test_a_gas_tariff_has_one_unit_rate() -> None:
    d = draft(band("00:00", "12:00", "5"), band("12:00", "00:00", "6"), fuel="gas")
    assert errors_of(d) == [("bands", "A gas tariff has a single unit rate.")]
    split = draft(band("00:00", "12:00", "5"), band("12:00", "00:00", "5"), fuel="gas")
    assert build_tariff(split).is_flat


def test_end_date_before_start_date() -> None:
    d = draft(band("00:00", "00:00", "5"), effective_from="2026-05-01", effective_to="2026-04-30")
    assert errors_of(d) == [("effective_to", "The end date is before the start date.")]


def test_name_rules() -> None:
    assert "name" in fields_of(draft(band("00:00", "00:00", "5"), name="x" * 65))
    assert "name" in fields_of(draft(band("00:00", "00:00", "5"), name="bad\nname"))
    assert build_tariff(draft(band("00:00", "00:00", "5"), name="x" * 64))


# -- the constructors re-check everything -------------------------------------------------


def test_constructing_directly_enforces_the_same_rules() -> None:
    whole = Band(0, 48, MilliPence(5), ALL_WEEKDAYS)
    bad_bands: list[Callable[[], object]] = [
        lambda: Band(5, 5, MilliPence(1), ALL_WEEKDAYS),
        lambda: Band(-1, 5, MilliPence(1), ALL_WEEKDAYS),
        lambda: Band(0, 49, MilliPence(1), ALL_WEEKDAYS),
        lambda: Band(0, 48, MilliPence(500_001), ALL_WEEKDAYS),
        lambda: Band(0, 48, MilliPence(-1), ALL_WEEKDAYS),
        lambda: Band(0, 48, MilliPence(1), frozenset()),
        lambda: Band(0, 48, MilliPence(1), frozenset({7})),
        lambda: Band(0, 48, MilliPence(1), frozenset({True})),
        lambda: Band(0, 48, 1.5, ALL_WEEKDAYS),  # type: ignore[arg-type]
        lambda: Band(True, 48, MilliPence(1), ALL_WEEKDAYS),
    ]
    for bad_band in bad_bands:
        with pytest.raises(TariffError):
            bad_band()
    bad_tariffs: list[Callable[[], object]] = [
        lambda: Tariff("", Fuel.GAS, MilliPence(0), (whole,)),
        lambda: Tariff("n", "gas", MilliPence(0), (whole,)),  # type: ignore[arg-type]
        lambda: Tariff("n", Fuel.GAS, MilliPence(-1), (whole,)),
        lambda: Tariff("n", Fuel.GAS, MilliPence(1_000_001), (whole,)),
        lambda: Tariff("n", Fuel.GAS, 1.5, (whole,)),  # type: ignore[arg-type]
        lambda: Tariff("n", Fuel.GAS, MilliPence(0), ()),
        lambda: Tariff("n", Fuel.GAS, MilliPence(0), [whole]),  # type: ignore[arg-type]
        lambda: Tariff("n", Fuel.GAS, MilliPence(0), ("x",)),  # type: ignore[arg-type]
        lambda: Tariff("n", Fuel.GAS, MilliPence(0), (whole, whole)),
        lambda: Tariff("n", Fuel.GAS, MilliPence(0), (whole,), "2026-01-01"),  # type: ignore[arg-type]
        lambda: Tariff("n", Fuel.GAS, MilliPence(0), (whole,), date(2026, 2, 1), date(2026, 1, 1)),
    ]
    for bad_tariff in bad_tariffs:
        with pytest.raises(TariffError):
            bad_tariff()


def test_tariff_error_message_lists_the_fields() -> None:
    with pytest.raises(TariffError, match="bands: Add at least one band"):
        Tariff("n", Fuel.GAS, MilliPence(0), ())


@given(st.integers(min_value=0, max_value=47), st.integers(min_value=1, max_value=48))
def test_any_split_of_the_day_into_two_bands_is_a_valid_tariff(a: int, b: int) -> None:
    start, end = sorted((a, b))
    if start == end or start == 0 or end == 48:
        return
    day = Band(start, end, MilliPence(10_000), ALL_WEEKDAYS)
    before = Band(0, start, MilliPence(20_000), ALL_WEEKDAYS)
    after = Band(end, 48, MilliPence(30_000), ALL_WEEKDAYS)
    tariff = Tariff("t", Fuel.ELECTRICITY, MilliPence(0), (before, day, after))
    assert [tariff.rate_at(3, s) for s in (0, start - 1, start, end - 1, end, 47)] == [
        20_000,
        20_000,
        10_000,
        10_000,
        30_000,
        30_000,
    ]


def test_slot_labels() -> None:
    assert [slot_label(s) for s in (0, 1, 47, 48)] == ["00:00", "00:30", "23:30", "24:00"]


# -- versions of one tariff ---------------------------------------------------------------


def flat(name: str, start: date | None, end: date | None, fuel: Fuel = Fuel.GAS) -> Tariff:
    return Tariff.flat(name, fuel, MilliPence(5_000), MilliPence(25_000), start, end)


def test_versions_with_the_same_name_must_not_overlap_in_time() -> None:
    existing = [flat("Cap", date(2026, 1, 1), date(2026, 3, 31))]
    assert check_versions(existing, flat("Cap", date(2026, 4, 1), None)) == []
    assert check_versions(existing, flat("Cap", date(2025, 1, 1), date(2025, 12, 31))) == []
    for new in (
        flat("Cap", date(2026, 3, 31), None),  # shares the last day
        flat("Cap", None, None),  # always
        flat("cap", date(2026, 2, 1), date(2026, 2, 2)),  # inside, name case ignored
        flat("Cap", None, date(2026, 1, 1)),
    ):
        [error] = check_versions(existing, new)
        assert error.field == "effective_from"


def test_different_names_or_fuels_never_conflict_and_a_tariff_doesnt_clash_with_itself() -> None:
    saved = Tariff.flat("Cap", Fuel.GAS, MilliPence(1), MilliPence(1))
    other_fuel = flat("Cap", None, None, Fuel.ELECTRICITY)
    assert check_versions([saved], other_fuel) == []
    assert check_versions([saved], flat("Other", None, None)) == []
    same = Tariff("Cap", Fuel.GAS, MilliPence(1), saved.bands, None, None, tariff_id=3)
    stored = Tariff("Cap", Fuel.GAS, MilliPence(1), saved.bands, None, None, tariff_id=3)
    assert check_versions([stored], same) == []


def test_field_error_is_a_plain_value() -> None:
    assert FieldError("name", "x") == FieldError("name", "x")

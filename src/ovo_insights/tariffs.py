"""Tariffs: a name, a fuel, a daily standing charge and bands of unit rates.

All rates are inc. VAT, as entered, and held as integer milli-pence (per kWh, and per day for
the standing charge).

A *band* covers half-hour slots `[start_slot, end_slot)` of the UK wall-clock day (slot 0 is
00:00, slot 47 is 23:30, an end of 48 is midnight) on a set of weekdays (0 = Monday). It never
wraps past midnight. A weekday means the local calendar day of the slot itself, so "Monday
23:30 to 05:30" is Monday 23:30-24:00 plus Monday 00:00-05:30, and nothing carries into
Tuesday. A slot's wall-clock time picks its band, which also settles clock-change days: on the
50-slot day both 01:00 slots get the same rate.

Every weekday must have all 48 slots covered exactly once, so any day can be costed. A gas
tariff has one unit rate (gas is costed from daily kWh).

Rules are checked twice: `build_tariff` turns raw form input into a `Tariff`, reporting
field-level errors for everything wrong at once; and the `Tariff` and `Band` constructors
re-check the invariants, so a value read back from the database is as trustworthy as one just
built.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from itertools import groupby

from ovo_insights.domain import Fuel
from ovo_insights.money import MilliPence, MoneyError, parse_pence

SLOTS_PER_DAY = 48
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
ALL_WEEKDAYS = frozenset(range(7))

MAX_UNIT_RATE = MilliPence(500_000)  # 500 p/kWh
MAX_STANDING_CHARGE = MilliPence(1_000_000)  # 1,000 p/day (£10)
MAX_NAME_LENGTH = 64

_CLOCK = re.compile(r"([01]\d|2[0-3]):([03]0)", re.ASCII)
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)


@dataclass(frozen=True, slots=True)
class FieldError:
    """What is wrong and where. Fields are `name`, `fuel`, `standing`, `effective_from`,
    `effective_to`, `bands`, and `bands[i].start|end|rate|weekdays` (i from 0)."""

    field: str
    message: str


class TariffError(ValueError):
    """A tariff broke a rule. `errors` lists every problem found."""

    def __init__(self, errors: Sequence[FieldError]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(f"{e.field}: {e.message}" for e in self.errors))


def slot_label(slot: int) -> str:
    """`00:00`-`23:30` for a slot start, and `24:00` for the end of the day."""
    return f"{slot // 2:02d}:{30 * (slot % 2):02d}"


@dataclass(frozen=True, slots=True)
class Band:
    start_slot: int
    end_slot: int  # exclusive; 48 is midnight
    unit_rate: MilliPence
    weekdays: frozenset[int]

    def __post_init__(self) -> None:
        errors = _band_errors(self)
        if errors:
            raise TariffError(errors)


def _band_errors(band: Band) -> list[FieldError]:
    errors = []
    numbers: tuple[tuple[object, str], ...] = (
        (band.start_slot, "start"),
        (band.end_slot, "end"),
        (band.unit_rate, "rate"),
    )
    for value, what in numbers:
        if not _is_int(value):
            errors.append(FieldError("bands", f"band {what} must be an integer"))
    if errors:
        return errors
    if not 0 <= band.start_slot < band.end_slot <= SLOTS_PER_DAY:
        errors.append(FieldError("bands", "a band must start before it ends, within the day"))
    if not 0 <= band.unit_rate <= MAX_UNIT_RATE:
        errors.append(FieldError("bands", "a unit rate is outside 0 to 500 p/kWh"))
    days: object = band.weekdays
    if (
        not isinstance(days, frozenset)
        or not days
        or not all(_is_int(d) and 0 <= d <= 6 for d in days)
    ):
        errors.append(FieldError("bands", "a band needs weekdays from 0 (Monday) to 6 (Sunday)"))
    return errors


@dataclass(frozen=True, slots=True)
class Tariff:
    name: str
    fuel: Fuel
    standing_charge: MilliPence  # per day
    bands: tuple[Band, ...]
    effective_from: date | None = None  # inclusive; None = always
    effective_to: date | None = None  # inclusive
    tariff_id: int | None = None
    # rates[weekday][slot], filled in once the bands are known to cover everything.
    _rates: tuple[tuple[MilliPence, ...], ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        errors = _tariff_errors(self)
        if errors:
            raise TariffError(errors)
        table: list[list[MilliPence | None]] = [[None] * SLOTS_PER_DAY for _ in range(7)]
        for band in self.bands:
            for weekday in band.weekdays:
                for slot in range(band.start_slot, band.end_slot):
                    table[weekday][slot] = band.unit_rate
        object.__setattr__(
            self, "_rates", tuple(tuple(MilliPence(r or 0) for r in row) for row in table)
        )

    def rate_at(self, weekday: int, slot: int) -> MilliPence:
        """Unit rate (milli-pence per kWh) for a UK weekday (0 = Monday) and slot (0-47)."""
        return self._rates[weekday][slot]

    @property
    def is_flat(self) -> bool:
        """One unit rate for every slot of every day: costable from daily kWh alone."""
        return len({rate for row in self._rates for rate in row}) == 1

    def applies_on(self, day: date) -> bool:
        return (self.effective_from is None or day >= self.effective_from) and (
            self.effective_to is None or day <= self.effective_to
        )

    @classmethod
    def flat(
        cls,
        name: str,
        fuel: Fuel,
        unit_rate: MilliPence,
        standing_charge: MilliPence,
        effective_from: date | None = None,
        effective_to: date | None = None,
    ) -> "Tariff":
        """One rate all day, every day."""
        band = Band(0, SLOTS_PER_DAY, unit_rate, ALL_WEEKDAYS)
        return cls(name, fuel, standing_charge, (band,), effective_from, effective_to)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _tariff_errors(tariff: Tariff) -> list[FieldError]:
    errors: list[FieldError] = []
    errors += _name_errors(tariff.name)
    fuel: object = tariff.fuel
    if not isinstance(fuel, Fuel):
        errors.append(FieldError("fuel", "Choose electricity or gas."))
    standing: object = tariff.standing_charge
    if not _is_int(standing):
        errors.append(FieldError("standing", "The standing charge must be an integer."))
    elif not 0 <= tariff.standing_charge <= MAX_STANDING_CHARGE:
        errors.append(FieldError("standing", "The standing charge must be 0 to 1,000 p/day."))
    dates: tuple[tuple[object, str], ...] = (
        (tariff.effective_from, "effective_from"),
        (tariff.effective_to, "effective_to"),
    )
    for value, which in dates:
        if value is not None and type(value) is not date:
            errors.append(FieldError(which, "Enter a date."))
    if (
        tariff.effective_from is not None
        and tariff.effective_to is not None
        and type(tariff.effective_from) is date
        and type(tariff.effective_to) is date
        and tariff.effective_to < tariff.effective_from
    ):
        errors.append(FieldError("effective_to", "The end date is before the start date."))
    bands: object = tariff.bands
    if not isinstance(bands, tuple) or not bands:
        return [*errors, FieldError("bands", "Add at least one band.")]
    if not all(isinstance(b, Band) for b in bands):
        return [*errors, FieldError("bands", "Bands must be Band values.")]
    errors += _coverage_errors(tariff.bands)
    if tariff.fuel is Fuel.GAS and len({b.unit_rate for b in tariff.bands}) > 1:
        errors.append(FieldError("bands", "A gas tariff has a single unit rate."))
    return errors


def _name_errors(name: object) -> list[FieldError]:
    if not isinstance(name, str) or not name.strip():
        return [FieldError("name", "Enter a name.")]
    if len(name) > MAX_NAME_LENGTH or name != name.strip() or not name.isprintable():
        return [FieldError("name", f"Use 1 to {MAX_NAME_LENGTH} plain characters.")]
    return []


def _coverage_errors(bands: Iterable[Band]) -> list[FieldError]:
    """Each weekday's 48 slots must be covered exactly once: say which times are not."""
    counts = [[0] * SLOTS_PER_DAY for _ in range(7)]
    for band in bands:
        for weekday in band.weekdays:
            for slot in range(band.start_slot, band.end_slot):
                counts[weekday][slot] += 1
    errors = []
    for weekday, row in enumerate(counts):
        gaps = _runs([i for i, n in enumerate(row) if n == 0])
        overlaps = _runs([i for i, n in enumerate(row) if n > 1])
        if gaps:
            errors.append(
                FieldError("bands", f"{WEEKDAY_NAMES[weekday]} has no rate for {_times(gaps)}.")
            )
        if overlaps:
            errors.append(
                FieldError(
                    "bands", f"{WEEKDAY_NAMES[weekday]} has two rates for {_times(overlaps)}."
                )
            )
    return errors


def _runs(slots: list[int]) -> list[tuple[int, int]]:
    """Consecutive slots as (first, one past last) pairs."""
    runs = []
    for _, group in groupby(enumerate(slots), key=lambda pair: pair[1] - pair[0]):
        members = [slot for _, slot in group]
        runs.append((members[0], members[-1] + 1))
    return runs


def _times(runs: list[tuple[int, int]]) -> str:
    shown = [f"{slot_label(a)}-{slot_label(b)}" for a, b in runs[:3]]
    more = f" and {len(runs) - 3} more" if len(runs) > 3 else ""
    return ", ".join(shown) + more


# -- building from form input -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BandDraft:
    """A band as typed: times as `HH:MM`, the rate in p/kWh, weekdays 0 (Monday) to 6."""

    start: str
    end: str
    rate: str
    weekdays: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class TariffDraft:
    name: str
    fuel: str
    standing: str  # p/day
    effective_from: str  # `YYYY-MM-DD`, or empty
    effective_to: str
    bands: tuple[BandDraft, ...]


def build_tariff(draft: TariffDraft, tariff_id: int | None = None) -> Tariff:
    """The tariff a form describes, or a `TariffError` listing every problem at once.

    A band whose end is not after its start wraps past midnight (`23:30`-`05:30`) and is split
    into two. An end of `00:00` means midnight, so `00:00`-`00:00` is the whole day.
    """
    errors: list[FieldError] = []
    name = draft.name.strip()
    errors += _name_errors(name)

    fuel = _parse_fuel(draft.fuel)
    if fuel is None:
        errors.append(FieldError("fuel", "Choose electricity or gas."))

    standing = _parse_rate(
        draft.standing, "standing", MAX_STANDING_CHARGE, "0 to 1,000 p/day", errors
    )
    effective_from = _parse_date(draft.effective_from, "effective_from", errors)
    effective_to = _parse_date(draft.effective_to, "effective_to", errors)

    bands: list[Band] = []
    if not draft.bands:
        errors.append(FieldError("bands", "Add at least one band."))
    for index, band in enumerate(draft.bands):
        bands += _build_band(band, index, errors)

    if errors or fuel is None or standing is None:
        raise TariffError(errors)
    try:
        return Tariff(name, fuel, standing, tuple(bands), effective_from, effective_to, tariff_id)
    except TariffError as exc:
        raise TariffError([*errors, *exc.errors]) from exc


def _parse_fuel(text: str) -> Fuel | None:
    try:
        return Fuel(text.strip().lower())
    except ValueError:
        return None


def _parse_rate(
    text: str, field_name: str, ceiling: MilliPence, allowed: str, errors: list[FieldError]
) -> MilliPence | None:
    try:
        value = parse_pence(text)
    except (MoneyError, TypeError):
        errors.append(FieldError(field_name, "Enter a plain number such as 24.44 (pence)."))
        return None
    if value > ceiling:
        errors.append(FieldError(field_name, f"Must be {allowed}."))
        return None
    return value


def _parse_date(text: str, field_name: str, errors: list[FieldError]) -> date | None:
    cleaned = text.strip()
    if not cleaned:
        return None
    try:
        if not _ISO_DATE.fullmatch(cleaned):
            raise ValueError(cleaned)
        return date.fromisoformat(cleaned)
    except ValueError:
        errors.append(FieldError(field_name, "Enter a date as YYYY-MM-DD, or leave it empty."))
        return None


def _parse_clock(text: str, field_name: str, errors: list[FieldError]) -> int | None:
    match = _CLOCK.fullmatch(text.strip())
    if match is None:
        errors.append(FieldError(field_name, "Use a time on the hour or half hour, like 07:30."))
        return None
    return int(match.group(1)) * 2 + int(match.group(2)) // 30


def _build_band(band: BandDraft, index: int, errors: list[FieldError]) -> list[Band]:
    prefix = f"bands[{index}]"
    before = len(errors)
    start = _parse_clock(band.start, f"{prefix}.start", errors)
    end = _parse_clock(band.end, f"{prefix}.end", errors)
    rate = _parse_rate(band.rate, f"{prefix}.rate", MAX_UNIT_RATE, "0 to 500 p/kWh", errors)
    days = frozenset(band.weekdays)
    if (
        not days
        or not days <= ALL_WEEKDAYS
        or not all(isinstance(d, int) and not isinstance(d, bool) for d in days)
    ):
        errors.append(FieldError(f"{prefix}.weekdays", "Choose at least one day."))
    if len(errors) > before or start is None or end is None or rate is None:
        return []
    if end == 0:
        end = SLOTS_PER_DAY  # midnight
    if end == start:
        errors.append(
            FieldError(
                f"{prefix}.end", "A band can't be empty; use 00:00 to 00:00 for a whole day."
            )
        )
        return []
    if end > start:
        return [Band(start, end, rate, days)]
    return [Band(start, SLOTS_PER_DAY, rate, days), Band(0, end, rate, days)]  # wraps midnight


# -- versions of one tariff ---------------------------------------------------------------


def check_versions(existing: Iterable[Tariff], new: Tariff) -> list[FieldError]:
    """Tariffs with the same name and fuel are versions of one tariff, so their effective
    dates must not overlap. `existing` should not include `new` itself when editing it."""
    errors = []
    for other in existing:
        if (
            other.fuel is new.fuel
            and other.name.casefold() == new.name.casefold()
            and (new.tariff_id is None or other.tariff_id != new.tariff_id)
            and _overlap(other, new)
        ):
            errors.append(
                FieldError(
                    "effective_from",
                    f"Another version of {other.name} already covers some of these dates.",
                )
            )
    return errors


def _overlap(a: Tariff, b: Tariff) -> bool:
    starts = [d for d in (a.effective_from, b.effective_from) if d is not None]
    ends = [d for d in (a.effective_to, b.effective_to) if d is not None]
    latest_start = max(starts, default=None)
    earliest_end = min(ends, default=None)
    return latest_start is None or earliest_end is None or latest_start <= earliest_end

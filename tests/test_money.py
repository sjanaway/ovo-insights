from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ovo_insights.money import (
    MAX_ABS_POUNDS,
    MILLI_PENCE_PER_POUND,
    MilliPence,
    MoneyError,
    format_pence,
    format_pounds,
    parse_pence,
    parse_pounds,
    to_pounds,
)

LIMIT = int(MAX_ABS_POUNDS) * MILLI_PENCE_PER_POUND


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0", 0),
        ("1", 100_000),
        ("1.23", 123_000),
        ("0.01", 1_000),
        ("12.34567", 1_234_567),
        (" 2.50 ", 250_000),
        ("+3", 300_000),
        (".5", 50_000),
        ("7.", 700_000),
        (5, 500_000),
        (Decimal("0.00001"), 1),
    ],
)
def test_parse_pounds_examples(text: str | int | Decimal, expected: int) -> None:
    assert parse_pounds(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.000005", 0),  # half-even: ties go to the even milli-penny
        ("0.000015", 2),
        ("0.0000151", 2),
        ("0.0000149", 1),
    ],
)
def test_parse_pounds_rounds_half_even(text: str, expected: int) -> None:
    assert parse_pounds(text) == expected


def test_parse_pence_examples() -> None:
    assert parse_pence("24.567") == 24_567
    assert parse_pence(60) == 60_000


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "abc",
        "£1.00",
        "1,000.00",
        "1e3",
        "1E-2",
        "NaN",
        "nan",
        "Infinity",
        "-inf",
        "1.2.3",
        "--1",
        "0x10",
        "1_000",
        "\uff11\uff12",  # full-width digits must not sneak through \d
        "1" * 41,
    ],
)
def test_parse_pounds_rejects_malformed_text(text: str) -> None:
    with pytest.raises(MoneyError):
        parse_pounds(text)


@pytest.mark.parametrize("value", [1.5, True, None, [1], b"1"])
def test_parse_pounds_rejects_wrong_types(value: object) -> None:
    with pytest.raises(TypeError):
        parse_pounds(value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_parse_pounds_rejects_non_finite_decimals(value: Decimal) -> None:
    with pytest.raises(MoneyError):
        parse_pounds(value)


@pytest.mark.parametrize("value", ["1000000.01", "-1000000.01", Decimal("1E+999999")])
def test_parse_pounds_rejects_out_of_range(value: str | Decimal) -> None:
    with pytest.raises(MoneyError, match="out of range"):
        parse_pounds(value, allow_negative=True)


def test_negative_amounts_need_explicit_permission() -> None:
    with pytest.raises(MoneyError, match="Negative"):
        parse_pounds("-0.01")
    assert parse_pounds("-0.01", allow_negative=True) == -1_000
    assert parse_pounds("-0") == 0


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (0, "£0.00"),
        (123_000, "£1.23"),
        (123_456_789, "£1,234.57"),
        (-5_000, "-£0.05"),
        (500, "£0.00"),  # half a penny rounds to even (0)
        (1_500, "£0.02"),
    ],
)
def test_format_pounds(amount: int, expected: str) -> None:
    assert format_pounds(MilliPence(amount)) == expected


@given(st.integers(min_value=-LIMIT, max_value=LIMIT))
def test_round_trip_through_exact_pounds_text(amount: int) -> None:
    text = format(to_pounds(MilliPence(amount)), "f")

    assert parse_pounds(text, allow_negative=True) == amount


@given(
    st.decimals(
        min_value=-MAX_ABS_POUNDS, max_value=MAX_ABS_POUNDS, allow_nan=False, allow_infinity=False
    )
)
def test_parsing_is_within_half_a_milli_penny(value: Decimal) -> None:
    parsed = parse_pounds(value, allow_negative=True)

    assert abs(to_pounds(parsed) - value) <= Decimal("0.000005")


@given(st.lists(st.integers(min_value=0, max_value=10**9), max_size=50))
def test_sum_of_parsed_equals_parsed_sum(amounts: list[int]) -> None:
    # Integer milli-pence never drift when summed, unlike floats.
    texts = [format(to_pounds(MilliPence(a)), "f") for a in amounts]
    total_text = format(sum(to_pounds(MilliPence(a)) for a in amounts), "f")

    assert sum(parse_pounds(t) for t in texts) == parse_pounds(total_text)


def test_format_pence_is_exactly_three_decimals() -> None:
    assert [
        format_pence(MilliPence(v)) for v in (24_440, 5_000, 0, 1, 500_000, 53_680, 1_234_567)
    ] == [
        "24.440",
        "5.000",
        "0.000",
        "0.001",
        "500.000",
        "53.680",
        "1234.567",
    ]

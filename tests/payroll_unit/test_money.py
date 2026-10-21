"""Pure tests for app.payroll.money (3.2 §3, 3.1 §5.13, EC-MON-*)."""

from __future__ import annotations

import ast
import decimal
import math
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.payroll import money
from app.payroll.money import (
    CALC_CONTEXT,
    CurrencyMismatch,
    Money,
    MoneyError,
    ROUNDING_MODES,
    allocate_percentages,
    amount_str,
    check_increment,
    format_many,
    format_money,
    is_storable,
    needs_iso_codes,
    quantize_amount,
    round_cash,
    round_line,
    round_net,
    round_statutory,
    sum_money,
    to_decimal,
    totals_by_currency,
    validate_net_pay_increment,
)

NBSP = " "
D = Decimal


def inr(value: str) -> Money:
    return Money.of(value, "INR")


def code_of(excinfo: pytest.ExceptionInfo[BaseException]) -> str:
    return getattr(excinfo.value, "code")


# --- golden vectors -------------------------------------------------------------------------------


def test_golden_3_5_s17_gross_to_net_lines_round_and_sum_exactly() -> None:
    """3.5 §17 worked example: each line rounded to the minor unit, gross and net are plain sums."""
    basic = round_line(inr("50000.00") * 28 / 30).money
    hra = round_line(inr("20000.00") * 28 / 30).money
    fixed = round_line(inr("27000.00") * 28 / 30).money
    overtime = round_line(inr("50000.00") / 26 / 8 * D("1.5") * 4).money
    bonus = inr("10000.00")
    assert [line.to_json()["amount"] for line in (basic, hra, fixed, overtime)] == ["46666.67", "18666.67", "25200.00", "1442.31"]
    gross = sum_money([basic, hra, fixed, overtime, bonus], "INR")
    assert gross.to_json() == {"amount": "101975.65", "currency": "INR"}
    deductions = sum_money([inr("3000.00"), inr("200.00"), inr("2000.00")], "INR")
    assert (gross - deductions).to_json()["amount"] == "96775.65"
    assert format_money(gross.amount, "INR", "en-IN") == "₹1,01,975.65"


def test_golden_3_5_s17_net_rounding_to_whole_rupees_gives_net_rounding_line() -> None:
    """3.5 §17 net (before the illustrative tax T) with payroll.rounding.net_pay_increment = 1 (3.1 §5.13)."""
    result = round_net(inr("96775.65"), 1, "HALF_UP")
    assert result.rounded.to_json() == {"amount": "96776.00", "currency": "INR"}
    assert result.difference.to_json()["amount"] == "-0.35"
    assert result.line_amount.to_json()["amount"] == "0.35"


def test_golden_3_2_s3_5_indian_grouping_from_cldr() -> None:
    """3.2 §3.5: en-IN shows 12,34,567.00."""
    assert format_money("1234567", "INR", "en-IN") == "₹12,34,567.00"
    assert format_money("1234567", "INR", "en_IN", use_code=True) == f"INR{NBSP}12,34,567.00"


def test_golden_3_2_s3_4_serialisation_examples() -> None:
    """3.2 §3.4: "12345.67" INR, "1500" JPY, "12.345" KWD."""
    assert Money.of("12345.67", "INR").to_json() == {"amount": "12345.67", "currency": "INR"}
    assert Money.of("1500", "JPY").to_json() == {"amount": "1500", "currency": "JPY"}
    assert Money.of("12.345", "KWD").to_json() == {"amount": "12.345", "currency": "KWD"}


# --- to_decimal and float rejection (EC-MON-20) ----------------------------------------------------


@pytest.mark.parametrize("value, expected", [("1.50", D("1.50")), (3, D(3)), (D("-0.10"), D("-0.10")), ("-0", D("0")), (".5", D("0.5"))])
def test_to_decimal_accepts_exact_inputs(value: object, expected: Decimal) -> None:
    result = to_decimal(value)
    assert result == expected and str(result) == str(expected)


@pytest.mark.parametrize(
    "value, code",
    [
        (1.5, "FLOAT_NOT_ALLOWED"),
        (True, "INVALID_DECIMAL"),
        (None, "INVALID_DECIMAL"),
        ([1], "INVALID_DECIMAL"),
        ("1,000", "INVALID_DECIMAL"),
        (" 1", "INVALID_DECIMAL"),
        ("1_000", "INVALID_DECIMAL"),
        ("١٢", "INVALID_DECIMAL"),
        ("NaN", "INVALID_DECIMAL"),
        (D("NaN"), "NOT_FINITE"),
        (D("Infinity"), "NOT_FINITE"),
        ("1" * 29, "PRECISION_EXCEEDED"),
        ("0." + "1" * 29, "PRECISION_EXCEEDED"),
    ],
)
def test_to_decimal_rejects_inexact_or_malformed_inputs(value: object, code: str) -> None:
    with pytest.raises(MoneyError) as excinfo:
        to_decimal(value)
    assert code_of(excinfo) == code


def test_ec_mon_20_float_rejected_everywhere() -> None:
    m = inr("10.00")
    for call in (
        lambda: Money.of(1.5, "INR"),  # type: ignore[arg-type]
        lambda: Money(1.5, "INR"),  # type: ignore[arg-type]
        lambda: m * 1.5,
        lambda: m / 1.5,
        lambda: quantize_amount(1.5, "INR"),  # type: ignore[arg-type]
        lambda: m.round("HALF_UP", 0.05),  # type: ignore[arg-type]
        lambda: check_increment(0.05, "INR"),  # type: ignore[arg-type]
        lambda: amount_str(1.5, "INR"),  # type: ignore[arg-type]
        lambda: format_money(1.5, "INR"),  # type: ignore[arg-type]
        lambda: allocate_percentages(m, [50.0]),  # type: ignore[list-item]
    ):
        with pytest.raises(MoneyError) as excinfo:
            call()
        assert code_of(excinfo) == "FLOAT_NOT_ALLOWED"


def _float_uses(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    offending: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            offending.append(node.lineno)
        if isinstance(node, ast.Name) and node.id == "float":
            parent = parents.get(node)
            if isinstance(parent, ast.Tuple):
                parent = parents.get(parent)
            is_type_check = isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) and parent.func.id == "isinstance"
            if not is_type_check:
                offending.append(node.lineno)
    return offending


@pytest.mark.parametrize("module", ["money.py", "currencies.py", "countries.py", "fx.py", "api_types.py", "money_errors.py"])
def test_ec_mon_20_lint_no_float_in_money_modules(module: str) -> None:
    """EC-MON-20: float appears only in isinstance checks that reject it; no float literals."""
    path = Path(money.__file__).with_name(module)
    assert _float_uses(path) == []


def test_ec_mon_20_lint_detects_a_float(tmp_path: Path) -> None:
    """The lint itself must catch both a float() conversion and a float literal."""
    sample = tmp_path / "sample.py"
    sample.write_text("x = float('1')\ny = 0.5\nz = isinstance(x, float)\n", encoding="utf-8")
    assert sorted(_float_uses(sample)) == [1, 2]


# --- rounding modes ------------------------------------------------------------------------------

ROUNDING_TABLE = [
    # value, HALF_UP, HALF_EVEN, UP, DOWN, CEILING, FLOOR (to the INR minor unit)
    ("2.345", "2.35", "2.34", "2.35", "2.34", "2.35", "2.34"),
    ("2.355", "2.36", "2.36", "2.36", "2.35", "2.36", "2.35"),
    ("-2.345", "-2.35", "-2.34", "-2.35", "-2.34", "-2.34", "-2.35"),
    ("2.341", "2.34", "2.34", "2.35", "2.34", "2.35", "2.34"),
    ("-2.349", "-2.35", "-2.35", "-2.35", "-2.34", "-2.34", "-2.35"),
    ("2.34", "2.34", "2.34", "2.34", "2.34", "2.34", "2.34"),
    ("-0.001", "0.00", "0.00", "-0.01", "0.00", "0.00", "-0.01"),
]
MODES = ("HALF_UP", "HALF_EVEN", "UP", "DOWN", "CEILING", "FLOOR")


@pytest.mark.parametrize("row", ROUNDING_TABLE, ids=[row[0] for row in ROUNDING_TABLE])
def test_rounding_modes_table_to_minor_unit(row: tuple[str, ...]) -> None:
    value, *expected = row
    for mode, want in zip(MODES, expected):
        assert amount_str(quantize_amount(value, "INR", mode), "INR") == want, mode


def test_rounding_mode_names_match_architecture() -> None:
    assert ROUNDING_MODES == {"HALF_UP", "HALF_EVEN", "UP", "DOWN", "CEILING", "FLOOR"}


def test_rounding_never_produces_negative_zero() -> None:
    assert str(quantize_amount("-0.001", "INR", "HALF_UP")) == "0.00"
    assert str(quantize_amount("-0.4", "JPY", "HALF_UP", 1)) == "0"


@pytest.mark.parametrize(
    "value, currency, mode, increment, expected",
    [
        ("12.37", "CHF", "HALF_UP", "0.05", "12.35"),
        ("12.375", "CHF", "HALF_UP", "0.05", "12.40"),
        ("12.33", "CHF", "UP", "0.05", "12.35"),
        ("46666.67", "INR", "HALF_UP", "1", "46667.00"),
        ("46666.49", "INR", "HALF_UP", "1", "46666.00"),
        ("46666.50", "INR", "HALF_UP", "1", "46667.00"),
        ("46666.50", "INR", "HALF_EVEN", "1", "46666.00"),
        ("25", "INR", "HALF_EVEN", "10", "20.00"),
        ("35", "INR", "HALF_EVEN", "10", "40.00"),
        ("-46666.50", "INR", "HALF_UP", "1", "-46667.00"),
        ("-46666.01", "INR", "CEILING", "1", "-46666.00"),
        ("-46666.01", "INR", "FLOOR", "1", "-46667.00"),
        ("1499", "JPY", "HALF_UP", "100", "1500"),
        ("12.3449", "KWD", "HALF_UP", "0.005", "12.345"),
    ],
)
def test_increment_rounding(value: str, currency: str, mode: str, increment: str, expected: str) -> None:
    assert amount_str(quantize_amount(value, currency, mode, D(increment)), currency) == expected


@pytest.mark.parametrize("increment", ["0", "-1", "0.001", "0.015"])
def test_increment_must_be_positive_multiple_of_minor_unit(increment: str) -> None:
    with pytest.raises(MoneyError) as excinfo:
        quantize_amount("10", "INR", "HALF_UP", D(increment))
    assert code_of(excinfo) == "INVALID_INCREMENT"


def test_net_pay_increment_at_most_100_units_v13() -> None:
    assert validate_net_pay_increment(D("100"), "INR") == D("100")
    with pytest.raises(MoneyError) as excinfo:
        validate_net_pay_increment(D("100.01"), "INR")
    assert code_of(excinfo) == "INVALID_INCREMENT"
    with pytest.raises(MoneyError):
        validate_net_pay_increment(D("0.5"), "JPY")


def test_unknown_and_unsupported_rounding_modes() -> None:
    with pytest.raises(MoneyError) as excinfo:
        quantize_amount("1", "INR", "ROUND_HALF_UP")
    assert code_of(excinfo) == "UNKNOWN_ROUNDING_MODE"
    with pytest.raises(MoneyError) as excinfo:
        round_line(inr("1.005"), "CEILING")  # payroll.rounding.line_mode allows HALF_UP, HALF_EVEN, UP, DOWN
    assert code_of(excinfo) == "UNSUPPORTED_ROUNDING_MODE"
    with pytest.raises(MoneyError) as excinfo:
        round_net(inr("1.005"), 1, "HALF_EVEN")  # payroll.rounding.net_pay_mode allows HALF_UP, UP, DOWN
    assert code_of(excinfo) == "UNSUPPORTED_ROUNDING_MODE"


def test_round_line_records_unrounded_value_for_trace() -> None:
    rounded = round_line(inr("50000.00") * 28 / 30, "HALF_UP")
    trace = rounded.trace()
    assert trace["unrounded"].startswith("46666.6666666")
    assert trace["rounding"] == {"stage": "LINE", "mode": "HALF_UP", "increment": "0.01"}
    assert trace["amount"] == {"amount": "46666.67", "currency": "INR"}
    assert rounded.difference.amount == rounded.money.amount - rounded.unrounded


@pytest.mark.parametrize(
    "rule, value, expected",
    [
        ("NEAREST_WHOLE_UNIT", "1749.50", "1750.00"),
        ("NEAREST_WHOLE_UNIT", "1749.49", "1749.00"),
        ("UP_TO_WHOLE_UNIT", "1749.01", "1750.00"),
        ("UP_TO_WHOLE_UNIT", "1749.00", "1749.00"),
        ("UP_TO_WHOLE_UNIT", "-1749.01", "-1750.00"),
    ],
)
def test_statutory_rounding_rules_override_line_mode(rule: str, value: str, expected: str) -> None:
    rounded = round_statutory(inr(value), rule)
    assert rounded.money.to_json()["amount"] == expected
    assert rounded.stage == "STATUTORY"


def test_statutory_rounding_unknown_rule() -> None:
    with pytest.raises(MoneyError) as excinfo:
        round_statutory(inr("1.00"), "NEAREST_TEN")
    assert code_of(excinfo) == "UNKNOWN_ROUNDING_RULE"


# --- EC-MON-01 / 02: 0- and 3-decimal currencies ---------------------------------------------------


def test_ec_mon_01_jpy_has_no_decimals_end_to_end() -> None:
    salary = Money.of("300000", "JPY")
    prorated = round_line(salary * 28 / 30).money
    assert prorated.to_json() == {"amount": "280000", "currency": "JPY"}
    assert round_line(Money.of("1499.5", "JPY")).money.to_json()["amount"] == "1500"
    assert Money.zero("JPY").to_json()["amount"] == "0"
    assert format_money("1500", "JPY", "en") == "¥1,500"
    assert format_money("1500", "JPY", "en", use_code=True) == f"JPY{NBSP}1,500"
    assert is_storable(prorated.amount)


def test_ec_mon_01_jpy_rejects_fractional_output() -> None:
    with pytest.raises(MoneyError) as excinfo:
        Money.of("1500.5", "JPY").to_json()
    assert code_of(excinfo) == "AMOUNT_NOT_ROUNDED"
    with pytest.raises(MoneyError):
        format_money("1500.5", "JPY")
    assert amount_str("1500.0", "JPY") == "1500"  # trailing zeros are not extra precision


def test_ec_mon_02_kwd_has_three_decimals_end_to_end() -> None:
    line = round_line(Money.of("1000.000", "KWD") / 3).money
    assert line.to_json() == {"amount": "333.333", "currency": "KWD"}
    assert Money.zero("KWD").to_json()["amount"] == "0.000"
    assert Money.of("12.3", "KWD").round().to_json()["amount"] == "12.300"
    for code in ("BHD", "OMR", "JOD", "TND", "IQD", "LYD"):
        assert Money.of("1.2345", code).round("HALF_UP").to_json()["amount"] == "1.235"
    assert format_money("12.345", "KWD", "en") == f"KWD{NBSP}12.345"


def test_ec_mon_02_kwd_boundary_fourth_decimal() -> None:
    assert quantize_amount("12.3455", "KWD", "HALF_EVEN") == D("12.346")
    assert quantize_amount("12.3445", "KWD", "HALF_EVEN") == D("12.344")
    assert quantize_amount("12.3455", "KWD", "DOWN") == D("12.345")
    with pytest.raises(MoneyError):
        amount_str("12.3455", "KWD")


def test_four_decimal_fund_currency_is_representable() -> None:
    assert Money.of("1.2345", "CLF").to_json() == {"amount": "1.2345", "currency": "CLF"}


# --- arithmetic and EC-MON-04 ------------------------------------------------------------------------


def test_arithmetic_within_one_currency() -> None:
    a, b = inr("10.50"), inr("2.25")
    assert (a + b).amount == D("12.75")
    assert (a - b).amount == D("8.25")
    assert (a * 2).amount == D("21.00")
    assert (2 * a).amount == D("21.00")
    assert (a / 4).amount == D("2.625")
    assert a / b == D("4.666666666666666666666666667")
    assert -a == inr("-10.50") and abs(-a) == a and +a is a
    assert a > b and b < a and a >= a and b <= b
    assert not Money.zero("INR") and Money.zero("INR").is_zero()
    assert a.is_positive() and (-a).is_negative()
    assert str(a) == "10.50 INR"


def test_arithmetic_rejects_bad_operands() -> None:
    a = inr("10.00")
    with pytest.raises(MoneyError) as excinfo:
        a * a  # type: ignore[operator]
    assert code_of(excinfo) == "INVALID_OPERATION"
    with pytest.raises(MoneyError) as excinfo:
        a / D(0)
    assert code_of(excinfo) == "DIVISION_BY_ZERO"
    with pytest.raises(MoneyError) as excinfo:
        a / Money.zero("INR")
    assert code_of(excinfo) == "DIVISION_BY_ZERO"
    with pytest.raises(TypeError):
        a + 1  # type: ignore[operator]
    with pytest.raises(TypeError):
        a * True  # type: ignore[operator]
    with pytest.raises(TypeError):
        sum([a, a])  # type: ignore[list-item]


def test_ec_mon_04_mixed_currency_totals_never_summed() -> None:
    items = [inr("100.00"), Money.of("5.00", "USD"), inr("50.50"), Money.of("1500", "JPY"), Money.of("2.50", "USD")]
    totals = totals_by_currency(items)
    assert list(totals) == ["INR", "JPY", "USD"]
    assert {code: total.to_json()["amount"] for code, total in totals.items()} == {"INR": "150.50", "JPY": "1500", "USD": "7.50"}


def test_ec_mon_04_cross_currency_operations_raise() -> None:
    a, b = inr("1.00"), Money.of("1.00", "USD")
    for operation in (lambda: a + b, lambda: a - b, lambda: a / b, lambda: a < b, lambda: a >= b):
        with pytest.raises(CurrencyMismatch) as excinfo:
            operation()
        assert code_of(excinfo) == "CURRENCY_MISMATCH"
    with pytest.raises(CurrencyMismatch):
        sum_money([a, b], "INR")
    assert a != b
    assert totals_by_currency([]) == {}


def test_sum_money_empty_and_non_money() -> None:
    assert sum_money([], "KWD").to_json()["amount"] == "0.000"
    with pytest.raises(MoneyError) as excinfo:
        sum_money([D("1")], "INR")  # type: ignore[list-item]
    assert code_of(excinfo) == "INVALID_OPERATION"
    with pytest.raises(MoneyError):
        totals_by_currency([D("1")])  # type: ignore[list-item]


def test_money_validates_currency() -> None:
    for code, error in (("inr", "INVALID_CURRENCY_CODE"), ("ABC", "UNKNOWN_CURRENCY"), ("XAU", "NON_MONETARY_CURRENCY")):
        with pytest.raises(MoneyError) as excinfo:
            Money.of("1", code)
        assert code_of(excinfo) == error


def test_arithmetic_ignores_thread_decimal_context() -> None:
    with decimal.localcontext() as context:
        context.prec = 3
        context.rounding = decimal.ROUND_DOWN
        total = inr("12345.67") + inr("0.01")
        third = inr("100.00") / 3
    assert total.amount == D("12345.68")
    assert third.round().amount == D("33.33")
    assert CALC_CONTEXT.prec == 28


def test_from_json_rejects_numbers_and_unrounded_amounts() -> None:
    assert Money.from_json({"amount": "12.34", "currency": "INR"}) == inr("12.34")
    for payload, code in (
        ({"amount": 12.34, "currency": "INR"}, "AMOUNT_NOT_STRING"),
        ({"amount": 12, "currency": "INR"}, "AMOUNT_NOT_STRING"),
        ({"amount": "1", "currency": "INR", "extra": 1}, "INVALID_MONEY"),
        ({"amount": "12.345", "currency": "INR"}, "AMOUNT_NOT_ROUNDED"),
    ):
        with pytest.raises(MoneyError) as excinfo:
            Money.from_json(payload)
        assert code_of(excinfo) == code
    assert Money.from_json({"amount": "12.345", "currency": "INR"}, exact=False).amount == D("12.345")


# --- EC-MON-10 / 11 ---------------------------------------------------------------------------------


def test_ec_mon_10_totals_are_sums_of_rounded_lines() -> None:
    lines = [round_line(inr("100.00") / 3) for _ in range(3)]
    total = sum_money([line.money for line in lines], "INR")
    assert total.to_json()["amount"] == "99.99"  # never re-rounded to 100.00
    assert all(line.trace()["unrounded"].startswith("33.333333") for line in lines)


def test_ec_mon_10_many_lines_reconcile_with_differences() -> None:
    lines = [round_line(inr("0.005") * k, "HALF_EVEN") for k in range(1, 200)]
    rounded_total = sum_money([line.money for line in lines], "INR")
    unrounded_total = sum(line.unrounded for line in lines)
    differences = sum(line.difference.amount for line in lines)
    assert rounded_total.amount == unrounded_total + differences
    assert amount_str(rounded_total.amount, "INR") == format(rounded_total.amount, "f")  # exactly two decimals, never re-rounded


def test_ec_mon_11_net_rounding_line_or_carry_forward() -> None:
    first = round_net(inr("46666.67"), D("1"), "HALF_UP")
    assert first.rounded.to_json()["amount"] == "46667.00"
    assert first.difference.to_json()["amount"] == "-0.33"  # CARRY_FORWARD: next period starts 0.33 lower
    second = round_net(inr("46666.67") + first.difference, D("1"), "DOWN")
    assert second.rounded.to_json()["amount"] == "46666.00"
    assert second.difference.to_json()["amount"] == "0.34"
    assert (first.rounded + second.rounded) + second.difference == inr("46666.67") * 2


def test_ec_mon_11_boundary_whole_net_and_negative_net() -> None:
    whole = round_net(inr("50000.00"), D("1"))
    assert whole.difference.is_zero() and whole.rounded == inr("50000.00")
    negative = round_net(inr("-10.50"), D("1"), "HALF_UP")  # negative net is rounded, never clipped to zero
    assert negative.rounded.to_json()["amount"] == "-11.00"
    assert negative.difference.to_json()["amount"] == "0.50"
    default = round_net(inr("10.505"))  # no increment: the currency minor unit
    assert default.rounded.to_json()["amount"] == "10.51"
    up = round_net(Money.of("1001", "JPY"), D("100"), "UP")
    assert up.rounded.to_json()["amount"] == "1100" and up.line_amount.to_json()["amount"] == "99"


def test_round_cash_uses_cash_increment() -> None:
    chf = round_cash(Money.of("12.37", "CHF"))
    assert chf.rounded.to_json()["amount"] == "12.35" and chf.difference.to_json()["amount"] == "0.02"
    assert round_cash(Money.of("10.50", "SEK")).rounded.to_json()["amount"] == "11.00"
    assert round_cash(Money.of("10.24", "DKK")).rounded.to_json()["amount"] == "10.00"
    assert round_cash(Money.of("10.26", "DKK")).rounded.to_json()["amount"] == "10.50"
    assert round_cash(Money.of("10.25", "INR")).rounded.to_json()["amount"] == "10.25"


# --- EC-MON-12 / 16 / 17 ----------------------------------------------------------------------------


def test_ec_mon_12_very_large_amounts_fit_storage() -> None:
    vnd = Money.of("999999999999999", "VND")
    assert is_storable(vnd.amount)
    assert money.ensure_storable(vnd) is vnd
    assert format_money(vnd.amount, "VND", "en") == "₫999,999,999,999,999"
    assert format_money("123456789012345.67", "IDR", "en", use_code=True) == f"IDR{NBSP}123,456,789,012,345.67"


def test_ec_mon_12_beyond_numeric_19_4_is_rejected() -> None:
    assert not is_storable("1000000000000000")
    assert not is_storable("1.00001")
    with pytest.raises(MoneyError) as excinfo:
        money.ensure_storable(Money.of("1000000000000000", "VND"))
    assert code_of(excinfo) == "AMOUNT_NOT_STORABLE"


def test_ec_mon_16_symbol_for_one_currency_codes_for_several() -> None:
    assert not needs_iso_codes(["USD", "USD"])
    assert needs_iso_codes(["USD", "CAD"])
    assert format_many([Money.of("1.00", "USD"), Money.of("2.00", "USD")]) == ["$1.00", "$2.00"]
    assert format_many([Money.of("1.00", "USD"), Money.of("2.00", "CAD")]) == [f"USD{NBSP}1.00", f"CAD{NBSP}2.00"]


def test_format_money_negative_and_locale_errors() -> None:
    assert format_money("-1234.50", "USD", "en") == "-$1,234.50"
    assert format_money("1234.5", "EUR", "de-DE") == f"1.234,50{NBSP}€"
    assert format_money("83.912345678901", "INR", "en", fraction_digits=12) == "₹83.912345678901"
    with pytest.raises(MoneyError) as excinfo:
        format_money("1.00", "INR", "en", fraction_digits=1)
    assert code_of(excinfo) == "INVALID_FRACTION_DIGITS"
    with pytest.raises(MoneyError) as excinfo:
        format_money("1.00", "INR", "xx-!!")
    assert code_of(excinfo) == "UNKNOWN_LOCALE"
    assert Money.of("5.00", "USD").format("en", use_code=True) == f"USD{NBSP}5.00"


def test_ec_mon_17_split_remainder_gets_rounding_difference() -> None:
    allocation = allocate_percentages(Money.of("1000.01", "AED"), [D("70"), D("30")])
    assert [share.to_json()["amount"] for share in allocation.shares] == ["700.00", "300.00"]
    assert allocation.remainder.to_json()["amount"] == "0.01"
    odd = allocate_percentages(Money.of("100.00", "AED"), [D("33.333")])
    assert odd.shares[0].to_json()["amount"] == "33.33" and odd.remainder.to_json()["amount"] == "66.67"


def test_ec_mon_17_split_percentages_validated() -> None:
    for percents in ([D("70"), D("31")], [D("-1")], [D("100.01")]):
        with pytest.raises(MoneyError) as excinfo:
            allocate_percentages(Money.of("1.00", "AED"), percents)
        assert code_of(excinfo) == "INVALID_PERCENT"


# --- properties -------------------------------------------------------------------------------------

CURRENCIES = st.sampled_from(["INR", "JPY", "KWD", "USD", "CLF"])
AMOUNTS = st.decimals(min_value=D("-1e12"), max_value=D("1e12"), places=6, allow_nan=False, allow_infinity=False)
ROUND_MODES = st.sampled_from(sorted(ROUNDING_MODES))


@settings(max_examples=300, deadline=None)
@given(AMOUNTS, CURRENCIES, ROUND_MODES)
def test_property_quantize_is_idempotent_and_exact(value: Decimal, currency: str, mode: str) -> None:
    once = quantize_amount(value, currency, mode)
    assert quantize_amount(once, currency, mode) == once
    assert amount_str(once, currency) == format(once, "f")


@settings(max_examples=300, deadline=None)
@given(AMOUNTS, CURRENCIES, ROUND_MODES)
def test_property_increment_rounding_matches_decimal_quantize(value: Decimal, currency: str, mode: str) -> None:
    unit = money.quantum(currency)
    assert quantize_amount(value, currency, mode, unit) == quantize_amount(value, currency, mode)


@settings(max_examples=300, deadline=None)
@given(AMOUNTS, AMOUNTS, CURRENCIES)
def test_property_add_then_subtract_is_identity(a: Decimal, b: Decimal, currency: str) -> None:
    x, y = Money.of(a, currency), Money.of(b, currency)
    assert (x + y) - y == x
    assert x + y == y + x


@settings(max_examples=300, deadline=None)
@given(AMOUNTS, st.sampled_from(["0.01", "0.05", "1", "10", "100"]), st.sampled_from(["HALF_UP", "UP", "DOWN"]))
def test_property_round_net_rounded_plus_difference_is_original(value: Decimal, increment: str, mode: str) -> None:
    net = inr(format(value.quantize(D("0.01")), "f"))
    step = D(increment)
    result = round_net(net, step, mode)
    assert result.rounded + result.difference == net
    assert CALC_CONTEXT.remainder(result.rounded.amount, step).is_zero()
    assert result.difference.amount.copy_abs() < step
    if mode == "HALF_UP":
        assert result.difference.amount.copy_abs() * 2 <= step


@settings(max_examples=200, deadline=None)
@given(AMOUNTS, CURRENCIES)
def test_property_floor_le_value_le_ceiling(value: Decimal, currency: str) -> None:
    assert quantize_amount(value, currency, "FLOOR") <= value <= quantize_amount(value, currency, "CEILING")
    assert quantize_amount(value, currency, "DOWN").copy_abs() <= value.copy_abs() <= quantize_amount(value, currency, "UP").copy_abs()


@settings(max_examples=200, deadline=None)
@given(
    st.decimals(min_value=D("0"), max_value=D("1e9"), places=2, allow_nan=False, allow_infinity=False),
    st.lists(st.decimals(min_value=D("0"), max_value=D("25"), places=3, allow_nan=False, allow_infinity=False), max_size=4),
)
def test_property_allocation_shares_plus_remainder_is_total(total: Decimal, percents: list[Decimal]) -> None:
    whole = inr(format(total, "f"))
    allocation = allocate_percentages(whole, percents)
    assert sum_money(allocation.shares, "INR") + allocation.remainder == whole
    assert all(not share.is_negative() for share in allocation.shares)


# --- review additions: single rounding of exact values -------------------------------------------


def _reference_round(value: Fraction, step: Fraction, mode: str) -> Fraction:
    """Rounding to a multiple of ``step`` computed with exact rationals (the oracle for every mode)."""
    quotient = value / step
    floor, ceiling = math.floor(quotient), math.ceil(quotient)
    if floor == ceiling:
        return value
    toward, away = (floor, ceiling) if quotient > 0 else (ceiling, floor)
    if mode == "DOWN":
        chosen = toward
    elif mode == "UP":
        chosen = away
    elif mode == "FLOOR":
        chosen = floor
    elif mode == "CEILING":
        chosen = ceiling
    else:
        distance = abs(quotient - toward)
        if distance != Fraction(1, 2):
            chosen = away if distance > Fraction(1, 2) else toward
        elif mode == "HALF_UP":
            chosen = away
        else:
            chosen = floor if floor % 2 == 0 else ceiling
    return chosen * step


@settings(max_examples=400, deadline=None)
@given(
    st.integers(min_value=-(10**17), max_value=10**17),
    st.integers(min_value=0, max_value=6),
    CURRENCIES,
    ROUND_MODES,
    st.sampled_from([None, "1", "0.05", "10", "0.5"]),
)
def test_property_rounding_matches_exact_rational_oracle(units: int, scale: int, currency: str, mode: str, increment: str | None) -> None:
    value = D(units).scaleb(-scale)
    step = money.quantum(currency) if increment is None else D(increment)
    try:
        money.check_increment(step, currency)
    except MoneyError:
        return
    expected = _reference_round(Fraction(value), Fraction(step), mode)
    assert Fraction(quantize_amount(value, currency, mode, None if increment is None else step)) == expected


def test_exact_product_never_rounds() -> None:
    product = money.exact_product(D("123456789012345.6789"), D("83.912345678901"))
    assert product == D("10359548756011097.3843865142508889")
    assert len(product.as_tuple().digits) > 28
    with pytest.raises(MoneyError) as excinfo:
        money.exact_product(1.5, D("1"))  # type: ignore[arg-type]
    assert code_of(excinfo) == "FLOAT_NOT_ALLOWED"


def test_quantize_exact_rounds_wide_values_once() -> None:
    """...67499999999995 rounds down; a 28-digit intermediate (...675) would have rounded up."""
    assert money.quantize_exact(D("101338462233914.67499999999995"), "INR", "HALF_UP") == D("101338462233914.67")
    assert money.quantize_exact(D("101338462233914.67499999999995"), "INR", "HALF_UP", D("1")) == D("101338462233915.00")
    assert money.quantize_exact(D("0.12345678901234567890123456789012345"), "KWD", "HALF_EVEN") == D("0.123")
    with pytest.raises(MoneyError) as excinfo:
        money.quantize_exact(D("1" * 40), "INR")
    assert code_of(excinfo) == "PRECISION_EXCEEDED"
    with pytest.raises(MoneyError) as excinfo:
        money.quantize_exact(D("NaN"), "INR")
    assert code_of(excinfo) == "INVALID_DECIMAL"


def test_increment_tie_decided_exactly_beyond_28_digits() -> None:
    """A remainder just below half an increment never becomes a tie through intermediate rounding."""
    below_half = D("10.0249999999999999999999999999999")  # 33 digits; 0.025 is half of 0.05
    assert money.quantize_exact(below_half, "CHF", "HALF_UP", D("0.05")) == D("10.00")
    assert money.quantize_exact(below_half, "CHF", "HALF_UP") == D("10.02")
    assert money.quantize_exact(D("10.025"), "CHF", "HALF_UP", D("0.05")) == D("10.05")
    assert money.quantize_exact(D("10.025"), "CHF", "HALF_EVEN", D("0.05")) == D("10.00")


def test_ec_mon_17_allocation_share_is_the_exact_floor() -> None:
    """33.3333333333333333333333333 % of 999999999999999.99 INR is 333333333333333.329999...; the share is .32."""
    allocation = allocate_percentages(Money.of("999999999999999.99", "INR"), [D("33.3333333333333333333333333")])
    assert allocation.shares[0].to_json()["amount"] == "333333333333333.32"
    assert allocation.shares[0] + allocation.remainder == Money.of("999999999999999.99", "INR")

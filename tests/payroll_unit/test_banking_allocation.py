"""Split-payment allocation (3.7 §5), multi-currency flags (3.2 §4.4) and cash rounding (3.7 §7.3, 3.2 §2.2)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.payroll.banking.allocation import Allocation, allocate_net, cash_payment, validate_allocations
from app.payroll.banking.errors import AllocationError

D = Decimal


def _codes(issues: tuple) -> set[str]:
    return {issue.code for issue in issues}


def test_single_primary_account_receives_full_net() -> None:
    result = allocate_net(D("46666.67"), "INR", [{"account_id": 1, "type": "PRIMARY"}])
    assert result.outcome == "ALLOCATED"
    (line,) = result.lines
    assert line.amount == D("46666.67") and line.explanation == "Full net pay 46666.67 INR"
    assert result.payable_lines == result.lines


def test_doc_3_2_section_4_1_seventy_aed_thirty_inr() -> None:
    """3.2 §4.1: 70% AED to a UAE account, 30% INR to an Indian account; the remainder goes to the primary account (§4.4)."""
    result = allocate_net(
        D("10000.00"),
        "AED",
        [
            Allocation(account_id=2, type="PERCENT", value=D("30"), order=1, currency="INR"),
            Allocation(account_id=1, type="REMAINDER", order=2, currency="AED"),
        ],
    )
    inr, aed = result.lines
    assert (inr.account_id, inr.amount, inr.currency, inr.account_currency, inr.requires_fx) == (2, D("3000.00"), "AED", "INR", True)
    assert (aed.account_id, aed.amount, aed.requires_fx) == (1, D("7000.00"), False)
    assert result.total == D("10000.00")


def test_fixed_then_percent_then_remainder_in_order() -> None:
    result = allocate_net(
        D("50000.00"),
        "INR",
        [
            {"account_id": "c", "type": "REMAINDER", "order": 3},
            {"account_id": "a", "type": "FIXED", "value": "5000", "order": 1},
            {"account_id": "b", "type": "PERCENT", "value": "10", "order": 2},
        ],
    )
    assert [(line.account_id, line.amount) for line in result.lines] == [("a", D("5000.00")), ("b", D("5000.00")), ("c", D("40000.00"))]
    assert not result.issues


def test_rounding_difference_goes_to_remainder() -> None:
    result = allocate_net(
        D("1000.01"),
        "USD",
        [
            {"account_id": 1, "type": "PERCENT", "value": "33.333333", "order": 1},
            {"account_id": 2, "type": "PERCENT", "value": "33.333333", "order": 2},
            {"account_id": 3, "type": "REMAINDER", "order": 3},
        ],
    )
    assert [line.amount for line in result.lines] == [D("333.33"), D("333.33"), D("333.35")]
    assert result.total == D("1000.01")


def test_ec_mon_17_remainder_rounding_with_foreign_account() -> None:
    result = allocate_net(
        D("1500"),
        "JPY",
        [{"account_id": 1, "type": "PERCENT", "value": "33.5", "order": 1, "currency": "USD"}, {"account_id": 2, "type": "REMAINDER", "order": 2}],
    )
    assert [line.amount for line in result.lines] == [D("502"), D("998")]
    assert result.lines[0].requires_fx and not result.lines[1].requires_fx


def test_ec_pay_17_foreign_currency_line_flagged_for_conversion() -> None:
    result = allocate_net(D("100.00"), "USD", [{"account_id": 1, "type": "PRIMARY", "currency": "EUR"}])
    assert result.lines[0].requires_fx and result.lines[0].currency == "USD" and result.lines[0].account_currency == "EUR"
    same = allocate_net(D("100.00"), "USD", [{"account_id": 1, "type": "PRIMARY", "currency": "USD"}])
    assert not same.lines[0].requires_fx


def test_kwd_three_decimals() -> None:
    result = allocate_net(D("1000.005"), "KWD", [{"account_id": 1, "type": "PERCENT", "value": "50", "order": 1}, {"account_id": 2, "type": "REMAINDER", "order": 2}])
    assert [line.amount for line in result.lines] == [D("500.002"), D("500.003")]


# --------------------------------------------------------------------------- EC-BNK-09


def test_ec_bnk_09_fixed_amounts_exceed_net_filled_in_order() -> None:
    result = allocate_net(
        D("1000.00"),
        "INR",
        [
            {"account_id": 1, "type": "FIXED", "value": "600", "order": 1},
            {"account_id": 2, "type": "FIXED", "value": "700", "order": 2},
            {"account_id": 3, "type": "REMAINDER", "order": 3},
        ],
    )
    assert result.outcome == "ALLOCATED"
    assert [line.amount for line in result.lines] == [D("600.00"), D("400.00"), D("0.00")]
    assert result.lines[1].requested == D("700.00")
    (issue,) = result.issues
    assert (issue.code, issue.severity, issue.account_ids) == ("FIXED_EXCEEDS_NET", "EXCEPTION", (2,))
    assert [line.account_id for line in result.payable_lines] == [1, 2]
    assert result.total == D("1000.00")


def test_ec_bnk_09_fixed_exactly_equal_to_net_has_no_exception() -> None:
    result = allocate_net(D("1000.00"), "INR", [{"account_id": 1, "type": "FIXED", "value": "1000", "order": 1}, {"account_id": 2, "type": "REMAINDER", "order": 2}])
    assert [line.amount for line in result.lines] == [D("1000.00"), D("0.00")]
    assert result.issues == ()


def test_ec_bnk_09_percent_after_fixed_exhausts_net() -> None:
    result = allocate_net(
        D("100.00"),
        "USD",
        [
            {"account_id": 1, "type": "FIXED", "value": "90", "order": 1},
            {"account_id": 2, "type": "PERCENT", "value": "20", "order": 2},
            {"account_id": 3, "type": "REMAINDER", "order": 3},
        ],
    )
    assert [line.amount for line in result.lines] == [D("90.00"), D("10.00"), D("0.00")]
    assert _codes(result.issues) == {"ALLOCATIONS_EXCEED_NET"}


def test_ec_bnk_09_invalid_rules_rejected_on_save() -> None:
    assert _codes(validate_allocations([{"account_id": 1, "type": "PERCENT", "value": "60", "order": 1}, {"account_id": 2, "type": "PERCENT", "value": "50", "order": 2}, {"account_id": 3, "type": "REMAINDER", "order": 3}])) == {"PERCENT_OVER_100"}
    assert _codes(validate_allocations([{"account_id": 1, "type": "FIXED", "value": "10", "order": 1}])) == {"REMAINDER_REQUIRED"}
    assert "REMAINDER_NOT_UNIQUE" in _codes(validate_allocations([{"account_id": 1, "type": "REMAINDER", "order": 1}, {"account_id": 2, "type": "REMAINDER", "order": 2}]))
    assert "PERCENT_RANGE" in _codes(validate_allocations([{"account_id": 1, "type": "PERCENT", "value": "101", "order": 1}, {"account_id": 2, "type": "REMAINDER", "order": 2}]))
    assert "FIXED_NOT_POSITIVE" in _codes(validate_allocations([{"account_id": 1, "type": "FIXED", "value": "0", "order": 1}, {"account_id": 2, "type": "REMAINDER", "order": 2}]))
    assert "VALUE_NOT_ALLOWED" in _codes(validate_allocations([{"account_id": 1, "type": "REMAINDER", "value": "5", "order": 1}]))
    assert "DUPLICATE_ACCOUNT" in _codes(validate_allocations([{"account_id": 1, "type": "FIXED", "value": "5", "order": 1}, {"account_id": 1, "type": "REMAINDER", "order": 2}]))
    assert "DUPLICATE_ORDER" in _codes(validate_allocations([{"account_id": 1, "type": "FIXED", "value": "5", "order": 1}, {"account_id": 2, "type": "REMAINDER", "order": 1}]))
    assert "TYPE_INVALID" in _codes(validate_allocations([{"account_id": 1, "type": "HALF", "order": 1}]))
    assert "NO_ACCOUNTS" in _codes(validate_allocations([]))


def test_ec_bnk_09_valid_split_and_exactly_100_percent_accepted() -> None:
    rules = [{"account_id": 1, "type": "PERCENT", "value": "100", "order": 1}, {"account_id": 2, "type": "REMAINDER", "order": 2}]
    assert validate_allocations(rules, max_accounts=2) == ()
    assert [line.amount for line in allocate_net(D("10.00"), "USD", rules).lines] == [D("10.00"), D("0.00")]


def test_max_accounts_and_split_disabled() -> None:
    rules = [{"account_id": 1, "type": "FIXED", "value": "5", "order": 1}, {"account_id": 2, "type": "REMAINDER", "order": 2}]
    issues = validate_allocations(rules, max_accounts=1)
    assert _codes(issues) == {"TOO_MANY_ACCOUNTS"} and "not enabled" in issues[0].message
    result = allocate_net(D("100.00"), "USD", rules, max_accounts=1)
    assert result.outcome == "INVALID_RULES" and result.lines == ()


def test_fixed_precision_checked_against_pay_currency() -> None:
    rules = [{"account_id": 1, "type": "FIXED", "value": "10.5", "order": 1}, {"account_id": 2, "type": "REMAINDER", "order": 2}]
    assert _codes(validate_allocations(rules, pay_currency="JPY")) == {"FIXED_PRECISION"}
    assert validate_allocations(rules, pay_currency="USD") == ()


# --------------------------------------------------------------------------- EC-BNK-14


def test_ec_bnk_14_currency_unsupported_by_rail_blocked() -> None:
    rules = [{"account_id": 1, "type": "PERCENT", "value": "20", "order": 1, "currency": "GBP"}, {"account_id": 2, "type": "REMAINDER", "order": 2}]
    assert _codes(validate_allocations(rules, supported_currencies={"INR"}, pay_currency="INR")) == {"CURRENCY_NOT_SUPPORTED"}
    assert validate_allocations(rules, supported_currencies={"INR", "GBP"}, pay_currency="INR") == ()


def test_ec_bnk_14_fund_or_unknown_account_currency_not_payable() -> None:
    rules = [{"account_id": 1, "type": "PRIMARY", "currency": "CLF"}]
    assert _codes(validate_allocations(rules)) == {"CURRENCY_NOT_PAYABLE"}
    assert _codes(validate_allocations([{"account_id": 1, "type": "PRIMARY", "currency": "XYZ"}])) == {"CURRENCY_NOT_PAYABLE"}


# --------------------------------------------------------------------------- EC-PAY-15 / EC-PAY-16


def test_ec_pay_15_zero_net_creates_no_lines() -> None:
    result = allocate_net(D("0.00"), "INR", [{"account_id": 1, "type": "PRIMARY"}])
    assert (result.outcome, result.lines, result.payable_lines) == ("ZERO_NET", (), ())


def test_ec_pay_15_smallest_positive_net_is_paid() -> None:
    result = allocate_net(D("0.01"), "INR", [{"account_id": 1, "type": "PRIMARY"}])
    assert result.outcome == "ALLOCATED" and result.lines[0].amount == D("0.01")


def test_ec_pay_16_negative_net_creates_no_lines() -> None:
    result = allocate_net(D("-125.50"), "INR", [{"account_id": 1, "type": "PRIMARY"}])
    assert (result.outcome, result.net, result.lines) == ("NEGATIVE_NET", D("-125.50"), ())


def test_ec_pay_16_negative_net_not_hidden_even_with_invalid_rules() -> None:
    assert allocate_net(D("-1.00"), "INR", []).outcome == "NEGATIVE_NET"


# --------------------------------------------------------------------------- argument errors


@pytest.mark.parametrize(
    "net,currency,code",
    [(1000.0, "INR", "INVALID_AMOUNT"), (D("10.001"), "INR", "NET_PRECISION"), (D("10"), "CLF", "CURRENCY_NOT_PAYABLE"), (D("10"), "inr1", "CURRENCY_INVALID"), (D("1e16"), "USD", "AMOUNT_OUT_OF_RANGE")],
)
def test_bad_arguments_raise_typed_errors(net: object, currency: str, code: str) -> None:
    with pytest.raises(AllocationError) as exc:
        allocate_net(net, currency, [{"account_id": 1, "type": "PRIMARY"}])  # type: ignore[arg-type]
    assert exc.value.code == code


def test_float_allocation_value_rejected() -> None:
    with pytest.raises(AllocationError) as exc:
        Allocation(account_id=1, type="PERCENT", value=12.5)  # type: ignore[arg-type]
    assert exc.value.code == "INVALID_AMOUNT"
    with pytest.raises(AllocationError):
        Allocation.from_mapping({"account_id": 1, "type": "PRIMARY", "colour": "red"})


@settings(max_examples=200, deadline=None)
@given(
    net=st.integers(min_value=1, max_value=10**10).map(lambda cents: D(cents).scaleb(-2)),
    fixed=st.lists(st.integers(min_value=1, max_value=10**9).map(lambda c: D(c).scaleb(-2)), max_size=2),
    percents=st.lists(st.integers(min_value=1, max_value=4000).map(lambda p: D(p).scaleb(-2)), max_size=2),
)
def test_lines_always_sum_to_net_and_are_non_negative(net: Decimal, fixed: list[Decimal], percents: list[Decimal]) -> None:
    rules: list[dict[str, object]] = []
    order = 0
    for value in fixed:
        order += 1
        rules.append({"account_id": order, "type": "FIXED", "value": value, "order": order})
    for value in percents:
        order += 1
        rules.append({"account_id": order, "type": "PERCENT", "value": value, "order": order})
    rules.append({"account_id": 99, "type": "REMAINDER", "order": 99})
    result = allocate_net(net, "USD", rules)
    assert result.outcome == "ALLOCATED"
    assert result.total == net
    assert all(line.amount >= 0 and line.amount == line.amount.quantize(D("0.01")) for line in result.lines)


# --------------------------------------------------------------------------- cash rounding (3.2 §2.2)


def test_cash_rounding_to_currency_increment() -> None:
    chf = cash_payment(D("1234.57"), "CHF")
    assert (chf.amount, chf.rounding_difference, chf.increment) == (D("1234.55"), D("-0.02"), D("0.05"))
    up = cash_payment(D("1234.58"), "CHF")
    assert (up.amount, up.rounding_difference) == (D("1234.60"), D("0.02"))
    assert cash_payment(D("1234.57"), "CHF", mode="DOWN").amount == D("1234.55")


def test_cash_rounding_without_increment_uses_minor_unit() -> None:
    inr = cash_payment(D("1234.57"), "INR")
    assert inr.amount == D("1234.57") and inr.rounding_difference == D("0.00")


def test_cash_payment_rejects_bad_input() -> None:
    with pytest.raises(AllocationError):
        cash_payment(D("0"), "CHF")
    with pytest.raises(AllocationError):
        cash_payment(D("10.00"), "CHF", mode="BANKERS")

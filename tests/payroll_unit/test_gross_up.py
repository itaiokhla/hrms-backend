"""Net-to-gross solver (3.3 §6.3): bisection over minor units, 50 evaluations, one-unit tolerance (EC-CMP-15)."""

from datetime import date
from decimal import Decimal

import pytest

from app.payroll.engine import statutory
from app.payroll.engine.gross_up import MAX_ITERATIONS, solve_gross
from app.payroll.money import MoneyError, quantize_amount
from app.payroll.packs import pack_snapshot, zz_test
from app.payroll.packs.base import earning

D = Decimal


def flat_tax(rate):
    return lambda gross: gross - quantize_amount(gross * rate, "INR", "HALF_UP", D(1))


def test_converges_to_the_smallest_gross_reaching_the_target():
    result = solve_gross(D("80000.00"), flat_tax(D("0.2")), "INR")
    assert result.converged and result.gross == D("100000.00") and result.net == D("80000.00")
    assert result.issue is None
    assert len(result.iterations) <= MAX_ITERATIONS
    assert result.trace["method"] == "NET_TO_GROSS" and result.trace["gross"] == "100000.00"
    assert result.trace["iterations"][0] == {"n": "1", "gross": "0.00", "net": "0.00"}


def test_whole_unit_steps_are_accepted_within_one_minor_unit():
    # net moves in steps of 0.80 per rupee of gross; 80,000.50 is reached at 1,00,000.63 (net 80,000.51)
    result = solve_gross(D("80000.50"), flat_tax(D("0.2")), "INR")
    assert result.converged and abs(result.net - D("80000.50")) <= D("0.01")


def test_ec_cmp_15_not_converged_is_a_blocking_issue_with_the_trace():
    stepped = lambda gross: quantize_amount(gross * D("0.8"), "INR", "DOWN", D(100))
    result = solve_gross(D("80050.00"), stepped, "INR", component="BONUS")
    assert not result.converged and result.gross is None
    issue = result.issue
    assert (issue.code, issue.severity, issue.component) == ("NOT_CONVERGED", "BLOCKING", "BONUS")
    assert "no payment" in issue.message and issue.details["evaluations"] == len(result.iterations)
    assert result.trace["converged"] is False and result.trace["iterations"]


def test_iteration_limit_is_enforced():
    result = solve_gross(D("80000.00"), flat_tax(D("0.2")), "INR", max_iterations=3)
    assert not result.converged and len(result.iterations) == 3 and result.issue.details["reason"] == "iteration limit"
    with pytest.raises(ValueError):
        solve_gross(D("1.00"), flat_tax(D("0")), "INR", max_iterations=51)


def test_bounds_and_inputs_are_checked():
    result = solve_gross(D("80000.00"), flat_tax(D("0.2")), "INR", upper=D("90000.00"))
    assert not result.converged and "upper bound" in result.issue.details["reason"]
    assert solve_gross(D("0.00"), flat_tax(D("0.2")), "INR").gross == D("0.00")
    above = solve_gross(D("10.00"), lambda gross: gross + D("100"), "INR")
    assert not above.converged and "lower bound" in above.issue.details["reason"]
    with pytest.raises(MoneyError):
        solve_gross(D("10.001"), flat_tax(D("0.2")), "INR")
    with pytest.raises(MoneyError):
        solve_gross(10.5, flat_tax(D("0.2")), "INR")
    with pytest.raises(ValueError):
        solve_gross(D("10.00"), flat_tax(D("0.2")), "INR", lower=D("5"), upper=D("1"))


def test_deterministic_and_respects_currency_minor_units():
    first = solve_gross(D("50000"), lambda g: g - quantize_amount(g * D("0.1"), "JPY"), "JPY")
    second = solve_gross(D("50000"), lambda g: g - quantize_amount(g * D("0.1"), "JPY"), "JPY")
    assert first == second and first.gross == D("55556") and first.trace["tolerance"] == "1"
    kwd = solve_gross(D("100.000"), lambda g: g, "KWD")
    assert kwd.gross == D("100.000") and kwd.trace["tolerance"] == "0.001"


def test_gross_up_a_bonus_net_of_zz_withholding():
    """'Pay 1,000.00 net of tax' through the real ZZ statutory stage."""

    class Ctx:
        currency, environment, attestation, preview, country = "EUR", "test", None, False, None
        period_start, period_end, frequency = date(2026, 3, 1), date(2026, 3, 31), "MONTHLY"
        settings = {"payroll.statutory.ZZ.pension.enabled": True, "payroll.statutory.ZZ.wht.enabled": True}
        pack, ytd, tax_profile, employee, pack_state = pack_snapshot(zz_test.PACK), {}, {"resident": True}, {}, {}

    base = [earning("BASIC", "2000.00", "EUR", ("ZZ_PENSION_WAGE", "TAXABLE_INCOME"))]

    def net(lines):
        result, _ = statutory.apply(Ctx(), lines)
        return sum(l.amount for l in result if l.kind == "EARNING") - sum(l.amount for l in result if l.kind == "DEDUCTION")

    before = net(base)
    extra = lambda bonus: net([*base, earning("BONUS", str(bonus), "EUR", ("TAXABLE_INCOME",), category="BONUS")]) - before
    result = solve_gross(D("1000.00"), extra, "EUR", component="BONUS")
    # 2,000 basic: pension 100, taxable 1,900 at 10% = 190, net 1,710. A bonus B above 1,100 moves the whole
    # taxable 1,900 + B into the 20% band: net = 2,000 + B − 100 − 20% × (1,900 + B) = 1,520 + 0.8 B;
    # + 1,000.00 needs 0.8 B − 190 = 1,000, B = 1,487.50
    assert result.converged and result.gross == D("1487.50") and extra(result.gross) == D("1000.00")

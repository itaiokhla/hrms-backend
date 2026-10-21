"""Stages 9-11: net pay with net rounding, employer cost, and the payment split (3.5 §6-§7, 3.7 §5).

Totals are sums of rounded lines (EC-MON-10), always in the pay currency::

    gross          Σ EARNING                      (cash; no benefits in kind, reimbursements or employer items)
    deductions     Σ DEDUCTION                    (pre-tax and post-tax)
    net            gross + Σ REIMBURSEMENT − deductions + Σ ROUNDING
    employer cost  gross + Σ EMPLOYER_CONTRIBUTION (contributions and employer-only benefits)

Benefits in kind and employer contributions never change net. Net pay is rounded
to ``payroll.rounding.net_pay_increment`` with ``net_pay_mode``; the difference is
a ``NET_ROUNDING`` line (``ROUNDING_LINE``) or, with ``CARRY_FORWARD``, the same
line marked as carried so the next period adds it back through a ROUNDING-kind
wage input (EC-MON-11). A negative net is never clipped or rounded: it stays
negative and validation raises ``NEGATIVE_NET`` (EC-RUN-10). The payment split
uses ``banking.allocation.allocate_net`` on the rounded net.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any

from app.payroll.banking.allocation import Allocation, allocate_net
from app.payroll.banking.errors import AllocationError
from app.payroll.engine.context import CalcContext
from app.payroll.engine.earnings import blocking, component_or_default, make_line, warning
from app.payroll.engine.lines import STAGE_NET, CalcIssue, Line
from app.payroll.engine.trace import money_json, money_text, rounding_trace
from app.payroll.money import CALC_CONTEXT, NET_PAY_ROUNDING_MODES, Money, MoneyError, amount_str, round_net

ZERO = Decimal(0)
NET_ROUNDING = "NET_ROUNDING"


@dataclass(frozen=True)
class Totals:
    """The employee's figures for the period, in the pay currency (3.5 §6)."""

    currency: str
    gross: Decimal
    reimbursements: Decimal
    deductions: Decimal
    employer_contributions: Decimal
    employer_benefits: Decimal
    benefits_in_kind: Decimal
    net_rounding: Decimal
    net: Decimal
    employer_cost: Decimal
    rounding_carried: Decimal = ZERO

    @property
    def net_before_rounding(self) -> Decimal:
        return self.net - self.net_rounding

    def to_json(self) -> dict[str, Any]:
        def text(value: Decimal) -> str:
            return amount_str(value, self.currency)

        return {
            "currency": self.currency, "gross": text(self.gross), "reimbursements": text(self.reimbursements),
            "deductions": text(self.deductions), "employer_contributions": text(self.employer_contributions),
            "employer_benefits": text(self.employer_benefits), "benefits_in_kind": text(self.benefits_in_kind),
            "net_rounding": text(self.net_rounding), "net": text(self.net), "employer_cost": text(self.employer_cost),
            "rounding_carried": text(self.rounding_carried),
        }


def _sum(lines: Iterable[Line], kind: str, category: str | None = None) -> Decimal:
    with localcontext(CALC_CONTEXT):
        return sum((line.amount for line in lines if line.kind == kind and (category is None or line.category == category)),
                   ZERO)


def totals(lines: Sequence[Line], currency: str, rounding_carried: Decimal = ZERO) -> Totals:
    """Totals recomputed from the lines; validation compares them with the invariants."""
    with localcontext(CALC_CONTEXT):
        gross = _sum(lines, "EARNING")
        reimbursements = _sum(lines, "REIMBURSEMENT")
        deductions = _sum(lines, "DEDUCTION")
        employer = _sum(lines, "EMPLOYER_CONTRIBUTION")
        employer_benefits = _sum(lines, "EMPLOYER_CONTRIBUTION", "BENEFIT")
        rounding = _sum(lines, "ROUNDING")
        return Totals(currency, gross, reimbursements, deductions, employer - employer_benefits, employer_benefits,
                      _sum(lines, "BENEFIT_IN_KIND"), rounding, gross + reimbursements - deductions + rounding,
                      gross + employer, rounding_carried)


def carried_rounding_lines(ctx: CalcContext) -> list[Line]:
    """Rounding differences carried from earlier periods: wage inputs of ROUNDING components (EC-MON-11)."""
    lines = []
    for item in ctx.snapshot.wage_inputs:
        component = ctx.component(item.component)
        if component is None or component.kind != "ROUNDING" or item.amount is None:
            continue
        lines.append(make_line(
            ctx, component, item.amount, stage=STAGE_NET,
            explanation=f"Rounding carried from an earlier period: {money_text(item.amount, ctx.minor_unit)}",
            trace={"component": component.code, "kind": "ROUNDING", "stage": STAGE_NET, "method": "INPUT",
                   "unrounded": format(item.amount, "f"), "amount": money_json(item.amount, ctx.currency)},
            sources=[{"type": "wage_input", "id": item.id}]))
    return lines


def net_rounding(ctx: CalcContext, lines: Sequence[Line]) -> tuple[list[Line], Decimal, list[CalcIssue]]:
    """Stage 9: the NET_ROUNDING line (if any), the amount carried forward, and configuration issues."""
    issues: list[CalcIssue] = []
    current = totals(lines, ctx.currency)
    if current.net <= 0:
        return [], ZERO, issues
    increment = ctx.setting_decimal("payroll.rounding.net_pay_increment")
    mode = ctx.setting("payroll.rounding.net_pay_mode", "HALF_UP")
    handling = ctx.setting("payroll.rounding.difference_handling", "ROUNDING_LINE")
    if increment is None or increment == ctx.quantum:
        return [], ZERO, issues
    if mode not in NET_PAY_ROUNDING_MODES:
        issues.append(blocking("CONFIG_ERROR", f"payroll.rounding.net_pay_mode {mode!r} is not a net rounding mode."))
        return [], ZERO, issues
    try:
        result = round_net(Money.of(current.net, ctx.currency), increment, mode)
    except MoneyError as error:
        issues.append(blocking("CONFIG_ERROR", f"Net pay cannot be rounded: {error}",
                               setting="payroll.rounding.net_pay_increment"))
        return [], ZERO, issues
    difference = result.line_amount.amount
    if difference == 0:
        return [], ZERO, issues
    component = component_or_default(ctx, NET_ROUNDING, "ROUNDING", "ROUNDING")
    carried = handling == "CARRY_FORWARD"
    sign = "+" if difference > 0 else "−"
    text = (f"Net pay {money_text(current.net, ctx.minor_unit)} rounded to {money_text(result.rounded.amount, ctx.minor_unit)}"
            f" (nearest {money_text(increment, ctx.minor_unit)}, {mode}): {sign}{money_text(abs(difference), ctx.minor_unit)}")
    if carried:
        text += "; the difference is carried to the next period"
    line = make_line(
        ctx, component, difference, stage=STAGE_NET, kind="ROUNDING", category="ROUNDING", explanation=text,
        trace={"component": NET_ROUNDING, "kind": "ROUNDING", "stage": STAGE_NET, "method": "SYSTEM",
               "unrounded": format(current.net, "f"), "rounding": rounding_trace("NET", mode, increment),
               "amount": money_json(difference, ctx.currency), "difference_handling": handling,
               "carry_forward": money_json(-difference, ctx.currency) if carried else None})
    return [line], (-difference if carried else ZERO), issues


@dataclass(frozen=True)
class PaymentLine:
    account_id: int | str
    type: str
    order: int
    amount: Decimal
    currency: str
    account_currency: str
    requires_fx: bool
    on_hold: bool
    explanation: str

    def to_json(self) -> dict[str, Any]:
        return {"account_id": self.account_id, "type": self.type, "order": self.order,
                "amount": format(self.amount, "f"), "currency": self.currency, "account_currency": self.account_currency,
                "requires_fx": self.requires_fx, "on_hold": self.on_hold, "explanation": self.explanation}


def payment_split(ctx: CalcContext, net: Decimal) -> tuple[tuple[PaymentLine, ...], list[CalcIssue]]:
    """Stage 11: the net split across the payee's accounts (3.7 §5); held accounts stay in the split, on hold."""
    issues: list[CalcIssue] = []
    accounts = ctx.snapshot.bank_accounts
    if net <= 0:
        return (), issues
    if not accounts:
        issues.append(warning("NO_BANK_ACCOUNT", "No bank account: net pay cannot be paid by bank transfer until one "
                              "is added and verified."))
        return (), issues
    split = ctx.setting_bool("payroll.bank.split_payments_enabled")
    max_accounts = int(ctx.setting("payroll.bank.max_accounts_per_payee", 2)) if split else 1
    hold_unverified = ctx.setting_bool("payroll.bank.exclude_unverified_from_payout")
    rules = [Allocation(a.account_id, a.type, a.value, a.order, a.currency) for a in accounts]
    try:
        result = allocate_net(net, ctx.currency, rules, max_accounts=max_accounts)
    except AllocationError as error:
        issues.append(blocking("PAYMENT_SPLIT_INVALID", f"Net pay cannot be split: {error}"))
        return (), issues
    for item in result.issues:
        maker = blocking if item.severity == "ERROR" else warning
        issues.append(maker("PAYMENT_SPLIT_INVALID" if item.severity == "ERROR" else "PAYMENT_SPLIT_EXCEPTION",
                            item.message, rule=item.code, accounts=list(item.account_ids)))
    verified: Mapping[Any, bool] = {a.account_id: a.verified for a in accounts}
    lines = []
    for line in result.lines:
        on_hold = hold_unverified and not verified.get(line.account_id, False)
        explanation = line.explanation + ("; on hold: the account is not verified" if on_hold else "")
        lines.append(PaymentLine(line.account_id, line.type, line.order, line.amount, line.currency,
                                 line.account_currency, line.requires_fx, on_hold, explanation))
    held = [line.account_id for line in lines if line.on_hold and line.amount > 0]
    if held:
        issues.append(warning("UNVERIFIED_ACCOUNT_ON_HOLD", "Payment to an unverified account is held and left out of "
                              "the bank file until the account is verified.", accounts=held))
    return tuple(lines), issues


__all__ = ["NET_ROUNDING", "PaymentLine", "Totals", "carried_rounding_lines", "net_rounding", "payment_split", "totals"]

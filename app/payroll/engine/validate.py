"""Stage 12: invariants, negative net, variance, outliers and review flags (3.5 §6, §10).

Invariant failures are bugs: BLOCKING, never waivable (EC-STA-10, EC-MON-10)::

    net = gross + reimbursements − deductions + net_rounding
    employer_cost = gross + employer_contributions + employer_only_benefits
    Σ payment allocations = net                       (when net > 0)
    every line in the pay currency, rounded to its minor unit; totals are sums of lines

A negative net is BLOCKING ``NEGATIVE_NET`` and is never clipped (EC-RUN-10). The
rest are review items: a net change above
``payroll.approval.variance_review_threshold_percent`` must be acknowledged
(WARNING, waivable); joiners, leavers, a first payment to a new account, a changed
payment currency, a component above 3× its trailing six-period median, a bonus
without an approved input and two inputs with the same amount are FLAGs.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, localcontext

from app.payroll.engine.context import CalcContext
from app.payroll.engine.lines import KINDS, SEVERITIES, CalcIssue, Line
from app.payroll.engine.net import PaymentLine, Totals, totals
from app.payroll.money import CALC_CONTEXT

ZERO = Decimal(0)
HUNDRED = Decimal(100)
OUTLIER_FACTOR = Decimal(3)
OUTLIER_WINDOW = 6


def _issue(code: str, severity: str, message: str, *, component: str | None = None, waivable: bool = False,
           **details: object) -> CalcIssue:
    assert severity in SEVERITIES
    return CalcIssue(code, severity, message, {k: _plain(v) for k, v in details.items()}, component, waivable)


def _plain(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def invariants(ctx: CalcContext, lines: Sequence[Line], figures: Totals,
               payments: Sequence[PaymentLine]) -> list[CalcIssue]:
    found: list[CalcIssue] = []

    def broken(rule: str, message: str, **details: object) -> None:
        found.append(_issue("INVARIANT_VIOLATION", "BLOCKING", f"Invariant {rule} does not hold: {message}",
                            rule=rule, **details))

    with localcontext(CALC_CONTEXT):
        for line in lines:
            if line.currency != ctx.currency:
                broken("SINGLE_CURRENCY", f"{line.component} is in {line.currency}, not {ctx.currency}",
                       component=line.component)
            if line.kind not in KINDS:
                broken("KNOWN_KIND", f"{line.component} has kind {line.kind}", component=line.component)
            if line.amount != line.amount.quantize(ctx.quantum):
                broken("ROUNDED_LINES", f"{line.component} {line.amount} has more digits than {ctx.currency} allows",
                       component=line.component)
        recomputed = totals(lines, ctx.currency, figures.rounding_carried)
        if recomputed != figures:
            broken("TOTALS_ARE_SUMS", "the totals differ from the sum of the lines",
                   expected=str(recomputed.to_json()), actual=str(figures.to_json()))
        expected_net = figures.gross + figures.reimbursements - figures.deductions + figures.net_rounding
        if figures.net != expected_net:
            broken("NET", f"net {figures.net} ≠ gross + reimbursements − deductions + rounding {expected_net}")
        expected_cost = figures.gross + figures.employer_contributions + figures.employer_benefits
        if figures.employer_cost != expected_cost:
            broken("EMPLOYER_COST", f"employer cost {figures.employer_cost} ≠ {expected_cost}")
        if figures.net > 0 and payments:
            paid = sum((p.amount for p in payments), ZERO)
            if paid != figures.net:
                broken("PAYMENTS_EQUAL_NET", f"payment allocations {paid} ≠ net {figures.net}")
        if figures.net <= 0 and any(p.amount for p in payments):
            broken("PAYMENTS_EQUAL_NET", "a payment is allocated although net pay is not positive")
    return found


def negative_net(figures: Totals, existing: Sequence[CalcIssue]) -> list[CalcIssue]:
    if figures.net >= 0 or any(issue.code == "NEGATIVE_NET" for issue in existing):
        return []
    return [_issue("NEGATIVE_NET", "BLOCKING",
                   f"Net pay is negative ({format(figures.net, 'f')} {figures.currency}). It is never clipped to zero: "
                   "defer deductions, carry the shortfall forward as a recovery, or put the employee on hold.",
                   net=figures.net, gross=figures.gross, deductions=figures.deductions)]


def review_flags(ctx: CalcContext, lines: Sequence[Line], figures: Totals,
                 payments: Sequence[PaymentLine]) -> list[CalcIssue]:
    found: list[CalcIssue] = []
    period, employee, previous = ctx.period, ctx.employee, ctx.previous
    threshold = ctx.setting_decimal("payroll.approval.variance_review_threshold_percent")
    if previous.net is not None and previous.currency in (None, ctx.currency) and threshold is not None:
        with localcontext(CALC_CONTEXT):
            if previous.net == 0:
                change = None if figures.net == 0 else HUNDRED
            else:
                change = abs(figures.net - previous.net) / abs(previous.net) * HUNDRED
        if change is not None and change > threshold:
            found.append(_issue(
                "NET_VARIANCE", "WARNING",
                f"Net pay changed by {change.quantize(Decimal('0.1'))}% against {previous.period_ref or 'the last period'} "
                f"(above the {threshold}% review threshold); acknowledge before approval.",
                waivable=True, previous_net=previous.net, net=figures.net, change_percent=change.quantize(Decimal("0.01"))))
    if period.start <= employee.hire_date <= period.end:
        found.append(_issue("NEW_JOINER", "FLAG", f"Joined on {employee.hire_date.isoformat()} during the period.",
                            hire_date=employee.hire_date.isoformat()))
    if employee.exit_date is not None and period.start <= employee.exit_date <= period.end:
        found.append(_issue("LEAVER", "FLAG", f"Leaves on {employee.exit_date.isoformat()}; final settlement follows.",
                            exit_date=employee.exit_date.isoformat()))
    known = set(map(str, previous.bank_account_ids))
    for account in ctx.snapshot.bank_accounts:
        paid = any(p.account_id == account.account_id and p.amount > 0 for p in payments)
        if paid and (account.first_payment or (previous.period_ref is not None and str(account.account_id) not in known)):
            found.append(_issue("NEW_BANK_ACCOUNT", "FLAG", "First payment to this bank account.",
                                account_id=account.account_id))
    currencies_now = sorted({p.account_currency for p in payments if p.amount > 0})
    if previous.payment_currency and currencies_now and currencies_now != [previous.payment_currency]:
        found.append(_issue("PAYMENT_CURRENCY_CHANGED", "FLAG",
                            f"Paid in {', '.join(currencies_now)}; last period in {previous.payment_currency}.",
                            previous=previous.payment_currency, current=currencies_now))
    found.extend(_outliers(ctx, lines))
    found.extend(_input_flags(ctx, lines))
    return found


def _outliers(ctx: CalcContext, lines: Sequence[Line]) -> list[CalcIssue]:
    found = []
    by_code: dict[str, Decimal] = {}
    for line in lines:
        if line.kind == "EARNING":
            by_code[line.component] = by_code.get(line.component, ZERO) + line.amount
    for code in sorted(by_code):
        history = [v for v in ctx.previous.history.get(code, ())[:OUTLIER_WINDOW]]
        if len(history) < 3:
            continue
        ordered = sorted(history)
        middle = len(ordered) // 2
        median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
        if median > 0 and by_code[code] > OUTLIER_FACTOR * median:
            found.append(_issue("COMPONENT_OUTLIER", "FLAG",
                                f"{code} is {format(by_code[code], 'f')}, above 3× its trailing median {format(median, 'f')}.",
                                component=code, amount=by_code[code], median=median))
    return found


def _input_flags(ctx: CalcContext, lines: Sequence[Line]) -> list[CalcIssue]:
    found = []
    for line in lines:
        if line.category == "BONUS" and line.kind == "EARNING" and not any(
                s.get("type") == "wage_input" for s in line.sources):
            found.append(_issue("BONUS_WITHOUT_INPUT", "FLAG", f"{line.component} is paid without an approved input.",
                                component=line.component))
    seen: dict[tuple[str, Decimal], list[object]] = {}
    for item in ctx.snapshot.wage_inputs:
        if item.amount is not None:
            seen.setdefault((item.component, item.amount), []).append(item.id)
    for (code, amount), ids in sorted(seen.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if len(ids) > 1:
            found.append(_issue("DUPLICATE_INPUT_AMOUNT", "FLAG",
                                f"{len(ids)} inputs for {code} have the same amount {format(amount, 'f')}.",
                                component=code, inputs=list(ids)))
    return found


def validate(ctx: CalcContext, lines: Sequence[Line], figures: Totals, payments: Sequence[PaymentLine],
             existing: Sequence[CalcIssue]) -> list[CalcIssue]:
    """Every stage-12 issue for one employee."""
    return [*invariants(ctx, lines, figures, payments), *negative_net(figures, existing),
            *review_flags(ctx, lines, figures, payments)]


__all__ = ["invariants", "negative_net", "review_flags", "validate"]

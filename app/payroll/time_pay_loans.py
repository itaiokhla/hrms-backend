"""Loans and salary advances: schedules, idempotent recoveries and the protected net (3.4 §15, 3.1 §5.11).

Schedules are rounded to the currency's minor unit and the last instalment
absorbs every rounding difference, so instalments always add up exactly:

    FLAT       interest = round(P × rate% × months ÷ 1200); each instalment P ÷ n + interest ÷ n
    REDUCING   EMI = round(P × i × (1 + i)^n ÷ ((1 + i)^n − 1)), i = rate% ÷ 1200 (P ÷ n at 0 %);
               interest_k = round(balance × i), principal_k = EMI − interest_k
    EQUAL      advances: amount ÷ runs, no interest, over 1–3 runs

The outstanding balance is derived from the ledger (``DISBURSEMENT``,
``INSTALMENT``, ``PREPAYMENT``, ``WAIVER``, ``ADJUSTMENT``). A run's recovery is
keyed by ``(run_employee_id, loan_id)``: an existing instalment for the same run
employee is replaced, never added, so regenerating a run recovers once
(EC-VAR-08). A paused loan is skipped with no ledger row, so the schedule resumes
at the same instalment one run later (EC-VAR-06). When the instalment would take net pay below the protected floor
(``payroll.deductions.min_net_pay_percent``, never below the pack floor) only the
room is recovered, the rest is carried to the next run and an exception is
returned (EC-VAR-05); with no floor configured automated recovery is blocked.
In the final settlement the whole balance is due within the same limits and any
remainder becomes a receivable (EC-VAR-07, EC-VAR-14); with
``recover_balance_on_exit`` false only the scheduled instalment is deducted and the
rest of the balance is the receivable. After the last scheduled instalment the
whole outstanding balance (arrears, positive adjustments) is due.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, List, Optional, Sequence, Tuple

from app.payroll.time_pay_common import (
    BLOCKING,
    DEDUCTION,
    INFO,
    INVALID_INPUT,
    INVALID_SCHEDULE,
    WARNING,
    Issue,
    TimePayError,
    TimePayLine,
    amount_str,
    in_calc_context,
    make_line,
    policy_bool,
    policy_choice,
    policy_decimal,
    policy_int,
    quantum,
    require_decimal,
    require_int,
    resolve_minor_unit,
    round_money,
    show,
)

INTEREST_METHODS = ("FLAT", "REDUCING")
EQUAL = "EQUAL"
LOAN = "LOAN"
ADVANCE = "ADVANCE"
ACTIVE = "ACTIVE"
PAUSED = "PAUSED"
CLOSED = "CLOSED"
LOAN_STATUSES = (ACTIVE, PAUSED, CLOSED)

DISBURSEMENT = "DISBURSEMENT"
INSTALMENT = "INSTALMENT"
PREPAYMENT = "PREPAYMENT"
WAIVER = "WAIVER"
ADJUSTMENT = "ADJUSTMENT"
TRANSACTION_KINDS = (DISBURSEMENT, INSTALMENT, PREPAYMENT, WAIVER, ADJUSTMENT)

RECOVERY_COMPONENTS = {LOAN: "LOAN_RECOVERY", ADVANCE: "ADVANCE_RECOVERY"}
MAX_ADVANCE_RUNS = 3


# --------------------------------------------------------------------------- schedules


@dataclass(frozen=True)
class Instalment:
    number: int
    principal: Decimal
    interest: Decimal
    total: Decimal
    balance_after: Decimal  # principal still outstanding after this instalment


@dataclass(frozen=True)
class RepaymentSchedule:
    kind: str  # LOAN | ADVANCE
    method: str  # FLAT | REDUCING | EQUAL
    principal: Decimal
    annual_rate_percent: Decimal
    currency: str
    instalments: Tuple[Instalment, ...]
    explanation: str

    @property
    def tenure(self) -> int:
        return len(self.instalments)

    @property
    def total_interest(self) -> Decimal:
        return sum((i.interest for i in self.instalments), Decimal(0))

    @property
    def total_payable(self) -> Decimal:
        return sum((i.total for i in self.instalments), Decimal(0))

    def instalment(self, number: int) -> Optional[Instalment]:
        """Instalment ``number`` (1-based), or ``None`` beyond the tenure."""
        return self.instalments[number - 1] if 1 <= number <= len(self.instalments) else None

    def to_dict(self) -> dict:
        minor = resolve_minor_unit(self.currency)
        return {
            "kind": self.kind, "method": self.method, "currency": self.currency,
            "principal": amount_str(self.principal, minor), "annual_rate_percent": str(self.annual_rate_percent),
            "total_interest": amount_str(self.total_interest, minor),
            "total_payable": amount_str(self.total_payable, minor),
            "instalments": [{"number": i.number, "principal": amount_str(i.principal, minor),
                             "interest": amount_str(i.interest, minor), "total": amount_str(i.total, minor),
                             "balance_after": amount_str(i.balance_after, minor)} for i in self.instalments],
            "explanation": self.explanation,
        }


def _principal(value: Decimal, currency: str, minor_unit: int) -> Decimal:
    amount = require_decimal(value, "principal", positive=True, code=INVALID_SCHEDULE)
    if amount != round_money(amount, minor_unit):
        raise TimePayError(INVALID_SCHEDULE, f"principal has more decimals than {currency} allows", "principal")
    return amount


@in_calc_context
def loan_schedule(
    principal: Decimal,
    tenure_months: int,
    currency: str,
    *,
    annual_rate_percent: Decimal = Decimal(0),
    method: str = "FLAT",
    rounding: str = "HALF_UP",
    minor_unit: Optional[int] = None,
) -> RepaymentSchedule:
    """Monthly instalments for a loan by ``interest_method``; the last instalment absorbs rounding."""
    minor = resolve_minor_unit(currency, minor_unit)
    amount = _principal(principal, currency, minor)
    months = require_int(tenure_months, "tenure_months", minimum=1, maximum=600, code=INVALID_SCHEDULE)
    rate = require_decimal(annual_rate_percent, "annual_rate_percent", minimum=Decimal(0), maximum=Decimal(100),
                           code=INVALID_SCHEDULE)
    if method not in INTEREST_METHODS:
        raise TimePayError(INVALID_SCHEDULE, f"interest_method must be one of {', '.join(INTEREST_METHODS)}", "method")
    if amount < quantum(minor) * months:
        raise TimePayError(INVALID_SCHEDULE, "principal is too small to spread over the tenure", "tenure_months")
    if method == "FLAT":
        instalments, explanation = _flat(amount, months, rate, minor, rounding)
    else:
        instalments, explanation = _reducing(amount, months, rate, minor, rounding)
    return RepaymentSchedule(LOAN, method, amount, rate, currency, instalments, explanation)


def _flat(amount: Decimal, months: int, rate: Decimal, minor: int, rounding: str) -> Tuple[Tuple[Instalment, ...], str]:
    interest = round_money(amount * rate * months / 1200, minor, rounding)
    principal_each = round_money(amount / months, minor, rounding)
    interest_each = round_money(interest / months, minor, rounding)
    result: List[Instalment] = []
    paid_principal = paid_interest = Decimal(0)
    for number in range(1, months + 1):
        if number < months:
            part, extra = principal_each, interest_each
        else:
            part, extra = amount - paid_principal, interest - paid_interest
        paid_principal += part
        paid_interest += extra
        result.append(Instalment(number, part, extra, part + extra, amount - paid_principal))
    if any(i.principal < 0 or i.interest < 0 for i in result):
        raise TimePayError(INVALID_SCHEDULE, "rounding leaves a negative last instalment; shorten the tenure", "tenure")
    explanation = (f"FLAT: interest {show(amount)} × {show(rate)}% × {months} ÷ 12 = {show(interest)}; "
                   f"{months} instalments of {show(principal_each + interest_each)}, the last one "
                   f"{show(result[-1].total)}")
    return tuple(result), explanation


def _reducing(amount: Decimal, months: int, rate: Decimal, minor: int,
              rounding: str) -> Tuple[Tuple[Instalment, ...], str]:
    monthly = rate / 1200
    if monthly == 0:
        emi = round_money(amount / months, minor, rounding)
    else:
        growth = (1 + monthly) ** months
        emi = round_money(amount * monthly * growth / (growth - 1), minor, rounding)
    balance = amount
    result: List[Instalment] = []
    for number in range(1, months + 1):
        interest = round_money(balance * monthly, minor, rounding)
        principal = balance if number == months else emi - interest
        if principal < 0 or principal > balance:
            raise TimePayError(INVALID_SCHEDULE, "the instalment does not amortise this loan", "tenure_months")
        balance -= principal
        result.append(Instalment(number, principal, interest, principal + interest, balance))
    explanation = (f"REDUCING: EMI on {show(amount)} at {show(rate)}% a year over {months} months = {show(emi)}; "
                   f"the last instalment {show(result[-1].total)}")
    return tuple(result), explanation


@dataclass(frozen=True)
class LoanRules:
    max_tenure_months: int
    interest_rate_percent: Decimal
    interest_method: str
    max_instalment_percent_of_net: Decimal
    min_service_months: int
    max_active_loans: int
    recover_balance_on_exit: bool


def parse_loan_policy(policy: Any) -> LoanRules:
    """Read a LOAN_POLICY (3.1 §5.11); ``max_amount_formula`` is evaluated by the formula engine."""
    return LoanRules(
        max_tenure_months=policy_int(policy, "max_tenure_months", 24, minimum=1, maximum=600) or 24,
        interest_rate_percent=policy_decimal(policy, "interest_rate_percent", "0", minimum=Decimal(0),
                                             maximum=Decimal(100)) or Decimal(0),
        interest_method=policy_choice(policy, "interest_method", "FLAT", INTEREST_METHODS),
        max_instalment_percent_of_net=policy_decimal(policy, "max_instalment_percent_of_net", "50",
                                                     minimum=Decimal(0), maximum=Decimal(100)) or Decimal(0),
        min_service_months=policy_int(policy, "min_service_months", 6, minimum=0, maximum=600) or 0,
        max_active_loans=policy_int(policy, "max_active_loans", 1, minimum=0, maximum=100) or 0,
        recover_balance_on_exit=policy_bool(policy, "recover_balance_on_exit", True),
    )


def loan_schedule_from_policy(principal: Decimal, tenure_months: int, policy: Any, currency: str, *,
                              rounding: str = "HALF_UP") -> RepaymentSchedule:
    """``loan_schedule`` with the policy's rate and method; a tenure above the maximum raises."""
    rules = parse_loan_policy(policy)
    if require_int(tenure_months, "tenure_months", minimum=1, code=INVALID_SCHEDULE) > rules.max_tenure_months:
        raise TimePayError(INVALID_SCHEDULE, f"tenure exceeds the policy maximum of {rules.max_tenure_months} months",
                           "tenure_months")
    return loan_schedule(principal, tenure_months, currency, annual_rate_percent=rules.interest_rate_percent,
                         method=rules.interest_method, rounding=rounding)


@in_calc_context
def check_loan_application(
    amount: Decimal,
    tenure_months: int,
    policy: Any,
    currency: str,
    *,
    max_amount: Optional[Decimal],
    service_months: int,
    active_loans: int,
    net_pay: Optional[Decimal] = None,
) -> Tuple[Issue, ...]:
    """Application checks against LOAN_POLICY (maximum, tenure, service, active loans, instalment share of net)."""
    rules = parse_loan_policy(policy)
    issues: List[Issue] = []
    requested = require_decimal(amount, "amount", positive=True)
    if max_amount is None:
        issues.append(Issue("LOAN_MAXIMUM_UNKNOWN", BLOCKING,
                            "The maximum loan amount (max_amount_formula) is not available; the application cannot be "
                            "checked"))
    elif requested > require_decimal(max_amount, "max_amount", minimum=Decimal(0)):
        issues.append(Issue("LOAN_ABOVE_MAXIMUM", BLOCKING, f"{show(requested)} exceeds the maximum of {show(max_amount)}"))
    if require_int(tenure_months, "tenure_months", minimum=1) > rules.max_tenure_months:
        issues.append(Issue("LOAN_TENURE_ABOVE_MAXIMUM", BLOCKING,
                            f"{tenure_months} months exceeds the maximum of {rules.max_tenure_months}"))
    if require_int(service_months, "service_months", minimum=0) < rules.min_service_months:
        issues.append(Issue("LOAN_SERVICE_TOO_SHORT", BLOCKING,
                            f"{service_months} months of service; {rules.min_service_months} needed"))
    if require_int(active_loans, "active_loans", minimum=0) >= rules.max_active_loans:
        issues.append(Issue("LOAN_TOO_MANY_ACTIVE", BLOCKING,
                            f"{active_loans} active loan(s); at most {rules.max_active_loans} allowed"))
    if net_pay is not None and not issues:
        schedule = loan_schedule(requested, tenure_months, currency, annual_rate_percent=rules.interest_rate_percent,
                                 method=rules.interest_method)
        limit = require_decimal(net_pay, "net_pay", minimum=Decimal(0)) * rules.max_instalment_percent_of_net / 100
        largest = max(i.total for i in schedule.instalments)
        if largest > limit:
            issues.append(Issue("LOAN_INSTALMENT_ABOVE_NET_SHARE", BLOCKING,
                                f"Instalment {show(largest)} exceeds {show(rules.max_instalment_percent_of_net)}% of "
                                f"net pay ({show(limit)})"))
    return tuple(issues)


# --------------------------------------------------------------------------- advances


@dataclass(frozen=True)
class AdvanceRules:
    max_percent_of_monthly_net: Decimal
    recover_in_runs: int
    max_per_year: int


def parse_advance_policy(policy: Any) -> AdvanceRules:
    """Read an ADVANCE_POLICY (3.1 §5.11)."""
    return AdvanceRules(
        max_percent_of_monthly_net=policy_decimal(policy, "max_percent_of_monthly_net", "50", minimum=Decimal(0),
                                                  maximum=Decimal(100)) or Decimal(0),
        recover_in_runs=policy_int(policy, "recover_in_runs", 1, minimum=1, maximum=MAX_ADVANCE_RUNS) or 1,
        max_per_year=policy_int(policy, "max_per_year", 2, minimum=0, maximum=100) or 0,
    )


@in_calc_context
def advance_schedule(amount: Decimal, currency: str, policy: Any = None, *, runs: Optional[int] = None,
                     rounding: str = "HALF_UP") -> RepaymentSchedule:
    """Equal recoveries over ``runs`` (default the policy's ``recover_in_runs``, at most 3) without interest."""
    rules = parse_advance_policy(policy)
    count = rules.recover_in_runs if runs is None else require_int(runs, "runs", minimum=1, maximum=MAX_ADVANCE_RUNS,
                                                                   code=INVALID_SCHEDULE)
    if count > rules.recover_in_runs:
        raise TimePayError(INVALID_SCHEDULE, f"the policy recovers advances in at most {rules.recover_in_runs} run(s)",
                           "runs")
    minor = resolve_minor_unit(currency)
    value = _principal(amount, currency, minor)
    if value < quantum(minor) * count:
        raise TimePayError(INVALID_SCHEDULE, "the advance is too small to spread over the runs", "runs")
    each = round_money(value / count, minor, rounding)
    result: List[Instalment] = []
    paid = Decimal(0)
    for number in range(1, count + 1):
        part = each if number < count else value - paid
        paid += part
        result.append(Instalment(number, part, Decimal(0), part, value - paid))
    explanation = f"Advance {show(value)} recovered in {count} run(s): " + " + ".join(show(i.total) for i in result)
    return RepaymentSchedule(ADVANCE, EQUAL, value, Decimal(0), currency, tuple(result), explanation)


@in_calc_context
def check_advance_request(amount: Decimal, *, monthly_net: Decimal, advances_this_year: int,
                          policy: Any = None) -> Tuple[Issue, ...]:
    """ADVANCE_POLICY checks: share of monthly net and the yearly count."""
    rules = parse_advance_policy(policy)
    value = require_decimal(amount, "amount", positive=True)
    limit = require_decimal(monthly_net, "monthly_net", minimum=Decimal(0)) * rules.max_percent_of_monthly_net / 100
    issues: List[Issue] = []
    if value > limit:
        issues.append(Issue("ADVANCE_ABOVE_MAXIMUM", BLOCKING,
                            f"{show(value)} exceeds {show(rules.max_percent_of_monthly_net)}% of monthly net "
                            f"({show(limit)})"))
    if require_int(advances_this_year, "advances_this_year", minimum=0) >= rules.max_per_year:
        issues.append(Issue("ADVANCE_YEARLY_LIMIT", BLOCKING,
                            f"{advances_this_year} advance(s) this year; at most {rules.max_per_year} allowed"))
    return tuple(issues)


# --------------------------------------------------------------------------- protected net


@dataclass(frozen=True)
class ProtectedNet:
    percent: Optional[Decimal]  # None: no floor configured, automated recoveries are blocked
    floor: Optional[Decimal]
    room: Optional[Decimal]
    explanation: str


@in_calc_context
def protected_net_room(gross: Decimal, net_before: Decimal, currency: str, *,
                       min_net_pay_percent: Optional[Decimal], pack_floor_percent: Optional[Decimal] = None,
                       already_recovered: Decimal = Decimal(0)) -> ProtectedNet:
    """How much non-statutory deductions may still take: ``net − ⌈gross × percent ÷ 100⌉``, never negative.

    The company percentage can only raise the pack floor. The floor is rounded up to the
    minor unit so net pay can never end below it.
    """
    minor = resolve_minor_unit(currency)
    gross_value = require_decimal(gross, "gross", minimum=Decimal(0))
    net_value = require_decimal(net_before, "net_before")
    used = require_decimal(already_recovered, "already_recovered", minimum=Decimal(0))
    candidates = [require_decimal(p, name, minimum=Decimal(0), maximum=Decimal(100))
                  for name, p in (("min_net_pay_percent", min_net_pay_percent), ("pack_floor_percent", pack_floor_percent))
                  if p is not None]
    if not candidates:
        return ProtectedNet(None, None, None, "No protected net pay percentage is configured")
    percent = max(candidates)
    floor = round_money(gross_value * percent / 100, minor, "CEILING")
    room = max(Decimal(0), net_value - used - floor)
    return ProtectedNet(percent, floor, room,
                        f"Net {show(net_value - used)} − floor {show(floor)} ({show(percent)}% of gross "
                        f"{show(gross_value)}) = room {show(room)}")


# --------------------------------------------------------------------------- recoveries


@dataclass(frozen=True)
class LoanTransaction:
    """A ledger row. Amounts are positive; an ADJUSTMENT is signed (positive adds to the balance)."""

    kind: str
    amount: Decimal
    ref: Optional[str] = None  # run_employee_id for INSTALMENT
    scheduled: Optional[Decimal] = None  # the scheduled part of an INSTALMENT (arrears excluded)


@dataclass(frozen=True)
class LoanAccount:
    loan_id: str
    schedule: RepaymentSchedule
    transactions: Tuple[LoanTransaction, ...] = ()
    status: str = ACTIVE

    @property
    def kind(self) -> str:
        return self.schedule.kind

    @property
    def currency(self) -> str:
        return self.schedule.currency


def _check_transactions(account: LoanAccount) -> None:
    refs = set()
    for tx in account.transactions:
        if tx.kind not in TRANSACTION_KINDS:
            raise TimePayError(INVALID_INPUT, f"{account.loan_id}: unknown transaction kind {tx.kind!r}", "kind")
        require_decimal(tx.amount, f"{account.loan_id} {tx.kind} amount",
                        minimum=None if tx.kind == ADJUSTMENT else Decimal(0))
        if tx.kind == INSTALMENT:
            if not tx.ref:
                raise TimePayError(INVALID_INPUT, f"{account.loan_id}: an instalment needs its run employee", "ref")
            if tx.ref in refs:
                raise TimePayError(INVALID_INPUT, f"{account.loan_id}: two instalments for run employee {tx.ref}", "ref")
            refs.add(tx.ref)
            if tx.scheduled is not None:
                require_decimal(tx.scheduled, "scheduled", minimum=Decimal(0))


def loan_balance(account: LoanAccount, *, excluding_ref: Optional[str] = None) -> Decimal:
    """Outstanding payable: scheduled total − instalments − prepayments − waivers ± adjustments.

    Before any DISBURSEMENT nothing is outstanding. ``excluding_ref`` leaves out the instalment
    of a run employee being recalculated (EC-VAR-08).
    """
    _check_transactions(account)
    disbursed = sum((tx.amount for tx in account.transactions if tx.kind == DISBURSEMENT), Decimal(0))
    if not disbursed:
        return Decimal(0)
    if disbursed != account.schedule.principal:
        raise TimePayError(INVALID_SCHEDULE, f"{account.loan_id}: disbursed {disbursed} differs from the schedule "
                           f"principal {account.schedule.principal}; restructure the schedule", "transactions")
    balance = account.schedule.total_payable
    for tx in account.transactions:
        if tx.kind == INSTALMENT and tx.ref == excluding_ref:
            continue
        if tx.kind in (INSTALMENT, PREPAYMENT, WAIVER):
            balance -= tx.amount
        elif tx.kind == ADJUSTMENT:
            balance += tx.amount
    return balance


@dataclass(frozen=True)
class LoanRecovery:
    loan_id: str
    kind: str
    run_employee_id: str
    instalment_number: Optional[int]
    scheduled: Decimal
    arrears: Decimal
    due: Decimal
    recovered: Decimal
    carried_forward: Decimal
    receivable: Decimal
    balance_after: Decimal
    replaces_existing: bool
    skipped_reason: Optional[str]
    line: Optional[TimePayLine]
    transaction: Optional[LoanTransaction]
    issues: Tuple[Issue, ...]
    explanation: str


@in_calc_context
def loan_recovery(
    account: LoanAccount,
    *,
    run_employee_id: str,
    room: Optional[Decimal],
    final_settlement: bool = False,
    enabled: bool = True,
    recover_balance_on_exit: bool = True,
    component: Optional[str] = None,
) -> LoanRecovery:
    """This run's recovery for one loan or advance (3.4 §15).

    ``room`` is what the protected net still allows (``protected_net_room(...).room``; ``None``
    when no floor is configured). ``enabled`` is ``payroll.loans.enabled`` or
    ``payroll.advances.enabled`` for the period.
    """
    if not isinstance(run_employee_id, str) or not run_employee_id:
        raise TimePayError(INVALID_INPUT, "run_employee_id is required", "run_employee_id")
    if account.status not in LOAN_STATUSES:
        raise TimePayError(INVALID_INPUT, f"{account.loan_id}: unknown status {account.status!r}", "status")
    minor = resolve_minor_unit(account.currency)
    name = RECOVERY_COMPONENTS[account.kind] if component is None else component
    prior = [tx for tx in account.transactions if tx.kind == INSTALMENT and tx.ref != run_employee_id]
    replaces = any(tx.kind == INSTALMENT and tx.ref == run_employee_id for tx in account.transactions)
    balance = loan_balance(account, excluding_ref=run_employee_id)
    number = len(prior) + 1
    planned = account.schedule.instalment(number)
    scheduled = planned.total if planned else Decimal(0)
    arrears = max(Decimal(0), sum((tx.scheduled if tx.scheduled is not None else tx.amount for tx in prior), Decimal(0))
                  - sum((tx.amount for tx in prior), Decimal(0)))
    label = "Loan" if account.kind == LOAN else "Advance"

    def skipped(reason: str, text: str, issues: Tuple[Issue, ...] = ()) -> LoanRecovery:
        return LoanRecovery(account.loan_id, account.kind, run_employee_id, None, Decimal(0), arrears, Decimal(0),
                            Decimal(0), arrears, Decimal(0), balance, replaces, reason, None, None, issues, text)

    if not enabled:
        return skipped("SWITCH_OFF", f"{label} recovery is switched OFF for this period")
    if account.status == CLOSED or balance <= 0:
        return skipped("NOTHING_OUTSTANDING", f"{label} {account.loan_id} has nothing outstanding")
    if account.status == PAUSED and not final_settlement:
        return skipped("PAUSED", f"{label} {account.loan_id} is paused; skipped this period and the schedule moves on")

    if final_settlement and recover_balance_on_exit:
        due = balance
        basis = f"final settlement: outstanding balance {show(balance)}"
    elif planned:
        due = min(balance, scheduled + arrears)
        basis = f"instalment {number} of {account.schedule.tenure}: {show(scheduled)}"
        if arrears:
            basis += f" + {show(arrears)} carried forward"
    else:
        due = balance
        basis = f"after the last instalment: outstanding balance {show(balance)}"
    issues: List[Issue] = []
    if room is None:
        issues.append(Issue("PROTECTED_NET_NOT_SET", BLOCKING,
                            f"{label} recovery needs a protected net pay percentage; nothing recovered",
                            ref=account.loan_id))
        recovered = Decimal(0)
    else:
        available = require_decimal(room, "room", minimum=Decimal(0))
        recovered = round_money(min(due, available), minor, "DOWN")
    remainder = due - recovered
    receivable = balance - recovered if final_settlement else Decimal(0)
    carried = Decimal(0) if final_settlement else remainder
    if final_settlement and receivable and room is not None:
        reason = ("cannot be recovered within the protected net" if remainder
                  else "is not recovered on exit (recover_balance_on_exit is false)")
        issues.append(Issue("LOAN_RECEIVABLE", WARNING,
                            f"{show(receivable)} of {label.lower()} {account.loan_id} {reason} and becomes a receivable",
                            ref=account.loan_id, details={"receivable": amount_str(receivable, minor)}))
    elif remainder and room is not None and not final_settlement:
        issues.append(Issue("LOAN_PARTIAL_RECOVERY", WARNING,
                            f"Only {show(recovered)} of {show(due)} recovered for {label.lower()} "
                            f"{account.loan_id} to protect net pay; {show(remainder)} carried forward",
                            ref=account.loan_id, details={"carried_forward": amount_str(remainder, minor)}))
    if replaces:
        issues.append(Issue("LOAN_RECOVERY_RECALCULATED", INFO,
                            "The earlier recovery for this run employee is replaced, not added", ref=account.loan_id))
    explanation = f"{label} {account.loan_id}, {basis}; due {show(due)}"
    if recovered != due:
        explanation += f", recovered {show(recovered)} within the protected net"
    line = None
    transaction = LoanTransaction(INSTALMENT, recovered, run_employee_id,
                                  None if final_settlement else min(scheduled, balance))
    if recovered:
        line = make_line(name, DEDUCTION, "LOAN", recovered, account.currency, minor_unit=minor, rounding="HALF_UP",
                         method="SCHEDULE", explanation=explanation,
                         sources=[{"type": "loan", "id": account.loan_id, "run_employee_id": run_employee_id,
                                   "instalment": str(number), "kind": account.kind}])
    return LoanRecovery(account.loan_id, account.kind, run_employee_id, number if planned else None, scheduled,
                        arrears, due, recovered, carried, receivable, balance - recovered, replaces, None, line,
                        transaction, tuple(issues), explanation)


@in_calc_context
def loan_recoveries(
    accounts: Sequence[LoanAccount],
    *,
    run_employee_id: str,
    room: Optional[Decimal],
    final_settlement: bool = False,
    enabled: bool = True,
    recover_balance_on_exit: bool = True,
) -> Tuple[LoanRecovery, ...]:
    """Recoveries for several loans in the given priority order, sharing one protected-net room."""
    ids = [a.loan_id for a in accounts]
    if len(set(ids)) != len(ids):
        raise TimePayError(INVALID_INPUT, "a loan is given twice", "accounts")
    remaining = room
    results: List[LoanRecovery] = []
    for account in accounts:
        result = loan_recovery(account, run_employee_id=run_employee_id, room=remaining,
                               final_settlement=final_settlement, enabled=enabled,
                               recover_balance_on_exit=recover_balance_on_exit)
        results.append(result)
        if remaining is not None:
            remaining = remaining - result.recovered
    return tuple(results)


@dataclass(frozen=True)
class ProtectedRecovery:
    due: Decimal
    recovered: Decimal
    carried_forward: Decimal
    explanation: str


@in_calc_context
def protected_recovery(amount_due: Decimal, room: Optional[Decimal], currency: str) -> ProtectedRecovery:
    """Recover ``min(due, room)`` now and carry the rest (clawbacks and negative arrears, EC-TSK-08)."""
    minor = resolve_minor_unit(currency)
    due = require_decimal(amount_due, "amount_due", minimum=Decimal(0))
    if room is None:
        return ProtectedRecovery(due, Decimal(0), due, "No protected net pay percentage is configured; nothing recovered")
    available = require_decimal(room, "room", minimum=Decimal(0))
    recovered = round_money(min(due, available), minor, "DOWN")
    text = f"Due {show(due)}, room {show(available)}: recovered {show(recovered)}"
    if recovered < due:
        text += f", {show(due - recovered)} carried to the next run"
    return ProtectedRecovery(due, recovered, due - recovered, text)


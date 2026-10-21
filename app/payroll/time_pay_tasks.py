"""Task-based pay: rates and rate cards, attribution, splits, caps, the minimum-wage floor and clawback
(3.4 §10, 3.1 §5.6).

Per task paid to one employee:

    rate          by ``rate_source``: TASK, RATE_CARD or TASK_THEN_RATE_CARD; a rate card row is
                  task_type × optional grade/skill/location × effective dates, the most specific
                  matching row wins (a tie between different rows is a review item)
    amount        accepted_quantity × rate (× FX into the pay currency, rounded once per task);
                  only APPROVED tasks are paid (EC-TSK-02), accepted ≤ submitted (EC-TSK-04)
    split         shares of the task amount by ``split_percent`` (exactly 100.00 in total,
                  EC-TSK-05); largest remainders, so the shares add up to the task amount
    attribution   APPROVAL_DATE or COMPLETION_DATE inside the period (earlier unpaid tasks are
                  paid now as late items); only dates where the switch is ON pay (EC-TSK-11)
    cap           cap_per_period over carried-in + current amounts: EXCEPTION, TRUNCATE or
                  TRUNCATE_AND_CARRY (EC-TSK-06)

``minimum_wage_check`` compares the task earnings paid in the period (carried-out
and truncated amounts excluded) with worked hours × the pack's minimum hourly
rate: WARN adds a review item, TOP_UP a MIN_WAGE_TOP_UP earning; without
verified pack data the result is always WARN "not verified" (EC-TSK-07). Task
pay never replaces contracted base pay (EC-TSK-13). A reversed paid task is
recovered as a negative TASK_EARNING within the protected net, the rest carried
(EC-TSK-08). A rate in another currency without an FX rate blocks the task
(EC-TSK-10).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.payroll.time_pay_common import (
    ACCEPTED_EXCEEDS_SUBMITTED,
    BLOCKING,
    CURRENCY_MISMATCH,
    DUPLICATE_EXTERNAL_REF,
    EARNING,
    INFO,
    INVALID_INPUT,
    INVALID_POLICY,
    INVALID_SPLIT,
    SPLIT_NOT_ALLOWED,
    WARNING,
    DayRanges,
    Issue,
    TimePayError,
    TimePayLine,
    amount_str,
    check_currency,
    check_ranges,
    field_value,
    in_calc_context,
    make_line,
    policy_bool,
    policy_choice,
    policy_money,
    quantum,
    require_decimal,
    resolve_minor_unit,
    round_money,
    show,
    switch_on,
)
from app.payroll.time_pay_loans import protected_recovery

APPROVED = "APPROVED"
TASK_STATUSES = ("DRAFT", "SUBMITTED", "APPROVED", "REJECTED", "DISPUTED", "PAID", "REVERSED")
ATTRIBUTIONS = ("APPROVAL_DATE", "COMPLETION_DATE")
RATE_SOURCES = ("TASK", "RATE_CARD", "TASK_THEN_RATE_CARD")
CAP_ACTIONS = ("EXCEPTION", "TRUNCATE_AND_CARRY", "TRUNCATE")
FLOOR_MODES = ("OFF", "WARN", "TOP_UP")
CLAWBACK_MODES = ("NEXT_RUN", "MANUAL")
CONTRACTOR_DOCUMENTS = ("INVOICE", "SELF_BILLED_INVOICE")
CONTRACTOR = "CONTRACTOR"
HUNDRED = Decimal(100)
MIN_WAGE_TOP_UP = "MIN_WAGE_TOP_UP"
NOT_VERIFIED_MESSAGE = "Minimum wage not verified for this jurisdiction"

# Exclusion reasons
NOT_APPROVED = "NOT_APPROVED"
ALREADY_PAID = "ALREADY_PAID"
NOT_ASSIGNED = "NOT_ASSIGNED"
NOT_YET_DUE = "NOT_YET_DUE"
SWITCH_OFF = "SWITCH_OFF"
BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class TaskAssignee:
    employee_id: str
    split_percent: Decimal = HUNDRED


@dataclass(frozen=True)
class TaskRecord:
    """A task as stored by the tasks module (3.4 §10.1)."""

    task_id: str
    task_type: str
    status: str
    quantity: Decimal
    assignees: Tuple[TaskAssignee, ...]
    accepted_quantity: Optional[Decimal] = None  # None: the whole submitted quantity was accepted
    rate: Optional[Decimal] = None
    rate_currency: Optional[str] = None
    approved_on: Optional[date] = None
    completed_on: Optional[date] = None
    evidence_count: int = 0
    external_ref: Optional[str] = None
    paid_in_run_id: Optional[str] = None
    unit: Optional[str] = None


@dataclass(frozen=True)
class RateCardRow:
    task_type: str
    rate: Decimal
    currency: str
    effective_from: date
    effective_to: Optional[date] = None
    grade: Optional[str] = None
    skill: Optional[str] = None
    location: Optional[str] = None


@dataclass(frozen=True)
class EmployeeAttributes:
    grade: Optional[str] = None
    skill: Optional[str] = None
    location: Optional[str] = None


@dataclass(frozen=True)
class TaskPayRules:
    attribution: str
    rate_source: str
    earning_component: str
    cap_per_period: Optional[Decimal]
    cap_action: str
    minimum_wage_floor: str
    allow_split: bool
    requires_evidence: bool
    clawback: str
    contractor_document: str


def parse_task_pay_policy(policy: Any, currency: str) -> TaskPayRules:
    """Read a TASK_PAY_POLICY (3.1 §5.6); the cap is money in the pay currency."""
    component = field_value(policy, "earning_component", "TASK_EARNING")
    if not isinstance(component, str) or not component:
        raise TimePayError(INVALID_POLICY, "earning_component must be a component code", "earning_component")
    return TaskPayRules(
        attribution=policy_choice(policy, "attribution", "APPROVAL_DATE", ATTRIBUTIONS),
        rate_source=policy_choice(policy, "rate_source", "TASK_THEN_RATE_CARD", RATE_SOURCES),
        earning_component=component,
        cap_per_period=policy_money(policy, "cap_per_period", currency),
        cap_action=policy_choice(policy, "cap_action", "EXCEPTION", CAP_ACTIONS),
        minimum_wage_floor=policy_choice(policy, "minimum_wage_floor", "WARN", FLOOR_MODES),
        allow_split=policy_bool(policy, "allow_split", False),
        requires_evidence=policy_bool(policy, "requires_evidence", False),
        clawback=policy_choice(policy, "clawback", "NEXT_RUN", CLAWBACK_MODES),
        contractor_document=policy_choice(policy, "contractor_document", "INVOICE", CONTRACTOR_DOCUMENTS),
    )


# --------------------------------------------------------------------------- validation and splits


def validate_split(assignees: Sequence[TaskAssignee], *, allow_split: bool, task_id: str = "task") -> None:
    """Assignees are unique; several need ``allow_split`` and percentages totalling exactly 100.00 (EC-TSK-05)."""
    if not assignees:
        raise TimePayError(INVALID_SPLIT, f"{task_id}: a task needs at least one assignee", "assignees")
    ids = [a.employee_id for a in assignees]
    if len(set(ids)) != len(ids) or not all(isinstance(i, str) and i for i in ids):
        raise TimePayError(INVALID_SPLIT, f"{task_id}: assignees must be distinct employees", "assignees")
    if len(assignees) > 1 and not allow_split:
        raise TimePayError(SPLIT_NOT_ALLOWED, f"{task_id}: the policy does not allow splitting a task", "assignees")
    total = Decimal(0)
    for assignee in assignees:
        percent = require_decimal(assignee.split_percent, "split_percent", positive=True, maximum=HUNDRED,
                                  code=INVALID_SPLIT)
        if percent != percent.quantize(Decimal("0.01")):
            raise TimePayError(INVALID_SPLIT, f"{task_id}: split percentages have at most 2 decimals", "split_percent")
        total += percent
    if total != HUNDRED:
        raise TimePayError(INVALID_SPLIT, f"{task_id}: split percentages total {total}, not exactly 100.00",
                           "split_percent")


@in_calc_context
def split_amount(amount: Decimal, assignees: Sequence[TaskAssignee], currency: str) -> Dict[str, Decimal]:
    """Shares of a rounded task amount; the minor units left over go to the largest remainders
    (then to the earlier assignee), so the shares always add up to ``amount``."""
    minor = resolve_minor_unit(currency)
    total = require_decimal(amount, "amount", minimum=Decimal(0))
    if total != round_money(total, minor):
        raise TimePayError(INVALID_INPUT, f"split amount has more decimals than {currency} allows", "amount")
    unit = quantum(minor)
    raw = [(a.employee_id, total * a.split_percent / HUNDRED) for a in assignees]
    shares = {employee: round_money(value, minor, "DOWN") for employee, value in raw}
    left = int((total - sum(shares.values(), Decimal(0))) / unit)
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i][1] - shares[raw[i][0]]), i))
    for index in order[:left]:
        shares[raw[index][0]] += unit
    return shares


def _check_tasks(tasks: Sequence[TaskRecord]) -> None:
    ids = set()
    refs = set()
    for task in tasks:
        if not isinstance(task.task_id, str) or not task.task_id or task.task_id in ids:
            raise TimePayError(INVALID_INPUT, f"task_id must be unique and non-empty: {task.task_id!r}", "task_id")
        ids.add(task.task_id)
        if task.external_ref is not None:
            if task.external_ref in refs:
                raise TimePayError(DUPLICATE_EXTERNAL_REF, f"external_ref {task.external_ref} is used twice",
                                   "external_ref")
            refs.add(task.external_ref)
        if task.status not in TASK_STATUSES:
            raise TimePayError(INVALID_INPUT, f"{task.task_id}: unknown status {task.status!r}", "status")
        submitted = require_decimal(task.quantity, f"{task.task_id} quantity", minimum=Decimal(0))
        if task.accepted_quantity is not None:
            accepted = require_decimal(task.accepted_quantity, f"{task.task_id} accepted_quantity", minimum=Decimal(0))
            if accepted > submitted:
                raise TimePayError(ACCEPTED_EXCEEDS_SUBMITTED,
                                   f"{task.task_id}: accepted {accepted} exceeds the submitted {submitted}",
                                   "accepted_quantity")
        if task.rate is not None:
            require_decimal(task.rate, f"{task.task_id} rate", minimum=Decimal(0))
            if task.rate_currency is None:
                raise TimePayError(INVALID_INPUT, f"{task.task_id}: a task rate needs its currency", "rate_currency")
            check_currency(task.rate_currency, "rate_currency")


# --------------------------------------------------------------------------- rates


@dataclass(frozen=True)
class ResolvedRate:
    rate: Decimal
    currency: str
    source: str  # TASK | RATE_CARD
    explanation: str


def resolve_task_rate(task: TaskRecord, rate_source: str, rate_card: Sequence[RateCardRow], on: date,
                      employee: EmployeeAttributes) -> Tuple[Optional[ResolvedRate], Optional[Issue]]:
    """The rate for ``task`` by ``rate_source`` on ``on``; a missing or ambiguous rate is a review item."""
    if rate_source in ("TASK", "TASK_THEN_RATE_CARD") and task.rate is not None:
        assert task.rate_currency is not None
        return ResolvedRate(task.rate, task.rate_currency, "TASK", f"task rate {show(task.rate)} {task.rate_currency}"), None
    if rate_source == "TASK":
        return None, Issue("TASK_RATE_MISSING", BLOCKING, "The task has no rate and the policy uses task rates only",
                           on, task.task_id)
    candidates: List[Tuple[int, RateCardRow]] = []
    for row in rate_card:
        if row.task_type != task.task_type or row.effective_from > on or (row.effective_to and row.effective_to < on):
            continue
        attributes = (("grade", row.grade, employee.grade), ("skill", row.skill, employee.skill),
                      ("location", row.location, employee.location))
        if any(wanted is not None and wanted != actual for _, wanted, actual in attributes):
            continue
        candidates.append((sum(1 for _, wanted, _ in attributes if wanted is not None), row))
    if not candidates:
        return None, Issue("TASK_RATE_NOT_FOUND", BLOCKING,
                           f"No rate card row for {task.task_type} on {on.isoformat()}", on, task.task_id)
    best = max(score for score, _ in candidates)
    rows = {(row.rate, row.currency) for score, row in candidates if score == best}
    if len(rows) > 1:
        return None, Issue("TASK_RATE_AMBIGUOUS", BLOCKING,
                           f"Several equally specific rate card rows for {task.task_type} on {on.isoformat()}",
                           on, task.task_id)
    rate, currency = rows.pop()
    require_decimal(rate, "rate card rate", minimum=Decimal(0))
    check_currency(currency, "rate card currency")
    return ResolvedRate(rate, currency, "RATE_CARD", f"rate card {task.task_type} {show(rate)} {currency}"), None


# --------------------------------------------------------------------------- minimum wage


@dataclass(frozen=True)
class MinimumWageInput:
    """Pack data for the floor: worked hours and the legal minimum hourly rate, with its verification."""

    worked_hours: Decimal
    min_hourly_rate: Optional[Decimal]
    verified: bool
    pack_forces_top_up: bool = False
    other_wages_counted: Decimal = Decimal(0)  # wages the pack counts toward the minimum, if any


@dataclass(frozen=True)
class MinimumWageCheck:
    mode: str
    verified: bool
    earnings: Decimal
    entitlement: Optional[Decimal]
    shortfall: Decimal
    line: Optional[TimePayLine]
    issues: Tuple[Issue, ...]
    explanation: str


@in_calc_context
def minimum_wage_check(
    task_earnings: Decimal,
    worked_hours: Decimal,
    *,
    mode: str,
    currency: str,
    min_hourly_rate: Optional[Decimal],
    verified: bool,
    pack_forces_top_up: bool = False,
    other_wages_counted: Decimal = Decimal(0),
) -> MinimumWageCheck:
    """The minimum-wage floor for task-paid employees (3.4 §10.6, §17.3).

    A TOP_UP shortfall is rounded up to the minor unit so pay never ends below the floor.
    """
    minor = resolve_minor_unit(currency)
    if mode not in FLOOR_MODES:
        raise TimePayError(INVALID_POLICY, f"minimum_wage_floor must be one of {', '.join(FLOOR_MODES)}",
                           "minimum_wage_floor")
    effective = "TOP_UP" if pack_forces_top_up else mode
    earnings = require_decimal(task_earnings, "task_earnings", minimum=Decimal(0))
    counted = earnings + require_decimal(other_wages_counted, "other_wages_counted", minimum=Decimal(0))
    hours = require_decimal(worked_hours, "worked_hours", minimum=Decimal(0))
    if effective == "OFF":
        return MinimumWageCheck("OFF", verified, earnings, None, Decimal(0), None, (), "Minimum-wage floor OFF")
    rate = None if min_hourly_rate is None else require_decimal(min_hourly_rate, "min_hourly_rate", minimum=Decimal(0))
    entitlement = None if rate is None else hours * rate
    shortfall = Decimal(0) if entitlement is None else max(Decimal(0), entitlement - counted)
    basis = (f"{show(hours)} h × {show(rate)} = {show(entitlement)} vs earnings {show(counted)}"
             if entitlement is not None else f"earnings {show(counted)}, no minimum rate")
    if not verified or rate is None:
        detail = f"; below the unverified figure by {show(shortfall)}" if shortfall else ""
        issue = Issue("MIN_WAGE_NOT_VERIFIED", WARNING, NOT_VERIFIED_MESSAGE + detail,
                      details={"shortfall": amount_str(round_money(shortfall, minor, "CEILING"), minor)})
        return MinimumWageCheck("WARN", False, earnings, entitlement, shortfall, None, (issue,),
                                f"{basis}: {NOT_VERIFIED_MESSAGE}")
    if not shortfall:
        return MinimumWageCheck(effective, True, earnings, entitlement, Decimal(0), None, (),
                                f"{basis}: at or above the minimum")
    if effective == "WARN":
        issue = Issue("MIN_WAGE_SHORTFALL", WARNING, f"Below minimum wage by {show(shortfall)}",
                      details={"shortfall": amount_str(round_money(shortfall, minor, "CEILING"), minor)})
        return MinimumWageCheck("WARN", True, earnings, entitlement, shortfall, None, (issue,),
                                f"{basis}: below minimum wage by {show(shortfall)}")
    explanation = f"{basis}: top-up {show(shortfall)}"
    line = make_line(MIN_WAGE_TOP_UP, EARNING, "TASK", shortfall, currency, minor_unit=minor, rounding="CEILING",
                     method="MIN_WAGE_FLOOR", explanation=explanation, units=hours, rate=rate,
                     sources=[{"type": "minimum_wage", "worked_hours": str(hours), "min_hourly_rate": str(rate)}])
    return MinimumWageCheck("TOP_UP", True, earnings, entitlement, shortfall, line, (), explanation)


# --------------------------------------------------------------------------- task pay


@dataclass(frozen=True)
class TaskItem:
    """One task's share for this employee, with what the cap did to it."""

    task_id: str
    attributed_on: date
    quantity: Decimal
    rate: Decimal
    rate_currency: str
    rate_source: str
    fx_rate: Optional[Decimal]
    split_percent: Decimal
    amount: Decimal  # rounded share in the pay currency
    paid_now: Decimal
    carried: Decimal
    forfeited: Decimal
    late: bool
    explanation: str


@dataclass(frozen=True)
class ExcludedTask:
    task_id: str
    reason: str


@dataclass(frozen=True)
class TaskPay:
    currency: str
    lines: Tuple[TimePayLine, ...]
    items: Tuple[TaskItem, ...]
    excluded: Tuple[ExcludedTask, ...]
    earned: Decimal  # current tasks, before the cap
    carried_in: Decimal
    paid: Decimal  # carried-in + current amounts paid now
    carried_out: Decimal
    forfeited: Decimal
    minimum_wage: Optional[MinimumWageCheck]
    document: str  # PAYSLIP, or INVOICE / SELF_BILLED_INVOICE for contractors (EC-TSK-09)
    issues: Tuple[Issue, ...] = field(default_factory=tuple)

    @property
    def total(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal(0))


@in_calc_context
def task_pay(
    tasks: Sequence[TaskRecord],
    policy: Any,
    *,
    employee_id: str,
    period_start: date,
    period_end: date,
    currency: str,
    enabled: Optional[DayRanges] = None,
    rate_card: Sequence[RateCardRow] = (),
    employee: EmployeeAttributes = EmployeeAttributes(),
    fx_rates: Optional[Mapping[str, Decimal]] = None,
    carried_in: Decimal = Decimal(0),
    minimum_wage: Optional[MinimumWageInput] = None,
    worker_class: str = "EMPLOYEE",
) -> TaskPay:
    """The TASK_EARNING line (and MIN_WAGE_TOP_UP) for one employee and period (3.4 §10, §17.3).

    ``period_start``/``period_end`` bound the attendance cycle that attribution dates fall in;
    ``enabled`` lists the dates ``payroll.task_pay.enabled`` is ON. ``fx_rates`` maps a rate
    currency to the pay-currency value of one unit. ``carried_in`` is the amount carried by
    TRUNCATE_AND_CARRY from the previous period; it is paid first, within this period's cap.
    With the floor ON and no ``minimum_wage`` input, the floor cannot be checked and the
    result carries the "not verified" warning instead of passing silently (EC-TSK-07).
    """
    minor = resolve_minor_unit(currency)
    check_ranges(enabled, "enabled")
    if period_end < period_start:
        raise TimePayError(INVALID_INPUT, "period_end is before period_start", "period_end")
    rules = parse_task_pay_policy(policy, currency)
    carry = require_decimal(carried_in, "carried_in", minimum=Decimal(0))
    if carry != round_money(carry, minor):
        raise TimePayError(INVALID_INPUT, f"carried_in has more decimals than {currency} allows", "carried_in")
    _check_tasks(tasks)
    issues: List[Issue] = []
    excluded: List[ExcludedTask] = []
    prepared: List[Tuple[TaskRecord, date, ResolvedRate, Optional[Decimal], Decimal, Decimal, Decimal]] = []
    for task in tasks:
        mine = [a for a in task.assignees if a.employee_id == employee_id]
        if not mine:
            excluded.append(ExcludedTask(task.task_id, NOT_ASSIGNED))
            continue
        validate_split(task.assignees, allow_split=rules.allow_split, task_id=task.task_id)
        if task.status != APPROVED or task.paid_in_run_id is not None:
            excluded.append(ExcludedTask(task.task_id, ALREADY_PAID if task.paid_in_run_id or task.status == "PAID"
                                         else NOT_APPROVED))
            continue
        attributed = task.approved_on if rules.attribution == "APPROVAL_DATE" else task.completed_on
        if attributed is None:
            issues.append(Issue("TASK_ATTRIBUTION_DATE_MISSING", BLOCKING,
                                f"An approved task has no {rules.attribution.replace('_', ' ').lower()}",
                                ref=task.task_id))
            excluded.append(ExcludedTask(task.task_id, BLOCKED))
            continue
        if attributed > period_end:
            excluded.append(ExcludedTask(task.task_id, NOT_YET_DUE))
            continue
        if not switch_on(attributed, enabled):
            excluded.append(ExcludedTask(task.task_id, SWITCH_OFF))
            continue
        if rules.requires_evidence and task.evidence_count <= 0:
            issues.append(Issue("TASK_EVIDENCE_MISSING", BLOCKING, "Evidence is required before a task is paid",
                                attributed, task.task_id))
            excluded.append(ExcludedTask(task.task_id, BLOCKED))
            continue
        rate, problem = resolve_task_rate(task, rules.rate_source, rate_card, task.completed_on or attributed, employee)
        if rate is None:
            assert problem is not None
            issues.append(problem)
            excluded.append(ExcludedTask(task.task_id, BLOCKED))
            continue
        fx = None
        if rate.currency != currency:
            fx_value = (fx_rates or {}).get(rate.currency)
            if fx_value is None:
                issues.append(Issue("TASK_FX_RATE_MISSING", BLOCKING,
                                    f"The task rate is in {rate.currency} and no FX rule converts it to {currency}",
                                    attributed, task.task_id, details={"code": CURRENCY_MISMATCH}))
                excluded.append(ExcludedTask(task.task_id, BLOCKED))
                continue
            fx = require_decimal(fx_value, f"fx_rates[{rate.currency}]", positive=True)
        quantity = task.quantity if task.accepted_quantity is None else task.accepted_quantity
        full = round_money(quantity * rate.rate * (fx if fx is not None else 1), minor)
        share = split_amount(full, task.assignees, currency)[employee_id] if len(task.assignees) > 1 else full
        prepared.append((task, attributed, rate, fx, quantity, mine[0].split_percent, share))

    prepared.sort(key=lambda item: (item[1], item[0].task_id))
    earned = sum((item[6] for item in prepared), Decimal(0))
    total = carry + earned
    cap = rules.cap_per_period
    room = total if cap is None or rules.cap_action == "EXCEPTION" else min(total, cap)
    if cap is not None and total > cap:
        excess = total - cap
        if rules.cap_action == "EXCEPTION":
            issues.append(Issue("TASK_CAP_EXCEEDED", BLOCKING,
                                f"Task pay {show(total)} exceeds the period cap of {show(cap)} by {show(excess)}; "
                                "resolve before approval", details={"excess": amount_str(excess, minor)}))
        elif rules.cap_action == "TRUNCATE":
            issues.append(Issue("TASK_CAP_TRUNCATED", WARNING,
                                f"Task pay truncated at the period cap of {show(cap)}; {show(excess)} is not paid",
                                details={"forfeited": amount_str(excess, minor)}))
        else:
            issues.append(Issue("TASK_CAP_CARRIED", INFO,
                                f"Task pay capped at {show(cap)}; {show(excess)} carried to the next period",
                                details={"carried": amount_str(excess, minor)}))

    carried_in_paid = min(carry, room)
    left = room - carried_in_paid
    carried_out = forfeited = Decimal(0)
    if carry > carried_in_paid:
        if rules.cap_action == "TRUNCATE_AND_CARRY":
            carried_out += carry - carried_in_paid
        else:
            forfeited += carry - carried_in_paid
    items: List[TaskItem] = []
    for task, attributed, rate, fx, quantity, percent, share in prepared:
        now = min(share, left)
        left -= now
        rest = share - now
        carried = rest if rules.cap_action == "TRUNCATE_AND_CARRY" else Decimal(0)
        lost = rest - carried
        carried_out += carried
        forfeited += lost
        late = attributed < period_start
        if late:
            issues.append(Issue("TASK_PAID_LATE", INFO, f"Attributed to {attributed.isoformat()}, before this period; "
                                "paid now as a late item", attributed, task.task_id))
        text = f"{show(quantity)} × {rate.explanation}"
        if fx is not None:
            text += f" × FX {show(fx)}"
        if percent != HUNDRED:
            text += f" × {show(percent)}%"
        text += f" = {show(share)}"
        if rest:
            text += f" ({show(now)} now, {show(carried)} carried)" if carried else f" ({show(now)} now, {show(lost)} over the cap)"
        items.append(TaskItem(task.task_id, attributed, quantity, rate.rate, rate.currency, rate.source, fx, percent,
                              share, now, carried, lost, late, text))
    paid = carried_in_paid + sum((item.paid_now for item in items), Decimal(0))

    lines: List[TimePayLine] = []
    if paid:
        sources: List[Mapping[str, str]] = []
        if carried_in_paid:
            sources.append({"type": "task_carry_forward", "amount": amount_str(carried_in_paid, minor)})
        sources += [{"type": "task", "id": item.task_id, "quantity": str(item.quantity), "rate": str(item.rate),
                     "rate_source": item.rate_source, "amount": amount_str(item.paid_now, minor),
                     "attributed_on": item.attributed_on.isoformat()} for item in items if item.paid_now]
        parts = ([f"carried in {show(carried_in_paid)}"] if carried_in_paid else []) + [item.explanation for item in items]
        explanation = "; ".join(parts)
        if cap is not None and total > cap and rules.cap_action != "EXCEPTION":
            explanation += f"; capped at {show(cap)}"
        lines.append(make_line(rules.earning_component, EARNING, "TASK", paid, currency, minor_unit=minor,
                               rounding="HALF_UP", method="RATE_X_UNITS", explanation=explanation, sources=sources))

    document = rules.contractor_document if worker_class == CONTRACTOR else "PAYSLIP"
    floor = None
    if minimum_wage is not None and worker_class != CONTRACTOR:
        floor = minimum_wage_check(paid, minimum_wage.worked_hours, mode=rules.minimum_wage_floor, currency=currency,
                                   min_hourly_rate=minimum_wage.min_hourly_rate, verified=minimum_wage.verified,
                                   pack_forces_top_up=minimum_wage.pack_forces_top_up,
                                   other_wages_counted=minimum_wage.other_wages_counted)
        issues.extend(floor.issues)
        if floor.line is not None:
            lines.append(floor.line)
    elif worker_class != CONTRACTOR and rules.minimum_wage_floor != "OFF" and (paid or items):
        issues.append(Issue("MIN_WAGE_NOT_VERIFIED", WARNING,
                            f"{NOT_VERIFIED_MESSAGE}: no worked hours or minimum rate were supplied",
                            details={"mode": rules.minimum_wage_floor}))
    return TaskPay(currency, tuple(lines), tuple(items), tuple(excluded), earned, carry, paid, carried_out, forfeited,
                   floor, document, tuple(issues))


# --------------------------------------------------------------------------- clawback


@dataclass(frozen=True)
class TaskClawback:
    task_id: str
    due: Decimal
    recovered: Decimal
    carried_forward: Decimal
    line: Optional[TimePayLine]
    issues: Tuple[Issue, ...]
    explanation: str


@in_calc_context
def task_clawback(task_id: str, amount: Decimal, policy: Any, *, currency: str,
                  room: Optional[Decimal]) -> TaskClawback:
    """Recover a reversed paid task as a negative TASK_EARNING within the protected net (EC-TSK-08).

    ``amount`` is what is still to recover (the paid amount, less earlier instalments);
    ``room`` is the protected-net room left in this run. MANUAL clawback only raises a review item.
    """
    minor = resolve_minor_unit(currency)
    rules = parse_task_pay_policy(policy, currency)
    due = require_decimal(amount, "amount", minimum=Decimal(0))
    if rules.clawback == "MANUAL":
        issue = Issue("TASK_CLAWBACK_MANUAL", INFO, f"Reversed task: {show(due)} to recover manually", ref=task_id)
        return TaskClawback(task_id, due, Decimal(0), due, None, (issue,), "Clawback is MANUAL; nothing deducted")
    outcome = protected_recovery(due, room, currency)
    issues: List[Issue] = []
    if room is None:
        issues.append(Issue("PROTECTED_NET_NOT_SET", BLOCKING,
                            "Clawback needs a protected net pay percentage; nothing recovered", ref=task_id))
    elif outcome.carried_forward:
        issues.append(Issue("TASK_CLAWBACK_PARTIAL", WARNING,
                            f"Only {show(outcome.recovered)} of {show(due)} recovered to protect net pay; "
                            f"{show(outcome.carried_forward)} carried to the next run", ref=task_id,
                            details={"carried_forward": amount_str(outcome.carried_forward, minor)}))
    line = None
    if outcome.recovered:
        line = make_line(rules.earning_component, EARNING, "TASK", -outcome.recovered, currency, minor_unit=minor,
                         rounding="HALF_UP", method="CLAWBACK",
                         explanation=f"Reversal of task {task_id}: {outcome.explanation}",
                         sources=[{"type": "task_reversal", "id": task_id}])
    return TaskClawback(task_id, due, outcome.recovered, outcome.carried_forward, line, tuple(issues),
                        outcome.explanation)

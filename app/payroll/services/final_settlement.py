"""Final settlement (full and final) of one employment period (3.5 §15; EC-COR-07, -08, -09, -12, -13, -14).

* ``open_case``: an employment period gets its end (the last working day, which must match a recorded
  termination date) and a termination type; one live case per employment period. The employment period
  starts at the latest REHIRE placement on or before the last day, else at the hire date, so a rehire after
  a settlement starts a new period and never touches the old case (EC-COR-09). DEATH names the payee (a
  nominee or legal heir, with evidence; never bank numbers: the payee's account goes through the bank
  account workflow, EC-COR-07); ABSCONDING is held: the settlement run gets a payment hold (EC-COR-12).
* The due date comes from a verified pack rule where one exists (a pack exposing
  ``final_settlement_due(last_working_day)`` whose usage is final); otherwise the company's configured
  deadline (``final_settlement_deadlines``, entity row first) with a "not verified" note, and the pack's
  own deadline text, if any, shown as unverified. No deadline at all is NOT_CONFIGURED, never invented.
  Cases past their due date are flagged overdue in lists (EC-COR-08).
* The checklist (``final_settlement_items``) is computed from live records and refreshed on request;
  decided items (CONFIRMED / WAIVED) are kept. Amounts are signed: positive is paid to the payee, negative
  is recovered. Salary to the last day, tax, loans, pending arrears/claims/inputs are calculated by the
  engine in the FINAL_SETTLEMENT run (loans are recovered within the protected net and the remainder
  becomes a receivable); the checklist shows them for confirmation. Leave encashment follows the
  LEAVE_PAY_POLICY (encashable types encashed on EXIT, the policy's rate formula evaluated on the
  employee's compensation rates, capped by the yearly maximum); a negative balance follows
  ``payroll.leave.negative_balance_on_exit`` (RECOVER / WRITE_OFF / EXCEPTION). Notice pay or a shortfall
  recovery is entered from the contract (no notice policy is assumed; a recovery needs the contract
  reference, EC-COR-13). Gratuity / end-of-service comes from the country pack only when the pack's usage
  is final (verified, or a fixture outside production); otherwise the item is BLOCKED "not verified" and
  needs an amount from a qualified source (never invented). An unreturned asset is deducted only with the
  employee's consent recorded by a payroll approver other than the case's creator, valued and capped at
  the asset's recorded value (EC-COR-14).
* ``create_settlement_run``: a READY case gets its FINAL_SETTLEMENT run (``runs.create_run``) for the
  period containing the last day; confirmed monetary items become system wage inputs of dedicated INPUT
  components (FS_*) targeted at that period and submitted through the normal input approval; a held case
  puts a PAYMENT hold on the run employee. The run follows the normal lifecycle; once it is finalized the
  case is SETTLED and ``payroll.final_settlement.finalized`` is emitted for the F&F statement (documents).

Services take (db, principal, ...) and never commit.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.core import clock
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.asset import Asset, AssetAssignment
from app.models.compensation import PayComponent
from app.models.corrections import (
    SETTLEMENT_ITEM_TYPES,
    TERMINATION_TYPES,
    ArrearsInstalment,
    ArrearsProposal,
    FinalSettlementCase,
    FinalSettlementDeadline,
    FinalSettlementItem,
)
from app.models.employee import Employee
from app.models.leave import LeaveType
from app.models.org import EmployeeAssignment, LegalEntity
from app.models.pay_org import PayGroup
from app.models.pay_runs import PayPeriod, PayrollRun, PayrollRunEmployee
from app.models.time_inputs import LeaveBalanceTransaction
from app.models.variable_inputs import Loan, ReimbursementClaim, WageInput
from app.payroll import audit as payroll_audit
from app.payroll import countries, packs
from app.payroll import formula as fm
from app.payroll import state_machines as sm
from app.payroll.money import CALC_CONTEXT, amount_str, decimal_str, is_storable, quantize_amount
from app.payroll.packs.base import FIXTURE_LABEL, NOT_VERIFIED_LABEL
from app.payroll.services import access, calendars, outbox, runs
from app.payroll.services import settings as settings_engine
from app.services.audit_log_service import AuditLogService

READ = "payroll:run:read"
EDIT = "payroll:run:create"
APPROVE = "payroll:run:approve"
INPUTS_EDIT = "payroll:inputs:edit"
AUDIT_MODULE = payroll_audit.PAYROLL_RUNS
FINALIZED_TOPIC = "payroll.final_settlement.finalized"

CASE_OPENED = "FINAL_SETTLEMENT_OPENED"
CASE_UPDATED = "FINAL_SETTLEMENT_UPDATED"
CASE_CANCELLED = "FINAL_SETTLEMENT_CANCELLED"
CHECKLIST_REFRESHED = "FINAL_SETTLEMENT_CHECKLIST_REFRESHED"
ITEM_CONFIRMED = "FINAL_SETTLEMENT_ITEM_CONFIRMED"
ITEM_WAIVED = "FINAL_SETTLEMENT_ITEM_WAIVED"
RUN_CREATED = "FINAL_SETTLEMENT_RUN_CREATED"
CASE_SETTLED = "FINAL_SETTLEMENT_SETTLED"
HOLD_RELEASED = "FINAL_SETTLEMENT_HOLD_RELEASED"
DEADLINE_SAVED = "FINAL_SETTLEMENT_DEADLINE_SAVED"

PAYEE_TYPES = ("NOMINEE", "LEGAL_HEIR")
DECIDED = ("CONFIRMED", "WAIVED", "NOT_APPLICABLE")
OPEN_ITEMS = ("PENDING", "BLOCKED")
LIVE_CASE = ("OPEN", "READY", "IN_RUN")
# Items paid or recovered through a wage input in the settlement run, with the sign their amount takes.
PAID = "PAY"
RECOVERED = "RECOVER"
EITHER = "EITHER"
MONETARY: Mapping[str, str] = {
    "NOTICE_PAY": EITHER, "LEAVE_ENCASHMENT": PAID, "NEGATIVE_LEAVE_BALANCE": RECOVERED, "GRATUITY": PAID,
    "STATUTORY_BONUS": PAID, "ASSET_RECOVERY": RECOVERED,
}
# Engine-calculated items: confirmed without an amount.
CALCULATED = ("SALARY_TO_LAST_DAY", "TAX", "PENDING_ARREARS", "PENDING_CLAIMS", "PENDING_INPUTS", "LOAN_RECOVERY")
# (code, name, kind, category) of the INPUT components settlement amounts are paid or recovered through.
FS_COMPONENTS: Mapping[Tuple[str, str], Tuple[str, str, str, str]] = {
    ("NOTICE_PAY", PAID): ("FS_NOTICE_PAY", "Notice pay", "EARNING", "SEVERANCE"),
    ("NOTICE_PAY", RECOVERED): ("FS_NOTICE_RECOVERY", "Notice shortfall recovery", "DEDUCTION", "RECOVERY"),
    ("LEAVE_ENCASHMENT", PAID): ("FS_LEAVE_ENCASHMENT", "Leave encashment on exit", "EARNING", "LEAVE_ENCASHMENT"),
    ("NEGATIVE_LEAVE_BALANCE", RECOVERED): ("FS_LEAVE_RECOVERY", "Negative leave balance recovery", "DEDUCTION",
                                            "RECOVERY"),
    ("GRATUITY", PAID): ("FS_GRATUITY", "Gratuity / end-of-service benefit", "EARNING", "GRATUITY"),
    ("STATUTORY_BONUS", PAID): ("FS_STATUTORY_BONUS", "Statutory bonus (pro rata)", "EARNING", "BONUS"),
    ("ASSET_RECOVERY", RECOVERED): ("FS_ASSET_RECOVERY", "Unreturned asset recovery", "DEDUCTION", "RECOVERY"),
}
ITEM_ORDER = {code: index for index, code in enumerate(SETTLEMENT_ITEM_TYPES)}
NOT_VERIFIED = NOT_VERIFIED_LABEL
ZERO = Decimal(0)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _today(db: Session, company_id: int) -> dt.date:
    return clock.company_today(db, company_id)


def _environment() -> str:
    return packs.environment_name(app_settings.environment)


def _text(value: Optional[str], limit: int = 1000) -> Optional[str]:
    cleaned = " ".join((value or "").split())
    return cleaned[:limit] or None


def _reason(value: Optional[str]) -> str:
    cleaned = _text(value)
    if cleaned is None:
        raise bad_request("Give a reason.", "REASON_REQUIRED")
    return cleaned


def _money(amount: Optional[Decimal], currency: str) -> Optional[dict]:
    if amount is None:
        return None
    return {"amount": amount_str(Decimal(amount), currency), "currency": currency}


def _audit(db: Session, principal: Optional[Principal], action: str, description: str, case: FinalSettlementCase,
           **details: Any) -> None:
    AuditLogService.log(db, principal, AUDIT_MODULE, action, description[:500], "final_settlement_case", case.id,
                        company_id=case.company_id, actor_type="user" if principal is not None else "system",
                        details=details)


def _check_version(row: Any, expected: Optional[int]) -> None:
    if expected is not None and row.row_version != expected:
        raise conflict("This record was changed by someone else. Reload and try again.", "ROW_VERSION_CONFLICT")


def employment_start(db: Session, employee: Employee, last_working_day: dt.date) -> dt.date:
    """The start of the employment period that ends on ``last_working_day``: the latest REHIRE placement on
    or before it, else the hire date (EC-COR-09: a rehire is a new employment period)."""
    rehire = (db.query(func.max(EmployeeAssignment.effective_from))
              .filter(EmployeeAssignment.company_id == employee.company_id,
                      EmployeeAssignment.employee_id == employee.id, EmployeeAssignment.is_primary.is_(True),
                      EmployeeAssignment.reason == "REHIRE", EmployeeAssignment.effective_from <= last_working_day)
              .scalar())
    return rehire if rehire is not None and rehire > employee.hire_date else employee.hire_date


# ---------------------------------------------------------------------------
# The case state machine (one transition function)
# ---------------------------------------------------------------------------
_CASE_TRANSITIONS: Mapping[Tuple[str, str], str] = {
    ("OPEN", "MARK_READY"): "READY",
    ("READY", "REOPEN_CHECKLIST"): "OPEN",
    ("READY", "CREATE_RUN"): "IN_RUN",
    ("IN_RUN", "RUN_CANCELLED"): "READY",
    ("IN_RUN", "RUN_FINALIZED"): "SETTLED",
    ("OPEN", "CANCEL"): "CANCELLED",
    ("READY", "CANCEL"): "CANCELLED",
}


def _move(db: Session, principal: Optional[Principal], case: FinalSettlementCase, action: str, *,
          reason: Optional[str] = None, **details: Any) -> FinalSettlementCase:
    """The only writer of ``final_settlement_cases.status`` (the caller holds the row lock)."""
    target = _CASE_TRANSITIONS.get((case.status, action))
    if target is None:
        raise conflict(f"A {case.status.lower()} settlement case cannot {action.lower().replace('_', ' ')}.",
                       "INVALID_TRANSITION")
    previous = case.status
    case.status = target
    now = clock.utcnow()
    if target == "SETTLED":
        case.settled_at = now
    elif target == "CANCELLED":
        case.cancelled_at, case.cancel_reason = now, reason
    case.row_version += 1
    db.flush()
    _audit(db, principal, {"CANCEL": CASE_CANCELLED, "RUN_FINALIZED": CASE_SETTLED}.get(action, CASE_UPDATED),
           f"Final settlement #{case.id}: {previous} -> {target} ({action})" + (f": {reason}" if reason else ""),
           case, transition=action, from_status=previous, to_status=target, reason=reason, **details)
    return case


# ---------------------------------------------------------------------------
# Loading with scope
# ---------------------------------------------------------------------------
def _load(db: Session, principal: Principal, case_id: int, permission: str, *, lock: bool = False
          ) -> FinalSettlementCase:
    """A case of the caller's tenant whose legal entity / pay group is in the permission's scope and whose
    employee was in the caller's employee scope on the last working day (403 without it, 404 otherwise)."""
    if not principal.has(permission):
        raise forbidden()
    query = db.query(FinalSettlementCase).filter(
        FinalSettlementCase.company_id == principal.company_id, FinalSettlementCase.id == case_id,
        access.run_scope_clause(principal, permission, FinalSettlementCase.legal_entity_id,
                                FinalSettlementCase.pay_group_id))
    if lock:
        query = query.with_for_update(of=FinalSettlementCase).populate_existing()
    case = query.first()
    if case is None or not _employee_visible(db, principal, permission, case):
        raise not_found("Final settlement")
    return case


def _employee_visible(db: Session, principal: Principal, permission: str, case: FinalSettlementCase) -> bool:
    from app.payroll.services.retro import employee_visible

    return employee_visible(db, principal, permission, case.employee_id, case.last_working_day,
                            case.last_working_day)


def _items(db: Session, case: FinalSettlementCase, *, lock: bool = False) -> List[FinalSettlementItem]:
    query = db.query(FinalSettlementItem).filter(FinalSettlementItem.company_id == case.company_id,
                                                 FinalSettlementItem.case_id == case.id)
    if lock:
        query = query.with_for_update().populate_existing()
    return sorted(query.all(), key=lambda i: (i.sort_order, i.id))


# ---------------------------------------------------------------------------
# Due date (EC-COR-08)
# ---------------------------------------------------------------------------
def _entity(db: Session, company_id: int, legal_entity_id: int) -> LegalEntity:
    return db.query(LegalEntity).filter(LegalEntity.company_id == company_id, LegalEntity.id == legal_entity_id).one()


def _add_working_days(start: dt.date, days: int, weekend: Iterable[int]) -> dt.date:
    off = set(weekend)
    current, added = start, 0
    while added < days:
        current += dt.timedelta(days=1)
        if current.weekday() not in off:
            added += 1
    return current


def company_deadline(db: Session, company_id: int, legal_entity_id: int) -> Optional[FinalSettlementDeadline]:
    rows = (db.query(FinalSettlementDeadline)
            .filter(FinalSettlementDeadline.company_id == company_id,
                    or_(FinalSettlementDeadline.legal_entity_id == legal_entity_id,
                        FinalSettlementDeadline.legal_entity_id.is_(None)))
            .all())
    rows.sort(key=lambda r: r.legal_entity_id is None)
    return rows[0] if rows else None


def due_date_for(db: Session, company_id: int, legal_entity_id: int, last_working_day: dt.date
                 ) -> Tuple[Optional[dt.date], str, Optional[str]]:
    """(due date, basis, note): a verified pack rule, else the company deadline (not verified), else none."""
    entity = _entity(db, company_id, legal_entity_id)
    country = (entity.country_code or "").upper()
    env = _environment()
    notes: List[str] = []
    for pack in packs.packs_for_country(country, on=last_working_day, environment=env) if country else ():
        verdict = packs.usage(pack.meta, environment=env, period=(last_working_day, last_working_day))
        rule = getattr(pack, "final_settlement_due", None)
        if verdict.final and callable(rule):
            due = rule(last_working_day)
            if isinstance(due, dt.date):
                label = "FIXTURE — not law: " if verdict.mode == packs.FIXTURE else ""
                return due, "PACK_VERIFIED", f"{label}Due date from pack {pack.version}."
        text = pack.payment_rules().final_settlement_deadline
        if text:
            notes.append(f"Pack {pack.version} says \"{text}\" — {NOT_VERIFIED.lower()}, not applied.")
    deadline = company_deadline(db, company_id, legal_entity_id)
    if deadline is not None:
        if deadline.day_basis == "WORKING_DAYS":
            weekend = countries.default_weekend(country) if country and countries.find(country) else {5, 6}
            due = _add_working_days(last_working_day, deadline.days, weekend)
        else:
            due = last_working_day + dt.timedelta(days=deadline.days)
        unit = "working days" if deadline.day_basis == "WORKING_DAYS" else "calendar days"
        note = f"Company deadline: {deadline.days} {unit} after the last working day (not verified against the law)."
        return due, "COMPANY_DEADLINE", " ".join([note, *notes])
    note = "No verified pack rule and no company deadline: configure a final-settlement deadline."
    return None, "NOT_CONFIGURED", " ".join([note, *notes])


def is_overdue(case: FinalSettlementCase, today: dt.date) -> bool:
    return case.status in LIVE_CASE and case.due_date is not None and case.due_date < today


# ---------------------------------------------------------------------------
# Checklist
# ---------------------------------------------------------------------------
@dataclass
class _Facts:
    case: FinalSettlementCase
    employee: Employee
    group: PayGroup
    entity: LegalEntity
    period: Optional[PayPeriod]
    rates: Dict[str, Decimal]
    rates_issue: Optional[str]


def _rates(db: Session, case: FinalSettlementCase) -> Tuple[Dict[str, Decimal], Optional[str]]:
    """Monthly component rates of the salary in force on the last working day (for encashment formulas and
    the pack's gratuity base)."""
    from app.payroll.services import compensation

    row = compensation.in_force_row(db, case.company_id, case.employee_id, case.last_working_day)
    if row is None:
        return {}, "The employee has no salary in force on the last working day."
    try:
        evaluation = compensation.evaluate_assignment(db, row, on=case.last_working_day)
    except AppError as exc:
        return {}, f"The salary could not be evaluated: {exc.detail}"
    if not evaluation.ok:
        return {}, "The salary structure has blocking issues on the last working day."
    return {code: Decimal(value) for code, value in evaluation.rates.items()}, None


def _facts(db: Session, case: FinalSettlementCase) -> _Facts:
    employee = db.query(Employee).filter(Employee.company_id == case.company_id, Employee.id == case.employee_id).one()
    group = calendars.load_pay_group(db, case.company_id, case.pay_group_id)
    entity = _entity(db, case.company_id, case.legal_entity_id)
    period = calendars.period_containing(db, case.company_id, case.pay_group_id, case.last_working_day)
    rates, issue = _rates(db, case)
    return _Facts(case, employee, group, entity, period, rates, issue)


def _setting(db: Session, case: FinalSettlementCase, key: str) -> Any:
    try:
        return settings_engine.resolve(db, case.company_id, key, employee_id=case.employee_id,
                                       on=case.last_working_day).value
    except settings_engine.SettingConfigError:
        return None


def _issue(code: str, message: str) -> dict:
    return {"code": code, "message": message}


@dataclass
class Proposed:
    """A computed checklist item; ``status`` PENDING, BLOCKED or NOT_APPLICABLE."""

    item_type: str
    status: str = "PENDING"
    source_key: str = ""
    amount: Optional[Decimal] = None
    units: Optional[Decimal] = None
    rate: Optional[Decimal] = None
    details: Optional[dict] = None
    issues: Tuple[dict, ...] = ()
    requires_consent: bool = False
    note: Optional[str] = None


def _salary_item(db: Session, facts: _Facts) -> Proposed:
    case = facts.case
    if facts.period is None:
        return Proposed("SALARY_TO_LAST_DAY", "BLOCKED", issues=(_issue(
            "NO_PAY_PERIOD", "No pay period of the pay group contains the last working day: generate the calendar."),))
    paid = (db.query(PayrollRun.number, PayrollRun.status)
            .join(PayrollRunEmployee, and_(PayrollRunEmployee.company_id == PayrollRun.company_id,
                                           PayrollRunEmployee.run_id == PayrollRun.id))
            .filter(PayrollRun.company_id == case.company_id, PayrollRun.pay_period_id == facts.period.id,
                    PayrollRun.run_type == "REGULAR", PayrollRun.status != "CANCELLED",
                    PayrollRunEmployee.employee_id == case.employee_id,
                    PayrollRunEmployee.status.in_(("PENDING", "CALCULATED", "ON_HOLD", "ERROR")))
            .first())
    details = {"period": {"id": facts.period.id, "start": facts.period.period_start.isoformat(),
                          "end": facts.period.period_end.isoformat()},
               "paid_to": case.last_working_day.isoformat()}
    if paid is not None:
        details["regular_run"] = {"number": paid.number, "status": paid.status}
        return Proposed("SALARY_TO_LAST_DAY", "PENDING", details=details, note=(
            f"Salary of the exit period is in regular run {paid.number}; the settlement run pays only the settlement "
            "items."))
    return Proposed("SALARY_TO_LAST_DAY", "PENDING", details=details,
                    note="Salary up to the last working day is prorated in the settlement run.")


def _notice_item(facts: _Facts) -> Proposed:
    case = facts.case
    served = None
    if case.notice_given_on is not None:
        served = (case.last_working_day - case.notice_given_on).days + 1
    details = {"notice_given_on": case.notice_given_on.isoformat() if case.notice_given_on else None,
               "days_served": served, "termination_type": case.termination_type}
    return Proposed("NOTICE_PAY", "PENDING", details=details, note=(
        "Enter the contractual notice: served (nothing), a buyout paid to the employee, or a shortfall recovered "
        "where lawful and in the contract (contract reference required)."))


def _leave_types(db: Session, company_id: int) -> Dict[str, LeaveType]:
    return {lt.code: lt for lt in db.query(LeaveType).filter(LeaveType.company_id == company_id).all()}


def leave_balance(db: Session, company_id: int, employee_id: int, leave_type: LeaveType, on: dt.date) -> Decimal:
    """Entitlement minus use of the year of ``on`` (negative when more was taken than earned)."""
    adjusted = used = ZERO
    for reason, delta in (
        db.query(LeaveBalanceTransaction.reason, func.coalesce(func.sum(LeaveBalanceTransaction.delta), 0))
        .filter(LeaveBalanceTransaction.company_id == company_id, LeaveBalanceTransaction.employee_id == employee_id,
                LeaveBalanceTransaction.leave_type == leave_type.code,
                LeaveBalanceTransaction.effective_date >= dt.date(on.year, 1, 1),
                LeaveBalanceTransaction.effective_date <= dt.date(on.year, 12, 31))
        .group_by(LeaveBalanceTransaction.reason)
    ):
        if reason == "USE":
            used -= Decimal(delta)
        else:
            adjusted += Decimal(delta)
    return Decimal(leave_type.annual_quota or 0) + adjusted - used


def _evaluate_rate(formula: str, facts: _Facts) -> Decimal:
    """A per-day rate formula of the LEAVE_PAY_POLICY on the monthly rates of the salary in force."""
    currency = facts.group.currency
    rates = {code: fm.money(value, currency) for code, value in facts.rates.items()}
    ctx = fm.EvalContext.build(currency, components=rates, rates=rates)
    result = fm.evaluate(fm.parse(formula), ctx)
    value = result.value
    amount = getattr(value, "amount", value)
    if not isinstance(amount, Decimal):
        raise ValueError("the formula does not give an amount")
    return amount


def _leave_items(db: Session, facts: _Facts) -> List[Proposed]:
    from app.payroll.services import policies

    case = facts.case
    currency = facts.group.currency
    types = _leave_types(db, case.company_id)
    encash_on = _setting(db, case, "payroll.leave.encashment.enabled") is True
    found = policies.resolve_for_employee(db, case.company_id, "LEAVE_PAY_POLICY", case.employee_id,
                                          case.last_working_day)
    rows = {}
    if found.get("status") == "RESOLVED":
        rows = {row.get("leave_type_code"): row for row in (found["version"]["fields"] or {}).get("rows", [])}
    negative_mode = _setting(db, case, "payroll.leave.negative_balance_on_exit") or "EXCEPTION"
    result: List[Proposed] = []
    for code in sorted(types):
        lt = types[code]
        if getattr(lt, "is_unlimited", False):
            continue
        balance = leave_balance(db, case.company_id, case.employee_id, lt, case.last_working_day)
        row = rows.get(code) or {}
        details = {"leave_type": code, "balance_days": decimal_str(balance), "policy": found.get("policy")}
        if balance > 0:
            if not encash_on:
                result.append(Proposed("LEAVE_ENCASHMENT", "PENDING", source_key=code, units=balance, details=details,
                                       issues=(_issue("ENCASHMENT_DECISION_REQUIRED",
                                                      "Leave encashment is off: decide whether the balance is paid "
                                                      "(enter the amount) or waive the item with a reason."),)))
                continue
            if not row.get("encashable") or "EXIT" not in (row.get("encash_on") or ()):
                result.append(Proposed("LEAVE_ENCASHMENT", "NOT_APPLICABLE", source_key=code, units=balance,
                                       details=details, note="This leave type is not encashed on exit by the policy."))
                continue
            days = balance
            cap = row.get("encashment_max_days_per_year")
            if cap is not None and days > Decimal(str(cap)):
                days = Decimal(str(cap))
                details["capped_at_days"] = decimal_str(days)
            formula = row.get("encashment_formula")
            issues: Tuple[dict, ...] = ()
            amount = rate = None
            if not formula:
                issues = (_issue("ENCASHMENT_FORMULA_MISSING", "The leave pay policy has no encashment rate formula."),)
            elif facts.rates_issue:
                issues = (_issue("SALARY_NOT_AVAILABLE", facts.rates_issue),)
            else:
                try:
                    rate = _evaluate_rate(str(formula), facts)
                    with localcontext(CALC_CONTEXT):
                        amount = quantize_amount(rate * days, currency)
                    details["formula"] = formula
                except (fm.FormulaError, ValueError, ArithmeticError) as exc:
                    issues = (_issue("ENCASHMENT_FORMULA_FAILED", f"The encashment formula failed: {exc}"),)
            result.append(Proposed("LEAVE_ENCASHMENT", "BLOCKED" if issues else "PENDING", source_key=code,
                                   amount=amount, units=days, rate=rate, details=details, issues=issues))
        elif balance < 0:
            details["negative_balance_on_exit"] = negative_mode
            if negative_mode == "WRITE_OFF":
                result.append(Proposed("NEGATIVE_LEAVE_BALANCE", "NOT_APPLICABLE", source_key=code, units=balance,
                                       details=details, note="Written off (payroll.leave.negative_balance_on_exit)."))
                continue
            if negative_mode == "EXCEPTION":
                result.append(Proposed("NEGATIVE_LEAVE_BALANCE", "BLOCKED", source_key=code, units=balance,
                                       details=details, issues=(_issue(
                                           "NEGATIVE_LEAVE_BALANCE_REVIEW",
                                           "Negative leave balance on exit: review it and enter a recovery or waive "
                                           "the item with a reason."),)))
                continue
            formula = row.get("encashment_formula")
            amount = rate = None
            issues = ()
            if not formula or facts.rates_issue:
                issues = (_issue("RECOVERY_RATE_REQUIRED",
                                 "No daily rate is defined for this leave type: enter the recovery amount."),)
            else:
                try:
                    rate = _evaluate_rate(str(formula), facts)
                    with localcontext(CALC_CONTEXT):
                        amount = quantize_amount(rate * balance, currency)
                except (fm.FormulaError, ValueError, ArithmeticError) as exc:
                    issues = (_issue("RECOVERY_RATE_FAILED", f"The rate formula failed: {exc}"),)
            result.append(Proposed("NEGATIVE_LEAVE_BALANCE", "BLOCKED" if issues else "PENDING", source_key=code,
                                   amount=amount, units=balance, rate=rate, details=details, issues=issues,
                                   note="Recovered within the protected net pay."))
    return result


def _implements(pack: Any, name: str) -> bool:
    """Whether the pack's class defines ``name`` itself (BasePack's defaults are empty placeholders)."""
    from app.payroll.packs.base import BasePack

    own = getattr(type(pack), name, None)
    return own is not None and own is not getattr(BasePack, name, None)


def _pack_context(db: Session, pack: Any, facts: _Facts, period: Tuple[dt.date, dt.date]) -> Any:
    """The frozen facts the pack's termination rule reads: its scheme switches and the employment start."""
    from app.payroll.packs.base import PackContext

    case = facts.case
    values: Dict[str, Any] = {}
    for scheme in pack.schemes():
        value = _setting(db, case, scheme.switch_key)
        if value is not None:
            values[scheme.switch_key] = value
    return PackContext(version=pack.meta, currency=facts.group.currency, period_start=period[0], period_end=period[1],
                       settings=values, employee={"date_of_joining": case.employment_start},
                       final_settlement=True)


def _gratuity_item(db: Session, facts: _Facts) -> Proposed:
    case = facts.case
    country = (facts.entity.country_code or "").upper()
    env = _environment()
    candidates = packs.packs_for_country(country, on=case.last_working_day, environment=env) if country else ()
    details: Dict[str, Any] = {"country": country or None, "service_start": case.employment_start.isoformat(),
                               "service_end": case.last_working_day.isoformat(),
                               "termination_type": case.termination_type}
    for pack in candidates:
        rule = getattr(pack, "termination_entitlements", None)
        if not callable(rule) or not _implements(pack, "termination_entitlements"):
            continue  # the pack has no end-of-service rule: never read "no rule" as "nothing due"
        verdict = packs.usage(pack.meta, environment=env, period=(case.last_working_day, case.last_working_day))
        details["pack"] = {"version": pack.version, "usage": verdict.mode}
        if not verdict.final:
            return Proposed("GRATUITY", "BLOCKED", details=details, issues=(_issue(
                "GRATUITY_NOT_VERIFIED",
                f"{NOT_VERIFIED}: the end-of-service rule of pack {pack.version} is not verified, so no amount is "
                "calculated. Enter the amount from a qualified source or waive the item with a reason."),))
        if facts.rates_issue or "BASIC" not in facts.rates:
            return Proposed("GRATUITY", "BLOCKED", details=details, issues=(_issue(
                "SALARY_NOT_AVAILABLE", facts.rates_issue or "No BASIC rate in the salary in force."),))
        ctx = _pack_context(db, pack, facts, (case.last_working_day, case.last_working_day))
        separation = {"last_basic_monthly": facts.rates["BASIC"], "service_start": case.employment_start,
                      "service_end": case.last_working_day, "unpaid_absence_days": 0,
                      "termination_type": case.termination_type}
        lines = rule(separation, ctx)
        total = sum((line.amount for line in lines if line.kind == "EARNING"), ZERO)
        details["explanation"] = "; ".join(line.explanation for line in lines if line.explanation) or None
        if verdict.mode == packs.FIXTURE:
            details["label"] = FIXTURE_LABEL
        if total <= 0:
            return Proposed("GRATUITY", "NOT_APPLICABLE", details=details,
                            note="The pack's rule gives no end-of-service benefit for this separation.")
        return Proposed("GRATUITY", "PENDING", amount=quantize_amount(total, facts.group.currency), details=details)
    return Proposed("GRATUITY", "PENDING", details=details, issues=(_issue(
        "NO_PACK_RULE", "No country pack rule for gratuity / end-of-service here: decide from the contract and the "
                        "law (enter the amount) or waive the item with a reason."),))


def _statutory_bonus_item(facts: _Facts) -> Proposed:
    return Proposed("STATUTORY_BONUS", "NOT_APPLICABLE",
                    note="The country pack defines no statutory bonus for exits; nothing is assumed.")


def _pending_arrears_item(db: Session, facts: _Facts) -> Proposed:
    case = facts.case
    rows = (db.query(ArrearsInstalment.amount, ArrearsInstalment.currency)
            .join(ArrearsProposal, and_(ArrearsProposal.company_id == ArrearsInstalment.company_id,
                                        ArrearsProposal.id == ArrearsInstalment.proposal_id))
            .filter(ArrearsInstalment.company_id == case.company_id, ArrearsInstalment.employee_id == case.employee_id,
                    ArrearsInstalment.status == "PLANNED", ArrearsProposal.status == "APPROVED")
            .all())
    pending = (db.query(func.count(ArrearsProposal.id))
               .filter(ArrearsProposal.company_id == case.company_id, ArrearsProposal.employee_id == case.employee_id,
                       ArrearsProposal.status == "PENDING_APPROVAL").scalar())
    totals: Dict[str, Decimal] = {}
    for amount, currency in rows:
        totals[currency] = totals.get(currency, ZERO) + Decimal(amount)
    details = {"approved_outstanding": {c: amount_str(a, c) for c, a in sorted(totals.items())},
               "pending_approval": pending}
    issues = (_issue("ARREARS_PENDING_APPROVAL", f"{pending} arrears proposal(s) still await approval."),) if pending \
        else ()
    status = "BLOCKED" if pending else ("PENDING" if totals else "NOT_APPLICABLE")
    return Proposed("PENDING_ARREARS", status, details=details, issues=issues,
                    note="Approved arrears and recoveries are settled in the settlement run.")


def _pending_claims_item(db: Session, facts: _Facts) -> Proposed:
    case = facts.case
    rows = (db.query(ReimbursementClaim.status, func.count(ReimbursementClaim.id))
            .filter(ReimbursementClaim.company_id == case.company_id,
                    ReimbursementClaim.employee_id == case.employee_id,
                    ReimbursementClaim.status.in_(("SUBMITTED", "APPROVED", "SCHEDULED")))
            .group_by(ReimbursementClaim.status).all())
    counts = {status: n for status, n in rows}
    issues = (_issue("CLAIMS_PENDING_DECISION", f"{counts['SUBMITTED']} claim(s) still await a decision."),) \
        if counts.get("SUBMITTED") else ()
    status = "BLOCKED" if issues else ("PENDING" if counts else "NOT_APPLICABLE")
    return Proposed("PENDING_CLAIMS", status, details={"claims": counts}, issues=issues,
                    note="Approved claims paid with payroll are included in the settlement run.")


def _pending_inputs_item(db: Session, facts: _Facts) -> Proposed:
    case = facts.case
    rows = (db.query(WageInput.status, func.count(WageInput.id))
            .filter(WageInput.company_id == case.company_id, WageInput.employee_id == case.employee_id,
                    WageInput.status.in_(("DRAFT", "PENDING_APPROVAL", "APPROVED")),
                    ~WageInput.component_code.like("FS\\_%", escape="\\"))
            .group_by(WageInput.status).all())
    counts = {status: n for status, n in rows}
    undecided = counts.get("DRAFT", 0) + counts.get("PENDING_APPROVAL", 0)
    issues = (_issue("INPUTS_PENDING_DECISION", f"{undecided} wage input(s) are not decided yet."),) if undecided else ()
    status = "BLOCKED" if issues else ("PENDING" if counts else "NOT_APPLICABLE")
    return Proposed("PENDING_INPUTS", status, details={"inputs": counts}, issues=issues,
                    note="Approved inputs targeting the exit period are included in the settlement run.")


def _loan_item(db: Session, facts: _Facts) -> Proposed:
    from app.payroll.services import loans

    case = facts.case
    rows = (db.query(Loan).filter(Loan.company_id == case.company_id, Loan.employee_id == case.employee_id,
                                  Loan.status.in_(("ACTIVE", "PAUSED"))).order_by(Loan.id).all())
    if not rows:
        return Proposed("LOAN_RECOVERY", "NOT_APPLICABLE", details={"loans": []})
    items = []
    for loan in rows:
        items.append({"loan_id": loan.id, "kind": loan.kind, "status": loan.status,
                      "outstanding": _money(loans.balance(db, loan), loan.currency),
                      "recover_balance_on_exit": bool(loan.recover_balance_on_exit)})
    return Proposed("LOAN_RECOVERY", "PENDING", details={"loans": items}, note=(
        "Outstanding balances are recovered in the settlement run within the protected net pay; whatever cannot be "
        "recovered becomes a receivable."))


def _asset_items(db: Session, facts: _Facts) -> List[Proposed]:
    case = facts.case
    rows = (db.query(AssetAssignment, Asset)
            .join(Asset, and_(Asset.company_id == AssetAssignment.company_id, Asset.id == AssetAssignment.asset_id))
            .filter(AssetAssignment.company_id == case.company_id, AssetAssignment.employee_id == case.employee_id,
                    AssetAssignment.returned_at.is_(None))
            .order_by(AssetAssignment.id).all())
    result = []
    for assignment, asset in rows:
        value = Decimal(asset.purchase_cost) if asset.purchase_cost is not None else None
        details = {"asset_id": asset.id, "asset_tag": asset.asset_tag, "name": asset.name,
                   "assignment_id": assignment.id, "valuation_cap": decimal_str(value) if value is not None else None}
        issues = () if value is not None else (_issue(
            "ASSET_VALUE_UNKNOWN", "The asset has no recorded value; a deduction cannot be valued and capped."),)
        result.append(Proposed("ASSET_RECOVERY", "PENDING", source_key=f"assignment:{assignment.id}", details=details,
                               issues=issues, requires_consent=True, note=(
                                   "Deduct only with the employee's consent and approval; otherwise recover the asset "
                                   "and waive this item.")))
    return result


def _tax_item(facts: _Facts) -> Proposed:
    return Proposed("TAX", "NOT_APPLICABLE",
                    note="Withholding on exit payments (including exemptions) is calculated by the country pack in "
                         "the settlement run; without a verified pack no statutory lines are produced.")


def compute_checklist(db: Session, case: FinalSettlementCase) -> List[Proposed]:
    facts = _facts(db, case)
    proposed: List[Proposed] = [
        _salary_item(db, facts), _notice_item(facts), *_leave_items(db, facts), _gratuity_item(db, facts),
        _statutory_bonus_item(facts), _pending_arrears_item(db, facts), _pending_claims_item(db, facts),
        _pending_inputs_item(db, facts), _loan_item(db, facts), *_asset_items(db, facts), _tax_item(facts),
    ]
    return proposed


def _store(db: Session, case: FinalSettlementCase, proposed: Sequence[Proposed]) -> Dict[str, int]:
    """Insert new items, update undecided ones, retire undecided items no longer applicable; decided items stay."""
    existing = {(i.item_type, i.source_key): i for i in _items(db, case, lock=True)}
    seen = set()
    counts = {"added": 0, "updated": 0, "retired": 0}
    for p in proposed:
        key = (p.item_type, p.source_key)
        seen.add(key)
        row = existing.get(key)
        values = dict(status=p.status, amount=p.amount, units=p.units, rate=p.rate, details=p.details or {},
                      issues=list(p.issues), requires_consent=p.requires_consent, note=p.note,
                      sort_order=ITEM_ORDER[p.item_type] * 100)
        if row is None:
            db.add(FinalSettlementItem(company_id=case.company_id, case_id=case.id, employee_id=case.employee_id,
                                       item_type=p.item_type, source_key=p.source_key, currency=case.currency,
                                       row_version=1, **values))
            counts["added"] += 1
        elif row.status in OPEN_ITEMS or (row.status == "NOT_APPLICABLE" and row.decided_by_user_id is None):
            for name, value in values.items():
                setattr(row, name, value)
            row.row_version += 1
            counts["updated"] += 1
    for key, row in existing.items():
        if key not in seen and row.status in OPEN_ITEMS:
            row.status, row.note = "NOT_APPLICABLE", "No longer applies after the checklist was refreshed."
            row.issues = []
            row.row_version += 1
            counts["retired"] += 1
    db.flush()
    return counts


def _refresh_readiness(db: Session, principal: Optional[Principal], case: FinalSettlementCase) -> None:
    open_count = sum(1 for i in _items(db, case) if i.status in OPEN_ITEMS)
    if case.status == "OPEN" and open_count == 0:
        _move(db, principal, case, "MARK_READY")
    elif case.status == "READY" and open_count:
        _move(db, principal, case, "REOPEN_CHECKLIST", open_items=open_count)


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CaseData:
    employee_id: int
    termination_type: str
    last_working_day: dt.date
    notice_given_on: Optional[dt.date] = None
    payee: Optional[Mapping[str, Any]] = None
    hold_reason: Optional[str] = None
    note: Optional[str] = None


def _clean_payee(termination_type: str, payee: Optional[Mapping[str, Any]]) -> Optional[dict]:
    if termination_type != "DEATH":
        if payee:
            raise bad_request("A separate payee is recorded only when the employee has died.", "PAYEE_NOT_ALLOWED")
        return None
    if not payee:
        raise bad_request("Name the nominee or legal heir who receives the settlement.", "PAYEE_REQUIRED")
    kind = str(payee.get("type") or "")
    if kind not in PAYEE_TYPES:
        raise bad_request("The payee is a NOMINEE or a LEGAL_HEIR.", "INVALID_PAYEE")
    name = _text(payee.get("name"), 150)
    relationship = _text(payee.get("relationship"), 60)
    evidence = _text(payee.get("evidence_ref"), 300)
    if not name or not relationship or not evidence:
        raise bad_request("The payee needs a name, a relationship and an evidence reference (for example the "
                          "nomination or succession document).", "PAYEE_INCOMPLETE")
    return {"type": kind, "name": name, "relationship": relationship, "evidence_ref": evidence}


def open_case(db: Session, principal: Principal, data: CaseData) -> FinalSettlementCase:
    if not principal.has(EDIT):
        raise forbidden()
    if data.termination_type not in TERMINATION_TYPES:
        raise bad_request(f"termination_type is one of {', '.join(TERMINATION_TYPES)}.", "INVALID_TERMINATION_TYPE")
    cid = principal.company_id
    access.ensure_employee_in_scope(db, principal, EDIT, data.employee_id, data.last_working_day,
                                    data.last_working_day)
    employee = (db.query(Employee).filter(Employee.company_id == cid, Employee.id == data.employee_id)
                .with_for_update().populate_existing().one())
    if principal.employee_id is not None and principal.employee_id == employee.id:
        raise forbidden("You cannot settle your own employment.")
    if employee.termination_date is not None and employee.termination_date != data.last_working_day:
        raise conflict(f"The recorded last working day is {employee.termination_date.isoformat()}.",
                       "LAST_DAY_MISMATCH")
    if data.notice_given_on is not None and data.notice_given_on > data.last_working_day:
        raise bad_request("Notice cannot be given after the last working day.", "INVALID_NOTICE_DATE")
    assignment = settings_engine.primary_assignment(db, cid, employee.id, data.last_working_day)
    if assignment is None:
        raise conflict("The employee has no placement on the last working day.", "NOT_PLACED")
    if assignment.pay_group_id is None:
        raise conflict("The employee is in no pay group on the last working day.", "NO_PAY_GROUP")
    if not access.target_scope(principal, EDIT).covers(assignment.legal_entity_id, assignment.pay_group_id):
        raise not_found("Employee")
    start = employment_start(db, employee, data.last_working_day)
    if data.last_working_day < start:
        raise bad_request("The last working day is before the start of the employment period.", "INVALID_LAST_DAY")
    payee = _clean_payee(data.termination_type, data.payee)
    hold = _text(data.hold_reason, 500)
    if data.termination_type == "ABSCONDING":
        hold = hold or "Absconding: payment held until the case is resolved (EC-COR-12)."
    group = calendars.load_pay_group(db, cid, assignment.pay_group_id)
    period = calendars.period_containing(db, cid, group.id, data.last_working_day)
    due, basis, due_note = due_date_for(db, cid, assignment.legal_entity_id, data.last_working_day)
    case = FinalSettlementCase(
        company_id=cid, employee_id=employee.id, employee_assignment_id=assignment.id,
        legal_entity_id=assignment.legal_entity_id, pay_group_id=group.id, pay_period_id=period.id if period else None,
        employment_start=start, last_working_day=data.last_working_day, notice_given_on=data.notice_given_on,
        termination_type=data.termination_type, currency=group.currency, status="OPEN", held=hold is not None,
        hold_reason=hold, payee=payee, due_date=due, due_basis=basis, due_note=due_note, note=_text(data.note),
        created_by_user_id=principal.user_id, row_version=1)
    try:
        with db.begin_nested():
            db.add(case)
            db.flush()
    except IntegrityError as exc:
        if "uq_final_settlement_cases_employment" in str(exc.orig):
            raise conflict("This employment period already has a final settlement case.", "DUPLICATE_CASE")
        raise
    counts = _store(db, case, compute_checklist(db, case))
    _audit(db, principal, CASE_OPENED, f"Opened final settlement #{case.id} for employee #{employee.id} "
           f"({data.termination_type}, last day {data.last_working_day.isoformat()})", case,
           employee_id=employee.id, termination_type=data.termination_type,
           last_working_day=data.last_working_day.isoformat(), employment_start=start.isoformat(),
           due_date=due.isoformat() if due else None, due_basis=basis, held=case.held,
           payee_type=payee["type"] if payee else None, items=counts)
    _refresh_readiness(db, principal, case)
    return case


def refresh_checklist(db: Session, principal: Principal, case_id: int, *, expected_version: Optional[int] = None
                      ) -> FinalSettlementCase:
    case = _load(db, principal, case_id, EDIT, lock=True)
    _check_version(case, expected_version)
    if case.status not in ("OPEN", "READY"):
        raise conflict(f"The checklist of a {case.status.lower()} case is frozen.", "CASE_LOCKED")
    case.due_date, case.due_basis, case.due_note = due_date_for(db, case.company_id, case.legal_entity_id,
                                                                case.last_working_day)
    counts = _store(db, case, compute_checklist(db, case))
    case.row_version += 1
    db.flush()
    _audit(db, principal, CHECKLIST_REFRESHED, f"Refreshed the checklist of final settlement #{case.id}", case,
           items=counts, due_date=case.due_date.isoformat() if case.due_date else None, due_basis=case.due_basis)
    _refresh_readiness(db, principal, case)
    return case


def cancel_case(db: Session, principal: Principal, case_id: int, reason: str, *,
                expected_version: Optional[int] = None) -> FinalSettlementCase:
    case = _load(db, principal, case_id, EDIT, lock=True)
    _check_version(case, expected_version)
    case = _move(db, principal, case, "CANCEL", reason=_reason(reason))
    if case.termination_type == "DEATH":
        from app.payroll.services import nominees

        nominees.on_case_cancelled(db, principal, case)  # nominees end, their accounts are deactivated
    return case


def release_hold(db: Session, principal: Principal, case_id: int, note: str, *,
                 expected_version: Optional[int] = None) -> FinalSettlementCase:
    """The reason for holding the settlement is resolved (EC-COR-12); a hold on a run employee is released in
    the run (POST runs/{id}/employees/{eid}/release)."""
    case = _load(db, principal, case_id, APPROVE, lock=True)
    _check_version(case, expected_version)
    if not case.held:
        raise conflict("The settlement is not held.", "NOT_HELD")
    if principal.user_id == case.created_by_user_id:
        raise forbidden("Another approver releases a held settlement.")
    previous = case.hold_reason
    case.held, case.hold_reason = False, None
    case.row_version += 1
    db.flush()
    _audit(db, principal, HOLD_RELEASED, f"Released the hold on final settlement #{case.id}", case,
           previous_reason=previous, note=_reason(note))
    return case


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ItemDecision:
    amount: Optional[Decimal] = None          # signed: positive paid, negative recovered
    currency: Optional[str] = None
    units: Optional[Decimal] = None
    rate: Optional[Decimal] = None
    direction: Optional[str] = None           # PAY | RECOVER, with units x rate
    note: Optional[str] = None
    consent_ref: Optional[str] = None
    contract_ref: Optional[str] = None
    expected_version: Optional[int] = None


def _load_item(db: Session, case: FinalSettlementCase, item_id: int) -> FinalSettlementItem:
    item = (db.query(FinalSettlementItem)
            .filter(FinalSettlementItem.company_id == case.company_id, FinalSettlementItem.case_id == case.id,
                    FinalSettlementItem.id == item_id)
            .with_for_update().populate_existing().first())
    if item is None:
        raise not_found("Settlement item")
    return item


def _decided_amount(case: FinalSettlementCase, item: FinalSettlementItem, data: ItemDecision) -> Optional[Decimal]:
    rule = MONETARY.get(item.item_type)
    if rule is None:
        if data.amount is not None or data.units is not None:
            raise bad_request(f"{item.item_type} is calculated in the settlement run; no amount is entered.",
                              "AMOUNT_NOT_ALLOWED")
        return None
    if data.currency is not None and data.currency != case.currency:
        raise bad_request(f"The settlement is paid in {case.currency}.", "CURRENCY_MISMATCH")
    if data.amount is not None and data.units is not None:
        raise bad_request("Give either an amount or units with a rate.", "AMOUNT_OR_UNITS")
    amount: Optional[Decimal]
    if data.units is not None:
        if data.rate is None or data.units <= 0 or data.rate < 0:
            raise bad_request("Units need a non-negative rate; units are positive.", "UNITS_AND_RATE")
        sign = Decimal(-1) if (data.direction or (RECOVERED if rule == RECOVERED else PAID)) == RECOVERED else Decimal(1)
        if data.direction not in (None, PAID, RECOVERED):
            raise bad_request("direction is PAY or RECOVER.", "INVALID_DIRECTION")
        with localcontext(CALC_CONTEXT):
            amount = quantize_amount(data.units * data.rate * sign, case.currency)
    elif data.amount is not None:
        amount = data.amount
        try:
            amount_str(amount, case.currency)
        except Exception:
            raise bad_request(f"The amount has more decimals than {case.currency} allows.", "AMOUNT_PRECISION")
    else:
        if item.amount is None:
            raise bad_request("Enter the amount of this item (or waive it with a reason).", "AMOUNT_REQUIRED")
        amount = Decimal(item.amount)
    if not is_storable(amount):
        raise bad_request("The amount is too large to store.", "AMOUNT_NOT_STORABLE")
    if rule == PAID and amount < 0:
        raise bad_request(f"{item.item_type} is paid to the employee: the amount cannot be negative.",
                          "AMOUNT_NEGATIVE")
    if rule == RECOVERED and amount > 0:
        raise bad_request(f"{item.item_type} is a recovery: the amount is negative.", "AMOUNT_POSITIVE")
    return amount


def confirm_item(db: Session, principal: Principal, case_id: int, item_id: int, data: ItemDecision
                 ) -> FinalSettlementItem:
    # Processors decide the checklist; a payroll approver decides an asset deduction (EC-COR-14).
    case = _load(db, principal, case_id, EDIT if principal.has(EDIT) else APPROVE, lock=True)
    if case.status not in ("OPEN", "READY"):
        raise conflict(f"The checklist of a {case.status.lower()} case is frozen.", "CASE_LOCKED")
    item = _load_item(db, case, item_id)
    if not item.requires_consent and not principal.has(EDIT):
        raise forbidden()
    _check_version(item, data.expected_version)
    if item.status == "NOT_APPLICABLE":
        raise conflict("This item does not apply.", "ITEM_NOT_APPLICABLE")
    amount = _decided_amount(case, item, data)
    note = _text(data.note)
    blocking = [i for i in item.issues or [] if i.get("code") in ("ASSET_VALUE_UNKNOWN",)]
    if blocking:
        raise conflict(blocking[0]["message"], blocking[0]["code"])
    if item.status == "BLOCKED" and data.amount is None and data.units is None:
        raise conflict("This item needs a decision: enter the amount (with a note) or waive it.", "ITEM_BLOCKED")
    if item.status == "BLOCKED" and note is None:
        raise bad_request("Explain the amount of a blocked item (its source).", "NOTE_REQUIRED")
    if item.item_type == "NOTICE_PAY" and amount is not None and amount < 0 and not _text(data.contract_ref):
        raise bad_request("A notice shortfall is recovered only where lawful and in the contract: give the "
                          "contract reference (EC-COR-13).", "CONTRACT_REFERENCE_REQUIRED")
    details = dict(item.details or {})
    if item.requires_consent and amount is not None and amount != 0:
        # EC-COR-14: approval by a payroll approver other than the case's creator, with recorded consent, valued
        # and capped at the asset's recorded value.
        if not principal.has(APPROVE) or not access.target_scope(principal, APPROVE).covers(
                case.legal_entity_id, case.pay_group_id):
            raise forbidden("A payroll approver confirms an asset deduction.")
        if principal.user_id == case.created_by_user_id:
            raise forbidden("Another approver than the case's creator confirms an asset deduction.")
        consent = _text(data.consent_ref, 300)
        if consent is None:
            raise bad_request("Record the employee's consent (document reference) before deducting.",
                              "CONSENT_REQUIRED")
        cap = details.get("valuation_cap")
        if cap is None:
            raise conflict("The asset has no recorded value.", "ASSET_VALUE_UNKNOWN")
        if -amount > Decimal(cap):
            raise bad_request(f"The deduction is capped at the asset's value {cap}.", "ABOVE_ASSET_VALUE")
        item.consent_ref, item.consent_recorded_by_user_id = consent, principal.user_id
        item.consent_recorded_at = clock.utcnow()
    if data.contract_ref:
        details["contract_ref"] = _text(data.contract_ref, 200)
    if data.units is not None:
        item.units, item.rate = data.units, data.rate
    if amount is not None and amount == 0:
        item.status = "NOT_APPLICABLE"
    else:
        item.status = "CONFIRMED"
    item.amount = amount if amount is not None else item.amount
    if item.item_type in CALCULATED:
        item.amount = None
    item.details = details
    item.decided_by_user_id, item.decided_at = principal.user_id, clock.utcnow()
    item.note = note or item.note
    item.row_version += 1
    db.flush()
    _audit(db, principal, ITEM_CONFIRMED, f"Confirmed {item.item_type} of final settlement #{case.id}", case,
           item_id=item.id, item_type=item.item_type, amount=_money(item.amount, case.currency),
           consent=bool(item.consent_ref), status=item.status)
    _refresh_readiness(db, principal, case)
    return item


def waive_item(db: Session, principal: Principal, case_id: int, item_id: int, note: str, *,
               expected_version: Optional[int] = None) -> FinalSettlementItem:
    case = _load(db, principal, case_id, EDIT, lock=True)
    if case.status not in ("OPEN", "READY"):
        raise conflict(f"The checklist of a {case.status.lower()} case is frozen.", "CASE_LOCKED")
    item = _load_item(db, case, item_id)
    _check_version(item, expected_version)
    if item.status not in OPEN_ITEMS + ("CONFIRMED",):
        raise conflict(f"A {item.status.lower()} item cannot be waived.", "INVALID_TRANSITION")
    item.status, item.amount = "WAIVED", None
    item.note = _reason(note)
    item.decided_by_user_id, item.decided_at = principal.user_id, clock.utcnow()
    item.row_version += 1
    db.flush()
    _audit(db, principal, ITEM_WAIVED, f"Waived {item.item_type} of final settlement #{case.id}", case,
           item_id=item.id, item_type=item.item_type, note=item.note)
    _refresh_readiness(db, principal, case)
    return item


# ---------------------------------------------------------------------------
# The settlement run
# ---------------------------------------------------------------------------
def ensure_component(db: Session, company_id: int, code: str, name: str, kind: str, category: str,
                     on: dt.date) -> PayComponent:
    """The tenant's INPUT component for a settlement amount, created (system, audited) when missing."""
    from app.payroll.services import components as comp

    row = db.query(PayComponent).filter(PayComponent.company_id == company_id, PayComponent.code == code).first()
    if row is not None:
        return row
    tags = ["PART_OF_GROSS"] if kind == "EARNING" else []
    comp.create_component(db, None, code=code, kind=kind, category=category, company_id=company_id,
                          data=comp.ComponentInput(name=name, calc_method="INPUT", tags=tags, effective_from=on,
                                                   reason="Final settlement component"))
    return db.query(PayComponent).filter(PayComponent.company_id == company_id, PayComponent.code == code).one()


def _settlement_inputs(db: Session, principal: Principal, case: FinalSettlementCase, period: PayPeriod,
                       run_employee: PayrollRunEmployee) -> List[int]:
    """Confirmed monetary items become system wage inputs of the exit period, submitted for approval and reserved
    for the settlement run once approved."""
    from app.payroll.services import wage_inputs

    created: List[int] = []
    on = min(period.period_start, _today(db, case.company_id))
    for item in _items(db, case, lock=True):
        if item.status != "CONFIRMED" or item.item_type not in MONETARY or not item.amount:
            continue
        amount = Decimal(item.amount)
        direction = PAID if amount > 0 else RECOVERED
        code, name, kind, category = FS_COMPONENTS[(item.item_type, direction)]
        ensure_component(db, case.company_id, code, name, kind, category, on)
        data = wage_inputs.InputData(
            employee_id=case.employee_id, component_code=code, period_start=period.period_start,
            period_end=period.period_end, amount=abs(amount), currency=case.currency, source="SYSTEM",
            reason=f"Final settlement #{case.id}: {item.item_type.replace('_', ' ').lower()}"
                   + (f" ({item.source_key})" if item.source_key else ""),
            external_ref=f"FS-{case.id}-{item.id}")
        rows = wage_inputs.create_input(db, principal, data, submit=True)
        row = db.query(WageInput).filter(WageInput.company_id == case.company_id, WageInput.id == rows[0].id).one()
        if row.status == sm.WageInputStatus.APPROVED:
            wage_inputs.reserve_inputs(db, case.company_id, run_employee.id, [row.id])
        item.wage_input_id = row.id
        item.row_version += 1
        created.append(row.id)
    db.flush()
    return created


def create_settlement_run(db: Session, principal: Principal, case_id: int, *, label: Optional[str] = None,
                          expected_version: Optional[int] = None) -> PayrollRun:
    case = _load(db, principal, case_id, EDIT, lock=True)
    _check_version(case, expected_version)
    if case.status != "READY":
        raise conflict("Every checklist item must be confirmed, waived or not applicable first.", "CASE_NOT_READY")
    employee = db.query(Employee).filter(Employee.company_id == case.company_id, Employee.id == case.employee_id).one()
    if employee.termination_date != case.last_working_day:
        raise conflict("Record the end of employment on the last working day before settling.",
                       "EMPLOYMENT_NOT_ENDED")
    if any(i.status == "CONFIRMED" and i.item_type in MONETARY and i.amount for i in _items(db, case)) \
            and not principal.has(INPUTS_EDIT):
        raise forbidden("Settlement amounts are paid through wage inputs: payroll:inputs:edit is needed.")
    period = calendars.period_containing(db, case.company_id, case.pay_group_id, case.last_working_day)
    if period is None:
        raise conflict("No pay period of the pay group contains the last working day.", "NO_PAY_PERIOD")
    from app.payroll.services import nominees

    nominees.ensure_ready_for_run(db, case)  # DEATH: nominees with shares adding up to 100 percent (EC-BNK-15)
    run = runs.create_run(db, principal, runs.RunRequest(
        run_type=sm.RunType.FINAL_SETTLEMENT.value, pay_group_id=case.pay_group_id, pay_period_id=period.id,
        label=label or f"Final settlement · {employee.first_name} {employee.last_name}"[:150],
        employee_ids=(case.employee_id,)))
    run_employee = (db.query(PayrollRunEmployee)
                    .filter(PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id,
                            PayrollRunEmployee.employee_id == case.employee_id,
                            PayrollRunEmployee.status != "EXCLUDED")
                    .order_by(PayrollRunEmployee.id).first())
    if run_employee is None:
        raise conflict("The employee cannot be paid in a run of this pay group and period.", "EMPLOYEE_EXCLUDED")
    salary = next((i for i in _items(db, case) if i.item_type == "SALARY_TO_LAST_DAY"), None)
    in_regular = bool(salary and (salary.details or {}).get("regular_run"))
    run.selection = {**(run.selection or {}), "final_settlement_case_id": case.id,
                     "salary": "PAID_IN_REGULAR_RUN" if in_regular else "IN_SETTLEMENT",
                     "payee": case.payee}
    db.flush()
    inputs = _settlement_inputs(db, principal, case, period, run_employee)
    case.run_id = run.id
    _move(db, principal, case, "CREATE_RUN", run_id=run.id, run_number=run.number, wage_inputs=inputs)
    if case.held:
        runs.hold_employee(db, principal, run.id, run_employee.id, hold_type="PAYMENT", reason_code="ABSCONDING"
                           if case.termination_type == "ABSCONDING" else "OTHER", note=case.hold_reason)
    _audit(db, principal, RUN_CREATED, f"Created settlement run {run.number} for final settlement #{case.id}", case,
           run_id=run.id, run_number=run.number, wage_inputs=inputs, held=case.held)
    return run


def on_run_cancelled(db: Session, run: PayrollRun) -> int:
    """A cancelled settlement run returns its case to READY (the wage inputs were released by the cancel)."""
    cases = (db.query(FinalSettlementCase)
             .filter(FinalSettlementCase.company_id == run.company_id, FinalSettlementCase.run_id == run.id,
                     FinalSettlementCase.status == "IN_RUN")
             .with_for_update().all())
    for case in cases:
        case.run_id = None
        _move(db, None, case, "RUN_CANCELLED", run_id=run.id)
    return len(cases)


def on_run_finalized(db: Session, run: PayrollRun) -> int:
    """A finalized settlement run settles its case and asks for the F&F statement (documents milestone)."""
    if run.run_type != sm.RunType.FINAL_SETTLEMENT.value or not sm.is_run_finalized(run.status):
        return 0
    cases = (db.query(FinalSettlementCase)
             .filter(FinalSettlementCase.company_id == run.company_id, FinalSettlementCase.run_id == run.id,
                     FinalSettlementCase.status == "IN_RUN")
             .with_for_update().all())
    for case in cases:
        _move(db, None, case, "RUN_FINALIZED", run_id=run.id)
        event = outbox.emit(db, FINALIZED_TOPIC, {"case_id": case.id, "run_id": run.id,
                                                  "employee_id": case.employee_id})
        _audit(db, None, CASE_SETTLED, f"Final settlement #{case.id} settled by run {run.number}", case,
               run_id=run.id, outbox_event_id=event.id)
    return len(cases)


def sync_with_runs(db: Session, company_id: int) -> int:
    """Catch up cases whose settlement run was finalized or cancelled meanwhile (idempotent)."""
    rows = (db.query(PayrollRun)
            .join(FinalSettlementCase, and_(FinalSettlementCase.company_id == PayrollRun.company_id,
                                            FinalSettlementCase.run_id == PayrollRun.id))
            .filter(PayrollRun.company_id == company_id, FinalSettlementCase.status == "IN_RUN")
            .all())
    changed = 0
    for run in rows:
        if sm.is_run_finalized(run.status):
            changed += on_run_finalized(db, run)
        elif run.status == sm.RunStatus.CANCELLED.value:
            changed += on_run_cancelled(db, run)
    return changed


# ---------------------------------------------------------------------------
# Company deadline (configuration)
# ---------------------------------------------------------------------------
def save_deadline(db: Session, principal: Principal, *, legal_entity_id: Optional[int], days: int, day_basis: str,
                  note: Optional[str], expected_version: Optional[int] = None) -> FinalSettlementDeadline:
    if not principal.has("payroll:configuration:write"):
        raise forbidden()
    if legal_entity_id is not None:
        if not access.target_scope(principal, "payroll:configuration:write").covers(legal_entity_id):
            raise not_found("Legal entity")
        if db.query(LegalEntity.id).filter(LegalEntity.company_id == principal.company_id,
                                           LegalEntity.id == legal_entity_id).first() is None:
            raise not_found("Legal entity")
    elif not principal.has_company_scope("payroll:configuration:write"):
        raise forbidden("The company-wide deadline needs company scope.")
    if not 0 <= days <= 365:
        raise bad_request("days is between 0 and 365.", "INVALID_DAYS")
    if day_basis not in ("CALENDAR_DAYS", "WORKING_DAYS"):
        raise bad_request("day_basis is CALENDAR_DAYS or WORKING_DAYS.", "INVALID_DAY_BASIS")
    query = db.query(FinalSettlementDeadline).filter(FinalSettlementDeadline.company_id == principal.company_id)
    query = query.filter(FinalSettlementDeadline.legal_entity_id == legal_entity_id) if legal_entity_id is not None \
        else query.filter(FinalSettlementDeadline.legal_entity_id.is_(None))
    row = query.with_for_update().first()
    before = None
    if row is None:
        row = FinalSettlementDeadline(company_id=principal.company_id, legal_entity_id=legal_entity_id, days=days,
                                      day_basis=day_basis, note=_text(note, 500), updated_by_user_id=principal.user_id,
                                      row_version=1)
        db.add(row)
    else:
        _check_version(row, expected_version)
        before = {"days": row.days, "day_basis": row.day_basis}
        row.days, row.day_basis, row.note = days, day_basis, _text(note, 500)
        row.updated_by_user_id = principal.user_id
        row.row_version += 1
    db.flush()
    AuditLogService.log(db, principal, payroll_audit.PAYROLL_SETTINGS, DEADLINE_SAVED,
                        f"Final-settlement deadline: {days} {day_basis.lower().replace('_', ' ')}",
                        "final_settlement_deadline", row.id, company_id=principal.company_id,
                        details={"legal_entity_id": legal_entity_id, "before": before,
                                 "after": {"days": days, "day_basis": day_basis}})
    return row


def list_deadlines(db: Session, principal: Principal) -> List[dict]:
    if not principal.has(READ) and not principal.has("payroll:configuration:write"):
        raise forbidden()
    rows = db.query(FinalSettlementDeadline).filter(
        FinalSettlementDeadline.company_id == principal.company_id).order_by(FinalSettlementDeadline.id).all()
    return [deadline_json(r) for r in rows]


def deadline_json(row: FinalSettlementDeadline) -> dict:
    return {"id": row.id, "legal_entity_id": row.legal_entity_id, "days": row.days, "day_basis": row.day_basis,
            "note": row.note, "row_version": row.row_version}


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
def item_json(item: FinalSettlementItem, currency: str) -> dict:
    return {
        "id": item.id, "item_type": item.item_type, "source_key": item.source_key or None, "status": item.status,
        "component_code": item.component_code, "amount": _money(item.amount, currency),
        "units": decimal_str(Decimal(item.units)) if item.units is not None else None,
        "rate": decimal_str(Decimal(item.rate)) if item.rate is not None else None,
        "details": item.details or {}, "issues": item.issues or [], "requires_consent": item.requires_consent,
        "consent_recorded": item.consent_ref is not None, "wage_input_id": item.wage_input_id,
        "decided_by_user_id": item.decided_by_user_id,
        "decided_at": item.decided_at.isoformat() if item.decided_at else None, "note": item.note,
        "row_version": item.row_version,
    }


def case_json(db: Session, case: FinalSettlementCase, *, today: dt.date, with_items: bool = True) -> dict:
    employee = db.query(Employee.employee_code, Employee.first_name, Employee.last_name).filter(
        Employee.company_id == case.company_id, Employee.id == case.employee_id).first()
    run = None
    if case.run_id is not None:
        found = db.query(PayrollRun.number, PayrollRun.status).filter(
            PayrollRun.company_id == case.company_id, PayrollRun.id == case.run_id).first()
        run = {"id": case.run_id, "number": found.number, "status": found.status} if found else None
    data = {
        "id": case.id, "employee": {"id": case.employee_id, "code": employee.employee_code if employee else None,
                                    "name": f"{employee.first_name} {employee.last_name}" if employee else None},
        "legal_entity_id": case.legal_entity_id, "pay_group_id": case.pay_group_id,
        "pay_period_id": case.pay_period_id, "employment_start": case.employment_start.isoformat(),
        "last_working_day": case.last_working_day.isoformat(),
        "notice_given_on": case.notice_given_on.isoformat() if case.notice_given_on else None,
        "termination_type": case.termination_type, "currency": case.currency, "status": case.status,
        "held": case.held, "hold_reason": case.hold_reason, "payee": case.payee,
        "due_date": case.due_date.isoformat() if case.due_date else None, "due_basis": case.due_basis,
        "due_note": case.due_note, "overdue": is_overdue(case, today), "run": run, "note": case.note,
        "settled_at": case.settled_at.isoformat() if case.settled_at else None,
        "cancel_reason": case.cancel_reason, "row_version": case.row_version,
    }
    if with_items:
        items = _items(db, case)
        data["items"] = [item_json(i, case.currency) for i in items]
        totals = {"paid": ZERO, "recovered": ZERO}
        for i in items:
            if i.status == "CONFIRMED" and i.amount is not None:
                key = "paid" if Decimal(i.amount) > 0 else "recovered"
                totals[key] += Decimal(i.amount)
        data["totals"] = {k: amount_str(v, case.currency) for k, v in totals.items()}
        data["open_items"] = sum(1 for i in items if i.status in OPEN_ITEMS)
        if case.termination_type == "DEATH":
            from app.payroll.services import nominees

            data["nominees"] = nominees.case_summary(db, case)  # shares and readiness; the records are /payroll/nominees
    return data


def get_case(db: Session, principal: Principal, case_id: int) -> dict:
    case = _load(db, principal, case_id, READ)
    return case_json(db, case, today=_today(db, principal.company_id))


def list_cases(db: Session, principal: Principal, *, status: Optional[str] = None, overdue: Optional[bool] = None,
               employee_id: Optional[int] = None, skip: int = 0, limit: int = 50) -> dict:
    if not principal.has(READ):
        raise forbidden()
    today = _today(db, principal.company_id)
    query = db.query(FinalSettlementCase).filter(
        FinalSettlementCase.company_id == principal.company_id,
        access.run_scope_clause(principal, READ, FinalSettlementCase.legal_entity_id,
                                FinalSettlementCase.pay_group_id))
    if status is not None:
        query = query.filter(FinalSettlementCase.status == status)
    if employee_id is not None:
        query = query.filter(FinalSettlementCase.employee_id == employee_id)
    rows = [r for r in query.order_by(FinalSettlementCase.due_date.asc().nullslast(), FinalSettlementCase.id.desc())
            if _employee_visible(db, principal, READ, r)]
    overdue_count = sum(1 for r in rows if is_overdue(r, today))
    if overdue is not None:
        rows = [r for r in rows if is_overdue(r, today) is overdue]
    return {"total": len(rows), "overdue": overdue_count,
            "items": [case_json(db, r, today=today, with_items=False) for r in rows[skip:skip + limit]]}

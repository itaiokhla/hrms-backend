"""Loans and salary advances with their transaction ledger (3.4 §15, 3.1 §5.11; EC-VAR-05..08, EC-VAR-14).

* Application: amount (in the pay group currency), tenure (months for a loan, runs for an advance)
  and purpose, checked against the employee's LOAN_POLICY or ADVANCE_POLICY by
  ``app.payroll.time_pay_loans``: the maximum (max_amount_formula with MONTHLY_GROSS, or the share of
  monthly net for advances), the tenure, the service length, the number of open loans and advances
  this year. A blocking issue refuses the application; warnings stay on the loan for the approver.
  The schedule (FLAT or REDUCING for loans, equal runs for advances) is computed and stored, and the
  loan goes to the approvals engine (module loan). Payroll staff apply on behalf of employees in
  scope (payroll:loans:manage); employees apply for their own through self-service (SELF).
* Status follows the LOAN machine. Disbursement is a system action recorded from payment evidence
  (``record_disbursement``, called by the payments milestone): it posts the DISBURSEMENT transaction
  and activates the loan, and is refused while payroll.deductions.min_net_pay_percent is unset (G-23,
  decision I-13).
* The balance is derived from the ledger: the schedule's total payable minus instalments,
  prepayments and waivers, plus adjustments. The ledger is append-only and holds one INSTALMENT per
  (loan, run employee), so a regenerated run never recovers twice (EC-VAR-08). A restructure
  re-spreads the outstanding balance over a new tenure without new interest; the new schedule's
  opening balance carries the earlier ledger forward.
* Pause, resume, restructure, prepayment and waiver are requested as loan actions and applied when
  approved through the approvals engine (EC-VAR-06); a paused loan is skipped by the run. A loan whose
  balance reaches zero closes.
* ``loan_instalments_due`` is the snapshot builder's read API (the engine applies the protected net,
  EC-VAR-05, and final settlement rules, EC-VAR-07/14); ``post_instalment`` is the finalization
  write.

Monthly gross and net pay come from hooks (``set_pay_figures_provider``): compensation and finalized
runs are built by other milestones. Without a figure the dependent check is a blocking issue, never
a guess.
"""

import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Callable, List, Optional, Tuple

from sqlalchemy import extract, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.variable_inputs import OPEN_LOAN_STATUSES, Loan, LoanAction, LoanTransaction
from app.payroll import audit as payroll_audit
from app.payroll import formula as fm
from app.payroll import state_machines as sm
from app.payroll import time_pay_loans as tpl
from app.payroll.money import Money, amount_str, decimal_str, is_storable
from app.payroll.money_errors import MoneyError
from app.payroll.services import approvals, policies
from app.payroll.services.wage_inputs import (
    Placement,
    audit,
    company_today,
    covers,
    money_json,
    placement,
    require_transition,
    resolve_setting,
    scope_clause,
    scoped_placement,
    transition,
)
from app.payroll.time_pay_common import BLOCKING, WARNING, Issue, TimePayError

READ = "payroll:loans:read"
MANAGE = "payroll:loans:manage"
APPROVE = "payroll:loans:approve"
MODULE = "loan"
SUBJECT = "loan"
ACTION_SUBJECT = "loan_action"
AUDIT_MODULE = payroll_audit.LOANS
POLICY_TYPES = {tpl.LOAN: "LOAN_POLICY", tpl.ADVANCE: "ADVANCE_POLICY"}
SWITCHES = {tpl.LOAN: "payroll.loans.enabled", tpl.ADVANCE: "payroll.advances.enabled"}
PROTECTED_NET = "payroll.deductions.min_net_pay_percent"
WITHDRAWN = "Withdrawn by the requester"
RUNNING = (sm.LoanStatus.ACTIVE, sm.LoanStatus.PAUSED)


# ---------------------------------------------------------------------------
# Pay figures (hooks for compensation and runs)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PayFigures:
    """Monthly gross (from compensation) and monthly net (last finalized run), in the pay currency."""

    monthly_gross: Optional[Decimal] = None
    monthly_net: Optional[Decimal] = None


PayFiguresProvider = Callable[[Session, int, int, date, str], PayFigures]


def _no_figures(db: Session, company_id: int, employee_id: int, on: date, currency: str) -> PayFigures:
    """Default until compensation and finalized runs are wired in: nothing is known."""
    return PayFigures()


_figures_provider: PayFiguresProvider = _no_figures


def set_pay_figures_provider(provider: Optional[PayFiguresProvider]) -> PayFiguresProvider:
    """Install the monthly gross/net lookup (None restores the default); returns the previous provider."""
    global _figures_provider
    previous = _figures_provider
    _figures_provider = provider or _no_figures
    return previous


def max_loan_amount(fields: dict, currency: str, figures: PayFigures) -> Optional[Decimal]:
    """LOAN_POLICY.max_amount_formula evaluated with MONTHLY_GROSS; None when it cannot be evaluated."""
    formula = fields.get("max_amount_formula")
    if not formula:
        return None
    components = {}
    if figures.monthly_gross is not None:
        components["MONTHLY_GROSS"] = Money(figures.monthly_gross, currency)
    try:
        result = fm.evaluate(fm.parse(formula), fm.EvalContext.build(currency, components=components))
    except fm.FormulaError:
        return None
    if not result.ok or result.value is None:
        return None
    value = getattr(result.value, "amount", result.value)
    return Decimal(value) if isinstance(value, (Decimal, int)) else None


# ---------------------------------------------------------------------------
# Schedules and the ledger
# ---------------------------------------------------------------------------
def _schedule_from_json(data: dict) -> tpl.RepaymentSchedule:
    instalments = tuple(
        tpl.Instalment(int(i["number"]), Decimal(i["principal"]), Decimal(i["interest"]), Decimal(i["total"]),
                       Decimal(i["balance_after"]))
        for i in data["instalments"]
    )
    return tpl.RepaymentSchedule(data["kind"], data["method"], Decimal(data["principal"]),
                                 Decimal(data["annual_rate_percent"]), data["currency"], instalments,
                                 data.get("explanation", ""))


def _transactions(db: Session, loan: Loan, *, version: Optional[int] = None) -> List[LoanTransaction]:
    query = db.query(LoanTransaction).filter(LoanTransaction.company_id == loan.company_id,
                                             LoanTransaction.loan_id == loan.id)
    if version is not None:
        query = query.filter(LoanTransaction.schedule_version == version)
    return query.order_by(LoanTransaction.id).all()


def _ledger_rows(db: Session, loan: Loan) -> List[tpl.LoanTransaction]:
    """The current schedule's ledger; after a restructure it opens with the carried balance."""
    rows: List[tpl.LoanTransaction] = []
    if loan.schedule_version > 1:
        rows.append(tpl.LoanTransaction(tpl.DISBURSEMENT, Decimal(loan.opening_balance)))
    for tx in _transactions(db, loan, version=loan.schedule_version):
        rows.append(tpl.LoanTransaction(
            tx.kind, Decimal(tx.amount), str(tx.run_employee_id) if tx.run_employee_id is not None else None,
            Decimal(tx.scheduled_amount) if tx.scheduled_amount is not None else None,
        ))
    return rows


def account(db: Session, loan: Loan) -> tpl.LoanAccount:
    status = loan.status if loan.status in tpl.LOAN_STATUSES else tpl.ACTIVE
    return tpl.LoanAccount(str(loan.id), _schedule_from_json(loan.schedule), tuple(_ledger_rows(db, loan)), status)


def balance(db: Session, loan: Loan) -> Decimal:
    """Outstanding amount derived from the ledger (zero before disbursement)."""
    if loan.disbursed_on is None:
        return Decimal(0)
    try:
        return tpl.loan_balance(account(db, loan))
    except TimePayError as exc:
        raise conflict(f"The ledger of loan #{loan.id} is inconsistent: {exc}", "LOAN_LEDGER_INCONSISTENT")


def _issue_json(issue: Issue) -> dict:
    return issue.as_dict()


def _tx_json(tx: LoanTransaction) -> dict:
    return {"id": tx.id, "kind": tx.kind, "amount": money_json(tx.amount, tx.currency),
            "schedule_version": tx.schedule_version, "run_employee_id": tx.run_employee_id,
            "scheduled": money_json(tx.scheduled_amount, tx.currency) if tx.scheduled_amount is not None else None,
            "effective_on": tx.effective_on.isoformat(), "reference": tx.reference, "note": tx.note,
            "action_id": tx.action_id, "created_at": tx.created_at}


def _action_json(action: LoanAction) -> dict:
    return {"id": action.id, "action": action.action, "params": action.params, "reason": action.reason,
            "status": action.status, "approval_request_id": action.approval_request_id,
            "requested_by_user_id": action.requested_by_user_id, "decided_at": action.decided_at,
            "status_reason": action.status_reason, "created_at": action.created_at}


def serialize(db: Session, loan: Loan, *, detail: bool = True) -> dict:
    body = {
        "id": loan.id, "employee_id": loan.employee_id, "legal_entity_id": loan.legal_entity_id,
        "pay_group_id": loan.pay_group_id, "kind": loan.kind, "principal": money_json(loan.principal, loan.currency),
        "currency": loan.currency, "purpose": loan.purpose, "tenure": loan.tenure, "interest": loan.interest,
        "status": loan.status, "balance": money_json(balance(db, loan), loan.currency),
        "schedule_version": loan.schedule_version, "recover_balance_on_exit": loan.recover_balance_on_exit,
        "application_issues": list(loan.application_issues or []), "approval_request_id": loan.approval_request_id,
        "applied_by_user_id": loan.applied_by_user_id, "decided_at": loan.decided_at,
        "disbursed_on": loan.disbursed_on.isoformat() if loan.disbursed_on else None,
        "closed_at": loan.closed_at, "status_reason": loan.status_reason, "row_version": loan.row_version,
        "created_at": loan.created_at,
    }
    if detail:
        body["schedule"] = loan.schedule
        body["transactions"] = [_tx_json(t) for t in _transactions(db, loan)]
        body["actions"] = [_action_json(a) for a in db.query(LoanAction).filter(
            LoanAction.company_id == loan.company_id, LoanAction.loan_id == loan.id).order_by(LoanAction.id)]
    return body


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LoanData:
    employee_id: int
    kind: str
    amount: Decimal
    currency: str
    tenure: Optional[int] = None
    purpose: Optional[str] = None


def _service_months(hire_date: date, on: date) -> int:
    months = (on.year - hire_date.year) * 12 + (on.month - hire_date.month)
    if on.day < hire_date.day:
        months -= 1
    return max(0, months)


def _policy(db: Session, company_id: int, kind: str, employee_id: int, on: date) -> dict:
    found = policies.resolve_for_employee(db, company_id, POLICY_TYPES[kind], employee_id, on)
    if found.get("status") != "RESOLVED":
        raise conflict(found.get("reason") or f"No {kind.lower()} policy applies to this employee.", "NO_POLICY")
    return found["version"]


def _protected_net(db: Session, company_id: int, legal_entity_id: int, on: date) -> Optional[Decimal]:
    value = resolve_setting(db, company_id, PROTECTED_NET, on=on, legal_entity_id=legal_entity_id)
    return Decimal(str(value)) if value is not None else None


def _application_issues(db: Session, data: LoanData, place: Placement, fields: dict, today: date
                        ) -> Tuple[List[Issue], tpl.RepaymentSchedule, int]:
    company_id = place.employee.company_id
    figures = _figures_provider(db, company_id, data.employee_id, today, place.currency)
    open_loans = db.query(func.count(Loan.id)).filter(
        Loan.company_id == company_id, Loan.employee_id == data.employee_id, Loan.kind == data.kind,
        Loan.status.in_(OPEN_LOAN_STATUSES)).scalar()
    if data.kind == tpl.LOAN:
        rules = tpl.parse_loan_policy(fields)
        tenure = data.tenure or rules.max_tenure_months
        issues = list(tpl.check_loan_application(
            data.amount, tenure, fields, place.currency, max_amount=max_loan_amount(fields, place.currency, figures),
            service_months=_service_months(place.employee.hire_date, today), active_loans=open_loans,
            net_pay=figures.monthly_net,
        ))
        schedule = None if any(i.severity == BLOCKING for i in issues) else \
            tpl.loan_schedule_from_policy(data.amount, tenure, fields, place.currency)
    else:
        rules = tpl.parse_advance_policy(fields)
        tenure = data.tenure or rules.recover_in_runs
        this_year = db.query(func.count(Loan.id)).filter(
            Loan.company_id == company_id, Loan.employee_id == data.employee_id, Loan.kind == tpl.ADVANCE,
            Loan.status.notin_((sm.LoanStatus.REJECTED, sm.LoanStatus.CANCELLED)),
            extract("year", Loan.created_at) == today.year).scalar()
        if figures.monthly_net is None:
            issues = [Issue("ADVANCE_NET_UNKNOWN", BLOCKING,
                            "Monthly net pay is not known yet (no finalized payroll); the advance cannot be checked")]
        else:
            issues = list(tpl.check_advance_request(data.amount, monthly_net=figures.monthly_net,
                                                    advances_this_year=this_year, policy=fields))
        schedule = None if any(i.severity == BLOCKING for i in issues) else \
            tpl.advance_schedule(data.amount, place.currency, fields, runs=tenure)
    return issues, schedule, tenure


def apply_loan(db: Session, principal: Principal, data: LoanData, *, self_service: bool = False) -> Loan:
    """Check the application against the policy, store the schedule and route it for approval."""
    company_id = principal.company_id
    if data.kind not in POLICY_TYPES:
        raise bad_request("kind must be LOAN or ADVANCE.", "INVALID_KIND")
    today = company_today(db, company_id)
    if self_service:
        if principal.employee_id is None or principal.employee_id != data.employee_id:
            raise not_found("Employee")
        place = placement(db, company_id, principal.employee_id, today)
        if not any(covers(principal, code, place.legal_entity_id, place.pay_group_id, principal.employee_id)
                   for code in (MANAGE, READ)):
            raise forbidden()
    else:
        place = scoped_placement(db, principal, MANAGE, data.employee_id, today, allow_self=True)
    if not resolve_setting(db, company_id, SWITCHES[data.kind], on=today, legal_entity_id=place.legal_entity_id):
        raise conflict(f"{'Loans' if data.kind == tpl.LOAN else 'Salary advances'} are switched off.",
                       "LOANS_DISABLED")
    if data.currency != place.currency:
        raise bad_request(f"Loans of this employee are in the pay group currency {place.currency}.",
                          "CURRENCY_MISMATCH")
    try:
        amount_str(data.amount, place.currency)
        if data.amount <= 0 or not is_storable(data.amount):
            raise MoneyError("AMOUNT_NOT_POSITIVE", "The amount must be greater than zero.")
    except MoneyError as exc:
        raise bad_request(exc.message, exc.code)
    version = _policy(db, company_id, data.kind, data.employee_id, today)
    fields = dict(version["fields"] or {})
    try:
        issues, schedule, tenure = _application_issues(db, data, place, fields, today)
    except TimePayError as exc:
        raise bad_request(exc.message, exc.code)
    blocking = [i for i in issues if i.severity == BLOCKING]
    if blocking:
        raise conflict("The application does not meet the policy: " + "; ".join(i.message for i in blocking) + ".",
                       blocking[0].code)
    if _protected_net(db, company_id, place.legal_entity_id, today) is None:
        issues.append(Issue("PROTECTED_NET_NOT_SET", WARNING,
                            "The protected net pay percentage is not set: the loan cannot be activated until it is"))
    rules = tpl.parse_loan_policy(fields) if data.kind == tpl.LOAN else None
    loan = Loan(
        company_id=company_id, employee_id=data.employee_id, legal_entity_id=place.legal_entity_id,
        pay_group_id=place.pay_group_id, kind=data.kind, principal=data.amount, currency=place.currency,
        purpose=data.purpose, tenure=tenure,
        interest={"rate_percent": decimal_str(schedule.annual_rate_percent), "method": schedule.method},
        schedule=schedule.to_dict(), schedule_version=1,
        recover_balance_on_exit=rules.recover_balance_on_exit if rules is not None else True,
        status=sm.LoanStatus.PENDING_APPROVAL, policy_version_id=version["id"],
        application_issues=[_issue_json(i) for i in issues], applied_by_user_id=principal.user_id, row_version=1,
    )
    db.add(loan)
    db.flush()
    request = approvals.submit(
        db, principal, module=MODULE, subject_type=SUBJECT, subject_id=loan.id, subject_employee_id=loan.employee_id,
        amount=loan.principal, currency=loan.currency,
        context={"legal_entity_id": loan.legal_entity_id, "pay_group_id": loan.pay_group_id, "kind": loan.kind},
    )
    loan.approval_request_id = request.id
    db.flush()
    audit(db, principal, AUDIT_MODULE, "LOAN_APPLIED",
          f"{loan.kind.capitalize()} #{loan.id} applied for employee #{loan.employee_id}", SUBJECT, loan.id, company_id,
          kind=loan.kind, amount=money_json(loan.principal, loan.currency), tenure=tenure,
          method=schedule.method, issues=[i.code for i in issues], self_service=self_service)
    return loan


def _load(db: Session, principal: Principal, loan_id: int, permission: str, *, lock: bool = False,
          own: bool = False) -> Loan:
    query = db.query(Loan).filter(Loan.company_id == principal.company_id, Loan.id == loan_id)
    if own:
        if principal.employee_id is None:
            raise not_found("Loan")
        query = query.filter(Loan.employee_id == principal.employee_id)
    else:
        if not principal.has(permission):
            raise forbidden()
        query = query.filter(scope_clause(principal, permission, Loan))
    if lock:
        query = query.with_for_update().populate_existing()
    loan = query.first()
    if loan is None:
        raise not_found("Loan")
    if own and not any(covers(principal, code, loan.legal_entity_id, loan.pay_group_id, principal.employee_id)
                       for code in (READ, MANAGE)):
        raise not_found("Loan")
    return loan


def get_loan(db: Session, principal: Principal, loan_id: int, *, own: bool = False) -> dict:
    return serialize(db, _load(db, principal, loan_id, READ, own=own))


def list_loans(db: Session, principal: Principal, *, own: bool = False, status: Optional[str] = None,
               kind: Optional[str] = None, employee_id: Optional[int] = None, skip: int = 0, limit: int = 50
               ) -> Tuple[int, List[dict]]:
    query = db.query(Loan).filter(Loan.company_id == principal.company_id)
    if own:
        if principal.employee_id is None or not (principal.has(READ) or principal.has(MANAGE)):
            raise forbidden()
        query = query.filter(Loan.employee_id == principal.employee_id)
    else:
        if not principal.has(READ):
            raise forbidden()
        query = query.filter(scope_clause(principal, READ, Loan))
    if status:
        query = query.filter(Loan.status == status)
    if kind:
        query = query.filter(Loan.kind == kind)
    if employee_id is not None:
        query = query.filter(Loan.employee_id == employee_id)
    total = query.count()
    rows = query.order_by(Loan.id.desc()).offset(skip).limit(limit).all()
    return total, [serialize(db, r, detail=False) for r in rows]


@approvals.on_decided(MODULE, SUBJECT)
def _on_loan_decided(db: Session, principal: Optional[Principal], request) -> None:
    loan = (
        db.query(Loan)
        .filter(Loan.company_id == request.company_id, Loan.id == request.subject_id)
        .with_for_update()
        .populate_existing()
        .one()
    )
    if loan.status != sm.LoanStatus.PENDING_APPROVAL:
        return
    remarks = next((s.remarks for s in reversed(request.steps) if s.remarks), None)
    if request.status == approvals.APPROVED:
        loan.status = transition(sm.LOAN, loan.status, "APPROVE").target
        action = "LOAN_APPROVED"
    else:
        loan.status = transition(sm.LOAN, loan.status, "REJECT").target
        loan.status_reason = WITHDRAWN if request.status == approvals.CANCELLED else remarks
        action = "LOAN_WITHDRAWN" if request.status == approvals.CANCELLED else "LOAN_REJECTED"
    loan.decided_at = clock.utcnow()
    loan.row_version += 1
    db.flush()
    audit(db, principal, AUDIT_MODULE, action, f"Loan #{loan.id} {loan.status.lower()}", SUBJECT, loan.id,
          loan.company_id, approval_request_id=request.id, remarks=remarks)


def cancel_loan(db: Session, principal: Principal, loan_id: int, reason: str) -> Loan:
    """An approved loan that was not disbursed is cancelled."""
    loan = _load(db, principal, loan_id, MANAGE, lock=True)
    loan.status = require_transition(principal, sm.LOAN, loan.status, "CANCEL")
    loan.status_reason = reason
    loan.row_version += 1
    db.flush()
    audit(db, principal, AUDIT_MODULE, "LOAN_CANCELLED", f"Cancelled loan #{loan.id}: {reason}", SUBJECT, loan.id,
          loan.company_id, reason=reason)
    return loan


def record_disbursement(db: Session, principal: Optional[Principal], company_id: int, loan_id: int, *,
                        paid_on: date, reference: str) -> Loan:
    """System action from payment evidence: DISBURSEMENT posted, APPROVED -> ACTIVE.

    Refused while the protected net pay percentage is unset (G-23) or the loan/advance switch is off.
    Repeating it for the same reference is a no-op.
    """
    if not reference:
        raise ValueError("a disbursement needs its payment reference")
    loan = (
        db.query(Loan).filter(Loan.company_id == company_id, Loan.id == loan_id)
        .with_for_update().populate_existing().first()
    )
    if loan is None:
        raise not_found("Loan")
    if loan.status != sm.LoanStatus.APPROVED and loan.disbursement_ref == reference:
        return loan
    target = transition(sm.LOAN, loan.status, "DISBURSE").target
    if not resolve_setting(db, company_id, SWITCHES[loan.kind], on=paid_on, legal_entity_id=loan.legal_entity_id):
        raise conflict("Loans or advances are switched off; the loan cannot be activated.", "LOANS_DISABLED")
    if _protected_net(db, company_id, loan.legal_entity_id, paid_on) is None:
        raise conflict("Set the protected net pay percentage (payroll.deductions.min_net_pay_percent) before "
                       "activating loans or advances.", "PROTECTED_NET_REQUIRED")
    db.add(LoanTransaction(
        company_id=company_id, loan_id=loan.id, kind=tpl.DISBURSEMENT, amount=loan.principal, currency=loan.currency,
        schedule_version=1, effective_on=paid_on, reference=reference[:100],
        created_by_user_id=principal.user_id if principal is not None else None,
    ))
    loan.status, loan.disbursed_on, loan.disbursement_ref = target, paid_on, reference[:100]
    loan.row_version += 1
    db.flush()
    audit(db, principal, AUDIT_MODULE, "LOAN_DISBURSED", f"Loan #{loan.id} disbursed ({reference})", SUBJECT, loan.id,
          company_id, amount=money_json(loan.principal, loan.currency), paid_on=paid_on.isoformat(),
          reference=reference)
    return loan


# ---------------------------------------------------------------------------
# Actions: pause, resume, restructure, prepay, waive (approved, EC-VAR-06)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ActionData:
    action: str
    reason: Optional[str] = None
    tenure: Optional[int] = None
    amount: Optional[Decimal] = None
    reference: Optional[str] = None
    currency: Optional[str] = None


def _validate_action(db: Session, loan: Loan, data: ActionData) -> dict:
    spec = transition(sm.LOAN, loan.status, data.action)
    if spec.reason_required and not (data.reason or "").strip():
        raise bad_request("Give a reason.", "REASON_REQUIRED")
    params: dict = {}
    if data.action == "RESTRUCTURE":
        if data.tenure is None or data.tenure < 1:
            raise bad_request("A restructure needs the new tenure.", "TENURE_REQUIRED")
        if loan.kind == tpl.ADVANCE and data.tenure > tpl.MAX_ADVANCE_RUNS:
            raise bad_request(f"An advance is recovered in at most {tpl.MAX_ADVANCE_RUNS} runs.", "TENURE_TOO_LONG")
        params["tenure"] = data.tenure
    if data.action in ("PREPAY", "WAIVE"):
        if data.amount is None:
            raise bad_request("Give the amount.", "AMOUNT_REQUIRED")
        if data.currency is not None and data.currency != loan.currency:
            raise bad_request(f"The loan is in {loan.currency}.", "CURRENCY_MISMATCH")
        try:
            amount_str(data.amount, loan.currency)
        except MoneyError as exc:
            raise bad_request(exc.message, exc.code)
        outstanding = balance(db, loan)
        if data.amount <= 0 or data.amount > outstanding:
            raise bad_request(f"The amount must be between 0 and the outstanding balance "
                              f"{amount_str(outstanding, loan.currency)}.", "AMOUNT_ABOVE_BALANCE")
        params["amount"] = amount_str(data.amount, loan.currency)
        if data.reference:
            params["reference"] = data.reference[:100]
    return params


def request_action(db: Session, principal: Principal, loan_id: int, data: ActionData) -> LoanAction:
    """Record the requested action and route it for approval (approvers hold payroll:loans:approve)."""
    if data.action not in ("PAUSE", "RESUME", "RESTRUCTURE", "PREPAY", "WAIVE"):
        raise bad_request("Unknown loan action.", "INVALID_ACTION")
    loan = _load(db, principal, loan_id, MANAGE, lock=True)
    params = _validate_action(db, loan, data)
    pending = db.query(LoanAction.id).filter(LoanAction.company_id == loan.company_id, LoanAction.loan_id == loan.id,
                                             LoanAction.status == "PENDING_APPROVAL").first()
    if pending is not None or loan.status == sm.LoanStatus.PENDING_APPROVAL:
        raise conflict("Another request for this loan is awaiting approval.", "ACTION_PENDING")
    action = LoanAction(company_id=loan.company_id, loan_id=loan.id, action=data.action, params=params,
                        reason=data.reason, status="PENDING_APPROVAL", requested_by_user_id=principal.user_id)
    db.add(action)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        raise conflict("Another request for this loan is awaiting approval.", "ACTION_PENDING")
    with_amount = "amount" in params
    request = approvals.submit(
        db, principal, module=MODULE, subject_type=ACTION_SUBJECT, subject_id=action.id,
        subject_employee_id=loan.employee_id, amount=params["amount"] if with_amount else None,
        currency=loan.currency if with_amount else None,
        context={"legal_entity_id": loan.legal_entity_id, "pay_group_id": loan.pay_group_id, "loan_id": loan.id,
                 "action": data.action},
    )
    action.approval_request_id = request.id
    db.flush()
    audit(db, principal, AUDIT_MODULE, "LOAN_ACTION_REQUESTED",
          f"Requested {data.action.lower()} of loan #{loan.id}", SUBJECT, loan.id, loan.company_id,
          action_id=action.id, loan_action=data.action, params=params, reason=data.reason)
    return action


def _close_if_settled(db: Session, loan: Loan) -> None:
    if loan.status in RUNNING and balance(db, loan) == 0:
        loan.status = transition(sm.LOAN, loan.status, "CLOSE").target
        loan.closed_at = clock.utcnow()


def _apply(db: Session, principal: Optional[Principal], loan: Loan, action: LoanAction) -> dict:
    data = ActionData(action.action, action.reason, action.params.get("tenure"),
                      Decimal(action.params["amount"]) if "amount" in action.params else None,
                      action.params.get("reference"))
    _validate_action(db, loan, data)
    today = company_today(db, loan.company_id)
    details: dict = {}
    if data.action in ("PAUSE", "RESUME"):
        loan.status = transition(sm.LOAN, loan.status, data.action).target
    elif data.action == "RESTRUCTURE":
        outstanding = balance(db, loan)
        if outstanding <= 0:
            raise conflict("Nothing is outstanding; the loan cannot be restructured.", "NOTHING_OUTSTANDING")
        try:
            if loan.kind == tpl.LOAN:
                schedule = tpl.loan_schedule(outstanding, data.tenure, loan.currency)
            else:
                schedule = tpl.advance_schedule(outstanding, loan.currency, runs=data.tenure,
                                                policy={"recover_in_runs": tpl.MAX_ADVANCE_RUNS})
        except TimePayError as exc:
            raise conflict(f"The new schedule is not possible: {exc}", "INVALID_SCHEDULE")
        details = {"from_version": loan.schedule_version, "opening_balance": amount_str(outstanding, loan.currency),
                   "tenure": data.tenure}
        loan.schedule, loan.schedule_version = schedule.to_dict(), loan.schedule_version + 1
        loan.opening_balance, loan.tenure = outstanding, data.tenure
        loan.interest = {**(loan.interest or {}), "restructured": True}
    else:
        kind = tpl.PREPAYMENT if data.action == "PREPAY" else tpl.WAIVER
        db.add(LoanTransaction(
            company_id=loan.company_id, loan_id=loan.id, kind=kind, amount=data.amount, currency=loan.currency,
            schedule_version=loan.schedule_version, effective_on=today, reference=data.reference,
            note=action.reason, action_id=action.id,
            created_by_user_id=principal.user_id if principal is not None else None,
        ))
        db.flush()
        _close_if_settled(db, loan)
        details = {"amount": amount_str(data.amount, loan.currency), "kind": kind}
    loan.row_version += 1
    return details


@approvals.on_decided(MODULE, ACTION_SUBJECT)
def _on_action_decided(db: Session, principal: Optional[Principal], request) -> None:
    action = (
        db.query(LoanAction)
        .filter(LoanAction.company_id == request.company_id, LoanAction.id == request.subject_id)
        .with_for_update()
        .one()
    )
    if action.status != "PENDING_APPROVAL":
        return
    loan = (
        db.query(Loan).filter(Loan.company_id == action.company_id, Loan.id == action.loan_id)
        .with_for_update().populate_existing().one()
    )
    remarks = next((s.remarks for s in reversed(request.steps) if s.remarks), None)
    details: dict = {}
    if request.status == approvals.APPROVED:
        details = _apply(db, principal, loan, action)
        action.status = "APPLIED"
        event = "LOAN_ACTION_APPLIED"
    else:
        action.status = "CANCELLED" if request.status == approvals.CANCELLED else "REJECTED"
        action.status_reason = remarks
        event = "LOAN_ACTION_REJECTED" if action.status == "REJECTED" else "LOAN_ACTION_CANCELLED"
    action.decided_at = clock.utcnow()
    db.flush()
    audit(db, principal, AUDIT_MODULE, event, f"{action.action.capitalize()} of loan #{loan.id}: "
          f"{action.status.lower()}", SUBJECT, loan.id, loan.company_id, action_id=action.id, loan_action=action.action,
          status=loan.status, remarks=remarks, **details)


def decide(db: Session, principal: Principal, loan_id: int, decision: str, remarks: Optional[str] = None,
           expected_step: Optional[int] = None) -> Loan:
    """Approve or reject what is pending on the loan: the application, else the pending action."""
    loan = _load(db, principal, loan_id, APPROVE)
    request = approvals.pending_request(db, MODULE, SUBJECT, loan.id)
    if request is None:
        action = db.query(LoanAction).filter(LoanAction.company_id == loan.company_id, LoanAction.loan_id == loan.id,
                                             LoanAction.status == "PENDING_APPROVAL").first()
        if action is not None:
            request = approvals.pending_request(db, MODULE, ACTION_SUBJECT, action.id)
    if request is None:
        raise conflict("Nothing on this loan is awaiting approval.", "NOT_PENDING")
    approvals.decide(db, principal, request.id, decision, remarks, expected_step)
    db.refresh(loan)
    return loan


# ---------------------------------------------------------------------------
# Payroll (snapshot builder and finalization)
# ---------------------------------------------------------------------------
def loan_instalments_due(db: Session, company_id: int, employee_id: int, period_start: date, period_end: date
                         ) -> List[dict]:
    """ACTIVE and PAUSED loans disbursed by the period end, as plain dicts for the snapshot.

    Each carries the schedule, the current schedule's ledger (``ref`` = run employee id) and the
    next scheduled instalment; the engine rebuilds ``time_pay_loans.LoanAccount`` from it and applies
    the protected net and pause rules (EC-VAR-05, EC-VAR-06).
    """
    loans = (
        db.query(Loan)
        .filter(Loan.company_id == company_id, Loan.employee_id == employee_id, Loan.status.in_(RUNNING),
                Loan.disbursed_on <= period_end)
        .order_by(Loan.disbursed_on, Loan.id)
        .all()
    )
    result = []
    for loan in loans:
        acct = account(db, loan)
        outstanding = tpl.loan_balance(acct)
        instalments = [t for t in acct.transactions if t.kind == tpl.INSTALMENT]
        planned = acct.schedule.instalment(len(instalments) + 1)
        ledger = [{"kind": t.kind, "amount": amount_str(t.amount, loan.currency) if t.kind != tpl.ADJUSTMENT
                   else decimal_str(t.amount), "ref": t.ref,
                   "scheduled": amount_str(t.scheduled, loan.currency) if t.scheduled is not None else None}
                  for t in acct.transactions]
        content = f"{loan.id}|{loan.status}|{loan.schedule_version}|{len(ledger)}|{outstanding}|{loan.row_version}"
        result.append({
            "loan_id": loan.id, "kind": loan.kind, "status": loan.status, "currency": loan.currency,
            "component": tpl.RECOVERY_COMPONENTS[loan.kind], "recover_balance_on_exit": loan.recover_balance_on_exit,
            "schedule": loan.schedule, "schedule_version": loan.schedule_version, "transactions": ledger,
            "balance": amount_str(outstanding, loan.currency),
            "next_instalment": {"number": planned.number, "amount": amount_str(planned.total, loan.currency)}
            if planned else None,
            "version_hash": hashlib.sha256(content.encode()).hexdigest(),
        })
    return result


def post_instalment(db: Session, company_id: int, loan_id: int, run_employee_id: int, amount: Decimal, *,
                    scheduled: Optional[Decimal], effective_on: date) -> LoanTransaction:
    """Finalization: record the run's recovery once per (loan, run employee) (EC-VAR-08).

    Posting the same amount again returns the existing row; a different amount is a 409 (finalized
    runs are immutable; corrections are ADJUSTMENT rows). The loan closes when nothing is left.
    """
    loan = (
        db.query(Loan).filter(Loan.company_id == company_id, Loan.id == loan_id)
        .with_for_update().populate_existing().first()
    )
    if loan is None:
        raise not_found("Loan")
    existing = db.query(LoanTransaction).filter(
        LoanTransaction.company_id == company_id, LoanTransaction.loan_id == loan.id,
        LoanTransaction.kind == tpl.INSTALMENT, LoanTransaction.run_employee_id == run_employee_id).first()
    if existing is not None:
        if Decimal(existing.amount) != Decimal(amount):
            raise conflict(f"Run employee #{run_employee_id} already recovered {amount_str(existing.amount, loan.currency)} "
                           f"for loan #{loan.id}.", "INSTALMENT_EXISTS")
        return existing
    if loan.status not in RUNNING:
        raise conflict(f"Loan #{loan.id} is {loan.status}; nothing can be recovered.", "LOAN_NOT_ACTIVE")
    amount_str(amount, loan.currency)
    if amount < 0 or amount > balance(db, loan):
        raise conflict("The instalment exceeds the outstanding balance.", "AMOUNT_ABOVE_BALANCE")
    tx = LoanTransaction(
        company_id=company_id, loan_id=loan.id, kind=tpl.INSTALMENT, amount=amount, currency=loan.currency,
        schedule_version=loan.schedule_version, run_employee_id=run_employee_id, scheduled_amount=scheduled,
        effective_on=effective_on,
    )
    db.add(tx)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        raise conflict(f"Run employee #{run_employee_id} already recovered loan #{loan.id}.", "INSTALMENT_EXISTS")
    _close_if_settled(db, loan)
    loan.row_version += 1
    db.flush()
    audit(db, None, AUDIT_MODULE, "LOAN_INSTALMENT_POSTED",
          f"Recovered {amount_str(amount, loan.currency)} of loan #{loan.id} (run employee #{run_employee_id})",
          SUBJECT, loan.id, company_id, run_employee_id=run_employee_id, amount=money_json(amount, loan.currency),
          status=loan.status)
    return tx

"""Nominees and legal heirs: the payees of a deceased employee's final settlement (3.5 §15, 3.7 §2.1;
EC-BNK-14, EC-BNK-15, EC-BNK-16, EC-COR-07).

Only a final-settlement case of type DEATH has nominees; the spec defines no standing nominee or beneficiary
rule, so nobody is ever paid as a nominee outside such a case (a nominee record is created under the case,
never under a bare HR record).

* Records (``nominees``): kind NOMINEE or LEGAL_HEIR, name, relationship, a proof document reference (the
  nomination, succession or court document HR holds), the share in percent (more than 0, at most 100, 6
  decimals, ACTIVE shares of a case never above 100 and exactly 100 before the settlement run is created), the
  nominee's country and an optional e-mail for bank alerts. Creating, changing and ending a nominee needs
  ``payroll:bank_details:edit`` in the case's scope and a fresh step-up, and is audited (names masked). The set
  is frozen once the settlement run exists (the case is IN_RUN or SETTLED) and a cancelled case ends its
  nominees. Nothing about a nominee's bank account is stored here.
* Bank accounts: a nominee's account is an ordinary ``bank_accounts`` row (``payee_type`` NOMINEE, composite
  key to the nominee) submitted through ``bank_accounts.submit_account``, so format checks per country, envelope
  encryption, masking, step-up, the velocity limit, maker-checker approval (the creator of the nominee record
  counts as a maker), duplicate-fingerprint holds, the holder-name check against the nominee's name,
  document verification and the cooling-off period are the employee flow's. Differences: always HR-entered, a
  verified account is always required before payout (whatever ``payroll.bank.exclude_unverified_from_payout``
  says) and the cooling-off is never waived.
* Payment (called from ``obligations``): the finalized settlement run of a DEATH case creates one obligation
  of payee type NOMINEE (the deceased employee's own accounts are never used) and one payment instruction
  per nominee share: each nominee receives share % of the net rounded down to the currency's minor unit and the
  nominee with the largest share absorbs the rounding difference, so instructions add up to the net exactly
  (``banking.nominee_shares``). Without a usable account a nominee's share waits NOT_READY; incomplete shares
  create one NOT_READY placeholder. Beneficiary snapshots carry the nominee marker.
* Statement: the event ``payroll.final_settlement.finalized`` issues one settlement statement per nominee
  through the documents service (type FINAL_SETTLEMENT_STATEMENT, subject PAYEE, never published to the
  employee's own portal); staff reach it through the documents API.

Services take (db, principal, ...) and never commit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.corrections import FinalSettlementCase
from app.models.employee import Employee
from app.models.nominees import NOMINEE_KINDS, Nominee
from app.models.pay_runs import PayrollRun, PayrollRunEmployee
from app.models.payment_flows import PaymentInstruction, PaymentObligation
from app.models.payments import BankAccount, BankAccountChangeRequest
from app.payroll import audit as payroll_audit
from app.payroll import countries
from app.payroll.banking import masking
from app.payroll.banking.errors import BankingError
from app.payroll.banking.nominee_shares import NomineeShare, split_net, validate_shares
from app.payroll.money import amount_str, to_decimal
from app.payroll.money_errors import MoneyError
from app.payroll.services import bank_accounts, jobs, outbox, step_up
from app.payroll.services import final_settlement as settlements
from app.payroll.services.wage_inputs import audit

EDIT = bank_accounts.EDIT
APPROVE = bank_accounts.APPROVE
VIEW_PERMISSIONS = bank_accounts.VIEW_PERMISSIONS
AUDIT_MODULE = payroll_audit.BANK_ACCOUNTS
ENTITY = "nominee"
EDITABLE_CASE = ("OPEN", "READY")  # the nominees of a settlement are frozen once its run exists
STATEMENT_JOB = "nominees.statements"
STATEMENT_DOC = "FINAL_SETTLEMENT_STATEMENT"

NOMINEE_ADDED = "NOMINEE_ADDED"
NOMINEE_UPDATED = "NOMINEE_UPDATED"
NOMINEE_ENDED = "NOMINEE_ENDED"
NOMINEE_SETTLEMENT_SPLIT = "NOMINEE_SETTLEMENT_SPLIT"
NOMINEE_STATEMENT_ISSUED = "NOMINEE_STATEMENT_ISSUED"

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")
MAX_SHARE_PLACES = 6


def _pct(value: Any) -> str:
    """A share as plain text without trailing zeros ("60", "33.333333")."""
    return format(Decimal(value).normalize(), "f")


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class NomineeData:
    name: str
    relationship: str
    proof_ref: str
    share_percent: Any
    country: str
    kind: str = "NOMINEE"
    email: Optional[str] = None


def _text(value: Optional[str], limit: int, *, minimum: int, label: str) -> str:
    cleaned = bank_accounts._clean(value, limit)
    if cleaned is None or len(cleaned) < minimum:
        raise AppError(422, [{"field": label, "code": "TEXT_REQUIRED",
                              "msg": f"{label} needs at least {minimum} characters"}], "TEXT_REQUIRED")
    return cleaned


def _share(value: Any) -> Decimal:
    try:
        share = to_decimal(value)
    except MoneyError as exc:
        raise AppError(422, [{"field": "share_percent", "code": exc.code, "msg": exc.message}], exc.code)
    exponent = share.as_tuple().exponent
    if not Decimal(0) < share <= Decimal(100):
        raise AppError(422, [{"field": "share_percent", "code": "SHARE_RANGE",
                              "msg": "a share is more than 0 and at most 100 percent"}], "SHARE_RANGE")
    if isinstance(exponent, int) and -exponent > MAX_SHARE_PLACES:
        raise AppError(422, [{"field": "share_percent", "code": "SHARE_PRECISION",
                              "msg": f"a share has at most {MAX_SHARE_PLACES} decimals"}], "SHARE_PRECISION")
    return share


def _country(value: str) -> str:
    code = (value or "").strip().upper()
    if countries.find(code) is None:
        raise AppError(422, [{"field": "country", "code": "COUNTRY_INVALID",
                              "msg": "the country must be an ISO 3166 two-letter code"}], "COUNTRY_INVALID")
    return code


def _email(value: Optional[str]) -> Optional[str]:
    cleaned = bank_accounts._clean(value, 254)
    if cleaned is None:
        return None
    if not _EMAIL.match(cleaned):
        raise AppError(422, [{"field": "email", "code": "EMAIL_INVALID", "msg": "enter a valid e-mail address"}],
                       "EMAIL_INVALID")
    return cleaned.lower()


def _clean(data: NomineeData) -> dict:
    kind = str(data.kind or "").strip().upper()
    if kind not in NOMINEE_KINDS:
        raise bad_request("The payee is a NOMINEE or a LEGAL_HEIR.", "INVALID_PAYEE")
    return {
        "kind": kind,
        "name": _text(data.name, 140, minimum=2, label="name"),
        "relationship": _text(data.relationship, 60, minimum=2, label="relationship"),
        "proof_ref": _text(data.proof_ref, 300, minimum=3, label="proof_ref"),
        "share_percent": _share(data.share_percent),
        "country": _country(data.country),
        "email": _email(data.email),
    }


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------
def _held(principal: Principal, permissions: Sequence[str]) -> List[str]:
    held = [p for p in permissions if principal.has(p)]
    if not held:
        raise forbidden()
    return held


def _not_the_deceased(principal: Optional[Principal], employee_id: int) -> None:
    """A login of the deceased employee never records, changes or ends the payees of their own settlement (the
    same rule as opening a settlement case: nobody settles their own employment)."""
    if principal is not None and principal.employee_id is not None and principal.employee_id == employee_id:
        raise forbidden("You cannot manage the payees of your own settlement.")


def _case_for(db: Session, principal: Principal, case_id: int, permissions: Sequence[str], *, lock: bool = False
              ) -> FinalSettlementCase:
    """The case when it is in the caller's scope for one of the permissions (403 without any, else 404)."""
    for permission in _held(principal, permissions):
        try:
            return settlements._load(db, principal, case_id, permission, lock=lock)
        except AppError as exc:
            if exc.status_code != 404:
                raise
    raise not_found("Final settlement")


def _row(db: Session, company_id: int, nominee_id: int, *, lock: bool = False) -> Optional[Nominee]:
    query = db.query(Nominee).filter(Nominee.company_id == company_id, Nominee.id == nominee_id)
    if lock:
        query = query.with_for_update().populate_existing()
    return query.first()


def get_nominee(db: Session, principal: Principal, nominee_id: int, permissions: Sequence[str] = VIEW_PERMISSIONS, *,
                lock: bool = False) -> Tuple[Nominee, FinalSettlementCase]:
    """A nominee of a case in the caller's scope; the case is locked first (then the nominee) when ``lock``."""
    _held(principal, permissions)
    found = _row(db, principal.company_id, nominee_id)
    if found is None:
        raise not_found("Nominee")
    case = _case_for(db, principal, found.final_settlement_case_id, permissions, lock=lock)
    nominee = _row(db, principal.company_id, nominee_id, lock=lock) if lock else found
    return nominee, case


def visible(db: Session, principal: Principal, permissions: Sequence[str], nominee_id: Optional[int]) -> bool:
    """Whether the nominee's case is in the caller's scope for one of the permissions."""
    if nominee_id is None or not any(principal.has(p) for p in permissions):
        return False
    try:
        get_nominee(db, principal, nominee_id, permissions)
    except AppError:
        return False
    return True


def payee_for_submission(db: Session, principal: Principal, nominee_id: int) -> Nominee:
    """The nominee a bank account is submitted for: ACTIVE, of a live DEATH case, in the caller's EDIT scope."""
    nominee, case = get_nominee(db, principal, nominee_id, (EDIT,))
    _not_the_deceased(principal, case.employee_id)
    if case.termination_type != "DEATH":
        raise conflict("Nominees are paid only for a final settlement after a death.", "NOT_A_DEATH_CASE")
    if nominee.status != "ACTIVE" or case.status == "CANCELLED":
        raise conflict("This nominee no longer receives payments.", "NOMINEE_ENDED")
    return nominee


def employee_of(db: Session, company_id: int, nominee_id: int) -> Optional[int]:
    return bank_accounts.nominee_employee_id(db, company_id, nominee_id)


def approval_context(db: Session, row: BankAccount) -> dict:
    """Context of the approval request of a nominee's account: the case scope (the deceased employee is not an
    approval subject, entity and pay group scope it) and what the approver checks."""
    nominee = _row(db, row.company_id, row.nominee_id)
    case = db.query(FinalSettlementCase).filter(FinalSettlementCase.company_id == row.company_id,
                                                FinalSettlementCase.id == nominee.final_settlement_case_id).one()
    return {"legal_entity_id": case.legal_entity_id, "pay_group_id": case.pay_group_id, "nominee_id": nominee.id,
            "final_settlement_case_id": case.id, "nominee_share_percent": _pct(nominee.share_percent)}


def staff_to_notify(db: Session, row: BankAccount, change: Optional[BankAccountChangeRequest]) -> List[int]:
    """The nominee has no login: staff who recorded the nominee and submitted the account are told."""
    nominee = _row(db, row.company_id, row.nominee_id)
    users = {nominee.created_by_user_id if nominee else None, row.created_by_user_id,
             change.requested_by_user_id if change is not None else None}
    users.discard(None)
    return sorted(users)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
def nominee_summary(db: Session, company_id: int, nominee_id: int) -> Optional[dict]:
    """What an approver needs about the payee of an account (never the account itself)."""
    nominee = _row(db, company_id, nominee_id)
    if nominee is None:
        return None
    return {"id": nominee.id, "kind": nominee.kind, "name": nominee.name, "relationship": nominee.relationship,
            "proof_ref": nominee.proof_ref, "share_percent": _pct(nominee.share_percent),
            "country": nominee.country, "status": nominee.status, "employee_id": nominee.employee_id,
            "final_settlement_case_id": nominee.final_settlement_case_id,
            "created_by_user_id": nominee.created_by_user_id}


def _accounts(db: Session, nominee: Nominee) -> List[BankAccount]:
    return (db.query(BankAccount)
            .filter(BankAccount.company_id == nominee.company_id, BankAccount.payee_type == "NOMINEE",
                    BankAccount.payee_id == nominee.id).order_by(BankAccount.id.desc()).all())


def nominee_json(db: Session, nominee: Nominee, *, accounts: bool = True) -> dict:
    data = {
        "id": nominee.id, "employee_id": nominee.employee_id, "final_settlement_case_id": nominee.final_settlement_case_id,
        "kind": nominee.kind, "name": nominee.name, "relationship": nominee.relationship,
        "proof_ref": nominee.proof_ref, "share_percent": _pct(nominee.share_percent),
        "country": nominee.country, "email_masked": bank_accounts.mask_email(nominee.email) or None,
        "status": nominee.status, "ended_at": nominee.ended_at, "ended_reason": nominee.ended_reason,
        "created_by_user_id": nominee.created_by_user_id, "created_at": nominee.created_at,
        "row_version": nominee.row_version,
    }
    if accounts:
        rows = _accounts(db, nominee)
        data["bank_accounts"] = [bank_accounts.serialize_account(r) for r in rows]
        now = clock.utcnow()
        data["has_usable_account"] = any(
            r.status == "ACTIVE" and r.usable_from is not None and r.usable_from <= now and not r.payment_hold
            and r.verification_status == "VERIFIED" for r in rows)
    return data


def _active(db: Session, case: FinalSettlementCase, *, lock: bool = False) -> List[Nominee]:
    query = db.query(Nominee).filter(Nominee.company_id == case.company_id,
                                     Nominee.final_settlement_case_id == case.id, Nominee.status == "ACTIVE")
    if lock:
        query = query.with_for_update().populate_existing()
    return query.order_by(Nominee.id).all()


def case_summary(db: Session, case: FinalSettlementCase) -> dict:
    """Shares and readiness of a DEATH case, for the case page and the run gate."""
    rows = _active(db, case)
    shares = [NomineeShare(n.id, Decimal(n.share_percent)) for n in rows]
    issues = validate_shares(shares)
    total = sum((s.share_percent for s in shares), Decimal(0))
    return {"active": len(rows), "share_total": _pct(total), "complete": not issues,
            "issues": [{"code": i.code, "message": i.message} for i in issues],
            "editable": case.status in EDITABLE_CASE and case.termination_type == "DEATH"}


def ensure_ready_for_run(db: Session, case: FinalSettlementCase) -> None:
    """A DEATH settlement run needs complete nominee shares (409 NOMINEES_INCOMPLETE)."""
    if case.termination_type != "DEATH":
        return
    summary = case_summary(db, case)
    if not summary["complete"]:
        issue = summary["issues"][0]
        raise conflict(f"Record the nominees before creating the settlement run: {issue['message']}.",
                       "NOMINEES_INCOMPLETE")


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
def _editable_case(case: FinalSettlementCase) -> None:
    if case.termination_type != "DEATH":
        raise conflict("Nominees are recorded only for a final settlement after a death.", "NOT_A_DEATH_CASE")
    if case.status not in EDITABLE_CASE:
        raise conflict("The nominees are frozen once the settlement run exists.", "CASE_LOCKED")


def _check_version(row, expected: Optional[int]) -> None:
    if expected is not None and expected != row.row_version:
        raise conflict("This record changed since you loaded it. Reload and try again.", "ROW_VERSION_CONFLICT")


def _audit(db: Session, principal: Optional[Principal], action: str, description: str, nominee: Nominee, **details) -> None:
    audit(db, principal, AUDIT_MODULE, action, description, ENTITY, nominee.id, nominee.company_id,
          nominee_id=nominee.id, final_settlement_case_id=nominee.final_settlement_case_id,
          employee_id=nominee.employee_id, kind=nominee.kind, name=masking.mask_name(nominee.name),
          share_percent=_pct(nominee.share_percent), country=nominee.country, **details)


def _total_other(db: Session, case: FinalSettlementCase, exclude_id: Optional[int]) -> Decimal:
    return sum((Decimal(n.share_percent) for n in _active(db, case) if n.id != exclude_id), Decimal(0))


def create_nominee(db: Session, principal: Principal, case_id: int, data: NomineeData) -> Nominee:
    case = _case_for(db, principal, case_id, (EDIT,), lock=True)
    _not_the_deceased(principal, case.employee_id)
    step_up.ensure_step_up(db, principal)
    _editable_case(case)
    values = _clean(data)
    if _total_other(db, case, None) + values["share_percent"] > Decimal(100):
        raise conflict("The shares of the nominees of one settlement add up to at most 100 percent.", "SHARES_EXCEED")
    nominee = Nominee(company_id=case.company_id, employee_id=case.employee_id, final_settlement_case_id=case.id,
                      status="ACTIVE", created_by_user_id=principal.user_id, row_version=1, **values)
    try:
        with db.begin_nested():
            db.add(nominee)
            db.flush()
    except IntegrityError as exc:
        if "uq_nominees_active_person" in str(exc.orig):
            raise conflict("This person is already a nominee of the settlement.", "DUPLICATE_NOMINEE")
        raise
    _audit(db, principal, NOMINEE_ADDED, f"Nominee #{nominee.id} added to final settlement #{case.id}", nominee)
    return nominee


def update_nominee(db: Session, principal: Principal, nominee_id: int, data: Mapping[str, Any], *,
                   expected_row_version: Optional[int] = None) -> Nominee:
    """Change the share, relationship, proof reference or e-mail; the name, kind and country never change once
    a bank account exists (end the nominee and add another)."""
    nominee, case = get_nominee(db, principal, nominee_id, (EDIT,), lock=True)
    _not_the_deceased(principal, case.employee_id)
    step_up.ensure_step_up(db, principal)
    _editable_case(case)
    if nominee.status != "ACTIVE":
        raise conflict("An ended nominee cannot be changed.", "NOMINEE_ENDED")
    _check_version(nominee, expected_row_version)
    allowed = {"name", "relationship", "proof_ref", "share_percent", "country", "kind", "email"}
    unknown = set(data) - allowed
    if unknown:
        raise bad_request(f"Unknown fields: {', '.join(sorted(unknown))}.", "UNKNOWN_FIELDS")
    merged = NomineeData(**{**{"name": nominee.name, "relationship": nominee.relationship,
                               "proof_ref": nominee.proof_ref, "share_percent": nominee.share_percent,
                               "country": nominee.country, "kind": nominee.kind, "email": nominee.email}, **data})
    values = _clean(merged)
    has_account = bool(_accounts(db, nominee))
    if has_account and (values["name"] != nominee.name or values["country"] != nominee.country
                        or values["kind"] != nominee.kind):
        raise conflict("The name, kind and country of a nominee with a bank account never change: end the nominee "
                       "and add the correct person.", "NOMINEE_HAS_ACCOUNT")
    if _total_other(db, case, nominee.id) + values["share_percent"] > Decimal(100):
        raise conflict("The shares of the nominees of one settlement add up to at most 100 percent.", "SHARES_EXCEED")
    before = {"share_percent": _pct(nominee.share_percent), "relationship": nominee.relationship,
              "proof_ref_changed": values["proof_ref"] != nominee.proof_ref}
    try:
        with db.begin_nested():
            for key, value in values.items():
                setattr(nominee, key, value)
            nominee.row_version += 1
            db.flush()
    except IntegrityError as exc:
        if "uq_nominees_active_person" in str(exc.orig):
            raise conflict("This person is already a nominee of the settlement.", "DUPLICATE_NOMINEE")
        raise
    _audit(db, principal, NOMINEE_UPDATED, f"Nominee #{nominee.id} changed", nominee, before=before)
    return nominee


def end_nominee(db: Session, principal: Principal, nominee_id: int, reason: str, *,
                expected_row_version: Optional[int] = None) -> Nominee:
    """The nominee stops receiving the settlement; their active accounts are deactivated. Refused while one of
    their bank changes is still open (decide it first)."""
    nominee, case = get_nominee(db, principal, nominee_id, (EDIT,), lock=True)
    _not_the_deceased(principal, case.employee_id)
    step_up.ensure_step_up(db, principal)
    _editable_case(case)
    if nominee.status != "ACTIVE":
        raise conflict("This nominee has already ended.", "NOMINEE_ENDED")
    _check_version(nominee, expected_row_version)
    reason = bank_accounts._reason(reason)
    _retire(db, principal, nominee, reason)
    nominee.status, nominee.ended_at = "ENDED", clock.utcnow()
    nominee.ended_reason, nominee.ended_by_user_id = reason, principal.user_id
    nominee.row_version += 1
    db.flush()
    _audit(db, principal, NOMINEE_ENDED, f"Nominee #{nominee.id} ended", nominee, reason=reason)
    return nominee


def _retire(db: Session, principal: Optional[Principal], nominee: Nominee, reason: str) -> int:
    """Deactivate the nominee's ACTIVE accounts (system action, audited by the bank account service)."""
    open_change = db.query(BankAccountChangeRequest.id).filter(
        BankAccountChangeRequest.company_id == nominee.company_id, BankAccountChangeRequest.payee_type == "NOMINEE",
        BankAccountChangeRequest.payee_id == nominee.id,
        BankAccountChangeRequest.status.in_(("SECURITY_REVIEW", "PENDING_APPROVAL"))).first()
    if open_change is not None:
        raise conflict("A bank change of this nominee is still open: approve, reject or withdraw it first.",
                       "CHANGE_PENDING")
    rows = (db.query(BankAccount).filter(BankAccount.company_id == nominee.company_id,
                                         BankAccount.payee_type == "NOMINEE", BankAccount.payee_id == nominee.id,
                                         BankAccount.status == "ACTIVE").with_for_update().all())
    now = clock.utcnow()
    for row in rows:
        bank_accounts._account_transition(
            db, principal, row, "DEACTIVATE", reason=reason, system=True,
            changes={"ended_at": max(now, row.usable_from or now),
                     "effective_to": max(clock.company_today(db, row.company_id), row.effective_from or now.date())},
            details={"nominee_ended": True})
    return len(rows)


def on_case_cancelled(db: Session, principal: Optional[Principal], case: FinalSettlementCase) -> int:
    """A cancelled case ends its nominees (accounts deactivated); open bank changes are withdrawn first."""
    ended = 0
    for nominee in _active(db, case, lock=True):
        for change in db.query(BankAccountChangeRequest).filter(
                BankAccountChangeRequest.company_id == nominee.company_id,
                BankAccountChangeRequest.payee_type == "NOMINEE", BankAccountChangeRequest.payee_id == nominee.id,
                BankAccountChangeRequest.status.in_(("SECURITY_REVIEW", "PENDING_APPROVAL"))).with_for_update():
            _withdraw_change(db, principal, change)
        _retire(db, principal, nominee, "The final settlement case was cancelled")
        nominee.status, nominee.ended_at = "ENDED", clock.utcnow()
        nominee.ended_reason = "The final settlement case was cancelled"
        nominee.ended_by_user_id = principal.user_id if principal is not None else nominee.created_by_user_id
        nominee.row_version += 1
        db.flush()
        _audit(db, principal, NOMINEE_ENDED, f"Nominee #{nominee.id} ended with its cancelled case", nominee,
               reason=nominee.ended_reason)
        ended += 1
    return ended


def _withdraw_change(db: Session, principal: Optional[Principal], change: BankAccountChangeRequest) -> None:
    from app.payroll.services import approvals

    if change.status == "PENDING_APPROVAL":
        request = approvals.pending_request(db, bank_accounts.MODULE, bank_accounts.SUBJECT, change.id)
        if request is not None:
            approvals.withdraw(db, principal, module=bank_accounts.MODULE, subject_type=bank_accounts.SUBJECT,
                               subject_id=change.id, reason="The final settlement case was cancelled")
    reason = "The final settlement case was cancelled"
    account = bank_accounts._lock_account(db, change.company_id, change.bank_account_id)
    if account.status == "PENDING_APPROVAL":
        bank_accounts._account_transition(db, principal, account, "REJECT", system=True, reason=reason)
    else:
        account.status_reason = reason
        account.row_version += 1
    change.status, change.decided_at = "CANCELLED", clock.utcnow()
    change.decision_reason = "The final settlement case was cancelled"
    change.row_version += 1
    db.flush()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def list_for_case(db: Session, principal: Principal, case_id: int) -> dict:
    case = _case_for(db, principal, case_id, VIEW_PERMISSIONS)
    rows = (db.query(Nominee).filter(Nominee.company_id == case.company_id,
                                     Nominee.final_settlement_case_id == case.id).order_by(Nominee.id).all())
    return {"case": {"id": case.id, "employee_id": case.employee_id, "termination_type": case.termination_type,
                     "status": case.status, "currency": case.currency, "run_id": case.run_id},
            "summary": case_summary(db, case) if case.termination_type == "DEATH" else None,
            "items": [nominee_json(db, n) for n in rows]}


def get_one(db: Session, principal: Principal, nominee_id: int) -> dict:
    nominee, case = get_nominee(db, principal, nominee_id)
    data = nominee_json(db, nominee)
    data["case"] = {"id": case.id, "status": case.status, "termination_type": case.termination_type,
                    "currency": case.currency, "run_id": case.run_id}
    data["editable"] = case.status in EDITABLE_CASE and nominee.status == "ACTIVE"
    return data


def list_nominees(db: Session, principal: Principal, *, case_id: Optional[int] = None,
                  employee_id: Optional[int] = None, status: Optional[str] = None, skip: int = 0,
                  limit: int = 50) -> Tuple[int, List[dict]]:
    """Nominees of the cases in the caller's scope (one case, one employee, or all)."""
    _held(principal, VIEW_PERMISSIONS)
    if case_id is not None:
        data = list_for_case(db, principal, case_id)
        items = [i for i in data["items"] if status is None or i["status"] == status]
        return len(items), items[skip:skip + limit]
    query = db.query(Nominee).filter(Nominee.company_id == principal.company_id)
    if employee_id is not None:
        query = query.filter(Nominee.employee_id == employee_id)
    if status:
        query = query.filter(Nominee.status == status)
    visible_rows = [n for n in query.order_by(Nominee.id.desc()).all()
                    if visible(db, principal, VIEW_PERMISSIONS, n.id)]
    return len(visible_rows), [nominee_json(db, n) for n in visible_rows[skip:skip + limit]]


def list_changes(db: Session, principal: Principal, *, status: Optional[str] = None, skip: int = 0, limit: int = 50
                 ) -> Tuple[int, List[dict]]:
    """Bank change requests of nominee accounts in the caller's scope (the generic list shows employees')."""
    _held(principal, VIEW_PERMISSIONS)
    query = db.query(BankAccountChangeRequest).filter(BankAccountChangeRequest.company_id == principal.company_id,
                                                      BankAccountChangeRequest.payee_type == "NOMINEE")
    if status:
        query = query.filter(BankAccountChangeRequest.status == status)
    rows = [c for c in query.order_by(BankAccountChangeRequest.id.desc()).all()
            if visible(db, principal, VIEW_PERMISSIONS, c.nominee_id)]
    return len(rows), [bank_accounts.serialize_change_request(db, c) for c in rows[skip:skip + limit]]


def payments_of_case(db: Session, principal: Principal, case_id: int) -> dict:
    """The settlement's obligation and its instruction per nominee (masked), for the case page."""
    case = _case_for(db, principal, case_id, VIEW_PERMISSIONS)
    obligations = (db.query(PaymentObligation).filter(
        PaymentObligation.company_id == case.company_id, PaymentObligation.run_id == case.run_id,
        PaymentObligation.employee_id == case.employee_id, PaymentObligation.payee_type == "NOMINEE")
        .order_by(PaymentObligation.id).all()) if case.run_id is not None else []
    out = []
    for obligation in obligations:
        rows = (db.query(PaymentInstruction).filter(PaymentInstruction.company_id == case.company_id,
                                                    PaymentInstruction.obligation_id == obligation.id)
                .order_by(PaymentInstruction.id).all())
        out.append({"obligation_id": obligation.id, "status": obligation.status,
                    "amount": {"amount": amount_str(obligation.amount, obligation.currency),
                               "currency": obligation.currency},
                    "instructions": [{
                        "id": r.id, "reference": r.reference, "status": r.status, "reason": r.status_reason,
                        "amount": {"amount": amount_str(r.amount, r.currency), "currency": r.currency},
                        "nominee_id": (r.beneficiary_snapshot or {}).get("nominee_id"),
                        "share_percent": (r.beneficiary_snapshot or {}).get("share_percent"),
                        "account_masked": (r.beneficiary_snapshot or {}).get("account_masked"),
                        "holder_name_masked": (r.beneficiary_snapshot or {}).get("holder_name_masked"),
                    } for r in rows]})
    return {"case_id": case.id, "obligations": out}


# ---------------------------------------------------------------------------
# Payment hooks (called by services.obligations)
# ---------------------------------------------------------------------------
def death_case_for(db: Session, run: PayrollRun, row: PayrollRunEmployee) -> Optional[FinalSettlementCase]:
    """The DEATH final-settlement case settled by this run employee, when there is one."""
    case_id = (run.selection or {}).get("final_settlement_case_id")
    if run.run_type != "FINAL_SETTLEMENT" or not isinstance(case_id, int):
        return None
    case = db.query(FinalSettlementCase).filter(
        FinalSettlementCase.company_id == run.company_id, FinalSettlementCase.id == case_id,
        FinalSettlementCase.employee_id == row.employee_id, FinalSettlementCase.run_id == run.id).first()
    return case if case is not None and case.termination_type == "DEATH" else None


def nominee_of_instruction(row: PaymentInstruction) -> Tuple[bool, Optional[int]]:
    """(payee is a nominee, nominee id) from the beneficiary snapshot's marker."""
    snapshot = row.beneficiary_snapshot or {}
    if snapshot.get("payee_type") != "NOMINEE":
        return False, None
    nominee_id = snapshot.get("nominee_id")
    return True, nominee_id if isinstance(nominee_id, int) else None


def payee_accounts(db: Session, company_id: int, nominee_id: int, at=None):
    """The accounts of an ACTIVE nominee serving at ``at`` (an ended nominee is paid nothing)."""
    from app.payroll.services import obligations

    nominee = _row(db, company_id, nominee_id)
    if nominee is None or nominee.status != "ACTIVE":
        return []
    return obligations.payee_accounts(db, company_id, nominee_id, at, payee_type="NOMINEE")


def _case_of_obligation(db: Session, obligation: PaymentObligation) -> Optional[FinalSettlementCase]:
    return db.query(FinalSettlementCase).filter(
        FinalSettlementCase.company_id == obligation.company_id,
        FinalSettlementCase.employee_id == obligation.employee_id, FinalSettlementCase.run_id == obligation.run_id,
        FinalSettlementCase.termination_type == "DEATH").first()


def _snapshot_extra(nominee: Nominee, share: Decimal) -> dict:
    return {"payee_type": "NOMINEE", "nominee_id": nominee.id, "nominee_kind": nominee.kind,
            "nominee_name_masked": masking.mask_name(nominee.name), "share_percent": _pct(share),
            "final_settlement_case_id": nominee.final_settlement_case_id}


def build_for_obligation(db: Session, obligation: PaymentObligation, *, amount: Optional[Decimal] = None,
                         hold_reason: Optional[str] = None, reissue_of: Optional[int] = None,
                         replaces: Optional[PaymentInstruction] = None, principal: Optional[Principal] = None
                         ) -> List[PaymentInstruction]:
    """Instructions of a NOMINEE obligation. Without ``replaces``: one per nominee share of the obligation (a
    placeholder when the shares are incomplete). With ``replaces`` (a cancelled or reissued instruction):
    that nominee's instruction again for ``amount``."""
    from app.payroll.services import obligations

    total = Decimal(obligation.amount) if amount is None else amount
    if replaces is not None:
        _, nominee_id = nominee_of_instruction(replaces)
        nominee = _row(db, obligation.company_id, nominee_id) if nominee_id is not None else None
        if nominee is None:
            return obligations.build_instructions(
                db, obligation, amount=total, hold_reason=hold_reason, reissue_of=reissue_of, single_account=True,
                principal=principal, accounts=[], snapshot_extra={"payee_type": "NOMINEE"})
        snapshot = replaces.beneficiary_snapshot or {}
        share = Decimal(str(snapshot.get("share_percent") or nominee.share_percent))
        return obligations.build_instructions(
            db, obligation, amount=total, hold_reason=hold_reason, reissue_of=reissue_of, single_account=True,
            principal=principal, accounts=payee_accounts(db, obligation.company_id, nominee.id),
            snapshot_extra=_snapshot_extra(nominee, share))
    case = _case_of_obligation(db, obligation)
    nominees = _active(db, case) if case is not None else []
    shares = [NomineeShare(n.id, Decimal(n.share_percent)) for n in nominees]
    issues = validate_shares(shares)
    if issues:  # fail closed: never the deceased employee's own account, never a guessed split
        note = f"Nominee shares are not complete: {issues[0].message}"
        created = obligations.build_instructions(
            db, obligation, amount=total, hold_reason=hold_reason, reissue_of=reissue_of, single_account=True,
            principal=principal, accounts=[], snapshot_extra={"payee_type": "NOMINEE", "nominee_issue": issues[0].code})
        for row in created:
            row.status_note = note[:1000]
        db.flush()
        return created
    try:
        lines = split_net(total, obligation.currency, shares)
    except BankingError as exc:
        raise conflict(exc.message, exc.code)
    by_id = {n.id: n for n in nominees}
    created: List[PaymentInstruction] = []
    for line in lines:
        if line.amount <= 0:
            continue
        nominee = by_id[line.nominee_id]
        created.extend(obligations.build_instructions(
            db, obligation, amount=line.amount, hold_reason=hold_reason, reissue_of=reissue_of, single_account=True,
            principal=principal, accounts=payee_accounts(db, obligation.company_id, nominee.id),
            snapshot_extra=_snapshot_extra(nominee, line.share_percent)))
    from app.payroll.services import payment_events as events

    events.audit(db, principal, NOMINEE_SETTLEMENT_SPLIT,
                 f"Settlement of employee #{obligation.employee_id} divided among {len(lines)} nominee(s)",
                 "payment_obligation", obligation.id, obligation.company_id, final_settlement_case_id=case.id,
                 currency=obligation.currency, net=amount_str(total, obligation.currency),
                 shares=[{"nominee_id": l.nominee_id, "share_percent": _pct(l.share_percent),
                          "amount": amount_str(l.amount, obligation.currency), "remainder": l.remainder}
                         for l in lines])
    return created


# ---------------------------------------------------------------------------
# Statement per payee (documents service)
# ---------------------------------------------------------------------------
def _statement_key(nominee: Nominee, run_employee_id: int) -> str:
    return f"nominee:{nominee.id}:re:{run_employee_id}"


def issue_statements(db: Session, company_id: int, case_id: int, principal: Optional[Principal] = None) -> list:
    """The settlement statement of every nominee of a settled DEATH case (idempotent per nominee and run)."""
    from app.models.pay_runs import PayrollLine
    from app.payroll.services import documents, registers

    case = db.query(FinalSettlementCase).filter(FinalSettlementCase.company_id == company_id,
                                                FinalSettlementCase.id == case_id).first()
    if case is None or case.termination_type != "DEATH" or case.run_id is None:
        return []
    run = db.query(PayrollRun).filter(PayrollRun.company_id == company_id, PayrollRun.id == case.run_id).one()
    from app.payroll import state_machines as sm

    if not sm.is_run_finalized(run.status):
        raise conflict("The settlement run is not finalized yet.", "NOT_A_FINALIZED_SETTLEMENT")
    row = (db.query(PayrollRunEmployee).filter(PayrollRunEmployee.company_id == company_id,
                                               PayrollRunEmployee.run_id == run.id,
                                               PayrollRunEmployee.employee_id == case.employee_id,
                                               PayrollRunEmployee.status == "CALCULATED").first())
    if row is None or row.net is None:
        return []
    employee = db.query(Employee).filter(Employee.company_id == company_id, Employee.id == case.employee_id).one()
    period = registers._period(db, run)
    nominees = (db.query(Nominee).filter(Nominee.company_id == company_id, Nominee.final_settlement_case_id == case.id)
                .order_by(Nominee.id).all())
    active = [n for n in nominees if n.status == "ACTIVE"]
    try:
        amounts = {l.nominee_id: l for l in split_net(Decimal(row.net), row.currency,
                                                      [NomineeShare(n.id, Decimal(n.share_percent)) for n in active])}
    except BankingError:
        amounts = {}
    lines = (db.query(PayrollLine).filter(PayrollLine.company_id == company_id, PayrollLine.run_employee_id == row.id,
                                          PayrollLine.visibility == "EMPLOYEE_VISIBLE")
             .order_by(PayrollLine.sort_order, PayrollLine.id).all())
    items = [line for line in lines if line.kind in registers.SETTLEMENT_KINDS]
    issued = []
    for nominee in active:
        key = _statement_key(nominee, row.id)
        existing = documents.find_version(db, company_id, STATEMENT_DOC, key, 1)
        if existing is not None:
            issued.append(existing)
            continue
        share = amounts.get(nominee.id)
        content = registers.base_content(db, company_id, run.legal_entity_id, "Final settlement statement",
                                         subtitle="Payable to the nominee or legal heir")
        info = [["Payee", f"{nominee.name} ({nominee.relationship})"],
                ["Payee type", nominee.kind.replace("_", " ").title()],
                ["Proof document", nominee.proof_ref],
                ["Deceased employee", employee.full_name], ["Employee ID", employee.employee_code],
                ["Last working day", case.last_working_day.isoformat()],
                ["Share of the settlement", f"{_pct(nominee.share_percent)} %"],
                ["Amount payable to this payee",
                 f"{row.currency} {registers._money(share.amount, row.currency)}" if share else "—"],
                ["Payment due date", (case.due_date or period.pay_date).isoformat()], ["Run", run.number]]
        tables = [{"title": "Settlement of the deceased employee",
                   "columns": ["Item", "Type", "Calculation", f"Amount ({row.currency})"],
                   "numeric": [False, False, False, True], "widths": [1.6, 1, 3, 1.3],
                   "rows": [[line.component_code, line.kind.replace("_", " ").title(), line.explanation or "",
                             registers._money(-Decimal(line.amount) if line.kind == "DEDUCTION" else line.amount,
                                              line.currency)] for line in items],
                   "total_row": ["Net settlement", "", "", registers._money(row.net, row.currency)]},
                  {"title": "Division among the payees",
                   "columns": ["Payee", "Relationship", "Share", f"Amount ({row.currency})"],
                   "numeric": [False, False, True, True], "widths": [2, 1.4, 1, 1.5],
                   "rows": [[n.name, n.relationship, f"{_pct(n.share_percent)} %",
                             registers._money(amounts[n.id].amount, row.currency) if n.id in amounts else "—"]
                            for n in active]}]
        content.update({"info": info, "tables": tables, "labels": registers.run_labels(db, run),
                        "notes": ["Rounding differences are paid to the payee with the largest share."]})
        doc = registers._issue(
            db, principal, content, doc_type=STATEMENT_DOC, legal_entity_id=run.legal_entity_id,
            subject_type="PAYEE", subject_id=nominee.id, source_key=key, title="Final settlement statement (payee)",
            file_stem=f"final-settlement-payee-{employee.employee_code}-{nominee.id}", employee_id=employee.id,
            pay_group_id=run.pay_group_id, run_id=run.id, run_employee_id=row.id,
            period=(period.period_start, period.period_end), publish=None, company_id=company_id,
            source_refs={"run_id": run.id, "run_employee_id": row.id, "case_id": case.id, "nominee_id": nominee.id})
        audit(db, principal, AUDIT_MODULE, NOMINEE_STATEMENT_ISSUED,
              f"Settlement statement issued for nominee #{nominee.id}", ENTITY, nominee.id, company_id,
              document_id=doc.id, final_settlement_case_id=case.id)
        issued.append(doc)
    return issued


def statements_of(db: Session, principal: Principal, nominee_id: int) -> List[dict]:
    """The settlement statements issued for one nominee (documents API ids; open them there)."""
    from app.models.pay_documents import PayDocument

    nominee, _ = get_nominee(db, principal, nominee_id)
    rows = (db.query(PayDocument).filter(PayDocument.company_id == nominee.company_id,
                                         PayDocument.doc_type == STATEMENT_DOC, PayDocument.subject_type == "PAYEE",
                                         PayDocument.subject_id == nominee.id).order_by(PayDocument.id).all())
    return [{"document_id": d.id, "number": d.number, "title": d.title, "status": d.status, "issue_date": d.issue_date,
             "sha256": d.sha256, "bytes": d.bytes} for d in rows]


def issue_statement(db: Session, principal: Principal, nominee_id: int) -> List[dict]:
    """HR asks for the statements of the nominee's case now (the event normally does it; idempotent)."""
    from app.payroll.services import documents

    documents.require_staff(principal, documents.PUBLISH)
    nominee, case = get_nominee(db, principal, nominee_id, (EDIT, APPROVE) + VIEW_PERMISSIONS)
    if case.status != "SETTLED":
        raise conflict("The statement exists once the settlement run is finalized.", "NOT_SETTLED")
    issue_statements(db, nominee.company_id, case.id, principal)
    return statements_of(db, principal, nominee_id)


@jobs.register(STATEMENT_JOB)
def _on_settled(ctx: jobs.JobContext) -> dict:
    case_id = ctx.payload.get("case_id")
    if not isinstance(case_id, int):
        raise jobs.PermanentJobError("payroll.final_settlement.finalized needs a case_id")
    try:
        docs = issue_statements(ctx.db, ctx.company_id, case_id)
    except AppError as exc:
        if exc.status_code == 409:
            raise jobs.PermanentJobError(str(exc.detail)) from exc
        raise
    return {"case_id": case_id, "statements": [d.id for d in docs]}


outbox.subscribe(settlements.FINALIZED_TOPIC, STATEMENT_JOB)

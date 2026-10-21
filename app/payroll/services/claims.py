"""Reimbursement claims (3.4 §14, 3.1 §5.10; EC-VAR-04, EC-VAR-09, EC-VAR-15).

* A claim names a claim type of the employee's REIMBURSEMENT_POLICY (resolved for the expense date),
  the expense date, the amount and currency, the merchant and receipts. Limits per claim, month and
  year, the receipt threshold, the submission window and the duplicate check come from
  ``app.payroll.time_pay_claims.check_claim``; problems are flags for the approver.
* A foreign-currency claim is converted into the pay group currency with the FX service at the
  expense date and the conversion is stored (EC-VAR-09); without a usable rate the claim is refused,
  never converted 1:1. A policy whose fx_rate_date is APPROVAL_DATE is converted again at the final
  approval (a missing rate then blocks the approval) and that conversion is the one kept.
* Receipts are kept in private storage with their SHA-256; the duplicate fingerprint (receipt, amount,
  date, merchant) and a receipt already used by another claim of the tenant flag the claim
  POSSIBLE_DUPLICATE for the approver (EC-VAR-04). Claimants never see other people's claim ids.
* Status follows the CLAIM machine: SUBMITTED -> APPROVED -> SCHEDULED -> PAID, or REJECTED. Approval
  goes through the approvals engine (module reimbursement). At the final approval the checks run
  again: a missing required receipt, an unknown type or a missing rate blocks the decision; the
  approved amount is the claim capped by the remaining limits. A request withdrawn by the requester
  ends REJECTED ("withdrawn"), the machine having no other end state for a submitted claim.
* Payout route (payroll.reimbursements.payout_route) WITH_PAYROLL feeds ``claims_for_payroll`` (a
  REIMBURSEMENT line that adds to net, never wages); SEPARATE_PAYMENT feeds ``claims_for_payment``.
  Runs and payment batches move claims with ``schedule_claims``, ``release_claims`` and
  ``mark_claims_paid``.
"""

import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session, selectinload

from app.config import settings as app_settings
from app.core import clock, storage
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.catalogue import FxConversion
from app.models.variable_inputs import ReimbursementClaim, ReimbursementReceipt
from app.payroll import audit as payroll_audit
from app.payroll import currencies
from app.payroll import fx as fxlib
from app.payroll import state_machines as sm
from app.payroll import time_pay_claims as tpc
from app.payroll.money import Money, amount_str, is_storable
from app.payroll.money_errors import MoneyError
from app.payroll.services import approvals
from app.payroll.services import fx as fx_service
from app.payroll.services import policies
from app.payroll.services.wage_inputs import (
    Placement,
    audit,
    company_today,
    covers,
    money_json,
    placement,
    resolve_setting,
    scope_clause,
    scoped_placement,
    transition,
)
from app.payroll.time_pay_common import TimePayError

READ = "payroll:reimbursements:read"
MANAGE = "payroll:reimbursements:manage"
APPROVE = "payroll:reimbursements:approve"
MODULE = "reimbursement"
SUBJECT = "reimbursement_claim"
AUDIT_MODULE = payroll_audit.REIMBURSEMENTS
POLICY_TYPE = "REIMBURSEMENT_POLICY"
RECEIPT_CATEGORY = "claim-receipts"
MAX_RECEIPTS = 10
RECEIPT_TYPES = (
    (b"%PDF-", "pdf", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
)
WITHDRAWN = "Withdrawn by the requester"


@dataclass(frozen=True)
class ClaimData:
    employee_id: int
    claim_type: str
    expense_date: date
    amount: Decimal
    currency: str
    merchant: Optional[str] = None
    description: Optional[str] = None


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
def _receipt_json(row: ReimbursementReceipt) -> dict:
    return {"id": row.id, "file_name": row.file_name, "sha256": row.sha256, "mime": row.mime, "bytes": row.bytes,
            "uploaded_at": row.created_at}


def _self_flags(flags: Sequence[dict]) -> List[dict]:
    """Flags as the claimant sees them: no other claims' ids."""
    return [{"code": f.get("code"), "severity": f.get("severity"),
             "message": "This receipt may already have been claimed." if f.get("code") == "CLAIM_POSSIBLE_DUPLICATE"
             else f.get("message")} for f in flags]


def serialize(db: Session, row: ReimbursementClaim, *, for_self: bool = False) -> dict:
    conversion = None
    if row.fx_conversion_id is not None:
        conv = db.query(FxConversion).filter(FxConversion.company_id == row.company_id,
                                             FxConversion.id == row.fx_conversion_id).first()
        if conv is not None:
            conversion = fx_service.serialize_conversion(conv)
    flags = list(row.flags or [])
    return {
        "id": row.id,
        "employee_id": row.employee_id,
        "legal_entity_id": row.legal_entity_id,
        "pay_group_id": row.pay_group_id,
        "claim_type": row.claim_type,
        "claim_type_name": row.claim_type_name,
        "taxability": row.taxability,
        "expense_date": row.expense_date.isoformat(),
        "amount": money_json(row.amount, row.currency),
        "merchant": row.merchant,
        "description": row.description,
        "payout_route": row.payout_route,
        "payout": money_json(row.payout_amount, row.payout_currency),
        "approved": money_json(row.approved_amount, row.payout_currency),
        "fx_conversion": conversion,
        "status": row.status,
        "flags": _self_flags(flags) if for_self else flags,
        "approval_request_id": row.approval_request_id,
        "receipts": [_receipt_json(r) for r in row.receipts],
        "paid_ref": row.paid_ref,
        "decided_at": row.decided_at,
        "status_reason": row.status_reason,
        "row_version": row.row_version,
        "created_at": row.created_at,
    }


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def _policy(db: Session, company_id: int, employee_id: int, on: date) -> dict:
    found = policies.resolve_for_employee(db, company_id, POLICY_TYPE, employee_id, on)
    if found.get("status") != "RESOLVED":
        raise conflict(found.get("reason") or "No reimbursement policy applies to this employee.", "NO_POLICY")
    return found["version"]


def _claim_type_name(fields: dict, code: str) -> str:
    for item in fields.get("claim_types") or []:
        if item.get("code") == code:
            return item.get("name") or code
    return code


def _as_claim(row: ReimbursementClaim, digests: Sequence[str], submitted_on: Optional[date]) -> tpc.Claim:
    return tpc.Claim(str(row.id), row.claim_type, row.expense_date, Decimal(row.amount), row.currency,
                     row.merchant or "", tuple(digests), submitted_on, row.status)


def _evaluate(db: Session, row: ReimbursementClaim, fields: dict) -> tpc.ClaimCheck:
    """check_claim for a stored claim: prior claims of the employee, fingerprints and receipts of the tenant."""
    receipts = list(row.receipts)
    priors = (
        db.query(ReimbursementClaim)
        .filter(ReimbursementClaim.company_id == row.company_id, ReimbursementClaim.employee_id == row.employee_id,
                ReimbursementClaim.claim_type == row.claim_type, ReimbursementClaim.id != row.id)
        .all()
    )
    prior_claims = [(tpc.Claim(str(p.id), p.claim_type, p.expense_date, Decimal(p.amount), p.currency,
                               status=p.status), Decimal(p.approved_amount if p.approved_amount is not None
                                                         else p.payout_amount)) for p in priors]
    known_fingerprints, known_receipts = {}, {}
    if receipts:
        hashes = [r.sha256 for r in receipts]
        fingerprints = [tpc.claim_fingerprint(r.sha256, Decimal(row.amount), row.currency, row.expense_date,
                                              row.merchant or "") for r in receipts]
        for other in (
            db.query(ReimbursementReceipt.claim_id, ReimbursementReceipt.sha256, ReimbursementReceipt.fingerprint)
            .join(ReimbursementClaim, (ReimbursementClaim.company_id == ReimbursementReceipt.company_id)
                  & (ReimbursementClaim.id == ReimbursementReceipt.claim_id))
            .filter(ReimbursementReceipt.company_id == row.company_id, ReimbursementReceipt.claim_id != row.id,
                    ReimbursementClaim.status != sm.ClaimStatus.REJECTED,
                    or_(ReimbursementReceipt.sha256.in_(hashes), ReimbursementReceipt.fingerprint.in_(fingerprints)))
        ):
            known_receipts.setdefault(other.sha256, str(other.claim_id))
            known_fingerprints.setdefault(other.fingerprint, str(other.claim_id))
    rate = rate_date = None
    if row.fx_conversion_id is not None:
        conv = db.query(FxConversion).filter(FxConversion.company_id == row.company_id,
                                             FxConversion.id == row.fx_conversion_id).one()
        rate, rate_date = Decimal(conv.rate), conv.rate_date
    submitted_on = row.created_at.date() if row.created_at else None
    try:
        return tpc.check_claim(
            _as_claim(row, [r.sha256 for r in receipts], submitted_on), fields, pay_currency=row.payout_currency,
            prior_claims=prior_claims, known_fingerprints=known_fingerprints, known_receipts=known_receipts,
            fx_rate=rate, fx_rate_date=rate_date,
        )
    except TimePayError as exc:
        raise conflict(f"The reimbursement policy cannot be applied: {exc}", "POLICY_INVALID")


def _flags(check: tpc.ClaimCheck) -> List[dict]:
    return [issue.as_dict() for issue in check.issues]


def _convert(db: Session, amount: Decimal, currency: str, target: str, on: date) -> FxConversion:
    """The stored FX conversion of a claim amount (409 without a usable rate: never 1:1)."""
    try:
        conversion = fx_service.convert(db, Money(amount, currency), target, on=on, purpose="REIMBURSEMENT")
    except fxlib.FxError as exc:
        raise conflict(f"No usable {currency}->{target} rate for {on.isoformat()}: {exc.message} "
                       "The claim cannot be paid until a rate exists.", "CLAIM_FX_RATE_MISSING")
    if Decimal(conversion.result_amount) <= 0:
        raise bad_request("The converted amount rounds to zero.", "AMOUNT_NOT_POSITIVE")
    return conversion


def _policy_fields(db: Session, row: ReimbursementClaim) -> dict:
    from app.models.payroll_config import PolicyVersion

    version = db.query(PolicyVersion).filter(PolicyVersion.company_id == row.company_id,
                                             PolicyVersion.id == row.policy_version_id).one()
    return dict(version.fields or {})


# ---------------------------------------------------------------------------
# Submit, receipts, decisions
# ---------------------------------------------------------------------------
def _own_placement(db: Session, principal: Principal, on: date) -> Placement:
    if principal.employee_id is None:
        raise not_found("Employee profile")
    place = placement(db, principal.company_id, principal.employee_id, on)
    if not any(covers(principal, code, place.legal_entity_id, place.pay_group_id, principal.employee_id)
               for code in (MANAGE, READ)):
        raise forbidden()
    return place


def submit_claim(db: Session, principal: Principal, data: ClaimData, *, self_service: bool = False
                 ) -> ReimbursementClaim:
    """Create a SUBMITTED claim and route it for approval.

    Payroll staff need payroll:reimbursements:manage at a scope covering the employee. A claimant
    submits their own claim through self-service with payroll:reimbursements:manage or :read at SELF
    (3.9 §5.1: SELF means "apply/read own").
    """
    company_id = principal.company_id
    if self_service:
        if principal.employee_id is None or data.employee_id != principal.employee_id:
            raise not_found("Employee")
        place = _own_placement(db, principal, data.expense_date)
    else:
        place = scoped_placement(db, principal, MANAGE, data.employee_id, data.expense_date, allow_self=True)
    today = company_today(db, company_id)
    if data.expense_date > today:
        raise bad_request("The expense date is in the future.", "EXPENSE_IN_FUTURE")
    if not resolve_setting(db, company_id, "payroll.reimbursements.enabled", on=data.expense_date,
                           pay_group_id=place.pay_group_id):
        raise conflict("Reimbursements are switched off for this employee.", "REIMBURSEMENTS_DISABLED")
    try:
        currency = currencies.require_payroll_currency(data.currency).code
        amount_str(data.amount, currency)
        if data.amount <= 0 or not is_storable(data.amount):
            raise MoneyError("AMOUNT_NOT_POSITIVE", "The amount must be greater than zero.")
    except MoneyError as exc:
        raise bad_request(exc.message, exc.code)
    version = _policy(db, company_id, data.employee_id, data.expense_date)
    fields = dict(version["fields"] or {})
    try:
        kind = tpc.claim_type(fields, data.claim_type, place.currency)
    except TimePayError as exc:
        raise conflict(f"The reimbursement policy cannot be applied: {exc}", "POLICY_INVALID")
    if kind is None:
        raise bad_request(f"Claim type {data.claim_type} is not in your reimbursement policy.", "CLAIM_TYPE_UNKNOWN")
    route = resolve_setting(db, company_id, "payroll.reimbursements.payout_route", on=data.expense_date,
                            legal_entity_id=place.legal_entity_id)

    conversion = None
    payout = data.amount
    if currency != place.currency:
        conversion = _convert(db, data.amount, currency, place.currency, data.expense_date)
        payout = Decimal(conversion.result_amount)

    row = ReimbursementClaim(
        company_id=company_id, employee_id=data.employee_id, legal_entity_id=place.legal_entity_id,
        pay_group_id=place.pay_group_id, policy_version_id=version["id"], claim_type=data.claim_type,
        claim_type_name=_claim_type_name(fields, data.claim_type)[:100], taxability=kind.taxability,
        expense_date=data.expense_date, amount=data.amount, currency=currency, merchant=data.merchant,
        description=data.description, payout_route=route, payout_amount=payout, payout_currency=place.currency,
        fx_conversion_id=conversion.id if conversion is not None else None, status=sm.ClaimStatus.SUBMITTED,
        flags=[], submitted_by_user_id=principal.user_id, row_version=1,
    )
    db.add(row)
    db.flush()
    if conversion is not None:
        conversion.ref_type, conversion.ref_id = SUBJECT, row.id
    row.flags = _flags(_evaluate(db, row, fields))
    request = approvals.submit(
        db, principal, module=MODULE, subject_type=SUBJECT, subject_id=row.id, subject_employee_id=row.employee_id,
        amount=payout, currency=place.currency,
        context={"legal_entity_id": row.legal_entity_id, "pay_group_id": row.pay_group_id,
                 "claim_type": row.claim_type},
    )
    row.approval_request_id = request.id
    db.flush()
    audit(db, principal, AUDIT_MODULE, "CLAIM_SUBMITTED",
          f"Submitted {row.claim_type} claim #{row.id} for employee #{row.employee_id}", SUBJECT, row.id, company_id,
          amount=money_json(row.amount, row.currency), payout=money_json(row.payout_amount, row.payout_currency),
          fx_conversion_id=row.fx_conversion_id, payout_route=row.payout_route,
          flags=[f["code"] for f in row.flags], self_service=self_service)
    return row


def _load(db: Session, principal: Principal, claim_id: int, permission: str, *, lock: bool = False,
          own: bool = False) -> ReimbursementClaim:
    query = db.query(ReimbursementClaim).options(selectinload(ReimbursementClaim.receipts)).filter(
        ReimbursementClaim.company_id == principal.company_id, ReimbursementClaim.id == claim_id)
    if own:
        if principal.employee_id is None:
            raise not_found("Claim")
        query = query.filter(ReimbursementClaim.employee_id == principal.employee_id)
    else:
        if not principal.has(permission):
            raise forbidden()
        query = query.filter(scope_clause(principal, permission, ReimbursementClaim))
    if lock:
        query = query.with_for_update(of=ReimbursementClaim).populate_existing()
    row = query.first()
    if row is None:
        raise not_found("Claim")
    if own and not any(covers(principal, code, row.legal_entity_id, row.pay_group_id, principal.employee_id)
                       for code in (READ, MANAGE)):
        raise not_found("Claim")
    return row


def get_claim(db: Session, principal: Principal, claim_id: int, *, own: bool = False) -> dict:
    return serialize(db, _load(db, principal, claim_id, READ, own=own), for_self=own)


def list_claims(db: Session, principal: Principal, *, own: bool = False, status: Optional[str] = None,
                employee_id: Optional[int] = None, claim_type: Optional[str] = None, skip: int = 0,
                limit: int = 50) -> Tuple[int, List[dict]]:
    query = db.query(ReimbursementClaim).options(selectinload(ReimbursementClaim.receipts)).filter(
        ReimbursementClaim.company_id == principal.company_id)
    if own:
        if principal.employee_id is None or not (principal.has(READ) or principal.has(MANAGE)):
            raise forbidden()
        query = query.filter(ReimbursementClaim.employee_id == principal.employee_id)
    else:
        if not principal.has(READ):
            raise forbidden()
        query = query.filter(scope_clause(principal, READ, ReimbursementClaim))
    if status:
        query = query.filter(ReimbursementClaim.status == status)
    if employee_id is not None:
        query = query.filter(ReimbursementClaim.employee_id == employee_id)
    if claim_type:
        query = query.filter(ReimbursementClaim.claim_type == claim_type)
    total = query.count()
    rows = query.order_by(ReimbursementClaim.expense_date.desc(), ReimbursementClaim.id.desc()) \
        .offset(skip).limit(limit).all()
    return total, [serialize(db, r, for_self=own) for r in rows]


def _sniff(data: bytes) -> Tuple[str, str]:
    for magic, ext, mime in RECEIPT_TYPES:
        if data.startswith(magic):
            return ext, mime
    raise bad_request("Receipts must be PDF, PNG or JPEG files.", "RECEIPT_TYPE")


def add_receipt(db: Session, principal: Principal, claim_id: int, file_name: str, data: bytes, *,
                own: bool = False) -> ReimbursementReceipt:
    """Attach a receipt while the claim is SUBMITTED; flags are recomputed (EC-VAR-04)."""
    row = _load(db, principal, claim_id, MANAGE, lock=True, own=own)
    if row.status != sm.ClaimStatus.SUBMITTED:
        raise conflict("Receipts can only be added while the claim is awaiting approval.", "CLAIM_NOT_SUBMITTED")
    if not data:
        raise bad_request("No file provided.", "RECEIPT_EMPTY")
    if len(data) > app_settings.max_upload_size:
        raise bad_request(f"The file is too large (max {app_settings.max_upload_size // (1024 * 1024)} MB).",
                          "RECEIPT_TOO_LARGE")
    if len(row.receipts) >= MAX_RECEIPTS:
        raise conflict(f"A claim has at most {MAX_RECEIPTS} receipts.", "TOO_MANY_RECEIPTS")
    ext, mime = _sniff(data)
    digest = hashlib.sha256(data).hexdigest()
    if any(r.sha256 == digest for r in row.receipts):
        raise conflict("This receipt is already attached to the claim.", "RECEIPT_ALREADY_ATTACHED")
    fingerprint = tpc.claim_fingerprint(digest, Decimal(row.amount), row.currency, row.expense_date, row.merchant or "")
    path = storage.save_bytes(principal.company_id, RECEIPT_CATEGORY, data, ext)
    receipt = ReimbursementReceipt(
        company_id=principal.company_id, claim_id=row.id, file_name=Path(file_name or f"receipt.{ext}").name[:200],
        storage_path=path, sha256=digest, fingerprint=fingerprint, mime=mime, bytes=len(data),
        uploaded_by_user_id=principal.user_id,
    )
    row.receipts.append(receipt)
    db.flush()
    row.flags = _flags(_evaluate(db, row, _policy_fields(db, row)))
    row.row_version += 1
    db.flush()
    audit(db, principal, AUDIT_MODULE, "CLAIM_RECEIPT_ADDED", f"Added a receipt to claim #{row.id}", SUBJECT, row.id,
          principal.company_id, receipt_id=receipt.id, sha256=digest, bytes=len(data),
          flags=[f["code"] for f in row.flags])
    return receipt


def receipt_file(db: Session, principal: Principal, claim_id: int, receipt_id: int, *, own: bool = False
                 ) -> Tuple[Path, ReimbursementReceipt]:
    row = _load(db, principal, claim_id, READ, own=own)
    receipt = next((r for r in row.receipts if r.id == receipt_id), None)
    if receipt is None:
        raise not_found("Receipt")
    audit(db, principal, AUDIT_MODULE, "CLAIM_RECEIPT_DOWNLOADED", f"Downloaded receipt #{receipt.id} of claim #{row.id}",
          SUBJECT, row.id, principal.company_id, receipt_id=receipt.id)
    return storage.resolve(principal.company_id, receipt.storage_path), receipt


@approvals.on_decided(MODULE, SUBJECT)
def _on_decided(db: Session, principal: Optional[Principal], request) -> None:
    row = (
        db.query(ReimbursementClaim)
        .options(selectinload(ReimbursementClaim.receipts))
        .filter(ReimbursementClaim.company_id == request.company_id, ReimbursementClaim.id == request.subject_id)
        .with_for_update(of=ReimbursementClaim)
        .populate_existing()
        .one()
    )
    if row.status != sm.ClaimStatus.SUBMITTED:
        return
    remarks = next((s.remarks for s in reversed(request.steps) if s.remarks), None)
    now = clock.utcnow()
    if request.status == approvals.APPROVED:
        fields = _policy_fields(db, row)
        if row.currency != row.payout_currency and fields.get("fx_rate_date") == "APPROVAL_DATE":
            conversion = _convert(db, Decimal(row.amount), row.currency, row.payout_currency,
                                  company_today(db, row.company_id))
            conversion.ref_type, conversion.ref_id = SUBJECT, row.id
            row.fx_conversion_id, row.payout_amount = conversion.id, Decimal(conversion.result_amount)
            db.flush()
        check = _evaluate(db, row, fields)
        blocking = [i for i in check.issues if i.severity == tpc.BLOCKING]
        if blocking:
            raise conflict("The claim cannot be approved: " + "; ".join(i.message for i in blocking) + ".",
                           blocking[0].code)
        if check.approvable_amount <= 0:
            raise conflict("The claim type's limits are used up; nothing can be approved.", "CLAIM_LIMIT_EXHAUSTED")
        row.status = transition(sm.CLAIM, row.status, "APPROVE").target
        row.approved_amount = min(Decimal(row.payout_amount), check.approvable_amount)
        row.flags = _flags(check)
        action, text = "CLAIM_APPROVED", "approved"
    else:
        row.status = transition(sm.CLAIM, row.status, "REJECT").target
        row.status_reason = WITHDRAWN if request.status == approvals.CANCELLED else remarks
        action, text = ("CLAIM_WITHDRAWN", "withdrawn") if request.status == approvals.CANCELLED \
            else ("CLAIM_REJECTED", "rejected")
    row.decided_at = now
    row.row_version += 1
    db.flush()
    audit(db, principal, AUDIT_MODULE, action, f"Claim #{row.id} {text}", SUBJECT, row.id, row.company_id,
          approval_request_id=request.id, approved=money_json(row.approved_amount, row.payout_currency),
          capped=row.approved_amount is not None and row.approved_amount < row.payout_amount, remarks=remarks)


def decide_claim(db: Session, principal: Principal, claim_id: int, decision: str, remarks: Optional[str] = None,
                 expected_step: Optional[int] = None) -> ReimbursementClaim:
    row = _load(db, principal, claim_id, APPROVE)
    request = approvals.pending_request(db, MODULE, SUBJECT, row.id)
    if request is None or row.status != sm.ClaimStatus.SUBMITTED:
        raise conflict("This claim is not awaiting approval.", "NOT_PENDING")
    approvals.decide(db, principal, request.id, decision, remarks, expected_step)
    db.refresh(row)
    return row


def withdraw_claim(db: Session, principal: Principal, claim_id: int, remarks: Optional[str] = None
                   ) -> ReimbursementClaim:
    """The requester withdraws a claim awaiting approval (it ends REJECTED, "withdrawn")."""
    row = _load(db, principal, claim_id, MANAGE, own=True)
    request = approvals.pending_request(db, MODULE, SUBJECT, row.id)
    if request is None or row.status != sm.ClaimStatus.SUBMITTED:
        raise conflict("This claim is not awaiting approval.", "NOT_PENDING")
    approvals.cancel(db, principal, request.id, remarks)
    db.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Payroll and payments
# ---------------------------------------------------------------------------
def _payable(row: ReimbursementClaim) -> dict:
    return {
        "id": row.id, "employee_id": row.employee_id, "claim_type": row.claim_type, "taxability": row.taxability,
        "expense_date": row.expense_date.isoformat(),
        "amount": amount_str(Decimal(row.approved_amount), row.payout_currency), "currency": row.payout_currency,
        "claimed": money_json(row.amount, row.currency), "fx_conversion_id": row.fx_conversion_id,
        "payout_route": row.payout_route, "pay_group_id": row.pay_group_id, "legal_entity_id": row.legal_entity_id,
        "version_hash": hashlib.sha256(f"{row.id}|{row.status}|{row.approved_amount}|{row.row_version}".encode())
        .hexdigest(),
    }


def claims_for_payroll(db: Session, company_id: int, employee_id: int, period_start: date, period_end: date
                       ) -> List[dict]:
    """APPROVED WITH_PAYROLL claims of the employee approved on or before the period end, not yet scheduled."""
    rows = (
        db.query(ReimbursementClaim)
        .filter(ReimbursementClaim.company_id == company_id, ReimbursementClaim.employee_id == employee_id,
                ReimbursementClaim.status == sm.ClaimStatus.APPROVED,
                ReimbursementClaim.payout_route == "WITH_PAYROLL",
                ReimbursementClaim.expense_date <= period_end)
        .order_by(ReimbursementClaim.expense_date, ReimbursementClaim.id)
        .all()
    )
    return [_payable(r) for r in rows]


def claims_for_payment(db: Session, company_id: int, *, legal_entity_id: Optional[int] = None) -> List[dict]:
    """APPROVED SEPARATE_PAYMENT claims for a payment batch (payment advice, no payslip)."""
    query = db.query(ReimbursementClaim).filter(
        ReimbursementClaim.company_id == company_id, ReimbursementClaim.status == sm.ClaimStatus.APPROVED,
        ReimbursementClaim.payout_route == "SEPARATE_PAYMENT")
    if legal_entity_id is not None:
        query = query.filter(ReimbursementClaim.legal_entity_id == legal_entity_id)
    return [_payable(r) for r in query.order_by(ReimbursementClaim.id).all()]


def _locked(db: Session, company_id: int, claim_ids: Iterable[int]) -> List[ReimbursementClaim]:
    ids = sorted(set(claim_ids))
    rows = (
        db.query(ReimbursementClaim)
        .filter(ReimbursementClaim.company_id == company_id, ReimbursementClaim.id.in_(ids))
        .order_by(ReimbursementClaim.id)
        .with_for_update()
        .populate_existing()
        .all()
    )
    if len(rows) != len(ids):
        raise not_found("Claim")
    return rows


def schedule_claims(db: Session, company_id: int, claim_ids: Iterable[int], scheduled_ref: str) -> int:
    """APPROVED -> SCHEDULED for a run employee or payment batch (``scheduled_ref``, e.g. "run_employee:42").

    Scheduling again for the same reference is a no-op; another reference is a 409 (paid once).
    """
    if not scheduled_ref:
        raise ValueError("scheduled_ref is required")
    changed = 0
    for row in _locked(db, company_id, claim_ids):
        if row.status == sm.ClaimStatus.SCHEDULED and row.scheduled_ref == scheduled_ref:
            continue
        row.status = transition(sm.CLAIM, row.status, "SCHEDULE").target
        row.scheduled_ref = scheduled_ref[:100]
        row.row_version += 1
        changed += 1
    db.flush()
    return changed


def release_claims(db: Session, company_id: int, scheduled_ref: str) -> int:
    """SCHEDULED -> APPROVED for every claim of a cancelled or reopened run / batch (EC-VAR-01)."""
    rows = (
        db.query(ReimbursementClaim)
        .filter(ReimbursementClaim.company_id == company_id, ReimbursementClaim.scheduled_ref == scheduled_ref,
                ReimbursementClaim.status == sm.ClaimStatus.SCHEDULED)
        .with_for_update()
        .all()
    )
    for row in rows:
        row.status = transition(sm.CLAIM, row.status, "UNSCHEDULE").target
        row.scheduled_ref = None
        row.row_version += 1
    db.flush()
    return len(rows)


def mark_claims_paid(db: Session, company_id: int, claim_ids: Iterable[int], paid_ref: str) -> int:
    """SCHEDULED -> PAID on settlement evidence (``paid_ref`` names it: finalized run, settled instruction)."""
    if not paid_ref:
        raise ValueError("paid_ref (settlement evidence) is required")
    changed = 0
    for row in _locked(db, company_id, claim_ids):
        if row.status == sm.ClaimStatus.PAID:
            continue
        row.status = transition(sm.CLAIM, row.status, "MARK_PAID").target
        row.scheduled_ref, row.paid_ref = None, paid_ref[:100]
        row.row_version += 1
        changed += 1
    db.flush()
    if changed:
        audit(db, None, AUDIT_MODULE, "CLAIM_PAID", f"{changed} claim(s) paid ({paid_ref})", SUBJECT,
              sorted(set(claim_ids))[0], company_id, claims=sorted(set(claim_ids)), paid_ref=paid_ref)
    return changed

"""Statutory attestations (decision I-15, 3.5 §5.8, EC-STA-02).

For a legal entity whose country has no VERIFIED_FOR_PERIOD pack (and no fixture pack in a
non-production environment), payroll calculates without statutory lines and a run may finalize
only while an ACTIVE attestation "statutory obligations are handled outside this system" covers
its period. Maker-checker: one person attests (``payroll:statutory:review``), another approves it
(``payroll:statutory:submit`` or a company administrator); the database also refuses an approver
equal to the attester. Either may revoke it with a reason, which blocks later finalizations
immediately (``active_attestation`` is read live by the finalize gate). Every step is audited.

Services take (db, principal, ...) and never commit.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.org import LegalEntity
from app.models.pay_runs import StatutoryAttestation
from app.payroll import audit as payroll_audit
from app.payroll.services import access
from app.services.audit_log_service import AuditLogService

REVIEW = "payroll:statutory:review"
SUBMIT = "payroll:statutory:submit"
AUDIT_MODULE = payroll_audit.COUNTRY_PACKS
LIVE_STATUSES = ("PENDING", "ACTIVE")

ATTESTATION_CREATED = "STATUTORY_ATTESTATION_CREATED"
ATTESTATION_APPROVED = "STATUTORY_ATTESTATION_APPROVED"
ATTESTATION_REVOKED = "STATUTORY_ATTESTATION_REVOKED"


def _audit(db: Session, principal: Principal, action: str, description: str, row: StatutoryAttestation,
           **details: Any) -> None:
    AuditLogService.log(db, principal, AUDIT_MODULE, action, description[:500], "statutory_attestation", row.id,
                        company_id=row.company_id, actor_type="user", details={
                            "legal_entity_id": row.legal_entity_id, "status": row.status, **details})


def _covers(principal: Principal, permission: str, legal_entity_id: int) -> bool:
    return principal.has(permission) and access.target_scope(principal, permission).covers(legal_entity_id)


def _can_approve(principal: Principal, legal_entity_id: int) -> bool:
    return principal.is_company_admin or _covers(principal, SUBMIT, legal_entity_id)


def _can_read(principal: Principal, legal_entity_id: int) -> bool:
    return _covers(principal, REVIEW, legal_entity_id) or _can_approve(principal, legal_entity_id)


def serialize(row: StatutoryAttestation) -> dict:
    return {
        "id": row.id, "legal_entity_id": row.legal_entity_id, "schemes": list(row.schemes or []),
        "effective_from": row.effective_from.isoformat(),
        "effective_to": row.effective_to.isoformat() if row.effective_to else None, "reason": row.reason,
        "evidence_ref": row.evidence_ref, "status": row.status, "attested_by_user_id": row.attested_by_user_id,
        "approved_by_user_id": row.approved_by_user_id,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "revoked_by_user_id": row.revoked_by_user_id,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None, "revoke_reason": row.revoke_reason,
        "row_version": row.row_version,
        "statement": "Statutory obligations of this legal entity are handled outside this system.",
    }


def _load(db: Session, principal: Principal, attestation_id: int, *, lock: bool = False) -> StatutoryAttestation:
    query = db.query(StatutoryAttestation).filter(StatutoryAttestation.company_id == principal.company_id,
                                                  StatutoryAttestation.id == attestation_id)
    if lock:
        query = query.with_for_update().populate_existing()
    row = query.first()
    if row is None or not _can_read(principal, row.legal_entity_id):
        raise not_found("Statutory attestation")
    return row


def _overlapping(db: Session, company_id: int, legal_entity_id: int, start: date, end: Optional[date],
                 exclude: Optional[int] = None) -> Optional[StatutoryAttestation]:
    query = db.query(StatutoryAttestation).filter(
        StatutoryAttestation.company_id == company_id, StatutoryAttestation.legal_entity_id == legal_entity_id,
        StatutoryAttestation.status.in_(LIVE_STATUSES),
        or_(StatutoryAttestation.effective_to.is_(None), StatutoryAttestation.effective_to >= start))
    if end is not None:
        query = query.filter(StatutoryAttestation.effective_from <= end)
    if exclude is not None:
        query = query.filter(StatutoryAttestation.id != exclude)
    return query.first()


def _clean_schemes(schemes: Iterable[str]) -> List[str]:
    result = sorted({str(s).strip().upper() for s in schemes if str(s).strip()})
    if any(len(s) > 40 for s in result):
        raise bad_request("Scheme codes are at most 40 characters.", "INVALID_SCHEME")
    return result


def create(db: Session, principal: Principal, *, legal_entity_id: int, effective_from: date,
           effective_to: Optional[date], reason: str, schemes: Sequence[str] = (),
           evidence_ref: Optional[str] = None) -> StatutoryAttestation:
    """Attest (maker) that the entity's statutory obligations are handled outside the system; PENDING."""
    if not principal.has(REVIEW):
        raise forbidden()
    entity = db.query(LegalEntity).filter(LegalEntity.company_id == principal.company_id,
                                          LegalEntity.id == legal_entity_id).first()
    if entity is None or not _covers(principal, REVIEW, entity.id):
        raise not_found("Legal entity")
    if effective_to is not None and effective_to < effective_from:
        raise bad_request("effective_to is before effective_from.", "INVALID_RANGE")
    text = (reason or "").strip()
    if not text:
        raise bad_request("Give a reason.", "REASON_REQUIRED")
    db.query(LegalEntity).filter(LegalEntity.id == entity.id).with_for_update().one()  # serialise per entity
    if _overlapping(db, principal.company_id, entity.id, effective_from, effective_to) is not None:
        raise conflict("Another pending or active attestation already covers part of this range.",
                       "ATTESTATION_OVERLAP")
    row = StatutoryAttestation(
        company_id=principal.company_id, legal_entity_id=entity.id, schemes=_clean_schemes(schemes),
        effective_from=effective_from, effective_to=effective_to, reason=text[:2000],
        evidence_ref=(evidence_ref or "").strip()[:300] or None, status="PENDING",
        attested_by_user_id=principal.user_id, row_version=1)
    db.add(row)
    db.flush()
    _audit(db, principal, ATTESTATION_CREATED, f"Statutory attestation for {entity.code} from "
           f"{effective_from.isoformat()} awaits approval", row, effective_from=effective_from.isoformat(),
           effective_to=effective_to.isoformat() if effective_to else None, schemes=row.schemes, reason=row.reason)
    return row


def approve(db: Session, principal: Principal, attestation_id: int, *,
            expected_version: Optional[int] = None) -> StatutoryAttestation:
    """The checker approves: a different user holding payroll:statutory:submit (or a company administrator)."""
    row = _load(db, principal, attestation_id, lock=True)
    if not _can_approve(principal, row.legal_entity_id):
        raise forbidden()
    if row.attested_by_user_id == principal.user_id:
        raise forbidden("The attestation must be approved by someone other than the person who made it.")
    if expected_version is not None and row.row_version != expected_version:
        raise conflict("The attestation was changed by someone else.", "ROW_VERSION_CONFLICT")
    if row.status != "PENDING":
        raise conflict(f"This attestation is {row.status.lower()}.", "ATTESTATION_NOT_PENDING")
    row.status, row.approved_by_user_id, row.approved_at = "ACTIVE", principal.user_id, clock.utcnow()
    row.row_version += 1
    db.flush()
    _audit(db, principal, ATTESTATION_APPROVED, f"Approved statutory attestation #{row.id}", row,
           attested_by_user_id=row.attested_by_user_id)
    return row


def revoke(db: Session, principal: Principal, attestation_id: int, reason: str, *,
           expected_version: Optional[int] = None) -> StatutoryAttestation:
    row = _load(db, principal, attestation_id, lock=True)
    if not (_covers(principal, REVIEW, row.legal_entity_id) or _can_approve(principal, row.legal_entity_id)):
        raise forbidden()
    if expected_version is not None and row.row_version != expected_version:
        raise conflict("The attestation was changed by someone else.", "ROW_VERSION_CONFLICT")
    if row.status == "REVOKED":
        raise conflict("This attestation is already revoked.", "ATTESTATION_REVOKED")
    text = (reason or "").strip()
    if not text:
        raise bad_request("Give a reason.", "REASON_REQUIRED")
    row.status, row.revoked_by_user_id, row.revoked_at = "REVOKED", principal.user_id, clock.utcnow()
    row.revoke_reason = text[:2000]
    row.row_version += 1
    db.flush()
    _audit(db, principal, ATTESTATION_REVOKED, f"Revoked statutory attestation #{row.id}: {text}", row, reason=text)
    return row


def list_attestations(db: Session, principal: Principal, *, legal_entity_id: Optional[int] = None,
                      status: Optional[str] = None, skip: int = 0, limit: int = 100) -> Tuple[int, List[dict]]:
    if not (principal.has(REVIEW) or principal.has(SUBMIT) or principal.is_company_admin):
        raise forbidden()
    query = db.query(StatutoryAttestation).filter(StatutoryAttestation.company_id == principal.company_id)
    if not principal.is_company_admin:
        clauses = [access.run_scope_clause(principal, code, StatutoryAttestation.legal_entity_id)
                   for code in (REVIEW, SUBMIT) if principal.has(code)]
        query = query.filter(or_(*clauses))
    if legal_entity_id is not None:
        query = query.filter(StatutoryAttestation.legal_entity_id == legal_entity_id)
    if status is not None:
        query = query.filter(StatutoryAttestation.status == status)
    total = query.count()
    rows = query.order_by(StatutoryAttestation.id.desc()).offset(skip).limit(limit).all()
    return total, [serialize(r) for r in rows]


def get_attestation(db: Session, principal: Principal, attestation_id: int) -> dict:
    return serialize(_load(db, principal, attestation_id))


def active_attestation(db: Session, legal_entity_id: int, period: Tuple[date, date]) -> Optional[StatutoryAttestation]:
    """The ACTIVE attestation of the entity covering the whole period, if any (the finalize gate, I-15).

    ``db`` is tenant-bound (RLS and the ORM tenant filter restrict rows to the session's company)."""
    start, end = period
    return (db.query(StatutoryAttestation)
            .filter(StatutoryAttestation.legal_entity_id == legal_entity_id, StatutoryAttestation.status == "ACTIVE",
                    StatutoryAttestation.effective_from <= start,
                    or_(StatutoryAttestation.effective_to.is_(None), StatutoryAttestation.effective_to >= end))
            .order_by(StatutoryAttestation.id)
            .first())

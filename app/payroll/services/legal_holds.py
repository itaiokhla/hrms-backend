"""Legal holds and document retention (EC-SEC-16, 3.6 document retention, 09 §6; FOLLOW-UPS F-73).

Retention
---------
Every document gets ``retention_until`` when it is issued: its issue date plus the company's own policy
``privacy.payroll_retention_years`` (company policy, never a statutory claim). The periodic job
``payroll.retention_sweep`` (one per tenant and day, queued with the daily payroll maintenance, and on demand
from the screen) deletes the **stored PDF** of payslips and payroll registers whose retention date has passed
and that are under no active legal hold, and marks the document ``retention_redacted_at``. It never touches the
finalized financial records themselves: the document row (frozen content, hashes, numbers), run lines and
payment evidence stay. A redacted document is not re-rendered or served again (410 ``RETENTION_EXPIRED``).
Documents without a retention date (issued before 0031) are kept. At most ``SWEEP_LIMIT`` files go per sweep;
the job re-queues itself after ten minutes while more are due. Every sweep is audited with counts only.

Legal holds
-----------
Placing and releasing needs ``payroll:legal_hold:manage``; releasing also needs a recent step-up and a second
person (never the one who placed the hold). A hold covers an employee, a legal entity, a document type or one
document (``app.models.legal_holds``); who is covered is defined once, in the database. While a hold is ACTIVE:

* the sweep skips every covered document (counted as ``held``), and the trigger ``documents_retention_guard``
  refuses to redact one even if a service tried (``LEGAL_HOLD``); documents are never deleted by any path
  (``documents_immutable``);
* views, downloads and exports of held documents work as usual;
* an employee erasure request touching held data is refused with ``LEGAL_HOLD`` (409).

Hold creation, the sweep and erasure requests take one advisory lock per tenant, so a hold committed before a
sweep's first statement is always seen by it and a sweep never races a hold into deleting a file.

Services take (db, principal, ...) and never commit (routes and the job runner do; the route of an erasure
request also commits the audit row of a refusal).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sqlalchemy import Boolean, func, text
from sqlalchemy.orm import Session

from app.core import clock, storage
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.audit_log import AuditLog
from app.models.employee import Employee
from app.models.legal_holds import HOLD_SCOPES, LegalHold
from app.models.org import LegalEntity
from app.models.pay_documents import DOC_TYPES, PayDocument
from app.payroll import audit as payroll_audit
from app.payroll.services import jobs
from app.services.audit_log_service import AuditLogService

logger = logging.getLogger(__name__)

MANAGE = "payroll:legal_hold:manage"
AUDIT_READ = payroll_audit.PAYROLL_AUDIT_PERMISSION
AUDIT_MODULE = payroll_audit.LEGAL_HOLDS
SWEEP_TYPES = ("PAYSLIP_V2", "PAYROLL_REGISTER")  # payslip PDFs and registers only (3.6)
SWEEP_JOB = "payroll.retention_sweep"
SWEEP_LIMIT = 500
SWEEP_AT = time(2, 30)  # UTC
CONTINUE_AFTER = timedelta(minutes=10)
DEFAULT_RETENTION_YEARS = 8  # registry default of privacy.payroll_retention_years
_LOCK_CLASS = 7311  # first key of the advisory lock; the company id is the second

HOLD_CREATED = "LEGAL_HOLD_CREATED"
HOLD_RELEASED = "LEGAL_HOLD_RELEASED"
SWEEP_DONE = "RETENTION_SWEEP"
ERASURE_DONE = "RETENTION_ERASURE"
ERASURE_REFUSED = "RETENTION_ERASURE_REFUSED"


# --------------------------------------------------------------------------- helpers


def legal_hold_error(message: str = "This data is under a legal hold and cannot be deleted or anonymised.") -> AppError:
    return AppError(409, message, "LEGAL_HOLD")


def lock_company(db: Session, company_id: int) -> None:
    """Serialise hold creation, sweeps and erasure requests of one tenant (released at commit)."""
    db.execute(text("SELECT pg_advisory_xact_lock(:k, :c)"), {"k": _LOCK_CLASS, "c": company_id})


def _can_manage(principal: Principal) -> bool:
    return principal.has(MANAGE)


def _can_read(principal: Principal) -> bool:
    return principal.has(MANAGE) or principal.has(AUDIT_READ)


def _under_hold():
    """SQL: whether the document is covered by an active hold (the database's single definition)."""
    return func.document_under_legal_hold(PayDocument.company_id, PayDocument.id, type_=Boolean)


def add_years(on: date, years: int) -> date:
    try:
        return on.replace(year=on.year + years)
    except ValueError:  # 29 February
        return on.replace(year=on.year + years, day=28)


def retention_date_for(db: Session, company_id: int, legal_entity_id: int, issue_date: date) -> date:
    """``issue_date`` + the company's retention policy (``privacy.payroll_retention_years``, default 8).

    The setting "cannot be set below the pack minimum (8 if unknown)" (3.1 §5.19). No pack carries a sourced
    minimum yet, so the floor is the documented 8: a lower value is kept as the company's wish but never shortens
    retention below the floor (a stored file is deleted by the sweep, and that cannot be undone)."""
    from app.payroll.services import settings as settings_service

    years = DEFAULT_RETENTION_YEARS
    try:
        value = settings_service.resolve(db, company_id, "privacy.payroll_retention_years",
                                         legal_entity_id=legal_entity_id, on=issue_date).value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            years = max(value, DEFAULT_RETENTION_YEARS)
    except settings_service.SettingConfigError:
        logger.error("privacy.payroll_retention_years could not be resolved for company %s; using %s years",
                     company_id, DEFAULT_RETENTION_YEARS)
    return add_years(issue_date, years)


def _audit(db: Session, principal: Optional[Principal], action: str, description: str, entity_type: str,
           entity_id: Any, company_id: int, **details: Any) -> None:
    AuditLogService.log(db, principal, AUDIT_MODULE, action, description[:500], entity_type, entity_id,
                        company_id=company_id, actor_type="user" if principal else "system", details=details)


# --------------------------------------------------------------------------- holds


def document_ids_under_hold(db: Session, company_id: int, hold_id: int):
    """Query of the ids of the documents one hold covers (active or not)."""
    return db.query(PayDocument.id).filter(
        PayDocument.company_id == company_id,
        func.legal_hold_covers_document(PayDocument.company_id, hold_id, PayDocument.id, type_=Boolean).is_(True))


def serialize(hold: LegalHold, *, covered: Optional[int] = None) -> dict:
    data = {
        "id": hold.id, "scope": hold.scope, "employee_id": hold.employee_id, "legal_entity_id": hold.legal_entity_id,
        "doc_type": hold.doc_type, "document_id": hold.document_id, "reference": hold.reference,
        "reason": hold.reason, "status": hold.status, "created_by_user_id": hold.created_by_user_id,
        "created_at": hold.created_at.isoformat() if hold.created_at else None,
        "released_by_user_id": hold.released_by_user_id,
        "released_at": hold.released_at.isoformat() if hold.released_at else None,
        "release_reason": hold.release_reason, "row_version": hold.row_version,
    }
    if covered is not None:
        data["documents_covered"] = covered
    return data


def _load(db: Session, principal: Principal, hold_id: int, *, lock: bool = False) -> LegalHold:
    query = db.query(LegalHold).filter(LegalHold.company_id == principal.company_id, LegalHold.id == hold_id)
    if lock:
        query = query.with_for_update().populate_existing()
    hold = query.first()
    if hold is None:
        raise not_found("Legal hold")
    return hold


def create(db: Session, principal: Principal, *, scope: str, reason: str, employee_id: Optional[int] = None,
           legal_entity_id: Optional[int] = None, doc_type: Optional[str] = None, document_id: Optional[int] = None,
           reference: Optional[str] = None) -> LegalHold:
    """Place a hold. Exactly the target field of ``scope`` is given; unknown or foreign targets are 404."""
    if not _can_manage(principal):
        raise forbidden()
    if scope not in HOLD_SCOPES:
        raise bad_request("Unknown legal hold scope.", "INVALID_SCOPE")
    given = {"EMPLOYEE": employee_id, "LEGAL_ENTITY": legal_entity_id, "DOCUMENT_TYPE": doc_type,
             "DOCUMENT": document_id}
    if given[scope] is None or any(v is not None for k, v in given.items() if k != scope):
        raise bad_request(f"A {scope.lower().replace('_', ' ')} hold needs exactly its own target.", "INVALID_TARGET")
    text_ = (reason or "").strip()
    if not text_:
        raise bad_request("Give a reason.", "REASON_REQUIRED")
    cid = principal.company_id
    label = ""
    if scope == "EMPLOYEE":
        employee = db.query(Employee).filter(Employee.company_id == cid, Employee.id == employee_id).first()
        if employee is None:
            raise not_found("Employee")
        label = "employee"
    elif scope == "LEGAL_ENTITY":
        entity = db.query(LegalEntity).filter(LegalEntity.company_id == cid, LegalEntity.id == legal_entity_id).first()
        if entity is None:
            raise not_found("Legal entity")
        label = f"legal entity {entity.code}"
    elif scope == "DOCUMENT_TYPE":
        if doc_type not in DOC_TYPES:
            raise bad_request("Unknown document type.", "INVALID_DOC_TYPE")
        label = f"document type {doc_type}"
    else:
        found = db.query(PayDocument.id).filter(PayDocument.company_id == cid, PayDocument.id == document_id).first()
        if found is None:
            raise not_found("Document")
        label = "one document"
    lock_company(db, cid)
    hold = LegalHold(company_id=cid, scope=scope, employee_id=employee_id, legal_entity_id=legal_entity_id,
                     doc_type=doc_type, document_id=document_id, reference=(reference or "").strip()[:120] or None,
                     reason=text_[:2000], status="ACTIVE", created_by_user_id=principal.user_id, row_version=1)
    db.add(hold)
    db.flush()
    covered = document_ids_under_hold(db, cid, hold.id).count()
    _audit(db, principal, HOLD_CREATED, f"Legal hold #{hold.id} placed on {label}", "legal_hold", hold.id, cid,
           scope=scope, employee_id=employee_id, legal_entity_id=legal_entity_id, doc_type=doc_type,
           document_id=document_id, reference=hold.reference, reason=hold.reason, documents_covered=covered)
    return hold


def release(db: Session, principal: Principal, hold_id: int, reason: str, *,
            expected_version: Optional[int] = None) -> LegalHold:
    """Release a hold: a different person than its creator, with a reason (the route requires a recent step-up)."""
    if not _can_manage(principal):
        raise forbidden()
    hold = _load(db, principal, hold_id, lock=True)
    if hold.created_by_user_id == principal.user_id:
        raise AppError(403, "A legal hold must be released by someone other than the person who placed it.",
                       "MAKER_CHECKER_REQUIRED")
    if expected_version is not None and hold.row_version != expected_version:
        raise conflict("The legal hold was changed by someone else.", "ROW_VERSION_CONFLICT")
    if hold.status != "ACTIVE":
        raise conflict("This legal hold is already released.", "LEGAL_HOLD_RELEASED")
    text_ = (reason or "").strip()
    if not text_:
        raise bad_request("Give a reason.", "REASON_REQUIRED")
    covered = document_ids_under_hold(db, principal.company_id, hold.id).count()
    hold.status, hold.released_by_user_id, hold.released_at = "RELEASED", principal.user_id, clock.utcnow()
    hold.release_reason = text_[:2000]
    hold.row_version += 1
    db.flush()
    _audit(db, principal, HOLD_RELEASED, f"Legal hold #{hold.id} released", "legal_hold", hold.id,
           principal.company_id, scope=hold.scope, created_by_user_id=hold.created_by_user_id,
           reason=hold.release_reason, documents_covered=covered)
    return hold


def list_holds(db: Session, principal: Principal, *, status: Optional[str] = None, scope: Optional[str] = None,
               skip: int = 0, limit: int = 50) -> Tuple[int, List[dict]]:
    if not _can_read(principal):
        raise forbidden()
    query = db.query(LegalHold).filter(LegalHold.company_id == principal.company_id)
    if status is not None:
        query = query.filter(LegalHold.status == status)
    if scope is not None:
        query = query.filter(LegalHold.scope == scope)
    total = query.count()
    rows = query.order_by(LegalHold.id.desc()).offset(skip).limit(limit).all()
    return total, [serialize(h, covered=document_ids_under_hold(db, principal.company_id, h.id).count()
                             if h.status == "ACTIVE" else None) for h in rows]


def get_hold(db: Session, principal: Principal, hold_id: int) -> dict:
    if not _can_read(principal):
        raise forbidden()
    hold = _load(db, principal, hold_id)
    covered = document_ids_under_hold(db, principal.company_id, hold.id).count()
    return serialize(hold, covered=covered)


def document_hold_ids(db: Session, company_id: int, document_id: int) -> List[int]:
    """Ids of the ACTIVE holds covering one document (empty: not held)."""
    return [i for (i,) in db.query(LegalHold.id).filter(
        LegalHold.company_id == company_id, LegalHold.status == "ACTIVE",
        func.legal_hold_covers_document(LegalHold.company_id, LegalHold.id, document_id, type_=Boolean).is_(True)
    ).order_by(LegalHold.id)]


def ensure_document_not_held(db: Session, document: PayDocument) -> None:
    """Service guard: raise LEGAL_HOLD when an active hold covers the document (deletion of any kind)."""
    if document_hold_ids(db, document.company_id, document.id):
        raise legal_hold_error("This document is under a legal hold and cannot be deleted.")


def ensure_employee_not_held(db: Session, company_id: int, employee_id: int) -> None:
    """Raise LEGAL_HOLD when an active hold covers the employee or any of their documents.

    Every employee erasure or anonymisation path calls this first."""
    direct = db.query(LegalHold.id).filter(LegalHold.company_id == company_id, LegalHold.status == "ACTIVE",
                                           LegalHold.scope == "EMPLOYEE", LegalHold.employee_id == employee_id).first()
    held_docs = db.query(PayDocument.id).filter(PayDocument.company_id == company_id,
                                                PayDocument.employee_id == employee_id,
                                                _under_hold().is_(True)).first()
    if direct is not None or held_docs is not None:
        raise legal_hold_error("This employee's data is under a legal hold and cannot be deleted or anonymised.")


# --------------------------------------------------------------------------- retention sweep


def _due(db: Session, company_id: int, today: date, employee_id: Optional[int] = None):
    query = db.query(PayDocument).filter(
        PayDocument.company_id == company_id, PayDocument.doc_type.in_(SWEEP_TYPES),
        PayDocument.retention_redacted_at.is_(None), PayDocument.retention_until.isnot(None),
        PayDocument.retention_until < today)
    if employee_id is not None:
        query = query.filter(PayDocument.employee_id == employee_id)
    return query


def _remove_file(company_id: int, path: Optional[str]) -> None:
    """Delete a stored file; a missing file is fine, any other failure (an OSError) raises: the sweep rolls back."""
    if not path:
        return
    try:
        target = storage.resolve(company_id, path)
    except AppError:  # already gone, or a path outside the company folder (never deleted)
        return
    target.unlink(missing_ok=True)


def sweep(db: Session, company_id: int, *, as_of: Optional[date] = None, employee_id: Optional[int] = None,
          limit: int = SWEEP_LIMIT, principal: Optional[Principal] = None, trigger: str = "JOB",
          action: str = SWEEP_DONE) -> Dict[str, Any]:
    """Delete the stored PDFs of due, unheld payslips and registers; counts only (and audited).

    ``due`` documents are past their retention date and not yet redacted; ``held`` of them are skipped.
    A file is removed after its row was updated (the trigger refuses held or not-due documents), inside the
    transaction: a failure rolls everything back and a rolled-back document is re-rendered from its frozen
    content on the next read, so a redaction is never claimed while its file remains.
    Idempotent: a second pass finds nothing left to do. ``more`` is true when ``limit`` cut the pass short.
    """
    lock_company(db, company_id)
    today = as_of or clock.company_today(db, company_id)
    due = _due(db, company_id, today, employee_id)
    due_total = due.count()
    held = due.filter(_under_hold().is_(True)).count()
    batch = (due.filter(_under_hold().is_(False)).order_by(PayDocument.id).limit(limit + 1)
             .with_for_update().all())
    more = len(batch) > limit
    now = clock.utcnow()
    redacted = 0
    for doc in batch[:limit]:
        path = doc.storage_path
        doc.storage_path = None
        doc.retention_redacted_at = now
        doc.row_version += 1
        db.flush()
        _remove_file(company_id, path)
        redacted += 1
    sweepable = db.query(PayDocument).filter(PayDocument.company_id == company_id,
                                             PayDocument.doc_type.in_(SWEEP_TYPES),
                                             PayDocument.retention_redacted_at.is_(None))
    if employee_id is not None:
        sweepable = sweepable.filter(PayDocument.employee_id == employee_id)
    counts = {
        "as_of": today.isoformat(), "trigger": trigger, "due": due_total, "held": held, "redacted": redacted,
        "remaining": due_total - held - redacted,
        "not_due": sweepable.filter(PayDocument.retention_until >= today).count(),
        "no_retention_date": sweepable.filter(PayDocument.retention_until.is_(None)).count(), "more": more,
    }
    _audit(db, principal, action, f"Retention sweep: {redacted} stored file(s) deleted, {held} held back",
           "company", company_id, company_id, **counts)
    return counts


def run_sweep_now(db: Session, principal: Principal) -> Dict[str, Any]:
    """The manual sweep of the screen (the route requires a recent step-up)."""
    if not _can_manage(principal):
        raise forbidden()
    return sweep(db, principal.company_id, principal=principal, trigger="MANUAL")


def erase_employee(db: Session, principal: Principal, employee_id: int, reason: str) -> Dict[str, Any]:
    """An employee's erasure or anonymisation request (the route requires a recent step-up).

    Refused with LEGAL_HOLD while any hold covers the employee or their documents (the refusal is audited).
    Otherwise their payslip PDFs whose retention has ended are deleted; documents still inside their
    retention period are kept until it ends (finalized records are never erased), and the answer says so."""
    if not _can_manage(principal):
        raise forbidden()
    text_ = (reason or "").strip()
    if not text_:
        raise bad_request("Give a reason.", "REASON_REQUIRED")
    cid = principal.company_id
    employee = db.query(Employee).filter(Employee.company_id == cid, Employee.id == employee_id).first()
    if employee is None:
        raise not_found("Employee")
    lock_company(db, cid)
    try:
        ensure_employee_not_held(db, cid, employee_id)
    except AppError:
        _audit(db, principal, ERASURE_REFUSED, "Employee erasure request refused: legal hold", "employee",
               employee_id, cid, employee_id=employee_id, reason=text_[:2000], refused="LEGAL_HOLD")
        raise  # the route commits this audit row (nothing else changed) before it answers 409
    counts = sweep(db, cid, employee_id=employee_id, principal=principal, trigger="ERASURE", action=ERASURE_DONE)
    return {"employee_id": employee_id, "redacted": counts["redacted"], "kept_until_retention_ends":
            counts["not_due"] + counts["no_retention_date"], "more": counts["more"]}


# --------------------------------------------------------------------------- reads for the screen


def summary(db: Session, principal: Principal) -> Dict[str, Any]:
    if not _can_read(principal):
        raise forbidden()
    cid = principal.company_id
    today = clock.company_today(db, cid)
    active = db.query(LegalHold).filter(LegalHold.company_id == cid, LegalHold.status == "ACTIVE").count()
    released = db.query(LegalHold).filter(LegalHold.company_id == cid, LegalHold.status == "RELEASED").count()
    documents = db.query(PayDocument).filter(PayDocument.company_id == cid)
    held_total = documents.filter(_under_hold().is_(True)).count() if active else 0
    due = _due(db, cid, today)
    due_total = due.count()
    due_held = due.filter(_under_hold().is_(True)).count() if active else 0
    last = (db.query(AuditLog).filter(AuditLog.company_id == cid, AuditLog.module == AUDIT_MODULE,
                                      AuditLog.action == SWEEP_DONE).order_by(AuditLog.id.desc()).first())
    return {
        "active_holds": active, "released_holds": released, "documents_under_hold": held_total,
        "documents_due": due_total, "documents_due_held": due_held, "documents_due_deletable": due_total - due_held,
        "documents_redacted": documents.filter(PayDocument.retention_redacted_at.isnot(None)).count(),
        "as_of": today.isoformat(), "sweep_types": list(SWEEP_TYPES), "sweep_limit": SWEEP_LIMIT,
        "last_sweep": _sweep_row(last) if last else None,
    }


def _sweep_row(row: AuditLog) -> dict:
    details = dict(row.details or {})
    return {"at": row.created_at.isoformat() if row.created_at else None, "action": row.action,
            **{k: details.get(k) for k in ("as_of", "trigger", "due", "held", "redacted", "remaining", "not_due",
                                           "no_retention_date", "more")}}


def list_sweeps(db: Session, principal: Principal, *, limit: int = 20) -> List[dict]:
    """Recent sweeps and erasure runs, newest first, from the audit trail (counts only)."""
    if not _can_read(principal):
        raise forbidden()
    rows = (db.query(AuditLog).filter(AuditLog.company_id == principal.company_id, AuditLog.module == AUDIT_MODULE,
                                      AuditLog.action.in_((SWEEP_DONE, ERASURE_DONE)))
            .order_by(AuditLog.id.desc()).limit(limit).all())
    return [_sweep_row(r) for r in rows]


# --------------------------------------------------------------------------- the periodic job


def next_slot(after: datetime) -> datetime:
    """The next 02:30 UTC strictly after ``after``."""
    after = after.astimezone(timezone.utc)
    slot = datetime.combine(after.date(), SWEEP_AT, tzinfo=timezone.utc)
    return slot if slot > after else slot + timedelta(days=1)


def schedule_sweep(db: Session, company_id: int, *, after: Optional[datetime] = None,
                   at: Optional[datetime] = None) -> None:
    """Queue the tenant's next retention sweep (one job per slot, idempotent)."""
    slot = at or next_slot(after or clock.utcnow())
    jobs.enqueue(db, company_id=company_id, type=SWEEP_JOB, payload={"slot": slot.isoformat()},
                 idempotency_key=f"retention-sweep:{slot.strftime('%Y%m%dT%H%M')}", run_at=slot, priority=250)


@jobs.register(SWEEP_JOB)
def _sweep_job(ctx: jobs.JobContext) -> dict:
    counts = sweep(ctx.db, ctx.company_id, trigger="JOB")
    now = clock.utcnow()
    schedule_sweep(ctx.db, ctx.company_id, at=(now + CONTINUE_AFTER) if counts["more"] else next_slot(now))
    return {k: counts[k] for k in ("as_of", "due", "held", "redacted", "remaining", "not_due",
                                   "no_retention_date", "more")}


__all__ = [
    "AUDIT_MODULE", "DEFAULT_RETENTION_YEARS", "MANAGE", "SWEEP_JOB", "SWEEP_LIMIT", "SWEEP_TYPES", "add_years",
    "create", "document_hold_ids", "document_ids_under_hold", "ensure_document_not_held", "ensure_employee_not_held",
    "erase_employee", "get_hold", "legal_hold_error", "list_holds", "list_sweeps", "lock_company", "next_slot",
    "release", "retention_date_for", "run_sweep_now", "schedule_sweep", "serialize", "summary", "sweep",
]

"""Payslip questions (3.6; setting ``payslip.queries_enabled``; table ``payslip_queries``).

* An employee asks about one of their **own published** payslips (``documents.get_own``: another employee's
  or another tenant's payslip, a draft or an unpublished one is 404), only while ``payslip.queries_enabled``
  resolves true for them (409 QUERIES_DISABLED otherwise; an invalid value fails closed with 409
  CONFIG_INVALID). At most ``MAX_OPEN_PER_PAYSLIP`` open questions per payslip (409 TOO_MANY_OPEN_QUESTIONS).
* Employees list, read and close their own questions only (``employee_id`` = the caller's employee; any
  other id is 404). They see the answer and when it was given, never staff user ids.
* Staff list and read questions with ``payroll:payslip:read`` beyond SELF, filtered by the legal entity /
  pay group scope of the payslip (the same scope as the payslips list: out-of-scope ids are 404, counts
  never include them); they answer (optionally closing) with ``payroll:payslip:publish`` in scope. Nobody
  answers a question about their own payslip (403 SELF_ANSWER; the database refuses it too).
* Asking, answering and closing are audited (PAYSLIP_QUESTION_ASKED / _ANSWERED / _CLOSED, without the
  texts); the employee gets an in-app notification when a question is answered (no amounts, no text).
* Optimistic concurrency: ``row_version`` (409 ROW_VERSION_CONFLICT).

Services take (db, principal, ...) and never commit.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, conflict, not_found
from app.core.principal import Principal
from app.models.employee import Employee
from app.models.pay_documents import PayDocument
from app.models.payslip_queries import ANSWER_MAX, QUESTION_MAX, PayslipQuery
from app.payroll import audit as payroll_audit
from app.payroll.services import access, documents
from app.payroll.services import settings as settings_engine
from app.services.audit_log_service import AuditLogService
from app.services.notification_service import NotificationService

SETTING = "payslip.queries_enabled"
READ = documents.READ
ANSWER = documents.PUBLISH
ENTITY = "payslip_query"
MAX_OPEN_PER_PAYSLIP = 5
ASKED = "PAYSLIP_QUESTION_ASKED"
ANSWERED = "PAYSLIP_QUESTION_ANSWERED"
CLOSED = "PAYSLIP_QUESTION_CLOSED"


def _audit(db: Session, principal: Principal, action: str, description: str, row: PayslipQuery,
           **details: Any) -> None:
    AuditLogService.log(db, principal, payroll_audit.PAYSLIPS, action, description[:500], ENTITY, row.id,
                        company_id=row.company_id, details={"document_id": row.document_id,
                                                            "employee_id": row.employee_id, **details})


def _text(value: Optional[str], limit: int, what: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise AppError(422, f"Write the {what}.", f"{what.upper()}_REQUIRED")
    if len(cleaned) > limit:
        raise AppError(422, f"The {what} is too long ({limit} characters at most).", f"{what.upper()}_TOO_LONG")
    return cleaned


def _check_version(row: PayslipQuery, expected_version: Optional[int]) -> None:
    if expected_version is not None and row.row_version != expected_version:
        raise conflict("The question was changed by someone else. Reload it.", "ROW_VERSION_CONFLICT")


def queries_enabled(db: Session, company_id: int, employee_id: int) -> bool:
    """``payslip.queries_enabled`` for the employee today (fails closed: an invalid value is 409 CONFIG_INVALID)."""
    try:
        resolved = settings_engine.resolve(db, company_id, SETTING, employee_id=employee_id,
                                           on=clock.company_today(db, company_id))
    except settings_engine.SettingConfigError as exc:
        raise conflict(str(exc), exc.code)
    return resolved.value is True


# --------------------------------------------------------------------------- serialization


def _document_brief(doc: Optional[PayDocument]) -> Optional[dict]:
    if doc is None:
        return None
    period = (doc.content or {}).get("period") or {}
    return {"id": doc.id, "number": doc.number, "version": doc.version, "period_label": period.get("label"),
            "period_start": doc.period_start.isoformat() if doc.period_start else None,
            "period_end": doc.period_end.isoformat() if doc.period_end else None}


def _iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def query_json(row: PayslipQuery, *, as_staff: bool, doc: Optional[PayDocument] = None,
               employee: Optional[dict] = None) -> dict:
    data = {
        "id": row.id, "document_id": row.document_id, "payslip": _document_brief(doc), "question": row.question,
        "status": row.status, "answer": row.answer, "answered_at": _iso(row.answered_at),
        "closed_at": _iso(row.closed_at), "created_at": _iso(row.created_at), "row_version": row.row_version,
    }
    if as_staff:
        data.update({"employee_id": row.employee_id, "employee": employee, "run_id": row.run_id,
                     "run_employee_id": row.run_employee_id, "legal_entity_id": row.legal_entity_id,
                     "pay_group_id": row.pay_group_id, "asked_by_user_id": row.asked_by_user_id,
                     "answered_by_user_id": row.answered_by_user_id, "closed_by_user_id": row.closed_by_user_id})
    return data


def _documents(db: Session, company_id: int, ids: Iterable[int]) -> dict[int, PayDocument]:
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    return {d.id: d for d in db.query(PayDocument).filter(PayDocument.company_id == company_id,
                                                         PayDocument.id.in_(wanted))}


def _serialize_many(db: Session, company_id: int, rows: list[PayslipQuery], *, as_staff: bool) -> list[dict]:
    docs = _documents(db, company_id, (r.document_id for r in rows))
    names = documents.employee_names(db, company_id, [r.employee_id for r in rows]) if as_staff else {}
    return [query_json(r, as_staff=as_staff, doc=docs.get(r.document_id), employee=names.get(r.employee_id))
            for r in rows]


# --------------------------------------------------------------------------- employee


def _own_employee_id(principal: Principal) -> int:
    if principal.employee_id is None:
        raise not_found("Employee")
    return principal.employee_id


def ask(db: Session, principal: Principal, document_id: int, question: str) -> PayslipQuery:
    """Raise a question about one of the caller's own published payslips."""
    employee_id = _own_employee_id(principal)
    doc = documents.get_own(db, principal, document_id, doc_types=(documents.PAYSLIP,))
    if not queries_enabled(db, principal.company_id, employee_id):
        raise conflict("Payslip questions are switched off. Contact HR directly.", "QUERIES_DISABLED")
    text = _text(question, QUESTION_MAX, "question")
    open_count = db.query(func.count(PayslipQuery.id)).filter(
        PayslipQuery.company_id == principal.company_id, PayslipQuery.document_id == doc.id,
        PayslipQuery.employee_id == employee_id, PayslipQuery.status == "OPEN").scalar() or 0
    if open_count >= MAX_OPEN_PER_PAYSLIP:
        raise conflict(f"This payslip already has {open_count} open questions; wait for an answer first.",
                       "TOO_MANY_OPEN_QUESTIONS")
    row = PayslipQuery(company_id=principal.company_id, document_id=doc.id, run_id=doc.run_id,
                       run_employee_id=doc.run_employee_id, employee_id=employee_id,
                       legal_entity_id=doc.legal_entity_id, pay_group_id=doc.pay_group_id, question=text,
                       status="OPEN", asked_by_user_id=principal.user_id, row_version=1)
    db.add(row)
    db.flush()
    _audit(db, principal, ASKED, f"Question about payslip {doc.number or doc.id} raised", row,
           length=len(text))
    return row


def _load_own(db: Session, principal: Principal, query_id: int, *, lock: bool = False) -> PayslipQuery:
    employee_id = _own_employee_id(principal)
    query = db.query(PayslipQuery).filter(PayslipQuery.company_id == principal.company_id,
                                          PayslipQuery.id == query_id, PayslipQuery.employee_id == employee_id)
    if lock:
        query = query.with_for_update().populate_existing()
    row = query.one_or_none()
    if row is None:
        raise not_found("Question")
    return row


def list_mine(db: Session, principal: Principal, *, status: Optional[str] = None, document_id: Optional[int] = None,
              skip: int = 0, limit: int = 50) -> dict:
    if principal.employee_id is None:
        return {"total": 0, "items": []}
    query = db.query(PayslipQuery).filter(PayslipQuery.company_id == principal.company_id,
                                          PayslipQuery.employee_id == principal.employee_id)
    if status:
        query = query.filter(PayslipQuery.status == status)
    if document_id is not None:
        query = query.filter(PayslipQuery.document_id == document_id)
    total = query.order_by(None).with_entities(func.count(PayslipQuery.id)).scalar() or 0
    rows = query.order_by(PayslipQuery.id.desc()).offset(skip).limit(limit).all()
    return {"total": total, "items": _serialize_many(db, principal.company_id, rows, as_staff=False)}


def get_mine(db: Session, principal: Principal, query_id: int) -> dict:
    row = _load_own(db, principal, query_id)
    return _serialize_many(db, principal.company_id, [row], as_staff=False)[0]


def close_mine(db: Session, principal: Principal, query_id: int, *,
               expected_version: Optional[int] = None) -> PayslipQuery:
    row = _load_own(db, principal, query_id, lock=True)
    _check_version(row, expected_version)
    if row.status == "CLOSED":
        raise conflict("This question is already closed.", "QUESTION_CLOSED")
    _close(row, principal)
    db.flush()
    _audit(db, principal, CLOSED, f"Payslip question #{row.id} closed by the employee", row, by="EMPLOYEE")
    return row


def notification_path(row: PayslipQuery) -> str:
    """The in-app deep link of a question (the employee's My Payslips page opens its detail modal); only ids,
    never text or amounts."""
    return f"/me/payslips?modal=payslip-question&id={row.id}"


def _close(row: PayslipQuery, principal: Principal) -> None:
    row.status, row.closed_at, row.closed_by_user_id = "CLOSED", clock.utcnow(), principal.user_id
    row.row_version += 1


# --------------------------------------------------------------------------- staff


def _staff_clause(principal: Principal, permission: str):
    return access.run_scope_clause(principal, permission, PayslipQuery.legal_entity_id, PayslipQuery.pay_group_id)


def _load_staff(db: Session, principal: Principal, query_id: int, permission: str, *,
                lock: bool = False) -> PayslipQuery:
    documents.require_staff(principal, permission)
    query = db.query(PayslipQuery).filter(PayslipQuery.company_id == principal.company_id,
                                          PayslipQuery.id == query_id, _staff_clause(principal, permission))
    if lock:
        query = query.with_for_update().populate_existing()
    row = query.one_or_none()
    if row is None:
        raise not_found("Question")
    return row


def list_staff(db: Session, principal: Principal, *, status: Optional[str] = None,
               employee_id: Optional[int] = None, run_id: Optional[int] = None, document_id: Optional[int] = None,
               legal_entity_id: Optional[int] = None, skip: int = 0, limit: int = 50) -> dict:
    """Questions about payslips in the caller's payroll:payslip:read scope (beyond SELF), newest first."""
    documents.require_staff(principal, READ)
    query = db.query(PayslipQuery).filter(PayslipQuery.company_id == principal.company_id,
                                          _staff_clause(principal, READ))
    for column, value in ((PayslipQuery.status, status), (PayslipQuery.employee_id, employee_id),
                          (PayslipQuery.run_id, run_id), (PayslipQuery.document_id, document_id),
                          (PayslipQuery.legal_entity_id, legal_entity_id)):
        if value is not None:
            query = query.filter(column == value)
    total = query.order_by(None).with_entities(func.count(PayslipQuery.id)).scalar() or 0
    rows = query.order_by(PayslipQuery.id.desc()).offset(skip).limit(limit).all()
    return {"total": total, "items": _serialize_many(db, principal.company_id, rows, as_staff=True)}


def get_staff(db: Session, principal: Principal, query_id: int) -> dict:
    row = _load_staff(db, principal, query_id, READ)
    return _serialize_many(db, principal.company_id, [row], as_staff=True)[0]


def answer(db: Session, principal: Principal, query_id: int, text: str, *, close: bool = False,
           expected_version: Optional[int] = None) -> PayslipQuery:
    """Answer an OPEN question (payroll:payslip:publish in scope); the employee is notified in-app."""
    row = _load_staff(db, principal, query_id, ANSWER, lock=True)
    if row.employee_id == principal.employee_id or row.asked_by_user_id == principal.user_id:
        raise AppError(403, "You cannot answer a question about your own payslip.", "SELF_ANSWER")
    _check_version(row, expected_version)
    if row.status != "OPEN":
        raise conflict(f"Only an open question can be answered; this one is {row.status}.", "QUESTION_NOT_OPEN")
    row.answer = _text(text, ANSWER_MAX, "answer")
    row.answered_by_user_id, row.answered_at = principal.user_id, clock.utcnow()
    row.status = "ANSWERED"
    row.row_version += 1
    if close:
        row.status, row.closed_at, row.closed_by_user_id = "CLOSED", row.answered_at, principal.user_id
    db.flush()
    _audit(db, principal, ANSWERED, f"Payslip question #{row.id} answered", row, closed=close,
           length=len(row.answer))
    user_id = db.query(Employee.user_id).filter(Employee.company_id == row.company_id,
                                                Employee.id == row.employee_id).scalar()
    NotificationService.create(db, user_id, "Your payslip question was answered",
                               "Open the payslip in the app to read the answer.", "info", notification_path(row))
    db.flush()
    return row


__all__ = [
    "ANSWER", "MAX_OPEN_PER_PAYSLIP", "READ", "SETTING", "answer", "ask", "close_mine", "get_mine", "get_staff",
    "list_mine", "list_staff", "notification_path", "query_json", "queries_enabled",
]

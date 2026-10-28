"""The attendance review queue (04-attendance/01 §11; spine §3.12, §4.10; AS 11 §4.12, §6.12; D-AS-28).

A signal never changes status or pay: it opens at most one OPEN item per ``dedup_key`` (producers call
``open_item``, insert-or-ignore on the partial unique index). Decisions: Acknowledge for informational kinds,
Approve / Reject for the others (note required for Reject), a second decision -> 409 ``INVALID_TRANSITION``, own
records -> 403 ``SELF_EDIT_FORBIDDEN``, out of scope -> 404.

Location kinds are decided with ``location:review``; the others with ``attendance:review`` (registered and granted in
as_m2_sites_consents, SP-AS-169).

Legacy location reviews (``location_samples.review_status``, routes ``/attendance/location-review*``) stay; the two
are mirrors: a sample that becomes PENDING opens an item (mapper listener below), a legacy decision decides the
item, and a queue decision writes the sample's review fields.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable, Mapping, Optional

from sqlalchemy import event, func, or_, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core import authz, clock
from app.core.errors import AppError, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.attendance import (
    INFORMATIONAL_REVIEW_KINDS,
    LOCATION_REVIEW_KINDS,
    REVIEW_ITEM_STATUSES,
    REVIEW_KINDS,
    REVIEW_SEVERITIES,
    AttendanceReviewItem,
    LocationSample,
)
from app.models.employee import Employee
from app.services.audit_log_service import AuditLogService

LOCATION_PERMISSION = "location:review"
REVIEW_PERMISSION = "attendance:review"
DEFAULT_SEVERITY = {
    "LOCATION_EXCEPTION": "WARNING", "MOVEMENT": "INFO", "OUTSIDE_GEOFENCE": "WARNING", "LOW_ACCURACY": "INFO",
    "MOCK_LOCATION": "HIGH", "IMPOSSIBLE_TRAVEL": "HIGH", "IP_NOT_ALLOWED": "INFO", "FACE_MISMATCH": "HIGH",
    "UNMAPPED_PUNCH": "WARNING", "ORPHAN_PUNCH": "WARNING", "DEVICE_CLOCK_SKEW": "WARNING", "DEVICE_OFFLINE": "HIGH",
    "SESSION_OVERLAP_LEGACY": "INFO", "QR_INVALID": "WARNING", "KIOSK_OFF_SITE": "HIGH",
}
SUBJECT_FIELDS = ("employee_id", "shift_date", "punch_id", "session_id", "location_sample_id", "device_id",
                  "device_user_id", "contractor_id")
# Legacy sample review status -> item status.
SAMPLE_TO_ITEM = {"APPROVED": "APPROVED", "REJECTED": "REJECTED", "ACKNOWLEDGED": "ACKNOWLEDGED"}


def permission_for(kind: str) -> str:
    return LOCATION_PERMISSION if kind in LOCATION_REVIEW_KINDS else REVIEW_PERMISSION


def dedup_key(kind: str, subject: Mapping[str, object]) -> str:
    """The 01 §11.2 dedup key of a kind from its subject."""
    if kind in ("OUTSIDE_GEOFENCE", "LOW_ACCURACY", "IP_NOT_ALLOWED"):
        return f"{kind}:{subject['employee_id']}:{subject['shift_date']}"
    if kind == "MOVEMENT":
        return f"{kind}:{subject['location_sample_id']}"
    if kind == "UNMAPPED_PUNCH":
        return f"{kind}:{subject.get('device_id') or 0}:{subject['device_user_id']}"
    if kind in ("DEVICE_CLOCK_SKEW", "DEVICE_OFFLINE", "DEVICE_ZONE_SUSPECT"):
        return f"{kind}:{subject['device_id']}"
    if kind == "KIOSK_OFF_SITE":
        return f"{kind}:{subject['device_id']}:{subject['shift_date']}"
    if subject.get("punch_id") is not None:
        return f"{kind}:{subject['punch_id']}"
    if subject.get("location_sample_id") is not None:
        return f"{kind}:sample:{subject['location_sample_id']}"
    if subject.get("session_id") is not None:
        return f"{kind}:{subject['session_id']}"
    raise ValueError(f"no dedup key for {kind} with {dict(subject)}")


def open_item(db: Session, company_id: int, kind: str, subject: Mapping[str, object], *,
              severity: Optional[str] = None, key: Optional[str] = None) -> Optional[AttendanceReviewItem]:
    """Open one item, or return the OPEN item of the same dedup key (producers are idempotent)."""
    if kind not in REVIEW_KINDS:
        raise ValueError(f"unknown review kind {kind!r}")
    severity = severity or DEFAULT_SEVERITY.get(kind, "WARNING")
    if severity not in REVIEW_SEVERITIES:
        raise ValueError(f"unknown severity {severity!r}")
    key = key or dedup_key(kind, subject)
    values = {k: subject.get(k) for k in SUBJECT_FIELDS if subject.get(k) is not None}
    now = clock.utcnow()
    db.execute(
        insert(AttendanceReviewItem)
        .values(company_id=company_id, kind=kind, severity=severity, status="OPEN", dedup_key=key[:80],
                created_at=now, updated_at=now, row_version=1, **values)
        .on_conflict_do_nothing(index_elements=["company_id", "dedup_key"], index_where=text("status = 'OPEN'"))
    )
    return db.query(AttendanceReviewItem).filter(
        AttendanceReviewItem.company_id == company_id, AttendanceReviewItem.dedup_key == key[:80],
        AttendanceReviewItem.status == "OPEN").first()


# ------------------------------------------------------------------------------------------------- reads
def _allowed_actions(item: AttendanceReviewItem) -> list:
    if item.status != "OPEN":
        return []
    return ["ACKNOWLEDGED"] if item.kind in INFORMATIONAL_REVIEW_KINDS else ["APPROVED", "REJECTED"]


def serialize(item: AttendanceReviewItem, employee: Optional[Employee] = None) -> dict:
    return {
        "id": item.id, "kind": item.kind, "severity": item.severity, "status": item.status,
        "employee_id": item.employee_id, "employee_name": employee.full_name if employee else None,
        "shift_date": item.shift_date.isoformat() if item.shift_date else None,
        "punch_id": item.punch_id, "session_id": item.session_id, "location_sample_id": item.location_sample_id,
        "device_id": item.device_id, "device_user_id": item.device_user_id, "dedup_key": item.dedup_key,
        "decided_by_user_id": item.decided_by_user_id,
        "decided_at": item.decided_at.isoformat() if item.decided_at else None, "note": item.note,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "row_version": item.row_version, "allowed_actions": _allowed_actions(item),
    }


def _visibility(db: Session, principal: Principal):
    """SQL condition of the items the principal may see (kind family x employee scope), or None for nothing."""
    parts = []
    for code, kinds in ((LOCATION_PERMISSION, LOCATION_REVIEW_KINDS),
                        (REVIEW_PERMISSION, tuple(k for k in REVIEW_KINDS if k not in LOCATION_REVIEW_KINDS))):
        scope = authz.employee_scope(db, principal, code)
        if scope.is_empty:
            continue
        employee_clause = scope.clause(AttendanceReviewItem.employee_id)
        if principal.has_company_scope(code):
            employee_clause = or_(employee_clause, AttendanceReviewItem.employee_id.is_(None))
        parts.append(AttendanceReviewItem.kind.in_(kinds) & employee_clause)
    if not parts:
        return None
    clause = or_(*parts)
    if principal.employee_id is not None:
        clause = clause & or_(AttendanceReviewItem.employee_id.is_(None),
                              AttendanceReviewItem.employee_id != principal.employee_id)
    return clause


def list_items(db: Session, principal: Principal, *, kinds: Iterable[str] = (), status: Optional[str] = "OPEN",
               severity: Optional[str] = None, employee_id: Optional[int] = None, device_id: Optional[int] = None,
               start: Optional[date] = None, end: Optional[date] = None, q: Optional[str] = None,
               skip: int = 0, limit: int = 25) -> dict:
    visible = _visibility(db, principal)
    if visible is None:
        raise forbidden()
    kinds = [k for k in kinds if k]
    if any(k not in REVIEW_KINDS for k in kinds) or (status and status not in REVIEW_ITEM_STATUSES) \
            or (severity and severity not in REVIEW_SEVERITIES):
        raise AppError(400, "Unknown review filter value.", "BAD_REQUEST")
    if start and end and end < start:
        raise AppError(400, "The end date is before the start date.", "INVALID_RANGE")
    query = (db.query(AttendanceReviewItem, Employee)
             .outerjoin(Employee, (Employee.id == AttendanceReviewItem.employee_id)
                        & (Employee.company_id == AttendanceReviewItem.company_id))
             .filter(AttendanceReviewItem.company_id == principal.company_id, visible))
    if kinds:
        query = query.filter(AttendanceReviewItem.kind.in_(kinds))
    if status:
        query = query.filter(AttendanceReviewItem.status == status)
    if severity:
        query = query.filter(AttendanceReviewItem.severity == severity)
    if employee_id is not None:
        query = query.filter(AttendanceReviewItem.employee_id == employee_id)
    if device_id is not None:
        query = query.filter(AttendanceReviewItem.device_id == device_id)
    if start:
        query = query.filter(func.coalesce(AttendanceReviewItem.shift_date, func.date(AttendanceReviewItem.created_at))
                             >= start)
    if end:
        query = query.filter(func.coalesce(AttendanceReviewItem.shift_date, func.date(AttendanceReviewItem.created_at))
                             <= end)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Employee.first_name.ilike(like), Employee.last_name.ilike(like),
                                 Employee.employee_code.ilike(like), AttendanceReviewItem.device_user_id.ilike(like)))
    total = query.count()
    rows = query.order_by(AttendanceReviewItem.created_at.desc(), AttendanceReviewItem.id.desc()) \
        .offset(skip).limit(limit).all()
    return {"items": [serialize(i, e) for i, e in rows], "total": total, "skip": skip, "limit": limit}


# ------------------------------------------------------------------------------------------------- decisions
def decide(db: Session, principal: Principal, item_id: int, status: str, note: Optional[str],
           expected_version: Optional[int] = None) -> dict:
    item = db.query(AttendanceReviewItem).filter(AttendanceReviewItem.company_id == principal.company_id,
                                                 AttendanceReviewItem.id == item_id).with_for_update().first()
    if item is None:
        raise not_found("Review item")
    code = permission_for(item.kind)
    if item.employee_id is not None:
        if authz.is_self(principal, item.employee_id):
            raise AppError(403, "You cannot review your own attendance records.", "SELF_EDIT_FORBIDDEN")
        if not authz.can_access_employee(db, principal, code, item.employee_id):
            raise not_found("Review item")
    elif not principal.has_company_scope(code):
        raise not_found("Review item")
    if expected_version is not None and expected_version != item.row_version:
        raise conflict("The item changed since you opened it.", "ROW_VERSION_CONFLICT")
    if item.status != "OPEN":
        raise conflict("This item has already been decided.", "INVALID_TRANSITION")
    if status not in _allowed_actions(item):
        raise AppError(422, f"Choose one of: {', '.join(_allowed_actions(item))}.", "INVALID_DECISION")
    if status == "REJECTED" and not (note or "").strip():
        raise AppError(422, "Add a note to reject.", "NOTE_REQUIRED")
    now = clock.utcnow()
    item.status, item.decided_by_user_id, item.decided_at, item.note = status, principal.user_id, now, note
    item.row_version += 1
    if item.location_sample_id is not None:
        sample = db.get(LocationSample, item.location_sample_id)
        if sample is not None and sample.review_status == "PENDING":
            sample.review_status = status
            sample.reviewed_by_user_id, sample.reviewed_at, sample.review_note = principal.user_id, now, note
    db.flush()
    AuditLogService.log(db, principal, "Attendance", "REVIEW_DECIDED", f"Review item {item.kind} #{item.id}: {status}",
                        "attendance_review_item", item.id, details={"kind": item.kind, "status": status})
    employee = db.get(Employee, item.employee_id) if item.employee_id else None
    return serialize(item, employee)


# ------------------------------------------------------------------------------------------------- legacy mirror
def _sample_subject(target: LocationSample) -> dict:
    kind = "LOCATION_EXCEPTION" if target.exception_reason else "MOVEMENT"
    key = f"LOCATION_EXCEPTION:sample:{target.id}" if kind == "LOCATION_EXCEPTION" else f"MOVEMENT:{target.id}"
    return {"kind": kind, "key": key}


@event.listens_for(LocationSample, "after_insert")
@event.listens_for(LocationSample, "after_update")
def _mirror_sample(mapper, connection, target: LocationSample) -> None:
    """Keep the queue in step with legacy location reviews (same transaction, raw SQL inside the flush)."""
    if target.review_status is None or target.id is None:
        return
    info = _sample_subject(target)
    now = clock.utcnow()
    if target.review_status == "PENDING":
        connection.execute(text(
            "INSERT INTO attendance_review_items (company_id, kind, employee_id, session_id, location_sample_id, "
            "shift_date, severity, status, dedup_key, created_at, updated_at, row_version) "
            "SELECT :c, :k, :e, :s, :sid, (SELECT date FROM attendance WHERE company_id = :c AND id = :s), :sev, "
            "'OPEN', :key, :now, :now, 1 "
            "ON CONFLICT (company_id, dedup_key) WHERE status = 'OPEN' DO NOTHING"),
            {"c": target.company_id, "k": info["kind"], "e": target.employee_id, "s": target.attendance_id,
             "sid": target.id, "sev": DEFAULT_SEVERITY[info["kind"]], "key": info["key"], "now": now})
    elif target.review_status in SAMPLE_TO_ITEM:
        connection.execute(text(
            "UPDATE attendance_review_items SET status = :st, decided_by_user_id = :u, decided_at = :at, note = :n, "
            "row_version = row_version + 1, updated_at = :now "
            "WHERE company_id = :c AND location_sample_id = :sid AND status = 'OPEN'"),
            {"st": SAMPLE_TO_ITEM[target.review_status], "u": target.reviewed_by_user_id,
             "at": target.reviewed_at or now, "n": target.review_note, "now": now, "c": target.company_id,
             "sid": target.id})


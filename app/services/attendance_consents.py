"""Consent and notice ledger and the capture consent gate (AS 07 §5, §12; 04-attendance/01 §4.2 step 4; F-2;
EC AS-CAP-06, AS-CAP-10, AS-SEC-09, AS-SEC-10, AS-ROS-06).

Ledger (``attendance_consents``, append-only, C-1): every change is a new row; the state on a date is the latest row
(``created_at``, ``id``) whose ``valid_from`` is on or before that date — so a withdrawal never rewrites history (C-4)
and the ledger proves which consent applied on any date (C-10). ``record`` writes a row (SELF_SERVICE or HR_RECORDED,
C-3), audits ``CONSENT_RECORDED`` (purpose, status, notice version; no free text, C-8), emits
``attendance.consent.changed`` (ids only; ``roster.revalidate_employee`` subscribes, AS-ROS-06) and applies the
effects of C-5 (a withdrawn ``BIOMETRIC_FACE`` queues the face data for deletion). ``expire_due`` is job
``attendance.consent_expire`` (C-6).

Gate rules:

* ``require_consent`` — 422 ``CONSENT_REQUIRED`` (``details.purpose``) only for PUNCH_PHOTO and LOCATION_AT_PUNCH (no
  non-biometric path); the inline notice of the punch dialog (``meta.location_consent``) is recorded as a
  SELF_SERVICE acknowledgement row, so it is asked once, not per browser.
* ``face_allowed`` — the face step runs only with a GRANTED ``BIOMETRIC_FACE`` valid on the date, or, under a
  LEGACY_V1 capture policy, a legacy enrolment within its grace (``face_encodings.legacy_enrolled``, SP-AS-219,
  07 §5.5); never where the resolved pack prohibits biometrics (B-2, AS-CTY-12; "no pack" until G-14). A missing face
  consent never refuses a punch: the caller skips the step (``verify_mode NONE``, no flag, no review item, F-2).
* ``can_enrol`` — enrolment needs a GRANTED ``BIOMETRIC_FACE`` (07 §5.1: else 422 ``CONSENT_REQUIRED``), except under
  LEGACY_V1 where the enrolment becomes a legacy one with the grace clock (zero diff for v1 tenants, R-AS-104).

Services never commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, bad_request, conflict, not_found
from app.models.attendance import (
    CONSENT_PURPOSES,
    CONSENT_SCOPES,
    LEGAL_BASES,
    AttendanceConsent,
    AttendancePolicyAcknowledgement,
    AttendancePolicyDocument,
)
from app.models.employee import Employee, FaceEncoding
from app.services.audit_log_service import AuditLogService

GATED_PURPOSES = ("PUNCH_PHOTO", "LOCATION_AT_PUNCH")
ROSTER_PURPOSES = ("NIGHT_WORK", "COMPRESSED_WEEK", "REST_WAIVER", "OVERTIME")  # AS-ROS-06 revalidation
SETTABLE_STATUSES = ("GRANTED", "DECLINED", "WITHDRAWN")
CHANGED_TOPIC = "attendance.consent.changed"
INLINE_NOTICE_VERSION = "INLINE-1"
LEGACY_FACE_GRACE_DAYS = 30  # attendance.privacy.legacy_face_grace_days default (SP-AS-212)
# Basis a new row carries when the caller names none: the "no pack" regime defaults (07 §5.1; SP-AS-223).
DEFAULT_BASIS = {
    "BIOMETRIC_FACE": "CONSENT", "BIOMETRIC_DEVICE": "CONSENT", "PUNCH_PHOTO": "EMPLOYMENT_LEGITIMATE_USE",
    "LOCATION_AT_PUNCH": "EMPLOYMENT_LEGITIMATE_USE", "LOCATION_TRACKING": "EMPLOYMENT_LEGITIMATE_USE",
    "NIGHT_WORK": "CONSENT", "OVERTIME": "CONSENT", "COMPRESSED_WEEK": "CONSENT", "REST_WAIVER": "CONSENT",
    "SCHEDULE_CHANGE_ACCEPTED": "CONSENT", "WEEKLY_HOURS_OPT_OUT": "CONSENT",
}
PURPOSE_LABELS = {
    "BIOMETRIC_FACE": "Face match", "BIOMETRIC_DEVICE": "Fingerprint or face on a terminal",
    "PUNCH_PHOTO": "Photo at punch", "LOCATION_AT_PUNCH": "Location when you punch",
    "LOCATION_TRACKING": "Location during your shift", "NIGHT_WORK": "Night work", "OVERTIME": "Overtime",
    "COMPRESSED_WEEK": "Compressed week", "REST_WAIVER": "Shorter rest between shifts",
    "SCHEDULE_CHANGE_ACCEPTED": "Late schedule changes", "WEEKLY_HOURS_OPT_OUT": "Weekly hours opt-out",
}
WITHDRAW_EFFECTS = {
    "BIOMETRIC_FACE": "Face matching stops at once and your face data is deleted. You keep punching without face.",
    "LOCATION_TRACKING": "Location during your shift stops.",
    "NIGHT_WORK": "Night shifts already published stay; your planners are told to review them.",
}


# ------------------------------------------------------------------------------------------------- ledger reads
def _rows(db: Session, company_id: int, employee_id: int, purpose: Optional[str] = None,
          on: Optional[date] = None) -> List[AttendanceConsent]:
    query = db.query(AttendanceConsent).filter(AttendanceConsent.company_id == company_id,
                                               AttendanceConsent.employee_id == employee_id)
    if purpose is not None:
        query = query.filter(AttendanceConsent.purpose == purpose)
    if on is not None:
        query = query.filter(AttendanceConsent.valid_from <= on)
    return query.order_by(AttendanceConsent.created_at.desc(), AttendanceConsent.id.desc()).all()


def state_on(db: Session, company_id: int, employee_id: int, purpose: str, on: date) -> Optional[AttendanceConsent]:
    """The STANDING ledger row in force for ``purpose`` on ``on`` (latest row with ``valid_from <= on``)."""
    for row in _rows(db, company_id, employee_id, purpose, on):
        if row.scope == "STANDING":
            return row
    return None


def latest(db: Session, company_id: int, employee_id: int, purpose: str) -> Optional[AttendanceConsent]:
    for row in _rows(db, company_id, employee_id, purpose):
        if row.scope == "STANDING":
            return row
    return None


def _granted_on(row: Optional[AttendanceConsent], on: date) -> bool:
    return (row is not None and row.status == "GRANTED" and row.valid_from <= on
            and (row.valid_to is None or on <= row.valid_to))


def has_consent(db: Session, company_id: int, employee_id: int, purpose: str, on: date) -> bool:
    """GRANTED consent / recorded acknowledgement of ``purpose`` valid on ``on``."""
    return _granted_on(state_on(db, company_id, employee_id, purpose, on), on)


def grant_intervals(db: Session, company_id: int, employee_ids: Iterable[int], purposes: Sequence[str]) -> List[dict]:
    """Every GRANTED interval of the purposes (one per ledger row, cut by the next row's ``valid_from``), as plain
    dicts for pure engines (``roster_rules.Consent``). PER_INSTANCE rows keep their ``subject_ref``."""
    ids = sorted(set(employee_ids))
    if not ids:
        return []
    rows = (db.query(AttendanceConsent)
            .filter(AttendanceConsent.company_id == company_id, AttendanceConsent.employee_id.in_(ids),
                    AttendanceConsent.purpose.in_(list(purposes)))
            .order_by(AttendanceConsent.created_at, AttendanceConsent.id).all())
    out: List[dict] = []
    groups: Dict[tuple, List[AttendanceConsent]] = {}
    for row in rows:
        key = (row.employee_id, row.purpose, row.scope, str(sorted((row.subject_ref or {}).items())))
        groups.setdefault(key, []).append(row)
    for chain in groups.values():
        for index, row in enumerate(chain):
            if row.status != "GRANTED":
                continue
            end = row.valid_to
            later = [r.valid_from for r in chain[index + 1:]]
            if later:
                cut = min(later) - timedelta(days=1)
                end = cut if end is None else min(end, cut)
            if end is not None and end < row.valid_from:
                continue
            out.append({"employee_id": row.employee_id, "purpose": row.purpose, "valid_from": row.valid_from,
                        "valid_to": end, "scope": row.scope, "subject_ref": row.subject_ref, "consent_id": row.id})
    return out


# ------------------------------------------------------------------------------------------------- notices
def current_document(db: Session, company_id: int, purpose: str, on: date) -> Optional[AttendancePolicyDocument]:
    """The PUBLISHED notice describing ``purpose`` in force on ``on`` (latest effective_from)."""
    rows = (db.query(AttendancePolicyDocument)
            .filter(AttendancePolicyDocument.company_id == company_id, AttendancePolicyDocument.status == "PUBLISHED",
                    AttendancePolicyDocument.effective_from <= on)
            .order_by(AttendancePolicyDocument.effective_from.desc(), AttendancePolicyDocument.id.desc()).all())
    return next((d for d in rows if purpose in (d.purposes or [])), None)


def notice_version_for(db: Session, company_id: int, purpose: str, on: date) -> str:
    doc = current_document(db, company_id, purpose, on)
    return f"{doc.code} v{doc.version}"[:32] if doc is not None else INLINE_NOTICE_VERSION


# ------------------------------------------------------------------------------------------------- writes
def record(db: Session, company_id: int, employee_id: int, purpose: str, status: str, *, channel: str,
           on: date, notice_version: Optional[str] = None, legal_basis: Optional[str] = None,
           valid_to: Optional[date] = None, scope: str = "STANDING", subject_ref: Optional[Mapping[str, Any]] = None,
           notice_period_days: Optional[int] = None, recorded_by_user_id: Optional[int] = None,
           principal=None) -> AttendanceConsent:
    """Append one ledger row (``on`` = the employee-local date it takes effect) and apply its effects (C-5)."""
    if purpose not in CONSENT_PURPOSES:
        raise bad_request("Unknown consent purpose.", "VALIDATION_ERROR")
    if status not in SETTABLE_STATUSES and not (status == "EXPIRED" and principal is None):
        raise bad_request("A consent is granted, declined or withdrawn.", "VALIDATION_ERROR")
    if scope not in CONSENT_SCOPES or (scope == "PER_INSTANCE") != bool(subject_ref):
        raise bad_request("A per-instance consent names its subject; a standing one does not.", "VALIDATION_ERROR")
    basis = legal_basis or DEFAULT_BASIS[purpose]
    if basis not in LEGAL_BASES:
        raise bad_request("Unknown legal basis.", "VALIDATION_ERROR")
    version = (notice_version or notice_version_for(db, company_id, purpose, on)).strip()[:32]
    if not version:
        raise bad_request("Name the notice version.", "VALIDATION_ERROR")
    valid_from = on
    if status == "WITHDRAWN" and purpose == "WEEKLY_HOURS_OPT_OUT":
        granted = latest(db, company_id, employee_id, purpose)
        valid_from = on + timedelta(days=int(getattr(granted, "notice_period_days", None) or 0))  # C-4
    if valid_to is not None and valid_to < valid_from:
        raise bad_request("The end date is before the start date.", "INVALID_RANGE")
    row = AttendanceConsent(
        company_id=company_id, employee_id=employee_id, purpose=purpose, scope=scope,
        subject_ref=dict(subject_ref) if subject_ref else None, legal_basis=basis, notice_version=version,
        status=status, valid_from=valid_from, valid_to=valid_to if status == "GRANTED" else None,
        notice_period_days=notice_period_days, channel=channel, recorded_by_user_id=recorded_by_user_id)
    db.add(row)
    db.flush()
    _effects(db, row)
    AuditLogService.log(db, principal, "Attendance", "CONSENT_RECORDED",
                        f"Consent {purpose} {status.lower()}", "employee", employee_id, company_id=company_id,
                        actor_type="user" if principal is not None else "system",
                        details={"purpose": purpose, "status": status, "notice_version": version,
                                 "channel": channel, "consent_id": row.id})
    from app.payroll.services import outbox

    outbox.emit(db, CHANGED_TOPIC, {"employee_id": employee_id, "purpose": purpose, "consent_id": row.id,
                                    "status": status})
    return row


def _effects(db: Session, row: AttendanceConsent) -> None:
    if row.purpose == "LOCATION_TRACKING" and row.status in ("DECLINED", "WITHDRAWN") and row.scope == "STANDING":
        from app.services import location_service

        # C-5: location during the shift stops at once (the session stays open).
        location_service.stop_for_employee(db, row.company_id, row.employee_id, "POLICY_DISABLED")
        return
    if row.purpose != "BIOMETRIC_FACE":
        return
    faces = db.query(FaceEncoding).filter(FaceEncoding.company_id == row.company_id,
                                          FaceEncoding.employee_id == row.employee_id,
                                          FaceEncoding.deleted_at.is_(None))
    if row.status == "GRANTED":
        faces.update({FaceEncoding.consent_id: row.id, FaceEncoding.retention_until: None},
                     synchronize_session=False)
    elif row.status == "WITHDRAWN":
        from app.services import attendance_faces

        attendance_faces.queue_deletion(db, row.company_id, row.employee_id, row.valid_from)


def require_consent(db: Session, company_id: int, employee_id: int, purpose: str, on: date, *,
                    acknowledged: bool = False) -> None:
    """422 ``CONSENT_REQUIRED`` (``details.purpose``) for PUNCH_PHOTO / LOCATION_AT_PUNCH without consent; never
    for any other purpose (a missing face consent skips the face step instead, F-2). An inline acknowledgement is
    recorded as a SELF_SERVICE row (notice of the purpose's current document)."""
    if purpose not in GATED_PURPOSES:
        return
    if has_consent(db, company_id, employee_id, purpose, on):
        return
    if acknowledged:
        record(db, company_id, employee_id, purpose, "GRANTED", channel="SELF_SERVICE", on=on)
        return
    doc = current_document(db, company_id, purpose, on)
    raise AppError(422, "Please read and acknowledge the notice before punching.", "CONSENT_REQUIRED",
                   extra={"details": {"purpose": purpose, "document_id": doc.id if doc is not None else None,
                                      "notice_version": notice_version_for(db, company_id, purpose, on)}})


def _legal_entity_on(db: Session, company_id: int, employee_id: int, on: date) -> Optional[int]:
    from app.models.org import EmployeeAssignment

    row = (db.query(EmployeeAssignment.legal_entity_id)
           .filter(EmployeeAssignment.company_id == company_id, EmployeeAssignment.employee_id == employee_id,
                   EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.effective_from <= on,
                   or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= on))
           .order_by(EmployeeAssignment.effective_from.desc()).first())
    return row[0] if row else None


def legacy_face_within_grace(db: Session, company_id: int, employee_id: int, on: date) -> bool:
    return db.query(FaceEncoding.id).filter(
        FaceEncoding.company_id == company_id, FaceEncoding.employee_id == employee_id,
        FaceEncoding.deleted_at.is_(None), FaceEncoding.legacy_enrolled.is_(True),
        FaceEncoding.legacy_grace_until >= on).first() is not None


def face_allowed(db: Session, company_id: int, employee_id: int, on: date, *, profile: Optional[str] = None) -> bool:
    """Whether the face step may run for this employee on ``on`` (F-2, B-2, 07 §5.5). ``profile`` = the capture
    policy profile when the caller already has it (kiosk 1:N); else it is resolved for the employee."""
    from app.services import pack_resolver

    if pack_resolver.biometrics_prohibited(db, company_id, _legal_entity_on(db, company_id, employee_id, on), on):
        return False
    state = state_on(db, company_id, employee_id, "BIOMETRIC_FACE", on)
    if _granted_on(state, on):
        return True
    if state is not None and state.status in ("WITHDRAWN", "DECLINED", "EXPIRED"):
        return False  # an explicit answer ends the legacy grace (C-4, C-7)
    if profile is None:
        from app.services import attendance_capture_policy

        profile = attendance_capture_policy.effective(db, company_id, employee_id, on).profile
    return profile == "LEGACY_V1" and legacy_face_within_grace(db, company_id, employee_id, on)


def can_enrol(db: Session, company_id: int, employee_id: int, on: date) -> Optional[str]:
    """The way an enrolment may proceed: ``"CONSENT"`` (GRANTED face consent), ``"LEGACY"`` (LEGACY_V1 capture policy:
    a legacy enrolment with the grace clock), or None (refuse with 422 CONSENT_REQUIRED, 07 §5.1, T-09)."""
    from app.services import attendance_capture_policy, pack_resolver

    if pack_resolver.biometrics_prohibited(db, company_id, _legal_entity_on(db, company_id, employee_id, on), on):
        return None
    if has_consent(db, company_id, employee_id, "BIOMETRIC_FACE", on):
        return "CONSENT"
    state = state_on(db, company_id, employee_id, "BIOMETRIC_FACE", on)
    if state is not None and state.status in ("WITHDRAWN", "DECLINED", "EXPIRED"):
        return None
    if attendance_capture_policy.effective(db, company_id, employee_id, on).profile == "LEGACY_V1":
        return "LEGACY"
    return None


def expire_due(db: Session, company_id: int, today: date) -> int:
    """Job ``attendance.consent_expire`` (C-6): a GRANTED STANDING row past its ``valid_to`` with no later row gets an
    EXPIRED row from the day after ``valid_to``. Idempotent per row."""
    candidates = (db.query(AttendanceConsent)
                  .filter(AttendanceConsent.company_id == company_id, AttendanceConsent.status == "GRANTED",
                          AttendanceConsent.scope == "STANDING", AttendanceConsent.valid_to.isnot(None),
                          AttendanceConsent.valid_to < today)
                  .order_by(AttendanceConsent.id).all())
    expired = 0
    for row in candidates:
        newest = latest(db, company_id, row.employee_id, row.purpose)
        if newest is None or newest.id != row.id:
            continue
        record(db, company_id, row.employee_id, row.purpose, "EXPIRED", channel=row.channel,
               on=row.valid_to + timedelta(days=1), notice_version=row.notice_version, legal_basis=row.legal_basis,
               recorded_by_user_id=row.recorded_by_user_id)
        expired += 1
    return expired


# ------------------------------------------------------------------------------------------------- serializers
def serialize(row: AttendanceConsent) -> dict:
    return {"id": row.id, "employee_id": row.employee_id, "purpose": row.purpose,
            "purpose_label": PURPOSE_LABELS.get(row.purpose, row.purpose), "scope": row.scope,
            "subject_ref": row.subject_ref, "legal_basis": row.legal_basis, "notice_version": row.notice_version,
            "status": row.status, "valid_from": row.valid_from.isoformat(),
            "valid_to": row.valid_to.isoformat() if row.valid_to else None,
            "notice_period_days": row.notice_period_days, "channel": row.channel,
            "recorded_by_user_id": row.recorded_by_user_id,
            "recorded_at": row.created_at.isoformat() if row.created_at else None}


def serialize_document(doc: AttendancePolicyDocument, acknowledged_at=None) -> dict:
    return {"id": doc.id, "code": doc.code, "version": doc.version, "title": doc.title,
            "legal_entity_id": doc.legal_entity_id, "effective_from": doc.effective_from.isoformat(),
            "requires_acknowledgement": doc.requires_acknowledgement, "purposes": list(doc.purposes or []),
            "summary": doc.summary, "privacy_contact": doc.privacy_contact, "status": doc.status,
            "published_at": doc.published_at.isoformat() if doc.published_at else None,
            "document_attachment_id": doc.document_attachment_id, "row_version": doc.row_version,
            "acknowledged_at": acknowledged_at.isoformat() if acknowledged_at else None}


# ------------------------------------------------------------------------------------------------- policy documents
DOCUMENT_FIELDS = ("title", "legal_entity_id", "effective_from", "requires_acknowledgement", "purposes", "summary",
                   "privacy_contact", "document_attachment_id")


def _document(db: Session, company_id: int, document_id: int, *, lock: bool = False) -> AttendancePolicyDocument:
    query = db.query(AttendancePolicyDocument).filter(AttendancePolicyDocument.company_id == company_id,
                                                      AttendancePolicyDocument.id == document_id)
    if lock:
        query = query.with_for_update()
    doc = query.first()
    if doc is None:
        raise not_found("Policy document")
    return doc


def _check_purposes(purposes: Iterable[str]) -> List[str]:
    values = sorted(set(purposes or ()))
    unknown = [p for p in values if p not in CONSENT_PURPOSES]
    if unknown:
        raise bad_request(f"Unknown purposes: {', '.join(unknown)}.", "VALIDATION_ERROR")
    return values


def create_document(db: Session, principal, data: Mapping[str, Any]) -> dict:
    cid = principal.company_id
    if db.query(AttendancePolicyDocument.id).filter(AttendancePolicyDocument.company_id == cid,
                                                    AttendancePolicyDocument.code == data["code"],
                                                    AttendancePolicyDocument.version == data["version"]).first():
        raise conflict("This document version already exists.", "CONFLICT")
    doc = AttendancePolicyDocument(company_id=cid, code=data["code"], version=data["version"], title=data["title"],
                                   legal_entity_id=data.get("legal_entity_id"),
                                   effective_from=data["effective_from"],
                                   requires_acknowledgement=bool(data.get("requires_acknowledgement")),
                                   purposes=_check_purposes(data.get("purposes") or ()),
                                   summary=data.get("summary"), privacy_contact=data.get("privacy_contact"),
                                   document_attachment_id=data.get("document_attachment_id"), status="DRAFT")
    db.add(doc)
    db.flush()
    AuditLogService.log(db, principal, "Attendance", "POLICY_DOCUMENT_SAVED",
                        f"Policy document {doc.code} {doc.version} created", "attendance_policy_document", doc.id)
    return serialize_document(doc)


def update_document(db: Session, principal, document_id: int, data: Mapping[str, Any],
                    expected_row_version: Optional[int]) -> dict:
    """Edit a DRAFT (any field) or change the status: DRAFT -> PUBLISHED -> RETIRED (a published version is never
    edited; publish a new version instead)."""
    doc = _document(db, principal.company_id, document_id, lock=True)
    if expected_row_version is not None and expected_row_version != doc.row_version:
        raise conflict("This document changed meanwhile. Reload and try again.", "STALE_VERSION")
    target = data.get("status")
    edits = {k: v for k, v in data.items() if k in DOCUMENT_FIELDS}
    if edits and doc.status != "DRAFT":
        raise conflict("A published document is not edited; publish a new version.", "INVALID_TRANSITION")
    for key, value in edits.items():
        setattr(doc, key, _check_purposes(value) if key == "purposes" else value)
    if target and target != doc.status:
        allowed = {("DRAFT", "PUBLISHED"), ("PUBLISHED", "RETIRED")}
        if (doc.status, target) not in allowed:
            raise conflict("This status change is not allowed.", "INVALID_TRANSITION")
        doc.status = target
        if target == "PUBLISHED":
            doc.published_at = clock.utcnow()
    doc.row_version += 1
    db.flush()
    AuditLogService.log(db, principal, "Attendance", "POLICY_DOCUMENT_SAVED",
                        f"Policy document {doc.code} {doc.version} saved ({doc.status.lower()})",
                        "attendance_policy_document", doc.id)
    return serialize_document(doc)


def list_documents(db: Session, company_id: int, *, status: Optional[str] = None) -> List[dict]:
    query = db.query(AttendancePolicyDocument).filter(AttendancePolicyDocument.company_id == company_id)
    if status:
        query = query.filter(AttendancePolicyDocument.status == status)
    return [serialize_document(d) for d in query.order_by(AttendancePolicyDocument.code,
                                                           AttendancePolicyDocument.effective_from.desc(),
                                                           AttendancePolicyDocument.id.desc())]


def acknowledge(db: Session, principal, employee: Employee, document_id: int, on: date) -> dict:
    """SELF acknowledgement of a PUBLISHED document (409 CONFLICT when already acknowledged). A document describing
    a gated purpose also records that purpose's acknowledgement in the ledger (its notice version)."""
    doc = _document(db, principal.company_id, document_id)
    if doc.status != "PUBLISHED":
        raise not_found("Policy document")
    if db.query(AttendancePolicyAcknowledgement.id).filter(
            AttendancePolicyAcknowledgement.company_id == principal.company_id,
            AttendancePolicyAcknowledgement.document_id == doc.id,
            AttendancePolicyAcknowledgement.employee_id == employee.id).first():
        raise conflict("You have already acknowledged this document.", "CONFLICT")
    ack = AttendancePolicyAcknowledgement(company_id=principal.company_id, document_id=doc.id, employee_id=employee.id,
                                          acknowledged_at=clock.utcnow(), channel="SELF_SERVICE")
    db.add(ack)
    db.flush()
    for purpose in doc.purposes or []:
        if purpose in GATED_PURPOSES + ("LOCATION_TRACKING",) and not has_consent(
                db, principal.company_id, employee.id, purpose, on):
            record(db, principal.company_id, employee.id, purpose, "GRANTED", channel="SELF_SERVICE", on=on,
                   notice_version=f"{doc.code} v{doc.version}", principal=principal)
    return {"id": ack.id, "document_id": doc.id, "employee_id": employee.id,
            "acknowledged_at": ack.acknowledged_at.isoformat(), "channel": ack.channel}


@dataclass(frozen=True)
class LedgerView:
    current: Dict[str, Optional[AttendanceConsent]]
    history: List[AttendanceConsent]


def ledger(db: Session, company_id: int, employee_id: int, on: date) -> LedgerView:
    rows = _rows(db, company_id, employee_id)
    current = {p: state_on(db, company_id, employee_id, p, on) for p in CONSENT_PURPOSES}
    return LedgerView(current=current, history=rows)


# Imported here so the subscription of roster.revalidate_employee exists wherever a consent change is emitted.
from app.services import roster_consents  # noqa: E402,F401

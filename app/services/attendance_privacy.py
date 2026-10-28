"""The transparency panel and the evidence reveal (AS 07 §8, §11, §12; SR-AS-09, SR-AS-14; EC AS-RBAC-08).

* ``panel(db, principal)`` — ``GET /me/attendance/privacy`` for the signed-in employee on their local today: what is
  collected now (from the effective capture policy), why and on what basis (per purpose, notice version), how long
  (retention per class), who can see what (plain words), the consent ledger (state + history, Grant / Withdraw), the
  notices with acknowledgements, who viewed my evidence (``EVIDENCE_VIEWED`` and Location ``VIEW`` rows about me, last
  12 months, no content) and the privacy contact. Never another employee's data.
* ``reveal(db, principal, punch_id)`` — E3 evidence of one punch (numeric face score, photo path presence) for a holder
  of ``attendance:evidence:read`` whose scope covers the punch's employee (else 404: the code never widens reach),
  with a step-up of at most 5 minutes (403 ``STEP_UP_REQUIRED``); writes ``EVIDENCE_VIEWED`` after success only
  (subject, punch id; never content).
"""

from __future__ import annotations

from datetime import timedelta
from typing import List

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.core import authz, clock
from app.core.errors import forbidden, not_found
from app.core.principal import Principal
from app.models.attendance import CONSENT_PURPOSES, Attendance, AttendancePolicyAcknowledgement, AttendancePunch
from app.models.audit_log import AuditLog
from app.models.user import User
from app.services import attendance_capture_policy, attendance_consents, attendance_policy, org_structure_service
from app.services.audit_log_service import AuditLogService

EVIDENCE = "attendance:evidence:read"
PHOTO_RETENTION_DAYS = 30  # attendance.privacy.photo_retention_days default (registry key in AS-M3)
FACE_RETENTION_DAYS_AFTER_EXIT = 30  # attendance.privacy.face_retention_days_after_exit default
TIME_RECORD_YEARS = 8
VIEWERS = (
    {"who": "You", "sees": "Everything recorded about you, including the exact place of your punches."},
    {"who": "Your line manager", "sees": "Whether you were at your site and your times, not the exact place."},
    {"who": "HR admins with location access", "sees": "The exact place of your punches; each view is recorded."},
    {"who": "Reviewers with evidence access", "sees": "Punch photos and face-match scores, one punch at a time "
                                                      "after confirming their identity; each view is recorded."},
)


def _needed(policy, purpose: str) -> bool:
    return {
        "BIOMETRIC_FACE": policy.uses_face, "PUNCH_PHOTO": policy.punch_photo,
        "LOCATION_AT_PUNCH": policy.location_mode != "OFF" or policy.location_on_breaks != "OFF",
        "LOCATION_TRACKING": policy.tracking_enabled,
    }.get(purpose, False)


def panel(db: Session, principal: Principal) -> dict:
    employee = authz.own_employee(db, principal)
    cid = principal.company_id
    today = org_structure_service.employee_local_today(db, employee)
    policy = attendance_capture_policy.effective(db, cid, employee.id, today)
    settings = attendance_policy.load(db, cid)
    view = attendance_consents.ledger(db, cid, employee.id, today)
    face_allowed = attendance_consents.face_allowed(db, cid, employee.id, today, profile=policy.profile)
    consents = []
    for purpose in CONSENT_PURPOSES:
        row = view.current.get(purpose)
        status = row.status if row is not None else None
        consents.append({
            "purpose": purpose, "label": attendance_consents.PURPOSE_LABELS[purpose],
            "needed": _needed(policy, purpose), "status": status,
            "legal_basis": row.legal_basis if row is not None else attendance_consents.DEFAULT_BASIS[purpose],
            "notice_version": row.notice_version if row is not None
            else attendance_consents.notice_version_for(db, cid, purpose, today),
            "valid_from": row.valid_from.isoformat() if row is not None else None,
            "valid_to": row.valid_to.isoformat() if row is not None and row.valid_to else None,
            "can_grant": status != "GRANTED", "can_withdraw": status == "GRANTED",
            "withdraw_effect": attendance_consents.WITHDRAW_EFFECTS.get(purpose),
        })
    acks = {a.document_id: a.acknowledged_at for a in db.query(AttendancePolicyAcknowledgement).filter(
        AttendancePolicyAcknowledgement.company_id == cid, AttendancePolicyAcknowledgement.employee_id == employee.id)}
    documents = [d | {"acknowledged_at": acks[d["id"]].isoformat() if d["id"] in acks else None}
                 for d in attendance_consents.list_documents(db, cid, status="PUBLISHED")]
    contact = next((d["privacy_contact"] for d in documents if d.get("privacy_contact")), None)
    return {
        "employee_id": employee.id, "as_of": today.isoformat(),
        "collection": {
            "sources": sorted(policy.allowed_sources), "location_at_punch": policy.location_mode,
            "location_on_breaks": policy.location_on_breaks, "tracking": policy.tracking_enabled,
            "face_match": {"on": policy.face_on_punch or policy.face_on_break,
                           "applies_to_me": (policy.face_on_punch or policy.face_on_break) and face_allowed,
                           "note": None if face_allowed else "You punch without face matching."},
            "punch_photo": policy.punch_photo, "kiosk_methods": sorted(policy.kiosk_methods),
            "site_checks": {"geofence": policy.geofence_mode, "network": policy.ip_mode, "qr": policy.qr_mode},
            "note": "Location is recorded only when you punch." if not policy.tracking_enabled else
                    "Location is recorded while you are checked in, never outside work.",
        },
        "retention": [
            {"class": "LOCATION", "label": f"Location: {settings.retention_days} days", "days": settings.retention_days},
            {"class": "IMAGE", "label": f"Punch photos: {PHOTO_RETENTION_DAYS} days", "days": PHOTO_RETENTION_DAYS},
            {"class": "BIOMETRIC", "label": "Face data: until you withdraw or "
                                            f"{FACE_RETENTION_DAYS_AFTER_EXIT} days after you leave",
             "days_after_exit": FACE_RETENTION_DAYS_AFTER_EXIT},
            {"class": "TIME_RECORD", "label": f"Times and sessions: {TIME_RECORD_YEARS} years",
             "years": TIME_RECORD_YEARS},
            {"class": "CONSENT", "label": "Consents and notices: as long as the time records they cover"},
        ],
        "viewers": list(VIEWERS),
        "consents": consents,
        "history": [attendance_consents.serialize(r) for r in view.history],
        "documents": documents,
        "evidence_views": evidence_views(db, principal, employee.id),
        "contact": contact,
    }


def evidence_views(db: Session, principal: Principal, employee_id: int) -> List[dict]:
    """Who viewed my evidence or exact location in the last 12 months (viewer, date, what; never content)."""
    since = clock.utcnow() - timedelta(days=365)
    my_sessions = db.query(Attendance.id).filter(Attendance.company_id == principal.company_id,
                                                 Attendance.employee_id == employee_id)
    rows = (db.query(AuditLog)
            .filter(AuditLog.company_id == principal.company_id, AuditLog.created_at >= since,
                    or_(and_(AuditLog.action == "EVIDENCE_VIEWED", AuditLog.entity_type == "employee",
                             AuditLog.entity_id == str(employee_id)),
                        and_(AuditLog.module == "Location", AuditLog.action == "VIEW",
                             AuditLog.entity_type == "attendance",
                             AuditLog.entity_id.in_(my_sessions.with_entities(
                                 Attendance.id.cast(AuditLog.entity_id.type))))))
            .order_by(AuditLog.created_at.desc()).limit(200).all())
    users = {u.id: u for u in db.query(User).filter(User.company_id == principal.company_id,
                                                    User.id.in_({r.actor_id for r in rows if r.actor_id} or {0}))}
    out = []
    for r in rows:
        viewer = users.get(r.actor_id)
        out.append({"viewer": getattr(viewer, "full_name", None) or r.actor_email or "Someone",
                    "at": r.created_at.isoformat(),
                    "what": "Punch evidence" if r.action == "EVIDENCE_VIEWED" else "Exact location"})
    return out


def reveal(db: Session, principal: Principal, punch_id: int) -> dict:
    """E3 reveal of one punch (07 §8, T-15, AS-RBAC-08): permission (403), scope (404), step-up (403), then audit."""
    from app.payroll.services import step_up

    if not principal.has(EVIDENCE):
        raise forbidden()
    punch = db.query(AttendancePunch).filter(AttendancePunch.company_id == principal.company_id,
                                             AttendancePunch.id == punch_id).first()
    if punch is None or punch.employee_id is None or \
            not authz.can_access_employee(db, principal, EVIDENCE, punch.employee_id):
        raise not_found("Punch")
    if authz.is_self(principal, punch.employee_id):
        raise not_found("Punch")  # own evidence lives in the transparency panel, never a score (SP-AS-141)
    step_up.ensure_step_up(db, principal)
    AuditLogService.log(db, principal, "Attendance", "EVIDENCE_VIEWED", "Viewed punch evidence", "employee",
                        punch.employee_id, details={"punch_ids": [punch.id]})
    return {"punch_id": punch.id, "employee_id": punch.employee_id, "verify_mode": punch.verify_mode,
            "face_match_score": punch.face_match_score, "photo_available": punch.photo_storage_path is not None,
            "photo_redacted": punch.redacted_at is not None, "evidence_flags": list(punch.evidence_flags or [])}

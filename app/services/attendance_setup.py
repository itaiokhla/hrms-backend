"""The ``attendance.v2_prepare`` steps that ship with AS-M2 (spine §6.3, §15; 13 §4.5 note C-2; 07 §5.5).

* ``v2_prepare_capture`` — one catch-all ``ATTENDANCE_CAPTURE_POLICY`` (everyone, priority 0) with an ACTIVE
  **LEGACY_V1** version mapping today's company attendance settings (face, location, GPS, tracking, breaks, maximum
  session length; spine §6.3). Resolution then never fails, and the effective policy of a LEGACY_V1 version keeps
  reading the live legacy settings (``attendance_capture_policy._finish``), so the tenant sees no change. Idempotent:
  a tenant that already has a capture policy is left alone.
* ``v2_prepare_faces`` — the face step: every plaintext encoding is encrypted (``ATTENDANCE_BIOMETRIC`` key, BUG-AS-19)
  and every employee with an encoding and no consent row is marked ``legacy_enrolled`` with the grace end of
  ``attendance.privacy.legacy_face_grace_days`` (SP-AS-219; no consent row is invented).

``v2_prepare(db, company_id)`` runs both; the session must be bound to the tenant. Services never commit.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.core import clock
from app.models.attendance import AttendanceConsent
from app.models.employee import FaceEncoding
from app.models.payroll_config import Policy
from app.payroll import policy_types as pt
from app.payroll.services import policies
from app.services import attendance_capture_policy, attendance_consents, attendance_faces, attendance_policy

CAPTURE = "ATTENDANCE_CAPTURE_POLICY"
LEGACY_NAME = "Capture (current settings)"
LEGACY_FROM = date(2000, 1, 1)  # the LEGACY_V1 version covers every past date too (zero diff)


def _today(db: Session, company_id: int) -> date:
    return clock.company_today(db, company_id)


def v2_prepare_capture(db: Session, company_id: int) -> Optional[int]:
    """Create the LEGACY_V1 capture policy of the tenant; returns its id (None when one exists already)."""
    if db.query(Policy.id).filter(Policy.company_id == company_id, Policy.type == CAPTURE).first() is not None:
        return None
    fields = pt.validate_fields(CAPTURE, attendance_capture_policy.legacy_fields(attendance_policy.load(db, company_id)))
    starter = pt.StarterPolicy(type=CAPTURE, name=LEGACY_NAME, fields=fields, status="ACTIVE",
                               policy_hash=pt.policy_hash(fields),
                               note="LEGACY_V1: follows the company attendance settings until a STANDARD version is "
                                    "activated.")
    policy = policies.create_starter_policy(db, company_id, starter, effective_from=LEGACY_FROM)
    return policy.id


def v2_prepare_faces(db: Session, company_id: int, today: Optional[date] = None) -> dict:
    today = today or _today(db, company_id)
    encrypted = attendance_faces.encrypt_legacy_rows(db, company_id)
    consented = {e for (e,) in db.query(AttendanceConsent.employee_id).filter(
        AttendanceConsent.company_id == company_id, AttendanceConsent.purpose == "BIOMETRIC_FACE")}
    grace = today + timedelta(days=attendance_consents.LEGACY_FACE_GRACE_DAYS)
    marked = 0
    for row in db.query(FaceEncoding).filter(FaceEncoding.company_id == company_id, FaceEncoding.deleted_at.is_(None),
                                             FaceEncoding.legacy_enrolled.is_(False)):
        if row.employee_id in consented:
            continue
        row.legacy_enrolled, row.legacy_grace_until = True, grace
        marked += 1
    db.flush()
    return {"encrypted": encrypted, "legacy_marked": marked}


def v2_prepare(db: Session, company_id: int) -> dict:
    policy_id = v2_prepare_capture(db, company_id)
    return {"capture_policy_id": policy_id, **v2_prepare_faces(db, company_id)}

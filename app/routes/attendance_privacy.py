"""Privacy, consents, policy documents and evidence (AS 11 §6.15; AS 07 §5, §8, §12; 04-attendance/01 §4.1).

Self service (SELF; the ``/api/v1/me`` prefix is never reachable by a delegated partner):
``GET /me/attendance/privacy`` (transparency panel) · ``GET /me/attendance/capture`` (the effective capture policy for
the punch screen) · ``POST /me/attendance/consents`` (grant / decline / withdraw) ·
``POST /me/attendance/policy-documents/{document_id:int}/acknowledge``.

HR (``attendance:manage_policy``): ``GET|POST /attendance/consents`` (HR-recorded rows; scope covers the employee) ·
``GET|POST /attendance/policy-documents`` · ``PATCH /attendance/policy-documents/{document_id:int}`` (edit a draft,
publish, retire). Evidence: ``POST /attendance/punches/{punch_id:int}/evidence`` (``attendance:evidence:read`` +
step-up; audited). Roster: ``GET /attendance/roster-consent-conflicts`` (``roster:plan``; EC AS-ROS-06 pending items).
"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core import authz
from app.core.errors import forbidden
from app.core.principal import Principal, get_principal, require
from app.database.db import get_db
from app.models.attendance import AttendanceConsent
from app.models.rosters import RosterConsentConflict
from app.payroll.services import idempotency
from app.schemas.attendance_capture import HrConsentIn, MyConsentIn, PolicyDocumentIn, PolicyDocumentPatch
from app.services import (
    attendance_capture_policy,
    attendance_consents,
    attendance_privacy,
    attendance_sites,
    org_structure_service,
    roster_consents,
)

router = APIRouter(prefix="/api/v1", tags=["Attendance privacy"])
MANAGE_POLICY = "attendance:manage_policy"


def _if_match(request: Request) -> Optional[int]:
    raw = (request.headers.get("If-Match") or "").strip().strip('"')
    return int(raw) if raw.isdigit() else None


# ------------------------------------------------------------------------------------------------- self service
@router.get("/me/attendance/privacy")
def my_privacy(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return attendance_privacy.panel(db, principal)


@router.get("/me/attendance/capture")
def my_capture(principal: Principal = Depends(require("attendance:mark")), db: Session = Depends(get_db)):
    employee = authz.own_employee(db, principal)
    today = org_structure_service.employee_local_today(db, employee)
    policy = attendance_capture_policy.effective(db, principal.company_id, employee.id, today)
    sites = attendance_sites.sites_for(db, principal.company_id, employee.id, policy, today) \
        if policy.geofence_mode != "OFF" or policy.ip_mode != "OFF" or policy.qr_mode != "OFF" else []
    face_ok = attendance_consents.face_allowed(db, principal.company_id, employee.id, today, profile=policy.profile)
    needed = [p for p in attendance_consents.GATED_PURPOSES
              if ((p == "LOCATION_AT_PUNCH" and (policy.location_mode != "OFF" or policy.location_on_breaks != "OFF"))
                  or (p == "PUNCH_PHOTO" and policy.punch_photo))
              and not attendance_consents.has_consent(db, principal.company_id, employee.id, p, today)]
    return {**attendance_capture_policy.client_view(policy), "date": today.isoformat(),
            "sites": [{"id": s.id, "name": s.name, "kind": s.kind} for s in sites],
            "face_step": "OFF" if not (policy.face_on_punch or policy.face_on_break)
            else ("REQUIRED" if face_ok else "SKIPPED"),
            "consents_needed": [{"purpose": p, "notice_version": attendance_consents.notice_version_for(
                db, principal.company_id, p, today)} for p in needed]}


@router.post("/me/attendance/consents", status_code=201)
def my_consent(data: MyConsentIn, request: Request, principal: Principal = Depends(get_principal),
               db: Session = Depends(get_db)):
    employee = authz.own_employee(db, principal)
    today = org_structure_service.employee_local_today(db, employee)

    def handler():
        row = attendance_consents.record(
            db, principal.company_id, employee.id, data.purpose, data.status, channel="SELF_SERVICE", on=today,
            notice_version=data.notice_version, scope="PER_INSTANCE" if data.subject_ref else "STANDING",
            subject_ref=data.subject_ref, principal=principal)
        return 201, attendance_consents.serialize(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.post("/me/attendance/policy-documents/{document_id:int}/acknowledge", status_code=201)
def acknowledge(document_id: int, request: Request, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    employee = authz.own_employee(db, principal)
    today = org_structure_service.employee_local_today(db, employee)
    return idempotency.execute(
        db, principal, request,
        lambda: (201, attendance_consents.acknowledge(db, principal, employee, document_id, today)), body={})


# ------------------------------------------------------------------------------------------------- HR
@router.get("/attendance/consents")
def list_consents(employee_id: Optional[int] = None, purpose: Optional[str] = Query(None, max_length=28),
                  skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                  principal: Principal = Depends(require(MANAGE_POLICY)), db: Session = Depends(get_db)):
    scope = authz.employee_scope(db, principal, MANAGE_POLICY)
    query = db.query(AttendanceConsent).filter(AttendanceConsent.company_id == principal.company_id,
                                               scope.clause(AttendanceConsent.employee_id))
    if employee_id is not None:
        query = query.filter(AttendanceConsent.employee_id == employee_id)
    if purpose:
        query = query.filter(AttendanceConsent.purpose == purpose)
    total = query.count()
    rows = query.order_by(AttendanceConsent.created_at.desc(), AttendanceConsent.id.desc()).offset(skip).limit(limit)
    return {"items": [attendance_consents.serialize(r) for r in rows], "total": total, "skip": skip, "limit": limit}


@router.post("/attendance/consents", status_code=201)
def record_consent(data: HrConsentIn, request: Request, principal: Principal = Depends(require(MANAGE_POLICY)),
                   db: Session = Depends(get_db)):
    employee = authz.ensure_employee_access(db, principal, MANAGE_POLICY, data.employee_id)
    on = data.valid_from or org_structure_service.employee_local_today(db, employee)

    def handler():
        row = attendance_consents.record(
            db, principal.company_id, employee.id, data.purpose, data.status, channel="HR_RECORDED", on=on,
            notice_version=data.notice_version, legal_basis=data.legal_basis, valid_to=data.valid_to,
            scope="PER_INSTANCE" if data.subject_ref else "STANDING", subject_ref=data.subject_ref,
            notice_period_days=data.notice_period_days, recorded_by_user_id=principal.user_id, principal=principal)
        return 201, attendance_consents.serialize(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/attendance/policy-documents")
def list_documents(status: Optional[str] = Query(None, max_length=10),
                   principal: Principal = Depends(require(MANAGE_POLICY)), db: Session = Depends(get_db)):
    return {"items": attendance_consents.list_documents(db, principal.company_id, status=status)}


def _company_writer(principal: Principal) -> None:
    if not principal.has_company_scope(MANAGE_POLICY):
        raise forbidden("Policy documents are company-wide: this needs company-wide access.")


@router.post("/attendance/policy-documents", status_code=201)
def create_document(data: PolicyDocumentIn, request: Request, principal: Principal = Depends(require(MANAGE_POLICY)),
                    db: Session = Depends(get_db)):
    _company_writer(principal)
    return idempotency.execute(db, principal, request,
                               lambda: (201, attendance_consents.create_document(db, principal, data.model_dump())),
                               body=data.model_dump(mode="json"))


@router.patch("/attendance/policy-documents/{document_id:int}")
def update_document(document_id: int, data: PolicyDocumentPatch, request: Request,
                    principal: Principal = Depends(require(MANAGE_POLICY)), db: Session = Depends(get_db)):
    _company_writer(principal)
    version = _if_match(request)
    values = data.model_dump(exclude_unset=True)
    return idempotency.execute(
        db, principal, request,
        lambda: (200, attendance_consents.update_document(db, principal, document_id, values, version)),
        body=data.model_dump(mode="json", exclude_unset=True))


# ------------------------------------------------------------------------------------------------- evidence (E3)
@router.post("/attendance/punches/{punch_id:int}/evidence")
def reveal_evidence(punch_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    result = attendance_privacy.reveal(db, principal, punch_id)
    db.commit()
    return result


# ------------------------------------------------------------------------------------------------- roster (AS-ROS-06)
@router.get("/attendance/roster-consent-conflicts")
def roster_consent_conflicts(period_id: Optional[int] = None, status: Optional[str] = Query("OPEN", max_length=10),
                             start: Optional[date] = Query(None, alias="from"),
                             principal: Principal = Depends(require("roster:plan")), db: Session = Depends(get_db)):
    scope = authz.employee_scope(db, principal, "roster:plan")
    query = db.query(RosterConsentConflict).filter(RosterConsentConflict.company_id == principal.company_id,
                                                   scope.clause(RosterConsentConflict.employee_id))
    if period_id is not None:
        query = query.filter(RosterConsentConflict.roster_period_id == period_id)
    if status:
        query = query.filter(RosterConsentConflict.status == status)
    if start is not None:
        query = query.filter(RosterConsentConflict.shift_date >= start)
    rows = query.order_by(RosterConsentConflict.shift_date, RosterConsentConflict.id).limit(500).all()
    return {"items": [roster_consents.serialize_conflict(r) for r in rows], "total": len(rows)}

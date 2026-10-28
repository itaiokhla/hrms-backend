"""Attendance policies on the shared policy framework (AS 11 §6.14; spine §6.2, §6.2.5; JC-AS-22, A-11) — AS-M2
ships ``ATTENDANCE_CAPTURE_POLICY`` (rail item 2, ``/settings/attendance/capture``; 13 note C-2).

``GET|POST /attendance/policies`` · ``GET /attendance/policies/resolve`` (literal before ``{policy_id}``) ·
``GET /attendance/policies/{policy_id:int}`` · ``POST …/versions`` · ``PUT …/versions/{version_no:int}`` ·
``POST …/versions/{version_no:int}/activate`` · ``GET|PUT …/assignments`` · ``POST …/archive`` ·
``POST /attendance/setup/prepare-capture`` (the AS-M2 steps of ``attendance.v2_prepare``).

Gate ``attendance:manage_policy`` (writes at COMPANY, enforced by the service through the type's
``write_permission``); resolve: ``attendance:read`` covering the employee, or ``attendance:manage_policy``. The routes
call ``app.payroll.services.policies`` with attendance types only. A version saved here is always ``profile =
STANDARD`` (LEGACY_V1 versions come only from v2_prepare). V-AS-02 does not apply to this type (SP-AS-221): a v1
tenant activates STANDARD capture versions; the punch path clamps sessions / sources while not V2_ACTIVE. Activation
refuses face options where the resolved pack prohibits biometrics and a punch photo where the pack forbids it (B-2,
SP-AS-024; "no pack" until G-14) and warns that employees without a face consent punch without face (T-09).
"""

from datetime import date
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core import authz, clock
from app.core.errors import AppError, bad_request, forbidden
from app.core.principal import Principal, require
from app.database.db import get_db
from app.models.org import LegalEntity
from app.models.payroll_config import Policy, PolicyVersion
from app.payroll import policy_types as pt
from app.payroll.routes.policies import ActivateIn, AssignmentsIn, PolicyIn, VersionIn, _dump
from app.payroll.services import idempotency
from app.payroll.services import policies as service
from app.services import attendance_access, attendance_policies, attendance_setup, pack_resolver

router = APIRouter(prefix="/api/v1", tags=["Attendance policies"])
MANAGE = "attendance:manage_policy"
WRITERS = require(MANAGE)
FACE_WARNING = "Employees without a face consent punch without face matching (no flag, no review)."


def _attendance_type(code: str) -> str:
    ptype = pt.POLICY_TYPES.get(code)
    if ptype is None or ptype.module != "attendance":
        raise bad_request(f"{code!r} is not an attendance policy type.", "UNKNOWN_POLICY_TYPE")
    return code


def _own_policy(db: Session, principal: Principal, policy_id: int) -> Policy:
    policy = service._load(db, principal.company_id, policy_id)
    _attendance_type(policy.type)
    return policy


def _standard(fields: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return None if fields is None else {**fields, "profile": "STANDARD"}


@router.get("/attendance/policies")
def list_policies(type: Optional[str] = Query(None, max_length=40), include_archived: bool = False,
                  principal: Principal = Depends(WRITERS), db: Session = Depends(get_db)):
    if type is not None:
        _attendance_type(type)
    return service.list_policies(db, principal, type_code=type, include_archived=include_archived, module="attendance")


@router.post("/attendance/policies", status_code=201)
def create_policy(data: PolicyIn, request: Request, principal: Principal = Depends(WRITERS),
                  db: Session = Depends(get_db)):
    _attendance_type(data.type)
    return idempotency.execute(db, principal, request, lambda: (201, service.create_policy(
        db, principal, type_code=data.type, name=data.name, description=data.description,
        fields=_standard(data.fields or {}), effective_from=data.effective_from)), body=_dump(data))


@router.get("/attendance/policies/resolve")
def resolve(type: str = Query(..., max_length=40), employee_id: int = Query(..., ge=1),
            on: Optional[date] = Query(None, alias="date"),
            principal: Principal = Depends(require("attendance:read")), db: Session = Depends(get_db)):
    _attendance_type(type)
    employee = authz.ensure_employee_access(db, principal, "attendance:read", employee_id)
    from app.services import org_structure_service

    day = on or org_structure_service.employee_local_today(db, employee)
    found = attendance_policies.explain(db, principal.company_id, type, employee.id, day)
    if type == attendance_policies.CAPTURE and found.get("status") != "RESOLVED":
        found = found | {"fallback": "LEGACY_V1"}
    return found


@router.get("/attendance/policies/{policy_id:int}")
def get_policy(policy_id: int, principal: Principal = Depends(WRITERS), db: Session = Depends(get_db)):
    _own_policy(db, principal, policy_id)
    return service.get_policy(db, principal, policy_id)


@router.post("/attendance/policies/{policy_id:int}/versions", status_code=201)
def create_version(policy_id: int, request: Request, data: Optional[VersionIn] = Body(None),
                   principal: Principal = Depends(WRITERS), db: Session = Depends(get_db)):
    body = data or VersionIn()
    _own_policy(db, principal, policy_id)
    fields = body.fields
    if fields is None:  # a copy of the latest version, saved as STANDARD
        latest = (db.query(PolicyVersion).filter(PolicyVersion.company_id == principal.company_id,
                                                 PolicyVersion.policy_id == policy_id)
                  .order_by(PolicyVersion.version.desc()).first())
        fields = dict(latest.fields) if latest is not None else {}
    return idempotency.execute(db, principal, request, lambda: (201, service.create_version(
        db, principal, policy_id, fields=_standard(fields), effective_from=body.effective_from)), body=_dump(data))


@router.put("/attendance/policies/{policy_id:int}/versions/{version_no:int}")
def update_version(policy_id: int, version_no: int, data: VersionIn, request: Request,
                   principal: Principal = Depends(WRITERS), db: Session = Depends(get_db)):
    _own_policy(db, principal, policy_id)
    return idempotency.execute(db, principal, request, lambda: (200, service.update_version(
        db, principal, policy_id, version_no, fields=_standard(data.fields), effective_from=data.effective_from)),
        body=_dump(data))


def _activation_checks(db: Session, principal: Principal, fields: Dict[str, Any], on: date) -> list:
    """B-2 / F-1 / SP-AS-024 against every legal entity of the tenant (no pack: nothing refused)."""
    face = bool(fields.get("face_on_punch") or fields.get("face_on_break") or "FACE" in (fields.get("kiosk_methods") or []))
    entities = [e for (e,) in db.query(LegalEntity.id).filter(LegalEntity.company_id == principal.company_id)]
    for entity in entities or [None]:
        if face and pack_resolver.biometrics_prohibited(db, principal.company_id, entity, on):
            raise AppError(400, {"message": "Face matching is prohibited for a covered legal entity.",
                                 "field": "face_on_punch", "details": {"legal_entity_id": entity}}, "INVALID_FIELDS")
        if fields.get("punch_photo") and not pack_resolver.punch_photo_allowed(db, principal.company_id, entity, on):
            raise AppError(400, {"message": "Punch photos are not allowed for a covered legal entity.",
                                 "field": "punch_photo", "details": {"legal_entity_id": entity}}, "INVALID_FIELDS")
    return [FACE_WARNING] if face else []


@router.post("/attendance/policies/{policy_id:int}/versions/{version_no:int}/activate")
def activate_version(policy_id: int, version_no: int, request: Request, data: Optional[ActivateIn] = Body(None),
                     principal: Principal = Depends(WRITERS), db: Session = Depends(get_db)):
    body = data or ActivateIn()
    policy = _own_policy(db, principal, policy_id)
    row = service._version(db, principal.company_id, policy_id, version_no)
    # V-AS-02: a STANDARD version of every attendance type except capture needs attendance v2 (SP-AS-221).
    if (policy.type != attendance_policies.CAPTURE and (row.fields or {}).get("profile", "STANDARD") == "STANDARD"
            and not attendance_access.v2_enabled(db, principal.company_id)):
        raise AppError(409, "Standard attendance rules need attendance v2: run the attendance cut-over first.",
                       "ATTENDANCE_V2_REQUIRED")
    start = body.effective_from or row.effective_from or clock.company_today(db, principal.company_id)
    warnings = _activation_checks(db, principal, dict(row.fields or {}), start)

    def handler():
        result = service.activate_version(db, principal, policy_id, version_no, effective_from=body.effective_from,
                                          reason=body.reason)
        return 200, result | {"warnings": warnings}

    return idempotency.execute(db, principal, request, handler, body=_dump(data))


@router.get("/attendance/policies/{policy_id:int}/assignments")
def get_assignments(policy_id: int, principal: Principal = Depends(WRITERS), db: Session = Depends(get_db)):
    _own_policy(db, principal, policy_id)
    return service.get_assignments(db, principal, policy_id)


@router.put("/attendance/policies/{policy_id:int}/assignments")
def put_assignments(policy_id: int, data: AssignmentsIn, request: Request, principal: Principal = Depends(WRITERS),
                    db: Session = Depends(get_db)):
    _own_policy(db, principal, policy_id)
    rules = [service.RuleInput(r.criteria, r.priority, r.effective_from, r.effective_to) for r in data.rules]
    return idempotency.execute(db, principal, request,
                               lambda: (200, service.put_assignments(db, principal, policy_id, rules)), body=_dump(data))


@router.post("/attendance/policies/{policy_id:int}/archive")
def archive(policy_id: int, request: Request, principal: Principal = Depends(WRITERS), db: Session = Depends(get_db)):
    _own_policy(db, principal, policy_id)
    return idempotency.execute(db, principal, request, lambda: (200, service.archive_policy(db, principal, policy_id)))


@router.post("/attendance/setup/prepare-capture")
def prepare_capture(request: Request, principal: Principal = Depends(WRITERS), db: Session = Depends(get_db)):
    """The AS-M2 steps of ``attendance.v2_prepare``: LEGACY_V1 capture policy + the face step (idempotent)."""
    if not principal.has_company_scope(MANAGE):
        raise forbidden("This needs company-wide attendance configuration access.")
    return idempotency.execute(db, principal, request,
                               lambda: (200, attendance_setup.v2_prepare(db, principal.company_id)), body={})

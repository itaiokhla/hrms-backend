"""Rotation templates, patterns, versions and assignments (AS-M5; AS 11 §6.4; 03-shifts/02 §2-§3).

Read ``roster:read``; patterns ``shift:manage``; assignments ``shift:assign``; draft generation ``roster:plan``.
Literal ``/rotations/templates`` before ``/rotations/{pattern_id:int}``. Assignments live at the canonical
``/rotation-assignments`` (AS 11 §6.4); ``/rotations/assignments`` stays as an alias on the same handlers.
"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import Field
from sqlalchemy import false, or_, true
from sqlalchemy.orm import Session

from app.core import concurrency
from app.core.authz import employee_scope
from app.core.errors import forbidden
from app.core.permission_registry import Scope
from app.core.principal import Principal, require, require_any
from app.database.db import get_db
from app.models.employee import Employee
from app.models.jobs import Job
from app.models.rosters import RotationAssignment, RotationPattern, RotationPatternVersion
from app.payroll.services import idempotency
from app.schemas.auth import StrictModel
from app.schemas.rosters import GenerateAheadIn, GenerateIn, PatternIn, PatternVersionIn, RotationAssignmentIn
from app.services import shift_rotations as service

router = APIRouter(prefix="/api/v1", tags=["Rotations"])

READ = require_any("roster:read", "shift:read")
MANAGE = require("shift:manage")
ASSIGN = require("shift:assign")
PLAN = require("roster:plan")

AUTO_PUBLISH_JOB = "roster.rotation_generate"
JOB_TERMINAL = ("SUCCEEDED", "FAILED", "DEAD", "CANCELLED")
ASSIGNMENT_READS = ("roster:read", "shift:read", "shift:assign")


def _beyond_self(principal: Principal) -> bool:
    """The Rotations page predicate (09-ui-ux/01 A16 "same as Shifts"): ``shift:manage`` ∨ ``shift:assign`` ∨
    ``roster:plan`` ∨ a rotation read beyond SELF. An employee's SELF read never sees company-wide runs."""
    if principal.has("shift:manage") or principal.has("shift:assign") or principal.has("roster:plan"):
        return True
    return any(principal.scopes(code) - {Scope.SELF.value} for code in ("roster:read", "shift:read"))


@router.get("/rotations/templates")
def templates(principal: Principal = Depends(READ)):
    return service.templates()


@router.get("/rotations/auto-publish/last")
def last_auto_publish(principal: Principal = Depends(READ), db: Session = Depends(get_db)):
    """The tenant's latest ``roster.rotation_generate`` run (03-shifts/02 §3.5, EC AS-REL-06: "2 of 17 chunks
    failed") or ``null``. The jobs table has no row-level security (app/models/jobs.py), so the company filter is
    explicit, as for the readiness page's ``last_acceptance``."""
    if not _beyond_self(principal):
        raise forbidden()
    job = (db.query(Job).filter(Job.company_id == principal.company_id, Job.type == AUTO_PUBLISH_JOB)
           .order_by(Job.id.desc()).first())
    if job is None:
        return None
    result = job.result if isinstance(job.result, dict) else None
    summary = None
    if result is not None:
        summary = {"publications": len(result.get("publications") or []), "entries": result.get("entries"),
                   "blocked_employees": len(result.get("blocked") or {}), "chunks": result.get("chunks"),
                   "failed_chunks": result.get("failed_chunks")}
    return {"job_id": job.id, "status": job.status, "local_date": (job.payload or {}).get("local_date"),
            "progress_done": job.progress_done, "progress_total": job.progress_total,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "finished_at": job.updated_at.isoformat() if job.status in JOB_TERMINAL and job.updated_at else None,
            "summary": summary, "error": job.error if job.status in ("FAILED", "DEAD") else None}


@router.get("/rotation-assignments")
@router.get("/rotations/assignments")  # alias of the canonical path (the first rotation UI and its tests)
def list_assignments(pattern_id: Optional[int] = Query(None, ge=1), skip: int = Query(0, ge=0),
                     limit: int = Query(100, ge=1, le=500), principal: Principal = Depends(READ),
                     db: Session = Depends(get_db)):
    """Rotation assignments (03-shifts/02 §3.3; list key ``shift.rotation_assignments``), newest start first, limited
    to the employees inside any of the viewer's ``roster:read`` / ``shift:read`` / ``shift:assign`` scopes."""
    scopes = [employee_scope(db, principal, code) for code in ASSIGNMENT_READS if principal.has(code)]
    visible = true() if any(s.all_company for s in scopes) else or_(
        *[s.clause(RotationAssignment.employee_id) for s in scopes if not s.is_empty], false())
    query = db.query(RotationAssignment, RotationPatternVersion, RotationPattern, Employee).join(
        RotationPatternVersion, (RotationPatternVersion.company_id == RotationAssignment.company_id)
        & (RotationPatternVersion.id == RotationAssignment.pattern_version_id)).join(
        RotationPattern, (RotationPattern.company_id == RotationPatternVersion.company_id)
        & (RotationPattern.id == RotationPatternVersion.pattern_id)).outerjoin(
        Employee, (Employee.company_id == RotationAssignment.company_id)
        & (Employee.id == RotationAssignment.employee_id)).filter(
        RotationAssignment.company_id == principal.company_id, visible,
        *([RotationPattern.id == pattern_id] if pattern_id else []))
    total = query.count()
    rows = query.order_by(RotationAssignment.effective_from.desc(), RotationAssignment.id.desc()).offset(skip).limit(
        limit).all()
    items = [{**service.serialize_assignment(a), "pattern_id": p.id, "pattern_code": p.code,
              "version_no": v.version_no, "target_kind": a.target_kind, "team_id": a.team_id,
              "employee_name": f"{e.first_name} {e.last_name}".strip() if e is not None else None,
              "employee_code": e.employee_code if e is not None else None}
             for a, v, p, e in rows]
    return {"items": items, "total": total, "skip": skip, "limit": limit}


@router.get("/rotations")
def list_patterns(principal: Principal = Depends(READ), db: Session = Depends(get_db)):
    return service.list_patterns(db, principal)


@router.post("/rotations", status_code=201)
def create_pattern(data: PatternIn, request: Request, principal: Principal = Depends(MANAGE),
                   db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (201, service.create_pattern(db, principal, data)),
                               body=data.model_dump(mode="json"))


@router.post("/rotations/generate-ahead")
def generate_ahead(data: GenerateAheadIn, request: Request, principal: Principal = Depends(PLAN),
                   db: Session = Depends(get_db)):
    """The roster generator: periods and rotation drafts for ``days`` (default 90) from the branch-local today."""
    return idempotency.execute(db, principal, request,
                               lambda: (200, service.generate_ahead(db, principal, data.branch_id, start=data.start,
                                                                    days=data.days)),
                               body=data.model_dump(mode="json"))


@router.post("/rotation-assignments", status_code=201)
@router.post("/rotations/assignments", status_code=201)  # alias of the canonical path (earlier tests and specs)
def assign(data: RotationAssignmentIn, request: Request, principal: Principal = Depends(ASSIGN),
           db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (201, service.assign(db, principal, data)),
                               body=data.model_dump(mode="json"))


class AssignmentEndIn(StrictModel):
    effective_to: date
    reason: str = Field(..., min_length=1, max_length=500)
    row_version: Optional[int] = Field(None, ge=1)


@router.post("/rotation-assignments/{assignment_id:int}/end")
def end_assignment(assignment_id: int, data: AssignmentEndIn, request: Request, principal: Principal = Depends(ASSIGN),
                   db: Session = Depends(get_db)):
    """UA-AS-161: ``ACTIVE`` → ``ENDED`` on ``effective_to`` (IK; ``row_version`` from ``If-Match`` or the body)."""
    version = concurrency.if_match(request, data.row_version)
    return idempotency.execute(db, principal, request,
                               lambda: (200, service.end_assignment(db, principal, assignment_id, data.effective_to,
                                                                    data.reason.strip(), version)),
                               body=data.model_dump(mode="json"))


@router.post("/rotations/{pattern_id:int}/versions", status_code=201)
def add_version(pattern_id: int, data: PatternVersionIn, request: Request, principal: Principal = Depends(MANAGE),
                db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request,
                               lambda: (201, service.add_version(db, principal, pattern_id, data)),
                               body=data.model_dump(mode="json"))


@router.post("/rotations/{pattern_id:int}/versions/{version_no:int}/activate")
def activate(pattern_id: int, version_no: int, request: Request, principal: Principal = Depends(MANAGE),
             db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request,
                               lambda: (200, service.activate_version(db, principal, pattern_id, version_no)))


@router.get("/rotations/{pattern_id:int}/versions/{version_no:int}/preview")
def preview(pattern_id: int, version_no: int, from_: date = Query(..., alias="from"), to: date = Query(...),
            crew_code: Optional[str] = Query(None, max_length=8), anchor_date: Optional[date] = None,
            offset_days: int = Query(0, ge=0, le=365), principal: Principal = Depends(READ),
            db: Session = Depends(get_db)):
    return service.preview(db, principal, pattern_id, version_no, from_, to, crew_code, anchor_date, offset_days)


@router.post("/rosters/{period_id:int}/generate")
def generate(period_id: int, request: Request, data: GenerateIn = Body(default_factory=GenerateIn),
             principal: Principal = Depends(PLAN), db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request,
                               lambda: (200, service.generate_drafts(db, principal, period_id,
                                                                     overwrite=data.overwrite_drafts)),
                               body=data.model_dump(mode="json"))

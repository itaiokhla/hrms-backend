"""Rotation templates, patterns, versions and assignments (AS-M5; AS 11 §6.4; 03-shifts/02 §2-§3).

Read ``roster:read``; patterns ``shift:manage``; assignments ``shift:assign``; draft generation ``roster:plan``.
Literal ``/rotations/templates`` before ``/rotations/{pattern_id:int}``.
"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Body, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.principal import Principal, require, require_any
from app.database.db import get_db
from app.payroll.services import idempotency
from app.schemas.rosters import GenerateAheadIn, GenerateIn, PatternIn, PatternVersionIn, RotationAssignmentIn
from app.services import shift_rotations as service

router = APIRouter(prefix="/api/v1", tags=["Rotations"])

READ = require_any("roster:read", "shift:read")
MANAGE = require("shift:manage")
ASSIGN = require("shift:assign")
PLAN = require("roster:plan")


@router.get("/rotations/templates")
def templates(principal: Principal = Depends(READ)):
    return service.templates()


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


@router.post("/rotations/assignments", status_code=201)
def assign(data: RotationAssignmentIn, request: Request, principal: Principal = Depends(ASSIGN),
           db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (201, service.assign(db, principal, data)),
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

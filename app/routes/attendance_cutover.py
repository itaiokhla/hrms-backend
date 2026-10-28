"""Attendance v2 cut-over (spine §15.1; AS 13 §4.6; Settings › Attendance › Overview status card) — AS-M3.

``GET /attendance/setup/status`` · ``POST /attendance/setup/prepare`` (every ``attendance.v2_prepare`` step) ·
``POST /attendance/setup/reconcile`` (queues ``attendance.v2_reconcile``, 202) · ``POST /attendance/setup/cutover``.

Gate: ``attendance:manage_policy`` at COMPANY scope (403 otherwise). A foreign or unknown reconcile is 404; invalid
states are 409 (``ATTENDANCE_V2_ACTIVE``, ``ATTENDANCE_V2_NOT_PREPARED``, ``ATTENDANCE_V2_NOT_RECONCILED``,
``SELF_DECISION_FORBIDDEN``); the cut-over needs a fresh step-up (403 ``STEP_UP_REQUIRED``). Writes commit through
``idempotency.execute``.
"""

from datetime import date

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.errors import forbidden
from app.core.principal import Principal, require
from app.database.db import get_db
from app.payroll.services import idempotency
from app.services import attendance_cutover

router = APIRouter(prefix="/api/v1", tags=["Attendance cut-over"])
MANAGE = "attendance:manage_policy"


class ReconcileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: date
    end: date


class CutoverIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reconcile_job_id: int = Field(..., ge=1)


def _company_admin(principal: Principal) -> Principal:
    if not principal.has_company_scope(MANAGE):
        raise forbidden("This needs company-wide attendance configuration access.")
    return principal


@router.get("/attendance/setup/status")
def status(principal: Principal = Depends(require(MANAGE)), db: Session = Depends(get_db)):
    return attendance_cutover.status(db, _company_admin(principal).company_id)


@router.post("/attendance/setup/prepare")
def prepare(request: Request, principal: Principal = Depends(require(MANAGE)), db: Session = Depends(get_db)):
    _company_admin(principal)
    return idempotency.execute(db, principal, request,
                               lambda: (200, attendance_cutover.v2_prepare(db, principal.company_id)), body={})


@router.post("/attendance/setup/reconcile", status_code=202)
def reconcile(data: ReconcileIn, request: Request, principal: Principal = Depends(require(MANAGE)),
              db: Session = Depends(get_db)):
    _company_admin(principal)

    def handler():
        job = attendance_cutover.start_reconcile(db, principal, data.start, data.end)
        return 202, attendance_cutover.reconcile_json(job)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.post("/attendance/setup/cutover")
def cutover(data: CutoverIn, request: Request, principal: Principal = Depends(require(MANAGE)),
            db: Session = Depends(get_db)):
    _company_admin(principal)
    return idempotency.execute(db, principal, request,
                               lambda: (200, attendance_cutover.cutover(db, principal, data.reconcile_job_id)),
                               body=data.model_dump(mode="json"))

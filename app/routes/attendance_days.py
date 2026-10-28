"""Attendance day evaluations (AS 11 §6; 04-attendance/03 §8.1, §14; spine §15.7 V2-only) — AS-M3.

``GET /attendance/days?employee_id&start&end`` · ``GET /attendance/days/{employee_id:int}/{shift_date}``
(``?explain=true`` adds what the evaluator answers now, never written) · ``POST /attendance/days/recompute``
(``MANUAL_RECOMPUTE`` ≤ 62 days, reason required, ``attendance:manage_policy``).

V2-only (D-AS-83): v1 tenants get 404 on every path. Reads need ``attendance:read`` covering the employee (403 without
the code, 404 for an employee outside the scope or another tenant). GET paths never evaluate (BUG-AS-70); writes run
through ``idempotency.execute``, which commits.
"""

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core import authz
from app.core.errors import bad_request, not_found
from app.core.principal import Principal, require
from app.database.db import get_db
from app.payroll.services import idempotency
from app.services import attendance_access, attendance_days
from app.services.audit_log_service import AuditLogService

router = APIRouter(prefix="/api/v1", tags=["Attendance days"])
READ = "attendance:read"
MANAGE = "attendance:manage_policy"
MAX_LIST_DAYS = 31


class RecomputeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_ids: List[int] = Field(..., min_length=1, max_length=attendance_days.MAX_EMPLOYEES)
    start: date
    end: date
    reason: str = Field(..., min_length=3, max_length=500)


@router.get("/attendance/days")
def list_days(employee_id: int = Query(..., ge=1), start: date = Query(...), end: date = Query(...),
              principal: Principal = Depends(require(READ)), db: Session = Depends(get_db)):
    attendance_access.require_v2(db, principal.company_id)
    employee = authz.ensure_employee_access(db, principal, READ, employee_id)
    if end < start or (end - start).days + 1 > MAX_LIST_DAYS:
        raise bad_request(f"Choose a range of at most {MAX_LIST_DAYS} days.", "RANGE_TOO_LONG")
    rows = attendance_days.days_for(db, principal.company_id, [employee.id], start, end)
    return {"items": [attendance_days.as_json(rows[key]) for key in sorted(rows)]}


@router.get("/attendance/exceptions")
def list_exceptions(start: date = Query(...), end: date = Query(...), code: Optional[str] = Query(None, max_length=40),
                    employee_id: Optional[int] = Query(None, ge=1), skip: int = Query(0, ge=0),
                    limit: int = Query(50, ge=1, le=200), principal: Principal = Depends(require(READ)),
                    db: Session = Depends(get_db)):
    """Exceptions of decided days in the caller's ``attendance:read`` scope (04-attendance/03 §12)."""
    attendance_access.require_v2(db, principal.company_id)
    if employee_id is not None:
        authz.ensure_employee_access(db, principal, READ, employee_id)
    from app.models.attendance import AttendanceDay

    scope = authz.employee_scope(db, principal, READ).clause(AttendanceDay.employee_id)
    return attendance_days.exceptions(db, principal.company_id, scope, start, end, code=code,
                                      employee_id=employee_id, skip=skip, limit=limit)


@router.get("/attendance/days/{employee_id:int}/{shift_date}")
def get_day(employee_id: int, shift_date: date, explain: bool = False,
            principal: Principal = Depends(require(READ)), db: Session = Depends(get_db)):
    attendance_access.require_v2(db, principal.company_id)
    employee = authz.ensure_employee_access(db, principal, READ, employee_id)
    row = attendance_days.get_day(db, principal.company_id, employee.id, shift_date)
    body = {"day": None if row is None else attendance_days.as_json(
        row, versions=attendance_days.versions_of(db, principal.company_id, row.id))}
    if explain:
        body["explain"] = attendance_days.explain(db, principal.company_id, employee.id, shift_date)
    elif row is None:
        raise not_found("Evaluation")
    return body


@router.post("/attendance/days/recompute")
def recompute(data: RecomputeIn, request: Request, principal: Principal = Depends(require(MANAGE)),
              db: Session = Depends(get_db)):
    attendance_access.require_v2(db, principal.company_id)
    ids: List[int] = []
    for employee_id in sorted(set(data.employee_ids)):
        ids.append(authz.ensure_employee_access(db, principal, MANAGE, employee_id).id)

    def handler():
        result = attendance_days.recompute(db, principal.company_id, ids, data.start, data.end,
                                           actor_user_id=principal.user_id)
        AuditLogService.log(db, principal, "Attendance", "RECOMPUTED",
                            f"Recomputed {len(ids)} employee(s) {data.start.isoformat()}..{data.end.isoformat()}: "
                            f"{data.reason}"[:500], "attendance_day", None)
        return 200, result

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


def evaluation_of(db: Session, company_id: int, employee_id: int, day: date) -> Optional[dict]:
    """The stored evaluation of one day for other read models (My day), or None (v1 / not evaluated)."""
    if not attendance_access.v2_enabled(db, company_id):
        return None
    row = attendance_days.get_day(db, company_id, employee_id, day)
    return None if row is None else attendance_days.as_json(row)

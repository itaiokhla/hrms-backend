"""Overtime review, live board and statutory registers (AS-M8 G-18, AS-M9 G-20).

* ``GET  /attendance/overtime/queue`` — pending entries the caller decides, with the attendance candidate;
* ``GET  /attendance/overtime/{entry_id}/context`` — approval context (day, caps, consent, comp-off option);
* ``POST /attendance/overtime/{entry_id}/convert`` — one-click "Approve for payroll" (``PAY``) or "Convert to
  comp-off" (``COMP_OFF``), through payroll's decide path (no self-approval, minutes only lowered);
* ``POST /attendance/overtime/proposals/run`` — the proposal job for a range in the caller's scope;
* ``GET  /attendance/live-board`` — live board tiles and rows (``attendance:read`` beyond SELF);
* ``GET  /attendance/registers`` and ``/attendance/registers/form-25|form-d`` (``format`` JSON preview, CSV, XLSX,
  PDF; ``attendance:export``).
"""

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Response
from pydantic import Field
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.payroll.services import overtime as ot
from app.schemas.auth import StrictModel
from app.services import attendance_analytics, attendance_overtime, attendance_registers

router = APIRouter(prefix="/api/v1", tags=["Attendance insights"])


class ConvertIn(StrictModel):
    outcome: str = Field(..., pattern="^(PAY|COMP_OFF)$")
    minutes: Optional[int] = Field(None, ge=0, le=1440)
    remarks: Optional[str] = Field(None, max_length=500)


class ProposalRunIn(StrictModel):
    from_: date = Field(..., alias="from")
    to: date
    employee_ids: Optional[List[int]] = Field(None, max_length=500)


@router.get("/attendance/overtime/queue")
def overtime_queue(skip: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100),
                   principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return attendance_overtime.queue(db, principal, skip=skip, limit=limit)


@router.get("/attendance/overtime/{entry_id:int}/context")
def overtime_context(entry_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return attendance_overtime.context(db, principal, entry_id)


@router.post("/attendance/overtime/{entry_id:int}/convert")
def overtime_convert(entry_id: int, data: ConvertIn, principal: Principal = Depends(get_principal),
                     db: Session = Depends(get_db)):
    entry = attendance_overtime.convert(db, principal, entry_id, data.outcome, minutes=data.minutes,
                                        remarks=data.remarks)
    db.commit()
    return ot.action_result(entry) | {"comp_off": bool(entry.comp_off), "minutes_approved": entry.minutes_approved}


@router.post("/attendance/overtime/proposals/run")
def overtime_proposals_run(data: ProposalRunIn, principal: Principal = Depends(get_principal),
                           db: Session = Depends(get_db)):
    result = attendance_overtime.run_for_principal(db, principal, first=data.from_, last=data.to,
                                                   employee_ids=data.employee_ids)
    db.commit()
    return result


@router.get("/attendance/live-board")
def live_board(on: Optional[date] = Query(None, alias="date"), branch_id: Optional[int] = None,
               department_id: Optional[int] = None, team_id: Optional[int] = None, site_id: Optional[int] = None,
               q: Optional[str] = Query(None, max_length=100), status: List[str] = Query(default=[]),
               quick: Optional[str] = Query(None, max_length=20), sort: str = Query("status", max_length=20),
               skip: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100),
               principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return attendance_analytics.board(db, principal, on=on, branch_id=branch_id, department_id=department_id,
                                      team_id=team_id, site_id=site_id, q=q, status=status, quick=quick, sort=sort,
                                      skip=skip, limit=limit)


@router.get("/attendance/registers")
def registers(principal: Principal = Depends(get_principal)):
    return attendance_registers.catalogue(principal)


def _register(form: str):
    def handler(start: date = Query(..., alias="from"), end: date = Query(..., alias="to"),
                branch_id: Optional[int] = None, legal_entity_id: Optional[int] = None,
                department_id: Optional[int] = None, fmt: str = Query("JSON", alias="format", max_length=5),
                principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
        register = attendance_registers.build(db, principal, form, start=start, end=end, branch_id=branch_id,
                                              legal_entity_id=legal_entity_id, department_id=department_id)
        if fmt.upper() == "JSON":
            return register.as_json()
        content, media, name = attendance_registers.export(db, principal, register, fmt)
        db.commit()
        return Response(content, media_type=media, headers={
            "Content-Disposition": f'attachment; filename="{name}"', "X-Register-Seal": register.seal["sha256"],
            "Cache-Control": "no-store"})

    handler.__name__ = f"register_{form.lower()}"
    return handler


router.add_api_route("/attendance/registers/form-25", _register("FORM_25"), methods=["GET"])
router.add_api_route("/attendance/registers/form-d", _register("FORM_D"), methods=["GET"])

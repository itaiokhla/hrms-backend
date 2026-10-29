"""Unified cutoff assistant and the retro breakdown (G-16 / X-M5; X 03 §3, §5; D-X-16).

``GET /cutoff?pay_group_id&period_id`` — cross-module health of a pay group's period before inputs lock (attendance
days, missing punches, pending attendance / on-duty / leave requests, devices behind, lock state, stale runs).
``GET /payroll/run-employees/{run_employee_id:int}/retro-breakdown`` — the retro attendance adjustments a run
employee pays and the later corrections of its own period, each linked to the record and version that caused it.
Reads only.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.services import payroll_sync

router = APIRouter(prefix="/api/v1", tags=["Payroll sync"])


@router.get("/cutoff")
def cutoff(pay_group_id: int = Query(..., ge=1), period_id: Optional[int] = Query(None, ge=1),
           principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return payroll_sync.cutoff(db, principal, pay_group_id=pay_group_id, period_id=period_id)


@router.get("/payroll/run-employees/{run_employee_id:int}/retro-breakdown")
def retro_breakdown(run_employee_id: int, principal: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)):
    return payroll_sync.retro_breakdown(db, principal, run_employee_id)

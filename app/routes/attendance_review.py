"""Attendance review queue (AS 11 §6.12; 04-attendance/01 §11.4).

``GET /attendance/review`` and ``POST /attendance/review/{item_id:int}/decide`` (``If-Match: <row_version>``,
``Idempotency-Key``). Location kinds need ``location:review``, the others ``attendance:review``. The legacy ``/attendance/location-review*``
routes stay in ``app/routes/attendance.py`` as aliases: their decisions are mirrored into the items.
"""

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.principal import Principal, require_any
from app.database.db import get_db
from app.payroll.services import idempotency
from app.schemas.attendance_capture import ReviewDecideIn
from app.services import attendance_review as service

router = APIRouter(prefix="/api/v1", tags=["Attendance review"])

REVIEWERS = require_any(service.LOCATION_PERMISSION, service.REVIEW_PERMISSION)


@router.get("/attendance/review")
def review_queue(kind: List[str] = Query(default=[]), status: Optional[str] = Query("OPEN", max_length=12),
                 severity: Optional[str] = Query(None, max_length=8), employee_id: Optional[int] = None,
                 device_id: Optional[int] = None, start: Optional[date] = Query(None, alias="from"),
                 end: Optional[date] = Query(None, alias="to"), q: Optional[str] = Query(None, max_length=100),
                 skip: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=200),
                 principal: Principal = Depends(REVIEWERS), db: Session = Depends(get_db)):
    return service.list_items(db, principal, kinds=kind, status=status or None, severity=severity,
                              employee_id=employee_id, device_id=device_id, start=start, end=end, q=q, skip=skip,
                              limit=limit)


@router.post("/attendance/review/{item_id:int}/decide")
def decide(item_id: int, data: ReviewDecideIn, request: Request, principal: Principal = Depends(REVIEWERS),
           db: Session = Depends(get_db)):
    raw = (request.headers.get("If-Match") or "").strip().strip('"')
    version = int(raw) if raw.isdigit() else None
    return idempotency.execute(
        db, principal, request,
        lambda: (200, service.decide(db, principal, item_id, data.status, data.note, version)),
        body=data.model_dump(mode="json"))

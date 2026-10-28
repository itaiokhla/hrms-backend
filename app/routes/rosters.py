"""Roster periods, drafts, publication and My schedule (AS-M5; AS 11 §6.5; 03-shifts/02 §4-§5).

Read ``roster:read``; drafts ``roster:plan``; publish ``roster:publish`` (exemptions ``roster:exempt``). Literal paths
(``/rosters/direct-publish``) are declared before ``/rosters/{period_id:int}``. Writes go through
``idempotency.execute`` (the only commit).
"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core import concurrency
from app.core.principal import Principal, get_principal, require
from app.database.db import get_db
from app.payroll.services import idempotency
from app.schemas.rosters import DirectPublishIn, DraftsIn, PeriodIn, PeriodStatusIn, PublishIn
from app.services import shift_rosters as service

router = APIRouter(prefix="/api/v1", tags=["Rosters"])

READ = require("roster:read")
PLAN = require("roster:plan")
PUBLISH = require("roster:publish")


@router.get("/rosters")
def list_periods(status: Optional[str] = Query(None, max_length=10), skip: int = Query(0, ge=0),
                 limit: int = Query(50, ge=1, le=200), principal: Principal = Depends(READ),
                 db: Session = Depends(get_db)):
    return service.list_periods(db, principal, status=status, skip=skip, limit=limit)


@router.post("/rosters", status_code=201)
def create_period(data: PeriodIn, request: Request, principal: Principal = Depends(PLAN),
                  db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (201, service.create_period(db, principal, data)),
                               body=data.model_dump(mode="json"))


@router.post("/rosters/direct-publish")
def direct_publish(data: DirectPublishIn, request: Request, principal: Principal = Depends(PUBLISH),
                   db: Session = Depends(get_db)):
    if data.dry_run:
        result = service.direct_publish(db, principal, data)
        db.rollback()
        return result
    return idempotency.execute(db, principal, request, lambda: (200, service.direct_publish(db, principal, data)),
                               body=data.model_dump(mode="json"))


@router.get("/rosters/{period_id:int}")
def grid(period_id: int, principal: Principal = Depends(READ), db: Session = Depends(get_db)):
    return service.grid(db, principal, period_id)


@router.put("/rosters/{period_id:int}/drafts")
def put_drafts(period_id: int, data: DraftsIn, request: Request, principal: Principal = Depends(PLAN),
               db: Session = Depends(get_db)):
    version = concurrency.if_match(request, data.row_version)
    return idempotency.execute(db, principal, request,
                               lambda: (200, service.put_drafts(db, principal, period_id, data.cells, version)),
                               body=data.model_dump(mode="json"))


@router.delete("/rosters/{period_id:int}/drafts")
def discard_drafts(period_id: int, request: Request, row_version: Optional[int] = Query(None, ge=1),
                   principal: Principal = Depends(PLAN), db: Session = Depends(get_db)):
    version = concurrency.if_match(request, row_version)
    return idempotency.execute(db, principal, request,
                               lambda: (200, service.discard_drafts(db, principal, period_id, version)))


@router.post("/rosters/{period_id:int}/validate")
def validate(period_id: int, principal: Principal = Depends(PLAN), db: Session = Depends(get_db)):
    result = service.publish(db, principal, period_id, PublishIn(dry_run=True), validate_only=True)
    db.rollback()
    return result


@router.post("/rosters/{period_id:int}/publish")
def publish(period_id: int, data: PublishIn, request: Request, principal: Principal = Depends(PUBLISH),
            db: Session = Depends(get_db)):
    if data.dry_run:
        result = service.publish(db, principal, period_id, data)
        db.rollback()
        return result
    return idempotency.execute(db, principal, request,
                               lambda: (200, service.publish(db, principal, period_id, data)),
                               body=data.model_dump(mode="json", by_alias=True))


@router.post("/rosters/{period_id:int}/cancel")
def cancel(period_id: int, request: Request, data: PeriodStatusIn = PeriodStatusIn(),
           principal: Principal = Depends(PLAN), db: Session = Depends(get_db)):
    version = concurrency.if_match(request, data.row_version)
    return idempotency.execute(db, principal, request,
                               lambda: (200, service.set_period_status(db, principal, period_id, "CANCELLED", version)))


@router.post("/rosters/{period_id:int}/close")
def close(period_id: int, request: Request, data: PeriodStatusIn = PeriodStatusIn(),
          principal: Principal = Depends(PUBLISH), db: Session = Depends(get_db)):
    version = concurrency.if_match(request, data.row_version)
    return idempotency.execute(db, principal, request,
                               lambda: (200, service.set_period_status(db, principal, period_id, "CLOSED", version)))


@router.get("/me/schedule")
def my_schedule(from_: Optional[date] = Query(None, alias="from"), to: Optional[date] = None,
                principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.my_schedule(db, principal, from_, to)  # defaults: the employee-local today + 13 days

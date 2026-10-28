"""Sites admin (AS 11 §6.14; 04-attendance/01 §9.1, §13.5; spine §8.2 "Sites").

``GET /attendance/sites`` · ``GET /attendance/sites/network-test`` (literal, declared before ``{site_id}``;
SP-AS-239) · ``POST /attendance/sites`` · ``GET|PATCH /attendance/sites/{site_id:int}`` (``If-Match: <row_version>``)
· ``POST /attendance/sites/{site_id:int}/archive|rotate-qr``. Gate ``attendance:manage_sites`` (reads at any scope,
writes at COMPANY). Writes go through ``idempotency.execute``. The QR poster payload is returned once by
``rotate-qr`` and never again.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core import clock
from app.core.principal import Principal, require
from app.core.rate_limit import client_ip
from app.database.db import get_db
from app.payroll.services import idempotency
from app.schemas.attendance_capture import SiteAction, SiteIn, SitePatch
from app.services import attendance_sites as service

router = APIRouter(prefix="/api/v1", tags=["Attendance sites"])
MANAGERS = require(service.MANAGE)


def _if_match(request: Request) -> Optional[int]:
    raw = (request.headers.get("If-Match") or "").strip().strip('"')
    return int(raw) if raw.isdigit() else None


@router.get("/attendance/sites")
def list_sites(status: Optional[str] = Query(None, max_length=10), branch_id: Optional[int] = None,
               q: Optional[str] = Query(None, max_length=100), skip: int = Query(0, ge=0),
               limit: int = Query(25, ge=1, le=200), principal: Principal = Depends(MANAGERS),
               db: Session = Depends(get_db)):
    return service.list_sites(db, principal, status=status, branch_id=branch_id, q=q, skip=skip, limit=limit)


@router.get("/attendance/sites/network-test")
def network_test(request: Request, site_id: Optional[int] = None, branch_id: Optional[int] = None,
                 principal: Principal = Depends(MANAGERS), db: Session = Depends(get_db)):
    return service.network_test(db, principal, client_ip(request), site_id=site_id, branch_id=branch_id)


@router.post("/attendance/sites", status_code=201)
def create_site(data: SiteIn, request: Request, principal: Principal = Depends(MANAGERS),
                db: Session = Depends(get_db)):
    today = clock.company_today(db, principal.company_id)
    return idempotency.execute(db, principal, request,
                               lambda: (201, service.create_site(db, principal, data.model_dump(), today)),
                               body=data.model_dump(mode="json"))


@router.get("/attendance/sites/{site_id:int}")
def get_site(site_id: int, principal: Principal = Depends(MANAGERS), db: Session = Depends(get_db)):
    return service.serialize_site(service.load_site(db, principal.company_id, site_id))


@router.patch("/attendance/sites/{site_id:int}")
def update_site(site_id: int, data: SitePatch, request: Request, principal: Principal = Depends(MANAGERS),
                db: Session = Depends(get_db)):
    version = _if_match(request)
    values = data.model_dump(exclude_unset=True)
    return idempotency.execute(db, principal, request,
                               lambda: (200, service.update_site(db, principal, site_id, values, version)),
                               body=data.model_dump(mode="json", exclude_unset=True))


@router.post("/attendance/sites/{site_id:int}/archive")
def archive_site(site_id: int, data: SiteAction, request: Request, principal: Principal = Depends(MANAGERS),
                 db: Session = Depends(get_db)):
    version = _if_match(request)
    return idempotency.execute(db, principal, request,
                               lambda: (200, service.archive_site(db, principal, site_id, version, data.reason)),
                               body=data.model_dump(mode="json"))


@router.post("/attendance/sites/{site_id:int}/rotate-qr")
def rotate_qr(site_id: int, request: Request, principal: Principal = Depends(MANAGERS),
              db: Session = Depends(get_db)):
    version = _if_match(request)
    return idempotency.execute(db, principal, request,
                               lambda: (200, service.rotate_qr(db, principal, site_id, version)), body={})

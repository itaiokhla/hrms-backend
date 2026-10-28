"""Device registry, KIOSK only in AS-M2 (AS 11 §6.13; 04-attendance/01 §5.1, §7.2, §13.4).

``GET/POST /attendance/devices``, ``GET /attendance/devices/{device_id:int}``,
``POST /attendance/devices/{device_id:int}/suspend|resume|retire|claim-code``. Create returns the one-time claim
code (shown once, 24 h). Writes go through ``idempotency.execute``.

Gate: ``attendance:manage_devices`` at COMPANY (registered and granted in as_m2_sites_consents, SP-AS-169); branch-
scoped device management and step-up on activation are FOLLOW-UPS (FU-AS-15).
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.errors import forbidden
from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.payroll.services import idempotency
from app.schemas.attendance_capture import DeviceAction, DeviceIn
from app.services import attendance_devices as service

router = APIRouter(prefix="/api/v1", tags=["Attendance devices"])

MANAGE_DEVICES = "attendance:manage_devices"


def manage_devices(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.has_company_scope(MANAGE_DEVICES):
        raise forbidden()
    return principal


def _if_match(request: Request) -> Optional[int]:
    raw = (request.headers.get("If-Match") or "").strip().strip('"')
    return int(raw) if raw.isdigit() else None


@router.get("/attendance/devices")
def list_devices(status: Optional[str] = Query(None, max_length=20), q: Optional[str] = Query(None, max_length=100),
                 skip: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=200),
                 principal: Principal = Depends(manage_devices), db: Session = Depends(get_db)):
    return service.list_devices(db, principal, status=status, q=q, skip=skip, limit=limit)


@router.post("/attendance/devices", status_code=201)
def create_device(data: DeviceIn, request: Request, principal: Principal = Depends(manage_devices),
                  db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (201, service.create_device(db, principal, data)),
                               body=data.model_dump(mode="json"))


@router.get("/attendance/devices/{device_id:int}")
def get_device(device_id: int, principal: Principal = Depends(manage_devices), db: Session = Depends(get_db)):
    return service.get_device(db, principal, device_id)


@router.post("/attendance/devices/{device_id:int}/claim-code")
def reissue_claim_code(device_id: int, request: Request, principal: Principal = Depends(manage_devices),
                       db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request,
                               lambda: (200, service.reissue_claim_code(db, principal, device_id)), body={})


def _transition(action: str):
    def handler(device_id: int, data: DeviceAction, request: Request, principal: Principal = Depends(manage_devices),
                db: Session = Depends(get_db)):
        version = _if_match(request)
        return idempotency.execute(
            db, principal, request,
            lambda: (200, service.transition(db, principal, device_id, action, data.reason, version)),
            body=data.model_dump(mode="json"))

    handler.__name__ = f"{action}_device"
    return handler


for _action in ("suspend", "resume", "retire"):
    router.add_api_route(f"/attendance/devices/{{device_id:int}}/{_action}", _transition(_action), methods=["POST"])

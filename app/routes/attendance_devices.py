"""Device registry (AS 11 §6.13; 04-attendance/01 §5.1, §7.2-§7.4, §8.6, §13.4): KIOSK (AS-M2), TERMINAL / AGENT
(AS-M7, G-17).

``GET/POST /attendance/devices``, ``GET/PATCH /attendance/devices/{device_id:int}``,
``POST /attendance/devices/{device_id:int}/suspend|resume|retire|claim-code``. Create returns the one-time claim
code (shown once, 24 h). Writes go through ``idempotency.execute``. AS-M7 adds ``…/activate`` and
``…/rotate-secret`` (step-up; the secret is shown once and never replayed), ``…/first-contact``, ``…/summary``,
``…/commands`` (+ cancel), ``…/rejects`` (+ reveal with step-up, dismiss), ``/attendance/device-mappings``
(+ ``auto-match``, ``{id}/end``) and ``/attendance/imports`` (CSV dry run, ``{id}/commit``, ``template``).

Gate: ``attendance:manage_devices`` at COMPANY (registered and granted in as_m2_sites_consents, SP-AS-169); branch-
scoped device management and step-up on activation are FOLLOW-UPS (FU-AS-15).
"""

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.core.errors import AppError, forbidden
from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.payroll.services import idempotency
from app.schemas.attendance_capture import (
    DeviceAction,
    DeviceActivateIn,
    DeviceAutoMatchIn,
    DeviceCommandIn,
    DeviceIn,
    DeviceMappingEnd,
    DeviceMappingsIn,
    DevicePatch,
)
from app.services import attendance_devices as service
from app.services import device_admin as admin

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
                 kind: Optional[str] = Query(None, max_length=10),
                 skip: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=200),
                 principal: Principal = Depends(manage_devices), db: Session = Depends(get_db)):
    return service.list_devices(db, principal, status=status, q=q, skip=skip, limit=limit, kind=kind)


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


# --------------------------------------------------------------------------- TERMINAL / AGENT (AS-M7, G-17)
def _commit(db: Session, fn):
    result = fn()
    db.commit()
    return result


@router.patch("/attendance/devices/{device_id:int}")
def update_device(device_id: int, data: DevicePatch, request: Request, principal: Principal = Depends(manage_devices),
                  db: Session = Depends(get_db)):
    return _commit(db, lambda: admin.update_device(db, principal, device_id, data.model_dump(exclude_unset=True),
                                                   _if_match(request)))


@router.get("/attendance/devices/{device_id:int}/first-contact")
def first_contact(device_id: int, principal: Principal = Depends(manage_devices), db: Session = Depends(get_db)):
    return admin.first_contact(db, principal, device_id)


@router.get("/attendance/devices/{device_id:int}/summary")
def device_summary(device_id: int, principal: Principal = Depends(manage_devices), db: Session = Depends(get_db)):
    return admin.device_summary(db, principal, device_id)


@router.post("/attendance/devices/{device_id:int}/activate")
def activate_device(device_id: int, data: DeviceActivateIn, principal: Principal = Depends(manage_devices),
                    db: Session = Depends(get_db)):
    """Step-up; the secret is in this reply only (never stored in an idempotency replay)."""
    try:
        result = admin.activate(db, principal, device_id, data.claim_code, data.serial_number)
    except AppError as exc:
        if exc.code == "CLAIM_CODE_INVALID":
            db.commit()  # the failure counter is kept
        raise
    db.commit()
    return result


@router.post("/attendance/devices/{device_id:int}/rotate-secret")
def rotate_secret(device_id: int, principal: Principal = Depends(manage_devices), db: Session = Depends(get_db)):
    return _commit(db, lambda: admin.rotate_secret(db, principal, device_id))


@router.get("/attendance/devices/{device_id:int}/commands")
def list_commands(device_id: int, principal: Principal = Depends(manage_devices), db: Session = Depends(get_db)):
    return _commit(db, lambda: admin.list_commands(db, principal, device_id))


@router.post("/attendance/devices/{device_id:int}/commands", status_code=201)
def queue_command(device_id: int, data: DeviceCommandIn, request: Request,
                  principal: Principal = Depends(manage_devices), db: Session = Depends(get_db)):
    return idempotency.execute(
        db, principal, request, lambda: (201, admin.queue_command(db, principal, device_id, data.command, data.payload)),
        body=data.model_dump(mode="json"))


@router.post("/attendance/devices/{device_id:int}/commands/{command_id:int}/cancel")
def cancel_command(device_id: int, command_id: int, request: Request, principal: Principal = Depends(manage_devices),
                   db: Session = Depends(get_db)):
    return _commit(db, lambda: admin.cancel_command(db, principal, device_id, command_id, _if_match(request)))


@router.get("/attendance/devices/{device_id:int}/rejects")
def list_rejects(device_id: int, status: str = Query("OPEN", pattern="^(OPEN|DISMISSED|FIXED)$"),
                 principal: Principal = Depends(manage_devices), db: Session = Depends(get_db)):
    return admin.list_rejects(db, principal, device_id, status)


@router.post("/attendance/devices/{device_id:int}/rejects/{reject_id:int}/reveal")
def reveal_reject(device_id: int, reject_id: int, principal: Principal = Depends(manage_devices),
                  db: Session = Depends(get_db)):
    return _commit(db, lambda: admin.reveal_reject(db, principal, device_id, reject_id))


@router.post("/attendance/devices/{device_id:int}/rejects/{reject_id:int}/dismiss")
def dismiss_reject(device_id: int, reject_id: int, principal: Principal = Depends(manage_devices),
                   db: Session = Depends(get_db)):
    return _commit(db, lambda: admin.dismiss_reject(db, principal, device_id, reject_id))


@router.get("/attendance/device-mappings")
def list_mappings(device_id: Optional[int] = Query(None), q: Optional[str] = Query(None, max_length=100),
                  skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                  principal: Principal = Depends(manage_devices), db: Session = Depends(get_db)):
    return admin.list_mappings(db, principal, device_id=device_id, q=q, skip=skip, limit=limit)


@router.post("/attendance/device-mappings", status_code=201)
def save_mappings(data: DeviceMappingsIn, request: Request, principal: Principal = Depends(manage_devices),
                  db: Session = Depends(get_db)):
    return idempotency.execute(
        db, principal, request,
        lambda: (201, admin.save_mappings(db, principal, [i.model_dump() for i in data.items])),
        body=data.model_dump(mode="json"))


@router.post("/attendance/device-mappings/auto-match")
def auto_match(data: DeviceAutoMatchIn, principal: Principal = Depends(manage_devices),
               db: Session = Depends(get_db)):
    return _commit(db, lambda: admin.auto_match(db, principal, data.device_id, data.effective_from, data.dry_run))


@router.post("/attendance/device-mappings/{mapping_id:int}/end")
def end_mapping(mapping_id: int, data: DeviceMappingEnd, request: Request,
                principal: Principal = Depends(manage_devices), db: Session = Depends(get_db)):
    return _commit(db, lambda: admin.end_mapping(db, principal, mapping_id, data.effective_to, _if_match(request)))


@router.get("/attendance/imports/template", response_class=PlainTextResponse)
def import_template(principal: Principal = Depends(manage_devices)):
    return PlainTextResponse(admin.TEMPLATE, media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="punch-import-template.csv"'})


async def _import(request: Request, file: UploadFile, dry_run: bool, principal: Principal, db: Session):
    content = await file.read(admin.MAX_IMPORT_BYTES + 1)
    result = admin.import_dry_run(db, principal, file.filename or "punches.csv", content)
    if not dry_run:
        result = admin.import_commit(db, principal, result["id"])
    db.commit()
    return result


@router.post("/attendance/imports", status_code=201)
async def create_import(request: Request, file: UploadFile = File(...), dry_run: bool = Form(True),
                        principal: Principal = Depends(manage_devices), db: Session = Depends(get_db)):
    """CSV punches (01 §8.6): ``dry_run`` (default) returns the per-row issues; commit with ``…/{id}/commit``."""
    return await _import(request, file, dry_run, principal, db)


@router.post("/attendance/devices/import", status_code=201, include_in_schema=False)
async def create_import_alias(request: Request, file: UploadFile = File(...), dry_run: bool = Form(True),
                              principal: Principal = Depends(manage_devices), db: Session = Depends(get_db)):
    return await _import(request, file, dry_run, principal, db)


@router.get("/attendance/imports/{import_id:int}")
def get_import(import_id: int, principal: Principal = Depends(manage_devices), db: Session = Depends(get_db)):
    return admin.get_import(db, principal, import_id)


@router.post("/attendance/imports/{import_id:int}/commit")
def commit_import(import_id: int, principal: Principal = Depends(manage_devices), db: Session = Depends(get_db)):
    return _commit(db, lambda: admin.import_commit(db, principal, import_id))

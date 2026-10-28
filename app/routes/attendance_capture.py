"""Capture v2 routes (AS 11 §6.8; 04-attendance/01 §4, §5; spine §8.1, §8.2).

``POST /attendance/me/punch`` (``attendance:mark`` SELF; 20/min per user) and the device-token kiosk
``POST /attendance/kiosk/pair`` (the one unauthenticated attendance route: claim code -> device-token cookie),
``POST /attendance/kiosk/identify`` (60/min per device) and ``POST /attendance/kiosk/punch``. Registered before
``attendance.router`` (IG-AS-09); the legacy ``/attendance/me/check-in`` ... and the operator ``/attendance/kiosk/scan``
stay in ``app/routes/attendance.py``.

The interactive punch carries its own idempotency (``Idempotency-Key`` header or ``meta.idempotency_key`` -> the
punch natural key, P-1: a replay returns the first result with ``replayed: true``), so these routes commit through
``_commit`` (map constraint errors, one commit) instead of ``idempotency.execute``, which needs a user principal.
"""

from typing import Callable, Optional

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import settings
from app.core import db_errors, storage
from app.core.errors import AppError, bad_request
from app.core.hosts import request_host
from app.core.principal import KIOSK_COOKIE_PATH, KIOSK_TOKEN_COOKIE, Principal, get_device_principal, require
from app.core.rate_limit import client_ip, limiter
from app.database.db import bind_tenant, get_db, system_session
from app.schemas.attendance_capture import KioskIdentifyIn, KioskPairIn, KioskPunchIn, PunchMeta
from app.services import attendance_capture, attendance_devices
from app.services.attendance_devices import DevicePrincipal

router = APIRouter(prefix="/api/v1", tags=["Attendance capture"])

PAIR_PER_IP = (10, 15 * 60)  # SP-AS-055
PAIR_PER_HOST = (60, 60 * 60)  # AS 07 §10


def _commit(db: Session, handler: Callable[[], dict]) -> dict:
    with db_errors.mapped():
        result = handler()
        db.flush()
    db.commit()
    return result


def _set_device_cookie(response: Response, token: str) -> None:
    response.set_cookie(KIOSK_TOKEN_COOKIE, token, max_age=int(attendance_devices.TOKEN_TTL.total_seconds()),
                        httponly=True, secure=settings.cookie_secure, samesite="strict", path=KIOSK_COOKIE_PATH)


def _parse_meta(raw: Optional[str], request: Request) -> PunchMeta:
    if not raw:
        raise bad_request("The punch details are missing.")
    if len(raw) > 8192:
        raise bad_request("The request metadata is too large.")
    try:
        meta = PunchMeta.model_validate_json(raw)
    except ValidationError as exc:
        raise RequestValidationError([{**err, "loc": ("meta", *err["loc"])} for err in exc.errors()])
    header = request.headers.get("Idempotency-Key")
    if header and not meta.idempotency_key:
        try:
            meta = PunchMeta.model_validate({**meta.model_dump(), "idempotency_key": header})
        except ValidationError as exc:
            raise RequestValidationError([{**err, "loc": ("header", "Idempotency-Key")} for err in exc.errors()])
    return meta


@router.post("/attendance/me/punch")
async def me_punch(request: Request, file: Optional[UploadFile] = File(None), meta: Optional[str] = Form(None),
                   principal: Principal = Depends(require("attendance:mark")), db: Session = Depends(get_db)):
    """The one v2 punch endpoint (direction IN / OUT / BREAK_START / BREAK_END)."""
    limiter.check(f"punch:{principal.company_id}:{principal.user_id}", attendance_capture.PUNCH_PER_MINUTE, 60)
    data = _parse_meta(meta, request)
    image = None
    if file is not None:
        image, _ = await storage.read_image_upload(file)
    return _commit(db, lambda: attendance_capture.punch(db, principal, data, image, client_ip(request)))


def _pair_company(request: Request, device_code: str, claim_code: str) -> Optional[int]:
    """Tenant of a pairing request: the tenant host, else the device row named by the code (system session)."""
    context = request_host(request)
    if context.company_id is not None:
        return context.company_id
    from app.models.attendance import AttendanceDevice

    code = (device_code or "").strip().upper()
    with system_session() as sdb:
        rows = sdb.query(AttendanceDevice.company_id, AttendanceDevice.claim_code_hash).filter(
            AttendanceDevice.code == code, AttendanceDevice.kind == "KIOSK",
            AttendanceDevice.status == "PENDING_ACTIVATION").all()
    if not rows:
        return None
    wanted = attendance_devices._claim_hash(claim_code)
    matching = [cid for cid, digest in rows if digest == wanted]
    if len(matching) == 1:
        return matching[0]
    return rows[0][0] if len(rows) == 1 else None


@router.post("/attendance/kiosk/pair")
def kiosk_pair(data: KioskPairIn, request: Request, db: Session = Depends(get_db)):
    """Pair a kiosk with the claim code shown once at registration (no user session; K-1, K-5)."""
    ip = client_ip(request)
    limiter.check(f"kiosk-pair:{ip}", *PAIR_PER_IP)
    limiter.check(f"kiosk-pair-host:{request_host(request).host}", *PAIR_PER_HOST)
    company_id = _pair_company(request, data.device_code, data.claim_code)
    if company_id is None:
        raise AppError(422, "The device code or claim code is not valid.", "CLAIM_CODE_INVALID")
    bind_tenant(db, company_id)
    try:
        body, token = attendance_capture.kiosk_pair(db, company_id, data.device_code, data.claim_code, ip)
    except AppError:
        db.commit()  # the failure counter of the device row is kept (K-5)
        raise
    db.commit()
    response = JSONResponse(jsonable_encoder(body))
    _set_device_cookie(response, token)
    return response


def _device_response(device: DevicePrincipal, body: dict) -> JSONResponse:
    response = JSONResponse(jsonable_encoder(body))
    if device.renewed_token:
        _set_device_cookie(response, device.renewed_token)  # K-12 sliding renewal
    return response


@router.post("/attendance/kiosk/identify")
async def kiosk_identify(request: Request, method: str = Form(...), value: Optional[str] = Form(None),
                         file: Optional[UploadFile] = File(None),
                         device: DevicePrincipal = Depends(get_device_principal), db: Session = Depends(get_db)):
    try:
        data = KioskIdentifyIn(method=method, value=value)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors())
    image = None
    if file is not None:
        image, _ = await storage.read_image_upload(file)
    body = _commit(db, lambda: attendance_capture.kiosk_identify(db, device, data, image))
    return _device_response(device, body)


@router.post("/attendance/kiosk/punch")
def kiosk_punch(data: KioskPunchIn, request: Request, device: DevicePrincipal = Depends(get_device_principal),
                db: Session = Depends(get_db)):
    body = _commit(db, lambda: attendance_capture.kiosk_punch(db, device, data.kiosk_ticket, data.direction,
                                                              client_ip(request)))
    return _device_response(device, body)

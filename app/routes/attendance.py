from datetime import date, timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core import authz, storage
from app.core.errors import bad_request
from app.core.principal import Principal, get_principal, require, require_any
from app.core.rate_limit import client_ip, limiter
from app.database.db import get_db
from app.modules.face_recognition import engine as face_engine
from app.schemas.admin import ManualAttendance
from app.schemas.attendance import EventMeta, ReviewDecision, SampleSubmission
from app.services import attendance_policy, attendance_service, location_service, org_structure_service, settings_service

router = APIRouter(prefix="/api/v1", tags=["Attendance"])


async def _optional_image(file: Optional[UploadFile]) -> Optional[bytes]:
    if file is None:
        return None
    data, _ = await storage.read_image_upload(file)
    return data


def _parse_meta(raw: Optional[str]) -> Optional[EventMeta]:
    """The JSON `meta` form field (idempotency key, location attempt, confirmations)."""
    if not raw:
        return None
    if len(raw) > 8192:
        raise bad_request("The request metadata is too large.")
    try:
        return EventMeta.model_validate_json(raw)
    except ValidationError as exc:
        raise RequestValidationError([{**err, "loc": ("meta", *err["loc"])} for err in exc.errors()])


# Punch endpoints 20 / min per user and the operator kiosk 60 / min per operator (AS 07 §10, spine §8.1; BUG-AS-62).
LEGACY_PUNCH_PER_MINUTE = 20
KIOSK_SCAN_PER_MINUTE = 60


async def _act(action: str, request: Request, file, meta, principal: Principal, db: Session) -> dict:
    limiter.check(f"punch-legacy:{principal.company_id}:{principal.user_id}", LEGACY_PUNCH_PER_MINUTE, 60)
    image = await _optional_image(file)
    result = attendance_service.perform(db, principal, action, image, _parse_meta(meta), client_ip(request))
    db.commit()
    return result


@router.post("/attendance/me/check-in")
async def check_in(request: Request, file: Optional[UploadFile] = File(None), meta: Optional[str] = Form(None),
                   principal: Principal = Depends(require("attendance:mark")), db: Session = Depends(get_db)):
    """Punch In."""
    return await _act("PUNCH_IN", request, file, meta, principal, db)


@router.post("/attendance/me/check-out")
async def check_out(request: Request, file: Optional[UploadFile] = File(None), meta: Optional[str] = Form(None),
                    principal: Principal = Depends(require("attendance:mark")), db: Session = Depends(get_db)):
    """Punch Out."""
    return await _act("PUNCH_OUT", request, file, meta, principal, db)


@router.post("/attendance/me/lunch-in")
async def lunch_in(request: Request, file: Optional[UploadFile] = File(None), meta: Optional[str] = Form(None),
                   principal: Principal = Depends(require("attendance:mark")), db: Session = Depends(get_db)):
    """Lunch In: start a lunch break."""
    return await _act("LUNCH_IN", request, file, meta, principal, db)


@router.post("/attendance/me/lunch-out")
async def lunch_out(request: Request, file: Optional[UploadFile] = File(None), meta: Optional[str] = Form(None),
                    principal: Principal = Depends(require("attendance:mark")), db: Session = Depends(get_db)):
    """Lunch Out: end the lunch break and resume work."""
    return await _act("LUNCH_OUT", request, file, meta, principal, db)


@router.get("/attendance/me/today")
def my_today(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    result = attendance_service.my_today(db, principal)
    db.commit()  # a shift left open past the company's limit is closed when first seen
    return result


@router.get("/attendance/me/tracking")
def my_tracking(principal: Principal = Depends(require("attendance:mark")), db: Session = Depends(get_db)):
    result = location_service.tracking_status(db, principal)
    db.commit()  # missed slots are recorded as soon as they are observed
    return result


@router.post("/attendance/me/location-samples")
def submit_location(data: SampleSubmission, principal: Principal = Depends(require("attendance:mark")),
                    db: Session = Depends(get_db)):
    result = location_service.submit_sample(db, principal, data)
    db.commit()
    return result


@router.get("/attendance/me/history")
def my_history(start_date: Optional[date] = None, end_date: Optional[date] = None,
               principal: Principal = Depends(require("attendance:read")), db: Session = Depends(get_db)):
    employee = authz.own_employee(db, principal)
    # The employee's own "today", never the server's date (BUG-AS-10).
    end = end_date or org_structure_service.employee_local_today(db, employee)
    start = start_date or end - timedelta(days=30)
    total, items = attendance_service.list_records(db, principal, start, end, employee_id=employee.id, limit=200)
    return {"total": total, "items": items}


@router.get("/attendance/policy")
def get_policy(principal: Principal = Depends(require_any("attendance:mark", "attendance:read", "settings:manage")),
               db: Session = Depends(get_db)):
    """The effective attendance and location policy (every member may see what is collected about them)."""
    result = {"policy": attendance_policy.load(db, principal.company_id).client_view()}
    if principal.has("settings:manage"):
        result["settings"] = attendance_policy.settings_view(db, principal.company_id)
    return result


@router.patch("/attendance/policy")
def update_policy(data: Dict[str, Any] = Body(...), principal: Principal = Depends(require("settings:manage")),
                  db: Session = Depends(get_db)):
    unknown = set(data) - settings_service.ATTENDANCE_POLICY_KEYS
    if unknown:
        raise bad_request(f"Not an attendance policy setting: {', '.join(sorted(unknown))}")
    settings_service.update(db, principal, data, only=settings_service.ATTENDANCE_POLICY_KEYS)
    db.commit()
    return {"policy": attendance_policy.load(db, principal.company_id).client_view(),
            "settings": attendance_policy.settings_view(db, principal.company_id)}


@router.post("/attendance/kiosk/scan")
async def kiosk_scan(request: Request, file: UploadFile = File(...),
                     principal: Principal = Depends(require("attendance:kiosk")), db: Session = Depends(get_db)):
    limiter.check(f"kiosk-scan:{principal.company_id}:{principal.user_id}", KIOSK_SCAN_PER_MINUTE, 60)
    data, _ = await storage.read_image_upload(file)
    result = attendance_service.kiosk_scan(db, principal, data, client_ip(request))
    db.commit()
    return result


@router.get("/attendance")
def list_attendance(
    start_date: date,
    end_date: date,
    employee_id: Optional[int] = None,
    department_id: Optional[int] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    principal: Principal = Depends(require("attendance:read")),
    db: Session = Depends(get_db),
):
    total, items = attendance_service.list_records(db, principal, start_date, end_date, employee_id, department_id, skip, limit)
    return {"total": total, "items": items, "skip": skip, "limit": limit}


@router.get("/attendance/hours")
def hours_summary(start_date: date, end_date: date, employee_id: Optional[int] = None,
                  department_id: Optional[int] = None, principal: Principal = Depends(require("attendance:read")),
                  db: Session = Depends(get_db)):
    return attendance_service.hours_summary(db, principal, start_date, end_date, employee_id, department_id)


@router.get("/attendance/sessions/{attendance_id}")
def session_detail(attendance_id: int, include_locations: bool = False,
                   principal: Principal = Depends(require("attendance:read")), db: Session = Depends(get_db)):
    result = attendance_service.session_detail(db, principal, attendance_id, include_locations)
    db.commit()  # records the access audit when coordinates were returned
    return result


@router.get("/attendance/location-review")
def location_review_queue(status: str = Query("PENDING", pattern=r"^(PENDING|APPROVED|REJECTED|ACKNOWLEDGED)$"),
                          limit: int = Query(100, ge=1, le=500),
                          principal: Principal = Depends(require("location:review")), db: Session = Depends(get_db)):
    return location_service.review_queue(db, principal, status, limit)


@router.post("/attendance/location-review/{sample_id}")
def review_location(sample_id: int, data: ReviewDecision, principal: Principal = Depends(require("location:review")),
                    db: Session = Depends(get_db)):
    result = location_service.review(db, principal, sample_id, data)
    db.commit()
    return result


@router.get("/attendance/summary/today")
def today_summary(principal: Principal = Depends(require("attendance:read")), db: Session = Depends(get_db)):
    return attendance_service.today_summary(db, principal)


@router.get("/attendance/trend")
def attendance_trend(days: int = Query(7, ge=1, le=90), principal: Principal = Depends(require("attendance:read")),
                     db: Session = Depends(get_db)):
    return attendance_service.trend(db, principal, days)


@router.get("/attendance/recent")
def recent(principal: Principal = Depends(require("attendance:read")), db: Session = Depends(get_db)):
    return attendance_service.recent_activity(db, principal)


@router.get("/attendance/statistics")
def statistics(employee_id: int, month: int = Query(..., ge=1, le=12), year: int = Query(..., ge=2000, le=2100),
               principal: Principal = Depends(require("attendance:read")), db: Session = Depends(get_db)):
    return attendance_service.monthly_stats(db, principal, employee_id, year, month)


@router.post("/attendance/manual")
def manual_entry(data: ManualAttendance, principal: Principal = Depends(require("attendance:regularize")),
                 db: Session = Depends(get_db)):
    record = attendance_service.record_manual(db, principal, data)
    db.commit()
    breaks = attendance_service.breaks_by_session(db, [record.id])[record.id]
    return attendance_service.serialize(record, breaks=breaks)


@router.get("/face/status")
def face_engine_status(principal: Principal = Depends(get_principal)):
    return {"engine_available": face_engine.available()}


@router.post("/face/enroll")
async def enroll_face(employee_id: int, file: UploadFile = File(...),
                      principal: Principal = Depends(require("face:enroll")), db: Session = Depends(get_db)):
    data, ext = await storage.read_image_upload(file)
    result = attendance_service.enroll_face(db, principal, employee_id, data, ext)
    db.commit()
    return result


@router.get("/face/employees/{employee_id}")
def face_status(employee_id: int, principal: Principal = Depends(require("employee:read")),
                db: Session = Depends(get_db)):
    return attendance_service.face_status(db, principal, employee_id)


@router.delete("/face/employees/{employee_id}")
def remove_faces(employee_id: int, principal: Principal = Depends(require("face:enroll")),
                 db: Session = Depends(get_db)):
    removed = attendance_service.remove_faces(db, principal, employee_id)
    db.commit()
    return {"removed": removed}

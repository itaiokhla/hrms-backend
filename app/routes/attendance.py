from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from sqlalchemy.orm import Session

from app.core import authz, storage
from app.core.principal import Principal, get_principal, require
from app.core.rate_limit import client_ip
from app.database.db import get_db
from app.modules.face_recognition import engine as face_engine
from app.schemas.admin import ManualAttendance
from app.services import attendance_service

router = APIRouter(prefix="/api/v1", tags=["Attendance"])


async def _optional_image(file: Optional[UploadFile]) -> Optional[bytes]:
    if file is None:
        return None
    data, _ = await storage.read_image_upload(file)
    return data


@router.post("/attendance/me/check-in")
async def check_in(request: Request, file: Optional[UploadFile] = File(None),
                   principal: Principal = Depends(require("attendance:mark")), db: Session = Depends(get_db)):
    image = await _optional_image(file)
    result = attendance_service.self_check(db, principal, "check_in", image, client_ip(request))
    db.commit()
    return result


@router.post("/attendance/me/check-out")
async def check_out(request: Request, file: Optional[UploadFile] = File(None),
                    principal: Principal = Depends(require("attendance:mark")), db: Session = Depends(get_db)):
    image = await _optional_image(file)
    result = attendance_service.self_check(db, principal, "check_out", image, client_ip(request))
    db.commit()
    return result


@router.get("/attendance/me/today")
def my_today(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return attendance_service.my_today(db, principal)


@router.get("/attendance/me/history")
def my_history(start_date: Optional[date] = None, end_date: Optional[date] = None,
               principal: Principal = Depends(require("attendance:read")), db: Session = Depends(get_db)):
    employee = authz.own_employee(db, principal)
    end = end_date or date.today()
    start = start_date or end - timedelta(days=30)
    total, items = attendance_service.list_records(db, principal, start, end, employee_id=employee.id, limit=200)
    return {"total": total, "items": items}


@router.post("/attendance/kiosk/scan")
async def kiosk_scan(request: Request, file: UploadFile = File(...),
                     principal: Principal = Depends(require("attendance:kiosk")), db: Session = Depends(get_db)):
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
    return attendance_service.serialize(record)


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

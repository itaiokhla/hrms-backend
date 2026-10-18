from fastapi import APIRouter, Depends, Query, Header, HTTPException
from sqlalchemy.orm import Session
from app.database.db import get_db
from app.schemas import AttendanceResponse
from app.services.attendance_service import AttendanceService
from datetime import date
from app.core.security import get_user_id_from_token


from typing import List

router = APIRouter(prefix="/api/v1", tags=["attendance"])

@router.get("/attendance/recent-activity")
async def recent_activity(
    db: Session = Depends(get_db),
):
    return AttendanceService.get_recent_activity(db)

@router.post("/attendance/check-in", response_model=AttendanceResponse)
async def check_in(
    employee_id: int = Query(...),
    face_match_score: float = Query(None),
    is_manual: bool = Query(False),
    db: Session = Depends(get_db),
):
    return AttendanceService.check_in(db, employee_id, face_match_score, is_manual)


@router.post("/attendance/check-out", response_model=AttendanceResponse)
async def check_out(
    employee_id: int = Query(...),
    db: Session = Depends(get_db),
):
    return AttendanceService.check_out(db, employee_id)


@router.get("/attendance/today", response_model=List[AttendanceResponse])
async def get_today_attendance(
    employee_id: int = Query(None),
    db: Session = Depends(get_db),
):
    return AttendanceService.get_today_attendance(db, employee_id)


@router.get("/attendance/history", response_model=List[AttendanceResponse])
async def get_attendance_history(
    employee_id: int = Query(...),
    start_date: date = Query(...),
    end_date: date = Query(...),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    return AttendanceService.get_attendance_history(db, employee_id, start_date, end_date, skip, limit)


@router.get("/attendance/statistics")
async def get_attendance_statistics(
    employee_id: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    year: int = Query(...),
    db: Session = Depends(get_db),
):
    return AttendanceService.get_attendance_statistics(
        db,
        employee_id,
        month,
        year,
    )


# 👇 YAHAN ADD KARO

@router.get("/dashboard/upcoming-leaves")
def upcoming_leaves(db: Session = Depends(get_db)):
    return AttendanceService.get_upcoming_leaves(db)

@router.get("/dashboard/notifications")
def get_notifications(db: Session = Depends(get_db)):
    return AttendanceService.get_notifications(db)

@router.get("/dashboard")
def get_dashboard(db: Session = Depends(get_db)):
    return AttendanceService.get_dashboard_data(db)


@router.get("/dashboard/attendance-trend")
def get_attendance_trend(db: Session = Depends(get_db)):
    return AttendanceService.get_attendance_trend(db)

@router.get("/dashboard/status-summary")
def get_status_summary(db: Session = Depends(get_db)):
    return AttendanceService.get_status_summary(db)


@router.get("/employee/attendance/today")
def employee_today_attendance(
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    token = authorization.replace("Bearer ", "")

    user_id = get_user_id_from_token(token)

    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="Invalid token"
        )

    return AttendanceService.get_employee_today_attendance(
        db,
        int(user_id),
    )
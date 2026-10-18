from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from app.database.db import get_db
from app.services.leave_service import LeaveService
from app.schemas.leave import LeaveApplyRequest
from datetime import date
from app.models.leave import Leave
from app.core.security import get_current_user
from typing import List
from app.models.employee import Employee

router = APIRouter(prefix="/api/v1", tags=["leaves"])


@router.post("/leaves/apply")
async def apply_leave(
    request: LeaveApplyRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):

    employee = LeaveService.get_employee_by_user_id(
        db,
        current_user.id,
    )

    leave = LeaveService.apply_leave(
        db=db,
        employee_id=employee.id,
        leave_type=request.leave_type,
        start_date=request.start_date,
        end_date=request.end_date,
        reason=request.reason,
    )

    return {
        "success": True,
        "leave": {
            "id": leave.id,
            "leave_type": leave.leave_type,
            "start_date": leave.start_date,
            "end_date": leave.end_date,
            "number_of_days": leave.number_of_days,
            "status": leave.status,
        },
    }

@router.post("/leaves/{leave_id}/approve")
async def approve_leave(
    leave_id: int,
    remarks: str = Query(None),
    db: Session = Depends(get_db),
):
    """Approve leave application"""
    leave = LeaveService.approve_leave(db, leave_id, remarks)

    return {
        "id": leave.id,
        "status": leave.status,
        "approval_date": leave.approval_date.isoformat() if leave.approval_date else None,
        "message": "Leave approved successfully",
    }


@router.post("/leaves/{leave_id}/reject")
async def reject_leave(
    leave_id: int,
    remarks: str = Query(None),
    db: Session = Depends(get_db),
):
    """Reject leave application"""
    leave = LeaveService.reject_leave(db, leave_id, remarks)

    return {
        "id": leave.id,
        "status": leave.status,
        "remarks": leave.remarks,
        "message": "Leave rejected successfully",
    }


@router.get("/leaves/pending")
async def get_pending_leaves(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Get all pending leave applications"""
    return LeaveService.get_pending_leaves(db, skip, limit)


@router.get("/leaves/employee/{employee_id}")
async def get_employee_leaves(
    employee_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Get all leave applications for employee"""
    leaves = LeaveService.get_employee_leaves(db, employee_id, skip, limit)

    return [
        {
            "id": leave.id,
            "leave_type": leave.leave_type,
            "start_date": leave.start_date.isoformat(),
            "end_date": leave.end_date.isoformat(),
            "number_of_days": leave.number_of_days,
            "reason": leave.reason,
            "status": leave.status,
            "created_at": leave.created_at.isoformat(),
        }
        for leave in leaves
    ]


@router.get("/leaves/balance/{employee_id}")
async def get_leave_balance(
    employee_id: int,
    year: int = Query(None),
    db: Session = Depends(get_db),
):
    """Get leave balance summary for employee"""
    summary = LeaveService.get_leave_summary(db, employee_id, year)

    return {
        "employee_id": employee_id,
        "year": year,
        "balance_summary": summary,
    }


@router.get("/leaves/calendar/{employee_id}")
async def get_leave_calendar(
    employee_id: int,
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    db: Session = Depends(get_db),
):
    """Get leave calendar for employee"""
    calendar_data = LeaveService.get_leave_calendar(db, employee_id, year, month)

    return {
        "employee_id": employee_id,
        "year": year,
        "month": month,
        "calendar": calendar_data,
    }


from app.models.employee import Employee

@router.get("/leaves")
async def get_all_leaves(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    status: str = Query(None),
    db: Session = Depends(get_db),
):
    """Get all leave applications"""

    query = (
        db.query(Leave, Employee)
        .join(Employee, Leave.employee_id == Employee.id)
    )

    if status:
        query = query.filter(Leave.status == status)

    results = query.offset(skip).limit(limit).all()

    return [
        {
            "id": leave.id,
            "employee_id": leave.employee_id,

        
            "employee_name": f"{employee.first_name} {employee.last_name}",

            "leave_type": leave.leave_type,
            "start_date": leave.start_date.isoformat(),
            "end_date": leave.end_date.isoformat(),
            "number_of_days": leave.number_of_days,

            
            "reason": leave.reason,

            "status": leave.status,
            "created_at": leave.created_at.isoformat(),
        }
        for leave, employee in results
    ]
@router.get("/leaves/my")
async def get_my_leaves(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):

    employee = LeaveService.get_employee_by_user_id(
        db,
        current_user.id,
    )

    return LeaveService.get_employee_leaves(
        db,
        employee.id,
    )

@router.get("/leaves/my/balance")
async def get_my_leave_balance(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):

    employee = LeaveService.get_employee_by_user_id(
        db,
        current_user.id,
    )

    return LeaveService.get_leave_summary(
        db,
        employee.id,
    )

@router.delete("/leaves/{leave_id}")
async def cancel_leave(
    leave_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):

    employee = LeaveService.get_employee_by_user_id(
        db,
        current_user.id,
    )

    leave = (
        db.query(Leave)
        .filter(
            Leave.id == leave_id,
            Leave.employee_id == employee.id,
        )
        .first()
    )

    if not leave:
        raise HTTPException(
            status_code=404,
            detail="Leave not found",
        )

    if leave.status != "pending":
        raise HTTPException(
            status_code=400,
            detail="Only pending leave can be cancelled",
        )

    db.delete(leave)
    db.commit()

    return {
        "success": True,
        "message": "Leave cancelled successfully",
    }
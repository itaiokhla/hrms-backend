from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.database.db import get_db
from app.services.payroll_service import PayrollService
from app.schemas import PayrollResponse
from decimal import Decimal
from datetime import date
from typing import List
from fastapi import Path
from fastapi import Header, HTTPException
from app.core.security import get_user_id_from_token
from app.models.employee import Employee



router = APIRouter(prefix="/api/v1", tags=["payroll"])


@router.post("/payroll/calculate")
async def calculate_salary(
    employee_id: int = Query(...),
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    overtime_hours: float = Query(0, ge=0),
    bonus: float = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Calculate salary without generating payroll"""
    salary_data = PayrollService.calculate_salary(
        db,
        employee_id,
        year,
        month,
        Decimal(str(overtime_hours)),
        Decimal(str(bonus)),
    )
    return salary_data


@router.post("/payroll/generate")
async def generate_payroll(
    employee_id: int = Query(...),
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    overtime_hours: float = Query(0, ge=0),
    bonus: float = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Generate payroll for an employee"""
    payroll = PayrollService.generate_payroll(
        db,
        employee_id,
        year,
        month,
        Decimal(str(overtime_hours)),
        Decimal(str(bonus)),
    )

    return {
        "id": payroll.id,
        "employee_id": payroll.employee_id,
        "salary_month": payroll.salary_month.isoformat(),
        "gross_salary": float(payroll.gross_salary),
        "net_salary": float(payroll.net_salary),
        "status": payroll.status,
        "created_at": payroll.created_at.isoformat(),
    }


@router.post("/payroll/generate-month")
async def generate_payroll_for_month(
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    db: Session = Depends(get_db),
):
    """Generate payroll for all active employees in a month"""
    from app.models.employee import Employee

    employees = db.query(Employee).filter(Employee.is_active == True).all()

    results = {
        "success": [],
        "failed": [],
        "already_exists": [],
    }

    for emp in employees:
        try:
            payroll = PayrollService.generate_payroll(db, emp.id, year, month)
            results["success"].append(
                {
                    "employee_id": emp.id,
                    "employee_name": f"{emp.first_name} {emp.last_name}",
                    "payroll_id": payroll.id,
                }
            )
        except Exception as e:
            error_msg = str(e.detail) if hasattr(e, "detail") else str(e)
            if "already exists" in error_msg:
                results["already_exists"].append(
                    {
                        "employee_id": emp.id,
                        "employee_name": f"{emp.first_name} {emp.last_name}",
                    }
                )
            else:
                results["failed"].append(
                    {
                        "employee_id": emp.id,
                        "employee_name": f"{emp.first_name} {emp.last_name}",
                        "error": error_msg,
                    }
                )

    return {
        "summary": {
            "total_employees": len(employees),
            "generated": len(results["success"]),
            "failed": len(results["failed"]),
            "already_exists": len(results["already_exists"]),
        },
        "results": results,
    }


@router.get("/payroll/{payroll_id}")
async def get_payroll(payroll_id: int, db: Session = Depends(get_db)):
    """Get payroll details"""
    payroll = PayrollService.get_payroll(db, payroll_id)

    return {
        "id": payroll.id,
        "employee_id": payroll.employee_id,
        "salary_month": payroll.salary_month.isoformat(),
        "basic_salary": float(payroll.basic_salary),
        "hra": float(payroll.house_rent_allowance),
        "da": float(payroll.dearness_allowance),
        "conveyance": float(payroll.conveyance_allowance),
        "other_allowances": float(payroll.other_allowances),
        "gross_salary": float(payroll.gross_salary),
        "overtime_hours": float(payroll.overtime_hours),
        "overtime_amount": float(payroll.overtime_amount),
        "total_deductions": float(payroll.total_deductions),
        "net_salary": float(payroll.net_salary),
        "working_days": payroll.working_days,
        "present_days": payroll.present_days,
        "absent_days": payroll.absent_days,
        "status": payroll.status,
        "created_at": payroll.created_at.isoformat(),
    }


@router.get("/payroll/employee/{employee_id}")
async def get_employee_payroll(
    employee_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Get payroll history for employee"""
    payrolls = PayrollService.get_employee_payroll(db, employee_id, skip, limit)

    return [
        {
            "id": p.id,
            "salary_month": p.salary_month.isoformat(),
            "gross_salary": float(p.gross_salary),
            "net_salary": float(p.net_salary),
            "status": p.status,
        }
        for p in payrolls
    ]


@router.post("/payroll/{payroll_id}/approve")
async def approve_payroll(payroll_id: int, db: Session = Depends(get_db)):
    """Approve payroll"""
    payroll = PayrollService.approve_payroll(db, payroll_id)

    return {
        "id": payroll.id,
        "status": payroll.status,
        "message": "Payroll approved successfully",
    }


@router.post("/payroll/{payroll_id}/process")
async def process_payroll(payroll_id: int, db: Session = Depends(get_db)):
    """Mark payroll as processed"""
    payroll = PayrollService.process_payroll(db, payroll_id)

    return {
        "id": payroll.id,
        "status": payroll.status,
        "payment_date": payroll.payment_date,
        "message": "Payroll processed successfully",
    }


@router.get("/payroll/month/{year}/{month}")
async def get_payroll_by_month(
    year: int,
    month: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Get payroll records for a specific month"""
    from app.models.payroll import Payroll

    payrolls = (
        db.query(Payroll)
        .filter(
            Payroll.salary_month >= date(year, month, 1),
            Payroll.salary_month < (
                date(year + 1, 1, 1)
                if month == 12
                else date(year, month + 1, 1)
            ),
        )
        .offset(skip)
        .limit(limit)
        .all()
    )

    return [
        {
            "id": p.id,
            "employee_id": p.employee_id,
            "gross_salary": float(p.gross_salary),
            "net_salary": float(p.net_salary),
            "status": p.status,
        }
        for p in payrolls
    ]


@router.get("/payroll/records")
async def get_payroll_records(
    year: int = Query(None),
    month: int = Query(None, ge=1, le=12),
    status: str = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Get payroll records with filters (year, month, status)"""
    payrolls = PayrollService.get_payroll_records(
        db, year=year, month=month, status=status, skip=skip, limit=limit
    )

    return [
        {
            "id": p.id,
            "employee_id": p.employee_id,
            "salary_month": p.salary_month.isoformat(),
            "gross_salary": float(p.gross_salary),
            "net_salary": float(p.net_salary),
            "status": p.status,
            "created_at": p.created_at.isoformat(),
        }
        for p in payrolls
    ]


@router.get("/payroll/summary/{year}/{month}")
async def get_payroll_summary(
    year: int,
    month: int = Path(..., ge=1, le=12),
    db: Session = Depends(get_db),
):
    """Get payroll summary statistics for a month"""
    return PayrollService.get_payroll_summary(db, year, month)


@router.get("/payroll/{payroll_id}/slip")
async def get_salary_slip(payroll_id: int, db: Session = Depends(get_db)):
    """Get salary slip data"""
    return PayrollService.generate_salary_slip_data(db, payroll_id)


@router.post("/payroll/process-bulk")
async def process_bulk_payroll(
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    status: str = Query("approved"),
    db: Session = Depends(get_db),
):
    """Process bulk payroll for a month"""
    results = PayrollService.process_bulk_payroll(db, year, month, status)
    
    return {
        "summary": {
            "processed": len(results["success"]),
            "failed": len(results["failed"]),
        },
        "results": results,
    }

@router.get("/employee/payroll")
async def employee_payroll(
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

    employee = (
        db.query(Employee)
        .filter(Employee.user_id == int(user_id))
        .first()
    )

    if not employee:
        raise HTTPException(
            status_code=404,
            detail="Employee not found"
        )

    payrolls = PayrollService.get_employee_payroll(
        db,
        employee.id,
        0,
        12,
    )

    return [
        {
            "id": p.id,
            "salary_month": p.salary_month.strftime("%B %Y"),
            "gross_salary": float(p.gross_salary),
            "net_salary": float(p.net_salary),
            "status": p.status,
        }
        for p in payrolls
    ]
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.principal import Principal, require
from app.database.db import get_db
from app.models.employee import Employee
from app.schemas.admin import SalaryStructureCreate, SalaryStructureUpdate
from app.services import payroll_service
from app.utils.pdf_generator import SalarySlipGenerator

router = APIRouter(prefix="/api/v1", tags=["Payroll"])

_Year = Query(..., ge=2000, le=2100)
_Month = Query(..., ge=1, le=12)


@router.post("/payroll/calculate")
def calculate(employee_id: int, year: int = _Year, month: int = _Month,
              overtime_hours: float = Query(0, ge=0, le=500), bonus: float = Query(0, ge=0),
              principal: Principal = Depends(require("payroll:prepare")), db: Session = Depends(get_db)):
    return payroll_service.calculate(db, principal, employee_id, year, month,
                                     Decimal(str(overtime_hours)), Decimal(str(bonus)))


@router.post("/payroll/generate")
def generate(employee_id: int, year: int = _Year, month: int = _Month,
             overtime_hours: float = Query(0, ge=0, le=500), bonus: float = Query(0, ge=0),
             principal: Principal = Depends(require("payroll:prepare")), db: Session = Depends(get_db)):
    payroll = payroll_service.generate(db, principal, employee_id, year, month,
                                       Decimal(str(overtime_hours)), Decimal(str(bonus)))
    db.commit()
    return payroll_service.serialize(payroll, db.get(Employee, payroll.employee_id))


@router.post("/payroll/generate-month")
def generate_month(year: int = _Year, month: int = _Month,
                   principal: Principal = Depends(require("payroll:prepare")), db: Session = Depends(get_db)):
    result = payroll_service.generate_month(db, principal, year, month)
    db.commit()
    return result


@router.post("/payroll/process-bulk")
def process_bulk(year: int = _Year, month: int = _Month,
                 principal: Principal = Depends(require("payroll:process")), db: Session = Depends(get_db)):
    result = payroll_service.process_bulk(db, principal, year, month)
    db.commit()
    return result


@router.get("/payroll")
def list_payroll(year: Optional[int] = Query(None, ge=2000, le=2100), month: Optional[int] = Query(None, ge=1, le=12),
                 status: Optional[str] = Query(None, pattern=r"^(draft|approved|processed)$"),
                 skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                 principal: Principal = Depends(require("payroll:read")), db: Session = Depends(get_db)):
    total, items = payroll_service.list_records(db, principal, year, month, status, skip, limit)
    return {"total": total, "items": items, "skip": skip, "limit": limit}


@router.get("/payroll/summary/{year}/{month}")
def summary(year: int = Path(..., ge=2000, le=2100), month: int = Path(..., ge=1, le=12),
            principal: Principal = Depends(require("payroll:read")), db: Session = Depends(get_db)):
    return payroll_service.summary(db, principal, year, month)


@router.get("/payroll/my")
def my_payslips(principal: Principal = Depends(require("payroll:read")), db: Session = Depends(get_db)):
    return payroll_service.my_payslips(db, principal)


@router.get("/payroll/{payroll_id}")
def get_payroll(payroll_id: int, principal: Principal = Depends(require("payroll:read")), db: Session = Depends(get_db)):
    payroll = payroll_service.get_visible(db, principal, payroll_id)
    return payroll_service.serialize(payroll, db.get(Employee, payroll.employee_id))


@router.get("/payroll/{payroll_id}/slip")
def get_slip(payroll_id: int, principal: Principal = Depends(require("payroll:read")), db: Session = Depends(get_db)):
    return payroll_service.slip(db, principal, payroll_id)


@router.get("/payroll/{payroll_id}/slip.pdf")
def get_slip_pdf(payroll_id: int, principal: Principal = Depends(require("payroll:read")),
                 db: Session = Depends(get_db)):
    slip = payroll_service.slip(db, principal, payroll_id)
    components, deductions = slip["salary_components"], slip["deductions"]
    pdf = SalarySlipGenerator.generate_pdf(
        {
            "salary_month": slip["salary_month"],
            "basic_salary": components["basic_salary"],
            "hra": components["house_rent_allowance"],
            "da": components["dearness_allowance"],
            "conveyance": components["conveyance_allowance"],
            "other_allowances": components["other_allowances"],
            "overtime_amount": components["overtime_amount"],
            "gross_salary": slip["gross_salary"],
            "absent_deduction": deductions["attendance_deductions"],
            "half_day_deduction": 0,
            "late_penalty": 0,
            "total_deductions": slip["total_deductions"],
            "net_salary": slip["net_salary"],
            "working_days": slip["attendance"]["working_days"],
            "present_days": slip["attendance"]["present_days"],
            "absent_days": slip["attendance"]["absent_days"],
        },
        {
            "name": slip["employee"]["name"],
            "employee_id": slip["employee"]["employee_code"],
            "department": slip["employee"]["department"],
            "position": slip["employee"]["designation"],
        },
    )
    filename = f"payslip-{slip['employee']['employee_code']}-{slip['salary_month'].replace(' ', '-')}.pdf"
    return StreamingResponse(pdf, media_type="application/pdf",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.post("/payroll/{payroll_id}/approve")
def approve(payroll_id: int, principal: Principal = Depends(require("payroll:approve")), db: Session = Depends(get_db)):
    payroll = payroll_service.approve(db, principal, payroll_id)
    db.commit()
    return payroll_service.serialize(payroll, db.get(Employee, payroll.employee_id))


@router.post("/payroll/{payroll_id}/process")
def process(payroll_id: int, principal: Principal = Depends(require("payroll:process")), db: Session = Depends(get_db)):
    payroll = payroll_service.process(db, principal, payroll_id)
    db.commit()
    return payroll_service.serialize(payroll, db.get(Employee, payroll.employee_id))


@router.get("/salary-structures")
def list_structures(employee_id: Optional[int] = None, principal: Principal = Depends(require("compensation:read")),
                    db: Session = Depends(get_db)):
    return payroll_service.list_structures(db, principal, employee_id)


@router.post("/salary-structures")
def create_structure(data: SalaryStructureCreate, principal: Principal = Depends(require("compensation:manage")),
                     db: Session = Depends(get_db)):
    structure = payroll_service.create_structure(db, principal, data)
    db.commit()
    return payroll_service.serialize_structure(structure, db.get(Employee, structure.employee_id))


@router.get("/salary-structures/{structure_id}")
def get_structure(structure_id: int, principal: Principal = Depends(require("compensation:read")),
                  db: Session = Depends(get_db)):
    structure = payroll_service.get_structure(db, principal, structure_id)
    return payroll_service.serialize_structure(structure, db.get(Employee, structure.employee_id))


@router.put("/salary-structures/{structure_id}")
def update_structure(structure_id: int, data: SalaryStructureUpdate,
                     principal: Principal = Depends(require("compensation:manage")), db: Session = Depends(get_db)):
    structure = payroll_service.update_structure(db, principal, structure_id, data)
    db.commit()
    return payroll_service.serialize_structure(structure, db.get(Employee, structure.employee_id))


@router.delete("/salary-structures/{structure_id}")
def delete_structure(structure_id: int, principal: Principal = Depends(require("compensation:manage")),
                     db: Session = Depends(get_db)):
    payroll_service.delete_structure(db, principal, structure_id)
    db.commit()
    return {"ok": True}

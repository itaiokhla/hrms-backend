from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.principal import Principal, require
from app.database.db import get_db
from app.models.employee import Employee
from app.schemas.admin import SalaryStructureCreate, SalaryStructureUpdate
from app.services import document_service, payroll_service

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
    payroll = payroll_service.get_visible(db, principal, payroll_id)
    employee = db.get(Employee, payroll.employee_id)
    data, document = document_service.payslip_pdf(db, principal, payroll)
    month = payroll.salary_month.strftime("%B-%Y")
    filename = f"payslip-{employee.employee_code}-{month}.pdf"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "private, no-store"}
    if document is not None:
        headers["X-Document-Number"] = document.snapshot.get("document_number", "")
        headers["X-Brand-Version"] = document.brand_version or ""
        headers["X-Document-SHA256"] = document.sha256 or ""
    return Response(content=data, media_type="application/pdf", headers=headers)


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

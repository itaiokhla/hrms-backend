from typing import Optional

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.core import authz, storage
from app.core.principal import Principal, get_principal, require
from app.database.db import get_db
from app.schemas.employees import DepartmentWrite, EmployeeCreate, EmployeeUpdate, ProfileUpdate, ShiftWrite
from app.services import employee_service
from app.services.employee_service import FieldAccess, serialize

router = APIRouter(prefix="/api/v1", tags=["Employees"])


@router.get("/employees")
def list_employees(
    q: Optional[str] = Query(None, max_length=100),
    department_id: Optional[int] = None,
    manager_id: Optional[int] = None,
    include_inactive: bool = False,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(require("employee:read")),
    db: Session = Depends(get_db),
):
    total, items = employee_service.list_employees(
        db, principal, q=q, department_id=department_id, manager_id=manager_id,
        include_inactive=include_inactive, skip=skip, limit=limit,
    )
    return {"total": total, "items": items, "skip": skip, "limit": limit}


@router.post("/employees")
def create_employee(data: EmployeeCreate, principal: Principal = Depends(require("employee:create")),
                    db: Session = Depends(get_db)):
    employee = employee_service.create_employee(db, principal, data)
    db.commit()
    return employee_service.get_employee(db, principal, employee.id)


@router.get("/employees/export")
def export_employees(principal: Principal = Depends(require("employee:read", "report:export")),
                     db: Session = Depends(get_db)):
    output = employee_service.export_employees(db, principal)
    db.commit()
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=employees.xlsx"},
    )


@router.get("/employees/me")
def my_profile(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    employee = authz.own_employee(db, principal)
    access = FieldAccess(db, principal, [employee.id])
    access.confidential.add(employee.id)  # your own personal details are always visible to you
    return serialize(employee, access)


@router.put("/employees/me")
def update_my_profile(data: ProfileUpdate, principal: Principal = Depends(require("profile:update")),
                      db: Session = Depends(get_db)):
    employee_service.update_own_profile(db, principal, data.model_dump(exclude_unset=True))
    db.commit()
    return my_profile(principal, db)


@router.post("/employees/me/photo")
async def upload_my_photo(file: UploadFile = File(...), principal: Principal = Depends(require("profile:update")),
                          db: Session = Depends(get_db)):
    employee = authz.own_employee(db, principal)
    data, ext = await storage.read_image_upload(file)
    old = employee.photo_path
    employee.photo_path = storage.save_bytes(principal.company_id, "photos", data, ext)
    db.commit()
    storage.delete(principal.company_id, old)
    return {"has_photo": True}


@router.get("/employees/{employee_id}")
def get_employee(employee_id: int, principal: Principal = Depends(require("employee:read")),
                 db: Session = Depends(get_db)):
    return employee_service.get_employee(db, principal, employee_id)


@router.put("/employees/{employee_id}")
def update_employee(employee_id: int, data: EmployeeUpdate, principal: Principal = Depends(require("employee:update")),
                    db: Session = Depends(get_db)):
    employee_service.update_employee(db, principal, employee_id, data)
    db.commit()
    return employee_service.get_employee(db, principal, employee_id)


@router.post("/employees/{employee_id}/deactivate")
def deactivate_employee(employee_id: int, principal: Principal = Depends(require("employee:deactivate")),
                        db: Session = Depends(get_db)):
    employee_service.deactivate_employee(db, principal, employee_id)
    db.commit()
    return employee_service.get_employee(db, principal, employee_id)


@router.post("/employees/{employee_id}/reactivate")
def reactivate_employee(employee_id: int, principal: Principal = Depends(require("employee:deactivate")),
                        db: Session = Depends(get_db)):
    employee_service.reactivate_employee(db, principal, employee_id)
    db.commit()
    return employee_service.get_employee(db, principal, employee_id)


@router.post("/employees/{employee_id}/photo")
async def upload_employee_photo(employee_id: int, file: UploadFile = File(...),
                                principal: Principal = Depends(require("employee:update")),
                                db: Session = Depends(get_db)):
    employee = authz.ensure_employee_access(db, principal, "employee:update", employee_id)
    data, ext = await storage.read_image_upload(file)
    old = employee.photo_path
    employee.photo_path = storage.save_bytes(principal.company_id, "photos", data, ext)
    db.commit()
    storage.delete(principal.company_id, old)
    return {"has_photo": True}


@router.get("/employees/{employee_id}/photo")
def get_employee_photo(employee_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    if authz.is_self(principal, employee_id):
        employee = authz.own_employee(db, principal)
    else:
        employee = authz.ensure_employee_access(db, principal, "employee:read", employee_id)
    path = storage.resolve(principal.company_id, employee.photo_path)
    return FileResponse(path, media_type=storage.media_type_for(path), headers={"Cache-Control": "private, max-age=300"})


@router.get("/departments")
def list_departments(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return employee_service.list_departments(db, principal)


@router.post("/departments")
def create_department(data: DepartmentWrite, principal: Principal = Depends(require("department:manage")),
                      db: Session = Depends(get_db)):
    department = employee_service.save_department(db, principal, data)
    db.commit()
    return {"id": department.id}


@router.put("/departments/{department_id}")
def update_department(department_id: int, data: DepartmentWrite,
                      principal: Principal = Depends(require("department:manage")), db: Session = Depends(get_db)):
    employee_service.save_department(db, principal, data, department_id)
    db.commit()
    return {"id": department_id}


@router.get("/shifts")
def list_shifts(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    from app.models.employee import Shift

    rows = db.query(Shift).filter(Shift.company_id == principal.company_id).order_by(Shift.name).all()
    return [
        {"id": s.id, "name": s.name, "start_time": s.start_time, "end_time": s.end_time,
         "duration_hours": s.duration_hours, "is_flexible": s.is_flexible, "is_active": s.is_active}
        for s in rows
    ]


@router.post("/shifts")
def create_shift(data: ShiftWrite, principal: Principal = Depends(require("department:manage")),
                 db: Session = Depends(get_db)):
    shift = employee_service.save_shift(db, principal, data)
    db.commit()
    return {"id": shift.id}


@router.put("/shifts/{shift_id}")
def update_shift(shift_id: int, data: ShiftWrite, principal: Principal = Depends(require("department:manage")),
                 db: Session = Depends(get_db)):
    employee_service.save_shift(db, principal, data, shift_id)
    db.commit()
    return {"id": shift_id}

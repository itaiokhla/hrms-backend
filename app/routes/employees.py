from fastapi import APIRouter, Depends, HTTPException, status, Query, UploadFile, File
from sqlalchemy.orm import Session
from app.database.db import get_db
from app.schemas import EmployeeCreate, EmployeeResponse, EmployeeUpdate, DepartmentResponse, ShiftResponse
from app.services.employee_service import EmployeeService
from app.models.employee import Department, Shift
from fastapi.responses import StreamingResponse
from openpyxl import load_workbook
from app.core.security import get_current_user
from typing import List
import os
from pathlib import Path

router = APIRouter(prefix="/api/v1", tags=["employees"])


@router.get("/departments", response_model=List[DepartmentResponse])
async def get_departments(db: Session = Depends(get_db)):
    departments = db.query(Department).filter(Department.is_active == True).all()
    return departments


@router.post("/departments", response_model=DepartmentResponse)
async def create_department(dept_data: DepartmentResponse, db: Session = Depends(get_db)):
    new_dept = Department(**dept_data.model_dump())
    db.add(new_dept)
    db.commit()
    db.refresh(new_dept)
    return new_dept


@router.get("/shifts", response_model=List[ShiftResponse])
async def get_shifts(db: Session = Depends(get_db)):
    shifts = db.query(Shift).filter(Shift.is_active == True).all()
    return shifts


@router.post("/shifts", response_model=ShiftResponse)
async def create_shift(shift_data: ShiftResponse, db: Session = Depends(get_db)):
    new_shift = Shift(**shift_data.model_dump())
    db.add(new_shift)
    db.commit()
    db.refresh(new_shift)
    return new_shift


@router.get("/employees", response_model=List[EmployeeResponse])
async def list_employees(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    department_id: int = Query(None),
    db: Session = Depends(get_db),
):
    employees = EmployeeService.get_employees(db, skip, limit, department_id)
    return employees


@router.post("/employees", response_model=EmployeeResponse)
async def create_employee(employee_data: EmployeeCreate, db: Session = Depends(get_db)):
    employee = EmployeeService.create_employee(db, employee_data,  None)
    return employee


@router.get("/employees/search", response_model=List[EmployeeResponse])
async def search_employees(
    q: str = Query(..., min_length=1),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    employees = EmployeeService.search_employees(db, q, skip, limit)
    return employees

@router.get("/employees/export")
async def export_employees(db: Session = Depends(get_db)):
    file = EmployeeService.export_employees(db)

    return StreamingResponse(
        file,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": "attachment; filename=employees.xlsx"
        },
    )
@router.get("/employees/profile")
async def get_my_profile(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    employee = EmployeeService.get_employee_by_user_id(
        db,
        current_user.id,
    )

    return {
        "id": employee.id,
        "employee_id": employee.employee_id,
        "first_name": employee.first_name,
        "last_name": employee.last_name,
        "email": employee.email,
        "phone": employee.phone,
        "date_of_birth": employee.date_of_birth,
        "gender": employee.gender,
        "address": employee.address,
        "city": employee.city,
        "state": employee.state,
        "postal_code": employee.postal_code,
        "country": employee.country,
        "department": employee.department.name if employee.department else None,
        "shift": employee.shift.name if employee.shift else None,
        "position": employee.position,
        "hire_date": employee.hire_date,
        "photo_url": employee.photo_url,
    }

@router.put("/employees/profile")
async def update_my_profile(
    data: dict,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    employee = EmployeeService.get_employee_by_user_id(
        db,
        current_user.id,
    )

    allowed_fields = [
        "phone",
        "address",
        "city",
        "state",
        "postal_code",
        "country",
    ]

    for field in allowed_fields:
        if field in data:
            setattr(employee, field, data[field])

    db.commit()
    db.refresh(employee)

    return {
        "success": True,
        "message": "Profile updated successfully",
    }

@router.post("/employees/profile/photo")
async def upload_my_photo(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    employee = EmployeeService.get_employee_by_user_id(
        db,
        current_user.id,
    )

    upload_dir = Path("./uploads/photos")
    upload_dir.mkdir(parents=True, exist_ok=True)

    if not file.filename:
        raise HTTPException(
            status_code=400,
            detail="No file provided",
        )

    ext = Path(file.filename).suffix.lower()

    if ext not in [".jpg", ".jpeg", ".png"]:
        raise HTTPException(
            status_code=400,
            detail="Only JPG, JPEG and PNG files are allowed",
        )

    filename = f"employee_{employee.id}{ext}"
    file_path = upload_dir / filename

    with open(file_path, "wb") as buffer:
        buffer.write(await file.read())

    employee.photo_url = f"/uploads/photos/{filename}"

    db.commit()
    db.refresh(employee)

    return {
        "success": True,
        "photo_url": employee.photo_url,
    }

@router.get("/employees/{employee_id}", response_model=EmployeeResponse)
async def get_employee(employee_id: int, db: Session = Depends(get_db)):
    return EmployeeService.get_employee(db, employee_id)


@router.put("/employees/{employee_id}", response_model=EmployeeResponse)
async def update_employee(
    employee_id: int,
    update_data: EmployeeUpdate,
    db: Session = Depends(get_db),
):
    return EmployeeService.update_employee(db, employee_id, update_data)



@router.delete("/employees/{employee_id}")
async def delete_employee(employee_id: int, db: Session = Depends(get_db)):
    db.commit()
    return EmployeeService.delete_employee(db, employee_id)




@router.post("/employees/{employee_id}/photo")
async def upload_employee_photo(
    employee_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    employee = EmployeeService.get_employee(db, employee_id)

    upload_dir = Path("./uploads/photos")
    upload_dir.mkdir(parents=True, exist_ok=True)

    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    file_extension = Path(file.filename).suffix.lower()
    if file_extension not in [".jpg", ".jpeg", ".png"]:
        raise HTTPException(status_code=400, detail="Only JPG, JPEG, and PNG files are allowed")

    file_path = upload_dir / f"employee_{employee_id}{file_extension}"

    with open(file_path, "wb") as f:
        f.write(file.file.read())

    employee.photo_url = str(file_path)
    db.commit()

    return {
        "message": "Photo uploaded successfully",
        "photo_url": str(file_path),
    }


@router.get("/employees/{employee_id}/photo")
async def get_employee_photo(employee_id: int, db: Session = Depends(get_db)):
    employee = EmployeeService.get_employee(db, employee_id)
    if not employee.photo_url:
        raise HTTPException(status_code=404, detail="Photo not found")
    return {"photo_url": employee.photo_url}


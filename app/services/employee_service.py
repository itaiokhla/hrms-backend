from sqlalchemy.orm import Session
from sqlalchemy import and_, or_
from fastapi import HTTPException, status, UploadFile
from app.models.employee import Employee, Department, Shift
from app.models.user import User
from app.core.security import hash_password
from app.schemas import EmployeeCreate, EmployeeUpdate
from datetime import date
from decimal import Decimal
from datetime import datetime
from app.services.audit_log_service import AuditLogService
from io import BytesIO
from openpyxl import Workbook
from fastapi.responses import StreamingResponse
from openpyxl import load_workbook


class EmployeeService:
    @staticmethod
    def create_employee(db: Session, employee_data: EmployeeCreate, user_id: int) -> Employee:
        existing_employee = db.query(Employee).filter(
        Employee.is_active == True,
        or_(
        Employee.email == employee_data.email,
        Employee.employee_id == employee_data.employee_id,
        Employee.user_id == user_id,
        )
    ).first()

        if existing_employee:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Employee with this email or employee_id already exists"
            )

        dept = db.query(Department).filter(Department.id == employee_data.department_id).first()
        if not dept:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Department not found"
            )

        shift = db.query(Shift).filter(Shift.id == employee_data.shift_id).first()
        if not shift:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Shift not found"
            )

        existing_user = db.query(User).filter(
            User.is_active == True,
            or_(User.username == employee_data.username, User.email == employee_data.email)
        ).first()

        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Username or email already in use"
            )

        new_user = User(
            username=employee_data.username,
            email=employee_data.email,
            password_hash=hash_password(employee_data.password),
            role="employee",
            is_active=True,
        )

        db.add(new_user)
        db.flush()

        print("USER CREATED:", new_user.id)

        new_employee = Employee(
            user_id=new_user.id,
            first_name=employee_data.first_name,
            last_name=employee_data.last_name,
            email=employee_data.email,
            phone=employee_data.phone,
            date_of_birth=employee_data.date_of_birth,
            gender=employee_data.gender,
            address=employee_data.address,
            employee_id=employee_data.employee_id,
            department_id=employee_data.department_id,
            shift_id=employee_data.shift_id,
            position=employee_data.position,
            hire_date=employee_data.hire_date,
            base_salary=employee_data.base_salary,
        )

        db.add(new_employee)
        print("EMPLOYEE OBJECT ADDED")

        try:
            db.commit()
            print("COMMIT SUCCESS")
        except Exception as e:
            db.rollback()
            print("COMMIT FAILED:", repr(e))
        

        db.refresh(new_employee)
        print("EMPLOYEE CREATED:", new_employee.id)

        AuditLogService.create_log(
        db=db,
        user_id=user_id,
        module="Employee",
        action="CREATE",
        description=f"Created employee {new_employee.employee_id}",
    )

        return new_employee
    
    @staticmethod
    def get_employee(db: Session, employee_id: int) -> Employee:
        employee = db.query(Employee).filter(Employee.id == employee_id).first()
        if not employee:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Employee not found"
            )
        return employee

    @staticmethod
    def get_employees(db: Session, skip: int = 0, limit: int = 50, department_id: int = None, is_active: bool = True):
        query = db.query(Employee)

        if is_active is not None:
            query = query.filter(Employee.is_active == is_active)

        if department_id:
            query = query.filter(Employee.department_id == department_id)

        return query.offset(skip).limit(limit).all()

    @staticmethod
    def update_employee(db: Session, employee_id: int, update_data: EmployeeUpdate) -> Employee:
        employee = EmployeeService.get_employee(db, employee_id)

        if update_data.email and update_data.email != employee.email:
            existing = db.query(Employee).filter(
                and_(Employee.email == update_data.email, Employee.id != employee_id)
            ).first()
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Email already in use"
                )

        update_dict = update_data.model_dump(exclude_unset=True)
        for key, value in update_dict.items():
            setattr(employee, key, value)

        db.commit()
        db.refresh(employee)

        AuditLogService.create_log(
            db=db,
            user_id=employee.user_id,
            module="Employee",
            action="UPDATE",
            description=f"Updated employee {employee.employee_id}",
        )

        return employee

    @staticmethod
    def delete_employee(db: Session, employee_id: int):
        employee = EmployeeService.get_employee(db, employee_id)

        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

        # Employee deactivate
        employee.is_active = False

        # Unique fields free karo
        employee.email = f"deleted_{timestamp}_{employee.email}"
        employee.employee_id = f"DEL_{timestamp}_{employee.employee_id}"

        # Linked user deactivate + unique fields free karo
        user = db.query(User).filter(User.id == employee.user_id).first()

        if user:
            user.is_active = False
            user.email = f"deleted_{timestamp}_{user.email}"
            user.username = f"deleted_{timestamp}_{user.username}"

        AuditLogService.create_log(
            db=db,
            user_id=employee.user_id,
            module="Employee",
            action="DELETE",
            description=f"Deleted employee {employee.employee_id}",
        )

    @staticmethod
    def search_employees(db: Session, query_str: str, skip: int = 0, limit: int = 50):
        return db.query(Employee).filter(
            or_(
                Employee.first_name.ilike(f"%{query_str}%"),
                Employee.last_name.ilike(f"%{query_str}%"),
                Employee.email.ilike(f"%{query_str}%"),
                Employee.employee_id.ilike(f"%{query_str}%"),
            )
        ).offset(skip).limit(limit).all()

    @staticmethod
    def get_employee_by_user_id(db: Session, user_id: int) -> Employee:
        employee = db.query(Employee).filter(Employee.user_id == user_id).first()
        if not employee:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Employee profile not found"
            )
        return employee

    @staticmethod
    def export_employees(db: Session):
            employees = db.query(Employee).filter(
                Employee.is_active == True
            ).all()

            wb = Workbook()
            ws = wb.active
            ws.title = "Employees"

            ws.append([
                "Employee ID",
                "First Name",
                "Last Name",
                "Email",
                "Phone",
                "Position",
                "Department",
                "Shift",
                "Hire Date",
                "Base Salary"
            ])

            for emp in employees:
                ws.append([
                emp.employee_id,
                emp.first_name,
                emp.last_name,
                emp.email,
                emp.phone,
                emp.position,
                emp.department.name if emp.department else "",
                emp.shift.name if emp.shift else "",
                str(emp.hire_date),
                float(emp.base_salary),
            ])

            output = BytesIO()
            wb.save(output)
            output.seek(0)

            return output

    
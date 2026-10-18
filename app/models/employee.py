from sqlalchemy import Column, String, Date, Integer, Float, Boolean, ForeignKey, Text, Numeric, Index
from sqlalchemy.orm import relationship
from app.models.base import BaseModel


class Department(BaseModel):
    __tablename__ = "departments"

    name = Column(String(100), unique=True, nullable=False, index=True)
    description = Column(Text, nullable=True)
    manager_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)

    employees = relationship("Employee", back_populates="department")

    __table_args__ = (
        Index("idx_name", "name"),
    )


class Shift(BaseModel):
    __tablename__ = "shifts"

    name = Column(String(100), unique=True, nullable=False)
    start_time = Column(String(5), nullable=False)
    end_time = Column(String(5), nullable=False)
    duration_hours = Column(Integer, nullable=True)
    is_flexible = Column(Boolean, default=False)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)

    employees = relationship("Employee", back_populates="shift")
    attendance_records = relationship("Attendance", back_populates="shift")


class Employee(BaseModel):
    __tablename__ = "employees"

    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False, index=True)
    first_name = Column(String(50), nullable=False)
    last_name = Column(String(50), nullable=False)
    email = Column(String(100), unique=True, nullable=False, index=True)
    phone = Column(String(20), nullable=True)
    date_of_birth = Column(Date, nullable=True)
    gender = Column(String(10), nullable=True)
    address = Column(Text, nullable=True)
    city = Column(String(50), nullable=True)
    state = Column(String(50), nullable=True)
    postal_code = Column(String(20), nullable=True)
    country = Column(String(50), nullable=True)
    employee_id = Column(String(50), unique=True, nullable=False, index=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=False, index=True)
    shift_id = Column(Integer, ForeignKey("shifts.id"), nullable=False)
    position = Column(String(100), nullable=True)
    hire_date = Column(Date, nullable=False)
    base_salary = Column(Numeric(12, 2), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    photo_url = Column(String(255), nullable=True)
    profile_complete = Column(Boolean, default=False)

    user = relationship("User", back_populates="employee", foreign_keys=[user_id])
    department = relationship("Department", back_populates="employees", foreign_keys=[department_id])
    shift = relationship("Shift", back_populates="employees", foreign_keys=[shift_id])
    face_encodings = relationship("FaceEncoding", back_populates="employee", cascade="all, delete-orphan")
    attendance_records = relationship("Attendance", back_populates="employee", cascade="all, delete-orphan")
    leaves = relationship("Leave", back_populates="employee", cascade="all, delete-orphan")
    payroll_records = relationship("Payroll", back_populates="employee", cascade="all, delete-orphan")
    salary_structures = relationship("SalaryStructure", back_populates="employee", cascade="all, delete-orphan")

    __table_args__ = (
       Index("idx_emp_employee_id", "employee_id"),
       Index("idx_emp_department_id", "department_id"),
       Index("idx_emp_user_id", "user_id"),
       Index("idx_emp_email", "email"),
    )


class FaceEncoding(BaseModel):
    __tablename__ = "face_encodings"

    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    encoding = Column(String, nullable=False)
    image_url = Column(String(255), nullable=True)
    quality_score = Column(Numeric(5, 2), nullable=True)
    is_primary = Column(Boolean, default=False, index=True)

    employee = relationship("Employee", back_populates="face_encodings", foreign_keys=[employee_id])

    __table_args__ = (
        Index("idx_face_employee_id", "employee_id"),
Index("idx_face_is_primary", "is_primary"),
    )

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.models.base import BaseModel, TenantMixin

PAYROLL_STATUSES = ("draft", "approved", "processed")


class SalaryStructure(TenantMixin, BaseModel):
    __tablename__ = "salary_structure"

    employee_id = Column(Integer, nullable=False, index=True)
    basic_salary = Column(Numeric(12, 2), nullable=False)
    house_rent_allowance = Column(Numeric(12, 2), default=0, nullable=False)
    dearness_allowance = Column(Numeric(12, 2), default=0, nullable=False)
    conveyance_allowance = Column(Numeric(12, 2), default=0, nullable=False)
    other_allowances = Column(Numeric(12, 2), default=0, nullable=False)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    created_by_user_id = Column(Integer, nullable=True)

    employee = relationship(
        "Employee", primaryjoin="SalaryStructure.employee_id == Employee.id", foreign_keys=[employee_id]
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "employee_id"], ["employees.company_id", "employees.id"], name="fk_salary_structure_employee"
        ),
        CheckConstraint("basic_salary >= 0", name="ck_salary_structure_basic"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_salary_structure_window"),
    )


class Payroll(TenantMixin, BaseModel):
    __tablename__ = "payroll"

    employee_id = Column(Integer, nullable=False, index=True)
    salary_month = Column(Date, nullable=False, index=True)
    basic_salary = Column(Numeric(12, 2), nullable=False)
    house_rent_allowance = Column(Numeric(12, 2), default=0, nullable=False)
    dearness_allowance = Column(Numeric(12, 2), default=0, nullable=False)
    conveyance_allowance = Column(Numeric(12, 2), default=0, nullable=False)
    other_allowances = Column(Numeric(12, 2), default=0, nullable=False)
    bonus = Column(Numeric(12, 2), default=0, nullable=False)
    gross_salary = Column(Numeric(12, 2), nullable=False)
    provident_fund = Column(Numeric(12, 2), default=0, nullable=False)
    insurance = Column(Numeric(12, 2), default=0, nullable=False)
    professional_tax = Column(Numeric(12, 2), default=0, nullable=False)
    income_tax = Column(Numeric(12, 2), default=0, nullable=False)
    other_deductions = Column(Numeric(12, 2), default=0, nullable=False)
    total_deductions = Column(Numeric(12, 2), default=0, nullable=False)
    overtime_hours = Column(Numeric(8, 2), default=0, nullable=False)
    overtime_amount = Column(Numeric(12, 2), default=0, nullable=False)
    net_salary = Column(Numeric(12, 2), nullable=False)
    working_days = Column(Integer, default=0, nullable=False)
    present_days = Column(Integer, default=0, nullable=False)
    absent_days = Column(Integer, default=0, nullable=False)
    leave_days = Column(Integer, default=0, nullable=False)
    # Snapshot of the month's canonical attendance hours when the run was generated, finished
    # shifts only (NULL for runs generated before hours were recorded). Informational: pay is
    # unchanged. break_seconds is all recorded lunch time; net = gross - unpaid breaks.
    gross_work_seconds = Column(Integer, nullable=True)
    break_seconds = Column(Integer, nullable=True)
    net_work_seconds = Column(Integer, nullable=True)
    incomplete_sessions = Column(Integer, nullable=True)
    status = Column(String(20), default="draft", nullable=False, index=True)
    remarks = Column(Text, nullable=True)
    # Separation of duties: preparer, approver and payer are recorded and checked.
    created_by_user_id = Column(Integer, nullable=True)
    approved_by_user_id = Column(Integer, nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    processed_by_user_id = Column(Integer, nullable=True)
    processed_at = Column(DateTime(timezone=True), nullable=True)

    employee = relationship("Employee", primaryjoin="Payroll.employee_id == Employee.id", foreign_keys=[employee_id])

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "employee_id"], ["employees.company_id", "employees.id"], name="fk_payroll_employee"
        ),
        UniqueConstraint("employee_id", "salary_month", name="uq_payroll_employee_month"),
        CheckConstraint(f"status IN {PAYROLL_STATUSES}", name="ck_payroll_status"),
        Index("ix_payroll_company_month", "company_id", "salary_month"),
    )

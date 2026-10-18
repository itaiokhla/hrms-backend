from sqlalchemy import Column, String, Date, Integer, Numeric, Boolean, ForeignKey, Text, Index, UniqueConstraint
from sqlalchemy.orm import relationship
from app.models.base import BaseModel


class SalaryStructure(BaseModel):
    __tablename__ = "salary_structure"

    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    basic_salary = Column(Numeric(12, 2), nullable=False)
    house_rent_allowance = Column(Numeric(12, 2), default=0)
    dearness_allowance = Column(Numeric(12, 2), default=0)
    conveyance_allowance = Column(Numeric(12, 2), default=0)
    other_allowances = Column(Numeric(12, 2), default=0)
    effective_from = Column(Date, nullable=False, index=True)
    effective_to = Column(Date, nullable=True)

    employee = relationship("Employee", back_populates="salary_structures", foreign_keys=[employee_id])

    __table_args__ = (
        Index("idx_salary_structure_employee_id", "employee_id"),
Index("idx_salary_structure_effective_from", "effective_from"),
    )


class Payroll(BaseModel):
    __tablename__ = "payroll"

    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    salary_month = Column(Date, nullable=False, index=True)
    basic_salary = Column(Numeric(12, 2), nullable=False)
    house_rent_allowance = Column(Numeric(12, 2), default=0)
    dearness_allowance = Column(Numeric(12, 2), default=0)
    conveyance_allowance = Column(Numeric(12, 2), default=0)
    other_allowances = Column(Numeric(12, 2), default=0)
    gross_salary = Column(Numeric(12, 2), nullable=False)
    provident_fund = Column(Numeric(12, 2), default=0)
    insurance = Column(Numeric(12, 2), default=0)
    professional_tax = Column(Numeric(12, 2), default=0)
    income_tax = Column(Numeric(12, 2), default=0)
    other_deductions = Column(Numeric(12, 2), default=0)
    total_deductions = Column(Numeric(12, 2), default=0)
    overtime_hours = Column(Numeric(8, 2), default=0)
    overtime_amount = Column(Numeric(12, 2), default=0)
    net_salary = Column(Numeric(12, 2), nullable=False)
    working_days = Column(Integer, default=0)
    present_days = Column(Integer, default=0)
    absent_days = Column(Integer, default=0)
    leave_days = Column(Integer, default=0)
    status = Column(String(20), default="draft", index=True)
    payment_date = Column(String, nullable=True)
    remarks = Column(Text, nullable=True)

    employee = relationship("Employee", back_populates="payroll_records", foreign_keys=[employee_id])
    salary_records = relationship("SalaryRecord", back_populates="payroll", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_payroll_employee_id", "employee_id"),
Index("idx_payroll_salary_month", "salary_month"),
Index("idx_payroll_status", "status"),
        UniqueConstraint("employee_id", "salary_month", name="uq_payroll_employee_month"),
    )


class SalaryRecord(BaseModel):
    __tablename__ = "salary_records"

    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    payroll_id = Column(Integer, ForeignKey("payroll.id"), nullable=False)
    net_salary = Column(Numeric(12, 2), nullable=False)
    payment_method = Column(String(50), nullable=True)
    transaction_id = Column(String(100), nullable=True)
    payment_date = Column(String, nullable=True)
    slip_generated = Column(Boolean, default=False)
    slip_url = Column(String(255), nullable=True)

    employee = relationship("Employee", foreign_keys=[employee_id])
    payroll = relationship("Payroll", back_populates="salary_records", foreign_keys=[payroll_id])

    __table_args__ = (
        Index("idx_salary_record_employee_id", "employee_id"),
Index("idx_salary_record_payment_date", "payment_date"),
    )

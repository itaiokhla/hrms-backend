from sqlalchemy import Column, String, Date, DateTime, Integer, Boolean, ForeignKey, Text, Index
from sqlalchemy.orm import relationship
from app.models.base import BaseModel


class Leave(BaseModel):
    __tablename__ = "leaves"

    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    leave_type = Column(String(50), nullable=False)
    start_date = Column(Date, nullable=False, index=True)
    end_date = Column(Date, nullable=False, index=True)
    number_of_days = Column(Integer, nullable=False)
    reason = Column(Text, nullable=True)
    status = Column(String(20), default="pending", index=True)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approval_date = Column(DateTime, nullable=True)
    remarks = Column(Text, nullable=True)

    employee = relationship("Employee", back_populates="leaves", foreign_keys=[employee_id])
    approved_by_user = relationship("User", back_populates="leaves", foreign_keys=[approved_by])

    __table_args__ = (
    Index("idx_leave_employee_id", "employee_id"),
    Index("idx_leave_status", "status"),
    Index("idx_leave_start_date", "start_date"),
    Index("idx_leave_end_date", "end_date"),
)


class LeaveBalance(BaseModel):
    __tablename__ = "leave_balance"

    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    leave_type = Column(String(50), nullable=False)
    year = Column(Integer, nullable=False, index=True)
    total_days = Column(Integer, nullable=False)
    used_days = Column(Integer, default=0)
    remaining_days = Column(Integer, nullable=False)

    __table_args__ = (
    Index("idx_leave_balance_employee_id", "employee_id"),
    Index("idx_leave_balance_year", "year"),
)





class CompanySettings(BaseModel):
    __tablename__ = "company_settings"

    setting_key = Column(String(100), unique=True, nullable=False)
    setting_value = Column(Text, nullable=True)
    data_type = Column(String(20), nullable=True)
    description = Column(Text, nullable=True)
    is_public = Column(Boolean, default=False)

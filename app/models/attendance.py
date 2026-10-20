from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.models.base import BaseModel, TenantMixin

ATTENDANCE_STATUSES = ("present", "absent", "late", "half-day")


class Attendance(TenantMixin, BaseModel):
    __tablename__ = "attendance"

    employee_id = Column(Integer, nullable=False, index=True)
    check_in_time = Column(DateTime(timezone=True), nullable=False)
    check_out_time = Column(DateTime(timezone=True), nullable=True)
    # Calendar day in the company's timezone.
    date = Column(Date, nullable=False, index=True)
    shift_id = Column(Integer, nullable=True)
    face_match_score = Column(Float, nullable=True)
    is_manual = Column(Boolean, default=False, nullable=False)
    source = Column(String(20), nullable=False, default="self")  # self | kiosk | manual
    notes = Column(Text, nullable=True)
    status = Column(String(20), default="present", nullable=False, index=True)
    recorded_by_user_id = Column(Integer, nullable=True)

    employee = relationship("Employee", primaryjoin="Attendance.employee_id == Employee.id", foreign_keys=[employee_id])

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "employee_id"],
            ["employees.company_id", "employees.id"],
            name="fk_attendance_employee",
        ),
        ForeignKeyConstraint(
            ["company_id", "shift_id"], ["shifts.company_id", "shifts.id"], name="fk_attendance_shift"
        ),
        CheckConstraint(f"status IN {ATTENDANCE_STATUSES}", name="ck_attendance_status"),
        CheckConstraint(
            "check_out_time IS NULL OR check_out_time >= check_in_time", name="ck_attendance_times"
        ),
        # One record per person per day, whichever path (self, kiosk, manual) creates it.
        UniqueConstraint("company_id", "employee_id", "date", name="uq_attendance_employee_day"),
        Index("ix_attendance_company_date", "company_id", "date"),
        Index("ix_attendance_employee_date", "employee_id", "date"),
    )

from sqlalchemy import Column, String, DateTime, Date, Integer, Float, Boolean, ForeignKey, Text, Enum, Index, UniqueConstraint
from sqlalchemy.orm import relationship
from app.models.base import BaseModel
from enum import Enum as PyEnum


class AttendanceStatus(str, PyEnum):
    present = "present"
    absent = "absent"
    late = "late"
    half_day = "half-day"


class Attendance(BaseModel):
    __tablename__ = "attendance"

    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    check_in_time = Column(DateTime, nullable=False)
    check_out_time = Column(DateTime, nullable=True)
    date = Column(Date, nullable=False, index=True)
    shift_id = Column(Integer, ForeignKey("shifts.id"), nullable=True)
    face_match_score = Column(Float, nullable=True)
    is_manual = Column(Boolean, default=False)
    notes = Column(Text, nullable=True)
    status = Column(String(20), default="present", nullable=False, index=True)

    employee = relationship("Employee", back_populates="attendance_records", foreign_keys=[employee_id])
    shift = relationship("Shift", back_populates="attendance_records", foreign_keys=[shift_id])

    __table_args__ = (
    Index("idx_attendance_employee_id", "employee_id"),
    Index("idx_attendance_date", "date"),
    Index("idx_attendance_check_in_time", "check_in_time"),
    Index("idx_attendance_status", "status"),
    UniqueConstraint("employee_id", "date", "check_in_time", name="uq_attendance_employee_date_checkin"),
)

from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import date, timedelta

from app.models.employee import Employee
from app.models.attendance import Attendance
from app.models.leave import Leave


class DashboardService:

    @staticmethod
    def get_dashboard_summary(db: Session):

        total_employees = db.query(Employee).count()

        present_today = (
            db.query(Attendance)
            .filter(Attendance.date == date.today())
            .count()
        )

        on_leave = (
            db.query(Leave)
            .filter(
                Leave.status == "Approved",
                Leave.start_date <= date.today(),
                Leave.end_date >= date.today(),
            )
            .count()
        )

        absent_today = max(
            total_employees - present_today - on_leave,
            0,
        )

        attendance_percentage = (
            round((present_today / total_employees) * 100, 2)
            if total_employees
            else 0
        )

        return {
            "total_employees": total_employees,
            "present_today": present_today,
            "on_leave": on_leave,
            "absent_today": absent_today,
            "attendance_percentage": attendance_percentage,
        }
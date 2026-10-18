from sqlalchemy.orm import Session
from sqlalchemy import and_, func
from app.models.attendance import Attendance
from app.models.payroll import Payroll
from app.models.leave import Leave
from app.models.employee import Employee
from app.models.user import User
from datetime import date, datetime, timedelta
from decimal import Decimal
import logging

logger = logging.getLogger(__name__)


class ReportService:
    @staticmethod
    def get_attendance_report(
        db: Session,
        start_date: date,
        end_date: date,
        employee_id: int = None,
        department_id: int = None,
    ):
        """Generate attendance report"""
        query = db.query(Attendance).filter(
            and_(Attendance.date >= start_date, Attendance.date <= end_date)
        )

        if employee_id:
            query = query.filter(Attendance.employee_id == employee_id)

        if department_id:
            query = query.join(Employee).filter(
                Employee.department_id == department_id
            )

        records = query.all()

        summary = {
            "total_present": len([r for r in records if r.status == "present"]),
            "total_absent": len([r for r in records if r.status == "absent"]),
            "total_late": len([r for r in records if r.status == "late"]),
            "total_half_day": len([r for r in records if r.status == "half-day"]),
        }

        return {
            "period": f"{start_date} to {end_date}",
            "total_records": len(records),
            "records": [
                {
                    "employee_id": r.employee_id,
                    "date": r.date.isoformat(),
                    "check_in_time": r.check_in_time.isoformat() if r.check_in_time else None,
                    "check_out_time": r.check_out_time.isoformat() if r.check_out_time else None,
                    "status": r.status,
                    "face_match_score": float(r.face_match_score) if r.face_match_score else None,
                }
                for r in records
            ],
            "summary": summary,
        }

    @staticmethod
    def get_payroll_report(db: Session, year: int, month: int = None, department_id: int = None):
        """Generate payroll report"""
        from datetime import date

        if month:
            start_date = date(year, month, 1)
            if month == 12:
                end_date = date(year + 1, 1, 1)
            else:
                end_date = date(year, month + 1, 1)
        else:
            start_date = date(year, 1, 1)
            end_date = date(year, 12, 31)

        query = db.query(Payroll).filter(
            and_(Payroll.salary_month >= start_date, Payroll.salary_month < end_date)
        )

        if department_id:
            query = query.join(Employee).filter(
                Employee.department_id == department_id
            )

        payrolls = query.all()

        total_gross = sum(float(p.gross_salary) for p in payrolls)
        total_deductions = sum(float(p.total_deductions) for p in payrolls)
        total_net = sum(float(p.net_salary) for p in payrolls)

        summary = {
            "total_gross": total_gross,
            "total_deductions": total_deductions,
            "total_net": total_net,
            "average_salary": total_net / len(payrolls) if payrolls else 0,
        }

        return {
            "period": f"{start_date} to {end_date}",
            "total_records": len(payrolls),
            "records": [
                {
                    "employee_id": p.employee_id,
                    "salary_month": p.salary_month.isoformat(),
                    "basic_salary": float(p.basic_salary),
                    "hra": float(p.house_rent_allowance),
                    "da": float(p.dearness_allowance),
                    "conveyance": float(p.conveyance_allowance),
                    "gross_salary": float(p.gross_salary),
                    "total_deductions": float(p.total_deductions),
                    "net_salary": float(p.net_salary),
                    "status": p.status,
                }
                for p in payrolls
            ],
            "summary": summary,
        }

    @staticmethod
    def get_leave_report(db: Session, year: int, employee_id: int = None):
        """Generate leave report"""
        query = db.query(Leave).filter(
            and_(
                Leave.start_date >= date(year, 1, 1),
                Leave.start_date <= date(year, 12, 31),
            )
        )

        if employee_id:
            query = query.filter(Leave.employee_id == employee_id)

        leaves = query.all()

        summary = {
            "sick_leave": {
                "total": len([l for l in leaves if l.leave_type == "sick"]),
                "approved": len([l for l in leaves if l.leave_type == "sick" and l.status == "approved"]),
            },
            "casual_leave": {
                "total": len([l for l in leaves if l.leave_type == "casual"]),
                "approved": len([l for l in leaves if l.leave_type == "casual" and l.status == "approved"]),
            },
            "earned_leave": {
                "total": len([l for l in leaves if l.leave_type == "earned"]),
                "approved": len([l for l in leaves if l.leave_type == "earned" and l.status == "approved"]),
            },
        }

        return {
            "period": f"{year}",
            "total_records": len(leaves),
            "records": [
                {
                    "employee_id": l.employee_id,
                    "leave_type": l.leave_type,
                    "start_date": l.start_date.isoformat(),
                    "end_date": l.end_date.isoformat(),
                    "days": l.number_of_days,
                    "status": l.status,
                    "reason": l.reason,
                }
                for l in leaves
            ],
            "summary": summary,
        }

    @staticmethod
    def get_dashboard_statistics(db: Session):
        """Get dashboard statistics"""
        today = date.today()

        total_employees = db.query(Employee).filter(Employee.is_active == True).count()

        today_attendance = db.query(Attendance).filter(
            Attendance.date == today
        ).all()

        present_today = len([a for a in today_attendance if a.status == "present"])
        absent_today = len([a for a in today_attendance if a.status == "absent"])

        current_month_payroll = db.query(Payroll).filter(
            and_(
                Payroll.salary_month >= date(today.year, today.month, 1),
                Payroll.salary_month < (
                    date(today.year + 1, 1, 1)
                    if today.month == 12
                    else date(today.year, today.month + 1, 1)
                ),
            )
        ).all()

        payroll_status = {
            "draft": len([p for p in current_month_payroll if p.status == "draft"]),
            "approved": len([p for p in current_month_payroll if p.status == "approved"]),
            "processed": len([p for p in current_month_payroll if p.status == "processed"]),
        }

        departments = db.query(func.count(Employee.id)).group_by(
            Employee.department_id
        ).all()

        return {
            "total_employees": total_employees,
            "today_attendance": {
                "present": present_today,
                "absent": absent_today,
                "total_expected": total_employees,
            },
            "payroll_status": payroll_status,
            "department_count": len(departments),
            "generated_at": datetime.now().isoformat(),
        }

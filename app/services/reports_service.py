from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, func
from fastapi import HTTPException
from app.models.attendance import Attendance
from app.models.employee import Employee
from app.models.payroll import Payroll
from app.models.leave import Leave, LeaveBalance
from app.models.employee import Department
from datetime import date, datetime, timedelta
from decimal import Decimal
import logging

logger = logging.getLogger(__name__)


class ReportsService:
    """Service for generating various reports and analytics"""

    @staticmethod
    def get_attendance_report(
        db: Session,
        start_date: date,
        end_date: date,
        employee_id: int = None,
        department_id: int = None,
    ) -> dict:
        """Generate attendance report for a date range"""
        query = db.query(Attendance).filter(
            and_(
                Attendance.date >= start_date,
                Attendance.date <= end_date,
            )
        )

        if employee_id:
            query = query.filter(Attendance.employee_id == employee_id)

        if department_id:
            query = query.filter(
                Attendance.employee_id.in_(
                    db.query(Employee.id).filter(Employee.department_id == department_id)
                )
            )

        attendance_records = query.order_by(Attendance.date.desc()).all()

        if not attendance_records:
            return {
                "period": f"{start_date} to {end_date}",
                "total_records": 0,
                "records": [],
                "summary": {
                    "total_present": 0,
                    "total_absent": 0,
                    "total_late": 0,
                    "total_half_day": 0,
                },
            }

        records_data = []
        summary = {"total_present": 0, "total_absent": 0, "total_late": 0, "total_half_day": 0}

        for record in attendance_records:
            employee = db.query(Employee).filter(Employee.id == record.employee_id).first()
            records_data.append({
                "employee_id": record.employee_id,
                "employee_name": f"{employee.first_name} {employee.last_name}" if employee else "N/A",
                "date": record.date.isoformat(),
                "check_in_time": record.check_in_time.isoformat() if record.check_in_time else None,
                "check_out_time": record.check_out_time.isoformat() if record.check_out_time else None,
                "status": record.status,
                "face_match_score": float(record.face_match_score) if record.face_match_score else None,
            })

            if record.status == "present":
                summary["total_present"] += 1
            elif record.status == "absent":
                summary["total_absent"] += 1
            elif record.status == "late":
                summary["total_late"] += 1
            elif record.status == "half-day":
                summary["total_half_day"] += 1

        return {
            "period": f"{start_date} to {end_date}",
            "total_records": len(records_data),
            "records": records_data,
            "summary": summary,
        }

    @staticmethod
    def get_payroll_report(
        db: Session,
        year: int,
        month: int = None,
        department_id: int = None,
    ) -> dict:
        """Generate payroll report for a period"""
        payrolls = []
        period_label = ""

        if month:
            start_date = date(year, month, 1)
            end_date = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
            period_label = f"{date(year, month, 1).strftime('%B %Y')}"
        else:
            start_date = date(year, 1, 1)
            end_date = date(year + 1, 1, 1)
            period_label = f"Year {year}"

        query = db.query(Payroll).filter(
            and_(
                Payroll.salary_month >= start_date,
                Payroll.salary_month < end_date,
            )
        )

        if department_id:
            query = query.filter(
                Payroll.employee_id.in_(
                    db.query(Employee.id).filter(Employee.department_id == department_id)
                )
            )

        payrolls = query.order_by(Payroll.salary_month.desc()).all()

        if not payrolls:
            return {
                "period": period_label,
                "total_records": 0,
                "records": [],
                "summary": {
                    "total_gross": 0,
                    "total_deductions": 0,
                    "total_net": 0,
                    "average_salary": 0,
                },
            }

        records_data = []
        total_gross = Decimal("0")
        total_deductions = Decimal("0")
        total_net = Decimal("0")

        for payroll in payrolls:
            employee = db.query(Employee).filter(Employee.id == payroll.employee_id).first()
            records_data.append({
                "employee_id": payroll.employee_id,
                "employee_name": f"{employee.first_name} {employee.last_name}" if employee else "N/A",
                "salary_month": payroll.salary_month.isoformat(),
                "basic_salary": float(payroll.basic_salary),
                "hra": float(payroll.house_rent_allowance),
                "da": float(payroll.dearness_allowance),
                "conveyance": float(payroll.conveyance_allowance),
                "gross_salary": float(payroll.gross_salary),
                "total_deductions": float(payroll.total_deductions),
                "net_salary": float(payroll.net_salary),
                "status": payroll.status,
            })

            total_gross += payroll.gross_salary
            total_deductions += payroll.total_deductions
            total_net += payroll.net_salary

        average_salary = total_net / len(payrolls) if payrolls else Decimal("0")

        return {
            "period": period_label,
            "total_records": len(records_data),
            "records": records_data,
            "summary": {
                "total_gross": float(total_gross),
                "total_deductions": float(total_deductions),
                "total_net": float(total_net),
                "average_salary": float(average_salary),
            },
        }

    @staticmethod
    def get_leave_report(
        db: Session,
        year: int = None,
        employee_id: int = None,
    ) -> dict:
        """Generate leave analytics report"""
        if year is None:
            year = date.today().year

        query = db.query(Leave)

        if employee_id:
            query = query.filter(Leave.employee_id == employee_id)

        leaves = query.all()

        # Get leave balances
        leave_balances = db.query(LeaveBalance).all()

        records_data = []
        leave_summary = {
            "sick_leave": {"applied": 0, "approved": 0, "rejected": 0},
            "casual_leave": {"applied": 0, "approved": 0, "rejected": 0},
            "earned_leave": {"applied": 0, "approved": 0, "rejected": 0},
            "maternity_leave": {"applied": 0, "approved": 0, "rejected": 0},
        }

        for leave in leaves:
            if leave.start_date.year == year or leave.end_date.year == year:
                employee = db.query(Employee).filter(Employee.id == leave.employee_id).first()
                days_taken = (leave.end_date - leave.start_date).days + 1

                records_data.append({
                    "employee_id": leave.employee_id,
                    "employee_name": f"{employee.first_name} {employee.last_name}" if employee else "N/A",
                    "leave_type": leave.leave_type,
                    "start_date": leave.start_date.isoformat(),
                    "end_date": leave.end_date.isoformat(),
                    "days": days_taken,
                    "status": leave.status,
                    "reason": leave.reason,
                })

                leave_type_key = leave.leave_type.lower().replace(" ", "_")
                if leave_type_key in leave_summary:
                    leave_summary[leave_type_key]["applied"] += 1
                    if leave.status == "approved":
                        leave_summary[leave_type_key]["approved"] += 1
                    elif leave.status == "rejected":
                        leave_summary[leave_type_key]["rejected"] += 1

        return {
            "period": f"Year {year}",
            "total_records": len(records_data),
            "records": records_data,
            "summary": leave_summary,
        }

    @staticmethod
    def get_department_analytics(db: Session, year: int = None, month: int = None) -> dict:
        """Generate department-wise analytics"""
        if year is None:
            year = date.today().year

        departments = db.query(Department).all()
        analytics = []

        for dept in departments:
            employees = db.query(Employee).filter(Employee.department_id == dept.id).all()
            employee_ids = [emp.id for emp in employees]

            if not employee_ids:
                continue

            # Attendance stats
            attendance_query = db.query(Attendance).filter(
                Attendance.employee_id.in_(employee_ids)
            )

            if month:
                start_date = date(year, month, 1)
                end_date = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
                attendance_query = attendance_query.filter(
                    and_(
                        Attendance.date >= start_date,
                        Attendance.date < end_date,
                    )
                )

            attendance_records = attendance_query.all()
            present_count = len([a for a in attendance_records if a.status == "present"])
            absent_count = len([a for a in attendance_records if a.status == "absent"])
            late_count = len([a for a in attendance_records if a.status == "late"])

            # Payroll stats
            payroll_query = db.query(Payroll).filter(
                Payroll.employee_id.in_(employee_ids)
            )

            if month:
                payroll_query = payroll_query.filter(
                    and_(
                        Payroll.salary_month >= start_date,
                        Payroll.salary_month < end_date,
                    )
                )

            payrolls = payroll_query.all()
            total_payroll = sum(Decimal(str(p.net_salary)) for p in payrolls)

            analytics.append({
                "department_id": dept.id,
                "department_name": dept.name,
                "total_employees": len(employees),
                "attendance": {
                    "present": present_count,
                    "absent": absent_count,
                    "late": late_count,
                },
                "payroll": {
                    "total_salary": float(total_payroll),
                    "average_salary": float(total_payroll / len(employees)) if employees else 0,
                    "records_count": len(payrolls),
                },
            })

        return {
            "period": f"{date(year, month, 1).strftime('%B %Y')}" if month else f"Year {year}",
            "total_departments": len(analytics),
            "analytics": analytics,
        }

    @staticmethod
    def get_employee_analytics(db: Session, employee_id: int, year: int = None) -> dict:
        """Generate individual employee analytics"""
        if year is None:
            year = date.today().year

        employee = db.query(Employee).filter(Employee.id == employee_id).first()
        if not employee:
            raise HTTPException(status_code=404, detail="Employee not found")

        # Attendance stats
        start_date = date(year, 1, 1)
        end_date = date(year + 1, 1, 1)

        attendance_records = db.query(Attendance).filter(
            and_(
                Attendance.employee_id == employee_id,
                Attendance.date >= start_date,
                Attendance.date < end_date,
            )
        ).all()

        attendance_summary = {
            "present": len([a for a in attendance_records if a.status == "present"]),
            "absent": len([a for a in attendance_records if a.status == "absent"]),
            "late": len([a for a in attendance_records if a.status == "late"]),
            "half_day": len([a for a in attendance_records if a.status == "half-day"]),
            "total": len(attendance_records),
        }

        # Payroll stats
        payrolls = db.query(Payroll).filter(
            and_(
                Payroll.employee_id == employee_id,
                Payroll.salary_month >= start_date,
                Payroll.salary_month < end_date,
            )
        ).all()

        payroll_summary = {
            "total_records": len(payrolls),
            "total_gross": float(sum(Decimal(str(p.gross_salary)) for p in payrolls)),
            "total_net": float(sum(Decimal(str(p.net_salary)) for p in payrolls)),
            "average_salary": float(sum(Decimal(str(p.net_salary)) for p in payrolls) / len(payrolls)) if payrolls else 0,
        }

        # Leave stats
        start_date = date(year, 1, 1)
        end_date = date(year + 1, 1, 1)

        leaves = (
            db.query(Leave)
            .filter(
                Leave.employee_id == employee_id,
                Leave.start_date >= start_date,
                Leave.start_date < end_date,
            )
            .all()
        )

        leave_summary = {
            "total_applied": len(leaves),
            "approved": len([l for l in leaves if l.status == "approved"]),
            "rejected": len([l for l in leaves if l.status == "rejected"]),
            "pending": len([l for l in leaves if l.status == "pending"]),
        }

        return {
            "employee_id": employee.id,
            "employee_name": f"{employee.first_name} {employee.last_name}",
            "department": employee.department.name if employee.department else "N/A",
            "designation": employee.position or "N/A",
            "year": year,
            "attendance": attendance_summary,
            "payroll": payroll_summary,
            "leaves": leave_summary,
        }

    @staticmethod
    def get_dashboard_statistics(db: Session) -> dict:
        """Get dashboard statistics for quick overview"""
        today = date.today()
        month_start = date(today.year, today.month, 1)

        # Total employees
        total_employees = db.query(func.count(Employee.id)).filter(Employee.is_active == True).scalar()

        # Today's attendance
        today_attendance = db.query(Attendance).filter(Attendance.date == today).all()
        today_present = len([a for a in today_attendance if a.status == "present"])
        today_absent = len([a for a in today_attendance if a.status == "absent"])

        # This month's payroll
        current_month_payroll = db.query(Payroll).filter(
            and_(
                Payroll.salary_month >= month_start,
                Payroll.salary_month < (date(today.year + 1, 1, 1) if today.month == 12 else date(today.year, today.month + 1, 1)),
            )
        ).all()

        payroll_status = {
            "draft": len([p for p in current_month_payroll if p.status == "draft"]),
            "approved": len([p for p in current_month_payroll if p.status == "approved"]),
            "processed": len([p for p in current_month_payroll if p.status == "processed"]),
            "paid": len([p for p in current_month_payroll if p.status == "paid"]),
        }

        # Department-wise count
        departments = db.query(Department).all()
        dept_count = {dept.name: len(db.query(Employee).filter(Employee.department_id == dept.id).all()) for dept in departments}

        return {
            "total_employees": total_employees,
            "today_attendance": {
                "present": today_present,
                "absent": today_absent,
                "pending": max(0, total_employees - len(today_attendance)),
            },
            "payroll_status": payroll_status,
            "department_count": dept_count,
            "generated_at": datetime.now().isoformat(),
        }

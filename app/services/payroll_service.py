from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, func
from fastapi import HTTPException, status
from app.models.payroll import Payroll, SalaryStructure, SalaryRecord
from app.models.attendance import Attendance
from app.models.employee import Employee
from datetime import date, datetime
from decimal import Decimal
from calendar import monthrange
from app.services.audit_log_service import AuditLogService
import logging

logger = logging.getLogger(__name__)


class PayrollService:
    @staticmethod
    def get_working_days(year: int, month: int) -> int:
        """Calculate working days (excluding weekends)"""
        _, last_day = monthrange(year, month)
        working_days = 0
        for day in range(1, last_day + 1):
            date_obj = date(year, month, day)
            if date_obj.weekday() < 5:  # Monday=0, Friday=4
                working_days += 1
        return working_days

    @staticmethod
    def get_attendance_data(
        db: Session, employee_id: int, year: int, month: int
    ) -> dict:
        """Get attendance statistics for an employee in a month"""
        from datetime import date

        first_day = date(year, month, 1)
        if month == 12:
            last_day = date(year + 1, 1, 1)
        else:
            last_day = date(year, month + 1, 1)

        attendance_records = db.query(Attendance).filter(
            and_(
                Attendance.employee_id == employee_id,
                Attendance.date >= first_day,
                Attendance.date < last_day,
            )
        ).all()

        present_days = len([a for a in attendance_records if a.status == "present"])
        half_days = len([a for a in attendance_records if a.status == "half-day"])
        late_days = len([a for a in attendance_records if a.status == "late"])
        absent_days = len([a for a in attendance_records if a.status == "absent"])

        return {
            "present_days": present_days,
            "half_days": half_days,
            "late_days": late_days,
            "absent_days": absent_days,
            "total_working_days": len(attendance_records),
        }

    @staticmethod
    def get_salary_structure(db: Session, employee_id: int) -> SalaryStructure:
        """Get active salary structure for employee"""

        structure = (
            db.query(SalaryStructure)
            .filter(
                SalaryStructure.employee_id == employee_id
            )
            .first()
        )

        print("SALARY STRUCTURE =", structure)

        return structure

    @staticmethod
    def calculate_salary(
        db: Session,
        employee_id: int,
        year: int,
        month: int,
        overtime_hours: Decimal = Decimal("0"),
        bonus: Decimal = Decimal("0"),
    ) -> dict:
        """Calculate salary for an employee for a given month"""
        from sqlalchemy import or_

        employee = db.query(Employee).filter(Employee.id == employee_id).first()
        if not employee:
            raise HTTPException(status_code=404, detail="Employee not found")

        salary_structure = PayrollService.get_salary_structure(db, employee_id)
        if not salary_structure:
            raise HTTPException(
                status_code=404, detail="Salary structure not found for employee"
            )

        attendance_data = PayrollService.get_attendance_data(db, employee_id, year, month)
        working_days = PayrollService.get_working_days(year, month)

        basic_salary = Decimal(str(salary_structure.basic_salary))
        hra = Decimal(str(salary_structure.house_rent_allowance or 0))
        da = Decimal(str(salary_structure.dearness_allowance or 0))
        conveyance = Decimal(str(salary_structure.conveyance_allowance or 0))
        other_allowances = Decimal(str(salary_structure.other_allowances or 0))

        gross_salary = basic_salary + hra + da + conveyance + other_allowances

        per_day_salary = gross_salary / Decimal(str(working_days)) if working_days > 0 else Decimal("0")

        absent_deduction = per_day_salary * Decimal(str(attendance_data["absent_days"]))
        half_day_deduction = per_day_salary * Decimal(
            str(attendance_data["half_days"])
        ) * Decimal("0.5")

        late_penalty = Decimal(str(max(0, attendance_data["late_days"]) * 100))

        overtime_amount = (per_day_salary / Decimal("8")) * overtime_hours

        from app.config import settings
        total_deductions = (
            Decimal("0")
            + absent_deduction
            + half_day_deduction
            + late_penalty
        )

        net_salary = gross_salary + overtime_amount + bonus - total_deductions

        return {
            "basic_salary": float(basic_salary),
            "hra": float(hra),
            "da": float(da),
            "conveyance": float(conveyance),
            "other_allowances": float(other_allowances),
            "gross_salary": float(gross_salary),
            "overtime_hours": float(overtime_hours),
            "overtime_amount": float(overtime_amount),
            "bonus": float(bonus),
            "absent_deduction": float(absent_deduction),
            "half_day_deduction": float(half_day_deduction),
            "late_penalty": float(late_penalty),
            "total_deductions": float(total_deductions),
            "net_salary": float(net_salary),
            "working_days": working_days,
            "present_days": attendance_data["present_days"],
            "absent_days": attendance_data["absent_days"],
            "half_days": attendance_data["half_days"],
            "late_days": attendance_data["late_days"],
        }

    @staticmethod
    def generate_payroll(
        db: Session,
        employee_id: int,
        year: int,
        month: int,
        overtime_hours: Decimal = Decimal("0"),
        bonus: Decimal = Decimal("0"),
    ) -> Payroll:
        """Generate payroll for an employee"""
        salary_data = PayrollService.calculate_salary(
            db, employee_id, year, month, overtime_hours, bonus
        )

        salary_month = date(year, month, 1)

        existing_payroll = db.query(Payroll).filter(
            and_(Payroll.employee_id == employee_id, Payroll.salary_month == salary_month)
        ).first()

        if existing_payroll:
            raise HTTPException(
                status_code=400, detail="Payroll already exists for this month"
            )

        payroll = Payroll(
            employee_id=employee_id,
            salary_month=salary_month,
            basic_salary=Decimal(str(salary_data["basic_salary"])),
            house_rent_allowance=Decimal(str(salary_data["hra"])),
            dearness_allowance=Decimal(str(salary_data["da"])),
            conveyance_allowance=Decimal(str(salary_data["conveyance"])),
            other_allowances=Decimal(str(salary_data["other_allowances"])),
            gross_salary=Decimal(str(salary_data["gross_salary"])),
            overtime_hours=Decimal(str(salary_data["overtime_hours"])),
            overtime_amount=Decimal(str(salary_data["overtime_amount"])),
            total_deductions=Decimal(str(salary_data["total_deductions"])),
            net_salary=Decimal(str(salary_data["net_salary"])),
            working_days=salary_data["working_days"],
            present_days=salary_data["present_days"],
            absent_days=salary_data["absent_days"],
            leave_days=salary_data["half_days"],
            status="draft",
        )

        db.add(payroll)
        db.commit()
        db.refresh(payroll)

        employee = db.query(Employee).filter(
            Employee.id == employee_id
        ).first()

        AuditLogService.create_log(
            db=db,
            user_id=employee.user_id,
            username=employee.email,
            module="Payroll",
            action="GENERATE",
            description=f"Generated payroll for {employee.first_name} {employee.last_name} ({month}/{year})",
        )
        return payroll

    @staticmethod
    def get_payroll(db: Session, payroll_id: int) -> Payroll:
        """Get payroll record"""
        payroll = db.query(Payroll).filter(Payroll.id == payroll_id).first()
        if not payroll:
            raise HTTPException(status_code=404, detail="Payroll not found")
        return payroll

    @staticmethod
    def get_employee_payroll(
        db: Session, employee_id: int, skip: int = 0, limit: int = 50
    ):
        """Get all payroll records for employee"""
        return (
            db.query(Payroll)
            .filter(Payroll.employee_id == employee_id)
            .order_by(Payroll.salary_month.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    @staticmethod
    def approve_payroll(db: Session, payroll_id: int) -> Payroll:
        """Approve payroll"""
        payroll = PayrollService.get_payroll(db, payroll_id)
        payroll.status = "approved"
        db.commit()
        db.refresh(payroll)

        employee = db.query(Employee).filter(
            Employee.id == payroll.employee_id
        ).first()

        AuditLogService.create_log(
            db=db,
            user_id=employee.user_id,
            username=employee.email,
            module="Payroll",
            action="APPROVE",
            description=f"Approved payroll of {employee.first_name} {employee.last_name}",
        )
        return payroll

    @staticmethod
    def process_payroll(db: Session, payroll_id: int) -> Payroll:
        """Mark payroll as processed"""
        payroll = PayrollService.get_payroll(db, payroll_id)
        if payroll.status != "approved":
            raise HTTPException(
                status_code=400, detail="Payroll must be approved first"
            )
        payroll.status = "processed"
        payroll.payment_date = str(datetime.now())
        db.commit()
        db.refresh(payroll)

        employee = db.query(Employee).filter(
            Employee.id == payroll.employee_id
        ).first()

        AuditLogService.create_log(
            db=db,
            user_id=employee.user_id,
            username=employee.email,
            module="Payroll",
            action="PAID",
            description=f"Salary processed for {employee.first_name} {employee.last_name}",
        )
        return payroll

    @staticmethod
    def process_bulk_payroll(db: Session, year: int, month: int, status_filter: str = None) -> dict:
        """Process bulk payroll for a month"""
        salary_month = date(year, month, 1)
        
        query = db.query(Payroll).filter(Payroll.salary_month == salary_month)
        if status_filter:
            query = query.filter(Payroll.status == status_filter)
        
        payrolls = query.all()
        
        results = {
            "success": [],
            "failed": [],
        }
        
        for payroll in payrolls:
            try:
                if payroll.status == "approved":
                    payroll.status = "processed"
                    payroll.payment_date = str(datetime.now())
                    db.commit()
                    employee = db.query(Employee).filter(
                        Employee.id == payroll.employee_id
                    ).first()

                    AuditLogService.create_log(
                        db=db,
                        user_id=employee.user_id,
                        username=employee.email,
                        module="Payroll",
                        action="BULK PAID",
                        description=f"Bulk salary processed for {employee.first_name} {employee.last_name}",
                    )
                    results["success"].append({
                        "payroll_id": payroll.id,
                        "employee_id": payroll.employee_id,
                        "status": "processed"
                    })
            except Exception as e:
                results["failed"].append({
                    "payroll_id": payroll.id,
                    "employee_id": payroll.employee_id,
                    "error": str(e)
                })
        
        return results

    @staticmethod
    def get_payroll_records(
        db: Session,
        year: int = None,
        month: int = None,
        status: str = None,
        skip: int = 0,
        limit: int = 50
    ) -> list:
        """Get payroll records with filters"""
        query = db.query(Payroll)
        
        if year and month:
            salary_month = date(year, month, 1)
            next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
            query = query.filter(
                and_(
                    Payroll.salary_month >= salary_month,
                    Payroll.salary_month < next_month
                )
            )
        elif year:
            start_date = date(year, 1, 1)
            end_date = date(year + 1, 1, 1)
            query = query.filter(
                and_(
                    Payroll.salary_month >= start_date,
                    Payroll.salary_month < end_date
                )
            )
        
        if status:
            query = query.filter(Payroll.status == status)
        
        return query.order_by(Payroll.salary_month.desc()).offset(skip).limit(limit).all()

    @staticmethod
    def get_payroll_summary(db: Session, year: int, month: int) -> dict:
        """Get payroll summary/statistics for a month"""
        salary_month = date(year, month, 1)
        next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        
        payrolls = db.query(Payroll).filter(
            and_(
                Payroll.salary_month >= salary_month,
                Payroll.salary_month < next_month
            )
        ).all()
        
        if not payrolls:
            return {
        "month": f"{year}-{month:02d}",
        "total_employees": 0,
        "total_gross_salary": 0,
        "total_deductions": 0,
        "total_net_salary": 0,
        "status_breakdown": {},
        "average_salary": 0,
    }
        
        total_gross = sum(Decimal(str(p.gross_salary)) for p in payrolls)
        total_deductions = sum(Decimal(str(p.total_deductions)) for p in payrolls)
        total_net = sum(Decimal(str(p.net_salary)) for p in payrolls)
        
        status_counts = {}
        for payroll in payrolls:
            status_counts[payroll.status] = status_counts.get(payroll.status, 0) + 1
        
        return {
            "month": f"{year}-{month:02d}",
            "total_employees": len(payrolls),
            "total_gross_salary": float(total_gross),
            "total_deductions": float(total_deductions),
            "total_net_salary": float(total_net),
            "status_breakdown": status_counts,
            "average_salary": float(total_net / len(payrolls)) if payrolls else 0,
        }

    @staticmethod
    def generate_salary_slip_data(db: Session, payroll_id: int) -> dict:
        """Generate salary slip data"""
        payroll = PayrollService.get_payroll(db, payroll_id)
        employee = db.query(Employee).filter(Employee.id == payroll.employee_id).first()
        
        if not employee:
            raise HTTPException(status_code=404, detail="Employee not found")
        
        return {
            "payroll_id": payroll.id,
            "employee": {
                "id": employee.id,
                "name": f"{employee.first_name} {employee.last_name}",
                "employee_id": employee.employee_id,
                "department": employee.department.name if employee.department else "N/A",
                "designation": employee.position or "N/A",
            },
            "salary_month": payroll.salary_month.strftime("%B %Y"),
            "salary_components": {
                "basic_salary": float(payroll.basic_salary),
                "house_rent_allowance": float(payroll.house_rent_allowance),
                "dearness_allowance": float(payroll.dearness_allowance),
                "conveyance_allowance": float(payroll.conveyance_allowance),
                "other_allowances": float(payroll.other_allowances),
                "overtime_amount": float(payroll.overtime_amount),
            },
            "gross_salary": float(payroll.gross_salary),
            "deductions": {
                "provident_fund": float(payroll.provident_fund),
                "insurance": float(payroll.insurance),
                "professional_tax": float(payroll.professional_tax),
                "income_tax": float(payroll.income_tax),
                "other_deductions": float(payroll.other_deductions),
            },
            "total_deductions": float(payroll.total_deductions),
            "net_salary": float(payroll.net_salary),
            "attendance": {
                "working_days": payroll.working_days,
                "present_days": payroll.present_days,
                "absent_days": payroll.absent_days,
                "leave_days": payroll.leave_days,
            },
            "status": payroll.status,
            "generated_date": datetime.now().isoformat(),
        }

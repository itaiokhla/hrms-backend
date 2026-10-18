from sqlalchemy.orm import Session
from sqlalchemy import and_
from fastapi import HTTPException, status
from app.models.leave import Leave, LeaveBalance
from app.models.employee import Employee
from datetime import date, datetime , timedelta
from calendar import monthrange
import logging
from app.services.audit_log_service import AuditLogService

logger = logging.getLogger(__name__)


class LeaveService:
    LEAVE_TYPES = {
        "sick": 12,
        "casual": 10,
        "earned": 15,
        "unpaid": 999,
        "maternity": 90,
        "paternity": 7,
    }

    @staticmethod
    def get_leave_balance(db: Session, employee_id: int, leave_type: str, year: int):
        """Get leave balance for employee"""
        balance = db.query(LeaveBalance).filter(
            and_(
                LeaveBalance.employee_id == employee_id,
                LeaveBalance.leave_type == leave_type,
                LeaveBalance.year == year,
            )
        ).first()

        if not balance:
            total_days = LeaveService.LEAVE_TYPES.get(leave_type, 0)
            balance = LeaveBalance(
                employee_id=employee_id,
                leave_type=leave_type,
                year=year,
                total_days=total_days,
                used_days=0,
                remaining_days=total_days,
            )
            db.add(balance)
            db.commit()
            db.refresh(balance)

        return balance

    @staticmethod
    def apply_leave(
        db: Session,
        employee_id: int,
        leave_type: str,
        start_date: date,
        end_date: date,
        reason: str = None,
    ) -> Leave:
        """Apply for leave"""
        employee = db.query(Employee).filter(Employee.id == employee_id).first()
        print("=" * 50)
        print("Employee ID :", employee_id)
        print("Leave Type  :", leave_type)
        print("Start Date  :", start_date)
        print("End Date    :", end_date)
        print("Today       :", date.today())
        if not employee:
            raise HTTPException(status_code=404, detail="Employee not found")

        if leave_type not in LeaveService.LEAVE_TYPES:
            print("Leave Type:", leave_type)
            print("Available:", LeaveService.LEAVE_TYPES.keys())
            raise HTTPException(
                status_code=400,
                detail=f"Invalid leave type. Available: {', '.join(LeaveService.LEAVE_TYPES.keys())}",
            )

        if start_date > end_date:
            raise HTTPException(status_code=400, detail="Start date must be before end date")

        if start_date < date.today():
            raise HTTPException(status_code=400, detail="Cannot apply for past dates")

        number_of_days = (end_date - start_date).days + 1

        if leave_type != "unpaid":
            balance = LeaveService.get_leave_balance(
                db, employee_id, leave_type, start_date.year
            )
            print("Remaining Balance :", balance.remaining_days)
            print("Requested Days    :", number_of_days)

            if balance.remaining_days < number_of_days:
                raise HTTPException(
                    status_code=400,
                    detail=f"Insufficient {leave_type} leave balance. Available: {balance.remaining_days} days",
                )

        existing_leave = db.query(Leave).filter(
            and_(
                Leave.employee_id == employee_id,
                Leave.start_date <= end_date,
                Leave.end_date >= start_date,
                Leave.status != "rejected",
            )
        ).first()
        if existing_leave:
            print("Existing Leave Found")
            print("ID :", existing_leave.id)
            print("Employee :", existing_leave.employee_id)
            print("Start :", existing_leave.start_date)
            print("End :", existing_leave.end_date)
            print("Status :", existing_leave.status)
        else:
            print("No Existing Leave")
        if existing_leave:
            raise HTTPException(
                status_code=400, detail="Leave already exists for these dates"
            )

        leave_application = Leave(
            employee_id=employee_id,
            leave_type=leave_type,
            start_date=start_date,
            end_date=end_date,
            number_of_days=number_of_days,
            reason=reason,
            status="pending",
        )

        db.add(leave_application)
        db.commit()
        db.refresh(leave_application)

        AuditLogService.create_log(
            db=db,
            user_id=employee.user_id,
            username=employee.email,
            module="Leave",
            action="APPLY",
            description=f"{employee.first_name} {employee.last_name} applied for {leave_type} leave ({number_of_days} day(s))",
        )
        return leave_application

    @staticmethod
    def approve_leave(db: Session, leave_id: int, remarks: str = None) -> Leave:
        """Approve leave application"""
        leave = db.query(Leave).filter(Leave.id == leave_id).first()
        if not leave:
            raise HTTPException(status_code=404, detail="Leave application not found")

        if leave.status != "pending":
            raise HTTPException(
                status_code=400,
                detail=f"Cannot approve leave with status: {leave.status}",
            )

        leave.status = "approved"
        leave.approval_date = datetime.now()
        leave.remarks = remarks

        if leave.leave_type != "unpaid":
            balance = LeaveService.get_leave_balance(
                db, leave.employee_id, leave.leave_type, leave.start_date.year
            )
            balance.used_days += leave.number_of_days
            balance.remaining_days = balance.total_days - balance.used_days

        db.commit()
        db.refresh(leave)

        employee = db.query(Employee).filter(
            Employee.id == leave.employee_id
        ).first()

        AuditLogService.create_log(
            db=db,
            user_id=employee.user_id,
            username=employee.email,
            module="Leave",
            action="APPROVE",
            description=f"Approved {leave.leave_type} leave for {employee.first_name} {employee.last_name}",
        )


        return leave

    @staticmethod
    def reject_leave(db: Session, leave_id: int, remarks: str = None) -> Leave:
        """Reject leave application"""
        leave = db.query(Leave).filter(Leave.id == leave_id).first()
        if not leave:
            raise HTTPException(status_code=404, detail="Leave application not found")

        if leave.status != "pending":
            raise HTTPException(
                status_code=400,
                detail=f"Cannot reject leave with status: {leave.status}",
            )

        leave.status = "rejected"
        leave.remarks = remarks
        db.commit()
        db.refresh(leave)

        employee = db.query(Employee).filter(
            Employee.id == leave.employee_id
        ).first()

        AuditLogService.create_log(
            db=db,
            user_id=employee.user_id,
            username=employee.email,
            module="Leave",
            action="REJECT",
            description=f"Rejected {leave.leave_type} leave for {employee.first_name} {employee.last_name}",
        )


        return leave

    @staticmethod
    def get_employee_leaves(
        db: Session, employee_id: int, skip: int = 0, limit: int = 50
    ):
        """Get all leave applications for employee"""
        return (
            db.query(Leave)
            .filter(Leave.employee_id == employee_id)
            .order_by(Leave.created_at.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    @staticmethod
    def get_pending_leaves(db: Session, skip: int = 0, limit: int = 50):

        records = (
            db.query(Leave, Employee)
            .join(Employee, Leave.employee_id == Employee.id)
            .filter(Leave.status == "pending")
            .order_by(Leave.created_at.asc())
            .offset(skip)
            .limit(limit)
            .all()
        )

        result = []

        for leave, employee in records:
            result.append({
                "id": leave.id,
                "employee_id": employee.id,
                "employee_name": f"{employee.first_name} {employee.last_name}",
                "leave_type": leave.leave_type,
                "start_date": leave.start_date,
                "end_date": leave.end_date,
                "number_of_days": leave.number_of_days,
                "reason": leave.reason,
                "status": leave.status,
                "created_at": leave.created_at,
            })

        return result

    @staticmethod
    def get_leave_summary(db: Session, employee_id: int, year: int = None) -> dict:
        """Get leave summary for employee"""
        if year is None:
            year = date.today().year

        summary = {}
        for leave_type in LeaveService.LEAVE_TYPES.keys():
            balance = LeaveService.get_leave_balance(
                db, employee_id, leave_type, year
            )
            summary[leave_type] = {
                "total": balance.total_days,
                "used": balance.used_days,
                "remaining": balance.remaining_days,
            }

        return summary

    @staticmethod
    def get_leave_calendar(db: Session, employee_id: int, year: int, month: int):
        """Get leaves in a calendar format"""
        first_day = date(year, month, 1)
        if month == 12:
            last_day = date(year + 1, 1, 1)
        else:
            last_day = date(year, month + 1, 1)

        leaves = db.query(Leave).filter(
            and_(
                Leave.employee_id == employee_id,
                Leave.start_date < last_day,
                Leave.end_date >= first_day,
                Leave.status == "approved",
            )
        ).all()

        calendar_data = {}
        for leave in leaves:
            current_date = max(leave.start_date, first_day)
            while current_date < min(leave.end_date + timedelta(days=1), last_day):
                date_str = current_date.isoformat()
                calendar_data[date_str] = {
                    "leave_type": leave.leave_type,
                    "leave_id": leave.id,
                }
                current_date += timedelta(days=1)

        return calendar_data

    @staticmethod
    def get_employee_by_user_id(db: Session, user_id: int):
        employee = (
            db.query(Employee)
            .filter(Employee.user_id == user_id)
            .first()
        )

        if not employee:
            raise HTTPException(
                status_code=404,
                detail="Employee not found"
            )

        return employee
        
        

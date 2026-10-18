from unittest import result

from sqlalchemy.orm import Session
from sqlalchemy import and_, func
from fastapi import HTTPException, status
from app.models.attendance import Attendance
from app.models.employee import Employee
from datetime import datetime, date, timedelta
from app.schemas import AttendanceResponse
from app.models.leave import Leave
from app.models.user import User


class AttendanceService:

    @staticmethod
    def get_status_summary(db: Session):

        today = date.today()

        total_employees = db.query(Employee).count()

        present = (
            db.query(Attendance)
            .filter(Attendance.date == today)
            .count()
        )

        leave = (
            db.query(Leave)
            .filter(
                Leave.status == "approved",
                Leave.start_date <= today,
                Leave.end_date >= today,
            )
            .count()
        )   

        working = (
            db.query(Attendance)
            .filter(
                Attendance.date == today,
                Attendance.check_out_time == None,
            )
            .count()
        )

        absent = max(total_employees - present - leave, 0)

        return {
            "present": present,
            "absent": absent,
            "leave": leave,
            "working": working,
        }

    @staticmethod
    def check_in(db: Session, employee_id: int, face_match_score: float = None, is_manual: bool = False) -> Attendance:
        employee = db.query(Employee).filter(Employee.id == employee_id).first()
        if not employee:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Employee not found"
            )

        today = date.today()

        

        existing_checkin = db.query(Attendance).filter(
            and_(
                Attendance.employee_id == employee_id,
                Attendance.date == today,
                Attendance.check_in_time != None,
            )
        ).first()

        if existing_checkin and existing_checkin.check_out_time is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Employee already checked in today"
            )

        attendance = Attendance(
            employee_id=employee_id,
            check_in_time=datetime.now(),
            date=today,
            shift_id=employee.shift_id,
            face_match_score=face_match_score,
            is_manual=is_manual,
            status="present",
        )

        

        db.add(attendance)
        db.commit()
        db.refresh(attendance)
        return attendance

    @staticmethod
    def check_out(db: Session, employee_id: int) -> Attendance:
        today = date.today()

        attendance = db.query(Attendance).filter(
            and_(
                Attendance.employee_id == employee_id,
                Attendance.date == today,
                Attendance.check_out_time == None,
            )
        ).first()

        if not attendance:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No active check-in found for today"
            )

        attendance.check_out_time = datetime.now()
        db.commit()
        db.refresh(attendance)
        return attendance

    @staticmethod
    def get_today_attendance(db: Session, employee_id: int = None):
        today = date.today()
        query = db.query(Attendance).filter(Attendance.date == today)

        if employee_id:
            query = query.filter(Attendance.employee_id == employee_id)

        return query.all()

    @staticmethod
    def get_employee_today_attendance(db: Session, user_id: int):

        employee = (
            db.query(Employee)
            .filter(
                Employee.user_id == user_id,
                Employee.is_active == True,
            )
            .first()
        )

        if not employee:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Employee not found",
            )

        today = date.today()

        attendance = (
            db.query(Attendance)
            .filter(
                Attendance.employee_id == employee.id,
             Attendance.date == today,
            )
            .first()
        )

        if not attendance:
            return {
                "status": "Absent",
                "check_in": None,
                "check_out": None,
                "working_hours": "00:00:00",
            }

        end_time = attendance.check_out_time or datetime.now()

        working_hours = str(
            end_time - attendance.check_in_time
        ).split(".")[0]

        return {
            "status": (
                "Working"
                if attendance.check_out_time is None
                else "Completed"
            ),
            "check_in": (
                attendance.check_in_time.isoformat()
                if attendance.check_in_time
                else None
            ),
            "check_out": (
                attendance.check_out_time.isoformat()
                if attendance.check_out_time
                else None
            ),
            "working_hours": working_hours,
        }

    @staticmethod
    def get_attendance_history(
        db: Session,
        employee_id: int,
        start_date: date,
        end_date: date,
        skip: int = 0,
        limit: int = 50,
    ):
        return db.query(Attendance).filter(
            and_(
                Attendance.employee_id == employee_id,
                Attendance.date >= start_date,
                Attendance.date <= end_date,
            )
        ).order_by(Attendance.date.desc()).offset(skip).limit(limit).all()

    @staticmethod
    def get_dashboard_data(db: Session):

        

        today = date.today()
        on_leave = (
    db.query(Leave)
    .join(Employee, Leave.employee_id == Employee.id)
    .filter(
        Leave.status == "approved",
        Leave.start_date <= today,
        Leave.end_date >= today,
        Employee.is_active == True
    )
    .count()
)

        total_employees = ( db.query(Employee).filter(Employee.is_active == True).count() )

        present_today = (
            db.query(Attendance)
            .join(Employee, Attendance.employee_id == Employee.id)
            .filter(
                Attendance.date == today,
                Employee.is_active == True
            )
            .count()
        )

        working_now = (
    db.query(Attendance)
    .join(Employee, Attendance.employee_id == Employee.id)
    .filter(
        Attendance.date == today,
        Attendance.check_out_time == None,
        Employee.is_active == True
    )
    .count()
)

        completed_today = (
    db.query(Attendance)
    .join(Employee, Attendance.employee_id == Employee.id)
    .filter(
        Attendance.date == today,
        Attendance.check_out_time != None,
        Employee.is_active == True
    )
    .count()
)
        absent_today = total_employees - present_today - on_leave

        if absent_today < 0:
            absent_today = 0

        attendance_percentage = 0

        if total_employees > 0:
            attendance_percentage = round(
                (present_today / total_employees) * 100,
                2
            )

        attendance_records = (
    db.query(Attendance, Employee)
    .join(Employee, Attendance.employee_id == Employee.id)
    .filter(
        Attendance.date == today,
        Employee.is_active == True
    )
    .all()
)

        employees = []

        for attendance, employee in attendance_records:

            working_hours = None

            if attendance.check_in_time:

                end_time = attendance.check_out_time or datetime.now()

                working_hours = str(
                    end_time - attendance.check_in_time
                ).split(".")[0]

            employees.append({

                "employee_id": employee.id,

                "employee_name":
                f"{employee.first_name} {employee.last_name}",

                "department":
                employee.department.name
                if employee.department else "-",

                "check_in":
                attendance.check_in_time.isoformat()
                if attendance.check_in_time else None,

                "check_out":
                attendance.check_out_time.isoformat()
                if attendance.check_out_time else None,

                "working_hours":
                working_hours,

                "status":
                "Working"
                if attendance.check_out_time is None
                else "Completed"

            })

        return {
                    
                    "total_employees": total_employees,
            "present_today": present_today,
            "absent_today": absent_today,
            "on_leave": on_leave,
            "working_now": working_now,
            "completed_today": completed_today,
            "attendance_percentage": attendance_percentage,
            "employees": employees,
                    
                }


    @staticmethod
    def get_recent_activity(db: Session):

        records = (
        db.query(Attendance, Employee)
        .join(Employee, Attendance.employee_id == Employee.id)
        .order_by(Attendance.created_at.desc())
        .limit(10)
        .all()
    )

        activities = []

        for attendance, employee in records:

            activities.append({
            "employee_name": f"{employee.first_name} {employee.last_name}",
            "check_in": attendance.check_in_time.isoformat() if attendance.check_in_time else None,
            "check_out": attendance.check_out_time.isoformat() if attendance.check_out_time else None,
            "status": "Working" if attendance.check_out_time is None else "Checked Out",
        })

        return activities     
    @staticmethod
    def get_attendance_trend(db: Session):

        today = date.today()

        total_employees = db.query(Employee).count()

        trend = []

        for i in range(6, -1, -1):

            current_day = today - timedelta(days=i)

            present = (
                db.query(Attendance)
                .filter(Attendance.date == current_day)
                .count()
        )

            absent = max(total_employees - present, 0)

            trend.append({
                "date": current_day.strftime("%d %b"),
                "present": present,
                "absent": absent,
        })

        return trend   

    @staticmethod
    def get_notifications(db: Session):

        notifications = []

        # Latest Check-ins / Check-outs
        recent_attendance = (
            db.query(Attendance, Employee)
            .join(Employee, Attendance.employee_id == Employee.id)
            .order_by(Attendance.created_at.desc())
            .limit(5)
            .all()
        )

        for attendance, employee in recent_attendance:

            if attendance.check_out_time:
                notifications.append({
                    "type": "checkout",
                    "message": f"{employee.first_name} {employee.last_name} checked out",
                    "time": attendance.check_out_time.isoformat()
                })
            else:
                notifications.append({
                    "type": "checkin",
                    "message": f"{employee.first_name} {employee.last_name} checked in",
                    "time": attendance.check_in_time.isoformat()
                })

        return notifications

    @staticmethod
    def get_upcoming_leaves(db: Session):

        today = date.today()

        leaves = (
            db.query(Leave, Employee)
            .join(Employee, Leave.employee_id == Employee.id)
            .filter(
                Leave.status == "Approved",
                Leave.start_date >= today
            )
            .order_by(Leave.start_date.asc())
            .limit(5)
            .all()
        )

        result = []

        for leave, employee in leaves:

            result.append({
                "employee_name": f"{employee.first_name} {employee.last_name}",
                "leave_type": leave.leave_type,
                "start_date": leave.start_date.isoformat(),
                "end_date": leave.end_date.isoformat()
            })

        return result

            

        
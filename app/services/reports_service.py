"""Scoped reports and analytics.

Every report intersects the caller's ``report:read`` scope with the scope of
the underlying module permission, and filters in SQL, so counts, totals,
averages and exports can only aggregate records the caller may see.
Payroll figures additionally require company-wide ``payroll:read``.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import extract, func
from sqlalchemy.orm import Session

from app.core import authz, clock
from app.core.errors import bad_request, forbidden
from app.core.principal import Principal
from app.models.attendance import Attendance
from app.models.employee import Department, Employee
from app.models.leave import Leave, LeaveType
from app.models.payroll import Payroll
from app.services import attendance_hours, attendance_service


def _require(principal: Principal, permission: str = "report:read") -> None:
    if not principal.has(permission):
        raise forbidden()


def _period(year: int, month: Optional[int]):
    if month:
        start = date(year, month, 1)
        end = date(year + (month == 12), (month % 12) + 1, 1)
        return start, end, start.strftime("%B %Y")
    return date(year, 1, 1), date(year + 1, 1, 1), f"Year {year}"


def payroll_visible(principal: Principal) -> bool:
    return principal.has_company_scope("payroll:read")


def attendance_report(db: Session, principal: Principal, start: date, end: date,
                      employee_id: Optional[int] = None, department_id: Optional[int] = None) -> dict:
    _require(principal)
    if end < start:
        raise bad_request("End date must be after the start date.")
    if (end - start).days > 366:
        raise bad_request("Choose a range of at most one year.")
    scope = authz.scoped_employee_ids(db, principal, "report:read", "attendance:read")
    query = (
        db.query(Attendance, Employee)
        .join(Employee, Employee.id == Attendance.employee_id)
        .filter(
            Attendance.company_id == principal.company_id,
            Attendance.date >= start,
            Attendance.date <= end,
            scope.clause(Attendance.employee_id),
        )
    )
    if employee_id:
        query = query.filter(Attendance.employee_id == employee_id)
    if department_id:
        query = query.filter(Employee.department_id == department_id)
    rows = query.order_by(Attendance.date.desc(), Employee.first_name).limit(20000).all()
    summary = {"total_present": 0, "total_absent": 0, "total_late": 0, "total_half_day": 0}
    records = []
    # Hours come from the canonical calculator, so the report reconciles with the
    # attendance views and the payroll snapshot to the second.
    now = clock.utcnow().replace(microsecond=0)
    breaks = attendance_service.breaks_by_session(db, [record.id for record, _ in rows])
    session_hours = []
    for record, employee in rows:
        hours = attendance_service.hours_for(record, breaks[record.id], now)
        session_hours.append(hours)
        records.append(
            {
                "employee_id": employee.id,
                "employee_code": employee.employee_code,
                "employee_name": employee.full_name,
                "date": record.date.isoformat(),
                "check_in_time": record.check_in_time.isoformat() if record.check_in_time else None,
                "check_out_time": record.check_out_time.isoformat() if record.check_out_time else None,
                "status": record.status,
                "face_match_score": record.face_match_score,
                "gross_seconds": hours.gross_seconds,
                "lunch_seconds": hours.lunch_seconds,
                "excluded_seconds": hours.excluded_seconds,
                "net_seconds": hours.net_seconds,
                "hours_complete": hours.complete,
                "hours_provisional": hours.provisional,
            }
        )
        key = {"present": "total_present", "absent": "total_absent", "late": "total_late", "half-day": "total_half_day"}.get(record.status)
        if key:
            summary[key] += 1
    totals = attendance_hours.total(session_hours)
    summary.update({
        "total_gross_seconds": totals["gross_seconds"],
        "total_lunch_seconds": totals["lunch_seconds"],
        "total_excluded_seconds": totals["excluded_seconds"],
        "total_net_seconds": totals["net_seconds"],
        "incomplete_sessions": totals["incomplete_sessions"],
        "provisional_sessions": totals["provisional_sessions"],
    })
    return {"period": f"{start} to {end}", "total_records": len(records), "records": records, "summary": summary}


def payroll_report(db: Session, principal: Principal, year: int, month: Optional[int] = None,
                   department_id: Optional[int] = None) -> dict:
    _require(principal)
    if not payroll_visible(principal):
        raise forbidden("Payroll reports require company-wide payroll access.")
    start, end, label = _period(year, month)
    query = (
        db.query(Payroll, Employee)
        .join(Employee, Employee.id == Payroll.employee_id)
        .filter(Payroll.company_id == principal.company_id, Payroll.salary_month >= start, Payroll.salary_month < end)
    )
    if department_id:
        query = query.filter(Employee.department_id == department_id)
    rows = query.order_by(Payroll.salary_month.desc(), Employee.first_name).all()
    totals = {"gross": Decimal("0"), "deductions": Decimal("0"), "net": Decimal("0")}
    records = []
    for payroll, employee in rows:
        records.append(
            {
                "employee_id": employee.id,
                "employee_code": employee.employee_code,
                "employee_name": employee.full_name,
                "salary_month": payroll.salary_month.isoformat(),
                "basic_salary": float(payroll.basic_salary),
                "hra": float(payroll.house_rent_allowance),
                "da": float(payroll.dearness_allowance),
                "conveyance": float(payroll.conveyance_allowance),
                "gross_salary": float(payroll.gross_salary),
                "total_deductions": float(payroll.total_deductions),
                "net_salary": float(payroll.net_salary),
                "status": payroll.status,
            }
        )
        totals["gross"] += payroll.gross_salary
        totals["deductions"] += payroll.total_deductions
        totals["net"] += payroll.net_salary
    return {
        "period": label,
        "total_records": len(records),
        "records": records,
        "summary": {
            "total_gross": float(totals["gross"]),
            "total_deductions": float(totals["deductions"]),
            "total_net": float(totals["net"]),
            "average_salary": float(totals["net"] / len(records)) if records else 0,
        },
    }


def leave_report(db: Session, principal: Principal, year: Optional[int] = None, employee_id: Optional[int] = None) -> dict:
    _require(principal)
    year = year or date.today().year
    scope = authz.scoped_employee_ids(db, principal, "report:read", "leave:read")
    query = (
        db.query(Leave, Employee)
        .join(Employee, Employee.id == Leave.employee_id)
        .filter(
            Leave.company_id == principal.company_id,
            extract("year", Leave.start_date) == year,
            scope.clause(Leave.employee_id),
        )
    )
    if employee_id:
        query = query.filter(Leave.employee_id == employee_id)
    type_names = dict(db.query(LeaveType.code, LeaveType.name).filter(LeaveType.company_id == principal.company_id).all())
    summary: dict = {}
    records = []
    for leave, employee in query.order_by(Leave.start_date.desc()).all():
        records.append(
            {
                "employee_id": employee.id,
                "employee_name": employee.full_name,
                "leave_type": leave.leave_type,
                "leave_type_name": type_names.get(leave.leave_type, leave.leave_type),
                "start_date": leave.start_date.isoformat(),
                "end_date": leave.end_date.isoformat(),
                "days": leave.number_of_days,
                "status": leave.status,
                "reason": leave.reason,
            }
        )
        bucket = summary.setdefault(leave.leave_type, {"applied": 0, "approved": 0, "rejected": 0})
        bucket["applied"] += 1
        if leave.status in ("approved", "rejected"):
            bucket[leave.status] += 1
    return {"period": f"Year {year}", "total_records": len(records), "records": records, "summary": summary}


def department_analytics(db: Session, principal: Principal, year: Optional[int] = None, month: Optional[int] = None) -> dict:
    _require(principal)
    year = year or date.today().year
    start, end, label = _period(year, month)
    people = authz.scoped_employee_ids(db, principal, "report:read", "employee:read")
    attendance_scope = authz.scoped_employee_ids(db, principal, "report:read", "attendance:read")
    show_payroll = payroll_visible(principal)

    headcount = dict(
        db.query(Employee.department_id, func.count(Employee.id))
        .filter(Employee.company_id == principal.company_id, Employee.is_active.is_(True), people.clause(Employee.id))
        .group_by(Employee.department_id)
        .all()
    )
    attendance_rows = (
        db.query(Employee.department_id, Attendance.status, func.count(Attendance.id))
        .join(Employee, Employee.id == Attendance.employee_id)
        .filter(
            Attendance.company_id == principal.company_id,
            Attendance.date >= start,
            Attendance.date < end,
            attendance_scope.clause(Attendance.employee_id),
        )
        .group_by(Employee.department_id, Attendance.status)
        .all()
    )
    attendance: dict = {}
    for dept_id, status, count in attendance_rows:
        attendance.setdefault(dept_id, {})[status] = count
    payroll: dict = {}
    if show_payroll:
        for dept_id, total, count in (
            db.query(Employee.department_id, func.sum(Payroll.net_salary), func.count(Payroll.id))
            .join(Employee, Employee.id == Payroll.employee_id)
            .filter(Payroll.company_id == principal.company_id, Payroll.salary_month >= start, Payroll.salary_month < end)
            .group_by(Employee.department_id)
            .all()
        ):
            payroll[dept_id] = (Decimal(total or 0), count)

    analytics = []
    for dept in db.query(Department).filter(Department.company_id == principal.company_id).order_by(Department.name).all():
        employees = headcount.get(dept.id, 0)
        if not employees:
            continue
        stats = attendance.get(dept.id, {})
        item = {
            "department_id": dept.id,
            "department_name": dept.name,
            "total_employees": employees,
            "attendance": {"present": stats.get("present", 0), "absent": stats.get("absent", 0), "late": stats.get("late", 0)},
        }
        if show_payroll:
            total, count = payroll.get(dept.id, (Decimal("0"), 0))
            item["payroll"] = {
                "total_salary": float(total),
                "average_salary": float(total / employees) if employees else 0,
                "records_count": count,
            }
        analytics.append(item)
    return {"period": label, "total_departments": len(analytics), "analytics": analytics, "payroll_visible": show_payroll}


def employee_analytics(db: Session, principal: Principal, employee_id: int, year: Optional[int] = None) -> dict:
    _require(principal)
    year = year or date.today().year
    employee = authz.ensure_employee_access(db, principal, "employee:read", employee_id)
    start, end = date(year, 1, 1), date(year + 1, 1, 1)
    result = {
        "employee_id": employee.id,
        "employee_name": employee.full_name,
        "department": employee.department.name if employee.department else "N/A",
        "designation": employee.position or "N/A",
        "year": year,
    }
    if authz.can_access_employee(db, principal, "attendance:read", employee.id):
        counts = dict(
            db.query(Attendance.status, func.count(Attendance.id))
            .filter(Attendance.employee_id == employee.id, Attendance.date >= start, Attendance.date < end)
            .group_by(Attendance.status)
            .all()
        )
        result["attendance"] = {
            "present": counts.get("present", 0), "absent": counts.get("absent", 0), "late": counts.get("late", 0),
            "half_day": counts.get("half-day", 0), "total": sum(counts.values()),
        }
    if authz.can_access_employee(db, principal, "leave:read", employee.id):
        counts = dict(
            db.query(Leave.status, func.count(Leave.id))
            .filter(Leave.employee_id == employee.id, Leave.start_date >= start, Leave.start_date < end)
            .group_by(Leave.status)
            .all()
        )
        result["leaves"] = {
            "total_applied": sum(counts.values()), "approved": counts.get("approved", 0),
            "rejected": counts.get("rejected", 0), "pending": counts.get("pending", 0),
        }
    if payroll_visible(principal):
        payrolls = db.query(Payroll).filter(
            Payroll.employee_id == employee.id, Payroll.salary_month >= start, Payroll.salary_month < end
        ).all()
        net = sum((Decimal(p.net_salary) for p in payrolls), Decimal("0"))
        result["payroll"] = {
            "total_records": len(payrolls),
            "total_gross": float(sum((Decimal(p.gross_salary) for p in payrolls), Decimal("0"))),
            "total_net": float(net),
            "average_salary": float(net / len(payrolls)) if payrolls else 0,
        }
    return result


def dashboard_statistics(db: Session, principal: Principal) -> dict:
    _require(principal)
    from app.services.attendance_service import local_today
    from app.models.company import Company

    today = local_today(db.get(Company, principal.company_id))
    people = authz.scoped_employee_ids(db, principal, "report:read", "employee:read")
    attendance_scope = authz.scoped_employee_ids(db, principal, "report:read", "attendance:read")
    total = db.query(func.count(Employee.id)).filter(
        Employee.company_id == principal.company_id, Employee.is_active.is_(True), people.clause(Employee.id)
    ).scalar()
    statuses = dict(
        db.query(Attendance.status, func.count(Attendance.id))
        .filter(Attendance.company_id == principal.company_id, Attendance.date == today, attendance_scope.clause(Attendance.employee_id))
        .group_by(Attendance.status)
        .all()
    )
    marked = sum(statuses.values())
    dept_count = {
        name: count
        for name, count in db.query(Department.name, func.count(Employee.id))
        .join(Employee, Employee.department_id == Department.id)
        .filter(Department.company_id == principal.company_id, Employee.is_active.is_(True), people.clause(Employee.id))
        .group_by(Department.name)
        .all()
    }
    result = {
        "total_employees": total,
        "today_attendance": {
            "present": statuses.get("present", 0) + statuses.get("late", 0),
            "absent": statuses.get("absent", 0),
            "pending": max(0, total - marked),
        },
        "department_count": dept_count,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "payroll_visible": payroll_visible(principal),
    }
    if payroll_visible(principal):
        month_start = date(today.year, today.month, 1)
        result["payroll_status"] = dict(
            db.query(Payroll.status, func.count(Payroll.id))
            .filter(Payroll.company_id == principal.company_id, Payroll.salary_month == month_start)
            .group_by(Payroll.status)
            .all()
        )
    return result

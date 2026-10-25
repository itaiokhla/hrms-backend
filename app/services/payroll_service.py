"""Payroll with explicit permissions and separation of duties.

Reporting hierarchy never grants payroll access: every operation requires a
payroll permission, and company-wide payroll data requires COMPANY scope.
Employees with SELF scope see only their own approved/processed payslips.
"""

from calendar import monthrange
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import List, Optional

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.core import authz
from app.core.clock import company_today
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.core.permission_registry import Scope
from app.core.principal import Principal
from app.core.security import utcnow
from app.models.attendance import Attendance
from app.models.employee import Employee
from app.models.holiday import Holiday
from app.models.payroll import Payroll, SalaryStructure
from app.services import settings_service
from app.services.audit_log_service import AuditLogService
from app.services.notification_service import NotificationService

TWO = Decimal("0.01")
VISIBLE_TO_EMPLOYEE = ("approved", "processed")


def totals_by_currency(rows, fields=("gross_salary", "total_deductions", "net_salary")) -> dict:
    """Per-currency sums. Amounts in different currencies are never added together."""
    result: dict = {}
    for row in rows:
        bucket = result.setdefault(row.currency, {"count": 0, **{f: Decimal("0") for f in fields}})
        bucket["count"] += 1
        for f in fields:
            bucket[f] += Decimal(getattr(row, f) or 0)
    return result


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(TWO, rounding=ROUND_HALF_UP)


def _month_bounds(year: int, month: int):
    if not 1 <= month <= 12:
        raise bad_request("Month must be between 1 and 12.")
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def _require_company_scope(principal: Principal, permission: str) -> None:
    if not principal.has_company_scope(permission):
        raise forbidden()


def working_days(db: Session, company_id: int, employee: Employee, year: int, month: int) -> int:
    first, last = _month_bounds(year, month)
    holidays = {
        h.date
        for h in db.query(Holiday).filter(
            Holiday.company_id == company_id,
            Holiday.date >= first,
            Holiday.date <= last,
            Holiday.is_optional.is_(False),
            or_(Holiday.location.is_(None), Holiday.location == employee.location),
        )
    }
    days = 0
    for day in range(1, last.day + 1):
        current = date(year, month, day)
        if current.weekday() < 5 and current not in holidays:
            days += 1
    return days


def active_structure(db: Session, employee_id: int, year: int, month: int) -> Optional[SalaryStructure]:
    first, last = _month_bounds(year, month)
    return (
        db.query(SalaryStructure)
        .filter(
            SalaryStructure.employee_id == employee_id,
            SalaryStructure.effective_from <= last,
            or_(SalaryStructure.effective_to.is_(None), SalaryStructure.effective_to >= first),
        )
        .order_by(SalaryStructure.effective_from.desc())
        .first()
    )


def calculate(db: Session, principal: Principal, employee_id: int, year: int, month: int,
              overtime_hours: Decimal = Decimal("0"), bonus: Decimal = Decimal("0")) -> dict:
    _require_company_scope(principal, "payroll:prepare")
    employee = authz.ensure_employee_access(db, principal, "employee:read", employee_id)
    structure = active_structure(db, employee.id, year, month)
    if structure is None:
        raise bad_request(f"No salary structure covers {month:02d}/{year} for {employee.full_name}.", "NO_SALARY_STRUCTURE")

    first, last = _month_bounds(year, month)
    statuses = [
        s for (s,) in db.query(Attendance.status).filter(
            Attendance.employee_id == employee.id, Attendance.date >= first, Attendance.date <= last
        )
    ]
    present = statuses.count("present")
    late = statuses.count("late")
    half = statuses.count("half-day")
    absent = statuses.count("absent")
    wdays = working_days(db, principal.company_id, employee, year, month)

    basic, hra = _money(structure.basic_salary), _money(structure.house_rent_allowance)
    da, conv, other = _money(structure.dearness_allowance), _money(structure.conveyance_allowance), _money(structure.other_allowances)
    gross = basic + hra + da + conv + other
    per_day = (gross / Decimal(wdays)) if wdays else Decimal("0")
    absent_deduction = _money(per_day * absent)
    half_day_deduction = _money(per_day * half * Decimal("0.5"))
    late_penalty = _money(Decimal(str(settings.late_penalty_amount)) * late)
    # Same overtime rule as the legacy implementation: hourly rate x hours.
    hours_per_day = Decimal(str(settings.default_work_hours_per_day))
    overtime_amount = _money((per_day / hours_per_day) * _money(overtime_hours)) if hours_per_day else Decimal("0")
    total_deductions = absent_deduction + half_day_deduction + late_penalty
    net = gross + overtime_amount + _money(bonus) - total_deductions
    # Same canonical calculator as attendance views and reports; informational, pay rules are unchanged.
    from app.services.attendance_service import month_hours

    hours = month_hours(db, employee.id, first, last, include_provisional=False)
    return {
        "employee_id": employee.id,
        "employee_name": employee.full_name,
        "basic_salary": float(basic), "hra": float(hra), "da": float(da), "conveyance": float(conv),
        "other_allowances": float(other), "gross_salary": float(gross),
        "overtime_hours": float(_money(overtime_hours)), "overtime_amount": float(overtime_amount),
        "bonus": float(_money(bonus)), "absent_deduction": float(absent_deduction),
        "half_day_deduction": float(half_day_deduction), "late_penalty": float(late_penalty),
        "total_deductions": float(total_deductions), "net_salary": float(max(net, Decimal("0"))),
        "working_days": wdays, "present_days": present + late, "absent_days": absent,
        "half_days": half, "late_days": late,
        "attendance_hours": {
            "gross_seconds": hours["gross_seconds"], "lunch_seconds": hours["lunch_seconds"],
            "excluded_seconds": hours["excluded_seconds"], "net_seconds": hours["net_seconds"],
            "complete_sessions": hours["complete_sessions"], "incomplete_sessions": hours["incomplete_sessions"],
            "provisional_sessions": hours["provisional_sessions"],
        },
    }


def generate(db: Session, principal: Principal, employee_id: int, year: int, month: int,
             overtime_hours: Decimal = Decimal("0"), bonus: Decimal = Decimal("0")) -> Payroll:
    data = calculate(db, principal, employee_id, year, month, overtime_hours, bonus)
    salary_month = date(year, month, 1)
    if db.query(Payroll.id).filter(Payroll.employee_id == employee_id, Payroll.salary_month == salary_month).first():
        raise conflict("Payroll already exists for this month.", "PAYROLL_EXISTS")
    from app.services import org_structure_service

    # Amounts are in the currency of the employee's legal entity at the end of the period.
    entity = org_structure_service.entity_for_employee_on(
        db, principal.company_id, employee_id, _month_bounds(year, month)[1]
    )
    payroll = Payroll(
        company_id=principal.company_id,
        employee_id=employee_id,
        salary_month=salary_month,
        currency=entity.base_currency if entity else (company_currency(db, principal.company_id)),
        legal_entity_id=entity.id if entity else None,
        basic_salary=_money(data["basic_salary"]),
        house_rent_allowance=_money(data["hra"]),
        dearness_allowance=_money(data["da"]),
        conveyance_allowance=_money(data["conveyance"]),
        other_allowances=_money(data["other_allowances"]),
        bonus=_money(data["bonus"]),
        gross_salary=_money(data["gross_salary"]),
        overtime_hours=_money(data["overtime_hours"]),
        overtime_amount=_money(data["overtime_amount"]),
        other_deductions=_money(data["absent_deduction"] + data["half_day_deduction"] + data["late_penalty"]),
        total_deductions=_money(data["total_deductions"]),
        net_salary=_money(data["net_salary"]),
        working_days=data["working_days"],
        present_days=data["present_days"],
        absent_days=data["absent_days"],
        leave_days=data["half_days"],
        gross_work_seconds=data["attendance_hours"]["gross_seconds"],
        break_seconds=data["attendance_hours"]["lunch_seconds"],
        net_work_seconds=data["attendance_hours"]["net_seconds"],
        incomplete_sessions=data["attendance_hours"]["incomplete_sessions"],
        status="draft",
        created_by_user_id=principal.user_id,
    )
    db.add(payroll)
    db.flush()
    AuditLogService.log(
        db, principal, "Payroll", "GENERATE", f"Generated payroll for {data['employee_name']} ({month:02d}/{year})",
        "payroll", payroll.id,
    )
    return payroll


def generate_month(db: Session, principal: Principal, year: int, month: int) -> dict:
    _require_company_scope(principal, "payroll:prepare")
    employees = (
        db.query(Employee)
        .filter(Employee.company_id == principal.company_id, Employee.is_active.is_(True))
        .order_by(Employee.employee_code)
        .all()
    )
    results = {"success": [], "failed": [], "already_exists": []}
    for emp in employees:
        try:
            with db.begin_nested():
                payroll = generate(db, principal, emp.id, year, month)
            results["success"].append({"employee_id": emp.id, "employee_name": emp.full_name, "payroll_id": payroll.id})
        except Exception as exc:  # per-employee isolation; AppError carries a user-safe message
            detail = getattr(exc, "detail", "Could not generate payroll")
            bucket = "already_exists" if getattr(exc, "code", "") == "PAYROLL_EXISTS" else "failed"
            results[bucket].append({"employee_id": emp.id, "employee_name": emp.full_name, "error": str(detail)})
    return {
        "summary": {
            "total_employees": len(employees),
            "generated": len(results["success"]),
            "failed": len(results["failed"]),
            "already_exists": len(results["already_exists"]),
        },
        "results": results,
    }


def _get(db: Session, principal: Principal, payroll_id: int, lock: bool = False) -> Payroll:
    query = db.query(Payroll).filter(Payroll.company_id == principal.company_id, Payroll.id == payroll_id)
    if lock:
        query = query.with_for_update()
    payroll = query.first()
    if payroll is None:
        raise not_found("Payroll record")
    return payroll


def get_visible(db: Session, principal: Principal, payroll_id: int) -> Payroll:
    """Payslip access: COMPANY scope sees all; SELF sees own approved/processed only."""
    payroll = _get(db, principal, payroll_id)
    if principal.has_company_scope("payroll:read"):
        return payroll
    if (
        principal.has("payroll:read", Scope.SELF.value)
        and payroll.employee_id == principal.employee_id
        and payroll.status in VISIBLE_TO_EMPLOYEE
    ):
        return payroll
    raise not_found("Payroll record")


def approve(db: Session, principal: Principal, payroll_id: int) -> Payroll:
    _require_company_scope(principal, "payroll:approve")
    payroll = _get(db, principal, payroll_id, lock=True)
    if payroll.status != "draft":
        raise bad_request(f"Only draft payroll can be approved (current: {payroll.status}).")
    if payroll.employee_id == principal.employee_id:
        raise forbidden("You cannot approve your own pay.")
    if (
        settings_service.get_value(db, principal.company_id, "payrollSeparationOfDuties")
        and payroll.created_by_user_id == principal.user_id
    ):
        raise forbidden("Separation of duties: payroll must be approved by someone other than its preparer.", )
    payroll.status = "approved"
    payroll.approved_by_user_id = principal.user_id
    payroll.approved_at = utcnow()
    db.flush()
    from app.services import document_service

    # The payslip is issued now and keeps today's branding forever (EC-WL-09).
    document = document_service.issue_payslip(db, payroll, principal.user_id)
    AuditLogService.log(db, principal, "Payroll", "APPROVE",
                        f"Approved payroll #{payroll.id}; payslip {document.snapshot['document_number']} issued",
                        "payroll", payroll.id)
    return payroll


def _process_one(db: Session, principal: Principal, payroll: Payroll) -> None:
    if payroll.status != "approved":
        raise bad_request("Payroll must be approved before it is processed.")
    if payroll.employee_id == principal.employee_id:
        raise forbidden("You cannot process your own pay.")
    payroll.status = "processed"
    payroll.processed_by_user_id = principal.user_id
    payroll.processed_at = utcnow()
    employee = db.get(Employee, payroll.employee_id)
    if employee and employee.user_id:
        NotificationService.create(
            db, employee.user_id, "Salary processed",
            f"Your salary for {payroll.salary_month.strftime('%B %Y')} has been processed.", "payroll", "/employee/salary",
        )
    if employee and employee.email:
        _queue_payslip_email(db, payroll, employee)
    AuditLogService.log(db, principal, "Payroll", "PROCESS", f"Processed payroll #{payroll.id}", "payroll", payroll.id)


def _queue_payslip_email(db: Session, payroll: Payroll, employee: Employee) -> None:
    """Branded 'payslip published' email after commit. It never contains amounts (01 §5.6)."""
    from sqlalchemy import event

    from app.services.email_branding import link
    from app.services.email_service import EmailService

    context = {
        "recipient": {"first_name": employee.first_name, "name": employee.full_name, "email": employee.email},
        "period": {"label": payroll.salary_month.strftime("%B %Y")},
        "link": {"url": link(payroll.company_id, "/payslips")},
    }

    # Sent once, after the outermost commit only. SQLAlchemy 2.0 also emits after_commit when a SAVEPOINT is
    # released inside a still-open transaction, and an outer rollback must cancel the e-mail rather than leave it
    # armed for the session's next commit (F-72 (2)). Rolling back the savepoint it was queued in cancels it too.
    state = {"done": False}
    queued_in = db.get_nested_transaction() or db.get_transaction()

    def send(session):
        if state["done"] or session.in_nested_transaction():
            return
        state["done"] = True
        EmailService.send_template("payroll.payslip_published", employee.email, context, payroll.company_id)

    def discard(_session, previous_transaction):
        tx = queued_in
        while tx is not None:  # the transaction it was queued in, or one enclosing it, rolled back
            if tx is previous_transaction:
                state["done"] = True
                return
            tx = tx.parent
        if previous_transaction.parent is None:
            state["done"] = True

    event.listen(db, "after_commit", send)
    event.listen(db, "after_soft_rollback", discard)


def process(db: Session, principal: Principal, payroll_id: int) -> Payroll:
    _require_company_scope(principal, "payroll:process")
    payroll = _get(db, principal, payroll_id, lock=True)
    _process_one(db, principal, payroll)
    return payroll


def process_bulk(db: Session, principal: Principal, year: int, month: int) -> dict:
    _require_company_scope(principal, "payroll:process")
    first, last = _month_bounds(year, month)
    payrolls = (
        db.query(Payroll)
        .filter(Payroll.company_id == principal.company_id, Payroll.salary_month == first, Payroll.status == "approved")
        .with_for_update()
        .all()
    )
    results = {"success": [], "failed": []}
    for payroll in payrolls:
        try:
            _process_one(db, principal, payroll)
            results["success"].append({"payroll_id": payroll.id, "employee_id": payroll.employee_id})
        except Exception as exc:
            results["failed"].append({"payroll_id": payroll.id, "employee_id": payroll.employee_id,
                                      "error": str(getattr(exc, "detail", "Failed"))})
    return {"summary": {"processed": len(results["success"]), "failed": len(results["failed"])}, "results": results}


def serialize(payroll: Payroll, employee: Optional[Employee] = None) -> dict:
    return {
        "id": payroll.id,
        "employee_id": payroll.employee_id,
        "employee_name": employee.full_name if employee else None,
        "employee_code": employee.employee_code if employee else None,
        "salary_month": payroll.salary_month.isoformat(),
        "basic_salary": float(payroll.basic_salary),
        "gross_salary": float(payroll.gross_salary),
        "total_deductions": float(payroll.total_deductions),
        "net_salary": float(payroll.net_salary),
        "status": payroll.status,
        "currency": payroll.currency,
        "legal_entity_id": payroll.legal_entity_id,
        "net_work_seconds": payroll.net_work_seconds,
        "created_by_user_id": payroll.created_by_user_id,
        "approved_by_user_id": payroll.approved_by_user_id,
        "created_at": payroll.created_at.isoformat(),
    }


def list_records(db: Session, principal: Principal, year: Optional[int], month: Optional[int],
                 status: Optional[str], skip: int, limit: int):
    _require_company_scope(principal, "payroll:read")
    query = (
        db.query(Payroll)
        .options(joinedload(Payroll.employee))
        .filter(Payroll.company_id == principal.company_id)
    )
    if year and month:
        first, last = _month_bounds(year, month)
        query = query.filter(Payroll.salary_month == first)
    elif year:
        query = query.filter(and_(Payroll.salary_month >= date(year, 1, 1), Payroll.salary_month <= date(year, 12, 31)))
    if status:
        query = query.filter(Payroll.status == status)
    total = query.order_by(None).count()
    rows = query.order_by(Payroll.salary_month.desc(), Payroll.id.desc()).offset(skip).limit(limit).all()
    return total, [serialize(p, p.employee) for p in rows]


def summary(db: Session, principal: Principal, year: int, month: int) -> dict:
    _require_company_scope(principal, "payroll:read")
    first, _ = _month_bounds(year, month)
    rows = db.query(Payroll).filter(Payroll.company_id == principal.company_id, Payroll.salary_month == first).all()
    breakdown = {}
    for p in rows:
        breakdown[p.status] = breakdown.get(p.status, 0) + 1
    by_currency = totals_by_currency(rows)
    single = next(iter(by_currency.values())) if len(by_currency) == 1 else None
    return {
        "month": f"{year}-{month:02d}",
        "total_employees": len(rows),
        # Company-wide totals exist only when every row is in one currency (no FX conversion).
        "currency": next(iter(by_currency)) if single else (None if by_currency else company_currency(db, principal.company_id)),
        "mixed_currency": len(by_currency) > 1,
        "total_gross_salary": float(single["gross_salary"]) if single else (None if by_currency else 0),
        "total_deductions": float(single["total_deductions"]) if single else (None if by_currency else 0),
        "total_net_salary": float(single["net_salary"]) if single else (None if by_currency else 0),
        "average_salary": float(single["net_salary"] / single["count"]) if single else (None if by_currency else 0),
        "by_currency": {
            code: {"employees": b["count"], "gross": float(b["gross_salary"]), "deductions": float(b["total_deductions"]),
                   "net": float(b["net_salary"])}
            for code, b in sorted(by_currency.items())
        },
        "status_breakdown": breakdown,
    }


def company_currency(db: Session, company_id: int) -> str:
    from app.models.company import Company

    company = db.get(Company, company_id)
    return company.currency if company else "INR"


def my_payslips(db: Session, principal: Principal) -> List[dict]:
    if not principal.has("payroll:read"):
        raise forbidden()
    employee = authz.own_employee(db, principal)
    rows = (
        db.query(Payroll)
        .filter(Payroll.employee_id == employee.id, Payroll.status.in_(VISIBLE_TO_EMPLOYEE))
        .order_by(Payroll.salary_month.desc())
        .limit(36)
        .all()
    )
    return [serialize(p, employee) for p in rows]


def slip(db: Session, principal: Principal, payroll_id: int) -> dict:
    payroll = get_visible(db, principal, payroll_id)
    employee = db.get(Employee, payroll.employee_id)
    return {
        "payroll_id": payroll.id,
        "employee": {
            "id": employee.id,
            "name": employee.full_name,
            "employee_code": employee.employee_code,
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
            "bonus": float(payroll.bonus),
        },
        "gross_salary": float(payroll.gross_salary),
        "deductions": {
            "provident_fund": float(payroll.provident_fund),
            "insurance": float(payroll.insurance),
            "professional_tax": float(payroll.professional_tax),
            "income_tax": float(payroll.income_tax),
            "attendance_deductions": float(payroll.other_deductions),
        },
        "total_deductions": float(payroll.total_deductions),
        "net_salary": float(payroll.net_salary),
        "currency": payroll.currency,
        "employer": _employer(db, payroll),
        "attendance": {
            "working_days": payroll.working_days,
            "present_days": payroll.present_days,
            "absent_days": payroll.absent_days,
            "half_days": payroll.leave_days,
            # Hours as recorded when this run was generated (None for older runs).
            "gross_work_seconds": payroll.gross_work_seconds,
            "net_work_seconds": payroll.net_work_seconds,
            "break_seconds": payroll.break_seconds,
            "incomplete_sessions": payroll.incomplete_sessions,
        },
        "status": payroll.status,
    }


def _employer(db: Session, payroll: Payroll) -> Optional[dict]:
    from app.models.org import LegalEntity

    entity = db.get(LegalEntity, payroll.legal_entity_id) if payroll.legal_entity_id else None
    if entity is None:
        return None
    return {"legal_name": entity.legal_name, "code": entity.code, "country_code": entity.country_code,
            "address": entity.registered_address or {}, "registration_lines": entity.registration_lines or []}


# ---------------------------------------------------------------------------
# Salary structures (compensation)
# ---------------------------------------------------------------------------
def serialize_structure(s: SalaryStructure, employee: Optional[Employee] = None) -> dict:
    return {
        "id": s.id,
        "employee_id": s.employee_id,
        "employee_name": employee.full_name if employee else None,
        "basic_salary": float(s.basic_salary),
        "house_rent_allowance": float(s.house_rent_allowance),
        "dearness_allowance": float(s.dearness_allowance),
        "conveyance_allowance": float(s.conveyance_allowance),
        "other_allowances": float(s.other_allowances),
        "gross": float(
            _money(s.basic_salary) + _money(s.house_rent_allowance) + _money(s.dearness_allowance)
            + _money(s.conveyance_allowance) + _money(s.other_allowances)
        ),
        "effective_from": s.effective_from.isoformat(),
        "effective_to": s.effective_to.isoformat() if s.effective_to else None,
    }


def list_structures(db: Session, principal: Principal, employee_id: Optional[int] = None) -> List[dict]:
    scope = authz.employee_scope(db, principal, "compensation:read")
    query = (
        db.query(SalaryStructure, Employee)
        .join(Employee, Employee.id == SalaryStructure.employee_id)
        .filter(SalaryStructure.company_id == principal.company_id, scope.clause(SalaryStructure.employee_id))
    )
    if employee_id:
        query = query.filter(SalaryStructure.employee_id == employee_id)
    return [serialize_structure(s, e) for s, e in query.order_by(SalaryStructure.effective_from.desc()).all()]


def get_structure(db: Session, principal: Principal, structure_id: int) -> SalaryStructure:
    structure = db.query(SalaryStructure).filter(
        SalaryStructure.company_id == principal.company_id, SalaryStructure.id == structure_id
    ).first()
    if structure is None or not authz.can_access_employee(db, principal, "compensation:read", structure.employee_id):
        raise not_found("Salary structure")
    return structure


def create_structure(db: Session, principal: Principal, data) -> SalaryStructure:
    _require_company_scope(principal, "compensation:manage")
    employee = authz.ensure_employee_access(db, principal, "employee:read", data.employee_id)
    if data.effective_to and data.effective_to < data.effective_from:
        raise bad_request("Effective end must be after the start.")
    # Close the currently open structure so revisions never overlap.
    open_structure = db.query(SalaryStructure).filter(
        SalaryStructure.employee_id == employee.id, SalaryStructure.effective_to.is_(None)
    ).first()
    if open_structure:
        if open_structure.effective_from >= data.effective_from:
            raise conflict("A newer salary structure already exists. Edit it instead.")
        from datetime import timedelta

        open_structure.effective_to = data.effective_from - timedelta(days=1)
    structure = SalaryStructure(company_id=principal.company_id, created_by_user_id=principal.user_id, **data.model_dump())
    db.add(structure)
    if data.effective_from <= company_today(db, principal.company_id):
        employee.base_salary = data.basic_salary  # a revision dated in the future applies when it starts
    db.flush()
    AuditLogService.log(
        db, principal, "Compensation", "CREATE", f"Salary structure for {employee.full_name} effective {data.effective_from}",
        "salary_structure", structure.id,
    )
    return structure


def update_structure(db: Session, principal: Principal, structure_id: int, data) -> SalaryStructure:
    _require_company_scope(principal, "compensation:manage")
    structure = get_structure(db, principal, structure_id)
    used = db.query(Payroll.id).filter(
        Payroll.employee_id == structure.employee_id,
        Payroll.status != "draft",
        Payroll.salary_month >= structure.effective_from,
    )
    if structure.effective_to is not None:
        used = used.filter(Payroll.salary_month <= structure.effective_to)
    if used.first():
        raise conflict("This structure was used by approved payroll. Create a revision instead.", "STRUCTURE_LOCKED")
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(structure, key, value)
    AuditLogService.log(db, principal, "Compensation", "UPDATE", f"Updated salary structure #{structure.id}", "salary_structure", structure.id)
    return structure


def delete_structure(db: Session, principal: Principal, structure_id: int) -> None:
    _require_company_scope(principal, "compensation:manage")
    structure = get_structure(db, principal, structure_id)
    if db.query(Payroll.id).filter(
        Payroll.employee_id == structure.employee_id, Payroll.salary_month >= structure.effective_from
    ).first():
        raise conflict("Payroll exists for this structure; it cannot be deleted.", "STRUCTURE_LOCKED")
    AuditLogService.log(db, principal, "Compensation", "DELETE", f"Deleted salary structure #{structure.id}", "salary_structure", structure.id)
    db.delete(structure)

from datetime import date
from io import BytesIO
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Set

from openpyxl import Workbook
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.core import authz
from app.core.clock import company_today
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.employee import Department, Employee, FaceEncoding, Shift
from app.models.payroll import SalaryStructure
from app.models.user import User
from app.schemas.employees import EmployeeCreate, EmployeeUpdate
from app.services.audit_log_service import AuditLogService

CONFIDENTIAL_FIELDS = ("phone", "date_of_birth", "gender", "address", "city", "state", "postal_code", "country")


class FieldAccess:
    """Per-record field visibility computed once for a page of employees."""

    def __init__(self, db: Session, principal: Principal, employee_ids: Iterable[int]):
        ids = list(set(employee_ids))
        self.confidential = self._allowed(db, principal, "employee:read_confidential", ids)
        self.compensation = self._allowed(db, principal, "compensation:read", ids)
        self.face_registered = (
            {r[0] for r in db.query(FaceEncoding.employee_id).filter(FaceEncoding.employee_id.in_(ids)).distinct()}
            if ids
            else set()
        )
        self.current_basic = current_basic_salaries(db, principal, self.compensation)

    def base_salary(self, employee: Employee) -> Optional[float]:
        value = self.current_basic.get(employee.id, employee.base_salary)
        return float(value) if value is not None else None

    @staticmethod
    def _allowed(db: Session, principal: Principal, permission: str, ids: List[int]) -> Set[int]:
        if not ids:
            return set()
        scope = authz.employee_scope(db, principal, permission)
        if scope.is_empty:
            return set()
        if scope.all_company:
            return set(ids)
        rows = db.query(Employee.id).filter(Employee.id.in_(ids), scope.clause(Employee.id)).all()
        return {r[0] for r in rows}


def current_basic_salaries(db: Session, principal: Principal, ids: Set[int]) -> Dict[int, Decimal]:
    """Basic pay of the salary structure in effect today; a revision dated next month waits."""
    if not ids:
        return {}
    today = company_today(db, principal.company_id)
    rows = (
        db.query(SalaryStructure.employee_id, SalaryStructure.basic_salary)
        .filter(
            SalaryStructure.employee_id.in_(ids),
            SalaryStructure.effective_from <= today,
            or_(SalaryStructure.effective_to.is_(None), SalaryStructure.effective_to >= today),
        )
        .order_by(SalaryStructure.employee_id, SalaryStructure.effective_from.desc())
        .all()
    )
    current: Dict[int, Decimal] = {}
    for employee_id, basic in rows:
        current.setdefault(employee_id, basic)
    return current


def serialize(employee: Employee, access: FieldAccess) -> dict:
    data = {
        "id": employee.id,
        "employee_code": employee.employee_code,
        "first_name": employee.first_name,
        "last_name": employee.last_name,
        "full_name": employee.full_name,
        "email": employee.email,
        "position": employee.position,
        "department_id": employee.department_id,
        "department": employee.department.name if employee.department else None,
        "shift_id": employee.shift_id,
        "shift": employee.shift.name if employee.shift else None,
        "location": employee.location,
        "hire_date": employee.hire_date.isoformat() if employee.hire_date else None,
        "termination_date": employee.termination_date.isoformat() if employee.termination_date else None,
        "is_active": employee.is_active,
        "manager_id": employee.manager_id,
        "manager_name": employee.manager.full_name if employee.manager else None,
        "has_login": employee.user_id is not None,
        "has_photo": bool(employee.photo_path),
        "face_registered": employee.id in access.face_registered,
    }
    if employee.id in access.confidential:
        for field in CONFIDENTIAL_FIELDS:
            value = getattr(employee, field)
            data[field] = value.isoformat() if isinstance(value, date) else value
    if employee.id in access.compensation:
        data["base_salary"] = access.base_salary(employee)
    return data


def _base_query(db: Session, principal: Principal, permission: str = "employee:read"):
    scope = authz.employee_scope(db, principal, permission)
    return (
        db.query(Employee)
        .options(joinedload(Employee.department), joinedload(Employee.shift), joinedload(Employee.manager))
        .filter(Employee.company_id == principal.company_id, scope.clause(Employee.id))
    )


def list_employees(
    db: Session,
    principal: Principal,
    *,
    q: Optional[str] = None,
    department_id: Optional[int] = None,
    manager_id: Optional[int] = None,
    include_inactive: bool = False,
    skip: int = 0,
    limit: int = 50,
):
    query = _base_query(db, principal)
    if not include_inactive:
        query = query.filter(Employee.is_active.is_(True))
    if department_id:
        query = query.filter(Employee.department_id == department_id)
    if manager_id:
        query = query.filter(Employee.manager_id == manager_id)
    if q:
        term = f"%{q.strip()}%"
        query = query.filter(
            or_(
                Employee.full_name.ilike(term),
                Employee.email.ilike(term),
                Employee.employee_code.ilike(term),
                Employee.position.ilike(term),
            )
        )
    # Totals are computed from the same scoped query, so pagination metadata
    # never reveals records outside the caller's scope.
    total = query.order_by(None).count()
    items = query.order_by(Employee.first_name, Employee.last_name, Employee.id).offset(skip).limit(limit).all()
    access = FieldAccess(db, principal, [e.id for e in items])
    return total, [serialize(e, access) for e in items]


def get_employee(db: Session, principal: Principal, employee_id: int) -> dict:
    employee = authz.ensure_employee_access(db, principal, "employee:read", employee_id)
    return serialize(employee, FieldAccess(db, principal, [employee.id]))


def _company_ref(db: Session, model, company_id: int, ref_id: Optional[int], label: str):
    if ref_id is None:
        return None
    row = db.query(model).filter(model.company_id == company_id, model.id == ref_id).first()
    if row is None:
        raise not_found(label)
    return row


def _assert_unique(db: Session, company_id: int, email: Optional[str], code: Optional[str], exclude_id=None) -> None:
    if email:
        q = db.query(Employee.id).filter(Employee.company_id == company_id, func.lower(Employee.email) == email.lower())
        if exclude_id:
            q = q.filter(Employee.id != exclude_id)
        if q.first():
            raise conflict("Another employee already uses this email.")
    if code:
        q = db.query(Employee.id).filter(Employee.company_id == company_id, Employee.employee_code == code)
        if exclude_id:
            q = q.filter(Employee.id != exclude_id)
        if q.first():
            raise conflict("Another employee already uses this employee code.")


def create_employee(db: Session, principal: Principal, data: EmployeeCreate) -> Employee:
    cid = principal.company_id
    _assert_unique(db, cid, data.email, data.employee_code)
    _company_ref(db, Department, cid, data.department_id, "Department")
    _company_ref(db, Shift, cid, data.shift_id, "Shift")
    if data.base_salary is not None and not principal.has("compensation:manage"):
        raise forbidden("Setting salary requires the Manage compensation permission.")

    values = data.model_dump(exclude={"manager_id", "legal_entity_id", "branch_id"})
    values["email"] = data.email.lower()
    employee = Employee(company_id=cid, **values)
    db.add(employee)
    db.flush()
    from app.services import org_structure_service

    org_structure_service.record_hire(db, employee, principal.user_id, data.legal_entity_id, data.branch_id)

    if data.manager_id is not None:
        from app.services import org_service

        org_service.assign_manager(db, principal, employee, data.manager_id, audit=False)

    AuditLogService.log(
        db, principal, "Employees", "CREATE", f"Created employee {employee.employee_code} ({employee.full_name})",
        "employee", employee.id,
    )
    return employee


def update_employee(db: Session, principal: Principal, employee_id: int, data: EmployeeUpdate) -> Employee:
    employee = authz.ensure_employee_access(db, principal, "employee:update", employee_id)
    changes = data.model_dump(exclude_unset=True)
    if "base_salary" in changes and not principal.has("compensation:manage"):
        raise forbidden("Changing salary requires the Manage compensation permission.")
    if "base_salary" in changes:
        # Once a salary structure is in effect, base salary follows its basic pay (and so does payroll).
        structured = current_basic_salaries(db, principal, {employee.id}).get(employee.id)
        if structured is not None:
            if changes["base_salary"] is None or Decimal(changes["base_salary"]) != structured:
                raise conflict(
                    "Base salary follows the employee's salary structure. Record a salary revision instead.",
                    "SALARY_FROM_STRUCTURE",
                )
            changes.pop("base_salary")
    if any(f in changes for f in CONFIDENTIAL_FIELDS) and not authz.can_access_employee(
        db, principal, "employee:read_confidential", employee.id
    ):
        raise forbidden("Editing personal details requires access to them.")
    _assert_unique(db, principal.company_id, changes.get("email"), changes.get("employee_code"), exclude_id=employee.id)
    if "department_id" in changes:
        _company_ref(db, Department, principal.company_id, changes["department_id"], "Department")
    if "shift_id" in changes:
        _company_ref(db, Shift, principal.company_id, changes["shift_id"], "Shift")
    if "email" in changes and changes["email"]:
        changes["email"] = changes["email"].lower()
    if "location" in changes:
        location = (changes["location"] or "").strip()
        if not location:
            changes.pop("location")  # the placement keeps its branch; clearing the text changes nothing
        elif location != (employee.location or ""):
            from app.services import org_structure_service

            # Work location is a branch: the change becomes a dated transfer (before any department change).
            org_structure_service.record_location_change(db, principal, employee, location)
            changes["location"] = location[:100]
    if "department_id" in changes and changes["department_id"] != employee.department_id:
        from app.services import org_structure_service

        # Keep placement history: the change becomes a dated assignment instead of an overwrite.
        org_structure_service.record_department_change(db, principal, employee, changes["department_id"])
    for key, value in changes.items():
        setattr(employee, key, value)
    AuditLogService.log(
        db, principal, "Employees", "UPDATE",
        f"Updated employee {employee.employee_code}: {', '.join(sorted(changes)) or 'no changes'}",
        "employee", employee.id,
    )
    return employee


def deactivate_employee(db: Session, principal: Principal, employee_id: int) -> Employee:
    employee = authz.ensure_employee_access(db, principal, "employee:deactivate", employee_id)
    if authz.is_self(principal, employee.id):
        raise forbidden("You cannot end your own employment.")
    if not employee.is_active:
        return employee
    reports = db.query(func.count(Employee.id)).filter(
        Employee.manager_id == employee.id, Employee.is_active.is_(True)
    ).scalar()
    if reports:
        raise conflict(f"Reassign this employee's {reports} direct report(s) first.", "HAS_REPORTS")

    from app.services import auth_service, rbac_service

    if employee.user_id:
        rbac_service.assert_not_last_admin(db, principal.company_id, employee.user_id)
        user = db.get(User, employee.user_id)
        if user and user.status in ("active", "invited", "suspended"):
            user.status = "deactivated"
            auth_service.revoke_user_sessions(db, user.id, "employment_ended")

    from app.services import location_service

    location_service.stop_for_employee(db, principal.company_id, employee.id, "EMPLOYMENT_ENDED")
    # Historical attendance, leave and payroll stay untouched.
    employee.is_active = False
    employee.termination_date = date.today()
    from app.services import org_structure_service

    org_structure_service.close_on_termination(db, employee, employee.termination_date)
    AuditLogService.log(
        db, principal, "Employees", "DEACTIVATE", f"Ended employment of {employee.employee_code}", "employee", employee.id
    )
    return employee


def reactivate_employee(db: Session, principal: Principal, employee_id: int) -> Employee:
    employee = authz.ensure_employee_access(db, principal, "employee:deactivate", employee_id)
    was_active = employee.is_active
    employee.is_active = True
    employee.termination_date = None
    if not was_active:
        from app.services import org_structure_service

        # Rehire: same employee row and code, new placement from today (02 §13 rule 7).
        org_structure_service.record_rehire(db, employee, principal.user_id, date.today())
    AuditLogService.log(
        db, principal, "Employees", "REACTIVATE", f"Reactivated employee {employee.employee_code}", "employee", employee.id
    )
    return employee


def update_own_profile(db: Session, principal: Principal, changes: dict) -> Employee:
    if not principal.has("profile:update"):
        raise forbidden()
    employee = authz.own_employee(db, principal)
    for key, value in changes.items():
        setattr(employee, key, value)
    AuditLogService.log(db, principal, "Employees", "PROFILE_UPDATE", "Updated own contact details", "employee", employee.id)
    return employee


def export_employees(db: Session, principal: Principal) -> BytesIO:
    query = _base_query(db, principal).filter(Employee.is_active.is_(True)).order_by(Employee.employee_code)
    employees = query.all()
    access = FieldAccess(db, principal, [e.id for e in employees])
    show_comp = bool(access.compensation)
    show_conf = bool(access.confidential)

    wb = Workbook()
    ws = wb.active
    ws.title = "Employees"
    header = ["Employee Code", "First Name", "Last Name", "Email", "Position", "Department", "Manager", "Location", "Hire Date"]
    if show_conf:
        header += ["Phone", "City", "Country"]
    if show_comp:
        header += ["Base Salary"]
    ws.append(header)
    for emp in employees:
        row = [
            emp.employee_code, emp.first_name, emp.last_name, emp.email, emp.position or "",
            emp.department.name if emp.department else "", emp.manager.full_name if emp.manager else "",
            emp.location or "", emp.hire_date.isoformat() if emp.hire_date else "",
        ]
        if show_conf:
            visible = emp.id in access.confidential
            row += [emp.phone if visible else "", emp.city if visible else "", emp.country if visible else ""]
        if show_comp:
            salary = access.base_salary(emp) if emp.id in access.compensation else None
            row += [salary if salary is not None else ""]
        ws.append(row)
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    AuditLogService.log(db, principal, "Employees", "EXPORT", f"Exported {len(employees)} employee records")
    return output


def list_departments(db: Session, principal: Principal) -> List[dict]:
    rows = (
        db.query(Department, func.count(Employee.id))
        .outerjoin(Employee, (Employee.department_id == Department.id) & Employee.is_active.is_(True))
        .filter(Department.company_id == principal.company_id)
        .group_by(Department.id)
        .order_by(Department.name)
        .all()
    )
    return [
        {
            "id": d.id,
            "name": d.name,
            "description": d.description,
            "is_active": d.is_active,
            "head_employee_id": d.head_employee_id,
            "head_name": d.head.full_name if d.head else None,
            "parent_department_id": d.parent_department_id,
            "employee_count": count,
        }
        for d, count in rows
    ]


def save_department(db: Session, principal: Principal, data, department_id: Optional[int] = None) -> Department:
    cid = principal.company_id
    clash = db.query(Department.id).filter(Department.company_id == cid, func.lower(Department.name) == data.name.lower())
    if department_id:
        clash = clash.filter(Department.id != department_id)
    if clash.first():
        raise conflict("A department with this name already exists.")
    if data.head_employee_id is not None:
        head = _company_ref(db, Employee, cid, data.head_employee_id, "Employee")
        if not head.is_active:
            raise bad_request("The department head must be an active employee.")
    from app.services import org_structure_service

    parent_id = org_structure_service.validate_department_parent(db, cid, department_id, data.parent_department_id)
    if department_id:
        department = _company_ref(db, Department, cid, department_id, "Department")
    else:
        department = Department(company_id=cid)
        db.add(department)
    department.parent_department_id = parent_id
    department.name = data.name.strip()
    department.description = data.description
    department.head_employee_id = data.head_employee_id
    department.is_active = data.is_active
    db.flush()
    AuditLogService.log(db, principal, "Departments", "SAVE", f"Saved department '{department.name}'", "department", department.id)
    return department


def save_shift(db: Session, principal: Principal, data, shift_id: Optional[int] = None) -> Shift:
    cid = principal.company_id
    clash = db.query(Shift.id).filter(Shift.company_id == cid, func.lower(Shift.name) == data.name.lower())
    if shift_id:
        clash = clash.filter(Shift.id != shift_id)
    if clash.first():
        raise conflict("A shift with this name already exists.")
    shift = _company_ref(db, Shift, cid, shift_id, "Shift") if shift_id else Shift(company_id=cid)
    if not shift_id:
        db.add(shift)
    for key, value in data.model_dump().items():
        setattr(shift, key, value)
    db.flush()
    AuditLogService.log(db, principal, "Departments", "SAVE_SHIFT", f"Saved shift '{shift.name}'", "shift", shift.id)
    return shift

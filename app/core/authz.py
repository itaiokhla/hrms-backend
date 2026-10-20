"""Central authorization service.

Every protected operation is decided from three independent inputs:
  1. the functional permission (resource:action) held through roles,
  2. the data scope attached to that grant, and
  3. tenant ownership of the target (enforced again by ORM events and RLS).

Scopes are turned into SQL sub-selects of employee ids that are applied to
the database query itself, so lists, counts, searches, aggregates and
exports all see exactly the same authorised set. Default is deny.
"""

from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from sqlalchemy import ColumnElement, Select, false, select, true, union
from sqlalchemy.orm import Session

from app.core import hierarchy
from app.core.errors import forbidden, not_found
from app.core.permission_registry import Scope
from app.core.principal import Principal
from app.models.employee import Department, Employee
from app.models.rbac import ScopeAssignment


@dataclass
class EmployeeScope:
    """The set of employees a principal may act on for one permission."""

    all_company: bool = False
    selects: List[Select] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.all_company and not self.selects

    def clause(self, column) -> ColumnElement:
        if self.all_company:
            return true()
        if not self.selects:
            return false()
        if len(self.selects) == 1:
            return column.in_(self.selects[0])
        return column.in_(union(*self.selects))


def employee_scope(db: Session, principal: Principal, permission: str) -> EmployeeScope:
    scopes = principal.scopes(permission)
    if not scopes:
        return EmployeeScope()
    if Scope.COMPANY.value in scopes:
        return EmployeeScope(all_company=True)

    cid = principal.company_id
    me = principal.employee_id
    selects: List[Select] = []

    if me is not None:
        if Scope.SELF.value in scopes:
            selects.append(select(Employee.id).where(Employee.company_id == cid, Employee.id == me))
        if Scope.REPORTING_TREE.value in scopes:
            selects.append(hierarchy.descendants_select(cid, me))
            selects.append(hierarchy.secondary_reports_select(cid, me))
        elif Scope.DIRECT_REPORTS.value in scopes:
            selects.append(hierarchy.direct_reports_select(cid, me))
            selects.append(hierarchy.secondary_reports_select(cid, me))
        if Scope.DEPARTMENT.value in scopes:
            headed = select(Department.id).where(Department.company_id == cid, Department.head_employee_id == me)
            own = select(Employee.department_id).where(Employee.company_id == cid, Employee.id == me)
            selects.append(
                select(Employee.id).where(
                    Employee.company_id == cid,
                    Employee.department_id.in_(union(headed, own)),
                )
            )
    if Scope.ASSIGNED_GROUP.value in scopes:
        selects.append(
            select(ScopeAssignment.employee_id).where(
                ScopeAssignment.company_id == cid, ScopeAssignment.user_id == principal.user_id
            )
        )
    return EmployeeScope(selects=selects)


def scoped_employee_ids(db: Session, principal: Principal, *permissions: str) -> EmployeeScope:
    """Intersection: an employee is included only if in scope for every permission."""
    scopes = [employee_scope(db, principal, p) for p in permissions]
    if any(s.is_empty for s in scopes):
        return EmployeeScope()
    restricted = [s for s in scopes if not s.all_company]
    if not restricted:
        return EmployeeScope(all_company=True)
    if len(restricted) == 1:
        return restricted[0]
    combined = select(Employee.id).where(
        Employee.company_id == principal.company_id,
        *[s.clause(Employee.id) for s in restricted],
    )
    return EmployeeScope(selects=[combined])


def can_access_employee(db: Session, principal: Principal, permission: str, employee_id: int) -> bool:
    scope = employee_scope(db, principal, permission)
    if scope.is_empty:
        return False
    found = (
        db.query(Employee.id)
        .filter(
            Employee.company_id == principal.company_id,
            Employee.id == employee_id,
            scope.clause(Employee.id),
        )
        .first()
    )
    return found is not None


def ensure_employee_access(db: Session, principal: Principal, permission: str, employee_id: int) -> Employee:
    """Load an employee the caller may act on, or raise 404 (never reveal existence)."""
    if not principal.has(permission):
        raise forbidden()
    scope = employee_scope(db, principal, permission)
    employee = (
        db.query(Employee)
        .filter(
            Employee.company_id == principal.company_id,
            Employee.id == employee_id,
            scope.clause(Employee.id),
        )
        .first()
    )
    if employee is None:
        raise not_found("Employee")
    return employee


def ensure_all_employees_access(
    db: Session, principal: Principal, permission: str, employee_ids: Iterable[int]
) -> List[Employee]:
    """Bulk check: all-or-nothing, so mixed authorized/unauthorized batches fail."""
    ids = sorted(set(employee_ids))
    if not ids:
        return []
    scope = employee_scope(db, principal, permission)
    found = (
        db.query(Employee)
        .filter(Employee.company_id == principal.company_id, Employee.id.in_(ids), scope.clause(Employee.id))
        .all()
    )
    if len(found) != len(ids):
        raise not_found("One or more employees")
    return found


def own_employee(db: Session, principal: Principal) -> Employee:
    if principal.employee_id is None:
        raise not_found("Employee profile")
    employee = db.get(Employee, principal.employee_id)
    if employee is None or employee.company_id != principal.company_id:
        raise not_found("Employee profile")
    return employee


def is_self(principal: Principal, employee_id: Optional[int]) -> bool:
    return principal.employee_id is not None and principal.employee_id == employee_id

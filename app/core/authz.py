"""Central authorization service.

Every protected operation is decided from three independent inputs:
  1. the functional permission (resource:action) held through roles,
  2. the data scope attached to that grant, and
  3. tenant ownership of the target (enforced again by ORM events and RLS).

Scopes are turned into SQL sub-selects of employee ids that are applied to
the database query itself, so lists, counts, searches, aggregates and
exports all see exactly the same authorised set. Default is deny.

Targeted scopes (LEGAL_ENTITY, BRANCH, PAY_GROUP) match the employee's primary placement in
employee_assignments: today's by default, or any placement overlapping a period for payroll
objects, so history follows the record's date (02 §15.2, EC-ORG-14).

Team scopes (X 01 §4; X-M1) match ``team_memberships`` on employee-local dates: TEAM / TEAM_TREE reach the members
of teams the principal leads (LEAD / DEPUTY_LEAD), with TEAM_TREE also their sub-teams by the parent valid on the date;
ASSIGNED_TEAMS reaches the members of the target teams of the role assignment. "Today" is the principal's local today;
with a period, a member counts only when the member row, the lead row and the period share a date (the per-date
predicate of X-ROLE-41; a one-day period is the detail-read rule). Members after their employment ended never count
(TM-8), and TEAM / TEAM_TREE never reach the lead's own management chain (upward exclusion, X-ROLE-43).
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, List, Optional, Tuple

from sqlalchemy import ColumnElement, Select, and_, false, func, literal, or_, select, true, union
from sqlalchemy.orm import Session, aliased

from app.core import hierarchy
from app.core.errors import forbidden, not_found
from app.core.permission_registry import TARGET_ASSIGNMENT_COLUMNS, TARGETED_SCOPES, Scope
from app.core.principal import Principal
from app.models.employee import Department, Employee
from app.models.org import EmployeeAssignment
from app.models.rbac import ScopeAssignment
from app.models.teams import LEAD_ROLES, TeamMembership, TeamParentHistory

# Inclusive date window [start, end] of a payroll object (pay period, effective date).
Period = Tuple[date, date]


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


def employee_scope(
    db: Session, principal: Principal, permission: str, *, period: Optional[Period] = None
) -> EmployeeScope:
    """Employees in scope for `permission`: placements of today, or (with `period`) of any day in it."""
    scopes = principal.scopes(permission)
    if not scopes:
        return EmployeeScope()
    if Scope.COMPANY.value in scopes:
        return EmployeeScope(all_company=True)
    if period is not None:
        _check_period(period)
    return EmployeeScope(
        selects=relationship_selects(principal, permission, period=period)
        + targeted_selects(principal, permission, period=period)
    )


def employee_scope_during(db: Session, principal: Principal, permission: str, start: date, end: date) -> EmployeeScope:
    """Employees who were inside the scope at any time in [start, end] (payroll history, 3.9 §5.4)."""
    return employee_scope(db, principal, permission, period=(start, end))


def relationship_selects(principal: Principal, permission: str, *, period: Optional[Period] = None) -> List[Select]:
    """Employee ids reached through the user's own position: SELF, reporting lines, departments, assigned group.

    With `period`, DEPARTMENT matches department placements overlapping it. Reporting lines are
    today's (there is no manager history yet); payroll permissions never offer those scopes.
    """
    scopes = principal.scopes(permission)
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
            departments = _with_child_departments(cid, union(headed, own))
            if period is None:
                selects.append(
                    select(Employee.id).where(Employee.company_id == cid, Employee.department_id.in_(departments))
                )
            else:
                selects.append(
                    select(EmployeeAssignment.employee_id).where(
                        EmployeeAssignment.company_id == cid,
                        EmployeeAssignment.department_id.in_(departments),
                        _overlap_filter(*period),
                    )
                )
    if Scope.ASSIGNED_GROUP.value in scopes:
        selects.append(
            select(ScopeAssignment.employee_id).where(
                ScopeAssignment.company_id == cid, ScopeAssignment.user_id == principal.user_id
            )
        )
    if me is not None and (Scope.TEAM.value in scopes or Scope.TEAM_TREE.value in scopes):
        start, end = period if period is not None else (principal_today(principal),) * 2
        selects.append(team_lead_select(cid, me, start, end, tree=Scope.TEAM_TREE.value in scopes))
    return selects


def principal_today(principal: Principal) -> date:
    """The principal's local today (set at login for team-scope holders), else the clock's UTC date."""
    today = principal.extra.get("local_today") if principal.extra else None
    if today is None:
        from app.core import clock

        today = clock.utcnow().date()
    return today


_FAR = date(9999, 12, 31)


def _window(start: date, end: date, *rows) -> ColumnElement:
    """Every dated row of ``rows`` and [start, end] share at least one date (empty, cancelled rows never match)."""
    first = func.greatest(literal(start), *[r.effective_from for r in rows])
    last = func.least(literal(end), *[func.coalesce(r.effective_to, _FAR) for r in rows])
    return first <= last


def _employed(member, start: date) -> ColumnElement:
    """Membership rows of people whose employment ended before ``start`` never count (TM-2, TM-8)."""
    return member.employee_id.in_(select(Employee.id).where(
        Employee.company_id == member.company_id,
        or_(Employee.termination_date.is_(None), Employee.termination_date >= start)))


def ancestors_select(company_id: int, employee_id: int) -> Select:
    """The reporting-line ancestors of ``employee_id`` (line manager upwards, depth <= 64)."""
    start = select(Employee.manager_id.label("id"), literal(1).label("depth")).where(
        Employee.company_id == company_id, Employee.id == employee_id)
    chain = start.cte(recursive=True)
    parent = aliased(Employee)
    chain = chain.union_all(select(parent.manager_id, chain.c.depth + 1).where(
        parent.company_id == company_id, parent.id == chain.c.id, chain.c.depth < hierarchy.MAX_DEPTH))
    return select(chain.c.id).where(chain.c.id.is_not(None))


def lead_teams_select(company_id: int, lead_employee_id: int, start: date, end: date, *, tree: bool) -> Select:
    """Teams ``lead_employee_id`` leads in [start, end], with TEAM_TREE also their sub-teams by the parent link valid
    in the window (team_parent_history; box P-10, X-ROLE-42)."""
    lead = aliased(TeamMembership)
    led = (lead.company_id == company_id, lead.employee_id == lead_employee_id, lead.member_role.in_(LEAD_ROLES),
           _window(start, end, lead))
    if not tree:
        return select(lead.team_id).where(*led)
    teams = select(lead.team_id.label("id"), literal(0).label("depth")).where(*led).cte(
        "lead_team_tree", recursive=True)
    link = aliased(TeamParentHistory)
    teams = teams.union(select(link.team_id, teams.c.depth + 1).where(
        link.company_id == company_id, link.parent_team_id == teams.c.id, teams.c.depth < hierarchy.MAX_DEPTH,
        _window(start, end, link)))
    return select(teams.c.id)


def team_lead_select(company_id: int, lead_employee_id: int, start: date, end: date, *, tree: bool) -> Select:
    """Members of the teams ``lead_employee_id`` leads (TEAM), plus their sub-teams (TEAM_TREE), in [start, end].

    A member counts only when the member row shares a date with a lead row of the principal (and the window), so a
    lead who starts mid-month reaches nobody before that date (X-ROLE-41)."""
    member = aliased(TeamMembership)
    lead = aliased(TeamMembership)
    return select(member.employee_id).where(
        member.company_id == company_id,
        member.team_id.in_(lead_teams_select(company_id, lead_employee_id, start, end, tree=tree)),
        _window(start, end, member), _employed(member, start),
        member.employee_id.not_in(ancestors_select(company_id, lead_employee_id)),
        select(lead.id).where(
            lead.company_id == company_id, lead.employee_id == lead_employee_id, lead.member_role.in_(LEAD_ROLES),
            _window(start, end, lead, member)).exists(),
    )


def assigned_teams_select(company_id: int, team_ids: Iterable[int], start: date, end: date) -> Select:
    """Members of the named teams in [start, end] (ASSIGNED_TEAMS; sub-teams only when named)."""
    member = aliased(TeamMembership)
    return select(member.employee_id).where(
        member.company_id == company_id, member.team_id.in_(sorted(team_ids)),
        _window(start, end, member), _employed(member, start))


def targeted_selects(principal: Principal, permission: str, *, period: Optional[Period] = None) -> List[Select]:
    """Employee ids placed in the targets of LEGAL_ENTITY / BRANCH / PAY_GROUP grants (none without targets)."""
    scopes = principal.scopes(permission)
    cid = principal.company_id
    selects: List[Select] = []
    for scope in TARGETED_SCOPES:
        if scope.value not in scopes:
            continue
        targets = principal.scope_targets(permission, scope.value)
        if not targets:
            continue
        if scope == Scope.ASSIGNED_TEAMS:  # team_memberships, not employee_assignments (X 01 §4.2)
            start, end = period if period is not None else (principal_today(principal),) * 2
            selects.append(assigned_teams_select(cid, targets, start, end))
            continue
        column = TARGET_ASSIGNMENT_COLUMNS[scope]
        if period is None:
            from app.services.org_structure_service import current_assignment_select

            selects.append(current_assignment_select(cid, column, targets))
        else:
            selects.append(assignment_overlap_select(cid, column, targets, *period))
    return selects


def assignment_overlap_select(company_id: int, column: str, ids: Iterable[int], start: date, end: date) -> Select:
    """Employee ids with a primary placement in `ids` (by `column`) on any day of [start, end]."""
    target = getattr(EmployeeAssignment, column)
    return select(EmployeeAssignment.employee_id).where(
        EmployeeAssignment.company_id == company_id, target.in_(sorted(ids)), _overlap_filter(start, end)
    )


def _overlap_filter(start: date, end: date) -> ColumnElement:
    return and_(
        EmployeeAssignment.is_primary.is_(True),
        EmployeeAssignment.effective_from <= end,
        or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= start),
    )


def _check_period(period: Period) -> None:
    start, end = period
    if not isinstance(start, date) or not isinstance(end, date) or start > end:
        raise ValueError("period must be (start, end) dates with start <= end")


def _with_child_departments(company_id: int, roots: Select) -> Select:
    """The given departments plus all their teams (child departments), cycle-safe and depth-limited."""
    roots_cte = roots.subquery()
    tree = (
        select(Department.id.label("id"), literal(0).label("depth"))
        .where(Department.company_id == company_id, Department.id.in_(select(roots_cte.c[0])))
        .cte("department_tree", recursive=True)
    )
    child = (
        select(Department.id, (tree.c.depth + 1))
        .join(tree, Department.parent_department_id == tree.c.id)
        .where(Department.company_id == company_id, tree.c.depth < hierarchy.MAX_DEPTH)
    )
    tree = tree.union(child)
    return select(tree.c.id)


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

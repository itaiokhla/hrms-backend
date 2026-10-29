"""Organisation hierarchy: reporting lines, secondary managers, delegations."""

from datetime import date
from typing import List, Optional

from sqlalchemy import and_, func, or_
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, aliased, joinedload

from app.core import authz, hierarchy
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.core.permission_registry import Scope
from app.core.principal import Principal
from app.core.security import utcnow
from app.database.db import lock_hierarchy
from app.models.employee import ApprovalDelegation, Employee, ManagerHistory, SecondaryReportingLine
from app.services.audit_log_service import AuditLogService
from app.services.notification_service import NotificationService

CYCLE_MESSAGE = "This change would create a circular reporting line."


def _employee_in_company(db: Session, company_id: int, employee_id: int) -> Employee:
    employee = db.query(Employee).filter(Employee.company_id == company_id, Employee.id == employee_id).first()
    if employee is None:
        raise not_found("Employee")
    return employee


def assign_manager(
    db: Session, principal: Principal, employee: Employee, manager_id: Optional[int], *, audit: bool = True
) -> Employee:
    """Change an employee's primary manager with full validation.

    Validation order: authority over the target, authority over the new
    manager, same company, no self-reporting, active manager, no cycle. The
    whole check runs under a per-company advisory lock, and the database
    trigger re-validates, so concurrent changes cannot form a cycle.
    """
    if not principal.has("org:manage_hierarchy"):
        raise forbidden()
    if authz.is_self(principal, employee.id):
        raise forbidden("You cannot change your own reporting line.")
    if not authz.can_access_employee(db, principal, "org:manage_hierarchy", employee.id):
        raise not_found("Employee")

    lock_hierarchy(db, principal.company_id)
    db.refresh(employee)
    old_manager_id = employee.manager_id
    if manager_id == old_manager_id:
        return employee

    new_manager = None
    if manager_id is not None:
        if manager_id == employee.id:
            raise bad_request("An employee cannot report to themselves.", "SELF_REPORTING")
        new_manager = _employee_in_company(db, principal.company_id, manager_id)
        if not new_manager.is_active:
            raise bad_request("The new manager must be an active employee.")
        if not (
            authz.is_self(principal, new_manager.id)
            or authz.can_access_employee(db, principal, "org:manage_hierarchy", new_manager.id)
        ):
            # Prevents pulling people into (or out to) a tree you don't control.
            raise not_found("Manager")
        if hierarchy.would_create_cycle(db, principal.company_id, employee.id, new_manager.id):
            raise bad_request(CYCLE_MESSAGE, "HIERARCHY_CYCLE")

    employee.manager_id = manager_id
    try:
        db.flush()
    except DBAPIError as exc:
        db.rollback()
        if "cycle" in str(exc.orig).lower() or "report to themselves" in str(exc.orig).lower():
            raise bad_request(CYCLE_MESSAGE, "HIERARCHY_CYCLE")
        raise

    now = utcnow()
    db.query(ManagerHistory).filter(
        ManagerHistory.employee_id == employee.id, ManagerHistory.effective_until.is_(None)
    ).update({ManagerHistory.effective_until: now}, synchronize_session=False)
    db.add(
        ManagerHistory(
            company_id=principal.company_id,
            employee_id=employee.id,
            manager_id=manager_id,
            effective_from=now,
            changed_by_user_id=principal.user_id,
        )
    )

    # Pending approvals follow the new reporting line; decided ones are kept.
    from app.services import leave_service

    leave_service.reroute_pending_for(db, principal.company_id, [employee.id])
    from app.payroll.services import approvals

    approvals.reroute_after_access_change(db, principal.company_id)  # other modules too (F-29, EC-RUN-21)

    if audit:
        AuditLogService.log(
            db, principal, "Organization", "MANAGER_CHANGED",
            f"{employee.full_name}: manager {old_manager_id or 'none'} -> {manager_id or 'none'}",
            "employee", employee.id,
        )
    if employee.user_id:
        NotificationService.create(
            db, employee.user_id, "Reporting line updated",
            f"You now report to {new_manager.full_name}." if new_manager else "You no longer have a primary manager.",
            "organization", "/organization",
        )
    if new_manager and new_manager.user_id:
        NotificationService.create(
            db, new_manager.user_id, "New direct report", f"{employee.full_name} now reports to you.",
            "organization", "/organization",
        )
    return employee


def org_nodes(db: Session, principal: Principal, parent_id: Optional[int], q: Optional[str] = None) -> List[dict]:
    """Lazy org-chart loading: roots (parent None) or children of one node.

    A root is an in-scope employee whose manager is absent or out of scope,
    so restricted users see only their own subtree.
    """
    scope = authz.employee_scope(db, principal, "employee:read")
    if scope.is_empty:
        return []
    manager = aliased(Employee)
    query = db.query(Employee).filter(
        Employee.company_id == principal.company_id,
        Employee.is_active.is_(True),
        scope.clause(Employee.id),
    )
    if q:
        term = f"%{q.strip()}%"
        query = query.filter(
            or_(Employee.full_name.ilike(term), Employee.position.ilike(term))
        )
    elif parent_id is None:
        in_scope_managers = db.query(manager.id).filter(
            manager.company_id == principal.company_id, scope.clause(manager.id)
        )
        query = query.filter(or_(Employee.manager_id.is_(None), Employee.manager_id.notin_(in_scope_managers)))
    else:
        query = query.filter(Employee.manager_id == parent_id)
    nodes = query.order_by(Employee.first_name, Employee.last_name).limit(500).all()

    ids = [n.id for n in nodes]
    manager_ids = {n.manager_id for n in nodes if n.manager_id is not None}
    visible_manager_ids = set()
    if manager_ids:
        visible_manager_ids = {
            row[0]
            for row in db.query(Employee.id).filter(
                Employee.company_id == principal.company_id,
                Employee.id.in_(manager_ids),
                scope.clause(Employee.id),
            ).all()
        }
    counts = dict(
        db.query(Employee.manager_id, func.count(Employee.id))
        .filter(Employee.manager_id.in_(ids), Employee.is_active.is_(True), scope.clause(Employee.id))
        .group_by(Employee.manager_id)
        .all()
    ) if ids else {}
    sec_info = _compute_security_info(db, principal.company_id, nodes, counts)
    return [
        {
            "id": n.id,
            "employee_code": n.employee_code,
            "full_name": n.full_name,
            "email": n.email,
            "position": n.position,
            "department_id": n.department_id,
            "department": n.department.name if n.department else None,
            "location": n.location,
            "manager_id": n.manager_id,
            "manager_name": n.manager.full_name if n.manager_id in visible_manager_ids and n.manager else None,
            "report_count": counts.get(n.id, 0),
            "has_photo": bool(n.photo_path),
            "security_level": sec_info.get(n.id, {}).get("security_level", "L5 - Contributor"),
            "security_tier": sec_info.get(n.id, {}).get("security_tier", 5),
            "role_name": sec_info.get(n.id, {}).get("role_name", "Employee"),
            "teams": sec_info.get(n.id, {}).get("teams", []),
        }
        for n in nodes
    ]


def _compute_security_info(db: Session, company_id: int, nodes: List[Employee], counts: dict) -> dict:
    from app.models.rbac import Role, UserRoleAssignment
    from app.models.teams import Team, TeamMembership
    from app.models.employee import Department
    from app.core.security import utcnow

    user_ids = [n.user_id for n in nodes if n.user_id]
    emp_ids = [n.id for n in nodes]
    now = utcnow()
    today = now.date()

    roles_by_user = {}
    if user_ids:
        rows = (
            db.query(UserRoleAssignment.user_id, Role.name, Role.system_key, Role.is_full_access)
            .join(Role, Role.id == UserRoleAssignment.role_id)
            .filter(
                Role.company_id == company_id,
                UserRoleAssignment.company_id == company_id,
                UserRoleAssignment.user_id.in_(user_ids),
                UserRoleAssignment.effective_from <= now,
                or_(UserRoleAssignment.effective_until.is_(None), UserRoleAssignment.effective_until > now),
            )
            .all()
        )
        for uid, rname, skey, is_full in rows:
            roles_by_user.setdefault(uid, []).append({
                "name": rname,
                "system_key": skey,
                "is_full_access": is_full,
            })

    dept_heads = set(
        r[0] for r in db.query(Department.head_employee_id).filter(
            Department.company_id == company_id,
            Department.head_employee_id.in_(emp_ids),
        ).all() if r[0] is not None
    )

    teams_by_emp = {}
    lead_emps = set()
    if emp_ids:
        memberships = (
            db.query(TeamMembership.employee_id, Team.name, TeamMembership.member_role)
            .join(Team, Team.id == TeamMembership.team_id)
            .filter(
                Team.company_id == company_id,
                Team.status == "ACTIVE",
                TeamMembership.employee_id.in_(emp_ids),
                TeamMembership.effective_from <= today,
                or_(TeamMembership.effective_to.is_(None), TeamMembership.effective_to >= today),
            )
            .all()
        )
        for eid, tname, mrole in memberships:
            teams_by_emp.setdefault(eid, []).append(tname)
            if mrole in ("LEAD", "DEPUTY_LEAD"):
                lead_emps.add(eid)

    result = {}
    for n in nodes:
        u_roles = roles_by_user.get(n.user_id, [])
        role_priority = {
            "company_admin": 0, "hr_admin": 1, "payroll_manager": 2, "manager": 3, "employee": 9,
        }
        primary = min(u_roles, key=lambda r: role_priority.get(r.get("system_key"), 5)) if u_roles else None
        primary_role = primary["name"] if primary else ("Manager" if counts.get(n.id, 0) > 0 else "Employee")
        is_admin = any(r.get("is_full_access") or r.get("system_key") == "company_admin" for r in u_roles)
        is_hr_or_payroll = any(r.get("system_key") in ("hr_admin", "payroll_manager") for r in u_roles)
        is_mgr = any(r.get("system_key") == "manager" for r in u_roles) or (counts.get(n.id, 0) > 0)
        is_lead = n.id in lead_emps or n.id in dept_heads

        if is_admin or (n.manager_id is None and counts.get(n.id, 0) > 0):
            sec_level = "L1 - Executive / Admin"
            sec_tier = 1
        elif is_hr_or_payroll or n.id in dept_heads:
            sec_level = "L2 - Operations Admin"
            sec_tier = 2
        elif is_mgr:
            sec_level = "L3 - People Manager"
            sec_tier = 3
        elif is_lead:
            sec_level = "L4 - Team Lead"
            sec_tier = 4
        else:
            sec_level = "L5 - Contributor"
            sec_tier = 5

        result[n.id] = {
            "role_name": primary_role,
            "security_level": sec_level,
            "security_tier": sec_tier,
            "teams": teams_by_emp.get(n.id, []),
        }
    return result


def full_hierarchy(
    db: Session,
    principal: Principal,
    department_id: Optional[int] = None,
    team_id: Optional[int] = None,
    q: Optional[str] = None,
) -> dict:
    scope = authz.employee_scope(db, principal, "employee:read")
    if scope.is_empty:
        return {
            "nodes": [],
            "roots": [],
            "departments": [],
            "teams": [],
            "summary": {"total_employees": 0, "total_managers": 0, "max_depth": 0},
            "can_manage": principal.has("org:manage_hierarchy"),
        }

    from app.models.employee import Department
    from app.models.teams import Team, TeamMembership
    from app.core.security import utcnow
    today = utcnow().date()

    base_query = (
        db.query(Employee)
        .options(joinedload(Employee.department), joinedload(Employee.manager))
        .filter(
            Employee.company_id == principal.company_id,
            Employee.is_active.is_(True),
            scope.clause(Employee.id),
        )
    )

    # Filter choices are derived only from the caller's employee scope.  This is
    # deliberately not a company-wide department/team directory: a self- or
    # reporting-tree scoped user must not learn names outside their subtree.
    scope_rows = base_query.all()
    scope_ids = {employee.id for employee in scope_rows}
    query = base_query

    if department_id:
        query = query.filter(Employee.department_id == department_id)
    if team_id:
        member_ids = db.query(TeamMembership.employee_id).filter(
            TeamMembership.company_id == principal.company_id,
            TeamMembership.team_id == team_id,
            TeamMembership.effective_from <= today,
            or_(TeamMembership.effective_to.is_(None), TeamMembership.effective_to >= today)
        )
        query = query.filter(Employee.id.in_(member_ids))
    if q:
        term = f"%{q.strip()}%"
        query = query.filter(
            or_(
                Employee.first_name.ilike(term),
                Employee.last_name.ilike(term),
                Employee.position.ilike(term),
                Employee.employee_code.ilike(term),
                Employee.email.ilike(term),
            )
        )

    all_nodes = query.order_by(Employee.first_name, Employee.last_name).all()
    filtered_ids = {n.id for n in all_nodes}

    counts = dict(
        db.query(Employee.manager_id, func.count(Employee.id))
        .filter(Employee.manager_id.in_(scope_ids), Employee.is_active.is_(True), scope.clause(Employee.id))
        .group_by(Employee.manager_id)
        .all()
    ) if scope_ids else {}

    sec_info = _compute_security_info(db, principal.company_id, all_nodes, counts)

    children_map = {}
    for n in all_nodes:
        if n.manager_id in filtered_ids:
            children_map.setdefault(n.manager_id, []).append(n.id)

    root_ids = [n.id for n in all_nodes if n.manager_id is None or n.manager_id not in filtered_ids]

    level_map = {}
    max_depth = 0

    def assign_depth(nid: int, current_depth: int, visited: set):
        nonlocal max_depth
        if nid in visited:
            return
        visited.add(nid)
        level_map[nid] = current_depth
        if current_depth > max_depth:
            max_depth = current_depth
        for cid in children_map.get(nid, []):
            assign_depth(cid, current_depth + 1, visited)

    visited_set = set()
    for r_id in root_ids:
        assign_depth(r_id, 0, visited_set)
    for n in all_nodes:
        if n.id not in visited_set:
            assign_depth(n.id, 0, visited_set)

    scoped_department_ids = {n.department_id for n in scope_rows if n.department_id is not None}
    depts = [
        {"id": d.id, "name": d.name, "count": sum(1 for n in scope_rows if n.department_id == d.id)}
        for d in db.query(Department).filter(
            Department.company_id == principal.company_id,
            Department.id.in_(scoped_department_ids or [-1]),
        ).order_by(Department.name).all()
    ]

    team_counts = dict(
        db.query(TeamMembership.team_id, func.count(TeamMembership.employee_id))
        .filter(
            TeamMembership.company_id == principal.company_id,
            TeamMembership.employee_id.in_(scope_ids or [-1]),
            TeamMembership.effective_from <= today,
            or_(TeamMembership.effective_to.is_(None), TeamMembership.effective_to >= today)
        )
        .group_by(TeamMembership.team_id)
        .all()
    ) if scope_ids else {}

    teams_query = db.query(Team).filter(
        Team.company_id == principal.company_id,
        Team.status == "ACTIVE",
        Team.id.in_(set(team_counts) or [-1]),
    ).order_by(Team.name).all()

    teams_list = [
        {"id": t.id, "name": t.name, "code": t.code, "count": team_counts.get(t.id, 0)}
        for t in teams_query
    ]

    manage_scope = authz.employee_scope(db, principal, "org:manage_hierarchy")
    manageable_ids = set()
    if not manage_scope.is_empty and filtered_ids:
        manageable_ids = {
            row[0] for row in db.query(Employee.id).filter(
                Employee.company_id == principal.company_id,
                Employee.id.in_(filtered_ids),
                manage_scope.clause(Employee.id),
            ).all()
        }
    if principal.has("org:manage_hierarchy") and principal.employee_id in filtered_ids:
        # A manager may attach reports to themselves, but assign_manager still
        # forbids changing their own upward reporting line.
        manageable_ids.add(principal.employee_id)

    serialized_nodes = []
    for n in all_nodes:
        info = sec_info.get(n.id, {})
        serialized_nodes.append({
            "id": n.id,
            "employee_code": n.employee_code,
            "full_name": n.full_name,
            "first_name": n.first_name,
            "last_name": n.last_name,
            "email": n.email,
            "position": n.position,
            "department_id": n.department_id,
            "department": n.department.name if n.department else None,
            "location": n.location,
            "manager_id": n.manager_id,
            "manager_name": n.manager.full_name if n.manager and n.manager_id in scope_ids else None,
            "report_count": counts.get(n.id, 0),
            "children_ids": children_map.get(n.id, []),
            "has_photo": bool(n.photo_path),
            "level": level_map.get(n.id, 0),
            "security_level": info.get("security_level", "L5 - Contributor"),
            "security_tier": info.get("security_tier", 5),
            "role_name": info.get("role_name", "Employee"),
            "teams": info.get("teams", []),
            "can_manage": n.id in manageable_ids,
        })

    return {
        "nodes": serialized_nodes,
        "roots": root_ids,
        "departments": depts,
        "teams": teams_list,
        "summary": {
            "total_employees": len(all_nodes),
            "total_managers": sum(1 for n in all_nodes if counts.get(n.id, 0) > 0),
            "max_depth": max_depth,
        },
        "can_manage": bool(manageable_ids),
    }


def bulk_assign_reports(
    db: Session,
    principal: Principal,
    manager_id: Optional[int],
    employee_ids: List[int],
) -> dict:
    if not principal.has("org:manage_hierarchy"):
        raise forbidden()

    if manager_id is not None and manager_id in employee_ids:
        raise bad_request("A manager cannot be included in their own report list.", "SELF_REPORTING")

    manager = None
    if manager_id is not None:
        manager = _employee_in_company(db, principal.company_id, manager_id)
        if not manager.is_active:
            raise bad_request("The new manager must be an active employee.")
        if not (
            authz.is_self(principal, manager.id)
            or authz.can_access_employee(db, principal, "org:manage_hierarchy", manager.id)
        ):
            raise not_found("Manager")

    # Resolve and validate the whole batch before changing a row.  A typo or a
    # cycle therefore cannot leave half of a bulk operation applied.
    employees = [
        authz.ensure_employee_access(db, principal, "org:manage_hierarchy", emp_id)
        for emp_id in employee_ids
    ]
    if any(authz.is_self(principal, employee.id) for employee in employees):
        raise forbidden("You cannot change your own reporting line.")
    if manager is not None:
        for employee in employees:
            if hierarchy.would_create_cycle(db, principal.company_id, employee.id, manager.id):
                raise bad_request(CYCLE_MESSAGE, "HIERARCHY_CYCLE")

    updated = []
    for employee in employees:
        assign_manager(db, principal, employee, manager_id, audit=True)
        updated.append(employee.id)

    db.commit()
    return {
        "success": True,
        "assigned_count": len(updated),
        "manager_id": manager_id,
        "updated_ids": updated,
        "errors": [],
    }


def management_chain(db: Session, principal: Principal, employee_id: int) -> List[dict]:
    authz.ensure_employee_access(db, principal, "employee:read", employee_id)
    chain_ids = hierarchy.management_chain(db, principal.company_id, employee_id)
    if not chain_ids:
        return []
    scope = authz.employee_scope(db, principal, "employee:read")
    people = {
        e.id: e for e in db.query(Employee).filter(
            Employee.company_id == principal.company_id,
            Employee.id.in_(chain_ids),
            scope.clause(Employee.id),
        ).all()
    }
    return [
        {"id": i, "full_name": people[i].full_name, "position": people[i].position}
        for i in chain_ids
        if i in people
    ]


def manager_history(db: Session, principal: Principal, employee_id: int) -> List[dict]:
    authz.ensure_employee_access(db, principal, "employee:read", employee_id)
    rows = (
        db.query(ManagerHistory, Employee)
        .outerjoin(Employee, Employee.id == ManagerHistory.manager_id)
        .filter(ManagerHistory.employee_id == employee_id)
        .order_by(ManagerHistory.effective_from.desc())
        .all()
    )
    manager_ids = {h.manager_id for h, _ in rows if h.manager_id is not None}
    scope = authz.employee_scope(db, principal, "employee:read")
    visible_manager_ids = {
        row[0] for row in db.query(Employee.id).filter(
            Employee.company_id == principal.company_id,
            Employee.id.in_(manager_ids or [-1]),
            scope.clause(Employee.id),
        ).all()
    }
    return [
        {
            "manager_id": h.manager_id,
            "manager_name": m.full_name if m else None,
            "effective_from": h.effective_from.isoformat(),
            "effective_until": h.effective_until.isoformat() if h.effective_until else None,
        }
        for h, m in rows
        if h.manager_id is None or h.manager_id in visible_manager_ids
    ]


# ---------------------------------------------------------------------------
# Secondary reporting lines
# ---------------------------------------------------------------------------
def list_secondary_lines(db: Session, principal: Principal, employee_id: Optional[int] = None) -> List[dict]:
    scope = authz.employee_scope(db, principal, "employee:read")
    emp, mgr = aliased(Employee), aliased(Employee)
    query = (
        db.query(SecondaryReportingLine, emp, mgr)
        .join(emp, emp.id == SecondaryReportingLine.employee_id)
        .join(mgr, mgr.id == SecondaryReportingLine.manager_id)
        .filter(
            SecondaryReportingLine.company_id == principal.company_id,
            scope.clause(emp.id),
            scope.clause(mgr.id),
        )
    )
    if employee_id:
        query = query.filter(SecondaryReportingLine.employee_id == employee_id)
    return [
        {
            "id": line.id,
            "employee_id": e.id,
            "employee_name": e.full_name,
            "manager_id": m.id,
            "manager_name": m.full_name,
            "relation_type": line.relation_type,
            "effective_from": line.effective_from.isoformat(),
            "effective_until": line.effective_until.isoformat() if line.effective_until else None,
        }
        for line, e, m in query.order_by(SecondaryReportingLine.effective_from.desc()).all()
    ]


def add_secondary_line(db: Session, principal: Principal, data) -> SecondaryReportingLine:
    employee = authz.ensure_employee_access(db, principal, "org:manage_hierarchy", data.employee_id)
    manager = _employee_in_company(db, principal.company_id, data.manager_id)
    if manager.id == employee.id:
        raise bad_request("An employee cannot report to themselves.", "SELF_REPORTING")
    if not manager.is_active:
        raise bad_request("The manager must be an active employee.")
    if data.effective_until and data.effective_until < data.effective_from:
        raise bad_request("End date must be after the start date.")
    duplicate = db.query(SecondaryReportingLine.id).filter(
        SecondaryReportingLine.employee_id == employee.id,
        SecondaryReportingLine.manager_id == manager.id,
        SecondaryReportingLine.relation_type == data.relation_type,
        or_(SecondaryReportingLine.effective_until.is_(None), SecondaryReportingLine.effective_until >= date.today()),
    ).first()
    if duplicate:
        raise conflict("This reporting line already exists.")
    line = SecondaryReportingLine(
        company_id=principal.company_id,
        employee_id=employee.id,
        manager_id=manager.id,
        relation_type=data.relation_type,
        effective_from=data.effective_from,
        effective_until=data.effective_until,
        created_by_user_id=principal.user_id,
    )
    db.add(line)
    db.flush()
    AuditLogService.log(
        db, principal, "Organization", "SECONDARY_LINE_ADDED",
        f"{employee.full_name} has {data.relation_type} manager {manager.full_name}", "employee", employee.id,
    )
    return line


def end_secondary_line(db: Session, principal: Principal, line_id: int) -> None:
    line = db.query(SecondaryReportingLine).filter(
        SecondaryReportingLine.company_id == principal.company_id, SecondaryReportingLine.id == line_id
    ).first()
    if line is None:
        raise not_found("Reporting line")
    authz.ensure_employee_access(db, principal, "org:manage_hierarchy", line.employee_id)
    line.effective_until = max(line.effective_from, date.today())
    AuditLogService.log(db, principal, "Organization", "SECONDARY_LINE_ENDED", f"Ended reporting line #{line.id}", "employee", line.employee_id)


# ---------------------------------------------------------------------------
# Approval delegations
# ---------------------------------------------------------------------------
def active_delegators_for(
    db: Session, delegate_employee_id: Optional[int], on: Optional[date] = None, module: str = "leave"
) -> List[int]:
    """Employees whose `module` approvals the delegate may act on, on `on` (revoked or expired never count)."""
    if delegate_employee_id is None:
        return []
    query = db.query(ApprovalDelegation.delegator_employee_id, ApprovalDelegation.starts_on,
                     ApprovalDelegation.ends_on).filter(
        ApprovalDelegation.delegate_employee_id == delegate_employee_id,
        ApprovalDelegation.module == module,
        ApprovalDelegation.revoked_at.is_(None),
    )
    if on is not None:
        return [r[0] for r in query.filter(ApprovalDelegation.starts_on <= on, ApprovalDelegation.ends_on >= on)]
    # Without a date, each delegation is in force on its delegator's own today (LH BUG-07), not the server's.
    from app.services.org_structure_service import employee_local_today

    active = []
    for delegator_id, starts_on, ends_on in query.all():
        delegator = db.get(Employee, delegator_id)
        if delegator is not None and starts_on <= employee_local_today(db, delegator) <= ends_on:
            active.append(delegator_id)
    return active


def delegation_candidates(db: Session, principal: Principal, q: Optional[str] = None) -> List[dict]:
    """People the caller may delegate approvals to (minimal fields only).

    With company-wide delegation rights: any active employee with a login.
    Otherwise: the caller's management chain, peers (same manager) and the
    people already in their read scope, so a team lead can delegate to their
    own manager or a fellow lead without gaining directory access.
    """
    if not principal.has("delegation:manage"):
        raise forbidden()
    query = db.query(Employee).filter(
        Employee.company_id == principal.company_id,
        Employee.is_active.is_(True),
        Employee.user_id.is_not(None),
    )
    if principal.employee_id is not None:
        query = query.filter(Employee.id != principal.employee_id)
    if Scope.COMPANY.value not in principal.scopes("delegation:manage"):
        chain = hierarchy.management_chain(db, principal.company_id, principal.employee_id) if principal.employee_id else []
        scope = authz.employee_scope(db, principal, "employee:read")
        conditions = [scope.clause(Employee.id), Employee.id.in_(chain or [-1])]
        if chain:
            conditions.append(Employee.manager_id == chain[0])
        query = query.filter(or_(*conditions))
    if q:
        term = f"%{q.strip()}%"
        query = query.filter(or_(Employee.full_name.ilike(term), Employee.position.ilike(term)))
    return [
        {"id": e.id, "full_name": e.full_name, "position": e.position, "employee_code": e.employee_code}
        for e in query.order_by(Employee.first_name, Employee.last_name).limit(20).all()
    ]


def list_delegations(db: Session, principal: Principal) -> List[dict]:
    delegator, delegate = aliased(Employee), aliased(Employee)
    query = (
        db.query(ApprovalDelegation, delegator, delegate)
        .join(delegator, delegator.id == ApprovalDelegation.delegator_employee_id)
        .join(delegate, delegate.id == ApprovalDelegation.delegate_employee_id)
        .filter(ApprovalDelegation.company_id == principal.company_id)
    )
    if Scope.COMPANY.value not in principal.scopes("delegation:manage"):
        me = principal.employee_id or -1
        query = query.filter(
            or_(ApprovalDelegation.delegator_employee_id == me, ApprovalDelegation.delegate_employee_id == me)
        )
    today = date.today()
    return [
        {
            "id": d.id,
            "delegator_employee_id": a.id,
            "delegator_name": a.full_name,
            "delegate_employee_id": b.id,
            "delegate_name": b.full_name,
            "module": d.module,
            "starts_on": d.starts_on.isoformat(),
            "ends_on": d.ends_on.isoformat(),
            "reason": d.reason,
            "active": d.revoked_at is None and d.starts_on <= today <= d.ends_on,
            "revoked": d.revoked_at is not None,
        }
        for d, a, b in query.order_by(ApprovalDelegation.starts_on.desc()).all()
    ]


def create_delegation(db: Session, principal: Principal, data) -> ApprovalDelegation:
    if not principal.has("delegation:manage"):
        raise forbidden()
    company_wide = Scope.COMPANY.value in principal.scopes("delegation:manage")
    delegator_id = data.delegator_employee_id or principal.employee_id
    if delegator_id is None:
        raise bad_request("Choose whose approvals to delegate.")
    if not company_wide and delegator_id != principal.employee_id:
        raise forbidden("You can only delegate your own approvals.")
    if data.ends_on < data.starts_on:
        raise bad_request("End date must be on or after the start date.")
    if data.delegate_employee_id == delegator_id:
        raise bad_request("You cannot delegate to yourself.")
    delegator = _employee_in_company(db, principal.company_id, delegator_id)
    delegate = _employee_in_company(db, principal.company_id, data.delegate_employee_id)
    if not delegate.is_active or delegate.user_id is None:
        raise bad_request("The delegate must be an active employee with a login.")
    overlap = db.query(ApprovalDelegation.id).filter(
        ApprovalDelegation.delegator_employee_id == delegator.id,
        ApprovalDelegation.delegate_employee_id == delegate.id,
        ApprovalDelegation.revoked_at.is_(None),
        and_(ApprovalDelegation.starts_on <= data.ends_on, ApprovalDelegation.ends_on >= data.starts_on),
    ).first()
    if overlap:
        raise conflict("An overlapping delegation already exists.")
    delegation = ApprovalDelegation(
        company_id=principal.company_id,
        delegator_employee_id=delegator.id,
        delegate_employee_id=delegate.id,
        module="leave",
        starts_on=data.starts_on,
        ends_on=data.ends_on,
        reason=data.reason,
        created_by_user_id=principal.user_id,
    )
    db.add(delegation)
    db.flush()
    AuditLogService.log(
        db, principal, "Organization", "DELEGATION_CREATED",
        f"{delegator.full_name} delegated leave approvals to {delegate.full_name} ({data.starts_on} to {data.ends_on})",
        "delegation", delegation.id,
    )
    NotificationService.create(
        db, delegate.user_id, "Approvals delegated to you",
        f"You can act on {delegator.full_name}'s leave approvals from {data.starts_on} to {data.ends_on}.",
        "leave", "/leaves",
    )
    return delegation


def revoke_delegation(db: Session, principal: Principal, delegation_id: int) -> None:
    delegation = db.query(ApprovalDelegation).filter(
        ApprovalDelegation.company_id == principal.company_id, ApprovalDelegation.id == delegation_id
    ).first()
    if delegation is None:
        raise not_found("Delegation")
    company_wide = Scope.COMPANY.value in principal.scopes("delegation:manage")
    if not company_wide and delegation.delegator_employee_id != principal.employee_id:
        raise not_found("Delegation")
    delegation.revoked_at = utcnow()
    AuditLogService.log(db, principal, "Organization", "DELEGATION_REVOKED", f"Revoked delegation #{delegation.id}", "delegation", delegation.id)

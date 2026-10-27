"""Organisation hierarchy: reporting lines, secondary managers, delegations."""

from datetime import date
from typing import List, Optional

from sqlalchemy import and_, func, or_
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, aliased

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
    counts = dict(
        db.query(Employee.manager_id, func.count(Employee.id))
        .filter(Employee.manager_id.in_(ids), Employee.is_active.is_(True), scope.clause(Employee.id))
        .group_by(Employee.manager_id)
        .all()
    ) if ids else {}
    return [
        {
            "id": n.id,
            "full_name": n.full_name,
            "position": n.position,
            "department": n.department.name if n.department else None,
            "location": n.location,
            "manager_id": n.manager_id,
            "report_count": counts.get(n.id, 0),
            "has_photo": bool(n.photo_path),
        }
        for n in nodes
    ]


def management_chain(db: Session, principal: Principal, employee_id: int) -> List[dict]:
    authz.ensure_employee_access(db, principal, "employee:read", employee_id)
    chain_ids = hierarchy.management_chain(db, principal.company_id, employee_id)
    if not chain_ids:
        return []
    people = {e.id: e for e in db.query(Employee).filter(Employee.id.in_(chain_ids)).all()}
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
    return [
        {
            "manager_id": h.manager_id,
            "manager_name": m.full_name if m else None,
            "effective_from": h.effective_from.isoformat(),
            "effective_until": h.effective_until.isoformat() if h.effective_until else None,
        }
        for h, m in rows
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
        .filter(SecondaryReportingLine.company_id == principal.company_id, scope.clause(emp.id))
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

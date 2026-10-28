"""Teams, dated memberships and the team-lead resolver (X-M1; owner X 01 §3, §4, §7, §11; spine §3.9, §4.11, §7.5).

* Teams: ``team:manage`` creates, edits, re-parents (dated, ``team_parent_history``) and archives teams whose home unit
  is in scope (NULL home needs COMPANY). Archive is refused with 409 ``TEAM_IN_USE`` while the team has active or
  future memberships, an active rotation assignment or an open roster period; role-assignment targets alone never
  block, but the archive then needs the typed confirmation (the team code, L3; X-ROLE-31).
* Memberships: ``team:membership:manage`` at C, LE / BR (home), ASSIGNED_TEAMS changes every row; at TEAM_TREE only
  MEMBER rows of teams the actor leads (§3.2). Nobody changes their own rows (SG-X3). Rows are never deleted: ending
  sets ``effective_to``, cancelling a future row sets ``effective_to = effective_from - 1`` (TM-7), a role or flag
  change ends the row the day before and starts a new one (TM-3). The DB guard maps to 409 ``MEMBERSHIP_OVERLAP``,
  ``PRIMARY_TEAM_CONFLICT``, ``ROUTING_LEAD_CONFLICT``; parent cycles to 409 ``TEAM_CYCLE``.
* Every applied change bumps ``users.permission_version`` of the team's leads and of ASSIGNED_TEAMS target holders and
  reroutes open approvals (§3.2); audit module ``Teams`` (``TEAM_SAVED``, ``TEAM_ARCHIVED``, ``MEMBERSHIP_CHANGED``)
  and outbox ``team.changed`` / ``team.membership.changed`` (ids only).
* ``resolve_team_lead`` is the one resolver of the approver type ``team_lead`` (§7.2): team -> eligible LEAD
  candidates (routing lead first) -> fallback ``approvals.team_lead.fallback``. The engine storage of the approver
  type arrives with LH-M5a (G-11, JC-AS-46); X-M1 ships and tests the resolver only (SP-AS-170).

Dates are employee-local (the subject's or the actor's local today; never the server date). Services never commit.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core import authz
from app.core.concurrency import check_row_version
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.permission_registry import Scope
from app.core.principal import Principal
from app.models.company import Company
from app.models.employee import Employee
from app.models.org import Branch, LegalEntity, RoleAssignmentTarget
from app.models.rbac import UserRoleAssignment
from app.models.teams import LEAD_ROLES, TEAM_MEMBER_ROLES, Team, TeamMembership, TeamParentHistory
from app.models.user import User
from app.payroll.services import jobs, outbox
from app.services.audit_log_service import AuditLogService

MODULE = "Teams"
READ, MANAGE, MEMBERS = "team:read", "team:manage", "team:membership:manage"
ONE_DAY = timedelta(days=1)
MAX_CANDIDATES = 20

# Teams & access settings (X 01 §13) with their registry defaults. Their storage and the Settings rail item ship with
# the AS-M3 settings sections (G-07, IG-X-02); until then every tenant runs on these defaults.
SETTING_DEFAULTS: Dict[str, Any] = {
    "team.lead_change.requires_approval": False,
    "team.primary_team.required": False,
    "approvals.team_lead.fallback": "DEPUTY_PARENT_NEXT",
    "approvals.team_lead.skip_on_leave": True,
    "access.changes.requires_approval": False,
    "delegation.max_days": 30,
}
FALLBACKS = ("DEPUTY_PARENT_NEXT", "NEXT_STEP", "PRIMARY_MANAGER")
# The module approve codes a team-lead step needs (JC-AS-14; §7.2 step 4).
MODULE_APPROVE_CODES = {"attendance": "attendance:approve", "timesheet": "attendance:approve",
                        "shift_request": "roster:approve", "leave": "leave:approve", "overtime": "overtime:approve"}


def setting(db: Session, company_id: int, key: str) -> Any:
    return SETTING_DEFAULTS[key]


# --------------------------------------------------------------------------- dates and helpers


def actor_today(db: Session, principal: Principal) -> date:
    """The actor's local today (linked employee's work zone, else the company zone)."""
    if principal.extra and principal.extra.get("local_today") is not None:
        return principal.extra["local_today"]
    from app.core.principal import principal_local_today

    return principal_local_today(db, principal.employee_id, db.get(Company, principal.company_id))


def _active_on(model, day: date):
    return (model.effective_from <= day) & (or_(model.effective_to.is_(None), model.effective_to >= day))


def _overlaps(model, start: date, end: Optional[date]):
    """Rows sharing a date with [start, end] (open end = forever); cancelled rows never match."""
    conditions = [or_(model.effective_to.is_(None), model.effective_to >= start),
                  or_(model.effective_to.is_(None), model.effective_to >= model.effective_from)]
    if end is not None:
        conditions.append(model.effective_from <= end)
    return conditions


def membership_state(row: TeamMembership, today: date) -> str:
    if row.effective_to is not None and row.effective_to < row.effective_from:
        return "CANCELLED"
    if row.effective_from > today:
        return "FUTURE"
    if row.effective_to is not None and row.effective_to < today:
        return "ENDED"
    return "ACTIVE"


_GUARD_ERRORS = {
    "membership_overlap": ("This person already has a membership of this team on these dates.", "MEMBERSHIP_OVERLAP"),
    "primary_team_conflict": ("This person already has a primary team on these dates.", "PRIMARY_TEAM_CONFLICT"),
    "routing_lead_conflict": ("The team already has a routing lead on these dates.", "ROUTING_LEAD_CONFLICT"),
    "ck_team_memberships_routing_lead": ("Only a lead can be the routing lead.", "ROUTING_LEAD_CONFLICT"),
    "team_cycle": ("A team cannot be placed under one of its own sub-teams.", "TEAM_CYCLE"),
    "uq_teams_code": ("Another team already uses this code.", "TEAM_CODE_TAKEN"),
}


def constraint_text(exc: Exception) -> str:
    """The violated constraint (trigger ``RAISE ... CONSTRAINT``) plus the message, for mapping to error codes."""
    orig = getattr(exc, "orig", exc)
    diag = getattr(orig, "diag", None)
    return f"{getattr(diag, 'constraint_name', None) or ''} {orig}"


def _flush(db: Session) -> None:
    """Flush in a savepoint and map the guard triggers and constraints to their 409 codes."""
    try:
        with db.begin_nested():
            db.flush()
    except Exception as exc:
        text = constraint_text(exc)
        for name, (detail, code) in _GUARD_ERRORS.items():
            if name in text:
                raise conflict(detail, code)
        raise


def _emit(db: Session, topic: str, payload: dict) -> None:
    outbox.emit(db, topic, payload)


def _employee_names(db: Session, company_id: int, ids: Iterable[int]) -> Dict[int, Employee]:
    ids = sorted(set(i for i in ids if i is not None))
    if not ids:
        return {}
    rows = db.query(Employee).filter(Employee.company_id == company_id, Employee.id.in_(ids)).all()
    return {e.id: e for e in rows}


def _name(employee: Optional[Employee]) -> Optional[str]:
    if employee is None:
        return None
    return " ".join(p for p in (employee.first_name, employee.last_name) if p)


# --------------------------------------------------------------------------- access


def _team_or_404(db: Session, principal: Principal, team_id: int) -> Team:
    team = db.query(Team).filter(Team.company_id == principal.company_id, Team.id == team_id).first()
    if team is None:
        raise not_found("Team")
    return team


def _home_covered(principal: Principal, code: str, team: Team) -> bool:
    """Home-unit rule of ``team:manage`` / ``team:membership:manage`` at LE / BR (NULL home needs COMPANY)."""
    if principal.is_company_admin or principal.has_company_scope(code):
        return True
    scopes = principal.scopes(code)
    if Scope.LEGAL_ENTITY.value in scopes and team.legal_entity_id is not None \
            and team.legal_entity_id in principal.scope_targets(code, Scope.LEGAL_ENTITY.value):
        return True
    if Scope.BRANCH.value in scopes and team.branch_id is not None \
            and team.branch_id in principal.scope_targets(code, Scope.BRANCH.value):
        return True
    return False


def lead_team_ids(db: Session, company_id: int, employee_id: Optional[int], on: date, *, tree: bool) -> Set[int]:
    if employee_id is None:
        return set()
    return {t for (t,) in db.execute(authz.lead_teams_select(company_id, employee_id, on, on, tree=tree))}


def membership_reach(db: Session, principal: Principal, team: Team) -> Optional[str]:
    """``LEADS`` (every row), ``MEMBERS`` (MEMBER rows only, TEAM_TREE holders) or None (§3.2)."""
    if _home_covered(principal, MEMBERS, team):
        return "LEADS"
    scopes = principal.scopes(MEMBERS)
    if Scope.ASSIGNED_TEAMS.value in scopes and team.id in principal.scope_targets(MEMBERS, Scope.ASSIGNED_TEAMS.value):
        return "LEADS"
    if Scope.TEAM_TREE.value in scopes and team.id in lead_team_ids(
            db, principal.company_id, principal.employee_id, actor_today(db, principal), tree=True):
        return "MEMBERS"
    return None


def _all_teams_visible(principal: Principal) -> bool:
    return principal.is_company_admin or any(principal.has_company_scope(c) for c in (READ, MANAGE, MEMBERS))


def visible_team_ids(db: Session, principal: Principal, on: date) -> Optional[Set[int]]:
    """None = every team; else the teams the principal may see: own teams, teams with a member in ``team:read``
    scope, teams the principal leads, home-unit teams of ``team:manage`` / ``team:membership:manage``, AT targets."""
    if _all_teams_visible(principal):
        return None
    cid = principal.company_id
    ids: Set[int] = set()
    if principal.employee_id is not None:
        ids |= {t for (t,) in db.query(TeamMembership.team_id).filter(
            TeamMembership.company_id == cid, TeamMembership.employee_id == principal.employee_id,
            *_overlaps(TeamMembership, on, None))}
        ids |= lead_team_ids(db, cid, principal.employee_id, on, tree=True)
    read_scopes = principal.scopes(READ) - {Scope.SELF.value}
    if read_scopes:
        scope = authz.employee_scope(db, principal, READ)
        if not scope.is_empty:
            ids |= {t for (t,) in db.query(TeamMembership.team_id).filter(
                TeamMembership.company_id == cid, _active_on(TeamMembership, on),
                scope.clause(TeamMembership.employee_id)).distinct()}
    for code in (READ, MEMBERS):
        ids |= set(principal.scope_targets(code, Scope.ASSIGNED_TEAMS.value))
    for code in (MANAGE, MEMBERS):
        les = principal.scope_targets(code, Scope.LEGAL_ENTITY.value)
        brs = principal.scope_targets(code, Scope.BRANCH.value)
        if les or brs:
            ids |= {t for (t,) in db.query(Team.id).filter(Team.company_id == cid, or_(
                Team.legal_entity_id.in_(sorted(les) or [0]), Team.branch_id.in_(sorted(brs) or [0])))}
    return ids


def _members_visible(db: Session, principal: Principal, team: Team, on: date) -> bool:
    """Member lists beyond the leads: team:read beyond SELF reaching the team, a lead of it, membership managers."""
    if _all_teams_visible(principal) or membership_reach(db, principal, team) is not None:
        return True
    if team.id in lead_team_ids(db, principal.company_id, principal.employee_id, on, tree=True):
        return True
    if team.id in principal.scope_targets(READ, Scope.ASSIGNED_TEAMS.value):
        return True
    if principal.scopes(READ) - {Scope.SELF.value}:
        scope = authz.employee_scope(db, principal, READ)
        return not scope.is_empty and db.query(TeamMembership.id).filter(
            TeamMembership.company_id == principal.company_id, TeamMembership.team_id == team.id,
            _active_on(TeamMembership, on), scope.clause(TeamMembership.employee_id)).first() is not None
    return False


def _deny(db: Session, principal: Principal, team: Team, allowed: bool, message: str) -> None:
    """404 for a team the principal cannot see (never reveal it), 403 for a visible one they cannot change."""
    if allowed:
        return
    visible = visible_team_ids(db, principal, actor_today(db, principal))
    if visible is not None and team.id not in visible:
        raise not_found("Team")
    raise forbidden(message)


def _ensure_visible(db: Session, principal: Principal, team: Team, on: date) -> None:
    visible = visible_team_ids(db, principal, on)
    if visible is not None and team.id not in visible:
        raise not_found("Team")


# --------------------------------------------------------------------------- serialization


def _leads_of(db: Session, company_id: int, team_ids: Sequence[int], on: date) -> Dict[int, List[dict]]:
    if not team_ids:
        return {}
    rows = db.query(TeamMembership).filter(
        TeamMembership.company_id == company_id, TeamMembership.team_id.in_(sorted(team_ids)),
        TeamMembership.member_role.in_(LEAD_ROLES), _active_on(TeamMembership, on)).all()
    names = _employee_names(db, company_id, [r.employee_id for r in rows])
    result: Dict[int, List[dict]] = {}
    for row in sorted(rows, key=lambda r: (r.member_role != "LEAD", not r.is_routing_lead, r.id)):
        result.setdefault(row.team_id, []).append({
            "employee_id": row.employee_id, "name": _name(names.get(row.employee_id)),
            "member_role": row.member_role, "routing": row.is_routing_lead})
    return result


def _member_counts(db: Session, company_id: int, team_ids: Sequence[int], on: date) -> Dict[int, int]:
    if not team_ids:
        return {}
    rows = db.query(TeamMembership.team_id, func.count(func.distinct(TeamMembership.employee_id))).filter(
        TeamMembership.company_id == company_id, TeamMembership.team_id.in_(sorted(team_ids)),
        _active_on(TeamMembership, on)).group_by(TeamMembership.team_id).all()
    return dict(rows)


def serialize_team(team: Team, leads: Sequence[dict] = (), member_count: Optional[int] = None) -> dict:
    lead_rows = [l for l in leads if l["member_role"] == "LEAD"]
    warnings = []
    if team.status == "ACTIVE" and not lead_rows:
        warnings.append("NO_LEAD")
    elif len(lead_rows) > 1 and not any(l["routing"] for l in lead_rows):
        warnings.append("NO_ROUTING_LEAD")
    return {
        "id": team.id, "code": team.code, "name": team.name, "team_type": team.team_type,
        "parent_team_id": team.parent_team_id, "legal_entity_id": team.legal_entity_id, "branch_id": team.branch_id,
        "site_id": team.site_id, "description": team.description, "status": team.status,
        "effective_from": team.effective_from.isoformat(),
        "effective_to": team.effective_to.isoformat() if team.effective_to else None,
        "row_version": team.row_version, "leads": list(leads), "member_count": member_count, "warnings": warnings,
    }


def serialize_membership(row: TeamMembership, today: date, employee: Optional[Employee] = None) -> dict:
    return {
        "id": row.id, "team_id": row.team_id, "employee_id": row.employee_id,
        "employee_name": _name(employee), "employee_code": employee.employee_code if employee else None,
        "member_role": row.member_role, "is_primary_team": row.is_primary_team,
        "is_routing_lead": row.is_routing_lead, "effective_from": row.effective_from.isoformat(),
        "effective_to": row.effective_to.isoformat() if row.effective_to else None,
        "state": membership_state(row, today), "reason": row.reason, "row_version": row.row_version,
    }


# --------------------------------------------------------------------------- side effects


def affected_user_ids(db: Session, company_id: int, team_ids: Iterable[int]) -> Set[int]:
    """Users whose reach a change of these teams moves: their leads (any date) and ASSIGNED_TEAMS target holders."""
    team_ids = sorted(set(team_ids))
    if not team_ids:
        return set()
    leads = {u for (u,) in db.query(Employee.user_id).join(
        TeamMembership, (TeamMembership.employee_id == Employee.id) & (TeamMembership.company_id == Employee.company_id)
    ).filter(Employee.company_id == company_id, TeamMembership.team_id.in_(team_ids),
             TeamMembership.member_role.in_(LEAD_ROLES), Employee.user_id.is_not(None))}
    holders = {u for (u,) in db.query(UserRoleAssignment.user_id).join(
        RoleAssignmentTarget, RoleAssignmentTarget.user_role_assignment_id == UserRoleAssignment.id
    ).filter(UserRoleAssignment.company_id == company_id, RoleAssignmentTarget.scope == Scope.ASSIGNED_TEAMS.value,
             RoleAssignmentTarget.target_id.in_(team_ids))}
    return leads | holders


def _touch(db: Session, company_id: int, team_ids: Iterable[int], extra_user_ids: Iterable[int] = ()) -> None:
    """Bump permission_version of everyone whose reach moved and reroute open approvals (§3.2)."""
    from app.services import rbac_service

    users = affected_user_ids(db, company_id, team_ids) | set(u for u in extra_user_ids if u)
    if users:
        db.query(User).filter(User.company_id == company_id, User.id.in_(sorted(users))).update(
            {User.permission_version: User.permission_version + 1}, synchronize_session=False)
    rbac_service.reroute_approvals(db, company_id)


def _user_of(db: Session, company_id: int, employee_id: int) -> Optional[int]:
    return db.query(Employee.user_id).filter(Employee.company_id == company_id, Employee.id == employee_id).scalar()


# --------------------------------------------------------------------------- teams


def _check_home(db: Session, principal: Principal, legal_entity_id: Optional[int], branch_id: Optional[int]) -> None:
    cid = principal.company_id
    if legal_entity_id is not None and db.query(LegalEntity.id).filter(
            LegalEntity.company_id == cid, LegalEntity.id == legal_entity_id).first() is None:
        raise not_found("Legal entity")
    if branch_id is not None:
        branch = db.query(Branch).filter(Branch.company_id == cid, Branch.id == branch_id).first()
        if branch is None:
            raise not_found("Branch")
        if legal_entity_id is not None and branch.legal_entity_id != legal_entity_id:
            raise bad_request("The branch belongs to another legal entity.", "BRANCH_ENTITY_MISMATCH")


def _parent_or_404(db: Session, principal: Principal, parent_id: Optional[int]) -> Optional[Team]:
    if parent_id is None:
        return None
    parent = _team_or_404(db, principal, parent_id)
    if parent.status != "ACTIVE":
        raise bad_request("The parent team is archived.", "TEAM_ARCHIVED")
    if not _home_covered(principal, MANAGE, parent):
        raise not_found("Team")
    return parent


def _ancestor_ids(db: Session, company_id: int, team_id: int) -> List[int]:
    seen, cursor = [], db.query(Team.parent_team_id).filter(Team.company_id == company_id, Team.id == team_id).scalar()
    while cursor is not None and cursor not in seen and len(seen) < 64:
        seen.append(cursor)
        cursor = db.query(Team.parent_team_id).filter(Team.company_id == company_id, Team.id == cursor).scalar()
    return seen


def create_team(db: Session, principal: Principal, data) -> dict:
    today = actor_today(db, principal)
    _check_home(db, principal, data.legal_entity_id, data.branch_id)
    team = Team(company_id=principal.company_id, code=data.code.upper(), name=data.name.strip(),
                team_type=data.team_type, legal_entity_id=data.legal_entity_id, branch_id=data.branch_id,
                description=data.description, status="ACTIVE", effective_from=data.effective_from or today,
                created_by_user_id=principal.user_id, row_version=1)
    if not _home_covered(principal, MANAGE, team):
        raise forbidden("You can create teams only for the legal entities or branches you manage.")
    parent = _parent_or_404(db, principal, data.parent_team_id)
    team.parent_team_id = parent.id if parent else None
    db.add(team)
    _flush(db)
    db.add(TeamParentHistory(company_id=principal.company_id, team_id=team.id, parent_team_id=team.parent_team_id,
                             effective_from=team.effective_from, created_by_user_id=principal.user_id))
    _flush(db)
    AuditLogService.log(db, principal, MODULE, "TEAM_SAVED", f"Created team {team.code}", "team", team.id,
                        details={"before": None, "after": _audit_team(team)})
    _emit(db, "team.changed", {"team_id": team.id})
    return serialize_team(team, [], 0)


def _audit_team(team: Team) -> dict:
    return {"code": team.code, "name": team.name, "team_type": team.team_type, "parent_team_id": team.parent_team_id,
            "legal_entity_id": team.legal_entity_id, "branch_id": team.branch_id, "status": team.status}


def update_team(db: Session, principal: Principal, team_id: int, data) -> dict:
    team = _team_or_404(db, principal, team_id)
    _deny(db, principal, team, _home_covered(principal, MANAGE, team), "You cannot edit this team.")
    check_row_version(team, data.row_version)
    if team.status != "ACTIVE":
        raise conflict("The team is archived.", "TEAM_ARCHIVED")
    before = _audit_team(team)
    today = actor_today(db, principal)
    if data.name is not None:
        team.name = data.name.strip()
    if data.team_type is not None:
        team.team_type = data.team_type
    if data.description is not None:
        team.description = data.description
    if data.legal_entity_id is not None or data.branch_id is not None:
        le = data.legal_entity_id if data.legal_entity_id is not None else team.legal_entity_id
        br = data.branch_id if data.branch_id is not None else team.branch_id
        _check_home(db, principal, le, br)
        team.legal_entity_id, team.branch_id = le, br
        if not _home_covered(principal, MANAGE, team):
            raise forbidden("The new home unit is outside the units you manage.")
    reparented = False
    if data.clear_parent or data.parent_team_id is not None:
        new_parent = None if data.clear_parent else _parent_or_404(db, principal, data.parent_team_id)
        new_id = new_parent.id if new_parent else None
        if new_id is not None and (new_id == team.id or team.id in _ancestor_ids(db, principal.company_id, new_id)):
            raise conflict("A team cannot be placed under one of its own sub-teams.", "TEAM_CYCLE")
        reparented = _reparent(db, principal, team, new_id, data.parent_effective_from or today + ONE_DAY, today)
    team.row_version += 1
    _flush(db)
    AuditLogService.log(db, principal, MODULE, "TEAM_SAVED", f"Edited team {team.code}", "team", team.id,
                        details={"before": before, "after": _audit_team(team)})
    _emit(db, "team.changed", {"team_id": team.id})
    if reparented:
        _touch(db, principal.company_id, [team.id] + _ancestor_ids(db, principal.company_id, team.id))
    leads = _leads_of(db, principal.company_id, [team.id], today)
    return serialize_team(team, leads.get(team.id, []), _member_counts(db, principal.company_id, [team.id], today)
                          .get(team.id, 0))


def _reparent(db: Session, principal: Principal, team: Team, parent_id: Optional[int], start: date,
              today: date) -> bool:
    """A dated re-parent (box P-10, X-ROLE-42): the parent link changes from ``start``; history keeps the old one."""
    if start < team.effective_from:
        raise bad_request("The new parent cannot apply before the team starts.", "INVALID_DATE")
    rows = db.query(TeamParentHistory).filter(
        TeamParentHistory.company_id == team.company_id, TeamParentHistory.team_id == team.id,
        *_overlaps(TeamParentHistory, start, None)).order_by(TeamParentHistory.effective_from).all()
    current = next((r for r in rows if r.effective_from <= start), None)
    if current is not None and current.parent_team_id == parent_id and current.effective_to is None:
        return False
    for row in rows:
        if row.effective_from >= start:
            row.effective_to = row.effective_from - ONE_DAY  # a later planned link is cancelled (empty window)
        else:
            row.effective_to = start - ONE_DAY
    db.add(TeamParentHistory(company_id=team.company_id, team_id=team.id, parent_team_id=parent_id,
                             effective_from=start, created_by_user_id=principal.user_id))
    if start <= today:
        team.parent_team_id = parent_id
    return True


def sync_parents(db: Session, company_id: int, on: date) -> int:
    """Apply parent links that start on or before ``on`` to ``teams.parent_team_id`` (job; idempotent)."""
    changed = 0
    for row in db.query(TeamParentHistory).filter(TeamParentHistory.company_id == company_id,
                                                   _active_on(TeamParentHistory, on)).all():
        team = db.get(Team, row.team_id)
        if team is not None and team.parent_team_id != row.parent_team_id:
            team.parent_team_id = row.parent_team_id
            changed += 1
    if changed:
        _flush(db)
    return changed


def _in_use(db: Session, team: Team, today: date) -> List[str]:
    from app.models.rosters import RosterPeriod, RotationAssignment

    reasons = []
    if db.query(TeamMembership.id).filter(
            TeamMembership.company_id == team.company_id, TeamMembership.team_id == team.id,
            *_overlaps(TeamMembership, today, None)).first() is not None:
        reasons.append("ACTIVE_MEMBERSHIPS")
    if db.query(RotationAssignment.id).filter(
            RotationAssignment.company_id == team.company_id, RotationAssignment.team_id == team.id,
            RotationAssignment.status == "ACTIVE").first() is not None:
        reasons.append("ROTATION_ASSIGNMENT")
    if db.query(RosterPeriod.id).filter(
            RosterPeriod.company_id == team.company_id, RosterPeriod.team_id == team.id,
            RosterPeriod.status.in_(("DRAFT", "PUBLISHED", "AMENDING"))).first() is not None:
        reasons.append("OPEN_ROSTER_PERIOD")
    return reasons


def _target_holders(db: Session, team: Team) -> List[dict]:
    from app.models.rbac import Role

    rows = db.query(UserRoleAssignment, User, Role).join(
        RoleAssignmentTarget, RoleAssignmentTarget.user_role_assignment_id == UserRoleAssignment.id
    ).join(User, User.id == UserRoleAssignment.user_id).join(Role, Role.id == UserRoleAssignment.role_id).filter(
        UserRoleAssignment.company_id == team.company_id, RoleAssignmentTarget.scope == Scope.ASSIGNED_TEAMS.value,
        RoleAssignmentTarget.target_id == team.id).all()
    return [{"user_id": u.id, "user_name": u.full_name, "role_id": r.id, "role_name": r.name,
             "assignment_id": a.id} for a, u, r in rows]


def archive_team(db: Session, principal: Principal, team_id: int, data) -> dict:
    """409 TEAM_IN_USE while in use; AT targets alone never block but need the typed code (X-ROLE-31)."""
    team = _team_or_404(db, principal, team_id)
    _deny(db, principal, team, _home_covered(principal, MANAGE, team), "You cannot archive this team.")
    check_row_version(team, data.row_version)
    if team.status == "ARCHIVED":
        raise conflict("The team is already archived.", "TEAM_ARCHIVED")
    today = actor_today(db, principal)
    reasons = _in_use(db, team, today)
    holders = _target_holders(db, team)
    if data.dry_run:
        return {"dry_run": True, "team_id": team.id, "in_use": reasons, "target_holders": holders,
                "confirm_code_required": bool(holders)}
    if reasons:
        raise AppError(409, "The team still has members, a rotation or an open roster period.", "TEAM_IN_USE", extra={"details": {"reasons": reasons}})
    if holders and (data.confirm_code or "").strip().upper() != team.code:
        raise bad_request("Type the team code to confirm: roles that name this team lose it.", "CONFIRMATION_REQUIRED")
    before = _audit_team(team)
    team.status = "ARCHIVED"
    team.effective_to = max(today, team.effective_from)
    team.row_version += 1
    _flush(db)
    AuditLogService.log(db, principal, MODULE, "TEAM_ARCHIVED", f"Archived team {team.code}", "team", team.id,
                        details={"before": before, "after": _audit_team(team), "target_holders": holders})
    _emit(db, "team.changed", {"team_id": team.id})
    _touch(db, principal.company_id, [team.id])
    return serialize_team(team, [], 0)


def list_teams(db: Session, principal: Principal, *, q: Optional[str] = None, team_type: Optional[str] = None,
               status: Optional[str] = None, legal_entity_id: Optional[int] = None, branch_id: Optional[int] = None,
               parent_team_id: Optional[int] = None, lead_employee_id: Optional[int] = None,
               member_employee_id: Optional[int] = None, has_lead: Optional[bool] = None,
               mine: bool = False, sort: str = "name", skip: int = 0, limit: int = 25) -> dict:
    cid = principal.company_id
    today = actor_today(db, principal)
    query = db.query(Team).filter(Team.company_id == cid)
    visible = visible_team_ids(db, principal, today)
    if visible is not None:
        query = query.filter(Team.id.in_(sorted(visible) or [0]))
    if q:
        like = f"%{q.strip().lower()}%"
        query = query.filter(or_(func.lower(Team.code).like(like), func.lower(Team.name).like(like)))
    if team_type:
        query = query.filter(Team.team_type == team_type)
    if status:
        query = query.filter(Team.status == status)
    if legal_entity_id is not None:
        query = query.filter(Team.legal_entity_id == legal_entity_id)
    if branch_id is not None:
        query = query.filter(Team.branch_id == branch_id)
    if parent_team_id is not None:
        query = query.filter(Team.parent_team_id == parent_team_id)
    lead_rows = select(TeamMembership.team_id).where(
        TeamMembership.company_id == cid, TeamMembership.member_role == "LEAD", _active_on(TeamMembership, today))
    if mine:
        lead_employee_id = principal.employee_id if principal.employee_id is not None else 0
    if lead_employee_id is not None:
        query = query.filter(Team.id.in_(select(TeamMembership.team_id).where(
            TeamMembership.company_id == cid, TeamMembership.employee_id == lead_employee_id,
            TeamMembership.member_role.in_(LEAD_ROLES), _active_on(TeamMembership, today))))
    if member_employee_id is not None:
        query = query.filter(Team.id.in_(select(TeamMembership.team_id).where(
            TeamMembership.company_id == cid, TeamMembership.employee_id == member_employee_id,
            _active_on(TeamMembership, today))))
    if has_lead is not None:
        query = query.filter(Team.id.in_(lead_rows) if has_lead else Team.id.not_in(lead_rows))
    total = query.count()
    order = {"code": Team.code, "-updated_at": Team.updated_at.desc()}.get(sort, func.lower(Team.name))
    rows = query.order_by(order, Team.id).offset(skip).limit(limit).all()
    if sort == "-member_count":
        rows = query.all()
    ids = [t.id for t in rows]
    leads = _leads_of(db, cid, ids, today)
    counts = _member_counts(db, cid, ids, today)
    items = [serialize_team(t, leads.get(t.id, []), counts.get(t.id, 0)) for t in rows]
    if sort == "-member_count":
        items = sorted(items, key=lambda i: (-(i["member_count"] or 0), i["name"].lower()))[skip:skip + limit]
    return {"items": items, "total": total, "skip": skip, "limit": limit}


def get_team(db: Session, principal: Principal, team_id: int) -> dict:
    team = _team_or_404(db, principal, team_id)
    today = actor_today(db, principal)
    _ensure_visible(db, principal, team, today)
    leads = _leads_of(db, principal.company_id, [team.id], today)
    result = serialize_team(team, leads.get(team.id, []), _member_counts(db, principal.company_id, [team.id], today)
                            .get(team.id, 0))
    reach = membership_reach(db, principal, team)
    result["permissions"] = {"manage": _home_covered(principal, MANAGE, team), "members": reach is not None,
                             "leads": reach == "LEADS", "member_list": _members_visible(db, principal, team, today)}
    history = db.query(TeamParentHistory).filter(TeamParentHistory.company_id == team.company_id,
                                                 TeamParentHistory.team_id == team.id) \
        .order_by(TeamParentHistory.effective_from).all()
    result["parent_history"] = [{"parent_team_id": h.parent_team_id, "effective_from": h.effective_from.isoformat(),
                                 "effective_to": h.effective_to.isoformat() if h.effective_to else None}
                                for h in history if h.effective_to is None or h.effective_to >= h.effective_from]
    return result


# --------------------------------------------------------------------------- memberships


def list_members(db: Session, principal: Principal, team_id: int, *, q: Optional[str] = None,
                 member_role: Optional[str] = None, state: Optional[str] = None, as_of: Optional[date] = None,
                 sort: str = "name", skip: int = 0, limit: int = 50) -> dict:
    team = _team_or_404(db, principal, team_id)
    today = actor_today(db, principal)
    _ensure_visible(db, principal, team, today)
    on = as_of or today
    query = db.query(TeamMembership, Employee).join(
        Employee, (Employee.id == TeamMembership.employee_id) & (Employee.company_id == TeamMembership.company_id)
    ).filter(TeamMembership.company_id == team.company_id, TeamMembership.team_id == team.id)
    if not _members_visible(db, principal, team, today):
        # team:read at SELF: the leads and one's own rows only (X 01 §10.3 runtime implication).
        query = query.filter(or_(TeamMembership.member_role.in_(LEAD_ROLES),
                                 TeamMembership.employee_id == (principal.employee_id or 0)))
    if member_role:
        query = query.filter(TeamMembership.member_role == member_role)
    if q:
        like = f"%{q.strip().lower()}%"
        query = query.filter(or_(func.lower(Employee.first_name).like(like), func.lower(Employee.last_name).like(like),
                                 func.lower(Employee.employee_code).like(like)))
    rows = query.all()
    states = set((state or "ACTIVE,FUTURE").upper().split(","))
    picked = []
    for row, employee in rows:
        row_state = membership_state(row, on)
        if "ALL" in states or row_state in states:
            picked.append((row, employee))
    if sort == "effective_from":
        picked.sort(key=lambda p: (p[0].effective_from, p[0].id))
    else:
        picked.sort(key=lambda p: ((_name(p[1]) or "").lower(), p[0].effective_from, p[0].id))
    items = [serialize_membership(r, on, e) for r, e in picked[skip:skip + limit]]
    return {"items": items, "total": len(picked), "skip": skip, "limit": limit}


def member_candidates(db: Session, principal: Principal, team_id: int, q: str,
                      effective_from: Optional[date] = None) -> List[dict]:
    """The masked member-candidate lookup (box P-10, X-ROLE-36): any tenant employee employed on ``effective_from``,
    by exact code or name prefix; only id, name, code and branch, at most 20; grants no other read."""
    team = _team_or_404(db, principal, team_id)
    if membership_reach(db, principal, team) is None:
        raise not_found("Team")
    text = (q or "").strip().lower()
    if len(text) < 2:
        raise bad_request("Type at least 2 characters.", "QUERY_TOO_SHORT")
    on = effective_from or actor_today(db, principal)
    prefix = f"{text}%"
    rows = db.query(Employee).filter(
        Employee.company_id == principal.company_id, Employee.hire_date <= on,
        or_(Employee.termination_date.is_(None), Employee.termination_date >= on),
        or_(func.lower(Employee.employee_code) == text, func.lower(Employee.first_name).like(prefix),
            func.lower(Employee.last_name).like(prefix),
            func.lower(func.concat(Employee.first_name, " ", Employee.last_name)).like(prefix)),
    ).order_by(Employee.first_name, Employee.last_name, Employee.id).limit(MAX_CANDIDATES).all()
    from app.services.org_structure_service import current_assignment

    result = []
    for employee in rows:
        placement = current_assignment(db, employee.id, on)
        branch = db.get(Branch, placement.branch_id) if placement is not None and placement.branch_id else None
        result.append({"employee_id": employee.id, "name": _name(employee), "employee_code": employee.employee_code,
                       "branch": branch.name if branch else None})
    return result


def _eligible(db: Session, company_id: int, employee_id: int, start: date, end: Optional[date]) -> Employee:
    employee = db.query(Employee).filter(Employee.company_id == company_id, Employee.id == employee_id).first()
    if employee is None:
        raise not_found("Employee")
    if employee.hire_date and employee.hire_date > start:
        raise AppError(422, "The person is not employed yet on the start date.", "NOT_ELIGIBLE", extra={"details": {"hire_date": employee.hire_date.isoformat()}})
    if employee.termination_date is not None and (end is None or employee.termination_date < end):
        if employee.termination_date < start:
            raise AppError(422, "The person's employment ends before this membership starts.", "NOT_ELIGIBLE", extra={"details": {"termination_date": employee.termination_date.isoformat()}})
    return employee


def _inside_team(team: Team, start: date, end: Optional[date]) -> None:
    if team.status != "ACTIVE":
        raise conflict("The team is archived.", "TEAM_ARCHIVED")
    if start < team.effective_from or (team.effective_to is not None and (end is None or end > team.effective_to)):
        raise AppError(422, "The membership must lie inside the team's dates.", "NOT_ELIGIBLE", extra={"details": {"team_from": team.effective_from.isoformat(),
                                "team_to": team.effective_to.isoformat() if team.effective_to else None}})


def _assert_can_change(db: Session, principal: Principal, team: Team, employee_id: int, *, lead_fields: bool) -> None:
    reach = membership_reach(db, principal, team)
    _deny(db, principal, team, reach is not None, "You cannot change the members of this team.")
    if lead_fields and reach != "LEADS":
        raise forbidden("Only HR or a team administrator can change leads.")
    if principal.employee_id is not None and employee_id == principal.employee_id and lead_fields:
        raise forbidden("You cannot change your own team role or routing lead flag.")  # SG-X3


def _warnings(db: Session, principal: Principal, team: Team, row: TeamMembership, on: date) -> List[dict]:
    """Form hints, never blocks: upward exclusion (X-ROLE-43) and an inert deputy (X-ROLE-35)."""
    cid = principal.company_id
    names = _employee_names(db, cid, [row.employee_id])
    warnings = []
    lead_ids = [l["employee_id"] for l in _leads_of(db, cid, [team.id], on).get(team.id, [])
                if l["employee_id"] != row.employee_id]
    for lead_id in lead_ids:
        ancestors = {a for (a,) in db.execute(authz.ancestors_select(cid, lead_id))}
        if row.employee_id in ancestors:
            lead = _employee_names(db, cid, [lead_id]).get(lead_id)
            warnings.append({"code": "ABOVE_LEAD", "message": f"{_name(names.get(row.employee_id))} is above "
                             f"{_name(lead)} in the reporting line: {_name(lead)} will not see or approve "
                             f"{_name(names.get(row.employee_id))}'s records through this team."})
    if row.member_role == "DEPUTY_LEAD":
        user_id = _user_of(db, cid, row.employee_id)
        principal_of = _principal_for_user(db, user_id) if user_id else None
        if principal_of is None or not any(principal_of.has(code) for code in set(MODULE_APPROVE_CODES.values())):
            warnings.append({"code": "DEPUTY_CANNOT_APPROVE", "message": f"{_name(names.get(row.employee_id))} "
                             "cannot approve yet — assign a role with the approve codes."})
    return warnings


def add_member(db: Session, principal: Principal, team_id: int, data) -> dict:
    team = _team_or_404(db, principal, team_id)
    today = actor_today(db, principal)
    lead_fields = data.member_role != "MEMBER" or data.is_routing_lead
    _assert_can_change(db, principal, team, data.employee_id, lead_fields=lead_fields)
    if principal.employee_id is not None and data.employee_id == principal.employee_id \
            and not (principal.is_company_admin or principal.has_company_scope(MEMBERS)):
        raise forbidden("You cannot add yourself to a team.")
    start = data.effective_from or today
    end = data.effective_to
    if end is not None and end < start:
        raise bad_request("The membership ends before it starts.", "INVALID_DATES")
    if data.is_routing_lead and data.member_role != "LEAD":
        raise conflict("Only a lead can be the routing lead.", "ROUTING_LEAD_CONFLICT")
    _inside_team(team, start, end)
    employee = _eligible(db, principal.company_id, data.employee_id, start, end)
    if data.is_primary_team and data.move_primary:
        _end_other_primary(db, principal, data.employee_id, team.id, start)
    row = TeamMembership(company_id=principal.company_id, team_id=team.id, employee_id=data.employee_id,
                         member_role=data.member_role, is_primary_team=data.is_primary_team,
                         is_routing_lead=data.is_routing_lead, effective_from=start, effective_to=end,
                         reason=data.reason, created_by_user_id=principal.user_id, row_version=1)
    if setting(db, principal.company_id, "team.primary_team.required") and not data.is_primary_team \
            and not _has_primary(db, principal.company_id, data.employee_id, start):
        row.is_primary_team = True  # TM-5: the first membership becomes primary
    db.add(row)
    _flush(db)
    _audit_membership(db, principal, team, row, None, "Added")
    _touch(db, principal.company_id, [team.id], [_user_of(db, principal.company_id, data.employee_id)])
    result = serialize_membership(row, today, employee)
    result["warnings"] = _warnings(db, principal, team, row, max(start, today))
    return result


def _has_primary(db: Session, company_id: int, employee_id: int, on: date) -> bool:
    return db.query(TeamMembership.id).filter(
        TeamMembership.company_id == company_id, TeamMembership.employee_id == employee_id,
        TeamMembership.is_primary_team.is_(True), _active_on(TeamMembership, on)).first() is not None


def _end_other_primary(db: Session, principal: Principal, employee_id: int, team_id: int, start: date) -> None:
    """§3.4: the employee's other primary rows stop being primary from ``start`` (split, history kept)."""
    rows = db.query(TeamMembership).filter(
        TeamMembership.company_id == principal.company_id, TeamMembership.employee_id == employee_id,
        TeamMembership.team_id != team_id, TeamMembership.is_primary_team.is_(True),
        *_overlaps(TeamMembership, start, None)).all()
    for row in rows:
        _split(db, principal, row, start, is_primary_team=False)


def _split(db: Session, principal: Principal, row: TeamMembership, start: date, **changes) -> TeamMembership:
    """End ``row`` the day before ``start`` and continue it with ``changes`` (TM-3); in place when not yet started."""
    if start <= row.effective_from:
        for key, value in changes.items():
            setattr(row, key, value)
        row.row_version += 1
        _flush(db)
        return row
    new = TeamMembership(company_id=row.company_id, team_id=row.team_id, employee_id=row.employee_id,
                         member_role=row.member_role, is_primary_team=row.is_primary_team,
                         is_routing_lead=row.is_routing_lead, effective_from=start, effective_to=row.effective_to,
                         reason=row.reason, created_by_user_id=principal.user_id, row_version=1)
    for key, value in changes.items():
        setattr(new, key, value)
    if new.member_role != "LEAD":
        new.is_routing_lead = False
    row.effective_to = start - ONE_DAY
    row.ended_by_user_id = principal.user_id
    row.row_version += 1
    _flush(db)
    db.add(new)
    _flush(db)
    return new


def _audit_membership(db: Session, principal: Principal, team: Team, row: TeamMembership, before: Optional[dict],
                      verb: str) -> None:
    after = {"employee_id": row.employee_id, "member_role": row.member_role, "is_primary_team": row.is_primary_team,
             "is_routing_lead": row.is_routing_lead, "effective_from": row.effective_from.isoformat(),
             "effective_to": row.effective_to.isoformat() if row.effective_to else None}
    AuditLogService.log(db, principal, MODULE, "MEMBERSHIP_CHANGED",
                        f"{verb} membership #{row.id} of team {team.code}", "team_membership", row.id,
                        details={"team_id": team.id, "before": before, "after": after})
    _emit(db, "team.membership.changed", {"team_id": team.id, "membership_id": row.id,
                                          "employee_id": row.employee_id})


def update_membership(db: Session, principal: Principal, membership_id: int, data) -> dict:
    row = db.query(TeamMembership).filter(TeamMembership.company_id == principal.company_id,
                                          TeamMembership.id == membership_id).first()
    if row is None:
        raise not_found("Team membership")
    team = _team_or_404(db, principal, row.team_id)
    today = actor_today(db, principal)
    lead_fields = (row.member_role != "MEMBER" or row.is_routing_lead
                   or (data.member_role not in (None, "MEMBER")) or bool(data.is_routing_lead))
    _assert_can_change(db, principal, team, row.employee_id, lead_fields=lead_fields)
    if principal.employee_id is not None and row.employee_id == principal.employee_id \
            and not (principal.is_company_admin or principal.has_company_scope(MEMBERS)):
        raise forbidden("You cannot change your own team membership.")  # SG-X3
    check_row_version(row, data.row_version)
    before = {"member_role": row.member_role, "is_primary_team": row.is_primary_team,
              "is_routing_lead": row.is_routing_lead, "effective_from": row.effective_from.isoformat(),
              "effective_to": row.effective_to.isoformat() if row.effective_to else None}
    state = membership_state(row, today)
    if state == "CANCELLED":
        raise conflict("The membership was cancelled.", "MEMBERSHIP_CANCELLED")
    result_row = row
    if data.cancel:
        if state != "FUTURE":
            raise conflict("Only a membership that has not started can be cancelled; end it instead.",
                           "MEMBERSHIP_STARTED")
        row.effective_to = row.effective_from - ONE_DAY
        row.is_routing_lead = False
        row.ended_by_user_id = principal.user_id
        row.row_version += 1
        _flush(db)
        verb = "Cancelled"
    elif data.effective_to is not None and data.member_role is None and data.is_primary_team is None \
            and data.is_routing_lead is None:
        if data.effective_to < row.effective_from:
            raise bad_request("A membership ends on or after its first day; cancel a future one instead.",
                              "INVALID_DATES")
        if team.effective_to is not None and data.effective_to > team.effective_to:
            raise AppError(422, "The membership must lie inside the team's dates.", "NOT_ELIGIBLE")
        row.effective_to = data.effective_to
        row.ended_by_user_id = principal.user_id
        row.row_version += 1
        _flush(db)
        verb = "Ended"
    else:
        changes = {}
        for key in ("member_role", "is_primary_team", "is_routing_lead"):
            value = getattr(data, key)
            if value is not None and value != getattr(row, key):
                changes[key] = value
        if not changes:
            raise bad_request("Nothing to change.", "NOTHING_TO_CHANGE")
        role_after = changes.get("member_role", row.member_role)
        if changes.get("is_routing_lead", row.is_routing_lead) and role_after != "LEAD":
            raise conflict("Only a lead can be the routing lead.", "ROUTING_LEAD_CONFLICT")
        start = data.change_from or today
        if start < row.effective_from or (row.effective_to is not None and start > row.effective_to):
            raise bad_request("The change must start inside the membership.", "INVALID_DATES")
        if changes.get("is_primary_team"):
            _end_other_primary(db, principal, row.employee_id, row.team_id, start)
        result_row = _split(db, principal, row, start, **changes)
        verb = "Changed"
    _audit_membership(db, principal, team, result_row, before, verb)
    _touch(db, principal.company_id, [team.id], [_user_of(db, principal.company_id, row.employee_id)])
    employee = _employee_names(db, principal.company_id, [row.employee_id]).get(row.employee_id)
    result = serialize_membership(result_row, today, employee)
    result["warnings"] = _warnings(db, principal, team, result_row, max(result_row.effective_from, today))
    return result


# --------------------------------------------------------------------------- reads for other modules


def active_memberships(db: Session, company_id: int, employee_id: int, on: date) -> List[TeamMembership]:
    """The employee's memberships on ``on`` (employment-checked; TM-2, TM-8)."""
    employee = db.query(Employee).filter(Employee.company_id == company_id, Employee.id == employee_id).first()
    if employee is None or (employee.termination_date is not None and employee.termination_date < on):
        return []
    return db.query(TeamMembership).filter(
        TeamMembership.company_id == company_id, TeamMembership.employee_id == employee_id,
        _active_on(TeamMembership, on)).order_by(TeamMembership.id).all()


def primary_team_on(db: Session, company_id: int, employee_id: int, d: date) -> Optional[int]:
    rows = active_memberships(db, company_id, employee_id, d)
    primary = [r.team_id for r in rows if r.is_primary_team]
    return primary[0] if primary else None


def team_attributes(db: Session, company_id: int, employee_ids: Sequence[int], on: date) -> Dict[int, Dict[int, str]]:
    """{employee_id: {team_id: member_role}} on ``on`` (AssignmentCriteria ``team_ids`` / ``team_member_roles``)."""
    if not employee_ids:
        return {}
    rows = db.query(TeamMembership.employee_id, TeamMembership.team_id, TeamMembership.member_role).filter(
        TeamMembership.company_id == company_id, TeamMembership.employee_id.in_(sorted(set(employee_ids))),
        _active_on(TeamMembership, on)).all()
    result: Dict[int, Dict[int, str]] = {}
    for employee_id, team_id, role in rows:
        result.setdefault(employee_id, {})[team_id] = role
    return result


# --------------------------------------------------------------------------- team-lead resolver (§7.2)


@dataclass
class TeamLeadResolution:
    team_id: Optional[int]
    approver_employee_ids: List[int] = field(default_factory=list)
    # TEAM_LEAD | DEPUTY | PARENT_LEAD | NEXT_STEP | PRIMARY_MANAGER | NO_TEAM
    via: str = "NO_TEAM"
    routing: bool = False
    skipped: List[dict] = field(default_factory=list)


def _principal_for_user(db: Session, user_id: Optional[int]) -> Optional[Principal]:
    if user_id is None:
        return None
    from app.core.principal import build_principal

    user = db.get(User, user_id)
    if user is None or user.status != "active":
        return None
    return build_principal(db, user, None, db.get(Company, user.company_id))


def lead_has_authority(db: Session, lead: Principal, code: str, subject_id: int, work_date: date,
                       action_date: date) -> bool:
    """§7.2 step 4 with the as-of rule of §4.3 row 2: the subject is covered on W, the lead holds the grant (and the
    lead row or AT target) on the action date."""
    scopes = lead.scopes(code)
    if not scopes:
        return False
    if Scope.COMPANY.value in scopes:
        return True
    cid = lead.company_id
    subject_teams = {r.team_id for r in active_memberships(db, cid, subject_id, work_date)}
    if subject_teams and lead.employee_id is not None and (Scope.TEAM.value in scopes or Scope.TEAM_TREE.value in scopes):
        led = lead_team_ids(db, cid, lead.employee_id, action_date, tree=Scope.TEAM_TREE.value in scopes)
        ancestors = {a for (a,) in db.execute(authz.ancestors_select(cid, lead.employee_id))}
        if subject_teams & led and subject_id not in ancestors:
            return True
    if subject_teams & set(lead.scope_targets(code, Scope.ASSIGNED_TEAMS.value)):
        return True
    others = scopes - {Scope.TEAM.value, Scope.TEAM_TREE.value, Scope.ASSIGNED_TEAMS.value}
    if others:
        reduced = replace(lead, grants={**lead.grants, code: frozenset(others)})
        return authz.can_access_employee(db, reduced, code, subject_id)
    return False


def _on_full_day_leave(db: Session, company_id: int, employee_id: int, day: date) -> bool:
    from app.models.leave import Leave
    from app.models.time_inputs import LeaveDay

    return db.query(LeaveDay.id).join(Leave, Leave.id == LeaveDay.leave_id).filter(
        LeaveDay.company_id == company_id, LeaveDay.employee_id == employee_id, LeaveDay.date == day,
        LeaveDay.voided_at.is_(None), LeaveDay.portion == "FULL", Leave.status == "approved").first() is not None


def _eligible_lead(db: Session, company_id: int, lead_employee_id: int, subject_id: int, work_date: date,
                   action_date: date, code: Optional[str], skipped: List[dict]) -> bool:
    """§7.2 step 2 eligibility (X-ROLE-32, -33, -35, -43): active user, employed on the action date, not on approved
    full-day leave (``approvals.team_lead.skip_on_leave``), not above the subject, holding the module's approve code."""
    employee = db.query(Employee).filter(Employee.company_id == company_id, Employee.id == lead_employee_id).first()
    reason = None
    if employee is None or employee.user_id is None:
        reason = "NO_USER"
    elif employee.termination_date is not None and employee.termination_date < action_date:
        reason = "NOT_EMPLOYED"
    elif subject_id in {a for (a,) in db.execute(authz.ancestors_select(company_id, lead_employee_id))}:
        reason = "SUBJECT_ABOVE_LEAD"
    elif setting(db, company_id, "approvals.team_lead.skip_on_leave") and \
            _on_full_day_leave(db, company_id, lead_employee_id, action_date):
        reason = "ON_LEAVE"
    else:
        lead = _principal_for_user(db, employee.user_id)
        if lead is None:
            reason = "USER_INACTIVE"
        elif code is not None and not lead_has_authority(db, lead, code, subject_id, work_date, action_date):
            reason = "NO_AUTHORITY"
    if reason:
        skipped.append({"employee_id": lead_employee_id, "reason": reason})
        return False
    return True


def _team_rows(db: Session, company_id: int, team_id: int, on: date, role: str) -> List[TeamMembership]:
    rows = db.query(TeamMembership).filter(
        TeamMembership.company_id == company_id, TeamMembership.team_id == team_id,
        TeamMembership.member_role == role, _active_on(TeamMembership, on)).all()
    return sorted(rows, key=lambda r: (not r.is_routing_lead, r.id))


def _pick(db: Session, company_id: int, rows: Sequence[TeamMembership], subject_id: int, work_date: date,
          action_date: date, code: Optional[str], skipped: List[dict]) -> Tuple[List[int], bool]:
    """Routing lead if eligible, else ANY other eligible lead (first decision wins)."""
    eligible = [r for r in rows if r.employee_id != subject_id and _eligible_lead(
        db, company_id, r.employee_id, subject_id, work_date, action_date, code, skipped)]
    if not eligible:
        return [], False
    routing = [r for r in eligible if r.is_routing_lead]
    if routing:
        return [routing[0].employee_id], True
    return sorted({r.employee_id for r in eligible}), False


def resolve_team_lead(db: Session, company_id: int, employee_id: int, work_date: date, *, action_date: date,
                      module: Optional[str] = None, owning_team_id: Optional[int] = None) -> TeamLeadResolution:
    """The ``team_lead`` approver for a request about ``employee_id`` on ``work_date`` (X 01 §7.2 steps 1-4)."""
    code = MODULE_APPROVE_CODES.get(module) if module else None
    skipped: List[dict] = []
    team_id = owning_team_id
    if team_id is None:
        rows = active_memberships(db, company_id, employee_id, work_date)
        primary = [r.team_id for r in rows if r.is_primary_team]
        team_ids = sorted({r.team_id for r in rows})
        team_id = primary[0] if primary else (team_ids[0] if len(team_ids) == 1 else None)
    if team_id is None:
        return TeamLeadResolution(None, [], "NO_TEAM")
    leads, routing = _pick(db, company_id, _team_rows(db, company_id, team_id, action_date, "LEAD"), employee_id,
                           work_date, action_date, code, skipped)
    if leads:
        return TeamLeadResolution(team_id, leads, "TEAM_LEAD", routing, skipped)
    fallback = setting(db, company_id, "approvals.team_lead.fallback")
    if fallback == "DEPUTY_PARENT_NEXT":
        deputies, _ = _pick(db, company_id, _team_rows(db, company_id, team_id, action_date, "DEPUTY_LEAD"),
                            employee_id, work_date, action_date, code, skipped)
        if deputies:
            return TeamLeadResolution(team_id, deputies, "DEPUTY", False, skipped)
        parent_id = db.query(TeamParentHistory.parent_team_id).filter(
            TeamParentHistory.company_id == company_id, TeamParentHistory.team_id == team_id,
            _active_on(TeamParentHistory, action_date)).scalar()
        if parent_id is not None:
            parents, routing = _pick(db, company_id, _team_rows(db, company_id, parent_id, action_date, "LEAD"),
                                     employee_id, work_date, action_date, code, skipped)
            if parents:
                return TeamLeadResolution(team_id, parents, "PARENT_LEAD", routing, skipped)
        return TeamLeadResolution(team_id, [], "NEXT_STEP", False, skipped)
    if fallback == "NEXT_STEP":
        return TeamLeadResolution(team_id, [], "NEXT_STEP", False, skipped)
    manager = db.query(Employee.manager_id).filter(Employee.company_id == company_id, Employee.id == employee_id).scalar()
    return TeamLeadResolution(team_id, [manager] if manager else [], "PRIMARY_MANAGER", False, skipped)


def routing_leads_for(db: Session, company_id: int, employee_id: int, work_date: date, *,
                      owning_team_id: Optional[int] = None, action_date: date, module: Optional[str] = None) -> List[int]:
    """WI-X-1.2 signature: the approver employee ids of the resolution ([] = go to the chain's next step)."""
    return resolve_team_lead(db, company_id, employee_id, work_date, action_date=action_date, module=module,
                             owning_team_id=owning_team_id).approver_employee_ids


# --------------------------------------------------------------------------- Me


def team_facts(db: Session, principal: Principal, *, counts: bool = False) -> dict:
    """``me.teams`` (X 01 §11): own teams and led teams on the user's local today; names only (counts on /me/teams)."""
    facts: Dict[str, Any] = {"member_of": [], "leads": [], "acting_for": []}
    if principal.employee_id is None:
        return facts
    cid = principal.company_id
    today = actor_today(db, principal)
    rows = active_memberships(db, cid, principal.employee_id, today)
    teams = {t.id: t for t in db.query(Team).filter(Team.company_id == cid, Team.id.in_(
        sorted({r.team_id for r in rows}) or [0]))}
    for row in sorted(rows, key=lambda r: (not r.is_primary_team, teams[r.team_id].name.lower())):
        facts["member_of"].append({"id": row.team_id, "name": teams[row.team_id].name, "code": teams[row.team_id].code,
                                   "primary": row.is_primary_team})
    led = [r for r in rows if r.member_role in LEAD_ROLES]
    counts_by = _member_counts(db, cid, [r.team_id for r in led], today) if counts else {}
    for row in sorted(led, key=lambda r: teams[r.team_id].name.lower()):
        entry = {"id": row.team_id, "name": teams[row.team_id].name, "code": teams[row.team_id].code,
                 "routing": row.is_routing_lead, "member_role": row.member_role}
        if counts:
            entry["member_count"] = counts_by.get(row.team_id, 0)
        facts["leads"].append(entry)
    facts["acting_for"] = _acting_for(db, principal, today)
    return facts


def _acting_for(db: Session, principal: Principal, today: date) -> List[dict]:
    """Active approval delegations to the user, one entry per delegator (earliest end; DG-5, LH 06 §4.7)."""
    from app.models.employee import ApprovalDelegation

    if principal.employee_id is None:
        return []
    rows = db.query(ApprovalDelegation).filter(
        ApprovalDelegation.company_id == principal.company_id,
        ApprovalDelegation.delegate_employee_id == principal.employee_id, ApprovalDelegation.revoked_at.is_(None),
        ApprovalDelegation.starts_on <= today, ApprovalDelegation.ends_on >= today).all()
    by_delegator: Dict[int, date] = {}
    for row in rows:
        by_delegator[row.delegator_employee_id] = min(by_delegator.get(row.delegator_employee_id, row.ends_on),
                                                      row.ends_on)
    names = _employee_names(db, principal.company_id, by_delegator)
    return [{"employee_id": e, "name": _name(names.get(e)), "until": u.isoformat()}
            for e, u in sorted(by_delegator.items())]


# --------------------------------------------------------------------------- jobs (daily-local, spine §9.2)


def _local_date(ctx) -> date:
    raw = (ctx.payload or {}).get("local_date")
    if raw:
        return date.fromisoformat(raw)
    from app.core import clock

    return clock.utcnow().date()


@jobs.register("team.membership_activate")
def membership_activate(ctx) -> dict:
    """Rows that start or end on the local date move reach: bump leads and AT holders, sync parents, reroute."""
    db: Session = ctx.db
    company_id = ctx.company_id
    day = _local_date(ctx)
    teams = {t for (t,) in db.query(TeamMembership.team_id).filter(
        TeamMembership.company_id == company_id,
        or_(TeamMembership.effective_from == day, TeamMembership.effective_to == day - ONE_DAY)).distinct()}
    members = {u for (u,) in db.query(Employee.user_id).join(
        TeamMembership, (TeamMembership.employee_id == Employee.id) & (TeamMembership.company_id == Employee.company_id)
    ).filter(TeamMembership.company_id == company_id, Employee.user_id.is_not(None),
             or_(TeamMembership.effective_from == day, TeamMembership.effective_to == day - ONE_DAY))}
    parents = sync_parents(db, company_id, day)
    reparented = {t for (t,) in db.query(TeamParentHistory.team_id).filter(
        TeamParentHistory.company_id == company_id, TeamParentHistory.effective_from == day)}
    if teams or reparented or members:
        _touch(db, company_id, teams | reparented, members)
    return {"teams": len(teams), "parents": parents, "local_date": day.isoformat()}


@jobs.register("access.assignment_expire")
def assignment_expire(ctx) -> dict:
    """Role windows that start or end within the last day bump their users' permission_version and reroute (§6.7)."""
    from datetime import datetime, time, timezone as tz

    import pytz

    from app.services import rbac_service

    db: Session = ctx.db
    company_id = ctx.company_id
    day = _local_date(ctx)
    zone = pytz.timezone((ctx.payload or {}).get("timezone") or "UTC")
    start = zone.localize(datetime.combine(day, time(0, 0))).astimezone(tz.utc) - timedelta(hours=1)
    end = start + timedelta(hours=26)
    users = {u for (u,) in db.query(UserRoleAssignment.user_id).filter(
        UserRoleAssignment.company_id == company_id,
        or_(UserRoleAssignment.effective_from.between(start, end),
            UserRoleAssignment.effective_until.between(start, end)))}
    if users:
        db.query(User).filter(User.company_id == company_id, User.id.in_(sorted(users))).update(
            {User.permission_version: User.permission_version + 1}, synchronize_session=False)
        rbac_service.reroute_approvals(db, company_id)
    return {"users": len(users), "local_date": day.isoformat()}

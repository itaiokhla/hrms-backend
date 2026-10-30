"""Leave dashboards, team calendar and reports (LH 10; 09-ui-ux/02 §7-§12).

Privacy (D-LH-17, 07 §3): colleagues see approved leave as "Away" with dates and portion only, inside
``leave.team_calendar.visibility`` (TEAM = same manager plus that manager; DEPARTMENT; BRANCH; COMPANY); the type name
only with ``leave.team_calendar.show_type`` and never for SENSITIVE leave. ``leave:read`` in scope adds pending
requests and types (sensitive types still masked unless ``leave:view_medical``). Reasons never appear in a report.
Aggregate reports suppress groups smaller than ``privacy.small_group_suppression_size``.
"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.core import authz, clock
from app.core.errors import bad_request, forbidden
from app.core.principal import Principal
from app.models.employee import Department, Employee
from app.models.leave import Leave, LeaveType
from app.models.leave_v2 import CompOffClaim, LeaveChangeRequest, LeaveEncashmentRequest
from app.models.org import EmployeeAssignment
from app.models.time_inputs import LeaveDay
from app.services import leave_access, leave_ledger, leave_plans

ZERO = Decimal(0)
MAX_DAYS = 93


def _types(db: Session, company_id: int) -> Dict[str, LeaveType]:
    return {t.code: t for t in db.query(LeaveType).filter(LeaveType.company_id == company_id)}


def _colleagues(db: Session, principal: Principal, viewer: Optional[Employee], on: date) -> set:
    """Employees visible as "Away" to any active employee under ``leave.team_calendar.visibility``."""
    if viewer is None:
        return set()
    mode = leave_access.setting(db, principal.company_id, "leave.team_calendar.visibility", default="TEAM")
    base = db.query(Employee.id).filter(Employee.company_id == principal.company_id, Employee.is_active.is_(True))
    if mode == "OFF":
        return set()
    if mode == "COMPANY":
        return {i for (i,) in base}
    if mode == "DEPARTMENT":
        return {i for (i,) in base.filter(Employee.department_id == viewer.department_id)} if viewer.department_id else set()
    if mode == "BRANCH":
        where = leave_plans.placement(db, principal.company_id, viewer.id, on)
        if where is None:
            return set()
        return {i for (i,) in db.query(EmployeeAssignment.employee_id).filter(
            EmployeeAssignment.company_id == principal.company_id, EmployeeAssignment.branch_id == where.branch_id,
            EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.effective_from <= on,
            or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= on))}
    team = {i for (i,) in base.filter(Employee.manager_id == viewer.manager_id)} if viewer.manager_id else set()
    team |= {i for (i,) in base.filter(Employee.manager_id == viewer.id)}
    if viewer.manager_id:
        team.add(viewer.manager_id)
    return team | {viewer.id}


def team_calendar(db: Session, principal: Principal, start: date, end: date, *, department_id: Optional[int] = None
                  ) -> dict:
    if end < start or (end - start).days > MAX_DAYS:
        raise bad_request(f"Choose at most {MAX_DAYS} days.", "RANGE_TOO_LONG")
    cid = principal.company_id
    viewer = db.get(Employee, principal.employee_id) if principal.employee_id else None
    scope = authz.employee_scope(db, principal, "leave:read")
    in_scope = {i for (i,) in db.query(Employee.id).filter(Employee.company_id == cid, Employee.is_active.is_(True),
                                                           scope.clause(Employee.id))}
    colleagues = _colleagues(db, principal, viewer, start)
    people_ids = (in_scope | colleagues)
    if department_id is not None:
        people_ids = {i for (i,) in db.query(Employee.id).filter(Employee.id.in_(people_ids or {0}),
                                                                 Employee.department_id == department_id)}
    people = (db.query(Employee).filter(Employee.company_id == cid, Employee.id.in_(people_ids or {0}))
              .order_by(Employee.first_name, Employee.last_name).limit(300).all())
    types = _types(db, cid)
    show_type = bool(leave_access.setting(db, cid, "leave.team_calendar.show_type", default=False))
    medical = principal.has("leave:view_medical")
    leaves = (db.query(Leave).filter(Leave.company_id == cid, Leave.employee_id.in_([p.id for p in people] or [0]),
                                     Leave.status.in_(("approved", "pending")), Leave.start_date <= end,
                                     Leave.end_date >= start).all())
    rows: Dict[int, list] = defaultdict(list)
    for leave in leaves:
        full = leave.employee_id in in_scope or leave.employee_id == principal.employee_id
        if leave.status == "pending" and not full:
            continue
        lt = types.get(leave.leave_type)
        sensitive = (leave.sensitivity == "SENSITIVE") or (lt is not None and lt.sensitivity == "SENSITIVE")
        if leave.employee_id == principal.employee_id:
            label, token = (lt.name if lt else leave.leave_type), (lt.color_token if lt else "leave-1")
        elif sensitive and not medical:
            label, token = ("Protected leave" if full else "Away"), "leave-neutral"
        elif full or show_type:
            label, token = (lt.name if lt else leave.leave_type), (lt.color_token if lt else "leave-1")
        else:
            label, token = "Away", "leave-neutral"
        rows[leave.employee_id].append({
            "id": leave.id if full else None, "start_date": leave.start_date.isoformat(),
            "end_date": leave.end_date.isoformat(), "start_portion": leave.start_portion,
            "end_portion": leave.end_portion, "status": leave.status.upper(), "label": label, "color_token": token,
            "leave_type": leave.leave_type if (full or show_type) and not (sensitive and not medical) else None})
    away_by_day = defaultdict(int)
    for items in rows.values():
        for item in items:
            if item["status"] != "APPROVED":
                continue
            day = max(start, date.fromisoformat(item["start_date"]))
            last = min(end, date.fromisoformat(item["end_date"]))
            while day <= last:
                away_by_day[day.isoformat()] += 1
                day += timedelta(days=1)
    return {"start": start.isoformat(), "end": end.isoformat(),
            "people": [{"employee_id": p.id, "name": p.full_name, "is_me": p.id == principal.employee_id,
                        "department_id": p.department_id, "leaves": rows.get(p.id, [])} for p in people],
            "away_by_day": dict(away_by_day), "visibility": leave_access.setting(
                db, cid, "leave.team_calendar.visibility", default="TEAM")}


def hr_balances(db: Session, principal: Principal, *, on: Optional[date] = None, leave_type: Optional[str] = None,
                department_id: Optional[int] = None, search: Optional[str] = None, limit: int = 200) -> dict:
    if not principal.has("leave:read") or principal.scopes("leave:read") == frozenset({"SELF"}):
        raise forbidden()
    cid = principal.company_id
    on = on or clock.company_today(db, cid)
    scope = authz.employee_scope(db, principal, "leave:read")
    query = db.query(Employee).filter(Employee.company_id == cid, Employee.is_active.is_(True), scope.clause(Employee.id))
    if department_id:
        query = query.filter(Employee.department_id == department_id)
    if search:
        like = f"%{search.strip()}%"
        query = query.filter(or_(Employee.first_name.ilike(like), Employee.last_name.ilike(like),
                                 Employee.employee_code.ilike(like)))
    people = query.order_by(Employee.first_name, Employee.last_name).limit(limit).all()
    out = []
    for person in people:
        items = leave_ledger.balances_for(db, cid, person, on, for_employee=False)
        if leave_type:
            items = [i for i in items if i["leave_type_code"] == leave_type]
        out.append({"employee_id": person.id, "name": person.full_name, "employee_code": person.employee_code,
                    "balances": items})
    return {"date": on.isoformat(), "v2_enabled": leave_access.v2_enabled(db, cid), "employees": out}


def usage(db: Session, principal: Principal, start: date, end: date) -> dict:
    if not (principal.has("report:read") or principal.has("leave:read", "COMPANY")):
        raise forbidden()
    cid = principal.company_id
    scope = authz.employee_scope(db, principal, "leave:read")
    types = _types(db, cid)
    rows = (db.query(LeaveDay.date, Leave.leave_type, LeaveDay.units, LeaveDay.employee_id)
            .join(Leave, and_(Leave.company_id == LeaveDay.company_id, Leave.id == LeaveDay.leave_id))
            .filter(LeaveDay.company_id == cid, LeaveDay.voided_at.is_(None), LeaveDay.counts_against_balance.is_(True),
                    LeaveDay.date >= start, LeaveDay.date <= end, Leave.status == "approved",
                    scope.clause(LeaveDay.employee_id)).all())
    by_type: Dict[str, dict] = {}
    by_month: Dict[str, Decimal] = defaultdict(lambda: ZERO)
    for day, code, units, emp in rows:
        item = by_type.setdefault(code, {"leave_type": code, "name": types[code].name if code in types else code,
                                         "units": ZERO, "employees": set()})
        item["units"] += Decimal(units)
        item["employees"].add(emp)
        by_month[day.strftime("%Y-%m")] += Decimal(units)
    suppress = int(leave_access.setting(db, cid, "privacy.small_group_suppression_size", default=5) or 5)
    result = []
    for item in sorted(by_type.values(), key=lambda x: -x["units"]):
        count = len(item["employees"])
        lt = types.get(item["leave_type"])
        sensitive = lt is not None and lt.sensitivity == "SENSITIVE"
        result.append({"leave_type": item["leave_type"], "name": item["name"], "units": leave_ledger._num(item["units"]),
                       "employees": count if not (sensitive and count < suppress) else None,
                       "suppressed": sensitive and count < suppress})
    pending = db.query(func.count(Leave.id)).filter(Leave.company_id == cid, Leave.status == "pending",
                                                    scope.clause(Leave.employee_id)).scalar()
    return {"start": start.isoformat(), "end": end.isoformat(), "by_type": result,
            "by_month": {k: leave_ledger._num(v) for k, v in sorted(by_month.items())}, "pending_requests": pending}


def liability(db: Session, principal: Principal, *, on: Optional[date] = None, legal_entity_id: Optional[int] = None
              ) -> dict:
    """Encashable units per employee and type (amounts are payroll's encashment formula)."""
    if not principal.has("leave:liability_read"):
        raise forbidden()
    leave_access.require_v2(db, principal.company_id)
    cid = principal.company_id
    on = on or clock.company_today(db, cid)
    scope = authz.employee_scope(db, principal, "leave:liability_read")
    people = db.query(Employee).filter(Employee.company_id == cid, Employee.is_active.is_(True),
                                       scope.clause(Employee.id)).order_by(Employee.id).limit(1000).all()
    types = _types(db, cid)
    rows, totals = [], defaultdict(lambda: ZERO)
    for person in people:
        if legal_entity_id is not None:
            where = leave_plans.placement(db, cid, person.id, on)
            if where is None or where.legal_entity_id != legal_entity_id:
                continue
        resolution = leave_plans.resolve(db, cid, person.id, on)
        for rule in resolution.fields.get("types", []):
            lt = types.get(rule["leave_type_code"])
            if lt is None or lt.is_unlimited or not (rule.get("encashment") or {}).get("eligible"):
                continue
            view = leave_ledger.balance(db, cid, person, lt, on, rule=rule, resolution=resolution)
            if view.balance <= 0:
                continue
            rows.append({"employee_id": person.id, "name": person.full_name, "leave_type": lt.code,
                         "encashable_units": leave_ledger._num(view.balance)})
            totals[lt.code] += view.balance
    return {"date": on.isoformat(), "rows": rows, "totals": {k: leave_ledger._num(v) for k, v in totals.items()}}


def dashboard(db: Session, principal: Principal) -> dict:
    """Home cards: my balances, my next leave, approvals waiting for me, who is away today."""
    from app.services import leave_changes, leave_service

    cid = principal.company_id
    viewer = db.get(Employee, principal.employee_id) if principal.employee_id else None
    today = clock.company_today(db, cid)
    if viewer is not None:
        from app.services import leave_requests

        today = leave_requests.local_today(db, viewer)
    mine = []
    upcoming = None
    if viewer is not None:
        mine = leave_ledger.balances_for(db, cid, viewer, today)
        nxt = (db.query(Leave).filter(Leave.company_id == cid, Leave.employee_id == viewer.id,
                                      Leave.status.in_(("approved", "pending")), Leave.end_date >= today)
               .order_by(Leave.start_date).first())
        if nxt is not None:
            upcoming = {"id": nxt.id, "start_date": nxt.start_date.isoformat(), "end_date": nxt.end_date.isoformat(),
                        "status": nxt.status.upper(), "leave_type": nxt.leave_type,
                        "leave_type_name": (_types(db, cid).get(nxt.leave_type).name
                                            if _types(db, cid).get(nxt.leave_type) else nxt.leave_type),
                        "color_token": (_types(db, cid).get(nxt.leave_type).color_token
                                        if _types(db, cid).get(nxt.leave_type) else None),
                        "units": leave_ledger._num(Decimal(nxt.number_of_days))}
    approvals = len(leave_service.approval_inbox(db, principal)) + len(leave_changes.pending_for_approver(db, principal))
    from app.services import leave_comp_off

    claims = sum(1 for c in leave_comp_off.list_claims(db, principal, status="PENDING") if c["can_decide"]) \
        if principal.has("leave:approve") and leave_access.v2_enabled(db, cid) else 0
    away = team_calendar(db, principal, today, today)
    away_today = [{"name": p["name"], "label": p["leaves"][0]["label"]} for p in away["people"]
                  if any(item["status"] == "APPROVED" for item in p["leaves"]) and not p["is_me"]]
    return {"date": today.isoformat(), "balances": mine, "upcoming": upcoming, "approvals_waiting": approvals,
            "comp_off_claims_waiting": claims, "away_today": away_today[:20],
            "v2_enabled": leave_access.v2_enabled(db, cid)}

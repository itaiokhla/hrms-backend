"""Recount approved leave after a calendar, work-week, plan or placement change (SPINE §6.12) — V2_ACTIVE tenants.

The request version goes up by one, its ``leave_days`` are regenerated whatever the payroll period state, and the
ledger difference is posted. Decreases are credited back; an increase may take the balance below the negative
allowance, which flags ``BALANCE_CONFLICT`` (an HR exception) — leave is never cancelled automatically. Every change is
logged for payroll (``source_change_log``), so locked periods go to retro.
"""

from datetime import date
from decimal import Decimal
from typing import Iterable, List, Mapping, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.employee import Employee
from app.models.leave import Leave, LeaveType
from app.models.leave_v2 import LeaveMessage
from app.models.time_inputs import LeaveDay
from app.services import leave_access, leave_ledger, leave_plans, leave_postings

INLINE_LIMIT = 200


def recount(db: Session, leave: Leave, *, actor_user_id: Optional[int] = None, note: str = "Recounted") -> bool:
    """Regenerate one approved leave; True when its charge changed."""
    before = Decimal(leave.number_of_days or 0)
    before_rows = {(r.date, r.portion, bool(r.counts_against_balance)) for r in leave_postings._active_rows(db, leave)}
    leave.version = (leave.version or 1) + 1
    rows = leave_postings.regenerate(db, leave, actor_user_id, note=note)
    after_rows = {(r.date, r.portion, bool(r.counts_against_balance)) for r in rows}
    changed = before_rows != after_rows
    if not changed:
        return False
    db.add(LeaveMessage(company_id=leave.company_id, leave_id=leave.id, kind="SYSTEM_EVENT",
                        body=f"{note}: charged days changed from {before.normalize()} to "
                             f"{Decimal(leave.number_of_days).normalize()}."))
    employee = db.get(Employee, leave.employee_id)
    leave_type = db.query(LeaveType).filter(LeaveType.company_id == leave.company_id,
                                            LeaveType.code == leave.leave_type).first()
    rule = leave_plans.rules_for(db, leave.company_id, leave.employee_id, leave.leave_type, leave.start_date) or {}
    if leave_type is not None and not leave_type.is_unlimited:
        view = leave_ledger.balance(db, leave.company_id, employee, leave_type, leave.start_date, rule=rule)
        allowance = Decimal(str(rule.get("negative_allowance") or "0"))
        if view.balance < -allowance:
            flags = list(leave.flags or [])
            if "BALANCE_CONFLICT" not in flags:
                leave.flags = flags + ["BALANCE_CONFLICT"]
    db.flush()
    return True


WORK, OFF = "WORK", "OFF"


def _overridden(facts, kind: str):
    """One date's day facts under a hypothetical roster entry: OFF = a full rostered rest day; WORK = no weekly
    off (a full non-working holiday stays non-working, as for a published WORK entry, 05 §3.4)."""
    from dataclasses import replace

    from app.payroll.services import time_inputs

    if kind == OFF:
        return replace(facts, weekly_off=True, weekly_off_portion="FULL", rest_day=True, working_portion="NONE",
                       scheduled_minutes=0, work_source="ROSTER")
    portion = time_inputs.working_portion(facts.employed, None, facts.holiday)
    return replace(facts, weekly_off=False, weekly_off_portion=None, rest_day=False, working_portion=portion,
                   work_source="ROSTER")


def preview_units(db: Session, company_id: int, leave_id: int, overrides: Mapping[date, str]) -> Optional[dict]:
    """Old -> new charged units of one PENDING or APPROVED request if the given dates became WORK / OFF (the roster
    publish preview, EC AS-INT-08 / AS-ROS-09). Charged exactly as at approval: ``leave_charging.charge`` with the
    type rule of the request's plan (counting basis, sandwich, half-day edges) — a rule-less (LEGACY_V1) type counts
    working days, or every date when the type counts non-working days. Never writes. ``overrides``:
    ``{date: "WORK" | "OFF"}``; dates outside the request are ignored (they cannot change its charge, except
    through a sandwich edge, which reads other requests as stored). None when the request does not exist or is
    neither pending nor approved."""
    from app.payroll.services import time_inputs
    from app.services import leave_charging

    leave = db.query(Leave).filter(Leave.company_id == company_id, Leave.id == int(leave_id)).first()
    if leave is None or (leave.status or "").lower() not in ("pending", "approved"):
        return None
    unknown = {v for v in overrides.values() if v not in (WORK, OFF)}
    if unknown:
        raise ValueError(f"overrides must be WORK or OFF, got {sorted(unknown)}")
    rule = None
    if leave_access.v2_enabled(db, company_id):
        rule = leave_plans.rules_for(db, company_id, leave.employee_id, leave.leave_type, leave.start_date)
    if rule is None:
        leave_type = db.query(LeaveType).filter(LeaveType.company_id == company_id,
                                                LeaveType.code == leave.leave_type).first()
        if leave_type is not None and leave_type.counts_non_working_days:
            rule = {"counting_basis": {"basis": "CALENDAR_DAYS"}}
    facts = time_inputs.day_facts(db, company_id, leave.employee_id, leave.start_date, leave.end_date)
    changed_facts = [_overridden(f, overrides[f.date]) if f.date in overrides else f for f in facts]
    args = (db, company_id, leave.employee_id, rule, leave.start_date, leave.end_date, leave.start_portion or "FULL",
            leave.end_portion or "FULL")
    old_total, old_days = leave_charging.charge(*args, facts_list=facts, exclude_leave_id=leave.id)
    new_total, new_days = leave_charging.charge(*args, facts_list=changed_facts, exclude_leave_id=leave.id)
    days: List[dict] = [{"date": a.date.isoformat(), "old": a.charged, "new": b.charged,
                         "charge_reason": b.charge_reason} for a, b in zip(old_days, new_days) if a.charged != b.charged]
    return {"leave_id": leave.id, "employee_id": leave.employee_id, "status": leave.status.lower(),
            "booked_units": Decimal(leave.number_of_days or 0), "old_units": old_total, "new_units": new_total,
            "changed": old_total != new_total, "days": days}


def recount_leaves(db: Session, leaves: Iterable[Leave], *, actor_user_id: Optional[int] = None,
                   note: str = "Recounted") -> int:
    return sum(1 for leave in leaves if recount(db, leave, actor_user_id=actor_user_id, note=note))


def recount_from(db: Session, company_id: int, start: date, *, actor_user_id: Optional[int] = None) -> int:
    """Plan activation with ``recalc_future_leave``: approved requests with dates on or after ``start``."""
    if not leave_access.v2_enabled(db, company_id):
        return 0
    leaves = (db.query(Leave).filter(Leave.company_id == company_id, Leave.status == "approved",
                                     Leave.end_date >= start).order_by(Leave.id).limit(INLINE_LIMIT).all())
    return recount_leaves(db, leaves, actor_user_id=actor_user_id, note="Leave plan changed")


def recount_dates(db: Session, company_id: int, dates: Iterable[date], *, actor_user_id: Optional[int] = None,
                  note: str = "Holiday calendar changed", employee_ids: Optional[Iterable[int]] = None) -> int:
    """Approved requests with an active row on one of ``dates`` (of ``employee_ids`` when given) are regenerated."""
    wanted = sorted(set(dates))
    if not wanted:
        return 0
    query = db.query(LeaveDay.leave_id).filter(LeaveDay.company_id == company_id, LeaveDay.date.in_(wanted),
                                               LeaveDay.voided_at.is_(None))
    if employee_ids is not None:
        query = query.filter(LeaveDay.employee_id.in_(sorted(set(employee_ids)) or [0]))
    ids = [i for (i,) in query.distinct()]
    leaves = (db.query(Leave).filter(Leave.company_id == company_id, Leave.id.in_(ids or [0]),
                                     Leave.status == "approved").order_by(Leave.id).all())
    return recount_leaves(db, leaves, actor_user_id=actor_user_id, note=note)

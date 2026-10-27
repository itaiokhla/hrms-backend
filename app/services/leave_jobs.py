"""Leave recalculation after work-calendar changes (LH SPINE §6.12; 04-holiday/03 §5.2 step 9; 04-holiday/01 §4.3 step 6).

When a work-week version, a branch link or a criteria row changes which dates are weekly offs, approved leave on
those dates is re-decided per date (``time_inputs.reevaluate_leave_days``: decreases credited, increases debited, each
with its own ledger row and change-log entry; locked periods reach payroll through ``source_change_log``). Small
changes run inline in the business transaction (at most ``RECALC_INLINE_LIMIT`` leave days); larger ones become one
``leave.recalculate`` job keyed by the change so a retry never doubles a recount.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.time_inputs import LeaveDay
from app.payroll import workweek
from app.payroll.services import jobs, time_inputs

RECALC_INLINE_LIMIT = 200
HORIZON_DAYS = time_inputs.MAX_RANGE_DAYS  # same horizon as day facts (04-holiday/03 §5.7)


def horizon(date_from: date) -> date:
    return date_from + timedelta(days=HORIZON_DAYS - 1)


def recount_dates(db: Session, company_id: int, dates: Iterable[date], *, key: str,
                  actor_user_id: Optional[int] = None) -> dict:
    """Re-decide approved leave on ``dates``: inline when small, else one job (idempotency key ``key``)."""
    wanted = sorted(set(dates))
    if not wanted:
        return {"mode": "none", "dates": 0, "leave_days": 0, "changed": 0}
    affected = db.query(func.count(LeaveDay.id)).filter(LeaveDay.company_id == company_id, LeaveDay.date.in_(wanted),
                                                        LeaveDay.voided_at.is_(None)).scalar() or 0
    if affected <= RECALC_INLINE_LIMIT:
        changed = time_inputs.reevaluate_leave_days(db, company_id, wanted, actor_user_id=actor_user_id)
        return {"mode": "inline", "dates": len(wanted), "leave_days": affected, "changed": changed}
    job = jobs.enqueue(db, company_id=company_id, type="leave.recalculate",
                       payload={"dates": [d.isoformat() for d in wanted]}, idempotency_key=key[:120],
                       created_by_user_id=actor_user_id, read_permission="holiday:manage")
    return {"mode": "job", "job_id": job.id, "dates": len(wanted), "leave_days": affected, "changed": None}


def changed_dates(before: Optional[workweek.WorkWeekPattern], after: workweek.WorkWeekPattern, start: date,
                  end: date) -> List[date]:
    """Dates whose weekly-off portion differs (every date when there was no pattern before)."""
    out = []
    for day in workweek.dates_between(start, end):
        if before is None or workweek.weekly_off_portion(before, day) != workweek.weekly_off_portion(after, day):
            out.append(day)
    return out


def recount_pattern_switch(db: Session, company_id: int, *, branch_id: int, old_pattern_id: Optional[int],
                           new_pattern_id: Optional[int], date_from: date,
                           actor_user_id: Optional[int] = None) -> dict:
    """A branch (or group) moves from one pattern to another from ``date_from``: recount the dates that change."""
    from app.services.holiday_assignments import WorkWeekResolver

    if not new_pattern_id or old_pattern_id == new_pattern_id:
        return {"mode": "none", "dates": 0, "leave_days": 0, "changed": 0}
    resolver = WorkWeekResolver(db, company_id)
    end = horizon(date_from)
    dates = [day for day in workweek.dates_between(date_from, end)
             if old_pattern_id is None
             or workweek.weekly_off_portion(resolver.version_on(old_pattern_id, day).pattern, day)
             != workweek.weekly_off_portion(resolver.version_on(new_pattern_id, day).pattern, day)]
    return recount_dates(db, company_id, dates, key=f"recalc:link:{branch_id}:{new_pattern_id}:{date_from.isoformat()}",
                         actor_user_id=actor_user_id)


@jobs.register("leave.recalculate")
def _recalculate_job(ctx: jobs.JobContext) -> dict:
    payload = ctx.payload or {}
    if payload.get("dates"):
        dates = [date.fromisoformat(d) for d in payload["dates"]]
    elif payload.get("date_from"):
        start = date.fromisoformat(payload["date_from"])
        dates = list(workweek.dates_between(start, horizon(start)))
    else:
        return {"changed": 0}
    changed = time_inputs.reevaluate_leave_days(ctx.db, ctx.company_id, dates, actor_user_id=ctx.created_by_user_id)
    return {"dates": len(dates), "changed": changed}

"""Which dates of a request are charged, and how much (SPINE §6.1-§6.2; 03-leave/01 §3.6, 03-leave/02 §3).

Per date: the requested portion (FULL, or SECOND_HALF on the first date / FIRST_HALF on the last) meets the day's
``working_portion`` from day facts. A charged date's row covers the overlap of the two (FULL leave on a FIRST_HALF
holiday charges the SECOND_HALF, 0.5). The type rule's counting basis may also charge non-working dates
(CALENDAR_DAYS every date; CALENDAR_EXCL_HOLIDAYS every date except holidays; FIXED_WEEKDAYS the listed weekdays
except holidays), and the leave sandwich charges a run of weekly offs / holidays lying between two charged days of
the same request, or between this request and another pending or approved sandwich-enabled leave. ``charge_reason``
records why (WORKING, BASIS, SANDWICH).
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from app.models.leave import Leave
from app.payroll.services import time_inputs

ZERO, HALF, ONE = Decimal(0), Decimal("0.5"), Decimal(1)
_HALVES = {"FULL": {"FIRST_HALF", "SECOND_HALF"}, "FIRST_HALF": {"FIRST_HALF"}, "SECOND_HALF": {"SECOND_HALF"},
           "NONE": set()}


def _portion(halves: set) -> Optional[str]:
    if halves == {"FIRST_HALF", "SECOND_HALF"}:
        return "FULL"
    if len(halves) == 1:
        return next(iter(halves))
    return None


@dataclass
class DayCharge:
    date: date
    requested: str           # portion asked for on this date
    portion: str             # portion the leave row covers (after overlap with the working part)
    units: Decimal           # size of the row's portion (1 or 0.5)
    charged: Decimal         # units charged to the balance (0 when not charged)
    charge_reason: Optional[str]
    working_portion: str
    employed: bool
    weekly_off: bool
    holiday: Optional[str]
    holiday_type: Optional[str]

    @property
    def counted(self) -> bool:
        return self.charged > 0

    def json(self) -> dict:
        return {"date": self.date.isoformat(), "requested": self.requested, "portion": self.portion,
                "charged": float(self.charged) if self.charged % 1 else int(self.charged),
                "charge_reason": self.charge_reason, "working_portion": self.working_portion,
                "employed": self.employed, "weekly_off": self.weekly_off, "holiday": self.holiday,
                "holiday_type": self.holiday_type}


def _basis_charges(facts, basis: Mapping) -> bool:
    kind = basis.get("basis") or "WORKING_DAYS"
    is_holiday = facts.holiday is not None and facts.holiday.non_working
    if kind == "CALENDAR_DAYS":
        return True
    if kind == "CALENDAR_EXCL_HOLIDAYS":
        return not is_holiday
    if kind == "FIXED_WEEKDAYS":
        return facts.date.weekday() in (basis.get("weekdays") or []) and not is_holiday
    return False


def _sandwich_kind(facts) -> Optional[str]:
    if facts.holiday is not None and facts.holiday.non_working:
        return "HOLIDAYS"
    if facts.weekly_off:
        return "WEEKLY_OFFS"
    return None


def charge(db: Session, company_id: int, employee_id: int, rule: Optional[Mapping], start: date, end: date,
           start_portion: str = "FULL", end_portion: str = "FULL", *, facts_list=None,
           exclude_leave_id: Optional[int] = None) -> Tuple[Decimal, List[DayCharge]]:
    """Charged units and the per-date plan of a request under its type rule (None rule = working days only)."""
    rule = rule or {}
    basis = rule.get("counting_basis") or {"basis": "WORKING_DAYS"}
    facts_list = facts_list or time_inputs.day_facts(db, company_id, employee_id, start, end)
    days: List[DayCharge] = []
    for facts in facts_list:
        requested = time_inputs.portion_on(facts.date, start, end, start_portion, end_portion)
        working = facts.working_portion if facts.employed else "NONE"
        overlap = _portion(_HALVES[requested] & _HALVES.get(working, set()))
        holiday = facts.holiday.name if facts.holiday is not None and facts.holiday.non_working else None
        htype = facts.holiday.type if facts.holiday is not None else None
        if not facts.employed:
            day = DayCharge(facts.date, requested, requested, _size(requested), ZERO, None, working, False,
                            facts.weekly_off, holiday, htype)
        elif overlap is not None:
            day = DayCharge(facts.date, requested, overlap, _size(overlap), _size(overlap), "WORKING", working, True,
                            facts.weekly_off, holiday, htype)
        elif _basis_charges(facts, basis):
            day = DayCharge(facts.date, requested, requested, _size(requested), _size(requested), "BASIS", working,
                            True, facts.weekly_off, holiday, htype)
        else:
            day = DayCharge(facts.date, requested, requested, _size(requested), ZERO, None, working, True,
                            facts.weekly_off, holiday, htype)
        days.append(day)
    _apply_sandwich(db, company_id, employee_id, rule, days, exclude_leave_id=exclude_leave_id)
    total = sum((d.charged for d in days), ZERO)
    return total, days


def _size(portion: str) -> Decimal:
    return ONE if portion == "FULL" else HALF


def _sandwich_on(rule: Mapping, kind: Optional[str]) -> bool:
    mode = (rule.get("sandwich") or {}).get("mode") or "OFF"
    return kind is not None and (mode == "BOTH" or mode == kind)


def _apply_sandwich(db: Session, company_id: int, employee_id: int, rule: Mapping, days: List[DayCharge], *,
                    exclude_leave_id: Optional[int]) -> None:
    mode = (rule.get("sandwich") or {}).get("mode") or "OFF"
    if mode == "OFF" or not days:
        return
    edges_count = (rule.get("sandwich") or {}).get("half_day_edges") == "COUNT"

    def closes(day: DayCharge) -> bool:
        return day.counted and (day.portion == "FULL" or edges_count)

    # Runs inside the request.
    i = 0
    while i < len(days):
        if days[i].counted or not days[i].employed:
            i += 1
            continue
        j = i
        while j < len(days) and not days[j].counted and days[j].employed and days[j].working_portion == "NONE":
            j += 1
        run = days[i:j]
        inside = i > 0 and j < len(days) and closes(days[i - 1]) and closes(days[j])
        if run and inside and all(_sandwich_on(rule, _kind_of(d)) for d in run):
            for d in run:
                d.charged, d.charge_reason = d.units, "SANDWICH"
        i = max(j, i + 1)
    # Runs at the edges, closed by another pending or approved leave of the employee.
    _edge(db, company_id, employee_id, rule, days, exclude_leave_id, leading=True)
    _edge(db, company_id, employee_id, rule, days, exclude_leave_id, leading=False)


def _kind_of(day: DayCharge) -> Optional[str]:
    if day.holiday is not None:
        return "HOLIDAYS"
    if day.weekly_off:
        return "WEEKLY_OFFS"
    return None


def _edge(db: Session, company_id: int, employee_id: int, rule: Mapping, days: List[DayCharge],
          exclude_leave_id: Optional[int], *, leading: bool) -> None:
    ordered = days if leading else list(reversed(days))
    run = []
    for d in ordered:
        if d.counted or d.working_portion != "NONE" or not d.employed:
            break
        run.append(d)
    if not run or len(run) == len(days):
        return
    if not ordered[len(run)].counted or not all(_sandwich_on(rule, _kind_of(d)) for d in run):
        return
    # The day just outside the request, next to the run, must be leave of the employee (pending or approved) of a
    # sandwich-enabled type (SPINE §6.2). ``run[0]`` is the request's first date (leading) or last date (trailing).
    outside = (run[0].date - timedelta(days=1)) if leading else (run[0].date + timedelta(days=1))
    query = db.query(Leave).filter(Leave.company_id == company_id, Leave.employee_id == employee_id,
                                   Leave.status.in_(("pending", "approved")), Leave.start_date <= outside,
                                   Leave.end_date >= outside)
    if exclude_leave_id is not None:
        query = query.filter(Leave.id != exclude_leave_id)
    from app.services import leave_plans

    for other in query.all():
        other_rule = leave_plans.rules_for(db, company_id, employee_id, other.leave_type, other.start_date) or {}
        if ((other_rule.get("sandwich") or {}).get("mode") or "OFF") == "OFF":
            continue
        for d in run:
            d.charged, d.charge_reason = d.units, "SANDWICH"
        return

"""Leave request lifecycle v2: one validator, preview, drafts, submit, on-behalf and HR-recorded leave, withdraw,
return and resubmit (LH 03-leave/03 §3-§9; rule catalogue 03-leave/01 §8.1).

``validate`` runs every stage and returns a ``Preview`` with ``issues[]`` (ERROR / WARNING / INFO, stable codes); it
never writes. ``submit`` locks the employee (LCK-02), validates again under the lock, refuses on any ERROR and creates
the request (DRAFT, PENDING or, for HR-recorded leave, APPROVED). PENDING reserves units; DRAFT and RETURNED reserve
nothing. Approval routing uses the company's leave chain with per-step conditions (leave types, units, legal
entities, sensitivity); a type rule with ``approval.auto_approve_eligible`` approves a clean request at once.

``SPLIT_TO_UNPAID``: when the balance is short, the latest dates move to a linked sibling request of the unpaid type
(``leave.unpaid_type_code``) that is decided, cancelled and paid together with its parent.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload

from app.core import authz, concurrency
from app.core.errors import AppError, bad_request, forbidden, not_found
from app.core.principal import Principal
from app.core.security import utcnow
from app.models.employee import Employee
from app.models.leave import ApprovalChainStep, Leave, LeaveApproval, LeaveType, V2_STATUS
from app.models.leave_v2 import LeaveAttachment, LeaveBlackout, LeaveCoverageRule, LeaveDocumentRequest, LeaveMessage
from app.models.time_inputs import EmployeeSuspension, LeaveDay, LopInput, OnDutyRequest
from app.services import leave_access, leave_charging, leave_ledger, leave_plans
from app.services.audit_log_service import AuditLogService
from app.services.leave_cycles import add_months

ZERO = Decimal(0)
MAX_REQUEST_SPAN_DAYS = 366
# 03-leave/01 §8: HTTP status per error code (422 for plan rules, today's 400 for the legacy codes, 409 conflicts).
STATUS = {"NOT_ELIGIBLE": 422, "NOTICE_TOO_SHORT": 422, "BACKDATE_LIMIT": 422, "FUTURE_LIMIT": 422, "MIN_UNITS": 422,
          "MAX_UNITS": 422, "MAX_INSTANCES": 422, "CLUBBING_NOT_ALLOWED": 422, "BLACKOUT_PERIOD": 422,
          "COVERAGE_CONFLICT": 422, "DOCUMENT_REQUIRED": 422, "NEGATIVE_LIMIT": 422, "PLAN_NOT_ASSIGNED": 422,
          "INSUFFICIENT_BALANCE": 400, "INVALID_LEAVE_TYPE": 400, "INVALID_PORTION": 400, "HALF_DAY_DISABLED": 400,
          "HALF_DAY_NOT_ALLOWED": 400, "REASON_REQUIRED": 400, "OUTSIDE_EMPLOYMENT": 400, "NO_WORKING_DAYS": 400,
          "INVALID_RANGE": 400, "LEAVE_OVERLAP": 409, "SLOT_CONFLICT": 409, "EVENT_REQUIRED": 422,
    "EVENT_WINDOW_CLOSED": 422}
PORTION_ERRORS = ("INVALID_PORTION", "HALF_DAY_DISABLED", "HALF_DAY_NOT_ALLOWED")


@dataclass
class Issue:
    code: str
    severity: str  # ERROR | WARNING | INFO
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    field: Optional[str] = None

    def json(self) -> dict:
        return {"code": self.code, "severity": self.severity, "message": self.message, "details": self.details,
                **({"field": self.field} if self.field else {})}


@dataclass
class Preview:
    employee: Employee
    leave_type: Optional[LeaveType]
    rule: Optional[dict]
    start: date
    end: date
    start_portion: str
    end_portion: str
    days: List[leave_charging.DayCharge] = field(default_factory=list)
    charged: Decimal = ZERO
    issues: List[Issue] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)
    balance_before: Optional[Decimal] = None
    balance_after: Optional[Decimal] = None
    split_units: Decimal = ZERO
    split_dates: List[date] = field(default_factory=list)
    unpaid_type: Optional[LeaveType] = None
    documents_required: List[dict] = field(default_factory=list)
    plan: Optional[dict] = None
    plan_version_id: Optional[int] = None
    auto_approve: bool = False
    source: str = "SELF"
    event: Optional[Any] = None  # the OPEN leave_events row of an EVENT request
    event_info: Optional[dict] = None

    @property
    def errors(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == "ERROR"]

    @property
    def warnings(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == "WARNING"]

    def add(self, code: str, severity: str, message: str, **details) -> None:
        field_name = details.pop("field", None)
        self.issues.append(Issue(code, severity, message, details, field_name))

    def json(self) -> dict:
        num = leave_ledger._num
        return {
            "employee_id": self.employee.id, "employee_name": self.employee.full_name,
            "leave_type": self.leave_type.code if self.leave_type else None,
            "leave_type_name": self.leave_type.name if self.leave_type else None,
            "unit": self.leave_type.unit if self.leave_type else "DAY",
            "start_date": self.start.isoformat(), "end_date": self.end.isoformat(),
            "start_portion": self.start_portion, "end_portion": self.end_portion,
            "days": [d.json() for d in self.days], "charged_units": num(self.charged),
            "balance_before": num(self.balance_before), "balance_after": num(self.balance_after),
            "unlimited": bool(self.leave_type and self.leave_type.is_unlimited),
            "issues": [i.json() for i in self.issues], "flags": self.flags,
            "blocking": bool(self.errors), "split": None if not self.split_units else {
                "units": num(self.split_units), "dates": [d.isoformat() for d in self.split_dates],
                "leave_type": self.unpaid_type.code if self.unpaid_type else None},
            "documents_required": self.documents_required, "plan": self.plan, "auto_approve": self.auto_approve,
            "reason_rule": ((self.rule or {}).get("request") or {}).get("reason", "OPTIONAL"),
            "documents_allowed": ((self.rule or {}).get("documents") or {}).get("allowed", True),
            "event": self.event_info,
        }

    def raise_first_error(self) -> None:
        if not self.errors:
            return
        first = self.errors[0]
        raise AppError(STATUS.get(first.code, 422), first.message, first.code,
                       extra={"issues": [i.json() for i in self.issues], "details": first.details})


class StoredRequest:
    """A saved request seen as validator input (resubmission, recount)."""

    def __init__(self, leave: Leave):
        for key in ("leave_type", "start_date", "end_date", "start_portion", "end_portion", "reason", "is_emergency",
                    "event_id", "contact_note", "handover_employee_id"):
            setattr(self, key, getattr(leave, key))
        self.employee_id = leave.employee_id
        self.attachment_ids: List[int] = []


# --------------------------------------------------------------------------- subject and type

def resolve_subject(db: Session, principal: Principal, employee_id: Optional[int], *, mode: str) -> Tuple[Employee, str]:
    """Self, on-behalf (``leave:apply_on_behalf`` + setting) or HR-recorded (``leave:record``) — always scoped."""
    if mode == "RECORD":
        if employee_id is None or not principal.has("leave:record"):
            raise forbidden("Recording leave needs the leave recording permission.")
        return authz.ensure_employee_access(db, principal, "leave:record", employee_id), "HR_RECORDED"
    if employee_id is None or employee_id == principal.employee_id:
        if not principal.has("leave:apply"):
            raise forbidden()
        return authz.own_employee(db, principal), "SELF"
    if not principal.has("leave:apply_on_behalf"):
        raise forbidden("Applying for someone else needs the apply-on-behalf permission.")
    employee = authz.ensure_employee_access(db, principal, "leave:apply_on_behalf", employee_id)
    if not leave_access.setting(db, principal.company_id, "leave.apply_on_behalf.enabled", default=True):
        raise forbidden("Applying leave for others is turned off.")
    return employee, "ON_BEHALF"


def local_today(db: Session, employee: Employee) -> date:
    from app.services import org_structure_service

    return org_structure_service.employee_local_today(db, employee)


# --------------------------------------------------------------------------- validator stages

def _type(db: Session, company_id: int, code: str) -> Optional[LeaveType]:
    return db.query(LeaveType).filter(LeaveType.company_id == company_id, LeaveType.code == code).first()


def _portions(db: Session, pv: Preview) -> None:
    sp, ep = pv.start_portion, pv.end_portion
    if sp not in ("FULL", "SECOND_HALF") or ep not in ("FULL", "FIRST_HALF") or (
            pv.start == pv.end and sp == "SECOND_HALF" and ep == "FIRST_HALF"):
        pv.add("INVALID_PORTION", "ERROR", "Choose either the first or the second half of the day.", field="start_portion")
        return
    if (sp, ep) == ("FULL", "FULL"):
        return
    if leave_access.setting(db, pv.employee.company_id, "leave.half_day.enabled", on=pv.start, default=True) is not True:
        pv.add("HALF_DAY_DISABLED", "ERROR", "Half-day leave is not enabled for your company.", field="start_portion")
    elif not pv.leave_type.allow_half_day or ((pv.rule or {}).get("request") or {}).get("portions") == "FULL_ONLY":
        pv.add("HALF_DAY_NOT_ALLOWED", "ERROR", f"{pv.leave_type.name} is taken in whole days only.",
               field="start_portion")


def _dates(db: Session, pv: Preview, today: date, *, mode: str) -> None:
    rules = (pv.rule or {}).get("request") or {}
    if pv.start > pv.end:
        pv.add("INVALID_RANGE", "ERROR", "Start date must be on or before the end date.", field="end_date")
        return
    if (pv.end - pv.start).days + 1 > MAX_REQUEST_SPAN_DAYS:
        pv.add("FUTURE_LIMIT", "ERROR", "A single request cannot exceed one year.", reason="MAX_SPAN")
    backdate = int(rules.get("backdate_limit_days", 0) or 0)
    earliest = today - timedelta(days=backdate)
    if pv.start < earliest:
        if mode == "RECORD":
            pv.add("BACKDATE_LIMIT", "WARNING", "This leave starts before the backdating limit (recorded by HR).",
                   limit=backdate, earliest_start=earliest.isoformat())
        else:
            message = ("Leave cannot start in the past." if backdate == 0 else
                       f"Leave can start at most {backdate} day(s) back (from {earliest:%d %b %Y}).")
            pv.add("BACKDATE_LIMIT", "ERROR", message, limit=backdate, earliest_start=earliest.isoformat(),
                   field="start_date")
    future = int(rules.get("future_limit_days", 365) or 365)
    if pv.end > today + timedelta(days=future):
        pv.add("FUTURE_LIMIT", "ERROR", f"Leave can be booked at most {future} days ahead.", limit=future,
               field="end_date")


def _eligibility(db: Session, pv: Preview) -> None:
    flagged_verify = False
    for day in pv.days:
        if not day.employed:
            continue
        elig = leave_plans.eligibility(db, pv.employee.company_id, pv.employee, pv.leave_type, pv.rule, day.date)
        if elig.status in ("NOT_ELIGIBLE", "ACCRUE_ONLY"):
            failure = elig.first_failure() or {}
            structural = failure.get("code") in leave_plans.STRUCTURAL_CHECKS
            message = ("This leave type is not available to you." if structural else
                       f"Not available on {day.date:%d %b %Y}" + (
                           f"; available from {elig.available_from:%d %b %Y}." if elig.available_from else "."))
            details = {"date": day.date.isoformat()}
            if not structural:
                details["check"] = failure.get("code")
                if elig.available_from:
                    details["available_from"] = elig.available_from.isoformat()
            pv.add("NOT_ELIGIBLE", "ERROR", message, **details)
            return
        if elig.status == "ELIGIBLE_VERIFY" and not flagged_verify:
            flagged_verify = True
            pv.flags.append("ELIGIBILITY_VERIFY")
            pv.add("ELIGIBILITY_VERIFY", "INFO", "HR will confirm your eligibility for this leave.")


def _taken_leaves(db: Session, pv: Preview, start: date, end: date, *, exclude_id: Optional[int]) -> List[Leave]:
    query = db.query(Leave).filter(Leave.company_id == pv.employee.company_id, Leave.employee_id == pv.employee.id,
                                   Leave.status.in_(("pending", "approved")), Leave.start_date <= end,
                                   Leave.end_date >= start)
    if exclude_id is not None:
        query = query.filter(Leave.id != exclude_id, or_(Leave.parent_leave_id.is_(None),
                                                         Leave.parent_leave_id != exclude_id))
    return query.all()


def _overlap(db: Session, pv: Preview, exclude_id: Optional[int]) -> None:
    from app.payroll.services import time_inputs

    wanted = {d.date: d.requested for d in pv.days}
    for other in _taken_leaves(db, pv, pv.start, pv.end, exclude_id=exclude_id):
        if other.status == "approved":
            # Approved leave holds only its active days: dates given back by a partial cancel or recall are free.
            slots = [(r.date, r.portion) for r in db.query(LeaveDay).filter(
                LeaveDay.company_id == other.company_id, LeaveDay.leave_id == other.id, LeaveDay.voided_at.is_(None),
                LeaveDay.date >= pv.start, LeaveDay.date <= pv.end).order_by(LeaveDay.date)]
            if not slots and not db.query(LeaveDay.id).filter(LeaveDay.company_id == other.company_id,
                                                              LeaveDay.leave_id == other.id).first():
                slots = None  # approved before day rows existed (legacy): fall back to its date range
        else:
            slots = None
        if slots is None:
            slots = []
            cur = max(other.start_date, pv.start)
            while cur <= min(other.end_date, pv.end):
                slots.append((cur, time_inputs.portion_on(cur, other.start_date, other.end_date,
                                                          other.start_portion or "FULL", other.end_portion or "FULL")))
                cur += timedelta(days=1)
        for cur, theirs in slots:
            mine = wanted.get(cur)
            if mine is not None and (mine == "FULL" or theirs == "FULL" or mine == theirs):
                pv.add("LEAVE_OVERLAP", "ERROR", "You already have leave booked on some of these dates.",
                       with_leave_id=other.id, date=cur.isoformat())
                return
    cid, emp = pv.employee.company_id, pv.employee.id
    lops = db.query(LopInput).filter(LopInput.company_id == cid, LopInput.employee_id == emp,
                                     LopInput.date >= pv.start, LopInput.date <= pv.end,
                                     LopInput.status.in_(("PENDING", "APPROVED"))).first()
    duty = db.query(OnDutyRequest).filter(OnDutyRequest.company_id == cid, OnDutyRequest.employee_id == emp,
                                          OnDutyRequest.date >= pv.start, OnDutyRequest.date <= pv.end,
                                          OnDutyRequest.status.in_(("PENDING", "APPROVED"))).first()
    suspended = db.query(EmployeeSuspension).filter(
        EmployeeSuspension.company_id == cid, EmployeeSuspension.employee_id == emp,
        EmployeeSuspension.status == "ACTIVE", EmployeeSuspension.start_date <= pv.end,
        EmployeeSuspension.end_date >= pv.start).first()
    for found, what in ((lops, "a loss-of-pay entry"), (duty, "an on-duty request"), (suspended, "a suspension")):
        if found is not None:
            pv.add("SLOT_CONFLICT", "ERROR", f"These dates already have {what}.", conflict=what)
            return


def _period(kind: str, day: date, cycle) -> Tuple[date, date]:
    if kind == "MONTH":
        start = day.replace(day=1)
        return start, add_months(start, 1) - timedelta(days=1)
    if kind == "QUARTER":
        start = date(day.year, (day.month - 1) // 3 * 3 + 1, 1)
        return start, add_months(start, 3) - timedelta(days=1)
    if kind == "ROLLING_12M":
        return day - timedelta(days=364), day
    if kind == "LIFETIME":
        return date(1900, 1, 1), date(9999, 12, 31)
    return cycle.start, cycle.end


def _pending_part(db: Session, pv: Preview, other: Leave, start: date, end: date) -> Decimal:
    """Units a PENDING request charges on dates in [start, end] (its whole charge when it lies inside)."""
    if start <= other.start_date and other.end_date <= end:
        return Decimal(other.number_of_days or 0)
    cid = pv.employee.company_id
    rule = leave_plans.rules_for(db, cid, other.employee_id, other.leave_type, other.start_date)
    _, days = leave_charging.charge(db, cid, other.employee_id, rule, other.start_date, other.end_date,
                                    other.start_portion or "FULL", other.end_portion or "FULL",
                                    exclude_leave_id=other.id)
    return sum((d.charged for d in days if start <= d.date <= end), ZERO)


def _charged_in(db: Session, pv: Preview, start: date, end: date, exclude_id: Optional[int]) -> Decimal:
    """Charged units of PENDING + APPROVED same-type requests on dates in [start, end] (a request crossing the
    period's edge counts only its dates inside the period)."""
    total = ZERO
    for other in _taken_leaves(db, pv, start, end, exclude_id=exclude_id):
        if other.leave_type != pv.leave_type.code:
            continue
        if other.status == "approved":
            total += Decimal(db.query(func.coalesce(func.sum(LeaveDay.units), 0)).filter(
                LeaveDay.company_id == other.company_id, LeaveDay.leave_id == other.id, LeaveDay.voided_at.is_(None),
                LeaveDay.counts_against_balance.is_(True), LeaveDay.date >= start, LeaveDay.date <= end).scalar() or 0)
        else:
            total += _pending_part(db, pv, other, start, end)
    return total


def _cap_periods(db: Session, pv: Preview, kind: str) -> List[Tuple[date, date]]:
    """Every period of ``kind`` the request charges a date in (a request crossing a month end is checked against
    both months' caps); a rolling window ends on the request's last charged date."""
    counted = [d.date for d in pv.days if d.counted] or [pv.start]
    if kind == "ROLLING_12M":
        return [_period(kind, max(counted), None)]
    periods = set()
    for day in counted:
        cycle = leave_plans.cycle_of(db, pv.employee.company_id, pv.employee, pv.rule, day) if kind == "CYCLE" \
            else None
        periods.add(_period(kind, day, cycle))
    return sorted(periods)


def _size_and_caps(db: Session, pv: Preview, exclude_id: Optional[int], *, mode: str) -> None:
    rules = (pv.rule or {}).get("request") or {}
    if rules.get("min_units") and pv.charged < Decimal(str(rules["min_units"])) and mode != "RECORD":
        pv.add("MIN_UNITS", "ERROR", f"The smallest request is {rules['min_units']} unit(s).", limit=rules["min_units"])
    if rules.get("max_units") and pv.charged > Decimal(str(rules["max_units"])):
        pv.add("MAX_UNITS", "ERROR", f"The largest request is {rules['max_units']} unit(s).", detail="request",
               limit=rules["max_units"])
    if rules.get("max_consecutive_units"):
        joined = pv.charged + _adjacent_units(db, pv, exclude_id)
        if joined > Decimal(str(rules["max_consecutive_units"])):
            pv.add("MAX_UNITS", "ERROR", f"At most {rules['max_consecutive_units']} consecutive unit(s) of this leave.",
                   detail="consecutive", limit=rules["max_consecutive_units"])
    cycle = leave_plans.cycle_of(db, pv.employee.company_id, pv.employee, pv.rule, pv.start)
    for cap in rules.get("caps") or []:
        limit = Decimal(str(cap["max_units"]))
        for p_start, p_end in _cap_periods(db, pv, cap["period"]):
            mine = sum((d.charged for d in pv.days if p_start <= d.date <= p_end), ZERO)
            used = _charged_in(db, pv, p_start, p_end, exclude_id)
            if used + mine > limit:
                pv.add("MAX_UNITS", "ERROR", f"At most {cap['max_units']} unit(s) per "
                                             f"{cap['period'].lower().replace('_', ' ')} (already "
                                             f"{leave_ledger._num(used)}).",
                       detail="cap", period=cap["period"], period_start=p_start.isoformat(),
                       used=leave_ledger._num(used), requested=leave_ledger._num(mine), limit=cap["max_units"])
                break
    for cap in rules.get("max_instances") or []:
        p_start, p_end = _period(cap["period"], pv.start, cycle)
        count = sum(1 for other in _taken_leaves(db, pv, p_start, p_end, exclude_id=exclude_id)
                    if other.leave_type == pv.leave_type.code and p_start <= other.start_date <= p_end)
        if count + 1 > int(cap["max_requests"]):
            pv.add("MAX_INSTANCES", "ERROR", f"At most {cap['max_requests']} request(s) per "
                                             f"{cap['period'].lower().replace('_', ' ')}.", limit=cap["max_requests"])


def _non_working(db: Session, pv: Preview, day: date) -> bool:
    from app.payroll.services import time_inputs

    facts = time_inputs.day_facts(db, pv.employee.company_id, pv.employee.id, day, day)[0]
    return facts.employed and facts.working_portion == "NONE"


def _walk(db: Session, pv: Preview, start: date, step: int, limit: int = 10) -> date:
    """From ``start`` (exclusive) walk over non-working dates; returns the first working-or-boundary date."""
    day = start + timedelta(days=step)
    for _ in range(limit):
        if not _non_working(db, pv, day):
            return day
        day += timedelta(days=step)
    return day


def _adjacent_units(db: Session, pv: Preview, exclude_id: Optional[int]) -> Decimal:
    before, after = _walk(db, pv, pv.start, -1), _walk(db, pv, pv.end, 1)
    total = ZERO
    for other in _taken_leaves(db, pv, before, after, exclude_id=exclude_id):
        if other.leave_type == pv.leave_type.code and (other.end_date == before or other.start_date == after):
            total += Decimal(other.number_of_days or 0)
    return total


def _notice(db: Session, pv: Preview, today: date, *, is_emergency: bool, reason: Optional[str], mode: str) -> None:
    rules = (pv.rule or {}).get("request") or {}
    notice = rules.get("min_notice")
    if not notice or mode == "RECORD":
        return
    required = int(notice.get("value") or 0)
    for step in sorted(notice.get("scale") or [], key=lambda s: Decimal(str(s["min_units"]))):
        if pv.charged >= Decimal(str(step["min_units"])):
            required = max(required, int(step["value"]))
    if notice.get("unit") == "WORKING_DAYS":
        given, cur = 0, today + timedelta(days=1)
        while cur < pv.start and given <= required:
            if not _non_working(db, pv, cur):
                given += 1
            cur += timedelta(days=1)
    else:
        given = (pv.start - today).days
    if given >= required:
        return
    emergency = rules.get("emergency_exception") or {}
    if is_emergency and emergency.get("enabled") and pv.charged <= Decimal(str(emergency.get("max_units") or "1")):
        pv.add("NOTICE_TOO_SHORT", "INFO", "Short notice (emergency).", required=required, given=given)
        pv.flags.append("EMERGENCY")
        if emergency.get("reason_required", True) and not (reason or "").strip():
            pv.add("REASON_REQUIRED", "ERROR", "Give a reason for an emergency request.", field="reason")
        return
    earliest = today + timedelta(days=required)
    pv.add("NOTICE_TOO_SHORT", "ERROR", f"This leave needs {required} {notice.get('unit', 'CALENDAR_DAYS').lower().replace('_', ' ')}"
                                        f" of notice; you gave {given}.", required=required, given=given,
           earliest_start=earliest.isoformat())


def _clubbing(db: Session, pv: Preview, exclude_id: Optional[int], *, mode: str) -> None:
    rules = (pv.rule or {}).get("request") or {}
    club = rules.get("clubbing") or {}
    before, after = _walk(db, pv, pv.start, -1), _walk(db, pv, pv.end, 1)
    gap = int(club.get("gap_days") or 0)
    if club.get("not_with"):
        lo, hi = before - timedelta(days=gap), after + timedelta(days=gap)
        for other in _taken_leaves(db, pv, lo, hi, exclude_id=exclude_id):
            # The other leave may end (start) on a non-working day between it and this request: it still adjoins.
            if other.leave_type in club["not_with"] and (lo <= other.end_date < pv.start or pv.end < other.start_date <= hi):
                pv.add("CLUBBING_NOT_ALLOWED", "ERROR", "This leave cannot be joined to your other leave.",
                       with_leave_id=other.id, type=other.leave_type)
                return
    adjacent = rules.get("adjacent_non_working") or "ALLOW"
    if adjacent != "ALLOW" and (_non_working(db, pv, pv.start - timedelta(days=1))
                                or _non_working(db, pv, pv.end + timedelta(days=1))):
        if adjacent == "BLOCK" and mode != "RECORD":
            pv.add("CLUBBING_NOT_ALLOWED", "ERROR", "This leave cannot be next to a holiday or weekly off.",
                   detail="ADJACENT_NON_WORKING")
        else:
            pv.add("ADJACENT_NON_WORKING", "WARNING", "This leave is next to a holiday or weekly off.")


def _blackouts(db: Session, pv: Preview, *, mode: str) -> None:
    if ((pv.rule or {}).get("request") or {}).get("blackouts") == "EXEMPT":
        return
    where = leave_plans.placement(db, pv.employee.company_id, pv.employee.id, pv.start)
    rows = db.query(LeaveBlackout).filter(LeaveBlackout.company_id == pv.employee.company_id,
                                          LeaveBlackout.status == "ACTIVE", LeaveBlackout.from_date <= pv.end,
                                          LeaveBlackout.to_date >= pv.start).all()
    charged_dates = [d.date for d in pv.days if d.counted]
    for row in rows:
        if row.leave_type_codes and pv.leave_type.code not in row.leave_type_codes:
            continue
        if row.legal_entity_id and (where is None or where.legal_entity_id != row.legal_entity_id):
            continue
        if row.branch_ids and (where is None or where.branch_id not in row.branch_ids):
            continue
        if row.department_ids and pv.employee.department_id not in row.department_ids:
            continue
        hit = [d for d in charged_dates if row.from_date <= d <= row.to_date]
        if not hit:
            continue
        severity = "ERROR" if row.mode == "BLOCK" and mode != "RECORD" else "WARNING"
        pv.add("BLACKOUT_PERIOD", severity, f"{row.name}: leave is {'not allowed' if row.mode == 'BLOCK' else 'discouraged'}"
                                            f" from {row.from_date:%d %b} to {row.to_date:%d %b %Y}.",
               blackout_id=row.id, dates=[d.isoformat() for d in hit[:10]])


def _group_members(db: Session, company_id: int, rule: LeaveCoverageRule, on: date) -> List[int]:
    from app.models.org import EmployeeAssignment

    if rule.department_id is not None:
        return [i for (i,) in db.query(Employee.id).filter(Employee.company_id == company_id, Employee.is_active.is_(True),
                                                           Employee.department_id == rule.department_id)]
    if rule.branch_id is not None:
        return [i for (i,) in db.query(EmployeeAssignment.employee_id).filter(
            EmployeeAssignment.company_id == company_id, EmployeeAssignment.branch_id == rule.branch_id,
            EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.effective_from <= on,
            or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= on)).distinct()]
    from app.models.teams import TeamMembership

    return [i for (i,) in db.query(TeamMembership.employee_id).filter(
        TeamMembership.company_id == company_id, TeamMembership.team_id == rule.team_id,
        TeamMembership.effective_from <= on,
        or_(TeamMembership.effective_to.is_(None), TeamMembership.effective_to >= on)).distinct()]


def _coverage(db: Session, pv: Preview, exclude_id: Optional[int], *, mode: str) -> None:
    if ((pv.rule or {}).get("request") or {}).get("coverage") == "EXEMPT":
        return
    cid = pv.employee.company_id
    rules = db.query(LeaveCoverageRule).filter(LeaveCoverageRule.company_id == cid,
                                               LeaveCoverageRule.status == "ACTIVE").all()
    for rule in rules:
        members = set(_group_members(db, cid, rule, pv.start))
        if pv.employee.id not in members or len(members) < 2:
            continue
        statuses = ("approved", "pending") if rule.count_pending else ("approved",)
        others = db.query(Leave).filter(Leave.company_id == cid, Leave.employee_id.in_(members - {pv.employee.id}),
                                        Leave.status.in_(statuses), Leave.start_date <= pv.end,
                                        Leave.end_date >= pv.start)
        if exclude_id:
            others = others.filter(Leave.id != exclude_id)
        others = others.all()
        for day in (d for d in pv.days if d.counted):
            away = 1 + sum(1 for o in others if o.start_date <= day.date <= o.end_date)
            limit_hit = (rule.max_absent_count is not None and away > rule.max_absent_count) or (
                rule.max_absent_pct is not None and Decimal(away * 100) / Decimal(len(members)) > Decimal(rule.max_absent_pct))
            if limit_hit:
                severity = "ERROR" if rule.mode == "BLOCK" and mode != "RECORD" else "WARNING"
                pv.add("COVERAGE_CONFLICT", severity, f"{rule.name}: too many people would be away on "
                                                      f"{day.date:%d %b %Y}.", rule_id=rule.id, date=day.date.isoformat(),
                       away=away, group_size=len(members))
                break


def _documents(db: Session, pv: Preview, today: date, attachment_ids: Sequence[int]) -> None:
    docs = (pv.rule or {}).get("documents") or {}
    if not docs.get("allowed", True):
        if attachment_ids:
            pv.add("DOCUMENT_NOT_ALLOWED", "ERROR", "Documents cannot be attached to this leave type.")
        return
    span = (pv.end - pv.start).days + 1
    needs = []
    for need in docs.get("required") or []:
        when = need["when"]
        if (when == "ALWAYS" or (when == "MIN_UNITS" and pv.charged >= Decimal(str(need["min_units"])))
                or (when == "MIN_CALENDAR_DAYS" and span >= Decimal(str(need["min_units"])))
                or (when == "BACKDATED" and pv.start < today)):
            needs.append(need["category"])
    if not needs:
        return
    due = docs.get("due") or "AT_SUBMIT"
    due_date = None
    if due == "WITHIN_DAYS_AFTER_RETURN":
        due_date = max(_walk(db, pv, pv.end, 1) + timedelta(days=int(docs.get("due_days") or 0)), today)
    elif due == "WITHIN_DAYS_AFTER_SUBMIT":
        due_date = today + timedelta(days=int(docs.get("due_days") or 0))
    for category in dict.fromkeys(needs):
        pv.documents_required.append({"category": category, "due": due,
                                      "due_date": due_date.isoformat() if due_date else None})
    if due == "AT_SUBMIT" and not attachment_ids:
        pv.add("DOCUMENT_REQUIRED", "ERROR", "Attach the required document to submit this request.",
               categories=list(dict.fromkeys(needs)))
    elif due_date is not None and not attachment_ids:
        pv.add("DOCUMENT_DUE", "INFO", f"Upload the document by {due_date:%d %b %Y}.", due_date=due_date.isoformat())


def _reason(pv: Preview, reason: Optional[str]) -> None:
    rule = ((pv.rule or {}).get("request") or {}).get("reason") or "OPTIONAL"
    if rule == "REQUIRED" and not (reason or "").strip():
        pv.add("REASON_REQUIRED", "ERROR", "Give a reason for this leave.", field="reason")


def _event_window(db: Session, pv: Preview, data, *, mode: str) -> None:
    """EVENT types (03-leave/06 §6.1 step 4): an OPEN event of this employee and type, and its window."""
    from app.models.leave_v2 import LeaveEvent
    from app.services import leave_events

    event_id = getattr(data, "event_id", None)
    if event_id is None:
        return  # EVENT_REQUIRED already recorded
    event = db.query(LeaveEvent).filter(LeaveEvent.company_id == pv.employee.company_id,
                                        LeaveEvent.id == event_id).first()
    if event is None or event.employee_id != pv.employee.id or event.leave_type_code != pv.leave_type.code \
            or event.status != "OPEN":
        pv.add("EVENT_REQUIRED", "ERROR", "Choose an open event of this leave type.", field="event_id")
        return
    pv.event = event
    rules = leave_events.event_rules(pv.rule)
    severity = "WARNING" if mode == "RECORD" else "ERROR"
    earliest = leave_events.earliest_start(event, rules)
    if pv.start < earliest:
        pv.add("EVENT_WINDOW_CLOSED", severity, f"This leave can start on {earliest:%d %b %Y} at the earliest.",
               reason="TOO_EARLY", earliest_start=earliest.isoformat(), field="start_date")
    if event.take_until is not None and pv.end > event.take_until:
        pv.add("EVENT_WINDOW_CLOSED", severity, f"This leave must end by {event.take_until:%d %b %Y}.",
               reason="TOO_LATE", take_until=event.take_until.isoformat(), field="end_date")


def _event_balance(db: Session, pv: Preview, exclude_id: Optional[int]) -> None:
    """The event's remaining units (entitled − used − pending) instead of the ledger; never negative, never split.
    WEEKS / CALENDAR_DAYS events take one continuous request (SPINE §6.1)."""
    from app.services import leave_events

    event = pv.event
    left = leave_events.remaining(db, event, exclude_leave_id=exclude_id)
    pv.balance_before, pv.balance_after = left, left - pv.charged
    rules = leave_events.event_rules(pv.rule)
    continuous = (rules.get("duration_unit") or "CALENDAR_DAYS") in leave_events.CONTINUOUS_UNITS
    whole = int(left) if left > 0 else 0
    pv.event_info = {"event_id": event.id, "event_type": event.event_type, "event_date": event.event_date.isoformat(),
                     "remaining_before": leave_ledger._num(left), "remaining_after": leave_ledger._num(left - pv.charged),
                     "take_until": event.take_until.isoformat() if event.take_until else None,
                     "suggested_end_date": (pv.start + timedelta(days=whole - 1)).isoformat()
                     if continuous and whole else None}
    if pv.charged > left:
        pv.add("INSUFFICIENT_BALANCE", "ERROR",
               f"Only {leave_ledger._num(max(left, ZERO))} day(s) of this event's leave remain.",
               available=leave_ledger._num(left), requested=leave_ledger._num(pv.charged), event_id=event.id)
        return
    if continuous and leave_events.has_active_requests(db, event, exclude_leave_id=exclude_id):
        pv.add("MAX_INSTANCES", "ERROR", "This event already has its leave request; change that request instead.",
               detail="ONE_REQUEST_PER_EVENT", event_id=event.id)


def _balance(db: Session, pv: Preview, exclude_id: Optional[int], *, mode: str) -> None:
    lt = pv.leave_type
    if lt.kind == "EVENT":
        if pv.event is not None and pv.charged:
            _event_balance(db, pv, exclude_id)
        return
    if lt.is_unlimited or not pv.charged:
        return
    cid = pv.employee.company_id
    rule = pv.rule or {}
    action = rule.get("insufficient_balance_action") or "BLOCK"
    allowance = Decimal(str(rule.get("negative_allowance") or "0"))
    view = leave_ledger.balance_view(db, cid, pv.employee, lt, pv.start, rule=rule,
                                     **({"exclude_leave_id": exclude_id} if leave_access.v2_enabled(db, cid) else {}))
    available = Decimal(view.available or 0)
    if not leave_access.v2_enabled(db, cid) and exclude_id is not None:
        excluded = db.get(Leave, exclude_id)
        if excluded is not None and excluded.status == "pending":
            available += Decimal(excluded.number_of_days or 0)
    if (rule.get("balance_check") or "CURRENT") == "PROJECTED" and leave_access.v2_enabled(db, cid):
        available += _projected_extra(db, pv)
    pv.balance_before = available
    pv.balance_after = available - pv.charged
    if pv.charged <= available:
        return
    short = pv.charged - available
    if action == "ALLOW_NEGATIVE":
        if pv.charged - available > allowance:
            pv.add("NEGATIVE_LIMIT", "ERROR", f"This would take your {lt.name} balance below the allowed "
                                              f"-{leave_ledger._num(allowance)}.", available=leave_ledger._num(available),
                   allowance=leave_ledger._num(allowance))
        else:
            pv.add("NEGATIVE_BALANCE", "WARNING", f"Your {lt.name} balance will go negative.",
                   available=leave_ledger._num(available))
        return
    if action == "SPLIT_TO_UNPAID":
        code = leave_access.setting(db, cid, "leave.unpaid_type_code", default="unpaid")
        unpaid = _type(db, cid, code)
        if unpaid is not None and unpaid.is_active:
            remaining, moved = short, []
            for day in sorted((d for d in pv.days if d.counted), key=lambda d: d.date, reverse=True):
                if remaining <= 0:
                    break
                moved.append(day.date)
                remaining -= day.charged
            pv.split_units = sum((d.charged for d in pv.days if d.date in moved), ZERO)
            pv.split_dates = sorted(moved)
            pv.unpaid_type = unpaid
            pv.balance_after = available - (pv.charged - pv.split_units)
            pv.add("SPLIT_TO_UNPAID", "WARNING", f"{leave_ledger._num(pv.split_units)} day(s) will be unpaid leave.",
                   units=leave_ledger._num(pv.split_units))
            return
    pv.add("INSUFFICIENT_BALANCE", "ERROR",
           f"Insufficient {lt.name} balance. Available: {leave_ledger._num(available)} day(s).",
           available=leave_ledger._num(available), requested=leave_ledger._num(pv.charged))


def _projected_extra(db: Session, pv: Preview) -> Decimal:
    """Accruals still to be credited between today and the start date (PROJECTED balance check)."""
    from app.services.leave_cycles import earned_to_date

    rule = pv.rule or {}
    ent = rule.get("entitlement") or {}
    if ent.get("method") != "PERIODIC":
        return ZERO
    cycle = leave_plans.cycle_of(db, pv.employee.company_id, pv.employee, rule, pv.start)
    today = local_today(db, pv.employee)
    kw = dict(joined=pv.employee.hire_date, left=pv.employee.termination_date,
              service_start=pv.employee.service_start_date or pv.employee.hire_date,
              rounding=(rule.get("rounding") or {}).get("mode", "NONE_2DP"))
    return max(earned_to_date(ent, cycle, pv.start, **kw) - earned_to_date(ent, cycle, max(today, cycle.start), **kw), ZERO)


def validate(db: Session, principal: Principal, data, *, mode: str = "APPLY", existing: Optional[Leave] = None,
             employee: Optional[Employee] = None, source: Optional[str] = None) -> Preview:
    """Every stage of 03-leave/03 §4.3 for a new request (or an edit of ``existing``); never writes."""
    if employee is None:
        employee, source = resolve_subject(db, principal, getattr(data, "employee_id", None), mode=mode)
    cid = principal.company_id
    lt = _type(db, cid, data.leave_type)
    pv = Preview(employee, lt, None, data.start_date, data.end_date, getattr(data, "start_portion", "FULL") or "FULL",
                 getattr(data, "end_portion", "FULL") or "FULL", source=source or "SELF")
    if lt is None or not lt.is_active:
        pv.add("INVALID_LEAVE_TYPE", "ERROR", "Unknown leave type.", field="leave_type")
        return pv
    resolution = leave_plans.resolve(db, cid, employee.id, data.start_date)
    pv.plan = resolution.json()
    pv.plan_version_id = resolution.version_id
    pv.rule = resolution.rule(lt.code)
    if pv.rule is None:
        if resolution.status not in ("RESOLVED", "LEGACY"):
            pv.add("PLAN_NOT_ASSIGNED", "ERROR", "No leave plan applies to you yet; contact HR.")
        else:
            pv.add("NOT_ELIGIBLE", "ERROR", "This leave type is not available to you.", check="IN_PLAN")
        return pv
    if lt.kind == "EVENT" and getattr(data, "event_id", None) is None:
        pv.add("EVENT_REQUIRED", "ERROR", "Choose the event (birth, adoption, bereavement…) this leave is for.",
               field="event_id")
    elif lt.kind == "EVENT":
        _event_window(db, pv, data, mode=mode)
    rules = pv.rule.get("request") or {}
    if pv.source == "SELF" and not rules.get("self_service", True):
        pv.add("INVALID_LEAVE_TYPE", "ERROR", "HR records this leave type for you.", field="leave_type")
    if pv.source == "ON_BEHALF" and not rules.get("on_behalf", True):
        pv.add("INVALID_LEAVE_TYPE", "ERROR", "This leave type cannot be applied for on someone's behalf.")
    today = local_today(db, employee)
    _portions(db, pv)
    _dates(db, pv, today, mode=mode)
    if pv.errors:
        return pv
    exclude_id = existing.id if existing is not None else None
    pv.charged, pv.days = leave_charging.charge(db, cid, employee.id, pv.rule, pv.start, pv.end, pv.start_portion,
                                                pv.end_portion, exclude_leave_id=exclude_id)
    if not all(d.employed for d in pv.days):
        pv.add("OUTSIDE_EMPLOYMENT", "ERROR", "Leave can only be taken while employed.")
        return pv
    if pv.charged <= 0:
        pv.add("NO_WORKING_DAYS", "ERROR", "The selected dates are weekly offs or holidays; no leave is needed.")
        return pv
    _eligibility(db, pv)
    _overlap(db, pv, exclude_id)
    _size_and_caps(db, pv, exclude_id, mode=mode)
    _notice(db, pv, today, is_emergency=bool(getattr(data, "is_emergency", False)), reason=getattr(data, "reason", None),
            mode=mode)
    _clubbing(db, pv, exclude_id, mode=mode)
    _blackouts(db, pv, mode=mode)
    _coverage(db, pv, exclude_id, mode=mode)
    _documents(db, pv, today, getattr(data, "attachment_ids", None) or [])
    _reason(pv, getattr(data, "reason", None))
    _balance(db, pv, exclude_id, mode=mode)
    approval = pv.rule.get("approval") or {}
    pv.auto_approve = bool(approval.get("auto_approve_eligible")) and not pv.warnings and not (
        set(pv.flags) & {"ELIGIBILITY_VERIFY", "EMERGENCY"}) and not pv.split_units and not pv.documents_required \
        and pv.start >= today
    return pv


# --------------------------------------------------------------------------- routing

def _condition_applies(condition: Optional[dict], leave: Leave, units: Decimal, legal_entity_id: Optional[int]) -> bool:
    if not condition:
        return True
    codes = condition.get("leave_type_codes")
    if codes and leave.leave_type not in codes:
        return False
    if condition.get("min_units") is not None and units < Decimal(str(condition["min_units"])):
        return False
    if condition.get("max_units") is not None and units > Decimal(str(condition["max_units"])):
        return False
    entities = condition.get("legal_entity_ids")
    if entities and legal_entity_id not in entities:
        return False
    if condition.get("sensitivity") and (leave.sensitivity or "STANDARD") != condition["sensitivity"]:
        return False
    return True


def route(db: Session, leave: Leave, employee: Employee) -> None:
    """Instantiate the company's leave chain for the request (steps whose conditions match) and advance it."""
    from app.services import leave_service

    units = Decimal(leave.number_of_days or 0)
    where = leave_plans.placement(db, leave.company_id, employee.id, leave.start_date)
    chain = (db.query(ApprovalChainStep).filter(ApprovalChainStep.company_id == leave.company_id,
                                                ApprovalChainStep.module == "leave",
                                                ApprovalChainStep.route_id.is_(None))
             .order_by(ApprovalChainStep.step_order).all())
    order = 0
    for step in chain:
        if step.min_days is not None and units < step.min_days:
            continue
        if not _condition_applies(step.condition, leave, units, where.legal_entity_id if where else None):
            continue
        order += 1
        leave.approvals.append(LeaveApproval(company_id=leave.company_id, step_order=order,
                                             approver_type=step.approver_type, role_id=step.role_id, status="waiting"))
    db.flush()
    leave_service.advance(db, leave, employee)


# --------------------------------------------------------------------------- commands

def _message(db: Session, leave: Leave, body: str, *, kind: str = "SYSTEM_EVENT", author: Optional[int] = None,
             visibility: str = "PARTICIPANTS", reason_code: Optional[str] = None, details: Optional[dict] = None):
    if leave_access.setting(db, leave.company_id, "leave.discussion.enabled", default=True) is False and kind == "COMMENT":
        return None
    row = LeaveMessage(company_id=leave.company_id, leave_id=leave.id, author_user_id=author, kind=kind,
                       visibility=visibility, body=body[:4000], reason_code=reason_code, details=details)
    db.add(row)
    return row


def _attach(db: Session, principal: Principal, leave: Leave, attachment_ids: Sequence[int]) -> None:
    for attachment_id in attachment_ids or []:
        row = db.query(LeaveAttachment).filter(LeaveAttachment.company_id == principal.company_id,
                                               LeaveAttachment.id == attachment_id,
                                               LeaveAttachment.deleted_at.is_(None)).first()
        if row is None or row.employee_id != leave.employee_id or (row.leave_id not in (None, leave.id)):
            raise not_found("Attachment")
        row.leave_id = leave.id


def _document_requests(db: Session, pv: Preview, leave: Leave, user_id: Optional[int], has_files: bool) -> None:
    for need in pv.documents_required:
        if need["due_date"] is None or has_files:
            continue
        db.add(LeaveDocumentRequest(company_id=leave.company_id, leave_id=leave.id, category=need["category"],
                                    due_date=date.fromisoformat(need["due_date"]), status="OPEN",
                                    requested_by_user_id=user_id, note="Required by the leave plan"))


def _apply_fields(leave: Leave, pv: Preview, data) -> None:
    leave.leave_type = pv.leave_type.code
    leave.start_date, leave.end_date = pv.start, pv.end
    leave.start_portion, leave.end_portion = pv.start_portion, pv.end_portion
    leave.number_of_days = pv.charged - pv.split_units
    leave.reason = (getattr(data, "reason", None) or "").strip() or None
    leave.is_emergency = bool(getattr(data, "is_emergency", False))
    leave.flags = list(dict.fromkeys(pv.flags))
    leave.issues = [i.json() for i in pv.issues if i.severity in ("WARNING", "INFO")]
    leave.plan_version_id = pv.plan_version_id
    # Only an EVENT request references an event (a stray event_id on another type is ignored, never stored).
    leave.event_id = getattr(data, "event_id", None) if pv.leave_type.kind == "EVENT" else None
    leave.contact_note = getattr(data, "contact_note", None)
    leave.handover_employee_id = getattr(data, "handover_employee_id", None)


def _sibling(db: Session, leave: Leave, pv: Preview, actor_user_id: Optional[int]) -> Optional[Leave]:
    """The linked unpaid request holding the excess dates (SPLIT_TO_UNPAID)."""
    if not pv.split_units or pv.unpaid_type is None:
        return None
    dates = pv.split_dates
    # The parent keeps the earlier dates; the sibling covers the moved (latest) dates.
    leave.end_date = min(dates) - timedelta(days=1) if min(dates) > leave.start_date else leave.end_date
    if leave.end_date != pv.end:
        leave.end_portion = "FULL"
    sibling = Leave(company_id=leave.company_id, employee_id=leave.employee_id, leave_type=pv.unpaid_type.code,
                    start_date=min(dates), end_date=pv.end, start_portion="FULL", end_portion=pv.end_portion,
                    number_of_days=pv.split_units, reason=leave.reason, status=leave.status, source=leave.source,
                    requested_by_user_id=actor_user_id, parent_leave_id=leave.id, submitted_at=leave.submitted_at,
                    sensitivity=pv.unpaid_type.sensitivity, flags=["SPLIT_TO_UNPAID"])
    db.add(sibling)
    db.flush()
    return sibling


def submit(db: Session, principal: Principal, data, *, mode: str = "APPLY") -> Leave:
    """Create a request: DRAFT (``data.draft``), PENDING, or APPROVED when HR records it."""
    from app.services import leave_service, shift_requests

    employee, source = resolve_subject(db, principal, getattr(data, "employee_id", None), mode=mode)
    concurrency.lock_employees(db, [employee.id])
    draft = bool(getattr(data, "draft", False)) and mode != "RECORD"
    pv = validate(db, principal, data, mode=mode, employee=employee, source=source)
    if draft:
        hard = [i for i in pv.errors if i.code in ("INVALID_LEAVE_TYPE", "INVALID_RANGE") + PORTION_ERRORS]
        if hard:
            pv.raise_first_error()
    else:
        pv.raise_first_error()
    now = utcnow()
    leave = Leave(company_id=principal.company_id, employee_id=employee.id, source=source,
                  requested_by_user_id=principal.user_id, sensitivity=pv.leave_type.sensitivity, version=1,
                  status="draft" if draft else ("approved" if mode == "RECORD" else "pending"),
                  submitted_at=None if draft else now)
    _apply_fields(leave, pv, data)
    db.add(leave)
    db.flush()
    _attach(db, principal, leave, getattr(data, "attachment_ids", None) or [])
    if draft:
        _message(db, leave, "Saved as a draft.")
        AuditLogService.log(db, principal, "Leave", "DRAFT_SAVED", f"Saved a leave draft #{leave.id}", "leave", leave.id)
        return leave
    sibling = _sibling(db, leave, pv, principal.user_id)
    _document_requests(db, pv, leave, principal.user_id, bool(getattr(data, "attachment_ids", None)))
    shift_requests.cancel_for_leave(db, principal.company_id, employee.id, pv.start, pv.end)
    who = {"SELF": "Submitted", "ON_BEHALF": f"Submitted on behalf by {principal.full_name}",
           "HR_RECORDED": f"Recorded by {principal.full_name}"}.get(source, "Submitted")
    _message(db, leave, f"{who}.", author=None)
    if mode == "RECORD":
        leave.decided_at, leave.decided_by_user_id = now, principal.user_id
        leave_service.finalize_v2(db, leave, employee, "approved", principal.user_id)
        if sibling is not None:
            sibling.decided_at, sibling.decided_by_user_id = now, principal.user_id
            leave_service.finalize_v2(db, sibling, employee, "approved", principal.user_id, notify=False)
    elif pv.auto_approve:
        leave.decided_at = now
        leave_service.finalize_v2(db, leave, employee, "approved", None)
        _message(db, leave, "Approved automatically under the leave plan.")
    else:
        route(db, leave, employee)
    AuditLogService.log(db, principal, "Leave", {"SELF": "APPLY", "ON_BEHALF": "APPLY_ON_BEHALF",
                                                 "HR_RECORDED": "RECORD"}.get(source, "APPLY"),
                        f"{source.replace('_', ' ').title()} leave #{leave.id}: {leave_ledger._num(pv.charged)} unit(s) "
                        f"of {pv.leave_type.code} for {employee.full_name}", "leave", leave.id,
                        details={"start_portion": pv.start_portion, "end_portion": pv.end_portion,
                                 "units": str(pv.charged), "flags": leave.flags, "source": source,
                                 "split_units": str(pv.split_units)})
    return leave


def load(db: Session, principal: Principal, leave_id: int, *, lock: bool = False) -> Leave:
    query = db.query(Leave).options(selectinload(Leave.approvals)).filter(Leave.company_id == principal.company_id,
                                                                          Leave.id == leave_id)
    if lock:
        query = query.with_for_update(of=Leave)
    leave = query.first()
    if leave is None:
        raise not_found("Leave request")
    return leave


def _can_edit_for(db: Session, principal: Principal, leave: Leave) -> bool:
    if leave.employee_id == principal.employee_id:
        return principal.has("leave:apply")
    return principal.has("leave:apply_on_behalf") and authz.can_access_employee(
        db, principal, "leave:apply_on_behalf", leave.employee_id)


def update_draft(db: Session, principal: Principal, leave_id: int, data) -> Leave:
    """Edit a DRAFT or RETURNED request (03-leave/03 §6); resubmission is a separate call."""
    leave = load(db, principal, leave_id, lock=True)
    if not _can_edit_for(db, principal, leave):
        raise not_found("Leave request")
    if leave.status not in ("draft", "returned"):
        raise AppError(409, "Only drafts and returned requests can be edited.", "INVALID_TRANSITION")
    employee = db.get(Employee, leave.employee_id)
    pv = validate(db, principal, data, existing=leave, employee=employee, source=leave.source)
    hard = [i for i in pv.errors if i.code in ("INVALID_LEAVE_TYPE", "INVALID_RANGE") + PORTION_ERRORS]
    if hard:
        pv.raise_first_error()
    _apply_fields(leave, pv, data)
    _drop_stale_siblings(db, leave)  # the edited dates are the whole request; a new split is made on submit
    leave.row_version = (leave.row_version or 1) + 1
    _attach(db, principal, leave, getattr(data, "attachment_ids", None) or [])
    db.flush()
    return leave


def _stale_siblings(db: Session, leave: Leave) -> List[Leave]:
    return db.query(Leave).filter(Leave.company_id == leave.company_id, Leave.parent_leave_id == leave.id,
                                  Leave.status.in_(("pending", "returned", "draft"))).all()


def _drop_stale_siblings(db: Session, leave: Leave) -> None:
    """The unpaid sibling of an earlier submission is replaced by the split of the next one (LH-REQ-10): two live
    siblings would hold the same dates and collide when the request is approved."""
    for old in _stale_siblings(db, leave):
        old.status, old.withdrawn_at, old.current_step = "withdrawn", utcnow(), None


def submit_existing(db: Session, principal: Principal, leave_id: int) -> Leave:
    """DRAFT → PENDING, or RETURNED → PENDING (resubmit; the route restarts at step 1 by default)."""
    from app.services import leave_service

    leave = load(db, principal, leave_id, lock=True)
    if not _can_edit_for(db, principal, leave):
        raise not_found("Leave request")
    if leave.status not in ("draft", "returned"):
        raise AppError(409, "Only drafts and returned requests can be submitted.", "INVALID_TRANSITION")
    employee = db.get(Employee, leave.employee_id)
    concurrency.lock_employees(db, [employee.id])

    data = StoredRequest(leave)
    data.attachment_ids = [a for (a,) in db.query(LeaveAttachment.id).filter(
        LeaveAttachment.company_id == leave.company_id, LeaveAttachment.leave_id == leave.id,
        LeaveAttachment.deleted_at.is_(None))]
    stale = _stale_siblings(db, leave)
    if stale:  # resubmitted unchanged: the request is still the parent's dates plus its unpaid sibling's
        last = max(stale, key=lambda s: s.end_date)
        if last.end_date > data.end_date:
            data.end_date, data.end_portion = last.end_date, last.end_portion
    pv = validate(db, principal, data, existing=leave, employee=employee, source=leave.source)
    pv.raise_first_error()
    resubmit = leave.status == "returned"
    _apply_fields(leave, pv, data)
    leave.status = "pending"
    leave.submitted_at = leave.submitted_at or utcnow()
    leave.returned_at = None
    leave.row_version = (leave.row_version or 1) + 1
    leave.sensitivity = pv.leave_type.sensitivity
    for step in list(leave.approvals):
        if step.status in ("waiting", "pending", "skipped", "rejected"):
            leave.approvals.remove(step)
    restart = leave_access.setting(db, leave.company_id, "leave.approval.resubmit_restarts_route", default=True)
    if resubmit and restart:
        leave.approvals.clear()
    _drop_stale_siblings(db, leave)
    db.flush()
    _sibling(db, leave, pv, principal.user_id)
    _document_requests(db, pv, leave, principal.user_id, bool(data.attachment_ids))
    _message(db, leave, "Resubmitted." if resubmit else "Submitted.")
    if pv.auto_approve and not resubmit:
        leave_service.finalize_v2(db, leave, employee, "approved", None)
    else:
        route(db, leave, employee)
    AuditLogService.log(db, principal, "Leave", "RESUBMIT" if resubmit else "SUBMIT",
                        f"{'Resubmitted' if resubmit else 'Submitted'} leave #{leave.id}", "leave", leave.id)
    return leave


def withdraw(db: Session, principal: Principal, leave_id: int, reason: Optional[str] = None) -> Leave:
    """PENDING / RETURNED / DRAFT → WITHDRAWN (drafts are deleted instead, 03 §6)."""
    leave = load(db, principal, leave_id, lock=True)
    if not _can_edit_for(db, principal, leave):
        raise not_found("Leave request")
    if leave.status == "draft":
        db.query(LeaveMessage).filter(LeaveMessage.company_id == leave.company_id,
                                      LeaveMessage.leave_id == leave.id).delete(synchronize_session=False)
        db.query(LeaveAttachment).filter(LeaveAttachment.company_id == leave.company_id,
                                         LeaveAttachment.leave_id == leave.id).update({"leave_id": None})
        db.delete(leave)
        db.flush()
        return leave
    if leave.status not in ("pending", "returned"):
        raise AppError(409, "Only pending or returned requests can be withdrawn; approved leave is cancelled with a "
                            "change request.", "INVALID_TRANSITION")
    _close(db, leave, "withdrawn")
    leave.cancellation_reason = (reason or "").strip() or leave.cancellation_reason
    _message(db, leave, "Withdrawn by the requester." if leave.employee_id == principal.employee_id else
             f"Withdrawn by {principal.full_name}.")
    AuditLogService.log(db, principal, "Leave", "WITHDRAW", f"Withdrew leave #{leave.id}", "leave", leave.id)
    return leave


def _close(db: Session, leave: Leave, status: str) -> None:
    now = utcnow()
    for item in [leave] + db.query(Leave).filter(Leave.company_id == leave.company_id,
                                                 Leave.parent_leave_id == leave.id).all():
        item.status = status
        item.current_step = None
        if status == "withdrawn":
            item.withdrawn_at = now
        for step in item.approvals:
            if step.status in ("waiting", "pending"):
                step.status = "cancelled"
    db.flush()


def return_for_changes(db: Session, principal: Principal, leave_id: int, reason: str,
                       reason_code: Optional[str] = None, suggestion: Optional[dict] = None) -> Leave:
    """An approver sends a PENDING request back (RETURNED, reason required, 03-leave/04 RETURN)."""
    from app.services import leave_service

    if not leave_access.setting(db, principal.company_id, "leave.approval.return_enabled", default=True):
        raise AppError(409, "Sending requests back is turned off.", "RETURN_DISABLED")
    if not (reason or "").strip():
        raise bad_request("Tell the employee what to change.", "REASON_REQUIRED")
    leave = load(db, principal, leave_id, lock=True)
    requester = db.get(Employee, leave.employee_id)
    acting = leave_service.can_act(db, principal, leave, requester)
    if acting is None or leave.status != "pending":
        if not authz.can_access_employee(db, principal, "leave:read", leave.employee_id):
            raise not_found("Leave request")
        raise forbidden("This request is not awaiting your approval.")
    step = leave_service._current_step(leave)
    if step is not None:
        step.status = "rejected"
        step.acted_by_user_id, step.acted_as, step.acted_at, step.remarks = (
            principal.user_id, acting, utcnow(), reason.strip())
    for later in leave.approvals:
        if later.status in ("waiting", "pending"):
            later.status = "cancelled"
    leave.status = "returned"
    leave.current_step = None
    leave.returned_at = utcnow()
    for sibling in db.query(Leave).filter(Leave.company_id == leave.company_id, Leave.parent_leave_id == leave.id,
                                          Leave.status == "pending").all():
        sibling.status, sibling.returned_at, sibling.current_step = "returned", leave.returned_at, None
    _message(db, leave, reason.strip(), kind="SUGGESTED_CHANGE" if suggestion else "DECISION_NOTE",
             author=principal.user_id, reason_code=reason_code, details={"decision": "RETURN", "suggestion": suggestion})
    from app.services.notification_service import NotificationService

    if requester.user_id:
        NotificationService.create(db, requester.user_id, "Leave request sent back",
                                   "Your leave request was returned for changes.", "leave",
                                   f"/leaves/requests?modal=leave&id={leave.id}")
    AuditLogService.log(db, principal, "Leave", "RETURN", f"Returned leave #{leave.id} for changes", "leave", leave.id)
    return leave


# --------------------------------------------------------------------------- reads

def allowed_actions(db: Session, principal: Principal, leave: Leave) -> List[str]:
    from app.services import leave_service

    own = leave.employee_id == principal.employee_id
    editor = _can_edit_for(db, principal, leave)
    actions = []
    if editor and leave.status in ("draft", "returned"):
        actions += ["EDIT", "SUBMIT"]
    if editor and leave.status in ("pending", "returned", "draft"):
        actions.append("WITHDRAW")
    if leave.status == "pending":
        requester = db.get(Employee, leave.employee_id)
        acting = leave_service.can_act(db, principal, leave, requester)
        if acting:
            actions += ["APPROVE", "REJECT"]
            if leave_access.setting(db, leave.company_id, "leave.approval.return_enabled", default=True):
                actions.append("RETURN")
    if leave.status == "approved" and (editor or (not own and principal.has("leave:change_any"))):
        actions.append("CHANGE")
    return actions


def masked_type(db: Session, principal: Principal, leave: Leave, leave_type: Optional[LeaveType]) -> bool:
    """SENSITIVE leave (stricter of the snapshot and the type) shows as "Protected leave" to anyone but the
    employee, the approvers acting on it and holders of ``leave:view_medical`` (V3-006/V3-007)."""
    sensitive = (leave.sensitivity == "SENSITIVE") or (leave_type is not None and leave_type.sensitivity == "SENSITIVE")
    if not sensitive or leave.employee_id == principal.employee_id:
        return False
    if principal.has("leave:view_medical") and authz.can_access_employee(db, principal, "leave:view_medical",
                                                                        leave.employee_id):
        return False
    from app.services import leave_service

    if leave.status == "pending" and leave_service.can_act(db, principal, leave, db.get(Employee, leave.employee_id)):
        return False
    return True


def _can_post_internal(db: Session, principal: Principal, leave: Leave) -> bool:
    from app.services import leave_docs

    return leave_docs._staff(db, principal, leave)


def detail(db: Session, principal: Principal, leave_id: int) -> dict:
    from app.services import leave_service

    leave = load(db, principal, leave_id)
    requester = db.get(Employee, leave.employee_id)
    own = leave.employee_id == principal.employee_id
    if not own and not authz.can_access_employee(db, principal, "leave:read", leave.employee_id):
        acting = leave_service.can_act(db, principal, leave, requester)
        if acting is None:
            raise not_found("Leave request")
    lt = _type(db, principal.company_id, leave.leave_type)
    data = leave_service.serialize(db, leave, requester, leave_service.can_act(db, principal, leave, requester))
    masked = masked_type(db, principal, leave, lt)
    data.update({
        "status_v2": V2_STATUS.get(leave.status, leave.status.upper()), "version": leave.version,
        "source": leave.source, "flags": leave.flags or [], "issues": leave.issues or [],
        "is_emergency": leave.is_emergency, "parent_leave_id": leave.parent_leave_id, "event_id": leave.event_id,
        "submitted_at": leave.submitted_at.isoformat() if leave.submitted_at else None,
        "leave_type_name": "Protected leave" if masked else (lt.name if lt else leave.leave_type),
        "color_token": "leave-neutral" if masked else (lt.color_token if lt else None),
        "masked": masked, "row_version": leave.row_version, "contact_note": leave.contact_note,
        "can_post_internal": _can_post_internal(db, principal, leave),
        "allowed_actions": allowed_actions(db, principal, leave),
        "siblings": [{"id": s.id, "leave_type": s.leave_type, "number_of_days": leave_ledger._num(Decimal(s.number_of_days)),
                      "status": s.status} for s in db.query(Leave).filter(Leave.company_id == leave.company_id,
                                                                          Leave.parent_leave_id == leave.id)],
        "days": [{"date": d.date.isoformat(), "portion": d.portion, "units": leave_ledger._num(Decimal(d.units)),
                  "counted": bool(d.counts_against_balance), "charge_reason": d.charge_reason}
                 for d in db.query(LeaveDay).filter(LeaveDay.company_id == leave.company_id, LeaveDay.leave_id == leave.id,
                                                    LeaveDay.voided_at.is_(None)).order_by(LeaveDay.date)],
    })
    if masked:
        data["leave_type"] = "protected"
        data["reason"] = None
    elif not own and lt is not None and lt.sensitivity == "SENSITIVE" and not principal.has("leave:view_medical"):
        data["reason"] = None  # sensitive reasons only for the employee, approvers and medical viewers
    return data


def decide_request(db: Session, principal: Principal, leave_id: int, approve: bool, remarks: Optional[str],
                   reason_code: Optional[str] = None) -> Leave:
    """Approve / reject the current step (or a pending cancellation) with the decision note on the discussion.
    Rejecting needs a reason while ``leave.approval.reject_reason_required`` is on. Shared by the leave v2 API and
    the assistant's confirmed approval actions (app/agent/modules/leave_actions.py)."""
    from app.services import leave_service

    remarks = (remarks or "").strip() or None
    if not approve and not remarks and leave_access.setting(db, principal.company_id,
                                                            "leave.approval.reject_reason_required", default=True):
        raise bad_request("Give the employee a reason for rejecting.", "REASON_REQUIRED")
    leave = leave_service.decide(db, principal, leave_id, approve, remarks)
    _message(db, leave, remarks or ("Approved." if approve else "Rejected."), kind="DECISION_NOTE",
             author=principal.user_id, reason_code=reason_code, details={"decision": "APPROVE" if approve else "REJECT"})
    db.flush()
    return leave

"""Time inputs of the day ledger (3.4 §2-§8, §16; 3.1 §5.2-§5.4; audit 03 §6-§8; FOLLOW-UPS F-02..F-04).

* Day facts: per date the primary assignment (employment window, branch, work week, shift, FTE), the
  weekly off of the work pattern (assignment, then branch, then legal entity, then the country's
  weekend) and the holiday of the branch's calendar lineage on that date (EC-HOL-04, EC-ATT-21).
* Leave days: generated at approval for every date of a leave (a half day on the first or last date);
  weekly offs and holidays are charged only when the leave type counts non-working days (EC-LV-06,
  EC-LV-10). Balances move through append-only leave_balance_transactions with idempotency keys.
* Manual LOP inputs (approvals module ``lop``), unpaid suspensions, on-duty requests (module
  ``attendance``) and optional holiday choices within the yearly quota (EC-HOL-05).
* source_change_log: every writer appends the record's new version hash (3.4 §16).
* ledger_inputs(): every date of a range as ``ledger.DayInput`` for the snapshot builder.

Services take (db, principal, ...) and never commit.
"""

from __future__ import annotations

import calendar as _calendar
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import func, or_
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core import authz, clock
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.core.security import utcnow
from app.models.attendance import Attendance
from app.models.employee import Employee, Shift
from app.models.holiday import Holiday
from app.models.leave import Leave, LeaveType
from app.models.org import EmployeeAssignment
from app.models.time_inputs import (
    EmployeeOptionalHoliday,
    EmployeeSuspension,
    LeaveBalanceTransaction,
    LeaveDay,
    LopInput,
    OnDutyRequest,
    SourceChangeLog,
)
from app.payroll import audit as payroll_audit
from app.payroll import ledger, policy_types, workweek
from app.payroll.services import access, approvals
from app.services import attendance_policy, attendance_service, holiday_service
from app.services.audit_log_service import AuditLogService

ONE, HALF, ZERO = Decimal(1), Decimal("0.5"), Decimal(0)
PORTIONS = ("FULL", "FIRST_HALF", "SECOND_HALF")
MAX_RANGE_DAYS = 400
INPUTS_READ, INPUTS_EDIT = "payroll:inputs:read", "payroll:inputs:edit"
LOP_MODULE, LOP_SUBJECT = "lop", "lop_input"
ON_DUTY_MODULE, ON_DUTY_SUBJECT = "attendance", "on_duty"
HOLIDAY_RANK = {"PUBLIC": 0, "COMPANY": 1, "OPTIONAL": 2}
STATUS_MAP = {"present": "PRESENT", "late": "LATE", "half-day": "HALF_DAY", "absent": "ABSENT"}
AUDIT_MODULE = payroll_audit.PAYROLL_INPUTS


# ---------------------------------------------------------------------------
# Version hashes and the source change log
# ---------------------------------------------------------------------------
def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        return (value.astimezone(timezone.utc) if value.tzinfo else value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError(f"cannot hash {type(value).__name__}")


def content_hash(value: Any) -> str:
    """SHA-256 of canonical JSON (sorted keys, no whitespace, decimals and dates as strings)."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def log_change(
    db: Session, company_id: int, *, record_type: str, record_id: int, change: str, content: Any,
    employee_id: Optional[int] = None, date_from: Optional[date] = None, date_to: Optional[date] = None,
    actor_user_id: Optional[int] = None, details: Optional[Mapping[str, Any]] = None,
) -> SourceChangeLog:
    """Append the record's new version to source_change_log (3.4 §16)."""
    row = SourceChangeLog(
        company_id=company_id, record_type=record_type, record_id=record_id, employee_id=employee_id,
        date_from=date_from, date_to=date_to if date_to is not None else date_from, change=change,
        version_hash=content_hash(content), changed_at=utcnow(), actor_user_id=actor_user_id,
        details=json.loads(json.dumps(dict(details), default=_json_default)) if details else None,
    )
    db.add(row)
    db.flush()
    return row


def changes_since(db: Session, company_id: int, since: datetime, *, employee_ids: Optional[Iterable[int]] = None,
                  start: Optional[date] = None, end: Optional[date] = None) -> List[SourceChangeLog]:
    """Change-log rows after ``since`` that can touch ``start..end`` (employee-less rows reach everyone)."""
    query = db.query(SourceChangeLog).filter(SourceChangeLog.company_id == company_id, SourceChangeLog.changed_at > since)
    if employee_ids is not None:
        query = query.filter(or_(SourceChangeLog.employee_id.is_(None), SourceChangeLog.employee_id.in_(list(employee_ids))))
    if end is not None:
        query = query.filter(or_(SourceChangeLog.date_from.is_(None), SourceChangeLog.date_from <= end))
    if start is not None:
        query = query.filter(or_(SourceChangeLog.date_to.is_(None), SourceChangeLog.date_to >= start))
    return query.order_by(SourceChangeLog.changed_at, SourceChangeLog.id).all()


# ---------------------------------------------------------------------------
# Day facts: employment window, branch, work week, holiday per date
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HolidayFact:
    id: int
    name: str
    type: str  # PUBLIC | COMPANY | OPTIONAL_AVAILED | OPTIONAL_NOT_AVAILED
    content_hash: str

    @property
    def paid(self) -> bool:
        return self.type != "OPTIONAL_NOT_AVAILED"


@dataclass(frozen=True)
class DayFacts:
    date: date
    employed: bool
    assignment: Optional[EmployeeAssignment]
    branch_id: Optional[int]
    work_week_id: Optional[int]
    weekly_off: bool
    holiday: Optional[HolidayFact]
    # LH-M1 (04-holiday/03 §5.4, SPINE §7): the version of the pattern valid on the date, where the pattern came
    # from, the weekly-off portion (half-day offs are working days for ``weekly_off``) and whether it is a rest day.
    pattern_version: Optional[int] = None
    work_source: str = "PATTERN"
    weekly_off_portion: Optional[str] = None
    rest_day: bool = False

    @property
    def working(self) -> bool:
        """A scheduled working day: neither a weekly off nor a paid holiday (3.4 §3)."""
        return not self.weekly_off and not (self.holiday is not None and self.holiday.paid)


def _dates(start: date, end: date) -> List[date]:
    if end < start:
        raise bad_request("The end date is before the start date.", "INVALID_RANGE")
    if (end - start).days >= MAX_RANGE_DAYS:
        raise bad_request(f"A range may cover at most {MAX_RANGE_DAYS} days.", "RANGE_TOO_LONG")
    return [start + timedelta(days=n) for n in range((end - start).days + 1)]


def _employee(db: Session, company_id: int, employee_id: int) -> Employee:
    employee = db.query(Employee).filter(Employee.company_id == company_id, Employee.id == employee_id).first()
    if employee is None:
        raise not_found("Employee")
    return employee


def primary_assignments(db: Session, company_id: int, employee_id: int) -> List[EmployeeAssignment]:
    return (
        db.query(EmployeeAssignment)
        .filter(EmployeeAssignment.company_id == company_id, EmployeeAssignment.employee_id == employee_id,
                EmployeeAssignment.is_primary.is_(True))
        .order_by(EmployeeAssignment.effective_from, EmployeeAssignment.id)
        .all()
    )


def _covering(assignments: Sequence[EmployeeAssignment], day: date) -> Optional[EmployeeAssignment]:
    for row in assignments:
        if row.effective_from <= day and (row.effective_to is None or day <= row.effective_to):
            return row
    return None


def _nearest(assignments: Sequence[EmployeeAssignment], day: date) -> Optional[EmployeeAssignment]:
    """The placement that decides weekly offs and holidays of a date outside employment (F-04)."""
    before = [a for a in assignments if a.effective_from <= day]
    return before[-1] if before else (assignments[0] if assignments else None)


def employed_on(employee: Employee, assignment: Optional[EmployeeAssignment], day: date) -> bool:
    if assignment is None:
        return False
    if employee.hire_date is not None and day < employee.hire_date:
        return False
    return employee.termination_date is None or day <= employee.termination_date


def _resolver(db: Session, company_id: int):
    """The effective-dated work-week resolver (LH-M1: roster > placement > criteria > branch link > entity >
    country weekend > MON_FRI, rules of the version valid on each date; app.services.holiday_assignments)."""
    from app.services.holiday_assignments import WorkWeekResolver

    return WorkWeekResolver(db, company_id)


def _holiday_fact(holiday: Holiday, chosen: set) -> HolidayFact:
    kind = holiday.holiday_type or "COMPANY"
    if kind == "OPTIONAL":
        kind = "OPTIONAL_AVAILED" if holiday.id in chosen else "OPTIONAL_NOT_AVAILED"
    return HolidayFact(holiday.id, holiday.name, kind, content_hash(holiday_service.content(holiday)))


def _chosen_optional(db: Session, company_id: int, employee_id: int, holiday_ids: Iterable[int]) -> set:
    ids = list(holiday_ids)
    if not ids:
        return set()
    return {h for (h,) in db.query(EmployeeOptionalHoliday.holiday_id).filter(
        EmployeeOptionalHoliday.company_id == company_id, EmployeeOptionalHoliday.employee_id == employee_id,
        EmployeeOptionalHoliday.holiday_id.in_(ids), EmployeeOptionalHoliday.status == "CHOSEN")}


def day_facts(db: Session, company_id: int, employee_id: int, start: date, end: date) -> List[DayFacts]:
    """Employment, branch, work week, weekly off and holiday of every date of ``start..end``."""
    days = _dates(start, end)
    employee = _employee(db, company_id, employee_id)
    assignments = primary_assignments(db, company_id, employee_id)
    patterns = _resolver(db, company_id)
    placed: List[Tuple[date, Optional[EmployeeAssignment], bool]] = []
    by_branch: Dict[Optional[int], List[date]] = defaultdict(list)
    for day in days:
        covering = _covering(assignments, day)
        placement = covering or _nearest(assignments, day)
        placed.append((day, placement, employed_on(employee, covering, day)))
        by_branch[placement.branch_id if placement else None].append(day)
    holidays = holiday_service.holidays_by_branch_date(db, company_id, by_branch)
    chosen = _chosen_optional(db, company_id, employee_id, {h.id for items in holidays.values() for h in items})
    result: List[DayFacts] = []
    for day, placement, employed in placed:
        week = patterns.resolve(placement, day, employee_id)
        options = sorted(holidays.get(day, ()), key=lambda h: (HOLIDAY_RANK.get(h.holiday_type, 3), h.id))
        facts = [_holiday_fact(h, chosen) for h in options]
        paid = [f for f in facts if f.paid]
        holiday = paid[0] if paid else (facts[0] if facts else None)
        portion = week.portion(day)
        result.append(DayFacts(day, employed, placement, placement.branch_id if placement else None,
                               week.pattern_id, portion == workweek.FULL, holiday, week.version, week.work_source,
                               portion, week.rest_day(day)))
    return result


# ---------------------------------------------------------------------------
# Leave days and balance transactions
# ---------------------------------------------------------------------------
def portion_on(day: date, start: date, end: date, start_portion: str, end_portion: str) -> str:
    if start == end:
        if start_portion == "SECOND_HALF":
            return "SECOND_HALF"
        return "FIRST_HALF" if end_portion == "FIRST_HALF" else "FULL"
    if day == start and start_portion == "SECOND_HALF":
        return "SECOND_HALF"
    if day == end and end_portion == "FIRST_HALF":
        return "FIRST_HALF"
    return "FULL"


def _units(portion: str) -> Decimal:
    return ONE if portion == "FULL" else HALF


def counts_against_balance(facts: DayFacts, leave_type: LeaveType) -> bool:
    return facts.employed and (facts.working or bool(leave_type.counts_non_working_days))


def plan_leave(db: Session, company_id: int, employee_id: int, leave_type: LeaveType, start: date, end: date,
               start_portion: str = "FULL", end_portion: str = "FULL") -> Tuple[Decimal, List[Tuple[DayFacts, str, bool]]]:
    """The charged days of a leave and, per date, its facts, portion and whether it is charged."""
    plan = []
    total = ZERO
    for facts in day_facts(db, company_id, employee_id, start, end):
        portion = portion_on(facts.date, start, end, start_portion, end_portion)
        counted = counts_against_balance(facts, leave_type)
        if counted:
            total += _units(portion)
        plan.append((facts, portion, counted))
    return total, plan


def pay_treatment_snapshot(db: Session, company_id: int, employee_id: int, leave_type: LeaveType, on: date) -> dict:
    """LEAVE_PAY_POLICY row of the leave type in force for the employee on ``on``, else the leave type's paid flag."""
    from app.payroll.services import policies

    snapshot = {"leave_type_code": leave_type.code, "counts_non_working_days": bool(leave_type.counts_non_working_days),
                "pay_treatment": "FULL" if leave_type.is_paid else "UNPAID", "source": "LEAVE_TYPE"}
    try:
        resolved = policies.resolve_for_employee(db, company_id, "LEAVE_PAY_POLICY", employee_id, on)
    except Exception:
        return snapshot
    if resolved.get("status") != "RESOLVED":
        return snapshot
    version = resolved["version"]
    row = next((r for r in (version.get("fields") or {}).get("rows", []) if r.get("leave_type_code") == leave_type.code), None)
    if row is None:
        return snapshot | {"policy_version_id": version["id"]}
    treatment = policy_types.leave_pay_treatment(row, is_paid=bool(leave_type.is_paid))
    snapshot.update(pay_treatment=treatment, source="POLICY", policy_version_id=version["id"],
                    policy_hash=version.get("policy_hash"))
    if treatment == "PARTIAL" and row.get("partial_percent") is not None:
        snapshot["partial_percent"] = str(row["partial_percent"])
    if treatment == "TIERED":
        snapshot["tiers"] = [{"from_day": t["from_day"], "to_day": t["to_day"], "percent": str(t["percent"])}
                             for t in row.get("tiers") or []]
        snapshot["tiers_window_months"] = int(row.get("tiers_window_months") or 12)
    return snapshot


def leave_content(leave: Leave, days: Sequence[LeaveDay]) -> dict:
    return {
        "id": leave.id, "employee_id": leave.employee_id, "leave_type": leave.leave_type, "status": leave.status,
        "start_date": leave.start_date, "end_date": leave.end_date, "start_portion": leave.start_portion,
        "end_portion": leave.end_portion,
        "days": sorted(
            [{"date": d.date, "portion": d.portion, "units": d.units, "counts": bool(d.counts_against_balance),
              "pay": d.pay_treatment_snapshot, "voided": d.voided_at is not None} for d in days],
            key=lambda item: (item["date"], item["portion"])),
    }


def _active_leave_days(db: Session, leave: Leave) -> List[LeaveDay]:
    return db.query(LeaveDay).filter(LeaveDay.company_id == leave.company_id, LeaveDay.leave_id == leave.id,
                                     LeaveDay.voided_at.is_(None)).order_by(LeaveDay.date, LeaveDay.portion).all()


def _transaction(db: Session, company_id: int, *, employee_id: int, leave_type: str, delta: Decimal, reason: str,
                 key: str, effective_date: date, ref_type: str, ref_id: int, actor_user_id: Optional[int],
                 note: Optional[str] = None) -> bool:
    """Append a balance transaction once per idempotency key; False when the key was already used (EC-LV-07)."""
    exists = db.query(LeaveBalanceTransaction.id).filter(
        LeaveBalanceTransaction.company_id == company_id, LeaveBalanceTransaction.idempotency_key == key).first()
    if exists or delta == 0:
        return False
    db.add(LeaveBalanceTransaction(
        company_id=company_id, employee_id=employee_id, leave_type=leave_type, delta=delta, reason=reason,
        idempotency_key=key, effective_date=effective_date, ref_type=ref_type, ref_id=ref_id,
        created_by_user_id=actor_user_id, note=note,
    ))
    db.flush()
    return True


def _by_year(days: Iterable[LeaveDay]) -> Dict[int, Tuple[Decimal, date]]:
    years: Dict[int, Tuple[Decimal, date]] = {}
    for row in days:
        if not row.counts_against_balance:
            continue
        total, first = years.get(row.date.year, (ZERO, row.date))
        years[row.date.year] = (total + Decimal(row.units), min(first, row.date))
    return years


def generate_leave_days(db: Session, leave: Leave, actor_user_id: Optional[int] = None) -> List[LeaveDay]:
    """At approval: one leave_days row per date, USE transactions per year, and a change-log entry."""
    if _active_leave_days(db, leave):
        return _active_leave_days(db, leave)
    leave_type = db.query(LeaveType).filter(LeaveType.company_id == leave.company_id,
                                            LeaveType.code == leave.leave_type).first()
    if leave_type is None:
        raise bad_request("The leave type no longer exists.", "INVALID_LEAVE_TYPE")
    attendance_service.lock_employee(db, leave.employee_id)
    total, plan = plan_leave(db, leave.company_id, leave.employee_id, leave_type, leave.start_date, leave.end_date,
                             leave.start_portion or "FULL", leave.end_portion or "FULL")
    snapshot = pay_treatment_snapshot(db, leave.company_id, leave.employee_id, leave_type, leave.start_date)
    rows = []
    for facts, portion, counted in plan:
        rows.append(LeaveDay(company_id=leave.company_id, leave_id=leave.id, employee_id=leave.employee_id,
                             date=facts.date, portion=portion, units=_units(portion), counts_against_balance=counted,
                             pay_treatment_snapshot=snapshot))
    db.add_all(rows)
    try:
        with db.begin_nested():
            db.flush()
    except DBAPIError:
        raise conflict("Another approved leave already covers some of these dates.", "LEAVE_OVERLAP")
    leave.number_of_days = total
    for year, (units, first) in sorted(_by_year(rows).items()):
        _transaction(db, leave.company_id, employee_id=leave.employee_id, leave_type=leave.leave_type, delta=-units,
                     reason="USE", key=f"leave:{leave.id}:use:{year}", effective_date=first, ref_type="leave",
                     ref_id=leave.id, actor_user_id=actor_user_id)
    log_change(db, leave.company_id, record_type="leave", record_id=leave.id, change="CREATED",
               content=leave_content(leave, rows), employee_id=leave.employee_id, date_from=leave.start_date,
               date_to=leave.end_date, actor_user_id=actor_user_id)
    return rows


def void_leave_days(db: Session, leave: Leave, reason: str, actor_user_id: Optional[int] = None) -> None:
    """Cancellation: void the days, give the charged days back (once), log the change (EC-LV-04)."""
    rows = _active_leave_days(db, leave)
    now = utcnow()
    for row in rows:
        row.voided_at, row.void_reason = now, reason[:30]
    db.flush()
    for year, (units, first) in sorted(_by_year(rows).items()):
        _transaction(db, leave.company_id, employee_id=leave.employee_id, leave_type=leave.leave_type, delta=units,
                     reason="USE", key=f"leave:{leave.id}:cancel:{year}", effective_date=first, ref_type="leave",
                     ref_id=leave.id, actor_user_id=actor_user_id, note="Approved leave cancelled")
    log_change(db, leave.company_id, record_type="leave", record_id=leave.id, change="VOIDED",
               content=leave_content(leave, rows), employee_id=leave.employee_id, date_from=leave.start_date,
               date_to=leave.end_date, actor_user_id=actor_user_id)


def reevaluate_leave_days(db: Session, company_id: int, dates: Iterable[date], actor_user_id: Optional[int] = None) -> int:
    """A holiday changed on ``dates``: re-decide whether approved leave there is charged (EC-LV-06, risk 7)."""
    wanted = sorted(set(dates))
    if not wanted:
        return 0
    rows = db.query(LeaveDay).filter(LeaveDay.company_id == company_id, LeaveDay.date.in_(wanted),
                                     LeaveDay.voided_at.is_(None)).order_by(LeaveDay.id).all()
    changed = 0
    for row in rows:
        leave = db.get(Leave, row.leave_id)
        leave_type = leave and db.query(LeaveType).filter(LeaveType.company_id == company_id,
                                                          LeaveType.code == leave.leave_type).first()
        if leave is None or leave_type is None:
            continue
        facts = day_facts(db, company_id, row.employee_id, row.date, row.date)[0]
        counted = counts_against_balance(facts, leave_type)
        if counted == bool(row.counts_against_balance):
            continue
        row.counts_against_balance = counted
        units = Decimal(row.units)
        delta = -units if counted else units
        previous = db.query(func.count(LeaveBalanceTransaction.id)).filter(
            LeaveBalanceTransaction.company_id == company_id, LeaveBalanceTransaction.ref_type == "leave_day",
            LeaveBalanceTransaction.ref_id == row.id).scalar() or 0
        _transaction(db, company_id, employee_id=row.employee_id, leave_type=leave.leave_type, delta=delta, reason="USE",
                     key=f"leave_day:{row.id}:recount:{previous + 1}", effective_date=row.date, ref_type="leave_day",
                     ref_id=row.id, actor_user_id=actor_user_id,
                     note="Holiday calendar changed" if not counted else "Holiday removed")
        leave.number_of_days = Decimal(leave.number_of_days) - delta
        db.flush()
        log_change(db, company_id, record_type="leave", record_id=leave.id, change="UPDATED",
                   content=leave_content(leave, _active_leave_days(db, leave)), employee_id=leave.employee_id,
                   date_from=row.date, actor_user_id=actor_user_id, details={"reason": "HOLIDAY_CHANGED"})
        changed += 1
    return changed


def _add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(day.day, _calendar.monthrange(year, month)[1]))


def tiered_history(db: Session, company_id: int, employee_id: int, leave_type_code: str, start: date, end: date,
                   window_months: int = 12) -> Tuple[Decimal, Tuple[Tuple[date, Decimal], ...]]:
    """``days_before_period`` and ``rolls_off`` of TIERED pay (F-02, EC-LV-05).

    A leave day ``x`` is inside the rolling window of date ``d`` while ``d < x + window_months``.
    """
    earliest = _add_months(start, -window_months)
    rows = (
        db.query(LeaveDay.date, LeaveDay.units)
        .join(Leave, (Leave.company_id == LeaveDay.company_id) & (Leave.id == LeaveDay.leave_id))
        .filter(LeaveDay.company_id == company_id, LeaveDay.employee_id == employee_id, LeaveDay.voided_at.is_(None),
                LeaveDay.counts_against_balance.is_(True), Leave.leave_type == leave_type_code,
                LeaveDay.date >= earliest, LeaveDay.date < start)
        .all()
    )
    before = ZERO
    rolls: Dict[date, Decimal] = defaultdict(lambda: ZERO)
    for day, units in rows:
        leaves_window = _add_months(day, window_months)
        if leaves_window < start:
            continue
        before += Decimal(units)
        if leaves_window <= end:
            rolls[leaves_window] += Decimal(units)
    return before, tuple(sorted(rolls.items()))


# ---------------------------------------------------------------------------
# Ledger inputs (the snapshot builder's read API)
# ---------------------------------------------------------------------------
def _shift_hours(shift: Optional[Shift]) -> Optional[Decimal]:
    if shift is None or shift.duration_hours is None:
        return None
    hours = Decimal(shift.duration_hours) - Decimal(shift.unpaid_break_minutes or 0) / Decimal(60)
    return hours if hours > 0 else None


def _leave_portions(db: Session, company_id: int, employee_id: int, start: date, end: date
                    ) -> Dict[date, List[ledger.LeavePortion]]:
    rows = (
        db.query(LeaveDay, Leave)
        .join(Leave, (Leave.company_id == LeaveDay.company_id) & (Leave.id == LeaveDay.leave_id))
        .filter(LeaveDay.company_id == company_id, LeaveDay.employee_id == employee_id, LeaveDay.voided_at.is_(None),
                LeaveDay.date >= start, LeaveDay.date <= end, Leave.status == "approved")
        .order_by(LeaveDay.date, LeaveDay.portion)
        .all()
    )
    hashes: Dict[int, str] = {}
    tiered: Dict[Tuple[str, int], ledger.TieredPay] = {}
    result: Dict[date, List[ledger.LeavePortion]] = defaultdict(list)
    for row, leave in rows:
        if leave.id not in hashes:
            hashes[leave.id] = content_hash(leave_content(leave, _active_leave_days(db, leave)))
        snap = row.pay_treatment_snapshot or {}
        treatment = snap.get("pay_treatment") or "FULL"
        partial = Decimal(snap["partial_percent"]) if treatment == "PARTIAL" and snap.get("partial_percent") else None
        tiered_pay = None
        if treatment == "TIERED":
            window = int(snap.get("tiers_window_months") or 12)
            key = (leave.leave_type, window)
            if key not in tiered:
                before, rolls = tiered_history(db, company_id, employee_id, leave.leave_type, start, end, window)
                tiers = tuple(ledger.Tier(t["from_day"], t.get("to_day"), Decimal(str(t["percent"])))
                              for t in snap.get("tiers") or [])
                tiered[key] = ledger.TieredPay(tiers, before, rolls)
            tiered_pay = tiered[key]
        if treatment == "PARTIAL" and partial is None:
            treatment = "UNPAID"
        result[row.date].append(ledger.LeavePortion(
            leave_id=leave.id, leave_type_code=leave.leave_type, portion=row.portion, pay_treatment=treatment,
            partial_percent=partial, tiered=tiered_pay,
            counts_non_working_days=bool(snap.get("counts_non_working_days", False)), content_hash=hashes[leave.id],
        ))
    return result


def _attendance_inputs(db: Session, company_id: int, employee_id: int, start: date, end: date,
                       lock_at: Optional[datetime], early_exit_grace_minutes: int) -> Dict[date, ledger.AttendanceInput]:
    records = (
        db.query(Attendance)
        .filter(Attendance.company_id == company_id, Attendance.employee_id == employee_id,
                Attendance.date >= start, Attendance.date <= end)
        .order_by(Attendance.date, Attendance.check_in_time)
        .all()
    )
    if not records:
        return {}
    if lock_at is not None and any(attendance_service.is_live(r) for r in records):
        policy = attendance_policy.load(db, company_id)
        attendance_service.lock_employee(db, employee_id)
        for record in records:
            db.refresh(record)
            if attendance_service.is_live(record):
                attendance_service.expire_if_needed(db, record, policy, lock_at)
    now = lock_at or attendance_service._now()
    breaks = attendance_service.breaks_by_session(db, [r.id for r in records])
    result: Dict[date, ledger.AttendanceInput] = {}
    for record in records:
        hours = attendance_service.hours_for(record, breaks[record.id], now)
        closed = record.closed_at is not None and record.check_out_time is None
        worked = hours.net_seconds if (hours.complete and not hours.provisional and not closed) else None
        early = bool(record.early_exit) and (record.early_exit_minutes or 0) > early_exit_grace_minutes
        # A correction rewrites the stored mark itself, so no mark is both stored and regularised (audit 03 §1.8).
        result[record.date] = ledger.AttendanceInput(
            status=STATUS_MAP.get(record.status, "PRESENT"), session_id=record.id, closed_without_punch_out=closed,
            worked_seconds=worked, late=record.status == "late", early_exit=early, regularised=False,
            content_hash=attendance_service.content_hash(record, breaks[record.id]),
        )
    return result


def _lop_by_date(db: Session, company_id: int, employee_id: int, start: date, end: date, statuses=("APPROVED",)):
    result: Dict[date, List[LopInput]] = defaultdict(list)
    for row in db.query(LopInput).filter(LopInput.company_id == company_id, LopInput.employee_id == employee_id,
                                         LopInput.date >= start, LopInput.date <= end, LopInput.status.in_(statuses)):
        result[row.date].append(row)
    return result


def _on_duty_by_date(db: Session, company_id: int, employee_id: int, start: date, end: date):
    result: Dict[date, List[OnDutyRequest]] = defaultdict(list)
    for row in db.query(OnDutyRequest).filter(OnDutyRequest.company_id == company_id,
                                              OnDutyRequest.employee_id == employee_id, OnDutyRequest.date >= start,
                                              OnDutyRequest.date <= end, OnDutyRequest.status == "APPROVED"):
        result[row.date].append(row)
    return result


def _suspensions(db: Session, company_id: int, employee_id: int, start: date, end: date) -> List[EmployeeSuspension]:
    return db.query(EmployeeSuspension).filter(
        EmployeeSuspension.company_id == company_id, EmployeeSuspension.employee_id == employee_id,
        EmployeeSuspension.status == "ACTIVE", EmployeeSuspension.paid.is_(False),
        EmployeeSuspension.start_date <= end, EmployeeSuspension.end_date >= start,
    ).order_by(EmployeeSuspension.start_date).all()


def lop_content(row: LopInput) -> dict:
    return {"id": row.id, "employee_id": row.employee_id, "date": row.date, "portion": row.portion,
            "status": row.status, "reason": row.reason}


def suspension_content(row: EmployeeSuspension) -> dict:
    return {"id": row.id, "employee_id": row.employee_id, "start_date": row.start_date, "end_date": row.end_date,
            "paid": bool(row.paid), "status": row.status}


def on_duty_content(row: OnDutyRequest) -> dict:
    return {"id": row.id, "employee_id": row.employee_id, "date": row.date, "portion": row.portion,
            "status": row.status}


def ledger_inputs(
    db: Session, company_id: int, employee_id: int, start: date, end: date, *,
    lock_at: Optional[datetime] = None, early_exit_grace_minutes: int = 0,
    standard_hours_per_day: Optional[Decimal] = None, use_shift_duration: bool = True,
) -> List[ledger.DayInput]:
    """Every date of ``start..end`` as a ledger.DayInput (F-02, F-03, F-04).

    Dates outside employment carry ``employed=False`` with the weekly off and holiday of the nearest
    placement. Approved leave days, holidays of the branch on each date (optional ones per the
    employee's choice), approved manual LOP and on-duty, unpaid suspensions and attendance with canonical
    net seconds (None when the punch-out is missing). With ``lock_at`` live sessions that are past the
    maximum shift length at that instant are first closed as missing punch-outs (audit 03 §1.3).
    ``segment`` is the primary assignment id; ``standard_hours`` is the net hours of the shift that resolves on
    the date (``shift_resolution``: rule > placement, JC-AS-02), or ``standard_hours_per_day``, times the
    assignment FTE when either differs from the default; ``use_shift_duration = False`` uses the standard hours
    for everyone (BUG-AS-31).
    """
    facts = day_facts(db, company_id, employee_id, start, end)
    leaves = _leave_portions(db, company_id, employee_id, start, end)
    attendance = _attendance_inputs(db, company_id, employee_id, start, end, lock_at, early_exit_grace_minutes)
    lops = _lop_by_date(db, company_id, employee_id, start, end)
    duties = _on_duty_by_date(db, company_id, employee_id, start, end)
    suspensions = _suspensions(db, company_id, employee_id, start, end)
    from app.services import shift_resolution

    resolved = shift_resolution.shifts_for_many(db, company_id, [employee_id], start, end)
    days: List[ledger.DayInput] = []
    for fact in facts:
        assignment = fact.assignment
        suspension = next((s for s in suspensions if s.start_date <= fact.date <= s.end_date), None)
        holiday = None
        if fact.holiday is not None:
            holiday = ledger.HolidayInput(fact.holiday.id, fact.holiday.type, fact.holiday.name, fact.holiday.content_hash)
        standard = None
        if assignment is not None and fact.employed:
            fte = Decimal(assignment.fte if assignment.fte is not None else 1)
            standard = shift_resolution.standard_hours(resolved.get((employee_id, fact.date)), fte,
                                                       standard_hours_per_day, use_shift_duration=use_shift_duration)
        days.append(ledger.DayInput(
            date=fact.date,
            employed=fact.employed,
            suspended_unpaid=suspension is not None and fact.employed,
            leaves=tuple(leaves.get(fact.date, ())),
            holiday=holiday,
            weekly_off=fact.weekly_off,
            manual_lop=tuple(ledger.ManualLop(r.id, r.portion, content_hash(lop_content(r))) for r in lops.get(fact.date, ())),
            attendance=attendance.get(fact.date),
            on_duty=tuple(ledger.OnDuty(r.id, r.portion, content_hash(on_duty_content(r))) for r in duties.get(fact.date, ())),
            segment=str(assignment.id) if assignment is not None and fact.employed else "",
            standard_hours=standard,
            assignment_id=assignment.id if assignment is not None else None,
            suspension_id=suspension.id if suspension is not None and fact.employed else None,
            work_week_id=fact.work_week_id,
        ))
    return days


def pending_inputs(db: Session, company_id: int, employee_id: int, start: date, end: date) -> List[dict]:
    """Requests still awaiting a decision that touch ``start..end`` (exceptions at the input cutoff, EC-LV-02)."""
    items: List[dict] = []
    for leave in db.query(Leave).filter(
        Leave.company_id == company_id, Leave.employee_id == employee_id, Leave.start_date <= end,
        Leave.end_date >= start,
        or_(Leave.status == "pending", (Leave.status == "approved") & (Leave.cancellation_status == "pending")),
    ).order_by(Leave.start_date, Leave.id):
        items.append({"type": "leave_cancellation" if leave.status == "approved" else "leave", "id": leave.id,
                      "start": leave.start_date.isoformat(), "end": leave.end_date.isoformat(),
                      "leave_type_code": leave.leave_type})
    for row in _lop_by_date(db, company_id, employee_id, start, end, statuses=("PENDING",)).values():
        items.extend({"type": "lop_input", "id": r.id, "start": r.date.isoformat(), "end": r.date.isoformat(),
                      "portion": r.portion} for r in row)
    for row in db.query(OnDutyRequest).filter(
        OnDutyRequest.company_id == company_id, OnDutyRequest.employee_id == employee_id,
        OnDutyRequest.date >= start, OnDutyRequest.date <= end, OnDutyRequest.status == "PENDING",
    ).order_by(OnDutyRequest.date, OnDutyRequest.id):
        items.append({"type": "on_duty", "id": row.id, "start": row.date.isoformat(), "end": row.date.isoformat(),
                      "portion": row.portion})
    return items


# ---------------------------------------------------------------------------
# Shared command helpers
# ---------------------------------------------------------------------------
def _audit(db: Session, principal: Optional[Principal], action: str, description: str, entity_type: str, entity_id: int,
           **details: Any) -> None:
    AuditLogService.log(db, principal, AUDIT_MODULE, action, description, entity_type, entity_id,
                        details=json.loads(json.dumps(details, default=_json_default)))


def _portion(value: str) -> str:
    if value not in PORTIONS:
        raise bad_request("Portion must be FULL, FIRST_HALF or SECOND_HALF.", "INVALID_PORTION")
    return value


def _slots(portion: str) -> set:
    return {"AM", "PM"} if portion == "FULL" else ({"AM"} if portion == "FIRST_HALF" else {"PM"})


def _overlapping(rows: Iterable[Any], portion: str) -> bool:
    return any(_slots(r.portion) & _slots(portion) for r in rows)


def _ensure_in_scope(db: Session, principal: Principal, permission: str, employee_id: int, start: date, end: date) -> None:
    access.ensure_employee_in_scope(db, principal, permission, employee_id, start, end)


def _check_row_version(row: Any, expected: Optional[int]) -> None:
    from app.core import concurrency

    concurrency.check_row_version(row, expected)


# ---------------------------------------------------------------------------
# Manual LOP inputs (EC-ATT-16)
# ---------------------------------------------------------------------------
def lop_json(row: LopInput) -> dict:
    return {"id": row.id, "employee_id": row.employee_id, "date": row.date.isoformat(), "portion": row.portion,
            "reason": row.reason, "status": row.status, "approval_request_id": row.approval_request_id,
            "created_by_user_id": row.created_by_user_id, "row_version": row.row_version,
            "decided_at": row.decided_at.isoformat() if row.decided_at else None,
            "cancelled_at": row.cancelled_at.isoformat() if row.cancelled_at else None}


def lop_conflicts(db: Session, company_id: int, employee_id: int, day: date, portion: str) -> List[dict]:
    """Why a manual LOP input on ``portion`` of ``day`` is refused: a leave, holiday, weekly off, suspension or LOP slot."""
    inputs = ledger_inputs(db, company_id, employee_id, day, day)[0]
    cfg = ledger.LedgerConfig(attendance_enabled=True, leave_enabled=True)
    found = [item.to_dict() for item in ledger.manual_lop_conflicts(inputs, portion, cfg)]
    pending = _lop_by_date(db, company_id, employee_id, day, day, statuses=("PENDING",)).get(day, [])
    if _overlapping(pending, portion):
        found.append({"code": "MANUAL_LOP_CONFLICT", "message": "A manual LOP input for this slot is awaiting approval.",
                      "detail": "PENDING_LOP"})
    return found


def create_lop_input(db: Session, principal: Principal, *, employee_id: int, day: date, portion: str, reason: str) -> dict:
    _ensure_in_scope(db, principal, INPUTS_EDIT, employee_id, day, day)
    portion = _portion(portion)
    reason = (reason or "").strip()
    if not reason:
        raise bad_request("Enter a reason for the LOP input.", "REASON_REQUIRED")
    attendance_service.lock_employee(db, employee_id)
    conflicts = lop_conflicts(db, principal.company_id, employee_id, day, portion)
    if conflicts:
        raise conflict("Manual LOP cannot be entered on this slot: " + conflicts[0]["message"], "LOP_SLOT_TAKEN")
    row = LopInput(company_id=principal.company_id, employee_id=employee_id, date=day, portion=portion, reason=reason,
                   status="PENDING", created_by_user_id=principal.user_id)
    db.add(row)
    db.flush()
    request = approvals.submit(db, principal, module=LOP_MODULE, subject_type=LOP_SUBJECT, subject_id=row.id,
                               subject_employee_id=employee_id, context={"date": day, "portion": portion})
    row.approval_request_id = request.id
    db.flush()
    _audit(db, principal, "LOP_INPUT_CREATED", f"Manual LOP for employee #{employee_id} on {day} ({portion})",
           "lop_input", row.id, after=lop_content(row), approval_request_id=request.id)
    return lop_json(row)


@approvals.on_decided(LOP_MODULE, LOP_SUBJECT)
def _lop_decided(db: Session, principal: Optional[Principal], request) -> None:
    row = db.query(LopInput).filter(LopInput.company_id == request.company_id, LopInput.id == request.subject_id).first()
    if row is None or row.status != "PENDING":
        return
    row.status = {"APPROVED": "APPROVED", "REJECTED": "REJECTED"}.get(request.status, "CANCELLED")
    row.decided_at = utcnow()
    row.row_version += 1
    db.flush()
    log_change(db, row.company_id, record_type="lop_input", record_id=row.id, change="UPDATED",
               content=lop_content(row), employee_id=row.employee_id, date_from=row.date,
               actor_user_id=principal.user_id if principal else None)


def cancel_lop_input(db: Session, principal: Principal, lop_id: int, *, row_version: Optional[int] = None) -> dict:
    row = db.query(LopInput).filter(LopInput.company_id == principal.company_id, LopInput.id == lop_id).with_for_update().first()
    if row is None:
        raise not_found("LOP input")
    _ensure_in_scope(db, principal, INPUTS_EDIT, row.employee_id, row.date, row.date)
    _check_row_version(row, row_version)
    if row.status not in ("PENDING", "APPROVED"):
        raise conflict(f"This LOP input is already {row.status.lower()}.", "LOP_INPUT_CLOSED")
    if row.status == "PENDING":
        approvals.withdraw(db, principal, module=LOP_MODULE, subject_type=LOP_SUBJECT, subject_id=row.id,
                           reason="LOP input cancelled")
    before = lop_content(row)
    row.status, row.cancelled_at, row.cancelled_by_user_id = "CANCELLED", utcnow(), principal.user_id
    row.row_version += 1
    db.flush()
    log_change(db, row.company_id, record_type="lop_input", record_id=row.id, change="VOIDED", content=lop_content(row),
               employee_id=row.employee_id, date_from=row.date, actor_user_id=principal.user_id)
    _audit(db, principal, "LOP_INPUT_CANCELLED", f"Cancelled manual LOP #{row.id}", "lop_input", row.id,
           before=before, after=lop_content(row))
    return lop_json(row)


def _range_filter(column_from, column_to, start: Optional[date], end: Optional[date]):
    clauses = []
    if start is not None:
        clauses.append(column_to >= start)
    if end is not None:
        clauses.append(column_from <= end)
    return clauses


def _list_scope(db: Session, principal: Principal, permission: str, start: Optional[date], end: Optional[date]):
    if not principal.has(permission):
        raise forbidden()
    if start is not None and end is not None and end < start:
        raise bad_request("The end date is before the start date.", "INVALID_RANGE")
    today = clock.company_today(db, principal.company_id)
    return access.payroll_employee_scope(db, principal, permission, start or date(1900, 1, 1), end or today + timedelta(days=3660))


def list_lop_inputs(db: Session, principal: Principal, *, employee_id: Optional[int] = None, start: Optional[date] = None,
                    end: Optional[date] = None, status: Optional[str] = None, limit: int = 200) -> List[dict]:
    scope = _list_scope(db, principal, INPUTS_READ, start, end)
    query = db.query(LopInput).filter(LopInput.company_id == principal.company_id, scope.clause(LopInput.employee_id),
                                      *_range_filter(LopInput.date, LopInput.date, start, end))
    if employee_id is not None:
        query = query.filter(LopInput.employee_id == employee_id)
    if status:
        query = query.filter(LopInput.status == status)
    return [lop_json(r) for r in query.order_by(LopInput.date.desc(), LopInput.id.desc()).limit(limit)]


# ---------------------------------------------------------------------------
# Suspensions
# ---------------------------------------------------------------------------
def suspension_json(row: EmployeeSuspension) -> dict:
    return {"id": row.id, "employee_id": row.employee_id, "start_date": row.start_date.isoformat(),
            "end_date": row.end_date.isoformat(), "paid": bool(row.paid), "reason": row.reason, "status": row.status,
            "row_version": row.row_version, "created_by_user_id": row.created_by_user_id,
            "cancelled_at": row.cancelled_at.isoformat() if row.cancelled_at else None}


def create_suspension(db: Session, principal: Principal, *, employee_id: int, start: date, end: date, paid: bool,
                      reason: str) -> dict:
    if end < start:
        raise bad_request("The end date is before the start date.", "INVALID_RANGE")
    _ensure_in_scope(db, principal, INPUTS_EDIT, employee_id, start, end)
    reason = (reason or "").strip()
    if not reason:
        raise bad_request("Enter a reason for the suspension.", "REASON_REQUIRED")
    attendance_service.lock_employee(db, employee_id)
    overlap = db.query(EmployeeSuspension.id).filter(
        EmployeeSuspension.company_id == principal.company_id, EmployeeSuspension.employee_id == employee_id,
        EmployeeSuspension.status == "ACTIVE", EmployeeSuspension.start_date <= end, EmployeeSuspension.end_date >= start,
    ).first()
    if overlap:
        raise conflict("Another suspension already covers some of these dates.", "SUSPENSION_OVERLAP")
    employee = _employee(db, principal.company_id, employee_id)
    assignments = primary_assignments(db, principal.company_id, employee_id)
    if not all(employed_on(employee, _covering(assignments, d), d) for d in (start, end)):
        raise bad_request("A suspension must fall inside the employment.", "OUTSIDE_EMPLOYMENT")
    row = EmployeeSuspension(company_id=principal.company_id, employee_id=employee_id, start_date=start, end_date=end,
                             paid=paid, reason=reason, status="ACTIVE", created_by_user_id=principal.user_id)
    db.add(row)
    db.flush()
    log_change(db, row.company_id, record_type="suspension", record_id=row.id, change="CREATED",
               content=suspension_content(row), employee_id=employee_id, date_from=start, date_to=end,
               actor_user_id=principal.user_id)
    _audit(db, principal, "SUSPENSION_CREATED",
           f"{'Paid' if paid else 'Unpaid'} suspension of employee #{employee_id} from {start} to {end}",
           "employee_suspension", row.id, after=suspension_content(row))
    return suspension_json(row)


def cancel_suspension(db: Session, principal: Principal, suspension_id: int, *, row_version: Optional[int] = None) -> dict:
    row = db.query(EmployeeSuspension).filter(EmployeeSuspension.company_id == principal.company_id,
                                              EmployeeSuspension.id == suspension_id).with_for_update().first()
    if row is None:
        raise not_found("Suspension")
    _ensure_in_scope(db, principal, INPUTS_EDIT, row.employee_id, row.start_date, row.end_date)
    _check_row_version(row, row_version)
    if row.status != "ACTIVE":
        raise conflict("This suspension is already cancelled.", "SUSPENSION_CLOSED")
    before = suspension_content(row)
    row.status, row.cancelled_at, row.cancelled_by_user_id = "CANCELLED", utcnow(), principal.user_id
    row.row_version += 1
    db.flush()
    log_change(db, row.company_id, record_type="suspension", record_id=row.id, change="VOIDED",
               content=suspension_content(row), employee_id=row.employee_id, date_from=row.start_date,
               date_to=row.end_date, actor_user_id=principal.user_id)
    _audit(db, principal, "SUSPENSION_CANCELLED", f"Cancelled suspension #{row.id}", "employee_suspension", row.id,
           before=before, after=suspension_content(row))
    return suspension_json(row)


def list_suspensions(db: Session, principal: Principal, *, employee_id: Optional[int] = None, start: Optional[date] = None,
                     end: Optional[date] = None, limit: int = 200) -> List[dict]:
    scope = _list_scope(db, principal, INPUTS_READ, start, end)
    query = db.query(EmployeeSuspension).filter(
        EmployeeSuspension.company_id == principal.company_id, scope.clause(EmployeeSuspension.employee_id),
        *_range_filter(EmployeeSuspension.start_date, EmployeeSuspension.end_date, start, end))
    if employee_id is not None:
        query = query.filter(EmployeeSuspension.employee_id == employee_id)
    return [suspension_json(r) for r in query.order_by(EmployeeSuspension.start_date.desc()).limit(limit)]


# ---------------------------------------------------------------------------
# On-duty requests
# ---------------------------------------------------------------------------
def on_duty_json(row: OnDutyRequest) -> dict:
    return {"id": row.id, "employee_id": row.employee_id, "date": row.date.isoformat(), "portion": row.portion,
            "reason": row.reason, "status": row.status, "approval_request_id": row.approval_request_id,
            "requested_by_user_id": row.requested_by_user_id, "row_version": row.row_version,
            "decided_at": row.decided_at.isoformat() if row.decided_at else None}


def _on_duty_target(db: Session, principal: Principal, employee_id: Optional[int]) -> Employee:
    if employee_id is None or authz.is_self(principal, employee_id):
        if not principal.has("attendance:mark"):
            raise forbidden()
        return authz.own_employee(db, principal)
    return authz.ensure_employee_access(db, principal, "attendance:regularize", employee_id)


def create_on_duty(db: Session, principal: Principal, *, employee_id: Optional[int], day: date, portion: str,
                   reason: str) -> dict:
    employee = _on_duty_target(db, principal, employee_id)
    portion = _portion(portion)
    reason = (reason or "").strip()
    if not reason:
        raise bad_request("Enter where or why you were on duty.", "REASON_REQUIRED")
    attendance_service.lock_employee(db, employee.id)
    facts = day_facts(db, principal.company_id, employee.id, day, day)[0]
    if not facts.employed:
        raise bad_request("On-duty work must fall inside the employment.", "OUTSIDE_EMPLOYMENT")
    existing = db.query(OnDutyRequest).filter(
        OnDutyRequest.company_id == principal.company_id, OnDutyRequest.employee_id == employee.id,
        OnDutyRequest.date == day, OnDutyRequest.status.in_(("PENDING", "APPROVED"))).all()
    if _overlapping(existing, portion):
        raise conflict("On-duty work is already recorded for this slot.", "ON_DUTY_OVERLAP")
    row = OnDutyRequest(company_id=principal.company_id, employee_id=employee.id, date=day, portion=portion,
                        reason=reason, status="PENDING", requested_by_user_id=principal.user_id)
    db.add(row)
    db.flush()
    request = approvals.submit(db, principal, module=ON_DUTY_MODULE, subject_type=ON_DUTY_SUBJECT, subject_id=row.id,
                               subject_employee_id=employee.id, context={"date": day, "portion": portion})
    row.approval_request_id = request.id
    db.flush()
    _audit(db, principal, "ON_DUTY_REQUESTED", f"On-duty request for employee #{employee.id} on {day} ({portion})",
           "on_duty_request", row.id, after=on_duty_content(row), approval_request_id=request.id)
    return on_duty_json(row)


@approvals.on_decided(ON_DUTY_MODULE, ON_DUTY_SUBJECT)
def _on_duty_decided(db: Session, principal: Optional[Principal], request) -> None:
    row = db.query(OnDutyRequest).filter(OnDutyRequest.company_id == request.company_id,
                                         OnDutyRequest.id == request.subject_id).first()
    if row is None or row.status != "PENDING":
        return
    row.status = {"APPROVED": "APPROVED", "REJECTED": "REJECTED"}.get(request.status, "CANCELLED")
    row.decided_at = utcnow()
    row.row_version += 1
    db.flush()
    log_change(db, row.company_id, record_type="on_duty", record_id=row.id, change="UPDATED",
               content=on_duty_content(row), employee_id=row.employee_id, date_from=row.date,
               actor_user_id=principal.user_id if principal else None)


def cancel_on_duty(db: Session, principal: Principal, request_id: int, *, row_version: Optional[int] = None) -> dict:
    row = db.query(OnDutyRequest).filter(OnDutyRequest.company_id == principal.company_id,
                                         OnDutyRequest.id == request_id).with_for_update().first()
    if row is None:
        raise not_found("On-duty request")
    own = authz.is_self(principal, row.employee_id)
    if not own:
        if not authz.can_access_employee(db, principal, "attendance:regularize", row.employee_id):
            raise not_found("On-duty request")
    _check_row_version(row, row_version)
    if row.status not in ("PENDING", "APPROVED"):
        raise conflict(f"This request is already {row.status.lower()}.", "ON_DUTY_CLOSED")
    if row.status == "APPROVED" and own and not principal.has_company_scope("attendance:regularize"):
        raise forbidden("Ask HR to withdraw approved on-duty work.")
    if row.status == "PENDING":
        approvals.withdraw(db, principal, module=ON_DUTY_MODULE, subject_type=ON_DUTY_SUBJECT, subject_id=row.id,
                           reason="On-duty request cancelled")
    before = on_duty_content(row)
    row.status, row.cancelled_at = "CANCELLED", utcnow()
    row.row_version += 1
    db.flush()
    log_change(db, row.company_id, record_type="on_duty", record_id=row.id, change="VOIDED",
               content=on_duty_content(row), employee_id=row.employee_id, date_from=row.date,
               actor_user_id=principal.user_id)
    _audit(db, principal, "ON_DUTY_CANCELLED", f"Cancelled on-duty request #{row.id}", "on_duty_request", row.id,
           before=before, after=on_duty_content(row))
    return on_duty_json(row)


def list_on_duty(db: Session, principal: Principal, *, employee_id: Optional[int] = None, start: Optional[date] = None,
                 end: Optional[date] = None, status: Optional[str] = None, limit: int = 200) -> List[dict]:
    if start is not None and end is not None and end < start:
        raise bad_request("The end date is before the start date.", "INVALID_RANGE")
    query = db.query(OnDutyRequest).filter(OnDutyRequest.company_id == principal.company_id,
                                           *_range_filter(OnDutyRequest.date, OnDutyRequest.date, start, end))
    if principal.has("attendance:read"):
        scope = authz.employee_scope(db, principal, "attendance:read")
        query = query.filter(scope.clause(OnDutyRequest.employee_id))
    elif principal.employee_id is not None and principal.has("attendance:mark"):
        query = query.filter(OnDutyRequest.employee_id == principal.employee_id)
    else:
        raise forbidden()
    if employee_id is not None:
        query = query.filter(OnDutyRequest.employee_id == employee_id)
    if status:
        query = query.filter(OnDutyRequest.status == status)
    return [on_duty_json(r) for r in query.order_by(OnDutyRequest.date.desc(), OnDutyRequest.id.desc()).limit(limit)]


# ---------------------------------------------------------------------------
# Optional holidays (EC-HOL-05)
# ---------------------------------------------------------------------------
def optional_quota(db: Session, company_id: int, employee_id: int, on: date) -> int:
    from app.payroll.services import settings as settings_engine
    from app.payroll.settings_registry import UNSET

    value = settings_engine.resolve(db, company_id, "holiday.optional_quota_per_year", employee_id=employee_id, on=on).value
    return 0 if value is UNSET or value is None else int(value)


def _optional_holidays_for(db: Session, company_id: int, employee_id: int, year: int) -> List[Holiday]:
    start, end = date(year, 1, 1), date(year, 12, 31)
    seen: Dict[int, Holiday] = {}
    for fact in day_facts(db, company_id, employee_id, start, end):
        if fact.holiday is None:
            continue
        for holiday in holiday_service.holidays_by_branch_date(db, company_id, {fact.branch_id: [fact.date]}).get(fact.date, []):
            if holiday.holiday_type == "OPTIONAL":
                seen[holiday.id] = holiday
    return sorted(seen.values(), key=lambda h: (h.date, h.id))


def optional_holidays(db: Session, principal: Principal, *, year: int, employee_id: Optional[int] = None) -> dict:
    if employee_id is None or authz.is_self(principal, employee_id):
        if not principal.has("holiday:read"):
            raise forbidden()
        employee = authz.own_employee(db, principal)
    else:
        if not principal.has_company_scope("holiday:manage"):
            raise forbidden()
        employee = _employee(db, principal.company_id, employee_id)
    available = _optional_holidays_for(db, principal.company_id, employee.id, year)
    chosen = {r.holiday_id: r for r in db.query(EmployeeOptionalHoliday).filter(
        EmployeeOptionalHoliday.company_id == principal.company_id, EmployeeOptionalHoliday.employee_id == employee.id,
        EmployeeOptionalHoliday.year == year, EmployeeOptionalHoliday.status == "CHOSEN")}
    quota = optional_quota(db, principal.company_id, employee.id, date(year, 12, 31))
    return {
        "employee_id": employee.id, "year": year, "quota": quota, "used": len(chosen),
        "remaining": max(quota - len(chosen), 0),
        "holidays": [holiday_service.serialize(h) | {"chosen": h.id in chosen} for h in available],
    }


def choose_optional_holiday(db: Session, principal: Principal, holiday_id: int) -> dict:
    if not principal.has("holiday:read"):
        raise forbidden()
    employee = authz.own_employee(db, principal)
    holiday = db.query(Holiday).filter(Holiday.company_id == principal.company_id, Holiday.id == holiday_id).first()
    if holiday is None or holiday.holiday_type != "OPTIONAL":
        raise not_found("Optional holiday")
    if holiday.id not in {h.id for h in _optional_holidays_for(db, principal.company_id, employee.id, holiday.date.year)}:
        raise not_found("Optional holiday")
    if holiday.date < clock.company_today(db, principal.company_id):
        raise conflict("Optional holidays are chosen before the day.", "HOLIDAY_IN_PAST")
    attendance_service.lock_employee(db, employee.id)
    rows = db.query(EmployeeOptionalHoliday).filter(
        EmployeeOptionalHoliday.company_id == principal.company_id, EmployeeOptionalHoliday.employee_id == employee.id,
        EmployeeOptionalHoliday.year == holiday.date.year, EmployeeOptionalHoliday.status == "CHOSEN").all()
    if any(r.holiday_id == holiday.id for r in rows):
        raise conflict("You already chose this holiday.", "ALREADY_CHOSEN")
    quota = optional_quota(db, principal.company_id, employee.id, holiday.date)
    if len(rows) >= quota:
        raise conflict(f"You have used all {quota} optional holidays of {holiday.date.year}; take leave instead.",
                       "OPTIONAL_QUOTA_EXCEEDED")
    row = EmployeeOptionalHoliday(company_id=principal.company_id, employee_id=employee.id, holiday_id=holiday.id,
                                  year=holiday.date.year, status="CHOSEN", chosen_by_user_id=principal.user_id)
    db.add(row)
    db.flush()
    log_change(db, row.company_id, record_type="optional_holiday", record_id=row.id, change="CREATED",
               content={"holiday_id": holiday.id, "employee_id": employee.id, "status": row.status},
               employee_id=employee.id, date_from=holiday.date, actor_user_id=principal.user_id)
    reevaluate_leave_days(db, principal.company_id, [holiday.date], actor_user_id=principal.user_id)
    AuditLogService.log(db, principal, "Holidays", "OPTIONAL_CHOSEN", f"Chose optional holiday '{holiday.name}'",
                        "employee_optional_holiday", row.id, details={"holiday_id": holiday.id, "year": row.year})
    return {"id": row.id, "holiday_id": holiday.id, "year": row.year, "status": row.status}


def cancel_optional_holiday(db: Session, principal: Principal, holiday_id: int) -> dict:
    if not principal.has("holiday:read"):
        raise forbidden()
    employee = authz.own_employee(db, principal)
    row = db.query(EmployeeOptionalHoliday).filter(
        EmployeeOptionalHoliday.company_id == principal.company_id, EmployeeOptionalHoliday.employee_id == employee.id,
        EmployeeOptionalHoliday.holiday_id == holiday_id, EmployeeOptionalHoliday.status == "CHOSEN").with_for_update().first()
    if row is None:
        raise not_found("Optional holiday choice")
    holiday = db.get(Holiday, holiday_id)
    if holiday.date < clock.company_today(db, principal.company_id):
        raise conflict("A past optional holiday stays as taken.", "HOLIDAY_IN_PAST")
    row.status, row.cancelled_at = "CANCELLED", utcnow()
    db.flush()
    log_change(db, row.company_id, record_type="optional_holiday", record_id=row.id, change="VOIDED",
               content={"holiday_id": holiday_id, "employee_id": employee.id, "status": row.status},
               employee_id=employee.id, date_from=holiday.date, actor_user_id=principal.user_id)
    reevaluate_leave_days(db, principal.company_id, [holiday.date], actor_user_id=principal.user_id)
    AuditLogService.log(db, principal, "Holidays", "OPTIONAL_CANCELLED", f"Withdrew optional holiday '{holiday.name}'",
                        "employee_optional_holiday", row.id, details={"holiday_id": holiday_id})
    return {"id": row.id, "holiday_id": holiday_id, "year": row.year, "status": row.status}


# ---------------------------------------------------------------------------
# Day-ledger preview
# ---------------------------------------------------------------------------
def _preview_config(db: Session, company_id: int, employee_id: int, on: date) -> ledger.LedgerConfig:
    """Platform-default ledger config overlaid with the employee's resolved switches (no policy fields)."""
    from app.payroll.services import settings as settings_engine
    from app.payroll.settings_registry import UNSET

    keys = ("payroll.attendance.enabled", "payroll.leave.enabled", "payroll.leave.when_disabled_treat_approved_leave_as",
            "payroll.attendance.half_day_value", "payroll.lop.sandwich_rule", "payroll.lop.zero_paid_days")
    try:
        values = {k: r.value for k, r in settings_engine.resolve_many(
            db, company_id, keys, employee_id=employee_id, on=on, strict=False).items()}
    except Exception:
        values = {}

    def pick(key: str, default: Any) -> Any:
        value = values.get(key, UNSET)
        return default if value is UNSET or value is None else value

    return ledger.LedgerConfig(
        attendance_enabled=bool(pick("payroll.attendance.enabled", False)),
        leave_enabled=bool(pick("payroll.leave.enabled", False)),
        when_disabled_treat_approved_leave_as=pick("payroll.leave.when_disabled_treat_approved_leave_as", "PAID_EXCUSED"),
        half_day_value=Decimal(str(pick("payroll.attendance.half_day_value", "0.5"))),
        sandwich_rule=pick("payroll.lop.sandwich_rule", "OFF"),
        zero_paid_days=pick("payroll.lop.zero_paid_days", "EXCEPTION"),
    )


def day_ledger_preview(db: Session, principal: Principal, employee_id: int, start: date, end: date) -> dict:
    """The ledger inputs of a range and the ledger they produce with the employee's switches (3.4 §4.4)."""
    if not principal.has(INPUTS_READ):
        raise forbidden()
    _dates(start, end)
    _ensure_in_scope(db, principal, INPUTS_READ, employee_id, start, end)
    from app.payroll.engine.snapshot import day_to_dict

    days = ledger_inputs(db, principal.company_id, employee_id, start, end)
    cfg = _preview_config(db, principal.company_id, employee_id, end)
    result = ledger.build_ledger(days, cfg)
    return {"employee_id": employee_id, "start": start.isoformat(), "end": end.isoformat(),
            "inputs": [day_to_dict(d) for d in days], "ledger": result.to_dict(),
            "pending": pending_inputs(db, principal.company_id, employee_id, start, end),
            "config": {"attendance_enabled": cfg.attendance_enabled, "leave_enabled": cfg.leave_enabled}}

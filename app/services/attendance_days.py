"""The evaluation write path (AS 04-attendance/03 §2.1, §8.3, §9; spine §5.7, §5.11; AS 11 §4.2–§4.3).

``load_inputs_many`` resolves everything ``day_evaluation.evaluate_day`` reads for up to 500 employees × 31 days
(LH day facts and ``expected_presence``, ``shift_resolution``, non-voided sessions with breaks, approved on-duty,
the attendance and payroll policies, settings). ``evaluate_range`` runs the pure evaluator and writes
``attendance_days`` idempotently:

1. employees are locked in ascending id (``attendance_service.lock_employee``) before any row lock (LCK-02);
2. an unchanged ``inputs_hash`` writes nothing;
3. while a row is ``OPEN`` it is updated in place (no version row, no change log); the first write after the close
   point writes version 1 (every output) — a decision trigger on an OPEN day writes the version row but no change log;
4. later output changes bump ``version`` and append the changed outputs (``attendance_day_versions``);
5. ``log_change('attendance_day')`` is written in ``NORMAL`` mode only, for rows past their close point, with the row
   ``content_hash`` (the hash payroll snapshots store, spine §5.7). ``SHADOW`` (reconciliation) writes rows and
   versions only, so shadow rows never stale a run (AS 05 §7.3).

Services take ``(db, …)`` and never commit; routes and jobs commit. Attendance publishes facts only: nothing here
prices time (payroll reads the rows through ``DAY_RESULT_TO_LEDGER``, JC-AS-01).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, bad_request
from app.models.attendance import Attendance, AttendanceDay, AttendanceDayVersion
from app.models.employee import Employee
from app.models.time_inputs import OnDutyRequest
from app.payroll.services import time_inputs as ti
from app.services import attendance_access, attendance_policies, attendance_service
from app.services import day_evaluation as de
from app.services import shift_engine as engine
from app.services import shift_resolution

MAX_EMPLOYEES = 500
MAX_DAYS = 31
MAX_RECOMPUTE_DAYS = 62
WRITE_MODES = de.EVALUATION_WRITE_MODES
RULES = "ATTENDANCE_RULES_POLICY"
# Triggers that carry a decision: on an OPEN day they still write a version row (decision lineage, AS-REL-02).
DECISION_TRIGGERS = ("REQUEST_DECIDED", "ON_DUTY_DECIDED", "LEAVE_CHANGED")
SETTING_KEYS = ("attendance.grace_minutes", "attendance.night_window", "attendance.day_close.buffer_minutes",
                "attendance.capture.enabled", "payroll.overtime.enabled", "payroll.holiday_work_premium.enabled",
                "payroll.weekly_off_pay.enabled")


@dataclass
class WriteResult:
    evaluated: int = 0
    written: int = 0
    unchanged: int = 0
    versions: int = 0
    changes_logged: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


# --------------------------------------------------------------------------- loading


def _check_range(employee_ids: Sequence[int], start: date, end: date, max_days: int = MAX_DAYS) -> List[date]:
    if end < start:
        raise bad_request("The end date is before the start date.", "INVALID_RANGE")
    days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    if len(days) > max_days or len(set(employee_ids)) > MAX_EMPLOYEES:
        raise bad_request(f"Choose at most {MAX_EMPLOYEES} employees and {max_days} days.", "RANGE_TOO_LONG")
    return days


def _night_window(value) -> Tuple[str, str]:
    """``attendance.night_window`` JSON -> (start, end); a malformed value falls back to 22:00–06:00."""
    try:
        start, end = str(value["start"]), str(value["end"])
        for part in (start, end):
            hour, minute = (int(x) for x in part.split(":"))
            if not (0 <= hour < 24 and 0 <= minute < 60):
                raise ValueError(part)
        return start, end
    except (TypeError, KeyError, ValueError, AttributeError):
        return "22:00", "06:00"


class _PerEmployee:
    """Policy and setting resolution per (employee, date): resolved at the first and last date of the range, and
    per date only for employees whose answer differs between the two (a mid-range change)."""

    def __init__(self, days: Sequence[date], resolve) -> None:
        self.days = list(days)
        self.resolve = resolve
        self.cache: Dict[Tuple[int, date], object] = {}

    def get(self, employee_id: int, day: date):
        key = (employee_id, day)
        if key in self.cache:
            return self.cache[key]
        first, last = self.days[0], self.days[-1]
        for edge in (first, last):
            if (employee_id, edge) not in self.cache:
                self.cache[(employee_id, edge)] = self.resolve(employee_id, edge)
        if self.cache[(employee_id, first)] == self.cache[(employee_id, last)]:
            for d in self.days:
                self.cache.setdefault((employee_id, d), self.cache[(employee_id, first)])
        else:
            self.cache[key] = self.resolve(employee_id, day)
        return self.cache[key]


def ti_policies():
    from app.payroll.services import policies  # lazy: payroll services import attendance services

    return policies


def _attendance_rules(db: Session, company_id: int, employee_id: int, day: date, v2: bool):
    """(rules_mode, RulesParams, {type: policy_version_id}): STANDARD only for v2 tenants with a STANDARD rules
    version (V-AS-02); without a resolved rules version the day is evaluated as LEGACY_V1 (the zero-diff port)."""
    found = attendance_policies.explain(db, company_id, RULES, employee_id, day)
    if found.get("status") != "RESOLVED":
        return de.LEGACY_V1, de.RulesParams(), {}
    version = found["version"]
    fields = dict(version.get("fields") or {})
    ids = {RULES: int(version["id"])}
    params = de.RulesParams.from_fields(fields)
    if not v2 or (fields.get("profile") or "STANDARD") != "STANDARD":
        return de.LEGACY_V1, params, ids
    return de.STANDARD, params, ids


def _company_zone(db: Session, company_id: int) -> str:
    from app.models.company import Company

    company = db.get(Company, company_id)
    return (company.timezone if company is not None else None) or "UTC"


def _legacy_params(db: Session, company_id: int, employee_id: int, day: date) -> de.LegacyPayParams:
    """Payroll's own ``ATTENDANCE_PAY_POLICY`` / ``LATE_PENALTY_POLICY`` fields (read, never copied; §2.4)."""
    pay = ti_policies().resolve_for_employee(db, company_id, "ATTENDANCE_PAY_POLICY", employee_id, day)
    late = ti_policies().resolve_for_employee(db, company_id, "LATE_PENALTY_POLICY", employee_id, day)
    pf = (pay.get("version") or {}).get("fields") or {} if pay.get("status") == "RESOLVED" else {}
    lf = (late.get("version") or {}).get("fields") or {} if late.get("status") == "RESOLVED" else {}
    return de.LegacyPayParams(
        derive_status_from_hours=bool(pf.get("derive_status_from_hours", False)),
        full_day_min_hours=Decimal(str(pf.get("full_day_min_hours", "8.0"))),
        half_day_min_hours=Decimal(str(pf.get("half_day_min_hours", "4.0"))),
        early_exit_grace_minutes=int(lf.get("early_exit_grace_minutes", 15)))


def _overtime_view(db: Session, company_id: int, employee_id: int, day: date, values: Mapping) -> de.OvertimeView:
    if not values.get("payroll.overtime.enabled"):
        return de.OvertimeView()
    from app.payroll.time_pay_overtime import parse_overtime_policy

    found = ti_policies().resolve_for_employee(db, company_id, "OVERTIME_POLICY", employee_id, day)
    fields = (found.get("version") or {}).get("fields") or {} if found.get("status") == "RESOLVED" else {}
    try:
        rules = parse_overtime_policy(fields)
        threshold = int(rules.daily_threshold * 60) if rules.daily_threshold is not None else None
        daily = rules.threshold_combination in ("DAILY", "DAILY_THEN_WEEKLY")
    except Exception:  # an invalid payroll policy is payroll's exception, never a reason to stop evaluating
        threshold, daily = None, True
    return de.OvertimeView(enabled=True, daily_applies=daily, daily_threshold_minutes=threshold,
                           holiday_work_premium=bool(values.get("payroll.holiday_work_premium.enabled")),
                           weekly_off_pay=bool(values.get("payroll.weekly_off_pay.enabled")))


def _settings(db: Session, company_id: int, employee_id: int, day: date) -> dict:
    from app.payroll.services import settings as settings_service

    resolved = settings_service.resolve_many(db, company_id, SETTING_KEYS, employee_id=employee_id, on=day,
                                             strict=False)
    return {key: getattr(item, "value", None) for key, item in resolved.items()}


def load_inputs_many(db: Session, company_id: int, employee_ids: Sequence[int], start: date, end: date, *,
                     now: datetime, v2: Optional[bool] = None) -> Dict[Tuple[int, date], de.DayInputs]:
    """Inputs of every (employee, date) of ``start..end`` (§2.1 batch loader; spine §5.11)."""
    days = _check_range(employee_ids, start, end)
    ids = sorted(set(employee_ids))
    if not ids:
        return {}
    v2 = attendance_access.v2_enabled(db, company_id) if v2 is None else v2
    owned = "lh_graph_memo" not in db.info
    if owned:
        db.info["lh_graph_memo"] = {}
    try:
        facts = ti.day_facts_many(db, company_id, ids, start, end)
        expectations = ti.expected_presence(db, company_id, ids, start, end)
    finally:
        if owned:
            db.info.pop("lh_graph_memo", None)
    shifts = shift_resolution.shifts_for_many(db, company_id, ids, start, end)
    records = (db.query(Attendance)
               .filter(Attendance.company_id == company_id, Attendance.employee_id.in_(ids),
                       Attendance.date >= start, Attendance.date <= end, Attendance.voided_at.is_(None))
               .order_by(Attendance.employee_id, Attendance.date, Attendance.check_in_time).all())
    breaks = attendance_service.breaks_by_session(db, [r.id for r in records])
    sessions: Dict[Tuple[int, date], List[de.SessionData]] = defaultdict(list)
    for r in records:
        sessions[(r.employee_id, r.date)].append(de.SessionData(
            id=r.id, check_in_at=r.check_in_time, check_out_at=r.check_out_time, closed_at=r.closed_at,
            breaks=tuple(de.BreakData(b.id, b.kind, b.started_at, b.ended_at, bool(b.is_paid)) for b in breaks[r.id]),
            content_hash=attendance_service.content_hash(r, breaks[r.id]), capture_source=r.capture_source,
            work_mode=r.work_mode, legacy_status=r.status, late_minutes=r.late_minutes,
            early_exit=bool(r.early_exit), early_exit_minutes=r.early_exit_minutes))
    on_duty: Dict[Tuple[int, date], List[de.OnDutyData]] = defaultdict(list)
    for row in (db.query(OnDutyRequest)
                .filter(OnDutyRequest.company_id == company_id, OnDutyRequest.employee_id.in_(ids),
                        OnDutyRequest.date >= start, OnDutyRequest.date <= end, OnDutyRequest.status == "APPROVED")):
        on_duty[(row.employee_id, row.date)].append(de.OnDutyData(
            row.id, getattr(row, "kind", None) or "ON_DUTY", row.portion or "FULL", row_version=row.row_version or 1))

    settings = _PerEmployee(days, lambda e, d: _settings(db, company_id, e, d))
    rules = _PerEmployee(days, lambda e, d: _attendance_rules(db, company_id, e, d, v2))
    legacy = _PerEmployee(days, lambda e, d: _legacy_params(db, company_id, e, d))
    capture = _PerEmployee(days, lambda e, d: _capture(db, company_id, e, d))

    company_zone = _company_zone(db, company_id)
    result: Dict[Tuple[int, date], de.DayInputs] = {}
    for employee_id in ids:
        exp_by_day = {x.date: x for x in expectations.get(employee_id, [])}
        fact_by_day = {f.date: f for f in facts.get(employee_id, [])}
        for day in days:
            fact = fact_by_day.get(day)
            exp = exp_by_day.get(day)
            resolved = shifts.get((employee_id, day))
            zone = (resolved.work_timezone if resolved is not None else None) or (fact.timezone if fact else None) \
                or company_zone
            instances: Tuple[engine.ShiftInstance, ...] = ()
            if resolved is not None:
                try:
                    built = engine.build_instance(resolved.version, day, resolved.work_timezone)
                    instances = (replace(built, shift_id=resolved.shift_id, shift_version_id=resolved.shift_version_id,
                                         roster_entry_id=resolved.roster_entry_id),)
                except engine.ShiftEngineError:
                    instances = ()
            values = settings.get(employee_id, day)
            mode, params, policy_ids = rules.get(employee_id, day)
            window = _night_window(values.get("attendance.night_window"))
            cap = capture.get(employee_id, day)
            result[(employee_id, day)] = de.DayInputs(
                employee_id=employee_id, shift_date=day, work_timezone=zone, now=now,
                expectation=de.ExpectationData(
                    exp.expected_portion if exp else "NONE", exp.expected_minutes if exp else 0,
                    tuple(exp.reasons) if exp else ("NOT_EMPLOYED",), exp.calendar_version if exp else None,
                    fact.work_source if fact else None),
                instances=instances,
                shift_source=resolved.source if resolved is not None else "NONE",
                shift_grace_in_minutes=resolved.version.grace_in_minutes if resolved is not None else None,
                shift_grace_out_minutes=resolved.version.grace_out_minutes if resolved is not None else None,
                scheduled_minutes=fact.scheduled_minutes if fact else 0,
                legal_entity_id=fact.legal_entity_id if fact else None,
                branch_id=fact.branch_id if fact else None,
                sessions=tuple(sessions.get((employee_id, day), ())),
                on_duty=tuple(on_duty.get((employee_id, day), ())),
                rules_mode=mode, rules=params, legacy=legacy.get(employee_id, day),
                settings=de.EvalSettings(
                    grace_minutes=int(values.get("attendance.grace_minutes") or 0)
                    if values.get("attendance.grace_minutes") is not None else 15,
                    night_window_start=window[0], night_window_end=window[1],
                    day_close_buffer_minutes=int(values.get("attendance.day_close.buffer_minutes") or 240)
                    if values.get("attendance.day_close.buffer_minutes") is not None else 240,
                    capture_enabled=values.get("attendance.capture.enabled") is not False),
                ot_view=_overtime_view(db, company_id, employee_id, day, values),
                capture_required=cap[0], shift_required=cap[1],
                policy_version_ids=policy_ids,
            )
    return result


def _capture(db: Session, company_id: int, employee_id: int, day: date) -> Tuple[bool, bool]:
    from app.services import attendance_capture_policy

    policy = attendance_capture_policy.effective(db, company_id, employee_id, day)
    return bool(policy.capture_required), bool(policy.shift_required)


# --------------------------------------------------------------------------- writing


def _row_outputs(row: AttendanceDay) -> dict:
    return {name: de._plain(getattr(row, name)) for name in de.OUTPUT_COLUMNS}


def _apply(row: AttendanceDay, ev: de.DayEvaluation) -> None:
    for name in de.OUTPUT_COLUMNS:
        value = getattr(ev, name)
        if name in ("exception_codes", "pending_reasons"):
            value = list(value)
        setattr(row, name, value)


def evaluate_range(db: Session, company_id: int, employee_ids: Sequence[int], start: date, end: date, *,
                   trigger: str, mode: str = "NORMAL", now: Optional[datetime] = None,
                   actor_user_id: Optional[int] = None) -> WriteResult:
    """Evaluate and write every (employee, date) of the range (§8.3). The caller commits."""
    if trigger not in de.EVALUATION_TRIGGERS:
        raise bad_request(f"Unknown evaluation trigger {trigger!r}.", "INVALID_TRIGGER")
    if mode not in WRITE_MODES:
        raise bad_request(f"Unknown write mode {mode!r}.", "INVALID_WRITE_MODE")
    ids = sorted(set(employee_ids))
    _check_range(ids, start, end)
    now = (now or clock.utcnow()).astimezone(timezone.utc).replace(microsecond=0)
    for employee_id in ids:  # LCK-02: every employee lock before any row lock, ascending
        attendance_service.lock_employee(db, employee_id)
    inputs = load_inputs_many(db, company_id, ids, start, end, now=now)
    existing = {(r.employee_id, r.shift_date): r for r in (
        db.query(AttendanceDay).filter(AttendanceDay.company_id == company_id, AttendanceDay.employee_id.in_(ids),
                                       AttendanceDay.shift_date >= start, AttendanceDay.shift_date <= end)
        .with_for_update().all())}
    versioned = {day_id: version for day_id, version in (
        db.query(AttendanceDayVersion.day_id, func.max(AttendanceDayVersion.version))
        .filter(AttendanceDayVersion.company_id == company_id,
                AttendanceDayVersion.day_id.in_([r.id for r in existing.values()] or [-1]))
        .group_by(AttendanceDayVersion.day_id).all())}
    result = WriteResult()
    for key in sorted(inputs):
        ev = de.evaluate_day(inputs[key])
        result.evaluated += 1
        row = existing.get(key)
        _write_one(db, company_id, row, ev, versioned, result, trigger=trigger, mode=mode, now=now,
                   actor_user_id=actor_user_id)
    db.flush()
    return result


def _write_one(db: Session, company_id: int, row: Optional[AttendanceDay], ev: de.DayEvaluation,
               versioned: Dict[int, int], result: WriteResult, *, trigger: str, mode: str, now: datetime,
               actor_user_id: Optional[int]) -> None:
    new_outputs = ev.outputs()
    decided = ev.evaluation_state != de.OPEN
    if row is not None and row.inputs_hash == ev.inputs_hash:
        result.unchanged += 1
        return
    if row is None:
        row = AttendanceDay(company_id=company_id, employee_id=ev.employee_id, shift_date=ev.shift_date, version=1,
                            row_version=1, inputs_hash=ev.inputs_hash, evaluated_at=now,
                            evaluated_by="USER" if actor_user_id else "SYSTEM", trigger=trigger)
        _apply(row, ev)
        db.add(row)
        db.flush()
        result.written += 1
        if decided or trigger in DECISION_TRIGGERS:
            _version(db, company_id, row, 1, new_outputs, ev, trigger, actor_user_id, result)
            versioned[row.id] = 1
            if decided:
                _log(db, company_id, row, "CREATED", mode, result)
        return
    old_outputs = _row_outputs(row)
    changed = de.changed_outputs(old_outputs, new_outputs)
    was_decided = row.evaluation_state != de.OPEN
    row.inputs_hash = ev.inputs_hash
    row.evaluated_at = now
    row.evaluated_by = "USER" if actor_user_id else "SYSTEM"
    row.trigger = trigger
    row.row_version = (row.row_version or 1) + 1
    result.written += 1
    if not changed:
        return
    _apply(row, ev)
    if not decided and trigger not in DECISION_TRIGGERS:
        return  # OPEN: in place, no version row, no change log (§2.5)
    last = versioned.get(row.id)
    if last is None:
        version, outputs = row.version or 1, new_outputs
    else:
        version, outputs = last + 1, changed
    row.version = version
    _version(db, company_id, row, version, outputs, ev, trigger, actor_user_id, result)
    versioned[row.id] = version
    if decided or was_decided:
        _log(db, company_id, row, "CREATED" if last is None else "UPDATED", mode, result)


def _version(db: Session, company_id: int, row: AttendanceDay, version: int, outputs: dict, ev: de.DayEvaluation,
             trigger: str, actor_user_id: Optional[int], result: WriteResult) -> None:
    db.add(AttendanceDayVersion(company_id=company_id, day_id=row.id, version=version, outputs=outputs,
                                inputs_hash=ev.inputs_hash, trigger=trigger, actor_user_id=actor_user_id))
    result.versions += 1


def _log(db: Session, company_id: int, row: AttendanceDay, change: str, mode: str, result: WriteResult) -> None:
    """JC-AS-11b: the change payroll detection reads; never in SHADOW mode (AS 05 §7.3)."""
    if mode != "NORMAL":
        return
    ti.log_change(db, company_id, record_type="attendance_day", record_id=row.id, change=change,
                  content={"content_hash": row.content_hash, "version": row.version},
                  employee_id=row.employee_id, date_from=row.shift_date, date_to=row.shift_date,
                  details={"version": row.version, "content_hash": row.content_hash})
    result.changes_logged += 1


# --------------------------------------------------------------------------- triggers


def evaluate_dates(db: Session, company_id: int, employee_id: int, dates: Iterable[date], *, trigger: str,
                   actor_user_id: Optional[int] = None, now: Optional[datetime] = None) -> Optional[WriteResult]:
    """Fact triggers (punch, session closed, decisions) for one employee: v2 tenants only; v1 tenants keep the
    sessions as the payroll input and get no rows outside reconciliation."""
    picked = sorted(set(dates))
    if not picked or not attendance_access.v2_enabled(db, company_id):
        return None
    return evaluate_range(db, company_id, [employee_id], picked[0], picked[-1], trigger=trigger, now=now,
                          actor_user_id=actor_user_id)


def day_close(db: Session, company_id: int, *, now: Optional[datetime] = None, lookback_days: int = 2) -> dict:
    """``attendance.day_close`` evaluation part (§9): every employed (E, D) of the last ``lookback_days`` dates plus
    today, and the PROVISIONAL rows of the last 31 days (device grace, A-37). v2 tenants only."""
    if not attendance_access.v2_enabled(db, company_id):
        return {"evaluated": 0, "skipped": "v1"}
    now = (now or clock.utcnow()).astimezone(timezone.utc).replace(microsecond=0)
    today = clock.company_today(db, company_id)
    start = today - timedelta(days=lookback_days)
    employees = [e for (e,) in db.query(Employee.id).filter(Employee.company_id == company_id)
                 .order_by(Employee.id).all()]
    totals = WriteResult()
    for index in range(0, len(employees), MAX_EMPLOYEES):
        chunk = employees[index:index + MAX_EMPLOYEES]
        part = evaluate_range(db, company_id, chunk, start, today, trigger="DAY_CLOSE", now=now)
        for name, value in part.as_dict().items():
            setattr(totals, name, getattr(totals, name) + value)
    provisional = (db.query(AttendanceDay.employee_id, func.min(AttendanceDay.shift_date))
                   .filter(AttendanceDay.company_id == company_id, AttendanceDay.evaluation_state == de.PROVISIONAL,
                           AttendanceDay.shift_date < start, AttendanceDay.shift_date >= today - timedelta(days=MAX_DAYS))
                   .group_by(AttendanceDay.employee_id).all())
    for employee_id, first in provisional:
        part = evaluate_range(db, company_id, [employee_id], first, start - timedelta(days=1), trigger="DAY_CLOSE",
                              now=now)
        for name, value in part.as_dict().items():
            setattr(totals, name, getattr(totals, name) + value)
    return totals.as_dict()


# --------------------------------------------------------------------------- reads


def get_day(db: Session, company_id: int, employee_id: int, day: date) -> Optional[AttendanceDay]:
    return (db.query(AttendanceDay).filter(AttendanceDay.company_id == company_id,
                                           AttendanceDay.employee_id == employee_id,
                                           AttendanceDay.shift_date == day).first())


def versions_of(db: Session, company_id: int, day_id: int) -> List[AttendanceDayVersion]:
    return (db.query(AttendanceDayVersion).filter(AttendanceDayVersion.company_id == company_id,
                                                  AttendanceDayVersion.day_id == day_id)
            .order_by(AttendanceDayVersion.version).all())


def days_for(db: Session, company_id: int, employee_ids: Sequence[int], start: date, end: date
             ) -> Dict[Tuple[int, date], AttendanceDay]:
    """Stored evaluations of a range (payroll's v2 read, AS 05 §4); GET paths never evaluate (BUG-AS-70)."""
    rows = (db.query(AttendanceDay).filter(AttendanceDay.company_id == company_id,
                                           AttendanceDay.employee_id.in_(list(employee_ids) or [-1]),
                                           AttendanceDay.shift_date >= start, AttendanceDay.shift_date <= end).all())
    return {(r.employee_id, r.shift_date): r for r in rows}


def as_json(row: AttendanceDay, *, versions: Sequence[AttendanceDayVersion] = ()) -> dict:
    body = {"id": row.id, "employee_id": row.employee_id, "shift_date": row.shift_date.isoformat(),
            "version": row.version, "row_version": row.row_version, "inputs_hash": row.inputs_hash,
            "evaluated_at": de._plain(row.evaluated_at), "evaluated_by": row.evaluated_by, "trigger": row.trigger,
            **_row_outputs(row)}
    if versions:
        body["history"] = [{"version": v.version, "trigger": v.trigger, "created_at": de._plain(v.created_at),
                            "actor_user_id": v.actor_user_id, "changed": sorted(v.outputs)} for v in versions]
    return body


def explain(db: Session, company_id: int, employee_id: int, day: date, *, now: Optional[datetime] = None) -> dict:
    """What the evaluator would answer now (preview path = real path; never written)."""
    now = (now or clock.utcnow()).astimezone(timezone.utc).replace(microsecond=0)
    inputs = load_inputs_many(db, company_id, [employee_id], day, day, now=now)[(employee_id, day)]
    ev = de.evaluate_day(inputs)
    return {"close_point": de._plain(de.close_point(inputs)), "phase": de.phase(inputs),
            "rules_mode": inputs.rules_mode, "expected_portion": inputs.expectation.expected_portion,
            "reasons": list(inputs.expectation.reasons), "instances": [
                {"kind": i.kind, "start": de._plain(i.start_at), "end": de._plain(i.end_at),
                 "shift_version_id": i.shift_version_id} for i in inputs.instances],
            "evaluation": ev.outputs(), "net_closed_seconds": ev.net_closed_seconds}


def recompute(db: Session, company_id: int, employee_ids: Sequence[int], start: date, end: date, *,
              actor_user_id: Optional[int], now: Optional[datetime] = None) -> dict:
    """``MANUAL_RECOMPUTE`` over ≤ 62 days, chunked by 31 days (§8.1)."""
    if end < start:
        raise bad_request("The end date is before the start date.", "INVALID_RANGE")
    if (end - start).days + 1 > MAX_RECOMPUTE_DAYS:
        raise AppError(400, f"Recompute at most {MAX_RECOMPUTE_DAYS} days at a time.", "RANGE_TOO_LONG")
    totals = WriteResult()
    cursor = start
    while cursor <= end:
        stop = min(end, cursor + timedelta(days=MAX_DAYS - 1))
        part = evaluate_range(db, company_id, employee_ids, cursor, stop, trigger="MANUAL_RECOMPUTE", now=now,
                              actor_user_id=actor_user_id)
        for name, value in part.as_dict().items():
            setattr(totals, name, getattr(totals, name) + value)
        cursor = stop + timedelta(days=1)
    return totals.as_dict()

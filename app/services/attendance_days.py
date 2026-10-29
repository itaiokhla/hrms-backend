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

import json
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
from app.services import attendance_access, attendance_service
from app.services import day_evaluation as de
from app.services import shift_engine as engine
from app.services import shift_resolution

MAX_EMPLOYEES = 500
MAX_DAYS = 31
MAX_RECOMPUTE_DAYS = 62
WRITE_MODES = de.EVALUATION_WRITE_MODES
RULES = "ATTENDANCE_RULES_POLICY"
CAPTURE = "ATTENDANCE_CAPTURE_POLICY"
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
    skipped_locked: int = 0  # configuration triggers leave INPUT_LOCKED / FINALIZED dates alone (§8.5)

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


AMBIGUOUS = "AMBIGUOUS"  # a runtime priority tie (SP-AS-085): the day is PENDING with NOT_EVALUATED
RESOLVED_TYPES = ("ATTENDANCE_RULES_POLICY", "ATTENDANCE_REQUEST_POLICY", "SCHEDULING_POLICY",
                  "ATTENDANCE_CAPTURE_POLICY", "ATTENDANCE_PAY_POLICY", "LATE_PENALTY_POLICY", "OVERTIME_POLICY")


class _Resolver:
    """Batch policy and setting resolution of one chunk (spine §5.11; IG-AS-72).

    Policies: per type and date, the employees' placements (``policies.employee_attributes`` for all ids), the type's
    assignment rules and every candidate policy's versions are loaded once and matched in memory. Settings: resolved
    once per placement context (legal entity, branch, pay group) and date; employees holding their own
    employee-level rows of the keys (one query finds them) are resolved individually. Both answer at the first and
    last date of the range and per date only where the two differ, so the query count follows the number of policy
    types and placement contexts of the chunk, never its head count."""

    def __init__(self, db: Session, company_id: int, employee_ids: Sequence[int], days: Sequence[date],
                 placement) -> None:
        self.db, self.company_id, self.ids, self.days = db, company_id, list(employee_ids), list(days)
        self.placement = placement  # (employee_id, day) -> (legal_entity_id, branch_id, pay_group_id)
        self._attributes: Dict[date, dict] = {}
        self._rules: Optional[Dict[str, list]] = None
        self._versions: dict = {}
        self._by_date: Dict[Tuple[str, date], dict] = {}
        self._settings: Dict[tuple, dict] = {}
        from app.models.payroll_config import SettingValue

        # One read of the keys' rows: employee-level holders and whether any value changes inside the range.
        rows = (db.query(SettingValue.scope_type, SettingValue.scope_id, SettingValue.effective_from,
                         SettingValue.effective_to)
                .filter(SettingValue.company_id == company_id, SettingValue.key.in_(SETTING_KEYS),
                        SettingValue.status == "ACTIVE").all())
        first, last = self.days[0], self.days[-1]
        self._personal = {r.scope_id for r in rows if r.scope_type == "EMPLOYEE" and r.scope_id in set(self.ids)}
        self._settings_stable = not any(
            first < r.effective_from <= last or (r.effective_to is not None and first <= r.effective_to < last)
            for r in rows)
        # Placements: the edge dates differ only when an assignment starts or ends inside the range.
        self._placements_stable = all(placement(e, first) == placement(e, last) for e in self.ids)

    # -- policies
    def _attrs(self, day: date) -> dict:
        if day != self.days[0] and self._placements_stable:
            day = self.days[0]
        if day not in self._attributes:
            self._attributes[day] = {a.employee_id: a for a in ti_policies().employee_attributes(
                self.db, self.company_id, day, self.ids)}
        return self._attributes[day]

    def _for_date(self, type_code: str, day: date) -> dict:
        key = (type_code, day)
        if key in self._by_date:
            return self._by_date[key]
        policies = ti_policies()
        if self._rules is None:
            self._load_rules()
        from app.payroll import policy_types as pt

        found = {}
        for employee_id in self.ids:
            person = self._attrs(day).get(employee_id)
            if person is None:
                found[employee_id] = None
                continue
            result = pt.resolve_assignment(self._rules.get(type_code, []), person, on=day)
            if result.status == "AMBIGUOUS":
                found[employee_id] = AMBIGUOUS
            elif result.status != "RESOLVED":
                found[employee_id] = None
            else:
                versions = self._versions.get(int(result.rule.policy_id), [])
                found[employee_id] = policies._active_on(versions, day)
        self._by_date[key] = found
        return found

    def _load_rules(self) -> None:
        """The ACTIVE assignment rules of every type the evaluation reads, and their policies' versions."""
        from sqlalchemy import and_

        from app.models.payroll_config import Policy, PolicyAssignment
        from app.payroll import policy_types as pt

        rows = (self.db.query(PolicyAssignment, Policy.type)
                .join(Policy, and_(Policy.id == PolicyAssignment.policy_id,
                                   Policy.company_id == PolicyAssignment.company_id))
                .filter(PolicyAssignment.company_id == self.company_id, PolicyAssignment.status == "ACTIVE",
                        Policy.type.in_(RESOLVED_TYPES), Policy.status == "ACTIVE")
                .order_by(PolicyAssignment.id).all())
        self._rules = {}
        for r, type_code in rows:  # the same conversion as policies._type_rules
            self._rules.setdefault(type_code, []).append(pt.AssignmentRule(
                rule_id=r.id, policy_id=r.policy_id, priority=r.priority, criteria=r.criteria,
                effective_from=r.effective_from, effective_to=r.effective_to))
        ids = sorted({int(rule.policy_id) for rules in self._rules.values() for rule in rules})
        self._versions = ti_policies()._versions(self.db, self.company_id, ids)

    def version(self, type_code: str, employee_id: int, day: date):
        """The ACTIVE ``PolicyVersion`` (or None, or ``AMBIGUOUS``) of the type for the employee on ``day``."""
        first, last = self.days[0], self.days[-1]
        a = self._for_date(type_code, first).get(employee_id)
        b = self._for_date(type_code, last).get(employee_id)
        if _same(a, b):
            return a
        return self._for_date(type_code, day).get(employee_id)

    # -- settings
    def _settings_at(self, employee_id: int, day: date) -> dict:
        from app.payroll.services import settings as settings_service

        if employee_id in self._personal:
            key = ("employee", employee_id, day)
            kwargs = {"employee_id": employee_id}
        else:
            le, branch, group = self.placement(employee_id, day)
            key = ("context", le, branch, group, day)
            kwargs = {"legal_entity_id": le, "branch_id": branch, "pay_group_id": group}
        if key not in self._settings:
            if "employee_id" in kwargs:
                resolved = settings_service.resolve_many(self.db, self.company_id, SETTING_KEYS, on=day,
                                                         strict=False, **kwargs)
            else:
                # The scope chain of a placement context, built from rows already in the session (no derived
                # defaults among SETTING_KEYS, so no context facts are needed): two queries per context and date.
                from app.models.company import Company
                from app.models.org import LegalEntity
                from app.payroll import settings_registry as reg

                company = self.db.get(Company, self.company_id)
                entity = self.db.get(LegalEntity, kwargs["legal_entity_id"]) if kwargs["legal_entity_id"] else None
                ctx = settings_service.Context(
                    company_id=self.company_id, on=day, legal_entity_id=kwargs["legal_entity_id"],
                    branch_id=kwargs["branch_id"], pay_group_id=kwargs["pay_group_id"],
                    country_code=getattr(entity, "country_code", None),
                    partner_id=getattr(company, "partner_id", None))
                canonical = [reg.canonical_key(k) for k in SETTING_KEYS]
                found = settings_service.resolve_in_context(self.db, ctx, canonical, strict=False)
                resolved = {name: found[key] for name, key in zip(SETTING_KEYS, canonical)}
            self._settings[key] = {k: getattr(item, "value", None) for k, item in resolved.items()}
        return self._settings[key]

    def settings(self, employee_id: int, day: date) -> dict:
        first, last = self.days[0], self.days[-1]
        if self._settings_stable and self.placement(employee_id, first) == self.placement(employee_id, last):
            return self._settings_at(employee_id, first)
        a, b = self._settings_at(employee_id, first), self._settings_at(employee_id, last)
        return a if a == b else self._settings_at(employee_id, day)


def _same(a, b) -> bool:
    if a is None or b is None or a == AMBIGUOUS or b == AMBIGUOUS:
        return a == b
    return a.id == b.id


def _fields(version) -> dict:
    return dict(version.fields or {}) if version is not None and version != AMBIGUOUS else {}


def ti_policies():
    from app.payroll.services import policies  # lazy: payroll services import attendance services

    return policies


MODE_TYPES = (RULES, "ATTENDANCE_REQUEST_POLICY", "SCHEDULING_POLICY")  # capture never switches a day (02 §4.1)


def _rules_mode(versions: Mapping[str, object], v2: bool):
    """(rules_mode, RulesParams, {type: policy_version_id}, tie) — spine §5.6: LEGACY_V1 when every resolved version
    of the rules, request and scheduling types is LEGACY_V1 (or none resolves: the zero-diff port), else STANDARD,
    and STANDARD only on v2 tenants (V-AS-02). A LEGACY_V1 rules version inside a STANDARD day contributes its fields
    over the STANDARD defaults (mixed sets, AS-POL-03). A priority tie of any of the three is reported (SP-AS-085)."""
    ids: Dict[str, int] = {}
    profiles: List[str] = []
    tie = False
    for type_code in MODE_TYPES:
        version = versions.get(type_code)
        if version == AMBIGUOUS:
            tie = True
            continue
        if version is None:
            continue
        ids[type_code] = int(version.id)
        profiles.append(str((version.fields or {}).get("profile") or "STANDARD"))
    params = de.RulesParams.from_fields(_fields(versions.get(RULES)))
    standard = v2 and any(profile == "STANDARD" for profile in profiles)
    return (de.STANDARD if standard else de.LEGACY_V1), params, ids, tie


def _instance(resolved, day: date, rules_fields: Mapping) -> engine.ShiftInstance:
    """The instance of a resolved shift. A roster entry with custom times and no shift (``shift_id`` 0) takes the
    breaks of ``ATTENDANCE_RULES_POLICY.break_defaults`` on STANDARD days (03-shifts/01 §5.4; a window outside the
    block is expected as punched); LEGACY_V1 days keep today's custom block without breaks (zero diff)."""
    defaults = tuple(rules_fields.get("break_defaults") or ())
    if resolved.shift_id or not defaults:
        return engine.build_instance(resolved.version, day, resolved.work_timezone)
    entry = engine.custom_entry(resolved.version, roster_entry_id=resolved.roster_entry_id, break_defaults=defaults)
    return engine.build_instance(None, day, resolved.work_timezone, entry)


def _company_zone(db: Session, company_id: int) -> str:
    from app.models.company import Company

    company = db.get(Company, company_id)
    return (company.timezone if company is not None else None) or "UTC"


def _legacy_from(pay: Mapping, late: Mapping) -> de.LegacyPayParams:
    """Payroll's own ``ATTENDANCE_PAY_POLICY`` / ``LATE_PENALTY_POLICY`` fields (read, never copied; §2.4)."""
    return de.LegacyPayParams(
        derive_status_from_hours=bool(pay.get("derive_status_from_hours", False)),
        full_day_min_hours=Decimal(str(pay.get("full_day_min_hours", "8.0"))),
        half_day_min_hours=Decimal(str(pay.get("half_day_min_hours", "4.0"))),
        early_exit_grace_minutes=int(late.get("early_exit_grace_minutes", 15)))


def _legacy_params(db: Session, company_id: int, employee_id: int, day: date) -> de.LegacyPayParams:
    """``_legacy_from`` for one employee and date (the cut-over comparison)."""
    resolver = _Resolver(db, company_id, [employee_id], [day], lambda e, d: (None, None, None))
    return _legacy_from(_fields(resolver.version("ATTENDANCE_PAY_POLICY", employee_id, day)),
                        _fields(resolver.version("LATE_PENALTY_POLICY", employee_id, day)))


def _overtime_view(overtime_fields: Mapping, values: Mapping) -> de.OvertimeView:
    if not values.get("payroll.overtime.enabled"):
        return de.OvertimeView()
    from app.payroll.time_pay_overtime import parse_overtime_policy

    try:
        rules = parse_overtime_policy(dict(overtime_fields))
        threshold = int(rules.daily_threshold * 60) if rules.daily_threshold is not None else None
        daily = rules.threshold_combination in ("DAILY", "DAILY_THEN_WEEKLY")
    except Exception:  # an invalid payroll policy is payroll's exception, never a reason to stop evaluating
        threshold, daily = None, True
    return de.OvertimeView(enabled=True, daily_applies=daily, daily_threshold_minutes=threshold,
                           holiday_work_premium=bool(values.get("payroll.holiday_work_premium.enabled")),
                           weekly_off_pay=bool(values.get("payroll.weekly_off_pay.enabled")))


def _capture(capture_version) -> Tuple[bool, bool]:
    """(capture_required, shift_required) of the capture policy; LEGACY_V1 or none = the current behaviour (punching
    required, no shift required)."""
    fields = _fields(capture_version)
    if not fields or (fields.get("profile") or "STANDARD") == "LEGACY_V1":
        return True, False
    return bool(fields.get("capture_required", True)), bool(fields.get("shift_required", False))


def _attested_sessions(db: Session, company_id: int, session_ids: Sequence[int]) -> set:
    """Sessions whose punch-out attested "no break taken" (``attendance_events.details``, step 5 (d))."""
    if not session_ids:
        return set()
    from app.models.attendance import AttendanceEvent

    rows = (db.query(AttendanceEvent.attendance_id, AttendanceEvent.details)
            .filter(AttendanceEvent.company_id == company_id, AttendanceEvent.attendance_id.in_(list(session_ids)),
                    AttendanceEvent.event_type == "PUNCH_OUT", AttendanceEvent.details.like('%"no_break_taken"%'))
            .all())
    found = set()
    for attendance_id, details in rows:
        try:
            if json.loads(details or "{}").get("no_break_taken") is True:
                found.add(attendance_id)
        except ValueError:
            continue
    return found


def scheduling_fields(db: Session, company_id: int, employee_ids: Sequence[int], on: date) -> Dict[int, dict]:
    """The resolved ``SCHEDULING_POLICY`` fields per employee on ``on`` ({} when none resolves), in one batch — the
    roster validator's tenant limits (IG-AS-79)."""
    ids = sorted(set(employee_ids))
    if not ids:
        return {}
    resolver = _Resolver(db, company_id, ids, [on], lambda e, d: (None, None, None))
    return {e: _fields(resolver.version("SCHEDULING_POLICY", e, on)) for e in ids}


def _int_setting(values: Mapping, key: str, default: int) -> int:
    value = values.get(key)
    return int(value) if value is not None else default


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
    batch: dict = {}
    try:
        facts = ti.day_facts_many(db, company_id, ids, start, end, batch_out=batch)
        expectations = ti.expected_presence(db, company_id, ids, start, end, facts=facts)
    finally:
        if owned:
            db.info.pop("lh_graph_memo", None)
    shifts = batch.get("shifts")
    if shifts is None:  # light day facts never resolve shifts
        shifts = shift_resolution.shifts_for_many(db, company_id, ids, start, end)
    # The day before the range too: its last OUT is the rest input of the first date (step 17).
    records = (db.query(Attendance)
               .filter(Attendance.company_id == company_id, Attendance.employee_id.in_(ids),
                       Attendance.date >= start - timedelta(days=1), Attendance.date <= end,
                       Attendance.voided_at.is_(None))
               .order_by(Attendance.employee_id, Attendance.date, Attendance.check_in_time).all())
    last_out: Dict[Tuple[int, date], datetime] = {}
    for r in records:
        if r.check_out_time is not None:
            key = (r.employee_id, r.date)
            last_out[key] = max(last_out.get(key, r.check_out_time), r.check_out_time)
    breaks = attendance_service.breaks_by_session(db, [r.id for r in records])
    attested = _attested_sessions(db, company_id, [r.id for r in records])
    sessions: Dict[Tuple[int, date], List[de.SessionData]] = defaultdict(list)
    for r in records:
        sessions[(r.employee_id, r.date)].append(de.SessionData(
            id=r.id, check_in_at=r.check_in_time, check_out_at=r.check_out_time, closed_at=r.closed_at,
            breaks=tuple(de.BreakData(b.id, b.kind, b.started_at, b.ended_at, bool(b.is_paid)) for b in breaks[r.id]),
            content_hash=attendance_service.content_hash(r, breaks[r.id]), capture_source=r.capture_source,
            work_mode=r.work_mode, legacy_status=r.status, late_minutes=r.late_minutes,
            early_exit=bool(r.early_exit), early_exit_minutes=r.early_exit_minutes,
            no_break_attested=r.id in attested))
    on_duty: Dict[Tuple[int, date], List[de.OnDutyData]] = defaultdict(list)
    for row in (db.query(OnDutyRequest)
                .filter(OnDutyRequest.company_id == company_id, OnDutyRequest.employee_id.in_(ids),
                        OnDutyRequest.date >= start, OnDutyRequest.date <= end, OnDutyRequest.status == "APPROVED")):
        on_duty[(row.employee_id, row.date)].append(de.OnDutyData(
            row.id, getattr(row, "kind", None) or "ON_DUTY", row.portion or "FULL",
            requires_punch=bool(getattr(row, "requires_punch", False)), row_version=row.row_version or 1))
    # AS-M4 requests (04-attendance/04 §4.2): open requests keep the day PROVISIONAL (REQUEST_PENDING); approved
    # MARK_EXCUSE and STATUS_OVERRIDE are inputs of steps 7 and 11 (the latest applied override wins).
    from app.models.attendance_requests import AttendanceRequest

    pending_days: set = set()
    excuses: Dict[Tuple[int, date], List[de.ExcuseData]] = defaultdict(list)
    overrides: Dict[Tuple[int, date], List[de.OverrideData]] = defaultdict(list)
    for req in (db.query(AttendanceRequest)
                .filter(AttendanceRequest.company_id == company_id, AttendanceRequest.employee_id.in_(ids),
                        AttendanceRequest.shift_date >= start, AttendanceRequest.shift_date <= end,
                        AttendanceRequest.status.in_(("PENDING", "RETURNED", "APPROVED")))
                .order_by(AttendanceRequest.applied_at.nullsfirst(), AttendanceRequest.id)):
        key = (req.employee_id, req.shift_date)
        if req.status != "APPROVED":
            pending_days.add(key)
        elif req.kind == "MARK_EXCUSE":
            excuses[key].append(de.ExcuseData(req.id, (req.payload or {}).get("mark") or "LATE",
                                              int((req.payload or {}).get("minutes") or 0), version=req.version))
        elif req.kind == "STATUS_OVERRIDE":
            overrides[key] = [de.OverrideData(req.id, req.version, (req.payload or {}).get("first_half"),
                                              (req.payload or {}).get("second_half"))]

    fact_index = {(employee_id, f.date): f for employee_id, rows in facts.items() for f in rows}

    def placement(employee_id: int, day: date):
        fact = fact_index.get((employee_id, day))
        assignment = getattr(fact, "assignment", None) if fact is not None else None
        return (getattr(fact, "legal_entity_id", None), getattr(fact, "branch_id", None),
                getattr(assignment, "pay_group_id", None))

    resolver = _Resolver(db, company_id, ids, days, placement)
    packs: Dict[Tuple[Optional[int], date], tuple] = {}

    def pack(legal_entity_id: Optional[int], day: date) -> tuple:
        """(statutory breaks, rest hours) of the legal entity's WORKING_TIME pack ("no pack": none)."""
        key = (legal_entity_id, start)  # pack versions are dated per year; one read per entity and chunk
        if key not in packs:
            from app.services import pack_resolver

            rules = pack_resolver.resolve(db, company_id, legal_entity_id, "breaks.rules", day).value or ()
            rest = pack_resolver.resolve(db, company_id, legal_entity_id, "rest.min_between_shifts_hours", day).value
            packs[key] = (tuple(de.StatutoryBreak(**dict(r)) for r in rules),
                          Decimal(str(rest)) if rest is not None else None)
        return packs[key]

    company_zone = _company_zone(db, company_id)
    result: Dict[Tuple[int, date], de.DayInputs] = {}
    for employee_id in ids:
        exp_by_day = {x.date: x for x in expectations.get(employee_id, [])}
        for day in days:
            fact = fact_index.get((employee_id, day))
            exp = exp_by_day.get(day)
            resolved = shifts.get((employee_id, day))
            zone = (resolved.work_timezone if resolved is not None else None) or (fact.timezone if fact else None) \
                or company_zone
            values = resolver.settings(employee_id, day)
            versions = {t: resolver.version(t, employee_id, day) for t in MODE_TYPES}
            mode, params, policy_ids, tie = _rules_mode(versions, v2)
            instances: Tuple[engine.ShiftInstance, ...] = ()
            if resolved is not None:
                try:
                    built = _instance(resolved, day, _fields(versions.get(RULES)) if mode == de.STANDARD else {})
                    instances = (replace(built, shift_id=resolved.shift_id, shift_version_id=resolved.shift_version_id,
                                         roster_entry_id=resolved.roster_entry_id),)
                except engine.ShiftEngineError:
                    instances = ()
            window = _night_window(values.get("attendance.night_window"))
            capture_required, shift_required = _capture(resolver.version(CAPTURE, employee_id, day))
            overtime = (_fields(resolver.version("OVERTIME_POLICY", employee_id, day))
                        if values.get("payroll.overtime.enabled") else {})
            statutory, rest_hours = pack(fact.legal_entity_id if fact else None, day)
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
                excuses=tuple(excuses.get((employee_id, day), ())),
                overrides=tuple(overrides.get((employee_id, day), ())),
                request_pending=(employee_id, day) in pending_days,
                rules_mode=mode, rules=params,
                legacy=_legacy_from(_fields(resolver.version("ATTENDANCE_PAY_POLICY", employee_id, day)),
                                    _fields(resolver.version("LATE_PENALTY_POLICY", employee_id, day))),
                settings=de.EvalSettings(
                    grace_minutes=_int_setting(values, "attendance.grace_minutes", 15),
                    night_window_start=window[0], night_window_end=window[1],
                    day_close_buffer_minutes=_int_setting(values, "attendance.day_close.buffer_minutes", 240),
                    capture_enabled=values.get("attendance.capture.enabled") is not False),
                ot_view=_overtime_view(overtime, values),
                capture_required=capture_required, shift_required=shift_required,
                policy_version_ids=policy_ids, not_evaluated=tie,
                statutory_breaks=statutory, rest_min_between_shifts_hours=rest_hours,
                previous_last_out_at=last_out.get((employee_id, day - timedelta(days=1))),
            )
    return _with_device_lag(db, company_id, ids, start, end, result, now)


def _with_device_lag(db: Session, company_id: int, ids: Sequence[int], start: date, end: date,
                     result: Dict[Tuple[int, date], de.DayInputs], now: datetime) -> Dict[Tuple[int, date], de.DayInputs]:
    """AS-M7 watermarks (01 §8.8 W-2 / W-3): relevant devices behind the end of the day keep it PROVISIONAL."""
    import pytz

    from app.services import device_ingest

    if not result or not device_ingest.has_ingest_devices(db, company_id):
        return result
    day_end: Dict[Tuple[int, date], datetime] = {}
    branch_of: Dict[Tuple[int, date], Optional[int]] = {}
    for key, inputs in result.items():
        ends = [i.end_at for i in inputs.instances if getattr(i, "end_at", None) is not None]
        if ends:
            day_end[key] = max(ends)
        else:
            zone = pytz.timezone(inputs.work_timezone or "UTC")
            day_end[key] = zone.localize(datetime.combine(key[1] + timedelta(days=1), datetime.min.time())) \
                .astimezone(timezone.utc)
        branch_of[key] = inputs.branch_id
    lag = device_ingest.device_lag(db, company_id, ids, start, end, day_end, branch_of, now)
    for key, (behind, elapsed) in lag.items():
        result[key] = replace(result[key], device_ids_behind=behind, late_log_grace_elapsed=elapsed)
    return result


# --------------------------------------------------------------------------- payroll period states (§8.5, §10)

# Configuration triggers recompute dates whose payroll state is OPEN only, unless the change carries
# ``apply_to_locked_periods`` with a reason (D-AS-44); facts (punches, decisions, leave, holidays, rosters) always
# recompute their dates and reach locked periods through the stale / retro path.
CONFIG_TRIGGERS = ("POLICY_CHANGED", "SETTING_CHANGED", "SHIFT_VERSION")


def period_states(db: Session, company_id: int, employee_ids: Sequence[int], start: date, end: date, *,
                  now: Optional[datetime] = None) -> Dict[Tuple[int, date], str]:
    """``calendars.period_state_on`` for many employees and dates at once (same rule: the pay period of the primary
    placement's pay group; CLOSED = FINALIZED, PROCESSING = INPUT_LOCKED, OPEN after the employee-local
    ``input_cutoff_on`` = INPUT_LOCKED; no pay group or no stored period = OPEN)."""
    from sqlalchemy import or_

    from app.models.org import EmployeeAssignment
    from app.models.pay_runs import PayPeriod
    from app.payroll.services.calendars import PERIOD_STATE
    from app.services import org_structure_service

    ids = sorted(set(employee_ids))
    days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    states = {(e, d): "OPEN" for e in ids for d in days}
    if not ids:
        return states
    placements = (db.query(EmployeeAssignment)
                  .filter(EmployeeAssignment.company_id == company_id, EmployeeAssignment.employee_id.in_(ids),
                          EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.effective_from <= end,
                          or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= start))
                  .order_by(EmployeeAssignment.effective_from.desc()).all())
    groups = sorted({p.pay_group_id for p in placements if p.pay_group_id})
    if not groups:
        return states
    periods = (db.query(PayPeriod)
               .filter(PayPeriod.company_id == company_id, PayPeriod.pay_group_id.in_(groups),
                       PayPeriod.period_start <= end, PayPeriod.period_end >= start).all())
    instant = now or clock.utcnow()
    local_today: Dict[int, date] = {}
    for employee in db.query(Employee).filter(Employee.company_id == company_id, Employee.id.in_(ids)):
        local_today[employee.id] = org_structure_service.employee_local_date(db, employee, instant)
    for employee_id in ids:
        rows = [p for p in placements if p.employee_id == employee_id]
        for day in days:
            placement = next((p for p in rows if p.effective_from <= day
                              and (p.effective_to is None or p.effective_to >= day)), None)
            if placement is None or not placement.pay_group_id:
                continue
            period = next((p for p in periods if p.pay_group_id == placement.pay_group_id
                           and p.period_start <= day <= p.period_end), None)
            if period is None:
                continue
            if period.status in PERIOD_STATE:
                states[(employee_id, day)] = PERIOD_STATE[period.status]
            elif local_today.get(employee_id) and period.input_cutoff_on \
                    and local_today[employee_id] > period.input_cutoff_on:
                states[(employee_id, day)] = "INPUT_LOCKED"
    return states


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
                   actor_user_id: Optional[int] = None, apply_to_locked_periods: bool = False) -> WriteResult:
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
    locked: set = set()
    if trigger in CONFIG_TRIGGERS and not apply_to_locked_periods and mode == "NORMAL":
        locked = {k for k, state in period_states(db, company_id, ids, start, end, now=now).items() if state != "OPEN"}
    for key in sorted(inputs):
        if key in locked:
            result.skipped_locked += 1
            continue
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
    # AS-M4 (§12): a new version inside a submitted / approved timesheet week reopens it.
    from app.services import attendance_timesheets

    attendance_timesheets.reopen_on_change(db, company_id, row.employee_id, row.shift_date)


# --------------------------------------------------------------------------- triggers

RECOMPUTE_JOB = "attendance.recompute_scope"
# Change-log record types that change what a day evaluates to -> the evaluation trigger (04-attendance/03 §8.1).
# ``attendance_day`` itself is absent (the write path's own log never schedules itself); pay-only records (LOP,
# overtime, tasks, wage inputs) change no evaluation.
CHANGE_TRIGGERS = {
    "leave": "LEAVE_CHANGED", "on_duty": "ON_DUTY_DECIDED",
    "holiday": "HOLIDAY_CHANGED", "optional_holiday": "HOLIDAY_CHANGED", "holiday_year": "HOLIDAY_CHANGED",
    "working_day": "HOLIDAY_CHANGED", "work_week_version": "WORKWEEK_CHANGED", "calendar_link": "WORKWEEK_CHANGED",
    "shift_version": "SHIFT_VERSION", "shift_rule": "SHIFT_VERSION", "roster_entry": "ROSTER_PUBLISHED",
    "assignment": "PLACEMENT_CHANGED", "suspension": "PLACEMENT_CHANGED", "attendance": "SESSION_CLOSED",
}
# Facts naming one employee recompute their dates whatever their age (§8.5 "facts always recompute"); employee-less
# configuration changes reach back at most this far (``attendance.recompute.max_back_days``).
RECOMPUTE_MAX_BACK_DAYS = 62


def _v2_cached(db: Session, company_id: int) -> bool:
    cache = db.info.setdefault("attendance_v2_enabled", {})
    if company_id not in cache:
        cache[company_id] = attendance_access.v2_enabled(db, company_id)
    return cache[company_id]


def schedule_recompute(db: Session, company_id: int, change) -> Optional[object]:
    """Called by ``time_inputs.log_change`` in the changing transaction: one ``attendance.recompute_scope`` job per
    change of a v2 tenant (idempotent per record version), so leave, on-duty, holiday, work-week, shift, roster and
    placement decisions re-evaluate the dates they touch (the LH topics of spine §9.1 have no emitter for leave in
    this repository; the change log is the one signal every source writes, IG-AS-71)."""
    trigger = CHANGE_TRIGGERS.get(change.record_type)
    if trigger is None or not _v2_cached(db, company_id):
        return None
    from app.payroll.services import jobs

    payload = {"trigger": trigger, "record_type": change.record_type, "record_id": int(change.record_id),
               "employee_id": change.employee_id,
               "date_from": change.date_from.isoformat() if change.date_from else None,
               "date_to": change.date_to.isoformat() if change.date_to else None}
    key = f"recompute:{change.record_type}:{change.record_id}:{change.version_hash[:16]}"
    return jobs.enqueue(db, company_id=company_id, type=RECOMPUTE_JOB, payload=payload, idempotency_key=key,
                        priority=120)


def schedule_config_recompute(db: Session, company_id: int, *, trigger: str, source: str, effective_from: date,
                              apply_to_locked_periods: bool = False) -> Optional[object]:
    """A configuration change of a v2 tenant (policy activation, attendance setting): every employee's dates from
    ``max(effective_from, today - 62)`` to today, OPEN payroll periods only unless ``apply_to_locked_periods``
    (§8.5)."""
    if trigger not in CONFIG_TRIGGERS or not _v2_cached(db, company_id):
        return None
    from app.payroll.services import jobs

    payload = {"trigger": trigger, "record_type": "configuration", "record_id": 0, "employee_id": None,
               "date_from": effective_from.isoformat(), "date_to": None,
               "apply_to_locked_periods": bool(apply_to_locked_periods), "source": source[:80]}
    stamp = clock.utcnow().strftime("%Y%m%dT%H%M%S%f")
    return jobs.enqueue(db, company_id=company_id, type=RECOMPUTE_JOB, payload=payload,
                        idempotency_key=f"recompute:config:{source[:60]}:{stamp}", priority=130)


def recompute_scope(db: Session, company_id: int, payload: Mapping, *, now: Optional[datetime] = None) -> dict:
    """The job body: re-evaluate the employees and dates a change names, up to employee-local today (future dates
    are evaluated by their own punches and day close), in chunks of 500 employees × 31 days."""
    if not attendance_access.v2_enabled(db, company_id):
        return {"evaluated": 0, "skipped": "v1"}
    trigger = payload.get("trigger") if payload.get("trigger") in de.EVALUATION_TRIGGERS else "MANUAL_RECOMPUTE"
    today = clock.company_today(db, company_id)
    employee_id = payload.get("employee_id")
    floor = today - timedelta(days=RECOMPUTE_MAX_BACK_DAYS)
    first = date.fromisoformat(payload["date_from"]) if payload.get("date_from") else floor
    last = date.fromisoformat(payload["date_to"]) if payload.get("date_to") else today
    if employee_id is None:
        first = max(first, floor)
    last = min(last, today)
    if last < first:
        return {"evaluated": 0}
    if employee_id is not None:
        employees = [int(employee_id)]
    else:
        employees = [e for (e,) in db.query(Employee.id).filter(Employee.company_id == company_id)
                     .order_by(Employee.id).all()]
    totals = WriteResult()
    for index in range(0, len(employees), MAX_EMPLOYEES):
        cursor = first
        while cursor <= last:
            stop = min(last, cursor + timedelta(days=MAX_DAYS - 1))
            part = evaluate_range(db, company_id, employees[index:index + MAX_EMPLOYEES], cursor, stop,
                                  trigger=trigger, now=now,
                                  apply_to_locked_periods=bool(payload.get("apply_to_locked_periods")))
            for name, value in part.as_dict().items():
                setattr(totals, name, getattr(totals, name) + value)
            cursor = stop + timedelta(days=1)
    return totals.as_dict()


def evaluate_dates(db: Session, company_id: int, employee_id: int, dates: Iterable[date], *, trigger: str,
                   actor_user_id: Optional[int] = None, now: Optional[datetime] = None) -> Optional[WriteResult]:
    """Fact triggers (punch, session closed, decisions) for one employee: v2 tenants only; v1 tenants keep the
    sessions as the payroll input and get no rows outside reconciliation."""
    picked = sorted(set(dates))
    if not picked or not _v2_cached(db, company_id):
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


MAX_EXCEPTION_DAYS = 62
LH_CONFLICT_KINDS = de.EXCEPTION_CODES[:6]  # the six leave-holiday kinds, verbatim (LH 05 §4.3; 03 §12.1)


def exceptions(db: Session, company_id: int, scope_clause, start: date, end: date, *,
               code: Optional[str] = None, employee_id: Optional[int] = None, skip: int = 0,
               limit: int = 50, kinds: Optional[Sequence[str]] = None) -> dict:
    """Decided days with exception codes (04-attendance/03 §12; ``GET /attendance/exceptions``): OPEN days are still
    in progress and never listed. ``scope_clause`` is the caller's employee scope on ``AttendanceDay.employee_id``."""
    if end < start:
        raise bad_request("The end date is before the start date.", "INVALID_RANGE")
    if (end - start).days + 1 > MAX_EXCEPTION_DAYS:
        raise bad_request(f"Choose a range of at most {MAX_EXCEPTION_DAYS} days.", "RANGE_TOO_LONG")
    if code is not None and code not in de.EXCEPTION_CODES:
        raise bad_request(f"Unknown exception code {code!r}.", "INVALID_EXCEPTION_CODE")
    query = (db.query(AttendanceDay, Employee)
             .join(Employee, (Employee.company_id == AttendanceDay.company_id) & (Employee.id == AttendanceDay.employee_id))
             .filter(AttendanceDay.company_id == company_id, AttendanceDay.shift_date >= start,
                     AttendanceDay.shift_date <= end, AttendanceDay.evaluation_state != de.OPEN,
                     func.cardinality(AttendanceDay.exception_codes) > 0, scope_clause))
    if code is not None:
        query = query.filter(AttendanceDay.exception_codes.any(code))
    if kinds is not None:  # the LH conflicts alias: only these kinds, and only they are listed per day
        query = query.filter(AttendanceDay.exception_codes.overlap(list(kinds)))
    if employee_id is not None:
        query = query.filter(AttendanceDay.employee_id == employee_id)
    total = query.count()
    rows = (query.order_by(AttendanceDay.shift_date.desc(), Employee.first_name, AttendanceDay.employee_id)
            .offset(skip).limit(limit).all())
    label_of = labels(db, company_id, [day for day, _ in rows])
    return {"total": total, "items": [
        {"day_id": day.id, "employee_id": day.employee_id,
         "employee_name": f"{emp.first_name} {emp.last_name or ''}".strip(), "shift_date": day.shift_date.isoformat(),
         "day_result": day.day_result, "evaluation_state": day.evaluation_state,
         "exception_codes": [c for c in day.exception_codes if kinds is None or c in kinds],
         "late_minutes": day.late_minutes,
         "early_minutes": day.early_minutes, "net_minutes": day.net_minutes, "day_label": label_of[day.id]}
        for day, emp in rows]}


def labels(db: Session, company_id: int, rows: Iterable[AttendanceDay]) -> Dict[int, dict]:
    """``{row.id: day label}`` (04-attendance/03 §11, never stored). The expectation reasons matter only where a
    portion is not expected (leave, holiday, weekly off, not employed): one light ``expected_presence`` pass over those
    rows' employees and dates; a FULL day needs none."""
    from app.services.attendance_day_label import day_label

    rows = list(rows)
    partial = [r for r in rows if r.expected_portion != "FULL"]
    reasons: Dict[Tuple[int, date], tuple] = {}
    if partial:
        start, end = min(r.shift_date for r in partial), max(r.shift_date for r in partial)
        found = ti.expected_presence(db, company_id, sorted({r.employee_id for r in partial}), start, end, light=True)
        reasons = {(e, x.date): tuple(x.reasons) for e, items in found.items() for x in items}
    return {r.id: day_label(r, reasons.get((r.employee_id, r.shift_date), ())) for r in rows}


def day_label(db: Session, company_id: int, row: AttendanceDay) -> dict:
    """The computed label of one stored day (spine §5.8)."""
    return labels(db, company_id, [row])[row.id]


def as_json(row: AttendanceDay, *, versions: Sequence[AttendanceDayVersion] = (), label: Optional[dict] = None) -> dict:
    body = {"id": row.id, "employee_id": row.employee_id, "shift_date": row.shift_date.isoformat(),
            "version": row.version, "row_version": row.row_version, "inputs_hash": row.inputs_hash,
            "evaluated_at": de._plain(row.evaluated_at), "evaluated_by": row.evaluated_by, "trigger": row.trigger,
            **_row_outputs(row)}
    if label is not None:
        body["day_label"] = label
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

"""Attendance v2 cut-over per tenant (spine §15.1; AS 13 §4.6 "Cut-over jobs", exit criteria 1 and 4; AS 05 §7.3).

States: **LEGACY** (``attendance.v2.enabled`` false, no reconcile yet) → **RECONCILING** (a reconcile ran) →
**V2_ACTIVE** (the flag is true; only ``cutover`` sets it, through ``attendance_access.set_v2_enabled``).

* ``v2_prepare`` — the capture and face steps of AS-M2 plus the rules step: one catch-all ``ATTENDANCE_RULES_POLICY``
  (everyone, priority 0) with an ACTIVE **LEGACY_V1** version from 2000-01-01, so resolution never fails and every day
  is evaluated by the exact port until a STANDARD version is activated (02 §4.1, §6). Idempotent.
* ``start_reconcile`` / job ``attendance.v2_reconcile`` — evaluates every employee and date of a past window
  (≤ 100 days) in **SHADOW** mode (rows and versions only: no change log, so no run goes stale) and compares each
  stored day with the ledger outcome of today's session input for the same date: day class (present / half day /
  absent / missing punch / no input), ``worked_seconds``, the late and early flags. The differences (first 200) and
  their count are the job result — the report.
* ``cutover`` — refused 409 ``ATTENDANCE_V2_NOT_RECONCILED`` unless the named reconcile SUCCEEDED within 24 hours
  with zero differences; the administrator who started that reconcile cannot approve it (409
  ``SELF_DECISION_FORBIDDEN``); a fresh step-up is required (403 ``STEP_UP_REQUIRED``). Audit
  ``ATTENDANCE_V2_RECONCILED`` and ``ATTENDANCE_V2_ACTIVATED``.

Services take ``(db, …)`` and never commit (the routes and the job runner do).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import List, Optional

from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, bad_request, not_found
from app.core.principal import Principal
from app.models.employee import Employee
from app.models.jobs import Job
from app.models.payroll_config import Policy
from app.payroll import ledger
from app.payroll import policy_types as pt
from app.payroll.services import jobs, policies
from app.payroll.services import time_inputs as ti
from app.services import attendance_access, attendance_days, attendance_setup

RULES = "ATTENDANCE_RULES_POLICY"
LEGACY_RULES_NAME = "Attendance rules (current behaviour)"
RECONCILE_JOB = "attendance.v2_reconcile"
MAX_WINDOW_DAYS = 100
RECONCILE_VALID_HOURS = 24
REPORT_LIMIT = 200
# The LEGACY_V1 column of 02 §4.3 (every field the port does not read keeps the STANDARD default).
LEGACY_RULES_FIELDS = {"profile": "LEGACY_V1", "hours_basis": "NET", "work_time_basis": "SUM_OF_SESSIONS",
                       "absent_marking": "LEAVE_UNMARKED", "grace_in_minutes": None, "flexible_late_basis": "NONE",
                       "late_half_day_after_minutes": None, "early_half_day_before_minutes": None,
                       "late_waiver": "NONE", "early_waiver": "NONE", "short_hours_enabled": False,
                       "short_tolerance_minutes": 0, "rounding": {"mode": "NONE", "increment_minutes": 1},
                       "open_day_start_time": "04:00", "ot_early_arrival": "NEVER",
                       "ot_post_shift_buffer_minutes": 0, "ot_requires_pre_approval": False}


# --------------------------------------------------------------------------- prepare


def v2_prepare_rules(db: Session, company_id: int) -> Optional[int]:
    """The LEGACY_V1 rules policy of the tenant; returns its id (None when a rules policy exists already)."""
    if db.query(Policy.id).filter(Policy.company_id == company_id, Policy.type == RULES).first() is not None:
        return None
    fields = pt.validate_fields(RULES, LEGACY_RULES_FIELDS)
    starter = pt.StarterPolicy(type=RULES, name=LEGACY_RULES_NAME, fields=fields, status="ACTIVE",
                               policy_hash=pt.policy_hash(fields),
                               note="LEGACY_V1: today's attendance outcome exactly, until a STANDARD version is "
                                    "activated after the cut-over.")
    return policies.create_starter_policy(db, company_id, starter, effective_from=attendance_setup.LEGACY_FROM).id


LEGACY_TYPES = {
    "ATTENDANCE_REQUEST_POLICY": ("Requests (current behaviour)",
                                  {"profile": "LEGACY_V1", "self_kinds": [], "limits": [], "backdate_days": 7}),
    "SCHEDULING_POLICY": ("Schedules (current behaviour)", {"profile": "LEGACY_V1", "schedule_source": "PLACEMENT"}),
}


def _prepare_legacy(db: Session, company_id: int, type_code: str, name: str, raw: dict) -> Optional[int]:
    if db.query(Policy.id).filter(Policy.company_id == company_id, Policy.type == type_code).first() is not None:
        return None
    fields = pt.validate_fields(type_code, raw)
    starter = pt.StarterPolicy(type=type_code, name=name, fields=fields, status="ACTIVE",
                               policy_hash=pt.policy_hash(fields), note="LEGACY_V1: today's behaviour.")
    return policies.create_starter_policy(db, company_id, starter, effective_from=attendance_setup.LEGACY_FROM).id


def v2_prepare(db: Session, company_id: int) -> dict:
    """Every step of ``attendance.v2_prepare`` (idempotent): capture + faces (AS-M2), rules, requests, schedules."""
    result = {**attendance_setup.v2_prepare(db, company_id), "rules_policy_id": v2_prepare_rules(db, company_id)}
    for type_code, (name, raw) in LEGACY_TYPES.items():
        result[f"{type_code.lower()}_id"] = _prepare_legacy(db, company_id, type_code, name, raw)
    return result


# --------------------------------------------------------------------------- state


def _reconciles(db: Session, company_id: int):
    return (db.query(Job).filter(Job.company_id == company_id, Job.type == RECONCILE_JOB)
            .order_by(Job.id.desc()))


def reconcile_json(job: Job) -> dict:
    result = job.result or {}
    return {"job_id": job.id, "status": job.status, "started_by_user_id": job.created_by_user_id,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "start": (job.payload or {}).get("start"), "end": (job.payload or {}).get("end"),
            "days": result.get("days"), "differences": result.get("differences"),
            "items": result.get("items", [])}


def status(db: Session, company_id: int) -> dict:
    latest = _reconciles(db, company_id).first()
    if attendance_access.v2_enabled(db, company_id):
        state = "V2_ACTIVE"
    else:
        state = "RECONCILING" if latest is not None else "LEGACY"
    prepared = db.query(Policy.id).filter(Policy.company_id == company_id, Policy.type == RULES).first() is not None
    return {"state": state, "prepared": prepared,
            "last_reconcile": reconcile_json(latest) if latest is not None else None}


# --------------------------------------------------------------------------- reconcile


def start_reconcile(db: Session, principal: Principal, start: date, end: date) -> Job:
    company_id = principal.company_id
    if attendance_access.v2_enabled(db, company_id):
        raise AppError(409, "Attendance v2 is already active for this company.", "ATTENDANCE_V2_ACTIVE")
    today = clock.company_today(db, company_id)
    if end < start:
        raise bad_request("The end date is before the start date.", "INVALID_RANGE")
    if end >= today:
        raise bad_request("Reconcile past days only: end the window yesterday or earlier.", "INVALID_RANGE")
    if (end - start).days + 1 > MAX_WINDOW_DAYS:
        raise bad_request(f"Reconcile at most {MAX_WINDOW_DAYS} days at a time.", "RANGE_TOO_LONG")
    if db.query(Policy.id).filter(Policy.company_id == company_id, Policy.type == RULES).first() is None:
        raise AppError(409, "Prepare attendance v2 first (it creates the current-behaviour rules).",
                       "ATTENDANCE_V2_NOT_PREPARED")
    stamp = clock.utcnow().strftime("%Y%m%dT%H%M%S%f")
    return jobs.enqueue(db, company_id=company_id, type=RECONCILE_JOB,
                        payload={"start": start.isoformat(), "end": end.isoformat()},
                        idempotency_key=f"v2-reconcile:{start}:{end}:{stamp}", priority=150,
                        created_by_user_id=principal.user_id, read_permission="attendance:manage_policy")


def _day_class(day: Optional[ledger.DayRecord]) -> str:
    """The ledger's view of one date with attendance only (no leave, holiday or on-duty: those decide alike)."""
    if day is None:
        return "NO_INPUT"
    kinds = {day.am.classification, day.pm.classification}
    C = ledger.Classification
    if C.UNMARKED in kinds:
        return "NO_INPUT"
    if C.MISSING_PUNCH_OUT in kinds:
        return "MISSING_PUNCH"
    if C.HALF_DAY in kinds:
        return "HALF_DAY"
    if kinds == {C.ABSENT}:
        return "ABSENT"
    return "PRESENT"


ROW_CLASS = {"PRESENT": "PRESENT", "HALF_DAY": "HALF_DAY", "ABSENT": "ABSENT", "MISSING_PUNCH": "MISSING_PUNCH",
             "UNMARKED": "NO_INPUT", "PENDING": "NO_INPUT", "OFF": "NO_INPUT"}


def compare(db: Session, company_id: int, employee_ids: List[int], start: date, end: date) -> List[dict]:
    """Ledger-output comparison of the stored evaluations with today's session inputs (exit criterion 1)."""
    rows = attendance_days.days_for(db, company_id, employee_ids, start, end)
    items: List[dict] = []
    days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    for employee_id in employee_ids:
        params = attendance_days._legacy_params(db, company_id, employee_id, end)
        cfg = ledger.LedgerConfig(attendance_enabled=True, derive_status_from_hours=params.derive_status_from_hours,
                                  full_day_min_hours=params.full_day_min_hours,
                                  half_day_min_hours=params.half_day_min_hours)
        inputs = ti._attendance_inputs(db, company_id, employee_id, start, end, None,
                                       params.early_exit_grace_minutes)
        for day in days:
            row = rows.get((employee_id, day))
            if row is None:
                items.append({"employee_id": employee_id, "date": day.isoformat(), "field": "evaluation",
                              "legacy": "present", "v2": "missing"})
                continue
            att = inputs.get(day)
            record = ledger.build_ledger([ledger.DayInput(date=day, attendance=att)], cfg).days[0] \
                if att is not None else None
            checks = {}
            if att is not None:  # a session exists: its figures must match the port's
                checks = {"worked_seconds": (att.worked_seconds, row.net_seconds), "late": (att.late, row.late_mark),
                          "early_exit": (att.early_exit, row.early_mark)}
            if row.expected_portion != "NONE":
                checks["day_class"] = (_day_class(record), ROW_CLASS.get(row.day_result, row.day_result))
            elif att is not None or row.session_count:
                checks["worked"] = (att is not None, bool(row.session_count))
            for field, (legacy, v2) in checks.items():
                if legacy != v2:
                    items.append({"employee_id": employee_id, "date": day.isoformat(), "field": field,
                                  "legacy": legacy, "v2": v2})
    return items


def reconcile(db: Session, company_id: int, start: date, end: date, *, now: Optional[datetime] = None) -> dict:
    """Job body: SHADOW evaluation of the window (chunks of 500 × 31) and the comparison report."""
    employees = [e for (e,) in db.query(Employee.id).filter(Employee.company_id == company_id)
                 .order_by(Employee.id).all()]
    written = 0
    items: List[dict] = []
    for index in range(0, len(employees), attendance_days.MAX_EMPLOYEES):
        chunk = employees[index:index + attendance_days.MAX_EMPLOYEES]
        cursor = start
        while cursor <= end:
            stop = min(end, cursor + timedelta(days=attendance_days.MAX_DAYS - 1))
            written += attendance_days.evaluate_range(db, company_id, chunk, cursor, stop, trigger="BACKFILL",
                                                      mode="SHADOW", now=now).written
            items.extend(compare(db, company_id, chunk, cursor, stop))
            cursor = stop + timedelta(days=1)
    return {"start": start.isoformat(), "end": end.isoformat(), "employees": len(employees),
            "days": len(employees) * ((end - start).days + 1), "written": written, "differences": len(items),
            "items": items[:REPORT_LIMIT]}


# --------------------------------------------------------------------------- cut-over


def cutover(db: Session, principal: Principal, reconcile_job_id: int) -> dict:
    from app.payroll.services import step_up
    from app.services.audit_log_service import AuditLogService

    company_id = principal.company_id
    if attendance_access.v2_enabled(db, company_id):
        raise AppError(409, "Attendance v2 is already active for this company.", "ATTENDANCE_V2_ACTIVE")
    job = (db.query(Job).filter(Job.company_id == company_id, Job.type == RECONCILE_JOB,
                                Job.id == reconcile_job_id).first())
    if job is None:
        raise not_found("Reconcile")
    result = job.result or {}
    fresh = job.updated_at is not None and job.updated_at >= clock.utcnow() - timedelta(hours=RECONCILE_VALID_HOURS)
    if job.status != jobs.SUCCEEDED or result.get("differences") != 0 or not fresh:
        found = result.get("differences")
        raise AppError(409, {"message": "Attendance v2 can be switched on only after a reconcile from the last "
                                        f"{RECONCILE_VALID_HOURS} hours found no differences"
                                        + (f" (this one found {found})." if found else "."),
                             "details": {"differences": found, "status": job.status}},
                       "ATTENDANCE_V2_NOT_RECONCILED")
    if job.created_by_user_id == principal.user_id:
        raise AppError(409, "Another administrator must approve the cut-over you reconciled.",
                       "SELF_DECISION_FORBIDDEN")
    step_up.ensure_step_up(db, principal)
    AuditLogService.log(db, principal, "Attendance", "ATTENDANCE_V2_RECONCILED",
                        f"Reconcile {job.id} ({result.get('start')}..{result.get('end')}, {result.get('days')} days): "
                        "no differences", "job", job.id)
    attendance_access.set_v2_enabled(db, principal, True, reason=f"Attendance v2 cut-over (reconcile {job.id})")
    db.info.pop("attendance_v2_enabled", None)
    AuditLogService.log(db, principal, "Attendance", "ATTENDANCE_V2_ACTIVATED",
                        f"Attendance v2 switched on after reconcile {job.id}", "job", job.id)
    return status(db, company_id)

"""Attendance → payroll synchronisation and the unified cutoff assistant (G-16 / X-M5; X 03 §3, §5; AS 05 JC-AS-10,
JC-AS-11, JC-AS-40; WI-X-5.1..5.3).

**Change path** (one signal, ``source_change_log``): every attendance fact that payroll reads writes ``log_change``
(``attendance_day`` versions of v2 tenants, ``attendance`` sessions of v1 tenants, ``on_duty``). ``on_change`` is
called by ``time_inputs.log_change`` in the changing transaction and routes the change by the payroll state of the
date, read through the **attendance window** of the pay group (JC-AS-10: with a 26th–25th cycle, 27 Nov belongs to
the December period):

* OPEN — nothing more: the next input lock reads the live value (the run is re-evaluated in place);
* INPUT_LOCKED — the hourly ``payroll.match_source_changes`` marks the run stale (``change_detection``); a pass is
  scheduled for the next slot so the processor sees "Inputs changed since lock" within the hour;
* FINALIZED — job ``payroll.retro.detect`` (key ``retro:{record_type}:{id}:{version}``) runs ``retro.detect`` for the
  finalized period whose window holds the date: the period is recalculated with the corrected day ledger and the
  difference becomes an arrears / recovery proposal paid by the next run (finalized runs are never edited, INV-5).
  The proposal's change reference names the record and version that caused it (``ref``), which the run review and
  payslip show as the retro breakdown.

**Cutoff assistant** (``GET /cutoff?pay_group_id&period_id``; X 03 §5, D-X-16): one read across modules for a pay
group's period — period dates, window and lock state; attendance days not FINAL and MISSING_PUNCH days of the window;
pending attendance requests, on-duty and leave; device watermarks behind; runs and their stale flag; with a
severity per item (BLOCKING per ``payroll.attendance.pending_at_cutoff`` / ``payroll.leave.unapproved_at_cutoff``).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import forbidden, not_found
from app.core.principal import Principal
from app.payroll.services import jobs

RETRO_JOB = "payroll.retro.detect"
RETRO_RECORD_TYPES = ("attendance_day", "attendance", "on_duty")
MAX_RETRO_DAYS = 62
DEVICE_STALE_AFTER = timedelta(hours=2)


# --------------------------------------------------------------------------- period of a date (by window)


def placement_pay_group(db: Session, company_id: int, employee_id: int, day: date) -> Optional[int]:
    from app.models.org import EmployeeAssignment

    row = (db.query(EmployeeAssignment.pay_group_id)
           .filter(EmployeeAssignment.company_id == company_id, EmployeeAssignment.employee_id == employee_id,
                   EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.effective_from <= day,
                   or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= day))
           .order_by(EmployeeAssignment.effective_from.desc()).first())
    return row[0] if row else None


def period_of_window(db: Session, company_id: int, pay_group_id: int, day: date):
    """The pay period whose attendance window holds ``day`` (JC-AS-10), else the one whose dates hold it."""
    from app.models.pay_runs import PayPeriod

    found = (db.query(PayPeriod)
             .filter(PayPeriod.company_id == company_id, PayPeriod.pay_group_id == pay_group_id,
                     PayPeriod.attendance_start <= day, PayPeriod.attendance_end >= day)
             .order_by(PayPeriod.period_start).first())
    if found is not None:
        return found
    return (db.query(PayPeriod)
            .filter(PayPeriod.company_id == company_id, PayPeriod.pay_group_id == pay_group_id,
                    PayPeriod.period_start <= day, PayPeriod.period_end >= day).first())


# --------------------------------------------------------------------------- change path


def _v2(db: Session, company_id: int) -> bool:
    from app.services import attendance_days

    return attendance_days._v2_cached(db, company_id)


def on_change(db: Session, company_id: int, change) -> Optional[object]:
    """Route one change-log row by the payroll state of its dates (see the module doc). Never raises for data."""
    if change.record_type not in RETRO_RECORD_TYPES or change.employee_id is None or change.date_from is None:
        return None
    if change.record_type == "attendance" and _v2(db, company_id):
        return None  # v2: the evaluated day's own attendance_day change follows (one retro per change)
    first = change.date_from
    last = min(change.date_to or first, first + timedelta(days=MAX_RETRO_DAYS - 1))
    finalized: Dict[int, List[str]] = {}
    locked = False
    day = first
    while day <= last:
        group = placement_pay_group(db, company_id, change.employee_id, day)
        period = period_of_window(db, company_id, group, day) if group else None
        if period is not None:
            if period.status == "CLOSED":
                finalized.setdefault(period.id, []).append(day.isoformat())
            elif period.status == "PROCESSING":
                locked = True
        day += timedelta(days=1)
    if locked:
        from app.payroll.services import change_detection

        change_detection.schedule_matching(db, company_id)
    if not finalized:
        return None
    key = f"retro:{change.record_type}:{change.record_id}:{change.version_hash[:16]}"
    return jobs.enqueue(db, company_id=company_id, type=RETRO_JOB, idempotency_key=key, priority=140,
                        payload={"employee_id": change.employee_id, "record_type": change.record_type,
                                 "record_id": int(change.record_id), "version": change.version_hash[:16],
                                 "periods": {str(k): v for k, v in finalized.items()},
                                 "change_details": change.details or {}})


@jobs.register(RETRO_JOB)
def retro_detect_job(ctx) -> dict:
    """FINALIZED dates: one arrears / recovery proposal per finalized period the change reaches (JC-AS-11)."""
    from app.models.pay_runs import PayPeriod
    from app.payroll.services import retro

    payload = ctx.payload or {}
    employee_id = int(payload["employee_id"])
    ref = {"record_type": payload.get("record_type"), "record_id": payload.get("record_id"),
           "version": (payload.get("change_details") or {}).get("version") or payload.get("version")}
    proposals, outcomes_total = 0, 0
    for period_id, dates in sorted((payload.get("periods") or {}).items()):
        period = ctx.db.get(PayPeriod, int(period_id))
        if period is None:
            continue
        change = retro.Change("ATTENDANCE", reason="Attendance corrected after payroll was finalized",
                              ref={**ref, "dates": dates, "period_id": period.id})
        outcomes = retro.detect(ctx.db, None, ctx.company_id, employee_id, period.period_start, change,
                                to_date=period.period_start)
        outcomes_total += len(outcomes)
        proposals += sum(1 for o in outcomes if o.proposal is not None)
    return {"proposals": proposals, "outcomes": outcomes_total}


# --------------------------------------------------------------------------- retro breakdown (UI)


def _proposal_json(proposal, lines) -> dict:
    from app.payroll.money import amount_str

    ref = dict(proposal.trigger_ref or {})
    days = []
    if ref.get("record_type") == "attendance_day":
        days = [{"employee_id": proposal.employee_id, "date": d, "attendance_day_id": ref.get("record_id"),
                 "version": ref.get("version"), "link": f"/attendance/days/{proposal.employee_id}/{d}"}
                for d in ref.get("dates") or ()]
    return {
        "proposal_id": proposal.id, "trigger": proposal.trigger, "status": proposal.status,
        "reason": proposal.reason, "currency": proposal.currency,
        "original_period": {"start": proposal.period_start.isoformat(), "end": proposal.period_end.isoformat()},
        "lines": [{"component_code": line.component_code, "direction": line.direction,
                   "delta": amount_str(line.delta, line.currency), "explanation": line.explanation}
                  for line in lines],
        "cause": ref, "attendance_days": days,
    }


def retro_breakdown(db: Session, principal: Principal, run_employee_id: int) -> dict:
    """Retro attendance adjustments of one run employee (payslip and run review): the arrears / recovery proposals it
    pays (instalments in its snapshot or settled by its run) and the later corrections of its own period, each with
    the change that caused it — for attendance-day changes the evaluated day and version (JC-AS-11, X 03 §3)."""
    from app.models.corrections import ArrearsInstalment, ArrearsProposal, ArrearsProposalLine
    from app.models.pay_runs import PayPeriod, PayrollInputSnapshot, PayrollRun, PayrollRunEmployee
    from app.payroll.services import access

    row = db.query(PayrollRunEmployee).filter(PayrollRunEmployee.company_id == principal.company_id,
                                              PayrollRunEmployee.id == run_employee_id).first()
    if row is None:
        raise not_found("Run employee")
    run = db.get(PayrollRun, row.run_id)
    period = db.get(PayPeriod, run.pay_period_id) if run is not None else None
    own = principal.employee_id is not None and principal.employee_id == row.employee_id
    if own:
        if not principal.has("payroll:payslip:read"):
            raise forbidden()
    else:
        access.ensure_employee_in_scope(db, principal, "payroll:run:read", row.employee_id,
                                        period.period_start if period else date.min,
                                        period.period_end if period else date.max, what="Run employee")
    snapshot = (db.query(PayrollInputSnapshot).filter(PayrollInputSnapshot.company_id == principal.company_id,
                                                      PayrollInputSnapshot.run_employee_id == row.id)
                .order_by(PayrollInputSnapshot.version.desc()).first())
    instalment_ids = {int(i["id"]) for i in ((snapshot.content or {}).get("arrears") or [])
                      if isinstance(i, dict) and str(i.get("id", "")).isdigit()} if snapshot else set()
    paid = (db.query(ArrearsInstalment.proposal_id)
            .filter(ArrearsInstalment.company_id == principal.company_id,
                    ArrearsInstalment.employee_id == row.employee_id,
                    or_(ArrearsInstalment.settled_run_id == row.run_id,
                        ArrearsInstalment.id.in_(sorted(instalment_ids) or [0]))))
    paid_ids = {p for (p,) in paid}
    proposals = (db.query(ArrearsProposal)
                 .filter(ArrearsProposal.company_id == principal.company_id,
                         or_(ArrearsProposal.id.in_(sorted(paid_ids) or [0]),
                             ArrearsProposal.original_run_employee_id == row.id))
                 .order_by(ArrearsProposal.id).all())
    lines: Dict[int, list] = {}
    for line in (db.query(ArrearsProposalLine)
                 .filter(ArrearsProposalLine.company_id == principal.company_id,
                         ArrearsProposalLine.proposal_id.in_([p.id for p in proposals] or [0]))
                 .order_by(ArrearsProposalLine.sort_order, ArrearsProposalLine.id)):
        lines.setdefault(line.proposal_id, []).append(line)
    paid_here = [_proposal_json(p, lines.get(p.id, [])) for p in proposals if p.id in paid_ids]
    corrections = [_proposal_json(p, lines.get(p.id, [])) for p in proposals
                   if p.original_run_employee_id == row.id and p.status not in ("SUPERSEDED", "CANCELLED")]
    return {"run_employee_id": row.id, "employee_id": row.employee_id, "paid_here": paid_here,
            "corrections_of_this_period": corrections}


# --------------------------------------------------------------------------- cutoff assistant (X 03 §5)


def _severity(db: Session, company_id: int, key: str, pay_group_id: int, on: date) -> str:
    from app.payroll.services import settings as settings_engine

    try:
        value = settings_engine.resolve(db, company_id, key, pay_group_id=pay_group_id, on=on).value
    except Exception:  # noqa: BLE001 - an invalid stored value is reported by the run's own CONFIG check
        value = "EXCEPTION"
    return "BLOCKING" if value == "EXCEPTION" else "WARNING"


def cutoff(db: Session, principal: Principal, *, pay_group_id: int, period_id: Optional[int] = None) -> dict:
    from app.models.attendance import AttendanceDay, AttendanceDevice
    from app.models.attendance_requests import AttendanceRequest
    from app.models.leave import Leave
    from app.models.org import EmployeeAssignment
    from app.models.pay_org import PayGroup
    from app.models.pay_runs import PayPeriod, PayrollRun
    from app.models.time_inputs import OnDutyRequest
    from app.payroll.services import access

    if not (principal.has("payroll:run:read") or principal.has("attendance:close_period")):
        raise forbidden()
    cid = principal.company_id
    group = db.query(PayGroup).filter(PayGroup.company_id == cid, PayGroup.id == pay_group_id).first()
    if group is None:
        raise not_found("Pay group")
    if principal.has("payroll:run:read") and not principal.has_company_scope("payroll:run:read"):
        if not access.target_scope(principal, "payroll:run:read").covers(group.legal_entity_id, group.id):
            raise not_found("Pay group")
    today = clock.company_today(db, cid)
    query = db.query(PayPeriod).filter(PayPeriod.company_id == cid, PayPeriod.pay_group_id == group.id)
    if period_id is not None:
        period = query.filter(PayPeriod.id == period_id).first()
    else:
        period = (query.filter(PayPeriod.status != "CLOSED").order_by(PayPeriod.period_start).first()
                  or query.order_by(PayPeriod.period_start.desc()).first())
    if period is None:
        if period_id is not None:
            raise not_found("Pay period")
        # A pay group without a generated calendar: nothing can lock yet, say what to do (not a bare 404).
        return {"pay_group": {"id": group.id, "name": getattr(group, "name", None)}, "period": None,
                "attendance_window": None, "lock_state": None, "employees": 0, "attendance_v2": _v2(db, cid),
                "runs": [], "blocking": 1, "warnings": 0, "ready": False,
                "items": [{"code": "NO_PAY_PERIOD", "module": "payroll", "severity": "BLOCKING", "count": 1,
                           "message": "This pay group has no pay calendar yet: generate its periods first.",
                           "link": "/settings/payroll/general"}]}
    start, end = period.attendance_start, period.attendance_end
    if end < start:
        start, end = period.period_start, period.period_end
    state = {"CLOSED": "FINALIZED", "PROCESSING": "INPUT_LOCKED"}.get(period.status) or (
        "INPUT_LOCKED" if today > period.input_cutoff_on else "OPEN")
    ids = sorted({e for (e,) in db.query(EmployeeAssignment.employee_id).filter(
        EmployeeAssignment.company_id == cid, EmployeeAssignment.pay_group_id == group.id,
        EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.effective_from <= end,
        or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= start))})
    attendance_sev = _severity(db, cid, "payroll.attendance.pending_at_cutoff", group.id, period.period_end)
    leave_sev = _severity(db, cid, "payroll.leave.unapproved_at_cutoff", group.id, period.period_end)
    items: List[dict] = []
    v2 = _v2(db, cid)
    upto = min(end, today - timedelta(days=1))
    if v2 and ids and upto >= start:
        rows = (db.query(AttendanceDay.evaluation_state, AttendanceDay.day_result, func.count())
                .filter(AttendanceDay.company_id == cid, AttendanceDay.employee_id.in_(ids),
                        AttendanceDay.shift_date >= start, AttendanceDay.shift_date <= upto)
                .group_by(AttendanceDay.evaluation_state, AttendanceDay.day_result).all())
        not_final = sum(n for s, _, n in rows if s != "FINAL")
        missing = sum(n for s, r, n in rows if r == "MISSING_PUNCH")
        if not_final:
            items.append({"code": "ATTENDANCE_DAYS_NOT_FINAL", "module": "attendance", "severity": attendance_sev,
                          "count": not_final, "message": f"{not_final} day(s) of the window are not decided yet",
                          "link": f"/attendance/exceptions?from={start.isoformat()}&to={upto.isoformat()}"})
        if missing:
            items.append({"code": "MISSING_PUNCH_DAYS", "module": "attendance", "severity": "WARNING",
                          "count": missing, "message": f"{missing} day(s) with a missing punch",
                          "link": f"/attendance/exceptions?code=MISSING_PUNCH_OUT&from={start.isoformat()}"})
    pending_requests = db.query(func.count(AttendanceRequest.id)).filter(
        AttendanceRequest.company_id == cid, AttendanceRequest.employee_id.in_(ids or [0]),
        AttendanceRequest.shift_date >= start, AttendanceRequest.shift_date <= end,
        AttendanceRequest.status.in_(("PENDING", "RETURNED"))).scalar() or 0
    if pending_requests:
        items.append({"code": "ATTENDANCE_REQUESTS_PENDING", "module": "attendance", "severity": attendance_sev,
                      "count": pending_requests, "message": f"{pending_requests} attendance request(s) waiting",
                      "link": "/approvals?module=attendance"})
    pending_on_duty = db.query(func.count(OnDutyRequest.id)).filter(
        OnDutyRequest.company_id == cid, OnDutyRequest.employee_id.in_(ids or [0]), OnDutyRequest.date >= start,
        OnDutyRequest.date <= end, OnDutyRequest.status == "PENDING").scalar() or 0
    if pending_on_duty:
        items.append({"code": "ON_DUTY_PENDING", "module": "attendance", "severity": leave_sev,
                      "count": pending_on_duty, "message": f"{pending_on_duty} on-duty day(s) waiting",
                      "link": "/approvals?module=attendance"})
    pending_leave = db.query(func.count(Leave.id)).filter(
        Leave.company_id == cid, Leave.employee_id.in_(ids or [0]), Leave.start_date <= end, Leave.end_date >= start,
        or_(Leave.status == "pending", (Leave.status == "approved") & (Leave.cancellation_status == "pending"))
    ).scalar() or 0
    if pending_leave:
        items.append({"code": "LEAVE_PENDING", "module": "leave", "severity": leave_sev, "count": pending_leave,
                      "message": f"{pending_leave} leave request(s) waiting", "link": "/leaves?tab=approvals"})
    now = clock.utcnow()
    devices = db.query(AttendanceDevice).filter(AttendanceDevice.company_id == cid,
                                                AttendanceDevice.status == "ACTIVE").all()
    behind = [d for d in devices if _device_behind(d, now)]
    if behind:
        items.append({"code": "DEVICES_BEHIND", "module": "attendance", "severity": "WARNING", "count": len(behind),
                      "message": f"{len(behind)} device(s) have not synced in the last 2 hours: punches may be missing",
                      "devices": [{"id": d.id, "name": getattr(d, "name", None) or getattr(d, "code", None),
                                   "last_seen_at": _iso(_last_seen(d))} for d in behind[:20]],
                      "link": "/attendance/devices"})
    if period.input_cutoff_on <= end:
        # AS-PAY-06: a night shift of the window's last day closes after the cutoff, so it can never be FINAL at the
        # lock and is an attendance_day_pending item every month (X 03 recommends cutoff >= window end + 1).
        items.append({"code": "CUTOFF_BEFORE_WINDOW_END", "module": "payroll", "severity": "WARNING", "count": 1,
                      "message": f"Inputs lock on {period.input_cutoff_on.isoformat()}, before the attendance window "
                                 f"ends ({end.isoformat()}): night shifts of the last days will still be pending. "
                                 "Move the cutoff to at least one day after the window ends.",
                      "link": "/settings/payroll/general"})
    runs = (db.query(PayrollRun).filter(PayrollRun.company_id == cid, PayrollRun.pay_period_id == period.id)
            .order_by(PayrollRun.id).all())
    stale = [r for r in runs if getattr(r, "stale", False)]
    if stale:
        items.append({"code": "RUN_STALE", "module": "payroll", "severity": "BLOCKING", "count": len(stale),
                      "message": "Inputs changed since the run locked them: recalculate before approval",
                      "link": f"/payroll/runs/{stale[0].id}"})
    blocking = sum(1 for i in items if i["severity"] == "BLOCKING")
    return {
        "pay_group": {"id": group.id, "name": getattr(group, "name", None)},
        "period": {"id": period.id, "start": period.period_start.isoformat(), "end": period.period_end.isoformat(),
                   "status": period.status, "input_cutoff_on": period.input_cutoff_on.isoformat(),
                   "pay_date": period.pay_date.isoformat() if getattr(period, "pay_date", None) else None},
        "attendance_window": {"start": start.isoformat(), "end": end.isoformat(),
                              "split": (start, end) != (period.period_start, period.period_end)},
        "lock_state": state, "employees": len(ids), "attendance_v2": v2,
        "runs": [{"id": r.id, "status": r.status, "stale": bool(getattr(r, "stale", False))} for r in runs],
        "items": items, "blocking": blocking, "warnings": len(items) - blocking,
        "ready": blocking == 0,
    }


def _last_seen(device) -> Optional[datetime]:
    for name in ("last_seen_at", "last_sync_at", "watermark_at", "last_punch_at"):
        value = getattr(device, name, None)
        if value is not None:
            return value
    return None


def _device_behind(device, now: datetime) -> bool:
    if getattr(device, "health", None) in ("STALE", "OFFLINE"):
        return True
    seen = _last_seen(device)
    return seen is not None and now - seen > DEVICE_STALE_AFTER


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


__all__ = ["on_change", "retro_detect_job", "cutoff", "retro_breakdown", "period_of_window"]

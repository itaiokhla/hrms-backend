"""Supervisor muster: mark a crew or site for one shift date (AS-M4; 04-attendance/04 §11; D-AS-75; GV-AS-CAP-03,
GV-AS-REG-12, -18).

``GET /attendance/muster?team_id|branch_id&date`` lists the crew on the date (team memberships, or the people placed
in the branch) with their sessions, the current evaluation and open requests. ``POST /attendance/muster`` saves rows
``{employee_id, mark ∈ MUSTER_MARKS, in_time?, out_time?, reason_code?}`` one by one, each in its own savepoint and
employee lock, so one refused row never blocks the others (AS-REG-04):

* ``IN_OUT``: two ``SUPERVISOR`` punches and their session (times are wall clock in the worker's work zone; an end at
  or before the start is the next day);
* ``PRESENT`` / ``HALF_DAY`` / ``ABSENT`` / ``IDLE``: a DIRECT ``STATUS_OVERRIDE`` (``IDLE`` = PRESENT with basis
  OVERRIDE and reason ``EMPLOYER_IDLE`` or ``RAIN_OFF``, D-AS-64).

Every row needs ``attendance:muster`` covering the worker on the date; an uncovered or unknown employee is
``REFUSED NOT_IN_SCOPE`` with the same message (no existence leak); the supervisor's own row is
``SELF_EDIT_FORBIDDEN``. ``offline_batch_id`` replays return the stored results (in-process store; FU-AS-31).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy.orm import Session

from app.core import authz
from app.core.errors import AppError, bad_request, forbidden
from app.core.principal import Principal
from app.models.attendance import Attendance
from app.models.attendance_requests import MUSTER_MARKS, AttendanceRequest
from app.models.employee import Employee
from app.services import attendance_requests as ar
from app.services.audit_log_service import AuditLogService

MAX_ROWS = 200
IDLE_REASONS = ("EMPLOYER_IDLE", "RAIN_OFF")
OVERRIDES = {"PRESENT": {"first_half": "PRESENT", "second_half": "PRESENT"},
             "HALF_DAY": {"first_half": "PRESENT", "second_half": "ABSENT"},
             "ABSENT": {"first_half": "ABSENT", "second_half": "ABSENT"},
             "IDLE": {"first_half": "PRESENT", "second_half": "PRESENT"}}
_REPLAYS: Dict[tuple, dict] = {}


def _crew(db: Session, company_id: int, *, team_id: Optional[int], branch_id: Optional[int], d: date) -> List[int]:
    if team_id is not None:
        from app.models.teams import TeamMembership
        from app.services import team_service

        rows = db.query(TeamMembership.employee_id).filter(
            TeamMembership.company_id == company_id, TeamMembership.team_id == team_id,
            team_service._active_on(TeamMembership, d)).distinct().all()
        return sorted(e for (e,) in rows)
    if branch_id is not None:
        from sqlalchemy import or_

        from app.models.org import EmployeeAssignment

        rows = db.query(EmployeeAssignment.employee_id).filter(
            EmployeeAssignment.company_id == company_id, EmployeeAssignment.branch_id == branch_id,
            EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.effective_from <= d,
            or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= d)).distinct().all()
        return sorted(e for (e,) in rows)
    raise bad_request("Choose a team or a site.", "INVALID_FIELDS")


def _covered(db: Session, principal: Principal, employee_id: int) -> bool:
    return principal.has("attendance:muster") and authz.can_access_employee(db, principal, "attendance:muster",
                                                                            employee_id)


def grid(db: Session, principal: Principal, *, team_id: Optional[int], branch_id: Optional[int], d: date) -> dict:
    from app.services import attendance_access, attendance_service

    attendance_access.require_v2(db, principal.company_id)
    if not principal.has("attendance:muster"):
        raise forbidden()
    cid = principal.company_id
    ids = [e for e in _crew(db, cid, team_id=team_id, branch_id=branch_id, d=d)
           if e != principal.employee_id and _covered(db, principal, e)]
    employees = {e.id: e for e in db.query(Employee).filter(Employee.company_id == cid, Employee.id.in_(ids or [0]))}
    sessions: Dict[int, List[Attendance]] = {}
    for s in (db.query(Attendance).filter(Attendance.company_id == cid, Attendance.employee_id.in_(ids or [0]),
                                          Attendance.date == d, Attendance.voided_at.is_(None))
              .order_by(Attendance.check_in_time)):
        sessions.setdefault(s.employee_id, []).append(s)
    open_requests: Dict[int, int] = {}
    for (e,) in db.query(AttendanceRequest.employee_id).filter(
            AttendanceRequest.company_id == cid, AttendanceRequest.employee_id.in_(ids or [0]),
            AttendanceRequest.shift_date == d, AttendanceRequest.status.in_(("PENDING", "RETURNED"))):
        open_requests[e] = open_requests.get(e, 0) + 1
    rows = []
    for employee_id in ids:
        employee = employees.get(employee_id)
        if employee is None:
            continue
        zone = attendance_service.work_zone(db, employee, d)
        day = ar._day_row(db, cid, employee_id, d)

        def hm(moment: Optional[datetime]) -> Optional[str]:
            return moment.astimezone(zone).strftime("%H:%M") if moment else None

        rows.append({
            "employee_id": employee_id, "name": employee.full_name,
            "employee_code": getattr(employee, "employee_code", None), "work_timezone": zone.zone,
            "sessions": [{"id": s.id, "in": hm(s.check_in_time), "out": hm(s.check_out_time),
                          "source": s.capture_source} for s in sessions.get(employee_id, [])],
            "expected_portion": day.expected_portion if day is not None else None,
            "day_result": day.day_result if day is not None else None,
            "evaluation_state": day.evaluation_state if day is not None else None,
            "open_requests": open_requests.get(employee_id, 0),
        })
    return {"date": d.isoformat(), "team_id": team_id, "branch_id": branch_id, "rows": rows,
            "marks": list(MUSTER_MARKS)}


def _wall(zone, d: date, value: Any, field: str) -> datetime:
    try:
        parsed = time.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise bad_request(f"{field} must be HH:MM.", "INVALID_FIELDS")
    return zone.localize(datetime.combine(d, parsed))


def _refused(employee_id: Any, code: str, message: str) -> dict:
    return {"employee_id": employee_id, "status": "REFUSED", "code": code, "message": message}


def submit(db: Session, principal: Principal, data: Mapping[str, Any]) -> dict:
    from app.services import attendance_access, attendance_service, attendance_sessions
    from app.services import attendance_capture_policy

    cid = principal.company_id
    attendance_access.require_v2(db, cid)
    if not principal.has("attendance:muster"):
        raise forbidden()
    d = ar._as_date(data.get("date"))
    rows = data.get("rows") or []
    if not isinstance(rows, list) or not rows or len(rows) > MAX_ROWS:
        raise bad_request(f"Send 1 to {MAX_ROWS} rows.", "INVALID_FIELDS")
    batch = data.get("offline_batch_id")
    replay_key = (cid, principal.user_id, str(batch)) if batch else None
    if replay_key is not None and replay_key in _REPLAYS:
        return {**_REPLAYS[replay_key], "replayed": True}
    team_id, branch_id = data.get("team_id"), data.get("branch_id")
    crew = set(_crew(db, cid, team_id=team_id, branch_id=branch_id, d=d)) if (team_id or branch_id) else None
    results: List[dict] = []
    for item in rows:
        employee_id = item.get("employee_id") if isinstance(item, Mapping) else None
        mark = item.get("mark") if isinstance(item, Mapping) else None
        if not isinstance(employee_id, int) or isinstance(employee_id, bool):
            results.append(_refused(employee_id, "NOT_IN_SCOPE", "Not in your crew on this date."))
            continue
        if authz.is_self(principal, employee_id):
            results.append(_refused(employee_id, "SELF_EDIT_FORBIDDEN", "You cannot muster yourself."))
            continue
        exists = db.query(Employee.id).filter(Employee.company_id == cid, Employee.id == employee_id).first()
        if exists is None or not _covered(db, principal, employee_id) or (crew is not None and employee_id not in crew):
            results.append(_refused(employee_id, "NOT_IN_SCOPE", "Not in your crew on this date."))
            continue
        if mark not in MUSTER_MARKS:
            results.append(_refused(employee_id, "INVALID_FIELDS", "Unknown mark."))
            continue
        try:
            with db.begin_nested():
                if mark == "IN_OUT":
                    employee = db.get(Employee, employee_id)
                    attendance_service.lock_employee(db, employee_id)
                    zone = attendance_service.work_zone(db, employee, d)
                    start = _wall(zone, d, item.get("in_time"), "in_time")
                    end = _wall(zone, d, item.get("out_time"), "out_time")
                    if end <= start:
                        end += timedelta(days=1)
                    if end > ar._now():
                        raise ar.unprocessable("Times cannot be in the future.", "NOT_ELIGIBLE")
                    attendance_sessions.check_overlap(db, cid, employee_id, start, end)
                    policy = attendance_capture_policy.effective(db, cid, employee_id, d)
                    seq = attendance_sessions.next_session_seq(db, cid, employee_id, d,
                                                               policy.multiple_sessions_allowed)
                    where = ar.placement_on(db, cid, employee_id, d)
                    session = Attendance(
                        company_id=cid, employee_id=employee_id, date=d, check_in_time=start, check_out_time=end,
                        shift_id=employee.shift_id, is_manual=True, source="supervisor", capture_source="SUPERVISOR",
                        status="present", recorded_by_user_id=principal.user_id, session_seq=seq,
                        legal_entity_id=where.legal_entity_id, branch_id=where.branch_id, work_timezone=zone.zone)
                    session.occupied_until = attendance_sessions.occupied_until(session, policy.max_session_hours)
                    attendance_service._insert_day(db, session)
                    for direction, at in (("IN", start), ("OUT", end)):
                        punch = attendance_sessions.record_punch(
                            db, company_id=cid, employee_id=employee_id, source="SUPERVISOR", direction=direction,
                            occurred_at=at, session=session, shift_date=d, actor_user_id=principal.user_id,
                            reason=item.get("reason_code") or "SUPERVISOR_INSTRUCTION")
                        if direction == "IN":
                            session.first_punch_id = punch.id
                        else:
                            session.last_punch_id = punch.id
                    attendance_service.record_version(db, session, "CREATED", principal.user_id, bump=False)
                    if d <= ar.employee_today(db, employee):
                        from app.services import attendance_days

                        attendance_days.evaluate_range(db, cid, [employee_id], d, d, trigger="PUNCH",
                                                       actor_user_id=principal.user_id)
                    results.append({"employee_id": employee_id, "status": "APPLIED", "session_id": session.id})
                else:
                    reason = item.get("reason_code") or ("RAIN_OFF" if mark == "IDLE" else "SUPERVISOR_INSTRUCTION")
                    if mark == "IDLE" and reason not in IDLE_REASONS:
                        raise ar.unprocessable("An idle day needs the reason Work stopped by employer or Rain.",
                                               "NOT_ELIGIBLE", {"missing": "REASON"})
                    row = ar.create(db, principal, {"employee_id": employee_id, "shift_date": d.isoformat(),
                                                    "kind": "STATUS_OVERRIDE", "mode": "DIRECT",
                                                    "payload": OVERRIDES[mark], "reason_code": reason,
                                                    "comment": item.get("comment")}, via_muster=True)
                    results.append({"employee_id": employee_id, "status": "APPLIED", "request_id": row.id})
        except AppError as exc:
            code = exc.code if exc.status_code != 404 else "NOT_IN_SCOPE"
            results.append(_refused(employee_id, code, exc.detail if code != "NOT_IN_SCOPE"
                                    else "Not in your crew on this date."))
    db.flush()
    AuditLogService.log(db, principal, ar.AUDIT_MODULE, "MUSTER_SUBMITTED",
                        f"Muster for {d.isoformat()}: {sum(r['status'] == 'APPLIED' for r in results)} of "
                        f"{len(results)} rows applied", "attendance_muster", None,
                        details={"date": d.isoformat(), "team_id": team_id, "branch_id": branch_id,
                                 "results": [{k: r.get(k) for k in ("employee_id", "status", "code")}
                                             for r in results]})
    body = {"date": d.isoformat(), "results": results,
            "applied": sum(r["status"] == "APPLIED" for r in results),
            "refused": sum(r["status"] == "REFUSED" for r in results)}
    if replay_key is not None:
        _REPLAYS[replay_key] = body
    return body

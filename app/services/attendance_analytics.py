"""Attendance analytics: the live board and the today tiles (AS-M9, G-20; 10-analytics §2, §3.5, §4.1).

One scoped population per request computes both the tiles and the rows (rule D-1 "count = list"): the employees
in the viewer's ``attendance:read`` scope (beyond SELF; scope applied before totals, P-2) who are employed on the
date (P-1), filtered by branch, department, team (membership on the date, P-3) and site (site of the first punch).

Live status of a person (§2.4 classes at the current moment, §3.5 chips):

=================  ================================================================================================
``WORKING``         a live session (no OUT), not on a break
``ON_LUNCH``        a live session with an open break
``COMPLETED``       every session of the date is closed
``IN_FIELD``        approved on-duty / remote work, or a live session in work mode REMOTE / FIELD
``NOT_YET_IN``      expected today, no session, ``expected_start + grace`` has passed (No-show badge after 120 min)
``NOT_STARTED``     expected, no session, the shift has not started yet (incl. halves moved by half-day leave)
``ABSENT``          a closed date (PROVISIONAL / FINAL) evaluated ABSENT
``UNMARKED``        expected with no shift time (OPEN / FLEXIBLE) and no session, or evaluated UNMARKED
``ON_LEAVE``        the expected portion is removed by approved leave
``OFF``             weekly off, holiday, rostered OFF (``working_portion = NONE``)
=================  ================================================================================================

``LATE`` is a flag on WORKING / ON_LUNCH / COMPLETED rows (unexcused late mark, or IN after start + grace).
Tiles: Expected (K-AS-01, portion units), Present people / units (K-AS-02 / -03), Late (K-AS-08), Not yet in
(K-AS-07), Absent (K-AS-04 with composition), On leave (K-AS-05), Off (K-AS-06), On duty / remote (K-AS-39) and
the identity check D-2. Values are exact decimal strings; operational figures are masked, never suppressed (S-1).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

import pytz
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core import authz, clock
from app.core.errors import bad_request, forbidden, not_found
from app.core.principal import Principal
from app.models.attendance import Attendance, AttendanceBreak, AttendanceDay, AttendancePunch
from app.models.employee import Employee
from app.models.teams import TeamMembership
from app.models.time_inputs import OnDutyRequest

READ = "attendance:read"
STATUSES = ("NOT_YET_IN", "WORKING", "ON_LUNCH", "IN_FIELD", "COMPLETED", "NOT_STARTED", "ABSENT", "UNMARKED",
            "ON_LEAVE", "OFF")
STATUS_LABELS = {"NOT_YET_IN": "Not yet in", "WORKING": "Working", "ON_LUNCH": "On lunch", "IN_FIELD": "In field",
                 "COMPLETED": "Completed", "NOT_STARTED": "Shift not started", "ABSENT": "Absent",
                 "UNMARKED": "Unmarked", "ON_LEAVE": "On leave", "OFF": "Off today"}
QUICK = {"in_office": ("WORKING", "ON_LUNCH"), "remote": ("IN_FIELD",), "on_lunch": ("ON_LUNCH",),
         "late": (), "not_in": ("NOT_YET_IN", "ABSENT", "UNMARKED")}
NO_SHOW_MINUTES = 120
PORTION_UNITS = {"FULL": Decimal("1.0"), "FIRST_HALF": Decimal("0.5"), "SECOND_HALF": Decimal("0.5"),
                 "NONE": Decimal("0")}
SORT_ORDER = {s: i for i, s in enumerate(STATUSES)}


def _units(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.1")), "f")


def _grace(db: Session, company_id: int, on: date) -> int:
    from app.payroll.services import settings as settings_service

    try:
        value = settings_service.resolve(db, company_id, "attendance.grace_minutes", on=on).value
        return int(value) if value is not None else 15
    except Exception:  # noqa: BLE001 - the tenant default when the key cannot be resolved
        return 15


def _population(db: Session, principal: Principal, *, department_id: Optional[int], team_id: Optional[int],
                on: date) -> List[Employee]:
    if not beyond_self(principal, READ):
        raise forbidden()
    scope = authz.employee_scope(db, principal, READ)
    query = db.query(Employee).filter(Employee.company_id == principal.company_id, scope.clause(Employee.id),
                                      Employee.hire_date <= on,
                                      or_(Employee.termination_date.is_(None), Employee.termination_date >= on))
    if department_id is not None:
        query = query.filter(Employee.department_id == department_id)
    if team_id is not None:
        members = db.query(TeamMembership.employee_id).filter(
            TeamMembership.company_id == principal.company_id, TeamMembership.team_id == team_id,
            TeamMembership.effective_from <= on,
            or_(TeamMembership.effective_to.is_(None), TeamMembership.effective_to >= on))
        query = query.filter(Employee.id.in_(members))
    return query.order_by(Employee.first_name, Employee.last_name, Employee.id).all()


def beyond_self(principal: Principal, code: str) -> bool:
    """A grant of ``code`` at any scope other than SELF (spine §7.8 "Team today, Insights")."""
    return bool(principal.scopes(code) - {"SELF"})


def board(db: Session, principal: Principal, *, on: Optional[date] = None, branch_id: Optional[int] = None,
          department_id: Optional[int] = None, team_id: Optional[int] = None, site_id: Optional[int] = None,
          q: Optional[str] = None, status: Sequence[str] = (), quick: Optional[str] = None,
          sort: str = "status", skip: int = 0, limit: int = 25) -> dict:
    """``GET /attendance/live-board``: tiles + one page of rows from the same population."""
    from app.payroll.services import time_inputs as ti

    if not beyond_self(principal, READ):
        raise forbidden()
    unknown = [s for s in status if s not in STATUSES]
    if unknown:
        raise bad_request(f"Unknown status {unknown[0]}.", "INVALID_FILTER")
    if quick is not None and quick not in QUICK:
        raise bad_request("Unknown quick filter.", "INVALID_FILTER")
    company_id = principal.company_id
    now = clock.utcnow().replace(microsecond=0)
    today = clock.company_today(db, company_id)
    on = on or today
    if (on - today).days > 0 or (today - on).days > 366:
        raise bad_request("Choose a date in the last year.", "INVALID_RANGE")
    employees = _population(db, principal, department_id=department_id, team_id=team_id, on=on)
    ids = [e.id for e in employees]
    facts = ti.day_facts_many(db, company_id, ids, on, on) if ids else {}
    expectations = ti.expected_presence(db, company_id, ids, on, on, facts=facts) if ids else {}
    if branch_id is not None:
        if not any((f[0].branch_id if f else None) == branch_id for f in facts.values()):
            raise not_found("Branch")
        employees = [e for e in employees if facts.get(e.id) and facts[e.id][0].branch_id == branch_id]
        ids = [e.id for e in employees]
    sessions: Dict[int, List[Attendance]] = defaultdict(list)
    for s in db.query(Attendance).filter(Attendance.company_id == company_id, Attendance.employee_id.in_(ids or [0]),
                                         Attendance.date == on, Attendance.voided_at.is_(None)) \
            .order_by(Attendance.check_in_time):
        sessions[s.employee_id].append(s)
    open_breaks = {b.attendance_id for b in db.query(AttendanceBreak.attendance_id).filter(
        AttendanceBreak.company_id == company_id,
        AttendanceBreak.attendance_id.in_([s.id for rows in sessions.values() for s in rows] or [0]),
        AttendanceBreak.ended_at.is_(None))}
    days = {d.employee_id: d for d in db.query(AttendanceDay).filter(
        AttendanceDay.company_id == company_id, AttendanceDay.employee_id.in_(ids or [0]),
        AttendanceDay.shift_date == on)}
    on_duty = {r.employee_id: r for r in db.query(OnDutyRequest).filter(
        OnDutyRequest.company_id == company_id, OnDutyRequest.employee_id.in_(ids or [0]),
        OnDutyRequest.date == on, OnDutyRequest.status == "APPROVED")}
    first_punch_ids = [rows[0].first_punch_id for rows in sessions.values() if rows and rows[0].first_punch_id]
    punch_sites = {p.id: p.site_id for p in db.query(AttendancePunch.id, AttendancePunch.site_id).filter(
        AttendancePunch.company_id == company_id, AttendancePunch.id.in_(first_punch_ids or [0]))}
    grace = timedelta(minutes=_grace(db, company_id, on))
    live_date = on == today

    rows = []
    tiles = {"expected": Decimal(0), "present_units": Decimal(0), "present_people": 0, "missing_punch_units":
             Decimal(0), "late": 0, "not_yet_in": 0, "on_leave_units": Decimal(0), "off": 0,
             "on_duty_units": Decimal(0), "composition": defaultdict(Decimal), "no_show": 0}
    for e in employees:
        fact = (facts.get(e.id) or [None])[0]
        exp = (expectations.get(e.id) or [None])[0]
        if fact is None or not fact.employed:
            continue
        mine = sessions.get(e.id, [])
        day = days.get(e.id)
        expected = PORTION_UNITS.get(exp.expected_portion if exp else "NONE", Decimal(0))
        start = min((s for s, _ in fact.instances), default=None) if fact.instances else None
        if day is not None and day.expected_start_at is not None:
            start = day.expected_start_at
        live = [s for s in mine if s.check_out_time is None and s.closed_at is None]
        work_mode = next((s.work_mode for s in live or mine), None)
        late_by = None
        if mine and start is not None and mine[0].check_in_time > start + grace:
            late_by = int((mine[0].check_in_time - start).total_seconds() // 60)
        late = bool(day.late_mark and not day.late_excused) if day is not None else late_by is not None
        duty = on_duty.get(e.id)
        if not mine and (duty is not None or (exp is not None and exp.on_duty_portion != "NONE")):
            state = "IN_FIELD"
        elif live:
            state = "ON_LUNCH" if any(s.id in open_breaks for s in live) else (
                "IN_FIELD" if work_mode in ("REMOTE", "FIELD") else "WORKING")
        elif mine:
            state = "COMPLETED"
        elif expected == 0:
            state = "ON_LEAVE" if exp is not None and exp.on_leave_portion != "NONE" else "OFF"
        elif day is not None and day.evaluation_state in ("PROVISIONAL", "FINAL"):
            state = {"ABSENT": "ABSENT", "UNMARKED": "UNMARKED"}.get(day.day_result, "ABSENT")
        elif start is None:
            state = "UNMARKED"
        elif not live_date or now >= start + grace:
            state = "NOT_YET_IN" if live_date else "ABSENT"
        else:
            state = "NOT_STARTED"
        no_show = state == "NOT_YET_IN" and start is not None and now >= start + timedelta(minutes=NO_SHOW_MINUTES)
        # Tiles over the same population (identity D-2: expected = present + on duty + absent).
        tiles["expected"] += expected
        if exp is not None and exp.on_leave_portion != "NONE":
            tiles["on_leave_units"] += PORTION_UNITS.get(exp.on_leave_portion, Decimal(0))
        if fact.working_portion == "NONE":
            tiles["off"] += 1
        if expected:
            if state in ("WORKING", "ON_LUNCH", "COMPLETED"):
                tiles["present_units"] += expected
                tiles["present_people"] += 1
                if day is not None and day.day_result == "MISSING_PUNCH":
                    tiles["missing_punch_units"] += expected
            elif state == "IN_FIELD":
                tiles["on_duty_units"] += expected
            else:
                tiles["composition"][state] += expected
        if late and state in ("WORKING", "ON_LUNCH", "COMPLETED", "IN_FIELD"):
            tiles["late"] += 1
        if state == "NOT_YET_IN":
            tiles["not_yet_in"] += 1
        tiles["no_show"] += 1 if no_show else 0
        site = punch_sites.get(mine[0].first_punch_id) if mine else None
        rows.append({
            "employee_id": e.id, "name": e.full_name, "employee_code": e.employee_code,
            "department_id": e.department_id, "branch_id": fact.branch_id, "status": state,
            "status_label": STATUS_LABELS[state], "late": late and state != "NOT_YET_IN", "late_by_minutes": late_by,
            "no_show": no_show, "first_in_at": mine[0].check_in_time.isoformat() if mine else None,
            "last_out_at": next((s.check_out_time.isoformat() for s in reversed(mine) if s.check_out_time), None),
            "expected_start_at": start.isoformat() if start else None, "work_mode": work_mode,
            "site_id": site, "expected_units": _units(expected), "evaluation_state": day.evaluation_state if day else
            "OPEN", "day_result": day.day_result if day else None,
            "exceptions": list(day.exception_codes or []) if day else [],
        })
    # Filters that narrow the list only (the tiles stay the selection's figures, a tile click sets `status`).
    listed = rows
    if site_id is not None:
        listed = [r for r in listed if r["site_id"] == site_id]
    if q:
        needle = q.strip().lower()
        listed = [r for r in listed if needle in r["name"].lower() or needle in (r["employee_code"] or "").lower()]
    if status:
        listed = [r for r in listed if r["status"] in status]
    if quick == "late":
        listed = [r for r in listed if r["late"]]
    elif quick is not None:
        listed = [r for r in listed if r["status"] in QUICK[quick]]
    if sort == "name":
        listed.sort(key=lambda r: (r["name"].lower(), r["employee_id"]))
    elif sort == "first_in":
        listed.sort(key=lambda r: (r["first_in_at"] is None, r["first_in_at"] or "", r["employee_id"]))
    elif sort == "late_by":
        listed.sort(key=lambda r: (-(r["late_by_minutes"] or -1), r["employee_id"]))
    else:
        listed.sort(key=lambda r: (SORT_ORDER[r["status"]], r["name"].lower(), r["employee_id"]))
    counts = defaultdict(int)
    for r in rows:
        counts[r["status"]] += 1
    absent = sum(tiles["composition"].values(), Decimal(0))
    identity = tiles["expected"] == tiles["present_units"] + tiles["on_duty_units"] + absent
    zone = _zone(db, company_id)
    return {
        "date": on.isoformat(), "live": live_date, "as_of": now.isoformat(), "zone": zone,
        "zone_now": now.astimezone(pytz.timezone(zone)).strftime("%H:%M"),
        "tiles": [
            {"kpi": "K-AS-01", "key": "expected", "label": "Expected", "value": _units(tiles["expected"]),
             "unit": "PORTION_UNITS"},
            {"kpi": "K-AS-02", "key": "present_people", "label": "Present (people)",
             "value": str(tiles["present_people"]), "unit": "PEOPLE"},
            {"kpi": "K-AS-03", "key": "present_units", "label": "Present (portion units)",
             "value": _units(tiles["present_units"]), "unit": "PORTION_UNITS",
             "composition": {"MISSING_PUNCH": _units(tiles["missing_punch_units"])}},
            {"kpi": "K-AS-08", "key": "late", "label": "Late today", "value": str(tiles["late"]), "unit": "PEOPLE"},
            {"kpi": "K-AS-07", "key": "not_yet_in", "label": "Not yet in", "value": str(tiles["not_yet_in"]),
             "unit": "PEOPLE", "no_show": tiles["no_show"]},
            {"kpi": "K-AS-04", "key": "absent", "label": "Absent", "value": _units(absent), "unit": "PORTION_UNITS",
             "composition": {k: _units(v) for k, v in sorted(tiles["composition"].items())}},
            {"kpi": "K-AS-05", "key": "on_leave", "label": "On leave", "value": _units(tiles["on_leave_units"]),
             "unit": "PORTION_UNITS"},
            {"kpi": "K-AS-06", "key": "off", "label": "Off today", "value": str(tiles["off"]), "unit": "PEOPLE"},
            {"kpi": "K-AS-39", "key": "on_duty", "label": "On duty / remote", "value": _units(tiles["on_duty_units"]),
             "unit": "PORTION_UNITS"},
        ],
        "counts": {s: counts.get(s, 0) for s in STATUSES},
        "consistency": {"expected_equals_present_plus_on_duty_plus_absent": identity},
        "items": listed[skip:skip + limit], "total": len(listed), "skip": skip, "limit": limit,
    }


def _zone(db: Session, company_id: int) -> str:
    from app.models.company import Company

    return db.get(Company, company_id).timezone or "UTC"

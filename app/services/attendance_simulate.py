"""Impact preview of an attendance policy version (AS 04-attendance/02 §5.4; D-AS-51) — AS-M3.

``simulate(db, principal, policy_id, version_no, ...)`` answers, without writing anything: who the version covers now
and after (with the stored or proposed assignment rules), how many employee-dates of the bounded window would be
recomputed per payroll state, and what would change — ``day_result`` transitions, late marks added / removed, half
days, absences, missing punches, OT-candidate and night minutes — with a deterministic sample of the 20 employees with
the most changed days. The draft is substituted into the inputs the real path loads (``attendance_days
.load_inputs_many``) and evaluated by the same pure ``day_evaluation.evaluate_day``: the preview path is the real path.

Window: ``max(effective_from, today - 62)`` to yesterday (``bounded`` when capped); future dates have no evaluation
yet. At most 500 covered employees are evaluated (``bounded`` again). Services never commit.

A capture version never switches a day (02 §4.1): its preview is the population plus "punches that would be blocked
today" — the covered people's punches of the last 7 days re-checked against the draft's sources, location and geofence
by the live pure check (``attendance_sites.evaluate_location``). Only an IP hash is stored, so a draft with
``ip_mode BLOCK`` cannot re-check the network: those punches are counted in ``ip_not_rechecked``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date, timedelta
from typing import Dict, List, Mapping, Optional, Sequence

from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import bad_request
from app.core.principal import Principal
from app.models.employee import Employee
from app.payroll import policy_types as pt
from app.payroll.services import policies
from app.services import attendance_access, attendance_days
from app.services import day_evaluation as de

MAX_BACK_DAYS = attendance_days.RECOMPUTE_MAX_BACK_DAYS
MAX_EMPLOYEES = attendance_days.MAX_EMPLOYEES
SAMPLE_SIZE = 20
EVALUATED_TYPES = attendance_days.MODE_TYPES  # rules, request, scheduling (capture never switches a day)
CAPTURE_TYPE = "ATTENDANCE_CAPTURE_POLICY"
BLOCKED_LOOKBACK_DAYS = 7
INTERACTIVE_SOURCES = ("WEB", "MOBILE")


def _rules(raw: Sequence[Mapping], policy_id: int) -> List[pt.AssignmentRule]:
    def day(value):
        return date.fromisoformat(value) if isinstance(value, str) else value

    out = []
    for n, item in enumerate(raw):
        out.append(pt.AssignmentRule(rule_id=-(n + 1), policy_id=policy_id, priority=int(item.get("priority") or 0),
                                     criteria=item.get("criteria") or {"everyone": True},
                                     effective_from=day(item.get("effective_from")),
                                     effective_to=day(item.get("effective_to"))))
    return out


def _covered(rules: Sequence[pt.AssignmentRule], people: Mapping[int, pt.EmployeeAttributes], policy_id: int,
             on: date) -> set:
    found = set()
    for employee_id, person in people.items():
        result = pt.resolve_assignment(rules, person, on=on)
        if result.status == "RESOLVED" and int(result.rule.policy_id) == policy_id:
            found.add(employee_id)
    return found


def simulate(db: Session, principal: Principal, policy_id: int, version_no: int, *,
             effective_from: Optional[date] = None, assignments: Optional[Sequence[Mapping]] = None,
             apply_to_locked_periods: bool = False) -> dict:
    company_id = principal.company_id
    policy = policies._load(db, company_id, policy_id)
    if policy.type not in EVALUATED_TYPES and policy.type != CAPTURE_TYPE:
        raise bad_request("The impact preview covers attendance rules, capture, request and scheduling policies.",
                          "SIMULATE_NOT_SUPPORTED")
    version = policies._version(db, company_id, policy_id, version_no)
    draft = dict(version.fields or {})
    today = clock.company_today(db, company_id)
    start_wanted = effective_from or version.effective_from or today
    start = max(start_wanted, today - timedelta(days=MAX_BACK_DAYS))
    end = today - timedelta(days=1)
    bounded = start > start_wanted
    v2 = attendance_access.v2_enabled(db, company_id)

    employee_ids = [e for (e,) in db.query(Employee.id).filter(Employee.company_id == company_id)
                    .order_by(Employee.id).all()]
    anchor = min(max(start, end), today)
    people = {a.employee_id: a for a in policies.employee_attributes(db, company_id, anchor, employee_ids)}
    stored_all = policies._type_rules(db, company_id, policy.type)
    mine_stored = [r for r in stored_all if int(r.policy_id) == policy_id]
    others = [r for r in stored_all if int(r.policy_id) != policy_id]
    proposed = _rules(assignments, policy_id) if assignments is not None else mine_stored
    covered_now = _covered(stored_all, people, policy_id, anchor)
    covered_after = _covered(others + list(proposed), people, policy_id, anchor)
    joining, leaving = sorted(covered_after - covered_now), sorted(covered_now - covered_after)
    names = {e.id: f"{e.first_name} {e.last_name or ''}".strip() for e in db.query(Employee).filter(
        Employee.company_id == company_id, Employee.id.in_((joining + leaving)[:100] or [-1]))}
    population = {"covered_now": len(covered_now), "covered_after": len(covered_after),
                  "joining": [{"employee_id": e, "name": names.get(e)} for e in joining[:50]],
                  "joining_count": len(joining),
                  "leaving": [{"employee_id": e, "name": names.get(e)} for e in leaving[:50]],
                  "leaving_count": len(leaving)}
    result = {"policy_id": policy_id, "version": version_no, "type": policy.type, "window": None,
              "population": population, "dates": {"OPEN": 0, "INPUT_LOCKED": 0, "FINALIZED": 0},
              "changes": {}, "sample": [], "bounded": bounded, "v2_active": v2}
    if policy.type == CAPTURE_TYPE:
        result["changes"] = blocked_punches(db, company_id, sorted(covered_after)[:MAX_EMPLOYEES], draft, today)
        result["bounded"] = bounded or len(covered_after) > MAX_EMPLOYEES
        return result
    if end < start or not covered_after:
        return result
    result["window"] = {"start": start.isoformat(), "end": end.isoformat()}
    evaluated = sorted(covered_after)[:MAX_EMPLOYEES]
    result["bounded"] = bounded or len(covered_after) > MAX_EMPLOYEES

    transitions: Counter = Counter()
    counts: Counter = Counter()
    changed_days: Dict[int, List[dict]] = defaultdict(list)
    now = clock.utcnow()
    cursor = start
    while cursor <= end:
        stop = min(end, cursor + timedelta(days=attendance_days.MAX_DAYS - 1))
        states = attendance_days.period_states(db, company_id, evaluated, cursor, stop, now=now)
        for state in states.values():
            if state == "OPEN" or apply_to_locked_periods:
                result["dates"][state] += 1
        inputs = attendance_days.load_inputs_many(db, company_id, evaluated, cursor, stop, now=now, v2=v2)
        stored = attendance_days.days_for(db, company_id, evaluated, cursor, stop)
        for key in sorted(inputs):
            if states.get(key, "OPEN") != "OPEN" and not apply_to_locked_periods:
                continue
            current = inputs[key]
            before = stored.get(key)
            before_ev = None if before is not None else de.evaluate_day(current)
            after_inputs = _with_draft(current, policy.type, draft, int(version.id), v2)
            after = de.evaluate_day(after_inputs)
            b = _view(before, before_ev)
            a = _view(None, after)
            if a == b:
                continue
            employee_id, day = key
            if b["day_result"] != a["day_result"]:
                transitions[f"{b['day_result']} -> {a['day_result']}"] += 1
            counts["late_marks_added"] += int(a["late_mark"] and not b["late_mark"])
            counts["late_marks_removed"] += int(b["late_mark"] and not a["late_mark"])
            counts["half_days"] += int(a["day_result"] == "HALF_DAY") - int(b["day_result"] == "HALF_DAY")
            counts["absences"] += int(a["day_result"] == "ABSENT") - int(b["day_result"] == "ABSENT")
            counts["missing_punches"] += int(a["day_result"] == "MISSING_PUNCH") - int(b["day_result"] == "MISSING_PUNCH")
            counts["ot_candidate_minutes_delta"] += a["ot_candidate_minutes"] - b["ot_candidate_minutes"]
            counts["night_minutes_delta"] += a["night_minutes"] - b["night_minutes"]
            counts["changed_days"] += 1
            changed_days[employee_id].append({"date": day.isoformat(), "before": b, "after": a})
        cursor = stop + timedelta(days=1)
    result["changes"] = {"day_result": dict(sorted(transitions.items())), **dict(counts)}
    ranked = sorted(changed_days, key=lambda e: (-len(changed_days[e]), e))[:SAMPLE_SIZE]
    result["sample"] = [{"employee_id": e, "days": changed_days[e][:31]} for e in ranked]
    return result


def _with_draft(inputs: de.DayInputs, type_code: str, draft: Mapping, version_id: int, v2: bool) -> de.DayInputs:
    """The inputs as they would be with the draft version applying to the employee on the date (§5.4)."""
    ids = dict(inputs.policy_version_ids)
    ids[type_code] = version_id
    standard = v2 and (str(draft.get("profile") or "STANDARD") == "STANDARD" or inputs.rules_mode == de.STANDARD)
    rules = de.RulesParams.from_fields(draft) if type_code == "ATTENDANCE_RULES_POLICY" else inputs.rules
    return replace(inputs, rules=rules, rules_mode=de.STANDARD if standard else de.LEGACY_V1, policy_version_ids=ids)


def _view(row, evaluation: Optional[de.DayEvaluation]) -> dict:
    """The compared outputs of a stored row or a fresh evaluation (no evidence, no reasons)."""
    source = row if row is not None else evaluation
    return {"day_result": source.day_result, "late_mark": bool(source.late_mark),
            "early_mark": bool(source.early_mark), "net_minutes": source.net_minutes,
            "ot_candidate_minutes": int(source.ot_candidate_minutes or 0),
            "night_minutes": int(source.night_minutes or 0)}


def would_block(punch_source: str, fix, sites: Sequence, policy) -> Optional[str]:
    """Pure: why the draft capture ``policy`` would refuse this punch now, or None. ``fix`` is the stored location
    (``attendance_sites.LocationFix``; None = no reading); IP is never re-checked here (hash only)."""
    from app.services import attendance_sites

    if punch_source not in policy.allowed_sources:
        return "SOURCE_NOT_ALLOWED"
    if punch_source not in INTERACTIVE_SOURCES:
        return None  # devices and kiosks are not located by the employee's phone
    if fix is None and policy.location_mode == "REQUIRED" and policy.location_failure_action == "BLOCK":
        return "LOCATION_REQUIRED"
    if policy.geofence_mode == "OFF" and policy.qr_mode == "OFF":
        return None
    outcome = attendance_sites.evaluate_location(list(sites), fix or attendance_sites.LocationFix(),
                                                 replace(policy, ip_mode="OFF"))
    return outcome.refusal[0] if outcome.refusal is not None else None


def blocked_punches(db: Session, company_id: int, employee_ids: Sequence[int], draft: Mapping, today: date) -> dict:
    """"Punches that would be blocked today: n" for a capture draft (02 §5.4, V-AS-13): the last 7 days of the covered
    people's used punches, re-checked."""
    from app.models.attendance import AttendancePunch, LocationSample
    from app.services import attendance_capture_policy, attendance_sites

    policy = attendance_capture_policy.from_fields(draft)
    since = today - timedelta(days=BLOCKED_LOOKBACK_DAYS)
    punches = (db.query(AttendancePunch)
               .filter(AttendancePunch.company_id == company_id,
                       AttendancePunch.employee_id.in_(list(employee_ids) or [-1]),
                       AttendancePunch.shift_date >= since, AttendancePunch.shift_date <= today,
                       AttendancePunch.disposition.in_(("PENDING", "USED")))
               .order_by(AttendancePunch.id).all())
    sample_ids = {p.location_sample_id for p in punches if p.location_sample_id}
    samples = {x.id: x for x in db.query(LocationSample).filter(LocationSample.company_id == company_id,
                                                                LocationSample.id.in_(sample_ids or {-1}))}
    sites: Dict[int, list] = {}
    reasons: Counter = Counter()
    ip_unchecked = 0
    for punch in punches:
        if punch.employee_id not in sites:
            rows = attendance_sites.sites_for(db, company_id, punch.employee_id, policy, today)
            sites[punch.employee_id] = [attendance_sites.SiteShape.of(r) for r in rows]
        sample = samples.get(punch.location_sample_id)
        fix = None
        if sample is not None and sample.latitude is not None:
            fix = attendance_sites.LocationFix(latitude=sample.latitude, longitude=sample.longitude,
                                               accuracy_m=sample.accuracy_m, fix_age_seconds=sample.fix_age_seconds,
                                               exception_reason=punch.reason)
        why = would_block(punch.source, fix, sites[punch.employee_id], policy)
        if why is not None:
            reasons[why] += 1
        elif policy.ip_mode == "BLOCK" and punch.source in INTERACTIVE_SOURCES:
            ip_unchecked += 1
    return {"punches_checked": len(punches), "punches_blocked": sum(reasons.values()),
            "blocked_by": dict(sorted(reasons.items())), "ip_not_rechecked": ip_unchecked,
            "lookback_days": BLOCKED_LOOKBACK_DAYS}

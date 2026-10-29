"""Country packs, industry bundles and the per-tenant cut-over acceptance (G-22 / G-23).

* **Packs** — per legal entity: its country's WORKING_TIME pack (IN / AE / US, ``working_time_packs``), whether the
  entity adopted it (``attendance.packs.enabled`` at LEGAL_ENTITY scope, saved through the settings engine so change
  control and audit apply) and each value's source. Unverified packs WARN, never BLOCK (IG-AS-31).
* **Fair Workweek estimate** — for a US entity: employer-initiated roster changes inside the pack's notice window
  (14 days) and the predictability pay hours they would owe (1 h per change, 4 h per cancellation inside 24 h);
  changes the employee asked for (a request or a consent on the change row) owe nothing.
* **Bundles** — industry starters (factory 24×7, BPO nights, construction sites, UAE retail split shifts, US retail
  Fair Workweek) that create DRAFT attendance-rules and scheduling policies; nothing applies until an administrator
  activates them.
* **Readiness** — the cut-over checklist (PASS / WARN / FAIL with a link to fix each item); **acceptance run**
  (job ``attendance.acceptance_run``) re-checks it together with read-only scenario probes (every active employee's
  schedule for the next 7 days resolves, every entity's pack resolves, the approval engine can route each module)
  and keeps the report; another administrator signs it off (maker-checker, audit ``ATTENDANCE_ACCEPTANCE_SIGNED``).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, bad_request, forbidden, not_found
from app.core.principal import Principal
from app.models.employee import Employee, Shift
from app.models.jobs import Job
from app.models.org import EmployeeAssignment, LegalEntity
from app.models.payroll_config import Policy, PolicyVersion
from app.payroll.services import jobs
from app.services import pack_resolver, working_time_packs
from app.services.audit_log_service import AuditLogService

MANAGE = "attendance:manage_policy"
ACCEPTANCE_JOB = "attendance.acceptance_run"
NOTICE_PAY = {"CHANGE": Decimal(1), "CANCELLATION_24H": Decimal(4)}


def _require(principal: Principal, *codes: str) -> None:
    if not any(principal.has_company_scope(code) for code in (codes or (MANAGE,))):
        raise forbidden()


def _entities(db: Session, company_id: int) -> List[LegalEntity]:
    return (db.query(LegalEntity).filter(LegalEntity.company_id == company_id, LegalEntity.status == "ACTIVE")
            .order_by(LegalEntity.id).all())


# =============================================================================================== packs
def packs_view(db: Session, principal: Principal) -> dict:
    _require(principal, MANAGE, "shift:manage")
    today = clock.company_today(db, principal.company_id)
    items = []
    for entity in _entities(db, principal.company_id):
        pack = working_time_packs.pack(entity.country_code)
        items.append({
            "legal_entity_id": entity.id, "name": entity.trade_name or entity.legal_name,
            "country": entity.country_code, "has_pack": pack is not None,
            "adopted": pack_resolver.adopted(db, principal.company_id, entity.id, today),
            "verified": bool(pack and pack.get("verified")),
            "daily_max_today": (format(working_time_packs.daily_max_on(pack, today), "f")
                                if pack and working_time_packs.daily_max_on(pack, today) is not None else None),
            "pack": working_time_packs.describe(entity.country_code)["pack"],
        })
    return {"version": working_time_packs.PACK_VERSION, "items": items,
            "available": sorted(working_time_packs.PACKS)}


def set_adoption(db: Session, principal: Principal, legal_entity_id: int, enabled: bool,
                 reason: Optional[str]) -> dict:
    from app.payroll.services import settings as settings_engine

    _require(principal)
    entity = db.query(LegalEntity).filter(LegalEntity.company_id == principal.company_id,
                                          LegalEntity.id == legal_entity_id).first()
    if entity is None:
        raise not_found("Legal entity")
    if enabled and working_time_packs.pack(entity.country_code) is None:
        raise bad_request(f"There is no working-time pack for {entity.country_code} yet.", "PACK_UNAVAILABLE")
    if not (reason or "").strip():
        raise bad_request("Say why the country rules are switched on or off.", "REASON_REQUIRED")
    change = settings_engine.write_module_value(db, principal, pack_resolver.PACK_SWITCH, bool(enabled),
                                                reason=reason.strip()[:500], legal_entity_id=entity.id)
    AuditLogService.log(db, principal, "Attendance", "WORKING_TIME_PACK_ADOPTED" if enabled else
                        "WORKING_TIME_PACK_RELEASED",
                        f"{entity.country_code} working-time pack {'adopted' if enabled else 'switched off'} for "
                        f"{entity.legal_name}", "legal_entity", entity.id,
                        details={"pack_version": working_time_packs.PACK_VERSION})
    return {"change": change, **packs_view(db, principal)}


def fww_estimate(db: Session, principal: Principal, legal_entity_id: int, start: date, end: date) -> dict:
    """Predictability pay hours the employer-initiated changes inside the notice window would owe (US pack)."""
    from app.models.rosters import RosterEntryChange

    _require(principal, MANAGE, "shift:manage")
    if end < start or (end - start).days > 92:
        raise bad_request("Choose a range of at most 93 days.", "INVALID_RANGE")
    entity = db.query(LegalEntity).filter(LegalEntity.company_id == principal.company_id,
                                          LegalEntity.id == legal_entity_id).first()
    if entity is None:
        raise not_found("Legal entity")
    pack = working_time_packs.pack(entity.country_code) or {}
    notice = (pack.get("scheduling") or {}).get("notice_hours")
    if not notice:
        return {"legal_entity_id": entity.id, "applies": False, "notice_hours": None, "total_hours": "0",
                "employees": [], "items": []}
    people = {e for (e,) in db.query(EmployeeAssignment.employee_id).filter(
        EmployeeAssignment.company_id == principal.company_id, EmployeeAssignment.legal_entity_id == entity.id,
        EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.effective_from <= end,
        (EmployeeAssignment.effective_to.is_(None)) | (EmployeeAssignment.effective_to >= start))}
    rows = (db.query(RosterEntryChange)
            .filter(RosterEntryChange.company_id == principal.company_id,
                    RosterEntryChange.employee_id.in_(people or {0}),
                    RosterEntryChange.shift_date >= start, RosterEntryChange.shift_date <= end,
                    RosterEntryChange.initiated_by == "EMPLOYER", RosterEntryChange.change_kind != "CREATED",
                    RosterEntryChange.notice_hours.isnot(None), RosterEntryChange.notice_hours < notice,
                    RosterEntryChange.request_id.is_(None), RosterEntryChange.consent_id.is_(None))
            .order_by(RosterEntryChange.shift_date, RosterEntryChange.id).all())
    names = dict(db.query(Employee.id, Employee.first_name + " " + Employee.last_name)
                 .filter(Employee.company_id == principal.company_id, Employee.id.in_(people or {0})).all())
    totals: Dict[int, Decimal] = {}
    items = []
    for row in rows:
        kind = "CANCELLATION_24H" if row.change_kind == "WITHDRAWN" and row.notice_hours < 24 else "CHANGE"
        hours = NOTICE_PAY[kind]
        totals[row.employee_id] = totals.get(row.employee_id, Decimal(0)) + hours
        items.append({"employee_id": row.employee_id, "employee_name": names.get(row.employee_id),
                      "shift_date": row.shift_date.isoformat(), "change_kind": row.change_kind,
                      "notice_hours": format(Decimal(row.notice_hours).normalize(), "f"), "kind": kind,
                      "hours": format(hours, "f")})
    return {"legal_entity_id": entity.id, "applies": True, "notice_hours": notice,
            "adopted": pack_resolver.adopted(db, principal.company_id, entity.id, end),
            "total_hours": format(sum(totals.values(), Decimal(0)), "f"),
            "employees": [{"employee_id": k, "employee_name": names.get(k), "hours": format(v, "f")}
                          for k, v in sorted(totals.items(), key=lambda kv: -kv[1])],
            "items": items[:500]}


# =============================================================================================== bundles
_RULES, _SCHED = "ATTENDANCE_RULES_POLICY", "SCHEDULING_POLICY"
BUNDLES: Dict[str, Dict[str, Any]] = {
    "IN_FACTORY_24X7": {
        "name": "India factory, 24×7 rotating shifts", "country": "IN",
        "description": "Rosters with three shifts, 48 h a week, 6 days, 12 h rest, fixed minutes for a full day.",
        "policies": [
            (_RULES, {"profile": "STANDARD", "threshold_mode": "ABSOLUTE", "full_day_min_minutes": 480,
                      "half_day_min_minutes": 240, "grace_in_minutes": 10, "absent_marking": "AUTO_ABSENT"}),
            (_SCHED, {"profile": "STANDARD", "schedule_source": "ROSTER", "publish_notice_days": 7,
                      "min_rest_hours": 12, "max_days_per_week": 6, "max_hours_per_week": 48,
                      "max_consecutive_days": 10, "holiday_mode": "WORK_THROUGH"}),
        ]},
    "IN_BPO_NIGHT": {
        "name": "India BPO, night shifts", "country": "IN",
        "description": "Night rosters, acknowledgement of published shifts, 11 h rest, 48 h a week.",
        "policies": [
            (_RULES, {"profile": "STANDARD", "threshold_mode": "PERCENT_OF_EXPECTED", "grace_in_minutes": 5,
                      "open_day_start_time": "12:00"}),
            (_SCHED, {"profile": "STANDARD", "schedule_source": "ROSTER", "publish_notice_days": 7,
                      "min_rest_hours": 11, "max_days_per_week": 6, "max_hours_per_week": 48,
                      "acknowledge_published_shifts": True}),
        ]},
    "IN_CONSTRUCTION_SITE": {
        "name": "India construction sites", "country": "IN",
        "description": "First in / last out at the site, placement shifts, 6 days a week.",
        "policies": [
            (_RULES, {"profile": "STANDARD", "work_time_basis": "FIRST_IN_LAST_OUT", "day_basis": "HOURS",
                      "grace_in_minutes": 15}),
            (_SCHED, {"profile": "STANDARD", "schedule_source": "PLACEMENT", "max_days_per_week": 6,
                      "max_hours_per_week": 48}),
        ]},
    "AE_RETAIL_SPLIT": {
        "name": "UAE retail, split shifts", "country": "AE",
        "description": "Split shifts summed per session, 48 h a week, rosters published a week ahead.",
        "policies": [
            (_RULES, {"profile": "STANDARD", "work_time_basis": "SUM_OF_SESSIONS", "grace_in_minutes": 10}),
            (_SCHED, {"profile": "STANDARD", "schedule_source": "ROSTER", "publish_notice_days": 7,
                      "max_days_per_week": 6, "max_hours_per_week": 48}),
        ]},
    "US_RETAIL_FWW": {
        "name": "US retail, Fair Workweek", "country": "US",
        "description": "Schedules published 14 days ahead, shift acknowledgement, 10 h rest between closing and "
                       "opening, pre-approved overtime.",
        "policies": [
            (_RULES, {"profile": "STANDARD", "ot_requires_pre_approval": True, "grace_in_minutes": 5}),
            (_SCHED, {"profile": "STANDARD", "schedule_source": "ROSTER", "publish_notice_days": 14,
                      "min_rest_hours": 10, "acknowledge_published_shifts": True}),
        ]},
}


def bundles(db: Session, principal: Principal) -> dict:
    _require(principal, MANAGE, "shift:manage")
    names = {n.lower() for (n,) in db.query(Policy.name).filter(Policy.company_id == principal.company_id)}
    return {"items": [{"code": code, "name": b["name"], "country": b["country"], "description": b["description"],
                       "applied": f"{b['name']} – rules".lower() in names,
                       "policies": [{"type": t, "fields": f} for t, f in b["policies"]]}
                      for code, b in BUNDLES.items()]}


def apply_bundle(db: Session, principal: Principal, code: str) -> dict:
    from app.payroll.services import policies

    bundle = BUNDLES.get(code)
    if bundle is None:
        raise not_found("Bundle")
    created = []
    for type_code, fields in bundle["policies"]:
        suffix = "rules" if type_code == _RULES else "schedules"
        policy = policies.create_policy(db, principal, type_code=type_code, name=f"{bundle['name']} – {suffix}",
                                        description=f"Starter from the {code} bundle. Review, assign and activate.",
                                        fields=fields)
        created.append({"policy_id": policy["id"], "type": type_code, "name": policy["name"]})
    AuditLogService.log(db, principal, "Attendance", "ATTENDANCE_BUNDLE_APPLIED", f"Bundle {bundle['name']} added "
                        "as draft policies", "company", principal.company_id,
                        details={"bundle": code, "policies": [c["policy_id"] for c in created]})
    return {"bundle": code, "created": created}


# =============================================================================================== readiness
def _item(key: str, label: str, status: str, detail: str, link: Optional[str] = None) -> dict:
    return {"key": key, "label": label, "status": status, "detail": detail, "link": link}


def checklist(db: Session, company_id: int) -> List[dict]:
    from app.models.attendance_requests import ApprovalRoute
    from app.models.integrations import NotificationChannel
    from app.models.pay_org import HolidayCalendar
    from app.services import attendance_cutover, shift_resolution

    today = clock.company_today(db, company_id)
    items: List[dict] = []
    state = attendance_cutover.status(db, company_id)
    items.append(_item("v2_prepared", "Attendance v2 prepared", "PASS" if state["prepared"] else "FAIL",
                       "Current-behaviour rules exist." if state["prepared"] else
                       "Prepare attendance v2 (creates the current-behaviour rules).", "/settings/attendance/overview"))
    last = state.get("last_reconcile")
    if state["state"] == "V2_ACTIVE":
        items.append(_item("cutover", "Cut-over", "PASS", "Attendance v2 is active.", "/settings/attendance/overview"))
    elif last and last.get("status") == jobs.SUCCEEDED and last.get("differences") == 0:
        items.append(_item("cutover", "Cut-over", "WARN", "Reconcile clean: another administrator can switch v2 on.",
                           "/settings/attendance/overview"))
    else:
        found = last.get("differences") if last else None
        items.append(_item("cutover", "Cut-over", "FAIL",
                           "Run a reconcile of past days with no differences" +
                           (f" (the last one found {found})." if found else "."), "/settings/attendance/overview"))
    standard = (db.query(PolicyVersion.id).join(Policy, Policy.id == PolicyVersion.policy_id)
                .filter(Policy.company_id == company_id, Policy.type == "ATTENDANCE_RULES_POLICY",
                        PolicyVersion.status == "ACTIVE", PolicyVersion.fields["profile"].astext == "STANDARD")
                .first())
    items.append(_item("standard_rules", "Standard attendance rules", "PASS" if standard else "WARN",
                       "A STANDARD rules version is active." if standard else
                       "Days are still evaluated with the current behaviour (LEGACY_V1).",
                       "/settings/attendance/rules"))
    shifts = db.query(Shift.id).filter(Shift.company_id == company_id).count()
    items.append(_item("shifts", "Shifts defined", "PASS" if shifts else "FAIL", f"{shifts} shift(s).",
                       "/attendance/shifts"))
    active = [e for (e,) in db.query(Employee.id).filter(Employee.company_id == company_id,
                                                         Employee.is_active.is_(True)).order_by(Employee.id)
              .limit(5000)]
    unscheduled = 0
    for index in range(0, len(active), 500):
        chunk = active[index:index + 500]
        resolved = shift_resolution.shifts_for_many(db, company_id, chunk, today, today)
        unscheduled += sum(1 for e in chunk if resolved.get((e, today)) is None)
    items.append(_item("schedules", "Everyone has a schedule today", "PASS" if not unscheduled else "WARN",
                       f"{unscheduled} of {len(active)} active employees have no shift today (weekly off, holiday or "
                       "missing placement)." if unscheduled else f"All {len(active)} active employees resolve a shift.",
                       "/rosters"))
    calendars = db.query(HolidayCalendar.id).filter(HolidayCalendar.company_id == company_id).count()
    items.append(_item("holidays", "Holiday calendar", "PASS" if calendars else "WARN",
                       f"{calendars} calendar(s)." if calendars else "No holiday calendar yet.", "/holidays"))
    for module, label in (("attendance", "Attendance requests"), ("shift_request", "Shift requests")):
        routes = db.query(ApprovalRoute.id).filter(ApprovalRoute.company_id == company_id,
                                                   ApprovalRoute.module == module,
                                                   ApprovalRoute.status == "ACTIVE").count()
        items.append(_item(f"routes_{module}", f"{label}: approval routes", "PASS" if routes else "WARN",
                           f"{routes} active route(s)." if routes else
                           "No route: the default chain (reporting manager / team lead) decides.",
                           "/settings/attendance/requests" if module == "attendance"
                           else "/settings/attendance/shift-requests"))
    for entity in _entities(db, company_id):
        pack = working_time_packs.pack(entity.country_code)
        adopted = pack_resolver.adopted(db, company_id, entity.id, today)
        items.append(_item(f"pack_{entity.id}", f"Country rules: {entity.trade_name or entity.legal_name}",
                           "PASS" if adopted or pack is None else "WARN",
                           ("No working-time pack for this country." if pack is None else
                            f"{entity.country_code} pack adopted" + ("" if pack.get("verified") else
                                                                     " (not verified: warnings only).")
                            if adopted else f"The {entity.country_code} pack is shown as guidance only."),
                           "/settings/attendance/readiness?tab=packs"))
    channels = db.query(NotificationChannel.id).filter(NotificationChannel.company_id == company_id,
                                                       NotificationChannel.active.is_(True)).count()
    items.append(_item("channels", "Notification channels", "PASS" if channels else "INFO",
                       f"{channels} active channel(s)." if channels else "Optional: Slack, Teams or webhooks.",
                       "/settings/integrations"))
    return items


def readiness(db: Session, principal: Principal) -> dict:
    _require(principal)
    items = checklist(db, principal.company_id)
    latest = (db.query(Job).filter(Job.company_id == principal.company_id, Job.type == ACCEPTANCE_JOB)
              .order_by(Job.id.desc()).first())
    return {"items": items, "summary": _summary(items), "last_acceptance": _run_json(latest) if latest else None}


def _summary(items: List[dict]) -> dict:
    counts = {s: sum(1 for i in items if i["status"] == s) for s in ("PASS", "WARN", "FAIL", "INFO")}
    return {**counts, "ready": counts["FAIL"] == 0}


def _run_json(job: Job) -> dict:
    result = job.result or {}
    return {"job_id": job.id, "status": job.status, "started_by_user_id": job.created_by_user_id,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "summary": result.get("summary"), "items": result.get("items", []), "probes": result.get("probes", []),
            "signed_off_by_user_id": result.get("signed_off_by_user_id"),
            "signed_off_at": result.get("signed_off_at"), "sign_off_note": result.get("sign_off_note"),
            "error": job.error}


def start_acceptance(db: Session, principal: Principal) -> dict:
    _require(principal)
    stamp = clock.utcnow().strftime("%Y%m%dT%H%M%S%f")
    job = jobs.enqueue(db, company_id=principal.company_id, type=ACCEPTANCE_JOB, payload={},
                       idempotency_key=f"acceptance:{stamp}", created_by_user_id=principal.user_id,
                       read_permission=MANAGE)
    AuditLogService.log(db, principal, "Attendance", "ATTENDANCE_ACCEPTANCE_STARTED", "Acceptance run started",
                        "job", job.id)
    return {"job_id": job.id}


def get_acceptance(db: Session, principal: Principal, job_id: int) -> dict:
    _require(principal)
    job = (db.query(Job).filter(Job.company_id == principal.company_id, Job.type == ACCEPTANCE_JOB,
                                Job.id == job_id).first())
    if job is None:
        raise not_found("Acceptance run")
    return _run_json(job)


def _probes(db: Session, company_id: int) -> List[dict]:
    from app.payroll.services import approvals
    from app.services import shift_resolution

    today = clock.company_today(db, company_id)
    probes = []
    active = [e for (e,) in db.query(Employee.id).filter(Employee.company_id == company_id,
                                                         Employee.is_active.is_(True)).limit(2000)]
    try:
        shift_resolution.shifts_for_many(db, company_id, active, today, today + timedelta(days=6))
        probes.append({"key": "schedule_resolution", "status": "PASS",
                       "detail": f"Next 7 days resolved for {len(active)} employees."})
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        probes.append({"key": "schedule_resolution", "status": "FAIL", "detail": f"{type(exc).__name__}: {exc}"[:300]})
    for entity in _entities(db, company_id):
        try:
            for field in pack_resolver.NO_PACK_DEFAULTS:
                pack_resolver.resolve(db, company_id, entity.id, field, today)
            probes.append({"key": f"pack_{entity.id}", "status": "PASS",
                           "detail": f"All pack fields resolve for {entity.legal_name}."})
        except Exception as exc:  # noqa: BLE001
            probes.append({"key": f"pack_{entity.id}", "status": "FAIL", "detail": str(exc)[:300]})
    for module in ("attendance", "shift_request", "team_inputs"):
        try:
            approvals.select_route(db, company_id, module, {}) if module in approvals.ROUTED_MODULES else None
            probes.append({"key": f"engine_{module}", "status": "PASS",
                           "detail": f"The approval engine accepts module {module}."})
        except Exception as exc:  # noqa: BLE001
            probes.append({"key": f"engine_{module}", "status": "FAIL", "detail": str(exc)[:300]})
    return probes


@jobs.register(ACCEPTANCE_JOB, access=lambda db, principal, job: principal.has_company_scope(MANAGE))
def acceptance_run(ctx) -> dict:
    items = checklist(ctx.db, ctx.company_id)
    probes = _probes(ctx.db, ctx.company_id)
    summary = _summary(items + probes)
    return {"items": items, "probes": probes, "summary": summary,
            "ran_at": clock.utcnow().replace(microsecond=0).isoformat()}


def sign_off(db: Session, principal: Principal, job_id: int, note: Optional[str]) -> dict:
    """Another administrator accepts a run with no FAIL (maker-checker; fresh step-up)."""
    from app.payroll.services import step_up

    _require(principal)
    job = (db.query(Job).filter(Job.company_id == principal.company_id, Job.type == ACCEPTANCE_JOB,
                                Job.id == job_id).with_for_update().first())
    if job is None:
        raise not_found("Acceptance run")
    result = dict(job.result or {})
    if job.status != jobs.SUCCEEDED or not (result.get("summary") or {}).get("ready"):
        raise AppError(409, "Only a finished run with no failed check can be signed off.", "ACCEPTANCE_NOT_READY")
    if result.get("signed_off_by_user_id"):
        raise AppError(409, "This run is signed off already.", "ALREADY_SIGNED")
    if job.created_by_user_id == principal.user_id:
        raise AppError(409, "Another administrator must sign off the run you started.", "SELF_DECISION_FORBIDDEN")
    step_up.ensure_step_up(db, principal)
    result.update(signed_off_by_user_id=principal.user_id, signed_off_at=clock.utcnow().isoformat(),
                  sign_off_note=(note or "").strip()[:500] or None)
    job.result = result
    db.flush()
    AuditLogService.log(db, principal, "Attendance", "ATTENDANCE_ACCEPTANCE_SIGNED",
                        f"Acceptance run {job.id} signed off", "job", job.id,
                        details={"summary": result.get("summary")})
    return _run_json(job)

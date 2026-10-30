"""Leave v2 API (LH 11 §6; SPINE §11). Prefix ``/api/v1/leaves``; registered before the legacy leave router, with
literal paths before parameterised ones and int converters on every id (no shadowing of ``/types``, ``/my``,
``/approvals``, ``/approval-chain``).

Gates (13 §2.3): GLOBAL — type catalogue, plans, setup, preview, requests, drafts, return / resubmit, CANCEL changes,
discussion, documents, blackouts, coverage, reason codes, approval route, team calendar; V2 (404 while
``leave.v2.enabled`` is false) — ledger, adjustments, imports, year end, comp-off, encashment, liability, partial /
modify / convert / recall changes. Every state-changing call accepts ``Idempotency-Key``; ids outside the caller's
company or scope answer 404.
"""

from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core import authz, clock
from app.core.errors import AppError, bad_request, forbidden, not_found
from app.core.principal import Principal, get_principal, require, require_any
from app.database.db import get_db
from app.models.employee import Employee
from app.models.leave import Leave, V2_STATUS
from app.payroll.services import idempotency
from app.payroll.services import policies as policy_service
from app.schemas.leave import LeaveTypeWriteV2
from app.schemas.leave_v2 import (
    AdjustmentIn, BlackoutIn, ChangeIn, CompOffClaimIn, CoverageIn, DecisionIn, DocumentRequestIn, EncashmentIn,
    GoLiveIn, LeaveEventIn, LeaveRequestIn, LifecycleIn, MessageIn, PlanActivateIn, PlanAssignmentsIn, PlanCreateIn,
    PlanPatchIn, PlanVersionIn, ReasonCodeIn, ReasonIn, ReturnIn, RollbackIn, RouteIn, SimulateIn, VerifyIn, YearEndIn,
)
from app.services import (
    leave_access, leave_accrual, leave_catalogue, leave_changes, leave_comp_off, leave_cutover, leave_docs,
    leave_encashment, leave_events, leave_ledger, leave_plans, leave_reports, leave_requests, leave_service,
    leave_types,
)
from app.services.audit_log_service import AuditLogService

router = APIRouter(prefix="/api/v1/leaves", tags=["Leave v2"])
PLAN = leave_plans.PLAN


def _exec(db: Session, principal: Principal, request: Request, handler, body=None, status: int = 200):
    return idempotency.execute(db, principal, request, lambda: (status, jsonable_encoder(handler())), body=body)


def _body(data) -> Optional[dict]:
    return None if data is None else data.model_dump(mode="json")


# =========================================================================== access facts
@router.get("/access")
def access(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """``me.leave`` facts (V3-115): what the signed-in user may do, the tenant's state and local today."""
    employee = db.get(Employee, principal.employee_id) if principal.employee_id else None
    today = leave_requests.local_today(db, employee) if employee else clock.company_today(db, principal.company_id)
    codes = ("leave:read", "leave:apply", "leave:approve", "leave:manage_policy", "leave:apply_on_behalf",
             "leave:record", "leave:override", "leave:change_any", "leave:balance_adjust", "leave:import",
             "leave:year_end", "leave:view_documents", "leave:verify_documents", "leave:view_medical",
             "leave:analytics_individual", "leave:liability_read")
    return {"v2_enabled": leave_access.v2_enabled(db, principal.company_id), "local_today": today.isoformat(),
            "employee_id": principal.employee_id,
            "permissions": {c: sorted(principal.scopes(c)) for c in codes if principal.has(c)},
            "manage_company": principal.has("leave:manage_policy", "COMPANY"),
            "half_day_enabled": leave_access.setting(db, principal.company_id, "leave.half_day.enabled", default=True),
            "discussion_enabled": leave_access.setting(db, principal.company_id, "leave.discussion.enabled",
                                                       default=True),
            "return_enabled": leave_access.setting(db, principal.company_id, "leave.approval.return_enabled",
                                                   default=True),
            "on_behalf_enabled": leave_access.setting(db, principal.company_id, "leave.apply_on_behalf.enabled",
                                                      default=True)}


@router.get("/dashboard")
def dashboard(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return jsonable_encoder(leave_reports.dashboard(db, principal))


# =========================================================================== type catalogue (03-leave/01 §3.10)
@router.get("/v2/types")
def list_types_v2(include_inactive: bool = False, principal: Principal = Depends(get_principal),
                  db: Session = Depends(get_db)):
    return leave_types.list_types(db, principal.company_id, include_inactive=include_inactive)


@router.post("/v2/types", status_code=201)
def create_type_v2(data: LeaveTypeWriteV2, request: Request, principal: Principal = Depends(require("leave:manage_policy")),
                   db: Session = Depends(get_db)):
    return _exec(db, principal, request, lambda: leave_types.save_type(db, principal, data), _body(data), 201)


@router.get("/v2/types/{type_id:int}")
def get_type_v2(type_id: int, principal: Principal = Depends(require("leave:manage_policy")),
                db: Session = Depends(get_db)):
    return leave_types.get_type(db, principal, type_id)


@router.put("/v2/types/{type_id:int}")
def update_type_v2(type_id: int, data: LeaveTypeWriteV2, request: Request,
                   if_match: Optional[int] = Header(None, alias="If-Match"),
                   principal: Principal = Depends(require("leave:manage_policy")), db: Session = Depends(get_db)):
    return _exec(db, principal, request, lambda: leave_types.save_type(db, principal, data, type_id, if_match),
                 _body(data))


# =========================================================================== plans (03-leave/01 §5.10)
def _plan_json(db: Session, principal: Principal, policy_id: int) -> dict:
    data = policy_service.get_policy(db, principal, policy_id)
    if data.get("type") != PLAN:
        raise not_found("Leave plan")
    for version in data.get("versions", []):
        row = policy_service._version(db, principal.company_id, policy_id, version["version"])
        version["activation_request"] = row.activation_request
        version["activation_requested_by_user_id"] = row.activation_requested_by_user_id
    return data


def _own_plan(db: Session, principal: Principal, policy_id: int):
    policy = policy_service._load(db, principal.company_id, policy_id)
    if policy.type != PLAN:
        raise not_found("Leave plan")
    return policy


@router.get("/plans")
def list_plans(include_archived: bool = False, principal: Principal = Depends(get_principal),
               db: Session = Depends(get_db)):
    leave_plans.require_plan_read(principal)
    return policy_service.list_policies(db, principal, type_code=PLAN, include_archived=include_archived, module="leave")


@router.get("/plans/templates")
def plan_templates(principal: Principal = Depends(get_principal)):
    leave_plans.require_plan_read(principal)
    return leave_plans.template_list()


@router.get("/plans/resolve")
def resolve_plan(employee_id: int = Query(..., ge=1), on: Optional[date] = Query(None, alias="date"),
                 principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    if employee_id != principal.employee_id:
        leave_plans.require_plan_read(principal)
    employee = (authz.own_employee(db, principal) if employee_id == principal.employee_id
                else authz.ensure_employee_access(db, principal, "leave:read", employee_id))
    day = on or leave_requests.local_today(db, employee)
    return jsonable_encoder(leave_plans.explain(db, principal.company_id, employee, day))


@router.post("/plans/simulate")
def simulate_plan(data: SimulateIn, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return leave_plans.simulate(db, principal, data.policy_id, data.version, employee_ids=data.employee_ids, on=data.on)


@router.get("/plans/unassigned")
def unassigned(on: Optional[date] = None, legal_entity_id: Optional[int] = None, branch_id: Optional[int] = None,
               principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    leave_plans.require_plan_read(principal)
    day = on or clock.company_today(db, principal.company_id)
    return policy_service.employees_without_policy(db, principal.company_id, PLAN, on=day,
                                                   legal_entity_id=legal_entity_id, branch_id=branch_id)


@router.post("/plans", status_code=201)
def create_plan(data: PlanCreateIn, request: Request, principal: Principal = Depends(require("leave:manage_policy")),
                db: Session = Depends(get_db)):
    def handler():
        leave_plans.require_plan_write(db, principal)
        fields = data.fields or {}
        if data.template_key:
            fields = leave_plans.template_fields(db, principal.company_id, data.template_key)
        elif data.copy_from_plan_id:
            source = _own_plan(db, principal, data.copy_from_plan_id)
            versions = policy_service._versions(db, principal.company_id, [source.id]).get(source.id, [])
            fields = dict(versions[-1].fields) if versions else {}
        if not data.copy_from_plan_id:
            leave_plans.check_frozen_writes({"types": []}, fields)
        # LE / BR leave managers create plans and then assign them inside their own scope (require_plan_write).
        created = policy_service.create_policy(db, _company_writer(principal), type_code=PLAN, name=data.name,
                                               description=data.description, fields=fields,
                                               effective_from=data.effective_from)
        return _plan_json(db, principal, created["id"])
    return _exec(db, principal, request, handler, _body(data), 201)


def _company_writer(principal: Principal) -> Principal:
    """The policy framework checks COMPANY scope; LE/BR leave managers were already checked by require_plan_write."""
    if principal.has("leave:manage_policy", "COMPANY"):
        return principal
    from dataclasses import replace

    grants = dict(principal.grants)
    grants["leave:manage_policy"] = frozenset(set(grants.get("leave:manage_policy", frozenset())) | {"COMPANY"})
    return replace(principal, grants=grants)


@router.get("/plans/{plan_id:int}")
def get_plan(plan_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    leave_plans.require_plan_read(principal)
    return _plan_json(db, principal, plan_id)


@router.patch("/plans/{plan_id:int}")
def patch_plan(plan_id: int, data: PlanPatchIn, request: Request,
               principal: Principal = Depends(require("leave:manage_policy")), db: Session = Depends(get_db)):
    def handler():
        leave_plans.require_plan_write(db, principal, plan_id)
        policy = _own_plan(db, principal, plan_id)
        if data.name is not None:
            policy.name = policy_service._name(data.name)
        if data.description is not None:
            policy.description = policy_service._description(data.description)
        db.flush()
        return _plan_json(db, principal, plan_id)
    return _exec(db, principal, request, handler, _body(data))


@router.post("/plans/{plan_id:int}/versions", status_code=201)
def create_plan_version(plan_id: int, request: Request, data: Optional[PlanVersionIn] = None,
                        principal: Principal = Depends(require("leave:manage_policy")), db: Session = Depends(get_db)):
    body = data or PlanVersionIn()

    def handler():
        leave_plans.require_plan_write(db, principal, plan_id)
        _own_plan(db, principal, plan_id)
        return policy_service.create_version(db, _company_writer(principal), plan_id, fields=body.fields,
                                             effective_from=body.effective_from)
    return _exec(db, principal, request, handler, _body(data), 201)


@router.put("/plans/{plan_id:int}/versions/{version:int}")
def update_plan_version(plan_id: int, version: int, data: PlanVersionIn, request: Request,
                        principal: Principal = Depends(require("leave:manage_policy")), db: Session = Depends(get_db)):
    def handler():
        leave_plans.require_plan_write(db, principal, plan_id)
        _own_plan(db, principal, plan_id)
        row = policy_service._version(db, principal.company_id, plan_id, version, lock=True)
        if data.fields is not None:
            leave_plans.check_frozen_writes(row.fields or {}, data.fields)
        result = policy_service.update_version(db, _company_writer(principal), plan_id, version,
                                               fields=data.fields, effective_from=data.effective_from)
        row.activation_request = None  # editing the draft clears a pending activation request (§5.6 step 0)
        row.activation_requested_by_user_id = None
        row.activation_requested_at = None
        warnings = leave_plans.reference_problems(db, principal.company_id, row.fields or {}, activation=False)
        return result | {"warnings": warnings}
    return _exec(db, principal, request, handler, _body(data))


@router.post("/plans/{plan_id:int}/versions/{version:int}/activate")
def activate_plan_version(plan_id: int, version: int, request: Request, data: Optional[PlanActivateIn] = None,
                          principal: Principal = Depends(require("leave:manage_policy")), db: Session = Depends(get_db)):
    body = data or PlanActivateIn()

    def handler():
        _own_plan(db, principal, plan_id)
        return leave_plans.activate(db, _company_writer(principal) if principal.has("leave:manage_policy", "COMPANY")
                                    else principal, plan_id, version, effective_from=body.effective_from,
                                    reason=body.reason, confirm=body.confirm,
                                    recalc_future_leave=body.recalc_future_leave)
    return _exec(db, principal, request, handler, _body(data))


@router.get("/plans/{plan_id:int}/assignments")
def get_plan_assignments(plan_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    leave_plans.require_plan_read(principal)
    _own_plan(db, principal, plan_id)
    return policy_service.get_assignments(db, _company_writer(principal) if principal.has("leave:manage_policy")
                                          else principal, plan_id)


@router.put("/plans/{plan_id:int}/assignments")
def put_plan_assignments(plan_id: int, data: PlanAssignmentsIn, request: Request,
                         principal: Principal = Depends(require("leave:manage_policy")), db: Session = Depends(get_db)):
    def handler():
        leave_plans.require_plan_write(db, principal, plan_id, criteria=[r.criteria for r in data.rules])
        _own_plan(db, principal, plan_id)
        rules = [policy_service.RuleInput(r.criteria, r.priority, r.effective_from, r.effective_to) for r in data.rules]
        result = policy_service.put_assignments(db, _company_writer(principal), plan_id, rules)
        AuditLogService.log(db, principal, "Leave", "PLAN_ASSIGNMENTS_CHANGED", f"Changed who leave plan #{plan_id} "
                            "applies to", "policy", plan_id, details={"rules": len(rules)})
        leave_plans._memo(db).clear()
        return result
    return _exec(db, principal, request, handler, _body(data))


@router.post("/plans/{plan_id:int}/archive")
def archive_plan(plan_id: int, request: Request, principal: Principal = Depends(require("leave:manage_policy")),
                 db: Session = Depends(get_db)):
    def handler():
        leave_plans.require_plan_write(db, principal, plan_id)
        policy = _own_plan(db, principal, plan_id)
        if leave_access.v2_enabled(db, principal.company_id):
            today = clock.company_today(db, principal.company_id)
            resolving = [e for e in policy_service.employee_attributes(db, principal.company_id, today)
                         if leave_plans.resolve(db, principal.company_id, e.employee_id, today).policy_id == policy.id]
            if resolving:
                raise AppError(409, f"{len(resolving)} employee(s) still use this plan today.", "PLAN_CONFLICT")
        result = policy_service.archive_policy(db, _company_writer(principal), plan_id)
        AuditLogService.log(db, principal, "Leave", "PLAN_ARCHIVED", f"Archived leave plan #{plan_id}", "policy", plan_id)
        return result
    return _exec(db, principal, request, handler)


# =========================================================================== setup and cut-over
@router.get("/setup")
def setup(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return jsonable_encoder(leave_cutover.setup(db, principal))


@router.get("/setup/reconcile")
def reconcile(principal: Principal = Depends(require("leave:manage_policy")), db: Session = Depends(get_db)):
    if not principal.has("leave:manage_policy", "COMPANY"):
        raise forbidden()
    return jsonable_encoder(leave_cutover.reconcile(db, principal.company_id))


@router.post("/setup/go-live")
def go_live(data: GoLiveIn, request: Request, principal: Principal = Depends(require("leave:manage_policy")),
            db: Session = Depends(get_db)):
    return _exec(db, principal, request, lambda: leave_cutover.go_live(db, principal, confirm=data.confirm), _body(data))


@router.post("/setup/rollback")
def rollback(data: RollbackIn, request: Request, principal: Principal = Depends(require("leave:manage_policy")),
             db: Session = Depends(get_db)):
    return _exec(db, principal, request,
                 lambda: leave_cutover.rollback(db, principal, confirm=data.confirm, reason=data.reason), _body(data))


@router.post("/accruals/run")
def run_accruals(request: Request, principal: Principal = Depends(require("leave:year_end")),
                 db: Session = Depends(get_db)):
    leave_access.require_v2(db, principal.company_id)
    return _exec(db, principal, request, lambda: leave_accrual.run_tenant(db, principal.company_id))


# =========================================================================== requests (03-leave/03)
@router.post("/preview")
def preview(data: LeaveRequestIn, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    pv = leave_requests.validate(db, principal, data)
    return jsonable_encoder(pv.json())


@router.post("/record/preview")
def record_preview(data: LeaveRequestIn, principal: Principal = Depends(require("leave:record")),
                   db: Session = Depends(get_db)):
    return jsonable_encoder(leave_requests.validate(db, principal, data, mode="RECORD").json())


def _leave_out(db: Session, principal: Principal, leave: Leave) -> dict:
    if leave.id is None or db.get(Leave, leave.id) is None:  # a deleted draft
        return {"id": leave.id, "deleted": True}
    return leave_requests.detail(db, principal, leave.id)


@router.post("/requests", status_code=201)
def create_request(data: LeaveRequestIn, request: Request, principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db)):
    return _exec(db, principal, request,
                 lambda: _leave_out(db, principal, leave_requests.submit(db, principal, data)), _body(data), 201)


@router.post("/record", status_code=201)
def record_leave(data: LeaveRequestIn, request: Request, principal: Principal = Depends(require("leave:record")),
                 db: Session = Depends(get_db)):
    return _exec(db, principal, request,
                 lambda: _leave_out(db, principal, leave_requests.submit(db, principal, data, mode="RECORD")),
                 _body(data), 201)


@router.get("/requests")
def list_requests(scope: str = Query("mine", pattern=r"^(mine|team|all)$"),
                  status: Optional[str] = Query(None, pattern=r"^[A-Z_]{4,12}$"),
                  employee_id: Optional[int] = Query(None, ge=1), start: Optional[date] = None,
                  end: Optional[date] = None, skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                  principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    query = db.query(Leave).filter(Leave.company_id == principal.company_id)
    if scope == "mine":
        if principal.employee_id is None:
            return {"total": 0, "items": [], "skip": skip, "limit": limit}
        query = query.filter((Leave.employee_id == principal.employee_id))
    else:
        if not principal.has("leave:read"):
            raise forbidden()
        sc = authz.employee_scope(db, principal, "leave:read")
        query = query.filter(sc.clause(Leave.employee_id), Leave.status != "draft")
        if scope == "team" and principal.employee_id:
            query = query.filter(Leave.employee_id != principal.employee_id)
    if status:
        query = query.filter(Leave.status == status.lower())
    if employee_id:
        query = query.filter(Leave.employee_id == employee_id)
    if start:
        query = query.filter(Leave.end_date >= start)
    if end:
        query = query.filter(Leave.start_date <= end)
    total = query.order_by(None).count()
    rows = query.order_by(Leave.start_date.desc(), Leave.id.desc()).offset(skip).limit(limit).all()
    types = {t.code: t for t in leave_service.leave_types(db, principal.company_id, include_inactive=True)}
    people = {e.id: e for e in db.query(Employee).filter(Employee.company_id == principal.company_id,
                                                          Employee.id.in_({r.employee_id for r in rows} or {0}))}
    items = []
    for leave in rows:
        lt = types.get(leave.leave_type)
        masked = leave_requests.masked_type(db, principal, leave, lt)
        items.append({"id": leave.id, "employee_id": leave.employee_id,
                      "employee_name": people[leave.employee_id].full_name if leave.employee_id in people else None,
                      "leave_type": "protected" if masked else leave.leave_type,
                      "leave_type_name": "Protected leave" if masked else (lt.name if lt else leave.leave_type),
                      "color_token": "leave-neutral" if masked else (lt.color_token if lt else None),
                      "start_date": leave.start_date.isoformat(), "end_date": leave.end_date.isoformat(),
                      "start_portion": leave.start_portion, "end_portion": leave.end_portion,
                      "number_of_days": leave_ledger._num(Decimal(leave.number_of_days or 0)),
                      "status": V2_STATUS.get(leave.status, leave.status.upper()), "source": leave.source,
                      "flags": leave.flags or [], "parent_leave_id": leave.parent_leave_id,
                      "cancellation_status": leave.cancellation_status,
                      "created_at": leave.created_at.isoformat() if leave.created_at else None})
    return {"total": total, "items": items, "skip": skip, "limit": limit}


@router.get("/requests/{leave_id:int}")
def get_request(leave_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return jsonable_encoder(leave_requests.detail(db, principal, leave_id))


@router.put("/requests/{leave_id:int}")
def update_request(leave_id: int, data: LeaveRequestIn, request: Request, principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db)):
    return _exec(db, principal, request,
                 lambda: _leave_out(db, principal, leave_requests.update_draft(db, principal, leave_id, data)),
                 _body(data))


@router.post("/requests/{leave_id:int}/submit")
def submit_request(leave_id: int, request: Request, principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db)):
    return _exec(db, principal, request,
                 lambda: _leave_out(db, principal, leave_requests.submit_existing(db, principal, leave_id)))


@router.post("/requests/{leave_id:int}/withdraw")
def withdraw_request(leave_id: int, request: Request, data: Optional[ReasonIn] = None,
                     principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    reason = data.reason if data else None
    return _exec(db, principal, request,
                 lambda: _leave_out(db, principal, leave_requests.withdraw(db, principal, leave_id, reason)), _body(data))


@router.post("/requests/{leave_id:int}/return")
def return_request(leave_id: int, data: ReturnIn, request: Request, principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db)):
    return _exec(db, principal, request, lambda: _leave_out(db, principal, leave_requests.return_for_changes(
        db, principal, leave_id, data.reason, data.reason_code, data.suggestion)), _body(data))


def _decide(db: Session, principal: Principal, leave_id: int, approve: bool, data: DecisionIn):
    leave = leave_requests.decide_request(db, principal, leave_id, approve, data.remarks, data.reason_code)
    return _leave_out(db, principal, leave)


@router.post("/requests/{leave_id:int}/approve")
def approve_request(leave_id: int, request: Request, data: Optional[DecisionIn] = None,
                    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    body = data or DecisionIn()
    return _exec(db, principal, request, lambda: _decide(db, principal, leave_id, True, body), _body(data))


@router.post("/requests/{leave_id:int}/reject")
def reject_request(leave_id: int, request: Request, data: Optional[DecisionIn] = None,
                   principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    body = data or DecisionIn()
    return _exec(db, principal, request, lambda: _decide(db, principal, leave_id, False, body), _body(data))


@router.get("/approvals/inbox")
def approvals_inbox(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """One inbox: leave requests, changes, comp-off claims and encashments waiting for the caller."""
    requests = leave_service.approval_inbox(db, principal)
    types = {t.code: t for t in leave_service.leave_types(db, principal.company_id, include_inactive=True)}
    for item in requests:
        leave = db.get(Leave, item["id"])
        lt = types.get(leave.leave_type)
        item["flags"] = leave.flags or []
        item["issues"] = leave.issues or []
        item["source"] = leave.source
        item["leave_type_name"] = lt.name if lt else leave.leave_type
        item["color_token"] = lt.color_token if lt else None
    claims = [c for c in leave_comp_off.list_claims(db, principal, status="PENDING") if c["can_decide"]] \
        if leave_access.v2_enabled(db, principal.company_id) and principal.has("leave:approve") else []
    encash = [e for e in leave_encashment.list_requests(db, principal, status="PENDING") if e["can_decide"]] \
        if leave_access.v2_enabled(db, principal.company_id) and principal.has("leave:approve") else []
    return jsonable_encoder({"requests": requests, "changes": leave_changes.pending_for_approver(db, principal),
                             "comp_off_claims": claims, "encashments": encash})


# =========================================================================== changes (SPINE §5.2)
@router.get("/requests/{leave_id:int}/changes")
def list_changes(leave_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return leave_changes.list_changes(db, principal, leave_id)


@router.post("/requests/{leave_id:int}/changes", status_code=201)
def request_change(leave_id: int, data: ChangeIn, request: Request, principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db)):
    return _exec(db, principal, request, lambda: leave_changes.change_json(leave_changes.request_change(
        db, principal, leave_id, kind=data.kind, proposed=data.proposed, reason=data.reason)), _body(data), 201)


@router.get("/changes/pending")
def pending_changes(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return leave_changes.pending_for_approver(db, principal)


@router.post("/changes/{change_id:int}/approve")
def approve_change(change_id: int, request: Request, data: Optional[DecisionIn] = None,
                   principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    note = data.remarks if data else None
    return _exec(db, principal, request,
                 lambda: leave_changes.change_json(leave_changes.decide(db, principal, change_id, True, note)), _body(data))


@router.post("/changes/{change_id:int}/reject")
def reject_change(change_id: int, request: Request, data: Optional[DecisionIn] = None,
                  principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    note = data.remarks if data else None
    return _exec(db, principal, request,
                 lambda: leave_changes.change_json(leave_changes.decide(db, principal, change_id, False, note)),
                 _body(data))


@router.post("/changes/{change_id:int}/withdraw")
def withdraw_change(change_id: int, request: Request, principal: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)):
    return _exec(db, principal, request,
                 lambda: leave_changes.change_json(leave_changes.withdraw(db, principal, change_id)))


@router.post("/changes/{change_id:int}/acknowledge")
def acknowledge_change(change_id: int, request: Request, principal: Principal = Depends(get_principal),
                       db: Session = Depends(get_db)):
    return _exec(db, principal, request,
                 lambda: leave_changes.change_json(leave_changes.acknowledge(db, principal, change_id)))


# =========================================================================== EVENT leaves (03-leave/06 §6.1) — V2
@router.get("/events")
def list_events(scope: str = Query("mine", pattern=r"^(mine|team)$"),
                status: Optional[str] = Query(None, pattern=r"^(OPEN|CLOSED|CANCELLED)$"),
                employee_id: Optional[int] = Query(None, ge=1), principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    return leave_events.list_events(db, principal, scope=scope, status=status, employee_id=employee_id)


@router.post("/events", status_code=201)
def create_event(data: LeaveEventIn, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    def handler():
        row = leave_events.create(db, principal, leave_type=data.leave_type_code, event_type=data.event_type,
                                  event_date=data.event_date, employee_id=data.employee_id, note=data.note)
        return leave_events.event_json(db, row, principal=principal)
    return _exec(db, principal, request, handler, _body(data), 201)


@router.post("/events/{event_id:int}/cancel")
def cancel_event(event_id: int, request: Request, data: Optional[ReasonIn] = None,
                 principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    reason = data.reason if data else None
    return _exec(db, principal, request, lambda: leave_events.event_json(
        db, leave_events.cancel(db, principal, event_id, reason), principal=principal), _body(data))


# =========================================================================== discussion and documents (03-leave/05)
@router.get("/requests/{leave_id:int}/messages")
def list_messages(leave_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return leave_docs.messages(db, principal, leave_id)


@router.post("/requests/{leave_id:int}/messages", status_code=201)
def post_message(leave_id: int, data: MessageIn, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    return _exec(db, principal, request,
                 lambda: leave_docs.post_message(db, principal, leave_id, data.body, internal=data.internal),
                 _body(data), 201)


@router.get("/requests/{leave_id:int}/attachments")
def list_attachments(leave_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return leave_docs.list_attachments(db, principal, leave_id)


@router.post("/attachments", status_code=201)
async def upload_attachment(request: Request, file: UploadFile = File(...), category: str = Form(...),
                            leave_id: Optional[int] = Form(None), employee_id: Optional[int] = Form(None),
                            principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = await file.read(26 * 1024 * 1024)
    return _exec(db, principal, request, lambda: leave_docs.upload(
        db, principal, file_name=file.filename or "document", data=data, category=category, leave_id=leave_id,
        employee_id=employee_id), {"category": category, "leave_id": leave_id, "name": file.filename,
                                    "size": len(data)}, 201)


@router.get("/attachments/{attachment_id:int}/download")
def download_attachment(attachment_id: int, request: Request, principal: Principal = Depends(get_principal),
                        db: Session = Depends(get_db)):
    path, row = leave_docs.open_attachment(db, principal, attachment_id,
                                           ip=request.client.host if request.client else None)
    db.commit()
    return FileResponse(path, media_type=row.mime, filename=row.original_name,
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/attachments/{attachment_id:int}/verify")
def verify_attachment(attachment_id: int, data: VerifyIn, request: Request,
                      principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return _exec(db, principal, request,
                 lambda: leave_docs.verify(db, principal, attachment_id, approve=data.approve, reason=data.reason),
                 _body(data))


@router.get("/requests/{leave_id:int}/document-requests")
def list_document_requests(leave_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return leave_docs.document_requests(db, principal, leave_id)


@router.post("/requests/{leave_id:int}/document-requests", status_code=201)
def create_document_request(leave_id: int, data: DocumentRequestIn, request: Request,
                            principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return _exec(db, principal, request, lambda: leave_docs.request_document(
        db, principal, leave_id, category=data.category, due_date=data.due_date, note=data.note), _body(data), 201)


@router.get("/documents/queue")
def documents_queue(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return leave_docs.documents_queue(db, principal)


# =========================================================================== balances and ledger (03-leave/02)
@router.get("/balances/me")
def my_balances(on: Optional[date] = Query(None, alias="date"), principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    employee = authz.own_employee(db, principal)
    day = on or leave_requests.local_today(db, employee)
    return jsonable_encoder({"date": day.isoformat(), "v2_enabled": leave_access.v2_enabled(db, principal.company_id),
                             "balances": leave_ledger.balances_for(db, principal.company_id, employee, day)})


@router.get("/balances/employees/{employee_id:int}")
def employee_balances(employee_id: int, on: Optional[date] = Query(None, alias="date"),
                      principal: Principal = Depends(require("leave:read")), db: Session = Depends(get_db)):
    employee = authz.ensure_employee_access(db, principal, "leave:read", employee_id)
    day = on or leave_requests.local_today(db, employee)
    return jsonable_encoder({"date": day.isoformat(), "employee_id": employee.id, "name": employee.full_name,
                             "balances": leave_ledger.balances_for(db, principal.company_id, employee, day,
                                                                   for_employee=False)})


@router.get("/balances")
def balances_report(on: Optional[date] = Query(None, alias="date"), leave_type: Optional[str] = None,
                    department_id: Optional[int] = None, search: Optional[str] = Query(None, max_length=60),
                    principal: Principal = Depends(require("leave:read")), db: Session = Depends(get_db)):
    return jsonable_encoder(leave_reports.hr_balances(db, principal, on=on, leave_type=leave_type,
                                                      department_id=department_id, search=search))


@router.get("/ledger")
def ledger(employee_id: Optional[int] = Query(None, ge=1), leave_type: Optional[str] = None,
           start: Optional[date] = None, end: Optional[date] = None, principal: Principal = Depends(get_principal),
           db: Session = Depends(get_db)):
    if employee_id is None or employee_id == principal.employee_id:
        employee = authz.own_employee(db, principal)
    else:
        employee = authz.ensure_employee_access(db, principal, "leave:read", employee_id)
    return leave_ledger.ledger_rows(db, principal.company_id, employee.id, type_code=leave_type, start=start, end=end)


@router.get("/ledger/{txn_id:int}/explain")
def explain_ledger_row(txn_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    row = leave_ledger.explain_row(db, principal.company_id, txn_id)
    if row is None:
        raise not_found("Ledger row")
    from app.models.time_inputs import LeaveBalanceTransaction

    txn = db.get(LeaveBalanceTransaction, txn_id)
    if txn.employee_id != principal.employee_id and not authz.can_access_employee(db, principal, "leave:read",
                                                                                  txn.employee_id):
        raise not_found("Ledger row")
    return row


@router.post("/adjustments", status_code=201)
def adjust_balance(data: AdjustmentIn, request: Request, principal: Principal = Depends(require("leave:balance_adjust")),
                   db: Session = Depends(get_db)):
    return _exec(db, principal, request, lambda: leave_catalogue.adjust(
        db, principal, employee_id=data.employee_id, leave_type=data.leave_type, delta=data.delta,
        effective_date=data.effective_date, reason=data.reason, reason_code=data.reason_code,
        expires_on=data.expires_on), _body(data), 201)


@router.post("/imports", status_code=201)
async def import_upload(request: Request, file: UploadFile = File(...), kind: str = Form("OPENING_BALANCES"),
                        principal: Principal = Depends(require("leave:import")), db: Session = Depends(get_db)):
    content = await file.read(3 * 1024 * 1024)
    return _exec(db, principal, request, lambda: leave_catalogue.import_json(leave_catalogue.import_dry_run(
        db, principal, kind=kind, file_name=file.filename or "import.csv", content=content)),
        {"kind": kind, "name": file.filename, "size": len(content)}, 201)


@router.get("/imports/{import_id:int}")
def get_import(import_id: int, principal: Principal = Depends(require("leave:import")), db: Session = Depends(get_db)):
    from app.models.leave_v2 import LeaveImport

    row = db.query(LeaveImport).filter(LeaveImport.company_id == principal.company_id, LeaveImport.id == import_id).first()
    if row is None:
        raise not_found("Import")
    return leave_catalogue.import_json(row)


@router.post("/imports/{import_id:int}/commit")
def commit_import(import_id: int, request: Request, principal: Principal = Depends(require("leave:import")),
                  db: Session = Depends(get_db)):
    return _exec(db, principal, request,
                 lambda: leave_catalogue.import_json(leave_catalogue.import_commit(db, principal, import_id)))


# =========================================================================== year end
@router.get("/year-end")
def list_year_end(principal: Principal = Depends(require("leave:year_end")), db: Session = Depends(get_db)):
    from app.models.leave_v2 import LeaveYearEndRun

    leave_access.require_v2(db, principal.company_id)
    rows = (db.query(LeaveYearEndRun).filter(LeaveYearEndRun.company_id == principal.company_id)
            .order_by(LeaveYearEndRun.id.desc()).limit(50).all())
    return [leave_accrual.run_json(r) | {"rows": None} for r in rows]


@router.get("/year-end/{run_id:int}")
def get_year_end(run_id: int, principal: Principal = Depends(require("leave:year_end")), db: Session = Depends(get_db)):
    from app.models.leave_v2 import LeaveYearEndRun

    row = db.query(LeaveYearEndRun).filter(LeaveYearEndRun.company_id == principal.company_id,
                                           LeaveYearEndRun.id == run_id).first()
    if row is None:
        raise not_found("Year-end run")
    return leave_accrual.run_json(row)


@router.post("/year-end", status_code=201)
def preview_year_end(data: YearEndIn, request: Request, principal: Principal = Depends(require("leave:year_end")),
                     db: Session = Depends(get_db)):
    return _exec(db, principal, request, lambda: leave_accrual.run_json(leave_accrual.preview_year_end(
        db, principal, data.cycle_end, legal_entity_id=data.legal_entity_id)), _body(data), 201)


@router.post("/year-end/{run_id:int}/post")
def post_year_end(run_id: int, request: Request, principal: Principal = Depends(require("leave:year_end")),
                  db: Session = Depends(get_db)):
    def handler():
        from app.payroll.services.step_up import ensure_step_up

        ensure_step_up(db, principal)
        return leave_accrual.run_json(leave_accrual.post_year_end(db, principal, run_id))
    return _exec(db, principal, request, handler)


@router.post("/year-end/{run_id:int}/reverse")
def reverse_year_end(run_id: int, request: Request, principal: Principal = Depends(require("leave:year_end")),
                     db: Session = Depends(get_db)):
    def handler():
        from app.payroll.services.step_up import ensure_step_up

        ensure_step_up(db, principal)
        return leave_accrual.run_json(leave_accrual.reverse_year_end(db, principal, run_id))
    return _exec(db, principal, request, handler)


# =========================================================================== comp-off and encashment
@router.get("/comp-off/claims")
def list_claims(scope: str = Query("mine", pattern=r"^(mine|team)$"), status: Optional[str] = None,
                principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    leave_access.require_v2(db, principal.company_id)
    return leave_comp_off.list_claims(db, principal, mine=scope == "mine", status=status)


@router.get("/comp-off/preview")
def preview_claim(worked_date: date, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    leave_access.require_v2(db, principal.company_id)
    employee = authz.own_employee(db, principal)
    return leave_comp_off.preview_claim(db, principal.company_id, employee, worked_date,
                                        leave_requests.local_today(db, employee))


@router.post("/comp-off/claims", status_code=201)
def create_claim(data: CompOffClaimIn, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    return _exec(db, principal, request, lambda: leave_comp_off.claim_json(leave_comp_off.create(
        db, principal, worked_date=data.worked_date, reason=data.reason, employee_id=data.employee_id)), _body(data), 201)


@router.post("/comp-off/claims/{claim_id:int}/{action}")
def act_on_claim(claim_id: int, action: str, request: Request, data: Optional[DecisionIn] = None,
                 principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    note = data.remarks if data else None

    def handler():
        if action == "approve":
            return leave_comp_off.claim_json(leave_comp_off.decide(db, principal, claim_id, True, note))
        if action == "reject":
            return leave_comp_off.claim_json(leave_comp_off.decide(db, principal, claim_id, False, note))
        if action == "withdraw":
            return leave_comp_off.claim_json(leave_comp_off.withdraw(db, principal, claim_id))
        if action == "revoke":
            return leave_comp_off.claim_json(leave_comp_off.revoke(db, principal, claim_id, note or ""))
        raise not_found()
    return _exec(db, principal, request, handler, _body(data))


@router.get("/encashments")
def list_encashments(scope: str = Query("mine", pattern=r"^(mine|team)$"), status: Optional[str] = None,
                     principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    leave_access.require_v2(db, principal.company_id)
    return leave_encashment.list_requests(db, principal, mine=scope == "mine", status=status)


@router.post("/encashments", status_code=201)
def create_encashment(data: EncashmentIn, request: Request, principal: Principal = Depends(get_principal),
                      db: Session = Depends(get_db)):
    return _exec(db, principal, request, lambda: leave_encashment.request_json(leave_encashment.create(
        db, principal, leave_type=data.leave_type, units=data.units, reason=data.reason,
        employee_id=data.employee_id)), _body(data), 201)


@router.post("/encashments/{request_id:int}/{action}")
def act_on_encashment(request_id: int, action: str, request: Request, data: Optional[DecisionIn] = None,
                      principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    note = data.remarks if data else None

    def handler():
        if action in ("approve", "reject"):
            return leave_encashment.request_json(leave_encashment.decide(db, principal, request_id, action == "approve",
                                                                         note))
        if action == "withdraw":
            return leave_encashment.request_json(leave_encashment.withdraw(db, principal, request_id))
        raise not_found()
    return _exec(db, principal, request, handler, _body(data))


# =========================================================================== rule catalogue
@router.get("/blackouts")
def list_blackouts(include_archived: bool = False, principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db)):
    return leave_catalogue.list_blackouts(db, principal, include_archived)


@router.post("/blackouts", status_code=201)
def create_blackout(data: BlackoutIn, request: Request, principal: Principal = Depends(require("leave:manage_policy")),
                    db: Session = Depends(get_db)):
    return _exec(db, principal, request, lambda: leave_catalogue.save_blackout(db, principal, data), _body(data), 201)


@router.put("/blackouts/{blackout_id:int}")
def update_blackout(blackout_id: int, data: BlackoutIn, request: Request,
                    principal: Principal = Depends(require("leave:manage_policy")), db: Session = Depends(get_db)):
    return _exec(db, principal, request, lambda: leave_catalogue.save_blackout(db, principal, data, blackout_id),
                 _body(data))


@router.get("/coverage-rules")
def list_coverage(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return leave_catalogue.list_coverage(db, principal)


@router.post("/coverage-rules", status_code=201)
def create_coverage(data: CoverageIn, request: Request, principal: Principal = Depends(require("leave:manage_policy")),
                    db: Session = Depends(get_db)):
    return _exec(db, principal, request, lambda: leave_catalogue.save_coverage(db, principal, data), _body(data), 201)


@router.put("/coverage-rules/{rule_id:int}")
def update_coverage(rule_id: int, data: CoverageIn, request: Request,
                    principal: Principal = Depends(require("leave:manage_policy")), db: Session = Depends(get_db)):
    return _exec(db, principal, request, lambda: leave_catalogue.save_coverage(db, principal, data, rule_id),
                 _body(data))


LEAVE_USERS = require_any("leave:apply", "leave:approve", "leave:read", "leave:manage_policy")


@router.get("/reason-codes")
def list_reason_codes(applies_to: Optional[str] = None, principal: Principal = Depends(LEAVE_USERS),
                      db: Session = Depends(get_db)):
    return leave_catalogue.list_reason_codes(db, principal, applies_to)


@router.post("/reason-codes", status_code=201)
def save_reason_code(data: ReasonCodeIn, request: Request, principal: Principal = Depends(require("leave:manage_policy")),
                     db: Session = Depends(get_db)):
    return _exec(db, principal, request, lambda: leave_catalogue.save_reason_code(db, principal, data), _body(data), 201)


@router.get("/approval-route")
def get_route(principal: Principal = Depends(LEAVE_USERS), db: Session = Depends(get_db)):
    return leave_catalogue.get_route(db, principal.company_id)


@router.put("/approval-route")
def put_route(data: RouteIn, request: Request, principal: Principal = Depends(require("leave:manage_policy")),
              db: Session = Depends(get_db)):
    return _exec(db, principal, request, lambda: leave_catalogue.save_route(db, principal, data.steps), _body(data))


# =========================================================================== calendars and reports
@router.get("/team-calendar")
def team_calendar(start: date, end: date, department_id: Optional[int] = None,
                  principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return leave_reports.team_calendar(db, principal, start, end, department_id=department_id)


@router.get("/reports/usage")
def usage_report(start: date, end: date, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return leave_reports.usage(db, principal, start, end)


@router.get("/reports/liability")
def liability_report(on: Optional[date] = Query(None, alias="date"), legal_entity_id: Optional[int] = None,
                     principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return leave_reports.liability(db, principal, on=on, legal_entity_id=legal_entity_id)


# =========================================================================== employee lifecycle dates
@router.put("/employees/{employee_id:int}/lifecycle")
def set_lifecycle(employee_id: int, data: LifecycleIn, request: Request,
                  principal: Principal = Depends(require("employee:update")), db: Session = Depends(get_db)):
    def handler():
        employee = authz.ensure_employee_access(db, principal, "employee:update", employee_id)
        before = {k: getattr(employee, k) for k in LifecycleIn.model_fields}
        for key, value in data.model_dump().items():
            setattr(employee, key, value)
        if employee.probation_end_date and employee.probation_end_date < employee.hire_date:
            raise bad_request("Probation cannot end before the hire date.", "INVALID_RANGE")
        db.flush()
        AuditLogService.log(db, principal, "Leave", "LIFECYCLE_DATES_SET", f"Set leave lifecycle dates of "
                            f"{employee.full_name}", "employee", employee.id,
                            details={"before": {k: v.isoformat() if v else None for k, v in before.items()},
                                     "after": data.model_dump(mode="json")})
        leave_plans._memo(db).clear()
        return {"employee_id": employee.id, **data.model_dump(mode="json")}
    return _exec(db, principal, request, handler, _body(data))


# =========================================================================== leave settings (BUG-16: leave.* written by leave admins)
@router.get("/settings")
def leave_settings(principal: Principal = Depends(require("leave:manage_policy")), db: Session = Depends(get_db)):
    """The Leave section of the settings registry with the tenant's current values (read: any leave admin)."""
    from app.payroll import settings_registry as reg
    from app.payroll.services import settings as settings_service

    keys = [k for k, d in reg.REGISTRY.items() if d.section == reg.S_LEAVE_POLICIES or k == "leave.half_day.enabled"]
    today = clock.company_today(db, principal.company_id)
    values = settings_service.resolve_many(db, principal.company_id, keys, on=today, strict=False)
    out = []
    for key in keys:
        defn = reg.REGISTRY[key]
        value = getattr(values[key], "value", None)
        out.append(reg.definition_json(defn) | {
            "value": None if type(value).__name__ == "_Unset" else jsonable_encoder(value),
            "system_written": bool(defn.ui.get("system_written"))})
    return {"settings": out, "can_write": principal.has("leave:manage_policy", "COMPANY")}


@router.put("/settings/{key}")
def put_leave_setting(key: str, request: Request, data: dict = None,
                      principal: Principal = Depends(require("leave:manage_policy")), db: Session = Depends(get_db)):
    from app.payroll import settings_registry as reg
    from app.payroll.services import settings as settings_service

    if not principal.has("leave:manage_policy", "COMPANY"):
        raise forbidden("Leave settings are company-wide: they need company-wide leave configuration access.")
    defn = reg.REGISTRY.get(key)
    if defn is None or not key.startswith("leave."):
        raise not_found("Setting")
    if defn.ui.get("system_written"):
        raise AppError(409, f"{defn.label} is set by the system and cannot be changed here.", "SETTING_SYSTEM_WRITTEN")
    body = data or {}
    if "value" not in body:
        raise bad_request("Send {\"value\": ...}.", "INVALID_FIELDS")

    def handler():
        try:
            result = settings_service.write_module_value(db, principal, key, body["value"], reason=body.get("reason"))
        except reg.SettingValidationError as error:
            raise AppError(400, error.message, error.code)
        leave_access.forget(db)
        return result
    return _exec(db, principal, request, handler, body)

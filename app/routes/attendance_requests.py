"""Attendance requests, on-duty / remote work, muster, period close, approval routes and the module-neutral approvals
inbox (AS-M4; AS 04-attendance/04 §16, §5.4, §9.2, §9.8, §11, §14.3; AS 14 WI-AS-4.5).

* ``/attendance/requests*`` and ``/attendance/muster`` are V2-only (404 for v1 tenants, spine §15.7).
* ``/attendance/on-duty*`` works for every tenant (on-duty exists in v1; the legacy ``/payroll/on-duty`` stays).
* ``/approvals*`` is the module-neutral inbox over the generic engine (D-AS-55): every engine module, rows carrying
  the owning module's subject summary; ``/approval-routes*`` edits the routes of modules ``attendance`` and
  ``timesheet`` (gate ``attendance:manage_policy``).

Writes run through ``idempotency.execute`` (which commits); reads never write.
"""

from datetime import date
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.errors import bad_request, conflict, forbidden, not_found
from app.core.principal import Principal, get_principal, require, require_any
from app.database.db import get_db
from app.models.approvals import ApprovalRequest
from app.models.attendance_requests import (
    ATTENDANCE_REQUEST_KINDS,
    REASON_APPLIES_TO,
    ApprovalRoute,
    AttendanceReasonCode,
)
from app.models.leave import ApprovalChainStep
from app.payroll.services import approvals, idempotency
from app.services import attendance_cycles, attendance_muster, attendance_on_duty, attendance_timesheets
from app.services import attendance_requests as ar
from app.services.audit_log_service import AuditLogService

router = APIRouter(prefix="/api/v1", tags=["Attendance requests"])
MANAGE = "attendance:manage_policy"
ROUTE_MODULES = ("attendance", "timesheet")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequestIn(_Strict):
    employee_id: Optional[int] = Field(None, ge=1)
    shift_date: date
    kind: Literal[ATTENDANCE_REQUEST_KINDS]  # type: ignore[valid-type]
    mode: Literal["SELF", "ON_BEHALF", "DIRECT"] = "SELF"
    session_id: Optional[int] = Field(None, ge=1)
    payload: Dict[str, Any] = Field(default_factory=dict)
    reason_code: Optional[str] = Field(None, max_length=40)
    comment: Optional[str] = Field(None, max_length=1000)
    attachment_ids: List[int] = Field(default_factory=list, max_length=5)
    submit: bool = True
    acknowledged_warnings: List[str] = Field(default_factory=list, max_length=10)


class RequestPatch(_Strict):
    session_id: Optional[int] = Field(None, ge=1)
    payload: Optional[Dict[str, Any]] = None
    reason_code: Optional[str] = Field(None, max_length=40)
    comment: Optional[str] = Field(None, max_length=1000)
    attachment_ids: List[int] = Field(default_factory=list, max_length=5)
    row_version: Optional[int] = Field(None, ge=1)


class TransitionIn(_Strict):
    row_version: Optional[int] = Field(None, ge=1)
    acknowledged_warnings: List[str] = Field(default_factory=list, max_length=10)


class OnDutyIn(_Strict):
    employee_id: Optional[int] = Field(None, ge=1)
    kind: Literal["ON_DUTY", "REMOTE_WORK"] = "ON_DUTY"
    start: date
    end: Optional[date] = None
    portion: Literal["FULL", "FIRST_HALF", "SECOND_HALF"] = "FULL"
    start_time: Optional[str] = Field(None, max_length=5)
    end_time: Optional[str] = Field(None, max_length=5)
    reason_code: Optional[str] = Field(None, max_length=40)
    reason: str = Field(..., min_length=1, max_length=500)
    dry_run: bool = False


class MusterRow(_Strict):
    employee_id: Any
    mark: str = Field(..., max_length=12)
    in_time: Optional[str] = Field(None, max_length=5)
    out_time: Optional[str] = Field(None, max_length=5)
    reason_code: Optional[str] = Field(None, max_length=40)
    comment: Optional[str] = Field(None, max_length=1000)


class MusterIn(_Strict):
    date: date
    team_id: Optional[int] = Field(None, ge=1)
    branch_id: Optional[int] = Field(None, ge=1)
    offline_batch_id: Optional[str] = Field(None, max_length=64)
    rows: List[MusterRow] = Field(..., min_length=1, max_length=attendance_muster.MAX_ROWS)


class PeriodCloseIn(_Strict):
    legal_entity_id: int = Field(..., ge=1)
    branch_id: Optional[int] = Field(None, ge=1)
    pay_group_id: Optional[int] = Field(None, ge=1)
    cycle_start: date
    cycle_end: date


class CloseIn(_Strict):
    acknowledge_open_items: bool = False
    row_version: Optional[int] = Field(None, ge=1)


class ReopenIn(_Strict):
    reason: str = Field(..., min_length=5, max_length=500)


class ReasonCodeIn(_Strict):
    code: str = Field(..., pattern=r"^[A-Z][A-Z0-9_]{1,39}$")
    label: str = Field(..., min_length=1, max_length=80)
    applies_to: List[Literal[REASON_APPLIES_TO]] = Field(..., min_length=1)  # type: ignore[valid-type]
    requires_attachment: bool = False
    counts_toward_limit: bool = True
    sensitive: bool = False


class ReasonCodePatch(_Strict):
    label: Optional[str] = Field(None, min_length=1, max_length=80)
    applies_to: Optional[List[Literal[REASON_APPLIES_TO]]] = None  # type: ignore[valid-type]
    requires_attachment: Optional[bool] = None
    counts_toward_limit: Optional[bool] = None
    sensitive: Optional[bool] = None
    status: Optional[Literal["ACTIVE", "INACTIVE"]] = None
    row_version: Optional[int] = Field(None, ge=1)


class RouteStepIn(_Strict):
    approver_type: Literal["primary_manager", "skip_level_manager", "department_head", "team_lead", "role",
                           "specific_user"]
    role_id: Optional[int] = Field(None, ge=1)
    specific_user_id: Optional[int] = Field(None, ge=1)
    sla_hours: Optional[int] = Field(None, ge=1, le=720)
    condition: Optional[Dict[str, Any]] = None


class RouteIn(_Strict):
    module: Literal["attendance", "timesheet", "shift_request"] = "attendance"
    route_key: str = Field(..., pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")
    name: str = Field(..., min_length=1, max_length=100)
    priority: int = Field(0, ge=0, le=10000)
    criteria: Dict[str, Any] = Field(default_factory=dict)
    status: Literal["ACTIVE", "INACTIVE"] = "ACTIVE"
    steps: List[RouteStepIn] = Field(..., min_length=1, max_length=5)
    auto_approve: Optional[Any] = None
    row_version: Optional[int] = Field(None, ge=1)


class TimesheetSubmitIn(_Strict):
    week_start: date


class DecideIn(_Strict):
    decision: Literal["APPROVE", "REJECT", "RETURN"]
    remarks: Optional[str] = Field(None, max_length=1000)
    reason_code: Optional[str] = Field(None, max_length=40)
    expected_step: Optional[int] = Field(None, ge=1)


class BulkDecideIn(_Strict):
    module: Optional[str] = Field(None, max_length=20)
    request_ids: List[int] = Field(..., min_length=1, max_length=100)
    decision: Literal["APPROVE", "REJECT"]
    remarks: Optional[str] = Field(None, max_length=1000)


# --------------------------------------------------------------------------- attendance requests


@router.get("/attendance/requests")
def list_requests(mine: bool = False, employee_id: Optional[int] = Query(None, ge=1),
                  status: Optional[str] = Query(None, max_length=80), kind: Optional[str] = Query(None, max_length=20),
                  start: Optional[date] = Query(None, alias="from"), end: Optional[date] = Query(None, alias="to"),
                  skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                  principal: Principal = Depends(require_any("attendance:request", "attendance:read",
                                                             "attendance:approve", "attendance:request_on_behalf")),
                  db: Session = Depends(get_db)):
    total, items = ar.list_requests(db, principal, mine=mine, employee_id=employee_id, status=status, kind=kind,
                                    start=start, end=end, skip=skip, limit=limit)
    return {"total": total, "items": items}


@router.get("/attendance/requests/form")
def request_form(day: date = Query(..., alias="date"), employee_id: Optional[int] = Query(None, ge=1),
                 mode: Literal["SELF", "ON_BEHALF", "DIRECT"] = "SELF", principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    return ar.form_context(db, principal, employee_id=employee_id, d=day, mode=mode)


@router.post("/attendance/requests/preview")
def preview_request(data: RequestIn, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    result = ar.preview(db, principal, data.model_dump(mode="json"))
    db.rollback()  # never writes
    return result


@router.post("/attendance/requests", status_code=201)
def create_request(data: RequestIn, request: Request, principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db)):
    def handler():
        row = ar.create(db, principal, data.model_dump(mode="json"))
        return 201, ar.serialize(db, principal, row, detail=True)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/attendance/requests/{request_id:int}")
def get_request(request_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return ar.serialize(db, principal, ar.load(db, principal, request_id), detail=True)


@router.patch("/attendance/requests/{request_id:int}")
def update_request(request_id: int, data: RequestPatch, request: Request,
                   principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        body = data.model_dump(mode="json", exclude_unset=True)
        version = body.pop("row_version", None)
        row = ar.update(db, principal, request_id, body, expected_version=version)
        return 200, ar.serialize(db, principal, row, detail=True)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.post("/attendance/requests/{request_id:int}/submit")
def submit_request(request_id: int, data: TransitionIn, request: Request,
                   principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        row = ar.submit(db, principal, request_id, data.row_version, data.acknowledged_warnings)
        return 200, ar.serialize(db, principal, row, detail=True)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.post("/attendance/requests/{request_id:int}/withdraw")
def withdraw_request(request_id: int, data: TransitionIn, request: Request,
                     principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        row = ar.withdraw(db, principal, request_id, data.row_version)
        return 200, ar.serialize(db, principal, row, detail=True)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.post("/attendance/attachments", status_code=201)
async def upload_attachment(request: Request, file: UploadFile = File(...),
                            employee_id: Optional[int] = Form(None), category: str = Form("SUPPORTING_DOCUMENT"),
                            principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = await file.read(5 * 1024 * 1024 + 1)
    row = ar.upload_attachment(db, principal, employee_id=employee_id, file_name=file.filename or "",
                               data=data, category=category)
    db.commit()
    return ar.attachment_json(row)


@router.get("/attendance/attachments/{attachment_id:int}/download")
def download_attachment(attachment_id: int, principal: Principal = Depends(get_principal),
                        db: Session = Depends(get_db)):
    path, row = ar.attachment_for_download(db, principal, attachment_id)
    db.commit()  # the access-log row
    return FileResponse(path, media_type=row.mime, filename=row.file_name)


# --------------------------------------------------------------------------- reason codes


@router.get("/attendance/reason-codes")
def list_reason_codes(kind: Optional[str] = Query(None, max_length=20), include_inactive: bool = False,
                      principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return {"items": ar.reason_codes(db, principal, kind=kind,
                                     include_inactive=include_inactive and principal.has(MANAGE))}


@router.post("/attendance/reason-codes", status_code=201)
def create_reason_code(data: ReasonCodeIn, request: Request, principal: Principal = Depends(require(MANAGE)),
                       db: Session = Depends(get_db)):
    def handler():
        if db.query(AttendanceReasonCode.id).filter(AttendanceReasonCode.company_id == principal.company_id,
                                                    AttendanceReasonCode.code == data.code).first():
            raise conflict("This code exists already.", "DUPLICATE_CODE")
        row = AttendanceReasonCode(company_id=principal.company_id, **data.model_dump())
        db.add(row)
        db.flush()
        AuditLogService.log(db, principal, ar.AUDIT_MODULE, "POLICY_SAVED", f"Added reason code {row.code}",
                            "attendance_reason_code", row.id, details={"after": ar.reason_json(row)})
        return 201, ar.reason_json(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.patch("/attendance/reason-codes/{code}")
def update_reason_code(code: str, data: ReasonCodePatch, request: Request,
                       principal: Principal = Depends(require(MANAGE)), db: Session = Depends(get_db)):
    def handler():
        row = db.query(AttendanceReasonCode).filter(AttendanceReasonCode.company_id == principal.company_id,
                                                    AttendanceReasonCode.code == code).with_for_update().first()
        if row is None:
            raise not_found("Reason code")
        changes = data.model_dump(exclude_unset=True)
        version = changes.pop("row_version", None)
        if version is not None and version != row.row_version:
            raise conflict("This reason changed since you loaded it.", "ROW_VERSION_CONFLICT")
        before = ar.reason_json(row)
        for key, value in changes.items():
            setattr(row, key, value)
        row.row_version += 1
        db.flush()
        AuditLogService.log(db, principal, ar.AUDIT_MODULE, "POLICY_SAVED", f"Changed reason code {row.code}",
                            "attendance_reason_code", row.id, details={"before": before, "after": ar.reason_json(row)})
        return 200, ar.reason_json(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


# --------------------------------------------------------------------------- on-duty and remote work


@router.get("/attendance/on-duty")
def list_on_duty(mine: bool = False, kind: Optional[str] = Query(None, max_length=12),
                 employee_id: Optional[int] = Query(None, ge=1), status: Optional[str] = Query(None, max_length=60),
                 start: Optional[date] = Query(None, alias="from"), end: Optional[date] = Query(None, alias="to"),
                 principal: Principal = Depends(require_any("attendance:request", "attendance:read",
                                                            "attendance:request_on_behalf")),
                 db: Session = Depends(get_db)):
    return {"items": attendance_on_duty.list_on_duty(db, principal, mine=mine, kind=kind, employee_id=employee_id,
                                                     status=status, start=start, end=end)}


@router.post("/attendance/on-duty", status_code=201)
def file_on_duty(data: OnDutyIn, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    if data.dry_run:
        result = attendance_on_duty.file(db, principal, data.model_dump(mode="json"))
        db.rollback()
        return result

    def handler():
        return 201, attendance_on_duty.file(db, principal, data.model_dump(mode="json"))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.post("/attendance/on-duty/{on_duty_id:int}/cancel")
def cancel_on_duty(on_duty_id: int, data: TransitionIn, request: Request,
                   principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        return 200, attendance_on_duty.cancel(db, principal, on_duty_id, row_version=data.row_version)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


# --------------------------------------------------------------------------- muster


@router.get("/attendance/muster")
def muster_grid(day: date = Query(..., alias="date"), team_id: Optional[int] = Query(None, ge=1),
                branch_id: Optional[int] = Query(None, ge=1),
                principal: Principal = Depends(require("attendance:muster")), db: Session = Depends(get_db)):
    return attendance_muster.grid(db, principal, team_id=team_id, branch_id=branch_id, d=day)


@router.post("/attendance/muster")
def muster_submit(data: MusterIn, request: Request, principal: Principal = Depends(require("attendance:muster")),
                  db: Session = Depends(get_db)):
    def handler():
        return 200, attendance_muster.submit(db, principal, data.model_dump(mode="json"))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


# --------------------------------------------------------------------------- timesheets (§12)


@router.get("/attendance/timesheets")
def timesheet_week(week: date = Query(...), employee_id: Optional[int] = Query(None, ge=1),
                   principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return attendance_timesheets.week(db, principal, employee_id=employee_id, start=week)


@router.post("/attendance/timesheets/submit")
def timesheet_submit(data: TimesheetSubmitIn, request: Request, principal: Principal = Depends(get_principal),
                     db: Session = Depends(get_db)):
    def handler():
        return 200, attendance_timesheets.submit(db, principal, start=data.week_start)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


# --------------------------------------------------------------------------- period close


@router.get("/attendance/period-closes")
def list_period_closes(principal: Principal = Depends(require("attendance:close_period")),
                       db: Session = Depends(get_db)):
    return {"items": attendance_cycles.list_closes(db, principal)}


@router.post("/attendance/period-closes", status_code=201)
def create_period_close(data: PeriodCloseIn, request: Request,
                        principal: Principal = Depends(require("attendance:close_period")),
                        db: Session = Depends(get_db)):
    def handler():
        return 201, attendance_cycles.create(db, principal, data.model_dump(mode="json"))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.post("/attendance/period-closes/{close_id:int}/close")
def close_period(close_id: int, data: CloseIn, request: Request,
                 principal: Principal = Depends(require("attendance:close_period")), db: Session = Depends(get_db)):
    def handler():
        return 200, attendance_cycles.close(db, principal, close_id, acknowledge_open_items=data.acknowledge_open_items,
                                            row_version=data.row_version)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.post("/attendance/period-closes/{close_id:int}/reopen")
def reopen_period(close_id: int, data: ReopenIn, request: Request,
                  principal: Principal = Depends(require("attendance:close_period")), db: Session = Depends(get_db)):
    def handler():
        return 200, attendance_cycles.reopen(db, principal, close_id, reason=data.reason)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


# --------------------------------------------------------------------------- approval routes (§9.2)


def _route_json(db: Session, route: ApprovalRoute) -> dict:
    steps = (db.query(ApprovalChainStep).filter(ApprovalChainStep.company_id == route.company_id,
                                                ApprovalChainStep.route_id == route.id)
             .order_by(ApprovalChainStep.step_order).all())
    return {"id": route.id, "module": route.module, "route_key": route.route_key, "name": route.name,
            "priority": route.priority, "criteria": route.criteria, "status": route.status,
            "row_version": route.row_version,
            "steps": [{"step_order": s.step_order, "approver_type": s.approver_type, "role_id": s.role_id,
                       "specific_user_id": s.specific_user_id, "sla_hours": s.sla_hours, "condition": s.condition,
                       "approver_label": approvals.APPROVER_LABELS.get(s.approver_type, s.approver_type)}
                      for s in steps]}


@router.get("/approval-routes")
def list_routes(module: Literal["attendance", "timesheet", "shift_request"] = "attendance",
                principal: Principal = Depends(require_any(MANAGE, "attendance:approve")),
                db: Session = Depends(get_db)):
    rows = (db.query(ApprovalRoute).filter(ApprovalRoute.company_id == principal.company_id,
                                           ApprovalRoute.module == module)
            .order_by(ApprovalRoute.priority.desc(), ApprovalRoute.id).all())
    return {"items": [_route_json(db, r) for r in rows]}


def _save_route(db: Session, principal: Principal, data: RouteIn, route: Optional[ApprovalRoute]) -> dict:
    if data.auto_approve not in (None, False):
        raise bad_request("Attendance requests are never approved automatically.", "ROUTE_INVALID")
    criteria = approvals.validate_criteria(data.criteria)
    for step in data.steps:
        if (step.approver_type == "role") != (step.role_id is not None):
            raise bad_request("A role step names a role (and only role steps do).", "ROUTE_INVALID")
        if (step.approver_type == "specific_user") != (step.specific_user_id is not None):
            raise bad_request("A named-approver step names a user.", "ROUTE_INVALID")
        if step.condition:
            approvals.validate_criteria(step.condition)
    clash = db.query(ApprovalRoute.id).filter(
        ApprovalRoute.company_id == principal.company_id, ApprovalRoute.module == data.module,
        ApprovalRoute.status == "ACTIVE", ApprovalRoute.priority == data.priority,
        ApprovalRoute.id != (route.id if route is not None else 0)).first()
    if clash is not None and data.status == "ACTIVE":
        raise conflict("Another active route has this priority.", "ROUTE_INVALID")
    before = _route_json(db, route) if route is not None else None
    if route is None:
        route = ApprovalRoute(company_id=principal.company_id, module=data.module, route_key=data.route_key,
                              name=data.name, priority=data.priority, criteria=criteria, status=data.status)
        db.add(route)
        db.flush()
    else:
        if data.row_version is not None and data.row_version != route.row_version:
            raise conflict("This route changed since you loaded it.", "ROW_VERSION_CONFLICT")
        route.name, route.priority, route.criteria, route.status = data.name, data.priority, criteria, data.status
        route.row_version += 1
        db.query(ApprovalChainStep).filter(ApprovalChainStep.company_id == principal.company_id,
                                           ApprovalChainStep.route_id == route.id).delete(synchronize_session=False)
        db.flush()
    for order, step in enumerate(data.steps, start=1):
        db.add(ApprovalChainStep(company_id=principal.company_id, module=route.module, route_id=route.id,
                                 step_order=order, approver_type=step.approver_type, role_id=step.role_id,
                                 specific_user_id=step.specific_user_id, sla_hours=step.sla_hours,
                                 condition=approvals.validate_criteria(step.condition) if step.condition else None))
    db.flush()
    after = _route_json(db, route)
    AuditLogService.log(db, principal, ar.AUDIT_MODULE, "APPROVAL_ROUTE_SAVED",
                        f"Saved approval route {route.route_key} ({route.module})", "approval_route", route.id,
                        details={"before": before, "after": after})
    return after


@router.post("/approval-routes", status_code=201)
def create_route(data: RouteIn, request: Request, principal: Principal = Depends(require(MANAGE)),
                 db: Session = Depends(get_db)):
    def handler():
        if db.query(ApprovalRoute.id).filter(ApprovalRoute.company_id == principal.company_id,
                                             ApprovalRoute.module == data.module,
                                             ApprovalRoute.route_key == data.route_key).first():
            raise conflict("A route with this key exists already.", "DUPLICATE_CODE")
        return 201, _save_route(db, principal, data, None)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.put("/approval-routes/{route_id:int}")
def update_route(route_id: int, data: RouteIn, request: Request, principal: Principal = Depends(require(MANAGE)),
                 db: Session = Depends(get_db)):
    def handler():
        route = (db.query(ApprovalRoute).filter(ApprovalRoute.company_id == principal.company_id,
                                                ApprovalRoute.id == route_id).with_for_update().first())
        if route is None or route.module != data.module or route.route_key != data.route_key:
            raise not_found("Approval route")
        return 200, _save_route(db, principal, data, route)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


# --------------------------------------------------------------------------- module-neutral inbox (§9.8)


def _subject(db: Session, principal: Principal, item: dict) -> Optional[dict]:
    if item["module"] == "timesheet":
        from app.models.attendance_requests import AttendanceTimesheetApproval

        row = db.query(AttendanceTimesheetApproval).filter(
            AttendanceTimesheetApproval.company_id == principal.company_id,
            AttendanceTimesheetApproval.id == item["subject_id"]).first()
        if row is None:
            return None
        return {"summary": f"Timesheet {row.period_start.isoformat()} – {row.period_end.isoformat()}",
                "shift_date": row.period_start.isoformat(), "status": row.status,
                "link": f"/attendance/timesheets?employee_id={row.employee_id}&week={row.period_start.isoformat()}"}
    if item["module"] == "shift_request":
        from app.models.employee import Employee
        from app.models.shift_requests import ShiftRequest

        row = db.query(ShiftRequest).filter(ShiftRequest.company_id == principal.company_id,
                                            ShiftRequest.id == item["subject_id"]).first()
        if row is None:
            return None
        requester = db.get(Employee, row.requester_employee_id)
        who = requester.full_name if requester is not None else "Someone"
        return {"summary": f"{row.kind.title()} by {who} for {row.shift_date.strftime('%a %d %b')}",
                "shift_date": row.shift_date.isoformat(), "status": row.status,
                "link": "/attendance/shift-requests"}
    if item["module"] == "team_inputs":
        from app.models.team_payroll import TeamInputBatch

        row = db.query(TeamInputBatch).filter(TeamInputBatch.company_id == principal.company_id,
                                              TeamInputBatch.id == item["subject_id"]).first()
        if row is None:
            return None
        return {"summary": f"Team payroll batch \"{row.title}\" ({row.period_start.isoformat()} – "
                           f"{row.period_end.isoformat()})", "status": row.status,
                "link": f"/payroll/team-inputs?batch={row.id}"}
    if item["module"] != "attendance":
        return None
    if item["subject_type"] == ar.SUBJECT:
        row = db.query(ar.AttendanceRequest).filter(ar.AttendanceRequest.company_id == principal.company_id,
                                                    ar.AttendanceRequest.id == item["subject_id"]).first()
        return ar.serialize(db, principal, row) if row is not None else None
    if item["subject_type"] == attendance_on_duty.SUBJECT:
        from app.models.employee import Employee
        from app.models.time_inputs import OnDutyRequest

        rows = (db.query(OnDutyRequest).filter(OnDutyRequest.company_id == principal.company_id,
                                               OnDutyRequest.approval_request_id == item["id"])
                .order_by(OnDutyRequest.date).all())
        if not rows:
            rows = db.query(OnDutyRequest).filter(OnDutyRequest.company_id == principal.company_id,
                                                  OnDutyRequest.id == item["subject_id"]).all()
        if not rows:
            return None
        employee = db.get(Employee, rows[0].employee_id)
        first = attendance_on_duty.on_duty_json(rows[0], employee)
        return {**first, "dates": [r.date.isoformat() for r in rows],
                "summary": f"{'Remote work' if rows[0].kind == 'REMOTE_WORK' else 'On duty'} "
                           f"{rows[0].date.isoformat()}" + (f" – {rows[-1].date.isoformat()}" if len(rows) > 1 else "")}
    return None


@router.get("/approvals")
def inbox(module: Optional[str] = Query(None, max_length=20),
          tab: Literal["pending", "decided", "stuck"] = "pending", skip: int = Query(0, ge=0),
          limit: int = Query(50, ge=1, le=200), principal: Principal = Depends(get_principal),
          db: Session = Depends(get_db)):
    """Waiting for me · Decided by me · Stuck (``NO_ELIGIBLE_APPROVER``, holders of ``attendance:regularize``)."""
    if module is not None and module not in approvals.ENGINE_MODULES:
        raise bad_request("Unknown approval module.", "INVALID_APPROVAL_MODULE")
    if tab == "pending":
        total, items = approvals.list_requests(db, principal, module=module, mine=True, skip=skip, limit=limit)
    else:
        total, items = approvals.list_requests(db, principal, module=module, mine=False, skip=0, limit=1000)
        if tab == "decided":
            items = [i for i in items if any(s["acted_by"] and i["status"] != "PENDING" for s in i["steps"])
                     and _acted_by_me(db, principal, i["id"])]
        else:
            if not principal.has("attendance:regularize"):
                raise forbidden()
            items = [i for i in items if i["status"] == "PENDING" and i["pending_reason"] == "NO_ELIGIBLE_APPROVER"]
        total, items = len(items), items[skip: skip + limit]
    for item in items:
        item["subject"] = _subject(db, principal, item)
    return {"total": total, "items": items}


def _acted_by_me(db: Session, principal: Principal, request_id: int) -> bool:
    from app.models.approvals import ApprovalRequestStep

    return db.query(ApprovalRequestStep.id).filter(ApprovalRequestStep.company_id == principal.company_id,
                                                   ApprovalRequestStep.request_id == request_id,
                                                   ApprovalRequestStep.acted_by_user_id == principal.user_id
                                                   ).first() is not None


@router.get("/approvals/counts")
def inbox_counts(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return approvals.request_counts(db, principal, mine=True)


def _decide(db: Session, principal: Principal, request_id: int, data: DecideIn) -> dict:
    request = approvals.decide(db, principal, request_id, data.decision, data.remarks, data.expected_step)
    if request.module == "attendance" and data.reason_code:
        row = db.query(ar.AttendanceRequest).filter(ar.AttendanceRequest.company_id == principal.company_id,
                                                    ar.AttendanceRequest.approval_request_id == request.id).first()
        if row is not None:
            row.decision_reason_code = data.reason_code
    db.flush()
    return approvals.action_result(request)


@router.post("/approvals/{request_id:int}/decide")
def decide(request_id: int, data: DecideIn, request: Request, principal: Principal = Depends(get_principal),
           db: Session = Depends(get_db)):
    def handler():
        return 200, _decide(db, principal, request_id, data)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.post("/approvals/decide-bulk")
def decide_bulk(data: BulkDecideIn, request: Request, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    """Per-item results (``APPLIED`` / ``STALE`` / ``FORBIDDEN`` / ``NOT_PENDING``), each in its own savepoint."""
    from app.core.errors import AppError

    def handler():
        results = []
        for request_id in sorted(set(data.request_ids)):
            found = db.query(ApprovalRequest.module).filter(ApprovalRequest.company_id == principal.company_id,
                                                            ApprovalRequest.id == request_id).scalar()
            if found is None or (data.module and found != data.module):
                results.append({"id": request_id, "result": "FORBIDDEN"})
                continue
            try:
                with db.begin_nested():
                    _decide(db, principal, request_id, DecideIn(decision=data.decision, remarks=data.remarks))
                results.append({"id": request_id, "result": "APPLIED"})
            except AppError as exc:
                result = {"REQUEST_STALE": "STALE", "APPROVAL_ALREADY_DECIDED": "NOT_PENDING"}.get(exc.code,
                                                                                                "FORBIDDEN")
                results.append({"id": request_id, "result": result, "code": exc.code, "message": exc.detail})
        return 200, {"results": results, "applied": sum(r["result"] == "APPLIED" for r in results)}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))

"""Shift requests, open shifts, availability, skill tags and shift acknowledgement (AS-M6; 03-shifts/03 §13).

``GET|POST /shift-requests`` · ``POST /shift-requests/preview`` · ``GET /shift-requests/{id}`` ·
``POST /shift-requests/{id}/accept|decline|withdraw|approve|reject|extend`` ·``GET|POST /open-shifts`` ·
``GET|PATCH /open-shifts/{id}`` ·``POST /open-shifts/{id}/claim|cancel`` · ``GET|POST /me/availability`` ·
``POST /me/availability/{id}/cancel`` · ``GET /availability`` · ``POST /availability/{id}/approve|reject`` ·
``GET|POST /skill-tags`` · ``PUT /employees/{id}/skill-tags`` · ``POST /me/schedule/entries/{id}/acknowledge``.
Writes go through ``idempotency.execute`` (which commits). Permissions are checked by the service.
"""

from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.payroll.services import idempotency
from app.services import shift_requests as service

router = APIRouter(prefix="/api/v1", tags=["Shift requests"])


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequestedChange(_Strict):
    shift_id: Optional[int] = Field(None, ge=1)
    custom_start_time: Optional[str] = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    custom_end_time: Optional[str] = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    site_id: Optional[int] = Field(None, ge=1)
    planned_work_mode: Optional[Literal["ON_SITE", "REMOTE", "FIELD"]] = None


class ShiftRequestIn(_Strict):
    kind: Literal["SWAP", "DROP", "RELEASE", "CHANGE"]
    entry_id: int = Field(..., ge=1)
    target_employee_id: Optional[int] = Field(None, ge=1)
    counter_entry_id: Optional[int] = Field(None, ge=1)
    requested_change: Optional[RequestedChange] = None
    reason: Optional[str] = Field(None, max_length=500)
    acknowledged_warnings: List[str] = Field(default_factory=list, max_length=30)
    refile_of_request_id: Optional[int] = Field(None, ge=1)  # §4.4 re-file after an employer supersede


class ExtendIn(_Strict):
    expires_at: datetime
    reason: str = Field(..., min_length=3, max_length=500)


class AcceptIn(_Strict):
    acknowledged_warnings: List[str] = Field(default_factory=list, max_length=30)
    reason: Optional[str] = Field(None, max_length=500)
    consent_given: bool = False  # SCHEDULE_CHANGE_ACCEPTED inside the pack's notice window (§7.3)


class NoteIn(_Strict):
    note: Optional[str] = Field(None, max_length=500)
    acknowledged_warnings: List[str] = Field(default_factory=list, max_length=30)


class OpenShiftIn(_Strict):
    branch_id: int = Field(..., ge=1)
    team_id: Optional[int] = Field(None, ge=1)
    site_id: Optional[int] = Field(None, ge=1)
    shift_date: date
    shift_id: Optional[int] = Field(None, ge=1)
    custom_start_time: Optional[str] = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    custom_end_time: Optional[str] = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    required_count: int = Field(1, ge=1, le=50)
    required_tags: List[str] = Field(default_factory=list, max_length=10)
    approval_required: bool = True
    same_team: Optional[bool] = None
    exclusive_window_hours: int = Field(0, ge=0, le=168)
    source: Literal["MANUAL", "UNCOVERED"] = "MANUAL"
    note: Optional[str] = Field(None, max_length=500)


class OpenShiftPatchIn(_Strict):
    row_version: int = Field(..., ge=1)
    required_count: Optional[int] = Field(None, ge=1, le=50)
    expires_at: Optional[datetime] = None
    shift_id: Optional[int] = Field(None, ge=1)
    custom_start_time: Optional[str] = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    custom_end_time: Optional[str] = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    required_tags: Optional[List[str]] = Field(None, max_length=10)
    same_team: Optional[bool] = None
    note: Optional[str] = Field(None, max_length=500)


class ClaimIn(_Strict):
    reason: Optional[str] = Field(None, max_length=500)
    acknowledged_warnings: List[str] = Field(default_factory=list, max_length=30)
    consent_given: bool = False


class AvailabilityIn(_Strict):
    kind: Literal["UNAVAILABLE", "PREFERRED"] = "UNAVAILABLE"
    weekday: Optional[int] = Field(None, ge=0, le=6)
    start_date: date
    end_date: Optional[date] = None
    start_time: Optional[str] = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end_time: Optional[str] = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    note: Optional[str] = Field(None, max_length=300)


class TagIn(_Strict):
    code: str = Field(..., min_length=1, max_length=40)
    name: str = Field(..., min_length=1, max_length=100)


class EmployeeTagsIn(_Strict):
    tags: List[Dict[str, Any]] = Field(default_factory=list, max_length=30)


def _write(db: Session, principal: Principal, request: Request, body: Any, run, status_code: int = 200):
    return idempotency.execute(db, principal, request, lambda: (status_code, run()),
                               body=body.model_dump(mode="json") if hasattr(body, "model_dump") else (body or {}))


class SettingsIn(_Strict):
    requests_enabled: Optional[bool] = None
    open_shifts_enabled: Optional[bool] = None
    planner_cannot_publish: Optional[bool] = None  # shift.roster.planner_cannot_publish (EC AS-ROS-11)
    publish_requires_approval: Optional[bool] = None  # shift.roster.publish_requires_approval (§5.2)
    requests: Optional[Dict[str, Any]] = None
    open_shift_eligibility: Optional[Dict[str, Any]] = None
    availability: Optional[Dict[str, Any]] = None
    acknowledge_published_shifts: Optional[bool] = None
    reason: str = Field("Shift request settings", min_length=3, max_length=500)


# ----------------------------------------------------------------------------------------------- settings
@router.get("/shift-requests/settings")
def get_settings(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.settings_view(db, principal)


@router.put("/shift-requests/settings")
def put_settings(data: SettingsIn, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    body = data.model_dump(exclude_unset=True)
    reason = body.pop("reason", "Shift request settings")
    return _write(db, principal, request, data, lambda: service.save_settings(db, principal, body, reason))


# ----------------------------------------------------------------------------------------------- shift requests
@router.get("/shift-requests")
def list_requests(box: Literal["mine", "incoming", "approvals"] = "mine",
                  status: Optional[str] = Query(None, max_length=20),
                  principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.list_requests(db, principal, box=box, status=status)


@router.post("/shift-requests/preview")
def preview(data: Dict[str, Any], principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.preview(db, principal, data)


@router.post("/shift-requests", status_code=201)
def create(data: ShiftRequestIn, request: Request, principal: Principal = Depends(get_principal),
           db: Session = Depends(get_db)):
    body = data.model_dump(mode="python")
    if data.requested_change is not None:
        body["requested_change"] = data.requested_change.model_dump(exclude_none=True)
    return _write(db, principal, request, data,
                  lambda: service.serialize(db, service.create(db, principal, body), principal), 201)


@router.get("/shift-requests/{request_id:int}")
def get_request(request_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.get(db, principal, request_id)


@router.post("/shift-requests/{request_id:int}/accept")
def accept(request_id: int, data: AcceptIn, request: Request, principal: Principal = Depends(get_principal),
           db: Session = Depends(get_db)):
    return _write(db, principal, request, data, lambda: service.serialize(
        db, service.accept(db, principal, request_id, data.model_dump()), principal))


@router.post("/shift-requests/{request_id:int}/decline")
def decline(request_id: int, data: NoteIn, request: Request, principal: Principal = Depends(get_principal),
            db: Session = Depends(get_db)):
    return _write(db, principal, request, data, lambda: service.serialize(
        db, service.decline(db, principal, request_id, data.note), principal))


@router.post("/shift-requests/{request_id:int}/withdraw")
def withdraw(request_id: int, request: Request, principal: Principal = Depends(get_principal),
             db: Session = Depends(get_db)):
    return _write(db, principal, request, {}, lambda: service.serialize(
        db, service.withdraw(db, principal, request_id), principal))


@router.post("/shift-requests/{request_id:int}/approve")
def approve(request_id: int, data: NoteIn, request: Request, principal: Principal = Depends(get_principal),
            db: Session = Depends(get_db)):
    return _write(db, principal, request, data, lambda: service.serialize(
        db, service.decide(db, principal, request_id, "APPROVE", data.note, data.acknowledged_warnings), principal))


@router.post("/shift-requests/{request_id:int}/reject")
def reject(request_id: int, data: NoteIn, request: Request, principal: Principal = Depends(get_principal),
           db: Session = Depends(get_db)):
    return _write(db, principal, request, data, lambda: service.serialize(
        db, service.decide(db, principal, request_id, "REJECT", data.note), principal))


@router.post("/shift-requests/{request_id:int}/extend")
def extend(request_id: int, data: ExtendIn, request: Request, principal: Principal = Depends(get_principal),
           db: Session = Depends(get_db)):
    return _write(db, principal, request, data, lambda: service.serialize(
        db, service.extend(db, principal, request_id, data.expires_at, data.reason), principal))


# ----------------------------------------------------------------------------------------------- open shifts
@router.get("/open-shifts")
def list_open(start: Optional[date] = Query(None, alias="from"), end: Optional[date] = Query(None, alias="to"),
              status: Optional[str] = Query(None, max_length=10), eligible_only: Optional[bool] = Query(None),
              principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.list_open_shifts(db, principal, start=start, end=end, status=status, eligible_only=eligible_only)


@router.post("/open-shifts", status_code=201)
def post_open(data: OpenShiftIn, request: Request, principal: Principal = Depends(get_principal),
              db: Session = Depends(get_db)):
    body = data.model_dump(mode="python")
    body["shift_date"] = data.shift_date.isoformat()
    return _write(db, principal, request, data,
                  lambda: service.serialize_open_shift(db, service.post_open_shift(db, principal, body)), 201)


@router.get("/open-shifts/{open_shift_id:int}")
def open_detail(open_shift_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.open_shift_detail(db, principal, open_shift_id)


@router.patch("/open-shifts/{open_shift_id:int}")
def edit_open(open_shift_id: int, data: OpenShiftPatchIn, request: Request,
              principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    body = data.model_dump(mode="json", exclude_unset=True)
    return _write(db, principal, request, data, lambda: service.serialize_open_shift(
        db, service.edit_open_shift(db, principal, open_shift_id, body)))


@router.post("/open-shifts/{open_shift_id:int}/claim", status_code=201)
def claim(open_shift_id: int, data: ClaimIn, request: Request, principal: Principal = Depends(get_principal),
          db: Session = Depends(get_db)):
    return _write(db, principal, request, data, lambda: service.serialize(
        db, service.claim(db, principal, open_shift_id, data.model_dump()), principal), 201)


@router.post("/open-shifts/{open_shift_id:int}/cancel")
def cancel_open(open_shift_id: int, request: Request, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    return _write(db, principal, request, {}, lambda: service.serialize_open_shift(
        db, service.cancel_open_shift(db, principal, open_shift_id)))


# ----------------------------------------------------------------------------------------------- availability
@router.get("/me/availability")
def my_availability(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return {"items": service.my_availability(db, principal)}


@router.post("/me/availability", status_code=201)
def add_availability(data: AvailabilityIn, request: Request, principal: Principal = Depends(get_principal),
                     db: Session = Depends(get_db)):
    body = data.model_dump(mode="python")
    body["start_date"] = data.start_date.isoformat()
    body["end_date"] = data.end_date.isoformat() if data.end_date else None
    return _write(db, principal, request, data, lambda: service.serialize_availability(
        service.add_availability(db, principal, body)), 201)


@router.post("/me/availability/{availability_id:int}/cancel")
def cancel_availability(availability_id: int, request: Request, principal: Principal = Depends(get_principal),
                        db: Session = Depends(get_db)):
    return _write(db, principal, request, {}, lambda: service.serialize_availability(
        service.cancel_availability(db, principal, availability_id)))


@router.get("/availability")
def team_availability(status: Optional[str] = Query(None, max_length=10), principal: Principal = Depends(get_principal),
                      db: Session = Depends(get_db)):
    return {"items": service.team_availability(db, principal, status)}


@router.post("/availability/{availability_id:int}/{decision}")
def decide_availability(availability_id: int, decision: Literal["approve", "reject"], request: Request,
                        principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return _write(db, principal, request, {}, lambda: service.serialize_availability(
        service.decide_availability(db, principal, availability_id, decision.upper())))


# ----------------------------------------------------------------------------------------------- skill tags
@router.get("/skill-tags")
def list_tags(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return {"items": service.list_tags(db, principal)}


@router.post("/skill-tags", status_code=201)
def create_tag(data: TagIn, request: Request, principal: Principal = Depends(get_principal),
               db: Session = Depends(get_db)):
    def run():
        row = service.create_tag(db, principal, data.code, data.name)
        return {"id": row.id, "code": row.code, "name": row.name, "status": row.status, "holders": 0}

    return _write(db, principal, request, data, run, 201)


@router.put("/employees/{employee_id:int}/skill-tags")
def set_tags(employee_id: int, data: EmployeeTagsIn, request: Request, principal: Principal = Depends(get_principal),
             db: Session = Depends(get_db)):
    return _write(db, principal, request, data,
                  lambda: {"items": service.set_employee_tags(db, principal, employee_id, data.tags)})


# ----------------------------------------------------------------------------------------------- acknowledgement
@router.post("/me/schedule/entries/{entry_id:int}/acknowledge")
def acknowledge(entry_id: int, request: Request, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    return _write(db, principal, request, {}, lambda: service.acknowledge_entry(db, principal, entry_id))

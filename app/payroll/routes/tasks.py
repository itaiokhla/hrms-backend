"""Tasks module and task rate cards (3.4 §10, 3.9 §4 Inputs)."""

import datetime as dt
import hashlib
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.core import storage
from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.models.work_items import TASK_STATUSES
from app.payroll.api_types import DecimalStr, StrictModel, money_in_model
from app.payroll.services import idempotency
from app.payroll.services import tasks as service

router = APIRouter(prefix="/api/v1", tags=["Tasks"])

StatusName = Literal[TASK_STATUSES]  # type: ignore[valid-type]
RateIn = money_in_model("TaskRateIn", allow_extra_precision=True, payroll_currency_only=True, allow_negative=False)


def _clean(value: Optional[str]) -> Optional[str]:
    return (value.strip() or None) if value is not None else None


class AssigneeIn(StrictModel):
    employee_id: int = Field(..., ge=1)
    split_percent: Optional[DecimalStr] = None


class _TaskFields(StrictModel):
    description: Optional[str] = Field(None, max_length=5000)
    project: Optional[str] = Field(None, max_length=100)
    category: Optional[str] = Field(None, max_length=100)
    rate: Optional[RateIn] = None  # type: ignore[valid-type]
    due_date: Optional[dt.date] = None
    completed_on: Optional[dt.date] = None
    external_ref: Optional[str] = Field(None, min_length=1, max_length=100)

    @field_validator("description", "project", "category", "external_ref")
    @classmethod
    def _strip(cls, value: Optional[str]) -> Optional[str]:
        return _clean(value)


class TaskCreate(_TaskFields):
    title: str = Field(..., min_length=1, max_length=200)
    task_type: str = Field(..., min_length=1, max_length=50, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\- ]*$")
    quantity: DecimalStr
    unit: str = Field(..., min_length=1, max_length=30)
    assignees: List[AssigneeIn] = Field(..., min_length=1, max_length=service.MAX_ASSIGNEES)

    @field_validator("title", "unit")
    @classmethod
    def _required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class TaskUpdate(_TaskFields):
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    task_type: Optional[str] = Field(None, min_length=1, max_length=50, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\- ]*$")
    quantity: Optional[DecimalStr] = None
    unit: Optional[str] = Field(None, min_length=1, max_length=30)
    assignees: Optional[List[AssigneeIn]] = Field(None, min_length=1, max_length=service.MAX_ASSIGNEES)
    row_version: Optional[int] = Field(None, ge=1)

    @model_validator(mode="after")
    def _no_nulls(self) -> "TaskUpdate":
        for name in ("title", "task_type", "quantity", "unit", "assignees"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        for name in ("title", "unit"):
            value = getattr(self, name)
            if value is not None and not value.strip():
                raise ValueError(f"{name} must not be blank")
        return self


class TaskApprove(StrictModel):
    accepted_quantity: Optional[DecimalStr] = None
    remarks: Optional[str] = Field(None, max_length=1000)
    expected_step: Optional[int] = Field(None, ge=1)

    @field_validator("remarks")
    @classmethod
    def _strip(cls, value: Optional[str]) -> Optional[str]:
        return _clean(value)


class TaskReject(StrictModel):
    remarks: str = Field(..., min_length=1, max_length=1000)
    expected_step: Optional[int] = Field(None, ge=1)

    @field_validator("remarks")
    @classmethod
    def _required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Give a reason for the rejection.")
        return value


class ReasonBody(StrictModel):
    reason: str = Field(..., min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Give a reason.")
        return value


class RateCardCriteria(StrictModel):
    grade: Optional[str] = Field(None, min_length=1, max_length=50)
    skill: Optional[str] = Field(None, min_length=1, max_length=50)
    location: Optional[str] = Field(None, min_length=1, max_length=50)


class RateCardCreate(StrictModel):
    task_type: str = Field(..., min_length=1, max_length=50, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\- ]*$")
    criteria: Optional[RateCardCriteria] = None
    rate: RateIn  # type: ignore[valid-type]
    effective_from: dt.date
    effective_to: Optional[dt.date] = None


class RateCardEnd(StrictModel):
    effective_to: dt.date


def _task_data(data: StrictModel, *, exclude_unset: bool) -> dict:
    values = data.model_dump(exclude_unset=exclude_unset, exclude={"row_version"})
    for name in ("quantity",):
        if name in values:
            values[name] = getattr(data, name)
    if "rate" in values:
        rate = getattr(data, "rate")
        values["rate"] = {"amount": rate.amount, "currency": rate.currency} if rate is not None else None
    if "assignees" in values and getattr(data, "assignees") is not None:
        values["assignees"] = [{"employee_id": a.employee_id, "split_percent": a.split_percent}
                               for a in getattr(data, "assignees")]
    return values


# --------------------------------------------------------------------------- tasks


@router.get("/tasks")
def list_tasks(
    status: Optional[StatusName] = Query(None),
    employee_id: Optional[int] = Query(None, ge=1),
    task_type: Optional[str] = Query(None, max_length=50),
    awaiting: bool = Query(False, description="Only tasks awaiting the caller's approval"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=service.MAX_LIST),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    total, items = service.list_tasks(db, principal, status=status, employee_id=employee_id, task_type=task_type,
                                      awaiting=awaiting, skip=skip, limit=limit)
    return {"total": total, "items": items}


@router.post("/tasks", status_code=201)
def create_task(data: TaskCreate, request: Request, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    def handler():
        return 201, service.action_result(service.create_task(db, principal, _task_data(data, exclude_unset=False)))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/tasks/{task_id}")
def get_task(task_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.get_task(db, principal, task_id)


@router.patch("/tasks/{task_id}")
def update_task(task_id: int, data: TaskUpdate, request: Request, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    def handler():
        task = service.update_task(db, principal, task_id, _task_data(data, exclude_unset=True),
                                   row_version=data.row_version)
        return 200, service.action_result(task)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json", exclude_unset=True))


@router.post("/tasks/{task_id}/submit")
def submit_task(task_id: int, request: Request, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    def handler():
        return 200, service.action_result(service.submit_task(db, principal, task_id))

    return idempotency.execute(db, principal, request, handler, body={})


@router.post("/tasks/{task_id}/approve")
def approve_task(task_id: int, request: Request, data: Optional[TaskApprove] = None,
                 principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or TaskApprove()

    def handler():
        task = service.decide_task(db, principal, task_id, "APPROVE", accepted_quantity=data.accepted_quantity,
                                   remarks=data.remarks, expected_step=data.expected_step)
        return 200, service.action_result(task)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.post("/tasks/{task_id}/reject")
def reject_task(task_id: int, data: TaskReject, request: Request, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    def handler():
        task = service.decide_task(db, principal, task_id, "REJECT", remarks=data.remarks,
                                   expected_step=data.expected_step)
        return 200, service.action_result(task)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/tasks/{task_id}/dispute")
def dispute_task(task_id: int, data: ReasonBody, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    def handler():
        return 200, service.action_result(service.dispute_task(db, principal, task_id, data.reason))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/tasks/{task_id}/reverse")
def reverse_task(task_id: int, data: ReasonBody, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    def handler():
        return 200, service.action_result(service.reverse_task(db, principal, task_id, data.reason))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/tasks/{task_id}/evidence", status_code=201)
async def add_evidence(task_id: int, request: Request, file: UploadFile = File(...),
                       principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = await file.read(app_settings.max_upload_size + 1)
    body = {"file_name": file.filename, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}

    def handler():
        evidence = service.add_evidence(db, principal, task_id, file_name=file.filename, data=data)
        return 201, {"id": evidence.id, "task_id": task_id, "file_name": evidence.file_name, "mime": evidence.mime,
                     "bytes": evidence.bytes, "sha256": evidence.sha256}

    return idempotency.execute(db, principal, request, handler, body=body)


@router.get("/tasks/{task_id}/evidence/{evidence_id}")
def download_evidence(task_id: int, evidence_id: int, principal: Principal = Depends(get_principal),
                      db: Session = Depends(get_db)):
    path, media_type, file_name = service.evidence_file(db, principal, task_id, evidence_id)
    return FileResponse(path, media_type=media_type, filename=file_name,
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.delete("/tasks/{task_id}/evidence/{evidence_id}", status_code=204)
def remove_evidence(task_id: int, evidence_id: int, principal: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)):
    path = service.remove_evidence(db, principal, task_id, evidence_id)
    db.commit()
    storage.delete(principal.company_id, path)
    return Response(status_code=204)


# --------------------------------------------------------------------------- rate cards


@router.get("/payroll/task-rate-cards")
def list_rate_cards(
    task_type: Optional[str] = Query(None, max_length=50),
    on: Optional[dt.date] = Query(None, description="Only rows effective on this date"),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    return {"items": service.list_rate_cards(db, principal, task_type=task_type, on=on)}


@router.post("/payroll/task-rate-cards", status_code=201)
def create_rate_card(data: RateCardCreate, request: Request, principal: Principal = Depends(get_principal),
                     db: Session = Depends(get_db)):
    def handler():
        row = service.create_rate_card(db, principal, {
            "task_type": data.task_type.strip(),
            "criteria": data.criteria.model_dump(exclude_none=True) if data.criteria else {},
            "rate": {"amount": data.rate.amount, "currency": data.rate.currency},
            "effective_from": data.effective_from, "effective_to": data.effective_to,
        })
        return 201, service.rate_card_json(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.patch("/payroll/task-rate-cards/{rate_card_id}")
def end_rate_card(rate_card_id: int, data: RateCardEnd, request: Request, principal: Principal = Depends(get_principal),
                  db: Session = Depends(get_db)):
    def handler():
        return 200, service.rate_card_json(service.end_rate_card(db, principal, rate_card_id, data.effective_to))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))

"""Work weeks API (LH-M1 WI-1.9; 04-holiday/03 §5.9; 11 §6.11, §6.15).

Reads need ``holiday:read`` (or ``holiday:manage``, ``org:read``); writes need ``holiday:manage`` in the scope of the
pattern (§5.6), backdated versions also ``holiday:publish``. Literal paths (``/templates``, ``/preview``) are
declared before ``/{pattern_id:int}`` (MR-6). State-changing requests accept ``Idempotency-Key``; edits take
``If-Match`` (``row_version``). The effective-dated branch link lives here too (``/api/v1/branches/{id}/calendar-link``).
"""

from datetime import date
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field
from sqlalchemy.orm import Session

from app.core import concurrency
from app.core.principal import Principal, get_principal, require, require_any
from app.database.db import get_db
from app.payroll.services import idempotency
from app.schemas.auth import StrictModel
from app.services import holiday_assignments
from app.services import holiday_work_weeks as service

router = APIRouter(prefix="/api/v1/work-weeks", tags=["Work weeks"])
branches_router = APIRouter(prefix="/api/v1/branches", tags=["Work weeks"])

READ = require_any(*service.READ_CODES)
WRITE = require("holiday:manage")
Kind = Literal["WEEKLY", "CYCLE"]
Status = Literal["ACTIVE", "INACTIVE"]


class WorkWeekCreate(StrictModel):
    code: Optional[str] = Field(None, max_length=30)
    name: str = Field(..., min_length=1, max_length=100)
    name_i18n: Optional[Dict[str, str]] = None
    legal_entity_id: Optional[int] = None
    template_code: Optional[str] = Field(None, max_length=40)
    rules: Optional[Dict[str, Any]] = None
    effective_from: Optional[date] = None
    reason: Optional[str] = Field(None, max_length=500)
    acknowledge_warnings: bool = False


class WorkWeekPatch(StrictModel):
    code: Optional[str] = Field(None, max_length=30)
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    name_i18n: Optional[Dict[str, str]] = None
    status: Optional[Status] = None
    rules: Optional[Dict[str, Any]] = None
    effective_from: Optional[date] = None
    reason: Optional[str] = Field(None, max_length=500)
    acknowledge_warnings: bool = False
    row_version: Optional[int] = Field(None, ge=1)


class VersionIn(StrictModel):
    rules: Dict[str, Any]
    effective_from: Optional[date] = None
    reason: Optional[str] = Field(None, max_length=500)
    acknowledge_warnings: bool = False
    row_version: Optional[int] = Field(None, ge=1)


class PreviewIn(StrictModel):
    rules: Optional[Dict[str, Any]] = None  # optional only with pattern_id (then the rules valid today)
    stored: bool = False  # with pattern_id: each date under its own saved version, no impact
    pattern_id: Optional[int] = None
    legal_entity_id: Optional[int] = None
    effective_from: Optional[date] = None
    reason: Optional[str] = Field(None, max_length=500)
    from_: Optional[date] = Field(None, alias="from")
    to: Optional[date] = None

    model_config = {**StrictModel.model_config, "populate_by_name": True}


class CrewIn(StrictModel):
    code: Optional[str] = Field(None, max_length=30)
    name: str = Field(..., min_length=1, max_length=100)
    offset_days: int = Field(..., ge=1, le=55)
    acknowledge_warnings: bool = False


class BranchLinkIn(StrictModel):
    holiday_calendar_id: Optional[int] = None
    work_week_pattern_id: Optional[int] = None
    effective_from: Optional[date] = None
    reason: Optional[str] = Field(None, max_length=300)
    row_version: Optional[int] = Field(None, ge=1)


def _dump(data: StrictModel) -> dict:
    return data.model_dump(mode="json", exclude_unset=True, by_alias=True)


# ---------------------------------------------------------------------------------------------- literals first
@router.get("/templates")
def templates(principal: Principal = Depends(READ), db: Session = Depends(get_db)):
    return {"items": service.list_templates(db, principal)}


@router.post("/preview")
def preview(data: PreviewIn, principal: Principal = Depends(READ), db: Session = Depends(get_db)):
    values = data.model_dump(exclude_unset=True)
    if "from_" in values:
        values["from"] = values.pop("from_")
    return service.preview(db, principal, values)


@router.get("")
def list_work_weeks(q: Optional[str] = Query(None, max_length=100), kind: Optional[Kind] = None,
                    status: Optional[Status] = None, legal_entity_id: Optional[int] = None,
                    owner: Optional[Literal["TENANT"]] = None, sort: str = Query("code", max_length=20),
                    skip: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100),
                    principal: Principal = Depends(READ), db: Session = Depends(get_db)):
    return service.list_patterns(db, principal, q=q, kind=kind, status=status, legal_entity_id=legal_entity_id,
                                 owner=owner, sort=sort, skip=skip, limit=limit)


@router.post("", status_code=201)
def create_work_week(data: WorkWeekCreate, request: Request, principal: Principal = Depends(WRITE),
                     db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request,
                               lambda: (201, service.create_pattern(db, principal, data.model_dump(exclude_unset=True))),
                               body=_dump(data))


# ---------------------------------------------------------------------------------------------- one pattern
@router.get("/{pattern_id:int}")
def get_work_week(pattern_id: int, principal: Principal = Depends(READ), db: Session = Depends(get_db)):
    return service.get_pattern(db, principal, pattern_id)


@router.patch("/{pattern_id:int}")
def update_work_week(pattern_id: int, data: WorkWeekPatch, request: Request, principal: Principal = Depends(WRITE),
                     db: Session = Depends(get_db)):
    expected = concurrency.if_match(request, data.row_version)
    values = data.model_dump(exclude_unset=True, exclude={"row_version"})
    return idempotency.execute(
        db, principal, request,
        lambda: (200, service.update_pattern(db, principal, pattern_id, values, expected_row_version=expected)),
        body=_dump(data))


@router.get("/{pattern_id:int}/versions")
def list_versions(pattern_id: int, principal: Principal = Depends(READ), db: Session = Depends(get_db)):
    return {"items": service.list_versions(db, principal, pattern_id)}


@router.get("/{pattern_id:int}/history")
def history(pattern_id: int, principal: Principal = Depends(READ), db: Session = Depends(get_db)):
    return {"items": service.history(db, principal, pattern_id)}


@router.post("/{pattern_id:int}/versions", status_code=201)
def add_version(pattern_id: int, data: VersionIn, request: Request, principal: Principal = Depends(WRITE),
                db: Session = Depends(get_db)):
    expected = concurrency.if_match(request, data.row_version)
    values = data.model_dump(exclude_unset=True, exclude={"row_version"})

    def handler():
        result = service.add_version(db, principal, pattern_id, values, expected_row_version=expected)
        row = service._get_visible(db, principal, pattern_id)
        return 201, {**result, "pattern": service.serialize(db, principal, row, detail=True)}

    return idempotency.execute(db, principal, request, handler, body=_dump(data))


@router.post("/{pattern_id:int}/crews", status_code=201)
def duplicate_as_crew(pattern_id: int, data: CrewIn, request: Request, principal: Principal = Depends(WRITE),
                      db: Session = Depends(get_db)):
    return idempotency.execute(
        db, principal, request,
        lambda: (201, service.duplicate_as_crew(db, principal, pattern_id, data.model_dump())), body=_dump(data))


# ---------------------------------------------------------------------------------------------- branch links
@branches_router.get("/{branch_id:int}/calendar-links")
def list_branch_links(branch_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    from app.core.errors import not_found
    from app.models.org import Branch
    from app.models.pay_org import BranchCalendarLink

    branch = db.query(Branch).filter(Branch.company_id == principal.company_id, Branch.id == branch_id).first()
    if branch is None or not (service.can_read(principal)):
        raise not_found("Branch")
    rows = (db.query(BranchCalendarLink).filter(BranchCalendarLink.company_id == principal.company_id,
                                                BranchCalendarLink.branch_id == branch.id)
            .order_by(BranchCalendarLink.effective_from).all())
    return {"items": [holiday_assignments.serialize_link(r) for r in rows]}


@branches_router.put("/{branch_id:int}/calendar-link")
def set_branch_link(branch_id: int, data: BranchLinkIn, request: Request, principal: Principal = Depends(WRITE),
                    db: Session = Depends(get_db)):
    expected = concurrency.if_match(request, data.row_version)
    values = data.model_dump(exclude_unset=True, exclude={"row_version"})
    return idempotency.execute(
        db, principal, request,
        lambda: (200, holiday_assignments.set_branch_link(db, principal, branch_id, values,
                                                          expected_row_version=expected)),
        body=_dump(data))

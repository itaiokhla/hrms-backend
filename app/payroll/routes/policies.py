"""Payroll policies API (3.1 §3.2, §9): policies, versions, assignment rules, resolution, archive."""

from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import Field
from sqlalchemy.orm import Session

from app.core.principal import Principal, require
from app.database.db import get_db
from app.payroll.api_types import StrictModel
from app.payroll.services import idempotency
from app.payroll.services import policies as service

router = APIRouter(prefix="/api/v1/payroll/policies", tags=["Payroll policies"])


class PolicyIn(StrictModel):
    type: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=150)
    description: Optional[str] = Field(None, max_length=2000)
    fields: Optional[Dict[str, Any]] = None
    effective_from: Optional[date] = None


class VersionIn(StrictModel):
    fields: Optional[Dict[str, Any]] = None
    effective_from: Optional[date] = None


class ActivateIn(StrictModel):
    effective_from: Optional[date] = None
    reason: Optional[str] = Field(None, max_length=500)


class RuleIn(StrictModel):
    criteria: Dict[str, Any] = Field(default_factory=lambda: {"everyone": True})
    priority: int = Field(0, ge=0, le=1_000_000)
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None


class AssignmentsIn(StrictModel):
    rules: List[RuleIn] = Field(max_length=service.MAX_RULES)


def _dump(data: Optional[StrictModel]) -> Any:
    return data.model_dump(mode="json") if data is not None else None


@router.get("")
def list_policies(type: Optional[str] = Query(None, max_length=40), include_archived: bool = False,
                  principal: Principal = Depends(require(service.READ)), db: Session = Depends(get_db)):
    return service.list_policies(db, principal, type_code=type, include_archived=include_archived)


@router.post("", status_code=201)
def create_policy(data: PolicyIn, request: Request, principal: Principal = Depends(require(service.WRITE)),
                  db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (201, service.create_policy(
        db, principal, type_code=data.type, name=data.name, description=data.description, fields=data.fields,
        effective_from=data.effective_from)), body=_dump(data))


@router.get("/resolve")
def resolve(type: str = Query(..., max_length=40), employee_id: int = Query(..., ge=1),
            on: Optional[date] = Query(None, alias="date"),
            principal: Principal = Depends(require(service.READ)), db: Session = Depends(get_db)):
    return service.resolve_policy(db, principal, type, employee_id, on)


@router.get("/{policy_id}")
def get_policy(policy_id: int, principal: Principal = Depends(require(service.READ)), db: Session = Depends(get_db)):
    return service.get_policy(db, principal, policy_id)


@router.post("/{policy_id}/versions", status_code=201)
def create_version(policy_id: int, request: Request, data: Optional[VersionIn] = Body(None),
                   principal: Principal = Depends(require(service.WRITE)), db: Session = Depends(get_db)):
    body = data or VersionIn()
    return idempotency.execute(db, principal, request, lambda: (201, service.create_version(
        db, principal, policy_id, fields=body.fields, effective_from=body.effective_from)), body=_dump(data))


@router.put("/{policy_id}/versions/{version}")
def update_version(policy_id: int, version: int, data: VersionIn, request: Request,
                   principal: Principal = Depends(require(service.WRITE)), db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (200, service.update_version(
        db, principal, policy_id, version, fields=data.fields, effective_from=data.effective_from)), body=_dump(data))


@router.post("/{policy_id}/versions/{version}/activate")
def activate_version(policy_id: int, version: int, request: Request, data: Optional[ActivateIn] = Body(None),
                     principal: Principal = Depends(require(service.WRITE)), db: Session = Depends(get_db)):
    body = data or ActivateIn()
    return idempotency.execute(db, principal, request, lambda: (200, service.activate_version(
        db, principal, policy_id, version, effective_from=body.effective_from, reason=body.reason)), body=_dump(data))


@router.get("/{policy_id}/assignments")
def get_assignments(policy_id: int, principal: Principal = Depends(require(service.READ)),
                    db: Session = Depends(get_db)):
    return service.get_assignments(db, principal, policy_id)


@router.put("/{policy_id}/assignments")
def put_assignments(policy_id: int, data: AssignmentsIn, request: Request,
                    principal: Principal = Depends(require(service.WRITE)), db: Session = Depends(get_db)):
    rules = [service.RuleInput(r.criteria, r.priority, r.effective_from, r.effective_to) for r in data.rules]
    return idempotency.execute(db, principal, request,
                               lambda: (200, service.put_assignments(db, principal, policy_id, rules)), body=_dump(data))


@router.post("/{policy_id}/archive")
def archive(policy_id: int, request: Request, principal: Principal = Depends(require(service.WRITE)),
            db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (200, service.archive_policy(db, principal, policy_id)))

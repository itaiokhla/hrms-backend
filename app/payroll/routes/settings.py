"""Payroll settings engine v2 API (3.1 §9): definitions, values per scope, explain view, maker-checker."""

from datetime import date
from typing import Any, List, Literal, Optional

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field
from sqlalchemy.orm import Session

from app.core.errors import not_found
from app.core.principal import Principal, require, require_any
from app.database.db import get_db
from app.payroll import policy_types
from app.payroll import settings_registry as reg
from app.payroll.api_types import StrictModel
from app.payroll.services import idempotency
from app.payroll.services import settings as engine

router = APIRouter(prefix="/api/v1/payroll/settings", tags=["Payroll settings"])

ScopeType = Literal["TENANT", "LEGAL_ENTITY", "BRANCH", "PAY_GROUP", "EMPLOYEE"]


class ChangeIn(StrictModel):
    key: str = Field(min_length=1, max_length=160)
    scope_type: ScopeType = "TENANT"
    scope_id: Optional[int] = Field(None, ge=1)
    value: Any = None
    effective_from: Optional[date] = None
    locked: Optional[bool] = None
    reason: Optional[str] = Field(None, max_length=500)
    # Concurrency token from GET values (0 or omitted when nothing is saved at that scope yet).
    row_version: Optional[int] = Field(None, ge=0)


class ValuesIn(StrictModel):
    changes: List[ChangeIn] = Field(min_length=1, max_length=engine.MAX_BATCH)


class DecisionIn(StrictModel):
    comment: Optional[str] = Field(None, max_length=500)


@router.get("/definitions")
def definitions(request: Request, module: Optional[str] = Query(None, max_length=40),
                section: Optional[str] = Query(None, max_length=80),
                _: Principal = Depends(require(engine.READ))):
    etag = f'"{reg.registry_fingerprint()}"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    payload = {
        "registry_version": reg.REGISTRY_VERSION,
        "sections": [{"slug": s.slug, "title": s.title, "spec": s.spec, "policy_types": list(s.policy_types)}
                     for s in reg.SECTIONS],
        "definitions": [reg.definition_json(d) for d in reg.definitions(module=module, section=section)],
        "policy_types": policy_types.policy_type_metadata(),
    }
    return JSONResponse(payload, headers=headers)


@router.get("/values")
def values(scope_type: ScopeType = "TENANT", scope_id: Optional[int] = Query(None, ge=1),
           as_of: Optional[date] = None, module: Optional[str] = Query(None, max_length=40),
           section: Optional[str] = Query(None, max_length=80),
           principal: Principal = Depends(require(engine.READ)), db: Session = Depends(get_db)):
    return engine.values_at_scope(db, principal, scope_type, scope_id, as_of, module=module, section=section)


@router.put("/values")
def save_values(data: ValuesIn, request: Request, principal: Principal = Depends(require(engine.WRITE)),
                db: Session = Depends(get_db)):
    changes = [engine.SettingChange(**item.model_dump()) for item in data.changes]
    return idempotency.execute(db, principal, request, lambda: (200, engine.save_values(db, principal, changes)),
                               body=data.model_dump(mode="json"))


@router.get("/resolve")
def resolve(key: str = Query(..., min_length=1, max_length=160), employee_id: Optional[int] = Query(None, ge=1),
            pay_group_id: Optional[int] = Query(None, ge=1), branch_id: Optional[int] = Query(None, ge=1),
            legal_entity_id: Optional[int] = Query(None, ge=1), on: Optional[date] = Query(None, alias="date"),
            principal: Principal = Depends(require(engine.READ)), db: Session = Depends(get_db)):
    try:
        reg.canonical_key(key)
    except reg.SettingValidationError:
        raise not_found("Setting") from None
    day = on or engine.company_today(engine._company(db, principal.company_id))
    for scope_type, scope_id in (("EMPLOYEE", employee_id), ("PAY_GROUP", pay_group_id), ("BRANCH", branch_id),
                                 ("LEGAL_ENTITY", legal_entity_id)):
        if scope_id is not None:
            engine.scope_target(db, principal, engine.READ, scope_type, scope_id, on=day)
    return engine.explain(db, principal.company_id, key, employee_id=employee_id, pay_group_id=pay_group_id,
                          branch_id=branch_id, legal_entity_id=legal_entity_id, on=day)


@router.get("/change-requests")
def change_requests(status: Literal["PENDING", "APPROVED", "REJECTED", "ALL"] = "PENDING",
                    principal: Principal = Depends(require_any(engine.READ, engine.APPROVE)),
                    db: Session = Depends(get_db)):
    return engine.list_change_requests(db, principal, status=status)


def _decide(request_id: int, approve: bool, data: Optional[DecisionIn], request: Request, principal: Principal,
            db: Session):
    comment = data.comment if data is not None else None
    return idempotency.execute(
        db, principal, request,
        lambda: (200, engine.decide_change_request(db, principal, request_id, approve=approve, comment=comment)),
        body=data.model_dump(mode="json") if data is not None else None,
    )


@router.post("/change-requests/{request_id}/approve")
def approve(request_id: int, request: Request, data: Optional[DecisionIn] = Body(None),
            principal: Principal = Depends(require(engine.APPROVE)), db: Session = Depends(get_db)):
    return _decide(request_id, True, data, request, principal, db)


@router.post("/change-requests/{request_id}/reject")
def reject(request_id: int, request: Request, data: Optional[DecisionIn] = Body(None),
           principal: Principal = Depends(require(engine.APPROVE)), db: Session = Depends(get_db)):
    return _decide(request_id, False, data, request, principal, db)

"""Shifts, versions and assignment rules (AS-M1; AS 11 §6.2; 03-shifts/01 §18).

Read ``shift:read`` (BUG-AS-35); catalogue writes ``shift:manage`` (``department:manage`` at COMPANY is kept as an
alias for one release); rules and bulk assign ``shift:assign``. The legacy contract stays on the same paths
(IG-AS-28): ``GET /shifts`` without list parameters returns the legacy array, ``POST /shifts`` with the legacy body
creates a FIXED / FLEXIBLE shift with version 1, ``PUT /shifts/{id}`` posts a version from the owner's local tomorrow.
Literal paths are declared before the integer ones.
"""

from datetime import date
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core import concurrency
from app.core.authz import ensure_employee_access, is_self
from app.core.errors import AppError, forbidden
from app.core.principal import Principal, get_principal, require, require_any
from app.database.db import get_db
from app.payroll.services import idempotency
from app.schemas.employees import ShiftWrite
from app.schemas.shifts import (
    ArchiveIn, BulkAssignIn, RuleIn, RulePatch, ShiftCreate, ShiftPatch, VersionIn, VoidIn,
)
from app.services import shift_catalogue as service
from app.services import shift_resolution

router = APIRouter(prefix="/api/v1", tags=["Shifts"])

READ = require("shift:read")
MANAGE = require_any("shift:manage", "department:manage")
ASSIGN = require("shift:assign")
RESOLVE_MAX_DAYS = 62
_LIST_PARAMS = ("q", "status", "kind", "branch_id", "legal_entity_id", "sort", "skip", "limit")


def _validation(error: ValidationError) -> AppError:
    issues = [{"field": ".".join(str(p) for p in e["loc"]), "code": e["type"].upper(), "message": e["msg"]}
              for e in error.errors()]
    return AppError(422, issues[0]["message"] if issues else "Invalid request.", "VALIDATION_ERROR",
                    extra={"issues": issues})


@router.get("/shifts")
def list_shifts(request: Request, q: Optional[str] = Query(None, max_length=100),
                status: Optional[str] = Query(None, max_length=10), kind: Optional[str] = Query(None, max_length=10),
                branch_id: Optional[int] = None, legal_entity_id: Optional[int] = None,
                sort: Optional[str] = Query(None, max_length=20), skip: int = Query(0, ge=0),
                limit: int = Query(50, ge=1, le=200), principal: Principal = Depends(READ),
                db: Session = Depends(get_db)):
    if not any(name in request.query_params for name in _LIST_PARAMS):
        return service.legacy_list(db, principal)
    return service.list_shifts(db, principal, q=q, status=status or "ACTIVE", kind=kind, branch_id=branch_id,
                               legal_entity_id=legal_entity_id, sort=sort, skip=skip, limit=limit)


@router.post("/shifts")
def create_shift(request: Request, body: Dict[str, Any] = Body(...), principal: Principal = Depends(MANAGE),
                 db: Session = Depends(get_db)):
    if "version" not in body:  # legacy body (settings screen, seed scripts)
        try:
            data = ShiftWrite.model_validate(body)
        except ValidationError as error:
            raise _validation(error)
        shift = service.legacy_create(db, principal, data)
        db.commit()
        return {"id": shift.id}
    try:
        data = ShiftCreate.model_validate(body)
    except ValidationError as error:
        raise _validation(error)
    return idempotency.execute(db, principal, request, lambda: (201, service.create_shift(db, principal, data)),
                               body=body)


# Literal path before /shifts/{shift_id:int}.
@router.get("/shifts/resolve")
def resolve(employee_id: int, from_: date = Query(..., alias="from"), to: date = Query(...),
            principal: Principal = Depends(READ), db: Session = Depends(get_db)):
    if to < from_:
        raise AppError(400, "The end date is before the start date.", "INVALID_RANGE")
    if (to - from_).days + 1 > RESOLVE_MAX_DAYS:
        raise AppError(400, f"Choose at most {RESOLVE_MAX_DAYS} days.", "RANGE_TOO_LONG")
    ensure_employee_access(db, principal, "shift:read", employee_id)
    days = shift_resolution.explain(db, principal.company_id, employee_id, from_, to)
    if is_self(principal, employee_id) and set(principal.scopes("shift:read")) == {"SELF"}:
        for day in days:  # an employee sees where the shift came from, not other people's rules (§11.4)
            day["candidates"] = [{"step": c["step"], "won": c["won"]} for c in day["candidates"] if c["won"]]
    return {"employee_id": employee_id, "days": days}


@router.get("/shifts/{shift_id:int}")
def get_shift(shift_id: int, principal: Principal = Depends(READ), db: Session = Depends(get_db)):
    return service.get_shift(db, principal, shift_id)


@router.patch("/shifts/{shift_id:int}")
def update_shift(shift_id: int, data: ShiftPatch, request: Request, principal: Principal = Depends(MANAGE),
                 db: Session = Depends(get_db)):
    result = service.update_shift(db, principal, shift_id, data, concurrency.if_match(request, data.row_version))
    db.commit()
    return result


@router.put("/shifts/{shift_id:int}")
def legacy_update_shift(shift_id: int, data: ShiftWrite, principal: Principal = Depends(MANAGE),
                        db: Session = Depends(get_db)):
    service.legacy_update(db, principal, shift_id, data)
    db.commit()
    return {"id": shift_id}


@router.post("/shifts/{shift_id:int}/archive")
def archive_shift(shift_id: int, request: Request, data: ArchiveIn = Body(default_factory=ArchiveIn),
                  principal: Principal = Depends(MANAGE), db: Session = Depends(get_db)):
    version = concurrency.if_match(request, data.row_version)
    return idempotency.execute(db, principal, request,
                               lambda: (200, service.set_status(db, principal, shift_id, True, version)),
                               body=data.model_dump(mode="json"))


@router.post("/shifts/{shift_id:int}/restore")
def restore_shift(shift_id: int, request: Request, data: ArchiveIn = Body(default_factory=ArchiveIn),
                  principal: Principal = Depends(MANAGE), db: Session = Depends(get_db)):
    version = concurrency.if_match(request, data.row_version)
    return idempotency.execute(db, principal, request,
                               lambda: (200, service.set_status(db, principal, shift_id, False, version)),
                               body=data.model_dump(mode="json"))


@router.get("/shifts/{shift_id:int}/versions")
def list_versions(shift_id: int, principal: Principal = Depends(READ), db: Session = Depends(get_db)):
    return service.list_versions(db, principal, shift_id)


@router.post("/shifts/{shift_id:int}/versions")
def create_version(shift_id: int, data: VersionIn, request: Request, principal: Principal = Depends(MANAGE),
                   db: Session = Depends(get_db)):
    if data.dry_run:
        result = service.create_version(db, principal, shift_id, data)
        db.rollback()
        return result
    return idempotency.execute(db, principal, request,
                               lambda: (201, service.create_version(db, principal, shift_id, data)),
                               body=data.model_dump(mode="json"))


@router.post("/shifts/{shift_id:int}/versions/{version_no:int}/void")
def void_version(shift_id: int, version_no: int, data: VoidIn, request: Request,
                 principal: Principal = Depends(MANAGE), db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request,
                               lambda: (200, service.void_version(db, principal, shift_id, version_no, data.reason)),
                               body=data.model_dump(mode="json"))


# ------------------------------------------------------------------------------------------ assignment rules
@router.get("/shift-assignment-rules")
def list_rules(shift_id: Optional[int] = None, status: Optional[str] = Query(None, max_length=10),
               skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
               principal: Principal = Depends(READ), db: Session = Depends(get_db)):
    if set(principal.scopes("shift:read")) <= {"SELF"} and not principal.is_company_admin:
        raise forbidden("Viewing shift rules needs the View shifts permission beyond your own records.")
    return service.list_rules(db, principal, shift_id=shift_id, status=status, skip=skip, limit=limit)


@router.post("/shift-assignment-rules/simulate")
def simulate_rule(data: RuleIn, principal: Principal = Depends(ASSIGN), db: Session = Depends(get_db)):
    result = service.simulate_rule(db, principal, data)
    db.rollback()
    return result


@router.post("/shift-assignment-rules/bulk-assign")
def bulk_assign(data: BulkAssignIn, request: Request, principal: Principal = Depends(ASSIGN),
                db: Session = Depends(get_db)):
    if data.dry_run:
        result = service.bulk_assign(db, principal, data)
        db.rollback()
        return result
    return idempotency.execute(db, principal, request, lambda: (200, service.bulk_assign(db, principal, data)),
                               body=data.model_dump(mode="json"))


@router.post("/shift-assignment-rules")
def create_rule(data: RuleIn, request: Request, principal: Principal = Depends(ASSIGN),
                db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (201, service.create_rule(db, principal, data)),
                               body=data.model_dump(mode="json"))


@router.get("/shift-assignment-rules/{rule_id:int}")
def get_rule(rule_id: int, principal: Principal = Depends(READ), db: Session = Depends(get_db)):
    if set(principal.scopes("shift:read")) <= {"SELF"} and not principal.is_company_admin:
        raise forbidden("Viewing shift rules needs the View shifts permission beyond your own records.")
    return service.get_rule(db, principal, rule_id)


@router.patch("/shift-assignment-rules/{rule_id:int}")
def update_rule(rule_id: int, data: RulePatch, request: Request, principal: Principal = Depends(ASSIGN),
                db: Session = Depends(get_db)):
    result = service.update_rule(db, principal, rule_id, data, concurrency.if_match(request, data.row_version))
    db.commit()
    return result

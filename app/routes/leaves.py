from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from app.core import authz
from app.core.principal import Principal, get_principal, require, require_any
from app.database.db import get_db
from app.models.employee import Employee
from app.payroll.services import idempotency
from app.schemas.leave import CancelIn, ChainWrite, LeaveApply, LeaveDecision, LeaveTypeWrite
from app.services import leave_service

router = APIRouter(prefix="/api/v1/leaves", tags=["Leave"])


def _serialize_type(t) -> dict:
    return {"id": t.id, "code": t.code, "name": t.name, "annual_quota": t.annual_quota, "is_paid": t.is_paid,
            "is_unlimited": t.is_unlimited, "is_active": t.is_active, "kind": t.kind,
            "allow_half_day": bool(t.allow_half_day), "counts_non_working_days": bool(t.counts_non_working_days)}


@router.get("/types")
def list_types(include_inactive: bool = False, principal: Principal = Depends(get_principal),
               db: Session = Depends(get_db)):
    return [_serialize_type(t) for t in leave_service.leave_types(db, principal.company_id, include_inactive)]


@router.post("/types")
def create_type(data: LeaveTypeWrite, principal: Principal = Depends(require("leave:manage_policy")),
                db: Session = Depends(get_db)):
    lt = leave_service.save_leave_type(db, principal, data)
    db.commit()
    return _serialize_type(lt)


@router.put("/types/{type_id}")
def update_type(type_id: int, data: LeaveTypeWrite, principal: Principal = Depends(require("leave:manage_policy")),
                db: Session = Depends(get_db)):
    lt = leave_service.save_leave_type(db, principal, data, type_id)
    db.commit()
    return _serialize_type(lt)


@router.get("/approval-chain")
def get_chain(principal: Principal = Depends(require_any("leave:manage_policy", "leave:approve", "leave:apply")),
              db: Session = Depends(get_db)):
    return leave_service.get_chain(db, principal.company_id)


@router.put("/approval-chain")
def save_chain(data: ChainWrite, principal: Principal = Depends(require("leave:manage_policy")),
               db: Session = Depends(get_db)):
    result = leave_service.save_chain(db, principal, data.steps)
    db.commit()
    return result


def _leave_result(db: Session, leave) -> dict:
    # FastAPI's default encoding (decimals as numbers), exactly the shape these routes returned before.
    return jsonable_encoder(leave_service.serialize(db, leave, db.get(Employee, leave.employee_id)))


# The state-changing routes run through idempotency.execute (LH BUG-03): with an Idempotency-Key a retried or
# double-clicked request replays the first answer; without one they behave exactly as before.
@router.post("/apply")
def apply_leave(data: LeaveApply, request: Request, principal: Principal = Depends(require("leave:apply")),
                db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request,
                               lambda: (200, _leave_result(db, leave_service.apply(db, principal, data))),
                               body=data.model_dump(mode="json"))


@router.get("/my")
def my_leaves(principal: Principal = Depends(require("leave:apply")), db: Session = Depends(get_db)):
    return leave_service.my_leaves(db, principal)


@router.get("/my/balance")
def my_balance(year: Optional[int] = Query(None, ge=2000, le=2100), principal: Principal = Depends(get_principal),
               db: Session = Depends(get_db)):
    employee = authz.own_employee(db, principal)
    return leave_service.balances(db, principal.company_id, employee.id, year)


@router.get("/approvals")
def approval_inbox(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return leave_service.approval_inbox(db, principal)


@router.get("/upcoming")
def upcoming(principal: Principal = Depends(require("leave:read")), db: Session = Depends(get_db)):
    return leave_service.upcoming_approved(db, principal)


@router.get("/balance/{employee_id}")
def employee_balance(employee_id: int, year: Optional[int] = Query(None, ge=2000, le=2100),
                     principal: Principal = Depends(require("leave:read")), db: Session = Depends(get_db)):
    employee = authz.ensure_employee_access(db, principal, "leave:read", employee_id)
    return leave_service.balances(db, principal.company_id, employee.id, year)


@router.get("")
def list_leaves(
    status: Optional[str] = Query(None, pattern=r"^(pending|approved|rejected|cancelled)$"),
    employee_id: Optional[int] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(require("leave:read")),
    db: Session = Depends(get_db),
):
    total, items = leave_service.list_leaves(db, principal, status, employee_id, skip, limit)
    return {"total": total, "items": items, "skip": skip, "limit": limit}


@router.post("/{leave_id}/approve")
def approve(leave_id: int, request: Request, data: LeaveDecision = LeaveDecision(),
            principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request,
                               lambda: (200, _leave_result(db, leave_service.decide(db, principal, leave_id, True,
                                                                                    data.remarks))),
                               body=data.model_dump(mode="json"))


@router.post("/{leave_id}/reject")
def reject(leave_id: int, request: Request, data: LeaveDecision = LeaveDecision(),
           principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request,
                               lambda: (200, _leave_result(db, leave_service.decide(db, principal, leave_id, False,
                                                                                    data.remarks))),
                               body=data.model_dump(mode="json"))


@router.post("/{leave_id}/cancel")
def cancel(leave_id: int, request: Request, data: Optional[CancelIn] = None,
           principal: Principal = Depends(require("leave:apply")), db: Session = Depends(get_db)):
    reason = data.reason if data else None
    return idempotency.execute(db, principal, request,
                               lambda: (200, _leave_result(db, leave_service.cancel(db, principal, leave_id, reason))),
                               body={"reason": reason})

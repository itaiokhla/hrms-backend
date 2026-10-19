from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.principal import Principal, require, require_any
from app.database.db import get_db
from app.schemas.admin import (
    AssignedEmployeesUpdate,
    RoleCreate,
    RoleUpdate,
    UserInvite,
    UserRolesUpdate,
    UserStatusUpdate,
    UserUpdate,
)
from app.services import rbac_service, user_service

router = APIRouter(prefix="/api/v1", tags=["Users & Roles"])


@router.get("/users")
def list_users(
    q: Optional[str] = Query(None, max_length=100),
    status: Optional[str] = Query(None, pattern=r"^(invited|active|suspended|deactivated)$"),
    role_id: Optional[int] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(require("user:read")),
    db: Session = Depends(get_db),
):
    total, items = user_service.list_users(db, principal, q, status, role_id, skip, limit)
    return {"total": total, "items": items, "skip": skip, "limit": limit}


@router.get("/users/{user_id}")
def get_user(user_id: int, principal: Principal = Depends(require("user:read")), db: Session = Depends(get_db)):
    return user_service.get_user(db, principal, user_id)


@router.post("/users/invite")
def invite_user(data: UserInvite, principal: Principal = Depends(require("user:invite")), db: Session = Depends(get_db)):
    result = user_service.invite(db, principal, data)
    db.commit()
    return result


@router.post("/users/{user_id}/resend-invite")
def resend_invite(user_id: int, principal: Principal = Depends(require("user:invite")), db: Session = Depends(get_db)):
    result = user_service.resend_invitation(db, principal, user_id)
    db.commit()
    return result


@router.patch("/users/{user_id}")
def update_user(user_id: int, data: UserUpdate, principal: Principal = Depends(require("user:update")),
                db: Session = Depends(get_db)):
    user_service.update(db, principal, user_id, data)
    db.commit()
    return user_service.get_user(db, principal, user_id)


@router.put("/users/{user_id}/status")
def set_status(user_id: int, data: UserStatusUpdate, principal: Principal = Depends(require("user:deactivate")),
               db: Session = Depends(get_db)):
    user_service.set_status(db, principal, user_id, data.status)
    db.commit()
    return user_service.get_user(db, principal, user_id)


@router.post("/users/{user_id}/revoke-sessions")
def revoke_sessions(user_id: int, principal: Principal = Depends(require("user:deactivate")),
                    db: Session = Depends(get_db)):
    count = user_service.revoke_sessions(db, principal, user_id)
    db.commit()
    return {"revoked": count}


@router.put("/users/{user_id}/roles")
def set_roles(user_id: int, data: UserRolesUpdate, principal: Principal = Depends(require("user:assign_role")),
              db: Session = Depends(get_db)):
    user_service.set_roles(db, principal, user_id, data.role_ids)
    db.commit()
    return user_service.get_user(db, principal, user_id)


@router.put("/users/{user_id}/assigned-employees")
def set_assigned(user_id: int, data: AssignedEmployeesUpdate, principal: Principal = Depends(require("user:assign_role")),
                 db: Session = Depends(get_db)):
    user_service.set_assigned_employees(db, principal, user_id, data.employee_ids)
    db.commit()
    return user_service.get_user(db, principal, user_id)


@router.get("/roles")
def list_roles(principal: Principal = Depends(require_any("role:read", "user:invite", "user:assign_role")),
               db: Session = Depends(get_db)):
    return rbac_service.list_roles(db, principal)


@router.get("/permissions")
def permission_catalog(principal: Principal = Depends(require_any("role:read", "role:create", "role:update"))):
    return rbac_service.permission_catalog()


@router.post("/roles")
def create_role(data: RoleCreate, principal: Principal = Depends(require("role:create")), db: Session = Depends(get_db)):
    role = rbac_service.create_role(db, principal, data.name, data.description, [g.model_dump() for g in data.grants])
    db.commit()
    return rbac_service.serialize_role(role, 0, principal)


@router.patch("/roles/{role_id}")
def update_role(role_id: int, data: RoleUpdate, principal: Principal = Depends(require("role:update")),
                db: Session = Depends(get_db)):
    role = rbac_service.get_role(db, principal, role_id)
    grants = [g.model_dump() for g in data.grants] if data.grants is not None else None
    rbac_service.update_role(db, principal, role, data.name, data.description, grants)
    db.commit()
    return next(r for r in rbac_service.list_roles(db, principal) if r["id"] == role_id)


@router.delete("/roles/{role_id}")
def delete_role(role_id: int, principal: Principal = Depends(require("role:delete")), db: Session = Depends(get_db)):
    role = rbac_service.get_role(db, principal, role_id)
    rbac_service.delete_role(db, principal, role)
    db.commit()
    return {"ok": True}

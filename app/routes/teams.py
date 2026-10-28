"""Teams, memberships and ``/me/teams`` (X-M1; X 01 §14; spine §3.9).

Read ``team:read`` (or ``team:manage`` / ``team:membership:manage``, whose holders read the teams they change);
team writes ``team:manage``; membership writes ``team:membership:manage``. Visibility and the scope of every write
are decided in ``team_service``. Writes go through ``idempotency.execute`` (the only commit).
"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal, require, require_any
from app.database.db import get_db
from app.payroll.services import idempotency
from app.schemas.teams import MembershipIn, MembershipPatch, TeamArchiveIn, TeamIn, TeamPatch
from app.services import team_service as service

router = APIRouter(prefix="/api/v1", tags=["Teams"])

READ = require_any("team:read", "team:manage", "team:membership:manage")
MANAGE = require("team:manage")
MEMBERS = require("team:membership:manage")


@router.get("/teams")
def list_teams(q: Optional[str] = Query(None, max_length=100), team_type: Optional[str] = Query(None, max_length=12),
               status: Optional[str] = Query(None, max_length=10), legal_entity_id: Optional[int] = None,
               branch_id: Optional[int] = None, parent_team_id: Optional[int] = None,
               lead_employee_id: Optional[int] = None, member_employee_id: Optional[int] = None,
               has_lead: Optional[bool] = None, mine: bool = False,
               sort: str = Query("name", pattern=r"^(name|code|-member_count|-updated_at)$"),
               skip: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=200),
               principal: Principal = Depends(READ), db: Session = Depends(get_db)):
    return service.list_teams(db, principal, q=q, team_type=team_type, status=status, legal_entity_id=legal_entity_id,
                              branch_id=branch_id, parent_team_id=parent_team_id, lead_employee_id=lead_employee_id,
                              member_employee_id=member_employee_id, has_lead=has_lead, mine=mine, sort=sort,
                              skip=skip, limit=limit)


@router.post("/teams", status_code=201)
def create_team(data: TeamIn, request: Request, principal: Principal = Depends(MANAGE),
                db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (201, service.create_team(db, principal, data)),
                               body=data.model_dump(mode="json"))


@router.get("/teams/{team_id:int}")
def get_team(team_id: int, principal: Principal = Depends(READ), db: Session = Depends(get_db)):
    return service.get_team(db, principal, team_id)


@router.patch("/teams/{team_id:int}")
def update_team(team_id: int, data: TeamPatch, request: Request, principal: Principal = Depends(MANAGE),
                db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (200, service.update_team(db, principal, team_id, data)),
                               body=data.model_dump(mode="json"))


@router.post("/teams/{team_id:int}/archive")
def archive_team(team_id: int, data: TeamArchiveIn, request: Request, principal: Principal = Depends(MANAGE),
                 db: Session = Depends(get_db)):
    if data.dry_run:
        result = service.archive_team(db, principal, team_id, data)
        db.rollback()
        return result
    return idempotency.execute(db, principal, request,
                               lambda: (200, service.archive_team(db, principal, team_id, data)),
                               body=data.model_dump(mode="json"))


@router.get("/teams/{team_id:int}/members")
def list_members(team_id: int, q: Optional[str] = Query(None, max_length=100),
                 member_role: Optional[str] = Query(None, max_length=12),
                 state: Optional[str] = Query(None, max_length=40), as_of: Optional[date] = None,
                 sort: str = Query("name", pattern=r"^(name|effective_from)$"), skip: int = Query(0, ge=0),
                 limit: int = Query(50, ge=1, le=500), principal: Principal = Depends(READ),
                 db: Session = Depends(get_db)):
    return service.list_members(db, principal, team_id, q=q, member_role=member_role, state=state, as_of=as_of,
                                sort=sort, skip=skip, limit=limit)


@router.get("/teams/{team_id:int}/member-candidates")
def member_candidates(team_id: int, q: str = Query(..., min_length=2, max_length=100),
                      effective_from: Optional[date] = None, principal: Principal = Depends(MEMBERS),
                      db: Session = Depends(get_db)):
    return {"items": service.member_candidates(db, principal, team_id, q, effective_from)}


@router.post("/teams/{team_id:int}/members", status_code=201)
def add_member(team_id: int, data: MembershipIn, request: Request, principal: Principal = Depends(MEMBERS),
               db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (201, service.add_member(db, principal, team_id, data)),
                               body=data.model_dump(mode="json"))


@router.patch("/team-memberships/{membership_id:int}")
def update_membership(membership_id: int, data: MembershipPatch, request: Request,
                      principal: Principal = Depends(MEMBERS), db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request,
                               lambda: (200, service.update_membership(db, principal, membership_id, data)),
                               body=data.model_dump(mode="json"))


@router.get("/me/teams")
def my_teams(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.team_facts(db, principal, counts=True)

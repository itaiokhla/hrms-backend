from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core import authz
from app.core.principal import Principal, require
from app.database.db import get_db
from app.schemas.employees import BulkAssignReports, DelegationCreate, ManagerAssignment, SecondaryLineCreate
from app.services import employee_service, org_service

router = APIRouter(prefix="/api/v1/org", tags=["Organization"])


@router.get("/nodes")
def org_nodes(
    parent_id: Optional[int] = None,
    q: Optional[str] = Query(None, max_length=100),
    principal: Principal = Depends(require("employee:read")),
    db: Session = Depends(get_db),
):
    return org_service.org_nodes(db, principal, parent_id, q)


@router.get("/hierarchy")
def full_hierarchy(
    department_id: Optional[int] = None,
    team_id: Optional[int] = None,
    q: Optional[str] = Query(None, max_length=100),
    principal: Principal = Depends(require("employee:read")),
    db: Session = Depends(get_db),
):
    return org_service.full_hierarchy(db, principal, department_id, team_id, q)


@router.post("/assign-reports")
def bulk_assign_reports(
    data: BulkAssignReports,
    principal: Principal = Depends(require("org:manage_hierarchy")),
    db: Session = Depends(get_db),
):
    return org_service.bulk_assign_reports(db, principal, data.manager_id, data.employee_ids)


@router.delete("/employees/{employee_id}/manager")
def remove_manager(
    employee_id: int,
    principal: Principal = Depends(require("org:manage_hierarchy")),
    db: Session = Depends(get_db),
):
    employee = authz.ensure_employee_access(db, principal, "org:manage_hierarchy", employee_id)
    org_service.assign_manager(db, principal, employee, None)
    db.commit()
    return {"ok": True, "employee_id": employee_id}


@router.get("/employees/{employee_id}/chain")
def management_chain(employee_id: int, principal: Principal = Depends(require("employee:read")),
                     db: Session = Depends(get_db)):
    return org_service.management_chain(db, principal, employee_id)


@router.get("/employees/{employee_id}/history")
def manager_history(employee_id: int, principal: Principal = Depends(require("employee:read")),
                    db: Session = Depends(get_db)):
    return org_service.manager_history(db, principal, employee_id)


@router.put("/employees/{employee_id}/manager")
def set_manager(employee_id: int, data: ManagerAssignment,
                principal: Principal = Depends(require("org:manage_hierarchy")), db: Session = Depends(get_db)):
    employee = authz.ensure_employee_access(db, principal, "org:manage_hierarchy", employee_id)
    org_service.assign_manager(db, principal, employee, data.manager_id)
    db.commit()
    return employee_service.get_employee(db, principal, employee_id)


@router.get("/secondary-lines")
def list_secondary(employee_id: Optional[int] = None, principal: Principal = Depends(require("employee:read")),
                   db: Session = Depends(get_db)):
    return org_service.list_secondary_lines(db, principal, employee_id)


@router.post("/secondary-lines")
def add_secondary(data: SecondaryLineCreate, principal: Principal = Depends(require("org:manage_hierarchy")),
                  db: Session = Depends(get_db)):
    line = org_service.add_secondary_line(db, principal, data)
    db.commit()
    return {"id": line.id}


@router.delete("/secondary-lines/{line_id}")
def end_secondary(line_id: int, principal: Principal = Depends(require("org:manage_hierarchy")),
                  db: Session = Depends(get_db)):
    org_service.end_secondary_line(db, principal, line_id)
    db.commit()
    return {"ok": True}


@router.get("/delegation-candidates")
def delegation_candidates(q: Optional[str] = Query(None, max_length=100),
                          principal: Principal = Depends(require("delegation:manage")), db: Session = Depends(get_db)):
    return org_service.delegation_candidates(db, principal, q)


@router.get("/delegations")
def list_delegations(principal: Principal = Depends(require("delegation:manage")), db: Session = Depends(get_db)):
    return org_service.list_delegations(db, principal)


@router.post("/delegations")
def create_delegation(data: DelegationCreate, principal: Principal = Depends(require("delegation:manage")),
                      db: Session = Depends(get_db)):
    delegation = org_service.create_delegation(db, principal, data)
    db.commit()
    return {"id": delegation.id}


@router.delete("/delegations/{delegation_id}")
def revoke_delegation(delegation_id: int, principal: Principal = Depends(require("delegation:manage")),
                      db: Session = Depends(get_db)):
    org_service.revoke_delegation(db, principal, delegation_id)
    db.commit()
    return {"ok": True}

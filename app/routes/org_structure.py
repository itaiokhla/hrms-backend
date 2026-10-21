"""Organisation structure API: legal entities, branches, placement history, termination and scope targets."""

from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, File, Form, Query, Request, UploadFile
from pydantic import Field
from sqlalchemy.orm import Session

from app.core import authz
from app.core.errors import AppError, bad_request, forbidden, not_found
from app.core.permission_registry import PERMISSIONS_BY_CODE, TARGETED_SCOPES
from app.core.principal import Principal, get_principal_any_subscription, require, require_any
from app.database.db import get_db
from app.models.org import Branch, LegalEntity, RoleAssignmentTarget
from app.models.pay_org import PayGroup
from app.models.rbac import Role, RolePermission, UserRoleAssignment
from app.models.user import User
from app.payroll.api_types import DecimalStr
from app.payroll.services import idempotency
from app.schemas.auth import StrictModel
from app.services import brand_asset_service, brand_service, employee_service, org_structure_service
from app.services.audit_log_service import AuditLogService

router = APIRouter(prefix="/api/v1", tags=["Organization structure"])


@router.get("/org/catalog")
def catalog(principal: Principal = Depends(get_principal_any_subscription)):
    return org_structure_service.catalog()


@router.get("/org/subdivisions")
def subdivisions(country: str = Query(..., min_length=2, max_length=2),
                 principal: Principal = Depends(get_principal_any_subscription)):
    return org_structure_service.subdivisions(country)


@router.get("/org/tree")
def org_tree(principal: Principal = Depends(require("org:read")), db: Session = Depends(get_db)):
    return org_structure_service.tree(db, principal.company_id)


# ---------------------------------------------------------------------------
# Legal entities
# ---------------------------------------------------------------------------
class EntityWrite(StrictModel):
    code: Optional[str] = Field(None, max_length=20)
    legal_name: Optional[str] = Field(None, max_length=200)
    trade_name: Optional[str] = Field(None, max_length=200)
    country_code: Optional[str] = Field(None, max_length=2)
    base_currency: Optional[str] = Field(None, max_length=3)
    timezone: Optional[str] = Field(None, max_length=64)
    fiscal_year_start: Optional[str] = Field(None, max_length=5)
    default_locale: Optional[str] = Field(None, max_length=35)
    signatory_name: Optional[str] = Field(None, max_length=150)
    signatory_title: Optional[str] = Field(None, max_length=150)
    registered_address: Optional[Dict[str, Any]] = None
    registration_lines: Optional[List[Dict[str, Any]]] = Field(None, max_length=8)
    confirm_country: Optional[bool] = None


@router.get("/legal-entities")
def list_entities(principal: Principal = Depends(require_any("org:read", "org:manage_entities")),
                  db: Session = Depends(get_db)):
    return org_structure_service.list_entities(db, principal.company_id)


@router.post("/legal-entities")
def create_entity(data: EntityWrite, principal: Principal = Depends(require("org:manage_entities")),
                  db: Session = Depends(get_db)):
    entity = org_structure_service.save_entity(db, principal, data.model_dump(exclude_unset=True))
    db.commit()
    return org_structure_service.serialize_entity(entity)


@router.get("/legal-entities/{entity_id}")
def get_entity(entity_id: int, principal: Principal = Depends(require_any("org:read", "org:manage_entities")),
               db: Session = Depends(get_db)):
    entity = org_structure_service._entity(db, principal.company_id, entity_id)
    counts = org_structure_service._current_counts(db, principal.company_id,
                                                   org_structure_service.EmployeeAssignment.legal_entity_id)
    data = org_structure_service.serialize_entity(entity, counts)
    data["branches"] = org_structure_service.list_branches(db, principal.company_id, entity.id)
    data["has_finalized_payroll"] = org_structure_service.has_finalized_payroll(db, principal.company_id, entity.id)
    return data


@router.patch("/legal-entities/{entity_id}")
def update_entity(entity_id: int, data: EntityWrite, principal: Principal = Depends(require("org:manage_entities")),
                  db: Session = Depends(get_db)):
    entity = org_structure_service.save_entity(db, principal, data.model_dump(exclude_unset=True), entity_id)
    db.commit()
    brand_service.invalidate_cache(principal.company_id)
    return org_structure_service.serialize_entity(entity)


@router.post("/legal-entities/{entity_id}/{action}")
def entity_status(entity_id: int, action: str, principal: Principal = Depends(require("org:manage_entities")),
                  db: Session = Depends(get_db)):
    if action not in ("activate", "deactivate"):
        raise not_found()
    entity = org_structure_service.set_entity_status(db, principal, entity_id, action == "activate")
    db.commit()
    return org_structure_service.serialize_entity(entity)


# -- document branding of one legal entity (Enterprise; 01 §5.7) ----------------------------
class EntityBrandWrite(StrictModel):
    changes: Dict[str, Any] = Field(default_factory=dict)
    row_version: Optional[int] = None


def _entity_owner(db: Session, principal: Principal, entity_id: int):
    org_structure_service._entity(db, principal.company_id, entity_id)
    return brand_service.entity_owner(principal.company_id, entity_id)


def _entity_brand_view(db: Session, principal: Principal, entity_id: int) -> dict:
    owner = _entity_owner(db, principal, entity_id)
    from app.models.branding import BrandProfile

    draft = owner.profiles(db).filter(BrandProfile.status == "DRAFT").first()
    published = owner.profiles(db).filter(BrandProfile.status == "PUBLISHED").first()
    chain = brand_service.load_chain(principal.company_id, use_cache=False)
    return {
        "entitled": "documents.entity_branding" in chain.entitlements,
        "fields": list(brand_service.ENTITY_FIELDS),
        "draft": brand_service.serialize_admin_profile(db, owner, draft),
        "published": brand_service.serialize_admin_profile(db, owner, published),
        "history": brand_service.history(db, owner, 20),
        "effective": brand_service.document_brand(principal.company_id, entity_id),
    }


@router.get("/legal-entities/{entity_id}/document-brand")
def get_entity_brand(entity_id: int, principal: Principal = Depends(require("org:manage_entities")),
                     db: Session = Depends(get_db)):
    return _entity_brand_view(db, principal, entity_id)


@router.put("/legal-entities/{entity_id}/document-brand")
def save_entity_brand(entity_id: int, data: EntityBrandWrite, principal: Principal = Depends(require("org:manage_entities")),
                      db: Session = Depends(get_db)):
    owner = _entity_owner(db, principal, entity_id)
    chain = brand_service.load_chain(principal.company_id, use_cache=False)
    if "documents.entity_branding" not in chain.entitlements:
        raise AppError(403, "Document branding per legal entity is not included in your plan.", "BRAND_NOT_ENTITLED")
    brand_service.save_draft(db, owner, data.changes, actor=principal.email, actor_user_id=principal.user_id,
                             expected_row_version=data.row_version, chain=chain)
    db.commit()
    return _entity_brand_view(db, principal, entity_id)


@router.post("/legal-entities/{entity_id}/document-brand/assets")
async def upload_entity_logo(entity_id: int, file: UploadFile = File(...), kind: str = Form("document_logo"),
                             principal: Principal = Depends(require("org:manage_entities")), db: Session = Depends(get_db)):
    if kind != "document_logo":
        raise bad_request("Only a document logo can be uploaded for a legal entity.")
    _entity_owner(db, principal, entity_id)
    rule = brand_asset_service.KIND_RULES[kind]
    data = await file.read(rule.max_bytes + 1)
    asset = brand_service.add_asset(db, brand_service.tenant_owner(principal.company_id), kind, data, file.filename,
                                    principal.email)
    db.commit()
    payload = brand_service._asset_dict(asset)
    payload["url"] = brand_service.asset_url(payload, "document") or brand_service.asset_url(payload)
    return payload


@router.post("/legal-entities/{entity_id}/document-brand/publish")
def publish_entity_brand(entity_id: int, data: EntityBrandWrite = Body(EntityBrandWrite()),
                         principal: Principal = Depends(require("org:manage_entities")), db: Session = Depends(get_db)):
    owner = _entity_owner(db, principal, entity_id)
    chain = brand_service.load_chain(principal.company_id, use_cache=False)
    if "documents.entity_branding" not in chain.entitlements:
        raise AppError(403, "Document branding per legal entity is not included in your plan.", "BRAND_NOT_ENTITLED")
    profile, created = brand_service.publish(db, owner, actor=principal.email, actor_user_id=principal.user_id,
                                             expected_row_version=data.row_version, chain=chain)
    if created:
        AuditLogService.log(db, principal, "Branding", "ENTITY_PUBLISHED",
                            f"Published document branding v{profile.version} for legal entity #{entity_id}",
                            "brand_profile", profile.id)
    db.commit()
    return _entity_brand_view(db, principal, entity_id)


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------
class BranchWrite(StrictModel):
    legal_entity_id: Optional[int] = None
    code: Optional[str] = Field(None, max_length=20)
    name: Optional[str] = Field(None, max_length=100)
    subdivision_code: Optional[str] = Field(None, max_length=6)
    timezone: Optional[str] = Field(None, max_length=64)
    address: Optional[Dict[str, Any]] = None


@router.get("/branches")
def list_branches(legal_entity_id: Optional[int] = None,
                  principal: Principal = Depends(require_any("org:read", "org:manage_branches", "employee:create")),
                  db: Session = Depends(get_db)):
    return org_structure_service.list_branches(db, principal.company_id, legal_entity_id)


@router.post("/branches")
def create_branch(data: BranchWrite, principal: Principal = Depends(require("org:manage_branches")),
                  db: Session = Depends(get_db)):
    branch = org_structure_service.save_branch(db, principal, data.model_dump(exclude_unset=True))
    db.commit()
    return org_structure_service.serialize_branch(branch, db.get(LegalEntity, branch.legal_entity_id))


@router.patch("/branches/{branch_id}")
def update_branch(branch_id: int, data: BranchWrite, principal: Principal = Depends(require("org:manage_branches")),
                  db: Session = Depends(get_db)):
    branch = org_structure_service.save_branch(db, principal, data.model_dump(exclude_unset=True), branch_id)
    db.commit()
    return org_structure_service.serialize_branch(branch, db.get(LegalEntity, branch.legal_entity_id))


@router.post("/branches/{branch_id}/{action}")
def branch_status(branch_id: int, action: str, principal: Principal = Depends(require("org:manage_branches")),
                  db: Session = Depends(get_db)):
    if action not in ("activate", "deactivate"):
        raise not_found()
    branch = org_structure_service.set_branch_status(db, principal, branch_id, action == "activate")
    db.commit()
    return org_structure_service.serialize_branch(branch, db.get(LegalEntity, branch.legal_entity_id))


# ---------------------------------------------------------------------------
# Placement (effective-dated assignments)
# ---------------------------------------------------------------------------
class PlacementChange(StrictModel):
    """A new placement from `effective_from`. Payroll dimensions left out continue from the current
    placement (or take their defaults); null clears an optional one or picks the default of a required one."""

    legal_entity_id: Optional[int] = None
    branch_id: int
    department_id: Optional[int] = None
    effective_from: str = Field(..., max_length=10)
    reason: str = Field("TRANSFER", max_length=20)
    note: Optional[str] = Field(None, max_length=500)
    is_primary: Optional[bool] = None
    pay_group_id: Optional[int] = None
    grade_id: Optional[int] = None
    employment_type_id: Optional[int] = None
    designation_id: Optional[int] = None
    cost_center_id: Optional[int] = None
    work_week_pattern_id: Optional[int] = None
    shift_id: Optional[int] = None
    fte: Optional[DecimalStr] = None


class TerminationWrite(StrictModel):
    last_working_day: date
    note: Optional[str] = Field(None, max_length=500)


@router.get("/employees/{employee_id}/assignments")
def assignments(employee_id: int, principal: Principal = Depends(require("employee:read")), db: Session = Depends(get_db)):
    authz.ensure_employee_access(db, principal, "employee:read", employee_id)
    return org_structure_service.history(db, principal.company_id, employee_id)


@router.post("/employees/{employee_id}/assignments")
def change_placement(employee_id: int, data: PlacementChange, principal: Principal = Depends(require("employee:update")),
                     db: Session = Depends(get_db)):
    employee = authz.ensure_employee_access(db, principal, "employee:update", employee_id)
    payload = data.model_dump(exclude_unset=True)
    if "department_id" not in payload:
        payload["department_id"] = employee.department_id
    org_structure_service.change_placement(db, principal, employee, payload)
    db.commit()
    return org_structure_service.history(db, principal.company_id, employee_id)


@router.post("/employees/{employee_id}/termination")
def terminate(employee_id: int, data: TerminationWrite, request: Request,
              principal: Principal = Depends(require("employee:deactivate")), db: Session = Depends(get_db)):
    """End employment on a chosen last working day (not the click date); every placement ends on it."""
    def handler():
        employee = employee_service.deactivate_employee(db, principal, employee_id, data.last_working_day, data.note)
        return 200, {"employee_id": employee.id, "is_active": employee.is_active,
                     "termination_date": employee.termination_date.isoformat() if employee.termination_date else None}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Scope targets of one user's role assignments (LEGAL_ENTITY / BRANCH scopes)
# ---------------------------------------------------------------------------
class TargetWrite(StrictModel):
    role_id: int
    legal_entity_ids: List[int] = Field(default_factory=list, max_length=200)
    branch_ids: List[int] = Field(default_factory=list, max_length=500)
    pay_group_ids: List[int] = Field(default_factory=list, max_length=500)


class TargetsWrite(StrictModel):
    assignments: List[TargetWrite] = Field(default_factory=list, max_length=50)


def _targeted_grants(db: Session, role_id: int) -> Dict[str, List[str]]:
    rows = db.query(RolePermission.permission_code, RolePermission.scope).filter(
        RolePermission.role_id == role_id, RolePermission.scope.in_([s.value for s in TARGETED_SCOPES])
    ).all()
    result: Dict[str, List[str]] = {}
    for code, scope in rows:
        result.setdefault(scope, []).append(code)
    return result


def _targets_view(db: Session, principal: Principal, user_id: int) -> List[dict]:
    assignments = db.query(UserRoleAssignment).filter(
        UserRoleAssignment.company_id == principal.company_id, UserRoleAssignment.user_id == user_id
    ).all()
    roles = {r.id: r for r in db.query(Role).filter(Role.company_id == principal.company_id)}
    result = []
    for assignment in assignments:
        grants = _targeted_grants(db, assignment.role_id)
        if not grants:
            continue
        targets = db.query(RoleAssignmentTarget).filter(RoleAssignmentTarget.user_role_assignment_id == assignment.id).all()
        result.append({
            "role_id": assignment.role_id,
            "role_name": roles[assignment.role_id].name if assignment.role_id in roles else None,
            "scopes": sorted(grants),
            "permissions": grants,
            "legal_entity_ids": sorted(t.target_id for t in targets if t.scope == "LEGAL_ENTITY"),
            "branch_ids": sorted(t.target_id for t in targets if t.scope == "BRANCH"),
            "pay_group_ids": sorted(t.target_id for t in targets if t.scope == "PAY_GROUP"),
        })
    return result


@router.get("/users/{user_id}/scope-targets")
def get_targets(user_id: int, principal: Principal = Depends(require("user:read")), db: Session = Depends(get_db)):
    if db.query(User.id).filter(User.company_id == principal.company_id, User.id == user_id).first() is None:
        raise not_found("User")
    return _targets_view(db, principal, user_id)


@router.put("/users/{user_id}/scope-targets")
def set_targets(user_id: int, data: TargetsWrite, principal: Principal = Depends(require("user:assign_role")),
                db: Session = Depends(get_db)):
    user = db.query(User).filter(User.company_id == principal.company_id, User.id == user_id).first()
    if user is None:
        raise not_found("User")
    if user.id == principal.user_id:
        raise forbidden("You cannot change the reach of your own roles.")
    known = {
        "LEGAL_ENTITY": {i for (i,) in db.query(LegalEntity.id).filter(LegalEntity.company_id == principal.company_id)},
        "BRANCH": {i for (i,) in db.query(Branch.id).filter(Branch.company_id == principal.company_id)},
        "PAY_GROUP": {i for (i,) in db.query(PayGroup.id).filter(PayGroup.company_id == principal.company_id)},
    }
    for item in data.assignments:
        assignment = db.query(UserRoleAssignment).filter(
            UserRoleAssignment.company_id == principal.company_id, UserRoleAssignment.user_id == user.id,
            UserRoleAssignment.role_id == item.role_id,
        ).first()
        if assignment is None:
            raise not_found("Role assignment")
        grants = _targeted_grants(db, item.role_id)
        wanted = {"LEGAL_ENTITY": set(item.legal_entity_ids), "BRANCH": set(item.branch_ids),
                  "PAY_GROUP": set(item.pay_group_ids)}
        if any(not ids <= known[scope] for scope, ids in wanted.items()):
            raise not_found("Legal entity, branch or pay group")
        existing: Dict[str, set] = {}
        for target in db.query(RoleAssignmentTarget).filter(RoleAssignmentTarget.user_role_assignment_id == assignment.id):
            existing.setdefault(target.scope, set()).add(target.target_id)
        for scope, ids in wanted.items():
            if ids and scope not in grants:
                raise bad_request(f"This role has no {scope.replace('_', ' ').lower()}-scoped permissions.")
            if ids == existing.get(scope, set()):
                continue
            # Delegation: only someone with company-wide reach over every targeted permission may add or
            # remove targets (audit 01 D-13: a removal changes someone else's reach too).
            for code in grants.get(scope, []):
                if not (principal.is_company_admin or principal.has_company_scope(code)):
                    label = PERMISSIONS_BY_CODE[code].label if code in PERMISSIONS_BY_CODE else code
                    raise forbidden(f"You need company-wide '{label}' to change its targets.")
        db.query(RoleAssignmentTarget).filter(RoleAssignmentTarget.user_role_assignment_id == assignment.id).delete(
            synchronize_session=False
        )
        for scope, ids in wanted.items():
            for target_id in sorted(ids):
                db.add(RoleAssignmentTarget(company_id=principal.company_id, user_role_assignment_id=assignment.id,
                                            scope=scope, target_id=target_id, created_by_user_id=principal.user_id))
        AuditLogService.log(
            db, principal, "Users", "SCOPE_TARGETS",
            f"{user.email}: role #{item.role_id} covers entities {sorted(wanted['LEGAL_ENTITY'])}, branches "
            f"{sorted(wanted['BRANCH'])} and pay groups {sorted(wanted['PAY_GROUP'])}", "user", user.id,
            details={"role_id": item.role_id,
                     "before": {scope: sorted(existing.get(scope, set())) for scope in wanted},
                     "after": {scope: sorted(ids) for scope, ids in wanted.items()}},
        )
    user.permission_version += 1
    db.commit()
    return _targets_view(db, principal, user.id)

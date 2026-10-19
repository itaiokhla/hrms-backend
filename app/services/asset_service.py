"""Company asset lifecycle: inventory -> assignment -> acknowledgement -> transfer/maintenance -> return."""

from typing import List, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, aliased

from app.core import authz
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.core.permission_registry import Scope
from app.core.principal import Principal
from app.core.security import utcnow
from app.models.asset import Asset, AssetAssignment
from app.models.employee import Employee
from app.services.audit_log_service import AuditLogService
from app.services.notification_service import NotificationService


def _open_assignment(db: Session, asset_id: int) -> Optional[AssetAssignment]:
    return (
        db.query(AssetAssignment)
        .filter(AssetAssignment.asset_id == asset_id, AssetAssignment.returned_at.is_(None))
        .first()
    )


def serialize(asset: Asset, assignment: Optional[AssetAssignment], holder: Optional[Employee]) -> dict:
    return {
        "id": asset.id,
        "asset_tag": asset.asset_tag,
        "name": asset.name,
        "category": asset.category,
        "serial_number": asset.serial_number,
        "status": asset.status,
        "purchase_date": asset.purchase_date.isoformat() if asset.purchase_date else None,
        "purchase_cost": float(asset.purchase_cost) if asset.purchase_cost is not None else None,
        "notes": asset.notes,
        "assignment": (
            {
                "id": assignment.id,
                "employee_id": assignment.employee_id,
                "employee_name": holder.full_name if holder else None,
                "assigned_at": assignment.assigned_at.isoformat(),
                "acknowledged_at": assignment.acknowledged_at.isoformat() if assignment.acknowledged_at else None,
            }
            if assignment
            else None
        ),
    }


def list_assets(db: Session, principal: Principal, q: Optional[str], status: Optional[str],
                employee_id: Optional[int], skip: int, limit: int):
    if not principal.has("asset:read"):
        raise forbidden()
    holder = aliased(Employee)
    open_assignment = aliased(AssetAssignment)
    query = (
        db.query(Asset, open_assignment, holder)
        .outerjoin(open_assignment, (open_assignment.asset_id == Asset.id) & open_assignment.returned_at.is_(None))
        .outerjoin(holder, holder.id == open_assignment.employee_id)
        .filter(Asset.company_id == principal.company_id)
    )
    if Scope.COMPANY.value not in principal.scopes("asset:read"):
        # Without inventory access you only see assets held by people in scope.
        scope = authz.employee_scope(db, principal, "asset:read")
        query = query.filter(open_assignment.id.is_not(None), scope.clause(open_assignment.employee_id))
    if q:
        term = f"%{q.strip()}%"
        query = query.filter(or_(Asset.name.ilike(term), Asset.asset_tag.ilike(term), Asset.serial_number.ilike(term)))
    if status:
        query = query.filter(Asset.status == status)
    if employee_id:
        query = query.filter(open_assignment.employee_id == employee_id)
    total = query.order_by(None).count()
    rows = query.order_by(Asset.asset_tag).offset(skip).limit(limit).all()
    return total, [serialize(a, asg, h) for a, asg, h in rows]


def _get(db: Session, principal: Principal, asset_id: int) -> Asset:
    asset = db.query(Asset).filter(Asset.company_id == principal.company_id, Asset.id == asset_id).first()
    if asset is None:
        raise not_found("Asset")
    return asset


def get_asset(db: Session, principal: Principal, asset_id: int) -> dict:
    asset = _get(db, principal, asset_id)
    assignment = _open_assignment(db, asset.id)
    if Scope.COMPANY.value not in principal.scopes("asset:read"):
        if assignment is None or not authz.can_access_employee(db, principal, "asset:read", assignment.employee_id):
            raise not_found("Asset")
    holder = db.get(Employee, assignment.employee_id) if assignment else None
    data = serialize(asset, assignment, holder)
    history = (
        db.query(AssetAssignment, Employee)
        .join(Employee, Employee.id == AssetAssignment.employee_id)
        .filter(AssetAssignment.asset_id == asset.id)
        .order_by(AssetAssignment.assigned_at.desc())
        .limit(50)
        .all()
    )
    data["history"] = [
        {
            "employee_name": e.full_name,
            "assigned_at": a.assigned_at.isoformat(),
            "acknowledged_at": a.acknowledged_at.isoformat() if a.acknowledged_at else None,
            "returned_at": a.returned_at.isoformat() if a.returned_at else None,
            "return_condition": a.return_condition,
        }
        for a, e in history
    ]
    return data


def save_asset(db: Session, principal: Principal, data, asset_id: Optional[int] = None) -> Asset:
    clash = db.query(Asset.id).filter(Asset.company_id == principal.company_id, func.lower(Asset.asset_tag) == data.asset_tag.lower())
    if asset_id:
        clash = clash.filter(Asset.id != asset_id)
    if clash.first():
        raise conflict("Another asset already uses this tag.")
    if asset_id:
        asset = _get(db, principal, asset_id)
    else:
        asset = Asset(company_id=principal.company_id, status="in_stock")
        db.add(asset)
    for key, value in data.model_dump().items():
        setattr(asset, key, value)
    db.flush()
    AuditLogService.log(db, principal, "Assets", "SAVE", f"Saved asset {asset.asset_tag}", "asset", asset.id)
    return asset


def assign(db: Session, principal: Principal, asset_id: int, employee_id: int, notes: Optional[str]) -> Asset:
    asset = _get(db, principal, asset_id)
    if asset.status in ("retired", "maintenance"):
        raise bad_request(f"An asset in {asset.status} cannot be assigned.")
    employee = authz.ensure_employee_access(db, principal, "employee:read", employee_id)
    if not employee.is_active:
        raise bad_request("Assets can only be assigned to active employees.")
    current = _open_assignment(db, asset.id)
    now = utcnow()
    if current:
        if current.employee_id == employee.id:
            raise conflict("The asset is already assigned to this employee.")
        current.returned_at = now  # transfer: close the previous custody record
        current.received_by_user_id = principal.user_id
        current.return_condition = "transferred"
    db.add(
        AssetAssignment(
            company_id=principal.company_id,
            asset_id=asset.id,
            employee_id=employee.id,
            assigned_by_user_id=principal.user_id,
            assigned_at=now,
            notes=notes,
        )
    )
    asset.status = "assigned"
    db.flush()
    if employee.user_id:
        NotificationService.create(
            db, employee.user_id, "Asset assigned to you",
            f"{asset.name} ({asset.asset_tag}) was assigned to you. Please acknowledge receipt.", "asset", "/employee/assets",
        )
    AuditLogService.log(
        db, principal, "Assets", "TRANSFER" if current else "ASSIGN",
        f"{asset.asset_tag} assigned to {employee.full_name}", "asset", asset.id,
    )
    return asset


def acknowledge(db: Session, principal: Principal, asset_id: int) -> Asset:
    if not principal.has("asset:acknowledge"):
        raise forbidden()
    asset = _get(db, principal, asset_id)
    current = _open_assignment(db, asset.id)
    if current is None or current.employee_id != principal.employee_id:
        raise not_found("Asset")
    if current.acknowledged_at is None:
        current.acknowledged_at = utcnow()
        AuditLogService.log(db, principal, "Assets", "ACKNOWLEDGE", f"Acknowledged {asset.asset_tag}", "asset", asset.id)
    return asset


def recover(db: Session, principal: Principal, asset_id: int, condition: Optional[str], status: str) -> Asset:
    asset = _get(db, principal, asset_id)
    current = _open_assignment(db, asset.id)
    if current is None:
        raise bad_request("The asset is not currently assigned.")
    current.returned_at = utcnow()
    current.received_by_user_id = principal.user_id
    current.return_condition = condition
    asset.status = status
    AuditLogService.log(db, principal, "Assets", "RETURN", f"{asset.asset_tag} returned ({condition or 'no notes'})", "asset", asset.id)
    return asset


def set_status(db: Session, principal: Principal, asset_id: int, status: str) -> Asset:
    asset = _get(db, principal, asset_id)
    if _open_assignment(db, asset.id) is not None and status != "assigned":
        raise bad_request("Recover the asset from its holder first.")
    asset.status = status
    AuditLogService.log(db, principal, "Assets", "STATUS", f"{asset.asset_tag} marked {status}", "asset", asset.id)
    return asset


def my_assets(db: Session, principal: Principal) -> List[dict]:
    if not principal.has("asset:read"):
        raise forbidden()
    employee = authz.own_employee(db, principal)
    rows = (
        db.query(Asset, AssetAssignment)
        .join(AssetAssignment, AssetAssignment.asset_id == Asset.id)
        .filter(AssetAssignment.employee_id == employee.id, AssetAssignment.returned_at.is_(None))
        .order_by(AssetAssignment.assigned_at.desc())
        .all()
    )
    return [serialize(a, asg, employee) for a, asg in rows]

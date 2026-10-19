from typing import Optional

from sqlalchemy.orm import Session

from app.core.principal import Principal
from app.database.db import current_company_id
from app.models.audit_log import AuditLog


class AuditLogService:
    @staticmethod
    def log(
        db: Session,
        principal: Optional[Principal],
        module: str,
        action: str,
        description: str,
        entity_type: Optional[str] = None,
        entity_id=None,
        ip_address: Optional[str] = None,
        company_id: Optional[int] = None,
        actor_type: str = "user",
        actor_id: Optional[int] = None,
        actor_email: Optional[str] = None,
    ) -> AuditLog:
        """Queue an audit row in the caller's transaction (committed with the action)."""
        entry = AuditLog(
            company_id=company_id if company_id is not None else (
                principal.company_id if principal else current_company_id(db)
            ),
            actor_type=actor_type,
            actor_id=actor_id if actor_id is not None else (principal.user_id if principal else None),
            actor_email=actor_email if actor_email is not None else (principal.email if principal else None),
            module=module,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            description=description[:500],
            ip_address=ip_address,
        )
        db.add(entry)
        return entry

    @staticmethod
    def list(db: Session, principal: Principal, skip: int = 0, limit: int = 50, module: Optional[str] = None):
        query = db.query(AuditLog).filter(AuditLog.company_id == principal.company_id)
        if module:
            query = query.filter(AuditLog.module == module)
        total = query.count()
        rows = query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).offset(skip).limit(limit).all()
        return total, rows

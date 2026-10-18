from sqlalchemy.orm import Session
from app.models.audit_log import AuditLog


class AuditLogService:

    @staticmethod
    def create_log(
        db: Session,
        user_id=None,
        username=None,
        module="",
        action="",
        description="",
        ip_address=None,
    ):
        log = AuditLog(
            user_id=user_id,
            username=username,
            module=module,
            action=action,
            description=description,
            ip_address=ip_address,
        )

        db.add(log)
        db.commit()
        db.refresh(log)

        return log

    @staticmethod
    def get_logs(
        db: Session,
        skip=0,
        limit=50,
    ):
        return (
            db.query(AuditLog)
            .order_by(AuditLog.created_at.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )
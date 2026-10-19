from sqlalchemy import Column, ForeignKey, Index, Integer, String

from app.models.base import BaseModel


class AuditLog(BaseModel):
    """Append-only audit trail.

    company_id is NULL only for platform-level events. The application
    database role is granted INSERT/SELECT but never UPDATE/DELETE on this
    table, and tenant rows are protected by row-level security.
    """

    __tablename__ = "audit_logs"

    company_id = Column(Integer, ForeignKey("companies.id", ondelete="RESTRICT"), nullable=True, index=True)
    actor_type = Column(String(20), nullable=False, default="user")  # user | platform | system
    actor_id = Column(Integer, nullable=True)
    actor_email = Column(String(255), nullable=True)
    module = Column(String(50), nullable=False)
    action = Column(String(50), nullable=False)
    entity_type = Column(String(50), nullable=True)
    entity_id = Column(String(50), nullable=True)
    description = Column(String(500), nullable=False)
    ip_address = Column(String(64), nullable=True)

    __table_args__ = (Index("ix_audit_logs_company_created", "company_id", "created_at"),)

from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.sql import func
from app.database.db import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(Integer, nullable=True)

    username = Column(String(100))

    module = Column(String(100))

    action = Column(String(100))

    description = Column(String(500))

    ip_address = Column(String(50), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
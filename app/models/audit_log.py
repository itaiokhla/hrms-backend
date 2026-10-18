from sqlalchemy import Column, Integer, String
from app.models.base import BaseModel


class AuditLog(BaseModel):
    __tablename__ = "audit_logs"

    user_id = Column(Integer, nullable=True)
    username = Column(String(100))
    module = Column(String(100))
    action = Column(String(100))
    description = Column(String(500))
    ip_address = Column(String(50), nullable=True)  
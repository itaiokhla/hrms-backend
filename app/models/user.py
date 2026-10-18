from sqlalchemy import Column, String, Boolean, DateTime, Index, Enum
from sqlalchemy.orm import relationship
from app.models.base import BaseModel
from enum import Enum as PyEnum
from datetime import datetime


class UserRole(str, PyEnum):
    admin = "admin"
    hr = "hr"
    manager = "manager"
    employee = "employee"


class User(BaseModel):
    __tablename__ = "users"

    username = Column(String(50), unique=True, nullable=False, index=True)
    email = Column(String(100), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False, default="employee", index=True)
    is_active = Column(Boolean, default=True, nullable=False)
    last_login = Column(DateTime, nullable=True)

    __table_args__ = (
    Index("idx_user_username", "username"),
    Index("idx_user_email", "email"),
    Index("idx_user_role", "role"),
)
    notifications = relationship(
    "Notification",
    back_populates="user",
    cascade="all, delete-orphan",
)

    employee = relationship("Employee", back_populates="user", uselist=False, cascade="all, delete-orphan")
    leaves = relationship("Leave", back_populates="approved_by_user", foreign_keys="Leave.approved_by")
   
   

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "role": self.role,
            "is_active": self.is_active,
            "created_at": self.created_at,
        }

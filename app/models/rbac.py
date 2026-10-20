from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.core.permission_registry import SCOPE_VALUES
from app.models.base import BaseModel, TenantMixin


class Role(TenantMixin, BaseModel):
    """A company-defined role. Role definitions never cross tenants."""

    __tablename__ = "roles"

    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    # Stable identifier for roles seeded at company creation (e.g. "company_admin").
    system_key = Column(String(50), nullable=True)
    is_system_role = Column(Boolean, nullable=False, default=False)
    # Protected roles can only be granted or revoked by company administrators.
    is_protected = Column(Boolean, nullable=False, default=False)
    # Full-access roles implicitly hold every registered permission at COMPANY scope.
    is_full_access = Column(Boolean, nullable=False, default=False)

    permissions = relationship(
        "RolePermission",
        primaryjoin="Role.id == RolePermission.role_id",
        foreign_keys="RolePermission.role_id",
        back_populates="role",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_roles_company_id_id"),
        UniqueConstraint("company_id", "name", name="uq_roles_company_name"),
        UniqueConstraint("company_id", "system_key", name="uq_roles_company_system_key"),
    )


class RolePermission(TenantMixin, BaseModel):
    __tablename__ = "role_permissions"

    role_id = Column(Integer, nullable=False, index=True)
    permission_code = Column(String(64), nullable=False)
    scope = Column(String(20), nullable=False)

    role = relationship(
        "Role", primaryjoin="RolePermission.role_id == Role.id", foreign_keys=[role_id], back_populates="permissions"
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "role_id"],
            ["roles.company_id", "roles.id"],
            ondelete="CASCADE",
            name="fk_role_permissions_role",
        ),
        UniqueConstraint("role_id", "permission_code", "scope", name="uq_role_permission_scope"),
        CheckConstraint(f"scope IN {SCOPE_VALUES}", name="ck_role_permissions_scope"),
    )


class UserRoleAssignment(TenantMixin, BaseModel):
    __tablename__ = "user_role_assignments"

    user_id = Column(Integer, nullable=False, index=True)
    role_id = Column(Integer, nullable=False, index=True)
    assigned_by_user_id = Column(Integer, nullable=True)
    effective_from = Column(DateTime(timezone=True), nullable=False)
    effective_until = Column(DateTime(timezone=True), nullable=True)

    user = relationship(
        "User",
        primaryjoin="UserRoleAssignment.user_id == User.id",
        foreign_keys=[user_id],
        back_populates="role_assignments",
    )
    role = relationship("Role", primaryjoin="UserRoleAssignment.role_id == Role.id", foreign_keys=[role_id])

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "user_id"],
            ["users.company_id", "users.id"],
            ondelete="CASCADE",
            name="fk_user_role_assignments_user",
        ),
        ForeignKeyConstraint(
            ["company_id", "role_id"],
            ["roles.company_id", "roles.id"],
            ondelete="RESTRICT",
            name="fk_user_role_assignments_role",
        ),
        UniqueConstraint("user_id", "role_id", name="uq_user_role_assignment"),
        CheckConstraint(
            "effective_until IS NULL OR effective_until > effective_from",
            name="ck_user_role_assignment_window",
        ),
    )


class ScopeAssignment(TenantMixin, BaseModel):
    """Explicit employee delegation backing the ASSIGNED_GROUP scope."""

    __tablename__ = "scope_assignments"

    user_id = Column(Integer, nullable=False, index=True)
    employee_id = Column(Integer, nullable=False, index=True)
    created_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "user_id"],
            ["users.company_id", "users.id"],
            ondelete="CASCADE",
            name="fk_scope_assignments_user",
        ),
        ForeignKeyConstraint(
            ["company_id", "employee_id"],
            ["employees.company_id", "employees.id"],
            ondelete="CASCADE",
            name="fk_scope_assignments_employee",
        ),
        UniqueConstraint("user_id", "employee_id", name="uq_scope_assignment"),
    )

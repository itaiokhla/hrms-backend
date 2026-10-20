"""Organisational hierarchy inside one tenant (02-multi-level-company §9-§15).

Tenant -> legal entity (employer: country, currency, document identity) -> branch
(work location) -> department -> employee, with employee placement kept as an
effective-dated assignment history instead of being overwritten in place.
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import BaseModel, TenantMixin

ORG_STATUSES = ("ACTIVE", "INACTIVE")
ASSIGNMENT_REASONS = (
    "HIRE", "TRANSFER", "PROMOTION", "DEMOTION", "REORG", "SECONDMENT_START", "SECONDMENT_END",
    "CONCURRENT_START", "CONCURRENT_END", "TERMINATION", "REHIRE", "CORRECTION", "MIGRATION",
)
TARGET_SCOPES = ("LEGAL_ENTITY", "BRANCH")


class LegalEntity(TenantMixin, BaseModel):
    __tablename__ = "legal_entities"

    code = Column(String(20), nullable=False)
    legal_name = Column(String(200), nullable=False)
    trade_name = Column(String(200), nullable=True)
    country_code = Column(String(2), nullable=False)
    # Backfilled entities start unconfirmed (country guessed from currency/timezone).
    country_confirmed = Column(Boolean, nullable=False, default=True)
    # {"line1", "line2", "city", "subdivision_code", "postal_code", "country_code"}
    registered_address = Column(JSONB, nullable=False, default=dict)
    # Printed on documents, e.g. [{"label": "CIN", "value": "U72200KA2019PTC123456"}]
    registration_lines = Column(JSONB, nullable=False, default=list)
    base_currency = Column(String(3), nullable=False)
    fiscal_year_start = Column(String(5), nullable=False, default="01-01")
    timezone = Column(String(64), nullable=False)
    default_locale = Column(String(35), nullable=True)
    signatory_name = Column(String(150), nullable=True)
    signatory_title = Column(String(150), nullable=True)
    is_default = Column(Boolean, nullable=False, default=False)
    status = Column(String(10), nullable=False, default="ACTIVE")
    effective_from = Column(Date, nullable=True)
    effective_to = Column(Date, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_legal_entities_company_id_id"),
        UniqueConstraint("company_id", "code", name="uq_legal_entities_company_code"),
        CheckConstraint(f"status IN {ORG_STATUSES}", name="ck_legal_entities_status"),
        CheckConstraint("country_code ~ '^[A-Z]{2}$'", name="ck_legal_entities_country"),
        CheckConstraint("base_currency ~ '^[A-Z]{3}$'", name="ck_legal_entities_currency"),
        Index("uq_legal_entities_one_default", "company_id", unique=True, postgresql_where=text("is_default")),
    )


class Branch(TenantMixin, BaseModel):
    __tablename__ = "branches"

    legal_entity_id = Column(Integer, nullable=False, index=True)
    code = Column(String(20), nullable=False)
    name = Column(String(100), nullable=False)
    address = Column(JSONB, nullable=False, default=dict)
    country_code = Column(String(2), nullable=False)
    subdivision_code = Column(String(6), nullable=True)
    timezone = Column(String(64), nullable=False)
    status = Column(String(10), nullable=False, default="ACTIVE")
    effective_from = Column(Date, nullable=True)
    effective_to = Column(Date, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_branches_company_id_id"),
        UniqueConstraint("company_id", "legal_entity_id", "code", name="uq_branches_entity_code"),
        UniqueConstraint("company_id", "name", name="uq_branches_company_name"),
        ForeignKeyConstraint(
            ["company_id", "legal_entity_id"],
            ["legal_entities.company_id", "legal_entities.id"],
            name="fk_branches_legal_entity",
        ),
        CheckConstraint(f"status IN {ORG_STATUSES}", name="ck_branches_status"),
    )


class EmployeeAssignment(TenantMixin, BaseModel):
    """Where an employee is placed, from effective_from to effective_to (inclusive).

    Primary assignments of one employee never overlap (enforced by a trigger, as this
    PostgreSQL build has no btree_gist for an exclusion constraint).
    """

    __tablename__ = "employee_assignments"

    employee_id = Column(Integer, nullable=False, index=True)
    legal_entity_id = Column(Integer, nullable=False, index=True)
    branch_id = Column(Integer, nullable=False, index=True)
    department_id = Column(Integer, nullable=True)
    is_primary = Column(Boolean, nullable=False, default=True)
    reason = Column(String(20), nullable=False)
    note = Column(String(500), nullable=True)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    created_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "employee_id"], ["employees.company_id", "employees.id"],
            ondelete="CASCADE", name="fk_employee_assignments_employee",
        ),
        ForeignKeyConstraint(
            ["company_id", "legal_entity_id"], ["legal_entities.company_id", "legal_entities.id"],
            name="fk_employee_assignments_entity",
        ),
        ForeignKeyConstraint(
            ["company_id", "branch_id"], ["branches.company_id", "branches.id"],
            name="fk_employee_assignments_branch",
        ),
        ForeignKeyConstraint(
            ["company_id", "department_id"], ["departments.company_id", "departments.id"],
            name="fk_employee_assignments_department",
        ),
        CheckConstraint(f"reason IN {ASSIGNMENT_REASONS}", name="ck_employee_assignments_reason"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_employee_assignments_window"),
        Index("ix_employee_assignments_current", "company_id", "employee_id", "effective_from"),
    )


class RoleAssignmentTarget(TenantMixin, BaseModel):
    """Which legal entities or branches one role assignment covers (LEGAL_ENTITY / BRANCH scopes).

    A role granted at an entity scope with no targets grants nothing (deny by default).
    """

    __tablename__ = "user_role_assignment_targets"

    user_role_assignment_id = Column(Integer, nullable=False, index=True)
    scope = Column(String(20), nullable=False)
    target_id = Column(Integer, nullable=False)
    created_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "user_role_assignment_id"],
            ["user_role_assignments.company_id", "user_role_assignments.id"],
            ondelete="CASCADE", name="fk_role_assignment_targets_assignment",
        ),
        CheckConstraint(f"scope IN {TARGET_SCOPES}", name="ck_role_assignment_targets_scope"),
        UniqueConstraint("user_role_assignment_id", "scope", "target_id", name="uq_role_assignment_target"),
    )

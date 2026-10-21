"""Organisational hierarchy inside one tenant (02-multi-level-company §9-§15).

Tenant -> legal entity (employer: country, currency, document identity) -> branch
(work location) -> department -> employee, with employee placement kept as an
effective-dated assignment history instead of being overwritten in place. Payroll
dimensions (pay groups, grades, work weeks, holiday calendars...) are in app.models.pay_org.
"""

from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
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
TARGET_SCOPES = ("LEGAL_ENTITY", "BRANCH", "PAY_GROUP")


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
    # Weekly offs for branches without a pattern of their own (3.4 §3).
    default_work_week_pattern_id = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_legal_entities_company_id_id"),
        UniqueConstraint("company_id", "code", name="uq_legal_entities_company_code"),
        ForeignKeyConstraint(
            ["company_id", "default_work_week_pattern_id"], ["work_week_patterns.company_id", "work_week_patterns.id"],
            name="fk_legal_entities_work_week",
        ),
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
    # Holidays that apply here (with the calendar's ancestors) and weekly offs (3.4 §3, §8).
    holiday_calendar_id = Column(Integer, nullable=True)
    work_week_pattern_id = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_branches_company_id_id"),
        UniqueConstraint("company_id", "legal_entity_id", "code", name="uq_branches_entity_code"),
        UniqueConstraint("company_id", "name", name="uq_branches_company_name"),
        ForeignKeyConstraint(
            ["company_id", "legal_entity_id"],
            ["legal_entities.company_id", "legal_entities.id"],
            name="fk_branches_legal_entity",
        ),
        ForeignKeyConstraint(
            ["company_id", "holiday_calendar_id"], ["holiday_calendars.company_id", "holiday_calendars.id"],
            name="fk_branches_holiday_calendar",
        ),
        ForeignKeyConstraint(
            ["company_id", "work_week_pattern_id"], ["work_week_patterns.company_id", "work_week_patterns.id"],
            name="fk_branches_work_week",
        ),
        CheckConstraint(f"status IN {ORG_STATUSES}", name="ck_branches_status"),
    )


class EmployeeAssignment(TenantMixin, BaseModel):
    """Where an employee is placed, from effective_from to effective_to (inclusive).

    Primary assignments of one employee never overlap, the branch belongs to the legal entity
    and so does the pay group (enforced by a trigger, as this PostgreSQL build has no
    btree_gist for an exclusion constraint). The payroll dimensions are nullable while services
    do not set them yet; shift_id keeps shift history (employees.shift_id is only today's).
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
    pay_group_id = Column(Integer, nullable=True, index=True)
    grade_id = Column(Integer, nullable=True)
    employment_type_id = Column(Integer, nullable=True)
    designation_id = Column(Integer, nullable=True)
    cost_center_id = Column(Integer, nullable=True)
    work_week_pattern_id = Column(Integer, nullable=True)
    shift_id = Column(Integer, nullable=True)
    # Full-time equivalent, 0 < fte <= 1 (part-time 0.5 halves standard hours, 3.4 §3).
    fte = Column(Numeric(5, 4), nullable=False, default=Decimal("1"))

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
        ForeignKeyConstraint(
            ["company_id", "legal_entity_id", "pay_group_id"],
            ["pay_groups.company_id", "pay_groups.legal_entity_id", "pay_groups.id"],
            name="fk_employee_assignments_pay_group",
        ),
        ForeignKeyConstraint(
            ["company_id", "grade_id"], ["grades.company_id", "grades.id"], name="fk_employee_assignments_grade",
        ),
        ForeignKeyConstraint(
            ["company_id", "employment_type_id"], ["employment_types.company_id", "employment_types.id"],
            name="fk_employee_assignments_employment_type",
        ),
        ForeignKeyConstraint(
            ["company_id", "designation_id"], ["designations.company_id", "designations.id"],
            name="fk_employee_assignments_designation",
        ),
        ForeignKeyConstraint(
            ["company_id", "cost_center_id"], ["cost_centers.company_id", "cost_centers.id"],
            name="fk_employee_assignments_cost_center",
        ),
        ForeignKeyConstraint(
            ["company_id", "work_week_pattern_id"], ["work_week_patterns.company_id", "work_week_patterns.id"],
            name="fk_employee_assignments_work_week",
        ),
        ForeignKeyConstraint(
            ["company_id", "shift_id"], ["shifts.company_id", "shifts.id"], name="fk_employee_assignments_shift",
        ),
        CheckConstraint(f"reason IN {ASSIGNMENT_REASONS}", name="ck_employee_assignments_reason"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_employee_assignments_window"),
        CheckConstraint("fte > 0 AND fte <= 1", name="ck_employee_assignments_fte"),
        Index("ix_employee_assignments_current", "company_id", "employee_id", "effective_from"),
    )


class RoleAssignmentTarget(TenantMixin, BaseModel):
    """Which legal entities, branches or pay groups one role assignment covers
    (LEGAL_ENTITY / BRANCH / PAY_GROUP scopes).

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

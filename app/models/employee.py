from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import relationship

from app.models.base import BaseModel, TenantMixin
from app.models.leave import APPROVAL_MODULES

RELATION_TYPES = ("functional", "project", "dotted_line")


class Department(TenantMixin, BaseModel):
    __tablename__ = "departments"

    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    head_employee_id = Column(Integer, nullable=True)
    # Teams are child departments; cycles are rejected by a trigger and by the service.
    parent_department_id = Column(Integer, nullable=True, index=True)
    is_active = Column(Boolean, default=True, nullable=False)

    employees = relationship(
        "Employee",
        primaryjoin="Department.id == Employee.department_id",
        foreign_keys="Employee.department_id",
        back_populates="department",
    )
    head = relationship(
        "Employee",
        primaryjoin="Department.head_employee_id == Employee.id",
        foreign_keys=[head_employee_id],
        post_update=True,
    )

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_departments_company_id_id"),
        UniqueConstraint("company_id", "name", name="uq_departments_company_name"),
        ForeignKeyConstraint(
            ["company_id", "head_employee_id"],
            ["employees.company_id", "employees.id"],
            name="fk_departments_head_employee",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["company_id", "parent_department_id"],
            ["departments.company_id", "departments.id"],
            name="fk_departments_parent",
        ),
    )


class Shift(TenantMixin, BaseModel):
    """A catalogue shift (AS 11 §3.5; 03-shifts/01 §2.3). Times live in ``shift_versions`` (app/models/shifts.py);
    ``start_time`` … ``half_split_time`` and ``is_flexible`` mirror the version valid on the owner's local today
    (``current_version_id``) for legacy readers and are written only by ``shift_catalogue``. ``is_active`` mirrors
    ``status``. Owner scope: ``legal_entity_id`` / ``branch_id`` NULL = tenant-wide."""

    __tablename__ = "shifts"

    code = Column(String(20), nullable=False)
    name = Column(String(100), nullable=False)
    kind = Column(String(10), nullable=False, default="FIXED")
    color_token = Column(String(10), nullable=False, default="shift-1")
    legal_entity_id = Column(Integer, nullable=True)
    branch_id = Column(Integer, nullable=True)
    status = Column(String(10), nullable=False, default="ACTIVE")
    current_version_id = Column(Integer, nullable=True)
    start_time = Column(String(5), nullable=False)
    end_time = Column(String(5), nullable=False)
    # Decimal hours (8.5); gross of breaks unless unpaid_break_minutes says how much of it is unpaid.
    duration_hours = Column(Numeric(4, 2), nullable=True)
    unpaid_break_minutes = Column(SmallInteger, nullable=True)
    half_split_time = Column(String(5), nullable=True)
    is_flexible = Column(Boolean, default=False, nullable=False)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    row_version = Column(Integer, nullable=False, default=1)

    employees = relationship(
        "Employee",
        primaryjoin="Shift.id == Employee.shift_id",
        foreign_keys="Employee.shift_id",
        back_populates="shift",
    )

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_shifts_company_id_id"),
        UniqueConstraint("company_id", "name", name="uq_shifts_company_name"),
        UniqueConstraint("company_id", "code", name="uq_shifts_company_code"),
        ForeignKeyConstraint(["company_id", "legal_entity_id"], ["legal_entities.company_id", "legal_entities.id"],
                             name="fk_shifts_legal_entity"),
        ForeignKeyConstraint(["company_id", "branch_id"], ["branches.company_id", "branches.id"],
                             name="fk_shifts_branch"),
        ForeignKeyConstraint(["company_id", "current_version_id"], ["shift_versions.company_id", "shift_versions.id"],
                             name="fk_shifts_current_version", use_alter=True),
        CheckConstraint("duration_hours IS NULL OR (duration_hours > 0 AND duration_hours <= 24)",
                        name="ck_shifts_duration"),
        CheckConstraint("unpaid_break_minutes IS NULL OR unpaid_break_minutes BETWEEN 0 AND 1440",
                        name="ck_shifts_unpaid_break"),
        CheckConstraint("code ~ '^[A-Z0-9][A-Z0-9_-]{0,19}$'", name="ck_shifts_code"),
        CheckConstraint("kind IN ('FIXED', 'FLEXIBLE', 'OPEN', 'SPLIT')", name="ck_shifts_kind"),
        CheckConstraint("color_token ~ '^shift-([1-9]|1[0-2])$'", name="ck_shifts_color_token"),
        CheckConstraint("status IN ('ACTIVE', 'ARCHIVED')", name="ck_shifts_status"),
        CheckConstraint("is_active = (status = 'ACTIVE')", name="ck_shifts_is_active"),
        CheckConstraint("row_version >= 1", name="ck_shifts_row_version"),
    )


class Employee(TenantMixin, BaseModel):
    """An employment record. May exist without a login account."""

    __tablename__ = "employees"

    user_id = Column(Integer, nullable=True, unique=True)
    employee_code = Column(String(50), nullable=False)
    first_name = Column(String(50), nullable=False)
    last_name = Column(String(50), nullable=False)
    email = Column(String(255), nullable=False)
    phone = Column(String(20), nullable=True)
    date_of_birth = Column(Date, nullable=True)
    gender = Column(String(20), nullable=True)
    address = Column(Text, nullable=True)
    city = Column(String(50), nullable=True)
    state = Column(String(50), nullable=True)
    postal_code = Column(String(20), nullable=True)
    country = Column(String(50), nullable=True)
    location = Column(String(100), nullable=True)
    department_id = Column(Integer, nullable=True, index=True)
    shift_id = Column(Integer, nullable=True)
    position = Column(String(100), nullable=True)
    hire_date = Column(Date, nullable=False)
    termination_date = Column(Date, nullable=True)
    # Leave v2 lifecycle dates (SPINE §4.1, migration 0063): probation (NULL = no probation rule), early confirmation,
    # start of a resignation / termination notice, and service start for tenure (NULL = hire date).
    probation_end_date = Column(Date, nullable=True)
    confirmation_date = Column(Date, nullable=True)
    notice_start_date = Column(Date, nullable=True)
    service_start_date = Column(Date, nullable=True)
    base_salary = Column(Numeric(12, 2), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    photo_path = Column(String(255), nullable=True)
    # Primary reporting line: the source of truth for the organisation tree.
    manager_id = Column(Integer, nullable=True, index=True)

    # Relationships join on globally unique ids; the composite (company_id, id)
    # foreign keys below enforce same-company integrity in the database.
    user = relationship(
        "User", primaryjoin="Employee.user_id == User.id", foreign_keys=[user_id], back_populates="employee"
    )
    department = relationship(
        "Department",
        primaryjoin="Employee.department_id == Department.id",
        foreign_keys=[department_id],
        back_populates="employees",
    )
    shift = relationship(
        "Shift", primaryjoin="Employee.shift_id == Shift.id", foreign_keys=[shift_id], back_populates="employees"
    )
    manager = relationship(
        "Employee",
        primaryjoin="Employee.manager_id == remote(Employee.id)",
        foreign_keys=[manager_id],
    )
    face_encodings = relationship(
        "FaceEncoding",
        primaryjoin="Employee.id == FaceEncoding.employee_id",
        foreign_keys="FaceEncoding.employee_id",
        back_populates="employee",
    )

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_employees_company_id_id"),
        UniqueConstraint("company_id", "employee_code", name="uq_employees_company_code"),
        UniqueConstraint("company_id", "email", name="uq_employees_company_email"),
        ForeignKeyConstraint(
            ["company_id", "user_id"], ["users.company_id", "users.id"], name="fk_employees_user"
        ),
        ForeignKeyConstraint(
            ["company_id", "department_id"],
            ["departments.company_id", "departments.id"],
            name="fk_employees_department",
        ),
        ForeignKeyConstraint(
            ["company_id", "shift_id"], ["shifts.company_id", "shifts.id"], name="fk_employees_shift"
        ),
        ForeignKeyConstraint(
            ["company_id", "manager_id"],
            ["employees.company_id", "employees.id"],
            name="fk_employees_manager",
        ),
        CheckConstraint("manager_id IS NULL OR manager_id <> id", name="ck_employees_not_self_managed"),
        Index("ix_employees_company_manager", "company_id", "manager_id"),
    )

    @hybrid_property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @full_name.inplace.expression
    @classmethod
    def _full_name_expression(cls):
        # Lets searches match "Divya Menon", not only "Divya" or "Menon".
        return cls.first_name + " " + cls.last_name


class FaceEncoding(TenantMixin, BaseModel):
    __tablename__ = "face_encodings"

    employee_id = Column(Integer, nullable=False, index=True)
    # Legacy plaintext; new enrolments are stored only in encoding_enc (as_m2_sites_consents, BUG-AS-19). The
    # attendance.v2_prepare face step encrypts old rows; as_m11_legacy_cleanup drops this column.
    encoding = Column(Text, nullable=True)
    image_path = Column(String(255), nullable=True)
    quality_score = Column(Numeric(5, 2), nullable=True)
    is_primary = Column(Boolean, default=False, nullable=False)
    encoding_enc = Column(LargeBinary, nullable=True)  # envelope ciphertext (key purpose ATTENDANCE_BIOMETRIC)
    key_id = Column(String(64), nullable=True)  # tenant data key ids reach 40 characters
    consent_id = Column(Integer, nullable=True)  # the GRANTED BIOMETRIC_FACE consent at enrolment
    retention_until = Column(Date, nullable=True)  # deletion due (withdrawal / exit), AS 07 §6 B-8
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    # Enrolled before the consent gate (SP-AS-219): face keeps working until legacy_grace_until (AS 07 §5.5).
    legacy_enrolled = Column(Boolean, nullable=False, default=False, server_default="false")
    legacy_grace_until = Column(Date, nullable=True)

    employee = relationship(
        "Employee",
        primaryjoin="FaceEncoding.employee_id == Employee.id",
        foreign_keys=[employee_id],
        back_populates="face_encodings",
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "employee_id"],
            ["employees.company_id", "employees.id"],
            name="fk_face_encodings_employee",
        ),
        ForeignKeyConstraint(["company_id", "consent_id"], ["attendance_consents.company_id", "attendance_consents.id"],
                             name="fk_face_encodings_consent"),
        CheckConstraint("deleted_at IS NOT NULL OR encoding IS NOT NULL OR encoding_enc IS NOT NULL",
                        name="ck_face_encodings_stored"),
        CheckConstraint("(encoding_enc IS NULL) = (key_id IS NULL)", name="ck_face_encodings_key"),
        CheckConstraint("NOT legacy_enrolled OR legacy_grace_until IS NOT NULL", name="ck_face_encodings_legacy"),
    )


class SecondaryReportingLine(TenantMixin, BaseModel):
    """Functional, project or dotted-line manager. Never part of the primary tree."""

    __tablename__ = "secondary_reporting_lines"

    employee_id = Column(Integer, nullable=False, index=True)
    manager_id = Column(Integer, nullable=False, index=True)
    relation_type = Column(String(20), nullable=False)
    effective_from = Column(Date, nullable=False)
    effective_until = Column(Date, nullable=True)
    created_by_user_id = Column(Integer, nullable=True)

    employee = relationship(
        "Employee", primaryjoin="SecondaryReportingLine.employee_id == Employee.id", foreign_keys=[employee_id]
    )
    manager = relationship(
        "Employee", primaryjoin="SecondaryReportingLine.manager_id == Employee.id", foreign_keys=[manager_id]
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "employee_id"], ["employees.company_id", "employees.id"], name="fk_srl_employee"
        ),
        ForeignKeyConstraint(
            ["company_id", "manager_id"], ["employees.company_id", "employees.id"], name="fk_srl_manager"
        ),
        CheckConstraint("employee_id <> manager_id", name="ck_srl_not_self"),
        CheckConstraint(f"relation_type IN {RELATION_TYPES}", name="ck_srl_relation_type"),
        CheckConstraint(
            "effective_until IS NULL OR effective_until >= effective_from", name="ck_srl_window"
        ),
    )


class ManagerHistory(TenantMixin, BaseModel):
    """Immutable history of primary manager changes."""

    __tablename__ = "manager_history"

    employee_id = Column(Integer, nullable=False, index=True)
    manager_id = Column(Integer, nullable=True)
    effective_from = Column(DateTime(timezone=True), nullable=False)
    effective_until = Column(DateTime(timezone=True), nullable=True)
    changed_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "employee_id"], ["employees.company_id", "employees.id"], name="fk_mh_employee"
        ),
        ForeignKeyConstraint(
            ["company_id", "manager_id"], ["employees.company_id", "employees.id"], name="fk_mh_manager"
        ),
    )


class ApprovalDelegation(TenantMixin, BaseModel):
    """Temporarily lets `delegate` act on approvals routed to `delegator`."""

    __tablename__ = "approval_delegations"

    delegator_employee_id = Column(Integer, nullable=False, index=True)
    delegate_employee_id = Column(Integer, nullable=False, index=True)
    module = Column(String(20), nullable=False, default="leave")
    starts_on = Column(Date, nullable=False)
    ends_on = Column(Date, nullable=False)
    reason = Column(String(255), nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    delegator = relationship(
        "Employee",
        primaryjoin="ApprovalDelegation.delegator_employee_id == Employee.id",
        foreign_keys=[delegator_employee_id],
    )
    delegate = relationship(
        "Employee",
        primaryjoin="ApprovalDelegation.delegate_employee_id == Employee.id",
        foreign_keys=[delegate_employee_id],
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "delegator_employee_id"],
            ["employees.company_id", "employees.id"],
            name="fk_delegation_delegator",
        ),
        ForeignKeyConstraint(
            ["company_id", "delegate_employee_id"],
            ["employees.company_id", "employees.id"],
            name="fk_delegation_delegate",
        ),
        CheckConstraint("delegator_employee_id <> delegate_employee_id", name="ck_delegation_not_self"),
        CheckConstraint("ends_on >= starts_on", name="ck_delegation_window"),
        CheckConstraint(f"module IN {APPROVAL_MODULES}", name="ck_delegation_module"),
    )

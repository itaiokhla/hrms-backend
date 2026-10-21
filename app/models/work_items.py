"""Overtime entries and the minimal tasks module (3.4 §9-§10, 3.1 §5.5-§5.6, 3.9 §3.5).

An overtime entry is filed by the employee or a manager (source APPROVED_REQUESTS) or proposed from
attendance above the policy thresholds (ATTENDANCE_WITH_APPROVAL, or ATTENDANCE_AUTO where a policy
allows it). Only APPROVED entries are paid; an edit of an approved entry is a new version that
supersedes it once approved. Overlapping spans and more than 24 hours on one date are refused by a
trigger as well as by the service (EC-OT-10). consumed_by_run_employee_id is set by run finalization
(run tables come later in the migration chain, so it is not a foreign key).

A task (task_type is the rate-card key) has assignees with split percentages, optional evidence files
in private storage, and follows the TASK machine of app.payroll.state_machines; external_ref is unique
per tenant (EC-TSK-01) and accepted_quantity never exceeds the quantity (EC-TSK-04). Rate cards map
task_type x criteria (grade, skill, location) x effective dates to a rate per unit in a currency.
"""

from sqlalchemy import (
    CHAR,
    BigInteger,
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
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from app.models.base import BaseModel, TenantMixin

OVERTIME_STATUSES = ("PENDING_APPROVAL", "APPROVED", "REJECTED", "CANCELLED", "SUPERSEDED")
ACTIVE_OVERTIME_STATUSES = ("PENDING_APPROVAL", "APPROVED")
OVERTIME_SOURCES = ("APPROVED_REQUESTS", "ATTENDANCE_WITH_APPROVAL", "ATTENDANCE_AUTO")
OVERTIME_DAY_TYPES = ("WORKDAY", "WEEKLY_OFF", "HOLIDAY")
MAX_OVERTIME_MINUTES = 1440

TASK_STATUSES = ("DRAFT", "SUBMITTED", "APPROVED", "REJECTED", "DISPUTED", "PAID", "REVERSED")
TASK_APPROVED_STATUSES = ("APPROVED", "PAID", "REVERSED")
TASK_PAID_STATUSES = ("PAID", "REVERSED")
RATE_CARD_CRITERIA = ("grade", "skill", "location")


def _minutes_check(column: str) -> CheckConstraint:
    return CheckConstraint(
        f"{column} IS NULL OR {column} BETWEEN 0 AND {MAX_OVERTIME_MINUTES}", name=f"ck_overtime_entries_{column}"
    )


class OvertimeEntry(TenantMixin, BaseModel):
    """Overtime worked (or to be worked, as a pre-approval) on one shift start date.

    minutes_approved is what an approver accepted (edited down from minutes_requested, never up);
    minutes_computed is what attendance showed above the thresholds for proposals. bucket is the day
    type when known at entry. version/supersedes_id chain the versions of one piece of overtime.
    """

    __tablename__ = "overtime_entries"

    employee_id = Column(Integer, nullable=False, index=True)
    date = Column(Date, nullable=False)
    start_at = Column(DateTime(timezone=True), nullable=True)
    end_at = Column(DateTime(timezone=True), nullable=True)
    minutes_requested = Column(Integer, nullable=False)
    minutes_computed = Column(Integer, nullable=True)
    minutes_approved = Column(Integer, nullable=True)
    bucket = Column(String(12), nullable=True)
    source = Column(String(30), nullable=False, default="APPROVED_REQUESTS")
    status = Column(String(20), nullable=False, default="PENDING_APPROVAL")
    comp_off = Column(Boolean, nullable=False, default=False)
    pre_approval = Column(Boolean, nullable=False, default=False)
    reason = Column(Text, nullable=True)
    version = Column(Integer, nullable=False, default=1)
    supersedes_id = Column(Integer, nullable=True)
    approval_request_id = Column(Integer, nullable=True)
    requested_by_user_id = Column(Integer, nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    consumed_by_run_employee_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_overtime_entries_company_id_id"),
        ForeignKeyConstraint(
            ["company_id", "employee_id"], ["employees.company_id", "employees.id"], name="fk_overtime_entries_employee"
        ),
        ForeignKeyConstraint(
            ["company_id", "supersedes_id"], ["overtime_entries.company_id", "overtime_entries.id"],
            name="fk_overtime_entries_supersedes",
        ),
        ForeignKeyConstraint(
            ["company_id", "approval_request_id"], ["approval_requests.company_id", "approval_requests.id"],
            name="fk_overtime_entries_approval_request",
        ),
        ForeignKeyConstraint(
            ["company_id", "requested_by_user_id"], ["users.company_id", "users.id"],
            name="fk_overtime_entries_requested_by",
        ),
        CheckConstraint(f"status IN {OVERTIME_STATUSES}", name="ck_overtime_entries_status"),
        CheckConstraint(f"source IN {OVERTIME_SOURCES}", name="ck_overtime_entries_source"),
        CheckConstraint(f"bucket IS NULL OR bucket IN {OVERTIME_DAY_TYPES}", name="ck_overtime_entries_bucket"),
        CheckConstraint(
            f"minutes_requested BETWEEN 0 AND {MAX_OVERTIME_MINUTES}", name="ck_overtime_entries_minutes_requested"
        ),
        _minutes_check("minutes_computed"),
        _minutes_check("minutes_approved"),
        CheckConstraint(
            "minutes_approved IS NULL OR minutes_approved <= minutes_requested", name="ck_overtime_entries_approved_le"
        ),
        CheckConstraint(
            "status NOT IN ('APPROVED', 'SUPERSEDED') OR minutes_approved IS NOT NULL",
            name="ck_overtime_entries_approved_minutes",
        ),
        CheckConstraint("(start_at IS NULL) = (end_at IS NULL)", name="ck_overtime_entries_span_pair"),
        CheckConstraint(
            "start_at IS NULL OR (end_at > start_at AND end_at <= start_at + interval '24 hours')",
            name="ck_overtime_entries_span",
        ),
        CheckConstraint("version >= 1 AND (version = 1) = (supersedes_id IS NULL)", name="ck_overtime_entries_version"),
        CheckConstraint(
            "consumed_by_run_employee_id IS NULL OR status IN ('APPROVED', 'SUPERSEDED')",
            name="ck_overtime_entries_consumed",
        ),
        CheckConstraint("(status = 'PENDING_APPROVAL') = (decided_at IS NULL)", name="ck_overtime_entries_decided"),
        CheckConstraint("row_version >= 1", name="ck_overtime_entries_row_version"),
        Index("ix_overtime_entries_employee_date", "company_id", "employee_id", "date"),
        Index(
            "uq_overtime_entries_live_successor", "company_id", "supersedes_id", unique=True,
            postgresql_where=text("supersedes_id IS NOT NULL AND status IN ('PENDING_APPROVAL', 'APPROVED')"),
        ),
    )


class Task(TenantMixin, BaseModel):
    """A unit of paid work (3.4 §10.1). rate (per unit) overrides the rate card per TASK_PAY_POLICY.rate_source."""

    __tablename__ = "tasks"

    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    project = Column(String(100), nullable=True)
    category = Column(String(100), nullable=True)
    task_type = Column(String(50), nullable=False)
    quantity = Column(Numeric(12, 4), nullable=False)
    unit = Column(String(30), nullable=False)
    rate = Column(Numeric(19, 4), nullable=True)
    rate_currency = Column(CHAR(3), nullable=True)
    due_date = Column(Date, nullable=True)
    completed_on = Column(Date, nullable=True)
    status = Column(String(12), nullable=False, default="DRAFT")
    external_ref = Column(String(100), nullable=True)
    accepted_quantity = Column(Numeric(12, 4), nullable=True)
    approval_request_id = Column(Integer, nullable=True)
    approved_on = Column(Date, nullable=True)
    paid_in_run_id = Column(Integer, nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    reversed_at = Column(DateTime(timezone=True), nullable=True)
    status_reason = Column(Text, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    assignees = relationship(
        "TaskAssignee",
        primaryjoin="Task.id == TaskAssignee.task_id",
        foreign_keys="TaskAssignee.task_id",
        order_by="TaskAssignee.id",
        back_populates="task",
        cascade="all, delete-orphan",
    )
    evidence = relationship(
        "TaskEvidence",
        primaryjoin="Task.id == TaskEvidence.task_id",
        foreign_keys="TaskEvidence.task_id",
        order_by="TaskEvidence.id",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_tasks_company_id_id"),
        ForeignKeyConstraint(
            ["company_id", "approval_request_id"], ["approval_requests.company_id", "approval_requests.id"],
            name="fk_tasks_approval_request",
        ),
        ForeignKeyConstraint(
            ["company_id", "created_by_user_id"], ["users.company_id", "users.id"], name="fk_tasks_created_by"
        ),
        CheckConstraint(f"status IN {TASK_STATUSES}", name="ck_tasks_status"),
        CheckConstraint("quantity > 0", name="ck_tasks_quantity"),
        CheckConstraint(
            "accepted_quantity IS NULL OR (accepted_quantity >= 0 AND accepted_quantity <= quantity)",
            name="ck_tasks_accepted_quantity",
        ),
        CheckConstraint("(rate IS NULL) = (rate_currency IS NULL)", name="ck_tasks_rate_currency"),
        CheckConstraint("rate IS NULL OR rate >= 0", name="ck_tasks_rate"),
        CheckConstraint("rate_currency IS NULL OR rate_currency ~ '^[A-Z]{3}$'", name="ck_tasks_currency"),
        CheckConstraint(
            f"status NOT IN {TASK_APPROVED_STATUSES} OR approved_on IS NOT NULL", name="ck_tasks_approved_on"
        ),
        CheckConstraint(f"(status IN {TASK_PAID_STATUSES}) = (paid_in_run_id IS NOT NULL)", name="ck_tasks_paid"),
        CheckConstraint("row_version >= 1", name="ck_tasks_row_version"),
        Index(
            "uq_tasks_external_ref", "company_id", "external_ref", unique=True,
            postgresql_where=text("external_ref IS NOT NULL"),
        ),
        Index("ix_tasks_status", "company_id", "status"),
    )


class TaskAssignee(TenantMixin, BaseModel):
    """An employee paid for a task, with the share of the task amount (100.00 for a single assignee)."""

    __tablename__ = "task_assignees"

    task_id = Column(Integer, nullable=False, index=True)
    employee_id = Column(Integer, nullable=False, index=True)
    split_percent = Column(Numeric(5, 2), nullable=False, default=100)

    task = relationship(
        "Task",
        primaryjoin="TaskAssignee.task_id == Task.id",
        foreign_keys=[task_id],
        back_populates="assignees",
    )

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_task_assignees_company_id_id"),
        UniqueConstraint("task_id", "employee_id", name="uq_task_assignees_employee"),
        ForeignKeyConstraint(
            ["company_id", "task_id"], ["tasks.company_id", "tasks.id"], ondelete="CASCADE",
            name="fk_task_assignees_task",
        ),
        ForeignKeyConstraint(
            ["company_id", "employee_id"], ["employees.company_id", "employees.id"], name="fk_task_assignees_employee"
        ),
        CheckConstraint("split_percent > 0 AND split_percent <= 100", name="ck_task_assignees_split"),
    )


class TaskEvidence(TenantMixin, BaseModel):
    """A proof file of a task in private storage (app.core.storage, category task-evidence)."""

    __tablename__ = "task_evidence"

    task_id = Column(Integer, nullable=False, index=True)
    file_name = Column(String(200), nullable=False)
    storage_path = Column(String(300), nullable=False)
    mime = Column(String(100), nullable=False)
    bytes = Column(BigInteger, nullable=False)
    sha256 = Column(CHAR(64), nullable=False)
    uploaded_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_task_evidence_company_id_id"),
        UniqueConstraint("task_id", "sha256", name="uq_task_evidence_file"),
        ForeignKeyConstraint(
            ["company_id", "task_id"], ["tasks.company_id", "tasks.id"], ondelete="CASCADE",
            name="fk_task_evidence_task",
        ),
        ForeignKeyConstraint(
            ["company_id", "uploaded_by_user_id"], ["users.company_id", "users.id"],
            name="fk_task_evidence_uploaded_by",
        ),
        CheckConstraint("bytes > 0", name="ck_task_evidence_bytes"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_task_evidence_sha256"),
    )


class TaskRateCard(TenantMixin, BaseModel):
    """Rate per unit for a task type, optionally narrowed by grade, skill or location (3.4 §10.3).

    criteria holds only the given keys among grade (grade code), skill and location (branch code);
    rows with the same task type and criteria never overlap in time (trigger).
    """

    __tablename__ = "task_rate_cards"

    task_type = Column(String(50), nullable=False)
    criteria = Column(JSONB, nullable=False, default=dict)
    rate = Column(Numeric(19, 4), nullable=False)
    currency = Column(CHAR(3), nullable=False)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    created_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_task_rate_cards_company_id_id"),
        ForeignKeyConstraint(
            ["company_id", "created_by_user_id"], ["users.company_id", "users.id"],
            name="fk_task_rate_cards_created_by",
        ),
        CheckConstraint("rate >= 0", name="ck_task_rate_cards_rate"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_task_rate_cards_currency"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_task_rate_cards_window"),
        CheckConstraint("jsonb_typeof(criteria) = 'object'", name="ck_task_rate_cards_criteria"),
        Index("ix_task_rate_cards_type", "company_id", "task_type", "effective_from"),
    )

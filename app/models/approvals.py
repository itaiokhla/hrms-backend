"""Generic approval requests for modules other than leave (3.1 §5.14, 3.9 §3.9).

Leave keeps its own leave_approvals; overtime, tasks, wage inputs, payroll runs, bank changes,
settings and the other APPROVAL_MODULES route through approval_requests, whose steps mirror
leave_approvals. At most one PENDING request exists per subject. A request with an amount always
carries its currency.
"""

from sqlalchemy import (
    CheckConstraint,
    Column,
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
from app.models.leave import APPROVAL_MODULES, APPROVER_TYPES, STEP_STATUSES

APPROVAL_REQUEST_STATUSES = ("PENDING", "APPROVED", "REJECTED", "CANCELLED")
ACTED_AS = ("approver", "delegate", "override")


class ApprovalRequest(TenantMixin, BaseModel):
    """Approval of one subject (an overtime entry, a run, a bank change ...) through a chain of steps.

    requested_by_user_id is NULL for requests the system raises (for example arrears proposals).
    """

    __tablename__ = "approval_requests"

    module = Column(String(20), nullable=False)
    subject_type = Column(String(40), nullable=False)
    subject_id = Column(Integer, nullable=False)
    requested_by_user_id = Column(Integer, nullable=True)
    status = Column(String(12), nullable=False, default="PENDING")
    current_step = Column(Integer, nullable=True)
    amount = Column(Numeric(19, 4), nullable=True)
    currency = Column(String(3), nullable=True)
    context = Column(JSONB, nullable=False, default=dict)
    decided_at = Column(DateTime(timezone=True), nullable=True)

    steps = relationship(
        "ApprovalRequestStep",
        primaryjoin="ApprovalRequest.id == ApprovalRequestStep.request_id",
        foreign_keys="ApprovalRequestStep.request_id",
        order_by="ApprovalRequestStep.step_order",
        back_populates="request",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_approval_requests_company_id_id"),
        CheckConstraint(f"module IN {APPROVAL_MODULES}", name="ck_approval_requests_module"),
        CheckConstraint(f"status IN {APPROVAL_REQUEST_STATUSES}", name="ck_approval_requests_status"),
        CheckConstraint("(status = 'PENDING') = (decided_at IS NULL)", name="ck_approval_requests_decided"),
        CheckConstraint("(amount IS NULL) = (currency IS NULL)", name="ck_approval_requests_amount"),
        CheckConstraint("currency IS NULL OR currency ~ '^[A-Z]{3}$'", name="ck_approval_requests_currency"),
        CheckConstraint("jsonb_typeof(context) = 'object'", name="ck_approval_requests_context"),
        Index("ix_approval_requests_subject", "company_id", "subject_type", "subject_id"),
        Index(
            "uq_approval_requests_one_pending", "company_id", "module", "subject_type", "subject_id",
            unique=True, postgresql_where=text("status = 'PENDING'"),
        ),
    )


class ApprovalRequestStep(TenantMixin, BaseModel):
    """One step of an approval request, like leave_approvals: approver resolved when evaluated."""

    __tablename__ = "approval_request_steps"

    request_id = Column(Integer, nullable=False, index=True)
    step_order = Column(Integer, nullable=False)
    approver_type = Column(String(30), nullable=False)
    role_id = Column(Integer, nullable=True)
    specific_user_id = Column(Integer, nullable=True)
    status = Column(String(20), nullable=False, default="waiting")
    acted_by_user_id = Column(Integer, nullable=True)
    acted_as = Column(String(20), nullable=True)
    acted_at = Column(DateTime(timezone=True), nullable=True)
    remarks = Column(Text, nullable=True)

    request = relationship(
        "ApprovalRequest",
        primaryjoin="ApprovalRequestStep.request_id == ApprovalRequest.id",
        foreign_keys=[request_id],
        back_populates="steps",
    )

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_approval_request_steps_company_id_id"),
        UniqueConstraint("request_id", "step_order", name="uq_approval_request_steps_order"),
        ForeignKeyConstraint(
            ["company_id", "request_id"], ["approval_requests.company_id", "approval_requests.id"],
            ondelete="CASCADE", name="fk_approval_request_steps_request",
        ),
        ForeignKeyConstraint(
            ["company_id", "role_id"], ["roles.company_id", "roles.id"], name="fk_approval_request_steps_role"
        ),
        ForeignKeyConstraint(
            ["company_id", "specific_user_id"], ["users.company_id", "users.id"],
            name="fk_approval_request_steps_user",
        ),
        CheckConstraint("step_order >= 1", name="ck_approval_request_steps_order"),
        CheckConstraint(f"approver_type IN {APPROVER_TYPES}", name="ck_approval_request_steps_type"),
        CheckConstraint(
            "(approver_type = 'role') = (role_id IS NOT NULL)", name="ck_approval_request_steps_role_required"
        ),
        CheckConstraint(
            "(approver_type = 'specific_user') = (specific_user_id IS NOT NULL)",
            name="ck_approval_request_steps_user_required",
        ),
        CheckConstraint(f"status IN {STEP_STATUSES}", name="ck_approval_request_steps_status"),
        CheckConstraint(f"acted_as IN {ACTED_AS}", name="ck_approval_request_steps_acted_as"),
        Index("ix_approval_request_steps_pending", "company_id", postgresql_where=text("status = 'pending'")),
    )

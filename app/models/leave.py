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
    Text,
    TypeDecorator,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from app.models.base import BaseModel, TenantMixin

LEAVE_STATUSES = ("pending", "approved", "rejected", "cancelled")
START_PORTIONS = ("FULL", "SECOND_HALF")
END_PORTIONS = ("FULL", "FIRST_HALF")
CANCELLATION_STATUSES = ("pending", "approved", "rejected")
LEAVE_TYPE_KINDS = ("COMP_OFF",)
STEP_STATUSES = ("waiting", "pending", "approved", "rejected", "skipped", "cancelled")
# Leave chains use the first four (the leave API accepts no others); payroll modules add the last two.
APPROVER_TYPES = (
    "primary_manager", "skip_level_manager", "department_head", "role", "finance_role", "specific_user",
    "team_lead",  # JC-AS-21 / JC-AS-46: resolved by team_service.resolve_team_lead (AS-M4, migration 0052)
)
# Modules an approval chain, generic approval request or delegation belongs to (3.1 §5.14, 3.9 §3.9).
APPROVAL_MODULES = (
    "leave", "payroll_run", "compensation", "wage_input", "overtime", "task", "reimbursement", "loan",
    "bank_change", "payment_batch", "off_cycle", "final_settlement", "lop", "attendance", "settings",
    "timesheet",  # JC-AS-21 (AS-M4, migration 0052)
    "shift_request", "team_inputs",  # AS-M6 shift requests and manager-prepared payroll batches (migration 0058)
)


class LeaveDays(TypeDecorator):
    """NUMERIC(5,2) leave days read back as an int when whole (3, as before half days) and a Decimal otherwise."""

    impl = Numeric(5, 2)
    cache_ok = True

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return int(value) if value == value.to_integral_value() else value.normalize()


class LeaveType(TenantMixin, BaseModel):
    __tablename__ = "leave_types"

    code = Column(String(30), nullable=False)
    name = Column(String(100), nullable=False)
    annual_quota = Column(Integer, nullable=False, default=0)
    is_paid = Column(Boolean, nullable=False, default=True)
    is_unlimited = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)
    # Weekly offs and holidays inside a request are leave days of this type (3.4 §7.1, EC-LV-06, EC-LV-10).
    counts_non_working_days = Column(Boolean, nullable=False, default=False)
    allow_half_day = Column(Boolean, nullable=False, default=True)
    kind = Column(String(20), nullable=True)  # COMP_OFF; NULL for ordinary leave

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_leave_types_company_code"),
        CheckConstraint("kind IN ('COMP_OFF')", name="ck_leave_types_kind"),
    )


class ApprovalChainStep(TenantMixin, BaseModel):
    """Company-configured approval route of one module. Steps run in step_order.

    `role` steps name a role and `specific_user` steps a user; `finance_role` steps are resolved by
    the approvals service. min_days, min_amount (compared only with amounts in min_amount_currency)
    and condition limit when a step applies.
    """

    __tablename__ = "approval_chain_steps"

    module = Column(String(20), nullable=False, default="leave")
    step_order = Column(Integer, nullable=False)
    approver_type = Column(String(30), nullable=False)
    role_id = Column(Integer, nullable=True)
    specific_user_id = Column(Integer, nullable=True)
    # Step only applies when the request is at least this many days.
    min_days = Column(Integer, nullable=True)
    # ... or at least this amount (a money threshold always carries its currency).
    min_amount = Column(Numeric(19, 4), nullable=True)
    min_amount_currency = Column(String(3), nullable=True)
    # Thresholds other than days and amounts, interpreted by the approvals service.
    condition = Column(JSONB, nullable=True)
    # AS-M4 (migration 0052, IG-AS-81): the route the step belongs to (NULL = the module's legacy chain) and the
    # step SLA snapshot taken at instantiation (NULL = the module default, e.g. attendance.approval.sla_hours).
    route_id = Column(Integer, nullable=True)
    sla_hours = Column(Integer, nullable=True)

    __table_args__ = (
        Index("uq_approval_chain_step", "company_id", "module", text("COALESCE(route_id, 0)"), "step_order",
              unique=True),
        ForeignKeyConstraint(
            ["company_id", "route_id"], ["approval_routes.company_id", "approval_routes.id"],
            ondelete="CASCADE", name="fk_approval_chain_route",
        ),
        CheckConstraint("sla_hours IS NULL OR sla_hours BETWEEN 1 AND 720", name="ck_approval_chain_sla"),
        ForeignKeyConstraint(
            ["company_id", "role_id"], ["roles.company_id", "roles.id"], name="fk_approval_chain_role"
        ),
        ForeignKeyConstraint(
            ["company_id", "specific_user_id"], ["users.company_id", "users.id"],
            name="fk_approval_chain_specific_user",
        ),
        CheckConstraint(f"module IN {APPROVAL_MODULES}", name="ck_approval_chain_module"),
        CheckConstraint(f"approver_type IN {APPROVER_TYPES}", name="ck_approval_chain_type"),
        CheckConstraint(
            "(approver_type = 'role') = (role_id IS NOT NULL)", name="ck_approval_chain_role_required"
        ),
        CheckConstraint(
            "(approver_type = 'specific_user') = (specific_user_id IS NOT NULL)",
            name="ck_approval_chain_user_required",
        ),
        CheckConstraint(
            "(min_amount IS NULL) = (min_amount_currency IS NULL) AND (min_amount IS NULL OR min_amount >= 0)",
            name="ck_approval_chain_min_amount",
        ),
        CheckConstraint(
            "min_amount_currency IS NULL OR min_amount_currency ~ '^[A-Z]{3}$'", name="ck_approval_chain_currency"
        ),
    )


class Leave(TenantMixin, BaseModel):
    __tablename__ = "leaves"

    employee_id = Column(Integer, nullable=False, index=True)
    leave_type = Column(String(30), nullable=False)
    start_date = Column(Date, nullable=False, index=True)
    end_date = Column(Date, nullable=False, index=True)
    # Leave days charged (half days are 0.5); weekly offs and holidays count only per the leave type.
    number_of_days = Column(LeaveDays(), nullable=False)
    start_portion = Column(String(12), nullable=False, default="FULL")
    end_portion = Column(String(12), nullable=False, default="FULL")
    reason = Column(Text, nullable=True)
    status = Column(String(20), default="pending", nullable=False, index=True)
    current_step = Column(Integer, nullable=True)
    decided_by_user_id = Column(Integer, nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    remarks = Column(Text, nullable=True)
    # Cancelling approved leave is itself approved (EC-LV-04); status stays approved until then.
    cancellation_status = Column(String(12), nullable=True)
    cancellation_reason = Column(Text, nullable=True)
    cancellation_requested_at = Column(DateTime(timezone=True), nullable=True)
    cancellation_decided_at = Column(DateTime(timezone=True), nullable=True)
    cancellation_decided_by_user_id = Column(Integer, nullable=True)

    employee = relationship("Employee", primaryjoin="Leave.employee_id == Employee.id", foreign_keys=[employee_id])
    approvals = relationship(
        "LeaveApproval",
        primaryjoin="Leave.id == LeaveApproval.leave_id",
        foreign_keys="LeaveApproval.leave_id",
        order_by="LeaveApproval.step_order",
        back_populates="leave",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "employee_id"], ["employees.company_id", "employees.id"], name="fk_leaves_employee"
        ),
        UniqueConstraint("company_id", "id", name="uq_leaves_company_id_id"),
        CheckConstraint(f"status IN {LEAVE_STATUSES}", name="ck_leaves_status"),
        CheckConstraint("end_date >= start_date", name="ck_leaves_dates"),
        CheckConstraint(f"start_portion IN {START_PORTIONS}", name="ck_leaves_start_portion"),
        CheckConstraint(f"end_portion IN {END_PORTIONS}", name="ck_leaves_end_portion"),
        CheckConstraint(
            "NOT (start_date = end_date AND start_portion = 'SECOND_HALF' AND end_portion = 'FIRST_HALF')",
            name="ck_leaves_portions",
        ),
        CheckConstraint(f"cancellation_status IN {CANCELLATION_STATUSES}", name="ck_leaves_cancellation_status"),
        Index("ix_leaves_company_status", "company_id", "status"),
    )


class LeaveApproval(TenantMixin, BaseModel):
    """One step of a leave request's approval route.

    Hierarchy-based steps resolve their approver when evaluated, so a manager
    change re-routes pending steps automatically and removes the previous
    manager's authority.
    """

    __tablename__ = "leave_approvals"

    leave_id = Column(Integer, nullable=False, index=True)
    step_order = Column(Integer, nullable=False)
    approver_type = Column(String(30), nullable=False)
    role_id = Column(Integer, nullable=True)
    status = Column(String(20), nullable=False, default="waiting")
    acted_by_user_id = Column(Integer, nullable=True)
    acted_as = Column(String(20), nullable=True)  # approver | delegate | override
    acted_at = Column(DateTime(timezone=True), nullable=True)
    remarks = Column(Text, nullable=True)

    leave = relationship(
        "Leave", primaryjoin="LeaveApproval.leave_id == Leave.id", foreign_keys=[leave_id], back_populates="approvals"
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "leave_id"], ["leaves.company_id", "leaves.id"], ondelete="CASCADE", name="fk_leave_approvals_leave"
        ),
        ForeignKeyConstraint(
            ["company_id", "role_id"], ["roles.company_id", "roles.id"], name="fk_leave_approvals_role"
        ),
        UniqueConstraint("leave_id", "step_order", name="uq_leave_approval_step"),
        CheckConstraint(f"status IN {STEP_STATUSES}", name="ck_leave_approvals_status"),
    )

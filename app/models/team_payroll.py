"""Team payroll (owner request 2026-10-29; migration 0058).

* ``team_input_batches`` / ``team_input_lines`` / ``team_input_attachments`` — a manager or team lead
  (``team_pay:submit`` over the people) prepares one-time payroll inputs for their team (bonus, deduction, overtime
  units, arrears, adjustments) with supporting files and submits the batch; the approvals engine (module
  ``team_inputs``, ``payroll:inputs:approve``) routes it to HR / payroll, and approval writes each line as an
  APPROVED wage input in payroll (the batch approval is the input approval). RETURN sends it back for edits.
* ``cost_invoices`` — a payroll-cost invoice per finalized run and cost group (department, branch, team, legal
  entity or pay group): employee count, gross, employer contributions, an optional service fee and entered taxes;
  DRAFT -> ISSUED (by someone other than the preparer) -> VOID.
"""

from sqlalchemy import (
    Column, Date, DateTime, ForeignKeyConstraint, Index, Integer, LargeBinary, Numeric, String, Text, UniqueConstraint,
    CheckConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import BaseModel, TenantMixin

BATCH_STATUSES = ("DRAFT", "SUBMITTED", "RETURNED", "APPROVED", "REJECTED", "CANCELLED")
COST_GROUPS = ("DEPARTMENT", "BRANCH", "TEAM", "LEGAL_ENTITY", "PAY_GROUP", "COMPANY")
COST_INVOICE_STATUSES = ("DRAFT", "ISSUED", "VOID")


def _fk(table: str, column: str, target: str, name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["company_id", column], [f"{target}.company_id", f"{target}.id"],
                                name=f"fk_{table}_{name}")


def _in(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


class TeamInputBatch(TenantMixin, BaseModel):
    __tablename__ = "team_input_batches"

    title = Column(String(120), nullable=False)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    status = Column(String(10), nullable=False, default="DRAFT")
    notes = Column(Text, nullable=True)
    prepared_by_user_id = Column(Integer, nullable=False)
    prepared_by_employee_id = Column(Integer, nullable=True)
    approval_request_id = Column(Integer, nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decided_by_user_id = Column(Integer, nullable=True)
    decision_note = Column(String(500), nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_team_input_batches_company_id_id"),
        _fk("team_input_batches", "prepared_by_user_id", "users", "prepared_by"),
        _fk("team_input_batches", "prepared_by_employee_id", "employees", "prepared_by_employee"),
        _fk("team_input_batches", "approval_request_id", "approval_requests", "approval"),
        _fk("team_input_batches", "decided_by_user_id", "users", "decided_by"),
        CheckConstraint(f"status IN {_in(BATCH_STATUSES)}", name="ck_team_input_batches_status"),
        CheckConstraint("period_end >= period_start", name="ck_team_input_batches_period"),
        Index("ix_team_input_batches_status", "company_id", "status"),
    )


class TeamInputLine(TenantMixin, BaseModel):
    __tablename__ = "team_input_lines"

    batch_id = Column(Integer, nullable=False, index=True)
    employee_id = Column(Integer, nullable=False)
    component_code = Column(String(40), nullable=False)
    amount = Column(Numeric(14, 2), nullable=True)
    units = Column(Numeric(10, 2), nullable=True)
    rate = Column(Numeric(14, 4), nullable=True)
    reason = Column(String(500), nullable=True)
    wage_input_id = Column(Integer, nullable=True)
    problem = Column(String(300), nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_team_input_lines_company_id_id"),
        _fk("team_input_lines", "batch_id", "team_input_batches", "batch"),
        _fk("team_input_lines", "employee_id", "employees", "employee"),
        _fk("team_input_lines", "wage_input_id", "wage_inputs", "wage_input"),
        CheckConstraint("amount IS NOT NULL OR units IS NOT NULL", name="ck_team_input_lines_value"),
    )


class TeamInputAttachment(TenantMixin, BaseModel):
    __tablename__ = "team_input_attachments"

    batch_id = Column(Integer, nullable=False, index=True)
    file_name = Column(String(200), nullable=False)
    content_type = Column(String(100), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    sha256 = Column(String(64), nullable=False)
    content = Column(LargeBinary, nullable=False)
    uploaded_by_user_id = Column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_team_input_attachments_company_id_id"),
        _fk("team_input_attachments", "batch_id", "team_input_batches", "batch"),
        _fk("team_input_attachments", "uploaded_by_user_id", "users", "uploaded_by"),
        CheckConstraint("size_bytes > 0 AND size_bytes <= 5242880", name="ck_team_input_attachments_size"),
    )


class CostInvoice(TenantMixin, BaseModel):
    __tablename__ = "cost_invoices"

    run_id = Column(Integer, nullable=False, index=True)
    group_by = Column(String(14), nullable=False)
    group_id = Column(Integer, nullable=True)
    group_label = Column(String(200), nullable=False)
    number = Column(String(40), nullable=True)
    status = Column(String(8), nullable=False, default="DRAFT")
    currency = Column(String(3), nullable=False)
    bill_to = Column(JSONB, nullable=False, default=dict)
    lines = Column(JSONB, nullable=False, default=list)
    taxes = Column(JSONB, nullable=False, default=list)
    employee_count = Column(Integer, nullable=False, default=0)
    gross_total = Column(Numeric(16, 2), nullable=False, default=0)
    employer_total = Column(Numeric(16, 2), nullable=False, default=0)
    fee_percent = Column(Numeric(6, 3), nullable=False, default=0)
    fee_total = Column(Numeric(16, 2), nullable=False, default=0)
    subtotal = Column(Numeric(16, 2), nullable=False, default=0)
    tax_total = Column(Numeric(16, 2), nullable=False, default=0)
    total = Column(Numeric(16, 2), nullable=False, default=0)
    issue_date = Column(Date, nullable=True)
    due_date = Column(Date, nullable=True)
    notes = Column(Text, nullable=True)
    created_by_user_id = Column(Integer, nullable=False)
    issued_by_user_id = Column(Integer, nullable=True)
    issued_at = Column(DateTime(timezone=True), nullable=True)
    void_reason = Column(String(500), nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_cost_invoices_company_id_id"),
        _fk("cost_invoices", "run_id", "payroll_runs", "run"),
        _fk("cost_invoices", "created_by_user_id", "users", "created_by"),
        _fk("cost_invoices", "issued_by_user_id", "users", "issued_by"),
        CheckConstraint(f"group_by IN {_in(COST_GROUPS)}", name="ck_cost_invoices_group_by"),
        CheckConstraint(f"status IN {_in(COST_INVOICE_STATUSES)}", name="ck_cost_invoices_status"),
        CheckConstraint("status = 'DRAFT' OR number IS NOT NULL", name="ck_cost_invoices_number"),
        CheckConstraint("issued_by_user_id IS NULL OR issued_by_user_id <> created_by_user_id",
                        name="ck_cost_invoices_maker_checker"),
        Index("uq_cost_invoices_number", "company_id", "number", unique=True, postgresql_where=text("number IS NOT NULL")),
    )

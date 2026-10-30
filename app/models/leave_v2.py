"""Leave v2 tables (SPINE §4.2; migration 0063): change requests, blackouts, coverage rules, reason codes, year-end
runs, imports, events, comp-off claims, encashment requests, discussion, attachments and document requests.

Every table is tenant-owned (``TenantMixin`` + RLS ``tenant_isolation``) with composite same-company foreign keys.
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
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import BaseModel, TenantMixin

CHANGE_KINDS = ("CANCEL", "PARTIAL_CANCEL", "MODIFY", "RECALL", "CONVERT")
CHANGE_STATUSES = ("PENDING", "APPROVED", "REJECTED", "WITHDRAWN")
CHANGE_INITIATORS = ("EMPLOYEE", "EMPLOYER", "SYSTEM")
RULE_MODES = ("WARN", "BLOCK")
RULE_STATUSES = ("ACTIVE", "ARCHIVED")
REASON_APPLIES_TO = ("REJECT", "RETURN", "CANCEL", "RECALL", "ADJUST", "OVERRIDE")
YEAR_END_STATUSES = ("DRAFT", "POSTED", "REVERSED")
IMPORT_KINDS = ("OPENING_BALANCES", "BALANCE_ADJUSTMENTS", "LEAVE_HISTORY")
IMPORT_STATUSES = ("DRY_RUN", "COMMITTED", "FAILED", "EXPIRED")
EVENT_STATUSES = ("OPEN", "CLOSED", "CANCELLED")
COMP_OFF_SOURCES = ("HOLIDAY_WORK", "WEEKLY_OFF_WORK", "OVERTIME", "SUBSTITUTE_HOLIDAY", "RECALL", "MANUAL")
COMP_OFF_STATUSES = ("PENDING", "APPROVED", "REJECTED", "WITHDRAWN", "REVOKED")
ENCASH_TRIGGERS = ("EXIT", "YEAR_END", "ON_REQUEST")
ENCASH_STATUSES = ("PENDING", "APPROVED", "REJECTED", "WITHDRAWN", "PAID", "CANCELLED")
MESSAGE_KINDS = ("COMMENT", "DECISION_NOTE", "SYSTEM_EVENT", "DOCUMENT_REQUEST", "SUGGESTED_CHANGE", "INTERNAL_NOTE")
MESSAGE_VISIBILITIES = ("PARTICIPANTS", "APPROVERS_HR")
ATTACHMENT_CATEGORIES = ("MEDICAL_CERTIFICATE", "FIT_TO_WORK", "PARENTAL_EVIDENCE", "BEREAVEMENT_EVIDENCE",
                         "MARRIAGE_EVIDENCE", "OFFICIAL_SUMMONS", "STUDY_EVIDENCE", "TRAVEL_DOCUMENT",
                         "SUPPORTING_DOCUMENT")
SCAN_STATUSES = ("PENDING_SCAN", "CLEAN", "INFECTED", "SCAN_FAILED", "NOT_SCANNED")
VERIFICATION_STATUSES = ("SUBMITTED", "VERIFIED", "REJECTED")
DOCUMENT_REQUEST_STATUSES = ("OPEN", "FULFILLED", "WAIVED", "OVERDUE")


def _fk(table: str, column: str, target: str, name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["company_id", column], [f"{target}.company_id", f"{target}.id"],
                                name=f"fk_{table}_{name}")


class LeaveChangeRequest(TenantMixin, BaseModel):
    """A change to approved leave (SPINE §5.2): CANCEL, PARTIAL_CANCEL, MODIFY, RECALL or CONVERT; one PENDING per
    leave. ``proposed`` holds the new dates / portions / type; ``units_delta`` the balance effect once applied."""

    __tablename__ = "leave_change_requests"

    leave_id = Column(Integer, nullable=False)
    employee_id = Column(Integer, nullable=False)
    kind = Column(String(16), nullable=False)
    status = Column(String(10), nullable=False, default="PENDING")
    from_version = Column(Integer, nullable=False, default=1)
    proposed = Column(JSONB, nullable=False, default=dict)
    reason = Column(String(1000), nullable=True)
    reason_code = Column(String(40), nullable=True)
    initiator = Column(String(10), nullable=False, default="EMPLOYEE")
    requested_by_user_id = Column(Integer, nullable=True)
    decided_by_user_id = Column(Integer, nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_note = Column(String(1000), nullable=True)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    applied_at = Column(DateTime(timezone=True), nullable=True)
    units_delta = Column(Numeric(6, 2), nullable=False, default=0)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_leave_change_requests_company_id_id"),
        _fk("leave_change_requests", "leave_id", "leaves", "leave"),
        _fk("leave_change_requests", "employee_id", "employees", "employee"),
        CheckConstraint(f"kind IN {CHANGE_KINDS}", name="ck_leave_change_requests_kind"),
        CheckConstraint(f"status IN {CHANGE_STATUSES}", name="ck_leave_change_requests_status"),
        CheckConstraint(f"initiator IN {CHANGE_INITIATORS}", name="ck_leave_change_requests_initiator"),
        Index("uq_leave_change_requests_pending", "company_id", "leave_id", unique=True,
              postgresql_where=text("status = 'PENDING'")),
        Index("ix_leave_change_requests_employee", "company_id", "employee_id", "status"),
    )


class LeaveBlackout(TenantMixin, BaseModel):
    """Dates when leave is blocked or warned (03-leave/01 §8.1 rule 15); empty ``leave_type_codes`` = every type."""

    __tablename__ = "leave_blackouts"

    name = Column(String(100), nullable=False)
    from_date = Column(Date, nullable=False)
    to_date = Column(Date, nullable=False)
    legal_entity_id = Column(Integer, nullable=True)
    branch_ids = Column(JSONB(none_as_null=True), nullable=True)
    department_ids = Column(JSONB(none_as_null=True), nullable=True)
    leave_type_codes = Column(JSONB, nullable=False, default=list)
    mode = Column(String(5), nullable=False, default="BLOCK")
    reason = Column(String(500), nullable=True)
    status = Column(String(10), nullable=False, default="ACTIVE")
    created_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_leave_blackouts_company_id_id"),
        _fk("leave_blackouts", "legal_entity_id", "legal_entities", "entity"),
        CheckConstraint("to_date >= from_date", name="ck_leave_blackouts_dates"),
        CheckConstraint(f"mode IN {RULE_MODES}", name="ck_leave_blackouts_mode"),
        CheckConstraint(f"status IN {RULE_STATUSES}", name="ck_leave_blackouts_status"),
    )


class LeaveCoverageRule(TenantMixin, BaseModel):
    """Minimum staffing of one department, branch or team (rule 16): at most N people, or N %, away per day."""

    __tablename__ = "leave_coverage_rules"

    name = Column(String(100), nullable=False)
    department_id = Column(Integer, nullable=True)
    branch_id = Column(Integer, nullable=True)
    team_id = Column(Integer, nullable=True)
    max_absent_count = Column(Integer, nullable=True)
    max_absent_pct = Column(Numeric(5, 2), nullable=True)
    count_pending = Column(Boolean, nullable=False, default=True)
    mode = Column(String(5), nullable=False, default="WARN")
    status = Column(String(10), nullable=False, default="ACTIVE")
    created_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_leave_coverage_rules_company_id_id"),
        _fk("leave_coverage_rules", "department_id", "departments", "department"),
        _fk("leave_coverage_rules", "branch_id", "branches", "branch"),
        _fk("leave_coverage_rules", "team_id", "teams", "team"),
        CheckConstraint("num_nonnulls(department_id, branch_id, team_id) = 1", name="ck_leave_coverage_rules_group"),
        CheckConstraint("(max_absent_count IS NULL) <> (max_absent_pct IS NULL)", name="ck_leave_coverage_rules_limit"),
        CheckConstraint(f"mode IN {RULE_MODES}", name="ck_leave_coverage_rules_mode"),
        CheckConstraint(f"status IN {RULE_STATUSES}", name="ck_leave_coverage_rules_status"),
    )


class LeaveReasonCode(TenantMixin, BaseModel):
    __tablename__ = "leave_reason_codes"

    code = Column(String(40), nullable=False)
    label = Column(String(100), nullable=False)
    applies_to = Column(String(10), nullable=False)
    counts_as_refused = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_leave_reason_codes_company_id_id"),
        UniqueConstraint("company_id", "applies_to", "code", name="uq_leave_reason_codes_code"),
        CheckConstraint(f"applies_to IN {REASON_APPLIES_TO}", name="ck_leave_reason_codes_applies"),
    )


class LeaveYearEndRun(TenantMixin, BaseModel):
    """Year-end carry-forward, lapse and encashment of one cycle end (SPINE §6.7): DRAFT preview → POSTED → REVERSED."""

    __tablename__ = "leave_year_end_runs"

    legal_entity_id = Column(Integer, nullable=True)
    cycle_end = Column(Date, nullable=False)
    status = Column(String(10), nullable=False, default="DRAFT")
    summary = Column(JSONB, nullable=False, default=dict)
    rows = Column(JSONB, nullable=False, default=list)
    created_by_user_id = Column(Integer, nullable=True)
    posted_by_user_id = Column(Integer, nullable=True)
    posted_at = Column(DateTime(timezone=True), nullable=True)
    reversed_by_user_id = Column(Integer, nullable=True)
    reversed_at = Column(DateTime(timezone=True), nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_leave_year_end_runs_company_id_id"),
        _fk("leave_year_end_runs", "legal_entity_id", "legal_entities", "entity"),
        CheckConstraint(f"status IN {YEAR_END_STATUSES}", name="ck_leave_year_end_runs_status"),
    )


class LeaveImport(TenantMixin, BaseModel):
    __tablename__ = "leave_imports"

    kind = Column(String(24), nullable=False)
    status = Column(String(10), nullable=False, default="DRY_RUN")
    file_name = Column(String(200), nullable=True)
    report = Column(JSONB, nullable=False, default=dict)
    rows = Column(JSONB, nullable=False, default=list)
    created_by_user_id = Column(Integer, nullable=True)
    committed_by_user_id = Column(Integer, nullable=True)
    committed_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_leave_imports_company_id_id"),
        CheckConstraint(f"kind IN {IMPORT_KINDS}", name="ck_leave_imports_kind"),
        CheckConstraint(f"status IN {IMPORT_STATUSES}", name="ck_leave_imports_status"),
    )


class LeaveEvent(TenantMixin, BaseModel):
    """One occurrence of an EVENT leave (birth, death, marriage…) with its entitled units (03-leave/06 §6)."""

    __tablename__ = "leave_events"

    employee_id = Column(Integer, nullable=False)
    leave_type_code = Column(String(30), nullable=False)
    event_type = Column(String(30), nullable=False)
    event_date = Column(Date, nullable=False)
    entitled_units = Column(Numeric(6, 2), nullable=False)
    used_units = Column(Numeric(6, 2), nullable=False, default=0)
    take_until = Column(Date, nullable=True)
    status = Column(String(10), nullable=False, default="OPEN")
    note = Column(String(500), nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_leave_events_company_id_id"),
        _fk("leave_events", "employee_id", "employees", "employee"),
        CheckConstraint(f"status IN {EVENT_STATUSES}", name="ck_leave_events_status"),
        CheckConstraint("entitled_units > 0 AND used_units >= 0", name="ck_leave_events_units"),
        Index("ix_leave_events_employee", "company_id", "employee_id", "leave_type_code"),
    )


class CompOffClaim(TenantMixin, BaseModel):
    """A claim for compensatory leave for work on a holiday or weekly off (SPINE §5.3); approval posts a COMP_OFF lot."""

    __tablename__ = "comp_off_claims"

    employee_id = Column(Integer, nullable=False)
    worked_date = Column(Date, nullable=False)
    source = Column(String(20), nullable=False)
    worked_minutes = Column(Integer, nullable=False)
    credit_units = Column(Numeric(4, 2), nullable=False)
    leave_type_code = Column(String(30), nullable=False)
    status = Column(String(10), nullable=False, default="PENDING")
    reason = Column(String(1000), nullable=True)
    expires_on = Column(Date, nullable=True)
    requested_by_user_id = Column(Integer, nullable=True)
    decided_by_user_id = Column(Integer, nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_note = Column(String(1000), nullable=True)
    current_step = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_comp_off_claims_company_id_id"),
        _fk("comp_off_claims", "employee_id", "employees", "employee"),
        CheckConstraint(f"source IN {COMP_OFF_SOURCES}", name="ck_comp_off_claims_source"),
        CheckConstraint(f"status IN {COMP_OFF_STATUSES}", name="ck_comp_off_claims_status"),
        CheckConstraint("credit_units > 0 AND worked_minutes >= 0", name="ck_comp_off_claims_units"),
        Index("uq_comp_off_claims_active", "company_id", "employee_id", "worked_date", unique=True,
              postgresql_where=text("status IN ('PENDING', 'APPROVED')")),
    )


class LeaveEncashmentRequest(TenantMixin, BaseModel):
    """Units of a leave balance to be paid out (SPINE §5.4): approval reserves them; payroll pays them once."""

    __tablename__ = "leave_encashment_requests"

    employee_id = Column(Integer, nullable=False)
    leave_type_code = Column(String(30), nullable=False)
    cycle_start = Column(Date, nullable=True)
    units = Column(Numeric(6, 2), nullable=False)
    trigger = Column(String(10), nullable=False, default="ON_REQUEST")
    status = Column(String(10), nullable=False, default="PENDING")
    reason = Column(String(1000), nullable=True)
    requested_by_user_id = Column(Integer, nullable=True)
    decided_by_user_id = Column(Integer, nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_note = Column(String(1000), nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    payroll_reference = Column(String(100), nullable=True)
    year_end_run_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_leave_encashment_requests_company_id_id"),
        _fk("leave_encashment_requests", "employee_id", "employees", "employee"),
        _fk("leave_encashment_requests", "year_end_run_id", "leave_year_end_runs", "year_end_run"),
        CheckConstraint(f"trigger IN {ENCASH_TRIGGERS}", name="ck_leave_encashment_requests_trigger"),
        CheckConstraint(f"status IN {ENCASH_STATUSES}", name="ck_leave_encashment_requests_status"),
        CheckConstraint("units > 0", name="ck_leave_encashment_requests_units"),
        Index("ix_leave_encashment_requests_employee", "company_id", "employee_id", "leave_type_code", "status"),
    )


class LeaveMessage(TenantMixin, BaseModel):
    """One message of a request's discussion (SPINE §5.8); append-only; INTERNAL_NOTE is never shown to the requester."""

    __tablename__ = "leave_messages"

    leave_id = Column(Integer, nullable=False)
    author_user_id = Column(Integer, nullable=True)  # NULL = system
    kind = Column(String(20), nullable=False, default="COMMENT")
    visibility = Column(String(14), nullable=False, default="PARTICIPANTS")
    body = Column(Text, nullable=False)
    reason_code = Column(String(40), nullable=True)
    details = Column(JSONB(none_as_null=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_leave_messages_company_id_id"),
        _fk("leave_messages", "leave_id", "leaves", "leave"),
        CheckConstraint(f"kind IN {MESSAGE_KINDS}", name="ck_leave_messages_kind"),
        CheckConstraint(f"visibility IN {MESSAGE_VISIBILITIES}", name="ck_leave_messages_visibility"),
        CheckConstraint("char_length(body) BETWEEN 1 AND 4000", name="ck_leave_messages_body"),
        Index("ix_leave_messages_leave", "company_id", "leave_id", "id"),
    )


class LeaveAttachment(TenantMixin, BaseModel):
    """A document of a leave request (SPINE §5.7); MEDICAL ones need ``leave:view_medical``; bytes in tenant storage."""

    __tablename__ = "leave_attachments"

    employee_id = Column(Integer, nullable=False)
    leave_id = Column(Integer, nullable=True)  # NULL while uploaded before the request is saved
    category = Column(String(24), nullable=False)
    sensitivity = Column(String(8), nullable=False, default="GENERAL")
    storage_path = Column(String(400), nullable=False)
    sha256 = Column(String(64), nullable=False)
    mime = Column(String(100), nullable=False)
    bytes = Column(Integer, nullable=False)
    original_name = Column(String(200), nullable=False)
    scan_status = Column(String(12), nullable=False, default="NOT_SCANNED")
    verification_status = Column(String(10), nullable=False, default="SUBMITTED")
    verified_by_user_id = Column(Integer, nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    rejection_reason = Column(String(500), nullable=True)
    uploaded_by_user_id = Column(Integer, nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_leave_attachments_company_id_id"),
        _fk("leave_attachments", "employee_id", "employees", "employee"),
        _fk("leave_attachments", "leave_id", "leaves", "leave"),
        CheckConstraint(f"category IN {ATTACHMENT_CATEGORIES}", name="ck_leave_attachments_category"),
        CheckConstraint("sensitivity IN ('GENERAL', 'MEDICAL')", name="ck_leave_attachments_sensitivity"),
        CheckConstraint(f"scan_status IN {SCAN_STATUSES}", name="ck_leave_attachments_scan"),
        CheckConstraint(f"verification_status IN {VERIFICATION_STATUSES}", name="ck_leave_attachments_verification"),
        CheckConstraint("bytes > 0", name="ck_leave_attachments_bytes"),
        Index("ix_leave_attachments_leave", "company_id", "leave_id"),
    )


class LeaveAttachmentAccess(TenantMixin, BaseModel):
    __tablename__ = "leave_attachment_access_log"

    attachment_id = Column(Integer, nullable=False)
    user_id = Column(Integer, nullable=True)
    action = Column(String(10), nullable=False)
    ip_address = Column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_leave_attachment_access_log_company_id_id"),
        _fk("leave_attachment_access_log", "attachment_id", "leave_attachments", "attachment"),
        CheckConstraint("action IN ('VIEW', 'DOWNLOAD')", name="ck_leave_attachment_access_log_action"),
    )


class LeaveDocumentRequest(TenantMixin, BaseModel):
    __tablename__ = "leave_document_requests"

    leave_id = Column(Integer, nullable=False)
    category = Column(String(24), nullable=False)
    due_date = Column(Date, nullable=False)
    status = Column(String(10), nullable=False, default="OPEN")
    note = Column(String(500), nullable=True)
    requested_by_user_id = Column(Integer, nullable=True)
    fulfilled_attachment_id = Column(Integer, nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_leave_document_requests_company_id_id"),
        _fk("leave_document_requests", "leave_id", "leaves", "leave"),
        _fk("leave_document_requests", "fulfilled_attachment_id", "leave_attachments", "attachment"),
        CheckConstraint(f"category IN {ATTACHMENT_CATEGORIES}", name="ck_leave_document_requests_category"),
        CheckConstraint(f"status IN {DOCUMENT_REQUEST_STATUSES}", name="ck_leave_document_requests_status"),
        Index("ix_leave_document_requests_leave", "company_id", "leave_id", "status"),
    )

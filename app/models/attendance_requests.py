"""Attendance requests, reason codes, attachments, period closes and timesheet approvals (AS-M4, G-13).

Owner: docs/attendance-shift/04-attendance/04-regularization-requests-approvals.md (§3 kinds and payloads, §4 states,
§7 reason codes, §8.4 attachments, §12 timesheets, §14.3 period close). Tables of migration ``0052_as_m4_requests``.

* ``attendance_requests`` — one request to change attendance after the fact (kinds ``ATTENDANCE_REQUEST_KINDS``,
  modes ``REQUEST_MODES``). Raw punches are never edited: on approval (or at once for DIRECT) the service writes
  REQUEST / MANUAL punches and sessions (``app/services/attendance_requests.py``). At most one open (PENDING,
  RETURNED) request per (employee, date, kind, session) (``uq_attendance_requests_open``, P-10).
* ``attendance_reason_codes`` — per-company catalogue, seeded (§7.1); a seed's code never changes.
* ``attendance_attachments`` — evidence uploaded first and linked at submit (SP-AS-076). Interim storage of
  ``app.core.storage`` until the LH generic attachments module ships (G-12; IG-AS-83).
* ``attendance_period_closes`` — the optional attendance-side close of a cycle per legal entity (§14.3).
* ``attendance_timesheet_approvals`` — weekly / cycle sign-off rows (§12; storage only in G-13, FU-AS-30).
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
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from app.models.base import BaseModel, TenantMixin
from app.models.leave import APPROVAL_MODULES

ATTENDANCE_REQUEST_KINDS = ("MISSED_PUNCH", "ADD_SESSION", "CORRECTION", "VOID_SESSION", "MARK_EXCUSE",
                            "STATUS_OVERRIDE")
SESSION_KINDS = ("CORRECTION", "VOID_SESSION")  # session_id required
REQUEST_MODES = ("SELF", "ON_BEHALF", "DIRECT")
ATTENDANCE_REQUEST_STATUSES = ("DRAFT", "PENDING", "RETURNED", "APPROVED", "REJECTED", "CANCELLED", "SUPERSEDED")
OPEN_REQUEST_STATUSES = ("PENDING", "RETURNED")
REQUEST_LIMIT_PERIODS = ("DAY", "WEEK", "CYCLE")
MUSTER_MARKS = ("IN_OUT", "PRESENT", "HALF_DAY", "ABSENT", "IDLE")
REASON_APPLIES_TO = ATTENDANCE_REQUEST_KINDS + ("REVIEW", "ROSTER_CHANGE", "DECISION", "ON_DUTY")
REASON_CODE_STATUSES = ("ACTIVE", "INACTIVE")
ATTACHMENT_CATEGORIES = ("SUPPORTING_DOCUMENT", "TRAVEL_DOCUMENT")
ATTACHMENT_SENSITIVITIES = ("GENERAL", "MEDICAL")
PERIOD_CLOSE_STATUSES = ("OPEN", "CLOSED", "REOPENED")
TIMESHEET_STATUSES = ("OPEN", "SUBMITTED", "APPROVED", "REJECTED", "REOPENED")
TIMESHEET_PERIOD_KINDS = ("WEEK", "CYCLE")


def _in(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _fk(table: str, column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["company_id", column], [f"{target}.company_id", f"{target}.id"],
                                name=f"fk_{table}_{name}", ondelete=ondelete)


class AttendanceReasonCode(TenantMixin, BaseModel):
    __tablename__ = "attendance_reason_codes"

    code = Column(String(40), nullable=False)
    label = Column(String(80), nullable=False)
    applies_to = Column(ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]"))
    requires_attachment = Column(Boolean, nullable=False, default=False, server_default="false")
    counts_toward_limit = Column(Boolean, nullable=False, default=True, server_default="true")
    sensitive = Column(Boolean, nullable=False, default=False, server_default="false")
    system_only = Column(Boolean, nullable=False, default=False, server_default="false")
    is_seed = Column(Boolean, nullable=False, default=False, server_default="false")
    status = Column(String(10), nullable=False, default="ACTIVE", server_default="ACTIVE")
    row_version = Column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_attendance_reason_codes_company_id_id"),
        UniqueConstraint("company_id", "code", name="uq_attendance_reason_codes_code"),
        CheckConstraint("code ~ '^[A-Z][A-Z0-9_]{1,39}$'", name="ck_attendance_reason_codes_code"),
        CheckConstraint(f"status IN {_in(REASON_CODE_STATUSES)}", name="ck_attendance_reason_codes_status"),
        CheckConstraint(f"applies_to <@ ARRAY{list(REASON_APPLIES_TO)}::text[]",
                        name="ck_attendance_reason_codes_applies_to"),
    )


class AttendanceRequest(TenantMixin, BaseModel):
    __tablename__ = "attendance_requests"

    employee_id = Column(Integer, nullable=False)
    shift_date = Column(Date, nullable=False)
    kind = Column(String(20), nullable=False)
    mode = Column(String(10), nullable=False)
    status = Column(String(12), nullable=False, default="PENDING")
    session_id = Column(Integer, nullable=True)
    payload = Column(JSONB, nullable=False, default=dict)
    base_day_version = Column(Integer, nullable=True)
    base_session_row_version = Column(Integer, nullable=True)
    reason_code = Column(String(40), nullable=False)
    comment = Column(String(1000), nullable=True)
    minutes = Column(Integer, nullable=False, default=0, server_default="0")  # §6.2 minutes of the request
    counts_toward_limit = Column(Boolean, nullable=False, default=True, server_default="true")
    approval_request_id = Column(Integer, nullable=True)
    requested_by_user_id = Column(Integer, nullable=True)
    decided_by_user_id = Column(Integer, nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    first_submitted_on = Column(Date, nullable=True)  # local date of the first submit (§6.4 rolling re-check)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    applied_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    decision_remarks = Column(String(1000), nullable=True)
    decision_reason_code = Column(String(40), nullable=True)
    issues = Column(JSONB, nullable=False, default=list)  # acknowledged warnings (ON_BEHALF over the limit)
    version = Column(Integer, nullable=False, default=1, server_default="1")
    row_version = Column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_attendance_requests_company_id_id"),
        _fk("attendance_requests", "employee_id", "employees", "employee"),
        _fk("attendance_requests", "session_id", "attendance", "session"),
        _fk("attendance_requests", "approval_request_id", "approval_requests", "approval"),
        _fk("attendance_requests", "requested_by_user_id", "users", "requested_by"),
        _fk("attendance_requests", "decided_by_user_id", "users", "decided_by"),
        ForeignKeyConstraint(["company_id", "reason_code"], ["attendance_reason_codes.company_id",
                                                             "attendance_reason_codes.code"],
                             name="fk_attendance_requests_reason", ondelete="RESTRICT"),
        CheckConstraint(f"kind IN {_in(ATTENDANCE_REQUEST_KINDS)}", name="ck_attendance_requests_kind"),
        CheckConstraint(f"mode IN {_in(REQUEST_MODES)}", name="ck_attendance_requests_mode"),
        CheckConstraint(f"status IN {_in(ATTENDANCE_REQUEST_STATUSES)}", name="ck_attendance_requests_status"),
        CheckConstraint("mode <> 'DIRECT' OR status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')",
                        name="ck_attendance_requests_direct"),
        CheckConstraint("kind <> 'STATUS_OVERRIDE' OR mode <> 'SELF'", name="ck_attendance_requests_override"),
        CheckConstraint("status <> 'DRAFT' OR mode = 'SELF'", name="ck_attendance_requests_draft"),
        CheckConstraint(f"kind NOT IN {_in(SESSION_KINDS)} OR session_id IS NOT NULL",
                        name="ck_attendance_requests_session"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_attendance_requests_payload"),
        CheckConstraint("minutes >= 0 AND version >= 1 AND row_version >= 1", name="ck_attendance_requests_counts"),
        Index("ix_attendance_requests_employee_date", "company_id", "employee_id", "shift_date"),
        Index("ix_attendance_requests_open", "company_id", "status",
              postgresql_where=text("status IN ('PENDING', 'RETURNED')")),
        Index("uq_attendance_requests_open", "company_id", "employee_id", "shift_date", "kind",
              text("COALESCE(session_id, 0)"), unique=True,
              postgresql_where=text("status IN ('PENDING', 'RETURNED')")),
    )


class AttendanceAttachment(TenantMixin, BaseModel):
    __tablename__ = "attendance_attachments"

    employee_id = Column(Integer, nullable=False)  # the subject the file is about
    request_id = Column(Integer, nullable=True)  # NULL until linked at submit
    on_duty_request_id = Column(Integer, nullable=True)
    uploaded_by_user_id = Column(Integer, nullable=True)
    file_name = Column(String(200), nullable=False)
    mime = Column(String(60), nullable=False)
    bytes = Column(Integer, nullable=False)
    sha256 = Column(String(64), nullable=False)
    storage_path = Column(Text, nullable=False)
    category = Column(String(24), nullable=False, default="SUPPORTING_DOCUMENT")
    sensitivity = Column(String(10), nullable=False, default="GENERAL")

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_attendance_attachments_company_id_id"),
        _fk("attendance_attachments", "employee_id", "employees", "employee"),
        _fk("attendance_attachments", "request_id", "attendance_requests", "request", "CASCADE"),
        _fk("attendance_attachments", "on_duty_request_id", "on_duty_requests", "on_duty", "CASCADE"),
        _fk("attendance_attachments", "uploaded_by_user_id", "users", "uploaded_by"),
        CheckConstraint(f"category IN {_in(ATTACHMENT_CATEGORIES)}", name="ck_attendance_attachments_category"),
        CheckConstraint(f"sensitivity IN {_in(ATTACHMENT_SENSITIVITIES)}",
                        name="ck_attendance_attachments_sensitivity"),
        CheckConstraint("bytes > 0", name="ck_attendance_attachments_bytes"),
        Index("ix_attendance_attachments_request", "company_id", "request_id"),
    )


class AttendancePeriodClose(TenantMixin, BaseModel):
    __tablename__ = "attendance_period_closes"

    legal_entity_id = Column(Integer, nullable=False)
    branch_id = Column(Integer, nullable=True)
    pay_group_id = Column(Integer, nullable=True)
    cycle_start = Column(Date, nullable=False)
    cycle_end = Column(Date, nullable=False)
    status = Column(String(10), nullable=False, default="OPEN")
    open_exception_count = Column(Integer, nullable=False, default=0, server_default="0")
    closed_by_user_id = Column(Integer, nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    reopened_by_user_id = Column(Integer, nullable=True)
    reopened_at = Column(DateTime(timezone=True), nullable=True)
    reopen_reason = Column(String(500), nullable=True)
    row_version = Column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_attendance_period_closes_company_id_id"),
        _fk("attendance_period_closes", "legal_entity_id", "legal_entities", "legal_entity"),
        _fk("attendance_period_closes", "branch_id", "branches", "branch"),
        _fk("attendance_period_closes", "pay_group_id", "pay_groups", "pay_group"),
        _fk("attendance_period_closes", "closed_by_user_id", "users", "closed_by"),
        _fk("attendance_period_closes", "reopened_by_user_id", "users", "reopened_by"),
        CheckConstraint(f"status IN {_in(PERIOD_CLOSE_STATUSES)}", name="ck_attendance_period_closes_status"),
        CheckConstraint("cycle_end >= cycle_start", name="ck_attendance_period_closes_cycle"),
        CheckConstraint("open_exception_count >= 0", name="ck_attendance_period_closes_count"),
        Index("uq_attendance_period_closes", "company_id", "legal_entity_id", text("COALESCE(branch_id, 0)"),
              text("COALESCE(pay_group_id, 0)"), "cycle_start", unique=True),
    )


class AttendanceTimesheetApproval(TenantMixin, BaseModel):
    __tablename__ = "attendance_timesheet_approvals"

    employee_id = Column(Integer, nullable=False)
    period_kind = Column(String(6), nullable=False, default="WEEK")
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    status = Column(String(10), nullable=False, default="OPEN")
    content_hash = Column(String(64), nullable=True)
    approval_request_id = Column(Integer, nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decided_by_user_id = Column(Integer, nullable=True)
    version = Column(Integer, nullable=False, default=1, server_default="1")
    row_version = Column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_attendance_timesheet_approvals_company_id_id"),
        UniqueConstraint("company_id", "employee_id", "period_kind", "period_start",
                         name="uq_attendance_timesheet_approvals_period"),
        _fk("attendance_timesheet_approvals", "employee_id", "employees", "employee"),
        _fk("attendance_timesheet_approvals", "approval_request_id", "approval_requests", "approval"),
        _fk("attendance_timesheet_approvals", "decided_by_user_id", "users", "decided_by"),
        CheckConstraint(f"status IN {_in(TIMESHEET_STATUSES)}", name="ck_attendance_timesheet_approvals_status"),
        CheckConstraint(f"period_kind IN {_in(TIMESHEET_PERIOD_KINDS)}",
                        name="ck_attendance_timesheet_approvals_kind"),
        CheckConstraint("period_end >= period_start", name="ck_attendance_timesheet_approvals_period_range"),
    )


class ApprovalRoute(TenantMixin, BaseModel):
    """A route of an engine module (interim store until LH-M5a, IG-AS-81): the ACTIVE route with the highest
    priority whose ``criteria`` match the request's attributes is selected at submit; its steps are the
    ``approval_chain_steps`` rows with this ``route_id``. Steps with ``route_id`` NULL are the module's legacy chain."""

    __tablename__ = "approval_routes"

    module = Column(String(20), nullable=False)
    route_key = Column(String(40), nullable=False)
    name = Column(String(100), nullable=False)
    priority = Column(Integer, nullable=False, default=0, server_default="0")
    criteria = Column(JSONB, nullable=False, default=dict)
    status = Column(String(10), nullable=False, default="ACTIVE", server_default="ACTIVE")
    row_version = Column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_approval_routes_company_id_id"),
        UniqueConstraint("company_id", "module", "route_key", name="uq_approval_routes_key"),
        CheckConstraint(f"module IN {APPROVAL_MODULES}", name="ck_approval_routes_module"),
        CheckConstraint("status IN ('ACTIVE', 'INACTIVE')", name="ck_approval_routes_status"),
        CheckConstraint("jsonb_typeof(criteria) = 'object'", name="ck_approval_routes_criteria"),
        CheckConstraint("route_key ~ '^[A-Za-z][A-Za-z0-9_-]{0,39}$'", name="ck_approval_routes_key"),
    )


__all__ = [
    "AttendanceReasonCode", "AttendanceRequest", "AttendanceAttachment", "AttendancePeriodClose",
    "AttendanceTimesheetApproval", "ApprovalRoute",
]

"""Shift requests, open shifts, availability and skill tags (AS-M6; owner 03-shifts/03, availability / tags in
03-shifts/02; spine §3.11, §4.3, §4.4). Migration ``0057_as_m6_shift_requests``.

* ``shift_requests`` — SWAP / DROP / RELEASE / PICKUP / CHANGE filed by an employee on a published entry (PICKUP: on an
  open shift). ``APPROVED`` always means applied (RELEASE: posted to the pool). One open request per entry and one
  open claim per (open shift, claimant) — partial unique indexes.
* ``open_shifts`` — a slot planners post (MANUAL / UNCOVERED) or a RELEASE posts (RELEASED); claimed by PICKUPs.
* ``employee_availability`` — recurring (weekday) or dated windows an employee is UNAVAILABLE / PREFERRED; validation
  reads APPROVED rows (``UNAVAILABLE`` check of 03-shifts/03 §4.2).
* ``skill_tags`` / ``employee_skill_tags`` — tenant tags with optional expiry, read by ``required_tags`` checks.
"""

from sqlalchemy import (
    Boolean, CheckConstraint, Column, Date, DateTime, ForeignKeyConstraint, Index, Integer, SmallInteger, String,
    UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import BaseModel, TenantMixin

SHIFT_REQUEST_KINDS = ("SWAP", "DROP", "RELEASE", "PICKUP", "CHANGE")
SHIFT_REQUEST_STATUSES = ("PENDING_ACCEPTANCE", "PENDING_APPROVAL", "APPROVED", "DECLINED", "REJECTED", "CANCELLED",
                          "EXPIRED", "SUPERSEDED")
OPEN_REQUEST_STATUSES = ("PENDING_ACCEPTANCE", "PENDING_APPROVAL")
OPEN_SHIFT_STATUSES = ("OPEN", "FILLED", "CANCELLED", "EXPIRED")
OPEN_SHIFT_SOURCES = ("MANUAL", "RELEASED", "UNCOVERED")
AVAILABILITY_KINDS = ("UNAVAILABLE", "PREFERRED")
AVAILABILITY_STATUSES = ("PENDING", "APPROVED", "REJECTED", "CANCELLED")


def _fk(table: str, column: str, target: str, name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["company_id", column], [f"{target}.company_id", f"{target}.id"],
                                name=f"fk_{table}_{name}")


class OpenShift(TenantMixin, BaseModel):
    __tablename__ = "open_shifts"

    roster_period_id = Column(Integer, nullable=True)
    team_id = Column(Integer, nullable=True)
    branch_id = Column(Integer, nullable=False)
    site_id = Column(Integer, nullable=True)
    shift_date = Column(Date, nullable=False)
    shift_id = Column(Integer, nullable=True)
    custom_start_time = Column(String(5), nullable=True)
    custom_end_time = Column(String(5), nullable=True)
    required_count = Column(SmallInteger, nullable=False, default=1)
    filled_count = Column(SmallInteger, nullable=False, default=0)
    eligibility = Column(JSONB, nullable=False, default=dict)
    priority_until = Column(DateTime(timezone=True), nullable=True)
    approval_required = Column(Boolean, nullable=False, default=True)
    status = Column(String(10), nullable=False, default="OPEN")
    source = Column(String(10), nullable=False, default="MANUAL")
    released_from_entry_id = Column(Integer, nullable=True)
    released_by_employee_id = Column(Integer, nullable=True)
    posted_at = Column(DateTime(timezone=True), nullable=False)
    filled_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    note = Column(String(500), nullable=True)
    warnings = Column(JSONB, nullable=False, default=list)
    created_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_open_shifts_company_id_id"),
        _fk("open_shifts", "roster_period_id", "roster_periods", "period"),
        _fk("open_shifts", "team_id", "teams", "team"),
        _fk("open_shifts", "branch_id", "branches", "branch"),
        _fk("open_shifts", "site_id", "attendance_sites", "site"),
        _fk("open_shifts", "shift_id", "shifts", "shift"),
        _fk("open_shifts", "released_from_entry_id", "roster_entries", "released_entry"),
        _fk("open_shifts", "released_by_employee_id", "employees", "released_by"),
        CheckConstraint(f"status IN {OPEN_SHIFT_STATUSES}", name="ck_open_shifts_status"),
        CheckConstraint(f"source IN {OPEN_SHIFT_SOURCES}", name="ck_open_shifts_source"),
        CheckConstraint("required_count BETWEEN 1 AND 50", name="ck_open_shifts_required"),
        CheckConstraint("filled_count >= 0 AND filled_count <= required_count", name="ck_open_shifts_filled"),
        CheckConstraint("shift_id IS NOT NULL OR (custom_start_time IS NOT NULL AND custom_end_time IS NOT NULL)",
                        name="ck_open_shifts_times"),
        CheckConstraint("(status = 'FILLED') = (filled_at IS NOT NULL)", name="ck_open_shifts_filled_at"),
        CheckConstraint("source <> 'RELEASED' OR released_from_entry_id IS NOT NULL", name="ck_open_shifts_released"),
        CheckConstraint("row_version >= 1", name="ck_open_shifts_row_version"),
        Index("ix_open_shifts_date", "company_id", "shift_date", "status"),
    )


class ShiftRequest(TenantMixin, BaseModel):
    __tablename__ = "shift_requests"

    kind = Column(String(8), nullable=False)
    status = Column(String(18), nullable=False)
    requester_employee_id = Column(Integer, nullable=False)
    entry_id = Column(Integer, nullable=True)
    target_employee_id = Column(Integer, nullable=True)
    counter_entry_id = Column(Integer, nullable=True)
    open_shift_id = Column(Integer, nullable=True)
    shift_date = Column(Date, nullable=False)
    counter_date = Column(Date, nullable=True)
    requested_change = Column(JSONB(none_as_null=True), nullable=True)
    reason = Column(String(500), nullable=True)
    acknowledged_warnings = Column(JSONB, nullable=False, default=list)
    checks = Column(JSONB, nullable=False, default=list)
    approval_required = Column(Boolean, nullable=False, default=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=False)
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decided_by_user_id = Column(Integer, nullable=True)
    decision_note = Column(String(500), nullable=True)
    applied_publication_id = Column(Integer, nullable=True)
    close_reason = Column(String(40), nullable=True)
    refile_of_request_id = Column(Integer, nullable=True)
    approval_request_id = Column(Integer, nullable=True)  # the engine request (module shift_request; migration 0058)
    consent_ids = Column(JSONB, nullable=False, default=list)  # notice-window consents captured (§7.3)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_shift_requests_company_id_id"),
        _fk("shift_requests", "approval_request_id", "approval_requests", "approval"),
        _fk("shift_requests", "requester_employee_id", "employees", "requester"),
        _fk("shift_requests", "target_employee_id", "employees", "target"),
        _fk("shift_requests", "entry_id", "roster_entries", "entry"),
        _fk("shift_requests", "counter_entry_id", "roster_entries", "counter_entry"),
        _fk("shift_requests", "open_shift_id", "open_shifts", "open_shift"),
        _fk("shift_requests", "applied_publication_id", "roster_publications", "publication"),
        _fk("shift_requests", "decided_by_user_id", "users", "decided_by"),
        _fk("shift_requests", "refile_of_request_id", "shift_requests", "refile_of"),
        CheckConstraint(f"kind IN {SHIFT_REQUEST_KINDS}", name="ck_shift_requests_kind"),
        CheckConstraint(f"status IN {SHIFT_REQUEST_STATUSES}", name="ck_shift_requests_status"),
        CheckConstraint("(kind = 'PICKUP') = (open_shift_id IS NOT NULL AND entry_id IS NULL)",
                        name="ck_shift_requests_pickup"),
        CheckConstraint("kind NOT IN ('SWAP', 'DROP') OR target_employee_id IS NOT NULL",
                        name="ck_shift_requests_peer"),
        CheckConstraint("kind <> 'CHANGE' OR requested_change IS NOT NULL", name="ck_shift_requests_change"),
        CheckConstraint("target_employee_id IS NULL OR target_employee_id <> requester_employee_id",
                        name="ck_shift_requests_not_self"),
        CheckConstraint("status <> 'PENDING_ACCEPTANCE' OR kind IN ('SWAP', 'DROP')",
                        name="ck_shift_requests_acceptance"),
        Index("uq_shift_requests_open_entry", "company_id", "entry_id", unique=True,
              postgresql_where=text("status IN ('PENDING_ACCEPTANCE', 'PENDING_APPROVAL') AND entry_id IS NOT NULL")),
        Index("uq_shift_requests_open_claim", "company_id", "open_shift_id", "requester_employee_id", unique=True,
              postgresql_where=text("status IN ('PENDING_ACCEPTANCE', 'PENDING_APPROVAL') "
                                    "AND open_shift_id IS NOT NULL")),
        Index("ix_shift_requests_requester", "company_id", "requester_employee_id", "status"),
        Index("ix_shift_requests_target", "company_id", "target_employee_id", "status"),
    )


class EmployeeAvailability(TenantMixin, BaseModel):
    __tablename__ = "employee_availability"

    employee_id = Column(Integer, nullable=False)
    kind = Column(String(12), nullable=False, default="UNAVAILABLE")
    weekday = Column(SmallInteger, nullable=True)  # 0 = Monday … 6 = Sunday; NULL = the dated range below
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=True)
    start_time = Column(String(5), nullable=True)  # NULL = the whole day
    end_time = Column(String(5), nullable=True)
    note = Column(String(300), nullable=True)
    status = Column(String(10), nullable=False, default="PENDING")
    decided_by_user_id = Column(Integer, nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_employee_availability_company_id_id"),
        _fk("employee_availability", "employee_id", "employees", "employee"),
        _fk("employee_availability", "decided_by_user_id", "users", "decided_by"),
        CheckConstraint(f"kind IN {AVAILABILITY_KINDS}", name="ck_employee_availability_kind"),
        CheckConstraint(f"status IN {AVAILABILITY_STATUSES}", name="ck_employee_availability_status"),
        CheckConstraint("weekday IS NULL OR weekday BETWEEN 0 AND 6", name="ck_employee_availability_weekday"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="ck_employee_availability_range"),
        CheckConstraint("(start_time IS NULL) = (end_time IS NULL)", name="ck_employee_availability_times"),
        Index("ix_employee_availability_employee", "company_id", "employee_id", "status"),
    )


class SkillTag(TenantMixin, BaseModel):
    __tablename__ = "skill_tags"

    code = Column(String(40), nullable=False)
    name = Column(String(100), nullable=False)
    status = Column(String(10), nullable=False, default="ACTIVE")

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_skill_tags_company_id_id"),
        UniqueConstraint("company_id", "code", name="uq_skill_tags_code"),
        CheckConstraint("status IN ('ACTIVE', 'ARCHIVED')", name="ck_skill_tags_status"),
    )


class EmployeeSkillTag(TenantMixin, BaseModel):
    __tablename__ = "employee_skill_tags"

    employee_id = Column(Integer, nullable=False)
    tag_id = Column(Integer, nullable=False)
    valid_to = Column(Date, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_employee_skill_tags_company_id_id"),
        UniqueConstraint("company_id", "employee_id", "tag_id", name="uq_employee_skill_tags_pair"),
        _fk("employee_skill_tags", "employee_id", "employees", "employee"),
        _fk("employee_skill_tags", "tag_id", "skill_tags", "tag"),
    )

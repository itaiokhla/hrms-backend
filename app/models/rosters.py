"""Rotations and rosters (AS-M5; AS 11 §4.8-§4.9; owner 03-shifts/02).

* ``rotation_patterns`` / ``rotation_pattern_versions``: a cycle of tokens (single row or crew rows) mapped to shifts;
  an ACTIVE version is never edited. The 22 platform templates are code (``rotation_engine.TEMPLATES``).
* ``rotation_assignments``: an employee (or, from X-M1, a CREW team) follows a pattern version from an anchor date.
* ``roster_periods``: the planning unit (team and/or branch, <= 62 days); ``roster_drafts`` are invisible to every
  reader until published.
* ``roster_entries``: the published schedule (one row per employee, date and ``entry_seq``); written only by
  ``shift_rosters.publish``; a change keeps the id, bumps ``version`` and appends ``roster_entry_changes`` (append-only),
  each publication is one ``roster_publications`` row (append-only, ``snapshot_hash``).

``team_id`` references ``teams`` from X-M1 (0047). ``approval_request_id``, ``request_id``, ``consent_id``, ``reason_code``
and ``site_id`` are plain columns until their tables ship (M5a engine, AS-M6, AS-M2/M4).
"""

from sqlalchemy import (
    CHAR, Boolean, CheckConstraint, Column, Date, DateTime, ForeignKeyConstraint, Index, Integer, Numeric, SmallInteger,
    String, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from app.models.base import BaseModel, TenantMixin

PATTERN_STATUSES = ("ACTIVE", "ARCHIVED")
PATTERN_VERSION_STATUSES = ("DRAFT", "ACTIVE", "SUPERSEDED")
ASSIGNMENT_TARGETS = ("EMPLOYEE", "TEAM")
ASSIGNMENT_STATUSES = ("ACTIVE", "ENDED")
ROSTER_PERIOD_STATUSES = ("DRAFT", "PUBLISHED", "AMENDING", "CLOSED", "CANCELLED")
ROSTER_ENTRY_KINDS = ("WORK", "OFF")
ROSTER_ENTRY_SOURCES = ("ROTATION", "MANUAL", "COPY", "IMPORT", "DAY_OVERRIDE", "SUBSTITUTION", "SWAP", "DROP",
                        "PICKUP", "CHANGE", "HOLIDAY_RULE")
DRAFT_ACTIONS = ("UPSERT", "DELETE")
PUBLICATION_KINDS = ("PERIOD", "DIRECT", "REQUEST", "SUBSTITUTION", "ROTATION_AUTO")
CHANGE_KINDS = ("CREATED", "UPDATED", "WITHDRAWN")
INITIATORS = ("EMPLOYER", "EMPLOYEE", "SYSTEM")
WORK_MODES = ("ON_SITE", "REMOTE", "FIELD")
HOLIDAY_MODES = ("HOLIDAY_OFF", "HOLIDAY_SKELETON", "ROSTER_WINS")
WORK_LOCATION_PLAN_MODES = ("NONE", "PLANNED")
WORK_LOCATION_DAY_MODES = WORK_MODES
MAX_PERIOD_DAYS = 62


def _fk(table: str, column: str, target: str, name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["company_id", column], [f"{target}.company_id", f"{target}.id"],
                                name=f"fk_{table}_{name}")


class RotationPattern(TenantMixin, BaseModel):
    __tablename__ = "rotation_patterns"

    code = Column(String(40), nullable=False)
    name = Column(String(100), nullable=False)
    template_key = Column(String(40), nullable=True)
    legal_entity_id = Column(Integer, nullable=True)
    branch_id = Column(Integer, nullable=True)
    status = Column(String(10), nullable=False, default="ACTIVE")
    current_version_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_rotation_patterns_company_id_id"),
        UniqueConstraint("company_id", "code", name="uq_rotation_patterns_code"),
        _fk("rotation_patterns", "legal_entity_id", "legal_entities", "legal_entity"),
        _fk("rotation_patterns", "branch_id", "branches", "branch"),
        ForeignKeyConstraint(["company_id", "current_version_id"],
                             ["rotation_pattern_versions.company_id", "rotation_pattern_versions.id"],
                             name="fk_rotation_patterns_current_version", use_alter=True),
        CheckConstraint(f"status IN {PATTERN_STATUSES}", name="ck_rotation_patterns_status"),
        CheckConstraint("row_version >= 1", name="ck_rotation_patterns_row_version"),
    )


class RotationPatternVersion(TenantMixin, BaseModel):
    __tablename__ = "rotation_pattern_versions"

    pattern_id = Column(Integer, nullable=False, index=True)
    version_no = Column(Integer, nullable=False)
    status = Column(String(12), nullable=False, default="DRAFT")
    cycle_days = Column(SmallInteger, nullable=False)
    tokens = Column(String(366), nullable=True)
    crew_rows = Column(JSONB(none_as_null=True), nullable=True)
    crew_offsets = Column(JSONB(none_as_null=True), nullable=True)
    token_map = Column(JSONB, nullable=False)
    coverage = Column(JSONB(none_as_null=True), nullable=True)
    week_anchor_weekday = Column(SmallInteger, nullable=True)
    lints_muted = Column(ARRAY(Text), nullable=False, default=list, server_default=text("'{}'"))
    content_hash = Column(CHAR(64), nullable=False)
    activated_at = Column(DateTime(timezone=True), nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_rotation_pattern_versions_company_id_id"),
        UniqueConstraint("company_id", "pattern_id", "version_no", name="uq_rotation_pattern_versions_version"),
        _fk("rotation_pattern_versions", "pattern_id", "rotation_patterns", "pattern"),
        CheckConstraint(f"status IN {PATTERN_VERSION_STATUSES}", name="ck_rotation_pattern_versions_status"),
        CheckConstraint("cycle_days BETWEEN 1 AND 366", name="ck_rotation_pattern_versions_cycle"),
        CheckConstraint("tokens IS NULL OR length(tokens) = cycle_days", name="ck_rotation_pattern_versions_tokens"),
        CheckConstraint("num_nonnulls(tokens, crew_rows) = 1", name="ck_rotation_pattern_versions_shape"),
        CheckConstraint("week_anchor_weekday IS NULL OR week_anchor_weekday BETWEEN 0 AND 6",
                        name="ck_rotation_pattern_versions_weekday"),
        CheckConstraint("jsonb_typeof(token_map) = 'object'", name="ck_rotation_pattern_versions_token_map"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_rotation_pattern_versions_hash"),
        CheckConstraint("row_version >= 1", name="ck_rotation_pattern_versions_row_version"),
    )


class RotationAssignment(TenantMixin, BaseModel):
    __tablename__ = "rotation_assignments"

    pattern_version_id = Column(Integer, nullable=False, index=True)
    target_kind = Column(String(8), nullable=False, default="EMPLOYEE")
    employee_id = Column(Integer, nullable=True, index=True)
    team_id = Column(Integer, nullable=True)
    crew_code = Column(String(8), nullable=True)
    offset_days = Column(SmallInteger, nullable=False, default=0)
    anchor_date = Column(Date, nullable=False)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    auto_publish = Column(Boolean, nullable=False, default=False)
    status = Column(String(8), nullable=False, default="ACTIVE")
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_rotation_assignments_company_id_id"),
        _fk("rotation_assignments", "pattern_version_id", "rotation_pattern_versions", "version"),
        _fk("rotation_assignments", "employee_id", "employees", "employee"),
        _fk("rotation_assignments", "team_id", "teams", "team"),  # X-M1 (0047)
        CheckConstraint(f"target_kind IN {ASSIGNMENT_TARGETS}", name="ck_rotation_assignments_target_kind"),
        CheckConstraint("(target_kind = 'EMPLOYEE') = (employee_id IS NOT NULL AND team_id IS NULL)",
                        name="ck_rotation_assignments_target"),
        CheckConstraint("offset_days >= 0", name="ck_rotation_assignments_offset"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_rotation_assignments_dates"),
        CheckConstraint(f"status IN {ASSIGNMENT_STATUSES}", name="ck_rotation_assignments_status"),
        CheckConstraint("row_version >= 1", name="ck_rotation_assignments_row_version"),
        Index("ix_rotation_assignments_status", "company_id", "status", "effective_from"),
    )


class RosterPeriod(TenantMixin, BaseModel):
    __tablename__ = "roster_periods"

    code = Column(String(40), nullable=False)
    name = Column(String(100), nullable=False)
    team_id = Column(Integer, nullable=True)
    branch_id = Column(Integer, nullable=True)
    department_ids = Column(ARRAY(Integer), nullable=True)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    status = Column(String(10), nullable=False, default="DRAFT")
    publish_due_at = Column(DateTime(timezone=True), nullable=True)
    last_published_at = Column(DateTime(timezone=True), nullable=True)
    last_publication_id = Column(Integer, nullable=True)
    requires_approval = Column(Boolean, nullable=False, default=False)
    approval_request_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_roster_periods_company_id_id"),
        UniqueConstraint("company_id", "code", name="uq_roster_periods_code"),
        _fk("roster_periods", "branch_id", "branches", "branch"),
        _fk("roster_periods", "team_id", "teams", "team"),  # X-M1 (0047)
        ForeignKeyConstraint(["company_id", "last_publication_id"],
                             ["roster_publications.company_id", "roster_publications.id"],
                             name="fk_roster_periods_last_publication", use_alter=True),
        CheckConstraint("end_date >= start_date AND end_date - start_date <= 61", name="ck_roster_periods_dates"),
        CheckConstraint(f"status IN {ROSTER_PERIOD_STATUSES}", name="ck_roster_periods_status"),
        CheckConstraint("team_id IS NOT NULL OR branch_id IS NOT NULL", name="ck_roster_periods_owner"),
        CheckConstraint("row_version >= 1", name="ck_roster_periods_row_version"),
    )


class RosterDraft(TenantMixin, BaseModel):
    __tablename__ = "roster_drafts"

    roster_period_id = Column(Integer, nullable=False, index=True)
    employee_id = Column(Integer, nullable=False)
    shift_date = Column(Date, nullable=False)
    entry_seq = Column(SmallInteger, nullable=False, default=1)
    action = Column(String(6), nullable=False, default="UPSERT")
    kind = Column(String(6), nullable=True)
    shift_id = Column(Integer, nullable=True)
    custom_start_time = Column(String(5), nullable=True)
    custom_end_time = Column(String(5), nullable=True)
    custom_segments = Column(JSONB(none_as_null=True), nullable=True)
    site_id = Column(Integer, nullable=True)
    planned_work_mode = Column(String(10), nullable=True)
    planned_shift_id = Column(Integer, nullable=True)
    source = Column(String(14), nullable=False, default="MANUAL")
    base_entry_id = Column(Integer, nullable=True)
    base_entry_version = Column(Integer, nullable=True)
    note = Column(String(500), nullable=True)
    reason = Column(String(500), nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_roster_drafts_company_id_id"),
        UniqueConstraint("company_id", "roster_period_id", "employee_id", "shift_date", "entry_seq",
                         name="uq_roster_drafts_cell"),
        _fk("roster_drafts", "roster_period_id", "roster_periods", "period"),
        _fk("roster_drafts", "employee_id", "employees", "employee"),
        _fk("roster_drafts", "shift_id", "shifts", "shift"),
        _fk("roster_drafts", "planned_shift_id", "shifts", "planned_shift"),
        _fk("roster_drafts", "base_entry_id", "roster_entries", "base_entry"),
        CheckConstraint("entry_seq BETWEEN 1 AND 3", name="ck_roster_drafts_seq"),
        CheckConstraint(f"action IN {DRAFT_ACTIONS}", name="ck_roster_drafts_action"),
        CheckConstraint(f"kind IS NULL OR kind IN {ROSTER_ENTRY_KINDS}", name="ck_roster_drafts_kind"),
        CheckConstraint("action = 'DELETE' OR kind IS NOT NULL", name="ck_roster_drafts_upsert_kind"),
        CheckConstraint(f"planned_work_mode IS NULL OR planned_work_mode IN {WORK_MODES}",
                        name="ck_roster_drafts_work_mode"),
        CheckConstraint("planned_shift_id IS NULL OR (kind = 'OFF' AND source = 'HOLIDAY_RULE')",
                        name="ck_roster_drafts_planned_shift"),
        CheckConstraint(f"source IN {ROSTER_ENTRY_SOURCES}", name="ck_roster_drafts_source"),
        CheckConstraint("row_version >= 1", name="ck_roster_drafts_row_version"),
    )


class RosterPublication(TenantMixin, BaseModel):
    __tablename__ = "roster_publications"

    roster_period_id = Column(Integer, nullable=True, index=True)
    kind = Column(String(14), nullable=False)
    scope = Column(JSONB, nullable=False)
    published_by_user_id = Column(Integer, nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=False)
    entries_created = Column(Integer, nullable=False, default=0)
    entries_updated = Column(Integer, nullable=False, default=0)
    entries_withdrawn = Column(Integer, nullable=False, default=0)
    snapshot_hash = Column(CHAR(64), nullable=False)
    min_notice_hours = Column(Numeric(8, 2), nullable=True)
    warnings = Column(JSONB, nullable=False, default=list)
    approval_request_id = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_roster_publications_company_id_id"),
        _fk("roster_publications", "roster_period_id", "roster_periods", "period"),
        _fk("roster_publications", "published_by_user_id", "users", "published_by"),
        CheckConstraint(f"kind IN {PUBLICATION_KINDS}", name="ck_roster_publications_kind"),
        CheckConstraint("entries_created >= 0 AND entries_updated >= 0 AND entries_withdrawn >= 0",
                        name="ck_roster_publications_counts"),
        CheckConstraint("snapshot_hash ~ '^[0-9a-f]{64}$'", name="ck_roster_publications_hash"),
    )


class RosterEntry(TenantMixin, BaseModel):
    __tablename__ = "roster_entries"

    employee_id = Column(Integer, nullable=False)
    shift_date = Column(Date, nullable=False)
    entry_seq = Column(SmallInteger, nullable=False, default=1)
    kind = Column(String(6), nullable=False)
    shift_id = Column(Integer, nullable=True)
    shift_version_id = Column(Integer, nullable=True)
    custom_start_time = Column(String(5), nullable=True)
    custom_end_time = Column(String(5), nullable=True)
    custom_segments = Column(JSONB(none_as_null=True), nullable=True)
    site_id = Column(Integer, nullable=True)
    planned_work_mode = Column(String(10), nullable=True)
    planned_shift_id = Column(Integer, nullable=True)
    source = Column(String(14), nullable=False)
    roster_period_id = Column(Integer, nullable=True)
    publication_id = Column(Integer, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    published_at = Column(DateTime(timezone=True), nullable=False)
    published_by_user_id = Column(Integer, nullable=True)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    withdrawn_at = Column(DateTime(timezone=True), nullable=True)
    pair_id = Column(UUID(as_uuid=True), nullable=True)
    request_id = Column(Integer, nullable=True)
    team_id = Column(Integer, nullable=True)
    note = Column(String(500), nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_roster_entries_company_id_id"),
        _fk("roster_entries", "employee_id", "employees", "employee"),
        _fk("roster_entries", "team_id", "teams", "team"),  # X-M1 (0047)
        _fk("roster_entries", "shift_id", "shifts", "shift"),
        _fk("roster_entries", "shift_version_id", "shift_versions", "shift_version"),
        _fk("roster_entries", "planned_shift_id", "shifts", "planned_shift"),
        _fk("roster_entries", "roster_period_id", "roster_periods", "period"),
        _fk("roster_entries", "publication_id", "roster_publications", "publication"),
        # AS-M6 (0057). use_alter: roster_entries -> shift_requests -> open_shifts -> roster_entries is a cycle.
        ForeignKeyConstraint(["company_id", "request_id"], ["shift_requests.company_id", "shift_requests.id"],
                             name="fk_roster_entries_request", use_alter=True),
        CheckConstraint("entry_seq BETWEEN 1 AND 3", name="ck_roster_entries_seq"),
        CheckConstraint(f"kind IN {ROSTER_ENTRY_KINDS}", name="ck_roster_entries_kind"),
        CheckConstraint("kind = 'WORK' OR (shift_id IS NULL AND custom_start_time IS NULL)",
                        name="ck_roster_entries_off"),
        CheckConstraint("kind = 'OFF' OR shift_id IS NOT NULL OR custom_start_time IS NOT NULL",
                        name="ck_roster_entries_work"),
        CheckConstraint(f"planned_work_mode IS NULL OR planned_work_mode IN {WORK_MODES}",
                        name="ck_roster_entries_work_mode"),
        CheckConstraint(f"source IN {ROSTER_ENTRY_SOURCES}", name="ck_roster_entries_source"),
        CheckConstraint("version >= 1", name="ck_roster_entries_version"),
        CheckConstraint("row_version >= 1", name="ck_roster_entries_row_version"),
        Index("uq_roster_entries_active", "company_id", "employee_id", "shift_date", "entry_seq", unique=True,
              postgresql_where=text("withdrawn_at IS NULL")),
        Index("ix_roster_entries_date_team", "company_id", "shift_date", "team_id"),
    )


class RosterEntryChange(TenantMixin, BaseModel):
    __tablename__ = "roster_entry_changes"

    publication_id = Column(Integer, nullable=False, index=True)
    entry_id = Column(Integer, nullable=True)
    employee_id = Column(Integer, nullable=False)
    shift_date = Column(Date, nullable=False)
    entry_seq = Column(SmallInteger, nullable=False)
    change_kind = Column(String(10), nullable=False)
    before = Column(JSONB(none_as_null=True), nullable=True)
    after = Column(JSONB(none_as_null=True), nullable=True)
    notice_hours = Column(Numeric(8, 2), nullable=True)
    initiated_by = Column(String(8), nullable=False, default="EMPLOYER")
    reason_code = Column(String(40), nullable=True)
    request_id = Column(Integer, nullable=True)
    consent_id = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_roster_entry_changes_company_id_id"),
        _fk("roster_entry_changes", "publication_id", "roster_publications", "publication"),
        _fk("roster_entry_changes", "entry_id", "roster_entries", "entry"),
        _fk("roster_entry_changes", "employee_id", "employees", "employee"),
        CheckConstraint(f"change_kind IN {CHANGE_KINDS}", name="ck_roster_entry_changes_kind"),
        CheckConstraint("change_kind <> 'CREATED' OR before IS NULL", name="ck_roster_entry_changes_created"),
        CheckConstraint("change_kind <> 'WITHDRAWN' OR after IS NULL", name="ck_roster_entry_changes_withdrawn"),
        CheckConstraint(f"initiated_by IN {INITIATORS}", name="ck_roster_entry_changes_initiator"),
        Index("ix_roster_entry_changes_employee_date", "company_id", "employee_id", "shift_date"),
    )


CONSENT_CONFLICT_STATUSES = ("OPEN", "RESOLVED")


class RosterConsentConflict(TenantMixin, BaseModel):
    """Pending item ``roster_consent_conflict`` and grid badge of a published future entry that a consent change made
    invalid (EC AS-ROS-06; 03-shifts/02 §6.5): written by job ``roster.revalidate_employee``, RESOLVED when a later
    revalidation finds the entry valid again or it is withdrawn / changed. The entry itself is never withdrawn
    automatically. Table: as_m2_sites_consents."""

    __tablename__ = "roster_consent_conflicts"

    employee_id = Column(Integer, nullable=False)
    roster_entry_id = Column(Integer, nullable=False)
    roster_period_id = Column(Integer, nullable=True)
    shift_date = Column(Date, nullable=False)
    purpose = Column(String(28), nullable=False)
    rule_key = Column(String(40), nullable=False)
    consent_id = Column(Integer, nullable=True)
    status = Column(String(10), nullable=False, default="OPEN", server_default="OPEN")
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_roster_consent_conflicts_company_id_id"),
        _fk("roster_consent_conflicts", "employee_id", "employees", "employee"),
        _fk("roster_consent_conflicts", "roster_entry_id", "roster_entries", "entry"),
        _fk("roster_consent_conflicts", "roster_period_id", "roster_periods", "period"),
        _fk("roster_consent_conflicts", "consent_id", "attendance_consents", "consent"),
        CheckConstraint("purpose IN ('BIOMETRIC_FACE', 'BIOMETRIC_DEVICE', 'PUNCH_PHOTO', 'LOCATION_AT_PUNCH', "
                        "'LOCATION_TRACKING', 'NIGHT_WORK', 'OVERTIME', 'COMPRESSED_WEEK', 'REST_WAIVER', "
                        "'SCHEDULE_CHANGE_ACCEPTED', 'WEEKLY_HOURS_OPT_OUT')", name="ck_roster_consent_conflicts_purpose"),
        CheckConstraint(f"status IN {CONSENT_CONFLICT_STATUSES}", name="ck_roster_consent_conflicts_status"),
        CheckConstraint("(status = 'RESOLVED') = (resolved_at IS NOT NULL)", name="ck_roster_consent_conflicts_resolved"),
        Index("uq_roster_consent_conflicts_open", "company_id", "roster_entry_id", "purpose", unique=True,
              postgresql_where=text("status = 'OPEN'")),
        Index("ix_roster_consent_conflicts_period", "company_id", "roster_period_id", "status"),
    )

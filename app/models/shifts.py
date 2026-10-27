"""Shift versions and shift assignment rules (AS-M1; AS 11 §4.4-§4.5, spine §3.4; owner 03-shifts/01).

* ``shift_versions``: the effective-dated content of a shift (times, segments, break rules, flexible core, windows,
  anchor zone). Versions of one shift tile from version 1 without gaps or overlaps (deferred constraint trigger
  ``shift_versions_tiling``, immediate trigger ``shift_versions_no_overlap``); once a version starts on the owner's
  local today it is immutable except one shortening of ``effective_to`` and a void (trigger ``shift_versions_guard``).
  ``content_hash`` = sha256 of :func:`version_content` (no ids, no reason).
* ``shift_assignment_rules``: criteria rows giving matching employees a shift for a date range; **higher priority
  wins** (D-AS-85). Read by ``app.services.shift_resolution`` only.

The ``shifts`` legacy columns (times, ``duration_hours``, ``unpaid_break_minutes``, ``is_flexible``) are a mirror of
the version valid on the owner's local today (``current_version_id``), written only by ``shift_catalogue``.
"""

import hashlib
import json
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import (
    CHAR, Boolean, CheckConstraint, Column, Date, DateTime, ForeignKeyConstraint, Index, Integer, Numeric, SmallInteger,
    String, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import BaseModel, TenantMixin
from app.services.shift_engine import BREAK_KINDS, BREAK_MODES, SEGMENT_KINDS, SHIFT_KINDS  # noqa: F401  re-exported

SHIFT_STATUSES = ("ACTIVE", "ARCHIVED")
RULE_STATUSES = ("ACTIVE", "INACTIVE")
COLOR_TOKENS = tuple(f"shift-{n}" for n in range(1, 13))
SHIFT_SOURCES = ("ROSTER", "RULE", "PLACEMENT", "NONE")
FIRST_EFFECTIVE_DATE = "2000-01-01"
DEFAULT_RULE_PRIORITY = 500

# Business columns of a version, in hash order (03-shifts/01 §3.2 content_hash: no ids, no reason).
VERSION_CONTENT_FIELDS = (
    "start_time", "end_time", "crosses_midnight", "duration_hours", "unpaid_break_minutes", "half_split_time",
    "segments", "break_rules", "core_start_time", "core_end_time", "expected_net_minutes", "grace_in_minutes",
    "grace_out_minutes", "early_in_window_minutes", "late_out_window_minutes", "anchor_timezone", "safety_critical",
    "outdoor", "long_duty",
)


def version_content(values: Mapping[str, Any]) -> dict:
    """Canonical content of a version for ``content_hash`` (decimals as strings with two places)."""
    content = {}
    for field in VERSION_CONTENT_FIELDS:
        value = values.get(field)
        if isinstance(value, Decimal):
            value = f"{value:.2f}"
        content[field] = value
    return content


def version_hash(values: Mapping[str, Any]) -> str:
    raw = json.dumps(version_content(values), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


class ShiftVersion(TenantMixin, BaseModel):
    __tablename__ = "shift_versions"

    shift_id = Column(Integer, nullable=False, index=True)
    version_no = Column(Integer, nullable=False)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    start_time = Column(String(5), nullable=True)
    end_time = Column(String(5), nullable=True)
    crosses_midnight = Column(Boolean, nullable=False, default=False)
    duration_hours = Column(Numeric(4, 2), nullable=True)
    unpaid_break_minutes = Column(SmallInteger, nullable=True)
    half_split_time = Column(String(5), nullable=True)
    segments = Column(JSONB, nullable=False, default=list)
    break_rules = Column(JSONB, nullable=False, default=list)
    core_start_time = Column(String(5), nullable=True)
    core_end_time = Column(String(5), nullable=True)
    expected_net_minutes = Column(SmallInteger, nullable=True)
    grace_in_minutes = Column(SmallInteger, nullable=True)
    grace_out_minutes = Column(SmallInteger, nullable=True)
    early_in_window_minutes = Column(SmallInteger, nullable=False, default=120)
    late_out_window_minutes = Column(SmallInteger, nullable=False, default=240)
    anchor_timezone = Column(String(64), nullable=True)
    safety_critical = Column(Boolean, nullable=False, default=False)
    outdoor = Column(Boolean, nullable=False, default=False)
    long_duty = Column(Boolean, nullable=False, default=False)
    content_hash = Column(CHAR(64), nullable=False)
    reason = Column(String(500), nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    voided_at = Column(DateTime(timezone=True), nullable=True)
    voided_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_shift_versions_company_id_id"),
        UniqueConstraint("company_id", "shift_id", "version_no", name="uq_shift_versions_version"),
        ForeignKeyConstraint(["company_id", "shift_id"], ["shifts.company_id", "shifts.id"],
                             name="fk_shift_versions_shift"),
        ForeignKeyConstraint(["company_id", "created_by_user_id"], ["users.company_id", "users.id"],
                             name="fk_shift_versions_created_by"),
        ForeignKeyConstraint(["company_id", "voided_by_user_id"], ["users.company_id", "users.id"],
                             name="fk_shift_versions_voided_by"),
        CheckConstraint("version_no >= 1", name="ck_shift_versions_version_no"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_shift_versions_dates"),
        CheckConstraint("start_time IS NULL OR end_time IS NULL OR crosses_midnight = (end_time <= start_time)",
                        name="ck_shift_versions_crosses_midnight"),
        CheckConstraint("duration_hours IS NULL OR (duration_hours > 0 AND duration_hours <= 24)",
                        name="ck_shift_versions_duration"),
        CheckConstraint("unpaid_break_minutes IS NULL OR unpaid_break_minutes BETWEEN 0 AND 1440",
                        name="ck_shift_versions_unpaid_break"),
        CheckConstraint("jsonb_typeof(segments) = 'array' AND jsonb_array_length(segments) <= 4",
                        name="ck_shift_versions_segments"),
        CheckConstraint("jsonb_typeof(break_rules) = 'array' AND jsonb_array_length(break_rules) <= 4",
                        name="ck_shift_versions_break_rules"),
        CheckConstraint("expected_net_minutes IS NULL OR expected_net_minutes BETWEEN 1 AND 1440",
                        name="ck_shift_versions_expected_net"),
        CheckConstraint("(grace_in_minutes IS NULL OR grace_in_minutes BETWEEN 0 AND 240) AND "
                        "(grace_out_minutes IS NULL OR grace_out_minutes BETWEEN 0 AND 240)",
                        name="ck_shift_versions_grace"),
        CheckConstraint("early_in_window_minutes BETWEEN 0 AND 720 AND late_out_window_minutes BETWEEN 0 AND 720",
                        name="ck_shift_versions_windows"),
        CheckConstraint("NOT long_duty OR start_time = end_time", name="ck_shift_versions_long_duty"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_shift_versions_hash"),
        CheckConstraint("(voided_at IS NULL) = (voided_by_user_id IS NULL)", name="ck_shift_versions_void"),
        Index("ix_shift_versions_shift_dates", "company_id", "shift_id", "effective_from"),
    )


class ShiftAssignmentRule(TenantMixin, BaseModel):
    __tablename__ = "shift_assignment_rules"

    name = Column(String(100), nullable=False)
    shift_id = Column(Integer, nullable=False, index=True)
    criteria = Column(JSONB, nullable=False, default=dict)
    priority = Column(Integer, nullable=False, default=DEFAULT_RULE_PRIORITY)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    legal_entity_id = Column(Integer, nullable=True)
    status = Column(String(10), nullable=False, default="ACTIVE")
    reason = Column(String(500), nullable=True)
    replaces_rule_id = Column(Integer, nullable=True)
    # A team whose members were copied into employee_ids before X-M1 ships team criteria (SP-AS-235); the FK to
    # teams arrives with x_m1_teams.
    team_id_snapshot = Column(Integer, nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_shift_assignment_rules_company_id_id"),
        ForeignKeyConstraint(["company_id", "shift_id"], ["shifts.company_id", "shifts.id"],
                             name="fk_shift_assignment_rules_shift"),
        ForeignKeyConstraint(["company_id", "legal_entity_id"], ["legal_entities.company_id", "legal_entities.id"],
                             name="fk_shift_assignment_rules_legal_entity"),
        ForeignKeyConstraint(["company_id", "replaces_rule_id"],
                             ["shift_assignment_rules.company_id", "shift_assignment_rules.id"],
                             name="fk_shift_assignment_rules_replaces"),
        ForeignKeyConstraint(["company_id", "created_by_user_id"], ["users.company_id", "users.id"],
                             name="fk_shift_assignment_rules_created_by"),
        CheckConstraint(f"status IN {RULE_STATUSES}", name="ck_shift_assignment_rules_status"),
        CheckConstraint("priority >= 0", name="ck_shift_assignment_rules_priority"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from",
                        name="ck_shift_assignment_rules_dates"),
        CheckConstraint("jsonb_typeof(criteria) = 'object'", name="ck_shift_assignment_rules_criteria"),
        CheckConstraint("char_length(btrim(name)) BETWEEN 1 AND 100", name="ck_shift_assignment_rules_name"),
        CheckConstraint("row_version >= 1", name="ck_shift_assignment_rules_row_version"),
        Index("ix_shift_assignment_rules_status", "company_id", "status", "effective_from"),
    )

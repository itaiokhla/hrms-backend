"""Payroll organisation dimensions (02-multi-level-company §10-§13, §18; 3.4 §3 and §8).

A pay group belongs to one legal entity and fixes pay frequency, currency and calendar rules;
grades, cost centers, employment types and designations classify placements; work-week patterns
and holiday calendars decide which days are scheduled working days. Employee assignments,
branches, legal entities and holidays point at them (app.models.org, app.models.holiday).
"""

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Column,
    Date,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    literal,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import aliased

from app.models.base import BaseModel, TenantMixin
from app.models.org import ORG_STATUSES

PAY_FREQUENCIES = ("MONTHLY", "SEMI_MONTHLY", "BI_WEEKLY", "WEEKLY", "DAILY")
WORKER_CLASSES = ("EMPLOYEE", "CONTRACTOR", "INTERN", "APPRENTICE")
# Bound for walking holiday-calendar inheritance (the database rejects cycles).
MAX_CALENDAR_DEPTH = 64
EMPLOYEE_OBSERVANCE = ("NONE", "NEXT_WORKING_DAY", "OBSERVED_IF_OFF")


def _code_name_status(table: str):
    return (
        UniqueConstraint("company_id", "id", name=f"uq_{table}_company_id_id"),
        UniqueConstraint("company_id", "code", name=f"uq_{table}_company_code"),
        CheckConstraint(f"status IN {ORG_STATUSES}", name=f"ck_{table}_status"),
    )


class PayGroup(TenantMixin, BaseModel):
    """Employees paid together: one legal entity, one frequency, one currency (02 §12).

    Runs are created per pay group and period. `calendar_rules` holds the pay-calendar keys of
    3.1 §5 (for example {"monthly_start_day": 1, "pay_day_rule": "LAST_WORKING_DAY"}).
    """

    __tablename__ = "pay_groups"

    legal_entity_id = Column(Integer, nullable=False, index=True)
    code = Column(String(30), nullable=False)
    name = Column(String(100), nullable=False)
    currency = Column(CHAR(3), nullable=False)
    pay_frequency = Column(String(15), nullable=False)
    calendar_rules = Column(JSONB, nullable=False, default=dict)
    country_pack_code = Column(String(10), nullable=True)
    status = Column(String(10), nullable=False, default="ACTIVE")
    description = Column(String(500), nullable=True)

    __table_args__ = (
        *_code_name_status("pay_groups"),
        # Referenced by employee_assignments together with legal_entity_id, so a placement's pay
        # group always belongs to the placement's legal entity, even if the pay group is edited.
        UniqueConstraint("company_id", "legal_entity_id", "id", name="uq_pay_groups_entity_id"),
        ForeignKeyConstraint(
            ["company_id", "legal_entity_id"], ["legal_entities.company_id", "legal_entities.id"],
            name="fk_pay_groups_legal_entity",
        ),
        CheckConstraint(f"pay_frequency IN {PAY_FREQUENCIES}", name="ck_pay_groups_frequency"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_pay_groups_currency"),
        CheckConstraint("jsonb_typeof(calendar_rules) = 'object'", name="ck_pay_groups_calendar_rules"),
    )


class Grade(TenantMixin, BaseModel):
    """Grade or band with optional salary ranges per currency (02 §12).

    `salary_ranges` is a list of {"currency": "INR", "min": "300000.00", "max": "600000.00"}
    (optionally "mid"), amounts as decimal strings, one entry per currency; the database rejects
    anything else.
    """

    __tablename__ = "grades"

    code = Column(String(30), nullable=False)
    name = Column(String(100), nullable=False)
    rank = Column(Integer, nullable=True)
    salary_ranges = Column(JSONB, nullable=False, default=list)
    status = Column(String(10), nullable=False, default="ACTIVE")

    __table_args__ = (
        *_code_name_status("grades"),
        CheckConstraint("salary_ranges_valid(salary_ranges)", name="ck_grades_salary_ranges"),
    )


class CostCenter(TenantMixin, BaseModel):
    """Accounting cost center, tenant-wide or limited to one legal entity (02 §12)."""

    __tablename__ = "cost_centers"

    code = Column(String(30), nullable=False)
    name = Column(String(100), nullable=False)
    legal_entity_id = Column(Integer, nullable=True, index=True)
    status = Column(String(10), nullable=False, default="ACTIVE")

    __table_args__ = (
        *_code_name_status("cost_centers"),
        ForeignKeyConstraint(
            ["company_id", "legal_entity_id"], ["legal_entities.company_id", "legal_entities.id"],
            name="fk_cost_centers_legal_entity",
        ),
    )


class EmploymentType(TenantMixin, BaseModel):
    """Permanent, fixed-term, contractor...; the worker class decides payslip vs invoice (02 §12)."""

    __tablename__ = "employment_types"

    code = Column(String(30), nullable=False)
    name = Column(String(100), nullable=False)
    worker_class = Column(String(12), nullable=False)
    status = Column(String(10), nullable=False, default="ACTIVE")

    __table_args__ = (
        *_code_name_status("employment_types"),
        CheckConstraint(f"worker_class IN {WORKER_CLASSES}", name="ck_employment_types_worker_class"),
    )


class Designation(TenantMixin, BaseModel):
    """Job title shown on documents, optionally tied to a grade (02 §12)."""

    __tablename__ = "designations"

    code = Column(String(30), nullable=False)
    name = Column(String(100), nullable=False)
    grade_id = Column(Integer, nullable=True)
    status = Column(String(10), nullable=False, default="ACTIVE")

    __table_args__ = (
        *_code_name_status("designations"),
        ForeignKeyConstraint(["company_id", "grade_id"], ["grades.company_id", "grades.id"], name="fk_designations_grade"),
    )


class WorkWeekPattern(TenantMixin, BaseModel):
    """Weekly offs (3.4 §3; LH 04-holiday/03 §3.1): a WEEKLY or CYCLE pattern.

    The rule columns are a **mirror** of the version valid on the company-local today
    (`work_week_pattern_versions` is the source of truth, LH V3-014); only legacy readers and lists read them.
    WEEKLY: `weekly_off_days` (distinct weekday numbers, Monday = 0 ... Sunday = 6, at most six) and
    `nth_weekday_offs` such as the 2nd and 4th Saturday [{"weekday": 5, "nth": [2, 4]}] (nth 1-5 or -1 = last).
    CYCLE: `cycle_length_days` (2-56), `cycle_off_days` (cycle days 0..N-1) and `anchor_date` (cycle day 0).
    `half_day_offs` and `rest_days` apply to both kinds. The database rejects any other shape.
    `legal_entity_id` NULL = tenant-wide pattern; otherwise owned by (and usable in) that entity only.
    """

    __tablename__ = "work_week_patterns"

    code = Column(String(30), nullable=False)
    name = Column(String(100), nullable=False)
    name_i18n = Column(JSONB, nullable=False, default=dict)
    kind = Column(String(6), nullable=False, default="WEEKLY")
    weekly_off_days = Column(JSONB, nullable=False)
    nth_weekday_offs = Column(JSONB, nullable=False, default=list)
    half_day_offs = Column(JSONB, nullable=False, default=list)
    cycle_length_days = Column(Integer, nullable=True)
    cycle_off_days = Column(JSONB(none_as_null=True), nullable=True)
    anchor_date = Column(Date, nullable=True)
    rest_days = Column(JSONB(none_as_null=True), nullable=True)
    legal_entity_id = Column(Integer, nullable=True, index=True)
    status = Column(String(10), nullable=False, default="ACTIVE")
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        *_code_name_status("work_week_patterns"),
        ForeignKeyConstraint(["company_id", "legal_entity_id"], ["legal_entities.company_id", "legal_entities.id"],
                             name="fk_work_week_patterns_legal_entity", use_alter=True),
        CheckConstraint("kind IN ('WEEKLY', 'CYCLE')", name="ck_work_week_patterns_kind"),
        CheckConstraint("row_version >= 1", name="ck_work_week_patterns_row_version"),
        CheckConstraint(
            "work_week_rules_valid(kind, weekly_off_days, nth_weekday_offs, half_day_offs, cycle_length_days, "
            "cycle_off_days, anchor_date, rest_days)", name="ck_work_week_patterns_rules"),
    )


class WorkWeekPatternVersion(TenantMixin, BaseModel):
    """One effective-dated rule set of a pattern (LH 04-holiday/03 §5.1): the source of truth for day facts.

    Versions of one pattern tile without overlap from version 1 (trigger); the last one is open-ended.
    `rules` is the canonical engine JSON (`app.payroll.workweek.WorkWeekPattern.to_json`), `content_hash` its
    sha256. A version that has started is never changed except its `effective_to` (set when a later one is posted).
    """

    __tablename__ = "work_week_pattern_versions"

    pattern_id = Column(Integer, nullable=False, index=True)
    version = Column(Integer, nullable=False)
    rules = Column(JSONB, nullable=False)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    reason = Column(String(500), nullable=True)
    content_hash = Column(CHAR(64), nullable=False)
    created_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_work_week_pattern_versions_company_id_id"),
        UniqueConstraint("company_id", "pattern_id", "version", name="uq_work_week_pattern_versions_version"),
        ForeignKeyConstraint(["company_id", "pattern_id"], ["work_week_patterns.company_id", "work_week_patterns.id"],
                             name="fk_work_week_pattern_versions_pattern", ondelete="CASCADE"),
        CheckConstraint("version >= 1", name="ck_work_week_pattern_versions_version"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from",
                        name="ck_work_week_pattern_versions_dates"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_work_week_pattern_versions_hash"),
        CheckConstraint("jsonb_typeof(rules) = 'object'", name="ck_work_week_pattern_versions_rules"),
    )


class BranchCalendarLink(TenantMixin, BaseModel):
    """Effective-dated holiday calendar and work week of a branch (LH 04-holiday/01 §4.3; fixes BUG-14).

    Rows of one branch never overlap (trigger) and tile from the first row on; the row valid on the branch-local
    today is mirrored into `branches.holiday_calendar_id` / `work_week_pattern_id`.
    """

    __tablename__ = "branch_calendar_links"

    branch_id = Column(Integer, nullable=False, index=True)
    holiday_calendar_id = Column(Integer, nullable=True)
    work_week_pattern_id = Column(Integer, nullable=True)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    reason = Column(String(300), nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_branch_calendar_links_company_id_id"),
        ForeignKeyConstraint(["company_id", "branch_id"], ["branches.company_id", "branches.id"],
                             name="fk_branch_calendar_links_branch"),
        ForeignKeyConstraint(["company_id", "holiday_calendar_id"],
                             ["holiday_calendars.company_id", "holiday_calendars.id"],
                             name="fk_branch_calendar_links_calendar"),
        ForeignKeyConstraint(["company_id", "work_week_pattern_id"],
                             ["work_week_patterns.company_id", "work_week_patterns.id"],
                             name="fk_branch_calendar_links_work_week"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_branch_calendar_links_dates"),
        CheckConstraint("row_version >= 1", name="ck_branch_calendar_links_row_version"),
    )


class HolidayCalendarAssignment(TenantMixin, BaseModel):
    """Criteria row giving matching employees a calendar and/or a work week for a date range (04-holiday/01 §4.4).

    `criteria` is `AssignmentCriteria` (+ `shift_ids`, team and joiner dimensions); lower `priority` wins. Rows are
    written by the calendar-assignment service (calendar goal); the work-week resolver reads the pattern side.
    """

    __tablename__ = "holiday_calendar_assignments"

    calendar_id = Column(Integer, nullable=True)
    work_week_pattern_id = Column(Integer, nullable=True)
    criteria = Column(JSONB, nullable=False, default=dict)
    priority = Column(Integer, nullable=False)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    legal_entity_id = Column(Integer, nullable=True)
    reason = Column(String(500), nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_holiday_calendar_assignments_company_id_id"),
        ForeignKeyConstraint(["company_id", "calendar_id"], ["holiday_calendars.company_id", "holiday_calendars.id"],
                             name="fk_holiday_calendar_assignments_calendar"),
        ForeignKeyConstraint(["company_id", "work_week_pattern_id"],
                             ["work_week_patterns.company_id", "work_week_patterns.id"],
                             name="fk_holiday_calendar_assignments_work_week"),
        ForeignKeyConstraint(["company_id", "legal_entity_id"], ["legal_entities.company_id", "legal_entities.id"],
                             name="fk_holiday_calendar_assignments_legal_entity"),
        CheckConstraint("num_nonnulls(calendar_id, work_week_pattern_id) >= 1",
                        name="ck_holiday_calendar_assignments_target"),
        CheckConstraint("priority BETWEEN 1 AND 999", name="ck_holiday_calendar_assignments_priority"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from",
                        name="ck_holiday_calendar_assignments_dates"),
        CheckConstraint("jsonb_typeof(criteria) = 'object'", name="ck_holiday_calendar_assignments_criteria"),
        CheckConstraint("row_version >= 1", name="ck_holiday_calendar_assignments_row_version"),
    )


class HolidayCalendar(TenantMixin, BaseModel):
    """A set of holidays (3.4 §8; LH 04-holiday/01 §3). A calendar holds its own occurrences and includes other
    calendars (``holiday_calendar_includes``, a DAG) minus its exclusions; ``holiday_calendar_service.flatten`` is
    the single resolver. ``parent_calendar_id`` is the legacy single parent, mirrored as the priority-0 include.
    ``legal_entity_id`` NULL = tenant-wide. ``weekend_days`` NULL = derived (``effective_weekend``, V3-013).
    Inheritance cycles are rejected by triggers.
    """

    __tablename__ = "holiday_calendars"

    code = Column(String(50), nullable=False)
    name = Column(String(150), nullable=False)
    name_i18n = Column(JSONB, nullable=False, default=dict)
    description = Column(Text, nullable=True)
    parent_calendar_id = Column(Integer, nullable=True, index=True)
    legal_entity_id = Column(Integer, nullable=True, index=True)
    country_code = Column(String(2), nullable=True)
    subdivision_code = Column(String(6), nullable=True)
    weekend_days = Column(JSONB(none_as_null=True), nullable=True)
    employee_observance = Column(String(20), nullable=False, default="NONE")
    status = Column(String(10), nullable=False, default="ACTIVE")
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        *_code_name_status("holiday_calendars"),
        ForeignKeyConstraint(
            ["company_id", "parent_calendar_id"], ["holiday_calendars.company_id", "holiday_calendars.id"],
            name="fk_holiday_calendars_parent",
        ),
        ForeignKeyConstraint(["company_id", "legal_entity_id"], ["legal_entities.company_id", "legal_entities.id"],
                             name="fk_holiday_calendars_legal_entity", use_alter=True),
        CheckConstraint(f"employee_observance IN {EMPLOYEE_OBSERVANCE}", name="ck_holiday_calendars_observance"),
        CheckConstraint("row_version >= 1", name="ck_holiday_calendars_row_version"),
        CheckConstraint("parent_calendar_id IS NULL OR parent_calendar_id <> id", name="ck_holiday_calendars_not_own_parent"),
        CheckConstraint("country_code ~ '^[A-Z]{2}$'", name="ck_holiday_calendars_country"),
        CheckConstraint(
            "subdivision_code IS NULL OR (subdivision_code ~ '^[A-Z]{2}-[A-Z0-9]{1,3}$' "
            "AND (country_code IS NULL OR left(subdivision_code, 2) = country_code))",
            name="ck_holiday_calendars_subdivision",
        ),
    )


def calendar_lineage_select(company_id: int, calendar_id: int):
    """Ids of a holiday calendar and its ancestors, nearest first.

    The holidays that apply under a calendar are those whose calendar_id is in this list.
    """
    root = (
        select(HolidayCalendar.id, HolidayCalendar.parent_calendar_id, literal(0).label("depth"))
        .where(HolidayCalendar.company_id == company_id, HolidayCalendar.id == calendar_id)
        .cte("calendar_lineage", recursive=True)
    )
    parent = aliased(HolidayCalendar)
    lineage = root.union_all(
        select(parent.id, parent.parent_calendar_id, (root.c.depth + 1).label("depth")).where(
            parent.company_id == company_id, parent.id == root.c.parent_calendar_id, root.c.depth < MAX_CALENDAR_DEPTH
        )
    )
    return select(lineage.c.id).order_by(lineage.c.depth)

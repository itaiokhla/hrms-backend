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
    ForeignKeyConstraint,
    Integer,
    String,
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
    """Weekly offs (3.4 §3): whole weekdays plus nth-weekday rules.

    `weekly_off_days` lists distinct weekday numbers (Monday = 0 ... Sunday = 6), at most six.
    `nth_weekday_offs` adds days such as the 2nd and 4th Saturday: [{"weekday": 5, "nth": [2, 4]}],
    nth 1-5 or -1 for the last one in the month. The database rejects any other shape.
    """

    __tablename__ = "work_week_patterns"

    code = Column(String(30), nullable=False)
    name = Column(String(100), nullable=False)
    weekly_off_days = Column(JSONB, nullable=False)
    nth_weekday_offs = Column(JSONB, nullable=False, default=list)
    status = Column(String(10), nullable=False, default="ACTIVE")

    __table_args__ = (
        *_code_name_status("work_week_patterns"),
        CheckConstraint("work_week_rules_valid(weekly_off_days, nth_weekday_offs)", name="ck_work_week_patterns_rules"),
    )


class HolidayCalendar(TenantMixin, BaseModel):
    """A set of holidays (3.4 §8). A calendar inherits every holiday of its ancestors, so a branch's
    effective holidays are its calendar's plus its parents' (see calendar_lineage_select).
    Inheritance cycles are rejected by a trigger.
    """

    __tablename__ = "holiday_calendars"

    code = Column(String(50), nullable=False)
    name = Column(String(150), nullable=False)
    parent_calendar_id = Column(Integer, nullable=True, index=True)
    country_code = Column(String(2), nullable=True)
    subdivision_code = Column(String(6), nullable=True)
    status = Column(String(10), nullable=False, default="ACTIVE")

    __table_args__ = (
        *_code_name_status("holiday_calendars"),
        ForeignKeyConstraint(
            ["company_id", "parent_calendar_id"], ["holiday_calendars.company_id", "holiday_calendars.id"],
            name="fk_holiday_calendars_parent",
        ),
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

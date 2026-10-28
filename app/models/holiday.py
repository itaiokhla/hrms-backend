from sqlalchemy import (
    CHAR, Boolean, CheckConstraint, Column, Date, DateTime, ForeignKeyConstraint, Index, Integer, SmallInteger, String,
    Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import BaseModel, TenantMixin
# The calendar and its criteria rows live with the payroll organisation models (0008, 0037); re-exported here so
# holiday code imports every calendar object from one module (LH 14 WI-1.3).
from app.models.pay_org import HolidayCalendar, HolidayCalendarAssignment  # noqa: F401

HOLIDAY_TYPES = ("PUBLIC", "COMPANY", "OPTIONAL")
# 04-holiday/01 §5 (SPINE V3-008): occurrence vocabulary.
HOLIDAY_CATEGORIES = ("NATIONAL", "REGIONAL", "FESTIVAL", "ELECTION", "COMPANY_EVENT", "SHUTDOWN", "BRIDGE",
                      "EMERGENCY", "BANK", "OTHER")
HOLIDAY_SOURCES = ("MANUAL", "RULE", "PACK", "IMPORT", "GOVT_NOTICE")
HOLIDAY_STATUSES = ("PROVISIONAL", "CONFIRMED", "CANCELLED")
HOLIDAY_OBSERVANCES = ("ORIGINAL", "OBSERVED")
HOLIDAY_PORTIONS = ("FULL", "FIRST_HALF", "SECOND_HALF")
YEAR_STATUSES = ("DRAFT", "PUBLISHED")


class Holiday(TenantMixin, BaseModel):
    """Company holiday. `location` NULL means it applies to every location.

    `calendar_id` and `holiday_type` are the payroll view of the same holiday (3.4 §8): the
    holiday calendar it belongs to and whether it is a public, company or optional holiday.
    Migration 0008 filled both from `location` and `is_optional`, which stay for today's
    holiday screens until every reader uses calendars.
    """

    __tablename__ = "holidays"

    name = Column(String(150), nullable=False)
    date = Column(Date, nullable=False)
    location = Column(String(100), nullable=True)
    is_optional = Column(Boolean, nullable=False, default=False)
    description = Column(Text, nullable=True)
    calendar_id = Column(Integer, nullable=True)
    holiday_type = Column(String(10), nullable=False, default="COMPANY")
    # 0044 (04-holiday/01 §5.1): rule-generated rows, statuses, observed partners. Only ``status``, ``portion`` and
    # ``observance`` enter ``holiday_service.content()``, and only when they differ from the legacy default.
    rule_id = Column(Integer, nullable=True)
    portion = Column(String(12), nullable=False, default="FULL", server_default="FULL")
    status = Column(String(12), nullable=False, default="CONFIRMED", server_default="CONFIRMED")
    observance = Column(String(20), nullable=False, default="ORIGINAL", server_default="ORIGINAL")
    observed_from_date = Column(Date, nullable=True)
    source = Column(String(12), nullable=False, default="MANUAL", server_default="MANUAL")
    source_ref = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    category = Column(String(20), nullable=False, default="OTHER", server_default="OTHER")
    sequence_index = Column(SmallInteger, nullable=False, default=1, server_default="1")
    name_i18n = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    confirmed_by_user_id = Column(Integer, nullable=True)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    row_version = Column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_holidays_company_id_id"),
        # One holiday of a name per date and calendar: two branches may each observe "Pongal" (0016).
        Index("uq_holidays_calendar_date_name", "company_id", text("COALESCE(calendar_id, 0)"), "date",
              text("lower(name)"), unique=True, postgresql_where=text("status <> 'CANCELLED'")),
        Index("uq_holidays_rule_occurrence", "company_id", "calendar_id", "rule_id",
              text("(source_ref->>'occurrence_key')"), unique=True, postgresql_where=text("rule_id IS NOT NULL")),
        Index("ix_holidays_rule", "company_id", "rule_id"),
        Index("ix_holidays_company_date", "company_id", "date"),
        Index("ix_holidays_calendar_date", "calendar_id", "date"),
        ForeignKeyConstraint(
            ["company_id", "calendar_id"], ["holiday_calendars.company_id", "holiday_calendars.id"],
            name="fk_holidays_calendar",
        ),
        ForeignKeyConstraint(["company_id", "rule_id"], ["holiday_rules.company_id", "holiday_rules.id"],
                             name="fk_holidays_rule"),
        CheckConstraint(f"holiday_type IN {HOLIDAY_TYPES}", name="ck_holidays_type"),
        CheckConstraint(f"portion IN {HOLIDAY_PORTIONS}", name="ck_holidays_portion"),
        CheckConstraint(f"status IN {HOLIDAY_STATUSES}", name="ck_holidays_status"),
        CheckConstraint(f"observance IN {HOLIDAY_OBSERVANCES}", name="ck_holidays_observance"),
        CheckConstraint("(observance = 'OBSERVED') = (observed_from_date IS NOT NULL)", name="ck_holidays_observed_from"),
        CheckConstraint(f"source IN {HOLIDAY_SOURCES}", name="ck_holidays_source"),
        CheckConstraint(f"category IN {HOLIDAY_CATEGORIES}", name="ck_holidays_category"),
        CheckConstraint("sequence_index BETWEEN 1 AND 31", name="ck_holidays_sequence"),
        CheckConstraint("jsonb_typeof(source_ref) = 'object'", name="ck_holidays_source_ref"),
        CheckConstraint("row_version >= 1", name="ck_holidays_row_version"),
    )


class HolidayCalendarInclude(TenantMixin, BaseModel):
    """``calendar_id`` includes ``included_calendar_id`` (LH 04-holiday/01 §3.3): a DAG ordered by ``priority``
    (unique per calendar, lower first; 0 = the mirror of ``parent_calendar_id``). A trigger rejects cycles through
    includes or parents and paths deeper than ``MAX_CALENDAR_DEPTH`` (``ORG_CALENDAR_CYCLE``)."""

    __tablename__ = "holiday_calendar_includes"

    calendar_id = Column(Integer, nullable=False)
    included_calendar_id = Column(Integer, nullable=False)
    priority = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_holiday_calendar_includes_company_id_id"),
        UniqueConstraint("company_id", "calendar_id", "included_calendar_id", name="uq_holiday_calendar_includes_pair"),
        UniqueConstraint("company_id", "calendar_id", "priority", name="uq_holiday_calendar_includes_priority"),
        ForeignKeyConstraint(["company_id", "calendar_id"], ["holiday_calendars.company_id", "holiday_calendars.id"],
                             name="fk_holiday_calendar_includes_calendar", ondelete="CASCADE"),
        ForeignKeyConstraint(["company_id", "included_calendar_id"],
                             ["holiday_calendars.company_id", "holiday_calendars.id"],
                             name="fk_holiday_calendar_includes_included"),
        CheckConstraint("calendar_id <> included_calendar_id", name="ck_holiday_calendar_includes_not_self"),
        CheckConstraint("priority >= 0", name="ck_holiday_calendar_includes_priority"),
    )


class HolidayCalendarExclusion(TenantMixin, BaseModel):
    """An inherited item ``calendar_id`` refuses (LH 04-holiday/01 §3.4): one occurrence (``holiday_id``) or a rule's
    occurrences from ``effective_from_year`` (``holiday_rule_id``; NULL year = every year). Only rows reaching the
    calendar through its includes can be excluded; immutable (change = delete + add)."""

    __tablename__ = "holiday_calendar_exclusions"

    calendar_id = Column(Integer, nullable=False)
    holiday_rule_id = Column(Integer, nullable=True)
    holiday_id = Column(Integer, nullable=True)
    effective_from_year = Column(SmallInteger, nullable=True)
    reason = Column(String(500), nullable=False)
    created_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_holiday_calendar_exclusions_company_id_id"),
        ForeignKeyConstraint(["company_id", "calendar_id"], ["holiday_calendars.company_id", "holiday_calendars.id"],
                             name="fk_holiday_calendar_exclusions_calendar", ondelete="CASCADE"),
        ForeignKeyConstraint(["company_id", "holiday_id"], ["holidays.company_id", "holidays.id"],
                             name="fk_holiday_calendar_exclusions_holiday", ondelete="CASCADE"),
        CheckConstraint("num_nonnulls(holiday_rule_id, holiday_id) = 1", name="ck_holiday_calendar_exclusions_target"),
        CheckConstraint("effective_from_year IS NULL OR (holiday_rule_id IS NOT NULL "
                        "AND effective_from_year BETWEEN 2000 AND 2100)", name="ck_holiday_calendar_exclusions_from_year"),
        Index("uq_holiday_calendar_exclusions_holiday", "company_id", "calendar_id", "holiday_id", unique=True,
              postgresql_where=text("holiday_id IS NOT NULL")),
        Index("uq_holiday_calendar_exclusions_rule", "company_id", "calendar_id", "holiday_rule_id", unique=True,
              postgresql_where=text("holiday_rule_id IS NOT NULL")),
    )


class HolidayRule(TenantMixin, BaseModel):
    """A repeating holiday of one calendar (LH 04-holiday/02 §3.2): the generator turns it into dated occurrence
    rows per calendar year (``holiday_recurrence.generate_year``); only rows of PUBLISHED years ever count."""

    __tablename__ = "holiday_rules"

    calendar_id = Column(Integer, nullable=False)
    name = Column(String(150), nullable=False)
    name_i18n = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    holiday_type = Column(String(10), nullable=False, default="COMPANY")
    category = Column(String(20), nullable=False, default="OTHER")
    kind = Column(String(20), nullable=False)
    params = Column(JSONB, nullable=False)
    portion = Column(String(12), nullable=False, default="FULL")
    duration_days = Column(SmallInteger, nullable=False, default=1)
    duration_range = Column(JSONB(none_as_null=True), nullable=True)
    observed_rule = Column(String(20), nullable=False, default="NONE")
    applies_on = Column(JSONB(none_as_null=True), nullable=True)
    valid_from_year = Column(SmallInteger, nullable=False)
    valid_to_year = Column(SmallInteger, nullable=True)
    status = Column(String(10), nullable=False, default="ACTIVE")
    version = Column(Integer, nullable=False, default=1)
    source = Column(String(12), nullable=False, default="MANUAL")
    source_ref = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    created_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_holiday_rules_company_id_id"),
        ForeignKeyConstraint(["company_id", "calendar_id"], ["holiday_calendars.company_id", "holiday_calendars.id"],
                             name="fk_holiday_rules_calendar", ondelete="CASCADE"),
        Index("ix_holiday_rules_calendar_status", "company_id", "calendar_id", "status"),
        Index("uq_holiday_rules_active_name", "company_id", "calendar_id", text("lower(btrim(name))"), unique=True,
              postgresql_where=text("status = 'ACTIVE'")),
    )


class HolidayCalendarYear(TenantMixin, BaseModel):
    """One calendar year of one calendar (LH 04-holiday/01 §6.1): DRAFT -> PUBLISHED (one way); ``version`` 0 while
    DRAFT, 1 at the first publication, +1 per amendment. Only rows of PUBLISHED years count; rows of a year without
    a record are legacy rows and count too (DECISIONS IG-LH-15). The ``publish_*`` columns hold a pending
    second-person request (V3-027)."""

    __tablename__ = "holiday_calendar_years"

    calendar_id = Column(Integer, nullable=False)
    year = Column(SmallInteger, nullable=False)
    status = Column(String(10), nullable=False, default="DRAFT")
    version = Column(Integer, nullable=False, default=0)
    content_hash = Column(CHAR(64), nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    published_by_user_id = Column(Integer, nullable=True)
    approved_by_user_id = Column(Integer, nullable=True)
    publish_requested_by_user_id = Column(Integer, nullable=True)
    publish_requested_at = Column(DateTime(timezone=True), nullable=True)
    requested_content_hash = Column(CHAR(64), nullable=True)
    publish_request_payload = Column(JSONB(none_as_null=True), nullable=True)
    generated_at = Column(DateTime(timezone=True), nullable=True)
    # Rollover state of the Next year page (0046): NONE / OK / FAILED, the error and the last generation's counts.
    generation_status = Column(String(8), nullable=False, default="NONE")
    generation_error = Column(String(500), nullable=True)
    generation_summary = Column(JSONB(none_as_null=True), nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_holiday_calendar_years_company_id_id"),
        UniqueConstraint("company_id", "calendar_id", "year", name="uq_holiday_calendar_years_calendar_year"),
        ForeignKeyConstraint(["company_id", "calendar_id"], ["holiday_calendars.company_id", "holiday_calendars.id"],
                             name="fk_holiday_calendar_years_calendar", ondelete="CASCADE"),
        CheckConstraint(f"status IN {YEAR_STATUSES}", name="ck_holiday_calendar_years_status"),
        CheckConstraint("(status = 'PUBLISHED') = (version >= 1)", name="ck_holiday_calendar_years_version"),
    )


IMPORT_KINDS = ("CSV_DATED", "CSV_RULES", "ICS", "PACK", "CONNECTOR")
IMPORT_STATUSES = ("DRY_RUN", "COMMITTED", "FAILED", "EXPIRED")


class HolidayImport(TenantMixin, BaseModel):
    """One CSV / ICS import into a calendar year (LH 04-holiday/02 §9.1; 11 §4.2.14; 0046): a dry run stores the
    parsed lines and their classification in ``report``; the commit applies the accepted lines."""

    __tablename__ = "holiday_imports"

    calendar_id = Column(Integer, nullable=False)
    kind = Column(String(12), nullable=False)
    target_year = Column(SmallInteger, nullable=False)
    status = Column(String(10), nullable=False, default="DRY_RUN")
    file_name = Column(String(200), nullable=True)
    file_path = Column(String(300), nullable=True)
    sha256 = Column(CHAR(64), nullable=True)
    source_id = Column(String(60), nullable=True)
    source_version = Column(String(40), nullable=True)
    report = Column(JSONB, nullable=False, default=dict)
    target_hash = Column(CHAR(64), nullable=True)
    job_id = Column(Integer, nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    committed_at = Column(DateTime(timezone=True), nullable=True)
    committed_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_holiday_imports_company_id_id"),
        ForeignKeyConstraint(["company_id", "calendar_id"], ["holiday_calendars.company_id", "holiday_calendars.id"],
                             name="fk_holiday_imports_calendar", ondelete="CASCADE"),
        CheckConstraint(f"kind IN {IMPORT_KINDS}", name="ck_holiday_imports_kind"),
        CheckConstraint(f"status IN {IMPORT_STATUSES}", name="ck_holiday_imports_status"),
    )


class CalendarWorkingDay(TenantMixin, BaseModel):
    """A declared working day (LH 04-holiday/01 §5.5, 03 §4): turns a WEEKLY weekly off into a working day for
    everyone on the calendar and on calendars that include it; released with its calendar's year."""

    __tablename__ = "calendar_working_days"

    calendar_id = Column(Integer, nullable=False)
    date = Column(Date, nullable=False)
    name = Column(String(150), nullable=False)
    portion = Column(String(12), nullable=False, default="FULL")
    reason = Column(String(300), nullable=False)
    created_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_calendar_working_days_company_id_id"),
        UniqueConstraint("company_id", "calendar_id", "date", name="uq_calendar_working_days_date"),
        ForeignKeyConstraint(["company_id", "calendar_id"], ["holiday_calendars.company_id", "holiday_calendars.id"],
                             name="fk_calendar_working_days_calendar", ondelete="CASCADE"),
    )

"""LH-M1 holiday rules, calendar years and declared working days (LH 11 §4.2.13, §11.6 steps 2-3; LH 14 WI-1.4;
04-holiday/01 §5.1, §6.1; 04-holiday/02 §3.2, §4.4).

* NEW ``holiday_rules`` (V3-026 columns; ``kind`` without RRULE until LH-M9; params validated by
  ``holiday_recurrence.validate``); ``holiday_calendar_exclusions.holiday_rule_id`` gains its FK.
* ``holidays`` gains ``rule_id``, ``portion``, ``status``, ``observance``, ``observed_from_date``, ``source``,
  ``source_ref``, ``category``, ``sequence_index``, ``name_i18n``, ``confirmed_by_user_id``, ``confirmed_at`` and
  ``row_version``; ``uq_holidays_calendar_date_name`` becomes partial ``WHERE status <> 'CANCELLED'``; a generated
  row is unique per ``(calendar, rule, source_ref->>'occurrence_key')``.
* NEW ``holiday_calendar_years`` (DRAFT -> PUBLISHED one way, ``version`` 0 while DRAFT, pending second-person
  request columns of V3-027 plus ``publish_request_payload``, DECISIONS IG-LH-15) with a guard trigger, and
  ``holiday_calendar_years_published_guard()`` on ``holidays``: a row of a PUBLISHED year is written only inside an
  amendment (``SET LOCAL app.holiday_amendment = 'on'``); the table owner (migrations, maintenance) is exempt like
  it is from row-level security.
* NEW ``calendar_working_days`` (declared working days, 04-holiday/03 §4; one per calendar and date).
* ``employee_optional_holidays -> holidays`` becomes ``ON DELETE RESTRICT`` (BUG-15).
* Data (zero diff, 11 §11.6 step 2): one PUBLISHED year, ``version 1``, per (calendar, year) that has rows, with a
  ``content_hash`` from the frozen canonical function below. Every existing row stays CONFIRMED / FULL / ORIGINAL /
  MANUAL, so ``holiday_service.content()`` is unchanged.

Downgrade is refused while rules, DRAFT years, declared working days or non-default occurrence rows exist.

Revision ID: 0044_lh_m1_holiday_rules_years
Revises: 0043_lh_m1_calendar_structure
Create Date: 2026-10-27
"""
import hashlib
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0044_lh_m1_holiday_rules_years"
down_revision: Union[str, None] = "0043_lh_m1_calendar_structure"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies: later model changes must not alter what this revision does.
TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
KINDS = ("FIXED_DATE", "NTH_WEEKDAY", "WEEKDAY_RELATIVE", "EASTER_OFFSET", "ANNUAL_CONFIRM", "ONE_OFF")
OBSERVED_RULES = ("NONE", "NEAREST_WORKDAY", "NEXT_WORKDAY", "PREVIOUS_WORKDAY")
PORTIONS = ("FULL", "FIRST_HALF", "SECOND_HALF")
TYPES = ("PUBLIC", "COMPANY", "OPTIONAL")
CATEGORIES = ("NATIONAL", "REGIONAL", "FESTIVAL", "ELECTION", "COMPANY_EVENT", "SHUTDOWN", "BRIDGE", "EMERGENCY",
              "BANK", "OTHER")
RULE_SOURCES = ("MANUAL", "PACK", "IMPORT", "GOVT_NOTICE")
SOURCES = ("MANUAL", "RULE", "PACK", "IMPORT", "GOVT_NOTICE")
STATUSES = ("PROVISIONAL", "CONFIRMED", "CANCELLED")
OBSERVANCES = ("ORIGINAL", "OBSERVED")

YEAR_GUARD = """
CREATE OR REPLACE FUNCTION holiday_calendar_years_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status = 'PUBLISHED' THEN
            RAISE EXCEPTION 'YEAR_PUBLISHED: a published holiday year is never deleted'
                USING ERRCODE = 'check_violation', CONSTRAINT = 'holiday_calendar_years_guard';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status = 'PUBLISHED' AND NEW.status <> 'PUBLISHED' THEN
        RAISE EXCEPTION 'YEAR_PUBLISHED: a published holiday year cannot return to draft'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'holiday_calendar_years_guard';
    END IF;
    IF NEW.version < OLD.version OR NEW.calendar_id <> OLD.calendar_id OR NEW.year <> OLD.year THEN
        RAISE EXCEPTION 'YEAR_PUBLISHED: a holiday year keeps its calendar, year and version history'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'holiday_calendar_years_guard';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

# A row of a PUBLISHED (calendar, year) changes only inside an amendment (11 §4.2.13, WI-1.4). The owner of the table
# (migrations, maintenance scripts, test fixtures) is exempt, as it is from row-level security.
PUBLISHED_GUARD = """
CREATE OR REPLACE FUNCTION holiday_calendar_years_published_guard() RETURNS trigger AS $$
DECLARE
    owner_name text;
BEGIN
    SELECT pg_get_userbyid(relowner) INTO owner_name FROM pg_class WHERE oid = TG_RELID;
    IF current_user = owner_name OR coalesce(current_setting('app.holiday_amendment', true), '') = 'on' THEN
        RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
    END IF;
    IF TG_OP IN ('UPDATE', 'DELETE') AND OLD.calendar_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM holiday_calendar_years y WHERE y.company_id = OLD.company_id AND y.calendar_id = OLD.calendar_id
          AND y.year = extract(year FROM OLD.date)::int AND y.status = 'PUBLISHED') THEN
        RAISE EXCEPTION 'YEAR_PUBLISHED: % of a published holiday year needs an amendment', OLD.date
            USING ERRCODE = 'check_violation', CONSTRAINT = 'holiday_calendar_years_published_guard';
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') AND NEW.calendar_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM holiday_calendar_years y WHERE y.company_id = NEW.company_id AND y.calendar_id = NEW.calendar_id
          AND y.year = extract(year FROM NEW.date)::int AND y.status = 'PUBLISHED') THEN
        RAISE EXCEPTION 'YEAR_PUBLISHED: % of a published holiday year needs an amendment', NEW.date
            USING ERRCODE = 'check_violation', CONSTRAINT = 'holiday_calendar_years_published_guard';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$ LANGUAGE plpgsql;
"""


def _content(row) -> dict:
    """Frozen copy of ``holiday_service.content()`` for legacy rows (CONFIRMED, FULL, ORIGINAL)."""
    return {"id": row.id, "name": row.name, "date": row.date.isoformat(), "calendar_id": row.calendar_id,
            "holiday_type": row.holiday_type, "location": row.location, "is_optional": bool(row.is_optional)}


def _year_hash(rows) -> str:
    """Frozen canonical year hash: own occurrences sorted by (date, id); no declared working days existed before."""
    payload = {"holidays": [_content(r) for r in sorted(rows, key=lambda r: (r.date, r.id))], "working_days": []}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _common(table):
    return [
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("company_id", "id", name=f"uq_{table}_company_id_id"),
    ]


def _secure(bind, table):
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")
    roles = [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"
    ))]
    for role in roles:
        quoted = '"' + role.replace('"', '""') + '"'
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {quoted}")
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}")


def upgrade() -> None:
    bind = op.get_bind()

    # ---- holiday_rules
    op.create_table(
        "holiday_rules",
        *_common("holiday_rules"),
        sa.Column("calendar_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("name_i18n", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("holiday_type", sa.String(10), nullable=False, server_default="COMPANY"),
        sa.Column("category", sa.String(20), nullable=False, server_default="OTHER"),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("portion", sa.String(12), nullable=False, server_default="FULL"),
        sa.Column("duration_days", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("duration_range", postgresql.JSONB(), nullable=True),
        sa.Column("observed_rule", sa.String(20), nullable=False, server_default="NONE"),
        sa.Column("applies_on", postgresql.JSONB(), nullable=True),
        sa.Column("valid_from_year", sa.SmallInteger(), nullable=False),
        sa.Column("valid_to_year", sa.SmallInteger(), nullable=True),
        sa.Column("status", sa.String(10), nullable=False, server_default="ACTIVE"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source", sa.String(12), nullable=False, server_default="MANUAL"),
        sa.Column("source_ref", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["company_id", "calendar_id"], ["holiday_calendars.company_id", "holiday_calendars.id"],
                                name="fk_holiday_rules_calendar", ondelete="CASCADE"),
        sa.CheckConstraint(f"kind IN {KINDS}", name="ck_holiday_rules_kind"),
        sa.CheckConstraint(f"holiday_type IN {TYPES}", name="ck_holiday_rules_type"),
        sa.CheckConstraint(f"category IN {CATEGORIES}", name="ck_holiday_rules_category"),
        sa.CheckConstraint(f"portion IN {PORTIONS}", name="ck_holiday_rules_portion"),
        sa.CheckConstraint(f"observed_rule IN {OBSERVED_RULES}", name="ck_holiday_rules_observed_rule"),
        sa.CheckConstraint(f"source IN {RULE_SOURCES}", name="ck_holiday_rules_source"),
        sa.CheckConstraint("status IN ('ACTIVE', 'INACTIVE')", name="ck_holiday_rules_status"),
        sa.CheckConstraint("duration_days BETWEEN 1 AND 31", name="ck_holiday_rules_duration"),
        sa.CheckConstraint("duration_range IS NULL OR kind = 'ANNUAL_CONFIRM'", name="ck_holiday_rules_duration_range"),
        sa.CheckConstraint("valid_from_year BETWEEN 2000 AND 2100 AND (valid_to_year IS NULL OR "
                           "valid_to_year BETWEEN valid_from_year AND 2100)", name="ck_holiday_rules_years"),
        sa.CheckConstraint("char_length(btrim(name)) BETWEEN 1 AND 150", name="ck_holiday_rules_name"),
        sa.CheckConstraint("jsonb_typeof(params) = 'object'", name="ck_holiday_rules_params"),
        sa.CheckConstraint("version >= 1 AND row_version >= 1", name="ck_holiday_rules_versions"),
    )
    op.create_index("ix_holiday_rules_company_id", "holiday_rules", ["company_id"])
    op.create_index("ix_holiday_rules_calendar_status", "holiday_rules", ["company_id", "calendar_id", "status"])
    op.create_index("uq_holiday_rules_active_name", "holiday_rules",
                    ["company_id", "calendar_id", sa.text("lower(btrim(name))")], unique=True,
                    postgresql_where=sa.text("status = 'ACTIVE'"))
    _secure(bind, "holiday_rules")
    op.create_foreign_key("fk_holiday_calendar_exclusions_rule", "holiday_calendar_exclusions", "holiday_rules",
                          ["company_id", "holiday_rule_id"], ["company_id", "id"], ondelete="CASCADE")

    # ---- holidays: occurrence columns (04-holiday/01 §5.1)
    add = lambda col: op.add_column("holidays", col)  # noqa: E731
    add(sa.Column("rule_id", sa.Integer(), nullable=True))
    add(sa.Column("portion", sa.String(12), nullable=False, server_default="FULL"))
    add(sa.Column("status", sa.String(12), nullable=False, server_default="CONFIRMED"))
    add(sa.Column("observance", sa.String(20), nullable=False, server_default="ORIGINAL"))
    add(sa.Column("observed_from_date", sa.Date(), nullable=True))
    add(sa.Column("source", sa.String(12), nullable=False, server_default="MANUAL"))
    add(sa.Column("source_ref", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    add(sa.Column("category", sa.String(20), nullable=False, server_default="OTHER"))
    add(sa.Column("sequence_index", sa.SmallInteger(), nullable=False, server_default="1"))
    add(sa.Column("name_i18n", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    add(sa.Column("confirmed_by_user_id", sa.Integer(), nullable=True))
    add(sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))
    add(sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"))
    op.create_foreign_key("fk_holidays_rule", "holidays", "holiday_rules", ["company_id", "rule_id"],
                          ["company_id", "id"])
    for name, check in (
        ("ck_holidays_portion", f"portion IN {PORTIONS}"),
        ("ck_holidays_status", f"status IN {STATUSES}"),
        ("ck_holidays_observance", f"observance IN {OBSERVANCES}"),
        ("ck_holidays_observed_from", "(observance = 'OBSERVED') = (observed_from_date IS NOT NULL)"),
        ("ck_holidays_source", f"source IN {SOURCES}"),
        ("ck_holidays_category", f"category IN {CATEGORIES}"),
        ("ck_holidays_sequence", "sequence_index BETWEEN 1 AND 31"),
        ("ck_holidays_source_ref", "jsonb_typeof(source_ref) = 'object'"),
        ("ck_holidays_row_version", "row_version >= 1"),
    ):
        op.create_check_constraint(name, "holidays", check)
    op.execute("DROP INDEX IF EXISTS uq_holidays_calendar_date_name")
    op.execute("CREATE UNIQUE INDEX uq_holidays_calendar_date_name ON holidays "
               "(company_id, COALESCE(calendar_id, 0), date, lower(name)) WHERE status <> 'CANCELLED'")
    op.execute("CREATE UNIQUE INDEX uq_holidays_rule_occurrence ON holidays "
               "(company_id, calendar_id, rule_id, (source_ref->>'occurrence_key')) WHERE rule_id IS NOT NULL")
    op.create_index("ix_holidays_rule", "holidays", ["company_id", "rule_id"])

    # ---- holiday_calendar_years
    op.create_table(
        "holiday_calendar_years",
        *_common("holiday_calendar_years"),
        sa.Column("calendar_id", sa.Integer(), nullable=False),
        sa.Column("year", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="DRAFT"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content_hash", sa.CHAR(64), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by_user_id", sa.Integer(), nullable=True),
        sa.Column("approved_by_user_id", sa.Integer(), nullable=True),
        sa.Column("publish_requested_by_user_id", sa.Integer(), nullable=True),
        sa.Column("publish_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_content_hash", sa.CHAR(64), nullable=True),
        sa.Column("publish_request_payload", postgresql.JSONB(), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["company_id", "calendar_id"], ["holiday_calendars.company_id", "holiday_calendars.id"],
                                name="fk_holiday_calendar_years_calendar", ondelete="CASCADE"),
        sa.UniqueConstraint("company_id", "calendar_id", "year", name="uq_holiday_calendar_years_calendar_year"),
        sa.CheckConstraint("status IN ('DRAFT', 'PUBLISHED')", name="ck_holiday_calendar_years_status"),
        sa.CheckConstraint("(status = 'PUBLISHED') = (version >= 1)", name="ck_holiday_calendar_years_version"),
        sa.CheckConstraint("year BETWEEN 2000 AND 2100", name="ck_holiday_calendar_years_year"),
        sa.CheckConstraint("approved_by_user_id IS NULL OR approved_by_user_id <> published_by_user_id",
                           name="ck_holiday_calendar_years_second_person"),
        sa.CheckConstraint("(publish_requested_by_user_id IS NULL) = (requested_content_hash IS NULL)",
                           name="ck_holiday_calendar_years_request"),
        sa.CheckConstraint("row_version >= 1", name="ck_holiday_calendar_years_row_version"),
    )
    op.create_index("ix_holiday_calendar_years_company_id", "holiday_calendar_years", ["company_id"])
    op.execute(YEAR_GUARD)
    op.execute("CREATE TRIGGER trg_holiday_calendar_years_guard BEFORE UPDATE OR DELETE ON holiday_calendar_years "
               "FOR EACH ROW EXECUTE FUNCTION holiday_calendar_years_guard()")
    _secure(bind, "holiday_calendar_years")

    # ---- calendar_working_days
    op.create_table(
        "calendar_working_days",
        *_common("calendar_working_days"),
        sa.Column("calendar_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("portion", sa.String(12), nullable=False, server_default="FULL"),
        sa.Column("reason", sa.String(300), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["company_id", "calendar_id"], ["holiday_calendars.company_id", "holiday_calendars.id"],
                                name="fk_calendar_working_days_calendar", ondelete="CASCADE"),
        sa.UniqueConstraint("company_id", "calendar_id", "date", name="uq_calendar_working_days_date"),
        sa.CheckConstraint(f"portion IN {PORTIONS}", name="ck_calendar_working_days_portion"),
        sa.CheckConstraint("char_length(btrim(name)) BETWEEN 1 AND 150", name="ck_calendar_working_days_name"),
        sa.CheckConstraint("char_length(btrim(reason)) BETWEEN 1 AND 300", name="ck_calendar_working_days_reason"),
    )
    op.create_index("ix_calendar_working_days_company_id", "calendar_working_days", ["company_id"])
    _secure(bind, "calendar_working_days")

    # ---- employee_optional_holidays: a selected holiday is cancelled, never deleted (BUG-15)
    op.drop_constraint("fk_employee_optional_holidays_holiday", "employee_optional_holidays", type_="foreignkey")
    op.create_foreign_key("fk_employee_optional_holidays_holiday", "employee_optional_holidays", "holidays",
                          ["company_id", "holiday_id"], ["company_id", "id"], ondelete="RESTRICT")

    # ---- data: every existing (calendar, year) is PUBLISHED v1 (11 §11.6 step 2; a DRAFT would hide its rows)
    rows = bind.execute(sa.text(
        "SELECT id, company_id, calendar_id, name, date, holiday_type, location, is_optional FROM holidays "
        "WHERE calendar_id IS NOT NULL ORDER BY company_id, calendar_id, date, id")).fetchall()
    years = {}
    for row in rows:
        years.setdefault((row.company_id, row.calendar_id, row.date.year), []).append(row)
    for (company_id, calendar_id, year), items in sorted(years.items()):
        bind.execute(sa.text(
            "INSERT INTO holiday_calendar_years (company_id, calendar_id, year, status, version, content_hash, "
            "published_at) VALUES (:c, :cal, :y, 'PUBLISHED', 1, :h, now())"),
            {"c": company_id, "cal": calendar_id, "y": year, "h": _year_hash(items)})

    # The guard is created last so the backfill above is not an amendment.
    op.execute(PUBLISHED_GUARD)
    op.execute("CREATE TRIGGER trg_holidays_published_guard BEFORE INSERT OR UPDATE OR DELETE ON holidays "
               "FOR EACH ROW EXECUTE FUNCTION holiday_calendar_years_published_guard()")


def downgrade() -> None:
    bind = op.get_bind()
    blockers = bind.execute(sa.text(
        "SELECT (SELECT count(*) FROM holiday_rules) + (SELECT count(*) FROM calendar_working_days) "
        "+ (SELECT count(*) FROM holiday_calendar_years WHERE status = 'DRAFT') "
        "+ (SELECT count(*) FROM holidays WHERE status <> 'CONFIRMED' OR portion <> 'FULL' "
        "   OR observance <> 'ORIGINAL' OR rule_id IS NOT NULL)")).scalar()
    if blockers:
        raise RuntimeError("holiday rules, draft years, declared working days or provisional/cancelled/observed "
                           "occurrences exist that the previous schema cannot hold; remove them before downgrading")
    op.execute("DROP TRIGGER IF EXISTS trg_holidays_published_guard ON holidays")
    op.execute("DROP FUNCTION IF EXISTS holiday_calendar_years_published_guard()")
    op.drop_constraint("fk_employee_optional_holidays_holiday", "employee_optional_holidays", type_="foreignkey")
    op.create_foreign_key("fk_employee_optional_holidays_holiday", "employee_optional_holidays", "holidays",
                          ["company_id", "holiday_id"], ["company_id", "id"], ondelete="CASCADE")
    op.drop_table("calendar_working_days")
    op.drop_table("holiday_calendar_years")
    op.execute("DROP FUNCTION IF EXISTS holiday_calendar_years_guard()")
    op.drop_index("ix_holidays_rule", table_name="holidays")
    op.execute("DROP INDEX IF EXISTS uq_holidays_rule_occurrence")
    op.execute("DROP INDEX IF EXISTS uq_holidays_calendar_date_name")
    op.execute("CREATE UNIQUE INDEX uq_holidays_calendar_date_name ON holidays "
               "(company_id, COALESCE(calendar_id, 0), date, lower(name))")
    for name in ("ck_holidays_row_version",
                 "ck_holidays_source_ref", "ck_holidays_sequence", "ck_holidays_category", "ck_holidays_source",
                 "ck_holidays_observed_from", "ck_holidays_observance", "ck_holidays_status", "ck_holidays_portion"):
        op.drop_constraint(name, "holidays", type_="check")
    op.drop_constraint("fk_holidays_rule", "holidays", type_="foreignkey")
    for column in ("row_version", "confirmed_at", "confirmed_by_user_id", "name_i18n", "sequence_index", "category",
                   "source_ref", "source", "observed_from_date", "observance", "status", "portion", "rule_id"):
        op.drop_column("holidays", column)
    op.drop_constraint("fk_holiday_calendar_exclusions_rule", "holiday_calendar_exclusions", type_="foreignkey")
    op.drop_table("holiday_rules")

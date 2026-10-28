"""LH-M1 calendar structure (LH 11 §4.1.3, §4.1.11, §4.2.13, §11.6 step 1; LH 14 WI-1.3; 04-holiday/01 §3, §4.2).

* ``holiday_calendars`` gains ``legal_entity_id`` (NULL = tenant-wide), ``weekend_days`` (NULL = derived by
  ``holiday_calendar_service.effective_weekend``, V3-013), ``employee_observance``, ``name_i18n``, ``description`` and
  ``row_version``.
* NEW ``holiday_calendar_includes`` (DAG of calendars, priority unique per calendar, 0 = mirror of
  ``parent_calendar_id``); trigger ``holiday_calendar_includes_no_cycle()`` rejects a cycle through includes or
  parents and a path deeper than ``MAX_CALENDAR_DEPTH`` (64) with ``ORG_CALENDAR_CYCLE``.
* NEW ``holiday_calendar_exclusions`` (one inherited occurrence, or one inherited rule from ``effective_from_year``;
  the rule FK arrives with ``holiday_rules`` in ``lh_m1_holiday_rules_years``).
* ``employee_assignments`` gains ``holiday_calendar_id`` (employee override, §4.2) and ``work_timezone`` (IANA).
* ``source_change_log.record_type`` accepts ``holiday_year`` and ``working_day`` (``work_week_version`` and
  ``calendar_link`` came with ``lh_m1_work_weeks_links``, ``shift_version`` / ``shift_rule`` with AS-M1).
* ``holiday_calendar_assignments`` already exists (``0037_lh_m1_work_weeks_links``, DECISIONS IG-LH-01).
* Data (zero diff, 11 §11.6 step 1): one include (child -> parent, priority 0) per ``parent_calendar_id``; every
  calendar stays tenant-wide; no override is set.

Downgrade drops the new tables and columns (refused while an exclusion, override or non-mirror include exists).

Revision ID: 0043_lh_m1_calendar_structure
Revises: 0042_as_m1_permissions
Create Date: 2026-10-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0043_lh_m1_calendar_structure"
down_revision: Union[str, None] = "0042_as_m1_permissions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies: later model changes must not alter what this revision does.
TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
MAX_CALENDAR_DEPTH = 64
OBSERVANCE = ("NONE", "NEXT_WORKING_DAY", "OBSERVED_IF_OFF")
OLD_RECORD_TYPES = ("attendance", "leave", "holiday", "lop_input", "suspension", "on_duty", "optional_holiday",
                    "overtime", "task", "wage_input", "assignment", "work_week_version", "calendar_link",
                    "shift_version", "shift_rule")
NEW_RECORD_TYPES = OLD_RECORD_TYPES + ("holiday_year", "working_day")

INCLUDES_TRIGGER = f"""
CREATE OR REPLACE FUNCTION holiday_calendar_includes_no_cycle() RETURNS trigger AS $$
DECLARE
    below integer;
    above integer;
BEGIN
    IF NEW.calendar_id = NEW.included_calendar_id THEN
        RAISE EXCEPTION 'ORG_CALENDAR_CYCLE: a calendar cannot include itself'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'holiday_calendar_includes_no_cycle';
    END IF;
    -- Serialise structure changes of one tenant's calendars, then walk includes + parents (the legacy tree).
    PERFORM 1 FROM holiday_calendars WHERE company_id = NEW.company_id AND id = NEW.calendar_id FOR UPDATE;
    WITH RECURSIVE edges(src, dst) AS (
        SELECT i.calendar_id, i.included_calendar_id FROM holiday_calendar_includes i
        WHERE i.company_id = NEW.company_id AND i.id IS DISTINCT FROM NEW.id
        UNION
        SELECT c.id, c.parent_calendar_id FROM holiday_calendars c
        WHERE c.company_id = NEW.company_id AND c.parent_calendar_id IS NOT NULL
    ), down(node, depth) AS (
        SELECT NEW.included_calendar_id, 1
        UNION
        SELECT e.dst, d.depth + 1 FROM down d JOIN edges e ON e.src = d.node WHERE d.depth <= {MAX_CALENDAR_DEPTH}
    )
    SELECT max(depth) INTO below FROM down;
    IF EXISTS (
        WITH RECURSIVE edges(src, dst) AS (
            SELECT i.calendar_id, i.included_calendar_id FROM holiday_calendar_includes i
            WHERE i.company_id = NEW.company_id AND i.id IS DISTINCT FROM NEW.id
            UNION
            SELECT c.id, c.parent_calendar_id FROM holiday_calendars c
            WHERE c.company_id = NEW.company_id AND c.parent_calendar_id IS NOT NULL
        ), down(node, depth) AS (
            SELECT NEW.included_calendar_id, 1
            UNION
            SELECT e.dst, d.depth + 1 FROM down d JOIN edges e ON e.src = d.node WHERE d.depth <= {MAX_CALENDAR_DEPTH}
        )
        SELECT 1 FROM down WHERE node = NEW.calendar_id
    ) THEN
        RAISE EXCEPTION 'ORG_CALENDAR_CYCLE: the include would create a cycle'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'holiday_calendar_includes_no_cycle';
    END IF;
    WITH RECURSIVE edges(src, dst) AS (
        SELECT i.calendar_id, i.included_calendar_id FROM holiday_calendar_includes i
        WHERE i.company_id = NEW.company_id AND i.id IS DISTINCT FROM NEW.id
        UNION
        SELECT c.id, c.parent_calendar_id FROM holiday_calendars c
        WHERE c.company_id = NEW.company_id AND c.parent_calendar_id IS NOT NULL
    ), up(node, depth) AS (
        SELECT NEW.calendar_id, 0
        UNION
        SELECT e.src, u.depth + 1 FROM up u JOIN edges e ON e.dst = u.node WHERE u.depth <= {MAX_CALENDAR_DEPTH}
    )
    SELECT max(depth) INTO above FROM up;
    IF coalesce(below, 1) + coalesce(above, 0) > {MAX_CALENDAR_DEPTH} THEN
        RAISE EXCEPTION 'ORG_CALENDAR_CYCLE: the include chain would be too deep (more than {MAX_CALENDAR_DEPTH})'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'holiday_calendar_includes_no_cycle';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


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

    # ---- holiday_calendars
    op.add_column("holiday_calendars", sa.Column("legal_entity_id", sa.Integer(), nullable=True))
    op.add_column("holiday_calendars", sa.Column("weekend_days", postgresql.JSONB(), nullable=True))
    op.add_column("holiday_calendars", sa.Column("employee_observance", sa.String(20), nullable=False,
                                                 server_default="NONE"))
    op.add_column("holiday_calendars", sa.Column("name_i18n", postgresql.JSONB(), nullable=False,
                                                 server_default=sa.text("'{}'::jsonb")))
    op.add_column("holiday_calendars", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("holiday_calendars", sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"))
    op.create_foreign_key("fk_holiday_calendars_legal_entity", "holiday_calendars", "legal_entities",
                          ["company_id", "legal_entity_id"], ["company_id", "id"])
    op.create_index("ix_holiday_calendars_legal_entity_id", "holiday_calendars", ["legal_entity_id"])
    op.create_check_constraint(
        "ck_holiday_calendars_weekend", "holiday_calendars",
        "weekend_days IS NULL OR (jsonb_typeof(weekend_days) = 'array' AND jsonb_array_length(weekend_days) BETWEEN 1 AND 3"
        " AND NOT jsonb_path_exists(weekend_days, '$[*] ? (@ < 0 || @ > 6)'))")
    op.create_check_constraint("ck_holiday_calendars_observance", "holiday_calendars",
                               f"employee_observance IN {OBSERVANCE}")
    op.create_check_constraint("ck_holiday_calendars_name_i18n", "holiday_calendars",
                               "jsonb_typeof(name_i18n) = 'object'")
    op.create_check_constraint("ck_holiday_calendars_description", "holiday_calendars",
                               "description IS NULL OR char_length(description) <= 2000")
    op.create_check_constraint("ck_holiday_calendars_row_version", "holiday_calendars", "row_version >= 1")

    # ---- holiday_calendar_includes
    op.create_table(
        "holiday_calendar_includes",
        *_common("holiday_calendar_includes"),
        sa.Column("calendar_id", sa.Integer(), nullable=False),
        sa.Column("included_calendar_id", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["company_id", "calendar_id"], ["holiday_calendars.company_id", "holiday_calendars.id"],
                                name="fk_holiday_calendar_includes_calendar", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id", "included_calendar_id"],
                                ["holiday_calendars.company_id", "holiday_calendars.id"],
                                name="fk_holiday_calendar_includes_included"),
        sa.UniqueConstraint("company_id", "calendar_id", "included_calendar_id",
                            name="uq_holiday_calendar_includes_pair"),
        sa.UniqueConstraint("company_id", "calendar_id", "priority", name="uq_holiday_calendar_includes_priority"),
        sa.CheckConstraint("calendar_id <> included_calendar_id", name="ck_holiday_calendar_includes_not_self"),
        sa.CheckConstraint("priority >= 0", name="ck_holiday_calendar_includes_priority"),
    )
    op.create_index("ix_holiday_calendar_includes_company_id", "holiday_calendar_includes", ["company_id"])
    op.create_index("ix_holiday_calendar_includes_included", "holiday_calendar_includes",
                    ["company_id", "included_calendar_id"])
    op.execute(INCLUDES_TRIGGER)
    op.execute("CREATE TRIGGER trg_holiday_calendar_includes_no_cycle BEFORE INSERT OR UPDATE ON "
               "holiday_calendar_includes FOR EACH ROW EXECUTE FUNCTION holiday_calendar_includes_no_cycle()")
    _secure(bind, "holiday_calendar_includes")

    # ---- holiday_calendar_exclusions
    op.create_table(
        "holiday_calendar_exclusions",
        *_common("holiday_calendar_exclusions"),
        sa.Column("calendar_id", sa.Integer(), nullable=False),
        sa.Column("holiday_rule_id", sa.Integer(), nullable=True),
        sa.Column("holiday_id", sa.Integer(), nullable=True),
        sa.Column("effective_from_year", sa.SmallInteger(), nullable=True),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["company_id", "calendar_id"], ["holiday_calendars.company_id", "holiday_calendars.id"],
                                name="fk_holiday_calendar_exclusions_calendar", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id", "holiday_id"], ["holidays.company_id", "holidays.id"],
                                name="fk_holiday_calendar_exclusions_holiday", ondelete="CASCADE"),
        sa.CheckConstraint("num_nonnulls(holiday_rule_id, holiday_id) = 1", name="ck_holiday_calendar_exclusions_target"),
        sa.CheckConstraint("effective_from_year IS NULL OR (holiday_rule_id IS NOT NULL "
                           "AND effective_from_year BETWEEN 2000 AND 2100)",
                           name="ck_holiday_calendar_exclusions_from_year"),
        sa.CheckConstraint("char_length(btrim(reason)) BETWEEN 1 AND 500", name="ck_holiday_calendar_exclusions_reason"),
    )
    op.create_index("ix_holiday_calendar_exclusions_company_id", "holiday_calendar_exclusions", ["company_id"])
    op.create_index("ix_holiday_calendar_exclusions_calendar", "holiday_calendar_exclusions",
                    ["company_id", "calendar_id"])
    op.create_index("uq_holiday_calendar_exclusions_holiday", "holiday_calendar_exclusions",
                    ["company_id", "calendar_id", "holiday_id"], unique=True,
                    postgresql_where=sa.text("holiday_id IS NOT NULL"))
    op.create_index("uq_holiday_calendar_exclusions_rule", "holiday_calendar_exclusions",
                    ["company_id", "calendar_id", "holiday_rule_id"], unique=True,
                    postgresql_where=sa.text("holiday_rule_id IS NOT NULL"))
    _secure(bind, "holiday_calendar_exclusions")

    # ---- employee_assignments: calendar override and work time zone
    op.add_column("employee_assignments", sa.Column("holiday_calendar_id", sa.Integer(), nullable=True))
    op.add_column("employee_assignments", sa.Column("work_timezone", sa.String(64), nullable=True))
    op.create_foreign_key("fk_employee_assignments_holiday_calendar", "employee_assignments", "holiday_calendars",
                          ["company_id", "holiday_calendar_id"], ["company_id", "id"])

    # ---- change-log record types
    op.drop_constraint("ck_source_change_log_record_type", "source_change_log", type_="check")
    op.create_check_constraint("ck_source_change_log_record_type", "source_change_log",
                               f"record_type IN {NEW_RECORD_TYPES}")

    # ---- data: the legacy parent becomes the priority-0 include (zero diff)
    bind.execute(sa.text(
        "INSERT INTO holiday_calendar_includes (company_id, calendar_id, included_calendar_id, priority) "
        "SELECT company_id, id, parent_calendar_id, 0 FROM holiday_calendars WHERE parent_calendar_id IS NOT NULL "
        "ORDER BY id"))


def downgrade() -> None:
    bind = op.get_bind()
    blockers = bind.execute(sa.text(
        "SELECT (SELECT count(*) FROM holiday_calendar_exclusions) "
        "+ (SELECT count(*) FROM employee_assignments WHERE holiday_calendar_id IS NOT NULL) "
        "+ (SELECT count(*) FROM holiday_calendar_includes i JOIN holiday_calendars c "
        "   ON c.company_id = i.company_id AND c.id = i.calendar_id "
        "   WHERE i.priority <> 0 OR c.parent_calendar_id IS DISTINCT FROM i.included_calendar_id)")).scalar()
    if blockers:
        raise RuntimeError("calendar includes, exclusions or employee calendar overrides exist that the previous "
                           "schema cannot hold; remove them before downgrading")
    leftover = bind.execute(sa.text("SELECT count(*) FROM source_change_log "
                                    "WHERE record_type IN ('holiday_year', 'working_day')")).scalar()
    if leftover:
        raise RuntimeError("source_change_log holds holiday-year or working-day changes (append-only history)")
    op.drop_constraint("ck_source_change_log_record_type", "source_change_log", type_="check")
    op.create_check_constraint("ck_source_change_log_record_type", "source_change_log",
                               f"record_type IN {OLD_RECORD_TYPES}")
    op.drop_constraint("fk_employee_assignments_holiday_calendar", "employee_assignments", type_="foreignkey")
    op.drop_column("employee_assignments", "work_timezone")
    op.drop_column("employee_assignments", "holiday_calendar_id")
    op.drop_table("holiday_calendar_exclusions")
    op.drop_table("holiday_calendar_includes")
    op.execute("DROP FUNCTION IF EXISTS holiday_calendar_includes_no_cycle()")
    for name in ("ck_holiday_calendars_row_version", "ck_holiday_calendars_description",
                 "ck_holiday_calendars_name_i18n", "ck_holiday_calendars_observance", "ck_holiday_calendars_weekend"):
        op.drop_constraint(name, "holiday_calendars", type_="check")
    op.drop_index("ix_holiday_calendars_legal_entity_id", table_name="holiday_calendars")
    op.drop_constraint("fk_holiday_calendars_legal_entity", "holiday_calendars", type_="foreignkey")
    for column in ("row_version", "description", "name_i18n", "employee_observance", "weekend_days",
                   "legal_entity_id"):
        op.drop_column("holiday_calendars", column)

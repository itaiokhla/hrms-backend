"""LH-M1 work weeks and effective-dated links (LH 11 §4.1.12, §4.2.14, §11.7; LH 14 WI-1.5; 04-holiday/03 §3.1, §5).

* ``work_week_patterns`` gains ``kind`` (WEEKLY/CYCLE), ``half_day_offs``, ``cycle_length_days``, ``cycle_off_days``,
  ``anchor_date``, ``rest_days``, ``name_i18n``, ``legal_entity_id`` (NULL = tenant-wide) and ``row_version``. The
  rule columns become a mirror of the version valid today; ``work_week_rules_valid`` is replaced by an
  eight-argument version that checks the shape of both kinds (V-1..V-8 of 04-holiday/03 §3.3-3.4).
* NEW ``work_week_pattern_versions`` (source of truth; trigger ``work_week_pattern_versions_no_overlap()`` serialises
  per pattern, rejects overlapping windows and any change of a version other than its ``effective_to``).
* NEW ``branch_calendar_links`` (trigger ``branch_calendar_links_no_overlap()``) and NEW
  ``holiday_calendar_assignments`` (trigger ``holiday_calendar_assignments_guard()``: two rows with a calendar, or
  two with a pattern, whose date ranges overlap never share a priority -> ``CALENDAR_ASSIGNMENT_CONFLICT``).
* ``source_change_log.record_type`` accepts ``work_week_version`` and ``calendar_link``.
* Data (zero-diff, 04-holiday/03 §5.8, 11 §11.7): version 1 per pattern from 2000-01-01 with today's rules and a
  content hash computed by the frozen function below; one link per branch from 2000-01-01; placements whose pattern
  equals what the branch (else the entity default) resolves to lose the copied id, so links and criteria rows can
  take effect while every date keeps the same pattern.

Order note: written before ``lh_m1_scheduler``/``lh_m1_calendar_structure``/``lh_m1_holiday_rules_years`` because the
owner asked for the work-week goal first (DECISIONS IG-LH-01); those revisions follow it in the chain and no longer
add the two change-log record types.

Downgrade drops the three tables and the new columns, restores the two-argument CHECK and copies the resolved
pattern back into placements without one (the pre-revision placement behaviour).

Revision ID: 0037_lh_m1_work_weeks_links
Revises: 0036_x_m3_saved_views
Create Date: 2026-10-27
"""
import hashlib
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0037_lh_m1_work_weeks_links"
down_revision: Union[str, None] = "0036_x_m3_saved_views"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies: later model changes must not alter what this revision does.
TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
OLD_RECORD_TYPES = ("attendance", "leave", "holiday", "lop_input", "suspension", "on_duty", "optional_holiday",
                    "overtime", "task", "wage_input", "assignment")
NEW_RECORD_TYPES = OLD_RECORD_TYPES + ("work_week_version", "calendar_link")
MIGRATED_FROM = "2000-01-01"
LINK_REASON = "Migrated: link applied to all dates before effective dating"

RULES_FUNCTION = r"""
CREATE OR REPLACE FUNCTION work_week_rules_valid(
    kind text, off_days jsonb, nth_rules jsonb, half_rules jsonb, cycle_len integer, cycle_off jsonb,
    anchor date, rest jsonb) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    rule jsonb;
    full_days int[] := '{}';
    nth_days int[] := '{}';
    half_days int[] := '{}';
    day_value int;
BEGIN
    IF jsonb_typeof(half_rules) IS DISTINCT FROM 'array' OR jsonb_typeof(off_days) IS DISTINCT FROM 'array'
       OR jsonb_typeof(nth_rules) IS DISTINCT FROM 'array' THEN
        RETURN false;
    END IF;
    IF rest IS NOT NULL AND jsonb_typeof(rest) IS DISTINCT FROM 'array' THEN
        RETURN false;
    END IF;
    IF kind = 'WEEKLY' THEN
        -- V-1/V-2: distinct weekdays 0..6, at most six full offs; no cycle columns.
        IF cycle_len IS NOT NULL OR cycle_off IS NOT NULL OR anchor IS NOT NULL THEN
            RETURN false;
        END IF;
        IF jsonb_array_length(off_days) > 6 THEN
            RETURN false;
        END IF;
        IF EXISTS (SELECT 1 FROM jsonb_array_elements(off_days) AS d(value) WHERE d.value::text !~ '^[0-6]$') THEN
            RETURN false;
        END IF;
        SELECT coalesce(array_agg(DISTINCT d.value::text::int), '{}') INTO full_days FROM jsonb_array_elements(off_days) AS d(value);
        IF cardinality(full_days) <> jsonb_array_length(off_days) THEN
            RETURN false;
        END IF;
        FOR rule IN SELECT value FROM jsonb_array_elements(nth_rules) LOOP
            IF jsonb_typeof(rule) <> 'object' OR coalesce((rule -> 'weekday')::text, '') !~ '^[0-6]$'
               OR jsonb_typeof(rule -> 'nth') IS DISTINCT FROM 'array' OR jsonb_array_length(rule -> 'nth') = 0 THEN
                RETURN false;
            END IF;
            IF EXISTS (SELECT 1 FROM jsonb_array_elements(rule -> 'nth') AS n(value) WHERE n.value::text !~ '^(-1|[1-5])$') THEN
                RETURN false;
            END IF;
            day_value := (rule ->> 'weekday')::int;
            -- V-3 redundant, duplicate weekday
            IF day_value = ANY (full_days) OR day_value = ANY (nth_days) THEN
                RETURN false;
            END IF;
            nth_days := nth_days || day_value;
        END LOOP;
        FOR rule IN SELECT value FROM jsonb_array_elements(half_rules) LOOP
            IF jsonb_typeof(rule) <> 'object' OR coalesce((rule -> 'weekday')::text, '') !~ '^[0-6]$'
               OR coalesce(rule ->> 'portion', '') NOT IN ('FIRST_HALF', 'SECOND_HALF') THEN
                RETURN false;
            END IF;
            day_value := (rule ->> 'weekday')::int;
            -- V-5 redundant, V-7 one half rule per weekday, V-6 half rule beside an nth rule lists nth
            IF day_value = ANY (full_days) OR day_value = ANY (half_days) THEN
                RETURN false;
            END IF;
            IF rule ? 'nth' THEN
                IF jsonb_typeof(rule -> 'nth') IS DISTINCT FROM 'array' OR jsonb_array_length(rule -> 'nth') = 0 THEN
                    RETURN false;
                END IF;
                IF EXISTS (SELECT 1 FROM jsonb_array_elements(rule -> 'nth') AS n(value) WHERE n.value::text !~ '^(-1|[1-5])$') THEN
                    RETURN false;
                END IF;
            ELSIF day_value = ANY (nth_days) THEN
                RETURN false;
            END IF;
            half_days := half_days || day_value;
        END LOOP;
        IF rest IS NOT NULL AND EXISTS (
            SELECT 1 FROM jsonb_array_elements(rest) AS r(value)
            WHERE r.value::text !~ '^[0-6]$' OR NOT (r.value::text::int = ANY (full_days || nth_days))) THEN
            RETURN false;
        END IF;
        RETURN true;
    ELSIF kind = 'CYCLE' THEN
        -- V-8: no weekly columns; length 2..56, anchor, offs distinct in 0..N-1, at least one working day.
        IF jsonb_array_length(off_days) <> 0 OR jsonb_array_length(nth_rules) <> 0 THEN
            RETURN false;
        END IF;
        IF cycle_len IS NULL OR cycle_len < 2 OR cycle_len > 56 OR anchor IS NULL
           OR jsonb_typeof(cycle_off) IS DISTINCT FROM 'array' THEN
            RETURN false;
        END IF;
        IF EXISTS (SELECT 1 FROM jsonb_array_elements(cycle_off) AS d(value)
                   WHERE d.value::text !~ '^[0-9]+$' OR d.value::text::int >= cycle_len) THEN
            RETURN false;
        END IF;
        SELECT coalesce(array_agg(DISTINCT d.value::text::int), '{}') INTO full_days FROM jsonb_array_elements(cycle_off) AS d(value);
        IF cardinality(full_days) <> jsonb_array_length(cycle_off) OR cardinality(full_days) >= cycle_len THEN
            RETURN false;
        END IF;
        FOR rule IN SELECT value FROM jsonb_array_elements(half_rules) LOOP
            IF jsonb_typeof(rule) <> 'object' OR coalesce((rule -> 'cycle_day')::text, '') !~ '^[0-9]+$'
               OR coalesce(rule ->> 'portion', '') NOT IN ('FIRST_HALF', 'SECOND_HALF') THEN
                RETURN false;
            END IF;
            day_value := (rule ->> 'cycle_day')::int;
            IF day_value >= cycle_len OR day_value = ANY (full_days) OR day_value = ANY (half_days) THEN
                RETURN false;
            END IF;
            half_days := half_days || day_value;
        END LOOP;
        IF rest IS NOT NULL AND EXISTS (
            SELECT 1 FROM jsonb_array_elements(rest) AS r(value)
            WHERE r.value::text !~ '^[0-9]+$' OR NOT (r.value::text::int = ANY (full_days))) THEN
            RETURN false;
        END IF;
        RETURN true;
    END IF;
    RETURN false;
END;
$$
"""

VERSIONS_TRIGGER = """
CREATE OR REPLACE FUNCTION work_week_pattern_versions_no_overlap() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND (NEW.pattern_id <> OLD.pattern_id OR NEW.version <> OLD.version
        OR NEW.rules <> OLD.rules OR NEW.effective_from <> OLD.effective_from OR NEW.content_hash <> OLD.content_hash
        OR NEW.company_id <> OLD.company_id) THEN
        RAISE EXCEPTION 'A work-week version is immutable except its end date'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'work_week_pattern_versions_immutable';
    END IF;
    -- Serialise version posts per pattern, then reject overlapping windows.
    PERFORM 1 FROM work_week_patterns WHERE id = NEW.pattern_id AND company_id = NEW.company_id FOR UPDATE;
    IF EXISTS (
        SELECT 1 FROM work_week_pattern_versions v
        WHERE v.company_id = NEW.company_id AND v.pattern_id = NEW.pattern_id AND v.id <> NEW.id
          AND daterange(v.effective_from, v.effective_to, '[]') && daterange(NEW.effective_from, NEW.effective_to, '[]')
    ) THEN
        RAISE EXCEPTION 'Versions of one work week cannot overlap'
            USING ERRCODE = 'exclusion_violation', CONSTRAINT = 'work_week_pattern_versions_no_overlap';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

LINKS_TRIGGER = """
CREATE OR REPLACE FUNCTION branch_calendar_links_no_overlap() RETURNS trigger AS $$
BEGIN
    PERFORM 1 FROM branches WHERE id = NEW.branch_id AND company_id = NEW.company_id FOR UPDATE;
    IF EXISTS (
        SELECT 1 FROM branch_calendar_links l
        WHERE l.company_id = NEW.company_id AND l.branch_id = NEW.branch_id AND l.id <> NEW.id
          AND daterange(l.effective_from, l.effective_to, '[]') && daterange(NEW.effective_from, NEW.effective_to, '[]')
    ) THEN
        RAISE EXCEPTION 'Calendar links of one branch cannot overlap'
            USING ERRCODE = 'exclusion_violation', CONSTRAINT = 'branch_calendar_links_no_overlap';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

ASSIGNMENTS_TRIGGER = """
CREATE OR REPLACE FUNCTION holiday_calendar_assignments_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.effective_to IS NOT NULL AND NEW.effective_to < NEW.effective_from THEN
        RAISE EXCEPTION 'An assignment ends before it starts' USING ERRCODE = 'check_violation';
    END IF;
    IF num_nonnulls(NEW.calendar_id, NEW.work_week_pattern_id) = 0 THEN
        RAISE EXCEPTION 'An assignment needs a calendar or a work week' USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (
        SELECT 1 FROM holiday_calendar_assignments a
        WHERE a.company_id = NEW.company_id AND a.id <> NEW.id AND a.priority = NEW.priority
          AND ((a.calendar_id IS NOT NULL AND NEW.calendar_id IS NOT NULL)
               OR (a.work_week_pattern_id IS NOT NULL AND NEW.work_week_pattern_id IS NOT NULL))
          AND daterange(a.effective_from, a.effective_to, '[]') && daterange(NEW.effective_from, NEW.effective_to, '[]')
    ) THEN
        RAISE EXCEPTION 'CALENDAR_ASSIGNMENT_CONFLICT: overlapping assignments share a priority'
            USING ERRCODE = 'exclusion_violation', CONSTRAINT = 'holiday_calendar_assignments_priority';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def _canonical_rules(off_days, nth_rules) -> dict:
    """Frozen copy of workweek.WorkWeekPattern.to_json for today's two-key WEEKLY rows."""
    return {
        "weekly_off_days": sorted(int(d) for d in (off_days or [])),
        "nth_weekday_offs": [{"weekday": int(r["weekday"]), "nth": sorted(int(n) for n in r["nth"])}
                             for r in sorted(nth_rules or [], key=lambda r: int(r["weekday"]))],
    }


def _hash(rules: dict) -> str:
    text = json.dumps(rules, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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

    # ---- work_week_patterns: kinds, ownership, concurrency
    op.add_column("work_week_patterns", sa.Column("kind", sa.String(6), nullable=False, server_default="WEEKLY"))
    op.add_column("work_week_patterns", sa.Column("half_day_offs", postgresql.JSONB(), nullable=False,
                                                  server_default=sa.text("'[]'::jsonb")))
    op.add_column("work_week_patterns", sa.Column("cycle_length_days", sa.Integer(), nullable=True))
    op.add_column("work_week_patterns", sa.Column("cycle_off_days", postgresql.JSONB(), nullable=True))
    op.add_column("work_week_patterns", sa.Column("anchor_date", sa.Date(), nullable=True))
    op.add_column("work_week_patterns", sa.Column("rest_days", postgresql.JSONB(), nullable=True))
    op.add_column("work_week_patterns", sa.Column("name_i18n", postgresql.JSONB(), nullable=False,
                                                  server_default=sa.text("'{}'::jsonb")))
    op.add_column("work_week_patterns", sa.Column("legal_entity_id", sa.Integer(), nullable=True))
    op.add_column("work_week_patterns", sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"))
    op.create_foreign_key("fk_work_week_patterns_legal_entity", "work_week_patterns", "legal_entities",
                          ["company_id", "legal_entity_id"], ["company_id", "id"])
    op.create_index("ix_work_week_patterns_legal_entity_id", "work_week_patterns", ["legal_entity_id"])
    op.create_check_constraint("ck_work_week_patterns_kind", "work_week_patterns", "kind IN ('WEEKLY', 'CYCLE')")
    op.create_check_constraint("ck_work_week_patterns_row_version", "work_week_patterns", "row_version >= 1")
    op.execute(RULES_FUNCTION)
    op.drop_constraint("ck_work_week_patterns_rules", "work_week_patterns", type_="check")
    op.create_check_constraint(
        "ck_work_week_patterns_rules", "work_week_patterns",
        "work_week_rules_valid(kind, weekly_off_days, nth_weekday_offs, half_day_offs, cycle_length_days, "
        "cycle_off_days, anchor_date, rest_days)")

    # ---- work_week_pattern_versions
    op.create_table(
        "work_week_pattern_versions",
        *_common("work_week_pattern_versions"),
        sa.Column("pattern_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("rules", postgresql.JSONB(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.UniqueConstraint("company_id", "pattern_id", "version", name="uq_work_week_pattern_versions_version"),
        sa.ForeignKeyConstraint(["company_id", "pattern_id"], ["work_week_patterns.company_id", "work_week_patterns.id"],
                                name="fk_work_week_pattern_versions_pattern", ondelete="CASCADE"),
        sa.CheckConstraint("version >= 1", name="ck_work_week_pattern_versions_version"),
        sa.CheckConstraint("effective_to IS NULL OR effective_to >= effective_from",
                           name="ck_work_week_pattern_versions_dates"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_work_week_pattern_versions_hash"),
        sa.CheckConstraint("jsonb_typeof(rules) = 'object'", name="ck_work_week_pattern_versions_rules"),
    )
    op.create_index("ix_work_week_pattern_versions_company_id", "work_week_pattern_versions", ["company_id"])
    op.create_index("ix_work_week_pattern_versions_pattern", "work_week_pattern_versions",
                    ["company_id", "pattern_id", "effective_from"])
    op.execute(VERSIONS_TRIGGER)
    op.execute("CREATE TRIGGER trg_work_week_pattern_versions_no_overlap BEFORE INSERT OR UPDATE ON "
               "work_week_pattern_versions FOR EACH ROW EXECUTE FUNCTION work_week_pattern_versions_no_overlap()")
    _secure(bind, "work_week_pattern_versions")

    # ---- branch_calendar_links
    op.create_table(
        "branch_calendar_links",
        *_common("branch_calendar_links"),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        sa.Column("holiday_calendar_id", sa.Integer(), nullable=True),
        sa.Column("work_week_pattern_id", sa.Integer(), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("reason", sa.String(300), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["company_id", "branch_id"], ["branches.company_id", "branches.id"],
                                name="fk_branch_calendar_links_branch"),
        sa.ForeignKeyConstraint(["company_id", "holiday_calendar_id"],
                                ["holiday_calendars.company_id", "holiday_calendars.id"],
                                name="fk_branch_calendar_links_calendar"),
        sa.ForeignKeyConstraint(["company_id", "work_week_pattern_id"],
                                ["work_week_patterns.company_id", "work_week_patterns.id"],
                                name="fk_branch_calendar_links_work_week"),
        sa.CheckConstraint("effective_to IS NULL OR effective_to >= effective_from",
                           name="ck_branch_calendar_links_dates"),
        sa.CheckConstraint("row_version >= 1", name="ck_branch_calendar_links_row_version"),
    )
    op.create_index("ix_branch_calendar_links_company_id", "branch_calendar_links", ["company_id"])
    op.create_index("ix_branch_calendar_links_branch_id", "branch_calendar_links", ["branch_id"])
    op.create_index("ix_branch_calendar_links_branch", "branch_calendar_links",
                    ["company_id", "branch_id", "effective_from"])
    op.create_index("ix_branch_calendar_links_work_week", "branch_calendar_links",
                    ["company_id", "work_week_pattern_id"])
    op.execute(LINKS_TRIGGER)
    op.execute("CREATE TRIGGER trg_branch_calendar_links_no_overlap BEFORE INSERT OR UPDATE ON branch_calendar_links "
               "FOR EACH ROW EXECUTE FUNCTION branch_calendar_links_no_overlap()")
    _secure(bind, "branch_calendar_links")

    # ---- holiday_calendar_assignments
    op.create_table(
        "holiday_calendar_assignments",
        *_common("holiday_calendar_assignments"),
        sa.Column("calendar_id", sa.Integer(), nullable=True),
        sa.Column("work_week_pattern_id", sa.Integer(), nullable=True),
        sa.Column("criteria", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("legal_entity_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["company_id", "calendar_id"], ["holiday_calendars.company_id", "holiday_calendars.id"],
                                name="fk_holiday_calendar_assignments_calendar"),
        sa.ForeignKeyConstraint(["company_id", "work_week_pattern_id"],
                                ["work_week_patterns.company_id", "work_week_patterns.id"],
                                name="fk_holiday_calendar_assignments_work_week"),
        sa.ForeignKeyConstraint(["company_id", "legal_entity_id"], ["legal_entities.company_id", "legal_entities.id"],
                                name="fk_holiday_calendar_assignments_legal_entity"),
        sa.CheckConstraint("num_nonnulls(calendar_id, work_week_pattern_id) >= 1",
                           name="ck_holiday_calendar_assignments_target"),
        sa.CheckConstraint("priority BETWEEN 1 AND 999", name="ck_holiday_calendar_assignments_priority"),
        sa.CheckConstraint("effective_to IS NULL OR effective_to >= effective_from",
                           name="ck_holiday_calendar_assignments_dates"),
        sa.CheckConstraint("jsonb_typeof(criteria) = 'object'", name="ck_holiday_calendar_assignments_criteria"),
        sa.CheckConstraint("row_version >= 1", name="ck_holiday_calendar_assignments_row_version"),
    )
    op.create_index("ix_holiday_calendar_assignments_company_id", "holiday_calendar_assignments", ["company_id"])
    op.create_index("ix_holiday_calendar_assignments_dates", "holiday_calendar_assignments",
                    ["company_id", "effective_from"])
    op.execute(ASSIGNMENTS_TRIGGER)
    op.execute("CREATE TRIGGER trg_holiday_calendar_assignments_guard BEFORE INSERT OR UPDATE ON "
               "holiday_calendar_assignments FOR EACH ROW EXECUTE FUNCTION holiday_calendar_assignments_guard()")
    _secure(bind, "holiday_calendar_assignments")

    # ---- change-log record types
    op.drop_constraint("ck_source_change_log_record_type", "source_change_log", type_="check")
    op.create_check_constraint("ck_source_change_log_record_type", "source_change_log",
                               f"record_type IN {NEW_RECORD_TYPES}")

    # ---- data: version 1 per pattern, one link per branch, copied placement ids removed (zero diff)
    patterns = bind.execute(sa.text(
        "SELECT id, company_id, weekly_off_days, nth_weekday_offs FROM work_week_patterns ORDER BY id")).all()
    for pattern_id, company_id, off_days, nth_rules in patterns:
        rules = _canonical_rules(off_days, nth_rules)
        bind.execute(sa.text(
            "INSERT INTO work_week_pattern_versions (company_id, pattern_id, version, rules, effective_from, "
            "effective_to, reason, content_hash) VALUES (:cid, :pid, 1, CAST(:rules AS jsonb), :start, NULL, NULL, :hash)"),
            {"cid": company_id, "pid": pattern_id, "rules": json.dumps(rules), "start": MIGRATED_FROM,
             "hash": _hash(rules)})
    bind.execute(sa.text(
        "INSERT INTO branch_calendar_links (company_id, branch_id, holiday_calendar_id, work_week_pattern_id, "
        "effective_from, effective_to, reason) "
        "SELECT company_id, id, holiday_calendar_id, work_week_pattern_id, :start, NULL, :reason FROM branches "
        "WHERE holiday_calendar_id IS NOT NULL OR work_week_pattern_id IS NOT NULL ORDER BY id"),
        {"start": MIGRATED_FROM, "reason": LINK_REASON})
    bind.execute(sa.text(
        "UPDATE employee_assignments a SET work_week_pattern_id = NULL "
        "FROM branches b JOIN legal_entities le ON le.id = b.legal_entity_id AND le.company_id = b.company_id "
        "WHERE b.id = a.branch_id AND b.company_id = a.company_id AND a.work_week_pattern_id IS NOT NULL "
        "AND a.work_week_pattern_id = coalesce(b.work_week_pattern_id, le.default_work_week_pattern_id)"))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(
        "UPDATE employee_assignments a SET work_week_pattern_id = coalesce(b.work_week_pattern_id, "
        "le.default_work_week_pattern_id) "
        "FROM branches b JOIN legal_entities le ON le.id = b.legal_entity_id AND le.company_id = b.company_id "
        "WHERE b.id = a.branch_id AND b.company_id = a.company_id AND a.work_week_pattern_id IS NULL"))
    cycles = bind.execute(sa.text("SELECT count(*) FROM work_week_patterns WHERE kind <> 'WEEKLY' "
                                  "OR half_day_offs <> '[]'::jsonb OR rest_days IS NOT NULL")).scalar()
    if cycles:
        raise RuntimeError(f"{cycles} work weeks use cycles, half-day offs or rest days, which the previous schema "
                           "cannot hold; change or remove them before downgrading")
    # source_change_log is append-only: rows of the new types are never deleted, they block the downgrade.
    leftover = bind.execute(sa.text("SELECT count(*) FROM source_change_log "
                                    "WHERE record_type IN ('work_week_version', 'calendar_link')")).scalar()
    if leftover:
        raise RuntimeError("source_change_log holds work-week or calendar-link changes (append-only history); "
                           "downgrading would invalidate them")
    op.drop_constraint("ck_source_change_log_record_type", "source_change_log", type_="check")
    op.create_check_constraint("ck_source_change_log_record_type", "source_change_log",
                               f"record_type IN {OLD_RECORD_TYPES}")
    op.drop_table("holiday_calendar_assignments")
    op.drop_table("branch_calendar_links")
    op.drop_table("work_week_pattern_versions")
    op.execute("DROP FUNCTION IF EXISTS holiday_calendar_assignments_guard()")
    op.execute("DROP FUNCTION IF EXISTS branch_calendar_links_no_overlap()")
    op.execute("DROP FUNCTION IF EXISTS work_week_pattern_versions_no_overlap()")
    op.drop_constraint("ck_work_week_patterns_rules", "work_week_patterns", type_="check")
    op.create_check_constraint("ck_work_week_patterns_rules", "work_week_patterns",
                               "work_week_rules_valid(weekly_off_days, nth_weekday_offs)")
    op.execute("DROP FUNCTION IF EXISTS work_week_rules_valid(text, jsonb, jsonb, jsonb, integer, jsonb, date, jsonb)")
    op.drop_constraint("ck_work_week_patterns_row_version", "work_week_patterns", type_="check")
    op.drop_constraint("ck_work_week_patterns_kind", "work_week_patterns", type_="check")
    op.drop_index("ix_work_week_patterns_legal_entity_id", table_name="work_week_patterns")
    op.drop_constraint("fk_work_week_patterns_legal_entity", "work_week_patterns", type_="foreignkey")
    for column in ("row_version", "legal_entity_id", "name_i18n", "rest_days", "anchor_date", "cycle_off_days",
                   "cycle_length_days", "half_day_offs", "kind"):
        op.drop_column("work_week_patterns", column)

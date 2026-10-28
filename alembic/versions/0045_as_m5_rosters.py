"""AS-M5 rotations and rosters (AS 11 §4.8-§4.9; 03-shifts/02; D-AS-16, D-AS-18).

NEW tenant tables (RLS, grants, composite same-company foreign keys; app/models/rosters.py documents them):
``rotation_patterns``, ``rotation_pattern_versions``, ``rotation_assignments``, ``roster_periods``, ``roster_drafts``,
``roster_publications`` (append-only), ``roster_entries``, ``roster_entry_changes`` (append-only).

* Trigger ``roster_periods_no_overlap()``: two non-cancelled periods of the same owner (team, else branch) never share
  dates (23P01, constraint ``roster_periods_no_overlap`` -> 409 ``ROSTER_PERIOD_OVERLAP``).
* Triggers ``roster_publications_append_only()`` / ``roster_entry_changes_append_only()`` refuse UPDATE and DELETE;
  the application role also loses those privileges (app/cli.py ``grant_app_role``).
* ``source_change_log.record_type`` accepts ``roster_entry`` (added to the values the CHECK holds at upgrade time, so
  concurrent revisions that widen it keep their values).

No rows are created: tenants without rosters resolve shifts exactly as before (zero diff). Downgrade refuses while
any roster entry exists (export first), then drops the tables.

Revision ID: 0045_as_m5_rosters
Revises: 0044_lh_m1_holiday_rules_years
Create Date: 2026-10-27
"""
import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0045_as_m5_rosters"
down_revision: Union[str, None] = "0044_lh_m1_holiday_rules_years"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
BASE = ("id SERIAL PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE RESTRICT, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()")
SOURCES = "('ROTATION','MANUAL','COPY','IMPORT','DAY_OVERRIDE','SUBSTITUTION','SWAP','DROP','PICKUP','CHANGE','HOLIDAY_RULE')"
WORK_MODES = "('ON_SITE','REMOTE','FIELD')"


def _fk(table: str, column: str, target: str, name: str) -> str:
    return (f"CONSTRAINT fk_{table}_{name} FOREIGN KEY (company_id, {column}) "
            f"REFERENCES {target} (company_id, id)")


TABLES = {
    "rotation_patterns": f"""
        {BASE}, code VARCHAR(40) NOT NULL, name VARCHAR(100) NOT NULL, template_key VARCHAR(40),
        legal_entity_id INTEGER, branch_id INTEGER, status VARCHAR(10) NOT NULL DEFAULT 'ACTIVE',
        current_version_id INTEGER, row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_rotation_patterns_company_id_id UNIQUE (company_id, id),
        CONSTRAINT uq_rotation_patterns_code UNIQUE (company_id, code),
        {_fk('rotation_patterns', 'legal_entity_id', 'legal_entities', 'legal_entity')},
        {_fk('rotation_patterns', 'branch_id', 'branches', 'branch')},
        CONSTRAINT ck_rotation_patterns_status CHECK (status IN ('ACTIVE','ARCHIVED')),
        CONSTRAINT ck_rotation_patterns_row_version CHECK (row_version >= 1)""",
    "rotation_pattern_versions": f"""
        {BASE}, pattern_id INTEGER NOT NULL, version_no INTEGER NOT NULL,
        status VARCHAR(12) NOT NULL DEFAULT 'DRAFT', cycle_days SMALLINT NOT NULL, tokens VARCHAR(366),
        crew_rows JSONB, crew_offsets JSONB, token_map JSONB NOT NULL, coverage JSONB, week_anchor_weekday SMALLINT,
        lints_muted TEXT[] NOT NULL DEFAULT '{{}}', content_hash CHAR(64) NOT NULL, activated_at TIMESTAMPTZ,
        row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_rotation_pattern_versions_company_id_id UNIQUE (company_id, id),
        CONSTRAINT uq_rotation_pattern_versions_version UNIQUE (company_id, pattern_id, version_no),
        {_fk('rotation_pattern_versions', 'pattern_id', 'rotation_patterns', 'pattern')},
        CONSTRAINT ck_rotation_pattern_versions_status CHECK (status IN ('DRAFT','ACTIVE','SUPERSEDED')),
        CONSTRAINT ck_rotation_pattern_versions_cycle CHECK (cycle_days BETWEEN 1 AND 366),
        CONSTRAINT ck_rotation_pattern_versions_tokens CHECK (tokens IS NULL OR length(tokens) = cycle_days),
        CONSTRAINT ck_rotation_pattern_versions_shape CHECK (num_nonnulls(tokens, crew_rows) = 1),
        CONSTRAINT ck_rotation_pattern_versions_weekday CHECK (week_anchor_weekday IS NULL OR week_anchor_weekday BETWEEN 0 AND 6),
        CONSTRAINT ck_rotation_pattern_versions_token_map CHECK (jsonb_typeof(token_map) = 'object'),
        CONSTRAINT ck_rotation_pattern_versions_hash CHECK (content_hash ~ '^[0-9a-f]{{64}}$'),
        CONSTRAINT ck_rotation_pattern_versions_row_version CHECK (row_version >= 1)""",
    "rotation_assignments": f"""
        {BASE}, pattern_version_id INTEGER NOT NULL, target_kind VARCHAR(8) NOT NULL DEFAULT 'EMPLOYEE',
        employee_id INTEGER, team_id INTEGER, crew_code VARCHAR(8), offset_days SMALLINT NOT NULL DEFAULT 0,
        anchor_date DATE NOT NULL, effective_from DATE NOT NULL, effective_to DATE,
        auto_publish BOOLEAN NOT NULL DEFAULT false, status VARCHAR(8) NOT NULL DEFAULT 'ACTIVE',
        row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_rotation_assignments_company_id_id UNIQUE (company_id, id),
        {_fk('rotation_assignments', 'pattern_version_id', 'rotation_pattern_versions', 'version')},
        {_fk('rotation_assignments', 'employee_id', 'employees', 'employee')},
        CONSTRAINT ck_rotation_assignments_target_kind CHECK (target_kind IN ('EMPLOYEE','TEAM')),
        CONSTRAINT ck_rotation_assignments_target CHECK ((target_kind = 'EMPLOYEE') = (employee_id IS NOT NULL AND team_id IS NULL)),
        CONSTRAINT ck_rotation_assignments_offset CHECK (offset_days >= 0),
        CONSTRAINT ck_rotation_assignments_dates CHECK (effective_to IS NULL OR effective_to >= effective_from),
        CONSTRAINT ck_rotation_assignments_status CHECK (status IN ('ACTIVE','ENDED')),
        CONSTRAINT ck_rotation_assignments_row_version CHECK (row_version >= 1)""",
    "roster_periods": f"""
        {BASE}, code VARCHAR(40) NOT NULL, name VARCHAR(100) NOT NULL, team_id INTEGER, branch_id INTEGER,
        department_ids INTEGER[], start_date DATE NOT NULL, end_date DATE NOT NULL,
        status VARCHAR(10) NOT NULL DEFAULT 'DRAFT', publish_due_at TIMESTAMPTZ, last_published_at TIMESTAMPTZ,
        last_publication_id INTEGER, requires_approval BOOLEAN NOT NULL DEFAULT false, approval_request_id INTEGER,
        row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_roster_periods_company_id_id UNIQUE (company_id, id),
        CONSTRAINT uq_roster_periods_code UNIQUE (company_id, code),
        {_fk('roster_periods', 'branch_id', 'branches', 'branch')},
        CONSTRAINT ck_roster_periods_dates CHECK (end_date >= start_date AND end_date - start_date <= 61),
        CONSTRAINT ck_roster_periods_status CHECK (status IN ('DRAFT','PUBLISHED','AMENDING','CLOSED','CANCELLED')),
        CONSTRAINT ck_roster_periods_owner CHECK (team_id IS NOT NULL OR branch_id IS NOT NULL),
        CONSTRAINT ck_roster_periods_row_version CHECK (row_version >= 1)""",
    "roster_publications": f"""
        {BASE}, roster_period_id INTEGER, kind VARCHAR(14) NOT NULL, scope JSONB NOT NULL,
        published_by_user_id INTEGER, published_at TIMESTAMPTZ NOT NULL,
        entries_created INTEGER NOT NULL DEFAULT 0, entries_updated INTEGER NOT NULL DEFAULT 0,
        entries_withdrawn INTEGER NOT NULL DEFAULT 0, snapshot_hash CHAR(64) NOT NULL, min_notice_hours NUMERIC(8,2),
        warnings JSONB NOT NULL DEFAULT '[]', approval_request_id INTEGER,
        CONSTRAINT uq_roster_publications_company_id_id UNIQUE (company_id, id),
        {_fk('roster_publications', 'roster_period_id', 'roster_periods', 'period')},
        {_fk('roster_publications', 'published_by_user_id', 'users', 'published_by')},
        CONSTRAINT ck_roster_publications_kind CHECK (kind IN ('PERIOD','DIRECT','REQUEST','SUBSTITUTION','ROTATION_AUTO')),
        CONSTRAINT ck_roster_publications_counts CHECK (entries_created >= 0 AND entries_updated >= 0 AND entries_withdrawn >= 0),
        CONSTRAINT ck_roster_publications_hash CHECK (snapshot_hash ~ '^[0-9a-f]{{64}}$')""",
    "roster_entries": f"""
        {BASE}, employee_id INTEGER NOT NULL, shift_date DATE NOT NULL, entry_seq SMALLINT NOT NULL DEFAULT 1,
        kind VARCHAR(6) NOT NULL, shift_id INTEGER, shift_version_id INTEGER, custom_start_time VARCHAR(5),
        custom_end_time VARCHAR(5), custom_segments JSONB, site_id INTEGER, planned_work_mode VARCHAR(10),
        planned_shift_id INTEGER, source VARCHAR(14) NOT NULL, roster_period_id INTEGER,
        publication_id INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 1, published_at TIMESTAMPTZ NOT NULL,
        published_by_user_id INTEGER, acknowledged_at TIMESTAMPTZ, withdrawn_at TIMESTAMPTZ, pair_id UUID,
        request_id INTEGER, team_id INTEGER, note VARCHAR(500), row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_roster_entries_company_id_id UNIQUE (company_id, id),
        {_fk('roster_entries', 'employee_id', 'employees', 'employee')},
        {_fk('roster_entries', 'shift_id', 'shifts', 'shift')},
        {_fk('roster_entries', 'shift_version_id', 'shift_versions', 'shift_version')},
        {_fk('roster_entries', 'planned_shift_id', 'shifts', 'planned_shift')},
        {_fk('roster_entries', 'roster_period_id', 'roster_periods', 'period')},
        {_fk('roster_entries', 'publication_id', 'roster_publications', 'publication')},
        CONSTRAINT ck_roster_entries_seq CHECK (entry_seq BETWEEN 1 AND 3),
        CONSTRAINT ck_roster_entries_kind CHECK (kind IN ('WORK','OFF')),
        CONSTRAINT ck_roster_entries_off CHECK (kind = 'WORK' OR (shift_id IS NULL AND custom_start_time IS NULL)),
        CONSTRAINT ck_roster_entries_work CHECK (kind = 'OFF' OR shift_id IS NOT NULL OR custom_start_time IS NOT NULL),
        CONSTRAINT ck_roster_entries_work_mode CHECK (planned_work_mode IS NULL OR planned_work_mode IN {WORK_MODES}),
        CONSTRAINT ck_roster_entries_source CHECK (source IN {SOURCES}),
        CONSTRAINT ck_roster_entries_version CHECK (version >= 1),
        CONSTRAINT ck_roster_entries_row_version CHECK (row_version >= 1)""",
    "roster_drafts": f"""
        {BASE}, roster_period_id INTEGER NOT NULL, employee_id INTEGER NOT NULL, shift_date DATE NOT NULL,
        entry_seq SMALLINT NOT NULL DEFAULT 1, action VARCHAR(6) NOT NULL DEFAULT 'UPSERT', kind VARCHAR(6),
        shift_id INTEGER, custom_start_time VARCHAR(5), custom_end_time VARCHAR(5), custom_segments JSONB,
        site_id INTEGER, planned_work_mode VARCHAR(10), planned_shift_id INTEGER,
        source VARCHAR(14) NOT NULL DEFAULT 'MANUAL', base_entry_id INTEGER, base_entry_version INTEGER,
        note VARCHAR(500), reason VARCHAR(500), row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_roster_drafts_company_id_id UNIQUE (company_id, id),
        CONSTRAINT uq_roster_drafts_cell UNIQUE (company_id, roster_period_id, employee_id, shift_date, entry_seq),
        {_fk('roster_drafts', 'roster_period_id', 'roster_periods', 'period')},
        {_fk('roster_drafts', 'employee_id', 'employees', 'employee')},
        {_fk('roster_drafts', 'shift_id', 'shifts', 'shift')},
        {_fk('roster_drafts', 'planned_shift_id', 'shifts', 'planned_shift')},
        {_fk('roster_drafts', 'base_entry_id', 'roster_entries', 'base_entry')},
        CONSTRAINT ck_roster_drafts_seq CHECK (entry_seq BETWEEN 1 AND 3),
        CONSTRAINT ck_roster_drafts_action CHECK (action IN ('UPSERT','DELETE')),
        CONSTRAINT ck_roster_drafts_kind CHECK (kind IS NULL OR kind IN ('WORK','OFF')),
        CONSTRAINT ck_roster_drafts_upsert_kind CHECK (action = 'DELETE' OR kind IS NOT NULL),
        CONSTRAINT ck_roster_drafts_work_mode CHECK (planned_work_mode IS NULL OR planned_work_mode IN {WORK_MODES}),
        CONSTRAINT ck_roster_drafts_planned_shift CHECK (planned_shift_id IS NULL OR (kind = 'OFF' AND source = 'HOLIDAY_RULE')),
        CONSTRAINT ck_roster_drafts_source CHECK (source IN {SOURCES}),
        CONSTRAINT ck_roster_drafts_row_version CHECK (row_version >= 1)""",
    "roster_entry_changes": f"""
        {BASE}, publication_id INTEGER NOT NULL, entry_id INTEGER, employee_id INTEGER NOT NULL,
        shift_date DATE NOT NULL, entry_seq SMALLINT NOT NULL, change_kind VARCHAR(10) NOT NULL, before JSONB,
        after JSONB, notice_hours NUMERIC(8,2), initiated_by VARCHAR(8) NOT NULL DEFAULT 'EMPLOYER',
        reason_code VARCHAR(40), request_id INTEGER, consent_id INTEGER,
        CONSTRAINT uq_roster_entry_changes_company_id_id UNIQUE (company_id, id),
        {_fk('roster_entry_changes', 'publication_id', 'roster_publications', 'publication')},
        {_fk('roster_entry_changes', 'entry_id', 'roster_entries', 'entry')},
        {_fk('roster_entry_changes', 'employee_id', 'employees', 'employee')},
        CONSTRAINT ck_roster_entry_changes_kind CHECK (change_kind IN ('CREATED','UPDATED','WITHDRAWN')),
        CONSTRAINT ck_roster_entry_changes_created CHECK (change_kind <> 'CREATED' OR before IS NULL),
        CONSTRAINT ck_roster_entry_changes_withdrawn CHECK (change_kind <> 'WITHDRAWN' OR after IS NULL),
        CONSTRAINT ck_roster_entry_changes_initiator CHECK (initiated_by IN ('EMPLOYER','EMPLOYEE','SYSTEM'))""",
}
INDEXES = (
    "CREATE INDEX ix_rotation_assignments_status ON rotation_assignments (company_id, status, effective_from)",
    "CREATE INDEX ix_rotation_assignments_employee_id ON rotation_assignments (employee_id)",
    "CREATE INDEX ix_rotation_pattern_versions_pattern_id ON rotation_pattern_versions (pattern_id)",
    "CREATE INDEX ix_rotation_assignments_pattern_version_id ON rotation_assignments (pattern_version_id)",
    "CREATE INDEX ix_roster_drafts_roster_period_id ON roster_drafts (roster_period_id)",
    "CREATE INDEX ix_roster_publications_roster_period_id ON roster_publications (roster_period_id)",
    "CREATE INDEX ix_roster_entry_changes_publication_id ON roster_entry_changes (publication_id)",
    "CREATE UNIQUE INDEX uq_roster_entries_active ON roster_entries (company_id, employee_id, shift_date, entry_seq) "
    "WHERE withdrawn_at IS NULL",
    "CREATE INDEX ix_roster_entries_date_team ON roster_entries (company_id, shift_date, team_id)",
    "CREATE INDEX ix_roster_entry_changes_employee_date ON roster_entry_changes (company_id, employee_id, shift_date)",
)
LATE_FKS = (
    "ALTER TABLE rotation_patterns ADD CONSTRAINT fk_rotation_patterns_current_version FOREIGN KEY "
    "(company_id, current_version_id) REFERENCES rotation_pattern_versions (company_id, id)",
    "ALTER TABLE roster_periods ADD CONSTRAINT fk_roster_periods_last_publication FOREIGN KEY "
    "(company_id, last_publication_id) REFERENCES roster_publications (company_id, id)",
)

PERIOD_OVERLAP = """
CREATE OR REPLACE FUNCTION roster_periods_no_overlap() RETURNS trigger AS $$
BEGIN
    IF NEW.status = 'CANCELLED' THEN
        RETURN NEW;
    END IF;
    IF EXISTS (
        SELECT 1 FROM roster_periods p
        WHERE p.company_id = NEW.company_id AND p.id <> NEW.id AND p.status <> 'CANCELLED'
          AND ((NEW.team_id IS NOT NULL AND p.team_id = NEW.team_id)
               OR (NEW.team_id IS NULL AND p.team_id IS NULL AND p.branch_id = NEW.branch_id))
          AND daterange(p.start_date, p.end_date, '[]') && daterange(NEW.start_date, NEW.end_date, '[]')
    ) THEN
        RAISE EXCEPTION 'Roster periods of one owner cannot overlap'
            USING ERRCODE = 'exclusion_violation', CONSTRAINT = 'roster_periods_no_overlap';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""
APPEND_ONLY = """
CREATE OR REPLACE FUNCTION {table}_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '{table} is append-only' USING ERRCODE = 'check_violation', CONSTRAINT = '{table}_append_only';
END;
$$ LANGUAGE plpgsql;
"""


def _record_types(bind) -> list:
    definition = bind.execute(sa.text(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_source_change_log_record_type'"
    )).scalar() or ""
    return re.findall(r"'([a-z_]+)'", definition)


def _set_record_types(values) -> None:
    op.drop_constraint("ck_source_change_log_record_type", "source_change_log", type_="check")
    op.create_check_constraint("ck_source_change_log_record_type", "source_change_log",
                               "record_type IN (" + ", ".join(f"'{v}'" for v in values) + ")")


def upgrade() -> None:
    bind = op.get_bind()
    for table, body in TABLES.items():
        op.execute(f"CREATE TABLE {table} ({body})")
        op.execute(f"CREATE INDEX ix_{table}_company_id ON {table} (company_id)")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")
    for statement in INDEXES + LATE_FKS:
        op.execute(statement)
    op.execute(PERIOD_OVERLAP)
    op.execute("CREATE TRIGGER trg_roster_periods_no_overlap BEFORE INSERT OR UPDATE ON roster_periods "
               "FOR EACH ROW EXECUTE FUNCTION roster_periods_no_overlap()")
    for table in ("roster_publications", "roster_entry_changes"):
        op.execute(APPEND_ONLY.format(table=table))
        op.execute(f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                   f"FOR EACH ROW EXECUTE FUNCTION {table}_append_only()")
    roles = [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"))]
    for role in roles:
        quoted = '"' + role.replace('"', '""') + '"'
        for table in TABLES:
            privileges = "SELECT, INSERT" if table in ("roster_publications", "roster_entry_changes") \
                else "SELECT, INSERT, UPDATE, DELETE"
            op.execute(f"GRANT {privileges} ON {table} TO {quoted}")
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}")
    values = _record_types(bind)
    if "roster_entry" not in values:
        _set_record_types(values + ["roster_entry"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT count(*) FROM roster_entries")).scalar():
        raise RuntimeError("roster entries exist; export them before downgrading")
    if bind.execute(sa.text("SELECT count(*) FROM source_change_log WHERE record_type = 'roster_entry'")).scalar():
        raise RuntimeError("source_change_log holds roster_entry rows; export them before downgrading")
    _set_record_types([v for v in _record_types(bind) if v != "roster_entry"])
    op.execute("ALTER TABLE roster_periods DROP CONSTRAINT fk_roster_periods_last_publication")
    op.execute("ALTER TABLE rotation_patterns DROP CONSTRAINT fk_rotation_patterns_current_version")
    for table in ("roster_drafts", "roster_entry_changes", "roster_entries", "roster_publications", "roster_periods",
                  "rotation_assignments", "rotation_pattern_versions", "rotation_patterns"):
        op.execute(f"DROP TABLE {table}")
    for function in ("roster_periods_no_overlap", "roster_publications_append_only",
                     "roster_entry_changes_append_only"):
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")

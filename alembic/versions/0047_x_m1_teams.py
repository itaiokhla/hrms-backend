"""X-M1 teams and dated memberships, role-assignment windows (X 01 §3, §6.7, §17; spine §3.9, §4.11; D-X-02, D-X-07).

NEW tenant tables (RLS, grants, composite same-company foreign keys; app/models/teams.py documents them):
``teams``, ``team_parent_history``, ``team_memberships``.

* Trigger ``teams_no_cycle()``: a parent link never closes a cycle (depth <= 64) -> 409 ``TEAM_CYCLE``.
* Trigger ``team_memberships_guard()``: no overlapping rows of one (employee, team) -> 409 ``MEMBERSHIP_OVERLAP``;
  at most one primary team per employee per date -> 409 ``PRIMARY_TEAM_CONFLICT``; at most one routing lead per team
  per date -> 409 ``ROUTING_LEAD_CONFLICT``. Cancelled rows (``effective_to = effective_from - 1``) are empty windows.
* ``user_role_assignments``: the unique ``(user_id, role_id)`` is dropped (the same role several times, D-X-07);
  trigger ``user_role_assignments_no_overlap()`` refuses overlapping half-open windows of one user and role -> 409
  ``ROLE_WINDOW_OVERLAP`` (BUG-AS-48).
* CHECK widenings: ``ck_role_permissions_scope`` gains TEAM, TEAM_TREE, ASSIGNED_TEAMS;
  ``ck_role_assignment_targets_scope`` gains ASSIGNED_TEAMS.
* ``rotation_assignments.team_id``, ``roster_periods.team_id`` and ``roster_entries.team_id`` get their foreign keys to
  ``teams`` (they were plain columns until this revision, AS-M5).
* Scheduled rows ``team.membership_activate`` and ``access.assignment_expire`` (daily-local 00:05, enabled).

No data migration creates teams (X 01 §17). Downgrade refuses while any team or a second assignment of the same user
and role exists.

Revision ID: 0047_x_m1_teams
Revises: 0046_lh_m1_holiday_imports
Create Date: 2026-10-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0047_x_m1_teams"
down_revision: Union[str, None] = "0046_lh_m1_holiday_imports"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
BASE = ("id SERIAL PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE RESTRICT, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()")
# Frozen copies: later registry changes must not alter what this revision does.
OLD_SCOPES = ("SELF", "DIRECT_REPORTS", "REPORTING_TREE", "DEPARTMENT", "ASSIGNED_GROUP", "LEGAL_ENTITY", "BRANCH",
              "PAY_GROUP", "COMPANY")
NEW_SCOPES = ("SELF", "DIRECT_REPORTS", "REPORTING_TREE", "TEAM", "TEAM_TREE", "DEPARTMENT", "ASSIGNED_GROUP",
              "ASSIGNED_TEAMS", "LEGAL_ENTITY", "BRANCH", "PAY_GROUP", "COMPANY")
OLD_TARGET_SCOPES = ("LEGAL_ENTITY", "BRANCH", "PAY_GROUP")
NEW_TARGET_SCOPES = ("LEGAL_ENTITY", "BRANCH", "PAY_GROUP", "ASSIGNED_TEAMS")


def _fk(table: str, column: str, target: str, name: str, ondelete: str = "RESTRICT") -> str:
    return (f"CONSTRAINT fk_{table}_{name} FOREIGN KEY (company_id, {column}) "
            f"REFERENCES {target} (company_id, id) ON DELETE {ondelete}")


TABLES = {
    "teams": f"""
        {BASE}, code VARCHAR(30) NOT NULL, name VARCHAR(100) NOT NULL, team_type VARCHAR(12) NOT NULL,
        parent_team_id INTEGER, legal_entity_id INTEGER, branch_id INTEGER, site_id INTEGER,
        description VARCHAR(500), status VARCHAR(10) NOT NULL DEFAULT 'ACTIVE', effective_from DATE NOT NULL,
        effective_to DATE, created_by_user_id INTEGER, row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_teams_company_id_id UNIQUE (company_id, id),
        CONSTRAINT uq_teams_code UNIQUE (company_id, code),
        {_fk('teams', 'parent_team_id', 'teams', 'parent')},
        {_fk('teams', 'legal_entity_id', 'legal_entities', 'legal_entity')},
        {_fk('teams', 'branch_id', 'branches', 'branch')},
        {_fk('teams', 'created_by_user_id', 'users', 'created_by')},
        CONSTRAINT ck_teams_type CHECK (team_type IN ('FUNCTIONAL','PROJECT','CREW','SITE','OTHER')),
        CONSTRAINT ck_teams_status CHECK (status IN ('ACTIVE','ARCHIVED')),
        CONSTRAINT ck_teams_dates CHECK (effective_to IS NULL OR effective_to >= effective_from),
        CONSTRAINT ck_teams_not_own_parent CHECK (parent_team_id IS NULL OR parent_team_id <> id),
        CONSTRAINT ck_teams_row_version CHECK (row_version >= 1)""",
    "team_parent_history": f"""
        {BASE}, team_id INTEGER NOT NULL, parent_team_id INTEGER, effective_from DATE NOT NULL, effective_to DATE,
        created_by_user_id INTEGER,
        CONSTRAINT uq_team_parent_history_company_id_id UNIQUE (company_id, id),
        {_fk('team_parent_history', 'team_id', 'teams', 'team', 'CASCADE')},
        {_fk('team_parent_history', 'parent_team_id', 'teams', 'parent')},
        CONSTRAINT ck_team_parent_history_dates CHECK (effective_to IS NULL OR effective_to >= effective_from - 1)""",
    "team_memberships": f"""
        {BASE}, team_id INTEGER NOT NULL, employee_id INTEGER NOT NULL, member_role VARCHAR(12) NOT NULL DEFAULT 'MEMBER',
        is_primary_team BOOLEAN NOT NULL DEFAULT false, is_routing_lead BOOLEAN NOT NULL DEFAULT false,
        effective_from DATE NOT NULL, effective_to DATE, reason VARCHAR(500), created_by_user_id INTEGER,
        ended_by_user_id INTEGER, row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_team_memberships_company_id_id UNIQUE (company_id, id),
        {_fk('team_memberships', 'team_id', 'teams', 'team')},
        {_fk('team_memberships', 'employee_id', 'employees', 'employee')},
        {_fk('team_memberships', 'created_by_user_id', 'users', 'created_by')},
        {_fk('team_memberships', 'ended_by_user_id', 'users', 'ended_by')},
        CONSTRAINT ck_team_memberships_role CHECK (member_role IN ('LEAD','DEPUTY_LEAD','MEMBER')),
        CONSTRAINT ck_team_memberships_routing_lead CHECK (NOT is_routing_lead OR member_role = 'LEAD'),
        CONSTRAINT ck_team_memberships_dates CHECK (effective_to IS NULL OR effective_to >= effective_from - 1),
        CONSTRAINT ck_team_memberships_row_version CHECK (row_version >= 1)""",
}
INDEXES = (
    "CREATE INDEX ix_teams_parent_team_id ON teams (parent_team_id)",
    "CREATE INDEX ix_team_parent_history_team_id ON team_parent_history (team_id)",
    "CREATE INDEX ix_team_memberships_team_id ON team_memberships (team_id)",
    "CREATE INDEX ix_team_memberships_employee_id ON team_memberships (employee_id)",
    "CREATE INDEX ix_team_memberships_dates ON team_memberships (company_id, team_id, effective_from, effective_to)",
)
ROSTER_FKS = (("rotation_assignments", "team"), ("roster_periods", "team"), ("roster_entries", "team"))
# Daily-local jobs (X 01 §3.2, §6.7; spine §9.2): HOURLY slots with a local threshold, as 0039 seeds them.
SCHEDULED = (("team.membership_activate", 52), ("access.assignment_expire", 54))

NO_CYCLE = """
CREATE OR REPLACE FUNCTION teams_no_cycle() RETURNS trigger AS $$
DECLARE
    cursor_id INTEGER := NEW.parent_team_id;
    depth INTEGER := 0;
BEGIN
    WHILE cursor_id IS NOT NULL LOOP
        IF cursor_id = NEW.id OR depth >= 64 THEN
            RAISE EXCEPTION 'A team cannot be its own ancestor'
                USING ERRCODE = 'check_violation', CONSTRAINT = 'team_cycle';
        END IF;
        SELECT parent_team_id INTO cursor_id FROM teams WHERE company_id = NEW.company_id AND id = cursor_id;
        depth := depth + 1;
    END LOOP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""
MEMBERSHIP_GUARD = """
CREATE OR REPLACE FUNCTION team_memberships_guard() RETURNS trigger AS $$
DECLARE
    span daterange;
BEGIN
    IF NEW.effective_to IS NOT NULL AND NEW.effective_to < NEW.effective_from THEN
        RETURN NEW;  -- a cancelled future row: an empty window (TM-7)
    END IF;
    span := daterange(NEW.effective_from, NEW.effective_to, '[]');
    IF EXISTS (
        SELECT 1 FROM team_memberships m
        WHERE m.company_id = NEW.company_id AND m.id <> NEW.id AND m.team_id = NEW.team_id
          AND m.employee_id = NEW.employee_id
          AND (m.effective_to IS NULL OR m.effective_to >= m.effective_from)
          AND daterange(m.effective_from, m.effective_to, '[]') && span
    ) THEN
        RAISE EXCEPTION 'The person already has a membership of this team on these dates'
            USING ERRCODE = 'exclusion_violation', CONSTRAINT = 'membership_overlap';
    END IF;
    IF NEW.is_primary_team AND EXISTS (
        SELECT 1 FROM team_memberships m
        WHERE m.company_id = NEW.company_id AND m.id <> NEW.id AND m.employee_id = NEW.employee_id
          AND m.is_primary_team
          AND (m.effective_to IS NULL OR m.effective_to >= m.effective_from)
          AND daterange(m.effective_from, m.effective_to, '[]') && span
    ) THEN
        RAISE EXCEPTION 'The person already has a primary team on these dates'
            USING ERRCODE = 'exclusion_violation', CONSTRAINT = 'primary_team_conflict';
    END IF;
    IF NEW.is_routing_lead AND EXISTS (
        SELECT 1 FROM team_memberships m
        WHERE m.company_id = NEW.company_id AND m.id <> NEW.id AND m.team_id = NEW.team_id AND m.is_routing_lead
          AND (m.effective_to IS NULL OR m.effective_to >= m.effective_from)
          AND daterange(m.effective_from, m.effective_to, '[]') && span
    ) THEN
        RAISE EXCEPTION 'The team already has a routing lead on these dates'
            USING ERRCODE = 'exclusion_violation', CONSTRAINT = 'routing_lead_conflict';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""
ROLE_WINDOWS = """
CREATE OR REPLACE FUNCTION user_role_assignments_no_overlap() RETURNS trigger AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM user_role_assignments a
        WHERE a.company_id = NEW.company_id AND a.id <> NEW.id AND a.user_id = NEW.user_id AND a.role_id = NEW.role_id
          AND tstzrange(a.effective_from, a.effective_until, '[)') && tstzrange(NEW.effective_from, NEW.effective_until, '[)')
    ) THEN
        RAISE EXCEPTION 'The user already holds this role in an overlapping window'
            USING ERRCODE = 'exclusion_violation', CONSTRAINT = 'role_window_overlap';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def _scopes(values) -> str:
    return "scope IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    bind = op.get_bind()
    for table, body in TABLES.items():
        op.execute(f"CREATE TABLE {table} ({body})")
        op.execute(f"CREATE INDEX ix_{table}_company_id ON {table} (company_id)")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")
    for statement in INDEXES:
        op.execute(statement)
    op.execute(NO_CYCLE)
    op.execute("CREATE TRIGGER trg_teams_no_cycle BEFORE INSERT OR UPDATE OF parent_team_id ON teams "
               "FOR EACH ROW EXECUTE FUNCTION teams_no_cycle()")
    op.execute(MEMBERSHIP_GUARD)
    op.execute("CREATE TRIGGER trg_team_memberships_guard BEFORE INSERT OR UPDATE ON team_memberships "
               "FOR EACH ROW EXECUTE FUNCTION team_memberships_guard()")
    roles = [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"))]
    for role in roles:
        quoted = '"' + role.replace('"', '""') + '"'
        for table in TABLES:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {quoted}")
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}")

    op.drop_constraint("uq_user_role_assignment", "user_role_assignments", type_="unique")
    op.execute(ROLE_WINDOWS)
    op.execute("CREATE TRIGGER trg_user_role_assignments_no_overlap BEFORE INSERT OR UPDATE ON user_role_assignments "
               "FOR EACH ROW EXECUTE FUNCTION user_role_assignments_no_overlap()")
    op.create_index("ix_user_role_assignments_user_role", "user_role_assignments", ["company_id", "user_id", "role_id"])

    op.drop_constraint("ck_role_permissions_scope", "role_permissions", type_="check")
    op.create_check_constraint("ck_role_permissions_scope", "role_permissions", _scopes(NEW_SCOPES))
    op.drop_constraint("ck_role_assignment_targets_scope", "user_role_assignment_targets", type_="check")
    op.create_check_constraint("ck_role_assignment_targets_scope", "user_role_assignment_targets",
                               _scopes(NEW_TARGET_SCOPES))
    for table, name in ROSTER_FKS:
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_{name} FOREIGN KEY (company_id, team_id) "
                   f"REFERENCES teams (company_id, id) ON DELETE RESTRICT")
    for job_type, minute in SCHEDULED:
        bind.execute(sa.text(
            "INSERT INTO scheduled_jobs (job_type, cadence, at_minute, at_local_time, enabled, next_run_at) "
            "VALUES (:t, 'HOURLY', :m, CAST('00:05' AS time), true, now()) ON CONFLICT (job_type) DO NOTHING"),
            {"t": job_type, "m": minute})


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT count(*) FROM teams")).scalar():
        raise RuntimeError("teams exist; export them before downgrading")
    if bind.execute(sa.text(
            "SELECT count(*) FROM (SELECT user_id, role_id FROM user_role_assignments GROUP BY user_id, role_id "
            "HAVING count(*) > 1) d")).scalar():
        raise RuntimeError("users hold a role in several windows; merge them before downgrading")
    if bind.execute(sa.text(
            "SELECT count(*) FROM role_permissions WHERE scope IN ('TEAM','TEAM_TREE','ASSIGNED_TEAMS')")).scalar():
        raise RuntimeError("roles hold team-scoped grants; remove them (0048 downgrade) before downgrading")
    bind.execute(sa.text("DELETE FROM scheduled_jobs WHERE job_type IN ('team.membership_activate', "
                         "'access.assignment_expire')"))
    for table, name in ROSTER_FKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT fk_{table}_{name}")
    bind.execute(sa.text("DELETE FROM user_role_assignment_targets WHERE scope = 'ASSIGNED_TEAMS'"))
    op.drop_constraint("ck_role_assignment_targets_scope", "user_role_assignment_targets", type_="check")
    op.create_check_constraint("ck_role_assignment_targets_scope", "user_role_assignment_targets",
                               _scopes(OLD_TARGET_SCOPES))
    op.drop_constraint("ck_role_permissions_scope", "role_permissions", type_="check")
    op.create_check_constraint("ck_role_permissions_scope", "role_permissions", _scopes(OLD_SCOPES))
    op.drop_index("ix_user_role_assignments_user_role", table_name="user_role_assignments")
    op.execute("DROP TRIGGER IF EXISTS trg_user_role_assignments_no_overlap ON user_role_assignments")
    op.execute("DROP FUNCTION IF EXISTS user_role_assignments_no_overlap()")
    op.create_unique_constraint("uq_user_role_assignment", "user_role_assignments", ["user_id", "role_id"])
    for table in ("team_memberships", "team_parent_history", "teams"):
        op.execute(f"DROP TABLE {table}")
    for function in ("team_memberships_guard", "teams_no_cycle"):
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")

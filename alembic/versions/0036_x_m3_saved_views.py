"""X-M3 list foundation (G-02; spine §3.10, AS 11 §4.14, X 04 §2.8): saved_views and dashboard_preferences.

Additive: two tenant tables with row-level security, grants and composite same-company foreign keys (the model
``app.models.views`` documents them).

* ``saved_views``: per-user named list queries; ``list_key`` checked against the LIST_KEYS registry frozen here (a
  later revision that adds list keys widens the CHECK); unique name per (user, list) among named views; one system-
  view row per (user, list, system_key); at most one default per (user, list). The 20-per-list cap is a service rule
  (422 SAVED_VIEW_LIMIT_REACHED).
* ``dashboard_preferences``: per-user layouts and per-persona tenant defaults.

Downgrade drops both tables (only saved views and layouts are lost).

Revision ID: 0036_x_m3_saved_views
Revises: 0035_as_m0_baseline
Create Date: 2026-10-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0036_x_m3_saved_views"
down_revision: Union[str, None] = "0035_as_m0_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies: later model changes must not alter what this revision does.
TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
LIST_KEYS = (
    "attendance.days", "attendance.team_today", "attendance.timesheets", "attendance.timesheet_approvals",
    "attendance.exceptions", "attendance.requests", "attendance.on_duty", "attendance.review", "attendance.punches",
    "attendance.muster", "attendance.devices", "attendance.device_mappings", "attendance.device_commands",
    "attendance.sites", "attendance.reason_codes", "attendance.policies", "attendance.period_closes",
    "attendance.registers", "attendance.consents", "attendance.contractors",
    "shift.shifts", "shift.rules", "shift.rotations", "shift.rotation_assignments", "shift.skill_tags",
    "roster.periods", "roster.publications", "roster.open_shifts", "roster.shift_requests", "roster.availability",
    "roster.estimates",
    "team.teams", "team.members", "access.roles", "access.change_requests", "cutoff.items", "overtime.entries",
    "approvals.inbox", "audit.logs",
    "leave.my_requests", "leave.requests", "leave.approvals", "leave.team_calendar", "leave.balances", "leave.ledger",
    "leave.documents", "leave.types", "leave.plans", "leave.comp_off", "leave.encashments",
    "holiday.calendars", "holiday.holidays", "holiday.optional", "holiday.optional_selections", "holiday.work_weeks",
    "holiday.calendar_assignments",
)
PERSONAS = ("EMPLOYEE", "TEAM_LEAD", "MANAGER", "HR", "PAYROLL", "EXECUTIVE")
DASHBOARD_KEYS = ("home", "attendance", "leave_hr")


def _common(table):
    return [
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("company_id", "id", name=f"uq_{table}_company_id_id"),
        sa.CheckConstraint("row_version >= 1", name=f"ck_{table}_row_version"),
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
    op.create_table(
        "saved_views",
        *_common("saved_views"),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("list_key", sa.String(64), nullable=False),
        sa.Column("name", sa.String(80), nullable=True),
        sa.Column("query", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("is_shared", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("position", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("system_key", sa.String(64), nullable=True),
        sa.Column("is_hidden", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.ForeignKeyConstraint(["company_id", "user_id"], ["users.company_id", "users.id"],
                                name="fk_saved_views_user", ondelete="CASCADE"),
        sa.CheckConstraint(f"list_key IN {LIST_KEYS}", name="ck_saved_views_list_key"),
        sa.CheckConstraint("(system_key IS NULL) = (name IS NOT NULL)", name="ck_saved_views_name_or_system"),
        sa.CheckConstraint("name IS NULL OR char_length(btrim(name)) BETWEEN 1 AND 80", name="ck_saved_views_name"),
        sa.CheckConstraint("system_key IS NULL OR (NOT is_shared AND NOT is_default)", name="ck_saved_views_system_row"),
        sa.CheckConstraint("jsonb_typeof(query) = 'object'", name="ck_saved_views_query_object"),
    )
    op.create_index("ix_saved_views_company_id", "saved_views", ["company_id"])
    op.create_index("ix_saved_views_list", "saved_views", ["company_id", "list_key", "is_shared"])
    op.create_index("uq_saved_views_name", "saved_views", ["company_id", "user_id", "list_key", "name"], unique=True,
                    postgresql_where=sa.text("system_key IS NULL"))
    op.create_index("uq_saved_views_system", "saved_views", ["company_id", "user_id", "list_key", "system_key"],
                    unique=True, postgresql_where=sa.text("system_key IS NOT NULL"))
    op.create_index("uq_saved_views_default", "saved_views", ["company_id", "user_id", "list_key"], unique=True,
                    postgresql_where=sa.text("is_default"))
    _secure(bind, "saved_views")

    op.create_table(
        "dashboard_preferences",
        *_common("dashboard_preferences"),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("persona", sa.String(16), nullable=True),
        sa.Column("dashboard_key", sa.String(32), nullable=False),
        sa.Column("layout", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.ForeignKeyConstraint(["company_id", "user_id"], ["users.company_id", "users.id"],
                                name="fk_dashboard_preferences_user", ondelete="CASCADE"),
        sa.CheckConstraint("(user_id IS NULL) = (persona IS NOT NULL)", name="ck_dashboard_preferences_owner"),
        sa.CheckConstraint(f"persona IS NULL OR persona IN {PERSONAS}", name="ck_dashboard_preferences_persona"),
        sa.CheckConstraint(f"dashboard_key IN {DASHBOARD_KEYS}", name="ck_dashboard_preferences_key"),
        sa.CheckConstraint("jsonb_typeof(layout) = 'object'", name="ck_dashboard_preferences_layout_object"),
    )
    op.create_index("ix_dashboard_preferences_company_id", "dashboard_preferences", ["company_id"])
    op.create_index("uq_dashboard_preferences_user", "dashboard_preferences", ["company_id", "user_id", "dashboard_key"],
                    unique=True, postgresql_where=sa.text("user_id IS NOT NULL"))
    op.create_index("uq_dashboard_preferences_persona", "dashboard_preferences",
                    ["company_id", "persona", "dashboard_key"], unique=True,
                    postgresql_where=sa.text("user_id IS NULL"))
    _secure(bind, "dashboard_preferences")


def downgrade() -> None:
    op.drop_table("dashboard_preferences")
    op.drop_table("saved_views")

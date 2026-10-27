"""AS-M1 shift assignment rules (AS 11 §4.5; 03-shifts/01 §12; D-AS-15, D-AS-85, SP-AS-031, SP-AS-235).

NEW ``shift_assignment_rules``: criteria rows (``AssignmentCriteria`` extended by D-X-09, ``shift_ids`` refused by
the service) giving matching employees a shift for a date range; **higher priority wins**. An in-force rule is
replaced, not edited (``replaces_rule_id``). ``team_id_snapshot`` keeps the team a rule's ``employee_ids`` were copied
from until X-M1 ships team criteria (its FK to ``teams`` arrives with ``x_m1_teams``). Tenant table with RLS, grants
and composite same-company foreign keys. No rows are created (zero diff).

Downgrade refuses while rules exist (AS 11 §11.1 M-5: export first), then drops the table.

Revision ID: 0041_as_m1_shift_rules
Revises: 0040_as_m1_shift_versions
Create Date: 2026-10-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041_as_m1_shift_rules"
down_revision: Union[str, None] = "0040_as_m1_shift_versions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
TABLE = "shift_assignment_rules"
STATUSES = ("ACTIVE", "INACTIVE")


def _fk(columns, target, name):
    local = ["company_id", *([columns] if isinstance(columns, str) else columns)]
    return sa.ForeignKeyConstraint(local, [f"{target}.company_id", f"{target}.id"], name=f"fk_{TABLE}_{name}")


def upgrade() -> None:
    bind = op.get_bind()
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("shift_id", sa.Integer(), nullable=False),
        sa.Column("criteria", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="500"),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("legal_entity_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(10), nullable=False, server_default="ACTIVE"),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("replaces_rule_id", sa.Integer(), nullable=True),
        sa.Column("team_id_snapshot", sa.Integer(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("company_id", "id", name=f"uq_{TABLE}_company_id_id"),
        _fk("shift_id", "shifts", "shift"),
        _fk("legal_entity_id", "legal_entities", "legal_entity"),
        _fk("replaces_rule_id", TABLE, "replaces"),
        _fk("created_by_user_id", "users", "created_by"),
        sa.CheckConstraint(f"status IN {STATUSES}", name=f"ck_{TABLE}_status"),
        sa.CheckConstraint("priority >= 0", name=f"ck_{TABLE}_priority"),
        sa.CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name=f"ck_{TABLE}_dates"),
        sa.CheckConstraint("jsonb_typeof(criteria) = 'object'", name=f"ck_{TABLE}_criteria"),
        sa.CheckConstraint("char_length(btrim(name)) BETWEEN 1 AND 100", name=f"ck_{TABLE}_name"),
        sa.CheckConstraint("row_version >= 1", name=f"ck_{TABLE}_row_version"),
    )
    op.create_index(f"ix_{TABLE}_company_id", TABLE, ["company_id"])
    op.create_index(f"ix_{TABLE}_shift_id", TABLE, ["shift_id"])
    op.create_index(f"ix_{TABLE}_status", TABLE, ["company_id", "status", "effective_from"])
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON {TABLE} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")
    roles = [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"
    ))]
    for role in roles:
        quoted = '"' + role.replace('"', '""') + '"'
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {quoted}")
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {TABLE}_id_seq TO {quoted}")


def downgrade() -> None:
    rules = op.get_bind().execute(sa.text(f"SELECT count(*) FROM {TABLE}")).scalar()
    if rules:
        raise RuntimeError(f"{rules} shift assignment rule(s) exist; export them before downgrading")
    op.drop_table(TABLE)

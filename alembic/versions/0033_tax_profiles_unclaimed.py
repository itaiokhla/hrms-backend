"""Employee tax profiles and the unclaimed-pay log: employee_tax_profiles, employee_tax_proofs,
payment_unclaimed_events (3.2 §7, EC-STA-21, EC-PAY-19).

Additive: three tenant tables with row-level security, grants and composite same-company foreign keys (the models
``app.models.tax_profiles`` and ``app.models.unclaimed`` document them), and the grants of the two new permission
codes to the system roles that hold them by default:

* ``payroll:tax_profile:edit`` (SELF) to the ``employee``, ``hr_admin``, ``manager`` and ``payroll_manager`` roles
  (the roles built on the self-service grants): people submit their own declarations;
* ``payroll:tax_profile:review`` (COMPANY) to ``payroll_manager`` and ``payroll_processor`` roles: HR review.

Roles of every company are touched only when they are the seeded system roles and lack the grant; the
``permission_version`` of their holders is bumped so sessions pick the grants up.

Database rules services cannot be trusted to keep alone: ``employee_tax_profiles_guard`` freezes the content of a
version once submitted and every column once it was reviewed or withdrawn, and refuses every DELETE (withdraw
instead); a proof can only be added to a SUBMITTED version of the same employee and is never changed or deleted;
``payment_unclaimed_events`` is append-only.

Downgrade refuses while any tax profile exists (declarations are employee records that are never dropped
silently), then drops the triggers, tables and the two grants.

Revision ID: 0033_tax_profiles_unclaimed
Revises: 0032_nominees
Create Date: 2026-10-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0033_tax_profiles_unclaimed'
down_revision: Union[str, None] = '0032_nominees'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies: later model changes must not alter what this revision does.
TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
PROFILES = 'employee_tax_profiles'
PROOFS = 'employee_tax_proofs'
UNCLAIMED = 'payment_unclaimed_events'
PROFILE_STATUSES = ("SUBMITTED", "APPROVED", "REJECTED", "WITHDRAWN")
UNCLAIMED_ACTIONS = ("MARKED", "RELEASED")
NOTE_MAX = 2000
REASON_MAX = 2000

# system role key -> grants added by this revision
NEW_GRANTS = {
    # every role built on the self-service grants (SELF_SERVICE in the registry) gets the new self-service code
    'employee': (('payroll:tax_profile:edit', 'SELF'),),
    'hr_admin': (('payroll:tax_profile:edit', 'SELF'),),
    'manager': (('payroll:tax_profile:edit', 'SELF'),),
    'payroll_manager': (('payroll:tax_profile:edit', 'SELF'), ('payroll:tax_profile:review', 'COMPANY')),
    'payroll_processor': (('payroll:tax_profile:review', 'COMPANY'),),
}


def _sql_list(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _base():
    return [
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    ]


def _fk(table, columns, target, name):
    local = ['company_id', *([columns] if isinstance(columns, str) else columns)]
    return sa.ForeignKeyConstraint(local, [f'{target}.company_id', f'{target}.id'], name=f'fk_{table}_{name}')


PROFILE_GUARD = """
CREATE OR REPLACE FUNCTION employee_tax_profiles_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'tax profile versions are never deleted: withdraw the submission instead'
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status <> 'SUBMITTED' THEN
        RAISE EXCEPTION 'a reviewed or withdrawn tax profile version cannot change'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.company_id IS DISTINCT FROM OLD.company_id
       OR NEW.employee_id IS DISTINCT FROM OLD.employee_id
       OR NEW.legal_entity_id IS DISTINCT FROM OLD.legal_entity_id
       OR NEW.country IS DISTINCT FROM OLD.country
       OR NEW.pack_code IS DISTINCT FROM OLD.pack_code
       OR NEW.schema_hash IS DISTINCT FROM OLD.schema_hash
       OR NEW.version_no IS DISTINCT FROM OLD.version_no
       OR NEW.effective_from IS DISTINCT FROM OLD.effective_from
       OR NEW.declared_values IS DISTINCT FROM OLD.declared_values
       OR NEW.identifiers_enc IS DISTINCT FROM OLD.identifiers_enc
       OR NEW.identifiers_masked IS DISTINCT FROM OLD.identifiers_masked
       OR NEW.submitted_by_user_id IS DISTINCT FROM OLD.submitted_by_user_id
       OR NEW.submitted_at IS DISTINCT FROM OLD.submitted_at THEN
        RAISE EXCEPTION 'the declarations of a submitted tax profile version cannot be edited: submit a new version'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

PROOF_GUARD = """
CREATE OR REPLACE FUNCTION employee_tax_proofs_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE parent_status text; parent_employee integer;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'tax proofs are never changed or deleted' USING ERRCODE = 'check_violation';
    END IF;
    SELECT status, employee_id INTO parent_status, parent_employee FROM employee_tax_profiles
     WHERE company_id = NEW.company_id AND id = NEW.profile_id;
    IF parent_status IS DISTINCT FROM 'SUBMITTED' THEN
        RAISE EXCEPTION 'a proof can only be added to a submitted tax profile version'
            USING ERRCODE = 'check_violation';
    END IF;
    IF parent_employee IS DISTINCT FROM NEW.employee_id THEN
        RAISE EXCEPTION 'a proof belongs to the employee of its tax profile version'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

UNCLAIMED_APPEND_ONLY = """
CREATE OR REPLACE FUNCTION payment_unclaimed_events_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'payment_unclaimed_events are append-only' USING ERRCODE = 'check_violation';
END;
$$
"""


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


def _grant_roles(bind) -> int:
    """Add the new grants to the seeded system roles that lack them; bump their holders' permission version."""
    added = 0
    for key, grants in NEW_GRANTS.items():
        roles = bind.execute(sa.text(
            "SELECT id, company_id FROM roles WHERE system_key = :k AND is_system_role = true"), {"k": key}).all()
        for role_id, company_id in roles:
            changed = False
            for code, scope in grants:
                exists = bind.execute(sa.text(
                    "SELECT 1 FROM role_permissions WHERE role_id = :r AND permission_code = :c AND scope = :s"),
                    {"r": role_id, "c": code, "s": scope}).first()
                if exists:
                    continue
                bind.execute(sa.text(
                    "INSERT INTO role_permissions (company_id, role_id, permission_code, scope, created_at, updated_at) "
                    "VALUES (:cid, :r, :c, :s, now(), now())"), {"cid": company_id, "r": role_id, "c": code, "s": scope})
                added += 1
                changed = True
            if changed:
                bind.execute(sa.text(
                    "UPDATE users SET permission_version = permission_version + 1 WHERE company_id = :cid AND id IN "
                    "(SELECT user_id FROM user_role_assignments WHERE company_id = :cid AND role_id = :r)"),
                    {"cid": company_id, "r": role_id})
    return added


def upgrade() -> None:
    bind = op.get_bind()
    op.create_table(
        PROFILES, *_base(),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('country', sa.CHAR(2), nullable=False),
        sa.Column('pack_code', sa.String(20), nullable=False),
        sa.Column('schema_hash', sa.CHAR(64), nullable=False),
        sa.Column('version_no', sa.Integer(), nullable=False),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('status', sa.String(10), nullable=False, server_default='SUBMITTED'),
        sa.Column('declared_values', sa.dialects.postgresql.JSONB(), nullable=False, server_default='{}'),
        sa.Column('identifiers_enc', sa.dialects.postgresql.JSONB(), nullable=False, server_default='{}'),
        sa.Column('identifiers_masked', sa.dialects.postgresql.JSONB(), nullable=False, server_default='{}'),
        sa.Column('submitted_by_user_id', sa.Integer(), nullable=False),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('reviewed_by_user_id', sa.Integer(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('review_note', sa.Text(), nullable=True),
        sa.Column('withdrawn_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('row_version', sa.Integer(), nullable=False, server_default='1'),
        sa.UniqueConstraint('company_id', 'id', name=f'uq_{PROFILES}_company_id_id'),
        sa.UniqueConstraint('company_id', 'employee_id', 'version_no', name=f'uq_{PROFILES}_version'),
        _fk(PROFILES, 'employee_id', 'employees', 'employee'),
        _fk(PROFILES, 'legal_entity_id', 'legal_entities', 'legal_entity'),
        _fk(PROFILES, 'submitted_by_user_id', 'users', 'submitted_by'),
        _fk(PROFILES, 'reviewed_by_user_id', 'users', 'reviewed_by'),
        sa.CheckConstraint(f"status IN {_sql_list(PROFILE_STATUSES)}", name=f'ck_{PROFILES}_status'),
        sa.CheckConstraint("version_no >= 1 AND row_version >= 1", name=f'ck_{PROFILES}_versions'),
        sa.CheckConstraint("country ~ '^[A-Z]{2}$'", name=f'ck_{PROFILES}_country'),
        sa.CheckConstraint("jsonb_typeof(declared_values) = 'object' AND jsonb_typeof(identifiers_enc) = 'object' "
                           "AND jsonb_typeof(identifiers_masked) = 'object'", name=f'ck_{PROFILES}_shapes'),
        sa.CheckConstraint("(status IN ('APPROVED', 'REJECTED')) = (reviewed_by_user_id IS NOT NULL "
                           "AND reviewed_at IS NOT NULL)", name=f'ck_{PROFILES}_reviewed'),
        sa.CheckConstraint("reviewed_by_user_id IS NULL OR reviewed_by_user_id <> submitted_by_user_id",
                           name=f'ck_{PROFILES}_maker_checker'),
        sa.CheckConstraint(f"status <> 'REJECTED' OR char_length(btrim(coalesce(review_note, ''))) BETWEEN 1 AND {NOTE_MAX}",
                           name=f'ck_{PROFILES}_rejection_note'),
        sa.CheckConstraint(f"review_note IS NULL OR char_length(review_note) <= {NOTE_MAX}",
                           name=f'ck_{PROFILES}_note_length'),
        sa.CheckConstraint("(status = 'WITHDRAWN') = (withdrawn_at IS NOT NULL)", name=f'ck_{PROFILES}_withdrawn'),
    )
    op.create_index(f'ix_{PROFILES}_company_id', PROFILES, ['company_id'])
    op.create_index(f'ix_{PROFILES}_employee_id', PROFILES, ['employee_id'])
    op.create_index(f'ix_{PROFILES}_scope_status', PROFILES, ['company_id', 'legal_entity_id', 'status'])
    op.create_index(f'uq_{PROFILES}_one_open', PROFILES, ['company_id', 'employee_id'], unique=True,
                    postgresql_where=sa.text("status = 'SUBMITTED'"))
    _secure(bind, PROFILES)
    op.execute(PROFILE_GUARD)
    op.execute(f"CREATE TRIGGER trg_{PROFILES}_guard BEFORE UPDATE OR DELETE ON {PROFILES} "
               f"FOR EACH ROW EXECUTE FUNCTION {PROFILES}_guard()")

    op.create_table(
        PROOFS, *_base(),
        sa.Column('profile_id', sa.Integer(), nullable=False),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('field_code', sa.String(81), nullable=False),
        sa.Column('file_name', sa.String(200), nullable=False),
        sa.Column('storage_path', sa.String(300), nullable=False),
        sa.Column('sha256', sa.CHAR(64), nullable=False),
        sa.Column('mime', sa.String(60), nullable=False),
        sa.Column('bytes', sa.Integer(), nullable=False),
        sa.Column('uploaded_by_user_id', sa.Integer(), nullable=False),
        sa.UniqueConstraint('company_id', 'id', name=f'uq_{PROOFS}_company_id_id'),
        sa.UniqueConstraint('company_id', 'profile_id', 'field_code', 'sha256', name=f'uq_{PROOFS}_file'),
        _fk(PROOFS, 'profile_id', PROFILES, 'profile'),
        _fk(PROOFS, 'employee_id', 'employees', 'employee'),
        _fk(PROOFS, 'uploaded_by_user_id', 'users', 'uploaded_by'),
        sa.CheckConstraint("bytes > 0", name=f'ck_{PROOFS}_bytes'),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=f'ck_{PROOFS}_sha256'),
        sa.CheckConstraint("char_length(btrim(field_code)) > 0", name=f'ck_{PROOFS}_field'),
    )
    op.create_index(f'ix_{PROOFS}_company_id', PROOFS, ['company_id'])
    op.create_index(f'ix_{PROOFS}_profile_id', PROOFS, ['profile_id'])
    _secure(bind, PROOFS)
    op.execute(PROOF_GUARD)
    op.execute(f"CREATE TRIGGER trg_{PROOFS}_guard BEFORE INSERT OR UPDATE OR DELETE ON {PROOFS} "
               f"FOR EACH ROW EXECUTE FUNCTION {PROOFS}_guard()")

    op.create_table(
        UNCLAIMED, *_base(),
        sa.Column('obligation_id', sa.Integer(), nullable=False),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('action', sa.String(10), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('window_days', sa.Integer(), nullable=True),
        sa.Column('amount', sa.Numeric(19, 4), nullable=False),
        sa.Column('currency', sa.CHAR(3), nullable=False),
        sa.Column('held_instruction_ids', sa.dialects.postgresql.JSONB(), nullable=False, server_default='[]'),
        sa.Column('result_status', sa.String(10), nullable=True),
        sa.Column('actor_user_id', sa.Integer(), nullable=True),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint('company_id', 'id', name=f'uq_{UNCLAIMED}_company_id_id'),
        _fk(UNCLAIMED, 'obligation_id', 'payment_obligations', 'obligation'),
        _fk(UNCLAIMED, 'employee_id', 'employees', 'employee'),
        _fk(UNCLAIMED, 'legal_entity_id', 'legal_entities', 'legal_entity'),
        _fk(UNCLAIMED, 'actor_user_id', 'users', 'actor'),
        sa.CheckConstraint(f"action IN {_sql_list(UNCLAIMED_ACTIONS)}", name=f'ck_{UNCLAIMED}_action'),
        sa.CheckConstraint(f"char_length(btrim(reason)) BETWEEN 1 AND {REASON_MAX}", name=f'ck_{UNCLAIMED}_reason'),
        sa.CheckConstraint("amount > 0", name=f'ck_{UNCLAIMED}_amount'),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=f'ck_{UNCLAIMED}_currency'),
        sa.CheckConstraint("jsonb_typeof(held_instruction_ids) = 'array'", name=f'ck_{UNCLAIMED}_held'),
        sa.CheckConstraint("window_days IS NULL OR window_days >= 1", name=f'ck_{UNCLAIMED}_window'),
        sa.CheckConstraint("action <> 'RELEASED' OR actor_user_id IS NOT NULL", name=f'ck_{UNCLAIMED}_release_actor'),
        sa.CheckConstraint("action <> 'MARKED' OR window_days IS NOT NULL OR actor_user_id IS NOT NULL",
                           name=f'ck_{UNCLAIMED}_marked_cause'),
    )
    op.create_index(f'ix_{UNCLAIMED}_company_id', UNCLAIMED, ['company_id'])
    op.create_index(f'ix_{UNCLAIMED}_obligation_id', UNCLAIMED, ['obligation_id'])
    op.create_index(f'ix_{UNCLAIMED}_scope', UNCLAIMED, ['company_id', 'legal_entity_id', 'at'])
    _secure(bind, UNCLAIMED)
    op.execute(UNCLAIMED_APPEND_ONLY)
    op.execute(f"CREATE TRIGGER trg_{UNCLAIMED}_append_only BEFORE UPDATE OR DELETE ON {UNCLAIMED} "
               f"FOR EACH ROW EXECUTE FUNCTION {UNCLAIMED}_append_only()")

    _grant_roles(bind)


def downgrade() -> None:
    bind = op.get_bind()
    left = bind.execute(sa.text(f"SELECT count(*) FROM {PROFILES}")).scalar()
    if left:
        raise RuntimeError("tax profile versions exist: declarations are employee records and are never dropped "
                           "silently; export or remove them deliberately before downgrading")
    for key, grants in NEW_GRANTS.items():
        for code, scope in grants:
            bind.execute(sa.text(
                "DELETE FROM role_permissions WHERE permission_code = :c AND scope = :s AND role_id IN "
                "(SELECT id FROM roles WHERE system_key = :k AND is_system_role = true)"),
                {"c": code, "s": scope, "k": key})
    bind.execute(sa.text("UPDATE users SET permission_version = permission_version + 1"))
    op.execute(f"DROP TRIGGER IF EXISTS trg_{UNCLAIMED}_append_only ON {UNCLAIMED}")
    op.execute(f"DROP TRIGGER IF EXISTS trg_{PROOFS}_guard ON {PROOFS}")
    op.execute(f"DROP TRIGGER IF EXISTS trg_{PROFILES}_guard ON {PROFILES}")
    op.drop_table(UNCLAIMED)
    op.drop_table(PROOFS)
    op.drop_table(PROFILES)
    op.execute(f"DROP FUNCTION IF EXISTS {UNCLAIMED}_append_only()")
    op.execute(f"DROP FUNCTION IF EXISTS {PROOFS}_guard()")
    op.execute(f"DROP FUNCTION IF EXISTS {PROFILES}_guard()")

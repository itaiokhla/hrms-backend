"""Nominees and legal heirs as payees of a deceased employee's final settlement (EC-BNK-15, EC-COR-07, 3.7 §2.1).

* ``nominees``: tenant table (RLS, grants, composite same-company foreign keys to the employee, the final
  settlement case and users) with the proof document reference, the share in percent, country and status.
  Rules the database keeps itself: a deferred constraint trigger refuses shares of ACTIVE nominees of one
  case that add up to more than 100; one active nominee per name and relationship and case (unique index);
  ``nominees_guard`` freezes company, employee, case and creator and refuses every DELETE.
* ``bank_accounts.nominee_id`` and ``bank_account_change_requests.nominee_id``: composite foreign key to the
  nominee, set exactly when ``payee_type = 'NOMINEE'`` (and equal to ``payee_id``). Employee rows are untouched.
  ``bank_accounts_guard`` now also freezes ``nominee_id``.

Downgrade drops the columns (nominee bank accounts must be removed first: the downgrade refuses while any exist),
the triggers and the table, and restores the 0021 guard.

Revision ID: 0032_nominees
Revises: 0031_legal_holds
Create Date: 2026-10-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0032_nominees'
down_revision: Union[str, None] = '0031_legal_holds'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies: later model changes must not alter what this revision does.
TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
TABLE = 'nominees'
KINDS = ("NOMINEE", "LEGAL_HEIR")
STATUSES = ("ACTIVE", "ENDED")


def _sql_list(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _fk(columns, target, name):
    local = ['company_id', *([columns] if isinstance(columns, str) else columns)]
    return sa.ForeignKeyConstraint(local, [f'{target}.company_id', f'{target}.id'], name=f'fk_{TABLE}_{name}')


GUARD_OLD = """
CREATE OR REPLACE FUNCTION bank_accounts_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'bank_accounts are never deleted: deactivate the account instead'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.company_id IS DISTINCT FROM OLD.company_id
       OR NEW.payee_type IS DISTINCT FROM OLD.payee_type
       OR NEW.payee_id IS DISTINCT FROM OLD.payee_id
       OR NEW.employee_id IS DISTINCT FROM OLD.employee_id
       OR NEW.holder_name IS DISTINCT FROM OLD.holder_name
       OR NEW.country IS DISTINCT FROM OLD.country
       OR NEW.currency IS DISTINCT FROM OLD.currency
       OR NEW.routing IS DISTINCT FROM OLD.routing
       OR NEW.last4 IS DISTINCT FROM OLD.last4
       OR NEW.fingerprint IS DISTINCT FROM OLD.fingerprint
       OR NEW.allocation IS DISTINCT FROM OLD.allocation
       OR NEW.created_by_user_id IS DISTINCT FROM OLD.created_by_user_id
       OR (NEW.account_number_enc IS NULL) <> (OLD.account_number_enc IS NULL)
       OR (NEW.iban_enc IS NULL) <> (OLD.iban_enc IS NULL) THEN
        RAISE EXCEPTION 'bank account details are immutable: submit a new account instead'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

GUARD_NEW = GUARD_OLD.replace("       OR NEW.employee_id IS DISTINCT FROM OLD.employee_id\n",
                              "       OR NEW.employee_id IS DISTINCT FROM OLD.employee_id\n"
                              "       OR NEW.nominee_id IS DISTINCT FROM OLD.nominee_id\n")

NOMINEES_GUARD = """
CREATE OR REPLACE FUNCTION nominees_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'nominees are never deleted: end the nominee instead'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.company_id IS DISTINCT FROM OLD.company_id
       OR NEW.employee_id IS DISTINCT FROM OLD.employee_id
       OR NEW.final_settlement_case_id IS DISTINCT FROM OLD.final_settlement_case_id
       OR NEW.created_by_user_id IS DISTINCT FROM OLD.created_by_user_id THEN
        RAISE EXCEPTION 'the employee, case and creator of a nominee never change'
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status = 'ENDED' AND NEW.status <> 'ENDED' THEN
        RAISE EXCEPTION 'an ended nominee is never reopened: add a new nominee'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

SHARES_CHECK = """
CREATE OR REPLACE FUNCTION nominees_shares_check() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE total numeric;
BEGIN
    SELECT COALESCE(SUM(share_percent), 0) INTO total FROM nominees
     WHERE company_id = NEW.company_id AND final_settlement_case_id = NEW.final_settlement_case_id
       AND status = 'ACTIVE';
    IF total > 100 THEN
        RAISE EXCEPTION 'the shares of the active nominees of a settlement add up to % percent (at most 100)', total
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END;
$$
"""


def upgrade() -> None:
    bind = op.get_bind()
    op.create_table(
        TABLE,
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('final_settlement_case_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(12), nullable=False, server_default='NOMINEE'),
        sa.Column('name', sa.String(140), nullable=False),
        sa.Column('relationship', sa.String(60), nullable=False),
        sa.Column('proof_ref', sa.String(300), nullable=False),
        sa.Column('share_percent', sa.Numeric(12, 6), nullable=False),
        sa.Column('country', sa.CHAR(2), nullable=False),
        sa.Column('email', sa.String(254), nullable=True),
        sa.Column('status', sa.String(10), nullable=False, server_default='ACTIVE'),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ended_reason', sa.Text(), nullable=True),
        sa.Column('ended_by_user_id', sa.Integer(), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=False),
        sa.Column('row_version', sa.Integer(), nullable=False, server_default='1'),
        sa.UniqueConstraint('company_id', 'id', name=f'uq_{TABLE}_company_id_id'),
        _fk('employee_id', 'employees', 'employee'),
        _fk('final_settlement_case_id', 'final_settlement_cases', 'case'),
        _fk('created_by_user_id', 'users', 'created_by'),
        _fk('ended_by_user_id', 'users', 'ended_by'),
        sa.CheckConstraint(f"kind IN {_sql_list(KINDS)}", name=f'ck_{TABLE}_kind'),
        sa.CheckConstraint(f"status IN {_sql_list(STATUSES)}", name=f'ck_{TABLE}_status'),
        sa.CheckConstraint("share_percent > 0 AND share_percent <= 100", name=f'ck_{TABLE}_share'),
        sa.CheckConstraint("char_length(btrim(name)) >= 2", name=f'ck_{TABLE}_name'),
        sa.CheckConstraint("char_length(btrim(relationship)) >= 2", name=f'ck_{TABLE}_relationship'),
        sa.CheckConstraint("char_length(btrim(proof_ref)) >= 3", name=f'ck_{TABLE}_proof'),
        sa.CheckConstraint("country ~ '^[A-Z]{2}$'", name=f'ck_{TABLE}_country'),
        sa.CheckConstraint("(status = 'ENDED') = (ended_at IS NOT NULL)", name=f'ck_{TABLE}_ended'),
        sa.CheckConstraint("status <> 'ENDED' OR (ended_reason IS NOT NULL AND ended_by_user_id IS NOT NULL)",
                           name=f'ck_{TABLE}_ended_reason'),
        sa.CheckConstraint("row_version >= 1", name=f'ck_{TABLE}_row_version'),
    )
    op.create_index(f'ix_{TABLE}_company_id', TABLE, ['company_id'])
    op.create_index(f'ix_{TABLE}_employee_id', TABLE, ['employee_id'])
    op.create_index(f'ix_{TABLE}_final_settlement_case_id', TABLE, ['final_settlement_case_id'])
    op.create_index(f'ix_{TABLE}_case_status', TABLE, ['company_id', 'final_settlement_case_id', 'status'])
    op.create_index(f'uq_{TABLE}_active_person', TABLE,
                    ['company_id', 'final_settlement_case_id', sa.text('lower(btrim(name))'),
                     sa.text('lower(btrim(relationship))')],
                    unique=True, postgresql_where=sa.text("status = 'ACTIVE'"))

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

    op.execute(NOMINEES_GUARD)
    op.execute(f"CREATE TRIGGER trg_nominees_guard BEFORE UPDATE OR DELETE ON {TABLE} "
               "FOR EACH ROW EXECUTE FUNCTION nominees_guard()")
    op.execute(SHARES_CHECK)
    op.execute(f"CREATE CONSTRAINT TRIGGER trg_nominees_shares AFTER INSERT OR UPDATE ON {TABLE} "
               "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION nominees_shares_check()")

    for table, check_name in (('bank_accounts', 'ck_bank_accounts_nominee'),
                              ('bank_account_change_requests', 'ck_bank_account_change_requests_nominee')):
        op.add_column(table, sa.Column('nominee_id', sa.Integer(), nullable=True))
        op.create_index(f'ix_{table}_nominee_id', table, ['nominee_id'])
        op.create_foreign_key(f'fk_{table}_nominee', table, TABLE, ['company_id', 'nominee_id'],
                              ['company_id', 'id'])
        op.create_check_constraint(
            check_name, table,
            "(payee_type = 'NOMINEE') = (nominee_id IS NOT NULL) AND (nominee_id IS NULL OR nominee_id = payee_id)")
    op.execute(GUARD_NEW)


def downgrade() -> None:
    bind = op.get_bind()
    left = bind.execute(sa.text("SELECT count(*) FROM bank_accounts WHERE nominee_id IS NOT NULL")).scalar()
    if left:
        raise RuntimeError("nominee bank accounts exist: they are payment records and are never dropped silently")
    op.execute(GUARD_OLD)
    for table, check_name in (('bank_account_change_requests', 'ck_bank_account_change_requests_nominee'),
                              ('bank_accounts', 'ck_bank_accounts_nominee')):
        op.drop_constraint(check_name, table, type_='check')
        op.drop_constraint(f'fk_{table}_nominee', table, type_='foreignkey')
        op.drop_index(f'ix_{table}_nominee_id', table_name=table)
        op.drop_column(table, 'nominee_id')
    op.execute(f"DROP TRIGGER IF EXISTS trg_nominees_shares ON {TABLE}")
    op.execute(f"DROP TRIGGER IF EXISTS trg_nominees_guard ON {TABLE}")
    op.execute("DROP FUNCTION IF EXISTS nominees_shares_check()")
    op.execute("DROP FUNCTION IF EXISTS nominees_guard()")
    op.drop_table(TABLE)

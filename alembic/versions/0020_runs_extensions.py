"""Extensions needed by the snapshot builder, calculation and finalization services.

Shared by three agents (docs/payroll/implementation/workflows/phase4.js): each owns one pair of
functions below and appends operations only there. ``upgrade()`` / ``downgrade()`` call every section
in a fixed order (downgrade in reverse).

* calculation: ``payroll_run_employees.stage_outputs`` — what the engine produced besides lines for one
  run employee (statutory liabilities, YTD increments, pack state, deduction carry-forwards, the
  payment split, the day ledger), persisted for finalization and review (FOLLOW-UPS F-15).

Revision ID: 0020_runs_extensions
Revises: 0019_payroll_runs
Create Date: 2026-10-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0020_runs_extensions'
down_revision: Union[str, None] = '0019_payroll_runs'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# --------------------------------------------------------------------------- snapshot builder (snapshot agent)
def upgrade_snapshot() -> None:
    # Which snapshots used a source record (policies.policy_in_use: "source_hashes ?| ARRAY['policy_version:12']",
    # change detection): a GIN index over the keys of payroll_input_snapshots.source_hashes.
    op.create_index('ix_payroll_input_snapshots_source_keys', 'payroll_input_snapshots', ['source_hashes'],
                    postgresql_using='gin')


def downgrade_snapshot() -> None:
    op.drop_index('ix_payroll_input_snapshots_source_keys', table_name='payroll_input_snapshots')


# --------------------------------------------------------------------------- calculation (calculate agent)
def upgrade_calculation() -> None:
    op.add_column('payroll_run_employees', sa.Column(
        'stage_outputs', postgresql.JSONB(astext_type=sa.Text()), nullable=False,
        server_default=sa.text("'{}'::jsonb")))
    op.create_check_constraint('ck_payroll_run_employees_stage_outputs', 'payroll_run_employees',
                               "jsonb_typeof(stage_outputs) = 'object'")


def downgrade_calculation() -> None:
    op.drop_constraint('ck_payroll_run_employees_stage_outputs', 'payroll_run_employees', type_='check')
    op.drop_column('payroll_run_employees', 'stage_outputs')


# --------------------------------------------------------------------------- finalization (finalize agent)
# Immutability of finalized runs in the database as well as in the services (EC-RUN-17): 0019 already
# refuses changes to payroll_runs results and to payroll_lines of a finalized run, and every UPDATE of a
# snapshot. These guards add the run employees (results, hashes, engine outputs, approvals of own-pay
# lines; a PAYMENT hold may still be placed or released, EC-RUN-13), deleting snapshots, the line sources
# (a late consumed source, such as an arrears instalment settled by retro, may still be added) and the
# approval history of a finalized run.
_FINALIZED_RUN_STATUSES = ('FINALIZED', 'PAYMENT_READY', 'SUBMITTED', 'PARTIALLY_PAID', 'PAID', 'RECONCILED', 'CLOSED')
_FINALIZED_SQL = "(" + ", ".join(f"'{s}'" for s in _FINALIZED_RUN_STATUSES) + ")"
_RUN_EMPLOYEE_RESULT_COLUMNS = (
    'run_id', 'employee_id', 'employee_assignment_id', 'status', 'exclusion_reason', 'gross', 'deductions',
    'reimbursements', 'net', 'employer_cost', 'currency', 'snapshot_id', 'content_hash', 'calc_hash', 'calculated_at',
    'stage_outputs', 'error', 'flags', 'self_line_approved_by_user_id',
)
_FINALIZE_GUARDS = (
    # (function, table, trigger, events)
    ('payroll_run_employees_finalized_guard', 'payroll_run_employees', 'trg_payroll_run_employees_finalized',
     'BEFORE UPDATE OR DELETE'),
    ('payroll_input_snapshots_finalized_guard', 'payroll_input_snapshots', 'trg_payroll_input_snapshots_finalized',
     'BEFORE DELETE'),
    ('payroll_line_sources_finalized_guard', 'payroll_line_sources', 'trg_payroll_line_sources_finalized',
     'BEFORE INSERT OR UPDATE OR DELETE'),
    ('payroll_approvals_finalized_guard', 'payroll_approvals', 'trg_payroll_approvals_finalized',
     'BEFORE UPDATE OR DELETE'),
)


def _finalize_functions() -> list:
    changed = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in _RUN_EMPLOYEE_RESULT_COLUMNS)
    finalized = f"EXISTS (SELECT 1 FROM payroll_runs r WHERE r.id = {{run}} AND r.status IN {_FINALIZED_SQL})"
    return [
        f"""
CREATE OR REPLACE FUNCTION payroll_run_employees_finalized_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF {finalized.format(run='OLD.run_id')} THEN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'RUN_FINALIZED: employees of a finalized run cannot be removed; create a correction'
                USING ERRCODE = 'check_violation';
        END IF;
        IF {changed} THEN
            RAISE EXCEPTION 'RUN_FINALIZED: results of a finalized run cannot change; create a correction'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$
""",
        f"""
CREATE OR REPLACE FUNCTION payroll_input_snapshots_finalized_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM payroll_run_employees e JOIN payroll_runs r ON r.id = e.run_id
               WHERE e.id = OLD.run_employee_id AND r.status IN {_FINALIZED_SQL}) THEN
        RAISE EXCEPTION 'RUN_FINALIZED: snapshots of a finalized run are kept for replay'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN OLD;
END;
$$
""",
        f"""
CREATE OR REPLACE FUNCTION payroll_line_sources_finalized_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NOT NEW.consumed AND {finalized.format(run='NEW.run_id')} THEN
            RAISE EXCEPTION 'RUN_FINALIZED: only a consumed source can be linked to a finalized run'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF {finalized.format(run='OLD.run_id')} THEN
        RAISE EXCEPTION 'RUN_FINALIZED: consumed sources of a finalized run cannot change'
            USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$
""",
        f"""
CREATE OR REPLACE FUNCTION payroll_approvals_finalized_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF {finalized.format(run='OLD.run_id')} THEN
        RAISE EXCEPTION 'RUN_FINALIZED: the approvals of a finalized run are history'
            USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$
""",
    ]


def upgrade_finalize() -> None:
    for statement in _finalize_functions():
        op.execute(statement)
    for function, table, trigger, events in _FINALIZE_GUARDS:
        op.execute(f"CREATE TRIGGER {trigger} {events} ON {table} FOR EACH ROW EXECUTE FUNCTION {function}()")


def downgrade_finalize() -> None:
    for function, table, trigger, _ in reversed(_FINALIZE_GUARDS):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")


def upgrade() -> None:
    upgrade_snapshot()
    upgrade_calculation()
    upgrade_finalize()


def downgrade() -> None:
    downgrade_finalize()
    downgrade_calculation()
    downgrade_snapshot()

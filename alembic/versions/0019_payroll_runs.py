"""Payroll runs v2: pay calendars and periods, runs, run employees, input snapshots, lines and line sources, exceptions, approvals, holds, YTD balances, statutory liabilities and attestations.

Additive (3.5, 3.9 §3.6, decision I-15); every table is a tenant table with row-level security,
grants and composite same-company foreign keys (app.models.pay_runs documents each table).

Database rules that services cannot be trusted to keep alone:

* pay_periods_guard: periods of one pay group never overlap (EC-CAL-12; a trigger because this
  PostgreSQL build has no btree_gist, decision I-05) and belong to their calendar's pay group.
* uq_payroll_runs_regular: one non-cancelled regular run per pay group, period and regular
  sequence (EC-RUN-01); payroll_runs_guard keeps the period inside the run's pay group and freezes
  the identity and results of a finalized run (EC-RUN-17).
* payroll_run_employees_guard: the assignment belongs to the employee and company, and one regular
  payment per (assignment, period) across non-cancelled regular runs (EC-RUN-02), serialised with
  an advisory lock so two concurrent runs cannot both pass.
* payroll_input_snapshots are immutable; payroll_lines of a finalized run cannot change.
* uq_payroll_line_sources_consumed: a consumable source is consumed by one finalized line (EC-VAR-01/08).
* statutory_attestations: approver differs from the attester (maker-checker, I-15).

Downgrade drops the tables, triggers and functions.

Revision ID: 0019_payroll_runs
Revises: 0018_variable_inputs
Create Date: 2026-10-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0019_payroll_runs'
down_revision: Union[str, None] = '0018_variable_inputs'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies: later model changes must not alter what this revision does.
TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
NEW_TABLES = ("pay_calendars", "pay_periods", "payroll_runs", "payroll_run_employees", "payroll_input_snapshots",
              "payroll_lines", "payroll_line_sources", "payroll_exceptions", "payroll_approvals", "ytd_balances",
              "statutory_liabilities", "statutory_attestations")
PAY_FREQUENCIES = ("MONTHLY", "SEMI_MONTHLY", "BI_WEEKLY", "WEEKLY", "DAILY")
CALENDAR_STATUSES = ("ACTIVE", "ENDED")
PERIOD_STATUSES = ("OPEN", "PROCESSING", "CLOSED")
RUN_TYPES = ("REGULAR", "OFF_CYCLE", "RETRO_ADJUSTMENT", "FINAL_SETTLEMENT", "REVERSAL")
RUN_STATUSES = (
    "DRAFT", "INPUTS_PENDING", "READY", "CALCULATING", "CALCULATED", "VALIDATION_FAILED", "REVIEW_REQUIRED",
    "APPROVAL_PENDING", "APPROVED", "REJECTED", "REOPENED_FOR_CORRECTION", "CANCELLED", "FINALIZED",
    "PAYMENT_READY", "SUBMITTED", "PARTIALLY_PAID", "PAID", "RECONCILED", "CLOSED",
)
FINALIZED_STATUSES = ("FINALIZED", "PAYMENT_READY", "SUBMITTED", "PARTIALLY_PAID", "PAID", "RECONCILED", "CLOSED")
RUN_ENVIRONMENTS = ("development", "test", "staging", "production")
RUN_EMPLOYEE_STATUSES = ("PENDING", "CALCULATED", "ERROR", "ON_HOLD", "EXCLUDED")
EXCLUSION_REASONS = (
    "NO_SALARY", "INVALID_ASSIGNMENT", "JOINED_AFTER_PERIOD", "LEFT_BEFORE_PERIOD", "SUSPENDED_ALL_PERIOD",
    "INELIGIBLE", "REMOVED", "NOT_SELECTED",
)
HOLD_TYPES = ("PAYMENT", "FULL")
LINE_KINDS = (
    "EARNING", "DEDUCTION", "EMPLOYER_CONTRIBUTION", "REIMBURSEMENT", "BENEFIT_IN_KIND", "INFORMATIONAL", "ROUNDING",
)
LINE_VISIBILITIES = ("EMPLOYEE_VISIBLE", "HR_ONLY")
LINE_SOURCE_TYPES = ("WAGE_INPUT", "OVERTIME", "TASK", "LOAN", "CLAIM", "ARREARS", "LEAVE_ENCASHMENT")
EXCEPTION_SEVERITIES = ("BLOCKING", "WARNING", "FLAG")
EXCEPTION_STATUSES = ("OPEN", "FIXED", "WAIVED", "DEFERRED")
EXCEPTION_SOURCES = ("PRECHECK", "CALCULATION", "VALIDATION", "SYSTEM")
DEFER_TARGETS = ("NEXT_PERIOD", "RETRO")
APPROVAL_DECISIONS = ("APPROVED", "REJECTED")
APPROVAL_ACTED_AS = ("approver", "delegate", "override")
LIABILITY_STATUSES = ("OPEN", "REMITTED", "RECONCILED")
ATTESTATION_STATUSES = ("PENDING", "ACTIVE", "REVOKED")

FUNCTIONS = ("pay_periods_guard", "payroll_runs_guard", "payroll_run_employees_guard",
             "payroll_input_snapshots_immutable", "payroll_lines_guard")

PAY_PERIODS_GUARD = """
CREATE OR REPLACE FUNCTION pay_periods_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pay_calendars c
        WHERE c.id = NEW.pay_calendar_id AND c.company_id = NEW.company_id AND c.pay_group_id = NEW.pay_group_id
    ) THEN
        RAISE EXCEPTION 'The pay period belongs to another pay group than its calendar'
            USING ERRCODE = 'check_violation';
    END IF;
    -- Serialise period writes per pay group, then reject overlapping date ranges (EC-CAL-12).
    PERFORM pg_advisory_xact_lock(hashtext('pay_periods:' || NEW.company_id::text || ':' || NEW.pay_group_id::text));
    IF EXISTS (
        SELECT 1 FROM pay_periods p
        WHERE p.company_id = NEW.company_id AND p.pay_group_id = NEW.pay_group_id AND p.id <> NEW.id
          AND p.period_start <= NEW.period_end AND p.period_end >= NEW.period_start
    ) THEN
        RAISE EXCEPTION 'Pay periods of one pay group cannot overlap' USING ERRCODE = 'exclusion_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

PAYROLL_RUNS_GUARD = f"""
CREATE OR REPLACE FUNCTION payroll_runs_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' OR NEW.pay_period_id IS DISTINCT FROM OLD.pay_period_id
            OR NEW.pay_group_id IS DISTINCT FROM OLD.pay_group_id THEN
        IF NOT EXISTS (
            SELECT 1 FROM pay_periods p
            WHERE p.id = NEW.pay_period_id AND p.company_id = NEW.company_id AND p.pay_group_id = NEW.pay_group_id
        ) THEN
            RAISE EXCEPTION 'The pay period belongs to another pay group' USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.status IN {FINALIZED_STATUSES} THEN
        IF NEW.status NOT IN {FINALIZED_STATUSES}
                OR NEW.number IS DISTINCT FROM OLD.number
                OR NEW.legal_entity_id IS DISTINCT FROM OLD.legal_entity_id
                OR NEW.pay_group_id IS DISTINCT FROM OLD.pay_group_id
                OR NEW.pay_period_id IS DISTINCT FROM OLD.pay_period_id
                OR NEW.run_type IS DISTINCT FROM OLD.run_type
                OR NEW.currency IS DISTINCT FROM OLD.currency
                OR NEW.totals IS DISTINCT FROM OLD.totals
                OR NEW.population IS DISTINCT FROM OLD.population
                OR NEW.pack_version_ids IS DISTINCT FROM OLD.pack_version_ids
                OR NEW.fx_snapshot_id IS DISTINCT FROM OLD.fx_snapshot_id
                OR NEW.engine_version IS DISTINCT FROM OLD.engine_version
                OR NEW.finalized_at IS DISTINCT FROM OLD.finalized_at
                OR NEW.finalized_by_user_id IS DISTINCT FROM OLD.finalized_by_user_id THEN
            RAISE EXCEPTION 'RUN_FINALIZED: a finalized run is immutable; create a correction'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$
"""

RUN_EMPLOYEES_GUARD = """
CREATE OR REPLACE FUNCTION payroll_run_employees_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    run_row RECORD;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM employee_assignments a
        WHERE a.id = NEW.employee_assignment_id AND a.company_id = NEW.company_id AND a.employee_id = NEW.employee_id
    ) THEN
        RAISE EXCEPTION 'The assignment belongs to another employee' USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF NEW.status = 'EXCLUDED' THEN
        RETURN NEW;
    END IF;
    SELECT r.pay_period_id, r.run_type, r.status INTO run_row
    FROM payroll_runs r WHERE r.id = NEW.run_id AND r.company_id = NEW.company_id;
    IF run_row.run_type = 'REGULAR' AND run_row.status <> 'CANCELLED' THEN
        -- One regular payment per assignment and period across live regular runs (EC-RUN-02).
        PERFORM pg_advisory_xact_lock(hashtext('payroll_run_employees:' || NEW.company_id::text || ':'
                                               || NEW.employee_assignment_id::text || ':'
                                               || run_row.pay_period_id::text));
        IF EXISTS (
            SELECT 1 FROM payroll_run_employees e
            JOIN payroll_runs r2 ON r2.id = e.run_id AND r2.company_id = e.company_id
            WHERE e.company_id = NEW.company_id AND e.employee_assignment_id = NEW.employee_assignment_id
              AND e.id <> NEW.id AND e.run_id <> NEW.run_id AND e.status <> 'EXCLUDED'
              AND r2.pay_period_id = run_row.pay_period_id AND r2.run_type = 'REGULAR' AND r2.status <> 'CANCELLED'
        ) THEN
            RAISE EXCEPTION 'The assignment is already paid by another regular run of the period'
                USING ERRCODE = 'unique_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$
"""

SNAPSHOTS_IMMUTABLE = """
CREATE OR REPLACE FUNCTION payroll_input_snapshots_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'payroll_input_snapshots are immutable: take a new version' USING ERRCODE = 'check_violation';
END;
$$
"""

LINES_GUARD = f"""
CREATE OR REPLACE FUNCTION payroll_lines_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    target_run integer;
BEGIN
    target_run := CASE WHEN TG_OP = 'DELETE' THEN OLD.run_id ELSE NEW.run_id END;
    IF EXISTS (SELECT 1 FROM payroll_runs r WHERE r.id = target_run AND r.status IN {FINALIZED_STATUSES}) THEN
        RAISE EXCEPTION 'RUN_FINALIZED: lines of a finalized run cannot change' USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP <> 'DELETE' AND NOT EXISTS (
        SELECT 1 FROM payroll_run_employees e WHERE e.id = NEW.run_employee_id AND e.run_id = NEW.run_id
    ) THEN
        RAISE EXCEPTION 'The line belongs to another run' USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$
"""


def _base():
    return [
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    ]


def _fk(table, columns, target, name, ondelete=None):
    local = ['company_id', *([columns] if isinstance(columns, str) else columns)]
    return sa.ForeignKeyConstraint(local, [f'{target}.company_id', f'{target}.id'], name=f'fk_{table}_{name}',
                                   ondelete=ondelete)


def _currency(table, column='currency'):
    return sa.CheckConstraint(f"{column} ~ '^[A-Z]{{3}}$'", name=f'ck_{table}_{column}')


def _uq(table):
    return sa.UniqueConstraint('company_id', 'id', name=f'uq_{table}_company_id_id')


def _version(table):
    return sa.CheckConstraint("row_version >= 1", name=f'ck_{table}_row_version')


def _row_version():
    return sa.Column('row_version', sa.Integer(), nullable=False, server_default='1')


def upgrade() -> None:
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    empty_object, empty_array = sa.text("'{}'::jsonb"), sa.text("'[]'::jsonb")

    op.create_table(
        'pay_calendars', *_base(),
        sa.Column('pay_group_id', sa.Integer(), nullable=False),
        sa.Column('frequency', sa.String(15), nullable=False),
        sa.Column('rules', jsonb, nullable=False, server_default=empty_object),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('effective_to', sa.Date(), nullable=True),
        sa.Column('status', sa.String(10), nullable=False, server_default='ACTIVE'),
        sa.Column('generated_until', sa.Date(), nullable=True),
        sa.Column('banking_calendar_id', sa.Integer(), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        _row_version(),
        _uq('pay_calendars'),
        _fk('pay_calendars', 'pay_group_id', 'pay_groups', 'pay_group'),
        _fk('pay_calendars', 'banking_calendar_id', 'holiday_calendars', 'banking_calendar'),
        _fk('pay_calendars', 'created_by_user_id', 'users', 'created_by'),
        sa.CheckConstraint(f"frequency IN {PAY_FREQUENCIES}", name='ck_pay_calendars_frequency'),
        sa.CheckConstraint(f"status IN {CALENDAR_STATUSES}", name='ck_pay_calendars_status'),
        sa.CheckConstraint("jsonb_typeof(rules) = 'object'", name='ck_pay_calendars_rules'),
        sa.CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name='ck_pay_calendars_window'),
        sa.CheckConstraint("(status = 'ENDED') = (effective_to IS NOT NULL)", name='ck_pay_calendars_ended'),
        _version('pay_calendars'),
    )
    op.create_index('ix_pay_calendars_company_id', 'pay_calendars', ['company_id'])
    op.create_index('ix_pay_calendars_pay_group_id', 'pay_calendars', ['pay_group_id'])
    op.create_index('uq_pay_calendars_open', 'pay_calendars', ['company_id', 'pay_group_id'], unique=True,
                    postgresql_where=sa.text("effective_to IS NULL"))

    op.create_table(
        'pay_periods', *_base(),
        sa.Column('pay_calendar_id', sa.Integer(), nullable=False),
        sa.Column('pay_group_id', sa.Integer(), nullable=False),
        sa.Column('frequency', sa.String(15), nullable=False),
        sa.Column('period_start', sa.Date(), nullable=False),
        sa.Column('period_end', sa.Date(), nullable=False),
        sa.Column('attendance_start', sa.Date(), nullable=False),
        sa.Column('attendance_end', sa.Date(), nullable=False),
        sa.Column('pay_date', sa.Date(), nullable=False),
        sa.Column('nominal_pay_date', sa.Date(), nullable=False),
        sa.Column('input_cutoff_on', sa.Date(), nullable=False),
        sa.Column('approval_deadline_on', sa.Date(), nullable=False),
        sa.Column('payment_submit_by', sa.Date(), nullable=False),
        sa.Column('tax_year', sa.String(9), nullable=False),
        sa.Column('number_in_tax_year', sa.Integer(), nullable=False),
        sa.Column('is_last_in_tax_year', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('is_transition', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('status', sa.String(12), nullable=False, server_default='OPEN'),
        sa.Column('pay_date_warnings', jsonb, nullable=False, server_default=empty_array),
        _row_version(),
        _uq('pay_periods'),
        _fk('pay_periods', 'pay_calendar_id', 'pay_calendars', 'calendar'),
        _fk('pay_periods', 'pay_group_id', 'pay_groups', 'pay_group'),
        sa.UniqueConstraint('company_id', 'pay_group_id', 'period_start', name='uq_pay_periods_group_start'),
        sa.CheckConstraint(f"frequency IN {PAY_FREQUENCIES}", name='ck_pay_periods_frequency'),
        sa.CheckConstraint(f"status IN {PERIOD_STATUSES}", name='ck_pay_periods_status'),
        sa.CheckConstraint("period_end >= period_start", name='ck_pay_periods_window'),
        sa.CheckConstraint("attendance_end >= attendance_start - 1 AND attendance_end <= period_end",
                           name='ck_pay_periods_attendance'),
        sa.CheckConstraint("input_cutoff_on <= approval_deadline_on AND approval_deadline_on <= pay_date",
                           name='ck_pay_periods_cutoffs'),
        sa.CheckConstraint("number_in_tax_year >= 1", name='ck_pay_periods_number'),
        sa.CheckConstraint("jsonb_typeof(pay_date_warnings) = 'array'", name='ck_pay_periods_warnings'),
        _version('pay_periods'),
    )
    op.create_index('ix_pay_periods_company_id', 'pay_periods', ['company_id'])
    op.create_index('ix_pay_periods_pay_calendar_id', 'pay_periods', ['pay_calendar_id'])
    op.create_index('ix_pay_periods_pay_group_id', 'pay_periods', ['pay_group_id'])
    op.create_index('ix_pay_periods_group_dates', 'pay_periods',
                    ['company_id', 'pay_group_id', 'period_start', 'period_end'])

    op.create_table(
        'payroll_runs', *_base(),
        sa.Column('number', sa.String(20), nullable=False),
        sa.Column('label', sa.String(150), nullable=False),
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('pay_group_id', sa.Integer(), nullable=False),
        sa.Column('pay_period_id', sa.Integer(), nullable=False),
        sa.Column('run_type', sa.String(20), nullable=False),
        sa.Column('status', sa.String(25), nullable=False, server_default='DRAFT'),
        sa.Column('regular_sequence', sa.Integer(), nullable=True),
        sa.Column('currency', sa.CHAR(3), nullable=False),
        sa.Column('reverses_run_id', sa.Integer(), nullable=True),
        sa.Column('selection', jsonb, nullable=False, server_default=empty_object),
        sa.Column('pack_version_ids', jsonb, nullable=False, server_default=empty_array),
        sa.Column('fx_snapshot_id', sa.Integer(), nullable=True),
        sa.Column('totals', jsonb, nullable=False, server_default=empty_object),
        sa.Column('population', jsonb, nullable=False, server_default=empty_object),
        sa.Column('stale', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('stale_reasons', jsonb, nullable=False, server_default=empty_array),
        sa.Column('environment', sa.String(12), nullable=False),
        sa.Column('engine_version', sa.String(20), nullable=True),
        sa.Column('calculation_job_id', sa.Integer(), nullable=True),
        sa.Column('approval_request_id', sa.Integer(), nullable=True),
        sa.Column('prechecked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('calculated_by_user_id', sa.Integer(), nullable=True),
        sa.Column('calculated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('submitted_by_user_id', sa.Integer(), nullable=True),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finalized_by_user_id', sa.Integer(), nullable=True),
        sa.Column('finalized_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cancelled_by_user_id', sa.Integer(), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status_reason', sa.Text(), nullable=True),
        _row_version(),
        _uq('payroll_runs'),
        sa.UniqueConstraint('company_id', 'number', name='uq_payroll_runs_number'),
        _fk('payroll_runs', 'legal_entity_id', 'legal_entities', 'legal_entity'),
        sa.ForeignKeyConstraint(
            ['company_id', 'legal_entity_id', 'pay_group_id'],
            ['pay_groups.company_id', 'pay_groups.legal_entity_id', 'pay_groups.id'],
            name='fk_payroll_runs_pay_group',
        ),
        _fk('payroll_runs', 'pay_period_id', 'pay_periods', 'pay_period'),
        _fk('payroll_runs', 'reverses_run_id', 'payroll_runs', 'reverses'),
        _fk('payroll_runs', 'fx_snapshot_id', 'fx_snapshots', 'fx_snapshot'),
        sa.ForeignKeyConstraint(['calculation_job_id'], ['jobs.id'], name='fk_payroll_runs_calculation_job',
                                ondelete='SET NULL'),
        _fk('payroll_runs', 'approval_request_id', 'approval_requests', 'approval_request'),
        _fk('payroll_runs', 'created_by_user_id', 'users', 'created_by'),
        _fk('payroll_runs', 'calculated_by_user_id', 'users', 'calculated_by'),
        _fk('payroll_runs', 'submitted_by_user_id', 'users', 'submitted_by'),
        _fk('payroll_runs', 'finalized_by_user_id', 'users', 'finalized_by'),
        _fk('payroll_runs', 'cancelled_by_user_id', 'users', 'cancelled_by'),
        sa.CheckConstraint(f"run_type IN {RUN_TYPES}", name='ck_payroll_runs_type'),
        sa.CheckConstraint(f"status IN {RUN_STATUSES}", name='ck_payroll_runs_status'),
        sa.CheckConstraint(f"environment IN {RUN_ENVIRONMENTS}", name='ck_payroll_runs_environment'),
        _currency('payroll_runs'),
        sa.CheckConstraint("number ~ '^PR-[0-9]{4}-[0-9]{6}$'", name='ck_payroll_runs_number'),
        sa.CheckConstraint("(run_type = 'REGULAR') = (regular_sequence IS NOT NULL)",
                           name='ck_payroll_runs_regular_seq'),
        sa.CheckConstraint("regular_sequence IS NULL OR regular_sequence >= 1", name='ck_payroll_runs_regular_seq_pos'),
        sa.CheckConstraint("(run_type = 'REVERSAL') = (reverses_run_id IS NOT NULL)", name='ck_payroll_runs_reversal'),
        sa.CheckConstraint("reverses_run_id IS NULL OR reverses_run_id <> id", name='ck_payroll_runs_not_self'),
        sa.CheckConstraint("jsonb_typeof(selection) = 'object'", name='ck_payroll_runs_selection'),
        sa.CheckConstraint("jsonb_typeof(pack_version_ids) = 'array'", name='ck_payroll_runs_packs'),
        sa.CheckConstraint("jsonb_typeof(totals) = 'object'", name='ck_payroll_runs_totals'),
        sa.CheckConstraint("jsonb_typeof(population) = 'object'", name='ck_payroll_runs_population'),
        sa.CheckConstraint("jsonb_typeof(stale_reasons) = 'array'", name='ck_payroll_runs_stale_reasons'),
        sa.CheckConstraint("stale OR stale_reasons = '[]'::jsonb", name='ck_payroll_runs_stale'),
        sa.CheckConstraint("(status = 'CANCELLED') = (cancelled_at IS NOT NULL)", name='ck_payroll_runs_cancelled'),
        sa.CheckConstraint(f"(finalized_at IS NULL) = (status NOT IN {FINALIZED_STATUSES})",
                           name='ck_payroll_runs_finalized'),
        _version('payroll_runs'),
    )
    op.create_index('ix_payroll_runs_company_id', 'payroll_runs', ['company_id'])
    op.create_index('ix_payroll_runs_legal_entity_id', 'payroll_runs', ['legal_entity_id'])
    op.create_index('ix_payroll_runs_pay_group_id', 'payroll_runs', ['pay_group_id'])
    op.create_index('ix_payroll_runs_pay_period_id', 'payroll_runs', ['pay_period_id'])
    op.create_index('uq_payroll_runs_regular', 'payroll_runs',
                    ['company_id', 'pay_group_id', 'pay_period_id', 'regular_sequence'], unique=True,
                    postgresql_where=sa.text("run_type = 'REGULAR' AND status <> 'CANCELLED'"))
    op.create_index('ix_payroll_runs_filters', 'payroll_runs', ['company_id', 'pay_period_id', 'status'])

    op.create_table(
        'payroll_input_snapshots', *_base(),
        sa.Column('run_employee_id', sa.Integer(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('content', jsonb, nullable=False),
        sa.Column('content_hash', sa.CHAR(64), nullable=False),
        sa.Column('source_hashes', jsonb, nullable=False, server_default=empty_object),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        _uq('payroll_input_snapshots'),
        sa.UniqueConstraint('run_employee_id', 'version', name='uq_payroll_input_snapshots_version'),
        _fk('payroll_input_snapshots', 'created_by_user_id', 'users', 'created_by'),
        sa.CheckConstraint("version >= 1", name='ck_payroll_input_snapshots_version'),
        sa.CheckConstraint("jsonb_typeof(content) = 'object'", name='ck_payroll_input_snapshots_content'),
        sa.CheckConstraint("jsonb_typeof(source_hashes) = 'object'", name='ck_payroll_input_snapshots_sources'),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name='ck_payroll_input_snapshots_hash'),
    )
    op.create_index('ix_payroll_input_snapshots_company_id', 'payroll_input_snapshots', ['company_id'])
    op.create_index('ix_payroll_input_snapshots_run_employee_id', 'payroll_input_snapshots', ['run_employee_id'])

    op.create_table(
        'payroll_run_employees', *_base(),
        sa.Column('run_id', sa.Integer(), nullable=False),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('employee_assignment_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(12), nullable=False, server_default='PENDING'),
        sa.Column('exclusion_reason', sa.String(30), nullable=True),
        sa.Column('exclusion_note', sa.String(500), nullable=True),
        sa.Column('hold_type', sa.String(10), nullable=True),
        sa.Column('hold', jsonb, nullable=True),
        sa.Column('gross', sa.Numeric(19, 4), nullable=True),
        sa.Column('deductions', sa.Numeric(19, 4), nullable=True),
        sa.Column('reimbursements', sa.Numeric(19, 4), nullable=True),
        sa.Column('net', sa.Numeric(19, 4), nullable=True),
        sa.Column('employer_cost', sa.Numeric(19, 4), nullable=True),
        sa.Column('currency', sa.CHAR(3), nullable=False),
        sa.Column('snapshot_id', sa.Integer(), nullable=True),
        sa.Column('content_hash', sa.CHAR(64), nullable=True),
        sa.Column('calc_hash', sa.CHAR(64), nullable=True),
        sa.Column('calculated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error', jsonb, nullable=True),
        sa.Column('flags', jsonb, nullable=False, server_default=empty_array),
        sa.Column('self_line_approved_by_user_id', sa.Integer(), nullable=True),
        _row_version(),
        _uq('payroll_run_employees'),
        sa.UniqueConstraint('run_id', 'employee_id', 'employee_assignment_id',
                            name='uq_payroll_run_employees_identity'),
        _fk('payroll_run_employees', 'run_id', 'payroll_runs', 'run', ondelete='CASCADE'),
        _fk('payroll_run_employees', 'employee_id', 'employees', 'employee'),
        sa.ForeignKeyConstraint(['company_id', 'employee_assignment_id'],
                                ['employee_assignments.company_id', 'employee_assignments.id'],
                                name='fk_payroll_run_employees_assignment'),
        _fk('payroll_run_employees', 'snapshot_id', 'payroll_input_snapshots', 'snapshot'),
        _fk('payroll_run_employees', 'self_line_approved_by_user_id', 'users', 'self_line_approver'),
        sa.CheckConstraint(f"status IN {RUN_EMPLOYEE_STATUSES}", name='ck_payroll_run_employees_status'),
        sa.CheckConstraint(f"exclusion_reason IS NULL OR exclusion_reason IN {EXCLUSION_REASONS}",
                           name='ck_payroll_run_employees_exclusion_reason'),
        sa.CheckConstraint("(status = 'EXCLUDED') = (exclusion_reason IS NOT NULL)",
                           name='ck_payroll_run_employees_excluded'),
        sa.CheckConstraint(f"hold_type IS NULL OR hold_type IN {HOLD_TYPES}",
                           name='ck_payroll_run_employees_hold_type'),
        sa.CheckConstraint("(hold_type IS NULL) = (hold IS NULL)", name='ck_payroll_run_employees_hold'),
        sa.CheckConstraint("status <> 'ON_HOLD' OR hold_type = 'FULL'", name='ck_payroll_run_employees_on_hold'),
        sa.CheckConstraint("hold IS NULL OR jsonb_typeof(hold) = 'object'",
                           name='ck_payroll_run_employees_hold_shape'),
        sa.CheckConstraint("jsonb_typeof(flags) = 'array'", name='ck_payroll_run_employees_flags'),
        _currency('payroll_run_employees'),
        sa.CheckConstraint("content_hash IS NULL OR content_hash ~ '^[0-9a-f]{64}$'",
                           name='ck_payroll_run_employees_content_hash'),
        sa.CheckConstraint("calc_hash IS NULL OR calc_hash ~ '^[0-9a-f]{64}$'",
                           name='ck_payroll_run_employees_calc_hash'),
        _version('payroll_run_employees'),
    )
    op.create_index('ix_payroll_run_employees_company_id', 'payroll_run_employees', ['company_id'])
    op.create_index('ix_payroll_run_employees_run_id', 'payroll_run_employees', ['run_id'])
    op.create_index('ix_payroll_run_employees_employee_id', 'payroll_run_employees', ['employee_id'])
    op.create_index('ix_payroll_run_employees_employee_assignment_id', 'payroll_run_employees',
                    ['employee_assignment_id'])
    op.create_index('ix_payroll_run_employees_run_status', 'payroll_run_employees', ['company_id', 'run_id', 'status'])
    op.create_foreign_key('fk_payroll_input_snapshots_run_employee', 'payroll_input_snapshots',
                          'payroll_run_employees', ['company_id', 'run_employee_id'], ['company_id', 'id'],
                          ondelete='CASCADE')

    op.create_table(
        'payroll_lines', *_base(),
        sa.Column('run_id', sa.Integer(), nullable=False),
        sa.Column('run_employee_id', sa.Integer(), nullable=False),
        sa.Column('component_code', sa.String(40), nullable=False),
        sa.Column('component_id', sa.Integer(), nullable=True),
        sa.Column('component_version_id', sa.Integer(), nullable=True),
        sa.Column('kind', sa.String(25), nullable=False),
        sa.Column('category', sa.String(40), nullable=True),
        sa.Column('stage', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Numeric(19, 4), nullable=False),
        sa.Column('currency', sa.CHAR(3), nullable=False),
        sa.Column('units', sa.Numeric(14, 6), nullable=True),
        sa.Column('rate', sa.Numeric(24, 10), nullable=True),
        sa.Column('period_ref', jsonb, nullable=True),
        sa.Column('segment', jsonb, nullable=True),
        sa.Column('tags', jsonb, nullable=False, server_default=empty_array),
        sa.Column('visibility', sa.String(20), nullable=False, server_default='EMPLOYEE_VISIBLE'),
        sa.Column('sources', jsonb, nullable=False, server_default=empty_array),
        sa.Column('trace', jsonb, nullable=False, server_default=empty_object),
        sa.Column('explanation', sa.Text(), nullable=True),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('statutory', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('fixture', sa.Boolean(), nullable=False, server_default=sa.false()),
        _uq('payroll_lines'),
        _fk('payroll_lines', 'run_id', 'payroll_runs', 'run', ondelete='CASCADE'),
        _fk('payroll_lines', 'run_employee_id', 'payroll_run_employees', 'run_employee', ondelete='CASCADE'),
        _fk('payroll_lines', 'component_id', 'pay_components', 'component'),
        _fk('payroll_lines', 'component_version_id', 'pay_component_versions', 'component_version'),
        sa.CheckConstraint(f"kind IN {LINE_KINDS}", name='ck_payroll_lines_kind'),
        sa.CheckConstraint(f"visibility IN {LINE_VISIBILITIES}", name='ck_payroll_lines_visibility'),
        sa.CheckConstraint("stage BETWEEN 1 AND 12", name='ck_payroll_lines_stage'),
        _currency('payroll_lines'),
        sa.CheckConstraint("jsonb_typeof(tags) = 'array'", name='ck_payroll_lines_tags'),
        sa.CheckConstraint("jsonb_typeof(sources) = 'array'", name='ck_payroll_lines_sources'),
        sa.CheckConstraint("jsonb_typeof(trace) = 'object'", name='ck_payroll_lines_trace'),
        sa.CheckConstraint("period_ref IS NULL OR jsonb_typeof(period_ref) = 'object'",
                           name='ck_payroll_lines_period_ref'),
        sa.CheckConstraint("segment IS NULL OR jsonb_typeof(segment) = 'object'", name='ck_payroll_lines_segment'),
    )
    op.create_index('ix_payroll_lines_company_id', 'payroll_lines', ['company_id'])
    op.create_index('ix_payroll_lines_run_id', 'payroll_lines', ['run_id'])
    op.create_index('ix_payroll_lines_run_employee_id', 'payroll_lines', ['run_employee_id'])
    op.create_index('ix_payroll_lines_run_employee_order', 'payroll_lines',
                    ['company_id', 'run_employee_id', 'sort_order'])

    op.create_table(
        'payroll_line_sources', *_base(),
        sa.Column('line_id', sa.Integer(), nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=False),
        sa.Column('run_employee_id', sa.Integer(), nullable=False),
        sa.Column('source_type', sa.String(20), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=False),
        sa.Column('source_key', sa.String(80), nullable=False, server_default=''),
        sa.Column('consumed', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
        _uq('payroll_line_sources'),
        sa.UniqueConstraint('line_id', 'source_type', 'source_id', 'source_key', name='uq_payroll_line_sources_line'),
        _fk('payroll_line_sources', 'line_id', 'payroll_lines', 'line', ondelete='CASCADE'),
        _fk('payroll_line_sources', 'run_id', 'payroll_runs', 'run', ondelete='CASCADE'),
        _fk('payroll_line_sources', 'run_employee_id', 'payroll_run_employees', 'run_employee', ondelete='CASCADE'),
        sa.CheckConstraint(f"source_type IN {LINE_SOURCE_TYPES}", name='ck_payroll_line_sources_type'),
        sa.CheckConstraint("consumed = (consumed_at IS NOT NULL)", name='ck_payroll_line_sources_consumed'),
    )
    op.create_index('ix_payroll_line_sources_company_id', 'payroll_line_sources', ['company_id'])
    op.create_index('ix_payroll_line_sources_line_id', 'payroll_line_sources', ['line_id'])
    op.create_index('ix_payroll_line_sources_run_id', 'payroll_line_sources', ['run_id'])
    op.create_index('ix_payroll_line_sources_run_employee_id', 'payroll_line_sources', ['run_employee_id'])
    op.create_index('uq_payroll_line_sources_consumed', 'payroll_line_sources',
                    ['company_id', 'source_type', 'source_id', 'source_key'], unique=True,
                    postgresql_where=sa.text("consumed"))
    op.create_index('ix_payroll_line_sources_source', 'payroll_line_sources', ['company_id', 'source_type', 'source_id'])

    op.create_table(
        'payroll_exceptions', *_base(),
        sa.Column('run_id', sa.Integer(), nullable=False),
        sa.Column('run_employee_id', sa.Integer(), nullable=True),
        sa.Column('employee_id', sa.Integer(), nullable=True),
        sa.Column('code', sa.String(40), nullable=False),
        sa.Column('severity', sa.String(10), nullable=False),
        sa.Column('source', sa.String(12), nullable=False, server_default='PRECHECK'),
        sa.Column('message', sa.String(1000), nullable=False),
        sa.Column('details', jsonb, nullable=False, server_default=empty_object),
        sa.Column('resolutions', jsonb, nullable=False, server_default=empty_array),
        sa.Column('fix_link', sa.String(300), nullable=True),
        sa.Column('waivable', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('blocks_finalization', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('dedupe_key', sa.String(200), nullable=False),
        sa.Column('status', sa.String(10), nullable=False, server_default='OPEN'),
        sa.Column('resolved_by_user_id', sa.Integer(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolution_note', sa.Text(), nullable=True),
        sa.Column('deferred_to', sa.String(12), nullable=True),
        sa.Column('deferred_to_period_id', sa.Integer(), nullable=True),
        _row_version(),
        _uq('payroll_exceptions'),
        _fk('payroll_exceptions', 'run_id', 'payroll_runs', 'run', ondelete='CASCADE'),
        _fk('payroll_exceptions', 'run_employee_id', 'payroll_run_employees', 'run_employee', ondelete='CASCADE'),
        _fk('payroll_exceptions', 'employee_id', 'employees', 'employee'),
        _fk('payroll_exceptions', 'resolved_by_user_id', 'users', 'resolved_by'),
        _fk('payroll_exceptions', 'deferred_to_period_id', 'pay_periods', 'deferred_to_period'),
        sa.CheckConstraint(f"severity IN {EXCEPTION_SEVERITIES}", name='ck_payroll_exceptions_severity'),
        sa.CheckConstraint(f"status IN {EXCEPTION_STATUSES}", name='ck_payroll_exceptions_status'),
        sa.CheckConstraint(f"source IN {EXCEPTION_SOURCES}", name='ck_payroll_exceptions_source'),
        sa.CheckConstraint(f"deferred_to IS NULL OR deferred_to IN {DEFER_TARGETS}", name='ck_payroll_exceptions_defer'),
        sa.CheckConstraint("(status = 'DEFERRED') = (deferred_to IS NOT NULL)", name='ck_payroll_exceptions_deferred'),
        sa.CheckConstraint("status <> 'WAIVED' OR waivable", name='ck_payroll_exceptions_waivable'),
        sa.CheckConstraint("status NOT IN ('WAIVED', 'DEFERRED') OR (resolved_by_user_id IS NOT NULL "
                           "AND resolution_note IS NOT NULL)", name='ck_payroll_exceptions_resolved_by'),
        sa.CheckConstraint("(status = 'OPEN') = (resolved_at IS NULL)", name='ck_payroll_exceptions_resolved_at'),
        sa.CheckConstraint("jsonb_typeof(details) = 'object'", name='ck_payroll_exceptions_details'),
        sa.CheckConstraint("jsonb_typeof(resolutions) = 'array'", name='ck_payroll_exceptions_resolutions'),
        _version('payroll_exceptions'),
    )
    op.create_index('ix_payroll_exceptions_company_id', 'payroll_exceptions', ['company_id'])
    op.create_index('ix_payroll_exceptions_run_id', 'payroll_exceptions', ['run_id'])
    op.create_index('ix_payroll_exceptions_run_employee_id', 'payroll_exceptions', ['run_employee_id'])
    op.create_index('uq_payroll_exceptions_open', 'payroll_exceptions', ['company_id', 'run_id', 'dedupe_key'],
                    unique=True, postgresql_where=sa.text("status = 'OPEN'"))
    op.create_index('ix_payroll_exceptions_run_status', 'payroll_exceptions',
                    ['company_id', 'run_id', 'status', 'severity'])

    op.create_table(
        'payroll_approvals', *_base(),
        sa.Column('run_id', sa.Integer(), nullable=False),
        sa.Column('run_employee_id', sa.Integer(), nullable=True),
        sa.Column('approval_request_id', sa.Integer(), nullable=True),
        sa.Column('step_order', sa.Integer(), nullable=False),
        sa.Column('approver_user_id', sa.Integer(), nullable=False),
        sa.Column('acted_as', sa.String(10), nullable=False, server_default='approver'),
        sa.Column('on_behalf_of_user_id', sa.Integer(), nullable=True),
        sa.Column('decision', sa.String(10), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('acted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('cleared_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cleared_reason', sa.Text(), nullable=True),
        _uq('payroll_approvals'),
        _fk('payroll_approvals', 'run_id', 'payroll_runs', 'run', ondelete='CASCADE'),
        _fk('payroll_approvals', 'run_employee_id', 'payroll_run_employees', 'run_employee', ondelete='CASCADE'),
        _fk('payroll_approvals', 'approval_request_id', 'approval_requests', 'approval_request'),
        _fk('payroll_approvals', 'approver_user_id', 'users', 'approver'),
        _fk('payroll_approvals', 'on_behalf_of_user_id', 'users', 'on_behalf_of'),
        sa.CheckConstraint(f"decision IN {APPROVAL_DECISIONS}", name='ck_payroll_approvals_decision'),
        sa.CheckConstraint(f"acted_as IN {APPROVAL_ACTED_AS}", name='ck_payroll_approvals_acted_as'),
        sa.CheckConstraint("step_order >= 1", name='ck_payroll_approvals_step'),
        sa.CheckConstraint("(acted_as = 'delegate') = (on_behalf_of_user_id IS NOT NULL)",
                           name='ck_payroll_approvals_delegate'),
        sa.CheckConstraint("decision <> 'REJECTED' OR comment IS NOT NULL", name='ck_payroll_approvals_reject_reason'),
        sa.CheckConstraint("cleared_reason IS NULL OR cleared_at IS NOT NULL", name='ck_payroll_approvals_cleared'),
    )
    op.create_index('ix_payroll_approvals_company_id', 'payroll_approvals', ['company_id'])
    op.create_index('ix_payroll_approvals_run_id', 'payroll_approvals', ['run_id'])
    op.create_index('uq_payroll_approvals_live', 'payroll_approvals',
                    ['company_id', 'run_id', sa.text('COALESCE(run_employee_id, 0)'), 'step_order', 'approver_user_id'],
                    unique=True, postgresql_where=sa.text("cleared_at IS NULL"))

    op.create_table(
        'ytd_balances', *_base(),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('tax_year', sa.String(9), nullable=False),
        sa.Column('bucket', sa.String(60), nullable=False),
        sa.Column('component_id', sa.Integer(), nullable=True),
        sa.Column('amount', sa.Numeric(19, 4), nullable=False, server_default='0'),
        sa.Column('currency', sa.CHAR(3), nullable=False),
        sa.Column('last_run_id', sa.Integer(), nullable=True),
        _row_version(),
        _uq('ytd_balances'),
        sa.UniqueConstraint('company_id', 'employee_id', 'legal_entity_id', 'tax_year', 'bucket', 'currency',
                            name='uq_ytd_balances_key'),
        _fk('ytd_balances', 'employee_id', 'employees', 'employee'),
        _fk('ytd_balances', 'legal_entity_id', 'legal_entities', 'legal_entity'),
        _fk('ytd_balances', 'component_id', 'pay_components', 'component'),
        _fk('ytd_balances', 'last_run_id', 'payroll_runs', 'last_run'),
        _currency('ytd_balances'),
        sa.CheckConstraint("tax_year ~ '^[0-9]{4}(-[0-9]{2,4})?$'", name='ck_ytd_balances_tax_year'),
        _version('ytd_balances'),
    )
    op.create_index('ix_ytd_balances_company_id', 'ytd_balances', ['company_id'])
    op.create_index('ix_ytd_balances_employee_id', 'ytd_balances', ['employee_id'])

    op.create_table(
        'statutory_liabilities', *_base(),
        sa.Column('run_id', sa.Integer(), nullable=False),
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('scheme', sa.String(40), nullable=False),
        sa.Column('payee_authority', sa.String(150), nullable=False),
        sa.Column('employee_amount', sa.Numeric(19, 4), nullable=False, server_default='0'),
        sa.Column('employer_amount', sa.Numeric(19, 4), nullable=False, server_default='0'),
        sa.Column('currency', sa.CHAR(3), nullable=False),
        sa.Column('due_date', sa.Date(), nullable=True),
        sa.Column('status', sa.String(12), nullable=False, server_default='OPEN'),
        sa.Column('remittance_ref', sa.String(100), nullable=True),
        sa.Column('fixture', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('details', jsonb, nullable=False, server_default=empty_object),
        _row_version(),
        _uq('statutory_liabilities'),
        sa.UniqueConstraint('company_id', 'run_id', 'scheme', 'payee_authority', 'currency',
                            name='uq_statutory_liabilities_key'),
        _fk('statutory_liabilities', 'run_id', 'payroll_runs', 'run'),
        _fk('statutory_liabilities', 'legal_entity_id', 'legal_entities', 'legal_entity'),
        sa.CheckConstraint(f"status IN {LIABILITY_STATUSES}", name='ck_statutory_liabilities_status'),
        _currency('statutory_liabilities'),
        sa.CheckConstraint("status = 'OPEN' OR remittance_ref IS NOT NULL", name='ck_statutory_liabilities_remitted'),
        sa.CheckConstraint("jsonb_typeof(details) = 'object'", name='ck_statutory_liabilities_details'),
        _version('statutory_liabilities'),
    )
    op.create_index('ix_statutory_liabilities_company_id', 'statutory_liabilities', ['company_id'])
    op.create_index('ix_statutory_liabilities_run_id', 'statutory_liabilities', ['run_id'])

    op.create_table(
        'statutory_attestations', *_base(),
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('schemes', jsonb, nullable=False, server_default=empty_array),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('effective_to', sa.Date(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('evidence_ref', sa.String(300), nullable=True),
        sa.Column('status', sa.String(10), nullable=False, server_default='PENDING'),
        sa.Column('attested_by_user_id', sa.Integer(), nullable=False),
        sa.Column('approved_by_user_id', sa.Integer(), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_by_user_id', sa.Integer(), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoke_reason', sa.Text(), nullable=True),
        _row_version(),
        _uq('statutory_attestations'),
        _fk('statutory_attestations', 'legal_entity_id', 'legal_entities', 'legal_entity'),
        _fk('statutory_attestations', 'attested_by_user_id', 'users', 'attested_by'),
        _fk('statutory_attestations', 'approved_by_user_id', 'users', 'approved_by'),
        _fk('statutory_attestations', 'revoked_by_user_id', 'users', 'revoked_by'),
        sa.CheckConstraint(f"status IN {ATTESTATION_STATUSES}", name='ck_statutory_attestations_status'),
        sa.CheckConstraint("approved_by_user_id IS NULL OR approved_by_user_id <> attested_by_user_id",
                           name='ck_statutory_attestations_maker_checker'),
        sa.CheckConstraint("status <> 'ACTIVE' OR (approved_by_user_id IS NOT NULL AND approved_at IS NOT NULL)",
                           name='ck_statutory_attestations_active'),
        sa.CheckConstraint("(status = 'REVOKED') = (revoked_at IS NOT NULL)", name='ck_statutory_attestations_revoked'),
        sa.CheckConstraint("effective_to IS NULL OR effective_to >= effective_from",
                           name='ck_statutory_attestations_window'),
        sa.CheckConstraint("jsonb_typeof(schemes) = 'array'", name='ck_statutory_attestations_schemes'),
        _version('statutory_attestations'),
    )
    op.create_index('ix_statutory_attestations_company_id', 'statutory_attestations', ['company_id'])
    op.create_index('ix_statutory_attestations_legal_entity_id', 'statutory_attestations', ['legal_entity_id'])

    for statement in (PAY_PERIODS_GUARD, PAYROLL_RUNS_GUARD, RUN_EMPLOYEES_GUARD, SNAPSHOTS_IMMUTABLE, LINES_GUARD):
        op.execute(statement)
    op.execute("CREATE TRIGGER trg_pay_periods_guard BEFORE INSERT OR UPDATE OF pay_calendar_id, pay_group_id, "
               "period_start, period_end ON pay_periods FOR EACH ROW EXECUTE FUNCTION pay_periods_guard()")
    op.execute("CREATE TRIGGER trg_payroll_runs_guard BEFORE INSERT OR UPDATE ON payroll_runs "
               "FOR EACH ROW EXECUTE FUNCTION payroll_runs_guard()")
    op.execute("CREATE TRIGGER trg_payroll_run_employees_guard BEFORE INSERT OR UPDATE OF run_id, employee_id, "
               "employee_assignment_id, status ON payroll_run_employees "
               "FOR EACH ROW EXECUTE FUNCTION payroll_run_employees_guard()")
    op.execute("CREATE TRIGGER trg_payroll_input_snapshots_immutable BEFORE UPDATE ON payroll_input_snapshots "
               "FOR EACH ROW EXECUTE FUNCTION payroll_input_snapshots_immutable()")
    op.execute("CREATE TRIGGER trg_payroll_lines_guard BEFORE INSERT OR UPDATE OR DELETE ON payroll_lines "
               "FOR EACH ROW EXECUTE FUNCTION payroll_lines_guard()")
    _install_security(op.get_bind())


def _install_security(bind) -> None:
    for table in NEW_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")
    roles = [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"
    ))]
    for role in roles:
        quoted = '"' + role.replace('"', '""') + '"'
        for table in NEW_TABLES:
            privileges = "SELECT, INSERT" if table == "payroll_input_snapshots" else "SELECT, INSERT, UPDATE, DELETE"
            op.execute(f"GRANT {privileges} ON {table} TO {quoted}")
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}")


def downgrade() -> None:
    for table, trigger in (("payroll_lines", "trg_payroll_lines_guard"),
                           ("payroll_input_snapshots", "trg_payroll_input_snapshots_immutable"),
                           ("payroll_run_employees", "trg_payroll_run_employees_guard"),
                           ("payroll_runs", "trg_payroll_runs_guard"),
                           ("pay_periods", "trg_pay_periods_guard")):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    for function in FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")
    op.drop_constraint('fk_payroll_input_snapshots_run_employee', 'payroll_input_snapshots', type_='foreignkey')
    for table in ("statutory_attestations", "statutory_liabilities", "ytd_balances", "payroll_approvals",
                  "payroll_exceptions", "payroll_line_sources", "payroll_lines", "payroll_run_employees",
                  "payroll_input_snapshots", "payroll_runs", "pay_periods", "pay_calendars"):
        op.drop_table(table)

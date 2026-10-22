"""Corrections: arrears proposals, retro deltas, reversal links, final settlement cases.

Additive (3.5 §12, §13, §15; 3.3 §8.3; 3.4 §16): arrears_proposals, arrears_proposal_lines,
arrears_instalments, payroll_reversals, final_settlement_cases, final_settlement_items and
final_settlement_deadlines. Every table is a tenant table with row-level security, grants and composite
same-company foreign keys (app.models.corrections documents each table).

Database rules services cannot be trusted to keep alone:

* one pending arrears proposal per finalized run employee (uq_arrears_proposals_pending);
* a proposal line's direction matches the sign of its delta; an instalment is settled once, by one run;
* a reversal is approved by two different users, neither the requester (CHECKs), and one pending
  reversal request per run;
* one live final-settlement case per employment period (EC-COR-09); a DEATH case names its payee (EC-COR-07);
  a hold always carries its reason (an ABSCONDING case opens held, EC-COR-12); an asset deduction
  needs recorded consent (EC-COR-14).

Downgrade drops the tables.

Revision ID: 0025_corrections
Revises: 0024_reports_compliance
Create Date: 2026-10-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0025_corrections'
down_revision: Union[str, None] = '0024_reports_compliance'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies: later model changes must not alter what this revision does.
TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
NEW_TABLES = ("arrears_proposals", "arrears_proposal_lines", "arrears_instalments", "payroll_reversals",
              "final_settlement_cases", "final_settlement_items", "final_settlement_deadlines")
ARREARS_TRIGGERS = (
    "COMPENSATION", "ATTENDANCE", "LEAVE", "OVERTIME", "TASK", "SETTING", "POLICY", "INPUT", "REVERSAL", "MANUAL",
)
ARREARS_STATUSES = ("PENDING_APPROVAL", "APPROVED", "SETTLED", "REJECTED", "CANCELLED", "SUPERSEDED")
ARREARS_DIRECTIONS = ("ARREARS", "RECOVERY")
ARREARS_PAYMENT_MODES = ("NEXT_REGULAR_RUN", "OFF_CYCLE")
ARREARS_RECOVERY_MODES = ("NEXT_RUN", "INSTALMENTS", "MANUAL")
INSTALMENT_STATUSES = ("PLANNED", "SETTLED", "CANCELLED")
REVERSAL_STATUSES = ("PENDING_APPROVAL", "APPROVED", "FINALIZED", "REJECTED", "CANCELLED")
TERMINATION_TYPES = (
    "RESIGNATION", "TERMINATION_WITH_CAUSE", "TERMINATION_WITHOUT_CAUSE", "REDUNDANCY", "RETIREMENT",
    "CONTRACT_END", "DEATH", "ABSCONDING",
)
SETTLEMENT_STATUSES = ("OPEN", "READY", "IN_RUN", "SETTLED", "CANCELLED")
DUE_BASES = ("PACK_VERIFIED", "COMPANY_DEADLINE", "NOT_CONFIGURED")
SETTLEMENT_ITEM_TYPES = (
    "SALARY_TO_LAST_DAY", "NOTICE_PAY", "LEAVE_ENCASHMENT", "NEGATIVE_LEAVE_BALANCE", "GRATUITY",
    "STATUTORY_BONUS", "PENDING_ARREARS", "PENDING_CLAIMS", "PENDING_INPUTS", "LOAN_RECOVERY", "ASSET_RECOVERY",
    "TAX",
)
SETTLEMENT_ITEM_STATUSES = ("PENDING", "CONFIRMED", "WAIVED", "NOT_APPLICABLE", "BLOCKED")
DEADLINE_BASES = ("CALENDAR_DAYS", "WORKING_DAYS")


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


def _jsonb(name, default, nullable=False):
    return sa.Column(name, postgresql.JSONB(), nullable=nullable,
                     server_default=None if default is None else sa.text(f"'{default}'::jsonb"))


def upgrade() -> None:
    bind = op.get_bind()
    op.create_table(
        'arrears_proposals', *_base(),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('pay_group_id', sa.Integer(), nullable=False),
        sa.Column('original_run_id', sa.Integer(), nullable=False),
        sa.Column('original_run_employee_id', sa.Integer(), nullable=False),
        sa.Column('original_period_id', sa.Integer(), nullable=False),
        sa.Column('period_start', sa.Date(), nullable=False),
        sa.Column('period_end', sa.Date(), nullable=False),
        sa.Column('currency', sa.CHAR(3), nullable=False),
        sa.Column('trigger', sa.String(20), nullable=False),
        _jsonb('trigger_ref', '{}'),
        _jsonb('changed_sources', '[]'),
        sa.Column('base_proposal_id', sa.Integer(), nullable=True),
        sa.Column('superseded_by_id', sa.Integer(), nullable=True),
        _jsonb('snapshot', None, nullable=True),
        sa.Column('snapshot_hash', sa.CHAR(64), nullable=True),
        sa.Column('calc_hash', sa.CHAR(64), nullable=True),
        sa.Column('engine_version', sa.String(20), nullable=True),
        _jsonb('totals', '{}'),
        _jsonb('issues', '[]'),
        sa.Column('status', sa.String(20), nullable=False, server_default='PENDING_APPROVAL'),
        sa.Column('payment_mode', sa.String(20), nullable=True),
        sa.Column('recovery_mode', sa.String(20), nullable=True),
        sa.Column('retro_run_id', sa.Integer(), nullable=True),
        sa.Column('approval_request_id', sa.Integer(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('decided_by_user_id', sa.Integer(), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('decision_note', sa.Text(), nullable=True),
        sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True),
        _row_version(),
        _uq('arrears_proposals'),
        _fk('arrears_proposals', 'employee_id', 'employees', 'employee'),
        _fk('arrears_proposals', 'legal_entity_id', 'legal_entities', 'legal_entity'),
        _fk('arrears_proposals', 'pay_group_id', 'pay_groups', 'pay_group'),
        _fk('arrears_proposals', 'original_run_id', 'payroll_runs', 'original_run'),
        _fk('arrears_proposals', 'original_run_employee_id', 'payroll_run_employees', 'original_run_employee'),
        _fk('arrears_proposals', 'original_period_id', 'pay_periods', 'original_period'),
        _fk('arrears_proposals', 'base_proposal_id', 'arrears_proposals', 'base_proposal'),
        _fk('arrears_proposals', 'superseded_by_id', 'arrears_proposals', 'superseded_by'),
        _fk('arrears_proposals', 'retro_run_id', 'payroll_runs', 'retro_run'),
        _fk('arrears_proposals', 'approval_request_id', 'approval_requests', 'approval_request'),
        _fk('arrears_proposals', 'created_by_user_id', 'users', 'created_by'),
        _fk('arrears_proposals', 'decided_by_user_id', 'users', 'decided_by'),
        _currency('arrears_proposals'),
        sa.CheckConstraint(f"trigger IN {ARREARS_TRIGGERS}", name='ck_arrears_proposals_trigger'),
        sa.CheckConstraint(f"status IN {ARREARS_STATUSES}", name='ck_arrears_proposals_status'),
        sa.CheckConstraint(f"payment_mode IS NULL OR payment_mode IN {ARREARS_PAYMENT_MODES}",
                           name='ck_arrears_proposals_payment_mode'),
        sa.CheckConstraint(f"recovery_mode IS NULL OR recovery_mode IN {ARREARS_RECOVERY_MODES}",
                           name='ck_arrears_proposals_recovery_mode'),
        sa.CheckConstraint("period_end >= period_start", name='ck_arrears_proposals_period'),
        sa.CheckConstraint("jsonb_typeof(trigger_ref) = 'object'", name='ck_arrears_proposals_trigger_ref'),
        sa.CheckConstraint("jsonb_typeof(changed_sources) = 'array'", name='ck_arrears_proposals_changed'),
        sa.CheckConstraint("jsonb_typeof(totals) = 'object'", name='ck_arrears_proposals_totals'),
        sa.CheckConstraint("jsonb_typeof(issues) = 'array'", name='ck_arrears_proposals_issues'),
        sa.CheckConstraint("snapshot IS NULL OR jsonb_typeof(snapshot) = 'object'",
                           name='ck_arrears_proposals_snapshot'),
        sa.CheckConstraint("(snapshot IS NULL) = (snapshot_hash IS NULL)", name='ck_arrears_proposals_snapshot_hash'),
        sa.CheckConstraint("snapshot_hash IS NULL OR snapshot_hash ~ '^[0-9a-f]{64}$'",
                           name='ck_arrears_proposals_snapshot_hash_hex'),
        sa.CheckConstraint("calc_hash IS NULL OR calc_hash ~ '^[0-9a-f]{64}$'", name='ck_arrears_proposals_calc_hash'),
        sa.CheckConstraint("status NOT IN ('APPROVED', 'SETTLED') OR payment_mode IS NOT NULL",
                           name='ck_arrears_proposals_approved_mode'),
        sa.CheckConstraint("status <> 'REJECTED' OR decision_note IS NOT NULL",
                           name='ck_arrears_proposals_reject_note'),
        sa.CheckConstraint("(status = 'SUPERSEDED') = (superseded_by_id IS NOT NULL)",
                           name='ck_arrears_proposals_superseded'),
        sa.CheckConstraint("(status = 'SETTLED') = (settled_at IS NOT NULL)", name='ck_arrears_proposals_settled'),
        sa.CheckConstraint("base_proposal_id IS NULL OR base_proposal_id <> id", name='ck_arrears_proposals_base'),
        _version('arrears_proposals'),
    )
    op.create_index('ix_arrears_proposals_company_id', 'arrears_proposals', ['company_id'])
    op.create_index('ix_arrears_proposals_employee_id', 'arrears_proposals', ['employee_id'])
    op.create_index('ix_arrears_proposals_original_run_id', 'arrears_proposals', ['original_run_id'])
    op.create_index('ix_arrears_proposals_original_run_employee_id', 'arrears_proposals',
                    ['original_run_employee_id'])
    op.create_index('uq_arrears_proposals_pending', 'arrears_proposals', ['company_id', 'original_run_employee_id'],
                    unique=True, postgresql_where=sa.text("status = 'PENDING_APPROVAL'"))
    op.create_index('ix_arrears_proposals_employee_status', 'arrears_proposals',
                    ['company_id', 'employee_id', 'status'])

    op.create_table(
        'arrears_proposal_lines', *_base(),
        sa.Column('proposal_id', sa.Integer(), nullable=False),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('component_code', sa.String(40), nullable=False),
        sa.Column('component_id', sa.Integer(), nullable=True),
        sa.Column('kind', sa.String(25), nullable=False),
        sa.Column('category', sa.String(40), nullable=True),
        _jsonb('tags', '[]'),
        sa.Column('original_amount', sa.Numeric(19, 4), nullable=False),
        sa.Column('recalculated_amount', sa.Numeric(19, 4), nullable=False),
        sa.Column('delta', sa.Numeric(19, 4), nullable=False),
        sa.Column('currency', sa.CHAR(3), nullable=False),
        sa.Column('direction', sa.String(10), nullable=False),
        _jsonb('period_ref', '{}'),
        sa.Column('explanation', sa.Text(), nullable=True),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        _uq('arrears_proposal_lines'),
        sa.UniqueConstraint('proposal_id', 'component_code', name='uq_arrears_proposal_lines_component'),
        _fk('arrears_proposal_lines', 'proposal_id', 'arrears_proposals', 'proposal', ondelete='CASCADE'),
        _fk('arrears_proposal_lines', 'employee_id', 'employees', 'employee'),
        _fk('arrears_proposal_lines', 'component_id', 'pay_components', 'component'),
        _currency('arrears_proposal_lines'),
        sa.CheckConstraint(f"direction IN {ARREARS_DIRECTIONS}", name='ck_arrears_proposal_lines_direction'),
        sa.CheckConstraint("delta <> 0", name='ck_arrears_proposal_lines_delta'),
        sa.CheckConstraint("(direction = 'ARREARS') = (delta > 0)", name='ck_arrears_proposal_lines_sign'),
        sa.CheckConstraint("jsonb_typeof(tags) = 'array'", name='ck_arrears_proposal_lines_tags'),
        sa.CheckConstraint("jsonb_typeof(period_ref) = 'object'", name='ck_arrears_proposal_lines_period_ref'),
    )
    op.create_index('ix_arrears_proposal_lines_company_id', 'arrears_proposal_lines', ['company_id'])
    op.create_index('ix_arrears_proposal_lines_proposal_id', 'arrears_proposal_lines', ['proposal_id'])

    op.create_table(
        'arrears_instalments', *_base(),
        sa.Column('proposal_line_id', sa.Integer(), nullable=False),
        sa.Column('proposal_id', sa.Integer(), nullable=False),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Numeric(19, 4), nullable=False),
        sa.Column('currency', sa.CHAR(3), nullable=False),
        sa.Column('status', sa.String(10), nullable=False, server_default='PLANNED'),
        sa.Column('settled_run_id', sa.Integer(), nullable=True),
        sa.Column('settled_amount', sa.Numeric(19, 4), nullable=True),
        sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True),
        _row_version(),
        _uq('arrears_instalments'),
        sa.UniqueConstraint('proposal_line_id', 'number', name='uq_arrears_instalments_number'),
        _fk('arrears_instalments', 'proposal_line_id', 'arrears_proposal_lines', 'proposal_line', ondelete='CASCADE'),
        _fk('arrears_instalments', 'proposal_id', 'arrears_proposals', 'proposal', ondelete='CASCADE'),
        _fk('arrears_instalments', 'employee_id', 'employees', 'employee'),
        _fk('arrears_instalments', 'settled_run_id', 'payroll_runs', 'settled_run'),
        _currency('arrears_instalments'),
        sa.CheckConstraint(f"status IN {INSTALMENT_STATUSES}", name='ck_arrears_instalments_status'),
        sa.CheckConstraint("number >= 1", name='ck_arrears_instalments_number_positive'),
        sa.CheckConstraint("amount <> 0", name='ck_arrears_instalments_amount'),
        sa.CheckConstraint("(status = 'SETTLED') = (settled_at IS NOT NULL AND settled_run_id IS NOT NULL "
                           "AND settled_amount IS NOT NULL)", name='ck_arrears_instalments_settled'),
        _version('arrears_instalments'),
    )
    op.create_index('ix_arrears_instalments_company_id', 'arrears_instalments', ['company_id'])
    op.create_index('ix_arrears_instalments_proposal_line_id', 'arrears_instalments', ['proposal_line_id'])
    op.create_index('ix_arrears_instalments_proposal_id', 'arrears_instalments', ['proposal_id'])
    op.create_index('ix_arrears_instalments_employee_id', 'arrears_instalments', ['employee_id'])

    op.create_table(
        'payroll_reversals', *_base(),
        sa.Column('original_run_id', sa.Integer(), nullable=False),
        sa.Column('reversal_run_id', sa.Integer(), nullable=True),
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('pay_group_id', sa.Integer(), nullable=False),
        _jsonb('run_employee_ids', '[]'),
        _jsonb('employee_ids', '[]'),
        sa.Column('selected', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='PENDING_APPROVAL'),
        _jsonb('preview', '{}'),
        _jsonb('effects', '{}'),
        sa.Column('requested_by_user_id', sa.Integer(), nullable=False),
        sa.Column('first_approver_user_id', sa.Integer(), nullable=True),
        sa.Column('first_approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('first_comment', sa.Text(), nullable=True),
        sa.Column('second_approver_user_id', sa.Integer(), nullable=True),
        sa.Column('second_approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('second_comment', sa.Text(), nullable=True),
        sa.Column('decided_by_user_id', sa.Integer(), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('decision_note', sa.Text(), nullable=True),
        sa.Column('finalized_at', sa.DateTime(timezone=True), nullable=True),
        _row_version(),
        _uq('payroll_reversals'),
        sa.UniqueConstraint('company_id', 'reversal_run_id', name='uq_payroll_reversals_reversal_run'),
        _fk('payroll_reversals', 'original_run_id', 'payroll_runs', 'original_run'),
        _fk('payroll_reversals', 'reversal_run_id', 'payroll_runs', 'reversal_run'),
        _fk('payroll_reversals', 'legal_entity_id', 'legal_entities', 'legal_entity'),
        _fk('payroll_reversals', 'pay_group_id', 'pay_groups', 'pay_group'),
        _fk('payroll_reversals', 'requested_by_user_id', 'users', 'requested_by'),
        _fk('payroll_reversals', 'first_approver_user_id', 'users', 'first_approver'),
        _fk('payroll_reversals', 'second_approver_user_id', 'users', 'second_approver'),
        _fk('payroll_reversals', 'decided_by_user_id', 'users', 'decided_by'),
        sa.CheckConstraint(f"status IN {REVERSAL_STATUSES}", name='ck_payroll_reversals_status'),
        sa.CheckConstraint("length(btrim(reason)) > 0", name='ck_payroll_reversals_reason'),
        sa.CheckConstraint("jsonb_typeof(run_employee_ids) = 'array' AND jsonb_array_length(run_employee_ids) > 0",
                           name='ck_payroll_reversals_run_employees'),
        sa.CheckConstraint("jsonb_typeof(employee_ids) = 'array'", name='ck_payroll_reversals_employees'),
        sa.CheckConstraint("jsonb_typeof(preview) = 'object'", name='ck_payroll_reversals_preview'),
        sa.CheckConstraint("jsonb_typeof(effects) = 'object'", name='ck_payroll_reversals_effects'),
        sa.CheckConstraint("first_approver_user_id IS NULL OR first_approver_user_id <> requested_by_user_id",
                           name='ck_payroll_reversals_first_not_requester'),
        sa.CheckConstraint("second_approver_user_id IS NULL OR (second_approver_user_id <> requested_by_user_id "
                           "AND second_approver_user_id <> first_approver_user_id)",
                           name='ck_payroll_reversals_second_distinct'),
        sa.CheckConstraint("(first_approver_user_id IS NULL) = (first_approved_at IS NULL)",
                           name='ck_payroll_reversals_first_at'),
        sa.CheckConstraint("(second_approver_user_id IS NULL) = (second_approved_at IS NULL)",
                           name='ck_payroll_reversals_second_at'),
        sa.CheckConstraint("second_approver_user_id IS NULL OR first_approver_user_id IS NOT NULL",
                           name='ck_payroll_reversals_order'),
        sa.CheckConstraint("status NOT IN ('APPROVED', 'FINALIZED') OR (second_approver_user_id IS NOT NULL "
                           "AND reversal_run_id IS NOT NULL)", name='ck_payroll_reversals_approved'),
        sa.CheckConstraint("(status = 'FINALIZED') = (finalized_at IS NOT NULL)",
                           name='ck_payroll_reversals_finalized'),
        sa.CheckConstraint("status <> 'REJECTED' OR decision_note IS NOT NULL",
                           name='ck_payroll_reversals_reject_note'),
        _version('payroll_reversals'),
    )
    op.create_index('ix_payroll_reversals_company_id', 'payroll_reversals', ['company_id'])
    op.create_index('ix_payroll_reversals_original_run_id', 'payroll_reversals', ['original_run_id'])
    op.create_index('uq_payroll_reversals_pending', 'payroll_reversals', ['company_id', 'original_run_id'],
                    unique=True, postgresql_where=sa.text("status = 'PENDING_APPROVAL'"))

    op.create_table(
        'final_settlement_cases', *_base(),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('employee_assignment_id', sa.Integer(), nullable=False),
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('pay_group_id', sa.Integer(), nullable=False),
        sa.Column('pay_period_id', sa.Integer(), nullable=True),
        sa.Column('employment_start', sa.Date(), nullable=False),
        sa.Column('last_working_day', sa.Date(), nullable=False),
        sa.Column('notice_given_on', sa.Date(), nullable=True),
        sa.Column('termination_type', sa.String(30), nullable=False),
        sa.Column('currency', sa.CHAR(3), nullable=False),
        sa.Column('status', sa.String(10), nullable=False, server_default='OPEN'),
        sa.Column('held', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('hold_reason', sa.String(500), nullable=True),
        _jsonb('payee', None, nullable=True),
        sa.Column('due_date', sa.Date(), nullable=True),
        sa.Column('due_basis', sa.String(20), nullable=False, server_default='NOT_CONFIGURED'),
        sa.Column('due_note', sa.Text(), nullable=True),
        sa.Column('run_id', sa.Integer(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cancel_reason', sa.Text(), nullable=True),
        _row_version(),
        _uq('final_settlement_cases'),
        _fk('final_settlement_cases', 'employee_id', 'employees', 'employee'),
        sa.ForeignKeyConstraint(['company_id', 'employee_assignment_id'],
                                ['employee_assignments.company_id', 'employee_assignments.id'],
                                name='fk_final_settlement_cases_assignment'),
        _fk('final_settlement_cases', 'legal_entity_id', 'legal_entities', 'legal_entity'),
        _fk('final_settlement_cases', 'pay_group_id', 'pay_groups', 'pay_group'),
        _fk('final_settlement_cases', 'pay_period_id', 'pay_periods', 'pay_period'),
        _fk('final_settlement_cases', 'run_id', 'payroll_runs', 'run'),
        _fk('final_settlement_cases', 'created_by_user_id', 'users', 'created_by'),
        _currency('final_settlement_cases'),
        sa.CheckConstraint(f"termination_type IN {TERMINATION_TYPES}", name='ck_final_settlement_cases_type'),
        sa.CheckConstraint(f"status IN {SETTLEMENT_STATUSES}", name='ck_final_settlement_cases_status'),
        sa.CheckConstraint(f"due_basis IN {DUE_BASES}", name='ck_final_settlement_cases_due_basis'),
        sa.CheckConstraint("last_working_day >= employment_start", name='ck_final_settlement_cases_window'),
        sa.CheckConstraint("notice_given_on IS NULL OR notice_given_on <= last_working_day",
                           name='ck_final_settlement_cases_notice'),
        sa.CheckConstraint("payee IS NULL OR jsonb_typeof(payee) = 'object'", name='ck_final_settlement_cases_payee'),
        sa.CheckConstraint("termination_type <> 'DEATH' OR payee IS NOT NULL",
                           name='ck_final_settlement_cases_death_payee'),
        sa.CheckConstraint("held = (hold_reason IS NOT NULL)", name='ck_final_settlement_cases_hold'),
        sa.CheckConstraint("(due_basis = 'NOT_CONFIGURED') = (due_date IS NULL)", name='ck_final_settlement_cases_due'),
        sa.CheckConstraint("status NOT IN ('IN_RUN', 'SETTLED') OR run_id IS NOT NULL",
                           name='ck_final_settlement_cases_run'),
        sa.CheckConstraint("(status = 'SETTLED') = (settled_at IS NOT NULL)", name='ck_final_settlement_cases_settled'),
        sa.CheckConstraint("(status = 'CANCELLED') = (cancelled_at IS NOT NULL)",
                           name='ck_final_settlement_cases_cancelled'),
        _version('final_settlement_cases'),
    )
    op.create_index('ix_final_settlement_cases_company_id', 'final_settlement_cases', ['company_id'])
    op.create_index('ix_final_settlement_cases_employee_id', 'final_settlement_cases', ['employee_id'])
    op.create_index('uq_final_settlement_cases_employment', 'final_settlement_cases',
                    ['company_id', 'employee_id', 'employment_start'], unique=True,
                    postgresql_where=sa.text("status <> 'CANCELLED'"))
    op.create_index('ix_final_settlement_cases_status_due', 'final_settlement_cases',
                    ['company_id', 'status', 'due_date'])

    op.create_table(
        'final_settlement_items', *_base(),
        sa.Column('case_id', sa.Integer(), nullable=False),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('item_type', sa.String(30), nullable=False),
        sa.Column('source_key', sa.String(80), nullable=False, server_default=''),
        sa.Column('status', sa.String(15), nullable=False, server_default='PENDING'),
        sa.Column('component_code', sa.String(40), nullable=True),
        sa.Column('amount', sa.Numeric(19, 4), nullable=True),
        sa.Column('currency', sa.CHAR(3), nullable=False),
        sa.Column('units', sa.Numeric(10, 4), nullable=True),
        sa.Column('rate', sa.Numeric(24, 10), nullable=True),
        _jsonb('details', '{}'),
        _jsonb('issues', '[]'),
        sa.Column('requires_consent', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('consent_ref', sa.String(300), nullable=True),
        sa.Column('consent_recorded_by_user_id', sa.Integer(), nullable=True),
        sa.Column('consent_recorded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('wage_input_id', sa.Integer(), nullable=True),
        sa.Column('decided_by_user_id', sa.Integer(), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        _row_version(),
        _uq('final_settlement_items'),
        sa.UniqueConstraint('case_id', 'item_type', 'source_key', name='uq_final_settlement_items_key'),
        _fk('final_settlement_items', 'case_id', 'final_settlement_cases', 'case', ondelete='CASCADE'),
        _fk('final_settlement_items', 'employee_id', 'employees', 'employee'),
        _fk('final_settlement_items', 'consent_recorded_by_user_id', 'users', 'consent_recorded_by'),
        _fk('final_settlement_items', 'wage_input_id', 'wage_inputs', 'wage_input'),
        _fk('final_settlement_items', 'decided_by_user_id', 'users', 'decided_by'),
        _currency('final_settlement_items'),
        sa.CheckConstraint(f"item_type IN {SETTLEMENT_ITEM_TYPES}", name='ck_final_settlement_items_type'),
        sa.CheckConstraint(f"status IN {SETTLEMENT_ITEM_STATUSES}", name='ck_final_settlement_items_status'),
        sa.CheckConstraint("jsonb_typeof(details) = 'object'", name='ck_final_settlement_items_details'),
        sa.CheckConstraint("jsonb_typeof(issues) = 'array'", name='ck_final_settlement_items_issues'),
        sa.CheckConstraint("status <> 'CONFIRMED' OR (amount IS NOT NULL OR item_type IN ('SALARY_TO_LAST_DAY', "
                           "'TAX', 'PENDING_ARREARS', 'PENDING_CLAIMS', 'PENDING_INPUTS', 'LOAN_RECOVERY'))",
                           name='ck_final_settlement_items_confirmed_amount'),
        sa.CheckConstraint("NOT (status = 'CONFIRMED' AND requires_consent) OR (consent_ref IS NOT NULL "
                           "AND consent_recorded_by_user_id IS NOT NULL)", name='ck_final_settlement_items_consent'),
        sa.CheckConstraint("status NOT IN ('CONFIRMED', 'WAIVED') OR decided_by_user_id IS NOT NULL",
                           name='ck_final_settlement_items_decided'),
        sa.CheckConstraint("status <> 'WAIVED' OR note IS NOT NULL", name='ck_final_settlement_items_waive_note'),
        _version('final_settlement_items'),
    )
    op.create_index('ix_final_settlement_items_company_id', 'final_settlement_items', ['company_id'])
    op.create_index('ix_final_settlement_items_case_id', 'final_settlement_items', ['case_id'])

    op.create_table(
        'final_settlement_deadlines', *_base(),
        sa.Column('legal_entity_id', sa.Integer(), nullable=True),
        sa.Column('days', sa.Integer(), nullable=False),
        sa.Column('day_basis', sa.String(15), nullable=False, server_default='CALENDAR_DAYS'),
        sa.Column('note', sa.String(500), nullable=True),
        sa.Column('updated_by_user_id', sa.Integer(), nullable=True),
        _row_version(),
        _uq('final_settlement_deadlines'),
        _fk('final_settlement_deadlines', 'legal_entity_id', 'legal_entities', 'legal_entity'),
        _fk('final_settlement_deadlines', 'updated_by_user_id', 'users', 'updated_by'),
        sa.CheckConstraint(f"day_basis IN {DEADLINE_BASES}", name='ck_final_settlement_deadlines_basis'),
        sa.CheckConstraint("days BETWEEN 0 AND 365", name='ck_final_settlement_deadlines_days'),
        _version('final_settlement_deadlines'),
    )
    op.create_index('ix_final_settlement_deadlines_company_id', 'final_settlement_deadlines', ['company_id'])
    op.execute("CREATE UNIQUE INDEX uq_final_settlement_deadlines_scope ON final_settlement_deadlines "
               "(company_id, COALESCE(legal_entity_id, 0))")

    _install_security(bind)


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
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {quoted}")
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}")


def downgrade() -> None:
    for table in ("final_settlement_deadlines", "final_settlement_items", "final_settlement_cases",
                  "payroll_reversals", "arrears_instalments", "arrears_proposal_lines", "arrears_proposals"):
        op.drop_table(table)

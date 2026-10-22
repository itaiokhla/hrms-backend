"""Compensation v2: pay components and versions, salary structures (templates) and versions/lines, compensation assignments and lines, revision cycles; legacy salary_structure migration.

Schema (3.3, 3.9 §3.4), every table a tenant table with row-level security, composite same-company
foreign keys and grants to the application role (as 0005/0008):

  * pay_components (code unique per tenant) + pay_component_versions (ACTIVE versions of one
    component never overlap; trigger);
  * salary_templates + salary_template_versions (DRAFT/ACTIVE/ARCHIVED, one ACTIVE per template) +
    salary_template_lines; lines and the content of a published version are frozen (trigger);
  * compensation_assignments + compensation_assignment_lines: ACTIVE/SCHEDULED rows of one employee
    placement never overlap (trigger instead of EXCLUDE: no btree_gist, EC-CMP-10); the placement must
    belong to the employee;
  * revision_cycles + revision_cycle_items; compensation_imports (validated CSV rows awaiting the
    commit job);
  * a component's code and kind cannot change once a structure or an assignment uses it (EC-CMP-06).

Legacy migration for existing tenants (3.3 §10, EC-MIG-01; embedded literals, idempotent):

  * components BASIC, HRA, DA, CONVEYANCE, OTHER_ALLOWANCE (FIXED, BY_PAID_DAYS, tag LOP_APPLICABLE)
    and the structure LEGACY_FIXED "Legacy fixed structure" (GROSS_BASED, v1 ACTIVE, five overridable
    FIXED lines) in every company;
  * one MIGRATED compensation assignment per salary_structure row: same dates, pay basis = the monthly
    gross (sum of the five columns), lines = the five columns, approved by the system, ACTIVE (SCHEDULED
    when it starts after the company's today), mapped by legacy_salary_structure_id. Legacy rows carry
    no currency (audit 07): the assumption recorded here is the base currency of the legal entity of the
    employee's primary placement on the row's start date (the earliest placement when none covers it).
    Overlapping legacy windows (the legacy update path never checked them) are resolved as the legacy
    engine reads them, the later start wins: an earlier row is closed the day before, a row with the
    same start is SUPERSEDED. Rows of employees without any placement are skipped and logged.
  * salary_structure, payroll and employees.base_salary are not modified.

Downgrade drops the new tables and functions; the legacy tables were never touched.

Revision ID: 0015_compensation_v2
Revises: 0014_approvals_engine
Create Date: 2026-10-21
"""
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0015_compensation_v2'
down_revision: Union[str, None] = '0014_approvals_engine'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
TENANT_TABLES = (
    "pay_components", "pay_component_versions", "salary_templates", "salary_template_versions",
    "salary_template_lines", "revision_cycles", "compensation_assignments", "compensation_assignment_lines",
    "revision_cycle_items", "compensation_imports",
)

# Frozen copies: later model changes must not alter what this revision does.
KINDS = ("EARNING", "DEDUCTION", "EMPLOYER_CONTRIBUTION", "REIMBURSEMENT", "BENEFIT_IN_KIND", "INFORMATIONAL",
         "ROUNDING")
CATEGORIES = (
    "ADVANCE_RECOVERY", "BASE", "BENEFIT", "BONUS", "COMMISSION", "EMPLOYEE_CONTRIBUTION", "EMPLOYER_CONTRIBUTION",
    "FIXED_ALLOWANCE", "GARNISHMENT_IF_LEGAL", "GRATUITY", "HOLIDAY_PREMIUM", "INFO", "LEAVE_ENCASHMENT",
    "LOAN_REPAYMENT", "OVERTIME", "PENALTY_IF_LEGAL", "RECOVERY", "REIMBURSEMENT", "RETRO_ADJUSTMENT", "ROUNDING",
    "SEVERANCE", "SHIFT_PREMIUM", "TASK_EARNING", "TAX", "VARIABLE", "VOLUNTARY",
)
METHODS = ("FIXED", "PERCENT_OF", "FORMULA", "RATE_X_UNITS", "SLAB", "INPUT", "BALANCING", "SYSTEM")
PRORATIONS = ("NONE", "BY_PAID_DAYS", "BY_PAID_HOURS", "BY_EMPLOYMENT_DAYS")
PRORATION_BASES = ("CALENDAR_DAYS", "WORKING_DAYS", "FIXED_30", "FIXED_26", "CUSTOM_DAYS", "HOURS")
VISIBILITIES = ("EMPLOYEE_VISIBLE", "HR_ONLY")
ROUNDING_MODES = ("HALF_UP", "HALF_EVEN", "UP", "DOWN", "CEILING", "FLOOR")
STRUCTURE_TYPES = ("CTC_BASED", "GROSS_BASED", "NET_BASED", "HOURLY", "DAILY", "TASK_BASED")
REASONS = ("HIRE", "REVISION", "PROMOTION", "TRANSFER", "CORRECTION", "MIGRATED")
STATUSES = ("DRAFT", "PENDING_APPROVAL", "SCHEDULED", "ACTIVE", "SUPERSEDED", "REJECTED")
CYCLE_STATUSES = ("DRAFT", "PREVIEWED", "PENDING_APPROVAL", "APPROVED", "APPLIED", "REJECTED", "CANCELLED")
CYCLE_ITEM_STATUSES = ("PROPOSED", "EXCLUDED", "APPLIED", "FAILED")
IMPORT_STATUSES = ("VALIDATED", "QUEUED", "COMMITTED", "FAILED")

LEGACY_COMPONENTS = (
    # code, name, category, salary_structure column, display order
    ("BASIC", "Basic", "BASE", "basic_salary", 10),
    ("HRA", "House rent allowance", "FIXED_ALLOWANCE", "house_rent_allowance", 20),
    ("DA", "Dearness allowance", "FIXED_ALLOWANCE", "dearness_allowance", 30),
    ("CONVEYANCE", "Conveyance allowance", "FIXED_ALLOWANCE", "conveyance_allowance", 40),
    ("OTHER_ALLOWANCE", "Other allowances", "FIXED_ALLOWANCE", "other_allowances", 50),
)
LEGACY_TEMPLATE_CODE = "LEGACY_FIXED"
LEGACY_TEMPLATE_NAME = "Legacy fixed structure"
EARNING_TOTALS = '{"in_gross": true, "in_net": true, "in_ctc": true, "in_employer_cost": true}'
MIGRATION_NOTE = ("Migrated from the legacy salary structure (migration 0015). Currency assumed: the legal entity "
                  "base currency of the placement on the start date (legacy rows carry no currency).")

COMPONENT_VERSION_GUARD = """
CREATE OR REPLACE FUNCTION pay_component_versions_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.status = 'ACTIVE' THEN
        PERFORM 1 FROM pay_components WHERE id = NEW.component_id AND company_id = NEW.company_id FOR UPDATE;
        IF EXISTS (
            SELECT 1 FROM pay_component_versions v
            WHERE v.company_id = NEW.company_id AND v.component_id = NEW.component_id AND v.status = 'ACTIVE'
              AND v.id <> NEW.id
              AND daterange(v.effective_from, v.effective_to, '[]') && daterange(NEW.effective_from, NEW.effective_to, '[]')
        ) THEN
            RAISE EXCEPTION 'Active versions of one pay component cannot overlap' USING ERRCODE = 'exclusion_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

COMPONENT_CODE_GUARD = """
CREATE OR REPLACE FUNCTION pay_components_code_guard() RETURNS trigger AS $$
BEGIN
    IF (NEW.code IS DISTINCT FROM OLD.code OR NEW.kind IS DISTINCT FROM OLD.kind) AND (
        EXISTS (SELECT 1 FROM salary_template_lines l WHERE l.company_id = OLD.company_id AND l.component_id = OLD.id)
        OR EXISTS (SELECT 1 FROM compensation_assignment_lines l
                   WHERE l.company_id = OLD.company_id AND l.component_id = OLD.id)
    ) THEN
        RAISE EXCEPTION 'The code and kind of a pay component in use cannot change' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

TEMPLATE_LINES_GUARD = """
CREATE OR REPLACE FUNCTION salary_template_lines_guard() RETURNS trigger AS $$
DECLARE
    version_status text;
BEGIN
    SELECT status INTO version_status FROM salary_template_versions
    WHERE id = COALESCE(NEW.template_version_id, OLD.template_version_id)
      AND company_id = COALESCE(NEW.company_id, OLD.company_id);
    IF version_status IS NOT NULL AND version_status <> 'DRAFT' THEN
        RAISE EXCEPTION 'The lines of a published salary structure version cannot change'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;
"""

TEMPLATE_VERSION_GUARD = """
CREATE OR REPLACE FUNCTION salary_template_versions_guard() RETURNS trigger AS $$
BEGIN
    IF OLD.status <> 'DRAFT' AND (
        NEW.eligibility IS DISTINCT FROM OLD.eligibility OR NEW.test_vectors IS DISTINCT FROM OLD.test_vectors
        OR NEW.test_range IS DISTINCT FROM OLD.test_range OR NEW.template_id IS DISTINCT FROM OLD.template_id
        OR NEW.version IS DISTINCT FROM OLD.version OR NEW.effective_from IS DISTINCT FROM OLD.effective_from
        OR NEW.status = 'DRAFT'
    ) THEN
        RAISE EXCEPTION 'A published salary structure version is frozen' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

ASSIGNMENT_GUARD = """
CREATE OR REPLACE FUNCTION compensation_assignments_guard() RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM employee_assignments a
        WHERE a.id = NEW.assignment_id AND a.company_id = NEW.company_id AND a.employee_id = NEW.employee_id
    ) THEN
        RAISE EXCEPTION 'The placement does not belong to the employee' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status IN ('SCHEDULED', 'ACTIVE') THEN
        -- Serialise compensation changes per employee, then reject overlapping windows of one placement.
        PERFORM 1 FROM employees WHERE id = NEW.employee_id AND company_id = NEW.company_id FOR UPDATE;
        IF EXISTS (
            SELECT 1 FROM compensation_assignments c
            WHERE c.company_id = NEW.company_id AND c.assignment_id = NEW.assignment_id
              AND c.status IN ('SCHEDULED', 'ACTIVE') AND c.id <> NEW.id
              AND daterange(c.effective_from, c.effective_to, '[]') && daterange(NEW.effective_from, NEW.effective_to, '[]')
        ) THEN
            RAISE EXCEPTION 'Compensation assignments of one placement cannot overlap'
                USING ERRCODE = 'exclusion_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def _base():
    return [
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    ]


def _tenant(name: str, *items):
    op.create_table(
        name,
        *items,
        sa.Column('company_id', sa.Integer(), nullable=False),
        *_base(),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='RESTRICT'),
        sa.UniqueConstraint('company_id', 'id', name=f'uq_{name}_company_id_id'),
    )
    op.create_index(f'ix_{name}_company_id', name, ['company_id'])


def _jsonb(name: str, default: str, nullable: bool = False):
    return sa.Column(name, postgresql.JSONB(), nullable=nullable, server_default=sa.text(f"'{default}'::jsonb"))


def upgrade() -> None:
    bind = op.get_bind()

    _tenant(
        'pay_components',
        sa.Column('code', sa.String(40), nullable=False),
        sa.Column('kind', sa.String(25), nullable=False),
        sa.Column('category', sa.String(25), nullable=False),
        sa.Column('owner', sa.String(20), nullable=False, server_default='COMPANY'),
        sa.Column('status', sa.String(10), nullable=False, server_default='ACTIVE'),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.UniqueConstraint('company_id', 'code', name='uq_pay_components_company_code'),
        sa.CheckConstraint("code ~ '^[A-Z][A-Z0-9_]{0,39}$' AND position('__' in code) = 0", name='ck_pay_components_code'),
        sa.CheckConstraint(f"kind IN {KINDS}", name='ck_pay_components_kind'),
        sa.CheckConstraint(f"category IN {CATEGORIES}", name='ck_pay_components_category'),
        sa.CheckConstraint("owner = 'COMPANY' OR owner ~ '^PACK:[A-Z0-9_]{1,15}$'", name='ck_pay_components_owner'),
        sa.CheckConstraint("status IN ('ACTIVE', 'ARCHIVED')", name='ck_pay_components_status'),
    )

    _tenant(
        'pay_component_versions',
        sa.Column('component_id', sa.Integer(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(150), nullable=False),
        _jsonb('translations', '{}'),
        sa.Column('description', sa.String(1000), nullable=True),
        sa.Column('calc_method', sa.String(15), nullable=False),
        sa.Column('formula', sa.Text(), nullable=True),
        _jsonb('params', '{}'),
        sa.Column('tags', postgresql.ARRAY(sa.String(40)), nullable=False, server_default=sa.text("'{}'::varchar[]")),
        sa.Column('proration', sa.String(20), nullable=False, server_default='NONE'),
        sa.Column('proration_basis', sa.String(20), nullable=True),
        _jsonb('frequency', '{"type": "EVERY_PERIOD"}'),
        _jsonb('taxability', '{}'),
        _jsonb('totals_flags', '{}'),
        sa.Column('cap', postgresql.JSONB(), nullable=True),
        sa.Column('floor', postgresql.JSONB(), nullable=True),
        sa.Column('rounding', sa.String(10), nullable=True),
        sa.Column('eligibility', sa.Text(), nullable=True),
        _jsonb('payslip', '{}'),
        sa.Column('visibility', sa.String(20), nullable=False, server_default='EMPLOYEE_VISIBLE'),
        sa.Column('gl', postgresql.JSONB(), nullable=True),
        _jsonb('reporting_codes', '{}'),
        sa.Column('input_approval_above', sa.Numeric(19, 4), nullable=True),
        sa.Column('input_approval_currency', sa.String(3), nullable=True),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('effective_to', sa.Date(), nullable=True),
        sa.Column('status', sa.String(12), nullable=False, server_default='ACTIVE'),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.UniqueConstraint('company_id', 'component_id', 'version', name='uq_pay_component_versions_version'),
        sa.ForeignKeyConstraint(['company_id', 'component_id'], ['pay_components.company_id', 'pay_components.id'],
                                ondelete='CASCADE', name='fk_pay_component_versions_component'),
        sa.CheckConstraint('version >= 1', name='ck_pay_component_versions_version'),
        sa.CheckConstraint(f"calc_method IN {METHODS}", name='ck_pay_component_versions_method'),
        sa.CheckConstraint(f"proration IN {PRORATIONS}", name='ck_pay_component_versions_proration'),
        sa.CheckConstraint(f"proration_basis IS NULL OR proration_basis IN {PRORATION_BASES}",
                           name='ck_pay_component_versions_basis'),
        sa.CheckConstraint(f"visibility IN {VISIBILITIES}", name='ck_pay_component_versions_visibility'),
        sa.CheckConstraint(f"rounding IS NULL OR rounding IN {ROUNDING_MODES}", name='ck_pay_component_versions_rounding'),
        sa.CheckConstraint("status IN ('ACTIVE', 'SUPERSEDED')", name='ck_pay_component_versions_status'),
        sa.CheckConstraint('effective_to IS NULL OR effective_to >= effective_from', name='ck_pay_component_versions_window'),
        sa.CheckConstraint("jsonb_typeof(params) = 'object'", name='ck_pay_component_versions_params'),
        sa.CheckConstraint('(input_approval_above IS NULL) = (input_approval_currency IS NULL)',
                           name='ck_pay_component_versions_approval_currency'),
    )
    op.create_index('ix_pay_component_versions_component_id', 'pay_component_versions', ['component_id'])
    op.create_index('ix_pay_component_versions_lookup', 'pay_component_versions',
                    ['company_id', 'component_id', 'effective_from'])

    _tenant(
        'salary_templates',
        sa.Column('code', sa.String(40), nullable=False),
        sa.Column('name', sa.String(150), nullable=False),
        sa.Column('description', sa.String(1000), nullable=True),
        sa.Column('type', sa.String(15), nullable=False),
        sa.Column('currency', sa.String(3), nullable=True),
        sa.Column('status', sa.String(10), nullable=False, server_default='ACTIVE'),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.UniqueConstraint('company_id', 'code', name='uq_salary_templates_company_code'),
        sa.CheckConstraint("code ~ '^[A-Z][A-Z0-9_-]{0,39}$'", name='ck_salary_templates_code'),
        sa.CheckConstraint(f"type IN {STRUCTURE_TYPES}", name='ck_salary_templates_type'),
        sa.CheckConstraint("currency IS NULL OR currency ~ '^[A-Z]{3}$'", name='ck_salary_templates_currency'),
        sa.CheckConstraint("status IN ('ACTIVE', 'ARCHIVED')", name='ck_salary_templates_status'),
    )

    _tenant(
        'salary_template_versions',
        sa.Column('template_id', sa.Integer(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(10), nullable=False, server_default='DRAFT'),
        _jsonb('eligibility', '{}'),
        sa.Column('test_range', postgresql.JSONB(), nullable=True),
        _jsonb('test_vectors', '[]'),
        sa.Column('validation', postgresql.JSONB(), nullable=True),
        sa.Column('effective_from', sa.Date(), nullable=True),
        sa.Column('row_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('published_by_user_id', sa.Integer(), nullable=True),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('company_id', 'template_id', 'version', name='uq_salary_template_versions_version'),
        sa.ForeignKeyConstraint(['company_id', 'template_id'], ['salary_templates.company_id', 'salary_templates.id'],
                                ondelete='CASCADE', name='fk_salary_template_versions_template'),
        sa.CheckConstraint('version >= 1', name='ck_salary_template_versions_version'),
        sa.CheckConstraint('row_version >= 1', name='ck_salary_template_versions_row_version'),
        sa.CheckConstraint("status IN ('DRAFT', 'ACTIVE', 'ARCHIVED')", name='ck_salary_template_versions_status'),
        sa.CheckConstraint("jsonb_typeof(eligibility) = 'object'", name='ck_salary_template_versions_eligibility'),
        sa.CheckConstraint("jsonb_typeof(test_vectors) = 'array'", name='ck_salary_template_versions_vectors'),
        sa.CheckConstraint("status = 'DRAFT' OR (published_at IS NOT NULL AND effective_from IS NOT NULL)",
                           name='ck_salary_template_versions_published'),
    )
    op.create_index('ix_salary_template_versions_template_id', 'salary_template_versions', ['template_id'])
    op.create_index('uq_salary_template_versions_active', 'salary_template_versions', ['template_id'], unique=True,
                    postgresql_where=sa.text("status = 'ACTIVE'"))

    _tenant(
        'salary_template_lines',
        sa.Column('template_version_id', sa.Integer(), nullable=False),
        sa.Column('component_id', sa.Integer(), nullable=False),
        sa.Column('method', sa.String(15), nullable=False),
        sa.Column('value', sa.Numeric(19, 6), nullable=True),
        sa.Column('formula', sa.Text(), nullable=True),
        _jsonb('params', '{}'),
        sa.Column('overridable', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('override_min', sa.Numeric(19, 6), nullable=True),
        sa.Column('override_max', sa.Numeric(19, 6), nullable=True),
        sa.Column('display_order', sa.Integer(), nullable=False, server_default='0'),
        sa.UniqueConstraint('company_id', 'template_version_id', 'component_id',
                            name='uq_salary_template_lines_component'),
        sa.ForeignKeyConstraint(['company_id', 'template_version_id'],
                                ['salary_template_versions.company_id', 'salary_template_versions.id'],
                                ondelete='CASCADE', name='fk_salary_template_lines_version'),
        sa.ForeignKeyConstraint(['company_id', 'component_id'], ['pay_components.company_id', 'pay_components.id'],
                                name='fk_salary_template_lines_component'),
        sa.CheckConstraint(f"method IN {METHODS}", name='ck_salary_template_lines_method'),
        sa.CheckConstraint("jsonb_typeof(params) = 'object'", name='ck_salary_template_lines_params'),
        sa.CheckConstraint('override_min IS NULL OR override_max IS NULL OR override_max >= override_min',
                           name='ck_salary_template_lines_bounds'),
        sa.CheckConstraint('overridable OR (override_min IS NULL AND override_max IS NULL)',
                           name='ck_salary_template_lines_overridable'),
    )
    op.create_index('ix_salary_template_lines_template_version_id', 'salary_template_lines', ['template_version_id'])
    op.create_index('ix_salary_template_lines_component_id', 'salary_template_lines', ['component_id'])

    _tenant(
        'revision_cycles',
        sa.Column('name', sa.String(150), nullable=False),
        _jsonb('population', '{}'),
        _jsonb('method', '{}'),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='DRAFT'),
        sa.Column('totals', postgresql.JSONB(), nullable=True),
        sa.Column('approval_request_id', sa.Integer(), nullable=True),
        sa.Column('row_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('applied_by_user_id', sa.Integer(), nullable=True),
        sa.Column('applied_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"status IN {CYCLE_STATUSES}", name='ck_revision_cycles_status'),
        sa.CheckConstraint("jsonb_typeof(population) = 'object'", name='ck_revision_cycles_population'),
        sa.CheckConstraint("jsonb_typeof(method) = 'object'", name='ck_revision_cycles_method'),
        sa.CheckConstraint('row_version >= 1', name='ck_revision_cycles_row_version'),
    )

    _tenant(
        'compensation_assignments',
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('assignment_id', sa.Integer(), nullable=False),
        sa.Column('template_version_id', sa.Integer(), nullable=False),
        sa.Column('pay_basis_amount', sa.Numeric(19, 4), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False),
        sa.Column('fx_rule', postgresql.JSONB(), nullable=True),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('effective_to', sa.Date(), nullable=True),
        sa.Column('reason', sa.String(12), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='DRAFT'),
        sa.Column('note', sa.String(1000), nullable=True),
        _jsonb('attachments', '[]'),
        sa.Column('split_period', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('previous_assignment_id', sa.Integer(), nullable=True),
        sa.Column('revision_cycle_id', sa.Integer(), nullable=True),
        sa.Column('legacy_salary_structure_id', sa.Integer(), nullable=True),
        sa.Column('row_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('submitted_by_user_id', sa.Integer(), nullable=True),
        sa.Column('approved_by_user_id', sa.Integer(), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['company_id', 'employee_id'], ['employees.company_id', 'employees.id'],
                                name='fk_compensation_assignments_employee'),
        # The guard trigger also checks that the placement belongs to the same employee.
        sa.ForeignKeyConstraint(['company_id', 'assignment_id'],
                                ['employee_assignments.company_id', 'employee_assignments.id'],
                                name='fk_compensation_assignments_placement'),
        sa.ForeignKeyConstraint(['company_id', 'template_version_id'],
                                ['salary_template_versions.company_id', 'salary_template_versions.id'],
                                name='fk_compensation_assignments_template_version'),
        sa.ForeignKeyConstraint(['company_id', 'previous_assignment_id'],
                                ['compensation_assignments.company_id', 'compensation_assignments.id'],
                                name='fk_compensation_assignments_previous'),
        sa.ForeignKeyConstraint(['company_id', 'revision_cycle_id'], ['revision_cycles.company_id', 'revision_cycles.id'],
                                name='fk_compensation_assignments_cycle'),
        sa.CheckConstraint('pay_basis_amount >= 0', name='ck_compensation_assignments_amount'),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name='ck_compensation_assignments_currency'),
        sa.CheckConstraint(f"reason IN {REASONS}", name='ck_compensation_assignments_reason'),
        sa.CheckConstraint(f"status IN {STATUSES}", name='ck_compensation_assignments_status'),
        sa.CheckConstraint('effective_to IS NULL OR effective_to >= effective_from',
                           name='ck_compensation_assignments_window'),
        sa.CheckConstraint('row_version >= 1', name='ck_compensation_assignments_row_version'),
        sa.CheckConstraint("jsonb_typeof(attachments) = 'array'", name='ck_compensation_assignments_attachments'),
        sa.CheckConstraint('approved_by_user_id IS NULL OR created_by_user_id IS NULL '
                           'OR approved_by_user_id <> created_by_user_id',
                           name='ck_compensation_assignments_maker_checker'),
    )
    for column in ('employee_id', 'assignment_id', 'template_version_id'):
        op.create_index(f'ix_compensation_assignments_{column}', 'compensation_assignments', [column])
    op.create_index('ix_compensation_assignments_current', 'compensation_assignments',
                    ['company_id', 'employee_id', 'effective_from'])
    op.create_index('uq_compensation_assignments_legacy', 'compensation_assignments',
                    ['company_id', 'legacy_salary_structure_id'], unique=True,
                    postgresql_where=sa.text('legacy_salary_structure_id IS NOT NULL'))

    _tenant(
        'compensation_assignment_lines',
        sa.Column('compensation_assignment_id', sa.Integer(), nullable=False),
        sa.Column('component_id', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Numeric(19, 4), nullable=True),
        sa.Column('percent', sa.Numeric(12, 6), nullable=True),
        sa.Column('formula', sa.Text(), nullable=True),
        sa.UniqueConstraint('company_id', 'compensation_assignment_id', 'component_id',
                            name='uq_compensation_assignment_lines_component'),
        sa.ForeignKeyConstraint(['company_id', 'compensation_assignment_id'],
                                ['compensation_assignments.company_id', 'compensation_assignments.id'],
                                ondelete='CASCADE', name='fk_compensation_assignment_lines_assignment'),
        sa.ForeignKeyConstraint(['company_id', 'component_id'], ['pay_components.company_id', 'pay_components.id'],
                                name='fk_compensation_assignment_lines_component'),
        sa.CheckConstraint('(amount IS NOT NULL)::int + (percent IS NOT NULL)::int + (formula IS NOT NULL)::int = 1',
                           name='ck_compensation_assignment_lines_one_value'),
    )
    op.create_index('ix_compensation_assignment_lines_compensation_assignment_id', 'compensation_assignment_lines',
                    ['compensation_assignment_id'])

    _tenant(
        'revision_cycle_items',
        sa.Column('cycle_id', sa.Integer(), nullable=False),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('old_assignment_id', sa.Integer(), nullable=True),
        sa.Column('new_assignment_id', sa.Integer(), nullable=True),
        _jsonb('proposed', '{}'),
        sa.Column('status', sa.String(10), nullable=False, server_default='PROPOSED'),
        sa.Column('note', sa.String(500), nullable=True),
        sa.UniqueConstraint('company_id', 'cycle_id', 'employee_id', name='uq_revision_cycle_items_employee'),
        sa.ForeignKeyConstraint(['company_id', 'cycle_id'], ['revision_cycles.company_id', 'revision_cycles.id'],
                                ondelete='CASCADE', name='fk_revision_cycle_items_cycle'),
        sa.ForeignKeyConstraint(['company_id', 'employee_id'], ['employees.company_id', 'employees.id'],
                                name='fk_revision_cycle_items_employee'),
        sa.ForeignKeyConstraint(['company_id', 'old_assignment_id'],
                                ['compensation_assignments.company_id', 'compensation_assignments.id'],
                                name='fk_revision_cycle_items_old'),
        sa.ForeignKeyConstraint(['company_id', 'new_assignment_id'],
                                ['compensation_assignments.company_id', 'compensation_assignments.id'],
                                name='fk_revision_cycle_items_new'),
        sa.CheckConstraint(f"status IN {CYCLE_ITEM_STATUSES}", name='ck_revision_cycle_items_status'),
    )
    op.create_index('ix_revision_cycle_items_cycle_id', 'revision_cycle_items', ['cycle_id'])

    _tenant(
        'compensation_imports',
        sa.Column('status', sa.String(10), nullable=False, server_default='VALIDATED'),
        sa.Column('file_name', sa.String(255), nullable=True),
        _jsonb('rows', '[]'),
        _jsonb('summary', '{}'),
        sa.Column('result', postgresql.JSONB(), nullable=True),
        sa.Column('job_id', sa.Integer(), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=False),
        sa.CheckConstraint(f"status IN {IMPORT_STATUSES}", name='ck_compensation_imports_status'),
        sa.CheckConstraint("jsonb_typeof(rows) = 'array'", name='ck_compensation_imports_rows'),
    )

    _install_security(bind)
    summary = backfill(bind)
    logger.info("Compensation v2 legacy migration: %s", summary)


def _install_security(bind) -> None:
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")

    for function in (COMPONENT_VERSION_GUARD, COMPONENT_CODE_GUARD, TEMPLATE_LINES_GUARD, TEMPLATE_VERSION_GUARD,
                     ASSIGNMENT_GUARD):
        op.execute(function)
    op.execute("CREATE TRIGGER trg_pay_component_versions_guard BEFORE INSERT OR UPDATE ON pay_component_versions "
               "FOR EACH ROW EXECUTE FUNCTION pay_component_versions_guard()")
    op.execute("CREATE TRIGGER trg_pay_components_code_guard BEFORE UPDATE OF code, kind ON pay_components "
               "FOR EACH ROW EXECUTE FUNCTION pay_components_code_guard()")
    op.execute("CREATE TRIGGER trg_salary_template_lines_guard BEFORE INSERT OR UPDATE OR DELETE ON salary_template_lines "
               "FOR EACH ROW EXECUTE FUNCTION salary_template_lines_guard()")
    op.execute("CREATE TRIGGER trg_salary_template_versions_guard BEFORE UPDATE ON salary_template_versions "
               "FOR EACH ROW EXECUTE FUNCTION salary_template_versions_guard()")
    op.execute("CREATE TRIGGER trg_compensation_assignments_guard BEFORE INSERT OR UPDATE ON compensation_assignments "
               "FOR EACH ROW EXECUTE FUNCTION compensation_assignments_guard()")

    roles = [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"
    ))]
    for role in roles:
        quoted = '"' + role.replace('"', '""') + '"'
        for table in TENANT_TABLES:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {quoted}")
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}")


# --------------------------------------------------------------------------------------- legacy backfill
def _local_today(timezone_name) -> date:
    """The company's local date (UTC when unknown); this PostgreSQL build ships no time zone files."""
    try:
        import pytz

        return datetime.now(timezone.utc).astimezone(pytz.timezone(timezone_name or "UTC")).date()
    except Exception:
        return datetime.now(timezone.utc).date()


def _overlaps(a_from: date, a_to, b_from: date, b_to) -> bool:
    return a_from <= (b_to or date.max) and b_from <= (a_to or date.max)


def backfill(bind) -> dict:
    """Legacy salary_structure rows -> compensation v2 (3.3 §10). Safe to run again: it only adds what is
    missing (components, the legacy structure, one assignment per unmapped row). Returns counts per step."""
    done = {"components": 0, "templates": 0, "assignments": 0, "closed": 0, "superseded": 0, "skipped": 0}

    def run(sql: str, **params):
        return bind.execute(sa.text(sql), params)

    for code, name, category, _column, order in LEGACY_COMPONENTS:
        done["components"] += run("""
            INSERT INTO pay_components (company_id, code, kind, category, owner, status, created_at, updated_at)
            SELECT c.id, :code, 'EARNING', :category, 'COMPANY', 'ACTIVE', now(), now() FROM companies c
            WHERE NOT EXISTS (SELECT 1 FROM pay_components p WHERE p.company_id = c.id AND p.code = :code)
        """, code=code, category=category).rowcount
        run(f"""
            INSERT INTO pay_component_versions (company_id, component_id, version, name, calc_method, params, tags,
                proration, frequency, totals_flags, payslip, visibility, effective_from, status, created_at, updated_at)
            SELECT p.company_id, p.id, 1, :name, 'FIXED', '{{}}'::jsonb, ARRAY['LOP_APPLICABLE']::varchar[],
                   'BY_PAID_DAYS', '{{"type": "EVERY_PERIOD"}}'::jsonb, '{EARNING_TOTALS}'::jsonb,
                   jsonb_build_object('order', :order, 'show_if_zero', false), 'EMPLOYEE_VISIBLE',
                   COALESCE((SELECT min(s.effective_from) FROM salary_structure s WHERE s.company_id = p.company_id),
                            DATE '2000-01-01'),
                   'ACTIVE', now(), now()
            FROM pay_components p
            WHERE p.code = :code
              AND NOT EXISTS (SELECT 1 FROM pay_component_versions v WHERE v.component_id = p.id)
        """, code=code, name=name, order=order)

    done["templates"] += run("""
        INSERT INTO salary_templates (company_id, code, name, description, type, currency, status, created_at, updated_at)
        SELECT c.id, :code, :name, 'Created by migration 0015 from the legacy fixed salary columns.', 'GROSS_BASED',
               NULL, 'ACTIVE', now(), now()
        FROM companies c
        WHERE NOT EXISTS (SELECT 1 FROM salary_templates t WHERE t.company_id = c.id AND t.code = :code)
    """, code=LEGACY_TEMPLATE_CODE, name=LEGACY_TEMPLATE_NAME).rowcount
    created = run("""
        INSERT INTO salary_template_versions (company_id, template_id, version, status, eligibility, test_vectors,
            effective_from, published_at, created_at, updated_at)
        SELECT t.company_id, t.id, 1, 'DRAFT', '{}'::jsonb, '[]'::jsonb,
               COALESCE((SELECT min(s.effective_from) FROM salary_structure s WHERE s.company_id = t.company_id),
                        DATE '2000-01-01'),
               now(), now(), now()
        FROM salary_templates t
        WHERE t.code = :code AND NOT EXISTS (SELECT 1 FROM salary_template_versions v WHERE v.template_id = t.id)
        RETURNING id
    """, code=LEGACY_TEMPLATE_CODE).fetchall()
    new_versions = [row[0] for row in created]
    if new_versions:
        for code, _name, _category, _column, order in LEGACY_COMPONENTS:
            run("""
                INSERT INTO salary_template_lines (company_id, template_version_id, component_id, method, params,
                    overridable, display_order, created_at, updated_at)
                SELECT v.company_id, v.id, p.id, 'FIXED', '{}'::jsonb, true, :order, now(), now()
                FROM salary_template_versions v
                JOIN pay_components p ON p.company_id = v.company_id AND p.code = :code
                WHERE v.id = ANY(:ids)
            """, code=code, order=order, ids=new_versions)
        run("UPDATE salary_template_versions SET status = 'ACTIVE' WHERE id = ANY(:ids)", ids=new_versions)

    companies = run("SELECT id, timezone FROM companies ORDER BY id").fetchall()
    columns = ", ".join(f"s.{column}" for _c, _n, _cat, column, _o in LEGACY_COMPONENTS)
    for company_id, timezone_name in companies:
        today = _local_today(timezone_name)
        version_id = run("""
            SELECT v.id FROM salary_template_versions v JOIN salary_templates t ON t.id = v.template_id
            WHERE t.company_id = :c AND t.code = :code ORDER BY v.version LIMIT 1
        """, c=company_id, code=LEGACY_TEMPLATE_CODE).scalar()
        components = dict(run("SELECT code, id FROM pay_components WHERE company_id = :c AND code = ANY(:codes)",
                              c=company_id, codes=[c for c, *_ in LEGACY_COMPONENTS]).fetchall())
        rows = run(f"""
            SELECT s.id, s.employee_id, s.effective_from, s.effective_to, s.created_by_user_id, {columns}
            FROM salary_structure s
            WHERE s.company_id = :c
            ORDER BY s.employee_id, s.effective_from, s.id
        """, c=company_id).fetchall()
        windows: dict = {}
        for row in rows:
            legacy_id, employee_id, start, end, created_by = row[0], row[1], row[2], row[3], row[4]
            amounts = row[5:]
            if employee_id not in windows:
                windows[employee_id] = [list(r) for r in run("""
                    SELECT id, effective_from, effective_to FROM compensation_assignments
                    WHERE company_id = :c AND employee_id = :e AND status IN ('ACTIVE', 'SCHEDULED')
                    ORDER BY effective_from
                """, c=company_id, e=employee_id).fetchall()]
            if run("SELECT 1 FROM compensation_assignments WHERE company_id = :c AND legacy_salary_structure_id = :s",
                   c=company_id, s=legacy_id).first():
                continue
            placement = run("""
                SELECT a.id, le.base_currency FROM employee_assignments a
                JOIN legal_entities le ON le.company_id = a.company_id AND le.id = a.legal_entity_id
                WHERE a.company_id = :c AND a.employee_id = :e AND a.is_primary
                ORDER BY (a.effective_from <= :d AND (a.effective_to IS NULL OR a.effective_to >= :d)) DESC,
                         a.effective_from
                LIMIT 1
            """, c=company_id, e=employee_id, d=start).first()
            if placement is None:
                done["skipped"] += 1
                logger.warning("Company %s: salary_structure %s (employee %s) has no placement; not migrated.",
                               company_id, legacy_id, employee_id)
                continue
            # The later start wins, as the legacy engine reads overlapping rows.
            for window in windows[employee_id]:
                other_id, other_from, other_to = window
                if not _overlaps(other_from, other_to, start, end):
                    continue
                if other_from < start:
                    window[2] = start - timedelta(days=1)
                    run("UPDATE compensation_assignments SET effective_to = :to, updated_at = now() WHERE id = :id",
                        to=window[2], id=other_id)
                    done["closed"] += 1
                else:
                    run("UPDATE compensation_assignments SET status = 'SUPERSEDED', updated_at = now() WHERE id = :id",
                        id=other_id)
                    window[1], window[2] = date.max, date.max
                    done["superseded"] += 1
            windows[employee_id] = [w for w in windows[employee_id] if w[1] != date.max]
            total = sum(amounts)
            new_id = run("""
                INSERT INTO compensation_assignments (company_id, employee_id, assignment_id, template_version_id,
                    pay_basis_amount, currency, effective_from, effective_to, reason, status, note, attachments,
                    split_period, legacy_salary_structure_id, row_version, created_by_user_id, approved_at,
                    created_at, updated_at)
                VALUES (:c, :e, :a, :v, :amount, :currency, :start, :end, 'MIGRATED', :status, :note, '[]'::jsonb,
                        false, :legacy, 1, :by, now(), now(), now())
                RETURNING id
            """, c=company_id, e=employee_id, a=placement[0], v=version_id, amount=total, currency=placement[1],
                start=start, end=end, status="SCHEDULED" if start > today else "ACTIVE", note=MIGRATION_NOTE,
                legacy=legacy_id, by=created_by).scalar()
            for (code, *_rest), amount in zip(LEGACY_COMPONENTS, amounts):
                run("""
                    INSERT INTO compensation_assignment_lines (company_id, compensation_assignment_id, component_id,
                        amount, created_at, updated_at)
                    VALUES (:c, :a, :p, :amount, now(), now())
                """, c=company_id, a=new_id, p=components[code], amount=amount)
            windows[employee_id].append([new_id, start, end])
            done["assignments"] += 1
    return done


def downgrade() -> None:
    for trigger, table in (
        ("trg_compensation_assignments_guard", "compensation_assignments"),
        ("trg_salary_template_versions_guard", "salary_template_versions"),
        ("trg_salary_template_lines_guard", "salary_template_lines"),
        ("trg_pay_components_code_guard", "pay_components"),
        ("trg_pay_component_versions_guard", "pay_component_versions"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    for table in reversed(TENANT_TABLES):
        op.drop_table(table)
    for function in ("compensation_assignments_guard", "salary_template_versions_guard", "salary_template_lines_guard",
                     "pay_components_code_guard", "pay_component_versions_guard"):
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")

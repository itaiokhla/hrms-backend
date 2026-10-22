"""Payroll organisation dimensions: pay groups, grades, cost centers, employment types, designations, work weeks, holiday calendars, assignment fields, PAY_GROUP scope, audit details.

Additive and backwards compatible (02-multi-level-company §10-§13, §18, §19; 3.4 §3 and §8; 3.9 §3.11):
  * tenant tables pay_groups (one legal entity; frequency, currency, calendar rules), grades
    (salary ranges as decimal strings), cost_centers, employment_types (worker class),
    designations, work_week_patterns (weekly offs Mon=0..Sun=6 plus nth-weekday rules such as
    "2nd and 4th Saturday") and holiday_calendars (a calendar inherits its ancestors' holidays;
    a trigger rejects cycles), each with row-level security, grants and composite same-company
    foreign keys;
  * employee_assignments gain pay group, grade, employment type, designation, cost center, work
    week, shift (shift history) and fte; employee_assignments_guard() keeps every rule of 0005 and
    also requires the pay group to belong to the row's legal entity (a composite foreign key on
    (company_id, legal_entity_id, pay_group_id) keeps that true if a pay group is edited later);
  * branches gain holiday_calendar_id and work_week_pattern_id, legal_entities
    default_work_week_pattern_id, holidays calendar_id and holiday_type (location and is_optional
    are untouched and still drive today's holiday screens), companies reporting_currency,
    data_region and setup_completed_at, audit_logs details (append-only rows: never backfilled);
  * role_permissions and user_role_assignment_targets accept the PAY_GROUP scope.

Backfill (idempotent: it only inserts missing rows and fills NULL new columns; no existing value
is changed or deleted):
  * per legal entity one MONTHLY pay group in the entity's base currency, periods from the 1st,
    paid on the last working day. Codes are unique per tenant, so the default entity's group is
    MONTHLY and any other entity's MONTHLY-<entity code>; it is INACTIVE when the entity is.
    Every primary assignment gets the pay group of its legal entity;
  * per tenant the employment type PERMANENT (EMPLOYEE) on every assignment, and the work week
    MON_FRI (Saturday and Sunday off, what payroll assumes today) as every legal entity's default
    and every branch's pattern;
  * per tenant the holiday calendar COMPANY holding the holidays without a location, and per
    branch a calendar BR-<branch code> (BR-<entity code>-<branch code> when entities share a
    branch code) inheriting from COMPANY and holding the holidays whose location equals the
    branch name exactly, as today's lookups match; each branch points at its calendar;
  * holidays whose location matches no branch name (a typo, a different case or spacing, a
    renamed branch) go to a calendar UNMATCHED that no branch uses, created only for tenants that
    have such holidays and logged per tenant, for an administrator to review. Today they reach
    only employees whose free-text location still equals that text;
  * holiday_type OPTIONAL where is_optional (others keep the default COMPANY);
  * the shift of each primary placement in effect today (company timezone) is the employee's
    current shift; earlier and future placements stay NULL (no shift history exists).

Downgrade deletes PAY_GROUP grants and targets, restores the 0005 guard and drops the new
columns and tables. Holiday-calendar trees take the advisory lock (6110, company_id); 0009 uses 6101-6104.

Revision ID: 0008_payroll_org
Revises: 0007_partner_admins
Create Date: 2026-10-20
"""
import json
import logging
from datetime import date, datetime
from typing import Sequence, Union

import pytz

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0008_payroll_org'
down_revision: Union[str, None] = '0007_partner_admins'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.0008")

TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
NEW_TABLES = ("work_week_patterns", "holiday_calendars", "grades", "employment_types", "cost_centers", "designations",
              "pay_groups")
STATUSES = "('ACTIVE', 'INACTIVE')"
OLD_SCOPES = "('SELF', 'DIRECT_REPORTS', 'REPORTING_TREE', 'DEPARTMENT', 'ASSIGNED_GROUP', 'LEGAL_ENTITY', 'BRANCH', 'COMPANY')"
NEW_SCOPES = ("('SELF', 'DIRECT_REPORTS', 'REPORTING_TREE', 'DEPARTMENT', 'ASSIGNED_GROUP', 'LEGAL_ENTITY', 'BRANCH', "
              "'PAY_GROUP', 'COMPANY')")
OLD_TARGET_SCOPES = "('LEGAL_ENTITY', 'BRANCH')"
NEW_TARGET_SCOPES = "('LEGAL_ENTITY', 'BRANCH', 'PAY_GROUP')"
ASSIGNMENT_REFERENCES = (  # column, constraint, referenced table
    ('grade_id', 'fk_employee_assignments_grade', 'grades'),
    ('employment_type_id', 'fk_employee_assignments_employment_type', 'employment_types'),
    ('designation_id', 'fk_employee_assignments_designation', 'designations'),
    ('cost_center_id', 'fk_employee_assignments_cost_center', 'cost_centers'),
    ('work_week_pattern_id', 'fk_employee_assignments_work_week', 'work_week_patterns'),
    ('shift_id', 'fk_employee_assignments_shift', 'shifts'),
)
CALENDAR_LOCK_NAMESPACE = 6110
MONTHLY_RULES = {"monthly_start_day": 1, "pay_day_rule": "LAST_WORKING_DAY"}

VALIDATORS = (
    """
    CREATE OR REPLACE FUNCTION work_week_rules_valid(off_days jsonb, nth_rules jsonb) RETURNS boolean
    LANGUAGE plpgsql IMMUTABLE AS $$
    DECLARE
        rule jsonb;
    BEGIN
        -- Weekly offs: distinct weekday numbers (Monday = 0 ... Sunday = 6), at least one working day.
        IF jsonb_typeof(off_days) IS DISTINCT FROM 'array' OR jsonb_typeof(nth_rules) IS DISTINCT FROM 'array' THEN
            RETURN false;
        END IF;
        IF jsonb_array_length(off_days) > 6 THEN
            RETURN false;
        END IF;
        IF EXISTS (SELECT 1 FROM jsonb_array_elements(off_days) AS d(value) WHERE d.value::text !~ '^[0-6]$') THEN
            RETURN false;
        END IF;
        IF (SELECT count(DISTINCT d.value::text) FROM jsonb_array_elements(off_days) AS d(value))
           <> jsonb_array_length(off_days) THEN
            RETURN false;
        END IF;
        -- Extra offs on the nth weekday of a month: {"weekday": 5, "nth": [2, 4]}; -1 = the last one.
        FOR rule IN SELECT value FROM jsonb_array_elements(nth_rules) LOOP
            IF jsonb_typeof(rule) <> 'object' THEN
                RETURN false;
            END IF;
            IF COALESCE((rule -> 'weekday')::text, '') !~ '^[0-6]$' THEN
                RETURN false;
            END IF;
            IF jsonb_typeof(rule -> 'nth') IS DISTINCT FROM 'array' THEN
                RETURN false;
            END IF;
            IF jsonb_array_length(rule -> 'nth') = 0 THEN
                RETURN false;
            END IF;
            IF EXISTS (SELECT 1 FROM jsonb_array_elements(rule -> 'nth') AS n(value) WHERE n.value::text !~ '^(-1|[1-5])$') THEN
                RETURN false;
            END IF;
        END LOOP;
        RETURN true;
    END;
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION salary_ranges_valid(ranges jsonb) RETURNS boolean
    LANGUAGE plpgsql IMMUTABLE AS $$
    DECLARE
        band jsonb;
        seen text[] := '{}';
        amount_pattern constant text := '^[0-9]{1,15}([.][0-9]{1,4})?$';
    BEGIN
        -- [{"currency": "INR", "min": "300000.00", "max": "600000.00"}, ...]: decimal strings, never
        -- JSON numbers; one band per currency; min <= mid <= max.
        IF jsonb_typeof(ranges) IS DISTINCT FROM 'array' THEN
            RETURN false;
        END IF;
        FOR band IN SELECT value FROM jsonb_array_elements(ranges) LOOP
            IF jsonb_typeof(band) <> 'object' THEN
                RETURN false;
            END IF;
            IF jsonb_typeof(band -> 'currency') IS DISTINCT FROM 'string' THEN
                RETURN false;
            END IF;
            IF band ->> 'currency' !~ '^[A-Z]{3}$' OR band ->> 'currency' = ANY (seen) THEN
                RETURN false;
            END IF;
            seen := seen || (band ->> 'currency');
            IF jsonb_typeof(band -> 'min') IS DISTINCT FROM 'string' OR jsonb_typeof(band -> 'max') IS DISTINCT FROM 'string' THEN
                RETURN false;
            END IF;
            IF band ->> 'min' !~ amount_pattern OR band ->> 'max' !~ amount_pattern THEN
                RETURN false;
            END IF;
            IF (band ->> 'min')::numeric > (band ->> 'max')::numeric THEN
                RETURN false;
            END IF;
            IF band ? 'mid' THEN
                IF jsonb_typeof(band -> 'mid') IS DISTINCT FROM 'string' THEN
                    RETURN false;
                END IF;
                IF band ->> 'mid' !~ amount_pattern THEN
                    RETURN false;
                END IF;
                IF (band ->> 'mid')::numeric NOT BETWEEN (band ->> 'min')::numeric AND (band ->> 'max')::numeric THEN
                    RETURN false;
                END IF;
            END IF;
        END LOOP;
        RETURN true;
    END;
    $$
    """,
)

GUARD_0005 = """
    CREATE OR REPLACE FUNCTION employee_assignments_guard() RETURNS trigger AS $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM branches b
            WHERE b.id = NEW.branch_id AND b.company_id = NEW.company_id AND b.legal_entity_id = NEW.legal_entity_id
        ) THEN
            RAISE EXCEPTION 'The branch does not belong to the legal entity' USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.is_primary THEN
            -- Serialise placement changes per employee, then reject overlapping primary windows.
            PERFORM 1 FROM employees WHERE id = NEW.employee_id AND company_id = NEW.company_id FOR UPDATE;
            IF EXISTS (
                SELECT 1 FROM employee_assignments a
                WHERE a.company_id = NEW.company_id AND a.employee_id = NEW.employee_id AND a.is_primary
                  AND a.id <> NEW.id
                  AND daterange(a.effective_from, a.effective_to, '[]')
                      && daterange(NEW.effective_from, NEW.effective_to, '[]')
            ) THEN
                RAISE EXCEPTION 'Primary assignments of one employee cannot overlap'
                    USING ERRCODE = 'exclusion_violation';
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """

GUARD_0008 = """
    CREATE OR REPLACE FUNCTION employee_assignments_guard() RETURNS trigger AS $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM branches b
            WHERE b.id = NEW.branch_id AND b.company_id = NEW.company_id AND b.legal_entity_id = NEW.legal_entity_id
        ) THEN
            RAISE EXCEPTION 'The branch does not belong to the legal entity' USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.pay_group_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM pay_groups g
            WHERE g.id = NEW.pay_group_id AND g.company_id = NEW.company_id AND g.legal_entity_id = NEW.legal_entity_id
        ) THEN
            RAISE EXCEPTION 'The pay group does not belong to the legal entity' USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.is_primary THEN
            -- Serialise placement changes per employee, then reject overlapping primary windows.
            PERFORM 1 FROM employees WHERE id = NEW.employee_id AND company_id = NEW.company_id FOR UPDATE;
            IF EXISTS (
                SELECT 1 FROM employee_assignments a
                WHERE a.company_id = NEW.company_id AND a.employee_id = NEW.employee_id AND a.is_primary
                  AND a.id <> NEW.id
                  AND daterange(a.effective_from, a.effective_to, '[]')
                      && daterange(NEW.effective_from, NEW.effective_to, '[]')
            ) THEN
                RAISE EXCEPTION 'Primary assignments of one employee cannot overlap'
                    USING ERRCODE = 'exclusion_violation';
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """

CALENDAR_CYCLE_GUARD = f"""
    CREATE OR REPLACE FUNCTION holiday_calendars_prevent_cycle() RETURNS trigger AS $$
    DECLARE
        hit integer;
    BEGIN
        IF NEW.parent_calendar_id IS NULL THEN
            RETURN NEW;
        END IF;
        IF NEW.parent_calendar_id = NEW.id THEN
            RAISE EXCEPTION 'A holiday calendar cannot inherit from itself' USING ERRCODE = 'check_violation';
        END IF;
        PERFORM pg_advisory_xact_lock({CALENDAR_LOCK_NAMESPACE}, NEW.company_id);
        WITH RECURSIVE chain(id, depth) AS (
            SELECT NEW.parent_calendar_id, 0
            UNION ALL
            SELECT c.parent_calendar_id, chain.depth + 1
            FROM holiday_calendars c JOIN chain ON c.id = chain.id
            WHERE c.company_id = NEW.company_id AND c.parent_calendar_id IS NOT NULL AND chain.depth < 1000
        )
        SELECT 1 INTO hit FROM chain WHERE id = NEW.id LIMIT 1;
        IF hit IS NOT NULL THEN
            RAISE EXCEPTION 'Holiday calendar inheritance would create a cycle' USING ERRCODE = 'check_violation';
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


def _tenant_table(name: str, *columns, code_length: int = 30, name_length: int = 100):
    """Create a tenant table with code, name, status, company_id and the shared constraints."""
    op.create_table(
        name,
        sa.Column('code', sa.String(code_length), nullable=False),
        sa.Column('name', sa.String(name_length), nullable=False),
        *columns,
        sa.Column('status', sa.String(10), nullable=False, server_default='ACTIVE'),
        sa.Column('company_id', sa.Integer(), nullable=False),
        *_base(),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='RESTRICT'),
        sa.UniqueConstraint('company_id', 'id', name=f'uq_{name}_company_id_id'),
        sa.UniqueConstraint('company_id', 'code', name=f'uq_{name}_company_code'),
        sa.CheckConstraint(f"status IN {STATUSES}", name=f'ck_{name}_status'),
    )
    op.create_index(f'ix_{name}_company_id', name, ['company_id'])


def upgrade() -> None:
    bind = op.get_bind()
    for function in VALIDATORS:
        op.execute(function)

    # -- dimensions -------------------------------------------------------------------------
    _tenant_table(
        'work_week_patterns',
        sa.Column('weekly_off_days', postgresql.JSONB(), nullable=False),
        sa.Column('nth_weekday_offs', postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.create_check_constraint('ck_work_week_patterns_rules', 'work_week_patterns',
                               'work_week_rules_valid(weekly_off_days, nth_weekday_offs)')

    _tenant_table(
        'holiday_calendars',
        sa.Column('parent_calendar_id', sa.Integer(), nullable=True),
        sa.Column('country_code', sa.String(2), nullable=True),
        sa.Column('subdivision_code', sa.String(6), nullable=True),
        code_length=50, name_length=150,
    )
    op.create_foreign_key('fk_holiday_calendars_parent', 'holiday_calendars', 'holiday_calendars',
                          ['company_id', 'parent_calendar_id'], ['company_id', 'id'])
    op.create_index('ix_holiday_calendars_parent_calendar_id', 'holiday_calendars', ['parent_calendar_id'])
    op.create_check_constraint('ck_holiday_calendars_not_own_parent', 'holiday_calendars',
                               'parent_calendar_id IS NULL OR parent_calendar_id <> id')
    op.create_check_constraint('ck_holiday_calendars_country', 'holiday_calendars', "country_code ~ '^[A-Z]{2}$'")
    op.create_check_constraint(
        'ck_holiday_calendars_subdivision', 'holiday_calendars',
        "subdivision_code IS NULL OR (subdivision_code ~ '^[A-Z]{2}-[A-Z0-9]{1,3}$' "
        "AND (country_code IS NULL OR left(subdivision_code, 2) = country_code))",
    )

    _tenant_table(
        'grades',
        sa.Column('rank', sa.Integer(), nullable=True),
        sa.Column('salary_ranges', postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.create_check_constraint('ck_grades_salary_ranges', 'grades', 'salary_ranges_valid(salary_ranges)')

    _tenant_table('employment_types', sa.Column('worker_class', sa.String(12), nullable=False))
    op.create_check_constraint('ck_employment_types_worker_class', 'employment_types',
                               "worker_class IN ('EMPLOYEE', 'CONTRACTOR', 'INTERN', 'APPRENTICE')")

    _tenant_table('cost_centers', sa.Column('legal_entity_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_cost_centers_legal_entity', 'cost_centers', 'legal_entities',
                          ['company_id', 'legal_entity_id'], ['company_id', 'id'])
    op.create_index('ix_cost_centers_legal_entity_id', 'cost_centers', ['legal_entity_id'])

    _tenant_table('designations', sa.Column('grade_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_designations_grade', 'designations', 'grades', ['company_id', 'grade_id'], ['company_id', 'id'])

    _tenant_table(
        'pay_groups',
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('currency', sa.CHAR(3), nullable=False),
        sa.Column('pay_frequency', sa.String(15), nullable=False),
        sa.Column('calendar_rules', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('country_pack_code', sa.String(10), nullable=True),
        sa.Column('description', sa.String(500), nullable=True),
    )
    op.create_foreign_key('fk_pay_groups_legal_entity', 'pay_groups', 'legal_entities',
                          ['company_id', 'legal_entity_id'], ['company_id', 'id'])
    op.create_unique_constraint('uq_pay_groups_entity_id', 'pay_groups', ['company_id', 'legal_entity_id', 'id'])
    op.create_index('ix_pay_groups_legal_entity_id', 'pay_groups', ['legal_entity_id'])
    op.create_check_constraint('ck_pay_groups_frequency', 'pay_groups',
                               "pay_frequency IN ('MONTHLY', 'SEMI_MONTHLY', 'BI_WEEKLY', 'WEEKLY', 'DAILY')")
    op.create_check_constraint('ck_pay_groups_currency', 'pay_groups', "currency ~ '^[A-Z]{3}$'")
    op.create_check_constraint('ck_pay_groups_calendar_rules', 'pay_groups', "jsonb_typeof(calendar_rules) = 'object'")

    # -- existing tables ----------------------------------------------------------------------
    op.add_column('legal_entities', sa.Column('default_work_week_pattern_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_legal_entities_work_week', 'legal_entities', 'work_week_patterns',
                          ['company_id', 'default_work_week_pattern_id'], ['company_id', 'id'])

    op.add_column('branches', sa.Column('holiday_calendar_id', sa.Integer(), nullable=True))
    op.add_column('branches', sa.Column('work_week_pattern_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_branches_holiday_calendar', 'branches', 'holiday_calendars',
                          ['company_id', 'holiday_calendar_id'], ['company_id', 'id'])
    op.create_foreign_key('fk_branches_work_week', 'branches', 'work_week_patterns',
                          ['company_id', 'work_week_pattern_id'], ['company_id', 'id'])

    op.add_column('employee_assignments', sa.Column('pay_group_id', sa.Integer(), nullable=True))
    for column, _, _ in ASSIGNMENT_REFERENCES:
        op.add_column('employee_assignments', sa.Column(column, sa.Integer(), nullable=True))
    op.add_column('employee_assignments', sa.Column('fte', sa.Numeric(5, 4), nullable=False, server_default=sa.text('1')))
    op.create_check_constraint('ck_employee_assignments_fte', 'employee_assignments', 'fte > 0 AND fte <= 1')
    op.create_foreign_key('fk_employee_assignments_pay_group', 'employee_assignments', 'pay_groups',
                          ['company_id', 'legal_entity_id', 'pay_group_id'], ['company_id', 'legal_entity_id', 'id'])
    for column, constraint, table in ASSIGNMENT_REFERENCES:
        op.create_foreign_key(constraint, 'employee_assignments', table, ['company_id', column], ['company_id', 'id'])
    op.create_index('ix_employee_assignments_pay_group_id', 'employee_assignments', ['pay_group_id'])
    # Payroll tables reference a placement together with its company (composite foreign keys).
    op.create_unique_constraint('uq_employee_assignments_company_id_id', 'employee_assignments', ['company_id', 'id'])

    op.add_column('holidays', sa.Column('calendar_id', sa.Integer(), nullable=True))
    op.add_column('holidays', sa.Column('holiday_type', sa.String(10), nullable=False, server_default='COMPANY'))
    op.create_check_constraint('ck_holidays_type', 'holidays', "holiday_type IN ('PUBLIC', 'COMPANY', 'OPTIONAL')")
    op.create_foreign_key('fk_holidays_calendar', 'holidays', 'holiday_calendars',
                          ['company_id', 'calendar_id'], ['company_id', 'id'])
    op.create_index('ix_holidays_calendar_date', 'holidays', ['calendar_id', 'date'])

    op.add_column('companies', sa.Column('reporting_currency', sa.CHAR(3), nullable=True))
    op.add_column('companies', sa.Column('data_region', sa.String(20), nullable=True))
    op.add_column('companies', sa.Column('setup_completed_at', sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint('ck_companies_reporting_currency', 'companies', "reporting_currency ~ '^[A-Z]{3}$'")

    # Nullable and without a default: adding it rewrites no row, so the append-only trigger never fires.
    op.add_column('audit_logs', sa.Column('details', postgresql.JSONB(), nullable=True))

    # -- RBAC scopes ----------------------------------------------------------------------------
    op.drop_constraint('ck_role_permissions_scope', 'role_permissions', type_='check')
    op.create_check_constraint('ck_role_permissions_scope', 'role_permissions', f"scope IN {NEW_SCOPES}")
    op.drop_constraint('ck_role_assignment_targets_scope', 'user_role_assignment_targets', type_='check')
    op.create_check_constraint('ck_role_assignment_targets_scope', 'user_role_assignment_targets',
                               f"scope IN {NEW_TARGET_SCOPES}")

    _install_security(bind)
    summary = backfill(bind)
    logger.info("Payroll organisation backfill: %s", summary)


def _install_security(bind) -> None:
    for table in NEW_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")

    op.execute(GUARD_0008)
    op.execute(CALENDAR_CYCLE_GUARD)
    op.execute(
        "CREATE TRIGGER trg_holiday_calendars_prevent_cycle BEFORE INSERT OR UPDATE OF parent_calendar_id "
        "ON holiday_calendars FOR EACH ROW EXECUTE FUNCTION holiday_calendars_prevent_cycle()"
    )

    roles = [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"
    ))]
    for role in roles:
        quoted = '"' + role.replace('"', '""') + '"'
        for table in NEW_TABLES:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {quoted}")
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}")


def backfill(bind) -> dict:
    """Fill the new structures from today's data. Safe to run again: it only adds missing rows and
    fills NULL new columns, so a second run changes nothing (EC-MIG-14). Returns counts per step."""
    done = {}

    def run(step: str, sql: str, **params) -> None:
        done[step] = done.get(step, 0) + bind.execute(sa.text(sql), params).rowcount

    run("pay_groups", """
        INSERT INTO pay_groups (company_id, legal_entity_id, code, name, currency, pay_frequency, calendar_rules, status,
                                description, created_at, updated_at)
        SELECT le.company_id, le.id, le.group_code, 'Monthly', le.base_currency, 'MONTHLY', CAST(:rules AS jsonb),
               le.status, 'Created by migration 0008: monthly payroll in the legal entity''s base currency.', now(), now()
        FROM (
            SELECT e.company_id, e.id, e.base_currency, e.status,
                   CASE WHEN row_number() OVER (PARTITION BY e.company_id ORDER BY e.is_default DESC, e.id) = 1
                        THEN 'MONTHLY' ELSE 'MONTHLY-' || e.code END AS group_code
            FROM legal_entities e
        ) le
        WHERE NOT EXISTS (SELECT 1 FROM pay_groups g WHERE g.company_id = le.company_id AND g.legal_entity_id = le.id)
          AND NOT EXISTS (SELECT 1 FROM pay_groups g WHERE g.company_id = le.company_id AND g.code = le.group_code)
    """, rules=json.dumps(MONTHLY_RULES))
    # The entity's first active pay group (the one created above, unless an administrator made another).
    run("assignment_pay_groups", """
        UPDATE employee_assignments a SET pay_group_id = g.id
        FROM (
            SELECT DISTINCT ON (company_id, legal_entity_id) company_id, legal_entity_id, id
            FROM pay_groups ORDER BY company_id, legal_entity_id, (status = 'ACTIVE') DESC, id
        ) g
        WHERE a.is_primary AND a.pay_group_id IS NULL
          AND g.company_id = a.company_id AND g.legal_entity_id = a.legal_entity_id
    """)

    run("employment_types", """
        INSERT INTO employment_types (company_id, code, name, worker_class, status, created_at, updated_at)
        SELECT c.id, 'PERMANENT', 'Permanent', 'EMPLOYEE', 'ACTIVE', now(), now() FROM companies c
        WHERE NOT EXISTS (SELECT 1 FROM employment_types t WHERE t.company_id = c.id AND t.code = 'PERMANENT')
    """)
    run("assignment_employment_types", """
        UPDATE employee_assignments a SET employment_type_id = t.id
        FROM employment_types t
        WHERE a.employment_type_id IS NULL AND t.company_id = a.company_id AND t.code = 'PERMANENT'
    """)

    run("work_week_patterns", """
        INSERT INTO work_week_patterns (company_id, code, name, weekly_off_days, nth_weekday_offs, status,
                                        created_at, updated_at)
        SELECT c.id, 'MON_FRI', 'Monday to Friday', CAST('[5, 6]' AS jsonb), CAST('[]' AS jsonb), 'ACTIVE', now(), now()
        FROM companies c
        WHERE NOT EXISTS (SELECT 1 FROM work_week_patterns w WHERE w.company_id = c.id AND w.code = 'MON_FRI')
    """)
    run("entity_work_weeks", """
        UPDATE legal_entities le SET default_work_week_pattern_id = w.id
        FROM work_week_patterns w
        WHERE le.default_work_week_pattern_id IS NULL AND w.company_id = le.company_id AND w.code = 'MON_FRI'
    """)
    run("branch_work_weeks", """
        UPDATE branches b SET work_week_pattern_id = w.id
        FROM work_week_patterns w
        WHERE b.work_week_pattern_id IS NULL AND w.company_id = b.company_id AND w.code = 'MON_FRI'
    """)

    # Holidays not yet in a calendar were written from location/is_optional only.
    run("optional_holidays", """
        UPDATE holidays SET holiday_type = 'OPTIONAL'
        WHERE calendar_id IS NULL AND is_optional AND holiday_type <> 'OPTIONAL'
    """)
    run("holiday_calendars", """
        INSERT INTO holiday_calendars (company_id, code, name, status, created_at, updated_at)
        SELECT c.id, 'COMPANY', 'Company holidays', 'ACTIVE', now(), now() FROM companies c
        WHERE NOT EXISTS (SELECT 1 FROM holiday_calendars h WHERE h.company_id = c.id AND h.code = 'COMPANY')
    """)
    done["holiday_calendars"] += _branch_calendars(bind)
    run("calendar_holidays", """
        UPDATE holidays h SET calendar_id = b.holiday_calendar_id
        FROM branches b
        WHERE h.calendar_id IS NULL AND h.location IS NOT NULL
          AND b.company_id = h.company_id AND b.name = h.location AND b.holiday_calendar_id IS NOT NULL
    """)
    run("calendar_holidays", """
        UPDATE holidays h SET calendar_id = c.id
        FROM holiday_calendars c
        WHERE h.calendar_id IS NULL AND h.location IS NULL AND c.company_id = h.company_id AND c.code = 'COMPANY'
    """)
    unmatched = bind.execute(sa.text(
        "SELECT company_id, count(*) FROM holidays WHERE calendar_id IS NULL AND location IS NOT NULL "
        "GROUP BY company_id ORDER BY company_id"
    )).fetchall()
    for company_id, count in unmatched:
        logger.warning("Company %s: %s holiday(s) name a location that matches no branch; they are in the holiday "
                       "calendar UNMATCHED for review.", company_id, count)
    run("holiday_calendars", """
        INSERT INTO holiday_calendars (company_id, code, name, status, created_at, updated_at)
        SELECT DISTINCT h.company_id, 'UNMATCHED', 'Holidays without a matching branch', 'ACTIVE', now(), now()
        FROM holidays h
        WHERE h.calendar_id IS NULL AND h.location IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM holiday_calendars c WHERE c.company_id = h.company_id AND c.code = 'UNMATCHED')
    """)
    run("unmatched_holidays", """
        UPDATE holidays h SET calendar_id = c.id
        FROM holiday_calendars c
        WHERE h.calendar_id IS NULL AND h.location IS NOT NULL AND c.company_id = h.company_id AND c.code = 'UNMATCHED'
    """)

    # "Today" is the company's local date, computed here: this PostgreSQL build ships no time zone
    # files, so AT TIME ZONE / pg_timezone_names fail.
    for company_id, timezone in bind.execute(sa.text("SELECT id, timezone FROM companies ORDER BY id")).fetchall():
        run("assignment_shifts", """
            UPDATE employee_assignments a SET shift_id = e.shift_id
            FROM employees e
            WHERE a.company_id = :cid AND a.shift_id IS NULL AND a.is_primary
              AND e.company_id = a.company_id AND e.id = a.employee_id AND e.shift_id IS NOT NULL
              AND a.effective_from <= :today AND (a.effective_to IS NULL OR a.effective_to >= :today)
        """, cid=company_id, today=_local_today(timezone))
    return done


def _local_today(timezone) -> date:
    """The date in the company's timezone (UTC when the name is unknown), like company_service.today_for."""
    try:
        zone = pytz.timezone(timezone or "UTC")
    except pytz.UnknownTimeZoneError:
        zone = pytz.utc
    return datetime.now(pytz.utc).astimezone(zone).date()


def _branch_calendars(bind) -> int:
    """One calendar per branch without one, inheriting from the tenant's COMPANY calendar."""
    parents = dict(bind.execute(sa.text("SELECT company_id, id FROM holiday_calendars WHERE code = 'COMPANY'")).fetchall())
    taken = {(cid, code) for cid, code in bind.execute(sa.text("SELECT company_id, code FROM holiday_calendars"))}
    branches = bind.execute(sa.text(
        """
        SELECT b.id, b.company_id, b.code, b.name, b.status, b.entity_code, b.shared_code,
               CASE WHEN b.country_code ~ '^[A-Z]{2}$' THEN b.country_code END AS country_code,
               CASE WHEN b.country_code ~ '^[A-Z]{2}$' AND b.subdivision_code ~ '^[A-Z]{2}-[A-Z0-9]{1,3}$'
                         AND left(b.subdivision_code, 2) = b.country_code THEN b.subdivision_code END AS subdivision_code
        FROM (
            SELECT br.*, le.code AS entity_code,
                   count(*) OVER (PARTITION BY br.company_id, br.code) > 1 AS shared_code
            FROM branches br JOIN legal_entities le ON le.company_id = br.company_id AND le.id = br.legal_entity_id
        ) b
        WHERE b.holiday_calendar_id IS NULL
        ORDER BY b.company_id, b.id
        """
    )).fetchall()
    created = 0
    for branch in branches:
        base = f"BR-{branch.entity_code}-{branch.code}" if branch.shared_code else f"BR-{branch.code}"
        code, n = base, 1
        while (branch.company_id, code) in taken:
            n += 1
            code = f"{base}-{n}"
        taken.add((branch.company_id, code))
        calendar_id = bind.execute(sa.text(
            """
            INSERT INTO holiday_calendars (company_id, code, name, parent_calendar_id, country_code, subdivision_code,
                                           status, created_at, updated_at)
            VALUES (:cid, :code, :name, :parent, :country, :subdivision, :status, now(), now())
            RETURNING id
            """
        ), {"cid": branch.company_id, "code": code, "name": f"{branch.name} holidays"[:150],
            "parent": parents.get(branch.company_id), "country": branch.country_code,
            "subdivision": branch.subdivision_code, "status": branch.status}).scalar()
        bind.execute(sa.text("UPDATE branches SET holiday_calendar_id = :cal WHERE id = :id AND holiday_calendar_id IS NULL"),
                     {"cal": calendar_id, "id": branch.id})
        created += 1
    return created


def downgrade() -> None:
    op.execute(GUARD_0005)
    op.execute("DROP TRIGGER IF EXISTS trg_holiday_calendars_prevent_cycle ON holiday_calendars")
    op.execute("DROP FUNCTION IF EXISTS holiday_calendars_prevent_cycle()")

    op.execute("DELETE FROM user_role_assignment_targets WHERE scope = 'PAY_GROUP'")
    op.drop_constraint('ck_role_assignment_targets_scope', 'user_role_assignment_targets', type_='check')
    op.create_check_constraint('ck_role_assignment_targets_scope', 'user_role_assignment_targets',
                               f"scope IN {OLD_TARGET_SCOPES}")
    op.execute("DELETE FROM role_permissions WHERE scope = 'PAY_GROUP'")
    op.drop_constraint('ck_role_permissions_scope', 'role_permissions', type_='check')
    op.create_check_constraint('ck_role_permissions_scope', 'role_permissions', f"scope IN {OLD_SCOPES}")

    op.drop_column('audit_logs', 'details')
    op.drop_constraint('ck_companies_reporting_currency', 'companies', type_='check')
    for column in ('setup_completed_at', 'data_region', 'reporting_currency'):
        op.drop_column('companies', column)

    op.drop_index('ix_holidays_calendar_date', table_name='holidays')
    op.drop_constraint('fk_holidays_calendar', 'holidays', type_='foreignkey')
    op.drop_constraint('ck_holidays_type', 'holidays', type_='check')
    op.drop_column('holidays', 'holiday_type')
    op.drop_column('holidays', 'calendar_id')

    op.drop_constraint('uq_employee_assignments_company_id_id', 'employee_assignments', type_='unique')
    op.drop_index('ix_employee_assignments_pay_group_id', table_name='employee_assignments')
    op.drop_constraint('fk_employee_assignments_pay_group', 'employee_assignments', type_='foreignkey')
    for _column, constraint, _table in ASSIGNMENT_REFERENCES:
        op.drop_constraint(constraint, 'employee_assignments', type_='foreignkey')
    op.drop_constraint('ck_employee_assignments_fte', 'employee_assignments', type_='check')
    for column in ('fte', 'pay_group_id', *(column for column, _, _ in ASSIGNMENT_REFERENCES)):
        op.drop_column('employee_assignments', column)

    op.drop_constraint('fk_branches_work_week', 'branches', type_='foreignkey')
    op.drop_constraint('fk_branches_holiday_calendar', 'branches', type_='foreignkey')
    op.drop_column('branches', 'work_week_pattern_id')
    op.drop_column('branches', 'holiday_calendar_id')
    op.drop_constraint('fk_legal_entities_work_week', 'legal_entities', type_='foreignkey')
    op.drop_column('legal_entities', 'default_work_week_pattern_id')

    for table in reversed(NEW_TABLES):
        op.drop_table(table)
    op.execute("DROP FUNCTION IF EXISTS salary_ranges_valid(jsonb)")
    op.execute("DROP FUNCTION IF EXISTS work_week_rules_valid(jsonb, jsonb)")

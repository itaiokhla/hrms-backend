"""Organisation structure: partners, legal entities, branches, effective-dated assignments.

Additive and backwards compatible (02-multi-level-company §19, steps 1-2):
  * partners (commercial level) and nullable companies columns (partner_id,
    country_code, default_locale, brand kill switch);
  * tenant tables legal_entities, branches, employee_assignments and
    user_role_assignment_targets, each with row-level security and composite
    same-company foreign keys;
  * departments.parent_department_id (teams) with a cycle-rejecting trigger;
  * role_permissions accepts the LEGAL_ENTITY and BRANCH scopes;
  * payroll rows gain currency (backfilled from the company) and legal_entity_id.

Backfill (nothing existing is changed or deleted):
  * one default legal entity per company, named after the company, in its currency
    and timezone; the country is guessed from currency/timezone and marked
    unconfirmed;
  * one branch per distinct employees.location ("Main" for blanks);
  * one primary assignment per employee from hire_date (to termination_date when set);
  * payroll.currency = companies.currency, payroll.legal_entity_id = default entity.

Primary assignments of one employee may not overlap (trigger; this PostgreSQL build
has no btree_gist for an exclusion constraint).

Revision ID: 0005_org_structure
Revises: 0004_attendance_gps_tracking
Create Date: 2026-10-20
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0005_org_structure'
down_revision: Union[str, None] = '0004_attendance_gps_tracking'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
NEW_TABLES = ("legal_entities", "branches", "employee_assignments", "user_role_assignment_targets")
OLD_SCOPES = "('SELF', 'DIRECT_REPORTS', 'REPORTING_TREE', 'DEPARTMENT', 'ASSIGNED_GROUP', 'COMPANY')"
NEW_SCOPES = "('SELF', 'DIRECT_REPORTS', 'REPORTING_TREE', 'DEPARTMENT', 'ASSIGNED_GROUP', 'LEGAL_ENTITY', 'BRANCH', 'COMPANY')"

CURRENCY_COUNTRY = {"INR": "IN", "AED": "AE", "USD": "US", "GBP": "GB", "SGD": "SG", "AUD": "AU", "CAD": "CA",
                    "SAR": "SA", "QAR": "QA", "JPY": "JP", "CNY": "CN", "ZAR": "ZA", "NZD": "NZ", "CHF": "CH"}
TIMEZONE_COUNTRY = {"Asia/Kolkata": "IN", "Asia/Calcutta": "IN", "Asia/Dubai": "AE", "Europe/London": "GB",
                    "Europe/Berlin": "DE", "Europe/Paris": "FR", "Asia/Singapore": "SG"}


def _base():
    return [
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    ]


def upgrade() -> None:
    bind = op.get_bind()

    # -- commercial level -----------------------------------------------------------------
    op.create_table(
        'partners',
        sa.Column('name', sa.String(150), nullable=False),
        sa.Column('slug', sa.String(80), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='active'),
        sa.Column('billing_mode', sa.String(30), nullable=False, server_default='PLATFORM_BILLS_TENANT'),
        sa.Column('tenant_entitlements', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        *_base(),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('slug'),
        sa.CheckConstraint("status IN ('active', 'suspended', 'closed')", name='ck_partners_status'),
        sa.CheckConstraint("billing_mode IN ('PLATFORM_BILLS_TENANT', 'PARTNER_BILLS_TENANT')", name='ck_partners_billing_mode'),
    )
    op.add_column('companies', sa.Column('partner_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_companies_partner', 'companies', 'partners', ['partner_id'], ['id'], ondelete='RESTRICT')
    op.create_index('ix_companies_partner_id', 'companies', ['partner_id'])
    op.add_column('companies', sa.Column('country_code', sa.String(2), nullable=True))
    op.add_column('companies', sa.Column('default_locale', sa.String(35), nullable=True))
    op.add_column('companies', sa.Column('brand_disabled_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('companies', sa.Column('brand_disabled_reason', sa.String(255), nullable=True))

    # -- organisation ---------------------------------------------------------------------
    op.create_table(
        'legal_entities',
        sa.Column('code', sa.String(20), nullable=False),
        sa.Column('legal_name', sa.String(200), nullable=False),
        sa.Column('trade_name', sa.String(200), nullable=True),
        sa.Column('country_code', sa.String(2), nullable=False),
        sa.Column('country_confirmed', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('registered_address', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('registration_lines', postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('base_currency', sa.String(3), nullable=False),
        sa.Column('fiscal_year_start', sa.String(5), nullable=False, server_default='01-01'),
        sa.Column('timezone', sa.String(64), nullable=False),
        sa.Column('default_locale', sa.String(35), nullable=True),
        sa.Column('signatory_name', sa.String(150), nullable=True),
        sa.Column('signatory_title', sa.String(150), nullable=True),
        sa.Column('is_default', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('status', sa.String(10), nullable=False, server_default='ACTIVE'),
        sa.Column('effective_from', sa.Date(), nullable=True),
        sa.Column('effective_to', sa.Date(), nullable=True),
        sa.Column('company_id', sa.Integer(), nullable=False),
        *_base(),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='RESTRICT'),
        sa.UniqueConstraint('company_id', 'id', name='uq_legal_entities_company_id_id'),
        sa.UniqueConstraint('company_id', 'code', name='uq_legal_entities_company_code'),
        sa.CheckConstraint("status IN ('ACTIVE', 'INACTIVE')", name='ck_legal_entities_status'),
        sa.CheckConstraint("country_code ~ '^[A-Z]{2}$'", name='ck_legal_entities_country'),
        sa.CheckConstraint("base_currency ~ '^[A-Z]{3}$'", name='ck_legal_entities_currency'),
    )
    op.create_index('ix_legal_entities_company_id', 'legal_entities', ['company_id'])
    op.create_index('uq_legal_entities_one_default', 'legal_entities', ['company_id'], unique=True,
                    postgresql_where=sa.text('is_default'))

    op.create_table(
        'branches',
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(20), nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('address', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('country_code', sa.String(2), nullable=False),
        sa.Column('subdivision_code', sa.String(6), nullable=True),
        sa.Column('timezone', sa.String(64), nullable=False),
        sa.Column('status', sa.String(10), nullable=False, server_default='ACTIVE'),
        sa.Column('effective_from', sa.Date(), nullable=True),
        sa.Column('effective_to', sa.Date(), nullable=True),
        sa.Column('company_id', sa.Integer(), nullable=False),
        *_base(),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['company_id', 'legal_entity_id'], ['legal_entities.company_id', 'legal_entities.id'],
                                name='fk_branches_legal_entity'),
        sa.UniqueConstraint('company_id', 'id', name='uq_branches_company_id_id'),
        sa.UniqueConstraint('company_id', 'legal_entity_id', 'code', name='uq_branches_entity_code'),
        sa.UniqueConstraint('company_id', 'name', name='uq_branches_company_name'),
        sa.CheckConstraint("status IN ('ACTIVE', 'INACTIVE')", name='ck_branches_status'),
    )
    op.create_index('ix_branches_company_id', 'branches', ['company_id'])
    op.create_index('ix_branches_legal_entity_id', 'branches', ['legal_entity_id'])

    op.create_table(
        'employee_assignments',
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('branch_id', sa.Integer(), nullable=False),
        sa.Column('department_id', sa.Integer(), nullable=True),
        sa.Column('is_primary', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('reason', sa.String(20), nullable=False),
        sa.Column('note', sa.String(500), nullable=True),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('effective_to', sa.Date(), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('company_id', sa.Integer(), nullable=False),
        *_base(),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['company_id', 'employee_id'], ['employees.company_id', 'employees.id'],
                                ondelete='CASCADE', name='fk_employee_assignments_employee'),
        sa.ForeignKeyConstraint(['company_id', 'legal_entity_id'], ['legal_entities.company_id', 'legal_entities.id'],
                                name='fk_employee_assignments_entity'),
        sa.ForeignKeyConstraint(['company_id', 'branch_id'], ['branches.company_id', 'branches.id'],
                                name='fk_employee_assignments_branch'),
        sa.ForeignKeyConstraint(['company_id', 'department_id'], ['departments.company_id', 'departments.id'],
                                name='fk_employee_assignments_department'),
        sa.CheckConstraint(
            "reason IN ('HIRE', 'TRANSFER', 'PROMOTION', 'DEMOTION', 'REORG', 'SECONDMENT_START', 'SECONDMENT_END', "
            "'CONCURRENT_START', 'CONCURRENT_END', 'TERMINATION', 'REHIRE', 'CORRECTION', 'MIGRATION')",
            name='ck_employee_assignments_reason'),
        sa.CheckConstraint('effective_to IS NULL OR effective_to >= effective_from', name='ck_employee_assignments_window'),
    )
    for column in ('company_id', 'employee_id', 'legal_entity_id', 'branch_id'):
        op.create_index(f'ix_employee_assignments_{column}', 'employee_assignments', [column])
    op.create_index('ix_employee_assignments_current', 'employee_assignments', ['company_id', 'employee_id', 'effective_from'])

    op.create_unique_constraint('uq_user_role_assignments_company_id_id', 'user_role_assignments', ['company_id', 'id'])
    op.create_table(
        'user_role_assignment_targets',
        sa.Column('user_role_assignment_id', sa.Integer(), nullable=False),
        sa.Column('scope', sa.String(20), nullable=False),
        sa.Column('target_id', sa.Integer(), nullable=False),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('company_id', sa.Integer(), nullable=False),
        *_base(),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['company_id', 'user_role_assignment_id'],
                                ['user_role_assignments.company_id', 'user_role_assignments.id'],
                                ondelete='CASCADE', name='fk_role_assignment_targets_assignment'),
        sa.CheckConstraint("scope IN ('LEGAL_ENTITY', 'BRANCH')", name='ck_role_assignment_targets_scope'),
        sa.UniqueConstraint('user_role_assignment_id', 'scope', 'target_id', name='uq_role_assignment_target'),
    )
    op.create_index('ix_user_role_assignment_targets_company_id', 'user_role_assignment_targets', ['company_id'])
    op.create_index('ix_user_role_assignment_targets_user_role_assignment_id', 'user_role_assignment_targets',
                    ['user_role_assignment_id'])

    # -- departments become a tree (teams are child departments) ---------------------------
    op.add_column('departments', sa.Column('parent_department_id', sa.Integer(), nullable=True))
    op.create_index('ix_departments_parent_department_id', 'departments', ['parent_department_id'])
    op.create_foreign_key('fk_departments_parent', 'departments', 'departments',
                          ['company_id', 'parent_department_id'], ['company_id', 'id'])

    # -- RBAC scopes ------------------------------------------------------------------------
    op.drop_constraint('ck_role_permissions_scope', 'role_permissions', type_='check')
    op.create_check_constraint('ck_role_permissions_scope', 'role_permissions', f"scope IN {NEW_SCOPES}")

    # -- payroll currency -------------------------------------------------------------------
    op.add_column('payroll', sa.Column('currency', sa.String(3), nullable=True))
    op.add_column('payroll', sa.Column('legal_entity_id', sa.Integer(), nullable=True))
    op.create_index('ix_payroll_legal_entity_id', 'payroll', ['legal_entity_id'])

    _install_security(bind)
    _backfill(bind)

    op.alter_column('payroll', 'currency', nullable=False, server_default='INR')
    op.create_check_constraint('ck_payroll_currency', 'payroll', "currency ~ '^[A-Z]{3}$'")


def _install_security(bind) -> None:
    for table in NEW_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")

    op.execute(
        """
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
    )
    op.execute(
        "CREATE TRIGGER trg_employee_assignments_guard BEFORE INSERT OR UPDATE ON employee_assignments "
        "FOR EACH ROW EXECUTE FUNCTION employee_assignments_guard()"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION departments_prevent_cycle() RETURNS trigger AS $$
        DECLARE
            hit integer;
        BEGIN
            IF NEW.parent_department_id IS NULL THEN
                RETURN NEW;
            END IF;
            IF NEW.parent_department_id = NEW.id THEN
                RAISE EXCEPTION 'A department cannot be its own parent' USING ERRCODE = 'check_violation';
            END IF;
            PERFORM pg_advisory_xact_lock(4243, NEW.company_id);
            WITH RECURSIVE chain(id, depth) AS (
                SELECT NEW.parent_department_id, 0
                UNION ALL
                SELECT d.parent_department_id, c.depth + 1
                FROM departments d JOIN chain c ON d.id = c.id
                WHERE d.company_id = NEW.company_id AND d.parent_department_id IS NOT NULL AND c.depth < 1000
            )
            SELECT 1 INTO hit FROM chain WHERE id = NEW.id LIMIT 1;
            IF hit IS NOT NULL THEN
                RAISE EXCEPTION 'Department structure would create a cycle' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_departments_prevent_cycle BEFORE INSERT OR UPDATE OF parent_department_id ON departments "
        "FOR EACH ROW EXECUTE FUNCTION departments_prevent_cycle()"
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


def _backfill(bind) -> None:
    companies = bind.execute(sa.text("SELECT id, name, currency, timezone FROM companies ORDER BY id")).fetchall()
    for company_id, name, currency, timezone in companies:
        country = CURRENCY_COUNTRY.get(currency) or TIMEZONE_COUNTRY.get(timezone) or "IN"
        entity_id = bind.execute(sa.text(
            """
            INSERT INTO legal_entities (company_id, code, legal_name, country_code, country_confirmed, base_currency,
                                        timezone, is_default, status, created_at, updated_at)
            VALUES (:cid, 'LE01', :name, :country, false, :currency, :tz, true, 'ACTIVE', now(), now())
            RETURNING id
            """
        ), {"cid": company_id, "name": name[:200], "country": country, "currency": currency or "INR",
            "tz": timezone or "UTC"}).scalar()

        locations = [row[0] for row in bind.execute(sa.text(
            "SELECT DISTINCT btrim(location) FROM employees WHERE company_id = :cid "
            "AND location IS NOT NULL AND btrim(location) <> '' ORDER BY 1"
        ), {"cid": company_id})]
        branch_ids = {}
        names = list(locations)
        needs_main = bind.execute(sa.text(
            "SELECT count(*) FROM employees WHERE company_id = :cid AND (location IS NULL OR btrim(location) = '')"
        ), {"cid": company_id}).scalar() > 0 or not locations
        if needs_main and "Main" not in names:
            names.append("Main")
        for index, branch_name in enumerate(names, start=1):
            branch_ids[branch_name] = bind.execute(sa.text(
                """
                INSERT INTO branches (company_id, legal_entity_id, code, name, country_code, timezone, status,
                                      created_at, updated_at)
                VALUES (:cid, :le, :code, :name, :country, :tz, 'ACTIVE', now(), now())
                RETURNING id
                """
            ), {"cid": company_id, "le": entity_id, "code": f"B{index:02d}", "name": branch_name[:100],
                "country": country, "tz": timezone or "UTC"}).scalar()
        main_branch = branch_ids.get("Main") or branch_ids[names[0]]
        bind.execute(sa.text(
            """
            INSERT INTO employee_assignments (company_id, employee_id, legal_entity_id, branch_id, department_id,
                                              is_primary, reason, effective_from, effective_to, created_at, updated_at)
            SELECT e.company_id, e.id, :le, COALESCE(b.id, :main), e.department_id, true, 'MIGRATION',
                   e.hire_date,
                   CASE WHEN e.termination_date IS NOT NULL AND e.termination_date >= e.hire_date
                        THEN e.termination_date END,
                   now(), now()
            FROM employees e
            LEFT JOIN branches b ON b.company_id = e.company_id AND b.name = btrim(e.location)
            WHERE e.company_id = :cid
            """
        ), {"cid": company_id, "le": entity_id, "main": main_branch})
        bind.execute(sa.text(
            "UPDATE payroll SET currency = :currency, legal_entity_id = :le WHERE company_id = :cid"
        ), {"currency": currency or "INR", "le": entity_id, "cid": company_id})


def downgrade() -> None:
    op.drop_constraint('ck_payroll_currency', 'payroll', type_='check')
    op.drop_index('ix_payroll_legal_entity_id', table_name='payroll')
    op.drop_column('payroll', 'legal_entity_id')
    op.drop_column('payroll', 'currency')
    op.execute("DELETE FROM role_permissions WHERE scope IN ('LEGAL_ENTITY', 'BRANCH')")
    op.drop_constraint('ck_role_permissions_scope', 'role_permissions', type_='check')
    op.create_check_constraint('ck_role_permissions_scope', 'role_permissions', f"scope IN {OLD_SCOPES}")
    op.execute("DROP TRIGGER IF EXISTS trg_departments_prevent_cycle ON departments")
    op.execute("DROP FUNCTION IF EXISTS departments_prevent_cycle()")
    op.drop_constraint('fk_departments_parent', 'departments', type_='foreignkey')
    op.drop_index('ix_departments_parent_department_id', table_name='departments')
    op.drop_column('departments', 'parent_department_id')
    op.execute("DROP TRIGGER IF EXISTS trg_employee_assignments_guard ON employee_assignments")
    op.execute("DROP FUNCTION IF EXISTS employee_assignments_guard()")
    op.drop_table('user_role_assignment_targets')
    op.drop_constraint('uq_user_role_assignments_company_id_id', 'user_role_assignments', type_='unique')
    op.drop_table('employee_assignments')
    op.drop_table('branches')
    op.drop_table('legal_entities')
    for column in ('brand_disabled_reason', 'brand_disabled_at', 'default_locale', 'country_code'):
        op.drop_column('companies', column)
    op.drop_index('ix_companies_partner_id', table_name='companies')
    op.drop_constraint('fk_companies_partner', 'companies', type_='foreignkey')
    op.drop_column('companies', 'partner_id')
    op.drop_table('partners')

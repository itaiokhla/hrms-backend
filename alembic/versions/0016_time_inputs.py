"""Time inputs: half-day leave and leave_days, leave balance transactions, attendance early exit/late minutes, decimal shift duration, manual LOP inputs, unpaid suspensions, on-duty requests, optional holiday choices, source change log.

Additive and backwards compatible (3.4 §2-§8, §16; 3.1 §5.2-§5.4; audit 03 §8 items 1-8, 10 and 13):
  * leaves.number_of_days INTEGER -> NUMERIC(5,2) (whole values are unchanged); start_portion
    (FULL | SECOND_HALF) and end_portion (FULL | FIRST_HALF), never "second half to first half" of
    one day; cancellation_status (pending | approved | rejected) with its request/decision columns.
    ck_leaves_status keeps its four values;
  * leave_types gain counts_non_working_days (false), allow_half_day (true) and kind (COMP_OFF);
  * attendance gains late_minutes and early_exit_minutes (raw minutes against the session's shift,
    NULL = unknown, so history stays unknown), early_exit (false) and row_version (1);
  * shifts.duration_hours INTEGER -> NUMERIC(4,2) in (0, 24], and unpaid_break_minutes;
  * holidays: unique (company_id, id) for composite references, and one holiday of a name per date
    *and calendar* (two branches may observe a holiday of the same name) instead of per tenant;
  * tenant tables leave_days (a trigger keeps a full day and a half day of active leave apart),
    leave_balance_transactions and source_change_log (both append-only by trigger), lop_inputs,
    employee_suspensions, on_duty_requests and employee_optional_holidays, each with row-level
    security, grants and composite same-company foreign keys.

Backfill (idempotent): every approved leave gets one FULL leave_days row per calendar date (legacy
leave counted calendar days) with the leave type's paid flag as its pay treatment snapshot, and one
USE balance transaction in its start year, exactly what balances() counted before.

Downgrade drops the new tables, triggers and columns, rounds fractional leave days up and restores
the per-tenant holiday name constraint (it fails while two calendars share a holiday name and date).

Revision ID: 0016_time_inputs
Revises: 0015_compensation_v2
Create Date: 2026-10-21
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0016_time_inputs'
down_revision: Union[str, None] = '0015_compensation_v2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
NEW_TABLES = ("leave_days", "leave_balance_transactions", "lop_inputs", "employee_suspensions", "on_duty_requests",
              "employee_optional_holidays", "source_change_log")
APPEND_ONLY = ("leave_balance_transactions", "source_change_log")
# Frozen copies: later model changes must not alter what this revision does.
PORTIONS = "('FULL', 'FIRST_HALF', 'SECOND_HALF')"
INPUT_STATUSES = "('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')"
ACTIVE_INPUT = "status IN ('PENDING', 'APPROVED')"
CHANGE_RECORD_TYPES = ("('attendance', 'leave', 'holiday', 'lop_input', 'suspension', 'on_duty', 'optional_holiday', "
                       "'overtime', 'task', 'wage_input', 'assignment')")

LEAVE_DAYS_GUARD = """
CREATE OR REPLACE FUNCTION leave_days_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.voided_at IS NULL AND EXISTS (
        SELECT 1 FROM leave_days d
        WHERE d.company_id = NEW.company_id AND d.employee_id = NEW.employee_id AND d.date = NEW.date
          AND d.voided_at IS NULL AND d.id <> NEW.id AND (d.portion = 'FULL' OR NEW.portion = 'FULL')
    ) THEN
        RAISE EXCEPTION 'leave_days: % already has leave on %', NEW.employee_id, NEW.date
            USING ERRCODE = '23P01', CONSTRAINT = 'leave_days_one_leave_per_slot';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

APPEND_ONLY_GUARD = """
CREATE OR REPLACE FUNCTION payroll_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '42501';
END;
$$ LANGUAGE plpgsql;
"""


def _base():
    return [
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    ]


def _tenant(name: str, *, employee: bool = True, approval: bool = False):
    items = [
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='RESTRICT'),
        sa.UniqueConstraint('company_id', 'id', name=f'uq_{name}_company_id_id'),
    ]
    if employee:
        items.append(sa.ForeignKeyConstraint(['company_id', 'employee_id'], ['employees.company_id', 'employees.id'],
                                             name=f'fk_{name}_employee'))
    if approval:
        items.append(sa.ForeignKeyConstraint(['company_id', 'approval_request_id'],
                                             ['approval_requests.company_id', 'approval_requests.id'],
                                             name=f'fk_{name}_approval_request'))
    return items


def upgrade() -> None:
    bind = op.get_bind()

    # -- leaves and leave types -------------------------------------------------------------------
    op.alter_column('leaves', 'number_of_days', type_=sa.Numeric(5, 2), existing_type=sa.Integer(),
                    existing_nullable=False, postgresql_using='number_of_days::numeric(5,2)')
    op.add_column('leaves', sa.Column('start_portion', sa.String(12), nullable=False, server_default='FULL'))
    op.add_column('leaves', sa.Column('end_portion', sa.String(12), nullable=False, server_default='FULL'))
    op.add_column('leaves', sa.Column('cancellation_status', sa.String(12), nullable=True))
    op.add_column('leaves', sa.Column('cancellation_reason', sa.Text(), nullable=True))
    op.add_column('leaves', sa.Column('cancellation_requested_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('leaves', sa.Column('cancellation_decided_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('leaves', sa.Column('cancellation_decided_by_user_id', sa.Integer(), nullable=True))
    op.create_check_constraint('ck_leaves_start_portion', 'leaves', "start_portion IN ('FULL', 'SECOND_HALF')")
    op.create_check_constraint('ck_leaves_end_portion', 'leaves', "end_portion IN ('FULL', 'FIRST_HALF')")
    op.create_check_constraint(
        'ck_leaves_portions', 'leaves',
        "NOT (start_date = end_date AND start_portion = 'SECOND_HALF' AND end_portion = 'FIRST_HALF')",
    )
    op.create_check_constraint('ck_leaves_cancellation_status', 'leaves',
                               "cancellation_status IN ('pending', 'approved', 'rejected')")

    op.add_column('leave_types', sa.Column('counts_non_working_days', sa.Boolean(), nullable=False,
                                           server_default=sa.false()))
    op.add_column('leave_types', sa.Column('allow_half_day', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column('leave_types', sa.Column('kind', sa.String(20), nullable=True))
    op.create_check_constraint('ck_leave_types_kind', 'leave_types', "kind IN ('COMP_OFF')")

    # -- attendance and shifts ----------------------------------------------------------------------
    op.add_column('attendance', sa.Column('late_minutes', sa.Integer(), nullable=True))
    op.add_column('attendance', sa.Column('early_exit', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('attendance', sa.Column('early_exit_minutes', sa.Integer(), nullable=True))
    op.add_column('attendance', sa.Column('row_version', sa.Integer(), nullable=False, server_default='1'))
    op.create_check_constraint('ck_attendance_late_minutes', 'attendance', 'late_minutes IS NULL OR late_minutes >= 0')
    op.create_check_constraint('ck_attendance_early_exit_minutes', 'attendance',
                               'early_exit_minutes IS NULL OR early_exit_minutes >= 0')
    op.create_check_constraint('ck_attendance_early_exit', 'attendance', 'NOT early_exit OR early_exit_minutes > 0')
    op.create_check_constraint('ck_attendance_row_version', 'attendance', 'row_version >= 1')

    op.alter_column('shifts', 'duration_hours', type_=sa.Numeric(4, 2), existing_type=sa.Integer(),
                    existing_nullable=True, postgresql_using='duration_hours::numeric(4,2)')
    op.add_column('shifts', sa.Column('unpaid_break_minutes', sa.SmallInteger(), nullable=True))
    op.create_check_constraint('ck_shifts_duration', 'shifts',
                               'duration_hours IS NULL OR (duration_hours > 0 AND duration_hours <= 24)')
    op.create_check_constraint('ck_shifts_unpaid_break', 'shifts',
                               'unpaid_break_minutes IS NULL OR unpaid_break_minutes BETWEEN 0 AND 1440')

    # -- holidays -------------------------------------------------------------------------------------
    op.create_unique_constraint('uq_holidays_company_id_id', 'holidays', ['company_id', 'id'])
    op.drop_constraint('uq_holidays_company_date_name', 'holidays', type_='unique')
    op.execute("CREATE UNIQUE INDEX uq_holidays_calendar_date_name ON holidays "
               "(company_id, COALESCE(calendar_id, 0), date, lower(name))")

    # -- new tables -------------------------------------------------------------------------------------
    op.create_table(
        'leave_days',
        *_base(),
        sa.Column('leave_id', sa.Integer(), nullable=False),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('portion', sa.String(12), nullable=False, server_default='FULL'),
        sa.Column('units', sa.Numeric(3, 2), nullable=False),
        sa.Column('counts_against_balance', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('pay_treatment_snapshot', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('voided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('void_reason', sa.String(30), nullable=True),
        *_tenant('leave_days'),
        sa.ForeignKeyConstraint(['company_id', 'leave_id'], ['leaves.company_id', 'leaves.id'], name='fk_leave_days_leave'),
        sa.CheckConstraint(f"portion IN {PORTIONS}", name='ck_leave_days_portion'),
        sa.CheckConstraint("units IN (0.5, 1) AND (portion = 'FULL') = (units = 1)", name='ck_leave_days_units'),
        sa.CheckConstraint("jsonb_typeof(pay_treatment_snapshot) = 'object'", name='ck_leave_days_snapshot'),
    )
    op.create_index('ix_leave_days_company_id', 'leave_days', ['company_id'])
    op.create_index('ix_leave_days_leave_id', 'leave_days', ['leave_id'])
    op.create_index('ix_leave_days_employee_date', 'leave_days', ['company_id', 'employee_id', 'date'])
    op.create_index('uq_leave_days_active_slot', 'leave_days', ['company_id', 'employee_id', 'date', 'portion'],
                    unique=True, postgresql_where=sa.text('voided_at IS NULL'))

    op.create_table(
        'leave_balance_transactions',
        *_base(),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('leave_type', sa.String(30), nullable=False),
        sa.Column('delta', sa.Numeric(6, 2), nullable=False),
        sa.Column('reason', sa.String(10), nullable=False),
        sa.Column('ref_type', sa.String(30), nullable=True),
        sa.Column('ref_id', sa.Integer(), nullable=True),
        sa.Column('idempotency_key', sa.String(120), nullable=False),
        sa.Column('effective_date', sa.Date(), nullable=False),
        sa.Column('expires_on', sa.Date(), nullable=True),
        sa.Column('note', sa.String(500), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        *_tenant('leave_balance_transactions'),
        sa.UniqueConstraint('company_id', 'idempotency_key', name='uq_leave_balance_transactions_key'),
        sa.CheckConstraint("reason IN ('ACCRUAL', 'USE', 'ENCASH', 'ADJUST', 'EXPIRE')",
                           name='ck_leave_balance_transactions_reason'),
        sa.CheckConstraint('delta <> 0', name='ck_leave_balance_transactions_delta'),
    )
    op.create_index('ix_leave_balance_transactions_company_id', 'leave_balance_transactions', ['company_id'])
    op.create_index('ix_leave_balance_transactions_balance', 'leave_balance_transactions',
                    ['company_id', 'employee_id', 'leave_type', 'effective_date'])

    op.create_table(
        'lop_inputs',
        *_base(),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('portion', sa.String(12), nullable=False, server_default='FULL'),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('status', sa.String(10), nullable=False, server_default='PENDING'),
        sa.Column('approval_request_id', sa.Integer(), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cancelled_by_user_id', sa.Integer(), nullable=True),
        sa.Column('row_version', sa.Integer(), nullable=False, server_default='1'),
        *_tenant('lop_inputs', approval=True),
        sa.CheckConstraint(f"portion IN {PORTIONS}", name='ck_lop_inputs_portion'),
        sa.CheckConstraint(f"status IN {INPUT_STATUSES}", name='ck_lop_inputs_status'),
    )
    op.create_index('ix_lop_inputs_company_id', 'lop_inputs', ['company_id'])
    op.create_index('ix_lop_inputs_employee_date', 'lop_inputs', ['company_id', 'employee_id', 'date'])
    op.create_index('uq_lop_inputs_active_slot', 'lop_inputs', ['company_id', 'employee_id', 'date', 'portion'],
                    unique=True, postgresql_where=sa.text(ACTIVE_INPUT))

    op.create_table(
        'employee_suspensions',
        *_base(),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('end_date', sa.Date(), nullable=False),
        sa.Column('paid', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('status', sa.String(10), nullable=False, server_default='ACTIVE'),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cancelled_by_user_id', sa.Integer(), nullable=True),
        sa.Column('row_version', sa.Integer(), nullable=False, server_default='1'),
        *_tenant('employee_suspensions'),
        sa.CheckConstraint('end_date >= start_date', name='ck_employee_suspensions_dates'),
        sa.CheckConstraint("status IN ('ACTIVE', 'CANCELLED')", name='ck_employee_suspensions_status'),
    )
    op.create_index('ix_employee_suspensions_company_id', 'employee_suspensions', ['company_id'])
    op.create_index('ix_employee_suspensions_employee', 'employee_suspensions', ['company_id', 'employee_id', 'start_date'])

    op.create_table(
        'on_duty_requests',
        *_base(),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('portion', sa.String(12), nullable=False, server_default='FULL'),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('status', sa.String(10), nullable=False, server_default='PENDING'),
        sa.Column('approval_request_id', sa.Integer(), nullable=True),
        sa.Column('requested_by_user_id', sa.Integer(), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('row_version', sa.Integer(), nullable=False, server_default='1'),
        *_tenant('on_duty_requests', approval=True),
        sa.CheckConstraint(f"portion IN {PORTIONS}", name='ck_on_duty_requests_portion'),
        sa.CheckConstraint(f"status IN {INPUT_STATUSES}", name='ck_on_duty_requests_status'),
    )
    op.create_index('ix_on_duty_requests_company_id', 'on_duty_requests', ['company_id'])
    op.create_index('uq_on_duty_requests_active_slot', 'on_duty_requests', ['company_id', 'employee_id', 'date', 'portion'],
                    unique=True, postgresql_where=sa.text(ACTIVE_INPUT))

    op.create_table(
        'employee_optional_holidays',
        *_base(),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('holiday_id', sa.Integer(), nullable=False),
        sa.Column('year', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(10), nullable=False, server_default='CHOSEN'),
        sa.Column('chosen_by_user_id', sa.Integer(), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
        *_tenant('employee_optional_holidays'),
        sa.ForeignKeyConstraint(['company_id', 'holiday_id'], ['holidays.company_id', 'holidays.id'], ondelete='CASCADE',
                                name='fk_employee_optional_holidays_holiday'),
        sa.CheckConstraint("status IN ('CHOSEN', 'CANCELLED')", name='ck_employee_optional_holidays_status'),
    )
    op.create_index('ix_employee_optional_holidays_company_id', 'employee_optional_holidays', ['company_id'])
    op.create_index('ix_employee_optional_holidays_year', 'employee_optional_holidays', ['company_id', 'employee_id', 'year'])
    op.create_index('uq_employee_optional_holidays_chosen', 'employee_optional_holidays',
                    ['company_id', 'employee_id', 'holiday_id'], unique=True,
                    postgresql_where=sa.text("status = 'CHOSEN'"))

    op.create_table(
        'source_change_log',
        *_base(),
        sa.Column('record_type', sa.String(20), nullable=False),
        sa.Column('record_id', sa.Integer(), nullable=False),
        sa.Column('employee_id', sa.Integer(), nullable=True),
        sa.Column('date_from', sa.Date(), nullable=True),
        sa.Column('date_to', sa.Date(), nullable=True),
        sa.Column('change', sa.String(10), nullable=False),
        sa.Column('version_hash', sa.String(64), nullable=False),
        sa.Column('changed_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('actor_user_id', sa.Integer(), nullable=True),
        sa.Column('details', postgresql.JSONB(), nullable=True),
        *_tenant('source_change_log'),
        sa.CheckConstraint(f"record_type IN {CHANGE_RECORD_TYPES}", name='ck_source_change_log_record_type'),
        sa.CheckConstraint("change IN ('CREATED', 'UPDATED', 'VOIDED', 'DELETED')", name='ck_source_change_log_change'),
        sa.CheckConstraint("version_hash ~ '^[0-9a-f]{64}$'", name='ck_source_change_log_hash'),
        sa.CheckConstraint('date_to IS NULL OR date_from IS NULL OR date_to >= date_from',
                           name='ck_source_change_log_dates'),
    )
    op.create_index('ix_source_change_log_company_id', 'source_change_log', ['company_id'])
    op.create_index('ix_source_change_log_employee', 'source_change_log', ['company_id', 'employee_id', 'date_from'])
    op.create_index('ix_source_change_log_changed', 'source_change_log', ['company_id', 'changed_at'])
    op.create_index('ix_source_change_log_record', 'source_change_log', ['company_id', 'record_type', 'record_id'])

    backfill(bind)
    _install_security(bind)


def backfill(bind) -> dict:
    """Approved leave as leave_days and USE transactions; a second run adds nothing."""
    done = {}
    done["leave_days"] = bind.execute(sa.text("""
        INSERT INTO leave_days (company_id, leave_id, employee_id, date, portion, units, counts_against_balance,
                                pay_treatment_snapshot, created_at, updated_at)
        SELECT l.company_id, l.id, l.employee_id, d::date, 'FULL', 1, true,
               jsonb_build_object('pay_treatment', CASE WHEN COALESCE(t.is_paid, true) THEN 'FULL' ELSE 'UNPAID' END,
                                  'source', 'LEGACY_MIGRATION', 'leave_type_code', l.leave_type,
                                  'counts_non_working_days', true),
               now(), now()
        FROM leaves l
        LEFT JOIN leave_types t ON t.company_id = l.company_id AND t.code = l.leave_type
        CROSS JOIN LATERAL generate_series(l.start_date, l.end_date, interval '1 day') AS d
        WHERE l.status = 'approved' AND NOT EXISTS (SELECT 1 FROM leave_days x WHERE x.leave_id = l.id)
          AND NOT EXISTS (SELECT 1 FROM leave_days y WHERE y.company_id = l.company_id AND y.employee_id = l.employee_id
                          AND y.date = d::date AND y.voided_at IS NULL)
        ON CONFLICT DO NOTHING
    """)).rowcount
    done["balance_transactions"] = bind.execute(sa.text("""
        INSERT INTO leave_balance_transactions (company_id, employee_id, leave_type, delta, reason, ref_type, ref_id,
                                                idempotency_key, effective_date, note, created_at, updated_at)
        SELECT company_id, employee_id, leave_type, -number_of_days, 'USE', 'leave', id,
               'leave:' || id || ':use:' || extract(year FROM start_date)::int, start_date,
               'Approved before the balance ledger (migration 0016)', now(), now()
        FROM leaves WHERE status = 'approved' AND number_of_days > 0
        ON CONFLICT (company_id, idempotency_key) DO NOTHING
    """)).rowcount
    return done


def _install_security(bind) -> None:
    for table in NEW_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")
    op.execute(LEAVE_DAYS_GUARD)
    op.execute("CREATE TRIGGER trg_leave_days_guard BEFORE INSERT OR UPDATE OF portion, date, voided_at ON leave_days "
               "FOR EACH ROW EXECUTE FUNCTION leave_days_guard()")
    op.execute(APPEND_ONLY_GUARD)
    for table in APPEND_ONLY:
        op.execute(f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION payroll_append_only()")

    roles = [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"
    ))]
    for role in roles:
        quoted = '"' + role.replace('"', '""') + '"'
        for table in NEW_TABLES:
            privileges = "SELECT, INSERT" if table in APPEND_ONLY else "SELECT, INSERT, UPDATE, DELETE"
            op.execute(f"GRANT {privileges} ON {table} TO {quoted}")
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}")


def downgrade() -> None:
    for table in APPEND_ONLY:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
    op.execute("DROP TRIGGER IF EXISTS trg_leave_days_guard ON leave_days")
    for table in reversed(NEW_TABLES):
        op.drop_table(table)
    op.execute("DROP FUNCTION IF EXISTS payroll_append_only()")
    op.execute("DROP FUNCTION IF EXISTS leave_days_guard()")

    op.execute("DROP INDEX IF EXISTS uq_holidays_calendar_date_name")
    op.create_unique_constraint('uq_holidays_company_date_name', 'holidays', ['company_id', 'date', 'name'])
    op.drop_constraint('uq_holidays_company_id_id', 'holidays', type_='unique')

    op.drop_constraint('ck_shifts_unpaid_break', 'shifts', type_='check')
    op.drop_constraint('ck_shifts_duration', 'shifts', type_='check')
    op.drop_column('shifts', 'unpaid_break_minutes')
    op.alter_column('shifts', 'duration_hours', type_=sa.Integer(), existing_type=sa.Numeric(4, 2),
                    existing_nullable=True, postgresql_using='ceil(duration_hours)::integer')

    for name in ('ck_attendance_row_version', 'ck_attendance_early_exit', 'ck_attendance_early_exit_minutes',
                 'ck_attendance_late_minutes'):
        op.drop_constraint(name, 'attendance', type_='check')
    for column in ('row_version', 'early_exit_minutes', 'early_exit', 'late_minutes'):
        op.drop_column('attendance', column)

    op.drop_constraint('ck_leave_types_kind', 'leave_types', type_='check')
    for column in ('kind', 'allow_half_day', 'counts_non_working_days'):
        op.drop_column('leave_types', column)

    for name in ('ck_leaves_cancellation_status', 'ck_leaves_portions', 'ck_leaves_end_portion', 'ck_leaves_start_portion'):
        op.drop_constraint(name, 'leaves', type_='check')
    for column in ('cancellation_decided_by_user_id', 'cancellation_decided_at', 'cancellation_requested_at',
                   'cancellation_reason', 'cancellation_status', 'end_portion', 'start_portion'):
        op.drop_column('leaves', column)
    op.alter_column('leaves', 'number_of_days', type_=sa.Integer(), existing_type=sa.Numeric(5, 2),
                    existing_nullable=False, postgresql_using='ceil(number_of_days)::integer')

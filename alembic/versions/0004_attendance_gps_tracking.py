"""Attendance sessions with lunch breaks, an append-only event log and location samples.

Additive and backwards compatible:
  * attendance gains nullable lifecycle/tracking columns (tracking_state has a
    server default), so existing rows and readers are unaffected;
  * three new tenant tables with row-level security, composite same-company
    foreign keys and the database-level rules the service relies on:
      - at most one *live* session per employee (partial unique index),
      - one open break per session and no overlapping or out-of-session breaks
        (trigger; this PostgreSQL build has no btree_gist for an exclusion
        constraint),
      - attendance_events is append-only (trigger, like audit_logs),
      - location rows carry coordinates only when a reading was captured;
  * payroll gains nullable hour snapshot columns (existing runs stay NULL);
  * default roles receive the new location permissions, and every role that lets
    people mark their own attendance can also show them their own location data
    (other custom-role grants are left to each company's administrators).

Existing data: an employee may have several open records from earlier days
(no punch-out). Only the newest stays open; older ones are marked
closed_reason='SUPERSEDED_OPEN_RECORD' without changing any time or status,
so payroll and reports are unaffected and nothing is deleted.

Grants: roles that can already write `attendance` (the RLS-restricted app
role) receive the matching privileges on the new tables. Re-running
`python -m app.cli setup-db-roles` afterwards is still recommended.

Revision ID: 0004_attendance_gps_tracking
Revises: 0003_attendance_one_per_day
Create Date: 2026-10-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0004_attendance_gps_tracking'
down_revision: Union[str, None] = '0003_attendance_one_per_day'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
NEW_TABLES = ("attendance_breaks", "attendance_events", "location_samples")
LIVE = "check_out_time IS NULL AND closed_at IS NULL"

# Frozen copy of the registry change in this revision (2026.10.2).
DEFAULT_GRANTS = {
    "employee": [("location:read", "SELF")],
    "payroll_manager": [("location:read", "SELF")],
    "manager": [("location:read", "SELF"), ("location:read", "DIRECT_REPORTS"), ("location:review", "DIRECT_REPORTS")],
    "hr_admin": [("location:read", "SELF"), ("location:read", "COMPANY"), ("location:review", "COMPANY")],
}


def _timestamps():
    return [
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    bind = op.get_bind()

    # -- attendance: lifecycle and tracking --------------------------------------
    op.add_column('attendance', sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('attendance', sa.Column('closed_reason', sa.String(length=40), nullable=True))
    op.add_column('attendance', sa.Column('tracking_state', sa.String(length=20), nullable=False,
                                          server_default='NOT_APPLICABLE'))
    op.add_column('attendance', sa.Column('tracking_stop_reason', sa.String(length=40), nullable=True))
    op.add_column('attendance', sa.Column('tracking_interval_minutes', sa.Integer(), nullable=True))
    op.add_column('attendance', sa.Column('tracking_anchor_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('attendance', sa.Column('tracking_next_due_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('attendance', sa.Column('tracking_overdue_since', sa.DateTime(timezone=True), nullable=True))
    op.add_column('attendance', sa.Column('tracking_consent_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('attendance', sa.Column('last_location_at', sa.DateTime(timezone=True), nullable=True))

    superseded = bind.execute(sa.text(
        """
        UPDATE attendance a SET closed_at = now(), closed_reason = 'SUPERSEDED_OPEN_RECORD'
        WHERE a.check_out_time IS NULL AND a.closed_at IS NULL AND EXISTS (
            SELECT 1 FROM attendance newer
            WHERE newer.company_id = a.company_id AND newer.employee_id = a.employee_id
              AND newer.check_out_time IS NULL AND newer.closed_at IS NULL
              AND (newer.check_in_time, newer.id) > (a.check_in_time, a.id)
        )
        """
    )).rowcount
    if superseded:
        print(f"[0004] {superseded} older open attendance record(s) marked SUPERSEDED_OPEN_RECORD")

    op.create_check_constraint(
        'ck_attendance_tracking_state', 'attendance',
        "tracking_state IN ('NOT_APPLICABLE', 'ACTIVE', 'PAUSED', 'STOPPED')",
    )
    op.create_check_constraint('ck_attendance_closed_without_out', 'attendance', 'closed_at IS NULL OR check_out_time IS NULL')
    op.create_unique_constraint('uq_attendance_company_id_id', 'attendance', ['company_id', 'id'])
    op.create_index('uq_attendance_one_live_session', 'attendance', ['company_id', 'employee_id'], unique=True,
                    postgresql_where=sa.text(LIVE))
    op.create_index('ix_attendance_tracking_due', 'attendance', ['company_id', 'tracking_next_due_at'],
                    postgresql_where=sa.text("tracking_state = 'ACTIVE'"))

    # -- breaks -------------------------------------------------------------------
    op.create_table(
        'attendance_breaks',
        sa.Column('attendance_id', sa.Integer(), nullable=False),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_paid', sa.Boolean(), nullable=False),
        sa.Column('end_reason', sa.String(length=40), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('company_id', sa.Integer(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("kind IN ('LUNCH')", name='ck_breaks_kind'),
        sa.CheckConstraint('ended_at IS NULL OR ended_at >= started_at', name='ck_breaks_window'),
        sa.ForeignKeyConstraint(['company_id', 'attendance_id'], ['attendance.company_id', 'attendance.id'], name='fk_breaks_attendance'),
        sa.ForeignKeyConstraint(['company_id', 'employee_id'], ['employees.company_id', 'employees.id'], name='fk_breaks_employee'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_attendance_breaks_company_id'), 'attendance_breaks', ['company_id'])
    op.create_index('ix_attendance_breaks_session', 'attendance_breaks', ['company_id', 'attendance_id'])
    op.create_index('uq_attendance_breaks_one_open', 'attendance_breaks', ['attendance_id'], unique=True,
                    postgresql_where=sa.text('ended_at IS NULL'))

    # -- events (append-only) --------------------------------------------------------
    op.create_table(
        'attendance_events',
        sa.Column('attendance_id', sa.Integer(), nullable=False),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('event_type', sa.String(length=20), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('actor_user_id', sa.Integer(), nullable=True),
        sa.Column('idempotency_key', sa.String(length=64), nullable=True),
        sa.Column('face_match_score', sa.Float(), nullable=True),
        sa.Column('location_status', sa.String(length=20), nullable=False),
        sa.Column('note', sa.String(length=500), nullable=True),
        sa.Column('details', sa.Text(), nullable=True),
        sa.Column('company_id', sa.Integer(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("event_type IN ('PUNCH_IN', 'LUNCH_IN', 'LUNCH_OUT', 'PUNCH_OUT', 'SESSION_CLOSED', 'CORRECTION')",
                           name='ck_events_type'),
        sa.CheckConstraint("source IN ('self', 'kiosk', 'manual', 'system')", name='ck_events_source'),
        sa.CheckConstraint("location_status IN ('CAPTURED', 'FAILED', 'EXCEPTION', 'NOT_PROVIDED', 'NOT_REQUIRED', "
                           "'NOT_APPLICABLE')", name='ck_events_location_status'),
        sa.ForeignKeyConstraint(['company_id', 'attendance_id'], ['attendance.company_id', 'attendance.id'], name='fk_events_attendance'),
        sa.ForeignKeyConstraint(['company_id', 'employee_id'], ['employees.company_id', 'employees.id'], name='fk_events_employee'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('company_id', 'id', name='uq_attendance_events_company_id_id'),
    )
    op.create_index(op.f('ix_attendance_events_company_id'), 'attendance_events', ['company_id'])
    op.create_index('ix_attendance_events_session', 'attendance_events', ['company_id', 'attendance_id', 'occurred_at'])
    op.create_index('uq_attendance_events_idempotency', 'attendance_events', ['company_id', 'employee_id', 'idempotency_key'],
                    unique=True, postgresql_where=sa.text('idempotency_key IS NOT NULL'))

    # -- location samples ---------------------------------------------------------------
    op.create_table(
        'location_samples',
        sa.Column('attendance_id', sa.Integer(), nullable=False),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('event_id', sa.Integer(), nullable=True),
        sa.Column('sample_type', sa.String(length=20), nullable=False),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('latitude', sa.Float(), nullable=True),
        sa.Column('longitude', sa.Float(), nullable=True),
        sa.Column('accuracy_m', sa.Float(), nullable=True),
        sa.Column('captured_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('client_sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('fix_age_seconds', sa.Integer(), nullable=True),
        sa.Column('clock_skew_seconds', sa.Integer(), nullable=True),
        sa.Column('slot_due_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_recovery', sa.Boolean(), nullable=False),
        sa.Column('attempts', sa.SmallInteger(), nullable=True),
        sa.Column('accuracy_class', sa.String(length=20), nullable=True),
        sa.Column('movement_status', sa.String(length=30), nullable=True),
        sa.Column('distance_from_punch_in_m', sa.Float(), nullable=True),
        sa.Column('distance_from_previous_m', sa.Float(), nullable=True),
        sa.Column('uncertainty_m', sa.Float(), nullable=True),
        sa.Column('error_message', sa.String(length=200), nullable=True),
        sa.Column('exception_reason', sa.String(length=500), nullable=True),
        sa.Column('review_status', sa.String(length=20), nullable=True),
        sa.Column('reviewed_by_user_id', sa.Integer(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('review_note', sa.String(length=500), nullable=True),
        sa.Column('idempotency_key', sa.String(length=64), nullable=True),
        sa.Column('redacted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('company_id', sa.Integer(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("sample_type IN ('PUNCH_IN', 'LUNCH_IN', 'LUNCH_OUT', 'PUNCH_OUT', 'PERIODIC')", name='ck_samples_type'),
        sa.CheckConstraint("status IN ('CAPTURED', 'PERMISSION_DENIED', 'POSITION_UNAVAILABLE', 'TIMEOUT', 'INSECURE_CONTEXT', "
                           "'UNSUPPORTED', 'ERROR', 'MISSED')", name='ck_samples_status'),
        sa.CheckConstraint('latitude IS NULL OR latitude BETWEEN -90 AND 90', name='ck_samples_latitude'),
        sa.CheckConstraint('longitude IS NULL OR longitude BETWEEN -180 AND 180', name='ck_samples_longitude'),
        sa.CheckConstraint('accuracy_m IS NULL OR accuracy_m > 0', name='ck_samples_accuracy'),
        sa.CheckConstraint("status = 'CAPTURED' OR (latitude IS NULL AND longitude IS NULL)", name='ck_samples_no_position_on_failure'),
        sa.CheckConstraint("status <> 'CAPTURED' OR redacted_at IS NOT NULL OR "
                           "(latitude IS NOT NULL AND longitude IS NOT NULL AND accuracy_m IS NOT NULL)",
                           name='ck_samples_captured_has_position'),
        sa.CheckConstraint("accuracy_class IS NULL OR accuracy_class IN ('PREFERRED', 'ACCEPTABLE', 'LOW')", name='ck_samples_accuracy_class'),
        sa.CheckConstraint("movement_status IS NULL OR movement_status IN ('BASELINE', 'WITHIN_RANGE', 'UNCERTAIN', "
                           "'LOW_ACCURACY', 'SIGNIFICANT_MOVEMENT_REVIEW')", name='ck_samples_movement'),
        sa.CheckConstraint("review_status IS NULL OR review_status IN ('PENDING', 'APPROVED', 'REJECTED', 'ACKNOWLEDGED')",
                           name='ck_samples_review'),
        sa.ForeignKeyConstraint(['company_id', 'attendance_id'], ['attendance.company_id', 'attendance.id'], name='fk_samples_attendance'),
        sa.ForeignKeyConstraint(['company_id', 'employee_id'], ['employees.company_id', 'employees.id'], name='fk_samples_employee'),
        sa.ForeignKeyConstraint(['company_id', 'event_id'], ['attendance_events.company_id', 'attendance_events.id'], name='fk_samples_event'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_location_samples_company_id'), 'location_samples', ['company_id'])
    op.create_index('ix_location_samples_session', 'location_samples', ['company_id', 'attendance_id', 'received_at'])
    op.create_index('ix_location_samples_employee', 'location_samples', ['company_id', 'employee_id', 'received_at'])
    op.create_index('ix_location_samples_review', 'location_samples', ['company_id', 'review_status'],
                    postgresql_where=sa.text("review_status = 'PENDING'"))
    op.create_index('uq_location_samples_idempotency', 'location_samples', ['company_id', 'employee_id', 'idempotency_key'],
                    unique=True, postgresql_where=sa.text('idempotency_key IS NOT NULL'))
    op.create_index('uq_location_samples_slot', 'location_samples', ['attendance_id', 'slot_due_at'], unique=True,
                    postgresql_where=sa.text("sample_type = 'PERIODIC' AND status IN ('CAPTURED', 'MISSED')"))
    op.create_index('uq_location_samples_event', 'location_samples', ['event_id'], unique=True,
                    postgresql_where=sa.text('event_id IS NOT NULL'))

    # -- payroll hour snapshot ----------------------------------------------------------------
    for column in ('gross_work_seconds', 'break_seconds', 'net_work_seconds', 'incomplete_sessions'):
        op.add_column('payroll', sa.Column(column, sa.Integer(), nullable=True))

    _install_security(bind)
    _grant_default_roles(bind)


def _install_security(bind) -> None:
    for table in NEW_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION attendance_events_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'attendance_events is append-only' USING ERRCODE = 'insufficient_privilege';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_attendance_events_immutable BEFORE UPDATE OR DELETE ON attendance_events "
        "FOR EACH ROW EXECUTE FUNCTION attendance_events_immutable()"
    )

    # Breaks must sit inside their session and never overlap. The parent row is
    # locked first, so two concurrent writers on one session are serialised.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION attendance_breaks_guard() RETURNS trigger AS $$
        DECLARE
            session record;
        BEGIN
            SELECT employee_id, check_in_time, check_out_time INTO session
            FROM attendance WHERE id = NEW.attendance_id AND company_id = NEW.company_id FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Break must belong to a session of the same company'
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            IF session.employee_id <> NEW.employee_id THEN
                RAISE EXCEPTION 'Break employee does not match its session' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.started_at < session.check_in_time THEN
                RAISE EXCEPTION 'Break starts before the punch-in' USING ERRCODE = 'check_violation';
            END IF;
            IF session.check_out_time IS NOT NULL
               AND (NEW.ended_at IS NULL OR NEW.ended_at > session.check_out_time) THEN
                RAISE EXCEPTION 'Break must end by the punch-out' USING ERRCODE = 'check_violation';
            END IF;
            IF EXISTS (
                SELECT 1 FROM attendance_breaks b
                WHERE b.attendance_id = NEW.attendance_id AND b.id <> NEW.id
                  AND tstzrange(b.started_at, COALESCE(b.ended_at, 'infinity'::timestamptz), '[)')
                      && tstzrange(NEW.started_at, COALESCE(NEW.ended_at, 'infinity'::timestamptz), '[)')
            ) THEN
                RAISE EXCEPTION 'Breaks in one session cannot overlap' USING ERRCODE = 'exclusion_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_attendance_breaks_guard BEFORE INSERT OR UPDATE ON attendance_breaks "
        "FOR EACH ROW EXECUTE FUNCTION attendance_breaks_guard()"
    )
    # The same containment rule when a correction moves the session itself.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION attendance_session_bounds_guard() RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM attendance_breaks b
                WHERE b.attendance_id = NEW.id AND (
                    b.started_at < NEW.check_in_time
                    OR (NEW.check_out_time IS NOT NULL AND (b.ended_at IS NULL OR b.ended_at > NEW.check_out_time))
                )
            ) THEN
                RAISE EXCEPTION 'Lunch breaks must stay within the attendance session' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_attendance_session_bounds BEFORE UPDATE OF check_in_time, check_out_time ON attendance "
        "FOR EACH ROW EXECUTE FUNCTION attendance_session_bounds_guard()"
    )

    # Give roles that can already write attendance (the RLS-restricted app role) the same reach.
    roles = [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'attendance' AND privilege_type = 'INSERT' AND grantee <> current_user"
    ))]
    for role in roles:
        quoted = '"' + role.replace('"', '""') + '"'
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON attendance_breaks, location_samples TO {quoted}")
        op.execute(f"GRANT SELECT, INSERT ON attendance_events TO {quoted}")
        for table in NEW_TABLES:
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}")


def _grant_default_roles(bind) -> None:
    # Anyone who may mark their own attendance may also see their own location data. This
    # also keeps custom roles built from the old self-service set equivalent to the updated
    # Employee role, so people who could assign that role still can (delegation needs every grant).
    bind.execute(sa.text(
        """
        INSERT INTO role_permissions (company_id, role_id, permission_code, scope, created_at, updated_at)
        SELECT rp.company_id, rp.role_id, 'location:read', 'SELF', now(), now() FROM role_permissions rp
        WHERE rp.permission_code = 'attendance:mark' AND rp.scope = 'SELF' AND NOT EXISTS (
            SELECT 1 FROM role_permissions x
            WHERE x.role_id = rp.role_id AND x.permission_code = 'location:read' AND x.scope = 'SELF'
        )
        """
    ))
    for system_key, grants in DEFAULT_GRANTS.items():
        for code, scope in grants:
            bind.execute(sa.text(
                """
                INSERT INTO role_permissions (company_id, role_id, permission_code, scope, created_at, updated_at)
                SELECT r.company_id, r.id, :code, :scope, now(), now() FROM roles r
                WHERE r.system_key = :key AND NOT EXISTS (
                    SELECT 1 FROM role_permissions rp
                    WHERE rp.role_id = r.id AND rp.permission_code = :code AND rp.scope = :scope
                )
                """
            ), {"code": code, "scope": scope, "key": system_key})


def downgrade() -> None:
    op.execute("DELETE FROM role_permissions WHERE permission_code IN ('location:read', 'location:review')")
    for column in ('incomplete_sessions', 'net_work_seconds', 'break_seconds', 'gross_work_seconds'):
        op.drop_column('payroll', column)
    op.execute("DROP TRIGGER IF EXISTS trg_attendance_session_bounds ON attendance")
    op.execute("DROP FUNCTION IF EXISTS attendance_session_bounds_guard()")
    op.execute("DROP TRIGGER IF EXISTS trg_attendance_breaks_guard ON attendance_breaks")
    op.execute("DROP FUNCTION IF EXISTS attendance_breaks_guard()")
    op.execute("DROP TRIGGER IF EXISTS trg_attendance_events_immutable ON attendance_events")
    op.execute("DROP FUNCTION IF EXISTS attendance_events_immutable()")
    op.drop_table('location_samples')
    op.drop_table('attendance_events')
    op.drop_table('attendance_breaks')
    op.drop_index('ix_attendance_tracking_due', table_name='attendance')
    op.drop_index('uq_attendance_one_live_session', table_name='attendance')
    op.drop_constraint('uq_attendance_company_id_id', 'attendance', type_='unique')
    op.drop_constraint('ck_attendance_closed_without_out', 'attendance', type_='check')
    op.drop_constraint('ck_attendance_tracking_state', 'attendance', type_='check')
    for column in ('last_location_at', 'tracking_consent_at', 'tracking_overdue_since', 'tracking_next_due_at',
                   'tracking_anchor_at', 'tracking_interval_minutes', 'tracking_stop_reason', 'tracking_state',
                   'closed_reason', 'closed_at'):
        op.drop_column('attendance', column)

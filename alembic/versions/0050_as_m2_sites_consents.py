"""AS-M2 capture v2, part 2: sites, consents, policy documents, face-data columns and the capture-side permission codes
(spine §3.5, §3.12, §7.1; AS 11 §3.4, §3.8, §4.6, §4.12, §11.2 note C-3; SP-AS-169, -219, -220).

NEW tenant tables (RLS ``tenant_isolation``, unique ``(company_id, id)``, composite same-company foreign keys, grants;
app/models/attendance.py and app/models/rosters.py document them):

* ``attendance_sites`` (circle / polygon / none, IP ranges ``CIDR[]``, QR secret hash; DB CHECK ``radius_m >= 25``,
  the service enforces ``attendance.geofence.min_radius_m``); the deferred foreign keys ``attendance_punches.site_id``
  and ``attendance_devices.site_id`` (AS 11 §11.3) are added;
* ``attendance_consents`` (append-only ledger: trigger ``attendance_consents_guard`` refuses UPDATE / DELETE, the app
  role holds SELECT, INSERT; the latest row per employee and purpose is the state; ``evidence_attachment_id`` arrives
  with as_m4_requests, A-19);
* ``attendance_policy_documents`` (``document_attachment_id`` is a plain column, its foreign key arrives with
  as_m4_requests, Q-11-04) and ``attendance_policy_acknowledgements`` (append-only, one per document and employee);
* ``roster_consent_conflicts`` — the pending item ``roster_consent_conflict`` and grid badge of EC AS-ROS-06 (a
  published future entry that a consent change made invalid; never withdrawn automatically).

Columns: ``location_samples`` + ``punch_id``, ``site_id``, ``distance_to_site_m``, ``geofence_result``,
``mock_location``; ``face_encodings`` + ``encoding_enc``, ``key_id``, ``consent_id``, ``retention_until``,
``deleted_at``, ``legacy_enrolled`` (SP-AS-219), ``legacy_grace_until``; ``face_encodings.encoding`` becomes nullable
(new enrolments are stored encrypted only; the encryption of existing rows is the ``attendance.v2_prepare`` face step,
never this revision). Data: every existing encoding is marked ``legacy_enrolled`` with a grace end of today + 30 days
(``attendance.privacy.legacy_face_grace_days``, 07 §5.5) so v1 tenants see no change on day one (R-AS-104).
``tenant_data_keys.purpose`` accepts ``ATTENDANCE_BIOMETRIC`` (SP-AS-011).

Permissions (registry 2026.10.8): ``attendance:review``, ``attendance:evidence:read`` (explicit-only, no default
grant), ``attendance:manage_devices``, ``attendance:manage_sites``, ``attendance:manage_policy``; default grants seeded
by ``system_key`` (``hr_admin`` COMPANY; ``hr_manager`` LEGAL_ENTITY and ``hr_executive`` BRANCH when those roles
exist); holders' ``permission_version`` is bumped.

Scheduled rows: ``attendance.consent_expire`` (daily-local 00:30) and ``attendance.location_sweep`` (hourly).

Downgrade refuses while face encodings exist only as ciphertext unless the master key can decrypt them back (the 0026
pattern), then drops everything above.

Revision ID: 0050_as_m2_sites_consents
Revises: 0049_as_m2_punches_sessions
Create Date: 2026-10-28
"""
import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0050_as_m2_sites_consents"
down_revision: Union[str, None] = "0049_as_m2_punches_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
BASE = ("id SERIAL PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE RESTRICT, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()")
LEGACY_FACE_GRACE_DAYS = 30

# Frozen copies of the vocabularies (spine §3.13 at this revision).
SITE_KINDS = ("OFFICE", "PLANT", "STORE", "CLINIC", "WAREHOUSE", "CLIENT_SITE", "PROJECT_SITE", "OTHER")
GEOMETRY_TYPES = ("CIRCLE", "POLYGON", "NONE")
SITE_STATUSES = ("ACTIVE", "ARCHIVED")
GEOFENCE_RESULTS = ("INSIDE", "OUTSIDE", "UNKNOWN", "NOT_CHECKED")
CONSENT_PURPOSES = ("BIOMETRIC_FACE", "BIOMETRIC_DEVICE", "PUNCH_PHOTO", "LOCATION_AT_PUNCH", "LOCATION_TRACKING",
                    "NIGHT_WORK", "OVERTIME", "COMPRESSED_WEEK", "REST_WAIVER", "SCHEDULE_CHANGE_ACCEPTED",
                    "WEEKLY_HOURS_OPT_OUT")
CONSENT_SCOPES = ("STANDING", "PER_INSTANCE")
LEGAL_BASES = ("CONSENT", "EMPLOYMENT_LEGITIMATE_USE", "LEGAL_OBLIGATION", "CONTRACT")
CONSENT_STATUSES = ("GRANTED", "DECLINED", "WITHDRAWN", "EXPIRED")
CONSENT_CHANNELS = ("SELF_SERVICE", "HR_RECORDED")
POLICY_DOCUMENT_STATUSES = ("DRAFT", "PUBLISHED", "RETIRED")
CONFLICT_STATUSES = ("OPEN", "RESOLVED")
KEY_PURPOSES_BEFORE = ("PAYROLL_PII", "MFA_SECRET")
KEY_PURPOSES_AFTER = KEY_PURPOSES_BEFORE + ("ATTENDANCE_BIOMETRIC",)

# Default grants by system_key (spine §7.1 table: HA = hr_admin, HM = hr_manager, HE = hr_executive; SP-AS-220).
NEW_GRANTS = {
    "hr_admin": (("attendance:review", "COMPANY"), ("attendance:manage_devices", "COMPANY"),
                 ("attendance:manage_sites", "COMPANY"), ("attendance:manage_policy", "COMPANY")),
    "hr_manager": (("attendance:manage_policy", "LEGAL_ENTITY"),),
    "hr_executive": (("attendance:review", "BRANCH"), ("attendance:manage_sites", "BRANCH")),
}
NEW_CODES = ("attendance:review", "attendance:evidence:read", "attendance:manage_devices", "attendance:manage_sites",
             "attendance:manage_policy")
SCHEDULED = (("attendance.consent_expire", "HOURLY", 17, "00:30"), ("attendance.location_sweep", "HOURLY", 22, None))


def _in(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _fk(table: str, column: str, target: str, name: str, ondelete: str = "RESTRICT") -> str:
    return (f"CONSTRAINT fk_{table}_{name} FOREIGN KEY (company_id, {column}) "
            f"REFERENCES {target} (company_id, id) ON DELETE {ondelete}")


TABLES = {
    "attendance_sites": f"""
        {BASE}, code VARCHAR(30) NOT NULL, name VARCHAR(100) NOT NULL, kind VARCHAR(14) NOT NULL,
        legal_entity_id INTEGER, branch_id INTEGER, timezone VARCHAR(64), geometry_type VARCHAR(8) NOT NULL,
        center_lat NUMERIC(9,6), center_lng NUMERIC(9,6), radius_m INTEGER, polygon JSONB, ip_ranges CIDR[],
        qr_secret_hash CHAR(64), outdoor BOOLEAN NOT NULL DEFAULT false, status VARCHAR(10) NOT NULL DEFAULT 'ACTIVE',
        effective_from DATE NOT NULL, effective_to DATE, created_by_user_id INTEGER,
        row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_attendance_sites_company_id_id UNIQUE (company_id, id),
        CONSTRAINT uq_attendance_sites_code UNIQUE (company_id, code),
        {_fk('attendance_sites', 'legal_entity_id', 'legal_entities', 'legal_entity')},
        {_fk('attendance_sites', 'branch_id', 'branches', 'branch')},
        {_fk('attendance_sites', 'created_by_user_id', 'users', 'created_by')},
        CONSTRAINT ck_attendance_sites_kind CHECK (kind IN {_in(SITE_KINDS)}),
        CONSTRAINT ck_attendance_sites_geometry CHECK (geometry_type IN {_in(GEOMETRY_TYPES)}),
        CONSTRAINT ck_attendance_sites_status CHECK (status IN {_in(SITE_STATUSES)}),
        CONSTRAINT ck_attendance_sites_lat CHECK (center_lat IS NULL OR center_lat BETWEEN -90 AND 90),
        CONSTRAINT ck_attendance_sites_lng CHECK (center_lng IS NULL OR center_lng BETWEEN -180 AND 180),
        CONSTRAINT ck_attendance_sites_radius CHECK (radius_m IS NULL OR radius_m >= 25),
        CONSTRAINT ck_attendance_sites_circle CHECK (geometry_type <> 'CIRCLE'
            OR (center_lat IS NOT NULL AND center_lng IS NOT NULL AND radius_m IS NOT NULL)),
        CONSTRAINT ck_attendance_sites_polygon CHECK (geometry_type <> 'POLYGON' OR polygon IS NOT NULL),
        CONSTRAINT ck_attendance_sites_qr CHECK (qr_secret_hash IS NULL OR qr_secret_hash ~ '^[0-9a-f]{{64}}$'),
        CONSTRAINT ck_attendance_sites_dates CHECK (effective_to IS NULL OR effective_to >= effective_from),
        CONSTRAINT ck_attendance_sites_row_version CHECK (row_version >= 1)""",
    "attendance_consents": f"""
        {BASE}, employee_id INTEGER NOT NULL, purpose VARCHAR(28) NOT NULL,
        scope VARCHAR(14) NOT NULL DEFAULT 'STANDING', subject_ref JSONB, legal_basis VARCHAR(28) NOT NULL,
        notice_version VARCHAR(32) NOT NULL, status VARCHAR(10) NOT NULL, valid_from DATE NOT NULL, valid_to DATE,
        notice_period_days SMALLINT, channel VARCHAR(14) NOT NULL, recorded_by_user_id INTEGER,
        CONSTRAINT uq_attendance_consents_company_id_id UNIQUE (company_id, id),
        {_fk('attendance_consents', 'employee_id', 'employees', 'employee')},
        {_fk('attendance_consents', 'recorded_by_user_id', 'users', 'recorded_by')},
        CONSTRAINT ck_attendance_consents_purpose CHECK (purpose IN {_in(CONSENT_PURPOSES)}),
        CONSTRAINT ck_attendance_consents_scope CHECK (scope IN {_in(CONSENT_SCOPES)}),
        CONSTRAINT ck_attendance_consents_subject CHECK ((scope = 'PER_INSTANCE') = (subject_ref IS NOT NULL)),
        CONSTRAINT ck_attendance_consents_basis CHECK (legal_basis IN {_in(LEGAL_BASES)}),
        CONSTRAINT ck_attendance_consents_status CHECK (status IN {_in(CONSENT_STATUSES)}),
        CONSTRAINT ck_attendance_consents_channel CHECK (channel IN {_in(CONSENT_CHANNELS)}),
        CONSTRAINT ck_attendance_consents_hr CHECK (channel <> 'HR_RECORDED' OR recorded_by_user_id IS NOT NULL),
        CONSTRAINT ck_attendance_consents_dates CHECK (valid_to IS NULL OR valid_to >= valid_from),
        CONSTRAINT ck_attendance_consents_notice CHECK (notice_period_days IS NULL OR notice_period_days >= 0)""",
    "attendance_policy_documents": f"""
        {BASE}, code VARCHAR(40) NOT NULL, version VARCHAR(16) NOT NULL, title VARCHAR(150) NOT NULL,
        legal_entity_id INTEGER, document_attachment_id INTEGER, effective_from DATE NOT NULL,
        requires_acknowledgement BOOLEAN NOT NULL DEFAULT false, purposes TEXT[] NOT NULL DEFAULT '{{}}'::text[],
        summary VARCHAR(2000), privacy_contact VARCHAR(200), status VARCHAR(10) NOT NULL DEFAULT 'DRAFT',
        published_at TIMESTAMPTZ, row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_attendance_policy_documents_company_id_id UNIQUE (company_id, id),
        CONSTRAINT uq_attendance_policy_documents_version UNIQUE (company_id, code, version),
        {_fk('attendance_policy_documents', 'legal_entity_id', 'legal_entities', 'legal_entity')},
        CONSTRAINT ck_attendance_policy_documents_status CHECK (status IN {_in(POLICY_DOCUMENT_STATUSES)}),
        CONSTRAINT ck_attendance_policy_documents_purposes CHECK (purposes <@ ARRAY{list(CONSENT_PURPOSES)}::text[]),
        CONSTRAINT ck_attendance_policy_documents_published CHECK (status = 'DRAFT' OR published_at IS NOT NULL),
        CONSTRAINT ck_attendance_policy_documents_row_version CHECK (row_version >= 1)""",
    "attendance_policy_acknowledgements": f"""
        {BASE}, document_id INTEGER NOT NULL, employee_id INTEGER NOT NULL, acknowledged_at TIMESTAMPTZ NOT NULL,
        channel VARCHAR(14) NOT NULL,
        CONSTRAINT uq_attendance_policy_acknowledgements_company_id_id UNIQUE (company_id, id),
        CONSTRAINT uq_attendance_policy_acknowledgements_once UNIQUE (company_id, document_id, employee_id),
        {_fk('attendance_policy_acknowledgements', 'document_id', 'attendance_policy_documents', 'document')},
        {_fk('attendance_policy_acknowledgements', 'employee_id', 'employees', 'employee')},
        CONSTRAINT ck_attendance_policy_acknowledgements_channel CHECK (channel IN {_in(CONSENT_CHANNELS)})""",
    "roster_consent_conflicts": f"""
        {BASE}, employee_id INTEGER NOT NULL, roster_entry_id INTEGER NOT NULL, roster_period_id INTEGER,
        shift_date DATE NOT NULL, purpose VARCHAR(28) NOT NULL, rule_key VARCHAR(40) NOT NULL, consent_id INTEGER,
        status VARCHAR(10) NOT NULL DEFAULT 'OPEN', resolved_at TIMESTAMPTZ,
        CONSTRAINT uq_roster_consent_conflicts_company_id_id UNIQUE (company_id, id),
        {_fk('roster_consent_conflicts', 'employee_id', 'employees', 'employee')},
        {_fk('roster_consent_conflicts', 'roster_entry_id', 'roster_entries', 'entry')},
        {_fk('roster_consent_conflicts', 'roster_period_id', 'roster_periods', 'period')},
        {_fk('roster_consent_conflicts', 'consent_id', 'attendance_consents', 'consent')},
        CONSTRAINT ck_roster_consent_conflicts_purpose CHECK (purpose IN {_in(CONSENT_PURPOSES)}),
        CONSTRAINT ck_roster_consent_conflicts_status CHECK (status IN {_in(CONFLICT_STATUSES)}),
        CONSTRAINT ck_roster_consent_conflicts_resolved CHECK ((status = 'RESOLVED') = (resolved_at IS NOT NULL))""",
}
APPEND_ONLY = ("attendance_consents", "attendance_policy_acknowledgements")
INDEXES = (
    "CREATE INDEX ix_attendance_sites_branch ON attendance_sites (company_id, branch_id, status)",
    "CREATE INDEX ix_attendance_consents_state ON attendance_consents "
    "(company_id, employee_id, purpose, created_at DESC, id DESC)",
    "CREATE INDEX ix_attendance_consents_expiry ON attendance_consents (company_id, valid_to) "
    "WHERE status = 'GRANTED' AND valid_to IS NOT NULL",
    "CREATE INDEX ix_attendance_policy_acknowledgements_employee ON attendance_policy_acknowledgements "
    "(company_id, employee_id)",
    "CREATE UNIQUE INDEX uq_roster_consent_conflicts_open ON roster_consent_conflicts "
    "(company_id, roster_entry_id, purpose) WHERE status = 'OPEN'",
    "CREATE INDEX ix_roster_consent_conflicts_period ON roster_consent_conflicts (company_id, roster_period_id, status)",
)
SAMPLE_COLUMNS = ("punch_id BIGINT", "site_id INTEGER", "distance_to_site_m NUMERIC(9,1)",
                  "geofence_result VARCHAR(12)", "mock_location BOOLEAN")
FACE_COLUMNS = ("encoding_enc BYTEA", "key_id VARCHAR(64)", "consent_id INTEGER", "retention_until DATE",
                "deleted_at TIMESTAMPTZ", "legacy_enrolled BOOLEAN NOT NULL DEFAULT false", "legacy_grace_until DATE")
GUARD = """
CREATE OR REPLACE FUNCTION {name}_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '{name} is append-only: a change is a new row'
        USING ERRCODE = 'check_violation', CONSTRAINT = '{name}_guard';
END;
$$ LANGUAGE plpgsql;
"""


def _app_roles(bind) -> list:
    return [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"))]


def _add_grant(bind, company_id: int, role_id: int, code: str, scope: str) -> bool:
    if bind.execute(sa.text(
            "SELECT 1 FROM role_permissions WHERE role_id = :r AND permission_code = :c AND scope = :s"),
            {"r": role_id, "c": code, "s": scope}).first():
        return False
    bind.execute(sa.text(
        "INSERT INTO role_permissions (company_id, role_id, permission_code, scope, created_at, updated_at) "
        "VALUES (:cid, :r, :c, :s, now(), now())"), {"cid": company_id, "r": role_id, "c": code, "s": scope})
    return True


def _master_key_configured() -> bool:
    if (os.environ.get("PAYROLL_MASTER_KEY") or "").strip():
        return True
    from app.config import settings

    return bool((settings.payroll_master_key or "").strip())


def upgrade() -> None:
    bind = op.get_bind()
    for table, body in TABLES.items():
        op.execute(f"CREATE TABLE {table} ({body})")
        op.execute(f"CREATE INDEX ix_{table}_company_id ON {table} (company_id)")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")
    for statement in INDEXES:
        op.execute(statement)
    for table in APPEND_ONLY:
        op.execute(GUARD.format(name=table))
        op.execute(f"CREATE TRIGGER trg_{table}_guard BEFORE UPDATE OR DELETE ON {table} "
                   f"FOR EACH ROW EXECUTE FUNCTION {table}_guard()")
    # Deferred site foreign keys (AS 11 §11.3).
    op.execute("ALTER TABLE attendance_punches ADD "
               + _fk("attendance_punches", "site_id", "attendance_sites", "site"))
    op.execute("ALTER TABLE attendance_devices ADD "
               + _fk("attendance_devices", "site_id", "attendance_sites", "site"))
    # location_samples (AS 11 §3.4).
    for column in SAMPLE_COLUMNS:
        op.execute(f"ALTER TABLE location_samples ADD COLUMN {column}")
    op.execute("ALTER TABLE location_samples ADD " + _fk("samples", "punch_id", "attendance_punches", "punch"))
    op.execute("ALTER TABLE location_samples ADD " + _fk("samples", "site_id", "attendance_sites", "site"))
    op.execute(f"ALTER TABLE location_samples ADD CONSTRAINT ck_samples_geofence "
               f"CHECK (geofence_result IS NULL OR geofence_result IN {_in(GEOFENCE_RESULTS)})")
    op.execute("CREATE INDEX ix_location_samples_punch ON location_samples (company_id, punch_id) "
               "WHERE punch_id IS NOT NULL")
    # face_encodings (AS 11 §3.8; SP-AS-219).
    for column in FACE_COLUMNS:
        op.execute(f"ALTER TABLE face_encodings ADD COLUMN {column}")
    op.execute("ALTER TABLE face_encodings ALTER COLUMN encoding DROP NOT NULL")
    op.execute("ALTER TABLE face_encodings ADD " + _fk("face_encodings", "consent_id", "attendance_consents", "consent"))
    op.execute("ALTER TABLE face_encodings ADD CONSTRAINT ck_face_encodings_stored CHECK "
               "(deleted_at IS NOT NULL OR encoding IS NOT NULL OR encoding_enc IS NOT NULL)")
    op.execute("ALTER TABLE face_encodings ADD CONSTRAINT ck_face_encodings_key CHECK "
               "((encoding_enc IS NULL) = (key_id IS NULL))")
    op.execute("ALTER TABLE face_encodings ADD CONSTRAINT ck_face_encodings_legacy CHECK "
               "(NOT legacy_enrolled OR legacy_grace_until IS NOT NULL)")
    bind.execute(sa.text(
        "UPDATE face_encodings SET legacy_enrolled = true, "
        f"legacy_grace_until = CURRENT_DATE + {LEGACY_FACE_GRACE_DAYS} "
        "WHERE employee_id IN (SELECT employee_id FROM face_encodings WHERE is_primary)"))
    op.drop_constraint("ck_tenant_data_keys_purpose", "tenant_data_keys", type_="check")
    op.create_check_constraint("ck_tenant_data_keys_purpose", "tenant_data_keys",
                               "purpose IN " + _in(KEY_PURPOSES_AFTER))
    for role in _app_roles(bind):
        quoted = '"' + role.replace('"', '""') + '"'
        for table in TABLES:
            privileges = "SELECT, INSERT" if table in APPEND_ONLY else "SELECT, INSERT, UPDATE, DELETE"
            op.execute(f"GRANT {privileges} ON {table} TO {quoted}")
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}")
    changed = set()
    for key, grants in NEW_GRANTS.items():
        roles = bind.execute(sa.text(
            "SELECT id, company_id FROM roles WHERE system_key = :k AND is_system_role = true ORDER BY id"),
            {"k": key}).all()
        for role_id, company_id in roles:
            for code, scope in grants:
                if _add_grant(bind, company_id, role_id, code, scope):
                    changed.add((company_id, role_id))
    for company_id, role_id in sorted(changed):
        bind.execute(sa.text(
            "UPDATE users SET permission_version = permission_version + 1 WHERE company_id = :cid AND id IN "
            "(SELECT user_id FROM user_role_assignments WHERE company_id = :cid AND role_id = :r)"),
            {"cid": company_id, "r": role_id})
    for job_type, cadence, minute, local in SCHEDULED:
        bind.execute(sa.text(
            "INSERT INTO scheduled_jobs (job_type, cadence, at_minute, at_local_time, enabled, next_run_at) "
            "VALUES (:t, :c, :m, CAST(:l AS time), true, now()) ON CONFLICT (job_type) DO NOTHING"),
            {"t": job_type, "c": cadence, "m": minute, "l": local})


def downgrade() -> None:
    bind = op.get_bind()
    encrypted_only = bind.execute(sa.text(
        "SELECT count(*) FROM face_encodings WHERE encoding IS NULL AND deleted_at IS NULL")).scalar()
    if encrypted_only:
        if not _master_key_configured():
            raise RuntimeError("0050 downgrade needs PAYROLL_MASTER_KEY to decrypt face encodings back to plaintext")
        from app.services import attendance_faces

        attendance_faces.decrypt_all_rows(bind)
    bind.execute(sa.text("DELETE FROM face_encodings WHERE encoding IS NULL"))
    bind.execute(sa.text("DELETE FROM scheduled_jobs WHERE job_type IN ('attendance.consent_expire', "
                         "'attendance.location_sweep')"))
    bind.execute(sa.text("DELETE FROM role_permissions WHERE permission_code IN " + _in(NEW_CODES)))
    bind.execute(sa.text("UPDATE users SET permission_version = permission_version + 1"))
    bind.execute(sa.text("DELETE FROM tenant_data_keys WHERE purpose = 'ATTENDANCE_BIOMETRIC'"))
    op.drop_constraint("ck_tenant_data_keys_purpose", "tenant_data_keys", type_="check")
    op.create_check_constraint("ck_tenant_data_keys_purpose", "tenant_data_keys",
                               "purpose IN " + _in(KEY_PURPOSES_BEFORE))
    for name in ("ck_face_encodings_legacy", "ck_face_encodings_key", "ck_face_encodings_stored",
                 "fk_face_encodings_consent"):
        op.execute(f"ALTER TABLE face_encodings DROP CONSTRAINT {name}")
    op.execute("ALTER TABLE face_encodings ALTER COLUMN encoding SET NOT NULL")
    for column in FACE_COLUMNS:
        op.execute(f"ALTER TABLE face_encodings DROP COLUMN {column.split()[0]}")
    op.execute("DROP INDEX ix_location_samples_punch")
    for name in ("ck_samples_geofence", "fk_samples_site", "fk_samples_punch"):
        op.execute(f"ALTER TABLE location_samples DROP CONSTRAINT {name}")
    for column in SAMPLE_COLUMNS:
        op.execute(f"ALTER TABLE location_samples DROP COLUMN {column.split()[0]}")
    op.execute("ALTER TABLE attendance_devices DROP CONSTRAINT fk_attendance_devices_site")
    op.execute("ALTER TABLE attendance_punches DROP CONSTRAINT fk_attendance_punches_site")
    for table in reversed(list(TABLES)):
        op.execute(f"DROP TABLE {table}")
    for table in APPEND_ONLY:
        op.execute(f"DROP FUNCTION IF EXISTS {table}_guard()")

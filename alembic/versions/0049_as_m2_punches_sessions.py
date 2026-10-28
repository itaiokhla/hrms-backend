"""AS-M2 capture v2: raw punch log, kiosk devices, review queue and the sessions model (spine §3.1, §3.2, §3.6,
§3.12, §15.2; AS 11 §3.1-§3.3, §4.1, §4.7, §4.12, §11.4; D-AS-04, D-AS-05).

NEW tenant tables (RLS, grants, composite same-company foreign keys; app/models/attendance.py documents them):
``attendance_devices``, ``attendance_punches`` (BIGINT id, GUARD: trigger ``attendance_punches_guard`` lets only the
processing columns change and refuses DELETE; the app role holds SELECT, INSERT and column UPDATE only),
``attendance_review_items`` (one OPEN item per ``dedup_key``).

``attendance`` becomes "sessions" in two steps (spine §15.2, A-18):

1. Schema: ``session_seq`` (default 1), nullable ``capture_source`` and ``occupied_until``, the void columns and the
   placement / lineage columns; ``uq_attendance_employee_day_seq (company_id, employee_id, date, session_seq) WHERE
   voided_at IS NULL`` replaces ``uq_attendance_employee_day`` (plain CREATE UNIQUE INDEX inside the transaction);
   ``uq_attendance_one_live_session`` gains ``AND voided_at IS NULL``; ``source`` CHECK added ``NOT VALID`` then
   ``VALIDATE CONSTRAINT`` (BUG-AS-20); index ``ix_attendance_employee_checkin``.
2. Data (autocommit, one commit per batch of 10 000 ids, ``WHERE capture_source IS NULL AND id BETWEEN :lo AND
   :hi`` so a killed run resumes): ``capture_source`` from the legacy ``source`` (self->WEB, kiosk->KIOSK,
   manual->MANUAL, system->SYSTEM, ...) and ``occupied_until`` = ``check_out_time``, else ``LEAST(closed_at,
   check_in_time + maxShiftHours)`` for closed rows, NULL while live (A-08). ``SET NOT NULL`` runs only when no NULL
   is left, otherwise the revision stops with a message and ``alembic upgrade`` is repeated (EC AS-MIG-02).
3. Trigger ``attendance_sessions_no_overlap()`` (23P01, constraint ``attendance_sessions_no_overlap`` -> 409
   ``SESSION_OVERLAP``) for new writes only; overlapping pairs that exist today are reported as one
   ``SESSION_OVERLAP_LEGACY`` review item per pair, never failed (EC AS-MIG-01). PENDING ``location_samples``
   reviews are copied into OPEN items (``location_samples.review_status`` stays a mirror).

Also: ``attendance_breaks`` (kind CHECK widened to BREAK_KINDS, ``source``, ``break_rule_index``, punch ids; the
guard's errors carry the constraint name ``attendance_breaks_guard``), ``attendance_events`` (``punch_id``,
``request_id``, widened CHECKs), ``location_samples`` unique ``(company_id, id)``, scheduled row
``attendance.day_close`` (hourly auto-close of overdue live sessions, jobs only).

Downgrade refuses while any date holds more than one session (non-voided pairs, or a voided row beside its
replacement); otherwise restores ``uq_attendance_employee_day`` and drops everything above.

Revision ID: 0049_as_m2_punches_sessions
Revises: 0048_x_m1_permissions
Create Date: 2026-10-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0049_as_m2_punches_sessions"
down_revision: Union[str, None] = "0048_x_m1_permissions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
BASE = ("id SERIAL PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE RESTRICT, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()")
BIG_BASE = BASE.replace("id SERIAL", "id BIGSERIAL")
BATCH = 10_000
DEFAULT_MAX_SHIFT_HOURS = 16  # settings_service maxShiftHours default (frozen)

# Frozen copies of the vocabularies (spine §3.13 at this revision).
CAPTURE_SOURCES = ("WEB", "MOBILE", "KIOSK", "DEVICE", "IMPORT", "MANUAL", "REQUEST", "SUPERVISOR", "SYSTEM")
ATTENDANCE_SOURCES = ("self", "kiosk", "manual", "system", "device", "import", "request", "supervisor")
CAPTURE_OF_SOURCE = {"self": "WEB", "kiosk": "KIOSK", "manual": "MANUAL", "system": "SYSTEM", "device": "DEVICE",
                     "import": "IMPORT", "request": "REQUEST", "supervisor": "SUPERVISOR"}
PUNCH_DIRECTIONS = ("IN", "OUT", "BREAK_START", "BREAK_END", "UNKNOWN")
VERIFY_MODES = ("FACE", "FINGER", "CARD", "PIN", "PASSWORD", "QR", "NFC", "SELFIE", "NONE", "OTHER")
EVIDENCE_FLAGS = ("MOCK_LOCATION", "LOW_ACCURACY", "OUTSIDE_GEOFENCE", "IP_NOT_ALLOWED", "FACE_MISMATCH", "CLOCK_SKEW",
                  "LATE_LOG", "OFFLINE_CAPTURED", "QR_INVALID", "OFFLINE_UNVERIFIED", "MANUALLY_CORRECTED",
                  "FUTURE_DATED", "OTHER_BRANCH_KIOSK", "LATE_CLOSED_PERIOD", "AMBIGUOUS_LOCAL_TIME",
                  "OUTAGE_FALLBACK")
GEOFENCE_RESULTS = ("INSIDE", "OUTSIDE", "UNKNOWN", "NOT_CHECKED")
MAPPING_STATUSES = ("MAPPED", "UNMAPPED", "AMBIGUOUS")
PUNCH_DISPOSITIONS = ("PENDING", "USED", "IGNORED_DUPLICATE", "IGNORED_WINDOW", "IGNORED_BY_ADMIN",
                      "OUTSIDE_EMPLOYMENT", "ORPHAN")
WORK_MODES = ("ON_SITE", "REMOTE", "FIELD")
VOID_REASONS = ("REQUEST", "REPAIRED", "DUPLICATE")
REVIEW_KINDS = ("LOCATION_EXCEPTION", "MOVEMENT", "OUTSIDE_GEOFENCE", "LOW_ACCURACY", "MOCK_LOCATION",
                "IP_NOT_ALLOWED", "FACE_MISMATCH", "IMPOSSIBLE_TRAVEL", "UNMAPPED_PUNCH", "ORPHAN_PUNCH",
                "DEVICE_CLOCK_SKEW", "DEVICE_OFFLINE", "SESSION_OVERLAP_LEGACY", "CONTRACTOR_HEADCOUNT_OVER_LICENCE",
                "CONTRACTOR_LICENCE_EXPIRED", "QR_INVALID", "KIOSK_OFF_SITE", "DEVICE_ZONE_SUSPECT", "STRAY_PUNCH",
                "PUNCH_OUTSIDE_EMPLOYMENT", "AMBIGUOUS_LOCAL_TIME", "LICENCE_NOT_RECORDED", "RULE_MEMBER_GAP")
REVIEW_ITEM_STATUSES = ("OPEN", "ACKNOWLEDGED", "APPROVED", "REJECTED", "RESOLVED")
DEVICE_KINDS = ("KIOSK", "TERMINAL", "AGENT")
DEVICE_VENDORS = ("ZKTECO", "ESSL", "SUPREMA", "HIKVISION", "MATRIX", "MANTRA", "REALTIME", "BROWSER", "OTHER")
DEVICE_INTEGRATIONS = ("PUSH", "AGENT", "WEBHOOK", "BROWSER")
DIRECTION_MODES = ("DEVICE_STATE", "ALTERNATE", "FIRST_LAST", "DEVICE_ROLE")
DEVICE_ROLES = ("IN_OUT", "IN_ONLY", "OUT_ONLY")
DEVICE_STATUSES = ("PENDING_ACTIVATION", "ACTIVE", "SUSPENDED", "RETIRED")
DEVICE_HEALTH = ("UNKNOWN", "ONLINE", "STALE", "OFFLINE", "CLOCK_SKEW")
OLD_BREAK_KINDS = ("LUNCH",)
BREAK_KINDS = ("LUNCH", "REST", "PERSONAL", "OTHER")
BREAK_SOURCES = ("PUNCHED", "CORRECTED", "REQUEST")
OLD_EVENT_TYPES = ("PUNCH_IN", "LUNCH_IN", "LUNCH_OUT", "PUNCH_OUT", "SESSION_CLOSED", "CORRECTION")
EVENT_TYPES = OLD_EVENT_TYPES + ("BREAK_START", "BREAK_END", "PAIRED", "REQUEST_APPLIED", "VOIDED")
OLD_EVENT_SOURCES = ("self", "kiosk", "manual", "system")
EVENT_SOURCES = ATTENDANCE_SOURCES
# Columns of attendance_punches the app role may UPDATE (the guard allows exactly these, AS 11 §4.1).
PUNCH_UPDATABLE = ("employee_id", "mapping_status", "disposition", "session_id", "shift_date", "photo_storage_path",
                   "redacted_at", "updated_at")


def _in(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _capture_case() -> str:
    whens = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in CAPTURE_OF_SOURCE.items())
    return f"CASE source {whens} ELSE 'WEB' END"


def _fk(table: str, column: str, target: str, name: str, ondelete: str = "RESTRICT") -> str:
    return (f"CONSTRAINT fk_{table}_{name} FOREIGN KEY (company_id, {column}) "
            f"REFERENCES {target} (company_id, id) ON DELETE {ondelete}")


TABLES = {
    "attendance_devices": f"""
        {BASE}, code VARCHAR(40) NOT NULL, name VARCHAR(100) NOT NULL, kind VARCHAR(10) NOT NULL,
        vendor VARCHAR(10) NOT NULL, integration VARCHAR(8) NOT NULL, serial_number VARCHAR(64), branch_id INTEGER,
        site_id INTEGER, timezone VARCHAR(64) NOT NULL, direction_mode VARCHAR(12) NOT NULL DEFAULT 'FIRST_LAST',
        device_role VARCHAR(8) NOT NULL DEFAULT 'IN_OUT', status VARCHAR(20) NOT NULL DEFAULT 'PENDING_ACTIVATION',
        health VARCHAR(12) NOT NULL DEFAULT 'UNKNOWN', secret_hash VARCHAR(64), secret_enc BYTEA,
        secret_key_id VARCHAR(32), token_hash VARCHAR(64), token_expires_at TIMESTAMPTZ, claim_code_hash VARCHAR(64),
        claim_code_expires_at TIMESTAMPTZ, claim_code_failed_attempts SMALLINT NOT NULL DEFAULT 0,
        comm_key_ref VARCHAR(64), firmware VARCHAR(32), protocol_version VARCHAR(32),
        protocol_state JSONB NOT NULL DEFAULT '{{}}'::jsonb, backfill_from DATE, last_seen_at TIMESTAMPTZ,
        last_punch_at TIMESTAMPTZ, watermark_at TIMESTAMPTZ, last_clock_skew_seconds INTEGER,
        settings JSONB NOT NULL DEFAULT '{{}}'::jsonb, activated_at TIMESTAMPTZ, activated_by_user_id INTEGER,
        retired_at TIMESTAMPTZ, created_by_user_id INTEGER, row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_attendance_devices_company_id_id UNIQUE (company_id, id),
        CONSTRAINT uq_attendance_devices_code UNIQUE (company_id, code),
        CONSTRAINT fk_attendance_devices_branch FOREIGN KEY (company_id, branch_id)
            REFERENCES branches (company_id, id) ON DELETE RESTRICT,
        CONSTRAINT fk_attendance_devices_activated_by FOREIGN KEY (company_id, activated_by_user_id)
            REFERENCES users (company_id, id) ON DELETE RESTRICT,
        CONSTRAINT fk_attendance_devices_created_by FOREIGN KEY (company_id, created_by_user_id)
            REFERENCES users (company_id, id) ON DELETE RESTRICT,
        CONSTRAINT ck_attendance_devices_kind CHECK (kind IN {_in(DEVICE_KINDS)}),
        CONSTRAINT ck_attendance_devices_vendor CHECK (vendor IN {_in(DEVICE_VENDORS)}),
        CONSTRAINT ck_attendance_devices_integration CHECK (integration IN {_in(DEVICE_INTEGRATIONS)}),
        CONSTRAINT ck_attendance_devices_direction_mode CHECK (direction_mode IN {_in(DIRECTION_MODES)}),
        CONSTRAINT ck_attendance_devices_role CHECK (device_role IN {_in(DEVICE_ROLES)}),
        CONSTRAINT ck_attendance_devices_status CHECK (status IN {_in(DEVICE_STATUSES)}),
        CONSTRAINT ck_attendance_devices_health CHECK (health IN {_in(DEVICE_HEALTH)}),
        CONSTRAINT ck_attendance_devices_activated CHECK (status <> 'ACTIVE' OR activated_at IS NOT NULL),
        CONSTRAINT ck_attendance_devices_retired CHECK ((status = 'RETIRED') = (retired_at IS NOT NULL)),
        CONSTRAINT ck_attendance_devices_claim_failures CHECK (claim_code_failed_attempts >= 0),
        CONSTRAINT ck_attendance_devices_row_version CHECK (row_version >= 1)""",
    "attendance_punches": f"""
        {BIG_BASE}, employee_id INTEGER, source VARCHAR(12) NOT NULL, direction VARCHAR(12) NOT NULL,
        direction_raw VARCHAR(16), occurred_at TIMESTAMPTZ NOT NULL, device_local_ts TIMESTAMP, device_tz VARCHAR(64),
        received_at TIMESTAMPTZ NOT NULL DEFAULT now(), client_sent_at TIMESTAMPTZ, device_id INTEGER,
        device_user_id VARCHAR(32), verify_mode VARCHAR(10), verify_raw SMALLINT, work_code VARCHAR(16),
        site_id INTEGER, geofence_result VARCHAR(12) NOT NULL DEFAULT 'NOT_CHECKED', location_sample_id INTEGER,
        ip_hash VARCHAR(64), face_match_score DOUBLE PRECISION, photo_storage_path TEXT, redacted_at TIMESTAMPTZ,
        evidence_flags TEXT[] NOT NULL DEFAULT '{{}}'::text[], natural_key VARCHAR(64) NOT NULL,
        payload_hash VARCHAR(64), ingest_batch_id VARCHAR(64), idempotency_key VARCHAR(64),
        mapping_status VARCHAR(10) NOT NULL, disposition VARCHAR(20) NOT NULL DEFAULT 'PENDING', session_id INTEGER,
        shift_date DATE, actor_user_id INTEGER, request_id INTEGER, reason VARCHAR(500),
        CONSTRAINT uq_attendance_punches_company_id_id UNIQUE (company_id, id),
        CONSTRAINT uq_attendance_punches_natural_key UNIQUE (company_id, natural_key),
        {_fk('attendance_punches', 'employee_id', 'employees', 'employee')},
        {_fk('attendance_punches', 'device_id', 'attendance_devices', 'device')},
        {_fk('attendance_punches', 'location_sample_id', 'location_samples', 'location_sample')},
        {_fk('attendance_punches', 'session_id', 'attendance', 'session')},
        {_fk('attendance_punches', 'actor_user_id', 'users', 'actor')},
        CONSTRAINT ck_attendance_punches_source CHECK (source IN {_in(CAPTURE_SOURCES)}),
        CONSTRAINT ck_attendance_punches_direction CHECK (direction IN {_in(PUNCH_DIRECTIONS)}),
        CONSTRAINT ck_attendance_punches_known_direction CHECK (source IN ('DEVICE', 'IMPORT') OR direction <> 'UNKNOWN'),
        CONSTRAINT ck_attendance_punches_device CHECK (source <> 'DEVICE' OR (device_id IS NOT NULL
            AND device_user_id IS NOT NULL AND device_local_ts IS NOT NULL AND device_tz IS NOT NULL)),
        CONSTRAINT ck_attendance_punches_verify_mode CHECK (verify_mode IS NULL OR verify_mode IN {_in(VERIFY_MODES)}),
        CONSTRAINT ck_attendance_punches_geofence CHECK (geofence_result IN {_in(GEOFENCE_RESULTS)}),
        CONSTRAINT ck_attendance_punches_flags CHECK (evidence_flags <@ ARRAY{list(EVIDENCE_FLAGS)}::text[]),
        CONSTRAINT ck_attendance_punches_mapping CHECK (mapping_status IN {_in(MAPPING_STATUSES)}),
        CONSTRAINT ck_attendance_punches_mapped_employee CHECK ((mapping_status = 'MAPPED') = (employee_id IS NOT NULL)),
        CONSTRAINT ck_attendance_punches_disposition CHECK (disposition IN {_in(PUNCH_DISPOSITIONS)}),
        CONSTRAINT ck_attendance_punches_actor CHECK (source IN ('MANUAL', 'SUPERVISOR', 'REQUEST')
            OR (actor_user_id IS NULL AND request_id IS NULL AND reason IS NULL)),
        CONSTRAINT ck_attendance_punches_photo CHECK (photo_storage_path IS NULL OR redacted_at IS NULL)""",
    "attendance_review_items": f"""
        {BASE}, kind VARCHAR(40) NOT NULL, employee_id INTEGER, shift_date DATE, punch_id BIGINT, session_id INTEGER,
        location_sample_id INTEGER, device_id INTEGER, device_user_id VARCHAR(32), contractor_id INTEGER,
        severity VARCHAR(8) NOT NULL, status VARCHAR(12) NOT NULL DEFAULT 'OPEN', dedup_key VARCHAR(80) NOT NULL,
        decided_by_user_id INTEGER, decided_at TIMESTAMPTZ, note VARCHAR(500), row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_attendance_review_items_company_id_id UNIQUE (company_id, id),
        {_fk('attendance_review_items', 'employee_id', 'employees', 'employee')},
        {_fk('attendance_review_items', 'punch_id', 'attendance_punches', 'punch')},
        {_fk('attendance_review_items', 'session_id', 'attendance', 'session')},
        {_fk('attendance_review_items', 'location_sample_id', 'location_samples', 'sample')},
        {_fk('attendance_review_items', 'device_id', 'attendance_devices', 'device')},
        {_fk('attendance_review_items', 'decided_by_user_id', 'users', 'decided_by')},
        CONSTRAINT ck_attendance_review_items_kind CHECK (kind IN {_in(REVIEW_KINDS)}),
        CONSTRAINT ck_attendance_review_items_severity CHECK (severity IN ('INFO', 'WARNING', 'HIGH')),
        CONSTRAINT ck_attendance_review_items_status CHECK (status IN {_in(REVIEW_ITEM_STATUSES)}),
        CONSTRAINT ck_attendance_review_items_decided CHECK ((status = 'OPEN') = (decided_at IS NULL)),
        CONSTRAINT ck_attendance_review_items_row_version CHECK (row_version >= 1)""",
}
INDEXES = (
    "CREATE UNIQUE INDEX uq_attendance_devices_serial ON attendance_devices (company_id, serial_number) "
    "WHERE retired_at IS NULL AND serial_number IS NOT NULL",
    "CREATE INDEX ix_attendance_devices_token_hash ON attendance_devices (token_hash) WHERE token_hash IS NOT NULL",
    "CREATE INDEX ix_attendance_punches_employee_time ON attendance_punches (company_id, employee_id, occurred_at)",
    "CREATE INDEX ix_attendance_punches_unmapped ON attendance_punches (company_id, device_id, device_user_id) "
    "WHERE mapping_status <> 'MAPPED'",
    "CREATE INDEX ix_attendance_punches_pending ON attendance_punches (company_id, received_at) "
    "WHERE disposition = 'PENDING'",
    "CREATE INDEX ix_attendance_punches_device_time ON attendance_punches (company_id, device_id, occurred_at) "
    "WHERE device_id IS NOT NULL",
    "CREATE INDEX ix_attendance_punches_received ON attendance_punches (company_id, received_at)",
    "CREATE INDEX ix_attendance_punches_session ON attendance_punches (company_id, session_id)",
    "CREATE UNIQUE INDEX uq_attendance_review_items_open ON attendance_review_items (company_id, dedup_key) "
    "WHERE status = 'OPEN'",
    "CREATE INDEX ix_attendance_review_items_queue ON attendance_review_items (company_id, status, kind, created_at)",
)
ATTENDANCE_COLUMNS = (
    "session_seq SMALLINT NOT NULL DEFAULT 1",
    "capture_source VARCHAR(12)",
    "occupied_until TIMESTAMPTZ",
    "legal_entity_id INTEGER",
    "branch_id INTEGER",
    "work_timezone VARCHAR(64)",
    "shift_version_id INTEGER",
    "roster_entry_id INTEGER",
    "roster_entry_version INTEGER",
    "work_mode VARCHAR(10) NOT NULL DEFAULT 'ON_SITE'",
    "first_punch_id BIGINT",
    "last_punch_id BIGINT",
    "pairing_key VARCHAR(64)",
    "request_id INTEGER",
    "voided_at TIMESTAMPTZ",
    "voided_by_user_id INTEGER",
    "void_reason VARCHAR(20)",
)
ATTENDANCE_FKS = (
    ("legal_entity", "legal_entity_id", "legal_entities"),
    ("branch", "branch_id", "branches"),
    ("shift_version", "shift_version_id", "shift_versions"),
    ("roster_entry", "roster_entry_id", "roster_entries"),
    ("voided_by", "voided_by_user_id", "users"),
    ("first_punch", "first_punch_id", "attendance_punches"),
    ("last_punch", "last_punch_id", "attendance_punches"),
)
ATTENDANCE_CHECKS = (
    ("ck_attendance_session_seq", "session_seq BETWEEN 1 AND 8"),
    ("ck_attendance_capture_source", f"capture_source IN {_in(CAPTURE_SOURCES)}"),
    ("ck_attendance_work_mode", f"work_mode IN {_in(WORK_MODES)}"),
    ("ck_attendance_void_reason", f"void_reason IS NULL OR void_reason IN {_in(VOID_REASONS)}"),
    ("ck_attendance_void_pair", "(voided_at IS NULL) = (void_reason IS NULL)"),
    ("ck_attendance_roster_entry", "(roster_entry_id IS NULL) = (roster_entry_version IS NULL)"),
)
OLD_LIVE = "check_out_time IS NULL AND closed_at IS NULL"
NEW_LIVE = OLD_LIVE + " AND voided_at IS NULL"

PUNCHES_GUARD = """
CREATE OR REPLACE FUNCTION attendance_punches_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Raw punches are never deleted'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'attendance_punches_guard';
    END IF;
    IF (NEW.id, NEW.company_id, NEW.created_at, NEW.source, NEW.direction, NEW.direction_raw, NEW.occurred_at,
        NEW.device_local_ts, NEW.device_tz, NEW.received_at, NEW.client_sent_at, NEW.device_id, NEW.device_user_id,
        NEW.verify_mode, NEW.verify_raw, NEW.work_code, NEW.site_id, NEW.geofence_result, NEW.location_sample_id,
        NEW.ip_hash, NEW.face_match_score, NEW.evidence_flags, NEW.natural_key, NEW.payload_hash,
        NEW.ingest_batch_id, NEW.idempotency_key, NEW.actor_user_id, NEW.request_id, NEW.reason)
       IS DISTINCT FROM
       (OLD.id, OLD.company_id, OLD.created_at, OLD.source, OLD.direction, OLD.direction_raw, OLD.occurred_at,
        OLD.device_local_ts, OLD.device_tz, OLD.received_at, OLD.client_sent_at, OLD.device_id, OLD.device_user_id,
        OLD.verify_mode, OLD.verify_raw, OLD.work_code, OLD.site_id, OLD.geofence_result, OLD.location_sample_id,
        OLD.ip_hash, OLD.face_match_score, OLD.evidence_flags, OLD.natural_key, OLD.payload_hash,
        OLD.ingest_batch_id, OLD.idempotency_key, OLD.actor_user_id, OLD.request_id, OLD.reason) THEN
        RAISE EXCEPTION 'A raw punch is never edited'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'attendance_punches_guard';
    END IF;
    -- employee_id: NULL -> value by mapping; value -> value only under the remap flag for DEVICE / IMPORT (SP-AS-013).
    IF OLD.employee_id IS NOT NULL AND NEW.employee_id IS DISTINCT FROM OLD.employee_id
       AND NOT (COALESCE(current_setting('app.attendance_remap', true), '') = 'on'
                AND OLD.source IN ('DEVICE', 'IMPORT') AND NEW.employee_id IS NOT NULL) THEN
        RAISE EXCEPTION 'A mapped punch keeps its employee'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'attendance_punches_guard';
    END IF;
    -- The photo is only ever purged: path -> NULL together with redacted_at, once.
    IF NEW.photo_storage_path IS DISTINCT FROM OLD.photo_storage_path
       AND NOT (NEW.photo_storage_path IS NULL AND NEW.redacted_at IS NOT NULL) THEN
        RAISE EXCEPTION 'A punch photo is only purged'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'attendance_punches_guard';
    END IF;
    IF OLD.redacted_at IS NOT NULL AND NEW.redacted_at IS DISTINCT FROM OLD.redacted_at THEN
        RAISE EXCEPTION 'A punch photo is redacted once'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'attendance_punches_guard';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

SESSIONS_NO_OVERLAP = """
CREATE OR REPLACE FUNCTION attendance_sessions_no_overlap() RETURNS trigger AS $$
DECLARE
    other_id INTEGER;
BEGIN
    IF NEW.voided_at IS NOT NULL THEN
        RETURN NEW;
    END IF;
    -- Shrinking a non-voided session cannot create an overlap (punch-out, auto-close); legacy overlaps are
    -- reported by the migration and never re-checked (spine §3.1).
    IF TG_OP = 'UPDATE' AND OLD.voided_at IS NULL AND NEW.employee_id = OLD.employee_id
       AND NEW.check_in_time >= OLD.check_in_time
       AND COALESCE(NEW.occupied_until, 'infinity'::timestamptz) <= COALESCE(OLD.occupied_until, 'infinity'::timestamptz) THEN
        RETURN NEW;
    END IF;
    PERFORM pg_advisory_xact_lock(4243, NEW.employee_id);
    -- A second row of the same (date, session_seq) or a second live session is answered by the unique indexes
    -- (409 ALREADY_CHECKED_IN), not by this rule.
    IF EXISTS (
        SELECT 1 FROM attendance a
        WHERE a.company_id = NEW.company_id AND a.employee_id = NEW.employee_id AND a.id <> NEW.id
          AND a.voided_at IS NULL
          AND ((a.date = NEW.date AND a.session_seq = NEW.session_seq)
               OR (a.check_out_time IS NULL AND a.closed_at IS NULL
                   AND NEW.check_out_time IS NULL AND NEW.closed_at IS NULL))
    ) THEN
        RETURN NEW;
    END IF;
    SELECT a.id INTO other_id FROM attendance a
    WHERE a.company_id = NEW.company_id AND a.employee_id = NEW.employee_id AND a.id <> NEW.id
      AND a.voided_at IS NULL
      AND tstzrange(a.check_in_time, GREATEST(a.check_in_time, COALESCE(a.occupied_until, 'infinity'::timestamptz)), '[)')
          && tstzrange(NEW.check_in_time,
                       GREATEST(NEW.check_in_time, COALESCE(NEW.occupied_until, 'infinity'::timestamptz)), '[)')
    LIMIT 1;
    IF other_id IS NOT NULL THEN
        RAISE EXCEPTION 'This session overlaps session % of the same person', other_id
            USING ERRCODE = 'exclusion_violation', CONSTRAINT = 'attendance_sessions_no_overlap';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

# Writers that set only the legacy columns (raw SQL, old code paths) still get a valid session: capture_source from
# the lowercase mirror, occupied_until = check_out_time whenever there is a punch-out (A-08). Fires before the
# overlap trigger (trigger names sort "session_" < "sessions_").
SESSION_DEFAULTS = f"""
CREATE OR REPLACE FUNCTION attendance_session_defaults() RETURNS trigger AS $$
BEGIN
    IF NEW.capture_source IS NULL THEN
        NEW.capture_source := {_capture_case().replace("CASE source", "CASE NEW.source")};
    END IF;
    IF NEW.check_out_time IS NOT NULL THEN
        NEW.occupied_until := NEW.check_out_time;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def _breaks_guard(named: bool) -> str:
    """0004's attendance_breaks_guard; this revision adds CONSTRAINT = 'attendance_breaks_guard' to every RAISE."""
    tag = ", CONSTRAINT = 'attendance_breaks_guard'" if named else ""
    return f"""
CREATE OR REPLACE FUNCTION attendance_breaks_guard() RETURNS trigger AS $$
DECLARE
    session record;
BEGIN
    SELECT employee_id, check_in_time, check_out_time INTO session
    FROM attendance WHERE id = NEW.attendance_id AND company_id = NEW.company_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Break must belong to a session of the same company'
            USING ERRCODE = 'foreign_key_violation'{tag};
    END IF;
    IF session.employee_id <> NEW.employee_id THEN
        RAISE EXCEPTION 'Break employee does not match its session' USING ERRCODE = 'check_violation'{tag};
    END IF;
    IF NEW.started_at < session.check_in_time THEN
        RAISE EXCEPTION 'Break starts before the punch-in' USING ERRCODE = 'check_violation'{tag};
    END IF;
    IF session.check_out_time IS NOT NULL
       AND (NEW.ended_at IS NULL OR NEW.ended_at > session.check_out_time) THEN
        RAISE EXCEPTION 'Break must end by the punch-out' USING ERRCODE = 'check_violation'{tag};
    END IF;
    IF EXISTS (
        SELECT 1 FROM attendance_breaks b
        WHERE b.attendance_id = NEW.attendance_id AND b.id <> NEW.id
          AND tstzrange(b.started_at, COALESCE(b.ended_at, 'infinity'::timestamptz), '[)')
              && tstzrange(NEW.started_at, COALESCE(NEW.ended_at, 'infinity'::timestamptz), '[)')
    ) THEN
        RAISE EXCEPTION 'Breaks in one session cannot overlap' USING ERRCODE = 'exclusion_violation'{tag};
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""



def _max_shift_hours(bind) -> dict:
    rows = bind.execute(sa.text("SELECT company_id, value FROM company_settings WHERE key = 'maxShiftHours'"))
    hours = {}
    for company_id, value in rows:
        try:
            hours[company_id] = max(4, min(24, int(str(value).strip().strip('"'))))
        except (TypeError, ValueError):
            continue
    return hours


def backfill(bind, batch: int = BATCH, stop_after=None) -> int:
    """Step 2 of spine §15.2: capture_source and occupied_until in id batches; each batch commits on its own when
    ``bind`` is in autocommit. ``stop_after`` (batches) simulates a killed deploy in the rehearsal test. Returns the
    number of batches run."""
    hours = _max_shift_hours(bind)
    low, high = bind.execute(sa.text(
        "SELECT min(id), max(id) FROM attendance WHERE capture_source IS NULL")).one()
    if low is None:
        return 0
    case_hours = " ".join(f"WHEN {cid} THEN {h}" for cid, h in sorted(hours.items()))
    hours_sql = f"(CASE company_id {case_hours} ELSE {DEFAULT_MAX_SHIFT_HOURS} END)" if hours \
        else str(DEFAULT_MAX_SHIFT_HOURS)
    statement = sa.text(
        f"UPDATE attendance SET capture_source = {_capture_case()}, "
        "occupied_until = CASE WHEN check_out_time IS NOT NULL THEN check_out_time "
        f"WHEN closed_at IS NOT NULL THEN GREATEST(check_in_time, LEAST(closed_at, check_in_time + "
        f"make_interval(hours => {hours_sql}))) ELSE NULL END "
        "WHERE capture_source IS NULL AND id BETWEEN :lo AND :hi")
    runs = 0
    start = low
    while start <= high:
        bind.execute(statement, {"lo": start, "hi": start + batch - 1})
        runs += 1
        start += batch
        if stop_after is not None and runs >= stop_after:
            break
    return runs


def report_legacy_overlaps(bind) -> int:
    """One SESSION_OVERLAP_LEGACY review item per overlapping pair of non-voided sessions (reported, never failed)."""
    result = bind.execute(sa.text("""
        INSERT INTO attendance_review_items (company_id, kind, employee_id, shift_date, session_id, severity, status,
                                             dedup_key)
        SELECT a.company_id, 'SESSION_OVERLAP_LEGACY', a.employee_id, a.date, a.id, 'INFO', 'OPEN',
               'SESSION_OVERLAP_LEGACY:' || a.id || ':' || b.id
        FROM attendance a
        JOIN attendance b ON b.company_id = a.company_id AND b.employee_id = a.employee_id AND b.id > a.id
         AND b.voided_at IS NULL
         AND tstzrange(a.check_in_time, GREATEST(a.check_in_time, COALESCE(a.occupied_until, 'infinity'::timestamptz)), '[)')
             && tstzrange(b.check_in_time, GREATEST(b.check_in_time, COALESCE(b.occupied_until, 'infinity'::timestamptz)), '[)')
        WHERE a.voided_at IS NULL
        ON CONFLICT (company_id, dedup_key) WHERE status = 'OPEN' DO NOTHING
    """))
    return result.rowcount or 0


def _copy_location_reviews(bind) -> None:
    bind.execute(sa.text("""
        INSERT INTO attendance_review_items (company_id, kind, employee_id, session_id, location_sample_id, severity,
                                             status, dedup_key, created_at)
        SELECT s.company_id,
               CASE WHEN s.exception_reason IS NOT NULL THEN 'LOCATION_EXCEPTION' ELSE 'MOVEMENT' END,
               s.employee_id, s.attendance_id, s.id,
               CASE WHEN s.exception_reason IS NOT NULL THEN 'WARNING' ELSE 'INFO' END, 'OPEN',
               CASE WHEN s.exception_reason IS NOT NULL THEN 'LOCATION_EXCEPTION:sample:' ELSE 'MOVEMENT:' END || s.id,
               s.received_at
        FROM location_samples s WHERE s.review_status = 'PENDING'
        ON CONFLICT (company_id, dedup_key) WHERE status = 'OPEN' DO NOTHING
    """))
    bind.execute(sa.text("""
        UPDATE attendance_review_items i SET shift_date = a.date
        FROM attendance a WHERE a.company_id = i.company_id AND a.id = i.session_id AND i.shift_date IS NULL
    """))


def _app_roles(bind) -> list:
    return [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"))]


def upgrade() -> None:
    bind = op.get_bind()
    # ---------------------------------------------------------------- step 1: schema
    op.execute("ALTER TABLE location_samples ADD CONSTRAINT uq_location_samples_company_id_id UNIQUE (company_id, id)")
    for table, body in TABLES.items():
        op.execute(f"CREATE TABLE {table} ({body})")
        op.execute(f"CREATE INDEX ix_{table}_company_id ON {table} (company_id)")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")
    for statement in INDEXES:
        op.execute(statement)
    op.execute(PUNCHES_GUARD)
    op.execute("CREATE TRIGGER trg_attendance_punches_guard BEFORE UPDATE OR DELETE ON attendance_punches "
               "FOR EACH ROW EXECUTE FUNCTION attendance_punches_guard()")

    for column in ATTENDANCE_COLUMNS:
        op.execute(f"ALTER TABLE attendance ADD COLUMN {column}")
    for name, column, target in ATTENDANCE_FKS:
        op.execute(f"ALTER TABLE attendance ADD {_fk('attendance', column, target, name)}")
    for name, condition in ATTENDANCE_CHECKS:
        op.execute(f"ALTER TABLE attendance ADD CONSTRAINT {name} CHECK ({condition})")
    op.execute("CREATE UNIQUE INDEX uq_attendance_employee_day_seq ON attendance "
               "(company_id, employee_id, date, session_seq) WHERE voided_at IS NULL")
    op.execute("ALTER TABLE attendance DROP CONSTRAINT uq_attendance_employee_day")
    op.execute("DROP INDEX uq_attendance_one_live_session")
    op.execute(f"CREATE UNIQUE INDEX uq_attendance_one_live_session ON attendance (company_id, employee_id) "
               f"WHERE {NEW_LIVE}")
    op.execute("CREATE INDEX ix_attendance_employee_checkin ON attendance (company_id, employee_id, check_in_time)")
    # BUG-AS-20: the legacy mirror gets its CHECK without a long lock, then is validated.
    op.execute(f"ALTER TABLE attendance ADD CONSTRAINT ck_attendance_source CHECK (source IN {_in(ATTENDANCE_SOURCES)}) "
               "NOT VALID")
    op.execute("ALTER TABLE attendance VALIDATE CONSTRAINT ck_attendance_source")

    op.execute("ALTER TABLE attendance_breaks DROP CONSTRAINT ck_breaks_kind")
    op.execute(f"ALTER TABLE attendance_breaks ADD CONSTRAINT ck_breaks_kind CHECK (kind IN {_in(BREAK_KINDS)})")
    op.execute("ALTER TABLE attendance_breaks ADD COLUMN source VARCHAR(10) NOT NULL DEFAULT 'PUNCHED'")
    op.execute("ALTER TABLE attendance_breaks ADD COLUMN break_rule_index SMALLINT")
    op.execute("ALTER TABLE attendance_breaks ADD COLUMN start_punch_id BIGINT")
    op.execute("ALTER TABLE attendance_breaks ADD COLUMN end_punch_id BIGINT")
    op.execute(f"ALTER TABLE attendance_breaks ADD CONSTRAINT ck_breaks_source CHECK (source IN {_in(BREAK_SOURCES)})")
    op.execute("ALTER TABLE attendance_breaks ADD CONSTRAINT ck_breaks_rule_index "
               "CHECK (break_rule_index IS NULL OR break_rule_index BETWEEN 0 AND 3)")
    op.execute(f"ALTER TABLE attendance_breaks ADD {_fk('breaks', 'start_punch_id', 'attendance_punches', 'start_punch')}")
    op.execute(f"ALTER TABLE attendance_breaks ADD {_fk('breaks', 'end_punch_id', 'attendance_punches', 'end_punch')}")
    op.execute(_breaks_guard(named=True))

    op.execute("ALTER TABLE attendance_events DROP CONSTRAINT ck_events_type")
    op.execute(f"ALTER TABLE attendance_events ADD CONSTRAINT ck_events_type CHECK (event_type IN {_in(EVENT_TYPES)})")
    op.execute("ALTER TABLE attendance_events DROP CONSTRAINT ck_events_source")
    op.execute(f"ALTER TABLE attendance_events ADD CONSTRAINT ck_events_source CHECK (source IN {_in(EVENT_SOURCES)})")
    op.execute("ALTER TABLE attendance_events ADD COLUMN punch_id BIGINT")
    op.execute("ALTER TABLE attendance_events ADD COLUMN request_id INTEGER")
    op.execute(f"ALTER TABLE attendance_events ADD {_fk('events', 'punch_id', 'attendance_punches', 'punch')}")

    for role in _app_roles(bind):
        quoted = '"' + role.replace('"', '""') + '"'
        for table in ("attendance_devices", "attendance_review_items"):
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {quoted}")
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}")
        op.execute(f"GRANT SELECT, INSERT ON attendance_punches TO {quoted}")
        op.execute(f"GRANT UPDATE ({', '.join(PUNCH_UPDATABLE)}) ON attendance_punches TO {quoted}")
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE attendance_punches_id_seq TO {quoted}")
    bind.execute(sa.text(
        "INSERT INTO scheduled_jobs (job_type, cadence, at_minute, at_local_time, enabled, next_run_at) "
        "VALUES ('attendance.day_close', 'HOURLY', 12, NULL, true, now()) ON CONFLICT (job_type) DO NOTHING"))

    # ---------------------------------------------------------------- step 2: batched backfill (autocommit)
    with op.get_context().autocommit_block():
        backfill(op.get_bind())
    if bind.execute(sa.text("SELECT 1 FROM attendance WHERE capture_source IS NULL LIMIT 1")).first():
        raise RuntimeError("attendance.capture_source still has NULL rows after the backfill; "
                           "run `alembic upgrade head` again to resume")
    op.execute("ALTER TABLE attendance ALTER COLUMN capture_source SET NOT NULL")
    op.execute("ALTER TABLE attendance ADD CONSTRAINT ck_attendance_occupied "
               "CHECK (occupied_until IS NULL OR occupied_until >= check_in_time) NOT VALID")
    op.execute("ALTER TABLE attendance VALIDATE CONSTRAINT ck_attendance_occupied")

    # ---------------------------------------------------------------- step 3: overlap rule and reports
    op.execute(SESSION_DEFAULTS)
    op.execute("CREATE TRIGGER trg_attendance_session_defaults BEFORE INSERT OR UPDATE ON attendance "
               "FOR EACH ROW EXECUTE FUNCTION attendance_session_defaults()")
    op.execute(SESSIONS_NO_OVERLAP)
    op.execute("CREATE TRIGGER trg_attendance_sessions_no_overlap BEFORE INSERT OR UPDATE OF check_in_time, "
               "check_out_time, occupied_until, voided_at ON attendance "
               "FOR EACH ROW EXECUTE FUNCTION attendance_sessions_no_overlap()")
    report_legacy_overlaps(bind)
    _copy_location_reviews(bind)


def downgrade_blocker(bind):
    """Why the downgrade must refuse, or None (spine §15.2: never while a date holds two sessions)."""
    if bind.execute(sa.text(
            "SELECT 1 FROM attendance WHERE voided_at IS NULL GROUP BY company_id, employee_id, date "
            "HAVING count(*) > 1 LIMIT 1")).first():
        return ("a date holds more than one non-voided session; one session per date is required before "
                "downgrading as_m2_punches_sessions")
    if bind.execute(sa.text(
            "SELECT 1 FROM attendance GROUP BY company_id, employee_id, date HAVING count(*) > 1 LIMIT 1")).first():
        return "a date holds a voided session beside its replacement; uq_attendance_employee_day cannot be restored"
    return None


def downgrade() -> None:
    bind = op.get_bind()
    blocker = downgrade_blocker(bind)
    if blocker:
        raise RuntimeError(blocker)
    bind.execute(sa.text("DELETE FROM scheduled_jobs WHERE job_type = 'attendance.day_close'"))
    op.execute("DROP TRIGGER IF EXISTS trg_attendance_session_defaults ON attendance")
    op.execute("DROP FUNCTION IF EXISTS attendance_session_defaults()")
    op.execute("DROP TRIGGER IF EXISTS trg_attendance_sessions_no_overlap ON attendance")
    op.execute("DROP FUNCTION IF EXISTS attendance_sessions_no_overlap()")
    op.execute(_breaks_guard(named=False))

    op.execute("ALTER TABLE attendance_events DROP CONSTRAINT fk_events_punch")
    op.execute("ALTER TABLE attendance_events DROP COLUMN punch_id")
    op.execute("ALTER TABLE attendance_events DROP COLUMN request_id")
    op.execute("ALTER TABLE attendance_events DROP CONSTRAINT ck_events_source")
    op.execute(f"ALTER TABLE attendance_events ADD CONSTRAINT ck_events_source CHECK (source IN {_in(OLD_EVENT_SOURCES)})")
    op.execute("ALTER TABLE attendance_events DROP CONSTRAINT ck_events_type")
    op.execute(f"ALTER TABLE attendance_events ADD CONSTRAINT ck_events_type CHECK (event_type IN {_in(OLD_EVENT_TYPES)})")

    for column in ("start_punch_id", "end_punch_id", "break_rule_index", "source"):
        op.execute(f"ALTER TABLE attendance_breaks DROP COLUMN {column}")
    op.execute("ALTER TABLE attendance_breaks DROP CONSTRAINT ck_breaks_kind")
    op.execute(f"ALTER TABLE attendance_breaks ADD CONSTRAINT ck_breaks_kind CHECK (kind IN {_in(OLD_BREAK_KINDS)})")

    op.execute("ALTER TABLE attendance DROP CONSTRAINT ck_attendance_source")
    op.execute("ALTER TABLE attendance DROP CONSTRAINT ck_attendance_occupied")
    op.execute("DROP INDEX ix_attendance_employee_checkin")
    op.execute("DROP INDEX uq_attendance_one_live_session")
    op.execute(f"CREATE UNIQUE INDEX uq_attendance_one_live_session ON attendance (company_id, employee_id) "
               f"WHERE {OLD_LIVE}")
    op.execute("ALTER TABLE attendance ADD CONSTRAINT uq_attendance_employee_day UNIQUE (company_id, employee_id, date)")
    op.execute("DROP INDEX uq_attendance_employee_day_seq")
    for name, _ in ATTENDANCE_CHECKS:
        op.execute(f"ALTER TABLE attendance DROP CONSTRAINT {name}")
    for name, _, _ in ATTENDANCE_FKS:
        op.execute(f"ALTER TABLE attendance DROP CONSTRAINT fk_attendance_{name}")
    for column in ATTENDANCE_COLUMNS:
        op.execute(f"ALTER TABLE attendance DROP COLUMN {column.split()[0]}")

    for table in ("attendance_review_items", "attendance_punches", "attendance_devices"):
        op.execute(f"DROP TABLE {table}")
    op.execute("DROP FUNCTION IF EXISTS attendance_punches_guard()")
    op.execute("ALTER TABLE location_samples DROP CONSTRAINT uq_location_samples_company_id_id")

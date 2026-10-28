from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import Numeric, cast
from sqlalchemy.dialects.postgresql import ARRAY, CIDR, JSONB
from sqlalchemy.orm import relationship, validates
from sqlalchemy.types import TypeDecorator

from app.models.base import BaseModel, BigIdModel, TenantMixin

ATTENDANCE_STATUSES = ("present", "absent", "late", "half-day")
TRACKING_STATES = ("NOT_APPLICABLE", "ACTIVE", "PAUSED", "STOPPED")
BREAK_KINDS = ("LUNCH", "REST", "PERSONAL", "OTHER")
BREAK_SOURCES = ("PUNCHED", "CORRECTED", "REQUEST")
EVENT_TYPES = ("PUNCH_IN", "LUNCH_IN", "LUNCH_OUT", "PUNCH_OUT", "SESSION_CLOSED", "CORRECTION",
               "BREAK_START", "BREAK_END", "PAIRED", "REQUEST_APPLIED", "VOIDED")
EVENT_SOURCES = ("self", "kiosk", "manual", "system", "device", "import", "request", "supervisor")
# Every value attendance.source may hold (spine §3.1; BUG-AS-20); the DB CHECK ck_attendance_source has the same list
# since as_m2_punches_sessions (G-06).
ATTENDANCE_SOURCES = ("self", "kiosk", "manual", "system", "device", "import", "request", "supervisor")

# Capture v2 vocabularies (spine §3.13, AS 11 §5.2; frozen copies live in 0049_as_m2_punches_sessions).
CAPTURE_SOURCES = ("WEB", "MOBILE", "KIOSK", "DEVICE", "IMPORT", "MANUAL", "REQUEST", "SUPERVISOR", "SYSTEM")
INTERACTIVE_SOURCES = ("WEB", "MOBILE", "KIOSK")
# The legacy lowercase attendance.source mirror of each capture source and back (spine §3.1 backfill).
SOURCE_OF_CAPTURE = {"WEB": "self", "MOBILE": "self", "KIOSK": "kiosk", "DEVICE": "device", "IMPORT": "import",
                     "MANUAL": "manual", "REQUEST": "request", "SUPERVISOR": "supervisor", "SYSTEM": "system"}
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
# Location kinds are decided with location:review, the others with attendance:review (spine §4.10).
LOCATION_REVIEW_KINDS = ("LOCATION_EXCEPTION", "MOVEMENT", "OUTSIDE_GEOFENCE", "LOW_ACCURACY", "MOCK_LOCATION",
                         "IMPOSSIBLE_TRAVEL")
# Acknowledge-only kinds (01 §11.4).
INFORMATIONAL_REVIEW_KINDS = ("MOVEMENT", "LOW_ACCURACY", "SESSION_OVERLAP_LEGACY", "CONTRACTOR_HEADCOUNT_OVER_LICENCE",
                              "QR_INVALID")
REVIEW_ITEM_STATUSES = ("OPEN", "ACKNOWLEDGED", "APPROVED", "REJECTED", "RESOLVED")
REVIEW_SEVERITIES = ("INFO", "WARNING", "HIGH")
DEVICE_KINDS = ("KIOSK", "TERMINAL", "AGENT")
DEVICE_VENDORS = ("ZKTECO", "ESSL", "SUPREMA", "HIKVISION", "MATRIX", "MANTRA", "REALTIME", "BROWSER", "OTHER")
DEVICE_INTEGRATIONS = ("PUSH", "AGENT", "WEBHOOK", "BROWSER")
DIRECTION_MODES = ("DEVICE_STATE", "ALTERNATE", "FIRST_LAST", "DEVICE_ROLE")
DEVICE_ROLES = ("IN_OUT", "IN_ONLY", "OUT_ONLY")
DEVICE_STATUSES = ("PENDING_ACTIVATION", "ACTIVE", "SUSPENDED", "RETIRED")
DEVICE_HEALTH = ("UNKNOWN", "ONLINE", "STALE", "OFFLINE", "CLOCK_SKEW")
# What happened to the location check of an event.
EVENT_LOCATION_STATUSES = ("CAPTURED", "FAILED", "EXCEPTION", "NOT_PROVIDED", "NOT_REQUIRED", "NOT_APPLICABLE")
SAMPLE_TYPES = ("PUNCH_IN", "LUNCH_IN", "LUNCH_OUT", "PUNCH_OUT", "PERIODIC")
# Outcome of one location attempt as reported by the browser, or MISSED when no
# scheduled reading arrived. Only CAPTURED rows ever carry coordinates.
SAMPLE_STATUSES = (
    "CAPTURED", "PERMISSION_DENIED", "POSITION_UNAVAILABLE", "TIMEOUT", "INSECURE_CONTEXT", "UNSUPPORTED",
    "ERROR", "MISSED",
)
ACCURACY_CLASSES = ("PREFERRED", "ACCEPTABLE", "LOW")
MOVEMENT_STATUSES = ("BASELINE", "WITHIN_RANGE", "UNCERTAIN", "LOW_ACCURACY", "SIGNIFICANT_MOVEMENT_REVIEW")
REVIEW_STATUSES = ("PENDING", "APPROVED", "REJECTED", "ACKNOWLEDGED")
# Sites, consents and policy documents (as_m2_sites_consents; spine §3.5, §3.12, §3.13).
SITE_KINDS = ("OFFICE", "PLANT", "STORE", "CLINIC", "WAREHOUSE", "CLIENT_SITE", "PROJECT_SITE", "OTHER")
SITE_GEOMETRY_TYPES = ("CIRCLE", "POLYGON", "NONE")
SITE_STATUSES = ("ACTIVE", "ARCHIVED")
DISTANCE_BANDS = ("INSIDE", "WITHIN_250M", "WITHIN_1KM", "BEYOND_1KM", "UNKNOWN")  # read model only (SP-AS-057)
CONSENT_PURPOSES = ("BIOMETRIC_FACE", "BIOMETRIC_DEVICE", "PUNCH_PHOTO", "LOCATION_AT_PUNCH", "LOCATION_TRACKING",
                    "NIGHT_WORK", "OVERTIME", "COMPRESSED_WEEK", "REST_WAIVER", "SCHEDULE_CHANGE_ACCEPTED",
                    "WEEKLY_HOURS_OPT_OUT")
CONSENT_SCOPES = ("STANDING", "PER_INSTANCE")
LEGAL_BASES = ("CONSENT", "EMPLOYMENT_LEGITIMATE_USE", "LEGAL_OBLIGATION", "CONTRACT")
CONSENT_STATUSES = ("GRANTED", "DECLINED", "WITHDRAWN", "EXPIRED")
CONSENT_CHANNELS = ("SELF_SERVICE", "HR_RECORDED")
POLICY_DOCUMENT_STATUSES = ("DRAFT", "PUBLISHED", "RETIRED")

LIVE_SESSION = "check_out_time IS NULL AND closed_at IS NULL AND voided_at IS NULL"


def _in(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


class CidrArray(TypeDecorator):
    """``CIDR[]``: psycopg2 sends a Python list as ``text[]``, which needs an explicit cast to ``cidr[]``."""

    impl = ARRAY(CIDR)
    cache_ok = True

    def bind_expression(self, bindvalue):
        return cast(bindvalue, ARRAY(CIDR))


def _capture_default(ctx) -> str:
    return CAPTURE_OF_SOURCE.get(ctx.get_current_parameters().get("source") or "self", "WEB")


class Attendance(TenantMixin, BaseModel):
    """One attendance session: one continuous work interval IN -> OUT with its breaks (spine §3.1).

    ``date`` is the shift date (start date of the matched shift instance in the work zone, D-AS-06; legacy rows keep
    the company-local date they were stored with). A shift date may hold several sessions (``session_seq``) for v2
    tenants that allow it; v1 tenants keep one per date in the service. A session is *live* until it has a punch-out,
    a closure (a missing punch-out detected by the system, or recorded by HR) or is voided; the database allows at
    most one live session per person and no two overlapping non-voided sessions (trigger
    ``attendance_sessions_no_overlap``, 409 ``SESSION_OVERLAP``). Readers exclude voided rows (``voided_at``).
    """

    __tablename__ = "attendance"

    employee_id = Column(Integer, nullable=False, index=True)
    check_in_time = Column(DateTime(timezone=True), nullable=False)
    check_out_time = Column(DateTime(timezone=True), nullable=True)
    # Calendar day in the company's timezone.
    date = Column(Date, nullable=False, index=True)
    shift_id = Column(Integer, nullable=True)
    face_match_score = Column(Float, nullable=True)
    is_manual = Column(Boolean, default=False, nullable=False)
    source = Column(String(20), nullable=False, default="self")  # self | kiosk | manual
    notes = Column(Text, nullable=True)
    status = Column(String(20), default="present", nullable=False, index=True)
    recorded_by_user_id = Column(Integer, nullable=True)
    # Closed without a punch-out (no end time is ever invented for it).
    closed_at = Column(DateTime(timezone=True), nullable=True)
    closed_reason = Column(String(40), nullable=True)
    # Raw minutes against the session's shift (NULL = unknown: no fixed shift, or recorded before 0016);
    # grace periods are policy and are applied by payroll, not here (3.4 §11).
    late_minutes = Column(Integer, nullable=True)
    early_exit = Column(Boolean, nullable=False, default=False, server_default="false")
    early_exit_minutes = Column(Integer, nullable=True)
    # Bumped by every change of times, status or breaks; each version is in source_change_log (3.4 §16).
    row_version = Column(Integer, nullable=False, default=1, server_default="1")
    # Periodic location tracking. The server owns the schedule; browsers only
    # follow it, so a missed reading is detected here even if no page is open.
    tracking_state = Column(String(20), nullable=False, default="NOT_APPLICABLE", server_default="NOT_APPLICABLE")
    tracking_stop_reason = Column(String(40), nullable=True)
    tracking_interval_minutes = Column(Integer, nullable=True)
    tracking_anchor_at = Column(DateTime(timezone=True), nullable=True)
    tracking_next_due_at = Column(DateTime(timezone=True), nullable=True)
    tracking_overdue_since = Column(DateTime(timezone=True), nullable=True)
    tracking_consent_at = Column(DateTime(timezone=True), nullable=True)
    last_location_at = Column(DateTime(timezone=True), nullable=True)
    # Capture v2 (as_m2_punches_sessions; spine §3.1, AS 11 §3.1).
    session_seq = Column(SmallInteger, nullable=False, default=1, server_default="1")
    # CAPTURE_SOURCES; the legacy lowercase `source` mirrors it. Never NULL for new rows (11 §11.4 step 2b): a writer
    # that sets only `source` gets the capture source of that mirror.
    capture_source = Column(String(12), nullable=False, default=lambda ctx: _capture_default(ctx))
    # End of the occupied interval: check_out_time, else LEAST(closed_at, check_in_time + max hours) once closed;
    # NULL while live (A-08). The overlap trigger reads [check_in_time, COALESCE(occupied_until, infinity)).
    occupied_until = Column(DateTime(timezone=True), nullable=True)
    legal_entity_id = Column(Integer, nullable=True)
    branch_id = Column(Integer, nullable=True)
    work_timezone = Column(String(64), nullable=True)  # zone used to compute `date`
    shift_version_id = Column(Integer, nullable=True)
    roster_entry_id = Column(Integer, nullable=True)
    roster_entry_version = Column(Integer, nullable=True)
    work_mode = Column(String(10), nullable=False, default="ON_SITE", server_default="ON_SITE")
    first_punch_id = Column(BigInteger, nullable=True)
    last_punch_id = Column(BigInteger, nullable=True)
    pairing_key = Column(String(64), nullable=True)  # device-paired sessions only (AS-M7)
    request_id = Column(Integer, nullable=True)  # FK -> attendance_requests in as_m4_requests
    voided_at = Column(DateTime(timezone=True), nullable=True)
    voided_by_user_id = Column(Integer, nullable=True)
    void_reason = Column(String(20), nullable=True)  # VOID_REASONS

    employee = relationship("Employee", primaryjoin="Attendance.employee_id == Employee.id", foreign_keys=[employee_id])

    @validates("source")
    def _check_source(self, _key, value):
        # Every writer goes through here (BUG-AS-20); the DB CHECK follows in as_m2_punches_sessions.
        if value not in ATTENDANCE_SOURCES:
            raise ValueError(f"unknown attendance source {value!r}")
        return value

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "employee_id"],
            ["employees.company_id", "employees.id"],
            name="fk_attendance_employee",
        ),
        ForeignKeyConstraint(
            ["company_id", "shift_id"], ["shifts.company_id", "shifts.id"], name="fk_attendance_shift"
        ),
        CheckConstraint(f"status IN {ATTENDANCE_STATUSES}", name="ck_attendance_status"),
        CheckConstraint(
            "check_out_time IS NULL OR check_out_time >= check_in_time", name="ck_attendance_times"
        ),
        CheckConstraint(f"tracking_state IN {TRACKING_STATES}", name="ck_attendance_tracking_state"),
        CheckConstraint("closed_at IS NULL OR check_out_time IS NULL", name="ck_attendance_closed_without_out"),
        CheckConstraint("late_minutes IS NULL OR late_minutes >= 0", name="ck_attendance_late_minutes"),
        CheckConstraint(
            "early_exit_minutes IS NULL OR early_exit_minutes >= 0", name="ck_attendance_early_exit_minutes"
        ),
        CheckConstraint("NOT early_exit OR early_exit_minutes > 0", name="ck_attendance_early_exit"),
        CheckConstraint("row_version >= 1", name="ck_attendance_row_version"),
        CheckConstraint(f"source IN {_in(ATTENDANCE_SOURCES)}", name="ck_attendance_source"),
        CheckConstraint("session_seq BETWEEN 1 AND 8", name="ck_attendance_session_seq"),
        CheckConstraint(f"capture_source IN {_in(CAPTURE_SOURCES)}", name="ck_attendance_capture_source"),
        CheckConstraint(f"work_mode IN {_in(WORK_MODES)}", name="ck_attendance_work_mode"),
        CheckConstraint(f"void_reason IS NULL OR void_reason IN {_in(VOID_REASONS)}", name="ck_attendance_void_reason"),
        CheckConstraint("(voided_at IS NULL) = (void_reason IS NULL)", name="ck_attendance_void_pair"),
        CheckConstraint("(roster_entry_id IS NULL) = (roster_entry_version IS NULL)", name="ck_attendance_roster_entry"),
        CheckConstraint("occupied_until IS NULL OR occupied_until >= check_in_time", name="ck_attendance_occupied"),
        ForeignKeyConstraint(["company_id", "legal_entity_id"], ["legal_entities.company_id", "legal_entities.id"],
                             name="fk_attendance_legal_entity"),
        ForeignKeyConstraint(["company_id", "branch_id"], ["branches.company_id", "branches.id"],
                             name="fk_attendance_branch"),
        ForeignKeyConstraint(["company_id", "shift_version_id"], ["shift_versions.company_id", "shift_versions.id"],
                             name="fk_attendance_shift_version"),
        ForeignKeyConstraint(["company_id", "roster_entry_id"], ["roster_entries.company_id", "roster_entries.id"],
                             name="fk_attendance_roster_entry"),
        ForeignKeyConstraint(["company_id", "voided_by_user_id"], ["users.company_id", "users.id"],
                             name="fk_attendance_voided_by"),
        ForeignKeyConstraint(["company_id", "first_punch_id"], ["attendance_punches.company_id", "attendance_punches.id"],
                             name="fk_attendance_first_punch", use_alter=True),
        ForeignKeyConstraint(["company_id", "last_punch_id"], ["attendance_punches.company_id", "attendance_punches.id"],
                             name="fk_attendance_last_punch", use_alter=True),
        UniqueConstraint("company_id", "id", name="uq_attendance_company_id_id"),
        Index("ix_attendance_company_date", "company_id", "date"),
        Index("ix_attendance_employee_date", "employee_id", "date"),
        Index("ix_attendance_employee_checkin", "company_id", "employee_id", "check_in_time"),
        # Several sessions per shift date, numbered; one number per date among non-voided rows (D-AS-05).
        Index(
            "uq_attendance_employee_day_seq", "company_id", "employee_id", "date", "session_seq",
            unique=True, postgresql_where=text("voided_at IS NULL"),
        ),
        Index(
            "uq_attendance_one_live_session", "company_id", "employee_id",
            unique=True, postgresql_where=text(LIVE_SESSION),
        ),
        Index(
            "ix_attendance_tracking_due", "company_id", "tracking_next_due_at",
            postgresql_where=text("tracking_state = 'ACTIVE'"),
        ),
    )


class AttendanceBreak(TenantMixin, BaseModel):
    """A lunch interval inside one session. Overlap and containment are enforced by a trigger."""

    __tablename__ = "attendance_breaks"

    attendance_id = Column(Integer, nullable=False)
    employee_id = Column(Integer, nullable=False)
    kind = Column(String(20), nullable=False, default="LUNCH")
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    # Snapshot of the lunch pay policy when the break started.
    is_paid = Column(Boolean, nullable=False, default=False)
    end_reason = Column(String(40), nullable=True)  # LUNCH_OUT | AUTO_CLOSED_AT_PUNCH_OUT | CORRECTED
    created_by_user_id = Column(Integer, nullable=True)
    source = Column(String(10), nullable=False, default="PUNCHED", server_default="PUNCHED")  # BREAK_SOURCES
    break_rule_index = Column(SmallInteger, nullable=True)
    start_punch_id = Column(BigInteger, nullable=True)
    end_punch_id = Column(BigInteger, nullable=True)

    __table_args__ = (
        CheckConstraint(f"source IN {_in(BREAK_SOURCES)}", name="ck_breaks_source"),
        CheckConstraint("break_rule_index IS NULL OR break_rule_index BETWEEN 0 AND 3", name="ck_breaks_rule_index"),
        ForeignKeyConstraint(["company_id", "start_punch_id"], ["attendance_punches.company_id", "attendance_punches.id"],
                             name="fk_breaks_start_punch", use_alter=True),
        ForeignKeyConstraint(["company_id", "end_punch_id"], ["attendance_punches.company_id", "attendance_punches.id"],
                             name="fk_breaks_end_punch", use_alter=True),
        ForeignKeyConstraint(
            ["company_id", "attendance_id"], ["attendance.company_id", "attendance.id"], name="fk_breaks_attendance"
        ),
        ForeignKeyConstraint(
            ["company_id", "employee_id"], ["employees.company_id", "employees.id"], name="fk_breaks_employee"
        ),
        CheckConstraint(f"kind IN {BREAK_KINDS}", name="ck_breaks_kind"),
        CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="ck_breaks_window"),
        Index("ix_attendance_breaks_session", "company_id", "attendance_id"),
        Index("uq_attendance_breaks_one_open", "attendance_id", unique=True, postgresql_where=text("ended_at IS NULL")),
    )


class AttendanceEvent(TenantMixin, BaseModel):
    """Append-only log of attendance actions with server-authoritative times.

    The database rejects UPDATE and DELETE on this table (trigger), so the
    original punches survive any later correction, which is itself an event.
    """

    __tablename__ = "attendance_events"

    attendance_id = Column(Integer, nullable=False)
    employee_id = Column(Integer, nullable=False)
    event_type = Column(String(20), nullable=False)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    source = Column(String(20), nullable=False)
    actor_user_id = Column(Integer, nullable=True)
    idempotency_key = Column(String(64), nullable=True)
    face_match_score = Column(Float, nullable=True)
    location_status = Column(String(20), nullable=False)
    note = Column(String(500), nullable=True)
    details = Column(Text, nullable=True)  # JSON (corrections: what changed)
    punch_id = Column(BigInteger, nullable=True)  # the raw punch of an interactive action (as_m2_punches_sessions)
    request_id = Column(Integer, nullable=True)  # FK -> attendance_requests in as_m4_requests

    __table_args__ = (
        ForeignKeyConstraint(["company_id", "punch_id"], ["attendance_punches.company_id", "attendance_punches.id"],
                             name="fk_events_punch", use_alter=True),
        ForeignKeyConstraint(
            ["company_id", "attendance_id"], ["attendance.company_id", "attendance.id"], name="fk_events_attendance"
        ),
        ForeignKeyConstraint(
            ["company_id", "employee_id"], ["employees.company_id", "employees.id"], name="fk_events_employee"
        ),
        CheckConstraint(f"event_type IN {EVENT_TYPES}", name="ck_events_type"),
        CheckConstraint(f"source IN {EVENT_SOURCES}", name="ck_events_source"),
        CheckConstraint(f"location_status IN {EVENT_LOCATION_STATUSES}", name="ck_events_location_status"),
        UniqueConstraint("company_id", "id", name="uq_attendance_events_company_id_id"),
        Index("ix_attendance_events_session", "company_id", "attendance_id", "occurred_at"),
        Index(
            "uq_attendance_events_idempotency", "company_id", "employee_id", "idempotency_key",
            unique=True, postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )


class LocationSample(TenantMixin, BaseModel):
    """One browser location attempt (event-time or periodic), or a missed periodic slot.

    Coordinates are the browser's estimate with its own 95% accuracy radius;
    failed attempts and missed slots are stored as such and never carry a
    position. Precise coordinates are redacted after the company's retention.
    """

    __tablename__ = "location_samples"

    attendance_id = Column(Integer, nullable=False)
    employee_id = Column(Integer, nullable=False)
    event_id = Column(Integer, nullable=True)
    sample_type = Column(String(20), nullable=False)
    status = Column(String(30), nullable=False)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    accuracy_m = Column(Float, nullable=True)
    captured_at = Column(DateTime(timezone=True), nullable=True)  # device clock (informational)
    client_sent_at = Column(DateTime(timezone=True), nullable=True)  # device clock (informational)
    received_at = Column(DateTime(timezone=True), nullable=False)  # server clock (authoritative)
    fix_age_seconds = Column(Integer, nullable=True)
    clock_skew_seconds = Column(Integer, nullable=True)
    slot_due_at = Column(DateTime(timezone=True), nullable=True)
    is_recovery = Column(Boolean, nullable=False, default=False)
    attempts = Column(SmallInteger, nullable=True)
    accuracy_class = Column(String(20), nullable=True)
    movement_status = Column(String(30), nullable=True)
    distance_from_punch_in_m = Column(Float, nullable=True)
    distance_from_previous_m = Column(Float, nullable=True)
    uncertainty_m = Column(Float, nullable=True)
    error_message = Column(String(200), nullable=True)
    exception_reason = Column(String(500), nullable=True)
    review_status = Column(String(20), nullable=True)
    reviewed_by_user_id = Column(Integer, nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    review_note = Column(String(500), nullable=True)
    idempotency_key = Column(String(64), nullable=True)
    redacted_at = Column(DateTime(timezone=True), nullable=True)
    # Capture v2 (as_m2_sites_consents, AS 11 §3.4): the punch this event reading belongs to and its site check.
    punch_id = Column(BigInteger, nullable=True)
    site_id = Column(Integer, nullable=True)
    distance_to_site_m = Column(Numeric(9, 1), nullable=True)
    geofence_result = Column(String(12), nullable=True)
    mock_location = Column(Boolean, nullable=True)  # NULL = unknown (browsers never report it)

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "attendance_id"], ["attendance.company_id", "attendance.id"], name="fk_samples_attendance"
        ),
        ForeignKeyConstraint(["company_id", "punch_id"], ["attendance_punches.company_id", "attendance_punches.id"],
                             name="fk_samples_punch", use_alter=True),  # punches <-> samples reference each other
        ForeignKeyConstraint(["company_id", "site_id"], ["attendance_sites.company_id", "attendance_sites.id"],
                             name="fk_samples_site"),
        CheckConstraint(f"geofence_result IS NULL OR geofence_result IN {_in(GEOFENCE_RESULTS)}",
                        name="ck_samples_geofence"),
        Index("ix_location_samples_punch", "company_id", "punch_id", postgresql_where=text("punch_id IS NOT NULL")),
        ForeignKeyConstraint(
            ["company_id", "employee_id"], ["employees.company_id", "employees.id"], name="fk_samples_employee"
        ),
        ForeignKeyConstraint(
            ["company_id", "event_id"], ["attendance_events.company_id", "attendance_events.id"], name="fk_samples_event"
        ),
        CheckConstraint(f"sample_type IN {SAMPLE_TYPES}", name="ck_samples_type"),
        CheckConstraint(f"status IN {SAMPLE_STATUSES}", name="ck_samples_status"),
        CheckConstraint("latitude IS NULL OR latitude BETWEEN -90 AND 90", name="ck_samples_latitude"),
        CheckConstraint("longitude IS NULL OR longitude BETWEEN -180 AND 180", name="ck_samples_longitude"),
        CheckConstraint("accuracy_m IS NULL OR accuracy_m > 0", name="ck_samples_accuracy"),
        # No fabricated points: only captured readings may carry coordinates...
        CheckConstraint("status = 'CAPTURED' OR (latitude IS NULL AND longitude IS NULL)", name="ck_samples_no_position_on_failure"),
        # ...and a captured reading keeps them until retention redacts it.
        CheckConstraint(
            "status <> 'CAPTURED' OR redacted_at IS NOT NULL OR "
            "(latitude IS NOT NULL AND longitude IS NOT NULL AND accuracy_m IS NOT NULL)",
            name="ck_samples_captured_has_position",
        ),
        CheckConstraint(f"accuracy_class IS NULL OR accuracy_class IN {ACCURACY_CLASSES}", name="ck_samples_accuracy_class"),
        CheckConstraint(f"movement_status IS NULL OR movement_status IN {MOVEMENT_STATUSES}", name="ck_samples_movement"),
        CheckConstraint(f"review_status IS NULL OR review_status IN {REVIEW_STATUSES}", name="ck_samples_review"),
        Index("ix_location_samples_session", "company_id", "attendance_id", "received_at"),
        Index("ix_location_samples_employee", "company_id", "employee_id", "received_at"),
        Index("ix_location_samples_review", "company_id", "review_status", postgresql_where=text("review_status = 'PENDING'")),
        Index(
            "uq_location_samples_idempotency", "company_id", "employee_id", "idempotency_key",
            unique=True, postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        # One outcome (a reading or a miss) per scheduled slot, whichever tab or retry gets there first.
        Index(
            "uq_location_samples_slot", "attendance_id", "slot_due_at",
            unique=True, postgresql_where=text("sample_type = 'PERIODIC' AND status IN ('CAPTURED', 'MISSED')"),
        ),
        Index("uq_location_samples_event", "event_id", unique=True, postgresql_where=text("event_id IS NOT NULL")),
        # Tenant key for the composite references of punches and review items (as_m2_punches_sessions).
        UniqueConstraint("company_id", "id", name="uq_location_samples_company_id_id"),
    )


# ------------------------------------------------------------------------------------------- capture v2 (AS-M2)
class AttendanceDevice(TenantMixin, BaseModel):
    """A registered capture device (spine §3.6, AS 11 §4.7). AS-M2 uses KIOSK devices with a device token.

    A KIOSK is created ``PENDING_ACTIVATION`` with a one-time claim code (hash + 24 h expiry, shown once); pairing at
    ``/kiosk/pair`` activates it and stores the hash of the device token (cookie ``Path=/api/v1/attendance/kiosk``).
    Suspend / retire clear ``token_hash`` (K-3). Secrets are only ever stored as hashes.
    """

    __tablename__ = "attendance_devices"

    code = Column(String(40), nullable=False)
    name = Column(String(100), nullable=False)
    kind = Column(String(10), nullable=False)
    vendor = Column(String(10), nullable=False)
    integration = Column(String(8), nullable=False)
    serial_number = Column(String(64), nullable=True)
    branch_id = Column(Integer, nullable=True)
    site_id = Column(Integer, nullable=True)  # FK -> attendance_sites (as_m2_sites_consents)
    timezone = Column(String(64), nullable=False)
    direction_mode = Column(String(12), nullable=False, default="FIRST_LAST", server_default="FIRST_LAST")
    device_role = Column(String(8), nullable=False, default="IN_OUT", server_default="IN_OUT")
    status = Column(String(20), nullable=False, default="PENDING_ACTIVATION", server_default="PENDING_ACTIVATION")
    health = Column(String(12), nullable=False, default="UNKNOWN", server_default="UNKNOWN")
    secret_hash = Column(String(64), nullable=True)
    secret_enc = Column(LargeBinary, nullable=True)
    secret_key_id = Column(String(32), nullable=True)
    token_hash = Column(String(64), nullable=True)
    token_expires_at = Column(DateTime(timezone=True), nullable=True)
    claim_code_hash = Column(String(64), nullable=True)
    claim_code_expires_at = Column(DateTime(timezone=True), nullable=True)
    claim_code_failed_attempts = Column(SmallInteger, nullable=False, default=0, server_default="0")
    comm_key_ref = Column(String(64), nullable=True)
    firmware = Column(String(32), nullable=True)
    protocol_version = Column(String(32), nullable=True)
    protocol_state = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    backfill_from = Column(Date, nullable=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    last_punch_at = Column(DateTime(timezone=True), nullable=True)
    watermark_at = Column(DateTime(timezone=True), nullable=True)
    last_clock_skew_seconds = Column(Integer, nullable=True)
    settings = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    activated_at = Column(DateTime(timezone=True), nullable=True)
    activated_by_user_id = Column(Integer, nullable=True)
    retired_at = Column(DateTime(timezone=True), nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_attendance_devices_company_id_id"),
        UniqueConstraint("company_id", "code", name="uq_attendance_devices_code"),
        ForeignKeyConstraint(["company_id", "branch_id"], ["branches.company_id", "branches.id"],
                             name="fk_attendance_devices_branch"),
        ForeignKeyConstraint(["company_id", "activated_by_user_id"], ["users.company_id", "users.id"],
                             name="fk_attendance_devices_activated_by"),
        ForeignKeyConstraint(["company_id", "created_by_user_id"], ["users.company_id", "users.id"],
                             name="fk_attendance_devices_created_by"),
        ForeignKeyConstraint(["company_id", "site_id"], ["attendance_sites.company_id", "attendance_sites.id"],
                             name="fk_attendance_devices_site"),
        CheckConstraint(f"kind IN {_in(DEVICE_KINDS)}", name="ck_attendance_devices_kind"),
        CheckConstraint(f"vendor IN {_in(DEVICE_VENDORS)}", name="ck_attendance_devices_vendor"),
        CheckConstraint(f"integration IN {_in(DEVICE_INTEGRATIONS)}", name="ck_attendance_devices_integration"),
        CheckConstraint(f"direction_mode IN {_in(DIRECTION_MODES)}", name="ck_attendance_devices_direction_mode"),
        CheckConstraint(f"device_role IN {_in(DEVICE_ROLES)}", name="ck_attendance_devices_role"),
        CheckConstraint(f"status IN {_in(DEVICE_STATUSES)}", name="ck_attendance_devices_status"),
        CheckConstraint(f"health IN {_in(DEVICE_HEALTH)}", name="ck_attendance_devices_health"),
        CheckConstraint("status <> 'ACTIVE' OR activated_at IS NOT NULL", name="ck_attendance_devices_activated"),
        CheckConstraint("(status = 'RETIRED') = (retired_at IS NOT NULL)", name="ck_attendance_devices_retired"),
        CheckConstraint("claim_code_failed_attempts >= 0", name="ck_attendance_devices_claim_failures"),
        CheckConstraint("row_version >= 1", name="ck_attendance_devices_row_version"),
        Index("uq_attendance_devices_serial", "company_id", "serial_number", unique=True,
              postgresql_where=text("retired_at IS NULL AND serial_number IS NOT NULL")),
        Index("ix_attendance_devices_token_hash", "token_hash", postgresql_where=text("token_hash IS NOT NULL")),
    )


class AttendancePunch(TenantMixin, BigIdModel):
    """The raw punch log of every capture source (GUARD; spine §3.2, AS 11 §4.1, D-AS-04).

    Written for every interactive punch, also for v1 tenants (the shadow input of reconciliation, spine §15.7).
    A punch is never edited or deleted: trigger ``attendance_punches_guard`` allows only the processing columns
    (``employee_id`` NULL -> value, ``mapping_status``, ``disposition``, ``session_id``, ``shift_date``, the photo
    redaction pair) to change, and the app role holds UPDATE on those columns only (app/cli.py).
    """

    __tablename__ = "attendance_punches"

    employee_id = Column(Integer, nullable=True)
    source = Column(String(12), nullable=False)
    direction = Column(String(12), nullable=False)
    direction_raw = Column(String(16), nullable=True)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    device_local_ts = Column(DateTime(timezone=False), nullable=True)
    device_tz = Column(String(64), nullable=True)
    received_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    client_sent_at = Column(DateTime(timezone=True), nullable=True)
    device_id = Column(Integer, nullable=True)
    device_user_id = Column(String(32), nullable=True)
    verify_mode = Column(String(10), nullable=True)
    verify_raw = Column(SmallInteger, nullable=True)
    work_code = Column(String(16), nullable=True)
    site_id = Column(Integer, nullable=True)  # FK -> attendance_sites (as_m2_sites_consents)
    geofence_result = Column(String(12), nullable=False, default="NOT_CHECKED", server_default="NOT_CHECKED")
    location_sample_id = Column(Integer, nullable=True)
    ip_hash = Column(String(64), nullable=True)
    face_match_score = Column(Float, nullable=True)
    photo_storage_path = Column(Text, nullable=True)
    redacted_at = Column(DateTime(timezone=True), nullable=True)
    evidence_flags = Column(ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]"))
    natural_key = Column(String(64), nullable=False)
    payload_hash = Column(String(64), nullable=True)
    ingest_batch_id = Column(String(64), nullable=True)
    idempotency_key = Column(String(64), nullable=True)
    mapping_status = Column(String(10), nullable=False)
    disposition = Column(String(20), nullable=False, default="PENDING", server_default="PENDING")
    session_id = Column(Integer, nullable=True)
    shift_date = Column(Date, nullable=True)
    actor_user_id = Column(Integer, nullable=True)
    request_id = Column(Integer, nullable=True)  # FK -> attendance_requests in as_m4_requests
    reason = Column(String(500), nullable=True)  # internal (SR-AS-19)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_attendance_punches_company_id_id"),
        UniqueConstraint("company_id", "natural_key", name="uq_attendance_punches_natural_key"),
        ForeignKeyConstraint(["company_id", "employee_id"], ["employees.company_id", "employees.id"],
                             name="fk_attendance_punches_employee"),
        ForeignKeyConstraint(["company_id", "device_id"], ["attendance_devices.company_id", "attendance_devices.id"],
                             name="fk_attendance_punches_device"),
        ForeignKeyConstraint(["company_id", "location_sample_id"], ["location_samples.company_id", "location_samples.id"],
                             name="fk_attendance_punches_location_sample"),
        ForeignKeyConstraint(["company_id", "session_id"], ["attendance.company_id", "attendance.id"],
                             name="fk_attendance_punches_session"),
        ForeignKeyConstraint(["company_id", "actor_user_id"], ["users.company_id", "users.id"],
                             name="fk_attendance_punches_actor"),
        ForeignKeyConstraint(["company_id", "site_id"], ["attendance_sites.company_id", "attendance_sites.id"],
                             name="fk_attendance_punches_site"),
        CheckConstraint(f"source IN {_in(CAPTURE_SOURCES)}", name="ck_attendance_punches_source"),
        CheckConstraint(f"direction IN {_in(PUNCH_DIRECTIONS)}", name="ck_attendance_punches_direction"),
        CheckConstraint("source IN ('DEVICE', 'IMPORT') OR direction <> 'UNKNOWN'",
                        name="ck_attendance_punches_known_direction"),
        CheckConstraint("source <> 'DEVICE' OR (device_id IS NOT NULL AND device_user_id IS NOT NULL "
                        "AND device_local_ts IS NOT NULL AND device_tz IS NOT NULL)",
                        name="ck_attendance_punches_device"),
        CheckConstraint(f"verify_mode IS NULL OR verify_mode IN {_in(VERIFY_MODES)}",
                        name="ck_attendance_punches_verify_mode"),
        CheckConstraint(f"geofence_result IN {_in(GEOFENCE_RESULTS)}", name="ck_attendance_punches_geofence"),
        CheckConstraint(f"evidence_flags <@ ARRAY{list(EVIDENCE_FLAGS)}::text[]", name="ck_attendance_punches_flags"),
        CheckConstraint(f"mapping_status IN {_in(MAPPING_STATUSES)}", name="ck_attendance_punches_mapping"),
        CheckConstraint("(mapping_status = 'MAPPED') = (employee_id IS NOT NULL)",
                        name="ck_attendance_punches_mapped_employee"),
        CheckConstraint(f"disposition IN {_in(PUNCH_DISPOSITIONS)}", name="ck_attendance_punches_disposition"),
        CheckConstraint("source IN ('MANUAL', 'SUPERVISOR', 'REQUEST') OR (actor_user_id IS NULL AND request_id IS NULL "
                        "AND reason IS NULL)", name="ck_attendance_punches_actor"),
        CheckConstraint("(photo_storage_path IS NULL) OR (redacted_at IS NULL)", name="ck_attendance_punches_photo"),
        Index("ix_attendance_punches_employee_time", "company_id", "employee_id", "occurred_at"),
        Index("ix_attendance_punches_unmapped", "company_id", "device_id", "device_user_id",
              postgresql_where=text("mapping_status <> 'MAPPED'")),
        Index("ix_attendance_punches_pending", "company_id", "received_at",
              postgresql_where=text("disposition = 'PENDING'")),
        Index("ix_attendance_punches_device_time", "company_id", "device_id", "occurred_at",
              postgresql_where=text("device_id IS NOT NULL")),
        Index("ix_attendance_punches_received", "company_id", "received_at"),
        Index("ix_attendance_punches_session", "company_id", "session_id"),
    )


class AttendanceReviewItem(TenantMixin, BaseModel):
    """One item of the attendance review queue (spine §3.12, §4.10; AS 01 §11).

    A signal never changes status or pay by itself; it opens at most one OPEN item per ``dedup_key`` (partial unique
    index, producers insert-or-ignore). Location kinds mirror ``location_samples.review_status``.
    """

    __tablename__ = "attendance_review_items"

    kind = Column(String(40), nullable=False)
    employee_id = Column(Integer, nullable=True)
    shift_date = Column(Date, nullable=True)
    punch_id = Column(BigInteger, nullable=True)
    session_id = Column(Integer, nullable=True)
    location_sample_id = Column(Integer, nullable=True)
    device_id = Column(Integer, nullable=True)
    device_user_id = Column(String(32), nullable=True)
    contractor_id = Column(Integer, nullable=True)  # FK in as_m10_packs_contractors
    severity = Column(String(8), nullable=False)
    status = Column(String(12), nullable=False, default="OPEN", server_default="OPEN")
    dedup_key = Column(String(80), nullable=False)
    decided_by_user_id = Column(Integer, nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    note = Column(String(500), nullable=True)
    row_version = Column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_attendance_review_items_company_id_id"),
        ForeignKeyConstraint(["company_id", "employee_id"], ["employees.company_id", "employees.id"],
                             name="fk_attendance_review_items_employee"),
        ForeignKeyConstraint(["company_id", "punch_id"], ["attendance_punches.company_id", "attendance_punches.id"],
                             name="fk_attendance_review_items_punch"),
        ForeignKeyConstraint(["company_id", "session_id"], ["attendance.company_id", "attendance.id"],
                             name="fk_attendance_review_items_session"),
        ForeignKeyConstraint(["company_id", "location_sample_id"], ["location_samples.company_id", "location_samples.id"],
                             name="fk_attendance_review_items_sample"),
        ForeignKeyConstraint(["company_id", "device_id"], ["attendance_devices.company_id", "attendance_devices.id"],
                             name="fk_attendance_review_items_device"),
        ForeignKeyConstraint(["company_id", "decided_by_user_id"], ["users.company_id", "users.id"],
                             name="fk_attendance_review_items_decided_by"),
        CheckConstraint(f"kind IN {_in(REVIEW_KINDS)}", name="ck_attendance_review_items_kind"),
        CheckConstraint(f"severity IN {_in(REVIEW_SEVERITIES)}", name="ck_attendance_review_items_severity"),
        CheckConstraint(f"status IN {_in(REVIEW_ITEM_STATUSES)}", name="ck_attendance_review_items_status"),
        CheckConstraint("(status = 'OPEN') = (decided_at IS NULL)", name="ck_attendance_review_items_decided"),
        CheckConstraint("row_version >= 1", name="ck_attendance_review_items_row_version"),
        Index("uq_attendance_review_items_open", "company_id", "dedup_key", unique=True,
              postgresql_where=text("status = 'OPEN'")),
        Index("ix_attendance_review_items_queue", "company_id", "status", "kind", "created_at"),
    )


class AttendanceSite(TenantMixin, BaseModel):
    """A workplace for geofence, IP and QR checks (spine §3.5, AS 11 §4.6; owner 04-attendance/01 §9.1, D-AS-27).

    CIRCLE = centre + radius (DB CHECK >= 25 m, the service enforces ``attendance.geofence.min_radius_m``); POLYGON =
    a closed ring of [lat, lng] points (<= 64, not self-intersecting); NONE = IP ranges or QR only. ``qr_secret_hash``
    is the SHA-256 of the current poster secret (shown once at rotation). Sites are archived, never deleted; punches
    keep their ``site_id``.
    """

    __tablename__ = "attendance_sites"

    code = Column(String(30), nullable=False)
    name = Column(String(100), nullable=False)
    kind = Column(String(14), nullable=False)
    legal_entity_id = Column(Integer, nullable=True)
    branch_id = Column(Integer, nullable=True)
    timezone = Column(String(64), nullable=True)
    geometry_type = Column(String(8), nullable=False)
    center_lat = Column(Numeric(9, 6), nullable=True)
    center_lng = Column(Numeric(9, 6), nullable=True)
    radius_m = Column(Integer, nullable=True)
    polygon = Column(JSONB(none_as_null=True), nullable=True)
    ip_ranges = Column(CidrArray, nullable=True)
    qr_secret_hash = Column(String(64), nullable=True)
    outdoor = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    status = Column(String(10), nullable=False, default="ACTIVE", server_default="ACTIVE")
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_attendance_sites_company_id_id"),
        UniqueConstraint("company_id", "code", name="uq_attendance_sites_code"),
        ForeignKeyConstraint(["company_id", "legal_entity_id"], ["legal_entities.company_id", "legal_entities.id"],
                             name="fk_attendance_sites_legal_entity"),
        ForeignKeyConstraint(["company_id", "branch_id"], ["branches.company_id", "branches.id"],
                             name="fk_attendance_sites_branch"),
        ForeignKeyConstraint(["company_id", "created_by_user_id"], ["users.company_id", "users.id"],
                             name="fk_attendance_sites_created_by"),
        CheckConstraint(f"kind IN {_in(SITE_KINDS)}", name="ck_attendance_sites_kind"),
        CheckConstraint(f"geometry_type IN {_in(SITE_GEOMETRY_TYPES)}", name="ck_attendance_sites_geometry"),
        CheckConstraint(f"status IN {_in(SITE_STATUSES)}", name="ck_attendance_sites_status"),
        CheckConstraint("center_lat IS NULL OR center_lat BETWEEN -90 AND 90", name="ck_attendance_sites_lat"),
        CheckConstraint("center_lng IS NULL OR center_lng BETWEEN -180 AND 180", name="ck_attendance_sites_lng"),
        CheckConstraint("radius_m IS NULL OR radius_m >= 25", name="ck_attendance_sites_radius"),
        CheckConstraint("geometry_type <> 'CIRCLE' OR (center_lat IS NOT NULL AND center_lng IS NOT NULL "
                        "AND radius_m IS NOT NULL)", name="ck_attendance_sites_circle"),
        CheckConstraint("geometry_type <> 'POLYGON' OR polygon IS NOT NULL", name="ck_attendance_sites_polygon"),
        CheckConstraint("qr_secret_hash IS NULL OR qr_secret_hash ~ '^[0-9a-f]{64}$'", name="ck_attendance_sites_qr"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_attendance_sites_dates"),
        CheckConstraint("row_version >= 1", name="ck_attendance_sites_row_version"),
        Index("ix_attendance_sites_branch", "company_id", "branch_id", "status"),
    )


class AttendanceConsent(TenantMixin, BaseModel):
    """The consent and notice ledger (AO; spine §3.12, AS 11 §4.12; rules AS 07 §5).

    Every change is a new row (trigger ``attendance_consents_guard``; the app role holds SELECT, INSERT); the latest
    row per (employee, purpose) — per (employee, purpose, ``subject_ref``) for PER_INSTANCE — is the state.
    """

    __tablename__ = "attendance_consents"

    employee_id = Column(Integer, nullable=False)
    purpose = Column(String(28), nullable=False)
    scope = Column(String(14), nullable=False, default="STANDING", server_default="STANDING")
    subject_ref = Column(JSONB(none_as_null=True), nullable=True)
    legal_basis = Column(String(28), nullable=False)
    notice_version = Column(String(32), nullable=False)
    status = Column(String(10), nullable=False)
    valid_from = Column(Date, nullable=False)
    valid_to = Column(Date, nullable=True)
    notice_period_days = Column(SmallInteger, nullable=True)
    channel = Column(String(14), nullable=False)
    recorded_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_attendance_consents_company_id_id"),
        ForeignKeyConstraint(["company_id", "employee_id"], ["employees.company_id", "employees.id"],
                             name="fk_attendance_consents_employee"),
        ForeignKeyConstraint(["company_id", "recorded_by_user_id"], ["users.company_id", "users.id"],
                             name="fk_attendance_consents_recorded_by"),
        CheckConstraint(f"purpose IN {_in(CONSENT_PURPOSES)}", name="ck_attendance_consents_purpose"),
        CheckConstraint(f"scope IN {_in(CONSENT_SCOPES)}", name="ck_attendance_consents_scope"),
        CheckConstraint("(scope = 'PER_INSTANCE') = (subject_ref IS NOT NULL)", name="ck_attendance_consents_subject"),
        CheckConstraint(f"legal_basis IN {_in(LEGAL_BASES)}", name="ck_attendance_consents_basis"),
        CheckConstraint(f"status IN {_in(CONSENT_STATUSES)}", name="ck_attendance_consents_status"),
        CheckConstraint(f"channel IN {_in(CONSENT_CHANNELS)}", name="ck_attendance_consents_channel"),
        CheckConstraint("channel <> 'HR_RECORDED' OR recorded_by_user_id IS NOT NULL", name="ck_attendance_consents_hr"),
        CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name="ck_attendance_consents_dates"),
        CheckConstraint("notice_period_days IS NULL OR notice_period_days >= 0", name="ck_attendance_consents_notice"),
        Index("ix_attendance_consents_state", "company_id", "employee_id", "purpose", text("created_at DESC"),
              text("id DESC")),
        Index("ix_attendance_consents_expiry", "company_id", "valid_to",
              postgresql_where=text("status = 'GRANTED' AND valid_to IS NOT NULL")),
    )


class AttendancePolicyDocument(TenantMixin, BaseModel):
    """A versioned privacy notice or monitoring policy (AS 07 §5.3, AS 11 §4.12). ``purposes`` names the consent
    purposes the notice describes; ``document_attachment_id`` gets its foreign key in as_m4_requests (Q-11-04)."""

    __tablename__ = "attendance_policy_documents"

    code = Column(String(40), nullable=False)
    version = Column(String(16), nullable=False)
    title = Column(String(150), nullable=False)
    legal_entity_id = Column(Integer, nullable=True)
    document_attachment_id = Column(Integer, nullable=True)
    effective_from = Column(Date, nullable=False)
    requires_acknowledgement = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    purposes = Column(ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]"))
    summary = Column(String(2000), nullable=True)
    privacy_contact = Column(String(200), nullable=True)
    status = Column(String(10), nullable=False, default="DRAFT", server_default="DRAFT")
    published_at = Column(DateTime(timezone=True), nullable=True)
    row_version = Column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_attendance_policy_documents_company_id_id"),
        UniqueConstraint("company_id", "code", "version", name="uq_attendance_policy_documents_version"),
        ForeignKeyConstraint(["company_id", "legal_entity_id"], ["legal_entities.company_id", "legal_entities.id"],
                             name="fk_attendance_policy_documents_legal_entity"),
        CheckConstraint(f"status IN {_in(POLICY_DOCUMENT_STATUSES)}", name="ck_attendance_policy_documents_status"),
        CheckConstraint(f"purposes <@ ARRAY{list(CONSENT_PURPOSES)}::text[]",
                        name="ck_attendance_policy_documents_purposes"),
        CheckConstraint("status = 'DRAFT' OR published_at IS NOT NULL", name="ck_attendance_policy_documents_published"),
        CheckConstraint("row_version >= 1", name="ck_attendance_policy_documents_row_version"),
    )


class AttendancePolicyAcknowledgement(TenantMixin, BaseModel):
    """One employee's acknowledgement of one policy document version (AO; AS 11 §4.12)."""

    __tablename__ = "attendance_policy_acknowledgements"

    document_id = Column(Integer, nullable=False)
    employee_id = Column(Integer, nullable=False)
    acknowledged_at = Column(DateTime(timezone=True), nullable=False)
    channel = Column(String(14), nullable=False)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_attendance_policy_acknowledgements_company_id_id"),
        UniqueConstraint("company_id", "document_id", "employee_id", name="uq_attendance_policy_acknowledgements_once"),
        ForeignKeyConstraint(["company_id", "document_id"],
                             ["attendance_policy_documents.company_id", "attendance_policy_documents.id"],
                             name="fk_attendance_policy_acknowledgements_document"),
        ForeignKeyConstraint(["company_id", "employee_id"], ["employees.company_id", "employees.id"],
                             name="fk_attendance_policy_acknowledgements_employee"),
        CheckConstraint(f"channel IN {_in(CONSENT_CHANNELS)}", name="ck_attendance_policy_acknowledgements_channel"),
        Index("ix_attendance_policy_acknowledgements_employee", "company_id", "employee_id"),
    )


# --------------------------------------------------------------------------- AS-M3 evaluations (as_m3_days_policies)

from app.services.day_evaluation import (  # noqa: E402  re-exported spine vocabularies (AS 11 §5)
    DAY_RESULTS, EVALUATION_STATES, EVALUATION_TRIGGERS, EXCEPTION_CODES, EXCUSE_SOURCES, PENDING_REASONS,
    PORTION_RESULTS, PRESENCE_BASES, RULES_MODES,
)

EXPECTED_PORTIONS = ("FULL", "FIRST_HALF", "SECOND_HALF", "NONE")
EVALUATED_BY = ("SYSTEM", "USER")
DAY_SHIFT_SOURCES = ("ROSTER", "RULE", "PLACEMENT", "NONE")


class AttendanceDay(TenantMixin, BigIdModel):
    """One evaluation per employee and shift date (AS 11 §4.2; spine §3.3; D-AS-08).

    Derived and rebuildable: written only by ``attendance_days`` (the evaluation write path, 04-attendance/03 §8.3)
    from the pure ``day_evaluation.evaluate_day``. Payroll reads FINAL / PROVISIONAL rows of v2 tenants through
    ``DAY_RESULT_TO_LEDGER`` (JC-AS-01); ``(id, version, content_hash)`` is the payroll lineage (INV-21).
    """

    __tablename__ = "attendance_days"

    employee_id = Column(Integer, nullable=False)
    shift_date = Column(Date, nullable=False)
    legal_entity_id = Column(Integer, nullable=True)
    branch_id = Column(Integer, nullable=True)
    work_timezone = Column(String(64), nullable=False)
    evaluation_state = Column(String(12), nullable=False)
    rules_mode = Column(String(10), nullable=False)
    version = Column(Integer, nullable=False, default=1, server_default="1")
    row_version = Column(Integer, nullable=False, default=1, server_default="1")
    inputs_hash = Column(String(64), nullable=False)
    content_hash = Column(String(64), nullable=False)
    policy_version_ids = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    calendar_version = Column(String(64), nullable=True)
    work_source = Column(String(20), nullable=True)
    shift_source = Column(String(10), nullable=True)
    shift_id = Column(Integer, nullable=True)
    shift_version_id = Column(Integer, nullable=True)
    roster_entry_id = Column(Integer, nullable=True)
    roster_version = Column(Integer, nullable=True)
    expected_portion = Column(String(12), nullable=False)
    expected_minutes = Column(Integer, nullable=True)
    expected_start_at = Column(DateTime(timezone=True), nullable=True)
    expected_end_at = Column(DateTime(timezone=True), nullable=True)
    session_count = Column(SmallInteger, nullable=False, default=0, server_default="0")
    first_in_at = Column(DateTime(timezone=True), nullable=True)
    last_out_at = Column(DateTime(timezone=True), nullable=True)
    gross_seconds = Column(Integer, nullable=True)
    net_seconds = Column(Integer, nullable=True)
    gross_minutes = Column(Integer, nullable=True)
    break_minutes = Column(Integer, nullable=True)
    unpaid_break_minutes = Column(Integer, nullable=True)
    net_minutes = Column(Integer, nullable=True)
    auto_deducted_minutes = Column(Integer, nullable=False, default=0, server_default="0")
    rounding_delta_minutes = Column(Integer, nullable=False, default=0, server_default="0")
    late_minutes = Column(Integer, nullable=True)
    early_minutes = Column(Integer, nullable=True)
    short_minutes = Column(Integer, nullable=True)
    late_mark = Column(Boolean, nullable=False, default=False, server_default="false")
    late_excused = Column(Boolean, nullable=False, default=False, server_default="false")
    late_excuse_source = Column(String(8), nullable=True)
    early_mark = Column(Boolean, nullable=False, default=False, server_default="false")
    early_excused = Column(Boolean, nullable=False, default=False, server_default="false")
    early_excuse_source = Column(String(8), nullable=True)
    short_mark = Column(Boolean, nullable=False, default=False, server_default="false")
    late_converted = Column(Boolean, nullable=False, default=False, server_default="false")
    early_converted = Column(Boolean, nullable=False, default=False, server_default="false")
    night_minutes = Column(Integer, nullable=False, default=0, server_default="0")
    ot_eligible_seconds = Column(Integer, nullable=False, default=0, server_default="0")
    ot_eligible_minutes = Column(Integer, nullable=False, default=0, server_default="0")
    ot_candidate_minutes = Column(Integer, nullable=False, default=0, server_default="0")
    meal_violations = Column(SmallInteger, nullable=False, default=0, server_default="0")
    rest_violations = Column(SmallInteger, nullable=False, default=0, server_default="0")
    first_half_result = Column(String(14), nullable=False)
    second_half_result = Column(String(14), nullable=False)
    day_result = Column(String(14), nullable=False)
    presence_basis = Column(String(12), nullable=True)
    work_mode = Column(String(12), nullable=True)
    exception_codes = Column(ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]"))
    pending_reasons = Column(ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]"))
    override_request_id = Column(Integer, nullable=True)  # FK -> attendance_requests in as_m4_requests
    evaluated_at = Column(DateTime(timezone=True), nullable=False)
    evaluated_by = Column(String(8), nullable=False, default="SYSTEM", server_default="SYSTEM")
    trigger = Column(String(30), nullable=False)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_attendance_days_company_id_id"),
        UniqueConstraint("company_id", "employee_id", "shift_date", name="uq_attendance_days_employee_date"),
        ForeignKeyConstraint(["company_id", "employee_id"], ["employees.company_id", "employees.id"],
                             name="fk_attendance_days_employee"),
        ForeignKeyConstraint(["company_id", "legal_entity_id"], ["legal_entities.company_id", "legal_entities.id"],
                             name="fk_attendance_days_legal_entity"),
        ForeignKeyConstraint(["company_id", "branch_id"], ["branches.company_id", "branches.id"],
                             name="fk_attendance_days_branch"),
        ForeignKeyConstraint(["company_id", "shift_id"], ["shifts.company_id", "shifts.id"],
                             name="fk_attendance_days_shift"),
        ForeignKeyConstraint(["company_id", "shift_version_id"], ["shift_versions.company_id", "shift_versions.id"],
                             name="fk_attendance_days_shift_version"),
        ForeignKeyConstraint(["company_id", "roster_entry_id"], ["roster_entries.company_id", "roster_entries.id"],
                             name="fk_attendance_days_roster_entry"),
        CheckConstraint(f"evaluation_state IN {_in(EVALUATION_STATES)}", name="ck_attendance_days_state"),
        CheckConstraint(f"rules_mode IN {_in(RULES_MODES)}", name="ck_attendance_days_rules_mode"),
        CheckConstraint("version >= 1 AND row_version >= 1", name="ck_attendance_days_versions"),
        CheckConstraint("inputs_hash ~ '^[0-9a-f]{64}$' AND content_hash ~ '^[0-9a-f]{64}$'",
                        name="ck_attendance_days_hashes"),
        CheckConstraint(f"shift_source IS NULL OR shift_source IN {_in(DAY_SHIFT_SOURCES)}",
                        name="ck_attendance_days_shift_source"),
        CheckConstraint(f"expected_portion IN {_in(EXPECTED_PORTIONS)}", name="ck_attendance_days_expected_portion"),
        CheckConstraint("expected_minutes IS NULL OR expected_minutes >= 0", name="ck_attendance_days_expected_minutes"),
        CheckConstraint("session_count >= 0", name="ck_attendance_days_session_count"),
        CheckConstraint("(late_minutes IS NULL OR late_minutes >= 0) AND (early_minutes IS NULL OR early_minutes >= 0)"
                        " AND (short_minutes IS NULL OR short_minutes >= 0)", name="ck_attendance_days_mark_minutes"),
        CheckConstraint("NOT late_mark OR late_minutes IS NOT NULL", name="ck_attendance_days_late_minutes"),
        CheckConstraint("NOT early_mark OR early_minutes IS NOT NULL", name="ck_attendance_days_early_minutes"),
        CheckConstraint("NOT late_excused OR late_mark", name="ck_attendance_days_late_excused"),
        CheckConstraint("NOT early_excused OR early_mark", name="ck_attendance_days_early_excused"),
        CheckConstraint("NOT late_converted OR NOT late_mark", name="ck_attendance_days_late_converted"),
        CheckConstraint("NOT early_converted OR NOT early_mark", name="ck_attendance_days_early_converted"),
        CheckConstraint(f"late_excuse_source IS NULL OR late_excuse_source IN {_in(EXCUSE_SOURCES)}",
                        name="ck_attendance_days_late_source"),
        CheckConstraint(f"early_excuse_source IS NULL OR early_excuse_source IN {_in(EXCUSE_SOURCES)}",
                        name="ck_attendance_days_early_source"),
        CheckConstraint("(late_excuse_source IS NOT NULL) = late_excused", name="ck_attendance_days_late_source_set"),
        CheckConstraint("(early_excuse_source IS NOT NULL) = early_excused", name="ck_attendance_days_early_source_set"),
        CheckConstraint("night_minutes >= 0 AND ot_eligible_minutes >= 0 AND ot_candidate_minutes >= 0 "
                        "AND ot_eligible_seconds >= 0 AND meal_violations >= 0 AND rest_violations >= 0",
                        name="ck_attendance_days_counts"),
        CheckConstraint(f"first_half_result IN {_in(PORTION_RESULTS)} AND second_half_result IN {_in(PORTION_RESULTS)}",
                        name="ck_attendance_days_portions"),
        CheckConstraint(f"day_result IN {_in(DAY_RESULTS)}", name="ck_attendance_days_day_result"),
        CheckConstraint(f"presence_basis IS NULL OR presence_basis IN {_in(PRESENCE_BASES)}",
                        name="ck_attendance_days_basis"),
        CheckConstraint(f"work_mode IS NULL OR work_mode IN {_in(WORK_MODES)}", name="ck_attendance_days_work_mode"),
        CheckConstraint(f"exception_codes <@ ARRAY{list(EXCEPTION_CODES)}::text[]",
                        name="ck_attendance_days_exceptions"),
        CheckConstraint(f"pending_reasons <@ ARRAY{list(PENDING_REASONS)}::text[]", name="ck_attendance_days_pending"),
        CheckConstraint(f"evaluated_by IN {_in(EVALUATED_BY)}", name="ck_attendance_days_evaluated_by"),
        CheckConstraint(f"trigger IN {_in(EVALUATION_TRIGGERS)}", name="ck_attendance_days_trigger"),
        Index("ix_attendance_days_date_branch", "company_id", "shift_date", "branch_id"),
        Index("ix_attendance_days_employee", "company_id", "employee_id", text("shift_date DESC")),
        Index("ix_attendance_days_exceptions", "exception_codes", postgresql_using="gin"),
        Index("ix_attendance_days_open", "company_id", "shift_date", postgresql_where=text("evaluation_state <> 'FINAL'")),
        Index("ix_attendance_days_dashboard", "company_id", "shift_date",
              postgresql_include=["employee_id", "day_result"]),
    )


class AttendanceDayVersion(TenantMixin, BigIdModel):
    """Append-only history of ``attendance_days`` (AS 11 §4.3): version 1 = every output, later = changed outputs.

    Payroll lineage (INV-21; retention TIME_RECORD): trigger ``attendance_day_versions_guard`` refuses UPDATE / DELETE
    and the app role holds SELECT, INSERT only.
    """

    __tablename__ = "attendance_day_versions"

    day_id = Column(BigInteger, nullable=False)
    version = Column(Integer, nullable=False)
    outputs = Column(JSONB, nullable=False)
    inputs_hash = Column(String(64), nullable=False)
    trigger = Column(String(30), nullable=False)
    actor_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_attendance_day_versions_company_id_id"),
        UniqueConstraint("company_id", "day_id", "version", name="uq_attendance_day_versions_version"),
        ForeignKeyConstraint(["company_id", "day_id"], ["attendance_days.company_id", "attendance_days.id"],
                             name="fk_attendance_day_versions_day"),
        ForeignKeyConstraint(["company_id", "actor_user_id"], ["users.company_id", "users.id"],
                             name="fk_attendance_day_versions_actor"),
        CheckConstraint("version >= 1", name="ck_attendance_day_versions_version"),
        CheckConstraint("inputs_hash ~ '^[0-9a-f]{64}$'", name="ck_attendance_day_versions_hash"),
        CheckConstraint(f"trigger IN {_in(EVALUATION_TRIGGERS)}", name="ck_attendance_day_versions_trigger"),
    )

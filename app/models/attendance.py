from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import relationship

from app.models.base import BaseModel, TenantMixin

ATTENDANCE_STATUSES = ("present", "absent", "late", "half-day")
TRACKING_STATES = ("NOT_APPLICABLE", "ACTIVE", "PAUSED", "STOPPED")
BREAK_KINDS = ("LUNCH",)
EVENT_TYPES = ("PUNCH_IN", "LUNCH_IN", "LUNCH_OUT", "PUNCH_OUT", "SESSION_CLOSED", "CORRECTION")
EVENT_SOURCES = ("self", "kiosk", "manual", "system")
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

LIVE_SESSION = "check_out_time IS NULL AND closed_at IS NULL"


class Attendance(TenantMixin, BaseModel):
    """One attendance session (shift) per employee per company-local day.

    A session is *live* until it has a punch-out or a closure (a missing
    punch-out detected by the system, or recorded by HR); the database allows
    at most one live session per person, whatever day it started on.
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

    employee = relationship("Employee", primaryjoin="Attendance.employee_id == Employee.id", foreign_keys=[employee_id])

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
        # One record per person per day, whichever path (self, kiosk, manual) creates it.
        UniqueConstraint("company_id", "employee_id", "date", name="uq_attendance_employee_day"),
        UniqueConstraint("company_id", "id", name="uq_attendance_company_id_id"),
        Index("ix_attendance_company_date", "company_id", "date"),
        Index("ix_attendance_employee_date", "employee_id", "date"),
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

    __table_args__ = (
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

    __table_args__ = (
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

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "attendance_id"], ["attendance.company_id", "attendance.id"], name="fk_samples_attendance"
        ),
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
    )

"""Effective attendance, lunch and location policy of one company."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import timedelta

from sqlalchemy.orm import Session

from app.services import settings_service

PUNCH_EVENTS = ("PUNCH_IN", "PUNCH_OUT")


@dataclass(frozen=True)
class AttendancePolicy:
    require_face: bool
    require_face_for_lunch: bool
    allow_multiple_lunches: bool
    lunch_is_paid: bool
    lunch_open_at_punch_out: str  # CONFIRM_AUTO_CLOSE | BLOCK
    max_shift_hours: int
    location_punch: str  # OFF | OPTIONAL | REQUIRED
    location_lunch: str
    location_exception: str  # ALLOW_WITH_REASON | BLOCK
    reject_low_accuracy: bool
    preferred_accuracy_m: int
    acceptable_accuracy_m: int
    gps_timeout_seconds: int
    gps_max_attempts: int
    max_fix_age_seconds: int
    tracking_enabled: bool
    tracking_interval_minutes: int
    tracking_grace_minutes: int
    pause_on_lunch: bool
    movement_threshold_m: int
    retention_days: int

    def location_mode(self, event_type: str) -> str:
        return self.location_punch if event_type in PUNCH_EVENTS else self.location_lunch

    @property
    def collects_location(self) -> bool:
        return self.tracking_enabled or self.location_punch != "OFF" or self.location_lunch != "OFF"

    @property
    def interval(self) -> timedelta:
        return timedelta(minutes=self.tracking_interval_minutes)

    @property
    def grace(self) -> timedelta:
        return timedelta(minutes=self.tracking_grace_minutes)

    @property
    def max_shift(self) -> timedelta:
        return timedelta(hours=self.max_shift_hours)

    @property
    def version(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:12]

    def client_view(self) -> dict:
        """What the browser needs to capture and explain location checks (no secrets)."""
        return {
            "version": self.version,
            "require_face": self.require_face,
            "require_face_for_lunch": self.require_face_for_lunch,
            "allow_multiple_lunches": self.allow_multiple_lunches,
            "lunch_is_paid": self.lunch_is_paid,
            "lunch_open_at_punch_out": self.lunch_open_at_punch_out,
            "max_shift_hours": self.max_shift_hours,
            "location": {
                "punch": self.location_punch,
                "lunch": self.location_lunch,
                "exception": self.location_exception,
                "reject_low_accuracy": self.reject_low_accuracy,
                "preferred_accuracy_m": self.preferred_accuracy_m,
                "acceptable_accuracy_m": self.acceptable_accuracy_m,
                "timeout_ms": self.gps_timeout_seconds * 1000,
                "max_attempts": self.gps_max_attempts,
                "max_fix_age_seconds": self.max_fix_age_seconds,
                "retention_days": self.retention_days,
            },
            "tracking": {
                "enabled": self.tracking_enabled,
                "interval_minutes": self.tracking_interval_minutes,
                "grace_minutes": self.tracking_grace_minutes,
                "pause_on_lunch": self.pause_on_lunch,
                "movement_threshold_m": self.movement_threshold_m,
            },
        }


_FIELDS = {
    "require_face": "requireFaceForSelfCheckIn",
    "require_face_for_lunch": "requireFaceForLunch",
    "allow_multiple_lunches": "allowMultipleLunches",
    "lunch_is_paid": "lunchIsPaid",
    "lunch_open_at_punch_out": "lunchOpenAtPunchOut",
    "max_shift_hours": "maxShiftHours",
    "location_punch": "locationPunchPolicy",
    "location_lunch": "locationLunchPolicy",
    "location_exception": "locationExceptionPolicy",
    "reject_low_accuracy": "locationRejectLowAccuracy",
    "preferred_accuracy_m": "gpsPreferredAccuracyM",
    "acceptable_accuracy_m": "gpsAcceptableAccuracyM",
    "gps_timeout_seconds": "gpsTimeoutSeconds",
    "gps_max_attempts": "gpsMaxAttempts",
    "max_fix_age_seconds": "gpsMaxFixAgeSeconds",
    "tracking_enabled": "trackingEnabled",
    "tracking_interval_minutes": "trackingIntervalMinutes",
    "tracking_grace_minutes": "trackingGraceMinutes",
    "pause_on_lunch": "trackingPauseOnLunch",
    "movement_threshold_m": "movementThresholdM",
    "retention_days": "locationRetentionDays",
}
SETTING_KEYS = tuple(_FIELDS.values())


def load(db: Session, company_id: int) -> AttendancePolicy:
    values = settings_service.get_values(db, company_id, SETTING_KEYS)
    return AttendancePolicy(**{field: values[key] for field, key in _FIELDS.items()})


def settings_view(db: Session, company_id: int) -> dict:
    """The policy as company settings keys (for the admin policy editor)."""
    return settings_service.get_values(db, company_id, settings_service.ATTENDANCE_POLICY_KEYS)

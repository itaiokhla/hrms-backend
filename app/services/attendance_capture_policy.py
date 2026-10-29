"""Effective capture policy of one employee on one date (spine §6.2.1 ``ATTENDANCE_CAPTURE_POLICY``; 13 §4.5 C-2).

``effective`` resolves the ``ATTENDANCE_CAPTURE_POLICY`` version of the employee on the employee-local date
(``attendance_policies.resolve``); ``for_branch`` resolves the branch-level rule of a kiosk before anyone is identified
(AS-CAP-09). A **LEGACY_V1** version (created by ``attendance_setup.v2_prepare_capture``) and "no version resolves"
both answer the LEGACY_V1 mapping of today's company attendance settings (``attendance_policy.load``; spine §6.3
"[source]" column), read live so the legacy settings page keeps working for v1 tenants (SP-AS-221, XV-REQ-02). A
**STANDARD** version answers its own fields — v1 tenants may activate STANDARD capture versions (V-AS-02 does not apply
to this type), but while ``attendance.v2.enabled`` is false ``v1_clamp`` forces one session per date, no offline queue
and interactive sources only.

``as_attendance_policy`` overlays the capture fields on the legacy ``AttendancePolicy`` the punch pipeline evaluates
location, face, break and session rules with (a LEGACY_V1 policy overlays nothing new).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, FrozenSet, Mapping, Optional, Tuple

from sqlalchemy.orm import Session

from app.services import attendance_access, attendance_policy

SWITCHABLE_SOURCES = ("WEB", "MOBILE", "KIOSK", "DEVICE", "IMPORT", "SUPERVISOR")
V1_SOURCES = frozenset({"WEB", "MOBILE", "KIOSK"})
KIOSK_METHODS = ("FACE", "PIN", "QR", "CARD")
LOCATION_MODES = ("OFF", "OPTIONAL", "REQUIRED")
GEOFENCE_MODES = ("OFF", "FLAG", "REQUIRE_REASON", "BLOCK")
IP_MODES = ("OFF", "FLAG", "BLOCK")
QR_MODES = ("OFF", "REQUIRED")
OFFLINE_MODES = ("OFF", "ALLOW")
AUTO_CLOSE_MODES = ("MAX_DURATION", "SHIFT_END_PLUS")
PROFILES = ("LEGACY_V1", "STANDARD")


@dataclass(frozen=True)
class CapturePolicy:
    """The capture fields this code consumes (spine §6.2.1; defaults = STANDARD defaults)."""

    profile: str = "STANDARD"
    allowed_sources: FrozenSet[str] = frozenset({"WEB", "MOBILE"})
    shift_required: bool = False
    capture_required: bool = True  # SP-AS-226
    face_on_punch: bool = False
    face_on_break: bool = False
    punch_photo: bool = False
    kiosk_methods: FrozenSet[str] = frozenset({"PIN"})
    location_mode: str = "OFF"
    location_on_breaks: str = "OFF"
    location_failure_action: str = "ALLOW_WITH_REASON"
    gps_required: bool = False  # derived: location_mode == REQUIRED
    geofence_mode: str = "OFF"
    allowed_site_ids: Tuple[int, ...] = ()
    ip_mode: str = "OFF"
    qr_mode: str = "OFF"
    multiple_sessions_allowed: bool = True
    allow_multiple_breaks: bool = True
    break_open_at_punch_out: str = "CONFIRM_AUTO_CLOSE"
    min_session_minutes: int = 1
    auto_close_mode: str = "SHIFT_END_PLUS"
    max_session_hours: int = 16
    auto_close_after_minutes: int = 240
    offline_punches: str = "OFF"
    offline_max_age_hours: int = 24
    # Phase 2 additions (same §6.2.1 groups).
    reject_low_accuracy: bool = False
    preferred_accuracy_m: int = 50
    acceptable_accuracy_m: int = 100
    max_fix_age_seconds: int = 120
    tracking_enabled: bool = False
    tracking_interval_minutes: int = 120
    tracking_grace_minutes: int = 15
    tracking_pause_on_break: bool = True
    tracking_movement_threshold_m: int = 1000
    dpia_reference: Optional[str] = None
    policy_id: Optional[int] = field(default=None, compare=False)
    policy_version: Optional[int] = field(default=None, compare=False)
    explain: Tuple[str, ...] = field(default=(), compare=False)

    def allows(self, source: str) -> bool:
        return source in self.allowed_sources

    @property
    def uses_face(self) -> bool:
        return self.face_on_punch or self.face_on_break or "FACE" in self.kiosk_methods


def legacy_v1(policy: attendance_policy.AttendancePolicy) -> CapturePolicy:
    """LEGACY_V1 values of spine §6.2.1 from today's company settings."""
    return CapturePolicy(
        profile="LEGACY_V1",
        allowed_sources=frozenset({"WEB", "KIOSK"}),
        face_on_punch=policy.require_face,
        face_on_break=policy.require_face_for_lunch,
        kiosk_methods=frozenset({"FACE"}),
        location_mode=policy.location_punch,
        location_on_breaks=policy.location_lunch,
        location_failure_action=policy.location_exception,
        gps_required=policy.location_punch == "REQUIRED",
        multiple_sessions_allowed=False,
        allow_multiple_breaks=policy.allow_multiple_lunches,
        break_open_at_punch_out=policy.lunch_open_at_punch_out,
        auto_close_mode="MAX_DURATION",
        max_session_hours=policy.max_shift_hours,
        reject_low_accuracy=policy.reject_low_accuracy,
        preferred_accuracy_m=policy.preferred_accuracy_m,
        acceptable_accuracy_m=policy.acceptable_accuracy_m,
        max_fix_age_seconds=policy.max_fix_age_seconds,
        tracking_enabled=policy.tracking_enabled,
        tracking_interval_minutes=policy.tracking_interval_minutes,
        tracking_grace_minutes=policy.tracking_grace_minutes,
        tracking_pause_on_break=policy.pause_on_lunch,
        tracking_movement_threshold_m=policy.movement_threshold_m,
        explain=("LEGACY_V1 from company attendance settings",),
    )


def legacy_fields(policy: attendance_policy.AttendancePolicy) -> dict:
    """The JSON fields of a LEGACY_V1 ``ATTENDANCE_CAPTURE_POLICY`` version (spine §6.3; v2_prepare_capture)."""
    return {
        "profile": "LEGACY_V1", "allowed_sources": ["WEB", "KIOSK"], "shift_required": False,
        "capture_required": True, "face_on_punch": policy.require_face, "face_on_break": policy.require_face_for_lunch,
        "punch_photo": False, "kiosk_methods": ["FACE"], "dpia_reference": None,
        "location_mode": policy.location_punch, "location_on_breaks": policy.location_lunch,
        "location_failure_action": policy.location_exception, "reject_low_accuracy": policy.reject_low_accuracy,
        "gps": {"preferred_accuracy_m": policy.preferred_accuracy_m,
                "acceptable_accuracy_m": policy.acceptable_accuracy_m, "timeout_seconds": policy.gps_timeout_seconds,
                "max_attempts": policy.gps_max_attempts, "max_fix_age_seconds": policy.max_fix_age_seconds},
        "geofence_mode": "OFF", "allowed_site_ids": None, "ip_mode": "OFF", "qr_mode": "OFF",
        "tracking": {"enabled": policy.tracking_enabled, "interval_minutes": policy.tracking_interval_minutes,
                     "grace_minutes": policy.tracking_grace_minutes, "pause_on_break": policy.pause_on_lunch,
                     "movement_threshold_m": policy.movement_threshold_m},
        "multiple_sessions_allowed": False, "allow_multiple_breaks": policy.allow_multiple_lunches,
        "break_open_at_punch_out": policy.lunch_open_at_punch_out, "min_session_minutes": 1,
        "auto_close_mode": "MAX_DURATION", "max_session_hours": policy.max_shift_hours,
        "auto_close_after_minutes": 240, "infer_breaks_from_punches": False, "max_break_gap_minutes": 90,
        "offline_punches": "OFF", "offline_max_age_hours": 24,
        "supervisor_muster": {"enabled": False, "photo_required": False, "geofence_mode": "OFF",
                              "offline_allowed": False},
    }


def from_fields(fields: Mapping[str, Any], *, policy_id: Optional[int] = None,
                version: Optional[int] = None) -> CapturePolicy:
    """A STANDARD version's fields (normalized by ``policy_types.validate_fields``) as a ``CapturePolicy``."""
    gps = fields.get("gps") or {}
    tracking = fields.get("tracking") or {}
    return CapturePolicy(
        profile=str(fields.get("profile") or "STANDARD"),
        allowed_sources=frozenset(fields.get("allowed_sources") or ("WEB", "MOBILE")),
        shift_required=bool(fields.get("shift_required", False)),
        capture_required=bool(fields.get("capture_required", True)),
        face_on_punch=bool(fields.get("face_on_punch", False)),
        face_on_break=bool(fields.get("face_on_break", False)),
        punch_photo=bool(fields.get("punch_photo", False)),
        kiosk_methods=frozenset(fields.get("kiosk_methods") or ("PIN",)),
        location_mode=fields.get("location_mode") or "OFF",
        location_on_breaks=fields.get("location_on_breaks") or "OFF",
        location_failure_action=fields.get("location_failure_action") or "ALLOW_WITH_REASON",
        gps_required=(fields.get("location_mode") or "OFF") == "REQUIRED",
        geofence_mode=fields.get("geofence_mode") or "OFF",
        allowed_site_ids=tuple(fields.get("allowed_site_ids") or ()),
        ip_mode=fields.get("ip_mode") or "OFF",
        qr_mode=fields.get("qr_mode") or "OFF",
        multiple_sessions_allowed=bool(fields.get("multiple_sessions_allowed", True)),
        allow_multiple_breaks=bool(fields.get("allow_multiple_breaks", True)),
        break_open_at_punch_out=fields.get("break_open_at_punch_out") or "CONFIRM_AUTO_CLOSE",
        min_session_minutes=int(fields.get("min_session_minutes", 1)),
        auto_close_mode=fields.get("auto_close_mode") or "SHIFT_END_PLUS",
        max_session_hours=int(fields.get("max_session_hours", 16)),
        auto_close_after_minutes=int(fields.get("auto_close_after_minutes", 240)),
        offline_punches=fields.get("offline_punches") or "OFF",
        offline_max_age_hours=int(fields.get("offline_max_age_hours", 24)),
        reject_low_accuracy=bool(fields.get("reject_low_accuracy", False)),
        preferred_accuracy_m=int(gps.get("preferred_accuracy_m", 50)),
        acceptable_accuracy_m=int(gps.get("acceptable_accuracy_m", 100)),
        max_fix_age_seconds=int(gps.get("max_fix_age_seconds", 120)),
        tracking_enabled=bool(tracking.get("enabled", False)),
        tracking_interval_minutes=int(tracking.get("interval_minutes", 120)),
        tracking_grace_minutes=int(tracking.get("grace_minutes", 15)),
        tracking_pause_on_break=bool(tracking.get("pause_on_break", True)),
        tracking_movement_threshold_m=int(tracking.get("movement_threshold_m", 1000)),
        dpia_reference=fields.get("dpia_reference"),
        policy_id=policy_id, policy_version=version,
        explain=(f"STANDARD capture policy {policy_id} version {version}",),
    )


def v1_clamp(policy: CapturePolicy) -> CapturePolicy:
    """While the tenant is not V2_ACTIVE: one session per date, no offline queue, interactive sources only (§16)."""
    return replace(policy, multiple_sessions_allowed=False, offline_punches="OFF",
                   allowed_sources=frozenset(policy.allowed_sources & V1_SOURCES))


def _finish(db: Session, company_id: int, found) -> CapturePolicy:
    if found is None or found.profile == "LEGACY_V1":
        policy = legacy_v1(attendance_policy.load(db, company_id))
        if found is not None:
            policy = replace(policy, policy_id=found.policy_id, policy_version=found.version,
                             explain=(f"LEGACY_V1 capture policy {found.policy_id}: company attendance settings",))
    else:
        policy = from_fields(found.fields, policy_id=found.policy_id, version=found.version)
    if not attendance_access.v2_enabled(db, company_id):
        policy = v1_clamp(policy)
    return policy


def effective(db: Session, company_id: int, employee_id: Optional[int], d: date) -> CapturePolicy:
    """The capture policy of ``employee_id`` on the employee-local date ``d`` (see the module doc)."""
    from app.services import attendance_policies

    found =attendance_policies.resolve(db, company_id, attendance_policies.CAPTURE, employee_id, d) \
        if employee_id is not None else None
    return _finish(db, company_id, found)


def for_branch(db: Session, company_id: int, branch_id: Optional[int], d: date) -> CapturePolicy:
    """The branch-level resolution used by a kiosk before anyone is identified (AS-CAP-09: branch and higher bands
    only, no team or employee criteria)."""
    from app.services import attendance_policies

    found = attendance_policies.resolve_branch(db, company_id, attendance_policies.CAPTURE, branch_id, d)
    return _finish(db, company_id, found)


def as_attendance_policy(capture: CapturePolicy,
                         base: attendance_policy.AttendancePolicy) -> attendance_policy.AttendancePolicy:
    """The legacy pipeline policy with the capture fields applied (location, face, breaks, session length)."""
    if capture.profile == "LEGACY_V1":
        return base
    return replace(
        base, require_face=capture.face_on_punch, require_face_for_lunch=capture.face_on_break,
        allow_multiple_lunches=capture.allow_multiple_breaks, lunch_open_at_punch_out=capture.break_open_at_punch_out,
        max_shift_hours=capture.max_session_hours, location_punch=capture.location_mode,
        location_lunch=capture.location_on_breaks, location_exception=capture.location_failure_action,
        reject_low_accuracy=capture.reject_low_accuracy, preferred_accuracy_m=capture.preferred_accuracy_m,
        acceptable_accuracy_m=capture.acceptable_accuracy_m, max_fix_age_seconds=capture.max_fix_age_seconds,
        tracking_enabled=capture.tracking_enabled, tracking_interval_minutes=capture.tracking_interval_minutes,
        tracking_grace_minutes=capture.tracking_grace_minutes, pause_on_lunch=capture.tracking_pause_on_break,
        movement_threshold_m=capture.tracking_movement_threshold_m)


def tracking_refused(db: Session, company_id: int, employee_id: int, d: date) -> bool:
    """07 §4 C-5 / L-2: a DECLINED, WITHDRAWN or EXPIRED ``LOCATION_TRACKING`` consent in force on ``d`` — tracking
    does not start (or stops); punching still works."""
    from app.services import attendance_consents

    row = attendance_consents.state_on(db, company_id, employee_id, "LOCATION_TRACKING", d)
    return row is not None and row.status != "GRANTED"


def for_session(db: Session, att) -> attendance_policy.AttendancePolicy:
    """The pipeline policy a live session is tracked under: the employee's capture policy on the session's shift date
    over the company settings (periodic location sampling, spine §6.2.1 ``tracking``), with tracking off when the
    employee refused ``LOCATION_TRACKING``."""
    base = attendance_policy.load(db, att.company_id)
    policy = as_attendance_policy(effective(db, att.company_id, att.employee_id, att.date), base)
    if policy.tracking_enabled and tracking_refused(db, att.company_id, att.employee_id, att.date):
        policy = replace(policy, tracking_enabled=False)
    return policy


def client_view(policy: CapturePolicy) -> dict:
    """What the punch screen needs (no site geometry, no secrets): sources and modes (GET /attendance/me/capture)."""
    return {
        "profile": policy.profile, "sources": sorted(policy.allowed_sources), "shift_required": policy.shift_required,
        "capture_required": policy.capture_required, "face_on_punch": policy.face_on_punch,
        "face_on_break": policy.face_on_break, "punch_photo": policy.punch_photo,
        "location_mode": policy.location_mode, "location_on_breaks": policy.location_on_breaks,
        "location_failure_action": policy.location_failure_action, "geofence_mode": policy.geofence_mode,
        "ip_mode": policy.ip_mode, "qr_mode": policy.qr_mode,
        "multiple_sessions_allowed": policy.multiple_sessions_allowed,
        "allow_multiple_breaks": policy.allow_multiple_breaks, "min_session_minutes": policy.min_session_minutes,
        "gps": {"preferred_accuracy_m": policy.preferred_accuracy_m,
                "acceptable_accuracy_m": policy.acceptable_accuracy_m,
                "max_fix_age_seconds": policy.max_fix_age_seconds},
        "tracking": {"enabled": policy.tracking_enabled, "interval_minutes": policy.tracking_interval_minutes,
                     "grace_minutes": policy.tracking_grace_minutes, "pause_on_break": policy.tracking_pause_on_break},
        "offline_punches": policy.offline_punches, "offline_max_age_hours": policy.offline_max_age_hours,
        "policy_id": policy.policy_id, "policy_version": policy.policy_version,
    }

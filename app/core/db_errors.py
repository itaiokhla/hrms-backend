"""Database constraint and trigger errors -> spine §8.3 codes (AS 11 §10.3; A-20, IG-AS-10).

Guard and overlap triggers raise SQLSTATE 23514 (check_violation) or 23P01 (exclusion_violation) with the rule name
as ``CONSTRAINT``; unique indexes raise 23505 with their index name. ``map_integrity`` turns
``exc.orig.diag.constraint_name`` into an ``AppError`` (generalising ``team_service.constraint_text``); an unknown
name returns None so the caller re-raises (500 + error log). Use ``mapped()`` around a flush or in the handler that
``idempotency.execute`` runs.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator, Mapping, Optional, Tuple

from sqlalchemy.exc import DBAPIError

from app.core.errors import AppError

logger = logging.getLogger(__name__)

# constraint / trigger rule -> (HTTP status, code, message)
CONSTRAINT_CODES: Mapping[str, Tuple[int, str, str]] = {
    "attendance_sessions_no_overlap": (409, "SESSION_OVERLAP", "This time overlaps another attendance session."),
    "uq_attendance_employee_day_seq": (409, "ALREADY_CHECKED_IN", "Attendance for this day has already been recorded."),
    "uq_attendance_employee_day": (409, "ALREADY_CHECKED_IN", "Attendance for this day has already been recorded."),
    "uq_attendance_one_live_session": (409, "ALREADY_CHECKED_IN",
                                       "This person already has a shift in progress. Close it first."),
    "attendance_breaks_guard": (409, "SESSION_OVERLAP", "The break overlaps another break or leaves its session."),
    "attendance_punches_guard": (409, "INVALID_TRANSITION", "A raw punch cannot be changed."),
    "uq_attendance_punches_natural_key": (409, "CONFLICT", "This punch has already been recorded."),
    "uq_attendance_devices_code": (409, "CONFLICT", "Another device already uses this code."),
    "uq_attendance_devices_serial": (409, "DEVICE_SERIAL_TAKEN", "This serial number is already registered."),
    "uq_attendance_review_items_open": (409, "CONFLICT", "An open review item already exists."),
    "shift_versions_no_overlap": (409, "SHIFT_VERSION_IN_FORCE", "Another version of this shift is in force."),
    "shift_versions_in_force": (409, "SHIFT_VERSION_IN_FORCE", "A version in force cannot be changed."),
    "shift_versions_gap": (422, "SHIFT_VERSION_GAP", "Shift versions must not leave a gap."),
    "roster_periods_no_overlap": (409, "ROSTER_PERIOD_OVERLAP", "Another roster period covers these dates."),
    "team_cycle": (409, "TEAM_CYCLE", "A team cannot be placed under one of its own sub-teams."),
    "membership_overlap": (409, "MEMBERSHIP_OVERLAP", "This person already has a membership of this team on these dates."),
    "primary_team_conflict": (409, "PRIMARY_TEAM_CONFLICT", "This person already has a primary team on these dates."),
    "routing_lead_conflict": (409, "ROUTING_LEAD_CONFLICT", "The team already has a routing lead on these dates."),
    "role_window_overlap": (409, "ROLE_WINDOW_OVERLAP", "The user already holds this role in an overlapping window."),
}
# Rules that the services never attempt; reaching them is a defect worth a log line (AS 11 §10.3).
DEFECT_RULES = frozenset({"attendance_punches_guard"})
MAPPED_SQLSTATES = frozenset({"23505", "23514", "23P01", "55P03"})


def constraint_name(exc: BaseException) -> Optional[str]:
    orig = getattr(exc, "orig", exc)
    diag = getattr(orig, "diag", None)
    return getattr(diag, "constraint_name", None)


def sqlstate(exc: BaseException) -> Optional[str]:
    orig = getattr(exc, "orig", exc)
    return getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)


def map_integrity(exc: BaseException) -> Optional[AppError]:
    """The AppError for a constraint / trigger violation, or None when the error is not a known rule."""
    state = sqlstate(exc)
    name = constraint_name(exc)
    if state == "55P03":
        return AppError(409, "Another change is in progress. Try again.", "LOCK_TIMEOUT")
    if state not in MAPPED_SQLSTATES:
        return None
    if name in CONSTRAINT_CODES:
        status, code, message = CONSTRAINT_CODES[name]
        if name in DEFECT_RULES:
            logger.error("guard %s refused a write the service should never attempt: %s", name, exc)
        return AppError(status, message, code)
    if state == "23505" and name and name.startswith("uq_"):
        return AppError(409, "This record already exists.", "CONFLICT")
    return None


@contextmanager
def mapped() -> Iterator[None]:
    """Re-raise a known constraint / trigger violation as its AppError; anything else propagates unchanged."""
    try:
        yield
    except DBAPIError as exc:
        error = map_integrity(exc)
        if error is None:
            raise
        raise error from exc

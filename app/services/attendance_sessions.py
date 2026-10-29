"""Sessions written by interactive punches (04-attendance/03 §3.1, §4, §5; spine §3.1, §3.2, §5.5.1; D-AS-05, -06).

* The shift date of an interactive punch is the start date, in the work zone, of the shift instance whose matching
  window contains it (``punch_pairing.shift_date``; instances of D-1, D, D+1 from ``shift_resolution`` +
  ``shift_engine``). A night punch after midnight stays on the evening's date and the next night opens the next date
  (BUG-AS-01 / -02, GV-AS-CAP-07).
* Several sessions per shift date (``session_seq`` 1-8) only with ``multiple_sessions_allowed``; tenants on v1
  (``attendance.v2.enabled`` false) and policies without it keep the legacy refusal 409 ``ALREADY_CHECKED_IN``.
* Two non-voided sessions of one employee never overlap: ``check_overlap`` answers 409 ``SESSION_OVERLAP`` before
  the write; the trigger ``attendance_sessions_no_overlap`` is the backstop (mapped by ``app.core.db_errors``).
* Every interactive capture writes one ``attendance_punches`` row (``record_punch``), also for v1 tenants (shadow
  input, spine §15.7); ``apply_interactive`` links the punch and its session (lineage, occupied interval).
* Auto-close happens in jobs only (``attendance_jobs``: ``attendance.day_close``); a GET never closes (AS-M3 removes
  the legacy close in ``my_today``).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional

from sqlalchemy import func, or_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core import db_errors
from app.core.errors import AppError, conflict
from app.models.attendance import Attendance, AttendancePunch
from app.models.employee import Employee
from app.services import punch_pairing
from app.services import shift_engine as engine

MAX_SESSIONS_PER_DATE = 8


@dataclass(frozen=True)
class ShiftDate:
    date: date
    work_timezone: str
    instance: Optional[engine.ShiftInstance]
    shift_id: Optional[int] = None
    shift_version_id: Optional[int] = None


def _zone_name(db: Session, employee: Employee, day: date) -> str:
    from app.services import attendance_service

    return attendance_service.work_zone(db, employee, day).zone


def instances_around(db: Session, company_id: int, employee: Employee, day: date) -> List[engine.ShiftInstance]:
    """Instances of the resolved shifts of ``day - 1 .. day + 1`` (unbuildable versions are skipped)."""
    from app.services import shift_resolution

    resolved = shift_resolution.shifts_for_many(db, company_id, [employee.id], day - timedelta(days=1),
                                                day + timedelta(days=1))
    instances = []
    for offset in (-1, 0, 1):
        d = day + timedelta(days=offset)
        item = resolved.get((employee.id, d))
        if item is None:
            continue
        try:
            instances.append(engine.build_instance(item.version, d, item.work_timezone))
        except Exception:  # noqa: BLE001 - a version that cannot be built never blocks a punch (S-4)
            continue
    return instances


def resolve_shift_date(db: Session, company_id: int, employee: Employee, at: datetime, direction: str) -> ShiftDate:
    """Shift date of an interactive punch at ``at`` (never refuses: S-4)."""
    from app.services import org_structure_service

    local = org_structure_service.employee_local_date(db, employee, at)
    zone = _zone_name(db, employee, local)
    instances = instances_around(db, company_id, employee, local)
    day, instance = punch_pairing.shift_date(instances, at, direction, zone)
    if instance is not None:
        return ShiftDate(day, instance.work_timezone, instance, instance.shift_id, instance.shift_version_id)
    return ShiftDate(day, zone, None)


def sessions_on(db: Session, company_id: int, employee_id: int, day: date) -> List[Attendance]:
    return (db.query(Attendance)
            .filter(Attendance.company_id == company_id, Attendance.employee_id == employee_id,
                    Attendance.date == day, Attendance.voided_at.is_(None))
            .order_by(Attendance.session_seq, Attendance.check_in_time).all())


def next_session_seq(db: Session, company_id: int, employee_id: int, day: date, multiple_allowed: bool) -> int:
    """``session_seq`` of a new session on ``day``; the legacy one-session guard when multiple sessions are off."""
    existing = sessions_on(db, company_id, employee_id, day)
    if not existing:
        return 1
    if not multiple_allowed:
        # Name the shift date: after midnight an IN can still belong to yesterday's shift (D-AS-06, FU-AS-43).
        raise conflict(f"Attendance for {day.strftime('%a %d %b')} is already complete (one session per day).",
                       "ALREADY_CHECKED_IN")
    seq = max(s.session_seq for s in existing) + 1
    if seq > MAX_SESSIONS_PER_DATE:
        raise conflict("No more sessions can be recorded on this date.", "ALREADY_CHECKED_IN")
    return seq


def check_overlap(db: Session, company_id: int, employee_id: int, start: datetime, end: Optional[datetime],
                  *, exclude_id: Optional[int] = None) -> None:
    """409 ``SESSION_OVERLAP`` when ``[start, end or infinity)`` overlaps a non-voided session of the employee."""
    query = db.query(Attendance.id).filter(
        Attendance.company_id == company_id, Attendance.employee_id == employee_id, Attendance.voided_at.is_(None),
        or_(Attendance.occupied_until.is_(None), Attendance.occupied_until > start))
    if end is not None:
        query = query.filter(Attendance.check_in_time < end)
    if exclude_id is not None:
        query = query.filter(Attendance.id != exclude_id)
    # A live session is answered by ALREADY_CHECKED_IN (the caller checks it first).
    query = query.filter(or_(Attendance.check_out_time.is_not(None), Attendance.closed_at.is_not(None)))
    if query.first() is not None:
        raise conflict("This time overlaps another attendance session.", "SESSION_OVERLAP")


# ------------------------------------------------------------------------------------------------- raw punches
def natural_key(company_id: int, source: str, employee_id: Optional[int], idempotency_key: Optional[str],
                occurred_at: datetime, direction: str) -> str:
    """Natural key of an interactive / manual punch (spine §3.2; P-1, P-5a): the client idempotency key (kiosk: the
    ticket nonce) when there is one, else the instant; the direction is part of the instant form so an IN and an
    OUT of the same second never collapse."""
    part = f"k:{idempotency_key}" if idempotency_key else f"t:{occurred_at.isoformat()}:{direction}"
    raw = f"{company_id}|{source}|{employee_id}|{part}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ip_hash(company_id: int, ip: Optional[str]) -> Optional[str]:
    """P-5: sha256(company_id || platform salt || ip); the raw IP only goes to the audit log."""
    if not ip or ip == "unknown":
        return None
    from app.config import settings

    return hashlib.sha256(f"{company_id}|{settings.secret_key}|{ip}".encode("utf-8")).hexdigest()


def record_punch(db: Session, *, company_id: int, employee_id: int, source: str, direction: str,
                 occurred_at: datetime, session: Optional[Attendance], shift_date: Optional[date],
                 idempotency_key: Optional[str] = None, client_sent_at: Optional[datetime] = None,
                 verify_mode: Optional[str] = None, face_match_score: Optional[float] = None,
                 device_id: Optional[int] = None, site_id: Optional[int] = None,
                 geofence_result: str = "NOT_CHECKED", evidence_flags=(), location_sample_id: Optional[int] = None,
                 ip: Optional[str] = None, actor_user_id: Optional[int] = None, reason: Optional[str] = None,
                 request_id: Optional[int] = None) -> AttendancePunch:
    """Insert one USED raw punch (ON CONFLICT on the natural key returns the first row, P-1)."""
    key = natural_key(company_id, source, employee_id, idempotency_key, occurred_at, direction)
    values = dict(
        company_id=company_id, employee_id=employee_id, source=source, direction=direction, occurred_at=occurred_at,
        received_at=occurred_at if source in ("WEB", "MOBILE", "KIOSK") else func.now(),
        client_sent_at=client_sent_at, device_id=device_id, verify_mode=verify_mode,
        face_match_score=face_match_score, site_id=site_id, geofence_result=geofence_result,
        location_sample_id=location_sample_id, ip_hash=ip_hash(company_id, ip),
        evidence_flags=list(evidence_flags), natural_key=key, idempotency_key=idempotency_key,
        mapping_status="MAPPED", disposition="USED", session_id=session.id if session is not None else None,
        shift_date=shift_date, actor_user_id=actor_user_id, reason=reason, request_id=request_id,
        created_at=func.now(), updated_at=func.now(),
    )
    with db_errors.mapped():
        db.execute(insert(AttendancePunch).values(**values)
                   .on_conflict_do_nothing(index_elements=["company_id", "natural_key"]))
    punch = db.query(AttendancePunch).filter(AttendancePunch.company_id == company_id,
                                             AttendancePunch.natural_key == key).one()
    return punch


def apply_interactive(db: Session, company_id: int, punch: AttendancePunch, instance: Optional[engine.ShiftInstance],
                      session: Attendance) -> Attendance:
    """Link an interactive punch to the session it opened, closed or broke (lineage and occupied interval)."""
    if punch.session_id != session.id or punch.company_id != company_id:
        raise AppError(500, "punch and session do not match", "INTERNAL_ERROR")
    if punch.direction == "IN":
        session.first_punch_id = punch.id
        if instance is not None:
            session.shift_version_id = session.shift_version_id or instance.shift_version_id
            if instance.roster_entry_id is not None and instance.roster_version is not None:
                session.roster_entry_id, session.roster_entry_version = instance.roster_entry_id, instance.roster_version
    if punch.direction == "OUT":
        session.occupied_until = session.check_out_time
    session.last_punch_id = punch.id
    db.flush()
    return session


def occupied_until(record: Attendance, max_session_hours: int) -> Optional[datetime]:
    """A-08: check_out_time, else LEAST(closed_at, check_in_time + max hours) once closed, else NULL (live)."""
    if record.check_out_time is not None:
        return record.check_out_time
    if record.closed_at is not None:
        return max(record.check_in_time, min(record.closed_at, record.check_in_time + timedelta(hours=max_session_hours)))
    return None

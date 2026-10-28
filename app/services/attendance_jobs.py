"""Attendance job handlers (AS 11 §8.2; spine §9.2).

``attendance.day_close`` (HOURLY, one job per tenant and slot; scheduled row seeded by as_m2_punches_sessions):
AS-M2 ships its auto-close part — live sessions past ``check_in_time + max_session_hours`` (MAX_DURATION, the
LEGACY_V1 ``auto_close_mode``) are closed with ``closed_reason = MISSING_PUNCH_OUT`` and ``occupied_until`` set, an
``attendance_events`` ``SESSION_CLOSED`` row and audit ``AUTO_CLOSE``; no punch is ever invented (01 §2.3). The
evaluation of closed days and ``SHIFT_END_PLUS`` (instance end + ``auto_close_after_minutes``, AS-CAP-04) arrive
with ``attendance_days`` in AS-M3.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core import clock
from app.models.attendance import Attendance
from app.payroll.services import jobs
from app.services import attendance_policy, attendance_service


def close_overdue_sessions(db: Session, company_id: int) -> int:
    policy = attendance_policy.load(db, company_id)
    now = clock.utcnow().replace(microsecond=0)
    overdue = (db.query(Attendance)
               .filter(Attendance.company_id == company_id, Attendance.check_out_time.is_(None),
                       Attendance.closed_at.is_(None), Attendance.voided_at.is_(None),
                       Attendance.check_in_time <= now - policy.max_shift)
               .order_by(Attendance.id).all())
    closed = 0
    for record in overdue:
        attendance_service.lock_employee(db, record.employee_id)
        db.refresh(record)
        if attendance_service.expire_if_needed(db, record, policy, now):
            closed += 1
    return closed


@jobs.register("attendance.day_close")
def day_close(ctx) -> dict:
    """Auto-close (AS-M2), then the evaluation of closed days for attendance v2 tenants (AS-M3, 04-attendance/03
    §9; v1 tenants get no rows outside reconciliation)."""
    from app.services import attendance_days

    closed = close_overdue_sessions(ctx.db, ctx.company_id)
    return {"auto_closed": closed, "evaluation": attendance_days.day_close(ctx.db, ctx.company_id)}


@jobs.register("attendance.location_sweep")
def location_sweep(ctx) -> dict:
    """HOURLY (scheduled row seeded by as_m2_sites_consents): the location part of the CLI ``attendance-sweep`` —
    missed tracking slots, stopped tracking and the redaction of coordinates past retention (07 §7 L-5). Overdue
    sessions it meets are closed by the same MAX_DURATION rule as ``attendance.day_close`` (idempotent)."""
    from app.services import location_service

    result = location_service.sweep(ctx.db, ctx.company_id)
    return {k: v for k, v in result.items() if k != "company_id"}


@jobs.register("attendance.consent_expire")
def consent_expire(ctx) -> dict:
    """DAILY, daily-local (payload ``local_date``): GRANTED consents past ``valid_to`` get an EXPIRED row (07 §5.2 C-6),
    and face data due for deletion (withdrawal, B-8) is purged unless a legal hold covers the employee."""
    from datetime import date

    from app.services import attendance_consents, attendance_faces

    raw = (ctx.payload or {}).get("local_date")
    today = date.fromisoformat(raw) if raw else clock.company_today(ctx.db, ctx.company_id)
    expired = attendance_consents.expire_due(ctx.db, ctx.company_id, today)
    purged = attendance_faces.purge_due(ctx.db, ctx.company_id, today)
    return {"expired": expired, **purged}

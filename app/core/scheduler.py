"""Leader-free periodic scheduler (LH-M1 WI-1.2; LH 02 §8.2, D-LH-28, SPINE V3-033 / V3-104).

``tick(db, now)`` runs once a minute in every worker loop (and is called directly by tests with a frozen clock). It
claims due ``scheduled_jobs`` rows with ``FOR UPDATE SKIP LOCKED`` and, for every slot between the row's
``next_run_at`` and ``now`` (catch-up, at most ``CATCH_UP_SLOTS`` per row), enqueues one platform job
``scheduler.fan_out`` with key ``sched:{job_type}:{slot}``; then it moves ``next_run_at`` past ``now``. Several
workers or API replicas may tick at once: the row lock and the job key make each slot fire once. The fan-out
(app/services/scheduler_fanout.py) turns a slot into tenant jobs; handlers are idempotent and catch up to their
horizon, so a slot beyond the catch-up limit loses nothing (LH 02 §8.2 rule 3).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

from sqlalchemy.orm import Session

from app.models.scheduler import ScheduledJob

FAN_OUT = "scheduler.fan_out"
CATCH_UP_SLOTS = 72  # LH 14 WI-1.2; a 72-hour stop replays every hourly slot once (LH 13 §4.2 exit 3)


def _floor(cadence: str, at: datetime, at_minute: int) -> datetime:
    at = at.astimezone(timezone.utc)
    if cadence == "HOURLY":
        base = at.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=at_minute % 60)
        return base if base <= at else base - timedelta(hours=1)
    if cadence == "DAILY":
        base = at.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=at_minute)
        return base if base <= at else base - timedelta(days=1)
    base = at.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=at_minute)
    return base if base <= at else base.replace(year=base.year - 1)


def _step(cadence: str, slot: datetime) -> datetime:
    if cadence == "HOURLY":
        return slot + timedelta(hours=1)
    if cadence == "DAILY":
        return slot + timedelta(days=1)
    return slot.replace(year=slot.year + 1)


def next_slot(cadence: str, at_minute: int, after: datetime) -> datetime:
    """Pure: the first slot of the cadence strictly after ``after`` (UTC)."""
    return _step(cadence, _floor(cadence, after, at_minute))


def slot_key(job_type: str, slot: datetime) -> str:
    return f"sched:{job_type}:{slot.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%MZ')}"


def due_slots(cadence: str, at_minute: int, next_run_at: datetime | None, now: datetime) -> List[datetime]:
    """Pure: the slots to fire now — from ``next_run_at`` (or the current slot when never run) up to ``now``,
    oldest first, at most CATCH_UP_SLOTS (the newest ones)."""
    current = _floor(cadence, now, at_minute)
    if next_run_at is None:
        return [current]
    if next_run_at > now:
        return []
    slots, slot = [], _floor(cadence, next_run_at, at_minute)
    while slot <= current:
        slots.append(slot)
        slot = _step(cadence, slot)
    return slots[-CATCH_UP_SLOTS:]


def tick(db: Session, now: datetime) -> int:
    """Claim due rows (SKIP LOCKED) and enqueue their fan-out slots; returns the number of slots enqueued. The caller
    commits (the worker loop commits right away; tests commit and run the jobs inline)."""
    from app.payroll.services import jobs

    rows = (db.query(ScheduledJob)
            .filter(ScheduledJob.enabled.is_(True),
                    (ScheduledJob.next_run_at.is_(None)) | (ScheduledJob.next_run_at <= now))
            .order_by(ScheduledJob.id)
            .with_for_update(skip_locked=True)
            .all())
    enqueued = 0
    for row in rows:
        for slot in due_slots(row.cadence, row.at_minute, row.next_run_at, now):
            jobs.enqueue(db, company_id=None, type=FAN_OUT, idempotency_key=slot_key(row.job_type, slot),
                         payload={"scheduled_type": row.job_type, "slot": slot.isoformat()})
            enqueued += 1
        row.last_run_at = now
        row.next_run_at = next_slot(row.cadence, row.at_minute, now)
    db.flush()
    return enqueued

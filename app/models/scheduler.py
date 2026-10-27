"""Periodic job schedule (LH-M1 WI-1.1; LH 02 §8.2, LH 11 §4.2.13; SPINE V3-033).

A platform table without ``company_id`` (a reviewed exception of the platform contract tests): one row per periodic
job type, seeded by migration ``lh_m1_scheduler``. The scheduler tick (app/core/scheduler.py) claims due rows with
``FOR UPDATE SKIP LOCKED`` and enqueues a ``scheduler.fan_out`` platform job per due slot; the application role may
only SELECT and UPDATE the rows.
"""

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, Integer, SmallInteger, String, Time

from app.models.base import BaseModel

CADENCES = ("HOURLY", "DAILY", "YEARLY")


class ScheduledJob(BaseModel):
    __tablename__ = "scheduled_jobs"

    job_type = Column(String(60), nullable=False, unique=True)
    cadence = Column(String(8), nullable=False)
    # Minute past the hour (HOURLY) or UTC minute of the day (DAILY) at which a slot starts.
    at_minute = Column(SmallInteger, nullable=False, default=0, server_default="0")
    # Daily-local job types: the local wall time each zone must reach (default 00:30, LH 02 §8.2 rule 4).
    at_local_time = Column(Time, nullable=True)
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    next_run_at = Column(DateTime(timezone=True), nullable=True)
    enabled = Column(Boolean, nullable=False, default=False, server_default="false")

    __table_args__ = (
        CheckConstraint(f"cadence IN {CADENCES}", name="ck_scheduled_jobs_cadence"),
        CheckConstraint("at_minute BETWEEN 0 AND 1439", name="ck_scheduled_jobs_at_minute"),
    )

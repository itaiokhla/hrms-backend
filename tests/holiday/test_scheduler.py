"""Leader-free scheduler (LH-M1 WI-1.2, WI-1.13; LH 02 §8.2; LH 13 §4.2 exit 3).

LH-REL-01 (a 72-hour stop then restart: every missed slot runs once), LH-REL-03 (two ticks at once: one fan-out per
slot), LH-TZ-12 (a UTC-negative tenant and India's +05:30 each process a local date exactly once), BUG-29 (a UTC−
tenant keeps getting its daily jobs without the payroll self-reschedule).
"""

from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import text

from app.core import scheduler
from app.database.db import SystemSessionLocal, system_engine
from app.models.scheduler import ScheduledJob
from app.services import local_days


def _system():
    return SystemSessionLocal()  # the platform session the worker loop ticks with


# ------------------------------------------------------------------------------------------------- pure
def test_next_slot_per_cadence():
    at = datetime(2026, 11, 1, 5, 47, tzinfo=timezone.utc)
    assert scheduler.next_slot("HOURLY", 5, at) == datetime(2026, 11, 1, 6, 5, tzinfo=timezone.utc)
    assert scheduler.next_slot("DAILY", 90, at) == datetime(2026, 11, 2, 1, 30, tzinfo=timezone.utc)
    assert scheduler.next_slot("YEARLY", 0, at) == datetime(2027, 1, 1, tzinfo=timezone.utc)


def test_lh_rel_01_due_slots_replay_a_72_hour_stop_once_each():
    stopped = datetime(2026, 11, 1, 0, 5, tzinfo=timezone.utc)
    now = stopped + timedelta(hours=72, minutes=3)
    slots = scheduler.due_slots("HOURLY", 5, stopped, now)
    # 73 slots are due (00:05 on Nov 1 .. 00:05 on Nov 4); the newest 72 run, each once, oldest first.
    assert len(slots) == scheduler.CATCH_UP_SLOTS == 72
    assert len(set(slots)) == len(slots) and slots == sorted(slots)
    assert slots[0] == datetime(2026, 11, 1, 1, 5, tzinfo=timezone.utc)
    assert slots[-1] == datetime(2026, 11, 4, 0, 5, tzinfo=timezone.utc)
    assert scheduler.due_slots("HOURLY", 5, now + timedelta(minutes=10), now) == []


def test_lh_tz_12_due_local_date_with_quarter_hour_and_negative_offsets():
    # 2026-11-01 19:05 UTC = 2026-11-02 00:35 IST (+05:30): Monday is due in India, still Sunday in New York.
    now = datetime(2026, 11, 1, 19, 5, tzinfo=timezone.utc)
    assert local_days.due_local_date(now, "Asia/Kolkata") == date(2026, 11, 2)
    assert local_days.due_local_date(now, "America/New_York") == date(2026, 11, 1)
    # 00:20 IST has not reached the 00:30 threshold yet: the due date is still the day before.
    assert local_days.due_local_date(now - timedelta(minutes=15), "Asia/Kolkata") == date(2026, 11, 1)
    # A 02:00 threshold on the US DST-end day is reached once.
    assert local_days.due_local_date(datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc), "America/New_York",
                                     time(2, 0)) == date(2026, 11, 1)


# --------------------------------------------------------------------------------------------- database
@pytest.fixture
def one_job():
    with system_engine.begin() as conn:
        conn.execute(text("UPDATE scheduled_jobs SET enabled = false"))
        conn.execute(text("UPDATE scheduled_jobs SET enabled = true, next_run_at = :t WHERE job_type = 'idempotency.purge'"),
                     {"t": datetime(2026, 11, 1, 1, 30, tzinfo=timezone.utc)})
    yield
    with system_engine.begin() as conn:
        conn.execute(text("UPDATE scheduled_jobs SET enabled = (job_type IN ('idempotency.purge', 'holiday.link_activate')), "
                          "next_run_at = CASE WHEN job_type IN ('idempotency.purge', 'holiday.link_activate') "
                          "THEN now() END, last_run_at = NULL"))


def _fan_outs():
    with system_engine.connect() as conn:
        return [r[0] for r in conn.execute(text(
            "SELECT idempotency_key FROM jobs WHERE type = 'scheduler.fan_out' ORDER BY id"))]


def test_lh_rel_01_restart_after_three_days_runs_each_missed_slot_once(one_job):
    db = _system()
    try:
        now = datetime(2026, 11, 4, 2, 0, tzinfo=timezone.utc)
        count = scheduler.tick(db, now)
        db.commit()
        assert count == 4  # the daily 01:30 slots of Nov 1, 2, 3 and 4
        assert _fan_outs() == [f"sched:idempotency.purge:2026-11-0{d}T01:30Z" for d in (1, 2, 3, 4)]
        again = scheduler.tick(db, now)
        db.commit()
        assert again == 0
        row = db.query(ScheduledJob).filter(ScheduledJob.job_type == "idempotency.purge").one()
        assert row.next_run_at == datetime(2026, 11, 5, 1, 30, tzinfo=timezone.utc)
    finally:
        db.close()


def test_lh_rel_03_two_ticks_at_once_fan_out_once_per_slot(one_job):
    first, second = _system(), _system()
    try:
        now = datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)
        a = scheduler.tick(first, now)  # holds the row lock until commit
        b = scheduler.tick(second, now)  # SKIP LOCKED: nothing to claim
        first.commit()
        second.commit()
        assert (a, b) == (1, 0)
        assert len(_fan_outs()) == 1
    finally:
        first.close()
        second.close()


def test_scheduled_jobs_is_a_platform_table_the_app_role_only_reads_and_advances():
    """V3-033: no company_id, no RLS; the application role may SELECT and UPDATE, never INSERT or DELETE."""
    import os

    from sqlalchemy.engine import make_url

    role = make_url(os.environ["DATABASE_URL"]).username
    with system_engine.connect() as conn:
        privileges = {p: conn.execute(text("SELECT has_table_privilege(:r, 'scheduled_jobs', :p)"),
                                      {"r": role, "p": p}).scalar() for p in ("SELECT", "INSERT", "UPDATE", "DELETE")}
        columns = {c for (c,) in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'scheduled_jobs'"))}
        seeded = dict(conn.execute(text("SELECT job_type, enabled FROM scheduled_jobs")).fetchall())
    assert privileges == {"SELECT": True, "INSERT": False, "UPDATE": True, "DELETE": False}
    assert "company_id" not in columns
    assert seeded["idempotency.purge"] and seeded["payroll.generate_periods"] and not seeded["leave.accrue"]


def test_bug29_utc_negative_tenant_keeps_running(app, one_job):
    """BUG-29: the payroll daily job is a scheduled type; its handler no longer re-queues itself, and the scheduler
    still fans it out every day for every tenant, also a UTC-negative one (tests run jobs inline after commit)."""
    from app.payroll.services import calendars, jobs
    from app.services import scheduler_fanout  # noqa: F401  registers scheduler.fan_out
    from tests.helpers import signup_company

    tenant = signup_company(app, "Pacific Works")
    with system_engine.begin() as conn:
        conn.execute(text("UPDATE companies SET timezone = 'America/Los_Angeles' WHERE id = :c"), {"c": tenant.company_id})
        conn.execute(text("DELETE FROM jobs WHERE company_id = :c"), {"c": tenant.company_id})  # the signup chain
        conn.execute(text("UPDATE scheduled_jobs SET enabled = (job_type = 'payroll.generate_periods'), "
                          "next_run_at = :t"), {"t": datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc)})
    assert jobs.find_handler(calendars.GENERATE_JOB) is not None
    db = _system()
    try:
        assert scheduler.tick(db, datetime(2026, 11, 3, 1, 30, tzinfo=timezone.utc)) == 3  # Nov 1, 2 and 3
        db.commit()  # the fan-outs, then the tenant jobs, run inline after this commit
    finally:
        db.close()
    assert _fan_outs() == [f"sched:payroll.generate_periods:2026-11-0{d}T01:00Z" for d in (1, 2, 3)]
    with system_engine.connect() as conn:
        tenant_jobs = conn.execute(text(
            "SELECT idempotency_key, status FROM jobs WHERE company_id = :c AND type = 'payroll.generate_periods' "
            "ORDER BY id"), {"c": tenant.company_id}).all()
        requeued = conn.execute(text("SELECT count(*) FROM jobs WHERE company_id = :c AND idempotency_key LIKE "
                                     "'generate-periods:%'"), {"c": tenant.company_id}).scalar()
        siblings = conn.execute(text("SELECT count(*) FROM jobs WHERE company_id = :c AND "
                                     "type = 'compensation.activate_scheduled'"), {"c": tenant.company_id}).scalar()
    assert [tuple(r) for r in tenant_jobs] == [
        (f"payroll.generate_periods:2026-11-0{d}T01:00:00+00:00", "SUCCEEDED") for d in (1, 2, 3)]
    assert requeued == 0  # no self-reschedule any more
    assert siblings >= 1  # the job still queues the next day's compensation activation

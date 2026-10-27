"""The scheduler's fan-out job (LH-M1 WI-1.2; LH 02 §8.2 rule 4, SPINE V3-104).

``scheduler.fan_out`` (platform job, system session) turns one slot of a periodic job type into tenant jobs: for a
daily-local type, one job per (tenant, zone, local date) whose threshold has passed, key
``{job_type}:{zone}:{local_date}`` and payload ``{local_date, timezone}``; for any other type one job per tenant and
slot, key ``{job_type}:{slot}``. Keys make repeated slots and several workers collapse into one job. A type whose
handler is not registered (shipped by a later goal) is skipped.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.core.scheduler import FAN_OUT
from app.models.company import Company
from app.models.scheduler import ScheduledJob
from app.payroll.services import jobs
from app.services import local_days


@jobs.register(FAN_OUT, platform=True)
def fan_out(ctx: jobs.JobContext) -> dict:
    db: Session = ctx.db
    job_type = ctx.payload["scheduled_type"]
    slot = datetime.fromisoformat(ctx.payload["slot"])
    row = db.query(ScheduledJob).filter(ScheduledJob.job_type == job_type).first()
    handler = jobs.find_handler(job_type)
    if row is None or handler is None:
        return {"skipped": job_type}
    if handler.platform:
        jobs.enqueue(db, company_id=None, type=job_type, payload={}, idempotency_key=f"{job_type}:{slot.isoformat()}")
        return {"tenants": 0, "jobs": 1}
    created = 0
    tenants = db.query(Company.id).filter(Company.status == "active").order_by(Company.id).all()
    for (company_id,) in tenants:
        if row.at_local_time is not None:
            for zone, local_date in local_days.zones_due(db, company_id, slot, row.at_local_time).items():
                jobs.enqueue(db, company_id=company_id, type=job_type,
                             payload={"local_date": local_date.isoformat(), "timezone": zone},
                             idempotency_key=f"{job_type}:{zone}:{local_date.isoformat()}")
                created += 1
        else:
            jobs.enqueue(db, company_id=company_id, type=job_type, payload={},
                         idempotency_key=f"{job_type}:{slot.isoformat()}")
            created += 1
    return {"tenants": len(tenants), "jobs": created}


@jobs.register("idempotency.purge", platform=True)
def purge_idempotency_keys(ctx: jobs.JobContext) -> dict:
    """Daily platform sweep of expired Idempotency-Key rows, tenant by tenant (LH 14 WI-1.2)."""
    from app.payroll.services import idempotency

    purged = 0
    for (company_id,) in ctx.db.query(Company.id).order_by(Company.id).all():
        purged += idempotency.purge_expired(ctx.db, company_id)
    return {"purged": purged}

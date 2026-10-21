"""Transactional outbox: side effects recorded in the business transaction, turned into jobs once (EC-REL-02).

`emit(db, topic, payload)` inserts an outbox_events row in the caller's transaction and queues the
platform dispatcher job in the same transaction, so a rolled-back business change leaves nothing
behind and a committed one is always dispatched. The dispatcher locks undispatched rows (SKIP LOCKED),
queues one job per subscriber with the idempotency key ``outbox:<event id>:<job type>`` and marks the
row dispatched in the same transaction as the dispatcher's own completion: each event becomes each of
its jobs exactly once, even when dispatch runs twice or concurrently. A topic without subscribers is
handled by the job type of the same name; an event nobody handles stays undispatched with an error.
"""

from typing import Any, Dict, List, Mapping, Tuple

from sqlalchemy.orm import Session

from app.core import clock
from app.database.db import TenantContextError, current_company_id
from app.models.jobs import OutboxEvent
from app.payroll.services import jobs

DISPATCH_JOB = "outbox.dispatch"
DISPATCH_BATCH = 200
SUBSCRIBERS: Dict[str, Tuple[str, ...]] = {}


def subscribe(topic: str, job_type: str) -> None:
    """Make every event of `topic` start a job of `job_type` (several subscribers fan out)."""
    current = SUBSCRIBERS.get(topic, ())
    if job_type not in current:
        SUBSCRIBERS[topic] = current + (job_type,)


def subscribers(topic: str) -> Tuple[str, ...]:
    found = SUBSCRIBERS.get(topic)
    if found:
        return found
    return (topic,) if jobs.find_handler(topic) is not None else ()


def emit(db: Session, topic: str, payload: Mapping[str, Any]) -> OutboxEvent:
    """Record a side effect of the current tenant transaction (payload: ids only)."""
    company_id = current_company_id(db)
    if company_id is None:
        raise TenantContextError("outbox events belong to a tenant transaction")
    if not topic or len(topic) > 80:
        raise ValueError("an outbox topic is 1-80 characters")
    jobs.check_payload(payload)
    row = OutboxEvent(topic=topic, payload=dict(payload), attempts=0)
    db.add(row)
    db.flush()
    jobs.enqueue(
        db, company_id=None, type=DISPATCH_JOB, payload={"event_id": row.id},
        idempotency_key=f"outbox:{company_id}:{row.id}", priority=10,
    )
    return row


def dispatch_pending(db: Session, *, limit: int = DISPATCH_BATCH) -> dict:
    """Turn undispatched events into jobs, in `db` (the system session); the caller commits."""
    now = clock.utcnow()
    rows: List[OutboxEvent] = (
        db.query(OutboxEvent)
        .filter(OutboxEvent.dispatched_at.is_(None))
        .order_by(OutboxEvent.id)
        .with_for_update(skip_locked=True)
        .limit(limit)
        .all()
    )
    dispatched = unhandled = 0
    for row in rows:
        row.attempts += 1
        targets = subscribers(row.topic)
        if not targets:
            row.last_error = f"No job type handles the topic {row.topic!r}."
            unhandled += 1
            continue
        for job_type in targets:
            jobs.enqueue(
                db, company_id=row.company_id, type=job_type, payload=dict(row.payload or {}),
                idempotency_key=f"outbox:{row.id}:{job_type}"[:120],
            )
        row.dispatched_at = now
        row.last_error = None
        dispatched += 1
    db.flush()
    return {"dispatched": dispatched, "unhandled": unhandled}


@jobs.register(DISPATCH_JOB, platform=True)
def _dispatch(ctx: jobs.JobContext) -> dict:
    return dispatch_pending(ctx.db)

"""Background jobs on the PostgreSQL `jobs` table (3.9 §6, 3.5 §16, decision I-09).

* `enqueue` inserts the job in the caller's (business) transaction, so a job exists exactly when the
  work that asked for it committed. The same (company, type, idempotency key) returns the first job
  (EC-RUN-02). Payloads carry ids only, never amounts or bank data (EC-SEC-08).
* `claim` marks due QUEUED jobs RUNNING with `SELECT ... FOR UPDATE SKIP LOCKED` through the system
  session and commits at once; `run_job` then works in a fresh session bound to the job's tenant
  (`bind_tenant`, so RLS applies exactly as for an API request) or, for a platform job, in the system
  session. The job becomes SUCCEEDED in the same transaction as the handler's work, and only while
  this worker still holds it, so a handler's effect commits at most once.
* A failure re-queues the job with exponential backoff, and makes it DEAD after `max_attempts`;
  `PermanentJobError` fails it at once (FAILED). Errors are stored redacted.
* A running job heartbeats every 30 s; `reap` re-queues RUNNING jobs whose heartbeat is older than
  5 minutes (EC-REL-01). Handlers must be idempotent: a re-queued job may run again.
* Modes (settings.jobs_mode): "inline" runs a job synchronously right after the enqueuing transaction
  commits, in a fresh session, through the same claim/run path (tests); "thread" runs an embedded
  worker thread in the API process (app.worker.start_embedded); "worker" is `python -m app.worker`.
"""

import importlib
import logging
import pkgutil
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple

from sqlalchemy import event, func, literal_column, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import settings
from app.core import clock
from app.core.errors import conflict, not_found
from app.core.principal import Principal
from app.database.db import (
    BYPASS_KEY,
    SessionLocal,
    SystemSessionLocal,
    TenantContextError,
    bind_tenant,
    current_company_id,
    system_session,
)
from app.models.jobs import Job

logger = logging.getLogger(__name__)

MODES = ("inline", "thread", "worker")
QUEUED, RUNNING, SUCCEEDED, FAILED, DEAD, CANCELLED = "QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "DEAD", "CANCELLED"
TERMINAL = (SUCCEEDED, FAILED, DEAD, CANCELLED)
HEARTBEAT_SECONDS = 30
STALE_AFTER = timedelta(minutes=5)
BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 3600
CLAIM_BATCH = 10
# Reserved payload key: the permission whose COMPANY-scope holders may read the job besides its creator.
READ_PERMISSION_KEY = "_read_permission"
_INLINE_PENDING = "payroll_jobs_inline_pending"
_TYPE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_MAX_ERROR = 2000


class PermanentJobError(Exception):
    """Raised by a handler for a failure that retrying cannot fix: the job becomes FAILED at once."""


class JobInterrupted(Exception):
    """The job was cancelled or re-queued by the reaper while running; its work is rolled back."""


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    company_id: Optional[int]
    type: str
    payload: dict
    attempts: int
    max_attempts: int
    created_by_user_id: Optional[int]


class JobContext:
    """What a handler receives: a session (tenant-bound, or the system session for platform jobs) and progress."""

    def __init__(self, db: Session, job: ClaimedJob, worker_id: str) -> None:
        self.db = db
        self.job_id = job.id
        self.company_id = job.company_id
        self.type = job.type
        self.payload = {k: v for k, v in job.payload.items() if k != READ_PERMISSION_KEY}
        self.attempt = job.attempts
        self.created_by_user_id = job.created_by_user_id
        self._worker_id = worker_id

    def progress(self, done: int, total: Optional[int] = None) -> None:
        """Record progress (and a heartbeat); raises JobInterrupted when the job was cancelled or taken away."""
        if done < 0 or (total is not None and total < 0):
            raise ValueError("progress must not be negative")
        values: Dict[str, Any] = {"progress_done": done}
        if total is not None:
            values["progress_total"] = total
        if not _touch(self.job_id, self._worker_id, values):
            raise JobInterrupted(f"job {self.job_id} is no longer running on this worker")

    def heartbeat(self) -> None:
        if not _touch(self.job_id, self._worker_id, {}):
            raise JobInterrupted(f"job {self.job_id} is no longer running on this worker")


Handler = Callable[[JobContext], Optional[dict]]
AccessCheck = Callable[[Session, Principal, Job], bool]


@dataclass(frozen=True)
class HandlerDef:
    type: str
    func: Handler
    platform: bool
    access: Optional[AccessCheck]


HANDLERS: Dict[str, HandlerDef] = {}


def register(job_type: str, *, platform: bool = False, access: Optional[AccessCheck] = None):
    """Decorator: `func(ctx) -> dict | None` handles jobs of `job_type` ("area.action").

    `platform` handlers run without a tenant on the system session; tenant handlers always get a
    session bound to the job's company. `access(db, principal, job)` may widen who can read a job.
    """
    if not _TYPE.match(job_type) or len(job_type) > 60:
        raise ValueError(f"invalid job type {job_type!r}")

    def decorator(func: Handler) -> Handler:
        existing = HANDLERS.get(job_type)
        # Re-importing the same module (reloads) re-registers the same function; anything else is a clash.
        if existing is not None and (existing.func.__module__, existing.func.__qualname__) != (
            func.__module__, func.__qualname__
        ):
            raise ValueError(f"job type {job_type!r} already has a handler")
        HANDLERS[job_type] = HandlerDef(job_type, func, platform, access)
        return func

    return decorator


# Handler modules outside app/payroll/services (JC-AS-15, LH V3-103): an explicit list, never a package scan of
# app/services. Each program goal appends the modules it ships; a module that a later goal ships is listed in
# FUTURE_HANDLER_MODULES until then and skipped while it does not exist.
HANDLER_MODULES: Tuple[str, ...] = ("app.services.scheduler_fanout", "app.services.holiday_assignments",
                                   "app.services.leave_jobs",
                                   "app.services.shift_rosters",  # AS-M5: registers the roster provider (JC-AS-31)
                                   "app.services.team_service",  # X-M1: team.membership_activate, access.assignment_expire
                                   "app.services.holiday_rollover",  # LH-M1 Goal 5: holiday.rollover / .reminders
                                   "app.services.holiday_closures",  # LH-M1 Goal 5: holiday.closure_retro
                                   "app.services.attendance_jobs",  # AS-M2: day_close, location_sweep, consent_expire
                                   "app.services.roster_consents",  # AS-M2: roster.revalidate_employee (AS-ROS-06)
                                   "app.services.attendance_requests",  # AS-M4: attendance.sla_scan + decided callback
                                   "app.services.payroll_sync",  # G-16: payroll.retro.detect
                                   "app.services.integrations",  # G-24: channels.deliver / .retry, push.tickle
                                   "app.services.team_payroll",  # team batch engine callback
                                   "app.services.attendance_readiness")  # G-23: attendance.acceptance_run
FUTURE_HANDLER_MODULES: FrozenSet[str] = frozenset({
    "app.services.scheduler_fanout", "app.services.holiday_rollover", "app.services.holiday_assignments",
    "app.services.holiday_calendar_service", "app.services.leave_jobs", "app.services.shift_catalogue",
    "app.services.attendance_jobs", "app.services.team_service", "app.services.access_changes",
})


def load_handlers() -> None:
    """Import every payroll service module and HANDLER_MODULES so their @register decorators run (worker start-up)."""
    from app.payroll import services

    for module in pkgutil.iter_modules(services.__path__):
        if module.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{services.__name__}.{module.name}")
        except Exception:
            logger.exception("Could not import job handlers from %s", module.name)
    for name in HANDLER_MODULES:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name == name and name in FUTURE_HANDLER_MODULES:
                continue
            logger.exception("Could not import job handlers from %s", name)
        except Exception:
            logger.exception("Could not import job handlers from %s", name)


def find_handler(job_type: str) -> Optional[HandlerDef]:
    found = HANDLERS.get(job_type)
    if found is None:
        load_handlers()
        found = HANDLERS.get(job_type)
    return found


# ---------------------------------------------------------------------------------------------- payloads
_SENSITIVE_WORDS = frozenset({
    "amount", "amounts", "salary", "gross", "net", "wage", "wages", "ctc", "earnings", "pay", "iban", "account",
    "routing", "ifsc", "bic", "swift", "password", "secret", "token", "otp", "pin", "pan", "ssn", "tax",
})
_ID_SUFFIXES = ("id", "ids", "count", "type", "code", "status", "key", "date", "period", "month", "year")
# Free text people write (reasons, notes, messages, diagnoses, file names) never travels in a payload (LH V3-104,
# LH-SEC-20): ``reason`` is refused, ``reason_code`` passes.
_FREE_TEXT_WORDS = frozenset({"reason", "reasons", "note", "notes", "message", "messages", "body", "comment",
                              "comments", "diagnosis", "remarks", "justification"})
_FREE_TEXT_KEYS = frozenset({"file_name", "filename"})


def _key_words(key: str) -> List[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    return [w for w in re.split(r"[^0-9a-zA-Z]+", spaced.lower()) if w]


def check_payload(payload: Mapping[str, Any], *, _depth: int = 0) -> None:
    """Payloads and outbox events carry ids and small flags only (EC-SEC-08): no decimals, no money or bank keys."""
    if not isinstance(payload, Mapping):
        raise ValueError("a job payload must be a JSON object")
    if _depth > 4:
        raise ValueError("a job payload may nest at most four levels")
    for key, value in payload.items():
        if not isinstance(key, str) or not key or len(key) > 60:
            raise ValueError("job payload keys are short strings")
        words = _key_words(key)
        if words and words[-1] not in _ID_SUFFIXES and _SENSITIVE_WORDS.intersection(words):
            raise ValueError(f"job payload key {key!r} looks like money, bank or credential data; pass ids only")
        if words and words[-1] not in _ID_SUFFIXES and (
                _FREE_TEXT_WORDS.intersection(words) or "_".join(words) in _FREE_TEXT_KEYS):
            raise ValueError(f"job payload key {key!r} looks like free text; pass ids or codes only")
        _check_value(key, value, _depth)


def _check_value(key: str, value: Any, depth: int) -> None:
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, str):
        if len(value) > 200:
            raise ValueError(f"job payload value {key!r} is too long; pass ids only")
        return
    if isinstance(value, Mapping):
        check_payload(value, _depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > 10000:
            raise ValueError(f"job payload list {key!r} is too long; split the work into chunks")
        for item in value:
            if isinstance(item, Mapping):
                check_payload(item, _depth=depth + 1)
            elif isinstance(item, (list, tuple)):
                raise ValueError("job payload lists may not nest")
            else:
                _check_value(key, item, depth)
        return
    raise ValueError(f"job payload value {key!r} has type {type(value).__name__}; use ids, strings and flags")


# ---------------------------------------------------------------------------------------------- enqueue
def enqueue(
    db: Session,
    *,
    company_id: Optional[int],
    type: str,  # noqa: A002 - the jobs column name, fixed by the coordination contract
    payload: Mapping[str, Any],
    idempotency_key: Optional[str] = None,
    run_at: Optional[datetime] = None,
    priority: int = 100,
    max_attempts: int = 5,
    parent_job_id: Optional[int] = None,
    created_by_user_id: Optional[int] = None,
    read_permission: Optional[str] = None,
) -> Job:
    """Queue a job in the caller's transaction; the same (company, type, idempotency key) returns the first job."""
    bound = current_company_id(db)
    if company_id is not None:
        if bound is not None and bound != company_id:
            raise TenantContextError("a job cannot be queued for another company")
        if bound is None and not db.info.get(BYPASS_KEY):
            raise TenantContextError("a tenant job needs a tenant-bound session")
    if not _TYPE.match(type or "") or len(type) > 60:
        raise ValueError(f"invalid job type {type!r}")
    if idempotency_key is not None and not (0 < len(idempotency_key) <= 120):
        raise ValueError("idempotency keys are 1-120 characters")
    if max_attempts < 1 or isinstance(priority, bool) or not isinstance(priority, int):
        raise ValueError("max_attempts must be at least 1 and priority an integer")
    check_payload(payload)
    data = dict(payload)
    data.pop(READ_PERMISSION_KEY, None)
    if read_permission is not None:
        data[READ_PERMISSION_KEY] = read_permission
    now = clock.utcnow()
    statement = insert(Job).values(
        company_id=company_id,
        type=type,
        payload=data,
        status=QUEUED,
        priority=priority,
        run_at=run_at or now,
        attempts=0,
        max_attempts=max_attempts,
        progress_done=0,
        parent_job_id=parent_job_id,
        idempotency_key=idempotency_key,
        created_by_user_id=created_by_user_id,
        created_at=now,
        updated_at=now,
    )
    if idempotency_key is not None:
        statement = statement.on_conflict_do_nothing(
            index_elements=[literal_column("COALESCE(company_id, 0)"), "type", "idempotency_key"],
            index_where=literal_column("idempotency_key").isnot(None),
        )
    job_id = db.execute(statement.returning(Job.id)).scalar()
    if job_id is None:
        return (
            db.query(Job)
            .filter(
                func.coalesce(Job.company_id, 0) == (company_id or 0),
                Job.type == type,
                Job.idempotency_key == idempotency_key,
            )
            .one()
        )
    if settings.jobs_mode == "inline":
        _schedule_inline(db, job_id)
    return db.get(Job, job_id)


def _schedule_inline(db: Session, job_id: int) -> None:
    pending = db.info.get(_INLINE_PENDING)
    if pending is None:
        pending = db.info[_INLINE_PENDING] = []
        event.listen(db, "after_commit", _run_inline_after_commit)
        event.listen(db, "after_soft_rollback", _discard_inline)
    pending.append(job_id)


def _discard_inline(session: Session, previous_transaction) -> None:
    if previous_transaction.parent is None:
        session.info.get(_INLINE_PENDING, []).clear()


def _run_inline_after_commit(session: Session) -> None:
    # SQLAlchemy also emits after_commit when a SAVEPOINT is released (``begin_nested``). The enclosing transaction
    # is still open then, so the claiming connection cannot see its jobs yet: they would be dropped from the list
    # and never run inline (F-68). Keep them for the outermost commit.
    if session.in_nested_transaction():
        return
    pending = session.info.get(_INLINE_PENDING) or []
    ids = list(pending)
    pending.clear()
    for job_id in ids:
        try:
            run_pending(ids=[job_id])
        except Exception:  # the enqueuing transaction already committed; never fail it afterwards
            logger.exception("Inline job %s could not run", job_id)


# ---------------------------------------------------------------------------------------------- execution
def backoff(attempts: int) -> timedelta:
    """Delay before retry number `attempts` (1, 2, 3 ...): 30 s, 60 s, 120 s ... capped at one hour."""
    return timedelta(seconds=min(BACKOFF_BASE_SECONDS * 2 ** max(attempts - 1, 0), BACKOFF_MAX_SECONDS))


def claim(worker_id: str, *, limit: int = CLAIM_BATCH, ids: Optional[Iterable[int]] = None,
          now: Optional[datetime] = None) -> List[ClaimedJob]:
    """Mark up to `limit` due QUEUED jobs RUNNING for `worker_id` (SKIP LOCKED) and commit."""
    now = now or clock.utcnow()
    params: Dict[str, Any] = {"w": worker_id, "now": now, "n": limit}
    only = ""
    if ids is not None:
        params["ids"] = list(ids)
        only = "AND id = ANY(:ids) "
    # The picked ids are MATERIALIZED: a plain ``id IN (SELECT ... LIMIT n SKIP LOCKED)`` may be re-evaluated by the
    # planner (semi-join rescans) and claim more than ``n`` rows once the table's statistics change.
    statement = text(
        "WITH picked AS MATERIALIZED (SELECT id FROM jobs WHERE status = 'QUEUED' AND run_at <= :now " + only +
        "ORDER BY priority, run_at, id LIMIT :n FOR UPDATE SKIP LOCKED) "
        "UPDATE jobs SET status = 'RUNNING', locked_by = :w, locked_at = :now, heartbeat_at = :now, "
        "attempts = attempts + 1, updated_at = :now "
        "FROM picked WHERE jobs.id = picked.id "
        "RETURNING jobs.id, jobs.company_id, jobs.type, jobs.payload, jobs.attempts, jobs.max_attempts, "
        "jobs.created_by_user_id, jobs.priority, jobs.run_at"
    )
    with system_session() as db:
        rows = db.execute(statement, params).all()
        db.commit()
    rows = sorted(rows, key=lambda r: (r.priority, r.run_at, r.id))
    return [
        ClaimedJob(r.id, r.company_id, r.type, dict(r.payload or {}), r.attempts, r.max_attempts, r.created_by_user_id)
        for r in rows
    ]


def run_pending(*, ids: Optional[Iterable[int]] = None, limit: int = CLAIM_BATCH, worker_id: Optional[str] = None) -> int:
    """Claim and run due jobs (all due ones, or only `ids`); returns how many ran."""
    worker_id = worker_id or default_worker_id("inline")
    claimed = claim(worker_id, limit=limit, ids=ids)
    for job in claimed:
        run_job(job, worker_id)
    return len(claimed)


def default_worker_id(kind: str) -> str:
    import os
    import socket
    import uuid

    return f"{kind}:{socket.gethostname()[:60]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _touch(job_id: int, worker_id: str, values: Dict[str, Any]) -> bool:
    now = clock.utcnow()
    with system_session() as db:
        result = db.execute(
            update(Job)
            .where(Job.id == job_id, Job.locked_by == worker_id, Job.status == RUNNING)
            .values(heartbeat_at=now, updated_at=now, **values)
        )
        db.commit()
    return result.rowcount == 1


def _heartbeat_loop(job_id: int, worker_id: str, stop: threading.Event) -> None:
    while not stop.wait(HEARTBEAT_SECONDS):
        try:
            if not _touch(job_id, worker_id, {}):
                return
        except Exception:
            logger.exception("Heartbeat of job %s failed", job_id)


def run_job(job: ClaimedJob, worker_id: str) -> str:
    """Execute one claimed job; returns its new status (or "INTERRUPTED" when it was taken away)."""
    handler = find_handler(job.type)
    if job.company_id is None:
        db = SystemSessionLocal()
    else:
        db = SessionLocal()
        bind_tenant(db, job.company_id)
    stop = threading.Event()
    beat = threading.Thread(target=_heartbeat_loop, args=(job.id, worker_id, stop), daemon=True,
                            name=f"job-heartbeat-{job.id}")
    beat.start()
    try:
        if handler is None:
            raise LookupError(f"No handler is registered for job type {job.type!r}")
        if handler.platform != (job.company_id is None):
            raise PermanentJobError("A platform job type was queued for a company, or the reverse")
        result = handler.func(JobContext(db, job, worker_id))
        if result is not None and not isinstance(result, dict):
            raise PermanentJobError("A job handler must return a JSON object or None")
        stop.set()
        beat.join()
        now = clock.utcnow()
        finished = db.execute(
            update(Job)
            .where(Job.id == job.id, Job.locked_by == worker_id, Job.status == RUNNING)
            .values(status=SUCCEEDED, result=result, error=None, locked_by=None, locked_at=None,
                    heartbeat_at=now, updated_at=now)
        )
        if finished.rowcount != 1:
            db.rollback()
            logger.warning("Job %s (%s) was cancelled or re-queued while running; its work was rolled back",
                           job.id, job.type)
            return "INTERRUPTED"
        db.commit()
        return SUCCEEDED
    except JobInterrupted:
        db.rollback()
        logger.info("Job %s (%s) was interrupted", job.id, job.type)
        return "INTERRUPTED"
    except Exception as exc:  # every failure is recorded on the job, never raised to the worker loop
        db.rollback()
        return _record_failure(job, worker_id, exc)
    finally:
        stop.set()
        db.close()


def _record_failure(job: ClaimedJob, worker_id: str, exc: Exception) -> str:
    message = redact(f"{type(exc).__name__}: {exc}")[:_MAX_ERROR]
    now = clock.utcnow()
    if isinstance(exc, PermanentJobError):
        status, run_at = FAILED, None
    elif job.attempts >= job.max_attempts:
        status, run_at = DEAD, None
    else:
        status, run_at = QUEUED, now + backoff(job.attempts)
    values: Dict[str, Any] = {"status": status, "error": message, "locked_by": None, "locked_at": None, "updated_at": now}
    if run_at is not None:
        values["run_at"] = run_at
    with system_session() as db:
        db.execute(
            update(Job).where(Job.id == job.id, Job.locked_by == worker_id, Job.status == RUNNING).values(**values)
        )
        db.commit()
    log = logger.error if status in (DEAD, FAILED) else logger.warning
    log("Job %s (%s) attempt %s/%s failed -> %s: %s", job.id, job.type, job.attempts, job.max_attempts, status, message)
    return status


def reap(now: Optional[datetime] = None) -> int:
    """Re-queue RUNNING jobs whose heartbeat is older than 5 minutes, or make them DEAD when out of attempts (EC-REL-01)."""
    now = now or clock.utcnow()
    with system_session() as db:
        rows = db.execute(
            text(
                "SELECT id, attempts, max_attempts FROM jobs WHERE status = 'RUNNING' "
                "AND (heartbeat_at IS NULL OR heartbeat_at < :cutoff) ORDER BY id FOR UPDATE SKIP LOCKED"
            ),
            {"cutoff": now - STALE_AFTER},
        ).all()
        for row in rows:
            dead = row.attempts >= row.max_attempts
            db.execute(
                update(Job)
                .where(Job.id == row.id)
                .values(
                    status=DEAD if dead else QUEUED,
                    run_at=now,
                    locked_by=None,
                    locked_at=None,
                    updated_at=now,
                    error="The worker stopped responding (no heartbeat for 5 minutes)"
                    + ("; no attempts left." if dead else "; the job was queued again."),
                )
            )
        db.commit()
    if rows:
        logger.warning("Reaper re-queued or retired %s stale job(s)", len(rows))
    return len(rows)


def cancel(db: Session, job_id: int, *, company_id: int) -> Job:
    """Cancel a QUEUED or RUNNING job of `company_id` and its unfinished children (a running handler stops at
    its next progress call and its work is rolled back). Callers authorize and audit."""
    job = db.query(Job).filter(Job.id == job_id, Job.company_id == company_id).with_for_update().one_or_none()
    if job is None:
        raise not_found("Job")
    if job.status in TERMINAL:
        raise conflict("This job has already finished.", "JOB_FINISHED")
    now = clock.utcnow()
    frontier = [job.id]
    while frontier:
        db.execute(
            update(Job)
            .where(Job.id.in_(frontier), Job.status.in_((QUEUED, RUNNING)))
            .values(status=CANCELLED, error="Cancelled", updated_at=now)
            .execution_options(synchronize_session=False)
        )
        frontier = [
            child_id for (child_id,) in db.query(Job.id).filter(
                Job.parent_job_id.in_(frontier), Job.status.in_((QUEUED, RUNNING))
            )
        ]
    db.refresh(job)
    return job


# ---------------------------------------------------------------------------------------------- reading
def can_read(db: Session, principal: Principal, job: Job) -> bool:
    if job.company_id != principal.company_id:
        return False
    if job.created_by_user_id is not None and job.created_by_user_id == principal.user_id:
        return True
    permission = (job.payload or {}).get(READ_PERMISSION_KEY)
    if permission and principal.has_company_scope(permission):
        return True
    handler = HANDLERS.get(job.type)
    return bool(handler and handler.access and handler.access(db, principal, job))


def get_for_principal(db: Session, principal: Principal, job_id: int) -> Job:
    """The job if the principal may see it; another tenant's job or a forbidden one is a 404."""
    job = db.query(Job).filter(Job.id == job_id, Job.company_id == principal.company_id).one_or_none()
    if job is None or not can_read(db, principal, job):
        raise not_found("Job")
    return job


def serialize(job: Job) -> dict:
    total = job.progress_total
    percent = None
    if total:
        percent = min(100, job.progress_done * 100 // total)
    elif job.status == SUCCEEDED:
        percent = 100
    return {
        "id": job.id,
        "type": job.type,
        "status": job.status,
        "progress": {"done": job.progress_done, "total": total, "percent": percent},
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "run_at": job.run_at,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "finished": job.status in TERMINAL,
        "result": job.result if job.status == SUCCEEDED else None,
        "error": job.error if job.status != SUCCEEDED else None,
    }


# ---------------------------------------------------------------------------------------------- redaction
_REDACTIONS = (
    # psycopg2 DETAIL lines echo the conflicting key values.
    (re.compile(r"(?im)^(\s*DETAIL:).*$"), r"\1 [redacted]"),
    (re.compile(r"\[parameters: .*?\](?=\s*(?:\(|$))", re.S), "[parameters: redacted]"),
    (re.compile(r"\bv1:[A-Za-z0-9][A-Za-z0-9_.-]{0,63}:[A-Za-z0-9+/=]{8,}:[A-Za-z0-9+/=]{8,}"), "[ciphertext]"),
    (re.compile(r"(?i)\b(password|passwd|secret|token|otp|pin|iban|account_number|account_no|accountNumber|"
                r"account|routing|ifsc|salary|amount|gross|net|net_pay|bonus|wage|wages|ctc|tax_id|pan|ssn)"
                r"(\"?'?\s*[=:]\s*[\"']?)(?!\[redacted\])([^\s&,;'\"}\]]+)"), r"\1\2[redacted]"),
    # IBANs (with or without spaces) and long digit runs (account and card numbers).
    (re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b"), "[redacted]"),
    (re.compile(r"(?<![\d.\-:/])\d(?:[ -]?\d){8,17}(?![\d.\-:/])"), "[redacted]"),
)


def redact(value: str) -> str:
    """Remove bank, salary and credential values from a log line or error text (EC-SEC-08)."""
    text_value = str(value)
    for pattern, replacement in _REDACTIONS:
        text_value = pattern.sub(replacement, text_value)
    return text_value


def _redact_arg(value: Any) -> Any:
    return redact(value) if isinstance(value, str) else value


class RedactingFilter(logging.Filter):
    """Logging filter applying `redact` to messages, arguments and tracebacks (install on handlers).

    Arguments are redacted in place, so formatters that read `record.args` (uvicorn's access log
    unpacks them) keep working; only when the composed message would still leak is it replaced by
    its redacted text.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, Mapping):
            record.args = {key: _redact_arg(value) for key, value in record.args.items()}
        elif isinstance(record.args, tuple):
            record.args = tuple(_redact_arg(value) for value in record.args)
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        cleaned = redact(message)
        if cleaned != message and not record.name.startswith("uvicorn.access"):
            record.msg, record.args = cleaned, None
        if record.exc_info and not record.exc_text:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install_log_redaction() -> None:
    """Attach one RedactingFilter to the root and uvicorn handlers (idempotent)."""
    loggers = [logging.getLogger(), logging.getLogger("uvicorn"), logging.getLogger("uvicorn.error"),
               logging.getLogger("uvicorn.access")]
    for log in loggers:
        for handler in log.handlers:
            if not any(isinstance(f, RedactingFilter) for f in handler.filters):
                handler.addFilter(RedactingFilter())


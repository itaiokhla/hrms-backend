"""Background jobs, Idempotency-Key records and the transactional outbox (3.9 §3.9, §6; 3.5 §16)."""

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import BaseModel, TenantMixin, utcnow

JOB_STATUSES = ("QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "DEAD", "CANCELLED")
IDEMPOTENT_METHODS = ("POST", "PUT", "PATCH", "DELETE")


class Job(BaseModel):
    """A unit of background work claimed with FOR UPDATE SKIP LOCKED (decision I-09).

    company_id NULL is a platform job. The table has no row-level security (the worker claims
    across tenants through the system session) and is not a TenantMixin model: code reading jobs
    for a tenant filters company_id itself. A parent is a platform job or a job of the same
    company (trigger). Payloads carry ids only, never amounts or bank data (EC-SEC-08).
    """

    __tablename__ = "jobs"

    company_id = Column(Integer, ForeignKey("companies.id", ondelete="RESTRICT"), nullable=True, index=True)
    type = Column(String(60), nullable=False)
    payload = Column(JSONB, nullable=False, default=dict)
    status = Column(String(12), nullable=False, default="QUEUED")
    # Lower runs first.
    priority = Column(Integer, nullable=False, default=100)
    run_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=5)
    locked_by = Column(String(120), nullable=True)
    locked_at = Column(DateTime(timezone=True), nullable=True)
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    progress_done = Column(Integer, nullable=False, default=0)
    progress_total = Column(Integer, nullable=True)
    result = Column(JSONB, nullable=True)
    # Redacted: never database DETAIL lines or payload values.
    error = Column(Text, nullable=True)
    parent_job_id = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=True, index=True)
    # Enqueueing the same (company, type, key) twice returns the first job (EC-RUN-02).
    idempotency_key = Column(String(120), nullable=True)
    created_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint(f"status IN {JOB_STATUSES}", name="ck_jobs_status"),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="ck_jobs_attempts"),
        CheckConstraint(
            "progress_done >= 0 AND (progress_total IS NULL OR progress_total >= 0)", name="ck_jobs_progress"
        ),
        CheckConstraint(
            "status <> 'RUNNING' OR (locked_by IS NOT NULL AND locked_at IS NOT NULL)", name="ck_jobs_running_lock"
        ),
        CheckConstraint("parent_job_id IS NULL OR parent_job_id <> id", name="ck_jobs_not_own_parent"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_jobs_payload"),
        Index(
            "uq_jobs_idempotency", text("COALESCE(company_id, 0)"), "type", "idempotency_key", unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("ix_jobs_queue", "status", "run_at", "priority", postgresql_where=text("status = 'QUEUED'")),
        Index("ix_jobs_running_heartbeat", "heartbeat_at", postgresql_where=text("status = 'RUNNING'")),
    )


class IdempotencyKey(TenantMixin, BaseModel):
    """The first outcome of a state-changing request, replayed for a repeat of the same key (3.5 §16).

    status_code NULL means the first request is still in progress. A repeat with a different
    request_hash is a conflict (EC-RUN-02, EC-RUN-04). Rows are purged after expires_at.
    """

    __tablename__ = "idempotency_keys"

    user_id = Column(Integer, nullable=False)
    key = Column(String(120), nullable=False)
    method = Column(String(10), nullable=False)
    path = Column(String(500), nullable=False)
    # SHA-256 (hex) of the canonical request.
    request_hash = Column(CHAR(64), nullable=False)
    status_code = Column(Integer, nullable=True)
    # Minimal response (resource id and status), never salary or bank data.
    response = Column(JSONB, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_idempotency_keys_company_id_id"),
        UniqueConstraint("company_id", "user_id", "key", name="uq_idempotency_keys_user_key"),
        ForeignKeyConstraint(
            ["company_id", "user_id"], ["users.company_id", "users.id"],
            ondelete="CASCADE", name="fk_idempotency_keys_user",
        ),
        CheckConstraint(f"method IN {IDEMPOTENT_METHODS}", name="ck_idempotency_keys_method"),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="ck_idempotency_keys_request_hash"),
        CheckConstraint(
            "status_code IS NULL OR status_code BETWEEN 100 AND 599", name="ck_idempotency_keys_status_code"
        ),
    )


class OutboxEvent(TenantMixin, BaseModel):
    """A side effect recorded in the business transaction and dispatched after commit (EC-REL-02)."""

    __tablename__ = "outbox_events"

    topic = Column(String(80), nullable=False)
    payload = Column(JSONB, nullable=False, default=dict)
    dispatched_at = Column(DateTime(timezone=True), nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_outbox_events_company_id_id"),
        CheckConstraint("attempts >= 0", name="ck_outbox_events_attempts"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_outbox_events_payload"),
        Index("ix_outbox_events_pending", "created_at", postgresql_where=text("dispatched_at IS NULL")),
    )

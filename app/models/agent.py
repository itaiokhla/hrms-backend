"""AI assistant conversations and prepared actions (docs/leave_agent.md §10; migration 0068_agent_tables).

* ``agent_threads`` — one private conversation of one user (owner = company + user; module ``leave`` first).
* ``agent_messages`` — the canonical user / assistant / system_event messages shown in the history UI. Never the
  model's hidden reasoning or raw tool payloads; ``client_message_id`` deduplicates a retried send per user.
* ``agent_actions`` — a proposed state change prepared by a tool (``prepared``) that only the deterministic confirm
  endpoint executes (``executing`` -> ``completed`` / ``failed``), or that is ``cancelled`` / ``expired``. The
  confirmation token is never stored: it is an HMAC over the row's id, owner, expiry and ``confirmation_nonce``.

Ids are random UUID strings (no enumeration). Every reference between these rows and to users is a composite
``(company_id, id)`` key with RESTRICT deletes: retention is an explicit archival job, never a cascade.
"""

import uuid

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKeyConstraint, Index, Integer, String, Text
from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import BaseModel, TenantMixin

THREAD_STATUSES = ("active", "archived")
MESSAGE_ROLES = ("user", "assistant", "system_event")
ACTION_STATUSES = ("prepared", "executing", "completed", "cancelled", "expired", "failed")
ACTION_RISKS = ("R2", "R3")
MESSAGE_MAX_CHARS = 20000


def new_id() -> str:
    return str(uuid.uuid4())


def _in(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _fk(table: str, column: str, target: str, name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["company_id", column], [f"{target}.company_id", f"{target}.id"],
                                name=f"fk_{table}_{name}", ondelete="RESTRICT")


class AgentThread(TenantMixin, BaseModel):
    __tablename__ = "agent_threads"

    id = Column(String(36), primary_key=True, default=new_id)
    user_id = Column(Integer, nullable=False)
    module = Column(String(30), nullable=False, default="leave")
    title = Column(String(120), nullable=True)
    status = Column(String(10), nullable=False, default="active")

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_agent_threads_company_id_id"),
        _fk("agent_threads", "user_id", "users", "user"),
        CheckConstraint(f"status IN {_in(THREAD_STATUSES)}", name="ck_agent_threads_status"),
        CheckConstraint("module ~ '^[a-z][a-z_]{1,29}$'", name="ck_agent_threads_module"),
        Index("ix_agent_threads_owner", "company_id", "user_id", "updated_at"),
    )


class AgentMessage(TenantMixin, BaseModel):
    __tablename__ = "agent_messages"

    id = Column(String(36), primary_key=True, default=new_id)
    thread_id = Column(String(36), nullable=False)
    user_id = Column(Integer, nullable=False)
    role = Column(String(12), nullable=False)
    content = Column(Text, nullable=False)
    client_message_id = Column(String(100), nullable=True)
    # The user message an assistant / system_event message answers (turn replay after a client retry).
    reply_to_id = Column(String(36), nullable=True)
    metadata_json = Column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_agent_messages_company_id_id"),
        # NULLs are distinct: only messages that carry a client id are deduplicated.
        UniqueConstraint("company_id", "user_id", "client_message_id", name="uq_agent_messages_client_message"),
        _fk("agent_messages", "thread_id", "agent_threads", "thread"),
        _fk("agent_messages", "user_id", "users", "user"),
        _fk("agent_messages", "reply_to_id", "agent_messages", "reply_to"),
        CheckConstraint(f"role IN {_in(MESSAGE_ROLES)}", name="ck_agent_messages_role"),
        CheckConstraint(f"char_length(content) <= {MESSAGE_MAX_CHARS}", name="ck_agent_messages_content"),
        CheckConstraint("jsonb_typeof(metadata_json) = 'object'", name="ck_agent_messages_metadata"),
        Index("ix_agent_messages_thread", "company_id", "thread_id", "created_at"),
        Index("ix_agent_messages_owner", "company_id", "user_id", "created_at"),
    )


class AgentAction(TenantMixin, BaseModel):
    __tablename__ = "agent_actions"

    id = Column(String(36), primary_key=True, default=new_id)
    thread_id = Column(String(36), nullable=False)
    user_id = Column(Integer, nullable=False)
    # The user message whose turn prepared the action.
    message_id = Column(String(36), nullable=True)
    module = Column(String(30), nullable=False)
    action_type = Column(String(60), nullable=False)
    permission_code = Column(String(60), nullable=False)
    risk = Column(String(4), nullable=False)
    payload_json = Column(JSONB, nullable=False, default=dict)
    preview_json = Column(JSONB, nullable=False, default=dict)
    status = Column(String(10), nullable=False, default="prepared")
    idempotency_key = Column(String(80), nullable=False)
    confirmation_nonce = Column(String(64), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    result_json = Column(JSONB, nullable=True)
    failure_code = Column(String(60), nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_agent_actions_company_id_id"),
        UniqueConstraint("company_id", "idempotency_key", name="uq_agent_actions_idempotency"),
        _fk("agent_actions", "thread_id", "agent_threads", "thread"),
        _fk("agent_actions", "user_id", "users", "user"),
        _fk("agent_actions", "message_id", "agent_messages", "message"),
        CheckConstraint(f"status IN {_in(ACTION_STATUSES)}", name="ck_agent_actions_status"),
        CheckConstraint(f"risk IN {_in(ACTION_RISKS)}", name="ck_agent_actions_risk"),
        CheckConstraint("status <> 'completed' OR completed_at IS NOT NULL", name="ck_agent_actions_completed"),
        CheckConstraint("jsonb_typeof(payload_json) = 'object' AND jsonb_typeof(preview_json) = 'object'",
                        name="ck_agent_actions_json"),
        Index("ix_agent_actions_thread", "company_id", "thread_id", "created_at"),
        Index("ix_agent_actions_owner", "company_id", "user_id", "status", "expires_at"),
    )

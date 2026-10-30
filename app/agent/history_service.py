"""User-owned conversations and their messages (docs/leave_agent.md §10, §19).

Every query names the company *and* the signed-in user, beside the ORM tenant filter and PostgreSQL RLS: another
user's or another company's thread is the same generic 404 as a thread that does not exist. History is application
data, never authorization state: permissions are rebuilt on every request and tools on every turn.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.agent import action_service
from app.core import clock
from app.core.errors import not_found
from app.core.principal import Principal
from app.models.agent import AgentAction, AgentMessage, AgentThread, new_id

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MODEL_MESSAGE_MAX_CHARS = 2000


def valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_UUID.match(value))


def create_thread(db: Session, principal: Principal, module: str, title: Optional[str] = None) -> AgentThread:
    thread = AgentThread(id=new_id(), company_id=principal.company_id, user_id=principal.user_id, module=module,
                         title=(title or "").strip()[:120] or None, status="active")
    db.add(thread)
    db.flush()
    return thread


def get_owned_thread(db: Session, principal: Principal, thread_id: str, *, lock: bool = False) -> AgentThread:
    if not valid_id(thread_id):
        raise not_found("Conversation")
    query = db.query(AgentThread).filter(AgentThread.id == thread_id,
                                         AgentThread.company_id == principal.company_id,
                                         AgentThread.user_id == principal.user_id)
    if lock:
        query = query.with_for_update()
    thread = query.one_or_none()
    if thread is None:
        raise not_found("Conversation")
    return thread


def list_threads(db: Session, principal: Principal, limit: int = 30) -> List[AgentThread]:
    return (db.query(AgentThread)
            .filter(AgentThread.company_id == principal.company_id, AgentThread.user_id == principal.user_id,
                    AgentThread.status == "active")
            .order_by(AgentThread.updated_at.desc(), AgentThread.id)
            .limit(limit).all())


def thread_json(thread: AgentThread) -> Dict[str, Any]:
    return {"id": thread.id, "module": thread.module, "title": thread.title, "status": thread.status,
            "created_at": thread.created_at.isoformat() if thread.created_at else None,
            "updated_at": thread.updated_at.isoformat() if thread.updated_at else None}


def message_json(message: AgentMessage, blocks: Optional[List[dict]] = None) -> Dict[str, Any]:
    return {"id": message.id, "role": message.role, "content": message.content,
            "created_at": message.created_at.isoformat() if message.created_at else None,
            "blocks": blocks or []}


def blocks_for(db: Session, principal: Principal, messages: List[AgentMessage]) -> Dict[str, List[dict]]:
    """Typed action blocks per assistant message, built from the action rows' current state."""
    wanted = {m.id: list((m.metadata_json or {}).get("action_ids") or []) for m in messages if m.role == "assistant"}
    actions = action_service.actions_by_id(db, principal, sorted({a for ids in wanted.values() for a in ids}))
    now = clock.utcnow()
    return {mid: [action_service.action_block(actions[a], now) for a in ids if a in actions]
            for mid, ids in wanted.items()}


def list_messages(db: Session, principal: Principal, thread: AgentThread, limit: int = 100
                  ) -> Tuple[List[Dict[str, Any]], bool]:
    rows = (db.query(AgentMessage)
            .filter(AgentMessage.company_id == principal.company_id, AgentMessage.user_id == principal.user_id,
                    AgentMessage.thread_id == thread.id)
            .order_by(AgentMessage.created_at.desc(), AgentMessage.id.desc())
            .limit(limit + 1).all())
    has_more = len(rows) > limit
    rows = list(reversed(rows[:limit]))
    blocks = blocks_for(db, principal, rows)
    return [message_json(m, blocks.get(m.id)) for m in rows], has_more


def find_client_message(db: Session, principal: Principal, client_message_id: str) -> Optional[AgentMessage]:
    return (db.query(AgentMessage)
            .filter(AgentMessage.company_id == principal.company_id, AgentMessage.user_id == principal.user_id,
                    AgentMessage.client_message_id == client_message_id)
            .one_or_none())


def reply_to(db: Session, principal: Principal, message_id: str) -> Optional[AgentMessage]:
    return (db.query(AgentMessage)
            .filter(AgentMessage.company_id == principal.company_id, AgentMessage.user_id == principal.user_id,
                    AgentMessage.reply_to_id == message_id, AgentMessage.role == "assistant")
            .order_by(AgentMessage.created_at.desc())
            .first())


def recent_for_model(db: Session, principal: Principal, thread: AgentThread, exclude_id: str, window: int
                     ) -> List[Tuple[str, str]]:
    """The last ``window`` messages as (role, text) for the model: user -> "user", assistant and system events ->
    "assistant" (status updates are marked). Each is cut to MODEL_MESSAGE_MAX_CHARS; consecutive roles merge."""
    rows = (db.query(AgentMessage)
            .filter(AgentMessage.company_id == principal.company_id, AgentMessage.user_id == principal.user_id,
                    AgentMessage.thread_id == thread.id, AgentMessage.id != exclude_id)
            .order_by(AgentMessage.created_at.desc(), AgentMessage.id.desc())
            .limit(window).all())
    out: List[Tuple[str, str]] = []
    for message in reversed(rows):
        role = "user" if message.role == "user" else "assistant"
        text = (message.content or "")[:MODEL_MESSAGE_MAX_CHARS]
        if message.role == "system_event":
            text = f"(HRMS status update) {text}"
        if out and out[-1][0] == role:
            out[-1] = (role, f"{out[-1][1]}\n\n{text}")
        else:
            out.append((role, text))
    return out


def turn_actions(db: Session, principal: Principal, message_id: str) -> List[AgentAction]:
    return (db.query(AgentAction)
            .filter(AgentAction.company_id == principal.company_id, AgentAction.user_id == principal.user_id,
                    AgentAction.message_id == message_id)
            .order_by(AgentAction.created_at, AgentAction.id).all())

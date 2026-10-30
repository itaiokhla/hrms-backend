"""Assistant API (docs/leave_agent.md §18). Prefix ``/api/v1/agent``.

Every route needs a signed-in tenant user (``get_principal``: session cookie + CSRF on writes, tenant bound, RLS);
partner delegates get 403 on all of them. Threads and actions are looked up by company + user + id: anything else is
the same generic 404. ``GET /status`` always answers (the sidebar entry depends on it); every other route answers
503 ``AGENT_DISABLED`` while ``AGENT_ENABLED`` is false, the LangChain runtime is missing or the provider is not
configured. Chat turns and confirmations are rate limited per user (turns also per company) and request bodies are
size limited. The provider / model is operator configuration and never comes from the browser.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.agent import action_service, history_service, model_factory, registry
from app.config import settings
from app.core.errors import AppError, forbidden
from app.core.principal import Principal, get_principal
from app.core.rate_limit import limiter
from app.database.db import get_db
from app.schemas.agent import ActionConfirm, ChatTurnCreate, ThreadCreate

router = APIRouter(prefix="/api/v1/agent", tags=["Assistant"])

MAX_BODY_BYTES = 32 * 1024
CONFIRMS_PER_MINUTE = 30
THREADS_PER_MINUTE = 20


def agent_principal(principal: Principal = Depends(get_principal)) -> Principal:
    if principal.delegation is not None:  # a partner operator is never a conversation owner
        raise forbidden()
    return principal


def available_principal(request: Request, principal: Principal = Depends(agent_principal)) -> Principal:
    if not model_factory.is_available():
        raise AppError(503, "The assistant is not available for this workspace.", "AGENT_DISABLED")
    length = request.headers.get("content-length")
    if length is not None and (not length.isdigit() or int(length) > MAX_BODY_BYTES):
        raise AppError(413, "The request is too large.", "PAYLOAD_TOO_LARGE")
    return principal


def _limit(key: str, limit: int) -> None:
    try:
        limiter.check(key, limit, 60)
    except AppError as exc:
        raise AppError(429, "Too many assistant requests. Wait a moment and try again.", "RATE_LIMITED",
                       headers=exc.headers)


def _message(row) -> Optional[dict]:
    return history_service.message_json(row) if row is not None else None


@router.get("/status")
def status(principal: Principal = Depends(agent_principal)):
    current = model_factory.status()
    body = {"enabled": bool(current["enabled"])}
    if current["enabled"]:
        caps = registry.capabilities_for("leave", principal)
        body.update(modules=["leave"], max_message_chars=settings.agent_message_max_chars,
                    action_ttl_minutes=settings.agent_action_ttl_minutes,
                    capabilities=[{"name": c.name, "risk": c.risk, "kind": c.kind} for c in caps])
    elif principal.is_company_admin:
        body["reason"] = current["reason"]
    return body


@router.post("/threads", status_code=201)
def create_thread(data: ThreadCreate, principal: Principal = Depends(available_principal),
                  db: Session = Depends(get_db)):
    _limit(f"agent:threads:{principal.company_id}:{principal.user_id}", THREADS_PER_MINUTE)
    thread = history_service.create_thread(db, principal, data.module, data.title)
    db.commit()
    return history_service.thread_json(thread)


@router.get("/threads")
def list_threads(limit: int = Query(30, ge=1, le=100), principal: Principal = Depends(available_principal),
                 db: Session = Depends(get_db)):
    return {"items": [history_service.thread_json(t) for t in history_service.list_threads(db, principal, limit)]}


@router.get("/threads/{thread_id}/messages")
def list_messages(thread_id: str, limit: int = Query(100, ge=1, le=200),
                  principal: Principal = Depends(available_principal), db: Session = Depends(get_db)):
    thread = history_service.get_owned_thread(db, principal, thread_id)
    if action_service.expire_stale(db, principal, thread.id):
        db.commit()
    messages, has_more = history_service.list_messages(db, principal, thread, limit)
    return {"thread": history_service.thread_json(thread), "messages": messages, "has_more": has_more}


@router.post("/threads/{thread_id}/messages")
def send_message(thread_id: str, data: ChatTurnCreate, principal: Principal = Depends(available_principal),
                 db: Session = Depends(get_db)):
    from app.agent import service

    history_service.get_owned_thread(db, principal, thread_id)  # 404 before spending the rate budget
    _limit(f"agent:turn:{principal.company_id}:{principal.user_id}", settings.agent_turns_per_minute)
    _limit(f"agent:turn-company:{principal.company_id}", settings.agent_company_turns_per_minute)
    return service.run_turn(db, principal, thread_id, data)


@router.post("/actions/{action_id}/confirm")
def confirm_action(action_id: str, data: ActionConfirm, principal: Principal = Depends(available_principal),
                   db: Session = Depends(get_db)):
    _limit(f"agent:confirm:{principal.company_id}:{principal.user_id}", CONFIRMS_PER_MINUTE)
    outcome = action_service.confirm_action(db, principal, action_id, data.confirmation_token)
    return {"action": outcome["action"], "replayed": outcome["replayed"], "message": _message(outcome["message"])}


@router.post("/actions/{action_id}/cancel")
def cancel_action(action_id: str, principal: Principal = Depends(available_principal),
                  db: Session = Depends(get_db)):
    _limit(f"agent:confirm:{principal.company_id}:{principal.user_id}", CONFIRMS_PER_MINUTE)
    outcome = action_service.cancel_action(db, principal, action_id)
    return {"action": outcome["action"], "message": _message(outcome["message"])}

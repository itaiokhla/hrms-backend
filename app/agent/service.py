"""One chat turn (docs/leave_agent.md §17).

1. The user message is saved first (``client_message_id`` makes a client retry replay the stored answer instead of
   running the model twice; a retry after a failed turn runs again).
2. Tools are built for this request from the caller's current permissions (registry); each closes over the
   ``ToolContext`` (session, principal, thread) and validates its arguments again in application code.
3. LangGraph's prebuilt ReAct agent runs with ``recursion_limit = AGENT_MAX_STEPS`` and a tool-call budget. No
   transaction is held while the provider answers: every tool call commits (prepare) or rolls back (read) at once.
4. Only the final assistant text and the ids of actions prepared in this turn are stored. Provider errors become
   safe codes; a failed turn withdraws the proposals it prepared.

LangChain / LangGraph are imported inside ``_invoke`` and ``_structured_tool`` only.
"""

import json
import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent import action_service, history_service, model_factory, prompts, registry
from app.agent.base import Capability, ToolContext
from app.config import settings
from app.core import clock
from app.core.errors import AppError, conflict
from app.core.principal import Principal
from app.models.agent import AgentMessage, AgentThread, new_id
from app.models.base import utcnow as wall_clock
from app.models.company import Company
from app.models.employee import Employee
from app.schemas.agent import ChatTurnCreate

logger = logging.getLogger("hrms.agent")

TOOL_OUTPUT_MAX_CHARS = 6000
REPLY_MAX_CHARS = 6000
MAX_TOOL_CALLS_PER_TURN = 12
NO_ANSWER = "I could not produce an answer. Please try again or rephrase your request."
REVIEW_CARD = "Please review the proposal below and confirm it if it is right."


# ----------------------------------------------------------------------------------------------- tools
def _clip(text: str) -> str:
    if len(text) <= TOOL_OUTPUT_MAX_CHARS:
        return text
    return text[: TOOL_OUTPUT_MAX_CHARS - 40] + ' ... [output truncated]"}'


def run_tool(ctx: ToolContext, cap: Capability, raw: Optional[Dict[str, Any]]) -> str:
    """Execute one capability for the model: call-time permission check, strict re-validation, one short
    transaction, and a JSON result with safe error codes (never a stack trace)."""
    with ctx.lock:
        ctx.calls.append(cap.name)
        if len(ctx.calls) > MAX_TOOL_CALLS_PER_TURN:
            out: Dict[str, Any] = {"error": "TOO_MANY_TOOL_CALLS", "message": "Stop calling tools and answer the user."}
        elif not registry.permitted(cap, ctx.principal):
            out = {"error": "NOT_ALLOWED", "message": "This tool is not available to the user."}
        else:
            try:
                args = cap.args_schema.model_validate(raw or {})
            except ValidationError as exc:
                out = {"error": "INVALID_ARGUMENTS",
                       "fields": sorted({".".join(str(p) for p in e.get("loc", ())) or "body" for e in exc.errors()})}
            else:
                try:
                    out = cap.handler(ctx, args)
                    if cap.kind == "read":
                        ctx.db.rollback()
                    else:
                        ctx.db.commit()
                except AppError as exc:
                    ctx.db.rollback()
                    out = {"error": exc.code or "REFUSED", "message": str(exc.detail)}
                    issues = (exc.extra or {}).get("issues")
                    if issues:
                        out["issues"] = [{"code": i.get("code"), "message": i.get("message")} for i in issues][:10]
                except Exception as exc:  # noqa: BLE001 - the model gets a safe code, the log a class name
                    ctx.db.rollback()
                    logger.warning("agent tool %s failed: %s", cap.name, type(exc).__name__)
                    out = {"error": "TOOL_FAILED", "message": "The tool failed; nothing was changed."}
        return _clip(json.dumps(out, default=str, ensure_ascii=False))


def _structured_tool(ctx: ToolContext, cap: Capability):
    from langchain_core.tools import StructuredTool

    def call(**kwargs):
        return run_tool(ctx, cap, kwargs)

    return StructuredTool.from_function(func=call, name=cap.name, description=cap.description,
                                        args_schema=cap.args_schema, infer_schema=False)


def build_tools(ctx: ToolContext, module: str) -> list:
    """LangChain tools for this request only (never cached: permissions and principal are request-specific)."""
    return [_structured_tool(ctx, cap) for cap in registry.capabilities_for(module, ctx.principal)]


# ----------------------------------------------------------------------------------------------- model call
def _final_text(messages: List[Any]) -> str:
    from langchain_core.messages import AIMessage

    for message in reversed(messages):
        if isinstance(message, AIMessage):
            content = message.content
            if isinstance(content, list):
                content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
            return str(content or "").strip()[:REPLY_MAX_CHARS]
    return ""


def _invoke(model, ctx: ToolContext, module: str, system_prompt: str, history: List[Tuple[str, str]],
            content: str) -> Tuple[str, int]:
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.prebuilt import create_react_agent

    tools = build_tools(ctx, module)
    agent = create_react_agent(model, tools, prompt=system_prompt)
    messages = [HumanMessage(content=text) if role == "user" else AIMessage(content=text) for role, text in history]
    messages.append(HumanMessage(content=content))
    result = agent.invoke({"messages": messages}, config={"recursion_limit": settings.agent_max_steps})
    produced = list(result.get("messages") or [])
    return _final_text(produced[len(messages):] or produced), max(len(produced) - len(messages), 0)


def safe_error(exc: BaseException) -> AppError:
    """Provider and framework failures as stable codes; never the provider's message or a stack trace."""
    if isinstance(exc, AppError):
        return exc
    if isinstance(exc, model_factory.AgentUnavailable):
        return AppError(503, "The assistant is not available right now.", "AGENT_DISABLED")
    names = {cls.__name__ for cls in type(exc).__mro__}
    if "GraphRecursionError" in names:
        return AppError(422, "That request needed too many steps. Try a simpler request.", "AGENT_STEP_LIMIT")
    if isinstance(exc, TimeoutError) or any("Timeout" in name for name in names):
        return AppError(504, "The assistant took too long to answer. Nothing was changed; please try again.",
                        "AGENT_TIMEOUT")
    if any("RateLimit" in name for name in names):
        return AppError(503, "The assistant is busy. Please try again in a moment.", "AGENT_BUSY")
    logger.warning("agent turn failed: %s", type(exc).__name__)
    return AppError(502, "The assistant could not answer right now. Nothing was changed; please try again.",
                    "AGENT_UNAVAILABLE")


# ----------------------------------------------------------------------------------------------- turn
def _today_and_zone(db: Session, principal: Principal):
    company = db.get(Company, principal.company_id)
    zone = (company.timezone if company is not None else None) or "UTC"
    employee = db.get(Employee, principal.employee_id) if principal.employee_id else None
    if employee is not None:
        from app.services import leave_requests

        return leave_requests.local_today(db, employee), zone
    return clock.company_today(db, principal.company_id), zone


def _stale_after() -> timedelta:
    per_call = settings.ai_timeout_seconds * (settings.ai_max_retries + 1)
    return timedelta(seconds=per_call * settings.agent_max_steps + 30)


def _claim(db: Session, principal: Principal, thread: AgentThread, content: str, client_message_id: str
           ) -> Tuple[AgentMessage, Optional[AgentMessage]]:
    """The turn's user message (new, or the retried one) and its stored reply when the turn already finished."""
    now = clock.utcnow()
    existing = history_service.find_client_message(db, principal, client_message_id)
    if existing is None:
        message = AgentMessage(id=new_id(), company_id=principal.company_id, thread_id=thread.id,
                               user_id=principal.user_id, role="user", content=content,
                               client_message_id=client_message_id,
                               metadata_json={"turn": "running", "started_at": now.isoformat()})
        db.add(message)
        try:
            db.commit()
            return message, None
        except IntegrityError:  # the same client id raced in from a second request
            db.rollback()
            existing = history_service.find_client_message(db, principal, client_message_id)
            if existing is None:
                raise conflict("This message could not be saved. Try again.", "CLIENT_MESSAGE_CONFLICT")
    if existing.thread_id != thread.id or existing.role != "user" or existing.content != content:
        raise conflict("This message id was already used for a different message.", "CLIENT_MESSAGE_ID_REUSED")
    reply = history_service.reply_to(db, principal, existing.id)
    if reply is not None:
        return existing, reply
    locked = (db.query(AgentMessage)
              .filter(AgentMessage.company_id == principal.company_id, AgentMessage.user_id == principal.user_id,
                      AgentMessage.id == existing.id).with_for_update().one())
    meta = dict(locked.metadata_json or {})
    started = meta.get("started_at")
    if meta.get("turn") == "running" and started:
        from datetime import datetime

        try:
            running = now - datetime.fromisoformat(started) < _stale_after()
        except (TypeError, ValueError):
            running = False
        if running:
            db.rollback()
            raise conflict("This message is still being answered.", "TURN_IN_PROGRESS")
    meta.update(turn="running", started_at=now.isoformat())
    meta.pop("error", None)
    locked.metadata_json = meta
    db.commit()
    return locked, None


def _set_turn(db: Session, principal: Principal, message_id: str, state: str, error: Optional[str] = None) -> None:
    row = (db.query(AgentMessage)
           .filter(AgentMessage.company_id == principal.company_id, AgentMessage.user_id == principal.user_id,
                   AgentMessage.id == message_id).one())
    meta = dict(row.metadata_json or {})
    meta["turn"] = state
    if error:
        meta["error"] = error
    else:
        meta.pop("error", None)
    row.metadata_json = meta


def _response(db: Session, principal: Principal, thread: AgentThread, user_message: AgentMessage,
              reply: AgentMessage, replayed: bool) -> Dict[str, Any]:
    blocks = history_service.blocks_for(db, principal, [reply]).get(reply.id, [])
    return {"thread_id": thread.id, "user_message": history_service.message_json(user_message),
            "message": history_service.message_json(reply, blocks), "blocks": blocks, "replayed": replayed}


def run_turn(db: Session, principal: Principal, thread_id: str, data: ChatTurnCreate) -> Dict[str, Any]:
    content = data.content.strip()
    if not content:
        raise AppError(422, "Type a message first.", "MESSAGE_EMPTY")
    if len(content) > settings.agent_message_max_chars:
        raise AppError(422, f"Messages are limited to {settings.agent_message_max_chars} characters.",
                       "MESSAGE_TOO_LONG")
    thread = history_service.get_owned_thread(db, principal, thread_id)
    if thread.status != "active":
        raise conflict("This conversation is archived.", "THREAD_ARCHIVED")
    module = thread.module
    user_message, reply = _claim(db, principal, thread, content, data.client_message_id)
    if reply is not None:
        return _response(db, principal, thread, user_message, reply, replayed=True)

    message_id = user_message.id
    today, zone = _today_and_zone(db, principal)
    ctx = ToolContext(db=db, principal=principal, thread_id=thread.id, message_id=message_id, today=today,
                      timezone=zone)
    history = history_service.recent_for_model(db, principal, thread, message_id, settings.agent_history_window)
    tool_names = [cap.name for cap in registry.capabilities_for(module, principal)]
    system_prompt = prompts.build_system_prompt(module, today, zone, tool_names)
    db.commit()  # nothing stays open while the provider answers
    try:
        model = model_factory.get_agent_model()
        text, steps = _invoke(model, ctx, module, system_prompt, history, content)
    except Exception as exc:  # noqa: BLE001 - mapped to a safe code below
        error = safe_error(exc)
        db.rollback()
        action_service.cancel_turn_actions(db, principal, message_id)
        _set_turn(db, principal, message_id, "failed", error.code)
        db.commit()
        raise error

    actions = history_service.turn_actions(db, principal, message_id)
    action_ids = [a.id for a in actions]
    reply = AgentMessage(id=new_id(), company_id=principal.company_id, thread_id=thread.id,
                         user_id=principal.user_id, role="assistant",
                         content=text or (REVIEW_CARD if action_ids else NO_ANSWER), reply_to_id=message_id,
                         metadata_json={"action_ids": action_ids, "tools": ctx.calls[:MAX_TOOL_CALLS_PER_TURN + 1],
                                        "model": model_factory.model_label(), "steps": steps})
    db.add(reply)
    _set_turn(db, principal, message_id, "done")
    thread = history_service.get_owned_thread(db, principal, thread.id)
    thread.updated_at = wall_clock()
    if not thread.title:
        thread.title = content[:60] + ("…" if len(content) > 60 else "")
    db.commit()
    user_message = db.get(AgentMessage, message_id)
    return _response(db, principal, thread, user_message, reply, replayed=False)

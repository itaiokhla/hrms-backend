"""Prepared actions: prepare, sign, confirm exactly once, cancel, expire (docs/leave_agent.md §13).

State machine::

    prepared --confirm--> executing --success--> completed
        |                        +----error----> failed
        +--cancel-------------------------------> cancelled
        +--TTL elapsed --------------------------> expired

``executing`` only exists inside the confirm transaction: the row is locked (``SELECT ... FOR UPDATE``) from the
status check to the commit, so a second confirm waits and then replays the stored result. A failed handler rolls the
whole transaction back (no partial domain write) and the failure is recorded in a new transaction.

The confirmation token is ``base64url(claims).base64url(HMAC-SHA256)`` with a key derived from SECRET_KEY; the claims
bind the action id, company, user, expiry and the row's random nonce. It is an integrity control on top of the
ownership query (company + user + id), never a replacement for it. Tokens are recomputed when needed, never stored
and never logged.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
from datetime import timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.agent.base import ToolContext
from app.config import settings
from app.core import clock
from app.core.errors import AppError, forbidden, not_found
from app.core.principal import Principal
from app.models.agent import AgentAction, AgentMessage, new_id
from app.services.audit_log_service import AuditLogService

logger = logging.getLogger("hrms.agent")

TOKEN_VERSION = "v1"
ACTION_ID_LENGTH = 36


# ----------------------------------------------------------------------------------------------- tokens
def _token_key() -> bytes:
    return hashlib.sha256(b"hrms.agent.confirmation." + TOKEN_VERSION.encode() + b"|" +
                          settings.secret_key.encode("utf-8")).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_token(action: AgentAction) -> str:
    claims = {"a": action.id, "c": action.company_id, "u": action.user_id,
              "e": int(action.expires_at.timestamp()), "n": action.confirmation_nonce, "v": TOKEN_VERSION}
    body = _b64(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signature = _b64(hmac.new(_token_key(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}"


def verify_token(token: str) -> Optional[Dict[str, Any]]:
    """The claims of a token signed by this deployment, else None (expiry is checked by the caller)."""
    try:
        body, signature = token.split(".", 1)
        expected = _b64(hmac.new(_token_key(), body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(expected, signature):
            return None
        claims = json.loads(_unb64(body))
    except (ValueError, UnicodeError, TypeError):
        return None
    if not isinstance(claims, dict) or claims.get("v") != TOKEN_VERSION:
        return None
    if not all(isinstance(claims.get(k), t) for k, t in (("a", str), ("c", int), ("u", int), ("e", int), ("n", str))):
        return None
    return claims


# ----------------------------------------------------------------------------------------------- reads
def _now():
    return clock.utcnow()


def _owned(db: Session, principal: Principal, action_id: str, *, lock: bool = False) -> AgentAction:
    if not isinstance(action_id, str) or len(action_id) != ACTION_ID_LENGTH:
        raise not_found("Action")
    query = db.query(AgentAction).filter(AgentAction.id == action_id,
                                         AgentAction.company_id == principal.company_id,
                                         AgentAction.user_id == principal.user_id)
    if lock:
        query = query.with_for_update()
    action = query.one_or_none()
    if action is None:
        raise not_found("Action")
    return action


def effective_status(action: AgentAction, now=None) -> str:
    if action.status == "prepared" and action.expires_at <= (now or _now()):
        return "expired"
    return action.status


def action_block(action: AgentAction, now=None) -> Dict[str, Any]:
    """The typed ``action_required`` block the UI renders (only server fields; a token only while confirmable)."""
    status = effective_status(action, now)
    preview = action.preview_json or {}
    return {
        "type": "action_required",
        "action_id": action.id,
        "action_type": action.action_type,
        "risk": action.risk,
        "status": status,
        "summary": preview.get("summary") or action.action_type,
        "details": preview.get("details") or {},
        "expires_at": action.expires_at.isoformat(),
        "confirmation_token": issue_token(action) if status == "prepared" else None,
        "result": action.result_json if status == "completed" else None,
        "failure_code": action.failure_code if status in ("failed", "cancelled", "expired") else None,
    }


def actions_by_id(db: Session, principal: Principal, ids: List[str]) -> Dict[str, AgentAction]:
    if not ids:
        return {}
    rows = db.query(AgentAction).filter(AgentAction.company_id == principal.company_id,
                                        AgentAction.user_id == principal.user_id,
                                        AgentAction.id.in_(ids)).all()
    return {row.id: row for row in rows}


def _audit(db: Session, principal: Principal, action: AgentAction, event: str, description: str,
           **details) -> None:
    AuditLogService.log(db, principal, "Agent", event, description[:500], "agent_action", action.id,
                        details={"action_type": action.action_type, "status": action.status,
                                 "thread_id": action.thread_id, **details})


def _system_event(db: Session, principal: Principal, action: AgentAction, text: str, **meta) -> AgentMessage:
    message = AgentMessage(id=new_id(), company_id=principal.company_id, thread_id=action.thread_id,
                           user_id=principal.user_id, role="system_event", content=text[:2000],
                           metadata_json={"action_id": action.id, **meta})
    db.add(message)
    from app.models.agent import AgentThread

    thread = db.query(AgentThread).filter(AgentThread.company_id == principal.company_id,
                                          AgentThread.id == action.thread_id).one_or_none()
    if thread is not None:
        from app.models.base import utcnow as wall_clock

        thread.updated_at = wall_clock()
    return message


# ----------------------------------------------------------------------------------------------- prepare
def prepare_action(ctx: ToolContext, action_type: str, payload: Dict[str, Any], details: Dict[str, Any],
                   summary: str) -> AgentAction:
    """Save a short-lived proposal owned by the current company / user / thread. Never executes anything."""
    from app.agent import registry

    spec = registry.ACTIONS.get(action_type)
    if spec is None:
        raise AppError(400, "This action is not available.", "UNSUPPORTED_ACTION")
    principal = ctx.principal
    if not registry.permitted(spec, principal):
        raise forbidden()
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    key = hashlib.sha256("|".join((str(principal.company_id), str(principal.user_id), ctx.thread_id,
                                   ctx.message_id or "", action_type, canonical)).encode("utf-8")).hexdigest()
    db = ctx.db
    existing = db.query(AgentAction).filter(AgentAction.company_id == principal.company_id,
                                            AgentAction.idempotency_key == key).one_or_none()
    if existing is not None:  # the same proposal twice in one turn is one action
        return existing
    action = AgentAction(id=new_id(), company_id=principal.company_id, thread_id=ctx.thread_id,
                         user_id=principal.user_id, message_id=ctx.message_id, module=spec.module,
                         action_type=action_type, permission_code=spec.permission, risk=spec.risk,
                         payload_json=payload, preview_json={"summary": summary[:300], "details": details},
                         status="prepared", idempotency_key=key, confirmation_nonce=secrets.token_urlsafe(24),
                         expires_at=_now() + timedelta(minutes=settings.agent_action_ttl_minutes))
    db.add(action)
    db.flush()
    _audit(db, principal, action, "ACTION_PREPARED", f"Assistant prepared {spec.label}: {summary}"[:500])
    ctx.action_ids.append(action.id)
    return action


# ----------------------------------------------------------------------------------------------- confirm
def _subject_matches(spec, principal: Principal, action: AgentAction) -> bool:
    subject = (action.payload_json or {}).get("employee_id")
    if spec.needs_employee:
        return principal.employee_id is not None and subject == principal.employee_id
    return subject in (None, principal.employee_id)


def _record_failure(db: Session, principal: Principal, action_id: str, code: str, detail: str) -> None:
    """After a rollback: mark a still-prepared action failed, in its own transaction."""
    action = _owned(db, principal, action_id, lock=True)
    if action.status == "prepared":
        action.status = "failed"
        action.failure_code = (code or "FAILED")[:60]
        _audit(db, principal, action, "ACTION_FAILED", f"Assistant action failed: {action.failure_code}",
               failure_code=action.failure_code)
        _system_event(db, principal, action, f"Not completed: {detail}", outcome="failed",
                      failure_code=action.failure_code)
    db.commit()


def confirm_action(db: Session, principal: Principal, action_id: str, token: str) -> Dict[str, Any]:
    """Deterministic: no model call. Token, ownership, state, expiry, permission and the domain rules are checked
    again here; the handler runs at most once and its stable result is replayed on a repeat."""
    from app.agent import registry

    claims = verify_token(token)
    if (claims is None or claims["a"] != action_id or claims["c"] != principal.company_id
            or claims["u"] != principal.user_id):
        raise not_found("Action")
    action = _owned(db, principal, action_id, lock=True)
    if not hmac.compare_digest(claims["n"], action.confirmation_nonce):
        raise not_found("Action")
    if action.status == "completed":
        db.commit()
        return {"action": action_block(action), "replayed": True, "message": None}
    if action.status != "prepared":
        db.commit()
        raise AppError(409, "This action can no longer be confirmed.", "ACTION_NOT_CONFIRMABLE",
                       extra={"details": {"status": action.status}})
    now = _now()
    if action.expires_at <= now or claims["e"] <= int(now.timestamp()):
        action.status = "expired"
        action.failure_code = "EXPIRED"
        _audit(db, principal, action, "ACTION_EXPIRED", "Assistant action expired before confirmation")
        db.commit()
        raise AppError(409, "This action expired. Ask the assistant to prepare it again.", "ACTION_EXPIRED")

    spec = registry.ACTIONS.get(action.action_type)
    if spec is None or spec.permission != action.permission_code:
        action.status, action.failure_code = "failed", "UNSUPPORTED_ACTION"
        _audit(db, principal, action, "ACTION_FAILED", "Assistant action type is not allowlisted",
               failure_code="UNSUPPORTED_ACTION")
        db.commit()
        raise AppError(409, "This action is not available.", "UNSUPPORTED_ACTION")
    # Permissions come from the request's freshly built principal: a role removed after preview applies now.
    if not registry.permitted(spec, principal) or not _subject_matches(spec, principal, action):
        action.status, action.failure_code = "failed", "PERMISSION_DENIED"
        _audit(db, principal, action, "ACTION_DENIED", "Assistant action denied at confirmation",
               failure_code="PERMISSION_DENIED")
        _system_event(db, principal, action, "Not completed: you no longer have permission for this action.",
                      outcome="denied", failure_code="PERMISSION_DENIED")
        db.commit()
        raise forbidden("You no longer have permission to do this.")

    action.status = "executing"
    db.flush()
    _audit(db, principal, action, "ACTION_CONFIRMED", f"Confirmed {spec.label}")
    try:
        result = spec.handler(db, principal, dict(action.payload_json or {}))
        db.flush()
    except AppError as exc:
        db.rollback()
        _record_failure(db, principal, action_id, exc.code or "REFUSED", str(exc.detail))
        raise
    except Exception as exc:  # noqa: BLE001 - never leak internals; nothing was committed
        db.rollback()
        logger.error("agent action %s (%s) failed: %s", action_id, action.action_type, type(exc).__name__)
        _record_failure(db, principal, action_id, "INTERNAL_ERROR", "the request could not be completed.")
        raise AppError(500, "The action could not be completed. Nothing was changed.", "AGENT_ACTION_FAILED")

    action.status = "completed"
    action.completed_at = _now()
    action.result_json = result
    message = _system_event(db, principal, action, str(result.get("message") or "Done."), outcome="completed")
    _audit(db, principal, action, "ACTION_COMPLETED", f"Completed {spec.label}",
           result={k: v for k, v in result.items() if k != "message"})
    db.commit()
    return {"action": action_block(action), "replayed": False, "message": message}


# ----------------------------------------------------------------------------------------------- cancel / expire
def cancel_action(db: Session, principal: Principal, action_id: str) -> Dict[str, Any]:
    action = _owned(db, principal, action_id, lock=True)
    if action.status == "cancelled":
        db.commit()
        return {"action": action_block(action), "message": None}
    if effective_status(action) == "expired":
        action.status, action.failure_code = "expired", "EXPIRED"
        _audit(db, principal, action, "ACTION_EXPIRED", "Assistant action expired")
        db.commit()
        return {"action": action_block(action), "message": None}
    if action.status != "prepared":
        db.commit()
        raise AppError(409, "This action can no longer be cancelled.", "ACTION_NOT_CANCELLABLE",
                       extra={"details": {"status": action.status}})
    action.status, action.failure_code = "cancelled", "CANCELLED_BY_USER"
    message = _system_event(db, principal, action, "Cancelled. Nothing was changed.", outcome="cancelled")
    _audit(db, principal, action, "ACTION_CANCELLED", "Assistant action cancelled by the user")
    db.commit()
    return {"action": action_block(action), "message": message}


def expire_stale(db: Session, principal: Principal, thread_id: Optional[str] = None) -> int:
    """Mark the caller's prepared actions past their expiry as expired (audited). The caller commits."""
    query = db.query(AgentAction).filter(AgentAction.company_id == principal.company_id,
                                         AgentAction.user_id == principal.user_id,
                                         AgentAction.status == "prepared", AgentAction.expires_at <= _now())
    if thread_id is not None:
        query = query.filter(AgentAction.thread_id == thread_id)
    rows = query.with_for_update(skip_locked=True).limit(200).all()
    for action in rows:
        action.status, action.failure_code = "expired", "EXPIRED"
        _audit(db, principal, action, "ACTION_EXPIRED", "Assistant action expired")
    return len(rows)


def cancel_turn_actions(db: Session, principal: Principal, message_id: str) -> None:
    """A turn that failed leaves no confirmable proposal behind."""
    for action in db.query(AgentAction).filter(AgentAction.company_id == principal.company_id,
                                               AgentAction.user_id == principal.user_id,
                                               AgentAction.message_id == message_id,
                                               AgentAction.status == "prepared").with_for_update().all():
        action.status, action.failure_code = "cancelled", "TURN_FAILED"
        _audit(db, principal, action, "ACTION_CANCELLED", "Assistant turn failed; proposal withdrawn")

"""Types shared by the registry, the module adapters and the services (no LangChain imports here)."""

import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.principal import Principal

RISKS = ("R0", "R1", "R2", "R3", "R4")


@dataclass
class ToolContext:
    """Request-scoped context captured by every tool closure. The model never sees or chooses any of it."""

    db: Session
    principal: Principal
    thread_id: str
    message_id: Optional[str]
    today: date
    timezone: str = "UTC"
    # ToolNode may run several calls of one model step in worker threads: one session, one call at a time.
    lock: threading.Lock = field(default_factory=threading.Lock)
    calls: List[str] = field(default_factory=list)
    action_ids: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class Capability:
    """One model-facing tool. ``permissions`` is any-of; R1 reads run at once, R2/R3 tools only prepare an action."""

    name: str
    module: str
    risk: str
    kind: str  # read | prepared_write
    permissions: Tuple[str, ...]
    needs_employee: bool
    args_schema: Type[BaseModel]
    description: str
    handler: Callable[[ToolContext, BaseModel], Dict[str, Any]]
    action_type: Optional[str] = None


@dataclass(frozen=True)
class ActionSpec:
    """An allowlisted, deterministic write executed only by the confirm endpoint (never by the model)."""

    action_type: str
    module: str
    permission: str
    risk: str
    needs_employee: bool
    handler: Callable[[Session, Principal, Dict[str, Any]], Dict[str, Any]]
    label: str

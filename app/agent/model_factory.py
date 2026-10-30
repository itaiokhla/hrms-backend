"""One .env-driven chat model for the assistant (docs/leave_agent.md §9).

``AI_MODEL=provider:model-name`` picks the provider (groq | openai | anthropic) and model; switching needs only the
environment and a restart. The key is ``AI_API_KEY`` or the provider's own variable (app/config.py) and goes only to
the provider client: it is never logged, never put in a message and never returned by the API.

LangChain is imported inside ``get_agent_model``; ``status()`` only checks that the packages are installed, so the
application imports and runs on an environment without them (the agent then reports itself unavailable).

Tests inject a deterministic chat model with ``set_model_override`` (no network).
"""

import importlib.util
import threading
from functools import lru_cache
from typing import Any, Callable, Dict, Optional

from app.config import AgentConfigError, parse_ai_model, settings

SUPPORTED_PROVIDERS = ("groq", "openai", "anthropic")
PROVIDER_PACKAGES = {"groq": "langchain_groq", "openai": "langchain_openai", "anthropic": "langchain_anthropic"}
RUNTIME_PACKAGES = ("langchain", "langchain_core", "langgraph")

_override: Optional[Callable[[], Any]] = None
_cache: Dict[tuple, Any] = {}
_lock = threading.Lock()


class AgentUnavailable(RuntimeError):
    """The assistant cannot run here: disabled, runtime missing or misconfigured (``reason`` is a safe code)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@lru_cache(maxsize=None)
def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def runtime_available() -> bool:
    return all(_installed(name) for name in RUNTIME_PACKAGES)


def status() -> Dict[str, Any]:
    """``{"enabled": bool, "reason": code}`` (+ provider / model internally). Never raises, never shows a key."""
    if not settings.agent_enabled:
        return {"enabled": False, "reason": "DISABLED"}
    if not runtime_available():
        return {"enabled": False, "reason": "RUNTIME_MISSING"}
    try:
        provider, model_name = parse_ai_model(settings.ai_model)
    except AgentConfigError:
        return {"enabled": False, "reason": "MISCONFIGURED"}
    if _override is None:
        if not _installed(PROVIDER_PACKAGES[provider]):
            return {"enabled": False, "reason": "RUNTIME_MISSING"}
        if settings.ai_key_for(provider) is None:
            return {"enabled": False, "reason": "MISCONFIGURED"}
    return {"enabled": True, "reason": None, "provider": provider, "model": model_name}


def is_available() -> bool:
    return bool(status()["enabled"])


def model_label() -> str:
    """``provider:model`` for internal metadata and logs (no credentials)."""
    current = status()
    return f"{current.get('provider')}:{current.get('model')}" if current["enabled"] else "unavailable"


def set_model_override(factory: Optional[Callable[[], Any]]) -> None:
    """Tests only: every turn uses ``factory()`` instead of the configured provider (``None`` restores it)."""
    global _override
    _override = factory


def reset_cache() -> None:
    with _lock:
        _cache.clear()


def get_agent_model():
    """The configured chat model (cached; stateless). Raises ``AgentUnavailable`` when the agent cannot run."""
    current = status()
    if not current["enabled"]:
        raise AgentUnavailable(current["reason"])
    if _override is not None:
        return _override()
    provider, model_name = current["provider"], current["model"]
    if provider not in SUPPORTED_PROVIDERS:  # parse_ai_model already refuses these; defence in depth
        raise AgentUnavailable("MISCONFIGURED")
    cache_key = (provider, model_name, float(settings.ai_timeout_seconds), int(settings.ai_max_retries))
    with _lock:
        model = _cache.get(cache_key)
        if model is None:
            from langchain.chat_models import init_chat_model

            key = settings.ai_key_for(provider)
            if key is None:
                raise AgentUnavailable("MISCONFIGURED")
            model = init_chat_model(
                model=model_name,
                model_provider=provider,
                api_key=key.get_secret_value(),
                timeout=float(settings.ai_timeout_seconds),
                max_retries=int(settings.ai_max_retries),
                temperature=0,
            )
            _cache.clear()
            _cache[cache_key] = model
    return model

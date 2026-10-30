"""Helpers of the assistant tests (docs/leave_agent.md §26): deterministic models, tenant sessions, direct tool calls.

No test talks to a provider. ``enable_agent`` switches the assistant on for one test and injects a model through
``model_factory.set_model_override``; tests that never run a chat turn inject a model that fails if it is used, and
pretend the LangChain runtime is installed (the deterministic confirm / cancel / history paths do not import it), so
they also run on the shared environment without LangChain.
"""

import uuid
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional

import pytest
from sqlalchemy import text

A = "/api/v1/agent"
L = "/api/v1/leaves"


def sql(statement: str, **params):
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        return conn.execute(text(statement), params).fetchall()


def ok(response, status: int = 200):
    assert response.status_code == status, response.text
    return response.json()


def cmid() -> str:
    return f"m-{uuid.uuid4().hex}"


class _NoModel:
    def __getattr__(self, name):  # any use of the model in a no-LangChain test is a bug in the test
        raise AssertionError("this test must not call the model")


@pytest.fixture
def enable_agent(monkeypatch):
    """The assistant on (groq model name only; no key is read, no network). Returns a setter for the model factory."""
    from app.agent import model_factory
    from app.config import settings

    monkeypatch.setattr(settings, "agent_enabled", True)
    monkeypatch.setattr(settings, "ai_model", "groq:test-model")
    monkeypatch.setattr(model_factory, "runtime_available", lambda: True)
    model_factory.set_model_override(lambda: _NoModel())

    def use(factory: Callable[[], Any]) -> None:
        model_factory.set_model_override(factory)

    yield use
    model_factory.set_model_override(None)


@contextmanager
def tenant_session(company_id: int) -> Iterator[Any]:
    from app.database.db import SessionLocal, bind_tenant

    db = SessionLocal()
    bind_tenant(db, company_id)
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def principal_for(db, user_id: int):
    from app.core.principal import build_principal
    from app.models.company import Company
    from app.models.user import User

    user = db.get(User, user_id)
    return build_principal(db, user, None, db.get(Company, user.company_id))


def user_id_of(client) -> int:
    return client.me()["user"]["id"]


def new_thread(client) -> str:
    return ok(client.post(f"{A}/threads", json={"module": "leave"}), 201)["id"]


def call_tool(client, thread_id: str, name: str, args: Optional[Dict[str, Any]] = None,
              message_id: Optional[str] = None) -> Dict[str, Any]:
    """Run one capability exactly as the model's tool call would (permission check, validation, transaction)."""
    import json

    from app.agent import registry, service
    from app.agent.base import ToolContext
    from app.core import clock

    me = client.me()
    with tenant_session(me["company"]["id"]) as db:
        principal = principal_for(db, me["user"]["id"])
        ctx = ToolContext(db=db, principal=principal, thread_id=thread_id, message_id=message_id,
                          today=clock.company_today(db, principal.company_id), timezone="Asia/Kolkata")
        cap = registry.CAPABILITIES[name]
        return json.loads(service.run_tool(ctx, cap, args or {}))


def action_row(action_id: str) -> Dict[str, Any]:
    rows = sql("SELECT id, company_id, user_id, status, failure_code, action_type, result_json FROM agent_actions "
               "WHERE id = :i", i=action_id)
    assert rows, action_id
    return dict(rows[0]._mapping)


def token_for(action_id: str) -> str:
    from app.agent.action_service import issue_token
    from app.models.agent import AgentAction

    with tenant_session(action_row(action_id)["company_id"]) as db:
        return issue_token(db.get(AgentAction, action_id))


def leave_count(company_id: int, employee_id: Optional[int] = None) -> int:
    query = "SELECT count(*) FROM leaves WHERE company_id = :c"
    params: Dict[str, Any] = {"c": company_id}
    if employee_id is not None:
        query += " AND employee_id = :e"
        params["e"] = employee_id
    return sql(query, **params)[0][0]


def confirm(client, block_or_id, token: Optional[str] = None):
    if isinstance(block_or_id, dict):
        action_id, token = block_or_id["action_id"], token or block_or_id["confirmation_token"]
    else:
        action_id = block_or_id
    return client.post(f"{A}/actions/{action_id}/confirm", json={"confirmation_token": token})


# ----------------------------------------------------------------------------------------------- scripted models
def tool_call(name: str, **args):
    from langchain_core.messages import AIMessage

    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{uuid.uuid4().hex[:12]}"}])


def tool_calls(*calls):
    """One model step asking for several tools at once: ``tool_calls(("name", {args}), ...)``."""
    from langchain_core.messages import AIMessage

    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{uuid.uuid4().hex[:12]}"}
                                             for name, args in calls])


def say(content: str):
    from langchain_core.messages import AIMessage

    return AIMessage(content=content)


class Script:
    """One scripted model per chat turn: ``turns`` is a list of message lists (or exceptions to raise on a step).
    Records the tool names each turn bound, and the messages each model call received."""

    def __init__(self, *turns: List[Any]):
        self.turns = list(turns)
        self.bound: List[List[str]] = []
        self.seen: List[List[Any]] = []
        self.models = 0

    def __call__(self):
        from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
        from langchain_core.outputs import ChatGeneration, ChatResult

        script = self
        steps = list(self.turns.pop(0)) if self.turns else [say("(no more scripted turns)")]
        self.models += 1

        class Scripted(FakeMessagesListChatModel):
            def bind_tools(self, tools, **kwargs):
                script.bound.append(sorted(getattr(t, "name", str(t)) for t in tools))
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                script.seen.append(list(messages))
                step = steps.pop(0) if steps else say("Done.")
                if isinstance(step, BaseException):
                    raise step
                return ChatResult(generations=[ChatGeneration(message=step)])

        return Scripted(responses=[say("unused")])


class APITimeoutError(Exception):
    """Named like the providers' timeout errors (openai / groq / anthropic ``APITimeoutError``)."""

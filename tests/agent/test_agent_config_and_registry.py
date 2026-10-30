"""Assistant configuration, model factory, capability catalogue and tool schemas (docs/leave_agent.md §8, §9, §11,
§15, §26 unit tests). Runs without LangChain except the provider-construction test (skipped there)."""

import dataclasses

import pytest
from pydantic import SecretStr, ValidationError

from tests.agent.agent_helpers import principal_for, tenant_session

LEAVE_SELF_TOOLS = {"get_leave_types", "get_my_leave_balance", "get_my_leave_requests", "prepare_leave_application",
                    "prepare_leave_cancellation"}
APPROVER_TOOLS = {"get_my_pending_approvals", "prepare_leave_approval", "prepare_leave_rejection"}


def _settings(**values):
    from app.config import Settings

    return Settings(_env_file=None, **values)


# ----------------------------------------------------------------------------------------------- settings
@pytest.mark.parametrize("model", ["", "groq", "llama-3.3-70b", "cohere:command-r", "mistral:large", "groq:",
                                   "openai: ", "anthropic:two words"])
def test_an_enabled_agent_refuses_a_malformed_or_unsupported_model(model):
    from app.config import AgentConfigError

    with pytest.raises(AgentConfigError):
        _settings(agent_enabled=True, ai_model=model, ai_api_key="k" * 20)


def test_an_enabled_agent_needs_a_key_for_its_provider_and_the_error_never_shows_a_key():
    from app.config import AgentConfigError

    secret = "sk-should-never-appear-123"
    with pytest.raises(AgentConfigError) as raised:
        _settings(agent_enabled=True, ai_model="openai:gpt-4o-mini", groq_api_key=secret)
    assert "OPENAI_API_KEY" in str(raised.value) and secret not in str(raised.value)
    assert secret not in repr(_settings(groq_api_key=secret))


def test_key_resolution_prefers_ai_api_key_then_the_providers_own_variable():
    both = _settings(agent_enabled=True, ai_model="groq:llama-3.3-70b-versatile", ai_api_key="generic-key",
                     groq_api_key="groq-key")
    assert both.ai_key_for("groq").get_secret_value() == "generic-key"
    own = _settings(agent_enabled=True, ai_model="Groq:llama-3.3-70b-versatile", groq_api_key="groq-key")
    assert own.ai_provider_model == ("groq", "llama-3.3-70b-versatile")
    assert own.ai_key_for("groq").get_secret_value() == "groq-key" and own.ai_key_for("openai") is None
    anthropic = _settings(agent_enabled=True, ai_model="anthropic:claude-model", anthropic_api_key="a-key")
    assert anthropic.ai_provider_model[0] == "anthropic"


def test_a_disabled_agent_starts_without_any_ai_configuration():
    disabled = _settings(agent_enabled=False, ai_model="not a model")
    assert disabled.agent_enabled is False and disabled.ai_api_key is None


def test_status_reports_why_the_assistant_is_unavailable(monkeypatch):
    from app.agent import model_factory
    from app.config import settings

    monkeypatch.setattr(settings, "agent_enabled", False)
    assert model_factory.status() == {"enabled": False, "reason": "DISABLED"}
    with pytest.raises(model_factory.AgentUnavailable):
        model_factory.get_agent_model()
    monkeypatch.setattr(settings, "agent_enabled", True)
    monkeypatch.setattr(model_factory, "runtime_available", lambda: False)
    assert model_factory.status()["reason"] == "RUNTIME_MISSING"
    monkeypatch.setattr(model_factory, "runtime_available", lambda: True)
    monkeypatch.setattr(settings, "ai_model", "cohere:command")
    assert model_factory.status()["reason"] == "MISCONFIGURED"
    monkeypatch.setattr(settings, "ai_model", "groq:llama-3.3-70b-versatile")
    for field in ("ai_api_key", "groq_api_key"):
        monkeypatch.setattr(settings, field, None)
    assert model_factory.status()["reason"] in ("MISCONFIGURED", "RUNTIME_MISSING")  # no key / no provider package


@pytest.mark.parametrize("provider,package,cls", [("groq", "langchain_groq", "ChatGroq"),
                                                  ("openai", "langchain_openai", "ChatOpenAI"),
                                                  ("anthropic", "langchain_anthropic", "ChatAnthropic")])
def test_the_provider_is_chosen_by_configuration_alone(monkeypatch, provider, package, cls):
    """No code change switches providers: AI_MODEL + the key build that provider's client (construction only,
    no network), with the configured timeout and retries and temperature 0."""
    pytest.importorskip(package)
    from app.agent import model_factory
    from app.config import settings

    monkeypatch.setattr(settings, "agent_enabled", True)
    monkeypatch.setattr(settings, "ai_model", f"{provider}:some-model")
    monkeypatch.setattr(settings, "ai_api_key", SecretStr("dummy-key-for-construction-only"))
    monkeypatch.setattr(settings, "ai_timeout_seconds", 12.0)
    monkeypatch.setattr(settings, "ai_max_retries", 2)
    model_factory.set_model_override(None)
    model_factory.reset_cache()
    try:
        model = model_factory.get_agent_model()
        assert type(model).__name__ == cls and model.max_retries == 2 and model.temperature == 0
        assert model_factory.get_agent_model() is model  # cached, stateless
        assert model_factory.model_label() == f"{provider}:some-model"
    finally:
        model_factory.reset_cache()


# ----------------------------------------------------------------------------------------------- tool schemas
def test_no_tool_schema_carries_identity_tenant_role_or_secret_fields():
    from app.agent import registry
    from app.schemas.agent import FORBIDDEN_TOOL_FIELDS

    assert registry.CAPABILITIES  # the catalogue is not empty
    for cap in registry.CAPABILITIES.values():
        schema = cap.args_schema.model_json_schema()
        assert set(schema.get("properties", {})) & FORBIDDEN_TOOL_FIELDS == set(), cap.name
        assert schema.get("additionalProperties") is False, cap.name
        for field in ("employee_id", "company_id", "user_id", "role", "is_admin"):
            with pytest.raises(ValidationError):
                cap.args_schema.model_validate({field: 1})


@pytest.mark.parametrize("args", [
    {"leave_type": "casual", "start_date": "2026-02-30", "end_date": "2026-03-01"},
    {"leave_type": "casual", "start_date": "02/11/2026", "end_date": "2026-11-02"},
    {"leave_type": "casual", "start_date": 1793577600, "end_date": "2026-11-02"},
    {"leave_type": "casual", "start_date": "2026-11-02", "end_date": "2026-11-02", "employee_id": 7},
    {"leave_type": "casual", "start_date": "2026-11-02", "end_date": "2026-11-02", "start_portion": "MORNING"},
    {"leave_type": "<script>", "start_date": "2026-11-02", "end_date": "2026-11-02"},
    {"leave_type": "casual", "start_date": "2026-11-02"},
])
def test_apply_arguments_are_strict(args):
    from app.schemas.agent import LeaveApplyArgs

    with pytest.raises(ValidationError):
        LeaveApplyArgs.model_validate(args)


def test_the_catalogue_is_a_reviewed_allowlist_with_risk_tiers():
    from app.agent import registry

    names = {c["name"]: c for c in registry.catalogue()}
    assert set(names) == LEAVE_SELF_TOOLS | APPROVER_TOOLS
    assert {n for n, c in names.items() if c["risk"] == "R1"} == {
        "get_leave_types", "get_my_leave_balance", "get_my_leave_requests", "get_my_pending_approvals"}
    assert {n for n, c in names.items() if c["risk"] == "R3"} == {"prepare_leave_approval", "prepare_leave_rejection"}
    assert set(registry.ACTIONS) == {"leave.apply", "leave.withdraw", "leave.cancel", "leave.approve", "leave.reject"}
    assert not any(word in n for n in names for word in ("sql", "http", "url", "query", "python", "admin"))


# ----------------------------------------------------------------------------------------------- tools per role
def test_only_permitted_tools_are_built_for_each_role(acme):
    from app.agent import registry
    from app.core.principal import DelegationContext

    expected = {"worker": LEAVE_SELF_TOOLS, "peer": LEAVE_SELF_TOOLS, "boss": LEAVE_SELF_TOOLS | APPROVER_TOOLS,
                "hr": LEAVE_SELF_TOOLS | APPROVER_TOOLS}
    with tenant_session(acme["cid"]) as db:
        for key, tools in expected.items():
            principal = principal_for(db, acme[key].me()["user"]["id"])
            assert {c.name for c in registry.capabilities_for("leave", principal)} == tools, key
        admin = principal_for(db, acme["admin"].me()["user"]["id"])
        admin_tools = {c.name for c in registry.capabilities_for("leave", admin)}
        if admin.employee_id is None:  # no employee record: nothing about "my" leave
            assert admin_tools == {"get_leave_types"} | APPROVER_TOOLS
        worker = principal_for(db, acme["worker"].me()["user"]["id"])
        delegate = dataclasses.replace(worker, delegation=DelegationContext(1, 1, 1, "op@partner.io", "s"))
        assert registry.capabilities_for("leave", delegate) == []
        no_leave = dataclasses.replace(worker, grants={})
        assert registry.capabilities_for("leave", no_leave) == []
        assert registry.capabilities_for("payroll", worker) == []  # unknown module: nothing

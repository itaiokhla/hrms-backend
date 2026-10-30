"""Secure HRMS assistant (docs/leave_agent.md).

The model understands language; the backend owns identity, authorization, validation, data and execution. This
package never imports LangChain / LangGraph at module import: ``service`` and ``model_factory`` import them inside
functions, so the application starts (and the agent routes answer 503 ``AGENT_DISABLED``) where the runtime is not
installed or ``AGENT_ENABLED`` is false.

* ``model_factory`` — one .env-driven chat model (``AI_MODEL=provider:model``; groq / openai / anthropic).
* ``registry`` — the reviewed capability catalogue: tools (R1 reads, R2/R3 prepared writes) and action handlers.
* ``modules/leave_tools`` / ``modules/leave_actions`` — thin adapters over the leave services.
* ``action_service`` — prepared actions, signed confirmation tokens, deterministic confirm / cancel / expiry.
* ``history_service`` — user-owned threads and messages.
* ``service`` — one chat turn (LangGraph ReAct loop with bounded steps).
"""

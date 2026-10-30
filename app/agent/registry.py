"""The reviewed capability catalogue (docs/leave_agent.md §15, §20, §28).

Default deny: only the capabilities and action handlers listed here exist. There is no discovery of routes or
services, no generic SQL / HTTP / "call any endpoint" tool. A tool is offered when the caller holds one of its
permissions now (and has an employee record where the tool is about their own data); the same check runs again
when the tool is called and, for prepared writes, when the action is confirmed (``ActionSpec.permission``).

Risk tiers: R1 personal read (runs at once), R2 personal write and R3 approval write (prepare + explicit confirm).
R0 / R4 have no tools: informational answers need none, sensitive administration is not available through chat.

Adding a module means adding ``modules/<module>_tools.py`` and ``modules/<module>_actions.py`` and listing them in
``MODULES``; the routes, the action service and the UI blocks stay the same.
"""

from typing import Any, Dict, List, Tuple

from app.agent.base import ActionSpec, Capability
from app.agent.modules import leave_actions, leave_tools
from app.core.principal import Principal

MODULES: Dict[str, Tuple[Tuple[Capability, ...], Tuple[ActionSpec, ...]]] = {
    "leave": (leave_tools.CAPABILITIES, leave_actions.ACTIONS),
}

CAPABILITIES: Dict[str, Capability] = {cap.name: cap for caps, _ in MODULES.values() for cap in caps}
ACTIONS: Dict[str, ActionSpec] = {spec.action_type: spec for _, specs in MODULES.values() for spec in specs}

# A prepared-write tool names the action types it may create ("a|b" when it picks one of several).
for _cap in CAPABILITIES.values():
    assert _cap.risk in ("R1", "R2", "R3"), _cap.name
    assert (_cap.kind == "read") == (_cap.risk == "R1"), _cap.name
    if _cap.kind == "prepared_write":
        assert _cap.action_type and all(t in ACTIONS for t in _cap.action_type.split("|")), _cap.name


def permitted(item, principal: Principal) -> bool:
    """Current-permission check for a Capability (any of its codes) or an ActionSpec (its one code)."""
    if principal is None or principal.delegation is not None:  # partner delegates are never offered the assistant
        return False
    if item.needs_employee and principal.employee_id is None:
        return False
    codes = item.permissions if isinstance(item, Capability) else (item.permission,)
    return any(principal.has(code) for code in codes)


def capabilities_for(module: str, principal: Principal) -> List[Capability]:
    caps, _ = MODULES.get(module, ((), ()))
    return [cap for cap in caps if permitted(cap, principal)]


def catalogue() -> List[Dict[str, Any]]:
    """The reviewed catalogue as data (documentation, tests, UI badges)."""
    return [{"name": c.name, "module": c.module, "risk": c.risk, "kind": c.kind, "permissions": list(c.permissions),
             "needs_employee": c.needs_employee, "action_types": (c.action_type or "").split("|") if c.action_type
             else []} for c in CAPABILITIES.values()]

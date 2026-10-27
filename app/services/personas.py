"""A user's personas (spine §12.3, C-06, D-X-26): from permissions and Me facts only, never from role names.

Used for system saved views (X 04 §2.8) and, from X-M4, Home. Team scopes (TEAM / TEAM_TREE / ASSIGNED_TEAMS) and
``me.teams`` arrive with X-M1; until then TEAM_LEAD has no qualifying grant and is never derived.
"""

from typing import FrozenSet

from app.core.principal import Principal
from app.models.views import PERSONAS

RELATIONAL_SCOPES = frozenset({"DIRECT_REPORTS", "REPORTING_TREE", "DEPARTMENT"})
TEAM_SCOPES = frozenset({"TEAM", "TEAM_TREE", "ASSIGNED_TEAMS"})
ORG_SCOPES = frozenset({"BRANCH", "LEGAL_ENTITY", "COMPANY"})
APPROVE_CODES = ("leave:approve", "attendance:approve", "overtime:approve", "roster:approve", "task:approve")
# Decision or write codes of attendance, leave or roster (read codes alone never make a user HR, SP-AS-097).
HR_CODES = ("attendance:approve", "attendance:regularize", "attendance:review", "attendance:close_period",
            "attendance:manage_policy", "attendance:manage_devices", "attendance:manage_sites", "leave:approve",
            "roster:plan")


def _scopes(principal: Principal, code: str) -> set:
    return set(principal.scopes(code))


def personas(principal: Principal) -> FrozenSet[str]:
    found = set()
    if principal.employee_id:
        found.add("EMPLOYEE")
    if any(_scopes(principal, code) & TEAM_SCOPES for code in APPROVE_CODES):
        found.add("TEAM_LEAD")
    if any(_scopes(principal, code) & RELATIONAL_SCOPES for code in ("attendance:read", "leave:approve")):
        found.add("MANAGER")
    if any(_scopes(principal, code) & ORG_SCOPES for code in HR_CODES):
        found.add("HR")
    # Payroll read codes beyond the self-service ones every employee holds at SELF.
    if any(code.startswith("payroll:") and code.endswith((":read", ":read_masked")) and set(held) - {"SELF"}
           for code, held in principal.grants.items()):
        found.add("PAYROLL")
    if "COMPANY" in _scopes(principal, "report:read"):
        found.add("EXECUTIVE")
    return frozenset(p for p in PERSONAS if p in found)

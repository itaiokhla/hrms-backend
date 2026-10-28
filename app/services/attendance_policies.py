"""Resolution of the attendance policy types on the shared policy framework (spine §6.2, §6.2.5; JC-AS-22; WI-AS-2.4).

``resolve(db, company_id, type_code, employee_id, d)`` returns the version that applies to the employee on the
employee-local date ``d`` (highest-priority matching assignment rule, ACTIVE version on ``d``) or ``None``;
``resolve_branch`` is the branch-level resolution of a kiosk before anyone is identified (AS-CAP-09: only branch,
legal-entity and catch-all rules can match). The rules, versions and explain text are those of
``app.payroll.services.policies`` (the attendance types are registered in ``POLICY_TYPES`` with
``module = "attendance"``); attendance callers never read payroll snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Optional

from sqlalchemy.orm import Session

from app.models.org import Branch
from app.payroll import policy_types as pt
from app.payroll.services import policies

CAPTURE = "ATTENDANCE_CAPTURE_POLICY"
ATTENDANCE_TYPES = tuple(code for code, d in pt.POLICY_TYPES.items() if d.module == "attendance")


@dataclass(frozen=True)
class PolicyVersionData:
    type_code: str
    policy_id: int
    policy_name: str
    version: int
    fields: Mapping[str, Any]
    effective_from: date
    effective_to: Optional[date]
    rule_id: Optional[int] = None
    explain: tuple = field(default=(), compare=False)

    @property
    def profile(self) -> str:
        return str(self.fields.get("profile") or "STANDARD")


def _data(type_code: str, found: Mapping[str, Any]) -> Optional[PolicyVersionData]:
    if found.get("status") != "RESOLVED":
        return None
    version = found["version"]
    rule = found.get("rule") or {}
    return PolicyVersionData(
        type_code=type_code, policy_id=found["policy"]["id"], policy_name=found["policy"]["name"],
        version=version["version"], fields=version["fields"],
        effective_from=date.fromisoformat(version["effective_from"]),
        effective_to=date.fromisoformat(version["effective_to"]) if version["effective_to"] else None,
        rule_id=rule.get("id"), explain=(found.get("reason") or "",))


def resolve(db: Session, company_id: int, type_code: str, employee_id: int, d: date) -> Optional[PolicyVersionData]:
    """The ACTIVE version of ``type_code`` for the employee on ``d``; ``None`` when nothing resolves (no rule, a tie,
    no active version, not placed): the caller applies its LEGACY_V1 fallback."""
    return _data(type_code, policies.resolve_for_employee(db, company_id, type_code, employee_id, d))


def explain(db: Session, company_id: int, type_code: str, employee_id: int, d: date) -> dict:
    """The resolver's full answer (status, rule, reason) for ``GET /attendance/policies/resolve``."""
    return policies.resolve_for_employee(db, company_id, type_code, employee_id, d)


def resolve_branch(db: Session, company_id: int, type_code: str, branch_id: Optional[int], d: date
                   ) -> Optional[PolicyVersionData]:
    """Branch-level resolution: rules on the branch, its legal entity or everyone (never team or employee rules)."""
    branch = db.get(Branch, branch_id) if branch_id else None
    person = pt.EmployeeAttributes(employee_id=0, branch_id=branch_id,
                                   legal_entity_id=getattr(branch, "legal_entity_id", None))
    rules = policies._type_rules(db, company_id, type_code)
    result = pt.resolve_assignment(rules, person, on=d)
    if result.status != "RESOLVED":
        return None
    policy = policies._load(db, company_id, int(result.rule.policy_id))
    versions = policies._versions(db, company_id, [policy.id]).get(policy.id, [])
    active = policies._active_on(versions, d)
    if active is None:
        return None
    return PolicyVersionData(type_code=type_code, policy_id=policy.id, policy_name=policy.name,
                             version=active.version, fields=active.fields, effective_from=active.effective_from,
                             effective_to=active.effective_to, rule_id=result.rule.rule_id,
                             explain=(f"branch-level rule {result.rule.rule_id} of \"{policy.name}\"",))

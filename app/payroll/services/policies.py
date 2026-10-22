"""Payroll policies: CRUD, versions, assignment rules and resolution (3.1 §3.2).

A policy has a type and a name; its versions are drafts until activated with an effective date,
which closes the previous ACTIVE version the day before (versions starting between that date and
today are superseded, a later scheduled one stays and bounds the new one). Activation validates every field the
features switched ON need (no legal constant is assumed) and the country packs' legal limits.

Assignment rules (criteria + priority + dates) decide which policy of a type applies to an
employee on a date: the highest-priority matching rule wins, and a rule set in which two rules of
the same priority match one employee is rejected listing the employees (EC-CFG-05). Employee
attributes come from the primary assignment on the date. ``employees_without_policy`` feeds the
V-11 pre-check (EC-CFG-07).

A policy is archived, never deleted (EC-CFG-16): ``policy_in_use`` returns False until pay runs
exist and then records that a run used it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.core.errors import AppError, bad_request, forbidden, not_found
from app.core.principal import Principal
from app.core.security import utcnow
from app.models.employee import Department, Employee
from app.models.org import Branch, EmployeeAssignment, LegalEntity
from app.models.pay_org import Designation, EmploymentType, Grade, PayGroup
from app.models.payroll_config import Policy, PolicyAssignment, PolicyVersion, SettingValue
from app.payroll import audit as payroll_audit
from app.payroll import policy_types as pt
from app.payroll import settings_registry as reg
from app.payroll.services import settings as engine
from app.services.audit_log_service import AuditLogService

READ = engine.READ
WRITE = engine.WRITE
MAX_RULES = 200
_ONE_DAY = timedelta(days=1)

POLICY_CREATED = "POLICY_CREATED"
POLICY_VERSION_CREATED = "POLICY_VERSION_CREATED"
POLICY_VERSION_UPDATED = "POLICY_VERSION_UPDATED"
POLICY_VERSION_ACTIVATED = "POLICY_VERSION_ACTIVATED"
POLICY_ASSIGNMENTS_CHANGED = "POLICY_ASSIGNMENTS_CHANGED"
POLICY_ARCHIVED = "POLICY_ARCHIVED"

_REFERENCES = (
    ("legal_entity_ids", LegalEntity, "legal entities"),
    ("branch_ids", Branch, "branches"),
    ("pay_group_ids", PayGroup, "pay groups"),
    ("department_ids", Department, "departments"),
    ("grade_ids", Grade, "grades"),
    ("designation_ids", Designation, "designations"),
    ("employee_ids", Employee, "employees"),
)


# --------------------------------------------------------------------------- hooks


def policy_in_use(db: Session, company_id: int, policy_id: int) -> bool:
    """Whether a pay run used the policy (then it can only be archived); False until runs exist."""
    return False


def mark_runs_stale(db: Session, company_id: int, policy_id: int, effective_from: date) -> int:
    """Runs with locked inputs affected by a newly active version become stale (EC-CFG-14); no-op until runs exist."""
    return 0


# --------------------------------------------------------------------------- helpers


def _policy_error(error: pt.PolicyValidationError, status: int = 400) -> AppError:
    detail: Any = error.message
    if error.details or error.field:
        detail = {"message": error.message, "field": error.field, "details": error.details}
    return AppError(status, detail, error.code)


def _require_company_write(principal: Principal) -> None:
    if not principal.has(WRITE, "COMPANY"):
        raise forbidden("Policies are company-wide: changing them needs company-wide payroll configuration access.")


def _today(db: Session, company_id: int) -> date:
    return engine.company_today(engine._company(db, company_id))


def _load(db: Session, company_id: int, policy_id: int, *, lock: bool = False) -> Policy:
    query = db.query(Policy).filter(Policy.company_id == company_id, Policy.id == policy_id)
    if lock:
        query = query.with_for_update()
    policy = query.first()
    if policy is None:
        raise not_found("Policy")
    return policy


def _version(db: Session, company_id: int, policy_id: int, version: int, *, lock: bool = False) -> PolicyVersion:
    query = db.query(PolicyVersion).filter(PolicyVersion.company_id == company_id, PolicyVersion.policy_id == policy_id,
                                           PolicyVersion.version == version)
    if lock:
        query = query.with_for_update()
    row = query.first()
    if row is None:
        raise not_found("Policy version")
    return row


def _name(value: str) -> str:
    name = (value or "").strip()
    if not 1 <= len(name) <= 150:
        raise bad_request("The policy name must be 1-150 characters.", "INVALID_NAME")
    return name


def _description(value: str | None) -> str | None:
    text = (value or "").strip()
    if len(text) > 2000:
        raise bad_request("The description is at most 2000 characters.", "INVALID_DESCRIPTION")
    return text or None


def _type(code: str) -> pt.PolicyTypeDef:
    try:
        return pt.policy_type(code)
    except pt.PolicyValidationError as error:
        raise _policy_error(error) from None


def _validate(code: str, fields: Mapping[str, Any], **options: Any) -> dict[str, Any]:
    try:
        return pt.validate_fields(code, fields, **options)
    except pt.PolicyValidationError as error:
        raise _policy_error(error) from None


def version_json(row: PolicyVersion) -> dict[str, Any]:
    return {
        "id": row.id,
        "version": row.version,
        "status": row.status,
        "effective_from": row.effective_from.isoformat(),
        "effective_to": row.effective_to.isoformat() if row.effective_to else None,
        "fields": row.fields,
        "policy_hash": row.policy_hash,
        "activated_at": row.activated_at.isoformat() if row.activated_at else None,
        "activated_by_user_id": row.activated_by_user_id,
    }


def rule_json(row: PolicyAssignment) -> dict[str, Any]:
    return {
        "id": row.id,
        "criteria": row.criteria,
        "priority": row.priority,
        "effective_from": row.effective_from.isoformat(),
        "effective_to": row.effective_to.isoformat() if row.effective_to else None,
        "status": row.status,
    }


def _versions(db: Session, company_id: int, policy_ids: Sequence[int]) -> dict[int, list[PolicyVersion]]:
    found: dict[int, list[PolicyVersion]] = {}
    if not policy_ids:
        return found
    rows = (db.query(PolicyVersion).filter(PolicyVersion.company_id == company_id,
                                           PolicyVersion.policy_id.in_(policy_ids))
            .order_by(PolicyVersion.policy_id, PolicyVersion.version))
    for row in rows:
        found.setdefault(row.policy_id, []).append(row)
    return found


def _rules(db: Session, company_id: int, policy_ids: Sequence[int], *, active_only: bool = True
           ) -> dict[int, list[PolicyAssignment]]:
    found: dict[int, list[PolicyAssignment]] = {}
    if not policy_ids:
        return found
    query = db.query(PolicyAssignment).filter(PolicyAssignment.company_id == company_id,
                                              PolicyAssignment.policy_id.in_(policy_ids))
    if active_only:
        query = query.filter(PolicyAssignment.status == "ACTIVE")
    for row in query.order_by(PolicyAssignment.priority.desc(), PolicyAssignment.id):
        found.setdefault(row.policy_id, []).append(row)
    return found


def _active_on(versions: Iterable[PolicyVersion], on: date) -> PolicyVersion | None:
    return next((v for v in versions if v.status == "ACTIVE" and v.effective_from <= on
                 and (v.effective_to is None or v.effective_to >= on)), None)


def policy_json(policy: Policy, versions: Sequence[PolicyVersion], rules: Sequence[PolicyAssignment], on: date,
                *, detail: bool = False) -> dict[str, Any]:
    active = _active_on(versions, on)
    found: dict[str, Any] = {
        "id": policy.id,
        "type": policy.type,
        "name": policy.name,
        "description": policy.description,
        "status": policy.status,
        "active_version": version_json(active) if active is not None else None,
        "latest_version": max((v.version for v in versions), default=None),
        "draft_versions": [v.version for v in versions if v.status == "DRAFT"],
        "rule_count": len(rules),
        "created_at": policy.created_at.isoformat() if policy.created_at else None,
    }
    if detail:
        found["versions"] = [version_json(v) for v in versions]
        found["assignments"] = [rule_json(r) for r in rules]
    return found


# --------------------------------------------------------------------------- policies


def list_policies(db: Session, principal: Principal, *, type_code: str | None = None,
                  include_archived: bool = False) -> list[dict[str, Any]]:
    query = db.query(Policy).filter(Policy.company_id == principal.company_id)
    if type_code is not None:
        query = query.filter(Policy.type == _type(type_code).code)
    if not include_archived:
        query = query.filter(Policy.status == "ACTIVE")
    policies = query.order_by(Policy.type, Policy.name).all()
    ids = [p.id for p in policies]
    versions, rules = _versions(db, principal.company_id, ids), _rules(db, principal.company_id, ids)
    today = _today(db, principal.company_id)
    counts = head_counts(db, principal, {p.type for p in policies}, today)
    scope = "COMPANY" if principal.has(READ, "COMPANY") else "SCOPED"
    return [policy_json(p, versions.get(p.id, []), rules.get(p.id, []), today)
            | {"head_count": counts.get(p.id, 0), "head_count_scope": scope} for p in policies]


def head_counts(db: Session, principal: Principal, type_codes: Iterable[str], on: date) -> dict[int, int]:
    """{policy_id: active employees placed on ``on`` whom the policy's rules resolve to}, counting only
    employees whose entity or pay group the caller's configuration access covers (F-25: a scoped
    administrator never learns head counts outside their scope)."""
    cid = principal.company_id
    active = {i for (i,) in db.query(Employee.id).filter(
        Employee.company_id == cid, Employee.is_active.is_(True),
        or_(Employee.termination_date.is_(None), Employee.termination_date >= on))}
    people = [p for p in employee_attributes(db, cid, on) if p.employee_id in active
              and engine.covers(principal, READ, legal_entity_id=p.legal_entity_id, pay_group_id=p.pay_group_id)]
    counts: dict[int, int] = {}
    for code in sorted(set(type_codes)):
        rules = _type_rules(db, cid, code)
        if not rules:
            continue
        for person in people:
            result = pt.resolve_assignment(rules, person, on=on)
            if result.status == "RESOLVED":
                counts[int(result.rule.policy_id)] = counts.get(int(result.rule.policy_id), 0) + 1
    return counts


def get_policy(db: Session, principal: Principal, policy_id: int) -> dict[str, Any]:
    policy = _load(db, principal.company_id, policy_id)
    versions = _versions(db, principal.company_id, [policy.id]).get(policy.id, [])
    rules = _rules(db, principal.company_id, [policy.id], active_only=False).get(policy.id, [])
    return policy_json(policy, versions, [r for r in rules if r.status == "ACTIVE"],
                       _today(db, principal.company_id), detail=True) | {
        "assignments": [rule_json(r) for r in rules if r.status == "ACTIVE"],
        "rule_history": [rule_json(r) for r in rules if r.status != "ACTIVE"],
    }


def _audit(db: Session, principal: Principal | None, action: str, policy: Policy, description: str,
           details: Mapping[str, Any], *, company_id: int | None = None) -> None:
    AuditLogService.log(
        db, principal, payroll_audit.PAYROLL_POLICIES, action, description, "policy", policy.id,
        company_id=company_id, actor_type="user" if principal is not None else "system",
        details={"policy_id": policy.id, "type": policy.type, "name": policy.name, **details},
    )


def create_policy(db: Session, principal: Principal, *, type_code: str, name: str, description: str | None = None,
                  fields: Mapping[str, Any] | None = None, effective_from: date | None = None) -> dict[str, Any]:
    """A new policy with its first DRAFT version (defaults filled in)."""
    _require_company_write(principal)
    ptype = _type(type_code)
    name = _name(name)
    cid = principal.company_id
    if db.query(Policy.id).filter(Policy.company_id == cid, Policy.type == ptype.code,
                                  func.lower(Policy.name) == name.lower()).first():
        raise AppError(409, f"A {ptype.label.lower()} named \"{name}\" already exists.", "POLICY_NAME_TAKEN")
    normalized = _validate(ptype.code, fields or {})
    policy = Policy(company_id=cid, type=ptype.code, name=name, description=_description(description),
                    status="ACTIVE", created_by_user_id=principal.user_id)
    db.add(policy)
    db.flush()
    version = PolicyVersion(company_id=cid, policy_id=policy.id, version=1, fields=normalized, status="DRAFT",
                            effective_from=effective_from or _today(db, cid))
    db.add(version)
    db.flush()
    _audit(db, principal, POLICY_CREATED, policy, f"Created {ptype.label.lower()} \"{name}\" (draft version 1)",
           {"version": 1})
    return get_policy(db, principal, policy.id)


def create_version(db: Session, principal: Principal, policy_id: int, *, fields: Mapping[str, Any] | None = None,
                   effective_from: date | None = None) -> dict[str, Any]:
    """A new DRAFT version; without fields it copies the latest version (editing never changes an active one)."""
    _require_company_write(principal)
    policy = _load(db, principal.company_id, policy_id, lock=True)
    if policy.status != "ACTIVE":
        raise AppError(409, "An archived policy cannot get new versions.", "POLICY_ARCHIVED")
    versions = _versions(db, principal.company_id, [policy.id]).get(policy.id, [])
    base = versions[-1].fields if versions else {}
    normalized = _validate(policy.type, base if fields is None else fields)
    number = (versions[-1].version if versions else 0) + 1
    row = PolicyVersion(company_id=principal.company_id, policy_id=policy.id, version=number, fields=normalized,
                        status="DRAFT", effective_from=effective_from or _today(db, principal.company_id))
    db.add(row)
    db.flush()
    _audit(db, principal, POLICY_VERSION_CREATED, policy, f"Created draft version {number} of \"{policy.name}\"",
           {"version": number})
    return version_json(row)


def update_version(db: Session, principal: Principal, policy_id: int, version: int, *,
                   fields: Mapping[str, Any] | None = None, effective_from: date | None = None) -> dict[str, Any]:
    """Edit a DRAFT version in place; an activated version is immutable (409)."""
    _require_company_write(principal)
    policy = _load(db, principal.company_id, policy_id)
    row = _version(db, principal.company_id, policy.id, version, lock=True)
    if row.status != "DRAFT":
        raise AppError(409, "Only a draft version can be edited; create a new version instead.", "VERSION_NOT_DRAFT")
    changed: list[str] = []
    if fields is not None:
        row.fields = _validate(policy.type, fields)
        changed.append("fields")
    if effective_from is not None:
        row.effective_from = effective_from
        changed.append("effective_from")
    db.flush()
    _audit(db, principal, POLICY_VERSION_UPDATED, policy, f"Edited draft version {version} of \"{policy.name}\"",
           {"version": version, "changed": changed})
    return version_json(row)


def switches_on_anywhere(db: Session, company_id: int, keys: Sequence[str], on: date) -> set[str]:
    """Bool switches ON at tenant level (resolved) or in any narrower ACTIVE row on ``on``."""
    if not keys:
        return set()
    ctx = engine.build_context(db, company_id, on=on)
    tenant = engine.resolve_in_context(db, ctx, list(keys), strict=False)
    found = {k for k, r in tenant.items() if r.value is True}
    rows = db.query(SettingValue.key, SettingValue.value).filter(
        SettingValue.company_id == company_id, SettingValue.key.in_(list(keys)), SettingValue.status == "ACTIVE",
        SettingValue.scope_type != "TENANT", SettingValue.effective_from <= on,
        or_(SettingValue.effective_to.is_(None), SettingValue.effective_to >= on),
    )
    found.update(key for key, value in rows if value is True)
    return found


def _legal_issues(db: Session, company_id: int, code: str, name: str, fields: Mapping[str, Any], on: date
                  ) -> list[reg.Issue]:
    countries = sorted({c for (c,) in db.query(LegalEntity.country_code).filter(LegalEntity.company_id == company_id)
                        if c})
    constraints = [c for country in countries for c in engine.pack_constraints(country, on)]
    return reg.policy_legal_issues(code, fields, constraints, name=name)


def activate_version(db: Session, principal: Principal, policy_id: int, version: int, *,
                     effective_from: date | None = None, reason: str | None = None) -> dict[str, Any]:
    """Activate a DRAFT version from ``effective_from``, closing the version it replaces.

    A version starting earlier ends the day before; versions starting from that date up to today
    are superseded; a version scheduled after both stays and bounds the new one.
    """
    _require_company_write(principal)
    cid = principal.company_id
    policy = _load(db, cid, policy_id, lock=True)
    if policy.status != "ACTIVE":
        raise AppError(409, "An archived policy cannot be activated.", "POLICY_ARCHIVED")
    row = _version(db, cid, policy.id, version, lock=True)
    if row.status != "DRAFT":
        raise AppError(409, "This version is already active or superseded.", "VERSION_NOT_DRAFT")
    today = _today(db, cid)
    start = effective_from or row.effective_from
    if start < today and not (reason or "").strip():
        raise bad_request("A reason is required to activate a version effective in the past.", "REASON_REQUIRED")
    ptype = pt.POLICY_TYPES[policy.type]
    switched_on = switches_on_anywhere(db, cid, ptype.gate_switches, start)
    fields = _validate(policy.type, row.fields, for_activation=True, enabled_switches=switched_on)
    issues = _legal_issues(db, cid, policy.type, policy.name, fields, start)
    if issues:
        raise AppError(400, {"message": issues[0].message, "issues": [
            {"rule": i.rule, "message": i.message, "details": dict(i.details)} for i in issues]}, "LEGAL_LIMIT")
    existing = (
        db.query(PolicyVersion)
        .filter(PolicyVersion.company_id == cid, PolicyVersion.policy_id == policy.id,
                PolicyVersion.status == "ACTIVE",
                or_(PolicyVersion.effective_to.is_(None), PolicyVersion.effective_to >= start))
        .with_for_update()
        .all()
    )
    horizon = max(start, today)
    later = [v.effective_from for v in existing if v.effective_from > horizon]
    closed: list[int] = []
    for old in existing:
        if old.effective_from < start:
            old.effective_to = start - _ONE_DAY
            closed.append(old.version)
        elif old.effective_from <= horizon:
            old.status = "SUPERSEDED"
            closed.append(old.version)
    db.flush()
    row.fields = fields
    row.effective_from = start
    row.effective_to = min(later) - _ONE_DAY if later else None
    row.policy_hash = pt.policy_hash(fields)
    row.status = "ACTIVE"
    row.activated_at = utcnow()
    row.activated_by_user_id = principal.user_id
    db.flush()
    _audit(db, principal, POLICY_VERSION_ACTIVATED, policy,
           f"Activated version {version} of \"{policy.name}\" from {start.isoformat()}",
           {"version": version, "effective_from": start, "effective_to": row.effective_to,
            "policy_hash": row.policy_hash, "replaced_versions": closed, "reason": (reason or "").strip() or None})
    mark_runs_stale(db, cid, policy.id, start)
    grain = ptype.grain
    notice = (f"Applies to the whole {start:%B %Y} period." if grain == "PERIOD"
              else f"Applies from {start.day} {start:%B %Y}.")
    return version_json(row) | {"message": notice}


def archive_policy(db: Session, principal: Principal, policy_id: int) -> dict[str, Any]:
    """Archive a policy (never deleted, EC-CFG-16): it stops resolving; versions and rules are kept."""
    _require_company_write(principal)
    policy = _load(db, principal.company_id, policy_id, lock=True)
    if policy.status == "ARCHIVED":
        raise AppError(409, "This policy is already archived.", "POLICY_ARCHIVED")
    policy.status = "ARCHIVED"
    db.flush()
    in_use = policy_in_use(db, principal.company_id, policy.id)
    _audit(db, principal, POLICY_ARCHIVED, policy, f"Archived \"{policy.name}\"", {"used_by_runs": in_use})
    return {"id": policy.id, "status": policy.status, "used_by_runs": in_use}


# --------------------------------------------------------------------------- assignment rules


@dataclass(frozen=True)
class RuleInput:
    criteria: Mapping[str, Any]
    priority: int = 0
    effective_from: date | None = None
    effective_to: date | None = None


def employee_attributes(db: Session, company_id: int, on: date, employee_ids: Iterable[int] | None = None
                        ) -> list[pt.EmployeeAttributes]:
    """Attributes of employees placed on ``on`` (primary assignment), for criteria matching."""
    query = (
        db.query(EmployeeAssignment, EmploymentType.code)
        .outerjoin(EmploymentType, and_(EmploymentType.id == EmployeeAssignment.employment_type_id,
                                        EmploymentType.company_id == EmployeeAssignment.company_id))
        .filter(EmployeeAssignment.company_id == company_id, EmployeeAssignment.is_primary.is_(True),
                EmployeeAssignment.effective_from <= on,
                or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= on))
    )
    if employee_ids is not None:
        query = query.filter(EmployeeAssignment.employee_id.in_(list(employee_ids)))
    return [
        pt.EmployeeAttributes(
            employee_id=a.employee_id, legal_entity_id=a.legal_entity_id, branch_id=a.branch_id,
            pay_group_id=a.pay_group_id, department_id=a.department_id, grade_id=a.grade_id,
            employment_type=code, designation_id=a.designation_id,
        )
        for a, code in query.order_by(EmployeeAssignment.employee_id)
    ]


def _check_references(db: Session, company_id: int, criteria: pt.AssignmentCriteria) -> None:
    for name, model, what in _REFERENCES:
        ids = getattr(criteria, name)
        if not ids:
            continue
        found = {i for (i,) in db.query(model.id).filter(model.company_id == company_id, model.id.in_(list(ids)))}
        missing = sorted(set(ids) - found)
        if missing:
            raise bad_request(f"Unknown {what} in the criteria: {', '.join(map(str, missing))}.", "UNKNOWN_REFERENCE")
    if criteria.employment_types:
        codes = {c for (c,) in db.query(EmploymentType.code).filter(EmploymentType.company_id == company_id,
                                                                    EmploymentType.code.in_(criteria.employment_types))}
        missing = sorted(set(criteria.employment_types) - codes)
        if missing:
            raise bad_request(f"Unknown employment types in the criteria: {', '.join(missing)}.", "UNKNOWN_REFERENCE")


def _type_rules(db: Session, company_id: int, code: str, *, exclude_policy: int | None = None
                ) -> list[pt.AssignmentRule]:
    query = (
        db.query(PolicyAssignment)
        .join(Policy, and_(Policy.id == PolicyAssignment.policy_id, Policy.company_id == PolicyAssignment.company_id))
        .filter(PolicyAssignment.company_id == company_id, PolicyAssignment.status == "ACTIVE",
                Policy.type == code, Policy.status == "ACTIVE")
    )
    if exclude_policy is not None:
        query = query.filter(PolicyAssignment.policy_id != exclude_policy)
    return [
        pt.AssignmentRule(rule_id=r.id, policy_id=r.policy_id, priority=r.priority, criteria=r.criteria,
                          effective_from=r.effective_from, effective_to=r.effective_to)
        for r in query.order_by(PolicyAssignment.id)
    ]


def get_assignments(db: Session, principal: Principal, policy_id: int) -> dict[str, Any]:
    policy = _load(db, principal.company_id, policy_id)
    rules = _rules(db, principal.company_id, [policy.id]).get(policy.id, [])
    return {"policy_id": policy.id, "type": policy.type, "rules": [rule_json(r) for r in rules]}


def put_assignments(db: Session, principal: Principal, policy_id: int, rules: Sequence[RuleInput]) -> dict[str, Any]:
    """Replace a policy's rule set; earlier rules become INACTIVE (kept for history)."""
    _require_company_write(principal)
    cid = principal.company_id
    policy = _load(db, cid, policy_id, lock=True)
    if policy.status != "ACTIVE":
        raise AppError(409, "An archived policy cannot be assigned.", "POLICY_ARCHIVED")
    ptype = pt.POLICY_TYPES[policy.type]
    if not ptype.assignable:
        raise bad_request(f"{ptype.label} is not assigned to employees.", "NOT_ASSIGNABLE")
    if len(rules) > MAX_RULES:
        raise bad_request(f"At most {MAX_RULES} rules per policy.", "TOO_MANY_RULES")
    today = _today(db, cid)
    proposed: list[pt.AssignmentRule] = []
    for index, rule in enumerate(rules):
        try:
            criteria = pt.parse_criteria(rule.criteria)
            proposed.append(pt.AssignmentRule(
                rule_id=f"new-{index + 1}", policy_id=policy.id, priority=rule.priority, criteria=criteria,
                effective_from=rule.effective_from or today, effective_to=rule.effective_to,
            ))
        except pt.PolicyValidationError as error:
            raise _policy_error(error) from None
        _check_references(db, cid, criteria)
    combined = _type_rules(db, cid, policy.type, exclude_policy=policy.id) + proposed
    days = sorted({today, *(r.effective_from for r in proposed if r.effective_from and r.effective_from > today)})
    for day in days:
        try:
            pt.check_assignment_rules(combined, employee_attributes(db, cid, day), on=day)
        except pt.PolicyValidationError as error:
            raise _policy_error(error) from None
    try:
        pt.check_assignment_rules(proposed)
    except pt.PolicyValidationError as error:
        raise _policy_error(error) from None
    previous = _rules(db, cid, [policy.id]).get(policy.id, [])
    for row in previous:
        row.status = "INACTIVE"
    db.flush()
    created = []
    for rule in proposed:
        row = PolicyAssignment(company_id=cid, policy_id=policy.id, criteria=rule.criteria.model_dump(exclude_none=True),
                               priority=rule.priority, effective_from=rule.effective_from,
                               effective_to=rule.effective_to, status="ACTIVE")
        db.add(row)
        created.append(row)
    db.flush()
    _audit(db, principal, POLICY_ASSIGNMENTS_CHANGED, policy, f"Changed the assignment rules of \"{policy.name}\"",
           {"old": [rule_json(r) for r in previous], "new": [rule_json(r) for r in created]})
    return {"policy_id": policy.id, "type": policy.type, "rules": [rule_json(r) for r in created]}


# --------------------------------------------------------------------------- resolution


_DIMENSION_TEXT = {
    "legal_entity_ids": "legal entity", "branch_ids": "branch", "pay_group_ids": "pay group",
    "department_ids": "department", "grade_ids": "grade", "employment_types": "employment type",
    "designation_ids": "designation", "employee_ids": "employee",
}


def _criteria_text(criteria: pt.AssignmentCriteria) -> str:
    if criteria.everyone:
        return "everyone"
    parts = []
    for name, _ in pt.CRITERIA_DIMENSIONS:
        values = getattr(criteria, name)
        if values is not None:
            parts.append(f"{_DIMENSION_TEXT[name]} in ({', '.join(map(str, values))})")
    return " and ".join(parts)


def resolve_for_employee(db: Session, company_id: int, type_code: str, employee_id: int, on: date) -> dict[str, Any]:
    """Which version of which policy applies to the employee on ``on``, and why."""
    ptype = _type(type_code)
    attributes = employee_attributes(db, company_id, on, [employee_id])
    base = {"type": ptype.code, "employee_id": employee_id, "date": on.isoformat(), "policy": None,
            "version": None, "rule": None}
    if not attributes:
        return base | {"status": "NOT_PLACED", "reason": "The employee has no placement on this date."}
    employee = attributes[0]
    rules = _type_rules(db, company_id, ptype.code)
    result = pt.resolve_assignment(rules, employee, on=on)
    if result.status == "NO_POLICY":
        return base | {"status": "NO_POLICY", "reason": f"No {ptype.label.lower()} rule matches this employee.",
                       "attributes": asdict(employee)}
    if result.status == "AMBIGUOUS":
        return base | {"status": "AMBIGUOUS", "candidates": [r.rule_id for r in result.candidates],
                       "reason": f"Rules {', '.join(str(r.rule_id) for r in result.candidates)} have the same priority "
                                 f"({result.candidates[0].priority}) and match this employee."}
    rule = result.rule
    policy = _load(db, company_id, int(rule.policy_id))
    versions = _versions(db, company_id, [policy.id]).get(policy.id, [])
    active = _active_on(versions, on)
    rule_info = {"id": rule.rule_id, "priority": rule.priority, "criteria": rule.criteria.model_dump(exclude_none=True)}
    others = [r.rule_id for r in result.matched if r.rule_id != rule.rule_id]
    reason = (f"Rule {rule.rule_id} of \"{policy.name}\" ({_criteria_text(rule.criteria)}, priority {rule.priority}) "
              f"is the highest-priority rule matching this employee")
    reason += f"; lower-priority matches: {', '.join(map(str, others))}." if others else "."
    found = base | {"policy": {"id": policy.id, "name": policy.name}, "rule": rule_info, "reason": reason}
    if active is None:
        return found | {"status": "NO_ACTIVE_VERSION",
                        "reason": reason + f" \"{policy.name}\" has no active version on this date."}
    return found | {"status": "RESOLVED", "version": version_json(active)}


def resolve_policy(db: Session, principal: Principal, type_code: str, employee_id: int, on: date | None = None
                   ) -> dict[str, Any]:
    """``resolve_for_employee`` for a caller whose configuration access covers the employee (else 404)."""
    today = _today(db, principal.company_id)
    engine.scope_target(db, principal, READ, "EMPLOYEE", employee_id, on=on or today)
    return resolve_for_employee(db, principal.company_id, type_code, employee_id, on or today)


def employees_without_policy(
    db: Session,
    company_id: int,
    type_code: str,
    *,
    on: date,
    legal_entity_id: int | None = None,
    branch_id: int | None = None,
    pay_group_id: int | None = None,
) -> list[dict[str, Any]]:
    """Employees in scope who resolve no ACTIVE version of ``type_code`` on ``on`` (V-11, EC-CFG-07)."""
    ptype = _type(type_code)
    people = [
        e for e in employee_attributes(db, company_id, on)
        if (legal_entity_id is None or e.legal_entity_id == legal_entity_id)
        and (branch_id is None or e.branch_id == branch_id)
        and (pay_group_id is None or e.pay_group_id == pay_group_id)
    ]
    rules = _type_rules(db, company_id, ptype.code)
    policy_ids = sorted({int(r.policy_id) for r in rules})
    versions = _versions(db, company_id, policy_ids)
    active = {pid for pid in policy_ids if _active_on(versions.get(pid, []), on) is not None}
    missing: list[dict[str, Any]] = []
    for person in people:
        result = pt.resolve_assignment(rules, person, on=on)
        if result.status == "RESOLVED" and int(result.rule.policy_id) in active:
            continue
        status = result.status if result.status != "RESOLVED" else "NO_ACTIVE_VERSION"
        missing.append({"employee_id": person.employee_id, "status": status})
    return missing


# --------------------------------------------------------------------------- profiles


def create_starter_policy(db: Session, company_id: int, starter: pt.StarterPolicy, *, effective_from: date) -> Policy:
    """Write one catch-all policy of a configuration profile (no permission checks; used by profiles)."""
    policy = Policy(company_id=company_id, type=starter.type, name=starter.name, status="ACTIVE",
                    description=starter.note)
    db.add(policy)
    db.flush()
    active = starter.status == "ACTIVE"
    db.add(PolicyVersion(
        company_id=company_id, policy_id=policy.id, version=1, fields=dict(starter.fields), status=starter.status,
        effective_from=effective_from, policy_hash=starter.policy_hash if active else None,
        activated_at=utcnow() if active else None,
    ))
    if pt.POLICY_TYPES[starter.type].assignable:
        db.add(PolicyAssignment(company_id=company_id, policy_id=policy.id, criteria=dict(starter.criteria),
                                priority=starter.priority, effective_from=effective_from, status="ACTIVE"))
    db.flush()
    return policy

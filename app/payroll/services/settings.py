"""Settings engine v2: resolution, values per scope and change control (3.1 §2, 02 §14).

Resolution is the read path of payroll. For a key, a company, a date and an employee (or an
explicit legal entity, branch or pay group) it walks EMPLOYEE > PAY_GROUP > BRANCH > LEGAL_ENTITY >
TENANT > PARTNER > COUNTRY_PACK > PLATFORM; a locked value at a broader level beats narrower
overrides (EC-CFG-04) and a value the law requires beats everything. Country packs contribute
legal limits through ``app.payroll.packs.setting_constraints(country_code, on)`` when that hook
exists. A payroll value that no longer validates, or breaks a legal limit, raises
``SettingConfigError`` (pre-check CONFIG_INVALID, EC-CFG-06, EC-CFG-09); other keys keep the
legacy warning and default.

Writes are effective-dated rows: a new row closes the previous ACTIVE row the day before (rows
starting between its date and today are superseded, a later scheduled row stays and bounds it), rows
are never deleted, past dates need a reason, and the per key-and-scope ``row_version`` makes a
stale save a 409 carrying the current value (EC-CFG-10). With ``settings.change_approval.enabled``
ON, keys that require approval wait as PENDING_APPROVAL for a second administrator while the old
value stays in force (EC-CFG-12).

Hooks until pay runs exist: ``finalized_period_overlaps`` always returns False, ``mark_runs_stale``
does nothing, and the open period named in a change notice is the calendar month of the date.
"""

from __future__ import annotations

import calendar
import importlib
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import pytz
from sqlalchemy import and_, false, func, or_
from sqlalchemy.orm import Session

from app.core.errors import AppError, bad_request, forbidden, not_found
from app.core.principal import Principal
from app.core.security import utcnow
from app.models.company import Company
from app.models.employee import Employee
from app.models.org import Branch, EmployeeAssignment, LegalEntity
from app.models.pay_org import HolidayCalendar, PayGroup
from app.models.payroll_config import (
    PlatformSettingValue,
    Policy,
    PolicyVersion,
    SettingChangeRequest,
    SettingValue,
)
from app.models.user import User
from app.payroll import audit as payroll_audit
from app.payroll import currencies
from app.payroll import settings_registry as reg
from app.payroll.settings_registry import UNSET, Derived, Issue, Level, SettingValidationError
from app.services.audit_log_service import AuditLogService

logger = logging.getLogger(__name__)

READ = "payroll:configuration:read"
WRITE = "payroll:configuration:write"
APPROVE = "settings:approve_changes"
# Keys outside the Payroll module are company settings: changing them also needs settings:manage.
COMPANY_SETTINGS = "settings:manage"
CHANGE_APPROVAL_KEY = "settings.change_approval.enabled"

SETTING_CHANGED = "SETTING_CHANGED"
SETTING_CHANGE_REQUESTED = "SETTING_CHANGE_REQUESTED"
SETTING_CHANGE_APPROVED = "SETTING_CHANGE_APPROVED"
SETTING_CHANGE_REJECTED = "SETTING_CHANGE_REJECTED"
SETTINGS_MODULE = "Settings"

MAX_BATCH = 200
_ONE_DAY = timedelta(days=1)
_SCOPE_TYPES = tuple(level.value for level in reg.SCOPE_LEVELS)
_LEVEL_NAMES = {
    Level.EMPLOYEE: "employee", Level.PAY_GROUP: "pay group", Level.BRANCH: "branch",
    Level.LEGAL_ENTITY: "legal entity", Level.TENANT: "tenant", Level.PARTNER: "partner",
    Level.COUNTRY_PACK: "country pack", Level.PLATFORM: "platform default",
}


class SettingConfigError(Exception):
    """A payroll setting resolved to an invalid or illegal value: a CONFIG_INVALID pre-check (EC-CFG-09)."""

    code = "CONFIG_INVALID"

    def __init__(self, key: str, issues: Sequence[Issue], *, context: Mapping[str, Any] | None = None) -> None:
        self.key = key
        self.issues = tuple(issues)
        self.context = dict(context or {})
        message = "; ".join(i.message for i in self.issues) or f"{key} has no valid value"
        super().__init__(message)
        self.message = message


# --------------------------------------------------------------------------- hooks


def finalized_period_overlaps(db: Session, company_id: int, key: str, level: Level, scope_id: int | None,
                              effective_from: date) -> bool:
    """Whether ``effective_from`` falls in a finalized pay period of the scope (EC-CFG-03).

    False until pay runs exist: app/payroll/services/runs.install_hooks() replaces this hook with the
    run-backed answer. A True result requires a reason (already required for any past date) and the
    retro impact preview (POST /api/v1/payroll/arrears-proposals/detect with trigger SETTING and preview).
    """
    return False


def mark_runs_stale(db: Session, company_id: int, keys: Iterable[str], level: Level, scope_id: int | None,
                    effective_from: date) -> int:
    """Mark runs with locked, unfinalized inputs affected by a change as stale (EC-CFG-14); no-op until runs exist."""
    return 0


def pack_constraints(country_code: str | None, on: date) -> tuple[reg.LegalConstraint, ...]:
    """Legal limits of the country pack in force on ``on`` (hook ``app.payroll.packs.setting_constraints``)."""
    if not country_code:
        return ()
    try:
        packs = importlib.import_module("app.payroll.packs")
    except ModuleNotFoundError as error:
        if error.name != "app.payroll.packs":
            raise
        return ()
    hook = getattr(packs, "setting_constraints", None)
    if hook is None:
        return ()
    return tuple(hook(country_code, on) or ())


# --------------------------------------------------------------------------- context


@dataclass(frozen=True)
class Context:
    """Where and when a value is resolved: the company, the date and the scope chain."""

    company_id: int
    on: date
    legal_entity_id: int | None = None
    branch_id: int | None = None
    pay_group_id: int | None = None
    employee_id: int | None = None
    country_code: str | None = None
    partner_id: int | None = None
    facts: Mapping[str, Any] = field(default_factory=dict, hash=False)

    def scope_id(self, level: Level) -> int | None:
        return {
            Level.LEGAL_ENTITY: self.legal_entity_id, Level.BRANCH: self.branch_id,
            Level.PAY_GROUP: self.pay_group_id, Level.EMPLOYEE: self.employee_id,
        }.get(level)

    def as_json(self) -> dict[str, Any]:
        return {
            "date": self.on.isoformat(), "legal_entity_id": self.legal_entity_id, "branch_id": self.branch_id,
            "pay_group_id": self.pay_group_id, "employee_id": self.employee_id, "country_code": self.country_code,
        }


def company_today(company: Company) -> date:
    return utcnow().astimezone(pytz.timezone(company.timezone or "UTC")).date()


def _company(db: Session, company_id: int) -> Company:
    company = db.get(Company, company_id)
    if company is None:
        raise not_found("Company")
    return company


def _get(db: Session, model: Any, company_id: int, object_id: int, what: str) -> Any:
    row = db.query(model).filter(model.company_id == company_id, model.id == object_id).first()
    if row is None:
        raise not_found(what)
    return row


def primary_assignment(db: Session, company_id: int, employee_id: int, on: date) -> EmployeeAssignment | None:
    return (
        db.query(EmployeeAssignment)
        .filter(
            EmployeeAssignment.company_id == company_id,
            EmployeeAssignment.employee_id == employee_id,
            EmployeeAssignment.is_primary.is_(True),
            EmployeeAssignment.effective_from <= on,
            or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= on),
        )
        .first()
    )


def build_context(
    db: Session,
    company_id: int,
    *,
    on: date,
    employee_id: int | None = None,
    pay_group_id: int | None = None,
    branch_id: int | None = None,
    legal_entity_id: int | None = None,
) -> Context:
    """The scope chain on ``on``; with only an employee, the employee's primary assignment that day."""
    company = _company(db, company_id)
    if employee_id is not None:
        _get(db, Employee, company_id, employee_id, "Employee")
        if legal_entity_id is None and branch_id is None and pay_group_id is None:
            placement = primary_assignment(db, company_id, employee_id, on)
            if placement is not None:
                legal_entity_id, branch_id = placement.legal_entity_id, placement.branch_id
                pay_group_id = placement.pay_group_id
    pay_group = _get(db, PayGroup, company_id, pay_group_id, "Pay group") if pay_group_id is not None else None
    branch = _get(db, Branch, company_id, branch_id, "Branch") if branch_id is not None else None
    for parent in (pay_group, branch):
        if parent is None:
            continue
        if legal_entity_id is None:
            legal_entity_id = parent.legal_entity_id
        elif parent.legal_entity_id != legal_entity_id:
            raise bad_request("The branch or pay group belongs to another legal entity.", "SCOPE_MISMATCH")
    entity = _get(db, LegalEntity, company_id, legal_entity_id, "Legal entity") if legal_entity_id is not None else None
    # Payslip languages default to the tenant's locale, else the entity's, else English.
    locale = company.default_locale or (entity.default_locale if entity is not None else None) or "en"
    facts: dict[str, Any] = {"tenant_locale": locale, "tenant_currency": company.currency}
    currency = pay_group.currency if pay_group is not None else (entity.base_currency if entity is not None else None)
    if entity is not None:
        facts["entity_base_currency"] = entity.base_currency
        # Same rule as calendars.banking_calendar_id: the entity country's national holiday calendar.
        national = (db.query(HolidayCalendar.id)
                    .filter(HolidayCalendar.company_id == company_id,
                            HolidayCalendar.country_code == entity.country_code,
                            HolidayCalendar.subdivision_code.is_(None),
                            HolidayCalendar.parent_calendar_id.is_(None),
                            HolidayCalendar.status == "ACTIVE")
                    .order_by(HolidayCalendar.id).first())
        if national is not None:
            facts["country_banking_calendar_id"] = national[0]
    if currency:
        info = currencies.find(currency)
        facts["currency_minor_unit"] = getattr(info, "minor_unit", None)
    if branch is not None:
        facts["branch_holiday_calendar_id"] = branch.holiday_calendar_id
    return Context(
        company_id=company_id, on=on, legal_entity_id=legal_entity_id, branch_id=branch_id,
        pay_group_id=pay_group_id, employee_id=employee_id,
        country_code=entity.country_code if entity is not None else None,
        partner_id=company.partner_id, facts={k: v for k, v in facts.items() if v is not None},
    )


# --------------------------------------------------------------------------- loading


def _scope_clause(ctx: Context):
    clauses = [and_(SettingValue.scope_type == Level.TENANT.value, SettingValue.scope_id.is_(None))]
    for level in (Level.LEGAL_ENTITY, Level.BRANCH, Level.PAY_GROUP, Level.EMPLOYEE):
        scope_id = ctx.scope_id(level)
        if scope_id is not None:
            clauses.append(and_(SettingValue.scope_type == level.value, SettingValue.scope_id == scope_id))
    return or_(*clauses)


def _covering(column_from, column_to, on: date):
    return and_(column_from <= on, or_(column_to.is_(None), column_to >= on))


def _tenant_rows(db: Session, ctx: Context, keys: Sequence[str]) -> dict[str, dict[Level, SettingValue]]:
    rows = (
        db.query(SettingValue)
        .filter(
            SettingValue.company_id == ctx.company_id,
            SettingValue.key.in_(keys),
            SettingValue.status == "ACTIVE",
            _covering(SettingValue.effective_from, SettingValue.effective_to, ctx.on),
            _scope_clause(ctx),
        )
        .all()
    )
    found: dict[str, dict[Level, SettingValue]] = {}
    for row in rows:
        found.setdefault(row.key, {})[Level(row.scope_type)] = row
    return found


def _platform_rows(db: Session, ctx: Context, keys: Sequence[str]) -> dict[str, dict[Level, PlatformSettingValue]]:
    levels = [PlatformSettingValue.level == Level.PLATFORM.value]
    if ctx.partner_id is not None:
        levels.append(and_(PlatformSettingValue.level == Level.PARTNER.value,
                           PlatformSettingValue.partner_id == ctx.partner_id))
    rows = (
        db.query(PlatformSettingValue)
        .filter(
            PlatformSettingValue.key.in_(keys),
            _covering(PlatformSettingValue.effective_from, PlatformSettingValue.effective_to, ctx.on),
            or_(*levels),
        )
        .all()
    )
    found: dict[str, dict[Level, PlatformSettingValue]] = {}
    for row in rows:
        found.setdefault(row.key, {})[Level(row.level)] = row
    return found


# --------------------------------------------------------------------------- resolution


@dataclass(frozen=True)
class Resolved:
    """The effective value of one key, where it came from and what it shadows."""

    key: str
    value: Any
    level: str | None
    row_id: int | None
    locked_by: str | None
    legal_limit: tuple[dict[str, Any], ...] | None
    effective_from: date | None
    effective_to: date | None
    shadowed: tuple[dict[str, Any], ...] = ()
    issues: tuple[Issue, ...] = ()
    source: str | None = None
    raw: Any = None

    @property
    def ok(self) -> bool:
        return self.value is not UNSET and not any(i.severity == "ERROR" for i in self.issues)

    @property
    def json_value(self) -> Any:
        return value_json(self.key, self.value)


def value_json(key: str, value: Any) -> Any:
    if value is UNSET or isinstance(value, Derived):
        return None
    try:
        return reg.to_json(key, value)
    except SettingValidationError:
        return value


def _issue_json(issue: Issue) -> dict[str, Any]:
    return {"rule": issue.rule, "severity": issue.severity, "keys": list(issue.keys), "message": issue.message,
            "details": dict(issue.details), "blocks_save": issue.blocks_save}


def _constraint_json(constraint: reg.LegalConstraint) -> dict[str, Any]:
    def show(value: Any) -> Any:
        return reg.decimal_str(value) if hasattr(value, "is_finite") else value

    found: dict[str, Any] = {"target": constraint.target, "source": constraint.source}
    if constraint.minimum is not None:
        found["minimum"] = show(constraint.minimum)
    if constraint.maximum is not None:
        found["maximum"] = show(constraint.maximum)
    if constraint.allowed is not None:
        found["allowed"] = [show(a) for a in constraint.allowed]
    if constraint.required_value is not UNSET:
        found["required_value"] = show(constraint.required_value)
    if constraint.applies_to:
        found["applies_to"] = constraint.applies_to
    return found


def _resolve_loaded(
    ctx: Context,
    key: str,
    tenant_rows: Mapping[Level, SettingValue],
    platform_rows: Mapping[Level, PlatformSettingValue],
    constraints: Sequence[reg.LegalConstraint],
) -> Resolved:
    defn = reg.REGISTRY[key]
    rows: dict[Level, Any] = {**platform_rows, **tenant_rows}
    candidates = [
        reg.Candidate(level, row.value, bool(row.locked), getattr(row, "scope_id", None) or getattr(row, "partner_id", None))
        for level, row in rows.items()
    ]
    limits = [c for c in constraints if c.target == key]
    resolution = reg.resolve_chain(candidates, key=key, legal_limits=limits, derive_context=dict(ctx.facts))
    winner = rows.get(resolution.level) if resolution.level is not None and not resolution.legal_source else None
    if resolution.legal_source:
        source = "COUNTRY_PACK"
    elif isinstance(winner, SettingValue):
        source = winner.source
    elif isinstance(winner, PlatformSettingValue):
        source = winner.level
    else:
        source = "REGISTRY_DEFAULT"
    value = resolution.value
    if value is UNSET and not defn.fails_closed:
        value = None if isinstance(defn.default, Derived) else defn.default
    shadowed = tuple(
        {
            "level": s.candidate.level.value,
            "value": value_json(key, s.candidate.value) if not isinstance(s.candidate.value, Derived) else None,
            "reason": s.reason,
            "by": s.by.value if s.by is not None else None,
            "row_id": getattr(rows.get(s.candidate.level), "id", None),
        }
        for s in resolution.shadowed
    )
    return Resolved(
        key=key,
        value=value,
        level=resolution.level.value if resolution.level is not None else None,
        row_id=getattr(winner, "id", None),
        locked_by=resolution.level.value if resolution.locked and resolution.level is not None else None,
        legal_limit=tuple(_constraint_json(c) for c in limits) or None,
        effective_from=getattr(winner, "effective_from", None),
        effective_to=getattr(winner, "effective_to", None),
        shadowed=shadowed,
        issues=resolution.issues,
        source=source,
        raw=getattr(winner, "value", None) if winner is not None else None,
    )


def _check(resolved: Resolved, ctx: Context) -> Resolved:
    """Fail closed for payroll keys; other keys keep today's warning and default."""
    errors = [i for i in resolved.issues if i.severity == "ERROR"]
    if reg.REGISTRY[resolved.key].fails_closed:
        if errors or resolved.value is UNSET:
            raise SettingConfigError(resolved.key, errors, context=ctx.as_json())
        return resolved
    for issue in resolved.issues:
        logger.warning("Setting %s for company %s: %s", resolved.key, ctx.company_id, issue.message)
    return resolved


def resolve_in_context(db: Session, ctx: Context, keys: Sequence[str], *, strict: bool = True) -> dict[str, Resolved]:
    """Resolve canonical ``keys`` in a prepared context (one query per table)."""
    canonical = list(dict.fromkeys(keys))
    tenant = _tenant_rows(db, ctx, canonical)
    platform = _platform_rows(db, ctx, canonical)
    constraints = pack_constraints(ctx.country_code, ctx.on)
    result: dict[str, Resolved] = {}
    for key in canonical:
        resolved = _resolve_loaded(ctx, key, tenant.get(key, {}), platform.get(key, {}), constraints)
        result[key] = _check(resolved, ctx) if strict else resolved
    return result


def resolve(
    db: Session,
    company_id: int,
    key: str,
    *,
    employee_id: int | None = None,
    pay_group_id: int | None = None,
    branch_id: int | None = None,
    legal_entity_id: int | None = None,
    on: date,
) -> Resolved:
    """The effective value of ``key`` (or a legacy alias) on ``on``; raises SettingConfigError for invalid payroll values."""
    canonical = reg.canonical_key(key)
    ctx = build_context(db, company_id, on=on, employee_id=employee_id, pay_group_id=pay_group_id,
                        branch_id=branch_id, legal_entity_id=legal_entity_id)
    return resolve_in_context(db, ctx, [canonical])[canonical]


def resolve_many(
    db: Session,
    company_id: int,
    keys: Iterable[str],
    *,
    employee_id: int | None = None,
    pay_group_id: int | None = None,
    branch_id: int | None = None,
    legal_entity_id: int | None = None,
    on: date,
    strict: bool = True,
) -> dict[str, Resolved]:
    """``resolve`` for several keys at once, keyed by the names given (aliases included)."""
    names = list(dict.fromkeys(keys))
    canonical = {name: reg.canonical_key(name) for name in names}
    ctx = build_context(db, company_id, on=on, employee_id=employee_id, pay_group_id=pay_group_id,
                        branch_id=branch_id, legal_entity_id=legal_entity_id)
    resolved = resolve_in_context(db, ctx, list(canonical.values()), strict=strict)
    return {name: resolved[key] for name, key in canonical.items()}


def resolve_ranges(
    db: Session,
    company_id: int,
    key: str,
    *,
    employee_id: int | None = None,
    pay_group_id: int | None = None,
    branch_id: int | None = None,
    legal_entity_id: int | None = None,
    start: date,
    end: date,
) -> list[tuple[date, date, Any]]:
    """``[(from, to, value)]`` covering ``start..end`` with day precision (EC-CFG-02, EC-CFG-08, EC-CFG-18).

    DAY-grain keys change on the day a value (or the employee's placement) changes; PERIOD-grain
    keys take the value effective on ``end`` for the whole range. Adjacent equal values merge.
    """
    if end < start:
        raise ValueError("end is before start")
    canonical = reg.canonical_key(key)
    scope = {"employee_id": employee_id, "pay_group_id": pay_group_id, "branch_id": branch_id,
             "legal_entity_id": legal_entity_id}
    if reg.REGISTRY[canonical].resolution_grain == "PERIOD":
        return [(start, end, resolve(db, company_id, canonical, on=end, **scope).value)]
    boundaries = {start}

    def add(day: date | None) -> None:
        if day is not None and start < day <= end:
            boundaries.add(day)

    for row in db.query(SettingValue.effective_from, SettingValue.effective_to).filter(
        SettingValue.company_id == company_id, SettingValue.key == canonical, SettingValue.status == "ACTIVE",
        SettingValue.effective_from <= end, or_(SettingValue.effective_to.is_(None), SettingValue.effective_to >= start),
    ):
        add(row.effective_from)
        add(row.effective_to + _ONE_DAY if row.effective_to else None)
    for row in db.query(PlatformSettingValue.effective_from, PlatformSettingValue.effective_to).filter(
        PlatformSettingValue.key == canonical, PlatformSettingValue.effective_from <= end,
        or_(PlatformSettingValue.effective_to.is_(None), PlatformSettingValue.effective_to >= start),
    ):
        add(row.effective_from)
        add(row.effective_to + _ONE_DAY if row.effective_to else None)
    if employee_id is not None and legal_entity_id is None and branch_id is None and pay_group_id is None:
        for row in db.query(EmployeeAssignment.effective_from, EmployeeAssignment.effective_to).filter(
            EmployeeAssignment.company_id == company_id, EmployeeAssignment.employee_id == employee_id,
            EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.effective_from <= end,
            or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= start),
        ):
            add(row.effective_from)
            add(row.effective_to + _ONE_DAY if row.effective_to else None)
    days = sorted(boundaries)
    segments: list[tuple[date, date, Any]] = []
    for index, day in enumerate(days):
        until = days[index + 1] - _ONE_DAY if index + 1 < len(days) else end
        value = resolve(db, company_id, canonical, on=day, **scope).value
        if segments and segments[-1][2] == value and type(segments[-1][2]) is type(value):
            segments[-1] = (segments[-1][0], until, value)
        else:
            segments.append((day, until, value))
    return segments


def _users(db: Session, company_id: int, ids: Iterable[int | None]) -> dict[int, User]:
    wanted = sorted({i for i in ids if i is not None})
    if not wanted:
        return {}
    return {u.id: u for u in db.query(User).filter(User.company_id == company_id, User.id.in_(wanted))}


def _person(users: Mapping[int, User], user_id: int | None) -> dict[str, Any] | None:
    user = users.get(user_id) if user_id is not None else None
    return {"id": user.id, "name": user.full_name, "email": user.email} if user is not None else None


def explain(
    db: Session,
    company_id: int,
    key: str,
    *,
    employee_id: int | None = None,
    pay_group_id: int | None = None,
    branch_id: int | None = None,
    legal_entity_id: int | None = None,
    on: date,
) -> dict[str, Any]:
    """Every level's candidate for ``key`` and why the effective value won (02 §14.5)."""
    canonical = reg.canonical_key(key)
    defn = reg.REGISTRY[canonical]
    ctx = build_context(db, company_id, on=on, employee_id=employee_id, pay_group_id=pay_group_id,
                        branch_id=branch_id, legal_entity_id=legal_entity_id)
    tenant = _tenant_rows(db, ctx, [canonical]).get(canonical, {})
    platform = _platform_rows(db, ctx, [canonical]).get(canonical, {})
    constraints = [c for c in pack_constraints(ctx.country_code, on) if c.target == canonical]
    resolved = _resolve_loaded(ctx, canonical, tenant, platform, constraints)
    pending = {
        Level(r.scope_type): r for r in db.query(SettingValue).filter(
            SettingValue.company_id == company_id, SettingValue.key == canonical,
            SettingValue.status == "PENDING_APPROVAL", _scope_clause(ctx),
        )
    }
    users = _users(db, company_id, [r.created_by_user_id for r in tenant.values()]
                   + [r.approved_by_user_id for r in tenant.values()])
    shadow = {s["level"]: s for s in resolved.shadowed}
    levels: list[dict[str, Any]] = []
    for level in reg.PRECEDENCE:
        row = tenant.get(level) or platform.get(level)
        entry: dict[str, Any] = {
            "level": level.value,
            "allowed": level not in reg.SCOPE_LEVELS or level in defn.allowed_levels,
            "scope_id": ctx.scope_id(level) if level in reg.SCOPE_LEVELS and level is not Level.TENANT else None,
            "value": None, "locked": False, "row_id": None, "source": None,
            "effective_from": None, "effective_to": None, "reason": None, "set_by": None, "set_at": None,
        }
        if row is not None:
            entry.update(
                value=row.value, locked=bool(row.locked), row_id=row.id,
                source=getattr(row, "source", row.level if isinstance(row, PlatformSettingValue) else None),
                effective_from=row.effective_from.isoformat(),
                effective_to=row.effective_to.isoformat() if row.effective_to else None,
                reason=getattr(row, "reason", None),
                set_by=_person(users, getattr(row, "created_by_user_id", None)) or getattr(row, "created_by", None),
                set_at=row.created_at.isoformat() if row.created_at else None,
            )
        elif level is Level.PLATFORM:
            entry.update(value=value_json(canonical, defn.default), source="REGISTRY_DEFAULT")
            if isinstance(defn.default, Derived):
                entry["derived"] = {"source": defn.default.source, "description": defn.default.description}
        if level is Level.COUNTRY_PACK:
            entry["country_code"] = ctx.country_code
            entry["constraints"] = [_constraint_json(c) for c in constraints]
        if level.value == resolved.level:
            entry["status"] = "EFFECTIVE"
        elif level.value in shadow:
            entry["status"] = shadow[level.value]["reason"]
        elif row is not None or (level is Level.PLATFORM):
            entry["status"] = "NOT_REACHED"
        else:
            entry["status"] = "NOT_ALLOWED" if not entry["allowed"] else "EMPTY"
        if level in pending:
            p = pending[level]
            entry["pending"] = {"row_id": p.id, "value": p.value, "effective_from": p.effective_from.isoformat()}
        levels.append(entry)
    return {
        "key": canonical,
        "label": defn.label,
        "module": defn.module,
        "resolution_grain": defn.resolution_grain,
        "context": ctx.as_json(),
        "value": resolved.json_value,
        "level": resolved.level,
        "locked_by": resolved.locked_by,
        "legal_limit": list(resolved.legal_limit or ()),
        "valid": resolved.ok,
        "issues": [_issue_json(i) for i in resolved.issues],
        "levels": levels,
    }


# --------------------------------------------------------------------------- scopes and permissions


@dataclass(frozen=True)
class ScopeTarget:
    level: Level
    scope_id: int | None
    name: str
    legal_entity_id: int | None = None
    branch_id: int | None = None
    pay_group_id: int | None = None
    employee_id: int | None = None

    def context(self, db: Session, company_id: int, on: date) -> Context:
        if self.level is Level.EMPLOYEE:
            return build_context(db, company_id, on=on, employee_id=self.employee_id)
        return build_context(db, company_id, on=on, legal_entity_id=self.legal_entity_id,
                             branch_id=self.branch_id, pay_group_id=self.pay_group_id)

    def as_json(self) -> dict[str, Any]:
        return {"scope_type": self.level.value, "scope_id": self.scope_id, "name": self.name}


def covers(principal: Principal, code: str, *, legal_entity_id: int | None = None,
           pay_group_id: int | None = None) -> bool:
    """Whether ``code`` is held at COMPANY scope or targets the legal entity or pay group."""
    if principal.has(code, "COMPANY"):
        return True
    if legal_entity_id is not None and legal_entity_id in principal.scope_targets(code, "LEGAL_ENTITY"):
        return True
    return pay_group_id is not None and pay_group_id in principal.scope_targets(code, "PAY_GROUP")


def scope_target(db: Session, principal: Principal, code: str, scope_type: str, scope_id: int | None,
                 *, on: date | None = None) -> ScopeTarget:
    """The scope object, if the caller's grant of ``code`` covers it; otherwise 404 (403 for TENANT writes)."""
    if scope_type not in _SCOPE_TYPES:
        raise bad_request(f"scope_type must be one of {', '.join(_SCOPE_TYPES)}.", "INVALID_SCOPE")
    level = Level(scope_type)
    cid = principal.company_id
    if level is Level.TENANT:
        if scope_id is not None:
            raise bad_request("A TENANT scope has no scope_id.", "INVALID_SCOPE")
        if code == WRITE and not principal.has(code, "COMPANY"):
            raise forbidden("Company-level payroll settings need company-wide payroll configuration access.")
        return ScopeTarget(level, None, "Company")
    if scope_id is None:
        raise bad_request(f"A {scope_type} scope needs a scope_id.", "INVALID_SCOPE")
    if level is Level.LEGAL_ENTITY:
        entity = _get(db, LegalEntity, cid, scope_id, "Legal entity")
        target = ScopeTarget(level, scope_id, entity.legal_name, legal_entity_id=entity.id)
    elif level is Level.BRANCH:
        branch = _get(db, Branch, cid, scope_id, "Branch")
        target = ScopeTarget(level, scope_id, branch.name, legal_entity_id=branch.legal_entity_id, branch_id=branch.id)
    elif level is Level.PAY_GROUP:
        group = _get(db, PayGroup, cid, scope_id, "Pay group")
        target = ScopeTarget(level, scope_id, group.name, legal_entity_id=group.legal_entity_id, pay_group_id=group.id)
    else:
        employee = _get(db, Employee, cid, scope_id, "Employee")
        placement = primary_assignment(db, cid, employee.id, on or company_today(_company(db, cid)))
        target = ScopeTarget(
            level, scope_id, f"{employee.first_name} {employee.last_name}".strip(), employee_id=employee.id,
            legal_entity_id=placement.legal_entity_id if placement else None,
            pay_group_id=placement.pay_group_id if placement else None,
        )
    # EC-CFG-13: an employee-level override needs scope over that employee's entity or pay group.
    if not covers(principal, code, legal_entity_id=target.legal_entity_id, pay_group_id=target.pay_group_id):
        raise not_found({Level.LEGAL_ENTITY: "Legal entity", Level.BRANCH: "Branch", Level.PAY_GROUP: "Pay group",
                         Level.EMPLOYEE: "Employee"}[level])
    return target


def _countries(db: Session, company_id: int, target: ScopeTarget, ctx: Context) -> tuple[str, ...]:
    """Countries whose packs constrain a value saved at ``target`` (TENANT: every legal entity's)."""
    if target.level is Level.TENANT:
        rows = db.query(LegalEntity.country_code).filter(LegalEntity.company_id == company_id).distinct()
        return tuple(sorted({code for (code,) in rows if code}))
    return (ctx.country_code,) if ctx.country_code else ()


def _scope_currencies(db: Session, company_id: int, target: ScopeTarget) -> list[str]:
    entities = db.query(LegalEntity.base_currency).filter(LegalEntity.company_id == company_id)
    groups = db.query(PayGroup.currency).filter(PayGroup.company_id == company_id)
    if target.pay_group_id is not None:
        groups = groups.filter(PayGroup.id == target.pay_group_id)
        entities = entities.filter(false())
    elif target.legal_entity_id is not None:
        entities = entities.filter(LegalEntity.id == target.legal_entity_id)
        groups = groups.filter(PayGroup.legal_entity_id == target.legal_entity_id)
    return sorted({c for (c,) in [*entities, *groups] if c})


def _scope_filter(level: Level, scope_id: int | None):
    return and_(SettingValue.scope_type == level.value,
                SettingValue.scope_id.is_(None) if scope_id is None else SettingValue.scope_id == scope_id)


def _scope_tokens(db: Session, company_id: int, keys: Sequence[str], level: Level, scope_id: int | None) -> dict[str, int]:
    """Concurrency token per key at one scope: the highest row_version of any of its rows (0 when none)."""
    rows = (
        db.query(SettingValue.key, func.max(SettingValue.row_version))
        .filter(SettingValue.company_id == company_id, SettingValue.key.in_(keys), _scope_filter(level, scope_id))
        .group_by(SettingValue.key)
    )
    return {key: int(version) for key, version in rows}


def _open_requests(db: Session, company_id: int, keys: Sequence[str], level: Level, scope_id: int | None
                   ) -> dict[str, tuple[SettingValue, SettingChangeRequest]]:
    rows = (
        db.query(SettingValue, SettingChangeRequest)
        .join(SettingChangeRequest, and_(SettingChangeRequest.setting_value_id == SettingValue.id,
                                         SettingChangeRequest.company_id == SettingValue.company_id))
        .filter(SettingValue.company_id == company_id, SettingValue.key.in_(keys), _scope_filter(level, scope_id),
                SettingValue.status == "PENDING_APPROVAL", SettingChangeRequest.decision.is_(None))
    )
    return {value.key: (value, request) for value, request in rows}


# --------------------------------------------------------------------------- values at a scope


_BADGE_LABELS = {
    "REQUIRED_BY_LAW": "Required by law",
    "OVERRIDDEN_HERE": "Overridden here",
    "SET_HERE": "Set here",
}


def _badge(resolved: Resolved, level: Level) -> dict[str, Any]:
    if resolved.source == "COUNTRY_PACK":
        sources = [c["source"] for c in resolved.legal_limit or () if "required_value" in c]
        return {"code": "REQUIRED_BY_LAW", "label": "Required by law", "source": sources[0] if sources else None}
    if resolved.level == level.value:
        code = "SET_HERE" if level is Level.TENANT else "OVERRIDDEN_HERE"
        return {"code": code, "label": _BADGE_LABELS[code], "level": level.value}
    winner = Level(resolved.level) if resolved.level else Level.PLATFORM
    name = _LEVEL_NAMES[winner]
    if resolved.locked_by:
        return {"code": "LOCKED", "label": f"Locked by {name}", "level": winner.value}
    return {"code": "INHERITED", "label": f"Inherited from {name}", "level": winner.value}


def _active_policies(db: Session, company_id: int, on: date) -> list[tuple[str, str, Mapping[str, Any]]]:
    rows = (
        db.query(Policy.type, Policy.name, PolicyVersion.fields)
        .join(PolicyVersion, and_(PolicyVersion.policy_id == Policy.id, PolicyVersion.company_id == Policy.company_id))
        .filter(Policy.company_id == company_id, Policy.status == "ACTIVE", PolicyVersion.status == "ACTIVE",
                _covering(PolicyVersion.effective_from, PolicyVersion.effective_to, on))
        .order_by(Policy.type, Policy.name)
    )
    return [(t, n, f) for t, n, f in rows]


def _validation_values(resolved: Mapping[str, Resolved]) -> dict[str, Any]:
    """Values for cross_validate: JSON of valid values, the stored raw value of invalid ones."""
    values: dict[str, Any] = {}
    for key, res in resolved.items():
        if res.value is UNSET:
            if res.raw is not None:
                values[key] = res.raw
        elif isinstance(res.value, Derived):
            values[key] = res.value
        else:
            values[key] = value_json(key, res.value)
    return values


def values_at_scope(
    db: Session,
    principal: Principal,
    scope_type: str,
    scope_id: int | None,
    as_of: date | None = None,
    *,
    module: str | None = None,
    section: str | None = None,
) -> dict[str, Any]:
    """Every setting as seen at one scope on ``as_of``, with inheritance badges (3.1 §8, 02 §14.5)."""
    company = _company(db, principal.company_id)
    on = as_of or company_today(company)
    target = scope_target(db, principal, READ, scope_type, scope_id, on=on)
    ctx = target.context(db, company.id, on)
    keys = list(reg.REGISTRY)
    resolved = resolve_in_context(db, ctx, keys, strict=False)
    here_rows = {k: rows[target.level] for k, rows in _tenant_rows(db, ctx, keys).items() if target.level in rows}
    tokens = _scope_tokens(db, company.id, keys, target.level, target.scope_id)
    pending = _open_requests(db, company.id, keys, target.level, target.scope_id)
    shown = {d.key for d in reg.definitions(module=module, section=section)}
    items: list[dict[str, Any]] = []
    for defn in reg.definitions():
        if defn.key not in shown:
            continue
        res = resolved[defn.key]
        here = here_rows.get(defn.key)
        item: dict[str, Any] = {
            "key": defn.key,
            "value": res.json_value,
            "level": res.level,
            "badge": _badge(res, target.level),
            "locked_by": res.locked_by,
            "legal_limit": list(res.legal_limit or ()),
            "effective_from": res.effective_from.isoformat() if res.effective_from else None,
            "effective_to": res.effective_to.isoformat() if res.effective_to else None,
            "editable": target.level in defn.allowed_levels,
            "row_version": tokens.get(defn.key, 0),
            "here": None,
            "shadowed": list(res.shadowed),
            "valid": res.ok,
            "issues": [_issue_json(i) for i in res.issues],
            "pending": None,
        }
        if isinstance(defn.default, Derived) and res.value is None:
            item["derived"] = {"source": defn.default.source, "description": defn.default.description}
        if here is not None:
            item["here"] = {"row_id": here.id, "value": here.value, "locked": bool(here.locked),
                            "effective_from": here.effective_from.isoformat(),
                            "effective_to": here.effective_to.isoformat() if here.effective_to else None,
                            "source": here.source, "profile_version": here.profile_version, "reason": here.reason}
        if defn.key in pending:
            row, request = pending[defn.key]
            item["pending"] = {"change_request_id": request.id, "value": row.value, "locked": bool(row.locked),
                               "effective_from": row.effective_from.isoformat(),
                               "requested_by_user_id": request.requested_by_user_id}
        items.append(item)
    issues = reg.cross_validate(
        _validation_values(resolved), pack_constraints(ctx.country_code, on),
        policies=_active_policies(db, company.id, on), currencies=_scope_currencies(db, company.id, target),
    )
    return {
        "scope": target.as_json(),
        "as_of": on.isoformat(),
        "registry_version": reg.REGISTRY_VERSION,
        "profile_version": company.payroll_profile_version,
        "items": items,
        "issues": [_issue_json(i) for i in issues],
    }


# --------------------------------------------------------------------------- writes


@dataclass(frozen=True)
class SettingChange:
    """One requested change of a batch (PUT /payroll/settings/values)."""

    key: str
    value: Any
    scope_type: str = "TENANT"
    scope_id: int | None = None
    effective_from: date | None = None
    locked: bool | None = None
    reason: str | None = None
    row_version: int | None = None


@dataclass
class _Planned:
    change: SettingChange
    key: str
    defn: reg.SettingDef
    target: ScopeTarget
    effective_from: date
    value_json: Any
    locked: bool
    old: Resolved
    token: int
    countries: tuple[str, ...]
    status: str = "ACTIVE"


def _period_notice(defn: reg.SettingDef, effective_from: date) -> str:
    start = effective_from.replace(day=1)
    end = effective_from.replace(day=calendar.monthrange(effective_from.year, effective_from.month)[1])
    notice = reg.effective_date_notice(defn.resolution_grain, effective_from, start, end,
                                       period_label=f"{effective_from:%B %Y}")
    return f"{notice}." if notice else f"Applies from {effective_from.isoformat()}."


def _validation_error(error: SettingValidationError) -> AppError:
    status = 403 if error.code == "ADMIN_ONLY" else 400
    detail: Any = error.message
    if error.details:
        detail = {"message": error.message, "key": error.key, "details": error.details}
    return AppError(status, detail, error.code)


def _stale(planned_key: str, target: ScopeTarget, current: Resolved, token: int, message: str,
           code: str = "STALE_ROW_VERSION") -> AppError:
    return AppError(409, {
        "message": message,
        "key": planned_key,
        "scope": target.as_json(),
        "current": {"value": current.json_value, "level": current.level, "locked_by": current.locked_by,
                    "row_version": token,
                    "effective_from": current.effective_from.isoformat() if current.effective_from else None},
    }, code)


def _same(key: str, left: Any, right: Any) -> bool:
    try:
        return reg.validate_value(key, left) == reg.validate_value(key, right)
    except SettingValidationError:
        return False


def _plan(db: Session, principal: Principal, company: Company, change: SettingChange, today: date) -> _Planned:
    try:
        key = reg.canonical_key(change.key)
    except SettingValidationError as error:
        raise bad_request(error.message, error.code) from None
    defn = reg.REGISTRY[key]
    if defn.ui.get("system_written"):  # e.g. attendance.v2.enabled: only the cut-over writes it (spine §15.1)
        raise AppError(409, {"message": f"{defn.label} is set by the system and cannot be changed here."},
                       "SETTING_SYSTEM_WRITTEN")
    target = scope_target(db, principal, WRITE, change.scope_type, change.scope_id, on=today)
    if defn.module != reg.PAYROLL_MODULE and not principal.has(COMPANY_SETTINGS):
        raise forbidden(f"Changing {defn.label} also needs the company settings permission.")
    effective_from = change.effective_from or today
    if not defn.effective_dated:
        effective_from = today
    if change.reason is not None and len(change.reason) > 500:
        raise bad_request("The reason is at most 500 characters.", "REASON_TOO_LONG")
    ctx = target.context(db, company.id, effective_from)
    rows = _tenant_rows(db, ctx, [key]).get(key, {})
    platform = _platform_rows(db, ctx, [key]).get(key, {})
    countries = _countries(db, company.id, target, ctx)
    limits = [c for code in countries for c in pack_constraints(code, effective_from) if c.target == key]
    current = _resolve_loaded(ctx, key, rows, platform, limits)
    here = rows.get(target.level)
    current_locked = bool(here.locked) if here is not None else False
    locked = current_locked if change.locked is None else change.locked
    try:
        normalized = reg.validate_write(
            key, change.value, level=target.level, locked=locked, actor_is_admin=principal.is_company_admin,
            current=current.raw if here is not None else current.json_value, current_locked=current_locked,
            legal_limits=limits,
        )
    except SettingValidationError as error:
        raise _validation_error(error) from None
    value = reg.to_json(key, normalized)
    token = _scope_tokens(db, company.id, [key], target.level, target.scope_id).get(key, 0)
    if (change.row_version or 0) != token:
        raise _stale(key, target, current, token,
                     f"{defn.label} was changed by someone else. Review the current value and save again.")
    if key in _open_requests(db, company.id, [key], target.level, target.scope_id):
        raise _stale(key, target, current, token,
                     f"A change of {defn.label} here is waiting for approval.", code="CHANGE_PENDING")
    planned = _Planned(change, key, defn, target, effective_from, value, locked, current, token, countries)
    later = _later_rows(db, company.id, key, target, effective_from)
    if here is not None:
        unchanged = _same(key, here.value, value) and current_locked == locked and not later
    else:
        unchanged = not locked and current.value is not UNSET and _same(key, current.json_value, value) and not later
    if unchanged:
        planned.status = "UNCHANGED"
        return planned
    broader = reg.PRECEDENCE[reg.PRECEDENCE.index(target.level) + 1:]
    lock = next((lv for lv in broader if (rows.get(lv) or platform.get(lv)) is not None
                 and (rows.get(lv) or platform.get(lv)).locked), None)
    if lock is not None:
        raise bad_request(f"{defn.label} is locked by the {_LEVEL_NAMES[lock]} and cannot be overridden here.",
                          "SETTING_LOCKED")
    if effective_from < today and not (change.reason or "").strip():
        raise bad_request(f"{defn.label}: a reason is required for a change effective in the past.", "REASON_REQUIRED")
    if finalized_period_overlaps(db, company.id, key, target.level, target.scope_id, effective_from) \
            and not (change.reason or "").strip():
        raise bad_request(f"{defn.label}: a reason is required for a change in a finalized period.", "REASON_REQUIRED")
    return planned


def _later_rows(db: Session, company_id: int, key: str, target: ScopeTarget, effective_from: date) -> list[SettingValue]:
    return (
        db.query(SettingValue)
        .filter(SettingValue.company_id == company_id, SettingValue.key == key,
                _scope_filter(target.level, target.scope_id), SettingValue.status == "ACTIVE",
                SettingValue.effective_from > effective_from)
        .all()
    )


def _relevant(issue: Issue, keys: set[str]) -> bool:
    from app.payroll.policy_types import POLICY_TYPES

    expanded: set[str] = set()
    for name in issue.keys:
        expanded.add(name)
        head = name.split(".", 1)[0]
        if head in POLICY_TYPES:
            expanded.update(POLICY_TYPES[head].gate_switches)
    return bool(expanded & keys)


def _cross_check(db: Session, company: Company, plans: Sequence[_Planned]) -> list[Issue]:
    """V-01..V-16 on the would-be values of each changed scope and date; blocking errors raise 400."""
    warnings: list[Issue] = []
    groups: dict[tuple[str, int | None, date], list[_Planned]] = {}
    for plan in plans:
        groups.setdefault((plan.target.level.value, plan.target.scope_id, plan.effective_from), []).append(plan)
    for (level, scope_id, day), group in groups.items():
        target = group[0].target
        ctx = target.context(db, company.id, day)
        resolved = resolve_in_context(db, ctx, list(reg.REGISTRY), strict=False)
        values = _validation_values(resolved)
        changed: set[str] = set()
        for plan in plans:
            if plan.target.level.value == level and plan.target.scope_id == scope_id and plan.effective_from <= day:
                values[plan.key] = plan.value_json
                changed.add(plan.key)
        constraints = [c for code in group[0].countries for c in pack_constraints(code, day)]
        issues = reg.cross_validate(values, constraints, policies=_active_policies(db, company.id, day),
                                    currencies=_scope_currencies(db, company.id, target))
        blocking = [i for i in issues if i.blocks_save and _relevant(i, changed)]
        if blocking:
            raise AppError(400, {"message": blocking[0].message, "scope": target.as_json(),
                                 "issues": [_issue_json(i) for i in blocking]}, "SETTINGS_INVALID")
        warnings.extend(i for i in issues if i.severity == "WARNING" and _relevant(i, changed))
    return warnings


def _activate(db: Session, company_id: int, row: SettingValue, token: int, today: date) -> None:
    """Close or supersede the ACTIVE rows that ``row`` replaces, then make ``row`` ACTIVE.

    A row starting before ``row`` ends the day before it; rows starting from ``row``'s date up to
    today are superseded (a backdated value applies from its date on); a row scheduled after both
    stays and bounds ``row``.
    """
    existing = (
        db.query(SettingValue)
        .filter(SettingValue.company_id == company_id, SettingValue.key == row.key,
                _scope_filter(Level(row.scope_type), row.scope_id), SettingValue.status == "ACTIVE",
                or_(SettingValue.effective_to.is_(None), SettingValue.effective_to >= row.effective_from),
                SettingValue.id != (row.id or 0))
        .with_for_update()
        .all()
    )
    version = token + 1
    horizon = max(row.effective_from, today)
    later = [r.effective_from for r in existing if r.effective_from > horizon]
    for old in existing:
        if old.effective_from < row.effective_from:
            old.effective_to = row.effective_from - _ONE_DAY
            old.row_version = version
        elif old.effective_from <= horizon:
            old.status = "SUPERSEDED"
            old.row_version = version
    db.flush()
    row.effective_to = min(later) - _ONE_DAY if later else None
    row.status = "ACTIVE"
    row.row_version = version
    if row.id is None:
        db.add(row)
    db.flush()


def _scope_text(target: ScopeTarget) -> str:
    return "company level" if target.level is Level.TENANT else f"{_LEVEL_NAMES[target.level]} {target.name}"


def _show(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _audit_module(defn: reg.SettingDef) -> str:
    return payroll_audit.PAYROLL_SETTINGS if defn.module == reg.PAYROLL_MODULE else SETTINGS_MODULE


def _audit_change(db: Session, principal: Principal | None, action: str, defn: reg.SettingDef, target: ScopeTarget,
                  old: Any, new: Any, row: SettingValue, *, reason: str | None, extra: Mapping[str, Any] | None = None,
                  company_id: int | None = None) -> None:
    verb = {SETTING_CHANGED: "Changed", SETTING_CHANGE_REQUESTED: "Requested a change of",
            SETTING_CHANGE_APPROVED: "Approved the change of", SETTING_CHANGE_REJECTED: "Rejected the change of"}[action]
    AuditLogService.log(
        db, principal, _audit_module(defn), action,
        f"{verb} {defn.label} at {_scope_text(target)} from {_show(old)} to {_show(new)}, "
        f"effective {row.effective_from.isoformat()}",
        "setting_value", row.id, company_id=company_id,
        actor_type="user" if principal is not None else "system",
        details={"key": defn.key, "scope": target.as_json(), "old": old, "new": new, "locked": bool(row.locked),
                 "effective_from": row.effective_from, "reason": reason, "source": row.source,
                 "profile_version": row.profile_version, **(extra or {})},
    )


def _change_approval_enabled(db: Session, company_id: int, today: date) -> bool:
    ctx = build_context(db, company_id, on=today)
    resolved = resolve_in_context(db, ctx, [CHANGE_APPROVAL_KEY], strict=False)[CHANGE_APPROVAL_KEY]
    return resolved.value is True


def _write(db: Session, principal: Principal, company: Company, plan: _Planned, approval: bool) -> dict[str, Any]:
    defn, target = plan.defn, plan.target
    result: dict[str, Any] = {
        "key": plan.key, "scope": target.as_json(), "effective_from": plan.effective_from.isoformat(),
        "resolution_grain": defn.resolution_grain,
    }
    notice = _period_notice(defn, plan.effective_from)
    if plan.status == "UNCHANGED":
        result.update(status="UNCHANGED", row_id=plan.old.row_id, row_version=plan.token,
                      message=f"{defn.label} is unchanged.")
        return result
    row = SettingValue(
        company_id=company.id, key=plan.key, scope_type=target.level.value, scope_id=target.scope_id,
        value=plan.value_json, locked=plan.locked, effective_from=plan.effective_from,
        reason=(plan.change.reason or "").strip() or None, created_by_user_id=principal.user_id, source="ADMIN",
    )
    old = plan.old.json_value
    if approval and defn.requires_approval:
        row.status = "PENDING_APPROVAL"
        row.row_version = plan.token + 1
        db.add(row)
        db.flush()
        request = SettingChangeRequest(company_id=company.id, setting_value_id=row.id,
                                       requested_by_user_id=principal.user_id)
        db.add(request)
        db.flush()
        _audit_change(db, principal, SETTING_CHANGE_REQUESTED, defn, target, old, plan.value_json, row,
                      reason=row.reason, extra={"change_request_id": request.id})
        result.update(status="PENDING_APPROVAL", row_id=row.id, row_version=row.row_version,
                      change_request_id=request.id,
                      message=f"Waiting for approval by another administrator; the current value stays in force "
                              f"until then. {notice}")
        return result
    _activate(db, company.id, row, plan.token, company_today(company))
    _audit_change(db, principal, SETTING_CHANGED, defn, target, old, plan.value_json, row, reason=row.reason)
    mark_runs_stale(db, company.id, [plan.key], target.level, target.scope_id, plan.effective_from)
    result.update(status="ACTIVE", row_id=row.id, row_version=row.row_version, message=notice)
    return result


def save_values(db: Session, principal: Principal, changes: Sequence[SettingChange]) -> dict[str, Any]:
    """Validate and save a batch of changes atomically (3.1 §2.3, §2.5, §9); the route commits."""
    if not changes:
        raise bad_request("Send at least one change.", "EMPTY_BATCH")
    if len(changes) > MAX_BATCH:
        raise bad_request(f"At most {MAX_BATCH} changes per request.", "BATCH_TOO_LARGE")
    company = _company(db, principal.company_id)
    today = company_today(company)
    plans = [_plan(db, principal, company, change, today) for change in changes]
    seen: set[tuple[str, str, int | None]] = set()
    for plan in plans:
        ident = (plan.key, plan.target.level.value, plan.target.scope_id)
        if ident in seen:
            raise bad_request(f"{plan.defn.label} is changed twice at the same scope.", "DUPLICATE_CHANGE")
        seen.add(ident)
    warnings = _cross_check(db, company, [p for p in plans if p.status != "UNCHANGED"])
    approval = _change_approval_enabled(db, company.id, today)
    results = [_write(db, principal, company, plan, approval) for plan in plans]
    _attendance_recompute(db, company.id, plans)
    return {"changes": results, "warnings": [_issue_json(i) for i in warnings]}


def _attendance_recompute(db: Session, company_id: int, plans: Sequence[_Planned]) -> None:
    """AS-M3 (§8.1 SETTING_CHANGED): a changed attendance evaluation key re-evaluates OPEN payroll periods from its
    effective date (v2 tenants; the cut-over flag itself is not an evaluation input)."""
    changed = [p for p in plans if p.key.startswith("attendance.") and p.key != "attendance.v2.enabled"
               and p.status != "UNCHANGED"]
    if not changed:
        return
    from app.services import attendance_days

    attendance_days.schedule_config_recompute(
        db, company_id, trigger="SETTING_CHANGED", source="settings:" + ",".join(sorted({p.key for p in changed})),
        effective_from=min(p.effective_from for p in changed))


def write_tenant_value(db: Session, principal: Principal, key: str, value: Any, *, reason: str | None = None
                       ) -> dict[str, Any]:
    """Legacy facade write (app.services.settings_service): TENANT level from today, with change control.

    The caller has already checked its own permission (settings:manage) and the admin-only rule;
    concurrency is last-write-wins as in the legacy screen. Returns the change result.
    """
    return write_module_value(db, principal, key, value, reason=reason)


def write_module_value(db: Session, principal: Principal, key: str, value: Any, *, reason: str | None = None,
                       legal_entity_id: int | None = None) -> dict[str, Any]:
    """A module's own write of one of its keys (TENANT, or LEGAL_ENTITY when ``legal_entity_id`` is given) from
    today, with change control and audit. The calling module has already checked its own permission (e.g.
    ``attendance:manage_policy`` for ``attendance.packs.enabled``), so payroll configuration access is not needed."""
    company = _company(db, principal.company_id)
    today = company_today(company)
    canonical = reg.canonical_key(key)
    defn = reg.REGISTRY[canonical]
    if legal_entity_id is None:
        level, target = Level.TENANT, ScopeTarget(Level.TENANT, None, "Company")
    else:
        entity = _get(db, LegalEntity, company.id, legal_entity_id, "Legal entity")
        level = Level.LEGAL_ENTITY
        target = ScopeTarget(level, entity.id, entity.legal_name, legal_entity_id=entity.id)
    ctx = build_context(db, company.id, on=today, legal_entity_id=legal_entity_id)
    rows = _tenant_rows(db, ctx, [canonical]).get(canonical, {})
    platform = _platform_rows(db, ctx, [canonical]).get(canonical, {})
    countries = _countries(db, company.id, target, ctx)
    limits = [c for code in countries for c in pack_constraints(code, today) if c.target == canonical]
    current = _resolve_loaded(ctx, canonical, rows, platform, limits)
    here = rows.get(level)
    locked = bool(here.locked) if here is not None else False
    normalized = reg.validate_write(canonical, value, level=level, locked=locked, actor_is_admin=True,
                                    legal_limits=limits)
    token = _scope_tokens(db, company.id, [canonical], level, target.scope_id).get(canonical, 0)
    if canonical in _open_requests(db, company.id, [canonical], level, target.scope_id):
        raise bad_request(f"A change of {defn.label} is waiting for approval.", "CHANGE_PENDING")
    change = SettingChange(canonical, value, scope_type=level.value, scope_id=target.scope_id, reason=reason,
                           row_version=token)
    plan = _Planned(change, canonical, defn, target, today, reg.to_json(canonical, normalized), locked, current,
                    token, countries)
    if here is not None and _same(canonical, here.value, plan.value_json)             and not _later_rows(db, company.id, canonical, target, today):
        plan.status = "UNCHANGED"
    return _write(db, principal, company, plan, _change_approval_enabled(db, company.id, today))


# --------------------------------------------------------------------------- maker-checker


def _load_request(db: Session, principal: Principal, request_id: int) -> tuple[SettingChangeRequest, SettingValue]:
    request = (
        db.query(SettingChangeRequest)
        .filter(SettingChangeRequest.company_id == principal.company_id, SettingChangeRequest.id == request_id)
        .with_for_update()
        .first()
    )
    if request is None:
        raise not_found("Change request")
    row = _get(db, SettingValue, principal.company_id, request.setting_value_id, "Change request")
    return request, row


def _request_json(db: Session, request: SettingChangeRequest, row: SettingValue, users: Mapping[int, User],
                  current: Any) -> dict[str, Any]:
    defn = reg.REGISTRY.get(row.key)
    return {
        "id": request.id,
        "key": row.key,
        "label": defn.label if defn else row.key,
        "scope": {"scope_type": row.scope_type, "scope_id": row.scope_id},
        "value": row.value,
        "current_value": current,
        "locked": bool(row.locked),
        "effective_from": row.effective_from.isoformat(),
        "reason": row.reason,
        "status": request.decision or "PENDING",
        "requested_by": _person(users, request.requested_by_user_id),
        "requested_at": request.created_at.isoformat() if request.created_at else None,
        "decided_by": _person(users, request.decided_by_user_id),
        "decided_at": request.decided_at.isoformat() if request.decided_at else None,
        "comment": request.comment,
    }


def list_change_requests(db: Session, principal: Principal, *, status: str = "PENDING") -> list[dict[str, Any]]:
    if status not in ("PENDING", "APPROVED", "REJECTED", "ALL"):
        raise bad_request("status must be PENDING, APPROVED, REJECTED or ALL.", "INVALID_STATUS")
    query = (
        db.query(SettingChangeRequest, SettingValue)
        .join(SettingValue, and_(SettingValue.id == SettingChangeRequest.setting_value_id,
                                 SettingValue.company_id == SettingChangeRequest.company_id))
        .filter(SettingChangeRequest.company_id == principal.company_id)
    )
    if status == "PENDING":
        query = query.filter(SettingChangeRequest.decision.is_(None))
    elif status != "ALL":
        query = query.filter(SettingChangeRequest.decision == status)
    pairs = query.order_by(SettingChangeRequest.id.desc()).limit(500).all()
    company = _company(db, principal.company_id)
    today = company_today(company)
    users = _users(db, principal.company_id, [r.requested_by_user_id for r, _ in pairs]
                   + [r.decided_by_user_id for r, _ in pairs])
    found = []
    for request, row in pairs:
        level = Level(row.scope_type)
        le = pg = None
        if level is Level.LEGAL_ENTITY:
            le = row.scope_id
        elif level is Level.PAY_GROUP:
            group = db.query(PayGroup).filter(PayGroup.company_id == principal.company_id, PayGroup.id == row.scope_id).first()
            pg, le = row.scope_id, group.legal_entity_id if group else None
        elif level is Level.BRANCH:
            branch = db.query(Branch).filter(Branch.company_id == principal.company_id, Branch.id == row.scope_id).first()
            le = branch.legal_entity_id if branch else None
        elif level is Level.EMPLOYEE:
            placement = primary_assignment(db, principal.company_id, row.scope_id, today)
            le, pg = (placement.legal_entity_id, placement.pay_group_id) if placement else (None, None)
        if level is not Level.TENANT and not (covers(principal, APPROVE, legal_entity_id=le, pay_group_id=pg)
                                              or covers(principal, READ, legal_entity_id=le, pay_group_id=pg)):
            continue
        current = None
        if row.key in reg.REGISTRY:
            target = _target_from_row(db, principal.company_id, row, today)
            ctx = target.context(db, principal.company_id, row.effective_from)
            current = resolve_in_context(db, ctx, [row.key], strict=False)[row.key].json_value
        found.append(_request_json(db, request, row, users, current))
    return found


def _target_from_row(db: Session, company_id: int, row: SettingValue, today: date) -> ScopeTarget:
    level = Level(row.scope_type)
    if level is Level.TENANT:
        return ScopeTarget(level, None, "Company")
    if level is Level.LEGAL_ENTITY:
        entity = _get(db, LegalEntity, company_id, row.scope_id, "Legal entity")
        return ScopeTarget(level, row.scope_id, entity.legal_name, legal_entity_id=entity.id)
    if level is Level.BRANCH:
        branch = _get(db, Branch, company_id, row.scope_id, "Branch")
        return ScopeTarget(level, row.scope_id, branch.name, legal_entity_id=branch.legal_entity_id, branch_id=branch.id)
    if level is Level.PAY_GROUP:
        group = _get(db, PayGroup, company_id, row.scope_id, "Pay group")
        return ScopeTarget(level, row.scope_id, group.name, legal_entity_id=group.legal_entity_id, pay_group_id=group.id)
    employee = _get(db, Employee, company_id, row.scope_id, "Employee")
    return ScopeTarget(level, row.scope_id, f"{employee.first_name} {employee.last_name}".strip(),
                       employee_id=employee.id)


def decide_change_request(db: Session, principal: Principal, request_id: int, *, approve: bool,
                          comment: str | None = None) -> dict[str, Any]:
    """Approve or reject a pending change; the maker can withdraw (reject) but never approve (EC-CFG-12)."""
    if comment is not None and len(comment) > 500:
        raise bad_request("The comment is at most 500 characters.", "COMMENT_TOO_LONG")
    request, row = _load_request(db, principal, request_id)
    company = _company(db, principal.company_id)
    today = company_today(company)
    target = _target_from_row(db, company.id, row, today)
    if target.level is not Level.TENANT:
        scope_target(db, principal, APPROVE, target.level.value, target.scope_id, on=today)
    if request.decision is not None or row.status != "PENDING_APPROVAL":
        raise AppError(409, "This change request has already been decided.", "ALREADY_DECIDED")
    defn = reg.REGISTRY.get(row.key)
    if defn is None:
        raise bad_request(f"{row.key} is no longer a registered setting.", "UNKNOWN_SETTING")
    if approve and request.requested_by_user_id == principal.user_id:
        raise AppError(403, "You cannot approve your own change; another administrator must approve it.",
                       "MAKER_CHECKER")
    ctx = target.context(db, company.id, row.effective_from)
    current = resolve_in_context(db, ctx, [row.key], strict=False)[row.key]
    now = utcnow()
    request.decided_by_user_id = principal.user_id
    request.decided_at = now
    request.comment = (comment or "").strip() or None
    if not approve:
        request.decision = "REJECTED"
        row.status = "REJECTED"
        db.flush()
        _audit_change(db, principal, SETTING_CHANGE_REJECTED, defn, target, current.json_value, row.value, row,
                      reason=row.reason, extra={"change_request_id": request.id, "comment": request.comment})
        return {"id": request.id, "status": "REJECTED", "row_id": row.id}
    try:
        reg.validate_value(row.key, row.value)
    except SettingValidationError as error:
        raise _validation_error(error) from None
    token = _scope_tokens(db, company.id, [row.key], target.level, target.scope_id).get(row.key, 0)
    request.decision = "APPROVED"
    row.approved_by_user_id = principal.user_id
    row.approved_at = now
    _activate(db, company.id, row, token, today)
    _audit_change(db, principal, SETTING_CHANGE_APPROVED, defn, target, current.json_value, row.value, row,
                  reason=row.reason, extra={"change_request_id": request.id, "comment": request.comment})
    _audit_change(db, principal, SETTING_CHANGED, defn, target, current.json_value, row.value, row,
                  reason=row.reason, extra={"change_request_id": request.id,
                                            "requested_by_user_id": request.requested_by_user_id,
                                            "approved_by_user_id": principal.user_id})
    mark_runs_stale(db, company.id, [row.key], target.level, target.scope_id, row.effective_from)
    return {"id": request.id, "status": "APPROVED", "row_id": row.id, "row_version": row.row_version,
            "message": _period_notice(defn, row.effective_from)}


# --------------------------------------------------------------------------- profiles (materialization)


def materialize_row(db: Session, company_id: int, *, key: str, value: Any, locked: bool, effective_from: date,
                    source: str, profile_version: str, reason: str) -> SettingValue:
    """Write one TENANT row of a configuration profile (no permission checks; used by profiles)."""
    token = _scope_tokens(db, company_id, [key], Level.TENANT, None).get(key, 0)
    row = SettingValue(company_id=company_id, key=key, scope_type=Level.TENANT.value, scope_id=None, value=value,
                       locked=locked, effective_from=effective_from, reason=reason, source=source,
                       profile_version=profile_version)
    _activate(db, company_id, row, token, effective_from)
    return row

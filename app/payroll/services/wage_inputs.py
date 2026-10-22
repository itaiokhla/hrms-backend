"""Wage inputs: bonus, commission, incentives, arrears and adjustments for one pay period (3.4 §13, 3.1 §5.9).

* An input is an amount, or units (x an optional rate), of a component whose method is INPUT or
  units-based (RATE_X_UNITS), for one natural period of the employee's pay group. The currency is the
  pay group's unless the input carries an FX rule (converted at calculation with the run's FX
  snapshot). Statuses follow the WAGE_INPUT machine: DRAFT -> PENDING_APPROVAL -> APPROVED ->
  CONSUMED, or REJECTED / CANCELLED. An input at or below payroll.wage_inputs.require_approval_above
  (and the component's own threshold) is approved on submission; anything else, a negative amount,
  a units input or an amount in another currency than the threshold goes through the approvals
  engine (module wage_input). A recurring input is one input per period, approved together.
* The target period must accept inputs (EC-VAR-03). Pay periods are created by the runs milestone
  (migration 0019); until then the target is a date range that must be one natural period of the
  pay group calendar, and ``period_is_open`` is a hook that returns True. The runs milestone replaces
  its body with the pay_periods lookup; a closed period is then refused with the next open period
  suggested.
* external_ref is unique per employee, component and period among live inputs (EC-VAR-02); an
  input with the same amount as another live input of the employee for the period is flagged
  POSSIBLE_DUPLICATE for the approver.
* CSV import: dry-run validates every row (unknown employee, out of scope, closed period, currency
  mismatch, negative amount or units where not allowed, duplicates, formula-like text) and nothing is
  committed while any row is bad (EC-VAR-16); the commit runs as a background job. Cells are only
  ever read as text; the result file escapes formula-like cells.
* Exactly once: run lock reserves approved inputs (``reserve_inputs``), finalization consumes them
  (``consume_inputs``) and a cancelled or reopened run releases its reservations (``release_inputs``)
  (EC-VAR-01). ``approved_inputs`` is the read API of the snapshot builder.

The module also holds the small helpers the claims and loans services share (placement, scope,
transitions). Services never commit.
"""

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal, build_principal
from app.core import storage
from app.models.company import Company
from app.models.compensation import PayComponent, PayComponentVersion
from app.models.employee import Employee
from app.models.org import EmployeeAssignment
from app.models.pay_org import PayGroup
from app.models.user import User
from app.models.variable_inputs import LIVE_WAGE_INPUT_STATUSES, WageInput, WageInputImport
from app.payroll import audit as payroll_audit
from app.payroll import calendar as paycal
from app.payroll import currencies
from app.payroll import state_machines as sm
from app.payroll.money import amount_str, decimal_str, is_storable, to_decimal
from app.payroll.money_errors import MoneyError
from app.payroll.services import access, approvals, jobs
from app.payroll.services import settings as settings_engine
from app.services.audit_log_service import AuditLogService

READ = "payroll:inputs:read"
EDIT = "payroll:inputs:edit"
IMPORT = "payroll:inputs:import"
APPROVE = "payroll:inputs:approve"
MODULE = "wage_input"
SUBJECT = "wage_input"
AUDIT_MODULE = payroll_audit.PAYROLL_INPUTS
IMPORT_JOB = "wage_inputs.import_commit"

INPUT_METHODS = ("INPUT",)
UNITS_METHODS = ("RATE_X_UNITS",)
MAX_RECURRENCE = 24
MAX_IMPORT_ROWS = 5000
MAX_IMPORT_BYTES = 2 * 1024 * 1024
IMPORT_COLUMNS = ("employee_code", "component_code", "amount", "units", "rate", "currency", "period_start",
                  "period_end", "external_ref", "reason")
REQUIRED_COLUMNS = ("employee_code", "component_code", "period_start", "period_end")
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"


# ---------------------------------------------------------------------------
# Shared helpers (wage inputs, claims, loans)
# ---------------------------------------------------------------------------
def company_today(db: Session, company_id: int) -> date:
    return clock.company_today(db, company_id)


@dataclass(frozen=True)
class Placement:
    employee: Employee
    legal_entity_id: int
    pay_group: PayGroup

    @property
    def pay_group_id(self) -> int:
        return self.pay_group.id

    @property
    def currency(self) -> str:
        return self.pay_group.currency


def load_employee(db: Session, company_id: int, employee_id: int) -> Employee:
    employee = db.query(Employee).filter(Employee.company_id == company_id, Employee.id == employee_id).first()
    if employee is None:
        raise not_found("Employee")
    return employee


def placement(db: Session, company_id: int, employee_id: int, on: date) -> Placement:
    """The employee's primary placement and pay group on `on` (409 NO_PAY_GROUP without one)."""
    employee = load_employee(db, company_id, employee_id)
    assignment: Optional[EmployeeAssignment] = settings_engine.primary_assignment(db, company_id, employee_id, on)
    if assignment is None:
        raise conflict(f"The employee has no placement on {on.isoformat()}.", "NOT_PLACED")
    if assignment.pay_group_id is None:
        raise conflict(f"The employee is in no pay group on {on.isoformat()}.", "NO_PAY_GROUP")
    group = db.query(PayGroup).filter(PayGroup.company_id == company_id, PayGroup.id == assignment.pay_group_id).one()
    return Placement(employee, assignment.legal_entity_id, group)


def scoped_placement(db: Session, principal: Principal, permission: str, employee_id: int, on: date, *,
                     allow_self: bool = False) -> Placement:
    """``placement`` of an employee the caller may act on (404 otherwise, also when the employee is not
    placed, so callers without company scope learn nothing about employees outside their scope)."""
    if not principal.has(permission):
        raise forbidden()
    own = allow_self and principal.employee_id == employee_id
    try:
        place = placement(db, principal.company_id, employee_id, on)
    except AppError as exc:
        if exc.status_code == 409 and not (own or principal.has_company_scope(permission)):
            raise not_found("Employee")
        raise
    require_covers(principal, permission, place.legal_entity_id, place.pay_group_id,
                   employee_id if allow_self else None, "Employee")
    return place


def covers(principal: Principal, permission: str, legal_entity_id: int, pay_group_id: int,
           employee_id: Optional[int]) -> bool:
    """The permission reaches the record: COMPANY, its entity or pay group, or (SELF) the caller's own."""
    if not principal.has(permission):
        return False
    if access.target_scope(principal, permission).covers(legal_entity_id, pay_group_id):
        return True
    return "SELF" in principal.scopes(permission) and employee_id is not None and principal.employee_id == employee_id


def require_covers(principal: Principal, permission: str, legal_entity_id: int, pay_group_id: int,
                   employee_id: Optional[int], what: str) -> None:
    """403 without the permission; 404 when the record lies outside the caller's scope."""
    if not principal.has(permission):
        raise forbidden()
    if not covers(principal, permission, legal_entity_id, pay_group_id, employee_id):
        raise not_found(what)


def employee_refs(db: Session, company_id: int, employee_ids: Iterable[int]) -> Dict[int, dict]:
    """{employee_id: {"employee_name", "employee_code"}} for the given employees of the company. Callers pass
    only the employees of records they already returned through their scope (F-44), so a name never
    reaches anyone who cannot see the record."""
    ids = sorted({i for i in employee_ids if i is not None})
    if not ids:
        return {}
    rows = db.query(Employee.id, Employee.first_name, Employee.last_name, Employee.employee_code).filter(
        Employee.company_id == company_id, Employee.id.in_(ids))
    return {i: {"employee_name": f"{first or ''} {last or ''}".strip(), "employee_code": code}
            for i, first, last, code in rows}


def with_employee_refs(db: Session, company_id: int, items: List[dict], key: str = "employee_id") -> List[dict]:
    """Adds employee_name and employee_code next to `key` in each serialized record (in place)."""
    refs = employee_refs(db, company_id, (item.get(key) for item in items))
    for item in items:
        ref = refs.get(item.get(key)) or {"employee_name": None, "employee_code": None}
        item.update(ref)
    return items


def scope_clause(principal: Principal, permission: str, model):
    """SQL filter for lists and counts: the same rule as ``covers``."""
    return access.run_employee_clause(principal, permission, model.legal_entity_id, model.pay_group_id,
                                      model.employee_id)


def transition(machine: sm.Machine, state: str, action: str) -> sm.TransitionSpec:
    """The machine's transition, or 409 with the machine's code."""
    try:
        return sm.transition_spec(machine, state, action)
    except sm.InvalidTransition as exc:
        raise conflict(exc.message, exc.code)


def require_transition(principal: Optional[Principal], machine: sm.Machine, state: str, action: str) -> str:
    """Target state of a user transition; 403 without the transition's permission."""
    spec = transition(machine, state, action)
    if principal is not None and spec.permission is not None and not principal.has(spec.permission):
        raise forbidden()
    return spec.target


def resolve_setting(db: Session, company_id: int, key: str, *, on: date, pay_group_id: Optional[int] = None,
                    legal_entity_id: Optional[int] = None, employee_id: Optional[int] = None):
    try:
        return settings_engine.resolve(db, company_id, key, on=on, pay_group_id=pay_group_id,
                                       legal_entity_id=legal_entity_id, employee_id=employee_id).value
    except settings_engine.SettingConfigError as exc:
        raise conflict(exc.message, exc.code)


def audit(db: Session, principal: Optional[Principal], module: str, action: str, description: str,
          entity_type: str, entity_id: int, company_id: int, **details: Any) -> None:
    AuditLogService.log(
        db, principal, module, action, description[:500], entity_type, entity_id, company_id=company_id,
        actor_type="user" if principal is not None else "system", details=details,
    )


def money_json(amount: Optional[Decimal], currency: str) -> Optional[dict]:
    if amount is None:
        return None
    return {"amount": amount_str(Decimal(amount), currency), "currency": currency}


def principal_for_user(db: Session, company_id: int, user_id: Optional[int]) -> Optional[Principal]:
    """The live principal of a user (grants as of now), or None if the user is gone or inactive."""
    if user_id is None:
        return None
    user = db.query(User).filter(User.company_id == company_id, User.id == user_id).first()
    if user is None or user.status != "active":
        return None
    return build_principal(db, user, None, db.get(Company, company_id))


# ---------------------------------------------------------------------------
# Components (app.models.compensation, 3.3 §2)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ComponentRef:
    id: int
    code: str
    calc_method: str
    allow_negative: bool = False
    approval_above: Optional[Decimal] = None
    approval_currency: Optional[str] = None

    @property
    def units_based(self) -> bool:
        return self.calc_method in UNITS_METHODS


ComponentResolver = Callable[[Session, int, str, date], Optional[ComponentRef]]


def catalogue_component(db: Session, company_id: int, code: str, on: date) -> Optional[ComponentRef]:
    """The ACTIVE version in force on `on` of the tenant's ACTIVE component `code`.

    A version's params may set allow_negative_input (clawbacks, EC-VAR-12); input_approval_above is
    the component's own approval threshold in input_approval_currency.
    """
    row = (
        db.query(PayComponent.id, PayComponent.code, PayComponentVersion.calc_method, PayComponentVersion.params,
                 PayComponentVersion.input_approval_above, PayComponentVersion.input_approval_currency)
        .join(PayComponentVersion, and_(PayComponentVersion.company_id == PayComponent.company_id,
                                        PayComponentVersion.component_id == PayComponent.id))
        .filter(
            PayComponent.company_id == company_id, PayComponent.code == code, PayComponent.status == "ACTIVE",
            PayComponentVersion.status == "ACTIVE", PayComponentVersion.effective_from <= on,
            or_(PayComponentVersion.effective_to.is_(None), PayComponentVersion.effective_to >= on),
        )
        .order_by(PayComponentVersion.effective_from.desc())
        .first()
    )
    if row is None:
        return None
    params = row.params if isinstance(row.params, Mapping) else {}
    threshold = Decimal(row.input_approval_above) if row.input_approval_above is not None else None
    return ComponentRef(row.id, row.code, str(row.calc_method), params.get("allow_negative_input") is True,
                        threshold, row.input_approval_currency)


_component_resolver: ComponentResolver = catalogue_component


def set_component_resolver(resolver: Optional[ComponentResolver]) -> ComponentResolver:
    """Replace the component lookup (None restores the catalogue); returns the previous resolver."""
    global _component_resolver
    previous = _component_resolver
    _component_resolver = resolver or catalogue_component
    return previous


def component(db: Session, company_id: int, code: str, on: date) -> ComponentRef:
    found = _component_resolver(db, company_id, code, on)
    if found is None:
        raise bad_request(f"Component {code} does not exist or is not active on {on.isoformat()}.",
                          "COMPONENT_NOT_FOUND")
    if found.calc_method not in INPUT_METHODS + UNITS_METHODS:
        raise bad_request(f"Component {code} is calculated by {found.calc_method}; wage inputs need an INPUT or "
                          "units-based component.", "COMPONENT_NOT_INPUT")
    return found


# ---------------------------------------------------------------------------
# Target periods
# ---------------------------------------------------------------------------
def period_is_open(db: Session, company_id: int, pay_group_id: int, start: date, end: date) -> bool:
    """Whether the pay period [start, end] of the pay group accepts inputs (EC-VAR-03).

    Hook until pay periods exist (migration 0019, runs milestone): every period is open. The runs
    milestone replaces this body with ``state_machines.accepts_inputs(period.status)`` of the
    pay_periods row (OPEN and PROCESSING accept inputs, CLOSED does not).
    """
    return True


_BOUND_FIELDS = ("monthly_start_day", "semi_monthly_split_day", "week_start_day", "biweekly_anchor_date")


def _calendar(group: PayGroup) -> Tuple[str, paycal.CalendarRules]:
    raw = dict(group.calendar_rules or {})
    fields: Dict[str, Any] = {}
    for key, value in raw.items():
        name = paycal.SETTING_FIELDS.get(key, key)
        if name in _BOUND_FIELDS and value is not None:
            fields[name] = value
    if isinstance(fields.get("biweekly_anchor_date"), str):
        fields["biweekly_anchor_date"] = date.fromisoformat(fields["biweekly_anchor_date"])
    try:
        rules = paycal.CalendarRules(banking_calendar=paycal.BankingCalendar(frozenset({5, 6})),
                                     tax_year_start="01-01", **fields)
        paycal.validate_rules(group.pay_frequency, rules)
    except (paycal.CalendarError, TypeError, ValueError) as exc:
        raise conflict(f"The calendar of pay group {group.code} is invalid: {exc}", "PAY_CALENDAR_INVALID")
    return group.pay_frequency, rules


def natural_periods(group: PayGroup, from_date: date, until: date) -> List[Tuple[date, date]]:
    frequency, rules = _calendar(group)
    return [(p.start, p.end) for p in paycal.generate_periods(frequency, rules, from_date, until)]


def next_open_period(db: Session, company_id: int, group: PayGroup, after: date) -> Optional[Tuple[date, date]]:
    for start, end in natural_periods(group, after + timedelta(days=1), after + timedelta(days=400)):
        if start > after and period_is_open(db, company_id, group.id, start, end):
            return start, end
    return None


def _period_text(period: Optional[Tuple[date, date]]) -> str:
    return f"{period[0].isoformat()} to {period[1].isoformat()}" if period else "none"


def check_target_period(db: Session, company_id: int, group: PayGroup, start: date, end: date) -> None:
    """The range is one natural period of the pay group and accepts inputs (EC-VAR-03)."""
    natural = natural_periods(group, start, start)
    if not natural or natural[0] != (start, end):
        expected = natural[0] if natural else None
        raise bad_request(f"{start.isoformat()} to {end.isoformat()} is not a pay period of {group.code}; "
                          f"the period containing {start.isoformat()} is {_period_text(expected)}.",
                          "TARGET_PERIOD_INVALID")
    if not period_is_open(db, company_id, group.id, start, end):
        suggestion = next_open_period(db, company_id, group, end)
        raise conflict(f"The period {_period_text((start, end))} is closed; corrections go through retro. "
                       f"Next open period: {_period_text(suggestion)}.", "PERIOD_CLOSED")


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
def serialize(row: WageInput) -> dict:
    return {
        "id": row.id,
        "employee_id": row.employee_id,
        "legal_entity_id": row.legal_entity_id,
        "pay_group_id": row.pay_group_id,
        "component_id": row.component_id,
        "component_code": row.component_code,
        "amount": money_json(row.amount, row.currency),
        "units": decimal_str(Decimal(row.units)) if row.units is not None else None,
        "rate": decimal_str(Decimal(row.rate)) if row.rate is not None else None,
        "currency": row.currency,
        "fx_rule": row.fx_rule,
        "target_period": {"id": row.target_period_id, "start": row.target_period_start.isoformat(),
                          "end": row.target_period_end.isoformat()},
        "reason": row.reason,
        "source": row.source,
        "status": row.status,
        "recurrence": row.recurrence,
        "recurrence_parent_id": row.recurrence_parent_id,
        "external_ref": row.external_ref,
        "import_id": row.import_id,
        "flags": list(row.flags or []),
        "approval_request_id": row.approval_request_id,
        "reserved_by_run_employee_id": row.reserved_by_run_employee_id,
        "consumed_by_run_employee_id": row.consumed_by_run_employee_id,
        "created_by_user_id": row.created_by_user_id,
        "submitted_at": row.submitted_at,
        "decided_at": row.decided_at,
        "status_reason": row.status_reason,
        "row_version": row.row_version,
        "created_at": row.created_at,
    }


def version_hash(row: WageInput) -> str:
    """Content hash for change detection against run snapshots (3.4 §16)."""
    canonical = "|".join(str(v) for v in (
        row.id, row.status, row.component_id, row.amount, row.units, row.rate, row.currency,
        row.target_period_start, row.target_period_end, row.row_version,
    ))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Create, edit, submit
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class InputData:
    employee_id: int
    component_code: str
    period_start: date
    period_end: date
    amount: Optional[Decimal] = None
    units: Optional[Decimal] = None
    rate: Optional[Decimal] = None
    currency: Optional[str] = None
    convert_currency: bool = False
    reason: Optional[str] = None
    external_ref: Optional[str] = None
    recurrence_until: Optional[date] = None
    source: str = "MANUAL"


@dataclass
class Checked:
    data: InputData
    placement: Placement
    component: ComponentRef
    currency: str
    fx_rule: Optional[dict]
    periods: List[Tuple[date, date]]


def _check_values(data: InputData, comp: ComponentRef, place: Placement) -> Tuple[str, Optional[dict]]:
    if (data.amount is None) == (data.units is None):
        raise bad_request("Give either an amount or units.", "AMOUNT_OR_UNITS")
    if data.units is not None:
        if not comp.units_based:
            raise bad_request(f"Component {comp.code} takes amounts, not units.", "UNITS_NOT_ALLOWED")
        if data.units <= 0:
            raise bad_request("Units must be greater than zero.", "UNITS_NOT_POSITIVE")
        if data.rate is not None and data.rate < 0:
            raise bad_request("The rate cannot be negative.", "RATE_NEGATIVE")
    else:
        if comp.units_based:
            raise bad_request(f"Component {comp.code} is units-based: give units (and optionally a rate).",
                              "UNITS_REQUIRED")
        if data.rate is not None:
            raise bad_request("A rate goes with units only.", "RATE_WITHOUT_UNITS")
        if data.amount == 0:
            raise bad_request("The amount cannot be zero.", "AMOUNT_ZERO")
        if data.amount < 0 and not comp.allow_negative:
            raise bad_request(f"Component {comp.code} does not take negative amounts.", "AMOUNT_NEGATIVE")
    currency = data.currency or place.currency
    try:
        currency = currencies.require_payroll_currency(currency).code
        if data.amount is not None:
            amount_str(data.amount, currency)
            if not is_storable(data.amount):
                raise MoneyError("AMOUNT_NOT_STORABLE", "The amount is too large to store.")
    except MoneyError as exc:
        raise bad_request(exc.message, exc.code)
    fx_rule = None
    if currency != place.currency:
        if not data.convert_currency:
            raise bad_request(f"The pay group {place.pay_group.code} pays in {place.currency}; an input in {currency} "
                              "needs convert_currency (converted with the run's FX snapshot).", "CURRENCY_MISMATCH")
        fx_rule = {"to": place.currency, "rate_date_rule": "RUN_SNAPSHOT"}
    return currency, fx_rule


def check_input(db: Session, principal: Principal, data: InputData, *, permission: str = EDIT) -> Checked:
    """Every validation of one input (and its recurrence), without writing anything."""
    company_id = principal.company_id
    place = scoped_placement(db, principal, permission, data.employee_id, data.period_start)
    if not resolve_setting(db, company_id, "payroll.wage_inputs.enabled", on=data.period_start,
                           pay_group_id=place.pay_group_id):
        raise conflict("Wage inputs are switched off for this pay group.", "WAGE_INPUTS_DISABLED")
    comp = component(db, company_id, data.component_code, data.period_start)
    currency, fx_rule = _check_values(data, comp, place)
    check_target_period(db, company_id, place.pay_group, data.period_start, data.period_end)
    periods = [(data.period_start, data.period_end)]
    if data.recurrence_until is not None:
        if data.recurrence_until < data.period_end:
            raise bad_request("The recurrence must end after the first period.", "RECURRENCE_INVALID")
        periods = [p for p in natural_periods(place.pay_group, data.period_start, data.recurrence_until)
                   if p[0] >= data.period_start and p[1] <= data.recurrence_until]
        if len(periods) > MAX_RECURRENCE:
            raise bad_request(f"A recurrence covers at most {MAX_RECURRENCE} periods.", "RECURRENCE_TOO_LONG")
        for start, end in periods[1:]:
            later = placement(db, company_id, data.employee_id, start)
            if later.pay_group_id != place.pay_group_id:
                raise conflict(f"The employee changes pay group before {start.isoformat()}; end the recurrence "
                               "there.", "RECURRENCE_CROSSES_PAY_GROUP")
            if not period_is_open(db, company_id, place.pay_group_id, start, end):
                raise conflict(f"The period {_period_text((start, end))} is closed.", "PERIOD_CLOSED")
    if data.external_ref:
        for start, _ in periods:
            if _external_ref_taken(db, company_id, data.employee_id, comp.id, start, data.external_ref):
                raise conflict(f"External reference {data.external_ref} is already used for this employee, "
                               f"component and period ({start.isoformat()}).", "DUPLICATE_EXTERNAL_REF")
    return Checked(data, place, comp, currency, fx_rule, periods)


def _external_ref_taken(db: Session, company_id: int, employee_id: int, component_id: int, start: date,
                        ref: str, exclude_id: Optional[int] = None) -> bool:
    query = db.query(WageInput.id).filter(
        WageInput.company_id == company_id, WageInput.employee_id == employee_id,
        WageInput.component_id == component_id, WageInput.target_period_start == start,
        WageInput.external_ref == ref, WageInput.status.in_(LIVE_WAGE_INPUT_STATUSES),
    )
    if exclude_id is not None:
        query = query.filter(WageInput.id != exclude_id)
    return query.first() is not None


def _duplicate_flags(db: Session, row: WageInput) -> List[dict]:
    """EC-VAR-02: other live inputs of the employee for the period with the same amount."""
    if row.amount is None:
        return []
    others = (
        db.query(WageInput.id, WageInput.component_code)
        .filter(
            WageInput.company_id == row.company_id, WageInput.employee_id == row.employee_id,
            WageInput.target_period_start == row.target_period_start, WageInput.amount == row.amount,
            WageInput.currency == row.currency, WageInput.status.in_(LIVE_WAGE_INPUT_STATUSES),
            WageInput.id != row.id,
        )
        .order_by(WageInput.id)
        .limit(10)
        .all()
    )
    if not others:
        return []
    return [{"code": POSSIBLE_DUPLICATE, "input_ids": [o.id for o in others],
             "message": "Another input of this employee for the period has the same amount ("
                        + ", ".join(f"#{o.id} {o.component_code}" for o in others) + ")."}]


def _insert(db: Session, principal: Optional[Principal], checked: Checked, *, company_id: int,
            import_id: Optional[int] = None, import_row: Optional[int] = None) -> List[WageInput]:
    data = checked.data
    recurrence = None
    if data.recurrence_until is not None:
        recurrence = {"until": data.recurrence_until.isoformat(), "periods": len(checked.periods)}
    rows: List[WageInput] = []
    for index, (start, end) in enumerate(checked.periods):
        row = WageInput(
            company_id=company_id, employee_id=data.employee_id, legal_entity_id=checked.placement.legal_entity_id,
            pay_group_id=checked.placement.pay_group_id, component_id=checked.component.id,
            component_code=checked.component.code, amount=data.amount, units=data.units, rate=data.rate,
            currency=checked.currency, fx_rule=checked.fx_rule, target_period_start=start, target_period_end=end,
            reason=data.reason, attachments=[], source=data.source, status=sm.WageInputStatus.DRAFT,
            recurrence=recurrence if index == 0 else None,
            recurrence_parent_id=rows[0].id if index else None, external_ref=data.external_ref,
            import_id=import_id if index == 0 else None, import_row=import_row if index == 0 else None, flags=[],
            created_by_user_id=principal.user_id if principal is not None else None, row_version=1,
        )
        db.add(row)
        try:
            with db.begin_nested():
                db.flush()
        except IntegrityError as exc:
            if "uq_wage_inputs_external_ref" not in str(exc.orig):
                raise
            raise conflict(f"External reference {data.external_ref} is already used for this employee, component "
                           f"and period ({start.isoformat()}).", "DUPLICATE_EXTERNAL_REF")
        row.flags = _duplicate_flags(db, row)
        rows.append(row)
    db.flush()
    parent = rows[0]
    audit(db, principal, AUDIT_MODULE, "WAGE_INPUT_CREATED",
          f"Created wage input #{parent.id} ({parent.component_code}) for employee #{parent.employee_id}, "
          f"period {_period_text(checked.periods[0])}" + (f" and {len(rows) - 1} later period(s)" if len(rows) > 1 else ""),
          "wage_input", parent.id, company_id,
          component=parent.component_code, amount=money_json(parent.amount, parent.currency),
          units=decimal_str(parent.units) if parent.units is not None else None, source=parent.source,
          periods=[p[0].isoformat() for p in checked.periods], external_ref=parent.external_ref,
          flags=[f["code"] for f in parent.flags])
    return rows


def create_input(db: Session, principal: Principal, data: InputData, *, submit: bool = True) -> List[WageInput]:
    """Create a DRAFT input (one per period of a recurrence) and, by default, submit it."""
    checked = check_input(db, principal, data)
    rows = _insert(db, principal, checked, company_id=principal.company_id)
    if submit:
        submit_input(db, principal, rows[0].id)
    return rows


def _load(db: Session, principal: Principal, input_id: int, permission: str, *, lock: bool = False) -> WageInput:
    if not principal.has(permission):
        raise forbidden()
    query = db.query(WageInput).filter(
        WageInput.company_id == principal.company_id, WageInput.id == input_id,
        scope_clause(principal, permission, WageInput),
    )
    if lock:
        query = query.with_for_update(of=WageInput).populate_existing()
    row = query.first()
    if row is None:
        raise not_found("Wage input")
    return row


def get_input(db: Session, principal: Principal, input_id: int) -> dict:
    [item] = with_employee_refs(db, principal.company_id, [serialize(_load(db, principal, input_id, READ))])
    return item


def list_inputs(
    db: Session, principal: Principal, *, status: Optional[str] = None, employee_id: Optional[int] = None,
    pay_group_id: Optional[int] = None, period_start: Optional[date] = None, component_code: Optional[str] = None,
    skip: int = 0, limit: int = 50, legal_entity_id: Optional[int] = None,
) -> Tuple[int, List[dict]]:
    if not principal.has(READ):
        raise forbidden()
    query = db.query(WageInput).filter(WageInput.company_id == principal.company_id,
                                       scope_clause(principal, READ, WageInput))
    if status:
        query = query.filter(WageInput.status == status)
    if employee_id is not None:
        query = query.filter(WageInput.employee_id == employee_id)
    if pay_group_id is not None:
        query = query.filter(WageInput.pay_group_id == pay_group_id)
    if legal_entity_id is not None:
        query = query.filter(WageInput.legal_entity_id == legal_entity_id)
    if period_start is not None:
        query = query.filter(WageInput.target_period_start == period_start)
    if component_code:
        query = query.filter(WageInput.component_code == component_code)
    total = query.count()
    rows = query.order_by(WageInput.target_period_start.desc(), WageInput.id.desc()).offset(skip).limit(limit).all()
    return total, with_employee_refs(db, principal.company_id, [serialize(r) for r in rows])


def update_input(db: Session, principal: Principal, input_id: int, changes: Mapping[str, Any], *,
                 row_version: int) -> WageInput:
    """Edit a DRAFT input (amount, units, rate, reason, external_ref); 409 on a stale row_version."""
    row = _load(db, principal, input_id, EDIT, lock=True)
    if row.status != sm.WageInputStatus.DRAFT:
        raise conflict("Only draft inputs can be edited; cancel and re-enter an input after submission.",
                       "NOT_DRAFT")
    if row.row_version != row_version:
        raise conflict("The input changed since you loaded it.", "STALE_ROW_VERSION")
    before = serialize(row)
    data = InputData(
        employee_id=row.employee_id, component_code=row.component_code, period_start=row.target_period_start,
        period_end=row.target_period_end, amount=changes.get("amount", row.amount),
        units=changes.get("units", row.units), rate=changes.get("rate", row.rate), currency=row.currency,
        convert_currency=row.fx_rule is not None, reason=changes.get("reason", row.reason),
        external_ref=changes.get("external_ref", row.external_ref),
    )
    place = placement(db, principal.company_id, row.employee_id, row.target_period_start)
    comp = component(db, principal.company_id, row.component_code, row.target_period_start)
    _check_values(data, comp, place)
    if data.external_ref and data.external_ref != row.external_ref and _external_ref_taken(
            db, principal.company_id, row.employee_id, row.component_id, row.target_period_start, data.external_ref,
            exclude_id=row.id):
        raise conflict(f"External reference {data.external_ref} is already used.", "DUPLICATE_EXTERNAL_REF")
    row.amount, row.units, row.rate = data.amount, data.units, data.rate
    row.reason, row.external_ref = data.reason, data.external_ref
    row.row_version += 1
    db.flush()
    row.flags = _duplicate_flags(db, row)
    after = serialize(row)
    audit(db, principal, AUDIT_MODULE, "WAGE_INPUT_UPDATED", f"Edited draft wage input #{row.id}",
          "wage_input", row.id, principal.company_id,
          before={k: before[k] for k in ("amount", "units", "rate", "reason", "external_ref")},
          after={k: after[k] for k in ("amount", "units", "rate", "reason", "external_ref")})
    return row


def _threshold_ok(db: Session, row: WageInput, comp: Optional[ComponentRef]) -> bool:
    """BELOW_APPROVAL_THRESHOLD: a positive amount at or below every threshold, in the threshold's currency."""
    if row.amount is None or row.amount < 0 or row.fx_rule is not None:
        return False
    value = resolve_setting(db, row.company_id, "payroll.wage_inputs.require_approval_above",
                            on=row.target_period_start, pay_group_id=row.pay_group_id)
    if value is None:
        return False
    limit = Decimal(value.amount)
    if value.currency is not None and value.currency != row.currency:
        return False
    if comp is not None and comp.approval_above is not None:
        if comp.approval_currency != row.currency:
            return False
        limit = min(limit, comp.approval_above)
    return Decimal(row.amount) <= limit


def _series(db: Session, row: WageInput) -> List[WageInput]:
    children = (
        db.query(WageInput)
        .filter(WageInput.company_id == row.company_id, WageInput.recurrence_parent_id == row.id)
        .order_by(WageInput.target_period_start)
        .with_for_update()
        .all()
    )
    return [row, *children]


def submit_input(db: Session, principal: Principal, input_id: int) -> WageInput:
    """DRAFT -> APPROVED (at or below the threshold) or PENDING_APPROVAL with an approval request."""
    row = _load(db, principal, input_id, EDIT, lock=True)
    if row.recurrence_parent_id is not None:
        raise conflict(f"Input #{row.id} belongs to recurring input #{row.recurrence_parent_id}; submit that one.",
                       "SERIES_MEMBER")
    series = [r for r in _series(db, row) if r.status == sm.WageInputStatus.DRAFT]
    for member in series:
        if not period_is_open(db, row.company_id, row.pay_group_id, member.target_period_start,
                              member.target_period_end):
            raise conflict(f"The period {_period_text((member.target_period_start, member.target_period_end))} "
                           "is closed.", "PERIOD_CLOSED")
    comp = _component_resolver(db, row.company_id, row.component_code, row.target_period_start)
    now = clock.utcnow()
    if _threshold_ok(db, row, comp):
        require_transition(principal, sm.WAGE_INPUT, row.status, "AUTO_APPROVE")
        for member in series:
            member.status, member.submitted_at, member.decided_at = sm.WageInputStatus.APPROVED, now, now
            member.row_version += 1
        db.flush()
        audit(db, principal, AUDIT_MODULE, "WAGE_INPUT_APPROVED",
              f"Wage input #{row.id} approved automatically (at or below the approval threshold)",
              "wage_input", row.id, row.company_id, automatic=True, inputs=[m.id for m in series])
        return row
    require_transition(principal, sm.WAGE_INPUT, row.status, "SUBMIT")
    request = approvals.submit(
        db, principal, module=MODULE, subject_type=SUBJECT, subject_id=row.id, subject_employee_id=row.employee_id,
        amount=row.amount if row.amount is not None and row.amount > 0 and row.fx_rule is None else None,
        currency=row.currency if row.amount is not None and row.amount > 0 and row.fx_rule is None else None,
        context={"legal_entity_id": row.legal_entity_id, "pay_group_id": row.pay_group_id,
                 "component": row.component_code, "periods": len(series)},
    )
    for member in series:
        member.status, member.submitted_at = sm.WageInputStatus.PENDING_APPROVAL, now
        member.approval_request_id = request.id
        member.row_version += 1
    db.flush()
    audit(db, principal, AUDIT_MODULE, "WAGE_INPUT_SUBMITTED", f"Submitted wage input #{row.id} for approval",
          "wage_input", row.id, row.company_id, approval_request_id=request.id, inputs=[m.id for m in series])
    return row


@approvals.on_decided(MODULE, SUBJECT)
def _on_decided(db: Session, principal: Optional[Principal], request) -> None:
    rows = (
        db.query(WageInput)
        .filter(WageInput.approval_request_id == request.id,
                WageInput.status == sm.WageInputStatus.PENDING_APPROVAL)
        .with_for_update()
        .all()
    )
    action = {approvals.APPROVED: "APPROVE", approvals.REJECTED: "REJECT", approvals.CANCELLED: "CANCEL"}[request.status]
    now = clock.utcnow()
    remarks = next((s.remarks for s in reversed(request.steps) if s.remarks), None)
    for row in rows:
        row.status = transition(sm.WAGE_INPUT, row.status, action).target
        row.decided_at = now
        row.status_reason = remarks if action != "APPROVE" else None
        row.row_version += 1
    if rows:
        event = {"APPROVE": "WAGE_INPUT_APPROVED", "REJECT": "WAGE_INPUT_REJECTED", "CANCEL": "WAGE_INPUT_CANCELLED"}
        audit(db, principal, AUDIT_MODULE, event[action],
              f"Wage input request #{request.id} {request.status.lower()}: inputs "
              + ", ".join(f"#{r.id}" for r in rows), "wage_input", request.subject_id, request.company_id,
              approval_request_id=request.id, inputs=[r.id for r in rows], remarks=remarks)
    db.flush()


def decide_input(db: Session, principal: Principal, input_id: int, decision: str, remarks: Optional[str] = None,
                 expected_step: Optional[int] = None) -> WageInput:
    """Approve or reject the pending approval of an input (the approvals engine decides who may)."""
    if not principal.has(APPROVE):
        raise forbidden()
    row = _load(db, principal, input_id, APPROVE)
    subject_id = row.recurrence_parent_id or row.id
    request = approvals.pending_request(db, MODULE, SUBJECT, subject_id)
    if request is None or row.status != sm.WageInputStatus.PENDING_APPROVAL:
        raise conflict("This input is not awaiting approval.", "NOT_PENDING")
    approvals.decide(db, principal, request.id, decision, remarks, expected_step)
    db.refresh(row)
    return row


def cancel_input(db: Session, principal: Principal, input_id: int, reason: str) -> WageInput:
    """Cancel a draft, pending or approved (not yet consumed or reserved) input; a pending series is
    cancelled together with its approval request."""
    row = _load(db, principal, input_id, EDIT, lock=True)
    target = require_transition(principal, sm.WAGE_INPUT, row.status, "CANCEL")
    if row.reserved_by_run_employee_id is not None:
        raise conflict("A payroll run has locked this input; release the run first.", "INPUT_RESERVED")
    members = [row]
    if row.status == sm.WageInputStatus.PENDING_APPROVAL and row.approval_request_id is not None:
        members = (
            db.query(WageInput)
            .filter(WageInput.company_id == row.company_id, WageInput.approval_request_id == row.approval_request_id,
                    WageInput.status == sm.WageInputStatus.PENDING_APPROVAL)
            .order_by(WageInput.id)
            .with_for_update()
            .all()
        )
        # The approvals callback moves the series to CANCELLED; the reason is recorded below.
        approvals.withdraw(db, principal, module=MODULE, subject_type=SUBJECT,
                           subject_id=row.recurrence_parent_id or row.id, reason=f"Wage input cancelled: {reason}")
    now = clock.utcnow()
    for member in members:
        member.status, member.status_reason, member.decided_at = target, reason, member.decided_at or now
        member.row_version += 1
    db.flush()
    audit(db, principal, AUDIT_MODULE, "WAGE_INPUT_CANCELLED", f"Cancelled wage input #{row.id}: {reason}",
          "wage_input", row.id, row.company_id, reason=reason, inputs=[m.id for m in members])
    return row


# ---------------------------------------------------------------------------
# Payroll consumption (snapshot builder, run lock, finalization)
# ---------------------------------------------------------------------------
def approved_inputs(db: Session, company_id: int, employee_id: int, period_start: date, period_end: date, *,
                    run_employee_id: Optional[int] = None) -> List[dict]:
    """Approved inputs targeted at a period inside [period_start, period_end], as plain dicts.

    Inputs reserved by another run employee are left out; ``run_employee_id`` keeps its own.
    """
    query = db.query(WageInput).filter(
        WageInput.company_id == company_id, WageInput.employee_id == employee_id,
        WageInput.status == sm.WageInputStatus.APPROVED,
        WageInput.target_period_start >= period_start, WageInput.target_period_end <= period_end,
    )
    if run_employee_id is None:
        query = query.filter(WageInput.reserved_by_run_employee_id.is_(None))
    else:
        query = query.filter(or_(WageInput.reserved_by_run_employee_id.is_(None),
                                 WageInput.reserved_by_run_employee_id == run_employee_id))
    result = []
    for row in query.order_by(WageInput.target_period_start, WageInput.id).all():
        result.append({
            "id": row.id, "component_id": row.component_id, "component_code": row.component_code,
            "amount": amount_str(Decimal(row.amount), row.currency) if row.amount is not None else None,
            "units": decimal_str(Decimal(row.units)) if row.units is not None else None,
            "rate": decimal_str(Decimal(row.rate)) if row.rate is not None else None,
            "currency": row.currency, "fx_rule": row.fx_rule, "source": row.source,
            "external_ref": row.external_ref, "reason": row.reason,
            "target_period": {"id": row.target_period_id, "start": row.target_period_start.isoformat(),
                              "end": row.target_period_end.isoformat()},
            "version_hash": version_hash(row),
        })
    return result


def _locked(db: Session, company_id: int, input_ids: Iterable[int]) -> List[WageInput]:
    ids = sorted(set(input_ids))
    rows = (
        db.query(WageInput)
        .filter(WageInput.company_id == company_id, WageInput.id.in_(ids))
        .order_by(WageInput.id)
        .with_for_update()
        .populate_existing()
        .all()
    )
    if len(rows) != len(ids):
        raise not_found("Wage input")
    return rows


def reserve_inputs(db: Session, company_id: int, run_employee_id: int, input_ids: Iterable[int]) -> int:
    """Run lock: reserve approved inputs for one run employee (409 if another run holds one)."""
    rows = _locked(db, company_id, input_ids)
    for row in rows:
        if row.status != sm.WageInputStatus.APPROVED:
            raise conflict(f"Wage input #{row.id} is {row.status}, not approved.", "INPUT_NOT_APPROVED")
        if row.reserved_by_run_employee_id not in (None, run_employee_id):
            raise conflict(f"Wage input #{row.id} is locked by another payroll run.", "INPUT_RESERVED")
        row.reserved_by_run_employee_id = run_employee_id
    db.flush()
    return len(rows)


def consume_inputs(db: Session, company_id: int, run_employee_id: int, input_ids: Iterable[int]) -> int:
    """Finalization: APPROVED -> CONSUMED exactly once (EC-VAR-01); repeating for the same run employee is a no-op."""
    rows = _locked(db, company_id, input_ids)
    now = clock.utcnow()
    consumed = 0
    for row in rows:
        if row.status == sm.WageInputStatus.CONSUMED:
            if row.consumed_by_run_employee_id != run_employee_id:
                raise conflict(f"Wage input #{row.id} was already paid by another run.", "INPUT_ALREADY_CONSUMED")
            continue
        if row.reserved_by_run_employee_id not in (None, run_employee_id):
            raise conflict(f"Wage input #{row.id} is locked by another payroll run.", "INPUT_RESERVED")
        row.status = transition(sm.WAGE_INPUT, row.status, "CONSUME").target
        row.reserved_by_run_employee_id = run_employee_id
        row.consumed_by_run_employee_id, row.consumed_at = run_employee_id, now
        row.row_version += 1
        consumed += 1
    db.flush()
    if consumed:
        audit(db, None, AUDIT_MODULE, "WAGE_INPUT_CONSUMED",
              f"{consumed} wage input(s) paid by run employee #{run_employee_id}", "run_employee", run_employee_id,
              company_id, inputs=[r.id for r in rows])
    return consumed


def release_inputs(db: Session, company_id: int, run_employee_ids: Iterable[int]) -> int:
    """A cancelled or reopened run releases its reservations; consumed inputs stay consumed (EC-VAR-01)."""
    ids = sorted(set(run_employee_ids))
    if not ids:
        return 0
    rows = (
        db.query(WageInput)
        .filter(WageInput.company_id == company_id, WageInput.reserved_by_run_employee_id.in_(ids),
                WageInput.status == sm.WageInputStatus.APPROVED)
        .with_for_update()
        .all()
    )
    for row in rows:
        row.reserved_by_run_employee_id = None
    db.flush()
    return len(rows)


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------
def _is_formula(value: str) -> bool:
    return bool(value) and value.startswith(FORMULA_PREFIXES)


def _escape_cell(value: Any) -> str:
    text_value = "" if value is None else str(value)
    return "'" + text_value if _is_formula(text_value) else text_value


def _decimal_cell(value: str, name: str, errors: List[str]) -> Optional[Decimal]:
    if not value:
        return None
    try:
        parsed = to_decimal(value)
    except MoneyError:
        errors.append(f"{name} {value!r} is not a decimal number")
        return None
    if not parsed.is_finite():
        errors.append(f"{name} {value!r} is not a decimal number")
        return None
    return parsed


def _date_cell(value: str, name: str, errors: List[str]) -> Optional[date]:
    if not value:
        errors.append(f"{name} is required")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        errors.append(f"{name} {value!r} is not a date (YYYY-MM-DD)")
        return None


def _parse_csv(data: bytes) -> Tuple[List[str], List[Dict[str, str]]]:
    if not data:
        raise bad_request("The file is empty.", "IMPORT_EMPTY")
    if len(data) > MAX_IMPORT_BYTES:
        raise bad_request(f"The file is larger than {MAX_IMPORT_BYTES // (1024 * 1024)} MB.", "IMPORT_TOO_LARGE")
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise bad_request("The file is not UTF-8 text.", "IMPORT_MALFORMED")
    if "\x00" in content:
        raise bad_request("The file is not a CSV text file.", "IMPORT_MALFORMED")
    try:
        reader = csv.DictReader(io.StringIO(content, newline=""), strict=True)
        header = [h.strip().lower() for h in (reader.fieldnames or [])]
        reader.fieldnames = header
        rows = []
        for record in reader:
            if None in record:
                raise csv.Error(f"line {reader.line_num} has more cells than the header")
            rows.append({k: (v or "").strip() for k, v in record.items()})
            if len(rows) > MAX_IMPORT_ROWS:
                raise bad_request(f"At most {MAX_IMPORT_ROWS} rows per import.", "IMPORT_TOO_LARGE")
    except csv.Error as exc:
        raise bad_request(f"The CSV file is malformed: {exc}.", "IMPORT_MALFORMED")
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    unknown = [c for c in header if c not in IMPORT_COLUMNS]
    if missing or unknown:
        raise bad_request("Columns: " + (f"missing {', '.join(missing)}" if missing else "")
                          + ("; " if missing and unknown else "") + (f"unknown {', '.join(unknown)}" if unknown else "")
                          + f". The template columns are {', '.join(IMPORT_COLUMNS)}.", "IMPORT_COLUMNS")
    if not rows:
        raise bad_request("The file has no data rows.", "IMPORT_EMPTY")
    return header, rows


def template_csv() -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(IMPORT_COLUMNS)
    writer.writerow(["E0001", "BONUS", "5000.00", "", "", "INR", "2026-10-01", "2026-10-31", "BONUS-2026-10-E0001",
                     "Festival bonus"])
    return out.getvalue()


def _validate_rows(db: Session, principal: Principal, records: Sequence[Dict[str, str]],
                   permission: str) -> List[dict]:
    codes = {r.get("employee_code", "") for r in records if r.get("employee_code")}
    employees = {
        e.employee_code: e.id for e in db.query(Employee.employee_code, Employee.id).filter(
            Employee.company_id == principal.company_id, Employee.employee_code.in_(sorted(codes)))
    } if codes else {}
    seen: Dict[tuple, int] = {}
    results: List[dict] = []
    for number, record in enumerate(records, start=2):
        errors: List[str] = []
        for column in ("employee_code", "component_code", "currency", "external_ref", "reason"):
            if _is_formula(record.get(column, "")):
                errors.append(f"{column} starts with {record[column][0]!r}: formula-like text is not accepted")
        amount = _decimal_cell(record.get("amount", ""), "amount", errors)
        units = _decimal_cell(record.get("units", ""), "units", errors)
        rate = _decimal_cell(record.get("rate", ""), "rate", errors)
        start = _date_cell(record.get("period_start", ""), "period_start", errors)
        end = _date_cell(record.get("period_end", ""), "period_end", errors)
        if units is not None and units < 0:
            errors.append("units cannot be negative")
        employee_id = employees.get(record.get("employee_code", ""))
        if not record.get("employee_code"):
            errors.append("employee_code is required")
        elif employee_id is None:
            errors.append(f"unknown employee {record['employee_code']}")
        if not record.get("component_code"):
            errors.append("component_code is required")
        external_ref = record.get("external_ref") or None
        if external_ref is not None and len(external_ref) > 100:
            errors.append("external_ref is longer than 100 characters")
        reason = record.get("reason") or None
        if reason is not None and len(reason) > 1000:
            errors.append("reason is longer than 1000 characters")
        values = None
        if not errors and employee_id is not None and start is not None and end is not None:
            data = InputData(employee_id=employee_id, component_code=record["component_code"], period_start=start,
                             period_end=end, amount=amount, units=units, rate=rate,
                             currency=record.get("currency") or None, reason=reason, external_ref=external_ref,
                             source="IMPORT")
            try:
                checked = check_input(db, principal, data, permission=permission)
            except AppError as exc:
                errors.append("employee not found or outside your scope" if exc.status_code == 404 else str(exc.detail))
            else:
                key = (employee_id, checked.component.id, start, external_ref or f"#amount:{amount}:{units}")
                if key in seen:
                    errors.append(f"duplicate of row {seen[key]} (same employee, component, period and "
                                  + ("external_ref)" if external_ref else "amount)"))
                else:
                    seen[key] = number
                values = {"employee_id": employee_id, "component_code": checked.component.code,
                          "period_start": start.isoformat(), "period_end": end.isoformat(),
                          "amount": decimal_str(amount) if amount is not None else None,
                          "units": decimal_str(units) if units is not None else None,
                          "rate": decimal_str(rate) if rate is not None else None, "currency": checked.currency,
                          "external_ref": external_ref, "reason": reason}
        results.append({"row": number, "ok": not errors, "errors": errors, "values": values if not errors else None,
                        "employee_code": record.get("employee_code", "")})
    return results


def dry_run_import(db: Session, principal: Principal, file_name: str, data: bytes) -> WageInputImport:
    """Validate every row and stage the file; nothing is created (EC-VAR-16). Importing creates and
    submits inputs, so it needs payroll:inputs:edit besides payroll:inputs:import."""
    if not (principal.has(IMPORT) and principal.has(EDIT)):
        raise forbidden()
    _, records = _parse_csv(data)
    results = _validate_rows(db, principal, records, IMPORT)
    errors = sum(1 for r in results if not r["ok"])
    path = storage.save_bytes(principal.company_id, "wage-input-imports", data, "csv")
    row = WageInputImport(
        company_id=principal.company_id, file_name=(file_name or "import.csv")[:200], storage_path=path,
        sha256=hashlib.sha256(data).hexdigest(), bytes=len(data), status="INVALID" if errors else "VALIDATED",
        row_count=len(results), error_count=errors, rows=results, created_by_user_id=principal.user_id,
    )
    db.add(row)
    db.flush()
    audit(db, principal, AUDIT_MODULE, "IMPORT_VALIDATED",
          f"Validated wage input import #{row.id}: {len(results)} row(s), {errors} with errors", "wage_input_import",
          row.id, principal.company_id, rows=len(results), errors=errors, sha256=row.sha256)
    return row


def _load_import(db: Session, principal: Principal, import_id: int, *, lock: bool = False) -> WageInputImport:
    if not principal.has(IMPORT):
        raise forbidden()
    query = db.query(WageInputImport).filter(WageInputImport.company_id == principal.company_id,
                                             WageInputImport.id == import_id)
    if not principal.has_company_scope(IMPORT):
        query = query.filter(WageInputImport.created_by_user_id == principal.user_id)
    if lock:
        query = query.with_for_update().populate_existing()
    row = query.first()
    if row is None:
        raise not_found("Import")
    return row


def serialize_import(row: WageInputImport, *, with_rows: bool = True) -> dict:
    body = {
        "id": row.id, "file_name": row.file_name, "sha256": row.sha256, "status": row.status,
        "row_count": row.row_count, "error_count": row.error_count, "job_id": row.job_id,
        "result": row.result, "created_at": row.created_at, "committed_at": row.committed_at,
    }
    if with_rows:
        body["rows"] = [{"row": r["row"], "ok": r["ok"], "errors": r["errors"], "employee_code": r["employee_code"]}
                        for r in row.rows or []]
    return body


def get_import(db: Session, principal: Principal, import_id: int) -> dict:
    return serialize_import(_load_import(db, principal, import_id))


def list_imports(db: Session, principal: Principal, *, status: Optional[str] = None, skip: int = 0,
                 limit: int = 50) -> Tuple[int, List[dict]]:
    """Imports the caller may open (the `_load_import` rule: everyone's with company-wide import access,
    otherwise their own), newest first, without the per-row details (F-44)."""
    if not principal.has(IMPORT):
        raise forbidden()
    query = db.query(WageInputImport).filter(WageInputImport.company_id == principal.company_id)
    if not principal.has_company_scope(IMPORT):
        query = query.filter(WageInputImport.created_by_user_id == principal.user_id)
    if status:
        query = query.filter(WageInputImport.status == status)
    total = query.count()
    rows = query.order_by(WageInputImport.created_at.desc(), WageInputImport.id.desc()).offset(skip).limit(limit).all()
    return total, [{**serialize_import(r, with_rows=False), "bytes": r.bytes,
                    "created_by_user_id": r.created_by_user_id} for r in rows]


def commit_import(db: Session, principal: Principal, import_id: int) -> WageInputImport:
    """Queue the commit job of a validated import (rows are re-validated inside the job)."""
    row = _load_import(db, principal, import_id, lock=True)
    if row.status == "INVALID":
        raise conflict(f"{row.error_count} row(s) have errors; fix the file and validate it again.",
                       "IMPORT_HAS_ERRORS")
    if row.status != "VALIDATED":
        raise conflict(f"This import is already {row.status.lower()}.", "IMPORT_NOT_VALIDATED")
    row.status = "COMMITTING"
    job = jobs.enqueue(db, company_id=principal.company_id, type=IMPORT_JOB, payload={"import_id": row.id},
                       idempotency_key=f"wage-input-import-{row.id}", created_by_user_id=principal.user_id,
                       read_permission=IMPORT)
    row.job_id = job.id
    db.flush()
    audit(db, principal, AUDIT_MODULE, "IMPORT_COMMIT_REQUESTED", f"Queued the commit of wage input import #{row.id}",
          "wage_input_import", row.id, principal.company_id, job_id=job.id, rows=row.row_count)
    return row


@jobs.register(IMPORT_JOB)
def _run_import(ctx: jobs.JobContext) -> dict:
    db = ctx.db
    import_id = ctx.payload["import_id"]
    row = (
        db.query(WageInputImport)
        .filter(WageInputImport.company_id == ctx.company_id, WageInputImport.id == import_id)
        .with_for_update()
        .first()
    )
    if row is None:
        raise jobs.PermanentJobError("The import no longer exists")
    if row.status == "COMMITTED":
        return dict(row.result or {})
    principal = principal_for_user(db, ctx.company_id, row.created_by_user_id)
    if principal is None or not (principal.has(IMPORT) and principal.has(EDIT)):
        return _fail_import(db, row, "The importing user can no longer import payroll inputs.")
    path = storage.resolve(ctx.company_id, row.storage_path)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != row.sha256:
        return _fail_import(db, row, "The staged file changed since it was validated.")
    _, records = _parse_csv(data)
    results = _validate_rows(db, principal, records, IMPORT)
    bad = [r for r in results if not r["ok"]]
    if bad:
        row.rows, row.error_count = results, len(bad)
        return _fail_import(db, row, f"{len(bad)} row(s) are no longer valid; nothing was imported.")
    created: List[dict] = []
    total = len(results)
    for index, result in enumerate(results, start=1):
        values = result["values"]
        existing = db.query(WageInput.id).filter(WageInput.import_id == row.id,
                                                 WageInput.import_row == result["row"]).first()
        if existing is not None:
            created.append({"row": result["row"], "input_id": existing.id})
            continue
        data_row = InputData(
            employee_id=values["employee_id"], component_code=values["component_code"],
            period_start=date.fromisoformat(values["period_start"]), period_end=date.fromisoformat(values["period_end"]),
            amount=Decimal(values["amount"]) if values["amount"] is not None else None,
            units=Decimal(values["units"]) if values["units"] is not None else None,
            rate=Decimal(values["rate"]) if values["rate"] is not None else None, currency=values["currency"],
            reason=values["reason"], external_ref=values["external_ref"], source="IMPORT",
        )
        checked = check_input(db, principal, data_row, permission=IMPORT)
        inserted = _insert(db, principal, checked, company_id=ctx.company_id, import_id=row.id,
                           import_row=result["row"])
        submit_input(db, principal, inserted[0].id)
        created.append({"row": result["row"], "input_id": inserted[0].id, "status": inserted[0].status})
        if index % 50 == 0:
            ctx.progress(index, total)
    ctx.progress(total, total)
    row.status, row.committed_at = "COMMITTED", clock.utcnow()
    row.result = {"created": len(created), "inputs": created}
    db.flush()
    audit(db, principal, AUDIT_MODULE, "IMPORT_COMMITTED", f"Imported {len(created)} wage input(s) from import #{row.id}",
          "wage_input_import", row.id, ctx.company_id, created=len(created), sha256=row.sha256)
    return {"import_id": row.id, "created_count": len(created)}


def _fail_import(db: Session, row: WageInputImport, message: str) -> dict:
    row.status = "FAILED"
    row.result = {"created": 0, "error": message}
    db.flush()
    audit(db, None, AUDIT_MODULE, "IMPORT_FAILED", f"Wage input import #{row.id} failed: {message}",
          "wage_input_import", row.id, row.company_id, error=message)
    return {"import_id": row.id, "created_count": 0}


def import_result_csv(db: Session, principal: Principal, import_id: int) -> str:
    """Per-row outcome as CSV; formula-like cells are escaped with a leading apostrophe."""
    row = _load_import(db, principal, import_id)
    created = {item["row"]: item for item in (row.result or {}).get("inputs", [])}
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["row", "employee_code", "outcome", "input_id", "errors"])
    for result in row.rows or []:
        item = created.get(result["row"])
        outcome = "CREATED" if item else ("ERROR" if not result["ok"] else row.status)
        writer.writerow([result["row"], _escape_cell(result["employee_code"]), outcome,
                         item["input_id"] if item else "", _escape_cell("; ".join(result["errors"]))])
    return out.getvalue()

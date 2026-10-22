"""Pay components: catalogue, versions, formula validation and the formula tester (3.3 §2-§5, §4.8).

* A component is created with version 1; every change is a new version from an effective date that
  closes the ACTIVE version before it the day before (a version starting on or after the new date is
  SUPERSEDED). Codes are UPPER_SNAKE, unique per tenant and immutable once a structure or an assignment
  uses them (EC-CMP-06; a database trigger enforces it too). Two components may share a display name
  (EC-CMP-05). Pack-owned components are read-only (EC-CMP-18).
* Formulas (FORMULA method, PERCENT_OF/RATE_X_UNITS/SLAB/INPUT/BALANCING parameters, cap/floor
  formulas, eligibility) are checked at save with app.payroll.formula: references, types, currency,
  protected attributes, limits and parse errors (EC-CMP-07/08/09/19) block saving; cycles across the
  catalogue are reported with their path (EC-CMP-01); NULL-able inputs without COALESCE are warnings
  (EC-CMP-03).
* The tester evaluates a formula with sample values, or for a real employee inside the caller's
  compensation scope (their structure's full-period values, employee facts, a full period without
  LOP), and returns the value, every reference and the step-by-step trace.

Components are tenant-wide: reading needs payroll:salary_structure:read, changing it at COMPANY scope.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.company import Company
from app.models.compensation import (
    CALC_METHODS,
    CATEGORIES_BY_KIND,
    COMPONENT_KINDS,
    DEFAULT_TOTALS,
    FREQUENCIES,
    PRORATION_BASES,
    PRORATIONS,
    ROUNDING_MODES,
    VISIBILITIES,
    CompensationAssignmentLine,
    PayComponent,
    PayComponentVersion,
    SalaryTemplateLine,
)
from app.models.payroll_config import PayrollTable
from app.payroll import audit as payroll_audit
from app.payroll import currencies
from app.payroll import formula as fm
from app.payroll.engine.snapshot import DEFAULT_STAGE
from app.payroll.formula.ast import ComponentRef
from app.payroll.formula.variables import VARIABLES
from app.payroll.money import amount_str, decimal_str, to_decimal
from app.payroll.money_errors import MoneyError
from app.services.audit_log_service import AuditLogService

READ = "payroll:salary_structure:read"
WRITE = "payroll:salary_structure:edit"
COMPANY_OWNER = "COMPANY"
ONE_DAY = dt.timedelta(days=1)
TAG_PATTERN = r"^[A-Z][A-Z0-9_]{0,39}$"
MAX_TAGS = 30
PAYSLIP_KEYS = {"group": str, "order": int, "show_if_zero": bool, "show_rate_column": bool, "label_override": str}
GL_KEYS = {"debit_account": str, "credit_account": str, "allocate_by_cost_center": bool}
TAXABILITY = ("TAXABLE", "EXEMPT", "PARTIAL")
# Totals a kind can never be part of (3.3 §2.1): a benefit in kind is never cash, an employer
# contribution never reaches net pay, informational lines touch no total.
FORBIDDEN_TOTALS = {
    "DEDUCTION": {"in_gross", "in_ctc"},
    "EMPLOYER_CONTRIBUTION": {"in_gross", "in_net"},
    "REIMBURSEMENT": {"in_gross"},
    "BENEFIT_IN_KIND": {"in_gross", "in_net"},
    "INFORMATIONAL": {"in_gross", "in_net", "in_ctc", "in_employer_cost"},
    "ROUNDING": {"in_gross", "in_ctc"},
}

COMPONENT_CREATED = "COMPONENT_CREATED"
COMPONENT_VERSION_CREATED = "COMPONENT_VERSION_CREATED"
COMPONENT_UPDATED = "COMPONENT_UPDATED"
COMPONENTS_SEEDED = "COMPONENTS_SEEDED"


# --------------------------------------------------------------------------- helpers
def today(db: Session, company_id: int) -> dt.date:
    return clock.company_today(db, company_id)


def company_currency(db: Session, company_id: int) -> str:
    company = db.get(Company, company_id)
    return company.currency if company is not None and company.currency else "USD"


def require_read(principal: Principal) -> None:
    if not principal.has(READ):
        raise forbidden()


def require_company_write(principal: Principal) -> None:
    if not principal.has(WRITE, "COMPANY"):
        raise forbidden("Pay components are company-wide: changing them needs company-wide salary structure access.")


def formula_error(error: fm.FormulaError, *, field_name: str | None = None, warnings: Sequence[Any] = ()) -> AppError:
    issues = [i.to_dict() for i in error.issues] or [error.to_dict()]
    return AppError(400, {"message": error.message, "field": field_name, "errors": issues,
                          "warnings": [w.to_dict() if hasattr(w, "to_dict") else w for w in warnings],
                          "path": error.path}, error.code)


def invalid(message: str, code: str = "INVALID_COMPONENT", field_name: str | None = None) -> AppError:
    return AppError(400, {"message": message, "field": field_name} if field_name else message, code)


def plain(value: Decimal | None) -> str | None:
    """A stored decimal without the column's trailing zeros ("40.000000" -> "40")."""
    return None if value is None else decimal_str(Decimal(value).normalize())


def parse_decimal(raw: Any, what: str, *, allow_negative: bool = False) -> Decimal:
    """A Decimal from a decimal string (or a Decimal from code); JSON numbers are refused (EC-MON-15)."""
    try:
        if isinstance(raw, str):
            value = to_decimal(raw.strip())
        elif isinstance(raw, Decimal):
            value = to_decimal(raw)
        else:
            raise TypeError(what)
    except (MoneyError, TypeError, ValueError):
        raise invalid(f"{what} must be a decimal string such as \"12.50\".", "INVALID_DECIMAL", what)
    if value < 0 and not allow_negative:
        raise invalid(f"{what} cannot be negative.", "INVALID_DECIMAL", what)
    return value


def load_component(db: Session, company_id: int, code: str, *, lock: bool = False) -> PayComponent:
    query = db.query(PayComponent).filter(PayComponent.company_id == company_id, PayComponent.code == code)
    if lock:
        query = query.with_for_update()
    found = query.first()
    if found is None:
        raise not_found("Pay component")
    return found


def pick_version(versions: Sequence[PayComponentVersion], on: dt.date) -> PayComponentVersion | None:
    """Of ACTIVE versions sorted by start: the one in force on `on`, else the earliest later one, else the last."""
    active = [v for v in versions if v.status == "ACTIVE"]
    current = [v for v in active if v.effective_from <= on and (v.effective_to is None or v.effective_to >= on)]
    if current:
        return current[-1]
    later = [v for v in active if v.effective_from > on]
    return later[0] if later else (active[-1] if active else None)


def version_on(db: Session, component: PayComponent, on: dt.date) -> PayComponentVersion | None:
    """The ACTIVE version in force on `on`, else the earliest later one (a component created for the future)."""
    versions = (
        db.query(PayComponentVersion)
        .filter(PayComponentVersion.company_id == component.company_id,
                PayComponentVersion.component_id == component.id, PayComponentVersion.status == "ACTIVE")
        .order_by(PayComponentVersion.effective_from)
        .all()
    )
    return pick_version(versions, on)


@dataclass(frozen=True)
class CatalogueEntry:
    component: PayComponent
    version: PayComponentVersion

    @property
    def code(self) -> str:
        return self.component.code

    @property
    def stage(self) -> int:
        return DEFAULT_STAGE[self.component.kind]


def catalogue(db: Session, company_id: int, on: dt.date, *, include_archived: bool = False) -> dict[str, CatalogueEntry]:
    """Every component with its version in force on `on`, by code (one query per table)."""
    query = db.query(PayComponent).filter(PayComponent.company_id == company_id)
    if not include_archived:
        query = query.filter(PayComponent.status == "ACTIVE")
    components = {c.id: c for c in query.all()}
    if not components:
        return {}
    versions = (
        db.query(PayComponentVersion)
        .filter(PayComponentVersion.company_id == company_id, PayComponentVersion.component_id.in_(sorted(components)),
                PayComponentVersion.status == "ACTIVE")
        .order_by(PayComponentVersion.component_id, PayComponentVersion.effective_from)
        .all()
    )
    grouped: dict[int, list[PayComponentVersion]] = {}
    for version in versions:
        grouped.setdefault(version.component_id, []).append(version)
    chosen = {cid: pick_version(items, on) for cid, items in grouped.items()}
    chosen = {cid: v for cid, v in chosen.items() if v is not None}
    return {c.code: CatalogueEntry(c, chosen[cid]) for cid, c in components.items() if cid in chosen}


def table_sigs(db: Session, company_id: int, on: dt.date) -> dict[str, fm.TableSig]:
    rows = (
        db.query(PayrollTable)
        .filter(PayrollTable.company_id == company_id, PayrollTable.status == "ACTIVE",
                PayrollTable.effective_from <= on)
        .all()
    )
    sigs: dict[str, fm.TableSig] = {}
    for table in rows:
        if table.effective_to is not None and table.effective_to < on:
            continue
        kind = fm.TableKind(table.kind)
        currency = table.currency
        sigs[table.code] = fm.TableSig(
            kind, key_currency=currency if kind is not fm.TableKind.LOOKUP else None,
            value_currency=currency if kind is not fm.TableKind.SLAB else None,
        )
    return sigs


def type_env(
    catalogue_: Mapping[str, CatalogueEntry], currency: str, *, tables: Mapping[str, fm.TableSig] | None = None,
    component: str | None = None, stage: int | None = None, expect: str = "AMOUNT",
    structure: Sequence[str] | None = None, extra: Mapping[str, int] | None = None,
) -> fm.TypeEnv:
    """The static environment of a formula: with `structure`, its components are required and the rest
    optional (NULL when absent); without it the whole catalogue is visible (component editor)."""
    members = set(structure) if structure is not None else None
    sigs: dict[str, fm.ComponentSig] = {}
    tags: dict[str, list[str]] = {}
    for code, entry in catalogue_.items():
        optional = members is not None and code not in members
        sigs[code] = fm.ComponentSig(currency, stage=entry.stage, optional=optional)
        for tag in entry.version.tags or ():
            tags.setdefault(tag, [])
            if not optional:
                tags[tag].append(code)
    for code, code_stage in (extra or {}).items():
        sigs[code] = fm.ComponentSig(currency, stage=code_stage)
    return fm.TypeEnv(currency, components=sigs, tags=tags, tables=dict(tables or {}), component=component,
                      stage=stage, expect=expect)


def check_formula(source: str, env: fm.TypeEnv, *, field_name: str) -> fm.Analysis:
    """Save-time check; raises a 400 with every error (and the warnings) of the formula."""
    if not isinstance(source, str) or not source.strip():
        raise invalid(f"{field_name} needs a formula.", "FORMULA_REQUIRED", field_name)
    analysis = fm.diagnose(source, env)
    if analysis.errors:
        first = analysis.errors[0]
        raise formula_error(fm.FormulaError.from_issue(first, analysis.errors), field_name=field_name,
                            warnings=analysis.warnings)
    return analysis


def method_text(method: str, params: Mapping[str, Any], formula: str | None, *, own: str | None = None,
                value: Decimal | None = None) -> str | tuple[str, str] | None:
    """The canonical formula(s) of a method: None for FIXED/SYSTEM, (rate, units) for RATE_X_UNITS."""
    try:
        if method in ("FIXED", "SYSTEM"):
            return None
        if method == "FORMULA":
            return fm.method_formula(method, {"formula": formula})
        if method == "PERCENT_OF":
            percent = value if value is not None else params.get("percent")
            return fm.method_formula(method, {"percent": parse_decimal(percent, "percent"),
                                              "bases": tuple(params.get("bases") or ()), "tag": params.get("tag")})
        if method == "RATE_X_UNITS":
            rate, units = params.get("rate"), params.get("units")
            fm.method_formula(method, {"rate": rate, "units": units})
            return fm.render(fm.parse(str(rate))), fm.render(fm.parse(str(units)))
        if method == "INPUT":
            return fm.method_formula(method, {"code": params.get("code") or own})
        if method == "SLAB":
            return fm.method_formula(method, {"table": params.get("table"), "base": params.get("base"),
                                              "progressive": params.get("progressive", True)})
        if method == "BALANCING":
            others = params.get("others")
            total = params.get("total") or "ctc.monthly"
            if not others:
                return fm.render(fm.parse(str(total)))
            return fm.method_formula(method, {"others": list(others), "total": total, "own": own})
    except fm.FormulaError as error:
        raise formula_error(error, field_name="params")
    raise invalid(f"Unknown calculation method {method}.", "INVALID_METHOD", "calc_method")


# --------------------------------------------------------------------------- input validation
@dataclass
class ComponentInput:
    """Everything a component version holds; None means "keep the previous version's value"."""

    name: str | None = None
    translations: dict[str, str] | None = None
    description: str | None = None
    calc_method: str | None = None
    formula: str | None = None
    params: dict[str, Any] | None = None
    tags: list[str] | None = None
    proration: str | None = None
    proration_basis: str | None = None
    frequency: dict[str, Any] | None = None
    taxability: dict[str, str] | None = None
    totals_flags: dict[str, bool] | None = None
    cap: dict[str, Any] | None = None
    floor: dict[str, Any] | None = None
    rounding: str | None = None
    eligibility: str | None = None
    payslip: dict[str, Any] | None = None
    visibility: str | None = None
    gl: dict[str, Any] | None = None
    reporting_codes: dict[str, str] | None = None
    input_approval_above: dict[str, str] | None = None
    effective_from: dt.date | None = None
    reason: str | None = None
    cleared: set[str] = field(default_factory=set)  # optional fields explicitly set to null


def _choice(value: Any, allowed: Sequence[str], what: str) -> str:
    if value not in allowed:
        raise invalid(f"{what} must be one of {', '.join(allowed)}.", "INVALID_VALUE", what)
    return value


def _typed_mapping(raw: Mapping[str, Any], spec: Mapping[str, type], what: str) -> dict[str, Any]:
    result = {}
    for key, value in raw.items():
        if key not in spec:
            raise invalid(f"{what} has an unknown key {key!r}.", "INVALID_VALUE", what)
        expected = spec[key]
        if value is None:
            continue
        if expected is int and (isinstance(value, bool) or not isinstance(value, int)):
            raise invalid(f"{what}.{key} must be an integer.", "INVALID_VALUE", what)
        if expected is not int and not isinstance(value, expected):
            raise invalid(f"{what}.{key} must be a {expected.__name__}.", "INVALID_VALUE", what)
        if expected is str and len(value) > 150:
            raise invalid(f"{what}.{key} is too long.", "INVALID_VALUE", what)
        result[key] = value
    return result


def _tags(raw: Sequence[str]) -> list[str]:
    import re

    tags = []
    for tag in raw:
        if not isinstance(tag, str) or not re.fullmatch(TAG_PATTERN, tag) or "__" in tag:
            raise invalid(f"Tag {tag!r} must be UPPER_SNAKE.", "INVALID_TAG", "tags")
        if tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        raise invalid(f"A component carries at most {MAX_TAGS} tags.", "INVALID_TAG", "tags")
    return sorted(tags)


def _bound(raw: Mapping[str, Any] | None, what: str, env: fm.TypeEnv) -> dict[str, Any] | None:
    if raw is None:
        return None
    if set(raw) == {"formula"}:
        check_formula(raw["formula"], env, field_name=what)
        return {"formula": fm.render(fm.parse(raw["formula"]))}
    if set(raw) == {"amount", "currency"}:
        amount = parse_decimal(raw["amount"], f"{what}.amount")
        try:
            currency = currencies.require_payroll_currency(raw["currency"]).code
            return {"amount": amount_str(amount, currency), "currency": currency}
        except MoneyError as exc:
            raise invalid(exc.message, exc.code, what)
    raise invalid(f"{what} is {{\"amount\", \"currency\"}} or {{\"formula\"}}.", "INVALID_VALUE", what)


def _frequency(raw: Mapping[str, Any]) -> dict[str, Any]:
    kind = _choice(raw.get("type"), FREQUENCIES, "frequency.type")
    if set(raw) - {"type", "months"}:
        raise invalid("frequency has unknown keys.", "INVALID_VALUE", "frequency")
    months = raw.get("months") or []
    if kind == "SPECIFIC_MONTHS":
        if not months or any(isinstance(m, bool) or not isinstance(m, int) or not 1 <= m <= 12 for m in months):
            raise invalid("SPECIFIC_MONTHS needs months between 1 and 12.", "INVALID_VALUE", "frequency")
        return {"type": kind, "months": sorted(set(months))}
    if months:
        raise invalid("Only SPECIFIC_MONTHS takes months.", "INVALID_VALUE", "frequency")
    return {"type": kind}


def _totals(kind: str, raw: Mapping[str, Any] | None) -> dict[str, bool]:
    flags = dict(DEFAULT_TOTALS[kind])
    for key, value in (raw or {}).items():
        if key not in flags or not isinstance(value, bool):
            raise invalid(f"totals_flags.{key} is not a known boolean flag.", "INVALID_VALUE", "totals_flags")
        flags[key] = value
    wrong = sorted(k for k in FORBIDDEN_TOTALS.get(kind, ()) if flags[k])
    if wrong:
        raise invalid(f"A {kind} component cannot be counted in {', '.join(wrong)}.", "INVALID_TOTALS", "totals_flags")
    return flags


def _params(method: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    """Method parameters as stored JSON (decimals as strings)."""
    allowed = {
        "FIXED": {"basis"}, "SYSTEM": set(), "FORMULA": set(), "INPUT": {"code", "allow_negative_input"},
        "PERCENT_OF": {"percent", "bases", "tag"}, "RATE_X_UNITS": {"rate", "units"},
        "SLAB": {"table", "base", "progressive"}, "BALANCING": {"others", "total"},
    }[method]
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise invalid(f"{method} does not take {', '.join(unknown)}.", "INVALID_PARAMS", "params")
    params = dict(raw)
    if "percent" in params and params["percent"] is not None:
        params["percent"] = decimal_str(parse_decimal(params["percent"], "percent"))
    if "basis" in params and params["basis"] not in ("MONTHLY", "ANNUAL"):
        raise invalid("basis is MONTHLY or ANNUAL.", "INVALID_PARAMS", "params")
    for key in ("rate", "units", "base", "total", "table", "tag", "code"):
        if key in params and params[key] is not None and not isinstance(params[key], str):
            raise invalid(f"params.{key} must be text.", "INVALID_PARAMS", "params")
    for key in ("bases", "others"):
        if key in params and params[key] is not None and (
            not isinstance(params[key], list) or not all(isinstance(c, str) for c in params[key])
        ):
            raise invalid(f"params.{key} must be a list of component codes.", "INVALID_PARAMS", "params")
    for key in ("progressive", "allow_negative_input"):
        if key in params and not isinstance(params[key], bool):
            raise invalid(f"params.{key} must be true or false.", "INVALID_PARAMS", "params")
    return {k: v for k, v in params.items() if v is not None}


@dataclass
class _Validated:
    values: dict[str, Any]
    warnings: list[dict[str, Any]]


def _validate(db: Session, company_id: int, code: str, kind: str, data: ComponentInput,
              previous: PayComponentVersion | None, on: dt.date) -> _Validated:
    def pick(name: str, default: Any = None) -> Any:
        value = getattr(data, name)
        if value is not None:
            return value
        if name in data.cleared:
            return None
        return getattr(previous, name) if previous is not None else default

    name = (pick("name") or "").strip()
    if not 1 <= len(name) <= 150:
        raise invalid("The component name must be 1-150 characters.", "INVALID_NAME", "name")
    translations = pick("translations", {}) or {}
    if not all(isinstance(k, str) and 2 <= len(k) <= 35 and isinstance(v, str) and 0 < len(v) <= 150
               for k, v in translations.items()):
        raise invalid("translations map a locale to a name of at most 150 characters.", "INVALID_VALUE", "translations")
    method = _choice(pick("calc_method"), CALC_METHODS, "calc_method")
    params = _params(method, pick("params", {}) or {})
    formula = pick("formula")
    if method != "FORMULA":
        formula = None
    proration = _choice(pick("proration", "NONE"), PRORATIONS, "proration")
    basis = pick("proration_basis")
    if basis is not None:
        _choice(basis, PRORATION_BASES, "proration_basis")
        if proration == "NONE":
            raise invalid("A proration basis needs a proration method.", "INVALID_VALUE", "proration_basis")
    rounding = pick("rounding")
    if rounding is not None:
        _choice(rounding, ROUNDING_MODES, "rounding")
    visibility = _choice(pick("visibility", "EMPLOYEE_VISIBLE"), VISIBILITIES, "visibility")
    taxability = pick("taxability", {}) or {}
    if not all(isinstance(k, str) and 2 <= len(k) <= 10 and v in TAXABILITY for k, v in taxability.items()):
        raise invalid("taxability maps a jurisdiction to TAXABLE, EXEMPT or PARTIAL.", "INVALID_VALUE", "taxability")
    reporting = pick("reporting_codes", {}) or {}
    if not all(isinstance(k, str) and isinstance(v, str) and len(k) <= 60 and len(v) <= 60 for k, v in reporting.items()):
        raise invalid("reporting_codes map a report to a code.", "INVALID_VALUE", "reporting_codes")
    approval = data.input_approval_above
    approval_amount = previous.input_approval_above if previous is not None and approval is None else None
    approval_currency = previous.input_approval_currency if previous is not None and approval is None else None
    if "input_approval_above" in data.cleared:
        approval_amount = approval_currency = None
    if approval is not None:
        approval_amount = parse_decimal(approval["amount"], "input_approval_above")
        approval_currency = approval["currency"]
        try:
            amount_str(approval_amount, approval_currency)
        except MoneyError as exc:
            raise invalid(exc.message, exc.code, "input_approval_above")

    currency = company_currency(db, company_id)
    entries = catalogue(db, company_id, on)
    stage = DEFAULT_STAGE[kind]
    tables = table_sigs(db, company_id, on)
    env = type_env(entries, currency, tables=tables, component=code, stage=stage, extra={code: stage})
    warnings: list[dict[str, Any]] = []
    text = method_text(method, params, formula, own=code)
    analyses = []
    for part in (text if isinstance(text, tuple) else (text,) if text else ()):
        expect = "ANY" if isinstance(text, tuple) else "AMOUNT"
        analysis = check_formula(part, type_env(entries, currency, tables=tables, component=code, stage=stage,
                                                expect=expect, extra={code: stage}), field_name="formula")
        analyses.append(analysis)
        warnings.extend(w.to_dict() for w in analysis.warnings)
    if formula is not None:
        formula = fm.render(fm.parse(formula))
    eligibility = pick("eligibility")
    if eligibility is not None:
        analysis = check_formula(eligibility, type_env(entries, currency, tables=tables, component=code, stage=stage,
                                                       expect="BOOLEAN", extra={code: stage}),
                                 field_name="eligibility")
        warnings.extend(w.to_dict() for w in analysis.warnings)
        eligibility = fm.render(fm.parse(eligibility))
    cap = _bound(pick("cap"), "cap", env)
    floor = _bound(pick("floor"), "floor", env)
    _check_cycles(entries, code, stage, analyses)
    return _Validated({
        "name": name, "translations": translations, "description": pick("description"),
        "calc_method": method, "formula": formula, "params": params,
        "tags": _tags(pick("tags", []) or []), "proration": proration, "proration_basis": basis,
        "frequency": _frequency(pick("frequency", {"type": "EVERY_PERIOD"}) or {"type": "EVERY_PERIOD"}),
        "taxability": taxability, "totals_flags": _totals(kind, data.totals_flags if data.totals_flags is not None
                                                          else (previous.totals_flags if previous else None)),
        "cap": cap, "floor": floor, "rounding": rounding, "eligibility": eligibility,
        "payslip": _typed_mapping(pick("payslip", {}) or {}, PAYSLIP_KEYS, "payslip"),
        "visibility": visibility,
        "gl": _typed_mapping(pick("gl"), GL_KEYS, "gl") if pick("gl") is not None else None,
        "reporting_codes": reporting, "input_approval_above": approval_amount,
        "input_approval_currency": approval_currency,
    }, warnings)


def _formula_deps(entry: CatalogueEntry) -> set[str]:
    version = entry.version
    try:
        text = method_text(version.calc_method, version.params or {}, version.formula, own=entry.code)
    except AppError:
        return set()
    deps: set[str] = set()
    for part in (text if isinstance(text, tuple) else (text,) if text else ()):
        try:
            expr = fm.parse(part)
        except fm.FormulaError:
            continue
        deps |= {node.code for node in fm.walk(expr) if isinstance(node, ComponentRef)}
    return deps


def _check_cycles(entries: Mapping[str, CatalogueEntry], code: str, stage: int, analyses: Sequence[fm.Analysis]) -> None:
    """Cycles through the component's default formula across the catalogue (EC-CMP-01)."""
    graph = {c: _formula_deps(e) for c, e in entries.items() if c != code}
    graph[code] = set().union(*(a.dependencies for a in analyses)) if analyses else set()
    stage_of = {c: e.stage for c, e in entries.items()}
    stage_of[code] = stage
    graph = {c: {d for d in reads if d in stage_of} for c, reads in graph.items()}
    try:
        fm.dependency_order(graph, stage_of, focus=code)
    except fm.FormulaError as error:
        if error.code in (fm.ErrorCode.CYCLE_DETECTED, fm.ErrorCode.STAGE_VIOLATION) and code in (error.path or []):
            raise formula_error(error, field_name="formula")


# --------------------------------------------------------------------------- serialization
def version_json(version: PayComponentVersion) -> dict[str, Any]:
    return {
        "id": version.id, "version": version.version, "name": version.name, "translations": version.translations or {},
        "description": version.description, "calc_method": version.calc_method, "formula": version.formula,
        "params": version.params or {}, "tags": list(version.tags or ()), "proration": version.proration,
        "proration_basis": version.proration_basis, "frequency": version.frequency, "taxability": version.taxability,
        "totals_flags": version.totals_flags, "cap": version.cap, "floor": version.floor, "rounding": version.rounding,
        "eligibility": version.eligibility, "payslip": version.payslip or {}, "visibility": version.visibility,
        "gl": version.gl, "reporting_codes": version.reporting_codes or {},
        "input_approval_above": None if version.input_approval_above is None else {
            "amount": amount_str(version.input_approval_above, version.input_approval_currency),
            "currency": version.input_approval_currency},
        "effective_from": version.effective_from, "effective_to": version.effective_to, "status": version.status,
        "created_by_user_id": version.created_by_user_id, "created_at": version.created_at,
    }


def component_json(component: PayComponent, version: PayComponentVersion | None, *, in_use: bool | None = None,
                   versions: Sequence[PayComponentVersion] | None = None) -> dict[str, Any]:
    data = {
        "id": component.id, "code": component.code, "kind": component.kind, "category": component.category,
        "owner": component.owner, "read_only": component.owner != COMPANY_OWNER, "status": component.status,
        "stage": DEFAULT_STAGE[component.kind], "name": version.name if version else component.code,
        "current_version": version_json(version) if version is not None else None,
    }
    if in_use is not None:
        data["in_use"] = in_use
    if versions is not None:
        data["versions"] = [version_json(v) for v in versions]
    return data


def component_def(entry: CatalogueEntry) -> dict[str, Any]:
    """The engine's ComponentDef JSON (snapshot `components`) for a catalogue entry."""
    version = entry.version
    frequency = version.frequency or {"type": "EVERY_PERIOD"}

    def amount(bound: Mapping[str, Any] | None) -> str | None:
        return bound.get("amount") if bound and "amount" in bound else None

    return {
        "code": entry.code, "kind": entry.component.kind, "category": entry.component.category,
        "version_id": version.id, "version": version.version, "name": version.name,
        "calc_method": version.calc_method, "tags": list(version.tags or ()), "proration": version.proration,
        "proration_basis": version.proration_basis, "frequency": frequency.get("type", "EVERY_PERIOD"),
        "months": frequency.get("months") or [], "rounding": version.rounding if version.rounding in (
            "HALF_UP", "HALF_EVEN", "UP", "DOWN") else None,
        "cap": amount(version.cap), "floor": amount(version.floor), "eligibility": version.eligibility,
        "visibility": version.visibility, "sort_order": int((version.payslip or {}).get("order") or 0),
        "owner": entry.component.owner, "stage": entry.stage,
    }


# --------------------------------------------------------------------------- queries
def in_use(db: Session, company_id: int, component_id: int) -> bool:
    """Used by a structure or an assignment (pay runs will add their lines here once they exist)."""
    for model in (SalaryTemplateLine, CompensationAssignmentLine):
        if db.query(model.id).filter(model.company_id == company_id, model.component_id == component_id).first():
            return True
    return component_in_runs(db, company_id, component_id)


def component_in_runs(db: Session, company_id: int, component_id: int) -> bool:
    """Hook for pay runs (EC-CMP-06): False until run lines exist."""
    return False


def in_use_ids(db: Session, company_id: int) -> set[int]:
    """Ids of the company's components used by a structure line or an assignment line (one query per table;
    the list's `in_use`, the same rule as `in_use`)."""
    used: set[int] = set()
    for model in (SalaryTemplateLine, CompensationAssignmentLine):
        used |= {cid for (cid,) in db.query(model.component_id).filter(model.company_id == company_id).distinct()}
    return used


def list_components(db: Session, principal: Principal, *, kind: str | None = None,
                    include_archived: bool = False) -> list[dict[str, Any]]:
    require_read(principal)
    cid = principal.company_id
    on = today(db, cid)
    entries = catalogue(db, cid, on, include_archived=include_archived)
    used = in_use_ids(db, cid)
    items = [component_json(e.component, e.version, in_use=e.component.id in used
                            or component_in_runs(db, cid, e.component.id))
             for e in entries.values() if kind is None or e.component.kind == kind]
    return sorted(items, key=lambda c: ((c["current_version"] or {}).get("payslip", {}).get("order") or 0, c["code"]))


def get_component(db: Session, principal: Principal, code: str) -> dict[str, Any]:
    require_read(principal)
    component = load_component(db, principal.company_id, code)
    versions = (
        db.query(PayComponentVersion)
        .filter(PayComponentVersion.company_id == principal.company_id, PayComponentVersion.component_id == component.id)
        .order_by(PayComponentVersion.version)
        .all()
    )
    current = version_on(db, component, today(db, principal.company_id))
    return component_json(component, current, in_use=in_use(db, principal.company_id, component.id), versions=versions)


# --------------------------------------------------------------------------- changes
def _audit(db: Session, principal: Principal | None, action: str, description: str, component: PayComponent,
           **details: Any) -> None:
    AuditLogService.log(db, principal, payroll_audit.COMPENSATION, action, description, "pay_component", component.id,
                        company_id=component.company_id, actor_type="user" if principal else "system",
                        details={"code": component.code, **details})


def _code(raw: str) -> str:
    code = (raw or "").strip()
    if not fm.is_valid_component_code(code) or len(code) > 40:
        raise invalid("A component code is UPPER_SNAKE (letters, digits, underscores; no double underscore), at most "
                      "40 characters.", "INVALID_CODE", "code")
    return code


def create_component(db: Session, principal: Principal | None, *, code: str, kind: str, category: str,
                     data: ComponentInput, owner: str = COMPANY_OWNER, company_id: int | None = None) -> dict[str, Any]:
    """Create a component with version 1 (principal None: system seeding)."""
    if principal is not None:
        require_company_write(principal)
        if owner != COMPANY_OWNER:
            raise forbidden("Pack-owned components come from the country pack.")
    cid = principal.company_id if principal is not None else company_id
    code = _code(code)
    _choice(kind, COMPONENT_KINDS, "kind")
    if category not in CATEGORIES_BY_KIND[kind]:
        raise invalid(f"{kind} components use the categories {', '.join(CATEGORIES_BY_KIND[kind])}.",
                      "INVALID_CATEGORY", "category")
    if db.query(PayComponent.id).filter(PayComponent.company_id == cid, PayComponent.code == code).first():
        raise conflict(f"A component with the code {code} exists.", "COMPONENT_CODE_TAKEN")
    if data.calc_method is None:
        raise invalid("calc_method is required.", "INVALID_VALUE", "calc_method")
    on = data.effective_from or today(db, cid)
    validated = _validate(db, cid, code, kind, data, None, on)
    component = PayComponent(company_id=cid, code=code, kind=kind, category=category, owner=owner, status="ACTIVE",
                             created_by_user_id=principal.user_id if principal else None)
    db.add(component)
    db.flush()
    version = PayComponentVersion(company_id=cid, component_id=component.id, version=1, effective_from=on,
                                  status="ACTIVE", created_by_user_id=principal.user_id if principal else None,
                                  **validated.values)
    db.add(version)
    db.flush()
    _audit(db, principal, COMPONENT_CREATED, f"Created pay component {code} ({validated.values['name']})", component,
           version=1, kind=kind, category=category, calc_method=version.calc_method, effective_from=on.isoformat())
    result = component_json(component, version, in_use=False, versions=[version])
    result["warnings"] = validated.warnings
    return result


def create_version(db: Session, principal: Principal, code: str, data: ComponentInput) -> dict[str, Any]:
    """A new version from `effective_from` (default today); a past date needs a reason."""
    require_company_write(principal)
    cid = principal.company_id
    component = load_component(db, cid, code, lock=True)
    if component.owner != COMPANY_OWNER:
        raise forbidden("This component belongs to a country pack and cannot be edited (EC-CMP-18).")
    if component.status != "ACTIVE":
        raise conflict("Archived components cannot change.", "COMPONENT_ARCHIVED")
    current_day = today(db, cid)
    start = data.effective_from or current_day
    if start < current_day and not (data.reason or "").strip():
        raise bad_request("A version effective in the past needs a reason.", "REASON_REQUIRED")
    versions = (
        db.query(PayComponentVersion)
        .filter(PayComponentVersion.company_id == cid, PayComponentVersion.component_id == component.id)
        .order_by(PayComponentVersion.version)
        .with_for_update()
        .all()
    )
    base = version_on(db, component, start) or versions[-1]
    validated = _validate(db, cid, component.code, component.kind, data, base, start)
    end = None
    for version in versions:
        if version.status != "ACTIVE":
            continue
        if version.effective_from >= start:
            if version.effective_from > start and version.effective_from > current_day and end is None:
                end = version.effective_from - ONE_DAY  # a scheduled later version stays and bounds this one
                continue
            version.status = "SUPERSEDED"
        elif version.effective_to is None or version.effective_to >= start:
            version.effective_to = start - ONE_DAY
    db.flush()
    new = PayComponentVersion(company_id=cid, component_id=component.id, version=versions[-1].version + 1,
                              effective_from=start, effective_to=end, status="ACTIVE",
                              created_by_user_id=principal.user_id, **validated.values)
    db.add(new)
    db.flush()
    before = version_json(base)
    after = version_json(new)
    changed = sorted(k for k in after if k not in ("id", "version", "effective_from", "effective_to", "status",
                                                    "created_by_user_id", "created_at") and before.get(k) != after.get(k))
    _audit(db, principal, COMPONENT_VERSION_CREATED,
           f"Created version {new.version} of pay component {component.code} from {start.isoformat()}", component,
           version=new.version, effective_from=start.isoformat(), changed=changed, reason=data.reason,
           before={k: before[k] for k in changed}, after={k: after[k] for k in changed})
    result = get_component(db, principal, component.code)
    result["warnings"] = validated.warnings
    return result


def update_component(db: Session, principal: Principal, code: str, *, new_code: str | None = None,
                     status: str | None = None) -> dict[str, Any]:
    """Rename an unused component (EC-CMP-06) or archive / restore it."""
    require_company_write(principal)
    cid = principal.company_id
    component = load_component(db, cid, code, lock=True)
    if component.owner != COMPANY_OWNER:
        raise forbidden("This component belongs to a country pack and cannot be edited (EC-CMP-18).")
    changes: dict[str, Any] = {}
    if new_code is not None and new_code != component.code:
        new_code = _code(new_code)
        if in_use(db, cid, component.id):
            raise conflict("The code of a component that is in use cannot change (EC-CMP-06).", "COMPONENT_CODE_IMMUTABLE")
        if db.query(PayComponent.id).filter(PayComponent.company_id == cid, PayComponent.code == new_code).first():
            raise conflict(f"A component with the code {new_code} exists.", "COMPONENT_CODE_TAKEN")
        changes["code"] = {"before": component.code, "after": new_code}
        component.code = new_code
    if status is not None and status != component.status:
        _choice(status, ("ACTIVE", "ARCHIVED"), "status")
        changes["status"] = {"before": component.status, "after": status}
        component.status = status
    if changes:
        db.flush()
        _audit(db, principal, COMPONENT_UPDATED, f"Updated pay component {component.code}", component, changes=changes)
    return get_component(db, principal, component.code)


# --------------------------------------------------------------------------- formula validation and tester
def _env_for(db: Session, principal: Principal, *, currency: str | None, component: str | None, kind: str | None,
             expect: str, structure: Sequence[str] | None = None, on: dt.date | None = None) -> fm.TypeEnv:
    cid = principal.company_id
    on = on or today(db, cid)
    try:
        currency = currencies.require_payroll_currency(currency or company_currency(db, cid)).code
    except MoneyError as exc:
        raise invalid(exc.message, exc.code, "currency")
    entries = catalogue(db, cid, on)
    if kind is not None:
        _choice(kind, COMPONENT_KINDS, "kind")
    stage = DEFAULT_STAGE[kind] if kind else (entries[component].stage if component in entries else None)
    extra = {component: stage or 4} if component and component not in entries else None
    if expect not in ("AMOUNT", "NUMBER", "BOOLEAN", "ANY"):
        raise invalid("expect is AMOUNT, NUMBER, BOOLEAN or ANY.", "INVALID_VALUE", "expect")
    return type_env(entries, currency, tables=table_sigs(db, cid, on), component=component, stage=stage,
                    expect=expect, structure=structure, extra=extra)


def validate_formula(db: Session, principal: Principal, formula: str, *, currency: str | None = None,
                     component: str | None = None, kind: str | None = None, expect: str = "AMOUNT") -> dict[str, Any]:
    """The editor's live check: every error and warning, the type and the references (never raises for formula errors)."""
    require_read(principal)
    env = _env_for(db, principal, currency=currency, component=component, kind=kind, expect=expect)
    analysis = fm.diagnose(formula, env)
    data = analysis.to_dict()
    if analysis.ok and component:
        entries = catalogue(db, principal.company_id, today(db, principal.company_id))
        stage = env.stage or 4
        try:
            _check_cycles(entries, component, stage, [analysis])
        except AppError as exc:
            data["errors"] = list(exc.detail.get("errors") or [])
    data["valid"] = not data["errors"]
    data["canonical"] = fm.render(analysis.expr) if analysis.expr is not None and data["valid"] else None
    return data


def _sample_value(name: str, raw: Any, currency: str) -> Any:
    definition = VARIABLES.get(name)
    if raw is None:
        return None
    kind = definition.type.kind if definition is not None else None
    if isinstance(raw, Mapping) and set(raw) == {"amount", "currency"}:
        return fm.MoneyValue(parse_decimal(raw["amount"], name, allow_negative=True), raw["currency"])
    if kind == fm.Kind.MONEY:
        return fm.MoneyValue(parse_decimal(raw, name, allow_negative=True), currency)
    if kind in (fm.Kind.DECIMAL, fm.Kind.INTEGER):
        value = parse_decimal(raw, name, allow_negative=True)
        return int(value) if kind == fm.Kind.INTEGER and value == value.to_integral_value() else value
    if kind == fm.Kind.BOOLEAN:
        if not isinstance(raw, bool):
            raise invalid(f"{name} must be true or false.", "INVALID_SAMPLE", name)
        return raw
    if kind == fm.Kind.DATE:
        try:
            return dt.date.fromisoformat(raw)
        except (TypeError, ValueError):
            raise invalid(f"{name} must be a date (YYYY-MM-DD).", "INVALID_SAMPLE", name)
    if definition is None and not name.startswith("emp.attr."):
        raise invalid(f"{name} is not a formula variable.", "UNKNOWN_REFERENCE", name)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        raise invalid(f"{name} must be a decimal string.", "INVALID_SAMPLE", name)
    return raw


def _money_map(raw: Mapping[str, Any] | None, currency: str, what: str) -> dict[str, Any]:
    result = {}
    for code, value in (raw or {}).items():
        if value is None:
            result[code] = None
        elif isinstance(value, Mapping):
            result[code] = fm.MoneyValue(parse_decimal(value.get("amount"), f"{what}.{code}", allow_negative=True),
                                         value.get("currency") or currency)
        else:
            result[code] = fm.MoneyValue(parse_decimal(value, f"{what}.{code}", allow_negative=True), currency)
    return result


def run_tester(db: Session, principal: Principal, code: str, *, formula: str | None = None,
                 currency: str | None = None, sample: Mapping[str, Any] | None = None,
                 employee_id: int | None = None, period_start: dt.date | None = None,
                 period_end: dt.date | None = None) -> dict[str, Any]:
    """Evaluate a formula (default: the component's current one) with sample values or a real employee."""
    require_read(principal)
    cid = principal.company_id
    component = load_component(db, cid, code)
    version = version_on(db, component, today(db, cid))
    if formula is None:
        text = method_text(version.calc_method, version.params or {}, version.formula, own=code) if version else None
        if text is None:
            raise invalid(f"{code} has no formula to test (method {version.calc_method if version else '-'}).",
                          "NO_FORMULA", "formula")
        formula = text if isinstance(text, str) else f"({text[0]}) * ({text[1]})"
    sample = dict(sample or {})
    unknown = sorted(set(sample) - {"components", "rates", "variables", "inputs", "units", "prev", "ytd"})
    if unknown:
        raise invalid(f"sample has unknown keys: {', '.join(unknown)}.", "INVALID_SAMPLE", "sample")
    source: dict[str, Any] = {"type": "SAMPLE"}
    if employee_id is not None:
        from app.payroll.services import structures

        employee_sample, currency, source = structures.employee_sample(
            db, principal, employee_id, period_start=period_start, period_end=period_end)
        for key, values in employee_sample.items():
            sample.setdefault(key, {})
            sample[key] = {**values, **(sample.get(key) or {})}
    try:
        pay_currency = currencies.require_payroll_currency(currency or company_currency(db, cid)).code
    except MoneyError as exc:
        raise invalid(exc.message, exc.code, "currency")
    entries = catalogue(db, cid, today(db, cid))
    components = _money_map(sample.get("components"), pay_currency, "components")
    for other in entries:
        if other != code:
            components.setdefault(other, None)
    ctx = fm.EvalContext.build(
        pay_currency, components=components, rates=_money_map(sample.get("rates"), pay_currency, "rates"),
        variables={k: _sample_value(k, v, pay_currency) for k, v in (sample.get("variables") or {}).items()},
        inputs=_money_map(sample.get("inputs"), pay_currency, "inputs"),
        units={k: parse_decimal(v, f"units.{k}", allow_negative=True) for k, v in (sample.get("units") or {}).items()},
        prev=_money_map(sample.get("prev"), pay_currency, "prev"), ytd=_money_map(sample.get("ytd"), pay_currency, "ytd"),
        tags={tag: [c for c, e in entries.items() if tag in (e.version.tags or ())]
              for e in entries.values() for tag in (e.version.tags or ())},
        component=code, stage=DEFAULT_STAGE[component.kind],
        minor_units={pay_currency: currencies.get(pay_currency).minor_unit or 0},
    )
    analysis = fm.diagnose(formula, ctx.env)
    if analysis.errors:
        return {"ok": False, "formula": formula, "value": None, "error": analysis.errors[0].to_dict(),
                "errors": [i.to_dict() for i in analysis.errors], "warnings": [w.to_dict() for w in analysis.warnings],
                "references": {}, "steps": [], "source": source}
    result = fm.evaluate(analysis.expr, ctx)
    data = result.to_dict()
    return {"ok": result.ok, "formula": fm.render(analysis.expr), "value": data.get("value"),
            "type": data.get("type"), "error": result.error.to_dict() if result.error else None,
            "references": result.references, "steps": result.steps,
            "warnings": [w.to_dict() for w in analysis.warnings], "source": source}

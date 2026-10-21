"""Salary structures (templates): versions, lines, validation, preview, publish, move (3.3 §6, 3.8 §5.8).

* A structure has versions: one DRAFT at a time is edited (row_version for concurrent edits), publishing
  validates it and makes it the ACTIVE version (the previous ACTIVE one is ARCHIVED: existing
  assignments keep it until moved explicitly, EC-CMP-17). Published versions are frozen (also by a
  database trigger).
* Lines copy the component's method and parameters when they omit them, so a published version never
  changes when a component does.
* Validation on publish (3.3 §6.2): every component exists; every formula checks (references, types,
  currency, stages, protected attributes, limits); no cycles (path reported, EC-CMP-01); the balancing
  component is >= 0 over the declared range, otherwise the minimum pay basis is reported (EC-CMP-04,
  F-08); one currency; pack checks (hook); every saved test vector passes.
* Evaluation runs the real calculation engine on a one-period snapshot (a full month, no loss of pay),
  so previews, test vectors and the balancing check agree with payroll to the minor unit. Statutory
  lines appear only through the country-pack hook of the engine in preview mode and carry its labels
  ("Compliance not verified" unless the pack version is verified).

Reading needs payroll:salary_structure:read, editing :edit, publishing :edit or :approve, at COMPANY
scope or a LEGAL_ENTITY scope whose targets cover the structure's eligible entities.
"""

from __future__ import annotations

import calendar
import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.permission_registry import Scope
from app.core.principal import Principal
from app.core.security import utcnow
from app.models.compensation import (
    STRUCTURE_TYPES,
    CompensationAssignment,
    PayComponent,
    SalaryTemplate,
    SalaryTemplateLine,
    SalaryTemplateVersion,
)
from app.models.org import LegalEntity
from app.models.pay_org import EmploymentType, Grade, PayGroup
from app.payroll import audit as payroll_audit
from app.payroll import currencies
from app.payroll import engine
from app.payroll import formula as fm
from app.payroll.engine.lines import CalcIssue
from app.payroll.money import amount_str, decimal_str, quantize_amount
from app.payroll.money_errors import MoneyError
from app.payroll.services import components as comp
from app.services.audit_log_service import AuditLogService

READ = comp.READ
EDIT = comp.WRITE
APPROVE = "payroll:salary_structure:approve"
MAX_LINES = 60
MAX_VECTORS = 50
SEARCH_ITERATIONS = 60
ELIGIBILITY_KEYS = {
    "legal_entity_ids": LegalEntity, "pay_group_ids": PayGroup, "grade_ids": Grade,
    "employment_type_ids": EmploymentType,
}
STRUCTURE_ISSUES = ("FORMULA_ERROR", "DEPENDS_ON_FAILED", "NEGATIVE_RESULT", "NO_SALARY", "CURRENCY_MISMATCH",
                    "MISSING_AMOUNT", "GROSS_UP_NOT_CONVERGED")
PREVIEW_SETTINGS = {
    "payroll.lop.proration_basis": "CALENDAR_DAYS",
    "payroll.attendance.enabled": False,
    "payroll.leave.enabled": False,
    "payroll.overtime.enabled": False,
    "payroll.wage_inputs.enabled": True,
    "payroll.reimbursements.enabled": False,
    "payroll.bank.split_payments_enabled": False,
}
STATUTORY_NOTE = "Statutory estimates come from the country pack in preview mode and are not verified."

STRUCTURE_CREATED = "STRUCTURE_CREATED"
STRUCTURE_VERSION_CREATED = "STRUCTURE_VERSION_CREATED"
STRUCTURE_VERSION_UPDATED = "STRUCTURE_VERSION_UPDATED"
STRUCTURE_PUBLISHED = "STRUCTURE_PUBLISHED"
STRUCTURE_ARCHIVED = "STRUCTURE_ARCHIVED"


# --------------------------------------------------------------------------- hooks
def pack_checks(db: Session, company_id: int, template: SalaryTemplate, version: SalaryTemplateVersion,
                lines: Sequence[dict[str, Any]], countries: Sequence[str]) -> list[dict[str, Any]]:
    """Country-pack structure checks (3.3 §6.2, e.g. statutory wage definition vs basic): a hook returning
    warnings/errors as {"code", "severity", "message"}. Packs expose no structure rules yet: empty."""
    return []


def ctc_employer_contributions(db: Session, company_id: int, structure_type: str, currency: str,
                               lines: Sequence[dict[str, Any]], country: str | None) -> dict[str, str]:
    """Employer contributions inside CTC that BALANCING subtracts ({code: monthly amount}), from a verified
    pack; none until packs publish them, so a CTC structure's balancing component includes them."""
    return {}


# --------------------------------------------------------------------------- access
def _targets(principal: Principal, code: str) -> frozenset[int] | None:
    """None = whole company; otherwise the legal entities the grant covers (empty = nothing)."""
    scopes = principal.scopes(code)
    if Scope.COMPANY.value in scopes:
        return None
    if Scope.LEGAL_ENTITY.value in scopes:
        return principal.scope_targets(code, Scope.LEGAL_ENTITY.value)
    return frozenset()


def _covers(targets: frozenset[int] | None, eligibility: Mapping[str, Any], *, write: bool) -> bool:
    if targets is None:
        return True
    entities = set(eligibility.get("legal_entity_ids") or [])
    if not entities:
        return not write  # company-wide structures: readable by entity-scoped holders, editable at COMPANY only
    return entities <= targets if write else bool(entities & targets)


def can_read(principal: Principal, eligibility: Mapping[str, Any]) -> bool:
    return principal.has(READ) and _covers(_targets(principal, READ), eligibility, write=False)


def _require_edit(principal: Principal, eligibility: Mapping[str, Any], *, publish: bool = False) -> None:
    codes = (EDIT, APPROVE) if publish else (EDIT,)
    if not any(principal.has(code) for code in codes):
        raise forbidden()
    if not any(principal.has(code) and _covers(_targets(principal, code), eligibility, write=True) for code in codes):
        raise forbidden("Changing this structure needs salary structure access covering all of its legal entities.")


def _load_template(db: Session, principal: Principal, template_id: int, *, lock: bool = False) -> SalaryTemplate:
    if not any(principal.has(code) for code in (READ, EDIT, APPROVE)):
        raise forbidden()
    query = db.query(SalaryTemplate).filter(SalaryTemplate.company_id == principal.company_id,
                                            SalaryTemplate.id == template_id)
    if lock:
        query = query.with_for_update()
    template = query.first()
    if template is None:
        raise not_found("Salary structure")
    latest = _versions(db, template)
    eligibility = latest[-1].eligibility if latest else {}
    if not (can_read(principal, eligibility) or any(
            principal.has(code) and _covers(_targets(principal, code), eligibility, write=False)
            for code in (EDIT, APPROVE))):
        raise not_found("Salary structure")
    return template


def _versions(db: Session, template: SalaryTemplate, *, lock: bool = False) -> list[SalaryTemplateVersion]:
    query = (db.query(SalaryTemplateVersion)
             .filter(SalaryTemplateVersion.company_id == template.company_id,
                     SalaryTemplateVersion.template_id == template.id)
             .order_by(SalaryTemplateVersion.version))
    if lock:
        query = query.with_for_update()
    return query.all()


def _version(db: Session, template: SalaryTemplate, number: int | None, *, lock: bool = False) -> SalaryTemplateVersion:
    versions = _versions(db, template, lock=lock)
    if number is None:
        found = next((v for v in reversed(versions) if v.status == "ACTIVE"), versions[-1] if versions else None)
    else:
        found = next((v for v in versions if v.version == number), None)
    if found is None:
        raise not_found("Salary structure version")
    return found


# --------------------------------------------------------------------------- lines
@dataclass
class LineInput:
    component: str
    method: str | None = None
    value: Decimal | None = None
    formula: str | None = None
    params: dict[str, Any] | None = None
    overridable: bool = False
    override_min: Decimal | None = None
    override_max: Decimal | None = None
    display_order: int | None = None


def _line_rows(db: Session, version: SalaryTemplateVersion) -> list[SalaryTemplateLine]:
    return (db.query(SalaryTemplateLine)
            .filter(SalaryTemplateLine.company_id == version.company_id,
                    SalaryTemplateLine.template_version_id == version.id)
            .order_by(SalaryTemplateLine.display_order, SalaryTemplateLine.id)
            .all())


def line_dicts(db: Session, version: SalaryTemplateVersion) -> list[dict[str, Any]]:
    """The version's lines with their component code, kind and name (in display order)."""
    rows = _line_rows(db, version)
    ids = {row.component_id for row in rows}
    components = {c.id: c for c in db.query(PayComponent).filter(PayComponent.company_id == version.company_id,
                                                                 PayComponent.id.in_(sorted(ids)))} if ids else {}
    return [{
        "id": row.id, "component_id": row.component_id, "component": components[row.component_id].code,
        "kind": components[row.component_id].kind, "category": components[row.component_id].category,
        "method": row.method, "value": comp.plain(row.value),
        "formula": row.formula, "params": row.params or {}, "overridable": row.overridable,
        "override_min": comp.plain(row.override_min), "override_max": comp.plain(row.override_max),
        "display_order": row.display_order,
    } for row in rows]


def _normalize_lines(db: Session, company_id: int, items: Sequence[LineInput], on: dt.date,
                     structure_type: str) -> list[dict[str, Any]]:
    if len(items) > MAX_LINES:
        raise comp.invalid(f"A structure has at most {MAX_LINES} lines.", "TOO_MANY_LINES", "lines")
    entries = comp.catalogue(db, company_id, on)
    seen: set[str] = set()
    result = []
    for index, item in enumerate(items):
        entry = entries.get(item.component)
        if entry is None:
            raise comp.invalid(f"Line {index + 1}: {item.component} is not an active pay component.",
                               "UNKNOWN_COMPONENT", "lines")
        if item.component in seen:
            raise comp.invalid(f"{item.component} appears twice in the structure.", "DUPLICATE_COMPONENT", "lines")
        seen.add(item.component)
        version = entry.version
        method = item.method or version.calc_method
        comp._choice(method, ("FIXED", "PERCENT_OF", "FORMULA", "RATE_X_UNITS", "SLAB", "INPUT", "BALANCING",
                              "SYSTEM"), "method")
        if item.method is None or item.method == version.calc_method:
            params = dict(version.params or {}) if item.params is None else dict(item.params)
            formula = item.formula if item.formula is not None else version.formula
        else:
            params, formula = dict(item.params or {}), item.formula
        params = comp._params(method, params)
        value = item.value
        if method == "PERCENT_OF":
            if value is None and params.get("percent") is not None:
                value = Decimal(params["percent"])
            params.pop("percent", None)
            if value is None:
                raise comp.invalid(f"{item.component}: PERCENT_OF needs a percentage.", "VALUE_REQUIRED", "lines")
        elif method == "FIXED":
            if value is None and not item.overridable:
                raise comp.invalid(f"{item.component}: a FIXED line needs an amount or must be overridable so each "
                                   "assignment gives it.", "VALUE_REQUIRED", "lines")
        elif value is not None and method != "RATE_X_UNITS":
            raise comp.invalid(f"{item.component}: {method} lines take no value.", "INVALID_VALUE", "lines")
        if value is not None and value < 0:
            raise comp.invalid(f"{item.component}: the value cannot be negative.", "INVALID_VALUE", "lines")
        if method != "FORMULA":
            formula = None
        elif not formula:
            raise comp.invalid(f"{item.component}: a FORMULA line needs a formula.", "FORMULA_REQUIRED", "lines")
        if not item.overridable and (item.override_min is not None or item.override_max is not None):
            raise comp.invalid(f"{item.component}: override bounds need an overridable line.", "INVALID_BOUNDS", "lines")
        if item.overridable and method not in ("FIXED", "PERCENT_OF", "FORMULA", "RATE_X_UNITS"):
            raise comp.invalid(f"{item.component}: only FIXED, PERCENT_OF, FORMULA and RATE_X_UNITS lines can be "
                               "overridden.", "NOT_OVERRIDABLE", "lines")
        if item.override_min is not None and item.override_max is not None and item.override_max < item.override_min:
            raise comp.invalid(f"{item.component}: override_max is below override_min.", "INVALID_BOUNDS", "lines")
        result.append({
            "component": item.component, "component_id": entry.component.id, "kind": entry.component.kind,
            "method": method, "value": value, "formula": fm.render(fm.parse(formula)) if formula else None,
            "params": params, "overridable": item.overridable, "override_min": item.override_min,
            "override_max": item.override_max,
            "display_order": item.display_order if item.display_order is not None else (index + 1) * 10,
        })
    if structure_type in ("CTC_BASED", "GROSS_BASED", "NET_BASED") and \
            sum(1 for line in result if line["method"] == "BALANCING") > 1:
        raise comp.invalid("A structure has at most one balancing component.", "TOO_MANY_BALANCING", "lines")
    return result


def _write_lines(db: Session, version: SalaryTemplateVersion, lines: Sequence[dict[str, Any]]) -> None:
    for row in _line_rows(db, version):
        db.delete(row)
    db.flush()
    for line in lines:
        db.add(SalaryTemplateLine(
            company_id=version.company_id, template_version_id=version.id, component_id=line["component_id"],
            method=line["method"], value=line["value"], formula=line["formula"], params=line["params"],
            overridable=line["overridable"], override_min=line["override_min"], override_max=line["override_max"],
            display_order=line["display_order"],
        ))
    db.flush()


def _as_inputs(lines: Sequence[dict[str, Any]]) -> list[LineInput]:
    def dec(raw: Any) -> Decimal | None:
        return None if raw is None else Decimal(str(raw))

    return [LineInput(line["component"], line["method"], dec(line["value"]), line["formula"], dict(line["params"]),
                      line["overridable"], dec(line["override_min"]), dec(line["override_max"]),
                      line["display_order"]) for line in lines]


# --------------------------------------------------------------------------- eligibility, vectors, range
def _eligibility(db: Session, company_id: int, raw: Mapping[str, Any] | None) -> dict[str, list[int]]:
    result: dict[str, list[int]] = {}
    for key, value in (raw or {}).items():
        model = ELIGIBILITY_KEYS.get(key)
        if model is None:
            raise comp.invalid(f"eligibility has an unknown key {key!r}.", "INVALID_ELIGIBILITY", "eligibility")
        if not isinstance(value, list) or any(isinstance(i, bool) or not isinstance(i, int) for i in value):
            raise comp.invalid(f"eligibility.{key} is a list of ids.", "INVALID_ELIGIBILITY", "eligibility")
        ids = sorted(set(value))
        if ids:
            found = {i for (i,) in db.query(model.id).filter(model.company_id == company_id, model.id.in_(ids))}
            if found != set(ids):
                raise comp.invalid(f"eligibility.{key} names unknown records.", "INVALID_ELIGIBILITY", "eligibility")
            result[key] = ids
    return result


def _test_range(raw: Mapping[str, Any] | None) -> dict[str, str] | None:
    if raw is None:
        return None
    if set(raw) != {"min", "max"}:
        raise comp.invalid("test_range is {\"min\", \"max\"}.", "INVALID_RANGE", "test_range")
    low, high = comp.parse_decimal(raw["min"], "test_range.min"), comp.parse_decimal(raw["max"], "test_range.max")
    if high < low:
        raise comp.invalid("test_range.max is below test_range.min.", "INVALID_RANGE", "test_range")
    return {"min": decimal_str(low), "max": decimal_str(high)}


def _vectors(raw: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    vectors = []
    for index, item in enumerate(raw or []):
        if len(vectors) >= MAX_VECTORS:
            raise comp.invalid(f"At most {MAX_VECTORS} test vectors.", "INVALID_VECTOR", "test_vectors")
        if not isinstance(item, Mapping) or set(item) - {"name", "pay_basis_amount", "expected", "overrides"} or \
                "pay_basis_amount" not in item or not isinstance(item.get("expected"), Mapping) or not item["expected"]:
            raise comp.invalid(f"Test vector {index + 1} is {{name, pay_basis_amount, expected: {{CODE: amount}}, "
                               "overrides?}}.", "INVALID_VECTOR", "test_vectors")
        expected = {str(code): decimal_str(comp.parse_decimal(value, f"expected.{code}", allow_negative=True))
                    for code, value in item["expected"].items()}
        overrides = {str(code): str(value) for code, value in (item.get("overrides") or {}).items()}
        vectors.append({"name": str(item.get("name") or f"Vector {index + 1}")[:100],
                        "pay_basis_amount": decimal_str(comp.parse_decimal(item["pay_basis_amount"], "pay_basis_amount")),
                        "expected": expected, "overrides": overrides})
    return vectors


# --------------------------------------------------------------------------- evaluation
@dataclass
class Evaluation:
    currency: str
    pay_basis_amount: Decimal
    periods_per_year: int
    rates: dict[str, Decimal]
    amounts: dict[str, Decimal]
    lines: list[Any]
    issues: list[CalcIssue]
    result: Any = None
    statutory: list[Any] = field(default_factory=list)

    @property
    def blocking(self) -> list[CalcIssue]:
        return [i for i in self.issues if i.severity == "BLOCKING" and (i.code in STRUCTURE_ISSUES or i.component)]

    @property
    def ok(self) -> bool:
        return not self.blocking


def month_of(on: dt.date) -> tuple[dt.date, dt.date]:
    return on.replace(day=1), on.replace(day=calendar.monthrange(on.year, on.month)[1])


def compensation_dict(structure_type: str, currency: str, lines: Sequence[Mapping[str, Any]],
                      pay_basis_amount: Decimal, overrides: Mapping[str, Mapping[str, Any]] | None = None, *,
                      employer_contributions: Mapping[str, str] | None = None, ids: Mapping[str, Any] | None = None
                      ) -> dict[str, Any]:
    """The engine snapshot's `compensation` (snapshot.Compensation) for a structure version and an
    assignment: overrides ({code: {amount|percent|formula}}) replace overridable values. HOURLY / DAILY
    RATE_X_UNITS lines without a rate are paid at the pay basis rate per payable hour / paid day.
    Raises AppError MISSING_AMOUNT when a FIXED line has no amount."""
    overrides = overrides or {}
    result_lines = []
    for line in lines:
        code, method = line["component"], line["method"]
        params = dict(line.get("params") or {})
        override = overrides.get(code) or {}
        value = line.get("value")
        value = None if value is None else Decimal(str(value))
        entry: dict[str, Any] = {"component": code, "method": method, "order": int(line.get("display_order") or 0)}
        if method == "FIXED":
            amount = override.get("amount", value)
            if amount is None:
                raise AppError(400, f"{code} needs an amount: the structure leaves it to the assignment.",
                               "MISSING_AMOUNT")
            if params.get("basis") == "ANNUAL" and "amount" not in override:
                entry["annual_amount"] = decimal_str(Decimal(str(amount)))
            else:
                entry["amount"] = decimal_str(Decimal(str(amount)))
            params = {}
        elif method == "PERCENT_OF":
            params["percent"] = decimal_str(Decimal(str(override.get("percent", value))))
        elif method == "FORMULA":
            params = {"formula": override.get("formula") or line.get("formula")}
        elif method == "RATE_X_UNITS":
            rate = override.get("amount", value)
            if rate is not None:
                params["rate"] = decimal_str(Decimal(str(rate)))
            elif not params.get("rate") and structure_type in ("HOURLY", "DAILY"):
                params["rate"] = decimal_str(pay_basis_amount)
            if not params.get("units") and structure_type in ("HOURLY", "DAILY"):
                params["units"] = "att.payable_hours" if structure_type == "HOURLY" else "att.paid_days"
        elif method == "INPUT":
            params.setdefault("code", code)
        entry["params"] = params
        result_lines.append(entry)
    data = {"structure_type": structure_type, "currency": currency, "pay_basis_amount": decimal_str(pay_basis_amount),
            "ctc_employer_contributions": dict(employer_contributions or {}), "lines": result_lines}
    data.update({k: v for k, v in (ids or {}).items() if v is not None})
    return data


def _statutory_pack(country: str | None, start: dt.date) -> dict[str, Any] | None:
    """The country-pack hook of previews: the pack version for the country (the engine labels its lines)."""
    if not country:
        return None
    try:
        from app.payroll import packs

        found = packs.packs_for_country(country, on=start, environment=app_settings.environment)
        return packs.pack_snapshot(found[-1]) if found else None
    except Exception:  # a broken pack never breaks a preview; the engine reports NO_PACK instead
        return None


def evaluate(
    db: Session, company_id: int, *, structure_type: str, currency: str, lines: Sequence[Mapping[str, Any]],
    pay_basis_amount: Decimal, overrides: Mapping[str, Mapping[str, Any]] | None = None, on: dt.date | None = None,
    employee: Mapping[str, Any] | None = None, country: str | None = None, statutory: bool = False,
    ids: Mapping[str, Any] | None = None,
) -> Evaluation:
    """Calculate one full month (no loss of pay) of a structure with the engine."""
    on = on or comp.today(db, company_id)
    start, end = month_of(on)
    entries = comp.catalogue(db, company_id, on)
    employer = ctc_employer_contributions(db, company_id, structure_type, currency, lines, country)
    compensation = compensation_dict(structure_type, currency, lines, pay_basis_amount, overrides,
                                     employer_contributions=employer, ids=ids)
    days = []
    when = start
    while when <= end:
        days.append({"date": when.isoformat(), "weekly_off": False, "segment": "S1", "employed": True})
        when += dt.timedelta(days=1)
    settings = dict(PREVIEW_SETTINGS)
    if structure_type == "NET_BASED":
        settings["payroll.structures.net_based_enabled"] = True
    snapshot = {
        "run": {"run_type": "PREVIEW", "environment": app_settings.environment},
        "employee": dict(employee or {"employee_id": "PREVIEW", "hire_date": (start - dt.timedelta(days=366)).isoformat(),
                                      "country": country or "ZZ"}),
        "period": {"start": start.isoformat(), "end": end.isoformat(), "pay_date": end.isoformat()},
        "currency": currency,
        "settings": settings,
        "components": [comp.component_def(entry) for entry in entries.values()],
        "segments": [{"key": "S1", "start": start.isoformat(), "end": end.isoformat(), "compensation": compensation}],
        "days": days,
        "pack": _statutory_pack(country, start) if statutory else None,
    }
    try:
        result = engine.calculate(snapshot)
    except engine.SnapshotError as error:
        raise AppError(400, {"message": error.message, "path": error.path}, "INVALID_STRUCTURE")
    codes = [line["component"] for line in lines]
    amounts: dict[str, Decimal] = {}
    for line in result.lines:
        amounts[line.component] = amounts.get(line.component, Decimal(0)) + line.amount
    rates = {code: Decimal(result.rates[code]) for code in codes if code in result.rates}
    for code in codes:
        if code not in rates and code in amounts:
            rates[code] = amounts[code]
    return Evaluation(currency, pay_basis_amount, 12, rates, amounts,
                      [line for line in result.lines if line.component in codes],
                      list(result.exceptions) + list(result.flags), result,
                      [line for line in result.lines if line.statutory])


def _balancing_code(lines: Sequence[Mapping[str, Any]]) -> str | None:
    return next((line["component"] for line in lines if line["method"] == "BALANCING"), None)


def _balancing_ok(evaluation: Evaluation, code: str) -> bool:
    if any(i.component == code and i.severity == "BLOCKING" for i in evaluation.issues):
        return False
    return evaluation.rates.get(code, Decimal(0)) >= 0


def minimum_pay_basis(db: Session, company_id: int, *, structure_type: str, currency: str,
                      lines: Sequence[Mapping[str, Any]], low: Decimal, high: Decimal, on: dt.date | None = None,
                      overrides: Mapping[str, Mapping[str, Any]] | None = None) -> Decimal | None:
    """The smallest pay basis in [low, high] (to the minor unit) at which the balancing component is >= 0,
    by bisection (balancing grows with the pay basis); None when it is negative even at `high` (EC-CMP-04)."""
    code = _balancing_code(lines)
    if code is None:
        return low

    def ok(amount: Decimal) -> bool:
        return _balancing_ok(evaluate(db, company_id, structure_type=structure_type, currency=currency, lines=lines,
                                      pay_basis_amount=amount, overrides=overrides, on=on), code)

    if ok(low):
        return low
    if not ok(high):
        return None
    unit = Decimal(1).scaleb(-(currencies.get(currency).minor_unit or 0))
    for _ in range(SEARCH_ITERATIONS):
        if high - low <= unit:
            break
        middle = quantize_amount((low + high) / 2, currency, "FLOOR")
        if middle <= low:
            middle = low + unit
        if ok(middle):
            high = middle
        else:
            low = middle
    return high.quantize(unit, rounding=ROUND_CEILING)


# --------------------------------------------------------------------------- validation
def _countries(db: Session, company_id: int, eligibility: Mapping[str, Any]) -> list[str]:
    entity_ids = set(eligibility.get("legal_entity_ids") or [])
    pay_groups = eligibility.get("pay_group_ids") or []
    if pay_groups:
        entity_ids |= {le for (le,) in db.query(PayGroup.legal_entity_id).filter(PayGroup.company_id == company_id,
                                                                                    PayGroup.id.in_(pay_groups))}
    query = db.query(LegalEntity.country_code).filter(LegalEntity.company_id == company_id)
    if entity_ids:
        query = query.filter(LegalEntity.id.in_(sorted(entity_ids)))
    else:
        query = query.filter(LegalEntity.is_default.is_(True))
    return sorted({c for (c,) in query})


def structure_currency(db: Session, company_id: int, template: SalaryTemplate,
                       eligibility: Mapping[str, Any]) -> tuple[str, list[str]]:
    """The structure's currency and the distinct currencies of its eligible pay groups / entities."""
    found: set[str] = set()
    if eligibility.get("pay_group_ids"):
        found |= {c for (c,) in db.query(PayGroup.currency).filter(PayGroup.company_id == company_id,
                                                                    PayGroup.id.in_(eligibility["pay_group_ids"]))}
    if eligibility.get("legal_entity_ids"):
        found |= {c for (c,) in db.query(LegalEntity.base_currency).filter(
            LegalEntity.company_id == company_id, LegalEntity.id.in_(eligibility["legal_entity_ids"]))}
    if template.currency:
        return template.currency, sorted(found)
    if len(found) == 1:
        return next(iter(found)), sorted(found)
    return comp.company_currency(db, company_id), sorted(found)


def _issue(code: str, message: str, *, severity: str = "ERROR", component: str | None = None,
           **details: Any) -> dict[str, Any]:
    return {"code": code, "severity": severity, "message": message, "component": component, **details}


def validate_lines(db: Session, company_id: int, template: SalaryTemplate, version: SalaryTemplateVersion,
                   lines: Sequence[dict[str, Any]], *, on: dt.date, deep: bool = True) -> dict[str, Any]:
    """The publish checks of 3.3 §6.2 as a report: {ok, errors, warnings, minimum_pay_basis, vectors}."""
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    eligibility = version.eligibility or {}
    currency, eligible_currencies = structure_currency(db, company_id, template, eligibility)
    fixed_money = any(line["method"] == "FIXED" and line["value"] is not None for line in lines)
    if len(eligible_currencies) > 1 or (template.currency and eligible_currencies and
                                        eligible_currencies != [template.currency]):
        errors.append(_issue("MIXED_CURRENCIES", "All amounts of a structure share one currency: the eligible pay "
                             f"groups and entities use {', '.join(eligible_currencies)}" +
                             (f" but the structure is in {template.currency}." if template.currency else "."),
                             currencies=eligible_currencies, fixed_amounts=fixed_money))
    if not lines:
        errors.append(_issue("NO_LINES", "A structure needs at least one line."))
    entries = comp.catalogue(db, company_id, on)
    tables = comp.table_sigs(db, company_id, on)
    codes = [line["component"] for line in lines]
    deps: dict[str, set[str]] = {}
    stage_of = {}
    for line in lines:
        code = line["component"]
        entry = entries.get(code)
        if entry is None:
            errors.append(_issue("UNKNOWN_REFERENCE", f"{code} is not an active pay component.", component=code))
            continue
        stage_of[code] = entry.stage
        params = dict(line["params"] or {})
        if line["method"] == "PERCENT_OF":
            params["percent"] = line["value"]
        if line["method"] == "RATE_X_UNITS" and template.type in ("HOURLY", "DAILY"):
            params.setdefault("rate", "1")
            params.setdefault("units", "att.payable_hours" if template.type == "HOURLY" else "att.paid_days")
        if line["method"] == "RATE_X_UNITS" and line["value"] is not None:
            params["rate"] = decimal_str(Decimal(str(line["value"])))
        if line["method"] == "BALANCING" and not params.get("others"):
            params["others"] = [c for c in codes if c != code and c in entries and
                                "PART_OF_CTC" in (entries[c].version.tags or ()) and
                                next(ln for ln in lines if ln["component"] == c)["method"] not in
                                ("BALANCING", "SYSTEM", "INPUT")]
        try:
            text = comp.method_text(line["method"], params, line["formula"], own=code)
        except AppError as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"message": exc.detail}
            errors.append(_issue(exc.code or "INVALID_LINE", detail.get("message", ""), component=code,
                                 errors=detail.get("errors"), path=detail.get("path")))
            deps[code] = set()
            continue
        found: set[str] = set()
        for part in (text if isinstance(text, tuple) else (text,) if text else ()):
            env = comp.type_env(entries, currency, tables=tables, component=code, stage=entry.stage,
                                expect="ANY" if isinstance(text, tuple) else "AMOUNT", structure=codes)
            analysis = fm.diagnose(part, env)
            for issue in analysis.errors:
                errors.append(_issue(issue.code, f"{code}: {issue.message}", component=code, path=
                                     list(issue.path) if issue.path else None, position=issue.position))
            for issue in analysis.warnings:
                warnings.append(_issue(issue.code, f"{code}: {issue.message}", severity="WARNING", component=code))
            found |= set(analysis.dependencies)
        deps[code] = found - {code} if line["method"] != "BALANCING" else found - {code}
    if not errors:
        try:
            fm.dependency_order({c: {d for d in r if d in stage_of} for c, r in deps.items()}, stage_of)
        except fm.FormulaError as error:
            errors.append(_issue(error.code, error.message, component=(error.path or [None])[0], path=error.path))
    for item in pack_checks(db, company_id, template, version, lines, _countries(db, company_id, eligibility)):
        (errors if item.get("severity") == "ERROR" else warnings).append(item)

    report: dict[str, Any] = {"currency": currency, "minimum_pay_basis": None, "vectors": [], "balancing": None}
    test_range = version.test_range
    balancing = _balancing_code(lines)
    if deep and not errors:
        if balancing and test_range is None:
            errors.append(_issue("TEST_RANGE_REQUIRED", "Declare the pay basis range (minimum and maximum) so the "
                                 f"balancing component {balancing} can be checked (EC-CMP-04).", component=balancing))
        elif balancing:
            low, high = Decimal(test_range["min"]), Decimal(test_range["max"])
            try:
                minimum = minimum_pay_basis(db, company_id, structure_type=template.type, currency=currency,
                                            lines=lines, low=low, high=high, on=on)
            except AppError as exc:
                minimum = None
                errors.append(_issue(exc.code or "INVALID_STRUCTURE", str(exc.detail)))
            report["balancing"] = {"component": balancing, "range": test_range}
            if minimum is None:
                errors.append(_issue("NEGATIVE_BALANCING", f"The balancing component {balancing} is negative across "
                                     f"the whole range up to {amount_str(high, currency)} {currency}.",
                                     component=balancing))
            elif minimum > low:
                report["minimum_pay_basis"] = {"amount": amount_str(minimum, currency), "currency": currency}
                errors.append(_issue("NEGATIVE_BALANCING", f"The balancing component {balancing} is negative below "
                                     f"{amount_str(minimum, currency)} {currency}; raise the range minimum or change "
                                     "the other components (EC-CMP-04).", component=balancing,
                                     minimum_pay_basis=report["minimum_pay_basis"]))
        for vector in version.test_vectors or []:
            report["vectors"].append(run_vector(db, company_id, template, lines, vector, currency=currency, on=on))
        failed = [v["name"] for v in report["vectors"] if not v["passed"]]
        if failed:
            errors.append(_issue("TEST_VECTOR_FAILED", f"Test vectors fail: {', '.join(failed)}.", vectors=failed))
    report.update(ok=not errors, errors=errors, warnings=warnings)
    return report


def run_vector(db: Session, company_id: int, template: SalaryTemplate, lines: Sequence[dict[str, Any]],
               vector: Mapping[str, Any], *, currency: str, on: dt.date) -> dict[str, Any]:
    overrides = {code: {"amount": value} for code, value in (vector.get("overrides") or {}).items()}
    try:
        evaluation = evaluate(db, company_id, structure_type=template.type, currency=currency, lines=lines,
                              pay_basis_amount=Decimal(vector["pay_basis_amount"]), overrides=overrides, on=on)
    except AppError as exc:
        return {"name": vector["name"], "passed": False, "error": str(exc.detail), "mismatches": []}
    mismatches = []
    for code, expected in vector["expected"].items():
        actual = evaluation.rates.get(code)
        if actual is None or actual != Decimal(expected):
            mismatches.append({"component": code, "expected": expected,
                               "actual": None if actual is None else amount_str(actual, currency)})
    issues = [i.to_json() for i in evaluation.blocking]
    return {"name": vector["name"], "passed": not mismatches and not issues, "mismatches": mismatches,
            "issues": issues}


# --------------------------------------------------------------------------- serialization
def _money(amount: Decimal, currency: str) -> dict[str, str]:
    return {"amount": amount_str(quantize_amount(amount, currency), currency), "currency": currency}


def version_json(db: Session, version: SalaryTemplateVersion, *, with_lines: bool = True) -> dict[str, Any]:
    data = {
        "id": version.id, "version": version.version, "status": version.status, "eligibility": version.eligibility or {},
        "test_range": version.test_range, "test_vectors": version.test_vectors or [],
        "validation": version.validation, "effective_from": version.effective_from, "row_version": version.row_version,
        "created_by_user_id": version.created_by_user_id, "published_by_user_id": version.published_by_user_id,
        "published_at": version.published_at, "created_at": version.created_at,
    }
    if with_lines:
        data["lines"] = line_dicts(db, version)
    return data


def template_json(db: Session, template: SalaryTemplate, versions: Sequence[SalaryTemplateVersion], *,
                  detail: bool = False) -> dict[str, Any]:
    active = next((v for v in versions if v.status == "ACTIVE"), None)
    draft = next((v for v in versions if v.status == "DRAFT"), None)
    counts = dict(db.query(CompensationAssignment.template_version_id, func.count(CompensationAssignment.id))
                  .filter(CompensationAssignment.company_id == template.company_id,
                          CompensationAssignment.template_version_id.in_([v.id for v in versions] or [0]),
                          CompensationAssignment.status.in_(("ACTIVE", "SCHEDULED", "PENDING_APPROVAL")))
                  .group_by(CompensationAssignment.template_version_id).all())
    data = {
        "id": template.id, "code": template.code, "name": template.name, "description": template.description,
        "type": template.type, "currency": template.currency, "status": template.status,
        "active_version": active.version if active else None, "draft_version": draft.version if draft else None,
        "eligibility": (active or draft or (versions[-1] if versions else None)).eligibility if versions else {},
        "assignment_counts": {v.version: counts.get(v.id, 0) for v in versions},
    }
    if detail:
        data["versions"] = [version_json(db, v) for v in versions]
    return data


# --------------------------------------------------------------------------- queries
def list_structures(db: Session, principal: Principal, *, include_archived: bool = False) -> list[dict[str, Any]]:
    if not principal.has(READ):
        raise forbidden()
    query = db.query(SalaryTemplate).filter(SalaryTemplate.company_id == principal.company_id)
    if not include_archived:
        query = query.filter(SalaryTemplate.status == "ACTIVE")
    result = []
    for template in query.order_by(SalaryTemplate.name).all():
        versions = _versions(db, template)
        eligibility = versions[-1].eligibility if versions else {}
        if can_read(principal, eligibility):
            result.append(template_json(db, template, versions))
    return result


def get_structure(db: Session, principal: Principal, template_id: int) -> dict[str, Any]:
    template = _load_template(db, principal, template_id)
    return template_json(db, template, _versions(db, template), detail=True)


# --------------------------------------------------------------------------- changes
def _audit(db: Session, principal: Principal, action: str, description: str, template: SalaryTemplate,
           **details: Any) -> None:
    AuditLogService.log(db, principal, payroll_audit.COMPENSATION, action, description, "salary_structure",
                        template.id, details={"code": template.code, **details})


def _net_based_allowed(db: Session, company_id: int) -> bool:
    from app.payroll.services import settings as settings_engine

    try:
        resolved = settings_engine.resolve(db, company_id, "payroll.structures.net_based_enabled",
                                           on=comp.today(db, company_id))
    except Exception:
        return False
    return resolved.value is True


def create_structure(db: Session, principal: Principal, *, code: str, name: str, type_: str,
                     description: str | None = None, currency: str | None = None,
                     eligibility: Mapping[str, Any] | None = None, lines: Sequence[LineInput] = (),
                     test_range: Mapping[str, Any] | None = None,
                     test_vectors: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    cid = principal.company_id
    elig = _eligibility(db, cid, eligibility)
    _require_edit(principal, elig)
    code = (code or "").strip().upper()
    import re

    if not re.fullmatch(r"[A-Z][A-Z0-9_-]{0,39}", code):
        raise comp.invalid("A structure code is 1-40 upper-case letters, digits, '_' or '-'.", "INVALID_CODE", "code")
    name = (name or "").strip()
    if not 1 <= len(name) <= 150:
        raise comp.invalid("The structure name must be 1-150 characters.", "INVALID_NAME", "name")
    comp._choice(type_, STRUCTURE_TYPES, "type")
    if type_ == "NET_BASED" and not _net_based_allowed(db, cid):
        raise bad_request("Net-based structures need payroll.structures.net_based_enabled.", "NET_BASED_DISABLED")
    if currency is not None:
        try:
            currency = currencies.require_payroll_currency(currency).code
        except MoneyError as exc:
            raise comp.invalid(exc.message, exc.code, "currency")
    if db.query(SalaryTemplate.id).filter(SalaryTemplate.company_id == cid, SalaryTemplate.code == code).first():
        raise conflict(f"A structure with the code {code} exists.", "STRUCTURE_CODE_TAKEN")
    on = comp.today(db, cid)
    normalized = _normalize_lines(db, cid, lines, on, type_)
    template = SalaryTemplate(company_id=cid, code=code, name=name, description=description, type=type_,
                              currency=currency, status="ACTIVE", created_by_user_id=principal.user_id)
    db.add(template)
    db.flush()
    version = SalaryTemplateVersion(company_id=cid, template_id=template.id, version=1, status="DRAFT",
                                    eligibility=elig, test_range=_test_range(test_range),
                                    test_vectors=_vectors(test_vectors), row_version=1,
                                    created_by_user_id=principal.user_id)
    db.add(version)
    db.flush()
    _write_lines(db, version, normalized)
    _audit(db, principal, STRUCTURE_CREATED, f"Created salary structure {code} ({name})", template, type=type_,
           version=1, lines=[line["component"] for line in normalized])
    return get_structure(db, principal, template.id)


def create_version(db: Session, principal: Principal, template_id: int, *, from_version: int | None = None
                   ) -> dict[str, Any]:
    """A new DRAFT copied from `from_version` (default: the ACTIVE one, else the latest)."""
    template = _load_template(db, principal, template_id, lock=True)
    versions = _versions(db, template, lock=True)
    _require_edit(principal, versions[-1].eligibility or {})
    if any(v.status == "DRAFT" for v in versions):
        raise conflict("This structure already has a draft version; edit or publish it first.", "DRAFT_EXISTS")
    source = _version(db, template, from_version)
    lines = line_dicts(db, source)
    version = SalaryTemplateVersion(company_id=template.company_id, template_id=template.id,
                                    version=versions[-1].version + 1, status="DRAFT",
                                    eligibility=dict(source.eligibility or {}), test_range=source.test_range,
                                    test_vectors=list(source.test_vectors or []), row_version=1,
                                    created_by_user_id=principal.user_id)
    db.add(version)
    db.flush()
    _write_lines(db, version, [{**line, "value": None if line["value"] is None else Decimal(line["value"]),
                                "override_min": None if line["override_min"] is None else Decimal(line["override_min"]),
                                "override_max": None if line["override_max"] is None else Decimal(line["override_max"])}
                               for line in lines])
    _audit(db, principal, STRUCTURE_VERSION_CREATED,
           f"Created draft version {version.version} of salary structure {template.code}", template,
           version=version.version, copied_from=source.version)
    return get_structure(db, principal, template.id)


def update_version(db: Session, principal: Principal, template_id: int, number: int, *, row_version: int | None,
                   lines: Sequence[LineInput] | None = None, eligibility: Mapping[str, Any] | None = None,
                   test_range: Mapping[str, Any] | None = None, clear_test_range: bool = False,
                   test_vectors: Sequence[Mapping[str, Any]] | None = None, name: str | None = None,
                   description: str | None = None) -> dict[str, Any]:
    template = _load_template(db, principal, template_id, lock=True)
    version = _version(db, template, number, lock=True)
    _require_edit(principal, version.eligibility or {})
    if version.status != "DRAFT":
        raise conflict("Only a draft version can change; create a new version.", "VERSION_NOT_DRAFT")
    if row_version is not None and row_version != version.row_version:
        raise conflict("This draft changed since you loaded it; reload and try again.", "ROW_VERSION_CONFLICT")
    cid = principal.company_id
    changed = []
    if eligibility is not None:
        elig = _eligibility(db, cid, eligibility)
        _require_edit(principal, elig)
        version.eligibility = elig
        changed.append("eligibility")
    if lines is not None:
        _write_lines(db, version, _normalize_lines(db, cid, lines, comp.today(db, cid), template.type))
        changed.append("lines")
    if test_range is not None or clear_test_range:
        version.test_range = None if clear_test_range else _test_range(test_range)
        changed.append("test_range")
    if test_vectors is not None:
        version.test_vectors = _vectors(test_vectors)
        changed.append("test_vectors")
    if name is not None:
        if not 1 <= len(name.strip()) <= 150:
            raise comp.invalid("The structure name must be 1-150 characters.", "INVALID_NAME", "name")
        template.name = name.strip()
        changed.append("name")
    if description is not None:
        template.description = description or None
        changed.append("description")
    if changed:
        version.row_version += 1
        version.validation = None
        db.flush()
        _audit(db, principal, STRUCTURE_VERSION_UPDATED,
               f"Updated draft version {version.version} of salary structure {template.code}", template,
               version=version.version, changed=changed)
    return get_structure(db, principal, template.id)


def _resolved_lines(db: Session, version: SalaryTemplateVersion) -> list[dict[str, Any]]:
    return [{**line, "value": None if line["value"] is None else Decimal(line["value"])}
            for line in line_dicts(db, version)]


def validate_version(db: Session, principal: Principal, template_id: int, number: int | None) -> dict[str, Any]:
    template = _load_template(db, principal, template_id)
    version = _version(db, template, number)
    report = validate_lines(db, principal.company_id, template, version, _resolved_lines(db, version),
                            on=comp.today(db, principal.company_id))
    return {"structure_id": template.id, "version": version.version, **report}


def publish(db: Session, principal: Principal, template_id: int, number: int, *,
            effective_from: dt.date | None = None) -> dict[str, Any]:
    """Validate and publish a draft; the previous ACTIVE version is ARCHIVED (its assignments keep it)."""
    template = _load_template(db, principal, template_id, lock=True)
    versions = _versions(db, template, lock=True)
    version = next((v for v in versions if v.version == number), None)
    if version is None:
        raise not_found("Salary structure version")
    _require_edit(principal, version.eligibility or {}, publish=True)
    if version.status != "DRAFT":
        raise conflict("This version is already published.", "VERSION_NOT_DRAFT")
    cid = principal.company_id
    on = comp.today(db, cid)
    report = validate_lines(db, cid, template, version, _resolved_lines(db, version), on=on)
    stored = {k: report[k] for k in ("ok", "errors", "warnings", "minimum_pay_basis", "vectors", "currency")}
    if not report["ok"]:
        raise AppError(400, {"message": "The structure cannot be published until these problems are fixed.",
                             **stored}, report["errors"][0]["code"])
    previous = next((v for v in versions if v.status == "ACTIVE"), None)
    if previous is not None:
        previous.status = "ARCHIVED"
        db.flush()
    version.status = "ACTIVE"
    version.effective_from = effective_from or on
    version.published_by_user_id = principal.user_id
    version.published_at = utcnow()
    version.validation = {**stored, "validated_at": utcnow().isoformat()}
    db.flush()
    _audit(db, principal, STRUCTURE_PUBLISHED, f"Published version {version.version} of salary structure "
           f"{template.code}", template, version=version.version, archived_version=previous.version if previous else None,
           effective_from=version.effective_from.isoformat(), warnings=len(report["warnings"]),
           vectors=len(report["vectors"]))
    return get_structure(db, principal, template.id)


def archive_structure(db: Session, principal: Principal, template_id: int) -> dict[str, Any]:
    template = _load_template(db, principal, template_id, lock=True)
    versions = _versions(db, template)
    _require_edit(principal, versions[-1].eligibility or {} if versions else {})
    if template.status != "ARCHIVED":
        template.status = "ARCHIVED"
        db.flush()
        _audit(db, principal, STRUCTURE_ARCHIVED, f"Archived salary structure {template.code}", template)
    return get_structure(db, principal, template.id)


# --------------------------------------------------------------------------- preview
def breakdown(db: Session, evaluation: Evaluation, lines: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Monthly and annual amounts per line and the totals (3.3 §6.4)."""
    currency = evaluation.currency
    per_year = evaluation.periods_per_year
    rows = []
    totals = {"gross": Decimal(0), "deductions": Decimal(0), "employer_contributions": Decimal(0),
              "reimbursements": Decimal(0), "ctc": Decimal(0)}
    for line in lines:
        code = line["component"]
        monthly = evaluation.rates.get(code)
        engine_line = next((ln for ln in evaluation.lines if ln.component == code), None)
        kind = line.get("kind") or (engine_line.kind if engine_line else "EARNING")
        row = {"component": code, "kind": kind, "method": line["method"],
               "monthly": None if monthly is None else _money(monthly, currency),
               "annual": None if monthly is None else _money(monthly * per_year, currency),
               "explanation": engine_line.explanation if engine_line else None}
        rows.append(row)
        if monthly is None:
            continue
        if kind == "EARNING":
            totals["gross"] += monthly
            totals["ctc"] += monthly
        elif kind == "DEDUCTION":
            totals["deductions"] += monthly
        elif kind == "EMPLOYER_CONTRIBUTION":
            totals["employer_contributions"] += monthly
            totals["ctc"] += monthly
        elif kind == "REIMBURSEMENT":
            totals["reimbursements"] += monthly
    statutory_rows = []
    for line in evaluation.statutory:
        statutory_rows.append({"component": line.component, "kind": line.kind, "monthly": _money(line.amount, currency),
                               "annual": _money(line.amount * per_year, currency),
                               "verified": bool((line.trace or {}).get("verified")), "fixture": line.fixture,
                               "explanation": line.explanation})
        if line.kind == "DEDUCTION":
            totals["deductions"] += line.amount
        elif line.kind == "EMPLOYER_CONTRIBUTION":
            totals["employer_contributions"] += line.amount
    net = totals["gross"] + totals["reimbursements"] - totals["deductions"]
    return {
        "currency": currency, "periods_per_year": per_year, "lines": rows,
        "totals": {key: {"monthly": _money(value, currency), "annual": _money(value * per_year, currency)}
                   for key, value in {**totals, "net_estimate": net}.items()},
        "statutory": {"lines": statutory_rows, "verified": bool(statutory_rows) and all(r["verified"] for r in
                                                                                    statutory_rows),
                      "label": "Compliance not verified", "note": STATUTORY_NOTE},
        "issues": [i.to_json() for i in evaluation.issues if i.code in STRUCTURE_ISSUES or i.component],
        "ok": evaluation.ok,
    }


def preview(db: Session, principal: Principal, template_id: int, *, pay_basis_amount: Decimal,
            number: int | None = None, overrides: Mapping[str, Mapping[str, Any]] | None = None,
            currency: str | None = None) -> dict[str, Any]:
    template = _load_template(db, principal, template_id)
    version = _version(db, template, number)
    cid = principal.company_id
    lines = _resolved_lines(db, version)
    structure_cur, _ = structure_currency(db, cid, template, version.eligibility or {})
    if currency is not None and currency != structure_cur:
        raise bad_request(f"This structure pays in {structure_cur}; there is no implicit FX (EC-CMP-07).",
                          "CURRENCY_MISMATCH")
    countries = _countries(db, cid, version.eligibility or {})
    evaluation = evaluate(db, cid, structure_type=template.type, currency=structure_cur, lines=lines,
                          pay_basis_amount=pay_basis_amount, overrides=overrides, country=countries[0] if
                          len(countries) == 1 else None, statutory=len(countries) == 1)
    result = breakdown(db, evaluation, lines)
    result.update(structure={"id": template.id, "code": template.code, "name": template.name, "type": template.type},
                  version=version.version, pay_basis_amount=_money(pay_basis_amount, structure_cur))
    return result


# --------------------------------------------------------------------------- formula tester (real employee)
def employee_sample(db: Session, principal: Principal, employee_id: int, *, period_start: dt.date | None,
                    period_end: dt.date | None) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Sample values for the formula tester from a real employee in the caller's compensation scope: the
    employee facts (`emp.*`), `ctc.*` and the full-period values of their structure in the period; time
    variables describe a full period without loss of pay (the run supplies the real day ledger)."""
    from app.payroll.services import compensation as comp_service

    cid = principal.company_id
    start, end = (period_start, period_end) if period_start and period_end else month_of(comp.today(db, cid))
    if end < start:
        raise bad_request("The period ends before it starts.", "INVALID_PERIOD")
    assignment = comp_service.in_force(db, principal, employee_id, end)
    facts = comp_service.employee_facts(db, cid, employee_id, end)
    variables: dict[str, Any] = {
        "period.start": start.isoformat(), "period.end": end.isoformat(), "period.days": str((end - start).days + 1),
        "period.pay_date": end.isoformat(), "att.paid_days": str((end - start).days + 1), "att.lop_days": "0",
        **{k: v for k, v in facts.items() if v is not None},
    }
    sample: dict[str, Any] = {"variables": variables, "components": {}, "rates": {}}
    source = {"type": "EMPLOYEE", "employee_id": employee_id, "period": {"start": start, "end": end},
              "compensation_id": None, "assumptions": ["Full period paid; no loss of pay or time inputs."]}
    if assignment is None:
        return sample, comp.company_currency(db, cid), source
    evaluation = comp_service.evaluate_assignment(db, assignment, on=end)
    for code, amount in evaluation.rates.items():
        sample["components"][code] = {"amount": decimal_str(amount), "currency": evaluation.currency}
        sample["rates"][code] = {"amount": decimal_str(amount), "currency": evaluation.currency}
    template = db.get(SalaryTemplate, db.get(SalaryTemplateVersion, assignment.template_version_id).template_id)
    if template.type == "CTC_BASED":
        variables["ctc.annual"] = decimal_str(assignment.pay_basis_amount)
        variables["ctc.monthly"] = decimal_str(assignment.pay_basis_amount / 12)
    elif template.type == "GROSS_BASED":
        variables["ctc.annual"] = decimal_str(assignment.pay_basis_amount * 12)
        variables["ctc.monthly"] = decimal_str(assignment.pay_basis_amount)
    source["compensation_id"] = assignment.id
    return sample, evaluation.currency, source


# --------------------------------------------------------------------------- move assignments
def move_assignments(db: Session, principal: Principal, template_id: int, *, to_version: int | None,
                     effective_from: dt.date, assignment_ids: Sequence[int] | None = None, dry_run: bool = True,
                     note: str | None = None) -> dict[str, Any]:
    """Move assignments on older versions of the structure to a published version, with a before/after
    preview per assignment (EC-CMP-17). Committing proposes a new assignment per employee for approval."""
    from app.payroll.services import compensation as comp_service

    template = _load_template(db, principal, template_id)
    target = _version(db, template, to_version)
    if target.status != "ACTIVE":
        raise conflict("Assignments can only move to the published (active) version.", "VERSION_NOT_ACTIVE")
    older = [v.id for v in _versions(db, template) if v.id != target.id]
    query = db.query(CompensationAssignment).filter(
        CompensationAssignment.company_id == principal.company_id,
        CompensationAssignment.template_version_id.in_(older or [0]),
        CompensationAssignment.status.in_(("ACTIVE", "SCHEDULED")),
    )
    if assignment_ids:
        query = query.filter(CompensationAssignment.id.in_(sorted(set(assignment_ids))))
    rows = [row for row in query.order_by(CompensationAssignment.employee_id).all()
            if row.effective_to is None or row.effective_to >= effective_from]
    items = []
    for row in rows:
        if not comp_service.can_edit_employee(db, principal, row.employee_id, effective_from):
            continue
        items.append(comp_service.compare(db, principal, row, template_version_id=target.id,
                                          pay_basis_amount=row.pay_basis_amount, on=effective_from))
    if assignment_ids and len(items) != len(set(assignment_ids)):
        raise not_found("One or more compensation assignments")
    if dry_run:
        return {"dry_run": True, "to_version": target.version, "effective_from": effective_from, "items": items,
                "count": len(items)}
    created = []
    for row in rows:
        if not any(item["assignment_id"] == row.id for item in items):
            continue
        result = comp_service.propose(
            db, principal, row.employee_id, template_version_id=target.id, pay_basis_amount=row.pay_basis_amount,
            currency=row.currency, effective_from=effective_from, reason="REVISION",
            overrides=comp_service.overrides_of(db, row), note=note or f"Moved to version {target.version} of "
            f"{template.code}", submit=True)
        created.append(result["id"])
    return {"dry_run": False, "to_version": target.version, "effective_from": effective_from, "items": items,
            "created": created, "count": len(created)}

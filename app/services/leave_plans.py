"""Leave plans (``LEAVE_PLAN``, LH 03-leave/01 §5-§10): templates, the LEGACY_V1 Default plan, resolution, eligibility,
request rules, explain and simulation.

Plans live in the shared policy framework (``app.payroll.services.policies``): DRAFT versions are edited in place,
ACTIVE ones are immutable, assignment rules match employees by criteria and priority. The builder is GLOBAL
(13 §2.3): HR builds, simulates and activates plans while the tenant is LEGACY_V1, but until the cut-over every reader
uses the legacy rules of the leave types (``legacy_rule``), which reproduce today's behaviour exactly. From the
cut-over (``leave.v2.enabled``) the highest-priority matching plan applies.

Eligibility (§7) runs the checks in order and returns the most restrictive status; structural failures are never
explained to the employee (protected attributes such as gender are never echoed).
"""

import copy
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, bad_request, forbidden, not_found
from app.core.principal import Principal
from app.core.security import utcnow
from app.models.employee import Employee
from app.models.leave import CATEGORY_DEFAULTS, LeaveType
from app.models.org import Branch, EmployeeAssignment, LegalEntity
from app.models.pay_org import EmploymentType
from app.models.payroll_config import Policy, PolicyAssignment, PolicyVersion
from app.payroll import policy_types as pt
from app.services import leave_access
from app.services.audit_log_service import AuditLogService
from app.services.leave_cycles import Cycle, cycle_for, earned_to_date

PLAN = "LEAVE_PLAN"
DEFAULT_PLAN_NAME = "Default plan"
MANAGE = "leave:manage_policy"
STATUS_ORDER = ("ELIGIBLE", "ELIGIBLE_VERIFY", "ACCRUE_ONLY", "NOT_ELIGIBLE")
STRUCTURAL_CHECKS = ("IN_PLAN", "WORKER_CLASS", "EMPLOYMENT_TYPE", "LOCATION", "FTE", "GENDER")
_MEMO = "leave_plan_memo"


# --------------------------------------------------------------------------- templates (03-leave/01 §4)

def _rule(code: str, **overrides: Any) -> Dict[str, Any]:
    rule: Dict[str, Any] = {"leave_type_code": code}
    rule.update(overrides)
    return rule


def _front(amount: str) -> dict:
    return {"method": "FRONT_LOADED", "amount": amount}


def _monthly(amount: str, post_at: str = "END") -> dict:
    return {"method": "PERIODIC", "amount": amount, "frequency": "MONTHLY", "post_at": post_at}


_SICK_DOCS = {"required": [{"when": "MIN_UNITS", "min_units": "3", "category": "MEDICAL_CERTIFICATE"}],
              "due": "WITHIN_DAYS_AFTER_RETURN", "due_days": 3}
_EXEMPT = {"blackouts": "EXEMPT", "coverage": "EXEMPT"}


@dataclass(frozen=True)
class TypeSpec:
    """A leave type a template needs; created when the tenant has no type with this code."""

    code: str
    name: str
    category: str
    is_paid: bool = True
    is_unlimited: bool = False
    kind: Optional[str] = None
    allow_half_day: bool = True
    counts_non_working_days: bool = False


@dataclass(frozen=True)
class Template:
    key: str
    name: str
    country: Optional[str]
    description: str
    types: tuple
    defaults: Mapping[str, Any] = field(default_factory=dict)
    comp_off: Optional[Mapping[str, Any]] = None


TEMPLATES: Dict[str, Template] = {t.key: t for t in (
    Template(
        "IN.OFFICE.V1", "India office", "IN",
        "Earned leave accrued monthly with carry-forward and encashment, casual and sick leave, maternity, paternity, "
        "unpaid leave and comp-off for an Indian office.",
        (
            (TypeSpec("earned", "Earned Leave", "ANNUAL"), _rule(
                "earned", entitlement=_monthly("18"), carry_forward={"cap": "30"},
                encashment={"eligible": True, "triggers": ["EXIT", "YEAR_END"], "max_units_per_cycle": "15"},
                request={"min_notice": {"value": 7, "unit": "CALENDAR_DAYS",
                                        "scale": [{"min_units": "5", "value": 14}]},
                         "emergency_exception": {"enabled": True, "max_units": "1", "reason_required": True}})),
            (TypeSpec("casual", "Casual Leave", "CASUAL"), _rule(
                "casual", entitlement=_front("12"), request={"max_units": "3",
                                                             "caps": [{"period": "MONTH", "max_units": "3"}]})),
            (TypeSpec("sick", "Sick Leave", "SICK"), _rule(
                "sick", entitlement=_front("12"), documents=_SICK_DOCS,
                request={"backdate_limit_days": 7, **_EXEMPT})),
            (TypeSpec("maternity", "Maternity Leave", "PARENTAL", kind="EVENT", counts_non_working_days=True), _rule(
                "maternity", balance_scope={"kind": "EVENT"}, counting_basis={"basis": "CALENDAR_DAYS"},
                entitlement={"method": "EVENT_GRANT"}, carry_forward={"cap": "0"},
                eligibility={"genders": ["FEMALE"]},
                event={"event_types": ["EXPECTED_DELIVERY", "BIRTH", "ADOPTION_HANDOVER"],
                       "entitlement_units": "182", "duration_unit": "CALENDAR_DAYS",
                       "earliest_start_offset_days": -56, "max_occurrences": 3},
                request=_EXEMPT, documents={"required": [{"when": "ALWAYS", "category": "PARENTAL_EVIDENCE"}],
                                            "due": "WITHIN_DAYS_AFTER_SUBMIT", "due_days": 30})),
            (TypeSpec("paternity", "Paternity Leave", "PARENTAL", kind="EVENT"), _rule(
                "paternity", balance_scope={"kind": "EVENT"}, entitlement={"method": "EVENT_GRANT"},
                carry_forward={"cap": "0"},
                event={"event_types": ["BIRTH", "ADOPTION_HANDOVER"], "entitlement_units": "5",
                       "duration_unit": "WORKING_DAYS", "take_within_days": 90}, request=_EXEMPT)),
            (TypeSpec("unpaid", "Unpaid Leave", "UNPAID", is_paid=False, is_unlimited=True), _rule(
                "unpaid", entitlement={"method": "NONE"}, request={"reason": "REQUIRED"})),
            (TypeSpec("comp_off", "Compensatory Off", "COMP_OFF", kind="COMP_OFF"), _rule(
                "comp_off", balance_scope={"kind": "LOTS"}, entitlement={"method": "NONE"},
                carry_forward={"cap": None})),
        ),
        comp_off={"validity_days": 60},
    ),
    Template(
        "IN.FACTORY.V1", "India factory / plant", "IN",
        "Plant workers: earned leave accrued monthly (OSH-style), casual and sick leave with probation rules, sandwich "
        "of weekly offs for casual leave, unpaid leave and comp-off for weekly-off and holiday work.",
        (
            (TypeSpec("earned", "Earned Leave", "ANNUAL"), _rule(
                "earned", entitlement=_monthly("15"), carry_forward={"cap": "30"}, during_probation="ACCRUE_ONLY",
                encashment={"eligible": True, "triggers": ["EXIT"]},
                request={"min_notice": {"value": 3, "unit": "WORKING_DAYS", "scale": []}})),
            (TypeSpec("casual", "Casual Leave", "CASUAL"), _rule(
                "casual", entitlement=_front("7"), sandwich={"mode": "WEEKLY_OFFS"},
                request={"max_units": "3", "min_notice": {"value": 1, "unit": "WORKING_DAYS", "scale": []},
                         "emergency_exception": {"enabled": True, "max_units": "1", "reason_required": True}})),
            (TypeSpec("sick", "Sick Leave", "SICK"), _rule(
                "sick", entitlement=_front("7"), documents=_SICK_DOCS, request={"backdate_limit_days": 7, **_EXEMPT})),
            (TypeSpec("unpaid", "Unpaid Leave", "UNPAID", is_paid=False, is_unlimited=True), _rule(
                "unpaid", entitlement={"method": "NONE"}, request={"reason": "REQUIRED"})),
            (TypeSpec("comp_off", "Compensatory Off", "COMP_OFF", kind="COMP_OFF"), _rule(
                "comp_off", balance_scope={"kind": "LOTS"}, entitlement={"method": "NONE"},
                carry_forward={"cap": None})),
        ),
        comp_off={"validity_days": 30, "claim_window_days": 15},
    ),
    Template(
        "AE.PRIVATE.V1", "UAE private sector", "AE",
        "Annual leave counted in calendar days after six months of service, sick leave not available in probation, "
        "parental, maternity and bereavement leave (verify the amounts against the current UAE labour law).",
        (
            (TypeSpec("annual", "Annual Leave", "ANNUAL", counts_non_working_days=True), _rule(
                "annual", entitlement=_monthly("30"), counting_basis={"basis": "CALENDAR_DAYS"},
                carry_forward={"cap": "15"}, eligibility={"min_tenure": {"value": 6, "unit": "MONTHS",
                                                                          "basis": "SERVICE_START",
                                                                          "mode": "BLOCK_USE"}},
                encashment={"eligible": True, "triggers": ["EXIT"]})),
            (TypeSpec("sick", "Sick Leave", "SICK"), _rule(
                "sick", entitlement=_front("90"), during_probation="BLOCK", documents=_SICK_DOCS,
                request={"backdate_limit_days": 7, **_EXEMPT})),
            (TypeSpec("maternity", "Maternity Leave", "PARENTAL", kind="EVENT", counts_non_working_days=True), _rule(
                "maternity", balance_scope={"kind": "EVENT"}, counting_basis={"basis": "CALENDAR_DAYS"},
                entitlement={"method": "EVENT_GRANT"}, carry_forward={"cap": "0"},
                eligibility={"genders": ["FEMALE"]},
                event={"event_types": ["EXPECTED_DELIVERY", "BIRTH"], "entitlement_units": "60",
                       "duration_unit": "CALENDAR_DAYS", "earliest_start_offset_days": -30}, request=_EXEMPT)),
            (TypeSpec("parental", "Parental Leave", "PARENTAL", kind="EVENT"), _rule(
                "parental", balance_scope={"kind": "EVENT"}, entitlement={"method": "EVENT_GRANT"},
                carry_forward={"cap": "0"},
                event={"event_types": ["BIRTH", "ADOPTION_HANDOVER"], "entitlement_units": "5",
                       "duration_unit": "WORKING_DAYS", "take_within_days": 180}, request=_EXEMPT)),
            (TypeSpec("bereavement", "Bereavement Leave", "BEREAVEMENT", kind="EVENT"), _rule(
                "bereavement", balance_scope={"kind": "EVENT"}, entitlement={"method": "EVENT_GRANT"},
                carry_forward={"cap": "0"},
                event={"event_types": ["DEATH"], "entitlement_units": "5", "duration_unit": "WORKING_DAYS",
                       "take_within_days": 30}, request=_EXEMPT)),
            (TypeSpec("unpaid", "Unpaid Leave", "UNPAID", is_paid=False, is_unlimited=True), _rule(
                "unpaid", entitlement={"method": "NONE"}, request={"reason": "REQUIRED"})),
        ),
    ),
    Template(
        "US.PTO.V1", "United States PTO", "US",
        "Paid time off accrued monthly with a carry-over cap, front-loaded paid sick leave and unpaid leave.",
        (
            (TypeSpec("pto", "Paid Time Off", "ANNUAL"), _rule(
                "pto", entitlement=_monthly("15"), carry_forward={"cap": "5"}, max_balance="30",
                request={"min_notice": {"value": 14, "unit": "CALENDAR_DAYS", "scale": []},
                         "emergency_exception": {"enabled": True, "max_units": "2", "reason_required": False}})),
            (TypeSpec("sick", "Sick Leave", "SICK"), _rule(
                "sick", entitlement=_front("5"), carry_forward={"cap": "5"}, request=_EXEMPT)),
            (TypeSpec("unpaid", "Unpaid Leave", "UNPAID", is_paid=False, is_unlimited=True), _rule(
                "unpaid", entitlement={"method": "NONE"}, request={"reason": "REQUIRED"})),
        ),
    ),
    Template(
        "GLOBAL.BASIC.V1", "Basic plan", None,
        "Annual leave, sick leave and unpaid leave with common defaults; a starting point for any country.",
        (
            (TypeSpec("annual", "Annual Leave", "ANNUAL"), _rule(
                "annual", entitlement=_front("20"), carry_forward={"cap": "5"})),
            (TypeSpec("sick", "Sick Leave", "SICK"), _rule("sick", entitlement=_front("10"), request=_EXEMPT)),
            (TypeSpec("unpaid", "Unpaid Leave", "UNPAID", is_paid=False, is_unlimited=True), _rule(
                "unpaid", entitlement={"method": "NONE"}, request={"reason": "REQUIRED"})),
        ),
    ),
)}


def template_list() -> List[dict]:
    return [{"key": t.key, "name": t.name, "country": t.country, "description": t.description,
             "types": [spec.code for spec, _ in t.types]} for t in TEMPLATES.values()]


def _ensure_type(db: Session, company_id: int, spec: TypeSpec) -> LeaveType:
    found = db.query(LeaveType).filter(LeaveType.company_id == company_id, LeaveType.code == spec.code).first()
    if found is not None:
        return found
    sensitivity, absence_class, icon = CATEGORY_DEFAULTS[spec.category]
    taken = db.query(LeaveType.id).filter(LeaveType.company_id == company_id).count()
    row = LeaveType(company_id=company_id, code=spec.code, name=spec.name, annual_quota=0, is_paid=spec.is_paid,
                    is_unlimited=spec.is_unlimited, is_active=True, kind=spec.kind, category=spec.category,
                    sensitivity=sensitivity, absence_class=absence_class, icon=icon,
                    counts_toward_absence_score=absence_class == "UNPLANNED",
                    allow_half_day=spec.allow_half_day, counts_non_working_days=spec.counts_non_working_days,
                    color_token=f"leave-{taken % 12 + 1}", display_order=100 + 10 * taken)
    db.add(row)
    db.flush()
    return row


def template_fields(db: Session, company_id: int, key: str, *, create_types: bool = True) -> Dict[str, Any]:
    template = TEMPLATES.get(key)
    if template is None:
        raise bad_request(f"Unknown plan template {key!r}.", "UNKNOWN_REFERENCE")
    types = []
    for spec, rule in template.types:
        lt = _ensure_type(db, company_id, spec) if create_types else None
        rule = copy.deepcopy(rule)
        if lt is not None and rule.get("event") and lt.kind != "EVENT":
            # The tenant keeps this code as an ordinary yearly type: grant the event's units once a year instead.
            event = rule.pop("event")
            rule["balance_scope"] = {"kind": "CYCLE"}
            rule["entitlement"] = {"method": "FRONT_LOADED", "amount": event["entitlement_units"]}
        if lt is not None and lt.kind == "EVENT" and not rule.get("event"):
            continue
        types.append(rule)
    fields: Dict[str, Any] = {"types": types}
    if template.comp_off is not None:
        fields["comp_off"] = dict(template.comp_off)
    return pt.validate_fields(PLAN, fields)


# --------------------------------------------------------------------------- LEGACY_V1 rules (§5.9)

def legacy_rule(leave_type: LeaveType) -> Dict[str, Any]:
    """The type rule that reproduces today's behaviour for one type (the Default plan's rule)."""
    rule: Dict[str, Any] = {
        "leave_type_code": leave_type.code,
        "counting_basis": {"basis": "CALENDAR_DAYS" if leave_type.counts_non_working_days else "WORKING_DAYS"},
        "proration": {"joiner": "FULL", "leaver": "FULL"},
        "carry_forward": {"cap": "0"},
        "request": {"backdate_limit_days": 0, "future_limit_days": 365},
    }
    if leave_type.kind == "COMP_OFF":
        rule.update(balance_scope={"kind": "LOTS"}, entitlement={"method": "NONE"}, carry_forward={"cap": None})
    elif leave_type.is_unlimited:
        rule["entitlement"] = {"method": "NONE"}
    else:
        rule["entitlement"] = {"method": "FRONT_LOADED", "amount": str(leave_type.annual_quota or 0)}
    if not leave_type.is_active:
        rule["frozen"] = True
    return rule


def legacy_fields(db: Session, company_id: int) -> Dict[str, Any]:
    types = db.query(LeaveType).filter(LeaveType.company_id == company_id).order_by(LeaveType.id).all()
    fields: Dict[str, Any] = {"types": [legacy_rule(t) for t in types if t.kind not in ("EVENT", "FLOATING_HOLIDAY")]}
    if any(t.kind == "COMP_OFF" for t in types):
        fields["comp_off"] = {}
    return pt.validate_fields(PLAN, fields)


def ensure_default_plan(db: Session, company_id: int, effective_from: date) -> Policy:
    """The LEGACY_V1 "Default plan" (job ``leave.v2_legacy_plan``; cut-over preparation): one catch-all policy with
    version 1 ACTIVE from ``effective_from``. Idempotent; written as SYSTEM (no principal)."""
    policy = (db.query(Policy).filter(Policy.company_id == company_id, Policy.type == PLAN,
                                      Policy.name == DEFAULT_PLAN_NAME).first())
    if policy is not None:
        return policy
    fields = legacy_fields(db, company_id)
    policy = Policy(company_id=company_id, type=PLAN, name=DEFAULT_PLAN_NAME, status="ACTIVE",
                    description="Migrated from leave types: each limited type grants its yearly quota at the start of "
                                "the calendar year and unused leave lapses, as before.")
    db.add(policy)
    db.flush()
    db.add(PolicyVersion(company_id=company_id, policy_id=policy.id, version=1, fields=fields, status="ACTIVE",
                         effective_from=effective_from, policy_hash=pt.policy_hash(fields), activated_at=utcnow()))
    db.add(PolicyAssignment(company_id=company_id, policy_id=policy.id, criteria={"everyone": True}, priority=0,
                            effective_from=effective_from))
    db.flush()
    return policy


# --------------------------------------------------------------------------- resolution

@dataclass
class Resolution:
    status: str  # RESOLVED | LEGACY | NO_POLICY | NOT_PLACED | AMBIGUOUS | NO_ACTIVE_VERSION
    fields: Dict[str, Any]
    policy_id: Optional[int] = None
    policy_name: Optional[str] = None
    version_id: Optional[int] = None
    version: Optional[int] = None
    reason: Optional[str] = None

    def rule(self, code: str) -> Optional[Dict[str, Any]]:
        return next((t for t in self.fields.get("types", []) if t.get("leave_type_code") == code), None)

    def json(self) -> dict:
        return {"status": self.status, "policy": None if self.policy_id is None else
                {"id": self.policy_id, "name": self.policy_name},
                "version": self.version, "version_id": self.version_id, "reason": self.reason}


def _memo(db: Session) -> dict:
    return db.info.setdefault(_MEMO, {})


def resolve(db: Session, company_id: int, employee_id: int, on: date) -> Resolution:
    """The plan that applies to the employee on ``on``: LEGACY rules while the tenant is LEGACY_V1, else the
    highest-priority matching plan's version active on that date."""
    memo = _memo(db)
    key = ("resolve", company_id, employee_id, on)
    if key in memo:
        return memo[key]
    if not leave_access.v2_enabled(db, company_id):
        legacy = memo.get(("legacy", company_id))
        if legacy is None:
            legacy = memo[("legacy", company_id)] = legacy_fields(db, company_id)
        result = Resolution("LEGACY", legacy, policy_name=DEFAULT_PLAN_NAME,
                            reason="Legacy leave: the leave types' yearly quotas apply until the leave cut-over.")
    else:
        from app.payroll.services import policies

        found = policies.resolve_for_employee(db, company_id, PLAN, employee_id, on)
        if found["status"] == "RESOLVED":
            version = found["version"]
            result = Resolution("RESOLVED", version["fields"], policy_id=found["policy"]["id"],
                                policy_name=found["policy"]["name"], version_id=version["id"],
                                version=version["version"], reason=found.get("reason"))
        else:
            result = Resolution(found["status"], {"types": []}, policy_id=(found.get("policy") or {}).get("id"),
                                policy_name=(found.get("policy") or {}).get("name"), reason=found.get("reason"))
    memo[key] = result
    return result


def rules_for(db: Session, company_id: int, employee_id: int, type_code: str, on: date) -> Optional[Dict[str, Any]]:
    """The type rule for (employee, type) on a date (None = the type is not in the employee's plan)."""
    return resolve(db, company_id, employee_id, on).rule(type_code)


def plan_defaults(resolution: Resolution) -> Dict[str, Any]:
    return (resolution.fields.get("defaults") or {}).get("cycle") or {"kind": "CALENDAR_YEAR"}


# --------------------------------------------------------------------------- placement facts

@dataclass(frozen=True)
class Placement:
    employee_id: int
    legal_entity_id: Optional[int]
    branch_id: Optional[int]
    department_id: Optional[int]
    employment_type: Optional[str]
    worker_class: Optional[str]
    country: Optional[str]
    fte: Decimal
    fiscal_start: str


def placement(db: Session, company_id: int, employee_id: int, on: date) -> Optional[Placement]:
    memo = _memo(db)
    key = ("placement", company_id, employee_id, on)
    if key in memo:
        return memo[key]
    row = (db.query(EmployeeAssignment, EmploymentType.code, EmploymentType.worker_class, Branch.country_code,
                    LegalEntity.fiscal_year_start)
           .outerjoin(EmploymentType, and_(EmploymentType.company_id == EmployeeAssignment.company_id,
                                           EmploymentType.id == EmployeeAssignment.employment_type_id))
           .outerjoin(Branch, and_(Branch.company_id == EmployeeAssignment.company_id,
                                   Branch.id == EmployeeAssignment.branch_id))
           .outerjoin(LegalEntity, and_(LegalEntity.company_id == EmployeeAssignment.company_id,
                                        LegalEntity.id == EmployeeAssignment.legal_entity_id))
           .filter(EmployeeAssignment.company_id == company_id, EmployeeAssignment.employee_id == employee_id,
                   EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.effective_from <= on,
                   or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= on))
           .order_by(EmployeeAssignment.effective_from.desc()).first())
    result = None
    if row is not None:
        a, etype, wclass, country, fiscal = row
        result = Placement(employee_id, a.legal_entity_id, a.branch_id, a.department_id, etype, wclass, country,
                           Decimal(a.fte or 1), fiscal or "01-01")
    memo[key] = result
    return result


def cycle_of(db: Session, company_id: int, employee: Employee, rule: Optional[Mapping[str, Any]], on: date,
             resolution: Optional[Resolution] = None) -> Cycle:
    """The leave year of a type for the employee on ``on`` (rule cycle, else the plan default)."""
    resolution = resolution or resolve(db, company_id, employee.id, on)
    spec = (rule or {}).get("cycle") or plan_defaults(resolution)
    kind = spec.get("kind") or "CALENDAR_YEAR"
    fiscal = "01-01"
    if kind == "FISCAL_YEAR":
        where = placement(db, company_id, employee.id, on)
        fiscal = where.fiscal_start if where else "01-01"
    return cycle_for(kind, on, custom_start=spec.get("custom_start"), fiscal_start=fiscal,
                     anniversary=employee.service_start_date or employee.hire_date)


# --------------------------------------------------------------------------- eligibility (§7)

@dataclass
class Eligibility:
    status: str
    checks: List[dict]
    available_from: Optional[date] = None
    rule: Optional[Dict[str, Any]] = None

    @property
    def bookable(self) -> bool:
        return self.status in ("ELIGIBLE", "ELIGIBLE_VERIFY")

    @property
    def structural(self) -> bool:
        return any(not c["passed"] and c["code"] in STRUCTURAL_CHECKS for c in self.checks)

    def first_failure(self) -> Optional[dict]:
        return next((c for c in self.checks if not c["passed"] and c.get("outcome") != "ELIGIBLE"), None)

    def json(self, *, for_employee: bool = False) -> dict:
        checks = self.checks
        if for_employee:  # never explain a structural or protected reason to the employee (§7.1)
            checks = [c for c in checks if c["code"] not in STRUCTURAL_CHECKS]
        return {"status": self.status, "available_from": self.available_from.isoformat() if self.available_from else None,
                "checks": checks}


def map_gender(value: Optional[str]) -> str:
    text = (value or "").strip().lower()
    if not text:
        return "UNSPECIFIED"
    if text in ("female", "f", "woman"):
        return "FEMALE"
    if text in ("male", "m", "man"):
        return "MALE"
    return "OTHER"


def _tenure_reached(employee: Employee, tenure: Mapping[str, Any]) -> Optional[date]:
    from app.services.leave_cycles import add_months

    basis = tenure.get("basis") or "SERVICE_START"
    if basis == "CONFIRMATION":
        start = employee.confirmation_date
    elif basis == "HIRE_DATE":
        start = employee.hire_date
    else:
        start = employee.service_start_date or employee.hire_date
    if start is None:
        return None
    value, unit = int(tenure["value"]), tenure.get("unit") or "MONTHS"
    if unit == "DAYS":
        return start + timedelta(days=value)
    return add_months(start, value * (12 if unit == "YEARS" else 1))


def eligibility(db: Session, company_id: int, employee: Employee, leave_type: Optional[LeaveType],
                rule: Optional[Mapping[str, Any]], on: date) -> Eligibility:
    """The checks of §7.1 for one date; status = the most restrictive outcome."""
    checks: List[dict] = []
    available_from: Optional[date] = None

    def check(code: str, passed: bool, outcome: str = "NOT_ELIGIBLE", detail: Optional[str] = None) -> None:
        checks.append({"code": code, "passed": passed, "outcome": "ELIGIBLE" if passed else outcome,
                       **({"detail": detail} if detail else {})})

    employed = employee.hire_date <= on and (employee.termination_date is None or on <= employee.termination_date)
    check("EMPLOYED", employed, detail=None if employed else "OUTSIDE_EMPLOYMENT")
    check("IN_PLAN", rule is not None, detail=None if rule is not None else "NOT_IN_PLAN")
    active = leave_type is not None and leave_type.is_active and not (rule or {}).get("frozen")
    check("TYPE_ACTIVE", active)
    if rule is not None:
        elig = rule.get("eligibility") or {}
        statutory = False  # no VERIFIED leave packs yet (13 §4.3): unknown data follows the plan's choice
        where = placement(db, company_id, employee.id, on)
        if elig.get("worker_classes"):
            known = where is not None and where.worker_class is not None
            check("WORKER_CLASS", known and where.worker_class in elig["worker_classes"],
                  "ELIGIBLE_VERIFY" if not known and statutory else "NOT_ELIGIBLE")
        if elig.get("employment_types"):
            check("EMPLOYMENT_TYPE", where is not None and where.employment_type in elig["employment_types"])
        if elig.get("countries") or elig.get("branch_ids"):
            ok = where is not None and (not elig.get("countries") or where.country in elig["countries"]) and (
                not elig.get("branch_ids") or where.branch_id in elig["branch_ids"])
            check("LOCATION", ok)
        if elig.get("min_fte") is not None:
            check("FTE", where is not None and where.fte >= Decimal(str(elig["min_fte"])))
        if elig.get("genders"):
            mapped = map_gender(employee.gender)
            if mapped in elig["genders"]:
                check("GENDER", True)
            elif mapped == "UNSPECIFIED":
                check("GENDER", False, elig.get("unknown_gender") or "ELIGIBLE_VERIFY",
                      "matches the plan's eligibility rule")
            else:
                check("GENDER", False, detail="matches the plan's eligibility rule")
        tenure = elig.get("min_tenure")
        if tenure:
            reached = _tenure_reached(employee, tenure)
            ok = reached is not None and on >= reached
            outcome = "ACCRUE_ONLY" if (tenure.get("mode") or "BLOCK_USE") == "BLOCK_USE" else "NOT_ELIGIBLE"
            check("TENURE", ok, outcome)
            if not ok and reached is not None:
                available_from = max(available_from or reached, reached)
        probation = rule.get("during_probation") or "ALLOW"
        if probation != "ALLOW" and employee.probation_end_date is not None:
            in_probation = on <= employee.probation_end_date and (
                employee.confirmation_date is None or on < employee.confirmation_date)
            outcome = "ACCRUE_ONLY" if probation == "ACCRUE_ONLY" else "NOT_ELIGIBLE"
            check("PROBATION", not in_probation, outcome)
            if in_probation:
                end = employee.probation_end_date
                if employee.confirmation_date is not None:
                    end = min(end, employee.confirmation_date - timedelta(days=1))
                available_from = max(available_from or end + timedelta(days=1), end + timedelta(days=1))
        if (rule.get("during_notice") or "ALLOW") == "BLOCK" and employee.notice_start_date is not None:
            in_notice = employee.notice_start_date <= on and (
                employee.termination_date is None or on <= employee.termination_date)
            check("NOTICE", not in_notice, detail="NOTICE_PERIOD" if in_notice else None)
    worst = max((STATUS_ORDER.index(c["outcome"]) for c in checks), default=0)
    return Eligibility(STATUS_ORDER[worst], checks, available_from, dict(rule) if rule is not None else None)


# --------------------------------------------------------------------------- permissions and scope (§5.7)

def _rules_of(db: Session, company_id: int, policy_id: int) -> List[PolicyAssignment]:
    return db.query(PolicyAssignment).filter(PolicyAssignment.company_id == company_id,
                                             PolicyAssignment.policy_id == policy_id).all()


def require_plan_write(db: Session, principal: Principal, policy_id: Optional[int] = None,
                       criteria: Optional[Sequence[Mapping[str, Any]]] = None) -> None:
    """COMPANY may write any plan; LE / BR holders only plans whose every assignment rule names entities / branches
    inside their targets (no ``everyone`` rule). Otherwise 403."""
    if principal.has(MANAGE, "COMPANY"):
        return
    entities = principal.scope_targets(MANAGE, "LEGAL_ENTITY")
    branches = principal.scope_targets(MANAGE, "BRANCH")
    if not entities and not branches:
        raise forbidden("Changing leave plans needs leave configuration access.")
    rules = list(criteria or []) + ([r.criteria for r in _rules_of(db, principal.company_id, policy_id)]
                                    if policy_id is not None else [])
    for item in rules:
        crit = dict(item or {})
        if crit.get("everyone"):
            raise forbidden("A plan for everyone needs company-wide leave configuration access.")
        le, br = set(crit.get("legal_entity_ids") or []), set(crit.get("branch_ids") or [])
        if not ((le and le <= entities) or (br and br <= branches)):
            raise forbidden("This plan covers people outside your legal entities or branches.")


def require_plan_read(principal: Principal) -> None:
    if not (principal.has(MANAGE) or principal.has("leave:read", "COMPANY")):
        raise forbidden("Viewing leave plans needs leave configuration access.")


# --------------------------------------------------------------------------- activation (§5.6)

def _type_rows(db: Session, company_id: int) -> Dict[str, LeaveType]:
    return {t.code: t for t in db.query(LeaveType).filter(LeaveType.company_id == company_id)}


def reference_problems(db: Session, company_id: int, fields: Mapping[str, Any], *, activation: bool) -> List[dict]:
    """§5.6 step 5 and the cross-field rules of §5.5 that need the tenant's types and settings."""
    types = _type_rows(db, company_id)
    problems: List[dict] = []
    comp_offs = 0
    codes = {r["leave_type_code"] for r in fields.get("types", [])}
    unpaid_code = leave_access.setting(db, company_id, "leave.unpaid_type_code", default="unpaid")
    hourly = bool(leave_access.setting(db, company_id, "leave.hourly.enabled", default=False))
    for i, rule in enumerate(fields.get("types", [])):
        lt = types.get(rule["leave_type_code"])
        path = f"types[{i}]"
        if lt is None:
            problems.append({"code": "INVALID_LEAVE_TYPE", "field": f"{path}.leave_type_code",
                             "message": f"There is no leave type {rule['leave_type_code']!r}."})
            continue
        if not lt.is_active and not rule.get("frozen"):
            problems.append({"code": "INVALID_LEAVE_TYPE", "field": f"{path}.leave_type_code",
                             "message": f"{lt.name} is inactive."})
        if lt.kind == "COMP_OFF":
            comp_offs += 1
            if (rule.get("balance_scope") or {}).get("kind") != "LOTS":
                problems.append({"code": "RULE_INVALID", "field": f"{path}.balance_scope.kind",
                                 "message": "A comp-off type keeps its balance as expiring credits (LOTS)."})
        elif (rule.get("balance_scope") or {}).get("kind") == "LOTS":
            problems.append({"code": "RULE_INVALID", "field": f"{path}.balance_scope.kind",
                             "message": "Only comp-off types keep expiring credits (LOTS)."})
        if lt.kind == "EVENT" and rule.get("event") is None:
            problems.append({"code": "RULE_INVALID", "field": f"{path}.event",
                             "message": f"{lt.name} is an event leave: describe the event."})
        if lt.kind != "EVENT" and rule.get("event") is not None:
            problems.append({"code": "RULE_INVALID", "field": f"{path}.event",
                             "message": f"{lt.name} is not an event leave."})
        if lt.unit == "HOUR" and not hourly:
            problems.append({"code": "PLAN_CONFLICT", "field": f"{path}.leave_type_code",
                             "message": "Hourly leave is turned off.", "details": {"reason": "HOURLY_DISABLED"}})
        if rule.get("insufficient_balance_action") == "SPLIT_TO_UNPAID" and unpaid_code not in codes:
            problems.append({"code": "PLAN_CONFLICT", "field": f"{path}.insufficient_balance_action",
                             "message": f"Add the unpaid type {unpaid_code!r} to this plan to split excess days."})
        if rule.get("during_notice") == "BLOCK" and (lt.kind == "EVENT" or lt.absence_class in ("STATUTORY", "PROTECTED")):
            problems.append({"code": "RULE_INVALID", "field": f"{path}.during_notice",
                             "message": "Notice never blocks statutory, protected or event leave."})
    if comp_offs > 1:
        problems.append({"code": "PLAN_CONFLICT", "field": "types", "message": "A plan has at most one comp-off type."})
    if comp_offs and fields.get("comp_off") is None:
        problems.append({"code": "RULE_INVALID", "field": "comp_off", "message": "Describe the comp-off claim rules."})
    return problems


def _raise_problems(problems: List[dict]) -> None:
    if problems:
        first = problems[0]
        raise AppError(400 if first["code"] != "PLAN_CONFLICT" else 409,
                       {"message": first["message"], "field": first.get("field"), "issues": problems}, first["code"])


def check_frozen_writes(before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    """``frozen`` is system-written: HR may keep or delete a frozen rule but never set or change the flag."""
    old = {r["leave_type_code"]: bool(r.get("frozen")) for r in before.get("types", [])}
    for i, rule in enumerate(after.get("types", [])):
        if bool(rule.get("frozen")) and old.get(rule["leave_type_code"]) is not True:
            raise AppError(400, {"message": "Frozen rules are kept by the system only.",
                                 "field": f"types[{i}].frozen"}, "RULE_INVALID")


def latest_local_today(db: Session, company_id: int) -> date:
    """The furthest-ahead employee-local today among the tenant's zones (back-dating needs a reason, §5.6 step 3)."""
    import pytz

    from app.models.company import Company

    zones = {z for (z,) in db.query(EmployeeAssignment.work_timezone).filter(
        EmployeeAssignment.company_id == company_id, EmployeeAssignment.work_timezone.isnot(None)).distinct()}
    zones.add(db.get(Company, company_id).timezone or "UTC")
    now = clock.utcnow()
    days = []
    for zone in zones:
        try:
            days.append(now.astimezone(pytz.timezone(zone)).date())
        except Exception:
            continue
    return max(days) if days else clock.company_today(db, company_id)


def activate(db: Session, principal: Principal, policy_id: int, version_no: int, *, effective_from: Optional[date],
             reason: Optional[str], confirm: bool = False, recalc_future_leave: bool = False) -> dict:
    """§5.6: optional second person, reference checks, then the policy framework's activation."""
    from app.payroll.services import policies

    require_plan_write(db, principal, policy_id)
    policy = policies._load(db, principal.company_id, policy_id, lock=True)
    if policy.type != PLAN:
        raise not_found("Leave plan")
    row = policies._version(db, principal.company_id, policy_id, version_no, lock=True)
    if row.status != "DRAFT":
        raise AppError(409, "This version is already active or superseded.", "VERSION_NOT_DRAFT")
    _raise_problems(reference_problems(db, principal.company_id, row.fields or {}, activation=True))
    start = effective_from or row.effective_from
    second = bool(leave_access.setting(db, principal.company_id, "leave.plan.requires_second_person", default=False))
    if second and confirm and row.activation_request:  # the checker confirms the maker's stored request
        start = date.fromisoformat(row.activation_request["effective_from"])
        reason = row.activation_request.get("reason") or reason
    if start < latest_local_today(db, principal.company_id) and not (reason or "").strip():
        raise bad_request("A reason is required to activate a version effective in the past.", "REASON_REQUIRED")
    requested_hash = pt.policy_hash({"fields": row.fields, "effective_from": start.isoformat(),
                                     "recalc": recalc_future_leave})
    if second:
        if not confirm:
            row.activation_requested_by_user_id = principal.user_id
            row.activation_requested_at = utcnow()
            row.activation_request = {"hash": requested_hash, "effective_from": start.isoformat(),
                                      "reason": (reason or "").strip() or None, "recalc_future_leave": recalc_future_leave}
            db.flush()
            AuditLogService.log(db, principal, "Leave", "PLAN_ACTIVATION_REQUESTED",
                                f"Requested activation of version {version_no} of \"{policy.name}\" from {start}",
                                "policy", policy.id, details={"version": version_no})
            return {"status": "AWAITING_SECOND_PERSON", "version": version_no,
                    "message": "A second leave administrator must confirm this activation."}
        pending = row.activation_request or {}
        if row.activation_requested_by_user_id is None:
            raise AppError(409, "No activation is waiting for confirmation.", "INVALID_TRANSITION")
        if row.activation_requested_by_user_id == principal.user_id:
            raise AppError(403, {"message": "Someone else must confirm your activation request.",
                                 "details": {"cause": "MAKER"}}, "SELF_DECISION_FORBIDDEN")
        if pending.get("hash") != pt.policy_hash({"fields": row.fields, "effective_from": pending.get("effective_from"),
                                                  "recalc": pending.get("recalc_future_leave", False)}):
            raise AppError(409, "The draft changed after the activation was requested.", "PREVIEW_CHANGED")
        start = date.fromisoformat(pending["effective_from"])
        reason = pending.get("reason") or reason
    result = policies.activate_version(db, principal, policy_id, version_no, effective_from=start, reason=reason)
    row.activation_request = None
    db.flush()
    _memo(db).clear()
    recount = 0
    if recalc_future_leave:
        from app.services import leave_recalc

        recount = leave_recalc.recount_from(db, principal.company_id, start, actor_user_id=principal.user_id)
    return result | {"recalculated_requests": recount}


# --------------------------------------------------------------------------- explain and simulate (§10)

def _types_by_code(db: Session, company_id: int) -> Dict[str, LeaveType]:
    return _type_rows(db, company_id)


def explain(db: Session, company_id: int, employee: Employee, on: date) -> dict:
    """§10.2: which plan applies to the employee on ``on`` and, per type, eligibility, cycle and yearly entitlement."""
    resolution = resolve(db, company_id, employee.id, on)
    types = _types_by_code(db, company_id)
    items = []
    for rule in resolution.fields.get("types", []):
        lt = types.get(rule["leave_type_code"])
        elig = eligibility(db, company_id, employee, lt, rule, on)
        cycle = cycle_of(db, company_id, employee, rule, on, resolution)
        ent = rule.get("entitlement") or {}
        yearly = earned_to_date(ent, cycle, cycle.end, joined=employee.hire_date, left=employee.termination_date,
                                joiner=(rule.get("proration") or {}).get("joiner", "PRORATE"),
                                leaver=(rule.get("proration") or {}).get("leaver", "PRORATE"),
                                service_start=employee.service_start_date or employee.hire_date,
                                rounding=(rule.get("rounding") or {}).get("mode", "NONE_2DP"))
        items.append({
            "leave_type_code": rule["leave_type_code"], "name": lt.name if lt else rule["leave_type_code"],
            "eligibility": elig.json(), "cycle": {"start": cycle.start.isoformat(), "end": cycle.end.isoformat()},
            "entitlement": {"method": ent.get("method"), "amount": ent.get("amount"),
                            "frequency": ent.get("frequency"), "this_cycle": str(yearly)},
            "carry_forward": rule.get("carry_forward"), "encashment": rule.get("encashment"),
            "counting_basis": (rule.get("counting_basis") or {}).get("basis"),
            "sandwich": (rule.get("sandwich") or {}).get("mode", "OFF"),
            "frozen": bool(rule.get("frozen")),
        })
    return {"employee_id": employee.id, "date": on.isoformat(), "plan": resolution.json(), "types": items,
            "v2_enabled": leave_access.v2_enabled(db, company_id)}


def simulate(db: Session, principal: Principal, policy_id: int, version_no: int, *, employee_ids: Optional[List[int]],
             on: Optional[date]) -> dict:
    """§10.1: what a version would give each employee (in the caller's scope) on ``on``; never writes."""
    from app.core import authz
    from app.payroll.services import policies

    require_plan_read(principal)
    policy = policies._load(db, principal.company_id, policy_id)
    if policy.type != PLAN:
        raise not_found("Leave plan")
    row = policies._version(db, principal.company_id, policy_id, version_no)
    day = on or clock.company_today(db, principal.company_id)
    scope = authz.employee_scope(db, principal, "leave:read")
    query = db.query(Employee).filter(Employee.company_id == principal.company_id, Employee.is_active.is_(True),
                                      scope.clause(Employee.id))
    if employee_ids:
        query = query.filter(Employee.id.in_(employee_ids))
    people = query.order_by(Employee.id).limit(200).all()
    types = _types_by_code(db, principal.company_id)
    fields = row.fields or {}
    resolution = Resolution("SIMULATED", fields, policy_id=policy.id, policy_name=policy.name, version_id=row.id,
                            version=row.version)
    results = []
    for person in people:
        entries = []
        for rule in fields.get("types", []):
            lt = types.get(rule["leave_type_code"])
            elig = eligibility(db, principal.company_id, person, lt, rule, day)
            cycle = cycle_of(db, principal.company_id, person, rule, day, resolution)
            ent = rule.get("entitlement") or {}
            yearly = earned_to_date(ent, cycle, cycle.end, joined=person.hire_date, left=person.termination_date,
                                    joiner=(rule.get("proration") or {}).get("joiner", "PRORATE"),
                                    leaver=(rule.get("proration") or {}).get("leaver", "PRORATE"),
                                    service_start=person.service_start_date or person.hire_date,
                                    rounding=(rule.get("rounding") or {}).get("mode", "NONE_2DP"))
            entries.append({"leave_type_code": rule["leave_type_code"], "status": elig.status,
                            "available_from": elig.available_from.isoformat() if elig.available_from else None,
                            "this_cycle": str(yearly), "cycle_start": cycle.start.isoformat(),
                            "cycle_end": cycle.end.isoformat(), "unlimited": bool(lt and lt.is_unlimited)})
        results.append({"employee_id": person.id, "name": person.full_name, "types": entries})
    return {"policy_id": policy.id, "version": row.version, "date": day.isoformat(), "employees": results,
            "truncated": len(people) == 200}

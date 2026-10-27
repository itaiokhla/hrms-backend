"""The snapshot builder: freezes every calculation input of one run employee (3.5 §5.3, §8.1; 3.4 §2, §16).

``build_snapshot(db, run, run_employee, *, lock_time)`` returns the JSON form of
``app.payroll.engine.snapshot.Snapshot`` (normalized by ``Snapshot.from_dict(...).to_dict()``, so the
engine can calculate it as is and ``content_hash`` is stable). ``build(...)`` returns the same content
with its content hash, the ``source_hashes`` ({"<source type>:<id>": sha256 of the canonical content of
every source record used}) and the issues found while building (FX_MISSING, CONFIG_INVALID ...).
``lock_inputs(db, run, employees)`` builds and stores the snapshots of the given run employees in the
caller's transaction (the calculate step moves the run to CALCULATING in the same transaction): one new
immutable ``payroll_input_snapshots`` version per run employee, the run employee pointing at it, approved
wage inputs reserved and WITH_PAYROLL claims scheduled for that run employee (released again by the run's
RELEASE_INPUTS effect), the issues recorded as SYSTEM exceptions of the run, and the change-detection job
scheduled. Services never commit.

What a snapshot holds and where it comes from (one employee, one run):

* run, period (dates, pay date, frequency, periods in the tax year, number in the tax year, tax-year
  start), environment = the run's environment (fixture packs never run in production, I-07);
* employee facts on the last date the employee is paid by this run (``compensation.employee_facts``):
  worker class, employment type, grade, designation, FTE, branch subdivision, hire/exit dates, the shift's
  standard hours; ``salaried`` from the structure type;
* settings resolved for the employee's scope on that date (PERIOD grain) and DAY-grain switches as date
  ranges, with the run environment's country-pack constraints (FOLLOW-UPS F-14);
* policies per type (version id, version, ``policy_hash``, fields) resolved on that date
  (``policies.resolve_for_employee``); a DAY-grain policy that changes inside the period is reported
  (POLICY_CHANGED_MID_PERIOD: the engine applies one version per period);
* the day ledger of every date of the period (``time_inputs.ledger_inputs``, F-02..F-04) with each date's
  segment; dates on which another legal entity or a pay group of another frequency pays the employee are
  not employed by this run (EC-ORG-06, 3.5 §14). Pay groups whose attendance cycle differs from the pay
  period are refused with a blocking ATTENDANCE_CYCLE_UNSUPPORTED while attendance or leave is ON (the
  engine's ledger covers the pay period's dates only; no silent re-dating);
* assignment segments: one per (placement, compensation assignment) run of dates, so transfers,
  revisions and FTE changes split the period (EC-CMP-16); compensation per segment from
  ``compensation.segments`` (structure version, component overrides), converted into the pay currency by
  the assignment's FX rule (FIXED_RATE) or the run's FX snapshot — FX_MISSING when no rate exists, never
  1:1 (3.2 §4.5, EC-CMP-07);
* the component catalogue and formula tables in force on the period end;
* approved overtime of the attendance cycle, holiday/weekly-off work and shifts (only while one of their
  switches is ON somewhere in the period), approved unpaid tasks of the period (attribution per
  TASK_PAY_POLICY) with the rate card;
* incentive plans (``payroll.incentives.enabled`` ON, decision I-22): in a period that ends a payout window of
  the employee's INCENTIVE_PLAN the ``incentive`` block (window, the employee's target: own, else the grade's)
  and the approved achievement measures (wage inputs with source INCENTIVE_PLAN) targeted inside the window;
  in any other period of an ON plan the measures are left out (unreserved: they wait for the payout run). With
  the switch OFF or no plan, the period's measures are frozen as they are and the engine reports them unpaid;
* approved wage inputs targeting the period, WITH_PAYROLL claims, loan and advance instalments due,
  approved arrears instalments (``retro.arrears_for_snapshot``); a BONUS / CORRECTION off-cycle run keeps
  only the structure lines and inputs of its selected components (``off_cycle.selected_components``);
* YTD per legal entity, tax year and currency from ``ytd_balances`` (F-17, EC-STA-08/22); the previous
  finalized regular periods (variance and outliers); the country pack frozen for the period and the
  ``pack_state`` carried from the previous finalized run (its snapshot's state plus the determinations
  that run made: stored stage outputs, its PACK_STATE_DETERMINED flag, or a replay of its snapshot, F-15);
* the active statutory attestation (I-15), no tax profile yet (null), the bank-account snapshot of the
  pay date (``bank_accounts.engine_bank_accounts``; empty when the payments module is absent);
* reviewer overrides of ledger days recorded on the run: a FIXED or WAIVED exception of the run employee
  whose ``details`` carry ``{"ledger_override": {"date": "YYYY-MM-DD", "action": "TREAT_AS_PRESENT" |
  "TREAT_AS_ABSENT" | "TREAT_AS_HALF_DAY"}}`` (see ``ledger_override_details``).

Hooks installed by ``install_hooks()`` (at import, like ``runs.install_hooks``):
``policies.policy_in_use`` answers from the stored snapshots (EC-CFG-16), ``settings.pack_constraints``
passes the environment to the country packs (F-14), and ``loans`` gets its pay-figures provider (F-36).
"""

from __future__ import annotations

import inspect
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import and_, func, or_
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.core import clock
from app.core.errors import AppError, conflict, not_found
from app.models.attendance import Attendance
from app.models.compensation import CompensationAssignment, SalaryTemplate, SalaryTemplateVersion
from app.models.employee import Employee
from app.models.org import Branch, EmployeeAssignment, LegalEntity
from app.models.pay_org import PayGroup
from app.models.pay_runs import (
    PayCalendar,
    PayPeriod,
    PayrollException,
    PayrollInputSnapshot,
    PayrollLine,
    PayrollRun,
    PayrollRunEmployee,
    YtdBalance,
)
from app.models.payroll_config import (
    PlatformSettingValue,
    PolicyVersion,
    PayrollTable,
    PayrollTableRow,
    SettingValue,
)
from app.models.time_inputs import EmployeeSuspension
from app.models.variable_inputs import ReimbursementClaim
from app.payroll import calendar as paycal
from app.payroll import incentives
from app.payroll import packs
from app.payroll import policy_types as pt
from app.payroll import settings_registry as reg
from app.payroll import state_machines as sm
from app.payroll.engine import snapshot as snap
from app.payroll.money import CALC_CONTEXT
from app.payroll.services import compensation as compensation_service
from app.payroll.services import components as component_service
from app.payroll.services import fx as fx_service
from app.payroll.services import policies as policy_service
from app.payroll.services import settings as settings_engine
from app.payroll.services import time_inputs

ONE_DAY = timedelta(days=1)
ZERO = Decimal(0)

SYSTEM_SOURCE = "SYSTEM"
ISSUE_PREFIX = "SNAPSHOT"
OVERRIDE_KEY = "ledger_override"
# Run states in which inputs may be (re-)locked: right before or right after the CALCULATE /
# RECALCULATE transition, and while a recalculation of selected employees is prepared.
LOCKABLE_STATES = ("READY", "CALCULATING", "CALCULATED", "VALIDATION_FAILED", "REVIEW_REQUIRED", "REJECTED",
                   "REOPENED_FOR_CORRECTION")
# Settings sections that never change an amount stay out of the snapshot (their services read them live).
EXCLUDED_SECTIONS = frozenset({reg.S_PAYSLIPS, reg.S_NOTIFICATIONS, reg.S_ACCOUNTING, reg.S_PRIVACY})
SKIPPED_POLICY_TYPES = frozenset({"APPROVAL_CHAIN", "PAYSLIP_TEMPLATE"})
NON_SALARIED_STRUCTURES = frozenset({"HOURLY", "DAILY", "TASK_BASED"})
NON_WORKING_DAY_SWITCHES = ("payroll.holiday_work_premium.enabled", "payroll.weekly_off_pay.enabled",
                            "payroll.overtime.enabled")
SHIFT_SWITCH = "payroll.shift_allowance.enabled"
TIME_SWITCHES = ("payroll.attendance.enabled", "payroll.leave.enabled")
FIXED_PERIODS = {"MONTHLY": 12, "SEMI_MONTHLY": 24}
MIN_PERIODS = {"WEEKLY": 52, "BI_WEEKLY": 26, "DAILY": 365}
HISTORY_PERIODS = 6
OVERRIDE_ACTIONS = ("TREAT_AS_PRESENT", "TREAT_AS_ABSENT", "TREAT_AS_HALF_DAY")
FX_ROUNDING_NOTE = "converted at full precision; lines are rounded by the engine"
# Off-cycle component selection (``off_cycle.selected_components``): the time-based inputs that produce
# each engine time-pay component; an input list is kept only when one of its components is selected.
TIME_PAY_INPUTS = {
    "overtime_entries": frozenset({"OT_WORKDAY", "OT_WEEKLY_OFF", "OT_HOLIDAY"}),
    "non_working_day_work": frozenset({"OT_WEEKLY_OFF", "OT_HOLIDAY", "HOLIDAY_PREMIUM", "WEEKLY_OFF_PREMIUM"}),
    "shifts": frozenset({"SHIFT_ALLOWANCE"}),
    "tasks": frozenset({"TASK_EARNING"}),
}
COMPONENT_INPUTS = ("wage_inputs", "arrears", "loans", "advances")
REIMBURSEMENT_COMPONENT = "REIMBURSEMENT"
# The source key each input list item was recorded under (``_Builder.source``): an item an off-cycle selection
# leaves out has its source hash dropped too, so a later change of it never marks the run stale. Lists whose
# items share sources with what stays (structure lines, attendance-derived work) keep every hash (fail closed).
SELECTION_SOURCE_KEYS = {
    "wage_inputs": lambda item: f"wage_input:{item.get('id')}",
    "arrears": lambda item: f"arrears_instalment:{item.get('id')}",
    "loans": lambda item: f"loan:{item.get('loan_id')}",
    "advances": lambda item: f"loan:{item.get('loan_id')}",
    "reimbursements": lambda item: f"claim:{item.get('id')}",
    "overtime_entries": lambda item: str(item.get("entry_id")),
}


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SnapshotIssue:
    """A problem found while freezing one employee's inputs; stored as a SYSTEM exception of the run."""

    code: str
    severity: str  # BLOCKING | WARNING | FLAG
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)
    key: str = ""
    waivable: bool = False

    def dedupe_key(self, run_employee_id: int) -> str:
        return f"{ISSUE_PREFIX}:{self.code}:{run_employee_id}" + (f":{self.key}" if self.key else "")

    def to_json(self) -> dict:
        return {"code": self.code, "severity": self.severity, "message": self.message,
                "details": _plain(dict(self.details)), "waivable": self.waivable}


@dataclass
class SnapshotBuild:
    """``content`` is the normalized engine snapshot JSON; ``source_hashes`` what change detection compares."""

    content: dict
    content_hash: str
    source_hashes: Dict[str, str]
    issues: List[SnapshotIssue]


@dataclass
class LockResult:
    """What ``lock_inputs`` stored: one snapshot per run employee, the issues found, and failures."""

    snapshots: Dict[int, PayrollInputSnapshot] = field(default_factory=dict)
    issues: Dict[int, List[SnapshotIssue]] = field(default_factory=dict)
    failed: Dict[int, str] = field(default_factory=dict)
    fx_snapshot_id: Optional[int] = None

    @property
    def blocking(self) -> Dict[int, List[SnapshotIssue]]:
        return {k: [i for i in v if i.severity == "BLOCKING"] for k, v in self.issues.items()
                if any(i.severity == "BLOCKING" for i in v)}


class SnapshotBuildError(AppError):
    """The employee's inputs cannot be frozen (a defect in a source record); 409 SNAPSHOT_FAILED."""

    def __init__(self, message: str, details: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(409, message, "SNAPSHOT_FAILED")
        self.details = dict(details or {})


# ---------------------------------------------------------------------------
# JSON helpers and hashing
# ---------------------------------------------------------------------------
def _plain(value: Any) -> Any:
    """JSON-safe copy: Decimals and dates as strings, tuples/sets as lists; a float becomes its decimal
    string (no money is a float; settings stored as JSON numbers are normalized, never hashed as floats)."""
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        return format(Decimal(str(value)), "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_plain(v) for v in value]
        return sorted(items, key=repr) if isinstance(value, (set, frozenset)) else items
    if hasattr(value, "value") and isinstance(getattr(value, "value"), (str, int)):
        return value.value
    return str(value)


def source_hash(content: Any) -> str:
    """SHA-256 of the canonical JSON of a source record (the same function the source change log uses)."""
    return time_inputs.content_hash(_plain(content))


def source_key(source_type: str, source_id: Any) -> str:
    return f"{source_type}:{source_id}"


def _dec(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _iso(value: Optional[date]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def ledger_override_details(day: date, action: str) -> dict:
    """The ``details`` entry a review service records on a FIXED/WAIVED exception so the next snapshot
    resolves that ledger day (``ledger.LedgerOverride``)."""
    if action not in OVERRIDE_ACTIONS:
        raise ValueError(f"action must be one of {OVERRIDE_ACTIONS}")
    return {OVERRIDE_KEY: {"date": day.isoformat(), "action": action}}


# ---------------------------------------------------------------------------
# FX: one frozen snapshot of rates per run (fx_snapshots), reused by every employee and re-snapshot
# ---------------------------------------------------------------------------
class FxBook:
    """Rates into the run currency. Rates already frozen in the run's FX snapshot win; a missing pair is
    looked up on the conversion date (``payroll.fx.conversion_date_rule``) and, unless ``probe``, frozen
    into a new run snapshot holding the old and the new rates. Nothing is ever converted 1:1."""

    def __init__(self, db: Session, run: PayrollRun, period: PayPeriod, *, lock_time: datetime,
                 probe: bool = False) -> None:
        self.db, self.run, self.period, self.probe = db, run, period, probe
        self.lock_time = lock_time
        self.quote = run.currency
        self.frozen: Dict[str, Any] = {}          # currency -> fx.Rate (from the run's snapshot)
        self.pending: Dict[str, Any] = {}         # currency -> fx.Rate looked up now
        self.missing: Dict[str, str] = {}         # currency -> issue code
        self.snapshot_id: Optional[int] = run.fx_snapshot_id
        self._conversion_on: Optional[date] = None
        if self.snapshot_id is not None:
            for rate in fx_service.snapshot_rates(db, self.snapshot_id):
                if rate.quote == self.quote:
                    self.frozen.setdefault(rate.base, rate)

    @property
    def conversion_date(self) -> date:
        if self._conversion_on is None:
            rule = "PAY_DATE"
            try:
                rule = fx_service.settings_for(self.db, self.run.company_id, self.period.period_end).conversion_date_rule
            except AppError:
                pass
            self._conversion_on = {"PERIOD_END": self.period.period_end,
                                   "CALCULATION_DATE": self.lock_time.date()}.get(rule, self.period.pay_date)
        return self._conversion_on

    def ensure(self, currencies: Iterable[str]) -> None:
        for currency in sorted({c for c in currencies if c and c != self.quote}):
            if currency in self.frozen or currency in self.pending or currency in self.missing:
                continue
            found = fx_service.lookup(self.db, self.run.company_id, currency, self.quote, self.conversion_date)
            if found.ok:
                self.pending[currency] = found.rate
            else:
                self.missing[currency] = found.issue.code

    def rate(self, currency: str) -> Optional[Decimal]:
        found = self.frozen.get(currency) or self.pending.get(currency)
        return None if found is None else Decimal(found.rate)

    def freeze(self) -> Optional[int]:
        """Capture the run's FX snapshot when new rates were looked up (no-op in probe mode)."""
        if self.probe or not self.pending:
            return self.snapshot_id
        ids = [r.rate_id for r in self.frozen.values() if r.rate_id is not None]
        for currency, rate in sorted(self.pending.items()):
            rate_id = rate.rate_id
            if rate_id is None or rate.derivation is not None:
                persist = getattr(fx_service, "_persist_derived", None)
                if persist is None:
                    self.missing[currency] = "RATE_NOT_PERSISTED"
                    continue
                rate_id = persist(self.db, self.run.company_id, rate).id
            ids.append(rate_id)
        self.snapshot_id = fx_service.capture_snapshot(self.db, ids, purpose="PAYROLL_RUN", run_id=self.run.id)
        self.frozen = {r.base: r for r in fx_service.snapshot_rates(self.db, self.snapshot_id) if r.quote == self.quote}
        self.pending = {}
        self.run.fx_snapshot_id = self.snapshot_id
        return self.snapshot_id


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------
@dataclass
class _Placement:
    row: EmployeeAssignment
    in_run: bool


class _Builder:
    def __init__(self, db: Session, run: PayrollRun, row: PayrollRunEmployee, *, lock_time: datetime,
                 fx: Optional[FxBook] = None, probe: bool = False) -> None:
        if row.company_id != run.company_id or row.run_id != run.id:
            raise not_found("Run employee")
        if row.status == "EXCLUDED":
            raise conflict("An excluded employee has no inputs to lock.", "RUN_EMPLOYEE_EXCLUDED")
        self.db, self.run, self.row, self.probe = db, run, row, probe
        self.cid, self.eid = run.company_id, row.employee_id
        self.lock_time = lock_time
        self.period = db.query(PayPeriod).filter(PayPeriod.company_id == self.cid,
                                                 PayPeriod.id == run.pay_period_id).one()
        self.start, self.end = self.period.period_start, self.period.period_end
        self.entity = db.query(LegalEntity).filter(LegalEntity.company_id == self.cid,
                                                   LegalEntity.id == run.legal_entity_id).one()
        self.group = db.query(PayGroup).filter(PayGroup.company_id == self.cid, PayGroup.id == run.pay_group_id).one()
        self.employee = db.query(Employee).filter(Employee.company_id == self.cid, Employee.id == self.eid).first()
        if self.employee is None:
            raise not_found("Employee")
        self.fx = fx or FxBook(db, run, self.period, lock_time=lock_time, probe=probe)
        self.hashes: Dict[str, str] = {}
        self.issues: List[SnapshotIssue] = []
        self.conversions: List[dict] = []
        self.fx_used: Dict[str, Decimal] = {}
        self._frequencies: Dict[int, Optional[str]] = {}

    # -- helpers --------------------------------------------------------------------------------
    def source(self, source_type: str, source_id: Any, content: Any = None, *, digest: Optional[str] = None) -> None:
        self.hashes[source_key(source_type, source_id)] = digest or source_hash(content)

    def issue(self, code: str, severity: str, message: str, key: str = "", waivable: bool = False,
              **details: Any) -> None:
        self.issues.append(SnapshotIssue(code, severity, message[:1000], details, key, waivable))

    def frequency_of(self, pay_group_id: Optional[int]) -> Optional[str]:
        if pay_group_id is None:
            return None
        if pay_group_id not in self._frequencies:
            group = self.db.query(PayGroup).filter(PayGroup.company_id == self.cid, PayGroup.id == pay_group_id).first()
            self._frequencies[pay_group_id] = group.pay_frequency if group is not None else None
        return self._frequencies[pay_group_id]

    # -- placements and dates -------------------------------------------------------------------
    def load_placements(self) -> None:
        own = self.db.query(EmployeeAssignment).filter(
            EmployeeAssignment.company_id == self.cid, EmployeeAssignment.id == self.row.employee_assignment_id).first()
        if own is None or own.employee_id != self.eid:
            raise not_found("Run employee")
        self.primary_row = own.is_primary
        if own.is_primary:
            rows = time_inputs.primary_assignments(self.db, self.cid, self.eid)
        else:
            rows = [own]
        self.placements: List[_Placement] = []
        for placement in rows:
            in_run = placement.legal_entity_id == self.run.legal_entity_id and (
                placement.pay_group_id == self.run.pay_group_id
                or (placement.pay_group_id is not None
                    and self.frequency_of(placement.pay_group_id) == self.group.pay_frequency))
            self.placements.append(_Placement(placement, in_run))
        for item in self.placements:
            p = item.row
            if p.effective_from <= self.end and (p.effective_to is None or p.effective_to >= self.start):
                self.source("assignment", p.id, {
                    "id": p.id, "employee_id": p.employee_id, "legal_entity_id": p.legal_entity_id,
                    "branch_id": p.branch_id, "pay_group_id": p.pay_group_id, "is_primary": p.is_primary,
                    "effective_from": p.effective_from, "effective_to": p.effective_to, "fte": p.fte,
                    "grade_id": p.grade_id, "designation_id": p.designation_id,
                    "employment_type_id": p.employment_type_id, "shift_id": p.shift_id,
                    "work_week_pattern_id": p.work_week_pattern_id, "department_id": p.department_id})
        e = self.employee
        self.source("employee", e.id, {"id": e.id, "code": e.employee_code, "hire_date": e.hire_date,
                                        "termination_date": e.termination_date, "date_of_birth": e.date_of_birth,
                                        "gender": e.gender})

    def placement_on(self, day: date) -> Optional[_Placement]:
        for item in self.placements:
            p = item.row
            if p.effective_from <= day and (p.effective_to is None or day <= p.effective_to):
                return item
        return None

    def employed_on(self, day: date) -> bool:
        e = self.employee
        if e.hire_date is not None and day < e.hire_date:
            return False
        return e.termination_date is None or day <= e.termination_date

    def paid_dates(self) -> List[date]:
        result = []
        day = self.start
        while day <= self.end:
            found = self.placement_on(day)
            if found is not None and found.in_run and self.employed_on(day):
                result.append(day)
            day += ONE_DAY
        return result

    # -- settings -------------------------------------------------------------------------------
    def scope_on(self, day: date) -> settings_engine.Context:
        found = self.placement_on(day)
        if found is None or not found.in_run:
            found = self.facts_placement
        placement = found.row if found is not None else None
        return settings_engine.build_context(
            self.db, self.cid, on=day, employee_id=self.eid, legal_entity_id=self.run.legal_entity_id,
            pay_group_id=self.run.pay_group_id,
            branch_id=placement.branch_id if placement is not None and placement.legal_entity_id ==
            self.run.legal_entity_id else None)

    def resolve_settings(self) -> Tuple[dict, dict]:
        keys = [k for k, d in reg.REGISTRY.items() if d.section not in EXCLUDED_SECTIONS]
        resolved = settings_engine.resolve_in_context(self.db, self.scope_on(self.facts_date), keys, strict=False)
        values: Dict[str, Any] = {}
        for key in sorted(resolved):
            found = resolved[key]
            defn = reg.REGISTRY[key]
            errors = [i for i in found.issues if i.severity == "ERROR"]
            if errors or (found.value is reg.UNSET and defn.fails_closed):
                message = "; ".join(i.message for i in errors) or "no value is configured"
                self.issue("CONFIG_INVALID", "BLOCKING" if defn.fails_closed else "WARNING",
                           f"Setting {key} cannot be frozen for this employee: {message}", key=key, setting=key,
                           level=found.level)
                continue
            values[key] = _plain(found.json_value)
        ranges = self.day_ranges([k for k in keys if reg.REGISTRY[k].resolution_grain == "DAY" and k in values])
        self.source("settings", "period", values)
        self.source("settings", "day_ranges", ranges)
        return values, ranges

    def day_ranges(self, keys: Sequence[str]) -> Dict[str, list]:
        """DAY-grain switches as the date ranges on which they are ON (EC-CFG-02, EC-OT-01); their PERIOD
        value in ``settings`` is the one on the facts date."""
        keys = [k for k in keys if reg.REGISTRY[k].type == "bool"]
        if not keys:
            return {}
        boundaries = {self.start}

        def add(day: Optional[date]) -> None:
            if day is not None and self.start < day <= self.end:
                boundaries.add(day)

        for start, end in self.db.query(SettingValue.effective_from, SettingValue.effective_to).filter(
                SettingValue.company_id == self.cid, SettingValue.key.in_(list(keys)), SettingValue.status == "ACTIVE",
                SettingValue.effective_from <= self.end,
                or_(SettingValue.effective_to.is_(None), SettingValue.effective_to >= self.start)):
            add(start)
            add(end + ONE_DAY if end else None)
        for start, end in self.db.query(PlatformSettingValue.effective_from, PlatformSettingValue.effective_to).filter(
                PlatformSettingValue.key.in_(list(keys)), PlatformSettingValue.effective_from <= self.end,
                or_(PlatformSettingValue.effective_to.is_(None), PlatformSettingValue.effective_to >= self.start)):
            add(start)
            add(end + ONE_DAY if end else None)
        for item in self.placements:
            add(item.row.effective_from)
            add(item.row.effective_to + ONE_DAY if item.row.effective_to else None)
        days = sorted(boundaries)
        per_day: List[Tuple[date, date, Dict[str, Any]]] = []
        for index, day in enumerate(days):
            until = days[index + 1] - ONE_DAY if index + 1 < len(days) else self.end
            found = settings_engine.resolve_in_context(self.db, self.scope_on(day), list(keys), strict=False)
            per_day.append((day, until, {k: (None if r.value is reg.UNSET else r.json_value) for k, r in found.items()}))
        ranges: Dict[str, list] = {}
        for key in keys:
            spans: List[List[date]] = []
            for start, until, values in per_day:
                if values.get(key) is True:
                    if spans and spans[-1][1] + ONE_DAY == start:
                        spans[-1][1] = until
                    else:
                        spans.append([start, until])
            ranges[key] = [[s.isoformat(), e.isoformat()] for s, e in spans]
        return ranges

    def switch_any(self, key: str) -> bool:
        if key in self.switch_ranges:
            return bool(self.switch_ranges[key])
        return self.settings.get(key) is True

    # -- policies -------------------------------------------------------------------------------
    def resolve_policies(self) -> dict:
        result: Dict[str, dict] = {}
        for code in sorted(pt.POLICY_TYPES):
            ptype = pt.POLICY_TYPES[code]
            if code in SKIPPED_POLICY_TYPES or not ptype.assignable:
                continue
            found = policy_service.resolve_for_employee(self.db, self.cid, code, self.eid, self.facts_date)
            gates_on = [k for k in ptype.gate_switches if self.switch_any(k)]
            if found.get("status") != "RESOLVED":
                if gates_on:
                    self.issue("CONFIG_INVALID", "BLOCKING", f"{', '.join(gates_on)} is ON but no active {code} policy "
                               f"applies to this employee ({found.get('status')}).", key=f"V-11:{code}", rule="V-11",
                               policy_type=code, status=found.get("status"))
                continue
            version = found["version"]
            result[code] = {"id": version["id"], "version": version["version"],
                            "policy_hash": version.get("policy_hash"), "fields": _plain(version.get("fields") or {})}
            self.source("policy_version", version["id"], digest=version.get("policy_hash") or source_hash(version))
            if ptype.grain == "DAY":
                other = self.db.query(PolicyVersion.id).filter(
                    PolicyVersion.company_id == self.cid, PolicyVersion.policy_id == found["policy"]["id"],
                    PolicyVersion.status == "ACTIVE", PolicyVersion.id != version["id"],
                    PolicyVersion.effective_from <= self.end,
                    or_(PolicyVersion.effective_to.is_(None), PolicyVersion.effective_to >= self.start)).first()
                if other is not None:
                    self.issue("POLICY_CHANGED_MID_PERIOD", "WARNING",
                               f"{code} has another version in force during the period; the version in force on "
                               f"{self.facts_date.isoformat()} is applied to the whole period.", key=code,
                               policy_type=code, applied_version_id=version["id"], other_version_id=other[0])
        return result

    # -- ledger and segments --------------------------------------------------------------------
    def ledger_days(self) -> List[dict]:
        grace = 0
        late = self.policies.get("LATE_PENALTY_POLICY")
        if late is not None:
            grace = int(_dec(late["fields"].get("early_exit_grace_minutes")) or 0)
        standard = _dec(self.settings.get("payroll.standard_hours_per_day"))
        # BUG-AS-31: the flag's consumer (JC-AS-02); a missing value keeps the default (true).
        use_shift = self.settings.get("payroll.standard_hours.use_shift_duration")
        days = time_inputs.ledger_inputs(self.db, self.cid, self.eid, self.start, self.end,
                                         lock_at=None if self.probe else self.lock_time,
                                         early_exit_grace_minutes=grace, standard_hours_per_day=standard,
                                         use_shift_duration=use_shift is not False)
        result = []
        suspensions: set = set()
        for day in days:
            item = snap.day_to_dict(day)
            found = self.placement_on(day.date)
            if self.primary_row is False:
                item["assignment_id"] = self.row.employee_assignment_id
                item["employed"] = bool(found is not None and self.employed_on(day.date))
            if item["employed"] and (found is None or not found.in_run):
                item["employed"] = False  # paid by another legal entity or pay group (EC-ORG-06, 3.5 §14)
                item["suspended_unpaid"] = False
                item["suspension_id"] = None
            item["segment"] = ""
            result.append(item)
            for leave in day.leaves:
                self.source("leave", leave.leave_id, digest=leave.content_hash or source_hash(snap.day_to_dict(day)))
            if day.holiday is not None:
                self.source("holiday", day.holiday.holiday_id, {"hash": day.holiday.content_hash,
                                                                "type": day.holiday.type.value})
            if day.attendance is not None and day.attendance.session_id is not None:
                self.source("attendance", day.attendance.session_id, digest=day.attendance.content_hash)
            for lop in day.manual_lop:
                self.source("lop_input", lop.source_id, digest=lop.content_hash)
            for duty in day.on_duty:
                self.source("on_duty", duty.source_id, digest=duty.content_hash)
            if day.suspension_id is not None:
                suspensions.add(day.suspension_id)
        if suspensions:
            for row in self.db.query(EmployeeSuspension).filter(EmployeeSuspension.company_id == self.cid,
                                                                EmployeeSuspension.id.in_(sorted(suspensions))):
                self.source("suspension", row.id, time_inputs.suspension_content(row))
        return result

    def compensation_pieces(self) -> List[dict]:
        try:
            pieces = compensation_service.segments(self.db, self.cid, self.eid, self.start, self.end)
        except AppError as error:
            self.issue("STRUCTURE_INVALID", "BLOCKING", f"The compensation cannot be frozen: {error.detail}",
                       error_code=error.code)
            return []
        allowed = {item.row.id for item in self.placements}
        return [p for p in pieces if p.get("compensation") is not None and p.get("assignment_id") in allowed]

    def build_segments(self, days: List[dict]) -> List[dict]:
        pieces = self.compensation_pieces()
        assignments = {p["compensation_assignment_id"]: p for p in pieces}
        rows = {r.id: r for r in self.db.query(CompensationAssignment).filter(
            CompensationAssignment.company_id == self.cid,
            CompensationAssignment.id.in_(sorted(assignments) or [0]))}
        converted: Dict[int, Optional[dict]] = {}
        for comp_id, piece in sorted(assignments.items()):
            converted[comp_id] = self.compensation_json(rows[comp_id], piece["compensation"])
        segments: List[dict] = []
        used: Dict[str, int] = defaultdict(int)
        current: Optional[dict] = None
        for item in days:
            if not item["employed"]:
                current = None
                continue
            day = date.fromisoformat(item["date"])
            placement = self.placement_on(day)
            piece = next((p for p in pieces if p["start"] <= day <= p["end"]), None)
            comp_id = piece["compensation_assignment_id"] if piece is not None else None
            base = f"A{placement.row.id}-C{comp_id if comp_id is not None else 'none'}"
            if current is not None and current["base"] == base and current["end"] + ONE_DAY == day:
                current["end"] = day
            else:
                used[base] += 1
                key = base if used[base] == 1 else f"{base}-{used[base]}"
                current = {"base": base, "key": key, "start": day, "end": day, "assignment_id": placement.row.id,
                           "compensation": converted.get(comp_id) if comp_id is not None else None}
                segments.append(current)
            item["segment"] = current["key"]
            if self.primary_row is not False:
                item["assignment_id"] = placement.row.id
        if not segments:
            self.issue("NO_PAID_DAYS", "BLOCKING", "The employee is not paid by this run on any date of the period "
                       "(another legal entity or pay group pays it, or employment does not overlap).")
            segments.append({"base": "none", "key": "NONE", "start": self.start, "end": self.end,
                             "assignment_id": self.row.employee_assignment_id, "compensation": None})
        return [{"key": s["key"], "start": s["start"].isoformat(), "end": s["end"].isoformat(),
                 "assignment_id": s["assignment_id"], "compensation": s["compensation"]} for s in segments]

    def compensation_json(self, row: CompensationAssignment, compensation: Mapping[str, Any]) -> dict:
        data = _plain(dict(compensation))
        if not data.get("ctc_employer_contributions") and data.get("structure_version_id") is not None:
            data["ctc_employer_contributions"] = self.ctc_contributions(data)
        self.source("compensation_assignment", row.id, {"compensation": data, "row_version": row.row_version,
                                                        "fx_rule": row.fx_rule, "status": row.status,
                                                        "effective_from": row.effective_from,
                                                        "effective_to": row.effective_to})
        if data.get("structure_version_id") is not None:
            self.source("structure_version", data["structure_version_id"], {"lines": data.get("lines")})
        if data.get("currency") == self.run.currency:
            return data
        return self.convert_compensation(row, data)

    def ctc_contributions(self, data: Mapping[str, Any]) -> dict:
        from app.payroll.services import structures

        version = self.db.query(SalaryTemplateVersion).filter(
            SalaryTemplateVersion.company_id == self.cid, SalaryTemplateVersion.id == data["structure_version_id"]).first()
        if version is None:
            return {}
        try:
            found = structures.ctc_employer_contributions(self.db, self.cid, data["structure_type"], data["currency"],
                                                          structures._resolved_lines(self.db, version),
                                                          self.entity.country_code)
        except AppError:
            return {}
        return {str(k): _plain(v) for k, v in dict(found or {}).items()}

    def convert_compensation(self, row: CompensationAssignment, data: dict) -> dict:
        """Contract currency -> pay currency (EC-CMP-07): FIXED_RATE rule, else the run's FX snapshot."""
        currency = data["currency"]
        rule = row.fx_rule or {}
        if rule.get("source") == "FIXED_RATE" and _dec(rule.get("rate")):
            rate, source = _dec(rule.get("rate")), "FIXED_RATE"
        else:
            self.fx.ensure([currency])
            rate, source = self.fx.rate(currency), "PERIOD_SNAPSHOT"
            if rate is not None:
                self.fx_used[currency] = rate
        if rate is None:
            self.issue("FX_MISSING", "BLOCKING", f"No usable {currency}->{self.run.currency} rate for "
                       f"{self.fx.conversion_date.isoformat()} ({self.fx.missing.get(currency, 'RATE_UNAVAILABLE')}); "
                       "the compensation is not converted (never 1:1).", key=currency, base=currency,
                       quote=self.run.currency, compensation_assignment_id=row.id)
            return data

        def times(value: Any) -> Any:
            amount = _dec(value)
            if amount is None:
                return value
            with localcontext(CALC_CONTEXT):
                return format(amount * rate, "f")

        out = dict(data)
        out["currency"] = self.run.currency
        out["pay_basis_amount"] = times(data.get("pay_basis_amount"))
        out["ctc_employer_contributions"] = {k: times(v) for k, v in (data.get("ctc_employer_contributions") or {}).items()}
        lines = []
        for line in data.get("lines") or []:
            line = dict(line)
            for name in ("amount", "annual_amount"):
                if line.get(name) is not None:
                    line[name] = times(line[name])
            params = dict(line.get("params") or {})
            if line.get("method") == "RATE_X_UNITS" and params.get("rate") is not None:
                params["rate"] = times(params["rate"])
            line["params"] = params
            lines.append(line)
        out["lines"] = lines
        self.conversions.append({"what": "compensation", "id": row.id, "from": currency, "to": self.run.currency,
                                 "rate": format(rate, "f"), "source": source, "note": FX_ROUNDING_NOTE})
        return out

    # -- employee and period --------------------------------------------------------------------
    def employee_json(self, segments: List[dict]) -> dict:
        e = self.employee
        facts = compensation_service.employee_facts(self.db, self.cid, self.eid, self.facts_date)
        placement = self.facts_placement.row if self.facts_placement is not None else None
        standard = None
        if placement is not None:
            from app.services import shift_resolution

            # The shift that resolves on the facts date (JC-AS-02; zero diff for placement shifts).
            standard = shift_resolution.scheduled_hours(
                shift_resolution.shift_for(self.db, self.cid, self.eid, self.facts_date))
        subdivision = facts.get("emp.subdivision")
        if placement is not None and placement.legal_entity_id == self.run.legal_entity_id:
            branch = self.db.query(Branch).filter(Branch.company_id == self.cid, Branch.id == placement.branch_id).first()
            subdivision = branch.subdivision_code if branch is not None else subdivision
        last = next((s for s in reversed(segments) if s.get("compensation")), None)
        structure = (last or {}).get("compensation") or {}
        protected = {k: v for k, v in (("gender", e.gender), ("date_of_birth", _iso(e.date_of_birth))) if v}
        fte = facts.get("emp.fte")
        if placement is not None and placement.fte is not None:
            fte = format(Decimal(placement.fte), "f")
        return {
            "employee_id": e.id, "code": e.employee_code, "hire_date": e.hire_date.isoformat(),
            "exit_date": _iso(e.termination_date), "country": self.entity.country_code,
            "worker_class": facts.get("emp.worker_class") or "EMPLOYEE",
            "employment_type": facts.get("emp.employment_type") or "PERMANENT",
            "grade": facts.get("emp.grade"), "designation": facts.get("emp.designation"),
            "subdivision": subdivision or None, "fte": fte or "1",
            "standard_hours_per_day": _plain(standard), "salaried": structure.get("structure_type")
            not in NON_SALARIED_STRUCTURES, "attributes": {}, "protected": protected,
        }

    def period_json(self) -> dict:
        p = self.period
        per_year = FIXED_PERIODS.get(p.frequency)
        if per_year is None:
            count = self.db.query(func.count(PayPeriod.id)).filter(
                PayPeriod.company_id == self.cid, PayPeriod.pay_group_id == p.pay_group_id,
                PayPeriod.tax_year == p.tax_year, PayPeriod.frequency == p.frequency).scalar() or 0
            per_year = max(int(count), MIN_PERIODS.get(p.frequency, 1))
        tax_year_start = None
        calendar_row = self.db.query(PayCalendar).filter(PayCalendar.company_id == self.cid,
                                                         PayCalendar.id == p.pay_calendar_id).first()
        start_rule = (calendar_row.rules or {}).get("tax_year_start") if calendar_row is not None else None
        if start_rule:
            try:
                tax_year_start = paycal.tax_year_from_label(p.tax_year, start_rule)[0]
            except (ValueError, paycal.CalendarError):
                tax_year_start = None
        return {"period_id": p.id, "start": p.period_start.isoformat(), "end": p.period_end.isoformat(),
                "pay_date": p.pay_date.isoformat(), "frequency": p.frequency, "periods_per_year": per_year,
                "number_in_tax_year": p.number_in_tax_year, "is_last_in_tax_year": bool(p.is_last_in_tax_year),
                "tax_year_start": _iso(tax_year_start)}

    # -- catalogue ------------------------------------------------------------------------------
    def components_json(self) -> List[dict]:
        result = []
        for code, entry in sorted(component_service.catalogue(self.db, self.cid, self.end).items()):
            data = _plain(component_service.component_def(entry))
            result.append(data)
            self.source("component_version", entry.version.id, data)
        return result

    def tables_json(self) -> List[dict]:
        tables = (self.db.query(PayrollTable)
                  .filter(PayrollTable.company_id == self.cid, PayrollTable.status == "ACTIVE",
                          PayrollTable.effective_from <= self.end,
                          or_(PayrollTable.effective_to.is_(None), PayrollTable.effective_to >= self.end))
                  .order_by(PayrollTable.code, PayrollTable.id).all())
        if not tables:
            return []
        rows: Dict[int, List[PayrollTableRow]] = defaultdict(list)
        for r in (self.db.query(PayrollTableRow)
                  .filter(PayrollTableRow.company_id == self.cid, PayrollTableRow.table_id.in_([t.id for t in tables]))
                  .order_by(PayrollTableRow.table_id, PayrollTableRow.sort_order, PayrollTableRow.id)):
            rows[r.table_id].append(r)
        result, seen = [], set()
        for table in tables:
            if table.code in seen:
                continue
            seen.add(table.code)
            lookup = table.kind == "LOOKUP"
            data = {"code": table.code, "kind": table.kind,
                    "key_currency": None if lookup else table.currency,
                    "value_currency": table.currency if table.kind != "SLAB" else None,
                    "rows": [{k: v for k, v in (("lower", _plain(_dec(r.lower))), ("upper", _plain(_dec(r.upper))),
                                                ("key", r.key), ("value", _plain(_dec(r.value))),
                                                ("rate", _plain(_dec(r.rate)))) if v is not None}
                             for r in rows[table.id]]}
            result.append(data)
            self.source("payroll_table", table.id, data)
        return result

    # -- time-based inputs ----------------------------------------------------------------------
    def attendance_window(self) -> Tuple[date, date]:
        start, end = self.period.attendance_start, self.period.attendance_end
        return (start, end) if end >= start else (self.start, self.end)

    def check_attendance_cycle(self) -> None:
        if (self.period.attendance_start, self.period.attendance_end) == (self.start, self.end):
            return
        if any(self.switch_any(key) for key in TIME_SWITCHES):
            self.issue("ATTENDANCE_CYCLE_UNSUPPORTED", "BLOCKING",
                       f"The pay group's attendance cycle ({self.period.attendance_start.isoformat()} to "
                       f"{self.period.attendance_end.isoformat()}) differs from the pay period, and the calculation "
                       "ledger covers the pay period's dates only. Use the SAME_AS_PERIOD attendance cycle, or switch "
                       "attendance and leave pay off for this pay group.", attendance_start=self.period.attendance_start,
                       attendance_end=self.period.attendance_end)

    def overtime_json(self) -> List[dict]:
        from app.payroll.services import overtime

        start, end = self.attendance_window()
        result = []
        for item in overtime.approved_overtime(self.db, self.cid, self.eid, start, end):
            consumed = item.get("consumed_by_run_employee_id")
            if consumed is not None and consumed != self.row.id:
                continue  # paid by another run already (EC-OT-07)
            result.append({"entry_id": item["entry_id"], "date": item["date"], "minutes": item["minutes"],
                           "status": item["status"], "comp_off": bool(item.get("comp_off")), "start": item.get("start"),
                           "end": item.get("end"), "origin": item.get("origin") or "REQUEST"})
            self.source("overtime", item["id"], item)
        return result

    def non_working_day_work(self, days: Sequence[dict]) -> List[dict]:
        if not any(self.switch_any(k) for k in NON_WORKING_DAY_SWITCHES):
            return []
        result = []
        for item in days:
            attendance = item.get("attendance") or {}
            seconds = attendance.get("worked_seconds")
            if not item["employed"] or not seconds:
                continue
            holiday = item.get("holiday")
            if holiday is not None and holiday.get("type") != "OPTIONAL_NOT_AVAILED":
                day_type = "HOLIDAY"
            elif item.get("weekly_off"):
                day_type = "WEEKLY_OFF"
            else:
                continue
            # Work on a non-working day counts as approved when an approved on-duty request covers it.
            result.append({"date": item["date"], "day_type": day_type, "worked_minutes": int(seconds) // 60,
                           "approved": bool(item.get("on_duty")), "choice": None,
                           "ref": f"attendance:{attendance.get('session_id')}"})
        return result

    def shifts_json(self, days: Sequence[dict]) -> List[dict]:
        if not self.switch_any(SHIFT_SWITCH):
            return []
        from app.services import attendance_service

        weekly_off = {item["date"]: bool(item.get("weekly_off")) for item in days if item["employed"]}
        records = (self.db.query(Attendance)
                   .filter(Attendance.company_id == self.cid, Attendance.employee_id == self.eid,
                           Attendance.date >= self.start, Attendance.date <= self.end,
                           Attendance.check_out_time.isnot(None))
                   .order_by(Attendance.date, Attendance.check_in_time).all())
        records = [r for r in records if r.date.isoformat() in weekly_off]
        breaks = attendance_service.breaks_by_session(self.db, [r.id for r in records]) if records else {}
        result = []
        for record in records:
            unpaid = [[b.started_at.isoformat(), b.ended_at.isoformat()] for b in breaks.get(record.id, [])
                      if b.ended_at is not None and not b.is_paid]
            result.append({"date": record.date.isoformat(), "start": record.check_in_time.isoformat(),
                           "end": record.check_out_time.isoformat(), "unpaid_breaks": unpaid,
                           "weekend": weekly_off[record.date.isoformat()], "ref": f"attendance:{record.id}"})
        return result

    def tasks_json(self) -> Optional[dict]:
        from app.payroll.services import tasks

        policy = self.policies.get("TASK_PAY_POLICY") or {}
        attribution = (policy.get("fields") or {}).get("attribution") or "APPROVAL_DATE"
        items = tasks.approved_tasks(self.db, self.cid, self.eid, self.start, self.end, attribution)
        if not items:
            return None
        records = []
        for item in items:
            records.append({k: item.get(k) for k in ("task_id", "task_type", "status", "quantity", "assignees",
                                                     "accepted_quantity", "rate", "rate_currency", "approved_on",
                                                     "completed_on", "evidence_count", "external_ref", "unit")})
            records[-1]["paid_in_run_id"] = item.get("paid_in_run_id")
            self.source("task", item["id"], item)
        types = sorted({item["task_type"] for item in items})
        card = []
        for row in tasks.rate_card_rows(self.db, self.cid, self.start, self.end, types):
            card.append({k: row.get(k) for k in ("task_type", "rate", "currency", "effective_from", "effective_to",
                                                 "grade", "skill", "location")})
            self.source("rate_card", row["id"], row)
        currencies = {r.get("rate_currency") for r in records} | {r.get("currency") for r in card}
        self.fx.ensure(currencies)
        for currency in sorted(c for c in currencies if c and c != self.run.currency):
            rate = self.fx.rate(currency)
            if rate is not None:
                self.fx_used[currency] = rate
        return {"records": records, "rate_card": card, "carried_in": "0"}

    # -- variable inputs ------------------------------------------------------------------------
    def foreign_amount(self, what: str, ident: Any, amount: Optional[Decimal], currency: Optional[str],
                       rule: Optional[Mapping[str, Any]] = None) -> Tuple[Optional[Decimal], Optional[str]]:
        """``amount`` converted into the pay currency (FIXED_RATE rule or the run's FX snapshot)."""
        if currency is None or currency == self.run.currency or amount is None:
            return amount, currency
        rule = rule or {}
        if rule.get("source") == "FIXED_RATE" and _dec(rule.get("rate")):
            rate, source = _dec(rule.get("rate")), "FIXED_RATE"
        else:
            self.fx.ensure([currency])
            rate, source = self.fx.rate(currency), "PERIOD_SNAPSHOT"
            if rate is not None:
                self.fx_used[currency] = rate
        if rate is None:
            self.issue("FX_MISSING", "BLOCKING", f"No usable {currency}->{self.run.currency} rate for {what} {ident} "
                       "(never converted 1:1).", key=f"{what}:{ident}", base=currency, quote=self.run.currency)
            return amount, currency
        with localcontext(CALC_CONTEXT):
            converted = amount * rate
        self.conversions.append({"what": what, "id": ident, "from": currency, "to": self.run.currency,
                                 "rate": format(rate, "f"), "source": source, "note": FX_ROUNDING_NOTE})
        return converted, self.run.currency

    def incentive_json(self) -> Optional[dict]:
        """The payout window this run pays and the employee's target, or None (switch OFF, no plan, or a period
        that ends no window). Sets ``self.incentive_window``: None means the period's measures are frozen as they
        are, a date pair the window whose measures are paid, and ``False`` that measures wait for a later run."""
        self.incentive_window: Any = None
        policy = self.policies.get(incentives.POLICY_TYPE)
        if self.settings.get(incentives.SWITCH) is not True or policy is None:
            return None
        fields = policy.get("fields") or {}
        tax_year_start = self.period_json().get("tax_year_start")
        try:
            window = incentives.payout_window(str(fields.get("payout_frequency") or "MONTHLY"), self.start, self.end,
                                              date.fromisoformat(tax_year_start) if tax_year_start else None)
        except incentives.IncentiveError as error:
            self.issue("CONFIG_INVALID", "BLOCKING", f"The incentive plan cannot be applied: {error.message}",
                       key="INCENTIVE_PLAN", policy_type=incentives.POLICY_TYPE)
            return None
        if window is None:
            self.incentive_window = False
            return None
        self.incentive_window = window
        placement = self.facts_placement.row if self.facts_placement is not None else None
        grade_id = placement.grade_id if placement is not None else None
        target = incentives.resolve_target(fields.get("targets") or [], employee_id=self.eid, grade_id=grade_id)
        return {"window_start": window[0].isoformat(), "window_end": window[1].isoformat(),
                "target": _plain(target.value), "target_source": target.source, "grade_id": grade_id}

    def wage_inputs_json(self) -> List[dict]:
        from app.payroll.services import wage_inputs

        result = []
        window = getattr(self, "incentive_window", None)
        lower = min(self.start, window[0]) if window else self.start
        for item in wage_inputs.approved_inputs(self.db, self.cid, self.eid, lower, self.end,
                                                run_employee_id=self.row.id):
            target_start = date.fromisoformat(item["target_period"]["start"])
            target_end = date.fromisoformat(item["target_period"]["end"])
            if item.get("source") == incentives.MEASURE_SOURCE and window is not None:
                # An ON plan pays the measures of its window in the run that ends it; others wait unreserved.
                if window is False or not incentives.measure_in_window(target_start, target_end, window[0],
                                                                         self.end):
                    continue
            elif target_start < self.start:
                continue
            amount, units, rate = _dec(item.get("amount")), _dec(item.get("units")), _dec(item.get("rate"))
            if amount is None and units is not None and rate is not None:
                with localcontext(CALC_CONTEXT):
                    amount = units * rate
            amount, currency = self.foreign_amount("wage input", item["id"], amount, item.get("currency"),
                                                   item.get("fx_rule"))
            result.append({"id": item["id"], "component": item["component_code"], "amount": _plain(amount),
                           "units": _plain(units), "currency": currency, "source": item.get("source") or "MANUAL",
                           "external_ref": item.get("external_ref"), "reason": item.get("reason"),
                           "content_hash": item.get("version_hash")})
            self.source("wage_input", item["id"], digest=item.get("version_hash") or source_hash(item))
        return result

    def claims_json(self) -> List[dict]:
        from app.payroll.services import claims, runs

        items = list(claims.claims_for_payroll(self.db, self.cid, self.eid, self.start, self.end))
        scheduled = (self.db.query(ReimbursementClaim)
                     .filter(ReimbursementClaim.company_id == self.cid, ReimbursementClaim.employee_id == self.eid,
                             ReimbursementClaim.status == sm.ClaimStatus.SCHEDULED,
                             ReimbursementClaim.scheduled_ref == runs.claims_ref(self.row.id))
                     .order_by(ReimbursementClaim.expense_date, ReimbursementClaim.id).all())
        seen = {i["id"] for i in items}
        items.extend(claims._payable(r) for r in scheduled if r.id not in seen)
        result = []
        for item in sorted(items, key=lambda i: (i.get("expense_date") or "", i["id"])):
            amount, currency = self.foreign_amount("claim", item["id"], _dec(item["amount"]), item.get("currency"))
            result.append({"id": item["id"], "claim_type": item["claim_type"], "amount": _plain(amount),
                           "currency": currency, "taxability": item.get("taxability")})
            self.source("claim", item["id"], digest=item.get("version_hash") or source_hash(item))
        return result

    def loans_json(self) -> Tuple[List[dict], List[dict]]:
        from app.payroll.services import loans

        final = self.run.run_type == sm.RunType.FINAL_SETTLEMENT.value
        found = {"LOAN": [], "ADVANCE": []}
        for item in loans.loan_instalments_due(self.db, self.cid, self.eid, self.start, self.end):
            nxt = item.get("next_instalment") or {}
            amount = item.get("balance") if final and item.get("recover_balance_on_exit") else nxt.get("amount")
            self.source("loan", item["loan_id"], digest=item.get("version_hash") or source_hash(item))
            if amount is None or (_dec(amount) or ZERO) <= 0:
                continue
            entry = {"id": item["loan_id"], "loan_id": item["loan_id"], "kind": item["kind"], "status": item["status"],
                     "component": item["component"], "amount": amount, "currency": item["currency"],
                     "instalment_number": nxt.get("number"), "balance": item.get("balance"),
                     "schedule_version": item.get("schedule_version"),
                     "recover_balance_on_exit": bool(item.get("recover_balance_on_exit"))}
            found.setdefault(item["kind"], []).append(entry)
        return found.get("LOAN", []), found.get("ADVANCE", [])

    def arrears_json(self) -> List[dict]:
        try:
            from app.payroll.services import retro
        except ImportError:
            return []
        hook = getattr(retro, "arrears_for_snapshot", None)
        if hook is None:
            return []
        items = [dict(i) for i in hook(self.db, self.run, self.row)]
        for item in items:
            self.source("arrears_instalment", item["id"], item)
        return items

    # -- history, YTD, pack ---------------------------------------------------------------------
    def finalized_rows(self, *, regular_only: bool, before_start: bool, limit: int):
        query = (self.db.query(PayrollRunEmployee, PayrollRun, PayPeriod)
                 .join(PayrollRun, and_(PayrollRun.company_id == PayrollRunEmployee.company_id,
                                        PayrollRun.id == PayrollRunEmployee.run_id))
                 .join(PayPeriod, and_(PayPeriod.company_id == PayrollRun.company_id,
                                       PayPeriod.id == PayrollRun.pay_period_id))
                 .filter(PayrollRunEmployee.company_id == self.cid, PayrollRunEmployee.employee_id == self.eid,
                         PayrollRunEmployee.status == "CALCULATED", PayrollRun.id != self.run.id,
                         PayrollRun.legal_entity_id == self.run.legal_entity_id,
                         PayrollRun.status.in_(sm.RUN_FINALIZED_STATES),
                         PayrollRun.run_type != sm.RunType.REVERSAL.value))
        if regular_only:
            query = query.filter(PayrollRun.run_type == sm.RunType.REGULAR.value)
        if before_start:
            query = query.filter(PayPeriod.period_end < self.start)
        else:
            query = query.filter(PayPeriod.period_end <= self.end)
        return query.order_by(PayPeriod.period_end.desc(), PayrollRun.finalized_at.desc(),
                              PayrollRun.id.desc()).limit(limit).all()

    def previous_json(self) -> dict:
        rows = [r for r in self.finalized_rows(regular_only=True, before_start=True, limit=HISTORY_PERIODS)
                if r[0].currency == self.run.currency]
        if not rows:
            return {}
        sums: Dict[int, Dict[str, Decimal]] = defaultdict(dict)
        for run_employee_id, code, total in (self.db.query(PayrollLine.run_employee_id, PayrollLine.component_code,
                                                           func.sum(PayrollLine.amount))
                                             .filter(PayrollLine.company_id == self.cid,
                                                     PayrollLine.run_employee_id.in_([r[0].id for r in rows]),
                                                     PayrollLine.currency == self.run.currency)
                                             .group_by(PayrollLine.run_employee_id, PayrollLine.component_code)):
            sums[run_employee_id][code] = Decimal(total)
        latest, latest_run, latest_period = rows[0]
        history: Dict[str, List[str]] = defaultdict(list)
        for code in sorted({c for r in rows for c in sums[r[0].id]}):
            history[code] = [format(sums[r[0].id].get(code, ZERO), "f") for r in rows]
        accounts = []
        if latest.snapshot_id is not None:
            content = self.db.query(PayrollInputSnapshot.content).filter(
                PayrollInputSnapshot.company_id == self.cid, PayrollInputSnapshot.id == latest.snapshot_id).scalar()
            accounts = sorted({a.get("account_id") for a in (content or {}).get("bank_accounts") or []
                               if a.get("account_id") is not None}, key=str)
        data = {"period_ref": f"{latest_period.period_start.isoformat()}..{latest_period.period_end.isoformat()}",
                "currency": latest.currency, "gross": _plain(_dec(latest.gross)), "net": _plain(_dec(latest.net)),
                "components": {k: format(v, "f") for k, v in sorted(sums[latest.id].items())},
                "history": dict(history), "payment_currency": latest.currency, "bank_account_ids": accounts}
        self.source("previous_run_employee", latest.id, {**data, "calc_hash": latest.calc_hash})
        return data

    def ytd_json(self) -> dict:
        rows = (self.db.query(YtdBalance)
                .filter(YtdBalance.company_id == self.cid, YtdBalance.employee_id == self.eid,
                        YtdBalance.legal_entity_id == self.run.legal_entity_id,
                        YtdBalance.tax_year == self.period.tax_year, YtdBalance.currency == self.run.currency)
                .order_by(YtdBalance.bucket, YtdBalance.id).all())
        result = {}
        for row in rows:
            result[row.bucket] = format(Decimal(row.amount), "f")
            self.source("ytd_balance", row.id, {"bucket": row.bucket, "amount": row.amount, "currency": row.currency,
                                                "tax_year": row.tax_year, "last_run_id": row.last_run_id})
        return result

    def pack_json(self) -> Optional[dict]:
        data = pack_reference(self.run, self.period, self.entity)
        if data is not None:
            self.source("pack", data["code"], data)
        return data

    def tax_profile_json(self) -> Optional[dict]:
        """The employee's approved tax declarations in force on the period end, read-only (tax_profiles service):
        None when the pack declares no fields or nothing is approved, so such a run is exactly as before."""
        from app.payroll.services import tax_profiles

        content, version_id, issues = tax_profiles.frozen_profile(
            self.db, self.cid, self.eid, pack_reference(self.run, self.period, self.entity), self.end)
        for code, severity, message, details in issues:
            self.issue(code, severity, message, **details)
        if content is not None:
            self.source("tax_profile", version_id, content)
        return content

    def pack_state_json(self) -> dict:
        """F-15: the previous finalized run's snapshot state plus the determinations that run made."""
        rows = self.finalized_rows(regular_only=False, before_start=False, limit=1)
        if not rows:
            return {}
        previous = rows[0][0]
        content = None
        if previous.snapshot_id is not None:
            content = self.db.query(PayrollInputSnapshot.content).filter(
                PayrollInputSnapshot.company_id == self.cid, PayrollInputSnapshot.id == previous.snapshot_id).scalar()
        state = dict((content or {}).get("pack_state") or {})
        made = self.determinations(previous, content)
        state.update(made)
        if state:
            self.source("pack_state", previous.id, state)
        return _plain(state)

    def determinations(self, previous: PayrollRunEmployee, content: Optional[Mapping[str, Any]]) -> dict:
        stored = getattr(previous, "stage_outputs", None)
        if isinstance(stored, Mapping):
            statutory = stored.get("statutory") or {}
            return dict(statutory.get("pack_state") or {})
        for flag in previous.flags or ():
            if isinstance(flag, Mapping) and flag.get("code") == "PACK_STATE_DETERMINED":
                return dict((flag.get("details") or {}).get("pack_state") or {})
        if not content or not content.get("pack"):
            return {}
        from app.payroll import engine

        try:
            matches, result = engine.replay(content, previous.calc_hash or "")
        except Exception:  # noqa: BLE001 - an unreadable earlier snapshot never blocks this one silently
            matches, result = False, None
        if result is None or not matches:
            self.issue("PACK_STATE_UNAVAILABLE", "WARNING", "The statutory determinations of the previous finalized "
                       "period could not be reproduced; only the state it carried is used.",
                       previous_run_employee_id=previous.id)
            return {}
        return dict(((result.stage_outputs or {}).get("statutory") or {}).get("pack_state") or {})

    def attestation_json(self) -> Optional[dict]:
        from app.payroll.services import attestations

        row = attestations.active_attestation(self.db, self.entity.id, (self.start, self.end))
        if row is None:
            return None
        data = {"id": row.id, "status": row.status, "schemes": _plain(row.schemes or []),
                "effective_from": row.effective_from.isoformat(), "effective_to": _iso(row.effective_to),
                "attested_by_user_id": row.attested_by_user_id, "approved_by_user_id": row.approved_by_user_id}
        self.source("attestation", row.id, data)
        return data

    def bank_accounts_json(self) -> List[dict]:
        """Payments milestone hook: the accounts serving on the pay date with their split rules."""
        try:
            from app.payroll.services import bank_accounts
        except ImportError:
            return []
        builder = getattr(bank_accounts, "engine_bank_accounts", None)
        if builder is not None:
            items = builder(self.db, self.cid, self.eid, self.period.pay_date)
        else:
            items = []
            for item in bank_accounts.usable_accounts(self.db, self.cid, self.eid, self.period.pay_date):
                if "COOLING_OFF" in (item.get("hold_reasons") or ()):
                    continue
                allocation = item.get("allocation") or {}
                items.append({"account_id": item["bank_account_id"], "type": allocation.get("type", "PRIMARY"),
                              "value": allocation.get("value"), "order": allocation.get("order", 0),
                              "currency": item.get("currency"), "verified": bool(item.get("verified")),
                              "first_payment": bool(item.get("first_payment"))})
        result = []
        for item in items:
            entry = {k: _plain(item.get(k)) for k in ("account_id", "type", "value", "order", "currency", "verified",
                                                      "first_payment")}
            entry["order"] = int(entry["order"] or 0)
            entry["type"] = entry["type"] or "PRIMARY"
            entry["verified"] = entry["verified"] is not False
            entry["first_payment"] = entry["first_payment"] is True
            result.append(entry)
            self.source("bank_account", entry["account_id"], entry)
        return result

    def overrides_json(self, days: Sequence[dict]) -> dict:
        dates = {item["date"] for item in days}
        result = {}
        for row in (self.db.query(PayrollException)
                    .filter(PayrollException.company_id == self.cid, PayrollException.run_id == self.run.id,
                            PayrollException.run_employee_id == self.row.id,
                            PayrollException.status.in_(("FIXED", "WAIVED")))
                    .order_by(PayrollException.resolved_at, PayrollException.id)):
            override = (row.details or {}).get(OVERRIDE_KEY)
            if not isinstance(override, Mapping):
                continue
            day, action = override.get("date"), override.get("action")
            if day not in dates or action not in OVERRIDE_ACTIONS:
                continue
            result[day] = {"action": action, "source_id": f"exception:{row.id}"}
            self.source("exception", row.id, override)
        return result

    # -- off-cycle selection --------------------------------------------------------------------
    def apply_selection(self, data: Dict[str, Any]) -> None:
        """A BONUS / CORRECTION off-cycle run pays only its selected components (``off_cycle.selected_components``,
        EC-COR-06): structure lines and inputs of other components are left out of the snapshot, so they are
        neither paid nor reserved by this run; the statutory stages still run on what remains.

        The source hashes of the inputs left out are dropped as well (``drop_unselected_sources``): change
        detection then ignores their changes instead of marking the run stale for an input it never pays."""
        from app.payroll.services import off_cycle

        selected = off_cycle.selected_components(self.run)
        if selected is None:
            return
        chosen = frozenset(selected)
        self.source("run_selection", self.run.id, sorted(chosen))
        before = {name: list(data.get(name) or ()) for name in SELECTION_SOURCE_KEYS}
        tasks_before = data.get("tasks")
        for segment in data["segments"]:
            compensation = segment.get("compensation")
            if compensation is not None:
                compensation["lines"] = [line for line in compensation.get("lines") or ()
                                         if line.get("component") in chosen]
        for name in COMPONENT_INPUTS:
            data[name] = [item for item in data.get(name) or () if item.get("component") in chosen]
        if REIMBURSEMENT_COMPONENT not in chosen:
            data["reimbursements"] = []
        for name, produces in TIME_PAY_INPUTS.items():
            if produces & chosen:
                continue
            if name == "tasks":
                data.pop("tasks", None)
            else:
                data[name] = []
        self.drop_unselected_sources(data, before, tasks_before)

    def drop_unselected_sources(self, data: Mapping[str, Any], before: Mapping[str, Sequence[dict]],
                                tasks_before: Optional[Mapping[str, Any]]) -> None:
        """Drop the source hashes of the input items ``apply_selection`` left out. Fail closed: a key is dropped
        only when it is an input item's own key, no kept item of any list has the same key, and it was recorded
        (a key the builder did not record is never invented)."""
        dropped, kept = set(), set()
        for name, key_of in SELECTION_SOURCE_KEYS.items():
            remaining = data.get(name) or ()
            dropped.update(key_of(item) for item in before.get(name) or () if item not in remaining)
            kept.update(key_of(item) for item in remaining)
        if tasks_before is not None and data.get("tasks") is None:
            dropped.update(str(r.get("task_id")) for r in tasks_before.get("records") or ())
            dropped.update(k for k in self.hashes if k.startswith("rate_card:"))   # recorded for tasks only
        for key in sorted(dropped - kept):
            self.hashes.pop(key, None)

    def check_paid_in_period(self) -> None:
        """An OFF_CYCLE run without a component selection pays the whole period (a missed joiner, 3.5 §13). For
        an employee whom a regular run of the same period already pays (live or finalized, and not reversed),
        that is the salary twice: a BLOCKING finding, waivable with a reason for a deliberate second payment.
        Bonus, correction and payment-only off-cycle runs select what they pay and are not concerned."""
        from app.payroll.services import off_cycle

        if self.run.run_type != sm.RunType.OFF_CYCLE.value or off_cycle.selected_components(self.run) is not None:
            return
        reversed_runs = self.db.query(PayrollRun.reverses_run_id).filter(
            PayrollRun.company_id == self.cid, PayrollRun.run_type == sm.RunType.REVERSAL.value,
            PayrollRun.status.in_(sm.RUN_FINALIZED_STATES), PayrollRun.reverses_run_id.isnot(None))
        paying = (self.db.query(PayrollRun.id, PayrollRun.number)
                  .join(PayrollRunEmployee, and_(PayrollRunEmployee.company_id == PayrollRun.company_id,
                                                 PayrollRunEmployee.run_id == PayrollRun.id))
                  .filter(PayrollRun.company_id == self.cid, PayrollRun.id != self.run.id,
                          PayrollRun.pay_period_id == self.run.pay_period_id,
                          PayrollRun.run_type == sm.RunType.REGULAR.value,
                          PayrollRun.status != sm.RunStatus.CANCELLED.value, PayrollRun.id.notin_(reversed_runs),
                          PayrollRunEmployee.employee_id == self.eid, PayrollRunEmployee.status != "EXCLUDED")
                  .order_by(PayrollRun.id).all())
        if paying:
            numbers = [number for _, number in paying]
            self.issue("ALREADY_PAID_IN_PERIOD", "BLOCKING", f"The regular run {', '.join(numbers)} already pays "
                       "this employee for the period: this off-cycle run would pay the salary a second time. Use a "
                       "bonus or correction off-cycle run for the components to pay, or waive with a reason.",
                       waivable=True, run_ids=[run_id for run_id, _ in paying], runs=numbers)

    # -- assemble -------------------------------------------------------------------------------
    def build(self) -> SnapshotBuild:
        self.load_placements()
        paid = self.paid_dates()
        self.facts_date = paid[-1] if paid else min(self.end, self.employee.termination_date or self.end)
        self.facts_placement = self.placement_on(self.facts_date) or next(
            (p for p in reversed(self.placements) if p.in_run), None)
        self.settings, self.switch_ranges = self.resolve_settings()
        self.policies = self.resolve_policies()
        self.check_attendance_cycle()
        self.check_paid_in_period()
        days = self.ledger_days()
        segments = self.build_segments(days)
        self.source("day_ledger", self.row.id, days)
        loans, advances = self.loans_json()
        data: Dict[str, Any] = {
            "schema_version": snap.SCHEMA_VERSION,
            "run": {"run_id": self.run.id, "run_employee_id": self.row.id, "run_type": self.run.run_type,
                    "environment": self.run.environment},
            "employee": self.employee_json(segments),
            "period": self.period_json(),
            "currency": self.run.currency,
            "settings": self.settings,
            "switch_ranges": self.switch_ranges,
            "policies": self.policies,
            "components": self.components_json(),
            "segments": segments,
            "days": days,
            "ledger_overrides": self.overrides_json(days),
            "overtime_entries": self.overtime_json(),
            "non_working_day_work": self.non_working_day_work(days),
            "shifts": self.shifts_json(days),
            "incentive": self.incentive_json(),
            "wage_inputs": self.wage_inputs_json(),
            "arrears": self.arrears_json(),
            "reimbursements": self.claims_json(),
            "loans": loans,
            "advances": advances,
            "court_orders": [],
            "previous": self.previous_json(),
            "ytd": self.ytd_json(),
            "pack": self.pack_json(),
            "tax_profile": self.tax_profile_json(),
            "attestation": self.attestation_json(),
            "bank_accounts": self.bank_accounts_json(),
            "tables": self.tables_json(),
        }
        tasks = self.tasks_json()
        if tasks is not None:
            data["tasks"] = tasks
        if data["incentive"] is None:
            data.pop("incentive")
        data["pack_state"] = self.pack_state_json()
        self.apply_selection(data)
        if not self.probe:
            self.fx.freeze()
        if self.fx_used:
            rates = {c: format(r, "f") for c, r in sorted(self.fx_used.items())}
            data["fx"] = {"snapshot_id": self.fx.snapshot_id, "rates": rates,
                          "conversion_date": self.fx.conversion_date.isoformat(), "conversions": self.conversions}
            self.source("fx", self.fx.snapshot_id or "probe", rates)
        elif self.conversions:
            data["fx"] = {"conversions": self.conversions}
        try:
            parsed = snap.Snapshot.from_dict(data)
        except snap.SnapshotError as error:
            raise SnapshotBuildError(f"The frozen inputs do not form a valid snapshot: {error}",
                                     {"path": error.path, "code": error.code}) from None
        return SnapshotBuild(parsed.to_dict(), parsed.content_hash, dict(sorted(self.hashes.items())), self.issues)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def pack_reference(run: PayrollRun, period: PayPeriod, entity: LegalEntity) -> Optional[dict]:
    """The country pack frozen for the run's entity and period (``packs.pack_snapshot``): the latest version
    that may produce lines in the run's environment, else the latest one (the engine then reports
    STATUTORY_NOT_VERIFIED); None when the country has no pack. Its ``fingerprint`` detects a pack that
    changed after the inputs were locked (EC-STA-03)."""
    found = packs.packs_for_country(entity.country_code, on=period.period_end, environment=run.environment)
    if not found:
        return None
    usable = [p for p in found if packs.usage(p.meta, environment=run.environment,
                                              period=(period.period_start, period.period_end)).produces_lines]
    return _plain(packs.pack_snapshot((usable or list(found))[-1]))


def build(db: Session, run: PayrollRun, run_employee: PayrollRunEmployee, *, lock_time: Optional[datetime] = None,
          fx: Optional[FxBook] = None, probe: bool = False) -> SnapshotBuild:
    """Freeze one run employee's inputs (no rows written except the run's FX snapshot and, at lock time,
    attendance sessions expired as missing punch-outs). ``probe`` rebuilds without any write (change
    detection)."""
    return _Builder(db, run, run_employee, lock_time=lock_time or clock.utcnow(), fx=fx, probe=probe).build()


def build_snapshot(db: Session, run: PayrollRun, run_employee: PayrollRunEmployee, *, lock_time: datetime) -> dict:
    """The engine snapshot JSON of one run employee (``Snapshot.from_dict`` accepts it unchanged)."""
    return build(db, run, run_employee, lock_time=lock_time).content


def latest_snapshot(db: Session, run_employee: PayrollRunEmployee) -> Optional[PayrollInputSnapshot]:
    query = db.query(PayrollInputSnapshot).filter(PayrollInputSnapshot.company_id == run_employee.company_id,
                                                  PayrollInputSnapshot.run_employee_id == run_employee.id)
    if run_employee.snapshot_id is not None:
        found = query.filter(PayrollInputSnapshot.id == run_employee.snapshot_id).first()
        if found is not None:
            return found
    return query.order_by(PayrollInputSnapshot.version.desc()).first()


def _store_issues(db: Session, run: PayrollRun, row: PayrollRunEmployee, issues: Sequence[SnapshotIssue]) -> None:
    """SYSTEM exceptions of the run employee: a re-lock updates, closes what is gone, never reopens a
    waived or deferred one."""
    now = clock.utcnow()
    prefix = f"{ISSUE_PREFIX}:%"
    current = {e.dedupe_key: e for e in db.query(PayrollException).filter(
        PayrollException.company_id == run.company_id, PayrollException.run_id == run.id,
        PayrollException.run_employee_id == row.id, PayrollException.source == SYSTEM_SOURCE,
        PayrollException.status == "OPEN", PayrollException.dedupe_key.like(prefix)).with_for_update()}
    resolved = {k for (k,) in db.query(PayrollException.dedupe_key).filter(
        PayrollException.company_id == run.company_id, PayrollException.run_id == run.id,
        PayrollException.run_employee_id == row.id, PayrollException.status.in_(("WAIVED", "DEFERRED")))}
    wanted = {i.dedupe_key(row.id): i for i in issues}
    for key, existing in current.items():
        if key not in wanted:
            existing.status, existing.resolved_at = "FIXED", now
            existing.resolution_note = "No longer found when the inputs were locked again."
            existing.row_version += 1
    for key, item in wanted.items():
        if key in resolved and key not in current:
            continue
        values = dict(code=item.code, severity=item.severity, message=item.message[:1000],
                      details=_plain(dict(item.details)), resolutions=["FIX", "WAIVE"] if item.waivable else ["FIX"],
                      waivable=item.waivable, employee_id=row.employee_id,
                      blocks_finalization=item.severity == "BLOCKING")
        existing = current.get(key)
        if existing is None:
            db.add(PayrollException(company_id=run.company_id, run_id=run.id, run_employee_id=row.id,
                                    source=SYSTEM_SOURCE, dedupe_key=key[:200], status="OPEN", row_version=1, **values))
        elif any(getattr(existing, k) != v for k, v in values.items()):
            for k, v in values.items():
                setattr(existing, k, v)
            existing.row_version += 1
    db.flush()


def _reserve(db: Session, run: PayrollRun, row: PayrollRunEmployee, content: Mapping[str, Any]) -> None:
    """Hold the consumable inputs this snapshot pays for this run employee (released by RELEASE_INPUTS)."""
    from app.payroll.services import claims, runs, wage_inputs

    input_ids = [int(i["id"]) for i in content.get("wage_inputs") or ()]
    if input_ids:
        wage_inputs.reserve_inputs(db, run.company_id, row.id, input_ids)
    claim_ids = [int(c["id"]) for c in content.get("reimbursements") or ()]
    if claim_ids:
        claims.schedule_claims(db, run.company_id, claim_ids, runs.claims_ref(row.id))


def lock_inputs(db: Session, run: PayrollRun, employees: Iterable[PayrollRunEmployee | int], *,
                lock_time: Optional[datetime] = None, actor_user_id: Optional[int] = None) -> LockResult:
    """Build and store the snapshots of ``employees`` (rows or ids of this run) in the caller's transaction.

    Each run employee gets a new immutable snapshot version and points at it (``snapshot_id``,
    ``content_hash``); its wage inputs are reserved and claims scheduled; issues become SYSTEM exceptions.
    An employee whose inputs cannot be frozen gets a blocking SNAPSHOT_FAILED exception and no snapshot;
    the others are still locked (one employee's problem never fails the run).
    """
    from app.payroll.services import runs

    run = runs.load_run(db, run.company_id, run.id, lock=True)
    runs.ensure_editable(run)
    if run.status not in LOCKABLE_STATES:
        raise conflict(f"Inputs cannot be locked while the run is {run.status}.", "RUN_NOT_READY")
    lock_time = lock_time or clock.utcnow()
    wanted = sorted({e if isinstance(e, int) else e.id for e in employees})
    rows = (db.query(PayrollRunEmployee)
            .filter(PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id,
                    PayrollRunEmployee.id.in_(wanted or [0]))
            .order_by(PayrollRunEmployee.id).with_for_update(of=PayrollRunEmployee).populate_existing().all())
    if len(rows) != len(wanted):
        raise not_found("Run employee")
    excluded = [r.id for r in rows if r.status == "EXCLUDED"]
    if excluded:
        raise conflict(f"Excluded run employees have no inputs to lock: {excluded}.", "RUN_EMPLOYEE_EXCLUDED")
    period = db.query(PayPeriod).filter(PayPeriod.company_id == run.company_id, PayPeriod.id == run.pay_period_id).one()
    book = FxBook(db, run, period, lock_time=lock_time)
    result = LockResult()
    for row in rows:
        try:
            with db.begin_nested():
                built = build(db, run, row, lock_time=lock_time, fx=book)
                version = (db.query(func.coalesce(func.max(PayrollInputSnapshot.version), 0))
                           .filter(PayrollInputSnapshot.company_id == run.company_id,
                                   PayrollInputSnapshot.run_employee_id == row.id).scalar()) + 1
                stored = PayrollInputSnapshot(company_id=run.company_id, run_employee_id=row.id, version=version,
                                              content=built.content, content_hash=built.content_hash,
                                              source_hashes=built.source_hashes, created_by_user_id=actor_user_id,
                                              created_at=clock.utcnow())
                db.add(stored)
                db.flush()
                _reserve(db, run, row, built.content)
                row.snapshot_id, row.content_hash = stored.id, built.content_hash
                row.row_version += 1
                db.flush()
        except SnapshotBuildError as error:
            result.failed[row.id] = str(error.detail)
            built = None
            issues = [SnapshotIssue("SNAPSHOT_FAILED", "BLOCKING", f"The inputs could not be frozen: {error.detail}",
                                    error.details)]
        except AppError as error:
            if error.code not in ("INPUT_RESERVED", "INPUT_NOT_APPROVED", "INVALID_TRANSITION", "CONFLICT"):
                raise
            result.failed[row.id] = str(error.detail)
            built = None
            issues = [SnapshotIssue("INPUTS_CONFLICT", "BLOCKING", f"An input of this employee is held elsewhere: "
                                    f"{error.detail}", {"error_code": error.code})]
        else:
            result.snapshots[row.id] = stored
            issues = built.issues
        if built is None:
            # The savepoint rolled back this employee's writes, an FX snapshot captured for it included.
            book = FxBook(db, run, period, lock_time=lock_time)
        result.issues[row.id] = list(issues)
        _store_issues(db, run, row, issues)
    result.fx_snapshot_id = run.fx_snapshot_id
    from app.payroll.services import change_detection

    change_detection.schedule_matching(db, run.company_id)
    return result


# ---------------------------------------------------------------------------
# Hooks (policies.policy_in_use, settings.pack_constraints F-14, loans pay figures F-36)
# ---------------------------------------------------------------------------
def policy_in_use(db: Session, company_id: int, policy_id: int) -> bool:
    """Whether a stored run snapshot used a version of the policy (EC-CFG-16: then archive only)."""
    keys = [source_key("policy_version", v) for (v,) in db.query(PolicyVersion.id).filter(
        PolicyVersion.company_id == company_id, PolicyVersion.policy_id == policy_id)]
    if not keys:
        return False
    return db.query(PayrollInputSnapshot.id).filter(
        PayrollInputSnapshot.company_id == company_id,
        PayrollInputSnapshot.source_hashes.has_any(array(keys))).first() is not None


def pack_constraints(country_code: Optional[str], on: date) -> tuple:
    """``settings.pack_constraints`` with the environment (F-14): fixture packs constrain settings only
    outside production, exactly as they calculate there."""
    if not country_code:
        return ()
    hook = getattr(packs, "setting_constraints", None)
    if hook is None:
        return ()
    environment = packs.environment_name(app_settings.environment)
    try:
        params = inspect.signature(hook).parameters
    except (TypeError, ValueError):
        params = {}
    if "environment" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return tuple(hook(country_code, on, environment=environment) or ())
    return tuple(hook(country_code, on) or ())


def pay_figures(db: Session, company_id: int, employee_id: int, on: date, currency: str):
    """Monthly gross from the compensation in force (a no-LOP month of its structure) and monthly net from
    the last finalized regular run, both in ``currency`` (F-36, loan eligibility); unknown stays None."""
    from app.payroll.services import loans

    gross = net = None
    row = compensation_service.in_force_row(db, company_id, employee_id, on)
    if row is not None and row.currency == currency:
        try:
            evaluation = compensation_service.evaluate_assignment(db, row, on=on)
            if evaluation.ok and evaluation.result is not None:
                gross = Decimal(evaluation.result.totals.gross)
        except AppError:
            gross = None
    last = (db.query(PayrollRunEmployee.net)
            .join(PayrollRun, and_(PayrollRun.company_id == PayrollRunEmployee.company_id,
                                   PayrollRun.id == PayrollRunEmployee.run_id))
            .join(PayPeriod, and_(PayPeriod.company_id == PayrollRun.company_id, PayPeriod.id == PayrollRun.pay_period_id))
            .filter(PayrollRunEmployee.company_id == company_id, PayrollRunEmployee.employee_id == employee_id,
                    PayrollRunEmployee.status == "CALCULATED", PayrollRunEmployee.currency == currency,
                    PayrollRun.status.in_(sm.RUN_FINALIZED_STATES), PayrollRun.run_type == sm.RunType.REGULAR.value,
                    PayPeriod.period_end <= on)
            .order_by(PayPeriod.period_end.desc(), PayrollRun.id.desc()).first())
    if last is not None and last[0] is not None:
        net = Decimal(last[0])
    return loans.PayFigures(monthly_gross=gross, monthly_net=net)


def install_hooks() -> None:
    """Rebind the module-level hooks their modules call by name. Idempotent; runs at import."""
    from app.payroll.services import loans

    policy_service.policy_in_use = policy_in_use       # EC-CFG-16
    settings_engine.pack_constraints = pack_constraints  # F-14
    loans.set_pay_figures_provider(pay_figures)          # F-36


install_hooks()


__all__ = [
    "FxBook", "LOCKABLE_STATES", "LockResult", "OVERRIDE_KEY", "SnapshotBuild", "SnapshotBuildError", "SnapshotIssue",
    "build", "build_snapshot", "install_hooks", "latest_snapshot", "ledger_override_details", "lock_inputs",
    "pack_constraints", "pack_reference", "pay_figures", "policy_in_use", "source_hash", "source_key",
]

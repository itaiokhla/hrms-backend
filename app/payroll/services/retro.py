"""Retro deltas: arrears proposals for finalized periods, their approval, payment and recovery (3.3 §8.3,
3.4 §16, 3.5 §12; settings 3.1 §2.3; EC-CMP-12, EC-COR-01, -02, -05, -10, -17, EC-ATT-06, EC-LV-03, EC-LV-04,
EC-OT-07, EC-TSK-03).

Finalized runs are never changed (EC-RUN-17). A change that reaches a finalized period becomes a proposal:

* ``propose``: the affected period is recalculated by the engine from the period's frozen snapshot (or the
  snapshot of the latest live proposal of that run employee, so changes accumulate) with ONLY the changed
  sources replaced by their current values (``TRIGGER_SECTIONS``: compensation segments, the ledger fields of
  corrected attendance or leave, overtime entries, tasks with completion-date attribution, the past-dated
  settings or policies named, wage inputs). The result is compared per component with the corrected view
  of the period (the stored lines plus the deltas of approved proposals). Each difference is a line:
  ARREARS (positive, paid to the employee) or RECOVERY (negative), carrying the original period and the
  component's tags. Deltas are in the employee's perspective: an earning that grows and a deduction that
  shrinks are both positive. Statutory lines, employer contributions, rounding, reimbursements and lines
  of consumable sources paid elsewhere (loans, advances, claims, earlier arrears) are not compared: the
  country pack recomputes statutory items in the paying run (3.3 §8.3, EC-COR-10).
* A proposal is routed through the approvals engine (module ``payroll_run``, subject ``arrears_proposal``;
  the maker never approves, nobody approves their own arrears). A newer change of the same period
  supersedes a pending proposal (one pending per run employee). With ``payroll.arrears.auto_compute`` OFF
  nothing is proposed automatically.
* Approval plans instalments: a positive line is paid at once; recoveries follow
  ``payroll.arrears.recovery_mode`` and are split into instalments when one run cannot absorb them within
  the protected net pay (``payroll.deductions.min_net_pay_percent``, EC-COR-05). Without a protected net
  percentage, or in MANUAL mode, a recovery is an outstanding receivable and is never deducted
  automatically (G-23).
* Payment (``payroll.arrears.payment_mode``): NEXT_REGULAR_RUN offers the next instalment of each approved
  line to the employee's next regular run through ``arrears_for_snapshot`` (the snapshot builder's arrears
  hook, item id = instalment id); OFF_CYCLE waits for a RETRO_ADJUSTMENT run (``create_retro_run``) that
  claims the proposals. A FINAL_SETTLEMENT run settles everything approved. When a paying run is
  finalized (outbox ``payroll.run.finalized``), ``settle_run`` settles the instalments its lines paid
  (exactly once, under row locks); a recovery deducted only partly within the protected net leaves a new
  instalment for the remainder. Sources paid by the arrears (late overtime, tasks, inputs) are consumed
  once by the paying run employee (EC-VAR-01).
* ``corrected_view``: original lines + linked deltas + reversal lines of a run employee.

Services take (db, principal, ...) and never commit.
"""

from __future__ import annotations

import copy
import datetime as dt
import math
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import and_, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.approvals import ApprovalRequest
from app.models.compensation import CompensationAssignment, PayComponent
from app.models.corrections import (
    ARREARS_TRIGGERS,
    ArrearsInstalment,
    ArrearsProposal,
    ArrearsProposalLine,
)
from app.models.pay_runs import PayPeriod, PayrollInputSnapshot, PayrollLine, PayrollLineSource, PayrollRun, \
    PayrollRunEmployee
from app.payroll import audit as payroll_audit
from app.payroll import engine
from app.payroll import settings_registry as reg
from app.payroll import state_machines as sm
from app.payroll.engine import snapshot as snap
from app.payroll.money import CALC_CONTEXT, amount_str, quantize_amount
from app.payroll.services import access, approvals, jobs, outbox
from app.payroll.services import settings as settings_engine
from app.services.audit_log_service import AuditLogService

READ = "payroll:run:read"
PROPOSE = "payroll:run:create"
APPROVE = "payroll:run:approve"
APPROVAL_MODULE = "payroll_run"
APPROVAL_SUBJECT = "arrears_proposal"
AUDIT_MODULE = payroll_audit.PAYROLL_RUNS
AUTO_KEY = "payroll.arrears.auto_compute"
PAYMENT_MODE_KEY = "payroll.arrears.payment_mode"
RECOVERY_MODE_KEY = "payroll.arrears.recovery_mode"
PROTECTED_NET_KEY = "payroll.deductions.min_net_pay_percent"
FINALIZED_TOPIC = "payroll.run.finalized"
FINALIZED_JOB = "corrections.run_finalized"

ARREARS_PROPOSED = "ARREARS_PROPOSED"
ARREARS_SUPERSEDED = "ARREARS_SUPERSEDED"
ARREARS_APPROVED = "ARREARS_APPROVED"
ARREARS_REJECTED = "ARREARS_REJECTED"
ARREARS_CANCELLED = "ARREARS_CANCELLED"
ARREARS_INSTALMENT_SETTLED = "ARREARS_INSTALMENT_SETTLED"
ARREARS_SETTLED = "ARREARS_SETTLED"
RETRO_RUN_CREATED = "RETRO_RUN_CREATED"

# The snapshot sections each source kind replaces ("ONLY the changed sources"). Ledger triggers replace
# only their own fields of each day.
TRIGGER_SECTIONS: Mapping[str, Tuple[str, ...]] = {
    "COMPENSATION": ("segments", "components"),
    "ATTENDANCE": ("days",),
    "LEAVE": ("days",),
    "OVERTIME": ("overtime_entries",),
    "TASK": ("tasks",),
    "SETTING": ("settings", "switch_ranges"),
    "POLICY": ("policies",),
    "INPUT": ("wage_inputs",),
}
DETECTABLE = tuple(TRIGGER_SECTIONS)
DAY_FIELDS: Mapping[str, Tuple[str, ...]] = {
    "ATTENDANCE": ("attendance", "manual_lop", "on_duty"),
    "LEAVE": ("leaves",),
}
LIVE = ("PENDING_APPROVAL", "APPROVED", "SETTLED")
DIFF_KINDS = ("EARNING", "DEDUCTION")
EXCLUDED_CODES = frozenset(("ARREARS", "ARREARS_RECOVERY", "NET_ROUNDING", "LOAN_RECOVERY", "ADVANCE_RECOVERY"))
EXCLUDED_SOURCES = frozenset(("loan", "advance", "arrears", "claim", "court_order", "reversal"))
PAYING_RUN_TYPES = (sm.RunType.REGULAR.value, sm.RunType.RETRO_ADJUSTMENT.value, sm.RunType.FINAL_SETTLEMENT.value)
CORRECTABLE_RUN_TYPES = (sm.RunType.REGULAR.value, sm.RunType.OFF_CYCLE.value, sm.RunType.FINAL_SETTLEMENT.value,
                         sm.RunType.RETRO_ADJUSTMENT.value)
ZERO = Decimal(0)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _now() -> dt.datetime:
    return clock.utcnow()


def _text(value: Optional[str], limit: int = 1000) -> Optional[str]:
    cleaned = " ".join((value or "").split())
    return cleaned[:limit] or None


def _audit(db: Session, principal: Optional[Principal], action: str, description: str, proposal: ArrearsProposal,
           **details: Any) -> None:
    AuditLogService.log(db, principal, AUDIT_MODULE, action, description[:500], "arrears_proposal", proposal.id,
                        company_id=proposal.company_id, actor_type="user" if principal is not None else "system",
                        details=details)


def _money(amount: Optional[Decimal], currency: str) -> Optional[dict]:
    if amount is None:
        return None
    return {"amount": amount_str(Decimal(amount), currency), "currency": currency}


def _date(value: Any) -> dt.date:
    return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value))


def _setting(db: Session, company_id: int, key: str, *, employee_id: Optional[int] = None,
             legal_entity_id: Optional[int] = None, on: dt.date) -> Any:
    """A resolved setting; an invalid value is treated as unknown (None), never as a silent default."""
    try:
        return settings_engine.resolve(db, company_id, key, employee_id=employee_id, legal_entity_id=legal_entity_id,
                                       on=on).value
    except settings_engine.SettingConfigError:
        return None


def period_ref_text(start: dt.date, end: dt.date) -> str:
    """The engine's period reference ``YYYY-MM-DD..YYYY-MM-DD`` (engine.lines.Line.period_ref)."""
    return f"{start.isoformat()}..{end.isoformat()}"


# ---------------------------------------------------------------------------
# Component figures and the per-component diff (pure)
# ---------------------------------------------------------------------------
@dataclass
class Figure:
    """One component of a period in the employee's perspective (deductions negative)."""

    code: str
    kind: str
    category: Optional[str]
    tags: Tuple[str, ...] = ()
    component_id: Optional[int] = None
    natural: Decimal = ZERO          # the line amounts as stored (deductions positive)
    signed: Decimal = ZERO           # employee perspective


def _diffable(kind: str, statutory: bool, code: str, sources: Iterable[Mapping[str, Any]]) -> bool:
    if kind not in DIFF_KINDS or statutory or code in EXCLUDED_CODES:
        return False
    return not any(str((s or {}).get("type") or "").lower() in EXCLUDED_SOURCES for s in sources or ())


def figures(lines: Iterable[Mapping[str, Any]]) -> Dict[str, Figure]:
    """Per-component totals of line dicts ({component, kind, category, amount, tags, sources, statutory,
    component_id}); only the components a retro delta compares."""
    result: Dict[str, Figure] = {}
    for line in lines:
        code, kind = str(line["component"]), str(line["kind"])
        if not _diffable(kind, bool(line.get("statutory")), code, line.get("sources") or ()):
            continue
        amount = Decimal(str(line["amount"]))
        fig = result.setdefault(code, Figure(code, kind, line.get("category"), tuple(line.get("tags") or ()),
                                             line.get("component_id")))
        fig.natural += amount
        fig.signed += -amount if kind == "DEDUCTION" else amount
        if fig.component_id is None and line.get("component_id") is not None:
            fig.component_id = line.get("component_id")
    return result


@dataclass(frozen=True)
class DeltaLine:
    code: str
    kind: str
    category: Optional[str]
    tags: Tuple[str, ...]
    component_id: Optional[int]
    original: Decimal        # corrected view, natural sign
    recalculated: Decimal    # natural sign
    delta: Decimal           # employee perspective: > 0 ARREARS, < 0 RECOVERY

    @property
    def direction(self) -> str:
        return "ARREARS" if self.delta > 0 else "RECOVERY"


def diff(corrected: Mapping[str, Figure], recalculated: Mapping[str, Figure], currency: str) -> List[DeltaLine]:
    """The per-component differences (recalculated minus corrected view), rounded to the currency."""
    result: List[DeltaLine] = []
    for code in sorted(set(corrected) | set(recalculated)):
        old, new = corrected.get(code), recalculated.get(code)
        ref = new or old
        with localcontext(CALC_CONTEXT):
            delta = quantize_amount((new.signed if new else ZERO) - (old.signed if old else ZERO), currency)
        if delta == 0:
            continue
        result.append(DeltaLine(code, ref.kind, ref.category, tuple(sorted(set((new.tags if new else ())
                                                                             or (old.tags if old else ())))),
                                (new.component_id if new else None) or (old.component_id if old else None),
                                old.natural if old else ZERO, new.natural if new else ZERO, delta))
    return result


def split_amount(amount: Decimal, parts: int, currency: str) -> List[Decimal]:
    """``amount`` in ``parts`` instalments of equal rounded size; the last absorbs the rounding."""
    if parts <= 1:
        return [amount]
    with localcontext(CALC_CONTEXT):
        share = quantize_amount(amount / parts, currency, "DOWN")
        result = [share] * (parts - 1)
        result.append(amount - share * (parts - 1))
    return [a for a in result if a != 0]


def instalment_count(recovery: Decimal, capacity: Decimal) -> int:
    """How many runs a recovery needs when each run can absorb ``capacity`` (EC-COR-05)."""
    if recovery <= 0:
        return 1
    return max(1, math.ceil(recovery / capacity))


# ---------------------------------------------------------------------------
# Snapshots: the base and the changed sections
# ---------------------------------------------------------------------------
def original_snapshot(db: Session, row: PayrollRunEmployee) -> Optional[PayrollInputSnapshot]:
    query = db.query(PayrollInputSnapshot).filter(PayrollInputSnapshot.company_id == row.company_id,
                                                  PayrollInputSnapshot.run_employee_id == row.id)
    if row.snapshot_id is not None:
        found = query.filter(PayrollInputSnapshot.id == row.snapshot_id).first()
        if found is not None:
            return found
    return query.order_by(PayrollInputSnapshot.version.desc()).first()


def _latest_live(db: Session, company_id: int, run_employee_id: int) -> Optional[ArrearsProposal]:
    return (db.query(ArrearsProposal)
            .filter(ArrearsProposal.company_id == company_id, ArrearsProposal.original_run_employee_id == run_employee_id,
                    ArrearsProposal.status.in_(LIVE), ArrearsProposal.snapshot.isnot(None))
            .order_by(ArrearsProposal.id.desc()).first())


@dataclass
class Target:
    """A finalized run employee whose period a change reaches."""

    run: PayrollRun
    row: PayrollRunEmployee
    period: PayPeriod
    base: Dict[str, Any]
    base_proposal: Optional[ArrearsProposal]
    pending: Optional[ArrearsProposal]

    @property
    def company_id(self) -> int:
        return self.row.company_id

    @property
    def employee_id(self) -> int:
        return self.row.employee_id

    @property
    def start(self) -> dt.date:
        return _date(self.base["period"]["start"])

    @property
    def end(self) -> dt.date:
        return _date(self.base["period"]["end"])

    def day_range(self) -> Tuple[dt.date, dt.date]:
        dates = [_date(d["date"]) for d in self.base.get("days") or ()]
        return (min(dates), max(dates)) if dates else (self.start, self.end)


def _segment_key(segments: Sequence[Mapping[str, Any]], day: dt.date) -> Optional[str]:
    for seg in segments:
        if _date(seg["start"]) <= day <= _date(seg["end"]):
            return str(seg["key"])
    return None


def _compensation_sections(db: Session, t: Target) -> Dict[str, Any]:
    """Segments with the compensation now in force (each placement segment split by salary windows) and any
    component the new salary uses that the frozen catalogue lacks."""
    from app.payroll.services import compensation
    from app.payroll.services import components as comp

    base_segments = list(t.base.get("segments") or ())
    pieces = compensation.segments(db, t.company_id, t.employee_id, t.start, t.end)
    new_segments: List[Dict[str, Any]] = []
    for seg in base_segments:
        s_start, s_end = _date(seg["start"]), _date(seg["end"])
        parts = [p for p in pieces if _date(p["start"]) <= s_end and _date(p["end"]) >= s_start]
        for index, piece in enumerate(parts):
            key = str(seg["key"]) if len(parts) == 1 else f"{seg['key']}.{index + 1}"
            new_segments.append({"key": key, "start": max(s_start, _date(piece["start"])).isoformat(),
                                 "end": min(s_end, _date(piece["end"])).isoformat(),
                                 "assignment_id": seg.get("assignment_id"), "compensation": piece["compensation"]})
        if not parts:
            new_segments.append(dict(seg, compensation=None))
    days = []
    for day in t.base.get("days") or ():
        item = dict(day)
        if item.get("segment"):
            item["segment"] = _segment_key(new_segments, _date(item["date"])) or item["segment"]
        days.append(item)
    codes = {line["component"] for seg in new_segments if seg.get("compensation")
             for line in seg["compensation"].get("lines") or ()}
    components = [dict(c) for c in t.base.get("components") or ()]
    present = {c["code"] for c in components}
    missing = codes - present
    if missing:
        catalogue = comp.catalogue(db, t.company_id, t.end)
        components.extend(comp.component_def(catalogue[code]) for code in sorted(missing) if code in catalogue)
    return {"segments": new_segments, "components": components, "days": days}


def _day_sections(db: Session, t: Target, trigger: str) -> Dict[str, Any]:
    """Only the attendance (or leave) fields of each day are replaced by their current values."""
    from app.payroll.services import time_inputs

    from app.payroll.services import cycle_ledger

    first, last = t.day_range()
    hours = (t.base.get("employee") or {}).get("standard_hours_per_day")
    standard = Decimal(str(hours)) if hours else None
    window = cycle_ledger.split_window(db, t.company_id, t.period)
    if window is not None:  # JC-AS-10: the run's own window mapping (cycle_ledger), so only real changes differ
        current = list(cycle_ledger.window_ledger(db, t.company_id, t.employee_id, (t.period.period_start,
                                                  t.period.period_end), window,
                                                  standard_hours_per_day=standard).days)
    else:
        current = time_inputs.ledger_inputs(db, t.company_id, t.employee_id, first, last,
                                            standard_hours_per_day=standard)
    by_date = {d.date.isoformat(): snap.day_to_dict(d) for d in current}
    fields = DAY_FIELDS[trigger]
    days = []
    for day in t.base.get("days") or ():
        item = dict(day)
        now = by_date.get(str(item["date"]))
        if now is not None:
            for name in fields:
                item[name] = now.get(name)
        days.append(item)
    return {"days": days}


_OT_KEYS = ("entry_id", "date", "minutes", "status", "comp_off", "start", "end", "origin")


def _overtime_sections(db: Session, t: Target) -> Dict[str, Any]:
    from app.payroll.services import overtime

    first, last = t.day_range()
    entries = [{k: e.get(k) for k in _OT_KEYS} for e in overtime.approved_overtime(db, t.company_id, t.employee_id,
                                                                                  first, last)]
    return {"overtime_entries": entries}


def _task_sections(db: Session, t: Target) -> Dict[str, Any]:
    """Tasks attributed by completion date to the period and approved after it was paid (EC-TSK-03). With
    APPROVAL_DATE attribution a late task belongs to the next period: nothing changes here."""
    from app.payroll.services import tasks

    policy = ((t.base.get("policies") or {}).get("TASK_PAY_POLICY") or {}).get("fields") or {}
    current = dict(t.base.get("tasks") or {})
    if policy.get("attribution") != "COMPLETION_DATE":
        return {"tasks": current}
    records = list(current.get("records") or ())
    known = {str(r.get("task_id")) for r in records}
    for task in tasks.approved_tasks(db, t.company_id, t.employee_id, t.start, t.end, "COMPLETION_DATE"):
        if str(task["task_id"]) in known:
            continue
        records.append({k: task.get(k) for k in snap._TASK_KEYS})
    current["records"] = records
    return {"tasks": current}


def _setting_sections(db: Session, t: Target, keys: Sequence[str]) -> Dict[str, Any]:
    values = dict(t.base.get("settings") or {})
    ranges = {k: list(v) for k, v in (t.base.get("switch_ranges") or {}).items()}
    for key in keys:
        canonical = reg.canonical_key(key)
        defn = reg.REGISTRY.get(canonical)
        if defn is None:
            raise bad_request(f"{key} is not a payroll setting.", "UNKNOWN_SETTING")
        try:
            resolved = settings_engine.resolve(db, t.company_id, canonical, employee_id=t.employee_id, on=t.end)
        except settings_engine.SettingConfigError as exc:
            raise conflict(f"{canonical} cannot be resolved for the period: {exc}", "CONFIG_INVALID")
        values[canonical] = settings_engine.value_json(canonical, resolved.value)
        if canonical in ranges or defn.resolution_grain == "DAY":
            spans = settings_engine.resolve_ranges(db, t.company_id, canonical, employee_id=t.employee_id,
                                                   start=t.start, end=t.end)
            ranges[canonical] = [[a.isoformat(), b.isoformat()] for a, b, value in spans if value is True]
    return {"settings": values, "switch_ranges": ranges}


def _policy_sections(db: Session, t: Target, types: Sequence[str]) -> Dict[str, Any]:
    from app.payroll.services import policies

    current = {k: dict(v) for k, v in (t.base.get("policies") or {}).items()}
    for code in types:
        found = policies.resolve_for_employee(db, t.company_id, code, t.employee_id, t.end)
        if found.get("status") != "RESOLVED":
            current.pop(code, None)
            continue
        version = found["version"]
        current[code] = {"id": found["policy"]["id"], "version": version["version"],
                         "policy_hash": version["policy_hash"], "fields": version["fields"] or {}}
    return {"policies": current}


def _input_sections(db: Session, t: Target) -> Dict[str, Any]:
    from app.payroll.services import wage_inputs

    items = [dict(i) for i in t.base.get("wage_inputs") or ()]
    known = {str(i.get("id")) for i in items}
    for row in wage_inputs.approved_inputs(db, t.company_id, t.employee_id, t.start, t.end):
        if str(row["id"]) in known:
            continue
        items.append({"id": row["id"], "component": row["component_code"], "amount": row.get("amount"),
                      "units": row.get("units"), "currency": row.get("currency"), "source": row.get("source") or "MANUAL",
                      "external_ref": row.get("external_ref"), "reason": row.get("reason"),
                      "content_hash": row.get("version_hash")})
    return {"wage_inputs": items}


def current_sections(db: Session, t: Target, trigger: str, *, keys: Sequence[str] = (),
                     policy_types: Sequence[str] = ()) -> Dict[str, Any]:
    """The replacement sections for one changed source kind (current values, nothing else)."""
    if trigger == "COMPENSATION":
        return _compensation_sections(db, t)
    if trigger in DAY_FIELDS:
        return _day_sections(db, t, trigger)
    if trigger == "OVERTIME":
        return _overtime_sections(db, t)
    if trigger == "TASK":
        return _task_sections(db, t)
    if trigger == "SETTING":
        if not keys:
            raise bad_request("Name the settings whose past-dated change is applied.", "KEYS_REQUIRED")
        return _setting_sections(db, t, keys)
    if trigger == "POLICY":
        if not policy_types:
            raise bad_request("Name the policy types whose change is applied.", "POLICY_TYPES_REQUIRED")
        return _policy_sections(db, t, policy_types)
    if trigger == "INPUT":
        return _input_sections(db, t)
    raise bad_request(f"{trigger} changes are not recalculated automatically.", "TRIGGER_NOT_DETECTABLE")


def recalculate(base: Mapping[str, Any], replacements: Mapping[str, Any]) -> Tuple[Dict[str, Any], Any]:
    """The base snapshot with ``replacements`` applied, normalized, and the engine's result for it."""
    merged = copy.deepcopy(dict(base))
    for name, value in replacements.items():
        merged[name] = copy.deepcopy(value)
    try:
        parsed = snap.Snapshot.from_dict(merged)
    except snap.SnapshotError as exc:
        raise conflict(f"The recalculated period has invalid inputs: {exc}", "RETRO_SNAPSHOT_INVALID")
    return parsed.to_dict(), engine.calculate(parsed)


# ---------------------------------------------------------------------------
# Loading with scope
# ---------------------------------------------------------------------------
def employee_visible(db: Session, principal: Principal, permission: str, employee_id: int, start: dt.date,
                     end: dt.date) -> bool:
    """Whether the employee was inside the caller's payroll scope for ``permission`` during [start, end]."""
    from app.models.employee import Employee

    scope = access.payroll_employee_scope(db, principal, permission, start, end)
    if scope.all_company:
        return True
    return db.query(Employee.id).filter(Employee.company_id == principal.company_id, Employee.id == employee_id,
                                        scope.clause(Employee.id)).first() is not None


def _scoped_run_employee(db: Session, principal: Principal, run_employee_id: int, permission: str
                         ) -> Tuple[PayrollRun, PayrollRunEmployee]:
    from app.payroll.services import runs

    if not principal.has(permission):
        raise forbidden()
    row = db.query(PayrollRunEmployee).filter(PayrollRunEmployee.company_id == principal.company_id,
                                              PayrollRunEmployee.id == run_employee_id).first()
    if row is None:
        raise not_found("Run employee")
    run = runs.load_run(db, principal.company_id, row.run_id)
    if not access.target_scope(principal, permission).covers(run.legal_entity_id, run.pay_group_id):
        raise not_found("Run employee")
    period = db.query(PayPeriod).filter(PayPeriod.company_id == run.company_id, PayPeriod.id == run.pay_period_id).one()
    if not employee_visible(db, principal, permission, row.employee_id, period.period_start, period.period_end):
        raise not_found("Run employee")
    return run, row


def _proposal_query(db: Session, principal: Principal, permission: str):
    return db.query(ArrearsProposal).filter(
        ArrearsProposal.company_id == principal.company_id,
        access.run_scope_clause(principal, permission, ArrearsProposal.legal_entity_id, ArrearsProposal.pay_group_id))


def load_proposal(db: Session, principal: Principal, proposal_id: int, permission: str = READ, *,
                  lock: bool = False) -> ArrearsProposal:
    """A proposal in the caller's run scope whose employee is in the caller's payroll scope for the original
    period (403 without the permission, 404 otherwise)."""
    if not principal.has(permission):
        raise forbidden()
    query = _proposal_query(db, principal, permission).filter(ArrearsProposal.id == proposal_id)
    if lock:
        query = query.with_for_update(of=ArrearsProposal).populate_existing()
    row = query.first()
    if row is None or not employee_visible(db, principal, permission, row.employee_id, row.period_start,
                                           row.period_end):
        raise not_found("Arrears proposal")
    return row


# ---------------------------------------------------------------------------
# Finalized periods reached by a change
# ---------------------------------------------------------------------------
def finalized_run_employees(db: Session, company_id: int, employee_id: int, from_date: dt.date, *,
                            to_date: Optional[dt.date] = None) -> List[Tuple[PayrollRun, PayrollRunEmployee, PayPeriod]]:
    """Finalized (or later) run employees of the employee whose period ends on or after ``from_date``
    (corrections of a reversed run are pointless: those are skipped)."""
    reversed_ids = {rid for (rid,) in db.query(PayrollRun.reverses_run_id).filter(
        PayrollRun.company_id == company_id, PayrollRun.run_type == sm.RunType.REVERSAL.value,
        PayrollRun.status.in_(sm.RUN_FINALIZED_STATES))}
    query = (db.query(PayrollRun, PayrollRunEmployee, PayPeriod)
             .join(PayrollRunEmployee, and_(PayrollRunEmployee.company_id == PayrollRun.company_id,
                                            PayrollRunEmployee.run_id == PayrollRun.id))
             .join(PayPeriod, and_(PayPeriod.company_id == PayrollRun.company_id,
                                   PayPeriod.id == PayrollRun.pay_period_id))
             .filter(PayrollRun.company_id == company_id, PayrollRunEmployee.employee_id == employee_id,
                     PayrollRun.status.in_(sm.RUN_FINALIZED_STATES),
                     PayrollRun.run_type.in_((sm.RunType.REGULAR.value, sm.RunType.FINAL_SETTLEMENT.value)),
                     PayrollRunEmployee.status == "CALCULATED", PayPeriod.period_end >= from_date))
    if to_date is not None:
        query = query.filter(PayPeriod.period_start <= to_date)
    rows = query.order_by(PayPeriod.period_start, PayrollRun.id).all()
    return [(run, row, period) for run, row, period in rows
            if run.id not in reversed_ids or _partially_reversed(db, run, row)]


def _partially_reversed(db: Session, run: PayrollRun, row: PayrollRunEmployee) -> bool:
    """True when the run was reversed for other employees only (this one stays correctable)."""
    from app.models.corrections import PayrollReversal

    reversals = db.query(PayrollReversal.run_employee_ids).filter(
        PayrollReversal.company_id == run.company_id, PayrollReversal.original_run_id == run.id,
        PayrollReversal.status == "FINALIZED").all()
    return all(row.id not in (ids or []) for (ids,) in reversals)


def finalized_periods(db: Session, company_id: int, employee_id: int, from_date: dt.date) -> List[dict]:
    """The finalized periods of the employee from ``from_date`` ({run_id, run_employee_id, period_id, start,
    end}) — the shape ``compensation.finalized_periods`` returns."""
    return [{"run_id": run.id, "run_employee_id": row.id, "period_id": period.id,
             "start": period.period_start, "end": period.period_end}
            for run, row, period in finalized_run_employees(db, company_id, employee_id, from_date)]


# ---------------------------------------------------------------------------
# The corrected view
# ---------------------------------------------------------------------------
def _line_dict(line: PayrollLine) -> dict:
    return {"component": line.component_code, "kind": line.kind, "category": line.category,
            "amount": Decimal(line.amount), "tags": list(line.tags or ()), "sources": list(line.sources or ()),
            "statutory": bool(line.statutory), "component_id": line.component_id}


def stored_lines(db: Session, row: PayrollRunEmployee) -> List[PayrollLine]:
    return (db.query(PayrollLine).filter(PayrollLine.company_id == row.company_id,
                                         PayrollLine.run_employee_id == row.id)
            .order_by(PayrollLine.sort_order, PayrollLine.id).all())


def corrected_figures(db: Session, row: PayrollRunEmployee) -> Dict[str, Figure]:
    """Stored lines of the run employee plus the deltas of its approved (or settled) proposals."""
    result = figures(_line_dict(line) for line in stored_lines(db, row))
    deltas = (db.query(ArrearsProposalLine)
              .join(ArrearsProposal, and_(ArrearsProposal.company_id == ArrearsProposalLine.company_id,
                                          ArrearsProposal.id == ArrearsProposalLine.proposal_id))
              .filter(ArrearsProposal.company_id == row.company_id,
                      ArrearsProposal.original_run_employee_id == row.id,
                      ArrearsProposal.status.in_(("APPROVED", "SETTLED")),
                      ArrearsProposal.trigger != "REVERSAL")
              .all())
    for line in deltas:
        fig = result.setdefault(line.component_code, Figure(line.component_code, line.kind, line.category,
                                                            tuple(line.tags or ()), line.component_id))
        delta = Decimal(line.delta)
        fig.signed += delta
        fig.natural += -delta if line.kind == "DEDUCTION" else delta
    return result


# ---------------------------------------------------------------------------
# Proposing
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Change:
    trigger: str
    reason: Optional[str] = None
    keys: Tuple[str, ...] = ()
    policy_types: Tuple[str, ...] = ()
    ref: Mapping[str, Any] = field(default_factory=dict)

    def entry(self) -> dict:
        data: Dict[str, Any] = {"type": self.trigger}
        if self.keys:
            data["keys"] = sorted(self.keys)
        if self.policy_types:
            data["policy_types"] = sorted(self.policy_types)
        if self.ref:
            data["ref"] = dict(self.ref)
        return data


@dataclass
class Outcome:
    """What a change did to one finalized run employee (``proposal`` None: no difference)."""

    run_employee_id: int
    run_id: int
    period: dict
    lines: List[DeltaLine]
    proposal: Optional[ArrearsProposal] = None
    superseded_id: Optional[int] = None
    cancelled_id: Optional[int] = None
    issues: List[dict] = field(default_factory=list)


def _target(db: Session, run: PayrollRun, row: PayrollRunEmployee, period: PayPeriod) -> Target:
    pending = (db.query(ArrearsProposal)
               .filter(ArrearsProposal.company_id == row.company_id, ArrearsProposal.original_run_employee_id == row.id,
                       ArrearsProposal.status == "PENDING_APPROVAL")
               .with_for_update(of=ArrearsProposal).populate_existing().first())
    latest = _latest_live(db, row.company_id, row.id)
    if latest is not None:
        base, base_proposal = dict(latest.snapshot), latest
    else:
        stored = original_snapshot(db, row)
        if stored is None:
            raise conflict(f"Run employee #{row.id} has no frozen snapshot to recalculate.", "SNAPSHOT_MISSING")
        base, base_proposal = dict(stored.content), None
    return Target(run, row, period, base, base_proposal, pending)


def _issues_of(result: Any) -> List[dict]:
    return [{"code": i.code, "severity": i.severity, "message": i.message}
            for i in getattr(result, "issues", ()) if i.severity in ("BLOCKING", "WARNING")][:50]


def _component_ids(db: Session, company_id: int, codes: Iterable[str]) -> Dict[str, int]:
    codes = sorted(set(codes))
    if not codes:
        return {}
    return {code: cid for code, cid in db.query(PayComponent.code, PayComponent.id).filter(
        PayComponent.company_id == company_id, PayComponent.code.in_(codes))}


def evaluate(db: Session, run: PayrollRun, row: PayrollRunEmployee, period: PayPeriod, change: Change
             ) -> Tuple[Target, Dict[str, Any], Any, List[DeltaLine]]:
    """Recalculate one finalized period with the changed sources and diff it with the corrected view."""
    if change.trigger not in DETECTABLE:
        raise bad_request(f"trigger is one of {', '.join(DETECTABLE)}.", "INVALID_TRIGGER")
    t = _target(db, run, row, period)
    replacements = current_sections(db, t, change.trigger, keys=change.keys, policy_types=change.policy_types)
    new_snapshot, result = recalculate(t.base, replacements)
    recalculated = figures(line.to_json() | {"amount": line.amount} for line in result.lines)
    lines = diff(corrected_figures(db, row), recalculated, run.currency)
    return t, new_snapshot, result, lines


def _period_ref(run: PayrollRun, row: PayrollRunEmployee, period: PayPeriod) -> dict:
    return {"period_id": period.id, "start": period.period_start.isoformat(), "end": period.period_end.isoformat(),
            "run_id": run.id, "run_number": run.number, "run_employee_id": row.id}


def _totals(lines: Sequence[DeltaLine], currency: str) -> dict:
    arrears = sum((l.delta for l in lines if l.delta > 0), ZERO)
    recovery = sum((l.delta for l in lines if l.delta < 0), ZERO)
    return {"arrears": amount_str(arrears, currency), "recovery": amount_str(recovery, currency),
            "net_change": amount_str(arrears + recovery, currency)}


def _close_pending(db: Session, principal: Optional[Principal], pending: ArrearsProposal, *, status: str,
                   by: Optional[ArrearsProposal] = None, note: str) -> None:
    """Supersede (by a newer proposal) or cancel a pending proposal and withdraw its approval request."""
    if pending.approval_request_id is not None:
        approvals.withdraw(db, principal, module=APPROVAL_MODULE, subject_type=APPROVAL_SUBJECT,
                           subject_id=pending.id, reason=note)
    pending.status = status
    pending.superseded_by_id = by.id if by is not None else None
    pending.decision_note = note
    pending.decided_at = _now()
    pending.row_version += 1
    db.flush()
    _audit(db, principal, ARREARS_SUPERSEDED if status == "SUPERSEDED" else ARREARS_CANCELLED,
           f"Arrears proposal #{pending.id} {status.lower()}: {note}", pending,
           superseded_by_id=pending.superseded_by_id)


def propose(db: Session, principal: Optional[Principal], run: PayrollRun, row: PayrollRunEmployee, period: PayPeriod,
            change: Change, *, preview: bool = False) -> Outcome:
    """Recalculate one finalized run employee and record (or preview) the arrears proposal."""
    if not sm.is_run_finalized(run.status) or run.run_type not in CORRECTABLE_RUN_TYPES:
        raise conflict("Only finalized pay runs are corrected with arrears.", "RUN_NOT_FINALIZED")
    t, new_snapshot, result, lines = evaluate(db, run, row, period, change)
    outcome = Outcome(row.id, run.id, _period_ref(run, row, period), lines, issues=_issues_of(result))
    if preview:
        return outcome
    if not lines:
        if t.pending is not None and change.trigger in [c.get("type") for c in t.pending.changed_sources or ()]:
            # The change was undone before approval: nothing is owed any more.
            outcome.cancelled_id = t.pending.id
            _close_pending(db, principal, t.pending, status="CANCELLED", note="No difference remains after a newer "
                                                                              "change of the same period.")
        return outcome
    cid = row.company_id
    changed = list((t.pending.changed_sources if t.pending is not None else None)
                   or (t.base_proposal.changed_sources if t.base_proposal is not None else None) or [])
    changed = [c for c in changed if c.get("type") != change.trigger] + [change.entry()]
    ids = _component_ids(db, cid, (l.code for l in lines))
    proposal = ArrearsProposal(
        company_id=cid, employee_id=row.employee_id, legal_entity_id=run.legal_entity_id,
        pay_group_id=run.pay_group_id, original_run_id=run.id, original_run_employee_id=row.id,
        original_period_id=period.id, period_start=period.period_start, period_end=period.period_end,
        currency=run.currency, trigger=change.trigger, trigger_ref=dict(change.ref or {}), changed_sources=changed,
        base_proposal_id=t.base_proposal.id if t.base_proposal is not None else None, snapshot=new_snapshot,
        snapshot_hash=snap.content_hash(new_snapshot), calc_hash=result.calc_hash, engine_version=result.engine_version,
        totals=_totals(lines, run.currency), issues=outcome.issues, status="PENDING_APPROVAL",
        reason=_text(change.reason), created_by_user_id=principal.user_id if principal is not None else None,
        row_version=1)
    if t.pending is not None:
        # One pending proposal per run employee: the newer one replaces it (its sources are carried over).
        pending = t.pending
        if pending.approval_request_id is not None:
            approvals.withdraw(db, principal, module=APPROVAL_MODULE, subject_type=APPROVAL_SUBJECT,
                               subject_id=pending.id, reason="Superseded by a newer change of the same period")
        pending.status = "CANCELLED"  # moved to SUPERSEDED once the newer proposal has an id
        pending.decided_at = _now()
        pending.decision_note = "Superseded by a newer change of the same period."
        db.flush()
    db.add(proposal)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError as exc:
        if "uq_arrears_proposals_pending" in str(exc.orig):
            raise conflict("Another arrears proposal for this period is being recorded. Try again.",
                           "ARREARS_PROPOSAL_PENDING")
        raise
    for index, line in enumerate(lines):
        db.add(ArrearsProposalLine(
            company_id=cid, proposal_id=proposal.id, employee_id=row.employee_id, component_code=line.code,
            component_id=line.component_id or ids.get(line.code), kind=line.kind, category=line.category,
            tags=list(line.tags), original_amount=line.original, recalculated_amount=line.recalculated,
            delta=line.delta, currency=run.currency, direction=line.direction, period_ref=outcome.period,
            explanation=_explain(line, run.currency, period), sort_order=index))
    db.flush()
    if t.pending is not None:
        t.pending.status, t.pending.superseded_by_id = "SUPERSEDED", proposal.id
        t.pending.row_version += 1
        db.flush()
        outcome.superseded_id = t.pending.id
        _audit(db, principal, ARREARS_SUPERSEDED, f"Arrears proposal #{t.pending.id} superseded by #{proposal.id}",
               t.pending, superseded_by_id=proposal.id)
    net = sum((l.delta for l in lines), ZERO)
    request = approvals.submit(
        db, principal, module=APPROVAL_MODULE, subject_type=APPROVAL_SUBJECT, subject_id=proposal.id,
        subject_employee_id=row.employee_id, amount=amount_str(abs(net), run.currency), currency=run.currency,
        context={"legal_entity_id": run.legal_entity_id, "pay_group_id": run.pay_group_id,
                 "maker_user_ids": [principal.user_id] if principal is not None else [],
                 "label": f"Arrears {period.period_start.isoformat()}..{period.period_end.isoformat()}"})
    proposal.approval_request_id = request.id
    db.flush()
    outcome.proposal = proposal
    _audit(db, principal, ARREARS_PROPOSED, f"Proposed arrears #{proposal.id} for {period.period_start.isoformat()}.."
           f"{period.period_end.isoformat()} ({change.trigger})", proposal, trigger=change.trigger,
           original_run_id=run.id, original_run_employee_id=row.id, lines=len(lines),
           totals=proposal.totals, superseded=outcome.superseded_id, changed_sources=changed)
    if request.status != approvals.PENDING:  # an engine outcome reached without a decision (never auto-approves)
        _on_decided(db, principal, request)
    return outcome


def _explain(line: DeltaLine, currency: str, period: PayPeriod) -> str:
    what = "Arrears" if line.delta > 0 else "Recovery"
    return (f"{what} of {line.code} for {period.period_start.isoformat()}..{period.period_end.isoformat()}: "
            f"{amount_str(line.original, currency)} -> {amount_str(line.recalculated, currency)} "
            f"({amount_str(line.delta, currency)} {currency})")[:1000]


def _auto_on(db: Session, company_id: int, legal_entity_id: int, on: dt.date) -> bool:
    return _setting(db, company_id, AUTO_KEY, legal_entity_id=legal_entity_id, on=on) is True


def detect(db: Session, principal: Optional[Principal], company_id: int, employee_id: int, from_date: dt.date,
           change: Change, *, to_date: Optional[dt.date] = None, preview: bool = False,
           respect_switch: bool = True) -> List[Outcome]:
    """Every finalized period of the employee from ``from_date`` (to ``to_date``) gets its proposal."""
    outcomes = []
    for run, row, period in finalized_run_employees(db, company_id, employee_id, from_date, to_date=to_date):
        if respect_switch and not preview and not _auto_on(db, company_id, run.legal_entity_id, period.period_end):
            outcomes.append(Outcome(row.id, run.id, _period_ref(run, row, period), [], issues=[{
                "code": "ARREARS_AUTO_OFF", "severity": "WARNING",
                "message": "Automatic arrears are off: enter the difference as a wage input."}]))
            continue
        outcomes.append(propose(db, principal, run, row, period, change, preview=preview))
    return outcomes


def detect_for(db: Session, principal: Principal, *, employee_id: int, from_date: dt.date, change: Change,
               to_date: Optional[dt.date] = None, preview: bool = False) -> List[Outcome]:
    """The API: a processor applies a past change of one employee to the finalized periods it reaches."""
    if not principal.has(PROPOSE):
        raise forbidden()
    if to_date is not None and to_date < from_date:
        raise bad_request("to_date is before from_date.", "INVALID_RANGE")
    rows = finalized_run_employees(db, principal.company_id, employee_id, from_date, to_date=to_date)
    if not rows:
        access.ensure_employee_in_scope(db, principal, PROPOSE, employee_id, from_date, to_date or from_date)
        return []
    for run, row, period in rows:
        if not access.target_scope(principal, PROPOSE).covers(run.legal_entity_id, run.pay_group_id):
            raise not_found("Employee")
        access.ensure_employee_in_scope(db, principal, PROPOSE, employee_id, period.period_start, period.period_end)
    if principal.employee_id is not None and principal.employee_id == employee_id and not preview:
        raise forbidden("You cannot propose arrears for your own pay.")
    return detect(db, principal, principal.company_id, employee_id, from_date, change, to_date=to_date,
                  preview=preview)


def on_compensation_change(db: Session, principal: Optional[Principal], assignment: CompensationAssignment,
                           periods: Sequence[Mapping[str, Any]]) -> List[dict]:
    """The compensation hook (EC-CMP-12): an approved backdated salary reaching finalized periods proposes
    arrears for each of them; finalized runs are untouched."""
    if not periods:
        return []
    start = min(_date(p["start"]) for p in periods)
    end = max(_date(p["end"]) for p in periods)
    change = Change("COMPENSATION", reason=f"Backdated salary change #{assignment.id}",
                    ref={"type": "compensation_assignment", "id": assignment.id})
    outcomes = detect(db, principal, assignment.company_id, assignment.employee_id,
                      max(start, assignment.effective_from), change, to_date=end)
    return [{"run_employee_id": o.run_employee_id, "proposal_id": o.proposal.id if o.proposal else None}
            for o in outcomes if o.proposal is not None]


# ---------------------------------------------------------------------------
# Decisions (approvals engine callback) and instalments (EC-COR-05)
# ---------------------------------------------------------------------------
def _lines_of(db: Session, proposal: ArrearsProposal) -> List[ArrearsProposalLine]:
    return (db.query(ArrearsProposalLine)
            .filter(ArrearsProposalLine.company_id == proposal.company_id,
                    ArrearsProposalLine.proposal_id == proposal.id)
            .order_by(ArrearsProposalLine.sort_order, ArrearsProposalLine.id).all())


def _instalments(db: Session, proposal: ArrearsProposal, *, lock: bool = False) -> List[ArrearsInstalment]:
    query = db.query(ArrearsInstalment).filter(ArrearsInstalment.company_id == proposal.company_id,
                                               ArrearsInstalment.proposal_id == proposal.id)
    if lock:
        query = query.with_for_update(of=ArrearsInstalment).populate_existing()
    return query.order_by(ArrearsInstalment.proposal_line_id, ArrearsInstalment.number).all()


@dataclass(frozen=True)
class Plan:
    recovery_mode: str
    parts: int
    capacity: Optional[Decimal]
    issues: Tuple[dict, ...] = ()


def plan_recovery(mode: Optional[str], recovery: Decimal, *, gross: Optional[Decimal], net: Optional[Decimal],
                  min_net_percent: Optional[Decimal], currency: str) -> Plan:
    """How a recovery of ``recovery`` (positive) is taken back (pure, EC-COR-05, EC-COR-17, G-23).

    The capacity of one run is the net pay above the protected share of gross of the original period
    (net - gross x percent / 100). A recovery above it is split into instalments; without a protected net
    percentage or capacity it is a MANUAL receivable (never deducted automatically)."""
    mode = mode or "NEXT_RUN"
    if recovery <= 0:
        return Plan(mode, 1, None)
    if mode == "MANUAL":
        return Plan("MANUAL", 1, None, ({"code": "RECOVERY_MANUAL", "severity": "WARNING",
                                         "message": "The recovery is an outstanding receivable recovered manually "
                                                    "(payroll.arrears.recovery_mode = MANUAL)."},))
    if min_net_percent is None:
        return Plan("MANUAL", 1, None, ({"code": "PROTECTED_NET_REQUIRED", "severity": "WARNING",
                                         "message": "Set the protected net pay percentage before recoveries are "
                                                    "deducted automatically; recorded as a receivable."},))
    if gross is None or net is None:
        return Plan("MANUAL", 1, None, ({"code": "NO_RECOVERY_CAPACITY", "severity": "WARNING",
                                         "message": "No pay figures to size the recovery; recorded as a receivable."},))
    with localcontext(CALC_CONTEXT):
        capacity = quantize_amount(net - gross * Decimal(min_net_percent) / Decimal(100), currency, "DOWN")
    if capacity <= 0:
        return Plan("MANUAL", 1, None, ({"code": "NO_RECOVERY_CAPACITY", "severity": "WARNING",
                                         "message": "Net pay leaves nothing above the protected net; recorded as a "
                                                    "receivable."},))
    parts = instalment_count(recovery, capacity)
    issues: Tuple[dict, ...] = ()
    if parts > 1:
        issues = ({"code": "RECOVERY_SPLIT", "severity": "WARNING",
                   "message": f"The recovery exceeds what one run can absorb within the protected net pay "
                              f"({amount_str(capacity, currency)} {currency}); split into {parts} instalments."},)
    return Plan("INSTALMENTS" if parts > 1 else mode, parts, capacity, issues)


def _plan_instalments(db: Session, proposal: ArrearsProposal) -> None:
    lines = _lines_of(db, proposal)
    row = db.query(PayrollRunEmployee).filter(PayrollRunEmployee.company_id == proposal.company_id,
                                              PayrollRunEmployee.id == proposal.original_run_employee_id).one()
    on = clock.company_today(db, proposal.company_id)
    recovery = -sum((Decimal(l.delta) for l in lines if Decimal(l.delta) < 0), ZERO)
    percent = _setting(db, proposal.company_id, PROTECTED_NET_KEY, employee_id=proposal.employee_id, on=on)
    plan = plan_recovery(_setting(db, proposal.company_id, RECOVERY_MODE_KEY, employee_id=proposal.employee_id, on=on),
                         recovery, gross=Decimal(row.gross) if row.gross is not None else None,
                         net=Decimal(row.net) if row.net is not None else None,
                         min_net_percent=Decimal(str(percent)) if percent is not None else None,
                         currency=proposal.currency)
    if recovery > 0:
        proposal.recovery_mode = plan.recovery_mode
        proposal.issues = [*(proposal.issues or []), *plan.issues]
    for line in lines:
        delta = Decimal(line.delta)
        amounts = [delta] if delta > 0 or plan.recovery_mode == "MANUAL" else split_amount(delta, plan.parts,
                                                                                            proposal.currency)
        for number, amount in enumerate(amounts, start=1):
            db.add(ArrearsInstalment(company_id=proposal.company_id, proposal_line_id=line.id,
                                     proposal_id=proposal.id, employee_id=proposal.employee_id, number=number,
                                     amount=amount, currency=proposal.currency, status="PLANNED", row_version=1))
    db.flush()


def _on_decided(db: Session, principal: Optional[Principal], request: ApprovalRequest) -> None:
    proposal = (db.query(ArrearsProposal)
                .filter(ArrearsProposal.company_id == request.company_id, ArrearsProposal.id == request.subject_id)
                .with_for_update(of=ArrearsProposal).populate_existing().first())
    if proposal is None or proposal.status != "PENDING_APPROVAL":
        return
    now = _now()
    proposal.decided_by_user_id = principal.user_id if principal is not None else None
    proposal.decided_at = now
    if request.status == approvals.APPROVED:
        on = clock.company_today(db, proposal.company_id)
        mode = _setting(db, proposal.company_id, PAYMENT_MODE_KEY, legal_entity_id=proposal.legal_entity_id, on=on)
        proposal.payment_mode = mode if mode in ("NEXT_REGULAR_RUN", "OFF_CYCLE") else "NEXT_REGULAR_RUN"
        proposal.status = "APPROVED"
        _plan_instalments(db, proposal)
        action = ARREARS_APPROVED
    elif request.status == approvals.REJECTED:
        proposal.status = "REJECTED"
        step = next((s for s in reversed(request.steps) if s.status == "rejected"), None)
        proposal.decision_note = (step.remarks if step is not None else None) or "Rejected"
        action = ARREARS_REJECTED
    else:
        proposal.status = "CANCELLED"
        proposal.decision_note = proposal.decision_note or "The approval request was withdrawn."
        action = ARREARS_CANCELLED
    proposal.row_version += 1
    db.flush()
    _audit(db, principal, action, f"Arrears proposal #{proposal.id} {proposal.status.lower()}", proposal,
           payment_mode=proposal.payment_mode, recovery_mode=proposal.recovery_mode, request_id=request.id)


approvals.register_callback(APPROVAL_MODULE, APPROVAL_SUBJECT, _on_decided)


def _request_of(db: Session, proposal: ArrearsProposal) -> ApprovalRequest:
    if proposal.status != "PENDING_APPROVAL" or proposal.approval_request_id is None:
        raise conflict(f"This proposal is {proposal.status.lower().replace('_', ' ')}.", "ARREARS_NOT_PENDING")
    return db.query(ApprovalRequest).filter(ApprovalRequest.company_id == proposal.company_id,
                                            ApprovalRequest.id == proposal.approval_request_id).one()


def decide(db: Session, principal: Principal, proposal_id: int, decision: str, *, remarks: Optional[str] = None,
           expected_version: Optional[int] = None) -> ArrearsProposal:
    """Approve or reject the current approval step of a proposal (the approvals engine decides who may)."""
    proposal = load_proposal(db, principal, proposal_id, APPROVE, lock=True)
    if expected_version is not None and proposal.row_version != expected_version:
        raise conflict("This proposal was changed by someone else. Reload and try again.", "ROW_VERSION_CONFLICT")
    request = _request_of(db, proposal)
    approvals.decide(db, principal, request.id, decision, remarks)
    db.refresh(proposal)
    return proposal


# ---------------------------------------------------------------------------
# Payment: the snapshot arrears hook and settlement of finalized paying runs
# ---------------------------------------------------------------------------
def _live_retro_run_ids(db: Session, company_id: int, run_ids: Iterable[int]) -> set:
    ids = sorted({i for i in run_ids if i is not None})
    if not ids:
        return set()
    return {rid for (rid,) in db.query(PayrollRun.id).filter(PayrollRun.company_id == company_id,
                                                             PayrollRun.id.in_(ids),
                                                             PayrollRun.status != sm.RunStatus.CANCELLED.value)}


def payable_proposals(db: Session, run: PayrollRun, employee_id: int) -> List[ArrearsProposal]:
    """APPROVED proposals a run pays for one employee: the next regular run (NEXT_REGULAR_RUN), the retro run
    that claimed them, or the employee's final settlement (everything outstanding)."""
    if run.run_type not in PAYING_RUN_TYPES:
        return []
    rows = (db.query(ArrearsProposal)
            .filter(ArrearsProposal.company_id == run.company_id, ArrearsProposal.employee_id == employee_id,
                    ArrearsProposal.status == "APPROVED", ArrearsProposal.legal_entity_id == run.legal_entity_id,
                    ArrearsProposal.currency == run.currency, ArrearsProposal.original_period_id != run.pay_period_id)
            .order_by(ArrearsProposal.period_start, ArrearsProposal.id).all())
    live_claims = _live_retro_run_ids(db, run.company_id, (p.retro_run_id for p in rows))
    result = []
    for p in rows:
        claimed = p.retro_run_id if p.retro_run_id in live_claims else None
        if run.run_type == sm.RunType.RETRO_ADJUSTMENT.value:
            ok = claimed == run.id
        elif run.run_type == sm.RunType.FINAL_SETTLEMENT.value:
            ok = claimed in (None, run.id)
        else:
            ok = claimed is None and p.payment_mode == "NEXT_REGULAR_RUN"
        if ok:
            result.append(p)
    return result


def arrears_for_snapshot(db: Session, run: PayrollRun, run_employee: PayrollRunEmployee) -> List[dict]:
    """The snapshot builder's arrears hook: snapshot ``arrears`` items (engine ArrearsItem JSON) for one run
    employee — the next PLANNED instalment of each line of every proposal this run pays. A MANUAL recovery
    is never offered (no automatic recovery). The item id is the instalment id; ``settle_run`` matches the
    finalized lines back to it."""
    items: List[dict] = []
    for proposal in payable_proposals(db, run, run_employee.employee_id):
        lines = {l.id: l for l in _lines_of(db, proposal)}
        nxt: Dict[int, ArrearsInstalment] = {}
        for inst in _instalments(db, proposal):
            if inst.status == "PLANNED" and inst.proposal_line_id not in nxt:
                nxt[inst.proposal_line_id] = inst
        for line_id, inst in sorted(nxt.items()):
            line = lines[line_id]
            amount = Decimal(inst.amount)
            if amount < 0 and proposal.recovery_mode == "MANUAL":
                continue
            items.append({"id": inst.id, "component": line.component_code,
                          "amount": amount_str(amount, proposal.currency),
                          "period_ref": period_ref_text(proposal.period_start, proposal.period_end),
                          "tags": sorted(set(line.tags or ())),
                          "reason": f"Arrears proposal #{proposal.id} ({proposal.trigger.lower()}), instalment "
                                    f"{inst.number}"})
    return items


def _arrears_sources(line: PayrollLine) -> List[int]:
    found = []
    for source in line.sources or ():
        if str((source or {}).get("type")) == "arrears":
            try:
                found.append(int(str(source.get("id"))))
            except (TypeError, ValueError):
                continue
    return found


def _consume_paid_sources(db: Session, proposal: ArrearsProposal, run: PayrollRun, run_employee_id: int) -> dict:
    """Late sources paid through the arrears are consumed once, by the paying run employee (EC-VAR-01)."""
    from app.payroll.services import overtime, tasks, wage_inputs

    snapshot = proposal.snapshot or {}
    done: Dict[str, int] = {}
    triggers = {c.get("type") for c in proposal.changed_sources or ()}
    if "OVERTIME" in triggers:
        ids = []
        for entry in snapshot.get("overtime_entries") or ():
            ref = str(entry.get("entry_id") or "")
            if ref.startswith("overtime:"):
                ids.append(int(ref.split(":", 1)[1]))
        from app.models.work_items import OvertimeEntry
        free = [i for (i,) in db.query(OvertimeEntry.id).filter(
            OvertimeEntry.company_id == run.company_id, OvertimeEntry.id.in_(ids or [0]),
            OvertimeEntry.consumed_by_run_employee_id.is_(None), OvertimeEntry.status == "APPROVED")]
        done["overtime"] = overtime.consume_entries(db, run.company_id, free, run_employee_id) if free else 0
    if "TASK" in triggers:
        from app.models.work_items import Task, TaskAssignee
        ids = []
        for record in (snapshot.get("tasks") or {}).get("records") or ():
            ref = str(record.get("task_id") or "")
            if ref.startswith("task:"):
                ids.append(int(ref.split(":", 1)[1]))
        # Only this employee's unpaid share: other assignees of a shared task are paid by their own runs.
        free = [i for (i,) in db.query(TaskAssignee.task_id)
                .join(Task, (Task.company_id == TaskAssignee.company_id) & (Task.id == TaskAssignee.task_id))
                .filter(TaskAssignee.company_id == run.company_id, TaskAssignee.task_id.in_(ids or [0]),
                        TaskAssignee.employee_id == proposal.employee_id, TaskAssignee.paid_in_run_id.is_(None),
                        Task.status == "APPROVED")]
        done["tasks"] = tasks.mark_task_shares_paid(
            db, run.company_id, [(i, proposal.employee_id) for i in free], run.id) if free else 0
    if "INPUT" in triggers:
        from app.models.variable_inputs import WageInput
        ids = [int(i["id"]) for i in snapshot.get("wage_inputs") or () if str(i.get("id", "")).isdigit()]
        free = [i for (i,) in db.query(WageInput.id).filter(
            WageInput.company_id == run.company_id, WageInput.id.in_(ids or [0]), WageInput.status == "APPROVED")]
        done["wage_inputs"] = wage_inputs.consume_inputs(db, run.company_id, run_employee_id, free) if free else 0
    return done


def settle_run(db: Session, run: PayrollRun) -> dict:
    """A finalized run settles the instalments its arrears lines paid (idempotent, row-locked). A recovery the
    protected net allowed only in part leaves a new PLANNED instalment for the remainder."""
    if not sm.is_run_finalized(run.status):
        return {"settled": 0}
    lines = (db.query(PayrollLine).filter(PayrollLine.company_id == run.company_id, PayrollLine.run_id == run.id)
             .order_by(PayrollLine.id).all())
    paid: Dict[int, Tuple[Decimal, PayrollLine]] = {}
    for line in lines:
        for inst_id in _arrears_sources(line):
            amount = Decimal(line.amount)
            signed = -amount if line.kind == "DEDUCTION" else amount
            prior = paid.get(inst_id)
            paid[inst_id] = ((prior[0] if prior else ZERO) + signed, line)
    if not paid:
        return {"settled": 0}
    rows = (db.query(ArrearsInstalment)
            .filter(ArrearsInstalment.company_id == run.company_id, ArrearsInstalment.id.in_(sorted(paid)))
            .with_for_update(of=ArrearsInstalment).populate_existing().all())
    now = _now()
    settled, touched = 0, set()
    for inst in rows:
        if inst.status != "PLANNED":
            continue
        amount, line = paid[inst.id]
        planned = Decimal(inst.amount)
        if amount == 0 or (amount > 0) != (planned > 0):
            continue
        if abs(amount) > abs(planned):
            amount = planned
        inst.status, inst.settled_run_id, inst.settled_amount, inst.settled_at = "SETTLED", run.id, amount, now
        inst.row_version += 1
        remainder = planned - amount
        if remainder != 0:
            last = db.query(func.max(ArrearsInstalment.number)).filter(
                ArrearsInstalment.company_id == run.company_id,
                ArrearsInstalment.proposal_line_id == inst.proposal_line_id).scalar() or inst.number
            db.add(ArrearsInstalment(company_id=inst.company_id, proposal_line_id=inst.proposal_line_id,
                                     proposal_id=inst.proposal_id, employee_id=inst.employee_id, number=last + 1,
                                     amount=remainder, currency=inst.currency, status="PLANNED", row_version=1))
        _record_line_source(db, run, line, inst.id)
        settled += 1
        touched.add((inst.proposal_id, line.run_employee_id))
    db.flush()
    for proposal_id, run_employee_id in sorted(touched):
        proposal = (db.query(ArrearsProposal)
                    .filter(ArrearsProposal.company_id == run.company_id, ArrearsProposal.id == proposal_id)
                    .with_for_update(of=ArrearsProposal).populate_existing().one())
        consumed = _consume_paid_sources(db, proposal, run, run_employee_id)
        open_left = any(i.status == "PLANNED" for i in _instalments(db, proposal))
        _audit(db, None, ARREARS_INSTALMENT_SETTLED, f"Run {run.number} paid arrears of proposal #{proposal.id}",
               proposal, run_id=run.id, consumed=consumed)
        if not open_left and proposal.status == "APPROVED":
            proposal.status, proposal.settled_at = "SETTLED", now
            proposal.row_version += 1
            db.flush()
            _audit(db, None, ARREARS_SETTLED, f"Arrears proposal #{proposal.id} settled", proposal, run_id=run.id)
    return {"settled": settled, "proposals": len(touched)}


def _record_line_source(db: Session, run: PayrollRun, line: PayrollLine, instalment_id: int) -> None:
    """The consumed ARREARS line source (exactly once, EC-VAR-01), unless finalization already wrote it."""
    exists = db.query(PayrollLineSource.id).filter(
        PayrollLineSource.company_id == run.company_id, PayrollLineSource.source_type == "ARREARS",
        PayrollLineSource.source_id == instalment_id, PayrollLineSource.consumed.is_(True)).first()
    if exists is not None:
        return
    db.add(PayrollLineSource(company_id=run.company_id, line_id=line.id, run_id=run.id,
                             run_employee_id=line.run_employee_id, source_type="ARREARS", source_id=instalment_id,
                             source_key="", consumed=True, consumed_at=_now()))
    db.flush()


# ---------------------------------------------------------------------------
# RETRO_ADJUSTMENT runs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RetroRunRequest:
    pay_group_id: int
    pay_period_id: int
    proposal_ids: Tuple[int, ...] = ()
    label: Optional[str] = None


def create_retro_run(db: Session, principal: Principal, data: RetroRunRequest) -> PayrollRun:
    """A RETRO_ADJUSTMENT run paying approved proposals that wait for an off-cycle payment (or that the
    caller selects), referencing their original periods; it claims them so no other run pays them."""
    from app.payroll.services import runs

    if not principal.has(PROPOSE):
        raise forbidden()
    if not data.proposal_ids:
        raise bad_request("Select the approved arrears proposals to pay.", "PROPOSALS_REQUIRED")
    proposals = (_proposal_query(db, principal, PROPOSE)
                 .filter(ArrearsProposal.id.in_(sorted(set(data.proposal_ids))))
                 .with_for_update(of=ArrearsProposal).populate_existing().all())
    if len(proposals) != len(set(data.proposal_ids)):
        raise not_found("Arrears proposal")
    for p in proposals:
        if not employee_visible(db, principal, PROPOSE, p.employee_id, p.period_start, p.period_end):
            raise not_found("Arrears proposal")
        if p.status != "APPROVED":
            raise conflict(f"Arrears proposal #{p.id} is {p.status.lower()}, not approved.", "ARREARS_NOT_APPROVED")
        if p.pay_group_id != data.pay_group_id:
            raise conflict(f"Arrears proposal #{p.id} belongs to another pay group.", "PAY_GROUP_MISMATCH")
    live = _live_retro_run_ids(db, principal.company_id, (p.retro_run_id for p in proposals))
    taken = [p.id for p in proposals if p.retro_run_id in live]
    if taken:
        raise conflict(f"Arrears proposals {taken} are already in a retro run.", "ARREARS_ALREADY_CLAIMED")
    run = runs.create_run(db, principal, runs.RunRequest(
        run_type=sm.RunType.RETRO_ADJUSTMENT.value, pay_group_id=data.pay_group_id, pay_period_id=data.pay_period_id,
        label=data.label, employee_ids=tuple(sorted({p.employee_id for p in proposals})),
        original_period_ids=tuple(sorted({p.original_period_id for p in proposals}))))
    run.selection = {**(run.selection or {}), "arrears_proposal_ids": sorted(p.id for p in proposals)}
    for p in proposals:
        p.retro_run_id = run.id
        p.row_version += 1
    db.flush()
    for p in proposals:
        _audit(db, principal, RETRO_RUN_CREATED, f"Arrears proposal #{p.id} claimed by retro run {run.number}", p,
               run_id=run.id)
    return run


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def line_json(line: ArrearsProposalLine, instalments: Sequence[ArrearsInstalment] = ()) -> dict:
    return {
        "id": line.id, "component_code": line.component_code, "kind": line.kind, "category": line.category,
        "tags": list(line.tags or ()), "direction": line.direction,
        "original": _money(line.original_amount, line.currency),
        "recalculated": _money(line.recalculated_amount, line.currency),
        "delta": _money(line.delta, line.currency), "period_ref": line.period_ref, "explanation": line.explanation,
        "instalments": [{"id": i.id, "number": i.number, "amount": _money(i.amount, i.currency), "status": i.status,
                         "settled_run_id": i.settled_run_id, "settled_amount": _money(i.settled_amount, i.currency)}
                        for i in instalments if i.proposal_line_id == line.id],
    }


def proposal_json(db: Session, proposal: ArrearsProposal, *, with_lines: bool = True) -> dict:
    data = {
        "id": proposal.id, "employee_id": proposal.employee_id, "legal_entity_id": proposal.legal_entity_id,
        "pay_group_id": proposal.pay_group_id, "original_run_id": proposal.original_run_id,
        "original_run_employee_id": proposal.original_run_employee_id,
        "original_period": {"id": proposal.original_period_id, "start": proposal.period_start.isoformat(),
                            "end": proposal.period_end.isoformat()},
        "currency": proposal.currency, "trigger": proposal.trigger, "trigger_ref": proposal.trigger_ref or {},
        "changed_sources": proposal.changed_sources or [], "base_proposal_id": proposal.base_proposal_id,
        "superseded_by_id": proposal.superseded_by_id, "totals": {
            k: {"amount": v, "currency": proposal.currency} for k, v in (proposal.totals or {}).items()},
        "issues": proposal.issues or [], "status": proposal.status, "payment_mode": proposal.payment_mode,
        "recovery_mode": proposal.recovery_mode, "retro_run_id": proposal.retro_run_id,
        "approval_request_id": proposal.approval_request_id, "reason": proposal.reason,
        "decision_note": proposal.decision_note, "engine_version": proposal.engine_version,
        "calc_hash": proposal.calc_hash, "created_at": proposal.created_at.isoformat() if proposal.created_at else None,
        "decided_at": proposal.decided_at.isoformat() if proposal.decided_at else None,
        "settled_at": proposal.settled_at.isoformat() if proposal.settled_at else None,
        "row_version": proposal.row_version,
    }
    if with_lines:
        instalments = _instalments(db, proposal)
        data["lines"] = [line_json(l, instalments) for l in _lines_of(db, proposal)]
        outstanding = sum((Decimal(i.amount) for i in instalments if i.status == "PLANNED"), ZERO)
        data["outstanding"] = _money(outstanding, proposal.currency)
    return data


def get_proposal(db: Session, principal: Principal, proposal_id: int) -> dict:
    return proposal_json(db, load_proposal(db, principal, proposal_id))


def list_proposals(db: Session, principal: Principal, *, status: Optional[str] = None,
                   employee_id: Optional[int] = None, run_id: Optional[int] = None, skip: int = 0,
                   limit: int = 50) -> dict:
    """Proposals in the caller's run scope, employees limited to the caller's payroll scope (EC-SEC-05)."""
    if not principal.has(READ):
        raise forbidden()
    query = _proposal_query(db, principal, READ)
    if status is not None:
        query = query.filter(ArrearsProposal.status == status)
    if employee_id is not None:
        query = query.filter(ArrearsProposal.employee_id == employee_id)
    if run_id is not None:
        query = query.filter(ArrearsProposal.original_run_id == run_id)
    rows = query.order_by(ArrearsProposal.id.desc()).all()
    visible = [r for r in rows if employee_visible(db, principal, READ, r.employee_id, r.period_start, r.period_end)]
    return {"total": len(visible), "items": [proposal_json(db, r, with_lines=False)
                                             for r in visible[skip:skip + limit]]}


def corrected_view(db: Session, principal: Principal, run_id: int, run_employee_id: int) -> dict:
    """The corrected view of one run employee: original lines + linked deltas (3.5 §12)."""
    from app.models.corrections import PayrollReversal

    run, row = _scoped_run_employee(db, principal, run_employee_id, READ)
    if run.id != run_id:
        raise not_found("Run employee")
    original = figures(_line_dict(line) for line in stored_lines(db, row))
    corrected = corrected_figures(db, row)
    proposals = (db.query(ArrearsProposal)
                 .filter(ArrearsProposal.company_id == row.company_id,
                         ArrearsProposal.original_run_employee_id == row.id)
                 .order_by(ArrearsProposal.id).all())
    reversals = (db.query(PayrollReversal)
                 .filter(PayrollReversal.company_id == row.company_id, PayrollReversal.original_run_id == run.id,
                         PayrollReversal.status.in_(("APPROVED", "FINALIZED")))
                 .order_by(PayrollReversal.id).all())
    reversed_by = [{"reversal_id": r.id, "reversal_run_id": r.reversal_run_id, "status": r.status}
                   for r in reversals if row.id in (r.run_employee_ids or [])]
    cur = run.currency
    return {
        "run_id": run.id, "run_employee_id": row.id, "employee_id": row.employee_id, "currency": cur,
        "original": {code: _money(f.signed, cur) for code, f in sorted(original.items())},
        "deltas": [proposal_json(db, p) for p in proposals],
        "corrected": {code: _money(f.signed, cur) for code, f in sorted(corrected.items())},
        "reversed_by": reversed_by,
        "note": "Amounts are in the employee's perspective (deductions negative); statutory lines are recomputed in "
                "the paying run.",
    }


# ---------------------------------------------------------------------------
# The outbox handler for finalized runs (all corrections)
# ---------------------------------------------------------------------------
@jobs.register(FINALIZED_JOB)
def _on_run_finalized(ctx: jobs.JobContext) -> dict:
    """payroll.run.finalized: settle arrears paid by the run, complete reversals, payment-only off-cycle
    effects and final settlements (each idempotent)."""
    from app.payroll.services import final_settlement, off_cycle, reversals, runs

    run_id = ctx.payload.get("run_id")
    if not isinstance(run_id, int):
        raise jobs.PermanentJobError("payroll.run.finalized needs a run_id")
    run = runs.load_run(ctx.db, ctx.company_id, run_id)
    result = {"arrears": settle_run(ctx.db, run)}
    if run.run_type == sm.RunType.REVERSAL.value:
        result["reversal"] = reversals.on_run_finalized(ctx.db, run)
    elif run.run_type == sm.RunType.OFF_CYCLE.value:
        result["off_cycle"] = off_cycle.on_run_finalized(ctx.db, run)
    elif run.run_type == sm.RunType.FINAL_SETTLEMENT.value:
        result["final_settlement"] = final_settlement.on_run_finalized(ctx.db, run)
    return result


outbox.subscribe(FINALIZED_TOPIC, FINALIZED_JOB)

assert set(ARREARS_TRIGGERS) >= set(TRIGGER_SECTIONS)

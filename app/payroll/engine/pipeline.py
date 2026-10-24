"""The calculation pipeline of 3.5 §7 for one run employee, as a pure function of the snapshot.

    1  resolve          context over the snapshot (currency, settings, switches)       context.build_context
    2  inputs           the snapshot itself (frozen when the run locked its inputs)
    3  payable units    day ledger per segment, late-penalty LOP, overtime hours        here
    4  earnings         structure, time-based pay, inputs, arrears, reimbursements      earnings
    5-7 statutory       pre-tax items, contributions, withholding (country pack)       statutory.apply
    8  post-tax         court orders, loans, advances, voluntary, penalties            deductions.apply
    9  net              net pay and NET_ROUNDING                                       net.net_rounding
    10 employer cost    gross + employer contributions                                 net.totals
    11 payment split    across the payee's bank accounts                               net.payment_split
    12 validate         invariants, NEGATIVE_NET, variance, outliers, flags            validate

No stage reads a clock, the network or a database; iteration orders are explicit
and all arithmetic uses the 28-digit context, so ``calc_hash`` is reproducible
(EC-STA-04). Data problems become issues and never a silent zero: an employee
without any compensation is excluded with ``NO_SALARY`` (EC-CMP-14) and gets no
lines; formula, ledger and configuration errors are BLOCKING for the employee.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from app.payroll import time_pay as tp
from app.payroll import time_pay_overtime as tpo
from app.payroll.engine.context import CalcContext, ContextError, build_context
from app.payroll.engine.earnings import (
    blocking,
    calculate_earnings,
    from_time_pay_issue,
    holiday_premium_result,
    period_basis,
    standard_hours,
)
from app.payroll.engine.lines import STAGE_POST_TAX, CalcIssue, Line
from app.payroll.engine.net import PaymentLine, Totals, carried_rounding_lines, net_rounding, payment_split, totals
from app.payroll.engine.snapshot import ComponentDef, Snapshot, load
from app.payroll.engine.trace import calc_hash, plain_trace, sorted_lines
from app.payroll.engine.validate import validate
from app.payroll.ledger import (
    Classification,
    DayLedger,
    LedgerConfig,
    LedgerError,
    LedgerException,
    Severity,
    build_ledger,
)
from app.payroll.policy_types import PAID_HOLIDAY_TYPES

ENGINE_VERSION = "2.0.0"
WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
LEDGER_SEVERITY = {Severity.BLOCKING: "BLOCKING", Severity.WARNING: "WARNING", Severity.INFO: "FLAG"}
HOLIDAY_SWITCHES = ("payroll.holiday_pay.non_salaried.enabled", "payroll.holiday_work_premium.enabled",
                    "payroll.weekly_off_pay.enabled")

Stage = Callable[[CalcContext, Sequence[Line]], tuple[Any, ...]]   # (lines, issues) or (lines, issues, outputs)


@dataclass(frozen=True)
class CalcResult:
    """The outcome for one run employee: lines, totals, issues, payments and the reproducible ``calc_hash``.

    ``exceptions`` are BLOCKING and WARNING issues (the review queue), ``flags`` the FLAG items.
    ``excluded`` is set (with ``NO_SALARY``) when the employee cannot be calculated at all.
    ``stage_outputs`` carries what the run services persist besides lines: statutory liabilities,
    YTD increments and pack state, and the deduction carry-forwards with the protected-net figures.
    """

    lines: tuple[Line, ...]
    totals: Totals
    exceptions: tuple[CalcIssue, ...]
    flags: tuple[CalcIssue, ...]
    calc_hash: str
    content_hash: str
    engine_version: str = ENGINE_VERSION
    payments: tuple[PaymentLine, ...] = ()
    ledger: Mapping[str, Any] | None = None
    excluded: bool = False
    rates: Mapping[str, Decimal] = field(default_factory=dict)
    stage_outputs: Mapping[str, Any] = field(default_factory=dict)

    @property
    def issues(self) -> tuple[CalcIssue, ...]:
        return self.exceptions + self.flags

    @property
    def blocking(self) -> tuple[CalcIssue, ...]:
        return tuple(i for i in self.exceptions if i.severity == "BLOCKING")

    @property
    def ok(self) -> bool:
        return not self.blocking

    def line(self, component: str) -> Line | None:
        found = [line for line in self.lines if line.component == component]
        return found[0] if len(found) == 1 else None

    def amount(self, component: str) -> Decimal:
        return sum((line.amount for line in self.lines if line.component == component), Decimal(0))

    def to_json(self) -> dict[str, Any]:
        return {
            "engine_version": self.engine_version, "content_hash": self.content_hash, "calc_hash": self.calc_hash,
            "excluded": self.excluded, "lines": [line.to_json() for line in self.lines],
            "totals": self.totals.to_json(), "exceptions": [i.to_json() for i in self.exceptions],
            "flags": [i.to_json() for i in self.flags], "payments": [p.to_json() for p in self.payments],
            "ledger": self.ledger, "stage_outputs": self.stage_outputs,
        }


# --------------------------------------------------------------------------- stage 3: payable units


def ledger_config(ctx: CalcContext) -> LedgerConfig:
    """The ledger configuration from resolved settings and the ATTENDANCE_PAY/HOLIDAY_PAY policies (3.1 §5.2-§5.4)."""
    attendance = ctx.policy_fields("ATTENDANCE_PAY_POLICY") or {}
    holiday = ctx.policy_fields("HOLIDAY_PAY_POLICY") or {}
    non_salaried = ctx.switch_any("payroll.holiday_pay.non_salaried.enabled")
    any_holiday = any(ctx.switch_any(key) for key in HOLIDAY_SWITCHES)
    paid_types = holiday.get("paid_holiday_types") or PAID_HOLIDAY_TYPES
    half_day = ctx.setting("payroll.attendance.half_day_value", "0.5")
    return LedgerConfig(
        attendance_enabled=ctx.setting_bool("payroll.attendance.enabled"),
        leave_enabled=ctx.setting_bool("payroll.leave.enabled"),
        when_disabled_treat_approved_leave_as=ctx.setting("payroll.leave.when_disabled_treat_approved_leave_as",
                                                           "PAID_EXCUSED"),
        half_day_value=Decimal(str(half_day)),
        unmarked_day=attendance.get("unmarked_day") or "EXCEPTION",
        missing_punch_out=attendance.get("missing_punch_out") or "EXCEPTION",
        derive_status_from_hours=bool(attendance.get("derive_status_from_hours", False)),
        full_day_min_hours=Decimal(str(attendance.get("full_day_min_hours") or "8.0")),
        half_day_min_hours=Decimal(str(attendance.get("half_day_min_hours") or "4.0")),
        count_on_duty_as_present=attendance.get("count_on_duty_as_present", True) is not False,
        sandwich_rule=ctx.setting("payroll.lop.sandwich_rule", "OFF"),
        zero_paid_days=ctx.setting("payroll.lop.zero_paid_days", "EXCEPTION"),
        paid_holiday_types=frozenset(paid_types),
        standard_hours_per_day=standard_hours(ctx),
        employee_is_salaried=ctx.employee.salaried,
        non_salaried_holiday_pay=(holiday.get("non_salaried_holiday_pay") or "STANDARD_HOURS") if non_salaried
        else "OFF",
        holiday_eligibility_adjacent_days=(holiday.get("eligibility_adjacent_days") or "NONE") if non_salaried
        else "NONE",
        holiday_on_weekly_off=(holiday.get("holiday_on_weekly_off") or "NONE") if any_holiday else "NONE",
    )


def ledger_issue(item: LedgerException) -> CalcIssue:
    details = {"day": item.day.isoformat() if item.day else None, "slots": [s.value for s in item.slots],
               "sources": [s.to_dict() for s in item.sources], "detail": item.detail}
    return CalcIssue(item.code.value, LEDGER_SEVERITY[item.severity], item.message, plain_trace(details))


def build_day_ledger(ctx: CalcContext, issues: list[CalcIssue]) -> DayLedger | None:
    """The day ledger of the whole period (F-04); segment keys must match the snapshot's segments."""
    keys = {s.key for s in ctx.segments}
    for day in ctx.snapshot.days:
        if day.employed and day.segment not in keys:
            issues.append(blocking("LEDGER_INPUT_ERROR", f"{day.date.isoformat()} is employed but belongs to no "
                                   f"assignment segment ({day.segment or 'none'}).", date=day.date.isoformat()))
            return None
    dates = [d.date for d in ctx.snapshot.days]
    expected = (ctx.period.end - ctx.period.start).days + 1
    if not dates or dates[0] != ctx.period.start or dates[-1] != ctx.period.end or len(dates) != expected:
        issues.append(blocking("LEDGER_INPUT_ERROR", "The snapshot must carry every date of the period, employed or "
                               "not (F-04).", period=ctx.period.ref, days=len(dates)))
        return None
    try:
        cfg = ledger_config(ctx)
        ledger = build_ledger(ctx.snapshot.days, cfg, dict(ctx.snapshot.ledger_overrides))
    except (LedgerError, ValueError) as error:
        issues.append(blocking("LEDGER_INPUT_ERROR", f"The day ledger cannot be built: {getattr(error, 'message', error)}",
                               error_code=getattr(error, "code", "INVALID_INPUT")))
        return None
    for segment in ctx.segments:
        if segment.key not in ledger.segments:
            issues.append(blocking("LEDGER_INPUT_ERROR", f"Segment {segment.key} has no dates in the period.",
                                   segment=segment.key))
            return None
    return ledger


def late_penalty_stage(ctx: CalcContext, ledger: DayLedger, issues: list[CalcIssue]) -> tuple[DayLedger, tuple[Line, ...]]:
    """Late marks to penalty LOP (DAY_FRACTION) or a proposed penalty deduction (FIXED_AMOUNT) (3.4 §11, §17.4)."""
    key = "payroll.late_penalty.enabled"
    if not ctx.switch_any(key):
        return ledger, ()
    marks = []
    for record in ledger.days:
        if not record.employed or not ctx.switch_on(key, record.date):
            continue
        if record.late_mark:
            marks.append(tp.LateMark(record.date, "LATE", record.late_mark_regularised))
        if record.early_exit:
            marks.append(tp.LateMark(record.date, "EARLY_EXIT", record.early_exit_regularised))
    if not marks:
        return ledger, ()
    policy = ctx.policy_fields("LATE_PENALTY_POLICY")
    if policy is None:
        issues.append(blocking("POLICY_MISSING", "Late penalties are on but no late penalty policy applies.",
                               component="LATE_PENALTY"))
        return ledger, ()
    absent = [r.date for r in ledger.days if any(s.classification is Classification.ABSENT for s in r.slots)]
    allowed = True if ctx.pack is None else ctx.pack.get("penalty_deductions_allowed", True) is not False
    try:
        result = tp.late_penalty(marks, policy, enabled=True, penalty_allowed=allowed, currency=ctx.currency,
                                 absent_dates=absent, basis_days=period_basis(ctx),
                                 ordinary_lop_days=ledger.totals.slot_lop_days, rounding=ctx.line_mode)
    except tp.TimePayError as error:
        issues.append(blocking("CONFIG_ERROR", f"Late penalty: {error.message}", component="LATE_PENALTY",
                               error_code=error.code))
        return ledger, ()
    issues.extend(from_time_pay_issue(i, "LATE_PENALTY") for i in result.issues)
    if result.penalty_lop_days > 0:
        ledger = ledger.with_penalty_lop(result.penalty_lop_days)
    lines: tuple[Line, ...] = ()
    if result.line is not None:
        component = ctx.component(result.line.component) or ComponentDef(
            result.line.component, "DEDUCTION", "PENALTY_IF_LEGAL", stage=STAGE_POST_TAX)
        data = result.line.to_dict()
        lines = (Line(component.code, "DEDUCTION", component.category, STAGE_POST_TAX, result.line.amount,
                      ctx.currency, component.version_id, result.line.units, result.line.rate,
                      tags=component.tags, visibility=component.visibility,
                      sources=tuple(plain_trace(dict(s)) for s in result.line.sources),
                      trace={**data["trace"], "late_penalty": result.explanation, "proposed": True},
                      explanation=result.line.explanation, sort_order=component.sort_order),)
    return ledger, lines


def _day_types(ledger: DayLedger) -> dict:
    types = {}
    for record in ledger.days:
        if record.paid_holiday or any(s.classification is Classification.HOLIDAY for s in record.slots):
            types[record.date] = tp.HOLIDAY
        elif record.weekly_off:
            types[record.date] = tp.WEEKLY_OFF
        else:
            types[record.date] = tp.WORKDAY
    return types


def overtime_stage(ctx: CalcContext, ledger: DayLedger, issues: list[CalcIssue]) -> tp.OvertimeHours | None:
    """Approved overtime minutes per bucket (3.4 §9): requests, attendance thresholds and holiday work (F-06)."""
    key = "payroll.overtime.enabled"
    entries = list(ctx.snapshot.overtime_entries)
    policy = ctx.policy_fields("OVERTIME_POLICY")
    probe_ctx = ctx.evolve(ledger=ledger)
    try:
        probe = holiday_premium_result(probe_ctx, Decimal(1))
    except tp.TimePayError:
        probe = None  # reported with the premium lines in stage 4
    if probe is not None:
        entries.extend(probe.overtime_entries)
    if policy is not None and policy.get("source") == "ATTENDANCE_AUTO" and ctx.setting_bool("payroll.attendance.enabled"):
        worked = [tp.WorkedDay(r.date, r.worked_seconds, tp.WORKDAY,
                               shift_hours=r.standard_hours)
                  for r in ledger.days if r.employed and r.scheduled_working_day and r.hours_recorded]
        try:
            proposed = tp.threshold_overtime(worked, policy, week_start=_week_start(ctx))
        except tp.TimePayError as error:
            issues.append(blocking("CONFIG_ERROR", f"Overtime thresholds: {error.message}", component="OT_WORKDAY",
                                   error_code=error.code))
            return None
        # The overtime service also materializes ATTENDANCE_AUTO proposals as approved, audited entries (from
        # the same worked hours and policy) that reach the snapshot with origin THRESHOLD: a date that already
        # has such a stored entry is paid from it, never a second time from the ledger (3.4 §9, EC-OT-03).
        stored = {e.date for e in entries if e.origin == tpo.ORIGIN_THRESHOLD and e.status not in tpo.INACTIVE_STATUSES}
        entries.extend(e for e in proposed.entries if e.date not in stored)
    if not entries:
        return None
    if policy is None:
        if ctx.switch_any(key) and any(e.status == "APPROVED" for e in entries):
            issues.append(blocking("POLICY_MISSING", "Overtime is approved but no overtime policy applies.",
                                   component="OT_WORKDAY"))
        return None
    try:
        result = tp.overtime_hours(entries, policy, day_types=_day_types(ledger), enabled=ctx.switch_ranges(key),
                                   week_start=_week_start(ctx))
    except tp.TimePayError as error:
        issues.append(blocking("CONFIG_ERROR", f"Overtime: {error.message}", component="OT_WORKDAY",
                               error_code=error.code))
        return None
    issues.extend(from_time_pay_issue(i, "OT_WORKDAY") for i in result.issues)
    return result


def _week_start(ctx: CalcContext) -> int:
    value = ctx.setting("payroll.calendar.week_start_day", "MON")
    return WEEKDAYS.index(value) if value in WEEKDAYS else 0


def payable_units(ctx: CalcContext, issues: list[CalcIssue]) -> CalcContext | None:
    """Stage 3: ledger, penalty LOP and overtime hours, attached to the context for the later stages."""
    ledger = build_day_ledger(ctx, issues)
    if ledger is None:
        return None
    ledger, penalty_lines = late_penalty_stage(ctx, ledger, issues)
    issues.extend(ledger_issue(item) for item in ledger.exceptions)
    overtime = overtime_stage(ctx, ledger, issues)
    keys = tuple(ledger.segments)
    return ctx.evolve(ledger=ledger, period_facts=ledger.period, segment_keys=keys,
                      segment_facts=tuple(ledger.segments[k].unit_facts() for k in keys), overtime=overtime,
                      penalty_lines=penalty_lines)


# --------------------------------------------------------------------------- stages 5-8 (country pack, deductions)


def _stage(name: str) -> Stage | None:
    """The real stage module (imported lazily); its ``evaluate`` also yields the outputs to persist."""
    try:
        module = importlib.import_module(f"app.payroll.engine.{name}")
    except ModuleNotFoundError as error:
        if error.name == f"app.payroll.engine.{name}":
            return None
        raise
    evaluate = getattr(module, "evaluate", None)
    if evaluate is None:
        return getattr(module, "apply")

    def run(ctx: CalcContext, lines: Sequence[Line]) -> tuple[Any, ...]:
        result = evaluate(ctx, lines)
        return list(result.lines), list(result.issues), stage_outputs(name, result, ctx.currency)

    return run


def stage_outputs(name: str, result: Any, currency: str) -> dict[str, Any]:
    """JSON-ready side results of stages 5-8 that the run services persist."""
    if name == "statutory":
        return {
            "liabilities": [item.to_json() for item in getattr(result, "liabilities", ())],
            "ytd_increments": plain_trace(dict(getattr(result, "ytd_increments", {}) or {})),
            "pack_state": plain_trace(dict(getattr(result, "pack_state", {}) or {})),
            "bases": plain_trace(dict(getattr(result, "bases", {}) or {})),
            "taxable": plain_trace(getattr(result, "taxable", None)),
        }
    protection = getattr(result, "protected_net", None)
    return {
        "carry_forwards": [item.to_json(currency) for item in getattr(result, "carry_forwards", ())],
        "protected_net": None if protection is None else plain_trace(dict(protection.__dict__)),
        "skipped": plain_trace([dict(item) for item in getattr(result, "skipped", ())]),
    }


def _merge(before: Sequence[Line], returned: Sequence[Line]) -> list[Line]:
    """Stage modules return the input lines followed by their own; tolerate a module that returns only its own."""
    returned = list(returned)
    if len(returned) >= len(before) and all(a is b or a == b for a, b in zip(before, returned)):
        return returned
    return [*before, *returned]


def run_stage(name: str, ctx: CalcContext, lines: list[Line], issues: list[CalcIssue], outputs: dict[str, Any], *,
              needed: bool) -> list[Line]:
    apply = _stage(name)
    if apply is None:
        if needed:
            issues.append(blocking(f"{name.upper()}_STAGE_MISSING", f"The {name} stage is not installed; the "
                                   "employee cannot be finalized."))
        return lines
    produced, found, *extra = apply(ctx, list(lines))
    issues.extend(found)
    if extra:
        outputs[name] = extra[0]
    return _merge(lines, produced)


def _deductions_needed(ctx: CalcContext) -> bool:
    return bool(ctx.loans or ctx.advances or ctx.court_orders or ctx.deduction_inputs or ctx.penalty_lines
                or ctx.arrears_recoveries)


# --------------------------------------------------------------------------- the pipeline


def _result(snapshot: Snapshot, lines: Sequence[Line], figures: Totals, issues: Sequence[CalcIssue],
            payments: Sequence[PaymentLine] = (), ledger: DayLedger | None = None, excluded: bool = False,
            rates: Mapping[str, Decimal] | None = None, outputs: Mapping[str, Any] | None = None) -> CalcResult:
    ordered = tuple(sorted_lines(lines))
    content = snapshot.content_hash
    exceptions = tuple(i for i in issues if i.severity in ("BLOCKING", "WARNING"))
    flags = tuple(i for i in issues if i.severity not in ("BLOCKING", "WARNING"))
    return CalcResult(ordered, figures, exceptions, flags, calc_hash(content, ENGINE_VERSION, ordered), content,
                      ENGINE_VERSION, tuple(payments), None if ledger is None else ledger.to_dict(), excluded,
                      dict(sorted((rates or {}).items())), dict(outputs or {}))


def _standard_period(snap: Snapshot, segment: Any) -> Snapshot:
    """The segment's compensation over a plain full period: no LOP, attendance, inputs or recoveries."""
    days = tuple(replace(day, employed=True, suspended_unpaid=False, leaves=(), manual_lop=(), attendance=None,
                         on_duty=(), segment=segment.key) for day in snap.days)
    values = {**snap.settings, "payroll.attendance.enabled": False, "payroll.leave.enabled": False,
              "payroll.late_penalty.enabled": False}
    whole = replace(segment, start=snap.period.start, end=snap.period.end)
    return replace(snap, segments=(whole,), days=days, settings=values, switch_ranges={}, ledger_overrides={},
                   overtime_entries=(), non_working_day_work=(), shifts=(), wage_inputs=(), incentive=None,
                   arrears=(), reimbursements=(), loans=(), advances=(), court_orders=(), bank_accounts=())


def resolve_net_based(snap: Snapshot, ctx: CalcContext) -> tuple[Snapshot, list[CalcIssue], dict[str, Any]]:
    """NET_BASED compensation → the monthly gross whose standard-period net is the target (3.3 §6.3, EC-CMP-15).

    The gross is found by ``gross_up.solve_gross`` (bisection, at most 50 evaluations, one minor unit); each
    evaluation is a full calculation of a plain period. The period itself is then calculated as GROSS_BASED with
    that gross, so LOP, overtime and inputs apply as usual. Failure is BLOCKING and nothing is paid.
    """
    targets = [s for s in snap.segments if s.compensation is not None and s.compensation.structure_type == "NET_BASED"]
    if not targets:
        return snap, [], {}
    if not ctx.setting_bool("payroll.structures.net_based_enabled"):
        return snap, [blocking("NET_BASED_DISABLED", "The salary structure is net-based but "
                               "payroll.structures.net_based_enabled is off.")], {}
    from app.payroll.engine.gross_up import solve_gross

    issues: list[CalcIssue] = []
    traces: dict[str, Any] = {}
    segments = []
    for segment in snap.segments:
        compensation = segment.compensation
        if segment not in targets:
            segments.append(segment)
            continue
        if compensation.pay_basis_amount is None:
            issues.append(blocking("NET_BASED_TARGET_MISSING", f"Segment {segment.key} has no target net pay.",
                                   segment=segment.key))
            segments.append(segment)
            continue

        def gross_of(gross: Decimal, compensation: Any = compensation, segment: Any = segment) -> Snapshot:
            solved = replace(compensation, structure_type="GROSS_BASED", pay_basis_amount=gross)
            return replace(segment, compensation=solved)

        def net_of(gross: Decimal, segment: Any = segment, gross_of: Any = gross_of) -> Decimal:
            return calculate(_standard_period(snap, gross_of(gross))).totals.net_before_rounding

        solved = solve_gross(compensation.pay_basis_amount, net_of, ctx.currency, component=segment.key)
        traces[segment.key] = plain_trace(solved.trace)
        if not solved.converged:
            issues.append(solved.issue)
            segments.append(segment)
            continue
        segments.append(gross_of(solved.gross))
    return replace(snap, segments=tuple(segments)), issues, traces


def calculate(snapshot: Snapshot | Mapping[str, Any]) -> CalcResult:
    """Every stage for one run employee; malformed snapshots raise SnapshotError, data problems become issues."""
    original = load(snapshot)
    snap = original
    ctx = build_context(snap)
    issues: list[CalcIssue] = []
    outputs: dict[str, Any] = {}
    snap, solver_issues, solver_trace = resolve_net_based(snap, ctx)
    if solver_trace:
        outputs["gross_up"] = solver_trace
    if solver_issues:
        issues.extend(solver_issues)
        return _result(original, (), totals((), ctx.currency), issues, outputs=outputs)
    ctx = build_context(snap)
    if all(segment.compensation is None for segment in snap.segments):
        issues.append(blocking("NO_SALARY", "No active compensation assignment in the period: the employee is excluded "
                               "with this reason and is never paid zero (EC-CMP-14)."))
        return _result(original, (), totals((), ctx.currency), issues, excluded=True)
    for segment in snap.segments:
        if segment.compensation is None:
            issues.append(blocking("NO_SALARY", f"No active compensation in segment {segment.key} "
                                   f"({segment.start.isoformat()} to {segment.end.isoformat()}).", segment=segment.key))
        elif segment.compensation.currency != ctx.currency:
            issues.append(blocking("CURRENCY_MISMATCH", f"The compensation of segment {segment.key} is in "
                                   f"{segment.compensation.currency}; the pay currency is {ctx.currency}. An FX rule "
                                   "must convert it before payroll (EC-CMP-07).", segment=segment.key))
    if any(i.code == "CURRENCY_MISMATCH" for i in issues):
        return _result(original, (), totals((), ctx.currency), issues, outputs=outputs)
    ctx3 = payable_units(ctx, issues)
    if ctx3 is None:
        return _result(original, (), totals((), ctx.currency), issues, outputs=outputs)
    earned = calculate_earnings(ctx3)
    ctx4 = ctx3.evolve(ot_hourly_rate=earned.ot_hourly_rate)
    issues.extend(earned.issues)
    lines = list(earned.lines)
    lines = run_stage("statutory", ctx4, lines, issues, outputs, needed=True)
    lines = run_stage("deductions", ctx4, lines, issues, outputs, needed=_deductions_needed(ctx4))
    lines.extend(carried_rounding_lines(ctx4))
    rounding, carried, found = net_rounding(ctx4, lines)
    issues.extend(found)
    lines.extend(rounding)
    figures = totals(lines, ctx.currency, carried)
    payments, found = payment_split(ctx4, figures.net)
    issues.extend(found)
    issues.extend(validate(ctx4, lines, figures, payments, issues))
    return _result(original, lines, figures, issues, payments, ctx3.ledger, rates=earned.rates, outputs=outputs)


def replay(snapshot: Snapshot | Mapping[str, Any], expected_hash: str) -> tuple[bool, CalcResult]:
    """Recalculate from a stored snapshot and compare ``calc_hash`` (3.5 §8.4, EC-STA-04); never writes."""
    result = calculate(snapshot)
    return result.calc_hash == expected_hash, result


__all__ = ["CalcResult", "ContextError", "ENGINE_VERSION", "calculate", "ledger_config", "replay"]

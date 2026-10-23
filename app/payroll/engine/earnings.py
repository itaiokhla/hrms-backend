"""Stage 4: earnings lines from the structure, time-based pay, wage inputs, arrears and reimbursements.

Structure components (3.3 §3-§6, §9) are evaluated per assignment segment in
dependency order (``formula.dependency_order``) in two passes:

* **rate pass** — every component's full-period value with the segment's
  compensation: FIXED amounts (annual ÷ periods per year), PERCENT_OF, FORMULA,
  SLAB and BALANCING formulas read the other components' full-period values,
  RATE_X_UNITS evaluates its rate and its units separately, INPUT reads the
  approved wage inputs. ``RATE()``/``SUM_TAG_RATE()`` read these values (for
  RATE_X_UNITS the per-unit rate), so LOP never lowers an hourly rate.
  Full-period values are rounded with the component's line rounding (per-unit
  RATE_X_UNITS rates stay exact), so BALANCING absorbs the rounding of the others,
  the structure adds up to its total, and "50,000.00 × 28 ÷ 30" reproduces the line.
  BALANCING subtracts the employer contributions inside CTC that the snapshot
  froze from the compensation preview and must stay >= 0 (EC-CMP-04).
* **amounts** — prorated components (BY_PAID_DAYS, BY_PAID_HOURS,
  BY_EMPLOYMENT_DAYS) earn ``proration.prorate_segments`` of their segment
  rates, every segment divided by the full-period basis N, and the segment lines
  are rounded cumulatively (``proration.round_segments``, F-01) so the segments of
  a fully paid period add up to exactly the full rate. Components with proration
  NONE are paid by the assignment in force at the end of their last segment:
  FIXED and INPUT at their value, formulas evaluated against the earned (rounded)
  amounts so far (``SUM_TAG`` = current-run amounts), with ``PRORATE(x)`` applying
  that component's proration. RATE_X_UNITS lines are per segment: each segment's
  rate × that segment's units.

Formula and data errors never pay zero silently: the component gets no line and
the employee a BLOCKING ``FORMULA_ERROR`` with the error and the evaluation trace
(EC-CMP-02); components that read a failed component are skipped with
``DEPENDS_ON_FAILED``. Time-based lines come from ``app.payroll.time_pay`` with the
DAY-grain switch ranges of the snapshot (overtime F-06, holiday-work premium and
weekly-off pay with the I-16 split F-05, shift allowance, task pay with caps and the
minimum-wage floor). A structure line for one of those components (method other
than SYSTEM) replaces the engine's own line for it. All amounts are in the pay
currency; there is no implicit FX (EC-CMP-07).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, ROUND_UP, Decimal, localcontext
from typing import Any

from app.payroll import formula as fm
from app.payroll import time_pay as tp
from app.payroll.engine.context import CalcContext
from app.payroll.formula.ast import Binary, Call, ComponentRef, VariableRef, children
from app.payroll.formula.functions import (
    HOURLY_CONVERTIBLE_UNITS,
    RATE_UNIT_DAY,
    RATE_UNIT_HOUR,
    RATE_UNIT_PERIOD,
    REFERENCE_FUNCTIONS,
)
from app.payroll.engine.lines import STAGE_EARNINGS, CalcIssue, Line
from app.payroll.engine.snapshot import Compensation, ComponentDef, Segment, StructureLine
from app.payroll.engine.trace import (
    date_ranges,
    lop_note,
    money_json,
    money_text,
    plain_trace,
    plural,
    qty_text,
    rate_text,
    rounding_trace,
)
from app.payroll.ledger import HolidayType, LedgerTotals
from app.payroll.money import CALC_CONTEXT
from app.payroll.policy_types import DEFAULT_OT_RATE_FORMULA
from app.payroll.proration import Basis, ProrationError, ProrationKind, basis_days, prorate_segments, round_segments

ROUNDINGS = {"HALF_UP": ROUND_HALF_UP, "HALF_EVEN": ROUND_HALF_EVEN, "UP": ROUND_UP, "DOWN": ROUND_DOWN}
STANDARD_TAGS = ("LOP_APPLICABLE", "OT_BASE", "HOLIDAY_PAY_BASE", "ENCASHMENT_BASE", "PART_OF_CTC", "PART_OF_GROSS",
                 "TAXABLE_INCOME")
CTC_TYPES = ("CTC_BASED",)
TIME_PAY_COMPONENTS = frozenset({"OT_WORKDAY", "OT_WEEKLY_OFF", "OT_HOLIDAY", "HOLIDAY_PREMIUM", "WEEKLY_OFF_PREMIUM",
                                 "SHIFT_ALLOWANCE", "TASK_EARNING", "MIN_WAGE_TOP_UP", "HOLIDAY_PAY"})
CATEGORY_FIX = {"TASK": "TASK_EARNING", "ALLOWANCE": "SHIFT_PREMIUM"}
PAID_KINDS = ("EARNING", "REIMBURSEMENT", "BENEFIT_IN_KIND")
# The unit of a RATE_X_UNITS rate is read from its units expression (SUM_TAG_HOURLY, OT_BASE unit guard).
HOUR_UNIT_VARIABLES = frozenset({"att.payable_hours", "att.worked_hours", "att.night_hours", "ot.hours_workday",
                                 "ot.hours_weekly_off", "ot.hours_holiday", "ot.hours_total"})
DAY_UNIT_VARIABLES = frozenset({"att.paid_days", "att.present_days", "att.leave_days_paid", "att.holidays",
                                "period.days", "period.working_days", "period.basis_days"})
RATE_UNIT_OTHER = "OTHER"
ZERO = Decimal(0)


def blocking(code: str, message: str, *, component: str | None = None, **details: Any) -> CalcIssue:
    return CalcIssue(code, "BLOCKING", message, plain_trace(details), component)


def warning(code: str, message: str, *, component: str | None = None, **details: Any) -> CalcIssue:
    return CalcIssue(code, "WARNING", message, plain_trace(details), component)


def from_time_pay_issue(issue: tp.Issue, component: str | None = None) -> CalcIssue:
    severity = {"BLOCKING": "BLOCKING", "WARNING": "WARNING"}.get(issue.severity, "FLAG")
    details = dict(issue.details)
    if issue.date is not None:
        details["date"] = issue.date.isoformat()
    if issue.ref is not None:
        details["ref"] = issue.ref
    return CalcIssue(issue.code, severity, issue.message, details, component)


# --------------------------------------------------------------------------- shared helpers


def rounding_mode(ctx: CalcContext, component: ComponentDef | None) -> str:
    mode = component.rounding if component is not None and component.rounding else ctx.line_mode
    return mode if mode in ROUNDINGS else ctx.line_mode


def round_amount(ctx: CalcContext, value: Decimal, mode: str) -> Decimal:
    with localcontext(CALC_CONTEXT):
        return (value / ctx.quantum).quantize(Decimal(1), rounding=ROUNDINGS[mode]) * ctx.quantum + ZERO


def component_or_default(ctx: CalcContext, code: str, kind: str = "EARNING", category: str = "VARIABLE") -> ComponentDef:
    return ctx.component(code) or ComponentDef(code=code, kind=kind, category=category)


def make_line(ctx: CalcContext, component: ComponentDef, amount: Decimal, *, explanation: str,
              trace: Mapping[str, Any], units: Decimal | None = None, rate: Decimal | None = None,
              sources: Sequence[Mapping[str, Any]] = (), segment: int | None = None, period_ref: str | None = None,
              tags: Sequence[str] | None = None, kind: str | None = None, category: str | None = None,
              stage: int = STAGE_EARNINGS, sort_order: int | None = None) -> Line:
    if rate is not None and rate == rate.quantize(ctx.quantum):
        rate = rate.quantize(ctx.quantum)
    return Line(
        component=component.code, kind=kind or component.kind, category=category or component.category, stage=stage,
        amount=amount, currency=ctx.currency, component_version_id=component.version_id, units=units, rate=rate,
        period_ref=period_ref, segment=segment, tags=tuple(sorted(component.tags if tags is None else tags)),
        visibility=component.visibility, sources=tuple(plain_trace(dict(s)) for s in sources),
        trace=plain_trace(dict(trace)), explanation=explanation,
        sort_order=component.sort_order if sort_order is None else sort_order,
    )


def time_pay_line(ctx: CalcContext, line: tp.TimePayLine, *, extra_trace: Mapping[str, Any] | None = None) -> Line:
    """A ``time_pay`` line as an engine line, with the catalogue's version, tags and visibility."""
    component = component_or_default(ctx, line.component, line.kind, CATEGORY_FIX.get(line.category, line.category))
    trace = dict(line.to_dict()["trace"])
    trace["rate"] = money_json(line.rate, ctx.currency)
    trace["amount"] = money_json(line.amount, ctx.currency)
    if extra_trace:
        trace.update(extra_trace)
    return make_line(ctx, component, line.amount, explanation=line.explanation, trace=trace, units=line.units,
                     rate=line.rate, sources=line.sources, kind=line.kind)


def money_amount(value: Any, currency: str, what: str) -> Decimal:
    """A formula result as an amount in the pay currency: plain numbers are pay-currency amounts."""
    if isinstance(value, fm.MoneyValue):
        if value.currency != currency:
            raise fm.FormulaError(fm.ErrorCode.CURRENCY_MISMATCH,
                                  f"{what} is in {value.currency}, the pay currency is {currency}; no implicit FX")
        return value.amount
    if isinstance(value, Decimal) and not isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    raise fm.FormulaError(fm.ErrorCode.TYPE_MISMATCH, f"{what} must be an amount, got {value!r}")


def _completed_months(start: dt.date, end: dt.date) -> int:
    months = (end.year - start.year) * 12 + end.month - start.month
    if end.day < start.day:
        months -= 1
    return max(0, months)


def build_tables(ctx: CalcContext) -> dict[str, fm.Table]:
    tables: dict[str, fm.Table] = {}
    for raw in ctx.snapshot.tables:
        rows = []
        for row in raw.get("rows") or ():
            rows.append(fm.TableRow(**{k: (Decimal(v) if k in ("value", "lower", "upper", "rate") and v is not None
                                           else v) for k, v in row.items()}))
        tables[raw["code"]] = fm.Table(raw["code"], raw["kind"], tuple(rows), raw.get("key_currency"),
                                       raw.get("value_currency"), bool(raw.get("upper_inclusive", False)))
    return tables


# --------------------------------------------------------------------------- variables (3.3 §4.3)


def period_basis(ctx: CalcContext, basis: str | None = None) -> Decimal | None:
    chosen = basis or ctx.setting("payroll.lop.proration_basis", "CALENDAR_DAYS")
    try:
        return basis_days(chosen, ctx.period_facts, custom_days=ctx.setting("payroll.lop.custom_days"))
    except ProrationError:
        return None


def variables(ctx: CalcContext, totals: LedgerTotals, *, compensation: Compensation | None,
              task: tp.TaskPay | None = None) -> dict[str, Any]:
    """``period.*``, ``att.*``, ``ot.*``, ``task.*``, ``emp.*`` and ``ctc.*`` for a formula (NULL when the source is OFF)."""
    period, emp, facts = ctx.period, ctx.employee, ctx.period_facts
    attendance = ctx.setting_bool("payroll.attendance.enabled")
    leave = ctx.setting_bool("payroll.leave.enabled")
    overtime = ctx.switch_any("payroll.overtime.enabled")
    tasks = ctx.switch_any("payroll.task_pay.enabled")
    end = period.end
    values: dict[str, Any] = {
        "period.start": period.start, "period.end": period.end, "period.days": period.days,
        "period.working_days": Decimal(facts.working_days) if facts else None,
        "period.basis_days": period_basis(ctx),
        "period.number_in_tax_year": period.number_in_tax_year,
        "period.is_last_in_tax_year": period.is_last_in_tax_year, "period.pay_date": period.pay_date,
        "att.paid_days": totals.paid_days_after_penalty, "att.lop_days": totals.lop_days,
        "att.present_days": totals.present_days if attendance else None,
        "att.half_days": totals.half_days if attendance else None,
        "att.leave_days_paid": totals.leave_days_paid if leave else None,
        "att.leave_days_unpaid": totals.leave_days_unpaid if leave else None,
        "att.holidays": totals.holidays, "att.weekly_offs": totals.weekly_offs,
        "att.payable_hours": totals.payable_hours if attendance else None,
        "att.worked_hours": totals.worked_hours if attendance else None,
        "att.night_hours": totals.night_hours if attendance else None,
        "att.late_marks": totals.late_marks if attendance else None,
        "emp.grade": emp.grade, "emp.employment_type": emp.employment_type, "emp.worker_class": emp.worker_class,
        "emp.designation": emp.designation,
        "emp.years_of_service": Decimal(_completed_months(emp.hire_date, end) // 12),
        "emp.months_of_service": Decimal(_completed_months(emp.hire_date, end)),
        "emp.hire_date": emp.hire_date, "emp.exit_date": emp.exit_date,
        "emp.is_joiner": period.start <= emp.hire_date <= end,
        "emp.is_leaver": emp.exit_date is not None and period.start <= emp.exit_date <= end,
        "emp.fte": emp.fte, "emp.standard_hours_per_day": standard_hours(ctx),
        "emp.country": emp.country, "emp.subdivision": emp.subdivision,
    }
    if overtime:
        ot = ctx.overtime
        values.update({
            "ot.hours_workday": ot.hours_workday if ot else ZERO,
            "ot.hours_weekly_off": ot.hours_weekly_off if ot else ZERO,
            "ot.hours_holiday": ot.hours_holiday if ot else ZERO,
            "ot.hours_total": ot.hours_total if ot else ZERO,
            "ot.hourly_rate": None if ctx.ot_hourly_rate is None else fm.MoneyValue(ctx.ot_hourly_rate, ctx.currency),
        })
    if tasks:
        values["task.amount"] = fm.MoneyValue(task.paid if task else ZERO, ctx.currency)
        values["task.units"] = sum((i.quantity for i in task.items), ZERO) if task else ZERO
    annual, monthly = ctc_values(ctx, compensation)
    values["ctc.annual"] = None if annual is None else fm.MoneyValue(annual, ctx.currency)
    values["ctc.monthly"] = None if monthly is None else fm.MoneyValue(monthly, ctx.currency)
    values["ctc.period"] = None if annual is None else fm.MoneyValue(per_period(ctx, annual), ctx.currency)
    for name, raw in emp.attributes.items():
        values[f"emp.attr.{name}"] = raw
    return values


def ctc_values(ctx: CalcContext, compensation: Compensation | None) -> tuple[Decimal | None, Decimal | None]:
    """``ctc.annual`` and ``ctc.monthly``: a CTC-based pay basis is annual, a gross-based one monthly.

    NET_BASED structures are solved into a gross-based one before earnings run (``pipeline.resolve_net_based``),
    so their formulas read the solved monthly gross as ``ctc.monthly``.
    """
    if compensation is None or compensation.pay_basis_amount is None:
        return None, None
    amount = compensation.pay_basis_amount
    with localcontext(CALC_CONTEXT):
        if compensation.structure_type in CTC_TYPES:
            return amount, amount / 12
        if compensation.structure_type == "GROSS_BASED":
            return amount * 12, amount
    return None, None


def per_period(ctx: CalcContext, annual: Decimal) -> Decimal:
    """``ctc.period``: the annual amount ÷ the periods of this calendar's tax year (3.5 §2.1: 26/27 bi-weekly).

    3.3 §6.1 defines the pay basis of a CTC_BASED structure as annual and of a GROSS_BASED one as monthly
    (annual = 12 × monthly), so on a monthly calendar ``ctc.period`` is exactly ``ctc.monthly`` (decision I-20).
    """
    with localcontext(CALC_CONTEXT):
        return annual / ctx.period.periods_per_year


def monthly_calendar(ctx: CalcContext) -> bool:
    return ctx.period.periods_per_year == 12


def standard_hours(ctx: CalcContext) -> Decimal:
    value = ctx.employee.standard_hours_per_day
    return value if value is not None else (ctx.setting_decimal("payroll.standard_hours_per_day") or Decimal(8))


def rate_unit(line: StructureLine) -> str:
    """The unit of a structure line's full-period rate (``EvalContext.rate_units``, read by SUM_TAG_HOURLY).

    RATE_X_UNITS lines are per HOUR when their units read only hour variables (``att.payable_hours``,
    ``ot.hours_*``, ...), per DAY when they read only day variables (``att.paid_days``, ...), and OTHER (pieces,
    wage-input units, constants or a mix) otherwise; math and logic functions such as COALESCE do not change
    the unit. Every other method gives an amount for the whole period (PERIOD).
    """
    if line.method != "RATE_X_UNITS":
        return RATE_UNIT_PERIOD
    try:
        expr = fm.parse(str(dict(line.params).get("units")))
    except fm.FormulaError:
        return RATE_UNIT_OTHER
    names: set[str] = set()
    for node in fm.walk(expr):
        if isinstance(node, VariableRef):
            names.add(node.name)
        elif isinstance(node, ComponentRef) or (isinstance(node, Call) and node.name in REFERENCE_FUNCTIONS):
            return RATE_UNIT_OTHER
    if names and names <= HOUR_UNIT_VARIABLES:
        return RATE_UNIT_HOUR
    if names and names <= DAY_UNIT_VARIABLES:
        return RATE_UNIT_DAY
    return RATE_UNIT_OTHER


# --------------------------------------------------------------------------- structure evaluation


@dataclass
class _Plan:
    """One segment's structure: formulas, analyses, order and the values of its rate pass."""

    segment: Segment
    compensation: Compensation
    env_components: dict[str, fm.ComponentSig]
    tags: dict[str, list[str]]
    order: list[str] = field(default_factory=list)
    exprs: dict[str, Any] = field(default_factory=dict)          # code -> parsed formula (or (rate, units))
    sources: dict[str, str] = field(default_factory=dict)        # code -> formula text for the trace
    rates: dict[str, Decimal] = field(default_factory=dict)      # full-period value (RATE_X_UNITS: amount)
    unit_rates: dict[str, Decimal] = field(default_factory=dict)  # RATE()/SUM_TAG_RATE values
    units: dict[str, Decimal] = field(default_factory=dict)       # RATE_X_UNITS units
    rate_units: dict[str, str] = field(default_factory=dict)      # code -> PERIOD | DAY | HOUR | OTHER
    steps: dict[str, list[str]] = field(default_factory=dict)
    references: dict[str, dict[str, str]] = field(default_factory=dict)
    notes: dict[str, list[str]] = field(default_factory=dict)
    deps: dict[str, set[str]] = field(default_factory=dict)       # code -> structure components it reads
    skipped: set[str] = field(default_factory=set)                # not eligible, frequency, ...
    failed: set[str] = field(default_factory=set)


def _tag_calls(node: Any, by_basis: bool = False) -> list[tuple[Call, bool]]:
    """SUM_TAG_RATE / SUM_TAG_HOURLY calls of a formula, each with whether it is (part of) a dividend whose
    divisor reads ``period.basis_days``: ``SUM_TAG_RATE("A") / period.basis_days / 8 + SUM_TAG_RATE("B")``
    divides A by the basis days, not B."""
    found: list[tuple[Call, bool]] = []
    if isinstance(node, Call) and node.name in ("SUM_TAG_RATE", "SUM_TAG_HOURLY"):
        found.append((node, by_basis))
    if isinstance(node, Binary) and node.op == "/":
        divisor_reads_basis = any(isinstance(n, VariableRef) and n.name == "period.basis_days"
                                  for n in fm.walk(node.right))
        found.extend(_tag_calls(node.left, by_basis or divisor_reads_basis))
        found.extend(_tag_calls(node.right, by_basis))
        return found
    for child in children(node):
        found.extend(_tag_calls(child, by_basis))
    return found


class StructureEvaluator:
    """Evaluates the structures of all segments and produces the structure lines."""

    def __init__(self, ctx: CalcContext, task: tp.TaskPay | None, inputs: Sequence[Any]) -> None:
        self.ctx = ctx
        self.task = task
        self.issues: list[CalcIssue] = []
        self.tables = build_tables(ctx)
        self.inputs: dict[str, Decimal] = {}
        self.input_units: dict[str, Decimal] = {}
        self.input_sources: dict[str, list[dict[str, Any]]] = {}
        for item in inputs:
            if item.amount is not None:
                self.inputs[item.component] = self.inputs.get(item.component, ZERO) + item.amount
            if item.units is not None:
                self.input_units[item.component] = self.input_units.get(item.component, ZERO) + item.units
            self.input_sources.setdefault(item.component, []).append({"type": "wage_input", "id": item.id})
        self.prev = {code: fm.MoneyValue(v, ctx.currency) for code, v in ctx.previous.components.items()}
        self.ytd = {code: fm.MoneyValue(v, ctx.currency) for code, v in ctx.ytd.items()}
        self.plans: list[_Plan] = []
        self.ot_rate: Decimal | None = ctx.ot_hourly_rate
        self.ot_rate_trace: dict[str, Any] = {}
        self.ot_rate_error: tuple[fm.FormulaError, list[str], dict[str, str]] | None = None
        self.ot_rate_reported = False
        self.ot_units_checked = False
        self.ot_formula = self._ot_formula()

    # -- setup -------------------------------------------------------------------------------

    def _ot_formula(self) -> str:
        fields = self.ctx.policy_fields("OVERTIME_POLICY") or {}
        return fields.get("rate_formula") or DEFAULT_OT_RATE_FORMULA

    def _fail(self, plan: _Plan | None, code: str, error: fm.FormulaError, steps: Sequence[str] = (),
              references: Mapping[str, str] | None = None) -> None:
        if plan is not None:
            plan.failed.add(code)
        self.issues.append(blocking(
            "FORMULA_ERROR", f"{code} cannot be calculated: {error.message}. The employee is not paid until it is fixed.",
            component=code, error=error.to_dict(), steps=list(steps), references=dict(references or {}),
            segment=None if plan is None else plan.segment.key))

    def _env(self, plan: _Plan, code: str | None, stage: int = STAGE_EARNINGS, expect: str = "AMOUNT") -> fm.TypeEnv:
        return fm.TypeEnv(self.ctx.currency, components=plan.env_components, tags=plan.tags, tables=self.tables,
                          attributes=self._attribute_types(), component=code, stage=stage, expect=expect)

    def _monthly_on_short_period(self, plan: _Plan, code: str) -> None:
        """I-20: a formula reading ``ctc.monthly`` on a non-monthly calendar pays the monthly figure every period."""
        if any(i.code == "CTC_MONTHLY_ON_NON_MONTHLY_PERIOD" and i.component == code for i in self.issues):
            return
        period = self.ctx.period
        self.issues.append(warning(
            "CTC_MONTHLY_ON_NON_MONTHLY_PERIOD", f"{code} reads ctc.monthly, a monthly figure, but this pay period is "
            f"{period.frequency} ({period.periods_per_year} periods a year), so every period pays the monthly value. "
            f"Use ctc.period (ctc.annual ÷ {period.periods_per_year}) for the amount of one pay period.",
            component=code, frequency=period.frequency, periods_per_year=period.periods_per_year,
            segment=plan.segment.key))

    def _attribute_types(self) -> dict[str, fm.FType]:
        result = {}
        for name, raw in self.ctx.employee.attributes.items():
            if isinstance(raw, bool):
                result[name] = fm.FType(fm.Kind.BOOLEAN)
            elif isinstance(raw, (int, Decimal)):
                result[name] = fm.FType(fm.Kind.DECIMAL)
            else:
                result[name] = fm.FType(fm.Kind.STRING)
        return result

    def _formula_text(self, plan: _Plan, line: StructureLine) -> str | tuple[str, str] | None:
        params = dict(line.params)
        method = line.method
        if method == "FIXED" or method == "SYSTEM":
            return None
        if method == "PERCENT_OF":
            percent = Decimal(str(params.get("percent")))
            return fm.method_formula(method, {"percent": percent, "bases": tuple(params.get("bases") or ()),
                                              "tag": params.get("tag")})
        if method == "RATE_X_UNITS":
            rate, units = params.get("rate"), params.get("units")
            fm.method_formula(method, {"rate": rate, "units": units})  # validates both parts
            return (fm.render(fm.parse(str(rate))), fm.render(fm.parse(str(units))))
        if method == "INPUT":
            return fm.method_formula(method, {"code": params.get("code") or line.component})
        if method == "BALANCING":
            # I-20: the balancing total is the pay basis for THIS period; on a non-monthly calendar that is
            # ctc.annual ÷ periods per year, not the monthly figure every period.
            total = params.get("total") or ("ctc.monthly" if monthly_calendar(self.ctx) else "ctc.period")
            others = list(params.get("others") or [
                other.component for other in plan.compensation.lines
                if other.component != line.component and other.method not in ("BALANCING", "SYSTEM", "INPUT")
                and self.ctx.has_tag(other.component, "PART_OF_CTC")])
            if not others:
                return fm.render(fm.parse(str(total)))
            return fm.method_formula(method, {"others": others, "total": total, "own": line.component})
        if method == "SLAB":
            return fm.method_formula(method, {"table": params.get("table"), "base": params.get("base"),
                                              "progressive": params.get("progressive", True)})
        return fm.method_formula(method, {"formula": params.get("formula")})

    def _plan(self, segment: Segment) -> _Plan:
        compensation = segment.compensation
        assert compensation is not None
        ctx = self.ctx
        codes = [line.component for line in compensation.lines if line.method != "SYSTEM"]
        sigs: dict[str, fm.ComponentSig] = {}
        for code in sorted(ctx.snapshot.components):
            component = ctx.snapshot.components[code]
            if code not in codes and component.stage <= STAGE_EARNINGS:
                sigs[code] = fm.ComponentSig(ctx.currency, stage=component.stage, optional=True)
        for code in codes:
            sigs[code] = fm.ComponentSig(ctx.currency, stage=component_or_default(ctx, code).stage)
        tags: dict[str, list[str]] = {tag: [] for tag in STANDARD_TAGS}
        for component in ctx.snapshot.components.values():
            for tag in component.tags:
                tags.setdefault(tag, [])
        for code in codes:
            for tag in component_or_default(ctx, code).tags:
                tags.setdefault(tag, []).append(code)
        plan = _Plan(segment, compensation, sigs, {k: sorted(v) for k, v in sorted(tags.items())})
        plan.rate_units = {line.component: rate_unit(line) for line in compensation.lines if line.method != "SYSTEM"}
        deps: dict[str, set[str]] = {}
        stage_of = {code: sigs[code].stage or STAGE_EARNINGS for code in codes}
        ot_deps: set[str] | None = None
        for line in compensation.lines:
            code = line.component
            if line.method == "SYSTEM":
                continue
            if ctx.component(code) is None:
                self._fail(plan, code, fm.FormulaError(fm.ErrorCode.UNKNOWN_REFERENCE,
                                                       f"{code} is not in the component catalogue of the snapshot"))
                deps[code] = set()
                continue
            try:
                text = self._formula_text(plan, line)
                if text is None:
                    deps[code] = set()
                    continue
                parts = text if isinstance(text, tuple) else (text,)
                found: set[str] = set()
                exprs = []
                for part in parts:
                    analysis = fm.check(part, self._env(plan, code, stage_of[code],
                                                        "ANY" if isinstance(text, tuple) else "AMOUNT"))
                    found |= set(analysis.dependencies)
                    if "ot.hourly_rate" in analysis.variables:
                        if ot_deps is None:
                            ot_deps = set(fm.check(self.ot_formula, self._env(plan, None)).dependencies)
                        found |= ot_deps
                    exprs.append(analysis.expr)
                    if "ctc.monthly" in analysis.variables and not monthly_calendar(ctx):
                        self._monthly_on_short_period(plan, code)
                plan.exprs[code] = tuple(exprs) if isinstance(text, tuple) else exprs[0]
                plan.sources[code] = " × ".join(f"({p})" for p in parts) if isinstance(text, tuple) else text
                deps[code] = found - {code} if code not in found else found
            except fm.FormulaError as error:
                self._fail(plan, code, error)
                deps[code] = set()
        plan.deps = deps
        plan.order = self._order(plan, deps, stage_of)
        return plan

    def _order(self, plan: _Plan, deps: dict[str, set[str]], stage_of: Mapping[str, int]) -> list[str]:
        """Dependency order; a cycle or a bad reference fails the components on its path, the rest still run."""
        graph = {code: set(reads) for code, reads in deps.items()}
        for _ in range(len(graph) + 1):
            try:
                order = fm.dependency_order(graph, {c: stage_of[c] for c in graph})
                return [c for c in order if c in graph]
            except fm.FormulaError as error:
                path = [c for c in (error.path or []) if c in graph] or sorted(graph)
                self.issues.append(blocking(
                    "FORMULA_ERROR", f"{path[0]} cannot be calculated: {error.message}. The employee is not paid "
                    "until it is fixed.", component=path[0], error=error.to_dict(), segment=plan.segment.key))
                for code in set(path):
                    plan.failed.add(code)
                    graph[code] = set()
                for code, reads in graph.items():
                    graph[code] = {r for r in reads if r in graph}
        raise AssertionError("dependency ordering did not converge")  # pragma: no cover

    # -- evaluation --------------------------------------------------------------------------

    def _eval_ctx(self, plan: _Plan, code: str | None, components: Mapping[str, Decimal],
                  variables: Mapping[str, Any], stage: int = STAGE_EARNINGS, expect: str = "AMOUNT",
                  proration: fm.Proration | None = None) -> fm.EvalContext:
        currency = self.ctx.currency
        return fm.EvalContext(
            self._env(plan, code, stage, expect),
            components={k: fm.MoneyValue(v, currency) for k, v in components.items()},
            rates={k: fm.MoneyValue(v, currency) for k, v in plan.unit_rates.items()},
            variables=variables,
            inputs={k: fm.MoneyValue(v, currency) for k, v in self.inputs.items()},
            units=dict(self.input_units), prev=self.prev, ytd=self.ytd, tables=self.tables, proration=proration,
            minor_units=dict(self.ctx.minor_units), rate_units=dict(plan.rate_units))

    def ensure_ot_rate(self, plan: _Plan, variables: dict[str, Any], *, report: bool) -> None:
        """``ot.hourly_rate`` from OVERTIME_POLICY.rate_formula over the full-period rates (3.4 §9.4).

        A failure becomes a BLOCKING issue only when something needs the rate (``report``/``report_ot_rate``).
        """
        if self.ot_rate is None and self.ot_rate_error is None:
            result = fm.evaluate(fm.parse(self.ot_formula), self._eval_ctx(plan, None, plan.rates, variables))
            error = result.error
            rate = None
            if error is None:
                try:
                    rate = money_amount(result.value, self.ctx.currency, "the overtime hourly rate")
                except fm.FormulaError as exc:
                    error = exc
            if error is not None:
                self.ot_rate_error = (error, list(result.steps), dict(result.references))
            else:
                self.ot_rate = rate
                self.ot_rate_trace = {"formula": self.ot_formula, "steps": result.steps,
                                      "references": result.references, "rate": format(rate, "f")}
                self.check_ot_units(plan)
        if report:
            self.report_ot_rate()
        if self.ot_rate is not None and "ot.hourly_rate" in variables:
            variables["ot.hourly_rate"] = fm.MoneyValue(self.ot_rate, self.ctx.currency)

    def check_ot_units(self, plan: _Plan) -> None:
        """Warn when the overtime rate formula reads tagged rates whose units it cannot convert to hours.

        ``SUM_TAG_RATE(tag)`` adds the rates as they are: a tag mixing period amounts with per-hour or per-day
        rates, or a per-unit rate divided by ``period.basis_days`` (the 3.1 §5.5 formula on an hourly worker,
        48.00/h ÷ 31 ÷ 8 = 0.19), gives a wrong hourly rate. ``SUM_TAG_HOURLY(tag)`` converts PERIOD, DAY and
        HOUR rates and leaves out any other unit (pieces, wage-input units), which is reported too.
        """
        if self.ot_units_checked:
            return
        self.ot_units_checked = True
        try:
            expr = fm.parse(self.ot_formula)
        except fm.FormulaError:
            return
        calls: dict[tuple[str, str], bool] = {}  # (function, tag) -> divided by period.basis_days
        for node, by_basis in _tag_calls(expr):
            tag = getattr(node.args[0], "value", None)
            if isinstance(tag, str):
                calls[(node.name, tag)] = calls.get((node.name, tag), False) or by_basis
        for (name, tag), reads_basis in calls.items():
            units = {code: plan.rate_units.get(code, RATE_UNIT_PERIOD) for code in plan.tags.get(tag, ())
                     if code in plan.unit_rates and code not in plan.skipped and code not in plan.failed}
            shown = ", ".join(f"{code} per {unit.lower()}" for code, unit in sorted(units.items()))
            if name == "SUM_TAG_HOURLY":
                left_out = sorted(code for code, unit in units.items() if unit not in HOURLY_CONVERTIBLE_UNITS)
                if left_out:
                    self.issues.append(warning(
                        "OT_BASE_UNIT_UNKNOWN", f"The overtime hourly rate leaves out {', '.join(left_out)}: "
                        f'SUM_TAG_HOURLY("{tag}") cannot convert a rate that is not per period, day or hour to an '
                        "hourly rate. Remove the tag from those components or change "
                        "OVERTIME_POLICY.rate_formula.", tag=tag, units=units, left_out=left_out,
                        segment=plan.segment.key))
                continue
            kinds = set(units.values())
            if len(kinds) > 1 or (kinds - {RATE_UNIT_PERIOD} and reads_basis):
                divides = " and divides them by period.basis_days" if reads_basis else ""
                self.issues.append(warning(
                    "OT_BASE_UNITS_MIXED", f'OVERTIME_POLICY.rate_formula reads SUM_TAG_RATE("{tag}"), which adds '
                    f"the rates as they are ({shown}){divides}; the overtime hourly rate is likely wrong. "
                    f'SUM_TAG_HOURLY("{tag}") converts each rate to an hourly rate.', tag=tag, units=units,
                    formula=self.ot_formula, segment=plan.segment.key))

    def report_ot_rate(self) -> None:
        if self.ot_rate_error is not None and not self.ot_rate_reported:
            self.ot_rate_reported = True
            error, steps, references = self.ot_rate_error
            self._fail(None, "OT_HOURLY_RATE", error, steps, references)

    def _included(self, plan: _Plan, code: str, variables: Mapping[str, Any]) -> bool:
        component = component_or_default(self.ctx, code)
        period = self.ctx.period
        if component.frequency == "SPECIFIC_MONTHS" and period.end.month not in component.months:
            plan.skipped.add(code)
            return False
        if component.frequency == "ON_EXIT" and not variables.get("emp.is_leaver"):
            plan.skipped.add(code)
            return False
        if component.frequency == "ONE_TIME" and (self.ctx.ytd.get(code) or any(
                v != 0 for v in self.ctx.previous.history.get(code, ()))):
            plan.skipped.add(code)
            return False
        if component.eligibility:
            try:
                expr = fm.check(component.eligibility, self._env(plan, code, expect="BOOLEAN")).expr
            except fm.FormulaError as error:
                self._fail(plan, code, error)
                return False
            result = fm.evaluate(expr, self._eval_ctx(plan, code, plan.rates, variables, expect="BOOLEAN"))
            if result.error is not None:
                self._fail(plan, code, result.error, result.steps, result.references)
                return False
            if result.value is not True:
                plan.skipped.add(code)
                plan.notes.setdefault(code, []).append("not eligible")
                return False
        return True

    def rate_pass(self, plan: _Plan, variables: dict[str, Any]) -> None:
        ctx = self.ctx
        lines = {line.component: line for line in plan.compensation.lines}
        for code in plan.order:
            if code in plan.failed:
                continue
            blocked = [d for d in plan.deps.get(code, ()) if d in plan.failed]
            if blocked:
                plan.failed.add(code)
                self.issues.append(blocking("DEPENDS_ON_FAILED", f"{code} is not calculated because it reads "
                                            f"{', '.join(sorted(blocked))}, which failed.", component=code,
                                            segment=plan.segment.key))
                continue
            if not self._included(plan, code, variables):
                plan.rates[code] = ZERO
                plan.unit_rates[code] = ZERO
                continue
            line = lines[code]
            component = component_or_default(ctx, code)
            expr = plan.exprs.get(code)
            if "ot.hourly_rate" in plan.sources.get(code, ""):
                self.ensure_ot_rate(plan, variables, report=True)
            if line.method == "FIXED":
                with localcontext(CALC_CONTEXT):
                    value = line.amount if line.amount is not None else line.annual_amount / ctx.period.periods_per_year
                plan.steps[code] = [f"fixed {format(value, 'f')} {ctx.currency}" if line.amount is not None else
                                    f"{format(line.annual_amount, 'f')} ÷ {ctx.period.periods_per_year} = "
                                    f"{format(value, 'f')} {ctx.currency}"]
                unit_rate = value
            elif line.method == "RATE_X_UNITS":
                rate_expr, units_expr = expr
                rate_result = fm.evaluate(rate_expr, self._eval_ctx(plan, code, plan.rates, variables))
                units_result = fm.evaluate(units_expr, self._eval_ctx(plan, code, plan.rates, variables, expect="NUMBER"))
                failure = rate_result if rate_result.error is not None else units_result
                if failure.error is not None:
                    self._fail(plan, code, failure.error, failure.steps, failure.references)
                    continue
                try:
                    unit_rate = money_amount(rate_result.value, ctx.currency, f"the rate of {code}")
                    units = units_result.value if isinstance(units_result.value, Decimal) else money_amount(
                        units_result.value, ctx.currency, f"the units of {code}")
                except fm.FormulaError as error:
                    self._fail(plan, code, error)
                    continue
                plan.units[code] = units
                with localcontext(CALC_CONTEXT):
                    value = unit_rate * units
                plan.steps[code] = rate_result.steps + units_result.steps
                plan.references[code] = {**rate_result.references, **units_result.references}
            else:
                result = fm.evaluate(expr, self._eval_ctx(plan, code, plan.rates, variables))
                if result.error is not None:
                    self._fail(plan, code, result.error, result.steps, result.references)
                    continue
                try:
                    value = money_amount(result.value, ctx.currency, code)
                except fm.FormulaError as error:
                    self._fail(plan, code, error, result.steps, result.references)
                    continue
                plan.steps[code] = list(result.steps)
                plan.references[code] = dict(result.references)
                if line.method == "BALANCING":
                    employer = plan.compensation.ctc_employer_contributions
                    if employer:
                        # The contributions are monthly amounts (snapshot contract); on a non-monthly calendar
                        # the balancing total is one period's CTC, so they are converted the same way (I-20).
                        if not monthly_calendar(ctx):
                            with localcontext(CALC_CONTEXT):
                                employer = {k: v * 12 / ctx.period.periods_per_year for k, v in employer.items()}
                        with localcontext(CALC_CONTEXT):
                            value = value - sum(employer.values(), ZERO)
                        per = "" if monthly_calendar(ctx) else f" (monthly × 12 ÷ {ctx.period.periods_per_year})"
                        plan.notes.setdefault(code, []).append(
                            "minus employer contributions inside CTC" + per + ": " + ", ".join(
                                f"{k} {format(v, 'f')}" for k, v in employer.items()))
                unit_rate = value
            if component.cap is not None and value > component.cap:
                plan.notes.setdefault(code, []).append(f"capped at {money_text(component.cap, ctx.minor_unit)}")
                value = component.cap
            if line.method != "RATE_X_UNITS":
                value = round_amount(ctx, value, rounding_mode(ctx, component))
                unit_rate = value
            floor = component.floor if component.floor is not None else (ZERO if line.method == "BALANCING" else None)
            if floor is not None:
                error = fm.check_floor(value, floor, component=code, minor_units=dict(ctx.minor_units))
                if error is not None:
                    self._fail(plan, code, error, plan.steps.get(code, ()), plan.references.get(code))
                    continue
            plan.rates[code] = value
            plan.unit_rates[code] = unit_rate

    def evaluate(self) -> None:
        ctx = self.ctx
        ledger = ctx.ledger
        for segment in ctx.segments:
            if segment.compensation is None:
                continue
            plan = self._plan(segment)
            self.plans.append(plan)
            self.rate_pass(plan, variables(ctx, ledger.totals, compensation=segment.compensation, task=self.task))

    # -- amounts and lines --------------------------------------------------------------------

    def lines(self) -> list[Line]:
        ctx = self.ctx
        result: list[Line] = []
        if not self.plans:
            return result
        codes: list[str] = []
        for plan in reversed(self.plans):
            for code in plan.order:
                if code not in codes:
                    codes.append(code)
        amounts: dict[str, Decimal] = {}
        deferred: list[str] = []
        for code in codes:
            owner = self._owner(code)
            if owner is None:
                continue
            line = owner.compensation.line(code)
            kind = self._proration_kind(code, line)
            if line.method == "RATE_X_UNITS":
                result.extend(self._rate_x_units_lines(code))
                amounts[code] = sum((ln.amount for ln in result if ln.component == code), ZERO)
            elif kind is ProrationKind.NONE:
                deferred.append(code)
            else:
                produced = self._prorated_lines(code, kind)
                result.extend(produced)
                amounts[code] = sum((ln.amount for ln in produced), ZERO)
        for code in deferred:
            line = self._unprorated_line(code, amounts)
            if line is not None:
                result.append(line)
                amounts[code] = line.amount
        return result

    def _owner(self, code: str) -> _Plan | None:
        for plan in reversed(self.plans):
            if code in plan.rates and code not in plan.failed and code not in plan.skipped:
                return plan
        return None

    def _proration_kind(self, code: str, line: StructureLine) -> ProrationKind:
        component = component_or_default(self.ctx, code)
        if line.method in ("INPUT", "RATE_X_UNITS"):
            return ProrationKind.NONE
        kind = ProrationKind(component.proration)
        lop_tag = (self.ctx.policy_fields("ATTENDANCE_PAY_POLICY") or {}).get("lop_component_tag") or "LOP_APPLICABLE"
        if kind is ProrationKind.BY_PAID_DAYS and lop_tag not in component.tags:
            return ProrationKind.BY_EMPLOYMENT_DAYS
        return kind

    def _segment_rates(self, code: str) -> tuple[list[Decimal], list[_Plan | None]]:
        by_key = {plan.segment.key: plan for plan in self.plans}
        rates: list[Decimal] = []
        plans: list[_Plan | None] = []
        for key in self.ctx.segment_keys:
            plan = by_key.get(key)
            ok = plan is not None and code in plan.rates and code not in plan.failed
            rates.append(plan.rates[code] if ok else ZERO)
            plans.append(plan if ok else None)
        return rates, plans

    def _prorated_lines(self, code: str, kind: ProrationKind) -> list[Line]:
        ctx = self.ctx
        component = component_or_default(ctx, code)
        basis = component.proration_basis or ctx.setting("payroll.lop.proration_basis", "CALENDAR_DAYS")
        rates, plans = self._segment_rates(code)
        zero_pay = ctx.ledger.guard.pay_zero and self._lop_tagged(component)
        try:
            results = prorate_segments(rates, basis, kind, ctx.period_facts, ctx.segment_facts,
                                       custom_days=ctx.setting("payroll.lop.custom_days"), zero_pay=zero_pay)
        except ProrationError as error:
            self.issues.append(blocking("PRORATION_ERROR", f"{code} cannot be prorated: {error.message}",
                                        component=code, error_code=error.code, basis=basis))
            return []
        mode = rounding_mode(ctx, component)
        rounded = round_segments([amount for amount, _ in results], ctx.quantum, ROUNDINGS[mode])
        split = sum(1 for plan in plans if plan is not None) > 1 or (len(plans) > 1 and plans[-1] is None)
        lines: list[Line] = []
        for index, ((unrounded, explanation), amount, plan) in enumerate(zip(results, rounded, plans)):
            if plan is None:
                continue
            key = ctx.segment_keys[index]
            totals = ctx.ledger.segment(key)
            text = self._proration_text(code, explanation, totals, key, split, kind, basis, zero_pay)
            trace = self._trace(plan, code, rate=explanation.rate, unrounded=unrounded, mode=mode, amount=amount,
                                proration=explanation.to_dict())
            lines.append(make_line(ctx, component, amount, explanation=text, trace=trace, units=explanation.paid_units,
                                   rate=explanation.rate, sources=self._sources(plan, code, totals, key),
                                   segment=index if len(ctx.segment_keys) > 1 else None,
                                   sort_order=self._sort(plan, code)))
        return lines

    def _lop_tagged(self, component: ComponentDef) -> bool:
        lop_tag = (self.ctx.policy_fields("ATTENDANCE_PAY_POLICY") or {}).get("lop_component_tag") or "LOP_APPLICABLE"
        return lop_tag in component.tags

    def _proration_text(self, code: str, explanation: Any, totals: LedgerTotals, key: str, split: bool,
                        kind: ProrationKind, basis: str, zero_pay: bool) -> str:
        ctx = self.ctx
        rate = money_text(explanation.rate, ctx.minor_unit)
        if zero_pay:
            return f"{rate} × 0: no paid working days (payroll.lop.zero_paid_days = PAY_ZERO)"
        n, u = explanation.basis_days, explanation.paid_units
        if n is None or u is None:
            return f"{rate} (not prorated)"
        hours = " hours" if Basis(explanation.basis or basis) is Basis.HOURS else ""
        if u == n and not split:
            text = f"{rate} × {qty_text(u)} ÷ {qty_text(n)}{hours} (full period)"
        else:
            text = f"{rate} × {qty_text(u)} ÷ {qty_text(n)}{hours}"
            if explanation.capped:
                text = f"min({rate}, {text})"
        dates = [d.date for d in ctx.ledger.days if d.segment == key and d.employed]
        where = f" for {date_ranges(dates)}" if split and dates else ""
        note = ""
        if kind is not ProrationKind.BY_EMPLOYMENT_DAYS and Basis(explanation.basis or basis) is not Basis.HOURS:
            lop = totals.lop_days_on_working_days if explanation.basis == "WORKING_DAYS" else totals.lop_days
            slot_lop = lop - totals.penalty_lop_days
            note = lop_note(slot_lop, ctx.ledger.lop_dates(key))
            if totals.penalty_lop_days > 0:
                penalty = plural(totals.penalty_lop_days, "penalty LOP day")
                note = f"{note[:-1]}; {penalty})" if note else f" ({penalty})"
        return f"{text}{where}{note}"

    def _trace(self, plan: _Plan, code: str, *, rate: Decimal, unrounded: Decimal, mode: str, amount: Decimal,
               proration: Mapping[str, Any] | None = None, units: Decimal | None = None) -> dict[str, Any]:
        line = plan.compensation.line(code)
        component = component_or_default(self.ctx, code)
        trace: dict[str, Any] = {
            "component": code, "component_version": component.version, "kind": component.kind,
            "stage": STAGE_EARNINGS, "method": line.method if line else component.calc_method,
            "rate": money_json(rate, self.ctx.currency), "proration": proration,
            "unrounded": format(unrounded, "f"), "rounding": rounding_trace("LINE", mode, self.ctx.quantum),
            "amount": money_json(amount, self.ctx.currency), "segment": plan.segment.key,
        }
        if code in plan.sources:
            trace["formula"] = plan.sources[code]
        if plan.steps.get(code):
            trace["steps"] = plan.steps[code]
        if plan.references.get(code):
            trace["references"] = plan.references[code]
        if units is not None:
            trace["units"] = format(units, "f")
        if plan.notes.get(code):
            trace["notes"] = plan.notes[code]
        if code == "OT_HOURLY_RATE" or (self.ot_rate_trace and "ot.hourly_rate" in plan.sources.get(code, "")):
            trace["ot_hourly_rate"] = self.ot_rate_trace
        return trace

    def _sources(self, plan: _Plan, code: str, totals: LedgerTotals | None, key: str | None) -> list[dict[str, Any]]:
        compensation = plan.compensation
        sources: list[dict[str, Any]] = [{"type": "compensation_assignment", "id": compensation.id,
                                          "version": compensation.version,
                                          "structure_version_id": compensation.structure_version_id}]
        if totals is not None and totals.lop_days > 0:
            sources.append({"type": "day_ledger", "segment": key, "lop_days": format(totals.lop_days, "f"),
                            "dates": [d.isoformat() for d in self.ctx.ledger.lop_dates(key)]})
        sources.extend(self.input_sources.get(code, []) if compensation.line(code).method == "INPUT" else [])
        return sources

    def _sort(self, plan: _Plan, code: str) -> int:
        component = self.ctx.component(code)
        if component is not None and component.sort_order:
            return component.sort_order
        line = plan.compensation.line(code)
        return line.order if line is not None else 0

    def _rate_x_units_lines(self, code: str) -> list[Line]:
        ctx = self.ctx
        component = component_or_default(ctx, code)
        mode = rounding_mode(ctx, component)
        lines: list[Line] = []
        plans = [plan for plan in self.plans if code in plan.rates and code not in plan.failed
                 and code not in plan.skipped]
        split = len(plans) > 1
        for plan in plans:
            if split:
                totals = ctx.ledger.segment(plan.segment.key)
                seg_vars = variables(ctx, totals, compensation=plan.compensation, task=self.task)
                expr = plan.exprs[code]
                units_result = fm.evaluate(expr[1], self._eval_ctx(plan, code, plan.rates, seg_vars, expect="NUMBER"))
                if units_result.error is not None:
                    self._fail(plan, code, units_result.error, units_result.steps, units_result.references)
                    continue
                units = units_result.value if isinstance(units_result.value, Decimal) else Decimal(0)
            else:
                units = plan.units[code]
            rate = plan.unit_rates[code]
            with localcontext(CALC_CONTEXT):
                unrounded = rate * units
            if component.cap is not None and unrounded > component.cap:
                unrounded = component.cap
            amount = round_amount(ctx, unrounded, mode)
            unit_word = self._unit_word(plan, code)
            text = f"{rate_text(rate, ctx.minor_unit)} × {qty_text(units)}{unit_word}"
            if plan.notes.get(code):
                text += f" ({'; '.join(plan.notes[code])})"
            if split:
                dates = [d.date for d in ctx.ledger.days if d.segment == plan.segment.key and d.employed]
                text += f" for {date_ranges(dates)}"
            index = ctx.segment_keys.index(plan.segment.key) if plan.segment.key in ctx.segment_keys else None
            lines.append(make_line(ctx, component, amount, explanation=text,
                                   trace=self._trace(plan, code, rate=rate, unrounded=unrounded, mode=mode,
                                                     amount=amount, units=units),
                                   units=units, rate=rate, sources=self._sources(plan, code, None, None),
                                   segment=index if len(ctx.segment_keys) > 1 else None,
                                   sort_order=self._sort(plan, code)))
        return lines

    @staticmethod
    def _unit_word(plan: _Plan, code: str) -> str:
        source = plan.sources.get(code, "")
        if "hours" in source:
            return " hours"
        if "days" in source:
            return " days"
        return ""

    def _unprorated_line(self, code: str, amounts: Mapping[str, Decimal]) -> Line | None:
        ctx = self.ctx
        plan = self._owner(code)
        if plan is None:
            return None
        component = component_or_default(ctx, code)
        line = plan.compensation.line(code)
        mode = rounding_mode(ctx, component)
        if line.method == "INPUT" and (line.params.get("code") or code) not in self.inputs:
            return None
        if line.method in ("FIXED", "INPUT"):
            unrounded = plan.rates[code]
            text = (f"Approved input {money_text(unrounded, ctx.minor_unit)}" if line.method == "INPUT"
                    else f"Fixed {money_text(unrounded, ctx.minor_unit)}")
            steps = plan.steps.get(code, [])
        else:
            totals = ctx.ledger.totals
            seg_vars = variables(ctx, totals, compensation=plan.compensation, task=self.task)
            if "ot.hourly_rate" in plan.sources.get(code, ""):
                self.ensure_ot_rate(plan, seg_vars, report=True)
            proration = None
            n = period_basis(ctx, component.proration_basis)
            if n is not None:
                proration = fm.Proration(totals.paid_days_after_penalty, n)
            result = fm.evaluate(plan.exprs[code], self._eval_ctx(plan, code, amounts, seg_vars, proration=proration))
            if result.error is not None:
                self._fail(plan, code, result.error, result.steps, result.references)
                return None
            try:
                unrounded = money_amount(result.value, ctx.currency, code)
            except fm.FormulaError as error:
                self._fail(plan, code, error, result.steps, result.references)
                return None
            if component.cap is not None and unrounded > component.cap:
                unrounded = component.cap
                plan.notes.setdefault(code, []).append(f"capped at {money_text(component.cap, ctx.minor_unit)}")
            floor = component.floor if component.floor is not None else (ZERO if line.method == "BALANCING" else None)
            if floor is not None:
                error = fm.check_floor(unrounded, floor, component=code, minor_units=dict(ctx.minor_units))
                if error is not None:
                    self._fail(plan, code, error, result.steps, result.references)
                    return None
            steps = list(result.steps)
            plan.steps[code] = steps
            plan.references[code] = dict(result.references)
            text = f"{plan.sources[code]} = {money_text(unrounded, ctx.minor_unit)}"
        amount = round_amount(ctx, unrounded, mode)
        if plan.notes.get(code):
            text += f" ({'; '.join(plan.notes[code])})"
        return make_line(ctx, component, amount, explanation=text,
                         trace=self._trace(plan, code, rate=plan.rates[code], unrounded=unrounded, mode=mode,
                                           amount=amount, proration={"method": "NONE"}),
                         rate=plan.unit_rates[code], sources=self._sources(plan, code, None, None),
                         sort_order=self._sort(plan, code))

    def structure_codes(self) -> set[str]:
        """Components the structures define with their own method (they replace engine-made lines)."""
        return {line.component for plan in self.plans for line in plan.compensation.lines if line.method != "SYSTEM"}

    def period_end_rates(self) -> dict[str, Decimal]:
        return dict(self.plans[-1].unit_rates) if self.plans else {}

    def final_plan(self) -> _Plan | None:
        return self.plans[-1] if self.plans else None


# --------------------------------------------------------------------------- time-based pay (3.4 §8-§12)


def holiday_premium_result(ctx: CalcContext, hourly_rate: Decimal | None) -> tp.HolidayPremiums | None:
    """Work on holidays and weekly offs split between premiums, comp-off and overtime (§8, §9.5; I-16, F-05)."""
    work = ctx.snapshot.non_working_day_work
    if not work:
        return None
    return tp.holiday_premiums(
        work, ctx.policy_fields("HOLIDAY_PAY_POLICY") or {}, hourly_rate=hourly_rate,
        standard_hours=standard_hours(ctx), currency=ctx.currency,
        holiday_pay_enabled=ctx.switch_ranges("payroll.holiday_work_premium.enabled"),
        weekly_off_pay_enabled=ctx.switch_ranges("payroll.weekly_off_pay.enabled"),
        overtime_enabled=ctx.switch_ranges("payroll.overtime.enabled"), rounding=ctx.line_mode)


def task_result(ctx: CalcContext) -> tp.TaskPay | None:
    tasks = ctx.snapshot.tasks
    if not tasks.records and not tasks.carried_in:
        return None
    if not ctx.switch_any("payroll.task_pay.enabled"):
        return None
    policy = dict(ctx.policy_fields("TASK_PAY_POLICY") or {})
    task_based = any(s.compensation is not None and s.compensation.structure_type == "TASK_BASED" for s in ctx.segments)
    minimum = None
    if task_based:
        minimum = tp.MinimumWageInput(ctx.ledger.totals.worked_hours, tasks.min_hourly_rate, tasks.min_wage_verified,
                                      tasks.pack_forces_top_up)
    else:
        policy["minimum_wage_floor"] = "OFF"  # task pay never replaces contracted base pay (EC-TSK-13)
    rates = {str(k): Decimal(str(v)) for k, v in (ctx.fx.get("rates") or {}).items()}
    return tp.task_pay(
        tasks.records, policy, employee_id=str(ctx.employee.employee_id), period_start=ctx.period.start,
        period_end=ctx.period.end, currency=ctx.currency, enabled=ctx.switch_ranges("payroll.task_pay.enabled"),
        rate_card=tasks.rate_card, employee=tp.EmployeeAttributes(ctx.employee.grade, tasks.skill, tasks.location),
        fx_rates=rates, carried_in=tasks.carried_in, minimum_wage=minimum, worker_class=ctx.employee.worker_class)


def _guarded(issues: list[CalcIssue], what: str, component: str | None, build: Any) -> Any:
    """Run a time_pay call; configuration or input errors become a BLOCKING issue instead of a silent zero."""
    try:
        return build()
    except tp.TimePayError as error:
        issues.append(blocking("CONFIG_ERROR", f"{what}: {error.message}", component=component,
                               error_code=error.code, field=error.field))
        return None


def time_based_lines(ctx: CalcContext, evaluator: StructureEvaluator, task: tp.TaskPay | None,
                     issues: list[CalcIssue]) -> list[Line]:
    lines: list[Line] = []
    own = evaluator.structure_codes()
    plan = evaluator.final_plan()
    variables_now = variables(ctx, ctx.ledger.totals, compensation=plan.compensation if plan else None, task=task)
    ot = ctx.overtime
    needs_rate = (ot is not None and ot.paid_minutes) or ctx.snapshot.non_working_day_work or ctx.snapshot.shifts
    if needs_rate and plan is not None:
        evaluator.ensure_ot_rate(plan, variables_now, report=False)
    rate = evaluator.ot_rate
    rate_trace = {"ot_hourly_rate": evaluator.ot_rate_trace} if evaluator.ot_rate_trace else {}

    if ot is not None and ot.paid_minutes:
        if rate is None:
            evaluator.report_ot_rate()
            issues.append(blocking("OT_RATE_MISSING", "Overtime is approved but the overtime hourly rate could not be "
                                   "calculated.", component="OT_WORKDAY"))
        else:
            paid = _guarded(issues, "Overtime pay", "OT_WORKDAY",
                            lambda: tp.overtime_pay(ot, rate, ctx.currency, rounding=ctx.line_mode))
            prefix = _rate_prefix(ctx, evaluator)
            for item in paid.lines if paid else ():
                if item.component not in own:
                    line = time_pay_line(ctx, item, extra_trace=rate_trace)
                    lines.append(replace(line, explanation=prefix + line.explanation))
    premiums = _guarded(issues, "Holiday and weekly-off work", "HOLIDAY_PREMIUM",
                        lambda: holiday_premium_result(ctx, rate))
    if premiums is None and rate is None and ctx.snapshot.non_working_day_work:
        evaluator.report_ot_rate()
    if premiums is not None:
        lines.extend(time_pay_line(ctx, item, extra_trace={
            **rate_trace, "comp_off": [plain_trace(g.__dict__) for g in premiums.comp_off]})
            for item in premiums.lines if item.component not in own)
        issues.extend(from_time_pay_issue(i, "HOLIDAY_PREMIUM") for i in premiums.issues)
    totals = ctx.ledger.totals
    policy = ctx.policy_fields("HOLIDAY_PAY_POLICY") or {}
    if totals.extra_day_pay_days > 0 and plan is not None:
        lines.extend(_extra_day_pay(ctx, evaluator, issues, policy))
    if not ctx.employee.salaried and policy.get("non_salaried_holiday_pay") == "AVERAGE_DAILY_EARNINGS":
        lines.extend(_average_earnings_holidays(ctx, issues, policy, own))
    if ctx.snapshot.shifts and ctx.switch_any("payroll.shift_allowance.enabled"):
        result = _guarded(issues, "Shift allowance", "SHIFT_ALLOWANCE", lambda: tp.shift_allowance(
            ctx.snapshot.shifts, ctx.policy_fields("SHIFT_ALLOWANCE_POLICY") or {}, currency=ctx.currency,
            hourly_rate=rate, enabled=ctx.switch_ranges("payroll.shift_allowance.enabled"), rounding=ctx.line_mode))
        if result is not None:
            lines.extend(time_pay_line(ctx, item) for item in result.lines if item.component not in own)
    if task is not None:
        lines.extend(time_pay_line(ctx, item, extra_trace={"carried_out": format(task.carried_out, "f"),
                                                           "forfeited": format(task.forfeited, "f")})
                     for item in task.lines if item.component not in own)
        issues.extend(from_time_pay_issue(i, "TASK_EARNING") for i in task.issues)
    return lines


def _rate_prefix(ctx: CalcContext, evaluator: StructureEvaluator) -> str:
    """"50,000.00 ÷ 26 ÷ 8 = 240.3846…; " when the overtime rate formula reads full-period rates."""
    trace = evaluator.ot_rate_trace
    if not trace or evaluator.ot_rate is None:
        return ""
    steps = trace.get("steps") or []
    refs = trace.get("references") or {}
    text = trace["formula"]
    for ref, shown in sorted(refs.items(), key=lambda kv: -len(kv[0])):
        number = shown.split(" ")[0]
        try:
            shown = money_text(Decimal(number), ctx.minor_unit) if " " in shown else qty_text(Decimal(number))
        except ArithmeticError:
            pass
        text = text.replace(ref, shown)
    text = text.replace(" / ", " ÷ ").replace(" * ", " × ")
    return f"{text} = {rate_text(evaluator.ot_rate, ctx.minor_unit)}; " if steps else ""


def _extra_day_pay(ctx: CalcContext, evaluator: StructureEvaluator, issues: list[CalcIssue],
                   policy: Mapping[str, Any]) -> list[Line]:
    """An extra day's pay for a holiday on a weekly off (EC-HOL-01): Σ LOP-applicable rates ÷ N per day."""
    rates = evaluator.period_end_rates()
    n = period_basis(ctx)
    if n is None:
        issues.append(blocking("PRORATION_ERROR", "The extra day's pay for a holiday on a weekly off needs a basis.",
                               component="HOLIDAY_PREMIUM"))
        return []
    base = sum((v for code, v in rates.items() if evaluator._lop_tagged(component_or_default(ctx, code))), ZERO)
    with localcontext(CALC_CONTEXT):
        daily = base / n
    dates = [d.date for d in ctx.ledger.days if d.holiday_on_weekly_off]
    result = _guarded(issues, "Holiday on a weekly off", "HOLIDAY_PREMIUM", lambda: tp.holiday_on_weekly_off(
        dates, policy, daily_rate=daily, currency=ctx.currency, holiday_pay_enabled=None, rounding=ctx.line_mode))
    return [time_pay_line(ctx, item, extra_trace={"daily_rate": format(daily, "f"), "basis_days": format(n, "f")})
            for item in (result.lines if result else ())]


def _average_earnings_holidays(ctx: CalcContext, issues: list[CalcIssue], policy: Mapping[str, Any],
                               own: set[str]) -> list[Line]:
    holidays = []
    for record in ctx.ledger.days:
        if not record.holiday_pay_eligible or record.holiday is None:
            continue
        kind = record.holiday.type
        holidays.append(tp.HolidayDay(record.date, "OPTIONAL" if kind is HolidayType.OPTIONAL_AVAILED else kind.value,
                                      availed=kind is HolidayType.OPTIONAL_AVAILED, ref=str(record.holiday.holiday_id)))
    if not holidays:
        return []
    fields = dict(policy)
    fields["eligibility_adjacent_days"] = "NONE"  # the day ledger already applied it (EC-HOL-03)
    result = _guarded(issues, "Holiday pay", "HOLIDAY_PAY", lambda: tp.non_salaried_holiday_pay(
        holidays, fields, hourly_rate=None, standard_hours=standard_hours(ctx), currency=ctx.currency,
        enabled=ctx.switch_ranges("payroll.holiday_pay.non_salaried.enabled"),
        average_daily_earnings=ctx.employee.average_daily_earnings, rounding=ctx.line_mode))
    if result is None:
        return []
    issues.extend(from_time_pay_issue(i, "HOLIDAY_PAY") for i in result.issues)
    return [time_pay_line(ctx, item) for item in result.lines if item.component not in own]


# --------------------------------------------------------------------------- inputs, arrears, reimbursements


def usable_inputs(ctx: CalcContext, issues: list[CalcIssue]) -> list[Any]:
    """Approved wage inputs that can be paid: known component, pay currency (no implicit FX), switch ON."""
    usable = []
    for item in ctx.snapshot.wage_inputs:
        component = ctx.component(item.component)
        if component is None:
            issues.append(blocking("UNKNOWN_COMPONENT", f"Wage input {item.id} names {item.component}, which is not in "
                                   "the component catalogue.", component=item.component, input_id=item.id))
        elif item.currency is not None and item.currency != ctx.currency:
            issues.append(blocking("CURRENCY_MISMATCH", f"Wage input {item.id} is in {item.currency}; the pay currency "
                                   f"is {ctx.currency} and nothing is converted implicitly.",
                                   component=item.component, input_id=item.id))
        elif component.kind in PAID_KINDS:
            usable.append(item)
    if usable and not ctx.setting_bool("payroll.wage_inputs.enabled"):
        issues.append(warning("WAGE_INPUTS_DISABLED", "Wage inputs are switched off; approved inputs are not paid.",
                              inputs=[i.id for i in usable]))
        return []
    return usable


def input_lines(ctx: CalcContext, inputs: Sequence[Any], own: set[str], issues: list[CalcIssue]) -> list[Line]:
    """Approved wage inputs of paid components outside the structure, one line per component (3.4 §13)."""
    grouped: dict[str, list[Any]] = {}
    for item in inputs:
        if item.component in own:
            continue
        if item.amount is None:
            issues.append(blocking("INPUT_NEEDS_RATE", f"Wage input {item.id} gives units only; {item.component} has no "
                                   "rate in the structure to multiply them by.", component=item.component,
                                   input_id=item.id))
            continue
        grouped.setdefault(item.component, []).append(item)
    lines = []
    for code in sorted(grouped):
        component = ctx.component(code)
        items = grouped[code]
        with localcontext(CALC_CONTEXT):
            total = sum((i.amount for i in items), ZERO)
        mode = rounding_mode(ctx, component)
        amount = round_amount(ctx, total, mode)
        if len(items) == 1:
            text = f"Approved input {money_text(items[0].amount, ctx.minor_unit)}"
        else:
            text = "Approved inputs: " + " + ".join(money_text(i.amount, ctx.minor_unit) for i in items)
        reasons = [i.reason for i in items if i.reason]
        if reasons:
            text += f" ({'; '.join(reasons)})"
        trace = {"component": code, "component_version": component.version, "kind": component.kind,
                 "stage": STAGE_EARNINGS, "method": "INPUT", "unrounded": format(total, "f"),
                 "rounding": rounding_trace("LINE", mode, ctx.quantum), "amount": money_json(amount, ctx.currency)}
        lines.append(make_line(ctx, component, amount, explanation=text, trace=trace,
                               sources=[{"type": "wage_input", "id": i.id, "source": i.source,
                                         "external_ref": i.external_ref, "content_hash": i.content_hash}
                                        for i in items]))
    return lines


def arrears_lines(ctx: CalcContext) -> list[Line]:
    """Positive arrears for earlier periods; each line inherits the original component's tags (3.3 §8.3)."""
    lines = []
    base = component_or_default(ctx, "ARREARS", "EARNING", "RETRO_ADJUSTMENT")
    for item in sorted(ctx.snapshot.arrears, key=lambda a: (a.period_ref, a.component, str(a.id))):
        if item.amount <= 0:
            continue
        original = ctx.component(item.component)
        tags = item.tags or (original.tags if original else ())
        mode = rounding_mode(ctx, base)
        amount = round_amount(ctx, item.amount, mode)
        start, _, end = item.period_ref.partition("..")
        text = f"Arrears of {item.component} for {_period_label(start, end)}: {money_text(amount, ctx.minor_unit)}"
        if item.reason:
            text += f" ({item.reason})"
        trace = {"component": "ARREARS", "kind": base.kind, "stage": STAGE_EARNINGS, "method": "SYSTEM",
                 "original_component": item.component, "period_ref": item.period_ref,
                 "unrounded": format(item.amount, "f"), "rounding": rounding_trace("LINE", mode, ctx.quantum),
                 "amount": money_json(amount, ctx.currency)}
        lines.append(make_line(ctx, base, amount, explanation=text, trace=trace, period_ref=item.period_ref,
                               tags=tags, sources=[{"type": "arrears", "id": item.id, "component": item.component}]))
    return lines


def _period_label(start: str, end: str) -> str:
    try:
        first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    except ValueError:
        return f"{start}..{end}"
    days = [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]
    return date_ranges(days) + f" {last.year}"


def reimbursement_lines(ctx: CalcContext, issues: list[CalcIssue]) -> list[Line]:
    claims = ctx.snapshot.reimbursements
    if not claims:
        return []
    if not ctx.setting_bool("payroll.reimbursements.enabled"):
        issues.append(warning("REIMBURSEMENTS_DISABLED", "Reimbursements are switched off; approved claims are not paid.",
                              claims=[c.id for c in claims]))
        return []
    usable = []
    for claim in claims:
        if claim.currency is not None and claim.currency != ctx.currency:
            issues.append(blocking("CURRENCY_MISMATCH", f"Claim {claim.id} is in {claim.currency}; it must be converted "
                                   f"to {ctx.currency} with an FX snapshot before payroll.", component="REIMBURSEMENT",
                                   claim_id=claim.id))
            continue
        usable.append(claim)
    route = ctx.setting("payroll.reimbursements.payout_route", "WITH_PAYROLL")
    taxability = {c.claim_type: c.taxability for c in usable if c.taxability}
    produced = _guarded(issues, "Reimbursements", "REIMBURSEMENT", lambda: tp.reimbursement_lines(
        [(str(c.id), c.claim_type, c.amount) for c in usable], ctx.currency, payout_route=route,
        taxability=taxability or None))
    return [time_pay_line(ctx, item) for item in (produced or ())]


# --------------------------------------------------------------------------- stage entry point


@dataclass(frozen=True)
class EarningsResult:
    lines: tuple[Line, ...]
    issues: tuple[CalcIssue, ...]
    rates: Mapping[str, Decimal]
    ot_hourly_rate: Decimal | None


def calculate_earnings(ctx: CalcContext) -> EarningsResult:
    """Stage 4: every EARNING and REIMBURSEMENT line of the period, with the issues found."""
    issues: list[CalcIssue] = []
    task = _guarded(issues, "Task pay", "TASK_EARNING", lambda: task_result(ctx))
    inputs = usable_inputs(ctx, issues)
    evaluator = StructureEvaluator(ctx, task, inputs)
    evaluator.evaluate()
    lines = evaluator.lines()
    lines.extend(time_based_lines(ctx, evaluator, task, issues))
    own = evaluator.structure_codes()
    lines.extend(input_lines(ctx, inputs, own, issues))
    lines.extend(arrears_lines(ctx))
    lines.extend(reimbursement_lines(ctx, issues))
    issues = evaluator.issues + issues
    return EarningsResult(tuple(lines), tuple(issues), evaluator.period_end_rates(), evaluator.ot_rate)


__all__ = ["EarningsResult", "StructureEvaluator", "calculate_earnings", "ctc_values", "standard_hours", "variables"]

"""Engine stage 8: post-tax deductions with net protection (3.5 §6, §7).

``apply(ctx, lines) -> (lines, issues)`` takes court orders, loan and advance instalments,
negative arrears, voluntary deduction inputs and late-penalty amounts from the context and
deducts them in the legal priority order

    statutory (already in the lines) > court orders (by legal priority) > loans and advances
    (then negative-arrears recoveries) > voluntary deductions > penalties

within the protected net: net pay may not fall below ``payroll.deductions.min_net_pay_percent`` of
gross cash earnings, never below the pack floor of a usable pack (the higher wins; the floor is
rounded up to the minor unit). Filling in priority order is the same as deferring in reverse
priority: what does not fit is not deducted and becomes a carry-forward record (a receivable in
a final settlement), with an issue (EC-VAR-05, EC-VAR-07, EC-VAR-11).

While ``payroll.deductions.min_net_pay_percent`` is not set (G-23), automated recoveries (loans,
advances, negative arrears, penalties) are not taken: they are carried forward and one blocking,
waivable ``PROTECTED_NET_NOT_SET`` issue lists them. Court orders and voluntary deductions are
still taken within the pack floor, or never below zero net when there is none. Negative net pay coming from earlier stages is never
clipped here (the net stage raises ``NEGATIVE_NET``, EC-RUN-10); every item is then deferred.

Context fields read (``engine.context.CalcContext``): ``currency``, ``settings``/``setting``,
``min_net_pay_percent``, ``court_orders``, ``loans``, ``advances`` (plain mappings: ``id`` or
``loan_id``; the amount due as ``amount``, ``due``, ``instalment_amount`` or ``instalment``; optional
``component``, ``priority``, ``status``, ``period_ref``, ``partial_allowed``, ``description``,
``instalment_number``, ``currency``), ``deduction_inputs`` (approved wage inputs of DEDUCTION
components), ``arrears_recoveries`` (negative arrears), ``penalty_lines`` (proposed penalty lines),
``final_settlement``, ``pack``/``environment``/``preview`` (pack floor) or ``pack_floor_percent``;
optionally ``deductions`` (``DeductionItem`` objects).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal, localcontext
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.payroll.engine.lines import STAGE_POST_TAX, CalcIssue, Line
from app.payroll.engine.statutory import ctx_get
from app.payroll.money import CALC_CONTEXT, decimal_str, quantize_amount, to_decimal
from app.payroll.money_errors import MoneyError
from app.payroll.packs.base import fmt
from app.payroll.time_pay_common import TimePayError
from app.payroll.time_pay_loans import ProtectedNet, protected_net_room

ZERO = Decimal(0)
COURT_ORDER, LOAN, ADVANCE, RECOVERY, VOLUNTARY, PENALTY = (
    "COURT_ORDER", "LOAN", "ADVANCE", "RECOVERY", "VOLUNTARY", "PENALTY")
TYPES = (COURT_ORDER, LOAN, ADVANCE, RECOVERY, VOLUNTARY, PENALTY)
RANK = {COURT_ORDER: 1, LOAN: 2, ADVANCE: 2, RECOVERY: 2, VOLUNTARY: 3, PENALTY: 4}
AUTOMATED = frozenset({LOAN, ADVANCE, RECOVERY, PENALTY})
CATEGORY = {COURT_ORDER: "GARNISHMENT_IF_LEGAL", LOAN: "LOAN_REPAYMENT", ADVANCE: "ADVANCE_RECOVERY",
            RECOVERY: "RECOVERY", VOLUNTARY: "VOLUNTARY", PENALTY: "PENALTY_IF_LEGAL"}
TYPE_OF_CATEGORY = {"GARNISHMENT_IF_LEGAL": COURT_ORDER, "LOAN_REPAYMENT": LOAN, "LOAN": LOAN,
                    "ADVANCE_RECOVERY": ADVANCE, "RECOVERY": RECOVERY, "PENALTY_IF_LEGAL": PENALTY}
DEFAULT_COMPONENT = {COURT_ORDER: "COURT_ORDER", LOAN: "LOAN_RECOVERY", ADVANCE: "ADVANCE_RECOVERY",
                     RECOVERY: "ARREARS_RECOVERY", VOLUNTARY: "OTHER_DEDUCTION", PENALTY: "LATE_PENALTY"}
SWITCHES = {LOAN: "payroll.loans.enabled", ADVANCE: "payroll.advances.enabled"}
PROTECTED_NET_KEY = "payroll.deductions.min_net_pay_percent"
AMOUNT_KEYS = ("amount", "due", "instalment_amount", "instalment")   # the amount due this period, first found

PROTECTED_NET_NOT_SET = "PROTECTED_NET_NOT_SET"
DEDUCTION_DEFERRED = "DEDUCTION_DEFERRED"
DEDUCTION_RECEIVABLE = "DEDUCTION_RECEIVABLE"
INVALID_DEDUCTION = "INVALID_DEDUCTION"
DEDUCTIONS_ALREADY_APPLIED = "DEDUCTIONS_ALREADY_APPLIED"


@dataclass(frozen=True)
class DeductionItem:
    """One amount due this period. ``amount`` already includes earlier carried-forward arrears."""

    id: str
    type: str
    amount: Decimal
    component: str
    priority: int = 0                       # within the type: lower first (court orders: legal priority)
    partial_allowed: bool = True
    period_ref: Optional[str] = None
    description: str = ""
    sources: Tuple[Mapping[str, Any], ...] = ()
    template: Optional[Line] = None         # a proposed line (penalties) whose trace and text are kept


@dataclass(frozen=True)
class CarryForward:
    """What was not deducted, for the run services to persist (next run's arrears or a receivable)."""

    item_id: str
    type: str
    component: str
    due: Decimal
    recovered: Decimal
    carried_forward: Decimal
    reason: str                             # PROTECTED_NET | PROTECTED_NET_NOT_SET
    receivable: bool = False                # final settlement: no next run
    period_ref: Optional[str] = None

    def to_json(self, currency: str) -> Dict[str, Any]:
        from app.payroll.money import amount_str

        return {"item_id": self.item_id, "type": self.type, "component": self.component,
                "due": amount_str(self.due, currency), "recovered": amount_str(self.recovered, currency),
                "carried_forward": amount_str(self.carried_forward, currency), "reason": self.reason,
                "receivable": self.receivable, "period_ref": self.period_ref, "currency": currency}


@dataclass(frozen=True)
class DeductionResult:
    lines: Tuple[Line, ...]
    added: Tuple[Line, ...]
    issues: Tuple[CalcIssue, ...]
    carry_forwards: Tuple[CarryForward, ...] = ()
    protected_net: Optional[ProtectedNet] = None
    net_before: Decimal = ZERO
    gross: Decimal = ZERO
    skipped: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)


def apply(ctx: Any, lines: Sequence[Line]) -> Tuple[List[Line], List[CalcIssue]]:
    """Stage 8 for one employee-period: ``(lines + post-tax deduction lines, issues)``."""
    result = evaluate(ctx, lines)
    return list(result.lines), list(result.issues)


def evaluate(ctx: Any, lines: Sequence[Line]) -> DeductionResult:
    """Like ``apply``, with the carry-forward records and the protected-net calculation."""
    lines = tuple(lines)
    currency = ctx_get(ctx, "currency")
    if any(line.stage == STAGE_POST_TAX and line.kind == "DEDUCTION" for line in lines):
        return DeductionResult(lines, (), (CalcIssue(DEDUCTIONS_ALREADY_APPLIED, "BLOCKING",
                                                     "Post-tax deductions are already present; stage 8 runs once."),))
    issues: List[CalcIssue] = []
    skipped: List[Mapping[str, Any]] = []
    items = _collect(ctx, currency, issues, skipped)
    with localcontext(CALC_CONTEXT):
        gross = sum((line.amount for line in lines if line.kind == "EARNING"), ZERO)
        reimbursements = sum((line.amount for line in lines if line.kind == "REIMBURSEMENT"), ZERO)
        deducted = sum((line.amount for line in lines if line.kind == "DEDUCTION"), ZERO)
        net_before = gross + reimbursements - deducted
    if not items:
        return DeductionResult(lines, (), tuple(issues), net_before=net_before, gross=gross, skipped=tuple(skipped))

    try:
        percent = _setting_percent(ctx)
        protection = protected_net_room(max(gross, ZERO), net_before, currency, min_net_pay_percent=percent,
                                        pack_floor_percent=_pack_floor(ctx))
    except (TimePayError, MoneyError, ValueError) as error:
        issues.append(CalcIssue(INVALID_DEDUCTION, "BLOCKING", f"Protected net pay cannot be applied: {error}",
                                {"setting": PROTECTED_NET_KEY}))
        return DeductionResult(lines, (), tuple(issues), net_before=net_before, gross=gross, skipped=tuple(skipped))

    unset = percent is None                       # G-23: the company percentage, not only a pack floor
    remaining = max(ZERO, net_before) if protection.percent is None else protection.room
    final = ctx_get(ctx, "final_settlement") is True
    added: List[Line] = []
    carries: List[CarryForward] = []
    blocked: List[DeductionItem] = []
    for item in sorted(items, key=_order(items)):
        due = item.amount
        if unset and item.type in AUTOMATED:
            blocked.append(item)
            carries.append(CarryForward(item.id, item.type, item.component, due, ZERO, due, PROTECTED_NET_NOT_SET,
                                        final, item.period_ref))
            continue
        room_before = remaining
        take = quantize_amount(min(due, max(ZERO, remaining)), currency, "DOWN")
        if take < due and not item.partial_allowed:
            take = ZERO
        with localcontext(CALC_CONTEXT):
            remaining = remaining - take
            rest = due - take
        if take > 0:
            added.append(_line(item, take, due, rest, room_before, remaining, protection, currency))
        if rest > 0:
            carries.append(CarryForward(item.id, item.type, item.component, due, take, rest, "PROTECTED_NET", final,
                                        item.period_ref))
            issues.append(_deferred_issue(item, due, take, rest, final, protection, currency))
    if blocked:
        issues.append(CalcIssue(
            PROTECTED_NET_NOT_SET, "BLOCKING",
            "Automated recoveries need a protected net pay percentage (Settings › Loans & advances); "
            f"{len(blocked)} item(s) were not deducted and are carried forward: "
            + ", ".join(f"{b.type.lower()} {b.id} {fmt(b.amount)}" for b in blocked) + ".",
            {"setting": PROTECTED_NET_KEY, "items": [{"id": b.id, "type": b.type, "amount": decimal_str(b.amount)}
                                                     for b in blocked]},
            waivable=True))
    return DeductionResult(lines + tuple(added), tuple(added), tuple(issues), tuple(carries), protection, net_before,
                           gross, tuple(skipped))


# --------------------------------------------------------------------------- collecting the items


def _collect(ctx: Any, currency: str, issues: List[CalcIssue], skipped: List[Mapping[str, Any]]) -> List[DeductionItem]:
    items: List[DeductionItem] = []

    def bad(message: str, **details: Any) -> None:
        issues.append(CalcIssue(INVALID_DEDUCTION, "BLOCKING", message, details))

    for type_, name in ((COURT_ORDER, "court_orders"), (LOAN, "loans"), (ADVANCE, "advances")):
        for raw in ctx_get(ctx, name, ()) or ():
            item = _from_mapping(raw, type_, currency, bad)
            if item is None:
                continue
            if str(raw.get("status", "ACTIVE")).upper() == "PAUSED" and not ctx_get(ctx, "final_settlement"):
                skipped.append({"id": item.id, "type": type_, "reason": "PAUSED"})       # EC-VAR-06
                continue
            switch = SWITCHES.get(type_)
            if switch and _setting(ctx, switch) is not True:
                skipped.append({"id": item.id, "type": type_, "reason": "SWITCH_OFF"})
                issues.append(CalcIssue("RECOVERY_SWITCHED_OFF", "FLAG",
                                        f"{type_.title()} {item.id} is due but {switch} is off; nothing deducted.",
                                        {"id": item.id, "setting": switch}))
                continue
            items.append(item)

    for wage_input in ctx_get(ctx, "deduction_inputs", ()) or ():
        item = _from_input(ctx, wage_input, currency, bad)
        if item is not None:
            items.append(item)

    recovery_mode = _setting(ctx, "payroll.arrears.recovery_mode")
    for arrears in ctx_get(ctx, "arrears_recoveries", ()) or ():
        amount = getattr(arrears, "amount", None)
        if amount is None or amount >= 0:
            continue
        item = DeductionItem(str(arrears.id), RECOVERY, -amount, DEFAULT_COMPONENT[RECOVERY],
                             period_ref=arrears.period_ref,
                             description=f"over-payment of {arrears.component} in {arrears.period_ref}",
                             sources=({"type": "arrears", "id": str(arrears.id), "component": arrears.component},))
        if recovery_mode == "MANUAL":
            skipped.append({"id": item.id, "type": RECOVERY, "reason": "MANUAL_RECOVERY"})
            issues.append(CalcIssue("ARREARS_RECOVERY_MANUAL", "WARNING",
                                    f"Negative arrears {fmt(item.amount)} ({item.description}) are recovered manually "
                                    "(payroll.arrears.recovery_mode = MANUAL); nothing deducted.",
                                    {"id": item.id, "amount": decimal_str(item.amount)}))
            continue
        items.append(item)

    for line in ctx_get(ctx, "penalty_lines", ()) or ():
        if line.amount <= 0:
            continue
        ref = next((str(s.get("id")) for s in line.sources if s.get("id") is not None), line.component)
        items.append(DeductionItem(ref, PENALTY, line.amount, line.component, period_ref=line.period_ref,
                                   description=line.explanation, sources=line.sources, template=line))

    for extra in ctx_get(ctx, "deductions", ()) or ():
        if isinstance(extra, DeductionItem):
            items.append(extra)
    return items


def _from_mapping(raw: Any, type_: str, currency: str, bad: Any) -> Optional[DeductionItem]:
    if not isinstance(raw, Mapping):
        bad(f"A {type_.lower()} item must be an object.", type=type_)
        return None
    ident = raw.get("id", raw.get("loan_id"))
    value = next((raw[k] for k in AMOUNT_KEYS if raw.get(k) is not None), None)
    try:
        amount = to_decimal(value)
        exact = quantize_amount(amount, currency) == amount
        priority = int(raw.get("priority") or 0)
    except (MoneyError, TypeError, ValueError):
        bad(f"{type_.title()} {ident}: the amount due must be a decimal string and the priority a whole number.",
            type=type_, id=ident)
        return None
    if ident is None or amount < 0 or not exact:
        bad(f"{type_.title()} {ident}: needs an id and a non-negative amount in {currency} minor units.",
            type=type_, id=ident, amount=str(value))
        return None
    if raw.get("currency") not in (None, currency):
        bad(f"{type_.title()} {ident} is in {raw.get('currency')}; this payroll is in {currency}.", type=type_, id=ident)
        return None
    if amount == 0:
        return None
    number = raw.get("instalment_number")
    description = str(raw.get("description") or "") or (
        f"{type_.replace('_', ' ').lower()} {ident}" + (f", instalment {number}" if number is not None else ""))
    source = {"type": type_.lower(), "id": str(ident)}
    for key in ("instalment_number", "scheduled", "arrears", "reference", "run_employee_id"):
        if raw.get(key) is not None:
            source[key] = str(raw.get(key))
    return DeductionItem(
        str(ident), type_, amount, str(raw.get("component") or DEFAULT_COMPONENT[type_]),
        priority=priority, partial_allowed=raw.get("partial_allowed", True) is not False,
        period_ref=raw.get("period_ref"), description=description, sources=(source,),
    )


def _from_input(ctx: Any, wage_input: Any, currency: str, bad: Any) -> Optional[DeductionItem]:
    amount = getattr(wage_input, "amount", None)
    ident = str(getattr(wage_input, "id", "?"))
    code = getattr(wage_input, "component", "OTHER_DEDUCTION")
    if amount is None or amount < 0:
        bad(f"Deduction input {ident} for {code} needs a non-negative amount.", id=ident, component=code)
        return None
    if getattr(wage_input, "currency", None) not in (None, currency):
        bad(f"Deduction input {ident} is in {wage_input.currency}; this payroll is in {currency}.", id=ident)
        return None
    if amount == 0:
        return None
    definition = ctx.component(code) if hasattr(ctx, "component") else None
    type_ = TYPE_OF_CATEGORY.get(getattr(definition, "category", None), VOLUNTARY)
    reason = getattr(wage_input, "reason", None)
    return DeductionItem(ident, type_, amount, code, description=reason or f"approved input {ident}",
                         sources=({"type": "wage_input", "id": ident},))


# --------------------------------------------------------------------------- helpers


def _setting(ctx: Any, key: str) -> Any:
    if hasattr(ctx, "setting") and callable(ctx.setting):
        return ctx.setting(key)
    return (ctx_get(ctx, "settings", {}) or {}).get(key)


def _setting_percent(ctx: Any) -> Optional[Decimal]:
    value = ctx_get(ctx, "min_net_pay_percent")
    if value is None:
        value = _setting(ctx, PROTECTED_NET_KEY)
    return None if value is None else to_decimal(value)


def _pack_floor(ctx: Any) -> Optional[Decimal]:
    """The pack floor of a pack that may calculate here (verified or allowed fixture); unverified data never binds."""
    explicit = ctx_get(ctx, "pack_floor_percent")
    if explicit is not None:
        return to_decimal(explicit)
    ref = ctx_get(ctx, "pack")
    if not ref:
        return None
    from app.payroll.packs import environment_name, lookup, usage

    pack = lookup(ref.get("code"), ref.get("version")) if ref.get("code") and ref.get("version") else None
    if pack is None:
        return None
    verdict = usage(pack.meta, environment=environment_name(ctx_get(ctx, "environment")), status=ref.get("status"))
    return pack.protected_net_floor_percent() if verdict.final else None


def _order(items: Sequence[DeductionItem]) -> Any:
    position = {id(item): index for index, item in enumerate(items)}
    return lambda item: (RANK[item.type], item.priority, position[id(item)])


def _line(item: DeductionItem, take: Decimal, due: Decimal, rest: Decimal, room_before: Decimal, room_after: Decimal,
          protection: ProtectedNet, currency: str) -> Line:
    unset = protection.percent is None
    floor = "net pay may not go below zero" if unset else (
        f"protected net {decimal_str(protection.percent)}% of gross = {fmt(protection.floor)}")
    text = f"{item.description or item.type.replace('_', ' ').lower()}: due {fmt(due)}"
    if rest:
        text += f", deducted {fmt(take)} ({floor}); {fmt(rest)} carried forward"
    trace = {"method": "DEDUCTION", "type": item.type, "rank": RANK[item.type], "priority": item.priority,
             "due": decimal_str(due), "recovered": decimal_str(take), "carried_forward": decimal_str(rest),
             "room_before": decimal_str(room_before), "room_after": decimal_str(room_after),
             "protected_net": {"percent": None if unset else decimal_str(protection.percent),
                               "floor": None if unset else decimal_str(protection.floor),
                               "explanation": protection.explanation}}
    if item.template is not None:
        template = item.template
        return replace(template, amount=take, stage=STAGE_POST_TAX, kind="DEDUCTION",
                       trace={**trace, "proposed": template.trace},
                       explanation=template.explanation if not rest else f"{template.explanation}; {text}")
    return Line(component=item.component, kind="DEDUCTION", category=CATEGORY[item.type], stage=STAGE_POST_TAX,
                amount=take, currency=currency, period_ref=item.period_ref, sources=tuple(dict(s) for s in item.sources),
                trace=trace, explanation=text[0].upper() + text[1:], sort_order=800 + RANK[item.type] * 10)


def _deferred_issue(item: DeductionItem, due: Decimal, take: Decimal, rest: Decimal, final: bool,
                    protection: ProtectedNet, currency: str) -> CalcIssue:
    from app.payroll.money import amount_str

    what = f"{item.type.replace('_', ' ').lower()} {item.id}"
    if final:
        code, message = DEDUCTION_RECEIVABLE, (f"{fmt(rest)} of {what} cannot be deducted in the final settlement "
                                               "within the protected net and becomes a receivable.")
    else:
        code, message = DEDUCTION_DEFERRED, (f"Only {fmt(take)} of {fmt(due)} deducted for {what} to protect net pay; "
                                             f"{fmt(rest)} carried forward to the next run.")
    return CalcIssue(code, "WARNING", message,
                     {"id": item.id, "type": item.type, "component": item.component,
                      "due": amount_str(due, currency), "recovered": amount_str(take, currency),
                      "carried_forward": amount_str(rest, currency), "receivable": final,
                      "protected_floor": None if protection.floor is None else amount_str(protection.floor, currency)},
                     component=item.component)


def carry_forwards_json(result: DeductionResult, currency: str) -> List[Dict[str, Any]]:
    return [c.to_json(currency) for c in result.carry_forwards]

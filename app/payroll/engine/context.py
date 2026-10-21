"""Calculation context: the resolved, read-only view of one snapshot that every engine stage reads (3.5 §7).

``build_context(snapshot)`` is stage 1 (resolve): it fixes the pay currency and its
minor unit, fills settings that the snapshot does not carry with the registry
default (never an invented one), and exposes DAY-grain switches as date ranges.
The pipeline adds the stage 2-3 results (day ledger, segment unit facts, overtime
hours, the overtime hourly rate) with ``ctx.evolve(...)`` before earnings run, so a
stage sees exactly the context of its position in the pipeline.

Contract with ``engine.statutory.apply(ctx, lines)`` and ``engine.deductions.apply(ctx, lines)``:

    ctx.currency, ctx.minor_unit, ctx.minor_units ({currency: digits}), ctx.quantum
    ctx.period (PeriodInfo), ctx.period_start, ctx.period_end, ctx.pay_date, ctx.frequency, ctx.period_days,
    ctx.tax_periods_remaining, ctx.paid_days (after penalty LOP), ctx.final_settlement, ctx.preview
    ctx.employee (EmployeeInfo; ``employee.get(key)`` gives the pack facts: branch_subdivision, gender,
                 date_of_birth, date_of_joining, worker_class, ...), ctx.country (legal entity country)
    ctx.environment, ctx.is_production       development | test | staging | production; fixtures never in production
    ctx.pack, ctx.pack_state, ctx.tax_profile, ctx.attestation  frozen mappings from the snapshot, or None
    ctx.ytd ({code: Decimal}), ctx.previous   tax-year-to-date and last finalized period
    ctx.setting(key), ctx.setting_decimal(key), ctx.switch_on(key, date), ctx.switch_ranges(key)
    ctx.component(code) -> ComponentDef | None, ctx.has_tag(code, tag)
    ctx.ledger (DayLedger), ctx.segment_facts, ctx.period_facts, ctx.segments
    ctx.line_mode                             company line rounding; statutory lines use the pack rule
    ctx.loans, ctx.advances, ctx.court_orders  instalments and orders due (plain mappings)
    ctx.deduction_inputs                      approved wage inputs of DEDUCTION components (voluntary items)
    ctx.penalty_lines                         proposed late-penalty deductions (FIXED_AMOUNT, stage 8) for
                                              deductions.apply to take within the protected net
    ctx.arrears_recoveries                    negative arrears (over-payments) to recover within the protected net
    ctx.min_net_pay_percent                   payroll.deductions.min_net_pay_percent (None = not set, G-23)

Nothing here reads a clock, the environment, the network or a database.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from app.payroll import currencies
from app.payroll.engine.lines import Line
from app.payroll.engine.snapshot import (
    ArrearsItem,
    ComponentDef,
    EmployeeInfo,
    PeriodInfo,
    PolicySnapshot,
    Previous,
    Segment,
    Snapshot,
    WageInput,
)
from app.payroll.ledger import DayLedger
from app.payroll.proration import PeriodFacts, UnitFacts
from app.payroll.settings_registry import REGISTRY, UNSET, Derived, canonical_key
from app.payroll.time_pay import OvertimeHours, switch_on

DayRanges = tuple[tuple[dt.date, dt.date | None], ...]
PRODUCTION = "production"


class ContextError(ValueError):
    """The snapshot cannot be calculated at all (for example an unusable pay currency)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _canon(key: str) -> str:
    try:
        return canonical_key(key)
    except ValueError:
        return key


def _registry_default(key: str) -> Any:
    definition = REGISTRY.get(_canon(key))
    if definition is None:
        return None
    default = definition.default
    if default is UNSET or isinstance(default, Derived):
        return None
    return default


@dataclass(frozen=True)
class CalcContext:
    """Read-only inputs of one employee's calculation; see the module docstring for the stage contract."""

    snapshot: Snapshot
    currency: str
    minor_unit: int
    line_mode: str
    ledger: DayLedger | None = None
    period_facts: PeriodFacts | None = None
    segment_facts: tuple[UnitFacts, ...] = ()
    segment_keys: tuple[str, ...] = ()
    overtime: OvertimeHours | None = None
    ot_hourly_rate: Decimal | None = None
    penalty_lines: tuple[Line, ...] = ()
    extras: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    # -- identity and period -------------------------------------------------------------------

    @property
    def period(self) -> PeriodInfo:
        return self.snapshot.period

    @property
    def employee(self) -> EmployeeInfo:
        return self.snapshot.employee

    @property
    def segments(self) -> tuple[Segment, ...]:
        return self.snapshot.segments

    @property
    def period_start(self) -> dt.date:
        return self.snapshot.period.start

    @property
    def period_end(self) -> dt.date:
        return self.snapshot.period.end

    @property
    def pay_date(self) -> dt.date:
        return self.snapshot.period.pay_date

    @property
    def frequency(self) -> str:
        return self.snapshot.period.frequency

    @property
    def period_days(self) -> Decimal:
        return Decimal(self.snapshot.period.days)

    @property
    def tax_periods_remaining(self) -> int | None:
        """Periods left in the tax year, this one included."""
        number = self.snapshot.period.number_in_tax_year
        return None if number is None else max(1, self.snapshot.period.periods_per_year - number + 1)

    @property
    def paid_days(self) -> Decimal | None:
        return None if self.ledger is None else self.ledger.totals.paid_days_after_penalty

    @property
    def final_settlement(self) -> bool:
        return self.snapshot.run.run_type == "FINAL_SETTLEMENT"

    @property
    def preview(self) -> bool:
        return self.snapshot.run.run_type in ("PREVIEW", "SIMULATION")

    @property
    def country(self) -> str:
        return self.snapshot.employee.country

    @property
    def environment(self) -> str:
        return self.snapshot.run.environment

    @property
    def is_production(self) -> bool:
        return self.environment == PRODUCTION

    @property
    def quantum(self) -> Decimal:
        return Decimal(1).scaleb(-self.minor_unit)

    @property
    def minor_units(self) -> Mapping[str, int]:
        """Minor units of every currency the snapshot mentions (display and the formula trace, F-07)."""
        codes = {self.currency, *(s.compensation.currency for s in self.segments if s.compensation)}
        codes |= {c for c in (self.snapshot.previous.currency, self.snapshot.previous.payment_currency) if c}
        codes |= {str(code) for code in (self.snapshot.fx.get("rates") or {})}
        result = {}
        for code in sorted(codes):
            info = currencies.get(code) if _known(code) else None
            if info is not None and info.minor_unit is not None:
                result[code] = info.minor_unit
        return MappingProxyType(result)

    # -- frozen external data ------------------------------------------------------------------

    @property
    def pack(self) -> Mapping[str, Any] | None:
        return self.snapshot.pack

    @property
    def pack_state(self) -> Mapping[str, Any]:
        return self.snapshot.pack_state

    @property
    def tax_profile(self) -> Mapping[str, Any] | None:
        return self.snapshot.tax_profile

    @property
    def attestation(self) -> Mapping[str, Any] | None:
        return self.snapshot.attestation

    @property
    def ytd(self) -> Mapping[str, Decimal]:
        return self.snapshot.ytd

    @property
    def previous(self) -> Previous:
        return self.snapshot.previous

    @property
    def fx(self) -> Mapping[str, Any]:
        return self.snapshot.fx

    @property
    def loans(self) -> tuple[Mapping[str, Any], ...]:
        return self.snapshot.loans

    @property
    def advances(self) -> tuple[Mapping[str, Any], ...]:
        return self.snapshot.advances

    @property
    def court_orders(self) -> tuple[Mapping[str, Any], ...]:
        return self.snapshot.court_orders

    @property
    def deduction_inputs(self) -> tuple[WageInput, ...]:
        """Approved wage inputs of DEDUCTION components (OTHER_DEDUCTION and similar), in snapshot order."""
        return tuple(w for w in self.snapshot.wage_inputs
                     if (c := self.component(w.component)) is not None and c.kind == "DEDUCTION")

    @property
    def arrears_recoveries(self) -> tuple[ArrearsItem, ...]:
        return tuple(a for a in self.snapshot.arrears if a.amount < 0)

    # -- settings, switches and policies -------------------------------------------------------

    @property
    def settings(self) -> Mapping[str, Any]:
        return self.snapshot.settings

    def setting(self, key: str, default: Any = None) -> Any:
        """The resolved value; a key the snapshot does not carry takes the registry default, then ``default``."""
        key = key if key in self.snapshot.settings else _canon(key)
        if key in self.snapshot.settings:
            return self.snapshot.settings[key]
        value = _registry_default(key)
        return default if value is None else value

    def setting_decimal(self, key: str) -> Decimal | None:
        value = self.setting(key)
        if value is None:
            return None
        if isinstance(value, Mapping):
            value = value.get("amount")
        if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
            raise ContextError("INVALID_SETTING", f"{key} must be a decimal, got {value!r}")
        return Decimal(value)

    def setting_bool(self, key: str) -> bool:
        value = self.setting(key, False)
        if not isinstance(value, bool):
            raise ContextError("INVALID_SETTING", f"{key} must be true or false, got {value!r}")
        return value

    def switch_ranges(self, key: str) -> DayRanges | None:
        """Dates on which a DAY-grain switch is ON (``None`` = every date, ``()`` = never), for time_pay."""
        key = key if key in self.snapshot.switch_ranges else _canon(key)
        if key in self.snapshot.switch_ranges:
            return self.snapshot.switch_ranges[key]
        return None if self.setting_bool(key) else ()

    def switch_on(self, key: str, when: dt.date) -> bool:
        return switch_on(when, self.switch_ranges(key))

    def switch_any(self, key: str) -> bool:
        """ON on at least one date of the period."""
        ranges = self.switch_ranges(key)
        if ranges is None:
            return True
        start, end = self.period.start, self.period.end
        return any(s <= end and (e is None or e >= start) for s, e in ranges)

    def policy(self, policy_type: str) -> PolicySnapshot | None:
        return self.snapshot.policies.get(policy_type)

    def policy_fields(self, policy_type: str) -> Mapping[str, Any] | None:
        policy = self.policy(policy_type)
        return None if policy is None else policy.fields

    @property
    def min_net_pay_percent(self) -> Decimal | None:
        return self.setting_decimal("payroll.deductions.min_net_pay_percent")

    # -- components ----------------------------------------------------------------------------

    def component(self, code: str) -> ComponentDef | None:
        return self.snapshot.components.get(code)

    def has_tag(self, code: str, tag: str) -> bool:
        component = self.component(code)
        return component is not None and tag in component.tags

    def evolve(self, **changes: Any) -> CalcContext:
        return replace(self, **changes)


def _known(code: str) -> bool:
    try:
        currencies.get(code)
    except Exception:  # noqa: BLE001 - unknown or malformed codes are simply not listed
        return False
    return True


def build_context(snapshot: Snapshot) -> CalcContext:
    """Stage 1 (resolve): a context over the snapshot; an unusable pay currency raises ContextError."""
    try:
        info = currencies.require_payroll_currency(snapshot.currency)
    except Exception as exc:  # noqa: BLE001 - every currency failure is the same refusal
        raise ContextError("INVALID_CURRENCY", f"{snapshot.currency} cannot be used for payroll: {exc}") from None
    if info.minor_unit is None:
        raise ContextError("INVALID_CURRENCY", f"{snapshot.currency} has no minor unit")
    probe = CalcContext(snapshot, snapshot.currency, info.minor_unit, "HALF_UP")
    mode = probe.setting("payroll.rounding.line_mode", "HALF_UP")
    if mode not in ("HALF_UP", "HALF_EVEN", "UP", "DOWN"):
        raise ContextError("INVALID_SETTING", f"payroll.rounding.line_mode {mode!r} is not a line rounding mode")
    return replace(probe, line_mode=mode)


__all__ = ["CalcContext", "ContextError", "DayRanges", "PRODUCTION", "build_context"]

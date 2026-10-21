"""Engine stages 5-7: pre-tax items, statutory contributions and tax withholding (3.5 §7).

``apply(ctx, lines) -> (lines, issues)`` runs the country pack frozen in the snapshot. The gate
(decision I-07):

* no pack, an unknown version, a version that does not cover the period, a DRAFT/UNDER_REVIEW
  version outside a preview, a fixture outside development/test, or a test-only pack outside
  tests → **no statutory lines** and ``STATUTORY_NOT_VERIFIED``. Its severity follows I-15:
  ``BLOCKING`` (finalization blocked) unless the snapshot carries an active statutory attestation,
  then ``WARNING`` ("Statutory deductions not calculated — compliance not verified"). The run
  services enforce the attestation gate; the engine only reports;
* a DISABLED version → no lines, ``STATUTORY_PACK_DISABLED`` (blocking, never attestable);
* a verified version → final statutory lines;
* a fixture in development/test → lines badged "FIXTURE — not law" (``fixture=True``) and a
  ``STATUTORY_FIXTURE`` flag;
* a draft in a preview → lines labelled "Compliance not verified" plus ``STATUTORY_NOT_VERIFIED``.

Determinations that later periods must reuse (for example ESI eligibility for a contribution
period, EC-STA-17) are returned as ``StatutoryResult.pack_state`` and, so that they survive the
pipeline's ``(lines, issues)`` contract, as a ``PACK_STATE_DETERMINED`` flag whose details carry
``pack_state``; the run services store it and feed it back as the next snapshot's ``pack_state``.

The pack's rounding is applied to its lines and overrides company rounding (EC-STA-13). Employee
and employer portions are separate lines with separate liabilities (EC-STA-10). Pack errors never
escape: they become a blocking ``STATUTORY_CALCULATION_ERROR`` with no statutory lines.

Packs calculate for employees only: a ``CONTRACTOR`` gets no statutory lines and a blocking,
waivable ``WORKER_CLASS_REVIEW`` (reclassification is an explicit HR/legal action, EC-STA-20). A tax
profile listing more than one tax jurisdiction raises ``TAX_SPECIALIST_REVIEW`` ("Requires
specialist review"); residency is never derived from citizenship (EC-STA-09). YTD comes from the
snapshot per legal entity and tax year (EC-STA-08, EC-STA-22); the pack decides what it combines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple, runtime_checkable

from app.payroll import countries
from app.payroll.engine.lines import CalcIssue, Line
from app.payroll.money_errors import MoneyError
from app.payroll.packs import (
    FIXTURE,
    PREVIEW,
    VERIFIED,
    CountryPack,
    PackUsage,
    VectorResult,
    environment_name,
    lookup,
    usage,
)
from app.payroll.packs.base import (
    FIXTURE_LABEL,
    NOT_VERIFIED_LABEL,
    Liability,
    PackContext,
    PackError,
    TestVector,
    liabilities,
    with_labels,
)

STATUTORY_NOT_VERIFIED = "STATUTORY_NOT_VERIFIED"
STATUTORY_PACK_DISABLED = "STATUTORY_PACK_DISABLED"
STATUTORY_FIXTURE = "STATUTORY_FIXTURE"
STATUTORY_SCHEME_OFF = "STATUTORY_SCHEME_OFF"
STATUTORY_CALCULATION_ERROR = "STATUTORY_CALCULATION_ERROR"
STATUTORY_CURRENCY_MISMATCH = "STATUTORY_CURRENCY_MISMATCH"
PACK_CHANGED = "PACK_CHANGED"
PACK_COUNTRY_MISMATCH = "PACK_COUNTRY_MISMATCH"
STATUTORY_ALREADY_APPLIED = "STATUTORY_ALREADY_APPLIED"
PACK_STATE_DETERMINED = "PACK_STATE_DETERMINED"
WORKER_CLASS_REVIEW = "WORKER_CLASS_REVIEW"
TAX_SPECIALIST_REVIEW = "TAX_SPECIALIST_REVIEW"
NON_EMPLOYEE_CLASSES = frozenset({"CONTRACTOR"})
NOT_CALCULATED_TEXT = "Statutory deductions not calculated — compliance not verified"


@runtime_checkable
class StatutoryCtx(Protocol):
    """What stages 5-8 read from the engine context (attribute or mapping access).

    Required: ``currency``, ``period_start``/``period_end`` (or ``period`` with ``start``/``end``),
    ``settings`` (resolved values by key), ``pack`` (``packs.pack_snapshot(...)``: code, version,
    status, is_fixture, fingerprint; None when the entity has no pack), ``ytd`` (per legal entity
    and tax year, by component code or wage-base name), ``tax_profile``, ``environment``
    (development | test | staging | production; anything else is production) and ``attestation``
    (the active statutory attestation of the entity, or None).

    Optional: ``employee`` (pack-readable facts), ``pack_state``, ``preview``, ``pay_date``,
    ``frequency``, ``tax_periods_remaining``, ``paid_days``, ``period_days``, ``final_settlement``,
    ``country`` (legal entity), ``ledger``; for stage 8 ``deductions``, ``loans``,
    ``run_employee_id``, ``recover_balance_on_exit``, ``pack_floor_percent``.
    """

    currency: str
    settings: Mapping[str, Any]


_MISSING = object()


def ctx_get(ctx: Any, name: str, default: Any = None) -> Any:
    """Read a context field by attribute or key; ``period_start``/``period_end`` also from ``period``."""
    if isinstance(ctx, Mapping):
        value = ctx.get(name, _MISSING)
    else:
        value = getattr(ctx, name, _MISSING)
    if value is _MISSING and name in ("period_start", "period_end"):
        period = ctx_get(ctx, "period")
        if period is not None:
            value = ctx_get(period, "start" if name == "period_start" else "end", _MISSING)
    return default if value is _MISSING or value is None else value


def attested(ctx: Any) -> bool:
    """An active statutory attestation (maker-checker, I-15) is in the snapshot."""
    attestation = ctx_get(ctx, "attestation")
    if attestation is True:
        return True
    if isinstance(attestation, Mapping):
        return attestation.get("active") is True or attestation.get("status") == "ACTIVE"
    return bool(getattr(attestation, "active", False)) or getattr(attestation, "status", None) == "ACTIVE"


@dataclass(frozen=True)
class StatutoryResult:
    lines: Tuple[Line, ...]                 # the input lines followed by the statutory lines
    added: Tuple[Line, ...]
    issues: Tuple[CalcIssue, ...]
    usage: Optional[PackUsage]
    pack: Optional[CountryPack]
    bases: Dict[str, Decimal] = field(default_factory=dict)
    taxable: Optional[Decimal] = None
    pack_state: Dict[str, Any] = field(default_factory=dict)   # new determinations to persist
    liabilities: Tuple[Liability, ...] = ()
    ytd_increments: Dict[str, Decimal] = field(default_factory=dict)


def apply(ctx: Any, lines: Sequence[Line]) -> Tuple[List[Line], List[CalcIssue]]:
    """Stages 5-7 for one employee-period: ``(lines + statutory lines, issues)``."""
    result = evaluate(ctx, lines)
    return list(result.lines), list(result.issues)


def evaluate(ctx: Any, lines: Sequence[Line]) -> StatutoryResult:
    """Like ``apply``, with the bases, taxable income, liabilities and pack state for the services."""
    lines = tuple(lines)
    ref = ctx_get(ctx, "pack") or ctx_get(ctx, "pack_snapshot")
    if any(line.statutory for line in lines):
        return _refused(lines, [CalcIssue(STATUTORY_ALREADY_APPLIED, "BLOCKING",
                                          "Statutory lines are already present; stages 5-7 run once per calculation.")])
    if not ref:
        return _refused(lines, [_not_verified(ctx, "NO_PACK", "No country pack applies to this legal entity.", None)])
    code, version = ref.get("code"), ref.get("version")
    pack = lookup(code, version) if code and version else None
    if pack is None:
        return _refused(lines, [_not_verified(ctx, "PACK_NOT_FOUND",
                                              f"Pack version {version or '?'} of {code or '?'} is not available.", ref)])
    start, end = ctx_get(ctx, "period_start"), ctx_get(ctx, "period_end")
    verdict = usage(pack.meta, environment=environment_name(ctx_get(ctx, "environment")),
                    preview=ctx_get(ctx, "preview") is True, status=ref.get("status"),
                    period=(start, end) if start and end else None)
    if verdict.reason == "PACK_DISABLED":
        return _refused(lines, [CalcIssue(STATUTORY_PACK_DISABLED, "BLOCKING", verdict.message,
                                          {"pack": pack.version})], pack, verdict)
    if not verdict.produces_lines:
        return _refused(lines, [_not_verified(ctx, verdict.reason or "PACK_REFUSED", verdict.message, ref)],
                        pack, verdict)
    blockers = _preconditions(ctx, pack, ref)
    if blockers:
        return _refused(lines, blockers, pack, verdict)
    worker = _worker_class_issue(ctx, pack)
    if worker is not None:
        return _refused(lines, [worker], pack, verdict)

    try:
        pctx = pack_context(ctx, pack)
    except PackError as error:
        return _refused(lines, [CalcIssue(STATUTORY_CALCULATION_ERROR, "BLOCKING", error.message,
                                          {"pack": pack.version, "error": error.code})], pack, verdict)
    added, issues, bases, taxable = compute(pack, pctx, lines, ledger=ctx_get(ctx, "ledger"))
    if verdict.mode in (VERIFIED, FIXTURE):
        issues = [*_scheme_issues(pack, pctx), *issues]
    issues.extend(_jurisdiction_issues(pctx))
    if any(i.code == STATUTORY_CALCULATION_ERROR for i in issues):
        added = []
        pctx.state_out.clear()
    status = verdict.status.value
    added = [with_labels(line, fixture=verdict.mode == FIXTURE, verified=verdict.mode == VERIFIED, status=status)
             for line in added]
    if verdict.mode == FIXTURE:
        issues.append(CalcIssue(STATUTORY_FIXTURE, "FLAG", f"{FIXTURE_LABEL}: statutory lines come from fixture "
                                f"pack {pack.version} and are not legal advice.", {"pack": pack.version}))
    elif verdict.mode == PREVIEW:
        issues.append(_not_verified(ctx, verdict.reason or "PACK_NOT_VERIFIED",
                                    f"{verdict.message} The statutory lines shown are a preview only.", ref))
    if pctx.state_out:
        issues.append(CalcIssue(PACK_STATE_DETERMINED, "FLAG",
                                "Statutory determinations made in this period apply to later periods too: "
                                + ", ".join(sorted(pctx.state_out)) + ".",
                                {"pack": pack.version, "pack_state": _plain(pctx.state_out)}))
    added = sorted(added, key=lambda line: (line.stage, line.sort_order, line.component))
    return StatutoryResult(
        lines=lines + tuple(added), added=tuple(added), issues=tuple(issues), usage=verdict, pack=pack,
        bases=dict(bases), taxable=taxable, pack_state=dict(pctx.state_out),
        liabilities=tuple(liabilities(added, pctx.period_end)),
        ytd_increments=pack.ytd_increments(added, taxable=taxable),
    )


def pack_context(ctx: Any, pack: CountryPack) -> PackContext:
    """The ``PackContext`` a pack sees, built from the engine context."""
    return PackContext(
        version=pack.meta,
        currency=ctx_get(ctx, "currency"),
        period_start=ctx_get(ctx, "period_start"),
        period_end=ctx_get(ctx, "period_end"),
        settings=ctx_get(ctx, "settings", {}),
        employee=ctx_get(ctx, "employee", {}),
        tax_profile=ctx_get(ctx, "tax_profile"),
        ytd=ctx_get(ctx, "ytd", {}),
        pack_state=ctx_get(ctx, "pack_state", {}),
        pay_date=ctx_get(ctx, "pay_date"),
        frequency=ctx_get(ctx, "frequency", "MONTHLY"),
        tax_periods_remaining=ctx_get(ctx, "tax_periods_remaining"),
        paid_days=_decimal_or_none(ctx_get(ctx, "paid_days")),
        period_days=_decimal_or_none(ctx_get(ctx, "period_days")),
        final_settlement=ctx_get(ctx, "final_settlement") is True,
        choice_defaults=pack.choice_defaults(),
    )


def compute(pack: CountryPack, pctx: PackContext, lines: Sequence[Line], *, ledger: Any = None
            ) -> Tuple[List[Line], List[CalcIssue], Dict[str, Decimal], Optional[Decimal]]:
    """Run the pack's stages in order. A blocking context problem or a pack error yields no lines."""
    issues: List[CalcIssue] = []
    try:
        issues.extend(pack.validate_context(pctx))
        if any(i.severity == "BLOCKING" for i in issues):
            return [], issues + pctx.issues, {}, None
        pre_tax = list(pack.pre_tax_deductions(lines, pctx))
        after_pre = [*lines, *pre_tax]
        bases = dict(pack.statutory_bases(after_pre, pctx))
        contributions = [*pack.employee_contributions(bases, pctx), *pack.employer_contributions(bases, pctx)]
        after_contributions = [*after_pre, *contributions]
        taxable = pack.taxable_income(after_contributions, pctx)
        withheld = list(pack.withholding(taxable, pctx.ytd, pctx.tax_profile, pctx))
        checks = list(pack.minimum_pay_checks(ledger, [*after_contributions, *withheld], pctx))
    except (PackError, MoneyError, ArithmeticError, KeyError, TypeError, ValueError) as error:
        message = getattr(error, "message", None) or str(error)
        return [], [*issues, *pctx.issues, CalcIssue(
            STATUTORY_CALCULATION_ERROR, "BLOCKING", f"Pack {pack.version} could not calculate: {message}",
            {"pack": pack.version, "error": type(error).__name__})], {}, None
    return [*pre_tax, *contributions, *withheld], [*issues, *pctx.issues, *checks], bases, taxable


def run_vector(pack: CountryPack, vector: TestVector) -> VectorResult:
    """One golden vector through the pack's stages, with no usage gate (vectors test the rules)."""
    pctx = PackContext(
        version=pack.meta, currency=pack.meta.currency, period_start=vector.period_start,
        period_end=vector.period_end, settings=vector.settings, employee=vector.employee,
        tax_profile=vector.tax_profile, ytd=vector.ytd, pack_state=vector.pack_state,
        choice_defaults=pack.choice_defaults(),
    )
    added, issues, _, _ = compute(pack, pctx, vector.lines)
    actual: Dict[str, Decimal] = {}
    for line in added:
        actual[line.component] = actual.get(line.component, Decimal(0)) + line.amount
    mismatches = [f"{code}: expected {expected}, got {actual.get(code, Decimal(0))}"
                  for code, expected in sorted(vector.expected.items())
                  if Decimal(expected) != actual.get(code, Decimal(0))]
    codes = tuple(i.code for i in issues)
    mismatches.extend(f"missing issue {code}" for code in vector.expected_issues if code not in codes)
    unexpected = [i.code for i in issues if i.severity == "BLOCKING" and i.code not in vector.expected_issues]
    mismatches.extend(f"unexpected blocking issue {code}" for code in unexpected)
    return VectorResult(vector, not mismatches, {k: str(v) for k, v in sorted(actual.items())}, codes,
                        tuple(mismatches))


# --------------------------------------------------------------------------- helpers


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, str)) and not isinstance(value, bool):
        return Decimal(value)
    raise PackError("INVALID_INPUT", "paid_days and period_days are decimals")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


def _not_verified(ctx: Any, reason: str, message: str, ref: Optional[Mapping[str, Any]]) -> CalcIssue:
    ok = attested(ctx)
    return CalcIssue(
        STATUTORY_NOT_VERIFIED, "WARNING" if ok else "BLOCKING",
        f"{NOT_CALCULATED_TEXT}. {message}" + (
            " An active statutory attestation covers this legal entity." if ok
            else " Finalization needs a verified pack or an approved statutory attestation."),
        {"reason": reason, "pack": dict(ref) if ref else None, "attested": ok, "label": NOT_VERIFIED_LABEL},
    )


def _refused(lines: Tuple[Line, ...], issues: List[CalcIssue], pack: Optional[CountryPack] = None,
             verdict: Optional[PackUsage] = None) -> StatutoryResult:
    return StatutoryResult(lines=lines, added=(), issues=tuple(issues), usage=verdict, pack=pack)


def _preconditions(ctx: Any, pack: CountryPack, ref: Mapping[str, Any]) -> List[CalcIssue]:
    issues: List[CalcIssue] = []
    fingerprint = ref.get("fingerprint")
    if fingerprint and fingerprint != pack.fingerprint():
        issues.append(CalcIssue(PACK_CHANGED, "BLOCKING",
                                f"Pack {pack.version} changed after the snapshot was taken; recalculate the run.",
                                {"pack": pack.version, "snapshot_fingerprint": fingerprint,
                                 "current_fingerprint": pack.fingerprint()}))
    country = ctx_get(ctx, "country")
    if country and country != pack.meta.country:
        issues.append(CalcIssue(PACK_COUNTRY_MISMATCH, "BLOCKING",
                                f"Pack {pack.version} is for {pack.meta.country}, the legal entity is in {country}.",
                                {"pack": pack.version, "country": country}))
    currency = ctx_get(ctx, "currency")
    if currency != pack.meta.currency:
        issues.append(CalcIssue(STATUTORY_CURRENCY_MISMATCH, "BLOCKING",
                                f"Pack {pack.version} calculates in {pack.meta.currency}; this payroll is in "
                                f"{currency}.", {"pack": pack.version, "currency": currency}))
    if not (ctx_get(ctx, "period_start") and ctx_get(ctx, "period_end")):
        issues.append(CalcIssue(STATUTORY_CALCULATION_ERROR, "BLOCKING", "The period dates are missing."))
    return issues


def _worker_class_issue(ctx: Any, pack: CountryPack) -> Optional[CalcIssue]:
    employee = ctx_get(ctx, "employee")
    getter = getattr(employee, "get", None)
    worker_class = getter("worker_class") if callable(getter) else None
    if worker_class not in NON_EMPLOYEE_CLASSES:
        return None
    return CalcIssue(WORKER_CLASS_REVIEW, "BLOCKING",
                     f"The worker class is {worker_class}: pack {pack.version} calculates statutory lines for employees "
                     "only, so none were calculated. Reclassifying the worker is an explicit HR or legal action.",
                     {"pack": pack.version, "worker_class": worker_class}, waivable=True)


def _jurisdiction_issues(pctx: PackContext) -> List[CalcIssue]:
    raw = pctx.profile("tax_jurisdictions")
    if not raw:
        return []
    listed = [str(j) for j in ([raw] if isinstance(raw, str) else raw if isinstance(raw, (list, tuple)) else [raw])]
    if len(set(listed)) < 2:
        return []
    try:
        several = countries.requires_specialist_review(listed)
    except (LookupError, ValueError, TypeError):
        several = True                          # an unrecognised jurisdiction is reviewed, never guessed
    if not several:
        return []
    return [CalcIssue(TAX_SPECIALIST_REVIEW, "WARNING",
                      "Requires specialist review: the tax profile lists more than one tax jurisdiction "
                      f"({', '.join(listed)}). Withholding follows this pack only.", {"tax_jurisdictions": listed})]


def _scheme_issues(pack: CountryPack, pctx: PackContext) -> List[CalcIssue]:
    """A mandatory scheme switched off is an unhandled legal obligation (never silently skipped)."""
    return [CalcIssue(STATUTORY_SCHEME_OFF, "BLOCKING",
                      f"{scheme.name} is mandatory ({scheme.legal_source}) but switched off for this employee.",
                      {"scheme": scheme.code, "setting": scheme.switch_key})
            for scheme in pack.schemes() if scheme.mandatory and not pctx.scheme_on(scheme)]

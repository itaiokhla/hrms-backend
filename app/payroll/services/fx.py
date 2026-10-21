"""FX rates, lookups, conversion records and run snapshots (3.2 §4, 3.9 §3.3; EC-MON-05..09, EC-MON-19).

Rates are "1 base = rate quote". A tenant enters manual rates (PENDING_APPROVAL while
payroll.fx.manual_rate_requires_approval is on, or when the move from the previous rate exceeds
payroll.fx.anomaly_threshold_percent; someone other than the maker approves). Platform operators publish
platform rates (company_id NULL) the same way. A lookup uses the tenant's own rates for a pair when it
has any, else platform rates; the latest rate on or before the date must be ACTIVE and not older than
payroll.fx.max_rate_age_hours, otherwise the lookup is blocked (never an older rate, never 1:1, EC-MON-05/06).
A MID pair with no rate is derived from its inverse or a cross through USD or EUR; the derived rate is
stored as a DERIVED fx_rates row of the tenant with its legs in derived_from, so each conversion
(fx_conversions) names the exact rate it used. Snapshots copy the rates a run used, so later rate
changes never alter it (EC-MON-09). Pure rules live in app.payroll.fx.
"""

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, List, Optional, Sequence

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, conflict, not_found
from app.core.principal import Principal
from app.database.db import TenantContextError, current_company_id
from app.models.catalogue import FxConversion, FxRate, FxSnapshot, FxSnapshotRate
from app.models.payroll_config import PlatformSettingValue
from app.payroll import audit, currencies
from app.payroll import fx as fxlib
from app.payroll import settings_registry as registry
from app.payroll.money import Money, MoneyError, decimal_str
from app.payroll.services import settings as settings_engine
from app.services.audit_log_service import AuditLogService

PIVOTS = ("USD", "EUR")
ACTIVE, PENDING, REJECTED = "ACTIVE", "PENDING_APPROVAL", "REJECTED"
PLATFORM_SOURCE = "PLATFORM"
FX_KEYS = (
    "payroll.fx.provider", "payroll.fx.rate_type", "payroll.fx.max_rate_age_hours",
    "payroll.fx.anomaly_threshold_percent", "payroll.fx.manual_rate_requires_approval",
    "payroll.fx.conversion_date_rule",
)


RATE_QUANTUM = Decimal(1).scaleb(-fxlib.RATE_SCALE)


def rate_str(value) -> str:
    """A rate at its stored scale (NUMERIC(24,12)), the same before and after a database round trip."""
    return decimal_str(Decimal(value).quantize(RATE_QUANTUM))


@dataclass(frozen=True)
class FxSettings:
    provider: str
    rate_type: str
    max_rate_age_hours: int
    anomaly_threshold_percent: Decimal
    manual_rate_requires_approval: bool
    conversion_date_rule: str


# ---------------------------------------------------------------------------------------------- settings
def settings_for(db: Session, company_id: int, on: Optional[date] = None) -> FxSettings:
    """The tenant's FX settings on `on`, through the settings engine (an invalid stored value fails closed)."""
    on = on or clock.utcnow().date()
    try:
        resolved = settings_engine.resolve_many(db, company_id, FX_KEYS, on=on)
    except settings_engine.SettingConfigError as exc:
        raise AppError(409, exc.message, exc.code)
    values = {key.rsplit(".", 1)[1]: resolved[key].value for key in FX_KEYS}
    return FxSettings(
        provider=values["provider"],
        rate_type=values["rate_type"],
        max_rate_age_hours=int(values["max_rate_age_hours"]),
        anomaly_threshold_percent=Decimal(str(values["anomaly_threshold_percent"])),
        manual_rate_requires_approval=bool(values["manual_rate_requires_approval"]),
        conversion_date_rule=values["conversion_date_rule"],
    )


def platform_settings(db: Session, on: Optional[date] = None) -> FxSettings:
    """FX settings for platform rates: PLATFORM-level values in force on `on`, else registry defaults."""
    on = on or clock.utcnow().date()
    rows = (
        db.query(PlatformSettingValue)
        .filter(
            PlatformSettingValue.level == "PLATFORM", PlatformSettingValue.key.in_(FX_KEYS),
            PlatformSettingValue.effective_from <= on,
            or_(PlatformSettingValue.effective_to.is_(None), PlatformSettingValue.effective_to >= on),
        )
        .order_by(PlatformSettingValue.effective_from)
        .all()
    )
    values = {key: registry.definition(key).default for key in FX_KEYS}
    for row in rows:
        try:
            values[row.key] = registry.validate_value(row.key, row.value)
        except registry.SettingValidationError as exc:
            raise AppError(409, f"Platform setting {row.key} is invalid: {exc.message}", "CONFIG_INVALID")
    short = {key.rsplit(".", 1)[1]: value for key, value in values.items()}
    # Platform rates are entered by operators, whose own maker-checker applies to anomalies only.
    return FxSettings(short["provider"], short["rate_type"], int(short["max_rate_age_hours"]),
                      Decimal(str(short["anomaly_threshold_percent"])), False, short["conversion_date_rule"])


# ---------------------------------------------------------------------------------------------- rows
def _derivation(record: dict) -> fxlib.Derivation:
    legs = tuple(_rate_from_record(leg) for leg in record["legs"])
    return fxlib.Derivation(record["method"], legs, Decimal(record["unrounded"]), record.get("pivot"))


def _rate_from_record(record: dict) -> fxlib.Rate:
    """A Rate from its to_record() form (the legs stored in derived_from)."""
    fetched = record.get("fetched_at")
    return fxlib.Rate(
        base=record["base_currency"], quote=record["quote_currency"], rate=Decimal(record["rate"]),
        rate_type=record["rate_type"], source=record["source"], as_of=date.fromisoformat(record["as_of"]),
        fetched_at=datetime.fromisoformat(fetched) if fetched else None, status=record["status"],
        rate_id=record.get("fx_rate_id"),
        derivation=_derivation(record["derived_from"]) if record.get("derived_from") else None,
    )


def to_rate(row: FxRate) -> fxlib.Rate:
    derived = row.source == fxlib.DERIVED_SOURCE and row.derived_from
    return fxlib.Rate(
        base=row.base_currency, quote=row.quote_currency, rate=Decimal(row.rate), rate_type=row.rate_type,
        source=row.source, as_of=row.as_of, fetched_at=row.fetched_at, status=row.status, rate_id=row.id,
        derivation=_derivation(row.derived_from) if derived else None,
    )


def serialize(row: FxRate, *, for_platform: bool = False) -> dict:
    platform = row.company_id is None
    return {
        "id": row.id,
        "scope": "PLATFORM" if platform else "TENANT",
        "base_currency": row.base_currency,
        "quote_currency": row.quote_currency,
        "rate": rate_str(row.rate),
        "quotation": f"1 {row.base_currency} = {rate_str(row.rate)} {row.quote_currency}",
        "rate_type": row.rate_type,
        "source": row.source,
        "as_of": row.as_of,
        "fetched_at": row.fetched_at,
        "status": row.status,
        "derived_from": row.derived_from,
        # Platform operators' ids are not shown to tenants.
        "created_by_user_id": row.created_by_user_id if for_platform or not platform else None,
        "approved_by_user_id": row.approved_by_user_id if for_platform or not platform else None,
    }


def _pair_rows(db: Session, company_id: Optional[int], base: str, quote: str, rate_type: str) -> List[FxRate]:
    """Entered (non-derived) rates of a pair: the tenant's own when it has any usable row, else the platform's."""
    query = db.query(FxRate).filter(
        FxRate.base_currency == base, FxRate.quote_currency == quote, FxRate.rate_type == rate_type,
        FxRate.source != fxlib.DERIVED_SOURCE,
    )
    if company_id is not None:
        own = query.filter(FxRate.company_id == company_id).all()
        if any(row.status != REJECTED for row in own):
            return own
    return query.filter(FxRate.company_id.is_(None)).all()


def _lookup_pair(db, company_id, base, quote, on, rate_type, max_age) -> fxlib.RateLookup:
    rows = _pair_rows(db, company_id, base, quote, rate_type)
    return fxlib.lookup_rate([to_rate(r) for r in rows], base, quote, on=on, rate_type=rate_type, max_age_hours=max_age)


def _leg(db, company_id, a, b, on, max_age) -> fxlib.RateLookup:
    """A MID leg between a and b, as stored in either direction (no rounding here)."""
    direct = _lookup_pair(db, company_id, a, b, on, "MID", max_age)
    if direct.ok or direct.issue.code != "RATE_UNAVAILABLE":
        return direct
    reverse = _lookup_pair(db, company_id, b, a, on, "MID", max_age)
    if reverse.ok or reverse.issue.code != "RATE_UNAVAILABLE":
        return reverse
    return direct


def lookup(db: Session, company_id: Optional[int], base: str, quote: str, on: date, *,
           rate_type: Optional[str] = None, max_age_hours: Optional[int] = None) -> fxlib.RateLookup:
    """The usable rate for base->quote on `on`, or the blocking issue as data (RATE_UNAVAILABLE, RATE_STALE ...).

    Derived (inverse/cross) rates are returned unsaved; `convert` persists the one it uses.
    """
    cfg = settings_for(db, company_id, on) if company_id is not None else platform_settings(db, on)
    rate_type = rate_type or cfg.rate_type
    max_age = cfg.max_rate_age_hours if max_age_hours is None else max_age_hours
    direct = _lookup_pair(db, company_id, base, quote, on, rate_type, max_age)
    if direct.ok or direct.issue.code != "RATE_UNAVAILABLE" or rate_type != "MID":
        return direct
    reverse = _lookup_pair(db, company_id, quote, base, on, "MID", max_age)
    if reverse.ok:
        return fxlib.RateLookup(fxlib.inverse(reverse.rate), None)
    if reverse.issue.code != "RATE_UNAVAILABLE":
        return reverse
    for pivot in PIVOTS:
        if pivot in (base, quote):
            continue
        first = _leg(db, company_id, base, pivot, on, max_age)
        if not first.ok:
            if first.issue.code != "RATE_UNAVAILABLE":
                return first
            continue
        second = _leg(db, company_id, pivot, quote, on, max_age)
        if not second.ok:
            if second.issue.code != "RATE_UNAVAILABLE":
                return second
            continue
        return fxlib.RateLookup(fxlib.cross(first.rate, second.rate, pivot), None)
    return direct


def require_rate(db: Session, company_id: Optional[int], base: str, quote: str, on: date, **kwargs) -> fxlib.Rate:
    found = lookup(db, company_id, base, quote, on, **kwargs)
    if not found.ok:
        if found.issue.code == "RATE_UNAVAILABLE":
            raise fxlib.RateUnavailable(base, quote, f"none usable on {on}")
        raise fxlib.FxError(found.issue.code, found.issue.message)
    return found.rate


def _persist_derived(db: Session, company_id: int, rate: fxlib.Rate) -> FxRate:
    record = rate.derivation.to_record()
    existing = (
        db.query(FxRate)
        .filter(
            FxRate.company_id == company_id, FxRate.source == fxlib.DERIVED_SOURCE,
            FxRate.base_currency == rate.base, FxRate.quote_currency == rate.quote, FxRate.rate_type == rate.rate_type,
            FxRate.as_of == rate.as_of, FxRate.rate == rate.rate,
        )
        .all()
    )
    for row in existing:
        if row.derived_from == record:
            return row
    row = FxRate(
        company_id=company_id, base_currency=rate.base, quote_currency=rate.quote, rate=rate.rate,
        rate_type=rate.rate_type, source=fxlib.DERIVED_SOURCE, as_of=rate.as_of,
        fetched_at=rate.fetched_at or clock.utcnow(), status=ACTIVE, derived_from=record,
    )
    db.add(row)
    db.flush()
    return row


def _tenant(db: Session) -> int:
    company_id = current_company_id(db)
    if company_id is None:
        raise TenantContextError("FX conversions and snapshots belong to a tenant")
    return company_id


def convert(db: Session, money: Money, target: str, *, on: date, purpose: str, ref_type: Optional[str] = None,
            ref_id: Optional[int] = None, mode: str = "HALF_UP", rate_type: Optional[str] = None) -> FxConversion:
    """Convert and store the conversion record; a missing, stale or unapproved rate raises (never 1:1)."""
    company_id = _tenant(db)
    if money.currency == target:
        raise fxlib.FxError("SAME_CURRENCY", f"{target} amounts need no conversion and never get a 1:1 rate.")
    cfg = settings_for(db, company_id, on)
    rate = require_rate(db, company_id, money.currency, target, on, rate_type=rate_type)
    if rate.derivation is not None:
        rate = replace(rate, rate_id=_persist_derived(db, company_id, rate).id)
    conversion = fxlib.convert(money, rate, mode, purpose=purpose, target=target, conversion_date=on,
                               max_age_hours=cfg.max_rate_age_hours)
    record = conversion.to_record()
    row = FxConversion(
        source_amount=Decimal(record["source_amount"]), source_currency=record["source_currency"],
        target_currency=record["target_currency"], purpose=purpose, fx_rate_id=rate.rate_id,
        rate=Decimal(record["rate"]), quotation=record["quotation"], rate_date=conversion.rate_date,
        rounding_mode=mode, result_amount=Decimal(record["result_amount"]), ref_type=ref_type, ref_id=ref_id,
    )
    db.add(row)
    db.flush()
    return row


def serialize_conversion(row: FxConversion) -> dict:
    return {
        "id": row.id,
        "source": Money(Decimal(row.source_amount), row.source_currency).to_json(),
        "result": Money(Decimal(row.result_amount), row.target_currency).to_json(),
        "purpose": row.purpose,
        "fx_rate_id": row.fx_rate_id,
        "rate": rate_str(row.rate),
        "quotation": row.quotation,
        "rate_date": row.rate_date,
        "rounding_mode": row.rounding_mode,
        "ref_type": row.ref_type,
        "ref_id": row.ref_id,
    }


def capture_snapshot(db: Session, rate_ids: Iterable[int], *, purpose: str = "PAYROLL_RUN",
                     run_id: Optional[int] = None) -> int:
    """Freeze copies of ACTIVE rates (platform or this tenant's) for a run; returns the snapshot id."""
    company_id = _tenant(db)
    ids = sorted(set(rate_ids))
    rows = db.query(FxRate).filter(FxRate.id.in_(ids)).all() if ids else []
    found = {row.id: row for row in rows if row.company_id in (None, company_id)}
    missing = [i for i in ids if i not in found]
    if missing:
        raise fxlib.FxError("RATE_NOT_FOUND", f"FX rate(s) {missing} do not exist.")
    unusable = [i for i in ids if found[i].status != ACTIVE]
    if unusable:
        raise fxlib.FxError("RATE_NOT_APPROVED", f"FX rate(s) {unusable} are not approved.")
    pairs = [(r.base_currency, r.quote_currency, r.rate_type) for r in found.values()]
    if len(set(pairs)) != len(pairs):
        raise fxlib.FxError("RATE_AMBIGUOUS", "A snapshot holds one rate per currency pair and rate type.")
    snapshot = FxSnapshot(run_id=run_id, purpose=purpose[:40], captured_at=clock.utcnow())
    db.add(snapshot)
    db.flush()
    for rate_id in ids:
        row = found[rate_id]
        db.add(FxSnapshotRate(
            snapshot_id=snapshot.id, fx_rate_id=row.id, base_currency=row.base_currency,
            quote_currency=row.quote_currency, rate=row.rate, rate_type=row.rate_type, source=row.source,
            as_of=row.as_of,
        ))
    db.flush()
    return snapshot.id


def snapshot_rates(db: Session, snapshot_id: int) -> List[fxlib.Rate]:
    """The frozen rates of a snapshot of this tenant (unknown or another tenant's: 404)."""
    company_id = _tenant(db)
    snapshot = db.query(FxSnapshot).filter(FxSnapshot.id == snapshot_id, FxSnapshot.company_id == company_id).first()
    if snapshot is None:
        raise not_found("FX snapshot")
    rows = db.query(FxSnapshotRate).filter(FxSnapshotRate.snapshot_id == snapshot.id).order_by(FxSnapshotRate.id).all()
    derived_ids = [r.fx_rate_id for r in rows if r.source == fxlib.DERIVED_SOURCE]
    legs = {r.id: r.derived_from for r in db.query(FxRate).filter(FxRate.id.in_(derived_ids))} if derived_ids else {}
    return [
        fxlib.Rate(base=r.base_currency, quote=r.quote_currency, rate=Decimal(r.rate), rate_type=r.rate_type,
                   source=r.source, as_of=r.as_of, status=ACTIVE, rate_id=r.fx_rate_id,
                   derivation=_derivation(legs[r.fx_rate_id]) if r.fx_rate_id in legs else None)
        for r in rows
    ]


# ---------------------------------------------------------------------------------------------- entering rates
def _validated(base: str, quote: str, rate: Decimal, rate_type: str, as_of: date, source: str) -> fxlib.Rate:
    for code in (base, quote):
        try:
            currencies.require_money_currency(code)
        except MoneyError as exc:
            raise AppError(422, exc.message, exc.code)
    candidate = fxlib.Rate(base=base, quote=quote, rate=rate, rate_type=rate_type, source=source, as_of=as_of,
                           fetched_at=clock.utcnow(), status=PENDING)
    try:
        return fxlib.validate_rate(candidate)
    except MoneyError as exc:
        raise AppError(422, exc.message, exc.code)


def _previous(rows: Sequence[FxRate], new: fxlib.Rate) -> Optional[fxlib.Rate]:
    earlier = [r for r in rows if r.status == ACTIVE and r.as_of <= new.as_of]
    if not earlier:
        return None
    latest = max(earlier, key=lambda r: (r.as_of, r.fetched_at, r.id))
    return to_rate(latest)


def _status(new: fxlib.Rate, previous: Optional[fxlib.Rate], *, manual_approval: bool, threshold: Decimal):
    reasons = fxlib.approval_reasons(new, previous=previous, manual_rate_requires_approval=manual_approval,
                                     anomaly_threshold_percent=threshold)
    warnings = []
    if previous is not None and fxlib.assess_move(previous, new, threshold).possibly_inverted:
        warnings.append("POSSIBLY_INVERTED")
    return (PENDING if reasons else ACTIVE), list(reasons), warnings


def create_manual_rate(db: Session, principal: Principal, *, base: str, quote: str, rate: Decimal, rate_type: str,
                       as_of: date) -> dict:
    """A tenant's manual rate (EC-MON-07 validation; EC-MON-08 approval for manual or anomalous rates)."""
    candidate = _validated(base, quote, rate, rate_type, as_of, fxlib.MANUAL_SOURCE)
    cfg = settings_for(db, principal.company_id, as_of)
    own = db.query(FxRate).filter(
        FxRate.company_id == principal.company_id, FxRate.base_currency == base, FxRate.quote_currency == quote,
        FxRate.rate_type == rate_type, FxRate.source != fxlib.DERIVED_SOURCE,
    ).all()
    platform = db.query(FxRate).filter(
        FxRate.company_id.is_(None), FxRate.base_currency == base, FxRate.quote_currency == quote,
        FxRate.rate_type == rate_type,
    ).all()
    previous = _previous(own, candidate) or _previous(platform, candidate)
    status, reasons, warnings = _status(candidate, previous, manual_approval=cfg.manual_rate_requires_approval,
                                        threshold=cfg.anomaly_threshold_percent)
    row = FxRate(
        company_id=principal.company_id, base_currency=base, quote_currency=quote, rate=candidate.rate,
        rate_type=rate_type, source=fxlib.MANUAL_SOURCE, as_of=as_of, fetched_at=candidate.fetched_at,
        status=status, created_by_user_id=principal.user_id,
    )
    db.add(row)
    db.flush()
    AuditLogService.log(
        db, principal, audit.PAYROLL_SETTINGS, "FX_RATE_CREATED",
        f"Manual FX rate {row.base_currency}/{row.quote_currency} for {as_of} entered ({status})", "fx_rate", row.id,
        details={"base_currency": base, "quote_currency": quote, "rate": rate_str(candidate.rate),
                 "rate_type": rate_type, "as_of": as_of, "status": status, "approval_reasons": reasons,
                 "previous_rate_id": previous.rate_id if previous else None, "warnings": warnings},
    )
    return {**serialize(row), "approval_reasons": reasons, "warnings": warnings}


def _decide(db: Session, row: Optional[FxRate], actor_id: int, approve: bool) -> FxRate:
    if row is None:
        raise not_found("FX rate")
    if row.status != PENDING:
        raise conflict("This FX rate is not waiting for approval.", "FX_RATE_NOT_PENDING")
    if approve:
        try:
            fxlib.check_approver(row.created_by_user_id, actor_id)
        except fxlib.FxError as exc:
            raise AppError(403, exc.message, exc.code)
        row.status = ACTIVE
        row.approved_by_user_id = actor_id
    else:
        row.status = REJECTED
    row.updated_at = clock.utcnow()
    db.flush()
    return row


def decide_manual_rate(db: Session, principal: Principal, rate_id: int, *, approve: bool) -> dict:
    """Approve (someone other than the maker, EC-MON-08) or reject a PENDING tenant rate."""
    row = (
        db.query(FxRate)
        .filter(FxRate.id == rate_id, FxRate.company_id == principal.company_id)
        .with_for_update()
        .one_or_none()
    )
    row = _decide(db, row, principal.user_id, approve)
    action = "FX_RATE_APPROVED" if approve else "FX_RATE_REJECTED"
    AuditLogService.log(
        db, principal, audit.PAYROLL_SETTINGS, action,
        f"FX rate {row.base_currency}/{row.quote_currency} for {row.as_of} {'approved' if approve else 'rejected'}",
        "fx_rate", row.id,
        details={"base_currency": row.base_currency, "quote_currency": row.quote_currency,
                 "rate": rate_str(row.rate), "as_of": row.as_of, "maker_user_id": row.created_by_user_id},
    )
    return serialize(row)


def get_visible(db: Session, company_id: int, rate_id: int) -> FxRate:
    row = db.query(FxRate).filter(
        FxRate.id == rate_id, or_(FxRate.company_id.is_(None), FxRate.company_id == company_id)
    ).one_or_none()
    if row is None:
        raise not_found("FX rate")
    return row


def list_rates(db: Session, company_id: Optional[int], *, base: Optional[str] = None, quote: Optional[str] = None,
               on: Optional[date] = None, status: Optional[str] = None, platform_only: bool = False,
               limit: int = 200) -> List[FxRate]:
    query = db.query(FxRate)
    if platform_only or company_id is None:
        query = query.filter(FxRate.company_id.is_(None))
    else:
        query = query.filter(or_(FxRate.company_id.is_(None), FxRate.company_id == company_id))
    if base:
        query = query.filter(FxRate.base_currency == base)
    if quote:
        query = query.filter(FxRate.quote_currency == quote)
    if on:
        query = query.filter(FxRate.as_of <= on)
    if status:
        query = query.filter(FxRate.status == status)
    return query.order_by(FxRate.as_of.desc(), FxRate.id.desc()).limit(limit).all()


# ---------------------------------------------------------------------------------------------- platform rates
def create_platform_rate(db: Session, admin_id: int, *, base: str, quote: str, rate: Decimal, rate_type: str,
                         as_of: date, source: str) -> dict:
    """A platform rate (system session); an anomalous move waits for another operator's approval."""
    candidate = _validated(base, quote, rate, rate_type, as_of, source)
    cfg = platform_settings(db, as_of)
    existing = db.query(FxRate).filter(
        FxRate.company_id.is_(None), FxRate.base_currency == base, FxRate.quote_currency == quote,
        FxRate.rate_type == rate_type,
    ).all()
    status, reasons, warnings = _status(candidate, _previous(existing, candidate), manual_approval=False,
                                        threshold=cfg.anomaly_threshold_percent)
    row = FxRate(
        company_id=None, base_currency=base, quote_currency=quote, rate=candidate.rate, rate_type=rate_type,
        source=source, as_of=as_of, fetched_at=candidate.fetched_at, status=status, created_by_user_id=admin_id,
    )
    db.add(row)
    db.flush()
    return {**serialize(row, for_platform=True), "approval_reasons": reasons, "warnings": warnings}


def decide_platform_rate(db: Session, admin_id: int, rate_id: int, *, approve: bool) -> dict:
    row = db.query(FxRate).filter(FxRate.id == rate_id, FxRate.company_id.is_(None)).with_for_update().one_or_none()
    return serialize(_decide(db, row, admin_id, approve), for_platform=True)

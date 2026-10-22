"""Compliance page: per legal entity capability, packs, attestation, registrations, liabilities, due dates
(3.8 §5.15, 3.2 §6.2, §8.1, §8.3; decisions I-07, I-15).

For every legal entity in the caller's scope:

* **country** and the **capability badge**. The level comes from the country's packs as they may be
  used in this environment (``packs.country_capability``: a verified version is VERIFIED, a fixture or a
  version under review is at most CALC_SUPPORTED, a draft is CONFIG_ONLY) and, when the platform recorded
  a level in ``country_capabilities``, the lower of the two: a recorded level can restrict a country but
  never upgrades what the packs support. The badge says whether statutory lines are production
  (verified), fixture ("FIXTURE — not law") or not available ("Statutory calculations not verified").
* the **pack versions** of the country with their lifecycle status, usage here, reviewer, review date
  and the next source review due.
* the **attestation status** (decision I-15): required when no pack may produce final statutory lines
  for the entity; ACTIVE, PENDING_APPROVAL, MISSING or NOT_REQUIRED.
* the **registrations** printed on documents (``legal_entities.registration_lines``).
* **statutory liabilities** and their remittance status per currency (never summed across currencies),
  overdue counts, and the **due-date calendar** published by a usable pack: contributions and reports
  with their due dates and current status. A draft or refused pack publishes no due dates.

Liabilities are written by finalization; recording a remittance (OPEN → REMITTED, a reference is
required) and reconciling it (REMITTED → RECONCILED) go through ``liability_transition`` with
``payroll:statutory:submit`` at the entity, a row lock, ``row_version`` and an audit event.

Services take (db, principal, ...) and never commit.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.catalogue import CountryCapability
from app.models.org import LegalEntity
from app.models.pay_runs import PayPeriod, PayrollRun, StatutoryAttestation, StatutoryLiability
from app.models.reports_compliance import StatutoryReport
from app.payroll import countries, packs
from app.payroll.money import amount_str
from app.payroll.packs.base import CAPABILITY_LABELS, FIXTURE_LABEL, NOT_VERIFIED_LABEL, Capability
from app.payroll.services import access
from app.payroll.services import statutory_reports as reports_service
from app.services.audit_log_service import AuditLogService

READ_PERMISSIONS = reports_service.READ_PERMISSIONS
SUBMIT = reports_service.SUBMIT
AUDIT_MODULE = reports_service.AUDIT_MODULE
LIABILITY_SUBJECT = "statutory_liability"
_ZERO = Decimal("0")

# The liability lifecycle (contract vocabulary of payroll_runs.LIABILITY_STATUSES).
LIABILITY_TRANSITIONS: Dict[Tuple[str, str], str] = {
    ("OPEN", "RECORD_REMITTANCE"): "REMITTED",
    ("REMITTED", "RECONCILE"): "RECONCILED",
}
ATTESTATION_TEXT = ("No verified country pack covers this entity: payroll calculates without statutory lines and "
                    "finalizes only with an approved attestation that statutory obligations are handled outside "
                    "this system (maker-checker).")


def _today(db: Session, company_id: int) -> date:
    return clock.company_today(db, company_id)


def entity_clause(principal: Principal, column=None):
    return reports_service.scope_clause(principal, READ_PERMISSIONS, column if column is not None
                                        else LegalEntity.id)


def _entities(db: Session, principal: Principal, legal_entity_id: Optional[int]) -> List[LegalEntity]:
    if not reports_service._held(principal, READ_PERMISSIONS):
        raise forbidden()
    query = db.query(LegalEntity).filter(LegalEntity.company_id == principal.company_id, entity_clause(principal))
    if legal_entity_id is not None:
        query = query.filter(LegalEntity.id == legal_entity_id)
        found = query.all()
        if not found:
            raise not_found("Legal entity")
        return found
    return query.order_by(LegalEntity.is_default.desc(), LegalEntity.code).all()


# --------------------------------------------------------------------------- capability


def capability_badge(db: Session, country_code: str, on: date) -> dict:
    env = reports_service.environment()
    from_packs = packs.country_capability(country_code, on, environment=env)
    recorded_row = db.get(CountryCapability, country_code)
    recorded = None
    if recorded_row is not None:
        try:
            recorded = Capability(recorded_row.capability_level)
        except ValueError:
            recorded = Capability.UNSUPPORTED
    level = min(from_packs, recorded, key=lambda c: c.rank) if recorded is not None else from_packs
    active = reports_service.entity_pack(country_code, on, on)
    if active is not None and active.usage.mode == packs.VERIFIED and level.rank >= Capability.VERIFIED.rank:
        kind, tone, note = "PRODUCTION", "success", "Statutory lines are verified for the period."
    elif active is not None and active.usage.mode == packs.FIXTURE:
        kind, tone, note = "FIXTURE", "warning", f"{FIXTURE_LABEL}: statutory lines use test data."
    elif level == Capability.UNSUPPORTED:
        kind, tone, note = "UNSUPPORTED", "danger", "This country is not offered for payroll."
    else:
        kind, tone, note = "NOT_VERIFIED", "warning", f"Statutory calculations not verified ({NOT_VERIFIED_LABEL})."
    return {
        "level": level.value, "label": CAPABILITY_LABELS[level], "kind": kind, "tone": tone, "note": note,
        "from_packs": from_packs.value, "recorded": recorded.value if recorded is not None else None,
        "recorded_note": recorded_row.note if recorded_row is not None else None, "environment": env,
    }


def _next_review_due(pack) -> Optional[date]:
    dates = [e.next_review_due for e in pack.manifest.entries if e.next_review_due]
    return min(dates) if dates else None


def pack_versions(country_code: str, on: date) -> List[dict]:
    env = reports_service.environment()
    result = []
    for pack in packs.packs_for_country(country_code, environment=env):
        meta = pack.meta
        verdict = packs.usage(meta, environment=env, period=(on, on) if meta.covers(on) else None)
        result.append({
            "code": meta.code, "version": meta.version, "name": meta.name, "status": meta.status.value,
            "effective_from": meta.effective_from, "effective_to": meta.effective_to, "covers_today": meta.covers(on),
            "usage": verdict.mode, "usage_message": verdict.message, "is_fixture": meta.is_fixture,
            "capability": packs.capability(meta, environment=env).value, "reviewer": meta.reviewer,
            "reviewed_on": meta.reviewed_on, "next_review_due": _next_review_due(pack),
            "watermark": FIXTURE_LABEL if meta.is_fixture else None,
            "defines_reports": bool(pack.reports()),
        })
    return result


# --------------------------------------------------------------------------- attestation


def attestation_status(db: Session, entity: LegalEntity, on: date, required: bool) -> dict:
    rows = (db.query(StatutoryAttestation)
            .filter(StatutoryAttestation.company_id == entity.company_id,
                    StatutoryAttestation.legal_entity_id == entity.id,
                    StatutoryAttestation.status.in_(("ACTIVE", "PENDING")))
            .order_by(StatutoryAttestation.effective_from.desc(), StatutoryAttestation.id.desc())
            .all())

    def covers(row: StatutoryAttestation) -> bool:
        return row.effective_from <= on and (row.effective_to is None or row.effective_to >= on)

    active = next((r for r in rows if r.status == "ACTIVE" and covers(r)), None)
    pending = next((r for r in rows if r.status == "PENDING"), None)
    if active is not None:
        status = "ACTIVE"
    elif pending is not None:
        status = "PENDING_APPROVAL"
    else:
        status = "MISSING" if required else "NOT_REQUIRED"

    def brief(row: Optional[StatutoryAttestation]) -> Optional[dict]:
        if row is None:
            return None
        return {"id": row.id, "status": row.status, "schemes": list(row.schemes or []),
                "effective_from": row.effective_from, "effective_to": row.effective_to,
                "attested_by_user_id": row.attested_by_user_id, "approved_by_user_id": row.approved_by_user_id,
                "approved_at": row.approved_at}

    return {"required": required, "status": status, "active": brief(active), "pending": brief(pending),
            "message": ATTESTATION_TEXT if required else None,
            "blocks_finalization": required and active is None}


# --------------------------------------------------------------------------- liabilities


def _liability_query(db: Session, principal: Principal):
    return (
        db.query(StatutoryLiability, PayrollRun.number, PayrollRun.status, PayPeriod.period_start, PayPeriod.period_end,
                 PayPeriod.pay_date)
        .join(PayrollRun, and_(PayrollRun.company_id == StatutoryLiability.company_id,
                               PayrollRun.id == StatutoryLiability.run_id))
        .join(PayPeriod, and_(PayPeriod.company_id == PayrollRun.company_id, PayPeriod.id == PayrollRun.pay_period_id))
        .filter(StatutoryLiability.company_id == principal.company_id,
                entity_clause(principal, StatutoryLiability.legal_entity_id))
    )


def liability_json(row: StatutoryLiability, run_number: str, run_status: str, period_start: date, period_end: date,
                   pay_date: date, today: date) -> dict:
    total = Decimal(row.employee_amount) + Decimal(row.employer_amount)
    return {
        "id": row.id, "run_id": row.run_id, "run_number": run_number, "run_status": run_status,
        "legal_entity_id": row.legal_entity_id, "scheme": row.scheme, "payee": row.payee_authority,
        "employee_amount": {"amount": amount_str(Decimal(row.employee_amount), row.currency), "currency": row.currency},
        "employer_amount": {"amount": amount_str(Decimal(row.employer_amount), row.currency), "currency": row.currency},
        "total": {"amount": amount_str(total, row.currency), "currency": row.currency},
        "period_start": period_start, "period_end": period_end, "pay_date": pay_date, "due_date": row.due_date,
        "status": row.status, "remittance_ref": row.remittance_ref,
        "remitted_on": (row.details or {}).get("remitted_on"),
        "overdue": row.status == "OPEN" and row.due_date is not None and row.due_date < today,
        "fixture": row.fixture, "watermark": FIXTURE_LABEL if row.fixture else None, "row_version": row.row_version,
    }


def list_liabilities(db: Session, principal: Principal, *, legal_entity_id: Optional[int] = None,
                     status: Optional[str] = None, scheme: Optional[str] = None, currency: Optional[str] = None,
                     due_from: Optional[date] = None, due_to: Optional[date] = None, overdue: Optional[bool] = None,
                     skip: int = 0, limit: int = 50) -> dict:
    if not reports_service._held(principal, READ_PERMISSIONS):
        raise forbidden()
    today = _today(db, principal.company_id)
    query = _liability_query(db, principal)
    if legal_entity_id is not None:
        query = query.filter(StatutoryLiability.legal_entity_id == legal_entity_id)
    if status:
        query = query.filter(StatutoryLiability.status == status)
    if scheme:
        query = query.filter(StatutoryLiability.scheme == scheme)
    if currency:
        query = query.filter(StatutoryLiability.currency == currency)
    if due_from is not None:
        query = query.filter(StatutoryLiability.due_date >= due_from)
    if due_to is not None:
        query = query.filter(StatutoryLiability.due_date <= due_to)
    if overdue is True:
        query = query.filter(StatutoryLiability.status == "OPEN", StatutoryLiability.due_date < today)
    rows = query.order_by(StatutoryLiability.due_date.asc().nullslast(), StatutoryLiability.id).all()
    totals: Dict[str, Dict[str, Decimal]] = {}
    for liability, *_ in rows:
        bucket = totals.setdefault(liability.currency, {})
        amount = Decimal(liability.employee_amount) + Decimal(liability.employer_amount)
        bucket[liability.status] = bucket.get(liability.status, _ZERO) + amount
    page = rows[skip:skip + limit]
    return {
        "total": len(rows),
        "items": [liability_json(*row, today=today) for row in page],
        "totals": {cur: {st: amount_str(v, cur) for st, v in sorted(b.items())} for cur, b in sorted(totals.items())},
    }


def liability_summary(db: Session, principal: Principal, entity: LegalEntity, today: date) -> dict:
    rows = (db.query(StatutoryLiability)
            .filter(StatutoryLiability.company_id == entity.company_id,
                    StatutoryLiability.legal_entity_id == entity.id).all())
    by_currency: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        amount = Decimal(row.employee_amount) + Decimal(row.employer_amount)
        bucket = by_currency.setdefault(row.currency, {"OPEN": _ZERO, "REMITTED": _ZERO, "RECONCILED": _ZERO,
                                                       "overdue": _ZERO, "overdue_count": 0, "count": 0})
        bucket[row.status] += amount
        bucket["count"] += 1
        if row.status == "OPEN" and row.due_date is not None and row.due_date < today:
            bucket["overdue"] += amount
            bucket["overdue_count"] += 1
    next_due = min((r.due_date for r in rows if r.status == "OPEN" and r.due_date and r.due_date >= today),
                   default=None)
    return {
        "per_currency": {
            cur: {"open": amount_str(b["OPEN"], cur), "remitted": amount_str(b["REMITTED"], cur),
                  "reconciled": amount_str(b["RECONCILED"], cur), "overdue": amount_str(b["overdue"], cur),
                  "overdue_count": b["overdue_count"], "count": b["count"]}
            for cur, b in sorted(by_currency.items())
        },
        "open_count": sum(1 for r in rows if r.status == "OPEN"),
        "overdue_count": sum(b["overdue_count"] for b in by_currency.values()),
        "next_due_date": next_due,
    }


def _load_liability(db: Session, principal: Principal, liability_id: int) -> StatutoryLiability:
    if not reports_service._held(principal, READ_PERMISSIONS + (SUBMIT,)):
        raise forbidden()
    row = (db.query(StatutoryLiability)
           .filter(StatutoryLiability.company_id == principal.company_id, StatutoryLiability.id == liability_id,
                   reports_service.scope_clause(principal, READ_PERMISSIONS + (SUBMIT,),
                                                StatutoryLiability.legal_entity_id))
           .with_for_update().populate_existing().first())
    if row is None:
        raise not_found("Statutory liability")
    return row


def liability_transition(db: Session, principal: Principal, liability_id: int, action: str, *,
                         remittance_ref: Optional[str] = None, remitted_on: Optional[date] = None,
                         note: Optional[str] = None, expected_version: Optional[int] = None) -> StatutoryLiability:
    """The ONLY writer of ``statutory_liabilities.status`` after finalization."""
    row = _load_liability(db, principal, liability_id)
    access.ensure_in_scope(principal, SUBMIT, row.legal_entity_id, None, "Statutory liability")
    if expected_version is not None and expected_version != row.row_version:
        raise conflict("The liability was changed by someone else; reload it and try again.", "ROW_VERSION_CONFLICT")
    target = LIABILITY_TRANSITIONS.get((row.status, action))
    if target is None:
        raise conflict(f"A liability that is {row.status} cannot be {action.lower().replace('_', ' ')}ed.",
                       "INVALID_TRANSITION")
    before = row.status
    details = dict(row.details or {})
    now = clock.utcnow()
    if action == "RECORD_REMITTANCE":
        ref = (remittance_ref or "").strip()[:100]
        if not ref:
            raise bad_request("Recording a remittance needs the payment or challan reference.",
                              "REMITTANCE_REF_REQUIRED")
        paid_on = remitted_on or _today(db, row.company_id)
        if paid_on > _today(db, row.company_id):
            raise bad_request("The remittance date cannot be in the future.", "REMITTANCE_DATE_IN_FUTURE")
        row.remittance_ref = ref
        details.update(remitted_on=paid_on.isoformat(), remitted_by_user_id=principal.user_id,
                       remitted_recorded_at=now.isoformat())
    else:
        details.update(reconciled_by_user_id=principal.user_id, reconciled_at=now.isoformat())
    if note:
        details.setdefault("notes", []).append({"action": action, "note": note.strip()[:500],
                                                "by_user_id": principal.user_id, "at": now.isoformat()})
    row.details = details
    row.status = target
    row.row_version += 1
    db.flush()
    AuditLogService.log(
        db, principal, AUDIT_MODULE, f"LIABILITY_{'REMITTED' if target == 'REMITTED' else 'RECONCILED'}",
        f"Statutory liability #{row.id} ({row.scheme}): {before} -> {target}", LIABILITY_SUBJECT, row.id,
        company_id=row.company_id, details={"before": before, "after": target, "remittance_ref": row.remittance_ref,
                                            "fixture": row.fixture},
    )
    return row


def get_liability(db: Session, principal: Principal, liability_id: int) -> dict:
    if not reports_service._held(principal, READ_PERMISSIONS):
        raise forbidden()
    found = _liability_query(db, principal).filter(StatutoryLiability.id == liability_id).first()
    if found is None:
        raise not_found("Statutory liability")
    return liability_json(*found, today=_today(db, principal.company_id))


# --------------------------------------------------------------------------- due-date calendar


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _recent_month_ends(on: date, back: int, ahead: int) -> List[date]:
    result = []
    for offset in range(-back, ahead + 1):
        index = on.year * 12 + on.month - 1 + offset
        result.append(_month_end(index // 12, index % 12 + 1))
    return result


def _period_of(frequency: str, period_end: date, tax_year_start: str) -> Optional[date]:
    """The period start of a report period ending on ``period_end`` (None when no period ends there)."""
    if frequency == "MONTHLY":
        return period_end.replace(day=1)
    if frequency == "QUARTERLY":
        return period_end.replace(day=1, month=period_end.month - 2) if period_end.month % 3 == 0 else None
    month, day = (int(part) for part in tax_year_start.split("-"))
    start = date(period_end.year - (1 if (period_end.month, period_end.day) < (month, day) else 0), month, day)
    end = date(start.year + 1, month, day) - timedelta(days=1)
    return start if end == period_end else None


def due_calendar(db: Session, entity: LegalEntity, on: date, *, back_months: int = 3, ahead_months: int = 1) -> dict:
    """Contribution and report due dates of the pack that governs the entity (3.2 §8.3)."""
    found = reports_service.entity_pack(entity.country_code, on, on)
    if found is None or not found.usage.final:
        reason = ("No country pack covers this entity." if found is None else
                  f"{found.usage.message} Due dates appear once a verified pack version covers this entity.")
        return {"available": False, "reason": reason, "items": [], "watermark": None}
    pack = found.pack
    liabilities = (db.query(StatutoryLiability)
                   .filter(StatutoryLiability.company_id == entity.company_id,
                           StatutoryLiability.legal_entity_id == entity.id,
                           StatutoryLiability.due_date.isnot(None)).all())
    reports = (db.query(StatutoryReport)
               .filter(StatutoryReport.company_id == entity.company_id, StatutoryReport.legal_entity_id == entity.id,
                       StatutoryReport.status != "AMENDED").all())
    items: List[dict] = []
    month_ends = _recent_month_ends(on, back_months, ahead_months)
    groups: Dict[Tuple[str, str, Any], List[str]] = {}
    for component in pack.components():
        if component.due is None or component.kind not in ("DEDUCTION", "EMPLOYER_CONTRIBUTION"):
            continue
        groups.setdefault((component.scheme, component.payee or "", component.due), []).append(component.code)
    for (scheme, payee, rule), codes in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        for period_end in month_ends:
            due = rule.due_date(period_end)
            matching = [r for r in liabilities if r.scheme == scheme and r.due_date == due]
            if matching:
                statuses = sorted({r.status for r in matching})
                status = "OPEN" if "OPEN" in statuses else ("REMITTED" if "REMITTED" in statuses else "RECONCILED")
            else:
                status = "NO_LIABILITY"
            items.append({"kind": "CONTRIBUTION", "scheme": scheme, "payee": payee, "components": sorted(codes),
                          "period_start": period_end.replace(day=1), "period_end": period_end, "due_date": due,
                          "rule": rule.describe(), "status": status,
                          "overdue": status == "OPEN" and due < on})
    for definition in pack.reports():
        if definition.due is None:
            continue
        ends = [e for e in month_ends if _period_of(definition.frequency, e, pack.meta.tax_year_start) is not None]
        for period_end in ends:
            start = _period_of(definition.frequency, period_end, pack.meta.tax_year_start)
            due = definition.due.due_date(period_end)
            existing = next((r for r in reports if r.report_code == definition.code and r.period_start == start), None)
            status = existing.status if existing is not None else "NOT_STARTED"
            items.append({"kind": "REPORT", "report_code": definition.code, "name": definition.name,
                          "period_start": start, "period_end": period_end, "due_date": due,
                          "rule": definition.due.describe(), "status": status,
                          "report_id": existing.id if existing is not None else None,
                          "overdue": due < on and status not in ("SUBMITTED", "ACCEPTED")})
    items.sort(key=lambda i: (i["due_date"], i["kind"], i.get("scheme") or i.get("report_code") or ""))
    return {"available": True, "reason": None, "items": items, "pack_version": pack.meta.version,
            "watermark": found.watermark}


# --------------------------------------------------------------------------- the page


def _registrations(entity: LegalEntity) -> List[dict]:
    result = []
    for line in entity.registration_lines or []:
        if isinstance(line, dict):
            result.append({"label": str(line.get("label", ""))[:100], "value": str(line.get("value", ""))[:200]})
    return result


def entity_card(db: Session, principal: Principal, entity: LegalEntity, on: date) -> dict:
    active = reports_service.entity_pack(entity.country_code, on, on)
    final = active is not None and active.usage.final
    info = countries.find(entity.country_code)
    recent = (db.query(StatutoryReport)
              .filter(StatutoryReport.company_id == entity.company_id, StatutoryReport.legal_entity_id == entity.id)
              .order_by(StatutoryReport.period_start.desc(), StatutoryReport.version.desc()).limit(24).all())
    return {
        "legal_entity": {"id": entity.id, "code": entity.code, "legal_name": entity.legal_name,
                         "status": entity.status, "base_currency": entity.base_currency,
                         "country_confirmed": entity.country_confirmed},
        "country": {"code": entity.country_code, "name": getattr(info, "name", entity.country_code)},
        "capability": capability_badge(db, entity.country_code, on),
        "active_pack": None if active is None else {
            "code": active.pack.meta.code, "version": active.pack.meta.version, "status": active.pack.meta.status.value,
            "usage": active.usage.mode, "usage_message": active.usage.message, "is_fixture": active.is_fixture,
            "watermark": active.watermark, "reviewer": active.pack.meta.reviewer,
            "reviewed_on": active.pack.meta.reviewed_on, "next_review_due": _next_review_due(active.pack),
            "effective_from": active.pack.meta.effective_from, "effective_to": active.pack.meta.effective_to,
        },
        "pack_versions": pack_versions(entity.country_code, on),
        "attestation": attestation_status(db, entity, on, required=not final),
        "registrations": _registrations(entity),
        "liabilities": liability_summary(db, principal, entity, on),
        "report_definitions": [
            {"code": r.code, "name": r.name, "frequency": r.frequency,
             "due_rule": r.due.describe() if r.due else None, "note": r.note}
            for r in (active.pack.reports() if final else [])
        ],
        "statutory_reports": [reports_service.serialize(db, principal, r) for r in recent],
        "due_calendar": due_calendar(db, entity, on),
    }


def overview(db: Session, principal: Principal, *, legal_entity_id: Optional[int] = None,
             on: Optional[date] = None) -> dict:
    today = on or _today(db, principal.company_id)
    entities = _entities(db, principal, legal_entity_id)
    return {"as_of": today, "environment": reports_service.environment(),
            "entities": [entity_card(db, principal, e, today) for e in entities]}


def entities_in_scope(db: Session, principal: Principal) -> List[int]:
    return [e.id for e in _entities(db, principal, None)]


__all__ = [
    "LIABILITY_TRANSITIONS", "attestation_status", "capability_badge", "due_calendar", "entity_card",
    "get_liability", "liability_transition", "list_liabilities", "overview", "pack_versions",
]


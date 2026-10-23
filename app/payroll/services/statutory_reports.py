"""Statutory reports: the lifecycle of the pack-defined returns of a legal entity (3.2 §8.2, 3.9 §3.10, §4).

``DRAFT → CALCULATED → REVIEW_REQUIRED → VERIFIED → READY_TO_FILE → SUBMITTED → ACCEPTED | REJECTED → AMENDED``

* Only packs that define reports (``CountryPack.reports()``) and may produce final statutory lines here
  (a VERIFIED_FOR_PERIOD version, or a fixture version in development/test) have reports. A report of
  a fixture version is watermarked "FIXTURE — not law" in every view and file (decision I-07); a
  draft or unverified pack has none (``PACK_NOT_VERIFIED``): nothing is ever presented as fileable
  from rules nobody verified.
* ``transition(db, principal, report_id, action, ...)`` is the ONLY writer of ``statutory_reports.status``.
  It locks the row (``FOR UPDATE``), checks ``row_version`` (409 ROW_VERSION_CONFLICT), looks the move up
  in ``state_machines.STATUTORY_REPORT`` (409 with the machine's code), checks the transition's permission
  at the report's legal entity (403 without it, 404 outside the caller's scope), requires the reason,
  evaluates the guards and writes the audit event:
    - GENERATE / REGENERATE (RUNS_FINALIZED): content is built from finalized runs only, and only when
      no live run of the entity paying in the period is still unfinalized;
    - VERIFY (RECONCILES_TO_RUNS): the stored totals still equal the totals recomputed from the
      immutable runs per component and liability (a later reversal makes the report out of date);
    - EXPORT writes the filing file (CSV, cells escaped, EC-DOC-17) and moves the report to
      READY_TO_FILE — never SUBMITTED (EC-STA-11);
    - MARK_SUBMITTED needs the filing reference (FILING_REFERENCE) and ``payroll:statutory:submit``;
      RECORD_ACCEPTED the authority's acknowledgement; RECORD_REJECTED its response;
    - AMEND (reason) turns a filed report into AMENDED and creates the amendment linked to it
      (``amends_report_id``, version + 1, EC-STA-12) as a new DRAFT built from the current runs.
* Report periods follow the report's frequency: a calendar month, a calendar quarter or the pack's
  tax year. A run belongs to the period of its pay date (payment basis).
* Report content: one row per employee and currency with the amount of every statutory component of
  the report, plus the liabilities (employee and employer portions separately, EC-STA-10) of the same
  runs. A pack ``ReportDef`` does not name its schemes yet, so a report covers the pack schemes whose
  short code appears in the report code (``ZZ_PENSION_RETURN`` → ``ZZ.pension``), else every scheme.

Services take (db, principal, ...) and never commit.
"""

from __future__ import annotations

import calendar
import csv
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import and_, false, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.core import clock, storage
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.employee import Employee
from app.models.org import LegalEntity
from app.models.pay_runs import PayPeriod, PayrollLine, PayrollRun, PayrollRunEmployee, StatutoryLiability
from app.models.reports_compliance import FILED_STATUSES, StatutoryReport
from app.payroll import audit as payroll_audit
from app.payroll import packs
from app.payroll import state_machines as sm
from app.payroll.banking.files import escape_csv_cell
from app.payroll.money import amount_str
from app.payroll.packs.base import FIXTURE_LABEL, CountryPack, ReportDef
from app.payroll.services import access
from app.services.audit_log_service import AuditLogService

REVIEW = sm.STATUTORY_REVIEW
SUBMIT = sm.STATUTORY_SUBMIT
REPORTS_READ = "payroll:reports:read"
READ_PERMISSIONS = (REVIEW, SUBMIT, REPORTS_READ)
AUDIT_MODULE = payroll_audit.COUNTRY_PACKS
SUBJECT = "statutory_report"
FILE_CATEGORY = "statutory-reports"
FINALIZED_RUN_STATUSES = ("FINALIZED", "PAYMENT_READY", "SUBMITTED", "PARTIALLY_PAID", "PAID", "RECONCILED", "CLOSED")
STATUTORY_KINDS = ("DEDUCTION", "EMPLOYER_CONTRIBUTION")
_ZERO = Decimal("0")

AUDIT_EVENTS = {
    "CREATE": "STATUTORY_REPORT_CREATED",
    "GENERATE": "STATUTORY_REPORT_GENERATED",
    "REGENERATE": "STATUTORY_REPORT_REGENERATED",
    "SUBMIT_FOR_REVIEW": "STATUTORY_REPORT_REVIEW_REQUESTED",
    "VERIFY": "STATUTORY_REPORT_VERIFIED",
    "EXPORT": "STATUTORY_REPORT_EXPORTED",
    "MARK_SUBMITTED": "STATUTORY_REPORT_SUBMITTED",
    "RECORD_ACCEPTED": "STATUTORY_REPORT_ACCEPTED",
    "RECORD_REJECTED": "STATUTORY_REPORT_REJECTED",
    "AMEND": "STATUTORY_REPORT_AMENDED",
    "DOWNLOAD": "STATUTORY_REPORT_DOWNLOADED",
}


def environment() -> str:
    return packs.environment_name(app_settings.environment)


def _audit(db: Session, principal: Optional[Principal], event: str, description: str, report: StatutoryReport,
           **details: Any) -> None:
    AuditLogService.log(
        db, principal, AUDIT_MODULE, event, description[:500], SUBJECT, report.id, company_id=report.company_id,
        actor_type="user" if principal is not None else "system", details=details,
    )


def _text(value: Optional[str], limit: int) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return value[:limit] if value else None


# --------------------------------------------------------------------------- packs and periods


@dataclass(frozen=True)
class EntityPack:
    """The pack version that governs an entity's statutory reports for a period, and how it may be used."""

    pack: CountryPack
    usage: packs.PackUsage

    @property
    def is_fixture(self) -> bool:
        return self.pack.meta.is_fixture

    @property
    def watermark(self) -> Optional[str]:
        return FIXTURE_LABEL if self.is_fixture else None


def entity_pack(country_code: str, start: date, end: date) -> Optional[EntityPack]:
    """The best pack version of the country covering the whole period (verified, then fixture, then draft)."""
    env = environment()
    candidates: List[EntityPack] = []
    for pack in packs.packs_for_country(country_code, on=end, environment=env):
        if not pack.meta.covers_period(start, end):
            continue
        candidates.append(EntityPack(pack, packs.usage(pack.meta, environment=env, period=(start, end))))
    if not candidates:
        return None
    rank = {packs.VERIFIED: 0, packs.FIXTURE: 1, packs.PREVIEW: 2, packs.REFUSED: 3}
    return sorted(candidates, key=lambda c: (rank.get(c.usage.mode, 9), c.pack.meta.effective_from))[0]


def report_period(frequency: str, period_start: date, pack: CountryPack) -> Tuple[date, date]:
    """The reporting period that starts on ``period_start``; 422 when it is not a period start."""
    if frequency == "MONTHLY":
        if period_start.day != 1:
            raise bad_request("A monthly report starts on the first day of a month.", "INVALID_REPORT_PERIOD")
        last = calendar.monthrange(period_start.year, period_start.month)[1]
        return period_start, period_start.replace(day=last)
    if frequency == "QUARTERLY":
        if period_start.day != 1 or period_start.month not in (1, 4, 7, 10):
            raise bad_request("A quarterly report starts on 1 January, 1 April, 1 July or 1 October.",
                              "INVALID_REPORT_PERIOD")
        end_month = period_start.month + 2
        last = calendar.monthrange(period_start.year, end_month)[1]
        return period_start, date(period_start.year, end_month, last)
    if frequency == "ANNUAL":
        month, day = (int(part) for part in pack.meta.tax_year_start.split("-"))
        if (period_start.month, period_start.day) != (month, day):
            raise bad_request(f"An annual report starts on the tax year start ({pack.meta.tax_year_start}).",
                              "INVALID_REPORT_PERIOD")
        return period_start, date(period_start.year + 1, month, day) - timedelta(days=1)
    raise bad_request(f"Unknown report frequency {frequency!r}.", "INVALID_REPORT_PERIOD")


def report_components(pack: CountryPack, definition: ReportDef) -> List[str]:
    """Statutory component codes a report covers (see the module docstring)."""
    components = [c for c in pack.components() if c.kind in STATUTORY_KINDS]
    tokens = {part for part in definition.code.upper().split("_") if part}
    matched = [c for c in components if c.scheme.partition(".")[2].upper() in tokens]
    return sorted(c.code for c in (matched or components))


def _definition(pack: CountryPack, report_code: str) -> ReportDef:
    found = next((r for r in pack.reports() if r.code == report_code), None)
    if found is None:
        raise bad_request(f"The pack {pack.meta.version} defines no report {report_code!r}.", "UNKNOWN_REPORT_CODE")
    return found


def require_report_pack(entity: LegalEntity, start: date, end: date) -> EntityPack:
    """The pack of a report: it must define reports and produce final statutory lines here."""
    found = entity_pack(entity.country_code, start, end)
    if found is None:
        raise conflict(f"No country pack covers {entity.country_code} for this period; there are no statutory "
                       "reports to prepare.", "NO_COUNTRY_PACK")
    if not found.pack.reports():
        raise conflict(f"The pack {found.pack.meta.version} defines no statutory reports.", "NO_STATUTORY_REPORTS")
    if not found.usage.final:
        raise conflict(f"{found.usage.message} Statutory reports need a verified pack version.", "PACK_NOT_VERIFIED")
    return found


# --------------------------------------------------------------------------- access


def _held(principal: Principal, permissions: Iterable[str]) -> List[str]:
    return [p for p in permissions if principal.has(p)]


def scope_clause(principal: Principal, permissions: Sequence[str] = READ_PERMISSIONS, column=None):
    """Reports the caller may see: the union of the entity scopes of the permissions held."""
    column = StatutoryReport.legal_entity_id if column is None else column
    held = _held(principal, permissions)
    if not held:
        return false()
    return or_(*(access.run_scope_clause(principal, code, column, None) for code in held))


def _load(db: Session, principal: Principal, report_id: int, *, lock: bool = False) -> StatutoryReport:
    if not _held(principal, READ_PERMISSIONS):
        raise forbidden()
    query = db.query(StatutoryReport).filter(
        StatutoryReport.company_id == principal.company_id, StatutoryReport.id == report_id,
        scope_clause(principal),
    )
    if lock:
        query = query.with_for_update().populate_existing()
    row = query.first()
    if row is None:
        raise not_found("Statutory report")
    return row


def _entity(db: Session, company_id: int, legal_entity_id: int) -> LegalEntity:
    entity = db.query(LegalEntity).filter(LegalEntity.company_id == company_id,
                                          LegalEntity.id == legal_entity_id).first()
    if entity is None:
        raise not_found("Legal entity")
    return entity


# --------------------------------------------------------------------------- content


def finalized_runs(db: Session, company_id: int, legal_entity_id: int, start: date, end: date) -> List[PayrollRun]:
    """Finalized runs of the entity whose pay date lies in the period (payment basis)."""
    return (
        db.query(PayrollRun)
        .join(PayPeriod, and_(PayPeriod.company_id == PayrollRun.company_id, PayPeriod.id == PayrollRun.pay_period_id))
        .filter(PayrollRun.company_id == company_id, PayrollRun.legal_entity_id == legal_entity_id,
                PayrollRun.status.in_(FINALIZED_RUN_STATUSES), PayPeriod.pay_date >= start, PayPeriod.pay_date <= end)
        .order_by(PayrollRun.id)
        .all()
    )


def unfinalized_runs(db: Session, company_id: int, legal_entity_id: int, start: date, end: date) -> List[str]:
    rows = (
        db.query(PayrollRun.number)
        .join(PayPeriod, and_(PayPeriod.company_id == PayrollRun.company_id, PayPeriod.id == PayrollRun.pay_period_id))
        .filter(PayrollRun.company_id == company_id, PayrollRun.legal_entity_id == legal_entity_id,
                PayrollRun.status.notin_(FINALIZED_RUN_STATUSES + ("CANCELLED",)),
                PayPeriod.pay_date >= start, PayPeriod.pay_date <= end)
        .order_by(PayrollRun.number)
        .all()
    )
    return [number for (number,) in rows]


def _add(bucket: Dict[str, Decimal], currency: str, amount: Decimal) -> None:
    bucket[currency] = bucket.get(currency, _ZERO) + amount


def _money_map(values: Mapping[str, Decimal]) -> Dict[str, str]:
    return {currency: amount_str(amount, currency) for currency, amount in sorted(values.items())}


def build_content(db: Session, report: StatutoryReport, pack: CountryPack) -> Tuple[dict, dict, List[int]]:
    """(content, totals, run ids) recomputed from the immutable runs; raises 409 when runs are missing."""
    cid = report.company_id
    pending = unfinalized_runs(db, cid, report.legal_entity_id, report.period_start, report.period_end)
    if pending:
        raise conflict("Runs paying in this period are not finalized yet: " + ", ".join(pending[:10])
                       + ". A statutory report reads finalized runs only.", "RUNS_NOT_FINALIZED")
    runs = finalized_runs(db, cid, report.legal_entity_id, report.period_start, report.period_end)
    if not runs:
        raise conflict("No finalized run pays in this period; there is nothing to report.", "NO_FINALIZED_RUNS")
    run_ids = [r.id for r in runs]
    definition = _definition(pack, report.report_code)
    codes = report_components(pack, definition)
    names = {c.code: c.name for c in pack.components()}
    schemes = sorted({c.scheme for c in pack.components() if c.code in codes})

    rows = (
        db.query(PayrollLine.component_code, PayrollLine.amount, PayrollLine.currency, PayrollLine.kind,
                 PayrollRunEmployee.employee_id, Employee.employee_code, Employee.first_name, Employee.last_name)
        .join(PayrollRunEmployee, and_(PayrollRunEmployee.company_id == PayrollLine.company_id,
                                       PayrollRunEmployee.id == PayrollLine.run_employee_id))
        .join(Employee, and_(Employee.company_id == PayrollRunEmployee.company_id,
                             Employee.id == PayrollRunEmployee.employee_id))
        .filter(PayrollLine.company_id == cid, PayrollLine.run_id.in_(run_ids),
                PayrollLine.component_code.in_(codes), PayrollLine.kind.in_(STATUTORY_KINDS),
                # Finalized pay only: a FULL hold (ON_HOLD) or an error is never finalized nor remitted.
                PayrollRunEmployee.status == "CALCULATED")
        .all()
    )
    per_employee: Dict[Tuple[int, str], dict] = {}
    component_totals: Dict[str, Dict[str, Decimal]] = {}
    for code, amount, currency, _kind, employee_id, employee_code, first, last in rows:
        key = (employee_id, currency)
        entry = per_employee.setdefault(key, {
            "employee_id": employee_id, "employee_code": employee_code,
            "employee_name": " ".join(p for p in (first, last) if p), "currency": currency, "amounts": {},
        })
        entry["amounts"][code] = entry["amounts"].get(code, _ZERO) + Decimal(amount)
        _add(component_totals.setdefault(code, {}), currency, Decimal(amount))

    liabilities: Dict[str, dict] = {}
    for row in (db.query(StatutoryLiability)
                .filter(StatutoryLiability.company_id == cid, StatutoryLiability.run_id.in_(run_ids),
                        StatutoryLiability.scheme.in_(schemes))
                .order_by(StatutoryLiability.scheme, StatutoryLiability.payee_authority, StatutoryLiability.id)):
        key = f"{row.scheme}|{row.payee_authority}|{row.currency}"
        item = liabilities.setdefault(key, {"scheme": row.scheme, "payee": row.payee_authority,
                                            "currency": row.currency, "employee": _ZERO, "employer": _ZERO,
                                            "due_dates": set()})
        item["employee"] += Decimal(row.employee_amount)
        item["employer"] += Decimal(row.employer_amount)
        if row.due_date:
            item["due_dates"].add(row.due_date.isoformat())

    content_rows = []
    for (_, currency), entry in sorted(per_employee.items(), key=lambda kv: (kv[1]["employee_code"] or "", kv[0])):
        content_rows.append({
            "employee_id": entry["employee_id"], "employee_code": entry["employee_code"],
            "employee_name": entry["employee_name"], "currency": currency,
            "amounts": {code: amount_str(value, currency) for code, value in sorted(entry["amounts"].items())},
        })
    liability_json = [
        {"scheme": v["scheme"], "payee": v["payee"], "currency": v["currency"],
         "employee_amount": amount_str(v["employee"], v["currency"]),
         "employer_amount": amount_str(v["employer"], v["currency"]),
         "due_dates": sorted(v["due_dates"])}
        for _, v in sorted(liabilities.items())
    ]
    content = {
        "columns": [{"code": code, "name": names.get(code, code)} for code in codes],
        "rows": content_rows,
        "liabilities": liability_json,
        "schemes": schemes,
    }
    totals = {
        "components": {code: _money_map(values) for code, values in sorted(component_totals.items())},
        "liabilities": {f"{v['scheme']}|{v['payee']}": {v["currency"]: {
            "employee": amount_str(v["employee"], v["currency"]), "employer": amount_str(v["employer"], v["currency"])}}
            for _, v in sorted(liabilities.items())},
        "employees": len({k[0] for k in per_employee}),
    }
    return content, totals, run_ids


def content_hash(content: Mapping[str, Any], totals: Mapping[str, Any], run_ids: Sequence[int]) -> str:
    canonical = json.dumps({"content": content, "totals": totals, "run_ids": list(run_ids)}, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _apply_content(db: Session, report: StatutoryReport, pack: CountryPack, principal: Optional[Principal]) -> None:
    content, totals, run_ids = build_content(db, report, pack)
    report.content, report.totals, report.run_ids = content, totals, run_ids
    report.content_hash = content_hash(content, totals, run_ids)
    report.row_count = len(content["rows"])
    report.pack_version = pack.meta.version
    report.generated_at = clock.utcnow()
    report.generated_by_user_id = principal.user_id if principal is not None else None
    # A regenerated report needs a fresh review, verification and file.
    report.verified_at = report.verified_by_user_id = report.review_requested_at = None
    report.exported_at = report.exported_by_user_id = report.export_sha256 = None
    report.export_storage_path = report.export_file_name = None


# --------------------------------------------------------------------------- files


def render_csv(report: StatutoryReport, entity: LegalEntity) -> bytes:
    """The filing file: header block, one row per employee, liabilities. Cells are escaped (EC-DOC-17)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")

    def row(values: Iterable[Any]) -> None:
        writer.writerow([escape_csv_cell("" if v is None else str(v)) for v in values])

    if report.watermark:
        row([report.watermark])
    row(["Report", report.report_code, report.report_name])
    row(["Legal entity", entity.code, entity.legal_name])
    row(["Period", report.period_start.isoformat(), report.period_end.isoformat()])
    row(["Pack version", report.pack_version])
    row(["Version", report.version] + (["Amends report", report.amends_report_id] if report.amends_report_id else []))
    row([])
    columns = report.content.get("columns", [])
    row(["Employee code", "Employee name", "Currency", *[c["name"] for c in columns]])
    for item in report.content.get("rows", []):
        row([item["employee_code"], item["employee_name"], item["currency"],
             *[item["amounts"].get(c["code"], "") for c in columns]])
    row([])
    row(["Scheme", "Payee", "Currency", "Employee portion", "Employer portion", "Due dates"])
    for item in report.content.get("liabilities", []):
        row([item["scheme"], item["payee"], item["currency"], item["employee_amount"], item["employer_amount"],
             " ".join(item["due_dates"])])
    return out.getvalue().encode("utf-8")


def file_bytes(db: Session, principal: Principal, report_id: int) -> Tuple[bytes, StatutoryReport]:
    """The exported file of a report, verified against its stored SHA-256; audited."""
    report = _load(db, principal, report_id)
    if not report.export_storage_path or not report.export_sha256:
        raise conflict("This report has not been exported yet.", "REPORT_NOT_EXPORTED")
    data = storage.resolve(report.company_id, report.export_storage_path).read_bytes()
    if hashlib.sha256(data).hexdigest() != report.export_sha256:
        raise conflict("The stored file does not match its recorded hash; export the report again.",
                       "FILE_HASH_MISMATCH")
    _audit(db, principal, AUDIT_EVENTS["DOWNLOAD"], f"Downloaded statutory report {report.report_code}", report,
           sha256=report.export_sha256)
    return data, report


# --------------------------------------------------------------------------- create, list, read


def create_report(db: Session, principal: Principal, *, legal_entity_id: int, report_code: str,
                  period_start: date) -> StatutoryReport:
    """A DRAFT report of a pack report code for one entity and period (``payroll:statutory:review``)."""
    access.ensure_in_scope(principal, REVIEW, legal_entity_id, None, "Legal entity")
    entity = _entity(db, principal.company_id, legal_entity_id)
    probe = entity_pack(entity.country_code, period_start, period_start)
    if probe is None:
        raise conflict(f"No country pack covers {entity.country_code}; there are no statutory reports to prepare.",
                       "NO_COUNTRY_PACK")
    definition = _definition(probe.pack, report_code)
    start, end = report_period(definition.frequency, period_start, probe.pack)
    found = require_report_pack(entity, start, end)
    definition = _definition(found.pack, report_code)
    report = StatutoryReport(
        company_id=principal.company_id, legal_entity_id=entity.id, pack_code=found.pack.meta.code,
        pack_version=found.pack.meta.version, report_code=definition.code, report_name=definition.name,
        frequency=definition.frequency, period_start=start, period_end=end,
        due_date=definition.due.due_date(end) if definition.due else None, status=sm.StatutoryReportStatus.DRAFT,
        version=1, is_fixture=found.is_fixture, watermark=found.watermark, run_ids=[], totals={}, content={},
        created_by_user_id=principal.user_id, row_version=1,
    )
    _insert(db, report)
    _audit(db, principal, AUDIT_EVENTS["CREATE"], f"Created statutory report {definition.code} for "
           f"{start.isoformat()} to {end.isoformat()}", report, legal_entity_id=entity.id, report_code=definition.code,
           period_start=start.isoformat(), pack_version=found.pack.meta.version, fixture=found.is_fixture)
    return report


def _insert(db: Session, report: StatutoryReport) -> None:
    try:
        with db.begin_nested():
            db.add(report)
            db.flush()
    except IntegrityError as exc:
        if "uq_statutory_reports_live" in str(exc.orig) or "uq_statutory_reports_amends" in str(exc.orig):
            raise conflict("A live report for this entity, report and period already exists.", "DUPLICATE_REPORT")
        raise


def get_report(db: Session, principal: Principal, report_id: int) -> dict:
    return serialize(db, principal, _load(db, principal, report_id), detail=True)


def list_reports(db: Session, principal: Principal, *, legal_entity_id: Optional[int] = None,
                 status: Optional[str] = None, report_code: Optional[str] = None, period_from: Optional[date] = None,
                 period_to: Optional[date] = None, skip: int = 0, limit: int = 50) -> Tuple[int, List[dict]]:
    if not _held(principal, READ_PERMISSIONS):
        raise forbidden()
    query = db.query(StatutoryReport).filter(StatutoryReport.company_id == principal.company_id,
                                             scope_clause(principal))
    if legal_entity_id is not None:
        query = query.filter(StatutoryReport.legal_entity_id == legal_entity_id)
    if status:
        query = query.filter(StatutoryReport.status == status)
    if report_code:
        query = query.filter(StatutoryReport.report_code == report_code)
    if period_from is not None:
        query = query.filter(StatutoryReport.period_end >= period_from)
    if period_to is not None:
        query = query.filter(StatutoryReport.period_start <= period_to)
    total = query.count()
    rows = query.order_by(StatutoryReport.period_start.desc(), StatutoryReport.report_code,
                          StatutoryReport.version.desc()).offset(skip).limit(limit).all()
    return total, [serialize(db, principal, r) for r in rows]


def serialize(db: Session, principal: Optional[Principal], report: StatutoryReport, *, detail: bool = False) -> dict:
    permissions = [code for code in (REVIEW, SUBMIT) if principal is None or principal.has(code)]
    data = {
        "id": report.id, "legal_entity_id": report.legal_entity_id, "pack_code": report.pack_code,
        "pack_version": report.pack_version, "report_code": report.report_code, "report_name": report.report_name,
        "frequency": report.frequency, "period_start": report.period_start, "period_end": report.period_end,
        "due_date": report.due_date, "status": report.status, "version": report.version,
        "amends_report_id": report.amends_report_id, "is_fixture": report.is_fixture, "watermark": report.watermark,
        "row_count": report.row_count, "content_hash": report.content_hash, "generated_at": report.generated_at,
        "verified_at": report.verified_at, "exported_at": report.exported_at, "export_sha256": report.export_sha256,
        "filing_ref": report.filing_ref, "submitted_at": report.submitted_at,
        "acknowledgement_ref": report.acknowledgement_ref, "acknowledged_at": report.acknowledged_at,
        "authority_response": report.authority_response, "rejected_at": report.rejected_at,
        "amended_at": report.amended_at, "status_reason": report.status_reason, "row_version": report.row_version,
        "available_actions": [s.action for s in sm.available_actions(sm.STATUTORY_REPORT, report.status, permissions)],
        "lifecycle_note": "Exporting a file makes the report ready to file; it is filed only when you record the "
                          "filing reference.",
    }
    if detail:
        data.update(content=report.content, totals=report.totals, run_ids=report.run_ids)
        amendment = (db.query(StatutoryReport.id).filter(StatutoryReport.company_id == report.company_id,
                                                         StatutoryReport.amends_report_id == report.id).first())
        data["amended_by_report_id"] = amendment[0] if amendment else None
    return data


# --------------------------------------------------------------------------- the transition


def transition(db: Session, principal: Principal, report_id: int, action: str, *,
               expected_version: Optional[int] = None, reason: Optional[str] = None,
               filing_ref: Optional[str] = None, acknowledgement_ref: Optional[str] = None,
               authority_response: Optional[str] = None) -> Tuple[StatutoryReport, Optional[StatutoryReport]]:
    """The ONLY writer of a report's status; returns (report, amendment or None). See the module docstring."""
    report = _load(db, principal, report_id, lock=True)
    if expected_version is not None and expected_version != report.row_version:
        raise conflict("The report was changed by someone else; reload it and try again.", "ROW_VERSION_CONFLICT")
    try:
        spec = sm.transition_spec(sm.STATUTORY_REPORT, report.status, action)
    except sm.InvalidTransition as exc:
        raise conflict(exc.message, exc.code)
    if spec.permission is not None:
        access.ensure_in_scope(principal, spec.permission, report.legal_entity_id, None, "Statutory report")
    reason = _text(reason, 1000)
    if spec.reason_required and not reason:
        raise bad_request("Give a reason.", "REASON_REQUIRED")
    before = report.status
    entity = _entity(db, report.company_id, report.legal_entity_id)
    now = clock.utcnow()
    amendment: Optional[StatutoryReport] = None
    details: Dict[str, Any] = {}

    if "RUNS_FINALIZED" in spec.guards:
        found = require_report_pack(entity, report.period_start, report.period_end)
        _apply_content(db, report, found.pack, principal)
        details.update(run_ids=report.run_ids, content_hash=report.content_hash, rows=report.row_count)
    if "RECONCILES_TO_RUNS" in spec.guards:
        found = require_report_pack(entity, report.period_start, report.period_end)
        content, totals, run_ids = build_content(db, report, found.pack)
        if content_hash(content, totals, run_ids) != report.content_hash:
            raise conflict("The report no longer reconciles to its runs (a run changed or was reversed); "
                           "regenerate it.", "REPORT_OUT_OF_DATE")
        report.verified_at, report.verified_by_user_id = now, principal.user_id
    if action == "SUBMIT_FOR_REVIEW":
        report.review_requested_at = now
    if action == "EXPORT":
        data = render_csv(report, entity)
        digest = hashlib.sha256(data).hexdigest()
        report.export_storage_path = storage.save_bytes(report.company_id, FILE_CATEGORY, data, "csv")
        report.export_sha256 = digest
        report.export_file_name = (f"{report.report_code}_{entity.code}_{report.period_start.isoformat()}"
                                   f"_v{report.version}.csv")[:150]
        report.exported_at, report.exported_by_user_id = now, principal.user_id
        details.update(sha256=digest, bytes=len(data), file_name=report.export_file_name)
    if "FILING_REFERENCE" in spec.guards:
        filing_ref = _text(filing_ref, 100)
        if not filing_ref:
            raise bad_request("Marking a report as submitted needs the filing reference from the authority.",
                              "FILING_REFERENCE_REQUIRED")
        report.filing_ref, report.submitted_at, report.submitted_by_user_id = filing_ref, now, principal.user_id
        details["filing_ref"] = filing_ref
    if "ACKNOWLEDGEMENT" in spec.guards:
        acknowledgement_ref = _text(acknowledgement_ref, 100)
        if not acknowledgement_ref:
            raise bad_request("Recording acceptance needs the authority's acknowledgement reference.",
                              "ACKNOWLEDGEMENT_REQUIRED")
        report.acknowledgement_ref, report.acknowledged_at = acknowledgement_ref, now
        details["acknowledgement_ref"] = acknowledgement_ref
    if "AUTHORITY_RESPONSE" in spec.guards:
        authority_response = _text(authority_response, 2000)
        if not authority_response:
            raise bad_request("Recording a rejection needs the authority's response.", "AUTHORITY_RESPONSE_REQUIRED")
        report.authority_response, report.rejected_at = authority_response, now
    if reason:
        report.status_reason = reason
        details["reason"] = reason

    report.status = spec.target
    if spec.target == sm.StatutoryReportStatus.AMENDED:
        report.amended_at, report.amended_by_user_id = now, principal.user_id
    report.row_version += 1
    db.flush()

    if "CREATE_AMENDMENT" in spec.effects:
        amendment = StatutoryReport(
            company_id=report.company_id, legal_entity_id=report.legal_entity_id, pack_code=report.pack_code,
            pack_version=report.pack_version, report_code=report.report_code, report_name=report.report_name,
            frequency=report.frequency, period_start=report.period_start, period_end=report.period_end,
            due_date=report.due_date, status=sm.StatutoryReportStatus.DRAFT, version=report.version + 1,
            amends_report_id=report.id, is_fixture=report.is_fixture, watermark=report.watermark, run_ids=[],
            totals={}, content={}, status_reason=reason, created_by_user_id=principal.user_id, row_version=1,
        )
        _insert(db, amendment)
        details["amendment_report_id"] = amendment.id
        _audit(db, principal, AUDIT_EVENTS["CREATE"], f"Created amendment v{amendment.version} of statutory report "
               f"{report.report_code}", amendment, amends_report_id=report.id, reason=reason)

    _audit(db, principal, AUDIT_EVENTS[action], f"Statutory report {report.report_code} "
           f"{report.period_start.isoformat()}: {before} -> {report.status}", report,
           action=action, before=before, after=report.status, **details)
    return report, amendment


def regenerate_needed(db: Session, report: StatutoryReport) -> bool:
    """True when a generated report no longer matches its runs (shown on the compliance page)."""
    if report.status in (sm.StatutoryReportStatus.DRAFT,) or report.status in FILED_STATUSES:
        return False
    entity = db.get(LegalEntity, report.legal_entity_id)
    found = entity_pack(entity.country_code, report.period_start, report.period_end) if entity else None
    if found is None or not found.usage.final:
        return False
    try:
        content, totals, run_ids = build_content(db, report, found.pack)
    except AppError:
        return True
    return content_hash(content, totals, run_ids) != report.content_hash

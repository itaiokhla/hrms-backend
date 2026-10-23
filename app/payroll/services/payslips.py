"""Payslip v2 view model (3.6 §3-§4, 01 §5.7): everything a payslip shows, frozen from a finalized run employee.

``build(db, run, row)`` returns a ``PayslipModel``: the JSON view model the PDF renderer and the web view
both read (``content``), plus what the document row stores beside it (template version, languages,
labels). Nothing here writes; ``services.publication`` issues the document.

Sections (3.6 §3):

* header: the legal entity's document brand (``brand_service.document_brand``), legal name, address and
  registration lines, title ("Payslip" / "Revised payslip"), period and pay date;
* employee: name, code, designation, department, branch, joining date, pay group, currency and the bank
  account **masked** (``payslip.show_bank_masked``: the account the payment instruction used, else the
  active account; never decrypted). Tax identifiers are not printed: no country pack in this release
  requires them;
* attendance summary (``payslip.show_attendance_summary``): from the day ledger of the run employee's
  frozen snapshot, recomputed by the pure engine and used only when it reproduces the stored
  ``calc_hash`` (otherwise "Not available", never a guess);
* earnings, deductions, reimbursements (not wages) and employer contributions (only with
  ``payslip.show_employer_contributions``, titled "Paid by your employer — not part of your take-home
  pay"); ``HR_ONLY`` lines are never included; arrears show their original period; each line carries the
  engine's stored plain-language ``explanation`` ("Why this amount?" for payslip-only users), never its
  trace or sources;
* totals: the run employee's stored gross, deductions, reimbursements and net, net in words (locale-aware);
* payment: method and status **from payment evidence only** (``state_machines.employee_label``):
  "Paid on …" needs a verified settlement; otherwise "Scheduled", "Processing" or "Submitted for manual
  payment — unverified";
* leave balances (``payslip.show_leave_balances``), YTD column (``payslip.show_ytd``: finalized runs of
  the same legal entity and tax year up to this period);
* labels: "FIXTURE — not law" for fixture statutory lines (I-07) and "Statutory deductions not
  calculated — compliance not verified" when the entity has no verified pack (I-15).

The layout, section order, titles, footer note and signature come from the PAYSLIP_TEMPLATE policy that
applies to the employee on the period end (default template when none is assigned); the template version
recorded with the document identifies the policy version and its hash.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence

from babel.dates import format_date
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.employee import Department, Employee
from app.models.org import Branch, EmployeeAssignment, LegalEntity
from app.models.pay_org import Designation, PayGroup
from app.models.pay_runs import PayPeriod, PayrollInputSnapshot, PayrollLine, PayrollRun, PayrollRunEmployee
from app.payroll import policy_types as pt
from app.payroll import state_machines as sm
from app.payroll.banking import masking
from app.payroll.money import amount_str

logger = logging.getLogger(__name__)

SETTING_KEYS = (
    "payslip.publish_trigger", "payslip.scheduled_publish_time", "payslip.show_ytd",
    "payslip.show_employer_contributions", "payslip.show_leave_balances", "payslip.show_attendance_summary",
    "payslip.show_bank_masked", "payslip.show_tax_projection", "payslip.languages", "payslip.pdf_protection",
    "payslip.email_notify", "payslip.email_include_net_pay", "payslip.email_attach_pdf",
)
FIXTURE_LABEL = "FIXTURE — not law"
NOT_VERIFIED_LABEL = "Statutory deductions not calculated — compliance not verified"
PREVIEW_LABEL = "Statutory deductions calculated by an unverified country pack — compliance not verified"
SANDBOX_NOTE = "SANDBOX — no real money moved"
EMPLOYER_NOTE = "Paid by your employer — not part of your take-home pay"
OPTIONAL_SECTION_SETTINGS = {
    "ATTENDANCE_SUMMARY": "payslip.show_attendance_summary",
    "EMPLOYER_CONTRIBUTIONS": "payslip.show_employer_contributions",
    "LEAVE_BALANCES": "payslip.show_leave_balances",
    "TAX_SUMMARY": "payslip.show_tax_projection",
}
EXPLANATION_MAX = 500
SECTION_OF_KIND = {
    "EARNING": "earnings", "DEDUCTION": "deductions", "REIMBURSEMENT": "reimbursements",
    "EMPLOYER_CONTRIBUTION": "employer_contributions",
}


@dataclass
class PayslipModel:
    content: dict
    template_version: str
    template_policy_version_id: Optional[int]
    locale: str
    languages: list[str]
    labels: list[str]
    settings: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- settings and template


def payslip_settings(db: Session, run: PayrollRun, period: PayPeriod, employee_id: Optional[int] = None
                     ) -> dict[str, Any]:
    """The payslip.* settings for the run's entity / pay group on the period end (strict: invalid values raise)."""
    from app.payroll.services import settings as settings_service

    resolved = settings_service.resolve_many(
        db, run.company_id, SETTING_KEYS, legal_entity_id=run.legal_entity_id, pay_group_id=run.pay_group_id,
        on=period.period_end,
    )
    return {key: value.value for key, value in resolved.items()}


def resolved_template(db: Session, company_id: int, employee_id: int, on: date) -> tuple[dict, str, Optional[int]]:
    """(fields, template_version, policy_version_id) of the PAYSLIP_TEMPLATE applying to the employee on ``on``."""
    from app.payroll.services import policies

    try:
        found = policies.resolve_for_employee(db, company_id, "PAYSLIP_TEMPLATE", employee_id, on)
    except Exception:  # noqa: BLE001 - an unplaced employee or a policy error falls back to the default template
        logger.exception("PAYSLIP_TEMPLATE could not be resolved for employee %s", employee_id)
        found = {"status": "ERROR"}
    if found.get("status") == "RESOLVED" and found.get("version"):
        version = found["version"]
        fields = pt.validate_fields("PAYSLIP_TEMPLATE", version["fields"])
        tag = f"PAYSLIP_TEMPLATE:{found['policy']['id']}:v{version['version']}:{(version['policy_hash'] or '')[:12]}"
        return fields, tag[:60], version["id"]
    fields = pt.validate_fields("PAYSLIP_TEMPLATE", {})
    return fields, f"PAYSLIP_TEMPLATE:DEFAULT:{pt.policy_hash(fields)[:12]}", None


def template_block(fields: Mapping[str, Any], settings: Mapping[str, Any], languages: Sequence[str],
                   entity: Optional[LegalEntity]) -> dict:
    """What the renderer needs from the template: layout, visible sections in order, titles, footer, signature."""
    toggled_off = {code for code, key in OPTIONAL_SECTION_SETTINGS.items() if settings.get(key) is False}
    sections = [s["code"] for s in fields.get("sections") or []
                if s.get("visible", True) and (s["code"] in pt.MANDATORY_PAYSLIP_SECTIONS or s["code"] not in toggled_off)]
    overrides = fields.get("locale_overrides") or {}
    override: Mapping[str, Any] = {}
    for loc in languages:
        override = overrides.get(loc) or overrides.get(loc.split("-")[0]) or {}
        if override:
            break
    signature = fields.get("signature_block")
    if not signature and entity is not None and (entity.signatory_name or entity.signatory_title):
        signature = {"name": entity.signatory_name, "title": entity.signatory_title}
    return {
        "layout": fields.get("layout") or "CLASSIC",
        "sections": sections,
        "titles": dict(override.get("section_titles") or {}),
        "footer_note": override.get("footer_note") or fields.get("footer_note"),
        "signature": signature,
        "component_groups": list(fields.get("component_groups") or []),
    }


def languages_of(value: Any, company: Optional[Company], entity: Optional[LegalEntity]) -> list[str]:
    if isinstance(value, (list, tuple)) and value and all(isinstance(v, str) and v for v in value):
        return [str(v) for v in value][:2]
    fallback = (entity.default_locale if entity is not None and entity.default_locale else None) or (
        company.default_locale if company is not None else None) or "en"
    return [fallback]


def number_locale_for(languages: Sequence[str], entity: Optional[LegalEntity]) -> str:
    """The first language with the entity's region when it has none ("en" + IN -> "en-IN": Indian grouping)."""
    first = (languages[0] if languages else "en").replace("_", "-")
    if "-" not in first and entity is not None and entity.country_code:
        return f"{first}-{entity.country_code.upper()}"
    return first


# --------------------------------------------------------------------------- blocks


def _employer(entity: LegalEntity) -> dict:
    address = dict(entity.registered_address or {})
    parts = [address.get(k) for k in ("line1", "line2", "city", "state", "postal_code", "country")]
    lines = []
    for item in entity.registration_lines or []:
        if isinstance(item, Mapping) and item.get("label") and item.get("value"):
            lines.append({"label": str(item["label"]), "value": str(item["value"])})
    return {
        "legal_name": entity.legal_name, "trade_name": entity.trade_name, "code": entity.code,
        "country_code": entity.country_code, "address_line": ", ".join(str(p) for p in parts if p) or None,
        "registration_lines": lines,
    }


def _name(db: Session, model, company_id: int, object_id: Optional[int]) -> Optional[str]:
    if object_id is None:
        return None
    row = db.query(model).filter(model.company_id == company_id, model.id == object_id).first()
    return getattr(row, "name", None) if row is not None else None


def _bank_line(db: Session, company_id: int, employee_id: int, instruction) -> Optional[str]:
    """The masked account of the paying instruction, else the employee's active account; never decrypted."""
    if instruction is not None:
        snapshot = instruction.beneficiary_snapshot or {}
        masked = snapshot.get("account_masked")
        if masked:
            bank = snapshot.get("bank_name")
            return f"{bank} {masked}" if bank else str(masked)
    from app.models.payments import BankAccount
    from app.payroll.services import bank_accounts

    account = (db.query(BankAccount)
               .filter(BankAccount.company_id == company_id, BankAccount.employee_id == employee_id,
                       BankAccount.status == "ACTIVE")
               .order_by(BankAccount.id.desc()).first())
    if account is None:
        return None
    masked = bank_accounts.masked_number(account)
    return f"{account.bank_name} {masked}" if account.bank_name else masked


def _employee_block(db: Session, run: PayrollRun, row: PayrollRunEmployee, employee: Employee,
                    settings: Mapping[str, Any], instruction, languages: Sequence[str]) -> dict:
    cid = run.company_id
    assignment = db.query(EmployeeAssignment).filter(EmployeeAssignment.company_id == cid,
                                                     EmployeeAssignment.id == row.employee_assignment_id).first()
    designation = _name(db, Designation, cid, assignment.designation_id if assignment else None) or employee.position
    department = _name(db, Department, cid, (assignment.department_id if assignment else None)
                       or employee.department_id)
    branch = _name(db, Branch, cid, assignment.branch_id if assignment else None)
    pay_group = db.query(PayGroup).filter(PayGroup.company_id == cid, PayGroup.id == run.pay_group_id).first()
    block = {
        "id": employee.id, "name": employee.full_name, "code": employee.employee_code, "designation": designation,
        "department": department, "branch": branch,
        "joining_date": employee.hire_date.isoformat() if employee.hire_date else None,
        "pay_group": pay_group.name if pay_group else None,
    }
    if settings.get("payslip.show_bank_masked") is not False:
        block["bank"] = _bank_line(db, cid, employee.id, instruction)
    return block


def _component_names(db: Session, company_id: int, version_ids: Sequence[int]) -> dict[int, Any]:
    from app.models.compensation import PayComponentVersion

    wanted = sorted({v for v in version_ids if v is not None})
    if not wanted:
        return {}
    rows = db.query(PayComponentVersion).filter(PayComponentVersion.company_id == company_id,
                                                PayComponentVersion.id.in_(wanted)).all()
    return {r.id: (r.name, dict(r.translations or {})) for r in rows}


def _display_name(code: str, names: Optional[tuple], languages: Sequence[str]) -> str:
    if names is None:
        return code.replace("_", " ").title()
    name, translations = names
    parts = [name]
    for loc in languages[1:]:
        other = translations.get(loc) or translations.get(loc.split("-")[0])
        if isinstance(other, str) and other and other not in parts:
            parts.append(other)
    return " · ".join(parts)


def _period_ref_label(ref: Any, locale: str) -> Optional[str]:
    if not ref:
        return None
    if isinstance(ref, str):
        return f"Arrears — {ref}"
    if isinstance(ref, Mapping):
        if ref.get("label"):
            return f"Arrears — {ref['label']}"
        start = ref.get("start") or ref.get("period_start")
        if start:
            try:
                return f"Arrears — {format_date(date.fromisoformat(str(start)[:10]), 'MMM yyyy', locale=_babel(locale))}"
            except Exception:  # noqa: BLE001
                return f"Arrears — {start}"
    return "Arrears"


def employee_explanation(line: PayrollLine) -> Optional[str]:
    """The stored plain-language explanation of an EMPLOYEE_VISIBLE line ("Why this amount?"), bounded.

    Only the line's own ``explanation`` text written by the engine is used: never the trace, sources or
    inputs behind it, and never anything of an HR_ONLY line.
    """
    if line.visibility != "EMPLOYEE_VISIBLE" or not line.explanation:
        return None
    text = " ".join(str(line.explanation).split())
    return (text[:EXPLANATION_MAX - 1] + "…") if len(text) > EXPLANATION_MAX else (text or None)


def _babel(locale: str) -> str:
    return (locale or "en").replace("-", "_")


def period_label(day: date, locale: str) -> str:
    try:
        return format_date(day, "MMMM yyyy", locale=_babel(locale))
    except Exception:  # noqa: BLE001 - unknown locale: English
        return format_date(day, "MMMM yyyy", locale="en")


def ytd_amounts(db: Session, run: PayrollRun, row: PayrollRunEmployee, period: PayPeriod) -> dict[tuple[str, str], Decimal]:
    """(component, kind) -> sum over finalized runs of the entity and tax year up to this period (EC-CMP-20)."""
    query = (
        db.query(PayrollLine.component_code, PayrollLine.kind, func.sum(PayrollLine.amount))
        .join(PayrollRunEmployee, (PayrollRunEmployee.company_id == PayrollLine.company_id)
              & (PayrollRunEmployee.id == PayrollLine.run_employee_id))
        .join(PayrollRun, (PayrollRun.company_id == PayrollLine.company_id) & (PayrollRun.id == PayrollLine.run_id))
        .join(PayPeriod, (PayPeriod.company_id == PayrollRun.company_id) & (PayPeriod.id == PayrollRun.pay_period_id))
        .filter(PayrollLine.company_id == run.company_id, PayrollRunEmployee.employee_id == row.employee_id,
                PayrollRun.legal_entity_id == run.legal_entity_id, PayrollRun.status.in_(sm.RUN_FINALIZED_STATES),
                # Only finalized pay: a FULL hold (ON_HOLD) or an error keeps the employee out of finalization.
                PayrollRunEmployee.status == "CALCULATED",
                PayPeriod.tax_year == period.tax_year, PayPeriod.period_end <= period.period_end,
                PayrollLine.currency == row.currency, PayrollLine.visibility == "EMPLOYEE_VISIBLE")
        .group_by(PayrollLine.component_code, PayrollLine.kind)
    )
    return {(code, kind): Decimal(total) for code, kind, total in query}


def attendance_summary(db: Session, row: PayrollRunEmployee, lines: Sequence[PayrollLine]) -> Optional[dict]:
    """Day-ledger totals of the frozen snapshot, only when the pure engine reproduces the stored calc_hash."""
    if row.snapshot_id is None or not row.calc_hash:
        return None
    snapshot = db.query(PayrollInputSnapshot).filter(PayrollInputSnapshot.company_id == row.company_id,
                                                     PayrollInputSnapshot.id == row.snapshot_id).first()
    if snapshot is None:
        return None
    try:
        from app.payroll.engine import calculate

        result = calculate(snapshot.content)
    except Exception:  # noqa: BLE001 - an unreadable snapshot gives "Not available", never a guess
        logger.warning("Attendance summary unavailable for run employee %s", row.id)
        return None
    if result.calc_hash != row.calc_hash or not result.ledger:
        return None
    totals = (result.ledger or {}).get("totals") or {}
    if not totals:
        return None

    def num(key: str) -> Optional[str]:
        value = totals.get(key)
        if value is None:
            return None
        text = format(Decimal(str(value)).normalize(), "f")
        return text

    overtime = sum((Decimal(line.units) for line in lines
                    if line.units is not None and (line.category or "").upper().startswith("OVERTIME")), Decimal(0))
    leave = sum((Decimal(str(totals.get(k) or 0)) for k in ("leave_days_paid", "leave_days_unpaid",
                                                            "leave_days_statutory")), Decimal(0))
    return {
        "days_in_period": str(totals.get("days")) if totals.get("days") is not None else None,
        "paid_days": num("paid_days_after_penalty") or num("paid_days"),
        "lop_days": num("lop_days"),
        "overtime_hours": format(overtime.normalize(), "f") if overtime else "0",
        "leave_days": format(leave.normalize(), "f") if leave else "0",
    }


def payment_block(db: Session, row: PayrollRunEmployee) -> tuple[dict, Any]:
    """(block, representative instruction): the employee label from evidence only (contract §23.2, I-17)."""
    from app.models.payment_flows import PaymentInstruction, PaymentObligation
    from app.payroll.services import payment_events

    net = Decimal(row.net) if row.net is not None else Decimal(0)
    instructions = (
        db.query(PaymentInstruction)
        .join(PaymentObligation, (PaymentObligation.company_id == PaymentInstruction.company_id)
              & (PaymentObligation.id == PaymentInstruction.obligation_id))
        .filter(PaymentInstruction.company_id == row.company_id, PaymentObligation.run_employee_id == row.id,
                PaymentInstruction.status.notin_(("REISSUED", "CANCELLED")))
        .order_by(PaymentInstruction.id).all()
    )
    if not instructions:
        if net <= 0:
            return {"key": "NO_PAYMENT", "text": "No payment due for this period", "method": None,
                    "reference": None}, None
        return {"key": "SCHEDULED", "text": sm.EMPLOYEE_LABEL_TEXT["SCHEDULED"], "method": None, "reference": None}, None
    labels = [(i, payment_events.employee_label(i)) for i in instructions]
    unpaid = [(i, label) for i, label in labels if label["key"] != "PAID"]
    instruction, label = unpaid[0] if unpaid else max(labels, key=lambda pair: pair[0].settled_at or pair[0].created_at)
    text = label["text"]
    if label.get("badge") == "SANDBOX":
        text = f"{text} ({SANDBOX_NOTE})"
    reference = instruction.reference
    masked_ref = f"{masking.MASK}{reference[-4:]}" if reference and len(reference) >= 4 else None
    return {"key": label["key"], "text": text, "method": instruction.submission_channel,
            "reference": masked_ref, "sandbox": label.get("badge") == "SANDBOX"}, instruction


def leave_balances(db: Session, company_id: int, employee_id: int, year: int) -> list[dict]:
    from app.services import leave_service

    try:
        balances = leave_service.balances(db, company_id, employee_id, year)
    except Exception:  # noqa: BLE001
        logger.exception("Leave balances unavailable for employee %s", employee_id)
        return []
    return [{"code": code, "name": item["name"], "used": str(item["used"]),
             "remaining": None if item["remaining"] is None else str(item["remaining"])}
            for code, item in sorted(balances.items(), key=lambda kv: kv[1]["name"])]


def statutory_labels(db: Session, run: PayrollRun, period: PayPeriod, entity: LegalEntity,
                     lines: Sequence[PayrollLine]) -> list[str]:
    """I-07 / I-15 labels: fixtures are badged; an entity without a verified pack is never presented as compliant.

    Only a pack in VERIFIED mode counts as verification here: a fixture pack may let a test run finalize
    (``runs.statutory_status``), but its documents are test data and never read as compliant.
    """
    labels: list[str] = []
    if any(line.fixture for line in lines):
        labels.append(FIXTURE_LABEL)
    try:
        from app.payroll.services import runs

        status = runs.statutory_status(db, run, period, entity)
        verified = any(p.get("mode") == "VERIFIED" for p in status.get("packs") or [])
    except Exception:  # noqa: BLE001 - fail closed: unknown means not verified
        logger.exception("Statutory status unavailable for run %s", run.id)
        verified = False
    if not verified:
        statutory = [line for line in lines if line.statutory]
        if not statutory:
            labels.append(NOT_VERIFIED_LABEL)
        elif not all(line.fixture for line in statutory):
            labels.append(PREVIEW_LABEL)
    return labels


# --------------------------------------------------------------------------- the view model


def build(db: Session, run: PayrollRun, row: PayrollRunEmployee, *, version: int = 1,
          title_key: str = "PAYSLIP", replaces: Optional[str] = None,
          settings: Optional[Mapping[str, Any]] = None) -> PayslipModel:
    """The frozen view model of one finalized run employee's payslip."""
    from app.payroll.services import pdf_render
    from app.services import brand_service, domain_service

    cid = run.company_id
    if not sm.is_run_finalized(run.status):
        raise ValueError("payslips are built from finalized runs only")
    period = db.query(PayPeriod).filter(PayPeriod.company_id == cid, PayPeriod.id == run.pay_period_id).one()
    entity = db.query(LegalEntity).filter(LegalEntity.company_id == cid, LegalEntity.id == run.legal_entity_id).one()
    employee = db.query(Employee).filter(Employee.company_id == cid, Employee.id == row.employee_id).one()
    company = db.get(Company, cid)
    values = dict(settings) if settings is not None else payslip_settings(db, run, period, row.employee_id)
    languages = languages_of(values.get("payslip.languages"), company, entity)
    locale = number_locale_for(languages, entity)
    fields, template_version, template_policy_version_id = resolved_template(db, cid, row.employee_id,
                                                                              period.period_end)
    template = template_block(fields, values, languages, entity)

    lines = (db.query(PayrollLine)
             .filter(PayrollLine.company_id == cid, PayrollLine.run_employee_id == row.id)
             .order_by(PayrollLine.sort_order, PayrollLine.id).all())
    visible = [line for line in lines if line.visibility != "HR_ONLY"]
    names = _component_names(db, cid, [line.component_version_id for line in visible])
    show_ytd = values.get("payslip.show_ytd") is not False
    ytd = ytd_amounts(db, run, row, period) if show_ytd else {}
    sections: dict[str, list[dict]] = defaultdict(list)
    currency = row.currency
    for line in visible:
        kind = line.kind
        if kind == "ROUNDING":
            kind = "EARNING" if Decimal(line.amount) >= 0 else "DEDUCTION"
        section = SECTION_OF_KIND.get(kind)
        if section is None:  # benefits in kind and informational lines are not cash on the payslip
            continue
        amount = Decimal(line.amount)
        if line.kind == "ROUNDING" and amount < 0:
            amount = -amount
        item = {
            "code": line.component_code, "label": _display_name(line.component_code, names.get(line.component_version_id),
                                                              languages),
            "amount": amount_str(amount, line.currency),
            "rate": amount_str(Decimal(line.rate).quantize(Decimal(1).scaleb(-pdf_render.minor_unit(line.currency))),
                               line.currency) if (line.rate is not None and line.units is not None
                                                  and kind == "EARNING") else None,
            "units": format(Decimal(line.units).normalize(), "f") if line.units is not None else None,
            "period_ref_label": _period_ref_label(line.period_ref, locale),
            "statutory": bool(line.statutory), "fixture": bool(line.fixture),
            "explanation": employee_explanation(line),
        }
        if show_ytd and section in ("earnings", "deductions"):
            total = ytd.get((line.component_code, line.kind))
            item["ytd"] = amount_str(abs(total) if line.kind == "ROUNDING" else total, currency) \
                if total is not None else None
        sections[section].append(item)
    if values.get("payslip.show_employer_contributions") is not True:
        sections.pop("employer_contributions", None)

    payment, instruction = payment_block(db, row)
    net = Decimal(row.net or 0)
    totals = {
        "gross": amount_str(Decimal(row.gross or 0), currency),
        "deductions": amount_str(Decimal(row.deductions or 0), currency),
        "reimbursements": amount_str(Decimal(row.reimbursements or 0), currency),
        "net": amount_str(net, currency),
        "net_words": pdf_render.amount_in_words(net, currency, locale),
    }
    labels = statutory_labels(db, run, period, entity, lines)
    content: dict[str, Any] = {
        "kind": "PAYSLIP",
        "schema": 1,
        "title_key": title_key,
        "replaces": replaces,
        "version": version,
        "brand": brand_service.document_brand(cid, entity.id),
        "employer": _employer(entity),
        "employee": _employee_block(db, run, row, employee, values, instruction, languages),
        "period": {
            "start": period.period_start.isoformat(), "end": period.period_end.isoformat(),
            "pay_date": period.pay_date.isoformat(), "label": period_label(period.period_start, locale),
            "tax_year": period.tax_year,
        },
        "run": {"id": run.id, "number": run.number, "run_type": run.run_type},
        "currency": currency,
        "earnings": sections.get("earnings", []),
        "deductions": sections.get("deductions", []),
        "reimbursements": sections.get("reimbursements", []),
        "employer_contributions": sections.get("employer_contributions", []),
        "employer_note": EMPLOYER_NOTE,
        "totals": totals,
        "payment": payment,
        "attendance": attendance_summary(db, row, lines) if values.get("payslip.show_attendance_summary") is not False
        else None,
        "leave_balances": leave_balances(db, cid, row.employee_id, period.period_end.year)
        if values.get("payslip.show_leave_balances") is not False else [],
        "tax_summary": None,
        "show_ytd": show_ytd,
        "labels": labels,
        "languages": languages,
        "locale": locale,
        "template": template,
        "verify_base": domain_service.public_base_url(cid),
    }
    return PayslipModel(content=content, template_version=template_version,
                        template_policy_version_id=template_policy_version_id, locale=locale, languages=languages,
                        labels=labels, settings=values)


def source_key(run_employee_id: int) -> str:
    """The v2 namespace key of a run employee's payslip (decision I-19), never a legacy payroll id."""
    return f"run_employee:{int(run_employee_id)}"


def file_stem(employee: Employee, period_start: date) -> str:
    return f"payslip-{employee.employee_code}-{period_start.strftime('%Y-%m')}"


__all__ = [
    "EMPLOYER_NOTE", "FIXTURE_LABEL", "NOT_VERIFIED_LABEL", "PayslipModel", "build", "file_stem", "languages_of",
    "payment_block", "payslip_settings", "resolved_template", "source_key", "template_block", "ytd_amounts",
]

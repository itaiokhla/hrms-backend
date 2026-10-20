"""Issued documents (payslips): frozen at approval, never changed afterwards (01 §5.7, EC-WL-09).

When payroll is approved, a snapshot of everything the payslip shows (figures,
employee, employer legal entity, effective brand and document branding) is stored
with the brand version and template version, and the PDF is rendered once and kept.
Downloads return those stored bytes, so later logo, colour or name changes never
alter an issued payslip. If the stored file is missing, the PDF is rendered again from
the same snapshot and the same content-addressed brand files, which are never deleted.

Payroll approved before this feature existed is issued on its first download.
Drafts are rendered on the fly with a DRAFT watermark and are never stored.
"""

import hashlib
import logging
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app.core import storage
from app.core.principal import Principal
from app.core.security import utcnow
from app.models.branding import IssuedDocument
from app.models.employee import Employee
from app.models.org import LegalEntity
from app.models.payroll import Payroll
from app.services import brand_asset_service, brand_service, org_structure_service
from app.utils.pdf_generator import PAYSLIP_TEMPLATE_VERSION, SalarySlipGenerator

logger = logging.getLogger(__name__)

PAYSLIP = "PAYSLIP"


def _employer(entity: Optional[LegalEntity], company_name: str) -> dict:
    if entity is None:
        return {"legal_name": company_name, "address": {}, "registration_lines": []}
    return {
        "legal_name": entity.legal_name,
        "trade_name": entity.trade_name,
        "country_code": entity.country_code,
        "address": dict(entity.registered_address or {}),
        "registration_lines": list(entity.registration_lines or []),
        "code": entity.code,
    }


def build_snapshot(db: Session, payroll: Payroll, status: Optional[str] = None) -> dict:
    employee = db.get(Employee, payroll.employee_id)
    entity = db.get(LegalEntity, payroll.legal_entity_id) if payroll.legal_entity_id else (
        org_structure_service.entity_for_employee_on(db, payroll.company_id, payroll.employee_id, payroll.salary_month)
    )
    brand = brand_service.document_brand(payroll.company_id, entity.id if entity else None)
    department, branch = org_structure_service.placement_names(
        db, payroll.company_id, payroll.employee_id, payroll.salary_month
    )
    earnings = [
        ("Basic salary", payroll.basic_salary), ("House rent allowance", payroll.house_rent_allowance),
        ("Dearness allowance", payroll.dearness_allowance), ("Conveyance allowance", payroll.conveyance_allowance),
        ("Other allowances", payroll.other_allowances), ("Overtime", payroll.overtime_amount), ("Bonus", payroll.bonus),
    ]
    deductions = [
        ("Provident fund", payroll.provident_fund), ("Insurance", payroll.insurance),
        ("Professional tax", payroll.professional_tax), ("Income tax", payroll.income_tax),
        ("Attendance deductions", payroll.other_deductions),
    ]
    return {
        "template_version": PAYSLIP_TEMPLATE_VERSION,
        "issued_at": utcnow().isoformat(),
        "document_number": f"PS-{payroll.company_id}-{payroll.id:06d}",
        "status": status or payroll.status,
        "brand": brand,
        "employer": _employer(entity, brand["display_name"]),
        "employee": {
            "name": employee.full_name if employee else "-",
            "employee_code": employee.employee_code if employee else "-",
            "department": department or (employee.department.name if employee and employee.department else None),
            "designation": employee.position if employee else None,
            "branch": branch,
        },
        "period": payroll.salary_month.strftime("%B %Y"),
        "currency": payroll.currency,
        "earnings": [[label, float(amount)] for label, amount in earnings if amount],
        "deductions": [[label, float(amount)] for label, amount in deductions if amount] or [["Deductions", 0.0]],
        "gross": float(payroll.gross_salary),
        "total_deductions": float(payroll.total_deductions),
        "net": float(payroll.net_salary),
        "attendance": {
            "working_days": payroll.working_days, "present_days": payroll.present_days,
            "absent_days": payroll.absent_days, "half_days": payroll.leave_days,
        },
    }


def _logo_bytes(snapshot: dict) -> Optional[bytes]:
    logo = (snapshot.get("brand") or {}).get("logo")
    if not logo:
        return None
    data = brand_asset_service.read_asset(logo["sha256"], logo["ext"])
    if data is None:
        logger.error("Payslip logo %s.%s is missing; rendering without it", logo["sha256"], logo["ext"])
    return data


def render(snapshot: dict) -> bytes:
    return SalarySlipGenerator.generate_pdf(snapshot, _logo_bytes(snapshot)).getvalue()


def issue_payslip(db: Session, payroll: Payroll, user_id: Optional[int]) -> IssuedDocument:
    existing = db.query(IssuedDocument).filter(
        IssuedDocument.company_id == payroll.company_id, IssuedDocument.document_type == PAYSLIP,
        IssuedDocument.source_id == payroll.id,
    ).first()
    if existing is not None:
        return existing
    snapshot = build_snapshot(db, payroll, status="issued")
    data = render(snapshot)
    path = storage.save_bytes(payroll.company_id, "documents", data, "pdf")
    document = IssuedDocument(
        company_id=payroll.company_id, document_type=PAYSLIP, source_id=payroll.id, employee_id=payroll.employee_id,
        legal_entity_id=payroll.legal_entity_id, brand_profile_id=snapshot["brand"]["tenant_profile_id"],
        entity_brand_profile_id=snapshot["brand"]["entity_profile_id"], brand_version=snapshot["brand"]["brand_version"],
        template_version=PAYSLIP_TEMPLATE_VERSION, snapshot=snapshot, sha256=hashlib.sha256(data).hexdigest(),
        storage_path=path, issued_by_user_id=user_id,
    )
    db.add(document)
    db.flush()
    return document


def payslip_pdf(db: Session, principal: Principal, payroll: Payroll) -> Tuple[bytes, Optional[IssuedDocument]]:
    """(pdf bytes, issued document) for a payroll row the caller is allowed to see."""
    if payroll.status == "draft":
        return render(build_snapshot(db, payroll, status="draft")), None
    document = db.query(IssuedDocument).filter(
        IssuedDocument.company_id == payroll.company_id, IssuedDocument.document_type == PAYSLIP,
        IssuedDocument.source_id == payroll.id,
    ).first()
    if document is None:
        document = issue_payslip(db, payroll, principal.user_id)
        db.commit()
    try:
        data = storage.resolve(payroll.company_id, document.storage_path).read_bytes()
        if hashlib.sha256(data).hexdigest() == document.sha256:
            return data, document
        logger.error("Issued payslip %s failed its integrity check; re-rendering from the snapshot", document.id)
    except Exception:
        logger.error("Issued payslip %s file is missing; re-rendering from the snapshot", document.id)
    data = render(document.snapshot)
    if hashlib.sha256(data).hexdigest() != document.sha256:
        # Same snapshot, same brand files, same template: only a missing logo file could differ.
        logger.warning("Re-rendered payslip %s differs from the original bytes", document.id)
    return data, document

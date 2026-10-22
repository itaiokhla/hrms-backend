"""Payroll v2 documents: numbering, issuing, storage, integrity, access, publication state and verification.

* Numbering (3.6 §9): ``documents.numbering.<type>.pattern`` per legal entity, tokens ``{LE}``,
  ``{FY}``, ``{YYYY}``, ``{MM}`` and ``{SEQ:n}``. ``allocate_number`` locks the sequence row
  (``SELECT ... FOR UPDATE``) in the issuing transaction, so concurrent issuers queue and invoices
  and credit notes are gapless (EC-DOC-08); a rolled-back issue rolls the counter back with it, and
  a number already used (for example after a pattern change) is skipped, never reused (EC-DOC-09).
* Issuing (``issue``): the frozen view model is rendered once, the bytes are written to private
  tenant storage under a random name and the row stores their SHA-256 with the brand version,
  template version and renderer (EC-DOC-03). A storage failure raises before any row exists, so a
  partial file is never registered or served (EC-DOC-16); files of a rolled-back transaction are
  removed.
* Reading (``document_bytes``): the stored file is served only when its hash matches; otherwise
  the document is re-rendered from the same content, template and brand files and the hashes are
  compared; the document is marked re-rendered (and ``rerender_mismatch`` on a difference), audited
  and logged (3.6 §6.2).
* Publication state changes only through ``transition`` (the DOCUMENT machine): row lock,
  ``row_version`` check (409 on conflict), reason where the machine requires one, audit.
* Access: HR reaches documents of legal entities / pay groups in the scope of the permission;
  an employee reaches their own documents only once published (EC-DOC-01/02). Unknown or
  out-of-scope ids are 404.
* Verification (``verify_public``): issuer, type, issue date, number and the masked name only,
  never amounts (EC-DOC-18).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional, Sequence

from sqlalchemy import and_, event, false, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core import storage
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.core.security import utcnow
from app.models.employee import Employee
from app.models.org import LegalEntity
from app.models.pay_documents import (
    DOC_TYPES,
    DocumentAccessLog,
    DocumentSequence,
    Invoice,
    PayDocument,
)
from app.payroll import audit
from app.payroll import state_machines as sm
from app.payroll.banking import masking
from app.payroll.services import access
from app.services.audit_log_service import AuditLogService

logger = logging.getLogger(__name__)

PAYSLIP = "PAYSLIP_V2"
REVERSAL_STATEMENT = "REVERSAL_STATEMENT"
READ = "payroll:payslip:read"
PUBLISH = sm.PAYSLIP_PUBLISH
STORAGE_CATEGORY = "payroll-documents"
PDF_MIME = "application/pdf"
FILE_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}

# doc_type -> (numbering setting type, extra prefix, gapless). None: not numbered (3.6 §2).
NUMBERING: Mapping[str, Optional[tuple[str, str, bool]]] = {
    PAYSLIP: ("payslip", "", False),
    REVERSAL_STATEMENT: ("payslip", "RV-", False),
    "PAYMENT_ADVICE": ("payment_advice", "", False),
    "INVOICE": ("invoice", "", True),
    "SELF_BILLED_INVOICE": ("invoice", "SB-", True),
    "CREDIT_NOTE": ("credit_note", "", True),
    "PAYROLL_REGISTER": ("payroll_register", "", False),
    "BANK_TRANSFER_REPORT": ("bank_transfer_report", "", False),
    "FUNDING_REQUEST": ("funding_request", "", False),
    "FINAL_SETTLEMENT_STATEMENT": ("final_settlement_statement", "", False),
    "SALARY_CERTIFICATE": ("salary_certificate", "", False),
    "EMPLOYMENT_LETTER": ("employment_letter", "", False),
    "INCREMENT_LETTER": ("increment_letter", "", False),
    "LOAN_STATEMENT": None,
    "REIMBURSEMENT_STATEMENT": None,
}
# Documents an employee (or a contractor payee) may see as their own once published; everything else is
# HR / finance only.
EMPLOYEE_FACING = frozenset({
    PAYSLIP, REVERSAL_STATEMENT, "PAYMENT_ADVICE", "FINAL_SETTLEMENT_STATEMENT", "SALARY_CERTIFICATE",
    "EMPLOYMENT_LETTER", "INCREMENT_LETTER", "LOAN_STATEMENT", "REIMBURSEMENT_STATEMENT", "INVOICE",
    "SELF_BILLED_INVOICE", "CREDIT_NOTE",
})
# Which permission reaches which document type for HR (besides payroll:payslip:read).
TYPE_PERMISSION: Mapping[str, str] = {
    **{t: READ for t in EMPLOYEE_FACING},
    "PAYROLL_REGISTER": "payroll:reports:read",
    "FUNDING_REQUEST": "payroll:reports:read",
    "BANK_TRANSFER_REPORT": "payroll:payment:prepare",
    "INVOICE": "payroll:payment:prepare",
    "SELF_BILLED_INVOICE": "payroll:payment:prepare",
    "CREDIT_NOTE": "payroll:payment:prepare",
}
TOKEN_RE = re.compile(r"\{(LE|FY|YYYY|MM|SEQ:(\d{1,2}))\}")
FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")
_FILES_KEY = "payroll_documents_written_files"


# --------------------------------------------------------------------------- pure helpers


def canonical_json(value: Any) -> str:
    """Sorted keys, no whitespace, Decimals and dates as strings: the hashed form of a view model."""

    def default(item: Any) -> Any:
        if isinstance(item, Decimal):
            return format(item, "f")
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        raise TypeError(f"{type(item).__name__} is not JSON serializable")

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=default)


def content_hash(content: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def jsonable(value: Any) -> Any:
    """The canonical JSON value (round trip through canonical_json)."""
    return json.loads(canonical_json(value))


def escape_cell(value: Any) -> str:
    """A CSV/XLSX cell that a spreadsheet never evaluates: =, +, -, @, tab or CR first get an apostrophe (EC-DOC-17)."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(FORMULA_TRIGGERS) else text


def fiscal_year_label(on: date, fiscal_year_start: str | None) -> str:
    """``2026-27`` for a fiscal year starting 1 April; the calendar year when it starts on 1 January."""
    month, day = (int(part) for part in (fiscal_year_start or "01-01").split("-"))
    start_year = on.year if (on.month, on.day) >= (month, day) else on.year - 1
    if (month, day) == (1, 1):
        return str(start_year)
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def scope_key(pattern: str, on: date, fiscal_year_start: str | None) -> str:
    """The counter a pattern restarts with: per fiscal year, calendar year, month, or never ("ALL")."""
    tokens = {m.group(1) for m in TOKEN_RE.finditer(pattern)}
    if "MM" in tokens:
        return f"{on.year:04d}-{on.month:02d}"
    if "FY" in tokens:
        return f"FY{fiscal_year_label(on, fiscal_year_start)}"
    if "YYYY" in tokens:
        return f"{on.year:04d}"
    return "ALL"


def render_number(pattern: str, *, entity_code: str, on: date, fiscal_year_start: str | None, seq: int) -> str:
    """``PS/{LE}/{FY}/{SEQ:6}`` -> ``PS/IN01/2026-27/000412``."""

    def token(match: re.Match) -> str:
        name = match.group(1)
        if name == "LE":
            return entity_code
        if name == "FY":
            return fiscal_year_label(on, fiscal_year_start)
        if name == "YYYY":
            return f"{on.year:04d}"
        if name == "MM":
            return f"{on.month:02d}"
        width = int(match.group(2))
        return str(seq).zfill(width)

    return TOKEN_RE.sub(token, pattern)


def new_public_id() -> str:
    """24 url-safe random characters (144 bits): not guessable, not derived from any id."""
    return secrets.token_urlsafe(18)


def safe_file_name(value: str, ext: str = "pdf") -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")[:100] or "document"
    return f"{stem}.{ext}"


# --------------------------------------------------------------------------- numbering


def numbering_setting(doc_type: str) -> Optional[str]:
    rule = NUMBERING.get(doc_type)
    return None if rule is None else f"documents.numbering.{rule[0]}.pattern"


def numbering_pattern(db: Session, company_id: int, legal_entity_id: int, doc_type: str, on: date) -> Optional[str]:
    """The effective pattern of the entity on ``on`` (None for unnumbered types)."""
    from app.payroll.services import settings as settings_service

    rule = NUMBERING.get(doc_type)
    if doc_type not in NUMBERING:
        raise ValueError(f"unknown document type {doc_type!r}")
    if rule is None:
        return None
    key = numbering_setting(doc_type)
    try:
        resolved = settings_service.resolve(db, company_id, key, legal_entity_id=legal_entity_id, on=on)
    except settings_service.SettingConfigError as exc:
        raise conflict(f"The numbering pattern {key} is invalid: {exc.message}", "NUMBERING_PATTERN_INVALID")
    pattern = resolved.value
    if not isinstance(pattern, str) or len([m for m in TOKEN_RE.finditer(pattern) if m.group(1).startswith("SEQ")]) != 1:
        raise conflict(f"The numbering pattern {key} has no valid value.", "NUMBERING_PATTERN_INVALID")
    return f"{rule[1]}{pattern}"


def _number_taken(db: Session, company_id: int, legal_entity_id: int, doc_type: str, number: str) -> bool:
    used = db.query(PayDocument.id).filter(
        PayDocument.company_id == company_id, PayDocument.legal_entity_id == legal_entity_id,
        PayDocument.doc_type == doc_type, PayDocument.number == number,
    ).first()
    if used is not None:
        return True
    if doc_type in ("INVOICE", "SELF_BILLED_INVOICE", "CREDIT_NOTE"):
        kind = "CREDIT_NOTE" if doc_type == "CREDIT_NOTE" else "INVOICE"
        return db.query(Invoice.id).filter(
            Invoice.company_id == company_id, Invoice.legal_entity_id == legal_entity_id, Invoice.kind == kind,
            Invoice.number == number,
        ).first() is not None
    return False


def allocate_number(db: Session, company_id: int, legal_entity_id: int, doc_type: str, on: date) -> Optional[str]:
    """Next number of the entity's sequence, allocated in the caller's (issuing) transaction.

    The sequence row is created on first use and locked ``FOR UPDATE``: concurrent issuers wait for
    each other, so numbers are unique and, for gapless types, consecutive (EC-DOC-08). A rendered
    number that already exists is skipped, so no number is ever issued twice (EC-DOC-09).
    """
    pattern = numbering_pattern(db, company_id, legal_entity_id, doc_type, on)
    if pattern is None:
        return None
    entity = db.query(LegalEntity).filter(LegalEntity.company_id == company_id,
                                          LegalEntity.id == legal_entity_id).one_or_none()
    if entity is None:
        raise not_found("Legal entity")
    key = scope_key(pattern, on, entity.fiscal_year_start)
    gapless = bool(NUMBERING[doc_type][2])
    now = utcnow()
    db.execute(
        insert(DocumentSequence)
        .values(company_id=company_id, legal_entity_id=legal_entity_id, doc_type=doc_type, scope_key=key,
                pattern=pattern, next_value=1, gapless=gapless, row_version=1, created_at=now, updated_at=now)
        .on_conflict_do_nothing(index_elements=["company_id", "legal_entity_id", "doc_type", "scope_key"])
    )
    row = (
        db.query(DocumentSequence)
        .filter(DocumentSequence.company_id == company_id, DocumentSequence.legal_entity_id == legal_entity_id,
                DocumentSequence.doc_type == doc_type, DocumentSequence.scope_key == key)
        .with_for_update()
        .populate_existing()
        .one()
    )
    for _ in range(1000):
        value = int(row.next_value)
        row.next_value = value + 1
        number = render_number(pattern, entity_code=entity.code, on=on, fiscal_year_start=entity.fiscal_year_start,
                               seq=value)
        if not _number_taken(db, company_id, legal_entity_id, doc_type, number):
            break
    else:  # pragma: no cover - a thousand collisions means the pattern lost its sequence token
        raise conflict("No free document number is left in this sequence.", "NUMBERING_EXHAUSTED")
    row.pattern = pattern
    row.row_version += 1
    db.flush()
    return number


# --------------------------------------------------------------------------- storage


def _cleanup_on_rollback(session: Session, previous_transaction) -> None:
    if previous_transaction.parent is not None:
        return
    for company_id, path in session.info.pop(_FILES_KEY, []):
        storage.delete(company_id, path)


def _forget_on_commit(session: Session) -> None:
    session.info.pop(_FILES_KEY, None)


def store_bytes(db: Session, company_id: int, data: bytes, ext: str = "pdf") -> str:
    """Write a file to private tenant storage; it is removed again if the transaction rolls back."""
    if not data:
        raise ValueError("an empty document is never stored")
    path = storage.save_bytes(company_id, STORAGE_CATEGORY, data, ext)
    files = db.info.get(_FILES_KEY)
    if files is None:
        files = db.info[_FILES_KEY] = []
        event.listen(db, "after_soft_rollback", _cleanup_on_rollback)
        event.listen(db, "after_commit", _forget_on_commit)
    files.append((company_id, path))
    return path


def _read_stored(doc: PayDocument) -> Optional[bytes]:
    if not doc.storage_path:
        return None
    try:
        data = storage.resolve(doc.company_id, doc.storage_path).read_bytes()
    except Exception:  # missing file, refused path or storage outage: re-render instead
        return None
    return data if hashlib.sha256(data).hexdigest() == doc.sha256 else None


# --------------------------------------------------------------------------- issuing


def issue(
    db: Session,
    principal: Optional[Principal],
    *,
    company_id: int,
    doc_type: str,
    legal_entity_id: int,
    subject_type: str,
    subject_id: int,
    source_key: str,
    content: dict,
    title: str,
    file_stem: str,
    version: int = 1,
    issue_date: date,
    number_date: Optional[date] = None,
    employee_id: Optional[int] = None,
    pay_group_id: Optional[int] = None,
    run_id: Optional[int] = None,
    run_employee_id: Optional[int] = None,
    batch_id: Optional[int] = None,
    instruction_id: Optional[int] = None,
    period: Optional[tuple[date, date]] = None,
    source_refs: Optional[Mapping[str, Any]] = None,
    draft: bool = False,
    number: Optional[str] = None,
    allocate: bool = True,
    supersedes: Optional[PayDocument] = None,
    template_version: str,
    template_policy_version_id: Optional[int] = None,
    locale: str = "en",
    languages: Sequence[str] = ("en",),
    labels: Sequence[str] = (),
    retention_until: Optional[date] = None,
    publish: Optional[str] = None,
) -> PayDocument:
    """Number, render, store and register one document version; idempotent on (type, source_key, version).

    ``content`` must not carry the number yet: it is added here, so the stored view model and the
    PDF show the allocated number. ``publish`` (a trigger name) publishes at once (employee-facing
    statements and letters, ON_ISSUE).
    """
    from app.payroll.services import pdf_render

    if doc_type not in DOC_TYPES:
        raise ValueError(f"unknown document type {doc_type!r}")
    existing = find_version(db, company_id, doc_type, source_key, version)
    if existing is not None:
        return existing
    if number is None and allocate and not draft:
        number = allocate_number(db, company_id, legal_entity_id, doc_type, number_date or issue_date)
    public_id = new_public_id()
    frozen = jsonable({**content, "number": number, "public_id": public_id, "issue_date": issue_date,
                       "doc_type": doc_type, "draft": draft})
    data, logo_missing = pdf_render.render_with_brand(frozen)
    if logo_missing:
        logger.error("Document %s/%s rendered without its logo file", doc_type, source_key)
    path = store_bytes(db, company_id, data)
    brand = frozen.get("brand") or {}
    doc = PayDocument(
        company_id=company_id, legal_entity_id=legal_entity_id, pay_group_id=pay_group_id, doc_type=doc_type,
        number=number, public_id=public_id, subject_type=subject_type, subject_id=subject_id,
        employee_id=employee_id, run_id=run_id, run_employee_id=run_employee_id, batch_id=batch_id,
        instruction_id=instruction_id, period_start=period[0] if period else None,
        period_end=period[1] if period else None, issue_date=issue_date, source_key=source_key, version=version,
        source_refs=jsonable(dict(source_refs or {})), title=title[:150], status="GENERATED", draft=draft,
        content=frozen, content_hash=content_hash(frozen), storage_path=path,
        sha256=hashlib.sha256(data).hexdigest(), bytes=len(data), mime=PDF_MIME,
        file_name=safe_file_name(f"{file_stem}-{number}" if number else file_stem),
        brand_version=brand.get("brand_version"), brand_profile_id=brand.get("tenant_profile_id"),
        entity_brand_profile_id=brand.get("entity_profile_id"), template_version=template_version[:60],
        template_policy_version_id=template_policy_version_id, renderer=pdf_render.RENDERER, locale=locale,
        languages=list(languages), labels=list(labels), supersedes_document_id=supersedes.id if supersedes else None,
        retention_until=retention_until, issued_by_user_id=principal.user_id if principal else None, row_version=1,
    )
    db.add(doc)
    db.flush()
    AuditLogService.log(
        db, principal, audit.PAYSLIPS, "DOCUMENT_GENERATED",
        f"{doc_type.replace('_', ' ').title()} {number or doc.id} generated", "document", doc.id,
        company_id=company_id, actor_type="user" if principal else "system",
        details={"doc_type": doc_type, "number": number, "version": version, "source_key": source_key,
                 "sha256": doc.sha256, "template_version": doc.template_version, "brand_version": doc.brand_version,
                 "draft": draft},
    )
    if publish is not None:
        transition(db, None, doc, "PUBLISH", trigger=publish)
    return doc


def find_version(db: Session, company_id: int, doc_type: str, source_key: str, version: int) -> Optional[PayDocument]:
    return db.query(PayDocument).filter(
        PayDocument.company_id == company_id, PayDocument.doc_type == doc_type,
        PayDocument.source_key == source_key, PayDocument.version == version,
    ).one_or_none()


def latest_version(db: Session, company_id: int, doc_type: str, source_key: str) -> Optional[PayDocument]:
    return (db.query(PayDocument)
            .filter(PayDocument.company_id == company_id, PayDocument.doc_type == doc_type,
                    PayDocument.source_key == source_key)
            .order_by(PayDocument.version.desc()).first())


# --------------------------------------------------------------------------- reading


def document_bytes(db: Session, principal: Optional[Principal], doc: PayDocument) -> tuple[bytes, bool]:
    """(bytes, rerendered). Stored bytes when intact, else a re-render compared with the stored hash."""
    from app.payroll.services import pdf_render

    data = _read_stored(doc)
    if data is not None:
        return data, False
    rendered, _ = pdf_render.render_with_brand(doc.content)
    digest = hashlib.sha256(rendered).hexdigest()
    matches = digest == doc.sha256
    row = lock(db, doc.company_id, doc.id)
    if matches:
        try:
            row.storage_path = store_bytes(db, doc.company_id, rendered)
        except Exception:  # storage still down: serve the verified re-render without storing it
            logger.error("Re-rendered document %s could not be stored again", doc.id)
    else:
        logger.error("Re-rendered document %s differs from its original bytes (stored %s, now %s)",
                     doc.id, doc.sha256, digest)
        row.rerender_mismatch = True
    row.rerendered = True
    row.rerendered_at = utcnow()
    row.row_version += 1
    AuditLogService.log(
        db, principal, audit.PAYSLIPS, "DOCUMENT_REGENERATED",
        f"Document {doc.number or doc.id} re-rendered from its frozen content", "document", doc.id,
        company_id=doc.company_id, actor_type="user" if principal else "system",
        details={"matches_original": matches, "sha256": doc.sha256, "rerendered_sha256": digest},
    )
    log_access(db, doc, principal.user_id if principal else None, "REGENERATE", None,
               {"matches_original": matches})
    db.flush()
    return rendered, True


def log_access(db: Session, doc: Optional[PayDocument], user_id: Optional[int], action: str, ip: Optional[str],
               details: Optional[Mapping[str, Any]] = None, *, export_job_id: Optional[int] = None) -> None:
    db.add(DocumentAccessLog(
        company_id=doc.company_id if doc is not None else None, document_id=doc.id if doc is not None else None,
        export_job_id=export_job_id, user_id=user_id, action=action, ip=(ip or None) and ip[:64], at=utcnow(),
        details=jsonable(dict(details or {})),
    ))


# --------------------------------------------------------------------------- state (DOCUMENT machine)


def lock(db: Session, company_id: int, document_id: int) -> PayDocument:
    row = (db.query(PayDocument)
           .filter(PayDocument.company_id == company_id, PayDocument.id == document_id)
           .with_for_update().populate_existing().one_or_none())
    if row is None:
        raise not_found("Document")
    return row


def _machine_error(exc: sm.InvalidTransition) -> AppError:
    return conflict(f"This document cannot {exc.action.lower().replace('_', ' ')} from {exc.state}.", exc.code)


def transition(
    db: Session,
    principal: Optional[Principal],
    doc: PayDocument | int,
    action: str,
    *,
    reason: Optional[str] = None,
    expected_version: Optional[int] = None,
    trigger: Optional[str] = None,
    details: Optional[Mapping[str, Any]] = None,
) -> PayDocument:
    """The one status change of a document (DOCUMENT machine): lock, version check, permission, audit.

    ``principal`` None is the system (publication triggers, delivery receipts, views); a user action
    needs the machine's permission. Guards that depend on the payslip's run (publish trigger met,
    run not reversed) are checked by the caller (services.publication).
    """
    company_id = doc.company_id if isinstance(doc, PayDocument) else (principal.company_id if principal else None)
    document_id = doc.id if isinstance(doc, PayDocument) else int(doc)
    if company_id is None:
        raise ValueError("a system transition needs the document row")
    row = lock(db, company_id, document_id)
    if expected_version is not None and row.row_version != expected_version:
        raise conflict("This document was changed by someone else; reload and try again.", "ROW_VERSION_CONFLICT")
    try:
        spec = sm.DOCUMENT.spec(row.status, action)
    except sm.InvalidTransition as exc:
        raise _machine_error(exc)
    if spec.permission is not None:
        if principal is None:
            raise ValueError(f"{action} is a user action")
        if not principal.has(spec.permission):
            raise forbidden()
    elif principal is not None and action in ("PUBLISH", "SUPERSEDE", "REVERSE"):
        raise forbidden()
    text = (reason or "").strip()
    if spec.reason_required and not text:
        raise bad_request("A reason is required.", "REASON_REQUIRED")
    now = utcnow()
    before = row.status
    row.status = spec.target
    if spec.target == "PUBLISHED" and before == "GENERATED":
        row.published_at = now
        row.published_by_user_id = principal.user_id if principal else None
        row.publish_reason = text or None
        row.publish_trigger = "MANUAL" if action == "PUBLISH_MANUALLY" else (trigger or "ON_FINALIZE")
    elif spec.target == "VIEWED" and row.first_viewed_at is None:
        row.first_viewed_at = now
    elif spec.target == "SUPERSEDED":
        row.superseded_at = now
    elif spec.target == "REVERSED":
        row.reversed_at = now
    row.row_version += 1
    db.flush()
    if spec.audit_event or action in ("SUPERSEDE", "REVERSE", "RECORD_EMAIL_SENT", "RECORD_EMAIL_FAILED"):
        AuditLogService.log(
            db, principal, audit.PAYSLIPS, spec.audit_event or f"DOCUMENT_{action}",
            f"Document {row.number or row.id}: {before} → {row.status}", "document", row.id,
            company_id=company_id, actor_type="user" if principal else "system",
            details={"action": action, "from": before, "to": row.status, "reason": text or None,
                     "trigger": row.publish_trigger if spec.target == "PUBLISHED" else None, **dict(details or {})},
        )
    return row


# --------------------------------------------------------------------------- access


def staff_clause(principal: Principal, permission: str):
    """Documents in the run-level scope of ``permission`` (legal entity / pay group); SELF never counts."""
    return access.run_scope_clause(principal, permission, PayDocument.legal_entity_id, PayDocument.pay_group_id)


def has_staff_scope(principal: Principal, permission: str) -> bool:
    return not access.target_scope(principal, permission).is_empty and any(
        scope != "SELF" for scope in principal.scopes(permission))


def own_clause(principal: Principal):
    """The caller's own employee-facing documents once published (EC-DOC-02)."""
    if principal.employee_id is None:
        return false()
    return and_(PayDocument.employee_id == principal.employee_id, PayDocument.status != "GENERATED",
                PayDocument.doc_type.in_(sorted(EMPLOYEE_FACING)), PayDocument.draft.is_(False))


def require_staff(principal: Principal, permission: str) -> None:
    """403 unless the caller holds ``permission`` at a scope beyond SELF."""
    if not has_staff_scope(principal, permission):
        raise forbidden()


def get_for_staff(db: Session, principal: Principal, document_id: int, *, doc_types: Iterable[str] | None = None,
                  permission: Optional[str] = None, lock_row: bool = False) -> PayDocument:
    """A document in the caller's HR scope, or 404 (403 without the permission beyond SELF)."""
    wanted = tuple(doc_types) if doc_types else None
    perm = permission or (TYPE_PERMISSION.get(wanted[0], READ) if wanted and len(wanted) == 1 else READ)
    require_staff(principal, perm)
    query = db.query(PayDocument).filter(PayDocument.company_id == principal.company_id,
                                         PayDocument.id == document_id, staff_clause(principal, perm))
    if wanted:
        query = query.filter(PayDocument.doc_type.in_(wanted))
    if lock_row:
        query = query.with_for_update()
    row = query.one_or_none()
    if row is None:
        raise not_found("Document")
    return row


def get_own(db: Session, principal: Principal, document_id: int, *, doc_types: Iterable[str] | None = None
            ) -> PayDocument:
    """One of the caller's own published documents, or 404 (another employee's id, a draft, unpublished)."""
    query = db.query(PayDocument).filter(PayDocument.company_id == principal.company_id,
                                         PayDocument.id == document_id, own_clause(principal))
    if doc_types:
        query = query.filter(PayDocument.doc_type.in_(tuple(doc_types)))
    row = query.one_or_none()
    if row is None:
        raise not_found("Document")
    return row


def get_visible(db: Session, principal: Principal, document_id: int, *, doc_types: Iterable[str] | None = None
                ) -> tuple[PayDocument, bool]:
    """(document, as_staff): HR scope first, then the caller's own published document; else 404."""
    wanted = tuple(doc_types) if doc_types else None
    row = (db.query(PayDocument)
           .filter(PayDocument.company_id == principal.company_id, PayDocument.id == document_id)
           .one_or_none())
    if row is None or (wanted and row.doc_type not in wanted):
        raise not_found("Document")
    permission = TYPE_PERMISSION.get(row.doc_type, READ)
    if has_staff_scope(principal, permission) and access.target_scope(principal, permission).covers(
            row.legal_entity_id, row.pay_group_id):
        return row, True
    if (principal.employee_id is not None and row.employee_id == principal.employee_id
            and row.status != "GENERATED" and row.doc_type in EMPLOYEE_FACING and not row.draft):
        return row, False
    raise not_found("Document")


# --------------------------------------------------------------------------- serialization


def document_json(doc: PayDocument, *, as_staff: bool = True) -> dict[str, Any]:
    data = {
        "id": doc.id, "doc_type": doc.doc_type, "number": doc.number, "title": doc.title, "version": doc.version,
        "status": doc.status, "draft": doc.draft, "legal_entity_id": doc.legal_entity_id,
        "employee_id": doc.employee_id, "run_id": doc.run_id, "run_employee_id": doc.run_employee_id,
        "period_start": doc.period_start.isoformat() if doc.period_start else None,
        "period_end": doc.period_end.isoformat() if doc.period_end else None,
        "issue_date": doc.issue_date.isoformat(), "published_at": doc.published_at.isoformat() if doc.published_at else None,
        "first_viewed_at": doc.first_viewed_at.isoformat() if doc.first_viewed_at else None,
        "supersedes_document_id": doc.supersedes_document_id, "labels": list(doc.labels or []),
        "languages": list(doc.languages or []), "sha256": doc.sha256, "bytes": doc.bytes, "file_name": doc.file_name,
        "row_version": doc.row_version,
    }
    if as_staff:
        data.update({
            "pay_group_id": doc.pay_group_id, "publish_trigger": doc.publish_trigger,
            "publish_reason": doc.publish_reason, "template_version": doc.template_version,
            "brand_version": doc.brand_version, "renderer": doc.renderer, "rerendered": doc.rerendered,
            "rerender_mismatch": doc.rerender_mismatch, "superseded_at": doc.superseded_at.isoformat()
            if doc.superseded_at else None, "reversed_at": doc.reversed_at.isoformat() if doc.reversed_at else None,
        })
    return data


# --------------------------------------------------------------------------- public verification (EC-DOC-18)

VERIFY_TYPE_LABELS = {
    PAYSLIP: "Payslip", REVERSAL_STATEMENT: "Reversal statement", "PAYMENT_ADVICE": "Payment advice",
    "INVOICE": "Invoice", "SELF_BILLED_INVOICE": "Self-billed invoice", "CREDIT_NOTE": "Credit note",
    "PAYROLL_REGISTER": "Payroll register", "BANK_TRANSFER_REPORT": "Bank transfer report",
    "FUNDING_REQUEST": "Funding request", "FINAL_SETTLEMENT_STATEMENT": "Final settlement statement",
    "SALARY_CERTIFICATE": "Salary certificate", "EMPLOYMENT_LETTER": "Employment letter",
    "INCREMENT_LETTER": "Increment letter", "LOAN_STATEMENT": "Loan statement",
    "REIMBURSEMENT_STATEMENT": "Reimbursement statement",
}
_PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,32}$")


def verify_public(public_id: str, *, host_company_id: Optional[int], ip: Optional[str]) -> dict[str, Any]:
    """What the verification page may show: issuer, type, issue date, number, masked name. Never amounts.

    Looked up through the system session (no tenant session exists on a public page); on a tenant host
    only that tenant's documents verify. Drafts and unknown ids are 404.
    """
    from app.database.db import bind_tenant, system_session

    if not _PUBLIC_ID_RE.match(public_id or ""):
        raise not_found("Document")
    with system_session() as db:
        row = db.query(PayDocument).filter(PayDocument.public_id == public_id).one_or_none()
        if row is None or row.draft or (host_company_id is not None and row.company_id != host_company_id):
            raise not_found("Document")
        entity = db.query(LegalEntity).filter(LegalEntity.company_id == row.company_id,
                                              LegalEntity.id == row.legal_entity_id).one()
        name = None
        if row.employee_id is not None:
            employee = db.query(Employee).filter(Employee.company_id == row.company_id,
                                                 Employee.id == row.employee_id).one_or_none()
            name = masking.mask_name(employee.full_name) if employee is not None else None
        result = {
            "valid": True,
            "issuer": entity.legal_name,
            "document_type": VERIFY_TYPE_LABELS.get(row.doc_type, row.doc_type.replace("_", " ").title()),
            "issue_date": row.issue_date.isoformat(),
            "number": row.number,
            "name": name,
            "status": "SUPERSEDED" if row.status == "SUPERSEDED" else ("REVERSED" if row.status == "REVERSED"
                                                                       else "ISSUED"),
        }
        company_id, document_id = row.company_id, row.id
    from app.database.db import SessionLocal

    db = SessionLocal()
    try:
        bind_tenant(db, company_id)
        doc = db.get(PayDocument, document_id)
        log_access(db, doc, None, "VERIFY", ip, {})
        db.commit()
    finally:
        db.close()
    return result


# --------------------------------------------------------------------------- listing helpers


def employee_names(db: Session, company_id: int, ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    wanted = sorted({i for i in ids if i is not None})
    if not wanted:
        return {}
    rows = db.query(Employee).filter(Employee.company_id == company_id, Employee.id.in_(wanted)).all()
    return {e.id: {"id": e.id, "name": e.full_name, "employee_code": e.employee_code} for e in rows}


def count_and_page(query, skip: int, limit: int):
    total = query.order_by(None).with_entities(func.count(PayDocument.id)).scalar() or 0
    return total, query.offset(skip).limit(limit).all()


__all__ = [
    "EMPLOYEE_FACING", "FILE_HEADERS", "NUMBERING", "PAYSLIP", "PUBLISH", "READ", "REVERSAL_STATEMENT",
    "allocate_number", "canonical_json", "content_hash", "count_and_page", "document_bytes", "document_json",
    "employee_names", "escape_cell", "find_version", "fiscal_year_label", "get_for_staff", "get_own", "get_visible",
    "has_staff_scope", "issue", "jsonable", "latest_version", "lock", "log_access", "new_public_id",
    "numbering_pattern", "own_clause", "render_number", "require_staff", "scope_key", "staff_clause", "store_bytes",
    "transition", "verify_public",
]

# Silence "imported but unused" for helpers re-exported to route modules.
_ = (or_, select)

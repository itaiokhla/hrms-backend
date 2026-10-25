"""Document downloads, the access trail and the download center (3.6 §7.3, §8; EC-DOC-06, EC-DOC-17, EC-DOC-20).

* Signed download links (EC-DOC-06): ``GET payslips/{id}/download`` returns a URL with a token that is
  an HMAC (keyed from the application secret) over the document or export id, the tenant, the user, a
  hash of the session id and an expiry five minutes ahead. Redemption needs the same user in the same
  session before the expiry, and re-checks the caller's access to the object at that moment: an expired
  link is 410, a link used by anyone else, from another session or tampered with is 404. Links are never
  public and never guessable.
* Access trail: every view, download and e-mail is a ``document_access_log`` row (actor, IP, time) and
  the audit log gets DOCUMENT_VIEWED / DOCUMENT_DOWNLOADED. The first time an employee opens their own
  published payslip, the DOCUMENT machine records VIEWED.
* Download center (``export_jobs``): a user's exports only (``download_center`` merges them with the user's
  report exports, which are redeemed through the same signed links, kind "report"); bulk payslip ZIPs and register CSV/XLSX are
  built by job ``documents.export`` with the requester's permissions and scope re-checked in the job.
  ZIPs are written to private storage in chunks of ``CHUNK`` documents and streamed back from disk, so
  10,000 payslips never sit in memory (EC-DOC-20). Every exported PDF carries the overlay "Confidential —
  exported by {user} at {time}", CSV/XLSX files start with the same line, and every cell that a
  spreadsheet could evaluate (=, +, -, @, tab, CR) is escaped (EC-DOC-17). Bank accounts are masked.
  A READY export expires after ``EXPORT_TTL``.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import json
import logging
import re
import secrets
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Optional

from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.core import clock, storage
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.employee import Employee
from app.models.pay_documents import EXPORT_KINDS, ExportJob, PayDocument
from app.models.pay_runs import PayrollLine, PayrollRun, PayrollRunEmployee
from app.payroll import audit
from app.payroll import state_machines as sm
from app.payroll.money import amount_str
from app.payroll.services import access, documents, jobs
from app.services.audit_log_service import AuditLogService

logger = logging.getLogger(__name__)

TOKEN_TTL = timedelta(minutes=5)
EXPORT_TTL = timedelta(hours=24)
EXPORT_JOB = "documents.export"
EXPORT_CATEGORY = "payroll-exports"
CHUNK = 100
EXPORT_PERMISSION = "payroll:reports:export"
REPORT_KIND = "report"  # a report export (app.payroll.services.reports, a ``jobs`` row of type payroll.report_export)
CENTER_SOURCES = ("EXPORT", "REPORT_EXPORT")
CENTER_MAX_WINDOW = 5000
EXPORT_TITLES = {"PAYSLIPS_ZIP": "Payslips (ZIP)", "REGISTER_CSV": "Payroll register (CSV)",
                 "REGISTER_XLSX": "Payroll register (XLSX)"}
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,600}\.[A-Za-z0-9_-]{43}$")
RESULTS_STATES = frozenset((
    "CALCULATED", "REVIEW_REQUIRED", "VALIDATION_FAILED", "APPROVAL_PENDING", "APPROVED", "REJECTED",
    "REOPENED_FOR_CORRECTION", *sm.RUN_FINALIZED_STATES,
))


# --------------------------------------------------------------------------- signed tokens (EC-DOC-06)


def _key() -> bytes:
    return hmac.new(app_settings.secret_key.encode("utf-8"), b"payroll-v2-download-token", hashlib.sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _session_hash(session_id: Optional[str]) -> str:
    return hashlib.sha256(f"session:{session_id or '-'}".encode("utf-8")).hexdigest()[:24]


def make_token(principal: Principal, kind: str, object_id: int, *, now: Optional[datetime] = None) -> tuple[str, datetime]:
    """A token for one object, one user and one session, valid for five minutes."""
    now = now or clock.utcnow()
    expires = now + TOKEN_TTL
    claims = {"v": 1, "k": kind, "i": int(object_id), "c": principal.company_id, "u": principal.user_id,
              "s": _session_hash(principal.session_id), "e": int(expires.timestamp()), "n": secrets.token_hex(6)}
    body = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _b64(hmac.new(_key(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}", expires


def read_token(principal: Principal, token: str, *, now: Optional[datetime] = None) -> tuple[str, int]:
    """(kind, object id) of a valid token of this user and session; 410 when expired, otherwise 404."""
    if not _TOKEN_RE.match(token or ""):
        raise not_found("Download")
    body, signature = token.split(".", 1)
    expected = _b64(hmac.new(_key(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, signature):
        raise not_found("Download")
    try:
        claims = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        raise not_found("Download")
    if (claims.get("v") != 1 or claims.get("c") != principal.company_id or claims.get("u") != principal.user_id
            or not hmac.compare_digest(str(claims.get("s")), _session_hash(principal.session_id))):
        raise not_found("Download")
    now = now or clock.utcnow()
    if int(claims.get("e", 0)) < int(now.timestamp()):
        raise AppError(410, "This download link has expired. Request a new one.", "DOWNLOAD_LINK_EXPIRED")
    kind, object_id = claims.get("k"), claims.get("i")
    if kind not in ("doc", "export", REPORT_KIND) or not isinstance(object_id, int):
        raise not_found("Download")
    return kind, object_id


def download_link(principal: Principal, kind: str, object_id: int) -> dict:
    token, expires = make_token(principal, kind, object_id)
    return {"url": f"/api/v1/payroll/downloads/{token}", "expires_at": expires.isoformat(),
            "expires_in_seconds": int(TOKEN_TTL.total_seconds())}


# --------------------------------------------------------------------------- views and downloads


def _actor(principal: Optional[Principal]) -> Optional[int]:
    return principal.user_id if principal is not None else None


def record_view(db: Session, principal: Principal, doc: PayDocument, *, as_staff: bool, ip: Optional[str]) -> PayDocument:
    """Log a view; the employee's first view of their own published document records VIEWED."""
    documents.log_access(db, doc, principal.user_id, "VIEW", ip, {"as_staff": as_staff})
    if not as_staff and doc.employee_id == principal.employee_id and doc.status in (
            "PUBLISHED", "EMAIL_SENT", "EMAIL_FAILED"):
        doc = documents.transition(db, None, doc, "RECORD_VIEW", details={"user_id": principal.user_id})
    else:
        AuditLogService.log(db, principal, audit.PAYSLIPS, "DOCUMENT_VIEWED",
                            f"Document {doc.number or doc.id} viewed", "document", doc.id,
                            details={"as_staff": as_staff})
    db.flush()
    return doc


def issue_download(db: Session, principal: Principal, doc: PayDocument) -> dict:
    """The signed link of a document the caller may already read (checked by the caller)."""
    return download_link(principal, "doc", doc.id)


@dataclass
class Served:
    data: Optional[bytes]
    path: Optional[Path]
    file_name: str
    mime: str
    headers: dict


def redeem(db: Session, principal: Principal, token: str, ip: Optional[str]) -> Served:
    """Serve a signed link: same user, same session, not expired, and access re-checked now."""
    kind, object_id = read_token(principal, token)
    if kind == "doc":
        doc, as_staff = documents.get_visible(db, principal, object_id)
        data, rerendered = documents.document_bytes(db, principal, doc)
        documents.log_access(db, doc, principal.user_id, "DOWNLOAD", ip, {"as_staff": as_staff,
                                                                          "rerendered": rerendered})
        AuditLogService.log(db, principal, audit.PAYSLIPS, "DOCUMENT_DOWNLOADED",
                            f"Document {doc.number or doc.id} downloaded", "document", doc.id,
                            details={"as_staff": as_staff, "sha256": doc.sha256, "rerendered": rerendered})
        if not as_staff and doc.status in ("PUBLISHED", "EMAIL_SENT", "EMAIL_FAILED"):
            documents.transition(db, None, doc, "RECORD_VIEW", details={"user_id": principal.user_id})
        headers = {**documents.FILE_HEADERS, "X-Document-Number": doc.number or "", "X-Document-SHA256": doc.sha256,
                   "X-Brand-Version": doc.brand_version or "", "X-Rerendered": "true" if rerendered else "false"}
        return Served(data, None, doc.file_name, doc.mime, headers)
    if kind == REPORT_KIND:
        from app.payroll.services import reports

        # Permission, ownership, readiness and the stored hash are re-checked and the download audited there.
        data, file_name, media_type = reports.export_file(db, principal, object_id)
        return Served(data, None, file_name, media_type, dict(documents.FILE_HEADERS))
    job = own_export(db, principal, object_id)
    recheck_export_access(db, principal, job)
    if job.status == "EXPIRED" or (job.expires_at is not None and job.expires_at <= clock.utcnow()):
        _expire(job)
        raise AppError(410, "This export has expired. Request it again.", "EXPORT_EXPIRED")
    if job.status != "READY":
        raise conflict("This export is not ready yet.", "EXPORT_NOT_READY")
    path = storage.resolve(job.company_id, job.storage_path)
    _log_export(db, job, principal, "DOWNLOAD", ip)
    AuditLogService.log(db, principal, audit.PAYSLIPS, "EXPORT_DOWNLOADED", f"Export {job.file_name} downloaded",
                        "export_job", job.id, details={"kind": job.kind, "sha256": job.sha256})
    return Served(None, path, job.file_name, job.mime or "application/octet-stream", dict(documents.FILE_HEADERS))


def _log_export(db: Session, job: ExportJob, principal: Optional[Principal], action: str, ip: Optional[str],
                details: Optional[Mapping[str, Any]] = None) -> None:
    from app.models.pay_documents import DocumentAccessLog

    db.add(DocumentAccessLog(company_id=job.company_id, document_id=None, export_job_id=job.id,
                             user_id=_actor(principal), action=action, ip=(ip or None) and ip[:64], at=clock.utcnow(),
                             details=documents.jsonable(dict(details or {"kind": job.kind}))))
    db.flush()


# --------------------------------------------------------------------------- download center


def export_json(job: ExportJob) -> dict:
    return {
        "id": job.id, "kind": job.kind, "params": dict(job.params or {}), "status": job.status,
        "item_count": job.item_count, "file_name": job.file_name, "bytes": job.bytes, "sha256": job.sha256,
        "watermark": job.watermark, "error": job.error, "job_id": job.job_id,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "ready_at": job.ready_at.isoformat() if job.ready_at else None,
        "expires_at": job.expires_at.isoformat() if job.expires_at else None, "row_version": job.row_version,
    }


def own_export(db: Session, principal: Principal, export_id: int, *, lock: bool = False) -> ExportJob:
    """Only the requester ever reaches an export (any other id is 404)."""
    query = db.query(ExportJob).filter(ExportJob.company_id == principal.company_id, ExportJob.id == export_id,
                                       ExportJob.requested_by_user_id == principal.user_id)
    if lock:
        query = query.with_for_update()
    job = query.one_or_none()
    if job is None:
        raise not_found("Export")
    return job


def _expire(job: ExportJob) -> None:
    if job.status == "READY":
        storage.delete(job.company_id, job.storage_path)
        job.status = "EXPIRED"
        job.row_version += 1


def list_exports(db: Session, principal: Principal, *, skip: int = 0, limit: int = 50) -> dict:
    query = db.query(ExportJob).filter(ExportJob.company_id == principal.company_id,
                                       ExportJob.requested_by_user_id == principal.user_id)
    now = clock.utcnow()
    for job in query.filter(ExportJob.status == "READY", ExportJob.expires_at <= now).with_for_update().all():
        _expire(job)
    db.flush()
    total = query.count()
    items = query.order_by(ExportJob.id.desc()).offset(skip).limit(limit).all()
    return {"total": total, "items": [export_json(j) for j in items]}


def _center_export_item(principal: Principal, job: ExportJob) -> dict:
    run_id = (job.params or {}).get("run_id")
    return {
        "source": "EXPORT", "id": job.id, "kind": job.kind, "title": EXPORT_TITLES.get(job.kind, job.kind),
        "format": {"PAYSLIPS_ZIP": "ZIP", "REGISTER_CSV": "CSV", "REGISTER_XLSX": "XLSX"}.get(job.kind),
        "run_id": run_id, "report_code": None, "status": job.status, "file_name": job.file_name,
        "bytes": job.bytes, "items": job.item_count, "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "expires_at": job.expires_at.isoformat() if job.expires_at else None,
        "link_endpoint": f"/api/v1/payroll/exports/{job.id}/download",
        "download": (download_link(principal, "export", job.id)
                     if job.status == "READY" and documents.has_staff_scope(principal, EXPORT_PERMISSION) else None),
    }


def _center_report_item(principal: Principal, job, now: datetime) -> dict:
    from app.payroll.services import reports

    data = reports.export_json(job, now)
    report = reports.BY_CODE.get(data.get("report_code") or "")
    # A link only while the caller may still download it (permissions and scope re-checked, EC-DOC-06).
    ready = data["status"] == "READY" and reports.can_download(principal, job)
    created = data.get("created_at")
    return {
        "source": "REPORT_EXPORT", "id": job.id, "kind": "REPORT",
        "title": f"{report.name if report is not None else data.get('report_code')} ({data.get('format')})",
        "format": data.get("format"), "run_id": None, "report_code": data.get("report_code"),
        "status": data["status"], "file_name": data.get("file_name"), "bytes": data.get("bytes"),
        "items": data.get("rows"), "error": data.get("error"),
        "created_at": created.isoformat() if isinstance(created, datetime) else created,
        "expires_at": data.get("expires_at"),
        "link_endpoint": f"/api/v1/payroll/report-exports/{job.id}/link",
        "download": download_link(principal, REPORT_KIND, job.id) if ready else None,
    }


def download_center(db: Session, principal: Principal, *, source: Optional[str] = None, skip: int = 0,
                    limit: int = 50) -> dict:
    """The caller's own downloads in one list, newest first: payslip ZIP / register exports (``export_jobs``)
    and report exports (``jobs`` of type payroll.report_export), with one skip/limit paging model and a
    signed five-minute link for every READY file (EC-DOC-06). Only the requester ever sees an item."""
    from app.models.jobs import Job
    from app.payroll.services import reports

    if source is not None and source not in CENTER_SOURCES:
        raise bad_request("Unknown download source.", "SOURCE_INVALID")
    if skip + limit > CENTER_MAX_WINDOW:
        raise bad_request(f"Page through at most {CENTER_MAX_WINDOW} downloads.", "PAGE_TOO_DEEP")
    now = clock.utcnow()
    window = skip + limit
    entries: list[tuple[datetime, int, str, Any]] = []
    total = 0
    if source in (None, "EXPORT"):
        exports = db.query(ExportJob).filter(ExportJob.company_id == principal.company_id,
                                             ExportJob.requested_by_user_id == principal.user_id)
        for job in exports.filter(ExportJob.status == "READY", ExportJob.expires_at <= now).with_for_update().all():
            _expire(job)
        db.flush()
        total += exports.count()
        entries += [(j.created_at, j.id, "EXPORT", j) for j in
                    exports.order_by(ExportJob.created_at.desc(), ExportJob.id.desc()).limit(window).all()]
    if source in (None, "REPORT_EXPORT"):
        jobs_query = db.query(Job).filter(Job.company_id == principal.company_id, Job.type == reports.EXPORT_JOB,
                                          Job.created_by_user_id == principal.user_id)
        total += jobs_query.count()
        entries += [(j.created_at, j.id, "REPORT_EXPORT", j) for j in
                    jobs_query.order_by(Job.created_at.desc(), Job.id.desc()).limit(window).all()]
    entries.sort(key=lambda e: (e[0], e[2] == "EXPORT", e[1]), reverse=True)
    items = [_center_export_item(principal, row) if kind == "EXPORT" else _center_report_item(principal, row, now)
             for _, _, kind, row in entries[skip:skip + limit]]
    return {"total": total, "skip": skip, "limit": limit, "items": items}


def _run_for_export(db: Session, principal: Principal, run_id: Any, permission: str) -> PayrollRun:
    if not isinstance(run_id, int) or isinstance(run_id, bool):
        raise bad_request("params.run_id is required.", "EXPORT_PARAMS_INVALID")
    documents.require_staff(principal, permission)
    return access.get_in_scope(db, principal, permission, PayrollRun, run_id, "Payroll run")


def check_export(db: Session, principal: Principal, kind: str, params: Mapping[str, Any]) -> PayrollRun:
    """Permission and scope of an export (403 without the permissions, 404 outside scope)."""
    if kind not in EXPORT_KINDS:
        raise bad_request("Unknown export kind.", "EXPORT_KIND_INVALID")
    unknown = set(params) - {"run_id"}
    if unknown:
        raise bad_request(f"Unknown export parameters: {', '.join(sorted(unknown))}.", "EXPORT_PARAMS_INVALID")
    documents.require_staff(principal, EXPORT_PERMISSION)
    if kind == "PAYSLIPS_ZIP":
        documents.require_staff(principal, documents.READ)
        run = _run_for_export(db, principal, params.get("run_id"), documents.READ)
        access.ensure_run_access(principal, EXPORT_PERMISSION, run)
        if not sm.is_run_finalized(run.status):
            raise conflict("Payslips exist for finalized runs only.", "RUN_NOT_FINALIZED")
        return run
    run = _run_for_export(db, principal, params.get("run_id"), EXPORT_PERMISSION)
    if run.status not in RESULTS_STATES:
        raise conflict("The run has no calculation results yet.", "RUN_NOT_CALCULATED")
    return run


def recheck_export_access(db: Session, principal: Principal, job: ExportJob) -> None:
    """The requester's permission and scope over the export's run, again at download time (EC-DOC-06): a user
    whose payroll:reports:export (or, for payslip ZIPs, payroll:payslip:read) was revoked gets 403, one whose
    scope no longer covers the run gets 404. The run's state is not re-checked: the file was built from it."""
    run_id = (job.params or {}).get("run_id")
    documents.require_staff(principal, EXPORT_PERMISSION)
    if job.kind == "PAYSLIPS_ZIP":
        documents.require_staff(principal, documents.READ)
        run = _run_for_export(db, principal, run_id, documents.READ)
        access.ensure_run_access(principal, EXPORT_PERMISSION, run)
    else:
        _run_for_export(db, principal, run_id, EXPORT_PERMISSION)


def request_export(db: Session, principal: Principal, kind: str, params: Mapping[str, Any]) -> ExportJob:
    run = check_export(db, principal, kind, params)
    job = ExportJob(company_id=principal.company_id, requested_by_user_id=principal.user_id, kind=kind,
                    params={"run_id": run.id}, status="QUEUED", row_version=1)
    db.add(job)
    db.flush()
    queued = jobs.enqueue(db, company_id=principal.company_id, type=EXPORT_JOB, payload={"export_job_id": job.id},
                          idempotency_key=f"export:{job.id}", created_by_user_id=principal.user_id)
    job.job_id = queued.id
    AuditLogService.log(db, principal, audit.PAYSLIPS, "EXPORT_REQUESTED", f"{kind} export of run {run.number} requested",
                        "export_job", job.id, details={"kind": kind, "run_id": run.id})
    db.flush()
    return job


def watermark_text(user_name: str, at: datetime) -> str:
    return f"Confidential — exported by {user_name} at {at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"


def stamp_pdf(data: bytes, text: str) -> bytes:
    """The PDF with ``text`` printed at the top of every page (stored bytes are never changed)."""
    try:
        import pymupdf  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return b""
    from app.utils.pdf_fonts import FONT_DIR

    font = FONT_DIR / ("NotoSansArabic-Regular.ttf" if re.search(r"[؀-ۿ]", text) else "NotoSans-Regular.ttf")
    document = pymupdf.open(stream=data, filetype="pdf")
    try:
        for page in document:
            page.insert_text((36, 14), text, fontsize=7, fontname="wm", fontfile=str(font), color=(0.6, 0, 0))
        try:
            document.subset_fonts()  # embed only the watermark's glyphs, not the whole font per payslip
        except Exception:  # noqa: BLE001 - an older PyMuPDF without subsetting still stamps correctly
            pass
        return document.tobytes(deflate=True, garbage=3)
    finally:
        document.close()


def _export_path(company_id: int, ext: str) -> tuple[str, Path]:
    relative = f"{int(company_id)}/{EXPORT_CATEGORY}/{uuid.uuid4().hex}.{ext}"
    path = Path(app_settings.upload_dir).resolve() / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    return relative, path


def _zip_name(doc: PayDocument, codes: Mapping[int, str]) -> str:
    stem = f"{codes.get(doc.employee_id, 'employee')}-{doc.number or doc.id}-v{doc.version}"
    return documents.safe_file_name(stem)


def build_payslip_zip(ctx_progress, db: Session, principal: Principal, job: ExportJob, run: PayrollRun,
                      watermark: str) -> tuple[str, Path, int]:
    """Write every in-scope payslip of the run into a ZIP on disk, CHUNK documents at a time (EC-DOC-20)."""
    from app.payroll.services import pdf_render

    relative, path = _export_path(job.company_id, "zip")
    base = (db.query(PayDocument.id)
            .filter(PayDocument.company_id == job.company_id, PayDocument.run_id == run.id,
                    PayDocument.doc_type == documents.PAYSLIP, PayDocument.retention_redacted_at.is_(None),
                    documents.staff_clause(principal, documents.READ))
            .order_by(PayDocument.id))
    ids = [i for (i,) in base.all()]
    count = 0
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", f"{watermark}\nPayslips of run {run.number}.\n")
        for start in range(0, len(ids), CHUNK):
            chunk = (db.query(PayDocument).filter(PayDocument.company_id == job.company_id,
                                                  PayDocument.id.in_(ids[start:start + CHUNK]))
                     .order_by(PayDocument.id).all())
            codes = {e.id: e.employee_code for e in db.query(Employee).filter(
                Employee.company_id == job.company_id, Employee.id.in_({d.employee_id for d in chunk}))}
            for doc in chunk:
                data, _ = documents.document_bytes(db, None, doc)
                stamped = stamp_pdf(data, watermark) or pdf_render.render_with_brand(doc.content, overlay=watermark)[0]
                archive.writestr(_zip_name(doc, codes), stamped)
                count += 1
            for doc in chunk:  # keep the identity map small while thousands of payslips stream through
                db.expunge(doc)
            ctx_progress(min(start + CHUNK, len(ids)), len(ids))
    return relative, path, count


def register_rows(db: Session, run: PayrollRun) -> tuple[list[str], list[list[str]]]:
    """Header and rows of the payroll register (every employee × component, totals, masked bank)."""
    rows = (db.query(PayrollRunEmployee)
            .filter(PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id,
                    PayrollRunEmployee.status.in_(("CALCULATED", "ON_HOLD")))
            .order_by(PayrollRunEmployee.id).all())
    lines = (db.query(PayrollLine).filter(PayrollLine.company_id == run.company_id, PayrollLine.run_id == run.id)
             .order_by(PayrollLine.sort_order, PayrollLine.id).all())
    order: list[tuple[str, str]] = []
    amounts: dict[tuple[int, str, str], Decimal] = {}
    for line in lines:
        key = (line.kind, line.component_code)
        if key not in order:
            order.append(key)
        slot = (line.run_employee_id, line.kind, line.component_code)
        amounts[slot] = amounts.get(slot, Decimal(0)) + Decimal(line.amount)
    employees = {e.id: e for e in db.query(Employee).filter(Employee.company_id == run.company_id,
                                                           Employee.id.in_({r.employee_id for r in rows}))}
    header = ["Employee code", "Employee name", "Status", "Currency",
              *[f"{kind.title().replace('_', ' ')}: {code}" for kind, code in order],
              "Gross", "Deductions", "Reimbursements", "Net", "Employer cost"]
    body = []
    for row in rows:
        employee = employees.get(row.employee_id)
        cells = [employee.employee_code if employee else "", employee.full_name if employee else "", row.status,
                 row.currency]
        for kind, code in order:
            value = amounts.get((row.id, kind, code))
            cells.append(amount_str(value, row.currency) if value is not None else "")
        for total in (row.gross, row.deductions, row.reimbursements, row.net, row.employer_cost):
            cells.append(amount_str(Decimal(total), row.currency) if total is not None else "")
        body.append(cells)
    return header, body


def build_register(db: Session, job: ExportJob, run: PayrollRun, watermark: str, kind: str) -> tuple[str, Path, int]:
    header, body = register_rows(db, run)
    status_line = "FINAL" if sm.is_run_finalized(run.status) else "DRAFT — not final"
    preamble = [[watermark], [f"Payroll register — run {run.number} — {run.label}", status_line]]
    if kind == "REGISTER_CSV":
        relative, path = _export_path(job.company_id, "csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            for row in [*preamble, header, *body]:
                writer.writerow([documents.escape_cell(cell) for cell in row])
        return relative, path, len(body)
    from openpyxl import Workbook

    relative, path = _export_path(job.company_id, "xlsx")
    book = Workbook(write_only=True)
    sheet = book.create_sheet("Register")
    for row in [*preamble, header, *body]:
        # Money stays text (exact decimal strings, never floats); formulas can never start a cell (EC-DOC-17).
        sheet.append([documents.escape_cell(cell) for cell in row])
    book.save(str(path))
    return relative, path, len(body)


def generate_export(db: Session, export_id: int, progress=lambda done, total=None: None) -> dict:
    """The job body: rebuild the requester's principal, re-check scope, write the file, mark READY or FAILED."""
    from app.core.principal import build_principal
    from app.models.company import Company
    from app.models.user import User

    job = db.query(ExportJob).filter(ExportJob.id == export_id).with_for_update().one_or_none()
    if job is None or job.status not in ("QUEUED", "GENERATING"):
        return {"skipped": True}
    job.status = "GENERATING"
    job.row_version += 1
    db.flush()
    user = db.get(User, job.requested_by_user_id)
    try:
        if user is None or not user.is_active:
            raise forbidden("The requester can no longer export.")
        principal = build_principal(db, user, None, db.get(Company, job.company_id))
        run = check_export(db, principal, job.kind, dict(job.params or {}))
        now = clock.utcnow()
        watermark = watermark_text(user.full_name or user.email, now)
        if job.kind == "PAYSLIPS_ZIP":
            relative, path, count = build_payslip_zip(progress, db, principal, job, run, watermark)
            mime, name = "application/zip", f"payslips-{run.number}.zip"
        else:
            relative, path, count = build_register(db, job, run, watermark, job.kind)
            ext = "csv" if job.kind == "REGISTER_CSV" else "xlsx"
            mime = "text/csv" if ext == "csv" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            name = f"payroll-register-{run.number}.{ext}"
    except AppError as exc:
        job.status, job.error = "FAILED", str(exc.detail)[:500]
        job.row_version += 1
        db.flush()
        return {"status": "FAILED"}
    except Exception as exc:  # noqa: BLE001 - recorded on the export; the user can request it again
        logger.exception("Export %s failed", job.id)
        job.status, job.error = "FAILED", f"{type(exc).__name__}"[:500]
        job.row_version += 1
        db.flush()
        return {"status": "FAILED"}
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    job.status, job.storage_path, job.sha256 = "READY", relative, digest.hexdigest()
    job.bytes, job.mime, job.file_name, job.item_count = path.stat().st_size, mime, name, count
    job.watermark, job.ready_at, job.expires_at = watermark, clock.utcnow(), clock.utcnow() + EXPORT_TTL
    job.error = None
    job.row_version += 1
    _log_export(db, job, principal, "EXPORT", None, {"kind": job.kind, "items": count, "run_id": run.id})
    AuditLogService.log(db, principal, audit.PAYSLIPS, "EXPORT_GENERATED", f"{job.kind} export ready ({count} items)",
                        "export_job", job.id, company_id=job.company_id,
                        details={"kind": job.kind, "items": count, "sha256": job.sha256})
    db.flush()
    return {"status": "READY", "items": count}


@jobs.register(EXPORT_JOB)
def _export(ctx: jobs.JobContext) -> dict:
    export_id = ctx.payload.get("export_job_id")
    if not isinstance(export_id, int):
        raise jobs.PermanentJobError("an export job needs export_job_id")
    return generate_export(ctx.db, export_id, ctx.progress)


__all__ = [
    "CHUNK", "EXPORT_JOB", "EXPORT_TTL", "REPORT_KIND", "Served", "TOKEN_TTL", "build_register", "check_export",
    "recheck_export_access",
    "download_center", "download_link",
    "export_json", "generate_export", "issue_download", "list_exports", "make_token", "own_export", "read_token",
    "record_view", "redeem", "register_rows", "request_export", "stamp_pdf", "watermark_text",
]

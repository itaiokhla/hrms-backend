"""Discussion, attachments and document requests of leave requests (SPINE §5.7-§5.8; 03-leave/05).

Who sees what (07 §3):
* messages — the requester, the approvers who can act on the request and holders of ``leave:read`` covering the
  employee; INTERNAL_NOTE (visibility APPROVERS_HR) is never shown to the requester;
* attachments — the uploader / requester; approvers acting on the request (GENERAL only, unless they hold
  ``leave:view_medical``); ``leave:view_documents`` holders in scope for GENERAL, ``leave:view_medical`` for MEDICAL.
  Every view or download is written to ``leave_attachment_access_log``. Files are sniffed by content (PDF, PNG, JPEG),
  limited by ``leave.documents.max_file_mb`` / ``max_files``, stored in the tenant folder and never served by path.
* verification — ``leave:verify_documents`` (MEDICAL also needs ``leave:view_medical``); document requests are created
  by the plan's rule at submit or by a verifier.
"""

import hashlib
import re
from datetime import date
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from app.core import authz, storage
from app.core.errors import AppError, bad_request, forbidden, not_found
from app.core.principal import Principal
from app.core.security import utcnow
from app.models.employee import Employee
from app.models.leave import Leave
from app.models.leave_v2 import (
    ATTACHMENT_CATEGORIES,
    LeaveAttachment,
    LeaveAttachmentAccess,
    LeaveDocumentRequest,
    LeaveMessage,
)
from app.models.user import User
from app.services import leave_access
from app.services.audit_log_service import AuditLogService
from app.services.notification_service import NotificationService

CATEGORY = "leave-documents"
FILE_TYPES = ((b"%PDF-", "pdf", "application/pdf"), (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
              (b"\xff\xd8\xff", "jpg", "image/jpeg"))
MEDICAL_CATEGORIES = ("MEDICAL_CERTIFICATE", "FIT_TO_WORK")
_NAME_RE = re.compile(r"[^A-Za-z0-9._ -]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sniff(data: bytes) -> Tuple[str, str]:
    for magic, ext, mime in FILE_TYPES:
        if data.startswith(magic):
            return ext, mime
    raise bad_request("Documents must be PDF, PNG or JPEG files.", "DOCUMENT_TYPE")


def _leave(db: Session, principal: Principal, leave_id: int) -> Leave:
    leave = db.query(Leave).filter(Leave.company_id == principal.company_id, Leave.id == leave_id).first()
    if leave is None:
        raise not_found("Leave request")
    return leave


def _acting_approver(db: Session, principal: Principal, leave: Leave) -> bool:
    from app.services import leave_service

    if leave.employee_id == principal.employee_id:
        return False
    return leave_service.can_act(db, principal, leave, db.get(Employee, leave.employee_id)) is not None


def can_see_request(db: Session, principal: Principal, leave: Leave) -> bool:
    return (leave.employee_id == principal.employee_id or leave.requested_by_user_id == principal.user_id
            or _acting_approver(db, principal, leave)
            or authz.can_access_employee(db, principal, "leave:read", leave.employee_id))


def _staff(db: Session, principal: Principal, leave: Leave) -> bool:
    """Approvers and HR (sees internal notes)."""
    if leave.employee_id == principal.employee_id:
        return False
    if _acting_approver(db, principal, leave):
        return True
    return any(principal.has(code) and authz.can_access_employee(db, principal, code, leave.employee_id)
               for code in ("leave:approve", "leave:read"))


# --------------------------------------------------------------------------- messages

def messages(db: Session, principal: Principal, leave_id: int) -> List[dict]:
    leave = _leave(db, principal, leave_id)
    if not can_see_request(db, principal, leave):
        raise not_found("Leave request")
    staff = _staff(db, principal, leave)
    query = db.query(LeaveMessage).filter(LeaveMessage.company_id == leave.company_id, LeaveMessage.leave_id == leave.id)
    if not staff:
        query = query.filter(LeaveMessage.visibility == "PARTICIPANTS")
    rows = query.order_by(LeaveMessage.id).all()
    users = {u.id: u.full_name for u in db.query(User).filter(User.company_id == leave.company_id, User.id.in_(
        {r.author_user_id for r in rows if r.author_user_id} or {0}))}
    return [{"id": r.id, "kind": r.kind, "visibility": r.visibility, "body": r.body, "reason_code": r.reason_code,
             "author": users.get(r.author_user_id) if r.author_user_id else "System",
             "author_user_id": r.author_user_id, "mine": r.author_user_id == principal.user_id,
             "created_at": r.created_at.isoformat() if r.created_at else None,
             "details": r.details if staff else {k: v for k, v in (r.details or {}).items() if k != "internal"}}
            for r in rows]


def post_message(db: Session, principal: Principal, leave_id: int, body: str, *, internal: bool = False) -> dict:
    if leave_access.setting(db, principal.company_id, "leave.discussion.enabled", default=True) is False:
        raise AppError(409, "Discussions on leave requests are turned off.", "DISCUSSION_DISABLED")
    leave = _leave(db, principal, leave_id)
    if not can_see_request(db, principal, leave):
        raise not_found("Leave request")
    text = _CONTROL_RE.sub("", (body or "")).strip()
    if not 1 <= len(text) <= 4000:
        raise bad_request("Write a message of 1-4000 characters.", "INVALID_MESSAGE")
    if internal and not _staff(db, principal, leave):
        raise forbidden("Only approvers and HR can write internal notes.")
    row = LeaveMessage(company_id=leave.company_id, leave_id=leave.id, author_user_id=principal.user_id,
                       kind="INTERNAL_NOTE" if internal else "COMMENT",
                       visibility="APPROVERS_HR" if internal else "PARTICIPANTS", body=text)
    db.add(row)
    db.flush()
    requester = db.get(Employee, leave.employee_id)
    if not internal and requester.user_id and requester.user_id != principal.user_id:
        NotificationService.create(db, requester.user_id, "New comment on your leave request",
                                   f"{principal.full_name} commented on your leave request.", "leave",
                                   f"/leaves/requests?modal=leave&id={leave.id}")
    return {"id": row.id, "kind": row.kind, "visibility": row.visibility, "body": row.body,
            "author": principal.full_name, "mine": True, "created_at": row.created_at.isoformat()}


# --------------------------------------------------------------------------- attachments

def _can_view_attachment(db: Session, principal: Principal, row: LeaveAttachment, leave: Optional[Leave]) -> bool:
    if row.employee_id == principal.employee_id or row.uploaded_by_user_id == principal.user_id:
        return True
    medical = row.sensitivity == "MEDICAL"
    if medical:
        return principal.has("leave:view_medical") and authz.can_access_employee(
            db, principal, "leave:view_medical", row.employee_id)
    if leave is not None and _acting_approver(db, principal, leave) and leave_access.setting(
            db, principal.company_id, "leave.documents.approvers_see_general", default=True) is not False:
        return True
    for code in ("leave:view_documents", "leave:verify_documents"):
        if principal.has(code) and authz.can_access_employee(db, principal, code, row.employee_id):
            return True
    return False


def attachment_json(row: LeaveAttachment) -> dict:
    return {"id": row.id, "leave_id": row.leave_id, "category": row.category, "sensitivity": row.sensitivity,
            "original_name": row.original_name, "mime": row.mime, "bytes": row.bytes, "scan_status": row.scan_status,
            "verification_status": row.verification_status, "rejection_reason": row.rejection_reason,
            "verified_at": row.verified_at.isoformat() if row.verified_at else None,
            "created_at": row.created_at.isoformat() if row.created_at else None}


def upload(db: Session, principal: Principal, *, file_name: str, data: bytes, category: str,
           leave_id: Optional[int] = None, employee_id: Optional[int] = None) -> dict:
    if category not in ATTACHMENT_CATEGORIES:
        raise bad_request("Unknown document category.", "UNKNOWN_REFERENCE")
    cid = principal.company_id
    leave = _leave(db, principal, leave_id) if leave_id else None
    subject_id = leave.employee_id if leave is not None else (employee_id or principal.employee_id)
    if subject_id is None:
        raise forbidden()
    if subject_id != principal.employee_id:
        allowed = any(principal.has(c) and authz.can_access_employee(db, principal, c, subject_id)
                      for c in ("leave:apply_on_behalf", "leave:record", "leave:verify_documents"))
        if not allowed:
            raise not_found("Leave request" if leave else "Employee")
    if leave is not None and leave.status in ("rejected", "cancelled", "withdrawn"):
        raise AppError(409, "This request is closed.", "INVALID_TRANSITION")
    max_mb = int(leave_access.setting(db, cid, "leave.documents.max_file_mb", default=10) or 10)
    max_files = int(leave_access.setting(db, cid, "leave.documents.max_files", default=5) or 5)
    if not data:
        raise bad_request("The file is empty.", "DOCUMENT_EMPTY")
    if len(data) > max_mb * 1024 * 1024:
        raise AppError(413, f"Files are at most {max_mb} MB.", "DOCUMENT_TOO_LARGE")
    if leave is not None:
        count = db.query(LeaveAttachment.id).filter(LeaveAttachment.company_id == cid, LeaveAttachment.leave_id == leave.id,
                                                    LeaveAttachment.deleted_at.is_(None)).count()
        if count >= max_files:
            raise AppError(409, f"A request has at most {max_files} documents.", "TOO_MANY_DOCUMENTS")
    ext, mime = _sniff(data)
    path = storage.save_bytes(cid, CATEGORY, data, ext)
    name = _NAME_RE.sub("_", (file_name or f"document.{ext}").strip())[:200] or f"document.{ext}"
    row = LeaveAttachment(company_id=cid, employee_id=subject_id, leave_id=leave.id if leave else None,
                          category=category, sensitivity="MEDICAL" if category in MEDICAL_CATEGORIES else "GENERAL",
                          storage_path=path, sha256=hashlib.sha256(data).hexdigest(), mime=mime, bytes=len(data),
                          original_name=name, scan_status="NOT_SCANNED", verification_status="SUBMITTED",
                          uploaded_by_user_id=principal.user_id)
    db.add(row)
    db.flush()
    if leave is not None:
        for request in db.query(LeaveDocumentRequest).filter(LeaveDocumentRequest.company_id == cid,
                                                             LeaveDocumentRequest.leave_id == leave.id,
                                                             LeaveDocumentRequest.category == category,
                                                             LeaveDocumentRequest.status.in_(("OPEN", "OVERDUE"))):
            request.status, request.fulfilled_attachment_id, request.closed_at = "FULFILLED", row.id, utcnow()
        db.add(LeaveMessage(company_id=cid, leave_id=leave.id, kind="SYSTEM_EVENT",
                            body=f"Document added: {category.replace('_', ' ').lower()}."))
    AuditLogService.log(db, principal, "Leave", "ATTACHMENT_UPLOADED", f"Uploaded a leave document ({category})",
                        "leave_attachment", row.id)
    return attachment_json(row)


def list_attachments(db: Session, principal: Principal, leave_id: int) -> List[dict]:
    leave = _leave(db, principal, leave_id)
    if not can_see_request(db, principal, leave):
        raise not_found("Leave request")
    rows = db.query(LeaveAttachment).filter(LeaveAttachment.company_id == leave.company_id,
                                            LeaveAttachment.leave_id == leave.id,
                                            LeaveAttachment.deleted_at.is_(None)).order_by(LeaveAttachment.id).all()
    out = []
    for row in rows:
        visible = _can_view_attachment(db, principal, row, leave)
        item = attachment_json(row) | {"can_view": visible,
                                       "can_verify": _can_verify(db, principal, row) and row.employee_id != principal.employee_id}
        if not visible:
            item["original_name"] = "Medical document" if row.sensitivity == "MEDICAL" else "Document"
        out.append(item)
    return out


def open_attachment(db: Session, principal: Principal, attachment_id: int, *, ip: Optional[str] = None):
    row = db.query(LeaveAttachment).filter(LeaveAttachment.company_id == principal.company_id,
                                           LeaveAttachment.id == attachment_id,
                                           LeaveAttachment.deleted_at.is_(None)).first()
    if row is None:
        raise not_found("Document")
    leave = db.get(Leave, row.leave_id) if row.leave_id else None
    if not _can_view_attachment(db, principal, row, leave):
        raise not_found("Document")
    if row.scan_status == "INFECTED":
        raise AppError(409, "This file failed the virus scan.", "DOCUMENT_QUARANTINED")
    db.add(LeaveAttachmentAccess(company_id=row.company_id, attachment_id=row.id, user_id=principal.user_id,
                                 action="DOWNLOAD", ip_address=(ip or "")[:64] or None))
    db.flush()
    return storage.resolve(row.company_id, row.storage_path), row


def _can_verify(db: Session, principal: Principal, row: LeaveAttachment) -> bool:
    if not (principal.has("leave:verify_documents") and authz.can_access_employee(
            db, principal, "leave:verify_documents", row.employee_id)):
        return False
    return row.sensitivity != "MEDICAL" or principal.has("leave:view_medical")


def verify(db: Session, principal: Principal, attachment_id: int, *, approve: bool, reason: Optional[str]) -> dict:
    row = db.query(LeaveAttachment).filter(LeaveAttachment.company_id == principal.company_id,
                                           LeaveAttachment.id == attachment_id).with_for_update().first()
    if row is None or not _can_verify(db, principal, row):
        raise not_found("Document")
    if row.employee_id == principal.employee_id:
        raise forbidden("You cannot verify your own document.")
    if not approve and not (reason or "").strip():
        raise bad_request("Say why the document is rejected.", "REASON_REQUIRED")
    row.verification_status = "VERIFIED" if approve else "REJECTED"
    row.verified_by_user_id, row.verified_at = principal.user_id, utcnow()
    row.rejection_reason = None if approve else reason.strip()[:500]
    if row.leave_id:
        db.add(LeaveMessage(company_id=row.company_id, leave_id=row.leave_id, kind="SYSTEM_EVENT",
                            body=f"Document {'verified' if approve else 'rejected'}" + ("" if approve else f": {row.rejection_reason}")))
    db.flush()
    AuditLogService.log(db, principal, "Leave", "ATTACHMENT_" + row.verification_status,
                        f"Leave document {row.verification_status.lower()}", "leave_attachment", row.id)
    return attachment_json(row)


def request_document(db: Session, principal: Principal, leave_id: int, *, category: str, due_date: date,
                     note: Optional[str]) -> dict:
    leave = _leave(db, principal, leave_id)
    if not (principal.has("leave:verify_documents") and authz.can_access_employee(
            db, principal, "leave:verify_documents", leave.employee_id)) and not _acting_approver(db, principal, leave):
        raise not_found("Leave request")
    if category not in ATTACHMENT_CATEGORIES:
        raise bad_request("Unknown document category.", "UNKNOWN_REFERENCE")
    row = LeaveDocumentRequest(company_id=leave.company_id, leave_id=leave.id, category=category, due_date=due_date,
                               status="OPEN", note=(note or "").strip()[:500] or None,
                               requested_by_user_id=principal.user_id)
    db.add(row)
    db.flush()
    db.add(LeaveMessage(company_id=leave.company_id, leave_id=leave.id, author_user_id=principal.user_id,
                        kind="DOCUMENT_REQUEST", body=f"Please upload a {category.replace('_', ' ').lower()} by "
                                                      f"{due_date:%d %b %Y}." + (f" {row.note}" if row.note else ""),
                        details={"document_request_id": row.id}))
    requester = db.get(Employee, leave.employee_id)
    if requester.user_id:
        NotificationService.create(db, requester.user_id, "Document requested for your leave",
                                   f"Please upload a {category.replace('_', ' ').lower()} by {due_date:%d %b %Y}.",
                                   "leave", f"/leaves/requests?modal=leave&id={leave.id}")
    return document_request_json(row)


def document_request_json(row: LeaveDocumentRequest) -> dict:
    return {"id": row.id, "leave_id": row.leave_id, "category": row.category, "due_date": row.due_date.isoformat(),
            "status": row.status, "note": row.note, "fulfilled_attachment_id": row.fulfilled_attachment_id}


def document_requests(db: Session, principal: Principal, leave_id: int) -> List[dict]:
    leave = _leave(db, principal, leave_id)
    if not can_see_request(db, principal, leave):
        raise not_found("Leave request")
    return [document_request_json(r) for r in db.query(LeaveDocumentRequest).filter(
        LeaveDocumentRequest.company_id == leave.company_id, LeaveDocumentRequest.leave_id == leave.id
    ).order_by(LeaveDocumentRequest.id)]


def documents_queue(db: Session, principal: Principal) -> List[dict]:
    """Verification queue: SUBMITTED documents and OPEN / OVERDUE requests of people the verifier covers."""
    if not principal.has("leave:verify_documents"):
        raise forbidden()
    scope = authz.employee_scope(db, principal, "leave:verify_documents")
    rows = (db.query(LeaveAttachment, Employee)
            .join(Employee, (Employee.company_id == LeaveAttachment.company_id) & (Employee.id == LeaveAttachment.employee_id))
            .filter(LeaveAttachment.company_id == principal.company_id, LeaveAttachment.deleted_at.is_(None),
                    LeaveAttachment.verification_status == "SUBMITTED", scope.clause(LeaveAttachment.employee_id))
            .order_by(LeaveAttachment.id).limit(200).all())
    out = []
    for row, person in rows:
        if row.employee_id == principal.employee_id:
            continue
        visible = _can_view_attachment(db, principal, row, None)
        out.append(attachment_json(row) | {"employee_name": person.full_name, "can_view": visible,
                                           "can_verify": _can_verify(db, principal, row)})
    return out

"""Face encodings at rest (BUG-AS-19; AS 07 §6 B-5..B-8; AS 11 §3.8; SR-AS-06).

* ``seal(db, company_id, employee_id, encoding)`` -> (ciphertext bytes, key id): envelope encryption with the tenant
  data key of purpose ``ATTENDANCE_BIOMETRIC`` (SP-AS-011), bound to the employee (associated data), so a value copied
  to another row or tenant does not decrypt. New enrolments store only the ciphertext.
* ``plaintext(db, row)`` — the matcher's in-memory read: the ciphertext when present, else the legacy plaintext column
  (rows enrolled before AS-M2 until the ``attendance.v2_prepare`` face step encrypts them). Deleted rows yield None.
  No API, export, audit row or log ever carries an encoding.
* ``encrypt_legacy_rows`` — the v2_prepare face step (plaintext -> ciphertext, plaintext column NULL);
  ``decrypt_all_rows`` — the downgrade helper of as_m2_sites_consents (0026 pattern).
* ``queue_deletion`` / ``purge_due`` — consent withdrawal (C-5) and the deletion of due rows (B-8; a legal hold on the
  employee postpones it).
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Iterable, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core import clock
from app.database.db import bind_tenant
from app.models.employee import FaceEncoding

logger = logging.getLogger(__name__)

PURPOSE = "ATTENDANCE_BIOMETRIC"


def _aad(employee_id: int) -> bytes:
    return f"face_encodings.encoding:{employee_id}".encode("ascii")


def seal(db: Session, company_id: int, employee_id: int, encoding: str) -> Tuple[bytes, str]:
    from app.payroll.banking import crypto
    from app.payroll.services import keys

    envelope = keys.encrypt_field(db, company_id, encoding, PURPOSE, aad=_aad(employee_id))
    return envelope.encode("ascii"), crypto.envelope_key_id(envelope)


def open_value(db: Session, company_id: int, employee_id: int, ciphertext: bytes) -> str:
    from app.payroll.services import keys

    if isinstance(ciphertext, (bytes, bytearray, memoryview)):
        raw = bytes(ciphertext).decode("ascii")
    else:
        raw = str(ciphertext)
    return keys.decrypt_field(db, company_id, raw, PURPOSE, aad=_aad(employee_id))


def plaintext(db: Session, row: FaceEncoding) -> Optional[str]:
    if row.deleted_at is not None:
        return None
    if row.encoding_enc is not None:
        return open_value(db, row.company_id, row.employee_id, row.encoding_enc)
    return row.encoding


def candidates(db: Session, rows: Iterable[FaceEncoding]) -> List[Tuple[int, str]]:
    """(employee_id, encoding) pairs for the face engine, decrypted in memory only."""
    found = []
    for row in rows:
        value = plaintext(db, row)
        if value:
            found.append((row.employee_id, value))
    return found


def new_row(db: Session, company_id: int, employee_id: int, encoding: str, **values) -> FaceEncoding:
    ciphertext, key_id = seal(db, company_id, employee_id, encoding)
    return FaceEncoding(company_id=company_id, employee_id=employee_id, encoding=None, encoding_enc=ciphertext,
                        key_id=key_id, **values)


def encrypt_legacy_rows(db: Session, company_id: int) -> int:
    """v2_prepare face step: encrypt every plaintext encoding of the tenant (``db`` is bound to it)."""
    rows = (db.query(FaceEncoding).filter(FaceEncoding.company_id == company_id, FaceEncoding.encoding.isnot(None),
                                          FaceEncoding.deleted_at.is_(None))
            .order_by(FaceEncoding.id).all())
    for row in rows:
        row.encoding_enc, row.key_id = seal(db, company_id, row.employee_id, row.encoding)
        row.encoding = None
    db.flush()
    return len(rows)


def decrypt_all_rows(connection) -> int:
    """Downgrade helper: put every encrypted encoding back in the plaintext column; returns how many."""
    companies = [cid for (cid,) in connection.execute(text(
        "SELECT DISTINCT company_id FROM face_encodings WHERE encoding IS NULL AND encoding_enc IS NOT NULL "
        "ORDER BY company_id"))]
    count = 0
    for company_id in companies:
        db = Session(bind=connection)
        try:
            bind_tenant(db, company_id)
            rows = db.execute(text(
                "SELECT id, employee_id, encoding_enc FROM face_encodings WHERE company_id = :c AND encoding IS NULL "
                "AND encoding_enc IS NOT NULL ORDER BY id"), {"c": company_id}).all()
            for row_id, employee_id, value in rows:
                plain = open_value(db, company_id, employee_id, bytes(value))
                db.execute(text("UPDATE face_encodings SET encoding = :e WHERE company_id = :c AND id = :i"),
                           {"e": plain, "c": company_id, "i": row_id})
                count += 1
            db.flush()
        finally:
            db.close()
    return count


def queue_deletion(db: Session, company_id: int, employee_id: int, on: date) -> int:
    """C-5: a withdrawn BIOMETRIC_FACE consent queues the employee's face data for deletion on ``on``."""
    return (db.query(FaceEncoding)
            .filter(FaceEncoding.company_id == company_id, FaceEncoding.employee_id == employee_id,
                    FaceEncoding.deleted_at.is_(None))
            .update({FaceEncoding.retention_until: on}, synchronize_session=False))


def _on_hold(db: Session, company_id: int, employee_id: int) -> bool:
    """A legal hold covering the employee postpones every purge (07 §13; the shared legal-hold table)."""
    from app.models.legal_holds import LegalHold

    return db.query(LegalHold.id).filter(LegalHold.company_id == company_id, LegalHold.status == "ACTIVE",
                                         LegalHold.scope == "EMPLOYEE",
                                         LegalHold.employee_id == employee_id).first() is not None


def purge_due(db: Session, company_id: int, today: date) -> dict:
    """B-8: delete the ciphertext / plaintext and the enrolment photo of rows whose ``retention_until`` has come
    (``deleted_at`` set, the row kept as the record); a legal hold on the employee postpones it."""
    from app.core import storage
    from app.services.audit_log_service import AuditLogService

    rows = (db.query(FaceEncoding)
            .filter(FaceEncoding.company_id == company_id, FaceEncoding.deleted_at.is_(None),
                    FaceEncoding.retention_until.isnot(None), FaceEncoding.retention_until <= today)
            .order_by(FaceEncoding.id).all())
    purged, held = 0, 0
    holds = {}
    now = clock.utcnow()
    for row in rows:
        if row.employee_id not in holds:
            holds[row.employee_id] = _on_hold(db, company_id, row.employee_id)
        if holds[row.employee_id]:
            held += 1
            continue
        if row.image_path:
            storage.delete(company_id, row.image_path)
        row.encoding, row.encoding_enc, row.key_id, row.image_path = None, None, None, None
        row.deleted_at = now
        row.is_primary = False
        purged += 1
    db.flush()
    if purged:
        AuditLogService.log(db, None, "Attendance", "RETENTION_PURGED", f"Face data purged: {purged} encoding(s)",
                            "face_encodings", None, company_id=company_id, actor_type="system",
                            details={"class": "BIOMETRIC", "count": purged, "held": held})
    return {"purged": purged, "held": held}

"""Reading the stored proof of a manual payment (3.7 §7.3, EC-PAY-14).

``proof_file(db, principal, proof_id)`` returns the file a reviewer needs before approving or rejecting a
manual settlement. Access is exactly that of the review itself (``payment_events.load_manual_proof``):
``payroll:payment:reconcile`` (403 without it), the instruction's legal entity inside the caller's scope and
the caller's own tenant (404 otherwise, never revealing whether the proof exists). The stored file is
resolved inside the tenant's storage folder only, its SHA-256 is checked against the hash recorded at upload
(409 FILE_HASH_MISMATCH if it changed) and every download is audited (MANUAL_PROOF_DOWNLOADED).

Services take (db, principal, ...) and never commit.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core import storage
from app.core.errors import conflict
from app.core.principal import Principal
from app.payroll.services import payment_events as events

MANUAL_PROOF_DOWNLOADED = "MANUAL_PROOF_DOWNLOADED"


@dataclass(frozen=True)
class ProofFile:
    data: bytes
    file_name: str
    mime: str
    sha256: str


def _safe_name(name: str, mime: str) -> str:
    # ASCII only: the name goes into a Content-Disposition header (latin-1), so a non-Latin upload name
    # (e.g. Cyrillic or Devanagari) must not turn the download into a server error.
    cleaned = "".join(ch for ch in (name or "") if ch.isascii() and (ch.isalnum() or ch in "._- ")).strip(" .")
    if not cleaned or cleaned.lower() in {"pdf", "png", "jpg", "jpeg"}:
        cleaned = "proof"
    ext = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg"}.get(mime, "")
    if ext and not cleaned.lower().endswith(ext) and not (ext == ".jpg" and cleaned.lower().endswith(".jpeg")):
        cleaned = f"{cleaned}{ext}"
    return cleaned[:150]


def proof_file(db: Session, principal: Principal, proof_id: int) -> ProofFile:
    proof, instruction = events.load_manual_proof(db, principal, proof_id)
    data = storage.resolve(principal.company_id, proof.evidence_storage_path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != proof.evidence_sha256:
        raise conflict("The stored proof does not match the file that was uploaded.", "FILE_HASH_MISMATCH")
    events.audit(db, principal, MANUAL_PROOF_DOWNLOADED,
                 f"Proof of manual payment for {instruction.reference or instruction.id} downloaded",
                 "payment_manual_settlement", proof.id, instruction.company_id, instruction_id=instruction.id,
                 sha256=digest)
    return ProofFile(data=data, file_name=_safe_name(proof.evidence_name, proof.evidence_mime),
                     mime=proof.evidence_mime or "application/octet-stream", sha256=digest)


__all__ = ["MANUAL_PROOF_DOWNLOADED", "ProofFile", "proof_file"]

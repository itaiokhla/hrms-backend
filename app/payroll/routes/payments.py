"""Payments API (3.9 §4 Payments; 3.7 §7-§11).

/api/v1/payroll/payment-batches (proposal, create, approve, reject, cancel, generate-file, file, release,
bank-changes), /payroll/payment-obligations, /payroll/payment-balances, /payroll/payment-instructions
(events, reissue, cancel, release-hold, manual proof) and /payroll/manual-settlements/{id}/approve|reject.

Permissions payroll:payment:prepare / approve / submit / reconcile, scoped by legal entity: objects of
entities outside the caller's scope are 404, a missing permission is 403. Responses mask every bank
account; bank files are regenerated per download (audited, ``Cache-Control: no-store``). Releasing a
batch needs a fresh step-up (403 STEP_UP_REQUIRED). State-changing requests accept ``Idempotency-Key``
and ``If-Match`` / ``row_version``.
"""

from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import Field
from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.core.errors import conflict
from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.models.payment_flows import BATCH_STATUSES, INSTRUCTION_STATUSES, MANUAL_METHODS, OBLIGATION_STATUSES
from app.payroll.api_types import StrictModel
from app.payroll.services import idempotency, obligations
from app.payroll.services import payment_batches as batches
from app.payroll.services import payment_events as events

router = APIRouter(prefix="/api/v1", tags=["Payments"])

BatchStatus = Literal[BATCH_STATUSES]  # type: ignore[valid-type]
InstructionStatus = Literal[INSTRUCTION_STATUSES]  # type: ignore[valid-type]
ObligationStatus = Literal[OBLIGATION_STATUSES]  # type: ignore[valid-type]
ManualMethod = Literal[MANUAL_METHODS]  # type: ignore[valid-type]
NO_STORE = {"Cache-Control": "private, no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff"}


class ProposalIn(StrictModel):
    run_id: int = Field(..., ge=1)
    funding_account_id: Optional[int] = Field(None, ge=1)
    method: Optional[Literal["BANK_FILE", "API"]] = None


class BatchIn(ProposalIn):
    value_date: Optional[date] = None


class VersionIn(StrictModel):
    row_version: Optional[int] = Field(None, ge=1)


class ReasonIn(StrictModel):
    reason: str = Field(..., min_length=1, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)


class ReleaseIn(StrictModel):
    row_version: Optional[int] = Field(None, ge=1)
    file_sha256: Optional[str] = Field(None, pattern=r"^[0-9a-fA-F]{64}$")


class BankChangesIn(StrictModel):
    decision: Literal["PROCEED", "RECREATE"]
    reason: str = Field(..., min_length=1, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)


class NoteIn(StrictModel):
    note: Optional[str] = Field(None, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)


class RejectProofIn(StrictModel):
    note: str = Field(..., min_length=1, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)


def _if_match(request: Request, body_version: Optional[int]) -> Optional[int]:
    header = request.headers.get("if-match")
    if header:
        value = header.strip().removeprefix("W/").strip('"')
        if value.isdigit():
            return int(value)
    return body_version


def _batch_result(data: dict) -> dict:
    """Compact action result (stored for idempotent replays; never bank details)."""
    return {k: data[k] for k in ("id", "reference", "status", "run_id", "method", "currency", "item_count", "total",
                                 "sandbox", "row_version", "file") if k in data}


# ---------------------------------------------------------------------------
# Batches
# ---------------------------------------------------------------------------
@router.post("/payroll/payment-batches/proposal")
def propose_batches(data: ProposalIn, request: Request, principal: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)):
    """The batches the run's ready payments would form and the exclusions report (EC-PAY-03). Re-checks
    readiness (holds whose cause is gone lift) and commits that, nothing else."""
    def handler():
        return 200, batches.propose(db, principal, data.run_id, funding_account_id=data.funding_account_id,
                                    method=data.method)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.post("/payroll/payment-batches", status_code=201)
def create_batches(data: BatchIn, request: Request, principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db)):
    def handler():
        result = batches.create_batches(db, principal, data.run_id, funding_account_id=data.funding_account_id,
                                        method=data.method, value_date=data.value_date)
        return 201, {"run_id": result["run_id"], "batches": [_batch_result(b) for b in result["batches"]],
                     "exclusions": result["exclusions"], "bank_changes": result["bank_changes"]}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/payroll/payment-batches")
def list_batches(run_id: Optional[int] = Query(None, ge=1), status: Optional[BatchStatus] = Query(None),
                 limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0),
                 principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return batches.list_batches(db, principal, run_id=run_id, status=status, limit=limit, offset=offset)


@router.get("/payroll/payment-batches/{batch_id}")
def get_batch(batch_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return batches.get_batch(db, principal, batch_id)


@router.post("/payroll/payment-batches/{batch_id}/approve")
def approve_batch(batch_id: int, request: Request, data: Optional[VersionIn] = None,
                  principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or VersionIn()

    def handler():
        return 200, _batch_result(batches.approve(db, principal, batch_id,
                                                  expected_version=_if_match(request, data.row_version)))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/payroll/payment-batches/{batch_id}/reject")
def reject_batch(batch_id: int, data: ReasonIn, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    def handler():
        return 200, _batch_result(batches.reject(db, principal, batch_id, data.reason,
                                                 expected_version=_if_match(request, data.row_version)))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/payroll/payment-batches/{batch_id}/cancel")
def cancel_batch(batch_id: int, data: ReasonIn, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    def handler():
        return 200, _batch_result(batches.cancel(db, principal, batch_id, data.reason,
                                                 expected_version=_if_match(request, data.row_version)))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/payroll/payment-batches/{batch_id}/generate-file")
def generate_file(batch_id: int, request: Request, data: Optional[VersionIn] = None,
                  principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or VersionIn()

    def handler():
        return 200, _batch_result(batches.generate_file(db, principal, batch_id,
                                                        expected_version=_if_match(request, data.row_version)))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.get("/payroll/payment-batches/{batch_id}/file")
def download_file(batch_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """The batch file, regenerated and verified against the stored SHA-256 (audited). Downloading moves no
    payment past QUEUED (EC-PAY-01)."""
    filename, media_type, content, sha256 = batches.download_file(db, principal, batch_id)
    db.commit()
    headers = {**NO_STORE, "Content-Disposition": f'attachment; filename="{filename}"', "X-Content-SHA256": sha256}
    return Response(content=content, media_type=media_type, headers=headers)


@router.post("/payroll/payment-batches/{batch_id}/release")
def release_batch(batch_id: int, request: Request, data: Optional[ReleaseIn] = None,
                  principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """"Mark as submitted" (bank file uploaded by the releaser) or submission to the SANDBOX payout adapter.
    Step-up, a funding-account signatory and dual control are required (EC-PAY-13: once only)."""
    data = data or ReleaseIn()

    def handler():
        return 200, _batch_result(batches.release(db, principal, batch_id,
                                                  expected_version=_if_match(request, data.row_version),
                                                  file_sha256=data.file_sha256))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/payroll/payment-batches/{batch_id}/bank-changes")
def resolve_bank_changes(batch_id: int, data: BankChangesIn, request: Request,
                         principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """EC-BNK-03: proceed with the beneficiary snapshot, or cancel and recreate the payments."""
    def handler():
        result = batches.resolve_bank_changes(db, principal, batch_id, data.decision, data.reason,
                                              expected_version=_if_match(request, data.row_version))
        return 200, {"decision": result["decision"], "batch": _batch_result(result["batch"]),
                     "created": [{"id": r["id"], "reference": r["reference"], "status": r["status"]}
                                 for r in result["created"]]}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


# ---------------------------------------------------------------------------
# Obligations and balances (EC-PAY-23)
# ---------------------------------------------------------------------------
@router.get("/payroll/payment-obligations")
def list_obligations(run_id: Optional[int] = Query(None, ge=1), employee_id: Optional[int] = Query(None, ge=1),
                     status: Optional[ObligationStatus] = Query(None), limit: int = Query(200, ge=1, le=500),
                     offset: int = Query(0, ge=0), principal: Principal = Depends(get_principal),
                     db: Session = Depends(get_db)):
    return obligations.list_obligations(db, principal, run_id=run_id, employee_id=employee_id, status=status,
                                        limit=limit, offset=offset)


@router.get("/payroll/payment-obligations/{obligation_id}")
def get_obligation(obligation_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return obligations.get_obligation(db, principal, obligation_id)


@router.get("/payroll/payment-balances")
def balances(group_by: Literal["employee", "run", "legal_entity", "batch"] = Query("employee"),
             employee_id: Optional[int] = Query(None, ge=1), run_id: Optional[int] = Query(None, ge=1),
             legal_entity_id: Optional[int] = Query(None, ge=1), principal: Principal = Depends(get_principal),
             db: Session = Depends(get_db)):
    """Obligated, intended, submitted, settled, returned and outstanding (= obligated − settled), exactly."""
    if group_by == "batch":
        return batches.batch_balances(db, principal, run_id=run_id)
    return obligations.balances(db, principal, group_by=group_by, employee_id=employee_id, run_id=run_id,
                                legal_entity_id=legal_entity_id)


# ---------------------------------------------------------------------------
# Instructions
# ---------------------------------------------------------------------------
@router.get("/payroll/payment-instructions")
def list_instructions(run_id: Optional[int] = Query(None, ge=1), batch_id: Optional[int] = Query(None, ge=1),
                      employee_id: Optional[int] = Query(None, ge=1), status: Optional[InstructionStatus] = Query(None),
                      limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
                      principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return batches.list_instructions(db, principal, run_id=run_id, batch_id=batch_id, employee_id=employee_id,
                                     status=status, limit=limit, offset=offset)


@router.get("/payroll/payment-instructions/{instruction_id}")
def get_instruction(instruction_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return events.get_instruction(db, principal, instruction_id)


@router.get("/payroll/payment-instructions/{instruction_id}/events")
def instruction_events(instruction_id: int, principal: Principal = Depends(get_principal),
                       db: Session = Depends(get_db)):
    return events.list_events(db, principal, instruction_id)


def _instruction_result(row) -> dict:
    return {"id": row.id, "reference": row.reference, "status": row.status, "row_version": row.row_version}


@router.post("/payroll/payment-instructions/{instruction_id}/cancel")
def cancel_instruction(instruction_id: int, data: ReasonIn, request: Request,
                       principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Cancel an unsent payment outside a batch (its obligation stays open)."""
    def handler():
        row = events.get_instruction_for(db, principal, instruction_id, events.PREPARE)
        if row.batch_id is not None:
            raise conflict("Cancel or reject the batch first.", "IN_BATCH")
        row = events.transition(db, principal, row, "CANCEL", reason=data.reason,
                                expected_version=_if_match(request, data.row_version),
                                updates={"status_reason": "CANCELLED_BY_USER"})
        events.sync_after(db, principal, [row])
        return 200, _instruction_result(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/payroll/payment-instructions/{instruction_id}/release-hold")
def release_hold(instruction_id: int, data: ReasonIn, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    """Release a hold (payroll:payment:approve); the payment is re-checked and joins the next batch."""
    def handler():
        row = obligations.release_hold(db, principal, instruction_id, data.reason,
                                       expected_version=_if_match(request, data.row_version))
        return 200, _instruction_result(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/payroll/payment-instructions/{instruction_id}/reissue")
def reissue(instruction_id: int, data: ReasonIn, request: Request, principal: Principal = Depends(get_principal),
            db: Session = Depends(get_db)):
    """Reissue a failed or returned payment (EC-PAY-04): same obligation and amount, no new earnings;
    with payroll.payments.reissue_requires_approval a second person approves it."""
    def handler():
        result = obligations.request_reissue(db, principal, instruction_id, data.reason,
                                             expected_version=_if_match(request, data.row_version))
        return 200, {"status": result["status"], "instruction": _compact(result["instruction"]),
                     "created": [_compact(r) for r in result["created"]]}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/payroll/payment-instructions/{instruction_id}/reissue/approve")
def approve_reissue(instruction_id: int, request: Request, data: Optional[NoteIn] = None,
                    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or NoteIn()

    def handler():
        result = obligations.approve_reissue(db, principal, instruction_id, data.note,
                                             expected_version=_if_match(request, data.row_version))
        return 200, {"status": result["status"], "instruction": _compact(result["instruction"]),
                     "created": [_compact(r) for r in result["created"]]}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


def _compact(data: dict) -> dict:
    return {k: data.get(k) for k in ("id", "reference", "status", "obligation_id", "amount", "reissue_of_instruction_id",
                                     "row_version")}


# ---------------------------------------------------------------------------
# Manual settlement with a second reviewer (EC-PAY-14)
# ---------------------------------------------------------------------------
@router.post("/payroll/payment-instructions/{instruction_id}/manual-settle", status_code=201)
async def submit_manual_proof(instruction_id: int, request: Request, method: ManualMethod = Form(...),
                              reference: str = Form(..., min_length=1, max_length=100),
                              note: Optional[str] = Form(None, max_length=1000),
                              row_version: Optional[int] = Form(None, ge=1), file: UploadFile = File(...),
                              principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Upload proof of a payment made outside the system; nothing settles until a second person approves."""
    content = await file.read(min(app_settings.max_upload_size, events.MAX_EVIDENCE_BYTES) + 1)
    proof = events.submit_manual_proof(db, principal, instruction_id, method=method, reference=reference, note=note,
                                       file_name=file.filename or "proof", content=content,
                                       expected_version=_if_match(request, row_version))
    db.commit()
    return events.manual_proof_json(proof)


@router.post("/payroll/manual-settlements/{proof_id}/approve")
def approve_manual_proof(proof_id: int, request: Request, data: Optional[NoteIn] = None,
                         principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or NoteIn()

    def handler():
        proof = events.review_manual_proof(db, principal, proof_id, approve=True, note=data.note,
                                           expected_version=_if_match(request, data.row_version))
        return 200, {"id": proof.id, "status": proof.status, "instruction_id": proof.instruction_id,
                     "row_version": proof.row_version}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/payroll/manual-settlements/{proof_id}/reject")
def reject_manual_proof(proof_id: int, data: RejectProofIn, request: Request,
                        principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        proof = events.review_manual_proof(db, principal, proof_id, approve=False, note=data.note,
                                           expected_version=_if_match(request, data.row_version))
        return 200, {"id": proof.id, "status": proof.status, "instruction_id": proof.instruction_id,
                     "row_version": proof.row_version}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())

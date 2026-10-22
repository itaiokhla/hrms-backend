"""Corrections API under /api/v1/payroll (3.5 §12, §13, §15; 3.9 §4 Runs): arrears proposals and retro
runs, run reversals, off-cycle runs and final settlements.

State-changing POSTs accept ``Idempotency-Key`` and an optional ``row_version`` (409 on conflict). Their
responses are compact action results (ids, statuses, row versions) without amounts, so idempotency
records never hold pay data; amounts are read with GET. Unknown or out-of-scope objects are 404, a
missing permission is 403.
"""

from datetime import date
from decimal import Decimal
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field, field_validator
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.models.corrections import (
    ARREARS_STATUSES,
    DEADLINE_BASES,
    REVERSAL_STATUSES,
    SETTLEMENT_STATUSES,
    TERMINATION_TYPES,
)
from app.payroll.api_types import DecimalStr, MoneyIn, StrictModel
from app.payroll.services import final_settlement, idempotency, off_cycle, retro, reversals

router = APIRouter(prefix="/api/v1/payroll", tags=["Payroll corrections"])

ArrearsStatus = Literal[ARREARS_STATUSES]  # type: ignore[valid-type]
ReversalStatus = Literal[REVERSAL_STATUSES]  # type: ignore[valid-type]
SettlementStatus = Literal[SETTLEMENT_STATUSES]  # type: ignore[valid-type]
TerminationType = Literal[TERMINATION_TYPES]  # type: ignore[valid-type]
DeadlineBasis = Literal[DEADLINE_BASES]  # type: ignore[valid-type]
Trigger = Literal[retro.DETECTABLE]  # type: ignore[valid-type]
OffCycleKind = Literal[off_cycle.KINDS]  # type: ignore[valid-type]


def _required(value: str) -> str:
    if not value.strip():
        raise ValueError("This field cannot be blank.")
    return value.strip()


class ReasonBody(StrictModel):
    reason: str = Field(min_length=1, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)

    @field_validator("reason")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _required(value)


class CommentBody(StrictModel):
    comment: Optional[str] = Field(None, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)


class NoteBody(StrictModel):
    note: str = Field(min_length=1, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)

    @field_validator("note")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _required(value)


class VersionBody(StrictModel):
    row_version: Optional[int] = Field(None, ge=1)


# ---------------------------------------------------------------------------
# Arrears proposals and retro runs
# ---------------------------------------------------------------------------
class DetectIn(StrictModel):
    employee_id: int = Field(ge=1)
    from_date: date
    to_date: Optional[date] = None
    trigger: Trigger
    keys: List[str] = Field(default_factory=list, max_length=50)
    policy_types: List[str] = Field(default_factory=list, max_length=20)
    reason: str = Field(min_length=1, max_length=1000)
    preview: bool = False

    @field_validator("reason")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _required(value)


class RetroRunIn(StrictModel):
    pay_group_id: int = Field(ge=1)
    pay_period_id: int = Field(ge=1)
    proposal_ids: List[int] = Field(min_length=1, max_length=5000)
    label: Optional[str] = Field(None, max_length=150)


def _proposal_result(row) -> dict:
    return {"id": row.id, "status": row.status, "payment_mode": row.payment_mode, "recovery_mode": row.recovery_mode,
            "row_version": row.row_version}


def _run_result(run) -> dict:
    return {"id": run.id, "number": run.number, "run_type": run.run_type, "status": run.status,
            "row_version": run.row_version}


@router.get("/arrears-proposals")
def list_arrears_proposals(
    status: Optional[ArrearsStatus] = None, employee_id: Optional[int] = Query(None, ge=1),
    run_id: Optional[int] = Query(None, ge=1), skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
):
    return retro.list_proposals(db, principal, status=status, employee_id=employee_id, run_id=run_id, skip=skip,
                                limit=limit)


@router.get("/arrears-proposals/{proposal_id}")
def get_arrears_proposal(proposal_id: int, principal: Principal = Depends(get_principal),
                         db: Session = Depends(get_db)):
    return retro.get_proposal(db, principal, proposal_id)


@router.post("/arrears-proposals/detect")
def detect_arrears(data: DetectIn, request: Request, principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db)):
    """Apply a past change of one employee to the finalized periods it reaches. ``preview`` returns the
    differences without recording anything; otherwise proposals are recorded for approval."""
    change = retro.Change(data.trigger, reason=data.reason, keys=tuple(data.keys),
                          policy_types=tuple(data.policy_types), ref={"type": "manual_detect"})
    if data.preview:
        outcomes = retro.detect_for(db, principal, employee_id=data.employee_id, from_date=data.from_date,
                                    to_date=data.to_date, change=change, preview=True)
        items = []
        for o in outcomes:
            cur = _currency(db, o)
            items.append({"run_id": o.run_id, "run_employee_id": o.run_employee_id, "period": o.period,
                          "issues": o.issues,
                          "lines": [{"component_code": l.code, "direction": l.direction,
                                     "delta": retro._money(l.delta, cur), "original": retro._money(l.original, cur),
                                     "recalculated": retro._money(l.recalculated, cur)} for l in o.lines]})
        return {"items": items}

    def handler():
        outcomes = retro.detect_for(db, principal, employee_id=data.employee_id, from_date=data.from_date,
                                    to_date=data.to_date, change=change)
        return 200, {"items": [{"run_id": o.run_id, "run_employee_id": o.run_employee_id,
                                "proposal_id": o.proposal.id if o.proposal is not None else None,
                                "status": o.proposal.status if o.proposal is not None else "NO_DIFFERENCE",
                                "superseded_id": o.superseded_id, "cancelled_id": o.cancelled_id,
                                "issues": [i["code"] for i in o.issues]} for o in outcomes]}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


def _currency(db: Session, outcome) -> str:
    from app.models.pay_runs import PayrollRun

    return db.query(PayrollRun.currency).filter(PayrollRun.id == outcome.run_id).scalar()


@router.post("/arrears-proposals/{proposal_id}/approve")
def approve_arrears(proposal_id: int, request: Request, data: Optional[CommentBody] = None,
                    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or CommentBody()

    def handler():
        row = retro.decide(db, principal, proposal_id, "APPROVE", remarks=data.comment,
                           expected_version=data.row_version)
        return 200, _proposal_result(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/arrears-proposals/{proposal_id}/reject")
def reject_arrears(proposal_id: int, data: ReasonBody, request: Request,
                   principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        row = retro.decide(db, principal, proposal_id, "REJECT", remarks=data.reason,
                           expected_version=data.row_version)
        return 200, _proposal_result(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/retro-runs", status_code=201)
def create_retro_run(data: RetroRunIn, request: Request, principal: Principal = Depends(get_principal),
                     db: Session = Depends(get_db)):
    def handler():
        run = retro.create_retro_run(db, principal, retro.RetroRunRequest(
            pay_group_id=data.pay_group_id, pay_period_id=data.pay_period_id,
            proposal_ids=tuple(data.proposal_ids), label=data.label))
        return 201, _run_result(run)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.get("/runs/{run_id}/employees/{run_employee_id}/corrected-view")
def corrected_view(run_id: int, run_employee_id: int, principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db)):
    return retro.corrected_view(db, principal, run_id, run_employee_id)


# ---------------------------------------------------------------------------
# Reversals
# ---------------------------------------------------------------------------
class ReverseIn(StrictModel):
    reason: str = Field(min_length=1, max_length=1000)
    run_employee_ids: List[int] = Field(default_factory=list, max_length=5000)
    row_version: Optional[int] = Field(None, ge=1)

    @field_validator("reason")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _required(value)


def _reversal_result(row) -> dict:
    return {"id": row.id, "status": row.status, "original_run_id": row.original_run_id,
            "reversal_run_id": row.reversal_run_id, "row_version": row.row_version}


@router.post("/runs/{run_id}/reverse", status_code=201)
def reverse_run(run_id: int, data: ReverseIn, request: Request, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    def handler():
        row = reversals.request_reversal(db, principal, run_id, reason=data.reason,
                                         run_employee_ids=tuple(data.run_employee_ids),
                                         expected_version=data.row_version)
        return 201, _reversal_result(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.get("/reversals")
def list_reversals(run_id: Optional[int] = Query(None, ge=1), status: Optional[ReversalStatus] = None,
                   skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                   principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return reversals.list_reversals(db, principal, run_id=run_id, status=status, skip=skip, limit=limit)


@router.get("/reversals/{reversal_id}")
def get_reversal(reversal_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return reversals.get_reversal(db, principal, reversal_id)


@router.post("/reversals/{reversal_id}/approve")
def approve_reversal(reversal_id: int, request: Request, data: Optional[CommentBody] = None,
                     principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or CommentBody()

    def handler():
        row = reversals.approve_reversal(db, principal, reversal_id, comment=data.comment,
                                         expected_version=data.row_version)
        return 200, _reversal_result(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/reversals/{reversal_id}/reject")
def reject_reversal(reversal_id: int, data: ReasonBody, request: Request,
                    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        row = reversals.reject_reversal(db, principal, reversal_id, reason=data.reason,
                                        expected_version=data.row_version)
        return 200, _reversal_result(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/reversals/{reversal_id}/cancel")
def cancel_reversal(reversal_id: int, data: ReasonBody, request: Request,
                    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        row = reversals.cancel_reversal(db, principal, reversal_id, reason=data.reason,
                                        expected_version=data.row_version)
        return 200, _reversal_result(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


# ---------------------------------------------------------------------------
# Off-cycle runs
# ---------------------------------------------------------------------------
class OffCycleIn(StrictModel):
    kind: OffCycleKind
    reason: str = Field(min_length=1, max_length=1000)
    pay_group_id: Optional[int] = Field(None, ge=1)
    pay_period_id: Optional[int] = Field(None, ge=1)
    employee_ids: List[int] = Field(default_factory=list, max_length=5000)
    component_codes: List[str] = Field(default_factory=list, max_length=50)
    original_run_id: Optional[int] = Field(None, ge=1)
    instruction_ids: List[int] = Field(default_factory=list, max_length=5000)
    loan_ids: List[int] = Field(default_factory=list, max_length=1000)
    label: Optional[str] = Field(None, max_length=150)

    @field_validator("reason")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _required(value)


@router.post("/off-cycle-runs", status_code=201)
def create_off_cycle_run(data: OffCycleIn, request: Request, principal: Principal = Depends(get_principal),
                         db: Session = Depends(get_db)):
    def handler():
        run = off_cycle.create_off_cycle_run(db, principal, off_cycle.OffCycleRequest(
            kind=data.kind, pay_group_id=data.pay_group_id, pay_period_id=data.pay_period_id, reason=data.reason,
            employee_ids=tuple(data.employee_ids), component_codes=tuple(data.component_codes),
            original_run_id=data.original_run_id, instruction_ids=tuple(data.instruction_ids),
            loan_ids=tuple(data.loan_ids), label=data.label))
        return 201, {**_run_result(run), "kind": data.kind}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.get("/off-cycle-runs")
def list_off_cycle_runs(kind: Optional[OffCycleKind] = None, skip: int = Query(0, ge=0),
                        limit: int = Query(50, ge=1, le=200), principal: Principal = Depends(get_principal),
                        db: Session = Depends(get_db)):
    return off_cycle.list_off_cycle_runs(db, principal, kind=kind, skip=skip, limit=limit)


# ---------------------------------------------------------------------------
# Final settlements
# ---------------------------------------------------------------------------
class PayeeIn(StrictModel):
    type: Literal["NOMINEE", "LEGAL_HEIR"]
    name: str = Field(min_length=1, max_length=150)
    relationship: str = Field(min_length=1, max_length=60)
    evidence_ref: str = Field(min_length=1, max_length=300)


class CaseIn(StrictModel):
    employee_id: int = Field(ge=1)
    termination_type: TerminationType
    last_working_day: date
    notice_given_on: Optional[date] = None
    payee: Optional[PayeeIn] = None
    hold_reason: Optional[str] = Field(None, max_length=500)
    note: Optional[str] = Field(None, max_length=1000)


class ItemConfirmIn(StrictModel):
    amount: Optional[MoneyIn] = None
    units: Optional[DecimalStr] = None
    rate: Optional[DecimalStr] = None
    direction: Optional[Literal["PAY", "RECOVER"]] = None
    note: Optional[str] = Field(None, max_length=1000)
    consent_ref: Optional[str] = Field(None, max_length=300)
    contract_ref: Optional[str] = Field(None, max_length=200)
    row_version: Optional[int] = Field(None, ge=1)


class SettlementRunIn(StrictModel):
    label: Optional[str] = Field(None, max_length=150)
    row_version: Optional[int] = Field(None, ge=1)


class DeadlineIn(StrictModel):
    legal_entity_id: Optional[int] = Field(None, ge=1)
    days: int = Field(ge=0, le=365)
    day_basis: DeadlineBasis
    note: Optional[str] = Field(None, max_length=500)
    row_version: Optional[int] = Field(None, ge=1)


def _case_result(case) -> dict:
    return {"id": case.id, "status": case.status, "held": case.held, "run_id": case.run_id,
            "due_basis": case.due_basis, "row_version": case.row_version}


def _item_result(item) -> dict:
    return {"id": item.id, "item_type": item.item_type, "status": item.status, "row_version": item.row_version}


@router.get("/final-settlements")
def list_final_settlements(status: Optional[SettlementStatus] = None, overdue: Optional[bool] = None,
                           employee_id: Optional[int] = Query(None, ge=1), skip: int = Query(0, ge=0),
                           limit: int = Query(50, ge=1, le=200), principal: Principal = Depends(get_principal),
                           db: Session = Depends(get_db)):
    return final_settlement.list_cases(db, principal, status=status, overdue=overdue, employee_id=employee_id,
                                       skip=skip, limit=limit)


@router.post("/final-settlements", status_code=201)
def open_final_settlement(data: CaseIn, request: Request, principal: Principal = Depends(get_principal),
                          db: Session = Depends(get_db)):
    def handler():
        case = final_settlement.open_case(db, principal, final_settlement.CaseData(
            employee_id=data.employee_id, termination_type=data.termination_type,
            last_working_day=data.last_working_day, notice_given_on=data.notice_given_on,
            payee=data.payee.model_dump() if data.payee is not None else None, hold_reason=data.hold_reason,
            note=data.note))
        return 201, _case_result(case)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/final-settlements/{case_id}")
def get_final_settlement(case_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return final_settlement.get_case(db, principal, case_id)


@router.post("/final-settlements/{case_id}/checklist/refresh")
def refresh_checklist(case_id: int, request: Request, data: Optional[VersionBody] = None,
                      principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or VersionBody()

    def handler():
        case = final_settlement.refresh_checklist(db, principal, case_id, expected_version=data.row_version)
        return 200, _case_result(case)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/final-settlements/{case_id}/items/{item_id}/confirm")
def confirm_item(case_id: int, item_id: int, request: Request, data: Optional[ItemConfirmIn] = None,
                 principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or ItemConfirmIn()

    def handler():
        item = final_settlement.confirm_item(db, principal, case_id, item_id, final_settlement.ItemDecision(
            amount=data.amount.decimal if data.amount is not None else None,
            currency=data.amount.currency if data.amount is not None else None,
            units=Decimal(data.units) if data.units is not None else None,
            rate=Decimal(data.rate) if data.rate is not None else None,
            direction=data.direction, note=data.note, consent_ref=data.consent_ref, contract_ref=data.contract_ref,
            expected_version=data.row_version))
        return 200, _item_result(item)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.post("/final-settlements/{case_id}/items/{item_id}/waive")
def waive_item(case_id: int, item_id: int, data: NoteBody, request: Request,
               principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        item = final_settlement.waive_item(db, principal, case_id, item_id, data.note,
                                           expected_version=data.row_version)
        return 200, _item_result(item)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/final-settlements/{case_id}/release-hold")
def release_hold(case_id: int, data: NoteBody, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    def handler():
        case = final_settlement.release_hold(db, principal, case_id, data.note, expected_version=data.row_version)
        return 200, _case_result(case)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/final-settlements/{case_id}/cancel")
def cancel_final_settlement(case_id: int, data: ReasonBody, request: Request,
                            principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        case = final_settlement.cancel_case(db, principal, case_id, data.reason, expected_version=data.row_version)
        return 200, _case_result(case)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/final-settlements/{case_id}/run", status_code=201)
def create_settlement_run(case_id: int, request: Request, data: Optional[SettlementRunIn] = None,
                          principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or SettlementRunIn()

    def handler():
        run = final_settlement.create_settlement_run(db, principal, case_id, label=data.label,
                                                     expected_version=data.row_version)
        return 201, _run_result(run)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.get("/final-settlement-deadlines")
def list_deadlines(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return {"items": final_settlement.list_deadlines(db, principal)}


@router.put("/final-settlement-deadlines")
def save_deadline(data: DeadlineIn, request: Request, principal: Principal = Depends(get_principal),
                  db: Session = Depends(get_db)):
    def handler():
        row = final_settlement.save_deadline(db, principal, legal_entity_id=data.legal_entity_id, days=data.days,
                                             day_basis=data.day_basis, note=data.note,
                                             expected_version=data.row_version)
        return 200, final_settlement.deadline_json(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())

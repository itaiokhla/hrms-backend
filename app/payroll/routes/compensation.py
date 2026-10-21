"""Compensation v2 API (3.9 §4): components, salary structures, compensation, revision cycles.

All under /api/v1/payroll; state-changing requests accept Idempotency-Key; money is {"amount": "<decimal
string>", "currency": "XXX"}; unknown or out-of-scope objects are 404, a missing permission 403.
"""

from datetime import date
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import Response
from pydantic import Field, model_validator
from sqlalchemy.orm import Session

from app.core.errors import bad_request
from app.core.principal import Principal, get_principal, require, require_any
from app.database.db import get_db
from app.models.company import Company
from app.payroll.api_types import DecimalStr, StrictModel, money_in_model
from app.payroll.services import compensation as comp_service
from app.payroll.services import component_seed, idempotency
from app.payroll.services import components as comp
from app.payroll.services import revisions
from app.payroll.services import structures

router = APIRouter(prefix="/api/v1/payroll", tags=["Payroll compensation"])

PayBasisIn = money_in_model("PayBasisIn", payroll_currency_only=True, allow_negative=False)
ThresholdIn = money_in_model("ThresholdIn", payroll_currency_only=True, allow_negative=False)
Code = Field(min_length=1, max_length=40)


def _dump(data: Optional[StrictModel]) -> Any:
    return data.model_dump(mode="json") if data is not None else None


# --------------------------------------------------------------------------- components
class ComponentFields(StrictModel):
    name: Optional[str] = Field(None, max_length=150)
    translations: Optional[Dict[str, str]] = None
    description: Optional[str] = Field(None, max_length=1000)
    calc_method: Optional[str] = Field(None, max_length=15)
    formula: Optional[str] = Field(None, max_length=4000)
    params: Optional[Dict[str, Any]] = None
    tags: Optional[List[str]] = Field(None, max_length=30)
    proration: Optional[str] = Field(None, max_length=20)
    proration_basis: Optional[str] = Field(None, max_length=20)
    frequency: Optional[Dict[str, Any]] = None
    taxability: Optional[Dict[str, str]] = None
    totals_flags: Optional[Dict[str, bool]] = None
    cap: Optional[Dict[str, Any]] = None
    floor: Optional[Dict[str, Any]] = None
    rounding: Optional[str] = Field(None, max_length=10)
    eligibility: Optional[str] = Field(None, max_length=4000)
    payslip: Optional[Dict[str, Any]] = None
    visibility: Optional[str] = Field(None, max_length=20)
    gl: Optional[Dict[str, Any]] = None
    reporting_codes: Optional[Dict[str, str]] = None
    input_approval_above: Optional[ThresholdIn] = None
    effective_from: Optional[date] = None

    def to_input(self, reason: Optional[str] = None) -> comp.ComponentInput:
        values = {name: getattr(self, name) for name in comp.ComponentInput.__dataclass_fields__
                  if name not in ("reason", "cleared") and hasattr(self, name)}
        if self.input_approval_above is not None:
            values["input_approval_above"] = {"amount": self.input_approval_above.amount,
                                              "currency": self.input_approval_above.currency}
        cleared = {name for name in self.model_fields_set if getattr(self, name) is None}
        return comp.ComponentInput(**values, reason=reason, cleared=cleared)


class ComponentIn(ComponentFields):
    code: str = Code
    kind: str = Field(min_length=1, max_length=25)
    category: str = Field(min_length=1, max_length=25)
    name: str = Field(min_length=1, max_length=150)
    calc_method: str = Field(min_length=1, max_length=15)


class ComponentVersionIn(ComponentFields):
    reason: Optional[str] = Field(None, max_length=500)


class ComponentPatchIn(StrictModel):
    code: Optional[str] = Field(None, min_length=1, max_length=40)
    status: Optional[Literal["ACTIVE", "ARCHIVED"]] = None


class FormulaCheckIn(StrictModel):
    formula: str = Field(min_length=1, max_length=4000)
    currency: Optional[str] = Field(None, min_length=3, max_length=3)
    component: Optional[str] = Field(None, max_length=40)
    kind: Optional[str] = Field(None, max_length=25)
    expect: Literal["AMOUNT", "NUMBER", "BOOLEAN", "ANY"] = "AMOUNT"


class TesterIn(StrictModel):
    formula: Optional[str] = Field(None, max_length=4000)
    currency: Optional[str] = Field(None, min_length=3, max_length=3)
    sample: Optional[Dict[str, Any]] = None
    employee_id: Optional[int] = Field(None, ge=1)
    period_start: Optional[date] = None
    period_end: Optional[date] = None


@router.get("/components")
def list_components(kind: Optional[str] = Query(None, max_length=25), include_archived: bool = False,
                    principal: Principal = Depends(require(comp.READ)), db: Session = Depends(get_db)):
    return comp.list_components(db, principal, kind=kind, include_archived=include_archived)


@router.post("/components", status_code=201)
def create_component(data: ComponentIn, request: Request, principal: Principal = Depends(require(comp.WRITE)),
                     db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (201, comp.create_component(
        db, principal, code=data.code, kind=data.kind, category=data.category, data=data.to_input())), body=_dump(data))


@router.post("/components/validate-formula")
def validate_formula(data: FormulaCheckIn, principal: Principal = Depends(require(comp.READ)),
                     db: Session = Depends(get_db)):
    return comp.validate_formula(db, principal, data.formula, currency=data.currency, component=data.component,
                                 kind=data.kind, expect=data.expect)


@router.post("/components/seed")
def seed_components(request: Request, principal: Principal = Depends(require(comp.WRITE)),
                    db: Session = Depends(get_db)):
    def handler():
        comp.require_company_write(principal)
        return 200, {"created": component_seed.seed(db, db.get(Company, principal.company_id))}

    return idempotency.execute(db, principal, request, handler)


@router.get("/components/{code}")
def get_component(code: str, principal: Principal = Depends(require(comp.READ)), db: Session = Depends(get_db)):
    return comp.get_component(db, principal, code)


@router.patch("/components/{code}")
def patch_component(code: str, data: ComponentPatchIn, request: Request,
                    principal: Principal = Depends(require(comp.WRITE)), db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (200, comp.update_component(
        db, principal, code, new_code=data.code, status=data.status)), body=_dump(data))


@router.post("/components/{code}/versions", status_code=201)
def create_component_version(code: str, data: ComponentVersionIn, request: Request,
                             principal: Principal = Depends(require(comp.WRITE)), db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (201, comp.create_version(
        db, principal, code, data.to_input(data.reason))), body=_dump(data))


@router.post("/components/{code}/test")
def test_component(code: str, data: Optional[TesterIn] = Body(None), principal: Principal = Depends(require(comp.READ)),
                   db: Session = Depends(get_db)):
    body = data or TesterIn()
    return comp.run_tester(db, principal, code, formula=body.formula, currency=body.currency, sample=body.sample,
                           employee_id=body.employee_id, period_start=body.period_start, period_end=body.period_end)


# --------------------------------------------------------------------------- structures
class LineIn(StrictModel):
    component: str = Code
    method: Optional[str] = Field(None, max_length=15)
    value: Optional[DecimalStr] = None
    formula: Optional[str] = Field(None, max_length=4000)
    params: Optional[Dict[str, Any]] = None
    overridable: bool = False
    override_min: Optional[DecimalStr] = None
    override_max: Optional[DecimalStr] = None
    display_order: Optional[int] = Field(None, ge=0, le=100000)

    def to_input(self) -> structures.LineInput:
        return structures.LineInput(self.component, self.method, self.value, self.formula, self.params,
                                    self.overridable, self.override_min, self.override_max, self.display_order)


class StructureIn(StrictModel):
    code: str = Code
    name: str = Field(min_length=1, max_length=150)
    type: str = Field(min_length=1, max_length=15)
    description: Optional[str] = Field(None, max_length=1000)
    currency: Optional[str] = Field(None, min_length=3, max_length=3)
    eligibility: Optional[Dict[str, List[int]]] = None
    lines: List[LineIn] = Field(default_factory=list, max_length=structures.MAX_LINES)
    test_range: Optional[Dict[str, Any]] = None
    test_vectors: Optional[List[Dict[str, Any]]] = Field(None, max_length=structures.MAX_VECTORS)


class StructureVersionIn(StrictModel):
    from_version: Optional[int] = Field(None, ge=1)


class StructureUpdateIn(StrictModel):
    row_version: Optional[int] = Field(None, ge=1)
    name: Optional[str] = Field(None, max_length=150)
    description: Optional[str] = Field(None, max_length=1000)
    eligibility: Optional[Dict[str, List[int]]] = None
    lines: Optional[List[LineIn]] = Field(None, max_length=structures.MAX_LINES)
    test_range: Optional[Dict[str, Any]] = None
    test_vectors: Optional[List[Dict[str, Any]]] = Field(None, max_length=structures.MAX_VECTORS)


class OverrideIn(StrictModel):
    amount: Optional[DecimalStr] = None
    percent: Optional[DecimalStr] = None
    formula: Optional[str] = Field(None, max_length=4000)

    @model_validator(mode="after")
    def _one(self):
        if sum(v is not None for v in (self.amount, self.percent, self.formula)) != 1:
            raise ValueError("an override is exactly one of amount, percent or formula")
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in (("amount", self.amount), ("percent", self.percent), ("formula", self.formula))
                if v is not None}


def _overrides(raw: Optional[Dict[str, OverrideIn]]) -> Optional[Dict[str, Dict[str, Any]]]:
    return None if raw is None else {code: item.to_dict() for code, item in raw.items()}


class PreviewIn(StrictModel):
    pay_basis_amount: PayBasisIn
    version: Optional[int] = Field(None, ge=1)
    overrides: Optional[Dict[str, OverrideIn]] = None


class ValidateIn(StrictModel):
    version: Optional[int] = Field(None, ge=1)


class PublishIn(StrictModel):
    version: int = Field(ge=1)
    effective_from: Optional[date] = None


class MoveIn(StrictModel):
    to_version: Optional[int] = Field(None, ge=1)
    effective_from: date
    assignment_ids: Optional[List[int]] = Field(None, max_length=5000)
    dry_run: bool = True
    note: Optional[str] = Field(None, max_length=1000)


@router.get("/salary-structures")
def list_structures(include_archived: bool = False, principal: Principal = Depends(require(structures.READ)),
                    db: Session = Depends(get_db)):
    return structures.list_structures(db, principal, include_archived=include_archived)


@router.post("/salary-structures", status_code=201)
def create_structure(data: StructureIn, request: Request, principal: Principal = Depends(require(structures.EDIT)),
                     db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (201, structures.create_structure(
        db, principal, code=data.code, name=data.name, type_=data.type, description=data.description,
        currency=data.currency, eligibility=data.eligibility, lines=[line.to_input() for line in data.lines],
        test_range=data.test_range, test_vectors=data.test_vectors)), body=_dump(data))


@router.get("/salary-structures/{template_id}")
def get_structure(template_id: int, principal: Principal = Depends(require(structures.READ)),
                  db: Session = Depends(get_db)):
    return structures.get_structure(db, principal, template_id)


@router.post("/salary-structures/{template_id}/versions", status_code=201)
def create_structure_version(template_id: int, request: Request, data: Optional[StructureVersionIn] = Body(None),
                             principal: Principal = Depends(require(structures.EDIT)), db: Session = Depends(get_db)):
    body = data or StructureVersionIn()
    return idempotency.execute(db, principal, request, lambda: (201, structures.create_version(
        db, principal, template_id, from_version=body.from_version)), body=_dump(data))


@router.put("/salary-structures/{template_id}/versions/{version}")
def update_structure_version(template_id: int, version: int, data: StructureUpdateIn, request: Request,
                             principal: Principal = Depends(require(structures.EDIT)), db: Session = Depends(get_db)):
    clear_range = "test_range" in data.model_fields_set and data.test_range is None
    return idempotency.execute(db, principal, request, lambda: (200, structures.update_version(
        db, principal, template_id, version, row_version=data.row_version,
        lines=None if data.lines is None else [line.to_input() for line in data.lines], eligibility=data.eligibility,
        test_range=data.test_range, clear_test_range=clear_range, test_vectors=data.test_vectors, name=data.name,
        description=data.description)), body=_dump(data))


@router.post("/salary-structures/{template_id}/validate")
def validate_structure(template_id: int, data: Optional[ValidateIn] = Body(None),
                       principal: Principal = Depends(require(structures.READ)), db: Session = Depends(get_db)):
    return structures.validate_version(db, principal, template_id, (data or ValidateIn()).version)


@router.post("/salary-structures/{template_id}/preview")
def preview_structure(template_id: int, data: PreviewIn, principal: Principal = Depends(require(structures.READ)),
                      db: Session = Depends(get_db)):
    return structures.preview(db, principal, template_id, pay_basis_amount=data.pay_basis_amount.decimal,
                              number=data.version, overrides=_overrides(data.overrides),
                              currency=data.pay_basis_amount.currency)


@router.post("/salary-structures/{template_id}/publish")
def publish_structure(template_id: int, data: PublishIn, request: Request,
                      principal: Principal = Depends(require_any(structures.EDIT, structures.APPROVE)),
                      db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (200, structures.publish(
        db, principal, template_id, data.version, effective_from=data.effective_from)), body=_dump(data))


@router.post("/salary-structures/{template_id}/move-assignments")
def move_assignments(template_id: int, data: MoveIn, request: Request,
                     principal: Principal = Depends(require(comp_service.EDIT)), db: Session = Depends(get_db)):
    if data.dry_run:
        return structures.move_assignments(db, principal, template_id, to_version=data.to_version,
                                           effective_from=data.effective_from, assignment_ids=data.assignment_ids,
                                           dry_run=True)
    return idempotency.execute(db, principal, request, lambda: (200, structures.move_assignments(
        db, principal, template_id, to_version=data.to_version, effective_from=data.effective_from,
        assignment_ids=data.assignment_ids, dry_run=False, note=data.note)), body=_dump(data))


@router.post("/salary-structures/{template_id}/archive")
def archive_structure(template_id: int, request: Request, principal: Principal = Depends(require(structures.EDIT)),
                      db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request,
                               lambda: (200, structures.archive_structure(db, principal, template_id)))


# --------------------------------------------------------------------------- compensation
class CompensationIn(StrictModel):
    template_version_id: int = Field(ge=1)
    pay_basis_amount: PayBasisIn
    effective_from: date
    effective_to: Optional[date] = None
    reason: Literal["HIRE", "REVISION", "PROMOTION", "TRANSFER", "CORRECTION"]
    overrides: Optional[Dict[str, OverrideIn]] = None
    fx_rule: Optional[Dict[str, Any]] = None
    note: Optional[str] = Field(None, max_length=1000)
    attachments: Optional[List[Dict[str, Any]]] = Field(None, max_length=20)
    submit: bool = True


class ChangePreviewIn(StrictModel):
    pay_basis_amount: PayBasisIn
    effective_from: date
    template_version_id: Optional[int] = Field(None, ge=1)
    overrides: Optional[Dict[str, OverrideIn]] = None


class DecisionIn(StrictModel):
    remarks: Optional[str] = Field(None, max_length=1000)


class ImportIn(StrictModel):
    mode: Literal["dry_run", "commit"]
    csv: Optional[str] = Field(None, max_length=5_000_000)
    file_name: Optional[str] = Field(None, max_length=255)
    import_id: Optional[int] = Field(None, ge=1)


@router.get("/compensation")
def list_compensation(filter: Literal["with", "without", "scheduled", "pending", "draft"] = "with",
                      legal_entity_id: Optional[int] = Query(None, ge=1), q: Optional[str] = Query(None, max_length=100),
                      limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                      principal: Principal = Depends(require(comp_service.READ)), db: Session = Depends(get_db)):
    return comp_service.list_compensation(db, principal, filter_=filter, legal_entity_id=legal_entity_id, search=q,
                                          limit=limit, offset=offset)


@router.get("/compensation/import-template")
def import_template(principal: Principal = Depends(require(comp_service.EDIT))):
    header = ",".join((*comp_service.CSV_COLUMNS, *comp_service.CSV_OPTIONAL))
    return Response(content=header + "\r\n", media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="compensation-import.csv"'})


@router.post("/compensation/import")
def import_compensation(data: ImportIn, request: Request, principal: Principal = Depends(require(comp_service.EDIT)),
                        db: Session = Depends(get_db)):
    if data.mode == "dry_run":
        if not data.csv:
            raise bad_request("Send the CSV text to validate.", "IMPORT_EMPTY")
        return idempotency.execute(db, principal, request, lambda: (200, comp_service.import_dry_run(
            db, principal, data.csv, data.file_name)), body=_dump(data))
    if data.import_id is None:
        raise bad_request("Give the import_id of a validated dry run.", "IMPORT_ID_REQUIRED")
    return idempotency.execute(db, principal, request, lambda: (202, comp_service.import_commit(
        db, principal, data.import_id)), body=_dump(data))


@router.get("/compensation/imports/{import_id}")
def get_import(import_id: int, principal: Principal = Depends(require(comp_service.EDIT)),
               db: Session = Depends(get_db)):
    return comp_service.get_import(db, principal, import_id)


@router.get("/compensation/{assignment_id}")
def get_assignment(assignment_id: int, principal: Principal = Depends(require(comp_service.READ)),
                   db: Session = Depends(get_db)):
    return comp_service.get_assignment(db, principal, assignment_id)


@router.post("/compensation/{assignment_id}/submit")
def submit_assignment(assignment_id: int, request: Request, principal: Principal = Depends(require(comp_service.EDIT)),
                      db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request,
                               lambda: (200, comp_service.submit_assignment(db, principal, assignment_id)))


@router.post("/compensation/{assignment_id}/approve")
def approve_assignment(assignment_id: int, request: Request, data: Optional[DecisionIn] = Body(None),
                       principal: Principal = Depends(require(comp_service.APPROVE)), db: Session = Depends(get_db)):
    remarks = (data or DecisionIn()).remarks
    return idempotency.execute(db, principal, request, lambda: (200, comp_service.decide(
        db, principal, assignment_id, "APPROVE", remarks)), body=_dump(data))


@router.post("/compensation/{assignment_id}/reject")
def reject_assignment(assignment_id: int, data: DecisionIn, request: Request,
                      principal: Principal = Depends(require(comp_service.APPROVE)), db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (200, comp_service.decide(
        db, principal, assignment_id, "REJECT", data.remarks)), body=_dump(data))


@router.get("/employees/{employee_id}/compensation")
def employee_compensation(employee_id: int, principal: Principal = Depends(get_principal),
                          db: Session = Depends(get_db)):
    return comp_service.employee_compensation(db, principal, employee_id)


@router.post("/employees/{employee_id}/compensation", status_code=201)
def assign_compensation(employee_id: int, data: CompensationIn, request: Request,
                        principal: Principal = Depends(require(comp_service.EDIT)), db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (201, comp_service.propose(
        db, principal, employee_id, template_version_id=data.template_version_id,
        pay_basis_amount=data.pay_basis_amount.decimal, currency=data.pay_basis_amount.currency,
        effective_from=data.effective_from, effective_to=data.effective_to, reason=data.reason,
        overrides=_overrides(data.overrides), fx_rule=data.fx_rule, note=data.note, attachments=data.attachments,
        submit=data.submit)), body=_dump(data))


@router.post("/employees/{employee_id}/compensation/preview")
def preview_change(employee_id: int, data: ChangePreviewIn, principal: Principal = Depends(require(comp_service.EDIT)),
                   db: Session = Depends(get_db)):
    return comp_service.preview_change(db, principal, employee_id, pay_basis_amount=data.pay_basis_amount.decimal,
                                       effective_from=data.effective_from, template_version_id=data.template_version_id,
                                       overrides=_overrides(data.overrides))


# --------------------------------------------------------------------------- revision cycles
class CycleIn(StrictModel):
    name: str = Field(min_length=1, max_length=150)
    population: Optional[Dict[str, Any]] = None
    method: Dict[str, Any]
    effective_from: date


class CycleUpdateIn(StrictModel):
    row_version: Optional[int] = Field(None, ge=1)
    name: Optional[str] = Field(None, max_length=150)
    population: Optional[Dict[str, Any]] = None
    method: Optional[Dict[str, Any]] = None
    effective_from: Optional[date] = None


@router.get("/revision-cycles")
def list_cycles(principal: Principal = Depends(require_any(revisions.EDIT, comp_service.APPROVE)),
                db: Session = Depends(get_db)):
    return revisions.list_cycles(db, principal)


@router.post("/revision-cycles", status_code=201)
def create_cycle(data: CycleIn, request: Request, principal: Principal = Depends(require(revisions.EDIT)),
                 db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (201, revisions.create_cycle(
        db, principal, name=data.name, population=data.population, method=data.method,
        effective_from=data.effective_from)), body=_dump(data))


@router.get("/revision-cycles/{cycle_id}")
def get_cycle(cycle_id: int, principal: Principal = Depends(require_any(revisions.EDIT, comp_service.APPROVE)),
              db: Session = Depends(get_db)):
    return revisions.get_cycle(db, principal, cycle_id)


@router.put("/revision-cycles/{cycle_id}")
def update_cycle(cycle_id: int, data: CycleUpdateIn, request: Request,
                 principal: Principal = Depends(require(revisions.EDIT)), db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (200, revisions.update_cycle(
        db, principal, cycle_id, row_version=data.row_version, name=data.name, population=data.population,
        method=data.method, effective_from=data.effective_from)), body=_dump(data))


@router.post("/revision-cycles/{cycle_id}/preview")
def preview_cycle(cycle_id: int, request: Request, principal: Principal = Depends(require(revisions.EDIT)),
                  db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (200, revisions.preview_cycle(db, principal, cycle_id)))


@router.post("/revision-cycles/{cycle_id}/submit")
def submit_cycle(cycle_id: int, request: Request, principal: Principal = Depends(require(revisions.EDIT)),
                 db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (200, revisions.submit_cycle(db, principal, cycle_id)))


@router.post("/revision-cycles/{cycle_id}/approve")
def approve_cycle(cycle_id: int, request: Request, data: Optional[DecisionIn] = Body(None),
                  principal: Principal = Depends(require(comp_service.APPROVE)), db: Session = Depends(get_db)):
    remarks = (data or DecisionIn()).remarks
    return idempotency.execute(db, principal, request, lambda: (200, revisions.decide_cycle(
        db, principal, cycle_id, "APPROVE", remarks)), body=_dump(data))


@router.post("/revision-cycles/{cycle_id}/reject")
def reject_cycle(cycle_id: int, data: DecisionIn, request: Request,
                 principal: Principal = Depends(require(comp_service.APPROVE)), db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (200, revisions.decide_cycle(
        db, principal, cycle_id, "REJECT", data.remarks)), body=_dump(data))


@router.post("/revision-cycles/{cycle_id}/apply")
def apply_cycle(cycle_id: int, request: Request, principal: Principal = Depends(require(revisions.EDIT)),
                db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (200, revisions.apply_cycle(db, principal, cycle_id)))

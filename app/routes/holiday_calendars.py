"""Holiday calendars, rules, years, branch links and criteria assignments (LH-M1 WI-1.9; 11 §6.10, §6.11). Day facts
(§6.13) are served by ``app/routes/calendar_days.py``.

Reads need ``holiday:read`` (calendars are filtered per 04-holiday/01 §8.4); structure writes need ``holiday:manage``
in the calendar's scope (§8.2), backdated changes also ``holiday:publish``. Literal paths precede ``{id:int}`` ones
(MR-6). State-changing requests accept ``Idempotency-Key``; edits take ``If-Match`` (``row_version``).
"""

from datetime import date

Day = date  # a field called ``date`` would shadow the type in later annotations of the same model
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import Field
from sqlalchemy.orm import Session

from app.core import concurrency
from app.core.errors import bad_request, forbidden, not_found
from app.core.principal import Principal, get_principal, require, require_any
from app.database.db import get_db
from app.payroll.services import idempotency
from app.schemas.auth import StrictModel
from app.services import holiday_assignments, holiday_years
from app.services import holiday_calendar_service as service

router = APIRouter(prefix="/api/v1/holiday-calendars", tags=["Holiday calendars"])
assignments_router = APIRouter(prefix="/api/v1/holiday-calendar-assignments", tags=["Holiday calendars"])
branches_router = APIRouter(prefix="/api/v1/branches", tags=["Holiday calendars"])

READ = require_any(*service.READ_CODES)
WRITE = require("holiday:manage")
# Year changes: DRAFT rows need holiday:manage, PUBLISHED years holiday:publish (the services decide per year).
YEAR_WRITE = require_any("holiday:manage", "holiday:publish")
PUBLISH = require("holiday:publish")
Kind = Literal["FIXED_DATE", "NTH_WEEKDAY", "WEEKDAY_RELATIVE", "EASTER_OFFSET", "ANNUAL_CONFIRM", "ONE_OFF", "RRULE"]
HolidayType = Literal["PUBLIC", "COMPANY", "OPTIONAL"]
Portion = Literal["FULL", "FIRST_HALF", "SECOND_HALF"]
Category = Literal["NATIONAL", "REGIONAL", "FESTIVAL", "ELECTION", "COMPANY_EVENT", "SHUTDOWN", "BRIDGE", "EMERGENCY",
                   "BANK", "OTHER"]
ObservedRule = Literal["NONE", "NEAREST_WORKDAY", "NEXT_WORKDAY", "PREVIOUS_WORKDAY"]
Status = Literal["ACTIVE", "INACTIVE"]
Observance = Literal["NONE", "NEXT_WORKING_DAY", "OBSERVED_IF_OFF"]


class CalendarCreate(StrictModel):
    code: Optional[str] = Field(None, max_length=50)
    name: str = Field(..., min_length=1, max_length=150)
    name_i18n: Optional[Dict[str, str]] = None
    description: Optional[str] = Field(None, max_length=2000)
    legal_entity_id: Optional[int] = None
    country_code: Optional[str] = Field(None, max_length=2)
    subdivision_code: Optional[str] = Field(None, max_length=6)
    weekend_days: Optional[List[int]] = Field(None, max_length=3)
    employee_observance: Optional[Observance] = None
    includes: Optional[List[Dict[str, int]]] = Field(None, max_length=50)
    reason: Optional[str] = Field(None, max_length=500)


class CalendarPatch(StrictModel):
    code: Optional[str] = Field(None, max_length=50)
    name: Optional[str] = Field(None, min_length=1, max_length=150)
    name_i18n: Optional[Dict[str, str]] = None
    description: Optional[str] = Field(None, max_length=2000)
    legal_entity_id: Optional[int] = None
    country_code: Optional[str] = Field(None, max_length=2)
    subdivision_code: Optional[str] = Field(None, max_length=6)
    weekend_days: Optional[List[int]] = Field(None, max_length=3)
    employee_observance: Optional[Observance] = None
    status: Optional[Status] = None
    row_version: Optional[int] = Field(None, ge=1)


class IncludeItem(StrictModel):
    included_calendar_id: int
    priority: int = Field(..., ge=0, le=999)


class IncludesIn(StrictModel):
    includes: List[IncludeItem] = Field(..., max_length=50)
    reason: Optional[str] = Field(None, max_length=500)
    row_version: Optional[int] = Field(None, ge=1)


class ExclusionIn(StrictModel):
    holiday_id: Optional[int] = None
    holiday_rule_id: Optional[int] = None
    effective_from_year: Optional[int] = Field(None, ge=2000, le=2100)
    reason: str = Field(..., min_length=1, max_length=500)


class OccurrenceIn(StrictModel):
    name: str = Field(..., min_length=1, max_length=150)
    date: Day
    holiday_type: HolidayType = "PUBLIC"
    category: Optional[Category] = None
    portion: Portion = "FULL"
    status: Literal["CONFIRMED", "PROVISIONAL"] = "CONFIRMED"
    duration_days: int = Field(1, ge=1, le=31)
    description: Optional[str] = Field(None, max_length=500)
    legal_ref: Optional[str] = Field(None, max_length=100)
    issued_on: Optional[Day] = None
    reason: Optional[str] = Field(None, max_length=500)
    dry_run: bool = False
    acknowledged_warnings: List[str] = Field(default_factory=list, max_length=20)


class ClosureIn(StrictModel):
    """An emergency / backdated closure (01 §5.8): today or a past date inside ``holiday.backdate_days``."""

    date: Day
    name: str = Field("Emergency closure", min_length=1, max_length=150)
    portion: Portion = "FULL"
    holiday_type: Literal["PUBLIC", "COMPANY"] = "COMPANY"
    category: Optional[Category] = "EMERGENCY"
    description: Optional[str] = Field(None, max_length=500)
    reason: str = Field(..., min_length=1, max_length=500)
    dry_run: bool = False
    confirm_date: Optional[str] = Field(None, max_length=10)
    acknowledged_warnings: List[str] = Field(default_factory=list, max_length=20)


class ImportDecision(StrictModel):
    line: int = Field(..., ge=1)
    action: Literal["ACCEPT", "SKIP"]


class ImportCommitIn(StrictModel):
    decisions: List[ImportDecision] = Field(default_factory=list, max_length=5000)
    reason: Optional[str] = Field(None, max_length=500)
    target_hash: Optional[str] = Field(None, max_length=64)


class RolloverGenerateIn(StrictModel):
    year: int = Field(..., ge=2000, le=2100)


class RuleIn(StrictModel):
    name: str = Field(..., min_length=1, max_length=150)
    name_i18n: Optional[Dict[str, str]] = None
    holiday_type: HolidayType = "COMPANY"
    category: Category = "OTHER"
    kind: Kind
    params: Dict[str, Any]
    portion: Portion = "FULL"
    duration_days: int = Field(1, ge=1, le=31)
    duration_range: Optional[List[int]] = Field(None, min_length=2, max_length=2)
    observed_rule: ObservedRule = "NONE"
    applies_on: Optional[List[int]] = Field(None, max_length=7)
    valid_from_year: Optional[int] = Field(None, ge=2000, le=2100)
    valid_to_year: Optional[int] = Field(None, ge=2000, le=2100)
    source: Literal["MANUAL", "PACK", "IMPORT", "GOVT_NOTICE"] = "MANUAL"
    legal_ref: Optional[str] = Field(None, max_length=100)
    issued_on: Optional[date] = None
    apply_to_draft_years: bool = True


class RulePatch(StrictModel):
    name: Optional[str] = Field(None, min_length=1, max_length=150)
    name_i18n: Optional[Dict[str, str]] = None
    holiday_type: Optional[HolidayType] = None
    category: Optional[Category] = None
    kind: Optional[Kind] = None
    params: Optional[Dict[str, Any]] = None
    portion: Optional[Portion] = None
    duration_days: Optional[int] = Field(None, ge=1, le=31)
    duration_range: Optional[List[int]] = Field(None, min_length=2, max_length=2)
    observed_rule: Optional[ObservedRule] = None
    applies_on: Optional[List[int]] = Field(None, max_length=7)
    valid_from_year: Optional[int] = Field(None, ge=2000, le=2100)
    valid_to_year: Optional[int] = Field(None, ge=2000, le=2100)
    source: Optional[Literal["MANUAL", "PACK", "IMPORT", "GOVT_NOTICE"]] = None
    legal_ref: Optional[str] = Field(None, max_length=100)
    issued_on: Optional[date] = None
    apply_to_draft_years: bool = True
    row_version: Optional[int] = Field(None, ge=1)


class RulePreviewIn(StrictModel):
    calendar_id: int
    rule_id: Optional[int] = None
    rule: Dict[str, Any]
    years: int = Field(5, ge=1, le=10)
    from_year: Optional[int] = Field(None, ge=2000, le=2100)


class DeactivateIn(StrictModel):
    reason: Optional[str] = Field(None, max_length=500)
    apply_to_draft_years: bool = True
    row_version: Optional[int] = Field(None, ge=1)


class ExpectedDateIn(StrictModel):
    year: int = Field(..., ge=2000, le=2100)
    date: Optional[Day] = None


class PublishIn(StrictModel):
    dry_run: bool = False
    acknowledged_warnings: List[str] = Field(default_factory=list, max_length=20)
    note: Optional[str] = Field(None, max_length=500)
    reason: Optional[str] = Field(None, max_length=500)
    content_hash: Optional[str] = Field(None, min_length=64, max_length=64)


class AmendIn(StrictModel):
    operations: List[Dict[str, Any]] = Field(default_factory=list, max_length=200)
    reason: Optional[str] = Field(None, max_length=500)
    dry_run: bool = False
    acknowledged_warnings: List[str] = Field(default_factory=list, max_length=20)
    content_hash: Optional[str] = Field(None, min_length=64, max_length=64)
    complete_request: bool = False


class DeclineIn(StrictModel):
    reason: Optional[str] = Field(None, max_length=500)


class OccurrenceAction(StrictModel):
    date: Optional[Day] = None
    new_date: Optional[Day] = None
    reason: Optional[str] = Field(None, max_length=500)
    dry_run: bool = False
    acknowledged_warnings: List[str] = Field(default_factory=list, max_length=20)
    row_version: Optional[int] = Field(None, ge=1)


class DraftRowPatch(StrictModel):
    name: Optional[str] = Field(None, min_length=1, max_length=150)
    name_i18n: Optional[Dict[str, str]] = None
    date: Optional[Day] = None
    holiday_type: Optional[HolidayType] = None
    category: Optional[Category] = None
    portion: Optional[Portion] = None
    description: Optional[str] = Field(None, max_length=500)
    action: Optional[Literal["SKIP", "RESET", "RESTORE"]] = None
    expected_date: Optional[Day] = None
    row_version: Optional[int] = Field(None, ge=1)


class WorkingDayIn(StrictModel):
    date: Day
    name: str = Field(..., min_length=1, max_length=150)
    portion: Portion = "FULL"
    reason: str = Field(..., min_length=1, max_length=300)
    dry_run: bool = False
    acknowledged_warnings: List[str] = Field(default_factory=list, max_length=20)


class AssignmentIn(StrictModel):
    calendar_id: Optional[int] = None
    work_week_pattern_id: Optional[int] = None
    criteria: Dict[str, Any]
    priority: int = Field(100, ge=1, le=999)
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None
    legal_entity_id: Optional[int] = None
    reason: Optional[str] = Field(None, max_length=500)


class AssignmentPatch(StrictModel):
    calendar_id: Optional[int] = None
    work_week_pattern_id: Optional[int] = None
    criteria: Optional[Dict[str, Any]] = None
    priority: Optional[int] = Field(None, ge=1, le=999)
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None
    reason: Optional[str] = Field(None, max_length=500)
    row_version: Optional[int] = Field(None, ge=1)


class BranchLinkIn(StrictModel):
    holiday_calendar_id: Optional[int] = None
    work_week_pattern_id: Optional[int] = None
    effective_from: Optional[date] = None
    reason: Optional[str] = Field(None, max_length=300)
    row_version: Optional[int] = Field(None, ge=1)


def _dump(data: StrictModel) -> dict:
    return data.model_dump(mode="json", exclude_unset=True)


def _check_year(*years: int) -> None:
    if not all(2000 <= y <= 2100 for y in years):
        raise bad_request("The year must be between 2000 and 2100.", "INVALID_YEAR")


# ============================================================================================== Next year (literal paths)
@router.get("/rollover")
def rollover(year: Optional[int] = Query(None, ge=2000, le=2100), principal: Principal = Depends(YEAR_WRITE),
             db: Session = Depends(get_db)):
    """Next year page (02 §8.5, SPINE V3-096): calendars in scope with the year's status and counts."""
    from app.services import holiday_rollover

    return holiday_rollover.overview(db, principal, year)


@router.post("/rollover/generate")
def rollover_generate(data: RolloverGenerateIn, request: Request, principal: Principal = Depends(WRITE),
                      db: Session = Depends(get_db)):
    """"Generate drafts now": the calendars in scope that lack a draft (or failed); never publishes."""
    from app.services import holiday_rollover

    return idempotency.execute(db, principal, request,
                               lambda: (200, holiday_rollover.generate_missing(db, principal, data.year)),
                               body=_dump(data))


# ============================================================================================== imports (literal paths)
@router.get("/imports/templates/{kind}", response_class=PlainTextResponse)
def import_template(kind: Literal["CSV_DATED", "CSV_RULES"], year: Optional[int] = Query(None, ge=2000, le=2100),
                    principal: Principal = Depends(WRITE), db: Session = Depends(get_db)):
    from app.core import clock
    from app.services import holiday_imports

    body = holiday_imports.template(kind, year or clock.company_today(db, principal.company_id).year + 1)
    name = "holidays-dates.csv" if kind == "CSV_DATED" else "holidays-repeating.csv"
    return PlainTextResponse(body, media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/imports/{import_id:int}")
def get_import(import_id: int, principal: Principal = Depends(WRITE), db: Session = Depends(get_db)):
    from app.services import holiday_imports

    return holiday_imports.import_json(holiday_imports.get_import(db, principal, import_id))


@router.get("/imports/{import_id:int}/errors.csv", response_class=PlainTextResponse)
def import_errors(import_id: int, principal: Principal = Depends(WRITE), db: Session = Depends(get_db)):
    from app.services import holiday_imports

    row = holiday_imports.get_import(db, principal, import_id)
    return PlainTextResponse(holiday_imports.errors_csv(row), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="import-{row.id}-errors.csv"'})


@router.post("/imports/{import_id:int}/commit")
def commit_import(import_id: int, data: ImportCommitIn, request: Request, principal: Principal = Depends(WRITE),
                  db: Session = Depends(get_db)):
    """Apply the accepted lines (02 §9.1 step 4): If-Match / ``target_hash`` = the report's year content."""
    from app.services import holiday_imports

    seen = data.target_hash or (request.headers.get("If-Match") or "").strip('"') or None
    return idempotency.execute(db, principal, request, lambda: holiday_imports.commit(
        db, principal, import_id, decisions=[d.model_dump() for d in data.decisions], reason=data.reason,
        target_hash=seen), body=_dump(data))


# ============================================================================================== rules (literal paths first)
@router.post("/rules/preview")
def preview_rule(data: RulePreviewIn, principal: Principal = Depends(YEAR_WRITE), db: Session = Depends(get_db)):
    """5-year (1-10) preview of an unsaved or edited rule with the calendar's other rules (02 §3.7); no writes."""
    return holiday_years.preview_rule(db, principal, data.model_dump(mode="python"))


@router.get("/rules/{rule_id:int}")
def get_rule(rule_id: int, principal: Principal = Depends(READ), db: Session = Depends(get_db)):
    return holiday_years.get_rule(db, principal, rule_id)


@router.patch("/rules/{rule_id:int}")
def update_rule(rule_id: int, data: RulePatch, request: Request, principal: Principal = Depends(WRITE),
                db: Session = Depends(get_db)):
    expected = concurrency.if_match(request, data.row_version)
    values = data.model_dump(mode="python", exclude_unset=True, exclude={"row_version"})
    if values.get("issued_on") is not None:
        values["issued_on"] = values["issued_on"].isoformat()
    return idempotency.execute(
        db, principal, request,
        lambda: (200, holiday_years.update_rule(db, principal, rule_id, values, expected_row_version=expected)),
        body=_dump(data))


@router.post("/rules/{rule_id:int}/deactivate")
def deactivate_rule(rule_id: int, data: DeactivateIn, request: Request, principal: Principal = Depends(WRITE),
                    db: Session = Depends(get_db)):
    expected = concurrency.if_match(request, data.row_version)
    return idempotency.execute(
        db, principal, request,
        lambda: (200, holiday_years.deactivate_rule(db, principal, rule_id, reason=data.reason,
                                                    apply_to_draft_years=data.apply_to_draft_years,
                                                    expected_row_version=expected)),
        body=_dump(data))


@router.put("/rules/{rule_id:int}/expected-date")
def set_expected_date(rule_id: int, data: ExpectedDateIn, request: Request, principal: Principal = Depends(WRITE),
                      db: Session = Depends(get_db)):
    """The tenant's expected start of an announced holiday for one year (02 §6.2 priority TENANT)."""
    return idempotency.execute(
        db, principal, request,
        lambda: (200, holiday_years.set_expected_date(db, principal, rule_id, data.year, data.date)),
        body=_dump(data))


# ============================================================================================== occurrences (literal)
@router.patch("/occurrences/{holiday_id:int}")
def edit_draft_row(holiday_id: int, data: DraftRowPatch, request: Request, principal: Principal = Depends(WRITE),
                   db: Session = Depends(get_db)):
    """DRAFT-year row: edit fields, SKIP / RESTORE this year, RESET to the rule, set the expected date (02 §4.5)."""
    expected = concurrency.if_match(request, data.row_version)
    values = data.model_dump(mode="python", exclude_unset=True, exclude={"row_version"})
    return idempotency.execute(
        db, principal, request,
        lambda: (200, holiday_years.edit_draft_row(db, principal, holiday_id, values,
                                                   expected_row_version=expected)),
        body=_dump(data))


@router.post("/occurrences/{holiday_id:int}/confirm")
def confirm_occurrence(holiday_id: int, data: OccurrenceAction, request: Request,
                       principal: Principal = Depends(YEAR_WRITE), db: Session = Depends(get_db)):
    expected = concurrency.if_match(request, data.row_version)
    return idempotency.execute(
        db, principal, request,
        lambda: holiday_years.confirm_occurrence(db, principal, holiday_id, date_=data.date, reason=data.reason,
                                                 dry_run=data.dry_run,
                                                 acknowledged_warnings=data.acknowledged_warnings,
                                                 expected_row_version=expected),
        body=_dump(data))


@router.post("/occurrences/{holiday_id:int}/cancel")
def cancel_occurrence(holiday_id: int, data: OccurrenceAction, request: Request,
                      principal: Principal = Depends(YEAR_WRITE), db: Session = Depends(get_db)):
    expected = concurrency.if_match(request, data.row_version)
    return idempotency.execute(
        db, principal, request,
        lambda: holiday_years.cancel_occurrence(db, principal, holiday_id, reason=data.reason, dry_run=data.dry_run,
                                                acknowledged_warnings=data.acknowledged_warnings,
                                                expected_row_version=expected),
        body=_dump(data))


@router.post("/occurrences/{holiday_id:int}/move")
def move_occurrence(holiday_id: int, data: OccurrenceAction, request: Request,
                    principal: Principal = Depends(YEAR_WRITE), db: Session = Depends(get_db)):
    if data.new_date is None:
        raise bad_request("new_date is required.", "VALIDATION_ERROR")
    expected = concurrency.if_match(request, data.row_version)
    return idempotency.execute(
        db, principal, request,
        lambda: holiday_years.move_occurrence(db, principal, holiday_id, to_date=data.new_date, reason=data.reason,
                                              dry_run=data.dry_run, acknowledged_warnings=data.acknowledged_warnings,
                                              expected_row_version=expected),
        body=_dump(data))


# ============================================================================================== calendars
@router.get("")
def list_calendars(q: Optional[str] = Query(None, max_length=100), status: Optional[Status] = None,
                   legal_entity_id: Optional[int] = None, owner: Optional[Literal["TENANT"]] = None,
                   country_code: Optional[str] = Query(None, max_length=2), skip: int = Query(0, ge=0),
                   limit: int = Query(50, ge=1, le=200), principal: Principal = Depends(READ),
                   db: Session = Depends(get_db)):
    return service.list_calendars(db, principal, q=q, status=status, legal_entity_id=legal_entity_id, owner=owner,
                                  country_code=country_code, skip=skip, limit=limit)


@router.post("", status_code=201)
def create_calendar(data: CalendarCreate, request: Request, principal: Principal = Depends(WRITE),
                    db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request,
                               lambda: (201, service.create_calendar(db, principal, data.model_dump(exclude_unset=True))),
                               body=_dump(data))


@router.get("/{calendar_id:int}")
def get_calendar(calendar_id: int, principal: Principal = Depends(READ), db: Session = Depends(get_db)):
    return service.get_calendar(db, principal, calendar_id)


@router.patch("/{calendar_id:int}")
def update_calendar(calendar_id: int, data: CalendarPatch, request: Request, principal: Principal = Depends(WRITE),
                    db: Session = Depends(get_db)):
    expected = concurrency.if_match(request, data.row_version)
    values = data.model_dump(exclude_unset=True, exclude={"row_version"})
    return idempotency.execute(
        db, principal, request,
        lambda: (200, service.update_calendar(db, principal, calendar_id, values, expected_row_version=expected)),
        body=_dump(data))


@router.get("/{calendar_id:int}/lineage")
def lineage(calendar_id: int, year: Optional[int] = Query(None, ge=2000, le=2100), principal: Principal = Depends(READ),
            db: Session = Depends(get_db)):
    return service.lineage(db, principal, calendar_id, year)


@router.get("/{calendar_id:int}/holidays")
def calendar_holidays(calendar_id: int, year: int = Query(..., ge=2000, le=2100), principal: Principal = Depends(READ),
                      db: Session = Depends(get_db)):
    return service.calendar_holidays(db, principal, calendar_id, year)


@router.put("/{calendar_id:int}/includes")
def set_includes(calendar_id: int, data: IncludesIn, request: Request, principal: Principal = Depends(WRITE),
                 db: Session = Depends(get_db)):
    expected = concurrency.if_match(request, data.row_version)
    return idempotency.execute(
        db, principal, request,
        lambda: (200, service.set_includes(db, principal, calendar_id, [i.model_dump() for i in data.includes],
                                           reason=data.reason, expected_row_version=expected)),
        body=_dump(data))


@router.get("/{calendar_id:int}/exclusions")
def list_exclusions(calendar_id: int, principal: Principal = Depends(READ), db: Session = Depends(get_db)):
    return {"items": service.lineage(db, principal, calendar_id)["exclusions"]}


@router.post("/{calendar_id:int}/exclusions", status_code=201)
def add_exclusion(calendar_id: int, data: ExclusionIn, request: Request, principal: Principal = Depends(WRITE),
                  db: Session = Depends(get_db)):
    return idempotency.execute(
        db, principal, request,
        lambda: (201, service.add_exclusion(db, principal, calendar_id, data.model_dump(exclude_unset=True))),
        body=_dump(data))


@router.delete("/{calendar_id:int}/exclusions/{exclusion_id:int}")
def remove_exclusion(calendar_id: int, exclusion_id: int, request: Request,
                     reason: Optional[str] = Query(None, max_length=500), principal: Principal = Depends(WRITE),
                     db: Session = Depends(get_db)):
    return idempotency.execute(
        db, principal, request,
        lambda: (200, service.remove_exclusion(db, principal, calendar_id, exclusion_id, reason)),
        body={"calendar_id": calendar_id, "exclusion_id": exclusion_id, "reason": reason})


@router.post("/{calendar_id:int}/occurrences", status_code=201)
def add_occurrence(calendar_id: int, data: OccurrenceIn, request: Request, principal: Principal = Depends(YEAR_WRITE),
                   db: Session = Depends(get_db)):
    """A DRAFT year gets the row directly (201); a PUBLISHED year is amended (200, or 202 for a second person)."""
    values = data.model_dump(mode="python")
    if values.get("issued_on"):
        values["issued_on"] = values["issued_on"].isoformat()
    return idempotency.execute(db, principal, request,
                               lambda: holiday_years.add_occurrence(db, principal, calendar_id, values),
                               body=_dump(data))


@router.post("/{calendar_id:int}/closures")
def record_closure(calendar_id: int, data: ClosureIn, request: Request, principal: Principal = Depends(PUBLISH),
                   db: Session = Depends(get_db)):
    """Emergency / backdated closure (01 §5.8; LH-HOL-24): ``dry_run`` returns the impact on that date (shifts,
    attendance, leave, pay periods); the commit amends the published year (audit BACKDATED_CLOSURE), recounts leave
    and flags payroll (locked: change detection; finalized: retro)."""
    values = data.model_dump(mode="python")
    values["date"] = values["date"].isoformat()
    if data.dry_run:
        return service.backdated_closure(db, principal, calendar_id, values)[1]
    return idempotency.execute(db, principal, request,
                               lambda: service.backdated_closure(db, principal, calendar_id, values),
                               body=_dump(data))


@router.get("/{calendar_id:int}/imports")
def list_imports(calendar_id: int, principal: Principal = Depends(WRITE), db: Session = Depends(get_db)):
    from app.services import holiday_imports

    return {"items": holiday_imports.recent(db, principal, calendar_id)}


@router.post("/{calendar_id:int}/imports", status_code=201)
async def create_import(calendar_id: int, kind: str = Form(...), target_year: int = Form(..., ge=2000, le=2100),
                        file: UploadFile = File(...), principal: Principal = Depends(WRITE),
                        db: Session = Depends(get_db)):
    """Upload a CSV / ICS file and check it (dry run, 02 §9.1 steps 1-2); nothing is written but the report."""
    from app.services import holiday_imports

    data = await file.read(holiday_imports.MAX_IMPORT_BYTES + 1)
    result = holiday_imports.dry_run(db, principal, calendar_id, kind=kind.upper(), target_year=target_year,
                                     data=data, file_name=file.filename)
    db.commit()
    return result


@router.post("/{calendar_id:int}/imports/{import_id:int}/commit")
def commit_calendar_import(calendar_id: int, import_id: int, data: ImportCommitIn, request: Request,
                           principal: Principal = Depends(WRITE), db: Session = Depends(get_db)):
    from app.services import holiday_imports

    seen = data.target_hash or (request.headers.get("If-Match") or "").strip('"') or None
    return idempotency.execute(db, principal, request, lambda: holiday_imports.commit(
        db, principal, import_id, decisions=[d.model_dump() for d in data.decisions], reason=data.reason,
        target_hash=seen, calendar_id=calendar_id), body=_dump(data))


# ============================================================================================== rules of a calendar
@router.get("/{calendar_id:int}/rules")
def list_rules(calendar_id: int, status: Optional[Status] = None, principal: Principal = Depends(READ),
               db: Session = Depends(get_db)):
    return holiday_years.list_rules(db, principal, calendar_id, status)


@router.post("/{calendar_id:int}/rules", status_code=201)
def create_rule(calendar_id: int, data: RuleIn, request: Request, principal: Principal = Depends(WRITE),
                db: Session = Depends(get_db)):
    values = data.model_dump(mode="python")
    if values.get("issued_on"):
        values["issued_on"] = values["issued_on"].isoformat()
    if values.get("valid_from_year") is None:
        values.pop("valid_from_year")
    return idempotency.execute(db, principal, request,
                               lambda: (201, holiday_years.create_rule(db, principal, calendar_id, values)),
                               body=_dump(data))


# ============================================================================================== years
@router.get("/{calendar_id:int}/years/{year:int}")
def get_year(calendar_id: int, year: int, principal: Principal = Depends(READ), db: Session = Depends(get_db)):
    _check_year(year)
    return holiday_years.get_year(db, principal, calendar_id, year)


@router.get("/{calendar_id:int}/years/{year:int}/diff")
def year_diff(calendar_id: int, year: int, against: Optional[int] = Query(None, ge=2000, le=2100),
              principal: Principal = Depends(READ), db: Session = Depends(get_db)):
    """The year against another (default the previous): NEW / SAME / MOVED / GONE lines (02 §8.6)."""
    from app.services import holiday_rollover

    other = against or year - 1
    _check_year(year, other)
    mine = holiday_years.get_year(db, principal, calendar_id, year)["items"]
    theirs = holiday_years.get_year(db, principal, calendar_id, other)["items"]
    own = [i for i in mine if i["own"]]
    prev = [i for i in theirs if i["own"]]
    return {"year": year, "against": other, "lines": holiday_rollover.diff(own, prev)}


@router.post("/{calendar_id:int}/years/{year:int}/generate")
def generate_year(calendar_id: int, year: int, request: Request, principal: Principal = Depends(WRITE),
                  db: Session = Depends(get_db)):
    _check_year(year)
    return idempotency.execute(db, principal, request,
                               lambda: (200, holiday_years.generate_year(db, principal, calendar_id, year)),
                               body={"calendar_id": calendar_id, "year": year})


@router.delete("/{calendar_id:int}/years/{year:int}")
def delete_draft_year(calendar_id: int, year: int, request: Request, principal: Principal = Depends(WRITE),
                      db: Session = Depends(get_db)):
    _check_year(year)
    return idempotency.execute(db, principal, request,
                               lambda: (200, holiday_years.delete_draft_year(db, principal, calendar_id, year)),
                               body={"calendar_id": calendar_id, "year": year})


@router.post("/{calendar_id:int}/years/{year:int}/publish")
def publish_year(calendar_id: int, year: int, data: PublishIn, request: Request,
                 principal: Principal = Depends(PUBLISH), db: Session = Depends(get_db)):
    """Dry run -> impact; commit -> 200 PUBLISHED, or 202 with a pending second-person request (01 §6.2)."""
    _check_year(year)
    return idempotency.execute(
        db, principal, request,
        lambda: holiday_years.publish_year(db, principal, calendar_id, year, dry_run=data.dry_run,
                                           acknowledged_warnings=data.acknowledged_warnings,
                                           note=data.note, reason=data.reason,
                                           content_hash_seen=data.content_hash),
        body=_dump(data))


@router.post("/{calendar_id:int}/years/{year:int}/amend")
def amend_year(calendar_id: int, year: int, data: AmendIn, request: Request, principal: Principal = Depends(PUBLISH),
               db: Session = Depends(get_db)):
    """Bulk amendment of a PUBLISHED year (01 §6.4): reason, date bands, one version; 202 for a second person."""
    _check_year(year)
    return idempotency.execute(
        db, principal, request,
        lambda: holiday_years.amend_year(db, principal, calendar_id, year, data.operations, reason=data.reason,
                                         dry_run=data.dry_run, acknowledged_warnings=data.acknowledged_warnings,
                                         content_hash_seen=data.content_hash, complete_request=data.complete_request),
        body=_dump(data))


class ApplyRulesIn(StrictModel):
    reason: Optional[str] = Field(None, max_length=500)
    dry_run: bool = False
    acknowledged_warnings: List[str] = Field(default_factory=list, max_length=20)


@router.post("/{calendar_id:int}/years/{year:int}/apply-rules")
def apply_rules(calendar_id: int, year: int, data: ApplyRulesIn, request: Request,
                principal: Principal = Depends(PUBLISH), db: Session = Depends(get_db)):
    """Apply the rules' output to a PUBLISHED year as one amendment ("Review and apply to <year>…", 02 §4.6)."""
    _check_year(year)
    return idempotency.execute(
        db, principal, request,
        lambda: holiday_years.apply_rules(db, principal, calendar_id, year, reason=data.reason, dry_run=data.dry_run,
                                          acknowledged_warnings=data.acknowledged_warnings),
        body=_dump(data))


@router.post("/{calendar_id:int}/years/{year:int}/request/decline")
def decline_request(calendar_id: int, year: int, data: DeclineIn, request: Request,
                    principal: Principal = Depends(PUBLISH), db: Session = Depends(get_db)):
    return idempotency.execute(
        db, principal, request,
        lambda: (200, holiday_years.decline_request(db, principal, calendar_id, year, data.reason)),
        body=_dump(data))


# ============================================================================================== working days, history
@router.post("/{calendar_id:int}/working-days", status_code=201)
def declare_working_day(calendar_id: int, data: WorkingDayIn, request: Request,
                        principal: Principal = Depends(YEAR_WRITE), db: Session = Depends(get_db)):
    values = data.model_dump(mode="json")
    return idempotency.execute(db, principal, request,
                               lambda: holiday_years.declare_working_day(db, principal, calendar_id, values),
                               body=_dump(data))


@router.delete("/{calendar_id:int}/working-days/{working_day_id:int}")
def delete_working_day(calendar_id: int, working_day_id: int, request: Request,
                       reason: Optional[str] = Query(None, max_length=300),
                       principal: Principal = Depends(YEAR_WRITE), db: Session = Depends(get_db)):
    return idempotency.execute(
        db, principal, request,
        lambda: holiday_years.delete_working_day(db, principal, calendar_id, working_day_id, reason=reason),
        body={"calendar_id": calendar_id, "working_day_id": working_day_id, "reason": reason})


@router.get("/{calendar_id:int}/history")
def calendar_history(calendar_id: int, principal: Principal = Depends(READ), db: Session = Depends(get_db)):
    return holiday_years.history(db, principal, calendar_id)


@router.get("/{calendar_id:int}/assignments")
def calendar_assignments(calendar_id: int, principal: Principal = Depends(READ), db: Session = Depends(get_db)):
    """The Assignments tab: effective-dated branch links, criteria rows and employee overrides of this calendar."""
    from app.models.org import Branch, EmployeeAssignment
    from app.models.pay_org import BranchCalendarLink

    row = service._get_visible(db, principal, calendar_id)
    cid = principal.company_id
    branches = {b.id: b for b in db.query(Branch).filter(Branch.company_id == cid)}
    links = (db.query(BranchCalendarLink).filter(BranchCalendarLink.company_id == cid,
                                                 BranchCalendarLink.holiday_calendar_id == row.id)
             .order_by(BranchCalendarLink.branch_id, BranchCalendarLink.effective_from).all())
    linked = {l.branch_id for l in db.query(BranchCalendarLink.branch_id).filter(BranchCalendarLink.company_id == cid)}
    legacy = [b for b in branches.values() if b.holiday_calendar_id == row.id and b.id not in linked]
    overrides = (db.query(EmployeeAssignment).filter(EmployeeAssignment.company_id == cid,
                                                     EmployeeAssignment.holiday_calendar_id == row.id)
                 .order_by(EmployeeAssignment.effective_from).all())
    return {
        "calendar_id": row.id,
        "branch_links": [{**holiday_assignments.serialize_link(l), "branch_code": branches[l.branch_id].code,
                          "branch_name": branches[l.branch_id].name} for l in links]
        + [{"id": None, "branch_id": b.id, "branch_code": b.code, "branch_name": b.name, "holiday_calendar_id": row.id,
            "work_week_pattern_id": b.work_week_pattern_id, "effective_from": None, "effective_to": None,
            "reason": None, "row_version": None} for b in legacy],
        "criteria": holiday_assignments.list_assignments(db, principal, calendar_id=row.id),
        "overrides": [{"assignment_id": a.id, "employee_id": a.employee_id,
                       "effective_from": a.effective_from.isoformat(),
                       "effective_to": a.effective_to.isoformat() if a.effective_to else None} for a in overrides],
    }


# ============================================================================================== criteria assignments
@assignments_router.get("")
def list_assignments(calendar_id: Optional[int] = None, work_week_pattern_id: Optional[int] = None,
                     principal: Principal = Depends(READ), db: Session = Depends(get_db)):
    return {"items": holiday_assignments.list_assignments(db, principal, calendar_id=calendar_id,
                                                          work_week_pattern_id=work_week_pattern_id)}


@assignments_router.post("", status_code=201)
def create_assignment(data: AssignmentIn, request: Request, principal: Principal = Depends(WRITE),
                      db: Session = Depends(get_db)):
    return idempotency.execute(
        db, principal, request,
        lambda: (201, holiday_assignments.save_assignment(db, principal, data.model_dump(exclude_unset=True))),
        body=_dump(data))


@assignments_router.patch("/{assignment_id:int}")
def update_assignment(assignment_id: int, data: AssignmentPatch, request: Request,
                      principal: Principal = Depends(WRITE), db: Session = Depends(get_db)):
    expected = concurrency.if_match(request, data.row_version)
    values = data.model_dump(exclude_unset=True, exclude={"row_version"})
    return idempotency.execute(
        db, principal, request,
        lambda: (200, holiday_assignments.save_assignment(db, principal, values, assignment_id,
                                                          expected_row_version=expected)),
        body=_dump(data))


# ============================================================================================== branch links
@branches_router.get("/{branch_id:int}/calendar-links")
def list_branch_links(branch_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    from app.models.org import Branch
    from app.models.pay_org import BranchCalendarLink

    branch = db.query(Branch).filter(Branch.company_id == principal.company_id, Branch.id == branch_id).first()
    if branch is None or not service.can_read(principal):
        raise not_found("Branch")
    rows = (db.query(BranchCalendarLink).filter(BranchCalendarLink.company_id == principal.company_id,
                                                BranchCalendarLink.branch_id == branch.id)
            .order_by(BranchCalendarLink.effective_from).all())
    return {"items": [holiday_assignments.serialize_link(r) for r in rows],
            "current": {"holiday_calendar_id": branch.holiday_calendar_id,
                        "work_week_pattern_id": branch.work_week_pattern_id},
            "local_today": holiday_assignments.branch_local_today(db, branch).isoformat()}


@branches_router.put("/{branch_id:int}/calendar-link")
def set_branch_link(branch_id: int, data: BranchLinkIn, request: Request, principal: Principal = Depends(WRITE),
                    db: Session = Depends(get_db)):
    expected = concurrency.if_match(request, data.row_version)
    values = data.model_dump(exclude_unset=True, exclude={"row_version"})
    return idempotency.execute(
        db, principal, request,
        lambda: (200, holiday_assignments.set_branch_link(db, principal, branch_id, values,
                                                          expected_row_version=expected)),
        body=_dump(data))

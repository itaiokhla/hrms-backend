"""Request bodies of rotations and rosters (AS 11 §6.4-§6.5; 03-shifts/02 §2-§5)."""

from datetime import date
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field

from app.schemas.auth import StrictModel

HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class PeriodIn(StrictModel):
    code: str = Field(..., min_length=1, max_length=40)
    name: str = Field(..., min_length=1, max_length=100)
    branch_id: Optional[int] = None
    team_id: Optional[int] = None  # X-M1: a period owned by a team (CREW / SITE), its members are the people planned
    department_ids: Optional[List[int]] = Field(None, max_length=50)
    start_date: date
    end_date: date
    publish_notice_days: Optional[int] = Field(None, ge=0, le=60)


class PeriodStatusIn(StrictModel):
    row_version: Optional[int] = Field(None, ge=1)


class DraftCellIn(StrictModel):
    employee_id: int
    shift_date: date
    entry_seq: int = Field(1, ge=1, le=3)
    action: Literal["UPSERT", "DELETE"] = "UPSERT"
    kind: Optional[Literal["WORK", "OFF"]] = None
    shift_id: Optional[int] = None
    custom_start_time: Optional[str] = Field(None, pattern=HHMM)
    custom_end_time: Optional[str] = Field(None, pattern=HHMM)
    site_id: Optional[int] = None
    planned_work_mode: Optional[Literal["ON_SITE", "REMOTE", "FIELD"]] = None
    source: Optional[Literal["MANUAL", "ROTATION", "COPY", "IMPORT", "DAY_OVERRIDE", "HOLIDAY_RULE"]] = None
    note: Optional[str] = Field(None, max_length=500)
    reason: Optional[str] = Field(None, max_length=500)


class DraftsIn(StrictModel):
    cells: List[DraftCellIn] = Field(..., min_length=1)
    row_version: Optional[int] = Field(None, ge=1)


class ExemptionIn(StrictModel):
    exemption_ref: str = Field(..., min_length=1, max_length=100)
    reason: str = Field(..., min_length=1, max_length=500)


class PublishIn(StrictModel):
    dry_run: bool = False
    acknowledged_warnings: List[str] = Field(default_factory=list, max_length=50)
    reasons: Dict[str, str] = Field(default_factory=dict)
    exemptions: Dict[str, ExemptionIn] = Field(default_factory=dict)
    employee_ids: Optional[List[int]] = Field(None, max_length=1000)
    date_from: Optional[date] = Field(None, alias="from")
    date_to: Optional[date] = Field(None, alias="to")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class DirectPublishIn(StrictModel):
    cells: List[DraftCellIn] = Field(..., min_length=1)
    dry_run: bool = False
    acknowledged_warnings: List[str] = Field(default_factory=list, max_length=50)
    reasons: Dict[str, str] = Field(default_factory=dict)
    exemptions: Dict[str, ExemptionIn] = Field(default_factory=dict)
    reason_code: Optional[str] = Field(None, max_length=40)


class PatternIn(StrictModel):
    code: str = Field(..., min_length=1, max_length=40)
    name: str = Field(..., min_length=1, max_length=100)
    template_key: Optional[str] = Field(None, max_length=40)
    branch_id: Optional[int] = None
    legal_entity_id: Optional[int] = None
    version: Optional[Dict[str, Any]] = None


class PatternVersionIn(StrictModel):
    cycle_days: Optional[int] = Field(None, ge=1, le=366)
    tokens: Optional[str] = Field(None, max_length=366)
    crew_rows: Optional[Dict[str, str]] = None
    crew_offsets: Optional[Dict[str, int]] = None
    token_map: Dict[str, Any]
    coverage: Optional[Dict[str, int]] = None
    week_anchor_weekday: Optional[int] = Field(None, ge=0, le=6)
    lints_muted: List[str] = Field(default_factory=list)


class RotationAssignmentIn(StrictModel):
    pattern_version_id: int
    employee_ids: List[int] = Field(..., min_length=1, max_length=100)
    crew_code: Optional[str] = Field(None, max_length=8)
    offset_days: int = Field(0, ge=0, le=365)
    anchor_date: date
    effective_from: date
    effective_to: Optional[date] = None


class GenerateIn(StrictModel):
    overwrite_drafts: bool = False


class GenerateAheadIn(StrictModel):
    branch_id: int
    days: int = Field(90, ge=1, le=366)
    start: Optional[date] = None

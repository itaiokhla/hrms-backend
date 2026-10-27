"""Request bodies of the shift catalogue and assignment rules (AS 11 §6.2; 03-shifts/01 §3.2, §12, §13)."""

from datetime import date
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field

from app.schemas.auth import StrictModel

HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
CODE = r"^[A-Z0-9][A-Z0-9_-]{0,19}$"


class VersionIn(StrictModel):
    """The body of one shift version (03-shifts/01 §3.2); times are the work zone's unless anchored."""

    effective_from: Optional[date] = None
    effective_to: Optional[date] = None
    start_time: Optional[str] = Field(None, pattern=HHMM)
    end_time: Optional[str] = Field(None, pattern=HHMM)
    segments: List[Dict[str, Any]] = Field(default_factory=list, max_length=4)
    break_rules: List[Dict[str, Any]] = Field(default_factory=list, max_length=4)
    unpaid_break_minutes: Optional[int] = Field(None, ge=0, le=1440)  # legacy value, used without break rules
    half_split_time: Optional[str] = Field(None, pattern=HHMM)
    core_start_time: Optional[str] = Field(None, pattern=HHMM)
    core_end_time: Optional[str] = Field(None, pattern=HHMM)
    expected_net_minutes: Optional[int] = Field(None, ge=1, le=1440)
    grace_in_minutes: Optional[int] = Field(None, ge=0, le=240)
    grace_out_minutes: Optional[int] = Field(None, ge=0, le=240)
    early_in_window_minutes: int = Field(120, ge=0, le=720)
    late_out_window_minutes: int = Field(240, ge=0, le=720)
    anchor_timezone: Optional[str] = Field(None, max_length=64)
    safety_critical: bool = False
    outdoor: bool = False
    long_duty: bool = False
    reason: Optional[str] = Field(None, max_length=500)
    on_overlap: Literal["REJECT", "TRUNCATE"] = "REJECT"
    acknowledged_warnings: List[str] = Field(default_factory=list, max_length=20)
    dry_run: bool = False


class ShiftCreate(StrictModel):
    code: str = Field(..., pattern=CODE)
    name: str = Field(..., min_length=1, max_length=100)
    kind: Literal["FIXED", "FLEXIBLE", "OPEN", "SPLIT"] = "FIXED"
    color_token: str = Field("shift-1", pattern=r"^shift-([1-9]|1[0-2])$")
    legal_entity_id: Optional[int] = None
    branch_id: Optional[int] = None
    description: Optional[str] = Field(None, max_length=500)
    version: VersionIn


class ShiftPatch(StrictModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    color_token: Optional[str] = Field(None, pattern=r"^shift-([1-9]|1[0-2])$")
    description: Optional[str] = Field(None, max_length=500)
    code: Optional[str] = Field(None, pattern=CODE)
    kind: Optional[Literal["FIXED", "FLEXIBLE", "OPEN", "SPLIT"]] = None
    row_version: Optional[int] = Field(None, ge=1)


class ArchiveIn(StrictModel):
    reason: Optional[str] = Field(None, max_length=500)
    row_version: Optional[int] = Field(None, ge=1)


class VoidIn(StrictModel):
    reason: str = Field(..., min_length=1, max_length=500)


class RuleIn(StrictModel):
    name: str = Field(..., min_length=1, max_length=100)
    shift_id: int
    criteria: Dict[str, Any]
    priority: int = Field(500, ge=0, le=1_000_000)
    effective_from: date
    effective_to: Optional[date] = None
    legal_entity_id: Optional[int] = None
    reason: Optional[str] = Field(None, max_length=500)
    replaces_rule_id: Optional[int] = None
    team_id_snapshot: Optional[int] = None
    apply_to_locked_periods: bool = False


class RulePatch(StrictModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    shift_id: Optional[int] = None
    criteria: Optional[Dict[str, Any]] = None
    priority: Optional[int] = Field(None, ge=0, le=1_000_000)
    effective_to: Optional[date] = None
    status: Optional[Literal["ACTIVE", "INACTIVE"]] = None
    reason: Optional[str] = Field(None, max_length=500)
    apply_to_locked_periods: bool = False
    row_version: Optional[int] = Field(None, ge=1)


class BulkAssignIn(StrictModel):
    """``apply_as`` (SP-AS-032); the old name ``mode`` is rejected by the strict model (SP-AS-033)."""

    employee_ids: Optional[List[int]] = None
    filter: Optional[Dict[str, Any]] = None
    expected_total: Optional[int] = Field(None, ge=0)
    snapshot: Optional[Dict[str, Any]] = None
    shift_id: int
    effective_from: date
    effective_to: Optional[date] = None
    apply_as: Literal["RULE", "PLACEMENT"] = "RULE"
    apply_to_locked_periods: bool = False
    reason: Optional[str] = Field(None, max_length=500)
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    dry_run: bool = False

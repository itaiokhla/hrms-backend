"""Request bodies of the leave v2 API (strict: unknown fields, company / status / approver ids are refused)."""

from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field

from app.schemas.auth import StrictModel

Portion = Literal["FULL", "SECOND_HALF"]
EndPortion = Literal["FULL", "FIRST_HALF"]


class LeaveRequestIn(StrictModel):
    leave_type: str = Field(..., min_length=1, max_length=30)
    start_date: date
    end_date: date
    start_portion: Portion = "FULL"
    end_portion: EndPortion = "FULL"
    reason: Optional[str] = Field(None, max_length=1000)
    employee_id: Optional[int] = Field(None, ge=1)  # on-behalf / HR-recorded only (permission-checked)
    is_emergency: bool = False
    draft: bool = False
    event_id: Optional[int] = Field(None, ge=1)
    attachment_ids: List[int] = Field(default_factory=list, max_length=10)
    contact_note: Optional[str] = Field(None, max_length=500)
    handover_employee_id: Optional[int] = Field(None, ge=1)


class LeaveEventIn(StrictModel):
    """Record a life event that gives EVENT leave (03-leave/06 §6.1 step 1); entitlement and window are the plan's."""

    leave_type_code: str = Field(..., min_length=1, max_length=30)
    event_type: Literal["EXPECTED_DELIVERY", "BIRTH", "ADOPTION_HANDOVER", "SURROGACY_HANDOVER", "PREGNANCY_LOSS",
                        "REPRODUCTIVE_PROCEDURE", "DEATH", "MARRIAGE", "PILGRIMAGE", "SABBATICAL_ELIGIBILITY",
                        "LONG_ILLNESS", "OTHER"]
    event_date: date
    employee_id: Optional[int] = Field(None, ge=1)  # on behalf / HR only (permission-checked)
    note: Optional[str] = Field(None, max_length=500)


class ReasonIn(StrictModel):
    reason: Optional[str] = Field(None, max_length=1000)
    reason_code: Optional[str] = Field(None, max_length=40)


class ReturnIn(StrictModel):
    reason: str = Field(..., min_length=1, max_length=1000)
    reason_code: Optional[str] = Field(None, max_length=40)
    suggestion: Optional[Dict[str, Any]] = None


class DecisionIn(StrictModel):
    remarks: Optional[str] = Field(None, max_length=1000)
    reason_code: Optional[str] = Field(None, max_length=40)


class ChangeIn(StrictModel):
    kind: Literal["CANCEL", "PARTIAL_CANCEL", "MODIFY", "CONVERT", "RECALL"]
    proposed: Dict[str, Any] = Field(default_factory=dict)
    reason: Optional[str] = Field(None, max_length=1000)


class PlanCreateIn(StrictModel):
    name: str = Field(..., min_length=1, max_length=150)
    description: Optional[str] = Field(None, max_length=2000)
    effective_from: Optional[date] = None
    template_key: Optional[str] = Field(None, max_length=40)
    copy_from_plan_id: Optional[int] = Field(None, ge=1)
    fields: Optional[Dict[str, Any]] = None


class PlanPatchIn(StrictModel):
    name: Optional[str] = Field(None, min_length=1, max_length=150)
    description: Optional[str] = Field(None, max_length=2000)


class PlanVersionIn(StrictModel):
    fields: Optional[Dict[str, Any]] = None
    effective_from: Optional[date] = None


class PlanActivateIn(StrictModel):
    effective_from: Optional[date] = None
    reason: Optional[str] = Field(None, max_length=500)
    recalc_future_leave: bool = False
    confirm: bool = False


class PlanRuleIn(StrictModel):
    criteria: Dict[str, Any]
    priority: int = Field(0, ge=0, le=1000)
    effective_from: date
    effective_to: Optional[date] = None


class PlanAssignmentsIn(StrictModel):
    rules: List[PlanRuleIn] = Field(..., max_length=200)


class SimulateIn(StrictModel):
    policy_id: int = Field(..., ge=1)
    version: int = Field(..., ge=1)
    employee_ids: Optional[List[int]] = Field(None, max_length=200)
    on: Optional[date] = None


class AdjustmentIn(StrictModel):
    employee_id: int = Field(..., ge=1)
    leave_type: str = Field(..., min_length=1, max_length=30)
    delta: Decimal = Field(..., ge=Decimal("-366"), le=Decimal("366"), decimal_places=2)
    effective_date: date
    reason: str = Field(..., min_length=3, max_length=500)
    reason_code: Optional[str] = Field(None, max_length=40)
    expires_on: Optional[date] = None


class BlackoutIn(StrictModel):
    name: str = Field(..., min_length=1, max_length=100)
    from_date: date
    to_date: date
    legal_entity_id: Optional[int] = Field(None, ge=1)
    branch_ids: Optional[List[int]] = None
    department_ids: Optional[List[int]] = None
    leave_type_codes: List[str] = Field(default_factory=list, max_length=60)
    mode: Literal["WARN", "BLOCK"] = "BLOCK"
    reason: Optional[str] = Field(None, max_length=500)
    status: Literal["ACTIVE", "ARCHIVED"] = "ACTIVE"


class CoverageIn(StrictModel):
    name: str = Field(..., min_length=1, max_length=100)
    department_id: Optional[int] = Field(None, ge=1)
    branch_id: Optional[int] = Field(None, ge=1)
    team_id: Optional[int] = Field(None, ge=1)
    max_absent_count: Optional[int] = Field(None, ge=0, le=10000)
    max_absent_pct: Optional[Decimal] = Field(None, gt=0, le=100)
    count_pending: bool = True
    mode: Literal["WARN", "BLOCK"] = "WARN"
    status: Literal["ACTIVE", "ARCHIVED"] = "ACTIVE"


class ReasonCodeIn(StrictModel):
    code: str = Field(..., min_length=2, max_length=40, pattern=r"^[A-Z][A-Z0-9_]*$")
    label: str = Field(..., min_length=1, max_length=100)
    applies_to: Literal["REJECT", "RETURN", "CANCEL", "RECALL", "ADJUST", "OVERRIDE"]
    counts_as_refused: bool = False
    is_active: bool = True


class RouteStepIn(StrictModel):
    approver_type: Literal["primary_manager", "skip_level_manager", "department_head", "role"]
    role_id: Optional[int] = None
    min_days: Optional[int] = Field(None, ge=1, le=366)
    condition: Optional[Dict[str, Any]] = None
    sla_hours: Optional[int] = Field(None, ge=1, le=720)


class RouteIn(StrictModel):
    steps: List[RouteStepIn] = Field(..., max_length=10)


class YearEndIn(StrictModel):
    cycle_end: date
    legal_entity_id: Optional[int] = Field(None, ge=1)


class GoLiveIn(StrictModel):
    confirm: str = Field(..., max_length=20)


class RollbackIn(StrictModel):
    confirm: str = Field(..., max_length=20)
    reason: str = Field(..., min_length=3, max_length=500)


class EncashmentIn(StrictModel):
    leave_type: str = Field(..., min_length=1, max_length=30)
    units: Decimal = Field(..., gt=0, le=Decimal("366"), decimal_places=2)
    reason: Optional[str] = Field(None, max_length=1000)
    employee_id: Optional[int] = Field(None, ge=1)


class CompOffClaimIn(StrictModel):
    worked_date: date
    reason: Optional[str] = Field(None, max_length=1000)
    employee_id: Optional[int] = Field(None, ge=1)


class MessageIn(StrictModel):
    body: str = Field(..., min_length=1, max_length=4000)
    internal: bool = False


class VerifyIn(StrictModel):
    approve: bool
    reason: Optional[str] = Field(None, max_length=500)


class DocumentRequestIn(StrictModel):
    category: str = Field(..., max_length=24)
    due_date: date
    note: Optional[str] = Field(None, max_length=500)


class LifecycleIn(StrictModel):
    """HR sets the dates leave eligibility reads (probation, confirmation, notice, service start)."""

    probation_end_date: Optional[date] = None
    confirmation_date: Optional[date] = None
    notice_start_date: Optional[date] = None
    service_start_date: Optional[date] = None

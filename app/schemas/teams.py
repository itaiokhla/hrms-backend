"""Request bodies of teams and memberships (X 01 §3, §14; spine §3.9) and role-assignment windows (X 01 §6.7, P-3)."""

from datetime import date
from typing import Dict, List, Literal, Optional

from pydantic import Field

from app.schemas.auth import StrictModel

TeamType = Literal["FUNCTIONAL", "PROJECT", "CREW", "SITE", "OTHER"]
MemberRole = Literal["LEAD", "DEPUTY_LEAD", "MEMBER"]


class TeamIn(StrictModel):
    code: str = Field(..., min_length=1, max_length=30, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    name: str = Field(..., min_length=1, max_length=100)
    team_type: TeamType
    parent_team_id: Optional[int] = None
    legal_entity_id: Optional[int] = None
    branch_id: Optional[int] = None
    description: Optional[str] = Field(None, max_length=500)
    effective_from: Optional[date] = None  # default: the actor's local today


class TeamPatch(StrictModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    team_type: Optional[TeamType] = None
    legal_entity_id: Optional[int] = None
    branch_id: Optional[int] = None
    description: Optional[str] = Field(None, max_length=500)
    # A re-parent is dated (box P-10): the parent changes from ``parent_effective_from`` (default local tomorrow).
    parent_team_id: Optional[int] = None
    clear_parent: bool = False
    parent_effective_from: Optional[date] = None
    row_version: Optional[int] = Field(None, ge=1)


class TeamArchiveIn(StrictModel):
    dry_run: bool = False
    confirm_code: Optional[str] = Field(None, max_length=30)  # L3 typed confirmation (the team code)
    row_version: Optional[int] = Field(None, ge=1)


class MembershipIn(StrictModel):
    employee_id: int
    member_role: MemberRole = "MEMBER"
    is_primary_team: bool = False
    is_routing_lead: bool = False
    effective_from: Optional[date] = None  # default: the actor's local today
    effective_to: Optional[date] = None
    reason: Optional[str] = Field(None, max_length=500)
    # TM-5 / §3.4: when this row becomes primary, the employee's other primary row ends the day before.
    move_primary: bool = False


class MembershipPatch(StrictModel):
    """End, cancel or change a membership. A change of role, primary or routing flag from ``change_from`` ends the row
    the day before and starts a new one (history kept, TM-3)."""

    effective_to: Optional[date] = None
    cancel: bool = False
    member_role: Optional[MemberRole] = None
    is_primary_team: Optional[bool] = None
    is_routing_lead: Optional[bool] = None
    change_from: Optional[date] = None
    reason: Optional[str] = Field(None, max_length=500)
    row_version: Optional[int] = Field(None, ge=1)


class AssignmentWindowIn(StrictModel):
    """One role assignment of the windowed form of ``PUT /users/{id}/roles`` (P-3, SP-AS-135)."""

    assignment_id: Optional[int] = None
    role_id: int
    effective_from: date
    effective_until: Optional[date] = None  # inclusive last local day
    targets: Dict[Literal["LEGAL_ENTITY", "BRANCH", "PAY_GROUP", "ASSIGNED_TEAMS"], List[int]] = Field(
        default_factory=dict)

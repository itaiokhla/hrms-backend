"""Teams and dated memberships (X-M1; owner X 01 §3, spine §3.9, §4.11; D-X-02).

* ``teams``: first-class groups separate from departments and the reporting line (FUNCTIONAL, PROJECT, CREW, SITE,
  OTHER). ``legal_entity_id`` / ``branch_id`` / ``site_id`` are the home unit (defaults, filters, terminology), never an
  eligibility rule. ``parent_team_id`` is the parent valid on the local today; ``team_parent_history`` keeps the parent
  per date range so TEAM_TREE reach over past dates follows the parent of that date (box P-10, X-ROLE-42).
* ``team_memberships``: effective-dated (employee, team, member role, primary team, routing lead) rows on inclusive
  employee-local dates; never deleted (ending sets ``effective_to``). Trigger ``team_memberships_guard()`` refuses
  overlapping rows of one (employee, team), two primary teams of an employee on one date and two routing leads of a
  team on one date; trigger ``teams_no_cycle()`` refuses parent cycles (depth <= 64).
"""

from sqlalchemy import (
    Boolean, CheckConstraint, Column, Date, ForeignKeyConstraint, Integer, String, UniqueConstraint,
)

from app.models.base import BaseModel, TenantMixin

TEAM_TYPES = ("FUNCTIONAL", "PROJECT", "CREW", "SITE", "OTHER")
TEAM_STATUSES = ("ACTIVE", "ARCHIVED")
TEAM_MEMBER_ROLES = ("LEAD", "DEPUTY_LEAD", "MEMBER")
LEAD_ROLES = ("LEAD", "DEPUTY_LEAD")
MAX_TEAM_DEPTH = 64


def _fk(table: str, column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["company_id", column], [f"{target}.company_id", f"{target}.id"],
                                ondelete=ondelete, name=f"fk_{table}_{name}")


class Team(TenantMixin, BaseModel):
    __tablename__ = "teams"

    code = Column(String(30), nullable=False)
    name = Column(String(100), nullable=False)
    team_type = Column(String(12), nullable=False)
    parent_team_id = Column(Integer, nullable=True)
    legal_entity_id = Column(Integer, nullable=True)
    branch_id = Column(Integer, nullable=True)
    site_id = Column(Integer, nullable=True)
    description = Column(String(500), nullable=True)
    status = Column(String(10), nullable=False, default="ACTIVE")
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_teams_company_id_id"),
        UniqueConstraint("company_id", "code", name="uq_teams_code"),
        _fk("teams", "parent_team_id", "teams", "parent"),
        _fk("teams", "legal_entity_id", "legal_entities", "legal_entity"),
        _fk("teams", "branch_id", "branches", "branch"),
        _fk("teams", "created_by_user_id", "users", "created_by"),
        CheckConstraint(f"team_type IN {TEAM_TYPES}", name="ck_teams_type"),
        CheckConstraint(f"status IN {TEAM_STATUSES}", name="ck_teams_status"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_teams_dates"),
        CheckConstraint("parent_team_id IS NULL OR parent_team_id <> id", name="ck_teams_not_own_parent"),
        CheckConstraint("row_version >= 1", name="ck_teams_row_version"),
    )


class TeamParentHistory(TenantMixin, BaseModel):
    """The parent of a team per inclusive local date range (box P-10); the current row mirrors ``teams``."""

    __tablename__ = "team_parent_history"

    team_id = Column(Integer, nullable=False, index=True)
    parent_team_id = Column(Integer, nullable=True)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    created_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_team_parent_history_company_id_id"),
        _fk("team_parent_history", "team_id", "teams", "team", "CASCADE"),
        _fk("team_parent_history", "parent_team_id", "teams", "parent"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from - 1",
                        name="ck_team_parent_history_dates"),
    )


class TeamMembership(TenantMixin, BaseModel):
    __tablename__ = "team_memberships"

    team_id = Column(Integer, nullable=False, index=True)
    employee_id = Column(Integer, nullable=False, index=True)
    member_role = Column(String(12), nullable=False, default="MEMBER")
    is_primary_team = Column(Boolean, nullable=False, default=False)
    is_routing_lead = Column(Boolean, nullable=False, default=False)
    effective_from = Column(Date, nullable=False)
    # Inclusive; ``effective_from - 1`` is a cancelled future row (empty window kept for audit, TM-7).
    effective_to = Column(Date, nullable=True)
    reason = Column(String(500), nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    ended_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_team_memberships_company_id_id"),
        _fk("team_memberships", "team_id", "teams", "team"),
        _fk("team_memberships", "employee_id", "employees", "employee"),
        _fk("team_memberships", "created_by_user_id", "users", "created_by"),
        _fk("team_memberships", "ended_by_user_id", "users", "ended_by"),
        CheckConstraint(f"member_role IN {TEAM_MEMBER_ROLES}", name="ck_team_memberships_role"),
        CheckConstraint("NOT is_routing_lead OR member_role = 'LEAD'", name="ck_team_memberships_routing_lead"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from - 1",
                        name="ck_team_memberships_dates"),
        CheckConstraint("row_version >= 1", name="ck_team_memberships_row_version"),
    )

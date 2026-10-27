"""Saved list views and dashboard preferences (X-M3; spine §3.10, AS 11 §4.14; X 04 §2.8, X 02).

* ``saved_views``: a user's named query of one list (``list_key`` of the ``LIST_KEYS`` registry,
  app/services/saved_views.py) — filters, sort, columns and page size, keys allow-listed per list. A shared view is
  visible to everyone who can open the list and edited only by its owner. A row with ``system_key`` set only hides
  or positions a platform system view for its user (``query = {}``; counts toward neither the cap nor sharing).
* ``dashboard_preferences``: per-user (or, with ``user_id`` NULL, per-persona tenant default) layout of a dashboard.
"""

from sqlalchemy import Boolean, CheckConstraint, Column, ForeignKeyConstraint, Index, Integer, SmallInteger, String, text
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import BaseModel, TenantMixin

# Frozen in migration x_m3_saved_views (CHECK); a revision that adds a list key widens both.
LIST_KEY_VALUES = (
    "attendance.days", "attendance.team_today", "attendance.timesheets", "attendance.timesheet_approvals",
    "attendance.exceptions", "attendance.requests", "attendance.on_duty", "attendance.review", "attendance.punches",
    "attendance.muster", "attendance.devices", "attendance.device_mappings", "attendance.device_commands",
    "attendance.sites", "attendance.reason_codes", "attendance.policies", "attendance.period_closes",
    "attendance.registers", "attendance.consents", "attendance.contractors",
    "shift.shifts", "shift.rules", "shift.rotations", "shift.rotation_assignments", "shift.skill_tags",
    "roster.periods", "roster.publications", "roster.open_shifts", "roster.shift_requests", "roster.availability",
    "roster.estimates",
    "team.teams", "team.members", "access.roles", "access.change_requests", "cutoff.items", "overtime.entries",
    "approvals.inbox", "audit.logs",
    "leave.my_requests", "leave.requests", "leave.approvals", "leave.team_calendar", "leave.balances", "leave.ledger",
    "leave.documents", "leave.types", "leave.plans", "leave.comp_off", "leave.encashments",
    "holiday.calendars", "holiday.holidays", "holiday.optional", "holiday.optional_selections", "holiday.work_weeks",
    "holiday.calendar_assignments",
)
PERSONAS = ("EMPLOYEE", "TEAM_LEAD", "MANAGER", "HR", "PAYROLL", "EXECUTIVE")
DASHBOARD_KEYS = ("home", "attendance", "leave_hr")
VIEW_NAME_MAX = 80
VIEWS_PER_LIST = 20


class SavedView(TenantMixin, BaseModel):
    __tablename__ = "saved_views"

    user_id = Column(Integer, nullable=False)
    list_key = Column(String(64), nullable=False)
    name = Column(String(VIEW_NAME_MAX), nullable=True)
    query = Column(JSONB, nullable=False, default=dict)
    is_shared = Column(Boolean, nullable=False, default=False, server_default="false")
    is_default = Column(Boolean, nullable=False, default=False, server_default="false")
    position = Column(SmallInteger, nullable=False, default=0, server_default="0")
    system_key = Column(String(64), nullable=True)
    is_hidden = Column(Boolean, nullable=False, default=False, server_default="false")
    row_version = Column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (
        ForeignKeyConstraint(["company_id", "user_id"], ["users.company_id", "users.id"], name="fk_saved_views_user",
                             ondelete="CASCADE"),
        CheckConstraint("(system_key IS NULL) = (name IS NOT NULL)", name="ck_saved_views_name_or_system"),
        CheckConstraint("system_key IS NULL OR (NOT is_shared AND NOT is_default)", name="ck_saved_views_system_row"),
        CheckConstraint("row_version >= 1", name="ck_saved_views_row_version"),
        Index("uq_saved_views_name", "company_id", "user_id", "list_key", "name", unique=True,
              postgresql_where=text("system_key IS NULL")),
        Index("uq_saved_views_system", "company_id", "user_id", "list_key", "system_key", unique=True,
              postgresql_where=text("system_key IS NOT NULL")),
        Index("uq_saved_views_default", "company_id", "user_id", "list_key", unique=True,
              postgresql_where=text("is_default")),
        Index("ix_saved_views_list", "company_id", "list_key", "is_shared"),
    )


class DashboardPreference(TenantMixin, BaseModel):
    __tablename__ = "dashboard_preferences"

    user_id = Column(Integer, nullable=True)  # NULL = the tenant default for ``persona``
    persona = Column(String(16), nullable=True)
    dashboard_key = Column(String(32), nullable=False)
    layout = Column(JSONB, nullable=False, default=dict)
    row_version = Column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (
        ForeignKeyConstraint(["company_id", "user_id"], ["users.company_id", "users.id"],
                             name="fk_dashboard_preferences_user", ondelete="CASCADE"),
        CheckConstraint("(user_id IS NULL) = (persona IS NOT NULL)", name="ck_dashboard_preferences_owner"),
        CheckConstraint("row_version >= 1", name="ck_dashboard_preferences_row_version"),
        Index("uq_dashboard_preferences_user", "company_id", "user_id", "dashboard_key", unique=True,
              postgresql_where=text("user_id IS NOT NULL")),
        Index("uq_dashboard_preferences_persona", "company_id", "persona", "dashboard_key", unique=True,
              postgresql_where=text("user_id IS NULL")),
    )

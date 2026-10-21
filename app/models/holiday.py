from sqlalchemy import Boolean, CheckConstraint, Column, Date, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint

from app.models.base import BaseModel, TenantMixin

HOLIDAY_TYPES = ("PUBLIC", "COMPANY", "OPTIONAL")


class Holiday(TenantMixin, BaseModel):
    """Company holiday. `location` NULL means it applies to every location.

    `calendar_id` and `holiday_type` are the payroll view of the same holiday (3.4 §8): the
    holiday calendar it belongs to and whether it is a public, company or optional holiday.
    Migration 0008 filled both from `location` and `is_optional`, which stay for today's
    holiday screens until every reader uses calendars.
    """

    __tablename__ = "holidays"

    name = Column(String(150), nullable=False)
    date = Column(Date, nullable=False)
    location = Column(String(100), nullable=True)
    is_optional = Column(Boolean, nullable=False, default=False)
    description = Column(Text, nullable=True)
    calendar_id = Column(Integer, nullable=True)
    holiday_type = Column(String(10), nullable=False, default="COMPANY")

    __table_args__ = (
        UniqueConstraint("company_id", "date", "name", name="uq_holidays_company_date_name"),
        Index("ix_holidays_company_date", "company_id", "date"),
        Index("ix_holidays_calendar_date", "calendar_id", "date"),
        ForeignKeyConstraint(
            ["company_id", "calendar_id"], ["holiday_calendars.company_id", "holiday_calendars.id"],
            name="fk_holidays_calendar",
        ),
        CheckConstraint(f"holiday_type IN {HOLIDAY_TYPES}", name="ck_holidays_type"),
    )

from sqlalchemy import Boolean, Column, Date, Index, String, Text, UniqueConstraint

from app.models.base import BaseModel, TenantMixin


class Holiday(TenantMixin, BaseModel):
    """Company holiday. `location` NULL means it applies to every location."""

    __tablename__ = "holidays"

    name = Column(String(150), nullable=False)
    date = Column(Date, nullable=False)
    location = Column(String(100), nullable=True)
    is_optional = Column(Boolean, nullable=False, default=False)
    description = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "date", "name", name="uq_holidays_company_date_name"),
        Index("ix_holidays_company_date", "company_id", "date"),
    )

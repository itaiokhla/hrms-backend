from sqlalchemy import Column, String, Text, UniqueConstraint

from app.models.base import BaseModel, TenantMixin


class CompanySetting(TenantMixin, BaseModel):
    """Per-company key/value settings. Keys are whitelisted in settings_service."""

    __tablename__ = "company_settings"

    key = Column(String(100), nullable=False)
    value = Column(Text, nullable=True)

    __table_args__ = (UniqueConstraint("company_id", "key", name="uq_company_settings_key"),)

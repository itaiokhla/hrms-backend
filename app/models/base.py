from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer
from sqlalchemy.orm import declarative_base, declared_attr

Base = declarative_base()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BaseModel(Base):
    __abstract__ = True

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class TenantMixin:
    """Marks a model as owned by exactly one company.

    The tenant boundary is enforced in three layers:
      1. Services resolve the company from the authenticated session only.
      2. ORM events (app.database.db) stamp company_id on insert, reject
         cross-tenant writes and add a company filter to every SELECT.
      3. PostgreSQL row-level security policies (see the baseline migration).
    """

    @declared_attr
    def company_id(cls):
        return Column(
            Integer,
            ForeignKey("companies.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        )

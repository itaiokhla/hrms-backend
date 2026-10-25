"""Unclaimed pay log (company policy, EC-PAY-19; migration 0033_tax_profiles_unclaimed).

``payment_unclaimed_events``: append-only history of an obligation entering and leaving the UNCLAIMED status.
The status itself is ``payment_obligations.status``; this table keeps what the status cannot: which policy window
moved it, the outstanding amount at that moment, which unsent instructions were held, and for a release who
released it and why. Nothing here moves money: marking holds unsent instructions, releasing lets the system
re-check readiness, and a payment still needs a batch and the usual approvals.
"""

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import BaseModel, TenantMixin, utcnow

UNCLAIMED_ACTIONS = ("MARKED", "RELEASED")
TABLE = "payment_unclaimed_events"
REASON_MAX = 2000


def _fk(columns, target: str, name: str) -> ForeignKeyConstraint:
    local = ["company_id", *([columns] if isinstance(columns, str) else columns)]
    return ForeignKeyConstraint(local, [f"{target}.company_id", f"{target}.id"], name=f"fk_{TABLE}_{name}")


class PaymentUnclaimedEvent(TenantMixin, BaseModel):
    __tablename__ = TABLE

    obligation_id = Column(Integer, nullable=False, index=True)
    employee_id = Column(Integer, nullable=False)
    legal_entity_id = Column(Integer, nullable=False)
    action = Column(String(10), nullable=False)
    reason = Column(Text, nullable=False)
    window_days = Column(Integer, nullable=True)
    amount = Column(Numeric(19, 4), nullable=False)
    currency = Column(CHAR(3), nullable=False)
    held_instruction_ids = Column(JSONB, nullable=False, default=list)
    result_status = Column(String(10), nullable=True)
    actor_user_id = Column(Integer, nullable=True)
    at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name=f"uq_{TABLE}_company_id_id"),
        Index(f"ix_{TABLE}_scope", "company_id", "legal_entity_id", "at"),
        _fk("obligation_id", "payment_obligations", "obligation"),
        _fk("employee_id", "employees", "employee"),
        _fk("legal_entity_id", "legal_entities", "legal_entity"),
        _fk("actor_user_id", "users", "actor"),
        CheckConstraint(f"action IN {UNCLAIMED_ACTIONS}", name=f"ck_{TABLE}_action"),
        CheckConstraint(f"char_length(btrim(reason)) BETWEEN 1 AND {REASON_MAX}", name=f"ck_{TABLE}_reason"),
        CheckConstraint("amount > 0", name=f"ck_{TABLE}_amount"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name=f"ck_{TABLE}_currency"),
        CheckConstraint("jsonb_typeof(held_instruction_ids) = 'array'", name=f"ck_{TABLE}_held"),
        CheckConstraint("window_days IS NULL OR window_days >= 1", name=f"ck_{TABLE}_window"),
        CheckConstraint("action <> 'RELEASED' OR actor_user_id IS NOT NULL", name=f"ck_{TABLE}_release_actor"),
        CheckConstraint("action <> 'MARKED' OR window_days IS NOT NULL OR actor_user_id IS NOT NULL",
                        name=f"ck_{TABLE}_marked_cause"),
    )


__all__ = ["REASON_MAX", "UNCLAIMED_ACTIONS", "PaymentUnclaimedEvent"]

"""Nominees and legal heirs: non-employee payees of a deceased employee's final settlement (3.5 §15, 3.7 §2.1;
EC-BNK-15, EC-COR-07; migration 0030_nominees).

``nominees``: one row per person who receives (a share of) the final settlement of an employee whose
employment ended by DEATH. A nominee belongs to exactly one final-settlement case (composite foreign
key) and carries the proof document reference, the share (percent of the settlement net) and a status.
Bank details are never stored here: a nominee's account is a ``bank_accounts`` row with
``payee_type = 'NOMINEE'`` and ``nominee_id`` (composite key to this table), so entry, encryption,
masking, change control, verification and duplicate checks are the bank account service's.

Database rules services cannot be trusted to keep alone: shares of ACTIVE nominees of a case never add
up to more than 100 (deferred constraint trigger), one active nominee per name and relationship in a
case, the case and employee of a nominee never change and rows are never deleted.
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
    text,
)

from app.models.base import BaseModel, TenantMixin

TABLE = "nominees"
NOMINEE_KINDS = ("NOMINEE", "LEGAL_HEIR")
NOMINEE_STATUSES = ("ACTIVE", "ENDED")


def _fk(columns, target: str, name: str) -> ForeignKeyConstraint:
    local = ["company_id", *([columns] if isinstance(columns, str) else columns)]
    return ForeignKeyConstraint(local, [f"{target}.company_id", f"{target}.id"], name=f"fk_{TABLE}_{name}")


def _sql_list(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


class Nominee(TenantMixin, BaseModel):
    """A nominee or legal heir paid a share of a deceased employee's final settlement."""

    __tablename__ = TABLE

    employee_id = Column(Integer, nullable=False, index=True)  # the deceased employee
    final_settlement_case_id = Column(Integer, nullable=False, index=True)
    kind = Column(String(12), nullable=False, default="NOMINEE")
    name = Column(String(140), nullable=False)
    relationship = Column(String(60), nullable=False)
    proof_ref = Column(String(300), nullable=False)  # the nomination / succession document, as referenced by HR
    share_percent = Column(Numeric(12, 6), nullable=False)
    country = Column(CHAR(2), nullable=False)
    email = Column(String(254), nullable=True)  # alert recipient for bank changes, optional
    status = Column(String(10), nullable=False, default="ACTIVE")
    ended_at = Column(DateTime(timezone=True), nullable=True)
    ended_reason = Column(Text, nullable=True)
    ended_by_user_id = Column(Integer, nullable=True)
    created_by_user_id = Column(Integer, nullable=False)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name=f"uq_{TABLE}_company_id_id"),
        _fk("employee_id", "employees", "employee"),
        _fk("final_settlement_case_id", "final_settlement_cases", "case"),
        _fk("created_by_user_id", "users", "created_by"),
        _fk("ended_by_user_id", "users", "ended_by"),
        CheckConstraint(f"kind IN {_sql_list(NOMINEE_KINDS)}", name=f"ck_{TABLE}_kind"),
        CheckConstraint(f"status IN {_sql_list(NOMINEE_STATUSES)}", name=f"ck_{TABLE}_status"),
        CheckConstraint("share_percent > 0 AND share_percent <= 100", name=f"ck_{TABLE}_share"),
        CheckConstraint("char_length(btrim(name)) >= 2", name=f"ck_{TABLE}_name"),
        CheckConstraint("char_length(btrim(relationship)) >= 2", name=f"ck_{TABLE}_relationship"),
        CheckConstraint("char_length(btrim(proof_ref)) >= 3", name=f"ck_{TABLE}_proof"),
        CheckConstraint("country ~ '^[A-Z]{2}$'", name=f"ck_{TABLE}_country"),
        CheckConstraint("(status = 'ENDED') = (ended_at IS NOT NULL)", name=f"ck_{TABLE}_ended"),
        CheckConstraint("status <> 'ENDED' OR (ended_reason IS NOT NULL AND ended_by_user_id IS NOT NULL)",
                        name=f"ck_{TABLE}_ended_reason"),
        CheckConstraint("row_version >= 1", name=f"ck_{TABLE}_row_version"),
        Index(f"uq_{TABLE}_active_person", "company_id", "final_settlement_case_id", text("lower(btrim(name))"),
              text("lower(btrim(relationship))"), unique=True, postgresql_where=text("status = 'ACTIVE'")),
        Index(f"ix_{TABLE}_case_status", "company_id", "final_settlement_case_id", "status"),
    )


__all__ = ["NOMINEE_KINDS", "NOMINEE_STATUSES", "Nominee"]

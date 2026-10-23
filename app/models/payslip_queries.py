"""Payslip questions (3.6, setting ``payslip.queries_enabled``; migration 0027_payslip_queries).

``payslip_queries``: one question an employee raises about one of their own published payslips, and the
answer of payroll staff. The row copies the payslip's run, run employee, employee, legal entity and pay
group so that staff lists filter by the same legal-entity / pay-group scope as the payslips themselves
(``app.payroll.services.access``), never by joining through another employee's data.

Status OPEN -> ANSWERED (staff answer) -> CLOSED (the employee or staff close it); an OPEN question may also
be closed directly. The database keeps what services alone cannot be trusted with: bounded texts, an
answer always carries who answered and when, nobody answers their own question, a closed question
records when it was closed. Every reference is a composite same-company foreign key.
"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from app.models.base import BaseModel, TenantMixin

QUERY_STATUSES = ("OPEN", "ANSWERED", "CLOSED")
QUESTION_MAX = 2000
ANSWER_MAX = 4000
TABLE = "payslip_queries"


def _fk(columns, target: str, name: str, ondelete=None) -> ForeignKeyConstraint:
    local = ["company_id", *([columns] if isinstance(columns, str) else columns)]
    return ForeignKeyConstraint(local, [f"{target}.company_id", f"{target}.id"], name=f"fk_{TABLE}_{name}",
                                ondelete=ondelete)


class PayslipQuery(TenantMixin, BaseModel):
    """An employee's question about one of their payslips, and the staff answer."""

    __tablename__ = TABLE

    document_id = Column(Integer, nullable=False, index=True)
    run_id = Column(Integer, nullable=True)
    run_employee_id = Column(Integer, nullable=True)
    employee_id = Column(Integer, nullable=False, index=True)
    legal_entity_id = Column(Integer, nullable=False)
    pay_group_id = Column(Integer, nullable=True)
    question = Column(Text, nullable=False)
    status = Column(String(10), nullable=False, default="OPEN")
    asked_by_user_id = Column(Integer, nullable=False)
    answer = Column(Text, nullable=True)
    answered_by_user_id = Column(Integer, nullable=True)
    answered_at = Column(DateTime(timezone=True), nullable=True)
    closed_by_user_id = Column(Integer, nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name=f"uq_{TABLE}_company_id_id"),
        _fk("document_id", "documents", "document"),
        _fk("run_id", "payroll_runs", "run"),
        _fk("run_employee_id", "payroll_run_employees", "run_employee"),
        _fk("employee_id", "employees", "employee"),
        _fk("legal_entity_id", "legal_entities", "legal_entity"),
        _fk("pay_group_id", "pay_groups", "pay_group"),
        _fk("asked_by_user_id", "users", "asked_by"),
        _fk("answered_by_user_id", "users", "answered_by"),
        _fk("closed_by_user_id", "users", "closed_by"),
        CheckConstraint(f"status IN {QUERY_STATUSES}", name=f"ck_{TABLE}_status"),
        CheckConstraint(f"char_length(btrim(question)) BETWEEN 1 AND {QUESTION_MAX}", name=f"ck_{TABLE}_question"),
        CheckConstraint(f"answer IS NULL OR char_length(btrim(answer)) BETWEEN 1 AND {ANSWER_MAX}",
                        name=f"ck_{TABLE}_answer"),
        CheckConstraint("(answer IS NULL) = (answered_at IS NULL) AND (answer IS NULL) = (answered_by_user_id IS NULL)",
                        name=f"ck_{TABLE}_answered"),
        CheckConstraint("status <> 'ANSWERED' OR answer IS NOT NULL", name=f"ck_{TABLE}_answered_status"),
        CheckConstraint("(status = 'CLOSED') = (closed_at IS NOT NULL)", name=f"ck_{TABLE}_closed"),
        CheckConstraint("answered_by_user_id IS NULL OR answered_by_user_id <> asked_by_user_id",
                        name=f"ck_{TABLE}_not_self_answered"),
        CheckConstraint("row_version >= 1", name=f"ck_{TABLE}_row_version"),
        Index(f"ix_{TABLE}_scope_status", "company_id", "legal_entity_id", "status"),
        Index(f"ix_{TABLE}_employee_status", "company_id", "employee_id", "status"),
    )


__all__ = ["ANSWER_MAX", "QUERY_STATUSES", "QUESTION_MAX", "PayslipQuery"]

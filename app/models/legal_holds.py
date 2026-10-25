"""Legal holds on payroll documents (EC-SEC-16, 3.6 document retention, 3.9 §6; migration 0030_legal_holds).

A hold freezes the retention of documents: while it is ACTIVE, the retention sweep never deletes the stored
file of a document it covers, and employee erasure requests that touch covered data are refused with
LEGAL_HOLD. Its scope is exactly one of

* ``EMPLOYEE``      the employee's documents, and run-level documents (registers) of runs that include them;
* ``LEGAL_ENTITY``  every document of the legal entity;
* ``DOCUMENT_TYPE`` every document of the type (``PAYSLIP_V2``, ``PAYROLL_REGISTER`` ...);
* ``DOCUMENT``      one document.

Several holds may cover the same document (different matters); the document is released from retention
protection only when the last of them is released. Releasing is maker-checker (the releaser is never the
creator) and needs a recent step-up. Rows are never deleted and a released hold never reopens: place a new
one. Who is covered is decided in the database (``legal_hold_covers_document``) so the retention sweep, the
trigger on ``documents`` and the screens can never disagree.

Retention itself (``documents.retention_until``, the sweep and ``documents.retention_redacted_at``) is
documented in app.payroll.services.legal_holds.
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

HOLD_SCOPES = ("EMPLOYEE", "LEGAL_ENTITY", "DOCUMENT_TYPE", "DOCUMENT")
HOLD_STATUSES = ("ACTIVE", "RELEASED")


def _fk(table: str, column: str, target: str, name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["company_id", column], [f"{target}.company_id", f"{target}.id"],
                                name=f"fk_{table}_{name}")


class LegalHold(TenantMixin, BaseModel):
    """One legal hold: scope, reason, who placed it and (once released) who released it and why."""

    __tablename__ = "legal_holds"

    scope = Column(String(14), nullable=False)
    employee_id = Column(Integer, nullable=True)
    legal_entity_id = Column(Integer, nullable=True)
    doc_type = Column(String(30), nullable=True)
    document_id = Column(Integer, nullable=True)
    reference = Column(String(120), nullable=True)
    reason = Column(Text, nullable=False)
    status = Column(String(10), nullable=False, default="ACTIVE")
    created_by_user_id = Column(Integer, nullable=False)
    released_by_user_id = Column(Integer, nullable=True)
    released_at = Column(DateTime(timezone=True), nullable=True)
    release_reason = Column(Text, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_legal_holds_company_id_id"),
        _fk("legal_holds", "employee_id", "employees", "employee"),
        _fk("legal_holds", "legal_entity_id", "legal_entities", "legal_entity"),
        _fk("legal_holds", "document_id", "documents", "document"),
        _fk("legal_holds", "created_by_user_id", "users", "created_by"),
        _fk("legal_holds", "released_by_user_id", "users", "released_by"),
        CheckConstraint("scope IN ('EMPLOYEE', 'LEGAL_ENTITY', 'DOCUMENT_TYPE', 'DOCUMENT')",
                        name="ck_legal_holds_scope"),
        CheckConstraint("status IN ('ACTIVE', 'RELEASED')", name="ck_legal_holds_status"),
        CheckConstraint(
            "(scope = 'EMPLOYEE' AND employee_id IS NOT NULL AND legal_entity_id IS NULL AND doc_type IS NULL "
            "AND document_id IS NULL) OR "
            "(scope = 'LEGAL_ENTITY' AND legal_entity_id IS NOT NULL AND employee_id IS NULL AND doc_type IS NULL "
            "AND document_id IS NULL) OR "
            "(scope = 'DOCUMENT_TYPE' AND doc_type IS NOT NULL AND employee_id IS NULL AND legal_entity_id IS NULL "
            "AND document_id IS NULL) OR "
            "(scope = 'DOCUMENT' AND document_id IS NOT NULL AND employee_id IS NULL AND legal_entity_id IS NULL "
            "AND doc_type IS NULL)", name="ck_legal_holds_target"),
        CheckConstraint("(status = 'RELEASED') = (released_at IS NOT NULL AND released_by_user_id IS NOT NULL "
                        "AND release_reason IS NOT NULL)", name="ck_legal_holds_released"),
        CheckConstraint("released_by_user_id IS NULL OR released_by_user_id <> created_by_user_id",
                        name="ck_legal_holds_maker_checker"),
        CheckConstraint("length(btrim(reason)) > 0", name="ck_legal_holds_reason"),
        CheckConstraint("row_version >= 1", name="ck_legal_holds_row_version"),
        Index("ix_legal_holds_active", "company_id", "scope", postgresql_where="status = 'ACTIVE'"),
    )

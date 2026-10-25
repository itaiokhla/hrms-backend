"""Employee tax profiles (3.2 §7.2-§7.3, 3.9 §3.10; migration 0033_tax_profiles_unclaimed).

* ``employee_tax_profiles``: one **version** of an employee's declarations, filled against the tax-profile schema
  of the country pack of their legal entity (``pack_code`` and the ``schema_hash`` it was filled in against).
  Versions are effective-dated (``effective_from``) and numbered per employee (``version_no``). ``declared_values``
  holds the flat ``code -> value`` mapping of every non-identifier field (money as decimal strings);
  identifiers live only in ``identifiers_enc`` (AES-GCM sealed with the tenant data key, one ciphertext per field)
  with a masked display copy in ``identifiers_masked``, and never enter the payroll snapshot.
* Status SUBMITTED -> APPROVED | REJECTED (HR review) or WITHDRAWN (the employee). Only one version per employee
  is SUBMITTED at a time. The database keeps what services alone cannot be trusted with: a reviewer is never the
  submitter (maker-checker), a review records who and when, a rejection carries a note, and a reviewed or
  withdrawn version is frozen by a trigger (a payroll snapshot may already cite an approved version).
* ``employee_tax_proofs``: documents an employee attaches to a SUBMITTED version for one field (stored privately
  with their SHA-256, never changed afterwards).

Every reference is a composite same-company foreign key; both tables have row-level security.
"""

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import BaseModel, TenantMixin

PROFILE_STATUSES = ("SUBMITTED", "APPROVED", "REJECTED", "WITHDRAWN")
PROFILE_TABLE = "employee_tax_profiles"
PROOF_TABLE = "employee_tax_proofs"
NOTE_MAX = 2000


def _fk(table: str, columns, target: str, name: str, ondelete=None) -> ForeignKeyConstraint:
    local = ["company_id", *([columns] if isinstance(columns, str) else columns)]
    return ForeignKeyConstraint(local, [f"{target}.company_id", f"{target}.id"], name=f"fk_{table}_{name}",
                                ondelete=ondelete)


class EmployeeTaxProfile(TenantMixin, BaseModel):
    """One version of an employee's tax declarations."""

    __tablename__ = PROFILE_TABLE

    employee_id = Column(Integer, nullable=False, index=True)
    legal_entity_id = Column(Integer, nullable=False)
    country = Column(CHAR(2), nullable=False)
    pack_code = Column(String(20), nullable=False)
    schema_hash = Column(CHAR(64), nullable=False)
    version_no = Column(Integer, nullable=False)
    effective_from = Column(Date, nullable=False)
    status = Column(String(10), nullable=False, default="SUBMITTED")
    declared_values = Column(JSONB, nullable=False, default=dict)
    identifiers_enc = Column(JSONB, nullable=False, default=dict)
    identifiers_masked = Column(JSONB, nullable=False, default=dict)
    submitted_by_user_id = Column(Integer, nullable=False)
    submitted_at = Column(DateTime(timezone=True), nullable=False)
    reviewed_by_user_id = Column(Integer, nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    review_note = Column(Text, nullable=True)
    withdrawn_at = Column(DateTime(timezone=True), nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name=f"uq_{PROFILE_TABLE}_company_id_id"),
        UniqueConstraint("company_id", "employee_id", "version_no", name=f"uq_{PROFILE_TABLE}_version"),
        Index(f"uq_{PROFILE_TABLE}_one_open", "company_id", "employee_id", unique=True,
              postgresql_where=text("status = 'SUBMITTED'")),
        Index(f"ix_{PROFILE_TABLE}_scope_status", "company_id", "legal_entity_id", "status"),
        _fk(PROFILE_TABLE, "employee_id", "employees", "employee"),
        _fk(PROFILE_TABLE, "legal_entity_id", "legal_entities", "legal_entity"),
        _fk(PROFILE_TABLE, "submitted_by_user_id", "users", "submitted_by"),
        _fk(PROFILE_TABLE, "reviewed_by_user_id", "users", "reviewed_by"),
        CheckConstraint(f"status IN {PROFILE_STATUSES}", name=f"ck_{PROFILE_TABLE}_status"),
        CheckConstraint("version_no >= 1 AND row_version >= 1", name=f"ck_{PROFILE_TABLE}_versions"),
        CheckConstraint("country ~ '^[A-Z]{2}$'", name=f"ck_{PROFILE_TABLE}_country"),
        CheckConstraint("jsonb_typeof(declared_values) = 'object' AND jsonb_typeof(identifiers_enc) = 'object' "
                        "AND jsonb_typeof(identifiers_masked) = 'object'", name=f"ck_{PROFILE_TABLE}_shapes"),
        CheckConstraint("(status IN ('APPROVED', 'REJECTED')) = (reviewed_by_user_id IS NOT NULL "
                        "AND reviewed_at IS NOT NULL)", name=f"ck_{PROFILE_TABLE}_reviewed"),
        CheckConstraint("reviewed_by_user_id IS NULL OR reviewed_by_user_id <> submitted_by_user_id",
                        name=f"ck_{PROFILE_TABLE}_maker_checker"),
        CheckConstraint(f"status <> 'REJECTED' OR char_length(btrim(coalesce(review_note, ''))) BETWEEN 1 AND {NOTE_MAX}",
                        name=f"ck_{PROFILE_TABLE}_rejection_note"),
        CheckConstraint(f"review_note IS NULL OR char_length(review_note) <= {NOTE_MAX}",
                        name=f"ck_{PROFILE_TABLE}_note_length"),
        CheckConstraint("(status = 'WITHDRAWN') = (withdrawn_at IS NOT NULL)", name=f"ck_{PROFILE_TABLE}_withdrawn"),
    )


class EmployeeTaxProof(TenantMixin, BaseModel):
    """A proof document of one field of a SUBMITTED tax-profile version."""

    __tablename__ = PROOF_TABLE

    profile_id = Column(Integer, nullable=False, index=True)
    employee_id = Column(Integer, nullable=False)
    field_code = Column(String(81), nullable=False)
    file_name = Column(String(200), nullable=False)
    storage_path = Column(String(300), nullable=False)
    sha256 = Column(CHAR(64), nullable=False)
    mime = Column(String(60), nullable=False)
    bytes = Column(Integer, nullable=False)
    uploaded_by_user_id = Column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name=f"uq_{PROOF_TABLE}_company_id_id"),
        UniqueConstraint("company_id", "profile_id", "field_code", "sha256", name=f"uq_{PROOF_TABLE}_file"),
        _fk(PROOF_TABLE, "profile_id", PROFILE_TABLE, "profile"),
        _fk(PROOF_TABLE, "employee_id", "employees", "employee"),
        _fk(PROOF_TABLE, "uploaded_by_user_id", "users", "uploaded_by"),
        CheckConstraint("bytes > 0", name=f"ck_{PROOF_TABLE}_bytes"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=f"ck_{PROOF_TABLE}_sha256"),
        CheckConstraint("char_length(btrim(field_code)) > 0", name=f"ck_{PROOF_TABLE}_field"),
    )


__all__ = ["NOTE_MAX", "PROFILE_STATUSES", "EmployeeTaxProfile", "EmployeeTaxProof"]

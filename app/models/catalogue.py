"""Mutable platform catalogue state and FX (3.2 §4, §6.2; 3.9 §3.3).

ISO currencies, countries and subdivisions are code (app.payroll.currencies / countries, decision
I-06); only state that changes at run time is stored. A country without a country_capabilities row
has the code default: nothing may depend on rows seeded by a migration.
"""

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import Base, BaseModel, TenantMixin, utcnow

CAPABILITY_LEVELS = (
    "UNSUPPORTED", "DISPLAY_ONLY", "CONFIG_ONLY", "CALC_SUPPORTED", "VERIFIED", "FILING_INTEGRATED",
)
FX_RATE_TYPES = ("MID", "BUY", "SELL")
FX_RATE_STATUSES = ("ACTIVE", "PENDING_APPROVAL", "REJECTED")
FX_PURPOSES = ("CONTRACT_TO_PAY", "PAY_TO_PAYMENT", "REPORTING", "ACCOUNTING", "REIMBURSEMENT")
ROUNDING_MODES = ("HALF_UP", "HALF_EVEN", "UP", "DOWN", "CEILING", "FLOOR")


class CountryCapability(Base):
    """Payroll capability of a country (3.2 §6.2). Never upgrades itself; set by platform operators only."""

    __tablename__ = "country_capabilities"

    alpha2 = Column(CHAR(2), primary_key=True)
    capability_level = Column(String(20), nullable=False)
    note = Column(String(500), nullable=True)
    updated_by = Column(String(255), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        CheckConstraint("alpha2 ~ '^[A-Z]{2}$'", name="ck_country_capabilities_alpha2"),
        CheckConstraint(f"capability_level IN {CAPABILITY_LEVELS}", name="ck_country_capabilities_level"),
    )


class FxRate(BaseModel):
    """1 base_currency = rate quote_currency on as_of (3.2 §4.2).

    company_id NULL is a platform rate; a tenant row is that tenant's own (usually manual) rate.
    Not a TenantMixin model, so services set company_id themselves: row-level security lets the
    application role read platform rows and its own, insert and update only its own, and delete
    nothing. Only ACTIVE rates may be used; the maker of a manual rate never approves it.
    """

    __tablename__ = "fx_rates"

    company_id = Column(Integer, ForeignKey("companies.id", ondelete="RESTRICT"), nullable=True, index=True)
    base_currency = Column(String(3), nullable=False)
    quote_currency = Column(String(3), nullable=False)
    rate = Column(Numeric(24, 12), nullable=False)
    rate_type = Column(String(10), nullable=False, default="MID")
    # "MANUAL" or a provider code.
    source = Column(String(40), nullable=False)
    as_of = Column(Date, nullable=False)
    fetched_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(20), nullable=False)
    # Inverse or cross rate: the legs it was computed from.
    derived_from = Column(JSONB, nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    approved_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "base_currency ~ '^[A-Z]{3}$' AND quote_currency ~ '^[A-Z]{3}$'", name="ck_fx_rates_currencies"
        ),
        CheckConstraint("base_currency <> quote_currency", name="ck_fx_rates_pair"),
        CheckConstraint("rate > 0", name="ck_fx_rates_rate"),
        CheckConstraint(f"rate_type IN {FX_RATE_TYPES}", name="ck_fx_rates_rate_type"),
        CheckConstraint(f"status IN {FX_RATE_STATUSES}", name="ck_fx_rates_status"),
        CheckConstraint(
            "approved_by_user_id IS NULL OR created_by_user_id IS NULL OR approved_by_user_id <> created_by_user_id",
            name="ck_fx_rates_maker_checker",
        ),
        Index("ix_fx_rates_lookup", "base_currency", "quote_currency", "rate_type", "as_of"),
    )


class FxSnapshot(TenantMixin, BaseModel):
    """The rates one run (or payment batch, report ...) used, frozen when captured (3.2 §4.3, EC-MON-09)."""

    __tablename__ = "fx_snapshots"

    # References payroll_runs once that table exists (no foreign key yet).
    run_id = Column(Integer, nullable=True, index=True)
    purpose = Column(String(40), nullable=False)
    captured_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (UniqueConstraint("company_id", "id", name="uq_fx_snapshots_company_id_id"),)


class FxSnapshotRate(TenantMixin, BaseModel):
    """A copy of one rate in a snapshot: later changes to fx_rates never alter it.

    fx_rate_id must name an ACTIVE platform rate or one of the same company (trigger).
    """

    __tablename__ = "fx_snapshot_rates"

    snapshot_id = Column(Integer, nullable=False, index=True)
    fx_rate_id = Column(Integer, nullable=False)
    base_currency = Column(String(3), nullable=False)
    quote_currency = Column(String(3), nullable=False)
    rate = Column(Numeric(24, 12), nullable=False)
    rate_type = Column(String(10), nullable=False)
    source = Column(String(40), nullable=False)
    as_of = Column(Date, nullable=False)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_fx_snapshot_rates_company_id_id"),
        UniqueConstraint("snapshot_id", "base_currency", "quote_currency", "rate_type", name="uq_fx_snapshot_rates_pair"),
        ForeignKeyConstraint(
            ["company_id", "snapshot_id"], ["fx_snapshots.company_id", "fx_snapshots.id"],
            ondelete="CASCADE", name="fk_fx_snapshot_rates_snapshot",
        ),
        CheckConstraint(
            "base_currency ~ '^[A-Z]{3}$' AND quote_currency ~ '^[A-Z]{3}$' AND base_currency <> quote_currency",
            name="ck_fx_snapshot_rates_pair",
        ),
        CheckConstraint("rate > 0", name="ck_fx_snapshot_rates_rate"),
        CheckConstraint(f"rate_type IN {FX_RATE_TYPES}", name="ck_fx_snapshot_rates_rate_type"),
    )


class FxConversion(TenantMixin, BaseModel):
    """One conversion as a record: amounts, purpose, rate, quotation basis, rate date and rounding (3.2 §4.3).

    fx_rate_id, when set, must name an ACTIVE platform rate or one of the same company (trigger).
    """

    __tablename__ = "fx_conversions"

    source_amount = Column(Numeric(19, 4), nullable=False)
    source_currency = Column(String(3), nullable=False)
    target_currency = Column(String(3), nullable=False)
    purpose = Column(String(20), nullable=False)
    fx_rate_id = Column(Integer, nullable=True)
    rate = Column(Numeric(24, 12), nullable=False)
    # How `rate` is quoted, e.g. "1 USD = rate AED".
    quotation = Column(String(40), nullable=False)
    rate_date = Column(Date, nullable=False)
    rounding_mode = Column(String(10), nullable=False)
    result_amount = Column(Numeric(19, 4), nullable=False)
    # What the conversion belongs to, e.g. ("payroll_line", 42).
    ref_type = Column(String(40), nullable=True)
    ref_id = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_fx_conversions_company_id_id"),
        CheckConstraint(
            "source_currency ~ '^[A-Z]{3}$' AND target_currency ~ '^[A-Z]{3}$' AND source_currency <> target_currency",
            name="ck_fx_conversions_currencies",
        ),
        CheckConstraint(f"purpose IN {FX_PURPOSES}", name="ck_fx_conversions_purpose"),
        CheckConstraint("rate > 0", name="ck_fx_conversions_rate"),
        CheckConstraint(f"rounding_mode IN {ROUNDING_MODES}", name="ck_fx_conversions_rounding"),
        CheckConstraint("(ref_type IS NULL) = (ref_id IS NULL)", name="ck_fx_conversions_ref"),
        Index("ix_fx_conversions_ref", "company_id", "ref_type", "ref_id"),
    )

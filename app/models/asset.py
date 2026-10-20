from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.models.base import BaseModel, TenantMixin

ASSET_STATUSES = ("in_stock", "assigned", "maintenance", "retired")


class Asset(TenantMixin, BaseModel):
    __tablename__ = "assets"

    asset_tag = Column(String(50), nullable=False)
    name = Column(String(150), nullable=False)
    category = Column(String(50), nullable=False)
    serial_number = Column(String(100), nullable=True)
    status = Column(String(20), nullable=False, default="in_stock", index=True)
    purchase_date = Column(Date, nullable=True)
    purchase_cost = Column(Numeric(12, 2), nullable=True)
    notes = Column(Text, nullable=True)

    assignments = relationship(
        "AssetAssignment",
        primaryjoin="Asset.id == AssetAssignment.asset_id",
        foreign_keys="AssetAssignment.asset_id",
        order_by="AssetAssignment.assigned_at.desc()",
        back_populates="asset",
    )

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_assets_company_id_id"),
        UniqueConstraint("company_id", "asset_tag", name="uq_assets_company_tag"),
        CheckConstraint(f"status IN {ASSET_STATUSES}", name="ck_assets_status"),
    )


class AssetAssignment(TenantMixin, BaseModel):
    __tablename__ = "asset_assignments"

    asset_id = Column(Integer, nullable=False, index=True)
    employee_id = Column(Integer, nullable=False, index=True)
    assigned_by_user_id = Column(Integer, nullable=True)
    assigned_at = Column(DateTime(timezone=True), nullable=False)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    returned_at = Column(DateTime(timezone=True), nullable=True)
    received_by_user_id = Column(Integer, nullable=True)
    return_condition = Column(String(50), nullable=True)
    notes = Column(Text, nullable=True)

    asset = relationship(
        "Asset", primaryjoin="AssetAssignment.asset_id == Asset.id", foreign_keys=[asset_id], back_populates="assignments"
    )
    employee = relationship(
        "Employee", primaryjoin="AssetAssignment.employee_id == Employee.id", foreign_keys=[employee_id]
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "asset_id"], ["assets.company_id", "assets.id"], name="fk_asset_assignments_asset"
        ),
        ForeignKeyConstraint(
            ["company_id", "employee_id"], ["employees.company_id", "employees.id"], name="fk_asset_assignments_employee"
        ),
        CheckConstraint("returned_at IS NULL OR returned_at >= assigned_at", name="ck_asset_assignment_window"),
        Index("ix_asset_assignments_open", "asset_id", "returned_at"),
    )

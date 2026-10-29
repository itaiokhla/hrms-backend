"""Devices and imports (AS-M7, G-17; AS 04-attendance/01 §7, §8; migration ``0055_as_m7_devices``).

Tenant tables (RLS):

* ``attendance_device_user_mappings`` — device user id (PIN / card, canonical form) → employee, device-specific or
  tenant-wide (``device_id`` NULL), effective-dated; overlaps are refused (409 ``MAPPING_OVERLAP``, M-1..M-11);
* ``attendance_device_commands`` — the allow-listed commands a PUSH terminal fetches with ``getrequest`` (E-1..E-5);
* ``attendance_ingest_rejects`` — malformed ATTLOG lines, raw line encrypted (key purpose ``DEVICE_SECRET``), H-8;
* ``attendance_imports`` — CSV punch imports: dry run, then commit (§8.6).

Platform tables (no tenant RLS; the app role has no privilege, only the system session reads them):

* ``device_quarantine`` — serials that pushed without a valid secret (first / last contact, masked IP hash, count);
  the first-contact time is the proof of possession at activation (AS-SEC-02);
* ``device_registry_index`` — serial → (company, device) of every activated PUSH / AGENT device (H-1).
"""

from sqlalchemy import (
    Boolean,
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

from app.models.base import Base, BaseModel, TenantMixin

DEVICE_COMMANDS = ("SYNC_TIME", "UPLOAD_USER", "DISABLE_USER", "DELETE_USER", "QUERY_USERS", "RESEND_LOGS",
                   "REFRESH_SETTINGS")
COMMAND_STATUSES = ("QUEUED", "SENT", "ACKED", "FAILED", "EXPIRED", "CANCELLED")
SYNC_STATES = ("PENDING", "SYNCED", "FAILED", "NOT_APPLICABLE")
REJECT_STATUSES = ("OPEN", "DISMISSED", "FIXED")
IMPORT_STATUSES = ("DRY_RUN", "COMMITTED")
QUARANTINE_STATUSES = ("SEEN", "CLAIMED", "BLOCKED")


def _in(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _fk(table: str, column: str, target: str, name: str, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["company_id", column], [f"{target}.company_id", f"{target}.id"],
                                name=f"fk_{table}_{name}", ondelete=ondelete)


class AttendanceDeviceUserMapping(TenantMixin, BaseModel):
    __tablename__ = "attendance_device_user_mappings"

    device_id = Column(Integer, nullable=True)  # NULL = every device of the tenant (M-1)
    device_user_id = Column(String(32), nullable=False)  # canonical (M-11)
    device_user_label = Column(String(32), nullable=True)  # as typed
    employee_id = Column(Integer, nullable=False)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    sync_state = Column(String(16), nullable=False, default="NOT_APPLICABLE", server_default="NOT_APPLICABLE")
    enrolment = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    created_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_attendance_device_user_mappings_company_id_id"),
        _fk("attendance_device_user_mappings", "device_id", "attendance_devices", "device"),
        _fk("attendance_device_user_mappings", "employee_id", "employees", "employee"),
        CheckConstraint(f"sync_state IN {_in(SYNC_STATES)}", name="ck_attendance_device_user_mappings_sync"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from",
                        name="ck_attendance_device_user_mappings_dates"),
        Index("ix_attendance_device_user_mappings_lookup", "company_id", "device_user_id"),
    )


class AttendanceDeviceCommand(TenantMixin, BaseModel):
    __tablename__ = "attendance_device_commands"

    device_id = Column(Integer, nullable=False)
    command = Column(String(20), nullable=False)
    payload = Column(JSONB, nullable=False, default=dict)
    status = Column(String(10), nullable=False, default="QUEUED")
    return_code = Column(String(20), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    acked_at = Column(DateTime(timezone=True), nullable=True)
    requested_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_attendance_device_commands_company_id_id"),
        _fk("attendance_device_commands", "device_id", "attendance_devices", "device"),
        CheckConstraint(f"command IN {_in(DEVICE_COMMANDS)}", name="ck_attendance_device_commands_command"),
        CheckConstraint(f"status IN {_in(COMMAND_STATUSES)}", name="ck_attendance_device_commands_status"),
        Index("ix_attendance_device_commands_queue", "company_id", "device_id",
              postgresql_where=text("status = 'QUEUED'")),
    )


class AttendanceIngestReject(TenantMixin, BaseModel):
    __tablename__ = "attendance_ingest_rejects"

    device_id = Column(Integer, nullable=True)
    ingest_batch_id = Column(String(64), nullable=False)
    line_no = Column(Integer, nullable=False)
    payload_hash = Column(String(64), nullable=False)
    raw_line_enc = Column(Text, nullable=True)
    reason = Column(String(60), nullable=False)
    status = Column(String(10), nullable=False, default="OPEN", server_default="OPEN")
    resolved_by_user_id = Column(Integer, nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_attendance_ingest_rejects_company_id_id"),
        _fk("attendance_ingest_rejects", "device_id", "attendance_devices", "device"),
        CheckConstraint(f"status IN {_in(REJECT_STATUSES)}", name="ck_attendance_ingest_rejects_status"),
        Index("ix_attendance_ingest_rejects_open", "company_id", "device_id", postgresql_where=text("status = 'OPEN'")),
    )


class AttendanceImport(TenantMixin, BaseModel):
    __tablename__ = "attendance_imports"

    file_name = Column(String(200), nullable=False)
    status = Column(String(10), nullable=False, default="DRY_RUN")
    rows = Column(JSONB, nullable=False, default=list)
    totals = Column(JSONB, nullable=False, default=dict)
    created_by_user_id = Column(Integer, nullable=True)
    committed_by_user_id = Column(Integer, nullable=True)
    committed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_attendance_imports_company_id_id"),
        CheckConstraint(f"status IN {_in(IMPORT_STATUSES)}", name="ck_attendance_imports_status"),
        CheckConstraint("jsonb_typeof(rows) = 'array'", name="ck_attendance_imports_rows"),
    )


class DeviceQuarantine(Base):
    """Platform table: a serial that contacted ``/iclock`` without a valid secret (no tenant rows are created)."""

    __tablename__ = "device_quarantine"

    id = Column(Integer, primary_key=True)
    serial_number = Column(String(64), nullable=False, unique=True)
    vendor_guess = Column(String(20), nullable=True)
    first_seen_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)
    last_ip_hash = Column(String(64), nullable=True)
    payload_count = Column(Integer, nullable=False, default=0, server_default="0")
    status = Column(String(10), nullable=False, default="SEEN", server_default="SEEN")
    claimed_company_id = Column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint(f"status IN {_in(QUARANTINE_STATUSES)}", name="ck_device_quarantine_status"),
    )


class DeviceRegistryIndex(Base):
    """Platform table: serial → tenant device of every activated PUSH / AGENT device (H-1, AS-SEC-02)."""

    __tablename__ = "device_registry_index"

    id = Column(Integer, primary_key=True)
    serial_number = Column(String(64), nullable=False, unique=True)
    company_id = Column(Integer, nullable=False)
    device_id = Column(Integer, nullable=False)
    activated_at = Column(DateTime(timezone=True), nullable=False)
    active = Column(Boolean, nullable=False, default=True, server_default="true")


__all__ = ["AttendanceDeviceUserMapping", "AttendanceDeviceCommand", "AttendanceIngestReject", "AttendanceImport",
           "DeviceQuarantine", "DeviceRegistryIndex"]

"""Device registry for TERMINAL / AGENT, mappings, commands, rejects and CSV imports (AS-M7, G-17;
04-attendance/01 §7.2-§7.4, §8.6; AS 11 §6.13).

* **Register** (``attendance_devices.create_device`` delegates here for TERMINAL / AGENT): TERMINAL needs a serial,
  checked against ``device_registry_index`` with the system session (409 ``DEVICE_SERIAL_TAKEN`` for any other
  tenant's active serial, never naming it); ``PENDING_ACTIVATION`` with a claim code shown once.
* **Activate** (step-up): the claim code, and for a TERMINAL the proof of possession — a ``device_quarantine`` row of
  the serial seen within 72 h (422 ``DEVICE_NOT_SEEN`` otherwise; the dialog shows first contact and a masked IP
  hash). Generates the per-device secret, shown **once**: PUSH keeps ``secret_hash``; AGENT keeps ``secret_enc``
  (key purpose ``DEVICE_SECRET``) for HMAC checks. An AGENT gets a server-generated agent id (its ``X-Device-Id``).
  Writes ``device_registry_index``, claims the quarantine row, sets ``backfill_from`` (activation date − 7 days).
* **Rotate secret** (step-up): the old secret stops at once. **Suspend / resume / retire**: retire releases the serial.
* **Mappings** (M-1..M-11): canonical ids, 409 ``MAPPING_OVERLAP``, bulk ≤ 500 (400 ``TOO_MANY_ITEMS``),
  "Map by employee code"; saving maps the matching UNMAPPED / AMBIGUOUS punches (NULL → value only), resolves the
  ``UNMAPPED_PUNCH`` items and re-pairs the affected dates (M-7, trigger ``MAPPING_CHANGED``).
* **Commands** (E-1..E-5): the allow-list only, PUSH terminals only, 24 h expiry, cancel from ``QUEUED``.
* **Rejects** (H-8): listed by reason and hash; the raw line only with step-up; dismiss.
* **Imports** (§8.6): CSV dry run with per-row issues, then commit (every valid-time row stored, unresolved rows
  ``UNMAPPED``; natural key P-5a, so re-committing inserts nothing).

Services never commit; routes commit.
"""

from __future__ import annotations

import csv
import io
import secrets
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pytz
from sqlalchemy import func, or_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, bad_request, conflict, not_found
from app.core.principal import Principal
from app.models.attendance import AttendanceDevice, AttendancePunch, AttendanceReviewItem
from app.models.devices import (
    DEVICE_COMMANDS,
    AttendanceDeviceCommand,
    AttendanceDeviceUserMapping,
    AttendanceImport,
    AttendanceIngestReject,
    DeviceQuarantine,
    DeviceRegistryIndex,
)
from app.models.employee import Employee
from app.services import device_ingest as ingest
from app.services import device_protocol as proto
from app.services.audit_log_service import AuditLogService

MAX_MAPPINGS = 500
MAX_IMPORT_ROWS = 5_000
MAX_IMPORT_BYTES = 5 * 1024 * 1024
DIRECTIONS = ("IN", "OUT", "BREAK_START", "BREAK_END")
VENDORS = ("ZKTECO", "ESSL", "SUPREMA", "HIKVISION", "MATRIX", "MANTRA", "REALTIME", "OTHER")


def _now() -> datetime:
    return clock.utcnow().replace(microsecond=0)


def _iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


# ------------------------------------------------------------------------------------------------- registry
def serialize(device: AttendanceDevice, now: Optional[datetime] = None) -> dict:
    """The common device shape plus the AS-M7 fields (computed health, watermark, protocol)."""
    from app.services import attendance_devices

    body = attendance_devices.serialize(device)
    if device.kind == "KIOSK":
        return body
    now = now or _now()
    actions = []
    if device.status == "PENDING_ACTIVATION":
        actions = ["activate", "reissue-claim-code", "retire"]
    elif device.status == "ACTIVE":
        actions = ["rotate-secret", "suspend", "retire"] + (["commands"] if device.integration == "PUSH" else [])
    elif device.status == "SUSPENDED":
        actions = ["resume", "rotate-secret", "retire"]
    body.update({
        "serial_number": device.serial_number, "direction_mode": device.direction_mode,
        "device_role": device.device_role, "has_secret": bool(device.secret_hash),
        "health": proto.health(device.last_seen_at, device.last_clock_skew_seconds, now)
        if device.status == "ACTIVE" else device.health,
        "watermark_at": _iso(device.watermark_at), "last_punch_at": _iso(device.last_punch_at),
        "last_clock_skew_seconds": device.last_clock_skew_seconds, "backfill_from": _iso(device.backfill_from),
        "protocol_version": device.protocol_version, "firmware": device.firmware,
        "endpoint": "/iclock/cdata" if device.integration == "PUSH" else "/api/v1/attendance/ingest/punches",
        "allowed_actions": actions,
    })
    return body


def _get(db: Session, company_id: int, device_id: int, *, lock: bool = False) -> AttendanceDevice:
    query = db.query(AttendanceDevice).filter(AttendanceDevice.company_id == company_id,
                                              AttendanceDevice.id == device_id)
    device = (query.with_for_update() if lock else query).first()
    if device is None or device.kind not in ingest.INGEST_KINDS:
        raise not_found("Device")
    return device


def _serial_holder(serial: str) -> Optional[Tuple[int, int]]:
    from app.database.db import system_session

    with system_session() as sdb:
        row = (sdb.query(DeviceRegistryIndex.company_id, DeviceRegistryIndex.device_id)
               .filter(DeviceRegistryIndex.serial_number == serial, DeviceRegistryIndex.active.is_(True)).first())
    return (row[0], row[1]) if row else None


def create_device(db: Session, principal: Principal, data, zone: str) -> dict:
    from app.core import db_errors
    from app.services import attendance_devices

    kind = data.kind
    vendor = (getattr(data, "vendor", None) or ("ZKTECO" if kind == "TERMINAL" else "OTHER")).upper()
    if vendor not in VENDORS:
        raise bad_request("Choose a supported device vendor.", "INVALID_FIELDS")
    serial = (getattr(data, "serial_number", None) or "").strip().upper() or None
    if kind == "TERMINAL" and not serial:
        raise bad_request("Enter the terminal's serial number (on its label or in Menu › System info).",
                          "INVALID_FIELDS")
    if serial:
        holder = _serial_holder(serial)
        if holder is not None and holder[0] != principal.company_id:
            raise conflict("This serial number is already registered. Ask support to release it if this is your "
                           "device.", "DEVICE_SERIAL_TAKEN")
        same = db.query(AttendanceDevice.id).filter(AttendanceDevice.company_id == principal.company_id,
                                                    AttendanceDevice.serial_number == serial,
                                                    AttendanceDevice.retired_at.is_(None)).first()
        if same is not None:
            raise conflict("A device with this serial number is already registered.", "DEVICE_SERIAL_TAKEN")
    integration = "PUSH" if kind == "TERMINAL" else "AGENT"
    direction_mode = getattr(data, "direction_mode", None) or "FIRST_LAST"
    device_role = getattr(data, "device_role", None) or "IN_OUT"
    now = _now()
    device = AttendanceDevice(
        company_id=principal.company_id, code=data.code.strip().upper(), name=data.name.strip(), kind=kind,
        vendor=vendor, integration=integration, serial_number=serial, branch_id=data.branch_id,
        site_id=getattr(data, "site_id", None), timezone=zone, direction_mode=direction_mode, device_role=device_role,
        settings=dict(data.settings or {}), created_by_user_id=principal.user_id or None,
    )
    claim = attendance_devices._issue_claim(device, now)
    with db_errors.mapped():
        with db.begin_nested():
            db.add(device)
            db.flush()
    AuditLogService.log(db, principal, "Attendance", "DEVICE_REGISTERED",
                        f"{kind.title()} {device.code} registered", "attendance_device", device.id)
    return {**serialize(device, now), **claim}


def first_contact(db: Session, principal: Principal, device_id: int) -> dict:
    """The activation dialog's proof-of-possession panel (AS-SEC-02)."""
    device = _get(db, principal.company_id, device_id)
    if device.kind != "TERMINAL" or not device.serial_number:
        return {"required": False}
    row = ingest.quarantine_row(device.serial_number)
    if row is None or row["status"] == "BLOCKED":
        return {"required": True, "seen": False}
    fresh = row["last_seen_at"] >= _now() - timedelta(hours=proto.PROOF_OF_POSSESSION_HOURS)
    return {"required": True, "seen": fresh, "first_seen_at": _iso(row["first_seen_at"]),
            "last_seen_at": _iso(row["last_seen_at"]), "ip_hash": ingest.masked_ip_hash(row["ip_hash"]),
            "payload_count": row["payload_count"]}


def _issue_secret(db: Session, device: AttendanceDevice) -> str:
    from app.payroll.services import keys

    secret = ingest.new_secret()
    device.secret_hash = ingest.secret_digest(secret)
    if device.integration == "AGENT":
        sealed = keys.encrypt_field(db, device.company_id, secret, "DEVICE_SECRET",
                                    aad=f"attendance_devices.secret:{device.id}".encode("ascii"))
        device.secret_enc = sealed.encode("ascii")
        device.secret_key_id = keys.active_key(db, device.company_id, "DEVICE_SECRET").key_id[:32]
    else:
        device.secret_enc, device.secret_key_id = None, None
    return secret


def _connection(device: AttendanceDevice, secret: str) -> dict:
    if device.integration == "PUSH":
        return {"secret": secret, "serial_number": device.serial_number, "server_path": "/iclock/cdata",
                "instructions": "In the terminal's Cloud server / ADMS settings, set the server address to this "
                                "HRMS and the Comm Key to the secret below."}
    return {"secret": secret, "agent_id": device.serial_number, "server_path": "/api/v1/attendance/ingest/punches",
            "instructions": "Configure the agent with this id (header X-Device-Id) and secret (HMAC-SHA256 of the "
                            "body in X-Attendance-Signature)."}


def activate(db: Session, principal: Principal, device_id: int, claim_code: str,
             serial_confirm: Optional[str] = None) -> dict:
    from app.database.db import system_session
    from app.core.security import tokens_equal
    from app.payroll.services import step_up
    from app.services import attendance_devices

    device = _get(db, principal.company_id, device_id, lock=True)
    if device.status != "PENDING_ACTIVATION" or device.claim_code_hash is None:
        raise conflict("Only a device waiting for activation can be activated.", "INVALID_TRANSITION")
    step_up.ensure_step_up(db, principal)
    now = _now()
    if not tokens_equal(device.claim_code_hash, attendance_devices._claim_hash(claim_code)):
        device.claim_code_failed_attempts += 1
        if device.claim_code_failed_attempts >= attendance_devices.CLAIM_CODE_MAX_FAILURES:
            device.claim_code_hash, device.claim_code_expires_at = None, None
        raise AppError(422, "The claim code is not valid.", "CLAIM_CODE_INVALID")
    if device.claim_code_expires_at is None or device.claim_code_expires_at <= now:
        raise AppError(410, "This claim code has expired. Issue a new one.", "CLAIM_CODE_EXPIRED")
    if device.kind == "TERMINAL":
        if (serial_confirm or "").strip().upper() != device.serial_number:
            raise AppError(422, "Type the serial number shown on the terminal to confirm it is this device.",
                           "SERIAL_MISMATCH")
        contact = ingest.quarantine_row(device.serial_number)
        if (contact is None or contact["status"] == "BLOCKED"
                or contact["last_seen_at"] < now - timedelta(hours=proto.PROOF_OF_POSSESSION_HOURS)):
            raise AppError(422, "This terminal has not contacted the server in the last 72 hours. Point its cloud "
                                "server setting at this HRMS, wait for it to connect, then activate.",
                           "DEVICE_NOT_SEEN")
    else:
        device.serial_number = device.serial_number or f"AG{secrets.token_hex(6).upper()}"
    holder = _serial_holder(device.serial_number)
    if holder is not None and holder != (device.company_id, device.id):
        raise conflict("This serial number is already registered.", "DEVICE_SERIAL_TAKEN")
    secret = _issue_secret(db, device)
    device.status, device.activated_at = "ACTIVE", now
    device.activated_by_user_id = principal.user_id or None
    device.claim_code_hash, device.claim_code_expires_at, device.claim_code_failed_attempts = None, None, 0
    device.backfill_from = proto.backfill_from(clock.company_today(db, device.company_id))
    device.health = "UNKNOWN"
    device.row_version += 1
    db.flush()
    with system_session() as sdb:
        sdb.execute(insert(DeviceRegistryIndex)
                    .values(serial_number=device.serial_number, company_id=device.company_id, device_id=device.id,
                            activated_at=now, active=True)
                    .on_conflict_do_update(index_elements=["serial_number"], set_={
                        "company_id": device.company_id, "device_id": device.id, "activated_at": now,
                        "active": True}))
        sdb.query(DeviceQuarantine).filter(DeviceQuarantine.serial_number == device.serial_number).update(
            {"status": "CLAIMED", "claimed_company_id": device.company_id}, synchronize_session=False)
        sdb.commit()
    AuditLogService.log(db, principal, "Attendance", "DEVICE_ACTIVATED", f"{device.kind.title()} {device.code} activated",
                        "attendance_device", device.id)
    return {**serialize(device, now), "connection": _connection(device, secret)}


def rotate_secret(db: Session, principal: Principal, device_id: int) -> dict:
    from app.payroll.services import step_up

    device = _get(db, principal.company_id, device_id, lock=True)
    if device.status not in ("ACTIVE", "SUSPENDED"):
        raise conflict("Only an activated device has a secret.", "INVALID_TRANSITION")
    step_up.ensure_step_up(db, principal)
    secret = _issue_secret(db, device)
    device.row_version += 1
    AuditLogService.log(db, principal, "Attendance", "DEVICE_SECRET_ROTATED", f"Secret of {device.code} rotated",
                        "attendance_device", device.id)
    return {**serialize(device), "connection": _connection(device, secret)}


TRANSITIONS = {
    "suspend": (("ACTIVE",), "SUSPENDED", "DEVICE_SUSPENDED"),
    "resume": (("SUSPENDED",), "ACTIVE", "DEVICE_ACTIVATED"),
    "retire": (("PENDING_ACTIVATION", "ACTIVE", "SUSPENDED"), "RETIRED", "DEVICE_RETIRED"),
}


def transition(db: Session, principal: Principal, device: AttendanceDevice, action: str, reason: Optional[str]) -> dict:
    from app.database.db import system_session

    sources, target, audit = TRANSITIONS[action]
    if device.status not in sources:
        raise conflict(f"A {device.status.lower().replace('_', ' ')} device cannot be {target.lower()}.",
                       "INVALID_TRANSITION")
    now = _now()
    device.status = target
    if target == "RETIRED":
        device.retired_at = now
        device.secret_hash, device.secret_enc, device.secret_key_id = None, None, None
        device.claim_code_hash, device.claim_code_expires_at = None, None
        if device.serial_number:
            with system_session() as sdb:
                sdb.query(DeviceRegistryIndex).filter(
                    DeviceRegistryIndex.serial_number == device.serial_number,
                    DeviceRegistryIndex.company_id == device.company_id,
                    DeviceRegistryIndex.device_id == device.id).update({"active": False}, synchronize_session=False)
                sdb.commit()
        _cancel_open_commands(db, device, now)
    device.row_version += 1
    AuditLogService.log(db, principal, "Attendance", audit, f"{device.kind.title()} {device.code} {target.lower()}",
                        "attendance_device", device.id, details={"reason": reason} if reason else None)
    return serialize(device, now)


def update_device(db: Session, principal: Principal, device_id: int, data: Mapping[str, Any],
                  expected_version: Optional[int]) -> dict:
    device = _get(db, principal.company_id, device_id, lock=True)
    if expected_version is not None and expected_version != device.row_version:
        raise conflict("The device changed since you opened it.", "ROW_VERSION_CONFLICT")
    settings = data.get("settings")
    if isinstance(settings, dict) and any(k in settings for k in ("ATTLOGStamp", "OPERLOGStamp")):
        raise AppError(422, "Protocol stamps are managed by the server.", "INVALID_FIELDS")  # SP-AS-069
    for field in ("name", "direction_mode", "device_role", "timezone", "branch_id", "site_id"):
        if field in data and data[field] is not None:
            setattr(device, field, data[field])
    if isinstance(settings, dict):
        device.settings = dict(settings)
    if device.timezone not in pytz.all_timezones_set:
        raise bad_request("Unknown time zone.", "INVALID_TIMEZONE")
    device.row_version += 1
    AuditLogService.log(db, principal, "Attendance", "DEVICE_UPDATED", f"Device {device.code} updated",
                        "attendance_device", device.id)
    return serialize(device)


def device_summary(db: Session, principal: Principal, device_id: int) -> dict:
    """Detail-sheet counters: punches, unmapped, rejects today, last batch."""
    device = _get(db, principal.company_id, device_id)
    today_start = _now().replace(hour=0, minute=0, second=0)
    punches = db.query(func.count(AttendancePunch.id)).filter(AttendancePunch.company_id == device.company_id,
                                                              AttendancePunch.device_id == device.id).scalar()
    unmapped = (db.query(func.count(func.distinct(AttendancePunch.device_user_id)))
                .filter(AttendancePunch.company_id == device.company_id, AttendancePunch.device_id == device.id,
                        AttendancePunch.mapping_status != "MAPPED").scalar())
    rejects = (db.query(func.count(AttendanceIngestReject.id))
               .filter(AttendanceIngestReject.company_id == device.company_id,
                       AttendanceIngestReject.device_id == device.id, AttendanceIngestReject.status == "OPEN").scalar())
    rejects_today = (db.query(func.count(AttendanceIngestReject.id))
                     .filter(AttendanceIngestReject.company_id == device.company_id,
                             AttendanceIngestReject.device_id == device.id,
                             AttendanceIngestReject.created_at >= today_start).scalar())
    recent = (db.query(AttendancePunch).filter(AttendancePunch.company_id == device.company_id,
                                               AttendancePunch.device_id == device.id)
              .order_by(AttendancePunch.occurred_at.desc()).limit(10).all())
    names = _names(db, device.company_id, {p.employee_id for p in recent if p.employee_id})
    return {
        "device": serialize(device), "punches": punches, "unmapped_user_ids": unmapped, "open_rejects": rejects,
        "rejected_today": rejects_today,
        "recent": [{"id": p.id, "occurred_at": _iso(p.occurred_at), "device_user_id": p.device_user_id,
                    "employee": names.get(p.employee_id), "direction": p.direction, "verify_mode": p.verify_mode,
                    "mapping_status": p.mapping_status, "disposition": p.disposition,
                    "evidence_flags": list(p.evidence_flags or [])} for p in recent],
    }


def _names(db: Session, company_id: int, ids) -> Dict[int, dict]:
    if not ids:
        return {}
    rows = db.query(Employee).filter(Employee.company_id == company_id, Employee.id.in_(list(ids))).all()
    return {e.id: {"id": e.id, "name": e.full_name, "code": e.employee_code} for e in rows}


# ------------------------------------------------------------------------------------------------- mappings
def _mapping_dict(m: AttendanceDeviceUserMapping, names: Dict[int, dict]) -> dict:
    return {"id": m.id, "device_id": m.device_id, "device_user_id": m.device_user_id,
            "device_user_label": m.device_user_label or m.device_user_id, "employee_id": m.employee_id,
            "employee": names.get(m.employee_id), "effective_from": _iso(m.effective_from),
            "effective_to": _iso(m.effective_to), "sync_state": m.sync_state, "row_version": m.row_version}


def list_mappings(db: Session, principal: Principal, *, device_id: Optional[int] = None, q: Optional[str] = None,
                  skip: int = 0, limit: int = 50) -> dict:
    query = db.query(AttendanceDeviceUserMapping).filter(
        AttendanceDeviceUserMapping.company_id == principal.company_id)
    if device_id is not None:
        query = query.filter(or_(AttendanceDeviceUserMapping.device_id == device_id,
                                 AttendanceDeviceUserMapping.device_id.is_(None)))
    if q:
        like = f"%{q.strip()}%"
        ids = [e for (e,) in db.query(Employee.id).filter(
            Employee.company_id == principal.company_id,
            or_(Employee.first_name.ilike(like), Employee.last_name.ilike(like), Employee.employee_code.ilike(like)))]
        query = query.filter(or_(AttendanceDeviceUserMapping.device_user_id.ilike(like),
                                 AttendanceDeviceUserMapping.employee_id.in_(ids or [0])))
    total = query.count()
    rows = (query.order_by(AttendanceDeviceUserMapping.device_user_id, AttendanceDeviceUserMapping.effective_from)
            .offset(skip).limit(limit).all())
    names = _names(db, principal.company_id, {m.employee_id for m in rows})
    unmapped = (db.query(AttendancePunch.device_id, AttendancePunch.device_user_id, func.count(AttendancePunch.id),
                         func.min(AttendancePunch.occurred_at), func.max(AttendancePunch.occurred_at))
                .filter(AttendancePunch.company_id == principal.company_id,
                        AttendancePunch.mapping_status != "MAPPED",
                        *([AttendancePunch.device_id == device_id] if device_id is not None else []))
                .group_by(AttendancePunch.device_id, AttendancePunch.device_user_id)
                .order_by(AttendancePunch.device_user_id).limit(200).all())
    return {"items": [_mapping_dict(m, names) for m in rows], "total": total, "skip": skip, "limit": limit,
            "unmapped": [{"device_id": d, "device_user_id": u, "count": c, "first_at": _iso(f), "last_at": _iso(l)}
                         for d, u, c, f, l in unmapped]}


def _vendor_for(db: Session, company_id: int, device_id: Optional[int]) -> Optional[str]:
    if device_id is None:
        vendors = {v for (v,) in db.query(AttendanceDevice.vendor).filter(
            AttendanceDevice.company_id == company_id, AttendanceDevice.kind.in_(ingest.INGEST_KINDS),
            AttendanceDevice.retired_at.is_(None))}
        return "ZKTECO" if vendors and vendors <= set(proto.NUMERIC_VENDORS) else None
    device = _get(db, company_id, device_id)
    return device.vendor


def _overlaps(db: Session, company_id: int, device_id: Optional[int], user_id: str, start: date, end: Optional[date],
              exclude_id: Optional[int] = None) -> Optional[AttendanceDeviceUserMapping]:
    query = db.query(AttendanceDeviceUserMapping).filter(
        AttendanceDeviceUserMapping.company_id == company_id, AttendanceDeviceUserMapping.device_user_id == user_id,
        (AttendanceDeviceUserMapping.device_id.is_(None) if device_id is None
         else AttendanceDeviceUserMapping.device_id == device_id),
        or_(AttendanceDeviceUserMapping.effective_to.is_(None), AttendanceDeviceUserMapping.effective_to >= start))
    if end is not None:
        query = query.filter(AttendanceDeviceUserMapping.effective_from <= end)
    if exclude_id is not None:
        query = query.filter(AttendanceDeviceUserMapping.id != exclude_id)
    return query.first()


def save_mappings(db: Session, principal: Principal, items: Sequence[Mapping[str, Any]]) -> dict:
    """M-9 bulk create (all or nothing); every row is checked before any write."""
    if not items:
        raise bad_request("Add at least one mapping.", "INVALID_FIELDS")
    if len(items) > MAX_MAPPINGS:
        raise bad_request(f"At most {MAX_MAPPINGS} mappings per request.", "TOO_MANY_ITEMS")
    company_id = principal.company_id
    prepared = []
    errors = []
    seen: Dict[Tuple[Optional[int], str], List[Tuple[date, Optional[date]]]] = defaultdict(list)
    for index, item in enumerate(items):
        device_id = item.get("device_id")
        try:
            vendor = _vendor_for(db, company_id, device_id)
            user_id = proto.canonical_user_id(vendor, item.get("device_user_id"))
        except proto.InvalidUserId as exc:
            errors.append({"index": index, "code": "INVALID_FIELDS", "message": str(exc)})
            continue
        employee = db.query(Employee).filter(Employee.company_id == company_id,
                                             Employee.id == item.get("employee_id")).first()
        if employee is None:
            errors.append({"index": index, "code": "NOT_FOUND", "message": "Employee not found."})
            continue
        start = item.get("effective_from") or clock.company_today(db, company_id)
        end = item.get("effective_to")
        if end is not None and end < start:
            errors.append({"index": index, "code": "INVALID_FIELDS", "message": "The end is before the start."})
            continue
        clash = _overlaps(db, company_id, device_id, user_id, start, end)
        in_batch = any(s <= (end or date.max) and (e or date.max) >= start for s, e in seen[(device_id, user_id)])
        if clash is not None or in_batch:
            errors.append({"index": index, "code": "MAPPING_OVERLAP",
                           "message": f"Device user {user_id} is already mapped for these dates."})
            continue
        seen[(device_id, user_id)].append((start, end))
        prepared.append((device_id, user_id, str(item.get("device_user_id")).strip()[:32], employee, start, end))
    if errors:
        status = 409 if all(e["code"] == "MAPPING_OVERLAP" for e in errors) else 400
        raise AppError(status, errors[0]["message"], "MAPPING_OVERLAP" if status == 409 else "INVALID_FIELDS",
                       extra={"issues": errors})
    created = []
    affected: Dict[int, set] = defaultdict(set)
    for device_id, user_id, label, employee, start, end in prepared:
        device = _get(db, company_id, device_id) if device_id is not None else None
        sync = "PENDING" if device is not None and device.integration == "PUSH" else "NOT_APPLICABLE"
        row = AttendanceDeviceUserMapping(
            company_id=company_id, device_id=device_id, device_user_id=user_id,
            device_user_label=label if label != user_id else None, employee_id=employee.id, effective_from=start,
            effective_to=end, sync_state=sync, created_by_user_id=principal.user_id or None)
        db.add(row)
        db.flush()
        if sync == "PENDING":
            _queue(db, principal, device, "UPLOAD_USER", {"device_user_id": user_id, "name": employee.full_name},
                   mapping_id=row.id)
        for day in _apply_mapping(db, row):
            affected[employee.id].add(day)
        created.append(row)
    for employee_id, days in affected.items():
        ingest.pair_dates(db, company_id, employee_id, days, trigger="MAPPING_CHANGED")
    names = _names(db, company_id, {m.employee_id for m in created})
    AuditLogService.log(db, principal, "Attendance", "MAPPING_SAVED", f"{len(created)} device mapping(s) saved",
                        "attendance_device_user_mapping", created[0].id if created else None,
                        details={"count": len(created), "punches_mapped": sum(len(d) for d in affected.values())})
    return {"items": [_mapping_dict(m, names) for m in created],
            "mapped_dates": sum(len(d) for d in affected.values())}


def _apply_mapping(db: Session, mapping: AttendanceDeviceUserMapping) -> set:
    """M-7: NULL → value on the matching UNMAPPED / AMBIGUOUS punches in the mapping's dates; resolves the items."""
    query = db.query(AttendancePunch).filter(
        AttendancePunch.company_id == mapping.company_id, AttendancePunch.mapping_status != "MAPPED",
        AttendancePunch.employee_id.is_(None), AttendancePunch.device_user_id == mapping.device_user_id)
    if mapping.device_id is not None:
        query = query.filter(AttendancePunch.device_id == mapping.device_id)
    days = set()
    devices_touched = set()
    for p in query.all():
        local_day = (p.device_local_ts.date() if p.device_local_ts is not None
                     else p.occurred_at.date())
        if local_day < mapping.effective_from or (mapping.effective_to is not None and local_day > mapping.effective_to):
            continue
        p.employee_id = mapping.employee_id
        p.mapping_status = "MAPPED"
        devices_touched.add(p.device_id)
        if "FUTURE_DATED" not in (p.evidence_flags or []):
            days.add(local_day)
    db.flush()
    now = _now()
    for device_id in devices_touched:
        remaining = db.query(AttendancePunch.id).filter(
            AttendancePunch.company_id == mapping.company_id, AttendancePunch.device_id == device_id,
            AttendancePunch.device_user_id == mapping.device_user_id,
            AttendancePunch.mapping_status != "MAPPED").first()
        if remaining is None:
            (db.query(AttendanceReviewItem)
             .filter(AttendanceReviewItem.company_id == mapping.company_id,
                     AttendanceReviewItem.kind == "UNMAPPED_PUNCH", AttendanceReviewItem.status == "OPEN",
                     AttendanceReviewItem.dedup_key == f"UNMAPPED_PUNCH:{device_id or 0}:{mapping.device_user_id}")
             .update({"status": "RESOLVED", "decided_at": now, "note": "Mapped to an employee"},
                     synchronize_session=False))
    return days


def end_mapping(db: Session, principal: Principal, mapping_id: int, effective_to: date,
                expected_version: Optional[int]) -> dict:
    row = db.query(AttendanceDeviceUserMapping).filter(
        AttendanceDeviceUserMapping.company_id == principal.company_id,
        AttendanceDeviceUserMapping.id == mapping_id).with_for_update().first()
    if row is None:
        raise not_found("Mapping")
    if expected_version is not None and expected_version != row.row_version:
        raise conflict("The mapping changed since you opened it.", "ROW_VERSION_CONFLICT")
    if effective_to < row.effective_from:
        raise bad_request("The end is before the start.", "INVALID_FIELDS")
    row.effective_to = effective_to
    row.row_version += 1
    if row.device_id is not None:
        device = _get(db, principal.company_id, row.device_id)
        if device.integration == "PUSH" and device.status == "ACTIVE":
            _queue(db, principal, device, "DELETE_USER", {"device_user_id": row.device_user_id})  # E-3
    AuditLogService.log(db, principal, "Attendance", "MAPPING_SAVED", f"Mapping {row.device_user_id} ended",
                        "attendance_device_user_mapping", row.id, details={"effective_to": effective_to.isoformat()})
    return _mapping_dict(row, _names(db, principal.company_id, {row.employee_id}))


def auto_match(db: Session, principal: Principal, device_id: Optional[int], effective_from: Optional[date],
               dry_run: bool = False) -> dict:
    """M-9 "Map by employee code": explicit rows for every active employee whose code fits; skipped codes listed."""
    company_id = principal.company_id
    vendor = _vendor_for(db, company_id, device_id)
    start = effective_from or clock.company_today(db, company_id)
    employees = (db.query(Employee).filter(Employee.company_id == company_id, Employee.is_active.is_(True))
                 .order_by(Employee.employee_code).all())
    items, skipped = [], []
    for e in employees:
        try:
            user_id = proto.canonical_user_id(vendor, e.employee_code)
        except proto.InvalidUserId:
            skipped.append({"employee_id": e.id, "employee_code": e.employee_code, "reason": "NOT_NUMERIC"})
            continue
        existing = _overlaps(db, company_id, device_id, user_id, start, None)
        mine = db.query(AttendanceDeviceUserMapping.id).filter(
            AttendanceDeviceUserMapping.company_id == company_id, AttendanceDeviceUserMapping.employee_id == e.id,
            (AttendanceDeviceUserMapping.device_id.is_(None) if device_id is None
             else AttendanceDeviceUserMapping.device_id == device_id),
            or_(AttendanceDeviceUserMapping.effective_to.is_(None),
                AttendanceDeviceUserMapping.effective_to >= start)).first()
        if existing is not None or mine is not None:
            skipped.append({"employee_id": e.id, "employee_code": e.employee_code,
                            "reason": "ALREADY_MAPPED" if mine is not None else "ID_TAKEN"})
            continue
        items.append({"device_id": device_id, "device_user_id": e.employee_code, "employee_id": e.id,
                      "effective_from": start, "employee_name": e.full_name, "canonical": user_id})
    if dry_run or not items:
        return {"dry_run": True, "would_create": len(items), "skipped": skipped,
                "items": [{**i, "effective_from": start.isoformat()} for i in items[:MAX_MAPPINGS]]}
    saved = save_mappings(db, principal, items[:MAX_MAPPINGS])
    return {"dry_run": False, "created": len(saved["items"]), "skipped": skipped, "items": saved["items"],
            "mapped_dates": saved["mapped_dates"]}


# ------------------------------------------------------------------------------------------------- commands
def _command_dict(c: AttendanceDeviceCommand) -> dict:
    return {"id": c.id, "device_id": c.device_id, "command": c.command, "payload": c.payload, "status": c.status,
            "return_code": c.return_code, "expires_at": _iso(c.expires_at), "sent_at": _iso(c.sent_at),
            "acked_at": _iso(c.acked_at), "created_at": _iso(c.created_at), "row_version": c.row_version}


def _queue(db: Session, principal: Optional[Principal], device: AttendanceDevice, command: str, payload: dict,
           mapping_id: Optional[int] = None) -> AttendanceDeviceCommand:
    if command not in DEVICE_COMMANDS:  # SR-AS-04
        raise bad_request("This command is not allowed.", "INVALID_FIELDS")
    allowed_keys = {"UPLOAD_USER": ("device_user_id", "name", "card"), "DISABLE_USER": ("device_user_id",),
                    "DELETE_USER": ("device_user_id",), "RESEND_LOGS": ("from", "to")}.get(command, ())
    clean = {k: str(v)[:40] for k, v in (payload or {}).items() if k in allowed_keys and v not in (None, "")}
    if mapping_id is not None:
        clean["mapping_id"] = mapping_id
    row = AttendanceDeviceCommand(company_id=device.company_id, device_id=device.id, command=command, payload=clean,
                                  status="QUEUED", expires_at=_now() + proto.COMMAND_TTL,
                                  requested_by_user_id=principal.user_id if principal else None)
    db.add(row)
    db.flush()
    AuditLogService.log(db, principal, "Attendance", "DEVICE_COMMAND_QUEUED", f"{command} queued for {device.code}",
                        "attendance_device", device.id, company_id=device.company_id,
                        details={"command_id": row.id, "command": command})
    return row


def queue_command(db: Session, principal: Principal, device_id: int, command: str, payload: dict) -> dict:
    device = _get(db, principal.company_id, device_id)
    if device.integration != "PUSH":
        raise AppError(422, "Commands are sent to push terminals only.", "FEATURE_DISABLED")
    if device.status != "ACTIVE":
        raise conflict("Commands are queued for active devices only.", "INVALID_TRANSITION")
    return _command_dict(_queue(db, principal, device, command, payload))


def list_commands(db: Session, principal: Principal, device_id: int) -> dict:
    device = _get(db, principal.company_id, device_id)
    _expire(db, device, _now())
    rows = (db.query(AttendanceDeviceCommand).filter(AttendanceDeviceCommand.company_id == device.company_id,
                                                     AttendanceDeviceCommand.device_id == device.id)
            .order_by(AttendanceDeviceCommand.id.desc()).limit(50).all())
    return {"items": [_command_dict(c) for c in rows]}


def cancel_command(db: Session, principal: Principal, device_id: int, command_id: int,
                   expected_version: Optional[int]) -> dict:
    row = (db.query(AttendanceDeviceCommand)
           .filter(AttendanceDeviceCommand.company_id == principal.company_id,
                   AttendanceDeviceCommand.device_id == device_id, AttendanceDeviceCommand.id == command_id)
           .with_for_update().first())
    if row is None:
        raise not_found("Command")
    if expected_version is not None and expected_version != row.row_version:
        raise conflict("The command changed since you opened it.", "ROW_VERSION_CONFLICT")
    if row.status != "QUEUED":
        raise conflict("Only a queued command can be cancelled.", "INVALID_TRANSITION")
    row.status = "CANCELLED"
    row.row_version += 1
    return _command_dict(row)


def _expire(db: Session, device: AttendanceDevice, now: datetime) -> None:
    (db.query(AttendanceDeviceCommand)
     .filter(AttendanceDeviceCommand.company_id == device.company_id, AttendanceDeviceCommand.device_id == device.id,
             AttendanceDeviceCommand.status.in_(("QUEUED", "SENT")), AttendanceDeviceCommand.expires_at <= now)
     .update({"status": "EXPIRED"}, synchronize_session=False))


def _cancel_open_commands(db: Session, device: AttendanceDevice, now: datetime) -> None:
    (db.query(AttendanceDeviceCommand)
     .filter(AttendanceDeviceCommand.company_id == device.company_id, AttendanceDeviceCommand.device_id == device.id,
             AttendanceDeviceCommand.status == "QUEUED")
     .update({"status": "CANCELLED"}, synchronize_session=False))


def pending_command_lines(db: Session, device: AttendanceDevice, now: datetime) -> List[str]:
    """``getrequest``: QUEUED commands become SENT, one ``C:<id>:<text>`` line each (E delivery)."""
    _expire(db, device, now)
    rows = (db.query(AttendanceDeviceCommand)
            .filter(AttendanceDeviceCommand.company_id == device.company_id,
                    AttendanceDeviceCommand.device_id == device.id, AttendanceDeviceCommand.status == "QUEUED")
            .order_by(AttendanceDeviceCommand.id).limit(20).all())
    local_now = now.astimezone(pytz.timezone(device.timezone)).replace(tzinfo=None)
    lines = []
    for row in rows:
        lines.append(proto.command_line(row.id, row.command, row.payload or {}, device_now_local=local_now))
        row.status, row.sent_at = "SENT", now
    return lines


def command_results(db: Session, device: AttendanceDevice, results: Sequence[Tuple[int, str]], now: datetime) -> int:
    """``devicecmd``: ``Return=0`` -> ACKED, else FAILED; UPLOAD_USER results update the mapping's sync state."""
    done = 0
    for command_id, code in results:
        row = (db.query(AttendanceDeviceCommand)
               .filter(AttendanceDeviceCommand.company_id == device.company_id,
                       AttendanceDeviceCommand.device_id == device.id, AttendanceDeviceCommand.id == command_id,
                       AttendanceDeviceCommand.status.in_(("SENT", "QUEUED"))).first())
        if row is None:
            continue
        ok = code in ("0", "")
        row.status, row.return_code, row.acked_at = ("ACKED" if ok else "FAILED"), code or "0", now
        row.row_version += 1
        mapping_id = (row.payload or {}).get("mapping_id")
        if row.command == "UPLOAD_USER" and mapping_id:
            (db.query(AttendanceDeviceUserMapping)
             .filter(AttendanceDeviceUserMapping.company_id == device.company_id,
                     AttendanceDeviceUserMapping.id == int(mapping_id))
             .update({"sync_state": "SYNCED" if ok else "FAILED"}, synchronize_session=False))
        done += 1
    return done


# ------------------------------------------------------------------------------------------------- rejects
def list_rejects(db: Session, principal: Principal, device_id: int, status: str = "OPEN") -> dict:
    device = _get(db, principal.company_id, device_id)
    rows = (db.query(AttendanceIngestReject)
            .filter(AttendanceIngestReject.company_id == device.company_id,
                    AttendanceIngestReject.device_id == device.id, AttendanceIngestReject.status == status)
            .order_by(AttendanceIngestReject.id.desc()).limit(100).all())
    return {"items": [{"id": r.id, "line_no": r.line_no, "reason": r.reason, "payload_hash": r.payload_hash,
                       "ingest_batch_id": r.ingest_batch_id, "status": r.status, "created_at": _iso(r.created_at)}
                      for r in rows]}


def reveal_reject(db: Session, principal: Principal, device_id: int, reject_id: int) -> dict:
    from app.payroll.services import keys, step_up

    device = _get(db, principal.company_id, device_id)
    row = db.query(AttendanceIngestReject).filter(AttendanceIngestReject.company_id == device.company_id,
                                                  AttendanceIngestReject.device_id == device.id,
                                                  AttendanceIngestReject.id == reject_id).first()
    if row is None:
        raise not_found("Rejected line")
    step_up.ensure_step_up(db, principal)
    raw = keys.decrypt_field(db, device.company_id, row.raw_line_enc, "DEVICE_SECRET",
                             aad=f"attendance_ingest_rejects.raw_line:{device.id}".encode("ascii")) \
        if row.raw_line_enc else None
    AuditLogService.log(db, principal, "Attendance", "EVIDENCE_VIEWED", f"Rejected line {row.id} viewed",
                        "attendance_ingest_reject", row.id)
    return {"id": row.id, "raw_line": raw, "reason": row.reason}


def dismiss_reject(db: Session, principal: Principal, device_id: int, reject_id: int) -> dict:
    device = _get(db, principal.company_id, device_id)
    row = db.query(AttendanceIngestReject).filter(AttendanceIngestReject.company_id == device.company_id,
                                                  AttendanceIngestReject.device_id == device.id,
                                                  AttendanceIngestReject.id == reject_id).with_for_update().first()
    if row is None:
        raise not_found("Rejected line")
    if row.status != "OPEN":
        raise conflict("This line was already handled.", "INVALID_TRANSITION")
    row.status, row.resolved_by_user_id, row.resolved_at = "DISMISSED", principal.user_id or None, _now()
    return {"id": row.id, "status": row.status}


# ------------------------------------------------------------------------------------------------- CSV imports
TEMPLATE = "employee_code,device_user_id,date,time,direction,timezone,note\nE001,,2026-10-27,09:02,IN,,\n"


def _read_csv(content: bytes) -> List[Dict[str, str]]:
    if len(content) > MAX_IMPORT_BYTES:
        raise AppError(413, "The file is larger than 5 MB.", "FILE_TOO_LARGE")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise bad_request("Save the file as CSV (UTF-8).", "INVALID_FILE")
    reader = csv.DictReader(io.StringIO(text))
    headers = {(h or "").strip().lower() for h in (reader.fieldnames or [])}
    if not {"date", "time"} <= headers or not ({"employee_code", "device_user_id"} & headers):
        raise bad_request("The file needs the columns employee_code or device_user_id, date and time.",
                          "INVALID_FILE")
    rows = []
    for raw in reader:
        rows.append({(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k is not None})
        if len(rows) > MAX_IMPORT_ROWS:
            raise bad_request(f"At most {MAX_IMPORT_ROWS} rows per file.", "TOO_MANY_ITEMS")
    return rows


def _plan_row(db: Session, company_id: int, n: int, row: Dict[str, str], codes: Dict[str, Employee],
              index: ingest.MappingIndex, company_zone: str) -> dict:
    """One dry-run row: resolution, instant, natural key and issues (§8.6)."""
    issues: List[str] = []
    code = row.get("employee_code") or ""
    pin = row.get("device_user_id") or ""
    employee: Optional[Employee] = None
    user_id: Optional[str] = None
    status = "MAPPED"
    try:
        day = date.fromisoformat(row.get("date") or "")
        clock_text = row.get("time") or ""
        local = datetime.combine(day, time.fromisoformat(clock_text if len(clock_text) > 5 else clock_text + ":00"))
    except ValueError:
        return {"row": n, "valid_time": False, "issues": ["INVALID_TIME"], "employee_code": code,
                "device_user_id": pin, "date": row.get("date"), "time": row.get("time")}
    direction = (row.get("direction") or "").upper() or "UNKNOWN"
    if direction not in DIRECTIONS + ("UNKNOWN",):
        issues.append("INVALID_DIRECTION")
        direction = "UNKNOWN"
    if code:
        employee = codes.get(code.upper())
        if employee is None:
            status = "UNMAPPED"
            issues.append("UNMAPPED")
            user_id = proto.lenient_user_id(None, code)
    elif pin:
        user_id = proto.lenient_user_id(None, pin)
        resolved, status = index.resolve(user_id, day)
        if resolved is not None:
            employee = db.get(Employee, resolved)
        else:
            issues.append("UNMAPPED")
    else:
        return {"row": n, "valid_time": False, "issues": ["MISSING_PERSON"], "date": row.get("date"),
                "time": row.get("time")}
    zone = (row.get("timezone") or "").strip()
    if zone and zone not in pytz.all_timezones_set:
        issues.append("INVALID_TIMEZONE")
        zone = ""
    if not zone:
        zone = ingest._employee_zone(db, employee, day) if employee is not None else company_zone
    localizer = proto.Localizer(zone)
    try:
        pytz.timezone(zone).localize(local, is_dst=None)
    except pytz.exceptions.AmbiguousTimeError:
        issues.append("AMBIGUOUS_LOCAL_TIME")
    except pytz.exceptions.NonExistentTimeError:
        issues.append("NONEXISTENT_LOCAL_TIME")
    occurred_at, _ = localizer.instant(local)
    if employee is not None and (day < employee.hire_date or (employee.termination_date and day > employee.termination_date)):
        issues.append("OUTSIDE_EMPLOYMENT")
    key = proto.import_natural_key(company_id, employee.id if employee else None, user_id, direction, occurred_at)
    return {"row": n, "valid_time": True, "employee_id": employee.id if employee else None,
            "employee": {"name": employee.full_name, "code": employee.employee_code} if employee else None,
            "employee_code": code or None, "device_user_id": user_id, "date": day.isoformat(),
            "time": local.strftime("%H:%M:%S"), "timezone": zone, "direction": direction,
            "occurred_at": occurred_at.isoformat(), "mapping_status": status, "natural_key": key,
            "note": (row.get("note") or "")[:200] or None, "issues": issues}


def import_dry_run(db: Session, principal: Principal, file_name: str, content: bytes) -> dict:
    company_id = principal.company_id
    rows = _read_csv(content)
    codes = {e.employee_code.upper(): e for e in db.query(Employee).filter(Employee.company_id == company_id)}
    pins = {proto.lenient_user_id(None, r["device_user_id"]) for r in rows if r.get("device_user_id")}
    index = ingest.MappingIndex(db, company_id, None, pins)
    from app.models.company import Company

    company_zone = db.get(Company, company_id).timezone or "UTC"
    planned = [_plan_row(db, company_id, n, row, codes, index, company_zone) for n, row in enumerate(rows, start=2)]
    keys = [p["natural_key"] for p in planned if p.get("natural_key")]
    existing = {k for (k,) in db.query(AttendancePunch.natural_key).filter(
        AttendancePunch.company_id == company_id, AttendancePunch.natural_key.in_(keys or [""]))}
    seen: Dict[str, int] = {}
    for p in planned:
        key = p.get("natural_key")
        if not key:
            continue
        if key in existing:
            p["issues"].append("DUPLICATE_EXISTING")
        if key in seen:
            p["issues"].append("DUPLICATE_IN_FILE")
        seen.setdefault(key, p["row"])
    totals = {"rows": len(planned), "valid": sum(1 for p in planned if p["valid_time"]),
              "invalid": sum(1 for p in planned if not p["valid_time"]),
              "unmapped": sum(1 for p in planned if "UNMAPPED" in p["issues"]),
              "duplicates": sum(1 for p in planned if {"DUPLICATE_EXISTING", "DUPLICATE_IN_FILE"} & set(p["issues"]))}
    record = AttendanceImport(company_id=company_id, file_name=(file_name or "punches.csv")[:200], status="DRY_RUN",
                              rows=planned, totals=totals, created_by_user_id=principal.user_id or None)
    db.add(record)
    db.flush()
    return _import_dict(record)


def _import_dict(record: AttendanceImport) -> dict:
    return {"id": record.id, "file_name": record.file_name, "status": record.status, "totals": record.totals,
            "rows": record.rows, "created_at": _iso(record.created_at), "committed_at": _iso(record.committed_at)}


def get_import(db: Session, principal: Principal, import_id: int) -> dict:
    record = db.query(AttendanceImport).filter(AttendanceImport.company_id == principal.company_id,
                                               AttendanceImport.id == import_id).first()
    if record is None:
        raise not_found("Import")
    return _import_dict(record)


def import_commit(db: Session, principal: Principal, import_id: int) -> dict:
    from app.services import attendance_review

    company_id = principal.company_id
    record = db.query(AttendanceImport).filter(AttendanceImport.company_id == company_id,
                                               AttendanceImport.id == import_id).with_for_update().first()
    if record is None:
        raise not_found("Import")
    if record.status == "COMMITTED":
        return {**_import_dict(record), "replayed": True}
    now = _now()
    inserted = duplicates = 0
    to_pair: Dict[int, set] = defaultdict(set)
    unmapped: set = set()
    for p in record.rows:
        if not p.get("valid_time"):
            continue
        occurred_at = datetime.fromisoformat(p["occurred_at"])
        employee_id = p.get("employee_id")
        values = dict(
            company_id=company_id, employee_id=employee_id, source="IMPORT", direction=p["direction"],
            direction_raw=p["direction"] if p["direction"] != "UNKNOWN" else None, occurred_at=occurred_at,
            received_at=now, device_user_id=p.get("device_user_id"), geofence_result="NOT_CHECKED",
            evidence_flags=["AMBIGUOUS_LOCAL_TIME"] if "AMBIGUOUS_LOCAL_TIME" in p["issues"] else [],
            natural_key=p["natural_key"], payload_hash=proto.payload_hash(f"{record.id}:{p['row']}"),
            ingest_batch_id=f"import:{record.id}", mapping_status="MAPPED" if employee_id else "UNMAPPED",
            disposition="PENDING", created_at=now, updated_at=now)
        row = db.execute(insert(AttendancePunch).values(**values)
                         .on_conflict_do_nothing(index_elements=["company_id", "natural_key"])
                         .returning(AttendancePunch.id)).first()
        if row is None:
            duplicates += 1
            continue
        inserted += 1
        if employee_id:
            to_pair[employee_id].add(date.fromisoformat(p["date"]))
        elif p.get("device_user_id"):
            unmapped.add(p["device_user_id"])
    for user_id in sorted(unmapped):
        attendance_review.open_item(db, company_id, "UNMAPPED_PUNCH", {"device_id": None, "device_user_id": user_id})
    record.status, record.committed_at, record.committed_by_user_id = "COMMITTED", now, principal.user_id or None
    record.totals = {**(record.totals or {}), "inserted": inserted, "duplicates_at_commit": duplicates}
    db.flush()
    for employee_id, days in to_pair.items():
        ingest.pair_dates(db, company_id, employee_id, days, trigger="BACKFILL")
    AuditLogService.log(db, principal, "Attendance", "PUNCH_RECORDED",
                        f"CSV import {record.id}: {inserted} punch(es) stored", "attendance_import", record.id,
                        details={"import_id": record.id, "count": inserted})
    return _import_dict(record)

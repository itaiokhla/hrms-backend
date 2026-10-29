"""Device ingest, pairing and watermarks (AS-M7, G-17; 04-attendance/01 §7.2, §7.3, §8; 03 §3).

* **Tenant resolution (H-1).** A serial (PUSH) or agent id (AGENT) is looked up in the platform table
  ``device_registry_index`` through ``system_session()``; the tenant session is bound before any tenant row is
  written. An unknown serial is upserted into ``device_quarantine`` (first / last contact, salted IP hash, payload
  count) and answered exactly like a wrong secret: 401 ``DEVICE_AUTH_FAILED`` (no existence oracle, I-4).
* **Authentication (H-2).** PUSH: the secret is compared in constant time with ``secret_hash``. AGENT: the HMAC-SHA256
  of the raw body is checked with the secret decrypted from ``secret_enc`` (key purpose ``DEVICE_SECRET``).
  ``SUSPENDED`` -> 403; ``PENDING_ACTIVATION`` / ``RETIRED`` -> 401; a v1 tenant -> 404 (v2-only routes).
* **Save-before-ACK (§8.3).** ``ingest`` stores one ``attendance_punches`` row per record (``source = DEVICE``,
  ``disposition = PENDING``, ``ON CONFLICT (company_id, natural_key) DO NOTHING``); malformed lines go to
  ``attendance_ingest_rejects`` with the raw line encrypted (H-8). The route commits each chunk and answers ``OK``
  only after the last commit.
* **Pairing (I-5, 03 §3).** ``pair_dates`` turns the DEVICE / IMPORT punches of an (employee, shift date) into one
  device session (``pairing_key``): near duplicates within 60 s -> ``IGNORED_DUPLICATE``; FIRST_LAST picks the
  earliest IN-capable and the latest OUT-capable punch (device roles respected); BREAK_START / BREAK_END pairs inside
  become breaks; inner punches -> ``IGNORED_WINDOW``; a lone OUT -> ``ORPHAN``; dates outside hire / termination ->
  ``OUTSIDE_EMPLOYMENT``; ``FUTURE_DATED`` punches stay ``PENDING`` (W-1a). Then the day is re-evaluated (``PAIRING``).
* **Watermarks (§8.8).** ``device_lag`` gives the evaluator ``device_ids_behind`` and ``late_log_grace_elapsed``
  for (employee, date): relevant ACTIVE devices whose ``watermark_at`` is before the end of the day's last instance
  keep the day ``PROVISIONAL`` (``DEVICE_SYNC``) until the 48 h grace (from the end of the day when the device was
  online then, else from when it was seen again), bounded by 7 days.

Services never commit; routes do.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import pytz
from sqlalchemy import and_, or_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError
from app.core.security import hash_token, tokens_equal
from app.models.attendance import Attendance, AttendanceBreak, AttendanceDevice, AttendancePunch
from app.models.devices import AttendanceDeviceUserMapping, AttendanceIngestReject, DeviceQuarantine, DeviceRegistryIndex
from app.models.employee import Employee
from app.services import device_protocol as proto

INGEST_KINDS = ("TERMINAL", "AGENT")
PAIRED_SOURCES = ("DEVICE", "IMPORT")
REPAIRABLE = ("PENDING", "USED", "IGNORED_DUPLICATE", "IGNORED_WINDOW", "ORPHAN", "OUTSIDE_EMPLOYMENT")


def _now() -> datetime:
    return clock.utcnow().replace(microsecond=0)


def auth_failed() -> AppError:
    return AppError(401, "DEVICE_AUTH_FAILED", "DEVICE_AUTH_FAILED")


# ------------------------------------------------------------------------------------------------- secrets
def new_secret() -> str:
    return secrets.token_urlsafe(24)


def secret_digest(secret: str) -> str:
    return hash_token("device-secret:" + (secret or ""))


def secret_matches(device: AttendanceDevice, secret: Optional[str]) -> bool:
    return bool(device.secret_hash) and bool(secret) and tokens_equal(device.secret_hash, secret_digest(secret))


def ip_digest(ip: Optional[str]) -> Optional[str]:
    """Platform-level salted IP hash for quarantine rows (no tenant yet)."""
    if not ip or ip == "unknown":
        return None
    from app.config import settings

    return hashlib.sha256(f"platform|{settings.secret_key}|{ip}".encode("utf-8")).hexdigest()


def masked_ip_hash(value: Optional[str]) -> Optional[str]:
    return f"{value[:4]}…{value[-4:]}" if value else None


# ------------------------------------------------------------------------------------------------- platform side
def resolve_serial(serial: str) -> Optional[Tuple[int, int]]:
    """H-1: (company_id, device_id) of an activated serial or agent id, read with the system session."""
    from app.database.db import system_session

    key = (serial or "").strip()[:64]
    if not key:
        return None
    with system_session() as sdb:
        row = (sdb.query(DeviceRegistryIndex.company_id, DeviceRegistryIndex.device_id)
               .filter(DeviceRegistryIndex.serial_number == key, DeviceRegistryIndex.active.is_(True)).first())
    return (row[0], row[1]) if row else None


def quarantine(serial: str, ip: Optional[str], vendor_guess: Optional[str] = None, payload_lines: int = 0) -> None:
    """Upsert the platform quarantine row of an unknown serial (no tenant rows, I-4)."""
    from app.database.db import system_session

    key = (serial or "").strip()[:64]
    if not key:
        return
    now = _now()
    with system_session() as sdb:
        sdb.execute(
            insert(DeviceQuarantine)
            .values(serial_number=key, vendor_guess=vendor_guess, first_seen_at=now, last_seen_at=now,
                    last_ip_hash=ip_digest(ip), payload_count=payload_lines, status="SEEN")
            .on_conflict_do_update(index_elements=["serial_number"], set_={
                "last_seen_at": now, "last_ip_hash": ip_digest(ip),
                "payload_count": DeviceQuarantine.payload_count + payload_lines}))
        sdb.commit()


def quarantine_row(serial: str) -> Optional[dict]:
    from app.database.db import system_session

    with system_session() as sdb:
        row = sdb.query(DeviceQuarantine).filter(DeviceQuarantine.serial_number == (serial or "").strip()).first()
        if row is None:
            return None
        return {"serial_number": row.serial_number, "first_seen_at": row.first_seen_at, "last_seen_at": row.last_seen_at,
                "ip_hash": row.last_ip_hash, "status": row.status, "payload_count": row.payload_count,
                "vendor_guess": row.vendor_guess, "claimed_company_id": row.claimed_company_id}


# ------------------------------------------------------------------------------------------------- tenant side
def load_device(db: Session, company_id: int, device_id: int, *, secret: Optional[str] = None,
                integration: str = "PUSH") -> AttendanceDevice:
    """H-2 on a tenant-bound session (status after authentication; v1 tenants -> 404)."""
    from app.services import attendance_access

    device = db.query(AttendanceDevice).filter(AttendanceDevice.company_id == company_id,
                                               AttendanceDevice.id == device_id).with_for_update().first()
    if device is None or device.kind not in INGEST_KINDS or device.integration != integration:
        raise auth_failed()
    if integration == "PUSH" and not secret_matches(device, secret):
        raise auth_failed()
    if not attendance_access.v2_enabled(db, company_id):
        raise AppError(404, "Not found", "NOT_FOUND")
    if device.status == "SUSPENDED":
        raise AppError(403, "DEVICE_SUSPENDED", "DEVICE_SUSPENDED")
    if device.status != "ACTIVE":
        raise auth_failed()
    return device


def agent_signature_ok(db: Session, device: AttendanceDevice, body: bytes, signature: Optional[str]) -> bool:
    """J-1: hex HMAC-SHA256 of the raw body with the decrypted agent secret."""
    from app.payroll.services import keys

    if not device.secret_enc or not signature:
        return False
    secret = keys.decrypt_field(db, device.company_id, device.secret_enc.decode("ascii"), "DEVICE_SECRET",
                                aad=f"attendance_devices.secret:{device.id}".encode("ascii"))
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


def seen(device: AttendanceDevice, now: datetime, *, handshake: bool = False) -> bool:
    """Mark an authenticated call (``last_seen_at``); returns whether the device was in real time before it.

    T-4 / GV-AS-CAP-12: a handshake opens a connection, it does not prove the device was pushing — the first upload
    after a handshake (typically the back-fill of a device that was offline or just activated) is a backlog, never
    a clock-skew measurement. Only a previous upload, poll or ping within 3 × the heartbeat makes it real time."""
    previous = device.last_seen_at
    state = dict(device.protocol_state or {})
    after_handshake = previous is not None and state.get("handshake_at") == previous.isoformat()
    device.last_seen_at = now
    if handshake:
        state["handshake_at"] = now.isoformat()
        device.protocol_state = state
    return (previous is not None and not after_handshake
            and now - previous <= timedelta(seconds=3 * proto.HEARTBEAT_SECONDS))


def handshake(device: AttendanceDevice, pushver: Optional[str]) -> str:
    """H-3 reply: the stored stamps (0 at first activation) and attendance-only transfer flags."""
    if pushver:
        device.protocol_version = pushver[:32]
    state = dict(device.protocol_state or {})
    return "\n".join([
        f"GET OPTION FROM: {device.serial_number}",
        f"ATTLOGStamp={state.get('ATTLOGStamp', 0)}",
        f"OPERLOGStamp={state.get('OPERLOGStamp', 0)}",
        "ATTPHOTOStamp=0",
        "ErrorDelay=60",
        f"Delay={proto.HEARTBEAT_SECONDS}",
        "TransTimes=00:00;14:05",
        "TransInterval=1",
        "TransFlag=TransData AttLog\tOpLog",
        "Realtime=1",
        "Encrypt=None",
        "ServerVer=2.4.1",
        "PushProtVer=2.4.1",
        f"TransMaxCount={proto.CHUNK_LINES}",
    ]) + "\n"


def store_stamp(device: AttendanceDevice, table: str, stamp: Optional[str]) -> None:
    if not stamp or not stamp.strip().isdigit():
        return
    state = dict(device.protocol_state or {})
    state[f"{table}Stamp"] = int(stamp.strip())
    device.protocol_state = state


@dataclass
class IngestResult:
    accepted: int = 0
    inserted: int = 0
    duplicates: int = 0
    rejected: int = 0
    skipped_backfill: int = 0
    unmapped: int = 0
    future_dated: int = 0
    to_pair: Set[Tuple[int, date]] = field(default_factory=set)
    records: List[dict] = field(default_factory=list)

    def add(self, other: "IngestResult") -> None:
        for name in ("accepted", "inserted", "duplicates", "rejected", "skipped_backfill", "unmapped", "future_dated"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.to_pair |= other.to_pair
        self.records.extend(other.records)

    def as_dict(self) -> dict:
        return {"accepted": self.accepted, "inserted": self.inserted, "duplicates": self.duplicates,
                "rejected": self.rejected, "skipped_backfill": self.skipped_backfill, "unmapped": self.unmapped,
                "future_dated": self.future_dated}


class MappingIndex:
    """M-4 resolution for one device: device-specific mapping first, then tenant-wide; else UNMAPPED."""

    def __init__(self, db: Session, company_id: int, device_id: Optional[int], user_ids: Iterable[str]):
        ids = sorted(set(user_ids))
        self.rows: Dict[str, List[AttendanceDeviceUserMapping]] = defaultdict(list)
        if ids:
            query = db.query(AttendanceDeviceUserMapping).filter(
                AttendanceDeviceUserMapping.company_id == company_id,
                AttendanceDeviceUserMapping.device_user_id.in_(ids))
            if device_id is None:
                query = query.filter(AttendanceDeviceUserMapping.device_id.is_(None))
            else:
                query = query.filter(or_(AttendanceDeviceUserMapping.device_id == device_id,
                                         AttendanceDeviceUserMapping.device_id.is_(None)))
            for row in query:
                self.rows[row.device_user_id].append(row)

    def resolve(self, user_id: str, day: date) -> Tuple[Optional[int], str]:
        active = [m for m in self.rows.get(user_id, ())
                  if m.effective_from <= day and (m.effective_to is None or m.effective_to >= day)]
        specific = [m for m in active if m.device_id is not None]
        if specific:
            return specific[0].employee_id, "MAPPED"
        wide = [m for m in active if m.device_id is None]
        if wide:
            return wide[0].employee_id, "MAPPED"
        return None, "UNMAPPED"


def _encrypt_line(db: Session, company_id: int, device_id: Optional[int], raw: str) -> Optional[str]:
    from app.payroll.services import keys

    return keys.encrypt_field(db, company_id, raw[:2000], "DEVICE_SECRET",
                              aad=f"attendance_ingest_rejects.raw_line:{device_id or 0}".encode("ascii"))


def store_reject(db: Session, device: AttendanceDevice, batch_id: str, reject: proto.Reject) -> None:
    """H-8 holding row; a resend of the same bad line (P-2) keeps the one OPEN row."""
    digest = proto.payload_hash(reject.raw)
    if db.query(AttendanceIngestReject.id).filter(
            AttendanceIngestReject.company_id == device.company_id, AttendanceIngestReject.device_id == device.id,
            AttendanceIngestReject.payload_hash == digest, AttendanceIngestReject.status == "OPEN").first():
        return
    db.add(AttendanceIngestReject(
        company_id=device.company_id, device_id=device.id, ingest_batch_id=batch_id[:64], line_no=reject.line_no,
        payload_hash=proto.payload_hash(reject.raw), raw_line_enc=_encrypt_line(db, device.company_id, device.id,
                                                                                reject.raw),
        reason=reject.reason[:60], status="OPEN"))


def ingest(db: Session, device: AttendanceDevice, lines: Sequence[proto.AttLine], rejects: Sequence[proto.Reject], *,
           batch_id: str, received_at: datetime, localizer: proto.Localizer,
           explicit_instants: Optional[Dict[int, datetime]] = None) -> IngestResult:
    """§8.3 steps 1-5 for one chunk (the caller commits, then replies)."""
    from app.services import attendance_review

    result = IngestResult()
    for reject in rejects:
        store_reject(db, device, batch_id, reject)
        result.rejected += 1
        result.records.append({"line_no": reject.line_no, "result": "REJECTED", "code": reject.reason})
    users = {}
    for line in lines:
        try:
            users[line.line_no] = proto.lenient_user_id(device.vendor, line.pin)
        except proto.InvalidUserId:
            users[line.line_no] = None
    index = MappingIndex(db, device.company_id, device.id, [u for u in users.values() if u])
    skew_limit = timedelta(seconds=proto.MAX_CLOCK_SKEW_SECONDS)
    late_limit = timedelta(hours=proto.LATE_LOG_GRACE_HOURS)
    newest: Optional[datetime] = None
    watermark = device.watermark_at
    unmapped: Set[str] = set()
    for line in lines:
        user_id = users.get(line.line_no)
        if user_id is None:
            reject = proto.Reject(line.line_no, line.raw, "INVALID_PIN")
            store_reject(db, device, batch_id, reject)
            result.rejected += 1
            result.records.append({"line_no": line.line_no, "result": "REJECTED", "code": reject.reason})
            continue
        result.accepted += 1
        if device.backfill_from is not None and line.local_ts.date() < device.backfill_from:
            result.skipped_backfill += 1  # H-3: pre-activation history, counted, importable by CSV
            result.records.append({"line_no": line.line_no, "result": "SKIPPED_BACKFILL"})
            continue
        if explicit_instants and line.line_no in explicit_instants:
            occurred_at, flags = explicit_instants[line.line_no], ()
        else:
            occurred_at, flags = localizer.instant(line.local_ts)
        flags = list(flags)
        future = occurred_at > received_at + skew_limit
        if future:
            flags.append("FUTURE_DATED")  # W-1a: stored, never advances the watermark, never paired
            result.future_dated += 1
        elif received_at - occurred_at > late_limit:
            flags.append("LATE_LOG")
        employee_id, mapping_status = index.resolve(user_id, line.local_ts.date())
        key = proto.device_natural_key(device.company_id, device.id, user_id, line.local_ts, line.verify, line.status)
        values = dict(
            company_id=device.company_id, employee_id=employee_id, source="DEVICE",
            direction=proto.direction_of(device.direction_mode, line.status),
            direction_raw=str(line.status) if line.status is not None else None,
            occurred_at=occurred_at, device_local_ts=line.local_ts, device_tz=device.timezone, received_at=received_at,
            device_id=device.id, device_user_id=user_id, verify_mode=proto.verify_mode_of(line.verify),
            verify_raw=line.verify if line.verify is not None and -32768 <= line.verify <= 32767 else None,
            work_code=line.work_code, site_id=device.site_id, geofence_result="NOT_CHECKED",
            evidence_flags=sorted(set(flags)), natural_key=key, payload_hash=proto.payload_hash(line.raw),
            ingest_batch_id=batch_id[:64], mapping_status=mapping_status, disposition="PENDING",
            created_at=received_at, updated_at=received_at)
        inserted = db.execute(insert(AttendancePunch).values(**values)
                              .on_conflict_do_nothing(index_elements=["company_id", "natural_key"])
                              .returning(AttendancePunch.id)).first()
        if inserted is None:
            result.duplicates += 1
            result.records.append({"line_no": line.line_no, "result": "DUPLICATE"})
            continue
        result.inserted += 1
        result.records.append({"line_no": line.line_no, "result": "INSERTED", "punch_id": inserted[0]})
        if not future:
            newest = occurred_at if newest is None else max(newest, occurred_at)
            watermark = occurred_at if watermark is None else max(watermark, occurred_at)
        if employee_id is None:
            result.unmapped += 1
            unmapped.add(user_id)
        elif not future:
            result.to_pair.add((employee_id, line.local_ts.date()))
    for user_id in sorted(unmapped):  # M-6: one OPEN item per (device, device user id)
        attendance_review.open_item(db, device.company_id, "UNMAPPED_PUNCH",
                                    {"device_id": device.id, "device_user_id": user_id})
    if newest is not None:
        device.last_punch_at = max(device.last_punch_at or newest, newest)
    if watermark is not None:
        device.watermark_at = min(watermark, received_at)  # W-1a: never beyond received_at
    if localizer.real_time and newest is not None:
        skew = int((received_at - newest).total_seconds())
        device.last_clock_skew_seconds = skew if abs(skew) > proto.MAX_CLOCK_SKEW_SECONDS else 0
        if abs(skew) > proto.MAX_CLOCK_SKEW_SECONDS:  # T-4 (AS-DEV-06): real-time uploads only
            attendance_review.open_item(db, device.company_id, "DEVICE_CLOCK_SKEW", {"device_id": device.id})
    if result.future_dated:
        attendance_review.open_item(db, device.company_id, "DEVICE_CLOCK_SKEW", {"device_id": device.id})
    return result


def advance_idle_watermark(device: AttendanceDevice, now: datetime, real_time: bool) -> None:
    """W-1: a poll from an ONLINE real-time device with no pending upload advances the watermark."""
    if not real_time:
        return
    candidate = now - timedelta(seconds=proto.MAX_CLOCK_SKEW_SECONDS)
    if device.watermark_at is None or candidate > device.watermark_at:
        device.watermark_at = candidate


# ------------------------------------------------------------------------------------------------- pairing
def _employee_zone(db: Session, employee: Employee, day: date) -> str:
    from app.services import attendance_service

    return attendance_service.work_zone(db, employee, day).zone


def _shift_date_of(instances, at: datetime, direction: str, zone: str) -> date:
    from app.services import punch_pairing

    day, _ = punch_pairing.shift_date(instances, at, "IN" if direction == "UNKNOWN" else direction, zone)
    return day


def pairing_key(employee_id: int, day: date) -> str:
    return f"dev:{employee_id}:{day.isoformat()}"


def pair_dates(db: Session, company_id: int, employee_id: int, dates: Iterable[date], *,
               trigger: str = "PAIRING", evaluate: bool = True) -> dict:
    """Re-pair the DEVICE / IMPORT punches of the employee on each shift date (idempotent)."""
    from app.services import attendance_days

    employee = db.get(Employee, employee_id)
    if employee is None or employee.company_id != company_id:
        return {"paired": 0}
    done = []
    for day in sorted(set(dates)):
        done.append(_pair_one(db, company_id, employee, day))
    if evaluate and done:
        attendance_days.evaluate_dates(db, company_id, employee_id, [d["date"] for d in done], trigger=trigger)
    return {"paired": len(done), "dates": [{**d, "date": d["date"].isoformat()} for d in done]}


def _pair_one(db: Session, company_id: int, employee: Employee, day: date) -> dict:
    from app.services import attendance_service as svc
    from app.services import attendance_sessions

    zone = _employee_zone(db, employee, day)
    tz = pytz.timezone(zone)
    lo = tz.localize(datetime.combine(day - timedelta(days=1), time.min)).astimezone(pytz.utc)
    hi = tz.localize(datetime.combine(day + timedelta(days=2), time.min)).astimezone(pytz.utc)
    rows = (db.query(AttendancePunch)
            .filter(AttendancePunch.company_id == company_id, AttendancePunch.employee_id == employee.id,
                    AttendancePunch.source.in_(PAIRED_SOURCES), AttendancePunch.disposition.in_(REPAIRABLE),
                    or_(AttendancePunch.shift_date == day,
                        and_(AttendancePunch.occurred_at >= lo, AttendancePunch.occurred_at < hi)))
            .order_by(AttendancePunch.occurred_at, AttendancePunch.id).all())
    instances = attendance_sessions.instances_around(db, company_id, employee, day)
    mine: List[AttendancePunch] = []
    for p in rows:
        if "FUTURE_DATED" in (p.evidence_flags or []):
            continue  # W-1a: stays PENDING
        if _shift_date_of(instances, p.occurred_at, p.direction, zone) == day:
            mine.append(p)
    pkey = pairing_key(employee.id, day)
    session = (db.query(Attendance).filter(Attendance.company_id == company_id, Attendance.employee_id == employee.id,
                                           Attendance.pairing_key == pkey, Attendance.voided_at.is_(None)).first())
    outcome = {"date": day, "punches": len(mine), "session_id": None}
    employed = employee.hire_date <= day and (employee.termination_date is None or day <= employee.termination_date)
    if not employed:
        for p in mine:
            _set(p, "OUTSIDE_EMPLOYMENT", None, day)
        _void(db, session)
        return outcome
    devices = {d.id: d for d in db.query(AttendanceDevice).filter(
        AttendanceDevice.company_id == company_id,
        AttendanceDevice.id.in_({p.device_id for p in mine if p.device_id})).all()} if mine else {}

    def role(p: AttendancePunch) -> str:
        device = devices.get(p.device_id)
        return device.device_role if device is not None else "IN_OUT"

    kept: List[AttendancePunch] = []
    window = timedelta(seconds=proto.DOUBLE_PUNCH_WINDOW_SECONDS)
    for p in mine:
        prev = kept[-1] if kept else None
        if prev is not None and p.occurred_at - prev.occurred_at <= window and (
                p.direction == prev.direction or "UNKNOWN" in (p.direction, prev.direction)):
            _set(p, "IGNORED_DUPLICATE", None, day)
            continue
        kept.append(p)
    first_i, last_i = proto.first_last([(p.occurred_at, p.direction, role(p)) for p in kept])
    if first_i is None:
        for p in kept:
            _set(p, "ORPHAN", None, day)
        if kept:
            from app.services import attendance_review

            attendance_review.open_item(db, company_id, "ORPHAN_PUNCH", {"punch_id": kept[0].id,
                                                                         "employee_id": employee.id,
                                                                         "shift_date": day})
        _void(db, session)
        return outcome
    first = kept[first_i]
    last = kept[last_i] if last_i is not None else None
    if session is None:
        others = [s for s in attendance_sessions.sessions_on(db, company_id, employee.id, day) if s.pairing_key is None]
        end = last.occurred_at if last is not None else None
        clash = [s for s in others if (s.occupied_until is None or s.occupied_until > first.occurred_at)
                 and (end is None or s.check_in_time < end)]
        if clash:  # an interactive session already covers this time: punches stay PENDING (explain pane)
            for p in kept:
                _set(p, "PENDING", None, day)
            outcome["conflict"] = "SESSION_EXISTS"
            return outcome
        seq = max([s.session_seq for s in others] or [0]) + 1
        session = Attendance(company_id=company_id, employee_id=employee.id, date=day, shift_id=employee.shift_id,
                             check_in_time=first.occurred_at, status="present", source="device",
                             capture_source="DEVICE", is_manual=False, session_seq=seq, pairing_key=pkey,
                             work_timezone=zone)
        created = True
    else:
        created = False
    session.check_in_time = first.occurred_at
    session.check_out_time = last.occurred_at if last is not None else None
    session.first_punch_id, session.last_punch_id = first.id, (last.id if last is not None else first.id)
    session.closed_at, session.closed_reason = None, None
    session.occupied_until = session.check_out_time
    if session.check_out_time is None and day < clock.company_today(db, company_id) - timedelta(days=1):
        from app.services import attendance_policy

        session.closed_at, session.closed_reason = _now(), "DEVICE_NO_PUNCH_OUT"
        session.occupied_until = attendance_sessions.occupied_until(
            session, attendance_policy.load(db, company_id).max_shift_hours)
    try:
        if created:
            svc._insert_day(db, session)
        else:
            with db.begin_nested():
                db.flush()
    except AppError:
        db.expire_all()
        outcome["conflict"] = "SESSION_OVERLAP"
        return outcome
    _rebuild_breaks(db, session, kept[first_i + 1:last_i] if last_i is not None else [])
    company = db.get(svc.Company, company_id)
    svc.apply_timing(db, company, session)
    for i, p in enumerate(kept):
        used = i in (first_i, last_i) or (p.direction in ("BREAK_START", "BREAK_END") and last_i is not None
                                          and first_i < i < last_i)
        _set(p, "USED" if used else "IGNORED_WINDOW", session.id if used else None, day)
    svc.record_version(db, session, "CREATED" if created else "UPDATED", None, bump=not created)
    outcome["session_id"] = session.id
    return outcome


def _rebuild_breaks(db: Session, session: Attendance, inner: Sequence[AttendancePunch]) -> None:
    db.query(AttendanceBreak).filter(AttendanceBreak.company_id == session.company_id,
                                     AttendanceBreak.attendance_id == session.id,
                                     AttendanceBreak.start_punch_id.isnot(None)).delete(synchronize_session=False)
    start: Optional[AttendancePunch] = None
    for p in inner:
        if p.direction == "BREAK_START" and start is None:
            start = p
        elif p.direction == "BREAK_END" and start is not None:
            db.add(AttendanceBreak(company_id=session.company_id, attendance_id=session.id,
                                   employee_id=session.employee_id, kind="REST", started_at=start.occurred_at,
                                   ended_at=p.occurred_at, is_paid=False, end_reason="DEVICE",
                                   source="PUNCHED", start_punch_id=start.id, end_punch_id=p.id))
            start = None
    db.flush()


def _set(p: AttendancePunch, disposition: str, session_id: Optional[int], day: date) -> None:
    if p.disposition != disposition:
        p.disposition = disposition
    if p.session_id != session_id:
        p.session_id = session_id
    if p.shift_date != day:
        p.shift_date = day


def _void(db: Session, session: Optional[Attendance]) -> None:
    from app.services import attendance_service as svc

    if session is None:
        return
    for p in db.query(AttendancePunch).filter(AttendancePunch.company_id == session.company_id,
                                              AttendancePunch.session_id == session.id):
        p.session_id = None
    session.voided_at, session.void_reason = _now(), "REPAIRED"
    session.occupied_until = session.occupied_until or session.check_in_time
    svc.record_version(db, session, "VOIDED", None)


# ------------------------------------------------------------------------------------------------- watermarks
def has_ingest_devices(db: Session, company_id: int) -> bool:
    return db.query(AttendanceDevice.id).filter(AttendanceDevice.company_id == company_id,
                                                AttendanceDevice.kind.in_(INGEST_KINDS),
                                                AttendanceDevice.status == "ACTIVE").first() is not None


def device_lag(db: Session, company_id: int, employee_ids: Sequence[int], start: date, end: date,
               day_end: Dict[Tuple[int, date], datetime], branch_of: Dict[Tuple[int, date], Optional[int]],
               now: datetime) -> Dict[Tuple[int, date], Tuple[Tuple[int, ...], bool]]:
    """W-2 / W-3: {(employee, date): (devices behind, grace elapsed)} for the pairs with a relevant device behind."""
    devices = (db.query(AttendanceDevice).filter(AttendanceDevice.company_id == company_id,
                                                 AttendanceDevice.kind.in_(INGEST_KINDS),
                                                 AttendanceDevice.status == "ACTIVE").all())
    if not devices:
        return {}
    by_id = {d.id: d for d in devices}
    mappings = (db.query(AttendanceDeviceUserMapping)
                .filter(AttendanceDeviceUserMapping.company_id == company_id,
                        AttendanceDeviceUserMapping.employee_id.in_(list(employee_ids)),
                        AttendanceDeviceUserMapping.effective_from <= end,
                        or_(AttendanceDeviceUserMapping.effective_to.is_(None),
                            AttendanceDeviceUserMapping.effective_to >= start)).all())
    by_employee: Dict[int, List[AttendanceDeviceUserMapping]] = defaultdict(list)
    for m in mappings:
        by_employee[m.employee_id].append(m)
    result = {}
    grace = timedelta(hours=proto.LATE_LOG_GRACE_HOURS)
    cap = timedelta(days=proto.MAX_PROVISIONAL_DAYS)
    online = timedelta(minutes=proto.OFFLINE_AFTER_MINUTES)
    for (employee_id, day), t_end in day_end.items():
        relevant: Set[int] = set()
        for m in by_employee.get(employee_id, ()):
            if m.effective_from > day or (m.effective_to is not None and m.effective_to < day):
                continue
            if m.device_id is not None:
                if m.device_id in by_id:
                    relevant.add(m.device_id)
            else:
                branch = branch_of.get((employee_id, day))
                relevant |= {d.id for d in devices if d.branch_id is None or d.branch_id == branch}
        behind = sorted(i for i in relevant if by_id[i].watermark_at is None or by_id[i].watermark_at < t_end)
        if not behind:
            continue
        # The grace runs from T_end for a device seen around or after T_end; a device offline at T_end and not seen
        # since has not started it. Every wait is capped at T_end + max_provisional_days.
        elapsed = now >= t_end + cap or all(
            by_id[i].last_seen_at is not None and by_id[i].last_seen_at >= t_end - online and now >= t_end + grace
            for i in behind)
        result[(employee_id, day)] = (tuple(behind), elapsed)
    return result


def parse_agent_body(raw: bytes) -> dict:
    if len(raw) > 2 * 1024 * 1024:
        raise AppError(413, "The batch is too large.", "INGEST_PAYLOAD_INVALID")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise AppError(400, "The batch is not valid JSON.", "INGEST_PAYLOAD_INVALID")
    if not isinstance(body, dict) or not isinstance(body.get("records"), list):
        raise AppError(400, "The batch needs ingest_batch_id and records.", "INGEST_PAYLOAD_INVALID")
    if len(body["records"]) > proto.MAX_AGENT_RECORDS:
        raise AppError(400, "At most 1000 records per batch.", "TOO_MANY_ITEMS")
    batch_id = str(body.get("ingest_batch_id") or "").strip()
    if not batch_id or len(batch_id) > 64:
        raise AppError(400, "The batch needs an ingest_batch_id (at most 64 characters).", "INGEST_PAYLOAD_INVALID")
    return body


def agent_lines(records: Sequence[dict]) -> Tuple[List[proto.AttLine], List[proto.Reject], Dict[int, datetime]]:
    """J-1 / J-2: agent records as ATTLOG lines; an explicit UTC offset converts with that offset."""
    lines, rejects, explicit = [], [], {}
    for n, record in enumerate(records, start=1):
        raw = json.dumps(record, sort_keys=True, default=str)[:2000]
        if not isinstance(record, dict) or not str(record.get("device_user_id") or "").strip():
            rejects.append(proto.Reject(n, raw, "MISSING_PIN"))
            continue
        ts = str(record.get("device_local_ts") or "")
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            rejects.append(proto.Reject(n, raw, "INVALID_DATETIME"))
            continue
        if parsed.tzinfo is not None:
            explicit[n] = parsed.astimezone(pytz.utc)
            parsed = parsed.replace(tzinfo=None)
        try:
            status = int(record["direction_raw"]) if record.get("direction_raw") not in (None, "") else None
            verify = int(record["verify_raw"]) if record.get("verify_raw") not in (None, "") else None
        except (TypeError, ValueError):
            rejects.append(proto.Reject(n, raw, "INVALID_STATUS"))
            continue
        work = record.get("work_code")
        lines.append(proto.AttLine(n, raw, str(record["device_user_id"]).strip(), parsed.replace(microsecond=0),
                                   status, verify, str(work)[:16] if work not in (None, "", "0") else None))
    return lines, rejects, explicit

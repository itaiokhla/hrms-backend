"""Device protocol helpers (AS-M7; 04-attendance/01 §7, §8) — pure functions, no database (L0).

* ``canonical_user_id`` — M-11 (AS-DEV-11): trimmed, NFKC, upper-cased; digits without leading zeros for the numeric
  vendors (ZKTECO, ESSL);
* ``parse_attlog`` — H-5: tab-separated ``PIN, DateTime, Status, Verify, [WorkCode, …]`` lines; a bad line becomes a
  ``Reject`` (H-8), never a punch;
* ``to_instant`` — T-1 / T-2 / T-3: device-local wall time in the device zone (pytz), the repeated DST hour resolved
  in log order, a non-existent spring-forward time read with the offset before the change and flagged;
* ``direction_of`` — ``DEVICE_STATE`` maps the ATTLOG status (0-5) at capture; other modes leave ``UNKNOWN`` and
  pairing decides (P-5b);
* ``device_natural_key`` / ``import_natural_key`` — P-2 / P-5a;
* ``health`` — §7.2 (ONLINE / STALE / OFFLINE / CLOCK_SKEW / UNKNOWN);
* ``command_line`` — the ADMS text of an allow-listed command (E-1..E-5; exact firmware strings are ``[UNVERIFIED]``).
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

import pytz

NUMERIC_VENDORS = ("ZKTECO", "ESSL")
MAX_PUSH_BYTES = 2 * 1024 * 1024  # AS-DEV-15
MAX_PUSH_LINES = 20_000
CHUNK_LINES = 500
MAX_AGENT_RECORDS = 1_000
# Documented defaults of attendance.device.* (SP-AS-212 / -214 / -177); the registry keys arrive with the device
# settings section (FOLLOW-UPS).
OFFLINE_AFTER_MINUTES = 30
MAX_CLOCK_SKEW_SECONDS = 120
LATE_LOG_GRACE_HOURS = 48
MAX_PROVISIONAL_DAYS = 7
ACTIVATION_BACKFILL_DAYS = 7
DOUBLE_PUNCH_WINDOW_SECONDS = 60
HEARTBEAT_SECONDS = 30  # the handshake Delay
PROOF_OF_POSSESSION_HOURS = 72
COMMAND_TTL = timedelta(hours=24)

# ATTLOG Status -> direction under DEVICE_STATE (0 check-in, 1 check-out, 2 break-out, 3 break-in, 4 OT-in, 5 OT-out).
STATE_DIRECTIONS = {0: "IN", 1: "OUT", 2: "BREAK_START", 3: "BREAK_END", 4: "IN", 5: "OUT"}
# ZK / eSSL verify codes (gap-device-integration D3; [UNVERIFIED] per firmware); unknown -> OTHER.
VERIFY_MODES = {0: "PASSWORD", 1: "FINGER", 2: "CARD", 3: "PASSWORD", 4: "CARD", 15: "FACE", 25: "FACE"}


class InvalidUserId(ValueError):
    pass


def canonical_user_id(vendor: Optional[str], raw: object) -> str:
    """M-11: one canonical form for storage, matching and the overlap check (``00417`` -> ``417`` on ZK / eSSL)."""
    text = unicodedata.normalize("NFKC", str(raw or "")).strip().upper()
    if not text:
        raise InvalidUserId("empty device user id")
    if len(text) > 32:
        raise InvalidUserId("device user id longer than 32 characters")
    if vendor in NUMERIC_VENDORS:
        if not text.isdigit():
            raise InvalidUserId("this device type accepts digits only")
        return text.lstrip("0") or "0"
    return text


def lenient_user_id(vendor: Optional[str], raw: object) -> str:
    """Ingest form: canonical when possible, else the trimmed upper-cased value (I-2: nothing is dropped)."""
    try:
        return canonical_user_id(vendor, raw)
    except InvalidUserId:
        text = unicodedata.normalize("NFKC", str(raw or "")).strip().upper()[:32]
        if not text:
            raise
        return text


@dataclass(frozen=True)
class AttLine:
    line_no: int
    raw: str
    pin: str
    local_ts: datetime
    status: Optional[int]
    verify: Optional[int]
    work_code: Optional[str]


@dataclass(frozen=True)
class Reject:
    line_no: int
    raw: str
    reason: str


def _int_or_none(value: str) -> Optional[int]:
    value = (value or "").strip()
    if not value:
        return None
    if not value.lstrip("-").isdigit():
        raise ValueError(value)
    return int(value)


def parse_local_ts(value: str) -> datetime:
    value = (value or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(value)


def parse_attlog(body: str, start_line: int = 1) -> Tuple[List[AttLine], List[Reject]]:
    """H-5 / H-8: punches and rejects of an ATTLOG body (blank lines skipped)."""
    lines: List[AttLine] = []
    rejects: List[Reject] = []
    for offset, raw in enumerate(body.splitlines()):
        line_no = start_line + offset
        text = raw.strip("\r\n")
        if not text.strip():
            continue
        parts = text.split("\t")
        if len(parts) < 2 or not parts[0].strip():
            rejects.append(Reject(line_no, text, "MISSING_PIN" if not parts[0].strip() else "TOO_FEW_FIELDS"))
            continue
        try:
            local_ts = parse_local_ts(parts[1])
        except ValueError:
            rejects.append(Reject(line_no, text, "INVALID_DATETIME"))
            continue
        try:
            status = _int_or_none(parts[2]) if len(parts) > 2 else None
            verify = _int_or_none(parts[3]) if len(parts) > 3 else None
        except ValueError:
            rejects.append(Reject(line_no, text, "INVALID_STATUS"))
            continue
        if status is not None and not (0 <= status <= 255):
            rejects.append(Reject(line_no, text, "INVALID_STATUS"))
            continue
        work_code = (parts[4].strip() or None) if len(parts) > 4 else None
        lines.append(AttLine(line_no, text, parts[0].strip(), local_ts, status, verify,
                             work_code[:16] if work_code and work_code != "0" else None))
    return lines, rejects


@dataclass
class Localizer:
    """T-2 log-order state of one upload: the first record in a repeated hour takes the first occurrence (fold 0);
    a later record whose local time goes back inside that hour, and every record after it there, take fold 1."""

    zone: str
    real_time: bool = False
    received_at: Optional[datetime] = None
    _fold_one: bool = False
    _last_ambiguous: Optional[datetime] = None
    flags: Dict[int, Tuple[str, ...]] = field(default_factory=dict)

    def instant(self, local: datetime) -> Tuple[datetime, Tuple[str, ...]]:
        tz = pytz.timezone(self.zone)
        try:
            return tz.localize(local, is_dst=None).astimezone(pytz.utc), ()
        except pytz.exceptions.NonExistentTimeError:
            # T-3: the offset in force before the change (standard time); the device has not applied DST.
            return tz.localize(local, is_dst=False).astimezone(pytz.utc), ("CLOCK_SKEW",)
        except pytz.exceptions.AmbiguousTimeError:
            first = tz.localize(local, is_dst=True).astimezone(pytz.utc)
            second = tz.localize(local, is_dst=False).astimezone(pytz.utc)
            if self.real_time and self.received_at is not None:
                return min((first, second), key=lambda at: abs((self.received_at - at).total_seconds())), ()
            if self._last_ambiguous is not None and local <= self._last_ambiguous:
                self._fold_one = True
            self._last_ambiguous = local
            return (second if self._fold_one else first), ()


def to_instant(local: datetime, zone: str) -> datetime:
    """A single device-local time in ``zone`` (first occurrence of a repeated hour)."""
    return Localizer(zone).instant(local)[0]


def direction_of(direction_mode: str, status: Optional[int]) -> str:
    if direction_mode == "DEVICE_STATE" and status is not None and status in STATE_DIRECTIONS:
        return STATE_DIRECTIONS[status]
    return "UNKNOWN"


def verify_mode_of(verify: Optional[int]) -> Optional[str]:
    if verify is None:
        return None
    return VERIFY_MODES.get(verify, "OTHER")


def device_natural_key(company_id: int, device_id: int, device_user_id: str, local_ts: datetime,
                       verify_raw: Optional[int], direction_raw: Optional[int]) -> str:
    """P-2: no server value, so every resend collapses onto the first row."""
    raw = f"{company_id}|{device_id}|{device_user_id}|{local_ts.isoformat()}|{verify_raw}|{direction_raw}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def import_natural_key(company_id: int, employee_id: Optional[int], device_user_id: Optional[str], direction: str,
                       occurred_at: datetime) -> str:
    """P-5a: ``coalesce(employee_id, 'u:' || canonical device user id)``, direction and instant."""
    who = str(employee_id) if employee_id is not None else f"u:{device_user_id}"
    raw = f"{company_id}|IMPORT|{who}|{direction}|{occurred_at.isoformat()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def payload_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def health(last_seen_at: Optional[datetime], skew_seconds: Optional[int], now: datetime,
           offline_after_minutes: int = OFFLINE_AFTER_MINUTES,
           max_skew_seconds: int = MAX_CLOCK_SKEW_SECONDS) -> str:
    """§7.2 chip, computed at read."""
    if last_seen_at is None:
        return "UNKNOWN"
    age = now - last_seen_at
    if age > timedelta(minutes=3 * offline_after_minutes):
        return "OFFLINE"
    if skew_seconds is not None and abs(skew_seconds) > max_skew_seconds:
        return "CLOCK_SKEW"
    if age > timedelta(minutes=offline_after_minutes):
        return "STALE"
    return "ONLINE"


def _zk_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def command_line(command_id: int, command: str, payload: dict, *, device_now_local: datetime) -> str:
    """``C:<id>:<text>`` of one command (allow-list only, SR-AS-04; [UNVERIFIED] per firmware)."""
    pin = str(payload.get("device_user_id") or "")
    if command == "SYNC_TIME":
        text = f"SET OPTIONS DateTime={_zk_time(device_now_local)}"
    elif command == "UPLOAD_USER":
        name = "".join(ch for ch in unicodedata.normalize("NFKD", str(payload.get("name") or ""))
                       if ord(ch) < 128 and ch not in "\t\r\n")[:24]
        text = f"DATA UPDATE USERINFO PIN={pin}\tName={name}\tPri=0"
        if payload.get("card"):
            text += f"\tCard={payload['card']}"
    elif command == "DISABLE_USER":
        text = f"DATA UPDATE USERINFO PIN={pin}\tDisable=1"
    elif command == "DELETE_USER":
        text = f"DATA DELETE USERINFO PIN={pin}"
    elif command == "QUERY_USERS":
        text = "DATA QUERY USERINFO"
    elif command == "RESEND_LOGS":
        text = f"DATA QUERY ATTLOG StartTime={payload.get('from')}\tEndTime={payload.get('to')}"
    elif command == "REFRESH_SETTINGS":
        text = "CHECK"
    else:  # never emitted (allow-list)
        raise ValueError(f"command {command!r} is not allow-listed")
    return f"C:{command_id}:{text}"


def parse_devicecmd(body: str) -> List[Tuple[int, str]]:
    """``ID=5&Return=0&CMD=DATA`` lines -> [(command id, return code)]."""
    results: List[Tuple[int, str]] = []
    for line in body.splitlines():
        fields = dict(part.split("=", 1) for part in line.strip().split("&") if "=" in part)
        try:
            results.append((int(fields.get("ID", "")), (fields.get("Return") or "").strip()[:20]))
        except ValueError:
            continue
    return results


def first_last(punches: Sequence[Tuple[datetime, str, str]]) -> Tuple[Optional[int], Optional[int]]:
    """Indexes of the IN and OUT of a FIRST_LAST pair in time-ordered ``(at, direction, device_role)`` rows: the
    earliest IN-capable punch and the latest OUT-capable punch after it (IN_ONLY / OUT_ONLY roles respected)."""
    first = next((i for i, (_, d, role) in enumerate(punches)
                  if d in ("IN", "UNKNOWN") and role != "OUT_ONLY"), None)
    if first is None:
        return None, None
    last = None
    for i in range(len(punches) - 1, first, -1):
        _, d, role = punches[i]
        if d in ("OUT", "UNKNOWN") and role != "IN_ONLY":
            last = i
            break
    return first, last


def backfill_from(activation_day: date, days: int = ACTIVATION_BACKFILL_DAYS) -> date:
    return activation_day - timedelta(days=days)

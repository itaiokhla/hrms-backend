"""Device ingest endpoints (AS-M7, G-17; 04-attendance/01 §8.1-§8.3, §8.7).

ADMS (ZKTeco / eSSL push) handlers, mounted at ``/api/v1/iclock/*`` and at ``/iclock/*`` (firmware cannot change
the path; both are listed in ``test_non_api_routes_are_reviewed``):

* ``GET /iclock/cdata`` — handshake (H-3): the stored stamps and attendance-only transfer options;
* ``POST /iclock/cdata?table=ATTLOG`` (or the old form without ``table``) — punches, ≤ 2 MiB and 20 000 lines
  (413 beyond), processed in chunks of 500 lines, each committed before the next; ``OK: <n>`` only after the last
  commit (save-before-ACK, H-6); other tables (OPERLOG, photos) are acknowledged and discarded (E-5, H-9);
* ``GET /iclock/getrequest`` — command poll and heartbeat (``C:<id>:<cmd>`` lines or ``OK``);
* ``POST /iclock/devicecmd`` — command results; ``GET /iclock/ping`` — heartbeat.

Authentication: ``SN`` + the per-device secret (Comm Key) in ``X-Device-Secret`` or ``key=``; an unknown serial is
quarantined and, like a wrong secret, answered 401 ``DEVICE_AUTH_FAILED`` in plain text (no input reflected,
SR-AS-18). 120 requests / min per serial.

AGENT / WEBHOOK: ``POST /api/v1/attendance/ingest/punches`` with ``X-Device-Id`` (agent id) and
``X-Attendance-Signature`` (hex HMAC-SHA256 of the raw body); ≤ 1 000 records; 60 batches / min per device;
per-record results (J-1..J-4).

No user session and no cookies on any of these routes.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Callable, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError
from app.core.rate_limit import client_ip, limiter
from app.database.db import bind_tenant, get_db
from app.models.attendance import AttendanceDevice
from app.services import device_admin
from app.services import device_ingest as ingest
from app.services import device_protocol as proto

logger = logging.getLogger("hrms.devices")

iclock = APIRouter(tags=["Device ingest (ADMS)"])
agent = APIRouter(prefix="/api/v1", tags=["Device ingest (agent)"])

PUSH_PER_MINUTE = 120
AGENT_PER_MINUTE = 60


def _plain(status: int, text: str) -> PlainTextResponse:
    return PlainTextResponse(text, status_code=status)


def _vendor_guess(request: Request) -> Optional[str]:
    agent_header = (request.headers.get("user-agent") or "").lower()
    if "essl" in agent_header:
        return "ESSL"
    if "iclock" in agent_header or "zk" in agent_header or request.query_params.get("pushver"):
        return "ZKTECO"
    return None


def _now() -> datetime:
    return clock.utcnow().replace(microsecond=0)


def _authenticate(request: Request, db: Session, payload_lines: int = 0) -> AttendanceDevice:
    serial = (request.query_params.get("SN") or "").strip()[:64]
    if not serial:
        raise ingest.auth_failed()
    limiter.check(f"iclock:{serial}", PUSH_PER_MINUTE, 60)
    target = ingest.resolve_serial(serial)
    if target is None:
        ingest.quarantine(serial, client_ip(request), _vendor_guess(request), payload_lines)
        raise ingest.auth_failed()
    company_id, device_id = target
    bind_tenant(db, company_id)
    secret = request.headers.get("X-Device-Secret") or request.query_params.get("key")
    return ingest.load_device(db, company_id, device_id, secret=secret, integration="PUSH")


def _guarded(handler: Callable[[], PlainTextResponse], db: Session) -> PlainTextResponse:
    try:
        return handler()
    except AppError as exc:
        db.rollback()
        if exc.status_code == 429:
            return _plain(429, "RATE_LIMITED")
        return _plain(exc.status_code, exc.code or "ERROR")


def _pair(db: Session, company_id: int, to_pair) -> None:
    """Pairing after the ACKed data is committed; a pairing failure never loses a stored punch (I-1, I-5)."""
    by_employee: dict = {}
    for employee_id, day in to_pair:
        by_employee.setdefault(employee_id, set()).add(day)
    for employee_id, days in sorted(by_employee.items()):
        try:
            with db.begin_nested():
                ingest.pair_dates(db, company_id, employee_id, days)
        except Exception:  # noqa: BLE001 - the day-close job re-pairs; logged by id only (SR-AS-19)
            logger.exception("device pairing failed for employee %s", employee_id)
    db.commit()


def _cdata_get(request: Request, db: Session = Depends(get_db)) -> PlainTextResponse:
    def run() -> PlainTextResponse:
        device = _authenticate(request, db)
        ingest.seen(device, _now(), handshake=True)
        reply = ingest.handshake(device, request.query_params.get("pushver"))
        db.commit()
        return _plain(200, reply)

    return _guarded(run, db)


async def _cdata_post(request: Request, db: Session = Depends(get_db)) -> PlainTextResponse:
    body = await request.body()
    if len(body) > proto.MAX_PUSH_BYTES:
        logger.warning("ADMS body refused: %s bytes, sha256 %s", len(body), proto.payload_hash(body[:4096].decode(
            "latin-1")))
        return _plain(413, "PAYLOAD_TOO_LARGE")
    text = body.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) > proto.MAX_PUSH_LINES:
        return _plain(413, "PAYLOAD_TOO_LARGE")
    table = (request.query_params.get("table") or "ATTLOG").upper()

    def run() -> PlainTextResponse:
        device = _authenticate(request, db, payload_lines=len(lines))
        received_at = _now()
        real_time = ingest.seen(device, received_at)
        if table != "ATTLOG":  # OPERLOG / photos: acknowledged, never stored (E-5, H-9, I-10)
            ingest.store_stamp(device, table, request.query_params.get("Stamp")
                               or request.query_params.get(f"{table}Stamp"))
            db.commit()
            return _plain(200, f"OK: {sum(1 for line in lines if line.strip())}")
        company_id, device_id = device.company_id, device.id
        batch_id = f"push:{uuid.uuid4().hex[:24]}"
        localizer = proto.Localizer(device.timezone, real_time=real_time, received_at=received_at)
        total = ingest.IngestResult()
        for start in range(0, len(lines), proto.CHUNK_LINES):
            chunk = "\n".join(lines[start:start + proto.CHUNK_LINES])
            parsed, rejects = proto.parse_attlog(chunk, start_line=start + 1)
            device = db.query(AttendanceDevice).filter(AttendanceDevice.company_id == company_id,
                                                       AttendanceDevice.id == device_id).with_for_update().one()
            part = ingest.ingest(db, device, parsed, rejects, batch_id=batch_id, received_at=received_at,
                                 localizer=localizer)
            db.commit()  # save-before-ACK per chunk (AS-DEV-15)
            total.add(part)
        device = db.query(AttendanceDevice).filter(AttendanceDevice.company_id == company_id,
                                                   AttendanceDevice.id == device_id).with_for_update().one()
        ingest.store_stamp(device, "ATTLOG", request.query_params.get("Stamp")
                           or request.query_params.get("ATTLOGStamp"))
        protocol = dict(device.protocol_state or {})
        protocol["last_batch"] = {**total.as_dict(), "at": received_at.isoformat()}
        protocol["skipped_backfill_total"] = int(protocol.get("skipped_backfill_total", 0)) + total.skipped_backfill
        device.protocol_state = protocol
        db.commit()
        _pair(db, company_id, total.to_pair)
        return _plain(200, f"OK: {total.accepted + total.rejected}")

    try:
        return _guarded(run, db)
    except Exception:  # noqa: BLE001 - H-6: no OK, the device re-sends; committed chunks collapse (P-2)
        db.rollback()
        logger.exception("ADMS upload failed")
        return _plain(500, "ERROR")


def _getrequest(request: Request, db: Session = Depends(get_db)) -> PlainTextResponse:
    def run() -> PlainTextResponse:
        device = _authenticate(request, db)
        now = _now()
        real_time = ingest.seen(device, now)
        ingest.advance_idle_watermark(device, now, real_time)
        lines = device_admin.pending_command_lines(db, device, now)
        db.commit()
        return _plain(200, "\n".join(lines) + "\n" if lines else "OK")

    return _guarded(run, db)


async def _devicecmd(request: Request, db: Session = Depends(get_db)) -> PlainTextResponse:
    body = (await request.body())[:65536].decode("utf-8", errors="replace")

    def run() -> PlainTextResponse:
        device = _authenticate(request, db)
        now = _now()
        ingest.seen(device, now)
        device_admin.command_results(db, device, proto.parse_devicecmd(body), now)
        db.commit()
        return _plain(200, "OK")

    return _guarded(run, db)


def _ping(request: Request, db: Session = Depends(get_db)) -> PlainTextResponse:
    def run() -> PlainTextResponse:
        device = _authenticate(request, db)
        ingest.seen(device, _now())
        db.commit()
        return _plain(200, "OK")

    return _guarded(run, db)


for _prefix in ("/iclock", "/api/v1/iclock"):
    iclock.add_api_route(f"{_prefix}/cdata", _cdata_get, methods=["GET"], response_class=PlainTextResponse,
                         name=f"iclock_cdata_get{_prefix.replace('/', '_')}")
    iclock.add_api_route(f"{_prefix}/cdata", _cdata_post, methods=["POST"], response_class=PlainTextResponse,
                         name=f"iclock_cdata_post{_prefix.replace('/', '_')}")
    iclock.add_api_route(f"{_prefix}/getrequest", _getrequest, methods=["GET"], response_class=PlainTextResponse,
                         name=f"iclock_getrequest{_prefix.replace('/', '_')}")
    iclock.add_api_route(f"{_prefix}/devicecmd", _devicecmd, methods=["POST"], response_class=PlainTextResponse,
                         name=f"iclock_devicecmd{_prefix.replace('/', '_')}")
    iclock.add_api_route(f"{_prefix}/ping", _ping, methods=["GET"], response_class=PlainTextResponse,
                         name=f"iclock_ping{_prefix.replace('/', '_')}")


@agent.post("/attendance/ingest/punches")
async def agent_batch(request: Request, db: Session = Depends(get_db)):
    """AGENT / WEBHOOK batch (J-1..J-4)."""
    raw = await request.body()
    agent_id = (request.headers.get("X-Device-Id") or "").strip()[:64]
    if not agent_id:
        raise ingest.auth_failed()
    limiter.check(f"agent:{agent_id}", AGENT_PER_MINUTE, 60)
    target = ingest.resolve_serial(agent_id)
    if target is None:
        raise ingest.auth_failed()
    company_id, device_id = target
    bind_tenant(db, company_id)
    device = ingest.load_device(db, company_id, device_id, integration="AGENT")
    if not ingest.agent_signature_ok(db, device, raw, request.headers.get("X-Attendance-Signature")):
        db.rollback()
        raise ingest.auth_failed()
    body = ingest.parse_agent_body(raw)
    received_at = _now()
    ingest.seen(device, received_at)
    lines, rejects, explicit = ingest.agent_lines(body["records"])
    localizer = proto.Localizer(device.timezone)
    if body.get("device_time_now"):  # T-4 (AS-TZ-05): the agent's clock offset
        try:
            device_now = proto.parse_local_ts(str(body["device_time_now"]))
            offset = int((received_at - proto.to_instant(device_now, device.timezone)).total_seconds())
            device.last_clock_skew_seconds = offset if abs(offset) > proto.MAX_CLOCK_SKEW_SECONDS else 0
        except ValueError:
            pass
    result = ingest.ingest(db, device, lines, rejects, batch_id=f"agent:{body['ingest_batch_id']}",
                           received_at=received_at, localizer=localizer, explicit_instants=explicit)
    db.commit()
    _pair(db, company_id, result.to_pair)
    return JSONResponse({"ingest_batch_id": body["ingest_batch_id"], **result.as_dict(),
                         "records": sorted(result.records, key=lambda r: r["line_no"])})

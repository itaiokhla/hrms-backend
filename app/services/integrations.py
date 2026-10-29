"""Channels, browser push and calendar feeds (G-24; AS-M12; owner choice 2026-10-29: webhook based).

* **Channels** (``integrations:manage``): Slack and Microsoft Teams incoming webhooks, signed generic webhooks and a
  WhatsApp provider slot. URL and signing secret are sealed with the tenant data key (purpose ``DEVICE_SECRET``,
  the integration-secret key) and never returned; the page shows ``url_hint`` only. ``emit`` writes one
  ``channel_deliveries`` row per subscribed channel (unique per channel and event key, so a retried transaction
  never sends twice) and queues ``channels.deliver``; ``channels.retry`` (hourly) re-sends QUEUED / FAILED rows with
  fewer than ``MAX_ATTEMPTS`` attempts. Generic webhooks carry ``X-HRMS-Event``, ``X-HRMS-Timestamp`` and
  ``X-HRMS-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>.<body>")>``.
* Event texts are short and carry no pay amounts, reasons or notes (only the kind, the date and a link): channel
  messages leave the tenant.
* **Push**: Web Push with VAPID (RFC 8292) and no payload — the "tickle" wakes the service worker, which fetches the
  newest notification over the user's own session, so no notification text crosses the push service.
* **Calendar feed**: ``/api/v1/calendar/<company>.<secret>.ics`` serves the published schedule of one employee
  (14 days back, 60 ahead). Only the SHA-256 of the secret is stored; rotating or revoking invalidates the old URL.

Outbound requests refuse private, loopback and link-local hosts outside development and test (SSRF).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import settings
from app.core import clock
from app.core.errors import AppError, bad_request, forbidden, not_found
from app.core.principal import Principal
from app.models.employee import Employee
from app.models.integrations import (
    CHANNEL_KINDS, CalendarFeedToken, ChannelDelivery, NotificationChannel, PushSubscription,
)
from app.payroll.services import jobs, keys
from app.services.audit_log_service import AuditLogService

logger = logging.getLogger(__name__)

MANAGE = "integrations:manage"
KEY_PURPOSE = "DEVICE_SECRET"
MAX_ATTEMPTS = 5
MAX_CHANNELS = 20
TIMEOUT_SECONDS = 6
DELIVER_JOB, RETRY_JOB, PUSH_JOB = "channels.deliver", "channels.retry", "push.tickle"

EVENT_TYPES: Dict[str, str] = {
    "shift_request.submitted": "Shift request waiting for approval",
    "shift_request.decided": "Shift request approved or rejected",
    "open_shift.posted": "Open shift posted",
    "attendance_request.submitted": "Attendance request waiting for approval",
    "attendance_request.decided": "Attendance request approved or rejected",
    "team_batch.submitted": "Team payroll batch submitted",
    "team_batch.decided": "Team payroll batch approved, returned or rejected",
    "channel.test": "Test message",
}


# =============================================================================================== URL safety
def _dev() -> bool:
    return settings.environment in ("development", "test")


def _check_url(url: str, kind: str) -> str:
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname or len(url) > 1000:
        raise bad_request("Enter the full webhook URL (https://…).", "INVALID_URL")
    if parsed.username or parsed.password:
        raise bad_request("Webhook URLs cannot carry a user name or password.", "INVALID_URL")
    if parsed.scheme == "http" and not _dev():
        raise bad_request("Webhook URLs must use https.", "INVALID_URL")
    if kind == "SLACK" and not _dev() and parsed.hostname != "hooks.slack.com":
        raise bad_request("A Slack incoming webhook URL starts with https://hooks.slack.com/.", "INVALID_URL")
    if not _dev():
        _refuse_private(parsed.hostname)
    return url


def _refuse_private(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise bad_request("The webhook host could not be resolved.", "INVALID_URL")
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved \
                or address.is_multicast or address.is_unspecified:
            raise bad_request("Webhook URLs must point to a public host.", "INVALID_URL")


def _hint(url: str) -> str:
    parsed = urlparse(url)
    tail = parsed.path[-4:] if len(parsed.path) > 4 else ""
    return f"{parsed.scheme}://{parsed.hostname}/…{tail}"[:80]


def _seal(db: Session, company_id: int, value: str, place: str) -> str:
    return keys.encrypt_field(db, company_id, value, KEY_PURPOSE, aad=place.encode("ascii"))


def _open(db: Session, company_id: int, sealed: str, place: str) -> str:
    return keys.decrypt_field(db, company_id, sealed, KEY_PURPOSE, aad=place.encode("ascii"))


# =============================================================================================== channels
def _require(principal: Principal) -> None:
    if not principal.has_company_scope(MANAGE):
        raise forbidden()


def channel_json(row: NotificationChannel) -> dict:
    return {"id": row.id, "kind": row.kind, "name": row.name, "url_hint": row.url_hint,
            "has_secret": bool(row.secret_enc), "events": list(row.events or []), "active": row.active,
            "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
            "last_error": row.last_error, "row_version": row.row_version,
            "provider_ready": row.kind != "WHATSAPP"}


def list_channels(db: Session, principal: Principal) -> dict:
    _require(principal)
    rows = (db.query(NotificationChannel).filter(NotificationChannel.company_id == principal.company_id)
            .order_by(NotificationChannel.id).all())
    return {"items": [channel_json(r) for r in rows],
            "event_types": [{"code": k, "label": v} for k, v in EVENT_TYPES.items() if k != "channel.test"],
            "kinds": list(CHANNEL_KINDS)}


def _load(db: Session, company_id: int, channel_id: int, lock: bool = False) -> NotificationChannel:
    query = db.query(NotificationChannel).filter(NotificationChannel.company_id == company_id,
                                                 NotificationChannel.id == channel_id)
    row = (query.with_for_update() if lock else query).first()
    if row is None:
        raise not_found("Channel")
    return row


def _events(values: Iterable[str]) -> List[str]:
    chosen = sorted({v for v in values or []})
    unknown = [v for v in chosen if v not in EVENT_TYPES or v == "channel.test"]
    if unknown:
        raise bad_request(f"Unknown events: {', '.join(unknown)}.", "INVALID_FIELDS")
    return chosen


def create_channel(db: Session, principal: Principal, body: Mapping[str, Any]) -> dict:
    _require(principal)
    cid = principal.company_id
    kind = body["kind"]
    if kind not in CHANNEL_KINDS:
        raise bad_request("Choose Slack, Teams, a webhook or WhatsApp.", "INVALID_FIELDS")
    if db.query(NotificationChannel.id).filter(NotificationChannel.company_id == cid).count() >= MAX_CHANNELS:
        raise AppError(409, f"At most {MAX_CHANNELS} channels.", "LIMIT_REACHED")
    row = NotificationChannel(company_id=cid, kind=kind, name=body["name"].strip(), events=_events(body.get("events")),
                              active=True, created_by_user_id=principal.user_id, row_version=1)
    db.add(row)
    db.flush()
    _set_secrets(db, row, body)
    AuditLogService.log(db, principal, "Settings", "CHANNEL_CREATED", f"{kind.title()} channel {row.name} added",
                        "notification_channel", row.id, details={"kind": kind, "events": row.events})
    return channel_json(row)


def _set_secrets(db: Session, row: NotificationChannel, body: Mapping[str, Any]) -> None:
    if body.get("url"):
        url = _check_url(body["url"], row.kind)
        row.url_enc = _seal(db, row.company_id, url, f"notification_channels.url:{row.id}")
        row.url_hint = _hint(url)
    elif row.url_enc is None and row.kind != "WHATSAPP":
        raise bad_request("Enter the webhook URL.", "INVALID_URL")
    if row.kind == "WEBHOOK" and (body.get("rotate_secret") or row.secret_enc is None):
        row.secret_enc = _seal(db, row.company_id, secrets.token_urlsafe(32), f"notification_channels.secret:{row.id}")
    db.flush()


def update_channel(db: Session, principal: Principal, channel_id: int, body: Mapping[str, Any]) -> dict:
    _require(principal)
    row = _load(db, principal.company_id, channel_id, lock=True)
    if body.get("row_version") is not None and body["row_version"] != row.row_version:
        raise AppError(409, "Someone else changed this channel. Reload it.", "STALE_ROW_VERSION")
    if body.get("name"):
        row.name = body["name"].strip()
    if body.get("events") is not None:
        row.events = _events(body["events"])
    if body.get("active") is not None:
        row.active = bool(body["active"])
    _set_secrets(db, row, body)
    row.row_version += 1
    db.flush()
    AuditLogService.log(db, principal, "Settings", "CHANNEL_UPDATED", f"Channel {row.name} changed",
                        "notification_channel", row.id,
                        details={"events": row.events, "active": row.active, "url_changed": bool(body.get("url")),
                                 "secret_rotated": bool(body.get("rotate_secret"))})
    return channel_json(row)


def reveal_secret(db: Session, principal: Principal, channel_id: int) -> dict:
    """The signing secret of a generic webhook (to configure the receiver); audited."""
    _require(principal)
    row = _load(db, principal.company_id, channel_id)
    if row.kind != "WEBHOOK" or not row.secret_enc:
        raise bad_request("Only generic webhooks have a signing secret.", "INVALID_FIELDS")
    AuditLogService.log(db, principal, "Settings", "CHANNEL_SECRET_VIEWED", f"Signing secret of {row.name} viewed",
                        "notification_channel", row.id)
    return {"secret": _open(db, row.company_id, row.secret_enc, f"notification_channels.secret:{row.id}"),
            "header": "X-HRMS-Signature", "algorithm": "sha256=HMAC-SHA256(secret, timestamp + '.' + body)"}


def delete_channel(db: Session, principal: Principal, channel_id: int) -> None:
    _require(principal)
    row = _load(db, principal.company_id, channel_id, lock=True)
    db.query(ChannelDelivery).filter(ChannelDelivery.company_id == row.company_id,
                                     ChannelDelivery.channel_id == row.id).delete(synchronize_session=False)
    AuditLogService.log(db, principal, "Settings", "CHANNEL_DELETED", f"Channel {row.name} removed",
                        "notification_channel", row.id)
    db.delete(row)
    db.flush()


def deliveries(db: Session, principal: Principal, channel_id: int) -> List[dict]:
    _require(principal)
    _load(db, principal.company_id, channel_id)
    rows = (db.query(ChannelDelivery).filter(ChannelDelivery.company_id == principal.company_id,
                                             ChannelDelivery.channel_id == channel_id)
            .order_by(ChannelDelivery.id.desc()).limit(50).all())
    return [{"id": r.id, "event_type": r.event_type, "status": r.status, "attempts": r.attempts,
             "last_error": r.last_error, "title": (r.payload or {}).get("title"),
             "created_at": r.created_at.isoformat() if r.created_at else None,
             "sent_at": r.sent_at.isoformat() if r.sent_at else None} for r in rows]


def test_channel(db: Session, principal: Principal, channel_id: int) -> dict:
    """Send a test message now (synchronously) and report the outcome."""
    _require(principal)
    row = _load(db, principal.company_id, channel_id)
    delivery = _queue(db, row, "channel.test", f"test:{clock.utcnow().isoformat()}",
                      {"title": "Test message from HRMS", "text": f"Channel {row.name} is connected.", "url": None})
    _send(db, delivery, row)
    return {"status": delivery.status, "error": delivery.last_error}


# =============================================================================================== emit / deliver
def _queue(db: Session, channel: NotificationChannel, event_type: str, event_key: str,
           payload: Mapping[str, Any]) -> ChannelDelivery:
    now = clock.utcnow()
    db.execute(insert(ChannelDelivery).values(
        company_id=channel.company_id, channel_id=channel.id, event_type=event_type, event_key=event_key[:120],
        payload=dict(payload), status="QUEUED", attempts=0, created_at=now, updated_at=now)
        .on_conflict_do_nothing(constraint="uq_channel_deliveries_event"))
    return (db.query(ChannelDelivery).filter(ChannelDelivery.company_id == channel.company_id,
                                             ChannelDelivery.channel_id == channel.id,
                                             ChannelDelivery.event_key == event_key[:120]).one())


def emit(db: Session, company_id: int, event_type: str, event_key: str, title: str, text: str,
         url: Optional[str] = None) -> int:
    """Queue ``event_type`` for every active channel subscribed to it; never fails the caller's action."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown channel event {event_type!r}")
    db.flush()  # the caller's own rows fail loudly here, never inside the savepoint below
    try:
        with db.begin_nested():
            channels = (db.query(NotificationChannel)
                        .filter(NotificationChannel.company_id == company_id, NotificationChannel.active.is_(True))
                        .all())
            queued = 0
            for channel in channels:
                if event_type not in (channel.events or []):
                    continue
                delivery = _queue(db, channel, event_type, f"{event_type}:{event_key}",
                                  {"title": title[:200], "text": text[:500], "url": url})
                if delivery.status == "QUEUED" and delivery.attempts == 0:
                    jobs.enqueue(db, company_id=company_id, type=DELIVER_JOB, payload={"delivery_id": delivery.id},
                                 idempotency_key=f"deliver:{delivery.id}", max_attempts=1)
                    queued += 1
            return queued
    except Exception:  # noqa: BLE001 - a channel problem never blocks an HR action
        logger.exception("Could not queue channel event %s", event_type)
        return 0


def _absolute(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    return settings.frontend_url.rstrip("/") + url if url.startswith("/") else url


def _body(kind: str, delivery: ChannelDelivery) -> Tuple[bytes, Dict[str, str]]:
    payload = delivery.payload or {}
    link = _absolute(payload.get("url"))
    text = f"*{payload.get('title')}*\n{payload.get('text')}" + (f"\n<{link}|Open in HRMS>" if link else "")
    if kind == "SLACK":
        data: Dict[str, Any] = {"text": text}
    elif kind == "TEAMS":
        data = {"@type": "MessageCard", "@context": "https://schema.org/extensions", "summary": payload.get("title"),
                "title": payload.get("title"), "text": payload.get("text"),
                "potentialAction": ([{"@type": "OpenUri", "name": "Open in HRMS",
                                      "targets": [{"os": "default", "uri": link}]}] if link else [])}
    else:
        data = {"event": delivery.event_type, "id": delivery.event_key, "title": payload.get("title"),
                "text": payload.get("text"), "url": link,
                "sent_at": clock.utcnow().replace(microsecond=0).isoformat()}
    return json.dumps(data, separators=(",", ":")).encode("utf-8"), {"Content-Type": "application/json"}


def sign(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _post(url: str, body: bytes, headers: Mapping[str, str]) -> int:
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"User-Agent": "HRMS-Webhooks/1.0", **headers})
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - scheme checked in _check_url
        return response.status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # a redirect could point at an internal host
        return None


def _send(db: Session, delivery: ChannelDelivery, channel: NotificationChannel) -> None:
    now = clock.utcnow()
    delivery.attempts += 1
    if channel.kind == "WHATSAPP":
        delivery.status, delivery.last_error = "SKIPPED", "No WhatsApp provider is connected yet."
        db.flush()
        return
    try:
        url = _open(db, channel.company_id, channel.url_enc, f"notification_channels.url:{channel.id}")
        if not _dev():
            _refuse_private(urlparse(url).hostname or "")
        body, headers = _body(channel.kind, delivery)
        if channel.kind == "WEBHOOK":
            stamp = str(int(time.time()))
            secret = _open(db, channel.company_id, channel.secret_enc, f"notification_channels.secret:{channel.id}")
            headers.update({"X-HRMS-Event": delivery.event_type, "X-HRMS-Timestamp": stamp,
                            "X-HRMS-Delivery": str(delivery.id), "X-HRMS-Signature": sign(secret, stamp, body)})
        status = _post(url, body, headers)
        if not 200 <= status < 300:
            raise RuntimeError(f"HTTP {status}")
        delivery.status, delivery.sent_at, delivery.last_error = "SENT", now, None
        channel.last_success_at, channel.last_error = now, None
    except AppError as exc:
        delivery.status, delivery.last_error = "FAILED", str(exc.detail)[:300] if hasattr(exc, "detail") else "refused"
        channel.last_error = delivery.last_error
    except urllib.error.HTTPError as exc:
        delivery.status, delivery.last_error = "FAILED", f"HTTP {exc.code}"
        channel.last_error = delivery.last_error
    except Exception as exc:  # noqa: BLE001 - network failures are recorded and retried
        delivery.status, delivery.last_error = "FAILED", f"{type(exc).__name__}: {exc}"[:300]
        channel.last_error = delivery.last_error
    db.flush()


@jobs.register(DELIVER_JOB)
def deliver(ctx) -> dict:
    db = ctx.db
    delivery = (db.query(ChannelDelivery).filter(ChannelDelivery.company_id == ctx.company_id,
                                                 ChannelDelivery.id == int(ctx.payload["delivery_id"]))
                .with_for_update().first())
    if delivery is None or delivery.status in ("SENT", "SKIPPED"):
        return {"skipped": True}
    channel = _load(db, ctx.company_id, delivery.channel_id)
    if not channel.active:
        delivery.status, delivery.last_error = "SKIPPED", "Channel paused."
        return {"status": "SKIPPED"}
    _send(db, delivery, channel)
    return {"status": delivery.status}


@jobs.register(RETRY_JOB)
def retry(ctx) -> dict:
    """Hourly: re-send QUEUED / FAILED deliveries of the last 3 days with attempts left."""
    db = ctx.db
    since = clock.utcnow() - timedelta(days=3)
    rows = (db.query(ChannelDelivery)
            .filter(ChannelDelivery.company_id == ctx.company_id, ChannelDelivery.status.in_(("QUEUED", "FAILED")),
                    ChannelDelivery.attempts < MAX_ATTEMPTS, ChannelDelivery.created_at >= since)
            .order_by(ChannelDelivery.id).limit(200).with_for_update(skip_locked=True).all())
    sent = 0
    for delivery in rows:
        channel = db.get(NotificationChannel, delivery.channel_id)
        if channel is None or not channel.active:
            continue
        _send(db, delivery, channel)
        sent += delivery.status == "SENT"
    return {"retried": len(rows), "sent": sent}


# =============================================================================================== web push (VAPID)
_VAPID: Optional[Tuple[Any, str]] = None
VAPID_FILE = Path(__file__).resolve().parents[2] / ".local-secrets" / "vapid-private.pem"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _vapid() -> Tuple[Any, str]:
    """The VAPID key pair: ``VAPID_PRIVATE_KEY`` (PEM) in production, a generated key file in development."""
    global _VAPID
    if _VAPID is not None:
        return _VAPID
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    pem = os.environ.get("VAPID_PRIVATE_KEY", "").replace("\\n", "\n").encode("ascii")
    if not pem and VAPID_FILE.exists():
        pem = VAPID_FILE.read_bytes()
    if pem:
        private = serialization.load_pem_private_key(pem, password=None)
    else:
        if not _dev():
            raise AppError(503, "Browser push is not configured (VAPID_PRIVATE_KEY).", "PUSH_NOT_CONFIGURED")
        private = ec.generate_private_key(ec.SECP256R1())
        VAPID_FILE.parent.mkdir(parents=True, exist_ok=True)
        VAPID_FILE.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption()))
    public = private.public_key().public_bytes(serialization.Encoding.X962,
                                               serialization.PublicFormat.UncompressedPoint)
    _VAPID = (private, _b64(public))
    return _VAPID


def vapid_public_key() -> str:
    return _vapid()[1]


def _vapid_header(endpoint: str) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    private, public = _vapid()
    parsed = urlparse(endpoint)
    header = _b64(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = _b64(json.dumps({"aud": f"{parsed.scheme}://{parsed.netloc}", "exp": int(time.time()) + 12 * 3600,
                              "sub": os.environ.get("VAPID_SUBJECT", "mailto:support@hrms.local")},
                             separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode("ascii")
    r, s = decode_dss_signature(private.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    token = f"{header}.{claims}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return f"vapid t={token}, k={public}"


def _push_endpoint_ok(endpoint: str) -> str:
    parsed = urlparse(endpoint or "")
    if parsed.scheme != "https" or not parsed.hostname or len(endpoint) > 1000:
        raise bad_request("That is not a push subscription endpoint.", "INVALID_FIELDS")
    if not _dev():
        _refuse_private(parsed.hostname)
    return endpoint


def subscribe(db: Session, principal: Principal, body: Mapping[str, Any], user_agent: Optional[str]) -> dict:
    endpoint = _push_endpoint_ok(body["endpoint"])
    keys_ = body.get("keys") or {}
    now = clock.utcnow()
    db.execute(insert(PushSubscription).values(
        company_id=principal.company_id, user_id=principal.user_id, endpoint=endpoint,
        p256dh=(keys_.get("p256dh") or "")[:200] or None, auth=(keys_.get("auth") or "")[:100] or None,
        user_agent=(user_agent or "")[:200] or None, active=True, created_at=now, updated_at=now)
        .on_conflict_do_update(constraint="uq_push_subscriptions_endpoint",
                               set_={"active": True, "last_error": None, "updated_at": now}))
    return push_status(db, principal)


def unsubscribe(db: Session, principal: Principal, endpoint: Optional[str]) -> dict:
    query = db.query(PushSubscription).filter(PushSubscription.company_id == principal.company_id,
                                              PushSubscription.user_id == principal.user_id)
    if endpoint:
        query = query.filter(PushSubscription.endpoint == endpoint)
    query.delete(synchronize_session=False)
    return push_status(db, principal)


def push_status(db: Session, principal: Principal) -> dict:
    count = (db.query(PushSubscription.id).filter(PushSubscription.company_id == principal.company_id,
                                                  PushSubscription.user_id == principal.user_id,
                                                  PushSubscription.active.is_(True)).count())
    try:
        key = vapid_public_key()
    except AppError:
        key = None
    return {"subscriptions": count, "vapid_public_key": key}


def tickle_user(db: Session, company_id: int, user_id: int) -> None:
    """Queue a payload-less push for ``user_id`` when they have a subscription (called after a notification)."""
    with db.no_autoflush:  # never flush the caller's pending rows from here
        subscribed = (db.query(PushSubscription.id).filter(PushSubscription.company_id == company_id,
                                                           PushSubscription.user_id == user_id,
                                                           PushSubscription.active.is_(True)).first())
    if not subscribed:
        return
    minute = clock.utcnow().strftime("%Y%m%d%H%M")
    try:
        jobs.enqueue(db, company_id=company_id, type=PUSH_JOB, payload={"user_id": user_id},
                     idempotency_key=f"push:{user_id}:{minute}", max_attempts=2)
    except (ValueError, jobs.TenantContextError):  # noqa: BLE001 - push is best effort (checked before any SQL)
        logger.exception("Could not queue a push tickle")


def send_push(db: Session, company_id: int, user_id: int) -> Dict[str, int]:
    rows = (db.query(PushSubscription).filter(PushSubscription.company_id == company_id,
                                              PushSubscription.user_id == user_id,
                                              PushSubscription.active.is_(True)).all())
    sent = failed = 0
    for row in rows:
        try:
            if not _dev():
                _refuse_private(urlparse(row.endpoint).hostname or "")
            request = urllib.request.Request(row.endpoint, data=b"", method="POST",
                                             headers={"TTL": "3600", "Urgency": "normal",
                                                      "Authorization": _vapid_header(row.endpoint)})
            with urllib.request.build_opener(_NoRedirect).open(request, timeout=TIMEOUT_SECONDS):  # noqa: S310
                pass
            row.last_sent_at, row.last_error = clock.utcnow(), None
            sent += 1
        except urllib.error.HTTPError as exc:
            row.last_error = f"HTTP {exc.code}"
            if exc.code in (404, 410):  # the browser unsubscribed
                row.active = False
            failed += 1
        except Exception as exc:  # noqa: BLE001
            row.last_error = f"{type(exc).__name__}"[:300]
            failed += 1
    db.flush()
    return {"sent": sent, "failed": failed}


@jobs.register(PUSH_JOB)
def push_tickle(ctx) -> dict:
    return send_push(ctx.db, ctx.company_id, int(ctx.payload["user_id"]))


def test_push(db: Session, principal: Principal) -> dict:
    from app.services.notification_service import NotificationService

    NotificationService.create(db, principal.user_id, "Push notifications are on",
                               "You will get a browser notification for approvals and schedule changes.",
                               "success", "/me")
    db.flush()
    return send_push(db, principal.company_id, principal.user_id)


# =============================================================================================== calendar feed
FEED_PERMISSION = "roster:read"


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def feed_status(db: Session, principal: Principal) -> dict:
    row = (db.query(CalendarFeedToken).filter(CalendarFeedToken.company_id == principal.company_id,
                                              CalendarFeedToken.user_id == principal.user_id,
                                              CalendarFeedToken.active.is_(True)).first())
    return {"active": row is not None,
            "last_used_at": row.last_used_at.isoformat() if row is not None and row.last_used_at else None}


def rotate_feed(db: Session, principal: Principal) -> dict:
    """Create (or replace) the caller's feed URL; the URL is returned once."""
    if principal.employee_id is None:
        raise not_found("Employee profile")
    if not principal.has(FEED_PERMISSION):
        raise forbidden()
    db.query(CalendarFeedToken).filter(CalendarFeedToken.company_id == principal.company_id,
                                       CalendarFeedToken.user_id == principal.user_id).delete(synchronize_session=False)
    secret = secrets.token_urlsafe(32)
    db.add(CalendarFeedToken(company_id=principal.company_id, user_id=principal.user_id,
                             employee_id=principal.employee_id, token_hash=_hash(secret), active=True))
    db.flush()
    AuditLogService.log(db, principal, "Shifts", "CALENDAR_FEED_CREATED", "Calendar feed link created",
                        "employee", principal.employee_id)
    return {**feed_status(db, principal), "path": f"/api/v1/calendar/{principal.company_id}.{secret}.ics"}


def revoke_feed(db: Session, principal: Principal) -> dict:
    db.query(CalendarFeedToken).filter(CalendarFeedToken.company_id == principal.company_id,
                                       CalendarFeedToken.user_id == principal.user_id).delete(synchronize_session=False)
    AuditLogService.log(db, principal, "Shifts", "CALENDAR_FEED_REVOKED", "Calendar feed link revoked",
                        "employee", principal.employee_id)
    return feed_status(db, principal)


def parse_feed_token(token: str) -> Optional[Tuple[int, str]]:
    company, _, secret = (token or "").partition(".")
    if not company.isdigit() or not 20 <= len(secret) <= 100:
        return None
    return int(company), secret


def _ics_escape(value: str) -> str:
    return (value or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def feed_ics(db: Session, company_id: int, secret: str) -> Optional[str]:
    """The ICS text for a feed secret (tenant-bound session), or None when the secret is unknown or revoked."""
    import pytz

    from app.models.user import User
    from app.services import shift_rosters

    row = (db.query(CalendarFeedToken).filter(CalendarFeedToken.company_id == company_id,
                                              CalendarFeedToken.token_hash == _hash(secret),
                                              CalendarFeedToken.active.is_(True)).first())
    if row is None:
        return None
    user = db.get(User, row.user_id)
    employee = db.get(Employee, row.employee_id)
    if user is None or employee is None or not getattr(user, "is_active", True):
        return None
    row.last_used_at = clock.utcnow()
    today = clock.company_today(db, company_id)

    class _Viewer:  # my_schedule reads the caller's own schedule only
        def __init__(self):
            self.company_id, self.employee_id, self.user_id = company_id, row.employee_id, row.user_id

    start = today - timedelta(days=14)
    events: List[str] = []
    stamp = clock.utcnow().strftime("%Y%m%dT%H%M%SZ")
    for offset in (0, 31, 62):
        chunk_start = start + timedelta(days=offset)
        data = shift_rosters.my_schedule(db, _Viewer(), chunk_start, chunk_start + timedelta(days=30))
        for day in data["days"]:
            if day.get("kind") != "WORK" or not day.get("start_time") or not day.get("end_time"):
                continue
            zone = pytz.timezone(day.get("work_timezone") or day.get("anchor_timezone") or "UTC")
            on = date.fromisoformat(day["date"])
            begin = zone.localize(datetime.combine(on, _time(day["start_time"])))
            end = zone.localize(datetime.combine(on, _time(day["end_time"])))
            if end <= begin:
                end += timedelta(days=1)
            title = f"Shift {day.get('shift_code') or ''}".strip()
            events.append("\r\n".join([
                "BEGIN:VEVENT", f"UID:shift-{company_id}-{row.employee_id}-{day['date']}@hrms", f"DTSTAMP:{stamp}",
                f"DTSTART:{begin.astimezone(pytz.utc).strftime('%Y%m%dT%H%M%SZ')}",
                f"DTEND:{end.astimezone(pytz.utc).strftime('%Y%m%dT%H%M%SZ')}",
                f"SUMMARY:{_ics_escape(title)}", "TRANSP:OPAQUE", "END:VEVENT"]))
    name = _ics_escape(f"{employee.full_name} – work schedule")
    return "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Intertech HRMS//Schedule//EN",
                        "CALSCALE:GREGORIAN", f"X-WR-CALNAME:{name}", "X-PUBLISHED-TTL:PT1H", *events,
                        "END:VCALENDAR"]) + "\r\n"


def _time(value: Any):
    from datetime import time as dtime

    if isinstance(value, dtime):
        return value
    hh, mm = str(value).split(":")[:2]
    return dtime(int(hh), int(mm))

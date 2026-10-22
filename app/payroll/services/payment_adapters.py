"""Payment adapters (3.7 §7): a bank-file adapter and a SANDBOX payout API. Nothing here moves money.

* ``GENERIC_CSV`` (file adapter): the only OFFERED bank file format (``app.payroll.banking.files``).
  Files are deterministic and hashed; a regeneration must reproduce the stored SHA-256 or fail
  loudly. Every other format is refused until a bank accepts a test file (contract §8.2).
* ``SANDBOX`` (API adapter): a mock payout provider that never leaves the process. ``submit`` accepts
  instructions and returns a provider reference derived from the instruction's idempotency key, so a
  retry with the same key returns the same reference and can never pay twice (EC-PAY-05). Its results
  are labelled SANDBOX everywhere and can never settle a production instruction (3.7 §7.2).
  Inbound webhooks carry ``X-Payout-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256 of "t." + body>``;
  ``verify_webhook`` checks the signature in constant time, a ±5 minute timestamp window and the
  payload shape; replay protection is the unique (adapter, event id) of the webhook inbox
  (EC-PAY-06, EC-PAY-08).
* Endpoints come only from the server-side allow-list ``ENDPOINTS`` (EC-PAY-18): a funding account's
  ``provider_profile`` names an adapter and optionally an endpoint key (``"SANDBOX"``,
  ``"SANDBOX:default"``), never a URL.

The sandbox is disabled in production. Its webhook secret is ``PAYMENTS_SANDBOX_WEBHOOK_SECRET``, or,
outside production only, a key derived from the application secret (never logged, never sent to the
browser).
"""

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping, Optional, Sequence

from app.config import settings
from app.payroll import state_machines as sm
from app.payroll.banking import files as bank_files
from app.payroll.banking.errors import BankFileError

GENERIC_CSV = "GENERIC_CSV"
SANDBOX = "SANDBOX"
FILE_ADAPTERS = (GENERIC_CSV,)
API_ADAPTERS = (SANDBOX,)
SIGNATURE_HEADER = "X-Payout-Signature"
TIMESTAMP_WINDOW_SECONDS = 300
MAX_WEBHOOK_BYTES = 64 * 1024
# Server-side allow-list (EC-PAY-18): the only endpoints an adapter may ever address. The sandbox
# endpoint is a reserved ``.invalid`` name and is never contacted: the sandbox runs in-process.
ENDPOINTS: Mapping[str, Mapping[str, str]] = MappingProxyType({
    SANDBOX: MappingProxyType({"default": "sandbox://payouts.sandbox.invalid/v1/payouts"}),
})
WEBHOOK_STATUSES: frozenset[str] = frozenset(sm.EVENT_STATUSES) - {"SUBMITTED"}
_EVENT_ID = re.compile(r"^[A-Za-z0-9_.:\-]{6,120}$")
_PROVIDER_REF = re.compile(r"^SBX-[0-9a-f]{32}$")
_REASON = re.compile(r"^[A-Z0-9_]{1,10}$")


class AdapterError(Exception):
    """A refused adapter operation; ``code`` is stable, ``message`` is safe to show and log."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


# --------------------------------------------------------------------------- bank files


def file_adapter(format_code: Optional[str]) -> bank_files.BankFileFormat:
    """The OFFERED file format, or AdapterError FORMAT_NOT_OFFERED / FORMAT_UNKNOWN (contract §8.2)."""
    try:
        return bank_files.require_offered_format(format_code or "")
    except BankFileError as exc:
        raise AdapterError(exc.code, exc.message) from None


def build_file(format_code: str, batch_reference: str, records: Sequence[bank_files.PaymentRecord], *,
               value_date: Optional[date] = None, expected_sha256: Optional[str] = None) -> bank_files.BankFile:
    """Write the batch file; with ``expected_sha256`` the bytes must reproduce the stored file exactly."""
    fmt = file_adapter(format_code)
    try:
        return bank_files.generate_bank_file(fmt.code, batch_reference, records, value_date=value_date,
                                             expected_sha256=expected_sha256)
    except BankFileError as exc:
        raise AdapterError(exc.code, exc.message) from None


# --------------------------------------------------------------------------- sandbox API


def sandbox_enabled() -> bool:
    """The SANDBOX adapter never runs in production (3.7 §7.2: implemented per provider only with a sandbox)."""
    if settings.environment == "production":
        return False
    return os.environ.get("PAYMENTS_SANDBOX_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def sandbox_secret() -> bytes:
    """The webhook signing secret: configured, or derived from the app secret outside production."""
    configured = os.environ.get("PAYMENTS_SANDBOX_WEBHOOK_SECRET")
    if configured:
        if len(configured) < 32:
            raise AdapterError("SANDBOX_SECRET_INVALID", "the sandbox webhook secret is too short", 503)
        return configured.encode("utf-8")
    if not sandbox_enabled():
        raise AdapterError("ADAPTER_DISABLED", "the sandbox payout adapter is disabled", 404)
    return hmac.new(settings.secret_key.encode("utf-8"), b"payments-sandbox-webhook-v1", hashlib.sha256).digest()


def require_api_adapter(code: Optional[str]) -> str:
    adapter = (code or "").strip().upper()
    if adapter not in API_ADAPTERS:
        raise AdapterError("ADAPTER_UNKNOWN", "no payout API adapter of that name is configured", 404)
    if adapter == SANDBOX and not sandbox_enabled():
        raise AdapterError("ADAPTER_DISABLED", "the sandbox payout adapter is disabled", 404)
    return adapter


def resolve_endpoint(provider_profile: Optional[str]) -> tuple[str, str]:
    """(adapter, endpoint) for a funding account's provider profile, from the allow-list only (EC-PAY-18).

    A profile is ``"<ADAPTER>"`` or ``"<ADAPTER>:<endpoint key>"`` (for example ``"SANDBOX"``); a URL, a
    path or an unknown endpoint key is refused, so a user-supplied address can never be called.
    """
    profile = (provider_profile or "").strip()
    if not profile:
        raise AdapterError("PROVIDER_PROFILE_MISSING", "the funding account has no payout provider profile")
    if "/" in profile or "\\" in profile or "@" in profile or profile.count(":") > 1:
        raise AdapterError("ENDPOINT_NOT_ALLOWED", "endpoints come from the server allow-list, never from a URL")
    name, _, key = profile.partition(":")
    adapter = require_api_adapter(name)
    key = key or "default"
    if key not in ENDPOINTS[adapter]:
        raise AdapterError("ENDPOINT_NOT_ALLOWED", "the endpoint is not on the server allow-list")
    return adapter, ENDPOINTS[adapter][key]


@dataclass(frozen=True)
class SubmissionItem:
    instruction_id: int
    idempotency_key: str
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class SubmissionResult:
    instruction_id: int
    provider_ref: str
    accepted: bool
    reason: Optional[str] = None


def sandbox_reference(idempotency_key: str) -> str:
    """The sandbox provider reference of an instruction: stable per idempotency key, unguessable."""
    digest = hmac.new(sandbox_secret(), f"ref|{idempotency_key}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"SBX-{digest[:32]}"


def sandbox_submit(endpoint: str, items: Sequence[SubmissionItem]) -> list[SubmissionResult]:
    """Submit payouts to the in-process sandbox (no network). Each request carries its idempotency key;
    the same key always yields the same provider reference (EC-PAY-05)."""
    if endpoint not in ENDPOINTS[SANDBOX].values():
        raise AdapterError("ENDPOINT_NOT_ALLOWED", "the endpoint is not on the server allow-list")
    results = []
    for item in items:
        if not isinstance(item.amount, Decimal) or item.amount <= 0:
            results.append(SubmissionResult(item.instruction_id, sandbox_reference(item.idempotency_key), False,
                                            "INVALID_AMOUNT"))
            continue
        results.append(SubmissionResult(item.instruction_id, sandbox_reference(item.idempotency_key), True))
    return results


def sign(body: bytes, timestamp: int, secret: Optional[bytes] = None) -> str:
    """The signature header value for ``body`` sent at ``timestamp`` (the sandbox side, and tests)."""
    key = sandbox_secret() if secret is None else secret
    mac = hmac.new(key, f"{int(timestamp)}.".encode("ascii") + body, hashlib.sha256).hexdigest()
    return f"t={int(timestamp)},v1={mac}"


@dataclass(frozen=True)
class VerifiedEvent:
    adapter: str
    event_id: str
    provider_ref: str
    status: str
    signed_at: datetime
    event_type: str
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    reason_code: Optional[str] = None


def _no_float(_: str) -> Decimal:
    raise ValueError("amounts are decimal strings, never JSON numbers")


def _parse_signature(header: Optional[str]) -> tuple[int, str]:
    if not header or len(header) > 200:
        raise AdapterError("SIGNATURE_MISSING", "the webhook is not signed", 401)
    parts = dict(part.split("=", 1) for part in header.split(",") if "=" in part)
    timestamp, mac = parts.get("t", ""), parts.get("v1", "")
    if not timestamp.isdigit() or not re.fullmatch(r"[0-9a-f]{64}", mac):
        raise AdapterError("SIGNATURE_INVALID", "the webhook signature is malformed", 401)
    return int(timestamp), mac


def verify_webhook(adapter: str, signature_header: Optional[str], body: bytes, now: datetime) -> VerifiedEvent:
    """Verify signature, timestamp window and shape of an inbound webhook (EC-PAY-08); raises AdapterError."""
    adapter = require_api_adapter(adapter)
    if len(body) > MAX_WEBHOOK_BYTES:
        raise AdapterError("PAYLOAD_TOO_LARGE", "the webhook payload is too large", 413)
    timestamp, mac = _parse_signature(signature_header)
    expected = hmac.new(sandbox_secret(), f"{timestamp}.".encode("ascii") + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, mac):
        raise AdapterError("SIGNATURE_INVALID", "the webhook signature does not verify", 401)
    if abs(now.timestamp() - timestamp) > TIMESTAMP_WINDOW_SECONDS:
        raise AdapterError("TIMESTAMP_OUT_OF_WINDOW", "the webhook timestamp is outside the accepted window", 401)
    try:
        data = json.loads(body.decode("utf-8"), parse_float=_no_float)
    except (UnicodeDecodeError, ValueError):
        raise AdapterError("PAYLOAD_INVALID", "the webhook body is not valid JSON with string amounts", 400) from None
    if not isinstance(data, dict):
        raise AdapterError("PAYLOAD_INVALID", "the webhook body must be an object", 400)
    event_id, ref, status = data.get("id"), data.get("provider_ref"), data.get("status")
    if not isinstance(event_id, str) or not _EVENT_ID.match(event_id):
        raise AdapterError("PAYLOAD_INVALID", "the event id is missing or malformed", 400)
    if not isinstance(ref, str) or not _PROVIDER_REF.match(ref):
        raise AdapterError("PAYLOAD_INVALID", "the provider reference is missing or malformed", 400)
    if not isinstance(status, str) or status not in WEBHOOK_STATUSES:
        raise AdapterError("PAYLOAD_INVALID", "the event status is not a payment status", 400)
    amount = currency = None
    if data.get("amount") is not None:
        if not isinstance(data["amount"], str) or not isinstance(data.get("currency"), str):
            raise AdapterError("PAYLOAD_INVALID", "amounts are decimal strings with a currency", 400)
        try:
            amount = Decimal(data["amount"])
        except InvalidOperation:
            raise AdapterError("PAYLOAD_INVALID", "the amount is not a decimal", 400) from None
        if not amount.is_finite() or amount <= 0 or not re.fullmatch(r"[A-Z]{3}", data["currency"]):
            raise AdapterError("PAYLOAD_INVALID", "the amount must be positive with an ISO currency", 400)
        currency = data["currency"]
    reason = data.get("reason_code")
    if reason is not None and (not isinstance(reason, str) or not _REASON.match(reason)):
        raise AdapterError("PAYLOAD_INVALID", "the reason code is malformed", 400)
    event_type = data.get("type") if isinstance(data.get("type"), str) else f"payout.{status.lower()}"
    return VerifiedEvent(adapter, event_id, ref, status, datetime.fromtimestamp(timestamp, tz=timezone.utc),
                         event_type[:60], amount, currency, reason)


def payload_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def claimed_event_id(body: bytes) -> Optional[str]:
    """The (untrusted) event id of a rejected delivery, for the log only."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    value = data.get("id") if isinstance(data, dict) else None
    return value[:120] if isinstance(value, str) and _EVENT_ID.match(value) else None

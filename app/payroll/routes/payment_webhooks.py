"""Inbound payment webhooks: POST /api/v1/webhooks/payments/{adapter} (3.7 §7.2, §12; EC-PAY-06, EC-PAY-07,
EC-PAY-08, EC-PAY-18).

No session and no cookies: the request is authenticated by its HMAC signature (``X-Payout-Signature``) and
timestamp window only; a missing, forged or stale signature is 401 and is logged (untrusted id only). The
tenant is never taken from the request: it is resolved from the provider reference, which is unique across
tenants. A verified event id is processed once per adapter (platform inbox + unique event id per tenant and
source), so duplicate or replayed deliveries change nothing. Only the SANDBOX adapter exists, and a SANDBOX
event can never settle a production instruction. Responses never reveal whether a reference exists in
another tenant.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from app.core import clock
from app.database.db import SessionLocal, bind_tenant, system_session
from app.payroll.services import payment_adapters as adapters
from app.payroll.services import reconciliation as service

public = APIRouter(prefix="/api/v1/webhooks", tags=["Payment webhooks"])


def _log(**values) -> bool:
    with system_session() as sdb:
        inserted = service.log_webhook(sdb, **values)
        sdb.commit()
        return inserted


@public.post("/payments/{adapter}")
async def receive(adapter: str, request: Request):
    body = await request.body()
    adapter_code = (adapter or "").strip().upper()[:20]
    digest = adapters.payload_sha256(body[: adapters.MAX_WEBHOOK_BYTES + 1])
    try:
        event = adapters.verify_webhook(adapter_code, request.headers.get(adapters.SIGNATURE_HEADER), body,
                                        clock.utcnow())
    except adapters.AdapterError as exc:
        if exc.code not in ("ADAPTER_UNKNOWN", "ADAPTER_DISABLED"):
            _log(adapter=adapter_code, outcome="REJECTED", payload_sha256=digest,
                 claimed_event_id=adapters.claimed_event_id(body[: adapters.MAX_WEBHOOK_BYTES]), error_code=exc.code)
        return JSONResponse({"detail": exc.message, "code": exc.code}, status_code=exc.status)

    with system_session() as sdb:
        if service.webhook_seen(sdb, event.adapter, event.event_id):
            return JSONResponse({"outcome": "DUPLICATE"}, status_code=200)
        target = service.find_webhook_target(sdb, event.provider_ref)
    if target is None:
        _log(adapter=event.adapter, outcome="UNRESOLVED", payload_sha256=digest, event=event)
        return JSONResponse({"outcome": "UNRESOLVED"}, status_code=202)

    company_id, instruction_id = target
    db = SessionLocal()
    try:
        bind_tenant(db, company_id)
        try:
            outcome = service.apply_webhook_event(db, company_id, instruction_id, event)
            db.commit()
        except IntegrityError as exc:  # the same event applied concurrently: processed once (EC-PAY-06)
            db.rollback()
            if not service.is_duplicate_error(exc):
                raise
            outcome = "DUPLICATE"
    finally:
        db.close()
    logged = _log(adapter=event.adapter, outcome=service.WEBHOOK_OUTCOME.get(outcome, "PROCESSED"),
                  payload_sha256=digest, event=event)
    if not logged:
        outcome = "DUPLICATE"
    return JSONResponse({"outcome": outcome}, status_code=200)

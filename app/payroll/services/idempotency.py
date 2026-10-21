"""Idempotency-Key handling for state-changing payroll requests (3.5 §16, 3.9 §4).

The first request with a key records a hash of (method, path, body) and, when its handler
succeeds, the status code and response body, in the same transaction as the work itself. A
repeat with the same key and the same request replays the stored response; a repeat with a
different request is a 409. A concurrent duplicate waits on the key row and then replays. A
failed request rolls back together with its key, so a retry runs again.

Handlers must not commit (routes commit, as everywhere): `execute` commits once, after storing
the outcome. Responses are compact action results (ids, statuses, row versions); salary detail
is fetched with GET and never stored here (see app.models.jobs.IdempotencyKey).
"""

import hashlib
import json
import re
from datetime import timedelta
from decimal import Decimal
from typing import Any, Callable, Optional, Tuple

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import bad_request, conflict
from app.core.principal import Principal
from app.models.jobs import IDEMPOTENT_METHODS, IdempotencyKey

HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replayed"
TTL = timedelta(hours=24)
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:\-]{1,120}$")

Handler = Callable[[], Tuple[int, Any]]
# Decimals stay exact strings (jsonable_encoder would turn them into floats): two different amounts must
# never hash alike, and a stored response never carries a float amount.
_ENCODERS = {Decimal: lambda value: format(value, "f")}


def _encode(value: Any) -> Any:
    return jsonable_encoder(value, custom_encoder=_ENCODERS)


def request_hash(method: str, path: str, body: Any) -> str:
    canonical = json.dumps(
        {"method": method.upper(), "path": path, "body": _encode(body)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def key_from(request: Request) -> Optional[str]:
    key = request.headers.get(HEADER)
    if key is None:
        return None
    if not _KEY_PATTERN.match(key):
        raise bad_request(
            f"{HEADER} must be 1-120 characters of letters, digits, '.', '_', ':' or '-'.", "IDEMPOTENCY_KEY_INVALID"
        )
    return key


def execute(db: Session, principal: Principal, request: Request, handler: Handler, *, body: Any = None) -> JSONResponse:
    """Run `handler` at most once per (user, Idempotency-Key) and commit; replay repeats.

    `handler()` returns (status_code, payload). Without the header the handler simply runs.
    """
    key = key_from(request)
    if key is None:
        status, payload = handler()
        encoded = _encode(payload)
        db.commit()
        return JSONResponse(encoded, status_code=status)

    method = request.method.upper()
    if method not in IDEMPOTENT_METHODS:
        raise bad_request(f"{HEADER} is only accepted on state-changing requests.", "IDEMPOTENCY_KEY_INVALID")
    path = request.url.path
    digest = request_hash(method, path, body)
    now = clock.utcnow()

    db.execute(
        insert(IdempotencyKey)
        .values(
            company_id=principal.company_id,
            user_id=principal.user_id,
            key=key,
            method=method,
            path=path,
            request_hash=digest,
            expires_at=now + TTL,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["company_id", "user_id", "key"])
    )
    row = (
        db.query(IdempotencyKey)
        .filter(IdempotencyKey.user_id == principal.user_id, IdempotencyKey.key == key)
        .with_for_update()
        .one()
    )
    if row.expires_at <= now:
        # An expired key may be reused for a new request.
        row.method, row.path, row.request_hash = method, path, digest
        row.status_code, row.response = None, None
        row.expires_at = now + TTL
    elif row.request_hash != digest:
        raise conflict(f"This {HEADER} was already used for a different request.", "IDEMPOTENCY_KEY_REUSED")
    elif row.status_code is not None:
        return JSONResponse(row.response, status_code=row.status_code, headers={REPLAY_HEADER: "true"})

    status, payload = handler()
    encoded = _encode(payload)
    row.status_code = status
    row.response = encoded
    db.commit()
    return JSONResponse(encoded, status_code=status)


def purge_expired(db: Session, company_id: int) -> int:
    """Delete expired keys of one tenant (called by a periodic job with the tenant bound)."""
    return (
        db.query(IdempotencyKey)
        .filter(IdempotencyKey.company_id == company_id, IdempotencyKey.expires_at <= clock.utcnow())
        .delete(synchronize_session=False)
    )

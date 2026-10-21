import json
import math
import re
from collections.abc import Mapping
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.principal import Principal
from app.database.db import current_company_id
from app.models.audit_log import AuditLog

MASK = "****"

# Keys are matched on their last words (snake_case or camelCase), at any depth: `bank_account_number`
# and `accountNumber` match "account_number", while `account_number_last4` or `tax_id_type` do not.
_CREDENTIALS = ("password", "password_hash", "secret", "token", "api_key", "private_key", "otp", "cvv", "pin")
_IDENTIFIERS = (
    "account_number", "account_no", "iban", "tax_id", "tax_number", "tin", "pan", "ssn", "sin", "nino",
    "national_id", "passport_number", "aadhaar", "uan", "card_number", "registration_number",
)
_AMOUNTS = ("amount", "salary", "ctc", "gross", "net", "wage", "wages", "earnings")


def _parts(terms):
    return tuple(tuple(term.split("_")) for term in terms)


_CREDENTIAL_PARTS, _IDENTIFIER_PARTS, _AMOUNT_PARTS = _parts(_CREDENTIALS), _parts(_IDENTIFIERS), _parts(_AMOUNTS)


def _words(key) -> tuple:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    return tuple(word for word in re.split(r"[^0-9a-zA-Z]+", spaced.lower()) if word)


def _ends_with(words: tuple, terms) -> bool:
    return any(words[-len(parts):] == parts for parts in terms)


def _jsonable(value):
    """JSON-ready copy: decimals as exact strings (never floats), dates as ISO 8601."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return format(value, "f") if value.is_finite() else str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    if callable(getattr(value, "to_json", None)):  # e.g. app.payroll.money.Money -> {"amount": "...", "currency": "..."}
        return _jsonable(value.to_json())
    raise TypeError(f"audit details cannot hold {type(value).__name__} values")


def _mask(value, amounts: bool, key=None):
    if key is not None and value is not None and not isinstance(value, bool):
        words = _words(key)
        if _ends_with(words, _CREDENTIAL_PARTS):
            return MASK
        if _ends_with(words, _IDENTIFIER_PARTS):
            text = str(value) if isinstance(value, (str, int)) else ""
            return MASK + text[-4:] if len(text) > 4 else MASK
        if amounts and _ends_with(words, _AMOUNT_PARTS):
            return MASK
    if isinstance(value, dict):
        return {k: _mask(item, amounts, k) for k, item in value.items()}
    if isinstance(value, list):
        return [_mask(item, amounts) for item in value]
    return value


def mask_sensitive(details: Optional[Mapping[str, Any]], *, amounts: bool = True) -> Optional[dict]:
    """JSON-ready copy of `details` without raw secrets (contract #78, 3.9 §3.11).

    Credentials become "****"; bank, tax and identity numbers keep their last four characters
    ("****1234"); salary and amount values become "****" unless amounts=False. None and booleans
    are kept, so "no account before" or "verified: true" stay readable.
    """
    if details is None:
        return None
    if not isinstance(details, Mapping):
        raise TypeError("audit details must be a mapping")
    return _mask(_jsonable(details), amounts)


class AuditLogService:
    @staticmethod
    def log(
        db: Session,
        principal: Optional[Principal],
        module: str,
        action: str,
        description: str,
        entity_type: Optional[str] = None,
        entity_id=None,
        ip_address: Optional[str] = None,
        company_id: Optional[int] = None,
        actor_type: str = "user",
        actor_id: Optional[int] = None,
        actor_email: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
        mask_amounts: bool = False,
    ) -> AuditLog:
        """Queue an audit row in the caller's transaction (committed with the action).

        `details` carries structured values such as {"before": ..., "after": ..., "reason": ...}.
        Credentials and bank, tax and identity numbers are always masked; money amounts too when
        mask_amounts is set (for events readable without payroll access).
        """
        entry = AuditLog(
            company_id=company_id if company_id is not None else (
                principal.company_id if principal else current_company_id(db)
            ),
            actor_type=actor_type,
            actor_id=actor_id if actor_id is not None else (principal.user_id if principal else None),
            actor_email=actor_email if actor_email is not None else (principal.email if principal else None),
            module=module,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            description=description[:500],
            ip_address=ip_address,
            details=mask_sensitive(details, amounts=mask_amounts),
        )
        db.add(entry)
        return entry

    @staticmethod
    def list(db: Session, principal: Principal, skip: int = 0, limit: int = 50, module: Optional[str] = None):
        query = db.query(AuditLog).filter(AuditLog.company_id == principal.company_id)
        if module:
            query = query.filter(AuditLog.module == module)
        total = query.count()
        rows = query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).offset(skip).limit(limit).all()
        return total, rows

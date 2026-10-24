"""Payee bank accounts: entry, change control, verification and the read API for payments (3.7 §2-§5, §12).

* Entry. Accounts belong to a payee (``payee_type`` EMPLOYEE today; CONTRACTOR and NOMINEE are reserved).
  Details are checked with ``app.payroll.banking.validators`` for the account's country: fields of another
  country are refused (EC-BNK-04), a failed IBAN checksum is refused (EC-BNK-05). Passing those checks is
  recorded as a FORMAT verification event, which never makes an account VERIFIED (3.7 §4). The account
  number and IBAN are sealed with the tenant's data key (``keys.encrypt_field``, purpose PAYROLL_PII, bound
  to the column, the row and the payee); ``last4`` and a per-tenant keyed ``fingerprint`` are stored beside
  them. Responses, audit details, notifications and e-mails only ever carry ``••••4321`` (EC-BNK-11).
* Diversion defences (3.7 §3.2). Every change needs a step-up of the current session within 5 minutes
  (EC-BNK-06). An employee's change is approved through the approvals engine (module ``bank_change``) when
  ``payroll.bank.change_requires_approval`` is ON; a change made by HR always needs another person
  (maker-checker, EC-BNK-12). More than 2 changes in 90 days are blocked pending a security review
  (EC-BNK-13). A new account is usable only from ``approved_at + payroll.bank.cooling_off_hours`` unless
  an approver waives it with a reason (audited); the account it replaces keeps serving until then.
  The old and new contact details are alerted on every change (in-app and e-mail through the outbox,
  masked). Risk flags: holder name not matching the payee (EC-BNK-08), the same fingerprint on another
  payee (both flagged REQUIRES_REVIEW, payments to the newer one held, EC-BNK-07), a new device or IP.
* Verification follows the BANK_VERIFICATION machine and ``payroll.bank.verification_methods``:
  DOCUMENT_REVIEW (evidence uploaded to private storage, decided by someone other than the maker);
  PENNY_DROP and PROVIDER_NAME_MATCH run only through the SANDBOX adapter here (simulated, clearly
  labelled, refused in production). A failed or returned payment (``mark_payment_failed``, called by
  payments) sets verification FAILED and asks the employee to update the account (EC-BNK-10).
* Accounts are never deleted (trigger); history is effective-dated: ``usable_from`` and ``ended_at`` say
  which account served at any instant (``usable_accounts``).
* Unmasking (``reveal_account``) needs payroll:bank_details:read_full in scope, a reason and a fresh
  step-up; it is audited as BANK_DETAILS_REVEALED and limited per user per hour.

Services take (db, principal, ...) and never commit. ``decrypt_field`` is used only by ``reveal_account``
and ``beneficiary_plaintext`` (bank files).
"""

import hashlib
import html
import logging
import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pytz
from sqlalchemy import and_, func, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.core import authz, clock, storage
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.database.db import TenantContextError, current_company_id
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.employee import Employee
from app.models.payments import (
    OPEN_CHANGE_STATUSES,
    BankAccount,
    BankAccountChangeRequest,
    BankVerification,
)
from app.models.pay_org import PayGroup
from app.models.user import User, UserSession
from app.payroll import audit as payroll_audit
from app.payroll import countries, currencies
from app.payroll import state_machines as sm
from app.payroll.banking import controls, masking, validators
from app.payroll.banking.allocation import Allocation, validate_allocations
from app.payroll.banking.errors import BankingError
from app.payroll.money import decimal_str, to_decimal
from app.payroll.money_errors import MoneyError
from app.payroll.services import approvals, jobs, keys, outbox, step_up
from app.payroll.services import settings as settings_engine
from app.payroll.services.wage_inputs import audit, principal_for_user, resolve_setting, transition
from app.services.audit_log_service import AuditLogService
from app.services.notification_service import NotificationService

logger = logging.getLogger(__name__)

READ_MASKED = "payroll:bank_details:read_masked"
EDIT = "payroll:bank_details:edit"
APPROVE = "payroll:bank_details:approve"
READ_FULL = "payroll:bank_details:read_full"
VIEW_PERMISSIONS = (READ_MASKED, EDIT, APPROVE, READ_FULL)
MODULE = "bank_change"
SUBJECT = "bank_change_request"
ENTITY = "bank_account"
AUDIT_MODULE = payroll_audit.BANK_ACCOUNTS
PURPOSE = keys.DEFAULT_PURPOSE
EVIDENCE_CATEGORY = "bank-evidence"
ALERT_JOB = "payroll.bank_change_alert"
PAYEE_LOCK_NAMESPACE = 6121  # payroll advisory-lock range 6100-6199
NAME_MATCH_THRESHOLD = Decimal("0.75")
REVEAL_LIMIT_PER_HOUR = 10
REVEAL_REASON_MIN = 10
MAX_EVIDENCE = 10
EVIDENCE_TYPES = (
    (b"%PDF-", "pdf", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
)
WITHDRAWN = "Withdrawn by the requester"
PAYEE_TYPES = {"employee": "EMPLOYEE", "contractor": "CONTRACTOR", "nominee": "NOMINEE"}

# Risk flags on an account that put its verification into REQUIRES_REVIEW (3.7 §3.5).
DUPLICATE_ACCOUNT = "DUPLICATE_ACCOUNT"
HOLDER_NAME_MISMATCH = "HOLDER_NAME_MISMATCH"
PAYMENT_RETURNED = "PAYMENT_RETURNED"
REVIEW_FLAGS = frozenset({DUPLICATE_ACCOUNT, HOLDER_NAME_MISMATCH})
# Risk flags of a change request shown to the approver (3.7 §3.2 step 2).
NEW_DEVICE, NEW_IP, VELOCITY_LIMIT, HR_CHANGE = "NEW_DEVICE", "NEW_IP", "VELOCITY_LIMIT", "HR_CHANGE"

SANDBOX = "SANDBOX"
SANDBOX_LABEL = "SANDBOX: simulated check; no money moved and no bank or provider was contacted"
PROVIDER_METHODS = ("PENNY_DROP", "PROVIDER_NAME_MATCH")

_V = sm.VerificationStatus
_BA = sm.BankAccountStatus
_TITLES = frozenset({"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "shri", "smt", "sri", "kumari", "km", "md",
                     "sir", "madam"})


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
@dataclass(frozen=True, repr=False)
class BankDetails:
    """Bank details as entered (never logged: ``repr`` is masked)."""

    holder_name: str
    country: str
    currency: str
    account_number: Optional[str] = None
    iban: Optional[str] = None
    routing: Mapping[str, str] = field(default_factory=dict)
    account_type: Optional[str] = None
    bank_name: Optional[str] = None
    branch_name: Optional[str] = None
    bank_address: Optional[str] = None

    def __repr__(self) -> str:
        return (f"BankDetails(country={self.country!r}, currency={self.currency!r}, "
                f"account_number={masking.mask_account(self.account_number)!r}, "
                f"iban={masking.mask_iban(self.iban)!r}, holder_name={masking.mask_name(self.holder_name)!r})")


@dataclass(frozen=True)
class AllocationIn:
    type: str
    value: Optional[Decimal] = None
    order: int = 0


@dataclass(frozen=True)
class ClientMeta:
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None


@dataclass(frozen=True)
class CheckedDetails:
    """Normalised, format-checked details (method FORMAT only, never ownership)."""

    account: validators.BankAccountDetails
    currency: str
    rule_set: str
    checks: Tuple[str, ...]
    warnings: Tuple[Dict[str, str], ...]
    identity: Tuple[str, str, str]

    def __repr__(self) -> str:
        return f"CheckedDetails(rule_set={self.rule_set!r}, currency={self.currency!r})"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _clean(value: Optional[str], limit: int) -> Optional[str]:
    if value is None:
        return None
    text_value = " ".join(str(value).split())
    if not text_value:
        return None
    if validators.has_unsafe_characters(text_value) or len(text_value) > limit:
        raise bad_request(f"Use at most {limit} printable characters.", "TEXT_INVALID")
    return text_value


def _reason(value: Optional[str], *, required: bool = True, minimum: int = 3) -> Optional[str]:
    cleaned = " ".join((value or "").split())
    if not cleaned:
        if required:
            raise bad_request("Give a reason.", "REASON_REQUIRED")
        return None
    if len(cleaned) < minimum:
        raise bad_request(f"The reason needs at least {minimum} characters.", "REASON_REQUIRED")
    return masking.scrub_text(cleaned)[:1000]


def _invalid(issues: Sequence[Any]) -> AppError:
    detail = [{"field": i.field, "code": i.code, "msg": i.message} for i in issues]
    return AppError(422, detail, issues[0].code if issues else "BANK_DETAILS_INVALID")


def _now() -> datetime:
    return clock.utcnow()


def _local_date(db: Session, company_id: int, value: datetime) -> date:
    company = db.get(Company, company_id)
    return value.astimezone(pytz.timezone((company.timezone if company else None) or "UTC")).date()


def _check_version(row, expected: Optional[int]) -> None:
    if expected is not None and expected != row.row_version:
        raise conflict("This record changed since you loaded it. Reload and try again.", "ROW_VERSION_CONFLICT")


def mask_email(value: Optional[str]) -> str:
    if not value or "@" not in value:
        return ""
    local, domain = value.split("@", 1)
    return f"{local[:1]}***@{domain}"


def masked_number(row) -> str:
    """``••••4321`` or ``AE••••3456`` from the stored display columns (never from decrypted data)."""
    if row.iban_enc is not None:
        return f"{validators.iban_country(row.country) or ''}{masking.MASK}{row.last4}"
    return f"{masking.MASK}{row.last4}"


def _bank_line(row) -> str:
    return f"{row.bank_name} {masked_number(row)}" if row.bank_name else masked_number(row)


def payee_type_of(value: str) -> str:
    """EMPLOYEE / CONTRACTOR / NOMINEE from a path segment (404 for anything else)."""
    return _payee_type(value)


def _payee_type(value: str) -> str:
    found = PAYEE_TYPES.get(str(value).lower())
    if found is None:
        raise not_found("Payee")
    return found


def _employee_name(employee: Optional[Employee]) -> str:
    if employee is None:
        return ""
    return " ".join(part for part in (employee.first_name, employee.last_name) if part)


def _name_tokens(value: str) -> List[str]:
    decomposed = unicodedata.normalize("NFKD", value or "")
    letters = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()
    words = re.split(r"[^\w]+|_", letters)
    return [w for w in words if w and not w.isdigit() and w.rstrip(".") not in _TITLES]


def name_match_score(holder_name: str, legal_name: str) -> Decimal:
    """Token match between the holder name and the payee's legal name in [0, 1] (initials count half).

    Order does not matter ("Rao Asha" = "Asha Rao"); a single letter matches a word with that initial.
    """
    a, b = _name_tokens(holder_name), _name_tokens(legal_name)
    if not a or not b:
        return Decimal(0)
    remaining = list(b)
    matched = Decimal(0)
    for token in a:
        if token in remaining:
            remaining.remove(token)
            matched += 1
            continue
        initial = next((w for w in remaining if (len(token) == 1 and w.startswith(token))
                        or (len(w) == 1 and token.startswith(w))), None)
        if initial is not None:
            remaining.remove(initial)
            matched += Decimal("0.5")
    score = (2 * matched) / Decimal(len(a) + len(b))
    return min(Decimal(1), score).quantize(Decimal("0.0001"))


def _setting(db: Session, company_id: int, key: str, employee_id: Optional[int], on: date):
    return resolve_setting(db, company_id, key, on=on, employee_id=employee_id)


def _methods(db: Session, company_id: int, employee_id: Optional[int], on: date) -> frozenset:
    value = _setting(db, company_id, "payroll.bank.verification_methods", employee_id, on) or ()
    return frozenset(str(v).upper() for v in value)


# ---------------------------------------------------------------------------
# Details: validation and encryption (shared with funding accounts)
# ---------------------------------------------------------------------------
def check_details(details: BankDetails) -> CheckedDetails:
    """Country-driven format checks; 422 with field issues (never echoing numbers) when invalid."""
    country = (details.country or "").strip().upper()
    if countries.find(country) is None:
        raise AppError(422, [{"field": "country", "code": "COUNTRY_INVALID",
                              "msg": "the country must be an ISO 3166 two-letter code"}], "COUNTRY_INVALID")
    try:
        currency = currencies.require_payroll_currency((details.currency or "").strip().upper()).code
    except MoneyError as exc:
        raise AppError(422, [{"field": "currency", "code": exc.code, "msg": exc.message}], exc.code)
    routing = {str(k): v for k, v in (details.routing or {}).items()}
    try:
        result = validators.validate_account(validators.BankAccountDetails(
            country=country, holder_name=details.holder_name, account_number=details.account_number,
            iban=details.iban, routing=routing, account_type=details.account_type,
            bank_address=details.bank_address))
    except BankingError as exc:
        raise AppError(422, [{"field": "account", "code": exc.code, "msg": exc.message}], exc.code)
    if not result.valid:
        raise _invalid(result.errors)
    account = result.account
    if "ACCOUNT_NUMBER_NOT_USED" in result.warning_codes() and account.iban:
        account = validators.BankAccountDetails(
            country=account.country, holder_name=account.holder_name, account_number=None, iban=account.iban,
            routing=dict(account.routing), account_type=account.account_type, bank_address=account.bank_address)
    try:
        identity = validators.account_identity(account)
    except BankingError as exc:
        raise AppError(422, [{"field": "account_number", "code": exc.code, "msg": exc.message}], exc.code)
    warnings = tuple({"field": w.field, "code": w.code, "msg": w.message} for w in result.warnings)
    return CheckedDetails(account, currency, result.rule_set, result.checks, warnings, identity)


def _aad(table: str, column: str, row_id: int, owner: str) -> bytes:
    return f"{table}.{column}|id={row_id}|{owner}".encode("ascii")


def seal(db: Session, company_id: int, table: str, row_id: int, owner: str, checked: CheckedDetails) -> Dict[str, Any]:
    """Ciphertexts, last4 and fingerprint of checked details, bound to (table, column, row, owner)."""
    account = checked.account
    number = account.account_number
    iban = account.iban
    return {
        "account_number_enc": keys.encrypt_field(db, company_id, number, PURPOSE,
                                                 aad=_aad(table, "account_number", row_id, owner)) if number else None,
        "iban_enc": keys.encrypt_field(db, company_id, iban, PURPOSE, aad=_aad(table, "iban", row_id, owner))
        if iban else None,
        "last4": masking.last4(iban or number or ""),
        "fingerprint": keys.fingerprint(db, company_id, *checked.identity),
    }


def unseal(db: Session, company_id: int, table: str, row_id: int, owner: str, account_number_enc: Optional[str],
           iban_enc: Optional[str]) -> Dict[str, Optional[str]]:
    """Plaintext of sealed values (reveal and bank files only)."""
    return {
        "account_number": keys.decrypt_field(db, company_id, account_number_enc, PURPOSE,
                                             aad=_aad(table, "account_number", row_id, owner))
        if account_number_enc else None,
        "iban": keys.decrypt_field(db, company_id, iban_enc, PURPOSE, aad=_aad(table, "iban", row_id, owner))
        if iban_enc else None,
    }


def _owner(row: BankAccount) -> str:
    return f"payee={row.payee_type}:{row.payee_id}"


def _next_id(db: Session, table: str) -> int:
    return int(db.execute(text(f"SELECT nextval('{table}_id_seq')")).scalar())


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
def _allocation_json(row: BankAccount) -> dict:
    data = dict(row.allocation or {})
    return {"type": data.get("type", "PRIMARY"), "value": data.get("value"), "order": int(data.get("order") or 0)}


def _open_flags(row: BankAccount) -> List[str]:
    return [f.get("code") for f in (row.risk_flags or []) if f.get("open")]


def serialize_account(row: BankAccount, *, for_self: bool = False) -> dict:
    now = _now()
    data = {
        "id": row.id,
        "payee_type": row.payee_type,
        "payee_id": row.payee_id,
        "employee_id": row.employee_id,
        "holder_name": row.holder_name,
        "country": row.country,
        "currency": row.currency,
        "account_masked": masked_number(row),
        "last4": row.last4,
        "has_iban": row.iban_enc is not None,
        "routing": dict(row.routing or {}),
        "bank_name": row.bank_name,
        "branch_name": row.branch_name,
        "account_type": row.account_type,
        "allocation": _allocation_json(row),
        "status": row.status,
        "verification_status": row.verification_status,
        "verification_method": row.verification_method,
        "verified_at": row.verified_at,
        "approved_at": row.approved_at,
        "usable_from": row.usable_from,
        "cooling_off": row.usable_from is not None and row.usable_from > now and row.ended_at is None,
        "ended_at": row.ended_at,
        "effective_from": row.effective_from,
        "effective_to": row.effective_to,
        "replaces_account_id": row.replaces_account_id,
        "replaced_by_account_id": row.replaced_by_account_id,
        "status_reason": row.status_reason,
        "row_version": row.row_version,
        "created_at": row.created_at,
    }
    if for_self:
        data["under_review"] = row.verification_status == _V.REQUIRES_REVIEW or row.payment_hold
        data["update_required"] = row.verification_status == _V.FAILED
    else:
        data.update({
            "payment_hold": row.payment_hold,
            "hold_reason": row.hold_reason,
            "risk_flags": [dict(f) for f in (row.risk_flags or [])],
            "failure_reason": row.failure_reason,
            "last_failed_at": row.last_failed_at,
            "first_paid_at": row.first_paid_at,
            "created_by_user_id": row.created_by_user_id,
            "approved_by_user_id": row.approved_by_user_id,
        })
    return data


def _verification_json(row: BankVerification) -> dict:
    return {
        "id": row.id, "method": row.method, "result": row.result, "provider": row.provider,
        "sandbox": row.provider == SANDBOX, "provider_ref": row.provider_ref,
        "name_match_score": decimal_str(row.name_match_score) if row.name_match_score is not None else None,
        "evidence_of_id": row.evidence_of_id, "has_evidence": row.evidence_storage_path is not None,
        "evidence_file_name": row.evidence_file_name, "evidence_mime": row.evidence_mime,
        "evidence_sha256": row.evidence_sha256, "evidence_bytes": row.evidence_bytes, "reason": row.reason,
        "details": dict(row.details or {}), "performed_by_user_id": row.performed_by_user_id, "at": row.at,
    }


def serialize_change_request(db: Session, row: BankAccountChangeRequest, *, for_self: bool = False) -> dict:
    account = db.query(BankAccount).filter(BankAccount.company_id == row.company_id,
                                           BankAccount.id == row.bank_account_id).one()
    before = (
        db.query(BankAccount)
        .filter(BankAccount.company_id == row.company_id, BankAccount.payee_type == row.payee_type,
                BankAccount.payee_id == row.payee_id, BankAccount.id != row.bank_account_id,
                or_(BankAccount.status == _BA.ACTIVE, BankAccount.id == row.replaces_account_id))
        .order_by(BankAccount.id)
        .all()
    )
    data = {
        "id": row.id, "bank_account_id": row.bank_account_id, "payee_type": row.payee_type,
        "payee_id": row.payee_id, "employee_id": row.employee_id, "kind": row.kind,
        "replaces_account_id": row.replaces_account_id, "channel": row.channel, "status": row.status,
        "approval_request_id": row.approval_request_id, "decided_at": row.decided_at,
        "decision_reason": row.decision_reason, "cooling_off_waived": row.cooling_off_waived_reason is not None,
        "after": serialize_account(account, for_self=for_self),
        "before": [serialize_account(b, for_self=for_self) for b in before],
        "row_version": row.row_version, "created_at": row.created_at,
    }
    if not for_self:
        data.update({
            "requested_by_user_id": row.requested_by_user_id, "step_up_at": row.step_up_at,
            "step_up_method": row.step_up_method, "ip_address": row.ip_address, "user_agent": row.user_agent,
            "risk_flags": [dict(f) for f in (row.risk_flags or [])],
            "alert_recipients": [mask_email(e) for e in (row.contact_emails or [])],
            "security_reviewed_by_user_id": row.security_reviewed_by_user_id,
            "security_reviewed_at": row.security_reviewed_at, "security_review_reason": row.security_review_reason,
            "decided_by_user_id": row.decided_by_user_id,
            "cooling_off_waived_reason": row.cooling_off_waived_reason,
            "cooling_off_waived_by_user_id": row.cooling_off_waived_by_user_id,
        })
    return data


def _audit_details(row: BankAccount, **extra: Any) -> dict:
    return {"account": masked_number(row), "payee_type": row.payee_type, "payee_id": row.payee_id,
            "country": row.country, "currency": row.currency, **extra}


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------
def _scope_clause(db: Session, principal: Principal, permissions: Sequence[str], column):
    held = [p for p in permissions if principal.has(p)]
    if not held:
        raise forbidden()
    return or_(*[authz.employee_scope(db, principal, p).clause(column) for p in held])


def _load_account(db: Session, principal: Principal, account_id: int, permissions: Sequence[str], *,
                  own: bool = False, lock: bool = False) -> BankAccount:
    query = db.query(BankAccount).filter(BankAccount.company_id == principal.company_id, BankAccount.id == account_id)
    if own:
        if not any(principal.has(p) for p in permissions):
            raise forbidden()
        if principal.employee_id is None:
            raise not_found("Bank account")
        query = query.filter(BankAccount.payee_type == "EMPLOYEE", BankAccount.employee_id == principal.employee_id)
    else:
        query = query.filter(_scope_clause(db, principal, permissions, BankAccount.employee_id))
    if lock:
        query = query.with_for_update(of=BankAccount).populate_existing()
    row = query.first()
    if row is None:
        raise not_found("Bank account")
    return row


def _lock_account(db: Session, company_id: int, account_id: int) -> BankAccount:
    row = (db.query(BankAccount).filter(BankAccount.company_id == company_id, BankAccount.id == account_id)
           .with_for_update().populate_existing().first())
    if row is None:
        raise not_found("Bank account")
    return row


def _payee_employee(db: Session, principal: Principal, payee_type: str, payee_id: int, permission: str) -> Employee:
    """The payee (an employee) in the caller's scope for `permission`: 403 without it, 404 outside scope."""
    if not principal.has(permission):
        raise forbidden()
    if payee_type != "EMPLOYEE":
        raise not_found("Payee")
    return authz.ensure_employee_access(db, principal, permission, payee_id)


def _own_employee(db: Session, principal: Principal, permission: str) -> Employee:
    if not principal.has(permission):
        raise forbidden()
    if principal.employee_id is None:
        raise not_found("Employee profile")
    return authz.own_employee(db, principal)


def _lock_payee(db: Session, company_id: int, payee_type: str, payee_id: int) -> None:
    key = payee_id * 4 + ("EMPLOYEE", "CONTRACTOR", "NOMINEE").index(payee_type)
    db.execute(text("SELECT pg_advisory_xact_lock(:ns, :key)"), {"ns": PAYEE_LOCK_NAMESPACE, "key": key % 2147483647})


def _makers(db: Session, row: BankAccount) -> set:
    makers = {row.created_by_user_id}
    makers.update(u for (u,) in db.query(BankAccountChangeRequest.requested_by_user_id).filter(
        BankAccountChangeRequest.company_id == row.company_id, BankAccountChangeRequest.bank_account_id == row.id))
    makers.update(u for (u,) in db.query(BankVerification.performed_by_user_id).filter(
        BankVerification.company_id == row.company_id, BankVerification.bank_account_id == row.id,
        BankVerification.result == "EVIDENCE_SUBMITTED"))
    makers.discard(None)
    return makers


def _ensure_not_maker(db: Session, principal: Optional[Principal], row: BankAccount) -> None:
    """Maker-checker (EC-BNK-12): whoever entered the account or its evidence, and the payee, never decide it."""
    if principal is None:
        return
    if principal.user_id in _makers(db, row) or (row.employee_id is not None and principal.employee_id == row.employee_id):
        raise forbidden("The person who entered a bank change or its evidence cannot approve or verify it.")


def _require_recent_step_up_user(db: Session, user_id: int) -> None:
    """A step-up within 5 minutes on one of the user's live sessions (decisions through the generic inbox)."""
    cutoff = _now() - controls.STEP_UP_MAX_AGE
    found = db.query(UserSession.id).filter(UserSession.user_id == user_id, UserSession.revoked_at.is_(None),
                                            UserSession.step_up_at >= cutoff).first()
    if found is None:
        raise AppError(403, "Confirm it's you to continue.", "STEP_UP_REQUIRED")


# ---------------------------------------------------------------------------
# State machines (one transition function each: validation, permission, guards, version, audit)
# ---------------------------------------------------------------------------
def _account_transition(db: Session, principal: Optional[Principal], row: BankAccount, action: str, *,
                        reason: Optional[str] = None, system: bool = False, expected_row_version: Optional[int] = None,
                        change: Optional[BankAccountChangeRequest] = None, changes: Optional[Mapping[str, Any]] = None,
                        details: Optional[Mapping[str, Any]] = None) -> sm.TransitionSpec:
    """BANK_ACCOUNT transition of a row the caller locked; 409 for an invalid move or a stale row_version."""
    spec = transition(sm.BANK_ACCOUNT, row.status, action)
    if not system and spec.permission is not None and (principal is None or not principal.has(spec.permission)):
        raise forbidden()
    if spec.reason_required and not reason:
        raise bad_request("Give a reason.", "REASON_REQUIRED")
    _check_version(row, expected_row_version)
    if "STEP_UP_MFA" in spec.guards:
        if change is None or not controls.step_up_is_fresh(change.step_up_at, change.created_at):
            raise AppError(403, "Confirm it's you to continue.", "STEP_UP_REQUIRED")
    if "VELOCITY_LIMIT" in spec.guards and change is not None and change.security_reviewed_at is None \
            and any(f.get("code") == VELOCITY_LIMIT for f in change.risk_flags or []):
        raise conflict("This change is blocked pending a security review.", "SECURITY_REVIEW_REQUIRED")
    if "CHANGE_APPROVAL_OFF" in spec.guards and (change is None or change.channel != "SELF"):
        raise conflict("This change needs approval.", "APPROVAL_REQUIRED")
    if "MAKER_CHECKER" in spec.guards and not system:
        _ensure_not_maker(db, principal, row)
    before = row.status
    for key, value in (changes or {}).items():
        setattr(row, key, value)
    row.status = spec.target
    if reason:
        row.status_reason = reason
    row.row_version += 1
    db.flush()
    event = spec.audit_event or f"BANK_ACCOUNT_{action}"
    audit(db, principal, AUDIT_MODULE, event, f"Bank account #{row.id} {before} -> {spec.target} ({action})",
          ENTITY, row.id, row.company_id,
          **_audit_details(row, transition=action, from_status=before, to_status=spec.target, reason=reason,
                           **dict(details or {})))
    return spec


def _passed_methods(db: Session, row: BankAccount) -> frozenset:
    """FORMAT (the details never change) plus every method that passed after the latest failure."""
    events = (db.query(BankVerification.method, BankVerification.result)
              .filter(BankVerification.company_id == row.company_id, BankVerification.bank_account_id == row.id)
              .order_by(BankVerification.id).all())
    passed: set = set()
    for method, result in events:
        if result == "FAILED":
            passed.clear()
        elif result == "PASSED" and method in controls.METHODS:
            passed.add(method)
    passed.add("FORMAT")
    return frozenset(passed)


def _required_passed(db: Session, row: BankAccount) -> bool:
    required = _methods(db, row.company_id, row.employee_id, clock.company_today(db, row.company_id))
    try:
        status = controls.verification_status(_passed_methods(db, row), required)
    except BankingError as exc:
        raise conflict(f"payroll.bank.verification_methods cannot verify accounts: {exc.message}",
                       "VERIFICATION_POLICY_INVALID")
    return status == "VERIFIED"


def _verification_transition(db: Session, principal: Optional[Principal], row: BankAccount, action: str, *,
                             reason: Optional[str] = None, system: bool = False,
                             expected_row_version: Optional[int] = None, method: Optional[str] = None,
                             details: Optional[Mapping[str, Any]] = None) -> sm.TransitionSpec:
    """BANK_VERIFICATION transition of a row the caller locked."""
    spec = transition(sm.BANK_VERIFICATION, row.verification_status, action)
    if not system and spec.permission is not None and (principal is None or not principal.has(spec.permission)):
        raise forbidden()
    if spec.reason_required and not reason:
        raise bad_request("Give a reason.", "REASON_REQUIRED")
    _check_version(row, expected_row_version)
    if "MAKER_CHECKER" in spec.guards and not system:
        _ensure_not_maker(db, principal, row)
    if "REQUIRED_METHODS_PASSED" in spec.guards and not _required_passed(db, row):
        raise conflict("The verification methods required by policy have not all passed; a format check alone "
                       "never verifies an account.", "VERIFICATION_INCOMPLETE")
    if "RISK_FLAG" in spec.guards and not set(_open_flags(row)) & REVIEW_FLAGS:
        raise conflict("There is no open risk flag on this account.", "NO_RISK_FLAG")
    before = row.verification_status
    row.verification_status = spec.target
    if spec.target == _V.VERIFIED:
        row.verified_at = _now()
        row.verification_method = method or row.verification_method
    elif spec.target in (_V.PENDING, _V.FAILED, _V.EXPIRED) and before == _V.VERIFIED:
        row.verified_at = None
    row.row_version += 1
    db.flush()
    event = spec.audit_event or f"BANK_VERIFICATION_{action}"
    audit(db, principal, AUDIT_MODULE, event, f"Bank account #{row.id} verification {before} -> {spec.target}",
          ENTITY, row.id, row.company_id,
          **_audit_details(row, transition=action, from_status=before, to_status=spec.target, reason=reason,
                           method=method, **dict(details or {})))
    return spec


def _record(db: Session, row: BankAccount, method: str, result: str, principal: Optional[Principal], **values: Any
            ) -> BankVerification:
    event = BankVerification(company_id=row.company_id, bank_account_id=row.id, method=method, result=result,
                             performed_by_user_id=principal.user_id if principal is not None else None, at=_now(),
                             details=dict(values.pop("details", {}) or {}), **values)
    db.add(event)
    db.flush()
    return event


def _add_flag(row: BankAccount, code: str, **detail: Any) -> bool:
    flags = [dict(f) for f in (row.risk_flags or [])]
    if any(f.get("code") == code and f.get("open") for f in flags):
        return False
    flags.append({"code": code, "open": True, "at": _now().isoformat(), **detail})
    row.risk_flags = flags
    return True


def _close_flags(row: BankAccount, principal: Optional[Principal], reason: str) -> List[str]:
    closed = []
    flags = []
    for item in row.risk_flags or []:
        item = dict(item)
        if item.get("open"):
            item.update({"open": False, "closed_at": _now().isoformat(), "closed_reason": reason[:200],
                         "closed_by_user_id": principal.user_id if principal is not None else None})
            closed.append(item["code"])
        flags.append(item)
    row.risk_flags = flags
    return closed


def _sync_review(db: Session, row: BankAccount) -> None:
    """Open review flags move a PENDING or VERIFIED account to REQUIRES_REVIEW (system FLAG_FOR_REVIEW)."""
    if set(_open_flags(row)) & REVIEW_FLAGS and row.verification_status in (_V.PENDING, _V.VERIFIED):
        _verification_transition(db, None, row, "FLAG_FOR_REVIEW", system=True,
                                 details={"flags": sorted(set(_open_flags(row)) & REVIEW_FLAGS)})


def _settle(db: Session, principal: Optional[Principal], row: BankAccount) -> None:
    """PENDING with every required method passed becomes VERIFIED (system RECORD_VERIFIED)."""
    if row.verification_status == _V.PENDING and not set(_open_flags(row)) & REVIEW_FLAGS \
            and _required_passed(db, row):
        _verification_transition(db, principal, row, "RECORD_VERIFIED", system=True,
                                 method=_last_ownership_method(db, row))


def _last_ownership_method(db: Session, row: BankAccount) -> Optional[str]:
    found = (db.query(BankVerification.method)
             .filter(BankVerification.company_id == row.company_id, BankVerification.bank_account_id == row.id,
                     BankVerification.result == "PASSED", BankVerification.method.in_(sorted(controls.OWNERSHIP_METHODS)))
             .order_by(BankVerification.id.desc()).first())
    return found[0] if found else None


# ---------------------------------------------------------------------------
# Notifications (in-app + e-mail through the outbox; masked; locked ON)
# ---------------------------------------------------------------------------
_EVENTS = {
    "REQUESTED": ("Bank account change requested",
                  "A change of your salary bank account to {account} was requested on {when} ({channel}). "
                  "If this wasn't you, contact HR immediately."),
    "BLOCKED": ("Bank account change held for a security review",
                "A change of your salary bank account to {account} was requested on {when} ({channel}) and is held "
                "for a security review because of several recent changes. If this wasn't you, contact HR."),
    "APPROVED": ("Your salary bank account was changed",
                 "Your salary bank account was changed to {account} on {when}. It receives payments from "
                 "{usable}. If this wasn't you, contact HR immediately."),
    "REJECTED": ("Bank account change not approved",
                 "The change of your salary bank account to {account} was not applied."),
    "DEACTIVATED": ("Bank account removed",
                    "Your bank account {account} was deactivated on {when}. If this wasn't you, contact HR."),
    "PAYMENT_FAILED": ("Payment failed — please update your bank details",
                       "A payment to your bank account {account} failed or was returned. Please update your bank "
                       "details so that we can pay you."),
}
_CHANNELS = {"SELF": "self-service", "HR": "by HR", "BUREAU": "by your payroll provider"}


def _contacts(db: Session, employee: Optional[Employee]) -> List[str]:
    found: List[str] = []
    if employee is None:
        return found
    candidates = [employee.email]
    if employee.user_id is not None:
        user = db.query(User).filter(User.company_id == employee.company_id, User.id == employee.user_id).first()
        if user is not None:
            candidates.append(user.email)
    for value in candidates:
        value = (value or "").strip().lower()
        if value and "@" in value and value not in found:
            found.append(value)
    return found


def _message(db: Session, row: BankAccount, event: str, change: Optional[BankAccountChangeRequest]) -> Tuple[str, str]:
    title, template = _EVENTS[event]
    when = (change.created_at if change is not None and event in ("REQUESTED", "BLOCKED") else _now())
    usable = row.usable_from.strftime("%d %b %Y %H:%M UTC") if row.usable_from else "after approval"
    return title, template.format(account=_bank_line(row), when=when.strftime("%d %b %Y %H:%M UTC"),
                                  channel=_CHANNELS.get(change.channel if change is not None else "", "by HR"),
                                  usable=usable)


def _notify(db: Session, row: BankAccount, event: str, change: Optional[BankAccountChangeRequest] = None) -> None:
    from app.payroll.services import notifications as payroll_notifications

    if event == "PAYMENT_FAILED":
        # A payment status message (notify.payroll.payment_status_to_employee): OFF sends nothing; the account is
        # FAILED either way and the employee's own page asks for new details.
        if not payroll_notifications.payment_status_enabled(db, row.company_id, row.employee_id):
            return
    else:
        # Bank change alerts are locked ON (notify.payroll.bank_change_alert): resolved, never silenced.
        payroll_notifications.bank_change_alert_required(db, row.company_id)
    employee = None
    if row.employee_id is not None:
        employee = db.query(Employee).filter(Employee.company_id == row.company_id,
                                             Employee.id == row.employee_id).first()
    title, message = _message(db, row, event, change)
    if employee is not None and employee.user_id is not None:
        kind = "error" if event in ("PAYMENT_FAILED", "BLOCKED") else "warning" if event != "REJECTED" else "info"
        NotificationService.create(db, employee.user_id, title, message, kind, "/me/bank-accounts")
    payload = {"bank_account_id": row.id, "event_code": event}
    if change is not None:
        payload["change_request_id"] = change.id
    outbox.emit(db, ALERT_JOB, payload)


def _alert_recipients(db: Session, row: BankAccount, change: Optional[BankAccountChangeRequest]) -> List[str]:
    """The old and the new contact details: those on file now, at this change, and at the previous change."""
    employee = None
    if row.employee_id is not None:
        employee = db.query(Employee).filter(Employee.company_id == row.company_id,
                                             Employee.id == row.employee_id).first()
    recipients = _contacts(db, employee)
    sources = []
    if change is not None:
        sources.append(change.contact_emails or [])
        previous = (db.query(BankAccountChangeRequest)
                    .filter(BankAccountChangeRequest.company_id == row.company_id,
                            BankAccountChangeRequest.payee_type == row.payee_type,
                            BankAccountChangeRequest.payee_id == row.payee_id,
                            BankAccountChangeRequest.id < change.id)
                    .order_by(BankAccountChangeRequest.id.desc()).first())
        if previous is not None:
            sources.append(previous.contact_emails or [])
    for source in sources:
        for value in source:
            value = str(value).strip().lower()
            if value and value not in recipients:
                recipients.append(value)
    return recipients


@jobs.register(ALERT_JOB)
def _send_alerts(ctx: jobs.JobContext) -> dict:
    """E-mail a bank change alert to every old and new contact address (masked account only)."""
    from app.services import email_branding, email_templates
    from app.services.email_service import EmailService, OutgoingEmail

    db, company_id = ctx.db, ctx.company_id
    row = db.query(BankAccount).filter(BankAccount.company_id == company_id,
                                       BankAccount.id == ctx.payload.get("bank_account_id")).first()
    event = ctx.payload.get("event_code")
    if row is None or event not in _EVENTS:
        return {"sent": 0}
    change = None
    if ctx.payload.get("change_request_id") is not None:
        change = db.query(BankAccountChangeRequest).filter(
            BankAccountChangeRequest.company_id == company_id,
            BankAccountChangeRequest.id == ctx.payload["change_request_id"]).first()
    title, message = _message(db, row, event, change)
    brand = email_branding.email_brand(company_id)
    signature = f"The {brand['display_name']} team"
    detail = ("For your security we never show full account numbers. This alert is always sent to your current "
              "and previous contact details.")
    escape = html.escape
    recipients = _alert_recipients(db, row, change)
    sent = 0
    for address in recipients:
        email = OutgoingEmail(
            to=address, subject=title,
            html=email_templates._layout_html(brand, escape(message), escape(detail), escape(signature), None, None),
            text=email_templates._layout_text(brand, message, detail, signature, None, None),
            from_name=brand["sender_name"], reply_to=brand.get("support_email"),
            template_key="payroll.bank_change_alert", company_id=company_id,
        )
        EmailService.deliver(email)
        sent += 1
    audit(db, None, AUDIT_MODULE, "BANK_CHANGE_ALERT_SENT", f"Bank change alert ({event}) sent for account #{row.id}",
          ENTITY, row.id, company_id, event=event, recipients=[mask_email(a) for a in recipients],
          change_request_id=change.id if change is not None else None)
    return {"sent": sent}


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------
def _active_accounts(db: Session, company_id: int, payee_type: str, payee_id: int, *, lock: bool = False
                     ) -> List[BankAccount]:
    query = db.query(BankAccount).filter(BankAccount.company_id == company_id, BankAccount.payee_type == payee_type,
                                         BankAccount.payee_id == payee_id, BankAccount.status == _BA.ACTIVE)
    if lock:
        query = query.with_for_update()
    return query.order_by(BankAccount.id).all()


def _pay_currency(db: Session, company_id: int, employee_id: Optional[int], on: date, fallback: str) -> str:
    if employee_id is None:
        return fallback
    placement = settings_engine.primary_assignment(db, company_id, employee_id, on)
    if placement is None or placement.pay_group_id is None:
        return fallback
    group = db.query(PayGroup).filter(PayGroup.company_id == company_id, PayGroup.id == placement.pay_group_id).first()
    return group.currency if group is not None else fallback


def _allocation_rule(item: Optional[AllocationIn], *, split: bool, others: int) -> dict:
    if item is None:
        if split and others:
            raise bad_request("Say how this account shares the net pay with the other accounts.", "ALLOCATION_REQUIRED")
        return {"type": "PRIMARY", "value": None, "order": 0}
    kind = str(item.type).strip().upper()
    if not split and kind != "PRIMARY":
        raise bad_request("Split payments are not enabled: one account receives the whole net pay.",
                          "SPLIT_PAYMENTS_DISABLED")
    value = None
    if item.value is not None:
        try:
            value = decimal_str(to_decimal(item.value))
        except MoneyError as exc:
            raise bad_request(exc.message, exc.code)
    return {"type": kind, "value": value, "order": int(item.order or 0)}


def _check_allocations(rules: Sequence[Tuple[Any, dict, str]], *, max_accounts: int, pay_currency: str) -> None:
    """Save-time allocation rules of the payee's accounts after the change (EC-BNK-09)."""
    issues = validate_allocations(
        [Allocation(account_id=key, type=rule["type"],
                    value=Decimal(rule["value"]) if rule.get("value") is not None else None,
                    order=int(rule.get("order") or 0), currency=currency) for key, rule, currency in rules],
        max_accounts=max_accounts, pay_currency=pay_currency)
    errors = [i for i in issues if i.severity == "ERROR"]
    if errors:
        raise AppError(400, [{"field": "allocation", "code": i.code, "msg": i.message} for i in errors], errors[0].code)


def _split(db: Session, company_id: int, employee_id: Optional[int], on: date) -> Tuple[bool, int]:
    split = bool(_setting(db, company_id, "payroll.bank.split_payments_enabled", employee_id, on))
    maximum = int(_setting(db, company_id, "payroll.bank.max_accounts_per_payee", employee_id, on) or 1) if split else 1
    return split, maximum


def _device_flags(db: Session, principal: Principal, meta: ClientMeta) -> List[dict]:
    """NEW_DEVICE / NEW_IP when the user's earlier sessions (90 days) never used this IP or browser."""
    since = _now() - timedelta(days=90)
    rows = (db.query(UserSession.ip_address, UserSession.user_agent)
            .filter(UserSession.user_id == principal.user_id, UserSession.created_at >= since,
                    UserSession.id != (principal.session_id or ""))
            .all())
    if not rows:
        return []
    flags = []
    if meta.ip_address and meta.ip_address not in {ip for ip, _ in rows}:
        flags.append({"code": NEW_IP, "detail": "First change from this network address"})
    if meta.user_agent and meta.user_agent[:255] not in {ua for _, ua in rows}:
        flags.append({"code": NEW_DEVICE, "detail": "First change from this browser or device"})
    return flags


def _apply_duplicates(db: Session, row: BankAccount) -> List[int]:
    """The same fingerprint on another payee flags every account involved and holds the newer payees' (EC-BNK-07)."""
    group = (db.query(BankAccount)
             .filter(BankAccount.company_id == row.company_id, BankAccount.fingerprint == row.fingerprint)
             .order_by(BankAccount.id).with_for_update().all())
    findings = controls.duplicate_fingerprints(
        controls.AccountFingerprint(a.id, f"{a.payee_type}:{a.payee_id}", a.fingerprint, a.id) for a in group)
    touched: List[int] = []
    by_id = {a.id: a for a in group}
    for finding in findings:
        for account_id in finding.flagged_account_ids:
            account = by_id[account_id]
            others = [f"{p}" for p in finding.payee_ids if p != f"{account.payee_type}:{account.payee_id}"]
            changed = _add_flag(account, DUPLICATE_ACCOUNT, role="NEWER" if account_id in finding.held_account_ids
                                else "OLDER", detail="The same account is registered for another payee")
            if account_id in finding.held_account_ids and not account.payment_hold:
                account.payment_hold, account.hold_reason = True, "DUPLICATE_FINGERPRINT"
                changed = True
            if changed:
                account.row_version += 1
                touched.append(account_id)
                db.flush()
                audit(db, None, AUDIT_MODULE, "BANK_DUPLICATE_FLAGGED",
                      f"Bank account #{account.id} shares its account with another payee", ENTITY, account.id,
                      account.company_id, **_audit_details(account, other_payees=len(others),
                                                            held=account.payment_hold))
            _sync_review(db, account)
    return touched


def submit_account(db: Session, principal: Principal, *, details: BankDetails, payee_type: str = "EMPLOYEE",
                   payee_id: Optional[int] = None, allocation: Optional[AllocationIn] = None,
                   replaces_account_id: Optional[int] = None, meta: ClientMeta = ClientMeta(),
                   self_service: bool = False) -> BankAccountChangeRequest:
    """Submit a new bank account for a payee: checks, encryption, defences, approval routing (3.7 §3)."""
    company_id = principal.company_id
    if self_service:
        employee = _own_employee(db, principal, EDIT)
        channel = "SELF"
    else:
        employee = _payee_employee(db, principal, payee_type, int(payee_id or 0), EDIT)
        channel = "HR"
    payee_type, payee_id = "EMPLOYEE", employee.id
    today = clock.company_today(db, company_id)
    if self_service and not _setting(db, company_id, "payroll.bank.self_service_enabled", employee.id, today):
        raise conflict("Bank account self-service is switched off; ask HR to update your account.",
                       "BANK_SELF_SERVICE_DISABLED")
    step_up.ensure_step_up(db, principal)  # EC-BNK-06
    session = db.get(UserSession, principal.session_id) if principal.session_id else None
    checked = check_details(details)
    bank_name = _clean(details.bank_name, 140)
    branch_name = _clean(details.branch_name, 140)

    _lock_payee(db, company_id, payee_type, payee_id)
    pending = db.query(BankAccountChangeRequest.id).filter(
        BankAccountChangeRequest.company_id == company_id, BankAccountChangeRequest.payee_type == payee_type,
        BankAccountChangeRequest.payee_id == payee_id,
        BankAccountChangeRequest.status.in_(OPEN_CHANGE_STATUSES)).first()
    if pending is not None:
        raise conflict("Another bank account change for this person is still open.", "CHANGE_ALREADY_PENDING")

    split, max_accounts = _split(db, company_id, employee.id, today)
    active = _active_accounts(db, company_id, payee_type, payee_id)
    if replaces_account_id is not None:
        replaced = [a for a in active if a.id == replaces_account_id]
        if not replaced:
            raise not_found("Bank account")
    else:
        replaced = [] if split else list(active)
    remaining = [a for a in active if a not in replaced]
    rule = _allocation_rule(allocation, split=split, others=len(remaining))
    pay_currency = _pay_currency(db, company_id, employee.id, today, checked.currency)
    _check_allocations([(a.id, _allocation_json(a), a.currency) for a in remaining]
                       + [("new", rule, checked.currency)], max_accounts=max_accounts, pay_currency=pay_currency)

    now = _now()
    window = now - controls.VELOCITY_WINDOW
    previous = [t for (t,) in db.query(BankAccountChangeRequest.created_at).filter(
        BankAccountChangeRequest.company_id == company_id, BankAccountChangeRequest.payee_type == payee_type,
        BankAccountChangeRequest.payee_id == payee_id, BankAccountChangeRequest.created_at >= window)]
    blocked = controls.velocity_blocked(previous, now)  # EC-BNK-13

    account_id = _next_id(db, "bank_accounts")
    owner = f"payee={payee_type}:{payee_id}"
    sealed = seal(db, company_id, "bank_accounts", account_id, owner, checked)
    row = BankAccount(
        id=account_id, company_id=company_id, payee_type=payee_type, payee_id=payee_id, employee_id=employee.id,
        holder_name=checked.account.holder_name, country=checked.account.country, currency=checked.currency,
        routing=dict(checked.account.routing), bank_name=bank_name, branch_name=branch_name,
        bank_address=checked.account.bank_address, account_type=checked.account.account_type, allocation=rule,
        status=_BA.DRAFT, verification_status=_V.NOT_SUBMITTED, risk_flags=[], payment_hold=False,
        replaces_account_id=replaced[0].id if replaced else None, created_by_user_id=principal.user_id,
        row_version=1, **sealed,
    )
    legal_name = _employee_name(employee)
    score = name_match_score(row.holder_name, legal_name)
    if score < NAME_MATCH_THRESHOLD:  # EC-BNK-08
        _add_flag(row, HOLDER_NAME_MISMATCH, score=decimal_str(score),
                  detail="The account holder's name does not match the payee's legal name")
    db.add(row)
    db.flush()

    change_flags = _device_flags(db, principal, meta)
    if channel == "HR":
        change_flags.append({"code": HR_CHANGE, "detail": "Entered by HR on the employee's behalf"})
    if blocked:
        change_flags.append({"code": VELOCITY_LIMIT, "detail": "More than 2 bank changes in 90 days"})
    change = BankAccountChangeRequest(
        company_id=company_id, bank_account_id=row.id, payee_type=payee_type, payee_id=payee_id,
        employee_id=employee.id, kind="REPLACE" if replaced else "ADD",
        replaces_account_id=replaced[0].id if replaced else None, requested_by_user_id=principal.user_id,
        channel=channel, step_up_at=session.step_up_at if session is not None else None,
        step_up_method=session.step_up_method if session is not None else None,
        ip_address=(meta.ip_address or None) and meta.ip_address[:64],
        user_agent=(meta.user_agent or None) and meta.user_agent[:255],
        risk_flags=change_flags + [{"code": f["code"], "detail": f.get("detail")} for f in row.risk_flags],
        contact_emails=_contacts(db, employee),
        status="SECURITY_REVIEW" if blocked else "PENDING_APPROVAL", created_at=now, row_version=1,
    )
    db.add(change)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        raise conflict("Another bank account change for this person is still open.", "CHANGE_ALREADY_PENDING")
    _record(db, row, "FORMAT", "PASSED", principal,
            details={"rule_set": checked.rule_set, "checks": list(checked.checks),
                     "warnings": [w["code"] for w in checked.warnings],
                     "note": "Format checks prove the format only, never ownership"})
    audit(db, principal, AUDIT_MODULE, "BANK_CHANGE_REQUESTED",
          f"Bank account #{row.id} submitted for payee {payee_type}:{payee_id} ({channel})", SUBJECT, change.id,
          company_id, **_audit_details(row, bank_account_id=row.id, channel=channel, kind=change.kind,
                                       replaces_account_id=change.replaces_account_id,
                                       risk_flags=[f["code"] for f in change.risk_flags],
                                       velocity_blocked=blocked, ip_address=change.ip_address))
    if blocked:
        audit(db, principal, AUDIT_MODULE, "BANK_CHANGE_SECURITY_REVIEW",
              f"Bank change #{change.id} held for a security review (velocity limit)", SUBJECT, change.id,
              company_id, changes_in_window=len(previous) + 1)
        _notify(db, row, "BLOCKED", change)
    else:
        _route(db, principal, row, change)
        _notify(db, row, "REQUESTED", change)
    _apply_duplicates(db, row)
    if blocked is False and change.status == "APPLIED":
        _notify(db, row, "APPROVED", change)
    return change


def _route(db: Session, principal: Optional[Principal], row: BankAccount, change: BankAccountChangeRequest) -> None:
    """Submitted account: approval (always for HR, else per setting) or activation after cooling-off."""
    today = clock.company_today(db, row.company_id)
    requires = change.channel != "SELF" or bool(
        _setting(db, row.company_id, "payroll.bank.change_requires_approval", row.employee_id, today))
    if requires:
        _account_transition(db, principal, row, "SUBMIT", system=True, change=change)
        _verification_transition(db, principal, row, "SUBMIT", system=True)
        _sync_review(db, row)
        requester = principal_for_user(db, row.company_id, change.requested_by_user_id)
        request = approvals.submit(
            db, requester, module=MODULE, subject_type=SUBJECT, subject_id=change.id,
            subject_employee_id=row.employee_id,
            context={"maker_user_ids": [u for u in (change.requested_by_user_id,) if u is not None],
                     "channel": change.channel, "bank_account_id": row.id, "kind": change.kind,
                     "risk_flags": [f["code"] for f in change.risk_flags]},
        )
        change.status = "PENDING_APPROVAL"
        change.approval_request_id = request.id
        change.row_version += 1
        db.flush()
    else:
        _activate(db, principal, row, change, action="ACTIVATE")


def _activate(db: Session, principal: Optional[Principal], row: BankAccount, change: BankAccountChangeRequest, *,
              action: str) -> None:
    """ACTIVATE or APPROVE: start the cooling-off, replace the old account(s) when the new one becomes usable."""
    company_id = row.company_id
    today = clock.company_today(db, company_id)
    now = _now()
    hours = int(_setting(db, company_id, "payroll.bank.cooling_off_hours", row.employee_id, today))
    usable = controls.usable_from(now, hours, waived_reason=change.cooling_off_waived_reason)
    split, max_accounts = _split(db, company_id, row.employee_id, today)
    active = [a for a in _active_accounts(db, company_id, row.payee_type, row.payee_id, lock=True) if a.id != row.id]
    if split:
        replaced = [a for a in active if a.id == change.replaces_account_id]
    else:
        replaced = active
    remaining = [a for a in active if a not in replaced]
    _check_allocations([(a.id, _allocation_json(a), a.currency) for a in remaining]
                       + [(row.id, _allocation_json(row), row.currency)], max_accounts=max_accounts,
                       pay_currency=_pay_currency(db, company_id, row.employee_id, today, row.currency))
    if action == "ACTIVATE":
        _account_transition(db, principal, row, "ACTIVATE", system=True, change=change,
                            changes={"approved_at": now, "usable_from": usable,
                                     "effective_from": _local_date(db, company_id, usable)},
                            details={"usable_from": usable, "cooling_off_hours": hours})
        _verification_transition(db, principal, row, "SUBMIT", system=True)
        _sync_review(db, row)
    else:
        _account_transition(db, principal, row, "APPROVE", system=principal is None,
                            changes={"approved_at": now, "approved_by_user_id": principal.user_id if principal else None,
                                     "usable_from": usable, "effective_from": _local_date(db, company_id, usable)},
                            details={"usable_from": usable, "cooling_off_hours": hours,
                                     "cooling_off_waived": change.cooling_off_waived_reason is not None})
    if change.cooling_off_waived_reason is not None:
        audit(db, principal, AUDIT_MODULE, "BANK_COOLING_OFF_WAIVED",
              f"Cooling-off of bank account #{row.id} waived", ENTITY, row.id, company_id,
              **_audit_details(row, reason=change.cooling_off_waived_reason, cooling_off_hours=hours))
    for old in replaced:
        old_from = old.effective_from or _local_date(db, company_id, usable)
        _account_transition(db, principal, old, "REPLACE", system=True,
                            changes={"ended_at": usable, "replaced_by_account_id": row.id,
                                     "effective_to": max(old_from, _local_date(db, company_id, usable))},
                            details={"replaced_by_account_id": row.id, "serves_until": usable})
    change.status = "APPLIED"
    change.decided_at = now
    change.decided_by_user_id = principal.user_id if principal is not None and action == "APPROVE" else None
    change.row_version += 1
    db.flush()


@approvals.on_decided(MODULE, SUBJECT)
def _on_decided(db: Session, principal: Optional[Principal], request) -> None:
    change = (db.query(BankAccountChangeRequest)
              .filter(BankAccountChangeRequest.company_id == request.company_id,
                      BankAccountChangeRequest.id == request.subject_id)
              .with_for_update().populate_existing().one())
    if change.status != "PENDING_APPROVAL":
        return
    row = _lock_account(db, change.company_id, change.bank_account_id)
    remarks = next((s.remarks for s in reversed(request.steps) if s.remarks), None)
    if request.status == approvals.APPROVED:
        if principal is not None:
            _require_recent_step_up_user(db, principal.user_id)
        change.decision_reason = remarks
        _activate(db, principal, row, change, action="APPROVE")
        _notify(db, row, "APPROVED", change)
        return
    withdrawn = request.status == approvals.CANCELLED
    reason = WITHDRAWN if withdrawn else (remarks or "Rejected")
    _account_transition(db, principal, row, "REJECT", system=True, reason=reason)
    change.status = "CANCELLED" if withdrawn else "REJECTED"
    change.decided_at = _now()
    change.decided_by_user_id = None if withdrawn or principal is None else principal.user_id
    change.decision_reason = reason
    change.cooling_off_waived_reason = change.cooling_off_waived_by_user_id = None
    change.row_version += 1
    db.flush()
    audit(db, principal, AUDIT_MODULE, "BANK_CHANGE_CANCELLED" if withdrawn else "BANK_CHANGE_REJECTED",
          f"Bank change #{change.id} {'withdrawn' if withdrawn else 'rejected'}", SUBJECT, change.id,
          change.company_id, bank_account_id=row.id, reason=reason)
    if not withdrawn:
        _notify(db, row, "REJECTED", change)


# ---------------------------------------------------------------------------
# Change requests: listing and decisions
# ---------------------------------------------------------------------------
def _load_change(db: Session, principal: Principal, change_id: int, permissions: Sequence[str], *,
                 own: bool = False, lock: bool = False) -> BankAccountChangeRequest:
    query = db.query(BankAccountChangeRequest).filter(BankAccountChangeRequest.company_id == principal.company_id,
                                                      BankAccountChangeRequest.id == change_id)
    if own:
        if not any(principal.has(p) for p in permissions):
            raise forbidden()
        if principal.employee_id is None:
            raise not_found("Bank change request")
        query = query.filter(BankAccountChangeRequest.employee_id == principal.employee_id)
    else:
        query = query.filter(_scope_clause(db, principal, permissions, BankAccountChangeRequest.employee_id))
    if lock:
        query = query.with_for_update(of=BankAccountChangeRequest).populate_existing()
    row = query.first()
    if row is None:
        raise not_found("Bank change request")
    return row


def list_change_requests(db: Session, principal: Principal, *, own: bool = False, status: Optional[str] = None,
                         employee_id: Optional[int] = None, skip: int = 0, limit: int = 50) -> Tuple[int, List[dict]]:
    query = db.query(BankAccountChangeRequest).filter(BankAccountChangeRequest.company_id == principal.company_id)
    if own:
        if principal.employee_id is None or not any(principal.has(p) for p in (READ_MASKED, EDIT)):
            raise forbidden()
        query = query.filter(BankAccountChangeRequest.employee_id == principal.employee_id)
    else:
        query = query.filter(_scope_clause(db, principal, VIEW_PERMISSIONS, BankAccountChangeRequest.employee_id))
    if status:
        query = query.filter(BankAccountChangeRequest.status == status)
    if employee_id is not None:
        query = query.filter(BankAccountChangeRequest.employee_id == employee_id)
    total = query.count()
    rows = query.order_by(BankAccountChangeRequest.created_at.desc(), BankAccountChangeRequest.id.desc()) \
        .offset(skip).limit(limit).all()
    return total, [serialize_change_request(db, r, for_self=own) for r in rows]


def get_change_request(db: Session, principal: Principal, change_id: int, *, own: bool = False) -> dict:
    permissions = (READ_MASKED, EDIT) if own else VIEW_PERMISSIONS
    return serialize_change_request(db, _load_change(db, principal, change_id, permissions, own=own), for_self=own)


def decide_change_request(db: Session, principal: Principal, change_id: int, decision: str,
                          remarks: Optional[str] = None, expected_step: Optional[int] = None,
                          waive_cooling_off_reason: Optional[str] = None) -> BankAccountChangeRequest:
    """Approve or reject through the approvals engine (maker-checker there and here); step-up required."""
    change = _load_change(db, principal, change_id, (APPROVE,), lock=True)
    step_up.ensure_step_up(db, principal)
    if change.status != "PENDING_APPROVAL":
        raise conflict("This bank change is not awaiting approval.", "NOT_PENDING")
    request = approvals.pending_request(db, MODULE, SUBJECT, change.id)
    if request is None:
        raise conflict("This bank change is not awaiting approval.", "NOT_PENDING")
    account = _lock_account(db, change.company_id, change.bank_account_id)
    _ensure_not_maker(db, principal, account)
    if decision.upper() == "APPROVE" and waive_cooling_off_reason is not None:
        change.cooling_off_waived_reason = _reason(waive_cooling_off_reason)
        change.cooling_off_waived_by_user_id = principal.user_id
        db.flush()
    approvals.decide(db, principal, request.id, decision, remarks, expected_step)
    db.refresh(change)
    return change


def cancel_change_request(db: Session, principal: Principal, change_id: int, remarks: Optional[str] = None
                          ) -> BankAccountChangeRequest:
    """The requester withdraws an open change (approval pending or held for review)."""
    change = _load_change(db, principal, change_id, (EDIT, READ_MASKED), own=True, lock=True)
    if change.requested_by_user_id != principal.user_id:
        raise not_found("Bank change request")
    if change.status == "PENDING_APPROVAL":
        request = approvals.pending_request(db, MODULE, SUBJECT, change.id)
        if request is None:
            raise conflict("This bank change is not open.", "NOT_PENDING")
        approvals.cancel(db, principal, request.id, remarks)
    elif change.status == "SECURITY_REVIEW":
        row = _lock_account(db, change.company_id, change.bank_account_id)
        row.status_reason = WITHDRAWN
        row.row_version += 1
        change.status, change.decided_at, change.decision_reason = "CANCELLED", _now(), WITHDRAWN
        change.row_version += 1
        db.flush()
        audit(db, principal, AUDIT_MODULE, "BANK_CHANGE_CANCELLED", f"Bank change #{change.id} withdrawn", SUBJECT,
              change.id, change.company_id, bank_account_id=row.id)
    else:
        raise conflict("This bank change is not open.", "NOT_PENDING")
    db.refresh(change)
    return change


def security_review(db: Session, principal: Principal, change_id: int, decision: str, reason: str
                    ) -> BankAccountChangeRequest:
    """Release (continue to approval) or reject a change held by the velocity limit (EC-BNK-13)."""
    change = _load_change(db, principal, change_id, (APPROVE,), lock=True)
    step_up.ensure_step_up(db, principal)
    if change.status != "SECURITY_REVIEW":
        raise conflict("This bank change is not held for a security review.", "NOT_IN_SECURITY_REVIEW")
    row = _lock_account(db, change.company_id, change.bank_account_id)
    _ensure_not_maker(db, principal, row)
    reason = _reason(reason)
    decision = str(decision).upper()
    if decision not in ("RELEASE", "REJECT"):
        raise bad_request("Decision must be RELEASE or REJECT.", "INVALID_DECISION")
    change.security_reviewed_by_user_id = principal.user_id
    change.security_reviewed_at = _now()
    change.security_review_reason = reason
    change.row_version += 1
    db.flush()
    audit(db, principal, AUDIT_MODULE, "BANK_CHANGE_SECURITY_REVIEWED",
          f"Bank change #{change.id} security review: {decision}", SUBJECT, change.id, change.company_id,
          bank_account_id=row.id, decision=decision, reason=reason)
    if decision == "RELEASE":
        _route(db, principal_for_user(db, change.company_id, change.requested_by_user_id), row, change)
        if change.status == "APPLIED":
            _notify(db, row, "APPROVED", change)
    else:
        row.status_reason = reason
        row.row_version += 1
        change.status, change.decided_at, change.decided_by_user_id = "REJECTED", _now(), principal.user_id
        change.decision_reason = reason
        db.flush()
        _notify(db, row, "REJECTED", change)
    return change


def waive_cooling_off(db: Session, principal: Principal, account_id: int, reason: str,
                      expected_row_version: Optional[int] = None) -> BankAccount:
    """An approver ends a running cooling-off period now, with a reason (audited)."""
    row = _load_account(db, principal, account_id, (APPROVE,), lock=True)
    step_up.ensure_step_up(db, principal)
    _check_version(row, expected_row_version)
    now = _now()
    if row.status != _BA.ACTIVE or row.usable_from is None or row.usable_from <= now:
        raise conflict("This account is not in a cooling-off period.", "NOT_COOLING_OFF")
    _ensure_not_maker(db, principal, row)
    reason = _reason(reason)
    before = row.usable_from
    row.usable_from = now
    row.effective_from = _local_date(db, row.company_id, now)
    row.row_version += 1
    for old in db.query(BankAccount).filter(BankAccount.company_id == row.company_id,
                                            BankAccount.replaced_by_account_id == row.id,
                                            BankAccount.ended_at == before).with_for_update():
        old.ended_at = now
        old.effective_to = max(old.effective_from or row.effective_from, row.effective_from)
        old.row_version += 1
    change = (db.query(BankAccountChangeRequest)
              .filter(BankAccountChangeRequest.company_id == row.company_id,
                      BankAccountChangeRequest.bank_account_id == row.id).order_by(BankAccountChangeRequest.id.desc())
              .with_for_update().first())
    if change is not None:
        change.cooling_off_waived_reason, change.cooling_off_waived_by_user_id = reason, principal.user_id
        change.row_version += 1
    db.flush()
    audit(db, principal, AUDIT_MODULE, "BANK_COOLING_OFF_WAIVED", f"Cooling-off of bank account #{row.id} waived",
          ENTITY, row.id, row.company_id, **_audit_details(row, reason=reason, was_usable_from=before))
    return row


def deactivate_account(db: Session, principal: Principal, account_id: int, reason: str, *, own: bool = False,
                       expected_row_version: Optional[int] = None) -> BankAccount:
    """ACTIVE -> INACTIVE now (step-up; the payee's remaining accounts must still allocate the net pay)."""
    row = _load_account(db, principal, account_id, (EDIT,), own=own, lock=True)
    step_up.ensure_step_up(db, principal)
    reason = _reason(reason)
    _lock_payee(db, row.company_id, row.payee_type, row.payee_id)
    today = clock.company_today(db, row.company_id)
    remaining = [a for a in _active_accounts(db, row.company_id, row.payee_type, row.payee_id) if a.id != row.id]
    if remaining:
        split, max_accounts = _split(db, row.company_id, row.employee_id, today)
        _check_allocations([(a.id, _allocation_json(a), a.currency) for a in remaining], max_accounts=max(max_accounts,
                           len(remaining)), pay_currency=_pay_currency(db, row.company_id, row.employee_id, today,
                                                                       row.currency))
    now = _now()
    effective_to = max(today, row.effective_from or today)
    _account_transition(db, principal, row, "DEACTIVATE", reason=reason, expected_row_version=expected_row_version,
                        changes={"ended_at": max(now, row.usable_from or now), "effective_to": effective_to},
                        details={"self_service": own, "remaining_accounts": len(remaining)})
    _notify(db, row, "DEACTIVATED")
    return row


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def list_accounts(db: Session, principal: Principal, *, own: bool = False, employee_id: Optional[int] = None,
                  status: Optional[str] = None, verification_status: Optional[str] = None,
                  payment_hold: Optional[bool] = None, skip: int = 0, limit: int = 50) -> Tuple[int, List[dict]]:
    query = db.query(BankAccount).filter(BankAccount.company_id == principal.company_id)
    if own:
        if principal.employee_id is None or not any(principal.has(p) for p in (READ_MASKED, EDIT)):
            raise forbidden()
        query = query.filter(BankAccount.payee_type == "EMPLOYEE", BankAccount.employee_id == principal.employee_id)
    else:
        query = query.filter(_scope_clause(db, principal, VIEW_PERMISSIONS, BankAccount.employee_id))
    if employee_id is not None:
        query = query.filter(BankAccount.employee_id == employee_id)
    if status:
        query = query.filter(BankAccount.status == status)
    if verification_status:
        query = query.filter(BankAccount.verification_status == verification_status)
    if payment_hold is not None:
        query = query.filter(BankAccount.payment_hold.is_(payment_hold))
    total = query.count()
    rows = query.order_by(BankAccount.id.desc()).offset(skip).limit(limit).all()
    return total, [serialize_account(r, for_self=own) for r in rows]


def payee_accounts(db: Session, principal: Principal, payee_type: str, payee_id: int) -> List[dict]:
    kind = _payee_type(payee_type)
    if not any(principal.has(p) for p in VIEW_PERMISSIONS):
        raise forbidden()
    if kind != "EMPLOYEE":
        raise not_found("Payee")
    held = [p for p in VIEW_PERMISSIONS if principal.has(p)]
    if not any(authz.can_access_employee(db, principal, p, payee_id) for p in held):
        raise not_found("Payee")
    rows = (db.query(BankAccount)
            .filter(BankAccount.company_id == principal.company_id, BankAccount.payee_type == kind,
                    BankAccount.payee_id == payee_id)
            .order_by(BankAccount.id.desc()).all())
    return [serialize_account(r) for r in rows]


def get_account(db: Session, principal: Principal, account_id: int, *, own: bool = False) -> dict:
    row = _load_account(db, principal, account_id, (READ_MASKED, EDIT) if own else VIEW_PERMISSIONS, own=own)
    data = serialize_account(row, for_self=own)
    events = (db.query(BankVerification)
              .filter(BankVerification.company_id == row.company_id, BankVerification.bank_account_id == row.id)
              .order_by(BankVerification.id).all())
    data["verifications"] = [_verification_json(e) for e in events] if not own else [
        {"id": e.id, "method": e.method, "result": e.result, "sandbox": e.provider == SANDBOX, "at": e.at}
        for e in events]
    changes = (db.query(BankAccountChangeRequest)
               .filter(BankAccountChangeRequest.company_id == row.company_id,
                       BankAccountChangeRequest.bank_account_id == row.id)
               .order_by(BankAccountChangeRequest.id).all())
    data["change_requests"] = [{"id": c.id, "status": c.status, "channel": c.channel, "kind": c.kind,
                                "created_at": c.created_at, "decided_at": c.decided_at} for c in changes]
    return data


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
def _sniff(data: bytes) -> Tuple[str, str]:
    for magic, ext, mime in EVIDENCE_TYPES:
        if data.startswith(magic):
            return ext, mime
    raise bad_request("Evidence must be a PDF, PNG or JPEG file.", "EVIDENCE_TYPE")


def add_evidence(db: Session, principal: Principal, account_id: int, file_name: str, data: bytes, *,
                 own: bool = False) -> BankVerification:
    """Upload a cancelled cheque or bank letter for DOCUMENT_REVIEW (private storage, SHA-256 kept)."""
    row = _load_account(db, principal, account_id, (EDIT,), own=own, lock=True)
    if row.status not in (_BA.PENDING_APPROVAL, _BA.ACTIVE) or row.verification_status not in (_V.PENDING,
                                                                                              _V.REQUIRES_REVIEW):
        raise conflict("Evidence can be added only while the account awaits verification.", "VERIFICATION_NOT_OPEN")
    if "DOCUMENT_REVIEW" not in _methods(db, row.company_id, row.employee_id, clock.company_today(db, row.company_id)):
        raise conflict("Document review is not a verification method of this company.", "METHOD_NOT_ENABLED")
    if not data:
        raise bad_request("No file provided.", "EVIDENCE_EMPTY")
    if len(data) > app_settings.max_upload_size:
        raise bad_request(f"The file is too large (max {app_settings.max_upload_size // (1024 * 1024)} MB).",
                          "EVIDENCE_TOO_LARGE")
    count = db.query(func.count(BankVerification.id)).filter(
        BankVerification.company_id == row.company_id, BankVerification.bank_account_id == row.id,
        BankVerification.result == "EVIDENCE_SUBMITTED").scalar()
    if count >= MAX_EVIDENCE:
        raise conflict(f"An account has at most {MAX_EVIDENCE} evidence files.", "TOO_MANY_EVIDENCE_FILES")
    ext, mime = _sniff(data)
    digest = hashlib.sha256(data).hexdigest()
    path = storage.save_bytes(row.company_id, EVIDENCE_CATEGORY, data, ext)
    event = _record(db, row, "DOCUMENT_REVIEW", "EVIDENCE_SUBMITTED", principal, evidence_storage_path=path,
                    evidence_sha256=digest, evidence_mime=mime, evidence_bytes=len(data),
                    evidence_file_name=Path(file_name or f"evidence.{ext}").name[:200])
    audit(db, principal, AUDIT_MODULE, "BANK_EVIDENCE_UPLOADED", f"Evidence added to bank account #{row.id}", ENTITY,
          row.id, row.company_id, **_audit_details(row, verification_id=event.id, sha256=digest, bytes=len(data),
                                                    self_service=own))
    return event


def evidence_file(db: Session, principal: Principal, account_id: int, verification_id: int
                  ) -> Tuple[Path, BankVerification]:
    """Evidence shows the full number, so only approvers and read_full holders open it (audited)."""
    row = _load_account(db, principal, account_id, (APPROVE, READ_FULL))
    event = db.query(BankVerification).filter(
        BankVerification.company_id == row.company_id, BankVerification.bank_account_id == row.id,
        BankVerification.id == verification_id, BankVerification.result == "EVIDENCE_SUBMITTED").first()
    if event is None:
        raise not_found("Evidence")
    audit(db, principal, AUDIT_MODULE, "BANK_EVIDENCE_DOWNLOADED", f"Evidence of bank account #{row.id} opened",
          ENTITY, row.id, row.company_id, **_audit_details(row, verification_id=event.id))
    return storage.resolve(row.company_id, event.evidence_storage_path), event


class SandboxVerificationAdapter:
    """SANDBOX ONLY: simulates a penny drop or name match without moving money or contacting anyone.

    Deterministic for tests and demos: an account whose last four characters are 0000 behaves like a
    closed account (FAILED); otherwise the "bank" returns the holder name as entered and the name match
    against the payee's legal name decides (below the threshold -> REQUIRES_REVIEW, EC-BNK-08).
    Refused in production, where no real provider is configured.
    """

    name = SANDBOX
    label = SANDBOX_LABEL

    def check(self, method: str, *, last4: str, holder_name: str, payee_name: str, fingerprint: str) -> dict:
        if app_settings.environment == "production":
            raise conflict("No verification provider is configured; use document review.", "PROVIDER_NOT_CONFIGURED")
        ref = "SBX-" + hashlib.sha256(f"{method}|{fingerprint}|{secrets.token_hex(8)}".encode()).hexdigest()[:20]
        if last4.endswith("0000"):
            return {"result": "FAILED", "provider_ref": ref, "reason_code": "ACCOUNT_CLOSED",
                    "message": "The sandbox bank reports the account as closed", "score": None}
        score = name_match_score(holder_name, payee_name)
        return {"result": "PASSED" if score >= NAME_MATCH_THRESHOLD else "FLAGGED", "provider_ref": ref,
                "reason_code": None if score >= NAME_MATCH_THRESHOLD else "NAME_MISMATCH",
                "message": "Simulated provider response", "score": score}


SANDBOX_ADAPTER = SandboxVerificationAdapter()


def verify_account(db: Session, principal: Principal, account_id: int, method: str, *, decision: Optional[str] = None,
                   evidence_id: Optional[int] = None, reason: Optional[str] = None,
                   expected_row_version: Optional[int] = None) -> Tuple[BankAccount, BankVerification]:
    """Record a verification of `method` (configured in payroll.bank.verification_methods; never FORMAT)."""
    method = str(method).upper()
    if method not in controls.OWNERSHIP_METHODS:
        raise bad_request("Choose DOCUMENT_REVIEW, PENNY_DROP or PROVIDER_NAME_MATCH; the format check runs on entry "
                          "and never verifies an account.", "METHOD_NOT_ALLOWED")
    needed = (APPROVE,) if method == "DOCUMENT_REVIEW" else (EDIT, APPROVE)
    row = _load_account(db, principal, account_id, needed, lock=True)
    if row.employee_id is not None and principal.employee_id == row.employee_id:
        raise forbidden("Verification of your own bank account is done by payroll staff.")
    _check_version(row, expected_row_version)
    if row.status not in (_BA.PENDING_APPROVAL, _BA.ACTIVE) or row.verification_status not in (_V.PENDING,
                                                                                              _V.REQUIRES_REVIEW):
        raise conflict("This account is not awaiting verification.", "VERIFICATION_NOT_OPEN")
    today = clock.company_today(db, row.company_id)
    if method not in _methods(db, row.company_id, row.employee_id, today):
        raise conflict(f"{method} is not a verification method of this company.", "METHOD_NOT_ENABLED")
    reason = _reason(reason, required=False)
    if method == "DOCUMENT_REVIEW":
        return row, _document_review(db, principal, row, decision, evidence_id, reason)
    employee = db.query(Employee).filter(Employee.company_id == row.company_id, Employee.id == row.employee_id).first()
    outcome = SANDBOX_ADAPTER.check(method, last4=row.last4, holder_name=row.holder_name,
                                    payee_name=_employee_name(employee), fingerprint=row.fingerprint)
    score = outcome["score"]
    event = _record(db, row, method, outcome["result"], principal, provider=SANDBOX,
                    provider_ref=outcome["provider_ref"], name_match_score=score, reason=reason,
                    details={"sandbox": True, "label": SANDBOX_LABEL, "reason_code": outcome["reason_code"],
                             "message": outcome["message"]})
    audit(db, principal, AUDIT_MODULE, "BANK_VERIFICATION_RECORDED",
          f"{method} ({SANDBOX}) on bank account #{row.id}: {outcome['result']}", ENTITY, row.id, row.company_id,
          **_audit_details(row, method=method, result=outcome["result"], provider=SANDBOX,
                           provider_ref=outcome["provider_ref"], sandbox=True))
    if outcome["result"] == "FAILED":
        if row.verification_status == _V.PENDING:
            _verification_transition(db, principal, row, "RECORD_FAILED", system=True, method=method,
                                     reason=outcome["reason_code"])
    elif outcome["result"] == "FLAGGED":
        if _add_flag(row, HOLDER_NAME_MISMATCH, score=decimal_str(score), source=method,
                     detail="The provider's account holder name does not match the payee"):
            row.row_version += 1
            db.flush()
        _sync_review(db, row)
    else:
        _settle(db, principal, row)
    return row, event


def _document_review(db: Session, principal: Principal, row: BankAccount, decision: Optional[str],
                     evidence_id: Optional[int], reason: Optional[str]) -> BankVerification:
    decision = str(decision or "").upper()
    if decision not in ("PASS", "FAIL"):
        raise bad_request("A document review decides PASS or FAIL.", "INVALID_DECISION")
    _ensure_not_maker(db, principal, row)
    evidence = None
    if evidence_id is not None:
        evidence = db.query(BankVerification).filter(
            BankVerification.company_id == row.company_id, BankVerification.bank_account_id == row.id,
            BankVerification.id == evidence_id, BankVerification.result == "EVIDENCE_SUBMITTED").first()
        if evidence is None:
            raise not_found("Evidence")
    if decision == "PASS" and evidence is None:
        raise bad_request("A document review passes only on uploaded evidence.", "EVIDENCE_REQUIRED")
    if decision == "FAIL" and not reason:
        raise bad_request("Give a reason.", "REASON_REQUIRED")
    event = _record(db, row, "DOCUMENT_REVIEW", "PASSED" if decision == "PASS" else "FAILED", principal,
                    evidence_of_id=evidence.id if evidence is not None else None, reason=reason)
    audit(db, principal, AUDIT_MODULE, "BANK_VERIFICATION_RECORDED",
          f"DOCUMENT_REVIEW on bank account #{row.id}: {decision}", ENTITY, row.id, row.company_id,
          **_audit_details(row, method="DOCUMENT_REVIEW", result=event.result, evidence_id=event.evidence_of_id))
    if decision == "FAIL":
        _verification_transition(db, principal, row, "MARK_FAILED", reason=reason, method="DOCUMENT_REVIEW")
    elif row.verification_status == _V.PENDING and _required_passed(db, row):
        _verification_transition(db, principal, row, "MARK_VERIFIED", method="DOCUMENT_REVIEW")
    return event


REVIEW_ACTIONS = ("CLEAR_REVIEW", "MARK_VERIFIED", "MARK_FAILED", "RESUBMIT")


def review_account(db: Session, principal: Principal, account_id: int, action: str, reason: Optional[str], *,
                   expected_row_version: Optional[int] = None) -> BankAccount:
    """Manual review of risk flags and verification outcomes (BANK_VERIFICATION user actions). CLEAR_REVIEW and
    MARK_VERIFIED on an account under a payment hold lift that hold, so they need a fresh step-up (403
    STEP_UP_REQUIRED), like waiving the cooling-off."""
    action = str(action).upper()
    if action not in REVIEW_ACTIONS:
        raise bad_request(f"Action must be one of {', '.join(REVIEW_ACTIONS)}.", "INVALID_ACTION")
    row = _load_account(db, principal, account_id, (EDIT,) if action == "RESUBMIT" else (APPROVE,), lock=True)
    if row.employee_id is not None and principal.employee_id == row.employee_id:
        # A payee never reopens or decides their own verification: reopening a FAILED account (e.g. after a
        # returned payment) would lift its payment hold. The payee submits a new account instead (EC-BNK-10).
        raise forbidden("Verification of your own bank account is done by payroll staff.")
    reason = _reason(reason, required=action != "RESUBMIT")
    if action in ("CLEAR_REVIEW", "MARK_VERIFIED", "MARK_FAILED"):
        _ensure_not_maker(db, principal, row)
    if action in ("CLEAR_REVIEW", "MARK_VERIFIED") and row.payment_hold:
        # Lifting a payment hold (a duplicate-account or fraud flag) lets money flow to the account again: a
        # payment-diversion control like waiving the cooling-off, so it needs a fresh step-up too.
        step_up.ensure_step_up(db, principal)
    _verification_transition(db, principal, row, action, reason=reason, expected_row_version=expected_row_version,
                             method=_last_ownership_method(db, row) if action == "MARK_VERIFIED" else None)
    result = {"CLEAR_REVIEW": "CLEARED", "MARK_VERIFIED": "PASSED", "MARK_FAILED": "FAILED",
              "RESUBMIT": "RESUBMITTED"}[action]
    _record(db, row, "MANUAL_REVIEW", result, principal, reason=reason, details={"action": action})
    if action in ("CLEAR_REVIEW", "MARK_VERIFIED"):
        closed = _close_flags(row, principal, reason or action)
        released = row.payment_hold
        row.payment_hold, row.hold_reason = False, None
        row.row_version += 1
        db.flush()
        audit(db, principal, AUDIT_MODULE, "BANK_REVIEW_CLEARED", f"Risk review of bank account #{row.id} closed",
              ENTITY, row.id, row.company_id, **_audit_details(row, closed_flags=closed, hold_released=released,
                                                                reason=reason))
        if action == "CLEAR_REVIEW":
            _settle(db, principal, row)
    return row


# ---------------------------------------------------------------------------
# Reveal (read_full + reason + step-up; audited; limited)
# ---------------------------------------------------------------------------
def _reveal_guard(db: Session, principal: Principal, reason: Optional[str]) -> str:
    if not principal.has(READ_FULL):
        raise forbidden()
    cleaned = " ".join((reason or "").split())
    if len(cleaned) < REVEAL_REASON_MIN:
        raise bad_request(f"Say why you need the full number (at least {REVEAL_REASON_MIN} characters).",
                          "REASON_REQUIRED")
    step_up.ensure_step_up(db, principal)
    since = _now() - timedelta(hours=1)
    count = db.query(func.count(AuditLog.id)).filter(
        AuditLog.company_id == principal.company_id, AuditLog.actor_id == principal.user_id,
        AuditLog.action == "BANK_DETAILS_REVEALED", AuditLog.created_at >= since).scalar()
    if count >= REVEAL_LIMIT_PER_HOUR:
        raise AppError(429, "Too many reveals in the last hour. Try again later.", "RATE_LIMITED",
                       headers={"Retry-After": "3600"})
    return masking.scrub_text(cleaned)[:500]


def reveal_account(db: Session, principal: Principal, account_id: int, reason: str,
                   ip_address: Optional[str] = None) -> dict:
    reason = _reveal_guard(db, principal, reason)
    row = _load_account(db, principal, account_id, (READ_FULL,))
    plain = unseal(db, row.company_id, "bank_accounts", row.id, _owner(row), row.account_number_enc, row.iban_enc)
    AuditLogService.log(db, principal, AUDIT_MODULE, "BANK_DETAILS_REVEALED",
                        f"Full bank details of account #{row.id} revealed", ENTITY, row.id, ip_address,
                        company_id=principal.company_id,
                        details=_audit_details(row, reason=reason, fields=[k for k, v in plain.items() if v]))
    return {"id": row.id, "holder_name": row.holder_name, "account_number": plain["account_number"],
            "iban": plain["iban"], "routing": dict(row.routing or {}), "account_masked": masked_number(row),
            "revealed_at": _now()}


# ---------------------------------------------------------------------------
# Hooks and read API for payments and the snapshot builder
# ---------------------------------------------------------------------------
def _tenant(db: Session) -> int:
    company_id = current_company_id(db)
    if company_id is None:
        raise TenantContextError("bank account hooks run in a tenant-bound session")
    return company_id


def mark_payment_failed(db: Session, account_id: int, reason: str) -> dict:
    """A payment to the account failed or came back (payments hook): verification FAILED, employee asked to
    update the account (EC-BNK-10, EC-PAY-04). Idempotent for an account already FAILED."""
    company_id = _tenant(db)
    row = _lock_account(db, company_id, account_id)
    reason = masking.scrub_text(" ".join((reason or "Payment failed").split()))[:200]
    now = _now()
    _record(db, row, "PAYMENT_RETURN", "FAILED", None, reason=reason)
    row.last_failed_at, row.failure_reason = now, reason
    row.row_version += 1
    db.flush()
    before = row.verification_status
    if before in (_V.PENDING, _V.VERIFIED):
        _verification_transition(db, None, row, "RECORD_PAYMENT_FAILURE", system=True, reason=reason)
    else:
        if _add_flag(row, PAYMENT_RETURNED, detail=reason):
            row.row_version += 1
            db.flush()
        audit(db, None, AUDIT_MODULE, "BANK_FAILED", f"Payment to bank account #{row.id} failed", ENTITY, row.id,
              company_id, **_audit_details(row, reason=reason, verification_status=before))
    _notify(db, row, "PAYMENT_FAILED")
    return {"bank_account_id": row.id, "verification_status": row.verification_status, "previous": before}


def mark_payment_settled(db: Session, account_id: int) -> None:
    """First settled payment to the account (payments hook): later snapshots carry first_payment = false."""
    company_id = _tenant(db)
    row = _lock_account(db, company_id, account_id)
    if row.first_paid_at is None:
        row.first_paid_at = _now()
        row.row_version += 1
        db.flush()


def _instant(db: Session, company_id: int, on) -> datetime:
    """A datetime as is; a date as its first instant in the company's timezone (conservative for cooling-off)."""
    if isinstance(on, datetime):
        return on if on.tzinfo is not None else on.replace(tzinfo=timezone.utc)
    company = db.get(Company, company_id)
    zone = pytz.timezone((company.timezone if company else None) or "UTC")
    return zone.localize(datetime.combine(on, time.min)).astimezone(timezone.utc)


def _verified_equivalent(row: BankAccount) -> bool:
    if row.verification_status == _V.VERIFIED:
        return True
    # The older account of a duplicate pair stays payable when it was verified before (EC-BNK-07).
    flags = [f for f in (row.risk_flags or []) if f.get("open")]
    return (row.verification_status == _V.REQUIRES_REVIEW and row.verified_at is not None and bool(flags)
            and all(f.get("code") == DUPLICATE_ACCOUNT and f.get("role") == "OLDER" for f in flags))


def _hold_reasons(row: BankAccount, instant: datetime, exclude_unverified: bool) -> List[str]:
    reasons = []
    if row.usable_from is not None and row.usable_from > instant:
        reasons.append("COOLING_OFF")
    if row.payment_hold:
        reasons.append(f"HELD_{row.hold_reason}")
    if row.verification_status in (_V.FAILED, _V.EXPIRED):
        reasons.append(f"VERIFICATION_{row.verification_status}")
    elif not _verified_equivalent(row) and exclude_unverified:
        reasons.append("UNVERIFIED")
    return reasons


def _serving(db: Session, company_id: int, payee_type: str, payee_id: int, instant: datetime) -> List[BankAccount]:
    """Accounts approved by `instant` and not ended at it, including those still in their cooling-off."""
    return (db.query(BankAccount)
            .filter(BankAccount.company_id == company_id, BankAccount.payee_type == payee_type,
                    BankAccount.payee_id == payee_id, BankAccount.approved_at.isnot(None),
                    BankAccount.approved_at <= instant,
                    BankAccount.status.in_((_BA.ACTIVE, _BA.INACTIVE)),
                    or_(BankAccount.ended_at.is_(None), BankAccount.ended_at > instant))
            .order_by(BankAccount.id).all())


def beneficiary_snapshot(row: BankAccount, *, hold_reasons: Sequence[str] = ()) -> dict:
    """What a payment instruction freezes (3.7 §3.4): masked display data plus the sealed number parts."""
    data = {
        "bank_account_id": row.id, "payee_type": row.payee_type, "payee_id": row.payee_id,
        "holder_name": row.holder_name, "country": row.country, "currency": row.currency,
        "account_masked": masked_number(row), "last4": row.last4, "routing": dict(row.routing or {}),
        "bank_name": row.bank_name, "branch_name": row.branch_name, "account_type": row.account_type,
        "allocation": _allocation_json(row), "verification_status": row.verification_status,
        "verified": _verified_equivalent(row), "first_payment": row.first_paid_at is None,
        "usable_from": row.usable_from.isoformat() if row.usable_from else None,
        "hold_reasons": list(hold_reasons), "payable": not hold_reasons, "fingerprint": row.fingerprint,
        "row_version": row.row_version,
        "encrypted": {"table": "bank_accounts", "row_id": row.id, "owner": _owner(row),
                      "account_number": row.account_number_enc, "iban": row.iban_enc},
        "snapshot_version": 1,
    }
    canonical = repr(sorted((k, repr(v)) for k, v in data.items() if k != "encrypted"))
    data["snapshot_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return data


def usable_accounts(db: Session, company_id: int, payee_id: int, on, *, payee_type: str = "EMPLOYEE") -> List[dict]:
    """Beneficiary snapshots of the payee's approved accounts at `on` (a date means its first instant).

    An account serves from ``usable_from`` (the end of its cooling-off) until ``ended_at``; the account it
    replaced keeps serving until then. An approved account still in its cooling-off is listed with the hold
    reason COOLING_OFF (never payable), so payments can say "usable from ..." instead of "no account". Each
    item carries ``payable`` and ``hold_reasons`` (COOLING_OFF, HELD_DUPLICATE_FINGERPRINT,
    VERIFICATION_FAILED/EXPIRED, UNVERIFIED when payroll.bank.exclude_unverified_from_payout): payments
    create ready instructions only for payable ones and hold the rest (EC-BNK-01/02/03/07).
    """
    instant = _instant(db, company_id, on)
    rows = _serving(db, company_id, payee_type, payee_id, instant)
    if not rows:
        return []
    day = on if not isinstance(on, datetime) else _local_date(db, company_id, instant)
    exclude = bool(_setting(db, company_id, "payroll.bank.exclude_unverified_from_payout",
                            payee_id if payee_type == "EMPLOYEE" else None, day))
    return [beneficiary_snapshot(r, hold_reasons=_hold_reasons(r, instant, exclude)) for r in rows]


def engine_bank_accounts(db: Session, company_id: int, employee_id: int, on) -> List[dict]:
    """The snapshot's ``bank_accounts`` entries (app.payroll.engine.snapshot.BankAccount shape): the accounts
    serving at `on` (not those still in their cooling-off), with their allocation rules."""
    out = []
    for item in usable_accounts(db, company_id, employee_id, on):
        if "COOLING_OFF" in item["hold_reasons"]:
            continue
        allocation = item["allocation"]
        out.append({"account_id": item["bank_account_id"], "type": allocation["type"], "value": allocation["value"],
                    "order": allocation["order"], "currency": item["currency"], "verified": item["verified"],
                    "first_payment": item["first_payment"]})
    return out


def precheck_findings(db: Session, company_id: int, employee_ids: Iterable[int], on, *,
                      require_verification: bool) -> List[dict]:
    """MISSING_BANK / UNVERIFIED_BANK warnings for a run's employees (3.5 §5.2; never block calculation)."""
    findings = []
    for employee_id in sorted(set(employee_ids)):
        listed = usable_accounts(db, company_id, employee_id, on)
        accounts = [a for a in listed if "COOLING_OFF" not in a["hold_reasons"]]
        if not accounts:
            cooling = [a for a in listed if "COOLING_OFF" in a["hold_reasons"]]
            message = "No active bank account on the pay date: the payment will be NOT_READY."
            if cooling:
                message = ("The only bank account is still in its cooling-off period on the pay date: the payment "
                           "will be NOT_READY until it ends.")
            findings.append({"employee_id": employee_id, "code": "MISSING_BANK", "message": message,
                             "details": {"cooling_off": [{"account": a["account_masked"],
                                                          "usable_from": a["usable_from"]} for a in cooling]}})
            continue
        if require_verification and not any(a["verified"] for a in accounts):
            findings.append({"employee_id": employee_id, "code": "UNVERIFIED_BANK",
                             "message": "No verified bank account on the pay date: the payment may be held.",
                             "details": {"accounts": [a["account_masked"] for a in accounts],
                                         "verification": sorted({a["verification_status"] for a in accounts})}})
    return findings


def beneficiary_plaintext(db: Session, company_id: int, snapshot: Mapping[str, Any]) -> Dict[str, Optional[str]]:
    """Decrypt the sealed parts of a beneficiary snapshot (bank-file generation only; never log the result)."""
    sealed = snapshot.get("encrypted") or {}
    if sealed.get("table") != "bank_accounts":
        raise ValueError("not a bank account snapshot")
    return unseal(db, company_id, "bank_accounts", int(sealed["row_id"]), str(sealed["owner"]),
                  sealed.get("account_number"), sealed.get("iban"))


def changed_since(db: Session, company_id: int, payee_id: int, since: datetime, *, payee_type: str = "EMPLOYEE"
                  ) -> bool:
    """Whether the payee's bank details changed after `since` (finance warning, EC-BNK-03)."""
    found = db.query(BankAccount.id).filter(
        BankAccount.company_id == company_id, BankAccount.payee_type == payee_type, BankAccount.payee_id == payee_id,
        or_(and_(BankAccount.approved_at.isnot(None), BankAccount.approved_at > since),
            and_(BankAccount.ended_at.isnot(None), BankAccount.ended_at > since,
                 BankAccount.replaced_by_account_id.is_(None)))).first()
    return found is not None

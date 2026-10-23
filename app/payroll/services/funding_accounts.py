"""Company funding accounts per legal entity (3.7 §6, 3.9 §3.8).

A funding account is the company account a payment batch is paid from: its bank details (sealed like
payee accounts, masked everywhere), currency, purpose (PAYROLL, STATUTORY, CONTRACTORS, REIMBURSEMENTS),
the users allowed to release batches from it (signatories) and the bank-file formats it supports (only
formats that are OFFERED, today GENERIC_CSV; see app.payroll.banking.files). ``provider_profile`` names
an adapter profile; only the clearly labelled SANDBOX profile exists, and no credentials are ever stored
here (provider secrets live in the server's secrets manager, contract §22.3).

Permissions: payroll:funding_accounts:manage at COMPANY or the account's LEGAL_ENTITY; changing bank
details also needs a fresh step-up. Revealing the full number needs payroll:bank_details:read_full for
the entity, a reason and a step-up, and is audited as BANK_DETAILS_REVEALED. Changing the signatories or the
provider profile (who may release, where payouts go) also needs a step-up. Accounts are never deleted:
they are deactivated. Concurrent edits use row_version (409 on conflict).

``list_options`` is a masked, read-only list for holders of any payment permission (or manage).

Read API for payments: ``funding_snapshot`` (masked display data plus the sealed parts),
``require_signatory`` and ``funding_plaintext`` (bank files only).
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.org import LegalEntity
from app.models.payments import FUNDING_PURPOSES, FundingAccount
from app.models.user import User
from app.payroll import audit as payroll_audit
from app.payroll.banking import files as bank_files
from app.payroll.banking import masking, validators
from app.payroll.banking.errors import BankFileError
from app.payroll.services import access, step_up
from app.payroll.services.bank_accounts import (
    READ_FULL,
    BankDetails,
    _check_version,
    _clean,
    _next_id,
    _reveal_guard,
    check_details,
    seal,
    unseal,
)
from app.payroll.services.wage_inputs import audit
from app.services.audit_log_service import AuditLogService

MANAGE = "payroll:funding_accounts:manage"
ENTITY = "funding_account"
AUDIT_MODULE = payroll_audit.PAYMENTS
TABLE = "funding_accounts"
MAX_SIGNATORIES = 20
PROVIDER_PROFILES = ("SANDBOX",)
# Payment users who choose or recognise funding accounts without managing them (masked list only).
OPTION_PERMISSIONS = ("payroll:payment:prepare", "payroll:payment:approve", "payroll:payment:submit",
                      "payroll:payment:reconcile", MANAGE)


@dataclass(frozen=True)
class FundingData:
    name: Optional[str] = None
    purpose: Optional[str] = None
    signatory_user_ids: Optional[Sequence[int]] = None
    formats: Optional[Sequence[str]] = None
    provider_profile: Optional[str] = None
    details: Optional[BankDetails] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _owner(legal_entity_id: int) -> str:
    return f"entity={legal_entity_id}"


def masked_number(row: FundingAccount) -> str:
    if row.iban_enc is not None:
        return f"{validators.iban_country(row.country) or ''}{masking.MASK}{row.last4}"
    return f"{masking.MASK}{row.last4}"


def serialize(row: FundingAccount) -> dict:
    return {
        "id": row.id, "legal_entity_id": row.legal_entity_id, "name": row.name, "holder_name": row.holder_name,
        "country": row.country, "currency": row.currency, "account_masked": masked_number(row), "last4": row.last4,
        "has_iban": row.iban_enc is not None, "routing": dict(row.routing or {}), "bank_name": row.bank_name,
        "account_type": row.account_type, "purpose": row.purpose,
        "signatory_user_ids": sorted(row.signatory_user_ids or []), "formats": list(row.formats or []),
        "provider_profile": row.provider_profile, "sandbox": row.provider_profile == "SANDBOX",
        "status": row.status, "status_reason": row.status_reason, "row_version": row.row_version,
        "created_at": row.created_at, "updated_at": row.updated_at,
    }


def _entity(db: Session, principal: Principal, legal_entity_id: int) -> LegalEntity:
    access.ensure_in_scope(principal, MANAGE, legal_entity_id, None, "Legal entity")
    entity = db.query(LegalEntity).filter(LegalEntity.company_id == principal.company_id,
                                          LegalEntity.id == legal_entity_id).first()
    if entity is None:
        raise not_found("Legal entity")
    return entity


def _load(db: Session, principal: Principal, account_id: int, permission: str = MANAGE, *,
          lock: bool = False) -> FundingAccount:
    if not principal.has(permission):
        raise forbidden()
    query = db.query(FundingAccount).filter(
        FundingAccount.company_id == principal.company_id, FundingAccount.id == account_id,
        access.run_scope_clause(principal, permission, FundingAccount.legal_entity_id))
    if lock:
        query = query.with_for_update().populate_existing()
    row = query.first()
    if row is None:
        raise not_found("Funding account")
    return row


def _purpose(value: Optional[str]) -> str:
    purpose = str(value or "PAYROLL").strip().upper()
    if purpose not in FUNDING_PURPOSES:
        raise bad_request(f"Purpose must be one of {', '.join(FUNDING_PURPOSES)}.", "PURPOSE_INVALID")
    return purpose


def _signatories(db: Session, company_id: int, user_ids: Optional[Sequence[int]]) -> List[int]:
    ids = sorted({int(u) for u in (user_ids or [])})
    if not ids:
        raise bad_request("Name at least one signatory who may release payments from this account.",
                          "SIGNATORIES_REQUIRED")
    if len(ids) > MAX_SIGNATORIES:
        raise bad_request(f"At most {MAX_SIGNATORIES} signatories.", "TOO_MANY_SIGNATORIES")
    found = {u for (u,) in db.query(User.id).filter(User.company_id == company_id, User.id.in_(ids),
                                                    User.status == "active")}
    if found != set(ids):
        raise not_found("Signatory user")
    return ids


def _formats(values: Optional[Sequence[str]]) -> List[str]:
    codes = list(dict.fromkeys(str(v).strip().upper() for v in (values or ["GENERIC_CSV"])))
    if not codes:
        raise bad_request("Name at least one bank file format.", "FORMATS_REQUIRED")
    for code in codes:
        try:
            bank_files.require_offered_format(code)
        except BankFileError as exc:  # FORMAT_UNKNOWN or FORMAT_NOT_OFFERED (e.g. AE_WPS_SIF, a draft)
            raise bad_request(exc.message, exc.code)
    return codes


def _profile(value: Optional[str]) -> Optional[str]:
    if value is None or not str(value).strip():
        return None
    profile = str(value).strip().upper()
    if profile not in PROVIDER_PROFILES:
        raise bad_request("Only the SANDBOX provider profile exists; no live provider is configured.",
                          "PROVIDER_PROFILE_INVALID")
    return profile


def _apply_details(db: Session, row: FundingAccount, details: BankDetails) -> None:
    checked = check_details(details)
    sealed = seal(db, row.company_id, TABLE, row.id, _owner(row.legal_entity_id), checked)
    row.holder_name = checked.account.holder_name
    row.country = checked.account.country
    row.currency = checked.currency
    row.routing = dict(checked.account.routing)
    row.bank_address = checked.account.bank_address
    row.account_type = checked.account.account_type
    row.bank_name = _clean(details.bank_name, 140)
    for key, value in sealed.items():
        setattr(row, key, value)


def _flush_unique(db: Session) -> None:
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        raise conflict("This bank account is already an active funding account of the entity.",
                       "FUNDING_ACCOUNT_DUPLICATE")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def create(db: Session, principal: Principal, legal_entity_id: int, data: FundingData) -> FundingAccount:
    entity = _entity(db, principal, legal_entity_id)
    if data.details is None:
        raise bad_request("Bank details are required.", "DETAILS_REQUIRED")
    name = _clean(data.name, 100)
    if not name:
        raise bad_request("Name the account.", "NAME_REQUIRED")
    step_up.ensure_step_up(db, principal)
    row = FundingAccount(
        id=_next_id(db, TABLE), company_id=principal.company_id, legal_entity_id=entity.id, name=name,
        purpose=_purpose(data.purpose), signatory_user_ids=_signatories(db, principal.company_id,
                                                                         data.signatory_user_ids),
        formats=_formats(data.formats), provider_profile=_profile(data.provider_profile), status="ACTIVE",
        created_by_user_id=principal.user_id, row_version=1,
    )
    _apply_details(db, row, data.details)
    db.add(row)
    _flush_unique(db)
    audit(db, principal, AUDIT_MODULE, "FUNDING_ACCOUNT_CREATED", f"Funding account #{row.id} created for entity "
          f"#{entity.id}", ENTITY, row.id, principal.company_id, account=masked_number(row),
          legal_entity_id=entity.id, currency=row.currency, purpose=row.purpose, formats=row.formats,
          signatory_user_ids=row.signatory_user_ids, provider_profile=row.provider_profile)
    return row


def update(db: Session, principal: Principal, account_id: int, data: FundingData,
           expected_row_version: Optional[int]) -> FundingAccount:
    row = _load(db, principal, account_id, lock=True)
    if expected_row_version is None:
        raise bad_request("Send the row_version you edited (If-Match or row_version).", "ROW_VERSION_REQUIRED")
    _check_version(row, expected_row_version)
    if row.status != "ACTIVE":
        raise conflict("An inactive funding account cannot be changed.", "FUNDING_ACCOUNT_INACTIVE")
    before = serialize(row)
    changed: List[str] = []
    if data.signatory_user_ids is not None or data.provider_profile is not None:
        # Who may release this account's batches, and where its payouts go, are payment-diversion controls
        # (3.7 §8.4, §12): changed only after a fresh step-up, like the bank details.
        step_up.ensure_step_up(db, principal)
    if data.name is not None:
        name = _clean(data.name, 100)
        if not name:
            raise bad_request("Name the account.", "NAME_REQUIRED")
        row.name = name
        changed.append("name")
    if data.purpose is not None:
        row.purpose = _purpose(data.purpose)
        changed.append("purpose")
    if data.signatory_user_ids is not None:
        row.signatory_user_ids = _signatories(db, row.company_id, data.signatory_user_ids)
        changed.append("signatory_user_ids")
    if data.formats is not None:
        row.formats = _formats(data.formats)
        changed.append("formats")
    if data.provider_profile is not None:
        row.provider_profile = _profile(data.provider_profile)
        changed.append("provider_profile")
    if data.details is not None:
        step_up.ensure_step_up(db, principal)
        _apply_details(db, row, data.details)
        changed.append("bank_details")
    if not changed:
        raise bad_request("Nothing to change.", "NO_CHANGES")
    row.row_version += 1
    _flush_unique(db)
    after = serialize(row)
    keys = ("name", "purpose", "signatory_user_ids", "formats", "provider_profile", "account_masked", "currency",
            "country", "routing", "holder_name")
    audit(db, principal, AUDIT_MODULE, "FUNDING_ACCOUNT_UPDATED", f"Funding account #{row.id} updated", ENTITY, row.id,
          row.company_id, changed=changed, before={k: before[k] for k in keys}, after={k: after[k] for k in keys})
    return row


def deactivate(db: Session, principal: Principal, account_id: int, reason: str,
               expected_row_version: Optional[int] = None) -> FundingAccount:
    row = _load(db, principal, account_id, lock=True)
    _check_version(row, expected_row_version)
    if row.status != "ACTIVE":
        raise conflict("The funding account is already inactive.", "FUNDING_ACCOUNT_INACTIVE")
    cleaned = " ".join((reason or "").split())
    if len(cleaned) < 3:
        raise bad_request("Give a reason.", "REASON_REQUIRED")
    step_up.ensure_step_up(db, principal)
    row.status, row.status_reason = "INACTIVE", cleaned[:1000]
    row.row_version += 1
    db.flush()
    audit(db, principal, AUDIT_MODULE, "FUNDING_ACCOUNT_DEACTIVATED", f"Funding account #{row.id} deactivated",
          ENTITY, row.id, row.company_id, account=masked_number(row), reason=row.status_reason)
    return row


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def list_accounts(db: Session, principal: Principal, *, legal_entity_id: Optional[int] = None,
                  status: Optional[str] = None, purpose: Optional[str] = None) -> List[dict]:
    if not principal.has(MANAGE):
        raise forbidden()
    if legal_entity_id is not None:
        _entity(db, principal, legal_entity_id)
    query = db.query(FundingAccount).filter(
        FundingAccount.company_id == principal.company_id,
        access.run_scope_clause(principal, MANAGE, FundingAccount.legal_entity_id))
    if legal_entity_id is not None:
        query = query.filter(FundingAccount.legal_entity_id == legal_entity_id)
    if status:
        query = query.filter(FundingAccount.status == status)
    if purpose:
        query = query.filter(FundingAccount.purpose == purpose)
    return [serialize(r) for r in query.order_by(FundingAccount.legal_entity_id, FundingAccount.id).all()]


def option_json(row: FundingAccount, entity: Optional[LegalEntity], user_id: Optional[int]) -> dict:
    """What payment users need to pick and recognise an account: masked, no signatories, routing or profile."""
    return {
        "id": row.id, "name": row.name, "currency": row.currency, "purpose": row.purpose,
        "legal_entity": {"id": row.legal_entity_id, "name": entity.legal_name if entity is not None else None,
                         "code": entity.code if entity is not None else None},
        "account_masked": masked_number(row), "bank_name": row.bank_name, "status": row.status,
        "active": row.status == "ACTIVE", "sandbox": row.provider_profile == "SANDBOX",
        "is_signatory": user_id is not None and user_id in (row.signatory_user_ids or []),
    }


def list_options(db: Session, principal: Principal, *, legal_entity_id: Optional[int] = None,
                 currency: Optional[str] = None, include_inactive: bool = False) -> List[dict]:
    """A masked, read-only list for payment users (OPTION_PERMISSIONS): the accounts of every legal entity in
    the scope of any payment permission the caller holds (403 with none; an entity filter outside that
    scope simply returns nothing, so no entity's existence is revealed)."""
    held = [p for p in OPTION_PERMISSIONS if principal.has(p)]
    if not held:
        raise forbidden()
    clauses = [access.run_scope_clause(principal, p, FundingAccount.legal_entity_id) for p in held]
    query = db.query(FundingAccount).filter(FundingAccount.company_id == principal.company_id, or_(*clauses))
    if legal_entity_id is not None:
        query = query.filter(FundingAccount.legal_entity_id == legal_entity_id)
    if currency:
        query = query.filter(FundingAccount.currency == currency)
    if not include_inactive:
        query = query.filter(FundingAccount.status == "ACTIVE")
    rows = query.order_by(FundingAccount.legal_entity_id, FundingAccount.name, FundingAccount.id).all()
    entities = {e.id: e for e in db.query(LegalEntity).filter(
        LegalEntity.company_id == principal.company_id,
        LegalEntity.id.in_(sorted({r.legal_entity_id for r in rows}) or [0]))}
    return [option_json(r, entities.get(r.legal_entity_id), principal.user_id) for r in rows]


def get(db: Session, principal: Principal, account_id: int) -> dict:
    return serialize(_load(db, principal, account_id))


def reveal(db: Session, principal: Principal, account_id: int, reason: str,
           ip_address: Optional[str] = None) -> dict:
    """Full number of a funding account: read_full for its entity, reason, step-up; audited and limited."""
    reason = _reveal_guard(db, principal, reason)
    row = _load(db, principal, account_id, READ_FULL)
    plain = unseal(db, row.company_id, TABLE, row.id, _owner(row.legal_entity_id), row.account_number_enc,
                   row.iban_enc)
    AuditLogService.log(
        db, principal, payroll_audit.BANK_ACCOUNTS, "BANK_DETAILS_REVEALED",
        f"Full bank details of funding account #{row.id} revealed", ENTITY, row.id, ip_address,
        company_id=row.company_id,
        details={"account": masked_number(row), "legal_entity_id": row.legal_entity_id, "reason": reason,
                 "fields": [k for k, v in plain.items() if v]},
    )
    return {"id": row.id, "holder_name": row.holder_name, "account_number": plain["account_number"],
            "iban": plain["iban"], "routing": dict(row.routing or {}), "account_masked": masked_number(row)}


# ---------------------------------------------------------------------------
# Read API for payments
# ---------------------------------------------------------------------------
def _row(db: Session, company_id: int, account_id: int) -> FundingAccount:
    row = db.query(FundingAccount).filter(FundingAccount.company_id == company_id,
                                          FundingAccount.id == account_id).first()
    if row is None:
        raise not_found("Funding account")
    return row


def funding_snapshot(db: Session, company_id: int, account_id: int) -> Dict[str, Any]:
    """The debtor side of a batch: masked display data plus the sealed number parts (404 if unknown)."""
    row = _row(db, company_id, account_id)
    data = serialize(row)
    data["encrypted"] = {"table": TABLE, "row_id": row.id, "owner": _owner(row.legal_entity_id),
                         "account_number": row.account_number_enc, "iban": row.iban_enc}
    return data


def require_signatory(db: Session, company_id: int, account_id: int, user_id: int) -> FundingAccount:
    """The ACTIVE funding account when `user_id` is one of its signatories (403 FUNDING_SIGNATORY otherwise)."""
    row = _row(db, company_id, account_id)
    if row.status != "ACTIVE":
        raise conflict("The funding account is inactive.", "FUNDING_ACCOUNT_INACTIVE")
    if user_id not in (row.signatory_user_ids or []):
        raise AppError(403, "Only a signatory of the funding account can release this batch.", "FUNDING_SIGNATORY")
    return row


def funding_plaintext(db: Session, company_id: int, snapshot: Mapping[str, Any]) -> Dict[str, Optional[str]]:
    """Decrypt a funding snapshot's sealed parts (bank-file generation only; never log the result)."""
    sealed = snapshot.get("encrypted") or {}
    if sealed.get("table") != TABLE:
        raise ValueError("not a funding account snapshot")
    return unseal(db, company_id, TABLE, int(sealed["row_id"]), str(sealed["owner"]), sealed.get("account_number"),
                  sealed.get("iban"))

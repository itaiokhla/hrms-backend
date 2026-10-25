"""Employee tax profiles: declarations and proofs, versioned, effective-dated and reviewed by HR
(3.2 §6.1, §7.2-§7.3; EC-STA-09, EC-STA-21; table ``employee_tax_profiles`` and ``employee_tax_proofs``).

The framework is generic and country-pack driven (``app.payroll.packs.tax_profile``): the pack of the employee's
legal entity declares which fields it needs (``pack.tax_profile_schema()``); this service stores what the employee
declares, lets HR approve or reject it and hands the approved values, read-only, to the payroll snapshot. It
invents no tax rule, no field and no meaning: a country whose pack declares nothing (or whose pack may not be
used in this environment) offers nothing to declare, and the pages say "not available for your country yet".

* **Employee** (own record only, ``payroll:tax_profile:read`` / ``:edit`` at SELF): ``context`` shows the schema,
  the current approved version, a pending one and the history; ``submit`` creates the next version (a full
  replacement of the declarations; one pending version at a time, 409 PENDING_EXISTS; ``withdraw`` ends it);
  proofs are added to the pending version (``add_proof``) and are private files with their SHA-256.
  Submitting a tax identifier needs a step-up (identity); an identifier the employee does not retype is carried
  over from the last approved version. Identifiers are sealed with the tenant data key and shown masked only.
* **HR** (``payroll:tax_profile:review`` in legal-entity scope, out-of-scope ids are 404): ``list_staff``,
  ``get_staff``, ``approve`` (step-up, never the submitter or the employee's own profile, every field the pack
  wants proven must have a proof, the schema must be the one in force) and ``reject`` (note required).
* **Payroll**: ``frozen_profile`` is what the snapshot builder freezes as ``tax_profile``: the latest APPROVED
  version of the pack's code effective on the period end, non-identifier values only, nested as packs read them
  (``previous_employment.income`` -> ``{"previous_employment": {"income": ...}}``) plus ``_meta``. Read-only: the
  engine and packs never write a profile.
* Every sensitive action is audited under the module "Tax Profiles" with field codes, never values;
  identifiers never leave encrypted storage except masked.

Services take (db, principal, ...) and never commit.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.core import clock, storage
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.employee import Employee
from app.models.org import LegalEntity
from app.models.tax_profiles import NOTE_MAX, EmployeeTaxProfile, EmployeeTaxProof
from app.payroll import audit as payroll_audit
from app.payroll import packs
from app.payroll.banking import masking
from app.payroll.packs import tax_profile as schema_lib
from app.payroll.services import access, documents, holders, keys, self_service, step_up
from app.payroll.services import settings as settings_engine
from app.services.audit_log_service import AuditLogService

READ = "payroll:tax_profile:read"
EDIT = "payroll:tax_profile:edit"
REVIEW = "payroll:tax_profile:review"
MODULE = payroll_audit.TAX_PROFILES
SUBJECT = "employee_tax_profile"
PROOF_CATEGORY = "tax-proofs"
MAX_PROOFS_PER_FIELD = 5
MAX_PROOFS_PER_VERSION = 20
MAX_HISTORY = 20
PROOF_TYPES = (
    (b"%PDF-", "pdf", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
)
EARLIEST = date(2000, 1, 1)
MAX_AHEAD_DAYS = 366

SUBMITTED_ACTION = "TAX_PROFILE_SUBMITTED"
WITHDRAWN_ACTION = "TAX_PROFILE_WITHDRAWN"
APPROVED_ACTION = "TAX_PROFILE_APPROVED"
REJECTED_ACTION = "TAX_PROFILE_REJECTED"
PROOF_ADDED = "TAX_PROOF_ADDED"
PROOF_DOWNLOADED = "TAX_PROOF_DOWNLOADED"

NOT_AVAILABLE_MESSAGE = "Tax declarations are not available for your country yet."


def _audit(db: Session, principal: Optional[Principal], action: str, description: str, row: EmployeeTaxProfile,
           **details: Any) -> None:
    AuditLogService.log(db, principal, MODULE, action, description[:500], SUBJECT, row.id,
                        company_id=row.company_id, actor_type="user" if principal is not None else "system",
                        details={"employee_id": row.employee_id, "version_no": row.version_no, **details})


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _identifier_aad(employee_id: int, code: str) -> bytes:
    return f"tax_profile.identifier:{employee_id}:{code}".encode()


# --------------------------------------------------------------------------- pack and entity of an employee


def _entity_of(db: Session, company_id: int, employee_id: int, on: date) -> Optional[LegalEntity]:
    placement = settings_engine.primary_assignment(db, company_id, employee_id, on)
    if placement is None:
        return None
    return db.query(LegalEntity).filter(LegalEntity.company_id == company_id,
                                        LegalEntity.id == placement.legal_entity_id).one_or_none()


def declaration_pack(db: Session, company_id: int, employee_id: int, on: date):
    """``(legal entity, pack)`` whose schema applies to the employee on ``on``; the pack is None when the country
    asks for nothing (no placement, no pack, a pack without fields, or one that may not be used here)."""
    entity = _entity_of(db, company_id, employee_id, on)
    if entity is None:
        return None, None
    return entity, packs.tax_profile_pack(entity.country_code, on, environment=app_settings.environment)


def _pack_json(pack) -> dict:
    meta = pack.meta
    verdict = packs.usage(meta, environment=app_settings.environment, preview=True)
    return {"code": meta.code, "version": meta.version, "name": meta.name, "country": meta.country,
            "currency": meta.currency, "status": meta.status.value, "is_fixture": meta.is_fixture,
            "usage": verdict.mode, "label": verdict.message}


# --------------------------------------------------------------------------- serialization


def _proofs_of(db: Session, company_id: int, profile_ids: Sequence[int]) -> Dict[int, List[EmployeeTaxProof]]:
    result: Dict[int, List[EmployeeTaxProof]] = {}
    if not profile_ids:
        return result
    rows = (db.query(EmployeeTaxProof).filter(EmployeeTaxProof.company_id == company_id,
                                              EmployeeTaxProof.profile_id.in_(list(profile_ids)))
            .order_by(EmployeeTaxProof.id).all())
    for proof in rows:
        result.setdefault(proof.profile_id, []).append(proof)
    return result


def _proof_json(proof: EmployeeTaxProof) -> dict:
    return {"id": proof.id, "field_code": proof.field_code, "file_name": proof.file_name, "mime": proof.mime,
            "bytes": proof.bytes, "sha256": proof.sha256, "uploaded_at": _iso(proof.created_at)}


def _schema_of(pack_code: str, environment: Optional[str] = None) -> Tuple[schema_lib.TaxProfileField, ...]:
    env = environment or app_settings.environment
    found = [p for p in packs.all_packs(environment=env) if p.code == pack_code and p.tax_profile_schema()]
    return found[-1].tax_profile_schema() if found else ()


def _pack_currency(pack_code: str) -> Optional[str]:
    found = [p for p in packs.all_packs(environment=app_settings.environment) if p.code == pack_code]
    return found[-1].meta.currency if found else None


def version_json(row: EmployeeTaxProfile, proofs: Sequence[EmployeeTaxProof], *, current_hash: Optional[str],
                 schema: Sequence[schema_lib.TaxProfileField], staff: bool = False) -> dict:
    missing = []
    if row.status == "SUBMITTED":
        wanted = schema_lib.proof_required_codes(schema, row.declared_values or {})
        have = {p.field_code for p in proofs}
        missing = [code for code in wanted if code not in have]
    data = {
        "id": row.id, "version_no": row.version_no, "status": row.status, "country": row.country,
        "pack_code": row.pack_code, "effective_from": _iso(row.effective_from),
        "values": dict(row.declared_values or {}), "identifiers": dict(row.identifiers_masked or {}),
        "proofs": [_proof_json(p) for p in proofs], "proofs_missing": missing,
        "schema_current": current_hash is None or row.schema_hash == current_hash,
        "submitted_at": _iso(row.submitted_at), "reviewed_at": _iso(row.reviewed_at), "review_note": row.review_note,
        "withdrawn_at": _iso(row.withdrawn_at), "row_version": row.row_version,
    }
    if staff:
        data.update({"employee_id": row.employee_id, "legal_entity_id": row.legal_entity_id,
                     "submitted_by_user_id": row.submitted_by_user_id, "reviewed_by_user_id": row.reviewed_by_user_id})
    return data


def _history(db: Session, company_id: int, employee_id: int) -> List[EmployeeTaxProfile]:
    return (db.query(EmployeeTaxProfile)
            .filter(EmployeeTaxProfile.company_id == company_id, EmployeeTaxProfile.employee_id == employee_id)
            .order_by(EmployeeTaxProfile.version_no.desc()).limit(MAX_HISTORY).all())


def current_version(db: Session, company_id: int, employee_id: int, pack_code: str, on: date
                    ) -> Optional[EmployeeTaxProfile]:
    """The latest APPROVED version of the pack's code that is in force on ``on`` (effective date, then number)."""
    return (db.query(EmployeeTaxProfile)
            .filter(EmployeeTaxProfile.company_id == company_id, EmployeeTaxProfile.employee_id == employee_id,
                    EmployeeTaxProfile.pack_code == pack_code, EmployeeTaxProfile.status == "APPROVED",
                    EmployeeTaxProfile.effective_from <= on)
            .order_by(EmployeeTaxProfile.effective_from.desc(), EmployeeTaxProfile.version_no.desc()).first())


# --------------------------------------------------------------------------- employee


def _own_employee(db: Session, principal: Principal, permission: str) -> Employee:
    if not principal.has(permission):
        raise forbidden()
    return self_service.own_employee(db, principal)


def context(db: Session, principal: Principal) -> dict:
    """What /me/tax shows: the pack-defined form, the status of the versions and the reason when none applies."""
    employee = _own_employee(db, principal, READ)
    cid = principal.company_id
    today = clock.company_today(db, cid)
    entity, pack = declaration_pack(db, cid, employee.id, today)
    history = _history(db, cid, employee.id)
    proofs = _proofs_of(db, cid, [h.id for h in history])
    base: Dict[str, Any] = {
        "employee_id": employee.id, "as_of": today.isoformat(), "can_edit": principal.has(EDIT),
        "legal_entity": ({"id": entity.id, "name": entity.legal_name, "country_code": entity.country_code}
                         if entity is not None else None),
        "country": entity.country_code if entity is not None else None,
    }
    if pack is None:
        reason = "NO_LEGAL_ENTITY" if entity is None else "NO_PACK_FIELDS"
        schema: Tuple[schema_lib.TaxProfileField, ...] = ()
        base.update({"available": False, "reason": reason, "message": NOT_AVAILABLE_MESSAGE, "pack": None,
                     "currency": None, "schema": [], "schema_hash": None})
        current_hash = None
    else:
        schema = pack.tax_profile_schema()
        current_hash = schema_lib.schema_hash(schema)
        base.update({"available": True, "reason": None, "message": None, "pack": _pack_json(pack),
                     "currency": pack.meta.currency, "schema": schema_lib.schema_json(schema),
                     "schema_hash": current_hash})
    code = pack.code if pack is not None else None
    items = [version_json(h, proofs.get(h.id, []), current_hash=current_hash if h.pack_code == code else None,
                          schema=schema if h.pack_code == code else _schema_of(h.pack_code)) for h in history]
    pending = next((i for i in items if i["status"] == "SUBMITTED"), None)
    in_force = current_version(db, cid, employee.id, code, today) if code else None
    base.update({
        "current": next((i for i in items if in_force is not None and i["id"] == in_force.id), None),
        "pending": pending,
        "latest": items[0] if items else None,
        "versions": items,
    })
    return base


def _parse_effective(value: Optional[date], today: date) -> date:
    day = value or today
    if day < EARLIEST or day > today + timedelta(days=MAX_AHEAD_DAYS):
        raise bad_request("The effective date must be between 2000-01-01 and one year from today.",
                          "EFFECTIVE_DATE_INVALID")
    return day


def submit(db: Session, principal: Principal, values: Mapping[str, Any], effective_from: Optional[date] = None
           ) -> EmployeeTaxProfile:
    """Create the next version (SUBMITTED) from the employee's declarations."""
    employee = _own_employee(db, principal, EDIT)
    cid = principal.company_id
    db.query(Employee).filter(Employee.company_id == cid, Employee.id == employee.id).with_for_update().one()
    today = clock.company_today(db, cid)
    entity, pack = declaration_pack(db, cid, employee.id, today)
    if pack is None or entity is None:
        raise conflict(NOT_AVAILABLE_MESSAGE, "TAX_PROFILE_NOT_AVAILABLE")
    pending = db.query(EmployeeTaxProfile.id).filter(
        EmployeeTaxProfile.company_id == cid, EmployeeTaxProfile.employee_id == employee.id,
        EmployeeTaxProfile.status == "SUBMITTED").first()
    if pending is not None:
        raise conflict("A submission is already waiting for review. Withdraw it first to send a new one.",
                       "PENDING_EXISTS")
    schema = schema_lib.validate_schema(pack.tax_profile_schema())
    previous = (db.query(EmployeeTaxProfile).filter(
        EmployeeTaxProfile.company_id == cid, EmployeeTaxProfile.employee_id == employee.id,
        EmployeeTaxProfile.pack_code == pack.code, EmployeeTaxProfile.status == "APPROVED")
        .order_by(EmployeeTaxProfile.effective_from.desc(), EmployeeTaxProfile.version_no.desc()).first())
    kept = previous.identifiers_enc or {} if previous is not None else {}
    identifier_codes = [f.code for f in schema if f.type == "IDENTIFIER"]
    carried = [c for c in identifier_codes if c in kept and schema_lib.is_empty(values.get(c))]
    clean, issues = schema_lib.validate_values(schema, values, pack.meta.currency, carried=carried)
    if issues:
        raise _invalid(issues)
    typed = {f.code for f in schema if f.type == "IDENTIFIER"}
    new_identifiers = {c: clean.pop(c) for c in list(clean) if c in typed}
    if new_identifiers:
        step_up.ensure_step_up(db, principal)  # a tax identifier is identity data (EC-BNK-06 style)
    effective = _parse_effective(effective_from, today)
    enc: Dict[str, str] = {}
    masked: Dict[str, str] = {}
    for code in identifier_codes:
        if code in new_identifiers:
            enc[code] = keys.encrypt_field(db, cid, new_identifiers[code], aad=_identifier_aad(employee.id, code))
            masked[code] = masking.mask_value(new_identifiers[code])
        elif code in carried and previous is not None:
            enc[code] = (previous.identifiers_enc or {})[code]
            masked[code] = (previous.identifiers_masked or {}).get(code) or masking.MASK
    number = (db.query(func.max(EmployeeTaxProfile.version_no)).filter(
        EmployeeTaxProfile.company_id == cid, EmployeeTaxProfile.employee_id == employee.id).scalar() or 0) + 1
    row = EmployeeTaxProfile(
        company_id=cid, employee_id=employee.id, legal_entity_id=entity.id, country=entity.country_code,
        pack_code=pack.code, schema_hash=schema_lib.schema_hash(schema), version_no=number, effective_from=effective,
        status="SUBMITTED", declared_values=clean, identifiers_enc=enc, identifiers_masked=masked,
        submitted_by_user_id=principal.user_id, submitted_at=clock.utcnow(), row_version=1)
    db.add(row)
    db.flush()
    _audit(db, principal, SUBMITTED_ACTION, f"Tax declarations version {number} submitted", row,
           fields=sorted(list(clean) + list(new_identifiers)), identifiers_changed=sorted(new_identifiers),
           effective_from=effective.isoformat(), pack=pack.version)
    holders.notify_holders(db, cid, REVIEW, entity.id, "Tax declarations to review",
                           "An employee submitted tax declarations for review.", "/payroll/compliance?tab=tax",
                           exclude_user_id=principal.user_id)
    return row


def _invalid(issues: List[Dict[str, str]]) -> AppError:
    """422 whose detail lists ``{field, msg}`` per problem, like request validation errors."""
    return AppError(422, [{"field": i["field"], "msg": i["message"], "code": i["code"]} for i in issues],
                    "TAX_PROFILE_INVALID")


def _load_own(db: Session, principal: Principal, profile_id: int, permission: str, *, lock: bool = False
              ) -> EmployeeTaxProfile:
    employee = _own_employee(db, principal, permission)
    query = db.query(EmployeeTaxProfile).filter(
        EmployeeTaxProfile.company_id == principal.company_id, EmployeeTaxProfile.id == profile_id,
        EmployeeTaxProfile.employee_id == employee.id)
    if lock:
        query = query.with_for_update().populate_existing()
    row = query.one_or_none()
    if row is None:
        raise not_found("Tax declaration")
    return row


def _check_version(row: EmployeeTaxProfile, expected: Optional[int]) -> None:
    if expected is not None and row.row_version != expected:
        raise conflict("The declaration was changed by someone else. Reload it.", "ROW_VERSION_CONFLICT")


def get_mine(db: Session, principal: Principal, profile_id: int) -> dict:
    row = _load_own(db, principal, profile_id, READ)
    schema = _schema_of(row.pack_code)
    current = schema_lib.schema_hash(schema) if schema else None
    return version_json(row, _proofs_of(db, row.company_id, [row.id]).get(row.id, []), current_hash=current,
                        schema=schema)


def withdraw(db: Session, principal: Principal, profile_id: int, *, expected_version: Optional[int] = None
             ) -> EmployeeTaxProfile:
    row = _load_own(db, principal, profile_id, EDIT, lock=True)
    _check_version(row, expected_version)
    if row.status != "SUBMITTED":
        raise conflict(f"Only a submission waiting for review can be withdrawn; this one is {row.status}.",
                       "NOT_SUBMITTED")
    row.status, row.withdrawn_at = "WITHDRAWN", clock.utcnow()
    row.row_version += 1
    db.flush()
    _audit(db, principal, WITHDRAWN_ACTION, f"Tax declarations version {row.version_no} withdrawn", row)
    return row


def _sniff(data: bytes) -> Tuple[str, str]:
    for magic, ext, mime in PROOF_TYPES:
        if data.startswith(magic):
            return ext, mime
    raise bad_request("Proofs must be PDF, PNG or JPEG files.", "PROOF_TYPE")


def add_proof(db: Session, principal: Principal, profile_id: int, field_code: str, file_name: str, data: bytes
              ) -> EmployeeTaxProof:
    """Attach a proof to a field of the employee's pending version."""
    row = _load_own(db, principal, profile_id, EDIT, lock=True)
    if row.status != "SUBMITTED":
        raise conflict("Proofs can only be added while the submission waits for review.", "NOT_SUBMITTED")
    schema = {f.code: f for f in _schema_of(row.pack_code)}
    field = schema.get(field_code)
    if field is None or field.type == "IDENTIFIER":
        raise bad_request("This field does not take a proof.", "PROOF_FIELD_INVALID")
    if schema_lib.is_empty((row.declared_values or {}).get(field_code)):
        raise bad_request("Declare a value for this field before attaching a proof.", "PROOF_WITHOUT_VALUE")
    if not data:
        raise bad_request("No file provided.", "PROOF_EMPTY")
    if len(data) > app_settings.max_upload_size:
        raise bad_request(f"The file is too large (max {app_settings.max_upload_size // (1024 * 1024)} MB).",
                          "PROOF_TOO_LARGE")
    existing = db.query(EmployeeTaxProof).filter(EmployeeTaxProof.company_id == row.company_id,
                                                 EmployeeTaxProof.profile_id == row.id).all()
    if len(existing) >= MAX_PROOFS_PER_VERSION or sum(1 for p in existing if p.field_code == field_code) \
            >= MAX_PROOFS_PER_FIELD:
        raise conflict("Too many proofs are attached already.", "TOO_MANY_PROOFS")
    ext, mime = _sniff(data)
    digest = hashlib.sha256(data).hexdigest()
    if any(p.field_code == field_code and p.sha256 == digest for p in existing):
        raise conflict("This proof is already attached to the field.", "PROOF_ALREADY_ATTACHED")
    path = storage.save_bytes(row.company_id, PROOF_CATEGORY, data, ext)
    proof = EmployeeTaxProof(company_id=row.company_id, profile_id=row.id, employee_id=row.employee_id,
                             field_code=field_code, file_name=Path(file_name or f"proof.{ext}").name[:200],
                             storage_path=path, sha256=digest, mime=mime, bytes=len(data),
                             uploaded_by_user_id=principal.user_id)
    db.add(proof)
    db.flush()
    _audit(db, principal, PROOF_ADDED, f"Proof added to tax declarations version {row.version_no}", row,
           proof_id=proof.id, field=field_code, sha256=digest, bytes=len(data))
    return proof


def proof_file(db: Session, principal: Principal, profile_id: int, proof_id: int, *, own: bool = False
               ) -> Tuple[Path, EmployeeTaxProof]:
    row = _load_own(db, principal, profile_id, READ) if own else _load_staff(db, principal, profile_id)
    proof = db.query(EmployeeTaxProof).filter(EmployeeTaxProof.company_id == row.company_id,
                                              EmployeeTaxProof.profile_id == row.id,
                                              EmployeeTaxProof.id == proof_id).one_or_none()
    if proof is None:
        raise not_found("Proof")
    _audit(db, principal, PROOF_DOWNLOADED, f"Proof downloaded from tax declarations version {row.version_no}", row,
           proof_id=proof.id, field=proof.field_code, by="EMPLOYEE" if own else "STAFF")
    return storage.resolve(row.company_id, proof.storage_path), proof


# --------------------------------------------------------------------------- HR review


def _staff_clause(principal: Principal):
    return access.run_scope_clause(principal, REVIEW, EmployeeTaxProfile.legal_entity_id)


def _load_staff(db: Session, principal: Principal, profile_id: int, *, lock: bool = False) -> EmployeeTaxProfile:
    documents.require_staff(principal, REVIEW)
    query = db.query(EmployeeTaxProfile).filter(
        EmployeeTaxProfile.company_id == principal.company_id, EmployeeTaxProfile.id == profile_id,
        _staff_clause(principal))
    if lock:
        query = query.with_for_update().populate_existing()
    row = query.one_or_none()
    if row is None:
        raise not_found("Tax declaration")
    return row


def _staff_json(db: Session, principal: Principal, rows: Sequence[EmployeeTaxProfile]) -> List[dict]:
    proofs = _proofs_of(db, principal.company_id, [r.id for r in rows])
    names = documents.employee_names(db, principal.company_id, [r.employee_id for r in rows])
    schemas: Dict[str, Tuple[schema_lib.TaxProfileField, ...]] = {}
    result = []
    for row in rows:
        schema = schemas.setdefault(row.pack_code, _schema_of(row.pack_code))
        current = schema_lib.schema_hash(schema) if schema else None
        data = version_json(row, proofs.get(row.id, []), current_hash=current, schema=schema, staff=True)
        data["employee"] = names.get(row.employee_id)
        data["schema"] = schema_lib.schema_json(schema)
        data["currency"] = _pack_currency(row.pack_code)
        data["is_own"] = row.employee_id == principal.employee_id
        result.append(data)
    return result


def list_staff(db: Session, principal: Principal, *, status: Optional[str] = None, employee_id: Optional[int] = None,
               legal_entity_id: Optional[int] = None, skip: int = 0, limit: int = 50) -> dict:
    """Versions of employees in the caller's legal-entity scope, pending ones first then newest."""
    documents.require_staff(principal, REVIEW)
    query = db.query(EmployeeTaxProfile).filter(EmployeeTaxProfile.company_id == principal.company_id,
                                                _staff_clause(principal))
    for column, value in ((EmployeeTaxProfile.status, status), (EmployeeTaxProfile.employee_id, employee_id),
                          (EmployeeTaxProfile.legal_entity_id, legal_entity_id)):
        if value is not None:
            query = query.filter(column == value)
    total = query.order_by(None).with_entities(func.count(EmployeeTaxProfile.id)).scalar() or 0
    pending_first = (EmployeeTaxProfile.status != "SUBMITTED")
    rows = query.order_by(pending_first, EmployeeTaxProfile.id.desc()).offset(skip).limit(limit).all()
    counts = dict(db.query(EmployeeTaxProfile.status, func.count(EmployeeTaxProfile.id)).filter(
        EmployeeTaxProfile.company_id == principal.company_id, _staff_clause(principal))
        .group_by(EmployeeTaxProfile.status).all())
    return {"total": total, "counts": counts, "items": _staff_json(db, principal, rows)}


def get_staff(db: Session, principal: Principal, profile_id: int) -> dict:
    return _staff_json(db, principal, [_load_staff(db, principal, profile_id)])[0]


def _guard_review(principal: Principal, row: EmployeeTaxProfile) -> None:
    if row.submitted_by_user_id == principal.user_id or row.employee_id == principal.employee_id:
        raise AppError(403, "You cannot review your own tax declarations.", "SELF_REVIEW")


def _notify_employee(db: Session, row: EmployeeTaxProfile, title: str, message: str) -> None:
    user_id = db.query(Employee.user_id).filter(Employee.company_id == row.company_id,
                                                Employee.id == row.employee_id).scalar()
    holders.notify_users(db, [user_id], title, message, "/me/tax")


def approve(db: Session, principal: Principal, profile_id: int, note: Optional[str] = None, *,
            expected_version: Optional[int] = None) -> EmployeeTaxProfile:
    row = _load_staff(db, principal, profile_id, lock=True)
    _guard_review(principal, row)
    step_up.ensure_step_up(db, principal)
    _check_version(row, expected_version)
    if row.status != "SUBMITTED":
        raise conflict(f"Only a submission waiting for review can be approved; this one is {row.status}.",
                       "NOT_SUBMITTED")
    schema = _schema_of(row.pack_code)
    if not schema or row.schema_hash != schema_lib.schema_hash(schema):
        raise conflict("The country's declaration form changed after this was submitted. Ask the employee to "
                       "submit it again.", "SCHEMA_CHANGED")
    proofs = _proofs_of(db, row.company_id, [row.id]).get(row.id, [])
    have = {p.field_code for p in proofs}
    missing = [c for c in schema_lib.proof_required_codes(schema, row.declared_values or {}) if c not in have]
    if missing:
        labels = {f.code: f.label for f in schema}
        raise conflict("A proof is missing for: " + ", ".join(labels.get(c, c) for c in missing) + ".",
                       "PROOF_MISSING")
    text = (note or "").strip() or None
    if text is not None and len(text) > NOTE_MAX:
        raise bad_request(f"The note is too long ({NOTE_MAX} characters at most).", "NOTE_TOO_LONG")
    row.status, row.reviewed_by_user_id, row.reviewed_at = "APPROVED", principal.user_id, clock.utcnow()
    row.review_note = text
    row.row_version += 1
    db.flush()
    _audit(db, principal, APPROVED_ACTION, f"Tax declarations version {row.version_no} approved", row,
           fields=sorted(row.declared_values or {}), identifiers=sorted(row.identifiers_masked or {}),
           effective_from=row.effective_from.isoformat(), proofs=len(proofs))
    _notify_employee(db, row, "Tax declarations approved", "HR approved your tax declarations.")
    return row


def reject(db: Session, principal: Principal, profile_id: int, note: str, *,
           expected_version: Optional[int] = None) -> EmployeeTaxProfile:
    row = _load_staff(db, principal, profile_id, lock=True)
    _guard_review(principal, row)
    _check_version(row, expected_version)
    if row.status != "SUBMITTED":
        raise conflict(f"Only a submission waiting for review can be rejected; this one is {row.status}.",
                       "NOT_SUBMITTED")
    text = (note or "").strip()
    if not text:
        raise AppError(422, "Write the reason for rejecting the declarations.", "NOTE_REQUIRED")
    if len(text) > NOTE_MAX:
        raise bad_request(f"The note is too long ({NOTE_MAX} characters at most).", "NOTE_TOO_LONG")
    row.status, row.reviewed_by_user_id, row.reviewed_at = "REJECTED", principal.user_id, clock.utcnow()
    row.review_note = text
    row.row_version += 1
    db.flush()
    _audit(db, principal, REJECTED_ACTION, f"Tax declarations version {row.version_no} rejected", row,
           note_length=len(text))
    _notify_employee(db, row, "Tax declarations need changes", "HR could not approve your tax declarations. "
                     "Open My Tax to read the reason and submit them again.")
    return row


# --------------------------------------------------------------------------- payroll snapshot (read-only)


def frozen_profile(db: Session, company_id: int, employee_id: int, pack_ref: Optional[Mapping[str, Any]], on: date
                   ) -> Tuple[Optional[dict], Optional[int], List[Tuple[str, str, str, dict]]]:
    """``(tax_profile for the snapshot, version id, issues)`` for the pack the run froze and the date ``on``.

    None when the pack declares no fields or the employee has no approved version in force: the pack then
    behaves exactly as before (``profile`` None). Identifier values never enter the snapshot. A value whose
    field the pack no longer declares is left out, and the changed schema is reported as a WARNING issue.
    ``issues`` are ``(code, severity, message, details)``."""
    if not pack_ref:
        return None, None, []
    pack = packs.lookup(pack_ref.get("code", ""), pack_ref.get("version", ""))
    schema = pack.tax_profile_schema() if pack is not None else ()
    if not schema:
        return None, None, []
    row = current_version(db, company_id, employee_id, pack.code, on)
    if row is None:
        return None, None, []
    allowed = {f.code for f in schema if f.type != "IDENTIFIER"}
    values = {c: v for c, v in (row.declared_values or {}).items() if c in allowed}
    issues: List[Tuple[str, str, str, dict]] = []
    if row.schema_hash != schema_lib.schema_hash(schema):
        issues.append(("TAX_PROFILE_SCHEMA_CHANGED", "WARNING",
                       "The employee's approved tax declarations were made against an earlier declaration form of "
                       "the country pack; only the fields the pack still declares are used. Ask the employee to "
                       "submit them again.", {"version_no": row.version_no}))
    content = schema_lib.nest(values)
    content["_meta"] = {"version_id": row.id, "version_no": row.version_no, "effective_from": row.effective_from.isoformat(),
                        "pack": pack.version, "schema_hash": row.schema_hash}
    return content, row.id, issues


__all__ = [
    "EDIT", "NOT_AVAILABLE_MESSAGE", "READ", "REVIEW", "add_proof", "approve", "context", "current_version",
    "declaration_pack", "frozen_profile", "get_mine", "get_staff", "list_staff", "proof_file", "reject", "submit",
    "version_json", "withdraw",
]

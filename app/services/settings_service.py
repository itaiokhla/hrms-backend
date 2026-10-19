"""Per-company settings with a typed whitelist (no arbitrary keys)."""

import json
from typing import Any, Dict

from sqlalchemy.orm import Session

from app.core.errors import bad_request
from app.core.principal import Principal
from app.models.company import Company
from app.models.system_settings import CompanySetting
from app.services.audit_log_service import AuditLogService
from app.services.company_service import validate_timezone

# key: (type, default)
SETTINGS: Dict[str, tuple] = {
    "companyEmail": (str, ""),
    "companyPhone": (str, ""),
    "companyAddress": (str, ""),
    "checkIn": (str, "09:00"),
    "checkOut": (str, "18:00"),
    "graceTime": (int, 15),
    "requireFaceForSelfCheckIn": (bool, True),
    "autoAttendance": (bool, True),
    "voiceWelcome": (bool, False),
    "pf": (float, 12.0),
    "tax": (float, 10.0),
    "overtime": (float, 1.5),
    "payrollSeparationOfDuties": (bool, True),
    "theme": (str, "system"),
    "emailNotification": (bool, True),
}
# Only company administrators may relax these controls.
ADMIN_ONLY = {"payrollSeparationOfDuties", "requireFaceForSelfCheckIn"}
COMPANY_FIELDS = {"companyName": "name", "timezone": "timezone", "currency": "currency"}


def _coerce(key: str, value: Any):
    kind, _ = SETTINGS[key]
    try:
        if kind is bool:
            if isinstance(value, bool):
                return value
            if str(value).lower() in ("true", "1", "yes"):
                return True
            if str(value).lower() in ("false", "0", "no"):
                return False
            raise ValueError
        if kind is int:
            return int(value)
        if kind is float:
            return float(value)
        text = str(value)
        if len(text) > 500:
            raise ValueError
        return text
    except (TypeError, ValueError):
        raise bad_request(f"Invalid value for {key}")


def get_value(db: Session, company_id: int, key: str):
    row = db.query(CompanySetting).filter(CompanySetting.company_id == company_id, CompanySetting.key == key).first()
    if row is None or row.value is None:
        return SETTINGS[key][1]
    return _coerce(key, json.loads(row.value))


def get_all(db: Session, company_id: int) -> dict:
    stored = {
        r.key: r.value
        for r in db.query(CompanySetting).filter(CompanySetting.company_id == company_id).all()
        if r.key in SETTINGS
    }
    result = {}
    for key, (_, default) in SETTINGS.items():
        result[key] = _coerce(key, json.loads(stored[key])) if key in stored else default
    company = db.get(Company, company_id)
    result.update(
        {
            "companyName": company.name,
            "timezone": company.timezone,
            "currency": company.currency,
            "hasLogo": bool(get_logo_path(db, company_id)),
        }
    )
    return result


def update(db: Session, principal: Principal, data: Dict[str, Any]) -> dict:
    company = db.get(Company, principal.company_id)
    changed = []
    for key, value in data.items():
        if key in COMPANY_FIELDS:
            if key == "timezone":
                value = validate_timezone(value)
            elif key == "companyName":
                value = str(value).strip()
                if not 2 <= len(value) <= 150:
                    raise bad_request("Company name must be 2-150 characters.")
            elif key == "currency":
                value = str(value).upper()
                if len(value) != 3 or not value.isalpha():
                    raise bad_request("Currency must be a 3-letter code.")
            setattr(company, COMPANY_FIELDS[key], value)
            changed.append(key)
            continue
        if key not in SETTINGS:
            continue  # read-only or unknown keys are ignored, never stored
        if key in ADMIN_ONLY and not principal.is_company_admin:
            raise bad_request(f"Only company administrators can change {key}.")
        coerced = _coerce(key, value)
        row = db.query(CompanySetting).filter(
            CompanySetting.company_id == principal.company_id, CompanySetting.key == key
        ).first()
        if row is None:
            db.add(CompanySetting(company_id=principal.company_id, key=key, value=json.dumps(coerced)))
        else:
            row.value = json.dumps(coerced)
        changed.append(key)
    if changed:
        AuditLogService.log(db, principal, "Settings", "UPDATE", f"Updated settings: {', '.join(sorted(changed))}")
    return get_all(db, principal.company_id)


LOGO_KEY = "_logoPath"


def get_logo_path(db: Session, company_id: int):
    row = db.query(CompanySetting).filter(CompanySetting.company_id == company_id, CompanySetting.key == LOGO_KEY).first()
    return row.value if row else None


def set_logo_path(db: Session, company_id: int, path: str) -> None:
    row = db.query(CompanySetting).filter(CompanySetting.company_id == company_id, CompanySetting.key == LOGO_KEY).first()
    if row is None:
        db.add(CompanySetting(company_id=company_id, key=LOGO_KEY, value=path))
    else:
        row.value = path

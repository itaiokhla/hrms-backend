"""Per-company settings with a typed whitelist (no arbitrary keys).

Compatibility facade over the payroll settings engine v2 (3.1 §2.4): the legacy keys that have a
v2 setting (graceTime, lunchIsPaid, payrollSeparationOfDuties) are read from and written through
the v2 engine at TENANT level for the company's today, with its validation and change control;
writes are mirrored into company_settings so a code rollback still sees the latest value. Every
other key keeps the company_settings row and today's fallback to the default on invalid values.
"""

import json
import logging
from typing import Any, Dict, Iterable

from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.core.errors import bad_request
from app.core.principal import Principal
from app.models.company import Company
from app.models.system_settings import CompanySetting
from app.services.audit_log_service import AuditLogService
from app.services.company_service import validate_timezone

logger = logging.getLogger(__name__)

LOCATION_MODES = ("OFF", "OPTIONAL", "REQUIRED")

# key: (type, default[, constraints])  constraints: min / max / choices
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
    # Lunch and shift rules
    "allowMultipleLunches": (bool, True),
    "lunchIsPaid": (bool, False),
    "lunchOpenAtPunchOut": (str, "CONFIRM_AUTO_CLOSE", {"choices": ("CONFIRM_AUTO_CLOSE", "BLOCK")}),
    "requireFaceForLunch": (bool, False),
    "maxShiftHours": (int, 16, {"min": 4, "max": 24}),
    # Location at punch and lunch events (off until an administrator opts in)
    "locationPunchPolicy": (str, "OFF", {"choices": LOCATION_MODES}),
    "locationLunchPolicy": (str, "OFF", {"choices": LOCATION_MODES}),
    "locationExceptionPolicy": (str, "ALLOW_WITH_REASON", {"choices": ("ALLOW_WITH_REASON", "BLOCK")}),
    "locationRejectLowAccuracy": (bool, False),
    "gpsPreferredAccuracyM": (int, 50, {"min": 5, "max": 1000}),
    "gpsAcceptableAccuracyM": (int, 100, {"min": 10, "max": 5000}),
    "gpsTimeoutSeconds": (int, 20, {"min": 5, "max": 60}),
    "gpsMaxAttempts": (int, 3, {"min": 1, "max": 5}),
    "gpsMaxFixAgeSeconds": (int, 120, {"min": 30, "max": 600}),
    # Periodic (best-effort) location during an active shift
    "trackingEnabled": (bool, False),
    "trackingIntervalMinutes": (int, 120, {"min": "tracking_min", "max": 480}),
    "trackingGraceMinutes": (int, 15, {"min": 1, "max": 60}),
    "trackingPauseOnLunch": (bool, True),
    "movementThresholdM": (int, 1000, {"min": 50, "max": 50000}),
    "locationRetentionDays": (int, 90, {"min": 7, "max": 730}),
}
LOCATION_KEYS = {
    "locationPunchPolicy", "locationLunchPolicy", "locationExceptionPolicy", "locationRejectLowAccuracy",
    "gpsPreferredAccuracyM", "gpsAcceptableAccuracyM", "gpsTimeoutSeconds", "gpsMaxAttempts", "gpsMaxFixAgeSeconds",
    "trackingEnabled", "trackingIntervalMinutes", "trackingGraceMinutes", "trackingPauseOnLunch",
    "movementThresholdM", "locationRetentionDays",
}
ATTENDANCE_POLICY_KEYS = LOCATION_KEYS | {
    "requireFaceForSelfCheckIn", "requireFaceForLunch", "allowMultipleLunches", "lunchIsPaid", "lunchOpenAtPunchOut",
    "maxShiftHours", "graceTime",
}
# Only company administrators may relax these controls or change what location data is collected.
ADMIN_ONLY = {"payrollSeparationOfDuties", "requireFaceForSelfCheckIn"} | LOCATION_KEYS
COMPANY_FIELDS = {"companyName": "name", "timezone": "timezone", "currency": "currency"}


def _limit(value):
    return app_settings.location_tracking_min_interval_minutes if value == "tracking_min" else value


def _coerce(key: str, value: Any):
    kind, _default, *rest = SETTINGS[key]
    rules = rest[0] if rest else {}
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
            if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
                raise ValueError
            result = int(value)
        elif kind is float:
            result = float(value)
        else:
            result = str(value)
            if len(result) > 500:
                raise ValueError
    except (TypeError, ValueError):
        raise bad_request(f"Invalid value for {key}")
    if "choices" in rules and result not in rules["choices"]:
        raise bad_request(f"{key} must be one of: {', '.join(rules['choices'])}.")
    low, high = _limit(rules.get("min")), _limit(rules.get("max"))
    if (low is not None and result < low) or (high is not None and result > high):
        raise bad_request(f"{key} must be between {low} and {high}.")
    return result


def _read(key: str, raw: str):
    """Stored values are re-validated on read; a value that no longer fits falls back to the default."""
    try:
        return _coerce(key, json.loads(raw))
    except Exception:
        logger.warning("Ignoring invalid stored value for setting %s", key)
        return SETTINGS[key][1]


# Legacy keys whose value lives in the v2 engine (settings_registry aliases).
V2_KEYS = ("graceTime", "lunchIsPaid", "payrollSeparationOfDuties")


def _v2_values(db: Session, company_id: int, keys) -> Dict[str, Any]:
    from app.payroll.services import settings as engine

    company = db.get(Company, company_id)
    resolved = engine.resolve_many(db, company_id, keys, on=engine.company_today(company), strict=False)
    values = {}
    for key, result in resolved.items():
        if result.ok:
            values[key] = result.value
        else:
            logger.warning("Ignoring invalid stored value for setting %s", key)
            values[key] = SETTINGS[key][1]
    return values


def get_value(db: Session, company_id: int, key: str):
    if key in V2_KEYS:
        return _v2_values(db, company_id, [key])[key]
    row = db.query(CompanySetting).filter(CompanySetting.company_id == company_id, CompanySetting.key == key).first()
    if row is None or row.value is None:
        return SETTINGS[key][1]
    return _read(key, row.value)


def get_values(db: Session, company_id: int, keys: Iterable[str]) -> Dict[str, Any]:
    keys = list(keys)
    legacy = [k for k in keys if k not in V2_KEYS]
    stored = {
        r.key: r.value
        for r in db.query(CompanySetting).filter(CompanySetting.company_id == company_id, CompanySetting.key.in_(legacy))
        if r.value is not None
    } if legacy else {}
    v2 = _v2_values(db, company_id, [k for k in keys if k in V2_KEYS]) if len(legacy) < len(keys) else {}
    return {key: v2[key] if key in v2 else (_read(key, stored[key]) if key in stored else SETTINGS[key][1])
            for key in keys}


def _write_v2(db: Session, principal: Principal, key: str, value) -> bool:
    """Write through the v2 engine; returns whether the value is now in force (not pending approval)."""
    from app.payroll.services import settings as engine
    from app.payroll.settings_registry import SettingValidationError

    try:
        result = engine.write_tenant_value(db, principal, key, value, reason="Company settings")
    except SettingValidationError as error:
        raise bad_request(f"Invalid value for {key}: {error.message}")
    return result["status"] in ("ACTIVE", "UNCHANGED")


def get_all(db: Session, company_id: int) -> dict:
    result = get_values(db, company_id, SETTINGS)
    company = db.get(Company, company_id)
    result.update(
        {
            "companyName": company.name,
            "timezone": company.timezone,
            "currency": company.currency,
            "hasLogo": bool(get_logo_path(db, company_id)) or _has_brand_logo(company_id),
        }
    )
    return result


def _validate_combination(values: Dict[str, Any]) -> None:
    if values["gpsAcceptableAccuracyM"] < values["gpsPreferredAccuracyM"]:
        raise bad_request("Acceptable accuracy must be at least the preferred accuracy.")
    if values["trackingGraceMinutes"] >= values["trackingIntervalMinutes"]:
        raise bad_request("The late-reading grace period must be shorter than the tracking interval.")


def update(db: Session, principal: Principal, data: Dict[str, Any], only: Iterable[str] = None) -> dict:
    company = db.get(Company, principal.company_id)
    allowed = set(only) if only is not None else None
    before = get_values(db, principal.company_id, ATTENDANCE_POLICY_KEYS)
    changed = []
    for key, value in data.items():
        if allowed is not None and key not in allowed:
            continue
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
        coerced = _coerce(key, value)
        if key in ADMIN_ONLY and not principal.is_company_admin:
            if key in before and before[key] == coerced:
                continue  # saving a form unchanged is not an attempt to change the control
            raise bad_request(f"Only company administrators can change {key}.")
        if key in V2_KEYS and not _write_v2(db, principal, key, coerced):
            changed.append(key)
            continue
        row = db.query(CompanySetting).filter(
            CompanySetting.company_id == principal.company_id, CompanySetting.key == key
        ).first()
        if row is None:
            db.add(CompanySetting(company_id=principal.company_id, key=key, value=json.dumps(coerced)))
        else:
            row.value = json.dumps(coerced)
        changed.append(key)
    if changed:
        db.flush()
        after = get_values(db, principal.company_id, ATTENDANCE_POLICY_KEYS)
        _validate_combination(after)
        policy_changes = {k: (before[k], after[k]) for k in ATTENDANCE_POLICY_KEYS if before[k] != after[k]}
        detail = ", ".join(
            f"{k} ({policy_changes[k][0]} -> {policy_changes[k][1]})" if k in policy_changes else k for k in sorted(changed)
        )
        AuditLogService.log(db, principal, "Settings", "UPDATE", f"Updated settings: {detail}")
        if policy_changes:
            from app.services import location_service

            location_service.apply_policy_change(db, principal.company_id, set(policy_changes))
    return get_all(db, principal.company_id)


LOGO_KEY = "_logoPath"


def _has_brand_logo(company_id: int) -> bool:
    from app.services import brand_service

    brand = brand_service.effective_for_company(company_id)
    return brand.sources["logo_light"]["level"] == "TENANT" and bool(brand.values.get("logo_light"))


def get_logo_path(db: Session, company_id: int):
    row = db.query(CompanySetting).filter(CompanySetting.company_id == company_id, CompanySetting.key == LOGO_KEY).first()
    return row.value if row else None


def set_logo_path(db: Session, company_id: int, path: str) -> None:
    row = db.query(CompanySetting).filter(CompanySetting.company_id == company_id, CompanySetting.key == LOGO_KEY).first()
    if row is None:
        db.add(CompanySetting(company_id=company_id, key=LOGO_KEY, value=path))
    else:
        row.value = path

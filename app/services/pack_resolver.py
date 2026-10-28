"""``WORKING_TIME`` pack fields for attendance consumers (appendix-a §2; SP-AS-223, XV-REQ-05).

``resolve(db, company_id, legal_entity_id, field, on) -> PackValue``. Until G-14 registers the ``WORKING_TIME`` pack
kind, its schema and the IN DRAFT pack, every legal entity answers the documented **"no pack"** default
(``source = NO_PACK``): no biometric gate, ``privacy.punch_photo_allowed`` allowed, no retention cap beyond the
SR-AS-17 floor, no rounding restriction, no statutory limit, no "stricter than the pack" check — never a BLOCK or a
WARN from a pack. Callers (capture policy checks, the consent gate, the transparency panel) branch on the value only,
so the real resolver replaces this stub without a caller change; G-06 tests (AS-CTY-10..12) assert this path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from sqlalchemy.orm import Session

NO_PACK = "NO_PACK"
BIOMETRIC_VALUES = ("ALLOWED", "CONSENT_REQUIRED", "ALTERNATIVE_REQUIRED", "PROHIBITED")

# field -> the "no pack" value (appendix-a §2).
NO_PACK_DEFAULTS = {
    "privacy.biometric_timekeeping": None,  # no biometric gate
    "privacy.punch_photo_allowed": True,
    "privacy.location_retention_days_max": None,  # no cap beyond the tenant value
    "privacy.photo_retention_days_max": None,
    "privacy.regime_code": None,
    "privacy.tracking_outside_hours_prohibited": False,
    "rounding.allowed_modes": None,  # all modes
    "breaks.rules": (),  # no statutory meal / rest breaks (04-attendance/03 step 5b)
    "rest.min_between_shifts_hours": None,  # no statutory rest between shifts (step 17)
}


@dataclass(frozen=True)
class PackValue:
    field: str
    value: Any
    source: str = NO_PACK  # NO_PACK | PACK (from G-14)
    pack_code: Optional[str] = None
    pack_version: Optional[str] = None
    verified: bool = False


def resolve(db: Session, company_id: int, legal_entity_id: Optional[int], field: str, on: date) -> PackValue:
    """The pack value of ``field`` for the legal entity on the local date ``on`` (no pack yet: the default)."""
    if field not in NO_PACK_DEFAULTS:
        raise KeyError(f"unknown WORKING_TIME pack field {field!r}")
    return PackValue(field=field, value=NO_PACK_DEFAULTS[field])


def biometrics_prohibited(db: Session, company_id: int, legal_entity_id: Optional[int], on: date) -> bool:
    """B-2 / AS-CTY-12: FACE stops at once where the resolved pack prohibits biometric timekeeping."""
    return resolve(db, company_id, legal_entity_id, "privacy.biometric_timekeeping", on).value == "PROHIBITED"


def punch_photo_allowed(db: Session, company_id: int, legal_entity_id: Optional[int], on: date) -> bool:
    return resolve(db, company_id, legal_entity_id, "privacy.punch_photo_allowed", on).value is not False

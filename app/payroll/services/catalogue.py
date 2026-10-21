"""Read-only ISO catalogues and country payroll capability (3.2 §2, §6.2; decision I-06).

Currencies, countries and subdivisions come from code (app.payroll.currencies / countries). A country's
capability level is platform state: a country_capabilities row when an operator set one, else the code
default: CONFIG_ONLY for the countries whose packs ship as drafts (IN, AE, US; ARCHITECTURE §10) and
DISPLAY_ONLY for every other active country. A default never claims statutory support (EC-STA-01).
"""

from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.errors import not_found
from app.models.catalogue import CountryCapability
from app.payroll import countries, currencies

DRAFT_PACK_COUNTRIES = frozenset({"IN", "AE", "US"})
DISPLAY_ONLY, CONFIG_ONLY, UNSUPPORTED = "DISPLAY_ONLY", "CONFIG_ONLY", "UNSUPPORTED"
# Whether payroll lines may be statutory-final at each level (3.2 §6.2, decision I-07).
STATUTORY_LEVELS = frozenset({"VERIFIED", "FILING_INTEGRATED"})


def list_currencies(*, include_historic: bool = False, payroll_only: bool = False) -> List[dict]:
    rows = currencies.all() if include_historic else currencies.active()
    return [c.to_dict() for c in rows if c.payroll_allowed or not payroll_only]


def list_countries(*, include_deprecated: bool = False) -> List[dict]:
    rows = countries.all() if include_deprecated else countries.active()
    return [c.to_dict() for c in rows]


def subdivisions(alpha2: str) -> List[dict]:
    country = countries.find((alpha2 or "").upper())
    if country is None:
        raise not_found("Country")
    if country.status != countries.ACTIVE:
        return []
    return [s.to_dict() for s in countries.subdivisions(country.alpha2)]


def default_capability(alpha2: str) -> str:
    country = countries.find(alpha2)
    if country is None or country.status != countries.ACTIVE:
        return UNSUPPORTED
    return CONFIG_ONLY if alpha2 in DRAFT_PACK_COUNTRIES else DISPLAY_ONLY


def _row(alpha2: str, stored: Optional[CountryCapability]) -> dict:
    level = stored.capability_level if stored is not None else default_capability(alpha2)
    return {
        "alpha2": alpha2,
        "capability_level": level,
        "source": "PLATFORM" if stored is not None else "DEFAULT",
        "note": stored.note if stored is not None else None,
        "updated_at": stored.updated_at if stored is not None else None,
        "statutory_verified": level in STATUTORY_LEVELS,
    }


def capability(db: Session, alpha2: str) -> dict:
    code = (alpha2 or "").upper()
    if countries.find(code) is None:
        raise not_found("Country")
    return _row(code, db.get(CountryCapability, code))


def capabilities(db: Session, *, country: Optional[str] = None) -> List[dict]:
    if country:
        return [capability(db, country)]
    stored: Dict[str, CountryCapability] = {row.alpha2: row for row in db.query(CountryCapability).all()}
    return [_row(c.alpha2, stored.get(c.alpha2)) for c in countries.active()]

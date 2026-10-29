"""WORKING_TIME country packs: India, UAE, United States (G-22; AS appendix-a; owner choice 2026-10-29).

One registry read by ``roster_validation`` (scheduling limits) and ``pack_resolver`` (breaks, rest, overtime consent,
privacy, scheduling notice). Each field carries its source; a pack is ``verified`` only where the documentation gives
a primary source **and** the tenant has not opted out — unverified values act as WARN ("Compliance not verified"),
never as BLOCK (IG-AS-31). Values are illustrative statutory floors, not legal advice; tenants make limits stricter
through their policies, never looser.

* **IN** — OSH Code 2020 / Factories Act style: 48 h a week, 9 h a day, 6 days a week, 24 h weekly rest, 30 min
  break after 5 h, overtime needs consent for women at night (night window 19:00–06:00), overtime pay ×2, location
  data retention ≤ 365 days.
* **AE** — Federal Decree-Law 33/2021 style: 8 h a day / 48 h a week, 1 h break after 5 h, overtime ≤ 2 h a day,
  ×1.25 (×1.5 at night), Ramadan hours reduced by 2 h a day (dated window).
* **US** — FLSA federal: no daily cap, overtime ×1.5 over 40 h a week, no statutory break; Fair Workweek (NYC/Seattle
  style, opt-in per legal entity): 14 days' notice, predictability pay 1 h per employer change inside the notice,
  4 h for a cancellation inside 24 h; late additions need the employee's written consent.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional

PACK_VERSION = "2026.10.1"

PACKS: Dict[str, Dict[str, Any]] = {
    "IN": {
        "name": "India (OSH Code / Factories Act)", "verified": False,
        "sources": ["OSH Code 2020 s.25-27 (hours, weekly holiday, overtime)", "OSH Code 2020 s.43 (women at night)",
                    "Factories Act 1948 s.51, s.54, s.55 (weekly 48 h, daily 9 h, interval of rest)"],
        "weekly_max_hours": Decimal(48), "daily_max_hours": None, "max_days_per_week": 6,  # 9 h/day: FU (not enforced yet)
        "max_consecutive_days": 10, "weekly_rest_min_hours": Decimal(24), "min_rest_hours": None,
        "night_work_groups": {"WOMEN": ("19:00", "06:00")},
        "breaks": [{"kind": "REST", "after_minutes": 300, "minutes": 30, "paid": False}],
        "overtime": {"multiplier": "2.0", "requires_consent": True, "consent_enforce": False,
                     "caps": [{"window": "QUARTER", "hours": 125}]},
        "privacy": {"location_retention_days_max": 365, "biometric_timekeeping": "CONSENT_REQUIRED",
                    "punch_photo_allowed": True},
        "scheduling": {"notice_hours": None, "late_addition_consent": False},
    },
    "AE": {
        "name": "United Arab Emirates (Decree-Law 33/2021)", "verified": False,
        "sources": ["Federal Decree-Law 33/2021 art.17 (8 h a day, 48 h a week)", "art.17(4) (Ramadan hours)",
                    "art.19 (overtime 2 h a day, 25 % / 50 %)", "art.15 (break after 5 h)"],
        "weekly_max_hours": Decimal(48), "daily_max_hours": Decimal(10), "max_days_per_week": 6,
        "max_consecutive_days": None, "weekly_rest_min_hours": Decimal(24), "min_rest_hours": None,
        "night_work_groups": {},
        "breaks": [{"kind": "REST", "after_minutes": 300, "minutes": 60, "paid": False}],
        "overtime": {"multiplier": "1.25", "night_multiplier": "1.5", "requires_consent": False,
                     "consent_enforce": False, "caps": [{"window": "DAY", "hours": 2}]},
        "privacy": {"location_retention_days_max": None, "biometric_timekeeping": "ALLOWED",
                    "punch_photo_allowed": True},
        "scheduling": {"notice_hours": None, "late_addition_consent": False},
        # Ramadan: two hours less a day (dated per Hijri year; tenants publish the window in their calendar).
        "reduced_hours_windows": [{"code": "RAMADAN_2027", "from": date(2027, 2, 8), "to": date(2027, 3, 9),
                                   "daily_max_hours": Decimal(6)}],
    },
    "US": {
        "name": "United States (FLSA; Fair Workweek opt-in)", "verified": False,
        "sources": ["FLSA 29 U.S.C. 207 (overtime over 40 h a week, ×1.5)",
                    "NYC Admin Code 20-1221/1222 (14 days' notice, predictability pay)",
                    "Seattle SMC 14.22 (secure scheduling, offer hours first)"],
        "weekly_max_hours": None, "daily_max_hours": None, "max_days_per_week": None, "max_consecutive_days": None,
        "weekly_rest_min_hours": None, "min_rest_hours": None, "night_work_groups": {},
        "breaks": [],
        "overtime": {"multiplier": "1.5", "weekly_threshold_hours": 40, "requires_consent": False,
                     "consent_enforce": False, "caps": []},
        "privacy": {"location_retention_days_max": None, "biometric_timekeeping": "CONSENT_REQUIRED",
                    "punch_photo_allowed": True},
        "scheduling": {"notice_hours": 336, "late_addition_consent": True,
                       "predictability_pay_hours": {"CHANGE": 1, "CANCELLATION_24H": 4}},
    },
}


def pack(country: Optional[str]) -> Optional[Dict[str, Any]]:
    return PACKS.get((country or "").upper())


def daily_max_on(p: Mapping[str, Any], on: date) -> Optional[Decimal]:
    """The daily limit in force on ``on`` (a reduced-hours window such as Ramadan wins)."""
    for window in p.get("reduced_hours_windows") or []:
        if window["from"] <= on <= window["to"]:
            return window["daily_max_hours"]
    return p.get("daily_max_hours")


def field(p: Optional[Mapping[str, Any]], name: str) -> Any:
    """``pack_resolver`` field names (appendix-a §2) -> pack values; KeyError for a field the pack lacks."""
    if p is None:
        raise KeyError(name)
    mapping = {
        "privacy.biometric_timekeeping": p["privacy"].get("biometric_timekeeping"),
        "privacy.punch_photo_allowed": p["privacy"].get("punch_photo_allowed", True),
        "privacy.location_retention_days_max": p["privacy"].get("location_retention_days_max"),
        "privacy.photo_retention_days_max": None,
        "privacy.regime_code": None,
        "privacy.tracking_outside_hours_prohibited": True,
        "rounding.allowed_modes": None,
        "breaks.rules": tuple(p.get("breaks") or ()),
        "rest.min_between_shifts_hours": p.get("min_rest_hours"),
        "overtime.requires_consent": p["overtime"].get("requires_consent", False),
        "overtime.consent_enforce": p["overtime"].get("consent_enforce", False),
        "overtime.caps": tuple(p["overtime"].get("caps") or ()),
        "scheduling.notice_hours": p["scheduling"].get("notice_hours"),
        "scheduling.late_addition_consent": p["scheduling"].get("late_addition_consent", False),
    }
    if name not in mapping:
        raise KeyError(name)
    return mapping[name]


def describe(country: str) -> Dict[str, Any]:
    """JSON view of a pack for the settings page (decimals and dates as strings)."""
    p = pack(country)
    if p is None:
        return {"country": country, "pack": None}

    def j(value):
        if isinstance(value, Decimal):
            return format(value.normalize(), "f")
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, dict):
            return {k: j(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [j(v) for v in value]
        return value

    return {"country": country, "version": PACK_VERSION, "pack": j(p)}

"""Seed the shared time-stack dataset: Alpha (IN), Beta (AE), Gamma (US) (docs/README.md §4; AS spine §0.3, §15.6;
LH 12 §1.1). Non-production databases only.

    python scripts/demo_stack.py --fresh --data-dir .local-postgres-time --port 8500
    python scripts/seed_time_three_companies.py --api http://localhost:8500/api/v1 [--goal G-01]

Everything goes through the public API as each company's own users, so the seed obeys the product's validation and
permissions. It is idempotent: objects are looked up by their natural key (e-mail, code, name, dates) before they
are created, and a second run changes nothing. Passwords are random per account and stored only in the git-ignored
``.local-secrets/time-credentials.json`` (never printed). A step the API refuses is recorded under ``blocked``,
never hidden.

The same module seeds the backend golden baselines (tests/time_goldens.py) through the in-process test client with
a fixed password and a frozen attendance clock, so the Chrome stack and the goldens share one dataset.

Goal G-01 (AS-M0 / LH-M0) seeds today's model only: placement shifts (no versions, codes or kinds yet — the shift
*name* is the spine §0.3 code), default roles (spine role templates arrive with G-05 / G-08 / G-11; the mapping is in
``ROLE_MAP``), one site-less placement for site 2203 (sites arrive in G-06). Later goals extend ``GOALS``.

Calendar anchors (spine §0.3): Mon 2026-10-26 = "today"; attendance history in October 2026; leave requests from
November 2026 to January 2027 (a cross-year request included).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytz

sys.path.insert(0, str(Path(__file__).resolve().parent))

from seed_payroll_three_companies import Api, SeedError, http_client_factory, items_of, new_password  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CREDENTIALS_JSON = ROOT / ".local-secrets" / "time-credentials.json"  # git-ignored (hrms-backend/.gitignore)
DATASET_VERSION = "time-2026.10.27"
GOALS = ("G-01", "G-02", "G-03", "G-04")  # a goal seeds its own objects and every earlier goal's
TODAY = date(2026, 10, 26)
HIRED = date(2025, 1, 6)
TAG = "time stack"

# Spine §7 role templates that do not exist yet, mapped onto today's default roles (DECISIONS IG-AS-11).
ROLE_MAP = {"executive": "manager", "hr_manager": "manager", "hr_executive": "manager",
            "department_head": "manager", "TEAM_LEAD": "manager", "SHIFT_SUPERVISOR": "manager",
            "TIMEKEEPER": "manager", "ROSTER_PLANNER": "employee"}

# G-03 custom role: the work-calendar duties of an HR administrator, without leave approval (personas unchanged).
WORK_CALENDAR_ADMIN_KEY = "work_calendar_admin"
WORK_CALENDAR_ADMIN = ("Work calendar administrator",
                       "Holiday calendars, work weeks and their effective-dated links for the whole company.",
                       [("holiday:read", "COMPANY"), ("holiday:manage", "COMPANY"), ("holiday:publish", "COMPANY")])

# Work weeks: code, name, weekly offs (0 = Monday), nth-weekday offs.
MON_FRI = ("MON_FRI", "Monday to Friday", [5, 6], [])
MON_SAT = ("MON_SAT", "Monday to Saturday", [6], [])
MON_SAT_2ND_4TH = ("MON_SAT_2ND_4TH_SAT_OFF", "Monday to Saturday, 2nd and 4th Saturday off", [6],
                   [{"weekday": 5, "nth": [2, 4]}])

# Shift: name (= spine code), start, end, gross hours, unpaid break minutes, flexible, description.
Shift = Tuple[str, str, str, str, int, bool, str]

# Person: key, first, last, roles (spine names; ROLE_MAP applied), entity, branch, department, manager key, shift code,
# position, extras.
Person = Tuple[str, str, str, List[str], str, str, str, Optional[str], Optional[str], str, dict]

COMPANIES: Dict[str, dict] = {
    "alpha": {
        "name": "Alpha Technologies", "timezone": "Asia/Kolkata", "domain": "alpha-time.example",
        "founder": ("Ananya", "Rao"), "currency": "INR",
        "entities": [
            {"code": "LE11", "legal_name": "Alpha Technologies Pvt Ltd", "country_code": "IN", "base_currency": "INR",
             "timezone": "Asia/Kolkata", "confirm_country": True},
            {"code": "LE12", "legal_name": "Alpha Gulf FZ-LLC", "country_code": "AE", "base_currency": "AED",
             "timezone": "Asia/Dubai", "confirm_country": True},
        ],
        # key, entity code, name, branch code, subdivision, timezone, work week
        "branches": [("br21", "LE11", "Bengaluru Office", "BR21", "IN-KA", "Asia/Kolkata", MON_FRI),
                     ("br22", "LE11", "Pune Plant", "BR22", "IN-MH", "Asia/Kolkata", MON_SAT_2ND_4TH),
                     ("br24", "LE11", "Alpha Care Clinic, Pune", "BR24", "IN-MH", "Asia/Kolkata", MON_SAT),
                     ("br23", "LE12", "Dubai Office", "BR23", "AE-DU", "Asia/Dubai", MON_FRI)],
        "departments": ["Engineering", "Operations", "Production", "Packing", "HR", "Finance", "Sales", "Clinic"],
        "shifts": [
            ("GEN", "09:00", "18:00", "9", 60, False, "General shift (Pune Plant)"),
            ("A", "06:00", "14:00", "8", 0, False, "Factory shift A (paid lunch)"),
            ("B", "14:00", "22:00", "8", 0, False, "Factory shift B (paid lunch)"),
            ("C", "22:00", "06:00", "8", 0, False, "Factory shift C, crosses midnight (paid lunch)"),
            ("FLEX-BLR", "09:00", "18:00", "9", 45, True, "Bengaluru IT flexible shift (9 h until AS-M1)"),
            ("US-E1", "18:30", "03:30", "9", 60, False, "BPO US process, New York 09:00-18:00 on EDT"),
            ("N12", "20:00", "08:00", "12", 0, False, "Clinic night nursing (paid rest)"),
            ("SITE-D", "08:00", "17:00", "9", 60, False, "Hinjewadi project site day shift"),
        ],
        "people": [
            # Managers are listed before their reports.
            ("rahul", "Rahul", "Kapoor", ["executive"], "LE11", "br21", "Operations", None, None, "Chief Executive", {}),
            ("kavya", "Kavya", "Nair", ["hr_admin"], "LE11", "br21", "HR", "rahul", None, "HR Administrator", {}),
            ("sunil", "Sunil", "Rao", ["hr_manager"], "LE11", "br21", "HR", "kavya", None, "HR Manager", {}),
            ("asha", "Asha", "Pillai", ["hr_executive", "TIMEKEEPER"], "LE11", "br22", "HR", "sunil", "GEN",
             "HR Executive", {}),
            ("arjun", "Arjun", "Mehta", ["manager"], "LE11", "br21", "Engineering", "rahul", "FLEX-BLR",
             "Engineering Manager", {}),
            ("meghna", "Meghna", "Rao", ["employee", "TEAM_LEAD"], "LE11", "br21", "Engineering", "arjun", "FLEX-BLR",
             "Tech Lead", {}),
            ("priya", "Priya", "Shah", ["employee"], "LE11", "br21", "Engineering", "arjun", "FLEX-BLR",
             "Software Engineer", {}),
            ("rohan", "Rohan", "Verma", ["employee"], "LE11", "br21", "Engineering", "arjun", "FLEX-BLR",
             "Software Engineer", {}),
            ("deepa", "Deepa", "Nair", ["employee", "TEAM_LEAD"], "LE11", "br21", "Operations", "arjun", "US-E1",
             "Team Lead, US process", {}),
            ("sameer", "Sameer", "Khan", ["employee"], "LE11", "br21", "Operations", "deepa", "US-E1",
             "Process Associate", {}),
            ("pooja", "Pooja", "Kulkarni", ["employee"], "LE11", "br21", "Operations", "deepa", "US-E1",
             "Process Associate", {}),
            ("nitin", "Nitin", "Gowda", ["employee"], "LE11", "br21", "Sales", "arjun", None, "Field Sales", {}),
            ("karan", "Karan", "Desai", ["payroll_manager", "employee"], "LE11", "br21", "Finance", "kavya", None,
             "Payroll Manager", {}),
            ("neha", "Neha", "Joshi", ["department_head", "manager"], "LE11", "br22", "Production", "rahul", "GEN",
             "Head of Production", {}),
            ("vikram", "Vikram", "Singh", ["manager", "SHIFT_SUPERVISOR"], "LE11", "br22", "Packing", "neha", "GEN",
             "Shift Supervisor", {}),
            ("kiran", "Kiran", "Joshi", ["employee", "ROSTER_PLANNER"], "LE11", "br22", "Production", "neha", "GEN",
             "Roster Planner", {}),
            ("ravi", "Ravi", "Kumar", ["employee"], "LE11", "br22", "Packing", "vikram", "B", "Operator", {}),
            ("sunita", "Sunita", "Patil", ["employee"], "LE11", "br22", "Packing", "vikram", "C", "Operator", {}),
            ("imran", "Imran", "Shaikh", ["employee"], "LE11", "br22", "Packing", "vikram", "A", "Contract Operator",
             {}),
            # Site 2203 Hinjewadi (under BR 22): sites arrive with G-06, the placement is BR 22 until then.
            ("prakash", "Prakash", "More", ["employee", "SHIFT_SUPERVISOR"], "LE11", "br22", "Operations", "neha",
             "SITE-D", "Site Supervisor", {"site": "2203"}),
            ("ramesh", "Ramesh", "Yadav", ["employee"], "LE11", "br22", "Operations", "prakash", "SITE-D",
             "Construction Worker", {"site": "2203"}),
            ("leena", "Leena", "D'Souza", ["employee", "SHIFT_SUPERVISOR"], "LE11", "br24", "Clinic", "neha", "N12",
             "Ward In-charge", {}),
            ("anjali", "Anjali", "Menon", ["employee"], "LE11", "br24", "Clinic", "leena", "N12", "Staff Nurse", {}),
            ("omar", "Omar", "Haddad", ["hr_manager"], "LE12", "br23", "HR", "kavya", None, "Gulf HR Manager", {}),
            ("farah", "Farah", "Ali", ["employee"], "LE12", "br23", "Engineering", "omar", None, "Software Engineer",
             {}),
        ],
        "holidays": [
            ("Gandhi Jayanti", "2025-10-02", None, False), ("Christmas", "2025-12-25", None, False),
            ("Republic Day", "2026-01-26", None, False), ("Maharashtra Day", "2026-05-01", "Pune Plant", False),
            ("Gandhi Jayanti", "2026-10-02", None, False), ("Kannada Rajyotsava", "2026-11-02", "Bengaluru Office", False),
            ("Diwali (optional)", "2026-11-09", None, True), ("Christmas", "2026-12-25", None, False),
            ("Republic Day", "2027-01-26", None, False),
        ],
        # employee key, type, start, end, start portion, end portion, outcome (approve | reject | cancel | pending)
        "leaves": [
            ("priya", "casual", "2026-11-16", "2026-11-17", "FULL", "FULL", "approve"),
            ("rohan", "sick", "2026-11-20", "2026-11-20", "FULL", "FULL", "pending"),
            ("meghna", "casual", "2026-12-30", "2027-01-01", "FULL", "FULL", "approve"),
            ("ravi", "earned", "2026-11-12", "2026-11-13", "FULL", "FIRST_HALF", "approve"),
            ("sunita", "casual", "2026-11-23", "2026-11-23", "FULL", "FULL", "reject"),
            ("kiran", "casual", "2026-12-07", "2026-12-07", "FULL", "FULL", "cancel"),
            ("nitin", "earned", "2026-11-03", "2026-11-06", "FULL", "FULL", "pending"),
            ("sameer", "sick", "2026-11-04", "2026-11-04", "SECOND_HALF", "FULL", "approve"),
        ],
        # G-02: overtime entries for the pilot list (filed by the manager): employee key, manager key, minutes.
        # G-03: Arjun also administers the work calendar (work weeks, links) company-wide (LH DECISIONS IG-LH-02).
        "work_calendar_admins": ["arjun"],
        "overtime": [("priya", "arjun", 90), ("rohan", "arjun", 60), ("meghna", "arjun", 120),
                     ("deepa", "arjun", 45), ("ravi", "vikram", 180), ("sunita", "vikram", 75)],
        "attendance_by": "kavya",
        # employee key, dates, in (local), out (local; "+1" = next day), status, unpaid lunch (local start, minutes)
        "attendance": [
            ("priya", "weekdays", "09:40", "18:55", "present", ("13:30", 45)),
            ("sameer", "weekdays", "18:25", "+1 03:35", "present", ("23:00", 60)),
            ("ravi", "weekdays", "13:58", "22:02", "present", None),
            ("ravi", ["2026-10-14"], "14:22", "22:00", "late", None),
            ("anjali", "weekdays", "19:55", "+1 08:05", "present", None),
            ("kiran", ["2026-10-07"], "09:05", "13:10", "half-day", None),
            ("ramesh", ["2026-10-16"], "08:02", None, "present", None),
        ],
    },
    "beta": {
        "name": "Beta Retail Group", "timezone": "Asia/Dubai", "domain": "beta-time.example",
        "founder": ("Salem", "Al Mansoori"), "currency": "AED",
        "entities": [
            {"code": "LE31", "legal_name": "Beta FZ-LLC", "country_code": "AE", "base_currency": "AED",
             "timezone": "Asia/Dubai", "confirm_country": True},
            {"code": "LE32", "legal_name": "Beta Clinic LLC", "country_code": "AE", "base_currency": "AED",
             "timezone": "Asia/Dubai", "confirm_country": True},
        ],
        "branches": [("br51", "LE31", "Dubai HQ", "BR51", "AE-DU", "Asia/Dubai", MON_FRI),
                     ("br52", "LE31", "Abu Dhabi Site", "BR52", "AE-AZ", "Asia/Dubai", MON_SAT),
                     ("br311", "LE31", "Beta Dubai Mall Store", "BR311", "AE-DU", "Asia/Dubai", MON_SAT),
                     ("br321", "LE32", "Beta Jumeirah Clinic", "BR321", "AE-DU", "Asia/Dubai", MON_SAT)],
        "departments": ["HR", "Retail", "Clinic"],
        "shifts": [
            ("BD9", "10:00", "19:00", "9", 60, False, "Store day shift"),
            ("BC12N", "19:00", "07:00", "12", 0, False, "Clinic night (paid rest)"),
        ],
        "people": [
            ("mariam", "Mariam", "Saeed", ["hr_admin"], "LE31", "br51", "HR", None, None, "HR Administrator", {}),
            ("yusuf", "Yusuf", "Rahman", ["manager", "SHIFT_SUPERVISOR"], "LE31", "br311", "Retail", None, "BD9",
             "Store Manager", {}),
            ("layla", "Layla", "Hassan", ["employee"], "LE31", "br311", "Retail", "yusuf", "BD9", "Sales Associate",
             {}),
            ("maria", "Maria", "Santos", ["employee"], "LE32", "br321", "Clinic", "mariam", "BC12N", "Clinic Nurse", {}),
        ],
        "holidays": [("UAE National Day", "2025-12-02", None, False), ("Eid al-Fitr", "2026-03-20", None, False),
                     ("UAE National Day", "2026-12-02", None, False)],
        "leaves": [("layla", "casual", "2026-11-10", "2026-11-10", "FULL", "FULL", "approve"),
                   ("maria", "sick", "2026-11-11", "2026-11-11", "FULL", "FULL", "pending")],
        "attendance_by": "mariam",
        "attendance": [("layla", "weekdays", "09:58", "19:04", "present", ("14:00", 60)),
                       ("maria", "weekdays", "18:55", "+1 07:05", "present", None)],
    },
    "gamma": {
        "name": "Gamma Retail", "timezone": "America/New_York", "domain": "gamma-time.example",
        "founder": ("Grace", "Miller"), "currency": "USD",
        "entities": [{"code": "LE41", "legal_name": "Gamma Retail Inc", "country_code": "US", "base_currency": "USD",
                      "timezone": "America/New_York", "confirm_country": True}],
        "branches": [("br411", "LE41", "Gamma Manhattan Store", "BR411", "US-NY", "America/New_York", MON_FRI),
                     ("br412", "LE41", "Gamma Queens Warehouse", "BR412", "US-NY", "America/New_York", MON_FRI)],
        "departments": ["Store", "Warehouse"],
        "shifts": [
            ("NY-OPEN", "08:30", "17:00", "8.5", 30, False, "Store opening shift"),
            ("WH-NIGHT", "22:00", "06:00", "8", 30, False, "Warehouse night shift"),
        ],
        "people": [
            ("emily", "Emily", "Clark", ["manager", "TEAM_LEAD", "SHIFT_SUPERVISOR"], "LE41", "br411", "Store", None,
             "NY-OPEN", "Store Manager", {}),
            ("jordan", "Jordan", "Lee", ["employee"], "LE41", "br411", "Store", "emily", "NY-OPEN", "Store Associate",
             {}),
            ("carlos", "Carlos", "Diaz", ["employee"], "LE41", "br412", "Warehouse", "emily", "WH-NIGHT",
             "Warehouse Associate", {}),
        ],
        "holidays": [("Thanksgiving", "2025-11-27", None, False), ("Christmas", "2025-12-25", None, False),
                     ("Independence Day (observed)", "2026-07-03", None, False),
                     ("Thanksgiving", "2026-11-26", None, False), ("Christmas", "2026-12-25", None, False)],
        "leaves": [("jordan", "casual", "2026-11-27", "2026-11-27", "FULL", "FULL", "approve"),
                   ("carlos", "casual", "2026-12-01", "2026-12-01", "FULL", "FULL", "pending")],
        "attendance_by": "founder",
        "attendance": [("jordan", "weekdays", "08:28", "17:03", "present", ("12:30", 30)),
                       ("carlos", "weekdays", "21:57", "+1 06:04", "present", ("02:00", 30))],
    },
}

# G-04 (AS-M1): the cast catalogue of 03-shifts/01 §2.4 through the new shift API, version 1 from 2000-01-01.
# code -> (name, kind, owner branch key, version body). GEN is the shift every new company gets (BUG-AS-33).
_LUNCH_60 = {"kind": "LUNCH", "mode": "FIXED_WINDOW", "minutes": 60, "paid": False, "start": "13:00", "end": "14:00"}
CAST_SHIFTS: Dict[str, Dict[str, Tuple[str, str, Optional[str], dict]]] = {
    "alpha": {
        "A": ("Factory shift A", "FIXED", "br22", {"start_time": "06:00", "end_time": "14:00", "grace_in_minutes": 10,
              "break_rules": [{"kind": "LUNCH", "mode": "FLOATING_WINDOW", "minutes": 30, "paid": True,
                               "window_start": "09:30", "window_end": "11:30"}]}),
        "B": ("Factory shift B", "FIXED", "br22", {"start_time": "14:00", "end_time": "22:00", "grace_in_minutes": 10,
              "break_rules": [{"kind": "LUNCH", "mode": "FLOATING_WINDOW", "minutes": 30, "paid": True,
                               "window_start": "17:30", "window_end": "19:30"}]}),
        "C": ("Factory shift C", "FIXED", "br22", {"start_time": "22:00", "end_time": "06:00", "grace_in_minutes": 10,
              "break_rules": [{"kind": "LUNCH", "mode": "FLOATING_WINDOW", "minutes": 30, "paid": True,
                               "window_start": "01:30", "window_end": "03:30", "next_day": True}]}),
        "FLEX-BLR": ("Flexi Bengaluru", "FLEXIBLE", "br21", {
            "start_time": "08:00", "end_time": "20:00", "core_start_time": "11:00", "core_end_time": "16:00",
            "expected_net_minutes": 480,
            "break_rules": [{"kind": "LUNCH", "mode": "AUTO_DEDUCT", "minutes": 45, "paid": False,
                             "auto_deduct_after_minutes": 360}]}),
        "US-E1": ("US process (New York hours)", "FIXED", "br21", {
            "start_time": "09:00", "end_time": "18:00", "anchor_timezone": "America/New_York",
            "break_rules": [{"kind": "LUNCH", "mode": "FLOATING_WINDOW", "minutes": 60, "paid": False,
                             "window_start": "12:00", "window_end": "14:00"}]}),
        "FIELD": ("Field sales", "OPEN", "br21", {"expected_net_minutes": 480}),
        "N12": ("Clinic night", "FIXED", "br24", {"start_time": "20:00", "end_time": "08:00",
                "break_rules": [{"kind": "REST", "mode": "FLOATING_WINDOW", "minutes": 15, "paid": True,
                                 "window_start": "00:00", "window_end": "04:00", "next_day": True}]}),
        "SITE-D": ("Hinjewadi site day", "FIXED", "br22", {"start_time": "08:00", "end_time": "17:00",
                   "outdoor": True, "break_rules": [_LUNCH_60]}),
    },
    "beta": {
        "BD9": ("Store day", "FIXED", "br311", {"start_time": "10:00", "end_time": "19:00", "break_rules": [
            {"kind": "LUNCH", "mode": "FIXED_WINDOW", "minutes": 60, "paid": False, "start": "14:00", "end": "15:00"}]}),
        "BC12N": ("Clinic night", "FIXED", "br321", {"start_time": "19:00", "end_time": "07:00", "break_rules": [
            {"kind": "REST", "mode": "FLOATING_WINDOW", "minutes": 30, "paid": True, "window_start": "23:00",
             "window_end": "03:00"}]}),
    },
    "gamma": {
        "NY-OPEN": ("Store opening", "FIXED", "br411", {"start_time": "08:30", "end_time": "17:00", "break_rules": [
            {"kind": "LUNCH", "mode": "PUNCHED", "minutes": 30, "paid": False}]}),
        "WH-NIGHT": ("Warehouse night", "FIXED", "br412", {"start_time": "22:00", "end_time": "06:00", "break_rules": [
            {"kind": "LUNCH", "mode": "PUNCHED", "minutes": 30, "paid": False}]}),
    },
}

# Attendance history dates (two working weeks of October 2026, before the "today" anchor).
HISTORY_WEEKDAYS = [date(2026, 10, d) for d in (5, 6, 7, 8, 9, 12, 13, 14, 15, 16)]


@dataclass
class Context:
    make_client: Callable[[], Any]
    store: dict
    password: Optional[str] = None  # fixed password (tests); random per account otherwise
    before_login: Optional[Callable[[], None]] = None
    log: Callable[[str], None] = print
    blocked: List[dict] = field(default_factory=list)
    stats: Dict[str, int] = field(default_factory=lambda: {"created": 0, "existing": 0})

    def created(self, what: str) -> None:
        self.stats["created"] += 1
        self.log(f"[time-seed]   + {what}")

    def existing(self) -> None:
        self.stats["existing"] += 1

    def block(self, company: str, step: str, error: Exception) -> None:
        entry = {"company": company, "step": step, "endpoint": getattr(error, "endpoint", None),
                 "status": getattr(error, "status", None), "code": getattr(error, "code", None),
                 "detail": str(getattr(error, "detail", error))[:300]}
        self.blocked.append(entry)
        self.log(f"[time-seed]   ! {company}: {step} blocked: {entry['endpoint']} {entry['status']} {entry['code']}")

    def new_password(self) -> str:
        return self.password or new_password()


def login(ctx: Context, address: str, password: str) -> Api:
    if ctx.before_login:
        ctx.before_login()
    api = Api(ctx.make_client(), address)
    data = api.post("auth/login", {"email": address, "password": password})
    if isinstance(data, dict) and data.get("mfa_required"):
        raise RuntimeError(f"{address} needs MFA; seed accounts never enrol MFA")
    return api


def local_iso(day: date, hhmm: str, tz: str) -> str:
    """ISO instant (with offset) of a local wall time; "+1 HH:MM" is the next day."""
    if hhmm.startswith("+1 "):
        day, hhmm = day + timedelta(days=1), hhmm[3:]
    h, m = (int(x) for x in hhmm.split(":"))
    return pytz.timezone(tz).localize(datetime(day.year, day.month, day.day, h, m)).isoformat()


class CompanySeed:
    def __init__(self, ctx: Context, key: str, spec: dict):
        self.ctx, self.key, self.spec = ctx, key, spec
        self.state = ctx.store.setdefault("companies", {}).setdefault(key, {"name": spec["name"], "accounts": {}})
        self.accounts: dict = self.state.setdefault("accounts", {})
        self.sessions: Dict[str, Api] = {}
        self.ids: Dict[str, dict] = {"entities": {}, "branches": {}, "departments": {}, "shifts": {}, "people": {},
                                     "roles": {}, "work_weeks": {}}

    # ---------------------------------------------------------------- helpers
    def email_of(self, first: str, last: str) -> str:
        clean = "".join(ch for ch in last.lower() if ch.isalpha())
        return f"{first.lower()}.{clean}@{self.spec['domain']}"

    @property
    def admin(self) -> Api:
        return self.sessions["founder"]

    def session(self, key: str) -> Api:
        if key not in self.sessions:
            account = self.accounts[key]
            self.sessions[key] = login(self.ctx, account["email"], account["password"])
        return self.sessions[key]

    def optional(self, step: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except (SeedError, RuntimeError, KeyError, ValueError) as error:
            self.ctx.block(self.spec["name"], step, error)
        return None

    # ---------------------------------------------------------------- tenant
    def ensure_founder(self) -> None:
        first, last = self.spec["founder"]
        address = self.email_of(first, last)
        account = self.accounts.get("founder")
        if account and account.get("password"):
            self.sessions["founder"] = login(self.ctx, account["email"], account["password"])
            self.ctx.existing()
        else:
            password = self.ctx.new_password()
            api = Api(self.ctx.make_client(), address)
            if self.ctx.before_login:
                self.ctx.before_login()
            try:
                api.post("auth/signup", {"company_name": self.spec["name"], "full_name": f"{first} {last}",
                                         "email": address, "password": password, "timezone": self.spec["timezone"]})
            except SeedError as error:
                raise RuntimeError(f"Signup of {self.spec['name']} failed ({error.code}); reset the stack if the "
                                   f"company exists without its credential file") from error
            self.accounts["founder"] = {"email": address, "password": password, "name": f"{first} {last}",
                                        "roles": ["company_admin"]}
            self.sessions["founder"] = api
            self.ctx.created(f"company {self.spec['name']} (founder {address})")
        me = self.admin.get("auth/me")
        self.state.update(company_id=me["company"]["id"], slug=me["company"]["slug"])
        self.ids["people"]["founder"] = {"employee_id": me["employee"]["id"], "user_id": me["user"]["id"],
                                         "email": address}
        current = self.admin.get("settings")
        if current.get("currency") != self.spec["currency"]:
            self.admin.put("settings", {"currency": self.spec["currency"]})
            self.ctx.created("company currency")

    def ensure_entities(self) -> None:
        rows = self.admin.get("legal-entities")
        by_code = {e["code"]: e for e in rows}
        default = next((e for e in rows if e.get("is_default")), None)
        for index, wanted in enumerate(self.spec["entities"]):
            row = by_code.get(wanted["code"]) or (default if index == 0 else None)
            fields = {k: v for k, v in wanted.items() if k != "confirm_country"}
            if row is None:
                row = self.admin.post("legal-entities", wanted)
                self.ctx.created(f"legal entity {wanted['code']}")
            elif any(row.get(k) != v for k, v in fields.items()) or not row.get("country_confirmed"):
                row = self.admin.patch(f"legal-entities/{row['id']}", wanted)
                self.ctx.created(f"legal entity {wanted['code']} (updated)")
            else:
                self.ctx.existing()
            self.ids["entities"][wanted["code"]] = row["id"]

    def ensure_work_weeks(self) -> None:
        rows = {w["code"]: w for w in self.admin.get("payroll/org/work-weeks")}
        weeks = {b[6][0]: b[6] for b in self.spec["branches"]}
        for code, (code_, name, offs, nth) in weeks.items():
            row = rows.get(code)
            if row is None:
                body = {"code": code, "name": name, "weekly_off_days": offs}
                if nth:
                    body["nth_weekday_offs"] = nth
                row = self.admin.post("payroll/org/work-weeks", body)
                self.ctx.created(f"work week {code}")
            else:
                self.ctx.existing()
            self.ids["work_weeks"][code] = row["id"]

    def ensure_branches(self) -> None:
        branches = self.admin.get("branches")
        by_code = {b["code"]: b for b in branches}
        for index, (bkey, entity, name, code, region, tz, week) in enumerate(self.spec["branches"]):
            row = by_code.get(code)
            if row is None and index == 0:
                starter = next((b for b in branches if b["code"] == "B01"), None)
                if starter is not None:
                    row = self.admin.patch(f"branches/{starter['id']}", {
                        "name": name, "code": code, "subdivision_code": region, "timezone": tz})
                    self.ctx.created(f"branch {code} (renamed starter branch)")
            if row is None:
                row = self.admin.post("branches", {"legal_entity_id": self.ids["entities"][entity], "code": code,
                                                   "name": name, "subdivision_code": region, "timezone": tz})
                self.ctx.created(f"branch {code}")
            else:
                self.ctx.existing()
            self.ids["branches"][bkey] = row["id"]
            self.admin.put(f"payroll/org/branches/{row['id']}/calendar",
                           {"work_week_pattern_id": self.ids["work_weeks"][week[0]]})

    def ensure_departments(self) -> None:
        rows = {d["name"]: d for d in items_of(self.admin.get("departments"))}
        for name in self.spec["departments"]:
            if name not in rows:
                rows[name] = self.admin.post("departments", {"name": name})
                self.ctx.created(f"department {name}")
            else:
                self.ctx.existing()
        self.ids["departments"] = {name: row["id"] for name, row in rows.items()}

    def ensure_shifts(self) -> None:
        if GOALS.index(self.ctx.store["goal"]) >= GOALS.index("G-04"):
            self.ensure_cast_shifts()
            return
        rows = {s["name"]: s for s in self.admin.get("shifts")}
        for name, start, end, hours, unpaid, flexible, description in self.spec["shifts"]:
            body = {"name": name, "start_time": start, "end_time": end, "duration_hours": hours,
                    "is_flexible": flexible, "description": description}
            row = rows.get(name)
            if row is None:
                # The unpaid break goes with the create: from AS-M1 a legacy edit takes effect the next day (A-24).
                created = self.admin.post("shifts", dict(body, unpaid_break_minutes=unpaid) if unpaid else body)
                row = {"id": created["id"], "name": name, "unpaid_break_minutes": unpaid or None}
                self.ctx.created(f"shift {name}")
            else:
                self.ctx.existing()
            self.ids["shifts"][name] = row["id"]
            if unpaid:
                self._ensure_unpaid_break(row, body, unpaid)

    def ensure_cast_shifts(self) -> None:
        """G-04: codes, kinds, owners, break rules and anchored zones (03-shifts/01 §2.4), version 1 from 2000-01-01
        so history resolves; GEN is the signup shift (BUG-AS-33)."""
        rows = {s["code"]: s for s in items_of(self.admin.get("shifts", status="ALL", limit=200))}
        if "GEN" in rows:
            self.ids["shifts"]["GEN"] = rows["GEN"]["id"]
        for code, (name, kind, branch, version) in CAST_SHIFTS.get(self.key, {}).items():
            row = rows.get(code)
            if row is None:
                row = self.admin.post("shifts", {
                    "code": code, "name": name, "kind": kind, "branch_id": self.ids["branches"][branch],
                    "version": {"effective_from": "2000-01-01", "reason": "Time-stack cast (G-04)", **version}},
                    key=f"time-seed-shift-{self.key}-{code}")
                self.ctx.created(f"shift {code}")
            else:
                self.ctx.existing()
            self.ids["shifts"][code] = row["id"]

    def _ensure_unpaid_break(self, row: dict, body: dict, minutes: int) -> None:
        """The unpaid break is writable from BUG-AS-22 (G-01) on; older code refuses the field (recorded)."""
        if row.get("unpaid_break_minutes") == minutes:
            self.ctx.existing()
            return
        try:
            self.admin.put(f"shifts/{row['id']}", dict(body, unpaid_break_minutes=minutes))
            self.ctx.created(f"unpaid break of shift {body['name']}")
        except SeedError as error:
            if error.status != 422:
                raise
            self.ctx.block(self.spec["name"], f"unpaid break of shift {body['name']}", error)

    def ensure_roles(self) -> None:
        roles = self.admin.get("roles")
        self.ids["roles"] = {r["system_key"]: r["id"] for r in roles if r.get("system_key")}
        if GOALS.index(self.ctx.store["goal"]) >= GOALS.index("G-03") and self.spec.get("work_calendar_admins"):
            name, description, grants = WORK_CALENDAR_ADMIN
            row = next((r for r in roles if r.get("name") == name), None)
            if row is None:
                row = self.admin.post("roles", {"name": name, "description": description, "grants": [
                    {"permission": code, "scope": scope} for code, scope in grants]})
                self.ctx.created(f"role {name}")
            else:
                self.ctx.existing()
            self.ids["roles"][WORK_CALENDAR_ADMIN_KEY] = row["id"]

    def _find(self, path: str, address: str, **params) -> Optional[dict]:
        found = items_of(self.admin.get(path, q=address, limit=50, **params))
        return next((row for row in found if (row.get("email") or "").lower() == address), None)

    def ensure_people(self) -> None:
        for index, (pkey, first, last, roles, entity, branch, dept, manager, shift, position, extras) in \
                enumerate(self.spec["people"], start=1):
            address = self.email_of(first, last)
            employee = self._find("employees", address, include_inactive="true")
            if employee is None:
                body = {"first_name": first, "last_name": last, "email": address,
                        "employee_code": f"{self.key[0].upper()}T-{index:03d}", "hire_date": HIRED.isoformat(),
                        "position": position, "legal_entity_id": self.ids["entities"][entity],
                        "branch_id": self.ids["branches"][branch], "department_id": self.ids["departments"][dept]}
                if manager:
                    body["manager_id"] = self.ids["people"][manager]["employee_id"]
                if shift:
                    body["shift_id"] = self.ids["shifts"][shift]
                employee = self.admin.post("employees", body)
                self.ctx.created(f"employee {first} {last}")
            else:
                self.ctx.existing()
            self.ids["people"][pkey] = {"employee_id": employee["id"], "email": address, "name": f"{first} {last}",
                                        "roles": roles, "shift": shift, "branch": branch, "extras": extras}
            self._ensure_login(pkey, address, f"{first} {last}", employee["id"], roles)

    def _ensure_login(self, pkey: str, address: str, name: str, employee_id: int, roles: List[str]) -> None:
        keys = sorted({ROLE_MAP.get(r, r) for r in roles})
        role_ids = sorted(self.ids["roles"][k] for k in keys)
        if pkey in self.spec.get("work_calendar_admins", ()) and WORK_CALENDAR_ADMIN_KEY in self.ids["roles"]:
            role_ids = sorted(role_ids + [self.ids["roles"][WORK_CALENDAR_ADMIN_KEY]])
            keys = sorted(keys + [WORK_CALENDAR_ADMIN_KEY])
        user = self._find("users", address)
        account = self.accounts.get(pkey)
        if user is None or user.get("status") == "invited":
            password = (account or {}).get("password") or self.ctx.new_password()
            if user is None:
                invite = self.admin.post("users/invite", {"email": address, "full_name": name,
                                                          "employee_id": employee_id, "role_ids": role_ids})
            else:
                invite = self.admin.post(f"users/{user['id']}/resend-invite")
            token = invite["invite_url"].split("token=")[1]
            if self.ctx.before_login:
                self.ctx.before_login()
            api = Api(self.ctx.make_client(), address)
            accepted = api.post("auth/invitations/accept", {"token": token, "password": password})
            self.accounts[pkey] = {"email": address, "password": password, "name": name, "roles": roles,
                                   "system_roles": keys}
            self.sessions[pkey] = api
            user_id = accepted["me"]["user"]["id"]
            self.ctx.created(f"login {address} ({', '.join(keys)})")
        else:
            user_id = user["id"]
            if not account:
                raise RuntimeError(f"{address} exists but its password is not in the credential file; reset the stack")
            account.update(roles=roles, system_roles=keys)
            have = sorted(r["id"] for r in self.admin.get(f"users/{user_id}").get("roles", []))
            if have != role_ids:
                self.admin.put(f"users/{user_id}/roles", {"role_ids": role_ids})
                self.ctx.created(f"roles of {address}")
            else:
                self.ctx.existing()
        self.ids["people"][pkey]["user_id"] = user_id

    # ---------------------------------------------------------------- calendars, leave, attendance
    def ensure_holidays(self) -> None:
        have = {(h["name"], str(h["date"])[:10]) for h in items_of(self.admin.get("holidays"))}
        for name, day, location, optional in self.spec["holidays"]:
            if (name, day) in have:
                self.ctx.existing()
                continue
            self.admin.post("holidays", {"name": name, "date": day, "location": location, "is_optional": optional})
            self.ctx.created(f"holiday {name} {day}")

    def _approver_for(self, leave: dict) -> Optional[str]:
        """The cast member who can act on a pending request now (the manager, then HR)."""
        for pkey in ("manager", "hr"):
            if pkey == "manager":
                person = next((p for p in self.spec["people"] if p[0] == leave["_who"]), None)
                candidate = person[7] if person else None
            else:
                candidate = next((p[0] for p in self.spec["people"] if "hr_admin" in p[3]), None)
            if candidate and candidate in self.accounts:
                inbox = self.session(candidate).get("leaves/approvals")
                if any(item.get("id") == leave["id"] for item in items_of(inbox)):
                    return candidate
        return None

    def ensure_leaves(self) -> None:
        for who, kind, start, end, start_portion, end_portion, outcome in self.spec["leaves"]:
            api = self.session(who)
            mine = items_of(api.get("leaves/my"))
            row = next((l for l in mine if l.get("leave_type") == kind and str(l.get("start_date"))[:10] == start
                        and str(l.get("end_date"))[:10] == end), None)
            if row is None:
                row = api.post("leaves/apply", {"leave_type": kind, "start_date": start, "end_date": end,
                                                "start_portion": start_portion, "end_portion": end_portion,
                                                "reason": f"Seeded {kind} leave ({TAG})"})
                self.ctx.created(f"leave {who} {kind} {start}")
            else:
                self.ctx.existing()
            row["_who"] = who
            for _ in range(3):  # a request may need the manager and then HR
                if row.get("status") != "pending" or outcome in ("pending", "cancel"):
                    break
                approver = self._approver_for(row)
                if approver is None:
                    self.ctx.block(self.spec["name"], f"decide leave of {who} {start}",
                                   RuntimeError("no cast approver has the request in their inbox"))
                    break
                action = "approve" if outcome == "approve" else "reject"
                decided = self.session(approver).post(f"leaves/{row['id']}/{action}", {"remarks": f"Seeded ({TAG})"})
                decided["_who"] = who
                row = decided
                self.ctx.created(f"leave {who} {start} {action} by {approver}")
            if outcome == "cancel" and row.get("status") == "pending":
                api.post(f"leaves/{row['id']}/cancel")
                self.ctx.created(f"leave {who} {start} cancelled")

    def ensure_attendance(self) -> None:
        recorder_key = self.spec["attendance_by"]
        recorder = self.admin if recorder_key == "founder" else self.session(recorder_key)
        tz = self.spec["timezone"]
        for who, days, check_in, check_out, status, lunch in self.spec["attendance"]:
            person = self.ids["people"][who]
            dates = HISTORY_WEEKDAYS if days == "weekdays" else [date.fromisoformat(d) for d in days]
            listed = recorder.get("attendance", start_date=dates[0].isoformat(), end_date=dates[-1].isoformat(),
                                  employee_id=person["employee_id"], limit=500)
            have = {str(r["date"])[:10] for r in items_of(listed)}
            for day in dates:
                if day.isoformat() in have:
                    self.ctx.existing()
                    continue
                body = {"employee_id": person["employee_id"], "date": day.isoformat(),
                        "check_in_time": local_iso(day, check_in, tz), "status": status,
                        "notes": f"Seeded attendance ({TAG})"}
                if check_out:
                    body["check_out_time"] = local_iso(day, check_out, tz)
                if lunch:
                    start, minutes = lunch
                    lunch_day = day + timedelta(days=1) if check_out and check_out.startswith("+1") \
                        and start < check_in else day
                    begin = local_iso(lunch_day, start, tz)
                    finish = (datetime.fromisoformat(begin) + timedelta(minutes=minutes)).isoformat()
                    body["breaks"] = [{"started_at": begin, "ended_at": finish}]
                recorder.post("attendance/manual", body)
                self.ctx.created(f"attendance {who} {day}")

    def ensure_overtime(self) -> None:
        """G-02: pending overtime entries (Oct 12-16 2026) for the overtime.entries pilot list."""
        days = [date(2026, 10, d) for d in (12, 13, 14, 15, 16)]
        for who, manager, minutes in self.spec.get("overtime", []):
            person = self.ids["people"][who]
            api = self.session(manager)
            listed = items_of(api.get("overtime/entries", employee_id=person["employee_id"], limit=200))
            have = {str(e.get("date"))[:10] for e in listed}
            for day in days:
                if day.isoformat() in have:
                    self.ctx.existing()
                    continue
                api.post("overtime/entries", {"employee_id": person["employee_id"], "date": day.isoformat(),
                                              "minutes": minutes, "reason": f"Release support ({TAG})",
                                              "comp_off": False}, key=f"time-seed-ot-{who}-{day}")
                self.ctx.created(f"overtime {who} {day}")

    # ---------------------------------------------------------------- run
    def run(self) -> None:
        self.ctx.log(f"[time-seed] {self.spec['name']}")
        self.ensure_founder()
        self.ensure_entities()
        self.ensure_work_weeks()
        self.ensure_branches()
        self.ensure_departments()
        self.ensure_shifts()
        self.ensure_roles()
        self.ensure_people()
        self.ensure_holidays()
        self.optional("leave requests", self.ensure_leaves)
        self.optional("attendance history", self.ensure_attendance)
        if GOALS.index(self.ctx.store["goal"]) >= GOALS.index("G-02"):
            self.optional("overtime entries", self.ensure_overtime)
        self.state["ids"] = self.ids


def run_seed(make_client: Callable[[], Any], *, store: dict, password: Optional[str] = None,
             before_login: Optional[Callable[[], None]] = None, companies: Optional[List[str]] = None,
             goal: str = GOALS[-1], log: Callable[[str], None] = print) -> Context:
    """Seed (or re-check) the dataset up to ``goal``; returns the context with stats and blocked steps."""
    if goal not in GOALS:
        raise ValueError(f"unknown goal {goal}; this seed knows {', '.join(GOALS)}")
    ctx = Context(make_client=make_client, store=store, password=password, before_login=before_login, log=log)
    store.setdefault("dataset_version", DATASET_VERSION)
    store["goal"] = goal
    store["anchors"] = {"today": TODAY.isoformat(), "hired": HIRED.isoformat()}
    for key in companies or list(COMPANIES):
        CompanySeed(ctx, key, COMPANIES[key]).run()
    store["blocked"] = ctx.blocked
    store["last_run_stats"] = dict(ctx.stats)
    return ctx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://localhost:8500/api/v1")
    parser.add_argument("--goal", default=GOALS[-1], choices=GOALS)
    parser.add_argument("--company", action="append", choices=list(COMPANIES))
    parser.add_argument("--credentials", default=str(CREDENTIALS_JSON))
    args = parser.parse_args()
    if any(f":{port}" in args.api for port in (8000, 8100, 8200, 8300, 8400)):
        sys.exit("That port belongs to another stack; the time stack is 8500.")
    import httpx

    try:
        httpx.get(args.api.rsplit("/api/", 1)[0] + "/health", timeout=10).raise_for_status()
    except Exception:  # noqa: BLE001
        sys.exit("The API is not reachable. Start it first: python scripts/demo_stack.py --fresh "
                 "--data-dir .local-postgres-time --port 8500")
    path = Path(args.credentials)
    store = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    store["api"] = args.api
    try:
        ctx = run_seed(http_client_factory(args.api), store=store, companies=args.company, goal=args.goal)
    finally:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(store, indent=2, default=str), encoding="utf-8")  # never lose issued passwords
    print(f"[time-seed] done: {ctx.stats['created']} created, {ctx.stats['existing']} already present, "
          f"{len(ctx.blocked)} blocked. Credentials: {path} (git-ignored).")


if __name__ == "__main__":
    main()

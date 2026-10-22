"""Seed the deterministic three-company payroll dataset (Goal 2 §4-§5; non-production databases only).

    python scripts/payroll_stack.py start
    python scripts/seed_payroll_three_companies.py --api http://localhost:8400/api/v1
    python scripts/verify_payroll_seed.py            # read-only database assertions
    python scripts/payroll_stack.py stop

Everything goes through the public API as each company's own users (and the platform operator for the
subscription plan), so the seed obeys the same validation, permissions, separation of duties, step-up and
maker-checker rules as the product. It is idempotent: every object is looked up by its natural key (email,
employee code, entity/pay-group/structure/component code, external reference, dates) before it is created,
and a second run changes nothing. Accounts get one random password each, stored only in the git-ignored
``.local-secrets/payroll-credentials.json`` (and never printed); a readable guide is written to
``.local-secrets/payroll-credentials.md``. Dates are anchored on the first run (``anchors`` in that file),
so later runs reuse them. To start over: ``python scripts/payroll_stack.py reset --start`` and delete the
two credential files.

Companies (all names, e-mail domains, bank details and registration numbers are fictitious test data):

* **Alpha Technologies** ("Alpha People", IN, INR, Asia/Kolkata): entity IN01 (Delhi IN-DL, Bengaluru IN-KA;
  pay groups Monthly (primary) and Weekly; Mon-Sat week) and a second Indian entity IN02 (Hyderabad) with its
  own pay group; CTC staff structure and an hourly weekly structure; attendance, leave/LOP, overtime and
  salary advances ON. Focus: attendance, leave, LOP, overtime, revisions, the IN fixture pack (development
  only, badged "FIXTURE - not law").
* **Beta Logistics** ("Beta Workforce", AE, AED, Asia/Dubai): Monthly AED pay group, Mon-Fri week, Dubai and
  Abu Dhabi branches; gross structures with different allowances, variable trip pay, holiday and overtime
  policies whose multipliers are company policy values entered by the administrator (never statutory facts),
  a verified bank account and a pending bank change (fictitious, checksum-valid test IBANs), an approved
  reimbursement, a loan with repayments, a manager without compensation visibility, and the I-15 statutory
  attestation made by one administrator and approved by another (AE stays CONFIG_ONLY).
* **Gamma Solutions** ("Gamma HR Cloud", US, USD, America/New_York): Bi-weekly USD pay group, Mon-Fri week,
  New York (US-NY) and California (US-CA); salaried and hourly structures, tasks with a rate card, commission
  and bonus inputs, a reimbursement, approved overtime, a mid-period joiner and leaver, and a backdated
  salary correction (retro) after the finalized period.

Each company: Company Administrator, HR Administrator, Payroll Processor, Payroll Approver, Finance &
Payments, Department Manager, Team Lead and Employee accounts plus denial-only accounts (an employee of
another department, a manager of another team, and for Alpha a payroll processor scoped to entity IN02).
Scopes use the role infrastructure (custom roles with LEGAL_ENTITY / DIRECT_REPORTS grants and scope
targets); nothing relies on hardcoded role names.

Run lifecycle (last): processor creates, pre-checks and calculates the regular run of the current period;
the processor submits; the independent approver approves; the payroll manager (the company administrator)
steps up with the e-mailed code (read from the development outbox) and finalizes. An open draft run is left
for UI testing. Whatever API is missing or refuses is recorded under ``blocked`` (endpoint and error code)
in the credential file and the guide, never hidden.
"""

from __future__ import annotations

import argparse
import base64
import email
import json
import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email import policy as email_policy
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
SECRETS_DIR = ROOT / ".local-secrets"
CREDENTIALS_JSON = SECRETS_DIR / "payroll-credentials.json"
CREDENTIALS_MD = SECRETS_DIR / "payroll-credentials.md"
DEFAULT_OUTBOX = ROOT / "logs" / "outbox-payroll"
PLATFORM_EMAIL = "ops@hrms-platform.io"
TAG = "PAYROLL-SEED"
DATASET_VERSION = 1

# ---------------------------------------------------------------------------------------------- roles
# Custom roles (permission codes of app/core/permission_registry.py; scopes are the registry's scopes).
TEAM_LEAD_GRANTS = [("employee:read", "DIRECT_REPORTS"), ("attendance:read", "DIRECT_REPORTS"),
                    ("leave:read", "DIRECT_REPORTS"), ("leave:approve", "DIRECT_REPORTS"),
                    ("overtime:approve", "DIRECT_REPORTS"), ("task:approve", "DIRECT_REPORTS"),
                    ("report:read", "DIRECT_REPORTS")]
SCOPED_PROCESSOR_CODES = ("payroll:run:read", "payroll:run:create", "payroll:run:calculate", "payroll:run:review",
                          "payroll:inputs:read", "payroll:inputs:edit", "payroll:compensation:read",
                          "payroll:payslip:read", "payroll:reports:read", "payroll:configuration:read")
COMPLIANCE_REVIEWER = [("payroll:statutory:review", "COMPANY")]

# What each account may do and see (the readable guide; the verification suites assert the real behaviour).
EXPECTED = {
    "company_admin": "Company settings, roles, branding; payroll through the Payroll Manager role granted at signup "
                     "(I-08): configuration, inputs, runs, finalize (step-up), payslips, statutory review. Cannot "
                     "approve a run they calculated (separation of duties).",
    "hr_admin": "Employees, org structure, attendance, leave, holidays, users and roles; no payroll amounts or bank "
                "data beyond their own (no payroll role).",
    "payroll_processor": "Payroll inputs, compensation proposals, run create / pre-check / calculate / review / submit, "
                         "loans and claims; cannot approve runs or release payments.",
    "payroll_approver": "Approves runs, compensation, structures and bank-detail changes prepared by others; never "
                        "their own pay line or anything they prepared.",
    "finance_payments": "Payment batches, release (step-up, dual control), reconciliation, funding accounts; masked bank "
                        "details only; no salary editing.",
    "manager": "Reporting tree: employees, attendance, leave, overtime and task approvals; no compensation or "
               "payslips of others (no payroll grants).",
    "Team Lead": "Direct reports only: employee, attendance, leave, overtime and task approval; no pay amounts.",
    "employee": "Self-service: own profile, attendance, leave, payslips, compensation, bank accounts, loans, claims.",
    "Payroll Processor (IN02 only)": "Payroll processing limited to legal entity IN02 (scope target); IN01 runs, "
                                     "pay groups and employees answer 404.",
    "Compliance Reviewer": "payroll:statutory:review (company): approves the statutory attestation another "
                           "administrator made (I-15 maker-checker).",
}


def _d(value: str) -> date:
    return date.fromisoformat(value)


def _iso(value: date) -> str:
    return value.isoformat()


# ---------------------------------------------------------------------------------------------- test bank data
def test_iban_ae(account: str) -> str:
    """A checksum-valid UAE IBAN with the fictitious bank code 999 (no real bank): AEkk 999 + 16 digits."""
    bban = "999" + account.rjust(16, "0")[-16:]
    digits = "".join(str(int(ch, 36)) for ch in bban + "AE00")
    check = 98 - int(digits) % 97
    return f"AE{check:02d}{bban}"


TEST_IFSC = "TEST0000001"  # IFSC format (4 letters, 0, 6 alphanumerics); "TEST" is no real bank code
TEST_ABA = "123456780"  # ABA checksum-valid documentation/test routing number


# ---------------------------------------------------------------------------------------------- HTTP
class SeedError(Exception):
    """An API call failed; the message carries method, path, status and the API error code (never the body)."""

    def __init__(self, method: str, path: str, status: int, text: str):
        self.method, self.path, self.status = method, path, status
        data: Any = None
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            pass
        self.code = data.get("code") if isinstance(data, dict) else None
        detail = data.get("detail") if isinstance(data, dict) else None
        if isinstance(detail, dict):
            self.code = self.code or detail.get("code")
            detail = detail.get("message") or detail
        self.detail = str(detail if detail is not None else (text or ""))[:400]
        super().__init__(f"{method} {self.endpoint} -> {status} {self.code or ''} {self.detail}")

    @property
    def endpoint(self) -> str:
        path = "/" + self.path.strip("/").split("?")[0]
        return "/api/v1" + re.sub(r"/\d+(?=/|$)", "/{id}", path)


class Api:
    """One signed-in browser-like session (cookies + CSRF header) over an httpx-compatible client."""

    def __init__(self, client, who: str = "anonymous"):
        self.client = client
        self.who = who

    def request(self, method: str, path: str, *, json_body: Any = None, params: Optional[dict] = None,
                headers: Optional[dict] = None, files: Any = None, data: Any = None) -> Any:
        merged = dict(headers or {})
        if method.upper() not in ("GET", "HEAD"):
            merged.setdefault("X-CSRF-Token", self.client.cookies.get("hrms_csrf", "") or "")
        kwargs: Dict[str, Any] = {"headers": merged}
        if json_body is not None:
            kwargs["json"] = json_body
        if params:
            kwargs["params"] = params
        if files is not None:
            kwargs["files"] = files
        if data is not None:
            kwargs["data"] = data
        response = self.client.request(method, path.lstrip("/"), **kwargs)
        if response.status_code >= 400:
            raise SeedError(method, path, response.status_code, response.text)
        if response.status_code == 204 or not response.content:
            return None
        if "json" in response.headers.get("content-type", ""):
            return response.json()
        return response.content

    def get(self, path, **params):
        return self.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path, body=None, *, key: Optional[str] = None, **kw):
        headers = {"Idempotency-Key": key} if key else None
        return self.request("POST", path, json_body=body, headers=headers, **kw)

    def put(self, path, body=None):
        return self.request("PUT", path, json_body=body)

    def patch(self, path, body=None):
        return self.request("PATCH", path, json_body=body)


def http_client_factory(api_base: str) -> Callable[[], Any]:
    import httpx

    base = api_base.rstrip("/") + "/"
    return lambda: httpx.Client(base_url=base, timeout=240)


def items_of(value: Any) -> list:
    if isinstance(value, dict):
        return list(value.get("items") or [])
    return list(value or [])


def new_password() -> str:
    return "Pw-" + secrets.token_urlsafe(18) + "-7k"


def money(amount: str, currency: str) -> dict:
    return {"amount": amount, "currency": currency}


# ---------------------------------------------------------------------------------------------- specs
# Person tuple fields: key, first, last, account roles (system keys or custom role names; [] = no login),
# branch key, department, manager key, position, structure key (None = no salary), pay basis (annual CTC for
# CTC_BASED, monthly gross for GROSS_BASED, hourly rate for HOURLY), extras.
COMPANIES: Dict[str, dict] = {
    "alpha": {
        "name": "Alpha Technologies", "timezone": "Asia/Kolkata", "currency": "INR", "country": "IN",
        "domain": "alpha-tech.example", "admin": ("Aarav", "Mehta"), "prefix": "AT", "plan": "enterprise",
        "entity": {"code": "IN01", "legal_name": "Alpha Technologies Pvt Ltd", "country_code": "IN",
                   "base_currency": "INR", "timezone": "Asia/Kolkata", "confirm_country": True,
                   "registered_address": {"line1": "14 Test Residency Road (fictitious)", "city": "Bengaluru",
                                          "subdivision_code": "IN-KA", "postal_code": "560025"},
                   "registration_lines": [{"label": "CIN (test)", "value": "U00000KA2018PTC000001"}]},
        "extra_entities": [{"code": "IN02", "legal_name": "Alpha Technologies Services LLP", "country_code": "IN",
                            "base_currency": "INR", "timezone": "Asia/Kolkata", "confirm_country": True,
                            "registered_address": {"line1": "2 Test Hitech City Road (fictitious)",
                                                   "city": "Hyderabad", "subdivision_code": "IN-TS"}}],
        # key, entity code, name, branch code, subdivision, timezone, work week code
        "branches": [("del", "IN01", "Delhi", "DEL", "IN-DL", "Asia/Kolkata", "MON_SAT"),
                     ("blr", "IN01", "Bengaluru", "BLR", "IN-KA", "Asia/Kolkata", "MON_SAT"),
                     ("hyd", "IN02", "Hyderabad", "HYD", "IN-TS", "Asia/Kolkata", "MON_SAT")],
        "work_weeks": [("MON_SAT", "Monday to Saturday", [6])],
        # key, entity, code, name, currency, frequency, rules, primary
        "pay_groups": [("monthly", "IN01", "MONTHLY", "Monthly staff", "INR", "MONTHLY",
                        {"monthly_start_day": 1, "pay_day_rule": "LAST_WORKING_DAY"}, True),
                       ("weekly", "IN01", "WEEKLY", "Weekly hourly crew", "INR", "WEEKLY",
                        {"week_start_day": "MON", "pay_day_rule": "DAYS_AFTER_PERIOD_END", "pay_day_value": 5}, False),
                       ("svc", "IN02", "SVC_MONTHLY", "Services monthly", "INR", "MONTHLY",
                        {"monthly_start_day": 1, "pay_day_rule": "LAST_WORKING_DAY"}, False)],
        "departments": ["Engineering", "Operations", "HR", "Finance", "Services"],
        "teams": [("Platform Team", "Engineering"), ("Field Ops Team", "Operations"), ("Delivery Team", "Services")],
        "admin_place": ("Finance", "Managing Director"),
        "components": [
            {"code": "HRA", "kind": "EARNING", "category": "FIXED_ALLOWANCE", "name": "House rent allowance",
             "calc_method": "PERCENT_OF", "params": {"percent": "40", "bases": ["BASIC"]},
             "tags": ["LOP_APPLICABLE", "PART_OF_CTC", "PART_OF_GROSS"], "proration": "BY_PAID_DAYS"},
            {"code": "HOURLY_WAGE", "kind": "EARNING", "category": "BASE", "name": "Hourly wage",
             "calc_method": "RATE_X_UNITS", "params": {"rate": "1", "units": "att.payable_hours"},
             "tags": ["PART_OF_GROSS", "OT_BASE"], "proration": "NONE"},
        ],
        "structures": [
            {"key": "ctc", "code": "STAFF_CTC", "name": "Staff CTC (India)", "type": "CTC_BASED",
             "lines": [{"component": "BASIC", "method": "FORMULA", "formula": "ctc.monthly * 50%"},
                       {"component": "HRA", "method": "PERCENT_OF", "value": "40", "params": {"bases": ["BASIC"]}},
                       {"component": "FIXED_ALLOWANCE", "method": "BALANCING"}],
             "test_range": {"min": "200000", "max": "6000000"}},
            {"key": "hourly", "code": "HOURLY_CREW", "name": "Hourly crew (weekly)", "type": "HOURLY",
             # Units written out: the assignment preview runs with attendance disabled, so a bare
             # att.payable_hours is NULL there and the assignment is refused (reported defect).
             "lines": [{"component": "HOURLY_WAGE", "method": "RATE_X_UNITS",
                        "params": {"units": "COALESCE(att.payable_hours, 0)"}}]},
        ],
        "custom_roles": {"Team Lead": TEAM_LEAD_GRANTS,
                         "Payroll Processor (IN02 only)": [(c, "LEGAL_ENTITY") for c in SCOPED_PROCESSOR_CODES]},
        "people": [
            ("hr", "Neha", "Sharma", ["hr_admin"], "del", "HR", "admin", "HR Administrator", "ctc", "1680000", {}),
            ("processor", "Karan", "Malhotra", ["payroll_processor", "employee"], "del", "Finance", "admin",
             "Payroll Processor", "ctc", "1440000", {}),
            ("approver", "Ritu", "Bansal", ["payroll_approver", "employee"], "del", "Finance", "admin",
             "Payroll Approver", None, None, {"no_salary_reason": "approver"}),
            ("finance", "Imran", "Qureshi", ["finance_payments", "employee"], "del", "Finance", "admin",
             "Finance & Payments Officer", "ctc", "1320000", {}),
            ("deptmgr", "Vikram", "Nair", ["manager"], "blr", "Engineering", "admin", "Engineering Manager", "ctc",
             "2520000", {}),
            ("teamlead", "Sneha", "Kapoor", ["employee", "Team Lead"], "blr", "Platform Team", "deptmgr",
             "Team Lead", "ctc", "1800000", {}),
            ("emp1", "Rohan", "Gupta", ["employee"], "blr", "Platform Team", "teamlead", "Software Engineer", "ctc",
             "1200000", {"leave": True, "overtime": True}),
            ("emp2", "Priya", "Iyer", ["employee"], "blr", "Platform Team", "teamlead", "Software Engineer (part-time)",
             "ctc", "600000", {"fte": "0.5"}),
            ("opsmgr", "Anil", "Verma", ["manager"], "del", "Operations", "admin", "Operations Manager", "ctc",
             "1920000", {}),
            ("emp3", "Arjun", "Singh", ["employee"], "del", "Field Ops Team", "opsmgr", "Operations Analyst", "ctc",
             "840000", {"joiner": 9}),
            ("emp4", "Meera", "Joshi", ["employee"], "del", "Field Ops Team", "opsmgr", "Field Coordinator", "ctc",
             "720000", {"advance": "15000.00", "lop_day": True}),
            ("emp5", "Kabir", "Das", ["employee"], "del", "Operations", "opsmgr", "Operations Trainee", None, None,
             {}),
            ("crew1", "Suresh", "Kumar", [], "del", "Field Ops Team", "opsmgr", "Warehouse Crew (hourly)", "hourly",
             "250.00", {"pay_group": "weekly", "attendance_hours": True}),
            ("svcproc", "Pooja", "Reddy", ["Payroll Processor (IN02 only)", "employee"], "hyd", "Delivery Team",
             "admin", "Payroll Officer (IN02)", "ctc", "960000", {"pay_group": "svc", "scope_entity": "IN02"}),
            ("svc1", "Farhan", "Ali", ["employee"], "hyd", "Delivery Team", "svcproc", "Delivery Consultant", "ctc",
             "1080000", {"pay_group": "svc"}),
        ],
        "accounts": {  # guide label -> person key (the admin is "admin")
            "Company Administrator": "admin", "HR Administrator": "hr", "Payroll Processor": "processor",
            "Payroll Approver": "approver", "Finance & Payments": "finance", "Department Manager": "deptmgr",
            "Team Lead": "teamlead", "Employee": "emp1",
            "Denial: employee of another department": "emp5", "Denial: manager of another team": "opsmgr",
            "Denial: payroll processor scoped to IN02": "svcproc",
        },
        "dept_heads": {"Engineering": "deptmgr", "Operations": "opsmgr", "HR": "hr", "Finance": "finance",
                       "Services": "svcproc"},
        "holidays": [("Gandhi Jayanti", "2026-10-02", None), ("Diwali", "2026-11-08", None),
                     ("Karnataka Rajyotsava", "2026-11-01", "Bengaluru")],
        "settings": {"payroll.overtime.enabled": True, "payroll.advances.enabled": True,
                     "payroll.deductions.min_net_pay_percent": "50"},
        # The IN fixture pack (development/test only) makes income-tax withholding mandatory per entity.
        "entity_settings": {"IN01": {"payroll.statutory.IN.tds.enabled": True},
                            "IN02": {"payroll.statutory.IN.tds.enabled": True,
                                     "payroll.statutory.IN.pt.enabled": True}},
        "policies": {
            "ATTENDANCE_PAY_POLICY": {"unmarked_day": "PRESENT", "missing_punch_out": "EXCEPTION"},
            "OVERTIME_POLICY": {"buckets": [{"day_type": "WORKDAY", "multiplier": "1.5", "after_hours": "0"},
                                            {"day_type": "WEEKLY_OFF", "multiplier": "2", "after_hours": "0"},
                                            {"day_type": "HOLIDAY", "multiplier": "2", "after_hours": "0"}]},
            "ADVANCE_POLICY": {},
        },
        "brand": {"product_name": "Alpha People", "short_name": "Alpha", "tagline": "People first, at Alpha",
                  "primary_color": "#4338CA", "accent_color": "#6366F1", "sidebar_style": "LIGHT",
                  "default_locale": "en-IN", "show_powered_by": False,
                  "legal_footer": "Alpha Technologies Pvt Ltd (fictitious test company)",
                  "support_email": "people@alpha-tech.example"},
        "finalize_group": "monthly", "draft_group": "weekly", "statutory_attestation": False,
        "bank": {"country": "IN", "currency": "INR", "people": ["emp1"]},
    },
    "beta": {
        "name": "Beta Logistics", "timezone": "Asia/Dubai", "currency": "AED", "country": "AE",
        "domain": "beta-logistics.example", "admin": ("Omar", "Al-Farsi"), "prefix": "BL", "plan": "enterprise",
        "entity": {"code": "AE01", "legal_name": "Beta Logistics LLC", "country_code": "AE", "base_currency": "AED",
                   "timezone": "Asia/Dubai", "confirm_country": True,
                   "registered_address": {"line1": "Test Free Zone, Plot T-0001 (fictitious)", "city": "Dubai",
                                          "subdivision_code": "AE-DU"},
                   "registration_lines": [{"label": "Trade licence (test)", "value": "TEST-000001"}]},
        "extra_entities": [],
        "branches": [("dxb", "AE01", "Dubai", "DXB", "AE-DU", "Asia/Dubai", None),
                     ("auh", "AE01", "Abu Dhabi", "AUH", "AE-AZ", "Asia/Dubai", None)],
        "work_weeks": [],
        "pay_groups": [("monthly", "AE01", "MONTHLY_AED", "Monthly (AED)", "AED", "MONTHLY",
                        {"monthly_start_day": 1, "pay_day_rule": "LAST_WORKING_DAY"}, True)],
        "departments": ["Operations", "Fleet", "HR", "Finance"],
        "teams": [("Dispatch Team", "Fleet"), ("Warehouse Team", "Operations")],
        "admin_place": ("Operations", "General Manager"),
        "components": [
            {"code": "HOUSING_ALLOWANCE", "kind": "EARNING", "category": "FIXED_ALLOWANCE", "name": "Housing allowance",
             "calc_method": "FIXED", "tags": ["LOP_APPLICABLE", "PART_OF_GROSS"], "proration": "BY_PAID_DAYS"},
            {"code": "TRANSPORT_ALLOWANCE", "kind": "EARNING", "category": "FIXED_ALLOWANCE",
             "name": "Transport allowance", "calc_method": "FIXED", "tags": ["LOP_APPLICABLE", "PART_OF_GROSS"],
             "proration": "BY_PAID_DAYS"},
            {"code": "FOOD_ALLOWANCE", "kind": "EARNING", "category": "FIXED_ALLOWANCE", "name": "Food allowance",
             "calc_method": "FIXED", "tags": ["PART_OF_GROSS"], "proration": "BY_PAID_DAYS"},
            {"code": "TRIP_PAY", "kind": "EARNING", "category": "VARIABLE", "name": "Trip pay (variable)",
             "calc_method": "INPUT", "tags": ["PART_OF_GROSS"], "proration": "NONE"},
        ],
        "structures": [
            {"key": "office", "code": "OFFICE_GROSS", "name": "Office staff (gross)", "type": "GROSS_BASED",
             "lines": [{"component": "BASIC", "method": "FIXED", "overridable": True},
                       {"component": "HOUSING_ALLOWANCE", "method": "FIXED", "overridable": True},
                       {"component": "TRANSPORT_ALLOWANCE", "method": "FIXED", "overridable": True}]},
            {"key": "fleet", "code": "FLEET_GROSS", "name": "Fleet crew (gross + trip pay)", "type": "GROSS_BASED",
             "lines": [{"component": "BASIC", "method": "FIXED", "overridable": True},
                       {"component": "HOUSING_ALLOWANCE", "method": "FIXED", "overridable": True},
                       {"component": "FOOD_ALLOWANCE", "method": "FIXED", "overridable": True}]},
        ],
        "custom_roles": {"Team Lead": TEAM_LEAD_GRANTS, "Compliance Reviewer": COMPLIANCE_REVIEWER},
        # GROSS_BASED: the basis is the monthly gross; "split" = the fixed line amounts (sum = basis).
        "people": [
            ("hr", "Mariam", "Haddad", ["hr_admin", "Compliance Reviewer"], "dxb", "HR", "admin", "HR Manager",
             "office", "24000.00", {"split": {"BASIC": "14000.00", "HOUSING_ALLOWANCE": "7000.00",
                                              "TRANSPORT_ALLOWANCE": "3000.00"}}),
            ("processor", "Joseph", "Mathew", ["payroll_processor", "employee"], "dxb", "Finance", "admin",
             "Payroll Processor", "office", "18000.00", {"split": {"BASIC": "10500.00", "HOUSING_ALLOWANCE": "5500.00",
                                                                 "TRANSPORT_ALLOWANCE": "2000.00"}}),
            ("approver", "Layla", "Nasser", ["payroll_approver", "employee"], "dxb", "Finance", "admin",
             "Payroll Approver", None, None, {"no_salary_reason": "approver"}),
            ("finance", "Daniel", "Okoro", ["finance_payments", "employee"], "dxb", "Finance", "admin",
             "Treasury & Payments", "office", "19000.00", {"split": {"BASIC": "11000.00", "HOUSING_ALLOWANCE": "6000.00",
                                                                    "TRANSPORT_ALLOWANCE": "2000.00"}}),
            ("deptmgr", "Khalid", "Rahman", ["manager"], "dxb", "Fleet", "admin", "Fleet Manager", "office",
             "26000.00", {"split": {"BASIC": "15000.00", "HOUSING_ALLOWANCE": "8000.00",
                                    "TRANSPORT_ALLOWANCE": "3000.00"}, "no_comp_visibility": True}),
            ("teamlead", "Aisha", "Siddiqui", ["employee", "Team Lead"], "dxb", "Dispatch Team", "deptmgr",
             "Dispatch Team Lead", "fleet", "12000.00", {"split": {"BASIC": "7000.00", "HOUSING_ALLOWANCE": "4000.00",
                                                                 "FOOD_ALLOWANCE": "1000.00"}}),
            ("emp1", "Ravi", "Menon", ["employee"], "dxb", "Dispatch Team", "teamlead", "Fleet Dispatcher", "fleet",
             "9500.00", {"split": {"BASIC": "5500.00", "HOUSING_ALLOWANCE": "3200.00", "FOOD_ALLOWANCE": "800.00"},
                         "trip_pay": "1250.00", "overtime": True, "bank": "verified"}),
            ("emp2", "Fatima", "Zahra", ["employee"], "auh", "Warehouse Team", "opsmgr", "Warehouse Supervisor",
             "fleet", "11000.00", {"split": {"BASIC": "6500.00", "HOUSING_ALLOWANCE": "3700.00",
                                             "FOOD_ALLOWANCE": "800.00"}, "claim": "320.00", "loan": "6000.00",
                                   "bank": "pending_change"}),
            ("opsmgr", "Hassan", "Karim", ["manager"], "auh", "Operations", "admin", "Operations Manager", "office",
             "23000.00", {"split": {"BASIC": "13500.00", "HOUSING_ALLOWANCE": "7000.00",
                                    "TRANSPORT_ALLOWANCE": "2500.00"}}),
            ("emp3", "Sara", "Ibrahim", ["employee"], "auh", "Operations", "opsmgr", "Inventory Clerk", "office",
             "8000.00", {"split": {"BASIC": "4800.00", "HOUSING_ALLOWANCE": "2400.00", "TRANSPORT_ALLOWANCE": "800.00"}}),
        ],
        "accounts": {
            "Company Administrator": "admin", "HR Administrator": "hr", "Payroll Processor": "processor",
            "Payroll Approver": "approver", "Finance & Payments": "finance",
            "Department Manager (no compensation visibility)": "deptmgr", "Team Lead": "teamlead",
            "Employee": "emp1", "Denial: employee of another department": "emp3",
            "Denial: manager of another team": "opsmgr",
        },
        "dept_heads": {"Fleet": "deptmgr", "Operations": "opsmgr", "HR": "hr", "Finance": "finance"},
        "holidays": [("Commemoration Day", "2026-11-30", None), ("UAE National Day", "2026-12-02", None)],
        "settings": {"payroll.overtime.enabled": True, "payroll.holiday_work_premium.enabled": True,
                     "payroll.loans.enabled": True, "payroll.deductions.min_net_pay_percent": "50"},
        "policies": {
            "ATTENDANCE_PAY_POLICY": {"unmarked_day": "PRESENT", "missing_punch_out": "EXCEPTION"},
            "OVERTIME_POLICY": {"buckets": [{"day_type": "WORKDAY", "multiplier": "1.25", "after_hours": "0"},
                                            {"day_type": "WEEKLY_OFF", "multiplier": "1.5", "after_hours": "0"},
                                            {"day_type": "HOLIDAY", "multiplier": "1.5", "after_hours": "0"}]},
            "HOLIDAY_PAY_POLICY": {"holiday_work_multiplier": "1.5", "weekly_off_work_multiplier": "1.5"},
            "LOAN_POLICY": {"min_service_months": 3},
        },
        "brand": {"product_name": "Beta Workforce", "short_name": "Beta", "tagline": "Moving people and freight",
                  "primary_color": "#0F766E", "accent_color": "#14B8A6", "sidebar_style": "DARK",
                  "default_locale": "en-AE", "legal_footer": "Beta Logistics LLC (fictitious test company)",
                  "support_email": "hr@beta-logistics.example"},
        "finalize_group": "monthly", "draft_group": "monthly", "statutory_attestation": True,
        "bank": {"country": "AE", "currency": "AED", "people": ["emp1", "emp2", "teamlead"]},
    },
    "gamma": {
        "name": "Gamma Solutions", "timezone": "America/New_York", "currency": "USD", "country": "US",
        "domain": "gamma-solutions.example", "admin": ("Emily", "Carter"), "prefix": "GS", "plan": "enterprise",
        "entity": {"code": "US01", "legal_name": "Gamma Solutions Inc.", "country_code": "US", "base_currency": "USD",
                   "timezone": "America/New_York", "confirm_country": True,
                   "registered_address": {"line1": "200 Test Avenue (fictitious)", "city": "New York",
                                          "subdivision_code": "US-NY", "postal_code": "10001"},
                   "registration_lines": [{"label": "EIN (test)", "value": "00-0000001"}]},
        "extra_entities": [],
        "branches": [("nyc", "US01", "New York", "NYC", "US-NY", "America/New_York", None),
                     ("cal", "US01", "California", "CAL", "US-CA", "America/Los_Angeles", None)],
        "work_weeks": [],
        "pay_groups": [("biweekly", "US01", "BIWEEKLY_USD", "Bi-weekly (USD)", "USD", "BIWEEKLY",
                        {"biweekly_anchor_date": "@anchor", "pay_day_rule": "DAYS_AFTER_PERIOD_END", "pay_day_value": 5},
                        True)],
        "departments": ["Engineering", "Sales", "Administration", "Finance"],
        "teams": [("Web Team", "Engineering"), ("Field Sales Team", "Sales")],
        "admin_place": ("Administration", "Chief Executive Officer"),
        "components": [
            {"code": "HOURLY_WAGE", "kind": "EARNING", "category": "BASE", "name": "Hourly wage",
             "calc_method": "RATE_X_UNITS", "params": {"rate": "1", "units": "att.payable_hours"},
             "tags": ["PART_OF_GROSS", "OT_BASE"], "proration": "NONE"},
        ],
        "structures": [
            {"key": "salaried", "code": "SALARIED_GROSS", "name": "Salaried (gross)", "type": "GROSS_BASED",
             "lines": [{"component": "BASIC", "method": "FIXED", "overridable": True}]},
            {"key": "hourly", "code": "HOURLY_US", "name": "Hourly (US)", "type": "HOURLY",
             # Units written out: the assignment preview runs with attendance disabled, so a bare
             # att.payable_hours is NULL there and the assignment is refused (reported defect).
             "lines": [{"component": "HOURLY_WAGE", "method": "RATE_X_UNITS",
                        "params": {"units": "COALESCE(att.payable_hours, 0)"}}]},
        ],
        "custom_roles": {"Team Lead": TEAM_LEAD_GRANTS, "Compliance Reviewer": COMPLIANCE_REVIEWER},
        # GROSS_BASED bi-weekly: the basis is the gross per pay period of the structure (monthly figure here).
        "people": [
            ("hr", "Olivia", "Martinez", ["hr_admin", "Compliance Reviewer"], "nyc", "Administration", "admin",
             "HR Administrator", "salaried", "8200.00", {"split": {"BASIC": "8200.00"}}),
            ("processor", "Noah", "Kim", ["payroll_processor", "employee"], "nyc", "Finance", "admin",
             "Payroll Processor", "salaried", "7400.00", {"split": {"BASIC": "7400.00"}}),
            ("approver", "Grace", "Lee", ["payroll_approver", "employee"], "nyc", "Finance", "admin",
             "Payroll Approver", None, None, {"no_salary_reason": "approver"}),
            ("finance", "Ethan", "Wright", ["finance_payments", "employee"], "nyc", "Finance", "admin",
             "Payments Specialist", "salaried", "7000.00", {"split": {"BASIC": "7000.00"}}),
            ("deptmgr", "Michael", "Brown", ["manager"], "nyc", "Engineering", "admin", "Engineering Manager",
             "salaried", "12500.00", {"split": {"BASIC": "12500.00"}}),
            ("teamlead", "Jessica", "Davis", ["employee", "Team Lead"], "nyc", "Web Team", "deptmgr", "Team Lead",
             "salaried", "10400.00", {"split": {"BASIC": "10400.00"}, "bonus": "750.00"}),
            ("emp1", "David", "Wilson", ["employee"], "nyc", "Web Team", "teamlead", "Frontend Engineer (hourly)",
             "hourly", "48.00", {"attendance_hours": True, "overtime": True, "task": True, "retro": "52.00"}),
            ("emp2", "Sarah", "Johnson", ["employee"], "cal", "Web Team", "teamlead", "Backend Engineer", "salaried",
             "9100.00", {"split": {"BASIC": "9100.00"}, "claim": "86.40"}),
            ("salesmgr", "Robert", "Garcia", ["manager"], "cal", "Sales", "admin", "Sales Manager", "salaried",
             "9800.00", {"split": {"BASIC": "9800.00"}}),
            ("emp3", "Chloe", "Nguyen", ["employee"], "cal", "Field Sales Team", "salesmgr", "Account Executive",
             "salaried", "6200.00", {"split": {"BASIC": "6200.00"}, "commission": "1340.00", "joiner": 2}),
            ("emp4", "Liam", "Turner", ["employee"], "cal", "Field Sales Team", "salesmgr", "Sales Associate (hourly)",
             "hourly", "31.50", {"attendance_hours": True, "leaver": True}),
        ],
        "accounts": {
            "Company Administrator": "admin", "HR Administrator": "hr", "Payroll Processor": "processor",
            "Payroll Approver": "approver", "Finance & Payments": "finance", "Department Manager": "deptmgr",
            "Team Lead": "teamlead", "Employee": "emp1", "Denial: employee of another department": "emp3",
            "Denial: manager of another team": "salesmgr",
        },
        "dept_heads": {"Engineering": "deptmgr", "Sales": "salesmgr", "Administration": "hr", "Finance": "finance"},
        "holidays": [("Thanksgiving Day", "2026-11-26", None), ("Cesar Chavez Day", "2027-03-31", "California")],
        "settings": {"payroll.overtime.enabled": True},
        "policies": {
            "ATTENDANCE_PAY_POLICY": {"unmarked_day": "PRESENT", "missing_punch_out": "EXCEPTION"},
            "OVERTIME_POLICY": {"buckets": [{"day_type": "WORKDAY", "multiplier": "1.5", "after_hours": "0"},
                                            {"day_type": "WEEKLY_OFF", "multiplier": "1.5", "after_hours": "0"},
                                            {"day_type": "HOLIDAY", "multiplier": "2", "after_hours": "0"}]},
        },
        "rate_cards": [("CODE_REVIEW", "45.00")],
        "brand": {"product_name": "Gamma HR Cloud", "short_name": "Gamma", "tagline": "Your team, in the cloud",
                  "primary_color": "#EA580C", "accent_color": "#1E3A8A", "sidebar_style": "DARK",
                  "default_locale": "en-US", "support_email": "people@gamma-solutions.example"},
        "finalize_group": "biweekly", "draft_group": "biweekly", "statutory_attestation": True,
        "bank": {"country": "US", "currency": "USD", "people": ["emp2"]},
    },
}


# ---------------------------------------------------------------------------------------------- context
@dataclass
class Context:
    make_client: Callable[[], Any]
    store: dict
    outbox_dir: Path
    platform_password: Optional[str] = None
    before_login: Optional[Callable[[], None]] = None
    today: Optional[date] = None
    log: Callable[[str], None] = print
    blocked: List[dict] = field(default_factory=list)
    stats: Dict[str, int] = field(default_factory=lambda: {"created": 0, "existing": 0})

    def created(self, what: str) -> None:
        self.stats["created"] += 1
        self.log(f"[payroll-seed]   + {what}")

    def existing(self) -> None:
        self.stats["existing"] += 1

    def block(self, company: str, step: str, error: Exception) -> None:
        entry = {"company": company, "step": step,
                 "endpoint": getattr(error, "endpoint", None), "status": getattr(error, "status", None),
                 "code": getattr(error, "code", None), "detail": str(getattr(error, "detail", error))[:300]}
        self.blocked.append(entry)
        self.log(f"[payroll-seed]   ! {company}: {step} blocked: {entry['endpoint']} {entry['status']} {entry['code']} "
                 f"{entry['detail'][:160]}")


def login(ctx: Context, email_address: str, password: str) -> Api:
    if ctx.before_login:
        ctx.before_login()
    api = Api(ctx.make_client(), email_address)
    data = api.post("auth/login", {"email": email_address, "password": password})
    if isinstance(data, dict) and data.get("mfa_required"):
        raise RuntimeError(f"{email_address} needs MFA; the seed accounts never enrol MFA")
    return api


def outbox_code(ctx: Context, company_id: int, address: str, since: float, timeout: float = 20.0) -> str:
    """The six-digit step-up code e-mailed to `address` (development/test outbox, written after `since`)."""
    folder = Path(ctx.outbox_dir) / str(company_id)
    safe = re.sub(r"[^a-zA-Z0-9@._-]", "_", address)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        files = sorted((p for p in folder.glob(f"auth.step_up_code-{safe}-*.eml") if p.stat().st_mtime >= since - 1),
                       key=lambda p: p.stat().st_mtime)
        if files:
            message = email.message_from_bytes(files[-1].read_bytes(), policy=email_policy.default)
            part = message.get_body(preferencelist=("plain", "html"))
            text = part.get_content() if part is not None else ""
            match = re.search(r"Code:\s*(\d{6})", text)
            if match:
                return match.group(1)
        time.sleep(0.5)
    raise RuntimeError(f"No step-up code reached the outbox {folder} (is EMAIL_OUTBOX_DIR the stack's outbox?)")


def step_up(ctx: Context, api: Api, company_id: int, address: str) -> None:
    status = api.get("payroll/step-up")
    if status.get("active"):
        return
    since = time.time()
    api.post("payroll/step-up/email-code")
    code = outbox_code(ctx, company_id, address, since)
    api.post("payroll/step-up", {"code": code})


def dates_in(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


# ---------------------------------------------------------------------------------------------- one company
class CompanySeed:
    def __init__(self, ctx: Context, key: str, spec: dict):
        self.ctx, self.key, self.spec = ctx, key, spec
        companies = ctx.store.setdefault("companies", {})
        self.state = companies.setdefault(key, {"name": spec["name"], "accounts": {}})
        self.state["name"] = spec["name"]
        self.accounts: dict = self.state.setdefault("accounts", {})
        self.sessions: Dict[str, Api] = {}
        self.ids: dict = {"entities": {}, "branches": {}, "departments": {}, "pay_groups": {}, "people": {},
                          "users": {}, "roles": {}, "structures": {}, "work_weeks": {}}
        self.cur = spec["currency"]

    # -------------------------------------------------------------- small helpers
    def log(self, text: str) -> None:
        self.ctx.log(f"[payroll-seed] {self.spec['name']}: {text}")

    def optional(self, step: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except SeedError as error:
            self.ctx.block(self.spec["name"], step, error)
        except (RuntimeError, KeyError, ValueError, TypeError, StopIteration) as error:
            self.ctx.block(self.spec["name"], step, error)
        return None

    @property
    def admin(self) -> Api:
        return self.sessions["admin"]

    def person(self, key: str) -> dict:
        return self.ids["people"][key]

    def session(self, key: str) -> Api:
        if key not in self.sessions:
            account = self.accounts[key]
            self.sessions[key] = login(self.ctx, account["email"], account["password"])
        return self.sessions[key]

    def email_of(self, first: str, last: str) -> str:
        return f"{first.lower()}.{re.sub(r'[^a-z]', '', last.lower())}@{self.spec['domain']}"

    # -------------------------------------------------------------- 1. tenant and admin
    def ensure_admin(self) -> None:
        spec = self.spec
        address = f"admin@{spec['domain']}"
        account = self.accounts.get("admin")
        if account and account.get("password"):
            self.sessions["admin"] = login(self.ctx, account["email"], account["password"])
            self.ctx.existing()
        else:
            password = new_password()
            api = Api(self.ctx.make_client(), address)
            if self.ctx.before_login:
                self.ctx.before_login()
            try:
                api.post("auth/signup", {"company_name": spec["name"], "full_name": " ".join(spec["admin"]),
                                         "email": address, "password": password, "timezone": spec["timezone"]})
            except SeedError as error:
                raise RuntimeError(f"Signup of {spec['name']} failed ({error.code}); if the company exists without "
                                   f"its credential file, reset the stack (payroll_stack.py reset)") from error
            self.accounts["admin"] = {"email": address, "password": password, "name": " ".join(spec["admin"]),
                                      "roles": ["company_admin", "payroll_manager"]}
            self.sessions["admin"] = api
            self.ctx.created(f"company {spec['name']} (admin {address})")
        me = self.admin.get("auth/me")
        self.state.update(company_id=me["company"]["id"], slug=me["company"]["slug"])
        self.ids["people"]["admin"] = {"employee_id": me["employee"]["id"], "user_id": me["user"]["id"],
                                       "email": address, "name": " ".join(spec["admin"])}
        self.ids["users"]["admin"] = me["user"]["id"]

    def ensure_plan(self) -> None:
        if not self.ctx.platform_password:
            return
        ops = Api(self.ctx.make_client(), PLATFORM_EMAIL)
        ops.post("platform/auth/login", {"email": PLATFORM_EMAIL, "password": self.ctx.platform_password})
        listed = items_of(ops.get("platform/companies", q=self.spec["name"], limit=50))
        current = next((c for c in listed if c.get("id") == self.state["company_id"]), {})
        if current.get("subscription_plan") == self.spec["plan"] and current.get("subscription_status") == "active":
            self.ctx.existing()
            return
        ops.patch(f"platform/companies/{self.state['company_id']}",
                  {"subscription_plan": self.spec["plan"], "subscription_status": "active"})
        self.ctx.created(f"plan {self.spec['plan']}")

    def ensure_company_settings(self) -> None:
        current = self.admin.get("settings")
        wanted = {"currency": self.cur, "companyEmail": f"hr@{self.spec['domain']}"}
        if all(current.get(k) == v for k, v in wanted.items()):
            self.ctx.existing()
            return
        self.admin.put("settings", wanted)
        self.ctx.created("company settings")

    # -------------------------------------------------------------- 2. organisation
    def ensure_entities(self) -> None:
        entities = self.admin.get("legal-entities")
        default = next(e for e in entities if e.get("is_default")) if entities else None
        by_code = {e["code"]: e for e in entities}
        main = self.spec["entity"]
        target = by_code.get(main["code"]) or default
        if any(target.get(k) != v for k, v in main.items() if k not in ("registered_address", "registration_lines",
                                                                         "confirm_country")) \
                or not target.get("country_confirmed"):
            target = self.admin.patch(f"legal-entities/{target['id']}", main)
            self.ctx.created(f"legal entity {main['code']}")
        else:
            self.ctx.existing()
        self.ids["entities"][main["code"]] = target["id"]
        for extra in self.spec["extra_entities"]:
            row = by_code.get(extra["code"])
            if row is None:
                row = self.admin.post("legal-entities", extra)
                self.ctx.created(f"legal entity {extra['code']}")
            else:
                self.ctx.existing()
            self.ids["entities"][extra["code"]] = row["id"]

    def ensure_work_weeks(self) -> None:
        rows = {w["code"]: w for w in self.admin.get("payroll/org/work-weeks")}
        for code, name, offs in self.spec["work_weeks"]:
            row = rows.get(code)
            if row is None:
                row = self.admin.post("payroll/org/work-weeks", {"code": code, "name": name, "weekly_off_days": offs})
                self.ctx.created(f"work week {code}")
            else:
                self.ctx.existing()
            self.ids["work_weeks"][code] = row["id"]

    def ensure_branches(self) -> None:
        branches = self.admin.get("branches")
        by_code = {b["code"]: b for b in branches}
        for index, (bkey, entity_code, name, code, region, tz, week) in enumerate(self.spec["branches"]):
            entity_id = self.ids["entities"][entity_code]
            row = by_code.get(code)
            if row is None and index == 0:
                main = next((b for b in branches if b["code"] == "B01"), None)
                if main is not None:
                    row = self.admin.patch(f"branches/{main['id']}", {"name": name, "code": code,
                                                                       "subdivision_code": region, "timezone": tz})
                    self.ctx.created(f"branch {code} (renamed starter branch)")
            if row is None:
                row = self.admin.post("branches", {"legal_entity_id": entity_id, "code": code, "name": name,
                                                   "subdivision_code": region, "timezone": tz})
                self.ctx.created(f"branch {code}")
            else:
                self.ctx.existing()
            self.ids["branches"][bkey] = row["id"]
            if week:
                link = {"work_week_pattern_id": self.ids["work_weeks"][week]}
                self.admin.put(f"payroll/org/branches/{row['id']}/calendar", link)

    def ensure_departments(self) -> None:
        rows = {d["name"]: d for d in items_of(self.admin.get("departments"))}
        for name in self.spec["departments"]:
            if name not in rows:
                rows[name] = self.admin.post("departments", {"name": name})
                self.ctx.created(f"department {name}")
            else:
                self.ctx.existing()
        for team, parent in self.spec["teams"]:
            if team not in rows:
                rows[team] = self.admin.post("departments", {"name": team,
                                                             "parent_department_id": rows[parent]["id"]})
                self.ctx.created(f"team {team}")
            else:
                self.ctx.existing()
        self.ids["departments"] = {name: row["id"] for name, row in rows.items()}

    def ensure_pay_groups(self) -> None:
        groups = {g["code"]: g for g in self.admin.get("payroll/org/pay-groups")}
        starter = groups.get("MONTHLY")
        for gkey, entity_code, code, name, currency, frequency, rules, _primary in self.spec["pay_groups"]:
            entity_id = self.ids["entities"][entity_code]
            rules = {k: (self.ctx.store["anchors"]["biweekly_anchor"] if v == "@anchor" else v)
                     for k, v in rules.items()}
            row = groups.get(code)
            reuse_starter = (code == "MONTHLY" and starter is not None)
            if row is None:
                row = self.admin.post("payroll/org/pay-groups", {
                    "legal_entity_id": entity_id, "code": code, "name": name, "currency": currency,
                    "pay_frequency": frequency, "calendar_rules": rules,
                    "description": f"{name} ({TAG} dataset)"})
                self.ctx.created(f"pay group {code}")
            elif reuse_starter and row.get("name") != name:
                row = self.admin.patch(f"payroll/org/pay-groups/{row['id']}", {"name": name})
                self.ctx.created(f"pay group {code} renamed")
            else:
                self.ctx.existing()
            self.ids["pay_groups"][gkey] = row["id"]
            calendar = self.admin.get(f"payroll/pay-groups/{row['id']}/calendar")
            calendar_row = calendar.get("calendar") if isinstance(calendar, dict) and "calendar" in calendar else calendar
            if not calendar_row or not calendar_row.get("id"):
                self.admin.post(f"payroll/pay-groups/{row['id']}/calendar",
                                {"frequency": frequency, "rules": rules, "effective_from": self.ctx.store["anchors"]["epoch"]})
                self.ctx.created(f"pay calendar {code}")
            else:
                self.ctx.existing()
        if starter is not None and "MONTHLY" not in {g[2] for g in self.spec["pay_groups"]}:
            # Beta / Gamma: the signup starter group is in the signup currency (INR); it keeps only the
            # founder's signup-day placement and is labelled so nobody runs payroll in it.
            label = "Signup starter group (not used for payroll)"
            if starter.get("name") != label:
                self.admin.patch(f"payroll/org/pay-groups/{starter['id']}", {"name": label})
                self.ctx.created("starter pay group relabelled")
            self.ids["pay_groups"]["starter"] = starter["id"]

    def period_of(self, gkey: str, day: date) -> dict:
        group_id = self.ids["pay_groups"][gkey]
        periods = items_of(self.admin.get(f"payroll/pay-groups/{group_id}/periods", limit=500))
        for period in periods:
            if period["start"] <= _iso(day) <= period["end"]:
                return period
        raise RuntimeError(f"No pay period of {gkey} contains {day}")

    # -------------------------------------------------------------- 3. roles and people
    def ensure_roles(self) -> None:
        roles = self.admin.get("roles")
        by_name = {(r.get("system_key") or r["name"]): r for r in roles}
        by_display = {r["name"]: r for r in roles}
        for name, grants in self.spec["custom_roles"].items():
            row = by_display.get(name)
            if row is None:
                row = self.admin.post("roles", {"name": name, "description": f"{name} ({TAG} custom role)",
                                                "grants": [{"permission": p, "scope": s} for p, s in grants]})
                self.ctx.created(f"role {name}")
            else:
                self.ctx.existing()
            by_name[name] = row
        self.ids["roles"] = {k: v["id"] for k, v in by_name.items()}

    def _find_employee(self, address: str) -> Optional[dict]:
        found = items_of(self.admin.get("employees", q=address, include_inactive="true", limit=50))
        return next((e for e in found if (e.get("email") or "").lower() == address), None)

    def _find_user(self, address: str) -> Optional[dict]:
        found = items_of(self.admin.get("users", q=address, limit=50))
        return next((u for u in found if (u.get("email") or "").lower() == address), None)

    def hire_date(self, extras: dict) -> date:
        anchors = self.ctx.store["anchors"]
        if extras.get("joiner") is not None:
            period = anchors["periods"][self.key][extras.get("pay_group", self.spec["finalize_group"])]
            return _d(period["start"]) + timedelta(days=int(extras["joiner"]))
        return date(2025, 1, 6)

    def ensure_people(self) -> None:
        spec = self.spec
        dept, position = spec["admin_place"]
        admin_emp = self.person("admin")["employee_id"]
        profile = self.admin.get(f"employees/{admin_emp}")
        if profile.get("position") != position or profile.get("department_id") != self.ids["departments"][dept]:
            self.admin.put(f"employees/{admin_emp}", {"position": position,
                                                       "department_id": self.ids["departments"][dept]})
            self.ctx.created("founder profile")
        primary = next(g[0] for g in spec["pay_groups"] if g[7])
        for index, (pkey, first, last, roles, branch, dept, manager, position, structure, basis, extras) in \
                enumerate(spec["people"], start=2):
            address = self.email_of(first, last)
            code = f"{spec['prefix']}-{index:03d}"
            employee = self._find_employee(address)
            gkey = extras.get("pay_group", primary)
            branch_id = self.ids["branches"][branch]
            entity_code = next(b[1] for b in spec["branches"] if b[0] == branch)
            if employee is None:
                hired = self.hire_date(extras)
                employee = self.admin.post("employees", {
                    "first_name": first, "last_name": last, "email": address, "employee_code": code,
                    "hire_date": _iso(hired), "position": position, "legal_entity_id": self.ids["entities"][entity_code],
                    "branch_id": branch_id, "department_id": self.ids["departments"][dept],
                    "manager_id": self.person(manager)["employee_id"], "pay_group_id": self.ids["pay_groups"][gkey],
                    "country": spec["country"], "location": next(b[2] for b in spec["branches"] if b[0] == branch)})
                self.ctx.created(f"employee {code} {first} {last}")
            else:
                self.ctx.existing()
            self.ids["people"][pkey] = {"employee_id": employee["id"], "email": address, "name": f"{first} {last}",
                                        "code": code, "hire_date": employee.get("hire_date"), "pay_group": gkey,
                                        "branch": branch, "department": dept, "manager": manager,
                                        "position": position, "structure": structure, "basis": basis,
                                        "extras": extras, "roles": roles}
            if extras.get("fte"):
                self._ensure_fte(pkey, employee, extras["fte"])
            if roles:
                self._ensure_login(pkey, address, f"{first} {last}", employee["id"], roles)
        for dept_name, head in spec["dept_heads"].items():
            dept_id = self.ids["departments"].get(dept_name)
            if dept_id is None or head not in self.ids["people"]:
                continue
            row = next((d for d in items_of(self.admin.get("departments")) if d["id"] == dept_id), {})
            if row.get("head_employee_id") != self.person(head)["employee_id"]:
                self.admin.put(f"departments/{dept_id}", {"name": dept_name,
                                                          "head_employee_id": self.person(head)["employee_id"],
                                                          **({"parent_department_id": row["parent_department_id"]}
                                                             if row.get("parent_department_id") else {})})
                self.ctx.created(f"head of {dept_name}")

    def _ensure_fte(self, pkey: str, employee: dict, fte: str) -> None:
        history = self.admin.get(f"employees/{employee['id']}/assignments")
        rows = items_of(history) if not isinstance(history, dict) or "items" in history else \
            history.get("assignments", [])
        if any(str(r.get("fte")) in (fte, f"{fte}000", f"{fte}0") or (r.get("fte") and float(r["fte"]) == float(fte))
               for r in rows):
            self.ctx.existing()
            return
        current = rows[-1] if rows else {}
        hired = _d(employee["hire_date"])
        self.admin.post(f"employees/{employee['id']}/assignments", {
            "branch_id": current.get("branch_id") or self.ids["branches"][self.ids["people"][pkey]["branch"]],
            "department_id": current.get("department_id"), "effective_from": _iso(hired + timedelta(days=1)),
            "reason": "CORRECTION", "note": f"Part-time contract, FTE {fte} ({TAG})", "fte": fte})
        self.ctx.created(f"part-time placement (FTE {fte}) for {pkey}")

    def _ensure_login(self, pkey: str, address: str, name: str, employee_id: int, roles: List[str]) -> None:
        role_ids = sorted(self.ids["roles"][r] for r in roles)
        user = self._find_user(address)
        account = self.accounts.get(pkey)
        if user is None or (user.get("status") == "invited"):
            password = (account or {}).get("password") or new_password()
            if user is None:
                invite = self.admin.post("users/invite", {"email": address, "full_name": name, "employee_id": employee_id,
                                                          "role_ids": role_ids})
            else:
                invite = self.admin.post(f"users/{user['id']}/resend-invite")
            token = invite["invite_url"].split("token=")[1]
            if self.ctx.before_login:
                self.ctx.before_login()
            api = Api(self.ctx.make_client(), address)
            accepted = api.post("auth/invitations/accept", {"token": token, "password": password})
            self.accounts[pkey] = {"email": address, "password": password, "name": name, "roles": roles}
            self.sessions[pkey] = api
            user_id = accepted["me"]["user"]["id"]
            self.ctx.created(f"login {address} ({', '.join(roles)})")
        else:
            user_id = user["id"]
            if not account:
                raise RuntimeError(f"{address} exists but its password is not in {CREDENTIALS_JSON}; reset the stack")
            account["roles"] = roles
            self.ctx.existing()
        self.ids["users"][pkey] = user_id
        self.ids["people"][pkey]["user_id"] = user_id
        current = self.admin.get(f"users/{user_id}")
        have = sorted(r["id"] for r in current.get("roles", []))
        if have != role_ids:
            self.admin.put(f"users/{user_id}/roles", {"role_ids": role_ids})
            self.ctx.created(f"roles of {address}")
        scope_entity = self.ids["people"][pkey]["extras"].get("scope_entity")
        if scope_entity:
            role_id = self.ids["roles"][next(r for r in roles if "IN02" in r)]
            wanted = [{"role_id": role_id, "legal_entity_ids": [self.ids["entities"][scope_entity]]}]
            targets = self.admin.get(f"users/{user_id}/scope-targets")
            have_t = [{"role_id": t["role_id"], "legal_entity_ids": t["legal_entity_ids"]} for t in targets
                      if t["legal_entity_ids"]]
            if have_t != wanted:
                self.admin.put(f"users/{user_id}/scope-targets", {"assignments": wanted})
                self.ctx.created(f"scope target {scope_entity} for {address}")

    # -------------------------------------------------------------- 4. brand and calendars
    def ensure_brand(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        current = self.admin.get("branding")
        published = current.get("published") or current.get("tenant") or current
        flat = json.dumps(published)
        if self.spec["brand"]["product_name"] in flat:
            self.ctx.existing()
            return
        self.admin.put("branding/draft", {"changes": self.spec["brand"]})
        self.admin.post("branding/publish", {})
        self.ctx.created(f"brand {self.spec['brand']['product_name']}")

    def ensure_holidays(self) -> None:
        rows = items_of(self.admin.get("holidays"))
        have = {(h["name"], str(h["date"])[:10]) for h in rows}
        for name, day, location in self.spec["holidays"]:
            if (name, day) in have:
                self.ctx.existing()
                continue
            self.admin.post("holidays", {"name": name, "date": day, "location": location, "is_optional": False})
            self.ctx.created(f"holiday {name}")

    # -------------------------------------------------------------- 5. payroll configuration
    def ensure_payroll_settings(self) -> None:
        """Company policy choices of the administrator, effective from the dataset epoch (with a reason).

        The starter profile writes its TENANT rows from the signup day; the dataset's periods start earlier,
        so every TENANT row is re-saved from the epoch (same value, reason recorded) and the company's own
        switches are added. Nothing statutory is switched ON here."""
        epoch = self.ctx.store["anchors"]["epoch"]
        items = self.admin.get("payroll/settings/values")["items"]
        dated = {d["key"] for d in self.admin.get("payroll/settings/definitions")["definitions"]
                 if d.get("effectiveDated", d.get("effective_dated", True))}
        by_key = {i["key"]: i for i in items}
        changes = []
        for item in items:
            here = item.get("here")
            if here and here["effective_from"] > epoch and item.get("editable") and item["key"] in dated:
                value = self.spec["settings"].get(item["key"], here["value"])
                changes.append({"key": item["key"], "scope_type": "TENANT", "value": value, "locked": here["locked"],
                                "effective_from": epoch, "row_version": item["row_version"],
                                "reason": f"{TAG}: company configuration applies from the dataset epoch {epoch}"})
        done = {c["key"] for c in changes}
        for key, value in self.spec["settings"].items():
            if key in done:
                continue
            item = by_key[key]
            here = item.get("here") or {}
            if here.get("value") == value and (here.get("effective_from", "9999") <= epoch or key not in dated):
                self.ctx.existing()
                continue
            changes.append({"key": key, "scope_type": "TENANT", "value": value, "effective_from": epoch,
                            "row_version": item["row_version"],
                            "reason": f"{TAG}: company policy choice of the administrator"})
        if changes:
            for start in range(0, len(changes), 100):
                self.admin.put("payroll/settings/values", {"changes": changes[start:start + 100]})
            self.ctx.created(f"{len(changes)} payroll settings from {epoch}")
        for entity_code, values in self.spec.get("entity_settings", {}).items():
            entity_id = self.ids["entities"][entity_code]
            scoped = {i["key"]: i for i in self.admin.get("payroll/settings/values", scope_type="LEGAL_ENTITY",
                                                          scope_id=entity_id)["items"]}
            batch = []
            for key, value in values.items():
                here = scoped[key].get("here") or {}
                if here.get("value") == value and here.get("effective_from", "9999") <= epoch:
                    self.ctx.existing()
                    continue
                batch.append({"key": key, "scope_type": "LEGAL_ENTITY", "scope_id": entity_id, "value": value,
                              "effective_from": epoch, "row_version": scoped[key]["row_version"],
                              "reason": f"{TAG}: statutory scheme of {entity_code} (fixture pack, development only)"})
            if batch:
                self.admin.put("payroll/settings/values", {"changes": batch})
                self.ctx.created(f"{len(batch)} {entity_code} settings")

    def ensure_policies(self) -> None:
        epoch = self.ctx.store["anchors"]["epoch"]
        policies = {p["type"]: p for p in self.admin.get("payroll/policies")}
        for type_code, overrides in self.spec["policies"].items():
            summary = policies.get(type_code)
            if summary is None:
                continue
            detail = self.admin.get(f"payroll/policies/{summary['id']}")
            versions = detail.get("versions") or []
            active = next((v for v in versions if v["status"] == "ACTIVE"), None)
            base = dict((active or versions[-1])["fields"])
            wanted = {**base, **overrides}
            if active and active["effective_from"] <= epoch and all(active["fields"].get(k) == v
                                                                     for k, v in overrides.items()):
                self.ctx.existing()
                continue
            draft = next((v for v in versions if v["status"] == "DRAFT"), None)
            if draft is None:
                draft = self.admin.post(f"payroll/policies/{summary['id']}/versions",
                                        {"fields": wanted, "effective_from": epoch})
            else:
                draft = self.admin.put(f"payroll/policies/{summary['id']}/versions/{draft['version']}",
                                       {"fields": wanted, "effective_from": epoch})
            number = draft.get("version") or draft.get("latest_version")
            self.admin.post(f"payroll/policies/{summary['id']}/versions/{number}/activate",
                            {"effective_from": epoch,
                             "reason": f"{TAG}: values entered by the administrator as company policy"})
            self.ctx.created(f"policy {type_code} v{number}")

    def ensure_components(self) -> None:
        self.admin.post("payroll/components/seed")
        have = {c["code"] for c in self.admin.get("payroll/components", include_archived="true")}
        for comp in self.spec["components"]:
            if comp["code"] in have:
                self.ctx.existing()
                continue
            self.admin.post("payroll/components", {**comp, "effective_from": "2025-01-01"})
            self.ctx.created(f"component {comp['code']}")

    def ensure_structures(self) -> None:
        rows = {s["code"]: s for s in self.admin.get("payroll/salary-structures")}
        for structure in self.spec["structures"]:
            row = rows.get(structure["code"])
            if row is None:
                body = {k: v for k, v in structure.items() if k != "key"}
                body["currency"] = self.cur
                row = self.admin.post("payroll/salary-structures", body)
                self.ctx.created(f"structure {structure['code']}")
            detail = self.admin.get(f"payroll/salary-structures/{row['id']}")
            versions = detail.get("versions") or []
            active = next((v for v in versions if v["status"] == "ACTIVE"), None)
            if active is None:
                draft = next(v for v in versions if v["status"] == "DRAFT")
                detail = self.admin.post(f"payroll/salary-structures/{row['id']}/publish",
                                         {"version": draft["version"], "effective_from": "2025-01-01"})
                active = next(v for v in detail["versions"] if v["status"] == "ACTIVE")
                self.ctx.created(f"structure {structure['code']} published")
            else:
                self.ctx.existing()
            self.ids["structures"][structure["key"]] = active["id"]

    def ensure_compensation(self) -> None:
        processor, approver = self.session("processor"), self.session("approver")
        for pkey, person in self.ids["people"].items():
            if pkey == "admin" or not person.get("structure"):
                continue
            current = self.admin.get(f"payroll/employees/{person['employee_id']}/compensation")
            records = current.get("assignments") or current.get("history") or items_of(current)
            if any(r.get("status") in ("ACTIVE", "SCHEDULED", "PENDING_APPROVAL", "SUPERSEDED") for r in records):
                pending = [r for r in records if r.get("status") == "PENDING_APPROVAL"]
                for record in pending:
                    approver.post(f"payroll/compensation/{record['id']}/approve",
                                  {"remarks": f"{TAG}: approved by the independent approver"})
                    self.ctx.created(f"compensation approved for {pkey}")
                if not pending:
                    self.ctx.existing()
                continue
            extras = person["extras"]
            body = {"template_version_id": self.ids["structures"][person["structure"]],
                    "pay_basis_amount": money(person["basis"], self.cur),
                    "effective_from": person["hire_date"] if extras.get("joiner") is not None else "2025-01-06",
                    "reason": "HIRE", "note": f"{TAG}: offer letter (test data)", "submit": True}
            if extras.get("split"):
                body["overrides"] = {code: {"amount": amount} for code, amount in extras["split"].items()}
            proposed = processor.post(f"payroll/employees/{person['employee_id']}/compensation", body)
            if proposed.get("status") == "PENDING_APPROVAL":
                approver.post(f"payroll/compensation/{proposed['id']}/approve",
                             {"remarks": f"{TAG}: approved by an independent approver"})
            self.ctx.created(f"compensation {person['structure']} {person['basis']} {self.cur} for {pkey}")

    # -------------------------------------------------------------- 6. time and variable inputs
    def working_day_before_today(self, gkey: str, offset: int = 1) -> date:
        """A deterministic working day of the finalized period, before the anchor day when possible."""
        anchors = self.ctx.store["anchors"]
        period = anchors["periods"][self.key][gkey]
        start, end = _d(period["start"]), _d(period["end"])
        today = _d(anchors["today"])
        candidates = [d for d in dates_in(start, min(end, today - timedelta(days=1))) if d.weekday() < 5]
        if not candidates:
            candidates = [d for d in dates_in(start, end) if d.weekday() < 5]
        return candidates[max(0, len(candidates) - offset)]

    def ensure_leave(self) -> None:
        for pkey, person in self.ids["people"].items():
            if not person.get("extras", {}).get("leave"):
                continue
            day = self.working_day_before_today(person["pay_group"], offset=2)
            existing = items_of(self.admin.get("leaves", employee_id=person["employee_id"], limit=200))
            if any(str(l.get("start_date"))[:10] == _iso(day) and l.get("status") in ("pending", "approved")
                   for l in existing):
                self.ctx.existing()
                continue
            leave = self.session(pkey).post("leaves/apply", {"leave_type": "casual", "start_date": _iso(day),
                                                             "end_date": _iso(day), "reason": f"{TAG}: family function"})
            self.session(person["manager"]).post(f"leaves/{leave['id']}/approve",
                                                 {"remarks": f"{TAG}: approved by the manager"})
            self.ctx.created(f"approved leave {day} for {pkey}")

    def ensure_lop(self) -> None:
        for pkey, person in self.ids["people"].items():
            if not person.get("extras", {}).get("lop_day"):
                continue
            day = self.working_day_before_today(person["pay_group"], offset=3)
            existing = items_of(self.admin.get("leaves", employee_id=person["employee_id"], limit=200))
            if any(str(l.get("start_date"))[:10] == _iso(day) and l.get("leave_type") == "unpaid"
                   and l.get("status") in ("pending", "approved") for l in existing):
                self.ctx.existing()
                continue
            leave = self.session(pkey).post("leaves/apply", {"leave_type": "unpaid", "start_date": _iso(day),
                                                             "end_date": _iso(day),
                                                             "reason": f"{TAG}: unpaid day (loss of pay)"})
            self.session(person["manager"]).post(f"leaves/{leave['id']}/approve",
                                                 {"remarks": f"{TAG}: unpaid leave approved"})
            self.ctx.created(f"approved unpaid leave (LOP) {day} for {pkey}")

    def ensure_attendance(self) -> None:
        """Punches for hourly workers on the worked days of the finalized period up to yesterday (8h + 30m lunch)."""
        tz = self.spec["timezone"]
        recorder = self.session("hr")
        for pkey, person in self.ids["people"].items():
            extras = person.get("extras", {})
            if not extras.get("attendance_hours"):
                continue
            period = self.ctx.store["anchors"]["periods"][self.key][person["pay_group"]]
            start, end = _d(period["start"]), _d(period["end"])
            today = _d(self.ctx.store["anchors"]["today"])
            hired = _d(person["hire_date"])
            last = min(end, today - timedelta(days=1))
            if extras.get("leaver"):
                last = min(last, _d(self.ctx.store["anchors"]["leavers"][self.key]))
            existing = items_of(self.admin.get("attendance", employee_id=person["employee_id"],
                                               start_date=_iso(start), end_date=_iso(end), limit=200))
            have = {str(a.get("date"))[:10] for a in existing}
            for day in dates_in(max(start, hired), last):
                if day.weekday() >= 5 or _iso(day) in have:
                    continue
                recorder.post("attendance/manual", {
                    "employee_id": person["employee_id"], "date": _iso(day),
                    "check_in_time": local_time(day, "09:00", tz), "check_out_time": local_time(day, "17:30", tz),
                    "status": "present", "notes": f"{TAG}: time clock import (test data)",
                    "breaks": [{"started_at": local_time(day, "12:30", tz), "ended_at": local_time(day, "13:00", tz)}]})
                self.ctx.created(f"attendance {day} for {pkey}")

    def ensure_overtime(self) -> None:
        for pkey, person in self.ids["people"].items():
            if not person.get("extras", {}).get("overtime"):
                continue
            day = self.working_day_before_today(person["pay_group"], offset=1)
            entries = items_of(self.admin.get("overtime/entries", employee_id=person["employee_id"], limit=200))
            entry = next((e for e in entries if str(e.get("date"))[:10] == _iso(day)
                          and e.get("status") not in ("CANCELLED", "REJECTED")), None)
            if entry is None:
                entry = self.session(pkey).post("overtime/entries", {
                    "date": _iso(day), "minutes": 120, "reason": f"{TAG}: release support after hours"})
                self.ctx.created(f"overtime request {day} for {pkey}")
            status = entry.get("status")
            if status not in ("APPROVED", "PAID", "CONSUMED"):
                self.session(person["manager"]).post(f"overtime/entries/{entry['id']}/approve",
                                                     {"remarks": f"{TAG}: approved by the manager"})
                self.ctx.created(f"overtime approved for {pkey}")
            else:
                self.ctx.existing()

    def ensure_wage_inputs(self) -> None:
        processor, approver = self.session("processor"), self.session("approver")
        mapping = (("trip_pay", "TRIP_PAY"), ("bonus", "BONUS"), ("commission", "COMMISSION"))
        for pkey, person in self.ids["people"].items():
            extras = person.get("extras", {})
            for extra_key, component in mapping:
                if not extras.get(extra_key):
                    continue
                period = self.ctx.store["anchors"]["periods"][self.key][person["pay_group"]]
                ref = f"{TAG}-{self.key}-{pkey}-{component}"
                rows = items_of(processor.get("payroll/wage-inputs", employee_id=person["employee_id"], limit=200))
                row = next((r for r in rows if r.get("external_ref") == ref), None)
                if row is None:
                    row = processor.post("payroll/wage-inputs", {
                        "employee_id": person["employee_id"], "component_code": component,
                        "period_start": period["start"], "period_end": period["end"],
                        "amount": money(extras[extra_key], self.cur), "reason": f"{TAG}: {component.lower()} (test)",
                        "external_ref": ref, "submit": True})
                    self.ctx.created(f"wage input {component} {extras[extra_key]} for {pkey}")
                if row.get("status") in ("SUBMITTED", "PENDING_APPROVAL"):
                    approver.post(f"payroll/wage-inputs/{row['id']}/approve", {"remarks": f"{TAG}: approved"})
                    self.ctx.created(f"wage input {component} approved")
                else:
                    self.ctx.existing()

    def ensure_tasks(self) -> None:
        for task_type, rate in self.spec.get("rate_cards", []):
            cards = items_of(self.admin.get("payroll/task-rate-cards", task_type=task_type))
            if not cards:
                self.session("processor").post("payroll/task-rate-cards", {
                    "task_type": task_type, "rate": money(rate, self.cur), "effective_from": "2025-01-01"})
                self.ctx.created(f"rate card {task_type} {rate}")
            else:
                self.ctx.existing()
        for pkey, person in self.ids["people"].items():
            if not person.get("extras", {}).get("task"):
                continue
            ref = f"{TAG}-{self.key}-{pkey}-TASK1"
            tasks = items_of(self.admin.get("tasks", limit=200))
            task = next((t for t in tasks if t.get("external_ref") == ref), None)
            day = self.working_day_before_today(person["pay_group"], offset=2)
            manager = self.session(person["manager"])
            if task is None:
                task = manager.post("tasks", {"title": "Code review batch (test)", "task_type": "CODE_REVIEW",
                                              "quantity": "6", "unit": "reviews", "completed_on": _iso(day),
                                              "external_ref": ref,
                                              "assignees": [{"employee_id": person["employee_id"]}]})
                self.ctx.created(f"task {ref}")
            task = self.admin.get(f"tasks/{task['id']}")
            if task.get("status") in ("DRAFT", "OPEN", "ASSIGNED"):
                evidence = task.get("evidence") or []
                if not evidence:
                    self.session(pkey).request(
                        "POST", f"tasks/{task['id']}/evidence",
                        files={"file": ("review-log.txt", b"Test evidence: six code reviews (fictitious).\n",
                                        "text/plain")})
                self.session(pkey).post(f"tasks/{task['id']}/submit")
                task = self.admin.get(f"tasks/{task['id']}")
                self.ctx.created("task submitted")
            if task.get("status") in ("SUBMITTED", "PENDING_APPROVAL"):
                manager.post(f"tasks/{task['id']}/approve", {"remarks": f"{TAG}: approved"})
                self.ctx.created("task approved")
            else:
                self.ctx.existing()

    def ensure_claims(self) -> None:
        for pkey, person in self.ids["people"].items():
            amount = person.get("extras", {}).get("claim")
            if not amount:
                continue
            mine = self.session(pkey)
            claims = items_of(mine.get("me/payroll/reimbursement-claims"))
            claim = next((c for c in claims if (c.get("merchant") or "").startswith(TAG)), None)
            if claim is None:
                day = self.working_day_before_today(person["pay_group"], offset=3)
                claim = mine.post("me/payroll/reimbursement-claims", {
                    "claim_type": "GENERAL", "expense_date": _iso(day), "amount": money(amount, self.cur),
                    "merchant": f"{TAG} Test Office Supplies", "description": "Printer toner (test receipt)"})
                self.ctx.created(f"claim {amount} {self.cur} for {pkey}")
            claim = mine.get(f"me/payroll/reimbursement-claims/{claim['id']}")
            if not (claim.get("receipts") or []):
                mine.request("POST", f"me/payroll/reimbursement-claims/{claim['id']}/receipts",
                             files={"file": ("receipt.png", tiny_png(), "image/png")})
                self.ctx.created("claim receipt")
            claim = self.session("processor").get(f"payroll/reimbursement-claims/{claim['id']}")
            if claim.get("status") in ("SUBMITTED", "PENDING_APPROVAL"):
                self.session("approver").post(f"payroll/reimbursement-claims/{claim['id']}/approve",
                                              {"remarks": f"{TAG}: approved"})
                self.ctx.created("claim approved")
            else:
                self.ctx.existing()

    def ensure_loans(self) -> None:
        for pkey, person in self.ids["people"].items():
            extras = person.get("extras", {})
            for kind, amount, tenure in (("ADVANCE", extras.get("advance"), 1), ("LOAN", extras.get("loan"), 6)):
                if not amount:
                    continue
                loans = items_of(self.session("processor").get("payroll/loans", employee_id=person["employee_id"]))
                loan = next((l for l in loans if l.get("kind") == kind and l.get("status") not in
                             ("REJECTED", "CANCELLED")), None)
                if loan is None:
                    loan = self.session(pkey).post("me/payroll/loans", {
                        "kind": kind, "amount": money(amount, self.cur), "tenure": tenure,
                        "purpose": f"{TAG}: {kind.lower()} (test)"})
                    self.ctx.created(f"{kind.lower()} {amount} for {pkey}")
                loan = self.session("processor").get(f"payroll/loans/{loan['id']}")
                if loan.get("status") in ("SUBMITTED", "PENDING_APPROVAL", "REQUESTED"):
                    self.session("approver").post(f"payroll/loans/{loan['id']}/approve",
                                                  {"reason": f"{TAG}: approved"})
                    self.ctx.created(f"{kind.lower()} approved")
                else:
                    self.ctx.existing()

    def ensure_bank_accounts(self) -> None:
        bank = self.spec.get("bank")
        if not bank:
            return
        for pkey in bank["people"]:
            person = self.ids["people"][pkey]
            mine = self.session(pkey)
            accounts = items_of(mine.get("me/payroll/bank-accounts"))
            want = person["extras"].get("bank", "verified")
            if not accounts:
                step_up(self.ctx, mine, self.state["company_id"], person["email"])
                mine.post("me/payroll/bank-accounts", bank_details(bank["country"], bank["currency"], person,
                                                                   variant=0))
                self.ctx.created(f"bank account (test data) for {pkey}")
                accounts = items_of(mine.get("me/payroll/bank-accounts"))
            account = accounts[0]
            changes = items_of(self.admin.get("payroll/bank-change-requests", employee_id=person["employee_id"])) \
                if False else items_of(self.session("approver").get("payroll/bank-change-requests"))
            pending = [c for c in changes if c.get("bank_account_id") == account["id"]
                       and c.get("status") in ("PENDING_APPROVAL", "PENDING", "SUBMITTED")]
            if pending:
                step_up(self.ctx, self.session("approver"), self.state["company_id"], self.accounts["approver"]["email"])
            for change in pending:
                self.session("approver").post(f"payroll/bank-change-requests/{change['id']}/approve", {
                    "remarks": f"{TAG}: approved", "waive_cooling_off_reason": f"{TAG}: seeded test account"})
                self.ctx.created("bank change approved")
            account = self.session("approver").get(f"payroll/bank-accounts/{account['id']}")
            if account.get("verification_status") != "VERIFIED":
                self.session("approver").post(f"payroll/bank-accounts/{account['id']}/verify", {
                    "method": "DOCUMENT_REVIEW", "decision": "PASS",
                    "reason": f"{TAG}: test bank letter reviewed (fictitious account)"})
                self.ctx.created(f"bank account verified for {pkey}")
            else:
                self.ctx.existing()
            if want == "pending_change":
                accounts = items_of(mine.get("me/payroll/bank-accounts"))
                if len(accounts) < 2:
                    step_up(self.ctx, mine, self.state["company_id"], person["email"])
                    mine.post("me/payroll/bank-accounts", {**bank_details(bank["country"], bank["currency"], person,
                                                                          variant=1),
                                                           "replaces_account_id": account["id"]})
                    self.ctx.created(f"pending bank change (test data) for {pkey}")
                else:
                    self.ctx.existing()

    # -------------------------------------------------------------- 7. statutory attestation (I-15)
    def ensure_attestation(self) -> None:
        if not self.spec.get("statutory_attestation"):
            return
        entity_id = self.ids["entities"][self.spec["entity"]["code"]]
        rows = items_of(self.admin.get("payroll/statutory-attestations", legal_entity_id=entity_id))
        row = next((r for r in rows if r.get("status") in ("PENDING", "ACTIVE", "APPROVED")), None)
        if row is None:
            row = self.admin.post("payroll/statutory-attestations", {
                "legal_entity_id": entity_id, "effective_from": self.ctx.store["anchors"]["epoch"],
                "reason": f"{TAG}: statutory obligations of {self.spec['entity']['legal_name']} are handled outside "
                          "this system (test dataset); the country pack is not verified (CONFIG_ONLY).",
                "evidence_ref": "TEST-ATTESTATION-0001"})
            self.ctx.created("statutory attestation (maker: company administrator)")
        if row.get("status") == "PENDING":
            self.session("hr").post(f"payroll/statutory-attestations/{row['id']}/approve",
                                    {"row_version": row.get("row_version")})
            self.ctx.created("statutory attestation approved (checker: HR administrator, Compliance Reviewer)")
        else:
            self.ctx.existing()
        self.state["attestation_id"] = row["id"]

    # -------------------------------------------------------------- 8. runs
    def _runs(self, api: Api, gkey: str, period_id: int) -> list:
        return items_of(api.get("payroll/runs", pay_group_id=self.ids["pay_groups"][gkey], pay_period_id=period_id))

    def ensure_finalized_run(self) -> None:
        """REGULAR run of the current period: processor pre-checks, calculates, reviews and submits; the
        independent approver approves; the payroll manager (founder) steps up and finalizes."""
        gkey = self.spec["finalize_group"]
        period = self.period_of(gkey, _d(self.ctx.store["anchors"]["periods"][self.key][gkey]["start"]))
        processor, approver, admin = self.session("processor"), self.session("approver"), self.admin
        runs = [r for r in self._runs(processor, gkey, period["id"]) if r.get("run_type") == "REGULAR"
                and r.get("status") != "CANCELLED"]
        run = runs[0] if runs else None
        record = self.state.setdefault("runs", {})
        if run is None:
            run = processor.post("payroll/runs", {"run_type": "REGULAR", "pay_group_id": self.ids["pay_groups"][gkey],
                                                  "pay_period_id": period["id"],
                                                  "label": f"{self.spec['name']} {period['start']} regular"},
                                 key=f"{TAG}-{self.key}-run-create")
            self.ctx.created(f"run {run['number']}")
        run_id = run["id"]
        record["finalized"] = {"id": run_id, "pay_group": gkey, "period": [period["start"], period["end"]]}
        done = ("FINALIZED", "PAYMENT_PENDING", "PARTIALLY_PAID", "PAID", "RECONCILED", "CLOSED")
        attempts = 0
        for _ in range(12):
            run = processor.get(f"payroll/runs/{run_id}")
            status = run["status"]
            record["finalized"]["status"] = status
            if status in done:
                break
            if status == "DRAFT":
                processor.post(f"payroll/runs/{run_id}/precheck", {"row_version": run["row_version"]})
                after = processor.get(f"payroll/runs/{run_id}")
                if after["status"] == "DRAFT":
                    raise RuntimeError("pre-check blocked: " + self._blocking_codes(processor, run_id))
                self.ctx.created("run pre-checked")
            elif status in ("READY", "VALIDATION_FAILED", "CALCULATION_FAILED"):
                self._waive_by_design(processor, run_id)
                attempts += 1
                if status != "READY" and attempts > 2:
                    raise RuntimeError(f"calculation ends in {status}: " + self._blocking_codes(processor, run_id))
                run = processor.get(f"payroll/runs/{run_id}")
                processor.post(f"payroll/runs/{run_id}/calculate", {"row_version": run["row_version"]},
                               key=f"{TAG}-{self.key}-run-{run_id}-calculate-{run['row_version']}")
                run = wait_status(processor, run_id, ("REVIEW_REQUIRED", "CALCULATION_FAILED", "VALIDATION_FAILED"))
                self.ctx.created(f"run calculated ({run['status']})")
            elif status == "REVIEW_REQUIRED":
                self._acknowledge_variance(processor, run_id)
                run = processor.get(f"payroll/runs/{run_id}")
                processor.post(f"payroll/runs/{run_id}/submit", {"comment": f"{TAG}: reviewed",
                                                                 "row_version": run["row_version"]})
                self.ctx.created("run submitted for approval")
            elif status == "APPROVAL_PENDING":
                approver.post(f"payroll/runs/{run_id}/approve", {
                    "row_version": run["row_version"], "comment": f"{TAG}: approved by the independent approver"})
                self.ctx.created("run approved by the independent approver")
            elif status == "APPROVED":
                approvals = admin.get(f"payroll/runs/{run_id}/approvals")
                for line in approvals.get("self_lines_pending") or approvals.get("pending_self_lines") or []:
                    admin.post(f"payroll/runs/{run_id}/employees/{line['run_employee_id']}/approve-self-line",
                               {"comment": f"{TAG}: approver's own pay line approved by the payroll manager"})
                    self.ctx.created("approver's own pay line approved by the payroll manager")
                run = admin.get(f"payroll/runs/{run_id}")
                step_up(self.ctx, admin, self.state["company_id"], self.accounts["admin"]["email"])
                admin.post(f"payroll/runs/{run_id}/finalize", {"confirm_label": run["label"],
                                                               "row_version": run["row_version"]},
                           key=f"{TAG}-{self.key}-run-{run_id}-finalize")
                self.ctx.created("run finalized by the payroll manager (step-up)")
            else:
                raise RuntimeError(f"the regular run is {status}; the seed does not drive that state")
        else:
            raise RuntimeError(f"the regular run did not finish (last status {run['status']})")
        if record["finalized"]["status"] in done:
            self.ctx.existing()

    def _blocking_codes(self, api: Api, run_id: int) -> str:
        items = items_of(api.get(f"payroll/runs/{run_id}/exceptions", status="OPEN", severity="BLOCKING", limit=500))
        counts: Dict[str, int] = {}
        for item in items:
            counts[item["code"]] = counts.get(item["code"], 0) + 1
        return ", ".join(f"{code} x{n}" for code, n in sorted(counts.items())) or "no open blocking exception"

    def _waive_by_design(self, api: Api, run_id: int) -> None:
        """People without a salary by design (founder, approver, trainee) are excluded: the reviewer waives
        those NO_SALARY findings with the reason; nothing else is waived."""
        for item in items_of(api.get(f"payroll/runs/{run_id}/exceptions", status="OPEN", code="NO_SALARY",
                                     limit=200)):
            api.post(f"payroll/exceptions/{item['id']}/waive",
                     {"reason": f"{TAG}: no salary by design (founder, approver or trainee); excluded from this run",
                      "row_version": item.get("row_version")})
            self.ctx.created(f"NO_SALARY finding waived for employee {item.get('employee_id')}")

    def _acknowledge_variance(self, api: Api, run_id: int) -> None:
        variance = api.get(f"payroll/runs/{run_id}/variance", only_flagged="true", limit=200)
        for item in items_of(variance):
            if item.get("requires_acknowledgment") and not item.get("acknowledged"):
                api.post(f"payroll/runs/{run_id}/variance/{item['run_employee_id']}/acknowledge",
                         {"comment": f"{TAG}: first payroll of the seeded company (no previous period)"})

    def ensure_draft_run(self) -> None:
        gkey = self.spec["draft_group"]
        anchor = self.ctx.store["anchors"]["periods"][self.key][gkey]
        day = _d(anchor["start"])
        if gkey == self.spec["finalize_group"]:
            day = _d(anchor["end"]) + timedelta(days=1)  # the next period of the finalized group
        period = self.period_of(gkey, day)
        processor = self.session("processor")
        runs = [r for r in self._runs(processor, gkey, period["id"]) if r.get("status") != "CANCELLED"]
        if runs:
            run = runs[0]
            self.ctx.existing()
        else:
            run = processor.post("payroll/runs", {"run_type": "REGULAR", "pay_group_id": self.ids["pay_groups"][gkey],
                                                  "pay_period_id": period["id"],
                                                  "label": f"{self.spec['name']} {period['start']} draft (UI testing)"},
                                 key=f"{TAG}-{self.key}-draft-create")
            self.ctx.created(f"draft run {run['number']}")
        self.state.setdefault("runs", {})["draft"] = {"id": run["id"], "pay_group": gkey,
                                                      "period": [period["start"], period["end"]],
                                                      "status": run["status"]}

    def ensure_retro_correction(self) -> None:
        """Gamma: a backdated salary correction inside the finalized period, then arrears detection."""
        for pkey, person in self.ids["people"].items():
            rate = person.get("extras", {}).get("retro")
            if not rate:
                continue
            period = self.ctx.store["anchors"]["periods"][self.key][person["pay_group"]]
            current = self.admin.get(f"payroll/employees/{person['employee_id']}/compensation")
            records = current.get("assignments") or current.get("history") or items_of(current)
            if any(r.get("reason") == "CORRECTION" for r in records):
                self.ctx.existing()
            else:
                proposed = self.session("processor").post(
                    f"payroll/employees/{person['employee_id']}/compensation", {
                        "template_version_id": self.ids["structures"][person["structure"]],
                        "pay_basis_amount": money(rate, self.cur), "effective_from": period["start"],
                        "reason": "CORRECTION", "note": f"{TAG}: backdated rate correction (retro test)",
                        "submit": True})
                if proposed.get("status") == "PENDING_APPROVAL":
                    self.session("approver").post(f"payroll/compensation/{proposed['id']}/approve",
                                                  {"remarks": f"{TAG}: correction approved"})
                self.ctx.created(f"backdated correction {rate} {self.cur}/h from {period['start']} for {pkey}")
            self.session("processor").post("payroll/arrears-proposals/detect", {"employee_ids": [person["employee_id"]]})

    def ensure_leaver(self) -> None:
        for pkey, person in self.ids["people"].items():
            if not person.get("extras", {}).get("leaver"):
                continue
            last_day = self.ctx.store["anchors"]["leavers"][self.key]
            employee = self.admin.get(f"employees/{person['employee_id']}")
            if employee.get("termination_date"):
                self.ctx.existing()
                continue
            self.session("hr").post(f"employees/{person['employee_id']}/termination",
                                    {"last_working_day": last_day, "note": f"{TAG}: resignation (test data)"},
                                    key=f"{TAG}-{self.key}-{pkey}-termination")
            self.ctx.created(f"leaver {pkey} last working day {last_day}")

    # -------------------------------------------------------------- everything
    def prepare(self) -> None:
        """Tenant, organisation and pay groups (the pay periods anchor the rest of the dataset)."""
        self.log("tenant and organisation")
        self.ensure_admin()
        self.optional("subscription plan (platform operator)", self.ensure_plan)
        self.ensure_company_settings()
        self.ensure_entities()
        self.ensure_work_weeks()
        self.ensure_branches()
        self.ensure_departments()
        self.ensure_pay_groups()

    def run(self) -> None:
        self.log("roles and people")
        self.ensure_roles()
        self.ensure_people()
        self.optional("brand", self.ensure_brand)
        self.optional("holidays", self.ensure_holidays)
        self.log("payroll configuration")
        self.ensure_payroll_settings()
        self.ensure_policies()
        self.ensure_components()
        self.ensure_structures()
        self.ensure_compensation()
        self.log("inputs")
        self.optional("leaver (termination)", self.ensure_leaver)
        self.optional("approved leave", self.ensure_leave)
        self.optional("unpaid leave (LOP)", self.ensure_lop)
        self.optional("attendance", self.ensure_attendance)
        self.optional("overtime", self.ensure_overtime)
        self.optional("wage inputs (bonus, commission, trip pay)", self.ensure_wage_inputs)
        self.optional("tasks and rate cards", self.ensure_tasks)
        self.optional("reimbursement claims", self.ensure_claims)
        self.optional("bank accounts", self.ensure_bank_accounts)
        self.optional("statutory attestation (I-15)", self.ensure_attestation)
        self.log("runs")
        self.optional("regular run: calculate, approve, finalize", self.ensure_finalized_run)
        # Advances need the monthly net of a finalized run (F-36), so they follow it and recover next period.
        self.optional("loans and advances", self.ensure_loans)
        self.optional("open draft run", self.ensure_draft_run)
        self.optional("backdated salary correction (retro)", self.ensure_retro_correction)
        self.state["ids"] = {k: v for k, v in self.ids.items() if k != "people"}
        self.state["people"] = {k: {f: v.get(f) for f in ("employee_id", "user_id", "email", "name", "code",
                                                           "hire_date", "pay_group", "branch", "department",
                                                           "manager", "position", "structure", "basis", "roles")}
                                for k, v in self.ids["people"].items()}


def local_time(day: date, hhmm: str, tz: str) -> str:
    import pytz

    h, m = (int(x) for x in hhmm.split(":"))
    return pytz.timezone(tz).localize(datetime(day.year, day.month, day.day, h, m)).isoformat()


def wait_status(api: Api, run_id: int, done: tuple, timeout: float = 300.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        run = api.get(f"payroll/runs/{run_id}")
        if run["status"] in done:
            return run
        if time.monotonic() > deadline:
            raise RuntimeError(f"run {run_id} still {run['status']} after {timeout:.0f}s")
        time.sleep(1.0)


def tiny_png() -> bytes:
    return base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def bank_details(country: str, currency: str, person: dict, variant: int) -> dict:
    """Fictitious, format-valid bank details labelled as test data (never a real person's account)."""
    seq = f"{person['employee_id']:06d}{variant}"
    holder = person["name"]
    if country == "IN":
        return {"holder_name": holder, "country": "IN", "currency": currency, "account_number": f"9{seq}00012",
                "routing": {"ifsc": TEST_IFSC}, "account_type": "SAVINGS", "bank_name": "Test Bank (fictitious)"}
    if country == "AE":
        return {"holder_name": holder, "country": "AE", "currency": currency, "iban": test_iban_ae(f"7{seq}"),
                "bank_name": "Test Bank (fictitious)"}
    return {"holder_name": holder, "country": "US", "currency": currency, "account_number": f"55{seq}",
            "routing": {"aba": TEST_ABA}, "account_type": "CHECKING", "bank_name": "Test Bank (fictitious)"}


# ---------------------------------------------------------------------------------------------- anchors
def compute_anchors(today: date) -> dict:
    """Dates fixed on the first run: today, the settings epoch, Gamma's bi-weekly anchor and leaver date."""
    epoch = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    pivot = today - timedelta(days=4)
    anchor = pivot - timedelta(days=pivot.weekday())  # the Monday of the week 4 days ago
    return {"today": _iso(today), "epoch": _iso(epoch), "biweekly_anchor": _iso(anchor), "periods": {},
            "leavers": {"gamma": _iso(today - timedelta(days=1))}}


def record_periods(seed: CompanySeed) -> None:
    anchors = seed.ctx.store["anchors"]
    periods = anchors["periods"].setdefault(seed.key, {})
    today = _d(anchors["today"])
    for gkey, *_ in seed.spec["pay_groups"]:
        if gkey not in periods:
            period = seed.period_of(gkey, today)
            periods[gkey] = {"start": period["start"], "end": period["end"]}


# ---------------------------------------------------------------------------------------------- guide
def frontend_base(store: dict, slug: str) -> str:
    port = store.get("frontend_port", 3400)
    if store.get("tenant_hosts"):
        return f"http://{slug}.hrms.localhost:{port}/login"
    return f"http://localhost:{port}/login"


def scope_text(spec: dict, person: dict, store_company: dict) -> str:
    extras = person.get("extras", {}) if isinstance(person.get("extras"), dict) else {}
    group = person.get("pay_group") or ""
    branch = next((b for b in spec["branches"] if b[0] == person.get("branch")), None)
    entity = branch[1] if branch else spec["entity"]["code"]
    text = f"{entity} / pay group {next((g[2] for g in spec['pay_groups'] if g[0] == group), group or '-')}"
    if extras.get("scope_entity"):
        text += f"; payroll scope LEGAL_ENTITY={extras['scope_entity']} only"
    return text


def write_guide(store: dict, path_json: Path, path_md: Path) -> None:
    path_json.parent.mkdir(parents=True, exist_ok=True)
    path_json.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = [
        "# Payroll v2 test accounts and dataset (development only, git-ignored)", "",
        f"Generated {store.get('generated_at')} by `scripts/seed_payroll_three_companies.py` (dataset v{DATASET_VERSION}). "
        f"Passwords: one random password per account, stored only in `{path_json.name}` "
        "(`companies.<company>.accounts.<key>.password`). Never commit, paste or screenshot them.", "",
        "All companies, people, e-mail domains, registration numbers and **bank details are fictitious test data** "
        "(checksum-valid test IBANs with bank code 999, IFSC `TEST0000001`, ABA `123456780`). No real money moves; "
        "payment adapters are file/sandbox only. Multipliers, allowances, holiday and overtime rules are company "
        "policy values entered by each administrator, not statutory facts. Country packs: IN runs the development "
        "**fixture** pack (\"FIXTURE - not law\"); AE and US are draft/CONFIG_ONLY, so Beta and Gamma finalize only "
        "behind the maker-checker statutory attestation (I-15) and their payslips say statutory deductions were "
        "not calculated.", "",
        f"Anchors: today {store['anchors']['today']}, settings epoch {store['anchors']['epoch']}.", "",
    ]
    for ckey, company in store.get("companies", {}).items():
        spec = COMPANIES[ckey]
        url = frontend_base(store, company.get("slug", ""))
        lines += [f"## {spec['name']} — \"{spec['brand']['product_name']}\" ({spec['country']}, {spec['currency']}, "
                  f"{spec['timezone']})", "",
                  f"Login URL: {url} · company id {company.get('company_id')} · slug `{company.get('slug')}`", "",
                  "| Account | E-mail | Roles | Credential location | Expected permissions | LE / pay-group scope | "
                  "Linked employee |", "|---|---|---|---|---|---|---|"]
        people = company.get("people", {})
        for label, pkey in spec["accounts"].items():
            account = company.get("accounts", {}).get(pkey, {})
            person = people.get(pkey, {})
            roles = account.get("roles", [])
            expected = " ".join(EXPECTED.get(r, "") for r in roles if r in EXPECTED and r != "employee") or \
                EXPECTED["employee"]
            if label.startswith("Denial"):
                expected = f"DENIAL-ONLY. {expected}"
            if "no compensation visibility" in label:
                expected += " Deliberately has no payroll:compensation:read: salary pages and amounts answer 403/404."
            spec_person = next((p for p in spec["people"] if p[0] == pkey), None)
            extras = spec_person[10] if spec_person else {}
            person_scope = scope_text(spec, {**person, "extras": extras}, company) if spec_person else \
                f"{spec['entity']['code']} (company-wide payroll via Payroll Manager)"
            lines.append(f"| {label} | {account.get('email', '-')} | {', '.join(roles)} | "
                         f"`{path_json.name}` → companies.{ckey}.accounts.{pkey}.password | {expected} | "
                         f"{person_scope} | {person.get('code', 'founder')} {person.get('name', account.get('name', ''))} "
                         f"({person.get('position', 'administrator')}) |")
        runs = company.get("runs", {})
        lines += ["", "Seeded data: " + "; ".join(seeded_facts(ckey)), ""]
        if runs:
            lines.append("Runs: " + "; ".join(f"{k} run #{v.get('id')} {v.get('pay_group')} {v.get('period')} "
                                               f"→ {v.get('status')}" for k, v in runs.items()))
            lines.append("")
    blocked = store.get("blocked") or []
    lines += ["## Blocked or refused steps", ""]
    if blocked:
        lines += ["| Company | Step | Endpoint | Status | Code | Detail |", "|---|---|---|---|---|---|"]
        for b in blocked:
            detail = str(b.get("detail", "")).replace("|", "/").replace("\n", " ")[:200]
            lines.append(f"| {b['company']} | {b['step']} | {b.get('endpoint')} | {b.get('status')} | {b.get('code')} | "
                         f"{detail} |")
    else:
        lines.append("None.")
    path_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def seeded_facts(ckey: str) -> List[str]:
    spec = COMPANIES[ckey]
    facts = []
    for p in spec["people"]:
        key, first, last, _roles, _b, dept, _m, position, structure, basis, extras = p
        bits = [f"{first} {last} ({dept}): " + (f"{structure} {basis} {spec['currency']}" if structure else
                                                 "NO salary assignment (excluded: NO_SALARY)")]
        for name, label in (("fte", "FTE"), ("joiner", "joined period day +"), ("advance", "advance"),
                            ("loan", "loan"), ("claim", "approved claim"), ("bonus", "bonus"),
                            ("commission", "commission"), ("trip_pay", "trip pay"), ("retro", "backdated rate")):
            if extras.get(name):
                bits.append(f"{label} {extras[name]}")
        for name, label in (("leave", "approved leave"), ("overtime", "approved overtime"), ("lop_day", "1 LOP day"),
                            ("task", "approved task"), ("leaver", "mid-period leaver"),
                            ("attendance_hours", "hourly punches"), ("no_comp_visibility", "no comp visibility")):
            if extras.get(name):
                bits.append(label)
        if extras.get("bank"):
            bits.append(f"bank: {extras['bank']}")
        facts.append(", ".join(bits))
    return facts


# ---------------------------------------------------------------------------------------------- entry points
def load_store(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def run_seed(make_client: Callable[[], Any], *, store: dict, outbox_dir: Path, platform_password: Optional[str],
             before_login: Optional[Callable[[], None]] = None, today: Optional[date] = None,
             companies: Optional[List[str]] = None, log: Callable[[str], None] = print) -> Context:
    """Seed (or re-check) the dataset; returns the context with stats and blocked steps. Raises on core failures."""
    ctx = Context(make_client=make_client, store=store, outbox_dir=outbox_dir, platform_password=platform_password,
                  before_login=before_login, today=today, log=log)
    store.setdefault("dataset_version", DATASET_VERSION)
    store.setdefault("anchors", compute_anchors(today or date.today()))
    store["blocked"] = []
    for key in companies or list(COMPANIES):
        seed = CompanySeed(ctx, key, COMPANIES[key])
        seed.prepare()
        record_periods(seed)
        seed.run()
    store["blocked"] = ctx.blocked
    store["generated_at"] = store.get("generated_at") or datetime.now().isoformat(timespec="seconds")
    store["last_run_at"] = datetime.now().isoformat(timespec="seconds")
    store["last_run_stats"] = dict(ctx.stats)
    return ctx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://localhost:8400/api/v1")
    parser.add_argument("--frontend-port", type=int, default=3400)
    parser.add_argument("--outbox", default=str(DEFAULT_OUTBOX), help="the stack's EMAIL_OUTBOX_DIR")
    parser.add_argument("--platform-password", default="Platform@12345",
                        help="platform operator password of the stack (sets the subscription plan)")
    parser.add_argument("--company", action="append", choices=list(COMPANIES), help="seed only these companies")
    parser.add_argument("--credentials", default=str(CREDENTIALS_JSON))
    args = parser.parse_args()
    if ":8000" in args.api or ":3000" in args.api:
        sys.exit("Ports 8000/3000 belong to the user's demo stack; use the payroll stack (8400).")
    import httpx

    try:
        httpx.get(args.api.rsplit("/api/", 1)[0] + "/health", timeout=10).raise_for_status()
    except Exception:  # noqa: BLE001
        sys.exit("The API is not reachable. Start it first: python scripts/payroll_stack.py start")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from payroll_stack import tenant_hosts_enabled
        hosts = tenant_hosts_enabled()
    except Exception:  # noqa: BLE001
        hosts = False
    path_json = Path(args.credentials)
    path_md = path_json.with_suffix(".md")
    store = load_store(path_json)
    store.update(api=args.api, frontend_port=args.frontend_port, tenant_hosts=hosts)
    try:
        ctx = run_seed(http_client_factory(args.api), store=store, outbox_dir=Path(args.outbox),
                       platform_password=args.platform_password, companies=args.company)
    finally:
        write_guide(store, path_json, path_md)  # passwords already issued must never be lost
    print(f"[payroll-seed] done: {ctx.stats['created']} created, {ctx.stats['existing']} already present, "
          f"{len(ctx.blocked)} blocked. Guide: {path_md} (git-ignored).")


if __name__ == "__main__":
    main()

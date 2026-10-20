"""Seed the three-company SaaS test environment (non-production databases only).

    python scripts/seed_saas_three_companies.py --api http://localhost:8300/api/v1 \
        --frontend http://localhost:3300 --dns-file logs/saas-dns.json

Start the stack first with per-tenant hosts (see docs/saas/C-three-company-seed.md):

    PLATFORM_HOSTS=localhost,127.0.0.1 PLATFORM_BASE_DOMAIN=hrms.localhost PROXY_SHARED_SECRET=<32+ chars>
    CUSTOM_DOMAIN_CNAME_TARGET=tenants.hrms.localhost DOMAIN_DNS_PROVIDER=fixture DOMAIN_DNS_FIXTURE_FILE=logs/saas-dns.json
    TLS_PROVIDER=local-dev FRONTEND_URL=http://localhost:3300
    python scripts/demo_stack.py --fresh --data-dir .local-postgres-saas --port 8300

Everything goes through the public API, as the platform operator and as each company's
own users, so the seed exercises the same rules as the product:

* Alpha Technologies (India, INR, direct customer, Enterprise): "Alpha People", indigo on white.
* Beta Logistics (UAE, AED, via partner Northwind, Professional): "Beta Workforce", teal on charcoal;
  an Operations Manager whose custom role covers the Dubai branch only; a pending custom domain.
* Gamma Solutions (USA, USD, via partner Northwind, Standard): "Gamma HR Cloud", orange and navy;
  a verified custom domain (DNS fixture, simulated TLS).
* Partners Northwind Partners (two tenants) and Southern Cross Resellers (none), each with an administrator.

For every company: legal entity, two branches, four departments and a team, a reporting tree
at least three levels deep, salary structures, last month's payroll, a week of attendance with
lunch breaks, leave (approved and pending), company and regional holidays, assigned assets,
published brand with logos. Gamma's roles (as specified) have no second payroll user, so its
payroll stays a draft: separation of duties forbids approving one's own preparation.

One random password per run is written with every login to .local-secrets/saas-e2e.json, and a
readable account reference to .local-secrets/saas-test-accounts.md (both git-ignored). Running
it again needs a fresh database (--fresh); all names are fictitious.
"""

import argparse
import json
import secrets
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
import httpx
import pytz

sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_demo import Api  # noqa: E402
from seed_white_label_e2e import square_mark, upload, wordmark  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ROOT / ".local-secrets" / "saas-e2e.json"
ACCOUNTS = ROOT / ".local-secrets" / "saas-test-accounts.md"
PLATFORM_EMAIL = "ops@hrms-platform.io"
SELF = [("employee:read", "SELF"), ("attendance:read", "SELF"), ("attendance:mark", "SELF"), ("leave:read", "SELF"),
        ("leave:apply", "SELF"), ("holiday:read", "COMPANY"), ("payroll:read", "SELF"), ("asset:read", "SELF")]
TEAM_LEAD = [("employee:read", "DIRECT_REPORTS"), ("attendance:read", "DIRECT_REPORTS"), ("leave:read", "DIRECT_REPORTS"),
             ("leave:approve", "DIRECT_REPORTS"), ("report:read", "DIRECT_REPORTS")]
OPERATIONS_MANAGER = [("employee:read", "BRANCH"), ("attendance:read", "BRANCH"), ("leave:read", "BRANCH"),
                      ("leave:approve", "BRANCH"), ("report:read", "BRANCH")]

# What each role may do and see, for the readable account reference (checked by the tests).
EXPECTED = {
    "company_admin": ("Everything in the company, including branding, domains and roles", "COMPANY"),
    "hr_admin": ("Employees, attendance, leave, holidays, assets, users and roles; email wording and branches; "
                 "no payroll or bank data beyond their own", "COMPANY (HR); SELF for pay"),
    "payroll_manager": ("Payroll prepare/approve/export and compensation; no user or role administration",
                        "COMPANY (payroll); cannot approve own preparation or own pay"),
    "manager": ("Employees, attendance, leave and reports of their reporting tree; approve leave", "REPORTING_TREE"),
    "Team Lead": ("Direct reports' employee, attendance and leave records; approve their leave", "DIRECT_REPORTS"),
    "Operations Manager": ("Employees, attendance and leave of the Dubai branch; approve their leave", "BRANCH (Dubai)"),
    "employee": ("Self-service: own profile, attendance, leave, payslips, assets", "SELF"),
}

COMPANIES = {
    "alpha": {
        "name": "Alpha Technologies", "timezone": "Asia/Kolkata", "currency": "INR", "country": "IN", "domain": "alpha-tech.example",
        "admin": ("Aarav", "Mehta"), "plan": "enterprise", "partner": None, "prefix": "AT",
        "entity": {"legal_name": "Alpha Technologies Pvt Ltd", "country_code": "IN", "base_currency": "INR", "timezone": "Asia/Kolkata",
                   "registered_address": {"line1": "14 Residency Road", "city": "Bengaluru", "subdivision_code": "IN-KA",
                                          "postal_code": "560025"},
                   "registration_lines": [{"label": "CIN", "value": "U72900KA2018PTC111222"}, {"label": "PAN", "value": "AAFCA4321Q"}]},
        "branches": [("blr", "Bengaluru", "BLR", "IN-KA", "Asia/Kolkata"), ("del", "Delhi", "DEL", "IN-DL", "Asia/Kolkata")],
        "departments": ["HR", "Engineering", "Finance", "Operations"], "team": ("Platform Team", "Engineering"),
        "admin_place": ("HR", "Managing Director"),
        "custom_roles": {"Team Lead": TEAM_LEAD + SELF},
        # key, first, last, roles, branch, department, manager, position, monthly salary
        "people": [
            ("hr", "Neha", "Sharma", ["hr_admin"], "del", "HR", "admin", "HR Administrator", 140000),
            ("finance", "Karan", "Malhotra", ["payroll_manager"], "del", "Finance", "admin", "Finance Manager", 160000),
            ("deptmgr", "Vikram", "Nair", ["manager"], "blr", "Engineering", "admin", "Engineering Manager", 210000),
            ("teamlead", "Sneha", "Kapoor", ["employee", "Team Lead"], "blr", "Platform Team", "deptmgr", "Team Lead", 150000),
            ("emp1", "Rohan", "Gupta", ["employee"], "blr", "Platform Team", "teamlead", "Software Engineer", 95000),
            ("emp2", "Priya", "Iyer", ["employee"], "blr", "Platform Team", "teamlead", "Software Engineer", 92000),
            ("emp3", "Arjun", "Singh", ["employee"], "del", "Operations", "finance", "Operations Analyst", 70000),
        ],
        "brand": {"product_name": "Alpha People", "short_name": "Alpha", "tagline": "People first, at Alpha",
                  "primary_color": "#4338CA", "accent_color": "#6366F1", "sidebar_style": "LIGHT", "radius": "ROUNDED",
                  "font": "POPPINS", "show_powered_by": False, "default_locale": "en-IN",
                  "legal_footer": "Alpha Technologies Pvt Ltd · 14 Residency Road, Bengaluru 560025",
                  "support_email": "people@alpha-tech.example", "support_url": "https://help.alpha-tech.example"},
        "logo_colours": ("#4338CA", "#A5B4FC"),
        "holidays": [("Diwali", "2026-11-08", None), ("Karnataka Rajyotsava", "2026-11-01", "Bengaluru")],
        "assets": [("AT-LAP-01", "ThinkPad T14", "Laptop", "emp1"), ("AT-MON-01", "Dell 27in monitor", "Monitor", None)],
        "payroll": ("finance", "admin", ["emp1", "emp2", "emp3"]),
        "leave": {"pending": "emp1", "approved": ("emp2", "teamlead")},
    },
    "beta": {
        "name": "Beta Logistics", "timezone": "Asia/Dubai", "currency": "AED", "country": "AE", "domain": "beta-logistics.example",
        "admin": ("Omar", "Al-Farsi"), "plan": "professional", "partner": "north", "prefix": "BL",
        "entity": {"legal_name": "Beta Logistics LLC", "country_code": "AE", "base_currency": "AED", "timezone": "Asia/Dubai",
                   "registered_address": {"line1": "Jebel Ali Free Zone, Plot S20104", "city": "Dubai", "subdivision_code": "AE-DU"},
                   "registration_lines": [{"label": "Trade licence", "value": "JAFZA-204871"}]},
        "branches": [("dxb", "Dubai", "DXB", "AE-DU", "Asia/Dubai"), ("auh", "Abu Dhabi", "AUH", "AE-AZ", "Asia/Dubai")],
        "departments": ["HR", "Operations", "Fleet", "Finance"], "team": ("Dispatch Team", "Fleet"),
        "admin_place": ("Operations", "General Manager"),
        "custom_roles": {"Operations Manager": OPERATIONS_MANAGER},
        "people": [
            ("hr", "Mariam", "Haddad", ["hr_admin"], "dxb", "HR", "admin", "HR Manager", 24000),
            ("payroll", "Joseph", "Mathew", ["payroll_manager"], "dxb", "Finance", "admin", "Payroll Manager", 22000),
            ("opsmgr", "Khalid", "Rahman", ["employee", "Operations Manager"], "dxb", "Operations", "admin", "Operations Manager", 26000),
            ("emp1", "Aisha", "Siddiqui", ["employee"], "dxb", "Dispatch Team", "opsmgr", "Fleet Dispatcher", 9500),
            ("emp2", "Ravi", "Menon", ["employee"], "auh", "Operations", "admin", "Warehouse Supervisor", 11000),
        ],
        "brand": {"product_name": "Beta Workforce", "short_name": "Beta", "tagline": "Moving people and freight",
                  "primary_color": "#0F766E", "accent_color": "#14B8A6", "sidebar_style": "DARK", "radius": "PILL",
                  "font": "ROBOTO", "default_locale": "en-AE", "legal_footer": "Beta Logistics LLC · Jebel Ali Free Zone, Dubai",
                  "support_email": "hr@beta-logistics.example"},
        "logo_colours": ("#0F766E", "#5EEAD4"),
        "holidays": [("UAE National Day", "2026-12-02", None), ("Commemoration Day", "2026-11-30", None)],
        "assets": [("BL-VAN-01", "Delivery van (Toyota Hiace)", "Vehicle", "emp1"), ("BL-TAB-01", "Rugged tablet", "Tablet", "emp2")],
        "payroll": ("payroll", "admin", ["emp1", "emp2", "opsmgr"]),
        "leave": {"pending": "emp2", "approved": ("emp1", "opsmgr")},
    },
    "gamma": {
        "name": "Gamma Solutions", "timezone": "America/New_York", "currency": "USD", "country": "US",
        "domain": "gamma-solutions.example", "admin": ("Emily", "Carter"), "plan": "standard", "partner": "north", "prefix": "GS",
        "entity": {"legal_name": "Gamma Solutions Inc.", "country_code": "US", "base_currency": "USD", "timezone": "America/New_York",
                   "registered_address": {"line1": "200 Park Avenue", "city": "New York", "subdivision_code": "US-NY",
                                          "postal_code": "10166"},
                   "registration_lines": [{"label": "EIN", "value": "84-1234567"}]},
        "branches": [("nyc", "New York", "NYC", "US-NY", "America/New_York"),
                     ("cal", "California", "CAL", "US-CA", "America/Los_Angeles")],
        "departments": ["Administration", "Engineering", "Sales", "Finance"], "team": ("Web Team", "Engineering"),
        "admin_place": ("Administration", "Chief Executive Officer"),
        "custom_roles": {"Team Lead": TEAM_LEAD + SELF},
        "people": [
            ("hr", "Olivia", "Martinez", ["hr_admin"], "nyc", "Administration", "admin", "HR Administrator", 8200),
            ("deptmgr", "Michael", "Brown", ["manager"], "nyc", "Engineering", "admin", "Engineering Manager", 12500),
            ("teamlead", "Jessica", "Davis", ["employee", "Team Lead"], "nyc", "Web Team", "deptmgr", "Team Lead", 10400),
            ("emp1", "David", "Wilson", ["employee"], "nyc", "Web Team", "teamlead", "Frontend Engineer", 8800),
            ("emp2", "Sarah", "Johnson", ["employee"], "cal", "Web Team", "teamlead", "Backend Engineer", 9100),
        ],
        "brand": {"product_name": "Gamma HR Cloud", "short_name": "Gamma", "tagline": "Your team, in the cloud",
                  "primary_color": "#EA580C", "accent_color": "#1E3A8A", "sidebar_style": "DARK", "radius": "SHARP",
                  "font": "INTER", "default_locale": "en-US", "support_email": "people@gamma-solutions.example"},
        "logo_colours": ("#EA580C", "#FDBA74"),
        "holidays": [("Thanksgiving Day", "2026-11-26", None), ("Cesar Chavez Day", "2027-03-31", "California")],
        "assets": [("GS-LAP-01", "MacBook Pro 14", "Laptop", "emp1"), ("GS-LAP-02", "MacBook Air 13", "Laptop", "emp2")],
        "payroll": ("admin", None, ["emp1", "emp2"]),
        "leave": {"pending": "emp2", "approved": ("emp1", "teamlead")},
    },
}


def previous_month(today: date) -> date:
    return (today.replace(day=1) - timedelta(days=1)).replace(day=1)


def recent_weekdays(today: date, count: int) -> list:
    days, day = [], today - timedelta(days=1)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return sorted(days)


def local(day: date, hhmm: str, tz: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    return pytz.timezone(tz).localize(datetime(day.year, day.month, day.day, h, m)).isoformat()


def login(base: str, email: str, password: str) -> Api:
    session = Api(base)
    session.post("/auth/login", {"email": email, "password": password})
    return session


def seed_company(base: str, ops: Api, key: str, spec: dict, partners: dict, password: str, month: date) -> dict:
    admin_email = f"admin@{spec['domain']}"
    admin = Api(base)
    me = admin.post("/auth/signup", {"company_name": spec["name"], "full_name": " ".join(spec["admin"]), "email": admin_email,
                                     "password": password, "timezone": spec["timezone"]})["me"]
    company_id = me["company"]["id"]
    ops.patch(f"/platform/companies/{company_id}", {"subscription_plan": spec["plan"], "subscription_status": "active",
                                                    **({"partner_id": partners[spec["partner"]]["id"]} if spec["partner"] else {})})
    admin.put("/settings", {"currency": spec["currency"], "companyEmail": f"hr@{spec['domain']}"})

    entity = admin.get("/legal-entities")[0]
    admin.patch(f"/legal-entities/{entity['id']}", spec["entity"])
    branches = {}
    for index, (bkey, name, code, region, tz) in enumerate(spec["branches"]):
        if index == 0:
            main = admin.get("/branches")[0]
            admin.patch(f"/branches/{main['id']}", {"name": name, "code": code, "subdivision_code": region, "timezone": tz})
            branches[bkey] = admin.get("/branches")[0]
        else:
            branches[bkey] = admin.post("/branches", {"legal_entity_id": entity["id"], "code": code, "name": name,
                                                      "subdivision_code": region, "timezone": tz})

    departments = {d["name"]: d["id"] for d in admin.get("/departments")}
    for name in spec["departments"]:
        if name not in departments:
            departments[name] = admin.post("/departments", {"name": name})["id"]
    team, parent = spec["team"]
    departments[team] = admin.post("/departments", {"name": team, "parent_department_id": departments[parent]})["id"]

    roles = {r["system_key"] or r["name"]: r["id"] for r in admin.get("/roles")}
    for name, grants in spec["custom_roles"].items():
        roles[name] = admin.post("/roles", {"name": name, "description": f"{name} (custom role)",
                                            "grants": [{"permission": p, "scope": s} for p, s in grants]})["id"]

    dept, position = spec["admin_place"]
    admin.put(f"/employees/{me['employee']['id']}", {"position": position, "department_id": departments[dept]})
    people = {"admin": {"employee_id": me["employee"]["id"], "user_id": me["user"]["id"], "email": admin_email,
                        "name": " ".join(spec["admin"]), "roles": ["company_admin"], "position": position,
                        "branch": spec["branches"][0][1], "department": dept, "manager": None}}
    sessions = {"admin": admin}
    salaries = {}
    for index, (pkey, first, last, role_names, branch, dept, manager, position, salary) in enumerate(spec["people"], start=2):
        email = f"{first.lower()}.{last.lower().replace('-', '')}@{spec['domain']}"
        employee = admin.post("/employees", {
            "first_name": first, "last_name": last, "email": email, "employee_code": f"{spec['prefix']}-{index:03d}",
            "hire_date": "2025-01-06", "position": position, "branch_id": branches[branch]["id"],
            "department_id": departments[dept], "manager_id": people[manager]["employee_id"],
        })
        invite = admin.post("/users/invite", {"email": email, "full_name": f"{first} {last}", "employee_id": employee["id"],
                                              "role_ids": [roles[r] for r in role_names]})
        session = Api(base)
        accepted = session.post("/auth/invitations/accept", {"token": invite["invite_url"].split("token=")[1], "password": password})
        people[pkey] = {"employee_id": employee["id"], "user_id": accepted["me"]["user"]["id"], "email": email,
                        "name": f"{first} {last}", "roles": role_names, "position": position,
                        "branch": next(b[1] for b in spec["branches"] if b[0] == branch), "department": dept,
                        "manager": people[manager]["name"]}
        sessions[pkey] = session
        salaries[pkey] = salary
    for dept_name, head in (("Engineering", "deptmgr"), ("Finance", "finance"), ("HR", "hr"), ("Operations", "opsmgr"),
                            ("Administration", "hr"), ("Fleet", "opsmgr")):
        if dept_name in departments and head in people:
            admin.put(f"/departments/{departments[dept_name]}", {"name": dept_name, "head_employee_id": people[head]["employee_id"]})

    if "Operations Manager" in spec["custom_roles"]:
        admin.put(f"/users/{people['opsmgr']['user_id']}/scope-targets", {"assignments": [
            {"role_id": roles["Operations Manager"], "legal_entity_ids": [], "branch_ids": [branches["dxb"]["id"]]}]})

    # Brand.
    light, dark = spec["logo_colours"]
    word = spec["brand"]["product_name"].split()[0]
    logo = upload(admin, "/branding/assets", "logo_light", wordmark(word, light), f"{key}.png")
    logo_dark = upload(admin, "/branding/assets", "logo_dark", wordmark(word, dark), f"{key}-dark.png")
    mark = upload(admin, "/branding/assets", "mark", square_mark(word[0], light), f"{key}-mark.png")
    admin.put("/branding/draft", {"changes": {**spec["brand"], "logo_light": logo["id"], "logo_dark": logo_dark["id"],
                                              "mark": mark["id"]}})
    admin.post("/branding/publish", {})

    # Module data: holidays, assets, salary structures, payroll, attendance, leave.
    for name, day, location in spec["holidays"]:
        admin.post("/holidays", {"name": name, "date": day, "location": location, "is_optional": False})
    for tag, name, category, holder in spec["assets"]:
        asset = admin.post("/assets", {"asset_tag": tag, "name": name, "category": category, "serial_number": f"SN-{tag}",
                                       "purchase_date": "2025-06-01"})
        if holder:
            admin.post(f"/assets/{asset['id']}/assign", {"employee_id": people[holder]["employee_id"]})
    for pkey, monthly in salaries.items():
        admin.post("/salary-structures", {"employee_id": people[pkey]["employee_id"], "basic_salary": str(monthly),
                                          "house_rent_allowance": str(round(monthly * 0.4, 2)), "effective_from": "2025-01-01"})
    preparer_key, approver_key, payees = spec["payroll"]
    preparer, approver = sessions[preparer_key], sessions[approver_key] if approver_key else None
    payroll_ids = {}
    for pkey in payees:
        run = preparer.post(f"/payroll/generate?employee_id={people[pkey]['employee_id']}&year={month.year}&month={month.month}")
        payroll_ids[pkey] = {"id": run["id"], "status": "draft"}
        if approver is not None:
            approver.post(f"/payroll/{run['id']}/approve")
            payroll_ids[pkey]["status"] = "approved"
    recorder = sessions["hr"]
    for pkey in [p[0] for p in spec["people"] if p[0] != "hr"]:
        for offset, day in enumerate(recent_weekdays(date.today(), 5)):
            check_in = "09:42" if offset == 2 and pkey == "emp1" else "09:05"
            recorder.post("/attendance/manual", {
                "employee_id": people[pkey]["employee_id"], "date": day.isoformat(),
                "check_in_time": local(day, check_in, spec["timezone"]), "check_out_time": local(day, "18:10", spec["timezone"]),
                "status": "late" if check_in == "09:42" else "present", "notes": "Imported from the access-control register",
                "breaks": [{"started_at": local(day, "13:00", spec["timezone"]), "ended_at": local(day, "13:40", spec["timezone"])}],
            })
    start = date.today() + timedelta(days=21)
    while start.weekday() >= 5:
        start += timedelta(days=1)
    pending = sessions[spec["leave"]["pending"]].post("/leaves/apply", {
        "leave_type": "casual", "start_date": start.isoformat(), "end_date": start.isoformat(), "reason": "Family function"})
    applicant, approver_key2 = spec["leave"]["approved"]
    approved = sessions[applicant].post("/leaves/apply", {
        "leave_type": "sick", "start_date": (start + timedelta(days=7)).isoformat(), "end_date": (start + timedelta(days=7)).isoformat(),
        "reason": "Medical appointment"})
    sessions[approver_key2].post(f"/leaves/{approved['id']}/approve", {"remarks": "Approved, get well soon"})

    return {"name": spec["name"], "company_id": company_id, "slug": me["company"]["slug"], "currency": spec["currency"],
            "timezone": spec["timezone"], "plan": spec["plan"], "partner": spec["partner"], "people": people,
            "branches": {k: {"id": v["id"], "name": v["name"]} for k, v in branches.items()},
            "departments": departments, "legal_entity": {"id": entity["id"], "legal_name": spec["entity"]["legal_name"]},
            "payroll": payroll_ids, "pending_leave_id": pending["id"], "approved_leave_id": approved["id"],
            "brand": spec["brand"]}


def domains(base: str, password: str, built: dict, dns_file: Path) -> dict:
    """Gamma: verified custom domain. Beta: pending (no DNS records). Alpha: added, then removed."""
    gamma = login(base, built["gamma"]["people"]["admin"]["email"], password)
    verified = gamma.post("/domains", {"hostname": "hr.gamma-solutions.localhost"})
    txt = next(r for r in verified["records"] if r["type"] == "TXT")
    dns_file.parent.mkdir(parents=True, exist_ok=True)
    dns_file.write_text(json.dumps({txt["name"]: {"TXT": [txt["value"]]},
                                    verified["hostname"]: {"CNAME": "tenants.hrms.localhost"}}, indent=2))
    status = gamma.post(f"/domains/{verified['id']}/verify")["status"]
    if status != "ACTIVE":
        raise SystemExit(f"Gamma's domain did not verify: {status}")
    beta = login(base, built["beta"]["people"]["admin"]["email"], password)
    beta.post("/domains", {"hostname": "people.beta-logistics.localhost"})
    alpha = login(base, built["alpha"]["people"]["admin"]["email"], password)
    removed = alpha.post("/domains", {"hostname": "old.alpha-people.localhost"})
    alpha.client.delete(f"/domains/{removed['id']}", headers=alpha._headers()).raise_for_status()
    return {"active": "hr.gamma-solutions.localhost", "pending": "people.beta-logistics.localhost",
            "removed": "old.alpha-people.localhost"}


def accounts_markdown(data: dict, frontend: str) -> str:
    port = frontend.rsplit(":", 1)[-1]
    lines = [
        "# SaaS test accounts (development only, git-ignored)", "",
        f"Generated {data['generated_at']}. Every account uses the run password stored as `password` in "
        "`.local-secrets/saas-e2e.json` (credential reference **SAAS_E2E**). Never commit or share it.", "",
        "| Company | Test login URL | Role | Test email | Credential reference | Expected permissions | Expected data scope |",
        "|---|---|---|---|---|---|---|",
    ]
    for company in data["companies"].values():
        url = f"http://{company['slug']}.hrms.localhost:{port}/login"
        for person in company["people"].values():
            role = person["roles"][-1]
            permissions, scope = EXPECTED[role]
            lines.append(f"| {company['name']} | {url} | {', '.join(person['roles'])} ({person['position']}) | {person['email']} | "
                         f"SAAS_E2E | {permissions} | {scope} |")
    lines.append(f"| Platform | {frontend}/platform/login | Platform operator | {data['platform']['email']} | "
                 "`platform.password` in saas-e2e.json | Tenants, plans, partners, platform brand; no tenant HR data | All tenants, aggregate counts |")
    for partner in data["partners"].values():
        lines.append(f"| {partner['name']} | {frontend}/partner/login | Partner administrator | {partner['admin_email']} | SAAS_E2E | "
                     "Own clients (aggregate counts) and own brand; no tenant HR data | Own partner's tenants only |")
    lines += ["", "Hosts: tenants on `<slug>.hrms.localhost`, Gamma also on `hr.gamma-solutions.localhost` (verified custom "
              "domain); `people.beta-logistics.localhost` is pending and `old.alpha-people.localhost` was removed, so both "
              "show the neutral unknown-address page."]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8300/api/v1")
    parser.add_argument("--frontend", default="http://localhost:3300")
    parser.add_argument("--dns-file", default=str(ROOT / "logs" / "saas-dns.json"))
    parser.add_argument("--platform-password", default="Platform@12345")
    args = parser.parse_args()
    base = args.api
    try:
        httpx.get(base.rsplit("/api/", 1)[0] + "/health", timeout=10).raise_for_status()
    except Exception:
        sys.exit("The API is not reachable. Start the stack first (see this file's docstring).")

    password = "Saas" + secrets.token_urlsafe(15) + "7"
    month = previous_month(date.today())
    ops = Api(base)
    ops.post("/platform/auth/login", {"email": PLATFORM_EMAIL, "password": args.platform_password})

    partners = {}
    north = ops.post("/platform/partners", {
        "name": "Northwind Partners", "billing_mode": "PARTNER_BILLS_TENANT",
        "tenant_entitlements": {"branding.product_name": True, "branding.remove_powered_by": False, "domains.custom": True,
                                "email.template_overrides": True}})
    mark = upload(ops, f"/platform/brands/partners/{north['id']}/assets", "mark", square_mark("N", "#334155"), "northwind.png")
    ops.put(f"/platform/brands/partners/{north['id']}", {
        "changes": {"product_name": "Northwind HR", "show_powered_by": True, "primary_color": "#334155", "mark": mark["id"],
                    "support_url": "https://support.northwind-partners.example"},
        "locked_fields": ["show_powered_by"], "powered_by_label": "Powered by Northwind"})
    ops.post(f"/platform/brands/partners/{north['id']}/publish")
    south = ops.post("/platform/partners", {"name": "Southern Cross Resellers"})
    for pkey, partner, email, name in (("north", north, "admin@northwind-partners.example", "Nora Whitfield"),
                                       ("south", south, "admin@southern-cross.example", "Sam Okafor")):
        ops.post(f"/platform/partners/{partner['id']}/admins", {"email": email, "full_name": name, "password": password})
        partners[pkey] = {"id": partner["id"], "name": partner["name"], "slug": partner["slug"], "admin_email": email}

    built = {key: seed_company(base, ops, key, spec, partners, password, month) for key, spec in COMPANIES.items()}
    hosts = domains(base, password, built, Path(args.dns_file))

    data = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "password": password,
        "frontend": args.frontend,
        "platform": {"email": PLATFORM_EMAIL, "password": args.platform_password},
        "partners": partners,
        "payroll_month": month.isoformat(),
        "domains": hosts,
        "companies": built,
    }
    SECRETS.parent.mkdir(parents=True, exist_ok=True)
    SECRETS.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    ACCOUNTS.write_text(accounts_markdown(data, args.frontend), encoding="utf-8")
    print(f"Seeded {', '.join(c['name'] for c in built.values())} and two partners.")
    print(f"Logins: {ACCOUNTS} ; machine-readable: {SECRETS} (both git-ignored).")


if __name__ == "__main__":
    main()

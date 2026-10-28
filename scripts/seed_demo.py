"""Seed demo companies through the public API (no database back doors).

    python scripts/seed_demo.py [--api http://localhost:8000/api/v1]

Run it once against a fresh database (python scripts/demo_stack.py --fresh).
It creates two fully populated companies:

* Nimbus Technologies - IT services, five management levels, custom roles
  ("Team Lead", "Recruiter"), a payroll team with separation of duties, an
  auditor, a functional (secondary) manager, attendance, leave in every
  state, assets, location-specific holidays and a processed payroll month.
* Saffron Foods - a manufacturing plant with department heads, a three-step
  conditional approval chain, shift-based late marking and button check-in.

The third company (Orbit Logistics) is created live by the browser tests.
Every account uses the password below.
"""

import argparse
import sys
from datetime import date, datetime, timedelta, timezone

import httpx

PASSWORD = "Demo@12345"
IST = timezone(timedelta(hours=5, minutes=30))


class Api:
    def __init__(self, base: str):
        self.client = httpx.Client(base_url=base, timeout=60)

    def _headers(self):
        return {"X-CSRF-Token": self.client.cookies.get("hrms_csrf", "")}

    def _ok(self, response: httpx.Response):
        if response.status_code >= 400:
            raise SystemExit(f"{response.request.method} {response.request.url} -> {response.status_code}: {response.text}")
        return response.json()

    def get(self, path, **kw):
        return self._ok(self.client.get(path, **kw))

    def post(self, path, json=None, **kw):
        return self._ok(self.client.post(path, json=json, headers=self._headers(), **kw))

    def put(self, path, json=None):
        return self._ok(self.client.put(path, json=json, headers=self._headers()))

    def patch(self, path, json=None):
        return self._ok(self.client.patch(path, json=json, headers=self._headers()))


# The Employee role's self-service grants (app.core.permission_registry.SELF_SERVICE). The custom
# Recruiter role includes all of them, so the "cannot hand out more power than they hold" rule lets
# Pooja grant Employee (e2e/04). Kept equal to the registry by tests/payroll_unit/test_legacy_e2e_seed.py.
SELF_SERVICE = [
    ("employee:read", "SELF"), ("employee:read_confidential", "SELF"), ("profile:update", "SELF"),
    ("attendance:read", "SELF"), ("attendance:mark", "SELF"), ("location:read", "SELF"), ("leave:read", "SELF"),
    ("leave:apply", "SELF"),
    ("holiday:read", "COMPANY"), ("payroll:read", "SELF"), ("compensation:read", "SELF"),
    ("asset:read", "SELF"), ("asset:acknowledge", "SELF"),
    # Payroll v2 self-service (3.9 §5.3)
    ("payroll:payslip:read", "SELF"), ("payroll:compensation:read", "SELF"),
    ("payroll:bank_details:read_masked", "SELF"), ("payroll:bank_details:edit", "SELF"),
    ("payroll:tax_profile:read", "SELF"), ("payroll:tax_profile:edit", "SELF"),
    ("payroll:loans:read", "SELF"), ("payroll:reimbursements:read", "SELF"),
    ("overtime:request", "SELF"), ("task:read", "SELF"),
    # LH-M1 / AS-M1 self-service (registry 2026.10.5-2026.10.6)
    ("holiday:choose_optional", "SELF"), ("shift:read", "SELF"), ("roster:read", "SELF"), ("roster:request", "SELF"),
    ("team:read", "SELF"),
]

NIMBUS = {
    "name": "Nimbus Technologies",
    "domain": "nimbustech.io",
    "admin": ("Ananya", "Rao"),
    "admin_profile": {"position": "Chief Executive Officer", "department": "Leadership", "location": "Bengaluru", "salary": 300000},
    "hr": "vikram",
    "departments": ["Leadership", "Engineering", "People & HR", "Finance", "Sales"],
    "shifts": [("Late Shift", "12:00", "21:00", False), ("Flexible", "10:00", "19:00", True)],
    "settings": {"graceTime": 15, "companyEmail": "people@nimbustech.io", "companyAddress": "Outer Ring Road, Bengaluru"},
    "custom_roles": {
        "Team Lead": [("employee:read", "DIRECT_REPORTS"), ("attendance:read", "DIRECT_REPORTS"),
                      ("attendance:regularize", "DIRECT_REPORTS"), ("leave:read", "DIRECT_REPORTS"),
                      ("leave:approve", "DIRECT_REPORTS"), ("asset:read", "DIRECT_REPORTS"),
                      ("report:read", "DIRECT_REPORTS"), ("delegation:manage", "SELF")],
        "Recruiter": [("employee:read", "COMPANY"), ("employee:create", "COMPANY"), ("user:read", "COMPANY"),
                      ("user:invite", "COMPANY"), ("role:read", "COMPANY")] + SELF_SERVICE,
    },
    # key, first, last, position, department, manager, location, shift, roles, monthly salary
    "people": [
        ("vikram", "Vikram", "Shah", "Head of People", "People & HR", "admin", "Bengaluru", None, ["hr_admin"], 180000),
        ("pooja", "Pooja", "Sharma", "Talent Acquisition Specialist", "People & HR", "vikram", "Bengaluru", None, ["Recruiter"], 70000),
        ("priya", "Priya", "Nair", "Finance Controller", "Finance", "admin", "Mumbai", None, ["payroll_manager"], 170000),
        ("karan", "Karan", "Mehta", "Payroll Analyst", "Finance", "priya", "Mumbai", None, ["payroll_manager"], 90000),
        ("neha", "Neha", "Joshi", "Internal Auditor", "Finance", "admin", "Mumbai", None, ["auditor", "employee"], 110000),
        ("rohan", "Rohan", "Gupta", "VP Engineering", "Engineering", "admin", "Bengaluru", None, ["manager"], 250000),
        ("sneha", "Sneha", "Iyer", "Engineering Manager", "Engineering", "rohan", "Bengaluru", None, ["manager"], 160000),
        ("arjun", "Arjun", "Das", "Tech Lead", "Engineering", "sneha", "Pune", None, ["employee", "Team Lead"], 120000),
        ("divya", "Divya", "Menon", "Software Engineer", "Engineering", "arjun", "Pune", None, ["employee"], 85000),
        ("farhan", "Farhan", "Ali", "Software Engineer", "Engineering", "arjun", "Pune", "Late Shift", ["employee"], 82000),
        ("sameer", "Sameer", "Khan", "Engineering Intern", "Engineering", "arjun", "Pune", None, [], 25000),
        ("meera", "Meera", "Pillai", "QA Engineer", "Engineering", "sneha", "Bengaluru", None, ["employee"], 78000),
        ("nikhil", "Nikhil", "Jain", "DevOps Engineer", "Engineering", "rohan", "Bengaluru", "Flexible", ["employee"], 95000),
        ("tara", "Tara", "Kapoor", "Sales Director", "Sales", "admin", "Mumbai", None, ["manager"], 200000),
        ("rahul", "Rahul", "Verma", "Account Executive", "Sales", "tara", "Mumbai", None, ["employee"], 75000),
        ("isha", "Isha", "Bose", "Account Executive", "Sales", "tara", "Mumbai", None, ["employee"], 72000),
    ],
    "department_heads": {"Engineering": "rohan", "Sales": "tara", "People & HR": "vikram", "Finance": "priya", "Leadership": "admin"},
    "holidays": [
        ("Republic Day", "2026-01-26", None, False), ("Holi", "2026-03-04", None, False),
        ("Independence Day", "2026-08-15", None, False), ("Ganesh Chaturthi", "2026-09-14", "Mumbai", False),
        ("Gandhi Jayanti", "2026-10-02", None, False), ("Karnataka Rajyotsava", "2026-11-01", "Bengaluru", False),
        ("Diwali", "2026-11-08", None, False), ("Guru Nanak Jayanti", "2026-11-24", None, True),
        ("Christmas", "2026-12-25", None, False),
    ],
    "assets": [
        ("LAP-001", "MacBook Pro 14", "Laptop", "C02XK1Y2JG5H", "divya", True),
        ("LAP-002", "Dell XPS 15", "Laptop", "DX15-88213", "farhan", False),
        ("LAP-003", "Lenovo ThinkPad X1", "Laptop", "PF3K9WZ", None, False),
        ("PHN-001", "iPhone 15", "Phone", "F17Z9KQ0N72", "tara", True),
        ("MON-001", "Dell UltraSharp 27", "Monitor", "CN-0P2723", None, False),
    ],
}

SAFFRON = {
    "name": "Saffron Foods",
    "domain": "saffronfoods.in",
    "admin": ("Rajesh", "Kumar"),
    "admin_profile": {"position": "Managing Director", "department": "HR & Admin", "location": "Chennai", "salary": 220000},
    "hr": "kavita",
    "departments": ["Production", "Quality", "HR & Admin", "Accounts"],
    "shifts": [("Morning Shift", "06:00", "14:00", False), ("Evening Shift", "14:00", "22:00", False)],
    "settings": {"graceTime": 10, "requireFaceForSelfCheckIn": False, "companyEmail": "hr@saffronfoods.in",
                 "companyAddress": "SIPCOT Industrial Park, Chennai"},
    "custom_roles": {
        # Same role name as at Nimbus, completely different permissions.
        "Team Lead": [("employee:read", "DEPARTMENT"), ("attendance:read", "DEPARTMENT"),
                      ("attendance:regularize", "DEPARTMENT")],
    },
    "people": [
        ("kavita", "Kavita", "Menon", "HR Manager", "HR & Admin", "admin", "Chennai", None, ["hr_admin"], 120000),
        ("suresh", "Suresh", "Pillai", "Plant Head", "Production", "admin", "Chennai", None, ["manager"], 150000),
        ("lakshmi", "Lakshmi", "Narayanan", "Shift Supervisor", "Production", "suresh", "Chennai", "Morning Shift", ["manager", "Team Lead"], 65000),
        ("ramesh", "Ramesh", "Babu", "Machine Operator", "Production", "lakshmi", "Chennai", "Morning Shift", ["employee"], 32000),
        ("ganesh", "Ganesh", "Murthy", "Machine Operator", "Production", "lakshmi", "Chennai", "Evening Shift", ["employee"], 32000),
        ("pooja", "Pooja", "Sharma", "Quality Analyst", "Quality", "suresh", "Chennai", "Morning Shift", ["employee"], 48000),
        ("deepa", "Deepa", "Raghavan", "Accounts Manager", "Accounts", "admin", "Chennai", None, ["payroll_manager"], 90000),
        ("anil", "Anil", "Kumar", "Accounts Executive", "Accounts", "deepa", "Chennai", None, ["payroll_manager"], 45000),
    ],
    "department_heads": {"Production": "suresh", "Quality": "suresh", "Accounts": "deepa", "HR & Admin": "kavita"},
    "holidays": [
        ("Pongal", "2026-01-15", None, False), ("Tamil New Year", "2026-04-14", None, False),
        ("Independence Day", "2026-08-15", None, False), ("Ayudha Pooja", "2026-10-20", "Chennai", False),
        ("Diwali", "2026-11-08", None, False), ("Christmas", "2026-12-25", None, False),
    ],
    "assets": [
        ("SAF-HHS-01", "Zebra handheld scanner", "Scanner", "ZB-77120", "ramesh", True),
        ("SAF-PPE-01", "Safety kit (helmet, gloves, boots)", "Safety", None, "ganesh", False),
        ("SAF-TAB-01", "Samsung Galaxy Tab Active", "Tablet", "SM-T575", None, False),
    ],
}


def email(company: dict, first: str, last: str) -> str:
    return f"{first.lower()}.{last.lower()}@{company['domain']}"


def ist(day: date, hhmm: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST).isoformat()


def salary_parts(monthly: int) -> dict:
    basic = round(monthly * 0.5)
    hra = round(monthly * 0.2)
    da = round(monthly * 0.1)
    conveyance = 1600
    other = max(monthly - basic - hra - da - conveyance, 0)
    return {"basic_salary": str(basic), "house_rent_allowance": str(hra), "dearness_allowance": str(da),
            "conveyance_allowance": str(conveyance), "other_allowances": str(other)}


def build_company(base: str, spec: dict) -> dict:
    admin_first, admin_last = spec["admin"]
    admin_email = email(spec, admin_first, admin_last)
    admin = Api(base)
    me = admin.post("/auth/signup", {"company_name": spec["name"], "full_name": f"{admin_first} {admin_last}",
                                     "email": admin_email, "password": PASSWORD, "timezone": "Asia/Kolkata"})["me"]
    print(f"\n== {spec['name']} (company #{me['company']['id']})")
    admin.put("/settings", spec["settings"])

    roles = {r["system_key"] or r["name"]: r["id"] for r in admin.get("/roles")}
    for name, grants in spec["custom_roles"].items():
        created = admin.post("/roles", {"name": name, "description": f"{name} (custom role)",
                                        "grants": [{"permission": p, "scope": s} for p, s in grants]})
        roles[name] = created["id"]

    departments = {d["name"]: d["id"] for d in admin.get("/departments")}
    for name in spec["departments"]:
        if name not in departments:
            departments[name] = admin.post("/departments", {"name": name})["id"]
    shifts = {s["name"]: s["id"] for s in admin.get("/shifts")}
    for name, start, end, flexible in spec["shifts"]:
        shifts[name] = admin.post("/shifts", {"name": name, "start_time": start, "end_time": end, "is_flexible": flexible})["id"]

    ids = {"admin": me["employee"]["id"]}
    sessions = {"admin": admin}
    emails = {"admin": admin_email}
    profile = spec["admin_profile"]
    admin.put(f"/employees/{ids['admin']}", {"position": profile["position"], "department_id": departments[profile["department"]],
                                             "location": profile["location"], "shift_id": shifts["General Shift"],
                                             "base_salary": str(profile["salary"])})
    salaries = {"admin": profile["salary"]}

    for idx, (key, first, last, position, dept, manager, location, shift, role_names, salary) in enumerate(spec["people"], start=2):
        mail = email(spec, first, last)
        employee = admin.post("/employees", {
            "first_name": first, "last_name": last, "email": mail, "employee_code": f"{spec['name'][:3].upper()}-{idx:03d}",
            "position": position, "department_id": departments[dept], "shift_id": shifts[shift or "General Shift"],
            "location": location, "hire_date": "2025-04-01" if idx < 10 else "2025-11-15",
            "manager_id": ids[manager], "base_salary": str(salary),
        })
        ids[key], emails[key], salaries[key] = employee["id"], mail, salary
        if role_names:
            invite = admin.post("/users/invite", {"email": mail, "full_name": f"{first} {last}", "employee_id": employee["id"],
                                                  "role_ids": [roles[r] for r in role_names]})
            session = Api(base)
            session.post("/auth/invitations/accept", {"token": invite["invite_url"].split("token=")[1], "password": PASSWORD})
            sessions[key] = session
        print(f"   + {first} {last:<10} {position:<32} -> reports to {manager:<7} roles={role_names or 'no login'}")

    for dept, head in spec["department_heads"].items():
        admin.put(f"/departments/{departments[dept]}", {"name": dept, "head_employee_id": ids[head]})
    for key, monthly in salaries.items():
        admin.post("/salary-structures", {"employee_id": ids[key], "effective_from": "2026-04-01", **salary_parts(monthly)})
    for name, day, location, optional in spec["holidays"]:
        admin.post("/holidays", {"name": name, "date": day, "location": location, "is_optional": optional})
    for tag, name, category, serial, holder, acknowledged in spec["assets"]:
        asset = admin.post("/assets", {"asset_tag": tag, "name": name, "category": category, "serial_number": serial,
                                       "purchase_date": "2025-06-01"})
        if holder:
            admin.post(f"/assets/{asset['id']}/assign", {"employee_id": ids[holder]})
            if acknowledged:
                sessions[holder].post(f"/assets/{asset['id']}/acknowledge")
        if tag == "MON-001":
            admin.put(f"/assets/{asset['id']}/status", {"status": "maintenance"})
    return {"admin": admin, "ids": ids, "sessions": sessions, "emails": emails, "roles": roles, "spec": spec}


def seed_attendance(company: dict, days: list, patterns: dict) -> None:
    # HR records attendance from the register; nobody may correct their own.
    hr_key = company["spec"]["hr"]
    ids = company["ids"]
    holidays = {h[1] for h in company["spec"]["holidays"] if h[2] is None}
    count = 0
    for key, (check_in, check_out, exceptions) in patterns.items():
        for day in days:
            if day.isoformat() in holidays:
                continue
            status, cin, cout = "present", check_in, check_out
            special = exceptions.get(day.isoformat())
            if special == "absent":
                continue
            if special == "late":
                status, cin = "late", (datetime.strptime(check_in, "%H:%M") + timedelta(minutes=37)).strftime("%H:%M")
            if special == "half-day":
                status, cout = "half-day", (datetime.strptime(check_in, "%H:%M") + timedelta(hours=4)).strftime("%H:%M")
            recorder = company["admin"] if key == hr_key else company["sessions"][hr_key]
            recorder.post("/attendance/manual", {"employee_id": ids[key], "date": day.isoformat(), "check_in_time": ist(day, cin),
                                              "check_out_time": ist(day, cout), "status": status,
                                              "notes": "Imported from biometric register"})
            count += 1
    print(f"   attendance: {count} records")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000/api/v1")
    args = parser.parse_args()
    try:
        httpx.get(args.api.rsplit("/api/", 1)[0] + "/health", timeout=10).raise_for_status()
    except Exception:
        sys.exit("The API is not reachable. Start it first: python scripts/demo_stack.py --fresh")

    today = datetime.now(IST).date()
    weekdays = [today - timedelta(days=d) for d in range(14, 0, -1) if (today - timedelta(days=d)).weekday() < 5]

    # ------------------------------------------------------------- Nimbus
    nimbus = build_company(args.api, NIMBUS)
    ids, s = nimbus["ids"], nimbus["sessions"]
    s["admin"].post("/org/secondary-lines", {"employee_id": ids["meera"], "manager_id": ids["tara"], "relation_type": "project",
                                            "effective_from": (today - timedelta(days=17)).isoformat()})
    s["sneha"].post("/org/delegations", {"delegate_employee_id": ids["rohan"], "reason": "Conference travel",
                                         "starts_on": (today - timedelta(days=8)).isoformat(),
                                         "ends_on": (today - timedelta(days=4)).isoformat()})
    seed_attendance(nimbus, weekdays, {
        "admin": ("09:20", "19:10", {}),
        "rohan": ("09:05", "18:40", {}),
        "sneha": ("09:00", "18:15", {}),
        "arjun": ("09:10", "18:30", {weekdays[2].isoformat(): "late"}),
        "divya": ("09:02", "18:05", {}),
        "farhan": ("12:05", "21:10", {weekdays[1].isoformat(): "late", weekdays[6].isoformat(): "late"}),
        "meera": ("08:55", "17:50", {weekdays[4].isoformat(): "half-day"}),
        "nikhil": ("10:30", "19:30", {weekdays[3].isoformat(): "absent"}),
        "tara": ("09:30", "19:00", {}),
        "rahul": ("09:12", "18:20", {}),
        "isha": ("09:08", "18:02", {weekdays[5].isoformat(): "absent"}),
        "vikram": ("09:15", "18:30", {}),
        "priya": ("09:25", "18:45", {}),
        "karan": ("09:05", "18:10", {}),
        "pooja": ("09:10", "18:00", {}),
        "neha": ("09:40", "18:20", {}),
        "sameer": ("09:30", "17:30", {}),
    })
    # A few people already at work today (on-call / weekend release).
    for key, cin in (("nikhil", "08:40"), ("rohan", "09:10"), ("arjun", "09:25")):
        s["admin"].post("/attendance/manual", {"employee_id": ids[key], "date": today.isoformat(),
                                               "check_in_time": ist(today, cin), "status": "present",
                                               "notes": "Release on-call"})

    def nxt(days: int) -> str:
        # A date that falls on a weekend moves to the Monday after, so a one-day leave always has a working day.
        day = today + timedelta(days=days)
        return (day + timedelta(days=(7 - day.weekday()) % 7 if day.weekday() >= 5 else 0)).isoformat()

    leave = lambda who, kind, a, b, why: s[who].post("/leaves/apply", {"leave_type": kind, "start_date": a, "end_date": b, "reason": why})
    leave("divya", "casual", nxt(8), nxt(8), "Family function")                             # pending at Arjun (Team Lead)
    meera = leave("meera", "earned", nxt(22), nxt(25), "Vacation in Kerala")              # 4 days -> manager then HR
    s["sneha"].post(f"/leaves/{meera['id']}/approve", {"remarks": "Enjoy!"})              # now waiting on HR
    rahul = leave("rahul", "casual", nxt(15), nxt(16), "Personal work")
    s["tara"].post(f"/leaves/{rahul['id']}/reject", {"remarks": "Quarter-end closing that week"})
    isha = leave("isha", "casual", nxt(5), nxt(5), "Doctor appointment")
    s["tara"].post(f"/leaves/{isha['id']}/approve", {"remarks": "Approved"})
    leave("nikhil", "sick", nxt(2), nxt(2), "Dental procedure")                            # pending at Rohan
    leave("farhan", "casual", nxt(11), nxt(12), "Moving house")                            # pending at Arjun
    print("   leave: 6 requests (pending, approved, rejected, multi-step)")

    # Payroll for last month: prepared by Karan, approved by Priya, paid by Karan.
    last_month = (today.replace(day=1) - timedelta(days=1))
    run = s["karan"].post(f"/payroll/generate-month?year={last_month.year}&month={last_month.month}")
    records = s["priya"].get("/payroll", params={"year": last_month.year, "month": last_month.month, "limit": 200})["items"]
    for record in records:
        if record["employee_id"] != ids["priya"]:
            s["priya"].post(f"/payroll/{record['id']}/approve")
    # Priya cannot approve her own pay; the CEO approves it.
    own = next(r for r in records if r["employee_id"] == ids["priya"])
    s["admin"].post(f"/payroll/{own['id']}/approve")
    s["karan"].post(f"/payroll/process-bulk?year={last_month.year}&month={last_month.month}")
    # Karan may not pay himself; the CEO processes the one the bulk run refused.
    for record in s["admin"].get("/payroll", params={"year": last_month.year, "month": last_month.month,
                                                       "status": "approved", "limit": 200})["items"]:
        s["admin"].post(f"/payroll/{record['id']}/process")
    print(f"   payroll {last_month:%B %Y}: {run['summary']['generated']} payslips prepared, approved and paid")

    # ------------------------------------------------------------- Saffron
    saffron = build_company(args.api, SAFFRON)
    ids2, s2 = saffron["ids"], saffron["sessions"]
    s2["admin"].put("/leaves/approval-chain", {"steps": [
        {"approver_type": "primary_manager"},
        {"approver_type": "department_head", "min_days": 2},
        {"approver_type": "role", "role_id": saffron["roles"]["hr_admin"], "min_days": 5},
    ]})
    seed_attendance(saffron, weekdays, {
        "suresh": ("08:30", "17:30", {}),
        "lakshmi": ("05:50", "14:05", {}),
        "ramesh": ("05:55", "14:00", {weekdays[2].isoformat(): "late"}),
        "ganesh": ("13:55", "22:00", {weekdays[4].isoformat(): "absent"}),
        "pooja": ("06:00", "14:10", {}),
        "kavita": ("09:00", "18:00", {}),
        "deepa": ("09:05", "18:00", {}),
        "anil": ("09:10", "18:05", {}),
    })
    s2["ramesh"].post("/leaves/apply", {"leave_type": "casual", "start_date": nxt(9), "end_date": nxt(11),
                                        "reason": "Village temple festival"})
    print("   leave: 3-day request routed supervisor -> department head")

    print("\nDemo data ready. Every account's password:", PASSWORD)
    for company in (nimbus, saffron):
        print(f"\n{company['spec']['name']}")
        for key, mail in company["emails"].items():
            print(f"   {mail}" if key in company["sessions"] else f"   ({mail}: employee record, no login)")


if __name__ == "__main__":
    main()

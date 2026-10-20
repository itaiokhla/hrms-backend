"""Check every seeded role's real data scope through Next.js and the tenant hosts (non-production only).

    python scripts/verify_saas_scopes.py --frontend http://127.0.0.1:3300 --out logs/saas-scope-matrix.json

Signs in as each account of .local-secrets/saas-e2e.json on its own company's host, then
compares what the API returns with what the role should see: employees visible, payroll rows
and salary fields, user administration, branding, and another company's records. Exits 1 on any
mismatch. Read-only apart from sign-ins.
"""

import argparse
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ROOT / ".local-secrets" / "saas-e2e.json"

# company -> person -> employees (by key) the person should see in GET /employees.
VISIBLE = {
    "alpha": {
        "admin": "all", "hr": "all", "finance": "all",
        "deptmgr": ["deptmgr", "teamlead", "emp1", "emp2"],
        "teamlead": ["teamlead", "emp1", "emp2"],
        "emp1": ["emp1"], "emp2": ["emp2"], "emp3": ["emp3"],
    },
    "beta": {
        "admin": "all", "hr": "all", "payroll": "all",
        "opsmgr": ["admin", "hr", "payroll", "opsmgr", "emp1"],  # the Dubai branch only
        "emp1": ["emp1"], "emp2": ["emp2"],
    },
    "gamma": {
        "admin": "all", "hr": "all",
        "deptmgr": ["deptmgr", "teamlead", "emp1", "emp2"],
        "teamlead": ["teamlead", "emp1", "emp2"],
        "emp1": ["emp1"], "emp2": ["emp2"],
    },
}
PAYROLL_READERS = {("alpha", "admin"), ("alpha", "finance"), ("beta", "admin"), ("beta", "payroll"), ("gamma", "admin")}
USER_ADMINS = {"admin", "hr"}
BRAND_ADMINS = {"admin"}


class Browser:
    def __init__(self, frontend: str, host: str):
        self.host = host
        self.client = httpx.Client(base_url=frontend, timeout=60, headers={"Host": host})

    def login(self, email: str, password: str) -> int:
        return self.client.post("/api/v1/auth/login", json={"email": email, "password": password},
                                headers={"Origin": f"http://{self.host}"}).status_code

    def get(self, path: str, **params):
        return self.client.get(f"/api/v1{path}", params=params)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frontend", default="http://127.0.0.1:3300")
    parser.add_argument("--out", default=str(ROOT / "logs" / "saas-scope-matrix.json"))
    args = parser.parse_args()
    data = json.loads(SECRETS.read_text(encoding="utf-8"))
    port = data["frontend"].rsplit(":", 1)[-1]
    password = data["password"]
    rows, failures = [], []
    for ckey, company in data["companies"].items():
        host = f"{company['slug']}.hrms.localhost:{port}"
        people = company["people"]
        by_employee = {p["employee_id"]: key for key, p in people.items()}
        others = [c for k, c in data["companies"].items() if k != ckey]
        foreign_employee = others[0]["people"]["emp1"]["employee_id"]
        foreign_payroll = next(iter(others[0]["payroll"].values()))["id"]
        for pkey, person in people.items():
            browser = Browser(args.frontend, host)
            status = browser.login(person["email"], password)
            row = {"company": company["name"], "person": person["name"], "roles": person["roles"], "login": status}
            if status != 200:
                failures.append(f"{company['name']} {person['name']}: login {status}")
                rows.append(row)
                continue
            listing = browser.get("/employees", limit=200).json()
            seen = sorted(by_employee.get(item["id"], f"?{item['id']}") for item in listing["items"])
            expected = VISIBLE[ckey][pkey]
            expected = sorted(people) if expected == "all" else sorted(expected)
            row["employees_seen"] = seen
            if seen != expected:
                failures.append(f"{company['name']} {person['name']}: sees {seen}, expected {expected}")
            payroll = browser.get("/payroll", limit=200)
            row["payroll_rows"] = payroll.json().get("total") if payroll.status_code == 200 else payroll.status_code
            own_rows = 1 if pkey in company["payroll"] else 0
            if (ckey, pkey) in PAYROLL_READERS:
                if row["payroll_rows"] != len(company["payroll"]):
                    failures.append(f"{company['name']} {person['name']}: payroll rows {row['payroll_rows']}")
            elif row["payroll_rows"] not in (own_rows, 403):
                failures.append(f"{company['name']} {person['name']}: sees {row['payroll_rows']} payroll rows, expected own only")
            salaries = [item.get("base_salary") for item in listing["items"] if item["id"] != person["employee_id"]]
            row["salary_fields_visible_for_others"] = sum(1 for s in salaries if s not in (None, "", "hidden"))
            if (ckey, pkey) not in PAYROLL_READERS and pkey not in ("admin",) and row["salary_fields_visible_for_others"]:
                failures.append(f"{company['name']} {person['name']}: sees salaries of others")
            row["users_admin"] = browser.get("/users").status_code
            if (row["users_admin"] == 200) != (pkey in USER_ADMINS):
                failures.append(f"{company['name']} {person['name']}: /users -> {row['users_admin']}")
            row["branding_admin"] = browser.get("/branding").status_code
            if (row["branding_admin"] == 200) != (pkey in BRAND_ADMINS):
                failures.append(f"{company['name']} {person['name']}: /branding -> {row['branding_admin']}")
            row["foreign_employee"] = browser.get(f"/employees/{foreign_employee}").status_code
            row["foreign_payslip"] = browser.get(f"/payroll/{foreign_payroll}/slip.pdf").status_code
            if row["foreign_employee"] not in (403, 404) or row["foreign_payslip"] not in (403, 404):
                failures.append(f"{company['name']} {person['name']}: foreign record {row['foreign_employee']}/{row['foreign_payslip']}")
            rows.append(row)

    for pkey, partner in data["partners"].items():
        browser = Browser(args.frontend, f"localhost:{port}")
        login = browser.client.post("/api/v1/partner/auth/login", json={"email": partner["admin_email"], "password": password},
                                    headers={"Origin": f"http://localhost:{port}"})
        clients = sorted(c["name"] for c in browser.get("/partner/companies").json()["items"]) if login.status_code == 200 else []
        expected = sorted(c["name"] for c in data["companies"].values() if c["partner"] == pkey)
        tenant_data = [browser.get(p).status_code for p in ("/employees", "/payroll", "/platform/companies")]
        rows.append({"partner": partner["name"], "login": login.status_code, "clients": clients, "tenant_and_platform_routes": tenant_data})
        if clients != expected or any(code != 401 for code in tenant_data):
            failures.append(f"{partner['name']}: clients {clients} (expected {expected}), routes {tenant_data}")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"rows": rows, "failures": failures}, indent=2, ensure_ascii=False), encoding="utf-8")
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))
    print(f"{len(failures)} mismatch(es)")
    for failure in failures:
        print("  -", failure)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

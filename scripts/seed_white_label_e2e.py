"""Seed three branded companies and one reseller for the white-label browser tests (non-production only).

    python scripts/seed_white_label_e2e.py --api http://localhost:8200/api/v1 --dns-file logs/wl-dns.json

Run it against a stack started with per-tenant hosts enabled, for example:

    PLATFORM_HOSTS=localhost,127.0.0.1 PLATFORM_BASE_DOMAIN=hrms.localhost PROXY_SHARED_SECRET=<32+ chars>
    CUSTOM_DOMAIN_CNAME_TARGET=tenants.hrms.localhost DOMAIN_DNS_PROVIDER=fixture DOMAIN_DNS_FIXTURE_FILE=logs/wl-dns.json
    TLS_PROVIDER=local-dev FRONTEND_URL=http://localhost:3200
    python scripts/demo_stack.py --fresh --data-dir .local-postgres-wl --port 8200

Everything is created through the public API (as the platform operator and as each
company's administrator):

* Partner "PayEase" (reseller) with a published brand that locks the product name
  and hides the platform credit, controlling Beta and Gamma.
* Alpha Technologies (India, INR, direct, Enterprise): teal brand, legal-entity
  payslip branding, HR admin, two payroll managers, a manager and employees
  (one with a Devanagari name); approved payroll with issued payslips.
* Beta Gulf Trading (UAE, AED, via PayEase, Arabic right-to-left): custom domain
  people.betagulf.localhost verified through the DNS fixture, two branches and a
  branch manager whose role covers one branch only; Arabic employee names.
* Gamma Health Systems (USA, USD, via PayEase): three branches, separate HR and
  finance (payroll) roles, light sidebar; approved payroll.

A random password is generated for every account of this run and written, with the
e-mail addresses and ids, to .local-secrets/wl-e2e.json (git-ignored). All names and
places are fictitious.
"""

import argparse
import io
import json
import secrets
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_demo import Api  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ROOT / ".local-secrets" / "wl-e2e.json"
FONT = ROOT / "app" / "assets" / "fonts" / "NotoSans-Bold.ttf"
PLATFORM_EMAIL = "ops@hrms-platform.io"
BRANCH_MANAGER_GRANTS = [
    ("employee:read", "BRANCH"), ("attendance:read", "BRANCH"), ("leave:read", "BRANCH"), ("leave:approve", "BRANCH"),
    ("report:read", "BRANCH"),
]


def wordmark(text: str, colour: str, *, width: int = 640, height: int = 160, background=None) -> bytes:
    """A simple synthetic logo: a coloured square mark and the company word."""
    rgb = tuple(int(colour[i : i + 2], 16) for i in (1, 3, 5))
    img = Image.new("RGBA", (width, height), background or (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = height // 8
    draw.rounded_rectangle((pad, pad, height - pad, height - pad), radius=height // 5, fill=rgb + (255,))
    font = ImageFont.truetype(str(FONT), int(height * 0.42))
    draw.text((height - pad // 2, height // 2), text[:1], font=ImageFont.truetype(str(FONT), int(height * 0.5)),
              fill=(255, 255, 255, 255), anchor="mm")
    draw.text((height + pad, height // 2), text, font=font, fill=rgb + (255,), anchor="lm")
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def square_mark(text: str, colour: str, size: int = 512) -> bytes:
    rgb = tuple(int(colour[i : i + 2], 16) for i in (1, 3, 5))
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 5, fill=rgb + (255,))
    draw.text((size // 2, size // 2), text, font=ImageFont.truetype(str(FONT), int(size * 0.42)), fill=(255, 255, 255, 255),
              anchor="mm")
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def upload(session: Api, path: str, kind: str, data: bytes, name: str) -> dict:
    response = session.client.post(path, headers=session._headers(), data={"kind": kind},
                                   files={"file": (name, data, "image/png")})
    if response.status_code >= 400:
        raise SystemExit(f"upload {path} {kind} -> {response.status_code}: {response.text}")
    return response.json()


def previous_month(today: date) -> date:
    return (today.replace(day=1) - timedelta(days=1)).replace(day=1)


def signup(base: str, name: str, admin: tuple, email: str, password: str, timezone: str) -> tuple:
    session = Api(base)
    me = session.post("/auth/signup", {"company_name": name, "full_name": " ".join(admin), "email": email,
                                       "password": password, "timezone": timezone})["me"]
    return session, me


def add_people(base: str, admin: Api, people: list, domain: str, password: str, roles: dict, prefix: str,
               branches: dict) -> dict:
    """people: (key, first, last, role names, branch key, manager key, position)."""
    created = {}
    for index, (key, first, last, role_names, branch, manager, position) in enumerate(people, start=1):
        email = f"{key}@{domain}"
        employee = admin.post("/employees", {
            "first_name": first, "last_name": last, "email": email, "employee_code": f"{prefix}-{index:03d}",
            "hire_date": "2025-01-06", "position": position, "branch_id": branches[branch]["id"],
            "manager_id": created[manager]["employee_id"] if manager else None,
        })
        entry = {"employee_id": employee["id"], "email": email, "name": f"{first} {last}"}
        if role_names is not None:
            invite = admin.post("/users/invite", {"email": email, "full_name": f"{first} {last}", "employee_id": employee["id"],
                                                  "role_ids": [roles[r] for r in role_names]})
            session = Api(base)
            me = session.post("/auth/invitations/accept", {"token": invite["invite_url"].split("token=")[1],
                                                           "password": password})["me"]
            entry["user_id"] = me["user"]["id"]
            entry["invite_url_host"] = invite["invite_url"].split("/accept-invite")[0]
        created[key] = entry
    return created


def publish_brand(admin: Api, changes: dict) -> dict:
    admin.put("/branding/draft", {"changes": changes})
    result = admin.post("/branding/publish", {})
    return result


def payroll(base: str, admin: Api, people: dict, preparer_key: str, approver_key: str, password: str, salaries: dict,
            month: date, approve: list) -> dict:
    preparer = Api(base)
    preparer.post("/auth/login", {"email": people[preparer_key]["email"], "password": password})
    approver = Api(base)
    approver.post("/auth/login", {"email": people[approver_key]["email"], "password": password})
    ids = {}
    for key, amount in salaries.items():
        admin.post("/salary-structures", {"employee_id": people[key]["employee_id"], "basic_salary": str(amount),
                                          "house_rent_allowance": str(round(amount * 0.4, 2)), "effective_from": "2025-01-01"})
        run = preparer.post(f"/payroll/generate?employee_id={people[key]['employee_id']}&year={month.year}&month={month.month}")
        ids[key] = run["id"]
        if key in approve:
            approver.post(f"/payroll/{run['id']}/approve")
    return ids


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8200/api/v1")
    parser.add_argument("--dns-file", default=str(ROOT / "logs" / "wl-dns.json"))
    parser.add_argument("--platform-password", default="Platform@12345")
    args = parser.parse_args()
    base = args.api
    try:
        httpx.get(base.rsplit("/api/", 1)[0] + "/health", timeout=10).raise_for_status()
    except Exception:
        sys.exit("The API is not reachable. Start the stack first (see this file's docstring).")

    password = "Wl" + secrets.token_urlsafe(15) + "9"
    month = previous_month(date.today())
    ops = Api(base)
    ops.post("/platform/auth/login", {"email": PLATFORM_EMAIL, "password": args.platform_password})

    # -- reseller ---------------------------------------------------------------------------
    partner = ops.post("/platform/partners", {
        "name": "PayEase", "billing_mode": "PARTNER_BILLS_TENANT",
        "tenant_entitlements": {"branding.product_name": True, "branding.remove_powered_by": True, "domains.custom": True,
                                "email.template_overrides": True},
    })
    mark = upload(ops, f"/platform/brands/partners/{partner['id']}/assets", "mark", square_mark("PE", "#7C3AED"), "payease.png")
    ops.put(f"/platform/brands/partners/{partner['id']}", {
        "changes": {"product_name": "PayEase HR", "short_name": "PayEase", "show_powered_by": False,
                    "primary_color": "#7C3AED", "mark": mark["id"], "support_url": "https://help.payease.example"},
        "locked_fields": ["product_name", "short_name", "show_powered_by"], "powered_by_label": "Powered by PayEase",
    })
    ops.post(f"/platform/brands/partners/{partner['id']}/publish")

    # -- Alpha (India, INR, direct, Enterprise) ---------------------------------------------------
    alpha, alpha_me = signup(base, "Alpha Technologies", ("Ananya", "Krishnan"), "admin@alpha-tech.example", password,
                             "Asia/Kolkata")
    ops.patch(f"/platform/companies/{alpha_me['company']['id']}", {"subscription_plan": "enterprise", "subscription_status": "active"})
    alpha_entity = alpha.get("/legal-entities")[0]
    alpha.patch(f"/legal-entities/{alpha_entity['id']}", {
        "legal_name": "Alpha Technologies India Private Limited", "country_code": "IN", "base_currency": "INR",
        "timezone": "Asia/Kolkata", "fiscal_year_start": "04-01",
        "registered_address": {"line1": "12 MG Road", "city": "Bengaluru", "subdivision_code": "IN-KA", "postal_code": "560001"},
        "registration_lines": [{"label": "CIN", "value": "U72200KA2019PTC123456"}, {"label": "PAN", "value": "AAACA1234A"}],
    })
    alpha_main = alpha.get("/branches")[0]
    alpha.patch(f"/branches/{alpha_main['id']}", {"name": "Bengaluru HQ", "code": "BLR", "subdivision_code": "IN-KA"})
    alpha_branches = {"blr": alpha.get("/branches")[0]}
    alpha_branches["bom"] = alpha.post("/branches", {"legal_entity_id": alpha_entity["id"], "code": "BOM", "name": "Mumbai",
                                                     "subdivision_code": "IN-MH"})
    roles = {r["system_key"] or r["name"]: r["id"] for r in alpha.get("/roles")}
    alpha_people = add_people(base, alpha, [
        ("hr", "Hema", "Rao", ["hr_admin"], "blr", None, "HR Administrator"),
        ("payroll", "Pranav", "Iyer", ["payroll_manager"], "blr", None, "Payroll Processor"),
        ("approver", "Kavya", "Menon", ["payroll_manager"], "blr", None, "Finance Controller"),
        ("manager", "Rohit", "Verma", ["manager"], "blr", None, "Engineering Manager"),
        ("employee", "Priya", "Sharma", ["employee"], "blr", "manager", "Software Engineer"),
        ("anita", "अनिता", "देशमुख", None, "bom", "manager", "QA Engineer"),
    ], "alpha-tech.example", password, roles, "AT", alpha_branches)
    logo = upload(alpha, "/branding/assets", "logo_light", wordmark("Alpha", "#0F766E"), "alpha.png")
    logo_dark = upload(alpha, "/branding/assets", "logo_dark", wordmark("Alpha", "#5EEAD4"), "alpha-dark.png")
    alpha_mark = upload(alpha, "/branding/assets", "mark", square_mark("A", "#0F766E"), "alpha-mark.png")
    publish_brand(alpha, {
        "product_name": "Alpha People", "short_name": "Alpha", "tagline": "Work, simplified at Alpha",
        "primary_color": "#0F766E", "accent_color": "#F59E0B", "sidebar_style": "BRAND", "font": "POPPINS",
        "logo_light": logo["id"], "logo_dark": logo_dark["id"], "mark": alpha_mark["id"], "show_powered_by": False,
        "legal_footer": "Alpha Technologies India Private Limited · 12 MG Road, Bengaluru 560001",
        "support_email": "people@alpha-tech.example", "privacy_url": "https://alpha-tech.example/privacy",
        "terms_url": "https://alpha-tech.example/terms", "default_locale": "en-IN",
    })
    doc_logo = upload(alpha, f"/legal-entities/{alpha_entity['id']}/document-brand/assets", "document_logo",
                      wordmark("Alpha India", "#134E4A", width=1000, height=200, background=(255, 255, 255, 255)), "doc.png")
    alpha.put(f"/legal-entities/{alpha_entity['id']}/document-brand", {"changes": {
        "document_logo": doc_logo["id"], "accent_color": "#134E4A",
        "legal_footer": "Alpha Technologies India Private Limited · CIN U72200KA2019PTC123456"}})
    alpha.post(f"/legal-entities/{alpha_entity['id']}/document-brand/publish", {})
    alpha_payroll = payroll(base, alpha, alpha_people, "payroll", "approver", password,
                            {"employee": 85000, "anita": 72000, "manager": 150000}, month, approve=["employee", "anita"])

    # -- Beta (UAE, AED, via PayEase, Arabic RTL, custom domain) -----------------------------------
    beta, beta_me = signup(base, "Beta Gulf Trading", ("Omar", "Haddad"), "admin@beta-gulf.example", password, "Asia/Dubai")
    ops.patch(f"/platform/companies/{beta_me['company']['id']}", {"partner_id": partner["id"], "subscription_plan": "professional",
                                                                  "subscription_status": "active"})
    beta.put("/settings", {"currency": "AED"})
    beta_entity = beta.get("/legal-entities")[0]
    beta.patch(f"/legal-entities/{beta_entity['id']}", {
        "legal_name": "Beta Gulf Trading LLC", "country_code": "AE", "base_currency": "AED", "timezone": "Asia/Dubai",
        "registered_address": {"line1": "Marina Plaza, Office 1204", "city": "Dubai", "subdivision_code": "AE-DU"},
        "registration_lines": [{"label": "Trade licence", "value": "DED-778899"}],
    })
    beta_main = beta.get("/branches")[0]
    beta.patch(f"/branches/{beta_main['id']}", {"name": "Dubai Marina", "code": "DXB", "subdivision_code": "AE-DU",
                                                "timezone": "Asia/Dubai"})
    beta_branches = {"dxb": beta.get("/branches")[0]}
    beta_branches["auh"] = beta.post("/branches", {"legal_entity_id": beta_entity["id"], "code": "AUH", "name": "Abu Dhabi",
                                                   "subdivision_code": "AE-AZ", "timezone": "Asia/Dubai"})
    beta_roles = {r["system_key"] or r["name"]: r["id"] for r in beta.get("/roles")}
    beta_roles["Branch Manager"] = beta.post("/roles", {
        "name": "Branch Manager", "description": "Sees and approves for the people of selected branches",
        "grants": [{"permission": p, "scope": s} for p, s in BRANCH_MANAGER_GRANTS]})["id"]
    beta_people = add_people(base, beta, [
        ("hr", "Layla", "Mansour", ["hr_admin"], "dxb", None, "HR Manager"),
        ("branchmgr", "Yusuf", "Karim", ["employee", "Branch Manager"], "dxb", None, "Branch Manager"),
        ("employee", "فاطمة", "الزهراء", ["employee"], "dxb", None, "Sales Associate"),
        ("rashid", "Rashid", "Khan", ["employee"], "auh", None, "Store Supervisor"),
        ("payroll", "Samir", "Aziz", ["payroll_manager"], "dxb", None, "Payroll Officer"),
        ("approver", "Nadia", "Farouk", ["payroll_manager"], "dxb", None, "Finance Manager"),
    ], "beta-gulf.example", password, beta_roles, "BG", beta_branches)
    beta.put(f"/users/{beta_people['branchmgr']['user_id']}/scope-targets", {"assignments": [
        {"role_id": beta_roles["Branch Manager"], "legal_entity_ids": [], "branch_ids": [beta_branches["dxb"]["id"]]}]})
    beta_logo = upload(beta, "/branding/assets", "logo_light", wordmark("Beta Gulf", "#C2410C"), "beta.png")
    publish_brand(beta, {
        "display_name": "Beta Gulf Trading", "tagline": "عملك، ببساطة · Your work, simplified", "primary_color": "#C2410C",
        "sidebar_style": "DARK", "radius": "PILL", "font": "NOTO", "default_locale": "ar-AE", "logo_light": beta_logo["id"],
        "support_email": "hr@beta-gulf.example", "legal_footer": "Beta Gulf Trading LLC · Marina Plaza, Dubai",
    })
    domain = beta.post("/domains", {"hostname": "people.betagulf.localhost"})
    txt = next(r for r in domain["records"] if r["type"] == "TXT")
    dns_file = Path(args.dns_file)
    dns_file.parent.mkdir(parents=True, exist_ok=True)
    dns_file.write_text(json.dumps({txt["name"]: {"TXT": [txt["value"]]},
                                    domain["hostname"]: {"CNAME": "tenants.hrms.localhost"}}, indent=2))
    verified = beta.post(f"/domains/{domain['id']}/verify")
    if verified["status"] != "ACTIVE":
        raise SystemExit(f"Beta's domain did not verify: {verified['status']} {verified.get('last_error')}")
    beta.post(f"/domains/{domain['id']}/primary")
    beta_payroll = payroll(base, beta, beta_people, "payroll", "approver", password, {"employee": 9500, "rashid": 11000},
                           month, approve=["employee", "rashid"])

    # -- Gamma (USA, USD, via PayEase, multi-branch) ------------------------------------------------
    gamma, gamma_me = signup(base, "Gamma Health Systems", ("Grace", "Holloway"), "admin@gamma-health.example", password,
                             "America/New_York")
    ops.patch(f"/platform/companies/{gamma_me['company']['id']}", {"partner_id": partner["id"], "subscription_plan": "standard",
                                                                   "subscription_status": "active"})
    gamma.put("/settings", {"currency": "USD"})
    gamma_entity = gamma.get("/legal-entities")[0]
    gamma.patch(f"/legal-entities/{gamma_entity['id']}", {
        "legal_name": "Gamma Health Systems Inc.", "country_code": "US", "base_currency": "USD",
        "timezone": "America/New_York",
        "registered_address": {"line1": "350 Fifth Avenue", "city": "New York", "subdivision_code": "US-NY", "postal_code": "10118"},
        "registration_lines": [{"label": "EIN", "value": "12-3456789"}],
    })
    gamma_main = gamma.get("/branches")[0]
    gamma.patch(f"/branches/{gamma_main['id']}", {"name": "New York", "code": "NYC", "subdivision_code": "US-NY",
                                                  "timezone": "America/New_York"})
    gamma_branches = {"nyc": gamma.get("/branches")[0]}
    gamma_branches["aus"] = gamma.post("/branches", {"legal_entity_id": gamma_entity["id"], "code": "AUS", "name": "Austin",
                                                     "subdivision_code": "US-TX", "timezone": "America/Chicago"})
    gamma_branches["den"] = gamma.post("/branches", {"legal_entity_id": gamma_entity["id"], "code": "DEN", "name": "Denver",
                                                     "subdivision_code": "US-CO", "timezone": "America/Denver"})
    gamma_roles = {r["system_key"] or r["name"]: r["id"] for r in gamma.get("/roles")}
    gamma_people = add_people(base, gamma, [
        ("hr", "Hannah", "Lee", ["hr_admin"], "nyc", None, "People Partner"),
        ("finance", "Frank", "Miller", ["payroll_manager"], "nyc", None, "Finance Lead"),
        ("approver", "Fiona", "Clark", ["payroll_manager"], "nyc", None, "Controller"),
        ("manager", "Marcus", "Reed", ["manager"], "aus", None, "Clinic Manager"),
        ("employee", "Gabriel", "Ortiz", ["employee"], "aus", "manager", "Nurse"),
        ("gina", "Gina", "Patel", ["employee"], "den", None, "Technician"),
    ], "gamma-health.example", password, gamma_roles, "GH", gamma_branches)
    gamma_logo = upload(gamma, "/branding/assets", "logo_light", wordmark("Gamma", "#1D4ED8"), "gamma.png")
    publish_brand(gamma, {
        "display_name": "Gamma Health Systems", "tagline": "Care teams, organised", "primary_color": "#1D4ED8",
        "accent_color": "#0891B2", "sidebar_style": "LIGHT", "radius": "SHARP", "font": "ROBOTO", "default_locale": "en-US",
        "logo_light": gamma_logo["id"],
    })
    gamma_payroll = payroll(base, gamma, gamma_people, "finance", "approver", password, {"employee": 6200, "gina": 5400},
                            month, approve=["employee"])

    def summary(session: Api, me: dict, people: dict, payroll_ids: dict, branches: dict, extra=None) -> dict:
        hosts = [d["hostname"] for d in session.get("/domains")["domains"]] if extra and extra.get("domains") else []
        return {
            "name": me["company"]["name"], "company_id": me["company"]["id"], "slug": me["company"]["slug"],
            "admin": me["user"]["email"], "people": people, "payroll_ids": payroll_ids,
            "branches": {k: {"id": v["id"], "name": v["name"]} for k, v in branches.items()}, "hosts": hosts,
        }

    SECRETS.parent.mkdir(parents=True, exist_ok=True)
    SECRETS.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "password": password,
        "platform": {"email": PLATFORM_EMAIL, "password": args.platform_password},
        "partner": {"id": partner["id"], "name": partner["name"]},
        "payroll_month": month.isoformat(),
        "companies": {
            "alpha": summary(alpha, alpha_me, alpha_people, alpha_payroll, alpha_branches),
            "beta": summary(beta, beta_me, beta_people, beta_payroll, beta_branches, {"domains": True}),
            "gamma": summary(gamma, gamma_me, gamma_people, gamma_payroll, gamma_branches),
        },
        "custom_domain": "people.betagulf.localhost",
        "dns_file": str(dns_file.resolve()),
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Seeded Alpha, Beta and Gamma with partner PayEase. Credentials for this run: {SECRETS} (git-ignored).")


if __name__ == "__main__":
    main()

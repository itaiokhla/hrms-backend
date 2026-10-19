"""Seed three synthetic companies for the attendance/GPS browser tests (non-production only).

    python scripts/seed_gps_e2e.py [--api http://localhost:8000/api/v1]

Run it against the local demo stack (python scripts/demo_stack.py --fresh). It
creates, through the public API only:

* Alpha Logistics - face + required location at punches, optional at lunch,
  periodic checks every 120 min (default), Manager A1 -> A1, A2 and Team Lead
  A1 -> A4 (nested), Manager A2 -> A3. Faces are enrolled from the test image.
* Beta Retail - optional location, periodic checks every 60 min, no face.
* Gamma Health - no location collection at all, one paid lunch, plus a
  suspended account.

A random password is generated for every account of this run and written,
with the e-mail addresses and ids, to .local-secrets/gps-e2e.json (git-ignored).
Nothing secret is printed or committed. All names and places are fictitious.
"""

import argparse
import json
import secrets
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_demo import Api, ist  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ROOT / ".local-secrets" / "gps-e2e.json"
FACE = ROOT.parent / "hrms-frontend" / "e2e" / "fixtures" / "face-enrol.jpg"
TEAM_LEAD = [("employee:read", "DIRECT_REPORTS"), ("attendance:read", "DIRECT_REPORTS"),
             ("location:read", "DIRECT_REPORTS"), ("location:review", "DIRECT_REPORTS")]

COMPANIES = {
    "alpha": {
        "name": "Alpha Logistics", "domain": "alpha-logistics.example",
        "policy": {"requireFaceForSelfCheckIn": True, "locationPunchPolicy": "REQUIRED", "locationLunchPolicy": "OPTIONAL",
                   "trackingEnabled": True, "trackingIntervalMinutes": 120, "trackingGraceMinutes": 15,
                   "movementThresholdM": 1000, "allowMultipleLunches": True},
        # key: first, last, manager, roles
        "people": [
            ("hr", "Hana", "Alpha-HR", None, ["hr_admin"]),
            ("m1", "Manoj", "Alpha-Mgr1", None, ["manager"]),
            ("m2", "Meera", "Alpha-Mgr2", None, ["manager"]),
            ("e1", "Esha", "Alpha-Emp1", "m1", ["employee"]),
            ("e2", "Eli", "Alpha-Emp2", "m1", ["employee"]),
            ("tl", "Tara", "Alpha-Lead1", "m1", ["employee", "Team Lead"]),
            ("e4", "Ezra", "Alpha-Emp4", "tl", ["employee"]),
            ("e3", "Emil", "Alpha-Emp3", "m2", ["employee"]),
        ],
        "faces": ["e1", "e2", "e3", "e4", "tl"],
    },
    "beta": {
        "name": "Beta Retail", "domain": "beta-retail.example",
        "policy": {"requireFaceForSelfCheckIn": False, "locationPunchPolicy": "OPTIONAL", "locationLunchPolicy": "OPTIONAL",
                   "trackingEnabled": True, "trackingIntervalMinutes": 60, "trackingGraceMinutes": 10},
        "people": [
            ("hr", "Hugo", "Beta-HR", None, ["hr_admin"]),
            ("m1", "Mira", "Beta-Mgr1", None, ["manager"]),
            ("e1", "Bela", "Beta-Emp1", "m1", ["employee"]),
            ("e2", "Bo", "Beta-Emp2", "m1", ["employee"]),
        ],
        "faces": [],
    },
    "gamma": {
        "name": "Gamma Health", "domain": "gamma-health.example",
        "policy": {"requireFaceForSelfCheckIn": False, "trackingEnabled": False, "locationPunchPolicy": "OFF",
                   "locationLunchPolicy": "OFF", "allowMultipleLunches": False, "lunchIsPaid": True,
                   "gpsAcceptableAccuracyM": 200},
        "people": [
            ("hr", "Hari", "Gamma-HR", None, ["hr_admin"]),
            ("m1", "Moana", "Gamma-Mgr1", None, ["manager"]),
            ("e1", "Gia", "Gamma-Emp1", "m1", ["employee"]),
            ("e2", "Gus", "Gamma-Emp2", "m1", ["employee"]),
            ("suspended", "Sal", "Gamma-Suspended", "m1", ["employee"]),
        ],
        "faces": [],
    },
}


def last_weekday(today: date) -> date:
    day = today - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def build(base: str, key: str, spec: dict, password: str) -> dict:
    admin_email = f"admin@{spec['domain']}"
    admin = Api(base)
    me = admin.post("/auth/signup", {"company_name": spec["name"], "full_name": f"{key.title()} Admin",
                                     "email": admin_email, "password": password, "timezone": "Asia/Kolkata"})["me"]
    admin.patch("/attendance/policy", spec["policy"])
    roles = {r["system_key"] or r["name"]: r["id"] for r in admin.get("/roles")}
    if any("Team Lead" in p[4] for p in spec["people"]):
        roles["Team Lead"] = admin.post("/roles", {"name": "Team Lead", "description": "Leads a small team (direct reports only)",
                                                   "grants": [{"permission": p, "scope": s} for p, s in TEAM_LEAD]})["id"]
    ids = {"admin": me["employee"]["id"]}
    emails = {"admin": admin_email}
    sessions = {"admin": admin}
    for index, (person, first, last, manager, role_names) in enumerate(spec["people"], start=1):
        email = f"{first.lower()}.{last.lower()}@{spec['domain']}"
        employee = admin.post("/employees", {
            "first_name": first, "last_name": last, "email": email, "employee_code": f"{key[:1].upper()}-{index:03d}",
            "hire_date": "2025-01-06", "position": "Synthetic test user", "manager_id": ids.get(manager),
        })
        invite = admin.post("/users/invite", {"email": email, "full_name": f"{first} {last}", "employee_id": employee["id"],
                                              "role_ids": [roles[r] for r in role_names]})
        session = Api(base)
        session.post("/auth/invitations/accept", {"token": invite["invite_url"].split("token=")[1], "password": password})
        ids[person], emails[person], sessions[person] = employee["id"], email, session
    hr = sessions["hr"]
    for person in spec["faces"]:
        with FACE.open("rb") as image:
            response = hr.client.post(f"/face/enroll?employee_id={ids[person]}", headers=hr._headers(),
                                      files={"file": ("face.jpg", image, "image/jpeg")})
        if response.status_code >= 400:
            raise SystemExit(f"Face enrolment failed for {person}: {response.status_code} {response.text}")
    return {"name": spec["name"], "company_id": me["company"]["id"], "ids": ids, "emails": emails, "sessions": sessions}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000/api/v1")
    args = parser.parse_args()
    try:
        httpx.get(args.api.rsplit("/api/", 1)[0] + "/health", timeout=10).raise_for_status()
    except Exception:
        sys.exit("The API is not reachable. Start it first: python scripts/demo_stack.py --fresh")
    if not FACE.exists():
        sys.exit(f"Missing test face image: {FACE}")

    password = "Gps" + secrets.token_urlsafe(15) + "7"
    built = {key: build(args.api, key, spec, password) for key, spec in COMPANIES.items()}

    # History for the hours examples, recorded by HR as a correction (the canonical calculator does the rest).
    day = last_weekday(datetime.now().date())
    alpha_hr, beta_hr = built["alpha"]["sessions"]["hr"], built["beta"]["sessions"]["hr"]
    alpha_hr.post("/attendance/manual", {
        "employee_id": built["alpha"]["ids"]["e1"], "date": day.isoformat(), "check_in_time": ist(day, "09:00"),
        "check_out_time": ist(day, "18:00"), "status": "present", "notes": "Synthetic history: one lunch",
        "breaks": [{"started_at": ist(day, "13:00"), "ended_at": ist(day, "13:45")}]})
    alpha_hr.post("/attendance/manual", {
        "employee_id": built["alpha"]["ids"]["e2"], "date": day.isoformat(), "check_in_time": ist(day, "09:00"),
        "check_out_time": ist(day, "18:00"), "status": "present", "notes": "Synthetic history: two lunches",
        "breaks": [{"started_at": ist(day, "12:30"), "ended_at": ist(day, "13:00")},
                   {"started_at": ist(day, "15:30"), "ended_at": ist(day, "15:45")}]})
    for person in ("e3", "e4"):  # the sibling branch and the nested report, for manager-scope checks
        alpha_hr.post("/attendance/manual", {
            "employee_id": built["alpha"]["ids"][person], "date": day.isoformat(), "check_in_time": ist(day, "09:15"),
            "check_out_time": ist(day, "17:45"), "status": "present", "notes": "Synthetic history"})
    beta_hr.post("/attendance/manual", {
        "employee_id": built["beta"]["ids"]["e1"], "date": day.isoformat(), "check_in_time": ist(day, "09:30"),
        "check_out_time": ist(day, "18:30"), "status": "present", "notes": "Synthetic history",
        "breaks": [{"started_at": ist(day, "13:00"), "ended_at": ist(day, "13:30")}]})

    gamma = built["gamma"]
    users = gamma["sessions"]["admin"].get("/users", params={"limit": 200})["items"]
    suspended = next(u for u in users if u["email"] == gamma["emails"]["suspended"])
    gamma["sessions"]["admin"].put(f"/users/{suspended['id']}/status", {"status": "suspended"})

    SECRETS.parent.mkdir(parents=True, exist_ok=True)
    SECRETS.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "password": password,
        "history_day": day.isoformat(),
        "companies": {key: {"name": c["name"], "company_id": c["company_id"], "emails": c["emails"], "ids": c["ids"]}
                      for key, c in built.items()},
    }, indent=2))
    print(f"Seeded {', '.join(c['name'] for c in built.values())}. Credentials for this run: {SECRETS} (git-ignored).")


if __name__ == "__main__":
    main()

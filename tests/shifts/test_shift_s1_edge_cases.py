"""AS-M1 catalogue cases left uncovered at the S0 baseline (03-shifts/01 §3.3 rule 4, §12.2; 12-edge-cases).

* AS-TZ-01 / GV-AS-SHF-26: a version of a shift used in a zone ahead of the owner starts no earlier than that zone's
  local today (409 SHIFT_VERSION_IN_FORCE with ``next_allowed_from`` and ``zone``); equal is accepted.
* AS-SHF-17: since X-M1, a rule on ``team_ids`` follows the team's dated memberships, so a member who joins later is
  covered from the join date (the interim RULE_MEMBER_GAP job of §12.2 is only needed before X-M1).
* AS-SHF-20: another tenant's shifts, rules and employees are 404 on every catalogue / rule / resolve path.
"""

from datetime import datetime, time, timedelta, timezone

from app.core import clock
from tests.helpers import create_employee, signup_company
from tests.shifts.test_shift_routes import tenant  # noqa: F401
from tests.shifts.test_shift_versions import IST, _shift, _today, admin  # noqa: F401
from tests.teams.test_teams import alpha, member, staff_payments  # noqa: F401


def test_as_tz_01_a_zone_ahead_of_the_owner_sets_the_earliest_version_start(admin, monkeypatch):
    day = _today()  # the owner's (Asia/Kolkata) today, as the DB guard sees it
    # 16:30 IST on `day` is already 01:00 on day + 1 in Kiritimati (UTC+14).
    fixed = IST.localize(datetime.combine(day, time(16, 30))).astimezone(timezone.utc)
    monkeypatch.setattr(clock, "utcnow", lambda: fixed)
    ahead = day + timedelta(days=1)
    entity = admin.post("/api/v1/legal-entities", json={"code": "LE-KI", "legal_name": "Line Islands", "country_code": "KI",
                                                        "base_currency": "AUD", "timezone": "Pacific/Kiritimati"})
    assert entity.status_code in (200, 201), entity.text
    branch = admin.post("/api/v1/branches", json={"legal_entity_id": entity.json()["id"], "code": "KI1",
                                                  "name": "Kiritimati", "timezone": "Pacific/Kiritimati"})
    assert branch.status_code in (200, 201), branch.text
    shift = _shift(admin, code="GEN-ALL", effective_from="2024-01-01", reason="test")
    create_employee(admin, "Asha", shift_id=shift["id"])  # owner zone
    body = {"start_time": "09:00", "end_time": "17:30", "reason": "Shorter day"}
    # Single zone: F = the owner's today is accepted (dry run, nothing written).
    r = admin.post(f"/api/v1/shifts/{shift['id']}/versions", json={**body, "effective_from": day.isoformat(),
                                                                 "dry_run": True})
    assert r.status_code in (200, 201), r.text
    create_employee(admin, "Teiti", shift_id=shift["id"], legal_entity_id=entity.json()["id"],
                    branch_id=branch.json()["id"])
    r = admin.post(f"/api/v1/shifts/{shift['id']}/versions", json={**body, "effective_from": day.isoformat()})
    assert r.status_code == 409 and r.json()["code"] == "SHIFT_VERSION_IN_FORCE", r.text
    assert r.json()["details"] == {"next_allowed_from": ahead.isoformat(), "zone": "Pacific/Kiritimati"}
    r = admin.post(f"/api/v1/shifts/{shift['id']}/versions", json={**body, "effective_from": ahead.isoformat(),
                                                                 "dry_run": True})
    assert r.status_code in (200, 201), r.text  # equal to the maximum is accepted


def test_as_shf_18_a_version_dry_run_lists_single_instance_compliance_per_pack(admin):
    """EC AS-SHF-18 / §3.6: employees on a placement shift (no roster) still get a planning-time signal — the dry run of
    a 12-hour outdoor version for a Dubai population lists DAILY_HOURS_EXCEEDED (AE 10 h) and OUTDOOR_MIDDAY_BAN
    (1 July is in the AE season) as WARN; nothing blocks the save. The rule simulate carries the same block."""
    entity = admin.post("/api/v1/legal-entities", json={"code": "LE-AE2", "legal_name": "Gulf Yard", "country_code": "AE",
                                                        "base_currency": "AED", "timezone": "Asia/Dubai"})
    assert entity.status_code in (200, 201), entity.text
    branch = admin.post("/api/v1/branches", json={"legal_entity_id": entity.json()["id"], "code": "DXB2",
                                                  "name": "Dubai yard", "timezone": "Asia/Dubai"}).json()
    shift = _shift(admin, code="YARD", effective_from="2024-01-01", reason="test")
    worker = create_employee(admin, "Omar", shift_id=shift["id"], legal_entity_id=entity.json()["id"],
                             branch_id=branch["id"])
    r = admin.post(f"/api/v1/shifts/{shift['id']}/versions", json={
        "effective_from": "2027-07-01", "start_time": "07:00", "end_time": "19:00", "outdoor": True,
        "reason": "Long summer days", "dry_run": True})
    assert r.status_code in (200, 201), r.text
    compliance = {c["rule_key"]: c for c in r.json()["compliance"]}
    assert set(compliance) >= {"DAILY_HOURS_EXCEEDED", "OUTDOOR_MIDDAY_BAN"}, r.json()["compliance"]
    assert {c["severity"] for c in compliance.values()} == {"WARN"}
    assert compliance["OUTDOOR_MIDDAY_BAN"]["country"] == "AE" and compliance["OUTDOOR_MIDDAY_BAN"]["employees"] == 1
    assert compliance["DAILY_HOURS_EXCEEDED"]["message_key"].endswith(".not_verified")  # the AE pack is a draft
    r = admin.post(f"/api/v1/shifts/{shift['id']}/versions", json={
        "effective_from": "2027-01-10", "start_time": "09:00", "end_time": "17:00", "reason": "Indoor winter",
        "dry_run": True})
    assert r.json()["compliance"] == []  # 8 h indoors in January: nothing to flag
    r = admin.post("/api/v1/shift-assignment-rules/simulate", json={
        "name": "Yard crew", "shift_id": shift["id"], "criteria": {"employee_ids": [worker["id"]]},
        "effective_from": (_today() + timedelta(days=3)).isoformat()})
    assert r.status_code == 200 and "compliance" in r.json(), r.text


def test_as_shf_17_a_team_rule_covers_a_member_who_joins_after_the_rule(alpha):
    staff_payments(alpha)
    admin, p, team = alpha["admin"], alpha["people"], alpha["payments"]["id"]
    r = admin.post("/api/v1/shifts", json={"code": "US-E1", "name": "US early", "kind": "FIXED", "version": {
        "effective_from": "2024-01-01", "reason": "test", "start_time": "18:30", "end_time": "03:30"}})
    assert r.status_code == 201, r.text
    shift = r.json()["id"]
    r = admin.post("/api/v1/shift-assignment-rules", json={"name": "US process", "shift_id": shift, "priority": 800,
                                                         "criteria": {"team_ids": [team]},
                                                         "effective_from": _today().isoformat()})
    assert r.status_code in (200, 201), r.text
    join = _today() + timedelta(days=3)
    member(admin, team, p["nitin"], effective_from=join.isoformat())  # Kabir's case: joins after the rule

    def resolved(day):
        r = admin.get(f"/api/v1/shifts/resolve?employee_id={p['nitin']['id']}&from={day}&to={day}")
        assert r.status_code == 200, r.text
        return r.json()["days"][0]

    before, after = resolved((join - timedelta(days=1)).isoformat()), resolved(join.isoformat())
    assert before["shift_id"] != shift
    assert (after["shift_id"], after["source"]) == (shift, "RULE")  # no silent miss, no manual list edit


def test_as_shf_20_another_tenants_shifts_rules_and_employees_are_not_found(app, tenant):
    other = signup_company(app, "Other Shift Tenant")
    sid, worker = tenant["flex"]["id"], tenant["worker"]["id"]
    day = _today().isoformat()
    assert other.get(f"/api/v1/shifts/{sid}").status_code == 404
    assert sid not in [s["id"] for s in other.get("/api/v1/shifts").json()]
    assert other.get("/api/v1/shifts/resolve", params={"employee_id": worker, "from": day, "to": day}).status_code == 404
    r = tenant["admin"].post("/api/v1/shift-assignment-rules", json={
        "name": "Flexi", "shift_id": sid, "criteria": {"everyone": True}, "effective_from": day})
    assert r.status_code in (200, 201), r.text
    rule = r.json()["id"]
    assert other.get(f"/api/v1/shift-assignment-rules/{rule}").status_code == 404
    assert rule not in [x["id"] for x in other.get("/api/v1/shift-assignment-rules").json().get("items", [])]
    assert other.patch(f"/api/v1/shift-assignment-rules/{rule}", json={"name": "Taken"}).status_code == 404
    r = other.post("/api/v1/shift-assignment-rules", json={"name": "Foreign", "shift_id": sid,
                                                          "criteria": {"employee_ids": [worker]}, "effective_from": day})
    assert r.status_code == 404
    # Inside the tenant an employee (SELF) never resolves a colleague.
    assert tenant["priya"].get("/api/v1/shifts/resolve", params={"employee_id": tenant["hr"].employee["id"],
                                                                 "from": day, "to": day}).status_code == 404

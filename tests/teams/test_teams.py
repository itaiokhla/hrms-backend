"""X-M1 teams and memberships (X 01 §3, §11, §14; WI-X-1.6): guards, lifecycle, permissions, me.teams.

Edge cases: X-ROLE-04 (second routing lead), -05 (two primaries; the §3.4 move passes), -06 (parent cycle), -07 (self
routing lead), -08 (TEAM_TREE lead adds members only), -22 / -31 (archive with a role target), -29 (archive in use),
-36 (member-candidate lookup), -46 (cross-tenant ids).
"""

from datetime import date, datetime, timedelta, timezone

import pytest
import pytz
from sqlalchemy import text

from app.database.db import system_engine
from tests.helpers import create_employee, invite_and_activate, login, role_id, signup_company

IST = pytz.timezone("Asia/Kolkata")


def _today() -> date:
    return datetime.now(timezone.utc).astimezone(IST).date()


def _sql(query, **params):
    with system_engine.begin() as conn:
        return conn.execute(text(query), params)


def custom_role(admin, name, grants):
    r = admin.post("/api/v1/roles", json={"name": name, "description": name, "grants": [
        {"permission": code, "scope": scope} for code, scope in grants]})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def give_roles(admin, client, *role_ids):
    r = admin.put(f"/api/v1/users/{client.user_id}/roles", json={"role_ids": list(role_ids)})
    assert r.status_code == 200, r.text


TECH_LEAD = [("attendance:read", "TEAM"), ("leave:read", "TEAM"), ("leave:approve", "TEAM"), ("team:read", "TEAM"),
             ("team:membership:manage", "TEAM_TREE"), ("roster:read", "TEAM"), ("roster:approve", "TEAM")]


@pytest.fixture
def alpha(app):
    admin = signup_company(app, "Teams Alpha")
    branch = admin.get("/api/v1/branches").json()[0]
    ceo = create_employee(admin, "Rahul", "Kapoor", branch_id=branch["id"])
    arjun = create_employee(admin, "Arjun", "Mehta", manager_id=ceo["id"], branch_id=branch["id"])
    meghna = create_employee(admin, "Meghna", "Rao", manager_id=arjun["id"], branch_id=branch["id"])
    priya = create_employee(admin, "Priya", "Shah", manager_id=arjun["id"], branch_id=branch["id"])
    rohan = create_employee(admin, "Rohan", "Verma", manager_id=arjun["id"], branch_id=branch["id"])
    farah = create_employee(admin, "Farah", "Ali", manager_id=arjun["id"], branch_id=branch["id"])
    nitin = create_employee(admin, "Nitin", "Gowda", manager_id=arjun["id"], branch_id=branch["id"])
    start = _today() - timedelta(days=30)
    r = admin.post("/api/v1/teams", json={"code": "PAYMENTS", "name": "Payments", "team_type": "PROJECT",
                                          "branch_id": branch["id"], "effective_from": start.isoformat()})
    assert r.status_code == 201, r.text
    payments = r.json()
    people = {"ceo": ceo, "arjun": arjun, "meghna": meghna, "priya": priya, "rohan": rohan, "farah": farah,
              "nitin": nitin}
    return {"app": app, "admin": admin, "branch": branch, "people": people, "start": start, "payments": payments}


def member(admin, team_id, employee, status=201, **body):
    r = admin.post(f"/api/v1/teams/{team_id}/members", json={"employee_id": employee["id"], **body})
    assert r.status_code == status, r.text
    return r.json()


def staff_payments(alpha):
    admin, p, team, start = alpha["admin"], alpha["people"], alpha["payments"]["id"], alpha["start"].isoformat()
    member(admin, team, p["meghna"], member_role="LEAD", is_routing_lead=True, effective_from=start)
    for key in ("priya", "rohan", "farah"):
        member(admin, team, p[key], is_primary_team=True, effective_from=start)


def test_team_lifecycle_list_detail_and_me_teams(alpha):
    admin, p = alpha["admin"], alpha["people"]
    staff_payments(alpha)
    listed = admin.get("/api/v1/teams?q=pay").json()
    assert listed["total"] == 1
    row = listed["items"][0]
    assert row["code"] == "PAYMENTS" and row["member_count"] == 4 and row["warnings"] == []
    assert row["leads"] == [{"employee_id": p["meghna"]["id"], "name": "Meghna Rao", "member_role": "LEAD",
                             "routing": True}]
    assert admin.get("/api/v1/teams?has_lead=false").json()["total"] == 0
    detail = admin.get(f"/api/v1/teams/{alpha['payments']['id']}").json()
    assert detail["permissions"]["manage"] and detail["parent_history"][0]["parent_team_id"] is None
    members = admin.get(f"/api/v1/teams/{alpha['payments']['id']}/members").json()
    assert {m["employee_name"] for m in members["items"]} == {"Meghna Rao", "Priya Shah", "Rohan Verma", "Farah Ali"}
    meghna = invite_and_activate(alpha["app"], admin, p["meghna"], ["employee"])
    me = meghna.get("/api/v1/auth/me").json()
    assert me["teams"]["leads"] == [{"id": alpha["payments"]["id"], "name": "Payments", "code": "PAYMENTS",
                                     "routing": True, "member_role": "LEAD"}]
    mine = meghna.get("/api/v1/me/teams").json()
    assert mine["leads"][0]["member_count"] == 4
    # A lead sees the members of the team she leads.
    seen = meghna.get(f"/api/v1/teams/{alpha['payments']['id']}/members").json()
    assert len(seen["items"]) == 4
    # team:read at SELF: own teams and their leads only, never other members (X 01 §10.3).
    priya = invite_and_activate(alpha["app"], admin, p["priya"], ["employee"])
    seen = priya.get(f"/api/v1/teams/{alpha['payments']['id']}/members").json()
    assert {m["employee_name"] for m in seen["items"]} == {"Meghna Rao", "Priya Shah"}
    assert priya.get("/api/v1/teams").json()["total"] == 1  # own team only
    r = admin.post("/api/v1/teams", json={"code": "payments", "name": "Again", "team_type": "OTHER"})
    assert r.status_code == 409 and r.json()["code"] == "TEAM_CODE_TAKEN"


def test_guards_overlap_primary_routing_and_cycle(alpha):
    admin, p, team, start = alpha["admin"], alpha["people"], alpha["payments"]["id"], alpha["start"]
    staff_payments(alpha)
    r = admin.post(f"/api/v1/teams/{team}/members", json={"employee_id": p["priya"]["id"]})
    assert r.status_code == 409 and r.json()["code"] == "MEMBERSHIP_OVERLAP"
    r = admin.post(f"/api/v1/teams/{team}/members", json={"employee_id": p["nitin"]["id"], "member_role": "LEAD",
                                                           "is_routing_lead": True})
    assert r.status_code == 409 and r.json()["code"] == "ROUTING_LEAD_CONFLICT"  # X-ROLE-04
    r = admin.post(f"/api/v1/teams/{team}/members", json={"employee_id": p["nitin"]["id"], "is_routing_lead": True})
    assert r.status_code == 409 and r.json()["code"] == "ROUTING_LEAD_CONFLICT"
    platform = admin.post("/api/v1/teams", json={"code": "PLATFORM", "name": "Platform", "team_type": "PROJECT",
                                                 "effective_from": start.isoformat()}).json()
    r = admin.post(f"/api/v1/teams/{platform['id']}/members", json={
        "employee_id": p["rohan"]["id"], "is_primary_team": True, "effective_from": start.isoformat()})
    assert r.status_code == 409 and r.json()["code"] == "PRIMARY_TEAM_CONFLICT"  # X-ROLE-05
    # §3.4: Rohan moves his primary team to PLATFORM from tomorrow in one save.
    move = (_today() + timedelta(days=1)).isoformat()
    moved = member(admin, platform["id"], p["rohan"], is_primary_team=True, effective_from=move, move_primary=True)
    assert moved["is_primary_team"] is True
    rows = _sql("SELECT team_id, is_primary_team, effective_from, effective_to FROM team_memberships "
                "WHERE employee_id = :e ORDER BY effective_from, team_id", e=p["rohan"]["id"]).all()
    payments_rows = [(r.is_primary_team, r.effective_to) for r in rows if r.team_id == team]
    assert payments_rows == [(True, _today()), (False, None)]
    # Parent cycle (X-ROLE-06).
    r = admin.patch(f"/api/v1/teams/{platform['id']}", json={"parent_team_id": team,
                                                             "parent_effective_from": _today().isoformat()})
    assert r.status_code == 200, r.text
    r = admin.patch(f"/api/v1/teams/{team}", json={"parent_team_id": platform["id"],
                                                   "parent_effective_from": _today().isoformat()})
    assert r.status_code == 409 and r.json()["code"] == "TEAM_CYCLE"


def test_not_eligible_dates_and_role_change_keeps_history(alpha):
    admin, p, team = alpha["admin"], alpha["people"], alpha["payments"]["id"]
    _sql("UPDATE employees SET termination_date = :d WHERE id = :e", d=_today() - timedelta(days=40),
         e=p["nitin"]["id"])
    r = admin.post(f"/api/v1/teams/{team}/members", json={"employee_id": p["nitin"]["id"]})
    assert r.status_code == 422 and r.json()["code"] == "NOT_ELIGIBLE"
    r = admin.post(f"/api/v1/teams/{team}/members", json={
        "employee_id": p["priya"]["id"], "effective_from": (alpha["start"] - timedelta(days=5)).isoformat()})
    assert r.status_code == 422 and r.json()["code"] == "NOT_ELIGIBLE"  # before the team exists (TM-2)
    row = member(admin, team, p["priya"], effective_from=alpha["start"].isoformat())
    changed = admin.patch(f"/api/v1/team-memberships/{row['id']}", json={
        "member_role": "DEPUTY_LEAD", "change_from": _today().isoformat(), "row_version": row["row_version"]})
    assert changed.status_code == 200, changed.text
    assert changed.json()["member_role"] == "DEPUTY_LEAD"
    assert {w["code"] for w in changed.json()["warnings"]} == {"DEPUTY_CANNOT_APPROVE"}  # X-ROLE-35 hint
    history = _sql("SELECT member_role, effective_from, effective_to FROM team_memberships WHERE employee_id = :e "
                   "ORDER BY effective_from", e=p["priya"]["id"]).all()
    assert [(h.member_role, h.effective_to) for h in history] == [
        ("MEMBER", _today() - timedelta(days=1)), ("DEPUTY_LEAD", None)]  # TM-3
    future = member(admin, team, p["rohan"], effective_from=(_today() + timedelta(days=10)).isoformat())
    cancelled = admin.patch(f"/api/v1/team-memberships/{future['id']}", json={"cancel": True})
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "CANCELLED"  # TM-7
    assert admin.get(f"/api/v1/teams/{team}/members?state=ALL").json()["total"] == 3


def test_tree_lead_changes_members_only_and_never_own_rows(alpha):
    app, admin, p, team, start = alpha["app"], alpha["admin"], alpha["people"], alpha["payments"]["id"], alpha["start"]
    staff_payments(alpha)
    meghna = invite_and_activate(app, admin, p["meghna"], ["employee"])
    give_roles(admin, meghna, role_id(admin, "employee"), custom_role(admin, "Tech Lead", TECH_LEAD))
    meghna = login(app, p["meghna"]["email"])
    found = meghna.get(f"/api/v1/teams/{team}/member-candidates?q=nit").json()["items"]
    assert found == [{"employee_id": p["nitin"]["id"], "name": "Nitin Gowda", "employee_code": p["nitin"]["employee_code"],
                      "branch": alpha["branch"]["name"]}]  # X-ROLE-36: masked, no employee:read needed
    added = member(meghna, team, p["nitin"])
    assert added["member_role"] == "MEMBER"
    r = meghna.post(f"/api/v1/teams/{team}/members", json={"employee_id": p["arjun"]["id"],
                                                            "member_role": "DEPUTY_LEAD"})
    assert r.status_code == 403  # X-ROLE-08
    own = _sql("SELECT id, row_version FROM team_memberships WHERE employee_id = :e", e=p["meghna"]["id"]).first()
    r = meghna.patch(f"/api/v1/team-memberships/{own.id}", json={"is_routing_lead": False, "row_version": own.row_version})
    assert r.status_code == 403  # X-ROLE-07 / SG-X3
    r = meghna.post("/api/v1/teams", json={"code": "MINE", "name": "Mine", "team_type": "OTHER"})
    assert r.status_code == 403


def test_archive_in_use_target_confirm_and_cross_tenant(alpha):
    app, admin, p, team = alpha["app"], alpha["admin"], alpha["people"], alpha["payments"]["id"]
    staff_payments(alpha)
    r = admin.post(f"/api/v1/teams/{team}/archive", json={})
    assert r.status_code == 409 and r.json()["code"] == "TEAM_IN_USE"  # X-ROLE-29
    supervisor = custom_role(admin, "Shift Supervisor", [("attendance:read", "ASSIGNED_TEAMS")])
    vikram = create_employee(admin, "Vikram", "Singh", branch_id=alpha["branch"]["id"])
    client = invite_and_activate(app, admin, vikram, ["employee"])
    give_roles(admin, client, role_id(admin, "employee"), supervisor)
    r = admin.put(f"/api/v1/users/{client.user_id}/scope-targets", json={"assignments": [
        {"role_id": supervisor, "assigned_team_ids": [team]}]})
    assert r.status_code == 200, r.text
    view = [a for a in r.json() if a["role_id"] == supervisor][0]
    assert view["assigned_team_ids"] == [team]
    for m in admin.get(f"/api/v1/teams/{team}/members").json()["items"]:
        r = admin.patch(f"/api/v1/team-memberships/{m['id']}", json={"effective_to": (_today() - timedelta(days=1)).isoformat()})
        assert r.status_code == 200, r.text
    dry = admin.post(f"/api/v1/teams/{team}/archive", json={"dry_run": True}).json()
    assert dry["in_use"] == [] and dry["confirm_code_required"] is True  # X-ROLE-31: targets never block
    assert [h["role_name"] for h in dry["target_holders"]] == ["Shift Supervisor"]
    r = admin.post(f"/api/v1/teams/{team}/archive", json={})
    assert r.status_code == 400 and r.json()["code"] == "CONFIRMATION_REQUIRED"
    r = admin.post(f"/api/v1/teams/{team}/archive", json={"confirm_code": "payments"})
    assert r.status_code == 200 and r.json()["status"] == "ARCHIVED"
    # X-ROLE-46: another tenant sees nothing and cannot name the team.
    other = signup_company(app, "Teams Beta")
    assert other.get(f"/api/v1/teams/{team}").status_code == 404
    beta_role = custom_role(other, "Beta supervisor", [("attendance:read", "ASSIGNED_TEAMS")])
    beta_emp = create_employee(other, "Bea", "Beta")
    beta_user = invite_and_activate(app, other, beta_emp, ["employee"])
    give_roles(other, beta_user, role_id(other, "employee"), beta_role)
    r = other.put(f"/api/v1/users/{beta_user.user_id}/scope-targets", json={"assignments": [
        {"role_id": beta_role, "assigned_team_ids": [team]}]})
    assert r.status_code == 404
    assert other.post(f"/api/v1/teams/{team}/members", json={"employee_id": beta_emp["id"]}).status_code == 404

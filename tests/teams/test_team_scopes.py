"""Team scopes, as-of rules, the team-lead resolver, role windows and team criteria (X 01 §4, §6.7, §7.2, §12; WI-X-1.6).

Edge cases: X-ROLE-02 / -11 (primary team on W decides), -09 (lead's own request), -12 / -33 (stale or powerless
lead), -13 / -44 (role windows), -14 (local midnight), -22 (archived AT target), -26 (cross-entity member), -28 (no
linked employee), -32 (lead on leave), -35 (inert deputy), -41 (per-date period predicate), -42 (dated re-parent),
-43 (upward exclusion).
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.core import authz
from app.core.permission_registry import scope_covers
from app.core.principal import build_principal
from app.models.company import Company
from app.models.employee import Employee
from app.models.user import User
from tests.helpers import create_employee, invite_and_activate, login, role_id
from tests.teams.test_teams import TECH_LEAD, _sql, _today, alpha, custom_role, give_roles, member, staff_payments  # noqa: F401
from tests.time_goldens import tenant_db


def _scope_ids(company_id: int, user_id: int, code: str, period=None):
    with tenant_db(company_id) as db:
        principal = build_principal(db, db.get(User, user_id), None, db.get(Company, company_id))
        scope = authz.employee_scope(db, principal, code, period=period)
        if scope.is_empty:
            return set()
        return {e for (e,) in db.query(Employee.id).filter(Employee.company_id == company_id,
                                                            scope.clause(Employee.id))}


@pytest.fixture
def lead(alpha):
    staff_payments(alpha)
    admin, p = alpha["admin"], alpha["people"]
    client = invite_and_activate(alpha["app"], admin, p["meghna"], ["employee"])
    tech = custom_role(admin, "Tech Lead", TECH_LEAD + [("attendance:read", "TEAM_TREE")])
    give_roles(admin, client, role_id(admin, "employee"), tech)
    meghna = login(alpha["app"], p["meghna"]["email"])
    meghna.user_id = client.user_id
    return {**alpha, "meghna": meghna, "tech_role": tech, "cid": admin.company_id}


def test_scope_covers_order():
    assert scope_covers(["TEAM_TREE"], "TEAM") and not scope_covers(["TEAM"], "TEAM_TREE")
    assert scope_covers(["COMPANY"], "ASSIGNED_TEAMS") and not scope_covers(["TEAM_TREE"], "ASSIGNED_TEAMS")
    assert not scope_covers(["TEAM"], "DEPARTMENT") and not scope_covers(["DEPARTMENT"], "TEAM")


def test_team_scope_covers_members_not_others_nor_the_management_chain(lead):
    p, cid = lead["people"], lead["cid"]
    ids = _scope_ids(cid, lead["meghna"].user_id, "attendance:read")
    assert ids == {p[k]["id"] for k in ("meghna", "priya", "rohan", "farah")}  # Nitin is not a member
    # X-ROLE-43: the CEO joins as a member; Meghna's team reach never covers her management chain.
    member(lead["admin"], lead["payments"]["id"], p["ceo"], effective_from=lead["start"].isoformat())
    member(lead["admin"], lead["payments"]["id"], p["arjun"], effective_from=lead["start"].isoformat())
    ids = _scope_ids(cid, lead["meghna"].user_id, "attendance:read")
    assert p["ceo"]["id"] not in ids and p["arjun"]["id"] not in ids
    # Payroll codes never gain team scopes: no pay data of anyone but herself (X-ROLE-26).
    assert _scope_ids(cid, lead["meghna"].user_id, "payroll:payslip:read") == {p["meghna"]["id"]}


def test_period_lists_use_the_per_date_predicate_and_lead_start(lead):
    admin, p, cid = lead["admin"], lead["people"], lead["cid"]
    team = lead["payments"]["id"]
    rohan_row = admin.get(f"/api/v1/teams/{team}/members?q=rohan").json()["items"][0]
    end = _today() - timedelta(days=11)
    assert admin.patch(f"/api/v1/team-memberships/{rohan_row['id']}", json={
        "effective_to": end.isoformat()}).status_code == 200
    # Today Rohan is gone; over a period that includes his last days he is in reach (both rows overlap there).
    assert p["rohan"]["id"] not in _scope_ids(cid, lead["meghna"].user_id, "attendance:read")
    window = (_today() - timedelta(days=20), _today())
    assert p["rohan"]["id"] in _scope_ids(cid, lead["meghna"].user_id, "attendance:read", window)
    # X-ROLE-41: a second lead who starts after Rohan left never reaches him over the same period.
    kiran = create_employee(admin, "Kiran", "Joshi", branch_id=lead["branch"]["id"])
    member(admin, team, kiran, member_role="LEAD", effective_from=(_today() - timedelta(days=5)).isoformat())
    client = invite_and_activate(lead["app"], admin, kiran, ["employee"])
    give_roles(admin, client, role_id(admin, "employee"), lead["tech_role"])
    reach = _scope_ids(cid, client.user_id, "attendance:read", window)
    assert p["rohan"]["id"] not in reach and p["priya"]["id"] in reach
    # Detail read of one date (a one-day period): Priya on a day before she joined is out of reach.
    before_join = lead["start"] - timedelta(days=3)
    assert p["priya"]["id"] not in _scope_ids(cid, lead["meghna"].user_id, "attendance:read", (before_join, before_join))


def test_team_tree_follows_the_parent_valid_on_the_date(lead):
    admin, p, cid = lead["admin"], lead["people"], lead["cid"]
    sub = admin.post("/api/v1/teams", json={"code": "PAY-OPS", "name": "Payments ops", "team_type": "FUNCTIONAL",
                                            "effective_from": lead["start"].isoformat()}).json()
    member(admin, sub["id"], p["nitin"], effective_from=lead["start"].isoformat())
    assert p["nitin"]["id"] not in _scope_ids(cid, lead["meghna"].user_id, "attendance:read")
    move = _today() - timedelta(days=4)
    r = admin.patch(f"/api/v1/teams/{sub['id']}", json={"parent_team_id": lead["payments"]["id"],
                                                        "parent_effective_from": move.isoformat()})
    assert r.status_code == 200, r.text
    assert p["nitin"]["id"] in _scope_ids(cid, lead["meghna"].user_id, "attendance:read")  # TEAM_TREE
    early = (move - timedelta(days=10), move - timedelta(days=5))
    assert p["nitin"]["id"] not in _scope_ids(cid, lead["meghna"].user_id, "attendance:read", early)  # X-ROLE-42


def test_assigned_teams_target_and_archived_target(lead):
    app, admin, p, cid, team = lead["app"], lead["admin"], lead["people"], lead["cid"], lead["payments"]["id"]
    supervisor = custom_role(admin, "Shift Supervisor", [("attendance:read", "ASSIGNED_TEAMS")])
    vikram = create_employee(admin, "Vikram", "Singh", branch_id=lead["branch"]["id"])
    client = invite_and_activate(app, admin, vikram, ["employee"])
    give_roles(admin, client, role_id(admin, "employee"), supervisor)
    assert _scope_ids(cid, client.user_id, "attendance:read") == {vikram["id"]}  # no targets: nothing beyond SELF
    r = admin.put(f"/api/v1/users/{client.user_id}/scope-targets", json={"assignments": [
        {"role_id": supervisor, "assigned_team_ids": [team]}]})
    assert r.status_code == 200, r.text
    reach = _scope_ids(cid, client.user_id, "attendance:read")
    assert {p[k]["id"] for k in ("meghna", "priya", "rohan", "farah")} <= reach
    # X-ROLE-22: once every membership ended and the team is archived, the target covers nobody on later dates.
    for m in admin.get(f"/api/v1/teams/{team}/members").json()["items"]:
        admin.patch(f"/api/v1/team-memberships/{m['id']}", json={"effective_to": (_today() - timedelta(days=1)).isoformat()})
    assert admin.post(f"/api/v1/teams/{team}/archive", json={"confirm_code": "PAYMENTS"}).status_code == 200
    assert _scope_ids(cid, client.user_id, "attendance:read") == {vikram["id"]}
    earlier = (_today() - timedelta(days=10), _today() - timedelta(days=5))
    assert p["priya"]["id"] in _scope_ids(cid, client.user_id, "attendance:read", earlier)  # history kept


def test_user_without_linked_employee_leads_nobody(lead):
    admin, cid = lead["admin"], lead["cid"]
    tech = lead["tech_role"]
    r = admin.post("/api/v1/users/invite", json={"email": "floater@corp.io", "full_name": "Floater",
                                                 "role_ids": [tech]})
    assert r.status_code == 200, r.text
    user_id = _sql("SELECT id FROM users WHERE email = 'floater@corp.io'").scalar()
    assert _scope_ids(cid, user_id, "attendance:read") == set()  # X-ROLE-28


def _resolve(cid, employee_id, work_date, action_date, module="leave", **kw):
    from app.services import team_service

    with tenant_db(cid) as db:
        return team_service.resolve_team_lead(db, cid, employee_id, work_date, action_date=action_date,
                                              module=module, **kw)


def test_team_lead_resolver_routing_fallbacks_and_eligibility(lead):
    app, admin, p, cid, team = lead["app"], lead["admin"], lead["people"], lead["cid"], lead["payments"]["id"]
    today = _today()
    got = _resolve(cid, p["priya"]["id"], today, today)
    assert (got.team_id, got.approver_employee_ids, got.via, got.routing) == (team, [p["meghna"]["id"]], "TEAM_LEAD", True)
    # X-ROLE-09: the lead's own request -> no deputy, no parent -> next step.
    own = _resolve(cid, p["meghna"]["id"], today, today)
    assert own.approver_employee_ids == [] and own.via == "NEXT_STEP"
    # X-ROLE-35: a deputy without the approve code is skipped (inert).
    deputy = member(admin, team, p["nitin"], member_role="DEPUTY_LEAD", effective_from=lead["start"].isoformat())
    assert {w["code"] for w in deputy["warnings"]} == {"DEPUTY_CANNOT_APPROVE"}
    invite_and_activate(app, admin, p["nitin"], ["employee"])
    own = _resolve(cid, p["meghna"]["id"], today, today)
    assert own.via == "NEXT_STEP" and {"employee_id": p["nitin"]["id"], "reason": "NO_AUTHORITY"} in own.skipped
    # X-ROLE-33: a stale routing lead (employment ended) falls through to ANY other eligible lead.
    second = create_employee(admin, "Vikram", "Iyer", branch_id=lead["branch"]["id"])
    member(admin, team, second, member_role="LEAD", effective_from=lead["start"].isoformat())
    client = invite_and_activate(app, admin, second, ["employee"])
    give_roles(admin, client, role_id(admin, "employee"), lead["tech_role"])
    _sql("UPDATE employees SET termination_date = :d WHERE id = :e", d=today - timedelta(days=1), e=p["meghna"]["id"])
    stale = _resolve(cid, p["priya"]["id"], today, today)
    assert stale.approver_employee_ids == [second["id"]] and stale.via == "TEAM_LEAD"
    _sql("UPDATE employees SET termination_date = NULL WHERE id = :e", e=p["meghna"]["id"])
    # X-ROLE-12: a lead without the module's approve code is ineligible ("shift_request" -> roster:approve).
    give_roles(admin, client, role_id(admin, "employee"))
    assert _resolve(cid, p["priya"]["id"], today, today, module="overtime").approver_employee_ids == []


def test_lead_on_approved_leave_is_skipped(lead):
    app, admin, p, cid = lead["app"], lead["admin"], lead["people"], lead["cid"]
    leave_day = _today() + timedelta(days=21)
    while leave_day.weekday() >= 5:
        leave_day += timedelta(days=1)
    r = lead["meghna"].post("/api/v1/leaves/apply", json={"leave_type": "casual", "start_date": leave_day.isoformat(),
                                                          "end_date": leave_day.isoformat(), "reason": "away"})
    assert r.status_code in (200, 201), r.text
    assert admin.post(f"/api/v1/leaves/{r.json()['id']}/approve").status_code == 200
    got = _resolve(cid, p["priya"]["id"], leave_day, leave_day)
    assert got.via == "NEXT_STEP" and {"employee_id": p["meghna"]["id"], "reason": "ON_LEAVE"} in got.skipped  # X-ROLE-32
    assert _resolve(cid, p["priya"]["id"], leave_day, _today()).approver_employee_ids == [p["meghna"]["id"]]


def test_primary_team_on_the_work_date_decides(lead):
    admin, p, cid, start = lead["admin"], lead["people"], lead["cid"], lead["start"]
    platform = admin.post("/api/v1/teams", json={"code": "PLATFORM", "name": "Platform", "team_type": "PROJECT",
                                                 "effective_from": start.isoformat()}).json()
    arjun_lead = member(admin, platform["id"], p["nitin"], member_role="LEAD", is_routing_lead=True,
                        effective_from=start.isoformat())
    assert arjun_lead["member_role"] == "LEAD"
    move = _today() + timedelta(days=3)
    member(admin, platform["id"], p["rohan"], is_primary_team=True, effective_from=move.isoformat(), move_primary=True)
    before = _resolve(cid, p["rohan"]["id"], _today(), _today(), module=None)
    after = _resolve(cid, p["rohan"]["id"], move, _today(), module=None)
    assert before.team_id == lead["payments"]["id"] and after.team_id == platform["id"]  # X-ROLE-02 / -11


def test_role_windows_overlap_local_midnight_and_history(lead):
    from app.services.rbac_service import local_midnight

    # X-ROLE-14: the window starts at 00:00 IST, not 05:30.
    assert local_midnight("Asia/Kolkata", date(2026, 12, 1)) == datetime(2026, 11, 30, 18, 30, tzinfo=timezone.utc)
    app, admin, p = lead["app"], lead["admin"], lead["people"]
    supervisor = custom_role(admin, "Rota Supervisor", [("attendance:read", "ASSIGNED_TEAMS")])
    vikram = create_employee(admin, "Vikram", "Rao", branch_id=lead["branch"]["id"])
    client = invite_and_activate(app, admin, vikram, ["employee"])
    employee_role = role_id(admin, "employee")
    current = admin.get(f"/api/v1/users/{client.user_id}/role-assignments").json()["items"]
    keep = [{"assignment_id": a["assignment_id"], "role_id": a["role_id"], "effective_from": a["effective_from"]}
            for a in current]
    first_end = _today() + timedelta(days=9)
    windows = keep + [
        {"role_id": supervisor, "effective_from": _today().isoformat(), "effective_until": first_end.isoformat(),
         "targets": {"ASSIGNED_TEAMS": [lead["payments"]["id"]]}},
        {"role_id": supervisor, "effective_from": (first_end + timedelta(days=1)).isoformat(), "targets": {}},
    ]
    r = admin.put(f"/api/v1/users/{client.user_id}/roles", json={"assignments": windows})
    assert r.status_code == 200, r.text  # X-ROLE-44: adjacent windows touch at local midnight
    rows = [a for a in r.json()["assignments"] if a["role_id"] == supervisor]
    assert [(a["effective_from"], a["effective_until"]) for a in rows] == [
        (_today().isoformat(), first_end.isoformat()), ((first_end + timedelta(days=1)).isoformat(), None)]
    assert rows[0]["targets"] == {"ASSIGNED_TEAMS": [lead["payments"]["id"]]}
    overlapping = windows[:-1] + [{"role_id": supervisor, "effective_from": (first_end - timedelta(days=2)).isoformat()}]
    r = admin.put(f"/api/v1/users/{client.user_id}/roles", json={"assignments": [
        {**w, "assignment_id": rows[0]["assignment_id"]} if w.get("targets") else w for w in overlapping]})
    assert r.status_code == 409 and r.json()["code"] == "ROLE_WINDOW_OVERLAP"  # X-ROLE-13
    assert p["priya"]["id"] in _scope_ids(lead["cid"], client.user_id, "attendance:read")
    # The legacy form still works; removing a role ends its window and keeps it as history.
    r = admin.put(f"/api/v1/users/{client.user_id}/roles", json={"role_ids": [employee_role]})
    assert r.status_code == 200, r.text
    assert p["priya"]["id"] not in _scope_ids(lead["cid"], client.user_id, "attendance:read")
    assert _sql("SELECT count(*) FROM user_role_assignments WHERE user_id = :u AND role_id = :r",
                u=client.user_id, r=supervisor).scalar() >= 1


def test_shift_rule_with_team_criteria_applies_to_members_only(lead):
    admin, p, team = lead["admin"], lead["people"], lead["payments"]["id"]
    r = admin.post("/api/v1/shifts", json={"code": "PAY-LATE", "name": "Payments late", "kind": "FIXED", "version": {
        "effective_from": "2024-01-01", "reason": "test", "start_time": "12:00", "end_time": "21:00"}})
    assert r.status_code == 201, r.text
    shift = r.json()["id"]
    r = admin.post("/api/v1/shift-assignment-rules", json={"name": "Payments team", "shift_id": shift, "priority": 800,
                                                "criteria": {"team_ids": [team]},
                                                "effective_from": _today().isoformat()})
    assert r.status_code in (200, 201), r.text
    assert admin.post("/api/v1/shift-assignment-rules", json={"name": "Bad", "shift_id": shift, "priority": 801,
                                                   "criteria": {"team_ids": [999999]},
                                                   "effective_from": _today().isoformat()}).status_code == 404
    day = (_today() + timedelta(days=1)).isoformat()

    def resolved(key):
        r = admin.get(f"/api/v1/shifts/resolve?employee_id={p[key]['id']}&from={day}&to={day}")
        assert r.status_code == 200, r.text
        return r.json()["days"][0]

    assert (resolved("priya")["shift_id"], resolved("priya")["source"]) == (shift, "RULE")
    assert resolved("nitin")["shift_id"] != shift

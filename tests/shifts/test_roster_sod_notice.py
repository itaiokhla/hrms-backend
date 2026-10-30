"""EC AS-ROS-11 / GV-AS-ROS-16, the half IG-AS-143 left open (FU-AS-53): when a ``roster:publish`` assignment ends and a
roster period with drafts under ``shift.roster.planner_cannot_publish`` keeps no publisher but its planner (the last
draft saver), the planners and the ``shift:manage`` COMPANY holders hear ``roster.sod_unavailable`` at once — through
the access module's reroute hook (a role removed, the daily ``access.assignment_expire``) — not at the next refused
publish. One unread notice per person; another company's access change touches nothing here.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import text

from app.database.db import SessionLocal, bind_tenant, system_engine
from tests.helpers import create_employee, invite_and_activate, signup_company
from tests.shifts.test_rosters import _drafts, _legal_week, plant  # noqa: F401


def _sql(query, **params):
    with system_engine.begin() as conn:
        return conn.execute(text(query), params)


def _role(admin, name, permission):
    r = admin.post("/api/v1/roles", json={"name": name, "description": name, "grants": [
        {"permission": "roster:read", "scope": "COMPANY"}, {"permission": permission, "scope": "COMPANY"}]})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _user_with(app, admin, name, branch_id, role):
    user = invite_and_activate(app, admin, create_employee(admin, name, branch_id=branch_id), ["employee"])
    assert admin.put(f"/api/v1/users/{user.user_id}/roles", json={"role_ids": [role]}).status_code == 200
    return user.user_id


def _sod_on(admin):
    r = admin.put("/api/v1/shift-requests/settings", json={"planner_cannot_publish": True})
    assert r.status_code == 200 and r.json()["planner_cannot_publish"] is True, r.text


def _window_ended(user_id):
    """The user's role window ended a minute ago (as a window set to end yesterday leaves it), with no hook run."""
    _sql("UPDATE user_role_assignments SET effective_from = now() - interval '2 days', "
         "effective_until = now() - interval '1 minute' WHERE user_id = :u", u=user_id)


def _expire(company_id):
    """Job ``access.assignment_expire`` for the UTC today: it bumps the users whose windows ended and reroutes."""
    from app.services import team_service

    with SessionLocal() as db:
        bind_tenant(db, company_id)
        team_service.assignment_expire(SimpleNamespace(db=db, company_id=company_id, payload={
            "local_date": datetime.now(timezone.utc).date().isoformat(), "timezone": "UTC"}))
        db.commit()


def _notices(link):
    return dict(_sql("SELECT user_id, count(*) FROM notifications WHERE action_url = :l GROUP BY user_id",
                     l=link).all())


def test_the_last_other_publisher_losing_roster_publish_tells_the_planners_and_admins_once(app, plant):
    admin, branch = plant["admin"], plant["branch"]["id"]
    admin_id = admin.me()["user"]["id"]
    publisher = _role(admin, "Roster publisher", "roster:publish")
    asha = _user_with(app, admin, "Asha", branch, publisher)
    vikram = _user_with(app, admin, "Vikram", branch, publisher)
    kiran = _user_with(app, admin, "Kiran", branch, _role(admin, "Roster planner", "roster:plan"))
    _sod_on(admin)
    _drafts(plant, _legal_week(plant))  # the admin (planner and publisher) is the last draft saver
    link = f"/rosters/{plant['period']['id']}?sod=unavailable"

    # Asha's role is removed while Vikram still publishes: nobody is told.
    assert admin.put(f"/api/v1/users/{asha}/roles", json={"role_ids": []}).status_code == 200
    assert _notices(link) == {}

    # Vikram's window ends and the daily expiry runs: the planners (the admin, Kiran) and the shift managers (the
    # admin) hear it once each, before anyone tries to publish.
    _window_ended(vikram)
    _expire(admin.company_id)
    assert _notices(link) == {admin_id: 1, kiran: 1}
    messages = dict(_sql("SELECT user_id, message FROM notifications WHERE action_url = :l", l=link).all())
    assert "Grant it to someone" in messages[admin_id] and "Ask an administrator" in messages[kiran]

    # A later access change (the job again) while the notice is unread adds nothing.
    _expire(admin.company_id)
    assert _notices(link) == {admin_id: 1, kiran: 1}


def test_a_publisher_ending_in_another_company_leaves_this_one_alone(app, plant):
    admin, branch = plant["admin"], plant["branch"]["id"]
    admin_id = admin.me()["user"]["id"]
    asha = _user_with(app, admin, "Asha", branch, _role(admin, "Roster publisher", "roster:publish"))
    _sod_on(admin)
    _drafts(plant, _legal_week(plant))
    link = f"/rosters/{plant['period']['id']}?sod=unavailable"
    _window_ended(asha)  # this company now meets the condition, but no hook has run here yet

    other = signup_company(app, "Other Roster Works")
    other_branch = other.get("/api/v1/branches").json()[0]["id"]
    leaver = _user_with(app, other, "Omar", other_branch, _role(other, "Roster publisher", "roster:publish"))
    assert other.put(f"/api/v1/users/{leaver}/roles", json={"role_ids": []}).status_code == 200
    _expire(other.company_id)
    assert _notices(link) == {}

    _expire(admin.company_id)  # this company's own hook flags it
    assert _notices(link) == {admin_id: 1}

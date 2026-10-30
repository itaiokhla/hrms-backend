"""The rotation page's "last auto-publish run" line (A16, 03-shifts/02 §3.5, EC AS-REL-06) and its assignments list.

``GET /rotations/auto-publish/last`` returns the tenant's latest ``roster.rotation_generate`` job (progress and a result
summary) or null; the jobs table has no row-level security, so another company sees null, never this one's run. An
employee whose only rotation read is SELF is refused. ``GET /rotations/assignments`` lists the assignments (with
``auto_publish``) of the employees inside the viewer's scope.
"""

from app.database.db import SessionLocal, bind_tenant
from tests.helpers import create_employee, invite_and_activate, login, signup_company
from tests.shifts.test_rosters import _today, plant  # noqa: F401

LAST = "/api/v1/rotations/auto-publish/last"


def _run_auto_publish(company_id: int, local_date) -> int:
    from app.payroll.services import jobs

    with SessionLocal() as db:
        bind_tenant(db, company_id)
        job_id = jobs.enqueue(db, company_id=company_id, type="roster.rotation_generate",
                              payload={"local_date": local_date.isoformat()}).id
        db.commit()  # JOBS_MODE=inline: the job runs right after this commit
    return job_id


def _planner(app, admin, branch_id):
    role = admin.post("/api/v1/roles", json={"name": "Rotation planner", "description": "plans", "grants": [
        {"permission": "roster:read", "scope": "COMPANY"}, {"permission": "roster:plan", "scope": "COMPANY"}]})
    assert role.status_code in (200, 201), role.text
    kiran = create_employee(admin, "Kiran", branch_id=branch_id)
    user = invite_and_activate(app, admin, kiran, ["employee"])
    r = admin.put(f"/api/v1/users/{user.user_id}/roles", json={"role_ids": [role.json()["id"]]})
    assert r.status_code == 200, r.text
    return login(app, kiran["email"])  # a fresh session carries the new grants


def test_the_planner_reads_the_latest_auto_publish_run_and_others_do_not(app, plant):
    admin, early, today = plant["admin"], plant["shifts"]["EARLY"], _today()
    planner = _planner(app, admin, plant["branch"]["id"])
    assert planner.get(LAST).status_code == 200 and planner.get(LAST).json() is None  # no run yet

    r = admin.post("/api/v1/rotations", json={
        "code": "LAST-RUN", "name": "Last run", "template_key": "SIX_ON_TWO_OFF_SLOW",
        "version": {"token_map": {t: {"shift_id": early} for t in ("M", "A", "N")}}})
    assert r.status_code == 201, r.text
    pattern_id = r.json()["id"]
    assert admin.post(f"/api/v1/rotations/{pattern_id}/versions/1/activate").status_code == 200
    version = next(p for p in admin.get("/api/v1/rotations").json() if p["id"] == pattern_id)["current_version_id"]
    r = admin.post("/api/v1/rotations/assignments", json={
        "pattern_version_id": version, "employee_ids": [plant["ravi"]["id"]], "crew_code": "A", "offset_days": 0,
        "anchor_date": today.isoformat(), "effective_from": today.isoformat(), "auto_publish": True})
    assert r.status_code == 201, r.text

    first = _run_auto_publish(admin.company_id, today)
    r = planner.get(LAST)
    assert r.status_code == 200, r.text
    run = r.json()
    assert (run["job_id"], run["status"], run["local_date"]) == (first, "SUCCEEDED", today.isoformat())
    assert (run["progress_done"], run["progress_total"]) == (1, 1) and run["finished_at"] and run["error"] is None
    assert run["summary"]["chunks"] == 1 and run["summary"]["failed_chunks"] == 0
    assert run["summary"]["publications"] == 1 and run["summary"]["entries"] > 0
    assert run["summary"]["blocked_employees"] == 0

    second = _run_auto_publish(admin.company_id, today)  # a later run (nothing new to publish): the latest wins
    assert second > first and planner.get(LAST).json()["job_id"] == second

    # The assignments list shows the auto-publish flag and the employee to the planner.
    r = planner.get("/api/v1/rotations/assignments", params={"pattern_id": pattern_id})
    assert r.status_code == 200, r.text
    [row] = r.json()["items"]
    assert (row["employee_id"], row["auto_publish"], row["crew_code"], row["pattern_code"], row["version_no"]) == (
        plant["ravi"]["id"], True, "A", "LAST-RUN", 1)
    assert row["employee_name"].startswith("Ravi") and r.json()["total"] == 1

    # Another company: no run of its own (null), never this one's; and none of these assignments.
    other = signup_company(app, "Other Rotations")
    assert other.get(LAST).status_code == 200 and other.get(LAST).json() is None
    assert other.get("/api/v1/rotations/assignments").json()["items"] == []

    # An employee whose rotation read is SELF only is refused the company-wide run and sees no one else's assignment.
    worker = invite_and_activate(app, admin, create_employee(admin, "Meera", branch_id=plant["branch"]["id"]),
                                 ["employee"])
    worker = login(app, worker.email)
    assert worker.get(LAST).status_code == 403
    assert worker.get("/api/v1/rotations/assignments").json()["items"] == []

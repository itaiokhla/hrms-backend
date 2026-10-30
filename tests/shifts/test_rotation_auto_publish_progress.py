"""EC AS-REL-06 progress (FU-AS-53, IG-AS-142): job ``roster.rotation_generate`` counts its ROTATION_AUTO chunks in the
job row (``progress_done`` / ``progress_total``: once the chunks are built, then after each chunk, failed or not) and its
result carries ``chunks`` / ``failed_chunks`` ("1 of 3 chunks failed"). Company-wide planners read the run at
``GET /jobs/{id}``; an employee gets 404.
"""

from sqlalchemy import text

from app.database.db import SessionLocal, bind_tenant, system_engine
from tests.helpers import create_employee, invite_and_activate, login
from tests.shifts.test_rosters import _today, plant  # noqa: F401


def _sql(query, **params):
    with system_engine.begin() as conn:
        return conn.execute(text(query), params)


def test_rotation_auto_publish_records_chunk_progress_on_its_job(app, plant, monkeypatch):
    from app.payroll.services import jobs
    from app.services import roster_consents, shift_rosters, shift_rotations

    admin, early = plant["admin"], plant["shifts"]["EARLY"]
    r = admin.post("/api/v1/rotations", json={
        "code": "AUTO-PROGRESS", "name": "Auto progress", "template_key": "SIX_ON_TWO_OFF_SLOW",
        "version": {"token_map": {t: {"shift_id": early} for t in ("M", "A", "N")}}})
    assert r.status_code == 201, r.text
    assert admin.post(f"/api/v1/rotations/{r.json()['id']}/versions/1/activate").status_code == 200
    version = next(p for p in admin.get("/api/v1/rotations").json()
                   if p["id"] == r.json()["id"])["current_version_id"]
    today = _today()
    kabir = create_employee(admin, "Kabir", branch_id=plant["branch"]["id"])["id"]
    for person, crew in ((plant["ravi"]["id"], "A"), (plant["sita"]["id"], "B"), (kabir, "C")):
        r = admin.post("/api/v1/rotations/assignments", json={
            "pattern_version_id": version, "employee_ids": [person], "crew_code": crew, "offset_days": 0,
            "anchor_date": today.isoformat(), "effective_from": today.isoformat(), "auto_publish": True})
        assert r.status_code == 201, r.text

    # One employee's horizon fits a chunk, two do not: three chunks; Kabir's (the last) fails to write.
    monkeypatch.setattr(shift_rotations, "AUTO_PUBLISH_CHUNK", roster_consents.HORIZON_DAYS + 2)
    real_write = shift_rosters._write

    def kabirs_chunk_fails(db, principal, company_id, **kw):
        if kabir in kw["scope"]["employee_ids"]:
            raise RuntimeError("chunk write failed")
        return real_write(db, principal, company_id, **kw)

    monkeypatch.setattr(shift_rosters, "_write", kabirs_chunk_fails)
    seen = []
    real_progress = jobs.JobContext.progress

    def recording(self, done, total=None):
        seen.append((done, total))
        real_progress(self, done, total)

    monkeypatch.setattr(jobs.JobContext, "progress", recording)
    with SessionLocal() as db:
        bind_tenant(db, admin.company_id)
        job_id = jobs.enqueue(db, company_id=admin.company_id, type="roster.rotation_generate",
                              payload={"local_date": today.isoformat()}).id
        db.commit()  # JOBS_MODE=inline: the job runs right after this commit

    assert seen == [(0, 3), (1, 3), (2, 3), (3, 3)]
    job = _sql("SELECT status, progress_done, progress_total, result FROM jobs WHERE id = :j", j=job_id).one()
    assert (job.status, job.progress_done, job.progress_total) == ("SUCCEEDED", 3, 3)
    assert job.result["chunks"] == 3 and job.result["failed_chunks"] == 1 and len(job.result["publications"]) == 2

    r = admin.get(f"/api/v1/jobs/{job_id}")
    assert r.status_code == 200 and r.json()["progress"] == {"done": 3, "total": 3, "percent": 100}, r.text
    assert (r.json()["result"]["chunks"], r.json()["result"]["failed_chunks"]) == (3, 1)
    worker = invite_and_activate(app, admin, create_employee(admin, "Meera", branch_id=plant["branch"]["id"]),
                                 ["employee"])
    assert login(app, worker.email).get(f"/api/v1/jobs/{job_id}").status_code == 404

"""Consent ledger, consent gate, face data and the transparency panel (AS-M2 WI-AS-2.6, 2.9; AS 07 §5, §6, §12, §18
T-09, T-11, T-12, T-20; EC AS-CAP-06, AS-SEC-09, AS-SEC-10).

* AS-CAP-06 / T-09: face_on_punch with no BIOMETRIC_FACE consent -> the face step is skipped (verify_mode NONE, no
  flag, no review item), never CONSENT_REQUIRED; with a consent the face step runs; enrolment under a STANDARD policy
  needs a consent (422 CONSENT_REQUIRED).
* AS-SEC-09: under LEGACY_V1 an enrolment is a legacy one (grace clock) and stored encrypted only (BUG-AS-19).
* C-1 / C-4: append-only ledger (the DB refuses UPDATE), withdrawal keeps history, a withdrawn face consent queues the
  deletion; T-12 / AS-SEC-10 the purge job deletes, a legal hold postpones it; C-6 expiry job.
* LOCATION_AT_PUNCH is asked once (inline notice recorded), policy documents and acknowledgements.
* T-20: the privacy panel shows only the viewer's own data, retention periods and documents.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.attendance.capture_v2_helpers import activate_capture, punches, review_items, rows, sql, v2_punch
from tests.attendance_helpers import employee_with_login, fake_clock, set_policy  # noqa: F401
from tests.helpers import signup_company

FACE_FIELDS = {"face_on_punch": True, "dpia_reference": "DPIA-ALPHA-2026-07"}


@pytest.fixture
def br22(app):
    admin = signup_company(app, "Alpha Consent Co")  # Asia/Kolkata
    set_policy(admin, requireFaceForSelfCheckIn=False)
    ravi = employee_with_login(app, admin, "Ravi")
    sameer = employee_with_login(app, admin, "Sameer")
    return {"admin": admin, "ravi": ravi, "ravi_id": ravi.me()["employee"]["id"], "sameer": sameer,
            "sameer_id": sameer.me()["employee"]["id"]}


def _consent(client, purpose, status, **extra):
    r = client.post("/api/v1/me/attendance/consents", json={"purpose": purpose, "status": status, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _hr_consent(admin, employee_id, purpose, status, **extra):
    r = admin.post("/api/v1/attendance/consents", json={"employee_id": employee_id, "purpose": purpose,
                                                        "status": status, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _fake_engine(monkeypatch, match_employee=None):
    from app.modules.face_recognition import engine as face_engine

    monkeypatch.setattr(face_engine, "available", lambda: True)
    monkeypatch.setattr(face_engine, "encode", lambda image: "encoding-of-the-probe")
    monkeypatch.setattr(face_engine, "best_match",
                        lambda probe, candidates, threshold: (match_employee or candidates[0][0], 0.2)
                        if candidates else None)


def _jpeg() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, format="JPEG")
    return buffer.getvalue()


def test_as_cap_06_ravi_without_face_consent_punches_with_the_face_step_skipped(br22, fake_clock, monkeypatch):
    """Exit (d) / AS-CAP-06 / T-09: STANDARD face_on_punch for BR 22; Ravi has no BIOMETRIC_FACE consent -> accepted,
    verify_mode NONE, no flag, no review item, face_step SKIPPED — never CONSENT_REQUIRED. Sameer, with a GRANTED
    consent, gets the face step (no image -> FACE_REQUIRED). The activation warns about the alternative."""
    policy = activate_capture(br22["admin"], FACE_FIELDS)
    assert policy["activation"]["warnings"] == [
        "Employees without a face consent punch without face matching (no flag, no review)."]
    fake_clock.at_ist("2026-11-17", "18:25")
    r = v2_punch(br22["ravi"], fake_clock)
    assert r.status_code == 200, r.text
    assert r.json()["face_step"] == "SKIPPED"
    row = punches(br22["ravi_id"])[0]
    assert row["verify_mode"] == "NONE" and row["evidence_flags"] == [] and row["face_match_score"] is None
    assert review_items() == []
    _consent(br22["sameer"], "BIOMETRIC_FACE", "GRANTED", notice_version="ATT-PRIV-IN v3")
    refused = v2_punch(br22["sameer"], fake_clock)
    assert refused.status_code == 400 and refused.json()["code"] == "FACE_REQUIRED"
    _fake_engine(monkeypatch)
    sql("INSERT INTO face_encodings (company_id, employee_id, encoding, is_primary, created_at, updated_at) "
        "VALUES (:c, :e, 'x', true, now(), now())", c=br22["admin"].company_id, e=br22["sameer_id"])
    face = v2_punch(br22["sameer"], fake_clock, image=_jpeg())
    assert face.status_code == 200 and face.json()["face_step"] == "REQUIRED"
    assert punches(br22["sameer_id"])[0]["verify_mode"] == "FACE"


def test_t_09_enrolment_needs_consent_under_a_standard_policy_and_is_encrypted(br22, monkeypatch):
    """T-09 / BUG-AS-19: STANDARD policy -> enrolment without consent is 422 CONSENT_REQUIRED; with an HR-recorded
    consent it is stored encrypted only (no plaintext), linked to the consent, and no API returns an encoding (T-11)."""
    _fake_engine(monkeypatch)
    activate_capture(br22["admin"], FACE_FIELDS)
    url = f"/api/v1/face/enroll?employee_id={br22['ravi_id']}"
    refused = br22["admin"].post(url, files={"file": ("f.jpg", _jpeg(), "image/jpeg")})
    assert refused.status_code == 422 and refused.json()["code"] == "CONSENT_REQUIRED"
    consent = _hr_consent(br22["admin"], br22["ravi_id"], "BIOMETRIC_FACE", "GRANTED", notice_version="ATT-PRIV-IN v3")
    assert consent["channel"] == "HR_RECORDED" and consent["recorded_by_user_id"]
    ok = br22["admin"].post(url, files={"file": ("f.jpg", _jpeg(), "image/jpeg")})
    assert ok.status_code == 200, ok.text
    face = rows("SELECT * FROM face_encodings WHERE employee_id = :e", e=br22["ravi_id"])[0]
    assert face["encoding"] is None and face["encoding_enc"] is not None and face["key_id"]
    assert b"encoding-of-the-probe" not in bytes(face["encoding_enc"])
    assert face["consent_id"] == consent["id"] and face["legacy_enrolled"] is False
    status = br22["admin"].get(f"/api/v1/face/employees/{br22['ravi_id']}")
    assert status.status_code == 200 and set(status.json()) == {"employee_id", "total_encodings", "primary_encoding",
                                                                "engine_available"}
    assert "encoding-of-the-probe" not in status.text and "v1:" not in status.text
    # the matcher decrypts in memory: Ravi's own 1:1 check works with the ciphertext
    from app.services import attendance_faces
    from app.database.db import SessionLocal, bind_tenant
    from app.models.employee import FaceEncoding

    db = SessionLocal()
    try:
        bind_tenant(db, br22["admin"].company_id)
        rows_ = db.query(FaceEncoding).filter(FaceEncoding.employee_id == br22["ravi_id"]).all()
        assert attendance_faces.candidates(db, rows_) == [(br22["ravi_id"], "encoding-of-the-probe")]
    finally:
        db.close()


def test_as_sec_09_legacy_v1_enrolment_keeps_working_within_its_grace(br22, monkeypatch, fake_clock):
    """AS-SEC-09 / SP-AS-219: under LEGACY_V1 (no STANDARD capture version) an enrolment without consent is accepted as
    a legacy enrolment with a grace end; within the grace the face step runs; an explicit withdrawal ends it."""
    from app.database.db import SessionLocal, bind_tenant
    from app.services import attendance_consents

    _fake_engine(monkeypatch)
    url = f"/api/v1/face/enroll?employee_id={br22['ravi_id']}"
    assert br22["admin"].post(url, files={"file": ("f.jpg", _jpeg(), "image/jpeg")}).status_code == 200
    face = rows("SELECT legacy_enrolled, legacy_grace_until, encoding FROM face_encodings WHERE employee_id = :e",
                e=br22["ravi_id"])[0]
    assert face["legacy_enrolled"] is True and face["legacy_grace_until"] is not None and face["encoding"] is None
    db = SessionLocal()
    try:
        bind_tenant(db, br22["admin"].company_id)
        grace = face["legacy_grace_until"]
        assert attendance_consents.face_allowed(db, br22["admin"].company_id, br22["ravi_id"], grace)
        from datetime import timedelta

        assert not attendance_consents.face_allowed(db, br22["admin"].company_id, br22["ravi_id"],
                                                    grace + timedelta(days=1))
    finally:
        db.close()
    _consent(br22["ravi"], "BIOMETRIC_FACE", "DECLINED")
    db = SessionLocal()
    try:
        bind_tenant(db, br22["admin"].company_id)
        assert not attendance_consents.face_allowed(db, br22["admin"].company_id, br22["ravi_id"], grace)
    finally:
        db.close()


def test_ledger_is_append_only_and_withdrawal_keeps_history(br22, fake_clock):
    """C-1 / C-4 / C-8: every change is a new row (the DB refuses UPDATE / DELETE); the state on a past date is still
    GRANTED after a withdrawal; CONSENT_RECORDED is audited without free text; the outbox carries ids only."""
    from datetime import date

    from app.database.db import SessionLocal, bind_tenant
    from app.services import attendance_consents

    fake_clock.at_ist("2026-11-10", "08:05")
    granted = _consent(br22["ravi"], "BIOMETRIC_FACE", "GRANTED", notice_version="ATT-PRIV-IN v3")
    fake_clock.at_ist("2027-02-03", "10:00")
    withdrawn = _consent(br22["ravi"], "BIOMETRIC_FACE", "WITHDRAWN")
    assert (granted["valid_from"], withdrawn["valid_from"]) == ("2026-11-10", "2027-02-03")
    cid = br22["admin"].company_id
    db = SessionLocal()
    try:
        bind_tenant(db, cid)
        assert attendance_consents.has_consent(db, cid, br22["ravi_id"], "BIOMETRIC_FACE", date(2026, 12, 1))
        assert not attendance_consents.has_consent(db, cid, br22["ravi_id"], "BIOMETRIC_FACE", date(2027, 2, 3))
        with pytest.raises(DBAPIError):
            db.execute(text("UPDATE attendance_consents SET status = 'GRANTED' WHERE id = :i"), {"i": withdrawn["id"]})
        db.rollback()
    finally:
        db.close()
    with pytest.raises(DBAPIError):
        sql("DELETE FROM attendance_consents WHERE id = :i", i=granted["id"])  # the guard refuses the owner too
    audit = rows("SELECT details FROM audit_logs WHERE action = 'CONSENT_RECORDED' AND company_id = :c ORDER BY id",
                 c=cid)
    assert [a["details"]["status"] for a in audit] == ["GRANTED", "WITHDRAWN"]
    events = rows("SELECT payload FROM outbox_events WHERE topic = 'attendance.consent.changed' ORDER BY id")
    assert set(events[-1]["payload"]) == {"employee_id", "purpose", "consent_id", "status"}


def test_t_12_withdrawal_deletes_face_data_and_a_legal_hold_postpones_it(br22, monkeypatch):
    """C-5 / B-8 / AS-SEC-10 / T-12: a withdrawn BIOMETRIC_FACE queues the deletion; the consent_expire job purges it
    unless a legal hold covers the employee (then RETENTION_PURGED waits for the release)."""
    from app.database.db import SessionLocal, bind_tenant
    from app.services import attendance_faces, org_structure_service

    _fake_engine(monkeypatch)
    _consent(br22["ravi"], "BIOMETRIC_FACE", "GRANTED")
    url = f"/api/v1/face/enroll?employee_id={br22['ravi_id']}"
    assert br22["admin"].post(url, files={"file": ("f.jpg", _jpeg(), "image/jpeg")}).status_code == 200
    _consent(br22["ravi"], "BIOMETRIC_FACE", "WITHDRAWN")
    face = rows("SELECT retention_until, deleted_at FROM face_encodings WHERE employee_id = :e", e=br22["ravi_id"])[0]
    assert face["retention_until"] is not None and face["deleted_at"] is None
    cid = br22["admin"].company_id
    admin_user = br22["admin"].me()["user"]["id"]
    hold = sql("INSERT INTO legal_holds (company_id, scope, employee_id, reason, status, created_by_user_id, "
               "row_version, created_at, updated_at) VALUES (:c, 'EMPLOYEE', :e, 'Dispute', 'ACTIVE', :u, 1, now(), "
               "now()) RETURNING id", c=cid, e=br22["ravi_id"], u=admin_user).scalar()
    db = SessionLocal()
    try:
        bind_tenant(db, cid)
        from app.models.employee import Employee

        today = org_structure_service.employee_local_today(db, db.get(Employee, br22["ravi_id"]))
        assert attendance_faces.purge_due(db, cid, today) == {"purged": 0, "held": 1}
        db.commit()
        sql("UPDATE legal_holds SET status = 'RELEASED', released_at = now(), released_by_user_id = :u, "
            "release_reason = 'Settled' WHERE id = :h", h=hold, u=br22["sameer"].user_id)
        assert attendance_faces.purge_due(db, cid, today) == {"purged": 1, "held": 0}
        db.commit()
    finally:
        db.close()
    face = rows("SELECT encoding, encoding_enc, deleted_at FROM face_encodings WHERE employee_id = :e",
                e=br22["ravi_id"])[0]
    assert face["encoding"] is None and face["encoding_enc"] is None and face["deleted_at"] is not None
    assert rows("SELECT count(*) AS n FROM audit_logs WHERE action = 'RETENTION_PURGED' AND company_id = :c",
                c=cid)[0]["n"] == 1


def test_c_6_consent_expire_job(br22, fake_clock):
    from datetime import date

    from app.database.db import SessionLocal, bind_tenant
    from app.services import attendance_consents

    fake_clock.at_ist("2026-11-01", "09:00")
    _hr_consent(br22["admin"], br22["ravi_id"], "NIGHT_WORK", "GRANTED", valid_from="2026-11-01",
                valid_to="2026-11-30", notice_version="NW-1")
    cid = br22["admin"].company_id
    db = SessionLocal()
    try:
        bind_tenant(db, cid)
        assert attendance_consents.expire_due(db, cid, date(2026, 11, 30)) == 0
        assert attendance_consents.expire_due(db, cid, date(2026, 12, 1)) == 1
        db.commit()
        assert attendance_consents.expire_due(db, cid, date(2026, 12, 2)) == 0  # idempotent
        assert not attendance_consents.has_consent(db, cid, br22["ravi_id"], "NIGHT_WORK", date(2026, 12, 1))
        assert attendance_consents.has_consent(db, cid, br22["ravi_id"], "NIGHT_WORK", date(2026, 11, 30))
    finally:
        db.close()
    states = rows("SELECT status, valid_from FROM attendance_consents WHERE employee_id = :e ORDER BY id",
                  e=br22["ravi_id"])
    assert [(s["status"], str(s["valid_from"])) for s in states] == [("GRANTED", "2026-11-01"),
                                                                     ("EXPIRED", "2026-12-01")]


def test_location_notice_is_recorded_once_and_documents_are_acknowledged(br22, fake_clock):
    """Step 4: the inline location notice becomes a SELF_SERVICE LOCATION_AT_PUNCH row, so the next punch needs no
    acknowledgement; a published notice that names the purpose is acknowledged once (409 the second time)."""
    set_policy(br22["admin"], locationPunchPolicy="OPTIONAL")
    fake_clock.at_ist("2026-11-03", "09:00")
    refused = v2_punch(br22["ravi"], fake_clock, location_consent=False)
    assert refused.status_code == 422 and refused.json()["details"]["purpose"] == "LOCATION_AT_PUNCH"
    assert v2_punch(br22["ravi"], fake_clock, location_consent=True).status_code == 200
    fake_clock.at_ist("2026-11-03", "18:00")
    assert v2_punch(br22["ravi"], fake_clock, "OUT", location_consent=False).status_code == 200
    ledger = rows("SELECT purpose, status, channel, notice_version FROM attendance_consents WHERE employee_id = :e",
                  e=br22["ravi_id"])
    assert ledger == [{"purpose": "LOCATION_AT_PUNCH", "status": "GRANTED", "channel": "SELF_SERVICE",
                       "notice_version": "INLINE-1"}]
    admin = br22["admin"]
    doc = admin.post("/api/v1/attendance/policy-documents", json={
        "code": "ATT-PRIV-IN", "version": "3", "title": "Attendance privacy notice", "effective_from": "2026-01-01",
        "requires_acknowledgement": True, "purposes": ["LOCATION_AT_PUNCH"], "privacy_contact": "privacy@alpha.test"})
    assert doc.status_code == 201, doc.text
    doc_id = doc.json()["id"]
    assert br22["sameer"].post(f"/api/v1/me/attendance/policy-documents/{doc_id}/acknowledge").status_code == 404
    published = admin.patch(f"/api/v1/attendance/policy-documents/{doc_id}", json={"status": "PUBLISHED"})
    assert published.status_code == 200 and published.json()["status"] == "PUBLISHED"
    edit = admin.patch(f"/api/v1/attendance/policy-documents/{doc_id}", json={"title": "Changed"})
    assert edit.status_code == 409 and edit.json()["code"] == "INVALID_TRANSITION"
    ack = br22["sameer"].post(f"/api/v1/me/attendance/policy-documents/{doc_id}/acknowledge")
    assert ack.status_code == 201, ack.text
    again = br22["sameer"].post(f"/api/v1/me/attendance/policy-documents/{doc_id}/acknowledge")
    assert again.status_code == 409 and again.json()["code"] == "CONFLICT"
    sameer = rows("SELECT purpose, notice_version FROM attendance_consents WHERE employee_id = :e",
                  e=br22["sameer_id"])
    assert sameer == [{"purpose": "LOCATION_AT_PUNCH", "notice_version": "ATT-PRIV-IN v3"}]
    assert v2_punch(br22["sameer"], fake_clock, location_consent=False).status_code == 200


def test_t_20_privacy_panel_shows_only_my_data(br22, fake_clock):
    """T-20 / exit (d): /me/attendance/privacy lists my consents with their notice versions, the retention periods,
    who can see what, the published documents and my evidence views — nothing about Sameer."""
    fake_clock.at_ist("2026-11-10", "08:05")
    _consent(br22["ravi"], "BIOMETRIC_FACE", "GRANTED", notice_version="ATT-PRIV-IN v3")
    _consent(br22["sameer"], "NIGHT_WORK", "GRANTED", notice_version="NW-1")
    panel = br22["ravi"].get("/api/v1/me/attendance/privacy")
    assert panel.status_code == 200, panel.text
    body = panel.json()
    assert body["employee_id"] == br22["ravi_id"]
    face = next(c for c in body["consents"] if c["purpose"] == "BIOMETRIC_FACE")
    assert (face["status"], face["notice_version"], face["can_withdraw"]) == ("GRANTED", "ATT-PRIV-IN v3", True)
    assert next(c for c in body["consents"] if c["purpose"] == "NIGHT_WORK")["status"] is None
    assert [h["employee_id"] for h in body["history"]] == [br22["ravi_id"]]
    labels = [r["label"] for r in body["retention"]]
    assert "Location: 90 days" in labels and "Punch photos: 30 days" in labels
    assert any(label.startswith("Face data: until you withdraw") for label in labels)
    assert body["collection"]["note"] == "Location is recorded only when you punch."
    assert body["viewers"] and body["evidence_views"] == []
    assert str(br22["sameer_id"]) not in [str(h["employee_id"]) for h in body["history"]]

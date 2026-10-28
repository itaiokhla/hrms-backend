"""AS 07 §18 security tests of AS-M2 phase 2 (WI-AS-2.6, 2.9; EC AS-SEC-13, AS-RBAC-08).

* T-15 / AS-RBAC-08: face scores are masked in every session payload; a reveal needs ``attendance:evidence:read``
  (explicit-only: the company admin does not hold it) covering the subject (else 404) and a fresh step-up (403
  ``STEP_UP_REQUIRED``), and writes ``EVIDENCE_VIEWED`` after success only; the subject sees the view in the panel.
* T-16 / BUG-AS-61 / AS-SEC-13 (a): partner delegation can never carry ``location:read``, ``attendance:evidence:read``
  or ``roster:exempt``.
* BUG-AS-62 / T-17 (single process): the legacy punch endpoints and the operator kiosk are rate limited too.
* T-03: consents and policy documents of one tenant are invisible to another.
* Permissions of SP-AS-169 / -220: the device registry and review queue gates moved to the registered codes.
"""

import json

import pytest

from tests.attendance.capture_v2_helpers import grant, rows, sql, step_up
from tests.attendance_helpers import act, employee_with_login, key, set_policy
from tests.helpers import signup_company


@pytest.fixture
def org(app):
    admin = signup_company(app, "Evidence Co")
    set_policy(admin, requireFaceForSelfCheckIn=True)
    hr = employee_with_login(app, admin, "Sunil", roles=("hr_admin",))
    nitin = employee_with_login(app, admin, "Nitin")
    return {"admin": admin, "hr": hr, "nitin": nitin, "nitin_id": nitin.me()["employee"]["id"]}


def _face_punch(org, monkeypatch):
    from app.modules.face_recognition import engine as face_engine

    monkeypatch.setattr(face_engine, "available", lambda: True)
    monkeypatch.setattr(face_engine, "encode", lambda image: "probe")
    monkeypatch.setattr(face_engine, "best_match", lambda probe, candidates, threshold: (candidates[0][0], 0.2))
    sql("INSERT INTO face_encodings (company_id, employee_id, encoding, is_primary, legacy_enrolled, "
        "legacy_grace_until, created_at, updated_at) VALUES (:c, :e, 'x', true, true, CURRENT_DATE + 30, now(), now())",
        c=org["admin"].company_id, e=org["nitin_id"])
    from tests.attendance.test_consents import _jpeg

    r = act(org["nitin"], "PUNCH_IN", {"idempotency_key": key(), "location_consent": True}, image=_jpeg())
    assert r.status_code == 200, r.text
    assert r.json()["face_match_score"] is None and r.json()["face_verified"] is True
    return r.json()["id"], rows("SELECT id FROM attendance_punches WHERE employee_id = :e", e=org["nitin_id"])[0]["id"]


def test_t_15_evidence_is_masked_and_revealed_one_punch_at_a_time(org, monkeypatch):
    session_id, punch_id = _face_punch(org, monkeypatch)
    detail = org["admin"].get(f"/api/v1/attendance/sessions/{session_id}").json()
    assert detail["face_match_score"] is None and all("face_match_score" not in e for e in detail["events"])
    url = f"/api/v1/attendance/punches/{punch_id}/evidence"
    assert org["admin"].post(url).status_code == 403  # company admin: explicit-only code never implied (AS-SEC-13 b)
    assert org["hr"].post(url).status_code == 403  # hr_admin has no default evidence grant
    grant(org["admin"].company_id, "hr_admin", "attendance:evidence:read")
    no_step = org["hr"].post(url)
    assert no_step.status_code == 403 and no_step.json()["code"] == "STEP_UP_REQUIRED"
    assert rows("SELECT count(*) AS n FROM audit_logs WHERE action = 'EVIDENCE_VIEWED'")[0]["n"] == 0
    step_up(org["hr"])
    shown = org["hr"].post(url)
    assert shown.status_code == 200, shown.text
    assert shown.json()["face_match_score"] is not None and shown.json()["verify_mode"] == "FACE"
    audit = rows("SELECT entity_type, entity_id, details FROM audit_logs WHERE action = 'EVIDENCE_VIEWED'")
    assert audit == [{"entity_type": "employee", "entity_id": str(org["nitin_id"]), "details": {"punch_ids": [punch_id]}}]
    assert org["hr"].post("/api/v1/attendance/punches/999999/evidence").status_code == 404
    views = org["nitin"].get("/api/v1/me/attendance/privacy").json()["evidence_views"]
    assert [v["what"] for v in views] == ["Punch evidence"] and views[0]["viewer"]


def test_as_rbac_08_evidence_code_never_widens_reach(org, monkeypatch, app):
    """AS-RBAC-08: a holder of attendance:evidence:read only for a team that does not include the subject gets 404."""
    _, punch_id = _face_punch(org, monkeypatch)
    lead = employee_with_login(app, org["admin"], "Meghna", roles=("manager",))
    grant(org["admin"].company_id, "manager", "attendance:evidence:read", "DIRECT_REPORTS")
    step_up(lead)
    assert lead.post(f"/api/v1/attendance/punches/{punch_id}/evidence").status_code == 404


def test_t_16_partner_delegation_never_carries_location_or_evidence_codes():
    from app.services import partner_delegation_service as pds

    for code in ("location:read", "attendance:evidence:read", "roster:exempt"):
        assert code in pds.NEVER_DELEGABLE and not pds.is_delegable(code), code
    assert pds.is_delegable("attendance:read")


def test_bug_as_62_legacy_punch_endpoints_and_operator_kiosk_are_rate_limited(org):
    set_policy(org["admin"], requireFaceForSelfCheckIn=False)
    codes = [act(org["nitin"], "PUNCH_OUT", {"idempotency_key": key()}).status_code for _ in range(20)]
    assert set(codes) == {400}  # NOT_CHECKED_IN, but counted
    limited = act(org["nitin"], "PUNCH_OUT", {"idempotency_key": key()})
    assert limited.status_code == 429 and limited.json()["code"] == "RATE_LIMITED" and "retry-after" in limited.headers
    image = {"file": ("f.jpg", b"not-an-image", "image/jpeg")}
    for _ in range(60):
        org["admin"].post("/api/v1/attendance/kiosk/scan", files=image)
    scan = org["admin"].post("/api/v1/attendance/kiosk/scan", files=image)
    assert scan.status_code == 429


def test_t_03_consents_and_documents_are_tenant_isolated(org, app):
    r = org["admin"].post("/api/v1/attendance/consents", json={"employee_id": org["nitin_id"], "purpose": "NIGHT_WORK",
                                                               "status": "GRANTED", "notice_version": "NW-1"})
    assert r.status_code == 201, r.text
    doc = org["admin"].post("/api/v1/attendance/policy-documents", json={
        "code": "ATT-PRIV", "version": "1", "title": "Notice", "effective_from": "2026-01-01"})
    assert doc.status_code == 201
    other = signup_company(app, "Other Evidence Co")
    assert other.get("/api/v1/attendance/consents").json()["total"] == 0
    assert other.get("/api/v1/attendance/policy-documents").json()["items"] == []
    assert other.patch(f"/api/v1/attendance/policy-documents/{doc.json()['id']}",
                       json={"status": "PUBLISHED"}).status_code == 404
    foreign = other.post("/api/v1/attendance/consents", json={"employee_id": org["nitin_id"], "purpose": "NIGHT_WORK",
                                                              "status": "GRANTED"})
    assert foreign.status_code == 404


def test_registered_codes_gate_devices_review_and_policy(org):
    """SP-AS-169 / -220: hr_admin manages devices (attendance:manage_devices), sites and capture policy and reviews
    non-location items (attendance:review); an employee holds none of them."""
    hr, nitin = org["hr"], org["nitin"]
    assert hr.get("/api/v1/attendance/devices").status_code == 200
    assert hr.get("/api/v1/attendance/sites").status_code == 200
    assert hr.get("/api/v1/attendance/policies").status_code == 200
    assert hr.get("/api/v1/attendance/review").status_code == 200
    for path in ("/api/v1/attendance/devices", "/api/v1/attendance/sites", "/api/v1/attendance/policies",
                 "/api/v1/attendance/consents", "/api/v1/attendance/policy-documents"):
        assert nitin.get(path).status_code == 403, path
    grants = rows("SELECT rp.permission_code FROM role_permissions rp JOIN roles r ON r.id = rp.role_id "
                  "WHERE r.company_id = :c AND r.system_key = 'hr_admin' AND rp.permission_code LIKE 'attendance:%'",
                  c=org["admin"].company_id)
    codes = {g["permission_code"] for g in grants}
    assert {"attendance:review", "attendance:manage_devices", "attendance:manage_sites",
            "attendance:manage_policy"} <= codes and "attendance:evidence:read" not in codes


def test_my_consent_payload_rejects_unknown_purposes(org):
    r = org["nitin"].post("/api/v1/me/attendance/consents", data=json.dumps({"purpose": "DNA", "status": "GRANTED"}),
                          headers={"Content-Type": "application/json"})
    assert r.status_code == 422

"""Remaining attendance goals: G-15 engine routing and notice consent, G-22 packs and bundles, G-23 readiness and
acceptance, G-24 channels (signed webhooks), browser push and the calendar feed.

Reuses the AS-M6 ``plant`` (Ravi EARLY Mon–Fri, Sita mixed, Mohan OFF; the week starts 8–14 days ahead).
"""

import hashlib
import hmac
import json
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tests.attendance.capture_v2_helpers import rows, sql
from tests.attendance_helpers import employee_with_login
from tests.helpers import ApiClient
from tests.shifts.test_shift_requests import _today, enable, entry, plant, submit  # noqa: F401 - fixture


# =============================================================================================== G-15
def test_pickup_routes_through_the_engine_and_the_inbox(plant):
    wed = entry(plant, "Ravi", 2)
    submit(plant, "Ravi", {"kind": "RELEASE", "entry_id": wed["id"], "reason": "Doctor"})
    pool = plant["people"]["Mohan"].get("/api/v1/open-shifts", params={"from": plant["start"].isoformat()}).json()
    shift = next(s for s in pool["items"] if s["source"] == "RELEASED")
    r = plant["people"]["Mohan"].post(f"/api/v1/open-shifts/{shift['id']}/claim", json={})
    assert r.status_code == 201 and r.json()["status"] == "PENDING_APPROVAL", r.text
    claim = rows("SELECT approval_request_id FROM shift_requests WHERE id = :i", i=r.json()["id"])[0]
    assert claim["approval_request_id"], "PENDING_APPROVAL requests open an engine request"
    engine = rows("SELECT module, subject_type, status FROM approval_requests WHERE id = :i",
                  i=claim["approval_request_id"])[0]
    assert engine == {"module": "shift_request", "subject_type": "shift_request", "status": "PENDING"}
    inbox = plant["admin"].get("/api/v1/approvals", params={"module": "shift_request"}).json()["items"]
    item = next(i for i in inbox if i["id"] == claim["approval_request_id"])
    assert item["subject"]["summary"].startswith("Pickup by") and item["subject"]["link"] == "/attendance/shift-requests"
    # The claimant cannot decide their own request through the module-neutral inbox either.
    r = plant["people"]["Mohan"].post(f"/api/v1/approvals/{claim['approval_request_id']}/decide",
                                      json={"decision": "APPROVE"})
    assert r.status_code in (403, 404, 409), r.text
    r = plant["admin"].post(f"/api/v1/approvals/{claim['approval_request_id']}/decide",
                            json={"decision": "APPROVE", "remarks": "Covered"})
    assert r.status_code == 200, r.text
    after = rows("SELECT status, decision_note FROM shift_requests WHERE id = :i", i=r.json()["subject_id"])[0]
    assert after["status"] == "APPROVED" and after["decision_note"] == "Covered"
    assert entry(plant, "Mohan", 2)["kind"] == "WORK"


def test_withdrawing_a_routed_request_cancels_the_engine_request(plant):
    fri = entry(plant, "Ravi", 4)
    req = submit(plant, "Ravi", {"kind": "CHANGE", "entry_id": fri["id"],
                                 "requested_change": {"shift_id": plant["shifts"]["LATE"]}, "reason": "Class"})
    engine_id = rows("SELECT approval_request_id FROM shift_requests WHERE id = :i", i=req["id"])[0][
        "approval_request_id"]
    assert engine_id
    r = plant["people"]["Ravi"].post(f"/api/v1/shift-requests/{req['id']}/withdraw")
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED", r.text
    assert rows("SELECT status FROM approval_requests WHERE id = :i", i=engine_id)[0]["status"] == "CANCELLED"


def _us_entity(cid: int) -> int:
    le = rows("SELECT id FROM legal_entities WHERE company_id = :c ORDER BY id LIMIT 1", c=cid)[0]["id"]
    sql("UPDATE legal_entities SET country_code = 'US' WHERE id = :i", i=le)
    enable(cid, "attendance.packs.enabled")
    return le


def test_claim_inside_the_notice_window_needs_consent_once_the_us_pack_is_adopted(plant):
    _us_entity(plant["cid"])
    soon = _today() + timedelta(days=3)
    r = plant["admin"].post("/api/v1/open-shifts", json={"branch_id": plant["branch"]["id"],
                                                         "shift_date": soon.isoformat(),
                                                         "custom_start_time": "10:00", "custom_end_time": "16:00",
                                                         "approval_required": False})
    assert r.status_code == 201, r.text
    shift = r.json()
    r = plant["people"]["Mohan"].post(f"/api/v1/open-shifts/{shift['id']}/claim", json={})
    assert r.status_code == 422 and r.json()["code"] == "CONSENT_REQUIRED", r.text
    assert rows("SELECT count(*) AS n FROM shift_requests WHERE open_shift_id = :i", i=shift["id"])[0]["n"] == 0
    r = plant["people"]["Mohan"].post(f"/api/v1/open-shifts/{shift['id']}/claim", json={"consent_given": True})
    assert r.status_code == 201 and r.json()["status"] == "APPROVED", r.text
    stored = rows("SELECT consent_ids FROM shift_requests WHERE id = :i", i=r.json()["id"])[0]["consent_ids"]
    assert len(stored) == 1
    consent = rows("SELECT purpose, scope, status FROM attendance_consents WHERE id = :i", i=stored[0])[0]
    assert consent == {"purpose": "SCHEDULE_CHANGE_ACCEPTED", "scope": "PER_INSTANCE", "status": "GRANTED"}


# =============================================================================================== G-22
def test_packs_view_adoption_and_ramadan_limit(plant):
    from app.services import working_time_packs

    admin = plant["admin"]
    view = admin.get("/api/v1/attendance/packs")
    assert view.status_code == 200, view.text
    body = view.json()
    assert sorted(body["available"]) == ["AE", "IN", "US"]
    entity = body["items"][0]
    assert entity["has_pack"] is True and entity["adopted"] is False and entity["verified"] is False
    r = admin.put(f"/api/v1/attendance/packs/{entity['legal_entity_id']}", json={"enabled": True, "reason": "Go live"})
    assert r.status_code == 200, r.text
    assert next(i for i in r.json()["items"] if i["legal_entity_id"] == entity["legal_entity_id"])["adopted"] is True
    assert admin.put(f"/api/v1/attendance/packs/{entity['legal_entity_id']}",
                     json={"enabled": False, "reason": "x"}).status_code == 422
    worker = plant["people"]["Ravi"]
    assert worker.get("/api/v1/attendance/packs").status_code == 403
    ae = working_time_packs.pack("AE")
    assert working_time_packs.daily_max_on(ae, date(2027, 2, 10)) == 6
    assert working_time_packs.daily_max_on(ae, date(2027, 4, 10)) == 10


def test_bundles_create_draft_policies_once(plant):
    admin = plant["admin"]
    listed = admin.get("/api/v1/attendance/bundles").json()["items"]
    assert {b["code"] for b in listed} >= {"IN_FACTORY_24X7", "US_RETAIL_FWW", "AE_RETAIL_SPLIT"}
    r = admin.post("/api/v1/attendance/bundles/US_RETAIL_FWW/apply")
    assert r.status_code == 201, r.text
    created = r.json()["created"]
    assert {c["type"] for c in created} == {"ATTENDANCE_RULES_POLICY", "SCHEDULING_POLICY"}
    versions = rows("SELECT status, fields FROM policy_versions WHERE policy_id = ANY(:ids)",
                    ids=[c["policy_id"] for c in created])
    assert all(v["status"] == "DRAFT" for v in versions)
    sched = next(v["fields"] for v in versions if "publish_notice_days" in v["fields"])
    assert sched["publish_notice_days"] == 14 and sched["min_rest_hours"] == 10
    again = admin.post("/api/v1/attendance/bundles/US_RETAIL_FWW/apply")
    assert again.status_code == 409 and again.json()["code"] == "POLICY_NAME_TAKEN"
    assert next(b for b in admin.get("/api/v1/attendance/bundles").json()["items"]
                if b["code"] == "US_RETAIL_FWW")["applied"] is True
    assert admin.post("/api/v1/attendance/bundles/NOPE/apply").status_code == 404


def test_fair_workweek_estimate_counts_employer_changes_inside_notice(plant):
    le = _us_entity(plant["cid"])
    mon = entry(plant, "Ravi", 0)
    r = plant["admin"].post("/api/v1/rosters/direct-publish", json={
        "cells": [{"action": "UPSERT", "employee_id": plant["ids"]["Ravi"], "shift_date": mon["shift_date"].isoformat(),
                   "entry_seq": 1, "kind": "WORK", "shift_id": plant["shifts"]["LATE"]}], "reason_code": "PLANNER",
        "acknowledged_warnings": ["LINT_EARLY_START", "INSUFFICIENT_REST_HOURS"],
        "reasons": {"NIGHT_WORK_CONSENT_MISSING": "Agreed", "INSUFFICIENT_REST_HOURS": "Agreed"}})
    assert r.status_code in (200, 422), r.text
    start, end = plant["start"], plant["start"] + timedelta(days=6)
    owed = rows("SELECT count(*) AS n FROM roster_entry_changes WHERE employee_id = ANY(:e) AND shift_date BETWEEN "
                ":s AND :t AND initiated_by = 'EMPLOYER' AND change_kind <> 'CREATED' AND notice_hours < 336 "
                "AND request_id IS NULL AND consent_id IS NULL", e=list(plant["ids"].values()), s=start, t=end)[0]["n"]
    est = plant["admin"].get(f"/api/v1/attendance/packs/{le}/fair-workweek",
                             params={"from": start.isoformat(), "to": end.isoformat()})
    assert est.status_code == 200, est.text
    body = est.json()
    assert body["applies"] is True and body["notice_hours"] == 336 and body["adopted"] is True
    assert float(body["total_hours"]) == owed and len(body["items"]) == owed


# =============================================================================================== G-23
def test_readiness_checklist_and_acceptance_sign_off_rules(plant):
    admin = plant["admin"]
    r = admin.get("/api/v1/attendance/readiness")
    assert r.status_code == 200, r.text
    keys = {i["key"] for i in r.json()["items"]}
    assert {"v2_prepared", "cutover", "standard_rules", "shifts", "schedules", "holidays",
            "routes_attendance", "routes_shift_request", "channels"} <= keys
    assert next(i for i in r.json()["items"] if i["key"] == "shifts")["status"] == "PASS"
    assert next(i for i in r.json()["items"] if i["key"] == "cutover")["status"] == "PASS"  # v2 is active
    r = admin.post("/api/v1/attendance/readiness/acceptance")
    assert r.status_code == 202, r.text
    run = admin.get(f"/api/v1/attendance/readiness/acceptance/{r.json()['job_id']}").json()
    assert run["status"] == "SUCCEEDED", run
    assert {p["key"] for p in run["probes"]} >= {"schedule_resolution", "engine_attendance", "engine_shift_request",
                                                 "engine_team_inputs"}
    assert all(p["status"] == "PASS" for p in run["probes"]), run["probes"]
    sign = admin.post(f"/api/v1/attendance/readiness/acceptance/{run['job_id']}/sign-off", json={})
    expected = "SELF_DECISION_FORBIDDEN" if run["summary"]["ready"] else "ACCEPTANCE_NOT_READY"
    assert sign.status_code == 409 and sign.json()["code"] == expected, sign.text
    assert plant["people"]["Ravi"].get("/api/v1/attendance/readiness").status_code == 403
    assert admin.get("/api/v1/attendance/readiness").json()["last_acceptance"]["job_id"] == run["job_id"]


# =============================================================================================== G-24 channels
class _Receiver(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        _Receiver.received.append({"headers": {k.lower(): v for k, v in self.headers.items()}, "body": self.rfile.read(length)})
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def receiver():
    _Receiver.received = []
    server = HTTPServer(("127.0.0.1", 0), _Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/hook", _Receiver.received
    server.shutdown()


def test_signed_webhook_channel_receives_events(plant, receiver):
    url, received = receiver
    admin = plant["admin"]
    assert plant["people"]["Ravi"].get("/api/v1/integrations/channels").status_code == 403
    bad = admin.post("/api/v1/integrations/channels", json={"kind": "WEBHOOK", "name": "Ops", "url": "ftp://x",
                                                            "events": ["shift_request.submitted"]})
    assert bad.status_code == 400 and bad.json()["code"] == "INVALID_URL"
    r = admin.post("/api/v1/integrations/channels", json={"kind": "WEBHOOK", "name": "Ops hook", "url": url,
                                                          "events": ["shift_request.submitted"]})
    assert r.status_code == 201, r.text
    channel = r.json()
    assert channel["has_secret"] is True and "url" not in channel and channel["url_hint"].startswith("http://127.0.0.1")
    stored = rows("SELECT url_enc, secret_enc FROM notification_channels WHERE id = :i", i=channel["id"])[0]
    assert url not in stored["url_enc"]  # sealed
    secret = admin.post(f"/api/v1/integrations/channels/{channel['id']}/secret").json()["secret"]
    test = admin.post(f"/api/v1/integrations/channels/{channel['id']}/test")
    assert test.status_code == 200 and test.json()["status"] == "SENT", test.text
    first = received[-1]
    stamp, signature = first["headers"]["x-hrms-timestamp"], first["headers"]["x-hrms-signature"]
    expected = "sha256=" + hmac.new(secret.encode(), stamp.encode() + b"." + first["body"], hashlib.sha256).hexdigest()
    assert signature == expected and json.loads(first["body"])["event"] == "channel.test"
    # A routed shift request emits shift_request.submitted (delivered by the inline job after commit).
    fri = entry(plant, "Ravi", 4)
    submit(plant, "Ravi", {"kind": "CHANGE", "entry_id": fri["id"],
                           "requested_change": {"shift_id": plant["shifts"]["LATE"]}, "reason": "Class"})
    events = [json.loads(x["body"])["event"] for x in received]
    assert "shift_request.submitted" in events, events
    body = json.loads(next(x["body"] for x in received if b"shift_request.submitted" in x["body"]))
    assert "Class" not in json.dumps(body)  # reasons never leave the tenant
    log = admin.get(f"/api/v1/integrations/channels/{channel['id']}/deliveries").json()["items"]
    assert {d["event_type"] for d in log} == {"channel.test", "shift_request.submitted"}
    assert all(d["status"] == "SENT" for d in log)
    # Pausing stops deliveries; WhatsApp is a provider slot only.
    r = admin.patch(f"/api/v1/integrations/channels/{channel['id']}", json={"active": False})
    assert r.status_code == 200 and r.json()["active"] is False and r.json()["row_version"] == 2
    wa = admin.post("/api/v1/integrations/channels", json={"kind": "WHATSAPP", "name": "WA", "events": []}).json()
    assert wa["provider_ready"] is False
    assert admin.post(f"/api/v1/integrations/channels/{wa['id']}/test").json()["status"] == "SKIPPED"
    assert admin.delete(f"/api/v1/integrations/channels/{wa['id']}").status_code == 204


# =============================================================================================== G-24 push
def test_push_subscription_tickle_and_vapid_signature(plant):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    import base64

    from app.services import integrations

    ravi = plant["people"]["Ravi"]
    status = ravi.get("/api/v1/me/push").json()
    assert status["subscriptions"] == 0 and status["vapid_public_key"]
    r = ravi.post("/api/v1/me/push", json={"endpoint": "https://push.example.test/send/abc",
                                           "keys": {"p256dh": "BPk", "auth": "xyz"}, "expirationTime": None})
    assert r.status_code == 200 and r.json()["subscriptions"] == 1, r.text
    assert ravi.post("/api/v1/me/push", json={"endpoint": "http://insecure/x"}).status_code == 400
    # A notification for Ravi queues a payload-less push.
    fri = entry(plant, "Ravi", 4)
    req = submit(plant, "Ravi", {"kind": "SWAP", "entry_id": fri["id"], "target_employee_id": plant["ids"]["Sita"]})
    plant["people"]["Sita"].post(f"/api/v1/shift-requests/{req['id']}/decline", json={"note": "Busy"})
    ravi_user = ravi.me()["user"]["id"]
    jobs = rows("SELECT payload FROM jobs WHERE type = 'push.tickle' AND company_id = :c", c=plant["cid"])
    assert any(j["payload"].get("user_id") == ravi_user for j in jobs), jobs
    # The VAPID JWT verifies with the published key.
    header = integrations._vapid_header("https://push.example.test/send/abc")
    token = header.split("t=")[1].split(",")[0]
    head, claims, sig = token.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    raw = base64.urlsafe_b64decode(pad(sig))
    public = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), base64.urlsafe_b64decode(pad(integrations.vapid_public_key())))
    public.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                  f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    assert json.loads(base64.urlsafe_b64decode(pad(claims)))["aud"] == "https://push.example.test"
    r = ravi.request("DELETE", "/api/v1/me/push", json={"endpoint": "https://push.example.test/send/abc"})
    assert r.status_code == 200 and r.json()["subscriptions"] == 0


# =============================================================================================== G-24 calendar
def test_calendar_feed_serves_the_published_schedule_until_revoked(app, plant):
    ravi = plant["people"]["Ravi"]
    assert ravi.get("/api/v1/me/calendar-feed").json()["active"] is False
    r = ravi.post("/api/v1/me/calendar-feed")
    assert r.status_code == 200 and r.json()["active"] is True, r.text
    path = r.json()["path"]
    anonymous = ApiClient(app)
    feed = anonymous.get(path)
    assert feed.status_code == 200 and feed.headers["content-type"].startswith("text/calendar"), feed.text
    assert feed.text.count("BEGIN:VEVENT") >= 5 and "SUMMARY:Shift EARLY" in feed.text
    assert anonymous.get(path.replace(".ics", "x.ics")).status_code == 404
    assert anonymous.get("/api/v1/calendar/999999.abcdefghijklmnopqrstuvwxyz.ics").status_code == 404
    rotated = ravi.post("/api/v1/me/calendar-feed").json()["path"]
    assert anonymous.get(path).status_code == 404 and anonymous.get(rotated).status_code == 200
    assert ravi.request("DELETE", "/api/v1/me/calendar-feed").json()["active"] is False
    assert anonymous.get(rotated).status_code == 404

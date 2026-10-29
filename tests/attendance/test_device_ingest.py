"""AS-M7 devices and ingest (G-17; 04-attendance/01 §7, §8; AS 07 SR-AS-02, -04, -18, -19).

Registration and the serial registry (AS-SEC-02), quarantine of unknown serials (AS-SEC-01, I-4), activation with
step-up, claim code and proof of possession, the secret shown once and only its hash stored (AS-SEC-07), ADMS
handshake / ATTLOG / getrequest / devicecmd, save-before-ACK with natural-key de-duplication (AS-DEV-01, P-2),
malformed lines held encrypted (AS-DEV-14), mappings with canonical ids (AS-DEV-11) and M-7 effects, pairing into a
device session and day evaluation (AS-CAP-11), future-dated records (AS-DEV-05), watermarks keeping the day
PROVISIONAL (AS-DEV-07), suspended and v1 tenants, the command allow-list (SR-AS-04), agent HMAC batches
(AS-SEC-08 signature part, J-4), CSV imports (AS-DEV-10) and tenant isolation.
"""

import hashlib
import hmac
import io
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from tests.attendance.capture_v2_helpers import rows, sql
from tests.attendance.test_requests import enable_v2
from tests.attendance_helpers import IST, fake_clock  # noqa: F401
from tests.helpers import create_employee, new_client, signup_company

DEVICES = "/api/v1/attendance/devices"
MAPPINGS = "/api/v1/attendance/device-mappings"
SERIAL = "CQZ7224960001"


@pytest.fixture
def plant(app, fake_clock):
    from app.core.rate_limit import limiter

    fake_clock.at_ist("2026-11-11", "08:00")
    admin = signup_company(app, "Pune Plant Co")  # Asia/Kolkata
    enable_v2(admin.company_id)
    sameer = create_employee(admin, "Sameer", "Khan", employee_code="0417")
    imran = create_employee(admin, "Imran", "Shaikh", employee_code="0418")
    limiter.reset()
    return {"app": app, "admin": admin, "cid": admin.company_id, "sameer": sameer, "imran": imran,
            "clock": fake_clock, "device": new_client(app)}


def step_up(client):
    """A fresh step-up at the fake clock's time (the clock runs ahead of the database's now())."""
    from app.core import clock

    sql("UPDATE user_sessions SET step_up_at = :t, step_up_method = 'EMAIL_OTP' WHERE user_id = :u",
        t=clock.utcnow(), u=client.me()["user"]["id"])


def register(admin, code="DEV-PUNE-GATE-A", serial=SERIAL, kind="TERMINAL", **extra):
    body = {"code": code, "name": "Pune plant gate A", "kind": kind, **extra}
    if serial is not None:
        body["serial_number"] = serial
    return admin.post(DEVICES, json=body)


def handshake(device, serial=SERIAL, secret=None):
    params = {"SN": serial, "options": "all", "pushver": "2.4.1"}
    if secret:
        params["key"] = secret
    return device.http.get("/iclock/cdata", params=params)


def upload(device, body, serial=SERIAL, secret=None, prefix="/iclock", **params):
    query = {"SN": serial, "table": "ATTLOG", "Stamp": "9999", **params}
    if secret:
        query["key"] = secret
    return device.http.post(f"{prefix}/cdata", params=query, content=body.encode("utf-8"))


def activated_terminal(plant, **extra):
    admin = plant["admin"]
    r = register(admin, **extra)
    assert r.status_code == 201, r.text
    created = r.json()
    assert handshake(plant["device"]).status_code == 401  # first contact: quarantined
    step_up(admin)
    r = admin.post(f"{DEVICES}/{created['id']}/activate", json={"claim_code": created["claim_code"],
                                                                 "serial_number": SERIAL})
    assert r.status_code == 200, r.text
    return created, r.json()


def test_as_sec_01_unknown_serial_is_quarantined_with_the_same_401(plant):
    device = plant["device"]
    r = handshake(device, serial="UNKNOWN0001")
    assert r.status_code == 401 and r.text == "DEVICE_AUTH_FAILED"
    r = upload(device, "1\t2026-11-11 09:00:00\t0\t1\n", serial="UNKNOWN0001")
    assert r.status_code == 401 and r.text == "DEVICE_AUTH_FAILED" and "UNKNOWN" not in r.text.replace("DEVICE", "")
    q = rows("SELECT * FROM device_quarantine WHERE serial_number = 'UNKNOWN0001'")[0]
    assert q["status"] == "SEEN" and q["payload_count"] == 1 and q["last_ip_hash"]
    assert rows("SELECT count(*) AS n FROM attendance_punches")[0]["n"] == 0  # I-4: no tenant rows
    assert device.http.get("/iclock/ping").status_code == 401  # no serial at all


def test_as_sec_02_register_activate_proof_of_possession_and_secret_once(plant, app):
    admin = plant["admin"]
    assert register(admin, serial=None).status_code == 400  # a TERMINAL needs its serial
    r = register(admin)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["kind"] == "TERMINAL" and created["status"] == "PENDING_ACTIVATION" and created["claim_code"]
    assert "activate" in created["allowed_actions"] and created["integration"] == "PUSH"
    activate = f"{DEVICES}/{created['id']}/activate"
    r = admin.post(activate, json={"claim_code": created["claim_code"], "serial_number": SERIAL})
    assert r.status_code == 403 and r.json()["code"] == "STEP_UP_REQUIRED"
    step_up(admin)
    r = admin.post(activate, json={"claim_code": created["claim_code"], "serial_number": SERIAL})
    assert r.status_code == 422 and r.json()["code"] == "DEVICE_NOT_SEEN"  # never contacted the server
    assert handshake(plant["device"]).status_code == 401
    contact = admin.get(f"{DEVICES}/{created['id']}/first-contact").json()
    assert contact["seen"] is True and "…" in contact["ip_hash"]
    r = admin.post(activate, json={"claim_code": created["claim_code"], "serial_number": "OTHER1"})
    assert r.status_code == 422 and r.json()["code"] == "SERIAL_MISMATCH"
    r = admin.post(activate, json={"claim_code": "AAAA-BBBB", "serial_number": SERIAL})
    assert r.status_code == 422 and r.json()["code"] == "CLAIM_CODE_INVALID"
    r = admin.post(activate, json={"claim_code": created["claim_code"], "serial_number": SERIAL.lower()})
    assert r.status_code == 200, r.text
    body = r.json()
    secret = body["connection"]["secret"]
    assert body["status"] == "ACTIVE" and body["backfill_from"] == "2026-11-04" and len(secret) >= 24
    stored = rows("SELECT secret_hash, secret_enc FROM attendance_devices WHERE id = :d", d=created["id"])[0]
    assert stored["secret_hash"] and secret not in stored["secret_hash"] and stored["secret_enc"] is None
    detail = admin.get(f"{DEVICES}/{created['id']}").json()
    assert "connection" not in detail and secret not in json.dumps(detail) and detail["has_secret"] is True
    assert rows("SELECT status FROM device_quarantine WHERE serial_number = :s", s=SERIAL)[0]["status"] == "CLAIMED"
    index = rows("SELECT company_id, device_id, active FROM device_registry_index WHERE serial_number = :s", s=SERIAL)
    assert index == [{"company_id": plant["cid"], "device_id": created["id"], "active": True}]
    other = signup_company(app, "Other Plant Co")
    enable_v2(other.company_id)
    r = register(other, code="GATE-B")
    assert r.status_code == 409 and r.json()["code"] == "DEVICE_SERIAL_TAKEN"
    assert "Pune" not in r.text  # never names the other tenant


def test_as_sec_07_secret_checked_in_constant_time_and_rotation_stops_the_old_one(plant):
    created, body = activated_terminal(plant)
    device, secret = plant["device"], body["connection"]["secret"]
    assert handshake(device).status_code == 401  # no secret
    assert handshake(device, secret="wrong").status_code == 401
    r = handshake(device, secret=secret)
    assert r.status_code == 200 and r.text.startswith(f"GET OPTION FROM: {SERIAL}") and "ATTLOGStamp=0" in r.text
    assert "TransFlag=TransData AttLog" in r.text
    r = device.http.get("/iclock/cdata", params={"SN": SERIAL, "options": "all"},
                        headers={"X-Device-Secret": secret})
    assert r.status_code == 200  # header form
    step_up(plant["admin"])
    rotated = plant["admin"].post(f"{DEVICES}/{created['id']}/rotate-secret").json()["connection"]["secret"]
    assert handshake(device, secret=secret).status_code == 401
    assert handshake(device, secret=rotated).status_code == 200


def test_as_dev_01_save_before_ack_dedup_rejects_mapping_and_pairing(plant):
    admin, device = plant["admin"], plant["device"]
    created, body = activated_terminal(plant)
    secret = body["connection"]["secret"]
    plant["clock"].at_ist("2026-11-11", "09:05")
    attlog = ("0417\t2026-11-11 09:00:30\t0\t1\t0\n"
              "0417\t2026-11-11 09:00:50\t0\t1\t0\n"   # second scan within 60 s
              "9045\t2026-11-11 09:02:00\t0\t15\n"     # unmapped PIN
              "0417\t2026-13-11 09:03:00\t0\t1\n")     # malformed
    r = upload(device, attlog, secret=secret)
    assert r.status_code == 200 and r.text == "OK: 4", r.text
    punches = rows("SELECT * FROM attendance_punches WHERE device_id = :d ORDER BY id", d=created["id"])
    assert len(punches) == 3 and {p["source"] for p in punches} == {"DEVICE"}
    assert all(p["employee_id"] is None and p["mapping_status"] == "UNMAPPED" for p in punches)
    assert all(p["disposition"] == "PENDING" and p["device_tz"] == "Asia/Kolkata" for p in punches)
    assert punches[0]["device_user_id"] == "417" and punches[0]["verify_mode"] == "FINGER"
    assert punches[0]["occurred_at"] == datetime(2026, 11, 11, 3, 30, 30, tzinfo=timezone.utc)
    reject = rows("SELECT * FROM attendance_ingest_rejects WHERE device_id = :d", d=created["id"])[0]
    assert reject["reason"] == "INVALID_DATETIME" and "2026-13-11" not in (reject["raw_line_enc"] or "")
    items = rows("SELECT dedup_key FROM attendance_review_items WHERE kind = 'UNMAPPED_PUNCH' AND status = 'OPEN'")
    assert {i["dedup_key"] for i in items} == {f"UNMAPPED_PUNCH:{created['id']}:417",
                                               f"UNMAPPED_PUNCH:{created['id']}:9045"}
    # P-2: a protocol resend collapses onto the first rows and is still acknowledged.
    assert upload(device, attlog, secret=secret).text == "OK: 4"
    assert len(rows("SELECT id FROM attendance_punches WHERE device_id = :d", d=created["id"])) == 3
    # AS-DEV-11: 0417 vs 417 is the same canonical id; a non-numeric id on a ZK device is refused.
    r = admin.post(MAPPINGS, json={"items": [{"device_id": created["id"], "device_user_id": "ALP-1",
                                              "employee_id": plant["imran"]["id"]}]})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_FIELDS"
    r = admin.post(MAPPINGS, json={"items": [{"device_id": created["id"], "device_user_id": "00417",
                                              "employee_id": plant["sameer"]["id"],
                                              "effective_from": "2026-11-01"}]})
    assert r.status_code == 201, r.text
    assert r.json()["items"][0]["device_user_id"] == "417" and r.json()["items"][0]["sync_state"] == "PENDING"
    r = admin.post(MAPPINGS, json={"items": [{"device_id": created["id"], "device_user_id": "417",
                                              "employee_id": plant["imran"]["id"], "effective_from": "2026-11-05"}]})
    assert r.status_code == 409 and r.json()["code"] == "MAPPING_OVERLAP"
    # M-7: the unmapped punches are mapped and paired; the review item is resolved.
    mapped = rows("SELECT employee_id, mapping_status, disposition, session_id FROM attendance_punches "
                  "WHERE device_user_id = '417' ORDER BY id")
    assert [m["disposition"] for m in mapped] == ["USED", "IGNORED_DUPLICATE"]
    assert all(m["employee_id"] == plant["sameer"]["id"] for m in mapped)
    session = rows("SELECT * FROM attendance WHERE id = :s", s=mapped[0]["session_id"])[0]
    assert session["capture_source"] == "DEVICE" and session["check_out_time"] is None
    assert session["pairing_key"] == f"dev:{plant['sameer']['id']}:2026-11-11"
    assert rows("SELECT status FROM attendance_review_items WHERE dedup_key = :k",
                k=f"UNMAPPED_PUNCH:{created['id']}:417")[0]["status"] == "RESOLVED"
    # The OUT scan closes the device session; AS-CAP-11: the day row is evaluated from it.
    plant["clock"].at_ist("2026-11-11", "18:06")
    assert upload(device, "417\t2026-11-11 18:05:10\t1\t1\n", secret=secret).text == "OK: 1"
    session = rows("SELECT * FROM attendance WHERE id = :s", s=mapped[0]["session_id"])[0]
    assert session["check_out_time"] == datetime(2026, 11, 11, 12, 35, 10, tzinfo=timezone.utc)
    day = rows("SELECT * FROM attendance_days WHERE employee_id = :e AND shift_date = '2026-11-11'",
               e=plant["sameer"]["id"])
    assert day and day[0]["gross_minutes"] == 544 and day[0]["evaluation_state"] == "OPEN"  # 09:00:30-18:05:10
    summary = admin.get(f"{DEVICES}/{created['id']}/summary").json()
    assert summary["open_rejects"] == 1 and summary["device"]["health"] == "ONLINE"
    assert summary["device"]["watermark_at"].startswith("2026-11-11T12:35:10")


def test_gv_as_cap_12_backlog_after_a_handshake_is_not_clock_skew_but_real_time_drift_is(plant):
    """T-4 (AS-DEV-06): old scans uploaded right after the handshake are a backlog (no CLOCK_SKEW); once the device
    polls, an upload whose newest record is 10 minutes behind the server is real-time drift."""
    created, body = activated_terminal(plant)
    secret, device, clock = body["connection"]["secret"], plant["device"], plant["clock"]
    clock.at_ist("2026-11-11", "14:25")
    assert handshake(device, secret=secret).status_code == 200
    clock.advance(seconds=20)
    backlog = "".join(f"417\t2026-11-11 14:{m:02d}:10\t0\t1\n" for m in range(5, 25, 5))
    assert upload(device, backlog, secret=secret).text == "OK: 4"
    stored = rows("SELECT last_clock_skew_seconds, health FROM attendance_devices WHERE id = :d", d=created["id"])[0]
    assert stored["last_clock_skew_seconds"] in (None, 0)
    assert not rows("SELECT id FROM attendance_review_items WHERE kind = 'DEVICE_CLOCK_SKEW'")
    clock.advance(seconds=30)
    assert device.http.get("/iclock/getrequest", params={"SN": SERIAL, "key": secret}).text == "OK"
    clock.advance(seconds=30)
    assert upload(device, "418\t2026-11-11 14:16:00\t0\t1\n", secret=secret).text == "OK: 1"  # 10 min behind
    stored = rows("SELECT last_clock_skew_seconds FROM attendance_devices WHERE id = :d", d=created["id"])[0]
    assert stored["last_clock_skew_seconds"] >= 600
    assert rows("SELECT id FROM attendance_review_items WHERE kind = 'DEVICE_CLOCK_SKEW'")


def test_as_dev_05_future_dated_record_is_stored_but_never_advances_the_watermark(plant):
    created, body = activated_terminal(plant)
    secret = body["connection"]["secret"]
    plant["admin"].post(MAPPINGS, json={"items": [{"device_user_id": "417", "employee_id": plant["sameer"]["id"],
                                                   "effective_from": "2026-01-01"}]})
    assert upload(plant["device"], "417\t2036-01-01 09:00:00\t0\t1\n", secret=secret).text == "OK: 1"
    punch = rows("SELECT * FROM attendance_punches WHERE device_id = :d", d=created["id"])[0]
    assert "FUTURE_DATED" in punch["evidence_flags"] and punch["disposition"] == "PENDING"
    device = rows("SELECT watermark_at FROM attendance_devices WHERE id = :d", d=created["id"])[0]
    assert device["watermark_at"] is None
    assert rows("SELECT kind FROM attendance_review_items WHERE kind = 'DEVICE_CLOCK_SKEW'")


def test_h3_records_before_backfill_are_acknowledged_not_stored(plant):
    created, body = activated_terminal(plant)
    r = upload(plant["device"], "417\t2026-10-01 09:00:00\t0\t1\n", secret=body["connection"]["secret"])
    assert r.text == "OK: 1"
    assert rows("SELECT count(*) AS n FROM attendance_punches WHERE device_id = :d", d=created["id"])[0]["n"] == 0
    detail = plant["admin"].get(f"{DEVICES}/{created['id']}").json()
    assert detail["status"] == "ACTIVE"


def test_as_dev_07_relevant_device_behind_keeps_the_day_provisional(plant):
    from app.database.db import SessionLocal, bind_tenant
    from app.services import attendance_days

    created, body = activated_terminal(plant)
    plant["admin"].post(MAPPINGS, json={"items": [{"device_id": created["id"], "device_user_id": "417",
                                                   "employee_id": plant["sameer"]["id"],
                                                   "effective_from": "2026-11-01"}]})
    db = SessionLocal()
    try:
        bind_tenant(db, plant["cid"])
        now = IST.localize(datetime(2026, 11, 12, 10, 0)).astimezone(timezone.utc)
        inputs = attendance_days.load_inputs_many(db, plant["cid"], [plant["sameer"]["id"]], date(2026, 11, 10),
                                                  date(2026, 11, 10), now=now)
        day = inputs[(plant["sameer"]["id"], date(2026, 11, 10))]
        assert day.device_ids_behind == (created["id"],) and day.late_log_grace_elapsed is False
        later = now + timedelta(days=8)  # bounded by max_provisional_days
        inputs = attendance_days.load_inputs_many(db, plant["cid"], [plant["sameer"]["id"]], date(2026, 11, 10),
                                                  date(2026, 11, 10), now=later)
        assert inputs[(plant["sameer"]["id"], date(2026, 11, 10))].late_log_grace_elapsed is True
        sql("UPDATE attendance_devices SET watermark_at = :w WHERE id = :d", d=created["id"],
            w=datetime(2026, 11, 12, 0, 0, tzinfo=timezone.utc))
        db.rollback()
        bind_tenant(db, plant["cid"])
        inputs = attendance_days.load_inputs_many(db, plant["cid"], [plant["sameer"]["id"]], date(2026, 11, 10),
                                                  date(2026, 11, 10), now=now)
        assert inputs[(plant["sameer"]["id"], date(2026, 11, 10))].device_ids_behind == ()
    finally:
        db.close()


def test_suspended_device_gets_403_and_retire_releases_the_serial(plant):
    created, body = activated_terminal(plant)
    secret = body["connection"]["secret"]
    admin = plant["admin"]
    assert admin.post(f"{DEVICES}/{created['id']}/suspend", json={"reason": "Moved"}).status_code == 200
    r = handshake(plant["device"], secret=secret)
    assert r.status_code == 403 and r.text == "DEVICE_SUSPENDED"
    assert admin.post(f"{DEVICES}/{created['id']}/resume", json={}).json()["status"] == "ACTIVE"
    assert handshake(plant["device"], secret=secret).status_code == 200
    assert admin.post(f"{DEVICES}/{created['id']}/retire", json={"reason": "Broken"}).json()["status"] == "RETIRED"
    assert rows("SELECT active FROM device_registry_index WHERE serial_number = :s", s=SERIAL)[0]["active"] is False
    assert handshake(plant["device"], secret=secret).status_code == 401


def test_v1_tenant_gets_404_and_cannot_register_terminals(app, fake_clock):
    admin = signup_company(app, "Legacy Co")
    r = register(admin, serial="V1SERIAL")
    assert r.status_code == 422 and r.json()["code"] == "FEATURE_DISABLED"


def test_sr_as_04_command_allow_list_poll_ack_and_cancel(plant):
    created, body = activated_terminal(plant)
    secret, admin, device = body["connection"]["secret"], plant["admin"], plant["device"]
    commands = f"{DEVICES}/{created['id']}/commands"
    assert admin.post(commands, json={"command": "CLEAR_LOG"}).status_code == 422
    sync = admin.post(commands, json={"command": "SYNC_TIME"}).json()
    other = admin.post(commands, json={"command": "QUERY_USERS"}).json()
    r = admin.post(f"{commands}/{other['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    r = device.http.get("/iclock/getrequest", params={"SN": SERIAL, "key": secret})
    assert r.status_code == 200 and r.text.startswith(f"C:{sync['id']}:SET OPTIONS DateTime=2026-11-11 08:00")
    assert device.http.get("/iclock/getrequest", params={"SN": SERIAL, "key": secret}).text == "OK"
    r = device.http.post("/iclock/devicecmd", params={"SN": SERIAL, "key": secret},
                         content=f"ID={sync['id']}&Return=0&CMD=SET".encode())
    assert r.status_code == 200
    listed = {c["id"]: c["status"] for c in admin.get(commands).json()["items"]}
    assert listed == {sync["id"]: "ACKED", other["id"]: "CANCELLED"}
    assert admin.post(f"{commands}/{sync['id']}/cancel").status_code == 409


def test_agent_batches_are_hmac_signed_and_retries_are_duplicates(plant):
    admin = plant["admin"]
    r = register(admin, code="AGENT-CLINIC", serial=None, kind="AGENT", vendor="SUPREMA")
    assert r.status_code == 201, r.text
    created = r.json()
    step_up(admin)
    r = admin.post(f"{DEVICES}/{created['id']}/activate", json={"claim_code": created["claim_code"]})
    assert r.status_code == 200, r.text
    connection = r.json()["connection"]
    stored = rows("SELECT secret_enc, secret_key_id FROM attendance_devices WHERE id = :d", d=created["id"])[0]
    assert stored["secret_enc"] and connection["secret"].encode() not in bytes(stored["secret_enc"])
    admin.post(MAPPINGS, json={"items": [{"device_id": created["id"], "device_user_id": "S-417",
                                          "employee_id": plant["sameer"]["id"], "effective_from": "2026-11-01"}]})
    batch = {"ingest_batch_id": "b-1", "records": [
        {"device_user_id": "S-417", "device_local_ts": "2026-11-11T07:58:00", "direction_raw": 0, "verify_raw": 15},
        {"device_user_id": "S-417", "device_local_ts": "2026-11-11T16:30:00+05:30", "direction_raw": 1},
        {"device_user_id": "", "device_local_ts": "2026-11-11T08:00:00"}]}
    raw = json.dumps(batch).encode()
    plant["clock"].at_ist("2026-11-11", "17:00")  # after the OUT scan (a later one would be FUTURE_DATED)
    step_up(admin)
    good = hmac.new(connection["secret"].encode(), raw, hashlib.sha256).hexdigest()
    url = "/api/v1/attendance/ingest/punches"
    r = plant["device"].http.post(url, content=raw, headers={"X-Device-Id": connection["agent_id"],
                                                             "X-Attendance-Signature": "00" * 32})
    assert r.status_code == 401
    r = plant["device"].http.post(url, content=raw, headers={"X-Device-Id": connection["agent_id"],
                                                             "X-Attendance-Signature": good})
    assert r.status_code == 200, r.text
    result = r.json()
    assert (result["inserted"], result["rejected"]) == (2, 1)
    assert [x["result"] for x in result["records"]] == ["INSERTED", "INSERTED", "REJECTED"]
    again = plant["device"].http.post(url, content=raw, headers={"X-Device-Id": connection["agent_id"],
                                                                 "X-Attendance-Signature": good}).json()
    assert (again["inserted"], again["duplicates"]) == (0, 2)
    out = rows("SELECT occurred_at FROM attendance_punches WHERE device_id = :d AND direction_raw = '1'",
               d=created["id"])[0]
    assert out["occurred_at"] == datetime(2026, 11, 11, 11, 0, tzinfo=timezone.utc)  # J-2 explicit offset
    sessions = rows("SELECT check_in_time, check_out_time FROM attendance WHERE employee_id = :e "
                    "AND capture_source = 'DEVICE' AND voided_at IS NULL", e=plant["sameer"]["id"])
    assert len(sessions) == 1 and sessions[0]["check_out_time"] is not None, (
        sessions, rows("SELECT occurred_at, direction, disposition, shift_date, session_id FROM attendance_punches "
                       "WHERE device_id = :d ORDER BY id", d=created["id"]))


def test_as_dev_10_csv_import_dry_run_commit_and_idempotent_recommit(plant):
    admin = plant["admin"]
    code = plant["sameer"]["employee_code"]
    csv_text = ("employee_code,device_user_id,date,time,direction,timezone,note\n"
                f"{code},,2026-11-10,09:02,IN,,\n"
                f"{code},,2026-11-10,18:01,OUT,,\n"
                ",7777,2026-11-10,09:02,IN,,\n"
                ",7777,2026-11-10,09:02,OUT,,\n"
                f"{code},,2026-11-10,25:00,IN,,\n"
                f"{code},,2026-11-10,09:02,IN,,\n")
    files = {"file": ("punches.csv", io.BytesIO(csv_text.encode()), "text/csv")}
    r = admin.post("/api/v1/attendance/imports", files=files, data={"dry_run": "true"})
    assert r.status_code == 201, r.text
    dry = r.json()
    assert dry["status"] == "DRY_RUN" and dry["totals"]["rows"] == 6 and dry["totals"]["invalid"] == 1
    issues = {row["row"]: row["issues"] for row in dry["rows"]}
    assert "UNMAPPED" in issues[4] and "UNMAPPED" in issues[5] and "INVALID_TIME" in issues[6]
    assert "DUPLICATE_IN_FILE" in issues[7] and not issues[2]
    r = admin.post(f"/api/v1/attendance/imports/{dry['id']}/commit")
    assert r.status_code == 200 and r.json()["totals"]["inserted"] == 4, r.text
    stored = rows("SELECT employee_id, mapping_status, direction, disposition FROM attendance_punches "
                  "WHERE source = 'IMPORT' ORDER BY id")
    assert [s["mapping_status"] for s in stored] == ["MAPPED", "MAPPED", "UNMAPPED", "UNMAPPED"]
    assert [s["disposition"] for s in stored[:2]] == ["USED", "USED"]
    files = {"file": ("punches.csv", io.BytesIO(csv_text.encode()), "text/csv")}
    again = admin.post("/api/v1/attendance/imports", files=files, data={"dry_run": "false"}).json()
    assert again["totals"]["inserted"] == 0
    assert len(rows("SELECT id FROM attendance_punches WHERE source = 'IMPORT'")) == 4


def test_auto_match_by_employee_code_and_tenant_isolation(plant, app):
    admin = plant["admin"]
    created, _ = activated_terminal(plant)
    preview = admin.post(f"{MAPPINGS}/auto-match", json={"device_id": None, "dry_run": True}).json()
    assert preview["would_create"] == 2
    done = admin.post(f"{MAPPINGS}/auto-match", json={"device_id": None}).json()
    assert done["created"] == 2 and {i["device_user_id"] for i in done["items"]} == {"417", "418"}
    again = admin.post(f"{MAPPINGS}/auto-match", json={"device_id": None}).json()
    assert again["dry_run"] is True and again["would_create"] == 0
    other = signup_company(app, "Spy Co")
    enable_v2(other.company_id)
    assert other.get(f"{DEVICES}/{created['id']}").status_code == 404
    assert other.get(MAPPINGS).json()["total"] == 0
    assert other.get(f"{DEVICES}/{created['id']}/commands").status_code == 404

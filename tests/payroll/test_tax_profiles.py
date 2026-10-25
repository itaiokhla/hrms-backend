"""Employee tax profiles: the pack-defined form, versioned effective-dated declarations, proofs, HR review with
maker-checker and step-up, identifiers kept encrypted and masked, the read-only flow into the payroll snapshot,
scope and three-tenant isolation (3.2 §6.1, §7.2-§7.3; EC-STA-09, EC-STA-21; 3.9 §4 Tax & statutory).

The IN fixture pack declares illustrative fields ("FIXTURE — not law") and is usable in the test environment, so
Acme's Indian employees (Emma, Ivan, Pia) get the form; Gus (AE01, a draft pack without fields) gets "not available
for your country yet". Nothing here claims a tax rule: the values are declarations the pack may read.
"""

import hashlib
from datetime import date, timedelta

import pytest

from tests.helpers import create_employee
from tests.payroll.test_runs_lifecycle import runs_world  # noqa: F401  (fixture used by snap_world)
from tests.payroll.test_snapshot_builder import snap_world  # noqa: F401  (fixture)
from tests.payroll.test_wage_inputs import (  # noqa: F401  (world is a fixture)
    CHECK,
    DENIED,
    expect_error,
    role,
    session,
    sql,
    tenant,
    user_with_role,
    world,
)

ME = "/api/v1/me/payroll/tax-profile"
STAFF = "/api/v1/payroll/tax-profiles"
PDF = b"%PDF-1.4\n% proof of previous employer income\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
INCOME, TAX, REGIME, TAX_ID = "previous_employment.income", "previous_employment.tax", "regime", "tax_id"
VALUES = {REGIME: "NEW", INCOME: "250000.00", TAX: "12000.00"}
SECRET_ID = "ABCDE1234F"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def user_id(client):
    if getattr(client, "user_id", None) is None:
        client.user_id = client.me()["user"]["id"]
    return client.user_id


def step_up(client):
    """Mark the client's live sessions as stepped up now (the emailed-code flow itself is covered by test_step_up.py)."""
    sql("UPDATE user_sessions SET step_up_at = now(), step_up_method = 'EMAIL_OTP' "
        "WHERE user_id = :u AND revoked_at IS NULL", u=user_id(client))


def submit(client, values=None, effective="2026-04-01", expect=200, **extra):
    body = {"values": dict(VALUES if values is None else values), **extra}
    if effective is not None:
        body["effective_from"] = effective
    r = client.put(ME, json=body)
    assert r.status_code == expect, r.text
    return r.json()


def upload(client, version_id, field=INCOME, data=PDF, name="statement.pdf", expect=201, mime="application/pdf"):
    r = client.post(f"{ME}/versions/{version_id}/proofs", data={"field_code": field},
                    files={"file": (name, data, mime)})
    assert r.status_code == expect, r.text
    return r.json()


def approve(client, profile_id, expect=200, stepped=True, **body):
    if stepped:
        step_up(client)
    r = client.post(f"{STAFF}/{profile_id}/approve", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def reject(client, profile_id, note="Proof is not readable", expect=200, **body):
    r = client.post(f"{STAFF}/{profile_id}/reject", json={"note": note, **body})
    assert r.status_code == expect, r.text
    return r.json()


def approved_version(w, who="emma", reviewer="pia", values=None, effective="2026-04-01", proof=True):
    """Submit as `who`, attach the proof the IN fixture wants, approve as `reviewer`; returns the version id."""
    version = submit(w["u"][who], values, effective)
    if proof and INCOME in (VALUES if values is None else values):
        upload(w["u"][who], version["id"])
    approve(w["u"][reviewer], version["id"])
    return version["id"]


def row(version_id):
    return sql("SELECT row_to_json(p) FROM employee_tax_profiles p WHERE id = :i", i=version_id)[0][0]


def audit_rows(company_id, entity_id=None):
    query = "SELECT action, details::text FROM audit_logs WHERE company_id = :c AND module = 'Tax Profiles'"
    params = {"c": company_id}
    if entity_id is not None:
        query += " AND entity_id = :e"
        params["e"] = str(entity_id)
    return sql(query + " ORDER BY id", **params)


def notifications_of(client):
    return [t for (t,) in sql("SELECT title FROM notifications WHERE user_id = :u ORDER BY id", u=user_id(client))]


# ---------------------------------------------------------------------------
# The pack-defined form
# ---------------------------------------------------------------------------
def test_the_form_comes_from_the_pack_and_a_country_without_fields_says_so(world):
    w = world
    r = w["u"]["emma"].get(ME)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True and body["country"] == "IN" and body["currency"] == "INR"
    assert [f["code"] for f in body["schema"]] == [REGIME, INCOME, TAX, TAX_ID]
    assert body["pack"]["is_fixture"] is True and "FIXTURE" in body["pack"]["label"]
    assert all("FIXTURE — not law" in f["note"] for f in body["schema"])
    assert next(f for f in body["schema"] if f["code"] == TAX_ID)["sensitive"] is True
    assert body["current"] is None and body["pending"] is None and body["versions"] == [] and body["can_edit"] is True
    gus = w["u"]["gus"].get(ME).json()
    assert gus["available"] is False and gus["reason"] == "NO_PACK_FIELDS" and gus["schema"] == []
    assert gus["message"] == "Tax declarations are not available for your country yet." and gus["country"] == "AE"
    submit(w["u"]["gus"], {REGIME: "NEW"}, expect=409)
    assert w["u"]["gus"].put(ME, json={"values": {}}).json()["code"] == "TAX_PROFILE_NOT_AVAILABLE"


def test_nobody_reaches_the_form_or_the_review_unauthenticated(world, app):
    from tests.helpers import ApiClient

    assert ApiClient(app).get(ME).status_code == 401
    assert ApiClient(app).put(ME, json={"values": {}}).status_code in (401, 403)
    assert ApiClient(app).get(STAFF).status_code == 401


# ---------------------------------------------------------------------------
# Submitting
# ---------------------------------------------------------------------------
def test_submit_creates_a_numbered_pending_version_and_audits_field_codes_not_values(world):
    w = world
    version = submit(w["u"]["emma"])
    assert (version["version_no"], version["status"], version["row_version"]) == (1, "SUBMITTED", 1)
    detail = w["u"]["emma"].get(f"{ME}/versions/{version['id']}").json()
    assert detail["values"] == VALUES and detail["effective_from"] == "2026-04-01" and detail["identifiers"] == {}
    assert detail["proofs_missing"] == [INCOME] and detail["schema_current"] is True
    context = w["u"]["emma"].get(ME).json()
    assert context["pending"]["id"] == version["id"] and context["current"] is None and context["latest"]["id"] == version["id"]
    record = row(version["id"])
    assert record["pack_code"] == "IN" and record["country"] == "IN" and len(record["schema_hash"]) == 64
    assert record["submitted_by_user_id"] == user_id(w["u"]["emma"]) and record["reviewed_by_user_id"] is None
    trail = audit_rows(w["cid"], version["id"])
    assert [a for a, _ in trail] == ["TAX_PROFILE_SUBMITTED"]
    assert "250000" not in trail[0][1] and "12000" not in trail[0][1] and INCOME in trail[0][1]
    assert "Tax declarations to review" in notifications_of(w["u"]["pia"])        # reviewers are told
    assert "Tax declarations to review" not in notifications_of(w["u"]["emma"])    # the submitter is not


@pytest.mark.parametrize("values, field, code", [
    ({REGIME: "MIXED"}, REGIME, "INVALID_CHOICE"),
    ({INCOME: 250000}, INCOME, "NUMBER_AS_TEXT"),                    # a JSON number in a money field
    ({INCOME: "250000.001"}, INCOME, "TOO_MANY_DECIMALS"),
    ({INCOME: "-5.00"}, INCOME, "BELOW_MINIMUM"),
    ({TAX: "1e3"}, TAX, "INVALID_NUMBER"),
    ({"bogus": "x"}, "bogus", "UNKNOWN_FIELD"),
    ({TAX_ID: "bad#id"}, TAX_ID, "INVALID_IDENTIFIER"),
])
def test_invalid_declarations_are_422_with_a_message_per_field(world, values, field, code):
    w = world
    step_up(w["u"]["emma"])
    r = w["u"]["emma"].put(ME, json={"values": values, "effective_from": "2026-04-01"})
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "TAX_PROFILE_INVALID"
    assert any(i["field"] == field and i["code"] == code for i in r.json()["detail"]), r.json()
    assert sql("SELECT count(*) FROM employee_tax_profiles WHERE company_id = :c", c=w["cid"])[0][0] == 0


def test_request_shape_is_strict_and_the_effective_date_is_bounded(world):
    w = world
    emma = w["u"]["emma"]
    for body in ({"values": VALUES, "status": "APPROVED"}, {"values": VALUES, "employee_id": w["emp"]["Ivan"]},
                 {"values": VALUES, "company_id": 999}, {"values": {REGIME: 1.5}}, {"values": {REGIME: ["NEW"]}}):
        assert emma.put(ME, json=body).status_code == 422, body
    assert emma.put(ME, json={"values": VALUES, "effective_from": "1999-12-31"}).json()["code"] == "EFFECTIVE_DATE_INVALID"
    far = (w["today"] + timedelta(days=400)).isoformat()
    assert emma.put(ME, json={"values": VALUES, "effective_from": far}).json()["code"] == "EFFECTIVE_DATE_INVALID"
    assert sql("SELECT count(*) FROM employee_tax_profiles WHERE company_id = :c", c=w["cid"])[0][0] == 0
    # No effective date means today.
    assert w["u"]["emma"].get(f"{ME}/versions/{submit(emma, effective=None)['id']}").json()["effective_from"] == \
        w["today"].isoformat()


def test_one_pending_version_at_a_time_withdraw_and_resubmit(world):
    w = world
    emma = w["u"]["emma"]
    first = submit(emma)
    assert emma.put(ME, json={"values": VALUES}).json()["code"] == "PENDING_EXISTS"
    # Only the owner withdraws; a peer's id is 404 and a stale row_version is a conflict.
    assert w["u"]["gus"].post(f"{ME}/versions/{first['id']}/withdraw").status_code == 404
    assert emma.post(f"{ME}/versions/{first['id']}/withdraw", json={"row_version": 7}).json()["code"] == \
        "ROW_VERSION_CONFLICT"
    done = emma.post(f"{ME}/versions/{first['id']}/withdraw", json={"row_version": 1}).json()
    assert done["status"] == "WITHDRAWN" and done["row_version"] == 2
    assert emma.post(f"{ME}/versions/{first['id']}/withdraw").json()["code"] == "NOT_SUBMITTED"
    second = submit(emma, {REGIME: "NEW"})
    assert second["version_no"] == 2
    versions = emma.get(ME).json()["versions"]
    assert [(v["version_no"], v["status"]) for v in versions] == [(2, "SUBMITTED"), (1, "WITHDRAWN")]
    assert [a for a, _ in audit_rows(w["cid"], first["id"])] == ["TAX_PROFILE_SUBMITTED", "TAX_PROFILE_WITHDRAWN"]
    # A withdrawn version can no longer be approved.
    assert w["u"]["pia"].post(f"{STAFF}/{first['id']}/approve", json={}).status_code in (403, 409)
    step_up(w["u"]["pia"])
    assert w["u"]["pia"].post(f"{STAFF}/{first['id']}/approve", json={}).json()["code"] == "NOT_SUBMITTED"


def test_idempotency_key_replays_a_submission_once(world):
    w = world
    headers = {"Idempotency-Key": "tax-profile-1"}
    a = w["u"]["emma"].put(ME, json={"values": {REGIME: "NEW"}, "effective_from": "2026-04-01"}, headers=headers)
    b = w["u"]["emma"].put(ME, json={"values": {REGIME: "NEW"}, "effective_from": "2026-04-01"}, headers=headers)
    assert a.status_code == b.status_code == 200 and a.json() == b.json()
    assert sql("SELECT count(*) FROM employee_tax_profiles WHERE company_id = :c", c=w["cid"])[0][0] == 1


# ---------------------------------------------------------------------------
# Identifiers: step-up, encryption, masking, carry-over
# ---------------------------------------------------------------------------
def test_a_tax_identifier_needs_step_up_is_sealed_masked_and_never_in_the_snapshot_or_audit(world):
    w = world
    emma = w["u"]["emma"]
    values = {**VALUES, TAX_ID: SECRET_ID}
    r = emma.put(ME, json={"values": values, "effective_from": "2026-04-01"})
    assert r.status_code == 403 and r.json()["code"] == "STEP_UP_REQUIRED"
    assert sql("SELECT count(*) FROM employee_tax_profiles WHERE company_id = :c", c=w["cid"])[0][0] == 0
    step_up(emma)
    version = submit(emma, values)
    record = row(version["id"])
    assert SECRET_ID not in str(record), "the identifier must only be stored sealed"
    assert TAX_ID not in record["declared_values"] and record["identifiers_enc"][TAX_ID] != SECRET_ID
    assert record["identifiers_masked"][TAX_ID].startswith("••••") and SECRET_ID not in record["identifiers_masked"][TAX_ID]
    shown = emma.get(f"{ME}/versions/{version['id']}").json()
    assert shown["identifiers"] == record["identifiers_masked"] and SECRET_ID not in str(shown)
    upload(emma, version["id"])
    approve(w["u"]["pia"], version["id"])
    staff = w["u"]["pia"].get(f"{STAFF}/{version['id']}").json()
    assert SECRET_ID not in str(staff) and staff["identifiers"][TAX_ID].startswith("••••")
    assert SECRET_ID not in str(audit_rows(w["cid"])) and "identifiers_changed" in str(audit_rows(w["cid"]))
    # Round trip: the data key opens what the service sealed.
    from app.payroll.services import keys

    with session(w["cid"]) as (db, _):
        opened = keys.decrypt_field(db, w["cid"], record["identifiers_enc"][TAX_ID],
                                    aad=f"tax_profile.identifier:{w['emp']['Emma']}:{TAX_ID}".encode())
    assert opened == SECRET_ID
    # The next version carries the identifier over without asking again (and so needs no step-up).
    sql("UPDATE user_sessions SET step_up_at = NULL WHERE user_id = :u", u=user_id(emma))
    nxt = submit(emma, {REGIME: "NEW"}, effective="2026-07-01")
    carried = row(nxt["id"])
    assert carried["identifiers_enc"] == record["identifiers_enc"] and carried["identifiers_masked"] == record["identifiers_masked"]


# ---------------------------------------------------------------------------
# Proofs
# ---------------------------------------------------------------------------
def test_proofs_are_typed_deduplicated_private_and_audited(world):
    w = world
    emma, gus, pia = w["u"]["emma"], w["u"]["gus"], w["u"]["pia"]
    version = submit(emma)
    proof = upload(emma, version["id"])
    assert proof["sha256"] == hashlib.sha256(PDF).hexdigest() and proof["mime"] == "application/pdf"
    assert upload(emma, version["id"], expect=409)["code"] == "PROOF_ALREADY_ATTACHED"
    assert upload(emma, version["id"], data=b"MZ not a document", name="x.exe", expect=400)["code"] == "PROOF_TYPE"
    assert upload(emma, version["id"], data=b"", expect=400)["code"] == "PROOF_EMPTY"
    assert upload(emma, version["id"], field=TAX_ID, expect=400)["code"] == "PROOF_FIELD_INVALID"
    assert upload(emma, version["id"], field="nope", expect=400)["code"] == "PROOF_FIELD_INVALID"
    assert upload(emma, version["id"], field=REGIME, data=PNG, mime="image/png")["mime"] == "image/png"
    assert upload(emma, version["id"], field=TAX)["field_code"] == TAX    # any declared non-identifier field
    # Downloads: the owner and a reviewer in scope; a peer and another tenant get 404; both are audited.
    own = emma.get(f"{ME}/versions/{version['id']}/proofs/{proof['id']}")
    assert own.status_code == 200 and own.content == PDF and own.headers["cache-control"] == "private, no-store"
    assert own.headers["x-content-type-options"] == "nosniff"
    assert gus.get(f"{ME}/versions/{version['id']}/proofs/{proof['id']}").status_code == 404
    assert w["u"]["beta"].get(f"{STAFF}/{version['id']}/proofs/{proof['id']}").status_code in (403, 404)
    staff = pia.get(f"{STAFF}/{version['id']}/proofs/{proof['id']}")
    assert staff.status_code == 200 and staff.content == PDF
    assert w["u"]["emma"].get(f"{STAFF}/{version['id']}/proofs/{proof['id']}").status_code == 403
    actions = [a for a, _ in audit_rows(w["cid"], version["id"])]
    assert actions.count("TAX_PROOF_ADDED") == 3 and actions.count("TAX_PROOF_DOWNLOADED") == 2
    # Proofs are frozen with the version: after review nothing can be added.
    approve(pia, version["id"])
    assert upload(emma, version["id"], field=REGIME, data=PDF + b"x", expect=409)["code"] == "NOT_SUBMITTED"


def test_a_proof_needs_a_declared_value_for_its_field(world):
    w = world
    version = submit(w["u"]["emma"], {REGIME: "NEW"})
    assert upload(w["u"]["emma"], version["id"], field=INCOME, expect=400)["code"] == "PROOF_WITHOUT_VALUE"


# ---------------------------------------------------------------------------
# HR review
# ---------------------------------------------------------------------------
def test_review_needs_a_step_up_the_required_proof_and_a_second_person(world):
    w = world
    emma, pia, founder = w["u"]["emma"], w["u"]["pia"], w["u"]["founder"]
    version = submit(emma)
    r = pia.post(f"{STAFF}/{version['id']}/approve", json={})
    assert r.status_code == 403 and r.json()["code"] == "STEP_UP_REQUIRED"
    missing = approve(pia, version["id"], expect=409)
    assert missing["code"] == "PROOF_MISSING" and "Income from a previous employer" in missing["detail"]
    upload(emma, version["id"])
    assert approve(pia, version["id"], row_version=9, expect=409)["code"] == "ROW_VERSION_CONFLICT"
    done = approve(pia, version["id"], note="Checked against the proof", row_version=1)
    assert done["status"] == "APPROVED" and done["row_version"] == 2
    record = row(version["id"])
    assert record["reviewed_by_user_id"] == user_id(pia) and record["reviewed_at"] and record["review_note"]
    assert approve(pia, version["id"], expect=409)["code"] == "NOT_SUBMITTED"
    shown = emma.get(ME).json()
    assert shown["current"]["id"] == version["id"] and shown["current"]["status"] == "APPROVED"
    assert shown["pending"] is None and shown["current"]["review_note"] == "Checked against the proof"
    assert "Tax declarations approved" in notifications_of(emma)
    assert [a for a, _ in audit_rows(w["cid"], version["id"])] == [
        "TAX_PROFILE_SUBMITTED", "TAX_PROOF_ADDED", "TAX_PROFILE_APPROVED"]
    # Maker-checker: Pia submits her own declarations; she can never review them, the founder can.
    own = submit(pia)
    upload(pia, own["id"])
    step_up(pia)
    r = pia.post(f"{STAFF}/{own['id']}/approve", json={})
    assert r.status_code == 403 and r.json()["code"] == "SELF_REVIEW"
    assert pia.post(f"{STAFF}/{own['id']}/reject", json={"note": "no"}).json()["code"] == "SELF_REVIEW"
    assert approve(founder, own["id"])["status"] == "APPROVED"
    assert emma.post(f"{STAFF}/{own['id']}/approve", json={}).status_code == 403   # no review permission


def test_rejection_needs_a_note_and_the_employee_can_submit_again(world):
    w = world
    emma, pia = w["u"]["emma"], w["u"]["pia"]
    version = submit(emma)
    assert pia.post(f"{STAFF}/{version['id']}/reject", json={"note": "   "}).status_code in (422,)
    assert pia.post(f"{STAFF}/{version['id']}/reject", json={}).status_code == 422
    assert pia.post(f"{STAFF}/{version['id']}/reject", json={"note": "x" * 2001}).status_code == 422
    done = reject(pia, version["id"], note="The proof does not show the employer")
    assert done["status"] == "REJECTED"
    shown = emma.get(ME).json()
    assert shown["pending"] is None and shown["current"] is None
    assert shown["latest"]["status"] == "REJECTED" and shown["latest"]["review_note"] == "The proof does not show the employer"
    assert "Tax declarations need changes" in notifications_of(emma)
    assert reject(pia, version["id"], expect=409)["code"] == "NOT_SUBMITTED"
    assert approve(pia, version["id"], expect=409)["code"] == "NOT_SUBMITTED"
    again = submit(emma)
    assert again["version_no"] == 2
    assert [a for a, _ in audit_rows(w["cid"], version["id"])][-1] == "TAX_PROFILE_REJECTED"


def test_staff_list_filters_counts_and_masks(world):
    w = world
    emma, pia = w["u"]["emma"], w["u"]["pia"]
    step_up(emma)
    first = submit(emma, {**VALUES, TAX_ID: SECRET_ID})
    upload(emma, first["id"])
    approve(pia, first["id"])
    second = submit(emma, {REGIME: "NEW"}, effective="2026-07-01")
    listed = pia.get(STAFF).json()
    assert listed["total"] == 2 and listed["counts"] == {"APPROVED": 1, "SUBMITTED": 1}
    assert [i["id"] for i in listed["items"]] == [second["id"], first["id"]]                 # pending first
    item = listed["items"][0]
    assert item["employee"]["name"] and item["employee_id"] == w["emp"]["Emma"] and item["is_own"] is False
    assert [f["code"] for f in item["schema"]] == [REGIME, INCOME, TAX, TAX_ID]
    assert SECRET_ID not in str(listed)
    assert pia.get(STAFF, params={"status": "APPROVED"}).json()["total"] == 1
    assert pia.get(STAFF, params={"employee_id": w["emp"]["Ivan"]}).json()["total"] == 0
    assert pia.get(STAFF, params={"status": "BOGUS"}).status_code == 422
    assert pia.get(STAFF, params={"legal_entity_id": w["le01"]["id"]}).json()["total"] == 2
    assert pia.get(STAFF, params={"legal_entity_id": w["ae"]["id"]}).json()["total"] == 0


def test_review_scope_is_the_legal_entity_and_other_roles_are_refused(world, app):
    w = world
    version = submit(w["u"]["emma"])
    upload(w["u"]["emma"], version["id"])
    # A reviewer of AE01 only does not reach Emma's LE01 declarations; one of LE01 does.
    reviewer_role = role(w["u"]["founder"], "Entity tax reviewer", "LEGAL_ENTITY", ("payroll:tax_profile:review",))
    ae_reviewer = user_with_role(app, w["u"]["founder"], create_employee(w["u"]["founder"], "Rae", hire_date="2024-01-15"),
                                 reviewer_role, [w["ae"]["id"]])
    le_reviewer = user_with_role(app, w["u"]["founder"], create_employee(w["u"]["founder"], "Lia", hire_date="2024-01-15"),
                                 reviewer_role, [w["le01"]["id"]])
    assert ae_reviewer.get(STAFF).json() == {"total": 0, "counts": {}, "items": []}
    assert ae_reviewer.get(f"{STAFF}/{version['id']}").status_code == 404
    step_up(ae_reviewer)
    assert ae_reviewer.post(f"{STAFF}/{version['id']}/approve", json={}).status_code == 404
    assert ae_reviewer.post(f"{STAFF}/{version['id']}/reject", json={"note": "no"}).status_code == 404
    assert ae_reviewer.get(f"{STAFF}/{version['id']}/proofs/1").status_code == 404
    assert le_reviewer.get(f"{STAFF}/{version['id']}").json()["status"] == "SUBMITTED"
    assert approve(le_reviewer, version["id"])["status"] == "APPROVED"
    # Roles without the review permission: employee, clerk, anonymous payroll roles.
    for who in ("emma", "gus", "cleo"):
        assert w["u"][who].get(STAFF).status_code == 403, who
        assert w["u"][who].get(f"{STAFF}/{version['id']}").status_code == 403, who
        assert w["u"][who].post(f"{STAFF}/{version['id']}/reject", json={"note": "x"}).status_code == 403, who


def test_the_employee_edit_permission_is_required_to_submit(world):
    w = world
    sql("DELETE FROM role_permissions WHERE company_id = :c AND permission_code = 'payroll:tax_profile:edit'", c=w["cid"])
    sql("UPDATE users SET permission_version = permission_version + 1 WHERE company_id = :c", c=w["cid"])
    r = w["u"]["emma"].put(ME, json={"values": VALUES})
    assert r.status_code == 403
    assert w["u"]["emma"].get(ME).status_code == 200      # reading stays possible


# ---------------------------------------------------------------------------
# Tenants and the database
# ---------------------------------------------------------------------------
def test_three_tenants_never_see_each_other(world):
    w = world
    version = submit(w["u"]["emma"])
    proof = upload(w["u"]["emma"], version["id"])
    for other in ("beta", "cobalt"):
        client = w["u"][other]
        assert client.get(STAFF).json() == {"total": 0, "counts": {}, "items": []}
        assert client.get(f"{STAFF}/{version['id']}").status_code == 404
        step_up(client)
        assert client.post(f"{STAFF}/{version['id']}/approve", json={}).status_code == 404
        assert client.post(f"{STAFF}/{version['id']}/reject", json={"note": "x"}).status_code == 404
        assert client.get(f"{STAFF}/{version['id']}/proofs/{proof['id']}").status_code == 404
        assert client.get(f"{ME}/versions/{version['id']}").status_code in (403, 404)
    beta_cid, cobalt_cid = w["u"]["beta"].company_id, w["u"]["cobalt"].company_id
    for cid in (beta_cid, cobalt_cid, None):
        with tenant(cid) as conn:
            for table in ("employee_tax_profiles", "employee_tax_proofs"):
                assert conn.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar() == 0
    with tenant(w["cid"]) as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM employee_tax_profiles").scalar() == 1
        assert conn.exec_driver_sql("SELECT count(*) FROM employee_tax_proofs").scalar() == 1
    with tenant(beta_cid) as conn:   # a row for another company is refused by row-level security
        expect_error(conn, DENIED, "INSERT INTO employee_tax_profiles (company_id, employee_id, legal_entity_id, country, "
                     "pack_code, schema_hash, version_no, effective_from, status, submitted_by_user_id, submitted_at, "
                     "created_at, updated_at) VALUES (:c, :e, :le, 'IN', 'IN', :h, 9, '2026-04-01', 'SUBMITTED', :u, now(), "
                     "now(), now())", c=w["cid"], e=w["emp"]["Emma"], le=w["le01"]["id"], h="0" * 64, u=user_id(w["u"]["emma"]))


def test_the_database_keeps_the_review_rules_itself(world):
    w = world
    version = submit(w["u"]["emma"])
    proof = upload(w["u"]["emma"], version["id"])
    emma_user, pia_user = user_id(w["u"]["emma"]), user_id(w["u"]["pia"])
    cid, vid = w["cid"], version["id"]
    with tenant(cid) as conn:
        # maker-checker, review bookkeeping and the rejection note are CHECK constraints
        expect_error(conn, CHECK, "UPDATE employee_tax_profiles SET status = 'APPROVED', reviewed_by_user_id = :u, "
                     "reviewed_at = now() WHERE id = :i", u=emma_user, i=vid)
        expect_error(conn, CHECK, "UPDATE employee_tax_profiles SET status = 'APPROVED' WHERE id = :i", i=vid)
        expect_error(conn, CHECK, "UPDATE employee_tax_profiles SET status = 'REJECTED', reviewed_by_user_id = :u, "
                     "reviewed_at = now() WHERE id = :i", u=pia_user, i=vid)
        expect_error(conn, CHECK, "UPDATE employee_tax_profiles SET status = 'WITHDRAWN' WHERE id = :i", i=vid)
        expect_error(conn, CHECK, "UPDATE employee_tax_profiles SET status = 'DONE' WHERE id = :i", i=vid)
        # a submitted version's declarations cannot be edited, and nothing is ever deleted
        expect_error(conn, CHECK, "UPDATE employee_tax_profiles SET declared_values = '{}'::jsonb WHERE id = :i", i=vid)
        expect_error(conn, CHECK, "UPDATE employee_tax_profiles SET effective_from = '2020-01-01' WHERE id = :i", i=vid)
        expect_error(conn, CHECK, "DELETE FROM employee_tax_profiles WHERE id = :i", i=vid)
        # proofs are never changed or deleted
        expect_error(conn, CHECK, "UPDATE employee_tax_proofs SET file_name = 'x.pdf' WHERE id = :i", i=proof["id"])
        expect_error(conn, CHECK, "DELETE FROM employee_tax_proofs WHERE id = :i", i=proof["id"])
        # one pending version per employee
        expect_error(conn, "23505", "INSERT INTO employee_tax_profiles (company_id, employee_id, legal_entity_id, country, "
                     "pack_code, schema_hash, version_no, effective_from, status, submitted_by_user_id, submitted_at, "
                     "created_at, updated_at) VALUES (:c, :e, :le, 'IN', 'IN', :h, 9, '2026-04-01', 'SUBMITTED', :u, now(), "
                     "now(), now())", c=cid, e=w["emp"]["Emma"], le=w["le01"]["id"], h="0" * 64, u=emma_user)
        # a legal entity of another company is a foreign-key violation (composite same-company keys)
        expect_error(conn, "23503", "INSERT INTO employee_tax_profiles (company_id, employee_id, legal_entity_id, country, "
                     "pack_code, schema_hash, version_no, effective_from, status, submitted_by_user_id, submitted_at, "
                     "withdrawn_at, created_at, updated_at) VALUES (:c, :e, 999999, 'IN', 'IN', :h, 9, '2026-04-01', "
                     "'WITHDRAWN', :u, now(), now(), now(), now())", c=cid, e=w["emp"]["Emma"], h="0" * 64, u=emma_user)
    upload(w["u"]["emma"], vid, field=REGIME, data=PDF + b"1")
    approve(w["u"]["pia"], vid)
    with tenant(cid) as conn:
        # after the review the version is frozen completely, and proofs can no longer be added by any path
        expect_error(conn, CHECK, "UPDATE employee_tax_profiles SET review_note = 'edited' WHERE id = :i", i=vid)
        expect_error(conn, CHECK, "UPDATE employee_tax_profiles SET row_version = row_version + 1 WHERE id = :i", i=vid)
        expect_error(conn, CHECK, "DELETE FROM employee_tax_profiles WHERE id = :i", i=vid)
        expect_error(conn, CHECK, "INSERT INTO employee_tax_proofs (company_id, profile_id, employee_id, field_code, "
                     "file_name, storage_path, sha256, mime, bytes, uploaded_by_user_id, created_at, updated_at) VALUES "
                     "(:c, :p, :e, 'regime', 'f.pdf', 'x', :h, 'application/pdf', 4, :u, now(), now())",
                     c=cid, p=vid, e=w["emp"]["Emma"], h="a" * 64, u=emma_user)


# ---------------------------------------------------------------------------
# Effective dating and the read-only flow into the snapshot
# ---------------------------------------------------------------------------
def frozen(w, employee, on):
    from app.payroll import packs
    from app.payroll.services import tax_profiles

    pack = packs.find_pack("IN", environment="test")
    with session(w["cid"]) as (db, _):
        return tax_profiles.frozen_profile(db, w["cid"], w["emp"][employee], packs.pack_snapshot(pack), on)


def test_approved_versions_are_effective_dated_and_flow_read_only_without_identifiers(world):
    w = world
    emma = w["u"]["emma"]
    assert frozen(w, "Emma", date(2026, 10, 31)) == (None, None, [])            # nothing approved: profile None
    step_up(emma)
    first = approved_version(w, values={**VALUES, TAX_ID: SECRET_ID}, effective="2026-04-01")
    content, version_id, issues = frozen(w, "Emma", date(2026, 10, 31))
    assert version_id == first and issues == []
    assert content["regime"] == "NEW"
    assert content["previous_employment"] == {"income": "250000.00", "tax": "12000.00"}   # nested as the pack reads it
    assert TAX_ID not in str(content) and SECRET_ID not in str(content)
    assert content["_meta"]["version_no"] == 1 and content["_meta"]["effective_from"] == "2026-04-01"
    assert frozen(w, "Emma", date(2026, 3, 31)) == (None, None, [])             # before it takes effect
    assert frozen(w, "Ivan", date(2026, 10, 31)) == (None, None, [])            # nobody else's profile
    # A later version supersedes from its own effective date only; a pending one never counts.
    second = approved_version(w, values={REGIME: "OLD"}, effective="2026-12-01", proof=False)
    pending = submit(emma, {REGIME: "NEW"}, effective="2026-04-01")
    assert frozen(w, "Emma", date(2026, 11, 30))[1] == first
    content, version_id, _ = frozen(w, "Emma", date(2026, 12, 31))
    assert version_id == second and content["regime"] == "OLD" and "previous_employment" not in content
    # Nothing in the flow writes: the stored rows are untouched by reading them.
    assert row(first)["status"] == "APPROVED" and row(pending["id"])["status"] == "SUBMITTED"
    # A different pack code (an employee who moved country) is not offered to this pack.
    from app.payroll import packs
    from app.payroll.services import tax_profiles

    with session(w["cid"]) as (db, _):
        assert tax_profiles.frozen_profile(db, w["cid"], w["emp"]["Emma"], {"code": "AE", "version": "x"},
                                           date(2026, 12, 31)) == (None, None, [])
        assert tax_profiles.frozen_profile(db, w["cid"], w["emp"]["Emma"], None, date(2026, 12, 31)) == (None, None, [])
        pack = packs.find_pack("US", environment="test")
        assert tax_profiles.frozen_profile(db, w["cid"], w["emp"]["Emma"], packs.pack_snapshot(pack),
                                           date(2026, 12, 31)) == (None, None, [])


def test_a_changed_declaration_form_warns_in_the_snapshot_and_blocks_approval(world, monkeypatch):
    from app.payroll import packs
    from app.payroll.packs import tax_profile as tp

    w = world
    emma, pia = w["u"]["emma"], w["u"]["pia"]
    first = approved_version(w, values={REGIME: "NEW", INCOME: "1.00", TAX: "0.00"}, effective="2026-04-01")
    pending = submit(emma, {REGIME: "NEW"}, effective="2026-06-01")
    pack = packs.find_pack("IN", environment="test")
    reduced = tuple(f for f in pack.tax_profile_schema() if f.code != TAX)
    monkeypatch.setattr(pack, "tax_profile_schema", lambda: reduced)
    content, version_id, issues = frozen(w, "Emma", date(2026, 10, 31))
    assert version_id == first and [i[0] for i in issues] == ["TAX_PROFILE_SCHEMA_CHANGED"] and issues[0][1] == "WARNING"
    assert content["previous_employment"] == {"income": "1.00"}                  # only fields still declared
    assert tp.schema_hash(reduced) != row(first)["schema_hash"]
    shown = emma.get(f"{ME}/versions/{first}").json()
    assert shown["schema_current"] is False
    assert approve(pia, pending["id"], expect=409)["code"] == "SCHEMA_CHANGED"


# ---------------------------------------------------------------------------
# Snapshot builder integration (a real run employee)
# ---------------------------------------------------------------------------
def test_snapshot_builder_freezes_the_approved_profile_and_a_later_approval_is_a_source_change(snap_world, app):
    from app.payroll import engine
    from app.payroll.engine import snapshot as snap
    from app.payroll.services import change_detection, snapshot
    from tests.payroll.test_compensation import login_as
    from tests.payroll.test_runs_lifecycle import force_status
    from tests.payroll.test_snapshot_builder import built, emma, lock, objects, session_for

    w = snap_world
    employee = login_as(app, w["acme"], w["emp"]["Emma"], ["employee"])
    processor = w["processor"]
    start = w["start"].isoformat()
    base = built(w["cid"], w["run"]["id"], emma(w))
    assert base.content["tax_profile"] is None
    assert not [k for k in base.source_hashes if k.startswith("tax_profile")]

    version = submit(employee, effective=start)
    upload(employee, version["id"])
    approve(processor, version["id"])
    with_profile = built(w["cid"], w["run"]["id"], emma(w))
    profile = with_profile.content["tax_profile"]
    assert profile["regime"] == "NEW" and profile["previous_employment"] == {"income": "250000.00", "tax": "12000.00"}
    assert profile["_meta"]["version_id"] == version["id"]
    assert f"tax_profile:{version['id']}" in with_profile.source_hashes
    assert with_profile.content_hash != base.content_hash
    parsed = snap.Snapshot.from_dict(with_profile.content)                      # strict schema accepts it
    assert parsed.tax_profile["regime"] == "NEW"
    assert engine.calculate(with_profile.content).content_hash == parsed.content_hash
    assert built(w["cid"], w["run"]["id"], emma(w)).content_hash == with_profile.content_hash   # deterministic

    # Locked inputs stay as frozen; a profile approved afterwards is an ADDED source of the stale detection.
    force_status(w["run"]["id"], "READY")
    lock(w["cid"], w["run"]["id"], [emma(w)])
    second = submit(employee, {REGIME: "OLD"}, effective=start)
    approve(processor, second["id"])
    db = session_for(w["cid"])
    try:
        run, row_ = objects(db, w["run"]["id"], emma(w))
        changes = change_detection.changed_sources(db, run, row_)
        stored = snapshot.latest_snapshot(db, row_)
    finally:
        db.close()
    assert stored.content["tax_profile"]["_meta"]["version_id"] == version["id"]
    assert {c.source for c in changes} >= {f"tax_profile:{second['id']}", f"tax_profile:{version['id']}"}


# ---------------------------------------------------------------------------
# Migration 0033: the role grants of the two new permission codes
# ---------------------------------------------------------------------------
def test_migration_0033_grants_the_new_codes_to_existing_system_roles_once_and_bumps_sessions(world):
    import importlib.util
    from pathlib import Path

    from app.core import permission_registry as reg
    from app.database.db import system_engine

    spec = importlib.util.spec_from_file_location(
        "m0033", Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0033_tax_profiles_unclaimed.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    w = world
    codes = ("payroll:tax_profile:edit", "payroll:tax_profile:review")

    def grants(company_id):
        rows = sql("SELECT r.system_key, rp.permission_code, rp.scope FROM role_permissions rp JOIN roles r ON r.id = rp.role_id "
                   "WHERE rp.company_id = :c AND rp.permission_code = ANY(:codes)", c=company_id, codes=list(codes))
        return {(k, c, s) for k, c, s in rows}

    expected = {(key, code, scope) for key, items in m.NEW_GRANTS.items() for code, scope in items}
    # What the registry gives the seeded roles is exactly what the migration adds to existing ones.
    by_role = {r.system_key: {(c, s.value) for c, s in r.grants} for r in reg.DEFAULT_ROLES}
    assert {(key, code, scope) for key, grants_ in by_role.items() for code, scope in grants_ if code in codes} == expected
    assert grants(w["cid"]) == expected
    # An existing tenant from before the migration: the grants are missing, then added once.
    sql("DELETE FROM role_permissions WHERE permission_code = ANY(:codes)", codes=list(codes))
    other_before = grants(w["u"]["beta"].company_id)
    assert other_before == set()
    versions = dict(sql("SELECT id, permission_version FROM users WHERE company_id = :c", c=w["cid"]))
    with system_engine.begin() as conn:
        added = m._grant_roles(conn)
    assert added == len(expected) * 3                                         # Acme, Beta and Cobalt each
    assert grants(w["cid"]) == expected and grants(w["u"]["beta"].company_id) == expected
    after = dict(sql("SELECT id, permission_version FROM users WHERE company_id = :c", c=w["cid"]))
    assert all(after[u] >= versions[u] for u in versions) and any(after[u] > versions[u] for u in versions)
    with system_engine.begin() as conn:
        assert m._grant_roles(conn) == 0                                       # idempotent
    # A custom role is never touched.
    assert not [r for r in sql("SELECT 1 FROM roles r JOIN role_permissions rp ON rp.role_id = r.id WHERE "
                               "r.is_system_role = false AND rp.permission_code = ANY(:codes)", codes=list(codes))]

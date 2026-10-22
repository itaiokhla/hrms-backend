"""Statutory reports: the pack-defined returns of a legal entity and their lifecycle (3.2 §8.2, 3.9 §3.10).

DRAFT → CALCULATED → REVIEW_REQUIRED → VERIFIED → READY_TO_FILE → SUBMITTED → ACCEPTED | REJECTED → AMENDED.

EC-STA-11 (an exported file is READY_TO_FILE, never SUBMITTED), EC-STA-12 (a correction after submission is
an amendment linked to the original), decision I-07 (only packs that define reports and may produce final
statutory lines; fixture reports watermarked), EC-SEC-01/02 (three tenants; foreign ids and cross-tenant
references are 404 or refused by composite keys).
"""

import hashlib
from datetime import date

from sqlalchemy import text

from tests.payroll.test_reports import (  # noqa: F401  (rc_world is a fixture)
    OCT,
    SEP,
    grant_role,
    line,
    member,
    ok,
    other_tenant,
    rc_world,
    seed_run,
)
from tests.payroll.test_wage_inputs import CHECK, DENIED, FOREIGN_KEY, audit_actions, expect_error, session, sql, tenant

PAY = "/api/v1/payroll"
SR = f"{PAY}/statutory-reports"


def create(client, entity, code="ZZ_PENSION_RETURN", start="2026-09-01", expect=201):
    return ok(client.post(SR, json={"legal_entity_id": entity, "report_code": code, "period_start": start}), expect)


def act(client, report_id, action, body=None, expect=200):
    return ok(client.post(f"{SR}/{report_id}/{action}", json=body if body is not None else {}), expect)


def get(client, report_id):
    return ok(client.get(f"{SR}/{report_id}"))


def to_ready(client, report_id):
    for action in ("generate", "submit-for-review", "verify", "export"):
        act(client, report_id, action)
    return get(client, report_id)


def test_ec_sta_11_lifecycle_export_is_ready_to_file_never_submitted(rc_world):
    w = rc_world
    acme = w["acme"]
    created = create(acme, w["zz01"])
    assert (created["status"], created["version"], created["row_version"]) == ("DRAFT", 1, 1)
    report = get(acme, created["id"])
    assert report["due_date"] == "2026-10-15" and report["period_end"] == "2026-09-30"
    assert report["pack_version"] == "ZZ-2026.1" and report["is_fixture"] is False and report["watermark"] is None
    assert report["available_actions"] == ["GENERATE"]

    act(acme, created["id"], "generate")
    report = get(acme, created["id"])
    assert report["status"] == "CALCULATED" and report["row_count"] == 2
    assert [c["code"] for c in report["content"]["columns"]] == ["ZZ_PENSION_EE", "ZZ_PENSION_ER"]
    rows = {r["employee_code"]: r for r in report["content"]["rows"]}
    assert rows[w["emp"]["Zoe"]["employee_code"]]["amounts"] == {"ZZ_PENSION_EE": "200.00", "ZZ_PENSION_ER": "280.00"}
    assert report["totals"]["components"] == {"ZZ_PENSION_EE": {"EUR": "350.00"}, "ZZ_PENSION_ER": {"EUR": "490.00"}}
    assert report["content"]["liabilities"] == [{
        "scheme": "ZZ.pension", "payee": "Zedland Pension Fund (fictional)", "currency": "EUR",
        "employee_amount": "350.00", "employer_amount": "490.00", "due_dates": ["2026-10-15"]}]
    assert report["run_ids"] == [w["runs"]["ZZ_SEP"]["id"]]

    act(acme, created["id"], "submit-for-review")
    act(acme, created["id"], "verify")
    exported = act(acme, created["id"], "export")
    assert exported["status"] == "READY_TO_FILE"  # EC-STA-11: never SUBMITTED by an export
    report = get(acme, created["id"])
    assert report["status"] == "READY_TO_FILE" and report["filing_ref"] is None and report["submitted_at"] is None
    assert set(report["available_actions"]) == {"MARK_SUBMITTED", "REGENERATE", "EXPORT"}
    assert "filing reference" in report["lifecycle_note"]
    r = acme.get(f"{SR}/{created['id']}/file")
    assert r.status_code == 200 and hashlib.sha256(r.content).hexdigest() == report["export_sha256"]
    assert r.content.decode().startswith("Report,ZZ_PENSION_RETURN,Monthly pension return")
    assert "no-store" in r.headers["cache-control"]

    # SUBMITTED needs the filing reference from the authority.
    assert acme.post(f"{SR}/{created['id']}/mark-submitted", json={}).status_code == 422
    assert acme.post(f"{SR}/{created['id']}/mark-submitted", json={"filing_ref": "   "}).status_code == 422
    submitted = act(acme, created["id"], "mark-submitted", {"filing_ref": "ZZP-2026-09-001"})
    assert submitted["status"] == "SUBMITTED"
    assert acme.post(f"{SR}/{created['id']}/record-accepted", json={}).status_code == 422
    act(acme, created["id"], "record-accepted", {"acknowledgement_ref": "ACK-778"})
    report = get(acme, created["id"])
    assert (report["status"], report["filing_ref"], report["acknowledgement_ref"]) == (
        "ACCEPTED", "ZZP-2026-09-001", "ACK-778")
    assert audit_actions(w["cid"], "statutory_report", created["id"]) == [
        "STATUTORY_REPORT_CREATED", "STATUTORY_REPORT_GENERATED", "STATUTORY_REPORT_REVIEW_REQUESTED",
        "STATUTORY_REPORT_VERIFIED", "STATUTORY_REPORT_EXPORTED", "STATUTORY_REPORT_DOWNLOADED",
        "STATUTORY_REPORT_SUBMITTED", "STATUTORY_REPORT_ACCEPTED"]
    # The database refuses SUBMITTED without a filing reference too.
    with tenant(w["cid"]) as conn:
        expect_error(conn, CHECK, "UPDATE statutory_reports SET filing_ref = NULL WHERE id = :r", r=created["id"])


def test_mark_submitted_needs_statutory_submit_and_scope(app, rc_world):
    w = rc_world
    cleo = w["cleo"]  # payroll:statutory:review at ZZ01 only
    created = create(cleo, w["zz01"])
    report = to_ready(cleo, created["id"])
    assert report["status"] == "READY_TO_FILE" and "MARK_SUBMITTED" not in report["available_actions"]
    assert cleo.post(f"{SR}/{created['id']}/mark-submitted", json={"filing_ref": "X-1"}).status_code == 403
    # Outside Cleo's entities everything is 404.
    founder_report = create(w["acme"], w["le01"], "IN_FORM_138", "2026-07-01")
    assert cleo.get(f"{SR}/{founder_report['id']}").status_code == 404
    assert cleo.post(f"{SR}/{founder_report['id']}/generate", json={}).status_code == 404
    assert cleo.post(SR, json={"legal_entity_id": w["le01"], "report_code": "IN_FORM_138",
                               "period_start": "2026-07-01"}).status_code == 404
    assert [r["id"] for r in ok(cleo.get(SR))["items"]] == [created["id"]]
    # No statutory or reports permission at all: 403.
    processor = grant_role(app, w["acme"], "Pete", [("payroll:run:read", "COMPANY")])
    assert processor.get(SR).status_code == 403
    assert processor.post(SR, json={"legal_entity_id": w["zz01"], "report_code": "ZZ_PENSION_RETURN",
                                    "period_start": "2026-09-01"}).status_code == 403
    # A reports reader sees the report but cannot act on it.
    reader = grant_role(app, w["acme"], "Rae", [("payroll:reports:read", "COMPANY")])
    assert get(reader, created["id"])["available_actions"] == []
    assert reader.post(f"{SR}/{created['id']}/regenerate", json={"reason": "x"}).status_code == 403


def test_ec_sta_12_correction_after_submission_is_an_amendment_linked_to_the_original(rc_world):
    w = rc_world
    acme = w["acme"]
    original = create(acme, w["zz01"])["id"]
    to_ready(acme, original)
    act(acme, original, "mark-submitted", {"filing_ref": "ZZP-1"})
    assert acme.post(f"{SR}/{original}/amend", json={}).status_code == 422  # a reason is required
    amended = act(acme, original, "amend", {"reason": "A late joiner was missing"}, expect=201)
    assert amended["status"] == "AMENDED"
    amendment = amended["amendment"]
    assert (amendment["status"], amendment["version"], amendment["amends_report_id"]) == ("DRAFT", 2, original)
    first = get(acme, original)
    assert first["amended_by_report_id"] == amendment["id"] and first["filing_ref"] == "ZZP-1"
    assert first["available_actions"] == []
    assert acme.post(f"{SR}/{original}/amend", json={"reason": "again"}).json()["code"] == "INVALID_TRANSITION"
    second = get(acme, amendment["id"])
    assert second["amends_report_id"] == original and second["status_reason"] == "A late joiner was missing"
    act(acme, amendment["id"], "generate")
    assert get(acme, amendment["id"])["totals"] == first["totals"]
    listed = ok(acme.get(SR, params={"legal_entity_id": w["zz01"]}))["items"]
    assert [(r["version"], r["status"]) for r in listed] == [(2, "CALCULATED"), (1, "AMENDED")]
    # A filed report is never edited in place, not even by the database owner; only drafts are deleted.
    expect_error_owner(CHECK, "UPDATE statutory_reports SET content = '{}'::jsonb WHERE id = :r", r=original)
    expect_error_owner(CHECK, "UPDATE statutory_reports SET period_start = '2026-08-01' WHERE id = :r",
                       r=amendment["id"])
    expect_error_owner(CHECK, "DELETE FROM statutory_reports WHERE id = :r", r=amendment["id"])
    assert audit_actions(w["cid"], "statutory_report", amendment["id"])[0] == "STATUTORY_REPORT_CREATED"


def expect_error_owner(code, statement, **params):
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        expect_error(conn, code, statement, **params)


def test_rejected_report_records_the_authority_response_and_can_be_amended(rc_world):
    w = rc_world
    acme = w["acme"]
    report = create(acme, w["zz01"])["id"]
    to_ready(acme, report)
    act(acme, report, "mark-submitted", {"filing_ref": "ZZP-2"})
    assert acme.post(f"{SR}/{report}/record-rejected", json={"authority_response": " "}).status_code == 422
    act(acme, report, "record-rejected", {"authority_response": "Employee identifier missing"})
    rejected = get(acme, report)
    assert rejected["status"] == "REJECTED" and rejected["authority_response"] == "Employee identifier missing"
    assert act(acme, report, "amend", {"reason": "Fix identifiers"}, expect=201)["amendment"]["version"] == 2


def test_only_packs_that_define_reports_and_may_produce_final_lines_have_reports(rc_world):
    w = rc_world
    acme = w["acme"]
    # The UAE pack is a DRAFT: nothing is fileable from unverified rules.
    assert acme.post(SR, json={"legal_entity_id": w["ae01"], "report_code": "AE_WPS_SIF",
                               "period_start": "2026-09-01"}).json()["code"] == "PACK_NOT_VERIFIED"
    # The Indian fixture defines a quarterly return: periods follow its frequency; the report is watermarked.
    assert acme.post(SR, json={"legal_entity_id": w["le01"], "report_code": "IN_FORM_138",
                               "period_start": "2026-08-01"}).json()["code"] == "INVALID_REPORT_PERIOD"
    fixture = create(acme, w["le01"], "IN_FORM_138", "2026-07-01")
    report = get(acme, fixture["id"])
    assert report["is_fixture"] is True and report["watermark"] == "FIXTURE — not law"
    assert (report["period_start"], report["period_end"], report["frequency"]) == ("2026-07-01", "2026-09-30",
                                                                                   "QUARTERLY")
    to_ready(acme, fixture["id"])
    file = acme.get(f"{SR}/{fixture['id']}/file").content.decode()
    assert file.startswith("FIXTURE — not law")
    totals = get(acme, fixture["id"])["totals"]["components"]
    assert totals["IN_EPF_EE"] == {"INR": "7200.00"}  # August and September runs
    # Unknown codes, duplicates, unknown entities.
    assert acme.post(SR, json={"legal_entity_id": w["zz01"], "report_code": "ZZ_NOPE",
                               "period_start": "2026-09-01"}).json()["code"] == "UNKNOWN_REPORT_CODE"
    create(acme, w["zz01"])
    assert acme.post(SR, json={"legal_entity_id": w["zz01"], "report_code": "ZZ_PENSION_RETURN",
                               "period_start": "2026-09-01"}).json()["code"] == "DUPLICATE_REPORT"
    assert acme.post(SR, json={"legal_entity_id": 999999, "report_code": "ZZ_PENSION_RETURN",
                               "period_start": "2026-09-01"}).status_code == 404
    # Server-owned fields are refused.
    assert acme.post(SR, json={"legal_entity_id": w["zz01"], "report_code": "ZZ_WHT_RETURN",
                               "period_start": "2026-09-01", "status": "SUBMITTED"}).status_code == 422


def test_generate_reads_finalized_runs_only_and_verify_detects_later_changes(rc_world):
    w = rc_world
    acme = w["acme"]
    october = create(acme, w["zz01"], start="2026-10-01")["id"]
    assert acme.post(f"{SR}/{october}/generate", json={}).json()["code"] == "NO_FINALIZED_RUNS"
    seed_run(w["cid"], entity=w["zz01"], group=w["groups"]["ZZ"], period=w["periods"]["ZZ"][OCT], currency="EUR",
             status="CALCULATED", members=[member(w["emp"]["Zoe"], "4000.00", "0.00", "4000.00", "4000.00")])
    assert acme.post(f"{SR}/{october}/generate", json={}).json()["code"] == "RUNS_NOT_FINALIZED"
    assert get(acme, october)["status"] == "DRAFT"

    september = create(acme, w["zz01"])["id"]
    act(acme, september, "generate")
    act(acme, september, "submit-for-review")
    seed_run(w["cid"], entity=w["zz01"], group=w["groups"]["ZZ"], period=w["periods"]["ZZ"][SEP], currency="EUR",
             run_type="OFF_CYCLE", members=[member(w["emp"]["Zoe"], "200.00", "10.00", "190.00", "214.00", lines=[
                 line("BONUS", amount="200.00", category="BONUS"),
                 line("ZZ_PENSION_EE", "DEDUCTION", "10.00", statutory=True),
                 line("ZZ_PENSION_ER", "EMPLOYER_CONTRIBUTION", "14.00", statutory=True)])])
    assert acme.post(f"{SR}/{september}/verify", json={}).json()["code"] == "REPORT_OUT_OF_DATE"
    assert acme.post(f"{SR}/{september}/regenerate", json={}).status_code == 422  # reason required
    act(acme, september, "regenerate", {"reason": "Off-cycle bonus run finalized"})
    report = get(acme, september)
    assert report["status"] == "CALCULATED" and len(report["run_ids"]) == 2
    assert report["totals"]["components"]["ZZ_PENSION_EE"] == {"EUR": "360.00"}


def test_row_version_conflicts_and_invalid_transitions_are_409(rc_world):
    w = rc_world
    acme = w["acme"]
    report = create(acme, w["zz01"])["id"]
    assert acme.post(f"{SR}/{report}/generate", json={"row_version": 7}).json()["code"] == "ROW_VERSION_CONFLICT"
    assert acme.post(f"{SR}/{report}/export", json={}).json()["code"] == "INVALID_TRANSITION"
    assert acme.post(f"{SR}/{report}/mark-submitted", json={"filing_ref": "X"}).status_code == 409
    act(acme, report, "generate", {"row_version": 1})
    assert get(acme, report)["row_version"] == 2
    assert acme.post(f"{SR}/{report}/generate", json={"bogus": 1}).status_code == 422
    # An idempotent retry replays the first response.
    headers = {"Idempotency-Key": "sr-review-1"}
    first = acme.post(f"{SR}/{report}/submit-for-review", json={}, headers=headers)
    again = acme.post(f"{SR}/{report}/submit-for-review", json={}, headers=headers)
    assert first.status_code == again.status_code == 200 and again.headers.get("Idempotent-Replayed") == "true"
    assert get(acme, report)["status"] == "REVIEW_REQUIRED"


def _seed_report(company_id, entity, amends=None, version=1):
    from app.models.reports_compliance import StatutoryReport

    with session(company_id) as (db, _):
        row = StatutoryReport(company_id=company_id, legal_entity_id=entity, pack_code="IN",
                              pack_version="IN-2026.10-FIXTURE", report_code="IN_FORM_138", report_name="Form 138",
                              frequency="QUARTERLY", period_start=date(2026, 7, 1), period_end=date(2026, 9, 30),
                              status="DRAFT", version=version, amends_report_id=amends, is_fixture=True,
                              watermark="FIXTURE — not law", run_ids=[], totals={}, content={}, row_version=1)
        db.add(row)
        db.commit()
        return row.id


def test_ec_sec_01_statutory_reports_are_isolated_across_three_tenants(app, rc_world):
    w = rc_world
    beta, cobalt = other_tenant(app, "Beta Industries"), other_tenant(app, "Cobalt Labs")
    acme_report = create(w["acme"], w["zz01"])["id"]
    seeded = {w["cid"]: acme_report, beta["cid"]: _seed_report(beta["cid"], beta["entity"]),
              cobalt["cid"]: _seed_report(cobalt["cid"], cobalt["entity"])}
    for cid, own in seeded.items():
        others = [c for c in seeded if c != cid]
        with tenant(cid) as conn:
            assert {i for (i,) in conn.execute(text("SELECT id FROM statutory_reports"))} == {own}
            for other in others:
                assert conn.execute(text("UPDATE statutory_reports SET status_reason = 'x' WHERE id = :i"),
                                    {"i": seeded[other]}).rowcount == 0
            expect_error(conn, DENIED, "UPDATE statutory_reports SET company_id = :o WHERE id = :i", o=others[0],
                         i=own)
    with tenant(beta["cid"]) as conn:  # composite keys refuse another tenant's entity or original report
        expect_error(conn, FOREIGN_KEY,
                     "INSERT INTO statutory_reports (company_id, legal_entity_id, pack_code, pack_version, report_code, "
                     "report_name, frequency, period_start, period_end, status, version, amends_report_id, "
                     "created_at, updated_at) VALUES (:c, :le, 'IN', 'v', 'IN_FORM_138', 'x', 'QUARTERLY', "
                     "'2026-04-01', '2026-06-30', 'DRAFT', 2, :a, now(), now())",
                     c=beta["cid"], le=beta["entity"], a=acme_report)
        expect_error(conn, FOREIGN_KEY,
                     "INSERT INTO statutory_reports (company_id, legal_entity_id, pack_code, pack_version, report_code, "
                     "report_name, frequency, period_start, period_end, status, created_at, updated_at) VALUES "
                     "(:c, :le, 'ZZ', 'v', 'ZZ_PENSION_RETURN', 'x', 'MONTHLY', '2026-04-01', '2026-04-30', 'DRAFT', "
                     "now(), now())", c=beta["cid"], le=w["zz01"])
    client = beta["client"]
    for method, path, body in (("get", f"{SR}/{acme_report}", None), ("get", f"{SR}/{acme_report}/file", None),
                               ("post", f"{SR}/{acme_report}/generate", {}),
                               ("post", f"{SR}/{acme_report}/amend", {"reason": "x"}),
                               ("post", f"{SR}/{acme_report}/mark-submitted", {"filing_ref": "x"})):
        r = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
        assert r.status_code == 404, (path, r.text)
    assert client.post(SR, json={"legal_entity_id": w["zz01"], "report_code": "ZZ_PENSION_RETURN",
                                 "period_start": "2026-09-01"}).status_code == 404
    assert [r["id"] for r in ok(client.get(SR))["items"]] == [seeded[beta["cid"]]]
    assert get(w["acme"], acme_report)["status"] == "DRAFT"

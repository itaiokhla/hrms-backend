"""Accounting journals of finalized runs (3.1 §5.18, 3.3 §2.2 gl; decision I-13: OFF by default).

One GENERIC_CSV journal per run at ``payroll.accounting.post_on`` (FINALIZE through the outbox event
``payroll.run.finalized``, or PAYMENT_SETTLED once the run is paid) when ``payroll.accounting.enabled`` is ON:
debit / credit per component GL mapping, optional cost-center allocation, balanced per currency, never
double-posted, immutable once posted; a missing mapping fails the whole journal, which is retried in place.

EC-DOC-17 (CSV cells escaped), EC-MON-04 (balanced and totalled per currency), EC-SEC-01/02/05.
"""

from decimal import Decimal

from sqlalchemy import text

from app.payroll.money import amount_str
from app.payroll.services import accounting
from tests.payroll.test_reports import (  # noqa: F401  (rc_world is a fixture)
    csv_rows,
    grant_role,
    ok,
    other_tenant,
    rc_world,
    with_roles,
)
from tests.payroll.test_runs_lifecycle import runs_world  # noqa: F401  (a fixture)
from tests.payroll.test_wage_inputs import (
    CHECK,
    DENIED,
    FOREIGN_KEY,
    audit_actions,
    expect_error,
    session,
    set_setting,
    sql,
    tenant,
)

PAY = "/api/v1/payroll"
JOURNALS = f"{PAY}/accounting/journals"
MAPPINGS = f"{PAY}/accounting/gl-mappings"
ZZ_ACCOUNTS = {"BASIC": ("6000", "2100"), "ZZ_PENSION_EE": ("2100", "2200"), "ZZ_PENSION_ER": ("6100", "2200"),
               "ZZ_WHT": ("2100", "+2300")}


def enable(company_id, post_on="FINALIZE"):
    set_setting(company_id, "payroll.accounting.enabled", True)
    set_setting(company_id, "payroll.accounting.post_on", post_on)


def map_codes(client, accounts, entity=None, allocate=False):
    for code, (debit, credit) in accounts.items():
        ok(client.put(MAPPINGS, json={"component_code": code, "debit_account": debit, "credit_account": credit,
                                      "legal_entity_id": entity, "allocate_by_cost_center": allocate}))


def handle(company_id, run_id, trigger="FINALIZE"):
    with session(company_id) as (db, _):
        result = accounting.handle_event(db, company_id, run_id, trigger)
        db.commit()
        return result


def emit_finalized(company_id, run_id):
    from app.payroll.services import outbox

    with session(company_id) as (db, _):
        outbox.emit(db, accounting.FINALIZED_TOPIC, {"run_id": run_id})
        db.commit()


def journals_of(client, run_id):
    return ok(client.get(JOURNALS, params={"run_id": run_id}))["items"]


# ---------------------------------------------------------------------------
# Pure journal building
# ---------------------------------------------------------------------------
def test_entries_balance_per_currency_negative_lines_swap_and_mapping_precedence():
    S = accounting.SourceLine
    mapping = accounting.Mapping("6000", "2100", False, "TENANT_MAPPING")
    lines = [S("BASIC", None, "EARNING", Decimal("100.00"), "EUR", None, None, False),
             S("BASIC", None, "EARNING", Decimal("-30.00"), "EUR", None, None, False),
             S("BASIC", None, "EARNING", Decimal("5000"), "INR", None, None, False),
             S("NOTE", None, "INFORMATIONAL", Decimal("9.00"), "EUR", None, None, False),
             S("BIK", None, "BENEFIT_IN_KIND", Decimal("9.00"), "EUR", None, None, False)]
    entries, errors = accounting.build_entries(lines, lambda line: mapping)
    assert errors == []
    by_key = {(e.currency, e.side, e.account): e.amount for e in entries}
    # A negative line swaps the sides; informational lines and benefits in kind move no money.
    assert by_key[("EUR", "DEBIT", "6000")] == Decimal("100.00")
    assert by_key[("EUR", "DEBIT", "2100")] == Decimal("30.00")
    assert by_key[("EUR", "CREDIT", "2100")] == Decimal("100.00")
    assert by_key[("EUR", "CREDIT", "6000")] == Decimal("30.00")
    assert accounting.balances(entries) == {"EUR": (Decimal("130.00"), Decimal("130.00")),
                                            "INR": (Decimal("5000"), Decimal("5000"))}
    entries, errors = accounting.build_entries(lines, lambda line: None)
    assert entries == [] and [e["code"] for e in errors] == ["MISSING_GL_MAPPING"]
    allocated = accounting.Mapping("6000", "2100", True, "COMPONENT")
    _, errors = accounting.build_entries(lines[:1], lambda line: allocated)
    assert [e["code"] for e in errors] == ["COST_CENTER_MISSING"]

    entity = {"BASIC": accounting.Mapping("E-D", "E-C", False, "ENTITY_MAPPING")}
    tenant_rows = {"BASIC": accounting.Mapping("T-D", "T-C", False, "TENANT_MAPPING")}
    own_gl = {"debit_account": "C-D", "credit_account": "C-C"}
    assert accounting.resolve_mapping("BASIC", own_gl, entity, tenant_rows).source == "ENTITY_MAPPING"
    assert accounting.resolve_mapping("BASIC", own_gl, {}, tenant_rows).debit_account == "C-D"
    assert accounting.resolve_mapping("BASIC", {"debit_account": "C-D"}, {}, tenant_rows).source == "TENANT_MAPPING"
    assert accounting.resolve_mapping("BASIC", None, {}, {}) is None


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------
def test_journal_posts_on_the_finalized_event_balanced_and_never_double_posted(rc_world):
    w = rc_world
    acme, cid = w["acme"], w["cid"]
    run = w["runs"]["ZZ_SEP"]["id"]
    enable(cid)
    map_codes(acme, ZZ_ACCOUNTS)
    emit_finalized(cid, run)
    [journal] = journals_of(acme, run)
    assert (journal["status"], journal["trigger"], journal["format"], journal["number"]) == (
        "POSTED", "FINALIZE", "GENERIC_CSV", "JE-2026-000001")
    assert journal["posting_date"] == "2026-09-30" and journal["line_count"] == 8
    assert journal["totals"] == {"EUR": {"credit": "8340.00", "debit": "8340.00", "lines": 8}}
    detail = ok(acme.get(f"{JOURNALS}/{journal['id']}"))
    sides = {(l["component_code"], l["side"]): (l["account"], l["amount"]) for l in detail["lines"]}
    assert sides[("BASIC", "DEBIT")] == ("6000", {"amount": "7000.00", "currency": "EUR"})
    assert sides[("ZZ_PENSION_ER", "CREDIT")] == ("2200", {"amount": "490.00", "currency": "EUR"})
    assert sides[("ZZ_WHT", "CREDIT")] == ("+2300", {"amount": "500.00", "currency": "EUR"})

    # A repeated event, a direct handler call or a manual post return the same journal (never twice).
    emit_finalized(cid, run)
    assert handle(cid, run) == {"journal_id": journal["id"], "status": "POSTED"}
    posted = ok(acme.post(f"{PAY}/runs/{run}/journal"))
    assert posted["id"] == journal["id"] and posted["status"] == "POSTED"
    assert sql("SELECT count(*), max(attempts) FROM accounting_journals WHERE company_id = :c", c=cid)[0] == (1, 1)
    assert sql("SELECT count(*) FROM accounting_journal_lines WHERE journal_id = :j", j=journal["id"])[0][0] == 8

    r = acme.get(f"{JOURNALS}/{journal['id']}/export")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = csv_rows(r.content)
    assert rows[0] == list(accounting.CSV_COLUMNS) and len(rows) == 9
    assert "'+2300" in [row[5] for row in rows[1:]]  # EC-DOC-17
    debit = sum(Decimal(row[8]) for row in rows[1:] if row[8])
    credit = sum(Decimal(row[9]) for row in rows[1:] if row[9])
    assert debit == credit == Decimal("8340.00")
    assert audit_actions(cid, "accounting_journal", journal["id"]) == ["JOURNAL_POSTED", "JOURNAL_EXPORTED"]


def test_missing_mapping_fails_the_whole_journal_and_a_retry_posts_the_same_row(rc_world):
    w = rc_world
    acme, cid = w["acme"], w["cid"]
    run = w["runs"]["IN_SEP"]["id"]
    enable(cid)
    map_codes(acme, {"BASIC": ("6000", "2100")})
    assert handle(cid, run)["status"] == "FAILED"
    [journal] = journals_of(acme, run)
    assert journal["status"] == "FAILED" and journal["line_count"] == 0 and journal["totals"] == {}
    assert sorted(e["component"] for e in journal["errors"]) == [
        "IN_EPF_EE", "IN_EPF_ER", "OTHER_DEDUCTION", "OT_WORKDAY"]
    assert sql("SELECT count(*) FROM accounting_journal_lines WHERE journal_id = :j", j=journal["id"])[0][0] == 0
    assert acme.get(f"{JOURNALS}/{journal['id']}/export").json()["code"] == "JOURNAL_NOT_POSTED"

    # Entity-level mappings for the rest; the retry turns the same row POSTED.
    map_codes(acme, {"OT_WORKDAY": ("6050", "2100"), "IN_EPF_EE": ("2100", "2400"),
                     "IN_EPF_ER": ("6100", "2400"), "OTHER_DEDUCTION": ("2100", "2500")}, entity=w["le01"])
    posted = ok(acme.post(f"{PAY}/runs/{run}/journal"))
    assert posted["id"] == journal["id"] and posted["status"] == "POSTED" and posted["errors"] == []
    journal = ok(acme.get(f"{JOURNALS}/{journal['id']}"))
    assert journal["attempts"] == 2 and journal["trigger"] == "MANUAL" and journal["fixture"] is True
    assert journal["totals"]["INR"]["debit"] == journal["totals"]["INR"]["credit"] == "104500.00"
    assert audit_actions(cid, "accounting_journal", journal["id"]) == ["JOURNAL_FAILED", "JOURNAL_POSTED"]


def test_cost_center_allocation_needs_a_cost_center_for_every_line(rc_world):
    from app.models.pay_org import CostCenter

    w = rc_world
    acme, cid = w["acme"], w["cid"]
    run = w["runs"]["ZZ_SEP"]["id"]
    enable(cid)
    map_codes(acme, ZZ_ACCOUNTS)
    map_codes(acme, {"BASIC": ("6000", "2100")}, entity=w["zz01"], allocate=True)
    assert handle(cid, run)["status"] == "FAILED"
    [journal] = journals_of(acme, run)
    assert [e["code"] for e in journal["errors"]] == ["COST_CENTER_MISSING"]
    with session(cid) as (db, _):
        centers = [CostCenter(company_id=cid, code=code, name=code, legal_entity_id=w["zz01"], status="ACTIVE")
                   for code in ("CC-OPS", "CC-SALES")]
        db.add_all(centers)
        db.commit()
        ops, sales = centers[0].id, centers[1].id
    for name, center in (("Zoe", ops), ("Zack", sales)):
        sql("UPDATE employee_assignments SET cost_center_id = :cc WHERE employee_id = :e AND legal_entity_id = :le",
            cc=center, e=w["emp"][name]["id"], le=w["zz01"])
    assert handle(cid, run)["status"] == "POSTED"
    detail = ok(acme.get(f"{JOURNALS}/{journal['id']}"))
    basic = {(l["side"], l["cost_center_code"]): l["amount"]["amount"] for l in detail["lines"]
             if l["component_code"] == "BASIC"}
    assert basic == {("DEBIT", "CC-OPS"): "4000.00", ("CREDIT", "CC-OPS"): "4000.00",
                     ("DEBIT", "CC-SALES"): "3000.00", ("CREDIT", "CC-SALES"): "3000.00"}
    assert all(l["cost_center_code"] is None for l in detail["lines"] if l["component_code"] != "BASIC")


def test_disabled_accounting_and_post_on_payment_settled(rc_world):
    w = rc_world
    acme, cid = w["acme"], w["cid"]
    run = w["runs"]["ZZ_SEP"]["id"]
    map_codes(acme, ZZ_ACCOUNTS)
    # OFF by default (decision I-13): nothing is posted, by event or by hand.
    assert handle(cid, run) == {"skipped": "ACCOUNTING_DISABLED"}
    assert acme.post(f"{PAY}/runs/{run}/journal").json()["code"] == "ACCOUNTING_DISABLED"
    enable(cid, post_on="PAYMENT_SETTLED")
    assert handle(cid, run) == {"skipped": "POST_ON_PAYMENT_SETTLED"}
    assert handle(cid, run, "PAYMENT_SETTLED") == {"skipped": "RUN_NOT_PAID"}
    assert acme.post(f"{PAY}/runs/{run}/journal").json()["code"] == "RUN_NOT_PAID"
    assert journals_of(acme, run) == []
    sql("UPDATE payroll_runs SET status = 'PAID' WHERE id = :r", r=run)
    with session(cid) as (db, _):
        result = accounting.on_payment_settled(db, cid, run)
        db.commit()
    assert result["status"] == "POSTED"
    [journal] = journals_of(acme, run)
    assert journal["trigger"] == "PAYMENT_SETTLED"
    # Unfinalized runs never have journals.
    assert handle(cid, w["runs"]["AE_SEP"]["id"]) == {"skipped": "RUN_NOT_FINALIZED"}
    assert acme.post(f"{PAY}/runs/{w['runs']['AE_SEP']['id']}/journal").status_code == 409


def test_payment_settled_posts_when_the_payments_milestone_moves_the_run_to_paid(rc_world):
    """post_on = PAYMENT_SETTLED: the commit that makes a run PAID queues the journal once; a rolled-back
    settlement queues nothing; a return and a second settlement never post a second journal."""
    from app.models.pay_runs import PayrollRun
    from app.payroll.services import runs

    w = rc_world
    acme, cid = w["acme"], w["cid"]
    run = w["runs"]["ZZ_SEP"]["id"]
    enable(cid, post_on="PAYMENT_SETTLED")
    map_codes(acme, ZZ_ACCOUNTS)
    sql("UPDATE payroll_runs SET status = 'SUBMITTED' WHERE id = :r", r=run)

    def settle(action="RECORD_FULL_SETTLEMENT", *, commit=True, **guards):
        with session(cid) as (db, _):
            row = db.query(PayrollRun).filter(PayrollRun.company_id == cid, PayrollRun.id == run).one()
            runs.transition(db, None, row, action, system=True, **(guards or {"SETTLEMENT_EVIDENCE": True}))
            if commit:
                db.commit()
            else:
                db.rollback()

    settle(commit=False)
    assert journals_of(acme, run) == []
    assert sql("SELECT status FROM payroll_runs WHERE id = :r", r=run)[0][0] == "SUBMITTED"
    settle()
    [journal] = journals_of(acme, run)
    assert (journal["status"], journal["trigger"], journal["line_count"]) == ("POSTED", "PAYMENT_SETTLED", 8)
    assert journal["totals"]["EUR"]["debit"] == journal["totals"]["EUR"]["credit"] == "8340.00"
    settle("RECORD_RETURN", RETURN_EVIDENCE=True)
    settle()
    assert [j["id"] for j in journals_of(acme, run)] == [journal["id"]]
    assert sql("SELECT count(*), max(attempts) FROM accounting_journals WHERE company_id = :c", c=cid)[0] == (1, 1)


def test_end_to_end_a_really_finalized_run_feeds_the_journal_statutory_report_and_overview(app, runs_world,
                                                                                         monkeypatch):
    """The real path (engine calculation, approval, step-up finalization, outbox): the finalized event posts
    the journal; the statutory report and the overview read the same finalized figures."""
    from datetime import date as day

    from app.payroll.services import outbox
    from tests.payroll.test_run_finalize import (
        FINALIZED_TOPIC,
        approve,
        calculate,
        confirm_countries,
        finalize,
        new_run,
        step_up,
        submit,
        user_id_of,
        zz_entity,
    )

    # Only the journal subscriber runs here (documents, payments and corrections have their own tests).
    monkeypatch.setitem(outbox.SUBSCRIBERS, FINALIZED_TOPIC, (accounting.FINALIZED_JOB,))
    w = runs_world
    acme, cid = w["acme"], w["cid"]
    user_id_of(acme)
    step_up(acme)
    confirm_countries(cid)
    zz = zz_entity(w)
    run = new_run(acme, zz["group"], zz["period"]["id"])["id"]
    calculate(acme, run, kind="ZZ", store_outputs=True)
    submit(acme, run)
    approve(w["approver"], run)
    enable(cid)
    codes = [c for (c,) in sql("SELECT DISTINCT component_code FROM payroll_lines WHERE run_id = :r", r=run)]
    map_codes(acme, {code: (f"6{i:03d}", f"2{i:03d}") for i, code in enumerate(sorted(codes))})
    finalize(acme, run, key="e2e-finalize")

    [journal] = journals_of(acme, run)
    assert (journal["status"], journal["trigger"], journal["fixture"]) == ("POSTED", "FINALIZE", False)
    moved = sql("SELECT coalesce(sum(abs(amount)), 0) FROM payroll_lines WHERE run_id = :r AND kind IN "
                "('EARNING', 'DEDUCTION', 'EMPLOYER_CONTRIBUTION', 'REIMBURSEMENT', 'ROUNDING')", r=run)[0][0]
    assert Decimal(journal["totals"]["EUR"]["debit"]) == Decimal(journal["totals"]["EUR"]["credit"]) == moved

    pay_date = sql("SELECT pay_date FROM pay_periods WHERE id = :p", p=zz["period"]["id"])[0][0]
    report = ok(acme.post(f"{PAY}/statutory-reports", json={
        "legal_entity_id": zz["entity"], "report_code": "ZZ_PENSION_RETURN",
        "period_start": day(pay_date.year, pay_date.month, 1).isoformat()}), 201)["id"]
    ok(acme.post(f"{PAY}/statutory-reports/{report}/generate", json={}))
    generated = ok(acme.get(f"{PAY}/statutory-reports/{report}"))
    assert generated["run_ids"] == [run] and generated["is_fixture"] is False
    pension = dict(sql("SELECT component_code, sum(amount)::text FROM payroll_lines WHERE run_id = :r "
                       "AND component_code IN ('ZZ_PENSION_EE', 'ZZ_PENSION_ER') GROUP BY component_code", r=run))
    assert pension and generated["totals"]["components"] == {
        code: {"EUR": amount_str(Decimal(amount), "EUR")} for code, amount in pension.items()}

    period = {"period_from": zz["period"]["start"], "period_to": zz["period"]["end"], "pay_group_id": zz["group"]}
    overview = ok(acme.get(f"{PAY}/overview", params=period))
    assert (overview["kpis"]["eligible"], overview["kpis"]["processed"], overview["kpis"]["paid"]) == (1, 1, 0)
    net = sql("SELECT net::text FROM payroll_run_employees WHERE run_id = :r AND status = 'CALCULATED'", r=run)[0][0]
    assert [(m["currency"], m["stage"], m["net"]) for m in overview["money"]] == [
        ("EUR", "Final", amount_str(Decimal(net), "EUR"))]
    assert [s["legal_entity_id"] for s in overview["statutory"]] == [zz["entity"]]


def test_posted_journals_and_lines_are_immutable(rc_world):
    from app.database.db import system_engine

    w = rc_world
    cid = w["cid"]
    run = w["runs"]["ZZ_SEP"]["id"]
    enable(cid)
    map_codes(w["acme"], ZZ_ACCOUNTS)
    handle(cid, run)
    journal = sql("SELECT id FROM accounting_journals WHERE run_id = :r", r=run)[0][0]
    with system_engine.begin() as conn:  # even the database owner cannot change a posted journal
        expect_error(conn, CHECK, "UPDATE accounting_journals SET totals = '{}'::jsonb WHERE id = :j", j=journal)
        expect_error(conn, CHECK, "DELETE FROM accounting_journals WHERE id = :j", j=journal)
        expect_error(conn, CHECK, "UPDATE accounting_journal_lines SET amount = 1 WHERE journal_id = :j", j=journal)
        expect_error(conn, CHECK, "DELETE FROM accounting_journal_lines WHERE journal_id = :j", j=journal)
    with tenant(cid) as conn:  # one journal per run
        expect_error(conn, "23505",
                     "INSERT INTO accounting_journals (company_id, run_id, legal_entity_id, number, trigger, status, "
                     "posting_date, errors, created_at, updated_at) VALUES (:c, :r, :le, 'JE-2026-000099', 'MANUAL', "
                     "'FAILED', '2026-09-30', '[{\"code\": \"X\"}]'::jsonb, now(), now())",
                     c=cid, r=run, le=w["zz01"])
    # A tampered line is detected on read.
    with system_engine.begin() as conn:
        conn.execute(text("ALTER TABLE accounting_journal_lines DISABLE TRIGGER trg_accounting_journal_lines_append_only"))
        conn.execute(text("UPDATE accounting_journal_lines SET amount = amount + 1 WHERE journal_id = :j AND line_no = 1"),
                     {"j": journal})
        conn.execute(text("ALTER TABLE accounting_journal_lines ENABLE TRIGGER trg_accounting_journal_lines_append_only"))
    assert w["acme"].get(f"{JOURNALS}/{journal}").json()["code"] == "JOURNAL_INTEGRITY"


def test_ec_sec_05_journal_and_mapping_access_follows_permissions_and_scope(app, rc_world):
    w = rc_world
    acme, cid = w["acme"], w["cid"]
    enable(cid)
    map_codes(acme, ZZ_ACCOUNTS)
    map_codes(acme, {"OT_WORKDAY": ("6050", "2100"), "IN_EPF_EE": ("2100", "2400"),
                     "IN_EPF_ER": ("6100", "2400"), "OTHER_DEDUCTION": ("2100", "2500")})
    handle(cid, w["runs"]["ZZ_SEP"]["id"])
    handle(cid, w["runs"]["IN_SEP"]["id"])
    zz_journal = journals_of(acme, w["runs"]["ZZ_SEP"]["id"])[0]["id"]
    in_journal = journals_of(acme, w["runs"]["IN_SEP"]["id"])[0]["id"]
    assert ok(acme.get(JOURNALS))["total"] == 2

    cleo = w["cleo"]  # reports read/export and run read at ZZ01 only
    assert [j["id"] for j in ok(cleo.get(JOURNALS))["items"]] == [zz_journal]
    assert cleo.get(f"{JOURNALS}/{in_journal}").status_code == 404
    assert cleo.get(f"{JOURNALS}/{in_journal}/export").status_code == 404
    assert cleo.get(f"{JOURNALS}/{zz_journal}/export").status_code == 200
    assert cleo.post(f"{PAY}/runs/{w['runs']['ZZ_SEP']['id']}/journal").status_code == 403
    assert cleo.get(MAPPINGS).status_code == 403
    assert cleo.put(MAPPINGS, json={"component_code": "BASIC", "debit_account": "1",
                                    "credit_account": "2"}).status_code == 403
    reader = grant_role(app, acme, "Rex", [("payroll:reports:read", "COMPANY")])
    assert reader.get(f"{JOURNALS}/{zz_journal}/export").status_code == 403
    employee = with_roles(app, acme, "Eve", ["employee"])
    assert employee.get(JOURNALS).status_code == 403
    # Entity-scoped configuration cannot write tenant-wide mappings.
    configurer = grant_role(app, acme, "Cora", [("payroll:configuration:write", "LEGAL_ENTITY")], [w["zz01"]])
    assert configurer.put(MAPPINGS, json={"component_code": "BASIC", "debit_account": "1",
                                          "credit_account": "2"}).status_code == 403
    assert ok(configurer.put(MAPPINGS, json={"component_code": "basic", "debit_account": "7000",
                                             "credit_account": "2100", "legal_entity_id": w["zz01"]}))[
        "component_code"] == "BASIC"
    assert configurer.put(MAPPINGS, json={"component_code": "BASIC", "debit_account": "1", "credit_account": "2",
                                          "legal_entity_id": w["le01"]}).status_code == 404
    assert [m["legal_entity_id"] for m in ok(configurer.get(MAPPINGS))["items"]] == [w["zz01"]]
    # Strict requests and row versions.
    assert acme.put(MAPPINGS, json={"component_code": "BASIC", "debit_account": " ", "credit_account": "2"}
                    ).status_code == 422
    assert acme.put(MAPPINGS, json={"component_code": "BASIC", "debit_account": "1", "credit_account": "2",
                                    "company_id": 1}).status_code == 422
    assert acme.put(MAPPINGS, json={"component_code": "BASIC", "debit_account": "1", "credit_account": "2",
                                    "row_version": 9}).json()["code"] == "ROW_VERSION_CONFLICT"
    mapping = next(m for m in ok(acme.get(MAPPINGS))["items"] if m["component_code"] == "ZZ_WHT")
    assert acme.delete(f"{MAPPINGS}/{mapping['id']}").status_code == 204
    assert acme.delete(f"{MAPPINGS}/{mapping['id']}").status_code == 404


def test_ec_sec_01_accounting_tables_are_isolated_across_three_tenants(app, rc_world):
    w = rc_world
    enable(w["cid"])
    map_codes(w["acme"], ZZ_ACCOUNTS)
    handle(w["cid"], w["runs"]["ZZ_SEP"]["id"])
    tenants = {w["cid"]: {"client": w["acme"], "entity": w["zz01"], "run": w["runs"]["ZZ_SEP"]["id"]}}
    for name in ("Beta Industries", "Cobalt Labs"):
        other = other_tenant(app, name)
        enable(other["cid"])
        map_codes(other["client"], {"BASIC": ("6000", "2100")})
        assert handle(other["cid"], other["run"]["id"])["status"] == "POSTED"
        tenants[other["cid"]] = {"client": other["client"], "entity": other["entity"], "run": other["run"]["id"]}
    ids = {}
    for cid in tenants:
        ids[cid] = {
            "accounting_journals": sql("SELECT id FROM accounting_journals WHERE company_id = :c", c=cid)[0][0],
            "accounting_journal_lines": sql("SELECT min(id) FROM accounting_journal_lines WHERE company_id = :c",
                                            c=cid)[0][0],
            "gl_account_mappings": sql("SELECT min(id) FROM gl_account_mappings WHERE company_id = :c", c=cid)[0][0],
        }
    companies = list(tenants)
    for cid in companies:
        others = [c for c in companies if c != cid]
        with tenant(cid) as conn:
            for table in ids[cid]:
                owners = {o for (o,) in conn.execute(text(f"SELECT company_id FROM {table}"))}
                assert owners == {cid}, table
                for other in others:
                    assert conn.execute(text(f"SELECT count(*) FROM {table} WHERE id = :i"),
                                        {"i": ids[other][table]}).scalar() == 0
                # A posted journal and its lines refuse any change (trigger) before row-level security does.
                code = DENIED if table == "gl_account_mappings" else CHECK
                expect_error(conn, code, f"UPDATE {table} SET company_id = :o WHERE id = :i", o=others[0],
                             i=ids[cid][table])
        client = tenants[cid]["client"]
        for other in others:
            foreign_journal = ids[other]["accounting_journals"]
            assert client.get(f"{JOURNALS}/{foreign_journal}").status_code == 404
            assert client.get(f"{JOURNALS}/{foreign_journal}/export").status_code == 404
            assert client.post(f"{PAY}/runs/{tenants[other]['run']}/journal").status_code == 404
            assert client.delete(f"{MAPPINGS}/{ids[other]['gl_account_mappings']}").status_code == 404
            assert client.put(MAPPINGS, json={"component_code": "BASIC", "debit_account": "1", "credit_account": "2",
                                              "legal_entity_id": tenants[other]["entity"]}).status_code == 404
        assert ok(client.get(JOURNALS))["total"] == 1
    a, b = companies[0], companies[1]
    with tenant(a) as conn:  # composite keys refuse another tenant's run, journal or entity
        expect_error(conn, FOREIGN_KEY,
                     "INSERT INTO accounting_journals (company_id, run_id, legal_entity_id, number, trigger, status, "
                     "posting_date, errors, created_at, updated_at) VALUES (:c, :r, :le, 'JE-2026-000500', 'MANUAL', "
                     "'FAILED', '2026-09-30', '[{\"code\": \"X\"}]'::jsonb, now(), now())",
                     c=a, r=tenants[b]["run"], le=tenants[a]["entity"])
        expect_error(conn, FOREIGN_KEY,
                     "INSERT INTO accounting_journal_lines (company_id, journal_id, line_no, account, side, amount, "
                     "currency, component_code, description, created_at, updated_at) VALUES (:c, :j, 99, '1', 'DEBIT', "
                     "1, 'EUR', 'BASIC', 'x', now(), now())", c=a, j=ids[b]["accounting_journals"])
        expect_error(conn, FOREIGN_KEY,
                     "INSERT INTO gl_account_mappings (company_id, legal_entity_id, component_code, debit_account, "
                     "credit_account, created_at, updated_at) VALUES (:c, :le, 'BONUS', '1', '2', now(), now())",
                     c=a, le=tenants[b]["entity"])

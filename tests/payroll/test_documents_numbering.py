"""Document numbering sequences (3.6 §9): patterns ``documents.numbering.<type>.pattern`` per legal entity, scope
per fiscal year / year / month, gapless allocation with ``SELECT ... FOR UPDATE`` (EC-DOC-08), numbers never
reused (EC-DOC-09), uniqueness per (company, legal entity, type, number), and CSV/XLSX cell escaping (EC-DOC-17).
"""

import threading
from datetime import date

import pytest

from tests.payroll.test_payslips_v2 import (  # noqa: F401  (docs_world is a fixture)
    docs_world,
    emma_run,
    generate,
    payslip_of,
)
from tests.payroll.test_wage_inputs import UNIQUE, expect_error, session, sql, tenant


def set_entity_setting(company_id, entity_id, key, value):
    """A legal-entity value of a setting from 2000-01-01 (numbering patterns are per legal entity only)."""
    import json

    sql("DELETE FROM setting_values WHERE company_id = :c AND key = :k AND scope_type = 'LEGAL_ENTITY' "
        "AND scope_id = :le", c=company_id, k=key, le=entity_id)
    sql("INSERT INTO setting_values (company_id, key, scope_type, scope_id, value, locked, effective_from, status, "
        "row_version, source, created_at, updated_at) VALUES (:c, :k, 'LEGAL_ENTITY', :le, CAST(:v AS jsonb), false, "
        "'2000-01-01', 'ACTIVE', 1, 'ADMIN', now(), now())", c=company_id, k=key, le=entity_id, v=json.dumps(value))


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def test_render_number_tokens_and_scope_keys():
    from app.payroll.services import documents as d

    assert d.render_number("PS/{LE}/{FY}/{SEQ:6}", entity_code="IN01", on=date(2026, 11, 3),
                           fiscal_year_start="04-01", seq=412) == "PS/IN01/2026-27/000412"
    assert d.render_number("INV-{YYYY}{MM}-{SEQ:3}", entity_code="X", on=date(2026, 2, 9), fiscal_year_start=None,
                           seq=7) == "INV-202602-007"
    assert d.fiscal_year_label(date(2026, 3, 31), "04-01") == "2025-26"
    assert d.fiscal_year_label(date(2026, 4, 1), "04-01") == "2026-27"
    assert d.fiscal_year_label(date(2026, 4, 1), "01-01") == "2026"
    assert d.scope_key("PS/{FY}/{SEQ:6}", date(2026, 4, 1), "04-01") == "FY2026-27"
    assert d.scope_key("PS/{YYYY}/{MM}/{SEQ:6}", date(2026, 4, 1), "04-01") == "2026-04"
    assert d.scope_key("PS/{YYYY}/{SEQ:6}", date(2026, 4, 1), "04-01") == "2026"
    assert d.scope_key("PS-{SEQ:6}", date(2026, 4, 1), "04-01") == "ALL"


def test_ec_doc_17_csv_and_xlsx_cells_are_escaped():
    from app.payroll.services.documents import escape_cell

    for dangerous in ("=HYPERLINK(\"http://x\")", "+1+1", "-2", "@SUM(A1)", "\tcmd", "\rcmd"):
        assert escape_cell(dangerous) == "'" + dangerous
    assert escape_cell("Emma") == "Emma" and escape_cell(None) == "" and escape_cell("12.50") == "12.50"


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------
def _allocate(cid, entity, doc_type, on=date(2026, 9, 30), commit=True):
    from app.payroll.services import documents

    with session(cid) as (db, _):
        number = documents.allocate_number(db, cid, entity, doc_type, on)
        if commit:
            db.commit()
        else:
            db.rollback()
    return number


def test_payslip_numbers_follow_the_entity_pattern_setting(docs_world):
    w = docs_world
    set_entity_setting(w["cid"], w["le01"], "documents.numbering.payslip.pattern", "SLIP-{YYYY}-{SEQ:4}")
    run_id, rows = emma_run(w)
    generate(w["cid"], run_id)
    numbers = sorted(payslip_of(r)[2] for r in rows.values())
    assert numbers == ["SLIP-2026-0001", "SLIP-2026-0002"]
    assert sql("SELECT scope_key, next_value, gapless FROM document_sequences WHERE company_id = :c "
               "AND doc_type = 'PAYSLIP_V2'", c=w["cid"]) == [("2026", 3, False)]


def test_invalid_numbering_pattern_fails_closed(docs_world):
    from app.core.errors import AppError

    w = docs_world
    set_entity_setting(w["cid"], w["le01"], "documents.numbering.invoice.pattern", "INV-NO-SEQUENCE")
    with pytest.raises(AppError) as info:
        _allocate(w["cid"], w["le01"], "INVOICE")
    assert info.value.status_code == 409 and info.value.code == "NUMBERING_PATTERN_INVALID"
    assert _allocate(w["cid"], w["ae"], "INVOICE").endswith("000001")  # other entities keep the default pattern
    assert sql("SELECT count(*) FROM document_sequences WHERE legal_entity_id = :le", le=w["le01"])[0][0] == 0


def test_ec_doc_08_gapless_invoice_numbers_under_concurrency(docs_world):
    """Two issuers at the same moment: the sequence row lock queues them; numbers are consecutive and unique."""
    from app.payroll.services import documents

    w = docs_world
    cid, entity = w["cid"], w["le01"]
    first = _allocate(cid, entity, "INVOICE")
    assert first.endswith("000001")
    # A rolled-back issue rolls its number back: gapless.
    assert _allocate(cid, entity, "INVOICE", commit=False).endswith("000002")
    barrier = threading.Barrier(4)
    results, errors = [], []

    def issue():
        try:
            with session(cid) as (db, _):
                barrier.wait(timeout=30)
                number = documents.allocate_number(db, cid, entity, "INVOICE", date(2026, 9, 30))
                db.commit()
                results.append(number)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=issue) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    assert sorted(int(n[-6:]) for n in results) == [2, 3, 4, 5]
    assert len(set(results)) == 4
    assert sql("SELECT gapless, next_value FROM document_sequences WHERE company_id = :c AND doc_type = 'INVOICE'",
               c=cid) == [(True, 6)]


def test_ec_doc_09_a_used_number_is_never_issued_again(docs_world):
    """After a sequence reset (for example a pattern change back), already used numbers are skipped."""
    w = docs_world
    run_id, rows = emma_run(w)
    generate(w["cid"], run_id)
    used = sorted(payslip_of(r)[2] for r in rows.values())
    sql("UPDATE document_sequences SET next_value = 1 WHERE company_id = :c AND doc_type = 'PAYSLIP_V2'", c=w["cid"])
    again = _allocate(w["cid"], w["le01"], "PAYSLIP_V2")
    assert again not in used and again.endswith("000003")


def test_numbers_are_unique_per_entity_and_sequences_are_per_entity(docs_world):
    w = docs_world
    assert _allocate(w["cid"], w["le01"], "PAYMENT_ADVICE").endswith("000001")
    assert _allocate(w["cid"], w["ae"], "PAYMENT_ADVICE").endswith("000001")  # own sequence per legal entity
    assert _allocate(w["cid"], w["le01"], "LOAN_STATEMENT") is None  # unnumbered type
    run_id, rows = emma_run(w)
    generate(w["cid"], run_id)
    doc_id = payslip_of(next(iter(rows.values())))[0]
    with tenant(w["cid"]) as conn:  # the database refuses a duplicate number in the entity
        expect_error(conn, UNIQUE,
                     "INSERT INTO documents (company_id, legal_entity_id, doc_type, number, public_id, subject_type, "
                     "subject_id, issue_date, source_key, version, title, status, content, content_hash, sha256, bytes, "
                     "file_name, template_version, renderer, locale, created_at, updated_at) SELECT company_id, "
                     "legal_entity_id, doc_type, number, 'dupdupdupdupdupdupdupdup', subject_type, subject_id, "
                     "issue_date, 'dup:1', 1, title, 'GENERATED', content, content_hash, sha256, bytes, file_name, "
                     "template_version, renderer, locale, now(), now() FROM documents WHERE id = :d", d=doc_id)


def test_three_tenants_have_independent_sequences(docs_world):
    w = docs_world
    for tenant_key in ("beta", "cobalt"):
        t = w[tenant_key]
        assert _allocate(t["cid"], t["entity"], "INVOICE").endswith("000001")
    assert _allocate(w["cid"], w["le01"], "INVOICE").endswith("000001")
    counts = dict(sql("SELECT company_id, count(*) FROM document_sequences GROUP BY company_id"))
    assert set(counts) == {w["cid"], w["beta"]["cid"], w["cobalt"]["cid"]}


def test_sequence_rows_belong_to_the_tenant_entity_only(docs_world):
    from tests.payroll.test_wage_inputs import FOREIGN_KEY

    w = docs_world
    beta_entity = w["beta"]["entity"]
    with tenant(w["cid"]) as conn:  # a sequence cannot point at another tenant's legal entity
        expect_error(conn, FOREIGN_KEY,
                     "INSERT INTO document_sequences (company_id, legal_entity_id, doc_type, scope_key, pattern, "
                     "next_value, gapless, row_version, created_at, updated_at) VALUES (:c, :le, 'INVOICE', 'ALL', "
                     "'X-{SEQ:4}', 1, true, 1, now(), now())", c=w["cid"], le=beta_entity)

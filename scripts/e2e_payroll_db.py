"""Read-only database assertions for the payroll Chrome suite (payroll E2E stack only).

    python scripts/e2e_payroll_db.py <query> [name=value ...]

Prints the rows of one whitelisted, parameterised SELECT as JSON. It connects as the owner of the
embedded PostgreSQL in ``.local-postgres-payroll`` (scripts/payroll_stack.py), in a read-only
transaction, so the browser tests can check what was really stored (statuses, totals, tenant
ownership, unchanged foreign records) independently of the API under test.

Never touches the user's demo cluster (.local-postgres): the data directory is fixed. No query returns
encrypted bank fields, account numbers, IBANs, password hashes or document storage paths.
"""

import json
import sys
from decimal import Decimal
from pathlib import Path

import psycopg2
import psycopg2.extras
from pgserver.utils import PostmasterInfo

DATA_DIR = Path(__file__).resolve().parents[1] / ".local-postgres-payroll"

# Tables whose ownership (company_id) a test may look up by id. Values are SQL identifiers, never user input.
OWNED_TABLES = {
    "run": "payroll_runs",
    "run_employee": "payroll_run_employees",
    "structure": "salary_templates",
    "component": "pay_components",
    "bank_account": "bank_accounts",
    "bank_change": "bank_account_change_requests",
    "document": "documents",
    "batch": "payment_batches",
    "instruction": "payment_instructions",
    "obligation": "payment_obligations",
    "setting": "setting_values",
    "assignment": "compensation_assignments",
    "pay_group": "pay_groups",
    "employee": "employees",
}

QUERIES = {
    "run": """SELECT id, company_id, number, run_type, status, currency, totals, population, stale,
                     created_by_user_id, calculated_by_user_id, submitted_by_user_id, finalized_by_user_id,
                     calculated_at, approved_at, finalized_at, row_version, reverses_run_id
              FROM payroll_runs WHERE id = %(id)s""",
    "runs_of_company": """SELECT id, number, run_type, status, currency, pay_group_id, pay_period_id
                          FROM payroll_runs WHERE company_id = %(company_id)s ORDER BY id""",
    "run_employees": """SELECT re.id, re.employee_id, e.first_name || ' ' || e.last_name AS name, re.status,
                               re.exclusion_reason, re.gross, re.deductions, re.reimbursements, re.net,
                               re.employer_cost, re.currency, re.company_id, e.company_id AS employee_company_id
                        FROM payroll_run_employees re JOIN employees e ON e.id = re.employee_id
                        WHERE re.run_id = %(run_id)s ORDER BY re.employee_id""",
    "run_employee_sums": """SELECT count(*) FILTER (WHERE status = 'CALCULATED') AS calculated,
                                   count(*) FILTER (WHERE status = 'EXCLUDED') AS excluded,
                                   count(*) AS total, sum(gross) AS gross, sum(deductions) AS deductions,
                                   sum(reimbursements) AS reimbursements, sum(net) AS net,
                                   sum(employer_cost) AS employer_cost, min(currency) AS currency,
                                   max(currency) AS currency_max
                            FROM payroll_run_employees WHERE run_id = %(run_id)s""",
    "lines": """SELECT l.component_code, l.kind, l.category, l.amount, l.currency, l.units, l.rate, l.statutory,
                       l.fixture, l.visibility
                FROM payroll_lines l JOIN payroll_run_employees re ON re.id = l.run_employee_id
                WHERE l.run_id = %(run_id)s AND re.employee_id = %(employee_id)s
                ORDER BY l.stage, l.sort_order, l.id""",
    "exceptions": """SELECT code, severity, status, blocks_finalization, employee_id
                     FROM payroll_exceptions WHERE run_id = %(run_id)s ORDER BY id""",
    "approvals": """SELECT a.*
                    FROM payroll_approvals a WHERE a.run_id = %(run_id)s ORDER BY a.id""",
    "obligations": """SELECT status, count(*) AS n, sum(amount) AS amount, min(currency) AS currency
                      FROM payment_obligations WHERE run_id = %(run_id)s GROUP BY status ORDER BY status""",
    "batches_of_run": """SELECT id, company_id, reference, status, method, adapter, submission_channel, item_count,
                                total_amount, currency
                         FROM payment_batches WHERE run_id = %(run_id)s ORDER BY id""",
    "batch": """SELECT id, company_id, reference, run_id, status, method, adapter, submission_channel, item_count,
                       total_amount, currency, prepared_by_user_id, approved_by_user_id, released_by_user_id
                FROM payment_batches WHERE id = %(id)s""",
    "instructions_of_batch": """SELECT id, employee_id, status, evidence_status, evidence_source, submission_channel,
                                       amount, currency, settled_amount, settled_at, reconciled_at
                                FROM payment_instructions WHERE batch_id = %(batch_id)s ORDER BY id""",
    "settled_without_evidence": """SELECT count(*) AS n FROM payment_instructions
                                   WHERE company_id = %(company_id)s AND status IN ('SETTLED', 'RECONCILED')
                                     AND coalesce(evidence_status, '') NOT IN ('VERIFIED', 'CONFIRMED', 'ACCEPTED')""",
    "bank_accounts_of_employee": """SELECT id, company_id, employee_id, status, verification_status, currency, country,
                                           payment_hold, effective_from, replaces_account_id
                                    FROM bank_accounts WHERE employee_id = %(employee_id)s ORDER BY id""",
    "bank_changes_of_employee": """SELECT id, company_id, kind, status, bank_account_id, decided_at
                                   FROM bank_account_change_requests WHERE employee_id = %(employee_id)s ORDER BY id""",
    "documents_of_run": """SELECT id, company_id, doc_type, number, status, employee_id, version, brand_version
                           FROM documents WHERE run_id = %(run_id)s ORDER BY id""",
    "documents_of_employee": """SELECT id, company_id, doc_type, number, status, run_id, version
                                FROM documents WHERE employee_id = %(employee_id)s ORDER BY id""",
    "setting": """SELECT key, scope_type, scope_id, value, locked, effective_from, effective_to, status, row_version
                  FROM setting_values WHERE company_id = %(company_id)s AND key = %(key)s ORDER BY id""",
    "settings_count": """SELECT company_id, count(*) AS n FROM setting_values GROUP BY company_id ORDER BY company_id""",
    "audit": """SELECT module, action, actor_email, description, created_at FROM audit_logs
                WHERE company_id = %(company_id)s AND module = %(module)s ORDER BY id DESC LIMIT 50""",
    "audit_since": """SELECT module, action, actor_email, company_id, created_at FROM audit_logs
                      WHERE created_at >= %(since)s::timestamptz ORDER BY id""",
    "company": """SELECT id, name, slug FROM companies WHERE id = %(id)s""",
    # Isolation: no payroll row references an employee of another company.
    "cross_company_refs": """SELECT
        (SELECT count(*) FROM payroll_run_employees re JOIN employees e ON e.id = re.employee_id
          WHERE e.company_id <> re.company_id) AS run_employees,
        (SELECT count(*) FROM payroll_lines l JOIN payroll_runs r ON r.id = l.run_id
          WHERE r.company_id <> l.company_id) AS lines,
        (SELECT count(*) FROM payment_obligations o JOIN payroll_runs r ON r.id = o.run_id
          WHERE r.company_id <> o.company_id) AS obligations,
        (SELECT count(*) FROM payment_instructions i JOIN employees e ON e.id = i.employee_id
          WHERE e.company_id <> i.company_id) AS instructions,
        (SELECT count(*) FROM bank_accounts b JOIN employees e ON e.id = b.employee_id
          WHERE e.company_id <> b.company_id) AS bank_accounts,
        (SELECT count(*) FROM documents d JOIN employees e ON e.id = d.employee_id
          WHERE e.company_id <> d.company_id) AS documents,
        (SELECT count(*) FROM compensation_assignments a JOIN employees e ON e.id = a.employee_id
          WHERE e.company_id <> a.company_id) AS assignments""",
    # Fingerprint of a company's payroll records: compared before and after attacks (nothing changed).
    "company_fingerprint": """SELECT
        (SELECT count(*) FROM payroll_runs WHERE company_id = %(company_id)s) AS runs,
        (SELECT coalesce(sum(row_version), 0) FROM payroll_runs WHERE company_id = %(company_id)s) AS run_versions,
        (SELECT count(*) FROM salary_templates WHERE company_id = %(company_id)s) AS structures,
        (SELECT count(*) FROM bank_accounts WHERE company_id = %(company_id)s) AS bank_accounts,
        (SELECT coalesce(sum(row_version), 0) FROM bank_accounts WHERE company_id = %(company_id)s) AS bank_versions,
        (SELECT count(*) FROM documents WHERE company_id = %(company_id)s) AS documents,
        (SELECT count(*) FROM payment_batches WHERE company_id = %(company_id)s) AS batches,
        (SELECT count(*) FROM setting_values WHERE company_id = %(company_id)s) AS settings,
        (SELECT coalesce(sum(row_version), 0) FROM setting_values WHERE company_id = %(company_id)s) AS setting_versions,
        (SELECT count(*) FROM compensation_assignments WHERE company_id = %(company_id)s) AS assignments""",
    "structures_of_company": """SELECT id, name, company_id FROM salary_templates WHERE company_id = %(company_id)s
                                ORDER BY id""",
    "payslips_of_company": """SELECT id, employee_id, run_id, number, status FROM documents
                              WHERE company_id = %(company_id)s AND doc_type LIKE 'PAYSLIP%%' ORDER BY id LIMIT 5""",
    "bank_accounts_of_company": """SELECT id, employee_id, status FROM bank_accounts
                                   WHERE company_id = %(company_id)s ORDER BY id LIMIT 5""",
    "employee_by_email":"""SELECT e.id AS employee_id, e.company_id, u.id AS user_id
                            FROM users u JOIN employees e ON e.user_id = u.id WHERE lower(u.email) = lower(%(email)s)""",
}
for _key, _table in OWNED_TABLES.items():
    QUERIES[f"owner_{_key}"] = f"SELECT id, company_id FROM {_table} WHERE id = %(id)s"


def _default(value):
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def main(argv) -> None:
    if not argv or argv[0] not in QUERIES:
        raise SystemExit(f"usage: e2e_payroll_db.py <{'|'.join(sorted(QUERIES))}> [name=value ...]")
    params = dict(arg.split("=", 1) for arg in argv[1:])
    info = PostmasterInfo.read_from_pgdata(DATA_DIR)
    if info is None or not info.is_running():
        raise SystemExit("The payroll E2E database is not running (python scripts/payroll_stack.py serve).")
    with psycopg2.connect(info.get_uri(database="hrms")) as conn:
        conn.set_session(readonly=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(QUERIES[argv[0]], params)
            print(json.dumps(cur.fetchall(), default=_default))


if __name__ == "__main__":
    main(sys.argv[1:])

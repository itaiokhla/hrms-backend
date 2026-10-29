"""Browser-test data for team payroll, contractor and cost invoices on the time stack (local demo cluster only).

    E2E_PG_DATA=.local-postgres-time python scripts/seed_remaining_goals_e2e.py

Idempotent; prints JSON ids. For company alpha of the time seed:

* every active employee without a pay group is placed in ``E2E-<entity code>`` (monthly, the entity's currency) so
  manager-prepared inputs can become wage inputs;
* Nitin (reports to Arjun) is a contractor (employment type CONTRACTOR) with one APPROVED timesheet week (Mon/Tue,
  8 h each) not on a live invoice;
* a FINALIZED run "E2E cost run" (previous month, three of Arjun's reports, BASIC + HRA + employer PF) exists for
  payroll-cost invoices.

Rows are written as finalized results (as the payroll document tests do) — the browser test then drives the real
pages and APIs on top of them.
"""

import json
import os
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg2
import psycopg2.extras
from pgserver.utils import PostmasterInfo

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / os.environ.get("E2E_PG_DATA", ".local-postgres-time")
CREDENTIALS = ROOT / ".local-secrets" / "time-credentials.json"
LINES = (("BASIC", "EARNING", "40000.00"), ("HRA", "EARNING", "16000.00"), ("PF_EMPLOYEE", "DEDUCTION", "1800.00"),
         ("EMPLOYER_PF", "EMPLOYER_CONTRIBUTION", "1800.00"))


def one(cursor, sql, **p):
    cursor.execute(sql, p)
    row = cursor.fetchone()
    return row[0] if row else None


def main() -> None:
    store = json.loads(CREDENTIALS.read_text(encoding="utf-8"))
    alpha = store["companies"]["alpha"]
    cid = alpha["company_id"]
    people = alpha["ids"]["people"]
    info = PostmasterInfo.read_from_pgdata(DATA_DIR)
    if info is None or not info.is_running():
        raise SystemExit("The time stack database is not running.")
    out = {}
    with psycopg2.connect(info.get_uri(database="hrms")) as conn, conn.cursor() as cur:
        # Pay groups for everyone unplaced.
        cur.execute("SELECT a.id, a.legal_entity_id, le.code, le.base_currency FROM employee_assignments a "
                    "JOIN employees e ON e.id = a.employee_id JOIN legal_entities le ON le.id = a.legal_entity_id "
                    "WHERE a.company_id = %(c)s AND a.is_primary AND a.pay_group_id IS NULL AND e.is_active", {"c": cid})
        placed = 0
        for assignment_id, entity_id, code, currency in cur.fetchall():
            group = one(cur, "SELECT id FROM pay_groups WHERE company_id = %(c)s AND code = %(code)s",
                        c=cid, code=f"E2E-{code}")
            if group is None:
                group = one(cur, "INSERT INTO pay_groups (company_id, legal_entity_id, code, name, currency, pay_frequency, "
                                 "calendar_rules, status, created_at, updated_at) VALUES (%(c)s, %(le)s, %(code)s, %(name)s, "
                                 "%(currency)s, 'MONTHLY', '{\"monthly_start_day\": 1}'::jsonb, 'ACTIVE', now(), now()) RETURNING id",
                            c=cid, le=entity_id, code=f"E2E-{code}", name=f"Monthly {code}", currency=currency)
            cur.execute("UPDATE employee_assignments SET pay_group_id = %(g)s WHERE id = %(a)s", {"g": group, "a": assignment_id})
            placed += 1
        out["placed"] = placed

        # Nitin is a contractor with an approved week.
        nitin = people["nitin"]["employee_id"]
        type_id = one(cur, "SELECT id FROM employment_types WHERE company_id = %(c)s AND code = 'CONTRACTOR'", c=cid)
        if type_id is None:
            type_id = one(cur, "INSERT INTO employment_types (company_id, code, name, worker_class, status, created_at, "
                               "updated_at) VALUES (%(c)s, 'CONTRACTOR', 'Contractor', 'CONTRACTOR', 'ACTIVE', now(), now()) "
                               "RETURNING id", c=cid)
        cur.execute("UPDATE employee_assignments SET employment_type_id = %(t)s WHERE employee_id = %(e)s",
                    {"t": type_id, "e": nitin})
        today = date.today()
        monday = today - timedelta(days=today.weekday() + 7)
        while True:  # a week not yet approved-and-invoiced
            sheet = one(cur, "SELECT t.id FROM attendance_timesheet_approvals t WHERE t.company_id = %(c)s AND "
                             "t.employee_id = %(e)s AND t.period_start = %(s)s", c=cid, e=nitin, s=monday)
            live = sheet and one(cur, "SELECT 1 FROM invoice_sources WHERE source_type = 'TIMESHEET' AND source_id = %(s)s "
                                      "AND active", s=sheet)
            if not live:
                break
            monday -= timedelta(days=7)
        if sheet is None:
            sheet = one(cur, "INSERT INTO attendance_timesheet_approvals (company_id, employee_id, period_kind, period_start, "
                             "period_end, status, version, row_version, created_at, updated_at) VALUES (%(c)s, %(e)s, 'WEEK', "
                             "%(s)s, %(t)s, 'APPROVED', 1, 1, now(), now()) RETURNING id",
                        c=cid, e=nitin, s=monday, t=monday + timedelta(days=6))
        else:
            cur.execute("UPDATE attendance_timesheet_approvals SET status = 'APPROVED' WHERE id = %(i)s", {"i": sheet})
        for offset in (0, 1):
            day = monday + timedelta(days=offset)
            row = one(cur, "SELECT id FROM attendance_days WHERE company_id = %(c)s AND employee_id = %(e)s AND shift_date = %(d)s",
                      c=cid, e=nitin, d=day)
            if row is None:
                cur.execute("INSERT INTO attendance_days (company_id, employee_id, shift_date, work_timezone, evaluation_state, "
                            "rules_mode, inputs_hash, content_hash, expected_portion, net_minutes, first_half_result, "
                            "second_half_result, day_result, evaluated_at, trigger, created_at, updated_at) VALUES (%(c)s, "
                            "%(e)s, %(d)s, 'Asia/Kolkata', 'FINAL', 'STANDARD', %(h)s, %(h)s, 'FULL', 480, 'PRESENT', "
                            "'PRESENT', 'PRESENT', now(), 'PUNCH', now(), now())", {"c": cid, "e": nitin, "d": day, "h": "e" * 64})
            else:
                cur.execute("UPDATE attendance_days SET net_minutes = GREATEST(COALESCE(net_minutes, 0), 480) WHERE id = %(i)s",
                            {"i": row})
        out["contractor"] = {"employee_id": nitin, "timesheet_id": sheet, "week": monday.isoformat()}

        # A finalized run for cost invoices.
        run = one(cur, "SELECT id FROM payroll_runs WHERE company_id = %(c)s AND label = 'E2E cost run'", c=cid)
        if run is None:
            staff = [people[k]["employee_id"] for k in ("priya", "rohan", "meghna")]
            cur.execute("SELECT legal_entity_id, pay_group_id, id FROM employee_assignments WHERE employee_id = %(e)s AND is_primary "
                        "ORDER BY id DESC LIMIT 1", {"e": staff[0]})
            entity, group, _ = cur.fetchone()
            currency = one(cur, "SELECT currency FROM pay_groups WHERE id = %(g)s", g=group)
            first = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
            last = today.replace(day=1) - timedelta(days=1)
            calendar = one(cur, "SELECT id FROM pay_calendars WHERE company_id = %(c)s AND pay_group_id = %(g)s", c=cid, g=group)
            if calendar is None:
                calendar = one(cur, "INSERT INTO pay_calendars (company_id, pay_group_id, frequency, rules, effective_from, status, "
                                    "row_version, created_at, updated_at) VALUES (%(c)s, %(g)s, 'MONTHLY', "
                                    "'{\"monthly_start_day\": 1}'::jsonb, '2020-01-01', 'ACTIVE', 1, now(), now()) RETURNING id",
                               c=cid, g=group)
            period = one(cur, "SELECT id FROM pay_periods WHERE company_id = %(c)s AND pay_group_id = %(g)s AND period_start = %(s)s",
                         c=cid, g=group, s=first)
            if period is None:
                period = one(cur, "INSERT INTO pay_periods (company_id, pay_calendar_id, pay_group_id, frequency, period_start, "
                                  "period_end, attendance_start, attendance_end, pay_date, nominal_pay_date, input_cutoff_on, "
                                  "approval_deadline_on, payment_submit_by, tax_year, number_in_tax_year, is_last_in_tax_year, "
                                  "is_transition, status, pay_date_warnings, row_version, created_at, updated_at) VALUES (%(c)s, "
                                  "%(cal)s, %(g)s, 'MONTHLY', %(s)s, %(e)s, %(s)s, %(e)s, %(e)s, %(e)s, %(e)s, %(e)s, %(e)s, "
                                  "'2026-27', 1, false, false, 'CLOSED', '[]'::jsonb, 1, now(), now()) RETURNING id",
                             c=cid, cal=calendar, g=group, s=first, e=last)
            run = one(cur, "INSERT INTO payroll_runs (company_id, number, label, legal_entity_id, pay_group_id, pay_period_id, "
                           "run_type, status, regular_sequence, currency, selection, pack_version_ids, totals, population, stale, "
                           "stale_reasons, environment, row_version, created_at, updated_at, finalized_at) VALUES (%(c)s, "
                           "%(n)s, 'E2E cost run', %(le)s, %(g)s, %(p)s, 'REGULAR', 'APPROVED', 1, %(currency)s, '{}'::jsonb, "
                           "'[]'::jsonb, '{}'::jsonb, '{}'::jsonb, false, '[]'::jsonb, 'test', 1, now(), now(), NULL) RETURNING id",
                      c=cid, n=f"PR-{first.year}-{990000 + first.month:06d}", le=entity, g=group, p=period, currency=currency)
            earnings = sum(Decimal(a) for _, k, a in LINES if k == "EARNING")
            deductions = sum(Decimal(a) for _, k, a in LINES if k == "DEDUCTION")
            employer = sum(Decimal(a) for _, k, a in LINES if k == "EMPLOYER_CONTRIBUTION")
            for employee_id in staff:
                assignment = one(cur, "SELECT id FROM employee_assignments WHERE employee_id = %(e)s AND is_primary ORDER BY id "
                                      "DESC LIMIT 1", e=employee_id)
                row = one(cur, "INSERT INTO payroll_run_employees (company_id, run_id, employee_id, employee_assignment_id, status, "
                               "gross, deductions, reimbursements, net, employer_cost, currency, flags, row_version, created_at, "
                               "updated_at) VALUES (%(c)s, %(r)s, %(e)s, %(a)s, 'CALCULATED', %(g)s, %(d)s, 0, %(n)s, %(ec)s, "
                               "%(currency)s, '[]'::jsonb, 1, now(), now()) RETURNING id",
                          c=cid, r=run, e=employee_id, a=assignment, g=earnings, d=deductions, n=earnings - deductions,
                          ec=earnings + employer, currency=currency)
                for order, (code, kind, amount) in enumerate(LINES):
                    cur.execute("INSERT INTO payroll_lines (company_id, run_id, run_employee_id, component_code, kind, stage, "
                                "amount, currency, tags, visibility, sources, trace, sort_order, statutory, fixture, created_at, "
                                "updated_at) VALUES (%(c)s, %(r)s, %(re)s, %(code)s, %(k)s, 4, %(a)s, %(currency)s, '[]'::jsonb, "
                                "'EMPLOYEE_VISIBLE', '[]'::jsonb, '{}'::jsonb, %(o)s, %(st)s, false, now(), now())",
                                {"c": cid, "r": run, "re": row, "code": code, "k": kind, "a": Decimal(amount), "currency": currency,
                                 "o": order, "st": code.endswith("PF") or code.startswith("PF")})
            cur.execute("UPDATE payroll_runs SET status = 'FINALIZED', finalized_at = now() WHERE id = %(r)s", {"r": run})
        out["cost_run_id"] = run
        conn.commit()
    print(json.dumps(out))


if __name__ == "__main__":
    main()

"""Read-only database assertions over the seeded three-company payroll dataset (Goal 2 §4-§5).

    python scripts/payroll_stack.py start
    python scripts/seed_payroll_three_companies.py
    python scripts/verify_payroll_seed.py                 # the payroll stack's database (.local-postgres-payroll)
    python scripts/verify_payroll_seed.py --database-url postgresql://...  [--app-database-url ...]

Everything runs in READ ONLY transactions. The owner connection counts what the seed left per company
(organisation, people, roles and scopes, compensation, inputs, banking, attestation, runs); the RLS-restricted
application role (``hrms_app``) proves isolation: with ``app.company_id`` set to one company it sees only that
company's rows of every checked table. The expected shape comes from ``seed_payroll_three_companies.COMPANIES``,
so the dataset and its checks cannot drift apart. Prints one line per check (no names of people, no bank data,
no secrets) and exits 1 when a check fails.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from seed_payroll_three_companies import COMPANIES  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CENT = Decimal("0.01")
FINALIZED = ("FINALIZED", "PAYMENT_PENDING", "PARTIALLY_PAID", "PAID", "RECONCILED", "CLOSED")
# Tenant tables whose isolation is checked through the application role (row-level security).
ISOLATED_TABLES = ("employees", "users", "legal_entities", "branches", "pay_groups", "departments",
                   "salary_templates", "compensation_assignments", "payroll_runs", "payroll_run_employees",
                   "payroll_lines", "wage_inputs", "reimbursement_claims", "loans", "bank_accounts",
                   "bank_account_change_requests", "funding_accounts", "statutory_attestations", "tasks",
                   "overtime_entries", "leaves", "documents", "payment_obligations", "setting_values")


@dataclass
class Result:
    company: str
    name: str
    ok: bool
    detail: str = ""


class Checker:
    def __init__(self, conn) -> None:
        from sqlalchemy import text

        self.conn = conn
        self._text = text
        self.results: List[Result] = []

    def q(self, sql: str, **params):
        return self.conn.execute(self._text(sql), params).fetchall()

    def one(self, sql: str, **params):
        rows = self.q(sql, **params)
        return rows[0][0] if rows else None

    def check(self, company: str, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append(Result(company, name, bool(ok), detail))
        return bool(ok)


def _email(spec: dict, first: str, last: str) -> str:
    import re

    return f"{first.lower()}.{re.sub(r'[^a-z]', '', last.lower())}@{spec['domain']}"


def check_company(c: Checker, key: str, spec: dict) -> Optional[int]:
    name = spec["name"]
    ids = [r[0] for r in c.q("SELECT id FROM companies WHERE name = :n", n=name)]
    if not c.check(name, "company exists exactly once", len(ids) == 1, f"{len(ids)} found"):
        return None
    cid = ids[0]
    row = c.q("SELECT currency, timezone FROM companies WHERE id = :c", c=cid)[0]
    c.check(name, "currency and timezone", tuple(row) == (spec["currency"], spec["timezone"]), f"{tuple(row)}")
    brand = c.one("SELECT product_name FROM brand_profiles WHERE company_id = :c AND status = 'PUBLISHED' "
                  "AND owner_type = 'TENANT' ORDER BY version DESC LIMIT 1", c=cid)
    c.check(name, "published brand", brand == spec["brand"]["product_name"], f"{brand!r}")

    # Organisation
    entities = {code: (cc, confirmed, cur) for code, cc, confirmed, cur in c.q(
        "SELECT code, country_code, country_confirmed, base_currency FROM legal_entities WHERE company_id = :c",
        c=cid)}
    for entity in [spec["entity"], *spec["extra_entities"]]:
        found = entities.get(entity["code"])
        c.check(name, f"legal entity {entity['code']} ({entity['country_code']}, confirmed)",
                found == (entity["country_code"], True, entity["base_currency"]), f"{found}")
    branches = {code: (sub, le) for code, sub, le in c.q(
        "SELECT b.code, b.subdivision_code, l.code FROM branches b JOIN legal_entities l ON l.id = b.legal_entity_id "
        "WHERE b.company_id = :c", c=cid)}
    for _bkey, entity_code, _bname, code, region, _tz, _week in spec["branches"]:
        c.check(name, f"branch {code} in {region} under {entity_code}", branches.get(code) == (region, entity_code),
                f"{branches.get(code)}")
    groups = {code: (cur, freq, le) for code, cur, freq, le in c.q(
        "SELECT g.code, g.currency, g.pay_frequency, l.code FROM pay_groups g JOIN legal_entities l "
        "ON l.id = g.legal_entity_id WHERE g.company_id = :c", c=cid)}
    for _gkey, entity_code, code, _gname, currency, frequency, _rules, _primary in spec["pay_groups"]:
        c.check(name, f"pay group {code} ({frequency}, {currency})", groups.get(code) == (currency, frequency,
                                                                                         entity_code),
                f"{groups.get(code)}")
        periods = c.one("SELECT count(*) FROM pay_periods p JOIN pay_groups g ON g.id = p.pay_group_id "
                        "WHERE g.company_id = :c AND g.code = :g", c=cid, g=code)
        c.check(name, f"pay periods of {code}", (periods or 0) >= 2, f"{periods}")
    departments = {n: parent for n, parent in c.q(
        "SELECT d.name, p.name FROM departments d LEFT JOIN departments p ON p.id = d.parent_department_id "
        "WHERE d.company_id = :c", c=cid)}
    c.check(name, ">= 2 departments", sum(1 for d in spec["departments"] if d in departments) >= 2)
    for team, parent in spec["teams"]:
        c.check(name, f"team {team} under {parent}", departments.get(team) == parent, f"{departments.get(team)}")
    for week_code, _wname, offs in spec.get("work_weeks", []):
        linked = c.one("SELECT count(*) FROM branches b JOIN work_week_patterns w ON w.id = b.work_week_pattern_id "
                       "WHERE b.company_id = :c AND w.code = :w", c=cid, w=week_code)
        c.check(name, f"work week {week_code} linked to branches", (linked or 0) >= 1, f"{linked}")

    # People, managers, logins and roles
    people = {p[0]: p for p in spec["people"]}
    emails = {pkey: _email(spec, p[1], p[2]) for pkey, p in people.items()}
    found = {e: (i, mgr, term, hire, uid) for e, i, mgr, term, hire, uid in c.q(
        "SELECT lower(email), id, manager_id, termination_date, hire_date, user_id FROM employees "
        "WHERE company_id = :c", c=cid)}
    c.check(name, "every seeded employee exists", all(emails[k] in found for k in people),
            f"{sum(emails[k] in found for k in people)}/{len(people)}")
    c.check(name, ">= 6 employees besides the founder", len(found) - 1 >= 6, f"{len(found) - 1}")
    by_id = {v[0]: v for v in found.values()}
    depth = 0
    for record in found.values():
        level, cursor = 0, record
        while cursor[1] is not None and cursor[1] in by_id and level < 10:
            level, cursor = level + 1, by_id[cursor[1]]
        depth = max(depth, level)
    c.check(name, ">= 2 manager levels above an employee", depth >= 3, f"deepest chain {depth}")
    roles_of: Dict[str, set] = {}
    for email_address, key_or_name in c.q(
            "SELECT lower(u.email), coalesce(r.system_key, r.name) FROM users u JOIN user_role_assignments a "
            "ON a.user_id = u.id AND a.company_id = u.company_id JOIN roles r ON r.id = a.role_id "
            "WHERE u.company_id = :c AND (a.effective_until IS NULL OR a.effective_until > now())", c=cid):
        roles_of.setdefault(email_address, set()).add(key_or_name)
    admin_roles = roles_of.get(f"admin@{spec['domain']}", set())
    c.check(name, "founder: company_admin + payroll_manager", {"company_admin", "payroll_manager"} <= admin_roles,
            f"{sorted(admin_roles)}")
    for pkey, person in people.items():
        wanted = set(person[3])
        if wanted:
            have = roles_of.get(emails[pkey], set())
            c.check(name, f"login {pkey} holds {sorted(wanted)}", wanted == have, f"{sorted(have)}")
    for label, pkey in spec["accounts"].items():
        if pkey != "admin":
            c.check(name, f"account '{label}' has a login", emails[pkey] in roles_of)
    for pkey, person in people.items():
        scope_entity = person[10].get("scope_entity")
        if scope_entity:
            targets = [r[0] for r in c.q(
                "SELECT l.code FROM user_role_assignment_targets t JOIN user_role_assignments a "
                "ON a.id = t.user_role_assignment_id JOIN users u ON u.id = a.user_id "
                "JOIN legal_entities l ON l.id = t.target_id AND t.scope = 'LEGAL_ENTITY' "
                "WHERE u.company_id = :c AND lower(u.email) = :e", c=cid, e=emails[pkey])]
            c.check(name, f"{pkey} payroll scope = LEGAL_ENTITY {scope_entity}", targets == [scope_entity],
                    f"{targets}")
        if person[10].get("no_comp_visibility"):
            grants = [r[0] for r in c.q(
                "SELECT rp.permission_code FROM role_permissions rp JOIN user_role_assignments a "
                "ON a.role_id = rp.role_id JOIN users u ON u.id = a.user_id WHERE u.company_id = :c "
                "AND lower(u.email) = :e AND rp.permission_code LIKE 'payroll:%' AND rp.scope <> 'SELF'", c=cid,
                e=emails[pkey])]
            c.check(name, f"{pkey} has no payroll grant beyond self-service (no compensation visibility)",
                    grants == [], f"{grants}")
    for role_name, grants in spec["custom_roles"].items():
        have = {(p, s) for p, s in c.q(
            "SELECT rp.permission_code, rp.scope FROM role_permissions rp JOIN roles r ON r.id = rp.role_id "
            "WHERE r.company_id = :c AND r.name = :n", c=cid, n=role_name)}
        c.check(name, f"custom role {role_name}", have == set(grants), f"{len(have)} grants")

    # Compensation
    templates = {code for (code,) in c.q("SELECT code FROM salary_templates WHERE company_id = :c", c=cid)}
    c.check(name, ">= 2 salary structures", all(s["code"] in templates for s in spec["structures"])
            and len(spec["structures"]) >= 2, f"{sorted(templates)}")
    in_force = {e for (e,) in c.q(
        "SELECT DISTINCT lower(e.email) FROM compensation_assignments a JOIN employees e ON e.id = a.employee_id "
        "WHERE a.company_id = :c AND a.status IN ('ACTIVE', 'SCHEDULED', 'SUPERSEDED')", c=cid)}
    with_salary = {emails[k] for k, p in people.items() if p[8]}
    without = {emails[k] for k, p in people.items() if not p[8]}
    c.check(name, "every salaried person has an approved salary", with_salary <= in_force,
            f"{len(with_salary & in_force)}/{len(with_salary)}")
    c.check(name, "people without a salary assignment by design", bool(without) and not (without & in_force),
            f"{len(without)} without")
    for pkey, person in people.items():
        fte = person[10].get("fte")
        if fte:
            value = c.one("SELECT a.fte FROM employee_assignments a JOIN employees e ON e.id = a.employee_id "
                          "WHERE e.company_id = :c AND lower(e.email) = :e AND a.is_primary "
                          "AND a.effective_to IS NULL", c=cid, e=emails[pkey])
            c.check(name, f"{pkey} part-time FTE {fte}", value is not None and float(value) == float(fte), f"{value}")

    # Inputs (approved source records only)
    def employee_rows(sql: str, pkey: str, **params):
        return c.q(sql, c=cid, e=emails[pkey], **params)

    for pkey, person in people.items():
        extras = person[10]
        if extras.get("leave"):
            n = employee_rows("SELECT count(*) FROM leaves l JOIN employees e ON e.id = l.employee_id WHERE "
                              "l.company_id = :c AND lower(e.email) = :e AND l.status = 'approved' "
                              "AND l.leave_type <> 'unpaid'", pkey)[0][0]
            c.check(name, f"{pkey} approved leave", n >= 1, f"{n}")
        if extras.get("lop_day"):
            n = employee_rows("SELECT count(*) FROM leaves l JOIN employees e ON e.id = l.employee_id WHERE "
                              "l.company_id = :c AND lower(e.email) = :e AND l.status = 'approved' "
                              "AND l.leave_type = 'unpaid'", pkey)[0][0]
            c.check(name, f"{pkey} approved unpaid leave (LOP)", n >= 1, f"{n}")
        if extras.get("overtime"):
            n = employee_rows("SELECT count(*) FROM overtime_entries o JOIN employees e ON e.id = o.employee_id "
                              "WHERE o.company_id = :c AND lower(e.email) = :e AND o.status IN "
                              "('APPROVED', 'CONSUMED', 'PAID')", pkey)[0][0]
            c.check(name, f"{pkey} approved overtime", n >= 1, f"{n}")
        for extra, code in (("bonus", "BONUS"), ("commission", "COMMISSION"), ("trip_pay", "TRIP_PAY")):
            if extras.get(extra):
                n = employee_rows("SELECT count(*) FROM wage_inputs w JOIN employees e ON e.id = w.employee_id "
                                  "WHERE w.company_id = :c AND lower(e.email) = :e AND w.component_code = :k AND "
                                  "w.status IN ('APPROVED', 'RESERVED', 'CONSUMED')", pkey, k=code)[0][0]
                c.check(name, f"{pkey} approved {code} input", n >= 1, f"{n}")
        if extras.get("claim"):
            n = employee_rows("SELECT count(*) FROM reimbursement_claims r JOIN employees e ON e.id = r.employee_id "
                              "WHERE r.company_id = :c AND lower(e.email) = :e AND r.status IN "
                              "('APPROVED', 'SCHEDULED', 'PAID')", pkey)[0][0]
            c.check(name, f"{pkey} approved reimbursement", n >= 1, f"{n}")
        for extra, kind in (("advance", "ADVANCE"), ("loan", "LOAN")):
            if extras.get(extra):
                rows = employee_rows(
                    "SELECT l.status, l.principal, l.tenure, l.disbursed_on IS NOT NULL, (SELECT array_agg(t.amount) "
                    "FROM loan_transactions t JOIN payroll_run_employees re ON re.id = t.run_employee_id "
                    "JOIN payroll_runs r ON r.id = re.run_id WHERE t.loan_id = l.id AND t.company_id = l.company_id "
                    "AND t.kind = 'INSTALMENT' AND t.amount > 0 AND r.status IN :f) FROM loans l JOIN employees e "
                    "ON e.id = l.employee_id WHERE l.company_id = :c AND lower(e.email) = :e AND l.kind = :k",
                    pkey, k=kind, f=FINALIZED)
                c.check(name, f"{pkey} {kind.lower()} disbursed and outstanding (ACTIVE)",
                        any(r[0] == "ACTIVE" and r[3] for r in rows), f"{[(r[0], r[3]) for r in rows]}")
                # Equal instalments without interest (company policy: interest 0): principal / tenure, e.g.
                # 15,000.00 / 2 = 7,500.00 (advance over two runs) and 6,000.00 / 6 = 1,000.00 (loan).
                expected = (Decimal(extras[extra]) / Decimal(2 if kind == "ADVANCE" else 6)).quantize(CENT)
                recovered = [Decimal(a).quantize(CENT) for r in rows for a in (r[4] or [])]
                c.check(name, f"{pkey} {kind.lower()} instalment {expected} recovered by a finalized run",
                        recovered == [expected], f"{recovered}")
        if extras.get("task"):
            n = employee_rows("SELECT count(*) FROM tasks t JOIN task_assignees a ON a.task_id = t.id JOIN employees e "
                              "ON e.id = a.employee_id WHERE t.company_id = :c AND lower(e.email) = :e AND t.status "
                              "IN ('APPROVED', 'PAID')", pkey)[0][0]
            c.check(name, f"{pkey} approved task", n >= 1, f"{n}")
        if extras.get("leaver"):
            c.check(name, f"{pkey} mid-period leaver", found.get(emails[pkey], (0, 0, None))[2] is not None)
        if extras.get("retro"):
            n = employee_rows("SELECT count(*) FROM compensation_assignments a JOIN employees e ON e.id = a.employee_id "
                              "WHERE a.company_id = :c AND lower(e.email) = :e AND a.reason = 'CORRECTION' AND "
                              "a.status IN ('ACTIVE', 'SUPERSEDED', 'SCHEDULED')", pkey)[0][0]
            c.check(name, f"{pkey} approved backdated salary correction", n >= 1, f"{n}")
        bank = extras.get("bank")
        if bank:
            rows = employee_rows("SELECT b.status, b.verification_status, b.bank_name FROM bank_accounts b "
                                 "JOIN employees e ON e.id = b.employee_id WHERE b.company_id = :c AND "
                                 "lower(e.email) = :e", pkey)
            c.check(name, f"{pkey} verified bank account (test data)",
                    any(s == "ACTIVE" and v == "VERIFIED" for s, v, _ in rows), f"{[(s, v) for s, v, _ in rows]}")
            c.check(name, f"{pkey} bank details labelled fictitious", all("fictitious" in (n or "") for *_, n in rows))
            if bank == "pending_change":
                n = employee_rows("SELECT count(*) FROM bank_account_change_requests r JOIN employees e "
                                  "ON e.id = r.employee_id WHERE r.company_id = :c AND lower(e.email) = :e AND "
                                  "r.status LIKE 'PENDING%'", pkey)[0][0]
                c.check(name, f"{pkey} pending bank change", n >= 1, f"{n}")

    check_amounts(c, cid, spec, emails)

    # Statutory attestation (I-15): made by one administrator, approved by another
    if spec.get("statutory_attestation"):
        rows = c.q("SELECT attested_by_user_id, approved_by_user_id FROM statutory_attestations "
                   "WHERE company_id = :c AND status = 'ACTIVE'", c=cid)
        c.check(name, "ACTIVE attestation, maker != checker",
                bool(rows) and all(a and b and a != b for a, b in rows), f"{len(rows)} active")
    # Funding accounts: one per legal entity with a signatory
    funding = {code: sig for code, sig in c.q(
        "SELECT l.code, f.signatory_user_ids FROM funding_accounts f JOIN legal_entities l ON l.id = f.legal_entity_id "
        "WHERE f.company_id = :c AND f.status = 'ACTIVE'", c=cid)}
    for entity in [spec["entity"], *spec["extra_entities"]]:
        c.check(name, f"funding account of {entity['code']} with a signatory", bool(funding.get(entity["code"])))

    # Runs: finalized through the chain (approver != preparer) and an open draft
    runs = c.q("SELECT r.status, r.run_type, g.code, r.created_by_user_id, r.finalized_by_user_id, "
               "(SELECT array_agg(DISTINCT a.approver_user_id) FROM payroll_approvals a WHERE a.run_id = r.id "
               "AND a.company_id = r.company_id AND a.decision = 'APPROVED' AND a.cleared_at IS NULL) "
               "FROM payroll_runs r JOIN pay_groups g "
               "ON g.id = r.pay_group_id WHERE r.company_id = :c", c=cid)
    finalized = [r for r in runs if r[0] in FINALIZED and r[1] == "REGULAR"]
    wanted_final = 2 if spec.get("previous_run") else 1
    c.check(name, f">= {wanted_final} finalized regular run(s)", len(finalized) >= wanted_final,
            f"{[(r[0], r[2]) for r in runs]}")
    c.check(name, "finalized runs approved by someone other than the preparer",
            all(r[5] and r[3] not in r[5] for r in finalized), f"{[(r[3], r[5]) for r in finalized]}")
    c.check(name, "an open draft run", any(r[0] == "DRAFT" for r in runs))
    nets = c.q("SELECT count(*), count(*) FILTER (WHERE e.net IS NOT NULL) FROM payroll_run_employees e "
               "JOIN payroll_runs r ON r.id = e.run_id WHERE r.company_id = :c AND r.status IN :f "
               "AND e.status = 'CALCULATED'", c=cid, f=FINALIZED) if finalized else [(0, 0)]
    c.check(name, "finalized run employees carry a net", nets[0][0] >= 1 and nets[0][0] == nets[0][1], f"{nets[0]}")
    return cid


def _half_up(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def check_amounts(c: Checker, cid: int, spec: dict, emails: Dict[str, str]) -> None:
    """The approved inputs reached the anchor period's finalized REGULAR run with the amounts worked out here by
    hand from the dataset (never by the engine): inputs and claims pay their amount, the task pays quantity x
    rate card, overtime pays hours x hourly base x the company's workday multiplier, one unpaid day prorates
    the LOP-applicable salary by paid days / calendar days, and the IN fixture withholds tax on a high salary."""
    name = spec["name"]
    period = c.q("SELECT p.period_start, p.period_end FROM payroll_runs r JOIN pay_periods p ON p.id = r.pay_period_id "
                 "JOIN pay_groups g ON g.id = r.pay_group_id WHERE r.company_id = :c AND r.run_type = 'REGULAR' AND "
                 "r.status IN :f AND g.code = :g ORDER BY p.period_start DESC LIMIT 1", c=cid, f=FINALIZED,
                 g=next(g[2] for g in spec["pay_groups"] if g[0] == spec["finalize_group"]))
    if not c.check(name, "anchor period has a finalized regular run", bool(period)):
        return
    start, end = period[0]
    days = Decimal((end - start).days + 1)  # calendar days: the starter proration basis (CALENDAR_DAYS, I-13)

    def lines(pkey: str) -> Dict[str, Decimal]:
        rows = c.q("SELECT l.component_code, sum(l.amount) FROM payroll_lines l JOIN payroll_run_employees e "
                   "ON e.id = l.run_employee_id JOIN payroll_runs r ON r.id = e.run_id JOIN pay_periods p "
                   "ON p.id = r.pay_period_id JOIN employees m ON m.id = e.employee_id WHERE r.company_id = :c "
                   "AND r.run_type = 'REGULAR' AND r.status IN :f AND p.period_start = :s AND lower(m.email) = :e "
                   "GROUP BY l.component_code", c=cid, f=FINALIZED, s=start, e=emails[pkey])
        return {code: _half_up(Decimal(amount)) for code, amount in rows}

    workday = next(Decimal(b["multiplier"]) for b in spec["policies"]["OVERTIME_POLICY"]["buckets"]
                   if b["day_type"] == "WORKDAY")
    for pkey, person in {p[0]: p for p in spec["people"]}.items():
        extras, structure, basis = person[10], person[8], person[9]
        paid = lines(pkey)
        for extra, code in (("bonus", "BONUS"), ("commission", "COMMISSION"), ("trip_pay", "TRIP_PAY")):
            if extras.get(extra):
                c.check(name, f"{pkey} {code} {extras[extra]} paid in the finalized run",
                        paid.get(code) == Decimal(extras[extra]), f"{paid.get(code)}")
        if extras.get("claim"):
            c.check(name, f"{pkey} reimbursement {extras['claim']} paid in the finalized run",
                    paid.get("REIMBURSEMENT") == Decimal(extras["claim"]), f"{paid.get('REIMBURSEMENT')}")
        if extras.get("task"):
            # 6 reviews x the CODE_REVIEW rate card 45.00 = 270.00
            quantity, rate = Decimal("6"), Decimal(dict(spec["rate_cards"])["CODE_REVIEW"])
            c.check(name, f"{pkey} task pay {quantity * rate} paid", paid.get("TASK_EARNING") == quantity * rate,
                    f"{paid.get('TASK_EARNING')}")
        if extras.get("overtime"):
            # 120 approved minutes = 2 hours at the WORKDAY multiplier. Hourly base: an hourly worker's own rate
            # (the run precedes the retro correction); salaried: BASIC (the only OT_BASE component) / calendar
            # days / 8 standard hours, e.g. Alpha 50,000 / 31 / 8 x 1.5 x 2 = 604.84.
            if structure == "hourly":
                hourly = Decimal(basis)
            else:
                monthly_basic = (Decimal(basis) / 12 / 2) if spec["country"] == "IN" else Decimal(extras["split"]["BASIC"])
                hourly = monthly_basic / days / Decimal(8)
            expected = _half_up(Decimal(2) * hourly * workday)
            c.check(name, f"{pkey} overtime 2h x {workday} = {expected}", paid.get("OT_WORKDAY") == expected,
                    f"{paid.get('OT_WORKDAY')}")
        if extras.get("lop_day"):
            # One approved unpaid day: every LOP_APPLICABLE line earns (days - 1) / days of its monthly rate.
            # CTC_BASED: BASIC = CTC / 12 x 50%, HRA = 40% of BASIC, FIXED_ALLOWANCE = the balance.
            monthly = Decimal(basis) / 12
            basic, hra = monthly / 2, monthly / 2 * Decimal("0.4")
            rates = {"BASIC": basic, "HRA": hra, "FIXED_ALLOWANCE": monthly - basic - hra}
            for code, rate in rates.items():
                expected = _half_up(rate * (days - 1) / days)
                c.check(name, f"{pkey} {code} prorated for 1 LOP day = {expected}", paid.get(code) == expected,
                        f"{paid.get(code)}")
    if spec["country"] == "IN":
        # The highest salary (Engineering manager, CTC 25,20,000) is far above the fixture's rebate limit
        # (12,00,000): the IN fixture pack must withhold income tax (FIXTURE - not law), never zero.
        top = max((p for p in spec["people"] if p[8] == "ctc"), key=lambda p: Decimal(p[9]))
        c.check(name, f"{top[0]} income tax withheld (IN fixture)", (lines(top[0]).get("IN_TDS") or 0) > 0,
                f"{lines(top[0]).get('IN_TDS')}")


def check_cross_company(c: Checker, companies: Dict[str, int]) -> None:
    domains = {key: COMPANIES[key]["domain"] for key in companies}
    for key, cid in companies.items():
        for other, domain in domains.items():
            if other == key:
                continue
            leaked = c.one("SELECT count(*) FROM users WHERE company_id = :c AND lower(email) LIKE :d", c=cid,
                           d=f"%@{domain}")
            c.check(COMPANIES[key]["name"], f"no {COMPANIES[other]['name']} login inside", leaked == 0, f"{leaked}")


def check_rls(c: Checker, app_conn_factory: Callable[[], object], companies: Dict[str, int]) -> None:
    """As the application role (RLS enforced) with app.company_id = one company: only its rows are visible."""
    from sqlalchemy import text

    for key, cid in companies.items():
        conn = app_conn_factory()
        try:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text("SELECT set_config('app.company_id', :cid, true)"), {"cid": str(cid)})
            for table in ISOLATED_TABLES:
                seen, foreign = conn.execute(text(
                    f"SELECT count(*), count(*) FILTER (WHERE company_id <> :c) FROM {table}"), {"c": cid}).one()
                total = c.one(f"SELECT count(*) FROM {table} WHERE company_id = :c", c=cid)
                c.check(COMPANIES[key]["name"], f"RLS {table}: own rows only", foreign == 0 and seen == total,
                        f"visible {seen}, own {total}, foreign {foreign}")
        finally:
            conn.rollback()
            conn.close()


def run_checks(conn, app_conn_factory: Optional[Callable[[], object]] = None,
               companies: Optional[Iterable[str]] = None) -> List[Result]:
    """All checks on an owner/system connection (read only); RLS checks when an app-role factory is given."""
    from sqlalchemy import text

    conn.execute(text("SET TRANSACTION READ ONLY"))
    checker = Checker(conn)
    ids: Dict[str, int] = {}
    for key in companies or COMPANIES:
        cid = check_company(checker, key, COMPANIES[key])
        if cid is not None:
            ids[key] = cid
    check_cross_company(checker, ids)
    if app_conn_factory is not None:
        check_rls(checker, app_conn_factory, ids)
    conn.rollback()
    return checker.results


def _stack_urls() -> tuple:
    import pgserver
    from sqlalchemy.engine import make_url

    data_dir = ROOT / ".local-postgres-payroll"
    if not (data_dir / "postmaster.pid").exists():
        raise SystemExit("The payroll stack's PostgreSQL is not running: python scripts/payroll_stack.py start")
    base = make_url(pgserver.get_server(str(data_dir), cleanup_mode=None).get_uri()).set(database="hrms")
    owner = base.render_as_string(hide_password=False)
    password_file = data_dir / ".app_password"
    app = base.set(username="hrms_app", password=password_file.read_text().strip()).render_as_string(
        hide_password=False) if password_file.exists() else None
    return owner, app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", help="owner/system URL (default: the payroll stack's database)")
    parser.add_argument("--app-database-url", help="RLS-restricted application role URL for the isolation checks")
    parser.add_argument("--company", action="append", choices=list(COMPANIES))
    args = parser.parse_args()
    from sqlalchemy import create_engine

    owner_url, app_url = (args.database_url, args.app_database_url) if args.database_url else _stack_urls()
    if ":8000" in owner_url or ".local-postgres/" in owner_url:
        raise SystemExit("Refusing the user's demo database.")
    owner = create_engine(owner_url)
    app_engine = create_engine(app_url) if app_url else None
    try:
        with owner.connect() as conn:
            results = run_checks(conn, app_engine.connect if app_engine else None, args.company)
    finally:
        owner.dispose()
        if app_engine is not None:
            app_engine.dispose()
    failed = [r for r in results if not r.ok]
    for r in results:
        print(f"[{'ok' if r.ok else 'FAIL'}] {r.company}: {r.name}" + (f" ({r.detail})" if not r.ok and r.detail else ""))
    print(f"[verify-payroll-seed] {len(results) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

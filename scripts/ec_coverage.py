"""Edge-case coverage map (Goal 2 §21): every EC id of docs/payroll/implementation/edge-cases.json -> the
automated tests that name it, or NOT COVERED with the reason.

A test "names" an id when the id appears in the test's name (``test_ec_sec_05_...`` -> EC-SEC-05) or in the
docstring of the test function (or of the test class around it). Module docstrings and comments do not count:
they describe a file, not an assertion. Frontend tests (Jest ``*.test.ts(x)`` and Playwright ``*.spec.ts``)
count when the id appears in the title string of a ``test(...)`` / ``it(...)`` call.

Pure static scan: no imports of the application, no database. Run from anywhere:

    venv/Scripts/python.exe scripts/ec_coverage.py            # writes docs/payroll/implementation/EC-COVERAGE.md
    venv/Scripts/python.exe scripts/ec_coverage.py --check    # exit 1 when a critical id (SEC/PAY/BNK/RUN) is uncovered
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
FRONTEND = REPO / "hrms-frontend"
EDGE_CASES = REPO / "docs" / "payroll" / "implementation" / "edge-cases.json"
OUTPUT = REPO / "docs" / "payroll" / "implementation" / "EC-COVERAGE.md"

LEVELS = OrderedDict([
    ("U", "unit"), ("G", "golden vector"), ("P", "property-based"), ("I", "integration (PostgreSQL)"),
    ("A", "authorization negative"), ("E", "Chrome end-to-end"), ("M", "manual / staging"),
])
# Areas whose cases the release gate treats as critical (Goal 2 §21: security/financial need backend assertions).
CRITICAL_AREAS = ("EC-SEC", "EC-PAY", "EC-BNK", "EC-RUN")

ID_IN_TEXT = re.compile(r"\bEC-([A-Z]{2,4})-(\d{2})\b")
ID_IN_NAME = re.compile(r"(?:^|_)ec_([a-z]{2,4})_(\d{2})(?=_|$)")
JS_TEST_TITLE = re.compile(r"""\b(?:test|it|describe)(?:\.\w+)?\(\s*(['"`])(.*?)\1""", re.S)

# Ids the implementation recorded as not implemented or deliberately out of automated scope, with the
# reason (FOLLOW-UPS.md / CHECKLIST.md). Anything else uncovered gets the generic reason of its test level.
KNOWN_GAPS = {
    "EC-ATT-11": "Not implemented: branch timezone for day assignment and lateness (FOLLOW-UPS F-40).",
    "EC-HOL-07": "Not implemented: comp-off expiry beyond the pure rules (FOLLOW-UPS F-40).",
    "EC-HOL-01": "Not implemented: holiday-on-weekly-off substitution beyond the pure rules (FOLLOW-UPS F-40).",
    # Critical-area gaps, checked against the code on 23 Oct 2026 (security verification agent).
    "EC-BNK-15": "Partial: the NOMINEE payee type exists (app/models/payments.py PAYEE_TYPES) and a death case pays a "
                 "named nominee through final settlement (tests/payroll/test_final_settlement.py::test_ec_cor_07_"
                 "death_pays_a_named_nominee_and_ec_cor_12_absconding_is_held); on the bank-account side NOMINEE is "
                 "reserved (app/payroll/services/bank_accounts.py docstring): a payee change to a nominee's own "
                 "account with documents and approval is not implemented, so no test can name the id.",
    "EC-PAY-19": "UNCLAIMED exists only as an obligation status (app/models/payment_flows.py); no country pack defines "
                 "an unclaimed period and no job moves obligations to it (packs ship as DRAFT, decision I-07). "
                 "Manual/staging per 04-edge-cases.",
    "EC-SEC-16": "Not implemented: documents carry retention_until (app/models/pay_documents.py) but there is no "
                 "legal-hold flag and no retention-deletion job to conflict with it. Manual/staging per 04-edge-cases.",
    # Checked against the code on 24 Oct 2026 (gate-ec): applicable but not implemented, or only partly.
    "EC-WL-23": "Not implemented: there is no custom sending domain; every e-mail leaves from the platform sender "
                "(settings.smtp_from) with the tenant's display name and Reply-To (app/services/email_service.py), so "
                "there is no domain verification that could lapse.",
    "EC-ORG-05": "Partial: redenomination is not supported. After the entity's first finalized run (legacy or v2) "
                 "the base currency is locked (409 ORG_CURRENCY_LOCKED, tests/payroll/verification/test_edge_cases_gap"
                 ".py::test_ec_org_04_a_finalized_v2_run_locks_the_entity_country_and_currency); an effective-dated "
                 "currency change with new periods in the new currency does not exist.",
    "EC-ORG-08": "Partial: org.allow_concurrent_assignments exists, but the ON behaviour (separate runs and payslips per "
                 "assignment) is not implemented: change_placement always refuses a non-primary placement "
                 "(ORG_CONCURRENT_DISABLED, asserted in tests/test_org_documents.py), whatever the setting.",
    "EC-ORG-16": "Not implemented: partners have no operator access into a tenant (the partner console shows aggregate "
                 "counts only, tests/test_partner_admins.py), so a partner cannot prepare or approve a run; the "
                 "separation of duties for tenant users is EC-RUN-05.",
    "EC-ORG-17": "Not implemented: there are no partner access grants into a tenant (no grant model, no expiry), so no "
                 "grant can expire under a running job.",
    "EC-ORG-18": "Not implemented: there are no partner access grants to revoke; the closest control (a suspended "
                 "partner or a re-scoped partner account loses its session at once) is asserted in tests/"
                 "test_partner_admins.py::test_suspended_partner_and_rescoped_accounts_lose_access_at_once.",
    "EC-ORG-19": "Not implemented: there is no platform support-access grant (approval, time limit, masked payroll). "
                 "Access without approval is impossible today because the platform principal reaches no tenant HR "
                 "route at all (EC-ORG-20, tests/test_tenant_isolation.py::test_platform_admin_manages_tenants_without"
                 "_hr_data).",
    "EC-ORG-24": "Not implemented: a placement carries one cost center (employee_assignments.cost_center_id); "
                 "percentage splits across cost centers do not exist, so there is no 100.00% total to validate.",
    "EC-CFG-21": "Not implemented as specified: the impact preview of a past-dated change is the synchronous arrears "
                 "detection with preview=true (POST /api/v1/payroll/arrears-proposals/detect); there is no background "
                 "job with progress for a large tenant.",
    "EC-STA-06": "Pack-defined: no verified country pack has tax regimes (the IN pack is a FIXTURE and AE/US are DRAFT, "
                 "decision I-07; the fictional ZZ pack has none), so whether a mid-year regime change is allowed and how "
                 "the projection is recomputed cannot be asserted without inventing a statutory rule.",
    "EC-COR-10": "Pack-defined: which tax year arrears belong to and any relief come from the country pack; no verified "
                 "pack defines them (decision I-07). The neutral part (arrears count in the YTD of the tax year of the "
                 "paying period) is asserted by test_ec_sta_22 in tests/payroll/verification/test_edge_cases_gap.py.",
    "EC-COR-16": "No single test walks one run through cancel, reverse, correct, reissue and partial settlement. Each "
                 "link is asserted on its own: reversal links and history (tests/payroll/test_reversals.py), "
                 "payment-only reissue referencing the original lines (tests/payroll/test_off_cycle.py), arrears runs "
                 "(tests/payroll/test_retro.py), PARTIALLY_PAID settlement (tests/payroll/test_payment_batches.py).",
    "EC-MIG-09": "The blank-location backfill (one 'Main' branch, country_confirmed = false) is migration 0005's "
                 "upgrade step; the test database is created at head, where later guards (0008 pay-group guard) refuse "
                 "its raw inserts, so it can only be exercised by a rehearsal from a pre-0005 database. The "
                 "unconfirmed-country banner of new tenants is EC-ORG-23.",
    "EC-MIG-11": "Not reached: the legacy payroll routes are deliberately still live for existing records (decisions "
                 "I-03 and I-11; tests/test_route_shadowing.py keeps v2 routes ahead of them); the read-only stage and "
                 "410 Gone have not been scheduled.",
    "EC-REL-06": "Not implemented: payslip e-mail delivery is recorded per document (document_notifications SENT / "
                 "FAILED, with retry), but there is no provider bounce feedback and no bouncing-address flag on users.",
    "EC-REL-08": "Not implemented as specified: imports are capped (wage inputs 5,000 rows / 2 MB, "
                 "app/payroll/services/wage_inputs.py MAX_IMPORT_ROWS) with per-row errors; a streamed 50,000-row "
                 "import does not exist.",
}

# Browser-only checks (no backend assertion is possible), specified for the Chrome stage (gate-ec, 24 Oct 2026).
CHROME_CHECKS = {
    "EC-WL-02": "Tenant with only a light logo; switch to dark mode: the sidebar and top bar show the light logo on a "
                "white rounded chip (container background white, border-radius > 0), no broken image.",
    "EC-WL-13": "Display name of 120 characters with emoji and an Arabic company name: the sidebar truncates with an "
                "ellipsis and shows the full name in a tooltip; the payslip PDF shows the full legal name; the "
                "Arabic name renders right-to-left.",
    "EC-UX-01": "User A on the pay runs page; an admin removes A's payroll role in another browser; on window focus "
                "(or within the 5-minute poll) A's actions disappear or AccessDenied shows; an action clicked before "
                "that gets 403 with a toast and changes nothing.",
    "EC-UX-02": "Role without payroll:run:read opens /payroll/runs: AccessDenied page; forbidden buttons are absent "
                "(not just disabled); a run id of another tenant in the URL shows the not-found view (API 404).",
    "EC-UX-04": "Two tabs edit the same setting or structure; save in tab 1, then in tab 2: a 409 dialog with the "
                "difference and Reload; Reload shows tab 1's value.",
    "EC-UX-05": "Start a calculation (or a report export) and reload the page mid-way: the progress resumes from the "
                "same job id and completes once.",
    "EC-UX-06": "While a run is open, change one of its inputs from another session: a stale banner appears with a "
                "one-click Refresh that reloads the figures.",
    "EC-UX-09": "Tenant locale ar: html dir=rtl, sidebar on the right, layout mirrored; amounts and currency codes "
                "stay left-to-right (money cells dir=ltr or bdi).",
    "EC-UX-10": "375x812 viewport: run list, run review, My payslips and My bank account work end to end; tables "
                "render as cards; no horizontal page scroll.",
    "EC-UX-11": "Toggle 'Hide amounts': every amount including KPI cards is blurred; hover or keyboard focus reveals "
                "one value; the toggle survives navigation.",
    "EC-UX-12": "Keyboard only (Tab, Enter, Space, Esc): approve a run including the step-up dialog, and change a bank "
                "account including its confirmation, with visible focus and focus trapped in dialogs.",
    "EC-UX-13": "Tenant with no employees vs a tenant whose employees have no salary: two different empty states with "
                "their next action; 'Create run' disabled with the reason.",
    "EC-UX-14": "Run list, run review and payslips each show: loading skeleton, long-running progress, partial "
                "failure with Retry, access denied and stale banner (force each with route interception).",
    "EC-MIG-12": "Pages that used fmtMoney (dashboard, reports, legacy payslips, employee drawer) render amounts with "
                 "the currency's decimals (JPY 0, BHD 3, INR 2), never NaN or a bare number.",
    "EC-MIG-13": "Visit /salary-structure and /payslips: each lands on the new route and renders. Note: next.config "
                 "sends /salary-structure to /payroll?tab=structures, which the '/payroll with ?tab' rule then sends to "
                 "/payroll/legacy, and /payslips is still a (legacy) page; confirm the intended destinations.",
}
LEVEL_REASON = {
    "E": "Browser-only check, not in the Chrome suite of milestone 16 (29 tests, FOLLOW-UPS F-63); no backend "
         "assertion is possible.",
    "M": "Manual or staging check (real DNS/e-mail/bank/provider); automation impossible in the test environment.",
}
AREA_REASON = {
    "EC-WL": "No white-label test names this id yet.",
    "EC-ORG": "No organisation test names this id yet.",
    "EC-UX": "UI state: verified only in Chrome.",
    "EC-COR": "No corrections test names this id yet.",
    "EC-STA": "Real country packs ship as DRAFT (decision I-07); only the fictional ZZ pack is verified, so "
              "pack-specific vectors wait for a qualified reviewer.",
    "EC-MIG": "Migration cases need the legacy-data rehearsal of the regression milestone (CHECKLIST #17).",
    "EC-REL": "Reliability and performance cases need load or fault-injection runs on an isolated stack, which the "
              "unit and integration suites do not perform.",
}


@dataclass
class Hit:
    file: str
    name: str
    level: str  # backend-db | backend-pure | frontend-jest | frontend-e2e


@dataclass
class Coverage:
    hits: dict = field(default_factory=dict)  # id -> list[Hit]
    unknown: dict = field(default_factory=dict)  # id named by tests but absent from edge-cases.json
    files_scanned: int = 0


def _ids_from_name(name: str) -> set:
    return {f"EC-{a.upper()}-{n}" for a, n in ID_IN_NAME.findall(name.lower())}


def _ids_from_text(text: str | None) -> set:
    return {f"EC-{a}-{n}" for a, n in ID_IN_TEXT.findall(text or "")}


def _python_level(path: Path) -> str:
    return "backend-pure" if "payroll_unit" in path.parts else "backend-db"


def _scan_python(path: Path, cov: Coverage, known: set) -> None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return
    rel = path.relative_to(REPO).as_posix()
    level = _python_level(path)

    def visit(nodes, class_doc_ids=frozenset(), prefix=""):
        for node in nodes:
            if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                visit(node.body, frozenset(_ids_from_text(ast.get_docstring(node))) | _ids_from_name(node.name),
                      f"{node.name}::")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                ids = _ids_from_name(node.name) | _ids_from_text(ast.get_docstring(node)) | set(class_doc_ids)
                for ec in ids:
                    target = cov.hits if ec in known else cov.unknown
                    target.setdefault(ec, []).append(Hit(rel, prefix + node.name, level))

    visit(tree.body)


def _scan_js(path: Path, cov: Coverage, known: set) -> None:
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return
    rel = path.relative_to(REPO).as_posix()
    level = "frontend-e2e" if path.name.endswith(".spec.ts") else "frontend-jest"
    for _, title in JS_TEST_TITLE.findall(source):
        for ec in _ids_from_text(title):
            target = cov.hits if ec in known else cov.unknown
            target.setdefault(ec, []).append(Hit(rel, title.strip()[:120], level))


def scan(known: set) -> Coverage:
    cov = Coverage()
    for path in sorted((BACKEND / "tests").rglob("test_*.py")):
        cov.files_scanned += 1
        _scan_python(path, cov, known)
    if FRONTEND.exists():
        roots = [p for p in FRONTEND.iterdir() if p.is_dir() and (p.name == "src" or p.name.startswith("e2e"))]
        for root in roots:
            for path in sorted(root.rglob("*")):
                if "node_modules" in path.parts or not path.is_file():
                    continue
                if path.name.endswith((".test.ts", ".test.tsx", ".spec.ts")):
                    cov.files_scanned += 1
                    _scan_js(path, cov, known)
    return cov


def _levels(raw: str) -> list:
    return [x.strip() for x in raw.split(",") if x.strip() in LEVELS]


def _reason(case: dict) -> str:
    if case["id"] in KNOWN_GAPS:
        return KNOWN_GAPS[case["id"]]
    if case["id"] in CHROME_CHECKS:
        return "Browser-only check for the Chrome stage (no backend assertion possible): " + CHROME_CHECKS[case["id"]]
    levels = _levels(case["test"])
    if levels and all(lv in ("E", "M") for lv in levels):
        return " ".join(LEVEL_REASON[lv] for lv in dict.fromkeys(levels))
    area = AREA_REASON.get(case["area"])
    wanted = "/".join(levels) or case["test"]
    base = f"No automated test names this id yet (required level: {wanted})."
    return f"{base} {area}" if area else base


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render(cases: list, cov: Coverage) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    areas: "OrderedDict[str, list]" = OrderedDict()
    for case in cases:
        areas.setdefault(case["area"], []).append(case)
    total = len(cases)
    covered = sum(1 for c in cases if c["id"] in cov.hits)
    out = [
        "# Payroll edge-case coverage map",
        "",
        f"Generated by `hrms-backend/scripts/ec_coverage.py` on {now} — do not edit by hand; re-run the script.",
        "",
        "Source: `docs/payroll/implementation/edge-cases.json` (" + str(total) + " ids). A test covers an id when "
        "the id is in the test's name or docstring (backend pytest) or in a Jest/Playwright test title. Module "
        "docstrings and comments are not counted. Coverage means a test *names* the case; whether it passes is "
        "the job of the test run.",
        "",
        f"**Covered: {covered} / {total}** · not covered: {total - covered} · test files scanned: "
        f"{cov.files_scanned}.",
        "",
        "Test levels (04-edge-cases.md): U unit · G golden vector · P property-based · I integration · "
        "A authorization negative · E Chrome end-to-end · M manual/staging. Found levels: backend-db "
        "(PostgreSQL pytest), backend-pure (`tests/payroll_unit`), frontend-jest, frontend-e2e (Playwright).",
        "",
        "## Summary per area",
        "",
        "| Area | Section | Ids | Covered | Not covered | Required levels (ids) | Found test levels (tests) |",
        "|---|---|---|---|---|---|---|",
    ]
    for area, items in areas.items():
        req = Counter(lv for c in items for lv in _levels(c["test"]))
        found = Counter(h.level for c in items for h in cov.hits.get(c["id"], []))
        n_cov = sum(1 for c in items if c["id"] in cov.hits)
        out.append(
            f"| {area} | {_md_escape(items[0]['section'])} | {len(items)} | {n_cov} | {len(items) - n_cov} | "
            + ", ".join(f"{k} {v}" for k, v in sorted(req.items(), key=lambda kv: list(LEVELS).index(kv[0])))
            + " | " + (", ".join(f"{k} {v}" for k, v in sorted(found.items())) or "—") + " |"
        )
    out += ["", "## Summary per required test level", "",
            "An id that requires several levels counts once under each. \"Covered\" = some test names the id "
            "(at any level).", "",
            "| Level | Ids requiring it | Covered | Not covered |", "|---|---|---|---|"]
    for code, label in LEVELS.items():
        wanting = [c for c in cases if code in _levels(c["test"])]
        n_cov = sum(1 for c in wanting if c["id"] in cov.hits)
        out.append(f"| {code} {label} | {len(wanting)} | {n_cov} | {len(wanting) - n_cov} |")
    found_levels = Counter(h.level for hits in cov.hits.values() for h in hits)
    out += ["", "Tests naming an id, by found level: "
            + (", ".join(f"{k} {v}" for k, v in sorted(found_levels.items())) or "none") + "."]
    crit = [c for c in cases if c["area"] in CRITICAL_AREAS and c["id"] not in cov.hits]
    out += ["", "## Critical areas without a backend assertion", ""]
    if crit:
        out.append("Security, payments, bank accounts and pay runs (" + ", ".join(CRITICAL_AREAS) + "):")
        out.append("")
        for c in crit:
            out.append(f"- **{c['id']}** {_md_escape(c['scenario'])} — {_md_escape(_reason(c))}")
    else:
        out.append("None: every id of " + ", ".join(CRITICAL_AREAS) + " is named by at least one test.")
    for area, items in areas.items():
        out += ["", f"## {area} — {items[0]['section']}", "", "| Id | Scenario | Level | Tests |", "|---|---|---|---|"]
        for c in items:
            hits = cov.hits.get(c["id"], [])
            if hits:
                seen, cells = set(), []
                for h in hits:
                    key = (h.file, h.name)
                    if key in seen:
                        continue
                    seen.add(key)
                    cells.append(f"`{h.file}::{_md_escape(h.name)}` ({h.level})")
                tests = "<br>".join(cells)
            else:
                tests = f"**NOT COVERED** — {_md_escape(_reason(c))}"
            out.append(f"| {c['id']} | {_md_escape(c['scenario'])} | {_md_escape(c['test'])} | {tests} |")
    if cov.unknown:
        out += ["", "## Ids named by tests but missing from edge-cases.json", ""]
        for ec, hits in sorted(cov.unknown.items()):
            out.append(f"- {ec}: " + ", ".join(f"`{h.file}::{h.name}`" for h in hits[:5])
                       + (f" (+{len(hits) - 5} more)" if len(hits) > 5 else ""))
    out.append("")
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--check", action="store_true", help="exit 1 when a critical-area id is not covered")
    args = parser.parse_args(argv)
    cases = json.loads(EDGE_CASES.read_text(encoding="utf-8"))
    known = {c["id"] for c in cases}
    cov = scan(known)
    args.output.write_text(render(cases, cov), encoding="utf-8")
    covered = sum(1 for c in cases if c["id"] in cov.hits)
    missing_critical = [c["id"] for c in cases if c["area"] in CRITICAL_AREAS and c["id"] not in cov.hits]
    print(f"{covered}/{len(cases)} edge cases covered; {len(missing_critical)} critical uncovered; "
          f"wrote {args.output}")
    return 1 if args.check and missing_critical else 0


if __name__ == "__main__":
    sys.exit(main())

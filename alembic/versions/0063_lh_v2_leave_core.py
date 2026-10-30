"""Leave v2 core schema (LH-M2..LH-M7; SPINE §4, 11 §4): leave type catalogue, employee lifecycle dates, request
lifecycle columns, ledger v2 reasons and scopes, and the new leave tables.

CHANGED tables:
* ``leave_types`` — ``category``, ``unit``, ``sensitivity``, ``absence_class``, ``counts_toward_absence_score``,
  ``description``, ``name_i18n``, ``color_token``, ``icon``, ``display_order``, ``row_version``; ``kind`` adds
  FLOATING_HOLIDAY and EVENT. Backfill per 03-leave/01 §3.11 (seed codes → categories, colour tokens in id order).
* ``employees`` — ``probation_end_date``, ``confirmation_date``, ``notice_start_date``, ``service_start_date``.
* ``leaves`` — statuses add ``draft``, ``returned`` and ``withdrawn`` (the stored vocabulary stays lowercase so every
  payroll and attendance reader keeps working, IG-LH-33); lifecycle columns (source, version, plan version, flags,
  sensitivity snapshot, emergency, unpaid sibling, event, timestamps, row_version).
* ``leave_days`` — ``charge_reason`` and ``counting_basis`` (what decided that a date is charged).
* ``leave_balance_transactions`` — ``reason`` widened (OPENING, GRANT, PRORATE, CARRY_FORWARD, COMP_OFF, LAPSE,
  PENALTY join ACCRUAL, USE, ENCASH, ADJUST, EXPIRE); ``cycle_start``, ``cycle_end``, ``source``,
  ``policy_version_id``. The table stays append-only (trigger of 0016).

NEW tenant tables (RLS ``tenant_isolation``, unique ``(company_id, id)``, composite same-company FKs, grants):
``leave_change_requests``, ``leave_blackouts``, ``leave_coverage_rules``, ``leave_reason_codes``,
``leave_year_end_runs``, ``leave_imports``, ``leave_events``, ``comp_off_claims``, ``leave_encashment_requests``,
``leave_messages``, ``leave_attachments``, ``leave_attachment_access_log``, ``leave_document_requests``.

Scheduled jobs (DAILY): ``leave.accrue`` (credits and expiry), ``leave.sla_scan`` (approval reminders, returned
expiry, overdue documents).

Downgrade refuses once v2 rows exist (new ledger reasons, change requests, claims or encashments).

Revision ID: 0063_lh_v2_leave_core
Revises: 0062_roster_publish_approval
Create Date: 2026-10-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0063_lh_v2_leave_core"
down_revision: Union[str, None] = "0062_roster_publish_approval"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
BASE = ("id SERIAL PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE RESTRICT, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()")
# Frozen copies (later model changes must not alter what this revision does).
OLD_REASONS = ("ACCRUAL", "USE", "ENCASH", "ADJUST", "EXPIRE")
REASONS = OLD_REASONS + ("OPENING", "GRANT", "PRORATE", "CARRY_FORWARD", "COMP_OFF", "LAPSE", "PENALTY")
OLD_STATUSES = ("pending", "approved", "rejected", "cancelled")
STATUSES = OLD_STATUSES + ("draft", "returned", "withdrawn")
CATEGORIES = ("ANNUAL", "CASUAL", "SICK", "PARENTAL", "BEREAVEMENT", "MARRIAGE", "STUDY", "CIVIC", "SABBATICAL",
              "COMP_OFF", "FLOATING", "UNPAID", "OTHER")
SOURCES = ("SELF", "ON_BEHALF", "HR_RECORDED", "SYSTEM", "AGENT", "IMPORT", "BULK")
ATTACHMENT_CATEGORIES = ("MEDICAL_CERTIFICATE", "FIT_TO_WORK", "PARENTAL_EVIDENCE", "BEREAVEMENT_EVIDENCE",
                         "MARRIAGE_EVIDENCE", "OFFICIAL_SUMMONS", "STUDY_EVIDENCE", "TRAVEL_DOCUMENT",
                         "SUPPORTING_DOCUMENT")


def _in(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _fk(table: str, column: str, target: str, name: str, on_delete: str = "RESTRICT") -> str:
    return (f"CONSTRAINT fk_{table}_{name} FOREIGN KEY (company_id, {column}) "
            f"REFERENCES {target} (company_id, id) ON DELETE {on_delete}")


TABLES = {
    "leave_change_requests": f"""
        {BASE}, leave_id INTEGER NOT NULL, employee_id INTEGER NOT NULL, kind VARCHAR(16) NOT NULL,
        status VARCHAR(10) NOT NULL DEFAULT 'PENDING', from_version INTEGER NOT NULL DEFAULT 1,
        proposed JSONB NOT NULL DEFAULT '{{}}'::jsonb, reason VARCHAR(1000), reason_code VARCHAR(40),
        initiator VARCHAR(10) NOT NULL DEFAULT 'EMPLOYEE', requested_by_user_id INTEGER, decided_by_user_id INTEGER,
        decided_at TIMESTAMPTZ, decision_note VARCHAR(1000), acknowledged_at TIMESTAMPTZ, applied_at TIMESTAMPTZ,
        units_delta NUMERIC(6, 2) NOT NULL DEFAULT 0, row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_leave_change_requests_company_id_id UNIQUE (company_id, id),
        {_fk('leave_change_requests', 'leave_id', 'leaves', 'leave')},
        {_fk('leave_change_requests', 'employee_id', 'employees', 'employee')},
        CONSTRAINT ck_leave_change_requests_kind CHECK (kind IN
            ('CANCEL', 'PARTIAL_CANCEL', 'MODIFY', 'RECALL', 'CONVERT')),
        CONSTRAINT ck_leave_change_requests_status CHECK (status IN
            ('PENDING', 'APPROVED', 'REJECTED', 'WITHDRAWN')),
        CONSTRAINT ck_leave_change_requests_initiator CHECK (initiator IN ('EMPLOYEE', 'EMPLOYER', 'SYSTEM'))""",
    "leave_blackouts": f"""
        {BASE}, name VARCHAR(100) NOT NULL, from_date DATE NOT NULL, to_date DATE NOT NULL, legal_entity_id INTEGER,
        branch_ids JSONB, department_ids JSONB, leave_type_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
        mode VARCHAR(5) NOT NULL DEFAULT 'BLOCK', reason VARCHAR(500), status VARCHAR(10) NOT NULL DEFAULT 'ACTIVE',
        created_by_user_id INTEGER, row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_leave_blackouts_company_id_id UNIQUE (company_id, id),
        {_fk('leave_blackouts', 'legal_entity_id', 'legal_entities', 'entity')},
        CONSTRAINT ck_leave_blackouts_dates CHECK (to_date >= from_date),
        CONSTRAINT ck_leave_blackouts_mode CHECK (mode IN ('WARN', 'BLOCK')),
        CONSTRAINT ck_leave_blackouts_status CHECK (status IN ('ACTIVE', 'ARCHIVED'))""",
    "leave_coverage_rules": f"""
        {BASE}, name VARCHAR(100) NOT NULL, department_id INTEGER, branch_id INTEGER, team_id INTEGER,
        max_absent_count INTEGER, max_absent_pct NUMERIC(5, 2), count_pending BOOLEAN NOT NULL DEFAULT true,
        mode VARCHAR(5) NOT NULL DEFAULT 'WARN', status VARCHAR(10) NOT NULL DEFAULT 'ACTIVE',
        created_by_user_id INTEGER, row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_leave_coverage_rules_company_id_id UNIQUE (company_id, id),
        {_fk('leave_coverage_rules', 'department_id', 'departments', 'department')},
        {_fk('leave_coverage_rules', 'branch_id', 'branches', 'branch')},
        {_fk('leave_coverage_rules', 'team_id', 'teams', 'team')},
        CONSTRAINT ck_leave_coverage_rules_group CHECK (num_nonnulls(department_id, branch_id, team_id) = 1),
        CONSTRAINT ck_leave_coverage_rules_limit CHECK ((max_absent_count IS NULL) <> (max_absent_pct IS NULL)),
        CONSTRAINT ck_leave_coverage_rules_count CHECK (max_absent_count IS NULL OR max_absent_count >= 0),
        CONSTRAINT ck_leave_coverage_rules_pct CHECK (max_absent_pct IS NULL OR
            (max_absent_pct > 0 AND max_absent_pct <= 100)),
        CONSTRAINT ck_leave_coverage_rules_mode CHECK (mode IN ('WARN', 'BLOCK')),
        CONSTRAINT ck_leave_coverage_rules_status CHECK (status IN ('ACTIVE', 'ARCHIVED'))""",
    "leave_reason_codes": f"""
        {BASE}, code VARCHAR(40) NOT NULL, label VARCHAR(100) NOT NULL, applies_to VARCHAR(10) NOT NULL,
        counts_as_refused BOOLEAN NOT NULL DEFAULT false, is_active BOOLEAN NOT NULL DEFAULT true,
        CONSTRAINT uq_leave_reason_codes_company_id_id UNIQUE (company_id, id),
        CONSTRAINT uq_leave_reason_codes_code UNIQUE (company_id, applies_to, code),
        CONSTRAINT ck_leave_reason_codes_applies CHECK (applies_to IN
            ('REJECT', 'RETURN', 'CANCEL', 'RECALL', 'ADJUST', 'OVERRIDE'))""",
    "leave_year_end_runs": f"""
        {BASE}, legal_entity_id INTEGER, cycle_end DATE NOT NULL, status VARCHAR(10) NOT NULL DEFAULT 'DRAFT',
        summary JSONB NOT NULL DEFAULT '{{}}'::jsonb, rows JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_by_user_id INTEGER, posted_by_user_id INTEGER, posted_at TIMESTAMPTZ, reversed_by_user_id INTEGER,
        reversed_at TIMESTAMPTZ, row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_leave_year_end_runs_company_id_id UNIQUE (company_id, id),
        {_fk('leave_year_end_runs', 'legal_entity_id', 'legal_entities', 'entity')},
        CONSTRAINT ck_leave_year_end_runs_status CHECK (status IN ('DRAFT', 'POSTED', 'REVERSED'))""",
    "leave_imports": f"""
        {BASE}, kind VARCHAR(24) NOT NULL, status VARCHAR(10) NOT NULL DEFAULT 'DRY_RUN', file_name VARCHAR(200),
        report JSONB NOT NULL DEFAULT '{{}}'::jsonb, rows JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_by_user_id INTEGER, committed_by_user_id INTEGER, committed_at TIMESTAMPTZ,
        expires_at TIMESTAMPTZ NOT NULL,
        CONSTRAINT uq_leave_imports_company_id_id UNIQUE (company_id, id),
        CONSTRAINT ck_leave_imports_kind CHECK (kind IN ('OPENING_BALANCES', 'BALANCE_ADJUSTMENTS', 'LEAVE_HISTORY')),
        CONSTRAINT ck_leave_imports_status CHECK (status IN ('DRY_RUN', 'COMMITTED', 'FAILED', 'EXPIRED'))""",
    "leave_events": f"""
        {BASE}, employee_id INTEGER NOT NULL, leave_type_code VARCHAR(30) NOT NULL, event_type VARCHAR(30) NOT NULL,
        event_date DATE NOT NULL, entitled_units NUMERIC(6, 2) NOT NULL, used_units NUMERIC(6, 2) NOT NULL DEFAULT 0,
        take_until DATE, status VARCHAR(10) NOT NULL DEFAULT 'OPEN', note VARCHAR(500), created_by_user_id INTEGER,
        row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_leave_events_company_id_id UNIQUE (company_id, id),
        {_fk('leave_events', 'employee_id', 'employees', 'employee')},
        CONSTRAINT ck_leave_events_status CHECK (status IN ('OPEN', 'CLOSED', 'CANCELLED')),
        CONSTRAINT ck_leave_events_units CHECK (entitled_units > 0 AND used_units >= 0)""",
    "comp_off_claims": f"""
        {BASE}, employee_id INTEGER NOT NULL, worked_date DATE NOT NULL, source VARCHAR(20) NOT NULL,
        worked_minutes INTEGER NOT NULL, credit_units NUMERIC(4, 2) NOT NULL, leave_type_code VARCHAR(30) NOT NULL,
        status VARCHAR(10) NOT NULL DEFAULT 'PENDING', reason VARCHAR(1000), expires_on DATE,
        requested_by_user_id INTEGER, decided_by_user_id INTEGER, decided_at TIMESTAMPTZ, decision_note VARCHAR(1000),
        current_step INTEGER, row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_comp_off_claims_company_id_id UNIQUE (company_id, id),
        {_fk('comp_off_claims', 'employee_id', 'employees', 'employee')},
        CONSTRAINT ck_comp_off_claims_source CHECK (source IN
            ('HOLIDAY_WORK', 'WEEKLY_OFF_WORK', 'OVERTIME', 'SUBSTITUTE_HOLIDAY', 'RECALL', 'MANUAL')),
        CONSTRAINT ck_comp_off_claims_status CHECK (status IN
            ('PENDING', 'APPROVED', 'REJECTED', 'WITHDRAWN', 'REVOKED')),
        CONSTRAINT ck_comp_off_claims_units CHECK (credit_units > 0 AND worked_minutes >= 0)""",
    "leave_encashment_requests": f"""
        {BASE}, employee_id INTEGER NOT NULL, leave_type_code VARCHAR(30) NOT NULL, cycle_start DATE,
        units NUMERIC(6, 2) NOT NULL, trigger VARCHAR(10) NOT NULL DEFAULT 'ON_REQUEST',
        status VARCHAR(10) NOT NULL DEFAULT 'PENDING', reason VARCHAR(1000), requested_by_user_id INTEGER,
        decided_by_user_id INTEGER, decided_at TIMESTAMPTZ, decision_note VARCHAR(1000), paid_at TIMESTAMPTZ,
        payroll_reference VARCHAR(100), year_end_run_id INTEGER, row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_leave_encashment_requests_company_id_id UNIQUE (company_id, id),
        {_fk('leave_encashment_requests', 'employee_id', 'employees', 'employee')},
        {_fk('leave_encashment_requests', 'year_end_run_id', 'leave_year_end_runs', 'year_end_run')},
        CONSTRAINT ck_leave_encashment_requests_trigger CHECK (trigger IN ('EXIT', 'YEAR_END', 'ON_REQUEST')),
        CONSTRAINT ck_leave_encashment_requests_status CHECK (status IN
            ('PENDING', 'APPROVED', 'REJECTED', 'WITHDRAWN', 'PAID', 'CANCELLED')),
        CONSTRAINT ck_leave_encashment_requests_units CHECK (units > 0)""",
    "leave_messages": f"""
        {BASE}, leave_id INTEGER NOT NULL, author_user_id INTEGER, kind VARCHAR(20) NOT NULL DEFAULT 'COMMENT',
        visibility VARCHAR(14) NOT NULL DEFAULT 'PARTICIPANTS', body TEXT NOT NULL, reason_code VARCHAR(40),
        details JSONB,
        CONSTRAINT uq_leave_messages_company_id_id UNIQUE (company_id, id),
        {_fk('leave_messages', 'leave_id', 'leaves', 'leave')},
        CONSTRAINT ck_leave_messages_kind CHECK (kind IN ('COMMENT', 'DECISION_NOTE', 'SYSTEM_EVENT',
            'DOCUMENT_REQUEST', 'SUGGESTED_CHANGE', 'INTERNAL_NOTE')),
        CONSTRAINT ck_leave_messages_visibility CHECK (visibility IN ('PARTICIPANTS', 'APPROVERS_HR')),
        CONSTRAINT ck_leave_messages_body CHECK (char_length(body) BETWEEN 1 AND 4000)""",
    "leave_attachments": f"""
        {BASE}, employee_id INTEGER NOT NULL, leave_id INTEGER, category VARCHAR(24) NOT NULL,
        sensitivity VARCHAR(8) NOT NULL DEFAULT 'GENERAL', storage_path VARCHAR(400) NOT NULL, sha256 VARCHAR(64) NOT NULL,
        mime VARCHAR(100) NOT NULL, bytes INTEGER NOT NULL, original_name VARCHAR(200) NOT NULL,
        scan_status VARCHAR(12) NOT NULL DEFAULT 'NOT_SCANNED', verification_status VARCHAR(10) NOT NULL DEFAULT 'SUBMITTED',
        verified_by_user_id INTEGER, verified_at TIMESTAMPTZ, rejection_reason VARCHAR(500),
        uploaded_by_user_id INTEGER, deleted_at TIMESTAMPTZ,
        CONSTRAINT uq_leave_attachments_company_id_id UNIQUE (company_id, id),
        {_fk('leave_attachments', 'employee_id', 'employees', 'employee')},
        {_fk('leave_attachments', 'leave_id', 'leaves', 'leave')},
        CONSTRAINT ck_leave_attachments_category CHECK (category IN {_in(ATTACHMENT_CATEGORIES)}),
        CONSTRAINT ck_leave_attachments_sensitivity CHECK (sensitivity IN ('GENERAL', 'MEDICAL')),
        CONSTRAINT ck_leave_attachments_scan CHECK (scan_status IN
            ('PENDING_SCAN', 'CLEAN', 'INFECTED', 'SCAN_FAILED', 'NOT_SCANNED')),
        CONSTRAINT ck_leave_attachments_verification CHECK (verification_status IN
            ('SUBMITTED', 'VERIFIED', 'REJECTED')),
        CONSTRAINT ck_leave_attachments_bytes CHECK (bytes > 0)""",
    "leave_attachment_access_log": f"""
        {BASE}, attachment_id INTEGER NOT NULL, user_id INTEGER, action VARCHAR(10) NOT NULL,
        ip_address VARCHAR(64),
        CONSTRAINT uq_leave_attachment_access_log_company_id_id UNIQUE (company_id, id),
        {_fk('leave_attachment_access_log', 'attachment_id', 'leave_attachments', 'attachment')},
        CONSTRAINT ck_leave_attachment_access_log_action CHECK (action IN ('VIEW', 'DOWNLOAD'))""",
    "leave_document_requests": f"""
        {BASE}, leave_id INTEGER NOT NULL, category VARCHAR(24) NOT NULL, due_date DATE NOT NULL,
        status VARCHAR(10) NOT NULL DEFAULT 'OPEN', note VARCHAR(500), requested_by_user_id INTEGER,
        fulfilled_attachment_id INTEGER, closed_at TIMESTAMPTZ,
        CONSTRAINT uq_leave_document_requests_company_id_id UNIQUE (company_id, id),
        {_fk('leave_document_requests', 'leave_id', 'leaves', 'leave')},
        {_fk('leave_document_requests', 'fulfilled_attachment_id', 'leave_attachments', 'attachment')},
        CONSTRAINT ck_leave_document_requests_category CHECK (category IN {_in(ATTACHMENT_CATEGORIES)}),
        CONSTRAINT ck_leave_document_requests_status CHECK (status IN ('OPEN', 'FULFILLED', 'WAIVED', 'OVERDUE'))""",
}
INDEXES = (
    "CREATE UNIQUE INDEX uq_leave_change_requests_pending ON leave_change_requests (company_id, leave_id) "
    "WHERE status = 'PENDING'",
    "CREATE INDEX ix_leave_change_requests_employee ON leave_change_requests (company_id, employee_id, status)",
    "CREATE INDEX ix_leave_blackouts_dates ON leave_blackouts (company_id, from_date, to_date) WHERE status = 'ACTIVE'",
    "CREATE INDEX ix_leave_events_employee ON leave_events (company_id, employee_id, leave_type_code)",
    "CREATE UNIQUE INDEX uq_comp_off_claims_active ON comp_off_claims (company_id, employee_id, worked_date) "
    "WHERE status IN ('PENDING', 'APPROVED')",
    "CREATE INDEX ix_leave_encashment_requests_employee ON leave_encashment_requests "
    "(company_id, employee_id, leave_type_code, status)",
    "CREATE INDEX ix_leave_messages_leave ON leave_messages (company_id, leave_id, id)",
    "CREATE INDEX ix_leave_attachments_leave ON leave_attachments (company_id, leave_id)",
    "CREATE INDEX ix_leave_document_requests_leave ON leave_document_requests (company_id, leave_id, status)",
    "CREATE INDEX ix_leaves_employee_status ON leaves (company_id, employee_id, status)",
    "CREATE INDEX ix_leave_balance_transactions_cycle ON leave_balance_transactions "
    "(company_id, employee_id, leave_type, cycle_start)",
)
SCHEDULED = (("leave.accrue", "DAILY", 35), ("leave.sla_scan", "HOURLY", 17))
# 03-leave/01 §3.3 defaults per category: (sensitivity, absence_class, icon).
CATEGORY_DEFAULTS = {
    "ANNUAL": ("STANDARD", "PLANNED", "sun"), "CASUAL": ("STANDARD", "PLANNED", "coffee"),
    "SICK": ("SENSITIVE", "PROTECTED", "heart-pulse"), "PARENTAL": ("STANDARD", "STATUTORY", "baby"),
    "BEREAVEMENT": ("SENSITIVE", "PROTECTED", "flower-2"), "MARRIAGE": ("STANDARD", "PLANNED", "gem"),
    "STUDY": ("STANDARD", "PLANNED", "graduation-cap"), "CIVIC": ("STANDARD", "STATUTORY", "landmark"),
    "SABBATICAL": ("STANDARD", "PLANNED", "plane"), "COMP_OFF": ("STANDARD", "PLANNED", "rotate-ccw"),
    "FLOATING": ("STANDARD", "PLANNED", "calendar-plus"), "UNPAID": ("STANDARD", "UNPLANNED", "calendar-x"),
    "OTHER": ("STANDARD", "PLANNED", "tag"),
}
SEED_CATEGORIES = {"casual": "CASUAL", "sick": "SICK", "earned": "ANNUAL", "annual": "ANNUAL", "pto": "ANNUAL",
                   "privilege": "ANNUAL", "maternity": "PARENTAL", "paternity": "PARENTAL", "unpaid": "UNPAID",
                   "lwp": "UNPAID", "lop": "UNPAID", "bereavement": "BEREAVEMENT", "marriage": "MARRIAGE",
                   "comp_off": "COMP_OFF", "study": "STUDY"}
# Seeded reason codes per tenant (03-leave/05 §4; V3-022).
REASON_CODES = (
    ("REJECT", "WORKLOAD", "Business need / workload", True), ("REJECT", "COVERAGE", "Not enough cover", True),
    ("REJECT", "POLICY", "Against leave policy", False), ("REJECT", "INSUFFICIENT_NOTICE", "Too little notice", False),
    ("RETURN", "DATES", "Please change the dates", False), ("RETURN", "DOCUMENT", "Document missing", False),
    ("RETURN", "DETAILS", "More details needed", False), ("CANCEL", "PLANS_CHANGED", "Plans changed", False),
    ("RECALL", "BUSINESS_CRITICAL", "Business-critical work", False),
    ("ADJUST", "CORRECTION", "Correction of an error", False), ("ADJUST", "GRANT", "Special grant", False),
    ("OVERRIDE", "HR_DECISION", "HR decision", False),
)


def _app_roles(bind) -> list:
    return [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"))]


def _columns() -> None:
    op.execute("""ALTER TABLE leave_types
        ADD COLUMN category VARCHAR(20) NOT NULL DEFAULT 'OTHER',
        ADD COLUMN unit VARCHAR(4) NOT NULL DEFAULT 'DAY',
        ADD COLUMN sensitivity VARCHAR(10) NOT NULL DEFAULT 'STANDARD',
        ADD COLUMN absence_class VARCHAR(12) NOT NULL DEFAULT 'PLANNED',
        ADD COLUMN counts_toward_absence_score BOOLEAN NOT NULL DEFAULT false,
        ADD COLUMN description TEXT,
        ADD COLUMN name_i18n JSONB NOT NULL DEFAULT '{}'::jsonb,
        ADD COLUMN color_token VARCHAR(20) NOT NULL DEFAULT 'leave-1',
        ADD COLUMN icon VARCHAR(40) NOT NULL DEFAULT 'tag',
        ADD COLUMN display_order INTEGER NOT NULL DEFAULT 100,
        ADD COLUMN row_version INTEGER NOT NULL DEFAULT 1""")
    op.execute("ALTER TABLE leave_types DROP CONSTRAINT ck_leave_types_kind")
    op.execute("ALTER TABLE leave_types ADD CONSTRAINT ck_leave_types_kind "
               "CHECK (kind IN ('COMP_OFF', 'FLOATING_HOLIDAY', 'EVENT'))")
    for name, check in (
        ("category", f"category IN {_in(CATEGORIES)}"),
        ("unit", "unit IN ('DAY', 'HOUR')"),
        ("sensitivity", "sensitivity IN ('STANDARD', 'SENSITIVE')"),
        ("absence_class", "absence_class IN ('PLANNED', 'UNPLANNED', 'STATUTORY', 'PROTECTED')"),
        ("absence_score", "NOT (absence_class IN ('STATUTORY', 'PROTECTED') AND counts_toward_absence_score)"),
        ("kind_category", "(kind IS DISTINCT FROM 'COMP_OFF' OR category = 'COMP_OFF') AND "
                          "(kind IS DISTINCT FROM 'FLOATING_HOLIDAY' OR category = 'FLOATING')"),
        ("unpaid", "category <> 'UNPAID' OR NOT is_paid"),
        ("display_order", "display_order BETWEEN 0 AND 10000"),
    ):
        op.execute(f"ALTER TABLE leave_types ADD CONSTRAINT ck_leave_types_{name} CHECK ({check})")
    op.execute("""ALTER TABLE employees
        ADD COLUMN probation_end_date DATE, ADD COLUMN confirmation_date DATE,
        ADD COLUMN notice_start_date DATE, ADD COLUMN service_start_date DATE""")
    # Second-person activation of a policy version (03-leave/01 §5.6 step 0; generic and nullable).
    op.execute("""ALTER TABLE policy_versions
        ADD COLUMN activation_requested_by_user_id INTEGER, ADD COLUMN activation_requested_at TIMESTAMPTZ,
        ADD COLUMN activation_request JSONB""")
    op.execute("ALTER TABLE leaves DROP CONSTRAINT ck_leaves_status")
    op.execute(f"ALTER TABLE leaves ADD CONSTRAINT ck_leaves_status CHECK (status IN {_in(STATUSES)})")
    op.execute(f"""ALTER TABLE leaves
        ADD COLUMN source VARCHAR(12) NOT NULL DEFAULT 'SELF',
        ADD COLUMN requested_by_user_id INTEGER,
        ADD COLUMN plan_version_id INTEGER,
        ADD COLUMN version INTEGER NOT NULL DEFAULT 1,
        ADD COLUMN flags JSONB NOT NULL DEFAULT '[]'::jsonb,
        ADD COLUMN issues JSONB NOT NULL DEFAULT '[]'::jsonb,
        ADD COLUMN sensitivity VARCHAR(10),
        ADD COLUMN is_emergency BOOLEAN NOT NULL DEFAULT false,
        ADD COLUMN parent_leave_id INTEGER,
        ADD COLUMN event_id INTEGER,
        ADD COLUMN reason_code VARCHAR(40),
        ADD COLUMN contact_note VARCHAR(500),
        ADD COLUMN handover_employee_id INTEGER,
        ADD COLUMN submitted_at TIMESTAMPTZ,
        ADD COLUMN returned_at TIMESTAMPTZ,
        ADD COLUMN withdrawn_at TIMESTAMPTZ,
        ADD COLUMN cancelled_at TIMESTAMPTZ,
        ADD COLUMN row_version INTEGER NOT NULL DEFAULT 1,
        ADD CONSTRAINT ck_leaves_source CHECK (source IN {_in(SOURCES)}),
        ADD CONSTRAINT ck_leaves_sensitivity CHECK (sensitivity IN ('STANDARD', 'SENSITIVE')),
        ADD CONSTRAINT ck_leaves_version CHECK (version >= 1),
        ADD CONSTRAINT fk_leaves_plan_version FOREIGN KEY (company_id, plan_version_id)
            REFERENCES policy_versions (company_id, id) ON DELETE RESTRICT,
        ADD CONSTRAINT fk_leaves_parent FOREIGN KEY (company_id, parent_leave_id)
            REFERENCES leaves (company_id, id) ON DELETE RESTRICT,
        ADD CONSTRAINT fk_leaves_handover FOREIGN KEY (company_id, handover_employee_id)
            REFERENCES employees (company_id, id) ON DELETE RESTRICT""")
    op.execute("""ALTER TABLE leave_days
        ADD COLUMN charge_reason VARCHAR(12),
        ADD COLUMN counting_basis VARCHAR(24),
        ADD CONSTRAINT ck_leave_days_charge_reason CHECK (charge_reason IN ('WORKING', 'BASIS', 'SANDWICH'))""")
    op.execute("ALTER TABLE leave_balance_transactions ALTER COLUMN reason TYPE VARCHAR(20)")
    op.execute("ALTER TABLE leave_balance_transactions DROP CONSTRAINT ck_leave_balance_transactions_reason")
    op.execute("ALTER TABLE leave_balance_transactions ADD CONSTRAINT ck_leave_balance_transactions_reason "
               f"CHECK (reason IN {_in(REASONS)})")
    op.execute("""ALTER TABLE leave_balance_transactions
        ADD COLUMN cycle_start DATE, ADD COLUMN cycle_end DATE,
        ADD COLUMN source VARCHAR(12), ADD COLUMN policy_version_id INTEGER,
        ADD CONSTRAINT ck_leave_balance_transactions_source CHECK (source IN
            ('LEAVE', 'ACCRUAL_JOB', 'YEAR_END', 'ADJUSTMENT', 'IMPORT', 'MIGRATION', 'COMP_OFF', 'ENCASHMENT',
             'PAYROLL', 'SYSTEM')),
        ADD CONSTRAINT ck_leave_balance_transactions_cycle CHECK (cycle_end IS NULL OR cycle_end >= cycle_start)""")


def _backfill(bind) -> None:
    rows = bind.execute(sa.text(
        "SELECT id, code, kind, is_paid, is_unlimited FROM leave_types ORDER BY company_id, id")).all()
    rank: dict = {}
    for type_id, code, kind, is_paid, is_unlimited in rows:
        if kind == "COMP_OFF":
            category = "COMP_OFF"
        elif code in SEED_CATEGORIES and not (SEED_CATEGORIES[code] == "UNPAID" and is_paid):
            category = SEED_CATEGORIES[code]
        elif not is_paid and is_unlimited:
            category = "UNPAID"
        else:
            category = "OTHER"
        sensitivity, absence_class, icon = CATEGORY_DEFAULTS[category]
        company = bind.execute(sa.text("SELECT company_id FROM leave_types WHERE id = :i"), {"i": type_id}).scalar()
        n = rank.get(company, 0)
        rank[company] = n + 1
        bind.execute(sa.text(
            "UPDATE leave_types SET category = :c, sensitivity = :s, absence_class = :a, icon = :i, "
            "counts_toward_absence_score = :score, color_token = :t, display_order = :o WHERE id = :id"),
            {"c": category, "s": sensitivity, "a": absence_class, "i": icon, "score": absence_class == "UNPLANNED",
             "t": f"leave-{n % 12 + 1}", "o": 100 + 10 * n, "id": type_id})
    companies = [c for (c,) in bind.execute(sa.text("SELECT id FROM companies ORDER BY id"))]
    for company_id in companies:
        for applies_to, code, label, refused in REASON_CODES:
            bind.execute(sa.text(
                "INSERT INTO leave_reason_codes (company_id, code, label, applies_to, counts_as_refused) "
                "VALUES (:c, :code, :label, :a, :r) ON CONFLICT DO NOTHING"),
                {"c": company_id, "code": code, "label": label, "a": applies_to, "r": refused})


def upgrade() -> None:
    bind = op.get_bind()
    _columns()
    for table, body in TABLES.items():
        op.execute(f"CREATE TABLE {table} ({body})")
        op.execute(f"CREATE INDEX ix_{table}_company_id ON {table} (company_id)")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")
    op.execute("ALTER TABLE leaves ADD CONSTRAINT fk_leaves_event FOREIGN KEY (company_id, event_id) "
               "REFERENCES leave_events (company_id, id) ON DELETE RESTRICT")
    for statement in INDEXES:
        op.execute(statement)
    for role in _app_roles(bind):
        quoted = '"' + role.replace('"', '""') + '"'
        for table in TABLES:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {quoted}")
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}")
    _backfill(bind)
    for job_type, cadence, minute in SCHEDULED:
        bind.execute(sa.text(
            "INSERT INTO scheduled_jobs (job_type, cadence, at_minute, enabled, next_run_at) "
            "VALUES (:t, :c, :m, true, now()) ON CONFLICT (job_type) DO NOTHING"),
            {"t": job_type, "c": cadence, "m": minute})


def downgrade() -> None:
    bind = op.get_bind()
    used = bind.execute(sa.text(
        "SELECT (SELECT count(*) FROM leave_change_requests) + (SELECT count(*) FROM comp_off_claims) "
        "+ (SELECT count(*) FROM leave_encashment_requests) + (SELECT count(*) FROM leave_year_end_runs) "
        f"+ (SELECT count(*) FROM leave_balance_transactions WHERE reason NOT IN {_in(OLD_REASONS)}) "
        f"+ (SELECT count(*) FROM leaves WHERE status NOT IN {_in(OLD_STATUSES)})")).scalar()
    if used:
        raise RuntimeError("Leave v2 data exists; refusing to drop the leave v2 schema (roll forward instead).")
    bind.execute(sa.text("DELETE FROM scheduled_jobs WHERE job_type IN ('leave.accrue', 'leave.sla_scan')"))
    op.execute("ALTER TABLE leaves DROP CONSTRAINT IF EXISTS fk_leaves_event")
    for table in reversed(list(TABLES)):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("""ALTER TABLE leave_balance_transactions DROP CONSTRAINT ck_leave_balance_transactions_cycle,
        DROP CONSTRAINT ck_leave_balance_transactions_source, DROP COLUMN cycle_start, DROP COLUMN cycle_end,
        DROP COLUMN source, DROP COLUMN policy_version_id""")
    op.execute("ALTER TABLE leave_balance_transactions DROP CONSTRAINT ck_leave_balance_transactions_reason")
    op.execute("ALTER TABLE leave_balance_transactions ADD CONSTRAINT ck_leave_balance_transactions_reason "
               f"CHECK (reason IN {_in(OLD_REASONS)})")
    op.execute("ALTER TABLE leave_balance_transactions ALTER COLUMN reason TYPE VARCHAR(10)")
    op.execute("ALTER TABLE leave_days DROP CONSTRAINT ck_leave_days_charge_reason, DROP COLUMN charge_reason, "
               "DROP COLUMN counting_basis")
    op.execute("""ALTER TABLE leaves DROP CONSTRAINT fk_leaves_plan_version, DROP CONSTRAINT fk_leaves_parent,
        DROP CONSTRAINT fk_leaves_handover, DROP CONSTRAINT ck_leaves_source, DROP CONSTRAINT ck_leaves_sensitivity,
        DROP CONSTRAINT ck_leaves_version, DROP COLUMN source, DROP COLUMN requested_by_user_id,
        DROP COLUMN plan_version_id, DROP COLUMN version, DROP COLUMN flags, DROP COLUMN issues, DROP COLUMN sensitivity,
        DROP COLUMN is_emergency, DROP COLUMN parent_leave_id, DROP COLUMN event_id, DROP COLUMN reason_code,
        DROP COLUMN contact_note, DROP COLUMN handover_employee_id, DROP COLUMN submitted_at, DROP COLUMN returned_at,
        DROP COLUMN withdrawn_at, DROP COLUMN cancelled_at, DROP COLUMN row_version""")
    op.execute("DROP INDEX IF EXISTS ix_leaves_employee_status")
    op.execute("ALTER TABLE leaves DROP CONSTRAINT ck_leaves_status")
    op.execute(f"ALTER TABLE leaves ADD CONSTRAINT ck_leaves_status CHECK (status IN {_in(OLD_STATUSES)})")
    op.execute("""ALTER TABLE employees DROP COLUMN probation_end_date, DROP COLUMN confirmation_date,
        DROP COLUMN notice_start_date, DROP COLUMN service_start_date""")
    op.execute("""ALTER TABLE policy_versions DROP COLUMN activation_requested_by_user_id,
        DROP COLUMN activation_requested_at, DROP COLUMN activation_request""")
    for name in ("category", "unit", "sensitivity", "absence_class", "absence_score", "kind_category", "unpaid",
                 "display_order"):
        op.execute(f"ALTER TABLE leave_types DROP CONSTRAINT ck_leave_types_{name}")
    op.execute("ALTER TABLE leave_types DROP CONSTRAINT ck_leave_types_kind")
    op.execute("ALTER TABLE leave_types ADD CONSTRAINT ck_leave_types_kind CHECK (kind IN ('COMP_OFF'))")
    op.execute("""ALTER TABLE leave_types DROP COLUMN category, DROP COLUMN unit, DROP COLUMN sensitivity,
        DROP COLUMN absence_class, DROP COLUMN counts_toward_absence_score, DROP COLUMN description,
        DROP COLUMN name_i18n, DROP COLUMN color_token, DROP COLUMN icon, DROP COLUMN display_order,
        DROP COLUMN row_version""")

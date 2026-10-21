"""Compensation v2: pay components, salary structures, compensation assignments, revisions (3.3, 3.9 §3.4).

* A pay component (code unique per tenant, immutable once used, EC-CMP-06) has effective-dated
  versions holding everything that can change: calculation method and its formula/parameters, tags,
  proration (with the I-16 basis override), frequency, totals flags (defaults from the kind),
  cap/floor, eligibility formula, payslip display, visibility, GL and reporting codes. ACTIVE
  versions of one component never overlap (trigger).
* A salary template (structure) has versions DRAFT -> ACTIVE -> ARCHIVED; at most one version is
  ACTIVE (selectable for new assignments) and a published version is frozen. Its lines name a
  component, a method with its value/formula/parameters (copied from the component when omitted),
  whether an assignment may override it and the override bounds.
* A compensation assignment links an employee placement (employee_assignments) to a template
  version with the pay basis amount (annual CTC, monthly gross, target net, hourly or daily rate)
  in a currency; its lines are the overrides. ACTIVE and SCHEDULED assignments of one placement
  never overlap (trigger, EC-CMP-10). Migrated legacy rows keep their salary_structure id.
* Revision cycles (bulk increments) hold their population, method and proposed items; compensation
  imports hold the validated rows of a CSV dry run until a background job commits them (job payloads
  carry ids only, EC-SEC-08).
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from app.models.base import BaseModel, TenantMixin

COMPONENT_KINDS = (
    "EARNING", "DEDUCTION", "EMPLOYER_CONTRIBUTION", "REIMBURSEMENT", "BENEFIT_IN_KIND", "INFORMATIONAL", "ROUNDING",
)
CATEGORIES_BY_KIND = {
    "EARNING": (
        "BASE", "FIXED_ALLOWANCE", "VARIABLE", "OVERTIME", "HOLIDAY_PREMIUM", "SHIFT_PREMIUM", "TASK_EARNING",
        "COMMISSION", "BONUS", "RETRO_ADJUSTMENT", "LEAVE_ENCASHMENT", "GRATUITY", "SEVERANCE",
    ),
    "DEDUCTION": (
        "TAX", "EMPLOYEE_CONTRIBUTION", "LOAN_REPAYMENT", "ADVANCE_RECOVERY", "VOLUNTARY", "PENALTY_IF_LEGAL",
        "GARNISHMENT_IF_LEGAL", "RECOVERY",
    ),
    "EMPLOYER_CONTRIBUTION": ("EMPLOYER_CONTRIBUTION", "BENEFIT"),
    "REIMBURSEMENT": ("REIMBURSEMENT",),
    "BENEFIT_IN_KIND": ("BENEFIT",),
    "INFORMATIONAL": ("INFO",),
    "ROUNDING": ("ROUNDING",),
}
CATEGORIES = tuple(sorted({c for cats in CATEGORIES_BY_KIND.values() for c in cats}))
CALC_METHODS = ("FIXED", "PERCENT_OF", "FORMULA", "RATE_X_UNITS", "SLAB", "INPUT", "BALANCING", "SYSTEM")
PRORATIONS = ("NONE", "BY_PAID_DAYS", "BY_PAID_HOURS", "BY_EMPLOYMENT_DAYS")
PRORATION_BASES = ("CALENDAR_DAYS", "WORKING_DAYS", "FIXED_30", "FIXED_26", "CUSTOM_DAYS", "HOURS")
FREQUENCIES = ("EVERY_PERIOD", "SPECIFIC_MONTHS", "ONE_TIME", "ON_EXIT")
VISIBILITIES = ("EMPLOYEE_VISIBLE", "HR_ONLY")
ROUNDING_MODES = ("HALF_UP", "HALF_EVEN", "UP", "DOWN", "CEILING", "FLOOR")
COMPONENT_STATUSES = ("ACTIVE", "ARCHIVED")
COMPONENT_VERSION_STATUSES = ("ACTIVE", "SUPERSEDED")
STRUCTURE_TYPES = ("CTC_BASED", "GROSS_BASED", "NET_BASED", "HOURLY", "DAILY", "TASK_BASED")
TEMPLATE_STATUSES = ("ACTIVE", "ARCHIVED")
TEMPLATE_VERSION_STATUSES = ("DRAFT", "ACTIVE", "ARCHIVED")
ASSIGNMENT_REASONS = ("HIRE", "REVISION", "PROMOTION", "TRANSFER", "CORRECTION", "MIGRATED")
ASSIGNMENT_STATUSES = ("DRAFT", "PENDING_APPROVAL", "SCHEDULED", "ACTIVE", "SUPERSEDED", "REJECTED")
# Rows the overlap trigger compares (EC-CMP-10).
IN_FORCE_STATUSES = ("SCHEDULED", "ACTIVE")
CYCLE_STATUSES = ("DRAFT", "PREVIEWED", "PENDING_APPROVAL", "APPROVED", "APPLIED", "REJECTED", "CANCELLED")
CYCLE_ITEM_STATUSES = ("PROPOSED", "EXCLUDED", "APPLIED", "FAILED")
IMPORT_STATUSES = ("VALIDATED", "QUEUED", "COMMITTED", "FAILED")

# Totals flags by kind (3.3 §2.1); a version may override them within the rules of the service.
DEFAULT_TOTALS = {
    "EARNING": {"in_gross": True, "in_net": True, "in_ctc": True, "in_employer_cost": True},
    "DEDUCTION": {"in_gross": False, "in_net": True, "in_ctc": False, "in_employer_cost": False},
    "EMPLOYER_CONTRIBUTION": {"in_gross": False, "in_net": False, "in_ctc": True, "in_employer_cost": True},
    "REIMBURSEMENT": {"in_gross": False, "in_net": True, "in_ctc": False, "in_employer_cost": True},
    "BENEFIT_IN_KIND": {"in_gross": False, "in_net": False, "in_ctc": True, "in_employer_cost": True},
    "INFORMATIONAL": {"in_gross": False, "in_net": False, "in_ctc": False, "in_employer_cost": False},
    "ROUNDING": {"in_gross": False, "in_net": True, "in_ctc": False, "in_employer_cost": False},
}


def _tenant_ids(table: str):
    return UniqueConstraint("company_id", "id", name=f"uq_{table}_company_id_id")


class PayComponent(TenantMixin, BaseModel):
    """A typed earning, deduction or contribution (3.3 §2). `owner` is COMPANY or PACK:<code> (read-only)."""

    __tablename__ = "pay_components"

    code = Column(String(40), nullable=False)
    kind = Column(String(25), nullable=False)
    category = Column(String(25), nullable=False)
    owner = Column(String(20), nullable=False, default="COMPANY")
    status = Column(String(10), nullable=False, default="ACTIVE")
    created_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        _tenant_ids("pay_components"),
        UniqueConstraint("company_id", "code", name="uq_pay_components_company_code"),
        CheckConstraint("code ~ '^[A-Z][A-Z0-9_]{0,39}$' AND position('__' in code) = 0", name="ck_pay_components_code"),
        CheckConstraint(f"kind IN {COMPONENT_KINDS}", name="ck_pay_components_kind"),
        CheckConstraint(f"category IN {CATEGORIES}", name="ck_pay_components_category"),
        CheckConstraint("owner = 'COMPANY' OR owner ~ '^PACK:[A-Z0-9_]{1,15}$'", name="ck_pay_components_owner"),
        CheckConstraint(f"status IN {COMPONENT_STATUSES}", name="ck_pay_components_status"),
    )


class PayComponentVersion(TenantMixin, BaseModel):
    """Every property of a component that can change (3.3 §2.2); each change is a new version.

    `params` holds the method parameters of formula.method_formula (percent, bases, tag, rate, units,
    table, base, progressive, others, total); `formula` the FORMULA expression. `cap` / `floor` are
    {"amount": "<decimal>", "currency": "XXX"} or {"formula": "..."}; `frequency` is {"type": ...,
    "months": [...]}; `totals_flags` {in_gross, in_net, in_ctc, in_employer_cost}; `payslip` {group,
    order, show_if_zero, show_rate_column, label_override}; `gl` {debit_account, credit_account,
    allocate_by_cost_center}; `taxability` {jurisdiction: TAXABLE|EXEMPT|PARTIAL}.
    """

    __tablename__ = "pay_component_versions"

    component_id = Column(Integer, nullable=False, index=True)
    version = Column(Integer, nullable=False)
    name = Column(String(150), nullable=False)
    translations = Column(JSONB, nullable=False, default=dict)
    description = Column(String(1000), nullable=True)
    calc_method = Column(String(15), nullable=False)
    formula = Column(Text, nullable=True)
    params = Column(JSONB, nullable=False, default=dict)
    tags = Column(ARRAY(String(40)), nullable=False, default=list)
    proration = Column(String(20), nullable=False, default="NONE")
    proration_basis = Column(String(20), nullable=True)
    frequency = Column(JSONB, nullable=False, default=lambda: {"type": "EVERY_PERIOD"})
    taxability = Column(JSONB, nullable=False, default=dict)
    totals_flags = Column(JSONB, nullable=False, default=dict)
    cap = Column(JSONB, nullable=True)
    floor = Column(JSONB, nullable=True)
    rounding = Column(String(10), nullable=True)
    eligibility = Column(Text, nullable=True)
    payslip = Column(JSONB, nullable=False, default=dict)
    visibility = Column(String(20), nullable=False, default="EMPLOYEE_VISIBLE")
    gl = Column(JSONB, nullable=True)
    reporting_codes = Column(JSONB, nullable=False, default=dict)
    input_approval_above = Column(Numeric(19, 4), nullable=True)
    input_approval_currency = Column(String(3), nullable=True)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    status = Column(String(12), nullable=False, default="ACTIVE")
    created_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        _tenant_ids("pay_component_versions"),
        UniqueConstraint("company_id", "component_id", "version", name="uq_pay_component_versions_version"),
        ForeignKeyConstraint(
            ["company_id", "component_id"], ["pay_components.company_id", "pay_components.id"],
            ondelete="CASCADE", name="fk_pay_component_versions_component",
        ),
        CheckConstraint("version >= 1", name="ck_pay_component_versions_version"),
        CheckConstraint(f"calc_method IN {CALC_METHODS}", name="ck_pay_component_versions_method"),
        CheckConstraint(f"proration IN {PRORATIONS}", name="ck_pay_component_versions_proration"),
        CheckConstraint(
            f"proration_basis IS NULL OR proration_basis IN {PRORATION_BASES}", name="ck_pay_component_versions_basis"
        ),
        CheckConstraint(f"visibility IN {VISIBILITIES}", name="ck_pay_component_versions_visibility"),
        CheckConstraint(f"rounding IS NULL OR rounding IN {ROUNDING_MODES}", name="ck_pay_component_versions_rounding"),
        CheckConstraint(f"status IN {COMPONENT_VERSION_STATUSES}", name="ck_pay_component_versions_status"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_pay_component_versions_window"),
        CheckConstraint("jsonb_typeof(params) = 'object'", name="ck_pay_component_versions_params"),
        CheckConstraint(
            "(input_approval_above IS NULL) = (input_approval_currency IS NULL)",
            name="ck_pay_component_versions_approval_currency",
        ),
        Index("ix_pay_component_versions_lookup", "company_id", "component_id", "effective_from"),
    )


class SalaryTemplate(TenantMixin, BaseModel):
    """A reusable salary structure (3.3 §6). `currency` NULL means "the pay group currency"."""

    __tablename__ = "salary_templates"

    code = Column(String(40), nullable=False)
    name = Column(String(150), nullable=False)
    description = Column(String(1000), nullable=True)
    type = Column(String(15), nullable=False)
    currency = Column(String(3), nullable=True)
    status = Column(String(10), nullable=False, default="ACTIVE")
    created_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        _tenant_ids("salary_templates"),
        UniqueConstraint("company_id", "code", name="uq_salary_templates_company_code"),
        CheckConstraint("code ~ '^[A-Z][A-Z0-9_-]{0,39}$'", name="ck_salary_templates_code"),
        CheckConstraint(f"type IN {STRUCTURE_TYPES}", name="ck_salary_templates_type"),
        CheckConstraint("currency IS NULL OR currency ~ '^[A-Z]{3}$'", name="ck_salary_templates_currency"),
        CheckConstraint(f"status IN {TEMPLATE_STATUSES}", name="ck_salary_templates_status"),
    )


class SalaryTemplateVersion(TenantMixin, BaseModel):
    """One version of a structure. Only DRAFT versions change; publishing freezes the version.

    `eligibility` {legal_entity_ids, pay_group_ids, grade_ids, employment_type_ids} (empty = everyone);
    `test_range` {"min": "<pay basis>", "max": "<pay basis>"} is the declared CTC/gross range the
    balancing check covers (EC-CMP-04); `test_vectors` [{name, pay_basis_amount, expected: {CODE:
    monthly amount}}] must all pass to publish; `validation` keeps the last validation report.
    """

    __tablename__ = "salary_template_versions"

    template_id = Column(Integer, nullable=False, index=True)
    version = Column(Integer, nullable=False)
    status = Column(String(10), nullable=False, default="DRAFT")
    eligibility = Column(JSONB, nullable=False, default=dict)
    test_range = Column(JSONB, nullable=True)
    test_vectors = Column(JSONB, nullable=False, default=list)
    validation = Column(JSONB, nullable=True)
    effective_from = Column(Date, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)
    created_by_user_id = Column(Integer, nullable=True)
    published_by_user_id = Column(Integer, nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        _tenant_ids("salary_template_versions"),
        UniqueConstraint("company_id", "template_id", "version", name="uq_salary_template_versions_version"),
        ForeignKeyConstraint(
            ["company_id", "template_id"], ["salary_templates.company_id", "salary_templates.id"],
            ondelete="CASCADE", name="fk_salary_template_versions_template",
        ),
        CheckConstraint("version >= 1", name="ck_salary_template_versions_version"),
        CheckConstraint("row_version >= 1", name="ck_salary_template_versions_row_version"),
        CheckConstraint(f"status IN {TEMPLATE_VERSION_STATUSES}", name="ck_salary_template_versions_status"),
        CheckConstraint("jsonb_typeof(eligibility) = 'object'", name="ck_salary_template_versions_eligibility"),
        CheckConstraint("jsonb_typeof(test_vectors) = 'array'", name="ck_salary_template_versions_vectors"),
        CheckConstraint(
            "status = 'DRAFT' OR (published_at IS NOT NULL AND effective_from IS NOT NULL)",
            name="ck_salary_template_versions_published",
        ),
        Index("uq_salary_template_versions_active", "template_id", unique=True,
              postgresql_where=text("status = 'ACTIVE'")),
    )


class SalaryTemplateLine(TenantMixin, BaseModel):
    """One component of a structure version: method, value or formula, override rules (3.3 §6.2).

    `value` is the FIXED monthly amount (or annual with params.basis = ANNUAL; NULL = the assignment
    supplies it) or the PERCENT_OF percentage (40 = 40%); `formula` the FORMULA expression; `params`
    the other method parameters. `override_min` / `override_max` bound an assignment's override.
    """

    __tablename__ = "salary_template_lines"

    template_version_id = Column(Integer, nullable=False, index=True)
    component_id = Column(Integer, nullable=False, index=True)
    method = Column(String(15), nullable=False)
    value = Column(Numeric(19, 6), nullable=True)
    formula = Column(Text, nullable=True)
    params = Column(JSONB, nullable=False, default=dict)
    overridable = Column(Boolean, nullable=False, default=False)
    override_min = Column(Numeric(19, 6), nullable=True)
    override_max = Column(Numeric(19, 6), nullable=True)
    display_order = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        _tenant_ids("salary_template_lines"),
        UniqueConstraint("company_id", "template_version_id", "component_id", name="uq_salary_template_lines_component"),
        ForeignKeyConstraint(
            ["company_id", "template_version_id"], ["salary_template_versions.company_id", "salary_template_versions.id"],
            ondelete="CASCADE", name="fk_salary_template_lines_version",
        ),
        ForeignKeyConstraint(
            ["company_id", "component_id"], ["pay_components.company_id", "pay_components.id"],
            name="fk_salary_template_lines_component",
        ),
        CheckConstraint(f"method IN {CALC_METHODS}", name="ck_salary_template_lines_method"),
        CheckConstraint("jsonb_typeof(params) = 'object'", name="ck_salary_template_lines_params"),
        CheckConstraint(
            "override_min IS NULL OR override_max IS NULL OR override_max >= override_min",
            name="ck_salary_template_lines_bounds",
        ),
        CheckConstraint(
            "overridable OR (override_min IS NULL AND override_max IS NULL)", name="ck_salary_template_lines_overridable"
        ),
    )


class CompensationAssignment(TenantMixin, BaseModel):
    """An employee's salary from effective_from to effective_to (inclusive) on one placement (3.3 §7).

    `pay_basis_amount` is the annual CTC, monthly gross, target net, hourly or daily rate depending on
    the structure type, in `currency`; a currency other than the pay group's needs `fx_rule`.
    `split_period` records that the start falls inside a pay period, so the run splits it (EC-CMP-16).
    `legacy_salary_structure_id` maps rows migrated from salary_structure (EC-MIG-01).
    """

    __tablename__ = "compensation_assignments"

    employee_id = Column(Integer, nullable=False, index=True)
    assignment_id = Column(Integer, nullable=False, index=True)
    template_version_id = Column(Integer, nullable=False, index=True)
    pay_basis_amount = Column(Numeric(19, 4), nullable=False)
    currency = Column(String(3), nullable=False)
    fx_rule = Column(JSONB, nullable=True)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    reason = Column(String(12), nullable=False)
    status = Column(String(20), nullable=False, default="DRAFT")
    note = Column(String(1000), nullable=True)
    attachments = Column(JSONB, nullable=False, default=list)
    split_period = Column(Boolean, nullable=False, default=False)
    previous_assignment_id = Column(Integer, nullable=True)
    revision_cycle_id = Column(Integer, nullable=True)
    legacy_salary_structure_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)
    created_by_user_id = Column(Integer, nullable=True)
    submitted_by_user_id = Column(Integer, nullable=True)
    approved_by_user_id = Column(Integer, nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        _tenant_ids("compensation_assignments"),
        ForeignKeyConstraint(
            ["company_id", "employee_id"], ["employees.company_id", "employees.id"],
            name="fk_compensation_assignments_employee",
        ),
        # employee_assignments has no UNIQUE (company_id, id); the guard trigger checks company and employee.
        ForeignKeyConstraint(["assignment_id"], ["employee_assignments.id"], name="fk_compensation_assignments_placement"),
        ForeignKeyConstraint(
            ["company_id", "template_version_id"], ["salary_template_versions.company_id", "salary_template_versions.id"],
            name="fk_compensation_assignments_template_version",
        ),
        ForeignKeyConstraint(
            ["company_id", "previous_assignment_id"], ["compensation_assignments.company_id", "compensation_assignments.id"],
            name="fk_compensation_assignments_previous",
        ),
        ForeignKeyConstraint(
            ["company_id", "revision_cycle_id"], ["revision_cycles.company_id", "revision_cycles.id"],
            name="fk_compensation_assignments_cycle",
        ),
        CheckConstraint("pay_basis_amount >= 0", name="ck_compensation_assignments_amount"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_compensation_assignments_currency"),
        CheckConstraint(f"reason IN {ASSIGNMENT_REASONS}", name="ck_compensation_assignments_reason"),
        CheckConstraint(f"status IN {ASSIGNMENT_STATUSES}", name="ck_compensation_assignments_status"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_compensation_assignments_window"),
        CheckConstraint("row_version >= 1", name="ck_compensation_assignments_row_version"),
        CheckConstraint("jsonb_typeof(attachments) = 'array'", name="ck_compensation_assignments_attachments"),
        CheckConstraint(
            "approved_by_user_id IS NULL OR created_by_user_id IS NULL OR approved_by_user_id <> created_by_user_id",
            name="ck_compensation_assignments_maker_checker",
        ),
        Index("ix_compensation_assignments_current", "company_id", "employee_id", "effective_from"),
        Index("uq_compensation_assignments_legacy", "company_id", "legacy_salary_structure_id", unique=True,
              postgresql_where=text("legacy_salary_structure_id IS NOT NULL")),
    )


class CompensationAssignmentLine(TenantMixin, BaseModel):
    """An override of one overridable structure line: `amount` (FIXED monthly amount or RATE_X_UNITS
    rate), `percent` (PERCENT_OF) or `formula` (FORMULA)."""

    __tablename__ = "compensation_assignment_lines"

    compensation_assignment_id = Column(Integer, nullable=False, index=True)
    component_id = Column(Integer, nullable=False)
    amount = Column(Numeric(19, 4), nullable=True)
    percent = Column(Numeric(12, 6), nullable=True)
    formula = Column(Text, nullable=True)

    __table_args__ = (
        _tenant_ids("compensation_assignment_lines"),
        UniqueConstraint("company_id", "compensation_assignment_id", "component_id",
                         name="uq_compensation_assignment_lines_component"),
        ForeignKeyConstraint(
            ["company_id", "compensation_assignment_id"],
            ["compensation_assignments.company_id", "compensation_assignments.id"],
            ondelete="CASCADE", name="fk_compensation_assignment_lines_assignment",
        ),
        ForeignKeyConstraint(
            ["company_id", "component_id"], ["pay_components.company_id", "pay_components.id"],
            name="fk_compensation_assignment_lines_component",
        ),
        CheckConstraint(
            "(amount IS NOT NULL)::int + (percent IS NOT NULL)::int + (formula IS NOT NULL)::int = 1",
            name="ck_compensation_assignment_lines_one_value",
        ),
    )


class RevisionCycle(TenantMixin, BaseModel):
    """A bulk increment (3.3 §8.2): population filters, method, effective date, approval and totals."""

    __tablename__ = "revision_cycles"

    name = Column(String(150), nullable=False)
    population = Column(JSONB, nullable=False, default=dict)
    method = Column(JSONB, nullable=False, default=dict)
    effective_from = Column(Date, nullable=False)
    status = Column(String(20), nullable=False, default="DRAFT")
    totals = Column(JSONB, nullable=True)
    approval_request_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)
    created_by_user_id = Column(Integer, nullable=True)
    applied_by_user_id = Column(Integer, nullable=True)
    applied_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        _tenant_ids("revision_cycles"),
        CheckConstraint(f"status IN {CYCLE_STATUSES}", name="ck_revision_cycles_status"),
        CheckConstraint("jsonb_typeof(population) = 'object'", name="ck_revision_cycles_population"),
        CheckConstraint("jsonb_typeof(method) = 'object'", name="ck_revision_cycles_method"),
        CheckConstraint("row_version >= 1", name="ck_revision_cycles_row_version"),
    )


class RevisionCycleItem(TenantMixin, BaseModel):
    """One employee of a cycle: the assignment in force, the proposal and the outcome."""

    __tablename__ = "revision_cycle_items"

    cycle_id = Column(Integer, nullable=False, index=True)
    employee_id = Column(Integer, nullable=False)
    old_assignment_id = Column(Integer, nullable=True)
    new_assignment_id = Column(Integer, nullable=True)
    proposed = Column(JSONB, nullable=False, default=dict)
    status = Column(String(10), nullable=False, default="PROPOSED")
    note = Column(String(500), nullable=True)

    __table_args__ = (
        _tenant_ids("revision_cycle_items"),
        UniqueConstraint("company_id", "cycle_id", "employee_id", name="uq_revision_cycle_items_employee"),
        ForeignKeyConstraint(
            ["company_id", "cycle_id"], ["revision_cycles.company_id", "revision_cycles.id"],
            ondelete="CASCADE", name="fk_revision_cycle_items_cycle",
        ),
        ForeignKeyConstraint(
            ["company_id", "employee_id"], ["employees.company_id", "employees.id"], name="fk_revision_cycle_items_employee",
        ),
        ForeignKeyConstraint(
            ["company_id", "old_assignment_id"], ["compensation_assignments.company_id", "compensation_assignments.id"],
            name="fk_revision_cycle_items_old",
        ),
        ForeignKeyConstraint(
            ["company_id", "new_assignment_id"], ["compensation_assignments.company_id", "compensation_assignments.id"],
            name="fk_revision_cycle_items_new",
        ),
        CheckConstraint(f"status IN {CYCLE_ITEM_STATUSES}", name="ck_revision_cycle_items_status"),
    )


class CompensationImport(TenantMixin, BaseModel):
    """A validated bulk-assignment CSV (3.3 §7): the parsed rows wait here for the commit job."""

    __tablename__ = "compensation_imports"

    status = Column(String(10), nullable=False, default="VALIDATED")
    file_name = Column(String(255), nullable=True)
    rows = Column(JSONB, nullable=False, default=list)
    summary = Column(JSONB, nullable=False, default=dict)
    result = Column(JSONB, nullable=True)
    job_id = Column(Integer, nullable=True)
    created_by_user_id = Column(Integer, nullable=False)

    __table_args__ = (
        _tenant_ids("compensation_imports"),
        CheckConstraint(f"status IN {IMPORT_STATUSES}", name="ck_compensation_imports_status"),
        CheckConstraint("jsonb_typeof(rows) = 'array'", name="ck_compensation_imports_rows"),
    )

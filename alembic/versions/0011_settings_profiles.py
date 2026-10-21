"""Settings profiles: companies payroll profile columns and LEGACY_V1 materialization for existing tenants.

Decision I-14 / ARCHITECTURE §9 (no silent drift): every tenant that exists when this migration runs
receives explicit TENANT-level setting_values rows reproducing the legacy payroll (profile LEGACY_V1,
source LEGACY_MIGRATION, effective from the company's creation date) and the profile's catch-all
policies (attendance pay and payslip template ACTIVE; overtime and late penalty DRAFT), so later
registry or partner default changes never alter its resolved values. The legacy values of
payrollSeparationOfDuties, graceTime and lunchIsPaid are carried over from company_settings, and the
legacy `overtime` multiplier (never used by legacy payroll) is copied into the draft overtime policy
while overtime stays OFF (EC-MIG-04). An invalid legacy value is skipped (the profile value applies)
and reported in the audit details. companies.payroll_profile_version is set to LEGACY_V1; tenants
created later get STARTER_V1 at signup (app.payroll.services.settings_starter).

The data are literals (a frozen copy of registry 2026.10.3), so later registry changes never change
what this migration did. Idempotent: tenants that already have a profile are skipped, and keys or
policy types already configured are left alone. Each tenant gets one "Payroll Settings"
PROFILE_APPLIED audit row (details.migration = 0011_settings_profiles, with the created policy ids);
downgrade() removes the LEGACY_MIGRATION rows and the policies the upgrade created, writes a
PROFILE_REVERTED row, and drops the two columns.

Revision ID: 0011_settings_profiles
Revises: 0010_permissions_v2
Create Date: 2026-10-20
"""
import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional, Sequence, Tuple, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0011_settings_profiles'
down_revision: Union[str, None] = '0010_permissions_v2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.0011")

MARKER = "0011_settings_profiles"
PROFILE = "LEGACY_V1"
SOURCE = "LEGACY_MIGRATION"
REASON = "Legacy payroll migration"
MODULE = "Payroll Settings"
APPLIED = "PROFILE_APPLIED"
REVERTED = "PROFILE_REVERTED"
# Legacy company_settings key -> v2 key and the check its value must pass.
LEGACY_KEYS = {
    "payrollSeparationOfDuties": ("payroll.approval.separation_of_duties", "bool"),
    "graceTime": ("attendance.grace_minutes", "minutes"),
    "lunchIsPaid": ("attendance.lunch_is_paid", "bool"),
}
OVERTIME_KEY = "overtime"

# Frozen copy of settings_registry LEGACY_V1 (registry 2026.10.3): every TENANT-level value.
LEGACY_VALUES = {'attendance.grace_minutes': 15,
 'attendance.lunch_is_paid': False,
 'holiday.optional_quota_per_year': 2,
 'leave.half_day.enabled': True,
 'notify.payroll.approval_requested': ['IN_APP', 'EMAIL'],
 'notify.payroll.exceptions_digest': 'DAILY',
 'notify.payroll.input_cutoff_reminder_days': 2,
 'notify.payroll.payment_status_to_employee': True,
 'org.allow_concurrent_assignments': False,
 'payroll.accounting.enabled': False,
 'payroll.accounting.journal_format': 'GENERIC_CSV',
 'payroll.accounting.post_on': 'FINALIZE',
 'payroll.advances.enabled': False,
 'payroll.approval.payment_dual_control': True,
 'payroll.approval.separation_of_duties': True,
 'payroll.approval.step_up_for_finalize': True,
 'payroll.approval.variance_review_threshold_percent': '20',
 'payroll.arrears.auto_compute': True,
 'payroll.arrears.payment_mode': 'NEXT_REGULAR_RUN',
 'payroll.arrears.recovery_mode': 'NEXT_RUN',
 'payroll.attendance.enabled': True,
 'payroll.attendance.half_day_value': '0.5',
 'payroll.bank.change_requires_approval': True,
 'payroll.bank.cooling_off_hours': 48,
 'payroll.bank.exclude_unverified_from_payout': True,
 'payroll.bank.max_accounts_per_payee': 2,
 'payroll.bank.require_verification': True,
 'payroll.bank.self_service_enabled': True,
 'payroll.bank.split_payments_enabled': False,
 'payroll.bank.verification_methods': ['FORMAT', 'DOCUMENT_REVIEW'],
 'payroll.compensation.enforce_grade_range': False,
 'payroll.deductions.min_net_pay_percent': None,
 'payroll.enabled': True,
 'payroll.fx.anomaly_threshold_percent': '5',
 'payroll.fx.conversion_date_rule': 'PAY_DATE',
 'payroll.fx.manual_rate_requires_approval': True,
 'payroll.fx.max_rate_age_hours': 24,
 'payroll.fx.provider': 'MANUAL',
 'payroll.fx.rate_type': 'MID',
 'payroll.holiday_pay.non_salaried.enabled': False,
 'payroll.holiday_work_premium.enabled': False,
 'payroll.incentives.enabled': False,
 'payroll.late_penalty.enabled': False,
 'payroll.leave.enabled': False,
 'payroll.leave.encashment.enabled': False,
 'payroll.leave.negative_balance_on_exit': 'EXCEPTION',
 'payroll.leave.unapproved_at_cutoff': 'EXCEPTION',
 'payroll.leave.when_disabled_treat_approved_leave_as': 'PAID_EXCUSED',
 'payroll.loans.enabled': False,
 'payroll.lop.custom_days': None,
 'payroll.lop.proration_basis': 'WORKING_DAYS',
 'payroll.lop.sandwich_rule': 'OFF',
 'payroll.lop.zero_paid_days': 'EXCEPTION',
 'payroll.migration.legacy_review_acknowledged': None,
 'payroll.overtime.enabled': False,
 'payroll.payments.reissue_requires_approval': True,
 'payroll.reimbursements.enabled': False,
 'payroll.reimbursements.payout_route': 'WITH_PAYROLL',
 'payroll.rounding.difference_handling': 'ROUNDING_LINE',
 'payroll.rounding.line_mode': 'HALF_UP',
 'payroll.rounding.net_pay_mode': 'HALF_UP',
 'payroll.shift_allowance.enabled': False,
 'payroll.structures.net_based_enabled': False,
 'payroll.task_pay.enabled': False,
 'payroll.wage_inputs.enabled': True,
 'payroll.wage_inputs.require_approval_above': {'amount': '0', 'currency': None},
 'payroll.weekly_off_pay.enabled': False,
 'payslip.email_attach_pdf': False,
 'payslip.email_include_net_pay': False,
 'payslip.email_notify': True,
 'payslip.pdf_protection': 'NONE',
 'payslip.publish_trigger': 'ON_FINALIZE',
 'payslip.queries_enabled': True,
 'payslip.scheduled_publish_time': '09:00',
 'payslip.show_attendance_summary': True,
 'payslip.show_bank_masked': True,
 'payslip.show_employer_contributions': False,
 'payslip.show_leave_balances': True,
 'payslip.show_tax_projection': False,
 'payslip.show_ytd': True,
 'privacy.payroll_retention_years': 8,
 'privacy.small_group_suppression_size': 5,
 'settings.change_approval.enabled': False,
 'tasks.enabled': False}

# Frozen copy of policy_types.starter_policies('LEGACY_V1'): catch-all policies (everyone, priority 0).
LEGACY_POLICIES = [{'type': 'ATTENDANCE_PAY_POLICY',
  'name': 'Legacy attendance pay',
  'status': 'ACTIVE',
  'fields': {'unmarked_day': 'PRESENT',
             'missing_punch_out': 'FULL_DAY',
             'derive_status_from_hours': False,
             'full_day_min_hours': '8',
             'half_day_min_hours': '4',
             'count_on_duty_as_present': True,
             'lop_component_tag': 'LOP_APPLICABLE',
             'break_type_pay_map': {}},
  'policy_hash': '1e73a4d0dac5eaa953150f0631881272220da5aa1807d4248da27824ddc21c10',
  'note': None},
 {'type': 'PAYSLIP_TEMPLATE',
  'name': 'Default payslip',
  'status': 'ACTIVE',
  'fields': {'layout': 'CLASSIC',
             'sections': [{'code': 'HEADER', 'visible': True},
                          {'code': 'EMPLOYEE', 'visible': True},
                          {'code': 'ATTENDANCE_SUMMARY', 'visible': True},
                          {'code': 'EARNINGS', 'visible': True},
                          {'code': 'DEDUCTIONS', 'visible': True},
                          {'code': 'REIMBURSEMENTS', 'visible': True},
                          {'code': 'EMPLOYER_CONTRIBUTIONS', 'visible': True},
                          {'code': 'TOTALS', 'visible': True},
                          {'code': 'PAYMENT', 'visible': True},
                          {'code': 'LEAVE_BALANCES', 'visible': True},
                          {'code': 'TAX_SUMMARY', 'visible': True},
                          {'code': 'FOOTER', 'visible': True}],
             'component_groups': [],
             'footer_note': None,
             'signature_block': None,
             'locale_overrides': {}},
  'policy_hash': '950f6e5763a83d0899fb95f07e0296d744c5b90fede3b36acac9a926c78de5bd',
  'note': None},
 {'type': 'OVERTIME_POLICY',
  'name': 'Legacy overtime',
  'status': 'DRAFT',
  'fields': {'exempt': False,
             'source': 'APPROVED_REQUESTS',
             'daily_threshold': 'SHIFT_DURATION',
             'weekly_threshold_hours': None,
             'threshold_combination': 'DAILY',
             'min_minutes': 30,
             'rounding': {'increment_minutes': 15, 'mode': 'DOWN'},
             'buckets': [{'day_type': 'WORKDAY', 'after_hours': '0', 'multiplier': None},
                         {'day_type': 'WEEKLY_OFF', 'after_hours': '0', 'multiplier': None},
                         {'day_type': 'HOLIDAY', 'after_hours': '0', 'multiplier': None}],
             'rate_formula': 'SUM_TAG_RATE("OT_BASE") / period.basis_days / emp.standard_hours_per_day',
             'caps': {'per_day': None, 'per_week': None, 'per_month': None, 'per_quarter': None},
             'cap_action': 'EXCEPTION',
             'cross_midnight': 'SHIFT_START_DATE',
             'comp_off_allowed': False,
             'comp_off_ratio': '1',
             'approval_chain_id': None},
  'policy_hash': None,
  'note': 'The legacy overtime multiplier, if any, was copied into this draft; overtime stays off until it '
          'is reviewed (EC-MIG-04).'},
 {'type': 'LATE_PENALTY_POLICY',
  'name': 'Legacy late penalty',
  'status': 'DRAFT',
  'fields': {'free_late_marks': 0,
             'penalty_type': 'FIXED_AMOUNT',
             'late_marks_per_unit': 3,
             'unit_days': '0.5',
             'amount_per_late': None,
             'count_early_exit': False,
             'early_exit_grace_minutes': 15,
             'cap_days_per_period': '2',
             'exempt_if_regularized': True},
  'policy_hash': None,
  'note': 'Legacy payroll deducted a fixed amount per late mark; it is not applied unless you enter an '
          'amount in your currency and turn penalties on (EC-MIG-03).'}]


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "", "-") else text


def _legacy_value(kind: str, raw: Optional[str]) -> Tuple[bool, Any]:
    """(valid, value) of a JSON-encoded company_settings value, checked as the v2 key would be."""
    try:
        value = json.loads(raw) if raw is not None else None
    except ValueError:
        return False, raw
    if kind == "bool":
        return isinstance(value, bool), value
    valid = isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 240
    return valid, value


def _legacy_multiplier(raw: Optional[str]) -> Tuple[bool, Any]:
    """The legacy overtime multiplier as an OVERTIME_POLICY bucket multiplier (0 < m <= 10, 4 places)."""
    try:
        value = json.loads(raw) if raw is not None else None
    except ValueError:
        return False, raw
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return False, value
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return False, value
    if not number.is_finite():
        return False, value
    exponent = number.normalize().as_tuple().exponent
    places = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    if not Decimal(0) < number <= Decimal(10) or places > 4:
        return False, value
    return True, _decimal_text(number)


def _audit(bind, company_id: int, action: str, description: str, details: dict) -> None:
    bind.execute(sa.text(
        """
        INSERT INTO audit_logs (company_id, actor_type, actor_email, module, action, entity_type, entity_id,
                                description, details, created_at, updated_at)
        VALUES (:cid, 'system', 'migration 0011', :module, :action, 'company', :eid, :description,
                CAST(:details AS jsonb), now(), now())
        """
    ), {"cid": company_id, "module": MODULE, "action": action, "eid": str(company_id),
        "description": description[:500], "details": json.dumps(dict(details, migration=MARKER))})


def _expanding(sql: str, name: str):
    return sa.text(sql).bindparams(sa.bindparam(name, expanding=True))


def _apply_company(bind, company_id: int, start) -> dict:
    stored = dict(bind.execute(
        _expanding("SELECT key, value FROM company_settings WHERE company_id = :cid AND key IN :keys", "keys"),
        {"cid": company_id, "keys": [*LEGACY_KEYS, OVERTIME_KEY]},
    ).all())
    values = dict(LEGACY_VALUES)
    carried: Dict[str, Any] = {}
    skipped: Dict[str, Any] = {}
    for legacy, (key, kind) in LEGACY_KEYS.items():
        if legacy not in stored:
            continue
        valid, value = _legacy_value(kind, stored[legacy])
        if valid:
            values[key] = carried[legacy] = value
        else:
            skipped[legacy] = value
            logger.warning("Company %s: legacy %s=%r is invalid; the LEGACY_V1 value is used", company_id, legacy, value)
    configured = {k for (k,) in bind.execute(sa.text(
        "SELECT DISTINCT key FROM setting_values WHERE company_id = :cid AND scope_type = 'TENANT' "
        "AND status = 'ACTIVE'"), {"cid": company_id})}
    written = 0
    for key in sorted(values):
        if key in configured:
            continue
        bind.execute(sa.text(
            """
            INSERT INTO setting_values (company_id, key, scope_type, scope_id, value, locked, effective_from,
                                        status, reason, row_version, source, profile_version, created_at, updated_at)
            VALUES (:cid, :key, 'TENANT', NULL, CAST(:value AS jsonb), false, :start, 'ACTIVE', :reason, 1,
                    :source, :profile, now(), now())
            """
        ), {"cid": company_id, "key": key, "value": json.dumps(values[key]), "start": start, "reason": REASON,
            "source": SOURCE, "profile": PROFILE})
        written += 1
    taken = {t for (t,) in bind.execute(sa.text(
        "SELECT DISTINCT type FROM policies WHERE company_id = :cid AND status = 'ACTIVE'"), {"cid": company_id})}
    created = []
    for spec in LEGACY_POLICIES:
        if spec["type"] in taken:
            continue
        fields = json.loads(json.dumps(spec["fields"]))
        if spec["type"] == "OVERTIME_POLICY" and OVERTIME_KEY in stored:
            valid, multiplier = _legacy_multiplier(stored[OVERTIME_KEY])
            if valid:
                fields["buckets"][0]["multiplier"] = multiplier
                carried[OVERTIME_KEY] = multiplier
            else:
                skipped[OVERTIME_KEY] = multiplier
        policy_id = bind.execute(sa.text(
            "INSERT INTO policies (company_id, type, name, description, status, created_at, updated_at) "
            "VALUES (:cid, :type, :name, :note, 'ACTIVE', now(), now()) RETURNING id"
        ), {"cid": company_id, "type": spec["type"], "name": spec["name"], "note": spec["note"]}).scalar()
        active = spec["status"] == "ACTIVE"
        bind.execute(sa.text(
            """
            INSERT INTO policy_versions (company_id, policy_id, version, fields, effective_from, status, policy_hash,
                                         activated_at, created_at, updated_at)
            VALUES (:cid, :pid, 1, CAST(:fields AS jsonb), :start, :status, :hash, :activated_at, now(), now())
            """
        ), {"cid": company_id, "pid": policy_id, "fields": json.dumps(fields), "start": start,
            "status": spec["status"], "hash": spec["policy_hash"] if active else None,
            "activated_at": _now(bind) if active else None})
        bind.execute(sa.text(
            "INSERT INTO policy_assignments (company_id, policy_id, criteria, priority, effective_from, status, "
            "created_at, updated_at) VALUES (:cid, :pid, CAST(:criteria AS jsonb), 0, :start, 'ACTIVE', now(), now())"
        ), {"cid": company_id, "pid": policy_id, "criteria": json.dumps({"everyone": True}), "start": start})
        taken.add(spec["type"])
        created.append({"id": policy_id, "type": spec["type"], "name": spec["name"], "status": spec["status"]})
    bind.execute(sa.text("UPDATE companies SET payroll_profile_version = :p, payroll_profile_applied_at = now() "
                         "WHERE id = :cid"), {"p": PROFILE, "cid": company_id})
    stats = {"rows_written": written, "policies": created, "legacy_values": carried, "skipped_legacy_values": skipped,
             "profile": PROFILE, "source": SOURCE, "effective_from": start.isoformat()}
    _audit(bind, company_id, APPLIED,
           f"Applied the payroll configuration profile {PROFILE}: {written} settings and {len(created)} policies from "
           f"{start.isoformat()}, reproducing legacy payroll (migration 0011, decision I-14)", stats)
    return stats


def _now(bind):
    return bind.execute(sa.text("SELECT now()")).scalar()


def apply(bind) -> Dict[int, dict]:
    """Materialize LEGACY_V1 for every tenant without a payroll profile; returns per-company statistics."""
    companies = bind.execute(sa.text(
        "SELECT id, created_at::date FROM companies WHERE payroll_profile_version IS NULL ORDER BY id")).all()
    return {cid: _apply_company(bind, cid, start) for cid, start in companies}


def _revert_company(bind, company_id: int) -> dict:
    last_revert = bind.execute(sa.text(
        "SELECT max(id) FROM audit_logs WHERE company_id = :cid AND action = :a AND details->>'migration' = :m"
    ), {"cid": company_id, "a": REVERTED, "m": MARKER}).scalar() or 0
    rows = bind.execute(sa.text(
        "SELECT details FROM audit_logs WHERE company_id = :cid AND id > :after AND action = :a "
        "AND details->>'migration' = :m ORDER BY id"
    ), {"cid": company_id, "after": last_revert, "a": APPLIED, "m": MARKER}).all()
    stats = {"rows_deleted": 0, "policies_deleted": 0}
    if not rows:
        return stats
    stats["rows_deleted"] = bind.execute(sa.text(
        "DELETE FROM setting_values WHERE company_id = :cid AND source = :s"), {"cid": company_id, "s": SOURCE}).rowcount
    ids = [p["id"] for (details,) in rows for p in details.get("policies", [])]
    if ids:
        params = {"cid": company_id, "ids": ids}
        for table, column in (("policy_assignments", "policy_id"), ("policy_versions", "policy_id"), ("policies", "id")):
            deleted = bind.execute(
                _expanding(f"DELETE FROM {table} WHERE company_id = :cid AND {column} IN :ids", "ids"), params
            ).rowcount
            if table == "policies":
                stats["policies_deleted"] = deleted
    bind.execute(sa.text("UPDATE companies SET payroll_profile_version = NULL, payroll_profile_applied_at = NULL "
                         "WHERE id = :cid AND payroll_profile_version = :p"), {"cid": company_id, "p": PROFILE})
    _audit(bind, company_id, REVERTED, "Payroll configuration profile LEGACY_V1 rolled back (migration 0011 downgrade)",
           stats)
    return stats


def revert(bind) -> Dict[int, dict]:
    return {cid: _revert_company(bind, cid) for (cid,) in bind.execute(sa.text("SELECT id FROM companies ORDER BY id")).all()}


def upgrade() -> None:
    op.add_column('companies', sa.Column('payroll_profile_version', sa.String(20), nullable=True))
    op.add_column('companies', sa.Column('payroll_profile_applied_at', sa.DateTime(timezone=True), nullable=True))
    apply(op.get_bind())


def downgrade() -> None:
    revert(op.get_bind())
    op.drop_column('companies', 'payroll_profile_applied_at')
    op.drop_column('companies', 'payroll_profile_version')

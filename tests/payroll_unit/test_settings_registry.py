"""Settings registry (3.1 §2, §4, §5, §7; 02 §14; decisions I-13, I-14, I-16)."""

import importlib.util
import re
from datetime import date, time
from decimal import Decimal
from pathlib import Path

import pytest

from app.payroll import settings_registry as sr
from app.payroll.settings_registry import (
    ALIASES,
    PRECEDENCE,
    PROFILES,
    REGISTRY,
    UNSET,
    Candidate,
    Derived,
    EffectiveValue,
    LegalConstraint,
    Level,
    MoneyValue,
    SettingDef,
    SettingValidationError,
    change_requirements,
    cross_validate,
    daily_values,
    definition_json,
    effective_date_notice,
    profile_rows,
    profile_values,
    read_stored_value,
    register_pack_settings,
    resolve_chain,
    unregister_pack_settings,
    validate_value,
    validate_write,
)
from app.payroll.policy_types import AssignmentRule, EmployeeAttributes, starter_policies, validate_fields

BACKEND = Path(__file__).resolve().parents[2]
DOC = BACKEND.parent / "docs" / "payroll" / "03-payroll-features" / "01-settings-and-switches.md"
LEAVE_TYPES = [
    {"code": "casual", "name": "Casual Leave", "is_paid": True},
    {"code": "sick", "name": "Sick Leave", "is_paid": True},
    {"code": "unpaid", "name": "Unpaid Leave", "is_paid": False},
]
OT_MULTIPLIERS = [
    {"day_type": "WORKDAY", "multiplier": "1.5"},
    {"day_type": "WEEKLY_OFF", "multiplier": "2"},
    {"day_type": "HOLIDAY", "multiplier": "2"},
]


def _doc_rows() -> list[tuple[str, dict[str, str]]]:
    text = DOC.read_text(encoding="utf-8")
    body = text[text.index("## 4. Master switch"):text.index("## 6. Switch combinations")]
    rows: list[tuple[str, dict[str, str]]] = []
    header: list[str] | None = None
    for line in body.splitlines():
        if re.match(r"\| (Key|Switch key|Example key)", line):
            header = [c.strip() for c in line.strip("|").split("|")]
            continue
        if line.startswith("| Field"):
            header = None
            continue
        match = re.match(r"\| `([a-z][a-zA-Z0-9_.]+)`", line)
        if match and header is not None:
            cells = [c.strip() for c in line.strip("|").split("|")]
            rows.append((match.group(1), dict(zip(header, cells))))
    return rows


def _rules(*specs: tuple[str, int, dict]) -> list[AssignmentRule]:
    return [AssignmentRule(rule_id=rid, policy_id=rid, priority=prio, criteria=crit) for rid, prio, crit in specs]


def _codes(issues: list[sr.Issue]) -> list[str]:
    return [i.rule for i in issues]


@pytest.fixture
def zz_pack():
    defs = [
        SettingDef(
            key="payroll.statutory.ZZ.fund.enabled", type="bool", default=False, allowed_levels=(Level.LEGAL_ENTITY,),
            module="Payroll", section="Statutory & tax", label="ZZ fund",
            help="ON: the ZZ fund is applied. OFF: not applied (only where optional).", statutory_scheme="ZZ.fund",
        ),
        SettingDef(
            key="payroll.statutory.ZZ.fund.rate", type="decimal", default=Decimal("10"), allowed_levels=(),
            module="Payroll", section="Statutory & tax", label="ZZ fund rate",
            help="Read-only rate from the ZZ pack.", statutory_scheme="ZZ.fund",
        ),
    ]
    # ZZ keys registered earlier in the process (the real test-only ZZ pack) are set aside and restored after.
    before = [REGISTRY[key] for key in sr.pack_setting_keys() if key.startswith("payroll.statutory.ZZ.")]
    unregister_pack_settings("ZZ")
    register_pack_settings(defs)
    try:
        yield defs
    finally:
        unregister_pack_settings("ZZ")
        register_pack_settings(before)


# --------------------------------------------------------------------------- catalogue (3.1 §4, §5)


def test_every_key_of_3_1_sections_4_and_5_is_registered():
    """3.1 §4 and §5.1-§5.19: every key is declared; the split holiday switch keeps the old key as alias."""
    doc_keys = {key for key, _ in _doc_rows() if not key.startswith("payroll.statutory.")}
    missing = sorted(k for k in doc_keys if k not in REGISTRY and k not in ALIASES and "<" not in k)
    assert missing == []
    assert len(REGISTRY) >= 120
    assert all(f"documents.numbering.{t.lower()}.pattern" in REGISTRY for t in sr.NUMBERED_DOCUMENT_TYPES)


def test_allowed_levels_match_the_levels_column_of_3_1():
    abbr = {"T": "TENANT", "LE": "LEGAL_ENTITY", "B": "BRANCH", "PG": "PAY_GROUP", "E": "EMPLOYEE"}
    checked = 0
    for key, cells in _doc_rows():
        if "Levels" not in cells or key not in REGISTRY and key not in ALIASES:
            continue
        levels = re.sub(r"\(.*?\)", "", cells["Levels"])
        levels = levels.split(";")[-1].replace("target", "")
        expected = {abbr[x] for x in re.split(r"[ ,]+|or", levels) if x in abbr}
        assert {lv.value for lv in sr.definition(key).allowed_levels} == expected, key
        checked += 1
    assert checked > 90


@pytest.mark.parametrize(
    ("key", "default"),
    [
        ("payroll.enabled", False),
        ("payroll.attendance.enabled", False),
        ("payroll.leave.enabled", False),
        ("payroll.overtime.enabled", False),
        ("payroll.wage_inputs.enabled", True),
        ("payroll.arrears.auto_compute", True),
        ("payroll.bank.self_service_enabled", True),
        ("payroll.bank.require_verification", True),
        ("payroll.bank.exclude_unverified_from_payout", True),
        ("payroll.approval.separation_of_duties", True),
        ("payslip.email_notify", True),
        ("payslip.email_include_net_pay", False),
        ("payroll.pay_frequency", "MONTHLY"),
        ("payroll.calendar.monthly_start_day", 1),
        ("payroll.calendar.semi_monthly_split_day", 15),
        ("payroll.calendar.pay_day_rule", "LAST_WORKING_DAY"),
        ("payroll.calendar.pay_day_shift", "PREVIOUS_BUSINESS_DAY"),
        ("payroll.attendance_cycle.start_day", 26),
        ("payroll.cutoff.input_days_before_pay_day", 5),
        ("payroll.cutoff.approval_days_before_pay_day", 2),
        ("payroll.cutoff.payment_lead_business_days", 1),
        ("payroll.standard_hours_per_day", Decimal("8.0")),
        ("payroll.attendance.half_day_value", Decimal("0.5")),
        ("payroll.lop.proration_basis", "CALENDAR_DAYS"),
        ("payroll.lop.sandwich_rule", "OFF"),
        ("payroll.lop.zero_paid_days", "EXCEPTION"),
        ("payroll.leave.unapproved_at_cutoff", "EXCEPTION"),
        ("holiday.optional_quota_per_year", 2),
        ("attendance.grace_minutes", 15),
        ("payroll.fx.max_rate_age_hours", 24),
        ("payroll.fx.anomaly_threshold_percent", Decimal("5.0")),
        ("payroll.fx.conversion_date_rule", "PAY_DATE"),
        ("payroll.rounding.line_mode", "HALF_UP"),
        ("payroll.approval.variance_review_threshold_percent", Decimal("20")),
        ("payslip.scheduled_publish_time", time(9, 0)),
        ("payroll.payments.batch_max_items", 1000),
        ("payroll.bank.cooling_off_hours", 48),
        ("payroll.bank.verification_methods", ("FORMAT", "DOCUMENT_REVIEW")),
        ("payroll.bank.max_accounts_per_payee", 2),
        ("notify.payroll.approval_requested", ("IN_APP", "EMAIL")),
        ("notify.payroll.input_cutoff_reminder_days", 2),
        ("privacy.payroll_retention_years", 8),
        ("privacy.small_group_suppression_size", 5),
    ],
)
def test_platform_defaults_match_3_1(key, default):
    assert REGISTRY[key].default == default


def test_contextual_defaults_are_derived_markers():
    assert isinstance(REGISTRY["payroll.currency"].default, Derived)
    assert isinstance(REGISTRY["payroll.rounding.net_pay_increment"].default, Derived)
    assert sr.derive_default("payroll.rounding.net_pay_increment", {"currency_minor_unit": 0}) == Decimal("1")
    assert sr.derive_default("payroll.rounding.net_pay_increment", {"currency_minor_unit": 3}) == Decimal("0.001")
    assert sr.derive_default("ui.money.compact_notation", {"tenant_currency": "INR"}) == "INDIAN"
    with pytest.raises(SettingValidationError) as error:
        sr.derive_default("payroll.currency", {})
    assert error.value.code == "DEFAULT_UNRESOLVED"


def test_bank_file_format_defaults_to_generic_csv_g22():
    assert REGISTRY["payroll.payments.bank_file_format"].default == "GENERIC_CSV"


def test_protected_net_pay_is_unset_by_default_g23():
    defn = REGISTRY["payroll.deductions.min_net_pay_percent"]
    assert defn.default is None and defn.nullable
    assert validate_value(defn.key, None) is None


def test_holiday_switch_is_split_with_legacy_alias_i16():
    assert "payroll.holiday_pay.enabled" not in REGISTRY
    assert ALIASES["payroll.holiday_pay.enabled"] == "payroll.holiday_pay.non_salaried.enabled"
    for key in ("payroll.holiday_pay.non_salaried.enabled", "payroll.holiday_work_premium.enabled"):
        defn = REGISTRY[key]
        assert defn.default is False and defn.resolution_grain == "DAY"
        assert defn.ui["policy_type"] == "HOLIDAY_PAY_POLICY"
    assert "salaried" in REGISTRY["payroll.holiday_pay.non_salaried.enabled"].help
    assert sr.definition("payroll.holiday_pay.enabled").key == "payroll.holiday_pay.non_salaried.enabled"


def test_legacy_aliases_of_3_1_section_2_4():
    assert ALIASES["payrollSeparationOfDuties"] == "payroll.approval.separation_of_duties"
    assert ALIASES["graceTime"] == "attendance.grace_minutes"
    assert ALIASES["lunchIsPaid"] == "attendance.lunch_is_paid"


def test_resolution_grain_per_3_1_section_2_2():
    day = {
        "payroll.overtime.enabled", "payroll.holiday_pay.non_salaried.enabled", "payroll.holiday_work_premium.enabled",
        "payroll.weekly_off_pay.enabled", "payroll.shift_allowance.enabled", "payroll.task_pay.enabled",
        "payroll.late_penalty.enabled",
    }
    for key in day:
        assert REGISTRY[key].resolution_grain == "DAY", key
    for key in ("payroll.lop.proration_basis", "payroll.rounding.line_mode", "payroll.pay_frequency",
                "payroll.attendance.enabled", "payroll.leave.enabled", "payslip.show_ytd"):
        assert REGISTRY[key].resolution_grain == "PERIOD", key


def test_change_control_flags():
    for key in ("payroll.lop.proration_basis", "payroll.rounding.line_mode", "payroll.rounding.net_pay_increment",
                "payroll.overtime.enabled", "payroll.lop.zero_paid_days", "payroll.late_penalty.enabled",
                "payroll.approval.separation_of_duties", "payroll.approval.payment_dual_control",
                "payroll.bank.change_requires_approval"):
        assert REGISTRY[key].requires_approval, key
    assert [k for k, d in REGISTRY.items() if d.admin_only] == ["payroll.approval.separation_of_duties"]
    assert not REGISTRY["notify.payroll.bank_change_alert"].lockable
    assert REGISTRY["payroll.payments.funding_account_id"].sensitivity == "confidential"


def test_every_definition_is_complete():
    titles = {s.title for s in sr.SECTIONS}
    for defn in REGISTRY.values():
        assert defn.label and defn.help and defn.section in titles
        assert defn.ui["control"] and isinstance(defn.ui["order"], int)
        assert defn.support_status in sr.SUPPORT_STATUSES and defn.legal_sensitivity in sr.LEGAL_SENSITIVITIES
        if defn.type == "bool":
            assert "ON:" in defn.help and "OFF:" in defn.help, defn.key
        if defn.module == "Payroll":
            assert defn.fails_closed


def test_definition_json_is_json_ready():
    data = definition_json(REGISTRY["payroll.attendance.half_day_value"])
    assert data["default"] == "0.5" and data["constraints"]["choices"] == ["0", "0.5", "1"]
    assert data["supportStatus"] == "SUPPORTED" and data["decisionRequired"] is False
    assert definition_json(REGISTRY["payroll.overtime.enabled"])["legalSensitivity"] == "LEGAL_FLOOR"
    assert sr.registry_fingerprint() == sr.registry_fingerprint()


# --------------------------------------------------------------------------- validate_value (EC-CFG-09)


@pytest.mark.parametrize(
    ("key", "value", "code"),
    [
        ("payroll.enabled", 1, "INVALID_TYPE"),
        ("payroll.enabled", "true", "INVALID_TYPE"),
        ("payroll.calendar.monthly_start_day", True, "INVALID_TYPE"),
        ("payroll.calendar.monthly_start_day", "1", "INVALID_TYPE"),
        ("payroll.calendar.monthly_start_day", 32, "ABOVE_MAXIMUM"),
        ("payroll.calendar.monthly_start_day", 0, "BELOW_MINIMUM"),
        ("payroll.standard_hours_per_day", 8.0, "INVALID_TYPE"),
        ("payroll.standard_hours_per_day", 8, "INVALID_TYPE"),
        ("payroll.standard_hours_per_day", "1e1", "INVALID_DECIMAL"),
        ("payroll.standard_hours_per_day", "25", "ABOVE_MAXIMUM"),
        ("payroll.attendance.half_day_value", "0.25", "INVALID_CHOICE"),
        ("payroll.pay_frequency", "FORTNIGHTLY", "INVALID_CHOICE"),
        ("payroll.wage_inputs.require_approval_above", {"amount": 10, "currency": "INR"}, "INVALID_TYPE"),
        ("payroll.wage_inputs.require_approval_above", {"amount": "10", "currency": None}, "INVALID_CURRENCY"),
        ("payroll.wage_inputs.require_approval_above", "10", "INVALID_MONEY"),
        ("payroll.bank.verification_methods", ["FORMAT", "FORMAT"], "DUPLICATE_ITEM"),
        ("payroll.bank.verification_methods", [], "TOO_FEW_ITEMS"),
        ("payroll.bank.verification_methods", "FORMAT", "INVALID_TYPE"),
        ("payslip.scheduled_publish_time", "9:00", "INVALID_FORMAT"),
        ("payroll.calendar.biweekly_anchor_date", "2026-02-30", "INVALID_FORMAT"),
        ("payroll.currency", "inr", "INVALID_FORMAT"),
        ("payroll.lop.proration_basis", None, "VALUE_REQUIRED"),
        ("notify.payroll.bank_change_alert", False, "FIXED_SETTING"),
        ("documents.numbering.payslip.pattern", "PS/{FY}", "INVALID_FORMAT"),
    ],
)
def test_validate_value_is_strict_and_fails_closed(key, value, code):
    with pytest.raises(SettingValidationError) as error:
        validate_value(key, value)
    assert error.value.code == code


def test_validate_value_normalizes():
    assert validate_value("payroll.standard_hours_per_day", "7.5") == Decimal("7.5")
    assert validate_value("payroll.wage_inputs.require_approval_above", {"amount": "0", "currency": None}) == MoneyValue(
        Decimal("0"), None)
    assert validate_value("payroll.bank.verification_methods", ["PENNY_DROP", "FORMAT"]) == ("FORMAT", "PENNY_DROP")
    assert validate_value("payslip.scheduled_publish_time", "18:30") == time(18, 30)
    assert validate_value("payroll.calendar.biweekly_anchor_date", "2026-01-05") == date(2026, 1, 5)
    assert validate_value("payroll.calendar.biweekly_anchor_date", None) is None
    assert validate_value("graceTime", 10) == 10
    assert validate_value("documents.numbering.payslip.pattern", "PS/{LE}/{FY}/{SEQ:6}") == "PS/{LE}/{FY}/{SEQ:6}"
    with pytest.raises(SettingValidationError) as error:
        validate_value("payroll.unknown.key", True)
    assert error.value.code == "UNKNOWN_SETTING"


def test_ec_cfg_09_invalid_stored_payroll_value_is_an_error_not_a_default():
    read = read_stored_value("payroll.lop.proration_basis", "FIXED_31")
    assert not read.ok and read.value is UNSET and not read.fell_back
    issue = read.issue()
    assert issue is not None and issue.rule == "CONFIG_INVALID" and issue.severity == "ERROR"
    resolution = resolve_chain([Candidate(Level.TENANT, "FIXED_31")], key="payroll.lop.proration_basis")
    assert resolution.value is UNSET and not resolution.ok


def test_ec_cfg_09_valid_value_and_non_payroll_fallback():
    assert read_stored_value("payroll.lop.proration_basis", "FIXED_30").value == "FIXED_30"
    read = read_stored_value("attendance.grace_minutes", 999)
    assert read.fell_back and read.value == 15
    assert read.issue().severity == "WARNING"


# --------------------------------------------------------------------------- resolution (02 §14)


def test_precedence_is_most_specific_first():
    assert PRECEDENCE[0] is Level.EMPLOYEE and PRECEDENCE[-1] is Level.PLATFORM


def test_ec_cfg_01_pay_group_beats_branch_and_branch_is_shadowed():
    resolution = resolve_chain(
        [Candidate(Level.BRANCH, 20), Candidate(Level.PAY_GROUP, 10), Candidate(Level.TENANT, 15)]
    )
    assert resolution.value == 10 and resolution.level is Level.PAY_GROUP
    shadowed = {(s.candidate.level, s.reason) for s in resolution.shadowed}
    assert (Level.BRANCH, "OVERRIDDEN") in shadowed and (Level.TENANT, "OVERRIDDEN") in shadowed


def test_ec_cfg_01_without_a_narrower_value_the_broader_one_applies():
    resolution = resolve_chain([Candidate(Level.BRANCH, 20)], key="attendance.grace_minutes")
    assert resolution.value == 20 and resolution.level is Level.BRANCH
    default = resolve_chain([], key="attendance.grace_minutes")
    assert default.value == 15 and default.level is Level.PLATFORM


def test_ec_cfg_01_levels_the_key_does_not_allow_are_ignored():
    resolution = resolve_chain(
        [Candidate(Level.PAY_GROUP, "WORKING_DAYS"), Candidate(Level.EMPLOYEE, "FIXED_30")],
        key="payroll.lop.proration_basis",
    )
    assert resolution.value == "WORKING_DAYS"
    assert any(s.reason == "LEVEL_NOT_ALLOWED" for s in resolution.shadowed)


def test_ec_cfg_04_tenant_lock_wins_over_narrower_override():
    resolution = resolve_chain(
        [Candidate(Level.TENANT, True, locked=True), Candidate(Level.LEGAL_ENTITY, False)],
        key="payroll.approval.payment_dual_control",
    )
    assert resolution.value is True and resolution.level is Level.TENANT and resolution.locked
    assert [(s.candidate.level, s.reason) for s in resolution.shadowed] == [(Level.LEGAL_ENTITY, "LOCKED_ABOVE")]


def test_ec_cfg_04_most_general_lock_wins_and_unlocked_does_not():
    two_locks = resolve_chain(
        [Candidate(Level.TENANT, 30, locked=True), Candidate(Level.LEGAL_ENTITY, 20, locked=True),
         Candidate(Level.PAY_GROUP, 10)],
    )
    assert two_locks.value == 30
    unlocked = resolve_chain([Candidate(Level.TENANT, 30), Candidate(Level.PAY_GROUP, 10)])
    assert unlocked.value == 10 and not unlocked.locked


def test_resolve_chain_rejects_duplicate_levels():
    with pytest.raises(SettingValidationError) as error:
        resolve_chain([Candidate(Level.TENANT, 1), Candidate(Level.TENANT, 2)])
    assert error.value.code == "DUPLICATE_LEVEL"


def test_resolve_chain_derives_contextual_default():
    resolution = resolve_chain([], key="payroll.currency", derive_context={"entity_base_currency": "AED"})
    assert resolution.value == "AED"
    missing = resolve_chain([], key="payroll.currency", derive_context={})
    assert missing.value is UNSET and missing.issues[0].rule == "CONFIG_INVALID"


def test_ec_cfg_02_period_grain_takes_the_period_end_value():
    rows = [EffectiveValue(date(2026, 1, 1), date(2026, 11, 19), "CALENDAR_DAYS"),
            EffectiveValue(date(2026, 11, 20), None, "WORKING_DAYS")]
    days = daily_values(rows, date(2026, 11, 1), date(2026, 11, 30), grain="PERIOD")
    assert set(days.values()) == {"WORKING_DAYS"} and len(days) == 30
    assert effective_date_notice("PERIOD", date(2026, 11, 20), date(2026, 11, 1), date(2026, 11, 30),
                                 period_label="November") == "Applies to the whole November period"


def test_ec_cfg_02_day_grain_changes_mid_period_ec_cfg_08():
    rows = [EffectiveValue(date(2026, 1, 1), date(2026, 11, 19), True),
            EffectiveValue(date(2026, 11, 20), None, False)]
    days = daily_values(rows, date(2026, 11, 1), date(2026, 11, 30), grain=REGISTRY["payroll.overtime.enabled"].resolution_grain)
    assert days[date(2026, 11, 19)] is True and days[date(2026, 11, 20)] is False
    assert effective_date_notice("DAY", date(2026, 11, 20), date(2026, 11, 1), date(2026, 11, 30)) == \
        "Applies from 20 November 2026"
    assert effective_date_notice("DAY", date(2026, 12, 2), date(2026, 11, 1), date(2026, 11, 30)) is None


def test_overlapping_effective_rows_are_a_data_error():
    rows = [EffectiveValue(date(2026, 1, 1), None, 1), EffectiveValue(date(2026, 6, 1), None, 2)]
    with pytest.raises(SettingValidationError) as error:
        sr.value_on(rows, date(2026, 7, 1))
    assert error.value.code == "OVERLAPPING_ROWS"


def test_change_requirements_ec_cfg_03_and_ec_cfg_12():
    change = change_requirements(
        "payroll.lop.proration_basis", date(2026, 10, 1), change_approval_enabled=True,
        finalized_through=date(2026, 10, 31), open_period=(date(2026, 11, 1), date(2026, 11, 30)),
    )
    assert change.status == "PENDING_APPROVAL" and change.reason_required and change.retro_preview_required
    assert change.impact_preview_required
    plain = change_requirements("payslip.show_ytd", date(2026, 12, 1), change_approval_enabled=True)
    assert plain.status == "ACTIVE" and not plain.reason_required and not plain.impact_preview_required


# --------------------------------------------------------------------------- writes (EC-CFG-11, -19, -20)


def test_ec_cfg_11_non_admin_cannot_change_admin_only_key():
    with pytest.raises(SettingValidationError) as error:
        validate_write("payrollSeparationOfDuties", False, level=Level.TENANT, actor_is_admin=False, current=True)
    assert error.value.code == "ADMIN_ONLY"
    with pytest.raises(SettingValidationError):
        validate_write("payroll.approval.separation_of_duties", True, level=Level.TENANT, locked=True,
                       actor_is_admin=False, current=True, current_locked=False)


def test_ec_cfg_11_unchanged_value_is_not_an_attempt_and_admins_may_change():
    assert validate_write("payroll.approval.separation_of_duties", True, level="TENANT", actor_is_admin=False,
                          current=True, current_locked=False) is True
    assert validate_write("payroll.approval.separation_of_duties", False, level="TENANT", actor_is_admin=True) is False


def test_write_rejects_level_lock_and_unavailable_feature():
    with pytest.raises(SettingValidationError) as error:
        validate_write("payroll.pay_frequency", "WEEKLY", level=Level.TENANT)
    assert error.value.code == "LEVEL_NOT_ALLOWED"
    with pytest.raises(SettingValidationError) as error:
        validate_write("notify.payroll.bank_change_alert", True, level=Level.TENANT)
    assert error.value.code == "FIXED_SETTING"
    with pytest.raises(SettingValidationError) as error:
        validate_write("payroll.accounting.enabled", True, level=Level.TENANT)
    assert error.value.code == "FEATURE_NOT_AVAILABLE"
    assert validate_write("payroll.accounting.enabled", False, level=Level.TENANT) is False


def test_ec_cfg_19_mandatory_scheme_is_locked_on(zz_pack):
    law = LegalConstraint("payroll.statutory.ZZ.fund.enabled", "ZZ Fund Act s.4", required_value=True)
    with pytest.raises(SettingValidationError) as error:
        validate_write("payroll.statutory.ZZ.fund.enabled", False, level=Level.LEGAL_ENTITY, legal_limits=[law])
    assert error.value.code == "STATUTORY_LOCK" and "ZZ Fund Act" in error.value.message
    resolution = resolve_chain([Candidate(Level.LEGAL_ENTITY, False)], key="payroll.statutory.ZZ.fund.enabled",
                               legal_limits=[law])
    assert resolution.value is True and resolution.locked and resolution.legal_source == "ZZ Fund Act s.4"
    assert [i.rule for i in resolution.issues] == ["V-10"]
    assert "V-10" in _codes(cross_validate({"payroll.statutory.ZZ.fund.enabled": False}, pack_constraints=[law]))


def test_ec_cfg_19_switching_a_mandatory_scheme_on_is_accepted(zz_pack):
    law = LegalConstraint("payroll.statutory.ZZ.fund.enabled", "ZZ Fund Act s.4", required_value=True)
    assert validate_write("payroll.statutory.ZZ.fund.enabled", True, level=Level.LEGAL_ENTITY, legal_limits=[law])
    assert "V-10" not in _codes(cross_validate({"payroll.statutory.ZZ.fund.enabled": True}, pack_constraints=[law]))


def test_ec_cfg_20_pack_owned_values_and_legal_minimums_cannot_be_lowered(zz_pack):
    with pytest.raises(SettingValidationError) as error:
        validate_write("payroll.statutory.ZZ.fund.rate", "5", level=Level.LEGAL_ENTITY)
    assert error.value.code == "PACK_OWNED"
    floor = LegalConstraint("privacy.payroll_retention_years", "ZZ Records Act", minimum=10)
    with pytest.raises(SettingValidationError) as error:
        validate_write("privacy.payroll_retention_years", 8, level=Level.TENANT, legal_limits=[floor])
    assert error.value.code == "LEGAL_MINIMUM" and error.value.details["apply_legal_minimum"] == "10"


def test_ec_cfg_20_values_at_or_above_the_legal_minimum_are_accepted():
    floor = LegalConstraint("privacy.payroll_retention_years", "ZZ Records Act", minimum=10)
    assert validate_write("privacy.payroll_retention_years", 10, level=Level.TENANT, legal_limits=[floor]) == 10
    assert validate_write("privacy.payroll_retention_years", 12, level=Level.TENANT, legal_limits=[floor]) == 12


def test_register_pack_settings_rules(zz_pack):
    assert REGISTRY["payroll.statutory.ZZ.fund.enabled"].legal_sensitivity == "STATUTORY"
    assert register_pack_settings(zz_pack) == ()
    bad = SettingDef(key="payroll.zz.fund", type="bool", default=False, allowed_levels=(Level.LEGAL_ENTITY,),
                     module="Payroll", section="Statutory & tax", label="x", help="ON: x OFF: y", statutory_scheme="ZZ.x")
    with pytest.raises(SettingValidationError) as error:
        register_pack_settings([bad])
    assert error.value.code == "INVALID_DEFINITION"
    tenant_level = SettingDef(key="payroll.statutory.ZZ.other.enabled", type="bool", default=False,
                              allowed_levels=(Level.TENANT,), module="Payroll", section="Statutory & tax", label="x",
                              help="ON: x OFF: y", statutory_scheme="ZZ.other")
    with pytest.raises(SettingValidationError):
        register_pack_settings([tenant_level])
    changed = SettingDef(**{**{f: getattr(zz_pack[0], f) for f in zz_pack[0].__dataclass_fields__}, "label": "Other"})
    with pytest.raises(SettingValidationError) as error:
        register_pack_settings([changed])
    assert error.value.code == "DUPLICATE_SETTING"


def test_unregister_pack_settings_removes_keys(zz_pack):
    assert "payroll.statutory.ZZ.fund.rate" in sr.pack_setting_keys()
    unregister_pack_settings("ZZ")
    assert "payroll.statutory.ZZ.fund.rate" not in REGISTRY
    register_pack_settings(zz_pack)


# --------------------------------------------------------------------------- cross-setting rules (3.1 §7)


def test_default_configuration_has_no_errors():
    issues = cross_validate({})
    assert [i for i in issues if i.severity == "ERROR"] == []


def test_ec_cfg_15_v01_late_penalty_requires_attendance():
    issues = cross_validate({"payroll.late_penalty.enabled": True, "payroll.attendance.enabled": False})
    v01 = [i for i in issues if i.rule == "V-01"]
    assert v01 and v01[0].severity == "ERROR" and v01[0].blocks_save and "attendance" in v01[0].message


def test_ec_cfg_15_v01_satisfied_when_attendance_is_on_or_penalties_off():
    assert "V-01" not in _codes(cross_validate({"payroll.late_penalty.enabled": True, "payroll.attendance.enabled": True}))
    assert "V-01" not in _codes(cross_validate({"payroll.late_penalty.enabled": False}))


def test_v02_attendance_overtime_source_requires_attendance():
    policy = ("OVERTIME_POLICY", "Factory OT", {"source": "ATTENDANCE_AUTO", "buckets": OT_MULTIPLIERS})
    base = {"payroll.overtime.enabled": True}
    assert "V-02" in _codes(cross_validate(base, policies=[policy]))
    assert "V-02" not in _codes(cross_validate({**base, "payroll.attendance.enabled": True}, policies=[policy]))
    approved = ("OVERTIME_POLICY", "Staff OT", {"buckets": OT_MULTIPLIERS})
    assert "V-02" not in _codes(cross_validate(base, policies=[approved]))


def test_v03_adjacent_day_eligibility_requires_attendance():
    policy = ("HOLIDAY_PAY_POLICY", "Hourly", {"eligibility_adjacent_days": "BOTH"})
    assert "V-03" in _codes(cross_validate({"payroll.holiday_pay.non_salaried.enabled": True}, policies=[policy]))
    assert "V-03" in _codes(cross_validate({"payroll.holiday_pay.enabled": True}, policies=[policy]))
    assert "V-03" not in _codes(cross_validate(
        {"payroll.holiday_pay.non_salaried.enabled": True, "payroll.attendance.enabled": True}, policies=[policy]))
    premium = ("HOLIDAY_PAY_POLICY", "Premium", {"eligibility_adjacent_days": "BOTH", "holiday_work_multiplier": "1"})
    assert "V-03" not in _codes(cross_validate({"payroll.holiday_work_premium.enabled": True}, policies=[premium]))


def test_v04_hours_basis_requires_attendance():
    assert "V-04" in _codes(cross_validate({"payroll.lop.proration_basis": "HOURS"}))
    assert "V-04" not in _codes(cross_validate({"payroll.lop.proration_basis": "HOURS", "payroll.attendance.enabled": True}))


def test_v05_custom_days_required_exactly_for_custom_basis():
    assert "V-05" in _codes(cross_validate({"payroll.lop.proration_basis": "CUSTOM_DAYS"}))
    assert "V-05" not in _codes(cross_validate({"payroll.lop.proration_basis": "CUSTOM_DAYS", "payroll.lop.custom_days": 26}))


def test_v06_custom_attendance_cycle_needs_start_day():
    assert "V-06" in _codes(cross_validate({"payroll.attendance_cycle.mode": "CUSTOM_START_DAY",
                                             "payroll.attendance_cycle.start_day": None}))
    assert "V-06" not in _codes(cross_validate({"payroll.attendance_cycle.mode": "CUSTOM_START_DAY"}))


def test_v07_deadlines_in_order():
    assert "V-07" not in _codes(cross_validate({}))
    assert "V-07" in _codes(cross_validate({"payroll.cutoff.input_days_before_pay_day": 2}))
    assert "V-07" in _codes(cross_validate({"payroll.cutoff.payment_lead_business_days": 2}))


def test_v08_task_pay_requires_tasks_module():
    assert "V-08" in _codes(cross_validate({"payroll.task_pay.enabled": True}))
    assert "V-08" not in _codes(cross_validate({"payroll.task_pay.enabled": True, "tasks.enabled": True}))


def test_v09_pdf_attachment_requires_protection():
    assert "V-09" in _codes(cross_validate({"payslip.email_attach_pdf": True}))
    assert "V-09" not in _codes(cross_validate({"payslip.email_attach_pdf": True, "payslip.pdf_protection": "EMPLOYEE_SECRET"}))


def test_v11_switch_on_requires_every_employee_to_resolve_a_policy():
    employees = [EmployeeAttributes(1, branch_id=10), EmployeeAttributes(2, branch_id=20)]
    rules = {"ATTENDANCE_PAY_POLICY": _rules((1, 5, {"branch_ids": [10]})),
             "PAYSLIP_TEMPLATE": _rules((9, 0, {}))}
    issues = cross_validate({"payroll.attendance.enabled": True}, rules=rules, employees=employees)
    v11 = [i for i in issues if i.rule == "V-11"]
    assert len(v11) == 1 and v11[0].details["employee_ids"] == [2] and not v11[0].blocks_save
    rules["ATTENDANCE_PAY_POLICY"] += _rules((2, 0, {"everyone": True}))
    assert "V-11" not in _codes(cross_validate({"payroll.attendance.enabled": True}, rules=rules, employees=employees))


def test_v12_same_priority_rules_matching_one_employee():
    employees = [EmployeeAttributes(1, branch_id=10, grade_id=3)]
    rules = {"OVERTIME_POLICY": _rules((1, 10, {"branch_ids": [10]}), (2, 10, {"grade_ids": [3]})),
             "PAYSLIP_TEMPLATE": _rules((9, 0, {}))}
    v12 = [i for i in cross_validate({}, rules=rules, employees=employees) if i.rule == "V-12"]
    assert v12 and v12[0].details["employee_ids"] == [1]
    rules["OVERTIME_POLICY"] = _rules((1, 10, {"branch_ids": [10]}), (2, 20, {"grade_ids": [3]}))
    assert "V-12" not in _codes(cross_validate({}, rules=rules, employees=employees))


def test_v13_net_pay_increment_multiple_of_minor_unit():
    assert "V-13" not in _codes(cross_validate({"payroll.rounding.net_pay_increment": "1", "payroll.currency": "INR"}))
    assert "V-13" in _codes(cross_validate({"payroll.rounding.net_pay_increment": "0.5", "payroll.currency": "JPY"}))
    assert "V-13" in _codes(cross_validate({"payroll.rounding.net_pay_increment": "0.001", "payroll.currency": "INR"}))
    assert "V-13" not in _codes(cross_validate({"payroll.rounding.net_pay_increment": "0.005", "payroll.currency": "KWD"}))
    assert "CONFIG_INVALID" in _codes(cross_validate({"payroll.rounding.net_pay_increment": "101"}))


def test_v14_pay_date_before_period_end_warns():
    early = {"payroll.calendar.pay_day_rule": "DAY_OF_MONTH", "payroll.calendar.pay_day_value": 25}
    v14 = [i for i in cross_validate(early) if i.rule == "V-14"]
    assert v14 and v14[0].severity == "WARNING" and "26th-25th" in v14[0].message
    assert "V-14" not in _codes(cross_validate({**early, "payroll.attendance_cycle.mode": "CUSTOM_START_DAY"}))
    assert "V-14" not in _codes(cross_validate({}))
    assert "V-14" in _codes(cross_validate({}, periods=[(date(2026, 11, 30), date(2026, 11, 28))]))


def test_v15_unverified_accounts_paid_warning():
    issues = cross_validate({"payroll.bank.exclude_unverified_from_payout": False})
    assert [(i.rule, i.severity) for i in issues if i.rule == "V-15"] == [("V-15", "WARNING")]
    assert "V-15" not in _codes(cross_validate({"payroll.bank.exclude_unverified_from_payout": False,
                                                 "payroll.bank.require_verification": False}))


def test_v16_policy_multiplier_below_pack_minimum_with_source():
    law = LegalConstraint("OVERTIME_POLICY.buckets.multiplier", "ZZ Labour Code s.9", minimum=Decimal("2"),
                          match={"day_type": "HOLIDAY"})
    low = ("OVERTIME_POLICY", "Factory OT", {"buckets": OT_MULTIPLIERS[:2] + [{"day_type": "HOLIDAY", "multiplier": "1.5"}]})
    v16 = [i for i in cross_validate({}, pack_constraints=[law], policies=[low]) if i.rule == "V-16"]
    assert v16 and v16[0].details["source"] == "ZZ Labour Code s.9" and v16[0].details["apply_legal_minimum"] == "2"
    ok = ("OVERTIME_POLICY", "Factory OT", {"buckets": OT_MULTIPLIERS[:2] + [{"day_type": "HOLIDAY", "multiplier": "2.5"}]})
    assert "V-16" not in _codes(cross_validate({}, pack_constraints=[law], policies=[ok]))


def test_v16_setting_below_pack_minimum():
    law = LegalConstraint("payroll.deductions.min_net_pay_percent", "ZZ Wages Act", minimum=Decimal("50"))
    assert "V-16" in _codes(cross_validate({"payroll.deductions.min_net_pay_percent": "40"}, pack_constraints=[law]))
    assert "V-16" not in _codes(cross_validate({"payroll.deductions.min_net_pay_percent": "50"}, pack_constraints=[law]))


def test_policies_in_force_must_be_complete_for_activation():
    draft_like = ("OVERTIME_POLICY", "Blank OT", {})
    issues = cross_validate({"payroll.overtime.enabled": True}, policies=[draft_like])
    assert [i.details["code"] for i in issues if i.rule == "POLICY_INVALID"] == ["FIELD_REQUIRED_FOR_ACTIVATION"]


def test_protected_net_required_for_loans_and_advances_g23():
    issues = cross_validate({"payroll.loans.enabled": True})
    assert [(i.rule, i.severity) for i in issues if i.keys[0] == "payroll.loans.enabled"] == [
        ("PROTECTED_NET_REQUIRED", "ERROR")]
    assert "PROTECTED_NET_REQUIRED" not in _codes(cross_validate(
        {"payroll.loans.enabled": True, "payroll.advances.enabled": True,
         "payroll.deductions.min_net_pay_percent": "60", "payroll.arrears.recovery_mode": "MANUAL"}))
    warning = [i for i in cross_validate({}) if i.rule == "PROTECTED_NET_REQUIRED"]
    assert [i.severity for i in warning] == ["WARNING"]


def test_feature_not_available_cannot_be_on():
    assert "FEATURE_NOT_AVAILABLE" in _codes(cross_validate({"payroll.accounting.enabled": True}))
    assert "FEATURE_NOT_AVAILABLE" not in _codes(cross_validate({"payroll.accounting.enabled": False}))


def test_invalid_and_unknown_values_are_reported_not_defaulted():
    issues = cross_validate({"payroll.late_penalty.enabled": "yes", "payroll.nope": 1,
                             "graceTime": 10, "attendance.grace_minutes": 12})
    assert {"CONFIG_INVALID", "UNKNOWN_SETTING"} <= set(_codes(issues))
    assert "V-01" not in _codes(issues)


# --------------------------------------------------------------------------- profiles (I-13, I-14)

STARTER_ON = {
    "payroll.enabled", "payroll.attendance.enabled", "payroll.leave.enabled", "payroll.wage_inputs.enabled",
    "payroll.arrears.auto_compute", "payroll.reimbursements.enabled", "tasks.enabled", "payroll.task_pay.enabled",
    "payroll.bank.self_service_enabled", "payroll.bank.require_verification",
    "payroll.bank.exclude_unverified_from_payout", "payroll.approval.separation_of_duties", "payslip.email_notify",
}
STARTER_OFF_WITH_DECISION = {
    "payroll.overtime.enabled", "payroll.holiday_pay.non_salaried.enabled", "payroll.holiday_work_premium.enabled",
    "payroll.weekly_off_pay.enabled", "payroll.shift_allowance.enabled", "payroll.late_penalty.enabled",
    "payroll.incentives.enabled", "payroll.loans.enabled", "payroll.advances.enabled",
    "payroll.leave.encashment.enabled", "payroll.accounting.enabled", "payroll.bank.split_payments_enabled",
}


def test_starter_v1_switches_i13():
    values = profile_values("STARTER_V1", leave_types=LEAVE_TYPES)
    safety_and_display = {
        "payroll.approval.payment_dual_control", "payroll.approval.step_up_for_finalize",
        "payroll.fx.manual_rate_requires_approval", "payroll.payments.reissue_requires_approval",
        "payroll.bank.change_requires_approval", "notify.payroll.payment_status_to_employee",
        "leave.half_day.enabled", "payslip.queries_enabled", "payslip.show_attendance_summary",
        "payslip.show_bank_masked", "payslip.show_leave_balances", "payslip.show_ytd",
        # LH WI-1.10 defaults (holiday calendar publication, not pay).
        "holiday.provisional_counts", "holiday.publish.notify_employees",
        # AS-M3: punching stays on (today's behaviour); it changes no pay by itself.
        "attendance.capture.enabled",
        # Team attendance: the manager tools stay usable once a tenant switches them on (no pay effect by themselves).
        "attendance.team.proxy_punch.allow_manual_location", "attendance.team.proxy_punch.reason_required",
        "attendance.team.edit_enabled",
    }
    assert {k for k, v in values.items() if v is True} == STARTER_ON | safety_and_display
    for key in STARTER_OFF_WITH_DECISION:
        assert values[key] is False and REGISTRY[key].decision_required, key
    assert REGISTRY["payroll.attendance.enabled"].decision_required
    assert values["payroll.lop.proration_basis"] == "CALENDAR_DAYS"
    assert values["payroll.attendance.half_day_value"] == Decimal("0.5")
    assert values["payroll.lop.sandwich_rule"] == "OFF" and values["payroll.lop.zero_paid_days"] == "EXCEPTION"
    assert values["payroll.arrears.recovery_mode"] == "MANUAL"
    assert values["payroll.deductions.min_net_pay_percent"] is None
    assert values["payslip.email_include_net_pay"] is False
    assert values["payroll.wage_inputs.require_approval_above"] == MoneyValue(Decimal("0"), None)
    assert not any(k.startswith("payroll.statutory.") for k in values)


def test_starter_v1_is_the_single_source_of_new_tenant_defaults():
    values = profile_values("STARTER_V1", leave_types=())
    for key, defn in REGISTRY.items():
        if key in values:
            assert defn.new_tenant_default == values[key], key
        else:
            assert defn.new_tenant_default is UNSET, key
    assert sr.new_tenant_settings() == {k: values[k] for k in (d.key for d in sr.definitions()) if k in values}


def test_starter_v1_materializes_every_tenant_key_ec_cfg_23():
    values = profile_values("STARTER_V1", leave_types=())
    for key, defn in REGISTRY.items():
        if Level.TENANT in defn.allowed_levels and not isinstance(defn.default, Derived):
            assert key in values, key


def test_starter_v1_rows_carry_locks_and_source():
    rows = {row.key: row for row in profile_rows("STARTER_V1", leave_types=LEAVE_TYPES)}
    assert rows["payroll.approval.separation_of_duties"].locked
    assert rows["payroll.approval.payment_dual_control"].locked
    assert not rows["payroll.enabled"].locked
    assert {row.source for row in rows.values()} == {"STARTER_PROFILE"}
    assert {row.profile_version for row in rows.values()} == {"STARTER_V1"}
    assert rows["payroll.attendance.half_day_value"].value == "0.5"
    assert rows["payroll.wage_inputs.require_approval_above"].value == {"amount": "0", "currency": None}


def test_starter_v1_configuration_is_consistent():
    values = profile_values("STARTER_V1", leave_types=LEAVE_TYPES)
    policies = [(p.type, p.name, p.fields) for p in starter_policies("STARTER_V1", leave_types=LEAVE_TYPES)
                if p.status == "ACTIVE"]
    assert cross_validate(values, policies=policies) == []


def test_legacy_v1_reproduces_legacy_payroll_i14():
    values = profile_values("LEGACY_V1", leave_types=(), legacy_values={"payrollSeparationOfDuties": False,
                                                                        "graceTime": 10, "pf": 12.0, "theme": "dark"})
    assert values["payroll.enabled"] is True and values["payroll.attendance.enabled"] is True
    assert values["payroll.lop.proration_basis"] == "WORKING_DAYS"
    assert values["payroll.leave.enabled"] is False and values["payroll.overtime.enabled"] is False
    assert values["payroll.wage_inputs.enabled"] is True
    assert values["payroll.approval.separation_of_duties"] is False
    assert values["attendance.grace_minutes"] == 10
    assert values["payroll.late_penalty.enabled"] is False
    rows = profile_rows("LEGACY_V1", leave_types=(), legacy_values={"payrollSeparationOfDuties": True})
    assert {row.source for row in rows} == {"LEGACY_MIGRATION"} and not any(row.locked for row in rows)
    assert [i for i in cross_validate(values) if i.severity == "ERROR"] == []
    assert len(PROFILES["LEGACY_V1"].acknowledgments) == 3


def test_legacy_v1_defaults_separation_of_duties_on_and_rejects_bad_legacy_values():
    assert profile_values("LEGACY_V1", leave_types=())["payroll.approval.separation_of_duties"] is True
    with pytest.raises(SettingValidationError) as error:
        profile_values("LEGACY_V1", leave_types=(), legacy_values={"graceTime": 999})
    assert error.value.code == "LEGACY_VALUE_INVALID"
    with pytest.raises(SettingValidationError) as error:
        profile_values("LEGACY_V1", leave_types=(), legacy_values={"payrollSeparationOfDuties": "yes"})
    assert error.value.code == "LEGACY_VALUE_INVALID"


def test_profile_errors():
    with pytest.raises(SettingValidationError) as error:
        profile_values("STARTER_V1", leave_types=(), legacy_values={"graceTime": 10})
    assert error.value.code == "LEGACY_VALUES_NOT_ALLOWED"
    with pytest.raises(SettingValidationError) as error:
        profile_values("STARTER_V9", leave_types=())
    assert error.value.code == "UNKNOWN_PROFILE"
    assert sr.profile_locks("LEGACY_V1") == frozenset()
    assert len(sr.profile_policies("STARTER_V1", leave_types=LEAVE_TYPES)) == len(PROFILES["STARTER_V1"].policies)


def test_profiles_never_switch_on_statutory_or_unimplemented_features():
    for profile in PROFILES.values():
        for key, value in profile.values.items():
            assert not key.startswith("payroll.statutory.")
            if REGISTRY[key].support_status == "NOT_IMPLEMENTED":
                assert value is not True


# --------------------------------------------------------------------------- generated frontend file


def _export_module():
    spec = importlib.util.spec_from_file_location("export_settings", BACKEND / "scripts" / "export_settings.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_export_settings_is_deterministic_and_complete():
    module = _export_module()
    first, second = module.render(), module.render()
    assert first == second
    for key in REGISTRY:
        if not key.startswith("payroll.statutory."):
            assert f'"{key}"' in first
    assert "SETTING_ALIASES" in first and "POLICY_TYPES" in first and "SETTINGS_PROFILES" in first


def test_generated_typescript_is_up_to_date():
    generated = BACKEND.parent / "hrms-frontend" / "src" / "lib" / "settings.generated.ts"
    assert generated.read_text(encoding="utf-8") == _export_module().render(), "run scripts/export_settings.py"


def test_policy_types_reference_registered_switches():
    for ptype in ("ATTENDANCE_PAY_POLICY", "OVERTIME_POLICY"):
        assert validate_fields(ptype, {})

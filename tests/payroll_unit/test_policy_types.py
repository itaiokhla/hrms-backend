"""Policy types, activation rules, hashing, assignment and starter policies (3.1 §3, §5; I-13, I-14, I-16)."""

import hashlib
from datetime import date
from decimal import Decimal

import pytest

from app.payroll.policy_types import (
    APPROVAL_MODULES,
    APPROVER_TYPES,
    POLICY_TYPES,
    AssignmentRule,
    EmployeeAttributes,
    PolicyValidationError,
    activation_problems,
    canonical_json,
    check_assignment_rules,
    default_fields,
    find_priority_conflicts,
    leave_pay_treatment,
    normalize_leave_types,
    parse_criteria,
    policy_hash,
    policy_type_metadata,
    resolve_assignment,
    starter_policies,
    validate_fields,
)
from app.payroll.settings_registry import REGISTRY

LEAVE_TYPES = [
    {"code": "casual", "name": "Casual Leave", "is_paid": True},
    {"code": "sick", "name": "Sick Leave", "is_paid": True},
    {"code": "earned", "name": "Earned Leave", "is_paid": True},
    {"code": "maternity", "name": "Maternity Leave", "is_paid": True},
    {"code": "paternity", "name": "Paternity Leave", "is_paid": True},
    {"code": "unpaid", "name": "Unpaid Leave", "is_paid": False},
]
OT_BUCKETS = [
    {"day_type": "WORKDAY", "after_hours": "0", "multiplier": "1.5"},
    {"day_type": "WORKDAY", "after_hours": "2", "multiplier": "2"},
    {"day_type": "WEEKLY_OFF", "after_hours": "0", "multiplier": "2.0"},
    {"day_type": "HOLIDAY", "after_hours": "0", "multiplier": "2.0"},
]


def _error(code: str, fields: dict, **kwargs) -> PolicyValidationError:
    with pytest.raises(PolicyValidationError) as error:
        validate_fields(code, fields, **kwargs)
    return error.value


def _rule(rule_id, priority, criteria, **kwargs) -> AssignmentRule:
    return AssignmentRule(rule_id=rule_id, policy_id=f"P{rule_id}", priority=priority, criteria=criteria, **kwargs)


# --------------------------------------------------------------------------- catalogue (3.1 §3.2)


def test_every_policy_type_of_3_1_section_3_2():
    assert set(POLICY_TYPES) == {
        "ATTENDANCE_PAY_POLICY", "LEAVE_PAY_POLICY", "HOLIDAY_PAY_POLICY", "OVERTIME_POLICY", "TASK_PAY_POLICY",
        "LATE_PENALTY_POLICY", "SHIFT_ALLOWANCE_POLICY", "INCENTIVE_PLAN", "REIMBURSEMENT_POLICY", "LOAN_POLICY",
        "ADVANCE_POLICY", "PAYSLIP_TEMPLATE", "APPROVAL_CHAIN",
        "ATTENDANCE_CAPTURE_POLICY",  # AS-M2 (JC-AS-22): attendance-owned, write gate attendance:manage_policy
        "ATTENDANCE_RULES_POLICY",  # AS-M3 (JC-AS-22): read by the day evaluation only
        "ATTENDANCE_REQUEST_POLICY", "SCHEDULING_POLICY",  # AS-M3 types (AS-M4 / AS-M5 consumers)
        "LEAVE_PLAN",  # LH-M2 (03-leave/01 §5.1): leave-owned, write gate leave:manage_policy, never in snapshots
    }
    assert {c for c, d in POLICY_TYPES.items() if d.module != "payroll"} == {
        "ATTENDANCE_CAPTURE_POLICY", "ATTENDANCE_RULES_POLICY", "ATTENDANCE_REQUEST_POLICY", "SCHEDULING_POLICY",
        "LEAVE_PLAN"}
    assert POLICY_TYPES["LEAVE_PLAN"].write_permission == "leave:manage_policy"
    assert POLICY_TYPES["SCHEDULING_POLICY"].write_permission == "shift:manage"
    assert POLICY_TYPES["ATTENDANCE_CAPTURE_POLICY"].write_permission == "attendance:manage_policy"
    assert POLICY_TYPES["ATTENDANCE_RULES_POLICY"].write_permission == "attendance:manage_policy"


@pytest.mark.parametrize(
    ("code", "gates", "grain"),
    [
        ("ATTENDANCE_PAY_POLICY", ("payroll.attendance.enabled",), "PERIOD"),
        ("LEAVE_PAY_POLICY", ("payroll.leave.enabled", "payroll.leave.encashment.enabled"), "PERIOD"),
        ("HOLIDAY_PAY_POLICY", ("payroll.holiday_pay.non_salaried.enabled", "payroll.holiday_work_premium.enabled",
                                "payroll.weekly_off_pay.enabled"), "DAY"),
        ("OVERTIME_POLICY", ("payroll.overtime.enabled",), "DAY"),
        ("TASK_PAY_POLICY", ("payroll.task_pay.enabled",), "DAY"),
        ("LATE_PENALTY_POLICY", ("payroll.late_penalty.enabled",), "DAY"),
        ("SHIFT_ALLOWANCE_POLICY", ("payroll.shift_allowance.enabled",), "DAY"),
        ("INCENTIVE_PLAN", ("payroll.incentives.enabled",), "PERIOD"),
        ("REIMBURSEMENT_POLICY", ("payroll.reimbursements.enabled",), "PERIOD"),
        ("LOAN_POLICY", ("payroll.loans.enabled",), "PERIOD"),
        ("ADVANCE_POLICY", ("payroll.advances.enabled",), "PERIOD"),
        ("PAYSLIP_TEMPLATE", (), "PERIOD"),
        ("APPROVAL_CHAIN", (), "PERIOD"),
        ("ATTENDANCE_CAPTURE_POLICY", (), "DAY"),
        ("ATTENDANCE_RULES_POLICY", (), "DAY"),
        ("ATTENDANCE_REQUEST_POLICY", (), "DAY"),
        ("SCHEDULING_POLICY", (), "DAY"),
    ],
)
def test_gate_switches_and_grain(code, gates, grain):
    ptype = POLICY_TYPES[code]
    assert ptype.gate_switches == gates and ptype.grain == grain
    for gate in gates:
        assert REGISTRY[gate].resolution_grain == grain
    assert ptype.assignable == (code != "APPROVAL_CHAIN")


def test_unknown_type_and_non_object_fields():
    assert _error("NOPE_POLICY", {}).code == "UNKNOWN_POLICY_TYPE"
    with pytest.raises(PolicyValidationError) as error:
        validate_fields("OVERTIME_POLICY", ["exempt"])  # type: ignore[arg-type]
    assert error.value.code == "INVALID_FIELDS"


# --------------------------------------------------------------------------- defaults (3.1 §5.2-§5.15)


def test_attendance_defaults_3_1_section_5_2():
    assert default_fields("ATTENDANCE_PAY_POLICY") == {
        "unmarked_day": "EXCEPTION", "missing_punch_out": "EXCEPTION", "derive_status_from_hours": False,
        "full_day_min_hours": "8", "half_day_min_hours": "4", "count_on_duty_as_present": True,
        "lop_component_tag": "LOP_APPLICABLE", "break_type_pay_map": {},
    }


def test_leave_row_defaults_3_1_section_5_3():
    row = validate_fields("LEAVE_PAY_POLICY", {"rows": [{"leave_type_code": "casual"}]})["rows"][0]
    assert row == {
        "leave_type_code": "casual", "pay_treatment": None, "partial_percent": None, "tiers": None,
        "tiers_window_months": 12, "counts_toward_ot_threshold": False, "encashable": False,
        "encashment_formula": None, "encash_on": ["EXIT"], "encashment_max_days_per_year": None,
        "year_end_excess_action": "CARRY_FORWARD",
    }
    assert leave_pay_treatment(row, is_paid=True) == "FULL"
    assert leave_pay_treatment(row, is_paid=False) == "UNPAID"
    assert leave_pay_treatment({**row, "pay_treatment": "PARTIAL"}, is_paid=True) == "PARTIAL"


def test_holiday_defaults_3_1_section_5_4_with_blank_multipliers():
    fields = default_fields("HOLIDAY_PAY_POLICY")
    assert fields["paid_holiday_types"] == ["PUBLIC", "COMPANY", "OPTIONAL_AVAILED"]
    assert fields["non_salaried_holiday_pay"] == "STANDARD_HOURS"
    assert fields["eligibility_adjacent_days"] == "NONE"
    assert fields["holiday_work_compensation"] == "PREMIUM" and fields["weekly_off_work_compensation"] == "PREMIUM"
    assert fields["holiday_work_multiplier"] is None and fields["weekly_off_work_multiplier"] is None
    assert fields["holiday_work_min_hours"] == "4" and fields["weekly_off_work_min_hours"] == "4"
    assert fields["holiday_work_requires_approval"] is True
    assert fields["holiday_hours_beyond_standard"] == "OVERTIME_HOLIDAY_BUCKET"
    assert fields["comp_off_validity_days"] == 60 and fields["holiday_on_weekly_off"] == "NONE"


def test_overtime_defaults_3_1_section_5_5_carry_no_multiplier():
    fields = default_fields("OVERTIME_POLICY")
    assert fields["exempt"] is False and fields["source"] == "APPROVED_REQUESTS"
    assert fields["daily_threshold"] == "SHIFT_DURATION" and fields["weekly_threshold_hours"] is None
    assert fields["threshold_combination"] == "DAILY" and fields["min_minutes"] == 30
    assert fields["rounding"] == {"increment_minutes": 15, "mode": "DOWN"}
    assert fields["buckets"] == [
        {"day_type": "WORKDAY", "after_hours": "0", "multiplier": None},
        {"day_type": "WEEKLY_OFF", "after_hours": "0", "multiplier": None},
        {"day_type": "HOLIDAY", "after_hours": "0", "multiplier": None},
    ]
    # 3.1 §5.5's SUM_TAG_RATE("OT_BASE") / period.basis_days / emp.standard_hours_per_day, with each OT_BASE rate
    # converted by its unit (seed defect: an hourly OT_BASE rate was divided by days x hours again); the salaried
    # result is identical (tests/payroll_unit/test_seed_defects.py).
    assert fields["rate_formula"] == 'SUM_TAG_HOURLY("OT_BASE")'
    assert fields["caps"] == {"per_day": None, "per_week": None, "per_month": None, "per_quarter": None}
    assert fields["cap_action"] == "EXCEPTION" and fields["cross_midnight"] == "SHIFT_START_DATE"
    assert fields["comp_off_allowed"] is False and fields["comp_off_ratio"] == "1"
    assert fields["approval_chain_id"] is None


def test_task_late_shift_defaults_3_1_sections_5_6_to_5_8():
    task = default_fields("TASK_PAY_POLICY")
    assert (task["attribution"], task["rate_source"], task["earning_component"], task["cap_action"],
            task["minimum_wage_floor"], task["clawback"], task["contractor_document"]) == (
        "APPROVAL_DATE", "TASK_THEN_RATE_CARD", "TASK_EARNING", "EXCEPTION", "WARN", "NEXT_RUN", "INVOICE")
    assert task["allow_split"] is False and task["requires_evidence"] is False and task["cap_per_period"] is None
    late = default_fields("LATE_PENALTY_POLICY")
    assert (late["free_late_marks"], late["penalty_type"], late["late_marks_per_unit"], late["unit_days"],
            late["early_exit_grace_minutes"], late["cap_days_per_period"]) == (2, "DAY_FRACTION", 3, "0.5", 15, "2")
    assert late["count_early_exit"] is False and late["exempt_if_regularized"] is True
    shift = default_fields("SHIFT_ALLOWANCE_POLICY")
    assert shift["night_window"] == {"start": "22:00", "end": "06:00"} and shift["min_minutes_in_window"] == 120
    assert shift["component"] == "SHIFT_ALLOWANCE"


def test_reimbursement_loan_advance_defaults_3_1_sections_5_10_and_5_11():
    claims = default_fields("REIMBURSEMENT_POLICY")
    assert claims == {"claim_types": [], "submission_window_days": 60, "duplicate_receipt_check": True}
    loan = default_fields("LOAN_POLICY")
    assert (loan["max_tenure_months"], loan["interest_rate_percent"], loan["max_instalment_percent_of_net"],
            loan["min_service_months"], loan["max_active_loans"], loan["recover_balance_on_exit"]) == (
        24, "0", "50", 6, 1, True)
    advance = default_fields("ADVANCE_POLICY")
    assert (advance["max_percent_of_monthly_net"], advance["recover_in_runs"], advance["max_per_year"]) == ("50", 1, 2)


def test_payslip_template_and_approval_chain_3_1_sections_5_14_and_5_15():
    template = default_fields("PAYSLIP_TEMPLATE")
    assert template["layout"] == "CLASSIC" and len(template["sections"]) == 12
    assert all(s["visible"] for s in template["sections"])
    assert set(APPROVAL_MODULES) == {
        "leave", "payroll_run", "compensation", "wage_input", "overtime", "task", "reimbursement", "loan",
        "bank_change", "payment_batch", "off_cycle", "final_settlement",
    }
    assert set(APPROVER_TYPES) == {"primary_manager", "skip_level_manager", "department_head", "role",
                                   "finance_role", "specific_user"}
    assert _error("APPROVAL_CHAIN", {}).field == "module"


# --------------------------------------------------------------------------- strict validation


@pytest.mark.parametrize(
    ("code", "fields", "where"),
    [
        ("ATTENDANCE_PAY_POLICY", {"full_day_min_hours": 8.0}, "full_day_min_hours"),
        ("ATTENDANCE_PAY_POLICY", {"full_day_min_hours": 8}, "full_day_min_hours"),
        ("ATTENDANCE_PAY_POLICY", {"derive_status_from_hours": "true"}, "derive_status_from_hours"),
        ("ATTENDANCE_PAY_POLICY", {"unmarked_day": "PAID"}, "unmarked_day"),
        ("ATTENDANCE_PAY_POLICY", {"company_id": 2}, "company_id"),
        ("ATTENDANCE_PAY_POLICY", {"lop_component_tag": "lop"}, "lop_component_tag"),
        ("ATTENDANCE_PAY_POLICY", {"break_type_pay_map": {"LUNCH": "yes"}}, "break_type_pay_map.LUNCH"),
        ("ATTENDANCE_PAY_POLICY", {"full_day_min_hours": "25"}, "full_day_min_hours"),
        ("OVERTIME_POLICY", {"min_minutes": True}, "min_minutes"),
        ("OVERTIME_POLICY", {"daily_threshold": "9.01"}, "daily_threshold"),
        ("OVERTIME_POLICY", {"daily_threshold": "SHIFT"}, "daily_threshold"),
        ("OVERTIME_POLICY", {"rounding": {"increment_minutes": 0, "mode": "DOWN"}}, "rounding.increment_minutes"),
        ("OVERTIME_POLICY", {"rate_formula": "1 +"}, "rate_formula"),
        ("TASK_PAY_POLICY", {"cap_per_period": {"amount": "100", "currency": None}}, "cap_per_period"),
        ("TASK_PAY_POLICY", {"cap_per_period": {"amount": "100.5", "currency": "JPY"}}, "cap_per_period"),
        ("TASK_PAY_POLICY", {"cap_per_period": {"amount": "-1", "currency": "INR"}}, "cap_per_period.amount"),
        ("TASK_PAY_POLICY", {"cap_per_period": {"amount": "1", "currency": "XAU"}}, "cap_per_period"),
        ("LEAVE_PAY_POLICY", {"rows": [{"leave_type_code": "casual"}, {"leave_type_code": "casual"}]}, None),
        ("LEAVE_PAY_POLICY", {"rows": [{"leave_type_code": "sick", "partial_percent": "50"}]}, "rows[0]"),
        ("LEAVE_PAY_POLICY", {"rows": [{"leave_type_code": "sick", "pay_treatment": "TIERED",
                                        "tiers": [{"from_day": 2, "to_day": 10, "percent": "100"}]}]}, "rows[0]"),
        ("LEAVE_PAY_POLICY", {"rows": [{"leave_type_code": "sick", "encash_on": []}]}, "rows[0].encash_on"),
        ("HOLIDAY_PAY_POLICY", {"paid_holiday_types": ["PUBLIC", "PUBLIC"]}, "paid_holiday_types"),
        ("SHIFT_ALLOWANCE_POLICY", {"night_window": {"start": "22:00", "end": "22:00"}}, "night_window"),
        ("SHIFT_ALLOWANCE_POLICY", {"night_window": {"start": "23:00", "end": "00:30"}}, None),
        ("PAYSLIP_TEMPLATE", {"sections": [{"code": "HEADER"}]}, None),
        ("REIMBURSEMENT_POLICY", {"claim_types": [{"code": "TRAVEL", "name": "Travel",
                                                   "limit_per_claim": {"amount": "500", "currency": "INR"},
                                                   "limit_per_month": {"amount": "400", "currency": "INR"}}]},
         "claim_types[0]"),
        ("REIMBURSEMENT_POLICY", {"claim_types": [{"code": "TRAVEL", "name": "Travel",
                                                   "limit_per_claim": {"amount": "5", "currency": "INR"},
                                                   "limit_per_month": {"amount": "50", "currency": "USD"}}]},
         "claim_types[0]"),
        ("INCENTIVE_PLAN", {"targets": [{"grade_id": 1, "employee_id": 2, "target": "10"}]}, "targets[0]"),
        ("APPROVAL_CHAIN", {"module": "payroll_run", "steps": [{"approver_type": "role"}]}, "steps[0]"),
        ("APPROVAL_CHAIN", {"module": "payroll_run", "steps": [{"approver_type": "finance_role", "role_id": 3}]},
         "steps[0]"),
        ("APPROVAL_CHAIN", {"module": "payroll_run", "steps": [{"approver_type": "specific_user"}]}, "steps[0]"),
        ("APPROVAL_CHAIN", {"module": "payroll_run", "steps": [{"approver_type": "primary_manager", "min_days": 3}]},
         None),
        ("APPROVAL_CHAIN", {"module": "expenses"}, "module"),
    ],
)
def test_fields_are_validated_strictly(code, fields, where):
    error = _error(code, fields)
    assert error.code == "INVALID_FIELDS"
    assert error.field == where
    assert error.details["errors"]


def test_valid_fields_are_normalized():
    fields = validate_fields("OVERTIME_POLICY", {"daily_threshold": "9.50", "buckets": OT_BUCKETS,
                                                 "caps": {"per_week": "12.0"}, "comp_off_ratio": "1.50"})
    assert fields["daily_threshold"] == "9.5" and fields["caps"]["per_week"] == "12" and fields["comp_off_ratio"] == "1.5"
    assert fields["buckets"][2] == {"day_type": "WEEKLY_OFF", "after_hours": "0", "multiplier": "2"}
    money = validate_fields("TASK_PAY_POLICY", {"cap_per_period": {"amount": "15000.00", "currency": "INR"}})
    assert money["cap_per_period"] == {"amount": "15000", "currency": "INR"}
    zero = validate_fields("REIMBURSEMENT_POLICY", {"claim_types": [
        {"code": "GENERAL", "name": "General expense", "receipt_required_above": {"amount": "0", "currency": None}}]})
    assert zero["claim_types"][0]["receipt_required_above"] == {"amount": "0", "currency": None}
    assert zero["claim_types"][0]["taxability"] == "UNCLASSIFIED"
    tiers = validate_fields("LEAVE_PAY_POLICY", {"rows": [{
        "leave_type_code": "sick", "pay_treatment": "TIERED",
        "tiers": [{"from_day": 1, "to_day": 10, "percent": "100"}, {"from_day": 11, "to_day": 30, "percent": "50"}]}]})
    assert tiers["rows"][0]["tiers"][1] == {"from_day": 11, "to_day": 30, "percent": "50"}
    sets = validate_fields("HOLIDAY_PAY_POLICY", {"paid_holiday_types": ["COMPANY", "PUBLIC"]})
    assert sets["paid_holiday_types"] == ["PUBLIC", "COMPANY"]
    breaks = validate_fields("ATTENDANCE_PAY_POLICY", {"break_type_pay_map": {"LUNCH": False, "TEA": True}})
    assert breaks["break_type_pay_map"] == {"LUNCH": False, "TEA": True}


def test_overtime_bucket_structure():
    assert "WEEKLY_OFF" in _error("OVERTIME_POLICY", {"buckets": [OT_BUCKETS[0], OT_BUCKETS[3]]}).message
    assert _error("OVERTIME_POLICY", {"buckets": []}).code == "INVALID_FIELDS"
    assert _error("OVERTIME_POLICY", {"buckets": OT_BUCKETS + [OT_BUCKETS[0]]}).code == "INVALID_FIELDS"
    odd = [{**OT_BUCKETS[1], "after_hours": "2.01"}] + [OT_BUCKETS[0], *OT_BUCKETS[2:]]
    assert _error("OVERTIME_POLICY", {"buckets": odd}).field == "buckets[0]"


def test_payslip_mandatory_sections_cannot_be_hidden():
    sections = [{"code": c, "visible": c != "PAYMENT"} for c in (
        "HEADER", "EMPLOYEE", "ATTENDANCE_SUMMARY", "EARNINGS", "DEDUCTIONS", "REIMBURSEMENTS",
        "EMPLOYER_CONTRIBUTIONS", "TOTALS", "PAYMENT", "LEAVE_BALANCES", "TAX_SUMMARY", "FOOTER")]
    assert "PAYMENT" in _error("PAYSLIP_TEMPLATE", {"sections": sections}).message
    optional_hidden = [{**s, "visible": s["code"] != "TAX_SUMMARY"} for s in sections]
    reordered = list(reversed(optional_hidden))
    fields = validate_fields("PAYSLIP_TEMPLATE", {"sections": reordered, "layout": "MODERN",
                                                  "locale_overrides": {"ar-AE": {"footer_note": "x"}}})
    assert fields["sections"][0]["code"] == "FOOTER"
    assert _error("PAYSLIP_TEMPLATE", {"locale_overrides": {"arabic": {}}}).code == "INVALID_FIELDS"


def test_approval_chain_steps():
    chain = validate_fields("APPROVAL_CHAIN", {"module": "payment_batch", "steps": [
        {"approver_type": "role", "role_id": 7},
        {"approver_type": "finance_role", "min_amount": {"amount": "100000", "currency": "AED"}},
        {"approver_type": "specific_user", "specific_user_id": 42},
    ]}, for_activation=True)
    assert [s["approver_type"] for s in chain["steps"]] == ["role", "finance_role", "specific_user"]
    leave = validate_fields("APPROVAL_CHAIN", {"module": "leave", "steps": [
        {"approver_type": "primary_manager"}, {"approver_type": "department_head", "min_days": 5}]})
    assert leave["steps"][1]["min_days"] == 5
    assert _error("APPROVAL_CHAIN", {"module": "leave", "steps": [
        {"approver_type": "primary_manager", "min_amount": {"amount": "1", "currency": "INR"}}]}).code == "INVALID_FIELDS"
    assert _error("APPROVAL_CHAIN", {"module": "loan"}, for_activation=True).code == "FIELD_REQUIRED_FOR_ACTIVATION"


# --------------------------------------------------------------------------- draft vs activation (I-13)


def test_draft_may_leave_legal_fields_blank_but_activation_rejects_them():
    assert validate_fields("OVERTIME_POLICY", {})["buckets"][0]["multiplier"] is None
    error = _error("OVERTIME_POLICY", {}, for_activation=True)
    assert error.code == "FIELD_REQUIRED_FOR_ACTIVATION" and error.field == "buckets[0].multiplier"
    assert {f["legal_sensitivity"] for f in error.details["fields"]} == {"LEGAL_FLOOR"}
    assert validate_fields("OVERTIME_POLICY", {"buckets": OT_BUCKETS}, for_activation=True)


def test_exempt_overtime_policy_needs_no_multiplier():
    assert validate_fields("OVERTIME_POLICY", {"exempt": True}, for_activation=True)["exempt"] is True


def test_weekly_threshold_required_for_weekly_combinations():
    error = _error("OVERTIME_POLICY", {"buckets": OT_BUCKETS, "threshold_combination": "DAILY_THEN_WEEKLY"},
                   for_activation=True)
    assert error.field == "weekly_threshold_hours"
    assert validate_fields("OVERTIME_POLICY", {"buckets": OT_BUCKETS, "threshold_combination": "DAILY_THEN_WEEKLY",
                                               "weekly_threshold_hours": "48"}, for_activation=True)


def test_holiday_activation_checks_only_switched_on_features():
    assert _error("HOLIDAY_PAY_POLICY", {}, for_activation=True).details["fields"] == [
        {"field": "holiday_work_multiplier", "message": "Enter the holiday-work multiplier.", "legal_sensitivity": "LEGAL_FLOOR"},
        {"field": "weekly_off_work_multiplier", "message": "Enter the weekly-off multiplier.", "legal_sensitivity": "LEGAL_FLOOR"},
    ]
    only_non_salaried = {"payroll.holiday_pay.non_salaried.enabled"}
    assert validate_fields("HOLIDAY_PAY_POLICY", {}, for_activation=True, enabled_switches=only_non_salaried)
    premium = {"payroll.holiday_work_premium.enabled"}
    assert _error("HOLIDAY_PAY_POLICY", {}, for_activation=True, enabled_switches=premium).field == "holiday_work_multiplier"
    comp_off = {"holiday_work_compensation": "COMP_OFF"}
    assert validate_fields("HOLIDAY_PAY_POLICY", comp_off, for_activation=True, enabled_switches=premium)
    assert validate_fields("HOLIDAY_PAY_POLICY", {"holiday_work_multiplier": "1.0"}, for_activation=True,
                           enabled_switches=premium)["holiday_work_multiplier"] == "1"


@pytest.mark.parametrize(
    ("code", "fields", "field", "fixed"),
    [
        ("LATE_PENALTY_POLICY", {"penalty_type": "FIXED_AMOUNT"}, "amount_per_late",
         {"penalty_type": "FIXED_AMOUNT", "amount_per_late": {"amount": "100", "currency": "INR"}}),
        ("SHIFT_ALLOWANCE_POLICY", {}, "amount", {"amount": {"amount": "150", "currency": "INR"}}),
        ("SHIFT_ALLOWANCE_POLICY", {"allowance_type": "PERCENT_OF_HOURLY"}, "percent",
         {"allowance_type": "PERCENT_OF_HOURLY", "percent": "25"}),
        ("INCENTIVE_PLAN", {}, "formula", {"formula": "10 * 100"}),
        ("LOAN_POLICY", {}, "max_amount_formula", {"max_amount_formula": "3 * 1000"}),
        ("REIMBURSEMENT_POLICY", {}, "claim_types", {"claim_types": [{"code": "GENERAL", "name": "General"}]}),
        ("LEAVE_PAY_POLICY", {"rows": [{"leave_type_code": "earned", "encashable": True}]}, "rows[0].encashment_formula",
         {"rows": [{"leave_type_code": "earned", "encashable": True, "encashment_formula": "BASIC / 26"}]}),
        ("LEAVE_PAY_POLICY", {"rows": [{"leave_type_code": "sick", "pay_treatment": "PARTIAL"}]},
         "rows[0].partial_percent",
         {"rows": [{"leave_type_code": "sick", "pay_treatment": "PARTIAL", "partial_percent": "50"}]}),
        ("LEAVE_PAY_POLICY", {"rows": [{"leave_type_code": "earned", "year_end_excess_action": "ENCASH"}]},
         "rows[0].year_end_excess_action",
         {"rows": [{"leave_type_code": "earned", "year_end_excess_action": "ENCASH", "encashable": True,
                    "encash_on": ["YEAR_END"], "encashment_formula": "BASIC / 30"}]}),
    ],
)
def test_activation_requires_completed_fields(code, fields, field, fixed):
    assert validate_fields(code, fields)
    assert _error(code, fields, for_activation=True).field == field
    assert validate_fields(code, fixed, for_activation=True)


def test_encashment_formula_only_needed_when_encashment_may_be_on():
    fields = {"rows": [{"leave_type_code": "earned", "encashable": True}]}
    assert validate_fields("LEAVE_PAY_POLICY", fields, for_activation=True, enabled_switches={"payroll.leave.enabled"})
    assert activation_problems("LEAVE_PAY_POLICY", fields)[0].field == "rows[0].encashment_formula"


# --------------------------------------------------------------------------- policy_hash (3.1 §2.6)


def test_policy_hash_is_sha256_of_canonical_json():
    assert policy_hash({}) == hashlib.sha256(b"{}").hexdigest()
    assert canonical_json({"b": Decimal("1.50"), "a": [True, None, "x"]}) == '{"a":[true,null,"x"],"b":"1.5"}'
    assert policy_hash({"x": "1", "y": 2}) == policy_hash({"y": 2, "x": "1"})
    assert policy_hash({"m": Decimal("2.0")}) == policy_hash({"m": "2"})
    assert policy_hash({"note": "ü"}) == hashlib.sha256('{"note":"ü"}'.encode()).hexdigest()


def test_policy_hash_rejects_floats_and_changes_with_content():
    with pytest.raises(PolicyValidationError):
        policy_hash({"m": 1.5})
    with pytest.raises(PolicyValidationError):
        policy_hash({"m": Decimal("NaN")})
    one = validate_fields("OVERTIME_POLICY", {"buckets": OT_BUCKETS})
    two = validate_fields("OVERTIME_POLICY", {"buckets": OT_BUCKETS, "min_minutes": 15})
    assert policy_hash(one) != policy_hash(two)
    assert len(policy_hash(one)) == 64


# --------------------------------------------------------------------------- assignment (EC-CFG-05, EC-CFG-07)

ALICE = EmployeeAttributes(1, legal_entity_id=1, branch_id=10, pay_group_id=100, grade_id=3, employment_type="FULL_TIME")
BOB = EmployeeAttributes(2, legal_entity_id=1, branch_id=20, pay_group_id=200, grade_id=1, employment_type="CONTRACT")


def test_criteria_parsing():
    assert parse_criteria({}).everyone is True
    assert parse_criteria({"everyone": True}).everyone is True
    branch = parse_criteria({"branch_ids": [20, 10, 10]})
    assert branch.everyone is False and branch.branch_ids == (10, 20)
    assert branch.model_dump(mode="json")["branch_ids"] == [10, 20]
    for bad in ({"everyone": True, "branch_ids": [1]}, {"everyone": False}, {"branch_ids": []},
                {"branch_ids": [0]}, {"grade_ids": ["3"]}, {"cost_center_ids": [1]}, {"employment_types": [""]}):
        with pytest.raises(PolicyValidationError) as error:
            parse_criteria(bad)
        assert error.value.code == "INVALID_CRITERIA"


def test_highest_priority_rule_wins():
    rules = [_rule(1, 0, {}), _rule(2, 10, {"grade_ids": [1, 2, 3]}), _rule(3, 20, {"pay_group_ids": [100]})]
    assert resolve_assignment(rules, ALICE).policy_id == "P3"
    assert resolve_assignment(rules, BOB).policy_id == "P2"
    contractor = [_rule(1, 0, {}), _rule(4, 5, {"employment_types": ["CONTRACT"], "legal_entity_ids": [1]})]
    assert resolve_assignment(contractor, BOB).policy_id == "P4"
    assert resolve_assignment(contractor, ALICE).policy_id == "P1"


def test_ec_cfg_05_same_priority_match_is_ambiguous_not_guessed():
    rules = [_rule(1, 10, {"branch_ids": [10]}), _rule(2, 10, {"grade_ids": [3]}), _rule(3, 0, {})]
    resolution = resolve_assignment(rules, ALICE)
    assert resolution.status == "AMBIGUOUS" and resolution.rule is None
    assert {r.rule_id for r in resolution.candidates} == {1, 2}
    with pytest.raises(PolicyValidationError) as error:
        check_assignment_rules(rules, [ALICE, BOB])
    assert error.value.code == "PRIORITY_CONFLICT"
    assert error.value.details["conflicts"] == [{"priority": 10, "rule_ids": [1, 2], "employee_ids": [1]}]
    assert "1" in error.value.message


def test_ec_cfg_05_same_priority_without_common_employee_or_overlapping_dates_is_allowed():
    disjoint = [_rule(1, 10, {"branch_ids": [10]}), _rule(2, 10, {"branch_ids": [20]})]
    check_assignment_rules(disjoint, [ALICE, BOB])
    assert resolve_assignment(disjoint, BOB).policy_id == "P2"
    sequential = [_rule(1, 10, {}, effective_to=date(2026, 6, 30)), _rule(2, 10, {}, effective_from=date(2026, 7, 1))]
    check_assignment_rules(sequential, [ALICE])
    assert resolve_assignment(sequential, ALICE, on=date(2026, 7, 1)).policy_id == "P2"
    assert resolve_assignment(sequential, ALICE, on=date(2026, 6, 30)).policy_id == "P1"


def test_ec_cfg_05_two_catch_alls_conflict_even_without_employees():
    conflicts = find_priority_conflicts([_rule(1, 0, {}), _rule(2, 0, {"everyone": True})], [])
    assert [(c.priority, c.rule_ids, c.employee_ids) for c in conflicts] == [(0, (1, 2), ())]
    assert find_priority_conflicts([_rule(1, 0, {}), _rule(2, 0, {"grade_ids": [1]})], []) == []


def test_ec_cfg_07_no_matching_rule_is_reported():
    rules = [_rule(1, 10, {"branch_ids": [10]}), _rule(2, 0, {"employee_ids": [5]})]
    assert resolve_assignment(rules, BOB).status == "NO_POLICY"
    inactive = [_rule(1, 0, {}, status="INACTIVE")]
    assert resolve_assignment(inactive, BOB).status == "NO_POLICY"
    future = [_rule(1, 0, {}, effective_from=date(2027, 1, 1))]
    assert resolve_assignment(future, BOB, on=date(2026, 12, 31)).status == "NO_POLICY"


def test_invalid_rules():
    for kwargs in ({"priority": -1}, {"priority": True}, {"priority": 1, "status": "DRAFT"},
                   {"priority": 1, "effective_from": date(2026, 2, 1), "effective_to": date(2026, 1, 1)}):
        with pytest.raises(PolicyValidationError) as error:
            AssignmentRule(rule_id=1, policy_id=1, criteria={}, **kwargs)
        assert error.value.code == "INVALID_RULE"


# --------------------------------------------------------------------------- starter policies (I-13, I-14)


def test_starter_v1_policies_i13():
    policies = {p.type: p for p in starter_policies("STARTER_V1", leave_types=LEAVE_TYPES)}
    active = {code for code, p in policies.items() if p.status == "ACTIVE"}
    assert active == {"ATTENDANCE_PAY_POLICY", "LEAVE_PAY_POLICY", "TASK_PAY_POLICY", "REIMBURSEMENT_POLICY",
                      "PAYSLIP_TEMPLATE"}
    assert {code for code, p in policies.items() if p.status == "DRAFT"} == {
        "OVERTIME_POLICY", "HOLIDAY_PAY_POLICY", "SHIFT_ALLOWANCE_POLICY", "LATE_PENALTY_POLICY", "INCENTIVE_PLAN",
        "LOAN_POLICY", "ADVANCE_POLICY"}
    for policy in policies.values():
        assert policy.priority == 0 and policy.criteria == {"everyone": True}
        if policy.status == "ACTIVE":
            assert policy.policy_hash == policy_hash(policy.fields)
            assert validate_fields(policy.type, policy.fields, for_activation=True) == policy.fields
        else:
            assert policy.policy_hash is None and policy.decision_required
    attendance = policies["ATTENDANCE_PAY_POLICY"].fields
    assert (attendance["unmarked_day"], attendance["missing_punch_out"], attendance["derive_status_from_hours"],
            attendance["count_on_duty_as_present"]) == ("EXCEPTION", "EXCEPTION", False, True)
    task = policies["TASK_PAY_POLICY"].fields
    assert (task["attribution"], task["rate_source"], task["cap_action"], task["minimum_wage_floor"],
            task["requires_evidence"], task["clawback"], task["allow_split"]) == (
        "APPROVAL_DATE", "TASK_THEN_RATE_CARD", "EXCEPTION", "WARN", True, "MANUAL", False)
    claims = policies["REIMBURSEMENT_POLICY"].fields
    assert claims["claim_types"] == [{
        "code": "GENERAL", "name": "General expense", "limit_per_claim": None, "limit_per_month": None,
        "limit_per_year": None, "receipt_required_above": {"amount": "0", "currency": None},
        "taxability": "UNCLASSIFIED", "approval_chain_id": None}]
    assert claims["duplicate_receipt_check"] is True and claims["submission_window_days"] == 60


def test_starter_v1_assumes_no_legal_constant_k157():
    policies = {p.type: p for p in starter_policies("STARTER_V1", leave_types=LEAVE_TYPES)}
    overtime = policies["OVERTIME_POLICY"].fields
    assert [b["multiplier"] for b in overtime["buckets"]] == [None, None, None]
    assert overtime["source"] == "APPROVED_REQUESTS" and overtime["comp_off_allowed"] is False
    holiday = policies["HOLIDAY_PAY_POLICY"].fields
    assert holiday["holiday_work_multiplier"] is None and holiday["weekly_off_work_multiplier"] is None
    assert holiday["holiday_work_requires_approval"] is True and holiday["holiday_work_min_hours"] == "4"
    for policy in policies.values():
        assert policy.fields.get("source") != "ATTENDANCE_AUTO"
    assert all(row["encashment_formula"] is None for row in policies["LEAVE_PAY_POLICY"].fields["rows"])


def test_starter_leave_rows_come_from_leave_types():
    leave = next(p for p in starter_policies("STARTER_V1", leave_types=LEAVE_TYPES) if p.type == "LEAVE_PAY_POLICY")
    treatments = {row["leave_type_code"]: row["pay_treatment"] for row in leave.fields["rows"]}
    assert treatments == {"casual": "FULL", "sick": "FULL", "earned": "FULL", "maternity": "FULL",
                          "paternity": "FULL", "unpaid": "UNPAID"}
    assert "STATUTORY" not in treatments.values()
    assert all(row["encashable"] is False for row in leave.fields["rows"])
    flagged = " ".join(leave.review_items)
    assert "maternity" in flagged and "paternity" in flagged and "sick" in flagged and "casual" not in flagged
    assert leave.decision_required


def test_starter_without_leave_types_has_empty_leave_policy():
    leave = next(p for p in starter_policies("STARTER_V1") if p.type == "LEAVE_PAY_POLICY")
    assert leave.fields == {"rows": []} and leave.status == "ACTIVE"


def test_invalid_leave_types():
    for bad in ([{"code": "casual", "is_paid": 1}], [{"code": "", "is_paid": True}],
                [{"code": "a", "is_paid": True}, {"code": "a", "is_paid": False}], "casual"):
        with pytest.raises(PolicyValidationError) as error:
            normalize_leave_types(bad)
        assert error.value.code == "INVALID_LEAVE_TYPE"

    class Row:
        code, name, is_paid = "earned", "Earned", True

    assert normalize_leave_types([Row()])[0].code == "earned"


def test_legacy_v1_policies_i14():
    policies = {p.type: p for p in starter_policies("LEGACY_V1")}
    assert set(policies) == {"ATTENDANCE_PAY_POLICY", "PAYSLIP_TEMPLATE", "OVERTIME_POLICY", "LATE_PENALTY_POLICY"}
    attendance = policies["ATTENDANCE_PAY_POLICY"]
    assert attendance.status == "ACTIVE"
    assert (attendance.fields["unmarked_day"], attendance.fields["missing_punch_out"]) == ("PRESENT", "FULL_DAY")
    assert policies["OVERTIME_POLICY"].status == "DRAFT" and policies["LATE_PENALTY_POLICY"].status == "DRAFT"


def test_ec_mig_04_legacy_overtime_multiplier_copied_into_draft_switch_stays_off():
    policies = {p.type: p for p in starter_policies("LEGACY_V1", legacy_values={"overtime": 1.5})}
    overtime = policies["OVERTIME_POLICY"]
    assert overtime.status == "DRAFT"
    assert [b["multiplier"] for b in overtime.fields["buckets"]] == ["1.5", None, None]
    with pytest.raises(PolicyValidationError) as error:
        starter_policies("LEGACY_V1", legacy_values={"overtime": "abc"})
    assert error.value.code == "LEGACY_VALUE_INVALID"
    with pytest.raises(PolicyValidationError):
        starter_policies("LEGACY_V1", legacy_values={"overtime": float("inf")})


def test_ec_mig_03_legacy_late_penalty_is_never_applied_automatically():
    late = next(p for p in starter_policies("LEGACY_V1") if p.type == "LATE_PENALTY_POLICY")
    assert late.status == "DRAFT"
    assert late.fields["penalty_type"] == "FIXED_AMOUNT" and late.fields["amount_per_late"] is None
    assert late.fields["free_late_marks"] == 0
    assert _error("LATE_PENALTY_POLICY", dict(late.fields), for_activation=True).field == "amount_per_late"


def test_starter_policies_errors():
    with pytest.raises(PolicyValidationError) as error:
        starter_policies("STARTER_V2")
    assert error.value.code == "UNKNOWN_PROFILE"
    with pytest.raises(PolicyValidationError) as error:
        starter_policies("STARTER_V1", legacy_values={"overtime": 1.5})
    assert error.value.code == "LEGACY_VALUES_NOT_ALLOWED"


# --------------------------------------------------------------------------- metadata for the frontend


def test_policy_type_metadata_describes_every_field():
    meta = {m["code"]: m for m in policy_type_metadata()}
    assert set(meta) == set(POLICY_TYPES)
    for code, entry in meta.items():
        names = [f["name"] for f in entry["fields"]]
        assert names == list(POLICY_TYPES[code].model.model_fields), code
        assert all("kind" in f for f in entry["fields"])
    overtime = {f["name"]: f for f in meta["OVERTIME_POLICY"]["fields"]}
    assert overtime["min_minutes"] == {"name": "min_minutes", "kind": "int", "minimum": 0, "maximum": 1440,
                                       "unit": "minutes", "default": 30, "help": "Shorter overtime is ignored.",
                                       "legal_sensitivity": "NONE"}
    bucket = {f["name"]: f for f in overtime["buckets"]["item"]["fields"]}
    assert bucket["multiplier"]["legal_sensitivity"] == "LEGAL_FLOOR" and bucket["multiplier"]["nullable"]
    holiday = {f["name"]: f for f in meta["HOLIDAY_PAY_POLICY"]["fields"]}
    assert holiday["holiday_work_multiplier"]["legal_sensitivity"] == "LEGAL_FLOOR"
    assert holiday["paid_holiday_types"]["choices"] == ["PUBLIC", "COMPANY", "OPTIONAL_AVAILED"]

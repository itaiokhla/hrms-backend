"""Loans and advances (3.4 §15, 3.1 §5.11) and reimbursement claims (3.4 §14, 3.1 §5.10)."""

from datetime import date
from decimal import Decimal as D

import pytest

from app.payroll.time_pay import (
    Claim,
    LoanAccount,
    LoanTransaction,
    TimePayError,
    advance_schedule,
    check_advance_request,
    check_claim,
    check_loan_application,
    claim_fingerprint,
    loan_balance,
    loan_recoveries,
    loan_recovery,
    loan_schedule,
    loan_schedule_from_policy,
    protected_net_room,
    reimbursement_lines,
)

FLAT = loan_schedule(D("12000.00"), 12, "INR", annual_rate_percent=D("10"))
DISBURSED = (LoanTransaction("DISBURSEMENT", D("12000.00")),)


def account(*transactions, status="ACTIVE", schedule=FLAT, loan_id="L1"):
    return LoanAccount(loan_id, schedule, DISBURSED + tuple(transactions), status)


# --------------------------------------------------------------------------- schedules


def test_flat_schedule_equal_instalments():
    assert [i.total for i in FLAT.instalments] == [D("1100.00")] * 12
    assert (FLAT.total_interest, FLAT.total_payable) == (D("1200.00"), D("13200.00"))
    assert FLAT.instalments[-1].balance_after == 0


def test_flat_schedule_last_instalment_absorbs_rounding():
    schedule = loan_schedule(D("10000.00"), 3, "INR")
    assert [i.total for i in schedule.instalments] == [D("3333.33"), D("3333.33"), D("3333.34")]
    assert sum(i.principal for i in schedule.instalments) == D("10000.00")


def test_reducing_schedule_emi():
    schedule = loan_schedule(D("100000.00"), 12, "INR", annual_rate_percent=D("12"), method="REDUCING")
    assert schedule.instalments[0] == schedule.instalments[0].__class__(1, D("7884.88"), D("1000.00"), D("8884.88"),
                                                                       D("92115.12"))
    assert all(i.total == D("8884.88") for i in schedule.instalments[:-1])
    assert schedule.instalments[-1].total == D("8884.85")
    assert sum(i.principal for i in schedule.instalments) == D("100000.00")
    assert schedule.total_payable == D("106618.53")
    zero = loan_schedule(D("1200"), 12, "INR", method="REDUCING")
    assert {i.total for i in zero.instalments} == {D("100.00")}


def test_schedule_in_zero_decimal_currency():
    schedule = loan_schedule(D("100000"), 3, "JPY")
    assert [i.total for i in schedule.instalments] == [D("33333"), D("33333"), D("33334")]
    assert schedule.to_dict()["instalments"][2]["total"] == "33334"


def test_schedule_validation():
    for kwargs in ({"tenure_months": 0}, {"method": "BALLOON"}, {"principal": D("100.001")},
                   {"principal": D("0.05"), "tenure_months": 12}):
        args = {"principal": D("1000"), "tenure_months": 12, "method": "FLAT", **kwargs}
        with pytest.raises(TimePayError) as err:
            loan_schedule(args["principal"], args["tenure_months"], "INR", method=args["method"])
        assert err.value.code == "INVALID_SCHEDULE"
    with pytest.raises(TimePayError) as err:
        loan_schedule_from_policy(D("1000"), 36, {"max_tenure_months": 24}, "INR")
    assert err.value.code == "INVALID_SCHEDULE"
    policy_schedule = loan_schedule_from_policy(D("12000"), 12, {"interest_rate_percent": "10"}, "INR")
    assert policy_schedule.total_payable == D("13200.00")


def test_loan_application_checks():
    issues = check_loan_application(D("60000"), 30, {}, "INR", max_amount=D("50000"), service_months=3,
                                    active_loans=1)
    assert {i.code for i in issues} == {"LOAN_ABOVE_MAXIMUM", "LOAN_TENURE_ABOVE_MAXIMUM", "LOAN_SERVICE_TOO_SHORT",
                                        "LOAN_TOO_MANY_ACTIVE"}
    share = check_loan_application(D("12000"), 12, {}, "INR", max_amount=D("12000"), service_months=12,
                                   active_loans=0, net_pay=D("1999.98"))
    assert [i.code for i in share] == ["LOAN_INSTALMENT_ABOVE_NET_SHARE"]
    assert check_loan_application(D("12000"), 12, {}, "INR", max_amount=D("12000"), service_months=12,
                                  active_loans=0, net_pay=D("2000.00")) == ()  # 1,000.00 is exactly 50 % of net


def test_loan_application_without_a_known_maximum_fails_closed():
    issues = check_loan_application(D("12000"), 12, {}, "INR", max_amount=None, service_months=12, active_loans=0)
    assert [(i.code, i.severity) for i in issues] == [("LOAN_MAXIMUM_UNKNOWN", "BLOCKING")]


# --------------------------------------------------------------------------- protected net


def test_protected_net_room():
    room = protected_net_room(D("50000"), D("20000"), "INR", min_net_pay_percent=D("30"))
    assert (room.floor, room.room) == (D("15000.00"), D("5000.00"))
    pack = protected_net_room(D("50000"), D("20000"), "INR", min_net_pay_percent=D("10"), pack_floor_percent=D("50"))
    assert pack.percent == D("50") and pack.room == 0
    odd = protected_net_room(D("333.33"), D("200"), "INR", min_net_pay_percent=D("50"))
    assert odd.floor == D("166.67")  # rounded up so net never ends below the floor
    unset = protected_net_room(D("50000"), D("20000"), "INR", min_net_pay_percent=None)
    assert unset.room is None


# --------------------------------------------------------------------------- recoveries (EC-VAR-05..08, EC-VAR-14)


def test_ec_var_05_partial_recovery_carries_the_rest_and_raises_exception():
    first = loan_recovery(account(), run_employee_id="re-1", room=D("500.00"))
    assert (first.due, first.recovered, first.carried_forward) == (D("1100.00"), D("500.00"), D("600.00"))
    assert first.issues[0].code == "LOAN_PARTIAL_RECOVERY" and first.line is not None
    assert first.line.amount == D("500.00") and first.line.component == "LOAN_RECOVERY"
    assert first.transaction == LoanTransaction("INSTALMENT", D("500.00"), "re-1", D("1100.00"))
    second = loan_recovery(account(first.transaction), run_employee_id="re-2", room=D("10000"))
    assert (second.instalment_number, second.arrears, second.due, second.recovered) == (2, D("600.00"), D("1700.00"),
                                                                                       D("1700.00"))


def test_ec_var_05_boundary_room_equal_to_instalment_and_zero_room():
    exact = loan_recovery(account(), run_employee_id="re-1", room=D("1100.00"))
    assert exact.recovered == D("1100.00") and exact.issues == ()
    zero = loan_recovery(account(), run_employee_id="re-1", room=D("0"))
    assert zero.recovered == 0 and zero.line is None and zero.carried_forward == D("1100.00")
    assert zero.transaction is not None and zero.transaction.amount == 0


def test_ec_var_06_paused_loan_is_skipped():
    paused = loan_recovery(account(status="PAUSED"), run_employee_id="re-1", room=D("5000"))
    assert paused.skipped_reason == "PAUSED" and paused.recovered == 0 and paused.transaction is None
    resumed = loan_recovery(account(), run_employee_id="re-2", room=D("5000"))
    assert resumed.instalment_number == 1  # the schedule moved on by one run, nothing was missed


def test_ec_var_08_regenerated_run_recovers_once():
    recorded = LoanTransaction("INSTALMENT", D("1100.00"), "re-1", D("1100.00"))
    again = loan_recovery(account(recorded), run_employee_id="re-1", room=D("5000"))
    assert again.replaces_existing and again.instalment_number == 1 and again.recovered == D("1100.00")
    assert again.balance_after == D("12100.00")
    assert loan_balance(account(recorded)) == D("12100.00")
    with pytest.raises(TimePayError):
        loan_balance(account(recorded, recorded))


def test_ec_var_07_final_settlement_recovers_balance_and_remainder_becomes_receivable():
    paid_one = LoanTransaction("INSTALMENT", D("1100.00"), "re-1", D("1100.00"))
    result = loan_recovery(account(paid_one), run_employee_id="re-fs", room=D("5000.00"), final_settlement=True)
    assert (result.due, result.recovered, result.receivable, result.carried_forward) == (
        D("12100.00"), D("5000.00"), D("7100.00"), D("0"))
    assert result.issues[0].code == "LOAN_RECEIVABLE"
    in_full = loan_recovery(account(paid_one), run_employee_id="re-fs", room=D("50000"), final_settlement=True)
    assert in_full.receivable == 0 and in_full.balance_after == 0


def test_ec_var_14_advance_over_three_runs_employee_leaves_after_one():
    schedule = advance_schedule(D("9000.00"), "INR", {"recover_in_runs": 3})
    assert [i.total for i in schedule.instalments] == [D("3000.00")] * 3
    advance = LoanAccount("A1", schedule, (LoanTransaction("DISBURSEMENT", D("9000.00")),))
    first = loan_recovery(advance, run_employee_id="re-1", room=D("10000"))
    assert first.line is not None and first.line.component == "ADVANCE_RECOVERY" and first.recovered == D("3000.00")
    assert first.transaction is not None
    leaver = LoanAccount("A1", schedule, advance.transactions + (first.transaction,))
    settlement = loan_recovery(leaver, run_employee_id="re-fs", room=D("10000"), final_settlement=True)
    assert settlement.recovered == D("6000.00") and settlement.balance_after == 0


def test_advance_schedule_limits_and_request_checks():
    assert [i.total for i in advance_schedule(D("100.00"), "INR", {"recover_in_runs": 3}).instalments] == [
        D("33.33"), D("33.33"), D("33.34")]
    with pytest.raises(TimePayError) as err:
        advance_schedule(D("900"), "INR", {"recover_in_runs": 2}, runs=3)
    assert err.value.code == "INVALID_SCHEDULE"
    with pytest.raises(TimePayError):
        advance_schedule(D("900"), "INR", {"recover_in_runs": 4})
    issues = check_advance_request(D("15000"), monthly_net=D("20000"), advances_this_year=2)
    assert {i.code for i in issues} == {"ADVANCE_ABOVE_MAXIMUM", "ADVANCE_YEARLY_LIMIT"}
    assert check_advance_request(D("10000"), monthly_net=D("20000"), advances_this_year=1) == ()


def test_recovery_blocked_without_protected_net_switch_off_or_not_disbursed():
    blocked = loan_recovery(account(), run_employee_id="re-1", room=None)
    assert blocked.recovered == 0 and blocked.issues[0].code == "PROTECTED_NET_NOT_SET"
    off = loan_recovery(account(), run_employee_id="re-1", room=D("5000"), enabled=False)
    assert off.skipped_reason == "SWITCH_OFF"
    undisbursed = loan_recovery(LoanAccount("L2", FLAT), run_employee_id="re-1", room=D("5000"))
    assert undisbursed.skipped_reason == "NOTHING_OUTSTANDING"
    with pytest.raises(TimePayError) as err:
        loan_balance(LoanAccount("L3", FLAT, (LoanTransaction("DISBURSEMENT", D("5000")),)))
    assert err.value.code == "INVALID_SCHEDULE"


def test_several_loans_share_one_room_in_priority_order():
    second = LoanAccount("L2", FLAT, DISBURSED)
    results = loan_recoveries([account(), second], run_employee_id="re-1", room=D("1500.00"))
    assert [r.recovered for r in results] == [D("1100.00"), D("400.00")]
    with pytest.raises(TimePayError):
        loan_recoveries([account(), account()], run_employee_id="re-1", room=D("1"))


def test_prepayment_and_waiver_reduce_balance_and_due():
    settled = account(LoanTransaction("PREPAYMENT", D("12700.00")))
    result = loan_recovery(settled, run_employee_id="re-1", room=D("5000"))
    assert result.due == D("500.00") and result.balance_after == 0
    waived = account(LoanTransaction("WAIVER", D("13200.00")))
    assert loan_recovery(waived, run_employee_id="re-1", room=D("5000")).skipped_reason == "NOTHING_OUTSTANDING"


# --------------------------------------------------------------------------- claims (EC-VAR-04, EC-VAR-09)


DIGEST = "ab" * 32
POLICY = {"claim_types": [{"code": "TRAVEL", "limit_per_claim": "5000", "limit_per_month": "8000",
                           "limit_per_year": "20000", "receipt_required_above": "500"}]}


def claim(amount="1000", **kwargs):
    kwargs.setdefault("receipt_digests", (DIGEST,))
    return Claim(kwargs.pop("claim_id", "c1"), kwargs.pop("claim_type", "TRAVEL"),
                 kwargs.pop("expense_date", date(2026, 10, 5)), D(amount), kwargs.pop("currency", "INR"),
                 merchant=kwargs.pop("merchant", "Blue Taxi Co"), **kwargs)


def test_ec_var_04_fingerprint_is_stable_and_normalised():
    one = claim_fingerprint(DIGEST, D("100.00"), "INR", date(2026, 10, 5), "Blue  Taxi Co")
    two = claim_fingerprint(DIGEST.upper(), D("100"), "INR", date(2026, 10, 5), "blue taxi co ")
    assert one == two and len(one) == 64
    assert claim_fingerprint(DIGEST, D("100.01"), "INR", date(2026, 10, 5), "Blue Taxi Co") != one
    assert claim_fingerprint(DIGEST, D("100"), "INR", date(2026, 10, 6), "Blue Taxi Co") != one
    with pytest.raises(TimePayError):
        claim_fingerprint("not-a-hash", D("100"), "INR", date(2026, 10, 5), "x")


def test_ec_var_04_duplicate_receipt_flagged_for_approver():
    fingerprint = claim_fingerprint(DIGEST, D("1000"), "INR", date(2026, 10, 5), "Blue Taxi Co")
    flagged = check_claim(claim(), POLICY, pay_currency="INR", known_fingerprints={fingerprint: "c0"})
    assert flagged.duplicates == ("c0",) and flagged.issues[0].code == "CLAIM_POSSIBLE_DUPLICATE"
    assert not flagged.blocked  # flagged, the approver decides
    reused = check_claim(claim("900"), POLICY, pay_currency="INR", known_receipts={DIGEST: "c7"})
    assert reused.duplicates == ("c7",)


def test_ec_var_04_no_flag_for_new_receipt_own_claim_or_check_off():
    clean = check_claim(claim(), POLICY, pay_currency="INR", known_fingerprints={"0" * 64: "c0"})
    assert clean.issues == () and clean.approvable_amount == D("1000")
    fingerprint = claim_fingerprint(DIGEST, D("1000"), "INR", date(2026, 10, 5), "Blue Taxi Co")
    own = check_claim(claim(), POLICY, pay_currency="INR", known_fingerprints={fingerprint: "c1"})
    assert own.duplicates == ()
    off = check_claim(claim(), {**POLICY, "duplicate_receipt_check": False}, pay_currency="INR",
                      known_fingerprints={fingerprint: "c0"})
    assert off.duplicates == ()


def test_claim_limits_per_claim_month_and_year():
    over = check_claim(claim("6000"), POLICY, pay_currency="INR")
    assert over.approvable_amount == D("5000") and over.issues[0].code == "CLAIM_ABOVE_PER_CLAIM_LIMIT"
    prior = [(claim("4000", claim_id="p1", expense_date=date(2026, 10, 1)), D("4000")),
             (claim("3000", claim_id="p2", expense_date=date(2026, 10, 2), status="REJECTED"), D("3000"))]
    month = check_claim(claim("5000"), POLICY, pay_currency="INR", prior_claims=prior)
    assert month.approvable_amount == D("4000") and month.issues[0].code == "CLAIM_ABOVE_MONTHLY_LIMIT"
    year_prior = [(claim("5000", claim_id=f"y{m}", expense_date=date(2026, m, 3)), D("5000")) for m in (4, 5, 6, 7)]
    year = check_claim(claim("1000"), POLICY, pay_currency="INR", prior_claims=year_prior, year_start="04-01")
    assert year.approvable_amount == 0 and year.issues[0].code == "CLAIM_ABOVE_YEARLY_LIMIT"
    calendar = check_claim(claim("1000"), POLICY, pay_currency="INR",
                           prior_claims=[(claim("5000", claim_id="old", expense_date=date(2025, 12, 3)), D("19500"))])
    assert calendar.issues == ()


def test_receipt_required_above_threshold_boundary():
    at = check_claim(claim("500", receipt_digests=()), POLICY, pay_currency="INR")
    assert not at.receipt_required and at.issues == ()
    above = check_claim(claim("500.01", receipt_digests=()), POLICY, pay_currency="INR")
    assert above.receipt_required and above.blocked and above.issues[0].code == "CLAIM_RECEIPT_REQUIRED"


def test_ec_var_09_foreign_currency_claim_converted_with_snapshot():
    usd = claim("100.00", currency="USD")
    converted = check_claim(usd, POLICY, pay_currency="INR", fx_rate=D("83.2567"), fx_rate_date=date(2026, 10, 5))
    assert converted.amount == D("8325.67") and converted.fx is not None
    assert (converted.fx.rate, converted.fx.rate_basis, converted.fx.source_amount) == (D("83.2567"), "EXPENSE_DATE",
                                                                                        D("100.00"))
    assert converted.approvable_amount == D("5000")


def test_ec_var_09_foreign_currency_without_rate_is_blocked():
    missing = check_claim(claim("100.00", currency="USD"), POLICY, pay_currency="INR")
    assert missing.amount is None and missing.blocked and missing.issues[0].code == "CLAIM_FX_RATE_MISSING"


def test_claim_type_window_and_switch():
    unknown = check_claim(claim(claim_type="MEALS"), POLICY, pay_currency="INR")
    assert unknown.issues[0].code == "CLAIM_TYPE_UNKNOWN"
    late = check_claim(claim(submitted_on=date(2026, 12, 15)), POLICY, pay_currency="INR")
    assert late.issues[0].code == "CLAIM_OUTSIDE_SUBMISSION_WINDOW"
    on_time = check_claim(claim(submitted_on=date(2026, 12, 4)), POLICY, pay_currency="INR")
    assert on_time.issues == ()
    off = check_claim(claim(), POLICY, pay_currency="INR", enabled=False)
    assert off.blocked


def test_ec_var_15_reimbursement_lines_are_not_wages():
    lines = reimbursement_lines([("c1", "TRAVEL", D("100.50")), ("c2", "TRAVEL", D("20")), ("c3", "MEALS", D("5"))],
                                "INR")
    assert [(line.kind, line.amount) for line in lines] == [("REIMBURSEMENT", D("5.00")),
                                                            ("REIMBURSEMENT", D("120.50"))]
    assert "not wages" in lines[1].explanation
    assert reimbursement_lines([("c1", "TRAVEL", D("1"))], "INR", payout_route="SEPARATE_PAYMENT") == ()


# --------------------------------------------------------------------------- review additions


def test_ec_var_06_active_loan_is_recovered_and_paused_loan_is_settled_on_exit():
    active = loan_recovery(account(), run_employee_id="re-1", room=D("5000"))
    assert active.skipped_reason is None and active.recovered == D("1100.00")
    paused_exit = loan_recovery(account(status="PAUSED"), run_employee_id="re-fs", room=D("50000"),
                                final_settlement=True)
    assert paused_exit.recovered == D("13200.00") and paused_exit.balance_after == 0


def test_ec_var_07_exit_without_balance_recovery_makes_the_rest_a_receivable():
    result = loan_recovery(account(), run_employee_id="re-fs", room=D("50000"), final_settlement=True,
                           recover_balance_on_exit=False)
    assert (result.due, result.recovered, result.receivable) == (D("1100.00"), D("1100.00"), D("12100.00"))
    assert result.issues[0].code == "LOAN_RECEIVABLE" and result.balance_after == D("12100.00")


def test_ec_var_08_a_different_run_employee_takes_the_next_instalment():
    recorded = LoanTransaction("INSTALMENT", D("1100.00"), "re-1", D("1100.00"))
    nxt = loan_recovery(account(recorded), run_employee_id="re-2", room=D("5000"))
    assert not nxt.replaces_existing and nxt.instalment_number == 2 and nxt.balance_after == D("11000.00")


def test_ec_var_14_boundary_leaver_with_little_room_leaves_a_receivable():
    schedule = advance_schedule(D("9000.00"), "INR", {"recover_in_runs": 3})
    advance = LoanAccount("A1", schedule, (LoanTransaction("DISBURSEMENT", D("9000.00")),))
    settlement = loan_recovery(advance, run_employee_id="re-fs", room=D("2500.00"), final_settlement=True)
    assert (settlement.recovered, settlement.receivable, settlement.carried_forward) == (
        D("2500.00"), D("6500.00"), D("0"))


def test_balance_left_after_the_last_instalment_is_due():
    schedule = loan_schedule(D("300.00"), 3, "INR")
    paid = tuple(LoanTransaction("INSTALMENT", D("100.00"), f"re-{n}", D("100.00")) for n in (1, 2, 3))
    loan = LoanAccount("L9", schedule, (LoanTransaction("DISBURSEMENT", D("300.00")),) + paid
                       + (LoanTransaction("ADJUSTMENT", D("40.00")),))
    result = loan_recovery(loan, run_employee_id="re-4", room=D("1000"))
    assert (result.instalment_number, result.due, result.recovered, result.balance_after) == (
        None, D("40.00"), D("40.00"), D("0.00"))


def test_ec_var_15_reimbursement_carries_pack_taxability_and_never_pays_twice():
    policy = {"claim_types": [{**POLICY["claim_types"][0], "taxability": "EXEMPT"}]}
    checked = check_claim(claim(), policy, pay_currency="INR")
    assert checked.taxability == "EXEMPT"
    assert check_claim(claim(), POLICY, pay_currency="INR").taxability == "UNCLASSIFIED"
    lines = reimbursement_lines([("c1", "TRAVEL", D("10"))], "INR", taxability={"TRAVEL": "EXEMPT"})
    assert lines[0].sources[0]["taxability"] == "EXEMPT" and lines[0].kind == "REIMBURSEMENT"
    with pytest.raises(TimePayError):
        reimbursement_lines([("c1", "TRAVEL", D("10")), ("c1", "TRAVEL", D("10"))], "INR")
    with pytest.raises(TimePayError):
        check_claim(claim(), {"claim_types": [{**POLICY["claim_types"][0], "taxability": "MAYBE"}]},
                    pay_currency="INR")


def test_claim_limits_accept_policy_types_money():
    policy = {"claim_types": [{"code": "TRAVEL", "limit_per_claim": {"amount": "5000.00", "currency": "INR"},
                               "receipt_required_above": {"amount": "0", "currency": None}}]}
    result = check_claim(claim("6000"), policy, pay_currency="INR")
    assert result.approvable_amount == D("5000.00") and result.receipt_required
    with pytest.raises(TimePayError) as err:
        check_claim(claim(), {"claim_types": [{"code": "TRAVEL",
                                               "limit_per_claim": {"amount": "50", "currency": "USD"}}]},
                    pay_currency="INR")
    assert err.value.code == "CURRENCY_MISMATCH"

"""Reconciliation of statement lines to payment instructions (3.7 §9), returns (§10) and value dates (EC-PAY-10)."""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal

import pytest

from app.payroll.banking.business_days import BusinessCalendar, payment_value_date
from app.payroll.banking.errors import BankValidationError, MatchingError
from app.payroll.banking.matching import (
    ALREADY_MATCHED,
    DUPLICATE,
    IGNORED,
    MATCHED,
    MISMATCH,
    RETURN,
    UNMATCHED,
    InstructionRef,
    accept_variance,
    find_overdue,
    match_lines,
    reconcile,
    references_in,
)
from app.payroll.banking.statements import CREDIT, DEBIT, StatementLine, parse_camt053

D = Decimal
FRI = date(2026, 10, 30)
ACCOUNT = "50100123456789"
_counter = iter(range(1, 10_000))


def _line(
    amount: str = "46666.67",
    *,
    direction: str = DEBIT,
    currency: str = "INR",
    day: date = FRI,
    reference: str | None = None,
    end_to_end_id: str | None = None,
    remittance: str | None = None,
    account: str | None = ACCOUNT,
    return_reason: str | None = None,
) -> StatementLine:
    row = next(_counter)
    return StatementLine(
        value_date=day, amount=D(amount), currency=currency, direction=direction, reference=reference, counterparty="Asha Rao",
        line_hash=hashlib.sha256(f"line-{row}".encode()).hexdigest(), end_to_end_id=end_to_end_id, remittance=remittance,
        counterparty_account=account, return_reason=return_reason, source_row=row,
    )


def _instruction(
    instruction_id: int = 3,
    amount: str = "46666.67",
    *,
    status: str = "SUBMITTED",
    reference: str | None = None,
    currency: str = "INR",
    expected: date | None = FRI,
    accounts: tuple[str, ...] = ("5010 0123 4567 89",),
    evidence: frozenset[str] = frozenset(),
) -> InstructionRef:
    return InstructionRef(
        instruction_id=instruction_id, reference=reference or f"PAY-2026-10-{instruction_id}", amount=D(amount), currency=currency,
        status=status, accounts=accounts, expected_settlement_date=expected, evidence_line_hashes=evidence,
    )


# --------------------------------------------------------------------------- matching by reference


def test_unique_reference_match_settles() -> None:
    (match,) = match_lines([_line(end_to_end_id="PAY-2026-10-3")], [_instruction()])
    assert (match.outcome, match.reason, match.instruction_id, match.settles) == (MATCHED, "REFERENCE", 3, True)
    assert not match.needs_review


def test_reference_token_in_remittance_but_not_prefix() -> None:
    instructions = [_instruction(1, reference="PAY-1"), _instruction(12, reference="PAY-12", amount="10.00")]
    line = _line("10.00", remittance="Salary PAY-12.", account=None)
    assert references_in(line, {"PAY-1": 1, "PAY-12": 12}) == (12,)
    (match,) = match_lines([line], instructions)
    assert (match.outcome, match.instruction_id) == (MATCHED, 12)
    assert references_in(_line(remittance="PAY-1-X"), {"PAY-1": 1}) == ()


def test_reference_match_takes_priority_over_amount_and_date() -> None:
    far = _line(end_to_end_id="PAY-2026-10-3", day=date(2026, 12, 1), account=None)
    (match,) = match_lines([far], [_instruction()])
    assert match.outcome == MATCHED


def test_line_with_two_instruction_references_is_a_mismatch() -> None:
    line = _line(reference="PAY-2026-10-3", remittance="also PAY-2026-10-4")
    (match,) = match_lines([line], [_instruction(3), _instruction(4)])
    assert (match.outcome, match.reason, match.candidate_ids) == (MISMATCH, "MULTIPLE_CANDIDATES", (3, 4))


# --------------------------------------------------------------------------- matching by amount + account + date


def test_fallback_window_three_business_days_each_side() -> None:
    inside = [_line(day=date(2026, 11, 4)), _line(day=date(2026, 10, 27))]
    for line in inside:
        (match,) = match_lines([line], [_instruction()])
        assert (match.outcome, match.reason) == (MATCHED, "AMOUNT_ACCOUNT_DATE"), line.value_date
    outside = [_line(day=date(2026, 11, 5)), _line(day=date(2026, 10, 26))]
    for line in outside:
        (match,) = match_lines([line], [_instruction()])
        assert (match.outcome, match.reason) == (UNMATCHED, "NO_CANDIDATE"), line.value_date


def test_fallback_window_skips_bank_holidays() -> None:
    calendar = BusinessCalendar(holidays=frozenset({date(2026, 11, 2)}))
    (match,) = match_lines([_line(day=date(2026, 11, 5))], [_instruction()], calendar=calendar)
    assert match.outcome == MATCHED


def test_fallback_needs_exact_amount_account_and_currency() -> None:
    instruction = _instruction()
    for line in (_line("46666.68"), _line(account="999999999"), _line(account=None), _line(currency="USD")):
        assert match_lines([line], [instruction])[0].outcome == UNMATCHED


def test_fallback_without_expected_date_fails_closed() -> None:
    (match,) = match_lines([_line()], [_instruction(expected=None)])
    assert match.outcome == UNMATCHED


def test_fallback_several_candidates_is_a_mismatch() -> None:
    (match,) = match_lines([_line()], [_instruction(3), _instruction(4)])
    assert (match.outcome, match.reason, match.candidate_ids, match.settles) == (MISMATCH, "MULTIPLE_CANDIDATES", (3, 4), False)


# --------------------------------------------------------------------------- EC-PAY-11 / EC-MON-18


def test_ec_pay_11_amount_differs_never_auto_settled() -> None:
    (match,) = match_lines([_line("46646.67", end_to_end_id="PAY-2026-10-3")], [_instruction()])
    assert (match.outcome, match.reason, match.settles, match.variance) == (MISMATCH, "AMOUNT_DIFFERS", False, D("-20.00"))
    assert match.needs_review


def test_ec_pay_11_exact_amount_settles() -> None:
    (match,) = match_lines([_line("46666.67", end_to_end_id="PAY-2026-10-3")], [_instruction()])
    assert match.settles and match.variance is None


def test_ec_pay_11_currency_differs_is_a_mismatch() -> None:
    (match,) = match_lines([_line("560.00", currency="USD", end_to_end_id="PAY-2026-10-3")], [_instruction()])
    assert (match.outcome, match.reason, match.variance) == (MISMATCH, "CURRENCY_DIFFERS", None)


def test_ec_mon_18_accepted_variance_needs_reason() -> None:
    instruction = _instruction()
    (match,) = match_lines([_line("46646.67", end_to_end_id="PAY-2026-10-3")], [instruction])
    accepted = accept_variance(match, instruction, "Intermediary bank fee")
    assert (accepted.variance, accepted.currency, accepted.reason, accepted.instruction_id) == (D("-20.00"), "INR", "Intermediary bank fee", 3)
    with pytest.raises(MatchingError) as exc:
        accept_variance(match, instruction, "  ")
    assert exc.value.code == "REASON_REQUIRED"


def test_ec_mon_18_matched_line_is_not_a_variance() -> None:
    instruction = _instruction()
    (match,) = match_lines([_line(end_to_end_id="PAY-2026-10-3")], [instruction])
    with pytest.raises(MatchingError) as exc:
        accept_variance(match, instruction, "fee")
    assert exc.value.code == "NOT_A_VARIANCE"


# --------------------------------------------------------------------------- EC-PAY-12 and duplicates


def test_ec_pay_12_unknown_debit_goes_to_unmatched_queue() -> None:
    result = reconcile([_line("999.00", reference="RENT")], [_instruction()])
    (match,) = result.matches
    assert (match.outcome, match.reason, match.needs_review) == (UNMATCHED, "NO_CANDIDATE", True)
    assert result.counts[UNMATCHED] == 1 and result.settled_instruction_ids == ()


def test_ec_pay_12_known_debit_is_not_unmatched() -> None:
    result = reconcile([_line(reference="PAY-2026-10-3")], [_instruction()])
    assert result.counts[UNMATCHED] == 0 and result.settled_instruction_ids == (3,)


def test_two_debits_for_one_instruction_second_is_duplicate() -> None:
    lines = [_line(end_to_end_id="PAY-2026-10-3"), _line(remittance="PAY-2026-10-3", day=date(2026, 11, 2))]
    first, second = match_lines(lines, [_instruction()])
    assert (first.outcome, first.settles) == (MATCHED, True)
    assert (second.outcome, second.reason, second.instruction_id, second.settles) == (DUPLICATE, "SECOND_DEBIT", 3, False)


def test_exact_amount_line_preferred_over_earlier_mismatch() -> None:
    lines = [_line("10.00", end_to_end_id="PAY-2026-10-3"), _line(end_to_end_id="PAY-2026-10-3")]
    first, second = match_lines(lines, [_instruction()])
    assert (first.outcome, second.outcome) == (DUPLICATE, MATCHED)


def test_debit_for_already_settled_instruction_is_duplicate() -> None:
    settled = _instruction(status="SETTLED")
    by_reference, by_amount = match_lines([_line(end_to_end_id="PAY-2026-10-3"), _line(day=date(2026, 11, 2))], [settled])
    assert by_reference.outcome == DUPLICATE and by_amount.outcome == DUPLICATE


def test_reimported_evidence_line_is_not_a_duplicate() -> None:
    line = _line(end_to_end_id="PAY-2026-10-3")
    instruction = _instruction(status="SETTLED", evidence=frozenset({line.line_hash}))
    (match,) = match_lines([line], [instruction])
    assert (match.outcome, match.instruction_id, match.settles) == (ALREADY_MATCHED, 3, False)


def test_debit_for_cancelled_instruction_needs_review() -> None:
    (match,) = match_lines([_line(end_to_end_id="PAY-2026-10-3")], [_instruction(status="CANCELLED")])
    assert (match.outcome, match.reason, match.settles) == (MISMATCH, "INSTRUCTION_NOT_PAYABLE", False)


# --------------------------------------------------------------------------- returns (3.7 §10, EC-PAY-04)


def test_ec_pay_04_return_credit_matched_to_instruction() -> None:
    line = _line(direction=CREDIT, end_to_end_id="PAY-2026-10-3", return_reason="AC04")
    (match,) = match_lines([line], [_instruction(status="SETTLED")])
    assert (match.outcome, match.instruction_id, match.settles) == (RETURN, 3, False)
    assert "account is closed" in match.message


def test_ec_pay_04_return_by_amount_and_account() -> None:
    line = _line(direction=CREDIT, return_reason="AC04", day=date(2026, 11, 3))
    (match,) = match_lines([line], [_instruction()])
    assert (match.outcome, match.reason) == (RETURN, "AMOUNT_ACCOUNT_DATE")


def test_unrelated_credit_ignored_unknown_return_unmatched() -> None:
    ignored, unknown = match_lines([_line("5000.00", direction=CREDIT, reference="FUNDING"), _line("1.00", direction=CREDIT, return_reason="AC01")], [_instruction()])
    assert ignored.outcome == IGNORED and not ignored.needs_review
    assert unknown.outcome == UNMATCHED and unknown.needs_review


# --------------------------------------------------------------------------- overdue, EC-PAY-01, EC-PAY-02


def test_overdue_instruction_without_evidence() -> None:
    instructions = [_instruction(3, status="ACKNOWLEDGED"), _instruction(4, status="SETTLED"), _instruction(5, status="SUBMITTED", expected=date(2026, 11, 5))]
    (item,) = find_overdue(instructions, date(2026, 11, 3))
    assert (item.instruction_id, item.business_days_overdue, item.status) == (3, 2, "ACKNOWLEDGED")
    assert find_overdue(instructions, date(2026, 11, 3), grace=2) == ()
    assert find_overdue(instructions, FRI) == ()


def test_ec_pay_02_acknowledged_without_statement_is_not_settled_and_becomes_overdue() -> None:
    result = reconcile([], [_instruction(status="ACKNOWLEDGED")], as_of=date(2026, 11, 2))
    assert result.settled_instruction_ids == ()
    assert [item.instruction_id for item in result.overdue] == [3]


def test_ec_pay_01_queued_file_never_settled_without_evidence() -> None:
    result = reconcile([_line("1.00", reference="OTHER")], [_instruction(status="QUEUED")], as_of=date(2026, 11, 3))
    assert result.settled_instruction_ids == ()
    assert [item.instruction_id for item in result.overdue] == [3]


def test_matched_instruction_is_not_overdue() -> None:
    result = reconcile([_line(end_to_end_id="PAY-2026-10-3")], [_instruction()], as_of=date(2026, 11, 10))
    assert result.overdue == () and result.settled_instruction_ids == (3,)


def test_ec_pay_10_bank_holiday_shifts_value_date_and_warns() -> None:
    calendar = BusinessCalendar(holidays=frozenset({FRI}))
    shifted = payment_value_date(FRI, FRI, calendar)
    assert shifted.value_date == date(2026, 11, 2) and shifted.warnings == ("VALUE_DATE_SHIFTED", "VALUE_DATE_AFTER_PAY_DATE")
    earlier = payment_value_date(FRI, FRI, calendar, convention="PRECEDING")
    assert earlier.value_date == date(2026, 10, 29) and earlier.warnings == ("VALUE_DATE_SHIFTED",)
    plain = payment_value_date(date(2026, 10, 29), FRI, calendar)
    assert not plain.shifted and plain.warnings == ()
    with pytest.raises(BankValidationError):
        BusinessCalendar(weekend=frozenset(range(7)))


def test_business_day_distance() -> None:
    calendar = BusinessCalendar()
    assert calendar.distance(FRI, date(2026, 11, 2)) == 1
    assert calendar.distance(FRI, date(2026, 10, 31)) == 0
    assert calendar.distance(date(2026, 11, 2), FRI) == -1
    assert calendar.add(FRI, 3) == date(2026, 11, 4) and calendar.add(FRI, -3) == date(2026, 10, 27)


# --------------------------------------------------------------------------- end to end and arguments


def test_camt053_statement_reconciles_end_to_end() -> None:
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08"><BkToCstmrStmt><Stmt><Id>S1</Id>
<Acct><Id><IBAN>AE070331234567890123456</IBAN></Id></Acct>
<Ntry><Amt Ccy="INR">46666.67</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts><ValDt><Dt>2026-10-30</Dt></ValDt>
<NtryDtls><TxDtls><Refs><EndToEndId>PAY-2026-10-3</EndToEndId></Refs>
<RltdPties><CdtrAcct><Id><Othr><Id>{ACCOUNT}</Id></Othr></Id></CdtrAcct></RltdPties></TxDtls></NtryDtls></Ntry>
<Ntry><Amt Ccy="INR">1200.00</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts><ValDt><Dt>2026-10-30</Dt></ValDt>
<NtryDtls><TxDtls><RltdPties><CdtrAcct><Id><Othr><Id>{ACCOUNT}</Id></Othr></Id></CdtrAcct></RltdPties></TxDtls></NtryDtls></Ntry>
<Ntry><Amt Ccy="INR">1200.00</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts><ValDt><Dt>2026-10-30</Dt></ValDt></Ntry>
</Stmt></BkToCstmrStmt></Document>""".encode()
    parsed = parse_camt053(xml)
    instructions = [_instruction(3), _instruction(4, amount="1200.00"), _instruction(5, amount="900.00")]
    result = reconcile(parsed.lines, instructions, as_of=date(2026, 11, 6))
    assert [m.outcome for m in result.matches] == [MATCHED, MATCHED, UNMATCHED]
    assert result.settled_instruction_ids == (3, 4)
    assert [item.instruction_id for item in result.overdue] == [5]


def test_results_are_deterministic_whatever_the_instruction_order() -> None:
    lines = [_line(end_to_end_id="PAY-2026-10-3"), _line("5.00"), _line()]
    instructions = [_instruction(3), _instruction(4)]
    assert match_lines(lines, instructions) == match_lines(lines, list(reversed(instructions)))


def test_invalid_arguments_raise_typed_errors() -> None:
    with pytest.raises(MatchingError) as negative:
        _instruction(amount="-1")
    assert negative.value.code == "INVALID_INSTRUCTION"
    with pytest.raises(MatchingError):
        InstructionRef(1, "PAY 1", D("1"), "INR", "SUBMITTED")
    with pytest.raises(MatchingError):
        InstructionRef(1, "PAY-1", 1.0, "INR", "SUBMITTED")  # type: ignore[arg-type]
    line = _line()
    with pytest.raises(MatchingError) as dup_line:
        match_lines([line, line], [])
    assert dup_line.value.code == "DUPLICATE_LINE"
    with pytest.raises(MatchingError) as dup_ref:
        match_lines([], [_instruction(1, reference="X"), _instruction(2, reference="X")])
    assert dup_ref.value.code == "DUPLICATE_REFERENCE"
    with pytest.raises(MatchingError):
        match_lines([], [], window=-1)
    with pytest.raises(MatchingError):
        match_lines(["line"], [])  # type: ignore[list-item]


def test_instruction_repr_masks_accounts() -> None:
    text = repr(_instruction(accounts=(ACCOUNT, "AE070331234567890123456")))
    assert ACCOUNT not in text and "AE070331234567890123456" not in text
    assert "••••6789" in text and "AE••••3456" in text

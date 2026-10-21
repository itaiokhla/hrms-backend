"""Payment files (3.7 §7.1): offered formats only, deterministic bytes, totals, hashes and CSV injection (EC-DOC-17)."""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.payroll.banking.errors import BankFileError
from app.payroll.banking.files import (
    DRAFT_NOT_OFFERED,
    FORMATS,
    OFFERED,
    PaymentRecord,
    WpsEmployer,
    WpsSalaryRecord,
    draft_wps_sif,
    escape_csv_cell,
    generate_bank_file,
    generic_csv,
    offered_formats,
    require_offered_format,
    verify_regeneration,
)

D = Decimal
AE_IBAN = "AE070331234567890123456"


def _records() -> list[PaymentRecord]:
    return [
        PaymentRecord(12, "PAY-2026-10-12", '=HYPERLINK("x")', D("1500"), "JPY", account_number="1234567", routing_scheme="BANK_CODE", routing_code="0001"),
        PaymentRecord(3, "PAY-2026-10-3", "Asha Rao", D("46666.67"), "INR", account_number="50100123456789", routing_scheme="IFSC", routing_code="HDFC0001234"),
        PaymentRecord(7, "PAY-2026-10-7", "Omar Ali", D("12.345"), "KWD", iban=AE_IBAN, bic="EBILAEAD"),
    ]


EXPECTED = (
    "record_type,batch_reference,sequence,instruction_id,instruction_reference,payee_name,account_masked,account_number,iban,"
    "routing_scheme,routing_code,bic,amount,currency,value_date,record_count\r\n"
    "D,B-2026-10-01,1,3,PAY-2026-10-3,Asha Rao,****6789,50100123456789,,IFSC,HDFC0001234,,46666.67,INR,2026-10-30,\r\n"
    "D,B-2026-10-01,2,7,PAY-2026-10-7,Omar Ali,AE****3456,,AE070331234567890123456,,,EBILAEAD,12.345,KWD,2026-10-30,\r\n"
    "D,B-2026-10-01,3,12,PAY-2026-10-12,\"'=HYPERLINK(\"\"x\"\")\",****567,1234567,,BANK_CODE,0001,,1500,JPY,2026-10-30,\r\n"
    "T,B-2026-10-01,,,,,,,,,,,46666.67,INR,,1\r\n"
    "T,B-2026-10-01,,,,,,,,,,,1500,JPY,,1\r\n"
    "T,B-2026-10-01,,,,,,,,,,,12.345,KWD,,1\r\n"
)


def test_generic_csv_exact_bytes_sorted_by_instruction_id() -> None:
    file = generate_bank_file("GENERIC_CSV", "B-2026-10-01", _records(), value_date=date(2026, 10, 30))
    assert file.content.decode("utf-8") == EXPECTED
    assert file.sha256 == hashlib.sha256(EXPECTED.encode("utf-8")).hexdigest()
    assert (file.format, file.status, file.filename, file.media_type) == ("GENERIC_CSV", OFFERED, "B-2026-10-01.csv", "text/csv")
    assert file.record_count == 3
    assert dict(file.totals) == {"INR": D("46666.67"), "JPY": D("1500"), "KWD": D("12.345")}
    assert dict(file.counts) == {"INR": 1, "JPY": 1, "KWD": 1}


def test_regeneration_is_byte_identical_whatever_the_input_order() -> None:
    first = generic_csv("B-1", _records())
    second = generic_csv("B-1", list(reversed(_records())))
    assert first.content == second.content and first.sha256 == second.sha256
    assert generate_bank_file("GENERIC_CSV", "B-1", _records(), expected_sha256=first.sha256.upper()).sha256 == first.sha256


def test_regeneration_mismatch_fails_loudly() -> None:
    stored = generic_csv("B-1", _records())
    changed = [replace(_records()[0], amount=D("1501"))] + _records()[1:]
    with pytest.raises(BankFileError) as exc:
        generate_bank_file("GENERIC_CSV", "B-1", changed, expected_sha256=stored.sha256)
    assert exc.value.code == "REGENERATION_MISMATCH"
    with pytest.raises(BankFileError):
        verify_regeneration(stored, "0" * 64)


def test_ec_pay_13_same_batch_gives_same_hash_for_duplicate_detection() -> None:
    """EC-PAY-13: the stored file hash identifies a batch file; regenerating it yields the same hash."""
    a, b = generic_csv("B-9", _records()), generic_csv("B-9", _records())
    assert a.sha256 == b.sha256
    assert generic_csv("B-10", _records()).sha256 != a.sha256


def test_trailer_totals_per_currency_exact() -> None:
    records = [
        PaymentRecord(i, f"PAY-{i}", "Asha Rao", D("0.10"), "USD", account_number="000123456789", routing_scheme="ABA", routing_code="021000021")
        for i in range(1, 11)
    ]
    file = generic_csv("B-USD", records)
    rows = list(csv.reader(io.StringIO(file.content.decode())))
    trailer = rows[-1]
    assert trailer[0] == "T" and trailer[12:] == ["1.00", "USD", "", "10"]
    assert file.totals["USD"] == D("1.00")


def test_full_account_and_masked_reference_in_each_row() -> None:
    rows = list(csv.reader(io.StringIO(generic_csv("B-1", _records()).content.decode())))
    header, first = rows[0], rows[1]
    row = dict(zip(header, first))
    assert row["account_number"] == "50100123456789" and row["account_masked"] == "****6789"
    assert row["routing_scheme"] == "IFSC" and row["routing_code"] == "HDFC0001234"
    assert row["instruction_reference"] == "PAY-2026-10-3"


# --------------------------------------------------------------------------- EC-DOC-17


@pytest.mark.parametrize("cell", ["=1+1", "+44", "-2", "@SUM(A1)", "\tx", "\rx"])
def test_ec_doc_17_formula_triggers_escaped(cell: str) -> None:
    assert escape_csv_cell(cell) == "'" + cell


@pytest.mark.parametrize("cell", ["Asha Rao", "1500", "a=b", "O'Brien", ""])
def test_ec_doc_17_ordinary_cells_untouched(cell: str) -> None:
    assert escape_csv_cell(cell) == cell


def test_ec_doc_17_payee_name_with_formula_escaped_in_file() -> None:
    rows = list(csv.reader(io.StringIO(generic_csv("B-1", _records()).content.decode())))
    names = [row[5] for row in rows[1:4]]
    assert "'=HYPERLINK(\"x\")" in names
    assert all(not name.startswith(("=", "+", "-", "@")) for name in names)


def test_ec_doc_17_negative_amount_rejected_not_escaped() -> None:
    bad = [replace(_records()[1], amount=D("-10.00"))]
    with pytest.raises(BankFileError) as exc:
        generic_csv("B-1", bad)
    assert exc.value.code == "NON_POSITIVE_AMOUNT"
    with pytest.raises(BankFileError):
        generic_csv("B-1", [replace(_records()[1], amount=D("0.00"))])


# --------------------------------------------------------------------------- record validation


@pytest.mark.parametrize(
    "change,code",
    [
        ({"amount": D("10.001")}, "AMOUNT_PRECISION"),
        ({"amount": 10.5}, "INVALID_AMOUNT"),
        ({"currency": "CLF"}, "CURRENCY_NOT_PAYABLE"),
        ({"reference": "bad ref"}, "INVALID_REFERENCE"),
        ({"payee_name": "Asha‮Rao"}, "INVALID_PAYEE_NAME"),
        ({"account_number": None}, "MISSING_ACCOUNT"),
        ({"routing_code": "HDFC1001234"}, "INVALID_ROUTING"),
        ({"routing_scheme": None, "routing_code": None}, "MISSING_ROUTING"),
        ({"bic": "XX"}, "INVALID_BIC"),
        ({"instruction_id": 0}, "INVALID_INSTRUCTION_ID"),
    ],
)
def test_invalid_records_rejected(change: dict[str, object], code: str) -> None:
    record = replace(_records()[1], **change)  # type: ignore[arg-type]
    with pytest.raises(BankFileError) as exc:
        generic_csv("B-1", [record])
    assert exc.value.code == code


def test_invalid_iban_and_duplicates_rejected() -> None:
    with pytest.raises(BankFileError) as bad_iban:
        generic_csv("B-1", [replace(_records()[2], iban=AE_IBAN[:-1] + "7")])
    assert bad_iban.value.code == "INVALID_IBAN"
    record = _records()[1]
    with pytest.raises(BankFileError) as dup:
        generic_csv("B-1", [record, record])
    assert dup.value.code == "DUPLICATE_INSTRUCTION"
    with pytest.raises(BankFileError) as dup_ref:
        generic_csv("B-1", [record, replace(record, instruction_id=99)])
    assert dup_ref.value.code == "DUPLICATE_REFERENCE"
    with pytest.raises(BankFileError) as empty:
        generic_csv("B-1", [])
    assert empty.value.code == "EMPTY_BATCH"
    with pytest.raises(BankFileError) as ref:
        generic_csv("=cmd", [record])
    assert ref.value.code == "INVALID_BATCH_REFERENCE"


def test_record_repr_masks_account_and_name() -> None:
    text = repr(_records()[1])
    assert "50100123456789" not in text and "Asha Rao" not in text and "6789" in text


# --------------------------------------------------------------------------- format registry


def test_only_generic_csv_is_offered() -> None:
    assert [fmt.code for fmt in offered_formats()] == ["GENERIC_CSV"]
    assert require_offered_format("generic_csv").code == "GENERIC_CSV"
    assert FORMATS["AE_WPS_SIF"].status == DRAFT_NOT_OFFERED


@pytest.mark.parametrize("code,error", [("AE_WPS_SIF", "FORMAT_NOT_OFFERED"), ("US_NACHA", "FORMAT_NOT_OFFERED"), ("HDFC_BULK", "FORMAT_UNKNOWN")])
def test_unknown_or_unverified_formats_refused(code: str, error: str) -> None:
    with pytest.raises(BankFileError) as exc:
        require_offered_format(code)
    assert exc.value.code == error
    with pytest.raises(BankFileError):
        generate_bank_file(code, "B-1", _records())


def _wps() -> tuple[WpsEmployer, list[WpsSalaryRecord]]:
    employer = WpsEmployer("1234567890123", "803320101", date(2026, 10, 1), datetime(2026, 10, 28, 14, 5, 9), "OCT")
    records = [
        WpsSalaryRecord(5, "10000000000002", "803320101", AE_IBAN, date(2026, 10, 1), date(2026, 10, 31), 31, D("8000.00"), D("500.50"), 0),
        WpsSalaryRecord(2, "10000000000001", "803320101", AE_IBAN, date(2026, 10, 1), date(2026, 10, 31), 31, D("12000.00"), D("0.00"), 2),
    ]
    return employer, records


def test_draft_wps_sif_is_marked_not_offered_and_deterministic() -> None:
    employer, records = _wps()
    file = draft_wps_sif(employer, records)
    assert file.status == DRAFT_NOT_OFFERED
    lines = file.content.decode("ascii").split("\r\n")
    assert lines[0].startswith("EDR,10000000000001") and lines[1].startswith("EDR,10000000000002")
    assert lines[2] == "SCR,1234567890123,803320101,2026-10-28,1405,102026,2,20500.50,AED,OCT"
    assert file.filename == "1234567890123261028140509.SIF"
    assert draft_wps_sif(employer, list(reversed(records))).sha256 == file.sha256


def test_draft_wps_sif_rejects_bad_data() -> None:
    employer, records = _wps()
    with pytest.raises(BankFileError):
        draft_wps_sif(replace(employer, employer_id="123"), records)
    with pytest.raises(BankFileError):
        draft_wps_sif(employer, [replace(records[0], iban="SA0380000000608010167519")])
    with pytest.raises(BankFileError):
        draft_wps_sif(employer, [replace(records[0], fixed_pay=D("-1.00"))])

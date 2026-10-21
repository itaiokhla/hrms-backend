"""Bank statement parsing (3.7 §9): CSV mappings, CAMT.053, XXE rejection and idempotent line hashes (3.7 §8.5)."""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal

import pytest

from app.payroll.banking.errors import StatementError
from app.payroll.banking.statements import (
    CREDIT,
    DEBIT,
    CsvColumnMapping,
    parse_camt053,
    parse_csv_statement,
    return_reason_message,
)

D = Decimal
NS = "urn:iso:std:iso:20022:tech:xsd:camt.053.001.08"

MAPPING = CsvColumnMapping(
    date="Value Date", amount="Amount", currency="Currency", reference="Reference", counterparty="Beneficiary",
    counterparty_account="Account", date_format="%d/%m/%Y",
)

CSV = (
    "Value Date,Amount,Currency,Reference,Beneficiary,Account\r\n"
    "30/10/2026,\"-46,666.67\",INR,PAY-2026-10-3,Asha Rao,5010 0123 4567 89\r\n"
    "30/10/2026,-1500,JPY,PAY-2026-10-12,Ken Sato,1234567\r\n"
    "31/10/2026,250000.00,INR,FUNDING,Treasury,\r\n"
    "\r\n"
    "31/10/2026,abc,INR,X,Y,\r\n"
)


def test_csv_with_signed_amounts() -> None:
    parsed = parse_csv_statement(CSV.encode("utf-8"), MAPPING)
    assert parsed.format == "CSV"
    assert parsed.file_sha256 == hashlib.sha256(CSV.encode("utf-8")).hexdigest()
    first, second, third = parsed.lines
    assert (first.value_date, first.amount, first.currency, first.direction) == (date(2026, 10, 30), D("46666.67"), "INR", DEBIT)
    assert first.reference == "PAY-2026-10-3" and first.counterparty == "Asha Rao"
    assert first.counterparty_account == "50100123456789"
    assert (second.amount, second.direction) == (D("1500"), DEBIT)
    assert (third.amount, third.direction) == (D("250000.00"), CREDIT)
    assert [(r.source_row, r.code) for r in parsed.rejected] == [(6, "AMOUNT_INVALID")]


def test_csv_debit_credit_columns_and_european_numbers() -> None:
    mapping = CsvColumnMapping(
        date="Datum", debit="Soll", credit="Haben", default_currency="EUR", reference="Verwendungszweck",
        delimiter=";", decimal_separator=",", thousands_separator=".", date_format="%d.%m.%Y",
    )
    data = "Datum;Soll;Haben;Verwendungszweck\n30.10.2026;1.234,56;;PAY-1\n30.10.2026;;10,00;REFUND\n30.10.2026;1,00;2,00;BOTH\n".encode()
    parsed = parse_csv_statement(data, mapping)
    assert [(line.amount, line.direction, line.currency) for line in parsed.lines] == [(D("1234.56"), DEBIT, "EUR"), (D("10.00"), CREDIT, "EUR")]
    assert [r.code for r in parsed.rejected] == ["DIRECTION_AMBIGUOUS"]


@pytest.mark.parametrize("text,amount", [("\"-1,00,000.00\"", D("100000.00")), ("\"-12,34,567.89\"", D("1234567.89")), ("\"-1,234,567.89\"", D("1234567.89"))])
def test_csv_western_and_indian_digit_grouping(text: str, amount: Decimal) -> None:
    mapping = CsvColumnMapping(date="date", amount="amount", currency="ccy")
    (line,) = parse_csv_statement(f"date,amount,ccy\n2026-10-01,{text},INR\n".encode(), mapping).lines
    assert (line.amount, line.direction) == (amount, DEBIT)


def test_csv_direction_column() -> None:
    mapping = CsvColumnMapping(date="date", amount="amount", direction="dc", default_currency="AED")
    data = b"date,amount,dc\n2026-10-30,100.00,DR\n2026-10-30,5.00,C\n2026-10-30,5.00,?\n"
    parsed = parse_csv_statement(data, mapping)
    assert [line.direction for line in parsed.lines] == [DEBIT, CREDIT]
    assert parsed.rejected[0].code == "DIRECTION_INVALID"


@pytest.mark.parametrize(
    "row,code",
    [
        ("2026-13-01,-1.00,INR", "DATE_INVALID"),
        ("2026-10-01,-1.001,INR", "AMOUNT_PRECISION"),
        ("2026-10-01,-1.00,XYZ", "UNKNOWN_CURRENCY"),
        ("2026-10-01,0.00,INR", "ZERO_AMOUNT"),
        ("2026-10-01,\"1,0,00.00\",INR", "AMOUNT_INVALID"),
        ("2026-10-01,\"1234,567.00\",INR", "AMOUNT_INVALID"),
        ("2026-10-01,,INR", "AMOUNT_MISSING"),
    ],
)
def test_csv_bad_rows_returned_as_rejections(row: str, code: str) -> None:
    mapping = CsvColumnMapping(date="date", amount="amount", currency="ccy")
    parsed = parse_csv_statement(f"date,amount,ccy\n{row}\n".encode(), mapping)
    assert parsed.lines == () and parsed.rejected[0].code == code


def test_csv_mapping_and_file_errors_raise() -> None:
    with pytest.raises(StatementError) as missing:
        parse_csv_statement(b"a,b\n1,2\n", MAPPING)
    assert missing.value.code == "COLUMN_MISSING"
    with pytest.raises(StatementError) as invalid:
        parse_csv_statement(b"x", CsvColumnMapping(date="d"))
    assert invalid.value.code == "MAPPING_INVALID"
    with pytest.raises(StatementError) as big:
        parse_csv_statement(CSV.encode(), MAPPING, max_bytes=10)
    assert big.value.code == "FILE_TOO_LARGE"
    with pytest.raises(StatementError) as decode:
        parse_csv_statement(b"\xff\xfe\x00", MAPPING)
    assert decode.value.code == "DECODE_ERROR"


def test_line_hash_idempotent_on_reimport_and_keeps_identical_debits() -> None:
    data = b"date,amount,ccy,ref\n2026-10-30,-100.00,INR,PAY-1\n2026-10-30,-100.00,INR,PAY-1\n"
    mapping = CsvColumnMapping(date="date", amount="amount", currency="ccy", reference="ref")
    first, again = parse_csv_statement(data, mapping), parse_csv_statement(data, mapping)
    assert [l.line_hash for l in first.lines] == [l.line_hash for l in again.lines]
    assert first.file_sha256 == again.file_sha256
    assert len(first.lines) == 2 and first.lines[0].line_hash != first.lines[1].line_hash


def test_line_hash_changes_with_content() -> None:
    mapping = CsvColumnMapping(date="date", amount="amount", currency="ccy", reference="ref")
    a = parse_csv_statement(b"date,amount,ccy,ref\n2026-10-30,-100.00,INR,PAY-1\n", mapping).lines[0]
    b = parse_csv_statement(b"date,amount,ccy,ref\n2026-10-30,-100.01,INR,PAY-1\n", mapping).lines[0]
    assert a.line_hash != b.line_hash


# --------------------------------------------------------------------------- CAMT.053


def _entry(amount: str, ccy: str, ind: str, *, status: str = "BOOK", e2e: str | None = "PAY-2026-10-3", extra_tx: str = "", tx: bool = True) -> str:
    details = ""
    if tx:
        details = f"""
        <NtryDtls><TxDtls>
          <Refs><EndToEndId>{e2e or 'NOTPROVIDED'}</EndToEndId></Refs>
          <AmtDtls><InstdAmt><Amt Ccy="{ccy}">{amount}</Amt></InstdAmt></AmtDtls>
          <RltdPties><Cdtr><Nm>Asha Rao</Nm></Cdtr><CdtrAcct><Id><Othr><Id>50100123456789</Id></Othr></Id></CdtrAcct></RltdPties>
          <RmtInf><Ustrd>Salary October</Ustrd><Ustrd>2026</Ustrd></RmtInf>
          {extra_tx}
        </TxDtls></NtryDtls>"""
    return f"""
      <Ntry>
        <NtryRef>E1</NtryRef>
        <Amt Ccy="{ccy}">{amount}</Amt>
        <CdtDbtInd>{ind}</CdtDbtInd>
        <Sts><Cd>{status}</Cd></Sts>
        <BookgDt><Dt>2026-10-30</Dt></BookgDt>
        <ValDt><Dt>2026-10-31</Dt></ValDt>
        <AcctSvcrRef>BANK-REF-1</AcctSvcrRef>
        {details}
      </Ntry>"""


def _camt(*entries: str, ns: str = NS) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="{ns}">
  <BkToCstmrStmt>
    <GrpHdr><MsgId>MSG1</MsgId></GrpHdr>
    <Stmt>
      <Id>STMT-2026-10-31</Id>
      <Acct><Id><IBAN>AE070331234567890123456</IBAN></Id><Ccy>INR</Ccy></Acct>
      {''.join(entries)}
    </Stmt>
  </BkToCstmrStmt>
</Document>""".encode("utf-8")


def test_camt053_booked_debit_entry() -> None:
    parsed = parse_camt053(_camt(_entry("46666.67", "INR", "DBIT")))
    assert parsed.format == "CAMT053" and parsed.statement_id == "STMT-2026-10-31"
    assert parsed.account == "AE070331234567890123456" and parsed.account_masked == "••••3456"
    (line,) = parsed.lines
    assert (line.value_date, line.booking_date, line.amount, line.currency, line.direction) == (
        date(2026, 10, 31), date(2026, 10, 30), D("46666.67"), "INR", DEBIT,
    )
    assert line.end_to_end_id == "PAY-2026-10-3" and line.reference == "PAY-2026-10-3"
    assert line.bank_reference == "BANK-REF-1"
    assert line.remittance == "Salary October 2026"
    assert line.counterparty == "Asha Rao" and line.counterparty_account == "50100123456789"
    assert line.instructed_amount == D("46666.67")
    assert "50100123456789" not in repr(line)


def test_camt053_minor_units_and_credit() -> None:
    parsed = parse_camt053(_camt(_entry("1500", "JPY", "CRDT", tx=False)))
    (line,) = parsed.lines
    assert (line.amount, line.currency, line.direction, line.reference) == (D("1500"), "JPY", CREDIT, "E1")
    bad = parse_camt053(_camt(_entry("1500.5", "JPY", "DBIT", tx=False)))
    assert bad.rejected[0].code == "AMOUNT_PRECISION"


def test_camt053_pending_entries_skipped_and_bad_entries_rejected() -> None:
    parsed = parse_camt053(_camt(_entry("1.00", "INR", "DBIT", status="PDNG"), _entry("2.00", "INR", "XXXX"), _entry("3.00", "INR", "DBIT")))
    assert parsed.skipped_entries == 1
    assert [(r.source_row, r.code) for r in parsed.rejected] == [(2, "DIRECTION_INVALID")]
    assert [line.amount for line in parsed.lines] == [D("3.00")]


def test_camt053_return_information() -> None:
    extra = "<RtrInf><Rsn><Cd>AC04</Cd></Rsn></RtrInf>"
    (line,) = parse_camt053(_camt(_entry("46666.67", "INR", "CRDT", extra_tx=extra))).lines
    assert line.return_reason == "AC04" and line.return_indicated
    assert return_reason_message("AC04") == "The account is closed."
    assert "ZZ99" in return_reason_message("ZZ99")


def test_camt053_batch_booking_itemised() -> None:
    entry = """
      <Ntry><Amt Ccy="INR">300.00</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts>
        <BookgDt><Dt>2026-10-30</Dt></BookgDt>
        <NtryDtls>
          <TxDtls><Refs><EndToEndId>PAY-1</EndToEndId></Refs><Amt Ccy="INR">100.00</Amt></TxDtls>
          <TxDtls><Refs><EndToEndId>PAY-2</EndToEndId></Refs><Amt Ccy="INR">200.00</Amt></TxDtls>
        </NtryDtls>
      </Ntry>"""
    parsed = parse_camt053(_camt(entry))
    assert [(line.end_to_end_id, line.amount, line.value_date) for line in parsed.lines] == [
        ("PAY-1", D("100.00"), date(2026, 10, 30)), ("PAY-2", D("200.00"), date(2026, 10, 30)),
    ]
    assert parsed.notices == ()


def test_camt053_reimport_same_hashes() -> None:
    data = _camt(_entry("1.00", "INR", "DBIT"), _entry("1.00", "INR", "DBIT"))
    a, b = parse_camt053(data), parse_camt053(data)
    assert [l.line_hash for l in a.lines] == [l.line_hash for l in b.lines]
    assert len({l.line_hash for l in a.lines}) == 2


XXE = b"""<?xml version="1.0"?>
<!DOCTYPE Document [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08"><BkToCstmrStmt>&xxe;</BkToCstmrStmt></Document>"""

LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08">&lol2;</Document>"""


@pytest.mark.parametrize("payload", [XXE, LAUGHS, b"<!DOCTYPE Document><Document/>", b'<?xml version="1.0"?>\n<!ENTITY x "y"><Document/>'])
def test_camt053_rejects_doctype_and_entities(payload: bytes) -> None:
    with pytest.raises(StatementError) as exc:
        parse_camt053(payload)
    assert exc.value.code == "XML_DTD_FORBIDDEN"


def test_camt053_comments_and_cdata_allowed() -> None:
    data = _camt("<!-- booked entries --><![CDATA[ ]]>" + _entry("1.00", "INR", "DBIT"))
    assert len(parse_camt053(data).lines) == 1


@pytest.mark.parametrize(
    "payload,code",
    [
        (b"<Document xmlns='urn:iso:std:iso:20022:tech:xsd:camt.052.001.08'/>", "NOT_CAMT053"),
        (b"<Document", "XML_MALFORMED"),
        ('<?xml version="1.0" encoding="UTF-16"?><Document/>'.encode("utf-8"), "XML_ENCODING"),
        ("<Document/>".encode("utf-16"), "XML_ENCODING"),
    ],
)
def test_camt053_other_file_errors(payload: bytes, code: str) -> None:
    with pytest.raises(StatementError) as exc:
        parse_camt053(payload)
    assert exc.value.code == code

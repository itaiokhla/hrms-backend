"""Bank statement import: CSV with a per-bank column mapping, and ISO 20022 CAMT.053 (3.7 §9).

Both parsers return normalised ``StatementLine`` objects: value and booking dates, a positive exact
``Decimal`` amount with the currency's minor-unit digits, a direction (DEBIT = money out of the
funding account), references, counterparty, remittance text and, for CAMT, the instructed amount
and return information. CSV amounts may use Western (1,234,567.89) or Indian (12,34,567.89) digit
grouping. Rows that cannot be read are returned as ``RejectedRow`` data with a code; only a file
that cannot be read safely at all raises ``StatementError``.

Re-imports are idempotent (3.7 §8.5): the file's SHA-256 identifies the file, and each line has a
``line_hash`` over its canonical content plus an occurrence counter, so two genuinely identical
debits in one file stay two lines (a duplicate payment must not disappear) while re-reading the
same file reproduces the same hashes. The counterparty account number is never part of the hash.

CAMT.053 is parsed with ``xml.etree.ElementTree`` only after the raw text is checked: the file must
be UTF-8 and may not contain DOCTYPE, ENTITY or any other markup declaration, which rules out
external entities (XXE) and entity expansion (billion laughs) before the parser sees a byte.
Booked entries are read (pending and information-only entries are counted and skipped); a batch
booking with itemised transaction details becomes one line per transaction.
"""

import codecs
import csv
import hashlib
import io
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from app.payroll.banking._currency import amount_text, fits, known_minor_unit
from app.payroll.banking.errors import StatementError
from app.payroll.banking.masking import mask_account

DEBIT = "DEBIT"
CREDIT = "CREDIT"
FORMATS: tuple[str, ...] = ("CSV", "CAMT053")
DEFAULT_MAX_BYTES = 25 * 1024 * 1024
MAX_TEXT = 500
MAX_REMITTANCE = 2000
RETURN_SUBFAMILIES: frozenset[str] = frozenset({"RRTN", "RPCR"})

RETURN_REASONS: Mapping[str, str] = {
    "AC01": "The account number is incorrect.",
    "AC03": "The beneficiary account number is invalid.",
    "AC04": "The account is closed.",
    "AC06": "The account is blocked.",
    "AG01": "The bank does not allow this transaction on the account.",
    "AG02": "The bank rejected the operation code.",
    "AM04": "Insufficient funds.",
    "AM05": "The bank reported a duplicate payment.",
    "BE04": "The beneficiary's address is missing or incorrect.",
    "BE05": "The bank did not recognise the sender.",
    "CUST": "Returned at the account holder's request.",
    "FOCR": "Returned after a cancellation request.",
    "MD07": "The account holder is deceased.",
    "MS02": "Returned by the account holder (no reason given).",
    "MS03": "Returned by the bank (no reason given).",
    "NARR": "Returned by the bank; see the narrative.",
    "RC01": "The bank identifier (BIC) is incorrect.",
    "RR01": "Regulatory reason: the sender's account or identification is missing.",
    "RR02": "Regulatory reason: the sender's name or address is missing.",
    "RR03": "Regulatory reason: the beneficiary's name or address is missing.",
    "RR04": "Regulatory reason.",
}

_CAMT053_NAMESPACE = re.compile(r"urn:iso:std:iso:20022:tech:xsd:camt\.053\.001\.[0-9]{2}")
_XML_DECLARED_ENCODING = re.compile(r"""\A\s*<\?xml[^>]*?\bencoding\s*=\s*["']([^"']+)["']""")
_MARKUP_DECLARATION = re.compile(r"<!(?!--|\[CDATA\[)")
_XML_AMOUNT = re.compile(r"[0-9]{1,18}(?:\.[0-9]{1,5})?")
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_CURRENCY = re.compile(r"[A-Z]{3}")
_PLAIN_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_SPACES = (" ", " ", " ")


@dataclass(frozen=True)
class StatementLine:
    value_date: date
    amount: Decimal  # positive, exactly the currency's minor-unit digits
    currency: str
    direction: str  # DEBIT (money out of the funding account) or CREDIT
    reference: str | None
    counterparty: str | None
    line_hash: str
    booking_date: date | None = None
    end_to_end_id: str | None = None
    bank_reference: str | None = None
    remittance: str | None = None
    instructed_amount: Decimal | None = None
    instructed_currency: str | None = None
    return_reason: str | None = None
    reversal: bool = False
    transaction_code: str | None = None
    source_row: int = 0  # 1-based CSV row or CAMT entry number
    counterparty_account: str | None = field(default=None, repr=False)

    @property
    def return_indicated(self) -> bool:
        """The bank marked this line as a return or reversal (reason code, reversal flag or RRTN/RPCR)."""
        family = (self.transaction_code or "").rsplit("/", 1)[-1]
        return bool(self.return_reason) or self.reversal or family in RETURN_SUBFAMILIES


@dataclass(frozen=True)
class RejectedRow:
    source_row: int
    code: str
    message: str


@dataclass(frozen=True)
class ParsedStatement:
    format: str
    file_sha256: str
    lines: tuple[StatementLine, ...]
    rejected: tuple[RejectedRow, ...]
    notices: tuple[RejectedRow, ...]  # warnings about rows that were still imported
    statement_id: str | None = None
    account: str | None = field(default=None, repr=False)
    account_currency: str | None = None
    skipped_entries: int = 0  # pending or information-only CAMT entries

    @property
    def account_masked(self) -> str:
        return mask_account(self.account)


class _RowError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def return_reason_message(code: str | None) -> str:
    """Plain message for an ISO 20022 return reason code (3.7 §10)."""
    if not code:
        return "The bank returned the payment."
    return RETURN_REASONS.get(code.strip().upper(), f"The bank returned the payment (reason {code.strip().upper()[:8]}).")


def _clean(value: str | None, limit: int = MAX_TEXT) -> str | None:
    if value is None:
        return None
    text = " ".join("".join(" " if unicodedata.category(char) == "Cc" else char for char in value).split())
    if not text:
        return None
    if len(text) > limit:
        raise _RowError("FIELD_TOO_LONG", f"a text field is longer than {limit} characters")
    return text


def _money(amount: Decimal, currency: str) -> tuple[Decimal, int]:
    if _CURRENCY.fullmatch(currency) is None:
        raise _RowError("CURRENCY_INVALID", "the currency is not a three-letter code")
    digits = known_minor_unit(currency)
    if digits is None:
        raise _RowError("UNKNOWN_CURRENCY", "the currency is not a known ISO 4217 currency")
    if not fits(amount, digits):
        raise _RowError("AMOUNT_PRECISION", f"the amount has more decimals than {currency} allows")
    return Decimal(amount_text(amount, digits)), digits


def _finish(kind: str, drafts: Iterable[dict[str, Any]]) -> tuple[StatementLine, ...]:
    occurrences: dict[str, int] = {}
    lines: list[StatementLine] = []
    for draft in drafts:
        canonical = "\x1f".join(
            (
                "v1",
                kind,
                draft["value_date"].isoformat(),
                draft["booking_date"].isoformat() if draft.get("booking_date") else "",
                draft["direction"],
                str(draft["amount"]),
                draft["currency"],
                draft.get("reference") or "",
                draft.get("end_to_end_id") or "",
                draft.get("bank_reference") or "",
                draft.get("remittance") or "",
                draft.get("counterparty") or "",
                str(draft["instructed_amount"]) if draft.get("instructed_amount") is not None else "",
                draft.get("instructed_currency") or "",
                draft.get("return_reason") or "",
                "1" if draft.get("reversal") else "0",
                draft.get("transaction_code") or "",
            )
        )
        seen = occurrences.get(canonical, 0)
        occurrences[canonical] = seen + 1
        digest = hashlib.sha256(f"{canonical}\x1f{seen}".encode("utf-8")).hexdigest()
        lines.append(StatementLine(line_hash=digest, **draft))
    return tuple(lines)


# --------------------------------------------------------------------------- CSV


@dataclass(frozen=True)
class CsvColumnMapping:
    """Header names of one bank's CSV export (matched case-insensitively) and how to read them.

    Amounts come either from ``amount`` (signed, or with a ``direction`` column whose values are in
    ``debit_values``/``credit_values``) or from separate ``debit`` and ``credit`` columns.
    """

    date: str
    amount: str | None = None
    debit: str | None = None
    credit: str | None = None
    direction: str | None = None
    currency: str | None = None
    default_currency: str | None = None
    reference: str | None = None
    counterparty: str | None = None
    counterparty_account: str | None = None
    remittance: str | None = None
    booking_date: str | None = None
    date_format: str = "%Y-%m-%d"
    decimal_separator: str = "."
    thousands_separator: str | None = ","
    delimiter: str = ","
    encoding: str = "utf-8-sig"
    debit_values: frozenset[str] = frozenset({"D", "DR", "DEBIT", "DBIT"})
    credit_values: frozenset[str] = frozenset({"C", "CR", "CREDIT", "CRDT"})
    negative_is_debit: bool = True
    skip_rows: int = 0

    def validate(self) -> None:
        def fail(message: str) -> StatementError:
            return StatementError("MAPPING_INVALID", message)

        if bool(self.amount) == bool(self.debit or self.credit):
            raise fail("map either an amount column or separate debit and credit columns")
        if (self.debit is None) != (self.credit is None):
            raise fail("debit and credit columns go together")
        if self.direction and not self.amount:
            raise fail("a direction column needs an amount column")
        if not self.currency and not self.default_currency:
            raise fail("map a currency column or give a default currency")
        if self.default_currency and _CURRENCY.fullmatch(self.default_currency) is None:
            raise fail("the default currency must be a three-letter code")
        if self.decimal_separator not in (".", ","):
            raise fail("the decimal separator must be '.' or ','")
        if self.thousands_separator not in (None, ",", ".", " ", "'") or self.thousands_separator == self.decimal_separator:
            raise fail("the thousands separator must be ',', '.', ' ', \"'\" or none, and differ from the decimal separator")
        if len(self.delimiter) != 1 or self.delimiter in ('"', "\r", "\n"):
            raise fail("the delimiter must be one character")
        if not self.date_format or isinstance(self.skip_rows, bool) or self.skip_rows < 0:
            raise fail("give a date format and a non-negative number of rows to skip")
        try:
            codecs.lookup(self.encoding)
        except LookupError:
            raise fail("unknown text encoding") from None
        names = [n for n in (self.date, self.amount, self.debit, self.credit, self.direction, self.currency, self.reference,
                             self.counterparty, self.counterparty_account, self.remittance, self.booking_date) if n]
        if len({n.strip().casefold() for n in names}) != len(names):
            raise fail("each column can be mapped once")


def _parse_number(text: str, mapping: CsvColumnMapping) -> Decimal:
    value = text.strip()
    negative = False
    if value.startswith("(") and value.endswith(")"):
        negative, value = True, value[1:-1].strip()
    if value[:1] in ("+", "-"):
        if negative:
            raise _RowError("AMOUNT_INVALID", "the amount is not a valid number")
        negative, value = value[0] == "-", value[1:].strip()
    elif value.endswith("-"):
        negative, value = True, value[:-1].strip()
    thousands = mapping.thousands_separator
    if thousands == " ":
        for space in _SPACES:
            value = value.replace(space, " ")
    decimal_sep = re.escape(mapping.decimal_separator)
    if thousands and thousands in value:
        sep = re.escape(thousands)
        western = rf"[0-9]{{1,3}}(?:{sep}[0-9]{{3}})+"
        indian = rf"[0-9]{{1,2}}(?:{sep}[0-9]{{2}})+{sep}[0-9]{{3}}"
        if re.fullmatch(rf"(?:{western}|{indian})(?:{decimal_sep}[0-9]+)?", value) is None:
            raise _RowError("AMOUNT_INVALID", "the amount has misplaced thousands separators")
        value = value.replace(thousands, "")
    if mapping.decimal_separator == ",":
        value = value.replace(",", ".")
    if _PLAIN_NUMBER.fullmatch(value) is None:
        raise _RowError("AMOUNT_INVALID", "the amount is not a valid number")
    number = Decimal(value)
    return -number if negative else number


def _parse_date(text: str, fmt: str) -> date:
    try:
        return datetime.strptime(text.strip(), fmt).date()
    except ValueError:
        raise _RowError("DATE_INVALID", "the date does not match the mapped date format") from None


def _csv_amount(cells: Mapping[str, str], mapping: CsvColumnMapping) -> tuple[Decimal, str]:
    if mapping.amount:
        raw = cells.get("amount", "")
        if not raw.strip():
            raise _RowError("AMOUNT_MISSING", "the amount is empty")
        number = _parse_number(raw, mapping)
        if mapping.direction:
            marker = cells.get("direction", "").strip().upper()
            if number < 0:
                raise _RowError("AMOUNT_INVALID", "a signed amount cannot also have a direction column")
            if marker in {v.upper() for v in mapping.debit_values}:
                return number, DEBIT
            if marker in {v.upper() for v in mapping.credit_values}:
                return number, CREDIT
            raise _RowError("DIRECTION_INVALID", "the debit/credit indicator is not recognised")
        if number == 0:
            raise _RowError("ZERO_AMOUNT", "the amount is zero")
        debit = (number < 0) == mapping.negative_is_debit
        return abs(number), DEBIT if debit else CREDIT
    debit_raw, credit_raw = cells.get("debit", "").strip(), cells.get("credit", "").strip()
    debit = abs(_parse_number(debit_raw, mapping)) if debit_raw else Decimal(0)
    credit = abs(_parse_number(credit_raw, mapping)) if credit_raw else Decimal(0)
    if debit and credit:
        raise _RowError("DIRECTION_AMBIGUOUS", "both the debit and the credit column have an amount")
    if not debit and not credit:
        raise _RowError("ZERO_AMOUNT", "neither the debit nor the credit column has an amount")
    return (debit, DEBIT) if debit else (credit, CREDIT)


def _csv_row(cells: Mapping[str, str], mapping: CsvColumnMapping) -> dict[str, Any]:
    amount, direction = _csv_amount(cells, mapping)
    if amount == 0:
        raise _RowError("ZERO_AMOUNT", "the amount is zero")
    currency = (cells.get("currency", "").strip() or mapping.default_currency or "").upper()
    amount, _ = _money(amount, currency)
    booking = cells.get("booking_date", "").strip()
    account = "".join(cells.get("counterparty_account", "").split()).upper() or None
    return {
        "value_date": _parse_date(cells.get("date", ""), mapping.date_format),
        "booking_date": _parse_date(booking, mapping.date_format) if booking else None,
        "amount": amount,
        "currency": currency,
        "direction": direction,
        "reference": _clean(cells.get("reference")),
        "counterparty": _clean(cells.get("counterparty")),
        "remittance": _clean(cells.get("remittance"), MAX_REMITTANCE),
        "counterparty_account": _clean(account, 64),
    }


def parse_csv_statement(data: bytes, mapping: CsvColumnMapping, *, max_bytes: int = DEFAULT_MAX_BYTES) -> ParsedStatement:
    """Read a bank's CSV export with its column mapping; unreadable rows are returned as rejections."""
    if not isinstance(data, bytes):
        raise StatementError("INVALID_ARGUMENT", "statement data must be bytes")
    if len(data) > max_bytes:
        raise StatementError("FILE_TOO_LARGE", f"statement files are limited to {max_bytes} bytes")
    mapping.validate()
    try:
        text = data.decode(mapping.encoding)
    except UnicodeDecodeError:
        raise StatementError("DECODE_ERROR", f"the file is not valid {mapping.encoding} text") from None
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=mapping.delimiter, strict=True))
    except csv.Error as exc:
        raise StatementError("CSV_MALFORMED", f"the CSV file is malformed ({exc.__class__.__name__})") from None
    rows = rows[mapping.skip_rows :]
    header_index = next((i for i, row in enumerate(rows) if any(cell.strip() for cell in row)), None)
    if header_index is None:
        raise StatementError("EMPTY_FILE", "the statement has no header row")
    header = [cell.strip().casefold() for cell in rows[header_index]]
    columns: dict[str, int] = {}
    for key in ("date", "amount", "debit", "credit", "direction", "currency", "reference", "counterparty",
                "counterparty_account", "remittance", "booking_date"):
        name = getattr(mapping, key)
        if not name:
            continue
        matches = [i for i, cell in enumerate(header) if cell == name.strip().casefold()]
        if not matches:
            raise StatementError("COLUMN_MISSING", f"the mapped column {name!r} is not in the header")
        if len(matches) > 1:
            raise StatementError("COLUMN_AMBIGUOUS", f"the header has more than one {name!r} column")
        columns[key] = matches[0]

    drafts: list[dict[str, Any]] = []
    rejected: list[RejectedRow] = []
    for offset, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if not any(cell.strip() for cell in row):
            continue
        number = offset + mapping.skip_rows
        cells = {key: (row[i] if i < len(row) else "") for key, i in columns.items()}
        try:
            draft = _csv_row(cells, mapping)
        except _RowError as exc:
            rejected.append(RejectedRow(number, exc.code, exc.message))
            continue
        draft["source_row"] = number
        drafts.append(draft)
    return ParsedStatement("CSV", hashlib.sha256(data).hexdigest(), _finish("CSV", drafts), tuple(rejected), ())


# --------------------------------------------------------------------------- CAMT.053


def _safe_root(data: bytes, max_bytes: int) -> ET.Element:
    if not isinstance(data, bytes):
        raise StatementError("INVALID_ARGUMENT", "statement data must be bytes")
    if len(data) > max_bytes:
        raise StatementError("FILE_TOO_LARGE", f"statement files are limited to {max_bytes} bytes")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise StatementError("XML_ENCODING", "CAMT.053 files must be UTF-8") from None
    declared = _XML_DECLARED_ENCODING.match(text)
    if declared and declared.group(1).strip().lower() not in ("utf-8", "utf8"):
        raise StatementError("XML_ENCODING", "CAMT.053 files must be UTF-8")
    if _MARKUP_DECLARATION.search(text):
        raise StatementError("XML_DTD_FORBIDDEN", "DOCTYPE, ENTITY and other markup declarations are not allowed")
    parser = ET.XMLParser()
    try:
        parser.feed(data)
        return parser.close()
    except ET.ParseError as exc:
        raise StatementError("XML_MALFORMED", f"the XML is not well-formed (line {exc.position[0]})") from None


class _Camt:
    def __init__(self, namespace: str) -> None:
        self._prefix = f"{{{namespace}}}"

    def _path(self, path: str) -> str:
        return "/".join(self._prefix + part for part in path.split("/"))

    def find(self, element: ET.Element | None, path: str) -> ET.Element | None:
        return None if element is None else element.find(self._path(path))

    def findall(self, element: ET.Element, path: str) -> list[ET.Element]:
        return element.findall(self._path(path))

    def text(self, element: ET.Element | None, path: str) -> str | None:
        node = self.find(element, path)
        if node is None or node.text is None:
            return None
        return node.text.strip() or None

    def first(self, element: ET.Element | None, *paths: str) -> str | None:
        for path in paths:
            value = self.text(element, path)
            if value is not None:
                return value
        return None

    def amount(self, element: ET.Element | None, path: str) -> tuple[Decimal, str] | None:
        node = self.find(element, path)
        if node is None:
            return None
        text, currency = (node.text or "").strip(), (node.get("Ccy") or "").strip()
        if _XML_AMOUNT.fullmatch(text) is None:
            raise _RowError("AMOUNT_INVALID", "an amount is not a valid ISO 20022 amount")
        try:
            value = Decimal(text)
        except InvalidOperation:
            raise _RowError("AMOUNT_INVALID", "an amount is not a valid ISO 20022 amount") from None
        money, _ = _money(value, currency)
        return money, currency

    def date(self, element: ET.Element | None, path: str) -> date | None:
        node = self.find(element, path)
        if node is None:
            return None
        text = self.first(node, "Dt", "DtTm")
        if text is None or _ISO_DATE.fullmatch(text[:10]) is None:
            raise _RowError("DATE_INVALID", "a booking or value date is not an ISO date")
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            raise _RowError("DATE_INVALID", "a booking or value date is not a valid date") from None


def _direction(code: str | None) -> str:
    if code == "DBIT":
        return DEBIT
    if code == "CRDT":
        return CREDIT
    raise _RowError("DIRECTION_INVALID", "CdtDbtInd must be CRDT or DBIT")


def _status(x: _Camt, entry: ET.Element) -> str | None:
    node = x.find(entry, "Sts")
    if node is None:
        return None
    return x.first(node, "Cd", "Prtry") or (node.text or "").strip() or None


def _transaction_code(x: _Camt, entry: ET.Element) -> str | None:
    domain = x.text(entry, "BkTxCd/Domn/Cd")
    family = x.text(entry, "BkTxCd/Domn/Fmly/Cd")
    sub_family = x.text(entry, "BkTxCd/Domn/Fmly/SubFmlyCd")
    if domain and family and sub_family:
        return f"{domain}/{family}/{sub_family}"
    return x.text(entry, "BkTxCd/Prtry/Cd")


def _join(*parts: str | None) -> str | None:
    text = " ".join(part for part in parts if part)
    return text or None


def _camt_entry(x: _Camt, entry: ET.Element, number: int) -> tuple[list[dict[str, Any]], list[RejectedRow]]:
    booked = x.amount(entry, "Amt")
    if booked is None:
        raise _RowError("AMOUNT_INVALID", "the entry has no amount")
    entry_amount, entry_currency = booked
    if entry_amount == 0:
        raise _RowError("ZERO_AMOUNT", "the entry amount is zero")
    entry_direction = _direction(x.text(entry, "CdtDbtInd"))
    booking_date = x.date(entry, "BookgDt")
    value_date = x.date(entry, "ValDt") or booking_date
    if value_date is None:
        raise _RowError("DATE_INVALID", "the entry has neither a value date nor a booking date")
    base = {
        "booking_date": booking_date,
        "value_date": value_date,
        "reversal": (x.text(entry, "RvslInd") or "").lower() in ("true", "1"),
        "transaction_code": _clean(_transaction_code(x, entry), 64),
    }
    entry_ref, entry_bank_ref = x.text(entry, "NtryRef"), x.text(entry, "AcctSvcrRef")
    entry_info = x.text(entry, "AddtlNtryInf")
    transactions = x.findall(entry, "NtryDtls/TxDtls")
    notices: list[RejectedRow] = []

    def from_entry() -> dict[str, Any]:
        return {
            **base, "amount": entry_amount, "currency": entry_currency, "direction": entry_direction,
            "reference": _clean(entry_ref), "end_to_end_id": None, "bank_reference": _clean(entry_bank_ref),
            "remittance": _clean(entry_info, MAX_REMITTANCE), "counterparty": None, "counterparty_account": None,
            "instructed_amount": None, "instructed_currency": None, "return_reason": None, "source_row": number,
        }

    if not transactions:
        return [from_entry()], notices
    amounts = [x.amount(tx, "Amt") or x.amount(tx, "AmtDtls/TxAmt/Amt") for tx in transactions]
    if len(transactions) > 1 and any(item is None for item in amounts):
        notices.append(RejectedRow(number, "BATCH_NOT_ITEMISED", "a batch booking without amounts per transaction was imported as one line"))
        return [from_entry()], notices
    if len(transactions) > 1:
        itemised = [item for item in amounts if item is not None]
        if any(cur != entry_currency for _, cur in itemised) or sum(a for a, _ in itemised) != entry_amount:
            notices.append(RejectedRow(number, "BATCH_TOTAL_MISMATCH", "the transactions of a batch booking do not add up to the entry amount"))
    drafts: list[dict[str, Any]] = []
    for tx, tx_amount in zip(transactions, amounts):
        direction = _direction(x.text(tx, "CdtDbtInd")) if x.text(tx, "CdtDbtInd") else entry_direction
        if len(transactions) == 1 or tx_amount is None:
            amount, currency = entry_amount, entry_currency
            if tx_amount is not None and tx_amount != (entry_amount, entry_currency):
                notices.append(RejectedRow(number, "BATCH_TOTAL_MISMATCH", "the transaction amount differs from the entry amount"))
        else:
            amount, currency = tx_amount
        instructed = x.amount(tx, "AmtDtls/InstdAmt/Amt")
        end_to_end = x.text(tx, "Refs/EndToEndId")
        end_to_end = None if end_to_end is None or end_to_end.upper() == "NOTPROVIDED" else end_to_end
        own_side, other_side = ("Cdtr", "Dbtr") if direction == DEBIT else ("Dbtr", "Cdtr")
        name = x.first(tx, f"RltdPties/{own_side}/Nm", f"RltdPties/{own_side}/Pty/Nm", f"RltdPties/{other_side}/Nm", f"RltdPties/{other_side}/Pty/Nm")
        account = x.first(
            tx, f"RltdPties/{own_side}Acct/Id/IBAN", f"RltdPties/{own_side}Acct/Id/Othr/Id",
            f"RltdPties/{other_side}Acct/Id/IBAN", f"RltdPties/{other_side}Acct/Id/Othr/Id",
        )
        unstructured = " ".join(node.text.strip() for node in x.findall(tx, "RmtInf/Ustrd") if node.text and node.text.strip())
        creditor_ref = x.text(tx, "RmtInf/Strd/CdtrRefInf/Ref")
        drafts.append(
            {
                **base,
                "amount": amount,
                "currency": currency,
                "direction": direction,
                "reference": _clean(end_to_end or x.text(tx, "Refs/InstrId") or creditor_ref or entry_ref),
                "end_to_end_id": _clean(end_to_end),
                "bank_reference": _clean(x.text(tx, "Refs/AcctSvcrRef") or entry_bank_ref),
                "remittance": _clean(_join(unstructured, creditor_ref, x.text(tx, "AddtlTxInf"), entry_info), MAX_REMITTANCE),
                "counterparty": _clean(name),
                "counterparty_account": _clean("".join(account.split()).upper() if account else None, 64),
                "instructed_amount": instructed[0] if instructed else None,
                "instructed_currency": instructed[1] if instructed else None,
                "return_reason": _clean(x.text(tx, "RtrInf/Rsn/Cd"), 8),
                "source_row": number,
            }
        )
    return drafts, notices


def parse_camt053(data: bytes, *, max_bytes: int = DEFAULT_MAX_BYTES) -> ParsedStatement:
    """Read an ISO 20022 camt.053 bank-to-customer statement (any 001.xx version), rejecting DTDs."""
    root = _safe_root(data, max_bytes)
    namespace = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
    if root.tag.rsplit("}", 1)[-1] != "Document" or _CAMT053_NAMESPACE.fullmatch(namespace) is None:
        raise StatementError("NOT_CAMT053", "the file is not an ISO 20022 camt.053 statement")
    x = _Camt(namespace)
    statements = x.findall(root, "BkToCstmrStmt/Stmt")
    if not statements:
        raise StatementError("NOT_CAMT053", "the file contains no statement")
    accounts = {x.first(stmt, "Acct/Id/IBAN", "Acct/Id/Othr/Id") for stmt in statements}
    if len(accounts) > 1:
        raise StatementError("MULTIPLE_ACCOUNTS", "import one account per file")
    account = accounts.pop()
    drafts: list[dict[str, Any]] = []
    rejected: list[RejectedRow] = []
    notices: list[RejectedRow] = []
    skipped = 0
    number = 0
    for stmt in statements:
        for entry in x.findall(stmt, "Ntry"):
            number += 1
            status = _status(x, entry)
            if status is None:
                rejected.append(RejectedRow(number, "STATUS_MISSING", "the entry has no booking status"))
                continue
            if status != "BOOK":
                skipped += 1
                continue
            try:
                entry_drafts, entry_notices = _camt_entry(x, entry, number)
            except _RowError as exc:
                rejected.append(RejectedRow(number, exc.code, exc.message))
                continue
            drafts.extend(entry_drafts)
            notices.extend(entry_notices)
    return ParsedStatement(
        format="CAMT053",
        file_sha256=hashlib.sha256(data).hexdigest(),
        lines=_finish("CAMT053", drafts),
        rejected=tuple(rejected),
        notices=tuple(notices),
        statement_id=x.text(statements[0], "Id"),
        account="".join(account.split()).upper() if account else None,
        account_currency=x.text(statements[0], "Acct/Ccy"),
        skipped_entries=skipped,
    )

"""Payment files: deterministic, hashed, and only in formats that are actually offered (3.7 §7.1).

Only ``GENERIC_CSV`` is OFFERED, for manual upload to any bank portal. A bank-specific format is
offered only after its specification is implemented and a test file has been accepted by that bank
(contract §8.2: do not invent bank-file compatibility). ``AE_WPS_SIF`` exists as a draft writer with
status DRAFT_NOT_OFFERED; BACS Standard 18, NACHA, SEPA pain.001 and GIRO are NOT_IMPLEMENTED.
``require_offered_format`` refuses everything that is not OFFERED.

Generic CSV (UTF-8, CRLF, RFC 4180 quoting): a column header row, then

    D  one row per instruction, sorted by instruction id: sequence, instruction id and reference,
       payee name, masked account, full account number or IBAN, routing scheme and code, BIC,
       amount with exactly the currency's minor-unit digits, currency, optional value date
    T  one trailer per currency, sorted by code: record count and total

and every row carries the batch reference. The bytes depend only on the input, so regenerating a
batch yields an identical file and SHA-256; passing the stored hash as ``expected_sha256`` makes a
difference fail loudly. Amounts are always positive: a zero or negative amount is rejected, never
escaped. Text cells starting with =, +, -, @, tab or carriage return get a leading apostrophe
against spreadsheet formula injection (3.9 §9, 3.6 §8, EC-DOC-17).
"""

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

from app.payroll.banking._currency import AMOUNT_LIMIT, amount_text, currency_code, fits, payable_minor_unit
from app.payroll.banking.errors import BankFileError
from app.payroll.banking.masking import ASCII_MASK, mask_account, mask_iban, mask_name
from app.payroll.banking.validators import (
    has_unsafe_characters,
    iban_problems,
    is_valid_aba,
    is_valid_bic,
    normalize_code,
    normalize_iban,
)

OFFERED = "OFFERED"
DRAFT_NOT_OFFERED = "DRAFT_NOT_OFFERED"
NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
FORMULA_TRIGGERS: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")
MAX_PAYEE_NAME = 140

GENERIC_CSV_COLUMNS: tuple[str, ...] = (
    "record_type",
    "batch_reference",
    "sequence",
    "instruction_id",
    "instruction_reference",
    "payee_name",
    "account_masked",
    "account_number",
    "iban",
    "routing_scheme",
    "routing_code",
    "bic",
    "amount",
    "currency",
    "value_date",
    "record_count",
)

_BATCH_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9/.-]{0,34}")
_ACCOUNT = re.compile(r"[A-Z0-9]{1,34}")
_DIGITS = {n: re.compile(rf"[0-9]{{{n}}}") for n in (6, 9, 13, 14)}
_ROUTING: Mapping[str, Callable[[str], bool]] = MappingProxyType(
    {
        "IFSC": lambda code: re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", code) is not None,
        "SORT_CODE": lambda code: _DIGITS[6].fullmatch(code) is not None,
        "ABA": is_valid_aba,
        "BSB": lambda code: _DIGITS[6].fullmatch(code) is not None,
        "BANK_CODE": lambda code: re.fullmatch(r"[A-Z0-9]{1,15}", code) is not None,
    }
)
ROUTING_SCHEMES: tuple[str, ...] = tuple(_ROUTING)


@dataclass(frozen=True)
class BankFileFormat:
    code: str
    name: str
    status: str  # OFFERED | DRAFT_NOT_OFFERED | NOT_IMPLEMENTED
    media_type: str
    extension: str
    note: str


FORMATS: Mapping[str, BankFileFormat] = MappingProxyType(
    {
        "GENERIC_CSV": BankFileFormat(
            "GENERIC_CSV", "Generic CSV (manual upload)", OFFERED, "text/csv", "csv",
            "Amount, masked account, IBAN or account number and routing; for manual upload to any bank portal.",
        ),
        "AE_WPS_SIF": BankFileFormat(
            "AE_WPS_SIF", "UAE WPS salary information file", DRAFT_NOT_OFFERED, "text/plain", "sif",
            "Draft from public guides; record order differs between banks. Offered only after the paying bank accepts a test file.",
        ),
        "UK_BACS_STD18": BankFileFormat(
            "UK_BACS_STD18", "UK BACS Standard 18", NOT_IMPLEMENTED, "text/plain", "txt",
            "Not implemented: needs the sponsor bank's specification and an accepted test file.",
        ),
        "US_NACHA": BankFileFormat(
            "US_NACHA", "US NACHA (ACH)", NOT_IMPLEMENTED, "text/plain", "ach",
            "Not implemented: needs the ODFI's specification and an accepted test file.",
        ),
        "SEPA_PAIN_001": BankFileFormat(
            "SEPA_PAIN_001", "SEPA credit transfer (pain.001)", NOT_IMPLEMENTED, "application/xml", "xml",
            "Not implemented: needs the bank's implementation guide and an accepted test file.",
        ),
        "SG_GIRO": BankFileFormat(
            "SG_GIRO", "Singapore GIRO", NOT_IMPLEMENTED, "text/plain", "txt",
            "Not implemented: needs the bank's specification and an accepted test file.",
        ),
    }
)


def offered_formats() -> tuple[BankFileFormat, ...]:
    return tuple(fmt for fmt in FORMATS.values() if fmt.status == OFFERED)


def require_offered_format(code: str) -> BankFileFormat:
    """The format if it is OFFERED; ``FORMAT_UNKNOWN`` or ``FORMAT_NOT_OFFERED`` otherwise."""
    fmt = FORMATS.get(code.strip().upper()) if isinstance(code, str) else None
    if fmt is None:
        raise BankFileError("FORMAT_UNKNOWN", "unknown bank file format; bank-file compatibility is never assumed")
    if fmt.status != OFFERED:
        raise BankFileError("FORMAT_NOT_OFFERED", f"{fmt.code} is {fmt.status.lower().replace('_', ' ')}: {fmt.note}")
    return fmt


def escape_csv_cell(value: str) -> str:
    """Prefix an apostrophe when a spreadsheet would read the cell as a formula (EC-DOC-17)."""
    return "'" + value if value.startswith(FORMULA_TRIGGERS) else value


@dataclass(frozen=True, repr=False)
class PaymentRecord:
    """One payment instruction as written to a file: the beneficiary snapshot, decrypted for writing."""

    instruction_id: int
    reference: str
    payee_name: str
    amount: Decimal
    currency: str
    account_number: str | None = None
    iban: str | None = None
    routing_scheme: str | None = None  # IFSC | SORT_CODE | ABA | BSB | BANK_CODE
    routing_code: str | None = None
    bic: str | None = None

    def __repr__(self) -> str:
        return (
            f"PaymentRecord(instruction_id={self.instruction_id!r}, reference={self.reference!r}, "
            f"payee_name={mask_name(self.payee_name)!r}, account={mask_account(self.account_number) or mask_iban(self.iban)!r}, "
            f"currency={self.currency!r})"
        )


@dataclass(frozen=True)
class BankFile:
    format: str
    status: str
    batch_reference: str
    filename: str
    media_type: str
    content: bytes = field(repr=False)
    sha256: str
    record_count: int
    totals: Mapping[str, Decimal]  # per currency, exact
    counts: Mapping[str, int]  # records per currency


@dataclass(frozen=True)
class _Row:
    instruction_id: int
    reference: str
    payee_name: str
    amount: str
    total_amount: Decimal
    currency: str
    digits: int
    account_number: str
    iban: str
    routing_scheme: str
    routing_code: str
    bic: str


def _fail(instruction_id: int, code: str, message: str) -> BankFileError:
    return BankFileError(code, f"instruction {instruction_id}: {message}")


def _text_ok(value: object, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value.strip()) <= limit and not has_unsafe_characters(value)


def _batch_reference(value: str) -> str:
    if not isinstance(value, str) or _BATCH_REFERENCE.fullmatch(value) is None:
        raise BankFileError("INVALID_BATCH_REFERENCE", "the batch reference is 1-64 letters, digits, '.', '_' or '-'")
    return value


def _positive_amount(record_id: int, value: object, currency: object) -> tuple[Decimal, str, int]:
    if isinstance(value, bool) or not isinstance(value, Decimal) or not value.is_finite():
        raise _fail(record_id, "INVALID_AMOUNT", "the amount must be a finite Decimal")
    code = currency_code(currency, BankFileError)
    digits = payable_minor_unit(code, BankFileError)
    if value <= 0:
        raise _fail(record_id, "NON_POSITIVE_AMOUNT", "payment amounts must be positive")
    if value >= AMOUNT_LIMIT:
        raise _fail(record_id, "AMOUNT_OUT_OF_RANGE", "the amount is outside the supported range")
    if not fits(value, digits):
        raise _fail(record_id, "AMOUNT_PRECISION", f"{code} amounts have {digits} decimals; a sub-unit cannot be paid")
    return value, code, digits


def _check_record(record: PaymentRecord) -> _Row:
    if not isinstance(record, PaymentRecord):
        raise BankFileError("INVALID_ARGUMENT", "records must be PaymentRecord objects")
    rid = record.instruction_id
    if isinstance(rid, bool) or not isinstance(rid, int) or rid <= 0:
        raise BankFileError("INVALID_INSTRUCTION_ID", "instruction ids must be positive integers")
    if not isinstance(record.reference, str) or _REFERENCE.fullmatch(record.reference) is None:
        raise _fail(rid, "INVALID_REFERENCE", "the instruction reference is 1-35 letters, digits, '/', '.' or '-'")
    if not _text_ok(record.payee_name, MAX_PAYEE_NAME):
        raise _fail(rid, "INVALID_PAYEE_NAME", f"the payee name is 1-{MAX_PAYEE_NAME} printable characters")
    amount, code, digits = _positive_amount(rid, record.amount, record.currency)

    iban = normalize_iban(record.iban) if isinstance(record.iban, str) else ""
    account = normalize_code(record.account_number) if isinstance(record.account_number, str) else ""
    if not iban and not account:
        raise _fail(rid, "MISSING_ACCOUNT", "an IBAN or an account number is required")
    if iban and iban_problems(iban):
        raise _fail(rid, "INVALID_IBAN", "the IBAN is not valid")
    if account and _ACCOUNT.fullmatch(account) is None:
        raise _fail(rid, "INVALID_ACCOUNT_NUMBER", "the account number must be 1-34 letters or digits")
    scheme = record.routing_scheme.strip().upper() if isinstance(record.routing_scheme, str) else ""
    routing = normalize_code(record.routing_code) if isinstance(record.routing_code, str) else ""
    if bool(scheme) != bool(routing):
        raise _fail(rid, "INVALID_ROUTING", "the routing scheme and code go together")
    if scheme and (scheme not in _ROUTING or not _ROUTING[scheme](routing)):
        raise _fail(rid, "INVALID_ROUTING", f"the routing code is not a valid {scheme or 'routing'} code")
    bic = normalize_code(record.bic) if isinstance(record.bic, str) else ""
    if bic and not is_valid_bic(bic):
        raise _fail(rid, "INVALID_BIC", "the BIC is not valid")
    if account and not iban and not scheme and not bic:
        raise _fail(rid, "MISSING_ROUTING", "an account number needs a routing code or a BIC")
    return _Row(
        rid, record.reference, " ".join(record.payee_name.split()), amount_text(amount, digits), amount, code, digits,
        account, iban, scheme, routing, bic,
    )


def _checked_rows(records: Sequence[PaymentRecord]) -> list[_Row]:
    if not records:
        raise BankFileError("EMPTY_BATCH", "a payment file needs at least one instruction")
    rows = sorted((_check_record(record) for record in records), key=lambda row: row.instruction_id)
    seen_ids: set[int] = set()
    seen_refs: set[str] = set()
    for row in rows:
        if row.instruction_id in seen_ids:
            raise _fail(row.instruction_id, "DUPLICATE_INSTRUCTION", "the instruction appears twice in the batch")
        if row.reference.upper() in seen_refs:
            raise _fail(row.instruction_id, "DUPLICATE_REFERENCE", "two instructions share a payment reference")
        seen_ids.add(row.instruction_id)
        seen_refs.add(row.reference.upper())
    return rows


def _bank_file(
    fmt: BankFileFormat,
    batch_reference: str,
    filename: str,
    content: bytes,
    totals: Mapping[str, Decimal],
    counts: Mapping[str, int],
) -> BankFile:
    return BankFile(
        format=fmt.code,
        status=fmt.status,
        batch_reference=batch_reference,
        filename=filename,
        media_type=fmt.media_type,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        record_count=sum(counts.values()),
        totals=MappingProxyType({code: totals[code] for code in sorted(totals)}),
        counts=MappingProxyType({code: counts[code] for code in sorted(counts)}),
    )


def _generic_csv(batch_reference: str, records: Sequence[PaymentRecord], value_date: date | None) -> BankFile:
    reference = _batch_reference(batch_reference)
    if value_date is not None and (not isinstance(value_date, date) or isinstance(value_date, datetime)):
        raise BankFileError("INVALID_ARGUMENT", "value_date must be a date")
    rows = _checked_rows(records)
    when = value_date.isoformat() if value_date else ""
    totals: dict[str, Decimal] = {}
    counts: dict[str, int] = {}
    digits: dict[str, int] = {}
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(GENERIC_CSV_COLUMNS)
    for sequence, row in enumerate(rows, start=1):
        masked = mask_iban(row.iban, mask=ASCII_MASK) if row.iban else mask_account(row.account_number, mask=ASCII_MASK)
        cells = [
            "D", reference, str(sequence), str(row.instruction_id), row.reference, row.payee_name, masked,
            row.account_number, row.iban, row.routing_scheme, row.routing_code, row.bic,
        ]
        writer.writerow([escape_csv_cell(cell) for cell in cells] + [row.amount, row.currency, when, ""])
        totals[row.currency] = totals.get(row.currency, Decimal(0)) + row.total_amount
        counts[row.currency] = counts.get(row.currency, 0) + 1
        digits[row.currency] = row.digits
    for code in sorted(totals):
        trailer = [amount_text(totals[code], digits[code]), code, "", str(counts[code])]
        writer.writerow(["T", reference] + [""] * 10 + trailer)
    content = buffer.getvalue().encode("utf-8")
    return _bank_file(FORMATS["GENERIC_CSV"], reference, f"{reference}.csv", content, totals, counts)


_WRITERS: Mapping[str, Callable[[str, Sequence[PaymentRecord], date | None], BankFile]] = MappingProxyType(
    {"GENERIC_CSV": _generic_csv}
)


def verify_regeneration(file: BankFile, expected_sha256: str) -> None:
    """Fail loudly when a regenerated file differs from the stored one (3.7 §7.1)."""
    if file.sha256 != expected_sha256.strip().lower():
        raise BankFileError("REGENERATION_MISMATCH", "the regenerated payment file differs from the stored file")


def generate_bank_file(
    format_code: str,
    batch_reference: str,
    records: Sequence[PaymentRecord],
    *,
    value_date: date | None = None,
    expected_sha256: str | None = None,
) -> BankFile:
    """Write a batch in an OFFERED format; with ``expected_sha256`` the bytes must match a stored file."""
    fmt = require_offered_format(format_code)
    file = _WRITERS[fmt.code](batch_reference, records, value_date)
    if expected_sha256 is not None:
        verify_regeneration(file, expected_sha256)
    return file


def generic_csv(batch_reference: str, records: Sequence[PaymentRecord], *, value_date: date | None = None) -> BankFile:
    return generate_bank_file("GENERIC_CSV", batch_reference, records, value_date=value_date)


# --------------------------------------------------------------------------- UAE WPS SIF (draft, not offered)


@dataclass(frozen=True)
class WpsEmployer:
    employer_id: str  # 13-digit MOHRE establishment id
    routing_code: str  # 9-digit agent routing code of the employer's bank
    salary_month: date  # any day of the month paid
    created_at: datetime  # file creation time, supplied by the caller (never read from the clock)
    reference: str = ""


@dataclass(frozen=True, repr=False)
class WpsSalaryRecord:
    instruction_id: int
    person_id: str  # 14-digit MOHRE person id
    routing_code: str  # 9-digit agent routing code of the employee's bank
    iban: str
    period_start: date
    period_end: date
    days: int
    fixed_pay: Decimal
    variable_pay: Decimal
    leave_days: int

    def __repr__(self) -> str:
        return f"WpsSalaryRecord(instruction_id={self.instruction_id!r}, iban={mask_iban(self.iban)!r})"


def _wps_amount(rid: int, value: object, digits: int) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal) or not value.is_finite():
        raise _fail(rid, "INVALID_AMOUNT", "WPS amounts must be finite Decimals")
    if value < 0 or value >= AMOUNT_LIMIT or not fits(value, digits):
        raise _fail(rid, "INVALID_AMOUNT", f"WPS amounts are non-negative AED with {digits} decimals")
    return value


def draft_wps_sif(employer: WpsEmployer, records: Sequence[WpsSalaryRecord]) -> BankFile:
    """Draft WPS SIF (status DRAFT_NOT_OFFERED): EDR rows sorted by instruction id, then the SCR.

    Field set from 3.2 §9.2. Not offered for payment until the paying bank's guide is implemented and
    a test file is accepted; ``generate_bank_file("AE_WPS_SIF", ...)`` refuses it.
    """
    if not isinstance(employer.created_at, datetime) or not isinstance(employer.salary_month, date):
        raise BankFileError("INVALID_EMPLOYER", "created_at must be a datetime and salary_month a date")
    if _DIGITS[13].fullmatch(employer.employer_id) is None or _DIGITS[9].fullmatch(employer.routing_code) is None:
        raise BankFileError("INVALID_EMPLOYER", "the employer id has 13 digits and the routing code 9")
    if re.fullmatch(r"[A-Za-z0-9 ._-]{0,35}", employer.reference) is None:
        raise BankFileError("INVALID_EMPLOYER", "the employer reference is up to 35 letters, digits, spaces, '.', '_' or '-'")
    if not records:
        raise BankFileError("EMPTY_BATCH", "a WPS file needs at least one employee record")
    digits = payable_minor_unit("AED", BankFileError)
    lines: list[str] = []
    seen: set[int] = set()
    total = Decimal(0)
    for record in sorted(records, key=lambda r: r.instruction_id):
        rid = record.instruction_id
        if isinstance(rid, bool) or not isinstance(rid, int) or rid <= 0 or rid in seen:
            raise BankFileError("INVALID_INSTRUCTION_ID", "instruction ids must be unique positive integers")
        seen.add(rid)
        if _DIGITS[14].fullmatch(record.person_id) is None or _DIGITS[9].fullmatch(record.routing_code) is None:
            raise _fail(rid, "INVALID_EMPLOYEE", "the person id has 14 digits and the agent routing code 9")
        iban = normalize_iban(record.iban)
        if iban_problems(iban, "AE"):
            raise _fail(rid, "INVALID_IBAN", "a valid UAE IBAN is required")
        if record.period_end < record.period_start or not 1 <= record.days <= 31 or not 0 <= record.leave_days <= record.days:
            raise _fail(rid, "INVALID_PERIOD", "check the salary period, its days and the unpaid leave days")
        fixed = _wps_amount(rid, record.fixed_pay, digits)
        variable = _wps_amount(rid, record.variable_pay, digits)
        total += fixed + variable
        lines.append(
            ",".join(
                (
                    "EDR", record.person_id, record.routing_code, iban, record.period_start.isoformat(),
                    record.period_end.isoformat(), str(record.days), amount_text(fixed, digits),
                    amount_text(variable, digits), str(record.leave_days),
                )
            )
        )
    created = employer.created_at
    lines.append(
        ",".join(
            (
                "SCR", employer.employer_id, employer.routing_code, created.strftime("%Y-%m-%d"), created.strftime("%H%M"),
                employer.salary_month.strftime("%m%Y"), str(len(seen)), amount_text(total, digits), "AED", employer.reference,
            )
        )
    )
    content = ("\r\n".join(lines) + "\r\n").encode("ascii")
    return _bank_file(
        FORMATS["AE_WPS_SIF"],
        f"{employer.employer_id}-{employer.salary_month:%Y%m}",
        f"{employer.employer_id}{created:%y%m%d%H%M%S}.SIF",
        content,
        {"AED": total},
        {"AED": len(seen)},
    )

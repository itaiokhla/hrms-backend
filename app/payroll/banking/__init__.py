"""Banking primitives for payroll v2 (3.7): pure, deterministic, no database or network.

    validators     per-country bank details and format checks (IFSC, IBAN, ABA, sort code, BIC)
    controls       verification status, duplicate fingerprints, step-up, cooling-off, velocity
    crypto         AES-256-GCM envelope encryption and keyed fingerprints
    masking        masked display of account numbers, IBANs and names; payload redaction
    allocation     split-payment allocation of net pay; cash rounding
    files          payment files (generic CSV offered; other formats refused until accepted by a bank)
    statements     CSV and CAMT.053 statement parsing with idempotent line hashes
    matching       reconciliation of statement lines to payment instructions; overdue detection
    business_days  bank business days, value-date rolling and settlement windows

Invalid user data (an IBAN with a wrong checksum, an unreadable statement row, fixed allocations
above the net) is returned as data; wrong arguments raise a ``BankingError`` with a stable code.
"""

from app.payroll.banking.allocation import Allocation, AllocationResult, allocate_net, cash_payment, validate_allocations
from app.payroll.banking.business_days import BusinessCalendar, payment_value_date
from app.payroll.banking.crypto import (
    DataKey,
    decrypt,
    derive_fingerprint_key,
    encrypt,
    fingerprint,
    fingerprints_equal,
    generate_data_key,
    rewrap,
    unwrap_key,
    wrap_key,
)
from app.payroll.banking.errors import (
    AllocationError,
    BankFileError,
    BankingError,
    BankValidationError,
    CryptoError,
    MatchingError,
    StatementError,
)
from app.payroll.banking.files import PaymentRecord, generate_bank_file, require_offered_format
from app.payroll.banking.masking import mask_account, mask_iban, mask_name, redact
from app.payroll.banking.matching import InstructionRef, find_overdue, match_lines, reconcile
from app.payroll.banking.statements import CsvColumnMapping, StatementLine, parse_camt053, parse_csv_statement
from app.payroll.banking.validators import BankAccountDetails, ValidationResult, validate_account

__all__ = [
    "Allocation",
    "AllocationError",
    "AllocationResult",
    "BankAccountDetails",
    "BankFileError",
    "BankValidationError",
    "BankingError",
    "BusinessCalendar",
    "CryptoError",
    "CsvColumnMapping",
    "DataKey",
    "InstructionRef",
    "MatchingError",
    "PaymentRecord",
    "StatementError",
    "StatementLine",
    "ValidationResult",
    "allocate_net",
    "cash_payment",
    "decrypt",
    "derive_fingerprint_key",
    "encrypt",
    "find_overdue",
    "fingerprint",
    "fingerprints_equal",
    "generate_bank_file",
    "generate_data_key",
    "mask_account",
    "mask_iban",
    "mask_name",
    "match_lines",
    "parse_camt053",
    "parse_csv_statement",
    "payment_value_date",
    "reconcile",
    "redact",
    "require_offered_format",
    "rewrap",
    "unwrap_key",
    "validate_account",
    "validate_allocations",
    "wrap_key",
]

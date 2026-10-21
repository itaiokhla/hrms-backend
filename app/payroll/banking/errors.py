"""Typed errors for the banking primitives.

Every error carries a stable upper-case ``code`` that callers map to API responses and tests assert
on. Messages describe the problem without echoing account numbers, IBANs, keys, plaintext or
amounts, so an error can be logged or shown as-is (3.7 §12, EC-BNK-11).

Problems in the data itself (an invalid IBAN typed by an employee, a statement row that cannot be
read, fixed allocations that exceed the net) are returned as results, not raised.
"""


class BankingError(Exception):
    """Base class: ``code`` is stable, ``message`` is safe to log."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class BankValidationError(BankingError):
    """Invalid arguments to a validator or control (not invalid bank details, which are data)."""


class CryptoError(BankingError):
    """Key handling, envelope format or authentication failure."""


class AllocationError(BankingError):
    """Invalid arguments to the split-payment allocator."""


class BankFileError(BankingError):
    """A payment file cannot be produced for this input or format."""


class StatementError(BankingError):
    """A bank statement file cannot be read safely."""


class MatchingError(BankingError):
    """Invalid arguments to reconciliation."""

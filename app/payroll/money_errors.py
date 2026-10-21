"""Typed errors shared by the money, currency and FX modules.

Every error carries a stable machine-readable ``code`` (used in API responses,
audit details and exception records) next to a human-readable message. They
subclass ValueError, so callers that already treat bad input as ValueError
keep working. The modules re-export what they raise:

    MoneyError          app.payroll.money        invalid amount, rounding or precision
    CurrencyMismatch    app.payroll.money        arithmetic or comparison across currencies
    CurrencyError       app.payroll.currencies   unknown, malformed or non-payroll currency
    LocaleError         app.payroll.currencies   malformed or unknown locale (display only)
    FxError             app.payroll.fx           invalid, stale, unapproved or mismatched rate
"""

from __future__ import annotations


class MoneyError(ValueError):
    """A money rule was violated; ``code`` is stable, the message is for people."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.code!r}, {self.message!r})"


class CurrencyMismatch(MoneyError):
    """Two amounts in different currencies were added, subtracted, divided or compared."""

    def __init__(self, left: str, right: str, operation: str) -> None:
        super().__init__(
            "CURRENCY_MISMATCH",
            f"Cannot {operation} {left} and {right} amounts; convert explicitly with a recorded FX rate.",
        )
        self.left = left
        self.right = right
        self.operation = operation


class CurrencyError(MoneyError):
    """A currency code is malformed, unknown or not allowed for the requested use."""


class LocaleError(MoneyError):
    """A locale identifier is malformed or unknown to CLDR."""

"""Masking of bank details for UI payloads, reports, exports, logs, notifications and errors.

Bank numbers are masked everywhere by default (3.7 §12, EC-BNK-11). At most the last four
characters stay visible and at least four are always hidden, so ``mask_account("123456784321")`` is
``"••••4321"`` while a six-digit number shows only its last two. The mask has a fixed width, so it
does not reveal the length either. The same rule produces the stored ``last4`` display column.

``redact`` masks bank fields inside nested audit or log payloads, ``scrub_text`` masks IBAN-like
strings and long digit runs in free text, ``reveals`` checks that a text (a notification, a log line)
contains none of a set of full values (EC-PAY-21), and ``Sensitive`` carries a plaintext value whose
``repr``/``str`` are masked so it cannot leak through logging or tracebacks by accident.
"""

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

MASK = "•" * 4
ASCII_MASK = "*" * 4
VISIBLE = 4

SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "account",
        "account_no",
        "account_number",
        "account_number_enc",
        "bank_account",
        "beneficiary_account",
        "counterparty_account",
        "iban",
        "iban_enc",
    }
)

_IBAN_IN_TEXT = re.compile(r"\b[A-Z]{2}[0-9]{2}(?: ?[A-Z0-9]){11,30}\b")
_DIGIT_RUN = re.compile(r"(?<![0-9])[0-9]{8,}(?![0-9])")


def _compact(value: str) -> str:
    return "".join(value.split())


def visible_length(length: int) -> int:
    """How many trailing characters of a value of this length may be shown."""
    return max(0, min(VISIBLE, length - VISIBLE))


def last4(value: str | None) -> str:
    """The visible suffix of an account number (the ``last4`` display column); may be shorter than 4."""
    if not value:
        return ""
    compact = _compact(value)
    shown = visible_length(len(compact))
    return compact[len(compact) - shown :] if shown else ""


def mask_account(value: str | None, *, mask: str = MASK) -> str:
    """``"123456784321"`` -> ``"••••4321"``; empty for a missing value."""
    if value is None or not _compact(value):
        return ""
    return mask + last4(value)


def mask_iban(value: str | None, *, mask: str = MASK) -> str:
    """``"AE070331234567890123456"`` -> ``"AE••••3456"`` (country code and the last four only)."""
    if value is None or not _compact(value):
        return ""
    compact = _compact(value).upper()
    if len(compact) < 2 + 2 * VISIBLE:
        return mask_account(compact, mask=mask)
    return compact[:2] + mask + compact[-VISIBLE:]


def mask_name(value: str | None) -> str:
    """``"Asha Rao"`` -> ``"A••• R•••"``: initials only, for logs and public verification pages."""
    if not value:
        return ""
    return " ".join(token[0] + "•" * 3 for token in value.split())


def mask_value(value: str | None, *, visible: int = VISIBLE, mask: str = MASK) -> str:
    """Generic mask keeping at most ``visible`` trailing characters and hiding at least as many."""
    if value is None or not _compact(value):
        return ""
    compact = _compact(value)
    shown = max(0, min(visible, len(compact) - visible))
    return mask + (compact[len(compact) - shown :] if shown else "")


def _mask_leaf(key: str, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "[encrypted]"
    if isinstance(value, Mapping):
        return {k: _mask_leaf(key, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_leaf(key, v) for v in value]
    text = str(value)
    return mask_iban(text) if "iban" in key else mask_account(text)


def redact(value: Any, *, keys: frozenset[str] = SENSITIVE_KEYS) -> Any:
    """Copy of a nested payload with every value under a sensitive key masked (keys match case-insensitively)."""
    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            name = key.lower() if isinstance(key, str) else None
            out[key] = _mask_leaf(name, item) if name in keys else redact(item, keys=keys)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item, keys=keys) for item in value]
    return value


def scrub_text(text: str) -> str:
    """Mask IBAN-like strings and runs of 8+ digits in free text (log lines, third-party error messages)."""
    masked = _IBAN_IN_TEXT.sub(lambda m: mask_iban(m.group()), text)
    return _DIGIT_RUN.sub(lambda m: mask_account(m.group()), masked)


def reveals(text: str, values: Iterable[str | None]) -> bool:
    """True when any full value (ignoring spaces and case) appears in the text."""
    haystack = _compact(text).upper()
    for value in values:
        if not value:
            continue
        needle = _compact(value).upper()
        if len(needle) > VISIBLE and needle in haystack:
            return True
    return False


@dataclass(frozen=True, repr=False)
class Sensitive:
    """A plaintext bank value whose ``repr``, ``str`` and ``format`` are masked; ``reveal()`` returns it."""

    value: str

    def reveal(self) -> str:
        return self.value

    @property
    def masked(self) -> str:
        return mask_account(self.value)

    def __repr__(self) -> str:
        return f"Sensitive({self.masked!r})"

    def __str__(self) -> str:
        return self.masked

    def __format__(self, spec: str) -> str:
        return format(self.masked, spec)

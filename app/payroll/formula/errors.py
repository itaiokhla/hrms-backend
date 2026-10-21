"""Formula errors: one exception type with stable codes, and issues as data.

``FormulaError`` is raised by ``parse`` and ``check``: a formula that fails them
cannot be saved (3.3 §4.5). At run time ``evaluate`` returns the error inside
``EvalResult.error`` together with the partial trace, so a data-dependent
failure (division by zero, a NULL input, a value outside every band) becomes an
employee exception with its trace instead of a silent zero. ``FormulaIssue`` is
the serialisable form used for analysis errors and warnings.

Codes beyond the contract list (ARCHITECTURE §4.5): ``INVALID_ARGUMENT`` for a
function argument outside its domain (an impossible date, ROUND places out of
range, CLAMP with lo > hi) and ``INVALID_TABLE`` for malformed table data.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Sequence


class ErrorCode(StrEnum):
    PARSE_ERROR = "PARSE_ERROR"
    UNKNOWN_REFERENCE = "UNKNOWN_REFERENCE"
    TYPE_MISMATCH = "TYPE_MISMATCH"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    DIVISION_BY_ZERO = "DIVISION_BY_ZERO"
    NULL_VALUE = "NULL_VALUE"
    CYCLE_DETECTED = "CYCLE_DETECTED"
    STAGE_VIOLATION = "STAGE_VIOLATION"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    TABLE_NOT_FOUND = "TABLE_NOT_FOUND"
    NO_BAND_MATCH = "NO_BAND_MATCH"
    NEGATIVE_RESULT = "NEGATIVE_RESULT"
    PROTECTED_ATTRIBUTE = "PROTECTED_ATTRIBUTE"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    INVALID_TABLE = "INVALID_TABLE"


Severity = Literal["ERROR", "WARNING"]


@dataclass(frozen=True)
class FormulaIssue:
    """An error or warning found in a formula, with the character span it refers to."""

    code: str
    message: str
    severity: Severity = "ERROR"
    position: int | None = None
    end: int | None = None
    path: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "position": self.position,
            "end": self.end,
            "path": list(self.path) if self.path is not None else None,
        }


class FormulaError(Exception):
    """A formula problem with a stable ``code`` (an ``ErrorCode`` value).

    ``position``/``end`` are 0-based character offsets into the formula source;
    ``path`` is the dependency path for CYCLE_DETECTED and STAGE_VIOLATION.
    ``issues`` holds every error found when several were collected (``check``).
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        position: int | None = None,
        end: int | None = None,
        path: Sequence[str] | None = None,
        issues: Sequence[FormulaIssue] = (),
    ) -> None:
        self.code: str = ErrorCode(code).value
        self.message = message
        self.position = position
        self.end = end
        self.path: list[str] | None = list(path) if path is not None else None
        self.issues: tuple[FormulaIssue, ...] = tuple(issues)
        super().__init__(f"{self.code}: {message}")

    def _key(self) -> tuple[Any, ...]:
        return (self.code, self.message, self.position, self.end,
                tuple(self.path) if self.path is not None else None, self.issues)

    def __eq__(self, other: object) -> bool:
        """Errors are values: the same problem found twice compares equal (deterministic results)."""
        if not isinstance(other, FormulaError):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __reduce__(self) -> tuple[Any, ...]:
        """Pickle by value, so errors survive job queues and process pools unchanged."""
        return (_rebuild, (self.code, self.message, self.position, self.end, self.path, self.issues))

    def to_issue(self, severity: Severity = "ERROR") -> FormulaIssue:
        return FormulaIssue(
            self.code,
            self.message,
            severity,
            self.position,
            self.end,
            tuple(self.path) if self.path is not None else None,
        )

    @classmethod
    def from_issue(cls, issue: FormulaIssue, issues: Sequence[FormulaIssue] = ()) -> FormulaError:
        return cls(
            issue.code,
            issue.message,
            position=issue.position,
            end=issue.end,
            path=issue.path,
            issues=issues,
        )

    def to_dict(self) -> dict[str, Any]:
        return self.to_issue().to_dict()


def _rebuild(
    code: str,
    message: str,
    position: int | None,
    end: int | None,
    path: Sequence[str] | None,
    issues: Sequence[FormulaIssue],
) -> FormulaError:
    return FormulaError(code, message, position=position, end=end, path=path, issues=issues)

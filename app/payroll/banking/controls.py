"""Pure rules behind bank-account verification and the payroll-diversion defences (3.7 §3.2, §3.5, §4).

* ``verification_status``: FORMAT proves the format only and is never enough for VERIFIED; the
  methods of ``payroll.bank.verification_methods`` must all pass, at least one of them a real
  ownership check (contract §22.2). A failed method gives FAILED (EC-BNK-10), a risk flag
  REQUIRES_REVIEW (EC-BNK-07, EC-BNK-08).
* ``duplicate_fingerprints``: one fingerprint on two payees flags every account involved and holds
  payments to the payees who added it later (EC-BNK-07).
* ``step_up_is_fresh``, ``usable_from`` and ``velocity_blocked``: step-up within 5 minutes
  (EC-BNK-06), the cooling-off period (default 48 hours, waivable only with a reason) and the
  velocity limit (more than 2 changes in 90 days is blocked pending review, EC-BNK-13).

Times are passed in by the caller; the clock is never read.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Collection, Iterable, Sequence

from app.payroll.banking.errors import BankValidationError

METHODS: tuple[str, ...] = ("FORMAT", "DOCUMENT_REVIEW", "PENNY_DROP", "PROVIDER_NAME_MATCH")
OWNERSHIP_METHODS: frozenset[str] = frozenset(METHODS) - {"FORMAT"}
STATUSES: tuple[str, ...] = ("NOT_SUBMITTED", "PENDING", "VERIFIED", "FAILED", "EXPIRED", "REQUIRES_REVIEW")
STEP_UP_MAX_AGE = timedelta(minutes=5)
DEFAULT_COOLING_OFF_HOURS = 48
VELOCITY_MAX_CHANGES = 2
VELOCITY_WINDOW = timedelta(days=90)


def _methods(values: Collection[str], label: str) -> frozenset[str]:
    if isinstance(values, str) or not all(isinstance(v, str) for v in values):
        raise BankValidationError("INVALID_ARGUMENT", f"{label} must be a collection of method codes")
    methods = frozenset(v.strip().upper() for v in values)
    unknown = methods - set(METHODS)
    if unknown:
        raise BankValidationError("UNKNOWN_METHOD", f"unknown verification methods in {label}: {', '.join(sorted(unknown))}")
    return methods


def verification_status(
    passed: Collection[str],
    required: Collection[str],
    *,
    submitted: bool = True,
    failed: Collection[str] = (),
    risk_flags: Collection[str] = (),
    verified_on: date | None = None,
    as_of: date | None = None,
    max_age_days: int | None = None,
) -> str:
    """The verification status of one account from the methods that passed or failed (3.7 §4)."""
    required_set = _methods(required, "required")
    if not required_set & OWNERSHIP_METHODS:
        raise BankValidationError(
            "FORMAT_NOT_SUFFICIENT", "FORMAT alone never verifies an account; require at least one ownership method"
        )
    passed_set, failed_set = _methods(passed, "passed"), _methods(failed, "failed")
    if not submitted:
        return "NOT_SUBMITTED"
    if failed_set:
        return "FAILED"
    if any(flag for flag in risk_flags):
        return "REQUIRES_REVIEW"
    if not required_set <= passed_set:
        return "PENDING"
    if max_age_days is not None:
        if isinstance(max_age_days, bool) or not isinstance(max_age_days, int) or max_age_days <= 0:
            raise BankValidationError("INVALID_ARGUMENT", "max_age_days must be a positive integer")
        if verified_on is None or as_of is None:
            raise BankValidationError("INVALID_ARGUMENT", "verified_on and as_of are needed to apply an expiry")
        if (as_of - verified_on).days > max_age_days:
            return "EXPIRED"
    return "VERIFIED"


@dataclass(frozen=True)
class AccountFingerprint:
    account_id: int
    payee_id: str  # "EMPLOYEE:12", "CONTRACTOR:4", ...
    fingerprint: str
    sequence: int  # creation order (for example the row id): lower is older


@dataclass(frozen=True)
class DuplicateFinding:
    fingerprint: str
    flagged_account_ids: tuple[int, ...]  # all accounts sharing it: REQUIRES_REVIEW
    held_account_ids: tuple[int, ...]  # accounts of later payees: payments held until reviewed
    payee_ids: tuple[str, ...]


def duplicate_fingerprints(accounts: Iterable[AccountFingerprint]) -> tuple[DuplicateFinding, ...]:
    """Fingerprints shared by different payees (3.7 §3.5, EC-BNK-07); one payee's own history is not a duplicate."""
    groups: dict[str, list[AccountFingerprint]] = {}
    for account in accounts:
        if not isinstance(account, AccountFingerprint):
            raise BankValidationError("INVALID_ARGUMENT", "accounts must be AccountFingerprint objects")
        groups.setdefault(account.fingerprint, []).append(account)
    findings: list[DuplicateFinding] = []
    for fingerprint in sorted(groups):
        group = sorted(groups[fingerprint], key=lambda a: (a.sequence, a.account_id))
        payees = list(dict.fromkeys(a.payee_id for a in group))
        if len(payees) < 2:
            continue
        first_payee = payees[0]
        findings.append(
            DuplicateFinding(
                fingerprint=fingerprint,
                flagged_account_ids=tuple(sorted(a.account_id for a in group)),
                held_account_ids=tuple(sorted(a.account_id for a in group if a.payee_id != first_payee)),
                payee_ids=tuple(sorted(payees)),
            )
        )
    return tuple(findings)


def _aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BankValidationError("INVALID_ARGUMENT", f"{label} must be a timezone-aware datetime")
    return value


def step_up_is_fresh(step_up_at: datetime | None, at: datetime, *, max_age: timedelta = STEP_UP_MAX_AGE) -> bool:
    """True when the step-up authentication happened within ``max_age`` before ``at`` (EC-BNK-06)."""
    if step_up_at is None:
        return False
    age = _aware(at, "at") - _aware(step_up_at, "step_up_at")
    return timedelta(0) <= age <= max_age


def usable_from(approved_at: datetime, cooling_off_hours: int, *, waived_reason: str | None = None) -> datetime:
    """When a newly approved account may receive payments (3.7 §3.2 step 3)."""
    _aware(approved_at, "approved_at")
    if isinstance(cooling_off_hours, bool) or not isinstance(cooling_off_hours, int) or cooling_off_hours < 0:
        raise BankValidationError("INVALID_ARGUMENT", "cooling_off_hours must be a non-negative integer")
    if waived_reason is not None:
        if len(" ".join(waived_reason.split())) < 3:
            raise BankValidationError("REASON_REQUIRED", "waiving the cooling-off period needs a reason")
        return approved_at
    return approved_at + timedelta(hours=cooling_off_hours)


def velocity_blocked(
    previous_changes: Sequence[datetime],
    at: datetime,
    *,
    max_changes: int = VELOCITY_MAX_CHANGES,
    window: timedelta = VELOCITY_WINDOW,
) -> bool:
    """True when a change at ``at`` would exceed ``max_changes`` within ``window`` (EC-BNK-13)."""
    _aware(at, "at")
    recent = [c for c in previous_changes if timedelta(0) <= at - _aware(c, "change time") < window]
    return len(recent) + 1 > max_changes

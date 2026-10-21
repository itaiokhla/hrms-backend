"""Reconciliation: match bank statement lines to payment instructions (3.7 §9).

Lines are matched in two passes:

    1. by instruction reference: the line's end-to-end id or reference equals the instruction
       reference, or the reference appears as a whole token in the remittance text;
    2. otherwise by exact amount and currency + beneficiary account + value date within
       ``window`` business days (default ±3) of the expected settlement date (or the submission date).

Outcomes per line:

    MATCHED          one instruction, exact amount and currency: evidence that settles it
    MISMATCH         the amount or currency differs (FX, bank fee), several candidates, or the
                     instruction is not in a payable state; never auto-settled (EC-PAY-11, EC-MON-18)
    UNMATCHED        a debit (or a return) nobody can explain: the "unmatched bank lines" queue (EC-PAY-12)
    DUPLICATE        a second debit for one instruction, or a debit for an instruction that already
                     has settlement evidence: escalated as a possible double payment
    RETURN           a credit that brings an instruction's money back (3.7 §10)
    ALREADY_MATCHED  the very line that is already stored as this instruction's evidence (re-import)
    IGNORED          a credit unrelated to any instruction (for example a funding transfer)

``find_overdue`` lists instructions past their expected settlement date without evidence (the
"overdue" queue). Results are data: nothing here changes an instruction's status, and the caller
decides (through the state machine) what a MATCHED line does. Dates come from the caller; the clock
is never read.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Collection, Iterable, Mapping, Sequence

from app.payroll.banking.business_days import BusinessCalendar
from app.payroll.banking.errors import MatchingError
from app.payroll.banking.masking import mask_account, mask_iban
from app.payroll.banking.statements import CREDIT, DEBIT, StatementLine, return_reason_message

MATCHED = "MATCHED"
MISMATCH = "MISMATCH"
UNMATCHED = "UNMATCHED"
DUPLICATE = "DUPLICATE"
RETURN = "RETURN"
ALREADY_MATCHED = "ALREADY_MATCHED"
IGNORED = "IGNORED"
OUTCOMES: tuple[str, ...] = (MATCHED, MISMATCH, UNMATCHED, DUPLICATE, RETURN, ALREADY_MATCHED, IGNORED)
REVIEW_OUTCOMES: frozenset[str] = frozenset({MISMATCH, UNMATCHED, DUPLICATE})

OPEN_STATUSES: frozenset[str] = frozenset({"QUEUED", "SUBMITTED", "ACKNOWLEDGED", "PROCESSING", "PARTIALLY_SETTLED"})
EVIDENCED_STATUSES: frozenset[str] = frozenset({"SETTLED", "RECONCILED"})
RETURNABLE_STATUSES: frozenset[str] = OPEN_STATUSES | EVIDENCED_STATUSES
DEFAULT_WINDOW = 3

_NORMAL = re.compile(r"[^A-Z0-9]")
_REFERENCE = re.compile(r"[A-Z0-9][A-Z0-9/.\-]{0,34}")
_TOKEN = re.compile(r"[A-Z0-9][A-Z0-9/.\-]*")
_DEFAULT_CALENDAR = BusinessCalendar()


@dataclass(frozen=True, repr=False)
class InstructionRef:
    """What reconciliation needs from a payment instruction and its beneficiary snapshot.

    ``accounts`` are the beneficiary's identifiers (IBAN and/or account number) in any spacing;
    ``evidence_line_hashes`` are statement lines already stored as this instruction's evidence.
    """

    instruction_id: int
    reference: str
    amount: Decimal
    currency: str
    status: str
    accounts: tuple[str, ...] = ()
    expected_settlement_date: date | None = None
    submitted_on: date | None = None
    evidence_line_hashes: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        rid = self.instruction_id
        if isinstance(rid, bool) or not isinstance(rid, int) or rid <= 0:
            raise MatchingError("INVALID_INSTRUCTION", "instruction ids must be positive integers")
        if not isinstance(self.reference, str) or _REFERENCE.fullmatch(self.reference.strip().upper()) is None:
            raise MatchingError("INVALID_INSTRUCTION", f"instruction {rid}: the reference is 1-35 letters, digits, '/', '.' or '-'")
        if isinstance(self.amount, bool) or not isinstance(self.amount, Decimal) or not self.amount.is_finite() or self.amount <= 0:
            raise MatchingError("INVALID_INSTRUCTION", f"instruction {rid}: the amount must be a positive Decimal")
        if not isinstance(self.currency, str) or re.fullmatch(r"[A-Z]{3}", self.currency.strip().upper()) is None:
            raise MatchingError("INVALID_INSTRUCTION", f"instruction {rid}: the currency must be a three-letter code")
        if not isinstance(self.status, str) or not self.status.strip():
            raise MatchingError("INVALID_INSTRUCTION", f"instruction {rid}: the status is required")
        for day in (self.expected_settlement_date, self.submitted_on):
            if day is not None and not isinstance(day, date):
                raise MatchingError("INVALID_INSTRUCTION", f"instruction {rid}: dates must be dates")
        if isinstance(self.accounts, str) or not all(isinstance(a, str) for a in self.accounts):
            raise MatchingError("INVALID_INSTRUCTION", f"instruction {rid}: accounts must be a tuple of strings")
        object.__setattr__(self, "reference", self.reference.strip().upper())
        object.__setattr__(self, "currency", self.currency.strip().upper())
        object.__setattr__(self, "status", self.status.strip().upper())
        object.__setattr__(self, "accounts", tuple(sorted({normalize_account(a) for a in self.accounts} - {""})))
        object.__setattr__(self, "evidence_line_hashes", frozenset(self.evidence_line_hashes))

    def __repr__(self) -> str:
        masked = [mask_iban(a) if a[:2].isalpha() else mask_account(a) for a in self.accounts]
        return (
            f"InstructionRef(instruction_id={self.instruction_id!r}, reference={self.reference!r}, "
            f"currency={self.currency!r}, status={self.status!r}, accounts={masked!r})"
        )


@dataclass(frozen=True)
class LineMatch:
    """The outcome for one statement line."""

    line_hash: str
    source_row: int
    direction: str
    outcome: str
    reason: str
    instruction_id: int | None = None
    candidate_ids: tuple[int, ...] = ()
    variance: Decimal | None = None  # line amount - instruction amount, same currency only
    settles: bool = False  # MATCHED debit: settlement evidence for ``instruction_id``
    message: str = ""

    @property
    def needs_review(self) -> bool:
        return self.outcome in REVIEW_OUTCOMES


@dataclass(frozen=True)
class OverdueItem:
    instruction_id: int
    reference: str
    status: str
    expected_settlement_date: date
    business_days_overdue: int


@dataclass(frozen=True)
class ReconciliationResult:
    matches: tuple[LineMatch, ...]
    overdue: tuple[OverdueItem, ...]

    def by_outcome(self, outcome: str) -> tuple[LineMatch, ...]:
        return tuple(match for match in self.matches if match.outcome == outcome)

    @property
    def counts(self) -> Mapping[str, int]:
        return {outcome: len(self.by_outcome(outcome)) for outcome in OUTCOMES}

    @property
    def settled_instruction_ids(self) -> tuple[int, ...]:
        return tuple(sorted({m.instruction_id for m in self.matches if m.settles and m.instruction_id is not None}))


@dataclass(frozen=True)
class AcceptedVariance:
    """A reviewer's decision to accept a MISMATCH as a settlement variance (EC-PAY-11, EC-MON-18)."""

    line_hash: str
    instruction_id: int
    variance: Decimal
    currency: str
    reason: str


def normalize_account(value: str) -> str:
    """Account identifiers compared without spaces, punctuation or case."""
    return _NORMAL.sub("", value.upper())


def reference_tokens(text: str) -> set[str]:
    """Whole reference-shaped tokens of a text ("Salary PAY-7." -> {"SALARY", "PAY-7"})."""
    return {token.rstrip(".") for token in _TOKEN.findall(text.upper())} - {""}


def references_in(line: StatementLine, references: Mapping[str, int]) -> tuple[int, ...]:
    """Instruction ids whose reference the line carries (end-to-end id, reference or remittance token).

    A reference matches only as a whole token, so ``PAY-1`` is not found in ``PAY-12`` or ``PAY-1-X``.
    """
    tokens: set[str] = set()
    for text in (line.end_to_end_id, line.reference, line.remittance):
        if text:
            tokens.add(text.strip().upper())
            tokens |= reference_tokens(text)
    return tuple(sorted({references[token] for token in tokens if token in references}))


def _in_window(instruction: InstructionRef, day: date, calendar: BusinessCalendar, window: int) -> bool:
    base = instruction.expected_settlement_date or instruction.submitted_on
    return base is not None and abs(calendar.distance(base, day)) <= window


_AmountIndex = Mapping[tuple[str, Decimal], Sequence[InstructionRef]]


def _amount_index(instructions: Sequence[InstructionRef]) -> _AmountIndex:
    index: dict[tuple[str, Decimal], list[InstructionRef]] = {}
    for item in instructions:
        index.setdefault((item.currency, item.amount), []).append(item)
    return index


def _fallback_candidates(line: StatementLine, index: _AmountIndex, calendar: BusinessCalendar, window: int) -> tuple[int, ...]:
    """Instructions with this exact amount and currency, this beneficiary account and a date in the window."""
    account = normalize_account(line.counterparty_account) if line.counterparty_account else ""
    if not account:
        return ()
    return tuple(
        item.instruction_id
        for item in index.get((line.currency, line.amount), ())
        if item.status in RETURNABLE_STATUSES and account in item.accounts and _in_window(item, line.value_date, calendar, window)
    )


def _check_inputs(lines: Sequence[StatementLine], instructions: Sequence[InstructionRef], window: int) -> None:
    if isinstance(window, bool) or not isinstance(window, int) or window < 0:
        raise MatchingError("INVALID_ARGUMENT", "the date window is a non-negative number of business days")
    if not all(isinstance(line, StatementLine) for line in lines):
        raise MatchingError("INVALID_ARGUMENT", "lines must be StatementLine objects")
    if not all(isinstance(item, InstructionRef) for item in instructions):
        raise MatchingError("INVALID_ARGUMENT", "instructions must be InstructionRef objects")
    hashes = [line.line_hash for line in lines]
    if len(set(hashes)) != len(hashes):
        raise MatchingError("DUPLICATE_LINE", "a statement line appears twice in the input")
    ids = [item.instruction_id for item in instructions]
    if len(set(ids)) != len(ids):
        raise MatchingError("DUPLICATE_INSTRUCTION", "an instruction appears twice in the input")
    references = [item.reference for item in instructions]
    if len(set(references)) != len(references):
        raise MatchingError("DUPLICATE_REFERENCE", "two instructions share a payment reference")


def _amount_problem(line: StatementLine, item: InstructionRef) -> tuple[str, Decimal | None] | None:
    if line.currency != item.currency:
        return "CURRENCY_DIFFERS", None
    if line.amount != item.amount:
        return "AMOUNT_DIFFERS", line.amount - item.amount
    return None


def _match(line: StatementLine, outcome: str, reason: str, message: str, **extra: object) -> LineMatch:
    return LineMatch(line.line_hash, line.source_row, line.direction, outcome, reason, message=message, **extra)  # type: ignore[arg-type]


def _resolve_debits(
    claims: Mapping[int, list[tuple[int, str]]],
    lines: Sequence[StatementLine],
    by_id: Mapping[int, InstructionRef],
    out: dict[int, LineMatch],
) -> None:
    """Turn the debit lines claiming each instruction into MATCHED / MISMATCH / DUPLICATE."""
    for instruction_id in sorted(claims):
        item = by_id[instruction_id]
        claimants = claims[instruction_id]
        evidenced = item.status in EVIDENCED_STATUSES or bool(item.evidence_line_hashes)
        primary: int | None = None
        if not evidenced:
            exact = [index for index, _ in claimants if _amount_problem(lines[index], item) is None]
            primary = exact[0] if exact else claimants[0][0]
        for index, how in claimants:
            line = lines[index]
            if index != primary:
                why = "already has settlement evidence" if evidenced else "already has a debit in this statement"
                out[index] = _match(
                    line, DUPLICATE, "SECOND_DEBIT", f"possible double payment: the instruction {why}",
                    instruction_id=instruction_id, candidate_ids=(instruction_id,),
                )
                continue
            if item.status not in OPEN_STATUSES:
                out[index] = _match(
                    line, MISMATCH, "INSTRUCTION_NOT_PAYABLE",
                    f"a debit for an instruction in status {item.status}; review before recording it",
                    instruction_id=instruction_id, candidate_ids=(instruction_id,),
                )
                continue
            problem = _amount_problem(line, item)
            if problem is None:
                out[index] = _match(
                    line, MATCHED, how, "unique match: settlement evidence for the instruction",
                    instruction_id=instruction_id, candidate_ids=(instruction_id,), settles=True,
                )
            else:
                reason, variance = problem
                message = (
                    "the bank amount differs from the instructed amount; never settled automatically"
                    if reason == "AMOUNT_DIFFERS"
                    else "the bank currency differs from the instructed currency; never settled automatically"
                )
                out[index] = _match(
                    line, MISMATCH, reason, message,
                    instruction_id=instruction_id, candidate_ids=(instruction_id,), variance=variance,
                )


def _credit(
    line: StatementLine,
    by_reference: Mapping[str, int],
    by_id: Mapping[int, InstructionRef],
    index: _AmountIndex,
    calendar: BusinessCalendar,
    window: int,
) -> LineMatch:
    ids = references_in(line, by_reference)
    how = "REFERENCE"
    if not ids and line.return_indicated:
        ids, how = _fallback_candidates(line, index, calendar, window), "AMOUNT_ACCOUNT_DATE"
    ids = tuple(i for i in ids if by_id[i].status in RETURNABLE_STATUSES) if ids else ids
    if len(ids) == 1:
        item = by_id[ids[0]]
        problem = _amount_problem(line, item)
        return _match(
            line, RETURN, how, f"the payment came back: {return_reason_message(line.return_reason)}",
            instruction_id=item.instruction_id, candidate_ids=ids,
            variance=None if problem is None else problem[1],
        )
    if len(ids) > 1:
        return _match(line, UNMATCHED, "MULTIPLE_CANDIDATES", "a returned payment matches several instructions", candidate_ids=ids)
    if line.return_indicated:
        return _match(line, UNMATCHED, "NO_CANDIDATE", "a returned payment that matches no instruction")
    return _match(line, IGNORED, "CREDIT", "a credit unrelated to payment instructions")


def match_lines(
    lines: Sequence[StatementLine],
    instructions: Sequence[InstructionRef],
    *,
    calendar: BusinessCalendar = _DEFAULT_CALENDAR,
    window: int = DEFAULT_WINDOW,
) -> tuple[LineMatch, ...]:
    """One ``LineMatch`` per statement line, in input order (3.7 §9)."""
    lines, instructions = tuple(lines), tuple(instructions)
    _check_inputs(lines, instructions, window)
    instructions = tuple(sorted(instructions, key=lambda item: item.instruction_id))
    amounts = _amount_index(instructions)
    by_id = {item.instruction_id: item for item in instructions}
    by_reference = {item.reference: item.instruction_id for item in instructions}
    evidence = {h: item.instruction_id for item in instructions for h in item.evidence_line_hashes}
    out: dict[int, LineMatch] = {}
    claims: dict[int, list[tuple[int, str]]] = {}
    pending: list[int] = []
    for index, line in enumerate(lines):
        if line.line_hash in evidence:
            owner = evidence[line.line_hash]
            out[index] = _match(
                line, ALREADY_MATCHED, "EVIDENCE_ON_FILE", "this line is already recorded as the instruction's evidence",
                instruction_id=owner, candidate_ids=(owner,),
            )
        elif line.direction == CREDIT:
            out[index] = _credit(line, by_reference, by_id, amounts, calendar, window)
        elif line.direction != DEBIT:
            raise MatchingError("INVALID_ARGUMENT", "statement lines are DEBIT or CREDIT")
        else:
            ids = references_in(line, by_reference)
            if len(ids) == 1:
                claims.setdefault(ids[0], []).append((index, "REFERENCE"))
            elif len(ids) > 1:
                out[index] = _match(
                    line, MISMATCH, "MULTIPLE_CANDIDATES", "the line carries the references of several instructions",
                    candidate_ids=ids,
                )
            else:
                pending.append(index)
    for index in pending:
        line = lines[index]
        ids = _fallback_candidates(line, amounts, calendar, window)
        if len(ids) == 1:
            claims.setdefault(ids[0], []).append((index, "AMOUNT_ACCOUNT_DATE"))
        elif len(ids) > 1:
            out[index] = _match(
                line, MISMATCH, "MULTIPLE_CANDIDATES",
                "several instructions have this amount, account and date window; choose one", candidate_ids=ids,
            )
        else:
            out[index] = _match(line, UNMATCHED, "NO_CANDIDATE", "a debit that matches no payment instruction")
    for claimants in claims.values():
        claimants.sort(key=lambda claim: claim[0])
    _resolve_debits(claims, lines, by_id, out)
    return tuple(out[index] for index in range(len(lines)))


def find_overdue(
    instructions: Iterable[InstructionRef],
    as_of: date,
    *,
    calendar: BusinessCalendar = _DEFAULT_CALENDAR,
    grace: int = 0,
    evidenced_ids: Collection[int] = (),
) -> tuple[OverdueItem, ...]:
    """Open instructions more than ``grace`` business days past their expected settlement date without evidence."""
    if not isinstance(as_of, date):
        raise MatchingError("INVALID_ARGUMENT", "as_of must be a date")
    if isinstance(grace, bool) or not isinstance(grace, int) or grace < 0:
        raise MatchingError("INVALID_ARGUMENT", "grace is a non-negative number of business days")
    items: list[OverdueItem] = []
    for item in sorted(instructions, key=lambda i: i.instruction_id):
        expected = item.expected_settlement_date
        if item.status not in OPEN_STATUSES or expected is None or item.evidence_line_hashes:
            continue
        if item.instruction_id in evidenced_ids:
            continue
        late = calendar.distance(expected, as_of)
        if late > grace:
            items.append(OverdueItem(item.instruction_id, item.reference, item.status, expected, late))
    return tuple(items)


def reconcile(
    lines: Sequence[StatementLine],
    instructions: Sequence[InstructionRef],
    *,
    as_of: date | None = None,
    calendar: BusinessCalendar = _DEFAULT_CALENDAR,
    window: int = DEFAULT_WINDOW,
    grace: int = 0,
) -> ReconciliationResult:
    """Match a statement and, when ``as_of`` is given, list the instructions still overdue after it."""
    matches = match_lines(lines, instructions, calendar=calendar, window=window)
    overdue: tuple[OverdueItem, ...] = ()
    if as_of is not None:
        claimed = {m.instruction_id for m in matches if m.instruction_id is not None and m.outcome in (MATCHED, MISMATCH, DUPLICATE, ALREADY_MATCHED)}
        overdue = find_overdue(instructions, as_of, calendar=calendar, grace=grace, evidenced_ids=claimed)
    return ReconciliationResult(matches, overdue)


def accept_variance(match: LineMatch, instruction: InstructionRef, reason: str) -> AcceptedVariance:
    """Record a reviewer accepting an amount difference; only an AMOUNT_DIFFERS mismatch qualifies."""
    if match.outcome != MISMATCH or match.reason != "AMOUNT_DIFFERS" or match.variance is None:
        raise MatchingError("NOT_A_VARIANCE", "only an amount mismatch can be accepted as a settlement variance")
    if match.instruction_id != instruction.instruction_id:
        raise MatchingError("INVALID_ARGUMENT", "the match belongs to another instruction")
    text = " ".join(reason.split()) if isinstance(reason, str) else ""
    if len(text) < 3 or len(text) > 500:
        raise MatchingError("REASON_REQUIRED", "give a reason of 3 to 500 characters for accepting the variance")
    return AcceptedVariance(match.line_hash, instruction.instruction_id, match.variance, instruction.currency, text)

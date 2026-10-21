"""Status state machines of payroll v2: one data-driven transition table per lifecycle.

Every status change goes through ``next_state(machine, state, action)``:

    MACHINES[name].transitions[(state, action)] == (to_state, permission_code | None)

A permission of ``None`` marks a system action (calculation jobs, settlement evidence, derived
statuses) that no user request may trigger directly; ``platform:`` codes belong to platform
operators, not tenant roles. Guards (``NOT_STALE``, ``STEP_UP_MFA``…), required reasons, side
effects and audit events are declared beside each transition, so the services that enforce them
read one table (``GUARDS`` and ``EFFECTS`` describe every code).

Vocabularies are the contract's, verbatim (D-09): pay runs (3.5 §4.1), payment instructions and
batches (3.7 §8), bank accounts and their verification (3.7 §3.3, §4), documents and invoices
(3.6 §5.2, §10), tasks, wage inputs and claims (3.4 §10.2, §13, §14), loans (3.4 §15 and the 3.9
loan actions), compensation assignments (3.3 §7), setting values and policy versions (3.1 §2.5,
§3.2, 3.9 §3.1), country pack versions and statutory reports (3.2 §6.3, §8.2) and pay periods
(3.5 §2.5); policies (3.1 §3.2) have their own small machine.

Rules a table alone cannot express:

* a finalized run is immutable: any action other than paying, closing or reversing it fails
  with ``RUN_FINALIZED`` (EC-RUN-17); a reversal is a new ``REVERSAL`` run, so ``REVERSE`` leaves
  the original's status unchanged;
* payment evidence arrives out of order: ``apply_event`` takes forward jumps, ignores older or
  duplicate events and turns contradictory evidence into a review conflict, so an instruction
  never moves backwards (EC-PAY-07) and is never paid without having been submitted (EC-PAY-01);
* how an instruction was submitted (``SubmissionChannel``) and whether its evidence is verified
  (``EvidenceStatus``) are data beside the status (decision I-17): a manually uploaded file shows
  "Submitted for manual payment — unverified", and nothing is labelled paid on unverified evidence;
* the payment stage of a run and a batch is derived from its instructions (3.5 §5.9): nothing
  is ``PAID`` unless every instruction that is not held or cancelled is settled, and a run with
  nothing to pay is never reported as paid; a partly paid run may reconcile with its unsettled
  items carried forward (decision I-17);
* run identity (3.5 §3): one non-cancelled regular run per pay group and period (EC-RUN-01),
  one final settlement per employment period, and reversals only of finalized runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

# --------------------------------------------------------------------------- permission codes (3.9 §5.1)

RUN_CREATE = "payroll:run:create"
RUN_CALCULATE = "payroll:run:calculate"
RUN_REVIEW = "payroll:run:review"
RUN_APPROVE = "payroll:run:approve"
RUN_FINALIZE = "payroll:run:finalize"
RUN_REVERSE = "payroll:run:reverse"
RUN_CLOSE = "payroll:run:close"
PAYMENT_PREPARE = "payroll:payment:prepare"
PAYMENT_APPROVE = "payroll:payment:approve"
PAYMENT_SUBMIT = "payroll:payment:submit"
PAYMENT_RECONCILE = "payroll:payment:reconcile"
BANK_DETAILS_EDIT = "payroll:bank_details:edit"
BANK_DETAILS_APPROVE = "payroll:bank_details:approve"
PAYSLIP_PUBLISH = "payroll:payslip:publish"
INPUTS_EDIT = "payroll:inputs:edit"
INPUTS_APPROVE = "payroll:inputs:approve"
COMPENSATION_EDIT = "payroll:compensation:edit"
COMPENSATION_APPROVE = "payroll:compensation:approve"
LOANS_MANAGE = "payroll:loans:manage"
LOANS_APPROVE = "payroll:loans:approve"
REIMBURSEMENTS_MANAGE = "payroll:reimbursements:manage"
REIMBURSEMENTS_APPROVE = "payroll:reimbursements:approve"
CONFIGURATION_WRITE = "payroll:configuration:write"
SETTINGS_APPROVE = "settings:approve_changes"
STATUTORY_REVIEW = "payroll:statutory:review"
STATUTORY_SUBMIT = "payroll:statutory:submit"
TASK_MANAGE = "task:manage"
TASK_APPROVE = "task:approve"
PLATFORM_PACK_EDIT = "platform:country_packs:edit"
PLATFORM_PACK_REVIEW = "platform:country_packs:review"


def is_platform_permission(code: str | None) -> bool:
    """Platform-operator capabilities are checked on the platform principal, not tenant grants."""
    return code is not None and code.startswith("platform:")


# --------------------------------------------------------------------------- guards and effects

GUARDS: Mapping[str, str] = MappingProxyType({
    "BLOCKING_EXCEPTIONS_OPEN": "Pre-checks left blocking exceptions open (3.5 §5.2)",
    "NO_BLOCKING_EXCEPTIONS": "Every blocking exception is fixed, deferred or waived where waivable",
    "IDEMPOTENCY_KEY": "The request carries an Idempotency-Key; a repeat returns the first response "
                       "(EC-RUN-02, EC-RUN-04)",
    "NO_CALCULATION_RUNNING": "No calculation job is running for the run",
    "BLOCKING_CHECK_FAILED": "A blocking validation check failed (3.5 §10)",
    "NO_BLOCKING_CHECK_FAILED": "No blocking validation check failed (3.5 §10)",
    "VARIANCE_FLAGS_ACKNOWLEDGED": "Every variance flag is acknowledged, with a comment for large changes",
    "NOT_STALE": "Inputs, settings and pack versions are unchanged since lock, or recalculated (EC-RUN-09)",
    "SEPARATION_OF_DUTIES": "The preparer or last recalculator cannot approve when "
                            "payroll.approval.separation_of_duties is ON (EC-RUN-05)",
    "NO_SELF_PAY_APPROVAL": "An approver's own pay line needs a different approver (EC-RUN-06)",
    "NOT_LAST_STEP": "Further approval steps remain in the resolved chain",
    "LAST_STEP": "This is the last step of the resolved approval chain",
    "CALCULATION_RESULTS_EXIST": "The run has calculation results to review",
    "STEP_UP_MFA": "Fresh multi-factor re-authentication (payroll.approval.step_up_for_finalize)",
    "STATUTORY_PACK_VERIFIED": "Statutory lines need a VERIFIED_FOR_PERIOD pack version (3.5 §5.8, EC-STA-02)",
    "PAYMENT_BATCH_APPROVED": "A payment batch of the run was approved",
    "DUAL_CONTROL": "The batch preparer cannot approve or release it (payroll.approval.payment_dual_control)",
    "FUNDING_SIGNATORY": "The releaser is a signatory of the funding account",
    "SETTLEMENT_EVIDENCE": "Settled only from a statement match, a signed provider event or reviewed "
                           "manual proof (contract §23.2)",
    "RETURN_EVIDENCE": "A settled payment came back (statement, provider event or bank report)",
    "SETTLED_INSTRUCTIONS_MATCHED": "Every settled instruction is matched to bank evidence",
    "NO_OPEN_ITEMS_EXCEPT_HOLDS": "Nothing is open except holds, which are carried forward (3.5 §5.10)",
    "NOTHING_TO_PAY": "No payment instruction exists except held or cancelled ones (reversal runs, "
                      "zero or negative net, everyone on hold)",
    "TWO_APPROVERS": "A reversal needs a reason and two approvers (3.5 §4.1)",
    "BANK_ACCOUNT_USABLE": "An active, approved bank account past its cooling-off period",
    "PROVIDER_ACCEPTED": "The payment provider accepted the request",
    "FILE_HASH_UNCHANGED": "The released file is the generated file (SHA-256), released once (EC-PAY-13)",
    "EVIDENCE_UPLOADED": "Proof of a manual payment is uploaded (EC-PAY-14)",
    "SECOND_REVIEWER": "A second person reviewed the evidence (EC-PAY-14)",
    "STATEMENT_MATCHED": "The instruction is matched to a bank statement line",
    "REISSUE_APPROVAL": "Approved when payroll.payments.reissue_requires_approval is ON",
    "RUN_REVERSAL_AUTHORIZED": "Part of an authorized reversal of the run (EC-COR-04)",
    "MAKER_CHECKER": "The maker of a change cannot approve it",
    "APPROVER_IN_CHAIN": "The approver is the current step of the module's approval chain",
    "NOT_OWN_SALARY": "Nobody approves their own salary (3.3 §7)",
    "VELOCITY_LIMIT": "At most 2 bank changes in 90 days, else security review (EC-BNK-13)",
    "CHANGE_APPROVAL_OFF": "payroll.bank.change_requires_approval is OFF",
    "REQUIRED_METHODS_PASSED": "The methods of payroll.bank.verification_methods passed; FORMAT alone never "
                               "verifies (3.7 §4)",
    "RISK_FLAG": "Name mismatch, duplicate fingerprint or another risk flag (EC-BNK-07, EC-BNK-08)",
    "PAYMENT_FAILED_OR_RETURNED": "A payment to the account failed or came back (EC-BNK-10)",
    "VERIFICATION_EXPIRED": "Verification is older than the policy allows",
    "PUBLISH_TRIGGER_MET": "payslip.publish_trigger is satisfied (3.6 §5.1)",
    "RUN_NOT_REVERSED": "The document's run was not reversed",
    "ONE_NOTIFICATION_PER_VERSION": "One notification per document version (EC-DOC-05)",
    "NEWER_VERSION": "A newer version replaces this one",
    "CONTRACTOR_PAYEE": "Invoices only for contractors and genuine transactions (EC-DOC-07)",
    "SELF_BILLING_AGREEMENT": "A self-billed invoice needs an agreement on file and pack permission",
    "EVIDENCE_IF_REQUIRED": "Evidence attached when the policy requires it",
    "SPLIT_TOTALS_100": "Assignee split percentages total exactly 100.00 when splitting (EC-TSK-05)",
    "ACCEPTED_QUANTITY_WITHIN_SUBMITTED": "Accepted quantity is at most the submitted quantity (EC-TSK-04)",
    "FINALIZED_RUN": "A finalized run paid or consumed it (exactly once)",
    "PERIOD_ACCEPTS_INPUTS": "The target period is OPEN or PROCESSING, never CLOSED (EC-VAR-03)",
    "BELOW_APPROVAL_THRESHOLD": "At or below payroll.wage_inputs.require_approval_above",
    "WITHIN_POLICY_LIMITS": "Within the policy's limits",
    "RECEIPT_IF_REQUIRED": "A receipt is attached above the policy threshold",
    "DISBURSEMENT_RECORDED": "The disbursement transaction is recorded from payment evidence",
    "ZERO_BALANCE": "The loan ledger balance is zero",
    "EFFECTIVE_IN_FUTURE": "The effective date is after today (EC-CMP-11)",
    "EFFECTIVE_REACHED": "The effective date has been reached",
    "VALID_SCHEMA": "Field values pass the policy type's schema",
    "IMPACT_PREVIEW_DONE": "Impact preview run for changes that need it (3.1 §2.5)",
    "SOURCE_MANIFEST_COMPLETE": "Every rule has its source manifest (3.2 §6.4)",
    "REVIEWER_SIGN_OFF": "A recorded qualified reviewer signs off, never the author",
    "ALL_VECTORS_PASS": "All test vectors pass",
    "EFFECTIVE_TO_PASSED": "The version's effective_to date has passed",
    "RUNS_FINALIZED": "The report reads finalized runs only",
    "RECONCILES_TO_RUNS": "Totals reconcile to the immutable runs per component and liability",
    "FILING_REFERENCE": "SUBMITTED needs a filing reference (EC-STA-11)",
    "ACKNOWLEDGEMENT": "ACCEPTED needs the authority's acknowledgement",
    "AUTHORITY_RESPONSE": "The authority's rejection is recorded",
    "REGULAR_RUN_CREATED": "A regular run exists for the period and is not finalized",
    "NO_ACTIVE_REGULAR_RUN": "The period's regular run was cancelled",
    "REGULAR_RUN_FINALIZED": "The period's regular run is finalized",
    "UNSETTLED_ITEMS_CARRIED": "Every instruction that is not settled is carried forward with its reason "
                               "(returned, failed, unclaimed or held) for a later payment (decision I-17)",
    "NOT_FIXTURE": "A fixture pack version can never be VERIFIED_FOR_PERIOD (decision I-07)",
    "NOT_IN_USE": "No unfinished run uses the policy",
    "PERIOD_OPEN": "The period is OPEN (regular runs)",
    "UNIQUE_REGULAR_RUN": "No other non-cancelled regular run for the pay group and period (EC-RUN-01)",
    "PAY_GROUP_ACTIVE": "The pay group is active",
})

EFFECTS: Mapping[str, str] = MappingProxyType({
    "LOCK_INPUTS": "Snapshot every included employee's inputs in the same transaction (3.5 §5.3)",
    "RESNAPSHOT_SELECTED": "Re-snapshot only the selected employees",
    "ENQUEUE_CALCULATION": "Enqueue the chunked calculation job",
    "STOP_CALCULATION": "Cancel the running calculation job; its results are discarded",
    "RESOLVE_APPROVAL_CHAIN": "Resolve payroll.approval.run_chain_id",
    "NOTIFY_APPROVERS": "Notify the next approvers",
    "CLEAR_APPROVALS": "Clear recorded approvals, with an audit record (EC-RUN-16)",
    "RELEASE_INPUTS": "Release the snapshot and every consumable input (EC-RUN-15, EC-VAR-01)",
    "WRITE_YTD": "Write YTD increments (idempotent)",
    "WRITE_LIABILITIES": "Write statutory liabilities (idempotent)",
    "POST_SUBLEDGERS": "Post loan and leave-balance transactions (idempotent)",
    "CONSUME_INPUTS": "Mark wage inputs, tasks and claims consumed (exactly once)",
    "CREATE_PAYMENT_INSTRUCTIONS": "Create instructions from the bank-account snapshot",
    "ENQUEUE_DOCUMENTS": "Enqueue payslips, register and journals through the outbox",
    "APPLY_PUBLICATION_RULE": "Apply payslip.publish_trigger",
    "CLOSE_PERIOD_IF_REGULAR": "Close the pay period when this is its regular run",
    "CARRY_HOLDS": "Carry held payments into the carried-holds list for an off-cycle payment",
    "CREATE_REVERSAL_RUN": "Create a REVERSAL run of exact negative lines (3.5 §12)",
    "STORE_FILE_HASH": "Store the generated file's SHA-256 on the batch",
    "CREATE_REPLACEMENT_INSTRUCTION": "Create a READY instruction for the same obligation",
    "START_COOLING_OFF": "usable_from = approval + payroll.bank.cooling_off_hours",
    "NOTIFY_OLD_AND_NEW_CONTACTS": "Alert the old and the new contact details (locked ON)",
    "ISSUE_REVERSAL_STATEMENT": "Issue a reversal statement; the original document is unchanged (EC-DOC-12)",
    "ALLOCATE_GAPLESS_NUMBER": "Allocate the gapless number in the issuing transaction (EC-DOC-08)",
    "ISSUE_CREDIT_NOTE": "Issue a credit note; the invoice and its number stay (EC-DOC-09)",
    "CREATE_CLAWBACK": "Create a negative TASK_EARNING adjustment in the next run (EC-TSK-08)",
    "NEW_SCHEDULE": "Replace the instalment schedule",
    "POST_TRANSACTION": "Post a loan ledger transaction",
    "CLOSE_PREVIOUS_ASSIGNMENT": "Close the previous assignment the day before",
    "PROPOSE_ARREARS": "Propose arrears for finalized periods reached by a backdated change (EC-CMP-12)",
    "MARK_LOCKED_RUNS_STALE": "Mark runs with locked, unfinalized inputs stale (EC-CFG-14, EC-CMP-13)",
    "BLOCK_RUNS": "Runs on the disabled version are blocked",
    "CREATE_AMENDMENT": "Create an amendment linked to the original report (EC-STA-12)",
    "CARRY_UNSETTLED_ITEMS": "Record the unsettled instructions as carried items for reissue or off-cycle payment",
})


# --------------------------------------------------------------------------- errors


class StateMachineError(ValueError):
    """Base of the state-machine errors; ``code`` is stable for API mapping."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class UnknownMachine(StateMachineError):
    def __init__(self, machine: str) -> None:
        super().__init__("UNKNOWN_MACHINE", f"no state machine named {machine!r}")
        self.machine = machine


class InvalidTransition(StateMachineError):
    """``action`` is not allowed from ``state``. ``code`` is ``INVALID_TRANSITION``, ``UNKNOWN_STATE``,
    ``UNKNOWN_ACTION`` or a machine's locked-state code such as ``RUN_FINALIZED``; all map to 409."""

    http_status = 409

    def __init__(self, machine: str, state: str, action: str, *, code: str = "INVALID_TRANSITION",
                 hint: str | None = None) -> None:
        super().__init__(code, f"{machine}: action {action!r} is not allowed from {state!r}")
        self.machine = machine
        self.state = state
        self.action = action
        self.hint = hint


class RunIdentityError(StateMachineError):
    """A run that may not be created (3.5 §3); maps to 409."""

    http_status = 409


# --------------------------------------------------------------------------- machine model


@dataclass(frozen=True)
class TransitionSpec:
    source: str
    action: str
    target: str
    permission: str | None
    reason_required: bool = False
    guards: tuple[str, ...] = ()
    effects: tuple[str, ...] = ()
    audit_event: str | None = None

    @property
    def is_system(self) -> bool:
        return self.permission is None


@dataclass(frozen=True)
class Machine:
    """One lifecycle: its states, where records start, side states shown as banners, and the
    transition table. ``locked`` maps a state to the error code used when an action is refused
    there (``hint`` tells the UI what to offer instead)."""

    name: str
    states: frozenset[str]
    initial_states: frozenset[str]
    side_states: frozenset[str]
    specs: tuple[TransitionSpec, ...]
    create_permission: str | None = None
    locked: Mapping[str, tuple[str, str | None]] = field(default_factory=dict)
    transitions: Mapping[tuple[str, str], tuple[str, str | None]] = field(init=False)
    _by_key: Mapping[tuple[str, str], TransitionSpec] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        by_key: dict[tuple[str, str], TransitionSpec] = {}
        for spec in self.specs:
            for state in (spec.source, spec.target):
                if state not in self.states:
                    raise ValueError(f"{self.name}: unknown state {state!r} in {spec}")
            key = (spec.source, spec.action)
            if key in by_key:
                raise ValueError(f"{self.name}: duplicate transition {key}")
            unknown = [g for g in spec.guards if g not in GUARDS] + [e for e in spec.effects if e not in EFFECTS]
            if unknown:
                raise ValueError(f"{self.name}: undeclared guard or effect {unknown} in {spec}")
            by_key[key] = spec
        if not self.initial_states <= self.states or not self.side_states <= self.states \
                or not set(self.locked) <= self.states:
            raise ValueError(f"{self.name}: initial, side or locked states outside the vocabulary")
        object.__setattr__(self, "_by_key", MappingProxyType(by_key))
        object.__setattr__(self, "transitions", MappingProxyType(
            {key: (spec.target, spec.permission) for key, spec in by_key.items()}))
        object.__setattr__(self, "locked", MappingProxyType(dict(self.locked)))

    @property
    def actions(self) -> frozenset[str]:
        return frozenset(action for _, action in self._by_key)

    @property
    def terminal_states(self) -> frozenset[str]:
        """States with no transition to another state (a self-loop such as REVERSE does not count)."""
        moving = {spec.source for spec in self.specs if spec.target != spec.source}
        return frozenset(self.states - moving)

    def spec(self, state: str, action: str) -> TransitionSpec:
        state, action = str(state), str(action)
        found = self._by_key.get((state, action))
        if found is not None:
            return found
        if state not in self.states:
            raise InvalidTransition(self.name, state, action, code="UNKNOWN_STATE")
        if action not in self.actions:
            raise InvalidTransition(self.name, state, action, code="UNKNOWN_ACTION")
        code, hint = self.locked.get(state, ("INVALID_TRANSITION", None))
        raise InvalidTransition(self.name, state, action, code=code, hint=hint)

    def next_state(self, state: str, action: str) -> str:
        return self.spec(state, action).target

    def allowed_actions(self, state: str) -> tuple[str, ...]:
        return tuple(sorted(action for source, action in self._by_key if source == state))

    def available(self, state: str, permissions: Iterable[str]) -> tuple[TransitionSpec, ...]:
        """User actions offered from ``state`` to someone holding ``permissions`` (system actions
        are never offered), sorted by action (3.8 §5.3 action bar)."""
        held = frozenset(permissions)
        return tuple(sorted((spec for (source, _), spec in self._by_key.items()
                             if source == state and spec.permission is not None and spec.permission in held),
                            key=lambda spec: spec.action))

    def actions_between(self, source: str, target: str) -> tuple[str, ...]:
        return tuple(sorted(spec.action for spec in self.specs if spec.source == source and spec.target == target))

    def reachable_from(self, state: str) -> frozenset[str]:
        seen, frontier = {state}, [state]
        while frontier:
            current = frontier.pop()
            for spec in self.specs:
                if spec.source == current and spec.target not in seen:
                    seen.add(spec.target)
                    frontier.append(spec.target)
        return frozenset(seen)


def _t(source: str, action: str, target: str, permission: str | None, *, reason: bool = False,
       guards: tuple[str, ...] = (), effects: tuple[str, ...] = (), audit: str | None = None) -> TransitionSpec:
    return TransitionSpec(str(source), action, str(target), permission, reason, guards, effects, audit)


def _each(sources: Iterable[str], action: str, target: str | None, permission: str | None, *,
          reason: bool = False, guards: tuple[str, ...] = (), effects: tuple[str, ...] = (),
          audit: str | None = None) -> list[TransitionSpec]:
    """One row per source; ``target=None`` means the status does not change (a self-loop)."""
    return [_t(source, action, source if target is None else target, permission, reason=reason, guards=guards,
               effects=effects, audit=audit) for source in sources]


def _machine(name: str, states: type[StrEnum], initial: Iterable[str], specs: Sequence[TransitionSpec], *,
             side: Iterable[str] = (), create_permission: str | None = None,
             locked: Mapping[str, tuple[str, str | None]] | None = None) -> Machine:
    return Machine(name=name, states=frozenset(s.value for s in states), initial_states=frozenset(map(str, initial)),
                   side_states=frozenset(map(str, side)), specs=tuple(specs), create_permission=create_permission,
                   locked=dict(locked or {}))


# --------------------------------------------------------------------------- pay run (3.5 §4.1)


class RunStatus(StrEnum):
    DRAFT = "DRAFT"
    INPUTS_PENDING = "INPUTS_PENDING"
    READY = "READY"
    CALCULATING = "CALCULATING"
    CALCULATED = "CALCULATED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REOPENED_FOR_CORRECTION = "REOPENED_FOR_CORRECTION"
    CANCELLED = "CANCELLED"
    FINALIZED = "FINALIZED"
    PAYMENT_READY = "PAYMENT_READY"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    RECONCILED = "RECONCILED"
    CLOSED = "CLOSED"


_R = RunStatus
RUN_PRE_FINALIZATION: tuple[str, ...] = tuple(s.value for s in (
    _R.DRAFT, _R.INPUTS_PENDING, _R.READY, _R.CALCULATING, _R.CALCULATED, _R.VALIDATION_FAILED, _R.REVIEW_REQUIRED,
    _R.APPROVAL_PENDING, _R.APPROVED, _R.REJECTED))
RUN_FINALIZED_STATES: tuple[str, ...] = tuple(s.value for s in (
    _R.FINALIZED, _R.PAYMENT_READY, _R.SUBMITTED, _R.PARTIALLY_PAID, _R.PAID, _R.RECONCILED, _R.CLOSED))
RUN_SIDE_STATES: tuple[str, ...] = (_R.CANCELLED.value, _R.REOPENED_FOR_CORRECTION.value)

_CALC_GUARDS = ("IDEMPOTENCY_KEY", "NO_CALCULATION_RUNNING")


def _stop_if_calculating(source: str, effects: tuple[str, ...]) -> tuple[str, ...]:
    return ("STOP_CALCULATION", *effects) if source == _R.CALCULATING else effects


_RUN_SPECS: list[TransitionSpec] = [
    _t(_R.DRAFT, "PRECHECK_BLOCKED", _R.INPUTS_PENDING, RUN_CREATE, guards=("BLOCKING_EXCEPTIONS_OPEN",),
       audit="PRECHECK_RUN"),
    _t(_R.DRAFT, "PRECHECK_PASSED", _R.READY, RUN_CREATE, guards=("NO_BLOCKING_EXCEPTIONS",), audit="PRECHECK_RUN"),
    _t(_R.INPUTS_PENDING, "PRECHECK_BLOCKED", _R.INPUTS_PENDING, RUN_CREATE, guards=("BLOCKING_EXCEPTIONS_OPEN",),
       audit="PRECHECK_RUN"),
    _t(_R.INPUTS_PENDING, "PRECHECK_PASSED", _R.READY, RUN_CREATE, guards=("NO_BLOCKING_EXCEPTIONS",),
       audit="PRECHECK_RUN"),
    _t(_R.READY, "CALCULATE", _R.CALCULATING, RUN_CALCULATE, guards=_CALC_GUARDS,
       effects=("LOCK_INPUTS", "ENQUEUE_CALCULATION"), audit="INPUTS_LOCKED"),
    _t(_R.CALCULATING, "COMPLETE_CALCULATION", _R.CALCULATED, None, audit="RUN_CALCULATED"),
    _t(_R.CALCULATED, "FAIL_VALIDATION", _R.VALIDATION_FAILED, None, guards=("BLOCKING_CHECK_FAILED",)),
    _t(_R.CALCULATED, "PASS_VALIDATION", _R.REVIEW_REQUIRED, None, guards=("NO_BLOCKING_CHECK_FAILED",)),
    *_each((_R.VALIDATION_FAILED, _R.REVIEW_REQUIRED, _R.REJECTED), "RECALCULATE", _R.CALCULATING, RUN_CALCULATE,
           guards=_CALC_GUARDS, effects=("RESNAPSHOT_SELECTED", "ENQUEUE_CALCULATION"), audit="INPUTS_LOCKED"),
    _t(_R.REVIEW_REQUIRED, "SUBMIT_FOR_APPROVAL", _R.APPROVAL_PENDING, RUN_REVIEW,
       guards=("NO_BLOCKING_EXCEPTIONS", "VARIANCE_FLAGS_ACKNOWLEDGED", "NOT_STALE"),
       effects=("RESOLVE_APPROVAL_CHAIN", "NOTIFY_APPROVERS"), audit="RUN_SUBMITTED"),
    _t(_R.APPROVAL_PENDING, "APPROVE_STEP", _R.APPROVAL_PENDING, RUN_APPROVE,
       guards=("SEPARATION_OF_DUTIES", "NO_SELF_PAY_APPROVAL", "NOT_LAST_STEP"), effects=("NOTIFY_APPROVERS",),
       audit="RUN_STEP_APPROVED"),
    _t(_R.APPROVAL_PENDING, "APPROVE_FINAL", _R.APPROVED, RUN_APPROVE,
       guards=("SEPARATION_OF_DUTIES", "NO_SELF_PAY_APPROVAL", "LAST_STEP"), audit="RUN_STEP_APPROVED"),
    _t(_R.APPROVAL_PENDING, "REJECT", _R.REJECTED, RUN_APPROVE, reason=True, audit="RUN_REJECTED"),
    _t(_R.REJECTED, "REWORK", _R.REVIEW_REQUIRED, RUN_CALCULATE),
    *[_t(source, "REOPEN", _R.REOPENED_FOR_CORRECTION, RUN_FINALIZE, reason=True,
         effects=_stop_if_calculating(source, ("CLEAR_APPROVALS",)), audit="RUN_REOPENED")
      for source in RUN_PRE_FINALIZATION],
    _t(_R.REOPENED_FOR_CORRECTION, "RESUME_REVIEW", _R.REVIEW_REQUIRED, RUN_FINALIZE,
       guards=("CALCULATION_RESULTS_EXIST",)),
    _t(_R.REOPENED_FOR_CORRECTION, "RETURN_TO_DRAFT", _R.DRAFT, RUN_FINALIZE, effects=("RELEASE_INPUTS",)),
    *[_t(source, "CANCEL", _R.CANCELLED, RUN_FINALIZE, reason=True,
         effects=_stop_if_calculating(source, ("RELEASE_INPUTS",)), audit="RUN_CANCELLED")
      for source in (*RUN_PRE_FINALIZATION, _R.REOPENED_FOR_CORRECTION)],
    _t(_R.APPROVED, "FINALIZE", _R.FINALIZED, RUN_FINALIZE,
       guards=("STEP_UP_MFA", "NOT_STALE", "STATUTORY_PACK_VERIFIED", "IDEMPOTENCY_KEY"),
       effects=("WRITE_YTD", "WRITE_LIABILITIES", "POST_SUBLEDGERS", "CONSUME_INPUTS", "CREATE_PAYMENT_INSTRUCTIONS",
                "ENQUEUE_DOCUMENTS", "APPLY_PUBLICATION_RULE", "CLOSE_PERIOD_IF_REGULAR"),
       audit="RUN_FINALIZED"),
    _t(_R.FINALIZED, "APPROVE_PAYMENTS", _R.PAYMENT_READY, PAYMENT_APPROVE, guards=("PAYMENT_BATCH_APPROVED",)),
    _t(_R.FINALIZED, "CLOSE", _R.CLOSED, RUN_CLOSE, guards=("NOTHING_TO_PAY",), effects=("CARRY_HOLDS",),
       audit="RUN_CLOSED"),
    _t(_R.PAYMENT_READY, "SUBMIT_PAYMENTS", _R.SUBMITTED, PAYMENT_SUBMIT, guards=("DUAL_CONTROL",)),
    _t(_R.SUBMITTED, "RECORD_PARTIAL_SETTLEMENT", _R.PARTIALLY_PAID, None, guards=("SETTLEMENT_EVIDENCE",)),
    *_each((_R.SUBMITTED, _R.PARTIALLY_PAID), "RECORD_FULL_SETTLEMENT", _R.PAID, None,
           guards=("SETTLEMENT_EVIDENCE",)),
    *_each((_R.PAID, _R.RECONCILED), "RECORD_RETURN", _R.PARTIALLY_PAID, None, guards=("RETURN_EVIDENCE",)),
    _t(_R.PAID, "RECONCILE", _R.RECONCILED, PAYMENT_RECONCILE, guards=("SETTLED_INSTRUCTIONS_MATCHED",)),
    _t(_R.PARTIALLY_PAID, "RECONCILE", _R.RECONCILED, PAYMENT_RECONCILE,
       guards=("SETTLED_INSTRUCTIONS_MATCHED", "UNSETTLED_ITEMS_CARRIED"), effects=("CARRY_UNSETTLED_ITEMS",),
       audit="RUN_RECONCILED_WITH_CARRIED_ITEMS"),
    _t(_R.RECONCILED, "CLOSE", _R.CLOSED, RUN_CLOSE, guards=("NO_OPEN_ITEMS_EXCEPT_HOLDS",),
       effects=("CARRY_HOLDS",), audit="RUN_CLOSED"),
    *_each(RUN_FINALIZED_STATES, "REVERSE", None, RUN_REVERSE, reason=True, guards=("TWO_APPROVERS",),
           effects=("CREATE_REVERSAL_RUN",), audit="RUN_REVERSED"),
]

RUN = _machine("RUN", RunStatus, (_R.DRAFT,), _RUN_SPECS, side=RUN_SIDE_STATES, create_permission=RUN_CREATE,
               locked={**{s: ("RUN_FINALIZED", "CREATE_CORRECTION") for s in RUN_FINALIZED_STATES},
                       _R.CANCELLED.value: ("RUN_CANCELLED", None)})
RUN_CREATE_GUARDS = ("PERIOD_OPEN", "UNIQUE_REGULAR_RUN", "PAY_GROUP_ACTIVE")

UI_STAGE_LABELS: Mapping[int, str] = MappingProxyType(
    {1: "Prepare", 2: "Calculate", 3: "Approve", 4: "Finalize", 5: "Pay", 6: "Close"})
RUN_UI_STAGES: Mapping[str, int] = MappingProxyType({
    **dict.fromkeys(("DRAFT", "INPUTS_PENDING", "READY"), 1),
    **dict.fromkeys(("CALCULATING", "CALCULATED", "VALIDATION_FAILED", "REVIEW_REQUIRED"), 2),
    **dict.fromkeys(("APPROVAL_PENDING", "REJECTED", "APPROVED"), 3),
    "FINALIZED": 4,
    **dict.fromkeys(("PAYMENT_READY", "SUBMITTED", "PARTIALLY_PAID", "PAID"), 5),
    **dict.fromkeys(("RECONCILED", "CLOSED"), 6),
})


def ui_stage(run_state: str) -> int | None:
    """The friendly stage 1-6 of a run state (3.5 §4.2); ``None`` for the side states CANCELLED and
    REOPENED_FOR_CORRECTION, which the UI shows as banners."""
    state = str(run_state)
    if state in RUN_SIDE_STATES:
        return None
    if state not in RUN_UI_STAGES:
        raise InvalidTransition("RUN", state, "ui_stage", code="UNKNOWN_STATE")
    return RUN_UI_STAGES[state]


def is_run_finalized(state: str) -> bool:
    return str(state) in RUN_FINALIZED_STATES


def ensure_run_editable(state: str) -> None:
    """Refuse edits to a run that can no longer change: finalized and later (``RUN_FINALIZED``,
    with the hint to create a correction, EC-RUN-17) or cancelled (``RUN_CANCELLED``). Other states
    may still be blocked by service guards (a running calculation, approval in progress)."""
    state = str(state)
    if state not in RUN.states:
        raise InvalidTransition("RUN", state, "EDIT", code="UNKNOWN_STATE")
    if state in RUN.locked:
        code, hint = RUN.locked[state]
        raise InvalidTransition("RUN", state, "EDIT", code=code, hint=hint)


# --------------------------------------------------------------------------- payment instructions (3.7 §8.1)


class InstructionStatus(StrEnum):
    NOT_READY = "NOT_READY"
    READY = "READY"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    APPROVED = "APPROVED"
    QUEUED = "QUEUED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PROCESSING = "PROCESSING"
    PARTIALLY_SETTLED = "PARTIALLY_SETTLED"
    SETTLED = "SETTLED"
    RECONCILED = "RECONCILED"
    ON_HOLD = "ON_HOLD"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    RETURNED = "RETURNED"
    REVERSED = "REVERSED"
    REISSUED = "REISSUED"


_I = InstructionStatus
INSTRUCTION_RANK: Mapping[str, int] = MappingProxyType({s.value: rank for rank, s in enumerate((
    _I.NOT_READY, _I.READY, _I.APPROVAL_PENDING, _I.APPROVED, _I.QUEUED, _I.SUBMITTED, _I.ACKNOWLEDGED,
    _I.PROCESSING, _I.PARTIALLY_SETTLED, _I.SETTLED, _I.RECONCILED))})
INSTRUCTION_SIDE_STATES: tuple[str, ...] = tuple(s.value for s in (
    _I.ON_HOLD, _I.CANCELLED, _I.REJECTED, _I.FAILED, _I.RETURNED, _I.REVERSED, _I.REISSUED))
EVENT_STATUSES: frozenset[str] = frozenset(s.value for s in (
    _I.SUBMITTED, _I.ACKNOWLEDGED, _I.PROCESSING, _I.PARTIALLY_SETTLED, _I.SETTLED, _I.REJECTED, _I.FAILED,
    _I.RETURNED, _I.REVERSED))
_UNSENT = (_I.NOT_READY, _I.READY, _I.APPROVAL_PENDING, _I.APPROVED)
_IN_FLIGHT = (_I.SUBMITTED, _I.ACKNOWLEDGED, _I.PROCESSING)
_SETTLED = (_I.PARTIALLY_SETTLED, _I.SETTLED, _I.RECONCILED)
_FAILURES = (_I.REJECTED, _I.FAILED)
_RETURNS = (_I.RETURNED, _I.REVERSED)


def event_action(event_status: str) -> str:
    """The system action that records evidence of ``event_status`` on an instruction."""
    return f"RECORD_{event_status}"


def _evidence_edges() -> list[TransitionSpec]:
    edges: list[TransitionSpec] = [_t(_I.QUEUED, event_action(_I.SUBMITTED), _I.SUBMITTED, None,
                                      guards=("PROVIDER_ACCEPTED",))]
    progress = (_I.ACKNOWLEDGED, _I.PROCESSING, _I.PARTIALLY_SETTLED, _I.SETTLED)
    for source in _IN_FLIGHT:
        later = [s for s in progress if INSTRUCTION_RANK[s] > INSTRUCTION_RANK[source]]
        for target in (*later, *_FAILURES, *_RETURNS):
            guards = ("SETTLEMENT_EVIDENCE",) if target in _SETTLED else ()
            edges.append(_t(source, event_action(target), target, None, guards=guards))
    edges.append(_t(_I.PARTIALLY_SETTLED, event_action(_I.SETTLED), _I.SETTLED, None, guards=("SETTLEMENT_EVIDENCE",)))
    for source in _SETTLED:
        edges.extend(_t(source, event_action(target), target, None, guards=("RETURN_EVIDENCE",)) for target in _RETURNS)
    return edges


_INSTRUCTION_SPECS: list[TransitionSpec] = [
    _t(_I.NOT_READY, "MARK_READY", _I.READY, None, guards=("BANK_ACCOUNT_USABLE",)),
    *_each(_UNSENT, "HOLD", _I.ON_HOLD, None, reason=True),
    _t(_I.ON_HOLD, "RELEASE_HOLD", _I.NOT_READY, PAYMENT_APPROVE, reason=True),
    _t(_I.READY, "SUBMIT_FOR_APPROVAL", _I.APPROVAL_PENDING, PAYMENT_PREPARE),
    _t(_I.APPROVAL_PENDING, "APPROVE", _I.APPROVED, PAYMENT_APPROVE, guards=("DUAL_CONTROL",)),
    *_each((_I.APPROVAL_PENDING, _I.APPROVED, _I.QUEUED), "RELEASE_FROM_BATCH", _I.READY, None),
    _t(_I.APPROVED, "QUEUE", _I.QUEUED, None),
    _t(_I.QUEUED, "SUBMIT", _I.SUBMITTED, PAYMENT_SUBMIT,
       guards=("DUAL_CONTROL", "STEP_UP_MFA", "FUNDING_SIGNATORY", "FILE_HASH_UNCHANGED")),
    *_evidence_edges(),
    *_each((*_IN_FLIGHT, _I.PARTIALLY_SETTLED), "MANUAL_SETTLE", _I.SETTLED, PAYMENT_RECONCILE,
           guards=("EVIDENCE_UPLOADED", "SECOND_REVIEWER"), audit="MANUAL_SETTLEMENT"),
    _t(_I.PARTIALLY_SETTLED, "ACCEPT_VARIANCE", _I.SETTLED, PAYMENT_RECONCILE, reason=True,
       audit="MISMATCH_RESOLVED"),
    _t(_I.SETTLED, "RECONCILE", _I.RECONCILED, PAYMENT_RECONCILE, guards=("STATEMENT_MATCHED",)),
    *_each((*_UNSENT, _I.ON_HOLD), "CANCEL", _I.CANCELLED, PAYMENT_PREPARE, reason=True),
    *_each((*_UNSENT, _I.ON_HOLD), "CANCEL_FOR_REVERSAL", _I.CANCELLED, None, guards=("RUN_REVERSAL_AUTHORIZED",)),
    *_each((*_FAILURES, *_RETURNS), "REISSUE", _I.REISSUED, PAYMENT_PREPARE,
           guards=("REISSUE_APPROVAL", "BANK_ACCOUNT_USABLE"), effects=("CREATE_REPLACEMENT_INSTRUCTION",),
           audit="PAYMENT_REISSUED"),
]

PAYMENT_INSTRUCTION = _machine("PAYMENT_INSTRUCTION", InstructionStatus, (_I.NOT_READY, _I.READY),
                               _INSTRUCTION_SPECS, side=INSTRUCTION_SIDE_STATES)


@dataclass(frozen=True)
class EventOutcome:
    """Result of applying one piece of payment evidence.

    ``outcome`` is APPLIED (``status`` is new; pass ``action`` to ``next_state``), DUPLICATE or STALE
    (older evidence, ignored) or CONFLICT (contradicts the current status: kept for review, status
    unchanged)."""

    status: str
    outcome: str
    action: str | None = None
    reason: str | None = None

    @property
    def applied(self) -> bool:
        return self.outcome == "APPLIED"


def apply_event(current: str, event_status: str) -> EventOutcome:
    """Apply evidence that an instruction reached ``event_status`` (webhook, statement, bank report).

    Forward jumps are taken (SETTLED may arrive before ACKNOWLEDGED); older events never move a
    status backwards (EC-PAY-07). Evidence for an instruction that was never sent, a failure after
    settlement, or a settlement after a failure or reissue is a CONFLICT for review: the status stays.
    """
    current, event = str(current), str(event_status)
    if current not in PAYMENT_INSTRUCTION.states:
        raise InvalidTransition("PAYMENT_INSTRUCTION", current, event_action(event), code="UNKNOWN_STATE")
    if event not in EVENT_STATUSES:
        raise InvalidTransition("PAYMENT_INSTRUCTION", current, event_action(event), code="NOT_AN_EVENT_STATUS")
    if event == current:
        return EventOutcome(current, "DUPLICATE")
    if current in (*_UNSENT, _I.ON_HOLD, _I.CANCELLED):
        return EventOutcome(current, "CONFLICT", reason="NOT_SUBMITTED")
    if current == _I.QUEUED:
        if event == _I.SUBMITTED:
            return EventOutcome(event, "APPLIED", event_action(event))
        return EventOutcome(current, "CONFLICT", reason="NOT_SUBMITTED")
    settles = event in (_I.PARTIALLY_SETTLED, _I.SETTLED)
    if current == _I.REISSUED:
        if settles:
            return EventOutcome(current, "CONFLICT", reason="SETTLED_AFTER_REISSUE")
        return EventOutcome(current, "STALE")
    if current in _FAILURES:
        if settles:
            return EventOutcome(current, "CONFLICT", reason="SETTLED_AFTER_FAILURE")
        return EventOutcome(current, "STALE")
    if current in _RETURNS:
        return EventOutcome(current, "STALE")
    if event in _FAILURES:
        if current in _SETTLED:
            return EventOutcome(current, "CONFLICT", reason="FAILED_AFTER_SETTLEMENT")
        return EventOutcome(event, "APPLIED", event_action(event))
    if event in _RETURNS:
        return EventOutcome(event, "APPLIED", event_action(event))
    if INSTRUCTION_RANK[event] > INSTRUCTION_RANK[current]:
        return EventOutcome(event, "APPLIED", event_action(event))
    return EventOutcome(current, "STALE")


class SubmissionChannel(StrEnum):
    """How an instruction reached the bank (decision I-17)."""

    MANUAL_UPLOAD = "MANUAL_UPLOAD"
    API = "API"
    SANDBOX = "SANDBOX"
    MANUAL_OFFLINE = "MANUAL_OFFLINE"


class EvidenceStatus(StrEnum):
    """Whether the payment is backed by verified evidence: a statement match, a signed provider
    event or reviewed manual proof (contract §23.2, decision I-17)."""

    UNVERIFIED = "UNVERIFIED"
    VERIFIED = "VERIFIED"


MANUAL_CHANNELS: frozenset[str] = frozenset({SubmissionChannel.MANUAL_UPLOAD.value,
                                             SubmissionChannel.MANUAL_OFFLINE.value})


@dataclass(frozen=True)
class PaymentLabel:
    """What an employee sees for an instruction status (3.7 §8.1); ``text`` may contain {date} and
    {amount} placeholders. The UI never says "Paid" before SETTLED (contract §23.2). ``badge`` is
    ``"SANDBOX"`` for sandbox instructions, which are labelled as such everywhere (3.7 §7.2)."""

    key: str
    text: str
    badge: str | None = None


EMPLOYEE_LABEL_TEXT: Mapping[str, str] = MappingProxyType({
    "SCHEDULED": "Scheduled",
    "PROCESSING": "Processing",
    "PAID": "Paid on {date}",
    "PARTLY_PAID": "Partly paid — {amount} received",
    "ON_HOLD": "On hold — HR will contact you",
    "FAILED": "Payment failed — please check your bank details",
    "SENT_AGAIN": "Sent again",
    "REVERSED": "Reversed",
    "CANCELLED": "Cancelled",
    "SUBMITTED_MANUAL_UNVERIFIED": "Submitted for manual payment — unverified",
})
INSTRUCTION_EMPLOYEE_LABELS: Mapping[str, str] = MappingProxyType({
    **dict.fromkeys(("NOT_READY", "READY", "APPROVAL_PENDING", "APPROVED", "QUEUED"), "SCHEDULED"),
    **dict.fromkeys(("SUBMITTED", "ACKNOWLEDGED", "PROCESSING"), "PROCESSING"),
    **dict.fromkeys(("SETTLED", "RECONCILED"), "PAID"),
    "PARTIALLY_SETTLED": "PARTLY_PAID",
    "ON_HOLD": "ON_HOLD",
    **dict.fromkeys(("FAILED", "REJECTED", "RETURNED"), "FAILED"),
    "REISSUED": "SENT_AGAIN",
    "REVERSED": "REVERSED",
    "CANCELLED": "CANCELLED",
})


def employee_payment_label(status: str) -> PaymentLabel:
    status = str(status)
    if status not in INSTRUCTION_EMPLOYEE_LABELS:
        raise InvalidTransition("PAYMENT_INSTRUCTION", status, "label", code="UNKNOWN_STATE")
    key = INSTRUCTION_EMPLOYEE_LABELS[status]
    return PaymentLabel(key, EMPLOYEE_LABEL_TEXT[key])


_PAID_LABELS = frozenset({"PAID", "PARTLY_PAID"})


def employee_label(status: str, channel: SubmissionChannel | str | None = None,
                   evidence: EvidenceStatus | str = EvidenceStatus.UNVERIFIED) -> PaymentLabel:
    """The employee label of an instruction given how it was submitted and its evidence (I-17).

    A submitted, acknowledged or processing instruction sent through a manual channel without
    verified evidence reads "Submitted for manual payment — unverified". A paid label needs
    verified evidence: without it a settled status fails closed to the in-flight label. Statuses
    before submission and the side states keep their 3.7 §8.1 labels. ``channel`` is ``None``
    only before submission.
    """
    base = employee_payment_label(status)
    try:
        chan = None if channel is None else SubmissionChannel(str(channel))
        proof = EvidenceStatus(str(evidence))
    except ValueError:
        raise InvalidTransition("PAYMENT_INSTRUCTION", str(status), "label", code="INVALID_LABEL_INPUT") from None
    status = str(status)
    if chan is None and (status in _IN_FLIGHT or status in _SETTLED):
        raise InvalidTransition("PAYMENT_INSTRUCTION", status, "label", code="SUBMISSION_CHANNEL_REQUIRED")
    badge = "SANDBOX" if chan is SubmissionChannel.SANDBOX else None
    key = base.key
    unverified = proof is EvidenceStatus.UNVERIFIED
    manual = chan is not None and chan.value in MANUAL_CHANNELS
    if unverified and (status in _IN_FLIGHT or key in _PAID_LABELS):
        key = "SUBMITTED_MANUAL_UNVERIFIED" if manual else "PROCESSING"
    return PaymentLabel(key, EMPLOYEE_LABEL_TEXT[key], badge)


# --------------------------------------------------------------------------- payment batches (3.7 §8.2)


class BatchStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    APPROVED = "APPROVED"
    FILE_GENERATED = "FILE_GENERATED"
    API_QUEUED = "API_QUEUED"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_SETTLED = "PARTIALLY_SETTLED"
    SETTLED = "SETTLED"
    RECONCILED = "RECONCILED"
    CANCELLED = "CANCELLED"


_B = BatchStatus
_RELEASE_GUARDS = ("DUAL_CONTROL", "STEP_UP_MFA", "FUNDING_SIGNATORY")
BATCH_SUBMITTED_STATES: tuple[str, ...] = tuple(s.value for s in (
    _B.SUBMITTED, _B.PARTIALLY_SETTLED, _B.SETTLED, _B.RECONCILED))

PAYMENT_BATCH = _machine("PAYMENT_BATCH", BatchStatus, (_B.DRAFT,), [
    _t(_B.DRAFT, "SUBMIT_FOR_APPROVAL", _B.APPROVAL_PENDING, PAYMENT_PREPARE, audit="BATCH_CREATED"),
    _t(_B.APPROVAL_PENDING, "APPROVE", _B.APPROVED, PAYMENT_APPROVE, guards=("DUAL_CONTROL",),
       audit="BATCH_APPROVED"),
    _t(_B.APPROVAL_PENDING, "REJECT", _B.CANCELLED, PAYMENT_APPROVE, reason=True, audit="BATCH_CANCELLED"),
    _t(_B.APPROVED, "GENERATE_FILE", _B.FILE_GENERATED, PAYMENT_PREPARE, effects=("STORE_FILE_HASH",),
       audit="PAYMENT_FILE_GENERATED"),
    _t(_B.APPROVED, "QUEUE_API", _B.API_QUEUED, PAYMENT_SUBMIT, guards=_RELEASE_GUARDS, audit="BATCH_RELEASED"),
    _t(_B.FILE_GENERATED, "MARK_SUBMITTED", _B.SUBMITTED, PAYMENT_SUBMIT,
       guards=(*_RELEASE_GUARDS, "FILE_HASH_UNCHANGED"), audit="BATCH_RELEASED"),
    _t(_B.API_QUEUED, "RECORD_SUBMITTED", _B.SUBMITTED, None, guards=("PROVIDER_ACCEPTED",)),
    _t(_B.SUBMITTED, "RECORD_PARTIAL_SETTLEMENT", _B.PARTIALLY_SETTLED, None, guards=("SETTLEMENT_EVIDENCE",)),
    *_each((_B.SUBMITTED, _B.PARTIALLY_SETTLED), "RECORD_SETTLEMENT", _B.SETTLED, None,
           guards=("SETTLEMENT_EVIDENCE",)),
    *_each((_B.PARTIALLY_SETTLED, _B.SETTLED), "RECONCILE", _B.RECONCILED, PAYMENT_RECONCILE,
           guards=("SETTLED_INSTRUCTIONS_MATCHED",)),
    *_each((_B.DRAFT, _B.APPROVAL_PENDING, _B.APPROVED, _B.FILE_GENERATED, _B.API_QUEUED), "CANCEL", _B.CANCELLED,
           PAYMENT_PREPARE, reason=True, audit="BATCH_CANCELLED"),
], locked={**{s: ("BATCH_SUBMITTED", None) for s in BATCH_SUBMITTED_STATES},
           _B.CANCELLED.value: ("BATCH_CANCELLED", None)})


# --------------------------------------------------------------------------- settlement derivation (3.5 §5.9)

_EXCLUDED_FROM_SETTLEMENT = frozenset({_I.ON_HOLD.value, _I.CANCELLED.value, _I.REISSUED.value})
_FULLY_SETTLED = frozenset({_I.SETTLED.value, _I.RECONCILED.value})
_SOME_SETTLED = _FULLY_SETTLED | {_I.PARTIALLY_SETTLED.value}


def settlement_level(instruction_statuses: Iterable[str]) -> str:
    """FULL when every instruction that is not held, cancelled or replaced by a reissue is SETTLED or
    RECONCILED; PARTIAL when some are (even partly); NONE when none are; NOTHING_TO_PAY when no
    instruction counts, which is never reported as paid."""
    counted = [str(s) for s in instruction_statuses if str(s) not in _EXCLUDED_FROM_SETTLEMENT]
    for status in counted:
        if status not in PAYMENT_INSTRUCTION.states:
            raise InvalidTransition("PAYMENT_INSTRUCTION", status, "settlement_level", code="UNKNOWN_STATE")
    if not counted:
        return "NOTHING_TO_PAY"
    if all(s in _FULLY_SETTLED for s in counted):
        return "FULL"
    if any(s in _SOME_SETTLED for s in counted):
        return "PARTIAL"
    return "NONE"


_RUN_SETTLEMENT: Mapping[tuple[str, str], str] = MappingProxyType({
    ("SUBMITTED", "PARTIAL"): "RECORD_PARTIAL_SETTLEMENT",
    ("SUBMITTED", "FULL"): "RECORD_FULL_SETTLEMENT",
    ("PARTIALLY_PAID", "FULL"): "RECORD_FULL_SETTLEMENT",
    ("PAID", "PARTIAL"): "RECORD_RETURN",
    ("PAID", "NONE"): "RECORD_RETURN",
    ("RECONCILED", "PARTIAL"): "RECORD_RETURN",
    ("RECONCILED", "NONE"): "RECORD_RETURN",
})
_BATCH_SETTLEMENT: Mapping[tuple[str, str], str] = MappingProxyType({
    ("SUBMITTED", "PARTIAL"): "RECORD_PARTIAL_SETTLEMENT",
    ("SUBMITTED", "FULL"): "RECORD_SETTLEMENT",
    ("PARTIALLY_SETTLED", "FULL"): "RECORD_SETTLEMENT",
})


def run_settlement_action(current: str, instruction_statuses: Iterable[str]) -> str | None:
    """The system RUN action that brings a submitted run in line with its instructions, or None:
    PARTIALLY_PAID when some are settled, PAID when all are, back to PARTIALLY_PAID when a paid run's
    payment comes back. Approving and releasing batches move the run in the batch actions."""
    return _RUN_SETTLEMENT.get((str(current), settlement_level(instruction_statuses)))


def batch_settlement_action(current: str, instruction_statuses: Iterable[str]) -> str | None:
    """The system PAYMENT_BATCH action implied by the batch's instructions, or None."""
    return _BATCH_SETTLEMENT.get((str(current), settlement_level(instruction_statuses)))


# --------------------------------------------------------------------------- bank accounts (3.7 §3.3, §4)


class BankAccountStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


_BA = BankAccountStatus
_BANK_CHANGE_GUARDS = ("STEP_UP_MFA", "VELOCITY_LIMIT")
BANK_ACCOUNT = _machine("BANK_ACCOUNT", BankAccountStatus, (_BA.DRAFT,), [
    _t(_BA.DRAFT, "SUBMIT", _BA.PENDING_APPROVAL, BANK_DETAILS_EDIT, guards=_BANK_CHANGE_GUARDS,
       audit="BANK_ACCOUNT_SUBMITTED"),
    _t(_BA.DRAFT, "ACTIVATE", _BA.ACTIVE, BANK_DETAILS_EDIT, guards=(*_BANK_CHANGE_GUARDS, "CHANGE_APPROVAL_OFF"),
       effects=("START_COOLING_OFF", "NOTIFY_OLD_AND_NEW_CONTACTS"), audit="BANK_ACCOUNT_SUBMITTED"),
    _t(_BA.PENDING_APPROVAL, "APPROVE", _BA.ACTIVE, BANK_DETAILS_APPROVE, guards=("MAKER_CHECKER",),
       effects=("START_COOLING_OFF", "NOTIFY_OLD_AND_NEW_CONTACTS"), audit="BANK_ACCOUNT_APPROVED"),
    _t(_BA.PENDING_APPROVAL, "REJECT", _BA.DRAFT, BANK_DETAILS_APPROVE, reason=True, audit="BANK_ACCOUNT_REJECTED"),
    _t(_BA.ACTIVE, "DEACTIVATE", _BA.INACTIVE, BANK_DETAILS_EDIT, reason=True),
    _t(_BA.ACTIVE, "REPLACE", _BA.INACTIVE, None, guards=("NEWER_VERSION",)),
], create_permission=BANK_DETAILS_EDIT)


class VerificationStatus(StrEnum):
    NOT_SUBMITTED = "NOT_SUBMITTED"
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"


_V = VerificationStatus
BANK_VERIFICATION = _machine("BANK_VERIFICATION", VerificationStatus, (_V.NOT_SUBMITTED,), [
    _t(_V.NOT_SUBMITTED, "SUBMIT", _V.PENDING, BANK_DETAILS_EDIT),
    _t(_V.PENDING, "RECORD_VERIFIED", _V.VERIFIED, None, guards=("REQUIRED_METHODS_PASSED",), audit="BANK_VERIFIED"),
    _t(_V.PENDING, "MARK_VERIFIED", _V.VERIFIED, BANK_DETAILS_APPROVE,
       guards=("REQUIRED_METHODS_PASSED", "MAKER_CHECKER"), audit="BANK_VERIFIED"),
    _t(_V.PENDING, "RECORD_FAILED", _V.FAILED, None, audit="BANK_FAILED"),
    _t(_V.PENDING, "MARK_FAILED", _V.FAILED, BANK_DETAILS_APPROVE, reason=True, audit="BANK_FAILED"),
    *_each((_V.PENDING, _V.VERIFIED), "FLAG_FOR_REVIEW", _V.REQUIRES_REVIEW, None, guards=("RISK_FLAG",)),
    _t(_V.REQUIRES_REVIEW, "MARK_VERIFIED", _V.VERIFIED, BANK_DETAILS_APPROVE, reason=True,
       guards=("REQUIRED_METHODS_PASSED", "MAKER_CHECKER"), audit="BANK_VERIFIED"),
    _t(_V.REQUIRES_REVIEW, "MARK_FAILED", _V.FAILED, BANK_DETAILS_APPROVE, reason=True, audit="BANK_FAILED"),
    _t(_V.REQUIRES_REVIEW, "CLEAR_REVIEW", _V.PENDING, BANK_DETAILS_APPROVE, reason=True),
    *_each((_V.PENDING, _V.VERIFIED), "RECORD_PAYMENT_FAILURE", _V.FAILED, None,
           guards=("PAYMENT_FAILED_OR_RETURNED",), audit="BANK_FAILED"),
    _t(_V.VERIFIED, "EXPIRE", _V.EXPIRED, None, guards=("VERIFICATION_EXPIRED",)),
    *_each((_V.FAILED, _V.EXPIRED), "RESUBMIT", _V.PENDING, BANK_DETAILS_EDIT),
], side=(_V.REQUIRES_REVIEW,))


# --------------------------------------------------------------------------- documents and invoices (3.6)


class DocumentStatus(StrEnum):
    GENERATED = "GENERATED"
    PUBLISHED = "PUBLISHED"
    VIEWED = "VIEWED"
    EMAIL_SENT = "EMAIL_SENT"
    EMAIL_FAILED = "EMAIL_FAILED"
    SUPERSEDED = "SUPERSEDED"
    REVERSED = "REVERSED"


_D = DocumentStatus
_PUBLISHED = (_D.PUBLISHED, _D.EMAIL_SENT, _D.EMAIL_FAILED, _D.VIEWED)
DOCUMENT = _machine("DOCUMENT", DocumentStatus, (_D.GENERATED,), [
    _t(_D.GENERATED, "PUBLISH", _D.PUBLISHED, None, guards=("PUBLISH_TRIGGER_MET", "RUN_NOT_REVERSED"),
       audit="PAYSLIP_PUBLISHED"),
    _t(_D.GENERATED, "PUBLISH_MANUALLY", _D.PUBLISHED, PAYSLIP_PUBLISH, reason=True, guards=("RUN_NOT_REVERSED",),
       audit="PAYSLIP_MANUAL_PUBLISH"),
    *_each((_D.PUBLISHED, _D.EMAIL_FAILED), "RECORD_EMAIL_SENT", _D.EMAIL_SENT, None,
           guards=("ONE_NOTIFICATION_PER_VERSION",)),
    *_each((_D.PUBLISHED, _D.EMAIL_SENT), "RECORD_EMAIL_FAILED", _D.EMAIL_FAILED, None),
    *_each((_D.PUBLISHED, _D.EMAIL_SENT, _D.EMAIL_FAILED), "RECORD_VIEW", _D.VIEWED, None, audit="DOCUMENT_VIEWED"),
    *_each(_PUBLISHED, "SUPERSEDE", _D.SUPERSEDED, None, guards=("NEWER_VERSION",)),
    *_each((*_PUBLISHED, _D.SUPERSEDED), "REVERSE", _D.REVERSED, None, effects=("ISSUE_REVERSAL_STATEMENT",)),
], side=(_D.SUPERSEDED, _D.REVERSED))


def document_visible_to_employee(status: str) -> bool:
    """Employees see their own documents only once published (EC-DOC-02); every status after
    GENERATED implies publication."""
    status = str(status)
    if status not in DOCUMENT.states:
        raise InvalidTransition("DOCUMENT", status, "view", code="UNKNOWN_STATE")
    return status != _D.GENERATED


class InvoiceStatus(StrEnum):
    DRAFT = "DRAFT"
    ISSUED = "ISSUED"
    APPROVED = "APPROVED"
    PAID = "PAID"
    CREDITED = "CREDITED"


_IV = InvoiceStatus
INVOICE = _machine("INVOICE", InvoiceStatus, (_IV.DRAFT,), [
    _t(_IV.DRAFT, "ISSUE", _IV.ISSUED, PAYMENT_PREPARE, guards=("CONTRACTOR_PAYEE", "SELF_BILLING_AGREEMENT"),
       effects=("ALLOCATE_GAPLESS_NUMBER",)),
    _t(_IV.ISSUED, "APPROVE", _IV.APPROVED, PAYMENT_APPROVE, guards=("MAKER_CHECKER",)),
    _t(_IV.APPROVED, "RECORD_PAYMENT", _IV.PAID, None, guards=("SETTLEMENT_EVIDENCE",)),
    *_each((_IV.ISSUED, _IV.APPROVED, _IV.PAID), "CREDIT", _IV.CREDITED, PAYMENT_APPROVE, reason=True,
           effects=("ISSUE_CREDIT_NOTE",)),
])


# --------------------------------------------------------------------------- tasks, wage inputs, claims, loans (3.4)


class TaskStatus(StrEnum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DISPUTED = "DISPUTED"
    PAID = "PAID"
    REVERSED = "REVERSED"


_T = TaskStatus
_TASK_APPROVAL = ("APPROVER_IN_CHAIN", "ACCEPTED_QUANTITY_WITHIN_SUBMITTED", "EVIDENCE_IF_REQUIRED")
TASK = _machine("TASK", TaskStatus, (_T.DRAFT,), [
    _t(_T.DRAFT, "SUBMIT", _T.SUBMITTED, TASK_MANAGE, guards=("SPLIT_TOTALS_100", "EVIDENCE_IF_REQUIRED"),
       audit="TASK_SUBMITTED"),
    *_each((_T.SUBMITTED, _T.DISPUTED), "APPROVE", _T.APPROVED, TASK_APPROVE, guards=_TASK_APPROVAL,
           audit="TASK_APPROVED"),
    *_each((_T.SUBMITTED, _T.DISPUTED), "REJECT", _T.REJECTED, TASK_APPROVE, reason=True, audit="TASK_REJECTED"),
    _t(_T.SUBMITTED, "DISPUTE", _T.DISPUTED, TASK_APPROVE, reason=True, audit="TASK_DISPUTED"),
    _t(_T.APPROVED, "MARK_PAID", _T.PAID, None, guards=("FINALIZED_RUN",)),
    _t(_T.PAID, "REVERSE", _T.REVERSED, TASK_APPROVE, reason=True, effects=("CREATE_CLAWBACK",),
       audit="TASK_REVERSED"),
], side=(_T.DISPUTED,), create_permission=TASK_MANAGE)


def task_is_payable(status: str) -> bool:
    """Only APPROVED tasks earn pay; rejected, disputed and unapproved tasks never do (EC-TSK-02)."""
    status = str(status)
    if status not in TASK.states:
        raise InvalidTransition("TASK", status, "pay", code="UNKNOWN_STATE")
    return status == _T.APPROVED


class WageInputStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    CONSUMED = "CONSUMED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


_W = WageInputStatus
WAGE_INPUT = _machine("WAGE_INPUT", WageInputStatus, (_W.DRAFT,), [
    _t(_W.DRAFT, "SUBMIT", _W.PENDING_APPROVAL, INPUTS_EDIT, guards=("PERIOD_ACCEPTS_INPUTS",),
       audit="WAGE_INPUT_SUBMITTED"),
    _t(_W.DRAFT, "AUTO_APPROVE", _W.APPROVED, INPUTS_EDIT, guards=("PERIOD_ACCEPTS_INPUTS", "BELOW_APPROVAL_THRESHOLD"),
       audit="WAGE_INPUT_APPROVED"),
    _t(_W.PENDING_APPROVAL, "APPROVE", _W.APPROVED, INPUTS_APPROVE, guards=("APPROVER_IN_CHAIN", "MAKER_CHECKER"),
       audit="WAGE_INPUT_APPROVED"),
    _t(_W.PENDING_APPROVAL, "REJECT", _W.REJECTED, INPUTS_APPROVE, reason=True, audit="WAGE_INPUT_REJECTED"),
    _t(_W.APPROVED, "CONSUME", _W.CONSUMED, None, guards=("FINALIZED_RUN",)),
    *_each((_W.DRAFT, _W.PENDING_APPROVAL, _W.APPROVED), "CANCEL", _W.CANCELLED, INPUTS_EDIT, reason=True,
           audit="WAGE_INPUT_CANCELLED"),
], create_permission=INPUTS_EDIT)


class ClaimStatus(StrEnum):
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    SCHEDULED = "SCHEDULED"
    PAID = "PAID"
    REJECTED = "REJECTED"


_C = ClaimStatus
CLAIM = _machine("CLAIM", ClaimStatus, (_C.SUBMITTED,), [
    _t(_C.SUBMITTED, "APPROVE", _C.APPROVED, REIMBURSEMENTS_APPROVE,
       guards=("APPROVER_IN_CHAIN", "WITHIN_POLICY_LIMITS", "RECEIPT_IF_REQUIRED")),
    _t(_C.SUBMITTED, "REJECT", _C.REJECTED, REIMBURSEMENTS_APPROVE, reason=True),
    _t(_C.APPROVED, "SCHEDULE", _C.SCHEDULED, None),
    _t(_C.SCHEDULED, "UNSCHEDULE", _C.APPROVED, None, effects=("RELEASE_INPUTS",)),
    _t(_C.SCHEDULED, "MARK_PAID", _C.PAID, None, guards=("SETTLEMENT_EVIDENCE",)),
], create_permission=REIMBURSEMENTS_MANAGE)


class LoanStatus(StrEnum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


_L = LoanStatus
_RUNNING_LOAN = (_L.ACTIVE, _L.PAUSED)
LOAN = _machine("LOAN", LoanStatus, (_L.PENDING_APPROVAL,), [
    _t(_L.PENDING_APPROVAL, "APPROVE", _L.APPROVED, LOANS_APPROVE,
       guards=("APPROVER_IN_CHAIN", "WITHIN_POLICY_LIMITS")),
    _t(_L.PENDING_APPROVAL, "REJECT", _L.REJECTED, LOANS_APPROVE, reason=True),
    _t(_L.APPROVED, "DISBURSE", _L.ACTIVE, None, guards=("DISBURSEMENT_RECORDED",), effects=("POST_TRANSACTION",)),
    _t(_L.APPROVED, "CANCEL", _L.CANCELLED, LOANS_MANAGE, reason=True),
    _t(_L.ACTIVE, "PAUSE", _L.PAUSED, LOANS_APPROVE, reason=True),
    _t(_L.PAUSED, "RESUME", _L.ACTIVE, LOANS_APPROVE),
    *_each(_RUNNING_LOAN, "RESTRUCTURE", None, LOANS_APPROVE, reason=True, effects=("NEW_SCHEDULE",)),
    *_each(_RUNNING_LOAN, "PREPAY", None, LOANS_APPROVE, effects=("POST_TRANSACTION",)),
    *_each(_RUNNING_LOAN, "WAIVE", None, LOANS_APPROVE, reason=True, effects=("POST_TRANSACTION",)),
    *_each(_RUNNING_LOAN, "CLOSE", _L.CLOSED, None, guards=("ZERO_BALANCE",)),
], create_permission=LOANS_MANAGE)


# --------------------------------------------------------------------------- compensation and configuration


class CompensationStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    SCHEDULED = "SCHEDULED"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"


_CA = CompensationStatus
_COMPENSATION_APPROVAL = ("APPROVER_IN_CHAIN", "MAKER_CHECKER", "NOT_OWN_SALARY")
COMPENSATION_ASSIGNMENT = _machine("COMPENSATION_ASSIGNMENT", CompensationStatus, (_CA.DRAFT,), [
    _t(_CA.DRAFT, "SUBMIT", _CA.PENDING_APPROVAL, COMPENSATION_EDIT, audit="COMPENSATION_SUBMITTED"),
    _t(_CA.PENDING_APPROVAL, "APPROVE_SCHEDULED", _CA.SCHEDULED, COMPENSATION_APPROVE,
       guards=(*_COMPENSATION_APPROVAL, "EFFECTIVE_IN_FUTURE"), audit="COMPENSATION_APPROVED"),
    _t(_CA.PENDING_APPROVAL, "APPROVE_ACTIVE", _CA.ACTIVE, COMPENSATION_APPROVE,
       guards=(*_COMPENSATION_APPROVAL, "EFFECTIVE_REACHED"),
       effects=("CLOSE_PREVIOUS_ASSIGNMENT", "PROPOSE_ARREARS", "MARK_LOCKED_RUNS_STALE"),
       audit="COMPENSATION_APPROVED"),
    _t(_CA.PENDING_APPROVAL, "REJECT", _CA.REJECTED, COMPENSATION_APPROVE, reason=True, audit="COMPENSATION_REJECTED"),
    _t(_CA.SCHEDULED, "ACTIVATE", _CA.ACTIVE, None, guards=("EFFECTIVE_REACHED",),
       effects=("CLOSE_PREVIOUS_ASSIGNMENT", "MARK_LOCKED_RUNS_STALE")),
    *_each((_CA.SCHEDULED, _CA.ACTIVE), "SUPERSEDE", _CA.SUPERSEDED, None, guards=("NEWER_VERSION",)),
], create_permission=COMPENSATION_EDIT)


class SettingValueStatus(StrEnum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"


_S = SettingValueStatus
SETTING_VALUE = _machine("SETTING_VALUE", SettingValueStatus, (_S.PENDING_APPROVAL, _S.ACTIVE), [
    _t(_S.PENDING_APPROVAL, "APPROVE", _S.ACTIVE, SETTINGS_APPROVE, guards=("MAKER_CHECKER",),
       effects=("MARK_LOCKED_RUNS_STALE",), audit="SETTING_CHANGE_APPROVED"),
    _t(_S.PENDING_APPROVAL, "REJECT", _S.REJECTED, SETTINGS_APPROVE, reason=True, audit="SETTING_CHANGE_REJECTED"),
    _t(_S.ACTIVE, "SUPERSEDE", _S.SUPERSEDED, None, guards=("NEWER_VERSION",)),
], create_permission=CONFIGURATION_WRITE)


class PolicyVersionStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"


_P = PolicyVersionStatus
POLICY_VERSION = _machine("POLICY_VERSION", PolicyVersionStatus, (_P.DRAFT,), [
    _t(_P.DRAFT, "ACTIVATE", _P.ACTIVE, CONFIGURATION_WRITE, guards=("VALID_SCHEMA", "IMPACT_PREVIEW_DONE"),
       effects=("MARK_LOCKED_RUNS_STALE",), audit="POLICY_VERSION_ACTIVATED"),
    _t(_P.ACTIVE, "SUPERSEDE", _P.SUPERSEDED, None, guards=("NEWER_VERSION",)),
], create_permission=CONFIGURATION_WRITE)


class PolicyStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


POLICY = _machine("POLICY", PolicyStatus, (PolicyStatus.ACTIVE,), [
    _t(PolicyStatus.ACTIVE, "ARCHIVE", PolicyStatus.ARCHIVED, CONFIGURATION_WRITE, reason=True,
       guards=("NOT_IN_USE",), audit="POLICY_ARCHIVED"),
], create_permission=CONFIGURATION_WRITE)
"""A policy used by any run is archived, never deleted (3.1 §3.2)."""


# --------------------------------------------------------------------------- country packs and statutory reports (3.2)


class PackVersionStatus(StrEnum):
    DRAFT = "DRAFT"
    UNDER_REVIEW = "UNDER_REVIEW"
    VERIFIED_FOR_PERIOD = "VERIFIED_FOR_PERIOD"
    EXPIRED = "EXPIRED"
    DISABLED = "DISABLED"


_PV = PackVersionStatus
PACK_VERSION = _machine("PACK_VERSION", PackVersionStatus, (_PV.DRAFT,), [
    _t(_PV.DRAFT, "SUBMIT_FOR_REVIEW", _PV.UNDER_REVIEW, PLATFORM_PACK_EDIT, guards=("SOURCE_MANIFEST_COMPLETE",)),
    _t(_PV.UNDER_REVIEW, "REQUEST_CHANGES", _PV.DRAFT, PLATFORM_PACK_REVIEW, reason=True),
    _t(_PV.UNDER_REVIEW, "VERIFY", _PV.VERIFIED_FOR_PERIOD, PLATFORM_PACK_REVIEW,
       guards=("REVIEWER_SIGN_OFF", "ALL_VECTORS_PASS", "NOT_FIXTURE")),
    _t(_PV.VERIFIED_FOR_PERIOD, "EXPIRE", _PV.EXPIRED, None, guards=("EFFECTIVE_TO_PASSED",)),
    _t(_PV.VERIFIED_FOR_PERIOD, "DISABLE", _PV.DISABLED, PLATFORM_PACK_REVIEW, reason=True, effects=("BLOCK_RUNS",)),
], create_permission=PLATFORM_PACK_EDIT)


class StatutoryReportStatus(StrEnum):
    DRAFT = "DRAFT"
    CALCULATED = "CALCULATED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    VERIFIED = "VERIFIED"
    READY_TO_FILE = "READY_TO_FILE"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    AMENDED = "AMENDED"


_SR = StatutoryReportStatus
STATUTORY_REPORT = _machine("STATUTORY_REPORT", StatutoryReportStatus, (_SR.DRAFT,), [
    _t(_SR.DRAFT, "GENERATE", _SR.CALCULATED, STATUTORY_REVIEW, guards=("RUNS_FINALIZED",)),
    _t(_SR.CALCULATED, "SUBMIT_FOR_REVIEW", _SR.REVIEW_REQUIRED, STATUTORY_REVIEW),
    _t(_SR.REVIEW_REQUIRED, "VERIFY", _SR.VERIFIED, STATUTORY_REVIEW, guards=("RECONCILES_TO_RUNS",)),
    *_each((_SR.REVIEW_REQUIRED, _SR.VERIFIED, _SR.READY_TO_FILE), "REGENERATE", _SR.CALCULATED, STATUTORY_REVIEW,
           reason=True, guards=("RUNS_FINALIZED",)),
    *_each((_SR.VERIFIED, _SR.READY_TO_FILE), "EXPORT", _SR.READY_TO_FILE, STATUTORY_REVIEW),
    _t(_SR.READY_TO_FILE, "MARK_SUBMITTED", _SR.SUBMITTED, STATUTORY_SUBMIT, guards=("FILING_REFERENCE",)),
    _t(_SR.SUBMITTED, "RECORD_ACCEPTED", _SR.ACCEPTED, STATUTORY_SUBMIT, guards=("ACKNOWLEDGEMENT",)),
    _t(_SR.SUBMITTED, "RECORD_REJECTED", _SR.REJECTED, STATUTORY_SUBMIT, guards=("AUTHORITY_RESPONSE",)),
    *_each((_SR.SUBMITTED, _SR.ACCEPTED, _SR.REJECTED), "AMEND", _SR.AMENDED, STATUTORY_SUBMIT, reason=True,
           effects=("CREATE_AMENDMENT",)),
], create_permission=STATUTORY_REVIEW)


# --------------------------------------------------------------------------- pay periods (3.5 §2.5)


class PeriodStatus(StrEnum):
    OPEN = "OPEN"
    PROCESSING = "PROCESSING"
    CLOSED = "CLOSED"


_PS = PeriodStatus
PAY_PERIOD = _machine("PAY_PERIOD", PeriodStatus, (_PS.OPEN,), [
    _t(_PS.OPEN, "START_PROCESSING", _PS.PROCESSING, None, guards=("REGULAR_RUN_CREATED",)),
    _t(_PS.PROCESSING, "RELEASE", _PS.OPEN, None, guards=("NO_ACTIVE_REGULAR_RUN",)),
    _t(_PS.PROCESSING, "CLOSE", _PS.CLOSED, None, guards=("REGULAR_RUN_FINALIZED",)),
])


def accepts_inputs(period_status: str) -> bool:
    """OPEN and PROCESSING periods take inputs (after lock they feed change detection); a CLOSED
    period takes none; corrections go through retro (3.5 §2.5, EC-VAR-03)."""
    status = str(period_status)
    if status not in PAY_PERIOD.states:
        raise InvalidTransition("PAY_PERIOD", status, "input", code="UNKNOWN_STATE")
    return status != _PS.CLOSED


# --------------------------------------------------------------------------- registry and module API

MACHINES: Mapping[str, Machine] = MappingProxyType({machine.name: machine for machine in (
    RUN, PAYMENT_INSTRUCTION, PAYMENT_BATCH, BANK_ACCOUNT, BANK_VERIFICATION, DOCUMENT, INVOICE, TASK, WAGE_INPUT,
    CLAIM, LOAN, COMPENSATION_ASSIGNMENT, SETTING_VALUE, POLICY_VERSION, POLICY, PACK_VERSION, STATUTORY_REPORT,
    PAY_PERIOD)})


def get_machine(machine: str | Machine) -> Machine:
    if isinstance(machine, Machine):
        return machine
    found = MACHINES.get(str(machine))
    if found is None:
        raise UnknownMachine(str(machine))
    return found


def next_state(machine: str | Machine, state: str, action: str) -> str:
    """The state ``action`` leads to from ``state``; raises ``InvalidTransition``."""
    return get_machine(machine).next_state(state, action)


def transition_spec(machine: str | Machine, state: str, action: str) -> TransitionSpec:
    return get_machine(machine).spec(state, action)


def required_permission(machine: str | Machine, state: str, action: str) -> str | None:
    """The permission code a user needs for the transition; ``None`` for a system action."""
    return get_machine(machine).spec(state, action).permission


def allowed_actions(machine: str | Machine, state: str) -> tuple[str, ...]:
    return get_machine(machine).allowed_actions(str(state))


def available_actions(machine: str | Machine, state: str, permissions: Iterable[str]) -> tuple[TransitionSpec, ...]:
    return get_machine(machine).available(str(state), permissions)


def is_terminal(machine: str | Machine, state: str) -> bool:
    found = get_machine(machine)
    if str(state) not in found.states:
        raise InvalidTransition(found.name, str(state), "is_terminal", code="UNKNOWN_STATE")
    return str(state) in found.terminal_states


def is_side_state(machine: str | Machine, state: str) -> bool:
    found = get_machine(machine)
    if str(state) not in found.states:
        raise InvalidTransition(found.name, str(state), "is_side_state", code="UNKNOWN_STATE")
    return str(state) in found.side_states


# --------------------------------------------------------------------------- run types and identity (3.5 §3)


class RunType(StrEnum):
    REGULAR = "REGULAR"
    OFF_CYCLE = "OFF_CYCLE"
    RETRO_ADJUSTMENT = "RETRO_ADJUSTMENT"
    FINAL_SETTLEMENT = "FINAL_SETTLEMENT"
    REVERSAL = "REVERSAL"


@dataclass(frozen=True)
class RunRef:
    """An existing run as the identity check needs it."""

    id: int
    run_type: RunType
    status: str
    pay_group_id: int
    period_id: int
    employment_period_id: int | None = None
    reverses_run_id: int | None = None


@dataclass(frozen=True)
class NewRun:
    """A run about to be created. Every run references a period (YTD and statutory purposes);
    retro adjustments also name the original periods, final settlements the employment period and
    reversals the run they negate."""

    run_type: RunType
    pay_group_id: int
    period_id: int | None
    original_period_ids: tuple[int, ...] = ()
    employment_period_id: int | None = None
    reverses_run_id: int | None = None


def check_run_identity(candidate: NewRun, existing: Iterable[RunRef], *,
                       allow_multiple_regular_runs: bool = False, period_status: str | None = None) -> None:
    """Raise ``RunIdentityError`` when the run may not be created (3.5 §3):

    REGULAR           one non-cancelled regular run per pay group and period (DUPLICATE_RUN,
                      EC-RUN-01) unless ``payroll.run.allow_multiple_regular_runs``; the period
                      must be OPEN when ``period_status`` is given (PERIOD_NOT_OPEN)
    OFF_CYCLE         many allowed
    RETRO_ADJUSTMENT  names the original periods (ORIGINAL_PERIODS_REQUIRED)
    FINAL_SETTLEMENT  one non-cancelled per employment period (DUPLICATE_FINAL_SETTLEMENT); later
                      corrections are other runs
    REVERSAL          the original run exists and is FINALIZED or later (ORIGINAL_RUN_NOT_FINALIZED)
    """
    try:
        run_type = RunType(str(candidate.run_type))
    except ValueError:
        raise RunIdentityError("INVALID_RUN_TYPE",
                               f"{candidate.run_type!r} is not one of {', '.join(RunType)}") from None
    if candidate.period_id is None:
        raise RunIdentityError("PERIOD_REQUIRED", "every run references a pay period")
    existing = list(existing)
    live = [run for run in existing if str(run.status) != RunStatus.CANCELLED]
    if run_type is RunType.REGULAR:
        if period_status is not None and str(period_status) != PeriodStatus.OPEN:
            raise RunIdentityError("PERIOD_NOT_OPEN", "a regular run needs an OPEN period")
        if not allow_multiple_regular_runs and any(
                run.run_type == RunType.REGULAR and run.pay_group_id == candidate.pay_group_id
                and run.period_id == candidate.period_id for run in live):
            raise RunIdentityError("DUPLICATE_RUN", "a regular run already exists for this pay group and period")
    elif run_type is RunType.RETRO_ADJUSTMENT and not candidate.original_period_ids:
        raise RunIdentityError("ORIGINAL_PERIODS_REQUIRED", "a retro adjustment references the original periods")
    elif run_type is RunType.FINAL_SETTLEMENT:
        if candidate.employment_period_id is None:
            raise RunIdentityError("EMPLOYMENT_PERIOD_REQUIRED", "a final settlement references the employment period")
        if any(run.run_type == RunType.FINAL_SETTLEMENT and run.employment_period_id == candidate.employment_period_id
               for run in live):
            raise RunIdentityError("DUPLICATE_FINAL_SETTLEMENT",
                                   "this employment period already has a final settlement; correct it with another run")
    elif run_type is RunType.REVERSAL:
        if candidate.reverses_run_id is None:
            raise RunIdentityError("ORIGINAL_RUN_REQUIRED", "a reversal references the run it negates")
        original = next((run for run in existing if run.id == candidate.reverses_run_id), None)
        if original is None:
            raise RunIdentityError("ORIGINAL_RUN_NOT_FOUND", "the run to reverse does not exist")
        if not is_run_finalized(original.status):
            raise RunIdentityError("ORIGINAL_RUN_NOT_FINALIZED",
                                   "only finalized runs are reversed; reopen or cancel a run that is not finalized")

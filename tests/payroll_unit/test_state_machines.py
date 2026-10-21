"""Status state machines (3.5 §4, 3.7 §3.3/§4/§8, 3.6 §5.2/§10, 3.4 §10.2/§13/§14/§15, 3.3 §7,
3.1 §2.5/§3.2, 3.2 §6.3/§8.2): every allowed transition, every forbidden one, and the rules a table
cannot express."""

from __future__ import annotations

import itertools
import re
from typing import Iterable

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.payroll import state_machines as SM
from app.payroll.state_machines import (
    InvalidTransition,
    NewRun,
    RunIdentityError,
    RunRef,
    RunType,
    apply_event,
    check_run_identity,
    employee_label,
    next_state,
)

Table = dict[tuple[str, str], tuple[str, str | None]]

RUN_CREATE, RUN_CALCULATE, RUN_REVIEW = "payroll:run:create", "payroll:run:calculate", "payroll:run:review"
RUN_APPROVE, RUN_FINALIZE, RUN_REVERSE = "payroll:run:approve", "payroll:run:finalize", "payroll:run:reverse"
RUN_CLOSE = "payroll:run:close"
PAY_PREPARE, PAY_APPROVE = "payroll:payment:prepare", "payroll:payment:approve"
PAY_SUBMIT, PAY_RECONCILE = "payroll:payment:submit", "payroll:payment:reconcile"
BANK_EDIT, BANK_APPROVE = "payroll:bank_details:edit", "payroll:bank_details:approve"


def rows(sources: Iterable[str], action: str, target: str | None, permission: str | None) -> Table:
    return {(source, action): (source if target is None else target, permission) for source in sources}


PRE_FINAL = ("DRAFT", "INPUTS_PENDING", "READY", "CALCULATING", "CALCULATED", "VALIDATION_FAILED",
             "REVIEW_REQUIRED", "APPROVAL_PENDING", "APPROVED", "REJECTED")
FINAL = ("FINALIZED", "PAYMENT_READY", "SUBMITTED", "PARTIALLY_PAID", "PAID", "RECONCILED", "CLOSED")
CONTRACT_RUN_STATES = {*PRE_FINAL, *FINAL, "REOPENED_FOR_CORRECTION", "CANCELLED"}

EXPECTED_RUN: Table = {
    ("DRAFT", "PRECHECK_BLOCKED"): ("INPUTS_PENDING", RUN_CREATE),
    ("DRAFT", "PRECHECK_PASSED"): ("READY", RUN_CREATE),
    ("INPUTS_PENDING", "PRECHECK_BLOCKED"): ("INPUTS_PENDING", RUN_CREATE),
    ("INPUTS_PENDING", "PRECHECK_PASSED"): ("READY", RUN_CREATE),
    ("READY", "CALCULATE"): ("CALCULATING", RUN_CALCULATE),
    ("CALCULATING", "COMPLETE_CALCULATION"): ("CALCULATED", None),
    ("CALCULATED", "FAIL_VALIDATION"): ("VALIDATION_FAILED", None),
    ("CALCULATED", "PASS_VALIDATION"): ("REVIEW_REQUIRED", None),
    **rows(("VALIDATION_FAILED", "REVIEW_REQUIRED", "REJECTED"), "RECALCULATE", "CALCULATING", RUN_CALCULATE),
    ("REVIEW_REQUIRED", "SUBMIT_FOR_APPROVAL"): ("APPROVAL_PENDING", RUN_REVIEW),
    ("APPROVAL_PENDING", "APPROVE_STEP"): ("APPROVAL_PENDING", RUN_APPROVE),
    ("APPROVAL_PENDING", "APPROVE_FINAL"): ("APPROVED", RUN_APPROVE),
    ("APPROVAL_PENDING", "REJECT"): ("REJECTED", RUN_APPROVE),
    ("REJECTED", "REWORK"): ("REVIEW_REQUIRED", RUN_CALCULATE),
    **rows(PRE_FINAL, "REOPEN", "REOPENED_FOR_CORRECTION", RUN_FINALIZE),
    ("REOPENED_FOR_CORRECTION", "RESUME_REVIEW"): ("REVIEW_REQUIRED", RUN_FINALIZE),
    ("REOPENED_FOR_CORRECTION", "RETURN_TO_DRAFT"): ("DRAFT", RUN_FINALIZE),
    **rows((*PRE_FINAL, "REOPENED_FOR_CORRECTION"), "CANCEL", "CANCELLED", RUN_FINALIZE),
    ("APPROVED", "FINALIZE"): ("FINALIZED", RUN_FINALIZE),
    ("FINALIZED", "APPROVE_PAYMENTS"): ("PAYMENT_READY", PAY_APPROVE),
    ("FINALIZED", "CLOSE"): ("CLOSED", RUN_CLOSE),
    ("PAYMENT_READY", "SUBMIT_PAYMENTS"): ("SUBMITTED", PAY_SUBMIT),
    ("SUBMITTED", "RECORD_PARTIAL_SETTLEMENT"): ("PARTIALLY_PAID", None),
    **rows(("SUBMITTED", "PARTIALLY_PAID"), "RECORD_FULL_SETTLEMENT", "PAID", None),
    **rows(("PAID", "RECONCILED"), "RECORD_RETURN", "PARTIALLY_PAID", None),
    ("PAID", "RECONCILE"): ("RECONCILED", PAY_RECONCILE),
    ("PARTIALLY_PAID", "RECONCILE"): ("RECONCILED", PAY_RECONCILE),  # decision I-17
    ("RECONCILED", "CLOSE"): ("CLOSED", RUN_CLOSE),
    **rows(FINAL, "REVERSE", None, RUN_REVERSE),
}

UNSENT = ("NOT_READY", "READY", "APPROVAL_PENDING", "APPROVED")
IN_FLIGHT = ("SUBMITTED", "ACKNOWLEDGED", "PROCESSING")
PROGRESS = ("SUBMITTED", "ACKNOWLEDGED", "PROCESSING", "PARTIALLY_SETTLED", "SETTLED")
EXPECTED_INSTRUCTION: Table = {
    ("NOT_READY", "MARK_READY"): ("READY", None),
    **rows(UNSENT, "HOLD", "ON_HOLD", None),
    ("ON_HOLD", "RELEASE_HOLD"): ("NOT_READY", PAY_APPROVE),
    ("READY", "SUBMIT_FOR_APPROVAL"): ("APPROVAL_PENDING", PAY_PREPARE),
    ("APPROVAL_PENDING", "APPROVE"): ("APPROVED", PAY_APPROVE),
    **rows(("APPROVAL_PENDING", "APPROVED", "QUEUED"), "RELEASE_FROM_BATCH", "READY", None),
    ("APPROVED", "QUEUE"): ("QUEUED", None),
    ("QUEUED", "SUBMIT"): ("SUBMITTED", PAY_SUBMIT),
    ("QUEUED", "RECORD_SUBMITTED"): ("SUBMITTED", None),
    **{(source, f"RECORD_{target}"): (target, None)
       for source in IN_FLIGHT for target in PROGRESS[PROGRESS.index(source) + 1:]},
    **{(source, f"RECORD_{target}"): (target, None)
       for source in IN_FLIGHT for target in ("REJECTED", "FAILED", "RETURNED", "REVERSED")},
    ("PARTIALLY_SETTLED", "RECORD_SETTLED"): ("SETTLED", None),
    **{(source, f"RECORD_{target}"): (target, None)
       for source in ("PARTIALLY_SETTLED", "SETTLED", "RECONCILED") for target in ("RETURNED", "REVERSED")},
    **rows((*IN_FLIGHT, "PARTIALLY_SETTLED"), "MANUAL_SETTLE", "SETTLED", PAY_RECONCILE),
    ("PARTIALLY_SETTLED", "ACCEPT_VARIANCE"): ("SETTLED", PAY_RECONCILE),
    ("SETTLED", "RECONCILE"): ("RECONCILED", PAY_RECONCILE),
    **rows((*UNSENT, "ON_HOLD"), "CANCEL", "CANCELLED", PAY_PREPARE),
    **rows((*UNSENT, "ON_HOLD"), "CANCEL_FOR_REVERSAL", "CANCELLED", None),
    **rows(("REJECTED", "FAILED", "RETURNED", "REVERSED"), "REISSUE", "REISSUED", PAY_PREPARE),
}

EXPECTED_BATCH: Table = {
    ("DRAFT", "SUBMIT_FOR_APPROVAL"): ("APPROVAL_PENDING", PAY_PREPARE),
    ("APPROVAL_PENDING", "APPROVE"): ("APPROVED", PAY_APPROVE),
    ("APPROVAL_PENDING", "REJECT"): ("CANCELLED", PAY_APPROVE),
    ("APPROVED", "GENERATE_FILE"): ("FILE_GENERATED", PAY_PREPARE),
    ("APPROVED", "QUEUE_API"): ("API_QUEUED", PAY_SUBMIT),
    ("FILE_GENERATED", "MARK_SUBMITTED"): ("SUBMITTED", PAY_SUBMIT),
    ("API_QUEUED", "RECORD_SUBMITTED"): ("SUBMITTED", None),
    ("SUBMITTED", "RECORD_PARTIAL_SETTLEMENT"): ("PARTIALLY_SETTLED", None),
    **rows(("SUBMITTED", "PARTIALLY_SETTLED"), "RECORD_SETTLEMENT", "SETTLED", None),
    **rows(("PARTIALLY_SETTLED", "SETTLED"), "RECONCILE", "RECONCILED", PAY_RECONCILE),
    **rows(("DRAFT", "APPROVAL_PENDING", "APPROVED", "FILE_GENERATED", "API_QUEUED"), "CANCEL", "CANCELLED",
           PAY_PREPARE),
}

EXPECTED_BANK_ACCOUNT: Table = {
    ("DRAFT", "SUBMIT"): ("PENDING_APPROVAL", BANK_EDIT),
    ("DRAFT", "ACTIVATE"): ("ACTIVE", BANK_EDIT),
    ("PENDING_APPROVAL", "APPROVE"): ("ACTIVE", BANK_APPROVE),
    ("PENDING_APPROVAL", "REJECT"): ("DRAFT", BANK_APPROVE),
    ("ACTIVE", "DEACTIVATE"): ("INACTIVE", BANK_EDIT),
    ("ACTIVE", "REPLACE"): ("INACTIVE", None),
}

EXPECTED_VERIFICATION: Table = {
    ("NOT_SUBMITTED", "SUBMIT"): ("PENDING", BANK_EDIT),
    ("PENDING", "RECORD_VERIFIED"): ("VERIFIED", None),
    ("PENDING", "MARK_VERIFIED"): ("VERIFIED", BANK_APPROVE),
    ("PENDING", "RECORD_FAILED"): ("FAILED", None),
    ("PENDING", "MARK_FAILED"): ("FAILED", BANK_APPROVE),
    **rows(("PENDING", "VERIFIED"), "FLAG_FOR_REVIEW", "REQUIRES_REVIEW", None),
    ("REQUIRES_REVIEW", "MARK_VERIFIED"): ("VERIFIED", BANK_APPROVE),
    ("REQUIRES_REVIEW", "MARK_FAILED"): ("FAILED", BANK_APPROVE),
    ("REQUIRES_REVIEW", "CLEAR_REVIEW"): ("PENDING", BANK_APPROVE),
    **rows(("PENDING", "VERIFIED"), "RECORD_PAYMENT_FAILURE", "FAILED", None),
    ("VERIFIED", "EXPIRE"): ("EXPIRED", None),
    **rows(("FAILED", "EXPIRED"), "RESUBMIT", "PENDING", BANK_EDIT),
}

PUBLISHED = ("PUBLISHED", "EMAIL_SENT", "EMAIL_FAILED", "VIEWED")
EXPECTED_DOCUMENT: Table = {
    ("GENERATED", "PUBLISH"): ("PUBLISHED", None),
    ("GENERATED", "PUBLISH_MANUALLY"): ("PUBLISHED", "payroll:payslip:publish"),
    **rows(("PUBLISHED", "EMAIL_FAILED"), "RECORD_EMAIL_SENT", "EMAIL_SENT", None),
    **rows(("PUBLISHED", "EMAIL_SENT"), "RECORD_EMAIL_FAILED", "EMAIL_FAILED", None),
    **rows(("PUBLISHED", "EMAIL_SENT", "EMAIL_FAILED"), "RECORD_VIEW", "VIEWED", None),
    **rows(PUBLISHED, "SUPERSEDE", "SUPERSEDED", None),
    **rows((*PUBLISHED, "SUPERSEDED"), "REVERSE", "REVERSED", None),
}

EXPECTED_INVOICE: Table = {
    ("DRAFT", "ISSUE"): ("ISSUED", PAY_PREPARE),
    ("ISSUED", "APPROVE"): ("APPROVED", PAY_APPROVE),
    ("APPROVED", "RECORD_PAYMENT"): ("PAID", None),
    **rows(("ISSUED", "APPROVED", "PAID"), "CREDIT", "CREDITED", PAY_APPROVE),
}

EXPECTED_TASK: Table = {
    ("DRAFT", "SUBMIT"): ("SUBMITTED", "task:manage"),
    **rows(("SUBMITTED", "DISPUTED"), "APPROVE", "APPROVED", "task:approve"),
    **rows(("SUBMITTED", "DISPUTED"), "REJECT", "REJECTED", "task:approve"),
    ("SUBMITTED", "DISPUTE"): ("DISPUTED", "task:approve"),
    ("APPROVED", "MARK_PAID"): ("PAID", None),
    ("PAID", "REVERSE"): ("REVERSED", "task:approve"),
}

EXPECTED_WAGE_INPUT: Table = {
    ("DRAFT", "SUBMIT"): ("PENDING_APPROVAL", "payroll:inputs:edit"),
    ("DRAFT", "AUTO_APPROVE"): ("APPROVED", "payroll:inputs:edit"),
    ("PENDING_APPROVAL", "APPROVE"): ("APPROVED", "payroll:inputs:approve"),
    ("PENDING_APPROVAL", "REJECT"): ("REJECTED", "payroll:inputs:approve"),
    ("APPROVED", "CONSUME"): ("CONSUMED", None),
    **rows(("DRAFT", "PENDING_APPROVAL", "APPROVED"), "CANCEL", "CANCELLED", "payroll:inputs:edit"),
}

EXPECTED_CLAIM: Table = {
    ("SUBMITTED", "APPROVE"): ("APPROVED", "payroll:reimbursements:approve"),
    ("SUBMITTED", "REJECT"): ("REJECTED", "payroll:reimbursements:approve"),
    ("APPROVED", "SCHEDULE"): ("SCHEDULED", None),
    ("SCHEDULED", "UNSCHEDULE"): ("APPROVED", None),
    ("SCHEDULED", "MARK_PAID"): ("PAID", None),
}

LOANS_APPROVE = "payroll:loans:approve"
EXPECTED_LOAN: Table = {
    ("PENDING_APPROVAL", "APPROVE"): ("APPROVED", LOANS_APPROVE),
    ("PENDING_APPROVAL", "REJECT"): ("REJECTED", LOANS_APPROVE),
    ("APPROVED", "DISBURSE"): ("ACTIVE", None),
    ("APPROVED", "CANCEL"): ("CANCELLED", "payroll:loans:manage"),
    ("ACTIVE", "PAUSE"): ("PAUSED", LOANS_APPROVE),
    ("PAUSED", "RESUME"): ("ACTIVE", LOANS_APPROVE),
    **rows(("ACTIVE", "PAUSED"), "RESTRUCTURE", None, LOANS_APPROVE),
    **rows(("ACTIVE", "PAUSED"), "PREPAY", None, LOANS_APPROVE),
    **rows(("ACTIVE", "PAUSED"), "WAIVE", None, LOANS_APPROVE),
    **rows(("ACTIVE", "PAUSED"), "CLOSE", "CLOSED", None),
}

COMP_EDIT, COMP_APPROVE = "payroll:compensation:edit", "payroll:compensation:approve"
EXPECTED_COMPENSATION: Table = {
    ("DRAFT", "SUBMIT"): ("PENDING_APPROVAL", COMP_EDIT),
    ("PENDING_APPROVAL", "APPROVE_SCHEDULED"): ("SCHEDULED", COMP_APPROVE),
    ("PENDING_APPROVAL", "APPROVE_ACTIVE"): ("ACTIVE", COMP_APPROVE),
    ("PENDING_APPROVAL", "REJECT"): ("REJECTED", COMP_APPROVE),
    ("SCHEDULED", "ACTIVATE"): ("ACTIVE", None),
    **rows(("SCHEDULED", "ACTIVE"), "SUPERSEDE", "SUPERSEDED", None),
}

EXPECTED_SETTING_VALUE: Table = {
    ("PENDING_APPROVAL", "APPROVE"): ("ACTIVE", "settings:approve_changes"),
    ("PENDING_APPROVAL", "REJECT"): ("REJECTED", "settings:approve_changes"),
    ("ACTIVE", "SUPERSEDE"): ("SUPERSEDED", None),
}

EXPECTED_POLICY_VERSION: Table = {
    ("DRAFT", "ACTIVATE"): ("ACTIVE", "payroll:configuration:write"),
    ("ACTIVE", "SUPERSEDE"): ("SUPERSEDED", None),
}

EXPECTED_POLICY: Table = {("ACTIVE", "ARCHIVE"): ("ARCHIVED", "payroll:configuration:write")}

EXPECTED_PACK_VERSION: Table = {
    ("DRAFT", "SUBMIT_FOR_REVIEW"): ("UNDER_REVIEW", "platform:country_packs:edit"),
    ("UNDER_REVIEW", "REQUEST_CHANGES"): ("DRAFT", "platform:country_packs:review"),
    ("UNDER_REVIEW", "VERIFY"): ("VERIFIED_FOR_PERIOD", "platform:country_packs:review"),
    ("VERIFIED_FOR_PERIOD", "EXPIRE"): ("EXPIRED", None),
    ("VERIFIED_FOR_PERIOD", "DISABLE"): ("DISABLED", "platform:country_packs:review"),
}

STAT_REVIEW, STAT_SUBMIT = "payroll:statutory:review", "payroll:statutory:submit"
EXPECTED_STATUTORY_REPORT: Table = {
    ("DRAFT", "GENERATE"): ("CALCULATED", STAT_REVIEW),
    ("CALCULATED", "SUBMIT_FOR_REVIEW"): ("REVIEW_REQUIRED", STAT_REVIEW),
    ("REVIEW_REQUIRED", "VERIFY"): ("VERIFIED", STAT_REVIEW),
    **rows(("REVIEW_REQUIRED", "VERIFIED", "READY_TO_FILE"), "REGENERATE", "CALCULATED", STAT_REVIEW),
    **rows(("VERIFIED", "READY_TO_FILE"), "EXPORT", "READY_TO_FILE", STAT_REVIEW),
    ("READY_TO_FILE", "MARK_SUBMITTED"): ("SUBMITTED", STAT_SUBMIT),
    ("SUBMITTED", "RECORD_ACCEPTED"): ("ACCEPTED", STAT_SUBMIT),
    ("SUBMITTED", "RECORD_REJECTED"): ("REJECTED", STAT_SUBMIT),
    **rows(("SUBMITTED", "ACCEPTED", "REJECTED"), "AMEND", "AMENDED", STAT_SUBMIT),
}

EXPECTED_PAY_PERIOD: Table = {
    ("OPEN", "START_PROCESSING"): ("PROCESSING", None),
    ("PROCESSING", "RELEASE"): ("OPEN", None),
    ("PROCESSING", "CLOSE"): ("CLOSED", None),
}

EXPECTED: dict[str, Table] = {
    "RUN": EXPECTED_RUN, "PAYMENT_INSTRUCTION": EXPECTED_INSTRUCTION, "PAYMENT_BATCH": EXPECTED_BATCH,
    "BANK_ACCOUNT": EXPECTED_BANK_ACCOUNT, "BANK_VERIFICATION": EXPECTED_VERIFICATION, "DOCUMENT": EXPECTED_DOCUMENT,
    "INVOICE": EXPECTED_INVOICE, "TASK": EXPECTED_TASK, "WAGE_INPUT": EXPECTED_WAGE_INPUT, "CLAIM": EXPECTED_CLAIM,
    "LOAN": EXPECTED_LOAN, "COMPENSATION_ASSIGNMENT": EXPECTED_COMPENSATION, "SETTING_VALUE": EXPECTED_SETTING_VALUE,
    "POLICY_VERSION": EXPECTED_POLICY_VERSION, "POLICY": EXPECTED_POLICY, "PACK_VERSION": EXPECTED_PACK_VERSION,
    "STATUTORY_REPORT": EXPECTED_STATUTORY_REPORT, "PAY_PERIOD": EXPECTED_PAY_PERIOD,
}

EXPECTED_STATES: dict[str, set[str]] = {
    "RUN": CONTRACT_RUN_STATES,
    "PAYMENT_INSTRUCTION": {"NOT_READY", "READY", "APPROVAL_PENDING", "APPROVED", "QUEUED", "SUBMITTED",
                            "ACKNOWLEDGED", "PROCESSING", "PARTIALLY_SETTLED", "SETTLED", "RECONCILED", "ON_HOLD",
                            "CANCELLED", "REJECTED", "FAILED", "RETURNED", "REVERSED", "REISSUED"},
    "PAYMENT_BATCH": {"DRAFT", "APPROVAL_PENDING", "APPROVED", "FILE_GENERATED", "API_QUEUED", "SUBMITTED",
                      "PARTIALLY_SETTLED", "SETTLED", "RECONCILED", "CANCELLED"},
    "BANK_ACCOUNT": {"DRAFT", "PENDING_APPROVAL", "ACTIVE", "INACTIVE"},
    "BANK_VERIFICATION": {"NOT_SUBMITTED", "PENDING", "VERIFIED", "FAILED", "EXPIRED", "REQUIRES_REVIEW"},
    "DOCUMENT": {"GENERATED", "PUBLISHED", "VIEWED", "EMAIL_SENT", "EMAIL_FAILED", "SUPERSEDED", "REVERSED"},
    "INVOICE": {"DRAFT", "ISSUED", "APPROVED", "PAID", "CREDITED"},
    "TASK": {"DRAFT", "SUBMITTED", "APPROVED", "REJECTED", "DISPUTED", "PAID", "REVERSED"},
    "WAGE_INPUT": {"DRAFT", "PENDING_APPROVAL", "APPROVED", "CONSUMED", "REJECTED", "CANCELLED"},
    "CLAIM": {"SUBMITTED", "APPROVED", "SCHEDULED", "PAID", "REJECTED"},
    "LOAN": {"PENDING_APPROVAL", "APPROVED", "ACTIVE", "PAUSED", "CLOSED", "REJECTED", "CANCELLED"},
    "COMPENSATION_ASSIGNMENT": {"DRAFT", "PENDING_APPROVAL", "SCHEDULED", "ACTIVE", "SUPERSEDED", "REJECTED"},
    "SETTING_VALUE": {"PENDING_APPROVAL", "ACTIVE", "SUPERSEDED", "REJECTED"},
    "POLICY_VERSION": {"DRAFT", "ACTIVE", "SUPERSEDED"},
    "POLICY": {"ACTIVE", "ARCHIVED"},
    "PACK_VERSION": {"DRAFT", "UNDER_REVIEW", "VERIFIED_FOR_PERIOD", "EXPIRED", "DISABLED"},
    "STATUTORY_REPORT": {"DRAFT", "CALCULATED", "REVIEW_REQUIRED", "VERIFIED", "READY_TO_FILE", "SUBMITTED",
                         "ACCEPTED", "REJECTED", "AMENDED"},
    "PAY_PERIOD": {"OPEN", "PROCESSING", "CLOSED"},
}

ALL_ALLOWED = [(name, state, action) for name, table in EXPECTED.items() for (state, action) in table]
ALL_FORBIDDEN = [(name, state, action) for name, machine in SM.MACHINES.items()
                 for state, action in itertools.product(sorted(machine.states), sorted(machine.actions))
                 if (state, action) not in machine.transitions]


# --------------------------------------------------------------------------- tables


def test_every_listed_machine_exists() -> None:
    assert set(SM.MACHINES) == set(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_state_vocabulary_is_the_contract_vocabulary(name: str) -> None:
    assert SM.MACHINES[name].states == EXPECTED_STATES[name]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_transition_table_matches_the_spec_exactly(name: str) -> None:
    assert dict(SM.MACHINES[name].transitions) == EXPECTED[name]


@pytest.mark.parametrize(("name", "state", "action"), ALL_ALLOWED)
def test_every_allowed_transition(name: str, state: str, action: str) -> None:
    target, permission = EXPECTED[name][(state, action)]
    assert next_state(name, state, action) == target
    assert SM.required_permission(name, state, action) == permission
    spec = SM.transition_spec(name, state, action)
    assert (spec.source, spec.action, spec.target, spec.is_system) == (state, action, target, permission is None)


@pytest.mark.parametrize(("name", "state", "action"), ALL_FORBIDDEN)
def test_every_forbidden_transition_raises_a_stable_code(name: str, state: str, action: str) -> None:
    machine = SM.MACHINES[name]
    with pytest.raises(InvalidTransition) as excinfo:
        next_state(name, state, action)
    error = excinfo.value
    assert (error.machine, error.state, error.action, error.http_status) == (name, state, action, 409)
    assert error.code == machine.locked.get(state, ("INVALID_TRANSITION", None))[0]
    assert str(error).startswith(error.code)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_tables_are_well_formed(name: str) -> None:
    machine = SM.MACHINES[name]
    assert machine.initial_states and machine.initial_states <= machine.states
    reachable = set().union(*(machine.reachable_from(s) for s in machine.initial_states))
    assert reachable == set(machine.states), f"unreachable: {set(machine.states) - reachable}"
    for spec in machine.specs:
        assert all(g in SM.GUARDS for g in spec.guards) and all(e in SM.EFFECTS for e in spec.effects)
        assert spec.permission is None or re.fullmatch(r"[a-z_]+(:[a-z_]+)+", spec.permission)
        if SM.is_platform_permission(spec.permission):
            assert name == "PACK_VERSION"


def test_unknown_machine_state_and_action() -> None:
    with pytest.raises(SM.UnknownMachine) as excinfo:
        next_state("PAYROLL", "DRAFT", "CALCULATE")
    assert excinfo.value.code == "UNKNOWN_MACHINE"
    with pytest.raises(InvalidTransition) as unknown_state:
        next_state("RUN", "DONE", "CALCULATE")
    assert unknown_state.value.code == "UNKNOWN_STATE"
    with pytest.raises(InvalidTransition) as unknown_action:
        next_state("RUN", "READY", "LAUNCH")
    assert unknown_action.value.code == "UNKNOWN_ACTION"
    assert SM.get_machine(SM.RUN) is SM.RUN and SM.next_state(SM.RUN, "READY", "CALCULATE") == "CALCULATING"


def test_machine_definition_rejects_inconsistent_tables() -> None:
    bad = SM.TransitionSpec("A", "GO", "Z", None)
    with pytest.raises(ValueError, match="unknown state"):
        SM.Machine("X", frozenset({"A"}), frozenset({"A"}), frozenset(), (bad,))
    twice = (SM.TransitionSpec("A", "GO", "A", None),) * 2
    with pytest.raises(ValueError, match="duplicate"):
        SM.Machine("X", frozenset({"A"}), frozenset({"A"}), frozenset(), twice)
    with pytest.raises(ValueError, match="undeclared"):
        SM.Machine("X", frozenset({"A"}), frozenset({"A"}), frozenset(),
                   (SM.TransitionSpec("A", "GO", "A", None, guards=("MAGIC",)),))


# --------------------------------------------------------------------------- run (3.5 §4)


@pytest.mark.parametrize("state", FINAL)
def test_nothing_leaves_the_finalized_family_except_payment_close_and_reverse(state: str) -> None:
    allowed = set(SM.allowed_actions("RUN", state))
    assert allowed <= {"APPROVE_PAYMENTS", "SUBMIT_PAYMENTS", "RECORD_PARTIAL_SETTLEMENT", "RECORD_FULL_SETTLEMENT",
                       "RECORD_RETURN", "RECONCILE", "CLOSE", "REVERSE"}
    assert SM.RUN.reachable_from(state) <= set(FINAL)
    assert next_state("RUN", state, "REVERSE") == state  # a reversal is a new REVERSAL run


@pytest.mark.parametrize("action", ["REOPEN", "CANCEL", "RECALCULATE", "FINALIZE", "REJECT", "APPROVE_FINAL"])
def test_ec_run_17_finalized_run_refuses_changes_with_run_finalized(action: str) -> None:
    for state in FINAL:
        with pytest.raises(InvalidTransition) as excinfo:
            next_state("RUN", state, action)
        assert (excinfo.value.code, excinfo.value.hint) == ("RUN_FINALIZED", "CREATE_CORRECTION")


def test_ec_run_17_ensure_run_editable() -> None:
    for state in PRE_FINAL + ("REOPENED_FOR_CORRECTION",):
        SM.ensure_run_editable(state)
    with pytest.raises(InvalidTransition) as excinfo:
        SM.ensure_run_editable("FINALIZED")
    assert (excinfo.value.code, excinfo.value.hint, excinfo.value.action) == ("RUN_FINALIZED", "CREATE_CORRECTION",
                                                                              "EDIT")
    with pytest.raises(InvalidTransition) as cancelled:
        SM.ensure_run_editable("CANCELLED")
    assert cancelled.value.code == "RUN_CANCELLED"
    with pytest.raises(InvalidTransition) as unknown:
        SM.ensure_run_editable("ARCHIVED")
    assert unknown.value.code == "UNKNOWN_STATE"


def test_cancelled_is_terminal_everywhere() -> None:
    for name, machine in SM.MACHINES.items():
        if "CANCELLED" in machine.states:
            assert SM.allowed_actions(name, "CANCELLED") == ()
            assert SM.is_terminal(name, "CANCELLED")
    with pytest.raises(InvalidTransition) as excinfo:
        next_state("RUN", "CANCELLED", "RETURN_TO_DRAFT")
    assert excinfo.value.code == "RUN_CANCELLED"


def test_ec_run_15_cancel_after_calculation_releases_inputs_and_creates_nothing() -> None:
    for state in ("CALCULATED", "REVIEW_REQUIRED", "APPROVED", "CALCULATING"):
        spec = SM.transition_spec("RUN", state, "CANCEL")
        assert spec.target == "CANCELLED" and spec.reason_required and "RELEASE_INPUTS" in spec.effects
        assert not {"ENQUEUE_DOCUMENTS", "CREATE_PAYMENT_INSTRUCTIONS", "WRITE_YTD", "CONSUME_INPUTS"} & set(
            spec.effects)
    assert "STOP_CALCULATION" in SM.transition_spec("RUN", "CALCULATING", "CANCEL").effects


def test_ec_run_15_negative_cancel_after_finalization_is_refused() -> None:
    with pytest.raises(InvalidTransition) as excinfo:
        next_state("RUN", "FINALIZED", "CANCEL")
    assert excinfo.value.code == "RUN_FINALIZED"


def test_ec_run_16_reopen_after_approval_clears_approvals_with_audit() -> None:
    spec = SM.transition_spec("RUN", "APPROVED", "REOPEN")
    assert (spec.target, spec.permission, spec.reason_required, spec.audit_event) == (
        "REOPENED_FOR_CORRECTION", RUN_FINALIZE, True, "RUN_REOPENED")
    assert "CLEAR_APPROVALS" in spec.effects
    assert next_state("RUN", "REOPENED_FOR_CORRECTION", "RESUME_REVIEW") == "REVIEW_REQUIRED"
    assert next_state("RUN", "REOPENED_FOR_CORRECTION", "RETURN_TO_DRAFT") == "DRAFT"
    assert "RELEASE_INPUTS" in SM.transition_spec("RUN", "REOPENED_FOR_CORRECTION", "RETURN_TO_DRAFT").effects


def test_ec_run_16_negative_reopen_is_not_possible_after_finalization_or_twice() -> None:
    with pytest.raises(InvalidTransition) as excinfo:
        next_state("RUN", "PAID", "REOPEN")
    assert excinfo.value.code == "RUN_FINALIZED"
    with pytest.raises(InvalidTransition) as twice:
        next_state("RUN", "REOPENED_FOR_CORRECTION", "REOPEN")
    assert twice.value.code == "INVALID_TRANSITION"


def test_finalize_guards_and_effects() -> None:
    spec = SM.transition_spec("RUN", "APPROVED", "FINALIZE")
    assert set(spec.guards) == {"STEP_UP_MFA", "NOT_STALE", "STATUTORY_PACK_VERIFIED", "IDEMPOTENCY_KEY"}
    assert set(spec.effects) >= {"WRITE_YTD", "CREATE_PAYMENT_INSTRUCTIONS", "ENQUEUE_DOCUMENTS", "CONSUME_INPUTS"}
    with pytest.raises(InvalidTransition) as excinfo:  # EC-RUN-04: a repeated finalize never runs twice
        next_state("RUN", "FINALIZED", "FINALIZE")
    assert excinfo.value.code == "RUN_FINALIZED"


def test_approval_guards_separation_of_duties_and_self_pay() -> None:
    for action in ("APPROVE_STEP", "APPROVE_FINAL"):
        guards = SM.transition_spec("RUN", "APPROVAL_PENDING", action).guards
        assert {"SEPARATION_OF_DUTIES", "NO_SELF_PAY_APPROVAL"} <= set(guards)
    assert SM.transition_spec("RUN", "APPROVAL_PENDING", "REJECT").reason_required
    assert set(SM.transition_spec("RUN", "REVIEW_REQUIRED", "SUBMIT_FOR_APPROVAL").guards) == {
        "NO_BLOCKING_EXCEPTIONS", "VARIANCE_FLAGS_ACKNOWLEDGED", "NOT_STALE"}
    assert set(SM.transition_spec("RUN", "READY", "CALCULATE").guards) == {"IDEMPOTENCY_KEY", "NO_CALCULATION_RUNNING"}
    assert set(SM.transition_spec("RUN", "FINALIZED", "REVERSE").guards) == {"TWO_APPROVERS"}
    assert SM.transition_spec("RUN", "FINALIZED", "REVERSE").reason_required
    assert set(SM.RUN_CREATE_GUARDS) <= set(SM.GUARDS)


def test_i_17_partially_paid_run_reconciles_with_carried_items() -> None:
    spec = SM.transition_spec("RUN", "PARTIALLY_PAID", "RECONCILE")
    assert spec.target == "RECONCILED" and spec.permission == PAY_RECONCILE
    assert set(spec.guards) == {"SETTLED_INSTRUCTIONS_MATCHED", "UNSETTLED_ITEMS_CARRIED"}
    assert spec.effects == ("CARRY_UNSETTLED_ITEMS",)
    with pytest.raises(InvalidTransition):
        next_state("RUN", "SUBMITTED", "RECONCILE")


def test_run_terminal_and_side_states() -> None:
    assert SM.RUN.terminal_states == {"CANCELLED", "CLOSED"}
    assert SM.RUN.side_states == {"CANCELLED", "REOPENED_FOR_CORRECTION"}
    assert SM.is_side_state("RUN", "REOPENED_FOR_CORRECTION") and not SM.is_side_state("RUN", "PAID")
    assert SM.is_run_finalized("CLOSED") and not SM.is_run_finalized("APPROVED")
    with pytest.raises(InvalidTransition):
        SM.is_terminal("RUN", "NOPE")
    with pytest.raises(InvalidTransition):
        SM.is_side_state("RUN", "NOPE")


@pytest.mark.parametrize(("state", "stage"), [
    ("DRAFT", 1), ("INPUTS_PENDING", 1), ("READY", 1), ("CALCULATING", 2), ("CALCULATED", 2),
    ("VALIDATION_FAILED", 2), ("REVIEW_REQUIRED", 2), ("APPROVAL_PENDING", 3), ("REJECTED", 3), ("APPROVED", 3),
    ("FINALIZED", 4), ("PAYMENT_READY", 5), ("SUBMITTED", 5), ("PARTIALLY_PAID", 5), ("PAID", 5),
    ("RECONCILED", 6), ("CLOSED", 6), ("CANCELLED", None), ("REOPENED_FOR_CORRECTION", None),
])
def test_ui_stage(state: str, stage: int | None) -> None:
    assert SM.ui_stage(state) == stage
    if stage is not None:
        assert SM.UI_STAGE_LABELS[stage] in {"Prepare", "Calculate", "Approve", "Finalize", "Pay", "Close"}


def test_ui_stage_negative_unknown_state() -> None:
    with pytest.raises(InvalidTransition) as excinfo:
        SM.ui_stage("ARCHIVED")
    assert excinfo.value.code == "UNKNOWN_STATE"
    assert set(SM.RUN_UI_STAGES) | set(SM.RUN_SIDE_STATES) == CONTRACT_RUN_STATES


def test_available_actions_respect_permissions_and_hide_system_actions() -> None:
    processor = {RUN_CREATE, RUN_CALCULATE, RUN_REVIEW}
    assert [s.action for s in SM.available_actions("RUN", "REVIEW_REQUIRED", processor)] == [
        "RECALCULATE", "SUBMIT_FOR_APPROVAL"]
    assert [s.action for s in SM.available_actions("RUN", "APPROVAL_PENDING", {RUN_APPROVE})] == [
        "APPROVE_FINAL", "APPROVE_STEP", "REJECT"]
    assert SM.available_actions("RUN", "CALCULATING", {RUN_CALCULATE}) == ()
    assert SM.allowed_actions("RUN", "CALCULATING") == ("CANCEL", "COMPLETE_CALCULATION", "REOPEN")


_RUN_ACTIONS = st.sampled_from(sorted(SM.RUN.actions))


@settings(max_examples=150, deadline=None)
@given(actions=st.lists(_RUN_ACTIONS, max_size=40))
def test_run_random_walks_never_return_from_finalized(actions: list[str]) -> None:
    state, finalized = "DRAFT", False
    for action in actions:
        if (state, action) not in SM.RUN.transitions:
            with pytest.raises(InvalidTransition):
                next_state("RUN", state, action)
            continue
        state = next_state("RUN", state, action)
        finalized = finalized or state in FINAL
        if finalized:
            assert state in FINAL
        assert state != "CANCELLED" or not finalized


# --------------------------------------------------------------------------- payment instructions (3.7 §8.1)


def test_instruction_rank_is_the_main_line_order() -> None:
    assert sorted(SM.INSTRUCTION_RANK, key=SM.INSTRUCTION_RANK.__getitem__) == [
        "NOT_READY", "READY", "APPROVAL_PENDING", "APPROVED", "QUEUED", "SUBMITTED", "ACKNOWLEDGED", "PROCESSING",
        "PARTIALLY_SETTLED", "SETTLED", "RECONCILED"]
    assert set(SM.INSTRUCTION_SIDE_STATES) == {"ON_HOLD", "CANCELLED", "REJECTED", "FAILED", "RETURNED", "REVERSED",
                                               "REISSUED"}
    assert SM.PAYMENT_INSTRUCTION.side_states == set(SM.INSTRUCTION_SIDE_STATES)


def test_ec_pay_07_forward_jump_is_accepted() -> None:
    outcome = apply_event("SUBMITTED", "SETTLED")
    assert (outcome.status, outcome.outcome, outcome.action, outcome.applied) == (
        "SETTLED", "APPLIED", "RECORD_SETTLED", True)
    assert apply_event("ACKNOWLEDGED", "PARTIALLY_SETTLED").status == "PARTIALLY_SETTLED"


def test_ec_pay_07_older_event_never_moves_backwards() -> None:
    for current, event in (("SETTLED", "ACKNOWLEDGED"), ("SETTLED", "PROCESSING"), ("PROCESSING", "SUBMITTED"),
                           ("RECONCILED", "SETTLED"), ("SETTLED", "PARTIALLY_SETTLED")):
        outcome = apply_event(current, event)
        assert (outcome.status, outcome.outcome, outcome.action, outcome.applied) == (current, "STALE", None, False)
    assert apply_event("SETTLED", "SETTLED").outcome == "DUPLICATE"


def test_ec_pay_07_contradictory_evidence_is_a_review_conflict() -> None:
    cases = {("SETTLED", "FAILED"): "FAILED_AFTER_SETTLEMENT", ("FAILED", "SETTLED"): "SETTLED_AFTER_FAILURE",
             ("REISSUED", "SETTLED"): "SETTLED_AFTER_REISSUE", ("APPROVED", "SETTLED"): "NOT_SUBMITTED",
             ("CANCELLED", "ACKNOWLEDGED"): "NOT_SUBMITTED", ("ON_HOLD", "SUBMITTED"): "NOT_SUBMITTED"}
    for (current, event), reason in cases.items():
        outcome = apply_event(current, event)
        assert (outcome.status, outcome.outcome, outcome.reason) == (current, "CONFLICT", reason)
    assert apply_event("RETURNED", "SETTLED").outcome == "STALE"
    assert apply_event("FAILED", "ACKNOWLEDGED").outcome == "STALE"


def test_ec_pay_07_returns_after_settlement_are_applied() -> None:
    assert apply_event("SETTLED", "RETURNED").status == "RETURNED"
    assert apply_event("RECONCILED", "REVERSED").status == "REVERSED"
    assert apply_event("PROCESSING", "FAILED").status == "FAILED"


def test_ec_pay_01_file_downloaded_but_not_uploaded_stays_queued() -> None:
    for event in ("ACKNOWLEDGED", "SETTLED", "PROCESSING"):
        outcome = apply_event("QUEUED", event)
        assert (outcome.status, outcome.outcome, outcome.reason) == ("QUEUED", "CONFLICT", "NOT_SUBMITTED")
    assert apply_event("QUEUED", "SUBMITTED").status == "SUBMITTED"
    assert employee_label("QUEUED").key == "SCHEDULED"


def test_ec_pay_02_acknowledged_is_not_settled() -> None:
    assert apply_event("SUBMITTED", "ACKNOWLEDGED").status == "ACKNOWLEDGED"
    assert employee_label("ACKNOWLEDGED", "API", "VERIFIED").key == "PROCESSING"
    assert SM.settlement_level(["ACKNOWLEDGED"]) == "NONE"


def test_apply_event_negative_inputs() -> None:
    with pytest.raises(InvalidTransition) as excinfo:
        apply_event("SUBMITTED", "APPROVED")
    assert excinfo.value.code == "NOT_AN_EVENT_STATUS"
    with pytest.raises(InvalidTransition) as unknown:
        apply_event("PAID", "SETTLED")
    assert unknown.value.code == "UNKNOWN_STATE"


@pytest.mark.parametrize(("current", "event"), list(itertools.product(
    sorted(SM.PAYMENT_INSTRUCTION.states), sorted(SM.EVENT_STATUSES))))
def test_apply_event_outcomes_agree_with_the_table(current: str, event: str) -> None:
    outcome = apply_event(current, event)
    assert outcome.outcome in {"APPLIED", "DUPLICATE", "STALE", "CONFLICT"}
    if outcome.applied:
        assert outcome.action is not None
        assert SM.PAYMENT_INSTRUCTION.next_state(current, outcome.action) == outcome.status == event
    else:
        assert outcome.status == current and outcome.action is None
    if current in SM.INSTRUCTION_RANK and event in SM.INSTRUCTION_RANK and outcome.applied:
        assert SM.INSTRUCTION_RANK[event] > SM.INSTRUCTION_RANK[current]


@settings(max_examples=200, deadline=None)
@given(events=st.lists(st.sampled_from(sorted(SM.EVENT_STATUSES)), max_size=12))
def test_ec_pay_07_property_any_event_order_never_moves_backwards(events: list[str]) -> None:
    status = "SUBMITTED"
    for event in events:
        before = status
        status = apply_event(status, event).status
        if before in SM.INSTRUCTION_RANK and status in SM.INSTRUCTION_RANK:
            assert SM.INSTRUCTION_RANK[status] >= SM.INSTRUCTION_RANK[before]
        if before in ("PARTIALLY_SETTLED", "SETTLED", "RECONCILED"):
            assert status not in ("FAILED", "REJECTED")


def test_ec_pay_14_manual_settlement_needs_evidence_and_a_second_reviewer() -> None:
    spec = SM.transition_spec("PAYMENT_INSTRUCTION", "SUBMITTED", "MANUAL_SETTLE")
    assert set(spec.guards) == {"EVIDENCE_UPLOADED", "SECOND_REVIEWER"} and spec.permission == PAY_RECONCILE
    with pytest.raises(InvalidTransition):
        next_state("PAYMENT_INSTRUCTION", "QUEUED", "MANUAL_SETTLE")


def test_ec_cor_04_reversal_cancels_unsubmitted_instructions_only() -> None:
    for state in (*UNSENT, "ON_HOLD"):
        assert next_state("PAYMENT_INSTRUCTION", state, "CANCEL_FOR_REVERSAL") == "CANCELLED"
    for state in ("QUEUED", "SUBMITTED", "SETTLED"):
        with pytest.raises(InvalidTransition):
            next_state("PAYMENT_INSTRUCTION", state, "CANCEL_FOR_REVERSAL")


def test_reissue_marks_the_old_instruction_and_creates_a_new_one() -> None:
    spec = SM.transition_spec("PAYMENT_INSTRUCTION", "RETURNED", "REISSUE")
    assert spec.target == "REISSUED" and "CREATE_REPLACEMENT_INSTRUCTION" in spec.effects
    assert "REISSUE_APPROVAL" in spec.guards and SM.is_terminal("PAYMENT_INSTRUCTION", "REISSUED")


# --------------------------------------------------------------------------- employee labels (3.7 §8.1, I-17)


@pytest.mark.parametrize(("status", "text"), [
    ("NOT_READY", "Scheduled"), ("READY", "Scheduled"), ("APPROVAL_PENDING", "Scheduled"), ("APPROVED", "Scheduled"),
    ("QUEUED", "Scheduled"), ("SUBMITTED", "Processing"), ("ACKNOWLEDGED", "Processing"),
    ("PROCESSING", "Processing"), ("SETTLED", "Paid on {date}"), ("RECONCILED", "Paid on {date}"),
    ("PARTIALLY_SETTLED", "Partly paid — {amount} received"), ("ON_HOLD", "On hold — HR will contact you"),
    ("FAILED", "Payment failed — please check your bank details"),
    ("REJECTED", "Payment failed — please check your bank details"),
    ("RETURNED", "Payment failed — please check your bank details"), ("REISSUED", "Sent again"),
    ("REVERSED", "Reversed"), ("CANCELLED", "Cancelled"),
])
def test_employee_labels_of_the_spec(status: str, text: str) -> None:
    assert SM.employee_payment_label(status).text == text
    channel = None if SM.INSTRUCTION_RANK.get(status, 99) < SM.INSTRUCTION_RANK["SUBMITTED"] else "API"
    assert employee_label(status, channel, "VERIFIED").text == text


def test_i_17_manual_upload_without_verified_evidence() -> None:
    label = employee_label("SUBMITTED", SM.SubmissionChannel.MANUAL_UPLOAD, SM.EvidenceStatus.UNVERIFIED)
    assert (label.key, label.text, label.badge) == (
        "SUBMITTED_MANUAL_UNVERIFIED", "Submitted for manual payment — unverified", None)
    assert employee_label("PROCESSING", "MANUAL_OFFLINE").text == "Submitted for manual payment — unverified"


def test_i_17_negative_verified_or_api_submissions_read_processing() -> None:
    assert employee_label("SUBMITTED", "MANUAL_UPLOAD", "VERIFIED").text == "Processing"
    assert employee_label("SUBMITTED", "API", "UNVERIFIED").text == "Processing"
    assert employee_label("QUEUED", "MANUAL_UPLOAD").text == "Scheduled"


def test_i_17_never_paid_without_verified_evidence() -> None:
    assert employee_label("SETTLED", "API", "UNVERIFIED").key == "PROCESSING"
    assert employee_label("PARTIALLY_SETTLED", "MANUAL_OFFLINE", "UNVERIFIED").key == "SUBMITTED_MANUAL_UNVERIFIED"
    assert employee_label("SETTLED", "MANUAL_OFFLINE", "VERIFIED").text == "Paid on {date}"
    assert employee_label("RETURNED", "API", "UNVERIFIED").key == "FAILED"


def test_i_17_sandbox_is_badged() -> None:
    label = employee_label("SETTLED", "SANDBOX", "VERIFIED")
    assert (label.key, label.badge) == ("PAID", "SANDBOX")
    assert employee_label("SUBMITTED", "API").badge is None


def test_i_17_negative_label_inputs() -> None:
    for status, channel, evidence, code in (("SUBMITTED", None, "UNVERIFIED", "SUBMISSION_CHANNEL_REQUIRED"),
                                            ("SUBMITTED", "FAX", "UNVERIFIED", "INVALID_LABEL_INPUT"),
                                            ("SUBMITTED", "API", "MAYBE", "INVALID_LABEL_INPUT"),
                                            ("PAID", "API", "VERIFIED", "UNKNOWN_STATE")):
        with pytest.raises(InvalidTransition) as excinfo:
            employee_label(status, channel, evidence)
        assert excinfo.value.code == code
    assert {c.value for c in SM.SubmissionChannel} == {"MANUAL_UPLOAD", "API", "SANDBOX", "MANUAL_OFFLINE"}
    assert {e.value for e in SM.EvidenceStatus} == {"UNVERIFIED", "VERIFIED"}


def test_no_label_says_paid_before_settled() -> None:
    for status in SM.PAYMENT_INSTRUCTION.states - {"SETTLED", "RECONCILED"}:
        assert "Paid on" not in SM.employee_payment_label(status).text


# --------------------------------------------------------------------------- settlement derivation (3.5 §5.9)


@pytest.mark.parametrize(("statuses", "level"), [
    (["SETTLED", "RECONCILED", "ON_HOLD", "CANCELLED"], "FULL"),
    (["SETTLED", "SUBMITTED"], "PARTIAL"),
    (["PARTIALLY_SETTLED"], "PARTIAL"),
    (["SUBMITTED", "ACKNOWLEDGED"], "NONE"),
    (["ON_HOLD", "CANCELLED", "REISSUED"], "NOTHING_TO_PAY"),
    ([], "NOTHING_TO_PAY"),
    (["SETTLED", "RETURNED"], "PARTIAL"),
    (["REISSUED", "SETTLED"], "FULL"),
])
def test_settlement_level(statuses: list[str], level: str) -> None:
    assert SM.settlement_level(statuses) == level


def test_settlement_level_negative_unknown_status() -> None:
    with pytest.raises(InvalidTransition):
        SM.settlement_level(["PAID"])


def test_run_and_batch_settlement_actions() -> None:
    assert SM.run_settlement_action("SUBMITTED", ["SETTLED", "SUBMITTED"]) == "RECORD_PARTIAL_SETTLEMENT"
    assert SM.run_settlement_action("PARTIALLY_PAID", ["SETTLED", "ON_HOLD"]) == "RECORD_FULL_SETTLEMENT"
    assert SM.run_settlement_action("PAID", ["SETTLED", "RETURNED"]) == "RECORD_RETURN"
    assert SM.run_settlement_action("SUBMITTED", ["ON_HOLD"]) is None  # never "paid" with nothing to pay
    assert SM.run_settlement_action("SUBMITTED", ["SUBMITTED"]) is None
    assert SM.batch_settlement_action("SUBMITTED", ["SETTLED"]) == "RECORD_SETTLEMENT"
    assert SM.batch_settlement_action("PARTIALLY_SETTLED", ["SETTLED", "PARTIALLY_SETTLED"]) is None
    for current, statuses in (("SUBMITTED", ["SETTLED"]), ("PAID", ["SETTLED", "FAILED"]),
                              ("RECONCILED", ["RETURNED"])):
        action = SM.run_settlement_action(current, statuses)
        assert action is not None and (current, action) in SM.RUN.transitions


# --------------------------------------------------------------------------- batches (3.7 §8.2)


def test_ec_pay_13_submitted_batch_cannot_be_submitted_again() -> None:
    for state in ("SUBMITTED", "PARTIALLY_SETTLED", "SETTLED", "RECONCILED"):
        for action in ("MARK_SUBMITTED", "QUEUE_API", "GENERATE_FILE", "CANCEL"):
            with pytest.raises(InvalidTransition) as excinfo:
                next_state("PAYMENT_BATCH", state, action)
            assert excinfo.value.code == "BATCH_SUBMITTED"
    assert "FILE_HASH_UNCHANGED" in SM.transition_spec("PAYMENT_BATCH", "FILE_GENERATED", "MARK_SUBMITTED").guards
    assert "STORE_FILE_HASH" in SM.transition_spec("PAYMENT_BATCH", "APPROVED", "GENERATE_FILE").effects


def test_batch_release_needs_dual_control_step_up_and_signatory() -> None:
    for state, action in (("FILE_GENERATED", "MARK_SUBMITTED"), ("APPROVED", "QUEUE_API")):
        assert {"DUAL_CONTROL", "STEP_UP_MFA", "FUNDING_SIGNATORY"} <= set(
            SM.transition_spec("PAYMENT_BATCH", state, action).guards)
    assert SM.transition_spec("PAYMENT_BATCH", "APPROVAL_PENDING", "APPROVE").guards == ("DUAL_CONTROL",)
    with pytest.raises(InvalidTransition) as excinfo:
        next_state("PAYMENT_BATCH", "CANCELLED", "SUBMIT_FOR_APPROVAL")
    assert excinfo.value.code == "BATCH_CANCELLED"


# --------------------------------------------------------------------------- bank accounts and verification


def test_bank_account_change_controls() -> None:
    assert set(SM.transition_spec("BANK_ACCOUNT", "DRAFT", "SUBMIT").guards) == {"STEP_UP_MFA", "VELOCITY_LIMIT"}
    approve = SM.transition_spec("BANK_ACCOUNT", "PENDING_APPROVAL", "APPROVE")
    assert approve.guards == ("MAKER_CHECKER",)
    assert set(approve.effects) == {"START_COOLING_OFF", "NOTIFY_OLD_AND_NEW_CONTACTS"}
    assert SM.transition_spec("BANK_ACCOUNT", "PENDING_APPROVAL", "REJECT").reason_required
    assert "CHANGE_APPROVAL_OFF" in SM.transition_spec("BANK_ACCOUNT", "DRAFT", "ACTIVATE").guards
    assert SM.BANK_ACCOUNT.terminal_states == {"INACTIVE"}


def test_ec_bnk_10_returned_payment_fails_the_verification() -> None:
    assert next_state("BANK_VERIFICATION", "VERIFIED", "RECORD_PAYMENT_FAILURE") == "FAILED"
    assert next_state("BANK_VERIFICATION", "FAILED", "RESUBMIT") == "PENDING"
    with pytest.raises(InvalidTransition):
        next_state("BANK_VERIFICATION", "NOT_SUBMITTED", "RECORD_PAYMENT_FAILURE")


def test_format_alone_never_verifies() -> None:
    for state, action in (("PENDING", "RECORD_VERIFIED"), ("PENDING", "MARK_VERIFIED"),
                          ("REQUIRES_REVIEW", "MARK_VERIFIED")):
        assert "REQUIRED_METHODS_PASSED" in SM.transition_spec("BANK_VERIFICATION", state, action).guards
    with pytest.raises(InvalidTransition):
        next_state("BANK_VERIFICATION", "NOT_SUBMITTED", "MARK_VERIFIED")


# --------------------------------------------------------------------------- documents, invoices


def test_ec_doc_02_unpublished_payslip_is_not_visible() -> None:
    assert not SM.document_visible_to_employee("GENERATED")
    assert all(SM.document_visible_to_employee(s) for s in SM.DOCUMENT.states - {"GENERATED"})
    with pytest.raises(InvalidTransition):
        SM.document_visible_to_employee("DRAFT")
    assert "RUN_NOT_REVERSED" in SM.transition_spec("DOCUMENT", "GENERATED", "PUBLISH").guards


def test_document_reversal_keeps_the_original() -> None:
    spec = SM.transition_spec("DOCUMENT", "VIEWED", "REVERSE")
    assert spec.target == "REVERSED" and spec.effects == ("ISSUE_REVERSAL_STATEMENT",)
    with pytest.raises(InvalidTransition):
        next_state("DOCUMENT", "REVERSED", "PUBLISH")


def test_invoice_lifecycle_and_credit() -> None:
    state = "DRAFT"
    for action in ("ISSUE", "APPROVE", "RECORD_PAYMENT", "CREDIT"):
        state = next_state("INVOICE", state, action)
    assert state == "CREDITED"
    assert "ALLOCATE_GAPLESS_NUMBER" in SM.transition_spec("INVOICE", "DRAFT", "ISSUE").effects
    assert "SETTLEMENT_EVIDENCE" in SM.transition_spec("INVOICE", "APPROVED", "RECORD_PAYMENT").guards
    with pytest.raises(InvalidTransition):
        next_state("INVOICE", "DRAFT", "CREDIT")


# --------------------------------------------------------------------------- tasks, wage inputs, claims, loans


def test_ec_tsk_02_only_approved_tasks_are_payable() -> None:
    assert SM.task_is_payable("APPROVED")
    for state in ("DRAFT", "SUBMITTED", "REJECTED", "DISPUTED", "PAID", "REVERSED"):
        assert not SM.task_is_payable(state)
    with pytest.raises(InvalidTransition):
        SM.task_is_payable("DONE")
    with pytest.raises(InvalidTransition):
        next_state("TASK", "REJECTED", "MARK_PAID")
    assert SM.transition_spec("TASK", "PAID", "REVERSE").effects == ("CREATE_CLAWBACK",)


def test_ec_var_03_closed_period_takes_no_inputs() -> None:
    assert SM.accepts_inputs("OPEN") and SM.accepts_inputs("PROCESSING") and not SM.accepts_inputs("CLOSED")
    with pytest.raises(InvalidTransition):
        SM.accepts_inputs("LOCKED")
    assert "PERIOD_ACCEPTS_INPUTS" in SM.transition_spec("WAGE_INPUT", "DRAFT", "SUBMIT").guards


def test_ec_var_01_wage_input_is_consumed_once_by_a_finalized_run() -> None:
    assert SM.transition_spec("WAGE_INPUT", "APPROVED", "CONSUME").guards == ("FINALIZED_RUN",)
    with pytest.raises(InvalidTransition):
        next_state("WAGE_INPUT", "CONSUMED", "CONSUME")
    with pytest.raises(InvalidTransition):
        next_state("WAGE_INPUT", "CONSUMED", "CANCEL")


def test_claim_and_loan_paths() -> None:
    state = "SUBMITTED"
    for action in ("APPROVE", "SCHEDULE", "UNSCHEDULE", "SCHEDULE", "MARK_PAID"):
        state = next_state("CLAIM", state, action)
    assert state == "PAID"
    state = "PENDING_APPROVAL"
    for action in ("APPROVE", "DISBURSE", "PAUSE", "RESTRUCTURE", "RESUME", "PREPAY", "CLOSE"):
        state = next_state("LOAN", state, action)
    assert state == "CLOSED"
    assert SM.transition_spec("LOAN", "ACTIVE", "CLOSE").guards == ("ZERO_BALANCE",)
    with pytest.raises(InvalidTransition):
        next_state("LOAN", "PENDING_APPROVAL", "DISBURSE")


# --------------------------------------------------------------------------- compensation and configuration


def test_compensation_approval_controls() -> None:
    for action in ("APPROVE_SCHEDULED", "APPROVE_ACTIVE"):
        guards = SM.transition_spec("COMPENSATION_ASSIGNMENT", "PENDING_APPROVAL", action).guards
        assert {"MAKER_CHECKER", "NOT_OWN_SALARY", "APPROVER_IN_CHAIN"} <= set(guards)
    assert "EFFECTIVE_IN_FUTURE" in SM.transition_spec("COMPENSATION_ASSIGNMENT", "PENDING_APPROVAL",
                                                       "APPROVE_SCHEDULED").guards
    assert "MARK_LOCKED_RUNS_STALE" in SM.transition_spec("COMPENSATION_ASSIGNMENT", "SCHEDULED", "ACTIVATE").effects
    with pytest.raises(InvalidTransition):
        next_state("COMPENSATION_ASSIGNMENT", "DRAFT", "APPROVE_ACTIVE")


def test_setting_and_policy_change_control() -> None:
    approve = SM.transition_spec("SETTING_VALUE", "PENDING_APPROVAL", "APPROVE")
    assert approve.guards == ("MAKER_CHECKER",) and "MARK_LOCKED_RUNS_STALE" in approve.effects
    assert SM.SETTING_VALUE.initial_states == {"PENDING_APPROVAL", "ACTIVE"}
    activate = SM.transition_spec("POLICY_VERSION", "DRAFT", "ACTIVATE")
    assert set(activate.guards) == {"VALID_SCHEMA", "IMPACT_PREVIEW_DONE"}
    assert SM.transition_spec("POLICY", "ACTIVE", "ARCHIVE").guards == ("NOT_IN_USE",)
    with pytest.raises(InvalidTransition):
        next_state("POLICY_VERSION", "SUPERSEDED", "ACTIVATE")


# --------------------------------------------------------------------------- packs and statutory reports (3.2)


def test_pack_verification_needs_sign_off_vectors_and_never_a_fixture() -> None:
    spec = SM.transition_spec("PACK_VERSION", "UNDER_REVIEW", "VERIFY")
    assert set(spec.guards) == {"REVIEWER_SIGN_OFF", "ALL_VECTORS_PASS", "NOT_FIXTURE"}
    assert SM.is_platform_permission(spec.permission) and not SM.is_platform_permission(RUN_CREATE)
    assert not SM.is_platform_permission(None)
    with pytest.raises(InvalidTransition):
        next_state("PACK_VERSION", "DRAFT", "VERIFY")
    assert "BLOCK_RUNS" in SM.transition_spec("PACK_VERSION", "VERIFIED_FOR_PERIOD", "DISABLE").effects


def test_ec_sta_11_export_moves_to_ready_to_file_never_submitted() -> None:
    assert next_state("STATUTORY_REPORT", "VERIFIED", "EXPORT") == "READY_TO_FILE"
    assert next_state("STATUTORY_REPORT", "READY_TO_FILE", "EXPORT") == "READY_TO_FILE"
    into_submitted = [key for key, (target, _) in SM.STATUTORY_REPORT.transitions.items() if target == "SUBMITTED"]
    assert into_submitted == [("READY_TO_FILE", "MARK_SUBMITTED")]
    assert SM.transition_spec("STATUTORY_REPORT", "READY_TO_FILE", "MARK_SUBMITTED").guards == ("FILING_REFERENCE",)
    assert SM.transition_spec("STATUTORY_REPORT", "SUBMITTED", "RECORD_ACCEPTED").guards == ("ACKNOWLEDGEMENT",)


def test_ec_sta_11_negative_export_is_not_allowed_before_verification() -> None:
    for state in ("DRAFT", "CALCULATED", "REVIEW_REQUIRED", "SUBMITTED"):
        with pytest.raises(InvalidTransition):
            next_state("STATUTORY_REPORT", state, "EXPORT")


def test_ec_sta_12_correction_after_submission_is_an_amendment() -> None:
    for state in ("SUBMITTED", "ACCEPTED", "REJECTED"):
        spec = SM.transition_spec("STATUTORY_REPORT", state, "AMEND")
        assert spec.target == "AMENDED" and spec.effects == ("CREATE_AMENDMENT",) and spec.reason_required
    with pytest.raises(InvalidTransition):
        next_state("STATUTORY_REPORT", "SUBMITTED", "REGENERATE")


# --------------------------------------------------------------------------- pay periods (3.5 §2.5)


def test_pay_period_lifecycle() -> None:
    assert next_state("PAY_PERIOD", "OPEN", "START_PROCESSING") == "PROCESSING"
    assert next_state("PAY_PERIOD", "PROCESSING", "CLOSE") == "CLOSED"
    assert SM.transition_spec("PAY_PERIOD", "PROCESSING", "CLOSE").guards == ("REGULAR_RUN_FINALIZED",)
    assert SM.PAY_PERIOD.terminal_states == {"CLOSED"}
    for action in ("START_PROCESSING", "RELEASE"):
        with pytest.raises(InvalidTransition):
            next_state("PAY_PERIOD", "CLOSED", action)


# --------------------------------------------------------------------------- run identity (3.5 §3)


def _run(run_id: int, run_type: RunType, status: str, *, period: int = 7, group: int = 1,
         employment: int | None = None) -> RunRef:
    return RunRef(id=run_id, run_type=run_type, status=status, pay_group_id=group, period_id=period,
                  employment_period_id=employment)


def test_ec_run_01_second_regular_run_is_refused() -> None:
    existing = [_run(1, RunType.REGULAR, "REVIEW_REQUIRED")]
    with pytest.raises(RunIdentityError) as excinfo:
        check_run_identity(NewRun(RunType.REGULAR, 1, 7), existing)
    assert (excinfo.value.code, excinfo.value.http_status) == ("DUPLICATE_RUN", 409)


def test_ec_run_01_negative_cancelled_other_group_other_period_or_setting_allow_it() -> None:
    check_run_identity(NewRun(RunType.REGULAR, 1, 7), [_run(1, RunType.REGULAR, "CANCELLED")])
    check_run_identity(NewRun(RunType.REGULAR, 1, 7), [_run(1, RunType.REGULAR, "DRAFT", group=2)])
    check_run_identity(NewRun(RunType.REGULAR, 1, 7), [_run(1, RunType.REGULAR, "DRAFT", period=8)])
    check_run_identity(NewRun(RunType.REGULAR, 1, 7), [_run(1, RunType.OFF_CYCLE, "DRAFT")])
    check_run_identity(NewRun(RunType.REGULAR, 1, 7), [_run(1, RunType.REGULAR, "DRAFT")],
                       allow_multiple_regular_runs=True)


def test_regular_run_needs_an_open_period() -> None:
    check_run_identity(NewRun(RunType.REGULAR, 1, 7), [], period_status="OPEN")
    for status in ("PROCESSING", "CLOSED"):
        with pytest.raises(RunIdentityError) as excinfo:
            check_run_identity(NewRun(RunType.REGULAR, 1, 7), [], period_status=status)
        assert excinfo.value.code == "PERIOD_NOT_OPEN"
    check_run_identity(NewRun(RunType.OFF_CYCLE, 1, 7), [], period_status="CLOSED")


def test_off_cycle_runs_are_many_but_reference_a_period() -> None:
    existing = [_run(i, RunType.OFF_CYCLE, "FINALIZED") for i in range(3)]
    check_run_identity(NewRun(RunType.OFF_CYCLE, 1, 7), existing)
    with pytest.raises(RunIdentityError) as excinfo:
        check_run_identity(NewRun(RunType.OFF_CYCLE, 1, None), existing)
    assert excinfo.value.code == "PERIOD_REQUIRED"


def test_retro_and_final_settlement_identity() -> None:
    with pytest.raises(RunIdentityError) as retro:
        check_run_identity(NewRun(RunType.RETRO_ADJUSTMENT, 1, 7), [])
    assert retro.value.code == "ORIGINAL_PERIODS_REQUIRED"
    check_run_identity(NewRun(RunType.RETRO_ADJUSTMENT, 1, 7, original_period_ids=(5, 6)), [])
    with pytest.raises(RunIdentityError) as missing:
        check_run_identity(NewRun(RunType.FINAL_SETTLEMENT, 1, 7), [])
    assert missing.value.code == "EMPLOYMENT_PERIOD_REQUIRED"
    existing = [_run(1, RunType.FINAL_SETTLEMENT, "FINALIZED", employment=40)]
    with pytest.raises(RunIdentityError) as duplicate:
        check_run_identity(NewRun(RunType.FINAL_SETTLEMENT, 1, 7, employment_period_id=40), existing)
    assert duplicate.value.code == "DUPLICATE_FINAL_SETTLEMENT"
    check_run_identity(NewRun(RunType.FINAL_SETTLEMENT, 1, 7, employment_period_id=41), existing)
    check_run_identity(NewRun(RunType.FINAL_SETTLEMENT, 1, 7, employment_period_id=40),
                       [_run(1, RunType.FINAL_SETTLEMENT, "CANCELLED", employment=40)])


def test_reversal_only_of_finalized_runs() -> None:
    existing = [_run(1, RunType.REGULAR, "APPROVED"), _run(2, RunType.REGULAR, "PAID", period=6)]
    check_run_identity(NewRun(RunType.REVERSAL, 1, 6, reverses_run_id=2), existing)
    for run_id, code in ((1, "ORIGINAL_RUN_NOT_FINALIZED"), (3, "ORIGINAL_RUN_NOT_FOUND"),
                         (None, "ORIGINAL_RUN_REQUIRED")):
        with pytest.raises(RunIdentityError) as excinfo:
            check_run_identity(NewRun(RunType.REVERSAL, 1, 6, reverses_run_id=run_id), existing)
        assert excinfo.value.code == code


def test_run_identity_negative_unknown_type() -> None:
    with pytest.raises(RunIdentityError) as excinfo:
        check_run_identity(NewRun("BONUS", 1, 7), [])  # type: ignore[arg-type]
    assert excinfo.value.code == "INVALID_RUN_TYPE"
    assert {t.value for t in RunType} == {"REGULAR", "OFF_CYCLE", "RETRO_ADJUSTMENT", "FINAL_SETTLEMENT", "REVERSAL"}

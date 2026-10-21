"""Generate the frontend payroll catalogues: currencies and status vocabularies (3.2 §3.4-§3.5, 3.8 §7, §9).

    python scripts/export_payroll_catalogues.py [output_path]

Sources of truth:

* app/payroll/currencies.py: every ISO 4217 code with its numeric code, ISO minor unit, status,
  fund flag, payroll flag and cash rounding increment. The frontend formats money with these
  digits, never with Intl/CLDR defaults (they differ for IQD, LBP, RSD and others).
* app/payroll/state_machines.py: every status vocabulary (its states, initial, side and terminal
  states), the run UI stages 1-6 and the employee payment labels, computed by calling
  ``employee_label`` for every status, submission channel and evidence status so the browser can
  never say "Paid" where the backend would not.

Display labels, tones and glossary text live here, beside the vocabularies they describe. The
script fails if a state has no label or a label names a state that does not exist, so a new
backend state can never reach the UI unlabelled.

Only those two pure modules are imported (no database, environment variables or app.config).
The output is deterministic: running the script twice produces the same file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.payroll import currencies  # noqa: E402
from app.payroll import state_machines as sm  # noqa: E402

DEFAULT_OUT = ROOT.parent / "hrms-frontend" / "src" / "lib" / "payroll" / "catalogues.generated.ts"

TONES = ("neutral", "primary", "success", "warning", "danger", "info")

# Machine name -> frontend vocabulary key.
VOCABULARY_KEYS: dict[str, str] = {
    "RUN": "run",
    "PAYMENT_INSTRUCTION": "instruction",
    "PAYMENT_BATCH": "batch",
    "BANK_ACCOUNT": "bank_account",
    "BANK_VERIFICATION": "verification",
    "DOCUMENT": "document",
    "INVOICE": "invoice",
    "TASK": "task",
    "WAGE_INPUT": "wage_input",
    "CLAIM": "claim",
    "LOAN": "loan",
    "COMPENSATION_ASSIGNMENT": "compensation",
    "SETTING_VALUE": "setting_value",
    "POLICY_VERSION": "policy_version",
    "POLICY": "policy",
    "PACK_VERSION": "pack_version",
    "STATUTORY_REPORT": "statutory_report",
    "PAY_PERIOD": "pay_period",
}

# (label, tone, glossary) per state. Glossary text is plain and consequence-first (3.8 §12).
L = tuple[str, str, str]
LABELS: dict[str, dict[str, L]] = {
    "RUN": {
        "DRAFT": ("Draft", "neutral", "The run exists but nothing has been calculated yet."),
        "INPUTS_PENDING": ("Inputs pending", "warning", "Waiting for wage inputs, attendance or approvals before it can be calculated."),
        "READY": ("Ready to calculate", "info", "All inputs are in. The run can be calculated."),
        "CALCULATING": ("Calculating", "info", "A calculation job is running. Actions are disabled until it finishes."),
        "CALCULATED": ("Calculated", "info", "Amounts are calculated and can be reviewed. Nothing is approved or paid yet."),
        "VALIDATION_FAILED": ("Validation failed", "danger", "The calculation found errors that must be fixed before review."),
        "REVIEW_REQUIRED": ("Review required", "warning", "Flags or exceptions need a reviewer's decision before approval."),
        "APPROVAL_PENDING": ("Awaiting approval", "warning", "Submitted for approval. The approver can approve or reject it."),
        "APPROVED": ("Approved", "success", "Approved, not finalized. It can still be reopened; nothing is locked or paid."),
        "REJECTED": ("Rejected", "danger", "The approver rejected the run. Fix the issues and recalculate."),
        "REOPENED_FOR_CORRECTION": ("Reopened for correction", "warning", "The run was reopened to correct it. It must be recalculated and approved again."),
        "CANCELLED": ("Cancelled", "neutral", "The run was cancelled. It will never be paid."),
        "FINALIZED": ("Finalized", "primary", "Locked. Payslips are generated and nothing in this run can be edited. Payment has not started."),
        "PAYMENT_READY": ("Ready for payment", "info", "Finalized and ready for payment batches. Nothing has been sent to a bank yet."),
        "SUBMITTED": ("Payment submitted", "info", "Payments were submitted. Money is not confirmed as received until settlement evidence arrives."),
        "PARTIALLY_PAID": ("Partly paid", "warning", "Some payments are confirmed settled; others are still pending, held or failed."),
        "PAID": ("Paid", "success", "Every payment that is not held or cancelled is confirmed settled with evidence."),
        "RECONCILED": ("Reconciled", "success", "Payments were matched to the bank statement."),
        "CLOSED": ("Closed", "neutral", "The run is complete and closed."),
    },
    "PAYMENT_INSTRUCTION": {
        "NOT_READY": ("Not ready", "neutral", "Missing something before it can be paid, such as a verified bank account."),
        "READY": ("Ready", "info", "Ready to be added to a payment batch."),
        "APPROVAL_PENDING": ("Awaiting approval", "warning", "Its batch is waiting for approval."),
        "APPROVED": ("Approved", "info", "Approved for payment. Not sent to the bank yet."),
        "QUEUED": ("Queued", "info", "Waiting to be sent to the bank."),
        "SUBMITTED": ("Submitted", "info", "Sent to the bank. Not confirmed as paid."),
        "ACKNOWLEDGED": ("Acknowledged", "info", "The bank acknowledged the instruction. Not confirmed as paid."),
        "PROCESSING": ("Processing", "info", "The bank is processing the payment. Not confirmed as paid."),
        "PARTIALLY_SETTLED": ("Partly settled", "warning", "Only part of the amount is confirmed received."),
        "SETTLED": ("Settled", "success", "The bank confirmed the payment."),
        "RECONCILED": ("Reconciled", "success", "The payment was matched to the bank statement."),
        "ON_HOLD": ("On hold", "warning", "Held on purpose. It will not be paid until the hold is released."),
        "CANCELLED": ("Cancelled", "neutral", "Cancelled. It will not be paid."),
        "REJECTED": ("Rejected", "danger", "The bank rejected the instruction. Check the account details and reissue."),
        "FAILED": ("Failed", "danger", "The payment failed. Check the account details and reissue."),
        "RETURNED": ("Returned", "danger", "The money came back from the receiving bank. Check the account and reissue."),
        "REVERSED": ("Reversed", "danger", "The payment was reversed after settlement."),
        "REISSUED": ("Reissued", "neutral", "Replaced by a new instruction."),
    },
    "PAYMENT_BATCH": {
        "DRAFT": ("Draft", "neutral", "The batch is being prepared."),
        "APPROVAL_PENDING": ("Awaiting approval", "warning", "Waiting for a payment approver."),
        "APPROVED": ("Approved", "info", "Approved. Not released to the bank yet."),
        "FILE_GENERATED": ("File generated", "info", "The bank file was generated. Uploading it does not mean anyone was paid."),
        "API_QUEUED": ("Queued for bank", "info", "Queued for sending through the bank connection."),
        "SUBMITTED": ("Submitted", "info", "Submitted to the bank. Not confirmed as paid."),
        "PARTIALLY_SETTLED": ("Partly settled", "warning", "Some instructions are confirmed settled; others are not."),
        "SETTLED": ("Settled", "success", "Every instruction in the batch is confirmed settled."),
        "RECONCILED": ("Reconciled", "success", "The batch was matched to the bank statement."),
        "CANCELLED": ("Cancelled", "neutral", "Cancelled. Nothing in it will be paid."),
    },
    "BANK_ACCOUNT": {
        "DRAFT": ("Draft", "neutral", "Being entered. Not used for payments."),
        "PENDING_APPROVAL": ("Awaiting approval", "warning", "A change waiting for approval. Payments keep using the current account."),
        "ACTIVE": ("Active", "success", "Used for payments once verified."),
        "INACTIVE": ("Inactive", "neutral", "No longer used for payments."),
    },
    "BANK_VERIFICATION": {
        "NOT_SUBMITTED": ("Not submitted", "neutral", "No proof of account has been submitted."),
        "PENDING": ("Pending", "warning", "Proof submitted and waiting for verification."),
        "VERIFIED": ("Verified", "success", "The account was verified."),
        "FAILED": ("Failed", "danger", "Verification failed. Submit new proof or correct the details."),
        "EXPIRED": ("Expired", "warning", "The verification expired and must be renewed."),
        "REQUIRES_REVIEW": ("Needs review", "warning", "A reviewer must check this account before it is used."),
    },
    "DOCUMENT": {
        "GENERATED": ("Generated", "info", "Generated but not visible to the employee yet."),
        "PUBLISHED": ("Published", "success", "Visible to the employee."),
        "EMAIL_SENT": ("Email sent", "success", "Published and the notification email was sent (no amounts in the email)."),
        "EMAIL_FAILED": ("Email failed", "warning", "Published, but the notification email could not be delivered."),
        "VIEWED": ("Viewed", "success", "The employee opened the document."),
        "SUPERSEDED": ("Superseded", "neutral", "Replaced by a corrected document."),
        "REVERSED": ("Reversed", "danger", "Withdrawn because its run was reversed."),
    },
    "INVOICE": {
        "DRAFT": ("Draft", "neutral", "Being prepared."),
        "APPROVED": ("Approved", "info", "Approved, not issued yet."),
        "ISSUED": ("Issued", "info", "Issued. Not paid yet."),
        "PAID": ("Paid", "success", "Payment received."),
        "CREDITED": ("Credited", "neutral", "Cancelled by a credit note."),
    },
    "TASK": {
        "DRAFT": ("Draft", "neutral", "Not submitted yet."),
        "SUBMITTED": ("Submitted", "warning", "Waiting for approval."),
        "APPROVED": ("Approved", "success", "Approved and payable in the next run."),
        "REJECTED": ("Rejected", "danger", "Rejected. It will not be paid."),
        "DISPUTED": ("Disputed", "warning", "The outcome is disputed and under review."),
        "PAID": ("Paid in a run", "success", "Included in a finalized pay run."),
        "REVERSED": ("Reversed", "danger", "The payment for this task was reversed."),
    },
    "WAGE_INPUT": {
        "DRAFT": ("Draft", "neutral", "Not submitted yet."),
        "PENDING_APPROVAL": ("Awaiting approval", "warning", "Waiting for approval before payroll can use it."),
        "APPROVED": ("Approved", "success", "Approved. The next calculation will include it."),
        "REJECTED": ("Rejected", "danger", "Rejected. Payroll will not use it."),
        "CANCELLED": ("Cancelled", "neutral", "Cancelled. Payroll will not use it."),
        "CONSUMED": ("Used in a run", "primary", "Included in a finalized pay run."),
    },
    "CLAIM": {
        "SUBMITTED": ("Submitted", "warning", "Waiting for approval."),
        "APPROVED": ("Approved", "success", "Approved. It will be scheduled for payment."),
        "SCHEDULED": ("Scheduled", "info", "Scheduled for a pay run. Not paid yet."),
        "PAID": ("Paid in a run", "success", "Included in a finalized pay run."),
        "REJECTED": ("Rejected", "danger", "Rejected. It will not be paid."),
    },
    "LOAN": {
        "PENDING_APPROVAL": ("Awaiting approval", "warning", "Waiting for approval."),
        "APPROVED": ("Approved", "info", "Approved. Disbursement and recovery have not started."),
        "ACTIVE": ("Active", "success", "Being recovered through payroll."),
        "PAUSED": ("Paused", "warning", "Recovery is paused."),
        "CLOSED": ("Closed", "neutral", "Fully recovered or settled."),
        "REJECTED": ("Rejected", "danger", "Rejected."),
        "CANCELLED": ("Cancelled", "neutral", "Cancelled."),
    },
    "COMPENSATION_ASSIGNMENT": {
        "DRAFT": ("Draft", "neutral", "Not submitted yet. Payroll does not use it."),
        "PENDING_APPROVAL": ("Awaiting approval", "warning", "Waiting for approval. Payroll does not use it yet."),
        "SCHEDULED": ("Scheduled", "info", "Approved; takes effect on its effective date."),
        "ACTIVE": ("Active", "success", "The salary payroll uses now."),
        "SUPERSEDED": ("Superseded", "neutral", "Replaced by a later salary."),
        "REJECTED": ("Rejected", "danger", "Rejected. Payroll will not use it."),
    },
    "SETTING_VALUE": {
        "PENDING_APPROVAL": ("Awaiting approval", "warning", "A change waiting for approval. The current value still applies."),
        "ACTIVE": ("Active", "success", "In effect from its effective date."),
        "SUPERSEDED": ("Superseded", "neutral", "Replaced by a newer value."),
        "REJECTED": ("Rejected", "danger", "The change was rejected and never applied."),
    },
    "POLICY_VERSION": {
        "DRAFT": ("Draft", "neutral", "Not active. Editing it changes nothing until it is activated."),
        "ACTIVE": ("Active", "success", "In effect for its population from its effective date."),
        "SUPERSEDED": ("Superseded", "neutral", "Replaced by a newer version."),
    },
    "POLICY": {
        "ACTIVE": ("Active", "success", "The policy can be assigned and versioned."),
        "ARCHIVED": ("Archived", "neutral", "No longer used for new periods."),
    },
    "PACK_VERSION": {
        "DRAFT": ("Draft", "neutral", "Not reviewed. It never produces final statutory lines."),
        "UNDER_REVIEW": ("Under review", "warning", "Being checked by a qualified reviewer. Not verified."),
        "VERIFIED_FOR_PERIOD": ("Verified for period", "success", "Reviewed and verified for its effective period."),
        "EXPIRED": ("Expired", "neutral", "Its effective period has ended."),
        "DISABLED": ("Disabled", "danger", "Withdrawn. It is not used."),
    },
    "STATUTORY_REPORT": {
        "DRAFT": ("Draft", "neutral", "Not calculated yet."),
        "CALCULATED": ("Calculated", "info", "Calculated, not reviewed."),
        "REVIEW_REQUIRED": ("Review required", "warning", "Needs a reviewer before it can be filed."),
        "VERIFIED": ("Verified", "success", "Reviewed and verified. Not filed yet."),
        "READY_TO_FILE": ("Ready to file", "info", "Ready to submit to the authority. Not filed yet."),
        "SUBMITTED": ("Submitted", "info", "Submitted to the authority. Acceptance is not confirmed."),
        "ACCEPTED": ("Accepted", "success", "The authority accepted the filing."),
        "REJECTED": ("Rejected", "danger", "The authority rejected the filing. Correct it and resubmit."),
        "AMENDED": ("Amended", "neutral", "Replaced by an amended filing."),
    },
    "PAY_PERIOD": {
        "OPEN": ("Open", "success", "Accepting inputs."),
        "PROCESSING": ("Processing", "info", "A run for this period is being processed; late inputs go to the next period or a correction."),
        "CLOSED": ("Closed", "neutral", "Closed. Changes need a correction run."),
    },
}

# What an employee sees for a payment (3.7 §8.1): label key -> (short label, tone, glossary).
EMPLOYEE_PAYMENT: dict[str, L] = {
    "SCHEDULED": ("Scheduled", "info", "Your payment is scheduled. It has not been sent yet."),
    "PROCESSING": ("Processing", "info", "Your payment is on its way. It is not confirmed as received yet."),
    "PAID": ("Paid", "success", "The bank confirmed your payment."),
    "PARTLY_PAID": ("Partly paid", "warning", "Only part of your pay is confirmed received."),
    "ON_HOLD": ("On hold", "warning", "Your payment is on hold. HR will contact you."),
    "FAILED": ("Payment failed", "danger", "Your payment failed. Please check your bank details."),
    "SENT_AGAIN": ("Sent again", "info", "A failed payment was sent again."),
    "REVERSED": ("Reversed", "danger", "The payment was reversed."),
    "CANCELLED": ("Cancelled", "neutral", "This payment was cancelled."),
    "SUBMITTED_MANUAL_UNVERIFIED": ("Submitted — unverified", "warning",
                                    "Submitted for manual payment. Not confirmed as received."),
}

# Generic job vocabulary (3.9 §6). Job statuses are not a state machine of state_machines.py; they
# are listed here so StatusChip covers them too.
JOB: dict[str, L] = {
    "QUEUED": ("Queued", "neutral", "Waiting to start."),
    "RUNNING": ("Running", "info", "In progress. You can leave this page and come back."),
    "SUCCEEDED": ("Done", "success", "Finished successfully."),
    "FAILED": ("Failed", "danger", "The job failed. Retry it or contact support."),
    "DEAD": ("Stopped", "danger", "The job failed repeatedly and was stopped."),
    "CANCELLED": ("Cancelled", "neutral", "The job was cancelled."),
}


def _check() -> None:
    errors: list[str] = []
    if set(VOCABULARY_KEYS) != set(sm.MACHINES):
        errors.append(f"vocabulary keys {sorted(set(VOCABULARY_KEYS) ^ set(sm.MACHINES))} do not match MACHINES")
    for name, machine in sm.MACHINES.items():
        labels = LABELS.get(name, {})
        missing = sorted(machine.states - labels.keys())
        extra = sorted(labels.keys() - machine.states)
        if missing:
            errors.append(f"{name}: states without a label {missing}")
        if extra:
            errors.append(f"{name}: labels for unknown states {extra}")
    if set(EMPLOYEE_PAYMENT) != set(sm.EMPLOYEE_LABEL_TEXT):
        errors.append(f"employee payment labels {sorted(set(EMPLOYEE_PAYMENT) ^ set(sm.EMPLOYEE_LABEL_TEXT))} differ")
    for group in (*LABELS.values(), EMPLOYEE_PAYMENT, JOB):
        for state, (label, tone, glossary) in group.items():
            if tone not in TONES or not label or not glossary:
                errors.append(f"{state}: bad label entry {(label, tone, glossary)}")
    if errors:
        raise SystemExit("export_payroll_catalogues: " + "; ".join(errors))


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def _rows(rows: list[Any]) -> str:
    """One compact JSON value per line, so the generated file stays reviewable."""
    return "[\n" + "".join(f"  {json.dumps(row, ensure_ascii=False)},\n" for row in rows) + "]"


def _nested(mapping: dict[str, dict[str, Any]]) -> str:
    """A two-level mapping with one compact line per inner entry."""
    out = ["{"]
    for key, inner in mapping.items():
        out.append(f"  {json.dumps(key)}: {{")
        out.extend(f"    {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}," for k, v in inner.items())
        out.append("  },")
    out.append("}")
    return "\n".join(out)


def _entry(entry: L, **extra: Any) -> dict[str, Any]:
    label, tone, glossary = entry
    return {"label": label, "tone": tone, "glossary": glossary, **extra}


def _currencies() -> list[dict[str, Any]]:
    rows = []
    for info in currencies.all():
        increment = info.cash_rounding_increment
        rows.append({
            "code": info.code,
            "numeric": info.numeric,
            "name": info.name,
            "minor_unit": info.minor_unit,
            "status": info.status,
            "is_fund": info.is_fund,
            "payroll_allowed": info.payroll_allowed,
            "cash_rounding_increment": None if increment is None else format(increment, "f"),
            "replaced_by": info.replaced_by,
        })
    return rows


def _vocabularies() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in sorted(sm.MACHINES, key=lambda n: VOCABULARY_KEYS[n]):
        machine = sm.MACHINES[name]
        labels = LABELS[name]
        out[VOCABULARY_KEYS[name]] = {
            "machine": name,
            "initial": sorted(machine.initial_states),
            "side": sorted(machine.side_states),
            "terminal": sorted(machine.terminal_states),
            "states": {state: _entry(labels[state]) for state in sorted(machine.states)},
        }
    out["employee_payment"] = {
        "machine": None,
        "initial": [],
        "side": [],
        "terminal": [],
        "states": {key: _entry(EMPLOYEE_PAYMENT[key], text=sm.EMPLOYEE_LABEL_TEXT[key])
                   for key in sorted(EMPLOYEE_PAYMENT)},
    }
    out["job"] = {
        "machine": None,
        "initial": ["QUEUED"],
        "side": [],
        "terminal": ["CANCELLED", "DEAD", "FAILED", "SUCCEEDED"],
        "states": {key: _entry(JOB[key]) for key in sorted(JOB)},
    }
    return dict(sorted(out.items()))


def _employee_payment_table() -> dict[str, Any]:
    """status -> channel ("NONE" before submission) -> evidence -> {key, badge} | null (not allowed)."""
    channels: list[str | None] = [None, *sorted(c.value for c in sm.SubmissionChannel)]
    evidence = sorted(e.value for e in sm.EvidenceStatus)
    table: dict[str, Any] = {}
    for status in sorted(sm.INSTRUCTION_EMPLOYEE_LABELS):
        per_channel: dict[str, Any] = {}
        for channel in channels:
            per_evidence: dict[str, Any] = {}
            for proof in evidence:
                try:
                    label = sm.employee_label(status, channel, proof)
                except sm.InvalidTransition:
                    per_evidence[proof] = None
                else:
                    per_evidence[proof] = {"key": label.key, "badge": label.badge}
            per_channel[channel or "NONE"] = per_evidence
        table[status] = per_channel
    return table


def render() -> str:
    _check()
    vocab_keys = sorted([*VOCABULARY_KEYS.values(), "employee_payment", "job"])
    stage_labels = {str(k): v for k, v in sorted(sm.UI_STAGE_LABELS.items())}
    lines = [
        "// AUTO-GENERATED by hrms-backend/scripts/export_payroll_catalogues.py - do not edit by hand.",
        "// Sources of truth: hrms-backend/app/payroll/currencies.py (ISO 4217 List One "
        f"{currencies.LIST_ONE_PUBLISHED.isoformat()}) and app/payroll/state_machines.py.",
        "",
        f'export const ISO_4217_PUBLISHED = "{currencies.LIST_ONE_PUBLISHED.isoformat()}";',
        "",
        'export type StatusTone = "neutral" | "primary" | "success" | "warning" | "danger" | "info";',
        "",
        "export interface CurrencyInfo {",
        "  code: string;",
        "  /** ISO 4217 numeric code (\"356\"), or null when ISO assigns none. */",
        "  numeric: string | null;",
        "  name: string;",
        "  /** ISO 4217 minor unit (0 JPY, 2 INR, 3 IQD); null for non-monetary codes (XAU, XXX) and unreviewed codes. */",
        "  minor_unit: number | null;",
        '  status: "ACTIVE" | "HISTORIC";',
        "  is_fund: boolean;",
        "  /** False for funds, metals, historic and unreviewed codes: never selectable as a pay currency. */",
        "  payroll_allowed: boolean;",
        "  /** Cash payments only (3.7 §7.3): smallest cash amount as a decimal string, e.g. \"0.05\". */",
        "  cash_rounding_increment: string | null;",
        "  replaced_by: string | null;",
        "}",
        "",
        f"export const CURRENCY_LIST: CurrencyInfo[] = {_rows(_currencies())};",
        "",
        "export const CURRENCIES: Record<string, CurrencyInfo> = Object.fromEntries(CURRENCY_LIST.map((c) => [c.code, c]));",
        "",
        "export interface StatusEntry {",
        "  label: string;",
        "  tone: StatusTone;",
        "  /** Plain-language meaning, shown as the glossary tooltip. */",
        "  glossary: string;",
        "  /** Employee payment labels only: the full text, with {date} and {amount} placeholders. */",
        "  text?: string;",
        "}",
        "",
        "export interface Vocabulary {",
        "  /** Backend state machine name, or null for derived vocabularies (employee payment, job). */",
        "  machine: string | null;",
        "  initial: string[];",
        "  /** Side states are shown as banners, not as a stage (e.g. CANCELLED runs). */",
        "  side: string[];",
        "  terminal: string[];",
        "  states: Record<string, StatusEntry>;",
        "}",
        "",
        f"export type VocabularyName = {' | '.join(json.dumps(k) for k in vocab_keys)};",
        "",
        f"export const VOCABULARIES: Record<VocabularyName, Vocabulary> = {_json(_vocabularies())};",
        "",
        "/** The six friendly run stages (3.5 §4.2). */",
        f"export const RUN_UI_STAGE_LABELS: Record<number, string> = {_json(stage_labels)};",
        "/** Run state -> stage 1-6. Side states (CANCELLED, REOPENED_FOR_CORRECTION) have no stage. */",
        f"export const RUN_STATE_STAGE: Record<string, number> = {_json(dict(sorted(sm.RUN_UI_STAGES.items())))};",
        f"export const RUN_SIDE_STATES: string[] = {_json(sorted(sm.RUN_SIDE_STATES))};",
        f"export const RUN_FINALIZED_STATES: string[] = {_json(sorted(sm.RUN_FINALIZED_STATES))};",
        "",
        f"export const SUBMISSION_CHANNELS = {_json(sorted(c.value for c in sm.SubmissionChannel))} as const;",
        "export type SubmissionChannel = (typeof SUBMISSION_CHANNELS)[number];",
        f"export const EVIDENCE_STATUSES = {_json(sorted(e.value for e in sm.EvidenceStatus))} as const;",
        "export type EvidenceStatus = (typeof EVIDENCE_STATUSES)[number];",
        "",
        "/**",
        " * Employee payment label of an instruction: status -> submission channel (\"NONE\" before",
        " * submission) -> evidence -> label key and badge, computed by the backend's employee_label().",
        " * null means the combination is invalid (an in-flight or settled status needs a channel).",
        " */",
        "export const EMPLOYEE_PAYMENT_LABELS: Record<string, Record<string, Record<string, { key: string; badge: string | null } | null>>> = "
        f"{_nested(_employee_payment_table())};",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(), encoding="utf-8", newline="\n")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()

"""Strict request bodies of the assistant API and the model-facing tool argument schemas (docs/leave_agent.md §11).

Every model is ``extra="forbid"``: unknown fields fail before any domain code runs. Tool schemas never contain
identity, tenant, role or secret fields (``FORBIDDEN_TOOL_FIELDS``; tests/agent assert it for every registered tool):
company, user and employee come only from the authenticated ``Principal`` captured in the tool closure. Dates cross
the tool boundary as ISO ``YYYY-MM-DD`` strings and are parsed strictly (no timestamps, no locale formats).
"""

from datetime import date
from typing import Literal, Optional

from pydantic import Field, field_validator

from app.schemas.auth import StrictModel

FORBIDDEN_TOOL_FIELDS = frozenset({
    "company_id", "user_id", "employee_id", "role", "roles", "permission", "permissions", "is_admin",
    "database_url", "access_token", "session_id", "approver", "approver_id", "tenant", "tenant_id", "api_key",
    "token", "cookie", "password", "scope",
})
ISO_DATE = r"^\d{4}-\d{2}-\d{2}$"
CLIENT_ID = r"^[A-Za-z0-9._:\-]+$"


def parse_iso_date(value: str) -> date:
    """Strict ISO calendar date (``2026-02-30`` and ``2026-1-5`` are refused)."""
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("Use a real calendar date in YYYY-MM-DD format.")


# ----------------------------------------------------------------------------------------------- API bodies
class ThreadCreate(StrictModel):
    module: Literal["leave"] = "leave"
    title: Optional[str] = Field(None, max_length=120)


class ChatTurnCreate(StrictModel):
    # The configured limit (AGENT_MESSAGE_MAX_CHARS, at most 10000) is enforced by the service with a clear code.
    content: str = Field(..., min_length=1, max_length=10000)
    client_message_id: str = Field(..., min_length=8, max_length=100, pattern=CLIENT_ID)


class ActionConfirm(StrictModel):
    confirmation_token: str = Field(..., min_length=20, max_length=500)


# ----------------------------------------------------------------------------------------------- tool arguments
class NoArgs(StrictModel):
    """A tool without arguments."""


class LeaveBalanceArgs(StrictModel):
    on_date: Optional[str] = Field(None, pattern=ISO_DATE,
                                   description="Optional YYYY-MM-DD date to read the balance on; default today.")

    @field_validator("on_date")
    @classmethod
    def _date(cls, value):
        if value is not None:
            parse_iso_date(value)
        return value


class LeaveRequestsArgs(StrictModel):
    status: Optional[Literal["PENDING", "APPROVED", "REJECTED", "CANCELLED", "WITHDRAWN", "RETURNED", "DRAFT"]] = Field(
        None, description="Only requests in this status.")
    limit: int = Field(10, ge=1, le=20, description="How many of the latest requests to return (1-20).")


class LeaveApplyArgs(StrictModel):
    leave_type: str = Field(..., min_length=1, max_length=60, pattern=r"^[A-Za-z0-9 _.()\-]+$",
                            description="Leave type code from get_leave_types (its exact name is accepted too).")
    start_date: str = Field(..., pattern=ISO_DATE, description="First day, YYYY-MM-DD.")
    end_date: str = Field(..., pattern=ISO_DATE, description="Last day, YYYY-MM-DD (same as start for one day).")
    start_portion: Literal["FULL", "SECOND_HALF"] = Field(
        "FULL", description="FULL, or SECOND_HALF to start in the afternoon of the first day.")
    end_portion: Literal["FULL", "FIRST_HALF"] = Field(
        "FULL", description="FULL, or FIRST_HALF to end at midday of the last day.")
    reason: Optional[str] = Field(None, max_length=1000, description="Reason in the user's words, if given.")

    @field_validator("start_date", "end_date")
    @classmethod
    def _dates(cls, value):
        parse_iso_date(value)
        return value


class LeaveCancelArgs(StrictModel):
    leave_request_id: int = Field(..., ge=1, le=2_147_483_647,
                                  description="Id of one of the user's own leave requests (from get_my_leave_requests).")
    reason: Optional[str] = Field(None, max_length=1000, description="Why the leave is cancelled, if given.")


class ApprovalsArgs(StrictModel):
    limit: int = Field(10, ge=1, le=20, description="How many waiting requests to return (1-20).")


class LeaveDecisionArgs(StrictModel):
    leave_request_id: int = Field(..., ge=1, le=2_147_483_647,
                                  description="Id of a request from get_my_pending_approvals.")
    remarks: Optional[str] = Field(None, max_length=1000, description="Note for the employee.")

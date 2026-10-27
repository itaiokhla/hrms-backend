"""Per-request correlation values (LH-M0 WI-0.5): the request id and user agent recorded on audit rows.

Set by the request middleware in app/main.py; read by AuditLogService.log when the caller passes none. Outside a
request (jobs, CLI, tests calling services directly) both are None.
"""

import re
import uuid
from contextvars import ContextVar
from typing import Optional

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
USER_AGENT_MAX = 300

request_id: ContextVar[Optional[str]] = ContextVar("hrms_request_id", default=None)
user_agent: ContextVar[Optional[str]] = ContextVar("hrms_user_agent", default=None)


def accept_request_id(value: Optional[str]) -> str:
    """The client's X-Request-ID when it is 8-64 letters, digits or dashes, else a new random id."""
    if value and _VALID_ID.fullmatch(value):
        return value
    return uuid.uuid4().hex


def clean_user_agent(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    printable = "".join(ch for ch in value if ch.isprintable())
    return printable[:USER_AGENT_MAX] or None

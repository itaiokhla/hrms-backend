"""Fixtures of the assistant tests: the leave v2 world (``acme``: Mon 2026-10-19 09:00 IST fake clock, LEGACY_V1 until
``office_plan`` + ``go_live``), a second tenant and the agent switch (``enable_agent``)."""

import pytest

from tests.agent.agent_helpers import enable_agent  # noqa: F401  (fixture)
from tests.attendance_helpers import fake_clock  # noqa: F401  (fixture)
from tests.helpers import create_employee, invite_and_activate, signup_company
from tests.leave.test_v2_core import acme  # noqa: F401  (fixture)


@pytest.fixture
def beta(app):
    admin = signup_company(app, "Beta Agent")
    worker = create_employee(admin, "Bela", "Brown")
    return {"admin": admin, "cid": admin.company_id, "worker": invite_and_activate(app, admin, worker, ["employee"]),
            "worker_id": worker["id"]}

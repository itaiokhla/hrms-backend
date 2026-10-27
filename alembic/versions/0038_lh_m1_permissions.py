"""LH-M1 holiday permissions (LH 14 WI-1.6; LH 06 §3.1; SPINE V3-081): registry 2026.10.5.

The registry (code) now offers ``holiday:manage`` at LEGAL_ENTITY, BRANCH and COMPANY, adds ``holiday:publish``
(LE/BR/COMPANY, restricted: publishing years, amendments, backdated work-week and link changes) and
``holiday:choose_optional`` (SELF, part of the self-service set). This revision seeds the grants so behaviour is
preserved (P-grants):

* ``holiday:publish`` COMPANY for every role (system or custom) that holds ``holiday:manage`` COMPANY;
* ``holiday:choose_optional`` SELF for every seeded system role built on the self-service grants.

Holders' ``permission_version`` is bumped so sessions pick the change up. Downgrade removes exactly these grants.

Revision ID: 0038_lh_m1_permissions
Revises: 0037_lh_m1_work_weeks_links
Create Date: 2026-10-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0038_lh_m1_permissions"
down_revision: Union[str, None] = "0037_lh_m1_work_weeks_links"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen: the system roles built on SELF_SERVICE when this revision was written.
SELF_SERVICE_ROLES = ("employee", "hr_admin", "manager", "payroll_manager")


def _bump(bind, company_id: int, role_id: int) -> None:
    bind.execute(sa.text(
        "UPDATE users SET permission_version = permission_version + 1 WHERE company_id = :cid AND id IN "
        "(SELECT user_id FROM user_role_assignments WHERE company_id = :cid AND role_id = :r)"),
        {"cid": company_id, "r": role_id})


def _add(bind, company_id: int, role_id: int, code: str, scope: str) -> bool:
    exists = bind.execute(sa.text(
        "SELECT 1 FROM role_permissions WHERE role_id = :r AND permission_code = :c AND scope = :s"),
        {"r": role_id, "c": code, "s": scope}).first()
    if exists:
        return False
    bind.execute(sa.text(
        "INSERT INTO role_permissions (company_id, role_id, permission_code, scope, created_at, updated_at) "
        "VALUES (:cid, :r, :c, :s, now(), now())"), {"cid": company_id, "r": role_id, "c": code, "s": scope})
    return True


def upgrade() -> None:
    bind = op.get_bind()
    publishers = bind.execute(sa.text(
        "SELECT DISTINCT r.id, r.company_id FROM roles r JOIN role_permissions p ON p.role_id = r.id "
        "WHERE p.permission_code = 'holiday:manage' AND p.scope = 'COMPANY' ORDER BY r.id")).all()
    for role_id, company_id in publishers:
        if _add(bind, company_id, role_id, "holiday:publish", "COMPANY"):
            _bump(bind, company_id, role_id)
    for key in SELF_SERVICE_ROLES:
        roles = bind.execute(sa.text(
            "SELECT id, company_id FROM roles WHERE system_key = :k AND is_system_role = true ORDER BY id"),
            {"k": key}).all()
        for role_id, company_id in roles:
            if _add(bind, company_id, role_id, "holiday:choose_optional", "SELF"):
                _bump(bind, company_id, role_id)


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(
        "DELETE FROM role_permissions WHERE permission_code IN ('holiday:publish', 'holiday:choose_optional')"))
    bind.execute(sa.text(
        "DELETE FROM role_permissions WHERE permission_code = 'holiday:manage' AND scope IN ('LEGAL_ENTITY', 'BRANCH')"))
    bind.execute(sa.text("UPDATE users SET permission_version = permission_version + 1"))

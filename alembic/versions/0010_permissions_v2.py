"""Permissions v2 data: payroll roles for existing tenants, payroll_manager for existing administrators, permission_version bump.

Data only and idempotent (3.9 §5.2-§5.3, contract §9, EC-MIG-07, EC-MIG-08). Registry 2026.10.4 stops
Company Administrator full access from implying payroll-confidential permissions, so for every tenant:
  * the new default roles payroll_processor, payroll_approver and finance_payments are created with
    their grants (payroll_manager too, if a tenant deleted it); a name already taken by a custom role
    gets the system key appended;
  * the system roles hr_admin, payroll_manager, manager, employee and auditor receive the grants the
    2026.10.4 defaults added (payroll self-service, org, overtime and task codes, payroll v2 codes);
    rows already present are left alone and nothing is removed;
  * custom roles holding legacy payroll codes (payroll:read/prepare/approve/process/export,
    compensation:read/manage, still valid as deprecated aliases) receive the v2 equivalents of the
    3.9 §5.2 mapping; their existing rows are never touched;
  * every holder of the protected Company Administrator role is granted payroll_manager, so nobody
    loses the payroll access they use today, with one "Payroll Roles" ROLE_GRANTED audit row per grant
    (actor_type system, EC-MIG-08);
  * the default descriptions of company_admin and payroll_manager are updated unless customised;
  * users.permission_version is bumped for every user of every tenant touched (EC-SEC-14).

The grant tables are literals (a frozen copy of registry 2026.10.4), so later registry changes never
change what this migration did. Each role change and each grant is recorded in audit_logs.details
({"migration": "0010_permissions_v2", ...}); downgrade() reads those rows and removes exactly what
upgrade added (created roles, added grants, the administrators' payroll_manager assignments), then
writes a PERMISSIONS_V2_REVERTED row so a later downgrade ignores rows of an earlier, reverted run.
Audit rows themselves are append-only and stay.

Revision ID: 0010_permissions_v2
Revises: 0009_payroll_platform
Create Date: 2026-10-20
"""
import json
import logging
from typing import Dict, List, Sequence, Set, Tuple, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0010_permissions_v2'
down_revision: Union[str, None] = '0009_payroll_platform'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.0010")

MARKER = "0010_permissions_v2"
ROLE_MODULE = "Roles"
PAYROLL_ROLES_MODULE = "Payroll Roles"
MIGRATED = "PERMISSIONS_MIGRATED"
REVERTED = "PERMISSIONS_V2_REVERTED"
GRANTED = "ROLE_GRANTED"
REVOKED = "ROLE_REVOKED"

Grant = Tuple[str, str]
S, C, RT = "SELF", "COMPANY", "REPORTING_TREE"


def _c(*codes: str) -> Tuple[Grant, ...]:
    return tuple((code, C) for code in codes)


PAYROLL_SELF_SERVICE: Tuple[Grant, ...] = tuple((code, S) for code in (
    "payroll:payslip:read", "payroll:compensation:read", "payroll:bank_details:read_masked", "payroll:bank_details:edit",
    "payroll:tax_profile:read", "payroll:loans:read", "payroll:reimbursements:read", "overtime:request", "task:read",
))
PAYROLL_PROCESSOR: Tuple[Grant, ...] = _c(
    "payroll:run:read", "payroll:run:create", "payroll:run:calculate", "payroll:run:review",
    "payroll:inputs:read", "payroll:inputs:edit", "payroll:inputs:import", "payroll:inputs:approve",
    "payroll:compensation:read", "payroll:compensation:edit", "payroll:salary_structure:read",
    "payroll:salary_structure:edit", "payroll:payslip:read", "payroll:bank_details:read_masked",
    "payroll:bank_details:edit", "payroll:loans:read", "payroll:loans:manage", "payroll:loans:approve",
    "payroll:reimbursements:read", "payroll:reimbursements:manage", "payroll:reimbursements:approve",
    "payroll:tax_profile:verify", "payroll:reports:read", "payroll:reports:export", "payroll:configuration:read",
)
PAYROLL_MANAGER_EXTRA: Tuple[Grant, ...] = _c(
    "payroll:run:finalize", "payroll:run:reverse", "payroll:run:close", "payroll:configuration:write",
    "payroll:payslip:publish", "payroll:statutory:review", "payroll:statutory:submit",
)

# Grants each existing system role receives (2026.10.4 defaults minus 2026.10.3 + migration 0006).
SYSTEM_ROLE_ADDITIONS: Dict[str, Tuple[Grant, ...]] = {
    "hr_admin": PAYROLL_SELF_SERVICE + _c("org:manage_entities", "org:manage_dimensions", "overtime:approve", "task:approve"),
    "payroll_manager": PAYROLL_SELF_SERVICE + PAYROLL_PROCESSOR + PAYROLL_MANAGER_EXTRA,
    "manager": PAYROLL_SELF_SERVICE + (("overtime:approve", RT), ("task:approve", RT)),
    "employee": PAYROLL_SELF_SERVICE,
    "auditor": _c("payroll:audit:read", "payroll:reports:read"),
}

# payroll_manager as seeded by registry 2026.10.3 (self-service + legacy payroll), for tenants that deleted it.
_LEGACY_SELF_SERVICE: Tuple[Grant, ...] = (
    ("employee:read", S), ("employee:read_confidential", S), ("profile:update", S), ("attendance:read", S),
    ("attendance:mark", S), ("location:read", S), ("leave:read", S), ("leave:apply", S), ("holiday:read", C),
    ("payroll:read", S), ("compensation:read", S), ("asset:read", S), ("asset:acknowledge", S),
)
PAYROLL_MANAGER_2026_10_3: Tuple[Grant, ...] = _LEGACY_SELF_SERVICE + _c(
    "employee:read", "payroll:read", "payroll:prepare", "payroll:approve", "payroll:process", "payroll:export",
    "compensation:read", "compensation:manage", "report:read", "report:export",
)

PAYROLL_MANAGER_DESCRIPTION = (
    "Runs payroll end to end: inputs, compensation, calculation, review, finalization, payslips, statutory "
    "filings and payroll settings. Approving runs needs the Payroll Approver role; the legacy payroll "
    "screens work as before."
)
NEW_ROLES: Dict[str, Tuple[str, str, Tuple[Grant, ...]]] = {
    "payroll_manager": (
        "Payroll Manager", PAYROLL_MANAGER_DESCRIPTION,
        PAYROLL_MANAGER_2026_10_3 + SYSTEM_ROLE_ADDITIONS["payroll_manager"],
    ),
    "payroll_processor": (
        "Payroll Processor",
        "Prepares payroll: inputs, compensation changes, calculation and review of runs, loans, claims and "
        "payroll reports. Cannot approve runs or release payments.",
        PAYROLL_PROCESSOR,
    ),
    "payroll_approver": (
        "Payroll Approver",
        "Approves payroll runs, compensation changes, salary structures and bank detail changes prepared by others.",
        _c("payroll:run:read", "payroll:run:approve", "payroll:compensation:read", "payroll:compensation:approve",
           "payroll:salary_structure:approve", "payroll:bank_details:approve", "payroll:payslip:read",
           "payroll:reports:read"),
    ),
    "finance_payments": (
        "Finance & Payments",
        "Prepares, approves, releases and reconciles salary payments of finalized runs. Sees masked bank details only.",
        _c("payroll:payment:prepare", "payroll:payment:approve", "payroll:payment:submit", "payroll:payment:reconcile",
           "payroll:funding_accounts:manage", "payroll:bank_details:read_masked", "payroll:run:read",
           "payroll:reports:read"),
    ),
}

# 3.9 §5.2: legacy grant -> v2 grants (custom roles only).
LEGACY_MAPPING: Dict[Grant, Tuple[Grant, ...]] = {
    ("payroll:read", S): (("payroll:payslip:read", S),),
    ("payroll:read", C): _c("payroll:run:read", "payroll:payslip:read", "payroll:reports:read"),
    ("payroll:prepare", C): _c("payroll:run:create", "payroll:run:calculate", "payroll:run:review",
                               "payroll:inputs:edit", "payroll:inputs:import"),
    ("payroll:approve", C): _c("payroll:run:approve"),
    ("payroll:process", C): _c("payroll:payment:prepare", "payroll:payment:submit"),
    ("payroll:export", C): _c("payroll:reports:export"),
    ("compensation:read", S): (("payroll:compensation:read", S),),
    ("compensation:read", C): _c("payroll:compensation:read"),
    ("compensation:manage", C): _c("payroll:compensation:edit", "payroll:salary_structure:edit"),
}

# (old default, new default) descriptions; customised descriptions are kept.
DESCRIPTIONS: Dict[str, Tuple[str, str]] = {
    "company_admin": (
        "Full control of this company's workspace. Only administrators can grant this role.",
        "Full control of this company's workspace except payroll data, which needs an explicit payroll role. "
        "Only administrators can grant this role.",
    ),
    "payroll_manager": (
        "Prepares, approves and pays payroll. Sees compensation for the whole company.",
        PAYROLL_MANAGER_DESCRIPTION,
    ),
}

EC_MIG_08 = (
    "Granted the Payroll Manager role to {email}: Company Administrators no longer see payroll data through "
    "full access (payroll confidentiality), so existing administrators keep today's payroll access through this "
    "explicit, removable role (EC-MIG-08)."
)


def legacy_equivalents(grants: Set[Grant]) -> Set[Grant]:
    """v2 grants a role holding `grants` should also hold under the 3.9 §5.2 mapping."""
    result: Set[Grant] = set()
    for grant in grants:
        result.update(LEGACY_MAPPING.get(grant, ()))
    return result


def _audit(bind, company_id: int, module: str, action: str, description: str, entity_type: str, entity_id, details: dict):
    bind.execute(sa.text(
        """
        INSERT INTO audit_logs (company_id, actor_type, actor_email, module, action, entity_type, entity_id,
                                description, details, created_at, updated_at)
        VALUES (:cid, 'system', 'migration 0010', :module, :action, :etype, :eid, :description,
                CAST(:details AS jsonb), now(), now())
        """
    ), {"cid": company_id, "module": module, "action": action, "etype": entity_type, "eid": str(entity_id),
        "description": description[:500], "details": json.dumps(dict(details, migration=MARKER))})


def _role_grants(bind, role_id: int) -> Set[Grant]:
    rows = bind.execute(sa.text("SELECT permission_code, scope FROM role_permissions WHERE role_id = :r"), {"r": role_id})
    return {(code, scope) for code, scope in rows}


def _add_grants(bind, company_id: int, role_id: int, grants) -> List[Grant]:
    missing = sorted(set(grants) - _role_grants(bind, role_id))
    for code, scope in missing:
        bind.execute(sa.text(
            "INSERT INTO role_permissions (company_id, role_id, permission_code, scope, created_at, updated_at) "
            "VALUES (:cid, :r, :code, :scope, now(), now())"
        ), {"cid": company_id, "r": role_id, "code": code, "scope": scope})
    return missing


def _free_name(bind, company_id: int, name: str, key: str) -> str:
    taken = bind.execute(sa.text("SELECT 1 FROM roles WHERE company_id = :cid AND lower(name) = lower(:n)"),
                         {"cid": company_id, "n": name}).first()
    return f"{name} ({key})" if taken else name


def _upgrade_company(bind, company_id: int) -> dict:
    stats = {"roles_created": 0, "grants_added": 0, "admins_granted": 0}
    roles = {key: rid for rid, key in bind.execute(sa.text(
        "SELECT id, system_key FROM roles WHERE company_id = :cid AND system_key IS NOT NULL"), {"cid": company_id})}

    def record(role_id: int, name: str, added: List[Grant], created: bool):
        if not added and not created:
            return
        stats["grants_added"] += len(added)
        _audit(bind, company_id, ROLE_MODULE, MIGRATED,
               f"Permissions v2: {'created role' if created else 'updated role'} '{name}' with {len(added)} new grant(s)",
               "role", role_id, {"role_id": role_id, "created": created, "added": [list(g) for g in added]})

    for key, (name, description, grants) in NEW_ROLES.items():
        if key in roles:
            continue
        role_id = bind.execute(sa.text(
            """
            INSERT INTO roles (company_id, name, description, system_key, is_system_role, is_protected, is_full_access,
                               created_at, updated_at)
            VALUES (:cid, :name, :description, :key, true, false, false, now(), now()) RETURNING id
            """
        ), {"cid": company_id, "name": _free_name(bind, company_id, name, key), "description": description,
            "key": key}).scalar()
        roles[key] = role_id
        stats["roles_created"] += 1
        record(role_id, name, _add_grants(bind, company_id, role_id, grants), True)

    for key, additions in SYSTEM_ROLE_ADDITIONS.items():
        if key in roles:
            record(roles[key], key, _add_grants(bind, company_id, roles[key], additions), False)

    custom = bind.execute(sa.text("SELECT id, name FROM roles WHERE company_id = :cid AND system_key IS NULL "
                                  "AND NOT is_full_access ORDER BY id"), {"cid": company_id}).all()
    for role_id, name in custom:
        record(role_id, name, _add_grants(bind, company_id, role_id, legacy_equivalents(_role_grants(bind, role_id))), False)

    manager_role = roles["payroll_manager"]
    admins = bind.execute(sa.text(
        """
        SELECT DISTINCT u.id, u.email FROM users u
        JOIN user_role_assignments a ON a.user_id = u.id
        JOIN roles r ON r.id = a.role_id
        WHERE u.company_id = :cid AND r.is_protected AND r.is_full_access
          AND (a.effective_until IS NULL OR a.effective_until > now())
          AND NOT EXISTS (SELECT 1 FROM user_role_assignments x WHERE x.user_id = u.id AND x.role_id = :role)
        ORDER BY u.id
        """
    ), {"cid": company_id, "role": manager_role}).all()
    for user_id, email in admins:
        assignment_id = bind.execute(sa.text(
            "INSERT INTO user_role_assignments (company_id, user_id, role_id, assigned_by_user_id, effective_from, "
            "created_at, updated_at) VALUES (:cid, :u, :r, NULL, now(), now(), now()) RETURNING id"
        ), {"cid": company_id, "u": user_id, "r": manager_role}).scalar()
        _audit(bind, company_id, PAYROLL_ROLES_MODULE, GRANTED, EC_MIG_08.format(email=email), "user", user_id,
               {"user_role_assignment_id": assignment_id, "role_id": manager_role, "role": "payroll_manager",
                "reason": "EC-MIG-08"})
        stats["admins_granted"] += 1

    for key, (old, new) in DESCRIPTIONS.items():
        bind.execute(sa.text("UPDATE roles SET description = :new, updated_at = now() "
                             "WHERE company_id = :cid AND system_key = :key AND description = :old"),
                     {"cid": company_id, "key": key, "old": old, "new": new})
    if any(stats.values()):
        bind.execute(sa.text("UPDATE users SET permission_version = permission_version + 1 WHERE company_id = :cid"),
                     {"cid": company_id})
    return stats


def apply(bind) -> Dict[int, dict]:
    """Run the upgrade for every tenant; returns per-company counts (used by the tests)."""
    result = {}
    for (company_id,) in bind.execute(sa.text("SELECT id FROM companies ORDER BY id")).all():
        result[company_id] = _upgrade_company(bind, company_id)
        if any(result[company_id].values()):
            logger.info("Company %s: permissions v2 %s", company_id, result[company_id])
    return result


def _marker_rows(bind, company_id: int):
    last_revert = bind.execute(sa.text(
        "SELECT max(id) FROM audit_logs WHERE company_id = :cid AND action = :a AND details->>'migration' = :m"
    ), {"cid": company_id, "a": REVERTED, "m": MARKER}).scalar() or 0
    return bind.execute(sa.text(
        "SELECT module, action, details FROM audit_logs WHERE company_id = :cid AND id > :after "
        "AND details->>'migration' = :m AND action IN (:migrated, :granted) ORDER BY id DESC"
    ), {"cid": company_id, "after": last_revert, "m": MARKER, "migrated": MIGRATED, "granted": GRANTED}).all()


def _revert_company(bind, company_id: int) -> dict:
    stats = {"assignments_removed": 0, "grants_removed": 0, "roles_deleted": 0}
    rows = _marker_rows(bind, company_id)
    if not rows:
        return stats
    for module, action, details in rows:
        if module == PAYROLL_ROLES_MODULE and action == GRANTED:
            removed = bind.execute(sa.text(
                "DELETE FROM user_role_assignments WHERE id = :id AND role_id = :r AND company_id = :cid RETURNING user_id"
            ), {"id": details["user_role_assignment_id"], "r": details["role_id"], "cid": company_id}).first()
            if removed:
                stats["assignments_removed"] += 1
                _audit(bind, company_id, PAYROLL_ROLES_MODULE, REVOKED,
                       "Permissions v2 rolled back: removed the Payroll Manager role granted by migration 0010",
                       "user", removed[0], {"role_id": details["role_id"], "reverted": True})
    for module, action, details in rows:
        if action != MIGRATED:
            continue
        role_id = details["role_id"]
        if details.get("created"):
            savepoint = bind.begin_nested()
            try:
                bind.execute(sa.text("DELETE FROM user_role_assignments WHERE role_id = :r"), {"r": role_id})
                deleted = bind.execute(sa.text("DELETE FROM roles WHERE id = :r AND company_id = :cid"),
                                       {"r": role_id, "cid": company_id}).rowcount
                savepoint.commit()
                stats["roles_deleted"] += deleted
                continue
            except sa.exc.IntegrityError:
                savepoint.rollback()
                logger.warning("Company %s: role %s is referenced and was kept; removing its migrated grants",
                               company_id, role_id)
        for code, scope in details.get("added", []):
            stats["grants_removed"] += bind.execute(sa.text(
                "DELETE FROM role_permissions WHERE role_id = :r AND permission_code = :code AND scope = :scope"
            ), {"r": role_id, "code": code, "scope": scope}).rowcount
    for key, (old, new) in DESCRIPTIONS.items():
        bind.execute(sa.text("UPDATE roles SET description = :old, updated_at = now() "
                             "WHERE company_id = :cid AND system_key = :key AND description = :new"),
                     {"cid": company_id, "key": key, "old": old, "new": new})
    bind.execute(sa.text("UPDATE users SET permission_version = permission_version + 1 WHERE company_id = :cid"),
                 {"cid": company_id})
    _audit(bind, company_id, ROLE_MODULE, REVERTED, "Permissions v2 rolled back (migration 0010 downgrade)",
           "company", company_id, dict(stats))
    return stats


def revert(bind) -> Dict[int, dict]:
    return {cid: _revert_company(bind, cid) for (cid,) in bind.execute(sa.text("SELECT id FROM companies ORDER BY id")).all()}


def upgrade() -> None:
    apply(op.get_bind())


def downgrade() -> None:
    revert(op.get_bind())

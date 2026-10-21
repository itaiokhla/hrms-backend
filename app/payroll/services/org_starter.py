"""Default payroll organisation for a new tenant (02-multi-level-company §12-§13; 3.4 §3, §8).

Called once from provisioning, after the default legal entity, branch and the founding
administrator's placement exist. Creates, unless already present:
  * pay group MONTHLY for the default legal entity, in the entity's base currency (only when it is a
    payroll currency), periods from the 1st, paid on the last working day;
  * work week MON_FRI (Saturday and Sunday off, what legacy payroll assumes; editable), the default
    entity's and every branch's work week;
  * holiday calendar COMPANY, linked to every branch without one;
  * employment type PERMANENT;
and sets them on placements that have none (at signup: the founder's). Existing tenants got the same
rows from migration 0008.
"""

from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.employee import Employee
from app.models.org import Branch, EmployeeAssignment, LegalEntity
from app.models.pay_org import PayGroup
from app.payroll import currencies
from app.payroll.money_errors import CurrencyError
from app.payroll.services import org_dimensions
from app.services.audit_log_service import AuditLogService


def provision(db: Session, company: Company) -> None:
    cid = company.id
    rows = org_dimensions.ensure_starter_rows(db, cid)
    week, calendar, kind = rows["work_week"], rows["holiday_calendar"], rows["employment_type"]
    entity = (
        db.query(LegalEntity).filter(LegalEntity.company_id == cid)
        .order_by(LegalEntity.is_default.desc(), LegalEntity.id).first()
    )
    group = None
    if entity is not None:
        if entity.default_work_week_pattern_id is None:
            entity.default_work_week_pattern_id = week.id
        group = org_dimensions.default_pay_group(db, cid, entity.id)
        if group is None and _payroll_currency(entity.base_currency) and db.query(PayGroup.id).filter(
            PayGroup.company_id == cid, PayGroup.code == org_dimensions.DEFAULT_PAY_GROUP
        ).first() is None:
            group = PayGroup(
                company_id=cid, legal_entity_id=entity.id, code=org_dimensions.DEFAULT_PAY_GROUP, name="Monthly",
                currency=entity.base_currency, pay_frequency="MONTHLY", calendar_rules=dict(org_dimensions.MONTHLY_RULES),
                status="ACTIVE", description="Starter pay group: monthly payroll in the legal entity's base currency.",
            )
            db.add(group)
    for branch in db.query(Branch).filter(Branch.company_id == cid):
        branch.work_week_pattern_id = branch.work_week_pattern_id or week.id
        branch.holiday_calendar_id = branch.holiday_calendar_id or calendar.id
    db.flush()

    placed = 0
    for assignment in db.query(EmployeeAssignment).filter(EmployeeAssignment.company_id == cid,
                                                          EmployeeAssignment.is_primary.is_(True)):
        before = (assignment.pay_group_id, assignment.employment_type_id, assignment.work_week_pattern_id,
                  assignment.shift_id)
        if assignment.pay_group_id is None and group is not None and assignment.legal_entity_id == group.legal_entity_id:
            assignment.pay_group_id = group.id
        assignment.employment_type_id = assignment.employment_type_id or kind.id
        if assignment.work_week_pattern_id is None:
            branch = db.get(Branch, assignment.branch_id)
            assignment.work_week_pattern_id = (branch.work_week_pattern_id if branch else None) or week.id
        if assignment.shift_id is None:
            assignment.shift_id = db.query(Employee.shift_id).filter(Employee.id == assignment.employee_id).scalar()
        placed += before != (assignment.pay_group_id, assignment.employment_type_id, assignment.work_week_pattern_id,
                             assignment.shift_id)
    db.flush()
    AuditLogService.log(
        db, None, org_dimensions.AUDIT_MODULE, "ORG_STARTER_PROVISIONED",
        "Created the starter payroll organisation: pay group MONTHLY, work week MON_FRI, holiday calendar COMPANY "
        "and employment type PERMANENT (all editable).",
        "company", cid, company_id=cid, actor_type="system",
        details={"pay_group_id": group.id if group else None, "work_week_pattern_id": week.id,
                 "holiday_calendar_id": calendar.id, "employment_type_id": kind.id, "placements_completed": placed},
    )


def _payroll_currency(code: str) -> bool:
    try:
        currencies.require_payroll_currency(code)
    except CurrencyError:
        return False
    return True

"""Reporting-hierarchy queries.

The primary reporting line (employees.manager_id) defines the organisation
tree. Trees are traversed with recursive CTEs evaluated in the database, so
no request ever loads a whole company hierarchy into memory.
"""

from datetime import date
from typing import List, Optional

from sqlalchemy import Select, literal, or_, select
from sqlalchemy.orm import Session, aliased

from app.models.employee import Employee, SecondaryReportingLine

MAX_DEPTH = 64


def descendants_select(company_id: int, root_employee_id: int) -> Select:
    """SELECT of every employee below root in the primary tree (root excluded)."""
    base = select(Employee.id.label("id"), literal(1).label("depth")).where(
        Employee.company_id == company_id, Employee.manager_id == root_employee_id
    )
    tree = base.cte(recursive=True)
    child = aliased(Employee)
    tree = tree.union_all(
        select(child.id, tree.c.depth + 1).where(
            child.company_id == company_id,
            child.manager_id == tree.c.id,
            tree.c.depth < MAX_DEPTH,
        )
    )
    return select(tree.c.id)


def direct_reports_select(company_id: int, manager_employee_id: int) -> Select:
    return select(Employee.id).where(
        Employee.company_id == company_id, Employee.manager_id == manager_employee_id
    )


def secondary_reports_select(company_id: int, manager_employee_id: int, on: Optional[date] = None) -> Select:
    on = on or date.today()
    return select(SecondaryReportingLine.employee_id).where(
        SecondaryReportingLine.company_id == company_id,
        SecondaryReportingLine.manager_id == manager_employee_id,
        SecondaryReportingLine.effective_from <= on,
        or_(SecondaryReportingLine.effective_until.is_(None), SecondaryReportingLine.effective_until >= on),
    )


def management_chain(db: Session, company_id: int, employee_id: int) -> List[int]:
    """Primary managers above an employee, nearest first."""
    start = select(Employee.manager_id.label("id"), literal(1).label("depth")).where(
        Employee.company_id == company_id, Employee.id == employee_id
    )
    chain = start.cte(recursive=True)
    parent = aliased(Employee)
    chain = chain.union_all(
        select(parent.manager_id, chain.c.depth + 1).where(
            parent.company_id == company_id,
            parent.id == chain.c.id,
            chain.c.depth < MAX_DEPTH,
        )
    )
    rows = db.execute(select(chain.c.id, chain.c.depth).where(chain.c.id.is_not(None)).order_by(chain.c.depth)).all()
    seen, result = set(), []
    for manager_id, _ in rows:
        if manager_id in seen:
            break  # defensive: never loop on corrupt data
        seen.add(manager_id)
        result.append(manager_id)
    return result


def would_create_cycle(db: Session, company_id: int, employee_id: int, new_manager_id: int) -> bool:
    if employee_id == new_manager_id:
        return True
    return employee_id in management_chain(db, company_id, new_manager_id)

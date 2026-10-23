"""Self-service payroll preferences (GET /api/v1/me/payroll/preferences).

What the employee-facing payroll pages need to know about the caller's **own** employee record, every value
resolved for that employee by the settings engine (never an assumed default):

* payslips: ``payslip.queries_enabled`` (the "Ask a question" button) and ``payslip.pdf_protection`` (the
  mode only; whether the employee has set a PDF password is ``GET /me/payroll/payslip-password``; no secret
  ever leaves the server);
* pay group: id, code, name and currency of today's primary placement, and the next pay date: the first
  stored pay period of that pay group whose pay date is today or later in the legal entity's time zone
  (null when none is stored, never a computed guess);
* bank accounts: ``payroll.bank.self_service_enabled``, ``change_requires_approval``, ``cooling_off_hours``,
  ``verification_methods``, ``split_payments_enabled`` and ``max_accounts_per_payee``, with the effective
  maximum (1 while split payments are off, as the bank account service enforces). There is no setting that
  restricts bank account countries, so ``allowed_countries`` is null (any ISO 3166 country is accepted).

A setting that does not resolve to a valid value is reported as null and listed in ``unavailable`` instead
of being replaced by a default. A user without an employee record gets 404.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

import pytz
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import not_found
from app.core.principal import Principal
from app.models.company import Company
from app.models.employee import Employee
from app.models.org import LegalEntity
from app.models.pay_org import PayGroup
from app.models.pay_runs import PayPeriod
from app.payroll.services import settings as settings_engine

PAYSLIP_KEYS = ("payslip.queries_enabled", "payslip.pdf_protection")
BANK_KEYS = (
    "payroll.bank.self_service_enabled", "payroll.bank.change_requires_approval", "payroll.bank.cooling_off_hours",
    "payroll.bank.verification_methods", "payroll.bank.split_payments_enabled", "payroll.bank.max_accounts_per_payee",
)


def own_employee(db: Session, principal: Principal) -> Employee:
    """The caller's own employee record, or 404."""
    if principal.employee_id is None:
        raise not_found("Employee")
    employee = db.query(Employee).filter(Employee.company_id == principal.company_id,
                                         Employee.id == principal.employee_id).one_or_none()
    if employee is None:
        raise not_found("Employee")
    return employee


def local_today(db: Session, company_id: int, entity: Optional[LegalEntity]) -> date:
    """Today in the legal entity's time zone (the company's when there is no entity or zone)."""
    zone = entity.timezone if entity is not None and entity.timezone else None
    if zone is None:
        return clock.company_today(db, company_id)
    try:
        return clock.utcnow().astimezone(pytz.timezone(zone)).date()
    except pytz.UnknownTimeZoneError:
        return clock.company_today(db, company_id)


def next_pay_period(db: Session, company_id: int, pay_group_id: int, on: date) -> Optional[PayPeriod]:
    return (db.query(PayPeriod)
            .filter(PayPeriod.company_id == company_id, PayPeriod.pay_group_id == pay_group_id,
                    PayPeriod.pay_date >= on)
            .order_by(PayPeriod.pay_date, PayPeriod.period_start, PayPeriod.id)
            .first())


def _values(db: Session, company_id: int, employee_id: int, today: date) -> tuple[dict[str, Any], list[str]]:
    resolved = settings_engine.resolve_many(db, company_id, (*PAYSLIP_KEYS, *BANK_KEYS), employee_id=employee_id,
                                            on=today, strict=False)
    values: dict[str, Any] = {}
    unavailable: list[str] = []
    for key, item in resolved.items():
        if item.ok:
            values[key] = item.json_value
        else:
            values[key] = None
            unavailable.append(key)
    return values, unavailable


def preferences(db: Session, principal: Principal) -> dict:
    employee = own_employee(db, principal)
    company_id = principal.company_id
    today = clock.company_today(db, company_id)
    values, unavailable = _values(db, company_id, employee.id, today)
    placement = settings_engine.primary_assignment(db, company_id, employee.id, today)
    entity = group = None
    if placement is not None:
        entity = db.query(LegalEntity).filter(LegalEntity.company_id == company_id,
                                              LegalEntity.id == placement.legal_entity_id).one_or_none()
        if placement.pay_group_id is not None:
            group = db.query(PayGroup).filter(PayGroup.company_id == company_id,
                                              PayGroup.id == placement.pay_group_id).one_or_none()
    if entity is None:
        company = db.get(Company, company_id)
        zone = company.timezone if company is not None else None
    else:
        zone = entity.timezone
    pay_group = None
    if group is not None:
        local = local_today(db, company_id, entity)
        upcoming = next_pay_period(db, company_id, group.id, local)
        pay_group = {
            "id": group.id, "code": group.code, "name": group.name, "currency": group.currency,
            "next_pay_date": upcoming.pay_date.isoformat() if upcoming is not None else None,
            "next_period": ({"id": upcoming.id, "start": upcoming.period_start.isoformat(),
                             "end": upcoming.period_end.isoformat(), "status": upcoming.status}
                            if upcoming is not None else None),
        }
    split = values.get("payroll.bank.split_payments_enabled")
    maximum = values.get("payroll.bank.max_accounts_per_payee")
    effective_max = None
    if split is False:
        effective_max = 1
    elif split is True and isinstance(maximum, int):
        effective_max = maximum
    return {
        "employee_id": employee.id,
        "as_of": today.isoformat(),
        "time_zone": zone,
        "legal_entity": ({"id": entity.id, "name": entity.legal_name, "country_code": entity.country_code}
                         if entity is not None else None),
        "payslip": {
            "queries_enabled": values.get("payslip.queries_enabled"),
            "pdf_protection": values.get("payslip.pdf_protection"),
        },
        "pay_group": pay_group,
        "bank_accounts": {
            "self_service_enabled": values.get("payroll.bank.self_service_enabled"),
            "change_requires_approval": values.get("payroll.bank.change_requires_approval"),
            "cooling_off_hours": values.get("payroll.bank.cooling_off_hours"),
            "verification_methods": values.get("payroll.bank.verification_methods"),
            "split_payments_enabled": split,
            "max_accounts_per_payee": maximum,
            "effective_max_accounts": effective_max,
            "allowed_countries": None,
        },
        "unavailable": unavailable,
    }


__all__ = ["BANK_KEYS", "PAYSLIP_KEYS", "local_today", "next_pay_period", "own_employee", "preferences"]

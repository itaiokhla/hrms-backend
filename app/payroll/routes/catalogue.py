"""Read-only catalogues for any signed-in user: currencies, countries, subdivisions, country capability."""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.payroll.services import catalogue

router = APIRouter(prefix="/api/v1/payroll/catalogues", tags=["Payroll catalogues"])


@router.get("/currencies")
def currencies(include_historic: bool = False, payroll_only: bool = False,
               _: Principal = Depends(get_principal)):
    return catalogue.list_currencies(include_historic=include_historic, payroll_only=payroll_only)


@router.get("/countries")
def countries(include_deprecated: bool = False, _: Principal = Depends(get_principal)):
    return catalogue.list_countries(include_deprecated=include_deprecated)


@router.get("/countries/{alpha2}/subdivisions")
def subdivisions(alpha2: str, _: Principal = Depends(get_principal)):
    return catalogue.subdivisions(alpha2)


@router.get("/capabilities")
def capabilities(country: Optional[str] = Query(None, min_length=2, max_length=2),
                 _: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return catalogue.capabilities(db, country=country)

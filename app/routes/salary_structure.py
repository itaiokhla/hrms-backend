from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database.db import get_db
from app.schemas import (
    SalaryStructureCreate,
    SalaryStructureUpdate,
    SalaryStructureResponse,
)
from app.services.salary_structure_service import SalaryStructureService

router = APIRouter(
    prefix="/api/v1/salary-structure",
    tags=["Salary Structure"],
)


@router.post("/", response_model=SalaryStructureResponse)
def create_salary_structure(
    data: SalaryStructureCreate,
    db: Session = Depends(get_db),
):
    return SalaryStructureService.create_salary_structure(db, data)


@router.get("/", response_model=list[SalaryStructureResponse])
def get_salary_structures(
    db: Session = Depends(get_db),
):
    return SalaryStructureService.get_salary_structures(db)


@router.get("/{salary_id}", response_model=SalaryStructureResponse)
def get_salary_structure(
    salary_id: int,
    db: Session = Depends(get_db),
):
    return SalaryStructureService.get_salary_structure(
        db,
        salary_id,
    )


@router.put("/{salary_id}", response_model=SalaryStructureResponse)
def update_salary_structure(
    salary_id: int,
    data: SalaryStructureUpdate,
    db: Session = Depends(get_db),
):
    return SalaryStructureService.update_salary_structure(
        db,
        salary_id,
        data,
    )


@router.delete("/{salary_id}")
def delete_salary_structure(
    salary_id: int,
    db: Session = Depends(get_db),
):
    return SalaryStructureService.delete_salary_structure(
        db,
        salary_id,
    )
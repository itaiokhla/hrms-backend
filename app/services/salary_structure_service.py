from sqlalchemy.orm import Session
from fastapi import HTTPException, status

from app.models.payroll import SalaryStructure
from app.models.employee import Employee
from app.schemas import SalaryStructureCreate, SalaryStructureUpdate


class SalaryStructureService:

    @staticmethod
    def create_salary_structure(
        db: Session,
        data: SalaryStructureCreate,
    ):
        employee = (
            db.query(Employee)
            .filter(Employee.id == data.employee_id)
            .first()
        )

        if not employee:
            raise HTTPException(
                status_code=404,
                detail="Employee not found"
            )

        existing = (
            db.query(SalaryStructure)
            .filter(
                SalaryStructure.employee_id == data.employee_id,
                SalaryStructure.effective_to == None,
            )
            .first()
        )

        if existing:
            raise HTTPException(
                status_code=400,
                detail="Salary structure already exists"
            )

        salary = SalaryStructure(**data.model_dump())

        db.add(salary)
        db.commit()
        db.refresh(salary)

        return salary

    @staticmethod
    def get_salary_structures(db: Session):
        return (
            db.query(SalaryStructure)
            .order_by(SalaryStructure.id.desc())
            .all()
        )

    @staticmethod
    def get_salary_structure(
        db: Session,
        salary_id: int,
    ):
        salary = (
            db.query(SalaryStructure)
            .filter(SalaryStructure.id == salary_id)
            .first()
        )

        if not salary:
            raise HTTPException(
                status_code=404,
                detail="Salary structure not found"
            )

        return salary

    @staticmethod
    def update_salary_structure(
        db: Session,
        salary_id: int,
        data: SalaryStructureUpdate,
    ):
        salary = SalaryStructureService.get_salary_structure(
            db,
            salary_id,
        )

        update_data = data.model_dump(exclude_unset=True)

        for key, value in update_data.items():
            setattr(salary, key, value)

        db.commit()
        db.refresh(salary)

        return salary

    @staticmethod
    def delete_salary_structure(
        db: Session,
        salary_id: int,
    ):
        salary = SalaryStructureService.get_salary_structure(
            db,
            salary_id,
        )

        db.delete(salary)
        db.commit()

        return {
            "message": "Salary Structure deleted successfully"
        }
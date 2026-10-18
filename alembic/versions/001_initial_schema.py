"""Initial database schema

Revision ID: 001_initial_schema
Revises:
Create Date: 2024-01-01 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=50), nullable=False),
        sa.Column("email", sa.String(length=100), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_login", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("idx_username", "users", ["username"])
    op.create_index("idx_email", "users", ["email"])
    op.create_index("idx_role", "users", ["role"])

    op.create_table(
        "departments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("manager_id", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["manager_id"], ["users.id"], ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_departments_name"),
    )
    op.create_index("idx_name", "departments", ["name"])

    op.create_table(
        "shifts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("start_time", sa.String(length=5), nullable=False),
        sa.Column("end_time", sa.String(length=5), nullable=False),
        sa.Column("duration_hours", sa.Integer(), nullable=True),
        sa.Column("is_flexible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_shifts_name"),
    )

    op.create_table(
        "employees",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("first_name", sa.String(length=50), nullable=False),
        sa.Column("last_name", sa.String(length=50), nullable=False),
        sa.Column("email", sa.String(length=100), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("date_of_birth", sa.Date(), nullable=True),
        sa.Column("gender", sa.String(length=10), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("city", sa.String(length=50), nullable=True),
        sa.Column("state", sa.String(length=50), nullable=True),
        sa.Column("postal_code", sa.String(length=20), nullable=True),
        sa.Column("country", sa.String(length=50), nullable=True),
        sa.Column("employee_id", sa.String(length=50), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=False),
        sa.Column("shift_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.String(length=100), nullable=True),
        sa.Column("hire_date", sa.Date(), nullable=False),
        sa.Column("base_salary", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("photo_url", sa.String(length=255), nullable=True),
        sa.Column("profile_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"], ),
        sa.ForeignKeyConstraint(["shift_id"], ["shifts.id"], ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_employees_user_id"),
        sa.UniqueConstraint("email", name="uq_employees_email"),
        sa.UniqueConstraint("employee_id", name="uq_employees_employee_id"),
    )
    op.create_index("idx_employee_id", "employees", ["employee_id"])
    op.create_index("idx_department_id", "employees", ["department_id"])
    op.create_index("idx_user_id", "employees", ["user_id"])
    op.create_index("idx_email", "employees", ["email"])

    op.create_table(
        "face_encodings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("encoding", sa.String(), nullable=False),
        sa.Column("image_url", sa.String(length=255), nullable=True),
        sa.Column("quality_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_employee_id", "face_encodings", ["employee_id"])
    op.create_index("idx_is_primary", "face_encodings", ["is_primary"])

    op.create_table(
        "attendance",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("check_in_time", sa.DateTime(), nullable=False),
        sa.Column("check_out_time", sa.DateTime(), nullable=True),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("shift_id", sa.Integer(), nullable=True),
        sa.Column("face_match_score", sa.Float(), nullable=True),
        sa.Column("is_manual", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="present"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ),
        sa.ForeignKeyConstraint(["shift_id"], ["shifts.id"], ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_employee_id", "attendance", ["employee_id"])
    op.create_index("idx_date", "attendance", ["date"])
    op.create_index("idx_check_in_time", "attendance", ["check_in_time"])
    op.create_index("idx_status", "attendance", ["status"])

    op.create_table(
        "salary_structure",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("basic_salary", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("house_rent_allowance", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("dearness_allowance", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("conveyance_allowance", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("other_allowances", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_employee_id", "salary_structure", ["employee_id"])
    op.create_index("idx_effective_from", "salary_structure", ["effective_from"])

    op.create_table(
        "payroll",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("salary_month", sa.Date(), nullable=False),
        sa.Column("basic_salary", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("house_rent_allowance", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("dearness_allowance", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("conveyance_allowance", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("other_allowances", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("gross_salary", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("provident_fund", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("insurance", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("professional_tax", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("income_tax", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("other_deductions", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("total_deductions", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("overtime_hours", sa.Numeric(precision=8, scale=2), nullable=False, server_default="0"),
        sa.Column("overtime_amount", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("net_salary", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("working_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("present_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("absent_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("leave_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("payment_date", sa.String(), nullable=True),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_employee_id", "payroll", ["employee_id"])
    op.create_index("idx_salary_month", "payroll", ["salary_month"])
    op.create_index("idx_status", "payroll", ["status"])
    op.create_unique_constraint("uq_payroll_employee_month", "payroll", ["employee_id", "salary_month"])

    op.create_table(
        "salary_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("payroll_id", sa.Integer(), nullable=False),
        sa.Column("net_salary", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("payment_method", sa.String(length=50), nullable=True),
        sa.Column("transaction_id", sa.String(length=100), nullable=True),
        sa.Column("payment_date", sa.String(), nullable=True),
        sa.Column("slip_generated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("slip_url", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ),
        sa.ForeignKeyConstraint(["payroll_id"], ["payroll.id"], ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_employee_id", "salary_records", ["employee_id"])
    op.create_index("idx_payment_date", "salary_records", ["payment_date"])

    op.create_table(
        "leaves",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("leave_type", sa.String(length=50), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("number_of_days", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("approved_by", sa.Integer(), nullable=True),
        sa.Column("approval_date", sa.DateTime(), nullable=True),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"], ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_employee_id", "leaves", ["employee_id"])
    op.create_index("idx_status", "leaves", ["status"])
    op.create_index("idx_start_date", "leaves", ["start_date"])
    op.create_index("idx_end_date", "leaves", ["end_date"])

    op.create_table(
        "leave_balance",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("leave_type", sa.String(length=50), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("total_days", sa.Integer(), nullable=False),
        sa.Column("used_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("remaining_days", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_employee_id", "leave_balance", ["employee_id"])
    op.create_index("idx_year", "leave_balance", ["year"])

    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("notification_type", sa.String(length=50), nullable=True),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("action_url", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_user_id", "notifications", ["user_id"])
    op.create_index("idx_is_read", "notifications", ["is_read"])
    op.create_index("idx_created_at", "notifications", ["created_at"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("old_values", sa.String(), nullable=True),
        sa.Column("new_values", sa.String(), nullable=True),
        sa.Column("ip_address", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_user_id", "audit_logs", ["user_id"])
    op.create_index("idx_entity_type", "audit_logs", ["entity_type"])
    op.create_index("idx_created_at", "audit_logs", ["created_at"])

    op.create_table(
        "company_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("setting_key", sa.String(length=100), nullable=False),
        sa.Column("setting_value", sa.Text(), nullable=True),
        sa.Column("data_type", sa.String(length=20), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("setting_key", name="uq_company_settings_key"),
    )


def downgrade() -> None:
    op.drop_table("company_settings")
    op.drop_table("audit_logs")
    op.drop_table("notifications")
    op.drop_table("leave_balance")
    op.drop_table("leaves")
    op.drop_table("salary_records")
    op.drop_table("payroll")
    op.drop_table("salary_structure")
    op.drop_table("attendance")
    op.drop_table("face_encodings")
    op.drop_table("employees")
    op.drop_table("shifts")
    op.drop_table("departments")
    op.drop_table("users")

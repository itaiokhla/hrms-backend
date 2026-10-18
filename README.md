# HRMS Backend

FastAPI-powered backend for Human Resource Management System (HRMS) featuring AI facial recognition attendance tracking, role-based access control (RBAC), SQLite/PostgreSQL database, Alembic migrations, and automated payroll calculation.

## 🚀 Key Features

- **Authentication & RBAC**: JWT Bearer tokens, password hashing via bcrypt, user signup & login, role checks (Admin, Manager, Employee), and email OTP password reset.
- **AI Face Recognition Attendance**: OpenCV facial detection and embedding comparison for automated attendance marking.
- **Database & Migrations**: SQLAlchemy ORM with SQLite (local zero-config) and PostgreSQL support, managed with Alembic schema migrations.
- **Leave Management**: Leave requests, quota tracking, status approval/rejection workflows.
- **Payroll Processing**: Automated salary calculation, deductions, tax handling, and payslip generation.
- **Audit Logs**: Immutable activity logging across critical actions.

## 🛠️ Tech Stack

- **Framework**: [FastAPI](https://fastapi.tiangolo.com/) (Python 3.11+)
- **Database**: SQLite (default local) or PostgreSQL
- **ORM**: SQLAlchemy 2.0
- **Migrations**: Alembic
- **Computer Vision**: OpenCV (`opencv-python`)
- **Server**: Uvicorn

## 📋 Prerequisites

- **Python**: 3.11+
- **pip** (Python package installer)

## ⚙️ Local Setup

### 1. Create Virtual Environment

```bash
cd hrms-backend
python -m venv venv

# On Windows (PowerShell):
venv\Scripts\activate

# On Linux/macOS:
source venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Environment Configuration

Copy `.env.example` to `.env`:

```bash
# On Windows (PowerShell):
Copy-Item .env.example .env

# On Linux/macOS:
cp .env.example .env
```

By default, it uses a local SQLite database (`sqlite:///./hrms.db`), so **no database server installation or Docker is required**!

### 4. Database Migrations (Optional)

The backend automatically creates tables on startup. You can also run Alembic:

```bash
alembic upgrade head
```

### 5. Start Development Server

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

- **API Root**: [http://localhost:8000](http://localhost:8000)
- **Interactive Swagger Docs**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc**: [http://localhost:8000/redoc](http://localhost:8000/redoc)

## 📄 License

Proprietary - All rights reserved.

# HRMS Backend

FastAPI-powered backend for Human Resource Management System (HRMS) featuring AI facial recognition attendance tracking, role-based access control (RBAC), PostgreSQL database, Alembic migrations, Redis caching, and automated payroll calculation.

## 🚀 Key Features

- **Authentication & RBAC**: JWT Bearer tokens with OAuth2 password flow, password hashing via bcrypt, role checks (Admin, Manager, Employee), and email OTP password reset.
- **AI Face Recognition Attendance**: OpenCV facial detection and embedding comparison for automated attendance marking.
- **Database & Migrations**: SQLAlchemy ORM with PostgreSQL and automated schema migration management using Alembic.
- **Leave Management**: Leave requests, quota tracking, status approval/rejection workflows.
- **Payroll Processing**: Automated salary calculation, deductions, tax handling, and payslip generation.
- **Audit Logs**: Immutable activity logging across critical actions.

## 🛠️ Tech Stack

- **Framework**: [FastAPI](https://fastapi.tiangolo.com/) (Python 3.11+)
- **Database**: PostgreSQL 16
- **ORM**: SQLAlchemy 2.0
- **Migrations**: Alembic
- **Cache & Message Broker**: Redis 7
- **Computer Vision**: OpenCV (`opencv-python-headless`)
- **Server**: Uvicorn

## 📋 Prerequisites

- **Python**: 3.11 or higher
- **PostgreSQL**: 15+ running locally or in Docker
- **Redis**: 6+ running locally or in Docker

## ⚙️ Local Setup

### 1. Create Virtual Environment

```bash
cd hrms-backend
python -m venv venv

# On Windows:
venv\Scripts\activate

# On Linux/macOS:
source venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Environment Configuration

Copy `.env.example` to `.env` and fill in your database and security credentials:

```bash
cp .env.example .env
```

Key environment variables:
```env
DATABASE_URL=postgresql://salary_user:secure_password@localhost:5432/salary_management
REDIS_URL=redis://localhost:6379
SECRET_KEY=your-super-secret-key-min-32-chars-change-in-production
ALLOWED_ORIGINS=http://localhost:3000
```

### 4. Database Migrations

Apply Alembic migrations to initialize the database schema:

```bash
alembic upgrade head
```

### 5. Start Development Server

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

- **Swagger API Docs**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc**: [http://localhost:8000/redoc](http://localhost:8000/redoc)

## 🐳 Docker Setup

Build and run the container:

```bash
docker build -t hrms-backend .
docker run -p 8000:8000 --env-file .env hrms-backend
```

## 📄 License

Proprietary - All rights reserved.

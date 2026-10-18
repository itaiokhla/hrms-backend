from app.routes import payroll
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from datetime import timedelta
from app.routes import dashboard
from app.schemas import ChangePasswordRequest
from app.routes import salary_structure
from app.core.security import get_current_user
from app.routes import audit_logs

from app.services.audit_log_service import AuditLogService
from fastapi import Request

import app.routes.notifications as notifications
import logging
import app.models.notification
from fastapi.staticfiles import StaticFiles
import os

os.makedirs("uploads", exist_ok=True)

from app.schemas import ResetPasswordRequest

from app.config import settings
from app.schemas import VerifyOTPRequest
from app.database.db import get_db, engine
from app.models.base import Base
import app.models.user
import app.models.employee
import app.models.attendance
import app.models.leave
import app.models.payroll
import app.models.password_reset
import app.models.system_settings

from app.routes import reports
from app.routes import employees
from app.routes import attendance
from app.routes import face_recognition
from app.routes import leaves
from app.schemas import LoginRequest, UserCreate, LoginResponse, TokenRefresh, TokenResponse
from app.services.auth_service import AuthService
from app.core.security import get_user_id_from_token, create_access_token
from app.routes import settings as settings_router
from app.schemas import ForgotPasswordRequest
from app.services.password_reset_service import PasswordResetService

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Facial Attendance Salary Management System API",
)

app.mount(
    "/uploads",
    StaticFiles(directory="uploads"),
    name="uploads",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(notifications.router)
app.include_router(salary_structure.router)
app.include_router(reports.router)
app.include_router(payroll.router)
app.include_router(settings_router.router)
app.include_router(employees.router)
app.include_router(attendance.router)
app.include_router(face_recognition.router)
app.include_router(leaves.router)
app.include_router(dashboard.router)
app.include_router(
    audit_logs.router,
    prefix="/api/v1"
)



@app.on_event("startup")
async def startup_event():
    logger.info("Application startup")
    Base.metadata.create_all(bind=engine)


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Application shutdown")


@app.get("/health")
async def health_check():
    return {"status": "healthy", "app": settings.app_name}


@app.post("/api/v1/auth/register", response_model=LoginResponse)
async def register(user_data: UserCreate, db: Session = Depends(get_db)):
    new_user = AuthService.register_user(db, user_data)
    tokens = AuthService.get_tokens(new_user)
    return {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "token_type": tokens["token_type"],
        "user": new_user.to_dict()
    }


@app.post("/api/v1/auth/login", response_model=LoginResponse)
async def login( request: Request, credentials: LoginRequest, db: Session = Depends(get_db)):
    user = AuthService.authenticate_user(db, credentials.username, credentials.password)
    tokens = AuthService.get_tokens(user)
    AuditLogService.create_log(
    db=db,
    user_id=user.id,
    username=user.username,
    module="Authentication",
    action="LOGIN",
    description=f"{user.username} logged into the system",
    ip_address=request.client.host if request.client else None,
)
    return {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "token_type": tokens["token_type"],
        "user": user.to_dict()
    }

@app.post("/api/v1/auth/forgot-password")
async def forgot_password(
    data: ForgotPasswordRequest,
    db: Session = Depends(get_db),
):
    return PasswordResetService.send_otp(
        db=db,
        email=data.email,
    )

@app.post("/api/v1/auth/verify-otp")
async def verify_otp(
    data: VerifyOTPRequest,
    db: Session = Depends(get_db),
):
    return PasswordResetService.verify_otp(
        db=db,
        email=data.email,
        otp=data.otp,
    )
@app.post("/api/v1/auth/reset-password")
async def reset_password(
    data: ResetPasswordRequest,
    db: Session = Depends(get_db),
):
    return PasswordResetService.reset_password(
        db=db,
        email=data.email,
        new_password=data.new_password,
    )

@app.put("/api/v1/auth/change-password")
async def change_password(
    data: ChangePasswordRequest,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    return AuthService.change_password(
        db=db,
        user_id=current_user.id,
        current_password=data.current_password,
        new_password=data.new_password,
    )
@app.post("/api/v1/auth/refresh-token", response_model=TokenResponse)
async def refresh_token(token_data: TokenRefresh):
    result = AuthService.refresh_access_token(token_data.refresh_token)
    return {
        "access_token": result["access_token"],
        "token_type": result["token_type"],
        "expires_in": settings.access_token_expire_minutes * 60
    }


@app.get("/api/v1/auth/me")
async def get_current_user(token: str = None, db: Session = Depends(get_db)):
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization token"
        )

    user_id = get_user_id_from_token(token)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )

    current_user = AuthService.get_user_by_id(db, user_id)
    return current_user.to_dict()


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    import traceback
    traceback.print_exc()

    return JSONResponse(
        status_code=500,
        content={"detail": str(exc)},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

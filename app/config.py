import os
from functools import lru_cache
from typing import Optional, List
from pydantic_settings import BaseSettings


class Settings(BaseSettings):

    # Email Settings
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""


    app_name: str = "Salary Management System"
    app_version: str = "1.0.0"
    debug: bool = os.getenv("DEBUG", False)

    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql://salary_user:secure_password@localhost:5432/salary_management"
    )
    db_pool_size: int = 20
    db_echo: bool = False

    secret_key: str = os.getenv("SECRET_KEY", "your-super-secret-key-min-32-chars-change-in-production")
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7

    face_recognition_threshold: float = 0.45
    face_detection_confidence: float = 0.7
    frame_buffer_size: int = 30
    duplicate_check_minutes: int = 10

    upload_dir: str = "./uploads"
    max_upload_size: int = 10485760
    allowed_photo_extensions: list = ["jpg", "jpeg", "png"]

    smtp_server: str = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port: int = 587
    smtp_username: str = os.getenv("SMTP_USERNAME", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")

    allowed_origins: List[str] = [
    "http://localhost:3000",
    "http://localhost:8000",
    "https://salary-management-system-5bmz.vercel.app",
]

    log_level: str = "INFO"
    log_file: str = "./logs/app.log"

    default_work_hours_per_day: float = 8.0
    overtime_multiplier: float = 1.5
    late_penalty_minutes: int = 15

    enable_face_recognition: bool = True
    enable_email_notifications: bool = False
    enable_sms_notifications: bool = False

    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379")

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

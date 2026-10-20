import json
import logging
from functools import lru_cache
from typing import List, Optional, Union

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

_INSECURE_SECRETS = {
    "",
    "your-super-secret-key-min-32-chars-change-in-production",
    "changeme",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    app_name: str = "Intertech HRMS"
    app_version: str = "2.0.0"
    environment: str = "development"  # development | test | staging | production
    debug: bool = False

    # ---------------------------------------------------------------
    # Database (Neon / PostgreSQL)
    #
    # DATABASE_URL        -> least-privilege application role (RLS enforced)
    # SYSTEM_DATABASE_URL -> owner role used only for pre-authentication
    #                        lookups (login, signup, invitations, password
    #                        reset) and platform administration.
    # MIGRATION_DATABASE_URL -> owner role used by Alembic.
    # ---------------------------------------------------------------
    database_url: str = "postgresql://postgres@localhost:5432/hrms"
    system_database_url: Optional[str] = None
    migration_database_url: Optional[str] = None
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_echo: bool = False

    # Auth
    secret_key: str = ""
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7
    mfa_token_expire_minutes: int = 5
    password_reset_token_expire_minutes: int = 10
    invitation_expire_hours: int = 72
    max_failed_logins: int = 5
    lockout_minutes: int = 15

    # Cookies
    cookie_secure: bool = False
    cookie_samesite: str = "lax"
    cookie_domain: Optional[str] = None

    # Set true only when every request arrives through a trusted proxy.
    trust_proxy_headers: bool = False
    # Multiplies every authentication rate limit. Keep 1.0 in production;
    # raise it only for load or end-to-end test environments.
    auth_rate_limit_scale: float = 1.0

    # Public URL of the Next.js app (used in invitation links)
    frontend_url: str = "http://localhost:3000"

    # Face Recognition
    face_recognition_threshold: float = 0.45
    face_detection_confidence: float = 0.7

    # File uploads (stored privately, served through authorized endpoints)
    upload_dir: str = "./uploads"
    max_upload_size: int = 5 * 1024 * 1024

    # Email
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""

    allowed_origins: Union[List[str], str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]

    log_level: str = "INFO"

    # Payroll rules
    default_work_hours_per_day: float = 8.0
    late_penalty_amount: float = 100.0

    # Subscription
    trial_days: int = 14

    @field_validator("allowed_origins", mode="after")
    @classmethod
    def _parse_origins(cls, v):
        if isinstance(v, str):
            if v.strip().startswith("["):
                try:
                    return json.loads(v)
                except ValueError:
                    pass
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @model_validator(mode="after")
    def _validate_secrets(self):
        if not self.database_url.startswith("postgres"):
            raise ValueError(
                "DATABASE_URL must point to PostgreSQL (Neon). The multi-tenant schema relies on "
                "row-level security and timezone-aware columns that SQLite does not support. "
                "See .env.example and README.md."
            )
        if self.secret_key in _INSECURE_SECRETS or len(self.secret_key) < 32:
            if self.environment in ("production", "staging"):
                raise ValueError("SECRET_KEY must be set to a random value of at least 32 characters")
            logger.warning("SECRET_KEY is missing or weak; using an ephemeral development key")
            import secrets

            self.secret_key = secrets.token_urlsafe(48)
        if self.environment == "production" and not self.cookie_secure:
            raise ValueError("COOKIE_SECURE must be true in production")
        return self

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgres")

    @property
    def effective_system_database_url(self) -> str:
        return self.system_database_url or self.database_url

    @property
    def effective_migration_database_url(self) -> str:
        return self.migration_database_url or self.effective_system_database_url


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

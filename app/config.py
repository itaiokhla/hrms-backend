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

    # Public URL of the Next.js app on the platform host. Links for tenants without a
    # verified host use it; tenant hosts reuse its scheme and port.
    frontend_url: str = "http://localhost:3000"

    # ---------------------------------------------------------------
    # White label: hosts, custom domains and branding
    #
    # PLATFORM_HOSTS          hostnames (no port) of the platform itself; users of any
    #                         tenant may sign in there.
    # PLATFORM_BASE_DOMAIN    tenants get <slug>.<base domain> automatically; empty
    #                         disables per-tenant hosts (single-host mode).
    # PROXY_SHARED_SECRET     the Next.js server sends it with the original Host. Without
    #                         a matching secret every request counts as the platform host.
    # ---------------------------------------------------------------
    platform_hosts: Union[List[str], str] = ["localhost", "127.0.0.1"]
    platform_base_domain: str = ""
    proxy_shared_secret: str = ""
    custom_domain_cname_target: str = ""
    # dns | fixture (development/test only: records come from DOMAIN_DNS_FIXTURE_FILE)
    domain_dns_provider: str = "dns"
    domain_dns_fixture_file: str = ""
    # probe (TLS handshake against the host) | local-dev (development/test only: no TLS)
    tls_provider: str = "probe"
    domain_verification_window_hours: int = 72
    domain_broken_grace_days: int = 14
    handoff_code_ttl_seconds: int = 60
    brand_cache_seconds: int = 60
    # Development and test only: rendered emails are written here when SMTP is not configured.
    email_outbox_dir: str = "./logs/outbox"

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

    # Payroll v2 (docs/payroll/implementation/ARCHITECTURE.md)
    # Background jobs: "inline" (tests: run right after the enqueuing transaction commits),
    # "thread" (dev/demo stacks: worker thread inside the API process), "worker" (python -m app.worker).
    jobs_mode: str = "thread"
    # Base64 of a 32-byte key protecting per-tenant data keys (bank account numbers, tax identifiers).
    # Required outside development and test; never logged, never sent to the browser.
    payroll_master_key: str = ""

    # Subscription
    trial_days: int = 14

    # Attendance location tracking. Companies choose an interval between this
    # floor and 8 hours. Lowering it below 15 minutes is only for accelerated
    # end-to-end tests and is refused in staging and production.
    location_tracking_min_interval_minutes: int = 15

    @field_validator("allowed_origins", "platform_hosts", mode="after")
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
        if self.environment in ("production", "staging") and self.db_echo:
            # SQL echo logs statement parameters (salaries, encrypted bank fields, tokens) and bypasses redaction.
            raise ValueError("DB_ECHO must be off in production and staging")
        if self.location_tracking_min_interval_minutes < 1 or (
            self.environment in ("production", "staging") and self.location_tracking_min_interval_minutes < 15
        ):
            raise ValueError("LOCATION_TRACKING_MIN_INTERVAL_MINUTES must be at least 15 in staging and production")
        self.platform_hosts = [h.strip().lower().rstrip(".") for h in self.platform_hosts if h.strip()]
        self.platform_base_domain = self.platform_base_domain.strip().lower().strip(".")
        if self.environment in ("production", "staging"):
            if self.domain_dns_provider != "dns" or self.tls_provider != "probe":
                raise ValueError("DNS fixtures and the local-dev TLS provider are for development and tests only")
            if self.platform_base_domain and len(self.proxy_shared_secret) < 32:
                raise ValueError("PROXY_SHARED_SECRET (32+ characters) is required when tenant hosts are enabled")
        if self.platform_base_domain and self.cookie_domain:
            # A shared cookie domain would send one tenant host's session to every other host.
            raise ValueError("COOKIE_DOMAIN must be unset when PLATFORM_BASE_DOMAIN enables tenant hosts")
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

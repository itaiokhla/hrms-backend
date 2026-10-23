import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings
from app.core.errors import AppError
from app.database.db import TenantContextError, rls_status
from app.routes import ROUTERS

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("hrms")

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Multi-tenant HRMS API",
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-CSRF-Token", "Idempotency-Key"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


for router in ROUTERS:
    app.include_router(router)


@app.on_event("startup")
def sync_tenant_hosts() -> None:
    """Every tenant gets <slug>.<PLATFORM_BASE_DOMAIN> when per-tenant hosts are enabled."""
    if not settings.platform_base_domain:
        return
    try:
        from app.services import domain_service

        result = domain_service.sync_platform_subdomains()
        if result["created"]:
            logger.info("Created %s tenant host(s) under %s", result["created"], settings.platform_base_domain)
    except Exception:
        logger.exception("Could not synchronise tenant hosts")


@app.on_event("startup")
def start_payroll_runtime() -> None:
    """Payroll v2 runtime: refuse a missing or malformed master key, redact logs, start the embedded worker."""
    from app import worker
    from app.payroll import packs
    from app.payroll.services import jobs, keys

    keys.check_master_key()
    jobs.install_log_redaction()
    # Country packs' employer choices (payroll.statutory.<CC>.*) join the settings registry, so a legal entity
    # can switch its schemes; an unverified pack still locks them OFF through its constraints (EC-STA-01).
    packs.register_settings(environment=packs.environment_name(settings.environment))
    if worker.validate_mode() == "thread":
        worker.start_embedded()


@app.on_event("shutdown")
def stop_payroll_runtime() -> None:
    if settings.jobs_mode == "thread":
        from app import worker

        worker.stop_embedded()


@app.on_event("startup")
def report_isolation_status() -> None:
    try:
        status = rls_status()
    except Exception:  # database unreachable at boot; health check will report it
        logger.exception("Could not determine row-level security status")
        return
    if not status.get("rls_enforced"):
        logger.warning(
            "Row-level security is NOT enforced for the application database role (%s). "
            "Tenant isolation still applies in the application layer; run "
            "`python -m app.cli setup-db-roles` and connect DATABASE_URL as that role.",
            status,
        )


@app.get("/health")
def health():
    try:
        status = rls_status()
        return {"status": "ok", "rls_enforced": status["rls_enforced"]}
    except Exception:
        return JSONResponse(status_code=503, content={"status": "degraded"})


def _error(status_code: int, detail, code: str):
    return JSONResponse(status_code=status_code, content={"detail": detail, "code": code})


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    response = _error(exc.status_code, exc.detail, exc.code or "ERROR")
    for key, value in (exc.headers or {}).items():
        response.headers[key] = value
    return response


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    return _error(exc.status_code, exc.detail, "ERROR")


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    errors = [
        {"field": ".".join(str(p) for p in err.get("loc", [])[1:]), "msg": err.get("msg", "Invalid value")}
        for err in exc.errors()
    ]
    return _error(422, errors, "VALIDATION_ERROR")


@app.exception_handler(TenantContextError)
async def tenant_error_handler(request: Request, exc: TenantContextError):
    logger.error("Tenant isolation guard blocked an operation on %s: %s", request.url.path, exc)
    return _error(403, "This operation is not allowed.", "TENANT_VIOLATION")


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return _error(500, "Something went wrong. Please try again.", "INTERNAL_ERROR")

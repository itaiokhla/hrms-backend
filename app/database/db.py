"""Database engines, sessions and tenant-isolation plumbing.

Two engines are used:

* ``engine`` (DATABASE_URL) connects as the least-privilege application role.
  Every request handler uses it. On PostgreSQL the role is subject to
  row-level security, and the active company is published to the database
  through a *transaction-local* setting (``app.company_id``), so it can never
  leak to the next user of a pooled connection.
* ``system_engine`` (SYSTEM_DATABASE_URL) is used only for operations that
  run before a tenant is known (login, signup, invitations, password reset)
  and for platform administration.

Independently of RLS, ORM events add ``company_id = <tenant>`` to every
SELECT/UPDATE/DELETE that touches a tenant-owned model, stamp ``company_id``
on new rows and refuse cross-tenant writes.
"""

import logging
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import create_engine, event, false, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker, with_loader_criteria

from app.config import settings
from app.models.base import TenantMixin

logger = logging.getLogger(__name__)

TENANT_KEY = "company_id"
BYPASS_KEY = "tenant_bypass"
HIERARCHY_LOCK_NAMESPACE = 4242


class TenantContextError(RuntimeError):
    """Raised when tenant-owned data is written without a valid tenant context."""


def _make_engine(url: str) -> Engine:
    # Statement parameters (salaries, account numbers, names) never reach logs or error messages
    # unless SQL echo is explicitly switched on for local debugging (EC-SEC-08).
    kwargs = {
        "echo": settings.db_echo,
        "hide_parameters": not settings.db_echo,
        "future": True,
        "pool_pre_ping": True,
    }
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # Neon scales idle compute to zero; recycle so we never reuse a dead socket.
        kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_recycle=300,
        )
    return create_engine(url, **kwargs)


engine = _make_engine(settings.database_url)
system_engine = (
    engine
    if settings.effective_system_database_url == settings.database_url
    else _make_engine(settings.effective_system_database_url)
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
SystemSessionLocal = sessionmaker(
    bind=system_engine, autoflush=False, expire_on_commit=False, info={BYPASS_KEY: True}
)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def system_session() -> Iterator[Session]:
    """Session for pre-authentication and platform-level work only."""
    db = SystemSessionLocal()
    try:
        yield db
    finally:
        db.close()


def _apply_pg_context(connection, company_id: Optional[int]) -> None:
    if connection.dialect.name != "postgresql":
        return
    connection.execute(
        text("SELECT set_config('app.company_id', :cid, true)"),
        {"cid": str(company_id) if company_id is not None else ""},
    )


def bind_tenant(db: Session, company_id: int) -> None:
    """Scope a session to one company for the rest of its life."""
    if company_id is None:
        raise TenantContextError("company_id is required")
    current = db.info.get(TENANT_KEY)
    if current is not None and current != company_id:
        raise TenantContextError("session is already bound to a different company")
    db.info[TENANT_KEY] = company_id
    db.info.pop(BYPASS_KEY, None)
    if db.in_transaction():
        _apply_pg_context(db.connection(), company_id)


def current_company_id(db: Session) -> Optional[int]:
    return db.info.get(TENANT_KEY)


def lock_hierarchy(db: Session, company_id: int) -> None:
    """Serialise reporting-line changes within one company (transaction scoped)."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:ns, :cid)"),
            {"ns": HIERARCHY_LOCK_NAMESPACE, "cid": company_id},
        )


@event.listens_for(Session, "after_begin")
def _publish_tenant_to_database(session, transaction, connection):
    # Runs at the start of every transaction, including those opened after a
    # commit, so the RLS context is always present and always transaction-local.
    if not session.info.get(BYPASS_KEY):
        _apply_pg_context(connection, session.info.get(TENANT_KEY))


@event.listens_for(Session, "do_orm_execute")
def _restrict_queries_to_tenant(state):
    if state.is_column_load or state.is_relationship_load:
        return
    if not (state.is_select or state.is_update or state.is_delete):
        return
    if state.session.info.get(BYPASS_KEY):
        return
    company_id = state.session.info.get(TENANT_KEY)
    if company_id is None:
        # Deny by default: no tenant context means no tenant rows.
        state.statement = state.statement.options(
            with_loader_criteria(TenantMixin, lambda cls: false(), include_aliases=True)
        )
        return
    state.statement = state.statement.options(
        with_loader_criteria(TenantMixin, lambda cls: cls.company_id == company_id, include_aliases=True)
    )


@event.listens_for(Session, "before_flush")
def _guard_tenant_writes(session, flush_context, instances):
    company_id = session.info.get(TENANT_KEY)
    bypass = session.info.get(BYPASS_KEY)

    for obj in session.new:
        if not isinstance(obj, TenantMixin):
            continue
        if obj.company_id is None:
            if company_id is None:
                raise TenantContextError(f"{type(obj).__name__} created without tenant context")
            obj.company_id = company_id
        elif company_id is not None and obj.company_id != company_id:
            raise TenantContextError(f"{type(obj).__name__} belongs to another company")
        elif company_id is None and not bypass:
            raise TenantContextError(f"{type(obj).__name__} created without tenant context")

    from sqlalchemy import inspect

    for obj in list(session.dirty) + list(session.deleted):
        if not isinstance(obj, TenantMixin):
            continue
        history = inspect(obj).attrs.company_id.history
        if history.has_changes() and history.deleted:
            raise TenantContextError("company ownership of a record cannot be changed")
        if company_id is not None and obj.company_id != company_id:
            raise TenantContextError(f"{type(obj).__name__} belongs to another company")


def rls_status() -> dict:
    """Report whether the application role is actually subject to RLS."""
    if engine.dialect.name != "postgresql":
        return {"database": engine.dialect.name, "rls_enforced": False}
    with engine.connect() as conn:
        role = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
        owns_tables = conn.execute(
            text("SELECT count(*) FROM pg_tables WHERE tablename = 'employees' AND tableowner = current_user")
        ).scalar()
        rls_enabled = conn.execute(
            text("SELECT relrowsecurity FROM pg_class WHERE relname = 'employees'")
        ).scalar()
        # Neon's admin group bypasses RLS through membership even for a NOSUPERUSER role (LH-SEC-19).
        # CASE (not AND) so pg_has_role never runs where the role does not exist (any non-Neon server).
        neon_member = bool(conn.execute(text(
            "SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'neon_superuser') "
            "THEN pg_has_role(current_user, 'neon_superuser', 'MEMBER') ELSE false END"
        )).scalar())
    enforced = (bool(rls_enabled) and not role.rolsuper and not role.rolbypassrls and not owns_tables
                and not neon_member)
    return {"database": "postgresql", "rls_enforced": enforced}

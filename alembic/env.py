import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

from app.config import settings  # noqa: E402
import app.models  # noqa: E402,F401  (registers every table on the metadata)
from app.models.base import Base  # noqa: E402

# Migrations run as the schema owner, never as the RLS-restricted app role.
config.set_main_option(
    "sqlalchemy.url", settings.effective_migration_database_url.replace("%", "%%")
)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

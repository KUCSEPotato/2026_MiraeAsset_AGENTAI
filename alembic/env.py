"""Alembic environment for the PostgreSQL-only canonical v2 schema."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, make_url, pool

from app.data.v2_schema import CANONICAL_V2_SCHEMA, metadata


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

configured_url = config.get_main_option("sqlalchemy.url")
environment_url = os.getenv("DATABASE_URL")
# A caller-provided Alembic Config (notably an isolated migration test) must
# never be redirected by an unrelated process-level DATABASE_URL.  The
# checked-in ``unused`` URL is only a sentinel that requires environment
# configuration for normal CLI use.
database_url = (
    configured_url
    if configured_url and "/unused" not in configured_url
    else environment_url
)
if not database_url:
    raise ValueError("DATABASE_URL or sqlalchemy.url is required for Alembic")
backend = make_url(database_url).get_backend_name()
if backend != "postgresql":
    raise ValueError(f"Canonical v2 migrations require PostgreSQL; unsupported backend: {backend}")
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

target_metadata = metadata


def include_name(
    name: str | None,
    type_: str,
    parent_names: dict[str, str | None],
) -> bool:
    """Keep autogenerate isolated from v1 and unrelated PostgreSQL schemas."""
    if type_ == "schema":
        return name == CANONICAL_V2_SCHEMA
    if type_ == "table":
        return parent_names.get("schema_name") == CANONICAL_V2_SCHEMA
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        include_name=include_name,
        version_table=config.get_main_option("version_table"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            include_name=include_name,
            version_table=config.get_main_option("version_table"),
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

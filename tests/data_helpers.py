"""PostgreSQL-only helpers shared by legacy integration tests."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

from app.data.schema import metadata


def postgres_engine(namespace: Path) -> Engine:
    """Return an isolated-schema engine in the configured disposable database."""

    value = os.getenv("POSTGRES_TEST_DATABASE_URL")
    if not value:
        pytest.skip("POSTGRES_TEST_DATABASE_URL is not configured")
    parsed = make_url(value)
    if parsed.get_backend_name() != "postgresql":
        pytest.fail("POSTGRES_TEST_DATABASE_URL must use PostgreSQL")
    database = (parsed.database or "").casefold()
    if not any(marker in database for marker in ("test", "audit", "c3")):
        pytest.fail("test helper requires a disposable PostgreSQL database")

    digest = hashlib.sha256(str(namespace).encode()).hexdigest()[:16]
    schema = f"test_{digest}"
    administrator = create_engine(value, future=True)
    try:
        with administrator.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    finally:
        administrator.dispose()

    engine = create_engine(
        value,
        future=True,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    metadata.create_all(engine)
    return engine

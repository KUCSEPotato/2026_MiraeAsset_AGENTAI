import os
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url

from app.runtime_bundle import RuntimeBundle, select_runtime_bundle_from_env


DATABASE_SCHEMA_VERSION = "m10.7-canonical-v1"
DATABASE_BACKEND = "postgresql"


@dataclass(frozen=True)
class DatabaseSettings:
    database_url: str
    snapshot_date: str = "2026-08-24"
    rdb_default_limit: int = 10
    rdb_max_limit: int = 10_000
    rdb_repository_version: Literal["v1", "v2"] = "v1"
    v2_generation: str = "260824"
    v2_ontology_version: str = "merged-optical-1.4"
    v2_transformer_version: str = "m10.9-c2-kodex-holdings-1"
    v2_multi_store_enabled: bool = False
    runtime_data_version: Literal["v1", "v2"] = "v1"
    trusted_holdings_runtime_enabled: bool = False
    trusted_holdings_scopes: tuple[str, ...] = ("KODEX_LONG_ONLY_COMPATIBLE",)
    trusted_issuer_runtime_enabled: bool = False
    trusted_issuer_scope: str = "KODEX_LONG_ONLY_COMPATIBLE"
    pool_size: int = 5
    max_overflow: int = 5
    pool_timeout_seconds: float = 30.0
    pool_recycle_seconds: int = 1_800

    def __post_init__(self) -> None:
        backend = make_url(self.database_url).get_backend_name()
        if backend != DATABASE_BACKEND:
            raise ValueError(
                "DATABASE_URL must use PostgreSQL; "
                f"unsupported backend: {backend}"
            )
        if self.rdb_repository_version not in {"v1", "v2"}:
            raise ValueError("RDB_REPOSITORY_VERSION must be v1 or v2")
        if self.runtime_data_version not in {"v1", "v2"}:
            raise ValueError("RUNTIME_DATA_VERSION must be v1 or v2")
        if self.pool_size <= 0 or self.max_overflow < 0:
            raise ValueError("database pool sizes are invalid")
        if self.pool_timeout_seconds <= 0 or self.pool_recycle_seconds <= 0:
            raise ValueError("database pool time settings must be positive")

    @property
    def runtime_bundle(self) -> RuntimeBundle:
        # Direct construction is used by isolated repository tests.  Only the
        # complete v2 combination is a v2 *runtime* bundle; a standalone v2
        # repository remains an integration fixture, never a mixed runtime.
        if self.rdb_repository_version == "v2" and self.v2_multi_store_enabled:
            return RuntimeBundle(version="v2")
        return RuntimeBundle(version="v1")

    @classmethod
    def from_env(cls, *, require_url: bool = True) -> "DatabaseSettings":
        del require_url
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise ValueError("DATABASE_URL is required and must use PostgreSQL")
        limit = int(os.getenv("RDB_DEFAULT_LIMIT", "10"))
        max_limit = int(os.getenv("RDB_MAX_LIMIT", "10000"))
        if limit <= 0:
            raise ValueError("RDB_DEFAULT_LIMIT must be positive")
        if max_limit < limit:
            raise ValueError("RDB_MAX_LIMIT must be at least RDB_DEFAULT_LIMIT")
        bundle = select_runtime_bundle_from_env()
        return cls(
            database_url=database_url,
            snapshot_date=os.getenv("DATA_SNAPSHOT_DATE", "2026-08-24"),
            rdb_default_limit=limit,
            rdb_max_limit=max_limit,
            rdb_repository_version=bundle.version,
            v2_generation=os.getenv("CANONICAL_V2_GENERATION", "260824"),
            v2_ontology_version=os.getenv(
                "CANONICAL_V2_ONTOLOGY_VERSION", "merged-optical-1.4"
            ),
            v2_transformer_version=os.getenv(
                "CANONICAL_V2_TRANSFORMER_VERSION", "m10.9-c2-kodex-holdings-1"
            ),
            v2_multi_store_enabled=bundle.uses_canonical_v2,
            runtime_data_version=bundle.version,
            trusted_holdings_runtime_enabled=(
                os.getenv("TRUSTED_HOLDINGS_RUNTIME_ENABLED", "0") == "1"
            ),
            trusted_holdings_scopes=tuple(
                item.strip() for item in os.getenv(
                    "TRUSTED_HOLDINGS_SCOPES", "KODEX_LONG_ONLY_COMPATIBLE"
                ).split(",") if item.strip()
            ),
            trusted_issuer_runtime_enabled=(
                os.getenv("TRUSTED_ISSUER_RUNTIME_ENABLED", "0") == "1"
            ),
            trusted_issuer_scope=os.getenv(
                "TRUSTED_ISSUER_SCOPE", "KODEX_LONG_ONLY_COMPATIBLE"
            ),
            pool_size=int(os.getenv("DATABASE_POOL_SIZE", "5")),
            max_overflow=int(os.getenv("DATABASE_MAX_OVERFLOW", "5")),
            pool_timeout_seconds=float(os.getenv("DATABASE_POOL_TIMEOUT_SECONDS", "30")),
            pool_recycle_seconds=int(os.getenv("DATABASE_POOL_RECYCLE_SECONDS", "1800")),
        )


def create_database_engine(settings: DatabaseSettings) -> Engine:
    return create_engine(
        settings.database_url,
        future=True,
        pool_pre_ping=True,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_timeout=settings.pool_timeout_seconds,
        pool_recycle=settings.pool_recycle_seconds,
    )

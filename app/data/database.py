import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, create_engine


DEFAULT_DATABASE_PATH = Path("data/financial_agent.db")


@dataclass(frozen=True)
class DatabaseSettings:
    database_url: str
    snapshot_date: str = "2026-07-11"
    rdb_default_limit: int = 10
    rdb_max_limit: int = 10_000

    @classmethod
    def from_env(cls, *, require_url: bool = False) -> "DatabaseSettings":
        database_url = os.getenv("DATABASE_URL")
        if require_url and not database_url:
            raise ValueError("DATABASE_URL is required for the real RDB runtime")
        if not database_url:
            database_url = f"sqlite:///{DEFAULT_DATABASE_PATH}"
        limit = int(os.getenv("RDB_DEFAULT_LIMIT", "10"))
        max_limit = int(os.getenv("RDB_MAX_LIMIT", "10000"))
        if limit <= 0:
            raise ValueError("RDB_DEFAULT_LIMIT must be positive")
        if max_limit < limit:
            raise ValueError("RDB_MAX_LIMIT must be at least RDB_DEFAULT_LIMIT")
        return cls(
            database_url=database_url,
            snapshot_date=os.getenv("DATA_SNAPSHOT_DATE", "2026-07-11"),
            rdb_default_limit=limit,
            rdb_max_limit=max_limit,
        )


def create_database_engine(settings: DatabaseSettings) -> Engine:
    if settings.database_url.startswith("sqlite:///"):
        raw_path = settings.database_url.removeprefix("sqlite:///")
        if raw_path and raw_path != ":memory:":
            Path(raw_path).parent.mkdir(parents=True, exist_ok=True)
    connect_args = (
        {"check_same_thread": False}
        if settings.database_url.startswith("sqlite")
        else {}
    )
    return create_engine(
        settings.database_url,
        connect_args=connect_args,
        future=True,
    )

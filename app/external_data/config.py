from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CRAWLER_VERSION = "external-crawler-foundation-v1"


@dataclass(frozen=True, slots=True)
class ExternalCrawlerSettings:
    request_timeout_seconds: float = 20.0
    max_retries: int = 3
    request_interval_seconds: float = 1.0
    max_concurrency: int = 2
    user_agent: str = "MiraeAssetAIFestival-ExternalCrawler/1.0 (+contact: project-team)"
    output_directory: Path = Path("external_data")
    respect_robots_txt: bool = True
    max_response_bytes: int = 50 * 1024 * 1024
    crawler_version: str = DEFAULT_CRAWLER_VERSION

    @classmethod
    def from_env(cls) -> "ExternalCrawlerSettings":
        settings = cls(
            request_timeout_seconds=float(os.getenv("EXTERNAL_CRAWLER_TIMEOUT_SECONDS", "20")),
            max_retries=int(os.getenv("EXTERNAL_CRAWLER_MAX_RETRIES", "3")),
            request_interval_seconds=float(os.getenv("EXTERNAL_CRAWLER_REQUEST_INTERVAL_SECONDS", "1")),
            max_concurrency=int(os.getenv("EXTERNAL_CRAWLER_MAX_CONCURRENCY", "2")),
            user_agent=os.getenv(
                "EXTERNAL_CRAWLER_USER_AGENT",
                "MiraeAssetAIFestival-ExternalCrawler/1.0 (+contact: project-team)",
            ),
            output_directory=Path(os.getenv("EXTERNAL_CRAWLER_OUTPUT_DIR", "external_data")),
            respect_robots_txt=_boolean_env("EXTERNAL_CRAWLER_RESPECT_ROBOTS", True),
            max_response_bytes=int(os.getenv("EXTERNAL_CRAWLER_MAX_RESPONSE_BYTES", str(50 * 1024 * 1024))),
            crawler_version=os.getenv("EXTERNAL_CRAWLER_VERSION", DEFAULT_CRAWLER_VERSION),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.request_timeout_seconds <= 0:
            raise ValueError("EXTERNAL_CRAWLER_TIMEOUT_SECONDS must be positive")
        if self.max_retries < 0:
            raise ValueError("EXTERNAL_CRAWLER_MAX_RETRIES must be non-negative")
        if self.request_interval_seconds < 0:
            raise ValueError("EXTERNAL_CRAWLER_REQUEST_INTERVAL_SECONDS must be non-negative")
        if not 1 <= self.max_concurrency <= 8:
            raise ValueError("EXTERNAL_CRAWLER_MAX_CONCURRENCY must be between 1 and 8")
        if not self.user_agent.strip():
            raise ValueError("EXTERNAL_CRAWLER_USER_AGENT must not be empty")
        if self.max_response_bytes <= 0:
            raise ValueError("EXTERNAL_CRAWLER_MAX_RESPONSE_BYTES must be positive")


def _boolean_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")

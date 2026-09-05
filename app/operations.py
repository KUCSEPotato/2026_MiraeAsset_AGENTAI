"""Small production-operation boundary for the evaluation API."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class OperationalSettings:
    request_timeout_seconds: float = 240.0
    log_level: str = "INFO"
    runtime_environment: str = "development"

    @classmethod
    def from_env(cls) -> "OperationalSettings":
        timeout = float(os.getenv("APP_TIMEOUT_SECONDS", "240"))
        if timeout <= 0 or timeout >= 300:
            raise ValueError("APP_TIMEOUT_SECONDS must be greater than 0 and below 300")
        level = os.getenv("LOG_LEVEL", "INFO").upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL is invalid")
        return cls(
            request_timeout_seconds=timeout,
            log_level=level,
            runtime_environment=os.getenv("RUNTIME_ENVIRONMENT", "development"),
        )


class JsonLogFormatter(logging.Formatter):
    """Emit allow-listed operational fields without exception bodies or secrets."""

    _fields = (
        "question_id",
        "request_id",
        "runtime_generation",
        "route_type",
        "selected_stores",
        "candidate_counts",
        "latency_ms",
        "answerability_reason",
        "error_class",
        "http_status",
        "hcx_error_code",
        "hcx_error_message",
        "request_purpose",
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact(record.getMessage()),
        }
        for field in self._fields:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = _redact(value) if isinstance(value, str) else value
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(settings: OperationalSettings) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)


_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)(postgres(?:ql)?(?:\+\w+)?://[^:\s/@]+:)[^@\s/]+(@)"),
    re.compile(r"(?i)((?:api[_-]?key|password|token)\s*[:=]\s*)[^\s,;]+"),
)


def _redact(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        if pattern.groups == 2:
            redacted = pattern.sub(r"\1[REDACTED]\2", redacted)
        else:
            redacted = pattern.sub(r"\1[REDACTED]", redacted)
    return redacted

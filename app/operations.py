"""Small production-operation boundary for the evaluation API."""

from __future__ import annotations

import json
import logging
import os
import re
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime

from app.hyperclova import sanitize_hyperclova_diagnostic


# Set for the lifetime of an API request; independent across concurrent tasks.
request_correlation_id: ContextVar[str | None] = ContextVar("request_correlation_id", default=None)


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
        "parser_path",
        "rule_latency_ms",
        "llm_latency_ms",
        "constraint_count",
        "unparsed_count",
        "validation_status",
        "failure_stage",
        "parser_failure_reason",
        "evidence_count",
        "answer_status",
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact(record.getMessage()),
        }
        correlation = request_correlation_id.get()
        if correlation is not None:
            payload["correlation_id"] = correlation
        for field in self._fields:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = _redact(value) if isinstance(value, str) else value
        # These diagnostics used to exist only on LogRecord and disappear from
        # production JSON. Reconstruct the nested allowlist; never emit input,
        # ctx, exception bodies, response bodies or request headers.
        errors = getattr(record, "validation_errors", None)
        if isinstance(errors, list):
            payload["validation_errors"] = [
                {"loc": [sanitize_hyperclova_diagnostic(part) for part in item.get("loc", [])[:20]],
                 "type": sanitize_hyperclova_diagnostic(item.get("type")),
                 "msg": sanitize_hyperclova_diagnostic(item.get("msg"))}
                for item in errors[:20] if isinstance(item, dict)
            ]
        keys = getattr(record, "parsed_top_level_keys", None)
        if isinstance(keys, list):
            payload["parsed_top_level_keys"] = [sanitize_hyperclova_diagnostic(key) for key in keys[:50]]
        reasons = getattr(record, "candidate_rejection_reasons", None)
        if isinstance(reasons, list):
            # Validator codes only. Never log a candidate field/value or text.
            payload["candidate_rejection_reasons"] = list(dict.fromkeys(
                value for value in reasons if isinstance(value, str)
                and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", value)
            ))[:30]
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

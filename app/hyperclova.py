"""Credential-safe diagnostics shared by HyperCLOVA integrations."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx


_MAX_ERROR_MESSAGE_LENGTH = 500
_SECRET_VALUE = re.compile(
    r"(?i)(authorization\s*[:=]\s*bearer\s+|api[_-]?key\s*[:=]\s*)\S+"
)
_NAVER_API_KEY = re.compile(r"\bnv-[A-Za-z0-9._~-]+\b")


@dataclass(frozen=True)
class HyperCLOVAErrorDetails:
    """Allow-listed provider error fields; never contains request data."""

    code: str
    message: str


def extract_hyperclova_error(
    response: httpx.Response,
) -> HyperCLOVAErrorDetails:
    """Read only documented error fields from a HyperCLOVA response."""

    try:
        body: Any = response.json()
    except (ValueError, TypeError):
        body = None

    code: object | None = None
    message: object | None = None
    if isinstance(body, dict):
        status = body.get("status")
        if isinstance(status, dict):
            code = status.get("code")
            message = status.get("message")
        if code is None:
            code = body.get("errorCode", body.get("code"))
        if message is None:
            message = body.get("errorMessage", body.get("message"))
        error = body.get("error")
        if isinstance(error, dict):
            if code is None:
                code = error.get("code")
            if message is None:
                message = error.get("message")

    return HyperCLOVAErrorDetails(
        code=_safe_scalar(code, fallback="unavailable"),
        message=_safe_scalar(message, fallback="unavailable"),
    )


def log_hyperclova_http_error(
    logger: logging.Logger,
    response: httpx.Response,
    *,
    request_purpose: str,
    request_id: str,
) -> HyperCLOVAErrorDetails:
    """Log the minimum useful upstream failure metadata."""

    details = extract_hyperclova_error(response)
    logger.error(
        "HyperCLOVA request failed",
        extra={
            "http_status": response.status_code,
            "hcx_error_code": details.code,
            "hcx_error_message": details.message,
            "request_purpose": request_purpose,
            "request_id": request_id,
            "error_class": "HTTPStatusError",
        },
    )
    return details


def _safe_scalar(value: object | None, *, fallback: str) -> str:
    if not isinstance(value, (str, int, float)):
        return fallback
    rendered = " ".join(str(value).split())
    rendered = _SECRET_VALUE.sub(r"\1[REDACTED]", rendered)
    rendered = _NAVER_API_KEY.sub("[REDACTED]", rendered)
    return rendered[:_MAX_ERROR_MESSAGE_LENGTH] or fallback

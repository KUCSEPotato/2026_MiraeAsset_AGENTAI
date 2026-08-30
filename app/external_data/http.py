from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
import urllib.robotparser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.external_data.config import ExternalCrawlerSettings
from app.external_data.models import ContentType, QualityStatus


RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class FetchResult:
    requested_url: str
    normalized_url: str
    retrieved_at: datetime
    status_code: int | None
    content: bytes | None
    content_type: ContentType
    media_type: str | None
    content_hash: str | None
    etag: str | None
    last_modified: str | None
    quality_status: QualityStatus
    attempts: int
    from_cache: bool = False
    not_modified: bool = False
    error_type: str | None = None
    error_message: str | None = None


class HttpCache:
    def __init__(self, root: Path) -> None:
        self.root = root

    def load(self, normalized_url: str) -> tuple[dict[str, object], bytes] | None:
        key = hashlib.sha256(normalized_url.encode()).hexdigest()
        metadata_path = self.root / f"{key}.json"
        body_path = self.root / f"{key}.body"
        if not metadata_path.is_file() or not body_path.is_file():
            return None
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        body = body_path.read_bytes()
        if hashlib.sha256(body).hexdigest() != metadata.get("content_hash"):
            return None
        return metadata, body

    def store(
        self, normalized_url: str, *, content: bytes, status_code: int,
        media_type: str | None, etag: str | None, last_modified: str | None,
    ) -> None:
        key = hashlib.sha256(normalized_url.encode()).hexdigest()
        content_hash = hashlib.sha256(content).hexdigest()
        metadata = {
            "schema_version": "external-http-cache-v1",
            "normalized_url": normalized_url,
            "status_code": status_code,
            "media_type": media_type,
            "etag": etag,
            "last_modified": last_modified,
            "content_hash": content_hash,
            "cached_at": datetime.now(UTC).isoformat(),
        }
        _atomic_write(self.root / f"{key}.body", content)
        _atomic_write(
            self.root / f"{key}.json",
            (json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(),
        )


class TrustedHttpClient:
    def __init__(
        self,
        settings: ExternalCrawlerSettings,
        *,
        client: httpx.AsyncClient | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        settings.validate()
        self.settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent},
        )
        self._cache = HttpCache(settings.output_directory / "cache" / "http")
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._rate_lock = asyncio.Lock()
        self._last_request_at: float | None = None
        self._sleeper = sleeper
        self._monotonic = monotonic
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    async def __aenter__(self) -> "TrustedHttpClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self, url: str) -> FetchResult:
        normalized_url = normalize_url(url)
        request_url = _request_url(url)
        retrieved_at = datetime.now(UTC)
        if self.settings.respect_robots_txt and not await self._robots_allowed(normalized_url):
            return _failure_result(
                url, normalized_url, retrieved_at, QualityStatus.BLOCKED,
                "RobotsDenied", "robots.txt does not allow this crawler user-agent", 0,
            )
        cached = self._cache.load(normalized_url)
        headers: dict[str, str] = {"User-Agent": self.settings.user_agent}
        if cached:
            if cached[0].get("etag"):
                headers["If-None-Match"] = str(cached[0]["etag"])
            if cached[0].get("last_modified"):
                headers["If-Modified-Since"] = str(cached[0]["last_modified"])

        response, attempts, error = await self._request_with_retries(request_url, headers=headers)
        if response is None:
            return _failure_result(
                url, normalized_url, retrieved_at, QualityStatus.FETCH_FAILED,
                type(error).__name__ if error else "FetchError", str(error or "fetch failed"), attempts,
            )
        if response.status_code == 304 and cached:
            metadata, body = cached
            return _success_result(
                url, normalized_url, retrieved_at, body,
                status_code=304, media_type=_optional_string(metadata.get("media_type")),
                etag=_optional_string(metadata.get("etag")),
                last_modified=_optional_string(metadata.get("last_modified")),
                attempts=attempts, from_cache=True, not_modified=True,
            )
        if not 200 <= response.status_code < 300:
            return _failure_result(
                url, normalized_url, retrieved_at, QualityStatus.FETCH_FAILED,
                "HTTPStatusError", f"unexpected HTTP status {response.status_code}", attempts,
                status_code=response.status_code,
            )
        content = response.content
        if len(content) > self.settings.max_response_bytes:
            return _failure_result(
                url, normalized_url, retrieved_at, QualityStatus.VALIDATION_FAILED,
                "ResponseTooLarge",
                f"response has {len(content)} bytes; maximum is {self.settings.max_response_bytes}",
                attempts, status_code=response.status_code,
            )
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip() or None
        etag = response.headers.get("etag")
        last_modified = response.headers.get("last-modified")
        self._cache.store(
            normalized_url, content=content, status_code=response.status_code,
            media_type=media_type, etag=etag, last_modified=last_modified,
        )
        return _success_result(
            url, normalized_url, retrieved_at, content,
            status_code=response.status_code, media_type=media_type,
            etag=etag, last_modified=last_modified, attempts=attempts,
        )

    async def _request_with_retries(
        self, url: str, *, headers: dict[str, str],
    ) -> tuple[httpx.Response | None, int, Exception | None]:
        error: Exception | None = None
        response: httpx.Response | None = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                async with self._semaphore:
                    await self._wait_for_rate_limit()
                    response = await self._client.get(url, headers=headers)
                if response.status_code not in RETRYABLE_HTTP_STATUSES:
                    return response, attempt + 1, None
                error = httpx.HTTPStatusError(
                    f"retryable HTTP status {response.status_code}",
                    request=response.request, response=response,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                error = exc
                response = None
            if attempt < self.settings.max_retries:
                await self._sleeper(_backoff_seconds(attempt))
        return None if error and not isinstance(error, httpx.HTTPStatusError) else response, self.settings.max_retries + 1, error

    async def _wait_for_rate_limit(self) -> None:
        async with self._rate_lock:
            now = self._monotonic()
            if self._last_request_at is not None:
                wait = self.settings.request_interval_seconds - (now - self._last_request_at)
                if wait > 0:
                    await self._sleeper(wait)
            self._last_request_at = self._monotonic()

    async def _robots_allowed(self, normalized_url: str) -> bool:
        parts = urlsplit(normalized_url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            robots_url = f"{origin}/robots.txt"
            response, _, error = await self._request_with_retries(
                robots_url, headers={"User-Agent": self.settings.user_agent}
            )
            if error is not None and response is None:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(["User-agent: *", "Disallow: /"])
                self._robots[origin] = parser
            elif response is not None and response.status_code == 200:
                parser = urllib.robotparser.RobotFileParser()
                parser.set_url(robots_url)
                parser.parse(response.text.splitlines())
                self._robots[origin] = parser
            elif response is not None and response.status_code in {401, 403}:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(["User-agent: *", "Disallow: /"])
                self._robots[origin] = parser
            else:
                self._robots[origin] = None
        parser = self._robots[origin]
        return True if parser is None else parser.can_fetch(self.settings.user_agent, normalized_url)


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
        raise ValueError("crawler URL must be an absolute HTTP(S) URL")
    if parts.username or parts.password:
        raise ValueError("crawler URL must not contain credentials")
    host = (parts.hostname or "").casefold()
    port = parts.port
    netloc = host
    if port and not ((parts.scheme.casefold() == "http" and port == 80) or (parts.scheme.casefold() == "https" and port == 443)):
        netloc = f"{host}:{port}"
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    path = parts.path or "/"
    return urlunsplit((parts.scheme.casefold(), netloc, path, query, ""))


def _request_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
        raise ValueError("crawler URL must be an absolute HTTP(S) URL")
    if parts.username or parts.password:
        raise ValueError("crawler URL must not contain credentials")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def content_type_for(media_type: str | None, url: str) -> ContentType:
    media = (media_type or "").casefold()
    suffix = Path(urlsplit(url).path).suffix.casefold()
    if "html" in media or suffix in {".html", ".htm"}:
        return ContentType.HTML
    if "json" in media or suffix == ".json":
        return ContentType.JSON
    if "csv" in media or suffix == ".csv":
        return ContentType.CSV
    if "spreadsheet" in media or suffix == ".xlsx":
        return ContentType.XLSX
    if "pdf" in media or suffix == ".pdf":
        return ContentType.PDF
    if "xml" in media or suffix == ".xml":
        return ContentType.XML
    if media.startswith("text/"):
        return ContentType.TEXT
    return ContentType.BINARY


def _success_result(
    requested_url: str, normalized_url: str, retrieved_at: datetime, content: bytes,
    *, status_code: int, media_type: str | None, etag: str | None,
    last_modified: str | None, attempts: int, from_cache: bool = False,
    not_modified: bool = False,
) -> FetchResult:
    return FetchResult(
        requested_url=requested_url, normalized_url=normalized_url,
        retrieved_at=retrieved_at, status_code=status_code, content=content,
        content_type=content_type_for(media_type, normalized_url), media_type=media_type,
        content_hash=hashlib.sha256(content).hexdigest(), etag=etag,
        last_modified=last_modified, quality_status=QualityStatus.VALID,
        attempts=attempts, from_cache=from_cache, not_modified=not_modified,
    )


def _failure_result(
    requested_url: str, normalized_url: str, retrieved_at: datetime,
    quality_status: QualityStatus, error_type: str, error_message: str,
    attempts: int, *, status_code: int | None = None,
) -> FetchResult:
    return FetchResult(
        requested_url=requested_url, normalized_url=normalized_url,
        retrieved_at=retrieved_at, status_code=status_code, content=None,
        content_type=ContentType.BINARY, media_type=None, content_hash=None,
        etag=None, last_modified=None, quality_status=quality_status,
        attempts=attempts, error_type=error_type, error_message=error_message,
    )


def _backoff_seconds(attempt: int) -> float:
    return min(2.0 ** attempt, 30.0)


def _optional_string(value: object) -> str | None:
    return None if value is None else str(value)


def _atomic_write(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

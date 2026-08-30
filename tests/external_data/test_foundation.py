from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.external_data.acquisition import ExternalSourceAcquirer, SourceRequest
from app.external_data.config import ExternalCrawlerSettings
from app.external_data.http import TrustedHttpClient, normalize_url
from app.external_data.manifest import SnapshotStatus, SnapshotWorkspace, load_snapshot_manifest
from app.external_data.models import (
    ContentType,
    ExternalSourceRecord,
    QualityStatus,
    SourceTrustTier,
    SourceType,
    deterministic_source_record_id,
)


FIXTURES = Path(__file__).parent / "fixtures"


def _settings(tmp_path: Path, **overrides: object) -> ExternalCrawlerSettings:
    values = {
        "output_directory": tmp_path,
        "request_interval_seconds": 0.0,
        "max_retries": 2,
        "respect_robots_txt": True,
    }
    values.update(overrides)
    return ExternalCrawlerSettings(**values)  # type: ignore[arg-type]


def _record(snapshot_id: str, artifact_path: str, content: bytes) -> ExternalSourceRecord:
    digest = __import__("hashlib").sha256(content).hexdigest()
    normalized = "https://official.example/products/etf"
    return ExternalSourceRecord(
        source_record_id=deterministic_source_record_id(
            source_provider="Official Manager", source_type=SourceType.ASSET_MANAGER,
            normalized_url=normalized, raw_content_hash=digest,
        ),
        source_provider="Official Manager", source_type=SourceType.ASSET_MANAGER,
        source_trust_tier=SourceTrustTier.AUTHORITATIVE,
        source_url=normalized, normalized_url=normalized,
        retrieved_at=datetime(2026, 8, 30, tzinfo=UTC),
        published_at=None, effective_date=None, source_title=None,
        content_type=ContentType.HTML, http_status=200,
        raw_content_hash=digest, parser_version="fixture-v1",
        crawler_version="crawler-v1", snapshot_id=snapshot_id,
        quality_status=QualityStatus.VALID, raw_artifact_path=artifact_path,
    )


def test_source_record_forbids_canonical_fields_and_naive_timestamps() -> None:
    record = _record("snapshot-1", "foundation/raw/file.html", b"source")
    assert record.published_at is None
    assert record.effective_date is None
    with pytest.raises(ValidationError):
        ExternalSourceRecord.model_validate({
            **record.model_dump(), "canonical_product_id": "must-not-exist"
        })
    with pytest.raises(ValidationError):
        ExternalSourceRecord.model_validate({
            **record.model_dump(), "published_at": datetime(2026, 8, 1)
        })


def test_snapshot_preserves_raw_before_record_and_deduplicates(tmp_path: Path) -> None:
    workspace = SnapshotWorkspace(
        tmp_path, snapshot_id="snapshot-1", snapshot_date=date(2026, 8, 30),
        crawler_version="crawler-v1",
    )
    content = (FIXTURES / "sample.html").read_bytes()
    first = workspace.preserve_raw(
        category="foundation", content=content, suffix="html",
        normalized_url="https://official.example/products/etf",
        content_type="HTML",
    )
    second = workspace.preserve_raw(
        category="foundation", content=content, suffix="html",
        normalized_url="https://official.example/products/etf",
        content_type="HTML",
    )
    assert first == second
    assert workspace.manifest.raw_file_count == 1
    artifact = workspace.path / first.relative_path
    assert artifact.read_bytes() == content
    assert artifact.stat().st_ino == (tmp_path / "objects" / first.sha256[:2] / first.sha256).stat().st_ino

    record = _record(workspace.snapshot_id, first.relative_path, content)
    output = workspace.write_source_records(category="foundation", records=[record, record])
    assert output.row_count == 1
    assert len((workspace.path / output.relative_path).read_text().splitlines()) == 1
    manifest = workspace.finalize(SnapshotStatus.READY, validation={"validated": True})
    assert manifest.status is SnapshotStatus.READY
    assert manifest.parser_versions == {"foundation": "fixture-v1"}
    assert manifest.normalized_row_counts == {"external-source-record-v1": 1}
    loaded = load_snapshot_manifest(
        tmp_path, snapshot_date=date(2026, 8, 30), snapshot_id="snapshot-1"
    )
    assert loaded is not None and loaded.status is SnapshotStatus.READY
    with pytest.raises(FileExistsError):
        SnapshotWorkspace(
            tmp_path, snapshot_id="snapshot-1", snapshot_date=date(2026, 8, 30),
            crawler_version="crawler-v1",
        )


def test_http_etag_cache_and_normalized_url(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    body = (FIXTURES / "sample.json").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        if request.headers.get("if-none-match") == '"fixture-v1"':
            return httpx.Response(304, request=request)
        return httpx.Response(
            200, content=body,
            headers={"Content-Type": "application/json", "ETag": '"fixture-v1"'},
            request=request,
        )

    async def scenario() -> None:
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = TrustedHttpClient(_settings(tmp_path), client=async_client)
        first = await client.fetch("HTTPS://Official.Example/data.json?b=2&a=1#fragment")
        second = await client.fetch("https://official.example/data.json?a=1&b=2")
        await async_client.aclose()
        assert first.quality_status is QualityStatus.VALID and not first.from_cache
        assert second.from_cache and second.not_modified and second.content == body
        assert second.content_hash == first.content_hash

    asyncio.run(scenario())
    assert normalize_url("HTTPS://Official.Example/data.json?b=2&a=1#x") == (
        "https://official.example/data.json?a=1&b=2"
    )
    assert len([request for request in requests if request.url.path == "/robots.txt"]) == 1


def test_retry_is_bounded_and_robots_denial_blocks_target(tmp_path: Path) -> None:
    attempts = 0
    sleeps: list[float] = []

    def retry_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        attempts += 1
        return httpx.Response(503 if attempts < 3 else 200, text="ok", request=request)

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def retry_scenario() -> None:
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(retry_handler))
        client = TrustedHttpClient(_settings(tmp_path), client=async_client, sleeper=fake_sleep)
        result = await client.fetch("https://official.example/retry")
        await async_client.aclose()
        assert result.quality_status is QualityStatus.VALID
        assert result.attempts == 3

    asyncio.run(retry_scenario())
    assert attempts == 3 and sleeps == [1.0, 2.0]

    target_requests = 0

    def blocked_handler(request: httpx.Request) -> httpx.Response:
        nonlocal target_requests
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private", request=request)
        target_requests += 1
        return httpx.Response(200, text="unsafe", request=request)

    async def blocked_scenario() -> None:
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(blocked_handler))
        client = TrustedHttpClient(_settings(tmp_path / "blocked"), client=async_client)
        result = await client.fetch("https://official.example/private/data")
        await async_client.aclose()
        assert result.quality_status is QualityStatus.BLOCKED
        assert result.content is None

    asyncio.run(blocked_scenario())
    assert target_requests == 0


def test_acquirer_creates_source_level_provenance_without_dates(tmp_path: Path) -> None:
    body = (FIXTURES / "sample.csv").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        return httpx.Response(200, content=body, headers={"Content-Type": "text/csv"}, request=request)

    async def run(snapshot_id: str) -> tuple[str, SnapshotWorkspace]:
        workspace = SnapshotWorkspace(
            tmp_path, snapshot_id=snapshot_id, snapshot_date=date(2026, 8, 30),
            crawler_version="crawler-v1",
        )
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = TrustedHttpClient(_settings(tmp_path), client=async_client)
        result = await ExternalSourceAcquirer(client, workspace).acquire(SourceRequest(
            provider="Official Exchange", source_type=SourceType.EXCHANGE,
            trust_tier=SourceTrustTier.AUTHORITATIVE,
            url="https://exchange.example/data.csv", category="foundation",
        ))
        await async_client.aclose()
        assert result.source_record is not None
        assert result.source_record.published_at is None
        assert result.source_record.effective_date is None
        assert (workspace.path / result.source_record.raw_artifact_path).is_file()
        return result.source_record.source_record_id, workspace

    first_id, first_workspace = asyncio.run(run("snapshot-a"))
    second_id, second_workspace = asyncio.run(run("snapshot-b"))
    assert first_id == second_id
    assert first_workspace.path != second_workspace.path
    assert first_workspace.manifest.raw_artifacts[0].sha256 == second_workspace.manifest.raw_artifacts[0].sha256


def test_rate_limit_waits_between_requests(tmp_path: Path) -> None:
    clock = [0.0]
    sleeps: list[float] = []

    async def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok", request=request)

    async def scenario() -> None:
        settings = _settings(
            tmp_path, request_interval_seconds=1.5, respect_robots_txt=False,
        )
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = TrustedHttpClient(
            settings, client=async_client, sleeper=sleeper,
            monotonic=lambda: clock[0],
        )
        await client.fetch("https://official.example/one")
        await client.fetch("https://official.example/two")
        await async_client.aclose()

    asyncio.run(scenario())
    assert sleeps == [1.5]


def test_fetch_failure_is_recorded_without_artifact(tmp_path: Path) -> None:
    async def no_sleep(_: float) -> None:
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        raise httpx.ReadTimeout("fixture timeout", request=request)

    async def scenario() -> SnapshotWorkspace:
        workspace = SnapshotWorkspace(
            tmp_path, snapshot_id="failed-snapshot",
            snapshot_date=date(2026, 8, 30), crawler_version="crawler-v1",
        )
        settings = _settings(tmp_path, max_retries=1)
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = TrustedHttpClient(settings, client=async_client, sleeper=no_sleep)
        result = await ExternalSourceAcquirer(client, workspace).acquire(SourceRequest(
            provider="Official Source", source_type=SourceType.CORPORATE_DISCLOSURE,
            trust_tier=SourceTrustTier.AUTHORITATIVE,
            url="https://official.example/failing", category="foundation",
        ))
        await async_client.aclose()
        assert result.source_record is None
        return workspace

    workspace = asyncio.run(scenario())
    assert workspace.manifest.failed_source_count == 1
    assert workspace.manifest.failures[0].quality_status is QualityStatus.FETCH_FAILED
    assert workspace.manifest.failures[0].retry_count == 1
    assert workspace.manifest.raw_file_count == 0


def test_fixture_formats_are_available_offline() -> None:
    assert "공식" in (FIXTURES / "sample.html").read_text()
    assert json.loads((FIXTURES / "sample.json").read_text())["items"]
    assert "effective_date" in (FIXTURES / "sample.csv").read_text()

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.external_data.acquisition import ExternalSourceAcquirer, SourceRequest  # noqa: E402
from app.external_data.config import ExternalCrawlerSettings  # noqa: E402
from app.external_data.http import TrustedHttpClient  # noqa: E402
from app.external_data.manifest import (  # noqa: E402
    SnapshotStatus,
    SnapshotWorkspace,
    load_snapshot_manifest,
)
from app.external_data.models import SourceQualityReport, SourceTrustTier, SourceType  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Trusted external source acquisition foundation")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--request-interval", type=float)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("show-config", help="show non-secret crawler configuration")

    initialize = subparsers.add_parser("init-snapshot", help="create an empty BUILDING snapshot")
    _snapshot_arguments(initialize)

    fetch = subparsers.add_parser("fetch", help="preserve one explicitly configured source")
    _snapshot_arguments(fetch)
    fetch.add_argument("--url", required=True)
    fetch.add_argument("--provider", required=True)
    fetch.add_argument("--source-type", choices=[item.value for item in SourceType], required=True)
    fetch.add_argument("--trust-tier", choices=("1", "2", "3"), required=True)
    fetch.add_argument("--category", choices=("holdings", "corporate", "documents", "foundation"), default="foundation")
    fetch.add_argument("--title")
    fetch.add_argument("--published-at", type=_aware_datetime)
    fetch.add_argument("--effective-date", type=date.fromisoformat)
    return parser


def _snapshot_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--snapshot-date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--snapshot-id")


def _settings(args: argparse.Namespace) -> ExternalCrawlerSettings:
    settings = ExternalCrawlerSettings.from_env()
    if args.output_dir is not None:
        settings = replace(settings, output_directory=args.output_dir)
    if args.request_interval is not None:
        settings = replace(settings, request_interval_seconds=args.request_interval)
    settings.validate()
    return settings


def _snapshot_id(args: argparse.Namespace) -> str:
    return args.snapshot_id or (
        f"{args.snapshot_date.isoformat()}T{datetime.now(UTC).strftime('%H%M%SZ')}-"
        f"{uuid.uuid4().hex[:8]}"
    )


async def _fetch(args: argparse.Namespace, settings: ExternalCrawlerSettings) -> int:
    snapshot_id = _snapshot_id(args)
    existing = load_snapshot_manifest(
        settings.output_directory,
        snapshot_date=args.snapshot_date, snapshot_id=snapshot_id,
    )
    if existing is not None:
        if (
            existing.status is SnapshotStatus.READY
            and args.url in existing.source_urls
            and args.provider in existing.sources
        ):
            print(existing.model_dump_json(indent=2))
            return 0
        raise FileExistsError(
            "snapshot already exists with a different or non-READY acquisition; "
            "use a new snapshot ID"
        )
    workspace = SnapshotWorkspace(
        settings.output_directory,
        snapshot_id=snapshot_id, snapshot_date=args.snapshot_date,
        crawler_version=settings.crawler_version,
    )
    async with TrustedHttpClient(settings) as client:
        result = await ExternalSourceAcquirer(client, workspace).acquire(SourceRequest(
            provider=args.provider,
            source_type=SourceType(args.source_type),
            trust_tier=SourceTrustTier(int(args.trust_tier)),
            url=args.url, category=args.category, title=args.title,
            published_at=args.published_at, effective_date=args.effective_date,
        ))
    status = SnapshotStatus.READY if result.source_record else SnapshotStatus.FAILED
    quality_report = SourceQualityReport(
        provider=args.provider,
        trust_tier=SourceTrustTier(int(args.trust_tier)),
        access_method="HTTP(S) with robots.txt, conditional cache, and bounded retries",
        data_types=[result.fetch.content_type],
        refresh_behavior="Source-specific refresh contract not yet configured in Crawl-1",
        identity_fields_available=[],
        timestamps_available=[
            field for field, value in (
                ("published_at", args.published_at),
                ("effective_date", args.effective_date),
                ("retrieved_at", result.fetch.retrieved_at),
            ) if value is not None
        ],
        known_limitations=["Crawl-1 preserves raw evidence but does not perform domain parsing"],
        terms_and_access_constraints=["robots.txt enforced; authentication and access controls are not bypassed"],
        attempted_sources=1,
        successful_sources=1 if result.source_record else 0,
        failed_sources=0 if result.source_record else 1,
    )
    manifest = workspace.finalize(
        status,
        validation={
            "raw_before_normalized": bool(result.source_record),
            "source_record_valid": bool(result.source_record),
            "canonical_v2_writes": 0,
        }, quality_reports=[quality_report],
    )
    print(manifest.model_dump_json(indent=2))
    return 0 if status is SnapshotStatus.READY else 1


def main() -> int:
    args = _parser().parse_args()
    settings = _settings(args)
    if args.command == "show-config":
        print(json.dumps({**asdict(settings), "output_directory": str(settings.output_directory)}, indent=2))
        return 0
    if args.command == "init-snapshot":
        workspace = SnapshotWorkspace(
            settings.output_directory,
            snapshot_id=_snapshot_id(args), snapshot_date=args.snapshot_date,
            crawler_version=settings.crawler_version,
        )
        print(workspace.manifest.model_dump_json(indent=2))
        return 0
    return asyncio.run(_fetch(args, settings))


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include timezone or Z")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())

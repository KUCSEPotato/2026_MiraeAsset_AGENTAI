from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.external_data.acquisition import ExternalSourceAcquirer, SourceRequest  # noqa: E402
from app.external_data.config import ExternalCrawlerSettings  # noqa: E402
from app.external_data.http import TrustedHttpClient  # noqa: E402
from app.external_data.holdings.contract import DATA_CUTOFF_DATE  # noqa: E402
from app.external_data.holdings.kodex_production import (  # noqa: E402
    run_kodex_production_crawl,
)
from app.external_data.holdings.kodex_scope import (  # noqa: E402
    load_trusted_scope,
)
from app.external_data.holdings.ishares_production import (  # noqa: E402
    ISHARES_HISTORICAL_DATE,
    run_ishares_production_crawl,
)
from app.external_data.holdings.ishares_scope import (  # noqa: E402
    build_ishares_ready_scope,
)
from app.external_data.holdings.tiger_production import (  # noqa: E402
    run_tiger_production_crawl,
)
from app.external_data.holdings.tiger_scope import (  # noqa: E402
    build_tiger_ready_scope,
    load_trusted_tiger_scope,
)
from app.external_data.issuers.krx_kind import (  # noqa: E402
    build_krx_kind_issuer_snapshot,
)
from app.external_data.metrics.ishares_returns import (  # noqa: E402
    ISHARES_RETURN_OBSERVATION_DATE,
    ISHARES_RETURN_PROVIDER,
    ISHARES_RETURN_SCOPE,
    run_ishares_return_crawl,
)
from app.external_data.manifest import (  # noqa: E402
    SnapshotStatus,
    SnapshotWorkspace,
    load_snapshot_manifest,
)
from app.external_data.models import (  # noqa: E402
    ContentType,
    SourceQualityReport,
    SourceTrustTier,
    SourceType,
)


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

    kodex = subparsers.add_parser(
        "kodex-holdings",
        help="discover and crawl cutoff-compatible KODEX holdings",
    )
    _snapshot_arguments(kodex)
    kodex.add_argument("--pref01-data", type=Path)
    kodex.add_argument("--cutoff", type=date.fromisoformat, default=DATA_CUTOFF_DATE)
    kodex.add_argument(
        "--product-id",
        action="append",
        default=[],
        help="optional exact fId subset for live contract validation",
    )
    kodex.add_argument("--no-rerun-check", action="store_true")

    tiger = subparsers.add_parser(
        "tiger-holdings",
        help="crawl the reviewed authoritative PREF01/TIGER historical holdings universe",
    )
    _snapshot_arguments(tiger)
    tiger.add_argument("--pref01-data", type=Path)
    tiger.add_argument("--cutoff", type=date.fromisoformat, default=DATA_CUTOFF_DATE)
    tiger.add_argument("--product-isin", action="append", default=[])
    tiger.add_argument("--no-rerun-check", action="store_true")
    tiger.add_argument(
        "--kodex-snapshot-root", type=Path,
        help="optional accepted KODEX scope used to build the TIGER READY Security-reuse scope",
    )

    ishares = subparsers.add_parser(
        "ishares-holdings",
        help="crawl the reviewed authoritative PREF02/iShares historical holdings subset",
    )
    _snapshot_arguments(ishares)
    ishares.add_argument("--pref02-data", type=Path)
    ishares.add_argument("--cutoff", type=date.fromisoformat, default=DATA_CUTOFF_DATE)
    ishares.add_argument(
        "--portfolio-date", type=date.fromisoformat, default=ISHARES_HISTORICAL_DATE,
    )
    ishares.add_argument("--product-ticker", action="append", default=[])
    ishares.add_argument("--no-rerun-check", action="store_true")

    ishares_return = subparsers.add_parser(
        "ishares-returns",
        help="crawl the reviewed official iShares published one-year return scope",
    )
    _snapshot_arguments(ishares_return)
    ishares_return.add_argument("--pref02-data", type=Path)
    ishares_return.add_argument(
        "--cutoff", type=date.fromisoformat, default=DATA_CUTOFF_DATE,
    )
    ishares_return.add_argument(
        "--observation-date", type=date.fromisoformat,
        default=ISHARES_RETURN_OBSERVATION_DATE,
    )
    ishares_return.add_argument("--product-ticker", action="append", default=[])
    ishares_return.add_argument("--no-rerun-check", action="store_true")

    issuers = subparsers.add_parser(
        "krx-security-issuers",
        help="acquire exact-cutoff KRX KIND issuer evidence for the KODEX READY scope",
    )
    _snapshot_arguments(issuers)
    issuers.add_argument(
        "--kodex-snapshot-root", type=Path, required=True,
        help="root of the immutable KODEX production snapshot containing the READY scope",
    )
    issuers.add_argument(
        "--tiger-snapshot-root", type=Path,
        help="optional immutable TIGER production snapshot containing its READY scope",
    )
    issuers.add_argument("--cutoff", type=date.fromisoformat, default=DATA_CUTOFF_DATE)
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


async def _kodex_holdings(
    args: argparse.Namespace, settings: ExternalCrawlerSettings,
) -> int:
    if args.cutoff != DATA_CUTOFF_DATE:
        raise ValueError("KODEX evaluation cutoff must be 2026-08-24")
    pref01_data = args.pref01_data or _authoritative_pref01()
    snapshot_id = _snapshot_id(args)
    existing = load_snapshot_manifest(
        settings.output_directory,
        snapshot_date=args.snapshot_date,
        snapshot_id=snapshot_id,
    )
    if existing is not None and existing.status is SnapshotStatus.READY:
        print(existing.model_dump_json(indent=2))
        return 0
    workspace = (
        SnapshotWorkspace.resume(
            settings.output_directory,
            snapshot_id=snapshot_id,
            snapshot_date=args.snapshot_date,
        )
        if existing is not None else
        SnapshotWorkspace(
            settings.output_directory,
            snapshot_id=snapshot_id,
            snapshot_date=args.snapshot_date,
            crawler_version=settings.crawler_version,
            data_cutoff_date=DATA_CUTOFF_DATE,
        )
    )
    async with TrustedHttpClient(settings) as client:
        result = await run_kodex_production_crawl(
            client,
            workspace,
            pref01_data=pref01_data,
            requested_date=args.cutoff,
            selected_product_ids=frozenset(args.product_id),
            verify_rerun=not args.no_rerun_check,
        )
    quality_report = SourceQualityReport(
        provider="Samsung Asset Management KODEX",
        trust_tier=SourceTrustTier.AUTHORITATIVE,
        access_method="official paginated catalog and historical product-pdf JSON APIs",
        data_types=[ContentType.JSON],
        refresh_behavior="exact raw responses retained; historical holdings requested at cutoff",
        identity_fields_available=["fId", "ticker", "PREF01 ISIN"],
        timestamps_available=["effective_date", "retrieved_at"],
        known_limitations=[
            "catalog does not expose ISIN directly",
            "constituent ISIN is not present in the KODEX PDF JSON contract",
        ],
        terms_and_access_constraints=[
            "official public endpoints; robots.txt and configured rate limit enforced",
        ],
        attempted_sources=result.eligible_products,
        successful_sources=result.status_counts.get("SUCCESS", 0),
        failed_sources=(
            result.status_counts.get("FETCH_FAILED", 0)
            + result.status_counts.get("PARSE_FAILED", 0)
            + result.status_counts.get("CUTOFF_UNVERIFIED", 0)
        ),
    )
    validation = {
        "catalog_complete": result.catalog_count > 0,
        "catalog_resolution_reported": (
            result.matched_by_isin + result.matched_by_ticker
            + result.ambiguous + result.unmatched == result.catalog_count
        ),
        "holding_ids_stable": result.holding_ids_stable,
        "semantic_checksum_stable": result.semantic_checksum_stable,
        "first_holding_count": result.first_holding_count,
        "second_holding_count": result.second_holding_count,
        "first_semantic_checksum": result.first_semantic_checksum,
        "second_semantic_checksum": result.second_semantic_checksum,
        "cutoff_valid": result.cutoff_valid,
        "provenance_valid": result.provenance_valid,
        "known_failures_accounted": True,
        "ready_policy_reasons": list(result.ready_reasons),
        "canonical_v2_writes": 0,
    }
    status = SnapshotStatus.READY if result.ready else SnapshotStatus.PARTIAL
    manifest = workspace.finalize(
        status,
        validation=validation,
        quality_reports=[quality_report],
    )
    loaded = load_snapshot_manifest(
        settings.output_directory,
        snapshot_date=args.snapshot_date,
        snapshot_id=snapshot_id,
    )
    if loaded is None or loaded != manifest:
        workspace.finalize(
            SnapshotStatus.PARTIAL,
            validation={**validation, "manifest_round_trip": False},
            quality_reports=[quality_report],
        )
        raise ValueError("manifest strict serialization round-trip failed")
    workspace.finalize(
        status,
        validation={**validation, "manifest_round_trip": True},
        quality_reports=[quality_report],
    )
    summary = {
        "snapshot_id": snapshot_id,
        "status": status.value,
        "cutoff": args.cutoff.isoformat(),
        "catalog_products": result.catalog_count,
        "matched_by_isin": result.matched_by_isin,
        "matched_by_ticker": result.matched_by_ticker,
        "ambiguous": result.ambiguous,
        "unmatched": result.unmatched,
        "eligible_products": result.eligible_products,
        "status_counts": {
            key.value if hasattr(key, "value") else str(key): value
            for key, value in result.status_counts.items()
        },
        "first_holding_count": result.first_holding_count,
        "second_holding_count": result.second_holding_count,
        "first_semantic_checksum": result.first_semantic_checksum,
        "second_semantic_checksum": result.second_semantic_checksum,
        "holding_ids_stable": result.holding_ids_stable,
        "semantic_checksum_stable": result.semantic_checksum_stable,
        "provenance_valid": result.provenance_valid,
        "ready_reasons": list(result.ready_reasons),
        "artifact_root": str(workspace.path),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if status is SnapshotStatus.READY else 1


async def _krx_security_issuers(
    args: argparse.Namespace, settings: ExternalCrawlerSettings,
) -> int:
    if args.cutoff != DATA_CUTOFF_DATE:
        raise ValueError("KRX issuer evaluation cutoff must be 2026-08-24")
    trusted = load_trusted_scope(
        args.kodex_snapshot_root,
        canonical_snapshot_id="snapshot:kodex-long-only:20260824:v1",
    )
    tickers = {
        item.constituent_ticker
        for item in trusted.holdings
        if item.constituent_ticker is not None
    }
    if args.tiger_snapshot_root is not None:
        tiger = load_trusted_tiger_scope(args.tiger_snapshot_root)
        tickers.update(
            item.constituent_ticker for item in tiger.holdings
            if item.constituent_ticker is not None
        )
    if args.tiger_snapshot_root is None and len(tickers) != 553:
        raise ValueError("expected exactly 553 Securities in the KODEX READY scope")
    snapshot_id = _snapshot_id(args)
    existing = load_snapshot_manifest(
        settings.output_directory,
        snapshot_date=args.snapshot_date,
        snapshot_id=snapshot_id,
    )
    if existing is not None:
        if existing.status is SnapshotStatus.READY:
            print(existing.model_dump_json(indent=2))
            return 0
        raise FileExistsError("issuer snapshot exists but is not READY; use a new ID")
    workspace = SnapshotWorkspace(
        settings.output_directory,
        snapshot_id=snapshot_id,
        snapshot_date=args.snapshot_date,
        crawler_version=settings.crawler_version,
        data_cutoff_date=DATA_CUTOFF_DATE,
    )
    async with httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        follow_redirects=True,
        headers={"User-Agent": settings.user_agent},
    ) as client:
        result = await build_krx_kind_issuer_snapshot(
            client, workspace, scoped_tickers=tickers, cutoff=args.cutoff,
        )
    manifest = load_snapshot_manifest(
        settings.output_directory,
        snapshot_date=args.snapshot_date,
        snapshot_id=snapshot_id,
    )
    if manifest is None or manifest.status is not SnapshotStatus.READY:
        raise ValueError("KRX issuer manifest did not finalize as READY")
    print(json.dumps({
        "snapshot_id": snapshot_id,
        "status": manifest.status.value,
        "cutoff": args.cutoff.isoformat(),
        "scoped_securities": result.scoped_security_count,
        "issuer_records": len(result.records),
        "unresolved_tickers": list(result.unresolved_tickers),
        "conflicting_tickers": list(result.conflicting_tickers),
        "source_records": result.source_record_count,
        "raw_artifacts": result.artifact_count,
        "artifact_root": str(workspace.path),
    }, ensure_ascii=False, indent=2))
    return 0


async def _tiger_holdings(
    args: argparse.Namespace, settings: ExternalCrawlerSettings,
) -> int:
    if args.cutoff != DATA_CUTOFF_DATE:
        raise ValueError("TIGER evaluation cutoff must be 2026-08-24")
    snapshot_id = _snapshot_id(args)
    existing = load_snapshot_manifest(
        settings.output_directory, snapshot_date=args.snapshot_date,
        snapshot_id=snapshot_id,
    )
    if existing is not None:
        raise FileExistsError("TIGER snapshots are immutable; use a new snapshot ID")
    workspace = SnapshotWorkspace(
        settings.output_directory, snapshot_id=snapshot_id,
        snapshot_date=args.snapshot_date, crawler_version=settings.crawler_version,
        data_cutoff_date=DATA_CUTOFF_DATE,
    )
    async with TrustedHttpClient(settings) as client:
        result = await run_tiger_production_crawl(
            client, workspace,
            pref01_data=args.pref01_data or _authoritative_pref01(),
            requested_date=args.cutoff,
            selected_product_isins=frozenset(args.product_isin),
            verify_rerun=not args.no_rerun_check,
        )
    validation = {
        "discovered_products": result.catalog.discovered,
        "contract_candidates": len(result.catalog.contract_candidates),
        "excluded_products": result.catalog.excluded,
        "unresolved_product_identity": result.catalog.unresolved_identity,
        "first_holding_count": result.first_holding_count,
        "second_holding_count": result.second_holding_count,
        "first_semantic_checksum": result.first_semantic_checksum,
        "second_semantic_checksum": result.second_semantic_checksum,
        "idempotent_rerun": result.idempotent,
        "cutoff_valid": all(
            item.effective_date is None or item.effective_date <= DATA_CUTOFF_DATE
            for item in result.second_results
        ),
        "canonical_v2_writes": 0,
    }
    workspace.finalize(
        SnapshotStatus.PARTIAL,
        validation=validation,
        quality_reports=[SourceQualityReport(
            provider="Mirae Asset Management TIGER",
            trust_tier=SourceTrustTier.AUTHORITATIVE,
            access_method="official date-qualified portfolio deposit file HTML endpoint",
            data_types=[ContentType.HTML],
            refresh_behavior="immutable raw responses retained at exact requested fixDate",
            identity_fields_available=["PREF01 ISIN", "PREF01 ticker", "KRX constituent code"],
            timestamps_available=["effective_date from exact fixDate", "retrieved_at"],
            known_limitations=[
                "constituent ISIN and instrument type are not supplied by this endpoint",
                "the full candidate crawl remains PARTIAL until product-level safety selection",
            ],
            terms_and_access_constraints=["official public source with bounded rate limiting"],
            attempted_sources=len(result.second_results),
            successful_sources=result.status_counts.get("SUCCESS", 0),
            failed_sources=sum(
                count for status, count in result.status_counts.items() if status != "SUCCESS"
            ),
        )],
    )
    scope = None
    if args.kodex_snapshot_root is not None:
        kodex = load_trusted_scope(
            args.kodex_snapshot_root,
            canonical_snapshot_id="snapshot:kodex-long-only:20260824:v1",
        )
        reviewed = frozenset(
            item.constituent_ticker for item in kodex.holdings
            if item.constituent_ticker is not None
        )
        _, scope = build_tiger_ready_scope(
            workspace.path, reviewed_security_tickers=reviewed,
        )
    print(json.dumps({
        "snapshot_id": snapshot_id,
        "status": "PARTIAL",
        "cutoff": args.cutoff.isoformat(),
        "products_discovered": result.catalog.discovered,
        "products_crawled": len(result.second_results),
        "status_counts": dict(result.status_counts),
        "normalized_holdings": result.second_holding_count,
        "idempotent_rerun": result.idempotent,
        "ready_scope": scope.model_dump(mode="json") if scope else None,
        "artifact_root": str(workspace.path),
    }, ensure_ascii=False, indent=2))
    return 0 if result.idempotent and result.status_counts.get("SUCCESS", 0) else 1


async def _ishares_holdings(
    args: argparse.Namespace, settings: ExternalCrawlerSettings,
) -> int:
    if args.cutoff != DATA_CUTOFF_DATE:
        raise ValueError("iShares evaluation cutoff must be 2026-08-24")
    if args.portfolio_date > args.cutoff:
        raise ValueError("iShares portfolio date cannot be after the evaluation cutoff")
    snapshot_id = _snapshot_id(args)
    existing = load_snapshot_manifest(
        settings.output_directory, snapshot_date=args.snapshot_date,
        snapshot_id=snapshot_id,
    )
    if existing is not None:
        raise FileExistsError("iShares snapshots are immutable; use a new snapshot ID")
    workspace = SnapshotWorkspace(
        settings.output_directory, snapshot_id=snapshot_id,
        snapshot_date=args.snapshot_date, crawler_version=settings.crawler_version,
        data_cutoff_date=DATA_CUTOFF_DATE,
    )
    async with TrustedHttpClient(settings) as client:
        result = await run_ishares_production_crawl(
            client, workspace,
            pref02_data=args.pref02_data or _authoritative_pref02(),
            requested_date=args.portfolio_date,
            selected_tickers=frozenset(args.product_ticker),
            verify_rerun=not args.no_rerun_check,
        )
    validation = {
        "pref02_foreign_etf_products": result.catalog.foreign_etf_products,
        "pref02_with_isin": result.catalog.with_isin,
        "pref02_with_ticker": result.catalog.with_ticker,
        "pref02_with_exchange": result.catalog.with_exchange,
        "pref02_unique_isin": result.catalog.unique_isin,
        "pref02_unique_ticker_exchange": result.catalog.unique_ticker_exchange,
        "reviewed_products": len(result.catalog.reviewed_products),
        "first_holding_count": result.first_holding_count,
        "second_holding_count": result.second_holding_count,
        "first_semantic_checksum": result.first_semantic_checksum,
        "second_semantic_checksum": result.second_semantic_checksum,
        "idempotent_rerun": result.idempotent,
        "historical_portfolio_date": args.portfolio_date.isoformat(),
        "cutoff_valid": args.portfolio_date <= DATA_CUTOFF_DATE,
        "canonical_v2_writes": 0,
    }
    workspace.finalize(
        SnapshotStatus.PARTIAL,
        validation=validation,
        quality_reports=[SourceQualityReport(
            provider="BlackRock iShares",
            trust_tier=SourceTrustTier.AUTHORITATIVE,
            access_method="official date-qualified historical fund-document CSV API",
            data_types=[ContentType.CSV],
            refresh_behavior="immutable raw responses retained at official portfolio date",
            identity_fields_available=[
                "PREF02 product ISIN", "PREF02 product ticker+exchange",
                "official constituent ticker+exchange",
            ],
            timestamps_available=[
                "effective_date from CSV Fund Holdings as of", "retrieved_at",
            ],
            known_limitations=[
                "the CSV does not expose constituent ISIN",
                "generic ForeignETF remains PARTIAL",
                "issuer/company traversal is not activated without authoritative issuer mapping",
            ],
            terms_and_access_constraints=[
                "official public source with bounded rate limiting and preserved evidence",
            ],
            attempted_sources=len(result.second_results),
            successful_sources=result.status_counts.get("SUCCESS", 0),
            failed_sources=sum(
                count for status, count in result.status_counts.items()
                if status != "SUCCESS"
            ),
        )],
    )
    _, scope = build_ishares_ready_scope(workspace.path)
    print(json.dumps({
        "snapshot_id": snapshot_id,
        "status": "PARTIAL",
        "cutoff": args.cutoff.isoformat(),
        "portfolio_date": args.portfolio_date.isoformat(),
        "pref02_foreign_etf_products": result.catalog.foreign_etf_products,
        "products_crawled": len(result.second_results),
        "status_counts": dict(result.status_counts),
        "normalized_holdings": result.second_holding_count,
        "idempotent_rerun": result.idempotent,
        "ready_scope": scope.model_dump(mode="json"),
        "artifact_root": str(workspace.path),
    }, ensure_ascii=False, indent=2))
    return 0 if result.idempotent and scope.ready_product_count > 0 else 1


async def _ishares_returns(
    args: argparse.Namespace, settings: ExternalCrawlerSettings,
) -> int:
    if args.cutoff != DATA_CUTOFF_DATE:
        raise ValueError("iShares return evaluation cutoff must be 2026-08-24")
    if args.observation_date > args.cutoff:
        raise ValueError("iShares return observation date cannot be post-cutoff")
    snapshot_id = _snapshot_id(args)
    existing = load_snapshot_manifest(
        settings.output_directory,
        snapshot_date=args.snapshot_date,
        snapshot_id=snapshot_id,
    )
    if existing is not None:
        raise FileExistsError("iShares return snapshots are immutable; use a new ID")
    workspace = SnapshotWorkspace(
        settings.output_directory,
        snapshot_id=snapshot_id,
        snapshot_date=args.snapshot_date,
        crawler_version=settings.crawler_version,
        data_cutoff_date=DATA_CUTOFF_DATE,
    )
    selected = frozenset(args.product_ticker) if args.product_ticker else None
    async with TrustedHttpClient(settings) as client:
        result = await run_ishares_return_crawl(
            client,
            workspace,
            pref02_data=args.pref02_data or _authoritative_pref02(),
            requested_date=args.observation_date,
            **({"selected_tickers": selected} if selected is not None else {}),
            verify_rerun=not args.no_rerun_check,
        )
    success = result.status_counts.get("SUCCESS", 0)
    ready = (
        result.rerun_performed
        and result.idempotent
        and success == 3
        and len(result.observations) == 3
    )
    workspace.finalize(
        SnapshotStatus.READY if ready else SnapshotStatus.PARTIAL,
        validation={
            "scope": ISHARES_RETURN_SCOPE,
            "metric_code": "ONE_YEAR_RETURN",
            "observation_date": args.observation_date.isoformat(),
            "cutoff_valid": args.observation_date <= DATA_CUTOFF_DATE,
            "products": len(result.second_results),
            "successful": success,
            "metric_observations": len(result.observations),
            "first_semantic_checksum": result.first_semantic_checksum,
            "second_semantic_checksum": result.second_semantic_checksum,
            "idempotent_rerun": result.idempotent,
            "rerun_performed": result.rerun_performed,
            "return_basis": "NAV_TOTAL_RETURN",
            "distribution_treatment": "INCLUDED",
            "canonical_v2_writes": 0,
        },
        quality_reports=[SourceQualityReport(
            provider=ISHARES_RETURN_PROVIDER,
            trust_tier=SourceTrustTier.AUTHORITATIVE,
            access_method="official date-qualified product-data performance JSON API",
            data_types=[ContentType.JSON],
            refresh_behavior="immutable raw response retained for selected historical asOfDate",
            identity_fields_available=[
                "PREF02 product ISIN", "PREF02 ticker+exchange", "iShares portfolioId",
            ],
            timestamps_available=["official performance asOfDate", "retrieved_at"],
            known_limitations=[
                "scope contains only the three accepted iShares Holdings products",
                "generic ForeignETF ONE_YEAR_RETURN remains PARTIAL",
                "domestic PREF01 return basis is insufficiently documented for cross-source ranking",
            ],
            terms_and_access_constraints=[
                "official public source with bounded rate limiting and preserved raw evidence",
            ],
            attempted_sources=len(result.second_results),
            successful_sources=success,
            failed_sources=len(result.second_results) - success,
        )],
    )
    print(json.dumps({
        "snapshot_id": snapshot_id,
        "status": "READY" if ready else "PARTIAL",
        "scope": ISHARES_RETURN_SCOPE,
        "cutoff": args.cutoff.isoformat(),
        "observation_date": args.observation_date.isoformat(),
        "status_counts": dict(result.status_counts),
        "metric_observations": len(result.observations),
        "idempotent_rerun": result.idempotent,
        "rerun_performed": result.rerun_performed,
        "semantic_checksum": result.second_semantic_checksum,
        "artifact_root": str(workspace.path),
    }, ensure_ascii=False, indent=2))
    return 0 if ready else 1


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
    if args.command == "kodex-holdings":
        return asyncio.run(_kodex_holdings(args, settings))
    if args.command == "tiger-holdings":
        return asyncio.run(_tiger_holdings(args, settings))
    if args.command == "ishares-holdings":
        return asyncio.run(_ishares_holdings(args, settings))
    if args.command == "ishares-returns":
        return asyncio.run(_ishares_returns(args, settings))
    if args.command == "krx-security-issuers":
        return asyncio.run(_krx_security_issuers(args, settings))
    return asyncio.run(_fetch(args, settings))


def _authoritative_pref01() -> Path:
    matches = sorted((ROOT / "material").rglob("pref01n001_data.xlsx"))
    if len(matches) != 1:
        raise FileNotFoundError("expected one authoritative pref01n001_data.xlsx")
    return matches[0]


def _authoritative_pref02() -> Path:
    matches = sorted((ROOT / "material").rglob("pref02n001_data.xlsx"))
    if len(matches) != 1:
        raise FileNotFoundError("expected one authoritative pref02n001_data.xlsx")
    return matches[0]


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include timezone or Z")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())

"""Trusted external holdings -> canonical_v2 integration boundary.

This module consumes already-normalized provider output.  It performs no HTTP
requests and contains no provider crawler behavior.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import Connection, and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.data.v2_schema import (
    CANONICAL_V2_SCHEMA_VERSION,
    canonical_entities,
    canonical_facts,
    dataset_snapshots,
    entity_identifiers,
    entity_relations,
    external_holding_records,
    external_raw_artifacts,
    external_snapshot_manifests,
    external_source_records,
    fact_evidence_links,
    financial_products,
    holding_fact_details,
    identifier_collision_cases,
    identifier_schemes,
    securities,
    source_datasets,
    source_field_assertions,
    source_records,
)
from app.external_data.holdings.contract import DATA_CUTOFF_DATE, validate_holdings
from app.external_data.holdings.models import ExternalHolding, IdentityStatus
from app.external_data.holdings.provider_contracts import (
    KODEX_CONTRACT,
    PROVIDER_CONTRACTS,
    HoldingsProviderContract,
    provider_contract,
)
from app.external_data.models import ExternalSourceRecord, QualityStatus, SourceTrustTier


KODEX_PROVIDER = KODEX_CONTRACT.provider
HOLDINGS_TRANSFORMER_VERSION = KODEX_CONTRACT.transformer_version
_KOREAN_EQUITY_TICKER = re.compile(r"\d{6}\Z")
KODEX_CANONICAL_SNAPSHOT_ID = KODEX_CONTRACT.canonical_snapshot_id


class HoldingsIntegrationError(ValueError):
    pass


class IdentifierCollisionError(HoldingsIntegrationError):
    pass


def ensure_kodex_canonical_snapshot(
    connection: Connection,
    *,
    scope_manifest_sha256: str,
    portfolio_row_count: int,
) -> str:
    """Register the reviewed external scope as one canonical_v2 source snapshot."""

    return ensure_holdings_canonical_snapshot(
        connection,
        contract=KODEX_CONTRACT,
        scope_manifest_sha256=scope_manifest_sha256,
        portfolio_row_count=portfolio_row_count,
    )


def ensure_holdings_canonical_snapshot(
    connection: Connection,
    *,
    contract: HoldingsProviderContract,
    scope_manifest_sha256: str,
    portfolio_row_count: int,
) -> str:
    """Register one reviewed provider scope as a canonical_v2 snapshot."""

    if connection.dialect.name != "postgresql":
        raise HoldingsIntegrationError("trusted holdings canonical integration is PostgreSQL-only")
    connection.execute(
        pg_insert(source_datasets).values(
            dataset_id=contract.dataset_id,
            dataset_code=contract.dataset_code,
            display_name=contract.display_name,
            source_system=contract.provider,
            schema_contract_version=contract.schema_contract_version,
            is_authoritative=True,
        ).on_conflict_do_nothing()
    )
    schema_sha256 = hashlib.sha256(contract.schema_contract_version.encode()).hexdigest()
    connection.execute(
        pg_insert(dataset_snapshots).values(
            snapshot_id=contract.canonical_snapshot_id,
            dataset_id=contract.dataset_id,
            snapshot_date=DATA_CUTOFF_DATE,
            generation="external",
            ontology_version="merged-optical-1.4",
            semantic_mapping_version=contract.semantic_mapping_version,
            transformer_version=contract.transformer_version,
            database_schema_version=CANONICAL_V2_SCHEMA_VERSION,
            data_sha256=scope_manifest_sha256,
            schema_sha256=schema_sha256,
            source_row_count=portfolio_row_count,
            accepted_row_count=portfolio_row_count,
            quarantined_row_count=0,
            status="READY",
            reconciliation_status="PASSED",
            row_count_reconciled=True,
            metadata_json={
                "coverage_scope": contract.coverage_scope,
                "source_snapshot_status": "PARTIAL",
            },
        ).on_conflict_do_nothing()
    )
    stored = connection.execute(
        select(
            dataset_snapshots.c.data_sha256,
            dataset_snapshots.c.source_row_count,
            dataset_snapshots.c.status,
        ).where(dataset_snapshots.c.snapshot_id == contract.canonical_snapshot_id)
    ).one()
    if stored != (scope_manifest_sha256, portfolio_row_count, "READY"):
        raise HoldingsIntegrationError(
            f"existing {contract.dataset_code} canonical snapshot is incompatible"
        )
    return contract.canonical_snapshot_id


@dataclass(frozen=True, slots=True)
class TrustedHoldingsSnapshot:
    external_snapshot_id: str
    canonical_snapshot_id: str
    manifest_schema_version: str
    manifest_status: str
    manifest_sha256: str
    manifest_json: Mapping[str, Any]
    data_cutoff_date: date
    artifact_root: Path
    source_records: Sequence[ExternalSourceRecord]
    holdings: Sequence[ExternalHolding]
    manifest_path: Path | None = None
    source_snapshot_id: str | None = None
    provider_contract: HoldingsProviderContract = KODEX_CONTRACT


@dataclass(slots=True)
class HoldingsIntegrationReport:
    source_products: set[str] = field(default_factory=set)
    product_resolved: int = 0
    product_ambiguous: int = 0
    product_unresolved: int = 0
    source_constituents: int = 0
    security_resolved: int = 0
    security_created: int = 0
    security_ambiguous: int = 0
    security_unresolved: int = 0
    non_security: int = 0
    identifier_collisions: int = 0
    eligible_holdings: int = 0
    canonical_holds_facts: int = 0
    deduplicated: int = 0
    issuer_resolved: int = 0
    issuer_unresolved: int = 0


@dataclass(frozen=True, slots=True)
class _Resolution:
    status: str
    entity_id: str | None = None
    matched_by: str | None = None


class TrustedHoldingsCanonicalIntegrator:
    """Fail-closed canonicalizer for reviewed provider holdings snapshots."""

    def __init__(
        self,
        connection: Connection,
        *,
        issuer_by_security_identifier: Mapping[tuple[str, str, str], str] | None = None,
    ) -> None:
        if connection.dialect.name != "postgresql":
            raise HoldingsIntegrationError("trusted holdings canonical integration is PostgreSQL-only")
        self._connection = connection
        self._issuer_mapping = dict(issuer_by_security_identifier or {})

    def integrate(self, snapshot: TrustedHoldingsSnapshot) -> HoldingsIntegrationReport:
        self._validate_snapshot(snapshot)
        rows = validate_holdings(snapshot.holdings, snapshot_id=snapshot.external_snapshot_id)
        sources = {row.source_record_id: row for row in snapshot.source_records}
        report = HoldingsIntegrationReport()
        self._register_snapshot(snapshot)
        self._register_sources(snapshot, sources)
        for position, row in enumerate(rows, start=1):
            report.source_products.add(row.product_source_id)
            report.source_constituents += 1
            product = self._resolve_product(row)
            self._increment_resolution(report, "product", product.status)
            report.identifier_collisions += int(product.status == "AMBIGUOUS")
            security, created = self._resolve_security(row, sources[row.source_record_id])
            self._increment_resolution(report, "security", security.status)
            report.identifier_collisions += int(security.status == "AMBIGUOUS")
            if created:
                report.security_created += 1
            canonical_source_id, assertion_id = self._record_normalized_holding(
                snapshot, row, position, product.status, security.status
            )
            del canonical_source_id
            if product.status != "RESOLVED" or security.status != "RESOLVED":
                continue
            report.eligible_holdings += 1
            fact_id = self._upsert_holds_fact(
                snapshot, row, product.entity_id or "", security.entity_id or "", assertion_id
            )
            report.canonical_holds_facts += int(fact_id is not None)
            report.deduplicated += int(fact_id is None)
            if self._upsert_security_issuer(
                snapshot, row, security.entity_id or "", assertion_id
            ):
                report.issuer_resolved += 1
            else:
                report.issuer_unresolved += 1
        return report

    @staticmethod
    def _validate_snapshot(snapshot: TrustedHoldingsSnapshot) -> None:
        if snapshot.manifest_status != "READY":
            raise HoldingsIntegrationError("external holdings manifest is not READY")
        if snapshot.data_cutoff_date != DATA_CUTOFF_DATE:
            raise HoldingsIntegrationError("external holdings manifest cutoff mismatch")
        if len(snapshot.manifest_sha256) != 64:
            raise HoldingsIntegrationError("manifest SHA-256 is invalid")
        manifest_path = snapshot.manifest_path or snapshot.artifact_root / "manifest.json"
        if not manifest_path.is_file():
            raise HoldingsIntegrationError("external snapshot manifest artifact is missing")
        manifest_bytes = manifest_path.read_bytes()
        if hashlib.sha256(manifest_bytes).hexdigest() != snapshot.manifest_sha256:
            raise HoldingsIntegrationError("external snapshot manifest checksum mismatch")
        if json.loads(manifest_bytes) != dict(snapshot.manifest_json):
            raise HoldingsIntegrationError("external snapshot manifest content mismatch")
        source_ids = {item.source_record_id for item in snapshot.source_records}
        if any(item.source_record_id not in source_ids for item in snapshot.holdings):
            raise HoldingsIntegrationError("holding references an absent External SourceRecord")
        for source in snapshot.source_records:
            expected_source_snapshot = (
                snapshot.source_snapshot_id or snapshot.external_snapshot_id
            )
            if source.snapshot_id != expected_source_snapshot:
                raise HoldingsIntegrationError("External SourceRecord snapshot mismatch")
            if source.quality_status is not QualityStatus.VALID:
                raise HoldingsIntegrationError("non-VALID external source cannot create canonical facts")
            if (
                source.source_provider != snapshot.provider_contract.provider
                or source.source_trust_tier is not SourceTrustTier.AUTHORITATIVE
            ):
                raise HoldingsIntegrationError(
                    "holdings evidence does not match the reviewed provider contract"
                )
            if source.effective_date is None or source.effective_date > DATA_CUTOFF_DATE:
                raise HoldingsIntegrationError("external source effective date is missing/post-cutoff")
            artifact = (snapshot.artifact_root / source.raw_artifact_path).resolve()
            root = snapshot.artifact_root.resolve()
            if root not in artifact.parents or not artifact.is_file():
                raise HoldingsIntegrationError("external raw artifact is missing/outside snapshot")
            if hashlib.sha256(artifact.read_bytes()).hexdigest() != source.raw_content_hash:
                raise HoldingsIntegrationError("external raw artifact checksum mismatch")

    def _register_snapshot(self, snapshot: TrustedHoldingsSnapshot) -> None:
        exists = self._connection.scalar(select(dataset_snapshots.c.snapshot_id).where(
            dataset_snapshots.c.snapshot_id == snapshot.canonical_snapshot_id
        ))
        if exists is None:
            raise HoldingsIntegrationError("canonical snapshot must exist before holdings integration")
        self._insert(external_snapshot_manifests, {
            "external_snapshot_id": snapshot.external_snapshot_id,
            "canonical_snapshot_id": snapshot.canonical_snapshot_id,
            "schema_version": snapshot.manifest_schema_version,
            "status": snapshot.manifest_status,
            "data_cutoff_date": snapshot.data_cutoff_date,
            "manifest_sha256": snapshot.manifest_sha256,
            "manifest_json": dict(snapshot.manifest_json),
        })

    def _register_sources(self, snapshot: TrustedHoldingsSnapshot,
                          sources: Mapping[str, ExternalSourceRecord]) -> None:
        for source in sources.values():
            artifact_id = _stable_id("external-artifact", source.raw_content_hash, source.normalized_url)
            self._insert(external_raw_artifacts, {
                "artifact_id": artifact_id, "external_snapshot_id": snapshot.external_snapshot_id,
                "sha256": source.raw_content_hash, "relative_path": source.raw_artifact_path,
                "source_url": source.normalized_url, "content_type": source.content_type.value,
            })
            self._insert(external_source_records, {
                "external_source_record_id": source.source_record_id,
                "external_snapshot_id": snapshot.external_snapshot_id,
                "artifact_id": artifact_id, "source_provider": source.source_provider,
                "source_url": source.source_url, "effective_date": source.effective_date,
                "retrieved_at": source.retrieved_at, "trust_tier": int(source.source_trust_tier),
                "quality_status": source.quality_status.value,
                "raw_content_hash": source.raw_content_hash,
            })

    def _resolve_product(self, row: ExternalHolding) -> _Resolution:
        if row.product_isin:
            result = self._identifier("ISIN", "iso-6166", row.product_isin, "FINANCIAL_PRODUCT")
            if result.status != "UNRESOLVED":
                return self._require_etf(result)
        if row.product_ticker:
            namespace = row.product_exchange or (
                "KRX" if row.product_category.value == "DOMESTIC_ETF" else None
            )
            result = (
                self._identifier(
                    "TICKER", namespace, row.product_ticker, "FINANCIAL_PRODUCT"
                )
                if namespace is not None
                else _Resolution("UNRESOLVED")
            )
            if result.status != "UNRESOLVED":
                return self._require_etf(result)
        contract = provider_contract(row.source_provider)
        return self._require_etf(self._identifier(
            "PROVIDER_SOURCE_ID", contract.product_identifier_namespace,
            row.product_source_id, "FINANCIAL_PRODUCT"
        ))

    def _require_etf(self, result: _Resolution) -> _Resolution:
        if result.status != "RESOLVED" or result.entity_id is None:
            return result
        product_type = self._connection.scalar(select(financial_products.c.product_type_code).where(
            financial_products.c.product_id == result.entity_id
        ))
        return result if product_type == "ETF" else _Resolution("UNRESOLVED")

    def _resolve_security(self, row: ExternalHolding,
                          source: ExternalSourceRecord) -> tuple[_Resolution, bool]:
        if row.constituent_identity_status is IdentityStatus.NON_SECURITY:
            return _Resolution("NON_SECURITY"), False
        ticker_namespace = row.constituent_exchange or (
            "KRX" if row.product_category.value == "DOMESTIC_ETF" else None
        )
        candidates: list[tuple[str, str, str]] = []
        if row.constituent_isin:
            candidates.append(("ISIN", "iso-6166", row.constituent_isin))
        if row.constituent_ticker and ticker_namespace:
            candidates.append(("TICKER", ticker_namespace, row.constituent_ticker))
        contract = provider_contract(row.source_provider)
        if row.constituent_source_id:
            candidates.append((
                "PROVIDER_SOURCE_ID", contract.security_identifier_namespace,
                row.constituent_source_id,
            ))
        resolutions: list[_Resolution] = []
        for scheme, namespace, value in candidates:
            found = self._identifier(scheme, namespace, value, "SECURITY")
            if found.status == "AMBIGUOUS":
                return found, False
            if found.status == "RESOLVED":
                resolutions.append(found)
        resolved_ids = {item.entity_id for item in resolutions}
        if len(resolved_ids) > 1:
            return _Resolution("AMBIGUOUS"), False
        if resolutions:
            resolved = resolutions[0]
            self._attach_security_identifiers(
                resolved.entity_id or "", candidates, primary=False
            )
            return resolved, False

        # Creating a Security requires provider-backed identifier evidence.  A
        # verified ISIN is strongest; a six-digit KRX ticker/provider code is
        # the current KODEX equity contract.  A name can never reach this path.
        identity: tuple[str, str, str] | None = None
        if (
            row.constituent_isin
            and row.constituent_identity_status is IdentityStatus.VERIFIED_IDENTIFIER
        ):
            identity = ("ISIN", "iso-6166", row.constituent_isin.upper())
        elif row.constituent_ticker and ticker_namespace:
            identity = ("TICKER", ticker_namespace, row.constituent_ticker.upper())
        elif row.constituent_source_id and _KOREAN_EQUITY_TICKER.fullmatch(row.constituent_source_id):
            identity = (
                "PROVIDER_SOURCE_ID", contract.security_identifier_namespace,
                row.constituent_source_id,
            )
        if identity is None:
            return _Resolution("UNRESOLVED"), False
        primary_scheme, primary_namespace, primary_value = identity
        security_id = (
            f"security:{primary_namespace.casefold()}:"
            + hashlib.sha256(primary_value.encode()).hexdigest()[:24]
        )
        self._insert(canonical_entities, {
            "entity_id": security_id, "entity_kind": "SECURITY",
            "preferred_name": row.constituent_name_raw,
            "normalized_preferred_name": _normalize(row.constituent_name_raw),
            "name_status": "SOURCE_ONLY", "identity_status": "VALIDATED",
            "query_eligible": True,
        })
        self._insert(securities, {
            "security_id": security_id, "entity_kind": "SECURITY", "security_type": "EQUITY",
            "ticker": row.constituent_ticker, "isin": row.constituent_isin,
            "exchange": ticker_namespace, "issuer_resolution_status": "UNRESOLVED",
        })
        self._attach_security_identifiers(
            security_id,
            candidates,
            primary=True,
            primary_identity=(primary_scheme, primary_namespace, primary_value),
        )
        return _Resolution("RESOLVED", security_id, f"{primary_scheme}:{primary_namespace}"), True

    def _attach_security_identifiers(
        self,
        security_id: str,
        identifiers: Sequence[tuple[str, str, str]],
        *,
        primary: bool,
        primary_identity: tuple[str, str, str] | None = None,
    ) -> None:
        for scheme, namespace, raw_value in identifiers:
            value = raw_value.strip().upper()
            label = "Exchange-scoped ticker" if scheme == "TICKER" else (
                "ISO 6166 security identifier" if scheme == "ISIN"
                else "Authoritative provider security ID"
            )
            self._ensure_scheme(scheme, label, scheme == "ISIN")
            self._insert(entity_identifiers, {
                "entity_id": security_id, "scheme_code": scheme, "namespace": namespace,
                "raw_value": value, "normalized_value": value,
                "validation_status": "VALIDATED", "resolution_status": "RESOLVED",
                "conflict_status": "NONE",
                "is_primary": primary and primary_identity == (scheme, namespace, value),
                "source_record_id": None,
            })

    def _identifier(self, scheme: str, namespace: str, value: str,
                    entity_kind: str) -> _Resolution:
        normalized = value.strip().upper()
        collision = self._connection.scalar(select(identifier_collision_cases.c.collision_case_id).where(and_(
            identifier_collision_cases.c.scheme_code == scheme,
            identifier_collision_cases.c.namespace == namespace,
            identifier_collision_cases.c.normalized_value == normalized,
            identifier_collision_cases.c.status == "OPEN",
        )))
        if collision is not None:
            return _Resolution("AMBIGUOUS")
        rows = self._connection.execute(
            select(entity_identifiers.c.entity_id)
            .join(canonical_entities, canonical_entities.c.entity_id == entity_identifiers.c.entity_id)
            .where(and_(
                entity_identifiers.c.scheme_code == scheme,
                entity_identifiers.c.namespace == namespace,
                entity_identifiers.c.normalized_value == normalized,
                entity_identifiers.c.validation_status == "VALIDATED",
                entity_identifiers.c.conflict_status == "NONE",
                canonical_entities.c.entity_kind == entity_kind,
            ))
        ).scalars().all()
        unique = sorted(set(str(item) for item in rows))
        if len(unique) == 1:
            return _Resolution("RESOLVED", unique[0], f"{scheme}:{namespace}")
        if len(unique) > 1:
            collision_id = _stable_id("external-identifier-collision", scheme, namespace, normalized)
            self._insert(identifier_collision_cases, {
                "collision_case_id": collision_id, "scheme_code": scheme,
                "namespace": namespace, "normalized_value": normalized,
                "candidate_entity_ids": unique, "status": "OPEN",
                "resolution_notes": "C2 external holdings identity collision; fail closed",
            })
            return _Resolution("AMBIGUOUS")
        return _Resolution("UNRESOLVED")

    def _record_normalized_holding(self, snapshot: TrustedHoldingsSnapshot,
                                   row: ExternalHolding, position: int,
                                   product_status: str, security_status: str) -> tuple[str, str]:
        canonical_source_id = "normalized:" + row.holding_record_id
        payload = row.model_dump(mode="json")
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        self._insert(source_records, {
            "source_record_id": canonical_source_id, "snapshot_id": snapshot.canonical_snapshot_id,
            "source_primary_key": row.holding_record_id, "source_row_number": position,
            "raw_payload": payload, "normalized_payload": payload,
            "payload_sha256": payload_hash, "quality_status": row.validation_status.value,
        })
        self._insert(external_holding_records, {
            "holding_record_id": row.holding_record_id,
            "external_source_record_id": row.source_record_id,
            "canonical_source_record_id": canonical_source_id,
            "product_source_id": row.product_source_id,
            "constituent_source_id": row.constituent_source_id,
            "effective_date": row.effective_date,
            "product_resolution_status": product_status,
            "security_resolution_status": security_status,
            "normalized_payload": payload, "payload_sha256": payload_hash,
        })
        assertion_id = _stable_id("holding-assertion", row.holding_record_id, "HOLDS")
        self._insert(source_field_assertions, {
            "assertion_id": assertion_id, "source_record_id": canonical_source_id,
            "source_column": "normalized_holding", "raw_value": row.constituent_name_raw,
            "normalized_value": row.constituent_source_id,
            "mapping_category": "ENTITY_RELATION", "target_semantic_key": "holds",
            "quality_status": "VALID",
            "transformation_rule": snapshot.provider_contract.transformer_version,
        })
        return canonical_source_id, assertion_id

    def _upsert_holds_fact(self, snapshot: TrustedHoldingsSnapshot, row: ExternalHolding,
                           product_id: str, security_id: str, assertion_id: str) -> str | None:
        semantic_key = f"holds:{security_id}:{row.effective_date.isoformat()}"
        fact_id = _stable_id("fact", product_id, snapshot.canonical_snapshot_id, semantic_key)
        inserted = self._insert(canonical_facts, {
            "fact_id": fact_id, "subject_entity_id": product_id,
            "snapshot_id": snapshot.canonical_snapshot_id, "fact_kind": "ENTITY_RELATION",
            "semantic_key": semantic_key, "resolution_status": "RESOLVED",
            "valid_from": row.effective_date, "valid_to": row.effective_date,
        })
        self._insert(entity_relations, {
            "fact_id": fact_id, "subject_entity_id": product_id,
            "relation_type": "HOLDS", "object_entity_id": security_id,
        })
        weight_ok = (
            row.weight_normalized is not None
            and row.weight_unit is not None
            and row.weight_scale is not None
            and row.source_provider in {item.provider for item in PROVIDER_CONTRACTS.values()}
        )
        self._insert(holding_fact_details, {
            "fact_id": fact_id, "effective_date": row.effective_date,
            "weight_normalized": row.weight_normalized if weight_ok else None,
            "weight_unit": row.weight_unit.value if weight_ok else None,
            "weight_scale": row.weight_scale.value if weight_ok else None,
            "source_provider": row.source_provider,
            "external_holding_record_id": row.holding_record_id,
        })
        self._insert(fact_evidence_links, {
            "fact_id": fact_id, "assertion_id": assertion_id, "evidence_role": "SUPPORTS",
        })
        return fact_id if inserted else None

    def _upsert_security_issuer(self, snapshot: TrustedHoldingsSnapshot,
                                row: ExternalHolding, security_id: str,
                                assertion_id: str) -> bool:
        keys = []
        if row.constituent_isin:
            keys.append(("ISIN", "iso-6166", row.constituent_isin.upper()))
        if row.constituent_ticker:
            ticker_namespace = row.constituent_exchange or (
                "KRX" if row.product_category.value == "DOMESTIC_ETF" else None
            )
            if ticker_namespace is not None:
                keys.append((
                    "TICKER", ticker_namespace, row.constituent_ticker.upper()
                ))
        if row.constituent_source_id:
            contract = provider_contract(row.source_provider)
            keys.append((
                "PROVIDER_SOURCE_ID", contract.security_identifier_namespace,
                row.constituent_source_id.upper(),
            ))
        organization_id = next((self._issuer_mapping[key] for key in keys if key in self._issuer_mapping), None)
        if organization_id is None:
            return False
        valid_org = self._connection.scalar(select(canonical_entities.c.entity_id).where(and_(
            canonical_entities.c.entity_id == organization_id,
            canonical_entities.c.entity_kind == "ORGANIZATION",
            canonical_entities.c.identity_status == "VALIDATED",
        )))
        if valid_org is None:
            raise HoldingsIntegrationError("issuer mapping target is not a validated Organization")
        semantic_key = f"securityIssuedBy:{organization_id}"
        fact_id = _stable_id("fact", security_id, snapshot.canonical_snapshot_id, semantic_key)
        self._insert(canonical_facts, {
            "fact_id": fact_id, "subject_entity_id": security_id,
            "snapshot_id": snapshot.canonical_snapshot_id, "fact_kind": "ENTITY_RELATION",
            "semantic_key": semantic_key, "resolution_status": "RESOLVED",
        })
        self._insert(entity_relations, {
            "fact_id": fact_id, "subject_entity_id": security_id,
            "relation_type": "SECURITY_ISSUED_BY", "object_entity_id": organization_id,
        })
        self._insert(fact_evidence_links, {
            "fact_id": fact_id, "assertion_id": assertion_id, "evidence_role": "DERIVES",
        })
        self._connection.execute(securities.update().where(
            securities.c.security_id == security_id
        ).values(issuer_resolution_status="RESOLVED"))
        return True

    def _ensure_scheme(self, code: str, label: str, globally_unique: bool) -> None:
        self._insert(identifier_schemes, {
            "scheme_code": code, "label": label, "default_namespace": None,
            "validation_pattern": None, "is_globally_unique": globally_unique,
        })

    def _insert(self, table, values: Mapping[str, Any]) -> bool:
        statement = (
            pg_insert(table)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(*table.primary_key.columns)
        )
        return self._connection.execute(statement).first() is not None

    @staticmethod
    def _increment_resolution(report: HoldingsIntegrationReport, prefix: str, status: str) -> None:
        if status == "NON_SECURITY":
            report.non_security += 1
            return
        setattr(report, f"{prefix}_{status.casefold()}", getattr(report, f"{prefix}_{status.casefold()}") + 1)


def _stable_id(kind: str, *parts: str) -> str:
    payload = "|".join((kind, *parts))
    return f"{kind}:" + hashlib.sha256(payload.encode()).hexdigest()


def _normalize(value: str) -> str:
    return "".join(value.casefold().split())

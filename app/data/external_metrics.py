"""Authoritative external metric observations -> canonical_v2 integration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
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
    external_metric_records,
    external_raw_artifacts,
    external_snapshot_manifests,
    external_source_records,
    fact_evidence_links,
    financial_products,
    metric_definitions,
    metric_observations,
    source_datasets,
    source_field_assertions,
    source_record_entities,
    source_records,
)
from app.external_data.holdings.contract import DATA_CUTOFF_DATE
from app.external_data.metrics.ishares_returns import (
    ISHARES_RETURN_DATASET_CODE,
    ISHARES_RETURN_PROVIDER,
    ISHARES_RETURN_SCOPE,
    ISHARES_RETURN_TRANSFORMER_VERSION,
)
from app.external_data.metrics.models import ExternalMetricObservation
from app.external_data.models import ExternalSourceRecord, QualityStatus, SourceTrustTier


ISHARES_RETURN_CANONICAL_SNAPSHOT_ID = (
    "snapshot:ishares-us-one-year-return:20260824:v1"
)
ISHARES_RETURN_DATASET_ID = "dataset:ishares-us-performance"


class ExternalMetricIntegrationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TrustedMetricSnapshot:
    external_snapshot_id: str
    canonical_snapshot_id: str
    manifest_schema_version: str
    manifest_status: str
    manifest_sha256: str
    manifest_json: Mapping[str, Any]
    data_cutoff_date: date
    artifact_root: Path
    source_records: Sequence[ExternalSourceRecord]
    observations: Sequence[ExternalMetricObservation]
    manifest_path: Path


@dataclass(slots=True)
class ExternalMetricIntegrationReport:
    observations: int = 0
    product_resolved: int = 0
    product_ambiguous: int = 0
    product_unresolved: int = 0
    canonical_metric_facts: int = 0
    deduplicated: int = 0
    evidence_links: int = 0


def load_trusted_ishares_return_snapshot(snapshot_root: Path) -> TrustedMetricSnapshot:
    manifest_path = snapshot_root / "manifest.json"
    if not manifest_path.is_file():
        raise ExternalMetricIntegrationError("external metric manifest is missing")
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    observations_path = snapshot_root / "metrics/normalized/metric_observations.jsonl"
    sources_path = snapshot_root / "metrics/normalized/source_records.jsonl"
    if not observations_path.is_file() or not sources_path.is_file():
        raise ExternalMetricIntegrationError("external metric normalized evidence is missing")
    _verify_normalized_output(manifest, snapshot_root, observations_path)
    _verify_normalized_output(manifest, snapshot_root, sources_path)
    observations = tuple(
        ExternalMetricObservation.model_validate_json(line)
        for line in observations_path.read_text(encoding="utf-8").splitlines() if line
    )
    sources = tuple(
        ExternalSourceRecord.model_validate_json(line)
        for line in sources_path.read_text(encoding="utf-8").splitlines() if line
    )
    cutoff = manifest.get("data_cutoff_date")
    if cutoff is None:
        raise ExternalMetricIntegrationError("external metric cutoff is missing")
    validation = manifest.get("validation") or {}
    if (
        validation.get("scope") != ISHARES_RETURN_SCOPE
        or validation.get("metric_code") != "ONE_YEAR_RETURN"
        or validation.get("metric_observations") != len(observations)
        or manifest.get("source_record_count") != len(sources)
    ):
        raise ExternalMetricIntegrationError(
            "external metric manifest accounting/contract mismatch"
        )
    return TrustedMetricSnapshot(
        external_snapshot_id=str(manifest["snapshot_id"]),
        canonical_snapshot_id=ISHARES_RETURN_CANONICAL_SNAPSHOT_ID,
        manifest_schema_version=str(manifest["schema_version"]),
        manifest_status=str(manifest["status"]),
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        manifest_json=manifest,
        data_cutoff_date=date.fromisoformat(str(cutoff)),
        artifact_root=snapshot_root,
        source_records=sources,
        observations=observations,
        manifest_path=manifest_path,
    )


def ensure_ishares_return_canonical_snapshot(
    connection: Connection, *, manifest_sha256: str, observation_count: int,
) -> str:
    if connection.dialect.name != "postgresql":
        raise ExternalMetricIntegrationError("external metric integration is PostgreSQL-only")
    connection.execute(pg_insert(source_datasets).values(
        dataset_id=ISHARES_RETURN_DATASET_ID,
        dataset_code=ISHARES_RETURN_DATASET_CODE,
        display_name="iShares published one-year NAV total return",
        source_system=ISHARES_RETURN_PROVIDER,
        schema_contract_version="external-ishares-one-year-return-v1",
        is_authoritative=True,
    ).on_conflict_do_nothing())
    schema_sha = hashlib.sha256(b"external-ishares-one-year-return-v1").hexdigest()
    connection.execute(pg_insert(dataset_snapshots).values(
        snapshot_id=ISHARES_RETURN_CANONICAL_SNAPSHOT_ID,
        dataset_id=ISHARES_RETURN_DATASET_ID,
        snapshot_date=DATA_CUTOFF_DATE,
        generation="external",
        ontology_version="merged-optical-1.4",
        semantic_mapping_version="c3.0-ishares-return-scope-v1",
        transformer_version=ISHARES_RETURN_TRANSFORMER_VERSION,
        database_schema_version=CANONICAL_V2_SCHEMA_VERSION,
        data_sha256=manifest_sha256,
        schema_sha256=schema_sha,
        source_row_count=observation_count,
        accepted_row_count=observation_count,
        quarantined_row_count=0,
        status="READY",
        reconciliation_status="PASSED",
        row_count_reconciled=True,
        metadata_json={
            "scope": ISHARES_RETURN_SCOPE,
            "metric_code": "ONE_YEAR_RETURN",
            "return_basis": "NAV_TOTAL_RETURN",
        },
    ).on_conflict_do_nothing())
    stored = connection.execute(select(
        dataset_snapshots.c.data_sha256,
        dataset_snapshots.c.source_row_count,
        dataset_snapshots.c.status,
    ).where(
        dataset_snapshots.c.snapshot_id == ISHARES_RETURN_CANONICAL_SNAPSHOT_ID
    )).one()
    if stored != (manifest_sha256, observation_count, "READY"):
        raise ExternalMetricIntegrationError(
            "existing iShares return canonical snapshot is incompatible"
        )
    return ISHARES_RETURN_CANONICAL_SNAPSHOT_ID


class TrustedExternalMetricIntegrator:
    def __init__(self, connection: Connection) -> None:
        if connection.dialect.name != "postgresql":
            raise ExternalMetricIntegrationError("external metric integration is PostgreSQL-only")
        self._connection = connection

    def integrate(self, snapshot: TrustedMetricSnapshot) -> ExternalMetricIntegrationReport:
        self._validate(snapshot)
        self._register_snapshot(snapshot)
        sources = {item.source_record_id: item for item in snapshot.source_records}
        self._register_sources(snapshot, sources)
        self._ensure_metric_definition()
        report = ExternalMetricIntegrationReport(observations=len(snapshot.observations))
        for position, observation in enumerate(
            sorted(snapshot.observations, key=lambda item: item.metric_observation_id), start=1
        ):
            resolution = self._resolve_product(observation)
            setattr(
                report, f"product_{resolution[0].casefold()}",
                getattr(report, f"product_{resolution[0].casefold()}") + 1,
            )
            source_id, assertion_id = self._record_observation(
                snapshot, observation, position, resolution[0]
            )
            if resolution[0] != "RESOLVED" or resolution[1] is None:
                continue
            self._insert(source_record_entities, {
                "source_record_id": source_id,
                "entity_id": resolution[1],
                "entity_kind": "FINANCIAL_PRODUCT",
                "provenance_role": "DESCRIBES",
            })
            inserted = self._upsert_metric(
                snapshot, observation, resolution[1], assertion_id
            )
            report.canonical_metric_facts += int(inserted)
            report.deduplicated += int(not inserted)
            report.evidence_links += 1
        return report

    @staticmethod
    def _validate(snapshot: TrustedMetricSnapshot) -> None:
        if snapshot.manifest_status != "READY":
            raise ExternalMetricIntegrationError("external metric manifest is not READY")
        if snapshot.data_cutoff_date != DATA_CUTOFF_DATE:
            raise ExternalMetricIntegrationError("external metric cutoff mismatch")
        if hashlib.sha256(snapshot.manifest_path.read_bytes()).hexdigest() != snapshot.manifest_sha256:
            raise ExternalMetricIntegrationError("external metric manifest checksum mismatch")
        if json.loads(snapshot.manifest_path.read_bytes()) != dict(snapshot.manifest_json):
            raise ExternalMetricIntegrationError("external metric manifest content mismatch")
        source_ids = {item.source_record_id for item in snapshot.source_records}
        if len(source_ids) != len(snapshot.source_records):
            raise ExternalMetricIntegrationError("duplicate external metric SourceRecord")
        if any(item.source_record_id not in source_ids for item in snapshot.observations):
            raise ExternalMetricIntegrationError("metric references an absent SourceRecord")
        semantic_ids = {item.metric_observation_id for item in snapshot.observations}
        if len(semantic_ids) != len(snapshot.observations):
            raise ExternalMetricIntegrationError("duplicate semantic metric observation")
        for source in snapshot.source_records:
            if source.snapshot_id != snapshot.external_snapshot_id:
                raise ExternalMetricIntegrationError("external metric SourceRecord snapshot mismatch")
            if source.source_provider != ISHARES_RETURN_PROVIDER:
                raise ExternalMetricIntegrationError("external metric provider mismatch")
            if source.source_trust_tier is not SourceTrustTier.AUTHORITATIVE:
                raise ExternalMetricIntegrationError("metric source is not authoritative")
            if source.quality_status is not QualityStatus.VALID:
                raise ExternalMetricIntegrationError("non-VALID metric source")
            if source.effective_date is None or source.effective_date > DATA_CUTOFF_DATE:
                raise ExternalMetricIntegrationError("metric source is missing/post-cutoff")
            artifact = (snapshot.artifact_root / source.raw_artifact_path).resolve()
            if snapshot.artifact_root.resolve() not in artifact.parents or not artifact.is_file():
                raise ExternalMetricIntegrationError("metric raw artifact is missing/outside snapshot")
            if hashlib.sha256(artifact.read_bytes()).hexdigest() != source.raw_content_hash:
                raise ExternalMetricIntegrationError("metric raw artifact checksum mismatch")
        for observation in snapshot.observations:
            if (
                observation.metric_code != "ONE_YEAR_RETURN"
                or observation.observation_end_date > DATA_CUTOFF_DATE
                or not observation.cutoff_valid
                or observation.return_basis != "NAV_TOTAL_RETURN"
                or observation.distribution_treatment != "INCLUDED"
            ):
                raise ExternalMetricIntegrationError("metric observation violates scoped contract")

    def _register_snapshot(self, snapshot: TrustedMetricSnapshot) -> None:
        exists = self._connection.scalar(select(dataset_snapshots.c.snapshot_id).where(
            dataset_snapshots.c.snapshot_id == snapshot.canonical_snapshot_id
        ))
        if exists is None:
            raise ExternalMetricIntegrationError("canonical metric snapshot must exist first")
        self._insert(external_snapshot_manifests, {
            "external_snapshot_id": snapshot.external_snapshot_id,
            "canonical_snapshot_id": snapshot.canonical_snapshot_id,
            "schema_version": snapshot.manifest_schema_version,
            "status": snapshot.manifest_status,
            "data_cutoff_date": snapshot.data_cutoff_date,
            "manifest_sha256": snapshot.manifest_sha256,
            "manifest_json": dict(snapshot.manifest_json),
        })

    def _register_sources(
        self, snapshot: TrustedMetricSnapshot,
        sources: Mapping[str, ExternalSourceRecord],
    ) -> None:
        for source in sources.values():
            artifact_id = _stable_id(
                "external-artifact", source.raw_content_hash, source.normalized_url
            )
            self._insert(external_raw_artifacts, {
                "artifact_id": artifact_id,
                "external_snapshot_id": snapshot.external_snapshot_id,
                "sha256": source.raw_content_hash,
                "relative_path": source.raw_artifact_path,
                "source_url": source.normalized_url,
                "content_type": source.content_type.value,
            })
            self._insert(external_source_records, {
                "external_source_record_id": source.source_record_id,
                "external_snapshot_id": snapshot.external_snapshot_id,
                "artifact_id": artifact_id,
                "source_provider": source.source_provider,
                "source_url": source.source_url,
                "effective_date": source.effective_date,
                "retrieved_at": source.retrieved_at,
                "trust_tier": int(source.source_trust_tier),
                "quality_status": source.quality_status.value,
                "raw_content_hash": source.raw_content_hash,
            })

    def _ensure_metric_definition(self) -> None:
        self._insert(metric_definitions, {
            "metric_code": "ONE_YEAR_RETURN",
            "canonical_field": "product.one_year_return",
            "label": "Exact one-year source return",
            "value_type": "NUMERIC",
            "expected_unit": "PERCENT",
            "expected_scale_basis": "SOURCE_SCOPED",
            "cross_source_comparable": False,
            "filter_enabled": False,
            "sort_enabled": True,
        })

    def _resolve_product(
        self, observation: ExternalMetricObservation,
    ) -> tuple[str, str | None]:
        candidates = [
            ("ISIN", "iso-6166", observation.product_isin),
            ("TICKER", observation.product_exchange, observation.product_ticker),
            ("PROVIDER_SOURCE_ID", "ISHARES_US", observation.product_source_id),
        ]
        resolved: set[str] = set()
        for scheme, namespace, value in candidates:
            rows = self._connection.execute(select(entity_identifiers.c.entity_id).join(
                canonical_entities,
                canonical_entities.c.entity_id == entity_identifiers.c.entity_id,
            ).where(and_(
                entity_identifiers.c.scheme_code == scheme,
                entity_identifiers.c.namespace == namespace,
                entity_identifiers.c.normalized_value == value.upper(),
                entity_identifiers.c.validation_status == "VALIDATED",
                entity_identifiers.c.conflict_status == "NONE",
                canonical_entities.c.entity_kind == "FINANCIAL_PRODUCT",
            ))).scalars().all()
            resolved.update(str(item) for item in rows)
        if len(resolved) > 1:
            return "AMBIGUOUS", None
        if not resolved:
            return "UNRESOLVED", None
        entity_id = next(iter(resolved))
        product_type = self._connection.scalar(select(
            financial_products.c.product_type_code
        ).where(financial_products.c.product_id == entity_id))
        return ("RESOLVED", entity_id) if product_type == "ETF" else ("UNRESOLVED", None)

    def _record_observation(
        self, snapshot: TrustedMetricSnapshot,
        observation: ExternalMetricObservation,
        position: int,
        resolution_status: str,
    ) -> tuple[str, str]:
        source_id = "normalized:" + observation.metric_observation_id
        payload = observation.model_dump(mode="json")
        payload["external_source_record_id"] = observation.source_record_id
        payload["product_resolution_status"] = resolution_status
        payload_json = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        self._insert(source_records, {
            "source_record_id": source_id,
            "snapshot_id": snapshot.canonical_snapshot_id,
            "source_primary_key": observation.metric_observation_id,
            "source_row_number": position,
            "raw_payload": payload,
            "normalized_payload": payload,
            "payload_sha256": hashlib.sha256(payload_json.encode()).hexdigest(),
            "quality_status": "VALID",
        })
        self._insert(external_metric_records, {
            "metric_observation_id": observation.metric_observation_id,
            "external_source_record_id": observation.source_record_id,
            "canonical_source_record_id": source_id,
            "product_source_id": observation.product_source_id,
            "metric_code": observation.metric_code,
            "observation_end_date": observation.observation_end_date,
            "product_resolution_status": resolution_status,
            "normalized_payload": payload,
            "payload_sha256": hashlib.sha256(payload_json.encode()).hexdigest(),
        })
        assertion_id = _stable_id(
            "metric-assertion", observation.metric_observation_id, observation.metric_code
        )
        self._insert(source_field_assertions, {
            "assertion_id": assertion_id,
            "source_record_id": source_id,
            "source_column": "oneYearAnnualized.navSourced",
            "raw_value": observation.raw_value,
            "normalized_value": str(observation.numeric_value),
            "mapping_category": "METRIC",
            "target_semantic_key": "product.one_year_return",
            "quality_status": "VALID",
            "transformation_rule": observation.transformer_version,
        })
        return source_id, assertion_id

    def _upsert_metric(
        self, snapshot: TrustedMetricSnapshot,
        observation: ExternalMetricObservation,
        product_id: str,
        assertion_id: str,
    ) -> bool:
        semantic_key = (
            f"ONE_YEAR_RETURN:{observation.return_basis}:"
            f"{observation.observation_end_date.isoformat()}"
        )
        fact_id = _stable_id(
            "fact", product_id, snapshot.canonical_snapshot_id, semantic_key
        )
        inserted = self._insert(canonical_facts, {
            "fact_id": fact_id,
            "subject_entity_id": product_id,
            "snapshot_id": snapshot.canonical_snapshot_id,
            "fact_kind": "METRIC",
            "semantic_key": semantic_key,
            "resolution_status": "RESOLVED",
            "valid_from": observation.observation_end_date,
            "valid_to": observation.observation_end_date,
        })
        self._insert(metric_observations, {
            "fact_id": fact_id,
            "metric_code": observation.metric_code,
            "subject_entity_id": product_id,
            "raw_value": observation.raw_value,
            "numeric_value": observation.numeric_value,
            "unit": observation.unit,
            "scale_basis": observation.scale_basis,
            "currency": observation.currency,
            "observed_on": observation.observation_end_date,
            "quality_status": "SOURCE_ZERO" if observation.numeric_value == 0 else "VALID",
            "comparability_status": "COMPARABLE",
        })
        self._insert(fact_evidence_links, {
            "fact_id": fact_id,
            "assertion_id": assertion_id,
            "evidence_role": "SUPPORTS",
        })
        return inserted

    def _insert(self, table, values: Mapping[str, Any]) -> bool:
        statement = pg_insert(table).values(**values).on_conflict_do_nothing().returning(
            *table.primary_key.columns
        )
        return self._connection.execute(statement).first() is not None


def _stable_id(kind: str, *parts: str) -> str:
    return f"{kind}:" + hashlib.sha256("|".join((kind, *parts)).encode()).hexdigest()


def _verify_normalized_output(
    manifest: Mapping[str, Any], snapshot_root: Path, path: Path,
) -> None:
    relative = path.relative_to(snapshot_root).as_posix()
    matches = [
        item for item in manifest.get("normalized_outputs", [])
        if item.get("relative_path") == relative
    ]
    if len(matches) != 1:
        raise ExternalMetricIntegrationError(
            f"normalized output is absent/ambiguous in manifest: {relative}"
        )
    payload = path.read_bytes()
    line_count = len([line for line in payload.splitlines() if line])
    if (
        hashlib.sha256(payload).hexdigest() != matches[0].get("sha256")
        or line_count != matches[0].get("row_count")
    ):
        raise ExternalMetricIntegrationError(
            f"normalized output checksum/count mismatch: {relative}"
        )

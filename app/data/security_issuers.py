"""Authoritative KRX issuer evidence projection into canonical_v2."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import Connection, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.data.cleaning import normalize_lookup_value
from app.data.holdings import HoldingsIntegrationError
from app.data.v2_schema import (
    CANONICAL_V2_SCHEMA_VERSION,
    canonical_entities,
    canonical_facts,
    dataset_snapshots,
    entity_aliases,
    entity_identifiers,
    entity_relations,
    external_raw_artifacts,
    external_security_issuer_records,
    external_snapshot_manifests,
    external_source_records,
    fact_evidence_links,
    identifier_schemes,
    organizations,
    securities,
    source_datasets,
    source_field_assertions,
    source_record_entities,
    source_records,
)
from app.external_data.holdings.contract import DATA_CUTOFF_DATE
from app.external_data.issuers.krx_kind import (
    KRX_KIND_ISSUER_SNAPSHOT_SCHEMA,
    KRX_KIND_PROVIDER,
    load_issuer_records,
)
from app.external_data.issuers.models import (
    ExternalSecurityIssuerRecord,
    IssuerIdentityStatus,
)
from app.external_data.manifest import ExternalSnapshotManifest, SnapshotStatus
from app.external_data.models import ExternalSourceRecord, QualityStatus, SourceTrustTier
from app.ontology.runtime_mapping import ONTOLOGY_VERSION


KRX_ISSUER_DATASET_ID = "dataset:krx-kind-security-issuer"
KRX_ISSUER_DATASET_CODE = "KRX_SECURITY_ISSUER"
KRX_ISSUER_CANONICAL_SNAPSHOT_ID = "snapshot:krx-kind-security-issuer:20260824:v1"
MULTI_PROVIDER_ISSUER_SCOPE = "KODEX_TIGER_LONG_ONLY_COMPATIBLE"
MULTI_PROVIDER_ISSUER_CANONICAL_SNAPSHOT_ID = (
    "snapshot:krx-kind-security-issuer:20260824:v2"
)
KRX_ISSUER_TRANSFORMER_VERSION = "m10.9-c2.6-krx-kind-issuer-1"
_LEGAL_FORM = re.compile(
    r"^(?:주식회사|\(주\)|㈜)|(?:주식회사|\(주\)|㈜)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class TrustedIssuerSnapshot:
    artifact_root: Path
    external_snapshot_id: str
    canonical_snapshot_id: str
    manifest: ExternalSnapshotManifest
    manifest_sha256: str
    source_records: Sequence[ExternalSourceRecord]
    issuer_records: Sequence[ExternalSecurityIssuerRecord]
    coverage_scope: str = "KODEX_LONG_ONLY_COMPATIBLE"


@dataclass(slots=True)
class IssuerIntegrationReport:
    eligible_records: int = 0
    security_resolved: int = 0
    security_ambiguous: int = 0
    security_conflict: int = 0
    security_unresolved: int = 0
    organization_existing: int = 0
    organization_created: int = 0
    organization_ambiguous: int = 0
    organization_conflict: int = 0
    organization_unresolved: int = 0
    canonical_facts: int = 0
    evidence_links: int = 0
    deduplicated: int = 0


def load_trusted_issuer_snapshot(
    snapshot_root: Path,
    *,
    canonical_snapshot_id: str = KRX_ISSUER_CANONICAL_SNAPSHOT_ID,
    coverage_scope: str = "KODEX_LONG_ONLY_COMPATIBLE",
) -> TrustedIssuerSnapshot:
    manifest_path = snapshot_root / "manifest.json"
    payload = manifest_path.read_bytes()
    manifest = ExternalSnapshotManifest.model_validate_json(payload)
    sources = tuple(
        ExternalSourceRecord.model_validate_json(line)
        for line in (
            snapshot_root / "issuers" / "normalized" / "source_records.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        if line
    )
    return TrustedIssuerSnapshot(
        artifact_root=snapshot_root,
        external_snapshot_id=manifest.snapshot_id,
        canonical_snapshot_id=canonical_snapshot_id,
        manifest=manifest,
        manifest_sha256=hashlib.sha256(payload).hexdigest(),
        source_records=sources,
        issuer_records=load_issuer_records(snapshot_root),
        coverage_scope=coverage_scope,
    )


class TrustedSecurityIssuerIntegrator:
    """Fail-closed issuer canonicalizer using KRX identifiers, never names alone."""

    def __init__(self, connection: Connection) -> None:
        if connection.dialect.name != "postgresql":
            raise HoldingsIntegrationError("trusted issuer integration is PostgreSQL-only")
        self._connection = connection

    def integrate(self, snapshot: TrustedIssuerSnapshot) -> IssuerIntegrationReport:
        self._validate_snapshot(snapshot)
        report = IssuerIntegrationReport(eligible_records=len(snapshot.issuer_records))
        self._register_snapshot(snapshot)
        self._register_external_sources(snapshot)
        self._ensure_scheme(
            "PROVIDER_ORGANIZATION_ID",
            "Authoritative provider organization identifier",
            False,
        )
        ticker_conflicts, issuer_conflicts = self._source_conflicts(
            snapshot.issuer_records
        )
        for position, row in enumerate(
            sorted(snapshot.issuer_records, key=lambda item: item.issuer_record_id),
            start=1,
        ):
            security_status, security_id = self._resolve_security(row)
            if row.security_ticker in ticker_conflicts:
                security_status, security_id = "CONFLICT", None
            canonical_source_id, assertion_id = self._record_source_row(
                snapshot, row, position, security_status,
                "CONFLICT" if row.issuer_source_id in issuer_conflicts else "RESOLVED",
            )
            self._increment(report, "security", security_status)
            if security_id is None:
                continue
            self._insert(source_record_entities, {
                "source_record_id": canonical_source_id,
                "entity_id": security_id,
                "entity_kind": "SECURITY",
                "provenance_role": "DESCRIBES",
            })
            if row.issuer_source_id in issuer_conflicts:
                self._increment(report, "organization", "CONFLICT")
                self._set_security_status(security_id, "UNRESOLVED")
                continue
            organization_status, organization_id, created = self._resolve_organization(
                row, canonical_source_id
            )
            self._update_resolution_status(
                row.issuer_record_id, security_status, organization_status
            )
            self._increment(report, "organization", organization_status)
            if created:
                report.organization_created += 1
                report.organization_existing -= 1
            if organization_id is None:
                self._set_security_status(
                    security_id,
                    "AMBIGUOUS" if organization_status == "AMBIGUOUS" else "UNRESOLVED",
                )
                continue
            inserted = self._upsert_fact(
                snapshot, row, security_id, organization_id, assertion_id
            )
            report.canonical_facts += int(inserted)
            report.deduplicated += int(not inserted)
            report.evidence_links += 1
            self._set_security_status(security_id, "RESOLVED")
        return report

    @staticmethod
    def _validate_snapshot(snapshot: TrustedIssuerSnapshot) -> None:
        manifest = snapshot.manifest
        if manifest.status is not SnapshotStatus.READY:
            raise HoldingsIntegrationError("issuer source manifest is not READY")
        if manifest.data_cutoff_date != DATA_CUTOFF_DATE:
            raise HoldingsIntegrationError("issuer source cutoff mismatch")
        if manifest.validation.get("schema_version") != KRX_KIND_ISSUER_SNAPSHOT_SCHEMA:
            raise HoldingsIntegrationError("issuer source schema version mismatch")
        manifest_path = snapshot.artifact_root / "manifest.json"
        if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != snapshot.manifest_sha256:
            raise HoldingsIntegrationError("issuer source manifest checksum mismatch")
        source_ids = {item.source_record_id for item in snapshot.source_records}
        for source in snapshot.source_records:
            if (
                source.snapshot_id != snapshot.external_snapshot_id
                or source.source_provider != KRX_KIND_PROVIDER
                or source.source_trust_tier is not SourceTrustTier.AUTHORITATIVE
                or source.quality_status is not QualityStatus.VALID
                or source.effective_date != DATA_CUTOFF_DATE
            ):
                raise HoldingsIntegrationError("issuer External SourceRecord is invalid")
            artifact = (snapshot.artifact_root / source.raw_artifact_path).resolve()
            if (
                snapshot.artifact_root.resolve() not in artifact.parents
                or not artifact.is_file()
                or hashlib.sha256(artifact.read_bytes()).hexdigest()
                != source.raw_content_hash
            ):
                raise HoldingsIntegrationError("issuer raw artifact is invalid")
        for record in snapshot.issuer_records:
            if (
                record.snapshot_id != snapshot.external_snapshot_id
                or record.source_record_id not in source_ids
                or record.effective_date != DATA_CUTOFF_DATE
                or record.source_provider != KRX_KIND_PROVIDER
                or record.relation_validation_status
                is not IssuerIdentityStatus.RESOLVED
            ):
                raise HoldingsIntegrationError("source-level issuer record is invalid")

    def _register_snapshot(self, snapshot: TrustedIssuerSnapshot) -> None:
        self._insert(source_datasets, {
            "dataset_id": KRX_ISSUER_DATASET_ID,
            "dataset_code": KRX_ISSUER_DATASET_CODE,
            "display_name": "KRX KIND security issuer mapping",
            "source_system": KRX_KIND_PROVIDER,
            "schema_contract_version": KRX_KIND_ISSUER_SNAPSHOT_SCHEMA,
            "is_authoritative": True,
        })
        data_hash = _records_checksum(snapshot.issuer_records)
        self._insert(dataset_snapshots, {
            "snapshot_id": snapshot.canonical_snapshot_id,
            "dataset_id": KRX_ISSUER_DATASET_ID,
            "snapshot_date": DATA_CUTOFF_DATE,
            "generation": "260824",
            "ontology_version": ONTOLOGY_VERSION,
            "semantic_mapping_version": "c2.6-security-issuer-v1",
            "transformer_version": KRX_ISSUER_TRANSFORMER_VERSION,
            "database_schema_version": CANONICAL_V2_SCHEMA_VERSION,
            "data_sha256": data_hash,
            "schema_sha256": hashlib.sha256(
                KRX_KIND_ISSUER_SNAPSHOT_SCHEMA.encode()
            ).hexdigest(),
            "source_row_count": len(snapshot.issuer_records),
            "accepted_row_count": len(snapshot.issuer_records),
            "quarantined_row_count": 0,
            "status": "READY",
            "reconciliation_status": "PASSED",
            "row_count_reconciled": True,
            "metadata_json": {
                "authority": KRX_KIND_PROVIDER,
                "cutoff": DATA_CUTOFF_DATE.isoformat(),
                "scope": snapshot.coverage_scope,
                "source_schema": KRX_KIND_ISSUER_SNAPSHOT_SCHEMA,
            },
        })
        stored = self._connection.execute(select(
            dataset_snapshots.c.data_sha256,
            dataset_snapshots.c.source_row_count,
            dataset_snapshots.c.status,
        ).where(
            dataset_snapshots.c.snapshot_id == snapshot.canonical_snapshot_id
        )).one()
        if stored != (data_hash, len(snapshot.issuer_records), "READY"):
            raise HoldingsIntegrationError("existing issuer snapshot is incompatible")
        self._insert(external_snapshot_manifests, {
            "external_snapshot_id": snapshot.external_snapshot_id,
            "canonical_snapshot_id": snapshot.canonical_snapshot_id,
            "schema_version": snapshot.manifest.schema_version,
            "status": snapshot.manifest.status.value,
            "data_cutoff_date": DATA_CUTOFF_DATE,
            "manifest_sha256": snapshot.manifest_sha256,
            "manifest_json": snapshot.manifest.model_dump(mode="json"),
        })

    def _register_external_sources(self, snapshot: TrustedIssuerSnapshot) -> None:
        artifacts = {
            item.sha256: item for item in snapshot.manifest.raw_artifacts
        }
        for source in snapshot.source_records:
            artifact = artifacts[source.raw_content_hash]
            artifact_id = _stable_id(
                "issuer-artifact", snapshot.external_snapshot_id,
                source.raw_content_hash, source.source_url,
            )
            self._insert(external_raw_artifacts, {
                "artifact_id": artifact_id,
                "external_snapshot_id": snapshot.external_snapshot_id,
                "sha256": source.raw_content_hash,
                "relative_path": source.raw_artifact_path,
                "source_url": source.source_url,
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

    @staticmethod
    def _source_conflicts(
        records: Sequence[ExternalSecurityIssuerRecord],
    ) -> tuple[set[str], set[str]]:
        ticker_values: dict[str, set[str]] = {}
        issuer_values: dict[str, set[str]] = {}
        for row in records:
            ticker_values.setdefault(row.security_ticker, set()).add(
                row.issuer_source_id
            )
            issuer_values.setdefault(row.issuer_source_id, set()).add(
                _legal_name_key(row.issuer_name_raw)
            )
        return (
            {key for key, values in ticker_values.items() if len(values) > 1},
            {key for key, values in issuer_values.items() if len(values) > 1},
        )

    def _resolve_security(
        self, row: ExternalSecurityIssuerRecord,
    ) -> tuple[str, str | None]:
        values = self._connection.execute(
            select(entity_identifiers.c.entity_id)
            .join(
                canonical_entities,
                canonical_entities.c.entity_id == entity_identifiers.c.entity_id,
            )
            .where(
                entity_identifiers.c.scheme_code == "TICKER",
                entity_identifiers.c.namespace == "KRX",
                entity_identifiers.c.normalized_value == row.security_ticker,
                entity_identifiers.c.validation_status == "VALIDATED",
                entity_identifiers.c.resolution_status == "RESOLVED",
                entity_identifiers.c.conflict_status == "NONE",
                canonical_entities.c.entity_kind == "SECURITY",
            )
        ).scalars().all()
        unique = sorted(set(str(value) for value in values))
        if len(unique) == 1:
            return "RESOLVED", unique[0]
        if len(unique) > 1:
            return "AMBIGUOUS", None
        return "UNRESOLVED", None

    def _resolve_organization(
        self, row: ExternalSecurityIssuerRecord, source_record_id: str,
    ) -> tuple[str, str | None, bool]:
        by_identifier = self._connection.execute(
            select(entity_identifiers.c.entity_id)
            .join(
                canonical_entities,
                canonical_entities.c.entity_id == entity_identifiers.c.entity_id,
            )
            .where(
                entity_identifiers.c.scheme_code == "PROVIDER_ORGANIZATION_ID",
                entity_identifiers.c.namespace == "KRX_KIND",
                entity_identifiers.c.normalized_value == row.issuer_source_id,
                entity_identifiers.c.validation_status == "VALIDATED",
                entity_identifiers.c.resolution_status == "RESOLVED",
                entity_identifiers.c.conflict_status == "NONE",
                canonical_entities.c.entity_kind == "ORGANIZATION",
            )
        ).scalars().all()
        identifier_matches = sorted(set(str(item) for item in by_identifier))
        if len(identifier_matches) > 1:
            return "CONFLICT", None, False
        if identifier_matches:
            organization_id = identifier_matches[0]
            self._add_official_alias(
                organization_id, row.issuer_name_raw, source_record_id
            )
            return "EXISTING", organization_id, False

        name_key = _legal_name_key(row.issuer_name_raw)
        name_candidates = self._organization_name_candidates(name_key)
        if len(name_candidates) > 1:
            return "AMBIGUOUS", None, False
        if name_candidates:
            organization_id = name_candidates[0]
            self._add_organization_identifier(
                organization_id, row.issuer_source_id, source_record_id
            )
            self._add_official_alias(
                organization_id, row.issuer_name_raw, source_record_id
            )
            return "EXISTING", organization_id, False

        organization_id = f"organization:krx-kind:{row.issuer_source_id.casefold()}"
        self._insert(canonical_entities, {
            "entity_id": organization_id,
            "entity_kind": "ORGANIZATION",
            "preferred_name": row.issuer_name_raw,
            "normalized_preferred_name": normalize_lookup_value(row.issuer_name_raw),
            "name_status": "AUTHORITATIVE",
            "identity_status": "VALIDATED",
            "query_eligible": True,
        })
        self._insert(organizations, {
            "organization_id": organization_id,
            "entity_kind": "ORGANIZATION",
            "organization_type": "ISSUER",
        })
        self._add_organization_identifier(
            organization_id, row.issuer_source_id, source_record_id
        )
        self._add_official_alias(
            organization_id, row.issuer_name_raw, source_record_id
        )
        return "EXISTING", organization_id, True

    def _organization_name_candidates(self, name_key: str) -> list[str]:
        rows = self._connection.execute(
            select(
                canonical_entities.c.entity_id,
                canonical_entities.c.preferred_name,
                entity_aliases.c.alias,
            )
            .select_from(canonical_entities.outerjoin(
                entity_aliases,
                entity_aliases.c.entity_id == canonical_entities.c.entity_id,
            ))
            .where(
                canonical_entities.c.entity_kind == "ORGANIZATION",
                canonical_entities.c.identity_status == "VALIDATED",
            )
        ).mappings()
        candidates = {
            str(row["entity_id"])
            for row in rows
            if any(
                value is not None and _legal_name_key(str(value)) == name_key
                for value in (row["preferred_name"], row["alias"])
            )
        }
        return sorted(candidates)

    def _record_source_row(
        self,
        snapshot: TrustedIssuerSnapshot,
        row: ExternalSecurityIssuerRecord,
        position: int,
        security_status: str,
        issuer_status: str,
    ) -> tuple[str, str]:
        source_record_id = "normalized:" + row.issuer_record_id
        payload = row.model_dump(mode="json")
        payload_json = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        payload_sha256 = hashlib.sha256(payload_json.encode()).hexdigest()
        self._insert(source_records, {
            "source_record_id": source_record_id,
            "snapshot_id": snapshot.canonical_snapshot_id,
            "source_primary_key": row.issuer_record_id,
            "source_row_number": position,
            "raw_payload": payload,
            "normalized_payload": payload,
            "payload_sha256": payload_sha256,
            "quality_status": "VALID",
        })
        relation_status = (
            "RESOLVED"
            if security_status == "RESOLVED" and issuer_status == "RESOLVED"
            else "CONFLICT"
            if "CONFLICT" in {security_status, issuer_status}
            else "AMBIGUOUS"
            if "AMBIGUOUS" in {security_status, issuer_status}
            else "UNRESOLVED"
        )
        self._insert(external_security_issuer_records, {
            "issuer_record_id": row.issuer_record_id,
            "external_source_record_id": row.source_record_id,
            "canonical_source_record_id": source_record_id,
            "security_ticker": row.security_ticker,
            "security_source_id": row.security_source_id,
            "issuer_source_id": row.issuer_source_id,
            "effective_date": row.effective_date,
            "security_identity_status": security_status,
            "issuer_identity_status": issuer_status,
            "relation_validation_status": relation_status,
            "normalized_payload": payload,
            "payload_sha256": payload_sha256,
        })
        assertion_id = _stable_id(
            "issuer-assertion", row.issuer_record_id, "SECURITY_ISSUED_BY"
        )
        self._insert(source_field_assertions, {
            "assertion_id": assertion_id,
            "source_record_id": source_record_id,
            "source_column": "isurCd+representative_ticker",
            "raw_value": f"{row.security_ticker}|{row.issuer_source_id}|{row.issuer_name_raw}",
            "normalized_value": f"{row.security_ticker}|{row.issuer_source_id}",
            "mapping_category": "ENTITY_RELATION",
            "target_semantic_key": "securityIssuedBy",
            "quality_status": "VALID",
            "transformation_rule": KRX_ISSUER_TRANSFORMER_VERSION,
        })
        return source_record_id, assertion_id

    def _upsert_fact(
        self,
        snapshot: TrustedIssuerSnapshot,
        row: ExternalSecurityIssuerRecord,
        security_id: str,
        organization_id: str,
        assertion_id: str,
    ) -> bool:
        semantic_key = f"securityIssuedBy:{organization_id}"
        fact_id = _stable_id(
            "fact", security_id, snapshot.canonical_snapshot_id, semantic_key
        )
        inserted = self._insert(canonical_facts, {
            "fact_id": fact_id,
            "subject_entity_id": security_id,
            "snapshot_id": snapshot.canonical_snapshot_id,
            "fact_kind": "ENTITY_RELATION",
            "semantic_key": semantic_key,
            "resolution_status": "RESOLVED",
            "valid_from": row.effective_date,
        })
        self._insert(entity_relations, {
            "fact_id": fact_id,
            "subject_entity_id": security_id,
            "relation_type": "SECURITY_ISSUED_BY",
            "object_entity_id": organization_id,
        })
        self._insert(fact_evidence_links, {
            "fact_id": fact_id,
            "assertion_id": assertion_id,
            "evidence_role": "SUPPORTS",
        })
        return inserted

    def _add_organization_identifier(
        self, organization_id: str, value: str, source_record_id: str,
    ) -> None:
        self._insert(entity_identifiers, {
            "entity_id": organization_id,
            "scheme_code": "PROVIDER_ORGANIZATION_ID",
            "namespace": "KRX_KIND",
            "raw_value": value,
            "normalized_value": value.upper(),
            "validation_status": "VALIDATED",
            "resolution_status": "RESOLVED",
            "conflict_status": "NONE",
            "is_primary": True,
            "source_record_id": source_record_id,
        })

    def _add_official_alias(
        self, organization_id: str, value: str, source_record_id: str,
    ) -> None:
        self._insert(entity_aliases, {
            "entity_id": organization_id,
            "alias": value,
            "normalized_alias": normalize_lookup_value(value),
            "alias_type": "OFFICIAL_NAME",
            "language": "ko",
            "source_record_id": source_record_id,
            "is_preferred": True,
        })

    def _set_security_status(self, security_id: str, status: str) -> None:
        self._connection.execute(
            securities.update().where(
                securities.c.security_id == security_id
            ).values(issuer_resolution_status=status)
        )

    def _update_resolution_status(
        self, issuer_record_id: str, security_status: str, issuer_status: str,
    ) -> None:
        normalized_issuer = "RESOLVED" if issuer_status == "EXISTING" else issuer_status
        relation_status = (
            "RESOLVED"
            if security_status == "RESOLVED" and normalized_issuer == "RESOLVED"
            else "CONFLICT"
            if "CONFLICT" in {security_status, normalized_issuer}
            else "AMBIGUOUS"
            if "AMBIGUOUS" in {security_status, normalized_issuer}
            else "UNRESOLVED"
        )
        self._connection.execute(
            external_security_issuer_records.update()
            .where(
                external_security_issuer_records.c.issuer_record_id
                == issuer_record_id
            )
            .values(
                security_identity_status=security_status,
                issuer_identity_status=normalized_issuer,
                relation_validation_status=relation_status,
            )
        )

    def _ensure_scheme(self, code: str, label: str, global_id: bool) -> None:
        self._insert(identifier_schemes, {
            "scheme_code": code,
            "label": label,
            "default_namespace": None,
            "validation_pattern": None,
            "is_globally_unique": global_id,
        })

    def _insert(self, table, values: Mapping[str, Any]) -> bool:
        statement = (
            pg_insert(table).values(**values).on_conflict_do_nothing()
            .returning(*table.primary_key.columns)
        )
        return self._connection.execute(statement).first() is not None

    @staticmethod
    def _increment(
        report: IssuerIntegrationReport, prefix: str, status: str,
    ) -> None:
        normalized = "existing" if status == "EXISTING" else status.casefold()
        field = f"{prefix}_{normalized}"
        setattr(report, field, getattr(report, field) + 1)


def _legal_name_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    previous = None
    while normalized != previous:
        previous = normalized
        normalized = _LEGAL_FORM.sub("", normalized).strip()
    return "".join(normalized.split())


def _records_checksum(records: Sequence[ExternalSecurityIssuerRecord]) -> str:
    return hashlib.sha256(
        "\n".join(sorted(item.canonical_json() for item in records)).encode()
    ).hexdigest()


def _stable_id(kind: str, *parts: str) -> str:
    return f"{kind}:" + hashlib.sha256(
        "|".join((kind, *parts)).encode()
    ).hexdigest()

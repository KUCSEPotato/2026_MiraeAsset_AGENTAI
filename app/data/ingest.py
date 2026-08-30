import argparse
import datetime as dt
import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, delete, func, insert, select

from app.data.catalog import DatasetFiles, discover_dataset_files
from app.data.cleaning import clean_source_row, json_value
from app.data.database import (
    DatabaseSettings,
    create_database_engine,
)
from app.data.loader import SourceSchema, iter_source_rows, load_source_schema
from app.data.mapping import MappedProduct, map_product
from app.data.ontology_mapping_registry import OntologyColumnMappingRegistry
from app.data.ontology_transform import transform_evidence
from app.data.product_validation import NAME_FIELDS, validate_product_row
from app.data.profiling import (
    rebuild_aggregate_profiles,
    rebuild_dataset_profiles,
)
from app.data.schema import (
    bond_attributes,
    canonical_products,
    etf_attributes,
    field_quality_profiles,
    field_coverage_stats,
    fund_classes,
    funds,
    metadata,
    product_identifiers,
    quarantine_records,
    identifier_conflicts,
    ingestion_runs,
    metric_observations,
    ontology_product_identifiers,
    product_relations,
    raw_code_values,
    semantic_source_documents,
    source_datasets,
    source_field_assertions,
    source_records,
)


@dataclass(frozen=True)
class IngestionReport:
    dataset: str
    source_file: str
    snapshot: str
    source_rows: int
    loaded_rows: int
    quarantined_rows: int
    duplicate_rows: int
    products: int = 0
    assertions: int = 0
    identifiers: int = 0
    observations: int = 0
    relations: int = 0
    raw_codes: int = 0
    semantic_documents: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    dry_run: bool = False
    status: str = "COMPLETED"
    skip_reason: str | None = None


TRANSFORMER_VERSION = "ontology-ingest-v2.2-team-1.1"


class FinancialDataIngestor:
    def __init__(self, engine: Engine, *, batch_size: int = 1000,
                 mapping_file: Path = Path("ontology/mappings/column_mapping.csv"),
                 dry_run: bool = False, prepare_search_documents: bool = False,
                 selected_datasets: frozenset[str] | None = None,
                 row_limit: int | None = None) -> None:
        self._engine = engine
        self._batch_size = batch_size
        self._registry = OntologyColumnMappingRegistry.load(mapping_file)
        self._mapping_file = mapping_file
        self._mapping_sha256 = OntologyColumnMappingRegistry.digest(mapping_file)
        self._dry_run = dry_run
        self._prepare_documents = prepare_search_documents
        self._selected_datasets = selected_datasets
        self._row_limit = row_limit

    def ingest_all(self, material_root: Path) -> list[IngestionReport]:
        files = discover_dataset_files(material_root)
        if self._selected_datasets:
            files = [item for item in files if item.spec.source_dataset in self._selected_datasets
                     or item.spec.prefix in self._selected_datasets]
            if not files:
                raise ValueError(f"no datasets matched {sorted(self._selected_datasets)}")
        if not self._dry_run:
            metadata.create_all(self._engine)
        run_id = str(uuid.uuid4())
        started = dt.datetime.now(dt.timezone.utc).isoformat()
        if not self._dry_run:
            with self._engine.begin() as connection:
                connection.execute(insert(ingestion_runs), {
                    "run_id": run_id, "started_at": started, "status": "RUNNING",
                    "dry_run": False, "options": {"datasets": sorted(self._selected_datasets or ()), "row_limit": self._row_limit},
                })
        reports = [self.ingest_dataset(item, run_id=run_id) for item in files]
        changed = [report for report in reports if report.status != "SKIPPED_UNCHANGED"]
        snapshots = sorted({report.snapshot for report in changed})
        if not self._dry_run:
          with self._engine.begin() as connection:
            for snapshot in snapshots:
                rebuild_aggregate_profiles(connection, snapshot=snapshot)
            self._rebuild_identifier_conflicts(connection, snapshots)
            connection.execute(ingestion_runs.update().where(ingestion_runs.c.run_id == run_id).values(
                completed_at=dt.datetime.now(dt.timezone.utc).isoformat(), status="COMPLETED",
                report=[asdict(report) for report in reports]))
        return reports

    @staticmethod
    def _rebuild_identifier_conflicts(connection: Connection, snapshots: list[str]) -> None:
        for snapshot in snapshots:
            connection.execute(delete(identifier_conflicts).where(identifier_conflicts.c.dataset_snapshot == snapshot))
            groups = connection.execute(
                select(ontology_product_identifiers.c.identifier_type,
                       ontology_product_identifiers.c.namespace,
                       ontology_product_identifiers.c.normalized_value)
                .where(ontology_product_identifiers.c.dataset_snapshot == snapshot,
                       ontology_product_identifiers.c.validation_status != "INVALID_FORMAT")
                .group_by(ontology_product_identifiers.c.identifier_type,
                          ontology_product_identifiers.c.namespace,
                          ontology_product_identifiers.c.normalized_value)
                .having(func.count(func.distinct(ontology_product_identifiers.c.canonical_product_id)) > 1)
            )
            for identifier_type, namespace, normalized in groups:
                product_ids = list(connection.scalars(select(ontology_product_identifiers.c.canonical_product_id).where(
                    ontology_product_identifiers.c.dataset_snapshot == snapshot,
                    ontology_product_identifiers.c.identifier_type == identifier_type,
                    ontology_product_identifiers.c.namespace == namespace,
                    ontology_product_identifiers.c.normalized_value == normalized).distinct()))
                connection.execute(insert(identifier_conflicts), {
                    "conflict_id": hashlib.sha256(f"{snapshot}|{identifier_type}|{namespace}|{normalized}".encode()).hexdigest(),
                    "dataset_snapshot": snapshot, "identifier_type": identifier_type,
                    "namespace": namespace, "normalized_value": normalized,
                    "canonical_product_ids": product_ids, "status": "REVIEW_REQUIRED",
                })

    def ingest_dataset(self, files: DatasetFiles, *, run_id: str | None = None) -> IngestionReport:
        schema = load_source_schema(files.schema_file)
        column_mappings = self._registry.for_dataset(files, schema)
        _validate_source_key_columns(files, schema)
        fingerprint = _dataset_fingerprint(files, self._mapping_sha256, self._row_limit)
        if not self._dry_run:
            with self._engine.connect() as connection:
                existing = connection.execute(select(
                    source_datasets.c.dataset_fingerprint,
                    source_datasets.c.source_row_count,
                ).where(
                    source_datasets.c.dataset_id == files.spec.source_dataset,
                    source_datasets.c.snapshot_date == files.snapshot_date,
                    source_datasets.c.ingestion_status == "SUCCESS",
                )).first()
            if existing is not None and existing.dataset_fingerprint == fingerprint:
                return IngestionReport(
                    dataset=files.spec.source_dataset, source_file=str(files.data_file),
                    snapshot=files.snapshot_date, source_rows=existing.source_row_count,
                    loaded_rows=0, quarantined_rows=0, duplicate_rows=0,
                    skipped=existing.source_row_count, status="SKIPPED_UNCHANGED",
                    skip_reason="DATA_SCHEMA_MAPPING_TRANSFORMER_FINGERPRINT_MATCH",
                )
        counters = {
            "source_rows": 0,
            "loaded_rows": 0,
            "quarantined_rows": 0,
            "duplicate_rows": 0,
            "products": 0, "assertions": 0, "identifiers": 0,
            "observations": 0, "relations": 0, "raw_codes": 0,
            "semantic_documents": 0,
        }
        coverage = {item.source_column: {
            "total_records": 0, "present_records": 0, "missing_records": 0,
            "invalid_records": 0, "sentinel_records": 0,
        } for item in column_mappings}
        with self._engine.begin() as connection:
            if not self._dry_run:
                self._delete_existing(connection, files)
            batches: dict[Any, list[dict[str, Any]]] = {}
            seen_source_keys: set[str] = set()
            seen_funds: set[str] = set()
            seen_products: set[str] = set()
            seen_identifiers: set[tuple[str, str, str]] = set()
            for row_number, raw in iter_source_rows(files.data_file, schema):
                if self._row_limit is not None and counters["source_rows"] >= self._row_limit:
                    break
                counters["source_rows"] += 1
                cleaned, raw_values = clean_source_row(
                    raw,
                    literal_null_fields=files.spec.literal_null_fields,
                )
                _update_coverage(coverage, cleaned)
                reason = _validate_types(cleaned, schema)
                source_key = files.spec.source_record_key(cleaned)
                if source_key is not None and source_key in seen_source_keys:
                    reason = "DUPLICATE_SOURCE_KEY"
                    counters["duplicate_rows"] += 1
                mapped = None
                validation_failure = validate_product_row(files.spec, cleaned)
                if reason is None and validation_failure is not None:
                    reason = validation_failure.code
                    for field in validation_failure.invalid_fields:
                        coverage[field]["invalid_records"] += 1
                if reason is None:
                    mapped, reason = map_product(
                        files.spec,
                        cleaned,
                        source_file=str(files.data_file),
                        source_row_number=row_number,
                        snapshot=files.snapshot_date,
                    )
                if reason is not None or mapped is None:
                    _add_batch(
                        batches,
                        quarantine_records,
                        {
                            "source_dataset": files.spec.source_dataset,
                            "source_file": str(files.data_file),
                            "source_row_number": row_number,
                            "dataset_snapshot": files.snapshot_date,
                            "reason_code": reason or "INVALID_ROW",
                            "ingestion_run_id": run_id,
                            "source_record_key": (
                                validation_failure.source_key if validation_failure else source_key
                            ),
                            "raw_product_name": (
                                validation_failure.raw_product_name if validation_failure
                                else cleaned.get(NAME_FIELDS[files.spec.source_dataset])
                            ),
                            "failure_reason": (
                                validation_failure.reason if validation_failure else reason or "invalid row"
                            ),
                            "raw_payload": {
                                key: json_value(value) for key, value in raw.items()
                            },
                        },
                    )
                    counters["quarantined_rows"] += 1
                else:
                    seen_source_keys.add(source_key or "")
                    self._stage_valid_row(
                        batches,
                        files,
                        row_number,
                        cleaned,
                        raw_values,
                        mapped,
                        seen_funds,
                        seen_products,
                    )
                    evidence = transform_evidence(
                        files, row_number, raw, cleaned, mapped, column_mappings,
                        prepare_documents=self._prepare_documents,
                    )
                    _add_batch(batches, source_records, evidence.source_record)
                    for table, rows in (
                        (source_field_assertions, evidence.assertions),
                        (ontology_product_identifiers, evidence.identifiers),
                        (metric_observations, evidence.observations),
                        (product_relations, evidence.relations),
                        (raw_code_values, evidence.raw_codes),
                        (semantic_source_documents, evidence.documents),
                    ):
                        for item in rows:
                            if table is ontology_product_identifiers:
                                identity = (item["canonical_product_id"], item["identifier_type"], item["normalized_value"])
                                if identity in seen_identifiers:
                                    continue
                                seen_identifiers.add(identity)
                            _add_batch(batches, table, item)
                            if table is ontology_product_identifiers:
                                counters["identifiers"] += 1
                    counters["assertions"] += len(evidence.assertions)
                    counters["observations"] += len(evidence.observations)
                    counters["relations"] += len(evidence.relations)
                    counters["raw_codes"] += len(evidence.raw_codes)
                    counters["semantic_documents"] += len(evidence.documents)
                    counters["loaded_rows"] += 1
                if sum(len(rows) for rows in batches.values()) >= self._batch_size:
                    _flush_batches(connection, batches) if not self._dry_run else batches.clear()
            if not self._dry_run: _flush_batches(connection, batches)
            counters["products"] = len(seen_products)
            counters["created"] = len(seen_products)
            if counters["source_rows"] != (
                counters["loaded_rows"] + counters["quarantined_rows"]
            ):
                raise RuntimeError("ingestion row-count integrity failure")
            if not self._dry_run: rebuild_dataset_profiles(
                connection,
                source_dataset=files.spec.source_dataset,
                snapshot=files.snapshot_date,
            )
            if not self._dry_run:
                connection.execute(insert(field_coverage_stats), [
                    {
                        "source_dataset": files.spec.source_dataset,
                        "dataset_snapshot": files.snapshot_date,
                        "source_column": column,
                        **values,
                        "coverage_fraction": (
                            values["present_records"] / values["total_records"]
                            if values["total_records"] else 0.0
                        ),
                    }
                    for column, values in coverage.items()
                ])
                connection.execute(insert(source_datasets), {
                    "dataset_id": files.spec.source_dataset,
                    "dataset_code": files.spec.prefix,
                    "source_table": files.spec.source_table.name,
                    "source_file": str(files.data_file), "schema_file": str(files.schema_file),
                    "snapshot_date": files.snapshot_date,
                    "schema_column_count": len(schema.columns),
                    "source_row_count": counters["source_rows"],
                    "data_sha256": _sha256_file(files.data_file),
                    "schema_sha256": _sha256_file(files.schema_file),
                    "mapping_sha256": self._mapping_sha256,
                    "transformer_version": TRANSFORMER_VERSION,
                    "dataset_fingerprint": fingerprint,
                    "ingestion_status": "SUCCESS",
                })
        return IngestionReport(
            dataset=files.spec.source_dataset,
            source_file=str(files.data_file),
            snapshot=files.snapshot_date,
            dry_run=self._dry_run,
            **counters,
        )

    @staticmethod
    def _stage_valid_row(
        batches: dict[Any, list[dict[str, Any]]],
        files: DatasetFiles,
        row_number: int,
        cleaned: dict[str, Any],
        raw_values: dict[str, Any],
        mapped: MappedProduct,
        seen_funds: set[str],
        seen_products: set[str],
    ) -> None:
        canonical_id = mapped.canonical["canonical_product_id"]
        source_key = mapped.canonical["source_record_key"]
        is_new_product = canonical_id not in seen_products
        _add_batch(
            batches,
            files.spec.source_table,
            {
                "source_dataset": files.spec.source_dataset,
                "source_file": str(files.data_file),
                "source_row_number": row_number,
                "source_record_key": source_key,
                "canonical_product_id": canonical_id,
                "dataset_snapshot": files.snapshot_date,
                "payload": cleaned,
                "raw_values": raw_values,
                "quality_annotations": mapped.quality_annotations,
            },
        )
        if is_new_product:
            seen_products.add(canonical_id)
            _add_batch(batches, canonical_products, mapped.canonical)
            if mapped.bond_attributes is not None:
                _add_batch(batches, bond_attributes, mapped.bond_attributes)
            if mapped.etf_attributes is not None:
                _add_batch(batches, etf_attributes, mapped.etf_attributes)
        if mapped.fund is not None and mapped.fund["fund_id"] not in seen_funds:
            seen_funds.add(mapped.fund["fund_id"])
            _add_batch(batches, funds, mapped.fund)
        if mapped.fund_class is not None:
            _add_batch(batches, fund_classes, mapped.fund_class)
        if is_new_product:
            for identifier in mapped.identifiers:
                if not identifier["identifier_value"]:
                    continue
                _add_batch(
                    batches,
                    product_identifiers,
                    {
                        **identifier,
                        "canonical_product_id": canonical_id,
                        "dataset_snapshot": files.snapshot_date,
                    },
                )

    @staticmethod
    def _delete_existing(
        connection: Connection,
        files: DatasetFiles,
    ) -> None:
        connection.execute(delete(identifier_conflicts).where(identifier_conflicts.c.dataset_snapshot == files.snapshot_date))
        record_ids = select(source_records.c.source_record_id).where(
            source_records.c.dataset_id == files.spec.source_dataset,
            source_records.c.dataset_snapshot == files.snapshot_date,
        )
        for table in (metric_observations, product_relations, source_field_assertions,
                      raw_code_values, ontology_product_identifiers, semantic_source_documents):
            connection.execute(delete(table).where(table.c.source_record_id.in_(record_ids)))
        connection.execute(delete(source_records).where(
            source_records.c.dataset_id == files.spec.source_dataset,
            source_records.c.dataset_snapshot == files.snapshot_date))
        connection.execute(delete(source_datasets).where(
            source_datasets.c.dataset_id == files.spec.source_dataset,
            source_datasets.c.snapshot_date == files.snapshot_date))
        connection.execute(delete(field_coverage_stats).where(
            field_coverage_stats.c.source_dataset == files.spec.source_dataset,
            field_coverage_stats.c.dataset_snapshot == files.snapshot_date))
        product_ids = select(canonical_products.c.canonical_product_id).where(
            canonical_products.c.source_dataset == files.spec.source_dataset,
            canonical_products.c.dataset_snapshot == files.snapshot_date,
        )
        for table in (bond_attributes, etf_attributes, fund_classes):
            connection.execute(
                delete(table).where(
                    table.c.dataset_snapshot == files.snapshot_date,
                    table.c.canonical_product_id.in_(product_ids),
                )
            )
        connection.execute(
            delete(product_identifiers).where(
                product_identifiers.c.source_dataset
                == files.spec.source_dataset,
                product_identifiers.c.dataset_snapshot == files.snapshot_date,
            )
        )
        if files.spec.source_dataset == "public_fund":
            connection.execute(
                delete(funds).where(
                    funds.c.dataset_snapshot == files.snapshot_date
                )
            )
        connection.execute(
            delete(canonical_products).where(
                canonical_products.c.source_dataset == files.spec.source_dataset,
                canonical_products.c.dataset_snapshot == files.snapshot_date,
            )
        )
        connection.execute(
            delete(files.spec.source_table).where(
                files.spec.source_table.c.dataset_snapshot == files.snapshot_date
            )
        )
        connection.execute(
            delete(quarantine_records).where(
                quarantine_records.c.source_dataset
                == files.spec.source_dataset,
                quarantine_records.c.dataset_snapshot == files.snapshot_date,
            )
        )
        connection.execute(
            delete(field_quality_profiles).where(
                field_quality_profiles.c.source_dataset
                == files.spec.source_dataset,
                field_quality_profiles.c.dataset_snapshot == files.snapshot_date,
            )
        )


def _validate_source_key_columns(
    files: DatasetFiles,
    schema: SourceSchema,
) -> None:
    missing = set(files.spec.source_key_fields) - set(schema.columns)
    if missing:
        raise ValueError(f"source key columns missing: {sorted(missing)}")


def _validate_types(
    row: dict[str, Any],
    schema: SourceSchema,
) -> str | None:
    for field, field_type in schema.column_types.items():
        value = row.get(field)
        is_numeric = (
            field_type.startswith("numeric")
            or field_type in {"double precision", "bigint"}
        )
        if value is None or not is_numeric:
            continue
        try:
            Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return f"INVALID_NUMERIC:{field}"
    return None


def _update_coverage(
    coverage: dict[str, dict[str, int]], row: dict[str, Any]
) -> None:
    for column, counters in coverage.items():
        counters["total_records"] += 1
        value = row.get(column)
        if value is None or (isinstance(value, str) and not value.strip()):
            counters["missing_records"] += 1
            continue
        counters["present_records"] += 1
        text = str(value).strip()
        if len(text) >= 3 and set(text.replace(".", "")) <= {"0"}:
            counters["sentinel_records"] += 1


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dataset_fingerprint(
    files: DatasetFiles, mapping_sha256: str, row_limit: int | None
) -> str:
    components = (
        files.spec.prefix,
        files.snapshot_date,
        _sha256_file(files.data_file),
        _sha256_file(files.schema_file),
        mapping_sha256,
        TRANSFORMER_VERSION,
        f"row-limit={row_limit if row_limit is not None else 'full'}",
    )
    return hashlib.sha256("|".join(components).encode()).hexdigest()


def _add_batch(
    batches: dict[Any, list[dict[str, Any]]],
    table: Any,
    row: dict[str, Any],
) -> None:
    batches.setdefault(table, []).append(row)


def _flush_batches(
    connection: Connection,
    batches: dict[Any, list[dict[str, Any]]],
) -> None:
    order = (
        funds,
        canonical_products,
        bond_attributes,
        etf_attributes,
        fund_classes,
        product_identifiers,
        quarantine_records,
        source_records,
        source_field_assertions,
        ontology_product_identifiers,
        metric_observations,
        product_relations,
        raw_code_values,
        semantic_source_documents,
    )
    source_tables = [
        table
        for table in batches
        if table.name.startswith("source_")
    ]
    for table in (*source_tables, *order):
        rows = batches.pop(table, [])
        if rows:
            connection.execute(insert(table), rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest financial Excel data")
    parser.add_argument("--material-root", type=Path, default=Path("material"))
    parser.add_argument("--database-url")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--prepare-search-documents", action="store_true")
    parser.add_argument("--datasets", help="comma-separated dataset names or PR* codes")
    parser.add_argument("--limit", type=int, help="maximum rows per selected dataset")
    parser.add_argument("--shacl", action="store_true", help="validate ontology and representative instances before ingestion")
    parser.add_argument("--graph-projection", action="store_true", help="prepare provenance-bearing graph relations in RDB")
    parser.add_argument("--report-file", type=Path)
    args = parser.parse_args()
    if args.database_url:
        settings = DatabaseSettings(
            database_url=args.database_url,
            snapshot_date=os.getenv("DATA_SNAPSHOT_DATE", "2026-08-24"),
        )
    else:
        settings = DatabaseSettings.from_env()
    engine = create_database_engine(settings)
    try:
        if args.shacl:
            from scripts.validate_ontology import main as validate_ontology
            validate_ontology()
        reports = FinancialDataIngestor(
            engine, batch_size=args.batch_size, dry_run=args.dry_run,
            prepare_search_documents=args.prepare_search_documents,
            selected_datasets=frozenset(args.datasets.split(",")) if args.datasets else None,
            row_limit=args.limit,
        ).ingest_all(args.material_root)
        rendered = json.dumps(
                [asdict(report) for report in reports],
                ensure_ascii=False,
                indent=2,
            )
        print(rendered)
        if args.report_file:
            args.report_file.write_text(rendered + "\n", encoding="utf-8")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

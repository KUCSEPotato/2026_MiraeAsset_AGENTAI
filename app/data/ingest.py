import argparse
import json
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, delete, insert, select

from app.data.catalog import DatasetFiles, discover_dataset_files
from app.data.cleaning import clean_source_row, json_value
from app.data.database import (
    DatabaseSettings,
    create_database_engine,
)
from app.data.loader import SourceSchema, iter_source_rows, load_source_schema
from app.data.mapping import MappedProduct, map_product
from app.data.profiling import (
    rebuild_aggregate_profiles,
    rebuild_dataset_profiles,
)
from app.data.schema import (
    bond_attributes,
    canonical_products,
    etf_attributes,
    field_quality_profiles,
    fund_classes,
    funds,
    metadata,
    product_identifiers,
    quarantine_records,
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


class FinancialDataIngestor:
    def __init__(self, engine: Engine, *, batch_size: int = 1000) -> None:
        self._engine = engine
        self._batch_size = batch_size

    def ingest_all(self, material_root: Path) -> list[IngestionReport]:
        metadata.create_all(self._engine)
        files = discover_dataset_files(material_root)
        reports = [self.ingest_dataset(item) for item in files]
        snapshots = sorted({item.snapshot_date for item in files})
        with self._engine.begin() as connection:
            for snapshot in snapshots:
                rebuild_aggregate_profiles(connection, snapshot=snapshot)
        return reports

    def ingest_dataset(self, files: DatasetFiles) -> IngestionReport:
        schema = load_source_schema(files.schema_file)
        _validate_source_key_columns(files, schema)
        counters = {
            "source_rows": 0,
            "loaded_rows": 0,
            "quarantined_rows": 0,
            "duplicate_rows": 0,
        }
        with self._engine.begin() as connection:
            self._delete_existing(connection, files)
            batches: dict[Any, list[dict[str, Any]]] = {}
            seen_source_keys: set[str] = set()
            seen_funds: set[str] = set()
            for row_number, raw in iter_source_rows(files.data_file, schema):
                counters["source_rows"] += 1
                cleaned, raw_values = clean_source_row(
                    raw,
                    literal_null_fields=files.spec.literal_null_fields,
                )
                reason = _validate_types(cleaned, schema)
                source_key = files.spec.source_record_key(cleaned)
                if source_key is not None and source_key in seen_source_keys:
                    reason = "DUPLICATE_SOURCE_KEY"
                    counters["duplicate_rows"] += 1
                mapped = None
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
                    )
                    counters["loaded_rows"] += 1
                if sum(len(rows) for rows in batches.values()) >= self._batch_size:
                    _flush_batches(connection, batches)
            _flush_batches(connection, batches)
            if counters["source_rows"] != (
                counters["loaded_rows"] + counters["quarantined_rows"]
            ):
                raise RuntimeError("ingestion row-count integrity failure")
            rebuild_dataset_profiles(
                connection,
                source_dataset=files.spec.source_dataset,
                snapshot=files.snapshot_date,
            )
        return IngestionReport(
            dataset=files.spec.source_dataset,
            source_file=str(files.data_file),
            snapshot=files.snapshot_date,
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
    ) -> None:
        canonical_id = mapped.canonical["canonical_product_id"]
        source_key = mapped.canonical["source_record_key"]
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
    numeric_types = {"numeric", "double precision", "bigint"}
    for field, field_type in schema.column_types.items():
        value = row.get(field)
        if value is None or field_type not in numeric_types:
            continue
        try:
            Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return f"INVALID_NUMERIC:{field}"
    return None


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
    args = parser.parse_args()
    settings = DatabaseSettings.from_env()
    if args.database_url:
        settings = DatabaseSettings(
            database_url=args.database_url,
            snapshot_date=settings.snapshot_date,
            rdb_default_limit=settings.rdb_default_limit,
        )
    engine = create_database_engine(settings)
    try:
        reports = FinancialDataIngestor(engine).ingest_all(args.material_root)
        print(
            json.dumps(
                [asdict(report) for report in reports],
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

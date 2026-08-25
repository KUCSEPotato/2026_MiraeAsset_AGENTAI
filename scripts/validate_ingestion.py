from __future__ import annotations

import argparse
import sys
import tempfile
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, func, select

from app.data.catalog import DATASET_SPECS, discover_dataset_files
from app.data.ingest import FinancialDataIngestor
from app.data.loader import load_source_schema
from app.data.loader import iter_source_rows
from app.data.cleaning import clean_source_row
from app.data.evidence import EvidenceRepository
from app.data.ontology_mapping_registry import OntologyColumnMappingRegistry
from app.data.schema import (
    canonical_products, metric_observations, ontology_product_identifiers,
    product_relations, source_field_assertions, source_records,
    field_coverage_stats, quarantine_records,
)
from app.data.product_validation import validate_product_row


def validate(material_root: Path, *, sample_size: int = 10) -> None:
    files = discover_dataset_files(material_root)
    assert len(files) == 4
    assert all("prfd_attr_cd" not in spec.source_key_fields for spec in DATASET_SPECS)
    fund = next(spec for spec in DATASET_SPECS if spec.source_dataset == "public_fund")
    assert fund.source_key_fields == ("itm_no",)

    registry = OntologyColumnMappingRegistry.load(Path("ontology/mappings/column_mapping.csv"))
    assert len(registry.mappings) == 280
    for item in files:
        registry.for_dataset(item, load_source_schema(item.schema_file))

    anomalies: list[tuple[str, int, str]] = []
    for item in files:
        schema = load_source_schema(item.schema_file)
        for row_number, raw in iter_source_rows(item.data_file, schema):
            cleaned, _ = clean_source_row(raw, literal_null_fields=item.spec.literal_null_fields)
            failure = validate_product_row(item.spec, cleaned)
            if failure:
                anomalies.append((item.spec.source_dataset, row_number, failure.code))
    assert anomalies == [("domestic_etf", 224, "INVALID_PRODUCT_IDENTITY_AND_NAME")]

    with tempfile.TemporaryDirectory() as directory:
        url = f"sqlite+pysqlite:///{directory}/ingestion.db"
        engine = create_engine(url)
        ingestor = FinancialDataIngestor(engine, row_limit=sample_size, batch_size=500)
        first = ingestor.ingest_all(material_root)
        counts1 = _counts(engine)
        second = ingestor.ingest_all(material_root)
        counts2 = _counts(engine)
        assert counts1 == counts2
        assert sum(report.quarantined_rows for report in first + second) == 0
        assert all(report.status == "SKIPPED_UNCHANGED" for report in second)
        with engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(
                ontology_product_identifiers).where(
                    ontology_product_identifiers.c.source_dataset == "foreign_etf",
                    ontology_product_identifiers.c.source_column.in_(("cik", "pd_itm_no_ma")))) == 0
            assert connection.scalar(select(func.count()).select_from(metric_observations).where(
                metric_observations.c.observation_type.in_((
                    "PriceObservation", "YieldObservation", "AUMObservation")))) > 0
            assert connection.scalar(select(func.count()).select_from(product_relations).where(
                product_relations.c.source_record_id.is_(None))) == 0
            etn_ids = select(canonical_products.c.canonical_product_id).where(
                canonical_products.c.product_type.like("%ETN"))
            assert connection.scalar(select(func.count()).select_from(product_relations).where(
                product_relations.c.canonical_product_id.in_(etn_ids),
                product_relations.c.source_column == "cu_fund_mgmt_co",
                product_relations.c.relation_type == "managedBy")) == 0
            assert connection.scalar(select(func.count()).select_from(metric_observations).where(
                metric_observations.c.numeric_value == 0)) > 0
            assert connection.scalar(select(func.count()).select_from(source_field_assertions)) < sum(
                report.loaded_rows for report in first) * 280
            coverage = connection.execute(select(field_coverage_stats).where(
                field_coverage_stats.c.source_dataset == "domestic_bond",
                field_coverage_stats.c.source_column == "after_tax_yield")).one()
            assert coverage.total_records == sample_size
            assert coverage.present_records + coverage.missing_records == sample_size
            record = connection.execute(select(source_records).where(
                source_records.c.normalized_payload["after_tax_yield"].is_(None))).first()
            if record is not None:
                evidence = EvidenceRepository().source_field(
                    connection, source_record_id=record.source_record_id,
                    source_column="after_tax_yield", transformation_rule="test",
                )
                assert evidence.is_missing and evidence.quality_status == "MISSING"
            plan = connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN SELECT * FROM field_coverage_stats WHERE source_dataset=? AND source_column=?",
                ("domestic_bond", "after_tax_yield"),
            ).all()
            assert not any("SCAN field_coverage_stats" in str(row) for row in plan)

        # A mapping byte change invalidates only the fingerprint and forces reload.
        changed_mapping = Path(directory) / "column_mapping.csv"
        shutil.copyfile("ontology/mappings/column_mapping.csv", changed_mapping)
        changed_mapping.write_bytes(changed_mapping.read_bytes() + b"\n")
        changed = FinancialDataIngestor(
            engine, row_limit=sample_size, batch_size=500,
            mapping_file=changed_mapping,
        ).ingest_all(material_root)
        assert all(report.status == "COMPLETED" for report in changed)

        # The actual malformed domestic row is quarantined without derivatives.
        domestic = FinancialDataIngestor(
            engine, row_limit=230, batch_size=500,
            selected_datasets=frozenset({"PREF01N001"}),
        ).ingest_all(material_root)
        assert domestic[0].quarantined_rows == 1
        with engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(canonical_products).where(
                canonical_products.c.canonical_product_id == "etf_kr:KR")) == 0
            quarantined = connection.execute(select(quarantine_records).where(
                quarantine_records.c.source_row_number == 224)).one()
            assert quarantined.reason_code == "INVALID_PRODUCT_IDENTITY_AND_NAME"
            for table in (source_records, ontology_product_identifiers,
                          metric_observations, product_relations):
                assert connection.scalar(select(func.count()).select_from(table).where(
                    table.c.canonical_product_id == "etf_kr:KR")) == 0
        engine.dispose()
    print(f"ingestion_ok datasets=4 mappings=280 sample_rows={sample_size} idempotent=true")


def _counts(engine) -> tuple[int, ...]:
    with engine.connect() as connection:
        return tuple(connection.scalar(select(func.count()).select_from(table)) for table in (
            canonical_products, source_records, source_field_assertions,
            ontology_product_identifiers, metric_observations, product_relations,
        ))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--material-root", type=Path, default=Path("material"))
    parser.add_argument("--sample-size", type=int, default=10)
    args = parser.parse_args()
    validate(args.material_root, sample_size=args.sample_size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

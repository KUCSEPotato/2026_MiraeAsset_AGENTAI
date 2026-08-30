from __future__ import annotations

from datetime import date
from decimal import Decimal
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import Engine, Float, Numeric, create_engine, insert, inspect, text
from sqlalchemy.exc import IntegrityError

from app.data.v2_schema import (
    CANONICAL_V2_SCHEMA,
    CANONICAL_V2_SCHEMA_VERSION,
    canonical_entities,
    canonical_facts,
    dataset_snapshots,
    entity_identifiers,
    fact_evidence_links,
    financial_products,
    fund_share_classes,
    funds,
    get_schema_version,
    identifier_schemes,
    metadata,
    metric_observations,
    sale_lots,
    source_datasets,
    source_field_assertions,
    source_record_entities,
    source_records,
)


pytestmark = pytest.mark.postgresql
ROOT = Path(__file__).resolve().parents[1]
VERSION_TABLE = "alembic_version_m10_8"


def _url() -> str:
    url = os.getenv("POSTGRES_TEST_DATABASE_URL")
    if not url:
        pytest.skip("POSTGRES_TEST_DATABASE_URL is not configured")
    if not url.startswith(("postgresql://", "postgresql+psycopg://")):
        pytest.fail("POSTGRES_TEST_DATABASE_URL must be a PostgreSQL URL")
    return url


def _config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


@pytest.fixture(scope="module")
def v2_engine() -> Engine:
    url = _url()
    engine = create_engine(url, future=True)
    with engine.begin() as connection:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {CANONICAL_V2_SCHEMA} CASCADE"))
        connection.execute(text(f"DROP TABLE IF EXISTS {VERSION_TABLE}"))
        connection.execute(text("DROP TABLE IF EXISTS m10_8_a_v1_sentinel"))
        connection.execute(text("CREATE TABLE m10_8_a_v1_sentinel (id integer PRIMARY KEY)"))
    command.upgrade(_config(url), "head")
    yield engine
    command.downgrade(_config(url), "base")
    with engine.begin() as connection:
        assert inspect(connection).has_table("m10_8_a_v1_sentinel")
        connection.execute(text(f"DROP TABLE IF EXISTS {VERSION_TABLE}"))
        connection.execute(text("DROP TABLE IF EXISTS m10_8_a_v1_sentinel"))
    engine.dispose()


def _expect_integrity_error(engine: Engine, statement) -> None:
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(statement)


def _entity(entity_id: str, kind: str, *, name=None, name_status="UNKNOWN"):
    return insert(canonical_entities).values(
        entity_id=entity_id,
        entity_kind=kind,
        preferred_name=name,
        normalized_preferred_name=name.lower() if name else None,
        name_status=name_status,
    )


def _seed_snapshot(connection) -> None:
    connection.execute(
        insert(source_datasets).values(
            dataset_id="dataset:test",
            dataset_code="TEST",
            display_name="M10.8-A test dataset",
            schema_contract_version="test-v1",
        )
    )
    connection.execute(
        insert(dataset_snapshots).values(
            snapshot_id="snapshot:test",
            dataset_id="dataset:test",
            snapshot_date=date(2026, 8, 24),
            data_sha256="d" * 64,
            schema_sha256="s" * 64,
            source_row_count=1,
        )
    )


def test_sqlite_migration_url_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="require PostgreSQL"):
        command.current(_config("sqlite+pysqlite:///:memory:"))


def test_fresh_upgrade_reports_schema_version_and_preserves_v1(v2_engine: Engine) -> None:
    inspector = inspect(v2_engine)
    assert CANONICAL_V2_SCHEMA in inspector.get_schema_names()
    assert len(inspector.get_table_names(schema=CANONICAL_V2_SCHEMA)) == 36
    assert inspector.has_table("m10_8_a_v1_sentinel")
    with v2_engine.connect() as connection:
        assert get_schema_version(connection) == CANONICAL_V2_SCHEMA_VERSION


def test_downgrade_is_safe_and_upgrade_is_repeatable(v2_engine: Engine) -> None:
    url = _url()
    command.downgrade(_config(url), "base")
    inspector = inspect(v2_engine)
    assert CANONICAL_V2_SCHEMA not in inspector.get_schema_names()
    assert inspector.has_table("m10_8_a_v1_sentinel")
    command.upgrade(_config(url), "head")
    assert CANONICAL_V2_SCHEMA in inspect(v2_engine).get_schema_names()


def test_product_and_entity_grains_are_database_enforced(v2_engine: Engine) -> None:
    with v2_engine.begin() as connection:
        connection.execute(_entity("bad-product", "FINANCIAL_PRODUCT"))
        connection.execute(_entity("class:orphan", "FUND_SHARE_CLASS"))
        connection.execute(_entity("lot:orphan", "SALE_LOT"))

    _expect_integrity_error(
        v2_engine,
        insert(financial_products).values(
            product_id="bad-product", product_type_code="PublicFund"
        ),
    )
    for non_product_id in ("class:orphan", "lot:orphan"):
        _expect_integrity_error(
            v2_engine,
            insert(financial_products).values(
                product_id=non_product_id,
                product_type_code="FUND",
            ),
        )
    _expect_integrity_error(
        v2_engine,
        insert(fund_share_classes).values(
            fund_share_class_id="class:orphan",
            parent_fund_id="fund_family:missing",
            source_class_key="orphan",
        ),
    )
    _expect_integrity_error(
        v2_engine,
        insert(sale_lots).values(
            sale_lot_id="lot:orphan",
            bond_id="bond_kr:missing",
            trading_market_raw="OTC",
            information_date=date(2026, 8, 24),
        ),
    )
    _expect_integrity_error(v2_engine, _entity("invalid-kind", "PUBLIC_FUND"))


def test_fund_can_have_no_authoritative_family_name(v2_engine: Engine) -> None:
    with v2_engine.begin() as connection:
        connection.execute(
            _entity(
                "fund_family:030410046605",
                "FINANCIAL_PRODUCT",
                name_status="NO_AUTHORITATIVE_FAMILY_NAME",
            )
        )
        connection.execute(
            insert(financial_products).values(
                product_id="fund_family:030410046605",
                product_type_code="FUND",
            )
        )
        connection.execute(
            insert(funds).values(fund_id="fund_family:030410046605")
        )


def test_validated_identifier_collision_is_rejected_but_unresolved_coexists(
    v2_engine: Engine,
) -> None:
    with v2_engine.begin() as connection:
        connection.execute(_entity("org:a", "ORGANIZATION"))
        connection.execute(_entity("org:b", "ORGANIZATION"))
        connection.execute(
            insert(entity_identifiers).values(
                entity_id="org:a",
                scheme_code="SOURCE_ID",
                namespace="test",
                raw_value="same",
                normalized_value="same",
                validation_status="VALIDATED",
                resolution_status="RESOLVED",
                conflict_status="NONE",
            )
        )
    _expect_integrity_error(
        v2_engine,
        insert(entity_identifiers).values(
            entity_id="org:b",
            scheme_code="SOURCE_ID",
            namespace="test",
            raw_value="same",
            normalized_value="same",
            validation_status="VALIDATED",
            resolution_status="RESOLVED",
            conflict_status="NONE",
        ),
    )
    with v2_engine.begin() as connection:
        connection.execute(
            insert(entity_identifiers),
            [
                {
                    "entity_id": "org:a",
                    "scheme_code": "SOURCE_ID",
                    "namespace": "unresolved-test",
                    "raw_value": "unknown",
                    "normalized_value": "unknown",
                    "validation_status": "UNVALIDATED",
                    "resolution_status": "UNRESOLVED",
                    "conflict_status": "OPEN",
                },
                {
                    "entity_id": "org:b",
                    "scheme_code": "SOURCE_ID",
                    "namespace": "unresolved-test",
                    "raw_value": "unknown",
                    "normalized_value": "unknown",
                    "validation_status": "UNVALIDATED",
                    "resolution_status": "UNRESOLVED",
                    "conflict_status": "OPEN",
                },
            ],
        )


def test_metric_quality_constraints_and_numeric_storage(v2_engine: Engine) -> None:
    with v2_engine.begin() as connection:
        _seed_snapshot(connection)
        connection.execute(_entity("metric-subject", "ORGANIZATION"))
        for suffix in ("zero", "missing", "na"):
            connection.execute(
                insert(canonical_facts).values(
                    fact_id=f"fact:{suffix}",
                    subject_entity_id="metric-subject",
                    snapshot_id="snapshot:test",
                    fact_kind="METRIC",
                    semantic_key=f"metric:{suffix}",
                    resolution_status="RESOLVED",
                )
            )
        connection.execute(
            insert(metric_observations).values(
                fact_id="fact:zero",
                metric_code="AUM",
                subject_entity_id="metric-subject",
                raw_value="0",
                numeric_value=Decimal("0"),
                quality_status="SOURCE_ZERO",
                comparability_status="UNKNOWN",
            )
        )
        connection.execute(
            insert(metric_observations),
            [
                {
                    "fact_id": "fact:missing",
                    "metric_code": "AUM",
                    "subject_entity_id": "metric-subject",
                    "raw_value": None,
                    "numeric_value": None,
                    "quality_status": "MISSING",
                    "comparability_status": "UNKNOWN",
                },
                {
                    "fact_id": "fact:na",
                    "metric_code": "EXPENSE_RATIO",
                    "subject_entity_id": "metric-subject",
                    "raw_value": "N/A",
                    "numeric_value": None,
                    "quality_status": "NOT_APPLICABLE",
                    "comparability_status": "NOT_COMPARABLE",
                },
            ],
        )

    for fact_id, status, value in (
        ("fact:zero", "SOURCE_ZERO", Decimal("1")),
        ("fact:missing", "MISSING", Decimal("1")),
        ("fact:na", "NOT_APPLICABLE", Decimal("1")),
    ):
        _expect_integrity_error(
            v2_engine,
            metric_observations.update()
            .where(metric_observations.c.fact_id == fact_id)
            .values(quality_status=status, numeric_value=value),
        )

    assert isinstance(metric_observations.c.numeric_value.type, Numeric)
    assert not any(
        isinstance(column.type, Float)
        for table in metadata.tables.values()
        for column in table.columns
    )


def test_ready_snapshot_requires_completed_reconciliation(v2_engine: Engine) -> None:
    _expect_integrity_error(
        v2_engine,
        dataset_snapshots.update()
        .where(dataset_snapshots.c.snapshot_id == "snapshot:test")
        .values(status="READY"),
    )
    with v2_engine.begin() as connection:
        connection.execute(
            dataset_snapshots.update()
            .where(dataset_snapshots.c.snapshot_id == "snapshot:test")
            .values(
                status="READY",
                reconciliation_status="PASSED",
                row_count_reconciled=True,
            )
        )


def test_source_record_has_one_described_and_multiple_supported_entities(
    v2_engine: Engine,
) -> None:
    with v2_engine.begin() as connection:
        connection.execute(_entity("bond_kr:001", "FINANCIAL_PRODUCT", name="Bond 1", name_status="AUTHORITATIVE"))
        connection.execute(_entity("bond_kr:002", "FINANCIAL_PRODUCT", name="Bond 2", name_status="AUTHORITATIVE"))
        connection.execute(_entity("salelot:001", "SALE_LOT"))
        connection.execute(_entity("salelot:extra", "SALE_LOT"))
        connection.execute(_entity("fund_family:provenance", "FINANCIAL_PRODUCT"))
        connection.execute(_entity("fund_pub:provenance", "FUND_SHARE_CLASS"))
        connection.execute(
            insert(source_records),
            [
                {
                    "source_record_id": "source:prbd:1",
                    "snapshot_id": "snapshot:test",
                    "source_primary_key": "PRBD-1",
                    "source_row_number": 1,
                    "raw_payload": {"pd_no": "001"},
                    "payload_sha256": "p" * 64,
                },
                {
                    "source_record_id": "source:prfd:1",
                    "snapshot_id": "snapshot:test",
                    "source_primary_key": "PRFD-1",
                    "source_row_number": 2,
                    "raw_payload": {"itm_no": "FUND-CLASS-1"},
                    "payload_sha256": "f" * 64,
                },
            ],
        )
        connection.execute(
            insert(source_record_entities),
            [
                {
                    "source_record_id": "source:prbd:1",
                    "entity_id": "salelot:001",
                    "entity_kind": "SALE_LOT",
                    "provenance_role": "DESCRIBES",
                },
                {
                    "source_record_id": "source:prbd:1",
                    "entity_id": "bond_kr:001",
                    "entity_kind": "FINANCIAL_PRODUCT",
                    "provenance_role": "SUPPORTS",
                },
                {
                    "source_record_id": "source:prbd:1",
                    "entity_id": "bond_kr:002",
                    "entity_kind": "FINANCIAL_PRODUCT",
                    "provenance_role": "SUPPORTS",
                },
                {
                    "source_record_id": "source:prfd:1",
                    "entity_id": "fund_pub:provenance",
                    "entity_kind": "FUND_SHARE_CLASS",
                    "provenance_role": "DESCRIBES",
                },
                {
                    "source_record_id": "source:prfd:1",
                    "entity_id": "fund_family:provenance",
                    "entity_kind": "FINANCIAL_PRODUCT",
                    "provenance_role": "SUPPORTS",
                },
            ],
        )
        connection.execute(
            insert(source_field_assertions).values(
                assertion_id="assertion:prbd:bond-type",
                source_record_id="source:prbd:1",
                source_column="bond_type",
                raw_value="국채",
                normalized_value="BondType.Government",
                mapping_category="CLASSIFICATION",
                target_semantic_key="bond.type",
                quality_status="VALID",
                transformation_rule="exact-controlled-value",
            )
        )
        connection.execute(
            insert(canonical_facts).values(
                fact_id="fact:prbd:bond-type",
                subject_entity_id="bond_kr:001",
                snapshot_id="snapshot:test",
                fact_kind="CLASSIFICATION",
                semantic_key="bond.type:government",
                resolution_status="RESOLVED",
            )
        )
        connection.execute(
            insert(fact_evidence_links).values(
                fact_id="fact:prbd:bond-type",
                assertion_id="assertion:prbd:bond-type",
                evidence_role="SUPPORTS",
            )
        )

        roles = connection.execute(
            text(
                "SELECT source_record_id, provenance_role, count(*) "
                "FROM canonical_v2.source_record_entities "
                "GROUP BY source_record_id, provenance_role"
            )
        ).all()
        assert ("source:prbd:1", "DESCRIBES", 1) in roles
        assert ("source:prbd:1", "SUPPORTS", 2) in roles
        assert ("source:prfd:1", "DESCRIBES", 1) in roles
        assert ("source:prfd:1", "SUPPORTS", 1) in roles
        assert connection.scalar(
            text(
                "SELECT count(*) FROM canonical_v2.fact_evidence_links "
                "WHERE fact_id = 'fact:prbd:bond-type' "
                "AND assertion_id = 'assertion:prbd:bond-type'"
            )
        ) == 1

    _expect_integrity_error(
        v2_engine,
        insert(source_record_entities).values(
            source_record_id="source:prbd:1",
            entity_id="salelot:extra",
            entity_kind="SALE_LOT",
            provenance_role="DESCRIBES",
        ),
    )


def test_lookup_tables_are_allow_listed(v2_engine: Engine) -> None:
    with v2_engine.connect() as connection:
        product_type_codes = connection.execute(
            text("SELECT product_type_code FROM canonical_v2.product_types ORDER BY product_type_code")
        ).scalars().all()
        metric_flags = connection.execute(
            text("SELECT metric_code, filter_enabled, sort_enabled FROM canonical_v2.metric_definitions ORDER BY metric_code")
        ).all()
        schemes = connection.execute(
            identifier_schemes.select()
        ).mappings().all()

    assert product_type_codes == ["BOND", "ETF", "ETN", "FUND"]
    assert metric_flags == [
        ("AUM", False, False),
        ("EXPENSE_RATIO", False, False),
    ]
    assert {row["scheme_code"] for row in schemes} >= {
        "ISIN",
        "KSD_ID",
        "REPRESENTATIVE_KSD_ID",
        "SOURCE_ID",
        "TICKER",
    }

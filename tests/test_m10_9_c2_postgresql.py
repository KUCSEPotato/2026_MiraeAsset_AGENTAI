from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, func, insert, inspect, select, text
from sqlalchemy.engine import make_url

from app.data.holdings import (
    KODEX_PROVIDER,
    TrustedHoldingsCanonicalIntegrator,
    TrustedHoldingsSnapshot,
)
from app.data.v2_schema import (
    CANONICAL_V2_SCHEMA,
    canonical_entities,
    canonical_facts,
    dataset_snapshots,
    entity_identifiers,
    entity_relations,
    exchange_traded_products,
    external_holding_records,
    fact_evidence_links,
    financial_products,
    holding_fact_details,
    identifier_collision_cases,
    organizations,
    securities,
    source_datasets,
    source_field_assertions,
)
from app.external_data.holdings.models import (
    ExternalHolding,
    HoldingValidationStatus,
    IdentityStatus,
    NumericStatus,
    ProductCategory,
    TemporalStatus,
    WeightScale,
    WeightUnit,
)
from app.external_data.models import (
    ContentType,
    ExternalSourceRecord,
    QualityStatus,
    SourceTrustTier,
    SourceType,
)
from app.graph.v2 import CanonicalV2GraphExtractor
from app.graph.config import GraphSettings
from app.graph.v2 import CanonicalV2GraphBackend
from app.derived.manifest import DerivedStoreStatus


pytestmark = pytest.mark.postgresql
ROOT = Path(__file__).resolve().parents[1]
VERSION_TABLE = "alembic_version_m10_9_c2"


def _url() -> str:
    value = os.getenv("M10_9_C2_DATABASE_URL") or os.getenv("POSTGRES_TEST_DATABASE_URL")
    if not value:
        pytest.skip("M10_9_C2_DATABASE_URL is not configured")
    parsed = make_url(value)
    if parsed.get_backend_name() != "postgresql":
        pytest.fail("C2 integration requires PostgreSQL")
    database = (parsed.database or "").casefold()
    if "test" not in database and "c2" not in database:
        pytest.fail("C2 test refuses a non-disposable database")
    return value


def _config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    config.set_main_option("version_table", VERSION_TABLE)
    return config


@pytest.fixture(scope="module")
def engine():
    url = _url()
    result = create_engine(url, future=True)
    with result.begin() as connection:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {CANONICAL_V2_SCHEMA} CASCADE"))
        connection.execute(text(f"DROP TABLE IF EXISTS {VERSION_TABLE}"))
    command.upgrade(_config(url), "head")
    yield result
    command.downgrade(_config(url), "base")
    with result.begin() as connection:
        connection.execute(text(f"DROP TABLE IF EXISTS {VERSION_TABLE}"))
    result.dispose()


def _seed_canonical(connection) -> None:
    connection.execute(insert(source_datasets).values(
        dataset_id="dataset:kodex-holdings", dataset_code="KODEX_HOLDINGS",
        display_name="KODEX trusted holdings", source_system="KODEX",
        schema_contract_version="external-holdings-v1", is_authoritative=True,
    ))
    connection.execute(insert(dataset_snapshots).values(
        snapshot_id="snapshot:kodex:20260824", dataset_id="dataset:kodex-holdings",
        snapshot_date=date(2026, 8, 24), generation="external",
        ontology_version="merged-optical-1.4", semantic_mapping_version="team-v1-runtime-2026-08-29",
        transformer_version="m10.9-c2-kodex-holdings-1",
        database_schema_version="m10.9-c2-canonical-v2",
        data_sha256="d" * 64, schema_sha256="s" * 64, source_row_count=8,
        accepted_row_count=8, quarantined_row_count=0, status="READY",
        reconciliation_status="PASSED", row_count_reconciled=True,
    ))
    for product_id, name, isin, ticker in (
        ("etf:kodex:one", "KODEX ONE", "KR7000000001", "111111"),
        ("etf:kodex:two", "KODEX TWO", "KR7000000002", "222222"),
    ):
        connection.execute(insert(canonical_entities).values(
            entity_id=product_id, entity_kind="FINANCIAL_PRODUCT", preferred_name=name,
            normalized_preferred_name=name.casefold(), name_status="AUTHORITATIVE",
            identity_status="VALIDATED", query_eligible=True,
        ))
        connection.execute(insert(financial_products).values(product_id=product_id, product_type_code="ETF"))
        connection.execute(insert(exchange_traded_products).values(etp_id=product_id, product_type_code="ETF"))
        connection.execute(insert(entity_identifiers), [
            {"entity_id": product_id, "scheme_code": "ISIN", "namespace": "iso-6166", "raw_value": isin, "normalized_value": isin, "validation_status": "VALIDATED", "resolution_status": "RESOLVED", "conflict_status": "NONE", "is_primary": True},
            {"entity_id": product_id, "scheme_code": "TICKER", "namespace": "KRX", "raw_value": ticker, "normalized_value": ticker, "validation_status": "VALIDATED", "resolution_status": "RESOLVED", "conflict_status": "NONE", "is_primary": False},
        ])
    connection.execute(insert(canonical_entities).values(
        entity_id="org:samsung", entity_kind="ORGANIZATION", preferred_name="삼성전자",
        normalized_preferred_name="삼성전자", name_status="AUTHORITATIVE",
        identity_status="VALIDATED", query_eligible=True,
    ))
    connection.execute(insert(organizations).values(
        organization_id="org:samsung", entity_kind="ORGANIZATION", organization_type="ISSUER",
    ))
    connection.execute(insert(canonical_entities).values(
        entity_id="security:preexisting:isin", entity_kind="SECURITY",
        preferred_name="ISIN 식별 증권", normalized_preferred_name="isin식별증권",
        name_status="AUTHORITATIVE", identity_status="VALIDATED", query_eligible=True,
    ))
    connection.execute(insert(securities).values(
        security_id="security:preexisting:isin", entity_kind="SECURITY",
        security_type="EQUITY", isin="KR7000000099", exchange="KRX",
        issuer_resolution_status="UNRESOLVED",
    ))
    connection.execute(insert(entity_identifiers).values(
        entity_id="security:preexisting:isin", scheme_code="ISIN", namespace="iso-6166",
        raw_value="KR7000000099", normalized_value="KR7000000099",
        validation_status="VALIDATED", resolution_status="RESOLVED",
        conflict_status="NONE", is_primary=True,
    ))
    connection.execute(insert(identifier_collision_cases).values(
        collision_case_id="collision:product-ticker:333333",
        scheme_code="TICKER", namespace="KRX", normalized_value="333333",
        candidate_entity_ids=["etf:unknown:a", "etf:unknown:b"], status="OPEN",
        resolution_notes="C2 fail-closed test",
    ))


def _source(root: Path, source_id: str, product_code: str) -> ExternalSourceRecord:
    raw = json.dumps({"product": product_code, "effective_date": "2026-08-24"}, sort_keys=True).encode()
    relative = f"holdings/raw/{source_id}.json"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    url = f"https://www.samsungfund.com/api/v1/kodex/product-pdf/{product_code}.do"
    return ExternalSourceRecord(
        source_record_id=source_id, source_provider=KODEX_PROVIDER,
        source_type=SourceType.ASSET_MANAGER, source_trust_tier=SourceTrustTier.AUTHORITATIVE,
        source_url=url, normalized_url=url, retrieved_at=datetime(2026, 8, 30, tzinfo=UTC),
        effective_date=date(2026, 8, 24), content_type=ContentType.JSON,
        raw_content_hash=hashlib.sha256(raw).hexdigest(), parser_version="kodex-holdings-json-v1",
        crawler_version="crawler-v1", snapshot_id="kodex-20260824",
        quality_status=QualityStatus.VALID, raw_artifact_path=relative,
    )


def _holding(number: int, *, source: ExternalSourceRecord, product_isin: str | None,
             product_source_id: str, security_ticker: str | None,
             security_name: str, product_ticker: str | None = None,
             constituent_status: IdentityStatus = IdentityStatus.SOURCE_ID_ONLY) -> ExternalHolding:
    return ExternalHolding(
        holding_record_id=f"holding:{number}", product_category=ProductCategory.DOMESTIC_ETF,
        product_name_raw=product_source_id, product_isin=product_isin,
        product_ticker=product_ticker,
        product_source_id=product_source_id, constituent_name_raw=security_name,
        constituent_ticker=security_ticker, constituent_source_id=security_ticker,
        weight_raw="10.0", weight_normalized=Decimal("0.10"),
        weight_unit=WeightUnit.PERCENT_OF_NON_CASH_ASSETS,
        weight_scale=WeightScale.PERCENT_POINTS, effective_date=date(2026, 8, 24),
        retrieved_at=datetime(2026, 8, 30, tzinfo=UTC), source_record_id=source.source_record_id,
        source_provider=KODEX_PROVIDER, source_url=source.source_url,
        source_trust_tier=SourceTrustTier.AUTHORITATIVE, snapshot_id="kodex-20260824",
        identity_status=IdentityStatus.SOURCE_ID_ONLY,
        product_identity_status=IdentityStatus.VERIFIED_IDENTIFIER,
        constituent_identity_status=constituent_status,
        numeric_status=NumericStatus.PARTIAL,
        temporal_status=TemporalStatus.EFFECTIVE_DATE_VERIFIED,
        validation_status=(
            HoldingValidationStatus.VALID
            if constituent_status is IdentityStatus.SOURCE_ID_ONLY
            else HoldingValidationStatus.PARTIAL
        ),
    )


def _snapshot(tmp_path: Path) -> TrustedHoldingsSnapshot:
    first = _source(tmp_path, "extrec:one", "2ETFONE")
    second = _source(tmp_path, "extrec:two", "2ETFTWO")
    third = _source(tmp_path, "extrec:three", "2ETFAMBIGUOUS")
    holdings = (
        _holding(1, source=first, product_isin="KR7000000001", product_source_id="2ETFONE", security_ticker="005930", security_name="삼성전자"),
        _holding(2, source=first, product_isin="KR7000000001", product_source_id="2ETFONE", security_ticker="000660", security_name="SK하이닉스"),
        _holding(3, source=second, product_isin="KR7000000002", product_source_id="2ETFTWO", security_ticker="005930", security_name="삼성전자"),
        _holding(4, source=first, product_isin=None, product_ticker="111111", product_source_id="2ETFONE", security_ticker="005930", security_name="삼성전자"),
        _holding(5, source=third, product_isin=None, product_ticker="333333", product_source_id="2ETFAMBIGUOUS", security_ticker="035420", security_name="NAVER"),
        _holding(6, source=first, product_isin="KR7000000001", product_source_id="2ETFONE", security_ticker=None, security_name="이름만 있는 자산", constituent_status=IdentityStatus.NAME_ONLY),
        _holding(7, source=first, product_isin="KR7000000001", product_source_id="2ETFONE", security_ticker=None, security_name="원화예금", constituent_status=IdentityStatus.NON_SECURITY),
        _holding(8, source=first, product_isin="KR7000000001", product_source_id="2ETFONE", security_ticker=None, security_name="ISIN 식별 증권", constituent_status=IdentityStatus.VERIFIED_IDENTIFIER).model_copy(update={"constituent_isin": "KR7000000099", "constituent_source_id": None}),
    )
    manifest = {"schema_version": "external-snapshot-manifest-v1", "status": "READY", "data_cutoff_date": "2026-08-24", "normalized_row_counts": {"external-holdings-v1": 8}}
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
    (tmp_path / "manifest.json").write_bytes(manifest_bytes)
    return TrustedHoldingsSnapshot(
        external_snapshot_id="kodex-20260824", canonical_snapshot_id="snapshot:kodex:20260824",
        manifest_schema_version="external-snapshot-manifest-v1", manifest_status="READY",
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(), manifest_json=manifest,
        data_cutoff_date=date(2026, 8, 24), artifact_root=tmp_path,
        source_records=(first, second, third), holdings=holdings,
    )


def test_fresh_migration_has_c2_tables(engine) -> None:
    names = set(inspect(engine).get_table_names(schema=CANONICAL_V2_SCHEMA))
    assert {"securities", "holding_fact_details", "external_snapshot_manifests", "external_raw_artifacts", "external_source_records", "external_holding_records"} <= names


def test_holdings_identity_provenance_and_idempotency(engine, tmp_path: Path) -> None:
    with engine.begin() as connection:
        _seed_canonical(connection)
        integrator = TrustedHoldingsCanonicalIntegrator(
            connection,
            issuer_by_security_identifier={
                ("TICKER", "KRX", "005930"): "org:samsung",
            },
        )
        snapshot = _snapshot(tmp_path)
        first = integrator.integrate(snapshot)
        second = integrator.integrate(snapshot)

    assert first.product_resolved == 7
    assert first.product_ambiguous == 1
    assert first.security_created == 3
    assert first.security_resolved == 6
    assert first.security_unresolved == 1
    assert first.non_security == 1
    assert first.canonical_holds_facts == 4
    assert first.deduplicated == 1
    assert first.issuer_resolved == 3
    assert first.issuer_unresolved == 2
    assert second.canonical_holds_facts == 0
    assert second.deduplicated == 5
    with engine.connect() as connection:
        counts = dict(connection.execute(
            select(entity_relations.c.relation_type, func.count())
            .group_by(entity_relations.c.relation_type)
        ).all())
        assert counts == {"HOLDS": 4, "SECURITY_ISSUED_BY": 1}
        assert connection.scalar(select(func.count()).select_from(holding_fact_details)) == 4
        assert connection.scalar(select(func.count()).select_from(external_holding_records)) == 8
        # Five source assertions support four HOLDS facts; three assertions
        # support the one deduplicated Samsung issuer fact.
        assert connection.scalar(select(func.count()).select_from(fact_evidence_links)) == 8
        unresolved = connection.scalar(select(func.count()).select_from(canonical_entities).where(
            canonical_entities.c.entity_kind == "SECURITY",
            canonical_entities.c.preferred_name == "SK하이닉스",
        ))
        assert unresolved == 1
        chain = connection.execute(
            select(canonical_facts.c.fact_id, source_field_assertions.c.source_record_id)
            .join(fact_evidence_links, fact_evidence_links.c.fact_id == canonical_facts.c.fact_id)
            .join(source_field_assertions, source_field_assertions.c.assertion_id == fact_evidence_links.c.assertion_id)
            .where(canonical_facts.c.semantic_key.like("holds:%"))
        ).all()
        assert len(chain) == 5
        assert all(record.startswith("normalized:holding:") for _, record in chain)


def test_postgresql_graph_projection_reconciles_c2_facts(engine) -> None:
    data = CanonicalV2GraphExtractor(
        engine,
        snapshot_ids=("snapshot:kodex:20260824",),
        snapshot="2026-08-24",
    ).extract()
    assert data.stats.edges_by_relation == {
        "HOLDS": 4,
        "SECURITY_ISSUED_BY": 1,
    }
    # NAVER is a valid canonical Security even though its source product is
    # ambiguous; no HOLDS fact is emitted for that unresolved product.
    assert data.stats.nodes_by_type["EquitySecurity"] == 4
    security_nodes = [node for node in data.nodes if node.node_type == "EquitySecurity"]
    assert security_nodes
    assert all(node.properties["exchange"] == "KRX" for node in security_nodes)
    holds = [edge for edge in data.edges if edge.edge_type == "HOLDS"]
    assert len(holds) == 4
    assert all(edge.object_id.startswith("security:") for edge in holds)
    assert all(edge.properties["effective_date"] == "2026-08-24" for edge in holds)
    assert all(edge.properties["canonical_fact_id"] for edge in holds)
    assert all(edge.properties["evidence_assertion_ids"] for edge in holds)
    assert all(edge.properties["source_fields"] for edge in holds)
    assert all(edge.properties["source_record_keys"] for edge in holds)


def test_global_security_identity_is_exchange_scoped_and_isin_stable(
    engine, tmp_path: Path,
) -> None:
    source = _source(tmp_path, "extrec:global-identity", "2ETFONE")
    base = _holding(
        90, source=source, product_isin="KR7000000001",
        product_source_id="2ETFONE", security_ticker="ABC",
        security_name="Global Security",
        constituent_status=IdentityStatus.VERIFIED_IDENTIFIER,
    ).model_copy(update={
        "product_category": ProductCategory.FOREIGN_ETF,
        "source_provider": "BlackRock iShares",
    })
    connection = engine.connect()
    transaction = connection.begin()
    try:
        integrator = TrustedHoldingsCanonicalIntegrator(connection)
        xnas, xnas_created = integrator._resolve_security(
            base.model_copy(update={
                "constituent_exchange": "XNAS",
                "constituent_source_id": "XNAS:ABC",
            }), source
        )
        xlon, xlon_created = integrator._resolve_security(
            base.model_copy(update={
                "constituent_exchange": "XLON",
                "constituent_source_id": "XLON:ABC",
            }), source
        )
        assert xnas_created and xlon_created
        assert xnas.entity_id != xlon.entity_id

        first_isin, first_created = integrator._resolve_security(
            base.model_copy(update={
                "constituent_ticker": "ONE",
                "constituent_exchange": "XNYS",
                "constituent_isin": "US0000000091",
                "constituent_source_id": "XNYS:ONE",
            }), source,
        )
        second_isin, second_created = integrator._resolve_security(
            base.model_copy(update={
                "constituent_ticker": "TWO",
                "constituent_exchange": "XNAS",
                "constituent_isin": "US0000000091",
                "constituent_source_id": "KODEX-ISIN-ALIAS",
                "source_provider": KODEX_PROVIDER,
            }), source,
        )
        assert first_created and not second_created
        assert first_isin.entity_id == second_isin.entity_id

        unresolved, created = integrator._resolve_security(
            base.model_copy(update={
                "constituent_exchange": None,
                "constituent_isin": None,
                "constituent_source_id": None,
            }), source,
        )
        assert unresolved.status == "UNRESOLVED"
        assert not created

        krx = integrator._identifier("TICKER", "KRX", "005930", "SECURITY")
        assert krx.status == "RESOLVED"
        assert krx.entity_id != xnas.entity_id
    finally:
        transaction.rollback()
        connection.close()


def test_real_neo4j_holds_reconciliation_and_reverse_traversal(engine) -> None:
    uri = os.getenv("M10_9_C2_NEO4J_URI")
    password = os.getenv("M10_9_C2_NEO4J_PASSWORD")
    if not uri or not password:
        pytest.skip("disposable C2 Neo4j is not configured")
    extractor = CanonicalV2GraphExtractor(
        engine,
        snapshot_ids=("snapshot:kodex:20260824",),
        snapshot="2026-08-24",
    )
    data = extractor.extract()
    settings = GraphSettings(uri=uri, password=password)

    async def verify() -> None:
        backend = CanonicalV2GraphBackend.connect(settings)
        try:
            await backend.verify_connectivity()
            ready = await backend.build(
                data,
                extractor.manifest(data, status=DerivedStoreStatus.BUILDING),
            )
            assert ready.status is DerivedStoreStatus.READY
            assert await backend.relation_counts(snapshot="2026-08-24") == {
                "HOLDS": 4,
                "SECURITY_ISSUED_BY": 1,
            }
            reverse = await backend.query(
                "MATCH (p:M108DNode)-[h:HOLDS]->(s:M108DNode) "
                "WHERE h.dataset_snapshot = $snapshot AND s.display_name = $name "
                "RETURN p.entity_id AS product_id ORDER BY product_id",
                {"snapshot": "2026-08-24", "name": "삼성전자"},
            )
            assert [row["product_id"] for row in reverse] == [
                "etf:kodex:one", "etf:kodex:two",
            ]
            issuer_path = await backend.query(
                "MATCH (p:M108DNode)-[:HOLDS]->(:M108DNode)-[:SECURITY_ISSUED_BY]->"
                "(o:M108DNode) WHERE o.display_name = $name "
                "RETURN DISTINCT p.entity_id AS product_id ORDER BY product_id",
                {"name": "삼성전자"},
            )
            assert [row["product_id"] for row in issuer_path] == [
                "etf:kodex:one", "etf:kodex:two",
            ]
        finally:
            await backend.close()

    asyncio.run(verify())

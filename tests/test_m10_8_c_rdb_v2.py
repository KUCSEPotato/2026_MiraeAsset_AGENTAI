from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.engine import Engine, make_url

from app.data.v2_schema import (
    canonical_entities,
    dataset_snapshots,
    entity_identifiers,
    financial_products,
    fund_share_classes,
    identifier_collision_cases,
    organization_relations,
)
from app.data.database import DatabaseSettings
from app.domain.models import (
    ExecutionContext,
    QueryOperation,
    QueryPlan,
    QueryStep,
    RetrievalSource,
)
from app.entity.rdb_v2_lookup import CanonicalV2EntityLookup
from app.entity.resolver import RegistryEntityResolver
from app.retrieval.exceptions import RDBQueryCompilationError
from app.retrieval.rdb import RDBFieldRegistry, RDBQueryCompiler, RealRDBRetriever
from app.retrieval.rdb_v2 import (
    CanonicalV2FieldRegistry,
    CanonicalV2QueryCompiler,
    CanonicalV2RDBRetriever,
    CanonicalV2SnapshotSelector,
    RDBShadowDifference,
    ReadOnlyRDBShadowComparator,
    V2SnapshotUnavailableError,
)


pytestmark = pytest.mark.postgresql
ROOT = Path(__file__).resolve().parents[1]


def _url() -> str:
    value = os.getenv("M10_8_C_DATABASE_URL")
    if not value:
        pytest.skip("M10_8_C_DATABASE_URL is not configured")
    parsed = make_url(value)
    if parsed.get_backend_name() != "postgresql":
        pytest.fail("M10_8_C_DATABASE_URL must use PostgreSQL")
    database = (parsed.database or "").casefold()
    if "test" not in database and "m108c" not in database:
        pytest.fail("M10.8-C requires a disposable test/m108c database")
    return value


@pytest.fixture(scope="module")
def v2_engine() -> Engine:
    engine = create_engine(_url(), future=True)
    with engine.connect() as connection:
        ready = connection.scalar(
            select(dataset_snapshots.c.snapshot_id).where(
                dataset_snapshots.c.status == "READY",
                dataset_snapshots.c.reconciliation_status == "PASSED",
            ).limit(1)
        )
    if ready is None:
        pytest.fail(
            "M10.8-C fixture requires the authoritative M10.8-B.2 rebuild"
        )
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def selector() -> CanonicalV2SnapshotSelector:
    return CanonicalV2SnapshotSelector(snapshot_date="2026-08-24")


@pytest.fixture(scope="module")
def retriever(v2_engine, selector) -> CanonicalV2RDBRetriever:
    return CanonicalV2RDBRetriever(
        v2_engine,
        CanonicalV2QueryCompiler(
            CanonicalV2FieldRegistry(), default_limit=100, max_limit=1_000
        ),
        selector,
    )


def _filter(field: str, value: str, *, operator: str = "eq") -> dict:
    return {
        "canonical_field": field,
        "canonical_value": value,
        "raw": {"field": field, "operator": operator, "value": value},
    }


def _step(**inputs) -> QueryStep:
    return QueryStep(
        step_id="v2-rdb",
        source=RetrievalSource.RDB,
        operation=QueryOperation.SEARCH_PRODUCTS,
        inputs=inputs,
    )


def _run(retriever, step):
    plan = QueryPlan(planner="rule", steps=[step])
    return asyncio.run(retriever.retrieve(step, ExecutionContext(plan=plan)))


def _run_result(retriever, step):
    plan = QueryPlan(planner="rule", steps=[step])
    return asyncio.run(
        retriever.retrieve_with_result(step, ExecutionContext(plan=plan))
    )


def _ids(records) -> list[str]:
    return list(dict.fromkeys(str(item.entity_id) for item in records))


def test_ready_snapshot_selection_is_exact(v2_engine, selector) -> None:
    with v2_engine.connect() as connection:
        selected = selector.select(connection)
    assert selected.generation == "260824"
    assert selected.ontology_version == "merged-optical-1.4"
    assert len(selected.snapshot_ids) == 4


def test_no_ready_snapshot_fails_closed(v2_engine, selector) -> None:
    with v2_engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(
            update(dataset_snapshots)
            .where(dataset_snapshots.c.status == "READY")
            .values(status="RETIRED")
        )
        with pytest.raises(V2SnapshotUnavailableError):
            selector.select(connection)
        transaction.rollback()


def test_collection_search_returns_multiple_deterministic_entities(retriever) -> None:
    step = _step(product_types=["FinancialProduct.ETF"], limit=100)
    first = _ids(_run(retriever, step))
    second = _ids(_run(retriever, step))
    assert len(first) > 1
    assert first == sorted(first)
    assert second == first


def test_collection_cardinality_is_distinct_from_result_window(retriever) -> None:
    step = _step(
        product_types=["FinancialProduct.ETF"],
        filters=[
            _filter("product.asset_type", "AssetType.Equity"),
            _filter("product.region", "Region.US"),
        ],
        limit=10,
    )
    first = _run_result(retriever, step)
    second = _run_result(retriever, step)
    assert first.total_matches == 1_749
    assert first.returned_count == len(first.records) == 10
    assert first.window_limit == 10
    assert [item.entity_id for item in first.records] == [item.entity_id for item in second.records]
    assert second.total_matches == first.total_matches


def test_collection_cardinality_preserves_zero_bond_and_public_fund_grain(retriever) -> None:
    zero = _run_result(
        retriever,
        _step(product_types=["FinancialProduct.ETN"], filters=[
            _filter("product.asset_type", "AssetType.Bond"),
            _filter("product.region", "Region.IN"),
        ], limit=10),
    )
    assert zero.total_matches == zero.returned_count == len(zero.records) == 0

    bond = _run_result(
        retriever, _step(product_types=["FinancialProduct.Bond"], limit=10)
    )
    assert bond.total_matches == 20_497
    assert bond.returned_count == len(bond.records) == 10

    public_fund = _run_result(
        retriever,
        _step(product_types=["FinancialProduct.Fund"], filters=[
            _filter("product.offering_type", "OfferingType.PUBLIC"),
        ], limit=10),
    )
    assert public_fund.total_matches == 2_783
    assert public_fund.returned_count == len(public_fund.records) == 10
    assert all(item.metadata["product_type"] == "FUND" for item in public_fund.records)


@pytest.mark.parametrize(
    ("region", "minimum"),
    [("Region.US", 2), ("Region.IN", 1)],
)
def test_equity_etf_exposure_region_uses_canonical_classifications(
    retriever, region, minimum
) -> None:
    records = _run(
        retriever,
        _step(
            product_types=["FinancialProduct.ETF"],
            filters=[
                _filter("product.asset_type", "AssetType.Equity"),
                _filter("product.region", region),
            ],
            requested_fields=["product.name", "product.region"],
            limit=1_000,
        ),
    )
    assert len(set(_ids(records))) >= minimum
    assert all(item.metadata["repository_version"] == "v2" for item in records)


def test_listing_country_is_not_exposure_region(retriever) -> None:
    exposure = _ids(
        _run(
            retriever,
            _step(
                product_types=["FinancialProduct.ETF"],
                filters=[_filter("product.region", "Region.IN")],
                limit=1_000,
            ),
        )
    )
    assert exposure
    with pytest.raises(RDBQueryCompilationError, match="relation target"):
        _run(
            retriever,
            _step(
                product_types=["FinancialProduct.ETF"],
                relations=[
                    {
                        "canonical_relation": "listedInCountry",
                        "target_entity_id": "country:IN",
                    }
                ],
                limit=1_000,
            ),
        )


@pytest.mark.parametrize(
    ("product_type", "currency", "has_rows"),
    [
        ("FinancialProduct.ETF", "USD", True),
        # M10.9-C1 normalizes the authoritative PREF01 CURR_CD_KRW code so
        # source-scoped KRW AUM comparisons have an enforceable currency fact.
        ("FinancialProduct.ETF", "KRW", True),
        ("FinancialProduct.Bond", "KRW", True),
    ],
)
def test_currency_filters_use_observed_controlled_entities(
    retriever, product_type, currency, has_rows
) -> None:
    records = _run(
        retriever,
        _step(
            product_types=[product_type],
            filters=[_filter("product.currency", currency)],
            requested_fields=["product.currency"],
            limit=1_000,
        ),
    )
    assert bool(records) is has_rows
    assert all(item.payload["value"] == currency for item in records)


def test_negation_requires_a_known_canonical_classification(retriever) -> None:
    records = _run(
        retriever,
        _step(
            product_types=["FinancialProduct.ETF"],
            filters=[
                _filter("product.region", "Region.US", operator="ne")
            ],
            requested_fields=["product.region"],
            limit=1_000,
        ),
    )
    assert records
    assert all(item.payload["value"] is not None for item in records)
    assert all(item.payload["value"] != "ExposureRegion.UnitedStates" for item in records)


def test_risk_grade_one_etf_is_structured_rdb_query(retriever) -> None:
    records = _run(
        retriever,
        _step(
            product_types=["FinancialProduct.ETF"],
            filters=[_filter("product.risk_grade", "RiskGrade.1")],
            limit=1_000,
        ),
    )
    assert _ids(records)


@pytest.mark.parametrize(
    ("product_type", "field", "value"),
    [
        ("FinancialProduct.Fund", "product.asset_type", "AssetType.Mixed"),
        ("FinancialProduct.Fund", "product.market_scope", "MarketScope.Domestic"),
        ("FinancialProduct.Bond", "product.bond_type", "BondType.국고채권"),
        ("FinancialProduct.Fund", "product.offering_type", "OfferingType.PUBLIC"),
    ],
)
def test_active_classification_dimensions_are_compositional(
    retriever, product_type, field, value
) -> None:
    records = _run(
        retriever,
        _step(
            product_types=[product_type],
            filters=[_filter(field, value)],
            limit=1_000,
        ),
    )
    assert _ids(records)


def test_india_bond_etn_is_true_zero_match(retriever) -> None:
    records = _run(
        retriever,
        _step(
            product_types=["FinancialProduct.ETN"],
            filters=[
                _filter("product.asset_type", "AssetType.Bond"),
                _filter("product.region", "Region.IN"),
            ],
            limit=1_000,
        ),
    )
    assert records == []


def test_unmapped_europe_exposure_fails_closed(retriever) -> None:
    with pytest.raises(RDBQueryCompilationError, match="unsupported.*classification"):
        _run(
            retriever,
            _step(
                product_types=["FinancialProduct.ETF"],
                filters=[_filter("product.region", "Region.Europe")],
            ),
        )


def test_public_fund_is_fund_exists_public_class_not_publicfund_type(retriever) -> None:
    records = _run(
        retriever,
        _step(product_types=["FinancialProduct.PublicFund"], limit=1_000),
    )
    assert _ids(records)
    assert all(item.metadata["entity_kind"] == "FINANCIAL_PRODUCT" for item in records)
    assert all(item.metadata["product_type"] == "FUND" for item in records)
    assert all(item.metadata["preferred_name"] is None for item in records)
    assert all(
        item.metadata["name_status"] == "NO_AUTHORITATIVE_FAMILY_NAME"
        for item in records
    )


def test_fund_share_class_grain_preserves_parent(retriever) -> None:
    records = _run(
        retriever,
        _step(
            result_grain="fund_share_class",
            product_types=["FinancialProduct.Fund"],
            limit=100,
        ),
    )
    assert len(_ids(records)) == 100
    assert all(item.metadata["entity_kind"] == "FUND_SHARE_CLASS" for item in records)
    assert all(item.metadata.get("parent_fund_id") for item in records)


def test_bond_product_grain_deduplicates_lots_and_lot_query_does_not(retriever) -> None:
    bonds = _run(
        retriever,
        _step(product_types=["FinancialProduct.Bond"], limit=1_000),
    )
    lots = _run(
        retriever,
        _step(
            result_grain="sale_lot",
            product_types=["FinancialProduct.Bond"],
            limit=1_000,
        ),
    )
    assert len(_ids(bonds)) == len(set(_ids(bonds)))
    assert all(item.metadata["entity_kind"] == "SALE_LOT" for item in lots)
    assert len({item.metadata["bond_id"] for item in lots}) < len(_ids(lots))


def test_etn_manager_relation_is_absent(retriever) -> None:
    records = _run(
        retriever,
        _step(
            product_types=["FinancialProduct.ETN"],
            requested_fields=["product.asset_manager"],
            limit=1_000,
        ),
    )
    assert records
    assert all(item.payload["value"] is None for item in records)


def test_fund_manager_query_uses_promoted_canonical_relation_only(
    v2_engine, retriever
) -> None:
    with v2_engine.connect() as connection:
        target = connection.scalar(
            select(organization_relations.c.organization_id)
            .join(
                financial_products,
                financial_products.c.product_id
                == organization_relations.c.subject_product_id,
            )
            .where(organization_relations.c.relation_type == "MANAGED_BY")
            .where(financial_products.c.product_type_code == "FUND")
            .limit(1)
        )
    assert target is not None
    records = _run(
        retriever,
        _step(
            product_types=["FinancialProduct.Fund"],
            relations=[
                {
                    "canonical_relation": "managedBy",
                    "target_entity_id": str(target),
                }
            ],
            requested_fields=["product.asset_manager"],
            limit=1_000,
        ),
    )
    assert records
    assert all(item.metadata["product_type"] == "FUND" for item in records)
    assert all(item.payload["value"] is not None for item in records)


def test_rejected_composite_benchmark_is_not_recovered_from_source_text(
    retriever,
) -> None:
    records = _run(
        retriever,
        _step(
            product_types=["FinancialProduct.Fund"],
            relations=[
                {
                    "canonical_relation": "hasBenchmark",
                    "target_entity_id": "index:rejected-composite",
                }
            ],
            limit=1_000,
        ),
    )
    assert records == []


def test_numeric_filter_and_sort_capability_gates(retriever) -> None:
    with pytest.raises(RDBQueryCompilationError, match="filtering.*disabled"):
        _run(
            retriever,
            _step(filters=[_filter("product.aum", "100")]),
        )
    with pytest.raises(RDBQueryCompilationError, match="sorting.*disabled"):
        _run(
            retriever,
            _step(
                sort=[
                    {
                        "canonical_field": "product.expense_ratio",
                        "raw": {"direction": "asc"},
                    }
                ]
            ),
        )


def test_records_carry_fact_to_source_provenance(retriever) -> None:
    record = _run(
        retriever,
        _step(product_types=["FinancialProduct.ETF"], limit=1),
    )[0]
    assert record.metadata["canonical_fact_ids"]
    assert record.metadata["evidence_assertion_ids"]
    assert record.metadata["source_record_ids"]
    assert record.metadata["source_datasets"]
    assert record.metadata["snapshot_identity"] == "260824:2026-08-24"


def test_exact_identifier_lookup_and_collision_ambiguity(v2_engine, selector) -> None:
    lookup = CanonicalV2EntityLookup(v2_engine, selector)
    resolver = RegistryEntityResolver(lookup)
    with v2_engine.connect() as connection:
        unique = connection.execute(
            select(
                entity_identifiers.c.raw_value,
                entity_identifiers.c.entity_id,
            ).where(
                entity_identifiers.c.validation_status == "VALIDATED",
                entity_identifiers.c.conflict_status == "NONE",
            ).limit(1)
        ).one()
        collision = connection.execute(
            select(
                identifier_collision_cases.c.normalized_value,
                identifier_collision_cases.c.candidate_entity_ids,
            ).where(identifier_collision_cases.c.status == "OPEN").limit(1)
        ).one()
    exact = asyncio.run(lookup.lookup(str(unique.raw_value), "product"))
    assert [item.entity.canonical_id for item in exact] == [str(unique.entity_id)]
    ambiguous = asyncio.run(lookup.lookup(str(collision.normalized_value), "product"))
    assert {item.entity.canonical_id for item in ambiguous} == set(
        collision.candidate_entity_ids
    )
    del resolver  # Resolver's exact-one/ambiguous behavior is covered separately.


def test_named_fund_share_class_resolves_at_class_grain(
    v2_engine, selector, retriever
) -> None:
    with v2_engine.connect() as connection:
        fixture = connection.execute(
            select(
                canonical_entities.c.entity_id,
                canonical_entities.c.preferred_name,
                fund_share_classes.c.parent_fund_id,
            )
            .join(
                fund_share_classes,
                fund_share_classes.c.fund_share_class_id
                == canonical_entities.c.entity_id,
            )
            .where(canonical_entities.c.preferred_name.is_not(None))
            .order_by(canonical_entities.c.entity_id)
            .limit(1)
        ).one()
    matches = asyncio.run(
        CanonicalV2EntityLookup(v2_engine, selector).lookup(
            str(fixture.preferred_name), "fund_share_class"
        )
    )
    assert any(item.entity.canonical_id == fixture.entity_id for item in matches)
    records = _run(
        retriever,
        _step(
            result_grain="fund_share_class",
            product_types=["FinancialProduct.Fund"],
            entity_ids=[str(fixture.entity_id)],
        ),
    )
    assert _ids(records) == [str(fixture.entity_id)]
    assert records[0].metadata["parent_fund_id"] == fixture.parent_fund_id


def test_repository_rejects_non_postgresql() -> None:
    sqlite = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with pytest.raises(ValueError, match="requires PostgreSQL"):
        CanonicalV2RDBRetriever(
            sqlite,
            CanonicalV2QueryCompiler(
                CanonicalV2FieldRegistry(), default_limit=10
            ),
            CanonicalV2SnapshotSelector(snapshot_date="2026-08-24"),
        )
    sqlite.dispose()


def test_repository_selection_is_explicit_and_production_default_stays_v1() -> None:
    url = "postgresql+psycopg://user:password@127.0.0.1/testdb"
    assert DatabaseSettings(database_url=url).rdb_repository_version == "v1"
    assert (
        DatabaseSettings(
            database_url=url, rdb_repository_version="v2"
        ).rdb_repository_version
        == "v2"
    )
    with pytest.raises(ValueError, match="must be v1 or v2"):
        DatabaseSettings(database_url=url, rdb_repository_version="invalid")


def test_v1_v2_read_only_shadow_comparison(v2_engine, retriever) -> None:
    step = _step(product_types=["FinancialProduct.ETF"], limit=100)
    plan = QueryPlan(planner="rule", steps=[step])
    v1 = RealRDBRetriever(
        v2_engine,
        RDBQueryCompiler(
            RDBFieldRegistry(),
            default_limit=100,
            max_limit=100,
            snapshot_date="2026-08-24",
        ),
    )
    comparison = asyncio.run(
        ReadOnlyRDBShadowComparator(v1, retriever).compare(
            step,
            ExecutionContext(plan=plan),
            expected_difference=RDBShadowDifference.EXPECTED_SEMANTIC_CHANGE,
        )
    )
    assert comparison.v1_entity_ids
    assert comparison.v2_entity_ids
    if comparison.v1_entity_ids != comparison.v2_entity_ids:
        assert (
            comparison.classification
            is RDBShadowDifference.EXPECTED_SEMANTIC_CHANGE
        )

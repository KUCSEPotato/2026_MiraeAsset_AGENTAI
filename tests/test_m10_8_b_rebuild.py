from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, func, insert, inspect, select, text
from sqlalchemy.engine import Engine, make_url

from app.data.ingest import FinancialDataIngestor
from app.data.schema import canonical_products as v1_products
from app.data.v2_rebuild import (
    CanonicalV2Rebuilder,
    _RELATION_DOMAIN_CONTRACTS,
    relation_domain_violations,
)
from app.data.v2_schema import (
    CANONICAL_V2_SCHEMA,
    bonds,
    canonical_entities,
    canonical_facts,
    dataset_snapshots,
    entity_classifications,
    entity_id_crosswalk,
    entity_identifiers,
    entity_relations,
    exchange_traded_products,
    fact_conflict_cases,
    fact_evidence_links,
    financial_products,
    fund_share_classes,
    funds,
    identifier_collision_cases,
    identity_resolution_cases,
    index_relations,
    metric_observations,
    ontology_concepts,
    organization_relations,
    organizations,
    quarantine_records,
    sale_lots,
    source_classification_values,
    source_field_assertions,
    source_record_entities,
    source_records,
)


pytestmark = pytest.mark.postgresql
ROOT = Path(__file__).resolve().parents[1]
VERSION_TABLE = "alembic_version_m10_8"


def test_subscription_status_relation_domain_contract_is_narrow() -> None:
    contract = _RELATION_DOMAIN_CONTRACTS["HAS_SUBSCRIPTION_STATUS"]
    assert contract.subject_grains == frozenset({("FUND_SHARE_CLASS", None)})
    assert contract.target_grains == frozenset(
        {("ONTOLOGY_CONCEPT", "subscription_status")}
    )
    assert ("FINANCIAL_PRODUCT", "BOND") not in contract.subject_grains
    assert ("ONTOLOGY_CONCEPT", "offering_type") not in contract.target_grains


def _url() -> str:
    value = os.getenv("M10_8_B_DATABASE_URL")
    if not value:
        pytest.skip("M10_8_B_DATABASE_URL is not configured")
    parsed = make_url(value)
    if parsed.get_backend_name() != "postgresql":
        pytest.fail("M10_8_B_DATABASE_URL must use PostgreSQL")
    database = (parsed.database or "").casefold()
    if "test" not in database and "m108b" not in database:
        pytest.fail("M10.8-B destructive acceptance requires a disposable test/m108b database")
    return value


def _config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def _table_counts(engine: Engine) -> dict[str, int]:
    tables = (
        canonical_entities,
        financial_products,
        bonds,
        funds,
        fund_share_classes,
        sale_lots,
        source_records,
        source_field_assertions,
        source_record_entities,
        entity_identifiers,
        entity_relations,
        entity_classifications,
        metric_observations,
        identity_resolution_cases,
        identifier_collision_cases,
        fact_conflict_cases,
        fact_evidence_links,
        entity_id_crosswalk,
    )
    with engine.connect() as connection:
        return {
            table.name: int(connection.scalar(select(func.count()).select_from(table)) or 0)
            for table in tables
        }


@pytest.fixture(scope="module")
def rebuilt() -> tuple[Engine, object, object, dict[str, int], dict[str, int]]:
    url = _url()
    engine = create_engine(url, future=True)

    # This opt-in is only for rechecking assertions after a completed clean
    # acceptance run; the default path below always rebuilds from empty v2.
    if os.getenv("M10_8_B_REUSE_READY") == "1":
        second = CanonicalV2Rebuilder(engine).rebuild(ROOT / "material")
        assert second.status == "SKIPPED_UNCHANGED"
        counts = _table_counts(engine)
        yield engine, replace(second, status="READY", skipped=False), second, counts, counts
        engine.dispose()
        return

    # Populate the authorized disposable v1 baseline if it is not already
    # present.  Never derive v2 entities from these rows; they are used only
    # for regression and crosswalk verification.
    if not inspect(engine).has_table(v1_products.name):
        FinancialDataIngestor(engine, batch_size=1_000).ingest_all(ROOT / "material")

    with engine.begin() as connection:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {CANONICAL_V2_SCHEMA} CASCADE"))
        connection.execute(text(f"DROP TABLE IF EXISTS {VERSION_TABLE}"))
    command.upgrade(_config(url), "head")

    with pytest.raises(RuntimeError, match="forced M10.8-B failure"):
        CanonicalV2Rebuilder(engine).rebuild(
            ROOT / "material", force_failure_stage="after_initialization"
        )
    with engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(dataset_snapshots).where(
                dataset_snapshots.c.status == "READY"
            )
        ) == 0
        assert connection.scalar(select(func.count()).select_from(source_records)) == 0
        assert connection.scalar(select(func.count()).select_from(v1_products)) > 0

    first = CanonicalV2Rebuilder(engine).rebuild(ROOT / "material")
    first_counts = _table_counts(engine)
    second = CanonicalV2Rebuilder(engine).rebuild(ROOT / "material")
    second_counts = _table_counts(engine)
    yield engine, first, second, first_counts, second_counts
    engine.dispose()


def test_clean_rebuild_counts_and_ready_gate(rebuilt) -> None:
    engine, first, _, _, _ = rebuilt
    assert first.status == "READY"
    expected = {
        "FinancialProduct": 35_180,
        "Bond": 20_497,
        "ETF": 7_206,
        "ETN": 610,
        "Fund": 6_867,
        "FundShareClass": 16_574,
        "SaleLot": 21_882,
    }
    assert {key: first.canonical_counts[key] for key in expected} == expected
    assert first.unresolved_rows == 7_102
    assert sum(item.source_rows for item in first.datasets) == 53_375
    assert sum(item.valid_rows for item in first.datasets) == 53_374
    assert sum(item.quarantined_rows for item in first.datasets) == 1
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(dataset_snapshots).where(dataset_snapshots.c.status == "READY")) == 4
        assert connection.scalar(select(func.count()).select_from(quarantine_records)) == 1


def test_entity_grains_names_and_parent_integrity(rebuilt) -> None:
    engine = rebuilt[0]
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(fund_share_classes).outerjoin(funds, fund_share_classes.c.parent_fund_id == funds.c.fund_id).where(funds.c.fund_id.is_(None))) == 0
        assert connection.scalar(select(func.count()).select_from(sale_lots).outerjoin(bonds, sale_lots.c.bond_id == bonds.c.bond_id).where(bonds.c.bond_id.is_(None))) == 0
        assert connection.scalar(select(func.count()).select_from(canonical_entities).join(funds, canonical_entities.c.entity_id == funds.c.fund_id).where(canonical_entities.c.preferred_name.is_not(None))) == 0
        assert connection.scalar(select(func.count()).select_from(canonical_entities).join(funds, canonical_entities.c.entity_id == funds.c.fund_id).where(canonical_entities.c.name_status != "NO_AUTHORITATIVE_FAMILY_NAME")) == 0
        unresolved_links = connection.scalar(
            select(func.count()).select_from(identity_resolution_cases)
            .join(source_record_entities, identity_resolution_cases.c.source_record_id == source_record_entities.c.source_record_id)
            .where(identity_resolution_cases.c.reason_code == "UNRESOLVED_PARENT")
        )
        assert unresolved_links == 0


def test_final_provenance_and_fact_evidence(rebuilt) -> None:
    engine, first, _, _, _ = rebuilt
    assert first.provenance_counts["SourceRecords"] == 53_374
    assert first.provenance_counts["DESCRIBES"] == 46_272
    assert first.provenance_counts["SUPPORTS"] == 38_456
    with engine.connect() as connection:
        duplicate_describes = connection.scalar(text(
            "SELECT count(*) FROM (SELECT source_record_id FROM canonical_v2.source_record_entities "
            "WHERE provenance_role='DESCRIBES' GROUP BY source_record_id HAVING count(*) > 1) q"
        ))
        evidence_free = connection.scalar(
            select(func.count()).select_from(canonical_facts)
            .outerjoin(fact_evidence_links, canonical_facts.c.fact_id == fact_evidence_links.c.fact_id)
            .where(canonical_facts.c.resolution_status == "RESOLVED", fact_evidence_links.c.fact_id.is_(None))
        )
        assert duplicate_describes == 0
        assert evidence_free == 0


def test_classification_conflicts_identifiers_and_composites(rebuilt) -> None:
    engine, first, _, _, _ = rebuilt
    assert first.identifier_counts["collision_cases"] > 0
    assert first.conflict_counts["fact_conflict_cases"] > 0
    for category in (
        "asset_class", "exposure_region", "market_scope", "risk_grade",
        "offering_type",
        "subscription_status",
    ):
        accounting = first.classification_accounting["PRFD01N001"][category]
        assert accounting.get("source", 0) + accounting.get("missing", 0) == 23_676
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(source_classification_values).where(source_classification_values.c.resolution_status == "UNRESOLVED")) > 0
        conflict_without_evidence = connection.scalar(
            select(func.count()).select_from(canonical_facts)
            .outerjoin(fact_evidence_links, canonical_facts.c.fact_id == fact_evidence_links.c.fact_id)
            .where(canonical_facts.c.resolution_status == "CONFLICT", fact_evidence_links.c.fact_id.is_(None))
        )
        assert conflict_without_evidence == 0
        assert connection.scalar(select(func.count()).select_from(source_field_assertions).where(source_field_assertions.c.target_semantic_key == "benchmark:COMPOSITE_UNRESOLVED")) > 0


def test_metric_numeric_date_and_safe_comparability(rebuilt) -> None:
    engine, first, _, _, _ = rebuilt
    # M10.9-C1 enables comparability only for observations backed by an
    # explicit source/grain-scoped contract (AUM, rating order, and exact 1Y
    # returns). Organizer purchasability is a lifecycle rule, not a metric.
    assert first.metric_status == {
        "COMPARABLE": 33_397,
        "NOT_COMPARABLE": 74_264,
    }
    assert first.metric_counts["ONE_YEAR_RETURN"] == 8_417
    assert "CURRENT_SALE_AVAILABILITY" not in first.metric_counts
    assert "BUYABLE_QUANTITY" not in first.metric_counts
    assert sum(first.metric_status.values()) == sum(first.metric_counts.values())
    with engine.connect() as connection:
        value = connection.scalar(select(metric_observations.c.numeric_value).where(metric_observations.c.numeric_value.is_not(None)).limit(1))
        assert isinstance(value, Decimal)
        assert connection.scalar(select(func.count()).select_from(bonds).where(bonds.c.maturity_date == date(9999, 12, 31))) == 0


def test_crosswalk_and_idempotent_second_run(rebuilt) -> None:
    engine, _, second, first_counts, second_counts = rebuilt
    assert second.status == "SKIPPED_UNCHANGED"
    assert second.skipped is True
    assert first_counts == second_counts
    with engine.connect() as connection:
        statuses = dict(connection.execute(
            select(entity_id_crosswalk.c.mapping_status, func.count())
            .group_by(entity_id_crosswalk.c.mapping_status)
        ).all())
        assert statuses["EXACT"] > 0
        assert statuses["RETIRED"] == 7_102


def test_semantic_relation_correction_and_safe_fund_promotion(rebuilt) -> None:
    engine, first, _, _, _ = rebuilt
    assert first.relation_counts["MANAGED_BY"] == 14_057
    assert first.relation_counts["HAS_TRUSTEE"] == 6_857
    assert first.relation_counts["HAS_BENCHMARK"] == 1_311
    # PREF01's authoritative CURR_CD_KRW code is now normalized to KRW.
    assert first.relation_counts["DENOMINATED_IN"] == 28_298

    with engine.connect() as connection:
        def product_relation_count(table, relation: str, product_type: str) -> int:
            subject_column = (
                table.c.subject_product_id
                if "subject_product_id" in table.c
                else table.c.subject_entity_id
            )
            return int(
                connection.scalar(
                    select(func.count())
                    .select_from(
                        table.join(
                            financial_products,
                            financial_products.c.product_id == subject_column,
                        )
                    )
                    .where(
                        table.c.relation_type == relation,
                        financial_products.c.product_type_code == product_type,
                    )
                )
                or 0
            )

        assert product_relation_count(organization_relations, "MANAGED_BY", "ETN") == 0
        assert product_relation_count(organization_relations, "ISSUED_BY", "ETN") == 0
        assert product_relation_count(organization_relations, "MANAGED_BY", "FUND") == 6_863
        assert product_relation_count(organization_relations, "HAS_TRUSTEE", "FUND") == 6_857
        assert product_relation_count(index_relations, "HAS_BENCHMARK", "FUND") == 1_311

        for relation in ("MANAGED_BY", "HAS_TRUSTEE", "HAS_BENCHMARK", "DENOMINATED_IN"):
            assert connection.scalar(
                select(func.count())
                .select_from(
                    entity_relations.join(
                        fund_share_classes,
                        fund_share_classes.c.fund_share_class_id
                        == entity_relations.c.subject_entity_id,
                    )
                )
                .where(entity_relations.c.relation_type == relation)
            ) == 0


def test_suppressed_relations_preserve_source_assertions(rebuilt) -> None:
    engine = rebuilt[0]
    with engine.connect() as connection:
        etn_manager_assertions = connection.scalar(
            select(func.count())
            .select_from(
                source_field_assertions
                .join(
                    source_records,
                    source_records.c.source_record_id
                    == source_field_assertions.c.source_record_id,
                )
                .join(
                    source_record_entities,
                    source_record_entities.c.source_record_id
                    == source_records.c.source_record_id,
                )
                .join(
                    exchange_traded_products,
                    exchange_traded_products.c.etp_id
                    == source_record_entities.c.entity_id,
                )
            )
            .where(
                source_field_assertions.c.source_column == "cu_fund_mgmt_co",
                source_record_entities.c.provenance_role == "DESCRIBES",
                exchange_traded_products.c.product_type_code == "ETN",
            )
        )
        assert etn_manager_assertions == 610

        for source_column in (
            "or_co_xtn_itt_cd",
            "trusc_xtn_itt_cd",
            "bmrk_nm",
            "curr_cd",
        ):
            assert connection.scalar(
                select(func.count())
                .select_from(
                    source_field_assertions
                    .join(
                        source_record_entities,
                        source_record_entities.c.source_record_id
                        == source_field_assertions.c.source_record_id,
                    )
                    .join(
                        fund_share_classes,
                        fund_share_classes.c.fund_share_class_id
                        == source_record_entities.c.entity_id,
                    )
                )
                .where(
                    source_field_assertions.c.source_column == source_column,
                    source_record_entities.c.provenance_role == "DESCRIBES",
                )
            ) > 0


def test_organization_target_rejection_is_explicit_and_evidenced(rebuilt) -> None:
    engine = rebuilt[0]
    rejected_value = "미래에셋증권글로벌헬스케어제14호(ETN)"
    with engine.connect() as connection:
        cases = connection.execute(
            select(
                identity_resolution_cases.c.resolution_status,
                identity_resolution_cases.c.raw_identity,
                identity_resolution_cases.c.source_record_id,
            ).where(
                identity_resolution_cases.c.reason_code
                == "INVALID_ORGANIZATION_TARGET"
            )
        ).all()
        assert len(cases) == 1
        assert cases[0].resolution_status == "REJECTED"
        assert cases[0].raw_identity["raw_value"] == rejected_value
        assert connection.scalar(
            select(func.count()).select_from(canonical_entities).where(
                canonical_entities.c.entity_kind == "ORGANIZATION",
                canonical_entities.c.preferred_name == rejected_value,
            )
        ) == 0
        assertion = connection.execute(
            select(
                source_field_assertions.c.quality_status,
                source_field_assertions.c.target_semantic_key,
                source_records.c.source_record_id,
            )
            .select_from(
                source_field_assertions.join(
                    source_records,
                    source_records.c.source_record_id
                    == source_field_assertions.c.source_record_id,
                )
            )
            .where(
                source_field_assertions.c.raw_value == rejected_value,
                source_field_assertions.c.source_column == "cu_fund_mgmt_co",
            )
        ).one()
        assert assertion.quality_status == "INVALID"
        assert assertion.target_semantic_key == "organization:INVALID_TARGET"
        assert assertion.source_record_id == cases[0].source_record_id
        assert connection.scalar(
            select(func.count()).select_from(identity_resolution_cases).where(
                identity_resolution_cases.c.reason_code
                == "UNSUPPORTED_RELATION_DOMAIN"
            )
        ) == 609


def test_conflicts_stay_unresolved_without_fund_relations(rebuilt) -> None:
    engine = rebuilt[0]
    expected = {
        "or_co_xtn_itt_cd": (4, "MANAGED_BY"),
        "trusc_xtn_itt_cd": (10, "HAS_TRUSTEE"),
        "bmrk_nm": (58, "HAS_BENCHMARK"),
        "prvo_pbff_desc": (3, "HAS_OFFERING_TYPE"),
        "or_attr_desc": (24, "HAS_ASSET_CLASS"),
        "fd_ivst_rgn_desc": (110, "HAS_EXPOSURE_REGION"),
        "ovrs_fd_desc": (27, "HAS_MARKET_SCOPE"),
    }
    with engine.connect() as connection:
        for semantic_key, (count, relation) in expected.items():
            subjects = select(fact_conflict_cases.c.subject_entity_id).where(
                fact_conflict_cases.c.semantic_key == semantic_key,
                fact_conflict_cases.c.status == "UNRESOLVED",
                fact_conflict_cases.c.winning_fact_id.is_(None),
            )
            assert connection.scalar(
                select(func.count()).select_from(subjects.subquery())
            ) == count
            if relation in {"MANAGED_BY", "HAS_TRUSTEE"}:
                asserted = connection.scalar(
                    select(func.count()).select_from(organization_relations).where(
                        organization_relations.c.relation_type == relation,
                        organization_relations.c.subject_product_id.in_(subjects),
                    )
                )
            elif relation == "HAS_BENCHMARK":
                asserted = connection.scalar(
                    select(func.count()).select_from(index_relations).where(
                        index_relations.c.relation_type == relation,
                        index_relations.c.subject_product_id.in_(subjects),
                    )
                )
            else:
                asserted = connection.scalar(
                    select(func.count()).select_from(entity_classifications).where(
                        ("HAS_" + entity_classifications.c.classification_type)
                        == relation,
                        entity_classifications.c.entity_id.in_(subjects),
                    )
                )
            assert asserted == 0


def test_relation_domain_ready_gate_and_relation_deduplication(rebuilt) -> None:
    engine = rebuilt[0]
    with engine.connect() as connection:
        assert relation_domain_violations(connection) == []
        subscription_grains = connection.execute(
            select(
                canonical_entities.c.entity_kind,
                ontology_concepts.c.concept_category,
                func.count(),
            )
            .select_from(
                entity_classifications
                .join(
                    canonical_entities,
                    canonical_entities.c.entity_id
                    == entity_classifications.c.entity_id,
                )
                .join(
                    ontology_concepts,
                    ontology_concepts.c.concept_iri
                    == entity_classifications.c.concept_iri,
                )
            )
            .where(
                entity_classifications.c.classification_type
                == "SUBSCRIPTION_STATUS"
            )
            .group_by(
                canonical_entities.c.entity_kind,
                ontology_concepts.c.concept_category,
            )
        ).all()
        assert subscription_grains == [
            ("FUND_SHARE_CLASS", "subscription_status", 16_574)
        ]
        duplicate_groups = connection.scalar(text(
            "WITH rel AS ("
            "SELECT cf.snapshot_id, er.subject_entity_id subject_id, er.relation_type, er.object_entity_id target_id "
            "FROM canonical_v2.entity_relations er JOIN canonical_v2.canonical_facts cf USING(fact_id) UNION ALL "
            "SELECT cf.snapshot_id, r.subject_product_id, r.relation_type, r.organization_id "
            "FROM canonical_v2.organization_relations r JOIN canonical_v2.canonical_facts cf USING(fact_id) UNION ALL "
            "SELECT cf.snapshot_id, r.subject_product_id, r.relation_type, r.index_id "
            "FROM canonical_v2.index_relations r JOIN canonical_v2.canonical_facts cf USING(fact_id)) "
            "SELECT count(*) FROM (SELECT snapshot_id,subject_id,relation_type,target_id "
            "FROM rel GROUP BY 1,2,3,4 HAVING count(*) > 1) q"
        ))
        assert duplicate_groups == 0

        connection.rollback()
        transaction = connection.begin()
        try:
            etn_id = connection.scalar(
                select(exchange_traded_products.c.etp_id)
                .where(exchange_traded_products.c.product_type_code == "ETN")
                .limit(1)
            )
            organization_id = connection.scalar(
                select(organizations.c.organization_id)
                .where(organizations.c.organization_type == "ASSET_MANAGER")
                .limit(1)
            )
            snapshot_id = connection.scalar(
                select(dataset_snapshots.c.snapshot_id)
                .where(dataset_snapshots.c.dataset_id == "PREF01N001")
                .limit(1)
            )
            assertion_id = connection.scalar(
                select(source_field_assertions.c.assertion_id).limit(1)
            )
            fact_id = "test:invalid-etn-managed-by"
            connection.execute(insert(canonical_facts).values(
                fact_id=fact_id,
                subject_entity_id=etn_id,
                snapshot_id=snapshot_id,
                fact_kind="ORGANIZATION_RELATION",
                semantic_key=f"MANAGED_BY:{organization_id}",
                resolution_status="RESOLVED",
            ))
            connection.execute(insert(organization_relations).values(
                fact_id=fact_id,
                subject_product_id=etn_id,
                relation_type="MANAGED_BY",
                organization_id=organization_id,
            ))
            connection.execute(insert(fact_evidence_links).values(
                fact_id=fact_id,
                assertion_id=assertion_id,
                evidence_role="SUPPORTS",
            ))
            violations = relation_domain_violations(connection)
            assert any(
                item.relation_type == "MANAGED_BY"
                and item.subject_product_type == "ETN"
                for item in violations
            )
            with pytest.raises(
                ValueError, match="canonical relation-domain validation failed"
            ):
                CanonicalV2Rebuilder(engine)._reconcile(connection)
        finally:
            transaction.rollback()

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, delete, distinct, func, insert, inspect, select, text
from sqlalchemy.engine import Engine, make_url

from app.data.ingest import FinancialDataIngestor
from app.data.schema import canonical_products as v1_products
from app.data.v2_rebuild import (
    CANONICAL_REBUILD_DATASET_IDS,
    CanonicalV2Rebuilder,
    EXPECTED_PREF01_RETURN_METRIC_COUNTS,
    EXPECTED_SOURCE_PROVENANCE_COUNTS,
    PREF01_RETURN_SOURCE_FIELDS,
    PRFD_MISSING_ASSERTION_FIELDS,
    TARGET_FIELDS,
    _Rows,
    _date,
    _RELATION_DOMAIN_CONTRACTS,
    _etp_insufficient_reasons,
    relation_domain_violations,
)
from app.data.cleaning import (
    PRBD_SALE_LOT_EVIDENCE_FIELDS,
    clean_source_row,
    has_prbd_sale_lot_evidence,
    normalized_date,
    source_assertion_semantics,
)
from app.data.catalog import DATASET_SPECS, discover_dataset_files
from app.data.mapping import map_product
from app.data.v2_schema import (
    CANONICAL_V2_SCHEMA,
    bonds,
    canonical_entities,
    canonical_facts,
    canonical_scalar_facts,
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
    source_datasets,
    source_field_assertions,
    source_record_entities,
    source_records,
)


pytestmark = pytest.mark.postgresql
ROOT = Path(__file__).resolve().parents[1]
VERSION_TABLE = "alembic_version_m10_8"


def _skip_if_organizer_material_is_fully_unprovisioned(root: Path) -> None:
    """Skip only when none of the four organizer datasets is provisioned."""

    matches: list[Path] = []
    for spec in DATASET_SPECS:
        matches.extend(root.rglob(f"{spec.prefix.lower()}_data.xlsx"))
        matches.extend(root.rglob(f"{spec.prefix.lower()}_schema.xlsx"))
        matches.extend(root.rglob(f"{spec.prefix}_*_datarows.xlsx"))
        matches.extend(root.rglob(f"{spec.prefix}_*_schema.xlsx"))
    if not matches:
        pytest.skip(
            "authoritative 2026-08-24 organizer material is not provisioned"
        )


def test_organizer_material_availability_skips_only_when_fully_absent(
    tmp_path: Path,
) -> None:
    with pytest.raises(pytest.skip.Exception):
        _skip_if_organizer_material_is_fully_unprovisioned(tmp_path)

    (tmp_path / "prbd01n001_data.xlsx").touch()
    _skip_if_organizer_material_is_fully_unprovisioned(tmp_path)
    with pytest.raises(FileNotFoundError):
        discover_dataset_files(tmp_path)


def test_subscription_status_relation_domain_contract_is_narrow() -> None:
    contract = _RELATION_DOMAIN_CONTRACTS["HAS_SUBSCRIPTION_STATUS"]
    assert contract.subject_grains == frozenset({("FUND_SHARE_CLASS", None)})
    assert contract.target_grains == frozenset(
        {("ONTOLOGY_CONCEPT", "subscription_status")}
    )
    assert ("FINANCIAL_PRODUCT", "BOND") not in contract.subject_grains
    assert ("ONTOLOGY_CONCEPT", "offering_type") not in contract.target_grains


def test_prbd_sale_lot_evidence_predicate_ignores_buyable_quantity() -> None:
    assert "buyable_quantity" not in PRBD_SALE_LOT_EVIDENCE_FIELDS
    assert not has_prbd_sale_lot_evidence({"buyable_quantity": 100})
    assert not has_prbd_sale_lot_evidence({"trade_price": "", "buy_yield": None})
    assert not has_prbd_sale_lot_evidence({"trade_price": float("nan")})
    assert not has_prbd_sale_lot_evidence({"trade_price": "NaN"})
    assert has_prbd_sale_lot_evidence({"trade_price": 0})
    assert has_prbd_sale_lot_evidence({"bdbns_abl_chnl_nm": "온오프 겸용"})


def test_trade_price_is_preserved_as_sale_lot_source_assertion() -> None:
    assert "trade_price" in TARGET_FIELDS["PRBD01N001"]


def test_pref01_return_metric_contract_uses_field_evidence_not_entity_support() -> None:
    assert set(PREF01_RETURN_SOURCE_FIELDS.values()).issubset(
        TARGET_FIELDS["PREF01N001"]
    )
    assert EXPECTED_PREF01_RETURN_METRIC_COUNTS == {
        "ONE_DAY_RETURN": 1_585,
        "ONE_MONTH_RETURN": 1_584,
        "THREE_MONTH_RETURN": 1_553,
        "SIX_MONTH_RETURN": 1_486,
        "ONE_YEAR_RETURN": 1_416,
        "YEAR_TO_DATE_RETURN": 1_477,
    }
    newly_added_assertions = sum(
        count
        for metric_code, count in EXPECTED_PREF01_RETURN_METRIC_COUNTS.items()
        if metric_code != "ONE_YEAR_RETURN"
    )
    assert newly_added_assertions == 7_685
    assert EXPECTED_SOURCE_PROVENANCE_COUNTS == {
        "source_records": 53_374,
        "quarantine_records": 1,
        "describes": 25_024,
        "supports": 38_456,
    }


def test_authoritative_organizer_source_baseline_is_unchanged() -> None:
    _skip_if_organizer_material_is_fully_unprovisioned(ROOT / "material")
    rebuilder = object.__new__(CanonicalV2Rebuilder)
    audit = rebuilder._audit(discover_dataset_files(ROOT / "material"))
    actual = {
        item.dataset: (
            item.source_rows,
            item.valid_rows,
            item.quarantined_rows,
        )
        for item in audit.datasets
    }
    assert actual == {
        "PRBD01N001": (21_882, 21_882, 0),
        "PREF01N001": (1_780, 1_779, 1),
        "PREF02N001": (6_037, 6_037, 0),
        "PRFD01N001": (23_676, 23_676, 0),
    }
    rebuilder._verify_source_baseline(audit)


def test_pref01_return_family_materializes_facts_and_field_evidence() -> None:
    rows = _Rows()
    assertions = {
        field_name: f"assertion:{field_name}"
        for field_name in PREF01_RETURN_SOURCE_FIELDS.values()
    }
    cleaned = {
        field_name: str(index)
        for index, field_name in enumerate(
            PREF01_RETURN_SOURCE_FIELDS.values(), start=1
        )
    }
    cleaned.update({"du_upt_dt": "20260824", "pd_curr_cd": "KRW"})

    rebuilder = object.__new__(CanonicalV2Rebuilder)
    rebuilder._metrics(
        rows,
        "PREF01N001",
        "etf:test",
        None,
        "snapshot:test",
        cleaned,
        assertions,
    )

    observations = rows._rows[metric_observations]
    facts = rows._rows[canonical_facts]
    evidence = rows._rows[fact_evidence_links]
    assert {row["metric_code"] for row in observations} == set(
        EXPECTED_PREF01_RETURN_METRIC_COUNTS
    )
    assert len(observations) == len(facts) == len(evidence) == 6
    assert {row["assertion_id"] for row in evidence} == set(assertions.values())
    assert all(row["evidence_role"] == "SUPPORTS" for row in evidence)
    assert rows._rows[source_record_entities] == []


@pytest.mark.parametrize("raw", [None, "", "   ", float("nan")])
def test_actual_missing_assertion_normalizes_to_null(raw) -> None:
    cleaned, changed = clean_source_row({"thco_sale_yn": raw})
    quality, normalized, _ = source_assertion_semantics(
        "PRFD01N001", "thco_sale_yn", raw, cleaned["thco_sale_yn"]
    )
    assert (quality, normalized) == ("MISSING", None)
    if isinstance(raw, str) and raw:
        assert changed["thco_sale_yn"] == raw


@pytest.mark.parametrize(
    ("dataset", "field", "raw"),
    [
        ("PRBD01N001", "isu_dt", "00000000"),
        ("PRBD01N001", "mat_dt", "00000000"),
        ("PREF01N001", "pd_lste_dt", "99991231"),
        ("PREF01N001", "pd_lstg_dt", "10001231"),
        ("PREF02N001", "pd_lstg_dt", "00000000"),
    ],
)
def test_known_date_sentinel_has_no_canonical_date(
    dataset: str, field: str, raw: str
) -> None:
    quality, normalized, _ = source_assertion_semantics(
        dataset, field, raw, raw
    )
    assert (quality, normalized) == ("SENTINEL", None)
    assert _date(raw) is None


def test_valid_and_malformed_dates_fail_closed_consistently() -> None:
    assert normalized_date("20260821") == ("2026-08-21", None)
    assert _date("20260821") == date(2026, 8, 21)
    assert normalized_date("20261340") == (None, "INVALID_DATE")
    quality, normalized, _ = source_assertion_semantics(
        "PREF01N001", "pd_lstg_dt", "20261340", "20261340"
    )
    assert (quality, normalized) == ("INVALID", None)


@pytest.mark.parametrize(
    ("raw", "quality"),
    [
        (None, "MISSING"),
        ("KR0000000000", "SENTINEL"),
        ("000000000000", "SENTINEL"),
        ("kr0000000000", "INVALID"),
        ("wtrewrwe", "INVALID"),
        ("031910490159", "VALID"),
    ],
)
def test_representative_fund_id_assertion_quality(raw, quality: str) -> None:
    cleaned, _ = clean_source_row({"rptt_ksd_itm_no": raw})
    actual, normalized, _ = source_assertion_semantics(
        "PRFD01N001", "rptt_ksd_itm_no", raw,
        cleaned["rptt_ksd_itm_no"],
    )
    assert actual == quality
    assert (normalized is not None) == (quality == "VALID")


@pytest.mark.parametrize(
    ("raw", "quality"),
    [
        ("Index is not provided by Management Company", "SOURCE_NOT_PROVIDED"),
        ("Index is not available on Lipper Database", "VENDOR_NOT_AVAILABLE"),
        (None, "MISSING"),
        ("MSCI ACWI", "VALID"),
    ],
)
def test_foreign_index_placeholder_reasons_are_distinct(
    raw, quality: str
) -> None:
    actual, normalized, _ = source_assertion_semantics(
        "PREF02N001", "cu_base_index", raw, raw
    )
    assert actual == quality
    assert (normalized is not None) == (quality == "VALID")


@pytest.mark.parametrize(
    ("raw_parent", "expected_raw", "expected_quality"),
    [
        (None, None, "MISSING"),
        ("   ", "   ", "MISSING"),
        ("KR0000000000", "KR0000000000", "SENTINEL"),
        ("000000000000", "000000000000", "SENTINEL"),
        ("wtrewrwe", "wtrewrwe", "INVALID"),
    ],
)
def test_unresolved_parent_assertion_preserves_missing_and_raw_invalid_values(
    raw_parent: str | None,
    expected_raw: str | None,
    expected_quality: str,
) -> None:
    raw = {
        "itm_no": "OS555085028M",
        "itm_nm": "테스트 공모펀드",
        "prvo_pbff_desc": "공모",
        "rptt_ksd_itm_no": raw_parent,
        "thco_sale_yn": None,
    }
    cleaned, _ = clean_source_row(raw)
    rows = _Rows()
    assertions = CanonicalV2Rebuilder(None)._assertions(
        rows, "PRFD01N001", "source:test", raw, cleaned
    )
    assertion = next(
        item
        for item in rows._rows[source_field_assertions]
        if item["source_column"] == "rptt_ksd_itm_no"
    )
    public_fund_spec = next(
        spec for spec in DATASET_SPECS if spec.prefix == "PRFD01N001"
    )
    mapped, error = map_product(
        public_fund_spec,
        cleaned,
        source_file="prfd01n001_data.xlsx",
        source_row_number=2,
        snapshot="2026-08-24",
    )

    assert PRFD_MISSING_ASSERTION_FIELDS == frozenset(
        {"rptt_ksd_itm_no", "thco_sale_yn"}
    )
    assert assertions["rptt_ksd_itm_no"] == assertion["assertion_id"]
    assert assertion["raw_value"] == expected_raw
    expected_normalized = (
        cleaned["rptt_ksd_itm_no"]
        if expected_quality == "VALID"
        else None
    )
    assert assertion["normalized_value"] == expected_normalized
    assert assertion["quality_status"] == expected_quality
    assert error is None
    assert mapped is not None and mapped.fund is None
    assert mapped.fund_class is None


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
        "SaleLot": 634,
    }
    assert {key: first.canonical_counts[key] for key in expected} == expected
    assert first.unresolved_rows == 7_102
    assert sum(item.source_rows for item in first.datasets) == 53_375
    assert sum(item.valid_rows for item in first.datasets) == 53_374
    assert sum(item.quarantined_rows for item in first.datasets) == 1
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(dataset_snapshots).where(dataset_snapshots.c.status == "READY")) == 4
        assert connection.scalar(select(func.count()).select_from(quarantine_records)) == 1
        assert "buyable_quantity" not in PRBD_SALE_LOT_EVIDENCE_FIELDS


def test_unresolved_parent_missing_evidence_and_reconciliation(rebuilt) -> None:
    engine = rebuilt[0]
    with engine.connect() as connection:
        counts = connection.execute(text("""
            WITH unresolved AS (
                SELECT irc.source_record_id, sr.normalized_payload
                FROM canonical_v2.identity_resolution_cases irc
                JOIN canonical_v2.source_records sr
                  ON sr.source_record_id = irc.source_record_id
                JOIN canonical_v2.dataset_snapshots ds
                  ON ds.snapshot_id = sr.snapshot_id
                WHERE ds.dataset_id = 'PRFD01N001'
                  AND irc.reason_code = 'UNRESOLVED_PARENT'
            ), parent_assertions AS (
                SELECT sfa.source_record_id, sfa.raw_value,
                       sfa.normalized_value, sfa.quality_status
                FROM canonical_v2.source_field_assertions sfa
                JOIN unresolved u ON u.source_record_id = sfa.source_record_id
                WHERE sfa.source_column = 'rptt_ksd_itm_no'
            )
            SELECT
                (SELECT count(*) FROM unresolved) AS unresolved_total,
                (SELECT count(*) FROM parent_assertions
                 WHERE quality_status = 'MISSING'
                   AND btrim(coalesce(raw_value, '')) = ''
                   AND normalized_value IS NULL) AS actual_null_or_blank,
                (SELECT count(*) FROM parent_assertions
                 WHERE raw_value = 'KR0000000000'
                   AND normalized_value IS NULL
                   AND quality_status = 'SENTINEL') AS kr_sentinel,
                (SELECT count(*) FROM parent_assertions
                 WHERE raw_value = '000000000000'
                   AND normalized_value IS NULL
                   AND quality_status = 'SENTINEL') AS zero_sentinel,
                (SELECT count(*) FROM parent_assertions
                 WHERE raw_value IS NOT NULL
                   AND raw_value NOT IN ('KR0000000000', '000000000000')
                   AND quality_status = 'INVALID') AS malformed,
                (SELECT count(*) FROM unresolved
                 WHERE normalized_payload ->> 'prvo_pbff_desc' = '공모'
                   AND normalized_payload ->> 'sale_yn' = '판매중') AS public_open_unresolved,
                (SELECT count(*) FROM unresolved
                 WHERE normalized_payload ->> 'prvo_pbff_desc' = '공모'
                   AND normalized_payload ->> 'sale_yn' = '판매중'
                   AND normalized_payload ->> 'thco_sale_yn' = 'Y') AS strict_unresolved
        """)).one()._mapping
        assert dict(counts) == {
            "unresolved_total": 7_102,
            "actual_null_or_blank": 120,
            "kr_sentinel": 5_308,
            "zero_sentinel": 1_645,
            "malformed": 29,
            "public_open_unresolved": 110,
            "strict_unresolved": 0,
        }

        source_counts = connection.execute(text("""
            SELECT
                count(*) AS source_rows,
                count(*) FILTER (
                    WHERE sr.normalized_payload ->> 'prvo_pbff_desc' = '공모'
                      AND sr.normalized_payload ->> 'sale_yn' = '판매중'
                ) AS raw_public_open,
                count(*) FILTER (
                    WHERE sr.normalized_payload ->> 'prvo_pbff_desc' = '공모'
                      AND sr.normalized_payload ->> 'sale_yn' = '판매중'
                      AND sr.normalized_payload ->> 'thco_sale_yn' = 'Y'
                ) AS raw_strict
            FROM canonical_v2.source_records sr
            JOIN canonical_v2.dataset_snapshots ds
              ON ds.snapshot_id = sr.snapshot_id
            WHERE ds.dataset_id = 'PRFD01N001'
        """)).one()._mapping
        assert dict(source_counts) == {
            "source_rows": 23_676,
            "raw_public_open": 8_969,
            "raw_strict": 8_550,
        }
        assert source_counts.raw_public_open == 8_859 + counts.public_open_unresolved
        assert source_counts.raw_strict == 8_550 + counts.strict_unresolved

        orphan_classes = connection.scalar(
            select(func.count())
            .select_from(
                fund_share_classes.outerjoin(
                    funds,
                    fund_share_classes.c.parent_fund_id == funds.c.fund_id,
                )
            )
            .where(funds.c.fund_id.is_(None))
        )
        false_company_sale_facts = connection.scalar(
            select(func.count())
            .select_from(
                canonical_facts.join(
                    canonical_scalar_facts,
                    canonical_scalar_facts.c.fact_id == canonical_facts.c.fact_id,
                )
            )
            .where(
                canonical_facts.c.semantic_key == "is_sold_by_mirae_asset",
                canonical_scalar_facts.c.boolean_value.is_(False),
            )
        )
        assert orphan_classes == 0
        assert false_company_sale_facts == 0


def test_entity_grains_names_and_parent_integrity(rebuilt) -> None:
    engine = rebuilt[0]
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(fund_share_classes).outerjoin(funds, fund_share_classes.c.parent_fund_id == funds.c.fund_id).where(funds.c.fund_id.is_(None))) == 0
        assert connection.scalar(select(func.count()).select_from(sale_lots).outerjoin(bonds, sale_lots.c.bond_id == bonds.c.bond_id).where(bonds.c.bond_id.is_(None))) == 0
        sale_lot_counts = dict(connection.execute(text(
            "SELECT lot_count, count(*) FROM ("
            "SELECT b.bond_id, count(sl.sale_lot_id) lot_count "
            "FROM canonical_v2.bonds b LEFT JOIN canonical_v2.sale_lots sl "
            "ON sl.bond_id = b.bond_id GROUP BY b.bond_id) q GROUP BY lot_count"
        )).all())
        assert sale_lot_counts[0] == 20_171
        assert sale_lot_counts[1] == 19
        assert sum(count for lot_count, count in sale_lot_counts.items() if lot_count > 1) == 307
        assert connection.scalar(select(func.count(distinct(sale_lots.c.bond_id)))) == 326
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
    assert first.provenance_counts["SourceRecords"] == (
        EXPECTED_SOURCE_PROVENANCE_COUNTS["source_records"]
    )
    assert first.provenance_counts["DESCRIBES"] == (
        EXPECTED_SOURCE_PROVENANCE_COUNTS["describes"]
    )
    assert first.provenance_counts["SUPPORTS"] == (
        EXPECTED_SOURCE_PROVENANCE_COUNTS["supports"]
    )
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
        no_lot_prbd_sources = connection.scalar(text(
            "SELECT count(*) FROM canonical_v2.source_records sr "
            "JOIN canonical_v2.dataset_snapshots ds "
            "ON ds.snapshot_id = sr.snapshot_id "
            "WHERE ds.dataset_id = 'PRBD01N001' "
            "AND NOT EXISTS ("
            "SELECT 1 FROM canonical_v2.source_record_entities sre "
            "WHERE sre.source_record_id = sr.source_record_id "
            "AND sre.provenance_role = 'DESCRIBES')"
        ))
        no_lot_prbd_supports = connection.scalar(text(
            "SELECT count(*) FROM canonical_v2.source_records sr "
            "JOIN canonical_v2.dataset_snapshots ds "
            "ON ds.snapshot_id = sr.snapshot_id "
            "JOIN canonical_v2.source_record_entities sre "
            "ON sre.source_record_id = sr.source_record_id "
            "WHERE ds.dataset_id = 'PRBD01N001' "
            "AND sre.provenance_role = 'SUPPORTS' "
            "AND sre.entity_kind = 'FINANCIAL_PRODUCT' "
            "AND NOT EXISTS ("
            "SELECT 1 FROM canonical_v2.source_record_entities described "
            "WHERE described.source_record_id = sr.source_record_id "
            "AND described.provenance_role = 'DESCRIBES')"
        ))
        assert no_lot_prbd_sources == 21_248
        assert no_lot_prbd_supports == 21_248


def test_external_activation_provenance_does_not_pollute_rebuild_scope(
    rebuilt,
) -> None:
    engine = rebuilt[0]
    dataset_id = "dataset:test-external-provenance"
    snapshot_id = "snapshot:test-external-provenance:20260824"
    described_record = "source:test-external-described"
    supported_record = "source:test-external-supported"

    with engine.begin() as connection:
        entity_id = connection.scalar(
            select(exchange_traded_products.c.etp_id).limit(1)
        )
        assert entity_id is not None
        connection.execute(
            insert(source_datasets).values(
                dataset_id=dataset_id,
                dataset_code="TEST_EXTERNAL_PROVENANCE",
                display_name="Test external activation provenance",
                source_system="isolated regression fixture",
                schema_contract_version="test-external-v1",
                is_authoritative=True,
            )
        )
        connection.execute(
            insert(dataset_snapshots).values(
                snapshot_id=snapshot_id,
                dataset_id=dataset_id,
                snapshot_date=date(2026, 8, 24),
                generation="external",
                ontology_version="test",
                semantic_mapping_version="test",
                transformer_version="test",
                database_schema_version="test",
                data_sha256="a" * 64,
                schema_sha256="b" * 64,
                source_row_count=2,
                accepted_row_count=2,
                quarantined_row_count=0,
                status="READY",
                reconciliation_status="PASSED",
                row_count_reconciled=True,
                metadata_json={"scope": "external-regression"},
            )
        )
        connection.execute(
            insert(source_records),
            [
                {
                    "source_record_id": described_record,
                    "snapshot_id": snapshot_id,
                    "source_primary_key": "external-1",
                    "source_row_number": 1,
                    "raw_payload": {"external": 1},
                    "normalized_payload": {"external": 1},
                    "payload_sha256": "c" * 64,
                    "quality_status": "VALID",
                },
                {
                    "source_record_id": supported_record,
                    "snapshot_id": snapshot_id,
                    "source_primary_key": "external-2",
                    "source_row_number": 2,
                    "raw_payload": {"external": 2},
                    "normalized_payload": {"external": 2},
                    "payload_sha256": "d" * 64,
                    "quality_status": "VALID",
                },
            ],
        )
        connection.execute(
            insert(source_record_entities),
            [
                {
                    "source_record_id": described_record,
                    "entity_id": entity_id,
                    "entity_kind": "FINANCIAL_PRODUCT",
                    "provenance_role": "DESCRIBES",
                },
                {
                    "source_record_id": supported_record,
                    "entity_id": entity_id,
                    "entity_kind": "FINANCIAL_PRODUCT",
                    "provenance_role": "SUPPORTS",
                },
            ],
        )

    try:
        with engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(source_records)) == (
                EXPECTED_SOURCE_PROVENANCE_COUNTS["source_records"] + 2
            )
            assert connection.scalar(
                select(func.count())
                .select_from(source_record_entities)
                .where(source_record_entities.c.provenance_role == "DESCRIBES")
            ) == EXPECTED_SOURCE_PROVENANCE_COUNTS["describes"] + 1
            assert CanonicalV2Rebuilder(engine)._reconcile(connection) == {
                **{
                    "financial_products": 35_180,
                    "bonds": 20_497,
                    "funds": 6_867,
                    "fund_share_classes": 16_574,
                    "etf": 7_206,
                    "etn": 610,
                    "unresolved_fund_rows": 7_102,
                },
                "sale_lots": 634,
            }

        report = CanonicalV2Rebuilder(engine).rebuild(ROOT / "material")
        assert report.status == "SKIPPED_UNCHANGED"
        assert report.skipped is True
        assert report.provenance_counts["SourceRecords"] == 53_374
        assert report.provenance_counts["DESCRIBES"] == 25_024
        assert report.provenance_counts["SUPPORTS"] == 38_456
        assert set(CANONICAL_REBUILD_DATASET_IDS) == {
            "PRBD01N001",
            "PREF01N001",
            "PREF02N001",
            "PRFD01N001",
        }
    finally:
        with engine.begin() as connection:
            connection.execute(
                delete(source_record_entities).where(
                    source_record_entities.c.source_record_id.in_(
                        (described_record, supported_record)
                    )
                )
            )
            connection.execute(
                delete(source_records).where(
                    source_records.c.snapshot_id == snapshot_id
                )
            )
            connection.execute(
                delete(dataset_snapshots).where(
                    dataset_snapshots.c.snapshot_id == snapshot_id
                )
            )
            connection.execute(
                delete(source_datasets).where(
                    source_datasets.c.dataset_id == dataset_id
                )
            )


def test_missing_owned_source_provenance_remains_fail_closed(rebuilt) -> None:
    engine = rebuilt[0]
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            source_record_id = connection.scalar(
                select(source_record_entities.c.source_record_id)
                .join(
                    source_records,
                    source_records.c.source_record_id
                    == source_record_entities.c.source_record_id,
                )
                .join(
                    dataset_snapshots,
                    dataset_snapshots.c.snapshot_id == source_records.c.snapshot_id,
                )
                .where(
                    dataset_snapshots.c.dataset_id.in_(
                        CANONICAL_REBUILD_DATASET_IDS
                    ),
                    source_record_entities.c.provenance_role == "SUPPORTS",
                )
                .limit(1)
            )
            assert source_record_id is not None
            connection.execute(
                delete(source_record_entities).where(
                    source_record_entities.c.source_record_id == source_record_id,
                    source_record_entities.c.provenance_role == "SUPPORTS",
                )
            )
            with pytest.raises(
                ValueError, match="source/provenance reconciliation mismatch"
            ) as exc_info:
                CanonicalV2Rebuilder(engine)._reconcile(connection)
            message = str(exc_info.value)
            assert "actual=" in message
            assert "expected=" in message
            assert "scope=dataset_ids:" in message
        finally:
            transaction.rollback()


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
    # explicit source/grain-scoped contract (AUM, rating order, and exact
    # period returns). Organizer purchasability is a lifecycle rule, not a metric.
    assert first.metric_status == {
        "COMPARABLE": 40_757,
        # Fourteen foreign rows have neither source price nor source volume;
        # absence is preserved instead of fabricating metric observations.
        # Return observations lacking du_upt_dt are retained but not rankable.
        "NOT_COMPARABLE": 90_167,
    }
    assert first.metric_counts["MARKET_PRICE"] == 7_799
    assert first.metric_counts["VOLUME"] == 7_799
    assert first.metric_counts["ONE_YEAR_RETURN"] == 8_417
    assert first.metric_counts["ONE_DAY_RETURN"] == 1_585
    assert first.metric_counts["ONE_MONTH_RETURN"] == 1_584
    assert first.metric_counts["THREE_MONTH_RETURN"] == 1_553
    assert first.metric_counts["SIX_MONTH_RETURN"] == 1_486
    assert first.metric_counts["YEAR_TO_DATE_RETURN"] == 1_477
    assert "CURRENT_SALE_AVAILABILITY" not in first.metric_counts
    assert "BUYABLE_QUANTITY" not in first.metric_counts
    assert sum(first.metric_status.values()) == sum(first.metric_counts.values())
    with engine.connect() as connection:
        return_metric_codes = tuple(EXPECTED_PREF01_RETURN_METRIC_COUNTS)
        pref01_return_counts = dict(
            connection.execute(
                select(metric_observations.c.metric_code, func.count())
                .select_from(
                    metric_observations
                    .join(
                        canonical_facts,
                        canonical_facts.c.fact_id == metric_observations.c.fact_id,
                    )
                    .join(
                        dataset_snapshots,
                        dataset_snapshots.c.snapshot_id
                        == canonical_facts.c.snapshot_id,
                    )
                )
                .where(
                    dataset_snapshots.c.dataset_id == "PREF01N001",
                    metric_observations.c.metric_code.in_(return_metric_codes),
                )
                .group_by(metric_observations.c.metric_code)
            )
        )
        assert pref01_return_counts == EXPECTED_PREF01_RETURN_METRIC_COUNTS

        evidence_counts = {
            (metric_code, source_column): count
            for metric_code, source_column, count in connection.execute(
                select(
                    metric_observations.c.metric_code,
                    source_field_assertions.c.source_column,
                    func.count(func.distinct(metric_observations.c.fact_id)),
                )
                .select_from(
                    metric_observations
                    .join(
                        canonical_facts,
                        canonical_facts.c.fact_id == metric_observations.c.fact_id,
                    )
                    .join(
                        dataset_snapshots,
                        dataset_snapshots.c.snapshot_id
                        == canonical_facts.c.snapshot_id,
                    )
                    .join(
                        fact_evidence_links,
                        fact_evidence_links.c.fact_id
                        == metric_observations.c.fact_id,
                    )
                    .join(
                        source_field_assertions,
                        source_field_assertions.c.assertion_id
                        == fact_evidence_links.c.assertion_id,
                    )
                )
                .where(
                    dataset_snapshots.c.dataset_id == "PREF01N001",
                    metric_observations.c.metric_code.in_(return_metric_codes),
                    fact_evidence_links.c.evidence_role == "SUPPORTS",
                )
                .group_by(
                    metric_observations.c.metric_code,
                    source_field_assertions.c.source_column,
                )
            )
        }
        assert evidence_counts == {
            (metric_code, PREF01_RETURN_SOURCE_FIELDS[metric_code]): count
            for metric_code, count in EXPECTED_PREF01_RETURN_METRIC_COUNTS.items()
        }
        value = connection.scalar(select(metric_observations.c.numeric_value).where(metric_observations.c.numeric_value.is_not(None)).limit(1))
        assert isinstance(value, Decimal)
        assert connection.scalar(select(func.count()).select_from(bonds).where(bonds.c.maturity_date == date(9999, 12, 31))) == 0


def test_missing_return_metric_evidence_still_fails_closed(rebuilt) -> None:
    engine = rebuilt[0]
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            fact_id = connection.scalar(
                select(metric_observations.c.fact_id)
                .select_from(
                    metric_observations.join(
                        canonical_facts,
                        canonical_facts.c.fact_id == metric_observations.c.fact_id,
                    ).join(
                        dataset_snapshots,
                        dataset_snapshots.c.snapshot_id
                        == canonical_facts.c.snapshot_id,
                    )
                )
                .where(
                    dataset_snapshots.c.dataset_id == "PREF01N001",
                    metric_observations.c.metric_code == "ONE_DAY_RETURN",
                )
                .limit(1)
            )
            assert fact_id is not None
            connection.execute(
                delete(fact_evidence_links).where(
                    fact_evidence_links.c.fact_id == fact_id
                )
            )
            with pytest.raises(
                ValueError,
                match="PREF01 return metric provenance reconciliation mismatch",
            ):
                CanonicalV2Rebuilder(engine)._reconcile(connection)
        finally:
            transaction.rollback()


def test_etp_availability_policy_counts_and_sentinels(rebuilt) -> None:
    engine = rebuilt[0]

    def scalar_boolean_count(connection, key: str, dataset: str, product_type: str | None = None) -> int:
        statement = (
            select(func.count(distinct(canonical_facts.c.subject_entity_id)))
            .select_from(
                canonical_facts
                .join(canonical_scalar_facts, canonical_scalar_facts.c.fact_id == canonical_facts.c.fact_id)
                .join(dataset_snapshots, dataset_snapshots.c.snapshot_id == canonical_facts.c.snapshot_id)
                .join(financial_products, financial_products.c.product_id == canonical_facts.c.subject_entity_id)
            )
            .where(
                dataset_snapshots.c.dataset_id == dataset,
                canonical_facts.c.semantic_key == key,
                canonical_scalar_facts.c.value_type == "BOOLEAN",
                canonical_scalar_facts.c.boolean_value.is_(True),
            )
        )
        if product_type is not None:
            statement = statement.where(financial_products.c.product_type_code == product_type)
        return int(connection.scalar(statement) or 0)

    def strict_count(connection, dataset: str, product_type: str | None = None) -> int:
        current = canonical_facts.alias("current_fact")
        current_scalar = canonical_scalar_facts.alias("current_scalar")
        latest = canonical_facts.alias("latest_fact")
        latest_scalar = canonical_scalar_facts.alias("latest_scalar")
        statement = (
            select(func.count(distinct(current.c.subject_entity_id)))
            .select_from(
                current
                .join(current_scalar, current_scalar.c.fact_id == current.c.fact_id)
                .join(latest, latest.c.subject_entity_id == current.c.subject_entity_id)
                .join(latest_scalar, latest_scalar.c.fact_id == latest.c.fact_id)
                .join(dataset_snapshots, dataset_snapshots.c.snapshot_id == current.c.snapshot_id)
                .join(financial_products, financial_products.c.product_id == current.c.subject_entity_id)
            )
            .where(
                dataset_snapshots.c.dataset_id == dataset,
                current.c.snapshot_id == latest.c.snapshot_id,
                current.c.semantic_key == "current_etp_sale_eligible",
                latest.c.semantic_key == "latest_etp_price_available",
                current_scalar.c.boolean_value.is_(True),
                latest_scalar.c.boolean_value.is_(True),
            )
        )
        if product_type is not None:
            statement = statement.where(financial_products.c.product_type_code == product_type)
        return int(connection.scalar(statement) or 0)

    with engine.connect() as connection:
        assert scalar_boolean_count(connection, "current_etp_sale_eligible", "PREF01N001", "ETF") == 1_160
        assert scalar_boolean_count(connection, "current_etp_sale_eligible", "PREF01N001", "ETN") == 373
        assert strict_count(connection, "PREF01N001") == 1_533
        assert scalar_boolean_count(connection, "current_etp_sale_eligible", "PREF02N001", "ETF") == 5_958
        assert scalar_boolean_count(connection, "current_etp_sale_eligible", "PREF02N001", "ETN") == 65
        assert strict_count(connection, "PREF02N001", "ETF") == 5_629
        assert strict_count(connection, "PREF02N001", "ETN") == 58
        assert scalar_boolean_count(connection, "etp_insufficient_info", "PREF01N001") == 3
        assert scalar_boolean_count(connection, "etp_insufficient_info", "PREF02N001") == 14
        assert scalar_boolean_count(connection, "stale_etp_price_warning", "PREF02N001") == 336
        assert connection.scalar(
            select(func.count())
            .select_from(exchange_traded_products)
            .where(
                exchange_traded_products.c.listing_date == date(1000, 12, 31)
            )
        ) == 0
        assert connection.scalar(
            select(func.count())
            .select_from(exchange_traded_products)
            .where(
                exchange_traded_products.c.delisting_date == date(9999, 12, 31)
            )
        ) == 0
        assert connection.scalar(
            select(func.count())
            .select_from(
                canonical_facts.join(
                    canonical_scalar_facts,
                    canonical_scalar_facts.c.fact_id == canonical_facts.c.fact_id,
                )
            )
            .where(
                canonical_facts.c.semantic_key == "listing_end_date_status",
                canonical_scalar_facts.c.text_value == "NO_KNOWN_END_DATE",
            )
        ) == 1_535
        assert connection.scalar(
            select(func.count())
            .select_from(
                metric_observations
                .join(canonical_facts, canonical_facts.c.fact_id == metric_observations.c.fact_id)
                .join(dataset_snapshots, dataset_snapshots.c.snapshot_id == canonical_facts.c.snapshot_id)
            )
            .where(
                dataset_snapshots.c.dataset_id == "PREF02N001",
                metric_observations.c.metric_code == "VOLUME",
                metric_observations.c.numeric_value == 0,
                metric_observations.c.quality_status == "VALID",
            )
        ) == 91
        insufficient_payloads = connection.execute(
            select(source_records.c.raw_payload)
            .select_from(
                canonical_facts
                .join(
                    canonical_scalar_facts,
                    canonical_scalar_facts.c.fact_id == canonical_facts.c.fact_id,
                )
                .join(
                    source_record_entities,
                    source_record_entities.c.entity_id == canonical_facts.c.subject_entity_id,
                )
                .join(
                    source_records,
                    source_records.c.source_record_id == source_record_entities.c.source_record_id,
                )
            )
            .where(
                canonical_facts.c.semantic_key == "etp_insufficient_info",
                canonical_scalar_facts.c.boolean_value.is_(True),
                source_records.c.snapshot_id == canonical_facts.c.snapshot_id,
            )
        ).scalars().all()
        assert len(insufficient_payloads) == 17
        assert all(_etp_insufficient_reasons(payload) for payload in insufficient_payloads)


def test_missingness_quality_assertion_cardinality(rebuilt) -> None:
    engine = rebuilt[0]
    sale_fields = sorted(PRBD_SALE_LOT_EVIDENCE_FIELDS)
    with engine.connect() as connection:
        sale_counts = dict(connection.execute(text("""
            SELECT source_column, count(*)
            FROM canonical_v2.source_field_assertions
            WHERE source_column = ANY(:fields)
              AND quality_status <> 'MISSING'
            GROUP BY source_column
        """), {"fields": sale_fields}).all())
        assert sale_counts == {field: 634 for field in sale_fields}
        assert connection.scalar(text("""
            SELECT count(*) FROM canonical_v2.source_field_assertions
            WHERE source_column = ANY(:fields) AND quality_status = 'MISSING'
        """), {"fields": sale_fields}) == 0

        expected = {
            ("PRBD01N001", "isu_dt", "SENTINEL"): 25,
            ("PRBD01N001", "mat_dt", "SENTINEL"): 4,
            ("PREF01N001", "pd_lste_dt", "SENTINEL"): 1_535,
            ("PREF01N001", "pd_lstg_dt", "SENTINEL"): 1,
            ("PREF02N001", "pd_lstg_dt", "SENTINEL"): 11,
            ("PREF02N001", "cu_base_index", "SOURCE_NOT_PROVIDED"): 2_285,
            ("PREF02N001", "cu_base_index", "VENDOR_NOT_AVAILABLE"): 635,
            ("PREF02N001", "cu_base_index", "MISSING"): 11,
            ("PRFD01N001", "rptt_ksd_itm_no", "MISSING"): 120,
            ("PRFD01N001", "rptt_ksd_itm_no", "SENTINEL"): 6_953,
            ("PRFD01N001", "rptt_ksd_itm_no", "INVALID"): 29,
            ("PRFD01N001", "thco_sale_yn", "MISSING"): 13_079,
            ("PRBD01N001", "buyable_quantity", "UNUSABLE_BY_POLICY"): 634,
        }
        result = connection.execute(text("""
            SELECT ds.dataset_id, sfa.source_column, sfa.quality_status,
                   count(*)
            FROM canonical_v2.source_field_assertions sfa
            JOIN canonical_v2.source_records sr USING (source_record_id)
            JOIN canonical_v2.dataset_snapshots ds USING (snapshot_id)
            WHERE (sfa.source_column, sfa.quality_status) IN (
                ('isu_dt', 'SENTINEL'), ('mat_dt', 'SENTINEL'),
                ('pd_lste_dt', 'SENTINEL'), ('pd_lstg_dt', 'SENTINEL'),
                ('cu_base_index', 'SOURCE_NOT_PROVIDED'),
                ('cu_base_index', 'VENDOR_NOT_AVAILABLE'),
                ('cu_base_index', 'MISSING'),
                ('rptt_ksd_itm_no', 'MISSING'),
                ('rptt_ksd_itm_no', 'SENTINEL'),
                ('rptt_ksd_itm_no', 'INVALID'),
                ('thco_sale_yn', 'MISSING'),
                ('buyable_quantity', 'UNUSABLE_BY_POLICY')
            )
            GROUP BY ds.dataset_id, sfa.source_column, sfa.quality_status
        """))
        actual = {(row[0], row[1], row[2]): row[3] for row in result}
        assert actual == expected


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
    assert first.relation_counts["HAS_SALE_LOT"] == 634
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

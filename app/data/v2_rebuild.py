"""M10.8-B clean rebuild of the 2026-08-24 source generation.

This module writes only ``canonical_v2``.  It deliberately reuses audited
workbook cleaning and identity rules, never v1 canonical rows, for entity
materialization.  v1 is consulted only for the optional identity crosswalk.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable

from sqlalchemy import Engine, String, func, inspect, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.data.catalog import DatasetFiles, discover_dataset_files
from app.data.cleaning import (
    canonical_mirae_sale_flag,
    clean_source_row,
    has_prbd_sale_lot_evidence,
    json_value,
    normalize_lookup_value,
)
from app.data.database import DatabaseSettings, create_database_engine
from app.data.loader import iter_source_rows, load_source_schema
from app.data.mapping import MappedProduct, map_product
from app.data.product_validation import validate_product_row
from app.data.schema import canonical_products as v1_products
from app.data.schema import funds as v1_funds
from app.data.v2_schema import (
    CANONICAL_V2_SCHEMA_VERSION,
    bonds,
    canonical_entities,
    canonical_facts,
    canonical_scalar_facts,
    dataset_snapshots,
    entity_aliases,
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
    indices,
    ingestion_runs,
    metric_definitions,
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
from app.data.v2_version import CANONICAL_V2_TRANSFORMER_VERSION
from app.graph.identity import explicit_source_id, source_scoped_name_id
from app.ontology.runtime_mapping import (
    ONTOLOGY_VERSION,
    SEMANTIC_MAPPING_VERSION,
    ConceptMapping,
    TeamOntologyRuntimeMapping,
)


GENERATION = "260824"
SNAPSHOT = "2026-08-24"
TRANSFORMER_VERSION = CANONICAL_V2_TRANSFORMER_VERSION

# The authoritative 260824 ETP sources do not expose a reviewed ETN issuer
# field.  Keeping this explicit prevents a management-company field from being
# silently reinterpreted as issuer evidence.
APPROVED_ETN_ISSUER_FIELDS: frozenset[str] = frozenset()

ORGANIZATION_SOURCE_FIELDS = frozenset(
    {"pd_pbcm", "cu_fund_mgmt_co", "or_co_xtn_itt_cd", "trusc_xtn_itt_cd"}
)
ETP_PREFIXES = frozenset({"PREF01N001", "PREF02N001"})
ETP_MISSING_ASSERTION_FIELDS = frozenset(
    {
        "pd_sale_yn",
        "pd_tr_yn",
        "pd_lstg_dt",
        "pd_lste_dt",
        "du_clpr",
        "du_clpr_base_dt",
        "du_upt_dt",
        "ru_mkt_price",
        "ru_mkt_volume",
    }
)
PRFD_MISSING_ASSERTION_FIELDS = frozenset({"rptt_ksd_itm_no"})
ETP_PRICE_DATE_FIELDS = {
    "PREF01N001": "du_upt_dt",
    "PREF02N001": "du_clpr_base_dt",
}
_PRODUCT_DESIGNATOR_SUFFIX = re.compile(r"\(\s*(?:ETF|ETN)\s*\)\s*$", re.IGNORECASE)
EXPECTED_SOURCE_COUNTS = {
    "PRBD01N001": (21_882, 21_882, 0),
    "PREF01N001": (1_780, 1_779, 1),
    "PREF02N001": (6_037, 6_037, 0),
    "PRFD01N001": (23_676, 23_676, 0),
}
EXPECTED_CANONICAL_COUNTS = {
    "financial_products": 35_180,
    "bonds": 20_497,
    "etf": 7_206,
    "etn": 610,
    "funds": 6_867,
    "fund_share_classes": 16_574,
    "unresolved_fund_rows": 7_102,
}

TARGET_FIELDS = {
    "PRBD01N001": frozenset(
        {
            "pd_no", "pd_nm", "pd_abrv_nm", "pd_pbcm", "isu_dt", "mat_dt",
            "isu_bal_amt", "pd_risk_gcd", "pd_risk_nm", "bd_knd", "bd_ofr_tcd",
            "curr_cd", "pd_ctry_cd", "pd_exg_mkt", "info_base_dt", "info_seq",
            "eval_price", "trade_price", "buy_yield", "crd_grd", "crd_grd_dt",
        }
    ),
    "PREF01N001": frozenset(
        {
            "pd_itm_no", "pd_isin_cd", "pd_itm_no_ma", "pd_ticker", "pd_ric",
            "pd_grp_no", "pd_nm", "pd_abrv_nm", "cu_fund_mgmt_co",
            "cu_base_index", "ref_base_index", "wu_inv_ast_type", "wu_inv_rgn",
            "pd_risk_nm", "pd_curr_cd", "pd_mkt_id", "pd_exg_mkt_cd",
            "du_last_aum", "cu_charge_rt", "du_last_nav", "du_clpr", "du_upt_dt",
            "du_er_1y", "pd_sale_yn", "pd_tr_yn", "pd_lstg_dt", "pd_lste_dt",
            "ru_mkt_price", "ru_mkt_volume",
            "cu_strtegy",
        }
    ),
    "PREF02N001": frozenset(
        {
            "pd_itm_no", "pd_isin_cd", "pd_lipper_id", "pd_grp_no", "pd_nm",
            "pd_abrv_nm", "cu_fund_mgmt_co", "cu_base_index",
            "cu_index_tracking_yn", "wu_inv_ast_type", "wu_inv_rgn", "pd_curr_cd",
            "pd_trd_ccy", "pd_mkt_id", "pd_exg_mkt_cd", "du_last_aum",
            "cu_charge_rt", "du_last_nav", "du_clpr", "du_clpr_base_dt",
            "du_upt_dt", "pd_sale_yn", "pd_tr_yn", "pd_lstg_dt",
            "ru_mkt_price", "ru_mkt_volume", "cu_strtegy",
        }
    ),
    "PRFD01N001": frozenset(
        {
            "itm_no", "itm_nm", "itm_abrv_nm", "rptt_ksd_itm_no", "std_itm_no",
            "ksd_itm_no", "mtco_itm_no", "fss_itm_no", "or_co_xtn_itt_cd",
            "trusc_xtn_itt_cd", "bmrk_nm", "or_attr_desc", "fd_ivst_rgn_desc",
            "ovrs_fd_desc", "zrin_fd_ivst_risk_gcd", "zrin_fd_ivst_risk_grd_nm",
            "prvo_pbff_desc", "curr_cd", "fd_nast_suma", "bns_bpr", "fd_sbpr",
            "fd_price_bas_dt", "fd_yr1_ern_r", "han_clas_nm", "han_clas_fee_type",
            "han_clas_sales_channel", "han_clas_policies", "sale_yn",
            "thco_sale_yn", "fd_daily_bas_dt",
        }
    ),
}

IDENTIFIER_FIELDS = {
    "PRBD01N001": (("pd_no", "ISIN", "iso-6166", "PRIMARY"),),
    "PREF01N001": (
        ("pd_itm_no", "ISIN", "iso-6166", "PRIMARY"),
        ("pd_isin_cd", "ISIN", "iso-6166", "SECONDARY"),
        ("pd_itm_no_ma", "MA_ID", "PREF01N001", "SECONDARY"),
        ("pd_ticker", "TICKER", "PREF01N001", "SECONDARY"),
        ("pd_ric", "RIC", "PREF01N001", "SECONDARY"),
    ),
    "PREF02N001": (
        ("pd_itm_no", "RIC", "PREF02N001", "PRIMARY"),
        ("pd_isin_cd", "ISIN", "iso-6166", "SECONDARY"),
        ("pd_lipper_id", "LIPPER_ID", "lipper", "SECONDARY"),
    ),
    "PRFD01N001": (
        ("itm_no", "SOURCE_ID", "PRFD01N001", "PRIMARY"),
        ("std_itm_no", "ISIN", "iso-6166", "SECONDARY"),
        ("ksd_itm_no", "KSD_ID", "ksd", "SECONDARY"),
        ("mtco_itm_no", "MA_ID", "miraeasset", "SECONDARY"),
        ("fss_itm_no", "FSS_ID", "fss", "SECONDARY"),
    ),
}

METRIC_FIELDS = {
    "PRBD01N001": {
        "eval_price": ("PRICE", "CURRENCY_AMOUNT", "SOURCE_RAW", "info_base_dt"),
        "buy_yield": ("BOND_BUY_YIELD", "PERCENT", "SOURCE_PERCENT", "info_base_dt"),
    },
    "PREF01N001": {
        "du_last_aum": ("AUM", "CURRENCY_AMOUNT", "CURRENCY_UNIT", "du_upt_dt"),
        "cu_charge_rt": ("EXPENSE_RATIO", "PERCENT", "SOURCE_SCALE_UNVERIFIED", "du_upt_dt"),
        "du_last_nav": ("NAV", "CURRENCY_AMOUNT", "SOURCE_RAW", "du_upt_dt"),
        "du_clpr": ("PRICE", "CURRENCY_AMOUNT", "SOURCE_RAW", "du_upt_dt"),
        "ru_mkt_price": ("MARKET_PRICE", "CURRENCY_AMOUNT", "SOURCE_RAW", "du_upt_dt"),
        "ru_mkt_volume": ("VOLUME", "COUNT", "SOURCE_RAW", "du_upt_dt"),
        "du_er_1y": ("ONE_YEAR_RETURN", "PERCENT", "SOURCE_PERCENT", "du_upt_dt"),
    },
    "PREF02N001": {
        "du_last_aum": ("AUM", "CURRENCY_AMOUNT", "CURRENCY_UNIT", "du_upt_dt"),
        "cu_charge_rt": ("EXPENSE_RATIO", "RATIO", "SOURCE_SCALE_UNVERIFIED", "du_upt_dt"),
        "du_last_nav": ("NAV", "CURRENCY_AMOUNT", "SOURCE_RAW", "du_upt_dt"),
        "du_clpr": ("PRICE", "CURRENCY_AMOUNT", "SOURCE_RAW", "du_clpr_base_dt"),
        "ru_mkt_price": ("MARKET_PRICE", "CURRENCY_AMOUNT", "SOURCE_RAW", "du_clpr_base_dt"),
        "ru_mkt_volume": ("VOLUME", "COUNT", "SOURCE_RAW", "du_clpr_base_dt"),
    },
    "PRFD01N001": {
        "fd_nast_suma": ("AUM", "CURRENCY_AMOUNT", "SOURCE_RAW_UNVERIFIED", "fd_price_bas_dt"),
        "bns_bpr": ("NAV", "CURRENCY_AMOUNT", "SOURCE_RAW", "fd_price_bas_dt"),
        "fd_sbpr": ("PRICE", "CURRENCY_AMOUNT", "SOURCE_RAW", "fd_price_bas_dt"),
        "fd_yr1_ern_r": ("ONE_YEAR_RETURN", "PERCENT", "SOURCE_PERCENT", "fd_price_bas_dt"),
    },
}


@dataclass
class DatasetResult:
    dataset: str
    source_rows: int = 0
    valid_rows: int = 0
    quarantined_rows: int = 0
    checksum: str = ""
    schema_checksum: str = ""


@dataclass
class RebuildReport:
    status: str
    skipped: bool
    datasets: list[DatasetResult]
    canonical_counts: dict[str, int] = field(default_factory=dict)
    unresolved_rows: int = 0
    classification_accounting: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    relation_counts: dict[str, int] = field(default_factory=dict)
    provenance_counts: dict[str, int] = field(default_factory=dict)
    conflict_counts: dict[str, int] = field(default_factory=dict)
    identifier_counts: dict[str, int] = field(default_factory=dict)
    crosswalk_counts: dict[str, int] = field(default_factory=dict)
    metric_counts: dict[str, int] = field(default_factory=dict)
    metric_status: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Audit:
    datasets: list[DatasetResult]
    bond_values: dict[tuple[str, str], set[str]] = field(default_factory=lambda: defaultdict(set))
    fund_values: dict[tuple[str, str], set[str]] = field(default_factory=lambda: defaultdict(set))
    identifier_owners: dict[tuple[str, str, str], set[str]] = field(default_factory=lambda: defaultdict(set))
    fund_ids: set[str] = field(default_factory=set)
    fund_class_ids: set[str] = field(default_factory=set)
    sale_lot_ids: set[str] = field(default_factory=set)
    product_ids: set[str] = field(default_factory=set)
    conflict_assertions: dict[tuple[str, str, str], set[str]] = field(
        default_factory=lambda: defaultdict(set)
    )
    latest_etp_price_dates: dict[str, date] = field(default_factory=dict)

    def conflicting(self, entity_id: str, field_name: str, *, fund: bool = False) -> bool:
        values = self.fund_values if fund else self.bond_values
        return len(values.get((entity_id, field_name), set())) > 1


@dataclass(frozen=True)
class RelationDomainContract:
    subject_grains: frozenset[tuple[str, str | None]]
    target_grains: frozenset[tuple[str, str | None]]


@dataclass(frozen=True)
class RelationDomainViolation:
    relation_type: str
    subject_entity_kind: str
    subject_product_type: str | None
    target_entity_kind: str
    target_subtype: str | None
    stored_rows: int
    reason: str = "DOMAIN_OR_RANGE"


_FINANCIAL_PRODUCT_GRAINS = frozenset(
    {
        ("FINANCIAL_PRODUCT", "BOND"),
        ("FINANCIAL_PRODUCT", "ETF"),
        ("FINANCIAL_PRODUCT", "ETN"),
        ("FINANCIAL_PRODUCT", "FUND"),
    }
)
_ETP_GRAINS = frozenset(
    {("FINANCIAL_PRODUCT", "ETF"), ("FINANCIAL_PRODUCT", "ETN")}
)
_RELATION_DOMAIN_CONTRACTS: dict[str, RelationDomainContract] = {
    "HAS_SHARE_CLASS": RelationDomainContract(
        frozenset({("FINANCIAL_PRODUCT", "FUND")}),
        frozenset({("FUND_SHARE_CLASS", None)}),
    ),
    "HAS_SALE_LOT": RelationDomainContract(
        frozenset({("FINANCIAL_PRODUCT", "BOND")}),
        frozenset({("SALE_LOT", None)}),
    ),
    "MANAGED_BY": RelationDomainContract(
        frozenset(
            {
                ("FINANCIAL_PRODUCT", "ETF"),
                ("FINANCIAL_PRODUCT", "FUND"),
            }
        ),
        frozenset({("ORGANIZATION", "ASSET_MANAGER")}),
    ),
    "ISSUED_BY": RelationDomainContract(
        frozenset(
            {
                ("FINANCIAL_PRODUCT", "BOND"),
                ("FINANCIAL_PRODUCT", "ETN"),
            }
        ),
        frozenset({("ORGANIZATION", "ISSUER")}),
    ),
    "HAS_TRUSTEE": RelationDomainContract(
        frozenset({("FINANCIAL_PRODUCT", "FUND")}),
        frozenset({("ORGANIZATION", "TRUSTEE")}),
    ),
    "HAS_BENCHMARK": RelationDomainContract(
        frozenset({("FINANCIAL_PRODUCT", "FUND")}),
        frozenset({("INDEX", None)}),
    ),
    "HAS_UNDERLYING_INDEX": RelationDomainContract(
        _ETP_GRAINS, frozenset({("INDEX", None)})
    ),
    "TRACKS_INDEX": RelationDomainContract(
        _ETP_GRAINS, frozenset({("INDEX", None)})
    ),
    "DENOMINATED_IN": RelationDomainContract(
        _FINANCIAL_PRODUCT_GRAINS, frozenset({("CURRENCY", None)})
    ),
    "TRADED_IN_CURRENCY": RelationDomainContract(
        _ETP_GRAINS, frozenset({("CURRENCY", None)})
    ),
    "LISTED_IN_COUNTRY": RelationDomainContract(
        _ETP_GRAINS, frozenset({("COUNTRY", None)})
    ),
    "HAS_INSTRUMENT_COUNTRY": RelationDomainContract(
        frozenset({("FINANCIAL_PRODUCT", "BOND")}),
        frozenset({("COUNTRY", None)}),
    ),
    "HAS_ASSET_CLASS": RelationDomainContract(
        _FINANCIAL_PRODUCT_GRAINS | frozenset({("FUND_SHARE_CLASS", None)}),
        frozenset({("ONTOLOGY_CONCEPT", "asset_class")}),
    ),
    "HAS_EXPOSURE_REGION": RelationDomainContract(
        _FINANCIAL_PRODUCT_GRAINS | frozenset({("FUND_SHARE_CLASS", None)}),
        frozenset({("ONTOLOGY_CONCEPT", "exposure_region")}),
    ),
    "HAS_MARKET_SCOPE": RelationDomainContract(
        frozenset({("FUND_SHARE_CLASS", None)}),
        frozenset({("ONTOLOGY_CONCEPT", "market_scope")}),
    ),
    "HAS_RISK_GRADE": RelationDomainContract(
        _FINANCIAL_PRODUCT_GRAINS | frozenset({("FUND_SHARE_CLASS", None)}),
        frozenset({("ONTOLOGY_CONCEPT", "risk_grade")}),
    ),
    "HAS_BOND_TYPE": RelationDomainContract(
        frozenset({("FINANCIAL_PRODUCT", "BOND")}),
        frozenset({("ONTOLOGY_CONCEPT", "bond_type")}),
    ),
    "HAS_OFFERING_TYPE": RelationDomainContract(
        frozenset(
            {
                ("FINANCIAL_PRODUCT", "BOND"),
                ("FUND_SHARE_CLASS", None),
            }
        ),
        frozenset({("ONTOLOGY_CONCEPT", "offering_type")}),
    ),
    "HAS_SUBSCRIPTION_STATUS": RelationDomainContract(
        frozenset({("FUND_SHARE_CLASS", None)}),
        frozenset({("ONTOLOGY_CONCEPT", "subscription_status")}),
    ),
    "HOLDS": RelationDomainContract(
        _FINANCIAL_PRODUCT_GRAINS,
        frozenset({("SECURITY", None)}),
    ),
    "SECURITY_ISSUED_BY": RelationDomainContract(
        frozenset({("SECURITY", None)}),
        frozenset({("ORGANIZATION", None)}),
    ),
}


class _Rows:
    def __init__(self) -> None:
        self._rows: dict[Any, list[dict[str, Any]]] = defaultdict(list)

    def add(self, table, row: dict[str, Any]) -> None:
        self._rows[table].append(row)

    def flush(self, connection) -> None:
        order = (
            source_records, quarantine_records, canonical_entities,
            financial_products, bonds, exchange_traded_products, funds,
            fund_share_classes, sale_lots, organizations, indices,
            ontology_concepts, entity_aliases, entity_identifiers,
            source_field_assertions, source_record_entities, canonical_facts,
            canonical_scalar_facts, entity_relations, organization_relations,
            index_relations, entity_classifications, source_classification_values,
            metric_observations, fact_evidence_links, identity_resolution_cases,
            fact_conflict_cases, identifier_collision_cases, entity_id_crosswalk,
        )
        for table in order:
            rows = self._rows.pop(table, [])
            if table is metric_observations:
                rows = list({str(row["fact_id"]): row for row in rows}.values())
            # PostgreSQL's extended-query protocol allows at most 65,535
            # parameters.  A row batch can expand substantially (notably for
            # SourceFieldAssertion), so cap each statement independently of
            # the source-row flush interval.
            for offset in range(0, len(rows), 500):
                chunk = rows[offset : offset + 500]
                statement = pg_insert(table).values(chunk)
                if table is metric_observations:
                    # Metric contracts can be tightened by a reviewed
                    # transformer without duplicating canonical facts.
                    excluded = statement.excluded
                    statement = statement.on_conflict_do_update(
                        index_elements=[metric_observations.c.fact_id],
                        set_={
                            column.name: getattr(excluded, column.name)
                            for column in metric_observations.c
                            if column.name != "fact_id"
                        },
                    )
                else:
                    statement = statement.on_conflict_do_nothing()
                connection.execute(statement)


def relation_domain_violations(connection) -> list[RelationDomainViolation]:
    """Return every canonical relation grain outside the reviewed contract.

    The registry intentionally combines explicit Team Ontology domains with
    the reviewed runtime policy for domainless properties.  Grouped SQL keeps
    this gate deterministic without loading individual relation rows.
    """

    observed: list[RelationDomainViolation] = []

    def collect(
        statement,
        *,
        target_kind: str | None = None,
    ) -> None:
        for row in connection.execute(statement):
            observed.append(
                RelationDomainViolation(
                    relation_type=str(row.relation_type),
                    subject_entity_kind=str(row.subject_entity_kind),
                    subject_product_type=(
                        str(row.subject_product_type)
                        if row.subject_product_type is not None
                        else None
                    ),
                    target_entity_kind=(
                        target_kind
                        if target_kind is not None
                        else str(row.target_entity_kind)
                    ),
                    target_subtype=(
                        str(row.target_subtype)
                        if row.target_subtype is not None
                        else None
                    ),
                    stored_rows=int(row.stored_rows),
                )
            )

    subject_entities = canonical_entities.alias("relation_subject_entities")
    subject_products = financial_products.alias("relation_subject_products")
    target_entities = canonical_entities.alias("relation_target_entities")
    collect(
        select(
            entity_relations.c.relation_type,
            subject_entities.c.entity_kind.label("subject_entity_kind"),
            subject_products.c.product_type_code.label("subject_product_type"),
            target_entities.c.entity_kind.label("target_entity_kind"),
            func.cast(None, String).label("target_subtype"),
            func.count().label("stored_rows"),
        )
        .select_from(
            entity_relations
            .join(
                subject_entities,
                subject_entities.c.entity_id == entity_relations.c.subject_entity_id,
            )
            .outerjoin(
                subject_products,
                subject_products.c.product_id == entity_relations.c.subject_entity_id,
            )
            .join(
                target_entities,
                target_entities.c.entity_id == entity_relations.c.object_entity_id,
            )
        )
        .group_by(
            entity_relations.c.relation_type,
            subject_entities.c.entity_kind,
            subject_products.c.product_type_code,
            target_entities.c.entity_kind,
        )
    )

    organization_subjects = canonical_entities.alias("organization_subjects")
    collect(
        select(
            organization_relations.c.relation_type,
            organization_subjects.c.entity_kind.label("subject_entity_kind"),
            financial_products.c.product_type_code.label("subject_product_type"),
            func.cast(None, String).label("target_entity_kind"),
            organizations.c.organization_type.label("target_subtype"),
            func.count().label("stored_rows"),
        )
        .select_from(
            organization_relations
            .join(
                financial_products,
                financial_products.c.product_id
                == organization_relations.c.subject_product_id,
            )
            .join(
                organization_subjects,
                organization_subjects.c.entity_id
                == organization_relations.c.subject_product_id,
            )
            .join(
                organizations,
                organizations.c.organization_id
                == organization_relations.c.organization_id,
            )
        )
        .group_by(
            organization_relations.c.relation_type,
            organization_subjects.c.entity_kind,
            financial_products.c.product_type_code,
            organizations.c.organization_type,
        ),
        target_kind="ORGANIZATION",
    )

    index_subjects = canonical_entities.alias("index_subjects")
    collect(
        select(
            index_relations.c.relation_type,
            index_subjects.c.entity_kind.label("subject_entity_kind"),
            financial_products.c.product_type_code.label("subject_product_type"),
            func.cast(None, String).label("target_entity_kind"),
            func.cast(None, String).label("target_subtype"),
            func.count().label("stored_rows"),
        )
        .select_from(
            index_relations
            .join(
                financial_products,
                financial_products.c.product_id == index_relations.c.subject_product_id,
            )
            .join(
                index_subjects,
                index_subjects.c.entity_id == index_relations.c.subject_product_id,
            )
            .join(indices, indices.c.index_id == index_relations.c.index_id)
        )
        .group_by(
            index_relations.c.relation_type,
            index_subjects.c.entity_kind,
            financial_products.c.product_type_code,
        ),
        target_kind="INDEX",
    )

    classification_subjects = canonical_entities.alias("classification_subjects")
    collect(
        select(
            ("HAS_" + entity_classifications.c.classification_type).label(
                "relation_type"
            ),
            classification_subjects.c.entity_kind.label("subject_entity_kind"),
            subject_products.c.product_type_code.label("subject_product_type"),
            func.cast(None, String).label("target_entity_kind"),
            ontology_concepts.c.concept_category.label("target_subtype"),
            func.count().label("stored_rows"),
        )
        .select_from(
            entity_classifications
            .join(
                classification_subjects,
                classification_subjects.c.entity_id == entity_classifications.c.entity_id,
            )
            .outerjoin(
                subject_products,
                subject_products.c.product_id == entity_classifications.c.entity_id,
            )
            .join(
                ontology_concepts,
                ontology_concepts.c.concept_iri
                == entity_classifications.c.concept_iri,
            )
        )
        .group_by(
            entity_classifications.c.classification_type,
            classification_subjects.c.entity_kind,
            subject_products.c.product_type_code,
            ontology_concepts.c.concept_category,
        ),
        target_kind="ONTOLOGY_CONCEPT",
    )

    violations: list[RelationDomainViolation] = []
    for grain in observed:
        contract = _RELATION_DOMAIN_CONTRACTS.get(grain.relation_type)
        subject = (grain.subject_entity_kind, grain.subject_product_type)
        target = (grain.target_entity_kind, grain.target_subtype)
        if (
            contract is None
            or subject not in contract.subject_grains
            or target not in contract.target_grains
        ):
            violations.append(grain)

    etn_issuer_rows = connection.execute(
        select(
            organization_relations.c.fact_id,
            source_field_assertions.c.source_column,
        )
        .select_from(
            organization_relations
            .join(
                financial_products,
                financial_products.c.product_id
                == organization_relations.c.subject_product_id,
            )
            .outerjoin(
                fact_evidence_links,
                fact_evidence_links.c.fact_id == organization_relations.c.fact_id,
            )
            .outerjoin(
                source_field_assertions,
                source_field_assertions.c.assertion_id
                == fact_evidence_links.c.assertion_id,
            )
        )
        .where(
            organization_relations.c.relation_type == "ISSUED_BY",
            financial_products.c.product_type_code == "ETN",
        )
    ).all()
    etn_issuer_sources: dict[str, set[str]] = defaultdict(set)
    for fact_id, source_column in etn_issuer_rows:
        if source_column is not None:
            etn_issuer_sources[str(fact_id)].add(str(source_column))
    unsupported_etn_issuers = sum(
        not (columns & APPROVED_ETN_ISSUER_FIELDS)
        for columns in etn_issuer_sources.values()
    )
    if unsupported_etn_issuers:
        violations.append(
            RelationDomainViolation(
                relation_type="ISSUED_BY",
                subject_entity_kind="FINANCIAL_PRODUCT",
                subject_product_type="ETN",
                target_entity_kind="ORGANIZATION",
                target_subtype="ISSUER",
                stored_rows=unsupported_etn_issuers,
                reason="UNAPPROVED_ETN_ISSUER_EVIDENCE",
            )
        )
    return violations


class CanonicalV2Rebuilder:
    def __init__(self, engine: Engine, *, batch_size: int = 1_000) -> None:
        self.engine = engine
        self.batch_size = batch_size
        self.semantic = TeamOntologyRuntimeMapping()
        self.classification_counts: dict[str, dict[str, Counter[str]]] = defaultdict(
            lambda: defaultdict(Counter)
        )

    def rebuild(
        self,
        material_root: Path,
        *,
        force_failure_stage: str | None = None,
    ) -> RebuildReport:
        files = discover_dataset_files(material_root)
        if {item.snapshot_date for item in files} != {SNAPSHOT}:
            raise ValueError("M10.8-B requires the authoritative 2026-08-24 generation")
        audit = self._audit(files)
        self._verify_source_baseline(audit)
        if self._is_ready(audit):
            return self._report(audit, status="SKIPPED_UNCHANGED", skipped=True)
        snapshot_ids = self._initialize(audit, files)
        try:
            if force_failure_stage == "after_initialization":
                raise RuntimeError(
                    "forced M10.8-B failure after snapshot initialization"
                )
            with self.engine.begin() as connection:
                self._ensure_metric_definitions(connection)
                rows = _Rows()
                processed = 0
                for item in files:
                    schema = load_source_schema(item.schema_file)
                    snapshot_id = snapshot_ids[item.spec.prefix]
                    for row_number, raw in iter_source_rows(item.data_file, schema):
                        self._process_row(
                            rows, audit, item, snapshot_id, row_number, raw
                        )
                        processed += 1
                        if processed % self.batch_size == 0:
                            rows.flush(connection)
                    rows.flush(connection)
                self._add_collision_cases(rows, audit)
                self._add_conflict_cases(rows, audit, snapshot_ids)
                self._add_crosswalk(rows, audit)
                rows.flush(connection)
                if force_failure_stage == "before_reconciliation":
                    raise RuntimeError("forced M10.8-B failure before reconciliation")
                counts = self._reconcile(
                    connection, expected_sale_lots=len(audit.sale_lot_ids)
                )
                self._mark_ready(connection, audit, snapshot_ids, counts)
            return self._report(audit, status="READY", skipped=False)
        except Exception as exc:
            with self.engine.begin() as connection:
                connection.execute(
                    dataset_snapshots.update()
                    .where(dataset_snapshots.c.snapshot_id.in_(snapshot_ids.values()))
                    .values(status="FAILED", reconciliation_status="FAILED")
                )
                connection.execute(
                    ingestion_runs.update()
                    .where(ingestion_runs.c.snapshot_id.in_(snapshot_ids.values()))
                    .values(status="FAILED", completed_at=func.now(), report={"error": str(exc)})
                )
            raise

    def _audit(self, files: list[DatasetFiles]) -> Audit:
        results: list[DatasetResult] = []
        audit = Audit(results)
        for item in files:
            result = DatasetResult(
                dataset=item.spec.prefix,
                checksum=_sha256(item.data_file),
                schema_checksum=_sha256(item.schema_file),
            )
            schema = load_source_schema(item.schema_file)
            for row_number, raw in iter_source_rows(item.data_file, schema):
                del row_number
                result.source_rows += 1
                cleaned, _ = clean_source_row(
                    raw, literal_null_fields=item.spec.literal_null_fields
                )
                if validate_product_row(item.spec, cleaned):
                    result.quarantined_rows += 1
                    continue
                result.valid_rows += 1
                mapped, error = map_product(
                    item.spec, cleaned, source_file=str(item.data_file),
                    source_row_number=0, snapshot=item.snapshot_date,
                )
                if mapped is None:
                    raise ValueError(f"validated row failed mapping: {error}")
                self._audit_identity(audit, item, cleaned, mapped)
                if item.spec.prefix in ETP_PREFIXES:
                    date_field = ETP_PRICE_DATE_FIELDS[item.spec.prefix]
                    observed = _date(cleaned.get(date_field))
                    if observed is not None:
                        current = audit.latest_etp_price_dates.get(item.spec.prefix)
                        if current is None or observed > current:
                            audit.latest_etp_price_dates[item.spec.prefix] = observed
            results.append(result)
        return audit

    def _audit_identity(
        self, audit: Audit, item: DatasetFiles, row: dict[str, Any], mapped: MappedProduct
    ) -> None:
        prefix = item.spec.prefix
        product_id = str(mapped.canonical["canonical_product_id"])
        if prefix == "PRBD01N001":
            audit.product_ids.add(product_id)
            source_key = str(mapped.canonical["source_record_key"])
            if has_prbd_sale_lot_evidence(row):
                audit.sale_lot_ids.add(
                    explicit_source_id("sale_lot", "domestic_bond", source_key)
                )
            for field_name in ("pd_nm", "pd_pbcm", "isu_dt", "mat_dt", "isu_bal_amt", "pd_risk_gcd", "pd_risk_nm", "bd_knd", "bd_ofr_tcd"):
                value = _value(row.get(field_name))
                if value is not None:
                    audit.bond_values[(product_id, field_name)].add(value)
            target_id = product_id
        elif prefix in {"PREF01N001", "PREF02N001"}:
            audit.product_ids.add(product_id)
            target_id = product_id
        else:
            if mapped.fund_class is None:
                return
            target_id = product_id
            fund_id = str(mapped.fund_class["fund_id"])
            audit.product_ids.add(fund_id)
            audit.fund_ids.add(fund_id)
            audit.fund_class_ids.add(product_id)
            for field_name in ("or_co_xtn_itt_cd", "trusc_xtn_itt_cd", "bmrk_nm", "prvo_pbff_desc", "or_attr_desc", "fd_ivst_rgn_desc", "ovrs_fd_desc"):
                value = _value(row.get(field_name))
                if value is not None:
                    audit.fund_values[(fund_id, field_name)].add(value)
        for field_name, scheme, namespace, _ in IDENTIFIER_FIELDS[prefix]:
            value = _identifier_value(row.get(field_name))
            if value is not None:
                audit.identifier_owners[(scheme, namespace, value)].add(target_id)
        if prefix == "PRFD01N001" and mapped.fund_class is not None:
            representative = _identifier_value(row.get("rptt_ksd_itm_no"))
            if representative:
                audit.identifier_owners[("REPRESENTATIVE_KSD_ID", "ksd", representative)].add(str(mapped.fund_class["fund_id"]))

    def _verify_source_baseline(self, audit: Audit) -> None:
        actual = {
            item.dataset: (item.source_rows, item.valid_rows, item.quarantined_rows)
            for item in audit.datasets
        }
        if actual != EXPECTED_SOURCE_COUNTS:
            raise ValueError(f"authoritative source reconciliation mismatch: {actual}")

    def _is_ready(self, audit: Audit) -> bool:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    dataset_snapshots.c.data_sha256,
                    dataset_snapshots.c.schema_sha256,
                    dataset_snapshots.c.status,
                    dataset_snapshots.c.ontology_version,
                    dataset_snapshots.c.semantic_mapping_version,
                    dataset_snapshots.c.transformer_version,
                    dataset_snapshots.c.database_schema_version,
                ).where(dataset_snapshots.c.snapshot_date == date.fromisoformat(SNAPSHOT))
            ).all()
        expected = {
            (
                item.checksum, item.schema_checksum, "READY", ONTOLOGY_VERSION,
                SEMANTIC_MAPPING_VERSION, TRANSFORMER_VERSION,
                CANONICAL_V2_SCHEMA_VERSION,
            )
            for item in audit.datasets
        }
        return len(rows) == 4 and set(rows) == expected

    def _initialize(
        self, audit: Audit, files: list[DatasetFiles]
    ) -> dict[str, str]:
        by_prefix = {item.spec.prefix: item for item in files}
        snapshot_ids: dict[str, str] = {}
        with self.engine.begin() as connection:
            for result in audit.datasets:
                item = by_prefix[result.dataset]
                snapshot_id = f"snapshot:{result.dataset}:{SNAPSHOT}:{result.checksum[:12]}"
                snapshot_ids[result.dataset] = snapshot_id
                connection.execute(
                    pg_insert(source_datasets).values(
                        dataset_id=result.dataset,
                        dataset_code=result.dataset,
                        display_name=item.spec.source_dataset,
                        source_system="260824 authoritative workbook",
                        schema_contract_version=f"{result.dataset}-{len(load_source_schema(item.schema_file).columns)}",
                        is_authoritative=True,
                    ).on_conflict_do_update(
                        index_elements=[source_datasets.c.dataset_id],
                        set_={"schema_contract_version": f"{result.dataset}-{len(load_source_schema(item.schema_file).columns)}", "is_authoritative": True},
                    )
                )
                values = {
                    "snapshot_id": snapshot_id,
                    "dataset_id": result.dataset,
                    "snapshot_date": date.fromisoformat(SNAPSHOT),
                    "generation": GENERATION,
                    "ontology_version": ONTOLOGY_VERSION,
                    "semantic_mapping_version": SEMANTIC_MAPPING_VERSION,
                    "transformer_version": TRANSFORMER_VERSION,
                    "database_schema_version": CANONICAL_V2_SCHEMA_VERSION,
                    "data_sha256": result.checksum,
                    "schema_sha256": result.schema_checksum,
                    "source_row_count": result.source_rows,
                    "accepted_row_count": result.valid_rows,
                    "quarantined_row_count": result.quarantined_rows,
                    "status": "STAGED",
                    "reconciliation_status": "PENDING",
                    "row_count_reconciled": False,
                    "metadata_json": {"generation": GENERATION},
                }
                connection.execute(
                    pg_insert(dataset_snapshots).values(**values).on_conflict_do_update(
                        index_elements=[dataset_snapshots.c.snapshot_id], set_=values
                    )
                )
                run_id = f"run:{TRANSFORMER_VERSION}:{result.dataset}:{result.checksum[:12]}"
                connection.execute(
                    pg_insert(ingestion_runs).values(
                        run_id=run_id, snapshot_id=snapshot_id, status="STARTED",
                        transformer_version=TRANSFORMER_VERSION,
                        options={"generation": GENERATION},
                    ).on_conflict_do_update(
                        index_elements=[ingestion_runs.c.run_id],
                        set_={"status": "STARTED", "completed_at": None, "report": None},
                    )
                )
        return snapshot_ids

    def _process_row(
        self, rows: _Rows, audit: Audit, item: DatasetFiles, snapshot_id: str,
        row_number: int, raw: dict[str, Any],
    ) -> None:
        cleaned, _ = clean_source_row(raw, literal_null_fields=item.spec.literal_null_fields)
        failure = validate_product_row(item.spec, cleaned)
        if failure:
            rows.add(quarantine_records, {
                "snapshot_id": snapshot_id, "source_row_number": row_number,
                "source_primary_key": failure.source_key,
                "reason_code": failure.code, "failure_reason": failure.reason,
                "raw_payload": _json_payload(raw), "status": "OPEN",
            })
            return
        mapped, error = map_product(
            item.spec, cleaned, source_file=str(item.data_file),
            source_row_number=row_number, snapshot=item.snapshot_date,
        )
        if mapped is None:
            raise ValueError(f"validated row failed mapping: {error}")
        prefix = item.spec.prefix
        source_key = str(mapped.canonical["source_record_key"])
        record_id = _stable_id("source", prefix, SNAPSHOT, source_key)
        rows.add(source_records, {
            "source_record_id": record_id, "snapshot_id": snapshot_id,
            "source_primary_key": source_key, "source_row_number": row_number,
            "raw_payload": _json_payload(raw), "normalized_payload": _json_payload(cleaned),
            "payload_sha256": _payload_hash(raw), "quality_status": "VALIDATED",
        })
        assertions = self._assertions(rows, prefix, record_id, raw, cleaned)
        if prefix == "PRFD01N001" and mapped.fund_class is None:
            self._classification_values_only(rows, prefix, cleaned, assertions)
            rows.add(identity_resolution_cases, {
                "resolution_case_id": _stable_id("resolution", record_id),
                "source_record_id": record_id,
                "raw_identity": {field: cleaned.get(field) for field in ("itm_no", "rptt_ksd_itm_no", "itm_nm")},
                "candidate_entity_ids": [], "resolution_status": "UNRESOLVED",
                "reason_code": "UNRESOLVED_PARENT",
                "resolution_rule": "representative fund identifier is absent or unsafe",
            })
            return
        primary_id, support_id = self._entities(rows, audit, item, mapped, cleaned)
        self._remember_conflict_assertions(
            audit, prefix, primary_id, support_id, cleaned, assertions
        )
        if primary_id:
            rows.add(source_record_entities, {
                "source_record_id": record_id, "entity_id": primary_id,
                "entity_kind": _entity_kind(prefix, primary=True),
                "provenance_role": "DESCRIBES",
            })
        if support_id:
            rows.add(source_record_entities, {
                "source_record_id": record_id, "entity_id": support_id,
                "entity_kind": "FINANCIAL_PRODUCT", "provenance_role": "SUPPORTS",
            })
        self._aliases(rows, prefix, primary_id, support_id, mapped, cleaned, record_id)
        self._identifiers(rows, audit, prefix, primary_id, support_id, cleaned, record_id)
        self._scalar_facts(rows, audit, prefix, primary_id, support_id, snapshot_id, cleaned, assertions)
        self._classifications(rows, audit, prefix, primary_id, support_id, snapshot_id, cleaned, assertions)
        self._relations(
            rows,
            audit,
            prefix,
            primary_id,
            support_id,
            snapshot_id,
            cleaned,
            assertions,
            record_id,
        )
        self._metrics(rows, prefix, primary_id, support_id, snapshot_id, cleaned, assertions)

    def _remember_conflict_assertions(
        self,
        audit: Audit,
        prefix: str,
        primary_id: str | None,
        support_id: str | None,
        cleaned: dict[str, Any],
        assertions: dict[str, str],
    ) -> None:
        del primary_id
        if prefix == "PRBD01N001":
            values = audit.bond_values
        elif prefix == "PRFD01N001":
            values = audit.fund_values
        else:
            return
        if not support_id:
            return
        field_names = (
            ("pd_nm", "pd_pbcm", "isu_dt", "mat_dt", "isu_bal_amt", "pd_risk_gcd", "pd_risk_nm", "bd_knd", "bd_ofr_tcd")
            if prefix == "PRBD01N001"
            else ("or_co_xtn_itt_cd", "trusc_xtn_itt_cd", "bmrk_nm", "prvo_pbff_desc", "or_attr_desc", "fd_ivst_rgn_desc", "ovrs_fd_desc")
        )
        for field_name in field_names:
            candidates = values.get((support_id, field_name), set())
            if len(candidates) <= 1:
                continue
            raw_value = _value(cleaned.get(field_name))
            assertion_id = assertions.get(field_name)
            if raw_value is not None and assertion_id is not None:
                audit.conflict_assertions[(support_id, field_name, raw_value)].add(
                    assertion_id
                )

    def _assertions(
        self, rows: _Rows, prefix: str, record_id: str,
        raw: dict[str, Any], cleaned: dict[str, Any],
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        for field_name in TARGET_FIELDS[prefix]:
            value = cleaned.get(field_name)
            missing = value is None or value == ""
            preserve_missing = (
                prefix in ETP_PREFIXES
                and field_name in ETP_MISSING_ASSERTION_FIELDS
            ) or (
                prefix == "PRFD01N001"
                and field_name in PRFD_MISSING_ASSERTION_FIELDS
            )
            if missing and not preserve_missing:
                continue
            assertion_id = _stable_id("assertion", record_id, field_name)
            result[field_name] = assertion_id
            organization_rejection = (
                _organization_target_rejection_reason(_value(value))
                if field_name in ORGANIZATION_SOURCE_FIELDS
                else None
            )
            etn_manager = (
                field_name == "cu_fund_mgmt_co"
                and str(cleaned.get("pd_grp_no") or "").strip().upper() == "ETN"
            )
            target_semantic_key = _target_key(prefix, field_name, value)
            transformation_rule = "M10.8-B.2 reviewed deterministic mapping"
            quality_status = "VALID"
            if missing:
                quality_status = "MISSING"
                transformation_rule = "source field is structurally present but blank/null; preserve as unknown"
            elif organization_rejection:
                quality_status = "INVALID"
                target_semantic_key = "organization:INVALID_TARGET"
                transformation_rule = organization_rejection
            elif etn_manager:
                target_semantic_key = "managedBy:UNSUPPORTED_FOR_ETN"
                transformation_rule = (
                    "source value preserved; Team Ontology v1.3 prohibits "
                    "ETN managedBy and no issuer reinterpretation is allowed"
                )
            rows.add(source_field_assertions, {
                "assertion_id": assertion_id, "source_record_id": record_id,
                "source_column": field_name, "raw_value": _text(raw.get(field_name)),
                "normalized_value": _text(value), "mapping_category": _field_category(prefix, field_name),
                "target_semantic_key": target_semantic_key,
                "quality_status": quality_status,
                "transformation_rule": transformation_rule,
            })
        return result

    def _entities(
        self, rows: _Rows, audit: Audit, item: DatasetFiles,
        mapped: MappedProduct, cleaned: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        prefix = item.spec.prefix
        product_id = str(mapped.canonical["canonical_product_id"])
        if prefix == "PRBD01N001":
            bond_name = None if audit.conflicting(product_id, "pd_nm") else _value(cleaned.get("pd_nm"))
            self._product(rows, product_id, "BOND", bond_name, "AUTHORITATIVE" if bond_name else "UNKNOWN")
            issue = None if audit.conflicting(product_id, "isu_dt") else _date(cleaned.get("isu_dt"))
            maturity = None if audit.conflicting(product_id, "mat_dt") else _date(cleaned.get("mat_dt"))
            rows.add(bonds, {"bond_id": product_id, "product_type_code": "BOND", "issue_date": issue, "maturity_date": maturity})
            if not has_prbd_sale_lot_evidence(cleaned):
                return None, product_id
            lot_id = explicit_source_id("sale_lot", "domestic_bond", str(mapped.canonical["source_record_key"]))
            self._entity(rows, lot_id, "SALE_LOT", str(mapped.canonical["source_record_key"]), "SOURCE_ONLY", True)
            rows.add(sale_lots, {
                "sale_lot_id": lot_id, "bond_id": product_id,
                "trading_market_raw": str(cleaned.get("pd_exg_mkt") or "UNKNOWN"),
                "information_date": _date(cleaned.get("info_base_dt")) or date.fromisoformat(SNAPSHOT),
                "lot_sequence": int(cleaned.get("info_seq") or 1),
            })
            return lot_id, product_id
        if prefix in {"PREF01N001", "PREF02N001"}:
            code = "ETF" if str(mapped.canonical["product_type"]).endswith("ETF") else "ETN"
            self._product(rows, product_id, code, _value(cleaned.get("pd_nm")), "AUTHORITATIVE")
            listing_start = _date(cleaned.get("pd_lstg_dt"))
            listing_end = _date(cleaned.get("pd_lste_dt")) if prefix == "PREF01N001" else None
            rows.add(exchange_traded_products, {
                "etp_id": product_id, "product_type_code": code,
                "listing_date": listing_start, "delisting_date": listing_end,
            })
            return product_id, None
        fund_id = str(mapped.fund_class["fund_id"])
        self._product(rows, fund_id, "FUND", None, "NO_AUTHORITATIVE_FAMILY_NAME")
        rows.add(funds, {"fund_id": fund_id, "product_type_code": "FUND"})
        self._entity(rows, product_id, "FUND_SHARE_CLASS", _value(cleaned.get("itm_nm")), "SOURCE_ONLY", True)
        rows.add(fund_share_classes, {
            "fund_share_class_id": product_id, "parent_fund_id": fund_id,
            "source_class_key": str(cleaned["itm_no"]),
        })
        return product_id, fund_id

    def _entity(
        self, rows: _Rows, entity_id: str, kind: str, name: str | None,
        name_status: str, query_eligible: bool,
    ) -> None:
        rows.add(canonical_entities, {
            "entity_id": entity_id, "entity_kind": kind, "preferred_name": name,
            "normalized_preferred_name": normalize_lookup_value(name) if name else None,
            "name_status": name_status, "identity_status": "VALIDATED",
            "query_eligible": query_eligible,
        })

    def _product(self, rows: _Rows, entity_id: str, code: str, name: str | None, status: str) -> None:
        self._entity(rows, entity_id, "FINANCIAL_PRODUCT", name, status, True)
        rows.add(financial_products, {"product_id": entity_id, "product_type_code": code})

    def _aliases(
        self, rows: _Rows, prefix: str, primary_id: str | None, support_id: str | None,
        mapped: MappedProduct, cleaned: dict[str, Any], record_id: str,
    ) -> None:
        alias_subject = support_id if prefix == "PRBD01N001" and primary_id is None else primary_id
        if alias_subject is None:
            return
        name_field = "itm_nm" if prefix == "PRFD01N001" else "pd_nm"
        name = _value(cleaned.get(name_field))
        if name:
            self._alias(rows, alias_subject, name, "SOURCE_NAME", record_id)
            if prefix == "PRFD01N001" and support_id:
                self._alias(rows, support_id, name, "MEMBER_CLASS_NAME", record_id)
        short = _value(mapped.canonical.get("short_name"))
        if short:
            self._alias(rows, alias_subject, short, "SHORT_NAME", record_id)

    def _alias(self, rows: _Rows, entity_id: str, value: str, alias_type: str, record_id: str) -> None:
        rows.add(entity_aliases, {
            "entity_id": entity_id, "alias": value,
            "normalized_alias": normalize_lookup_value(value), "alias_type": alias_type,
            "source_record_id": record_id, "is_preferred": False,
        })

    def _identifiers(
        self, rows: _Rows, audit: Audit, prefix: str, primary_id: str | None,
        support_id: str | None, cleaned: dict[str, Any], record_id: str,
    ) -> None:
        target = support_id if prefix == "PRBD01N001" else primary_id
        if target is None:
            return
        for field_name, scheme, namespace, priority in IDENTIFIER_FIELDS[prefix]:
            value = _identifier_value(cleaned.get(field_name))
            if not value:
                continue
            owners = audit.identifier_owners[(scheme, namespace, value)]
            collision = len(owners) > 1
            validation = _identifier_validation(scheme, value)
            rows.add(entity_identifiers, {
                "entity_id": target, "scheme_code": scheme, "namespace": namespace,
                "raw_value": str(cleaned[field_name]), "normalized_value": value,
                "validation_status": validation, "resolution_status": "RESOLVED",
                "conflict_status": "OPEN" if collision else "NONE",
                "is_primary": priority == "PRIMARY", "source_record_id": record_id,
            })
        if prefix == "PRFD01N001" and support_id:
            value = _identifier_value(cleaned.get("rptt_ksd_itm_no"))
            if value:
                owners = audit.identifier_owners[("REPRESENTATIVE_KSD_ID", "ksd", value)]
                rows.add(entity_identifiers, {
                    "entity_id": support_id, "scheme_code": "REPRESENTATIVE_KSD_ID",
                    "namespace": "ksd", "raw_value": str(cleaned["rptt_ksd_itm_no"]),
                    "normalized_value": value, "validation_status": "VALIDATED",
                    "resolution_status": "RESOLVED",
                    "conflict_status": "OPEN" if len(owners) > 1 else "NONE",
                    "is_primary": True, "source_record_id": record_id,
                })

    def _scalar_facts(
        self, rows: _Rows, audit: Audit, prefix: str, primary_id: str | None,
        support_id: str | None, snapshot_id: str, cleaned: dict[str, Any],
        assertions: dict[str, str],
    ) -> None:
        subject = support_id if prefix == "PRBD01N001" else primary_id
        if subject is None:
            return
        product_type_field = {
            "PRBD01N001": "pd_no",
            "PREF01N001": "pd_grp_no",
            "PREF02N001": "pd_grp_no",
            "PRFD01N001": "rptt_ksd_itm_no",
        }[prefix]
        product_type = {
            "PRBD01N001": "BOND",
            "PRFD01N001": "FUND",
        }.get(prefix, str(cleaned.get("pd_grp_no") or "").upper())
        self._scalar(
            rows,
            subject,
            snapshot_id,
            "product_type",
            "TEXT",
            product_type,
            assertions.get(product_type_field) or assertions.get("itm_no"),
        )
        name_field = "itm_nm" if prefix == "PRFD01N001" else "pd_nm"
        if not (prefix == "PRBD01N001" and audit.conflicting(subject, name_field)):
            self._scalar(rows, subject, snapshot_id, "name", "TEXT", _value(cleaned.get(name_field)), assertions.get(name_field))
        if prefix == "PRBD01N001":
            for field_name, key, value_type in (
                ("isu_dt", "issue_date", "DATE"),
                ("mat_dt", "maturity_date", "DATE"),
                ("isu_bal_amt", "issue_balance", "NUMERIC"),
            ):
                if not audit.conflicting(subject, field_name):
                    value = _date(cleaned.get(field_name)) if value_type == "DATE" else _decimal(cleaned.get(field_name))
                    self._scalar(rows, subject, snapshot_id, key, value_type, value, assertions.get(field_name))
        elif prefix == "PRFD01N001":
            self._scalar(
                rows, subject, snapshot_id, "is_sold_by_mirae_asset", "BOOLEAN",
                canonical_mirae_sale_flag(cleaned.get("thco_sale_yn")),
                assertions.get("thco_sale_yn"),
            )
        elif prefix in ETP_PREFIXES:
            self._etp_scalar_facts(
                rows, audit, prefix, subject, snapshot_id, cleaned, assertions
            )

    def _scalar(
        self, rows: _Rows, subject: str, snapshot_id: str, key: str,
        value_type: str, value: Any, assertion_id: str | None,
        *, resolution: str = "RESOLVED",
    ) -> str | None:
        if value is None or assertion_id is None:
            return None
        fact_id = self._fact(rows, subject, snapshot_id, "SCALAR", key, resolution)
        typed = {"text_value": None, "numeric_value": None, "date_value": None, "boolean_value": None}
        typed[{"TEXT": "text_value", "NUMERIC": "numeric_value", "DATE": "date_value", "BOOLEAN": "boolean_value"}[value_type]] = value
        rows.add(canonical_scalar_facts, {"fact_id": fact_id, "value_type": value_type, **typed})
        self._evidence(rows, fact_id, assertion_id)
        return fact_id

    def _derived_scalar(
        self, rows: _Rows, subject: str, snapshot_id: str, key: str,
        value_type: str, value: Any, assertion_ids: Iterable[str | None],
    ) -> str | None:
        evidence = [item for item in assertion_ids if item is not None]
        if value is None or not evidence:
            return None
        fact_id = self._fact(rows, subject, snapshot_id, "SCALAR", key)
        typed = {"text_value": None, "numeric_value": None, "date_value": None, "boolean_value": None}
        typed[{"TEXT": "text_value", "NUMERIC": "numeric_value", "DATE": "date_value", "BOOLEAN": "boolean_value"}[value_type]] = value
        rows.add(canonical_scalar_facts, {"fact_id": fact_id, "value_type": value_type, **typed})
        for assertion_id in evidence:
            self._evidence(rows, fact_id, assertion_id, role="DERIVES")
        return fact_id

    def _etp_scalar_facts(
        self, rows: _Rows, audit: Audit, prefix: str, subject: str,
        snapshot_id: str, cleaned: dict[str, Any], assertions: dict[str, str],
    ) -> None:
        sale_status = _etp_sale_status(cleaned.get("pd_sale_yn"))
        trading_status = _etp_trading_status(cleaned.get("pd_tr_yn"))
        listing_start = _date(cleaned.get("pd_lstg_dt"))
        listing_end = _date(cleaned.get("pd_lste_dt")) if prefix == "PREF01N001" else None
        snapshot_date = date.fromisoformat(SNAPSHOT)
        price_value = _decimal(cleaned.get("du_clpr"))
        price_observed = _date(cleaned.get(ETP_PRICE_DATE_FIELDS[prefix]))
        latest_price_date = audit.latest_etp_price_dates.get(prefix)
        valid_price = price_value is not None and price_value > 0 and price_observed is not None
        price_freshness = (
            "LATEST"
            if valid_price and latest_price_date is not None and price_observed == latest_price_date
            else "STALE"
            if valid_price and latest_price_date is not None and price_observed < latest_price_date
            else "UNKNOWN"
        )
        listing_has_ended = (
            listing_end is not None and listing_end <= snapshot_date
            if prefix == "PREF01N001"
            else None
        )
        current_sale_eligible = (
            sale_status == "AVAILABLE_FOR_SALE"
            and trading_status == "TRADING_ACTIVE"
            and listing_start is not None
            and listing_start <= snapshot_date
            and listing_has_ended is not True
        )
        insufficient_reasons = _etp_insufficient_reasons(cleaned)
        status_assertions = [
            assertions.get("pd_sale_yn"), assertions.get("pd_tr_yn"),
            assertions.get("pd_lstg_dt"), assertions.get("pd_lste_dt"),
            assertions.get("pd_itm_no"),
        ]
        price_assertions = [
            assertions.get("du_clpr"),
            assertions.get(ETP_PRICE_DATE_FIELDS[prefix]),
            assertions.get("ru_mkt_price"),
            assertions.get("ru_mkt_volume"),
            assertions.get("pd_itm_no"),
        ]

        self._scalar(
            rows, subject, snapshot_id, "etp_distribution_status", "TEXT",
            sale_status, assertions.get("pd_sale_yn"),
        )
        self._scalar(
            rows, subject, snapshot_id, "etp_trading_status", "TEXT",
            trading_status, assertions.get("pd_tr_yn"),
        )
        self._scalar(
            rows, subject, snapshot_id, "listing_start_date", "DATE",
            listing_start, assertions.get("pd_lstg_dt"),
        )
        self._scalar(
            rows, subject, snapshot_id, "listing_end_date", "DATE",
            listing_end, assertions.get("pd_lste_dt"),
        )
        if prefix == "PREF01N001" and _is_no_known_end_date(cleaned.get("pd_lste_dt")):
            self._scalar(
                rows, subject, snapshot_id, "listing_end_date_status", "TEXT",
                "NO_KNOWN_END_DATE", assertions.get("pd_lste_dt"),
            )
        if listing_has_ended is not None:
            self._derived_scalar(
                rows, subject, snapshot_id, "etp_listing_ended", "BOOLEAN",
                listing_has_ended, [assertions.get("pd_lste_dt")],
            )
        self._derived_scalar(
            rows, subject, snapshot_id, "current_etp_sale_eligible", "BOOLEAN",
            True if current_sale_eligible else None, status_assertions,
        )
        self._derived_scalar(
            rows, subject, snapshot_id, "etp_price_freshness_status", "TEXT",
            price_freshness, price_assertions,
        )
        self._derived_scalar(
            rows, subject, snapshot_id, "latest_etp_price_available", "BOOLEAN",
            True if price_freshness == "LATEST" else None, price_assertions,
        )
        self._derived_scalar(
            rows, subject, snapshot_id, "stale_etp_price_warning", "BOOLEAN",
            True if current_sale_eligible and price_freshness == "STALE" else None,
            [*status_assertions, *price_assertions],
        )
        self._derived_scalar(
            rows, subject, snapshot_id, "etp_insufficient_info", "BOOLEAN",
            True if insufficient_reasons else None, status_assertions,
        )

    def _classifications(
        self, rows: _Rows, audit: Audit, prefix: str, primary_id: str | None,
        support_id: str | None, snapshot_id: str, cleaned: dict[str, Any],
        assertions: dict[str, str],
    ) -> None:
        subject = support_id if prefix == "PRBD01N001" else primary_id
        if subject is None:
            return
        for field_name, category, raw_value in _classification_inputs(prefix, cleaned):
            counter = self.classification_counts[prefix][category]
            if not raw_value.strip():
                counter["missing"] += 1
                continue
            counter["source"] += 1
            assertion_id = assertions.get(field_name)
            if assertion_id is None and field_name == "pd_risk_nm":
                assertion_id = assertions.get("pd_risk_gcd")
            if assertion_id is None and field_name == "zrin_fd_ivst_risk_grd_nm":
                assertion_id = assertions.get("zrin_fd_ivst_risk_gcd")
            mapping = self.semantic.concept(raw_value, category)
            semantic = mapping.semantic_value() if mapping else None
            conflict = prefix == "PRBD01N001" and field_name != "pd_no" and audit.conflicting(subject, field_name)
            if conflict:
                counter["conflicting"] += 1
            elif semantic:
                counter["mapped"] += 1
            else:
                counter["unmapped"] += 1
            if assertion_id is None:
                continue
            concept_iri = semantic.ontology_uri if semantic else None
            if mapping and semantic:
                self._concept(rows, mapping)
            rows.add(source_classification_values, {
                "assertion_id": assertion_id, "raw_value": raw_value,
                "normalized_value": normalize_lookup_value(raw_value),
                "candidate_concept_iri": concept_iri,
                "resolution_status": "AMBIGUOUS" if conflict else ("RESOLVED" if concept_iri else "UNRESOLVED"),
                "resolution_rule": "Team Ontology v1.3 exact alias mapping",
            })
            if concept_iri and not conflict:
                fact_id = self._fact(rows, subject, snapshot_id, "CLASSIFICATION", f"{category}:{concept_iri}")
                rows.add(entity_classifications, {
                    "fact_id": fact_id, "entity_id": subject,
                    "concept_iri": concept_iri, "classification_type": category.upper(),
                })
                self._evidence(rows, fact_id, assertion_id)
            elif conflict:
                fact_id = self._fact(rows, subject, snapshot_id, "CLASSIFICATION", f"conflict:{category}:{normalize_lookup_value(raw_value)}", "CONFLICT")
                self._evidence(rows, fact_id, assertion_id)

    def _classification_values_only(
        self,
        rows: _Rows,
        prefix: str,
        cleaned: dict[str, Any],
        assertions: dict[str, str],
    ) -> None:
        """Preserve mapping state when no canonical entity can be asserted."""
        for field_name, category, raw_value in _classification_inputs(prefix, cleaned):
            counter = self.classification_counts[prefix][category]
            if not raw_value.strip():
                counter["missing"] += 1
                continue
            counter["source"] += 1
            assertion_id = assertions.get(field_name)
            if assertion_id is None and field_name == "zrin_fd_ivst_risk_grd_nm":
                assertion_id = assertions.get("zrin_fd_ivst_risk_gcd")
            mapping = self.semantic.concept(raw_value, category)
            semantic = mapping.semantic_value() if mapping else None
            counter["mapped" if semantic else "unmapped"] += 1
            if assertion_id is None:
                continue
            if mapping and semantic:
                self._concept(rows, mapping)
            rows.add(source_classification_values, {
                "assertion_id": assertion_id,
                "raw_value": raw_value,
                "normalized_value": normalize_lookup_value(raw_value),
                "candidate_concept_iri": semantic.ontology_uri if semantic else None,
                "resolution_status": "RESOLVED" if semantic else "UNRESOLVED",
                "resolution_rule": (
                    "Team Ontology v1.3 exact alias mapping; "
                    "canonical subject unresolved"
                ),
            })

    def _concept(self, rows: _Rows, mapping: ConceptMapping) -> None:
        rows.add(ontology_concepts, {
            "concept_iri": mapping.ontology_uri, "concept_category": mapping.category,
            "canonical_name": mapping.canonical_name, "ontology_version": ONTOLOGY_VERSION,
            "active": True,
        })

    def _relations(
        self, rows: _Rows, audit: Audit, prefix: str, primary_id: str | None,
        support_id: str | None, snapshot_id: str, cleaned: dict[str, Any],
        assertions: dict[str, str], record_id: str,
    ) -> None:
        if prefix == "PRBD01N001":
            if primary_id is not None:
                self._entity_relation(
                    rows,
                    support_id,
                    "HAS_SALE_LOT",
                    primary_id,
                    snapshot_id,
                    assertions.get("pd_no"),
                )
            issuer = _value(cleaned.get("pd_pbcm"))
            if issuer and not audit.conflicting(support_id, "pd_pbcm"):
                org = self._validated_organization(
                    rows,
                    "ISSUER",
                    "domestic_bond",
                    issuer,
                    record_id,
                    "pd_pbcm",
                    support_id,
                    "ISSUED_BY",
                )
                if org:
                    self._organization_relation(rows, support_id, "ISSUED_BY", org, snapshot_id, assertions.get("pd_pbcm"))
            self._currency_and_country_relations(
                rows, prefix, support_id, snapshot_id, cleaned, assertions
            )
            return
        if prefix in {"PREF01N001", "PREF02N001"}:
            product_type = str(cleaned.get("pd_grp_no") or "").strip().upper()
            manager = _value(cleaned.get("cu_fund_mgmt_co"))
            if manager:
                rejection = _organization_target_rejection_reason(manager)
                if rejection:
                    self._add_relation_resolution_case(
                        rows,
                        record_id,
                        "cu_fund_mgmt_co",
                        manager,
                        primary_id,
                        "MANAGED_BY",
                        "INVALID_ORGANIZATION_TARGET",
                        "REJECTED",
                        rejection,
                    )
                elif product_type == "ETF":
                    org = self._organization(rows, "ASSET_MANAGER", prefix, manager)
                    self._organization_relation(rows, primary_id, "MANAGED_BY", org, snapshot_id, assertions.get("cu_fund_mgmt_co"))
                else:
                    org = self._organization(rows, "ASSET_MANAGER", prefix, manager)
                    self._add_relation_resolution_case(
                        rows,
                        record_id,
                        "cu_fund_mgmt_co",
                        manager,
                        primary_id,
                        "MANAGED_BY",
                        "UNSUPPORTED_RELATION_DOMAIN",
                        "UNRESOLVED",
                        "Team Ontology v1.3 prohibits ETN managedBy; issuer reinterpretation is not allowed",
                        candidate_entity_ids=[org],
                    )
            index_value, index_field = _first(cleaned, "cu_base_index", "ref_base_index")
            if index_value and _atomic_index(index_value):
                index_id = self._index(rows, prefix, index_value)
                self._index_relation(rows, primary_id, "HAS_UNDERLYING_INDEX", index_id, snapshot_id, assertions.get(index_field))
                if prefix == "PREF02N001" and str(cleaned.get("cu_index_tracking_yn") or "").upper() == "Y":
                    self._index_relation(rows, primary_id, "TRACKS_INDEX", index_id, snapshot_id, assertions.get("cu_index_tracking_yn") or assertions.get(index_field))
            self._currency_and_country_relations(
                rows, prefix, primary_id, snapshot_id, cleaned, assertions
            )
            return
        self._entity_relation(rows, support_id, "HAS_SHARE_CLASS", primary_id, snapshot_id, assertions.get("rptt_ksd_itm_no") or assertions.get("itm_no"))
        for field_name, relation, org_type in (("or_co_xtn_itt_cd", "MANAGED_BY", "ASSET_MANAGER"), ("trusc_xtn_itt_cd", "HAS_TRUSTEE", "TRUSTEE")):
            value = _value(cleaned.get(field_name))
            if not value:
                continue
            org = self._validated_organization(
                rows,
                org_type,
                "PRFD01N001",
                value,
                record_id,
                field_name,
                support_id,
                relation,
            )
            if org and not audit.conflicting(support_id, field_name, fund=True):
                self._organization_relation(rows, support_id, relation, org, snapshot_id, assertions.get(field_name))
        benchmark = _value(cleaned.get("bmrk_nm"))
        if benchmark and _atomic_index(benchmark):
            index_id = self._index(rows, "PRFD01N001", benchmark)
            if not audit.conflicting(support_id, "bmrk_nm", fund=True):
                self._index_relation(rows, support_id, "HAS_BENCHMARK", index_id, snapshot_id, assertions.get("bmrk_nm"))
        self._currency_and_country_relations(
            rows, prefix, primary_id, snapshot_id, cleaned, assertions
        )

    def _currency_and_country_relations(
        self,
        rows: _Rows,
        prefix: str,
        subject: str,
        snapshot_id: str,
        cleaned: dict[str, Any],
        assertions: dict[str, str],
    ) -> None:
        denomination_field = "pd_curr_cd" if prefix.startswith("PREF") else "curr_cd"
        denomination = _currency_code(cleaned.get(denomination_field))
        if denomination:
            target = self._controlled_entity(rows, "CURRENCY", denomination)
            if prefix != "PRFD01N001":
                self._entity_relation(
                    rows, subject, "DENOMINATED_IN", target, snapshot_id,
                    assertions.get(denomination_field),
                )
        if prefix == "PREF02N001":
            traded = _currency_code(cleaned.get("pd_trd_ccy"))
            if traded:
                target = self._controlled_entity(rows, "CURRENCY", traded)
                self._entity_relation(
                    rows, subject, "TRADED_IN_CURRENCY", target, snapshot_id,
                    assertions.get("pd_trd_ccy"),
                )
        if prefix in {"PREF01N001", "PREF02N001"}:
            country = _country_code(cleaned.get("pd_mkt_id"))
            if country:
                target = self._controlled_entity(rows, "COUNTRY", country)
                self._entity_relation(
                    rows, subject, "LISTED_IN_COUNTRY", target, snapshot_id,
                    assertions.get("pd_mkt_id"),
                )
        elif prefix == "PRBD01N001":
            country = _country_code(cleaned.get("pd_ctry_cd"))
            if country:
                target = self._controlled_entity(rows, "COUNTRY", country)
                self._entity_relation(
                    rows, subject, "HAS_INSTRUMENT_COUNTRY", target,
                    snapshot_id, assertions.get("pd_ctry_cd"),
                )

    def _controlled_entity(self, rows: _Rows, kind: str, code: str) -> str:
        entity_id = f"{kind.casefold()}:{code}"
        self._entity(rows, entity_id, kind, code, "AUTHORITATIVE", False)
        return entity_id

    def _organization(self, rows: _Rows, org_type: str, dataset: str, value: str) -> str:
        entity_id = source_scoped_name_id(org_type, dataset, value)
        self._entity(rows, entity_id, "ORGANIZATION", value, "SOURCE_ONLY", False)
        rows.add(organizations, {"organization_id": entity_id, "organization_type": org_type})
        return entity_id

    def _validated_organization(
        self,
        rows: _Rows,
        org_type: str,
        dataset: str,
        value: str,
        record_id: str,
        source_field: str,
        subject_id: str | None,
        relation: str,
    ) -> str | None:
        rejection = _organization_target_rejection_reason(value)
        if rejection:
            self._add_relation_resolution_case(
                rows,
                record_id,
                source_field,
                value,
                subject_id,
                relation,
                "INVALID_ORGANIZATION_TARGET",
                "REJECTED",
                rejection,
            )
            return None
        return self._organization(rows, org_type, dataset, value)

    def _add_relation_resolution_case(
        self,
        rows: _Rows,
        record_id: str,
        source_field: str,
        raw_value: str,
        subject_id: str | None,
        relation: str,
        reason_code: str,
        status: str,
        notes: str,
        *,
        candidate_entity_ids: list[str] | None = None,
    ) -> None:
        rows.add(identity_resolution_cases, {
            "resolution_case_id": _stable_id(
                "relation-resolution", record_id, source_field, reason_code
            ),
            "source_record_id": record_id,
            "raw_identity": {
                "source_field": source_field,
                "raw_value": raw_value,
                "subject_entity_id": subject_id,
                "intended_relation": relation,
            },
            "candidate_entity_ids": candidate_entity_ids or [],
            "resolution_status": status,
            "reason_code": reason_code,
            "resolution_rule": notes,
        })

    def _index(self, rows: _Rows, dataset: str, value: str) -> str:
        entity_id = source_scoped_name_id("index", dataset, value)
        self._entity(rows, entity_id, "INDEX", value, "SOURCE_ONLY", False)
        rows.add(indices, {"index_id": entity_id, "resolution_status": "RESOLVED"})
        return entity_id

    def _entity_relation(self, rows: _Rows, subject: str | None, relation: str, target: str, snapshot_id: str, assertion: str | None) -> None:
        if not subject or not assertion:
            return
        fact = self._fact(rows, subject, snapshot_id, "ENTITY_RELATION", f"{relation}:{target}")
        rows.add(entity_relations, {"fact_id": fact, "subject_entity_id": subject, "relation_type": relation, "object_entity_id": target})
        self._evidence(rows, fact, assertion)

    def _organization_relation(self, rows: _Rows, subject: str | None, relation: str, target: str, snapshot_id: str, assertion: str | None) -> None:
        if not subject or not assertion:
            return
        fact = self._fact(rows, subject, snapshot_id, "ORGANIZATION_RELATION", f"{relation}:{target}")
        rows.add(organization_relations, {"fact_id": fact, "subject_product_id": subject, "relation_type": relation, "organization_id": target})
        self._evidence(rows, fact, assertion)

    def _index_relation(self, rows: _Rows, subject: str, relation: str, target: str, snapshot_id: str, assertion: str | None) -> None:
        if not assertion:
            return
        fact = self._fact(rows, subject, snapshot_id, "INDEX_RELATION", f"{relation}:{target}")
        rows.add(index_relations, {"fact_id": fact, "subject_product_id": subject, "relation_type": relation, "index_id": target})
        self._evidence(rows, fact, assertion)

    def _metrics(self, rows: _Rows, prefix: str, primary_id: str | None, support_id: str | None, snapshot_id: str, cleaned: dict[str, Any], assertions: dict[str, str]) -> None:
        subject = support_id if prefix == "PRBD01N001" and primary_id is None else primary_id
        if subject is None:
            return
        currency = _currency_code(cleaned.get("pd_curr_cd") or cleaned.get("pd_trd_ccy") or cleaned.get("curr_cd"))
        for field_name, (metric_code, unit, scale, date_field) in METRIC_FIELDS[prefix].items():
            value = _decimal(cleaned.get(field_name))
            assertion = assertions.get(field_name)
            if value is None or assertion is None:
                continue
            observed = _date(cleaned.get(date_field))
            key = f"{metric_code}:{observed or SNAPSHOT}:{_stable_id(assertion)[:12]}"
            fact = self._fact(rows, subject, snapshot_id, "METRIC", key)
            rows.add(metric_observations, {
                "fact_id": fact, "metric_code": metric_code,
                "subject_entity_id": subject, "raw_value": str(cleaned[field_name]),
                "numeric_value": value, "unit": unit, "scale_basis": scale,
                "currency": currency, "observed_on": observed,
                "quality_status": (
                    "VALID" if metric_code == "VOLUME" or value != 0 else "SOURCE_ZERO"
                ),
                "comparability_status": (
                    "COMPARABLE"
                    if (
                        metric_code == "AUM"
                        and prefix in {"PREF01N001", "PREF02N001"}
                        and currency in {"KRW", "USD"}
                    )
                    or (
                        metric_code == "ONE_YEAR_RETURN"
                        and prefix in {"PREF01N001", "PRFD01N001"}
                    )
                    else "NOT_COMPARABLE"
                ),
            })
            self._evidence(rows, fact, assertion)
        if prefix != "PRBD01N001":
            return
        rating = (_value(cleaned.get("crd_grd")) or "").upper()
        rating_rank = {
            value: rank
            for rank, value in enumerate(
                (
                    "C0", "B-", "B+", "BB-", "BB0", "BBB-", "BBB0",
                    "BBB+", "A-", "A0", "A+", "AA-", "AA0", "AA+", "AAA",
                ),
                start=1,
            )
        }.get(rating)
        if support_id and rating_rank is not None and assertions.get("crd_grd"):
            fact = self._fact(rows, support_id, snapshot_id, "METRIC", "CREDIT_RATING_ORDER")
            rows.add(metric_observations, {
                "fact_id": fact, "metric_code": "CREDIT_RATING_ORDER",
                "subject_entity_id": support_id, "raw_value": rating,
                "numeric_value": rating_rank, "unit": "ORDINAL",
                "scale_basis": "CREDIT_RATING_V1", "currency": None,
                "observed_on": _date(cleaned.get("crd_grd_dt")) or _date(cleaned.get("info_base_dt")),
                "quality_status": "VALID", "comparability_status": "COMPARABLE",
            })
            self._evidence(rows, fact, assertions["crd_grd"])


    def _fact(self, rows: _Rows, subject: str, snapshot_id: str, kind: str, key: str, resolution: str = "RESOLVED") -> str:
        fact_id = _stable_id("fact", subject, snapshot_id, kind, key)
        rows.add(canonical_facts, {
            "fact_id": fact_id, "subject_entity_id": subject,
            "snapshot_id": snapshot_id, "fact_kind": kind,
            "semantic_key": key, "resolution_status": resolution,
        })
        return fact_id

    def _evidence(
        self, rows: _Rows, fact_id: str, assertion_id: str,
        *, role: str = "SUPPORTS",
    ) -> None:
        rows.add(fact_evidence_links, {
            "fact_id": fact_id, "assertion_id": assertion_id,
            "evidence_role": role,
        })

    def _add_collision_cases(self, rows: _Rows, audit: Audit) -> None:
        for (scheme, namespace, value), owners in audit.identifier_owners.items():
            if len(owners) > 1:
                rows.add(identifier_collision_cases, {
                    "collision_case_id": _stable_id("identifier-collision", scheme, namespace, value),
                    "scheme_code": scheme, "namespace": namespace,
                    "normalized_value": value, "candidate_entity_ids": sorted(owners),
                    "status": "OPEN", "resolution_notes": "actual 260824 collision; no automatic merge",
                })

    def _add_conflict_cases(self, rows: _Rows, audit: Audit, snapshot_ids: dict[str, str]) -> None:
        for (entity_id, field_name), values in audit.bond_values.items():
            if len(values) <= 1:
                continue
            candidate_ids = []
            for value in sorted(values):
                fact_id = self._fact(rows, entity_id, snapshot_ids["PRBD01N001"], "SCALAR", f"conflict:{field_name}:{normalize_lookup_value(value)}", "CONFLICT")
                candidate_ids.append(fact_id)
                for assertion_id in audit.conflict_assertions.get(
                    (entity_id, field_name, value), set()
                ):
                    self._evidence(rows, fact_id, assertion_id)
            rows.add(fact_conflict_cases, {
                "conflict_case_id": _stable_id("fact-conflict", entity_id, field_name),
                "subject_entity_id": entity_id, "semantic_key": field_name,
                "candidate_fact_ids": candidate_ids, "status": "UNRESOLVED",
                "resolution_notes": "non-null source rows disagree; no first-row or majority selection",
            })
        for (fund_id, field_name), values in audit.fund_values.items():
            if len(values) <= 1:
                continue
            candidate_ids = []
            for value in sorted(values):
                fact_id = self._fact(rows, fund_id, snapshot_ids["PRFD01N001"], "SCALAR", f"conflict:{field_name}:{normalize_lookup_value(value)}", "CONFLICT")
                candidate_ids.append(fact_id)
                for assertion_id in audit.conflict_assertions.get(
                    (fund_id, field_name, value), set()
                ):
                    self._evidence(rows, fact_id, assertion_id)
            rows.add(fact_conflict_cases, {
                "conflict_case_id": _stable_id("fact-conflict", fund_id, field_name),
                "subject_entity_id": fund_id, "semantic_key": field_name,
                "candidate_fact_ids": candidate_ids, "status": "UNRESOLVED",
                "resolution_notes": "member share-class values disagree; no Fund-level promotion",
            })

    def _add_crosswalk(self, rows: _Rows, audit: Audit) -> None:
        inspector = inspect(self.engine)
        if not inspector.has_table("canonical_products"):
            return
        with self.engine.connect() as connection:
            for entity_id, product_type in connection.execute(select(v1_products.c.canonical_product_id, v1_products.c.product_type).where(v1_products.c.dataset_snapshot == SNAPSHOT)):
                entity_id = str(entity_id)
                if entity_id.startswith("fund_pub:"):
                    target = entity_id if entity_id in audit.fund_class_ids else None
                    status = "EXACT" if target else "RETIRED"
                    target_type = "FUND_SHARE_CLASS" if target else "NO_CANONICAL_TARGET"
                elif entity_id in audit.product_ids:
                    target, status, target_type = entity_id, "EXACT", "FINANCIAL_PRODUCT"
                else:
                    continue
                rows.add(entity_id_crosswalk, {
                    "v1_entity_id": entity_id, "v1_entity_type": str(product_type),
                    "v2_entity_id": target, "mapping_status": status,
                    "mapping_basis": f"260824 audited identity; v2 kind={target_type}",
                })
            if inspector.has_table("funds"):
                for fund_id in connection.scalars(select(v1_funds.c.fund_id).where(v1_funds.c.dataset_snapshot == SNAPSHOT)):
                    if str(fund_id) in audit.fund_ids:
                        rows.add(entity_id_crosswalk, {"v1_entity_id": str(fund_id), "v1_entity_type": "Fund", "v2_entity_id": str(fund_id), "mapping_status": "EXACT", "mapping_basis": "representative identifier preserved"})
        for sale_lot_id in audit.sale_lot_ids:
            rows.add(entity_id_crosswalk, {"v1_entity_id": sale_lot_id, "v1_entity_type": "GRAPH_SALE_LOT", "v2_entity_id": sale_lot_id, "mapping_status": "EXACT", "mapping_basis": "stable graph SaleLot ID promoted to PostgreSQL"})

    def _ensure_metric_definitions(self, connection) -> None:
        definitions = (
            ("NAV", "product.nav", "Net asset value"),
            ("PRICE", "product.price", "Observed source price"),
            ("MARKET_PRICE", "product.market_price", "Observed market price"),
            ("VOLUME", "product.market_volume", "Observed market volume"),
            ("BOND_BUY_YIELD", "bond.buy_yield", "Bond buy yield"),
            ("ONE_YEAR_RETURN", "product.one_year_return", "Exact one-year source return"),
            ("CREDIT_RATING_ORDER", "product.credit_rating", "Ordered credit rating"),
        )
        connection.execute(
            pg_insert(metric_definitions).values([
                {"metric_code": code, "canonical_field": field_name, "label": label, "value_type": "NUMERIC", "cross_source_comparable": False, "filter_enabled": False, "sort_enabled": False}
                for code, field_name, label in definitions
            ]).on_conflict_do_nothing()
        )

    def _reconcile(
        self, connection, *, expected_sale_lots: int | None = None
    ) -> dict[str, int]:
        counts = {
            "financial_products": connection.scalar(select(func.count()).select_from(financial_products)),
            "bonds": connection.scalar(select(func.count()).select_from(bonds)),
            "funds": connection.scalar(select(func.count()).select_from(funds)),
            "fund_share_classes": connection.scalar(select(func.count()).select_from(fund_share_classes)),
            "sale_lots": connection.scalar(select(func.count()).select_from(sale_lots)),
            "etf": connection.scalar(select(func.count()).select_from(exchange_traded_products).where(exchange_traded_products.c.product_type_code == "ETF")),
            "etn": connection.scalar(select(func.count()).select_from(exchange_traded_products).where(exchange_traded_products.c.product_type_code == "ETN")),
            "unresolved_fund_rows": connection.scalar(select(func.count()).select_from(identity_resolution_cases).where(identity_resolution_cases.c.reason_code == "UNRESOLVED_PARENT")),
        }
        expected_counts = {
            **EXPECTED_CANONICAL_COUNTS,
            "sale_lots": (
                counts["sale_lots"]
                if expected_sale_lots is None
                else expected_sale_lots
            ),
        }
        if counts != expected_counts:
            raise ValueError(f"canonical reconciliation mismatch: {counts}")
        source_count = connection.scalar(select(func.count()).select_from(source_records))
        quarantine_count = connection.scalar(select(func.count()).select_from(quarantine_records))
        described = connection.scalar(select(func.count()).select_from(source_record_entities).where(source_record_entities.c.provenance_role == "DESCRIBES"))
        supports = connection.scalar(select(func.count()).select_from(source_record_entities).where(source_record_entities.c.provenance_role == "SUPPORTS"))
        if (source_count, quarantine_count, described, supports) != (53_374, 1, 25_024, 38_456):
            raise ValueError("source/provenance reconciliation mismatch")
        orphan_classes = connection.scalar(select(func.count()).select_from(fund_share_classes).outerjoin(funds, fund_share_classes.c.parent_fund_id == funds.c.fund_id).where(funds.c.fund_id.is_(None)))
        orphan_lots = connection.scalar(select(func.count()).select_from(sale_lots).outerjoin(bonds, sale_lots.c.bond_id == bonds.c.bond_id).where(bonds.c.bond_id.is_(None)))
        evidence_free = connection.scalar(select(func.count()).select_from(canonical_facts).outerjoin(fact_evidence_links, canonical_facts.c.fact_id == fact_evidence_links.c.fact_id).where(canonical_facts.c.resolution_status == "RESOLVED", fact_evidence_links.c.fact_id.is_(None)))
        if orphan_classes or orphan_lots or evidence_free:
            raise ValueError(f"integrity reconciliation failed: class_orphans={orphan_classes}, lot_orphans={orphan_lots}, evidence_free={evidence_free}")
        domain_violations = relation_domain_violations(connection)
        if domain_violations:
            rendered = [asdict(violation) for violation in domain_violations]
            raise ValueError(
                "canonical relation-domain validation failed: "
                + json.dumps(rendered, ensure_ascii=False, sort_keys=True)
            )
        return {key: int(value or 0) for key, value in counts.items()}

    def _mark_ready(self, connection, audit: Audit, snapshot_ids: dict[str, str], counts: dict[str, int]) -> None:
        classification = _classification_json(self.classification_counts)
        connection.execute(
            dataset_snapshots.update()
            .where(dataset_snapshots.c.snapshot_id.in_(snapshot_ids.values()))
            .values(status="READY", reconciliation_status="PASSED", row_count_reconciled=True, metadata_json={"generation": GENERATION, "canonical_counts": counts, "classification_accounting": classification})
        )
        connection.execute(
            ingestion_runs.update()
            .where(ingestion_runs.c.snapshot_id.in_(snapshot_ids.values()))
            .values(status="SUCCEEDED", completed_at=func.now(), report={"canonical_counts": counts})
        )

    def _report(self, audit: Audit, *, status: str, skipped: bool) -> RebuildReport:
        with self.engine.connect() as connection:
            def count(table) -> int:
                return int(connection.scalar(select(func.count()).select_from(table)) or 0)
            canonical_counts = {
                "FinancialProduct": count(financial_products), "Bond": count(bonds),
                "ETF": int(connection.scalar(select(func.count()).select_from(exchange_traded_products).where(exchange_traded_products.c.product_type_code == "ETF")) or 0),
                "ETN": int(connection.scalar(select(func.count()).select_from(exchange_traded_products).where(exchange_traded_products.c.product_type_code == "ETN")) or 0),
                "Fund": count(funds), "FundShareClass": count(fund_share_classes),
                "SaleLot": count(sale_lots), "Organization": count(organizations),
                "Index": count(indices),
                "Currency": int(connection.scalar(select(func.count()).select_from(canonical_entities).where(canonical_entities.c.entity_kind == "CURRENCY")) or 0),
                "Country": int(connection.scalar(select(func.count()).select_from(canonical_entities).where(canonical_entities.c.entity_kind == "COUNTRY")) or 0),
            }
            relation_counts = {str(key): int(value) for key, value in connection.execute(select(entity_relations.c.relation_type, func.count()).group_by(entity_relations.c.relation_type))}
            for table in (organization_relations, index_relations):
                for key, value in connection.execute(select(table.c.relation_type, func.count()).group_by(table.c.relation_type)):
                    relation_counts[str(key)] = relation_counts.get(str(key), 0) + int(value)
            classification_relations = {
                "ASSET_CLASS": "HAS_ASSET_CLASS",
                "EXPOSURE_REGION": "HAS_EXPOSURE_REGION",
                "MARKET_SCOPE": "HAS_MARKET_SCOPE",
                "RISK_GRADE": "HAS_RISK_GRADE",
                "BOND_TYPE": "HAS_BOND_TYPE",
                "OFFERING_TYPE": "HAS_OFFERING_TYPE",
            }
            for key, value in connection.execute(
                select(entity_classifications.c.classification_type, func.count())
                .group_by(entity_classifications.c.classification_type)
            ):
                relation = classification_relations.get(str(key), f"HAS_{key}")
                relation_counts[relation] = int(value)
            for relation in (
                "HAS_SHARE_CLASS", "HAS_SALE_LOT", "MANAGED_BY", "ISSUED_BY",
                "HAS_TRUSTEE", "HAS_UNDERLYING_INDEX", "TRACKS_INDEX",
                "HAS_BENCHMARK", "DENOMINATED_IN", "TRADED_IN_CURRENCY",
                "LISTED_IN_COUNTRY", "HAS_INSTRUMENT_COUNTRY",
                *classification_relations.values(),
            ):
                relation_counts.setdefault(relation, 0)
            provenance = {"SourceRecords": count(source_records), "DESCRIBES": int(connection.scalar(select(func.count()).select_from(source_record_entities).where(source_record_entities.c.provenance_role == "DESCRIBES")) or 0), "SUPPORTS": int(connection.scalar(select(func.count()).select_from(source_record_entities).where(source_record_entities.c.provenance_role == "SUPPORTS")) or 0), "SourceFieldAssertions": count(source_field_assertions), "fact_evidence_links": count(fact_evidence_links)}
            identifiers = {"observations": count(entity_identifiers), "collision_cases": count(identifier_collision_cases), "validated": int(connection.scalar(select(func.count()).select_from(entity_identifiers).where(entity_identifiers.c.validation_status == "VALIDATED")) or 0), "conflicts": int(connection.scalar(select(func.count()).select_from(entity_identifiers).where(entity_identifiers.c.conflict_status == "OPEN")) or 0)}
            crosswalk = {str(key): int(value) for key, value in connection.execute(select(entity_id_crosswalk.c.mapping_status, func.count()).group_by(entity_id_crosswalk.c.mapping_status))}
            conflicts = {"fact_conflict_cases": count(fact_conflict_cases)}
            unresolved = int(connection.scalar(select(func.count()).select_from(identity_resolution_cases).where(identity_resolution_cases.c.reason_code == "UNRESOLVED_PARENT")) or 0)
            metric_counts = {
                str(key): int(value)
                for key, value in connection.execute(
                    select(metric_observations.c.metric_code, func.count())
                    .group_by(metric_observations.c.metric_code)
                )
            }
            metric_status = {
                str(key): int(value)
                for key, value in connection.execute(
                    select(metric_observations.c.comparability_status, func.count())
                    .group_by(metric_observations.c.comparability_status)
                )
            }
            classification = _classification_json(self.classification_counts)
            if not classification:
                metadata_json = connection.scalar(
                    select(dataset_snapshots.c.metadata_json)
                    .where(dataset_snapshots.c.status == "READY")
                    .limit(1)
                ) or {}
                classification = metadata_json.get("classification_accounting", {})
        return RebuildReport(
            status=status,
            skipped=skipped,
            datasets=audit.datasets,
            canonical_counts=canonical_counts,
            unresolved_rows=unresolved,
            classification_accounting=classification,
            relation_counts=relation_counts,
            provenance_counts=provenance,
            conflict_counts=conflicts,
            identifier_counts=identifiers,
            crosswalk_counts=crosswalk,
            metric_counts=metric_counts,
            metric_status=metric_status,
        )


def _classification_json(value) -> dict[str, dict[str, dict[str, int]]]:
    return {dataset: {category: dict(counter) for category, counter in categories.items()} for dataset, categories in value.items()}


def _classification_inputs(
    prefix: str, cleaned: dict[str, Any]
) -> list[tuple[str, str, str]]:
    if prefix == "PRBD01N001":
        return [
            ("pd_no", "asset_class", "채권"),
            ("pd_risk_nm", "risk_grade", str(cleaned.get("pd_risk_nm") or cleaned.get("pd_risk_gcd") or "")),
            ("bd_knd", "bond_type", str(cleaned.get("bd_knd") or "")),
            ("bd_ofr_tcd", "offering_type", str(cleaned.get("bd_ofr_tcd") or "")),
        ]
    if prefix in {"PREF01N001", "PREF02N001"}:
        return [
            ("wu_inv_ast_type", "asset_class", str(cleaned.get("wu_inv_ast_type") or "")),
            ("wu_inv_rgn", "exposure_region", str(cleaned.get("wu_inv_rgn") or "")),
            ("pd_risk_nm", "risk_grade", str(cleaned.get("pd_risk_nm") or "")),
        ]
    return [
        ("or_attr_desc", "asset_class", str(cleaned.get("or_attr_desc") or "")),
        ("fd_ivst_rgn_desc", "exposure_region", str(cleaned.get("fd_ivst_rgn_desc") or "")),
        ("ovrs_fd_desc", "market_scope", str(cleaned.get("ovrs_fd_desc") or "")),
        ("zrin_fd_ivst_risk_grd_nm", "risk_grade", str(cleaned.get("zrin_fd_ivst_risk_grd_nm") or cleaned.get("zrin_fd_ivst_risk_gcd") or "")),
        ("prvo_pbff_desc", "offering_type", str(cleaned.get("prvo_pbff_desc") or "")),
        ("sale_yn", "subscription_status", str(cleaned.get("sale_yn") or "")),
    ]


def _stable_id(*parts: object) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _payload_hash(raw: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(_json_payload(raw), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _json_payload(raw: dict[str, Any]) -> dict[str, Any]:
    return {key: json_value(value) for key, value in raw.items()}


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _value(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _identifier_value(value: Any) -> str | None:
    result = _value(value)
    if result is None:
        return None
    normalized = result.upper()
    if len(normalized) >= 3 and set(normalized) <= {"0"}:
        return None
    return normalized


def _identifier_validation(scheme: str, value: str) -> str:
    if scheme == "ISIN":
        return "VALIDATED" if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", value) else "INVALID"
    if scheme in {"SOURCE_ID", "REPRESENTATIVE_KSD_ID"}:
        return "VALIDATED"
    return "UNVALIDATED"


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _date(value: Any) -> date | None:
    raw = _value(value)
    if raw is None or raw.replace("-", "") in {"0", "00000000", "99991231", "10001231"}:
        return None
    compact = raw.replace(".", "-").replace("/", "-")
    if re.fullmatch(r"\d{8}", compact):
        compact = f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
    try:
        return date.fromisoformat(compact[:10])
    except ValueError:
        return None


def _first(row: dict[str, Any], *fields: str) -> tuple[str | None, str]:
    for field_name in fields:
        value = _value(row.get(field_name))
        if value:
            return value, field_name
    return None, fields[0]


def _atomic_index(value: str) -> bool:
    normalized = value.casefold()
    if any(item in normalized for item in ("not provided", "not available", "해당없음", "없음")):
        return False
    return "+" not in value and not any(weight in normalized for weight in (" 25%", " 50%", " 75%", " 90%"))


def _etp_sale_status(value: Any) -> str:
    raw = _value(value)
    if raw == "1":
        return "AVAILABLE_FOR_SALE"
    if raw == "0":
        return "NOT_AVAILABLE_FOR_SALE"
    return "UNKNOWN"


def _etp_trading_status(value: Any) -> str:
    raw = _value(value)
    if raw == "0":
        return "TRADING_ACTIVE"
    if raw == "1":
        return "TRADING_HALTED"
    return "UNKNOWN"


def _is_no_known_end_date(value: Any) -> bool:
    raw = _value(value)
    return raw is not None and raw.replace("-", "") == "99991231"


def _etp_insufficient_reasons(cleaned: dict[str, Any]) -> tuple[str, ...]:
    """Return unresolved core availability inputs, excluding vendor linkage.

    Price freshness is represented by its own canonical predicate. Missing
    Refinitiv identifiers are also not evidence that an otherwise identified
    ETP cannot be assessed for sale and trading availability.
    """
    reasons = []
    if _etp_sale_status(cleaned.get("pd_sale_yn")) == "UNKNOWN":
        reasons.append("SALE_STATUS_MISSING")
    if _etp_trading_status(cleaned.get("pd_tr_yn")) == "UNKNOWN":
        reasons.append("TRADING_STATUS_MISSING")
    if _date(cleaned.get("pd_lstg_dt")) is None:
        reasons.append("LISTING_START_DATE_MISSING")
    return tuple(reasons)


def _organization_target_rejection_reason(value: str | None) -> str | None:
    """Reject only explicit product-designator suffixes.

    Legal organization names can contain security-company words, numeric
    series, or fund-related terms.  Those are intentionally not guessed here;
    an explicit trailing ``(ETF)``/``(ETN)`` is the conservative product signal
    demonstrated by the authoritative source.
    """

    if value and _PRODUCT_DESIGNATOR_SUFFIX.search(value):
        return (
            "rejected organization target: value ends with an explicit "
            "ETF/ETN product designator"
        )
    return None


def _entity_kind(prefix: str, *, primary: bool) -> str:
    if prefix == "PRBD01N001" and primary:
        return "SALE_LOT"
    if prefix == "PRFD01N001" and primary:
        return "FUND_SHARE_CLASS"
    return "FINANCIAL_PRODUCT"


def _field_category(prefix: str, field_name: str) -> str:
    if any(field_name == item[0] for item in IDENTIFIER_FIELDS[prefix]) or field_name == "rptt_ksd_itm_no":
        return "IDENTITY"
    if field_name in METRIC_FIELDS[prefix]:
        return "METRIC"
    if field_name in {"pd_pbcm", "cu_fund_mgmt_co", "or_co_xtn_itt_cd", "trusc_xtn_itt_cd", "cu_base_index", "ref_base_index", "bmrk_nm"}:
        return "RELATION"
    if field_name in {"wu_inv_ast_type", "wu_inv_rgn", "pd_risk_nm", "pd_risk_gcd", "bd_knd", "bd_ofr_tcd", "or_attr_desc", "fd_ivst_rgn_desc", "ovrs_fd_desc", "zrin_fd_ivst_risk_gcd", "zrin_fd_ivst_risk_grd_nm", "prvo_pbff_desc"}:
        return "CLASSIFICATION"
    return "SCALAR"


def _target_key(prefix: str, field_name: str, value: Any) -> str:
    if prefix == "PRFD01N001" and field_name == "bmrk_nm":
        benchmark = _value(value)
        if benchmark and not _atomic_index(benchmark):
            return "benchmark:COMPOSITE_UNRESOLVED"
        return "benchmark"
    relation_keys = {
        "pd_pbcm": "issuedBy",
        "cu_fund_mgmt_co": "managedBy",
        "or_co_xtn_itt_cd": "managedBy",
        "trusc_xtn_itt_cd": "hasTrustee",
        "curr_cd": "denominatedIn",
        "pd_curr_cd": "denominatedIn",
        "pd_trd_ccy": "tradedInCurrency",
        "pd_mkt_id": "listedInCountry",
        "pd_ctry_cd": "hasInstrumentCountry",
        "pd_grp_no": "product.type",
        "pd_sale_yn": "etp_distribution_status",
        "pd_tr_yn": "etp_trading_status",
        "pd_lstg_dt": "listing_start_date",
        "pd_lste_dt": "listing_end_date",
        "du_clpr_base_dt": "price_observation_date",
        "du_upt_dt": "source_update_date",
        "ru_mkt_price": "market_price",
        "ru_mkt_volume": "market_volume",
    }
    if field_name in relation_keys:
        return relation_keys[field_name]
    return field_name


def _currency_code(value: Any) -> str | None:
    normalized = _identifier_value(value)
    if normalized and normalized.startswith("CURR_CD_"):
        normalized = normalized.removeprefix("CURR_CD_")
    if normalized and re.fullmatch(r"[A-Z]{3}", normalized):
        return normalized
    return None


def _country_code(value: Any) -> str | None:
    normalized = _identifier_value(value)
    if normalized and re.fullmatch(r"[A-Z]{2}", normalized):
        return normalized
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="M10.8-B canonical v2 clean rebuild")
    parser.add_argument("--material-root", type=Path, default=Path("material"))
    parser.add_argument("--database-url")
    parser.add_argument("--batch-size", type=int, default=1_000)
    parser.add_argument("--report-file", type=Path)
    parser.add_argument(
        "--force-failure-stage",
        choices=("after_initialization", "before_reconciliation"),
    )
    args = parser.parse_args()
    settings = DatabaseSettings(database_url=args.database_url) if args.database_url else DatabaseSettings.from_env()
    engine = create_database_engine(settings)
    try:
        report = CanonicalV2Rebuilder(engine, batch_size=args.batch_size).rebuild(
            args.material_root, force_failure_stage=args.force_failure_stage
        )
        rendered = json.dumps(report.as_dict(), ensure_ascii=False, indent=2)
        print(rendered)
        if args.report_file:
            args.report_file.write_text(rendered + "\n", encoding="utf-8")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

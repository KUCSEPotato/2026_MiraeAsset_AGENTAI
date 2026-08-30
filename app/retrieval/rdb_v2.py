"""Safe canonical_v2 PostgreSQL repository for structured QueryPlan steps.

This module is intentionally side-by-side with the v1 RDB compiler.  It reads
only canonical_v2 canonical entities/facts and never falls back to v1 tables.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any, Iterable

from sqlalchemy import (
    Engine,
    Select,
    and_,
    asc,
    exists,
    distinct,
    func,
    not_,
    or_,
    select,
)
from sqlalchemy.exc import SQLAlchemyError

from app.data.cleaning import normalize_lookup_value
from app.data.v2_schema import (
    CANONICAL_V2_SCHEMA_VERSION,
    canonical_entities,
    canonical_facts,
    canonical_scalar_facts,
    dataset_snapshots,
    entity_aliases,
    entity_classifications,
    entity_identifiers,
    entity_relations,
    fact_evidence_links,
    financial_products,
    fund_share_classes,
    index_relations,
    metric_definitions,
    metric_observations,
    ontology_concepts,
    organization_relations,
    sale_lots,
    source_datasets,
    source_field_assertions,
    source_records,
)
from app.domain.models import (
    ExecutionContext,
    FilterOperator,
    QueryOperation,
    QueryStep,
    RetrievalRecord,
    RetrievalResult,
    RetrievalSource,
)
from app.ontology.runtime_mapping import TeamOntologyRuntimeMapping
from app.retrieval.exceptions import (
    RDBQueryCompilationError,
    RetrieverUnavailableError,
    RetrievalError,
)


class V2SnapshotUnavailableError(RetrievalError):
    """No deterministic READY/PASSED canonical_v2 snapshot is available."""


class V2ResultGrain(str, Enum):
    FINANCIAL_PRODUCT = "financial_product"
    FUND_SHARE_CLASS = "fund_share_class"
    SALE_LOT = "sale_lot"


@dataclass(frozen=True, slots=True)
class V2SnapshotSelection:
    snapshot_date: date
    generation: str
    ontology_version: str
    snapshot_ids: tuple[str, ...]
    dataset_ids: tuple[str, ...]

    @property
    def identity(self) -> str:
        return f"{self.generation}:{self.snapshot_date.isoformat()}"


class CanonicalV2SnapshotSelector:
    REQUIRED_DATASETS = frozenset(
        {"PRBD01N001", "PREF01N001", "PREF02N001", "PRFD01N001"}
    )

    def __init__(
        self,
        *,
        snapshot_date: str,
        generation: str = "260824",
        ontology_version: str = "merged-optical-1.3",
        transformer_version: str = "m10.8-b2-relations-v2",
        schema_version: str = CANONICAL_V2_SCHEMA_VERSION,
        required_datasets: Iterable[str] | None = None,
    ) -> None:
        try:
            self._snapshot_date = date.fromisoformat(snapshot_date)
        except ValueError as exc:
            raise ValueError("canonical_v2 snapshot_date must be ISO-8601") from exc
        self._generation = generation
        self._ontology_version = ontology_version
        self._transformer_version = transformer_version
        self._schema_version = schema_version
        self._required = frozenset(required_datasets or self.REQUIRED_DATASETS)

    def select(self, connection) -> V2SnapshotSelection:
        rows = connection.execute(
            select(
                dataset_snapshots.c.snapshot_id,
                dataset_snapshots.c.dataset_id,
                source_datasets.c.dataset_code,
            )
            .join(
                source_datasets,
                source_datasets.c.dataset_id == dataset_snapshots.c.dataset_id,
            )
            .where(
                dataset_snapshots.c.snapshot_date == self._snapshot_date,
                dataset_snapshots.c.generation == self._generation,
                dataset_snapshots.c.ontology_version == self._ontology_version,
                dataset_snapshots.c.transformer_version == self._transformer_version,
                dataset_snapshots.c.database_schema_version == self._schema_version,
                dataset_snapshots.c.status == "READY",
                dataset_snapshots.c.reconciliation_status == "PASSED",
                dataset_snapshots.c.row_count_reconciled.is_(True),
                source_datasets.c.dataset_code.in_(self._required),
            )
            .order_by(source_datasets.c.dataset_code, dataset_snapshots.c.snapshot_id)
        ).mappings().all()
        by_code: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_code.setdefault(str(row["dataset_code"]), []).append(dict(row))
        missing = self._required - set(by_code)
        duplicates = {key for key, values in by_code.items() if len(values) != 1}
        if missing or duplicates:
            details = []
            if missing:
                details.append("missing=" + ",".join(sorted(missing)))
            if duplicates:
                details.append("ambiguous=" + ",".join(sorted(duplicates)))
            raise V2SnapshotUnavailableError(
                "canonical_v2 READY/PASSED snapshot unavailable: " + "; ".join(details)
            )
        selected = [by_code[key][0] for key in sorted(by_code)]
        return V2SnapshotSelection(
            snapshot_date=self._snapshot_date,
            generation=self._generation,
            ontology_version=self._ontology_version,
            snapshot_ids=tuple(str(row["snapshot_id"]) for row in selected),
            dataset_ids=tuple(str(row["dataset_id"]) for row in selected),
        )


@dataclass(frozen=True, slots=True)
class V2FieldMapping:
    canonical_field: str
    kind: str
    semantic_key: str
    filter_enabled: bool = True
    project_enabled: bool = True
    sort_enabled: bool = False


class CanonicalV2FieldRegistry:
    """Allow-listed semantic-to-canonical_v2 mapping (never source columns)."""

    def __init__(self) -> None:
        mappings = (
            V2FieldMapping("product.name", "entity", "preferred_name"),
            V2FieldMapping("product.short_name", "alias", "SHORT_NAME"),
            V2FieldMapping("product.ticker", "identifier", "TICKER"),
            V2FieldMapping("product.isin", "identifier", "ISIN"),
            V2FieldMapping("product.product_type", "product_type", "product_type"),
            V2FieldMapping("product.asset_type", "classification", "ASSET_CLASS"),
            V2FieldMapping("product.region", "classification", "EXPOSURE_REGION"),
            V2FieldMapping("product.market_scope", "classification", "MARKET_SCOPE"),
            V2FieldMapping("product.risk_grade", "classification", "RISK_GRADE"),
            V2FieldMapping("product.bond_type", "classification", "BOND_TYPE"),
            V2FieldMapping("product.offering_type", "classification", "OFFERING_TYPE"),
            V2FieldMapping("product.asset_manager", "organization_relation", "MANAGED_BY"),
            V2FieldMapping("product.issuer", "organization_relation", "ISSUED_BY"),
            V2FieldMapping("product.trustee", "organization_relation", "HAS_TRUSTEE"),
            V2FieldMapping("product.base_index", "index_relation", "HAS_UNDERLYING_INDEX"),
            V2FieldMapping("product.tracked_index", "index_relation", "TRACKS_INDEX"),
            V2FieldMapping("product.benchmark", "index_relation", "HAS_BENCHMARK"),
            V2FieldMapping("product.currency", "entity_relation", "DENOMINATED_IN"),
            V2FieldMapping("product.trading_currency", "entity_relation", "TRADED_IN_CURRENCY"),
            V2FieldMapping("product.listing_country", "entity_relation", "LISTED_IN_COUNTRY"),
            V2FieldMapping("product.instrument_country", "entity_relation", "HAS_INSTRUMENT_COUNTRY"),
            V2FieldMapping("product.share_class", "entity_relation", "HAS_SHARE_CLASS"),
            V2FieldMapping("product.sale_lot", "entity_relation", "HAS_SALE_LOT"),
            V2FieldMapping("product.maturity", "scalar", "maturity_date", False),
            # Storage is numeric, but the approved comparison contracts are disabled.
            V2FieldMapping("product.aum", "metric", "AUM", False, True, False),
            V2FieldMapping("product.expense_ratio", "metric", "EXPENSE_RATIO", False, True, False),
        )
        self._fields = {item.canonical_field: item for item in mappings}
        runtime = TeamOntologyRuntimeMapping()
        self._concepts: dict[tuple[str, str], str] = {}
        for item in runtime.concepts:
            semantic = item.semantic_value()
            if semantic is None:
                continue
            category = item.category.upper()
            for value in (
                item.runtime_key,
                item.canonical_name,
                semantic.ontology_uri,
                *item.legacy_names,
            ):
                self._concepts[(category, value)] = semantic.ontology_uri

    def field(self, canonical_field: Any) -> V2FieldMapping:
        if not isinstance(canonical_field, str):
            raise RDBQueryCompilationError("canonical field must be a string")
        try:
            return self._fields[canonical_field]
        except KeyError as exc:
            raise RDBQueryCompilationError(
                f"unsupported canonical_v2 field: {canonical_field}"
            ) from exc

    def concept_iri(self, classification_type: str, value: Any) -> str:
        if not isinstance(value, str):
            raise RDBQueryCompilationError("classification value must be canonical")
        iri = self._concepts.get((classification_type, value))
        if iri is None:
            raise RDBQueryCompilationError(
                f"unsupported canonical_v2 classification: {classification_type}:{value}"
            )
        return iri

    @property
    def canonical_fields(self) -> frozenset[str]:
        return frozenset(self._fields)


@dataclass(frozen=True, slots=True)
class CompiledV2RDBQuery:
    statement: Select
    count_statement: Select
    projected_fields: tuple[str, ...]
    result_grain: V2ResultGrain
    ranking_applied: bool


@dataclass(frozen=True, slots=True)
class CanonicalV2RetrievalRecord:
    entity_id: str
    entity_kind: str
    product_type: str | None
    preferred_name: str | None
    name_status: str
    matched_constraints: tuple[str, ...]
    canonical_fact_ids: tuple[str, ...]
    evidence_assertion_ids: tuple[str, ...]
    source_record_ids: tuple[str, ...]
    snapshot_identity: str


_PRODUCT_TYPES = {
    "FinancialProduct.ETF": "ETF",
    "FinancialProduct.ETN": "ETN",
    "FinancialProduct.Bond": "BOND",
    "FinancialProduct.Fund": "FUND",
    "ETF": "ETF",
    "ETN": "ETN",
    "BOND": "BOND",
    "Bond": "BOND",
    "FUND": "FUND",
    "Fund": "FUND",
}
_PUBLIC_FUND = "FinancialProduct.PublicFund"
_CONTROLLED_RELATION_TARGETS = {
    "DENOMINATED_IN": {
        value: f"currency:{value}"
        for value in ("AUD", "EUR", "GBP", "INR", "JPY", "KRW", "USD")
    },
    "TRADED_IN_CURRENCY": {
        value: f"currency:{value}"
        for value in ("AUD", "EUR", "GBP", "INR", "JPY", "KRW", "USD")
    },
    "LISTED_IN_COUNTRY": {
        value: f"country:{value}" for value in ("KR", "US", "XS")
    },
    "HAS_INSTRUMENT_COUNTRY": {
        value: f"country:{value}" for value in ("KR", "US", "XS")
    },
}


class CanonicalV2QueryCompiler:
    def __init__(
        self,
        field_registry: CanonicalV2FieldRegistry,
        *,
        default_limit: int,
        max_limit: int = 10_000,
    ) -> None:
        self._fields = field_registry
        self._default_limit = default_limit
        self._max_limit = max_limit

    def compile(
        self,
        step: QueryStep,
        snapshot: V2SnapshotSelection,
    ) -> CompiledV2RDBQuery:
        if step.source is not RetrievalSource.RDB:
            raise RDBQueryCompilationError("canonical_v2 compiler requires an RDB step")
        if step.operation is not QueryOperation.SEARCH_PRODUCTS:
            raise RDBQueryCompilationError(
                f"unsupported canonical_v2 operation: {step.operation.value}"
            )
        try:
            grain = V2ResultGrain(
                step.inputs.get("result_grain", V2ResultGrain.FINANCIAL_PRODUCT.value)
            )
        except ValueError as exc:
            raise RDBQueryCompilationError("unsupported canonical_v2 result grain") from exc

        entity_id, base = self._base(grain, snapshot)
        conditions = [base.c.query_eligible.is_(True)]
        product_types = step.inputs.get("product_types", [])
        if not isinstance(product_types, list):
            raise RDBQueryCompilationError("product_types must be a list")
        public_fund = _PUBLIC_FUND in product_types
        unknown = set(product_types) - set(_PRODUCT_TYPES) - {_PUBLIC_FUND}
        if unknown:
            raise RDBQueryCompilationError("unsupported canonical_v2 product type mapping")
        codes = {_PRODUCT_TYPES[value] for value in product_types if value in _PRODUCT_TYPES}
        if public_fund:
            codes.add("FUND")
        if codes:
            conditions.append(base.c.product_type.in_(sorted(codes)))
        if public_fund:
            if grain is not V2ResultGrain.FINANCIAL_PRODUCT:
                raise RDBQueryCompilationError("public-fund search returns Fund grain")
            conditions.append(self._public_fund_exists(entity_id, snapshot))

        ids = step.inputs.get("entity_ids", [])
        if ids:
            if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids):
                raise RDBQueryCompilationError("entity_ids must be canonical string IDs")
            conditions.append(entity_id.in_(ids))

        for item in step.inputs.get("filters", []):
            conditions.append(self._compile_filter(item, entity_id, base, snapshot))
        for item in step.inputs.get("relations", []):
            conditions.append(self._compile_relation(item, entity_id, snapshot))

        requested = step.inputs.get("requested_fields", [])
        if not isinstance(requested, list):
            raise RDBQueryCompilationError("requested_fields must be a list")
        projected = list(dict.fromkeys(requested or ["product.name"]))
        for field in projected:
            mapping = self._fields.field(field)
            if not mapping.project_enabled:
                raise RDBQueryCompilationError(f"projection disabled for {field}")

        sort_items = step.inputs.get("sort", [])
        if not isinstance(sort_items, list):
            raise RDBQueryCompilationError("sort must be a list")
        if sort_items:
            # Current v2 metrics are NOT_COMPARABLE and no approved rank exists.
            for item in sort_items:
                if not isinstance(item, dict):
                    raise RDBQueryCompilationError("sort item must be structured")
                mapping = self._fields.field(item.get("canonical_field"))
                if not mapping.sort_enabled:
                    raise RDBQueryCompilationError(
                        f"canonical_v2 sorting is semantically disabled for {mapping.canonical_field}"
                    )

        limit = step.inputs.get("limit", self._default_limit)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise RDBQueryCompilationError("canonical_v2 limit must be positive")
        limit = min(limit, self._max_limit)
        statement = (
            select(base)
            .where(*conditions)
            # Canonical entity IDs are an opaque, ASCII identifier namespace.
            # Pin collation so result-window selection is stable across
            # PostgreSQL deployments rather than depending on server locale.
            .order_by(asc(entity_id.collate("C")))
            .limit(limit)
        )
        return CompiledV2RDBQuery(
            statement=statement,
            count_statement=(
                select(func.count(distinct(entity_id)))
                .select_from(base)
                .where(*conditions)
            ),
            projected_fields=tuple(projected),
            result_grain=grain,
            ranking_applied=False,
        )

    @staticmethod
    def _base(grain: V2ResultGrain, snapshot: V2SnapshotSelection):
        if grain is V2ResultGrain.FINANCIAL_PRODUCT:
            base = (
                select(
                    canonical_entities.c.entity_id,
                    canonical_entities.c.entity_kind,
                    canonical_entities.c.preferred_name,
                    canonical_entities.c.normalized_preferred_name,
                    canonical_entities.c.name_status,
                    canonical_entities.c.query_eligible,
                    financial_products.c.product_type_code.label("product_type"),
                )
                .join(
                    financial_products,
                    financial_products.c.product_id == canonical_entities.c.entity_id,
                )
                .where(
                    exists(
                        select(1).where(
                            canonical_facts.c.subject_entity_id == canonical_entities.c.entity_id,
                            canonical_facts.c.snapshot_id.in_(snapshot.snapshot_ids),
                            canonical_facts.c.resolution_status == "RESOLVED",
                        )
                    )
                )
                .subquery("v2_product_base")
            )
        elif grain is V2ResultGrain.FUND_SHARE_CLASS:
            base = (
                select(
                    canonical_entities.c.entity_id,
                    canonical_entities.c.entity_kind,
                    canonical_entities.c.preferred_name,
                    canonical_entities.c.normalized_preferred_name,
                    canonical_entities.c.name_status,
                    canonical_entities.c.query_eligible,
                    fund_share_classes.c.parent_fund_id,
                    func.cast("FUND", canonical_entities.c.entity_kind.type).label("product_type"),
                )
                .join(
                    fund_share_classes,
                    fund_share_classes.c.fund_share_class_id
                    == canonical_entities.c.entity_id,
                )
                .where(
                    exists(
                        select(1).where(
                            canonical_facts.c.subject_entity_id == canonical_entities.c.entity_id,
                            canonical_facts.c.snapshot_id.in_(snapshot.snapshot_ids),
                        )
                    )
                )
                .subquery("v2_share_class_base")
            )
        else:
            base = (
                select(
                    canonical_entities.c.entity_id,
                    canonical_entities.c.entity_kind,
                    canonical_entities.c.preferred_name,
                    canonical_entities.c.normalized_preferred_name,
                    canonical_entities.c.name_status,
                    canonical_entities.c.query_eligible,
                    sale_lots.c.bond_id,
                    func.cast("BOND", canonical_entities.c.entity_kind.type).label("product_type"),
                )
                .join(sale_lots, sale_lots.c.sale_lot_id == canonical_entities.c.entity_id)
                .where(
                    exists(
                        select(1).where(
                            canonical_facts.c.subject_entity_id == canonical_entities.c.entity_id,
                            canonical_facts.c.snapshot_id.in_(snapshot.snapshot_ids),
                        )
                    )
                )
                .subquery("v2_sale_lot_base")
            )
        return base.c.entity_id, base

    def _compile_filter(self, item, entity_id, base, snapshot):
        if not isinstance(item, dict):
            raise RDBQueryCompilationError("filter item must be structured")
        mapping = self._fields.field(item.get("canonical_field"))
        if not mapping.filter_enabled:
            raise RDBQueryCompilationError(
                f"canonical_v2 filtering is semantically disabled for {mapping.canonical_field}"
            )
        raw = item.get("raw")
        if not isinstance(raw, dict):
            raise RDBQueryCompilationError("filter raw representation is missing")
        try:
            operator = FilterOperator(raw.get("operator"))
        except (TypeError, ValueError) as exc:
            raise RDBQueryCompilationError("unsupported canonical_v2 filter operator") from exc
        value = item.get("canonical_value")
        if value is None:
            value = raw.get("value")
        values = value if operator is FilterOperator.IN else [value]
        if operator is FilterOperator.IN and not isinstance(value, list):
            raise RDBQueryCompilationError("IN filter requires canonical values")
        if operator not in {FilterOperator.EQ, FilterOperator.NE, FilterOperator.IN}:
            raise RDBQueryCompilationError(
                f"operator {operator.value} is unsupported for canonical field {mapping.canonical_field}"
            )

        if mapping.kind == "product_type":
            try:
                codes = [_PRODUCT_TYPES[item] for item in values]
            except KeyError as exc:
                raise RDBQueryCompilationError("unsupported product type filter") from exc
            predicate = base.c.product_type.in_(codes)
        elif mapping.kind == "entity":
            normalized = [normalize_lookup_value(str(item)) for item in values]
            predicate = base.c.normalized_preferred_name.in_(normalized)
        elif mapping.kind == "alias":
            predicate = self._alias_exists(entity_id, values)
        elif mapping.kind == "identifier":
            predicate = self._identifier_exists(entity_id, mapping.semantic_key, values)
        elif mapping.kind == "classification":
            iris = [self._fields.concept_iri(mapping.semantic_key, item) for item in values]
            predicate = self._classification_exists(entity_id, mapping.semantic_key, iris, snapshot)
        elif mapping.kind.endswith("relation"):
            values = [
                _canonical_relation_target(mapping.semantic_key, item)
                for item in values
            ]
            predicate = self._relation_exists(
                entity_id, mapping.kind, mapping.semantic_key, values, snapshot
            )
        else:
            raise RDBQueryCompilationError(
                f"canonical_v2 filter is unsupported for {mapping.canonical_field}"
            )
        if operator is FilterOperator.NE:
            if mapping.kind == "classification":
                return and_(
                    self._classification_known(
                        entity_id, mapping.semantic_key, snapshot
                    ),
                    not_(predicate),
                )
            if mapping.kind.endswith("relation"):
                return and_(
                    self._relation_exists(
                        entity_id,
                        mapping.kind,
                        mapping.semantic_key,
                        None,
                        snapshot,
                    ),
                    not_(predicate),
                )
            return not_(predicate)
        return predicate

    def _compile_relation(self, item, entity_id, snapshot):
        if not isinstance(item, dict):
            raise RDBQueryCompilationError("relation constraint must be structured")
        relation = item.get("canonical_relation")
        target = item.get("target_entity_id") or item.get("target_value")
        relation_map = {
            "managedBy": ("organization_relation", "MANAGED_BY"),
            "issuedBy": ("organization_relation", "ISSUED_BY"),
            "hasTrustee": ("organization_relation", "HAS_TRUSTEE"),
            "hasUnderlyingIndex": ("index_relation", "HAS_UNDERLYING_INDEX"),
            "tracksIndex": ("index_relation", "TRACKS_INDEX"),
            "hasBenchmark": ("index_relation", "HAS_BENCHMARK"),
            "denominatedIn": ("entity_relation", "DENOMINATED_IN"),
            "tradedInCurrency": ("entity_relation", "TRADED_IN_CURRENCY"),
            "listedInCountry": ("entity_relation", "LISTED_IN_COUNTRY"),
            "hasInstrumentCountry": ("entity_relation", "HAS_INSTRUMENT_COUNTRY"),
            "hasShareClass": ("entity_relation", "HAS_SHARE_CLASS"),
            "hasSaleLot": ("entity_relation", "HAS_SALE_LOT"),
        }
        try:
            kind, edge = relation_map[relation]
        except KeyError as exc:
            raise RDBQueryCompilationError(f"unsupported canonical_v2 relation: {relation}") from exc
        if not isinstance(target, str) or not target:
            raise RDBQueryCompilationError("relation target must be a resolved canonical entity ID")
        target = _canonical_relation_target(edge, target)
        predicate = self._relation_exists(entity_id, kind, edge, [target], snapshot)
        if bool(item.get("negated")):
            return and_(
                self._relation_exists(entity_id, kind, edge, None, snapshot),
                not_(predicate),
            )
        return predicate

    @staticmethod
    def _classification_exists(entity_id, kind, iris, snapshot):
        direct = exists(
            select(1)
            .select_from(
                entity_classifications.join(
                    canonical_facts,
                    canonical_facts.c.fact_id == entity_classifications.c.fact_id,
                )
            )
            .where(
                entity_classifications.c.entity_id == entity_id,
                entity_classifications.c.classification_type == kind,
                entity_classifications.c.concept_iri.in_(iris),
                canonical_facts.c.snapshot_id.in_(snapshot.snapshot_ids),
                canonical_facts.c.resolution_status == "RESOLVED",
            )
        )
        child_from = (
            fund_share_classes.join(
                entity_classifications,
                entity_classifications.c.entity_id
                == fund_share_classes.c.fund_share_class_id,
            ).join(
                canonical_facts,
                canonical_facts.c.fact_id == entity_classifications.c.fact_id,
            )
        )
        matching_child = exists(
            select(1).select_from(child_from).where(
                fund_share_classes.c.parent_fund_id == entity_id,
                entity_classifications.c.classification_type == kind,
                entity_classifications.c.concept_iri.in_(iris),
                canonical_facts.c.snapshot_id.in_(snapshot.snapshot_ids),
                canonical_facts.c.resolution_status == "RESOLVED",
            )
        )
        if kind == "OFFERING_TYPE":
            # Public/private Fund is explicitly existential over share classes.
            return or_(direct, matching_child)
        conflicting_child = exists(
            select(1).select_from(child_from).where(
                fund_share_classes.c.parent_fund_id == entity_id,
                entity_classifications.c.classification_type == kind,
                entity_classifications.c.concept_iri.not_in(iris),
                canonical_facts.c.snapshot_id.in_(snapshot.snapshot_ids),
                canonical_facts.c.resolution_status == "RESOLVED",
            )
        )
        # Family classification is safe only when all observed class values
        # agree.  Conflicting families are deliberately excluded.
        return or_(direct, and_(matching_child, not_(conflicting_child)))

    @staticmethod
    def _classification_known(entity_id, kind, snapshot):
        direct_count = (
            select(func.count(func.distinct(entity_classifications.c.concept_iri)))
            .select_from(
                entity_classifications.join(
                    canonical_facts,
                    canonical_facts.c.fact_id == entity_classifications.c.fact_id,
                )
            )
            .where(
                entity_classifications.c.entity_id == entity_id,
                entity_classifications.c.classification_type == kind,
                canonical_facts.c.snapshot_id.in_(snapshot.snapshot_ids),
                canonical_facts.c.resolution_status == "RESOLVED",
            )
            .scalar_subquery()
        )
        child_count = (
            select(func.count(func.distinct(entity_classifications.c.concept_iri)))
            .select_from(
                fund_share_classes.join(
                    entity_classifications,
                    entity_classifications.c.entity_id
                    == fund_share_classes.c.fund_share_class_id,
                ).join(
                    canonical_facts,
                    canonical_facts.c.fact_id == entity_classifications.c.fact_id,
                )
            )
            .where(
                fund_share_classes.c.parent_fund_id == entity_id,
                entity_classifications.c.classification_type == kind,
                canonical_facts.c.snapshot_id.in_(snapshot.snapshot_ids),
                canonical_facts.c.resolution_status == "RESOLVED",
            )
            .scalar_subquery()
        )
        return or_(direct_count == 1, child_count == 1)

    @staticmethod
    def _alias_exists(entity_id, values):
        normalized = [normalize_lookup_value(str(item)) for item in values]
        return exists(
            select(1).where(
                entity_aliases.c.entity_id == entity_id,
                entity_aliases.c.normalized_alias.in_(normalized),
            )
        )

    @staticmethod
    def _identifier_exists(entity_id, scheme, values):
        normalized = {
            candidate
            for item in values
            for candidate in (
                normalize_lookup_value(str(item)),
                str(item).strip().upper(),
            )
        }
        return exists(
            select(1).where(
                entity_identifiers.c.entity_id == entity_id,
                entity_identifiers.c.scheme_code == scheme,
                entity_identifiers.c.normalized_value.in_(sorted(normalized)),
                entity_identifiers.c.validation_status == "VALIDATED",
                entity_identifiers.c.resolution_status == "RESOLVED",
                entity_identifiers.c.conflict_status == "NONE",
            )
        )

    @staticmethod
    def _relation_exists(entity_id, kind, edge, values, snapshot):
        if kind == "organization_relation":
            table, subject, target = (
                organization_relations,
                organization_relations.c.subject_product_id,
                organization_relations.c.organization_id,
            )
        elif kind == "index_relation":
            table, subject, target = (
                index_relations,
                index_relations.c.subject_product_id,
                index_relations.c.index_id,
            )
        else:
            table, subject, target = (
                entity_relations,
                entity_relations.c.subject_entity_id,
                entity_relations.c.object_entity_id,
            )
        conditions = [
            subject == entity_id,
            table.c.relation_type == edge,
            canonical_facts.c.snapshot_id.in_(snapshot.snapshot_ids),
            canonical_facts.c.resolution_status == "RESOLVED",
        ]
        if values is not None:
            conditions.append(target.in_(values))
        return exists(
            select(1)
            .select_from(
                table.join(
                    canonical_facts,
                    canonical_facts.c.fact_id == table.c.fact_id,
                )
            )
            .where(*conditions)
        )

    def _public_fund_exists(self, fund_id, snapshot):
        public_iri = self._fields.concept_iri(
            "OFFERING_TYPE", "OfferingType.PUBLIC"
        )
        return exists(
            select(1)
            .select_from(
                fund_share_classes.join(
                    entity_classifications,
                    entity_classifications.c.entity_id
                    == fund_share_classes.c.fund_share_class_id,
                ).join(
                    canonical_facts,
                    canonical_facts.c.fact_id == entity_classifications.c.fact_id,
                )
            )
            .where(
                fund_share_classes.c.parent_fund_id == fund_id,
                entity_classifications.c.classification_type == "OFFERING_TYPE",
                entity_classifications.c.concept_iri == public_iri,
                canonical_facts.c.snapshot_id.in_(snapshot.snapshot_ids),
                canonical_facts.c.resolution_status == "RESOLVED",
            )
        )


class CanonicalV2RDBRetriever:
    def __init__(
        self,
        engine: Engine,
        compiler: CanonicalV2QueryCompiler,
        snapshot_selector: CanonicalV2SnapshotSelector,
    ) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("canonical_v2 repository requires PostgreSQL")
        self._engine = engine
        self._compiler = compiler
        self._selector = snapshot_selector

    async def retrieve(
        self, step: QueryStep, context: ExecutionContext
    ) -> list[RetrievalRecord]:
        try:
            return (await self.retrieve_with_result(step, context)).records
        except (RDBQueryCompilationError, V2SnapshotUnavailableError):
            raise
        except SQLAlchemyError as exc:
            raise RetrieverUnavailableError("canonical_v2 RDB retrieval failed") from exc

    async def retrieve_with_result(
        self, step: QueryStep, context: ExecutionContext
    ) -> RetrievalResult:
        del context
        try:
            return await asyncio.to_thread(self._retrieve_sync, step)
        except (RDBQueryCompilationError, V2SnapshotUnavailableError):
            raise
        except SQLAlchemyError as exc:
            raise RetrieverUnavailableError("canonical_v2 RDB retrieval failed") from exc

    def _retrieve_sync(self, step: QueryStep) -> RetrievalResult:
        with self._engine.connect() as connection:
            snapshot = self._selector.select(connection)
            compiled = self._compiler.compile(step, snapshot)
            total_matches = int(connection.scalar(compiled.count_statement) or 0)
            rows = connection.execute(compiled.statement).mappings().all()
            entity_ids = [str(row["entity_id"]) for row in rows]
            projected = self._project(connection, entity_ids, compiled.projected_fields, snapshot)
            child_types = {
                mapping.semantic_key
                for item in step.inputs.get("filters", [])
                if isinstance(item, dict)
                and (mapping := self._compiler._fields.field(item.get("canonical_field"))).kind
                == "classification"
            }
            if _PUBLIC_FUND in step.inputs.get("product_types", []):
                child_types.add("OFFERING_TYPE")
            provenance = self._provenance(
                connection, entity_ids, snapshot, child_types
            )

        matched = tuple(
            str(item.get("canonical_field"))
            for item in step.inputs.get("filters", [])
            if isinstance(item, dict) and item.get("canonical_field")
        )
        records: list[RetrievalRecord] = []
        for row in rows:
            entity_id = str(row["entity_id"])
            facts = provenance.get(entity_id, {})
            typed = CanonicalV2RetrievalRecord(
                entity_id=entity_id,
                entity_kind=str(row["entity_kind"]),
                product_type=str(row["product_type"]) if row["product_type"] else None,
                preferred_name=row["preferred_name"],
                name_status=str(row["name_status"]),
                matched_constraints=matched,
                canonical_fact_ids=tuple(facts.get("fact_ids", ())),
                evidence_assertion_ids=tuple(facts.get("assertion_ids", ())),
                source_record_ids=tuple(facts.get("source_record_ids", ())),
                snapshot_identity=snapshot.identity,
            )
            for field in compiled.projected_fields:
                value = projected.get((entity_id, field))
                records.append(
                    RetrievalRecord(
                        step_id=step.step_id,
                        source=RetrievalSource.RDB.value,
                        source_id=f"canonical_v2:{snapshot.identity}:{entity_id}:{field}",
                        entity_id=entity_id,
                        payload={"field": field, "value": value, "text": typed.preferred_name},
                        metadata={
                            "repository_version": "v2",
                            "real_rdb": True,
                            "entity_kind": typed.entity_kind,
                            "product_type": typed.product_type,
                            "preferred_name": typed.preferred_name,
                            "name_status": typed.name_status,
                            "matched_constraints": list(typed.matched_constraints),
                            "canonical_fact_ids": list(typed.canonical_fact_ids),
                            "evidence_assertion_ids": list(typed.evidence_assertion_ids),
                            "source_record_ids": list(typed.source_record_ids),
                            "source_datasets": list(facts.get("dataset_codes", ())),
                            "snapshot_identity": typed.snapshot_identity,
                            "snapshot_ids": list(snapshot.snapshot_ids),
                            "dataset_snapshot": snapshot.snapshot_date.isoformat(),
                            "ranking_applied": compiled.ranking_applied,
                            **(
                                {"parent_fund_id": row["parent_fund_id"]}
                                if "parent_fund_id" in row else {}
                            ),
                            **({"bond_id": row["bond_id"]} if "bond_id" in row else {}),
                        },
                    )
                )
        return RetrievalResult(
            records=records,
            total_matches=total_matches,
            returned_count=len({record.entity_id for record in records if record.entity_id}),
            window_limit=compiled.statement._limit_clause.value if compiled.statement._limit_clause is not None else None,
            counts={"structured_total_matches": total_matches},
        )

    @staticmethod
    def _project(connection, entity_ids, fields, snapshot):
        result: dict[tuple[str, str], Any] = {}
        if not entity_ids:
            return result
        entity_rows = connection.execute(
            select(canonical_entities).where(canonical_entities.c.entity_id.in_(entity_ids))
        ).mappings()
        for row in entity_rows:
            result[(str(row["entity_id"]), "product.name")] = row["preferred_name"]
        product_rows = connection.execute(
            select(financial_products).where(financial_products.c.product_id.in_(entity_ids))
        ).mappings()
        for row in product_rows:
            result[(str(row["product_id"]), "product.product_type")] = row["product_type_code"]

        registry = CanonicalV2FieldRegistry()
        for field in fields:
            mapping = registry.field(field)
            if mapping.kind == "classification":
                rows = connection.execute(
                    select(
                        entity_classifications.c.entity_id,
                        ontology_concepts.c.canonical_name,
                    )
                    .join(ontology_concepts, ontology_concepts.c.concept_iri == entity_classifications.c.concept_iri)
                    .join(canonical_facts, canonical_facts.c.fact_id == entity_classifications.c.fact_id)
                    .where(
                        entity_classifications.c.entity_id.in_(entity_ids),
                        entity_classifications.c.classification_type == mapping.semantic_key,
                        canonical_facts.c.snapshot_id.in_(snapshot.snapshot_ids),
                        canonical_facts.c.resolution_status == "RESOLVED",
                    )
                    .order_by(entity_classifications.c.entity_id, ontology_concepts.c.canonical_name)
                ).all()
                CanonicalV2RDBRetriever._collect_projection(result, field, rows)
                child_rows = connection.execute(
                    select(
                        fund_share_classes.c.parent_fund_id,
                        ontology_concepts.c.canonical_name,
                    )
                    .join(
                        entity_classifications,
                        entity_classifications.c.entity_id
                        == fund_share_classes.c.fund_share_class_id,
                    )
                    .join(
                        ontology_concepts,
                        ontology_concepts.c.concept_iri
                        == entity_classifications.c.concept_iri,
                    )
                    .join(
                        canonical_facts,
                        canonical_facts.c.fact_id
                        == entity_classifications.c.fact_id,
                    )
                    .where(
                        fund_share_classes.c.parent_fund_id.in_(entity_ids),
                        entity_classifications.c.classification_type
                        == mapping.semantic_key,
                        canonical_facts.c.snapshot_id.in_(snapshot.snapshot_ids),
                        canonical_facts.c.resolution_status == "RESOLVED",
                    )
                    .order_by(
                        fund_share_classes.c.parent_fund_id,
                        ontology_concepts.c.canonical_name,
                    )
                ).all()
                child_values: dict[str, list[str]] = {}
                for parent_id, value in child_rows:
                    values = child_values.setdefault(str(parent_id), [])
                    if value not in values:
                        values.append(str(value))
                for parent_id, values in child_values.items():
                    if (parent_id, field) in result:
                        continue
                    if mapping.semantic_key == "OFFERING_TYPE":
                        result[(parent_id, field)] = (
                            values[0] if len(values) == 1 else values
                        )
                    elif len(values) == 1:
                        result[(parent_id, field)] = values[0]
            elif mapping.kind in {"organization_relation", "index_relation", "entity_relation"}:
                table, subject, target = CanonicalV2RDBRetriever._relation_columns(mapping.kind)
                rows = connection.execute(
                    select(subject, canonical_entities.c.preferred_name, target)
                    .select_from(table.join(canonical_entities, canonical_entities.c.entity_id == target))
                    .join(canonical_facts, canonical_facts.c.fact_id == table.c.fact_id)
                    .where(
                        subject.in_(entity_ids),
                        table.c.relation_type == mapping.semantic_key,
                        canonical_facts.c.snapshot_id.in_(snapshot.snapshot_ids),
                    )
                    .order_by(subject, target)
                ).all()
                normalized = [(row[0], row[1] if row[1] is not None else row[2]) for row in rows]
                CanonicalV2RDBRetriever._collect_projection(result, field, normalized)
            elif mapping.kind == "alias":
                rows = connection.execute(
                    select(entity_aliases.c.entity_id, entity_aliases.c.alias)
                    .where(
                        entity_aliases.c.entity_id.in_(entity_ids),
                        entity_aliases.c.alias_type == mapping.semantic_key,
                    ).order_by(entity_aliases.c.entity_id, entity_aliases.c.alias)
                ).all()
                CanonicalV2RDBRetriever._collect_projection(result, field, rows)
            elif mapping.kind == "identifier":
                rows = connection.execute(
                    select(entity_identifiers.c.entity_id, entity_identifiers.c.raw_value)
                    .where(
                        entity_identifiers.c.entity_id.in_(entity_ids),
                        entity_identifiers.c.scheme_code == mapping.semantic_key,
                        entity_identifiers.c.validation_status == "VALIDATED",
                        entity_identifiers.c.conflict_status == "NONE",
                    ).order_by(entity_identifiers.c.entity_id, entity_identifiers.c.raw_value)
                ).all()
                CanonicalV2RDBRetriever._collect_projection(result, field, rows)
            elif mapping.kind == "scalar":
                rows = connection.execute(
                    select(canonical_facts.c.subject_entity_id, canonical_scalar_facts.c.date_value)
                    .join(canonical_scalar_facts, canonical_scalar_facts.c.fact_id == canonical_facts.c.fact_id)
                    .where(
                        canonical_facts.c.subject_entity_id.in_(entity_ids),
                        canonical_facts.c.semantic_key == mapping.semantic_key,
                        canonical_facts.c.snapshot_id.in_(snapshot.snapshot_ids),
                        canonical_facts.c.resolution_status == "RESOLVED",
                    ).order_by(canonical_facts.c.subject_entity_id)
                ).all()
                CanonicalV2RDBRetriever._collect_projection(result, field, rows)
            elif mapping.kind == "metric":
                rows = connection.execute(
                    select(metric_observations.c.subject_entity_id, metric_observations.c.numeric_value)
                    .join(canonical_facts, canonical_facts.c.fact_id == metric_observations.c.fact_id)
                    .join(metric_definitions, metric_definitions.c.metric_code == metric_observations.c.metric_code)
                    .where(
                        metric_observations.c.subject_entity_id.in_(entity_ids),
                        metric_observations.c.metric_code == mapping.semantic_key,
                        canonical_facts.c.snapshot_id.in_(snapshot.snapshot_ids),
                        metric_observations.c.quality_status.in_(("VALID", "SOURCE_ZERO")),
                    ).order_by(metric_observations.c.subject_entity_id, metric_observations.c.observed_on.desc())
                ).all()
                CanonicalV2RDBRetriever._collect_projection(result, field, rows)
        return result

    @staticmethod
    def _collect_projection(result, field, rows):
        collected: dict[str, list[Any]] = {}
        for entity_id, value, *_ in rows:
            values = collected.setdefault(str(entity_id), [])
            if value not in values:
                values.append(value)
        for entity_id, values in collected.items():
            result[(entity_id, field)] = values[0] if len(values) == 1 else values

    @staticmethod
    def _relation_columns(kind):
        if kind == "organization_relation":
            return organization_relations, organization_relations.c.subject_product_id, organization_relations.c.organization_id
        if kind == "index_relation":
            return index_relations, index_relations.c.subject_product_id, index_relations.c.index_id
        return entity_relations, entity_relations.c.subject_entity_id, entity_relations.c.object_entity_id

    @staticmethod
    def _provenance(connection, entity_ids, snapshot, child_types=frozenset()):
        result: dict[str, dict[str, set[str]]] = {}
        if not entity_ids:
            return result
        rows = connection.execute(
            select(
                canonical_facts.c.subject_entity_id,
                canonical_facts.c.fact_id,
                fact_evidence_links.c.assertion_id,
                source_field_assertions.c.source_record_id,
                source_datasets.c.dataset_code,
            )
            .join(fact_evidence_links, fact_evidence_links.c.fact_id == canonical_facts.c.fact_id)
            .join(source_field_assertions, source_field_assertions.c.assertion_id == fact_evidence_links.c.assertion_id)
            .join(source_records, source_records.c.source_record_id == source_field_assertions.c.source_record_id)
            .join(dataset_snapshots, dataset_snapshots.c.snapshot_id == source_records.c.snapshot_id)
            .join(source_datasets, source_datasets.c.dataset_id == dataset_snapshots.c.dataset_id)
            .where(
                canonical_facts.c.subject_entity_id.in_(entity_ids),
                canonical_facts.c.snapshot_id.in_(snapshot.snapshot_ids),
                canonical_facts.c.resolution_status == "RESOLVED",
            )
            .order_by(canonical_facts.c.subject_entity_id, canonical_facts.c.fact_id, fact_evidence_links.c.assertion_id)
        ).all()
        for entity_id, fact_id, assertion_id, source_record_id, dataset_code in rows:
            item = result.setdefault(
                str(entity_id),
                {"fact_ids": set(), "assertion_ids": set(), "source_record_ids": set(), "dataset_codes": set()},
            )
            item["fact_ids"].add(str(fact_id))
            item["assertion_ids"].add(str(assertion_id))
            item["source_record_ids"].add(str(source_record_id))
            item["dataset_codes"].add(str(dataset_code))
        if child_types:
            child_rows = connection.execute(
                select(
                    fund_share_classes.c.parent_fund_id,
                    canonical_facts.c.fact_id,
                    fact_evidence_links.c.assertion_id,
                    source_field_assertions.c.source_record_id,
                    source_datasets.c.dataset_code,
                )
                .join(
                    entity_classifications,
                    entity_classifications.c.entity_id
                    == fund_share_classes.c.fund_share_class_id,
                )
                .join(
                    canonical_facts,
                    canonical_facts.c.fact_id == entity_classifications.c.fact_id,
                )
                .join(
                    fact_evidence_links,
                    fact_evidence_links.c.fact_id == canonical_facts.c.fact_id,
                )
                .join(
                    source_field_assertions,
                    source_field_assertions.c.assertion_id
                    == fact_evidence_links.c.assertion_id,
                )
                .join(
                    source_records,
                    source_records.c.source_record_id
                    == source_field_assertions.c.source_record_id,
                )
                .join(
                    dataset_snapshots,
                    dataset_snapshots.c.snapshot_id == source_records.c.snapshot_id,
                )
                .join(
                    source_datasets,
                    source_datasets.c.dataset_id == dataset_snapshots.c.dataset_id,
                )
                .where(
                    fund_share_classes.c.parent_fund_id.in_(entity_ids),
                    entity_classifications.c.classification_type.in_(child_types),
                    canonical_facts.c.snapshot_id.in_(snapshot.snapshot_ids),
                    canonical_facts.c.resolution_status == "RESOLVED",
                )
                .order_by(
                    fund_share_classes.c.parent_fund_id,
                    canonical_facts.c.fact_id,
                    fact_evidence_links.c.assertion_id,
                )
            ).all()
            for parent_id, fact_id, assertion_id, source_record_id, dataset_code in child_rows:
                item = result.setdefault(
                    str(parent_id),
                    {"fact_ids": set(), "assertion_ids": set(), "source_record_ids": set(), "dataset_codes": set()},
                )
                item["fact_ids"].add(str(fact_id))
                item["assertion_ids"].add(str(assertion_id))
                item["source_record_ids"].add(str(source_record_id))
                item["dataset_codes"].add(str(dataset_code))
        return {
            key: {name: tuple(sorted(values)) for name, values in item.items()}
            for key, item in result.items()
        }


class RDBShadowDifference(str, Enum):
    EXPECTED_SEMANTIC_CHANGE = "EXPECTED_SEMANTIC_CHANGE"
    REGRESSION = "REGRESSION"
    V1_LEGACY_ARTIFACT = "V1_LEGACY_ARTIFACT"
    V2_UNSUPPORTED = "V2_UNSUPPORTED"
    ZERO_MATCH = "ZERO_MATCH"


@dataclass(frozen=True, slots=True)
class RDBShadowComparison:
    v1_entity_ids: tuple[str, ...]
    v2_entity_ids: tuple[str, ...]
    classification: RDBShadowDifference | None


class ReadOnlyRDBShadowComparator:
    """Run the same immutable QueryStep against injected v1/v2 retrievers."""

    def __init__(self, v1, v2) -> None:
        self._v1 = v1
        self._v2 = v2

    async def compare(
        self,
        step: QueryStep,
        context: ExecutionContext,
        *,
        expected_difference: RDBShadowDifference | None = None,
    ) -> RDBShadowComparison:
        v1_result, v2_result = await asyncio.gather(
            self._v1.retrieve(step, context),
            self._v2.retrieve(step, context),
            return_exceptions=True,
        )
        if isinstance(v2_result, RDBQueryCompilationError):
            return RDBShadowComparison((), (), RDBShadowDifference.V2_UNSUPPORTED)
        if isinstance(v1_result, RDBQueryCompilationError):
            v1_records = []
            classification = RDBShadowDifference.V1_LEGACY_ARTIFACT
        elif isinstance(v1_result, BaseException):
            raise v1_result
        else:
            v1_records = v1_result
            classification = None
        if isinstance(v2_result, BaseException):
            raise v2_result
        v2_records = v2_result
        v1 = tuple(sorted({item.entity_id for item in v1_records if item.entity_id}))
        v2 = tuple(sorted({item.entity_id for item in v2_records if item.entity_id}))
        if v1 == v2:
            classification = None
        elif expected_difference is not None:
            classification = expected_difference
        elif not v2:
            classification = RDBShadowDifference.ZERO_MATCH
        elif classification is None:
            classification = RDBShadowDifference.REGRESSION
        return RDBShadowComparison(v1, v2, classification)


def _canonical_relation_target(relation: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RDBQueryCompilationError("relation target must be canonical")
    controlled = _CONTROLLED_RELATION_TARGETS.get(relation)
    if controlled is None:
        return value
    raw = value.strip()
    prefix = "currency:" if "CURRENCY" in relation or relation == "DENOMINATED_IN" else "country:"
    if raw.startswith(prefix):
        code = raw.removeprefix(prefix).upper()
    elif "." in raw:
        code = raw.rsplit(".", 1)[-1].upper()
    else:
        code = raw.upper()
    try:
        return controlled[code]
    except KeyError as exc:
        raise RDBQueryCompilationError(
            f"unsupported canonical_v2 relation target: {relation}:{value}"
        ) from exc

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ScalarValue = str | int | float | bool


class QueryIntent(str, Enum):
    SEARCH_PRODUCT = "search_product"
    COMPARE_PRODUCTS = "compare_products"
    LOOKUP_PRODUCT = "lookup_product"
    RECOMMEND_PRODUCT = "recommend_product"
    UNKNOWN = "unknown"


class FilterOperator(str, Enum):
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    IN = "in"
    BETWEEN = "between"


class ScalarUnit(str, Enum):
    NONE = "none"
    RATIO = "ratio"
    KRW = "krw"
    COUNT = "count"


class ConstraintStatus(str, Enum):
    PARSED = "parsed"
    GROUNDED = "grounded"
    PLANNED = "planned"
    UNSUPPORTED = "unsupported"
    AMBIGUOUS = "ambiguous"


class ConstraintSemanticType(str, Enum):
    PRODUCT_TYPE = "product_type"
    PRODUCT_UNIVERSE = "product_universe"
    ENTITY = "entity"
    FILTER = "filter"
    SORT = "sort"
    REQUESTED_FIELD = "requested_field"
    RELATION = "relation"
    SEMANTIC = "semantic"
    BOOLEAN = "boolean"
    LIMIT = "limit"
    AGGREGATION = "aggregation"
    TEMPORAL = "temporal"
    INTENT = "intent"
    COMPARISON = "comparison"
    SUBJECTIVE = "subjective"


class ParserSource(str, Enum):
    RULE = "rule"
    LLM_FALLBACK = "llm_fallback"


class SemanticCoverageStatus(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class BooleanNodeType(str, Enum):
    PREDICATE = "predicate"
    AND = "and"
    OR = "or"
    NOT = "not"


class AggregationOperator(str, Enum):
    COUNT = "count"


class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"


class GroundingStatus(str, Enum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"


class RelationDirection(str, Enum):
    OUTGOING = "outgoing"
    INCOMING = "incoming"


class ConceptCategory(str, Enum):
    PRODUCT_TYPE = "product_type"
    REGION = "region"
    ASSET_TYPE = "asset_type"
    EXPOSURE_REGION = "exposure_region"
    ASSET_CLASS = "asset_class"
    OFFERING_TYPE = "offering_type"
    CLASSIFICATION = "classification"
    SEMANTIC_TERM = "semantic_term"


class CanonicalConcept(str, Enum):
    FINANCIAL_PRODUCT_ETF = "FinancialProduct.ETF"
    FINANCIAL_PRODUCT_ETN = "FinancialProduct.ETN"
    FINANCIAL_PRODUCT_BOND = "FinancialProduct.Bond"
    FINANCIAL_PRODUCT_FUND = "FinancialProduct.Fund"
    FINANCIAL_PRODUCT_PUBLIC_FUND = "FinancialProduct.PublicFund"
    REGION_KR = "Region.KR"
    REGION_US = "Region.US"
    REGION_JP = "Region.JP"
    REGION_CN = "Region.CN"
    REGION_IN = "Region.IN"
    REGION_GLOBAL = "Region.Global"
    REGION_ASIA = "Region.Asia"
    ASSET_TYPE_EQUITY = "AssetType.Equity"
    ASSET_TYPE_BOND = "AssetType.Bond"
    ASSET_TYPE_COMMODITY = "AssetType.Commodity"
    ASSET_TYPE_MIXED = "AssetType.Mixed"
    ASSET_TYPE_MONEY_MARKET = "AssetType.MoneyMarket"
    ASSET_TYPE_CURRENCY = "AssetType.Currency"
    ASSET_TYPE_REAL_ESTATE = "AssetType.RealEstate"
    ASSET_TYPE_ALTERNATIVE = "AssetType.Alternative"
    ASSET_TYPE_OTHER = "AssetType.Other"


class SemanticCapabilityState(str, Enum):
    ACTIVE = "active"
    PROSPECTIVE = "prospective"
    UNSUPPORTED_BY_CURRENT_SNAPSHOT = "unsupported_by_current_snapshot"


class CanonicalSemanticValue(BaseModel):
    """Ontology-owned identity with an optional storage compatibility key.

    ``runtime_key`` is deliberately an adapter detail.  The ontology URI and
    canonical name remain the semantic identity carried through grounding.
    """

    model_config = ConfigDict(frozen=True)

    ontology_uri: str
    canonical_name: str
    category: str
    runtime_key: str | None = None
    mapping_version: str
    capability: SemanticCapabilityState = SemanticCapabilityState.ACTIVE
    legacy_names: tuple[str, ...] = ()

    @property
    def value(self) -> str:
        """Compatibility value consumed by existing planner/compiler code."""

        return self.runtime_key or self.canonical_name


SemanticValue = CanonicalSemanticValue | CanonicalConcept


class PlannerType(str, Enum):
    RULE = "rule"
    SUPERVISOR = "supervisor"


class RetrievalSource(str, Enum):
    RDB = "rdb"
    GRAPH = "graph"
    VECTOR = "vector"
    BM25 = "bm25"
    INTERNAL = "internal"


class QueryOperation(str, Enum):
    SEARCH_PRODUCTS = "search_products"
    SEMANTIC_SEARCH = "semantic_search"
    RELATIONSHIP_SEARCH = "relationship_search"
    FILTER_CANDIDATES = "filter_candidates"
    RANK_CANDIDATES = "rank_candidates"


class RoutingReason(str, Enum):
    DETERMINISTIC_STRUCTURED_QUERY = "deterministic_structured_query"
    REQUIRES_SEMANTIC_SEARCH = "requires_semantic_search"
    MULTI_SOURCE_QUERY = "multi_source_query"
    RELATION_TRAVERSAL = "relation_traversal"
    UNRESOLVED_SEMANTIC_TERM = "unresolved_semantic_term"
    UNRESOLVED_ENTITY = "unresolved_entity"
    AMBIGUOUS_ENTITY = "ambiguous_entity"
    UNSUPPORTED_STRUCTURED_CONDITION = "unsupported_structured_condition"


class StepExecutionStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    SKIPPED = "skipped"


class ExecutionErrorCode(str, Enum):
    RETRIEVER_NOT_REGISTERED = "retriever_not_registered"
    RETRIEVAL_FAILED = "retrieval_failed"
    STEP_TIMEOUT = "step_timeout"
    DEPENDENCY_FAILED = "dependency_failed"


class CoverageStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class SnapshotPolicy(str, Enum):
    IGNORE = "ignore"
    WARN = "warn"
    REQUIRE_CONSISTENT_FOR_COMPARISON = (
        "require_consistent_for_comparison"
    )


class FindingSeverity(str, Enum):
    WARNING = "warning"
    BLOCKING = "blocking"


class AnswerabilityReasonCode(str, Enum):
    ANSWERABLE = "ANSWERABLE"
    NO_EVIDENCE = "NO_EVIDENCE"
    ZERO_MATCH = "ZERO_MATCH"
    ENTITY_NOT_FOUND = "ENTITY_NOT_FOUND"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    UNSUPPORTED_CONSTRAINT = "UNSUPPORTED_CONSTRAINT"
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    INVALID_SENTINEL = "INVALID_SENTINEL"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    ENTITY_MISMATCH = "ENTITY_MISMATCH"
    SNAPSHOT_MISMATCH = "SNAPSHOT_MISMATCH"
    OBSERVATION_TIME_MISMATCH = "OBSERVATION_TIME_MISMATCH"
    RETRIEVAL_FAILED = "RETRIEVAL_FAILED"
    RETRIEVAL_TIMED_OUT = "RETRIEVAL_TIMED_OUT"
    DEPENDENCY_INCOMPLETE = "DEPENDENCY_INCOMPLETE"
    INSUFFICIENT_COVERAGE = "INSUFFICIENT_COVERAGE"
    ENTITY_AMBIGUOUS = "ENTITY_AMBIGUOUS"
    # Source compatibility for older call sites; serialized contract is the
    # evaluator-facing ENTITY_AMBIGUOUS name.
    AMBIGUOUS_ENTITY = "ENTITY_AMBIGUOUS"
    UNSUPPORTED_QUERY_SEMANTICS = "UNSUPPORTED_QUERY_SEMANTICS"
    RANKING_NOT_APPLIED = "RANKING_NOT_APPLIED"


class EntityMention(BaseModel):
    raw_text: str
    entity_type: str
    canonical_id: str | None = None
    resolution_status: ResolutionStatus = ResolutionStatus.UNRESOLVED
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    matched_alias: str | None = None
    identifier_type: str | None = None
    candidate_ids: list[str] = Field(default_factory=list)
    constraint_id: str | None = None


class SourceSpan(BaseModel):
    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_order(self) -> "SourceSpan":
        if self.end <= self.start:
            raise ValueError("source span end must be greater than start")
        return self


class TypedScalarValue(BaseModel):
    raw: str
    normalized: int | float
    unit: ScalarUnit = ScalarUnit.NONE
    currency: str | None = None


class SemanticConstraint(BaseModel):
    constraint_id: str
    source_span: SourceSpan
    raw_text: str
    semantic_type: ConstraintSemanticType
    status: ConstraintStatus = ConstraintStatus.PARSED
    required: bool = True
    payload: dict[str, Any] = Field(default_factory=dict)
    unsupported_reason: str | None = None


class UnparsedMaterialSpan(BaseModel):
    source_span: SourceSpan
    raw_text: str


class ParseProvenance(BaseModel):
    parser_source: ParserSource = ParserSource.RULE
    semantic_schema_version: str = "m10.6-semantic-v1"
    prompt_version: str | None = None
    model: str | None = None
    rule_latency_ms: float = Field(default=0.0, ge=0.0)
    llm_latency_ms: float = Field(default=0.0, ge=0.0)
    validation_status: Literal["not_required", "accepted", "rejected"] = (
        "not_required"
    )


class BooleanExpression(BaseModel):
    node_type: BooleanNodeType
    constraint_id: str | None = None
    children: list["BooleanExpression"] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_shape(self) -> "BooleanExpression":
        if self.node_type is BooleanNodeType.PREDICATE:
            if self.constraint_id is None or self.children:
                raise ValueError("predicate requires one constraint ID and no children")
        elif self.node_type is BooleanNodeType.NOT:
            if len(self.children) != 1:
                raise ValueError("NOT requires exactly one child")
        elif len(self.children) < 2:
            raise ValueError("AND/OR require at least two children")
        return self


class FilterSpec(BaseModel):
    field: str
    operator: FilterOperator
    value: ScalarValue | TypedScalarValue | list[ScalarValue | TypedScalarValue]
    constraint_id: str | None = None

    @model_validator(mode="after")
    def validate_operator_value(self) -> "FilterSpec":
        collection = isinstance(self.value, list)
        if self.operator is FilterOperator.IN:
            if not collection or not self.value:
                raise ValueError("IN filter requires a non-empty collection")
        elif self.operator is FilterOperator.BETWEEN:
            if not collection or len(self.value) != 2:
                raise ValueError("BETWEEN filter requires exactly two values")
        elif collection:
            raise ValueError(f"{self.operator.value} filter requires a scalar value")
        return self


class SortSpec(BaseModel):
    field: str
    direction: Literal["asc", "desc"]
    constraint_id: str | None = None


class SortOperation(BaseModel):
    """Backend-neutral ordered operation carried by a structured QueryPlan."""

    semantic_metric_key: str
    direction: Literal["asc", "desc"]


class TopN(BaseModel):
    """A bounded result window; it is valid only with an explicit sort."""

    value: int = Field(gt=0, le=1000)


class OrderedComparison(BaseModel):
    """An ordered comparison whose ordering contract is not lexical."""

    semantic_field: str
    operator: Literal["gt", "gte", "lt", "lte"]
    value: ScalarValue


class ProductUniverseUnion(BaseModel):
    """Allow-listed product categories combined before global filtering/ranking."""

    operands: list[
        Literal[
            "DomesticETF",
            "ForeignETF",
            "ETF",
            "PublicFund",
            "Fund",
            "KODEX_LONG_ONLY_COMPATIBLE",
            "KODEX_FULL",
            "TIGER_LONG_ONLY_COMPATIBLE",
            "TIGER_FULL",
            "ISHARES_US_FOREIGN_ETF_SECURITY_HOLDINGS",
            "ISHARES_US_FULL",
        ]
    ] = Field(min_length=1, max_length=9)
    constraint_id: str | None = None

    @model_validator(mode="after")
    def validate_operands(self) -> "ProductUniverseUnion":
        if len(self.operands) != len(set(self.operands)):
            raise ValueError("product-universe operands must be unique")
        return self


class RelationMention(BaseModel):
    raw_text: str
    direction: RelationDirection = RelationDirection.OUTGOING
    constraint_id: str | None = None
    subject_type: str | None = None
    target_raw_text: str | None = None
    target_type: str | None = None
    target_value: str | None = None
    negated: bool = False
    chain_id: str | None = None
    path_position: int | None = Field(default=None, ge=0)


class ResultLimit(BaseModel):
    value: int = Field(gt=0)
    raw_text: str
    constraint_id: str | None = None


class AggregationSpec(BaseModel):
    operator: AggregationOperator
    raw_text: str
    constraint_id: str | None = None


class TemporalConstraint(BaseModel):
    raw_text: str
    requested_snapshot: str | None = None
    constraint_id: str | None = None


class ParsedQuery(BaseModel):
    original_question: str
    intent: QueryIntent
    product_types: list[str] = Field(default_factory=list)
    product_universe: ProductUniverseUnion | None = None
    entities: list[EntityMention] = Field(default_factory=list)
    filters: list[FilterSpec] = Field(default_factory=list)
    relations: list[str | RelationMention] = Field(default_factory=list)
    sort: list[SortSpec] = Field(default_factory=list)
    requested_fields: list[str] = Field(default_factory=list)
    semantic_terms: list[str] = Field(default_factory=list)
    requires_semantic_search: bool = False
    boolean_expression: BooleanExpression | None = None
    result_limit: ResultLimit | None = None
    aggregation: AggregationSpec | None = None
    temporal_constraint: TemporalConstraint | None = None
    semantic_constraints: list[SemanticConstraint] = Field(default_factory=list)
    semantic_coverage: SemanticCoverageStatus = SemanticCoverageStatus.COMPLETE
    unparsed_material_spans: list[UnparsedMaterialSpan] = Field(
        default_factory=list
    )
    unsupported_constraint_ids: list[str] = Field(default_factory=list)
    parser_source: ParserSource = ParserSource.RULE
    parse_provenance: ParseProvenance = Field(default_factory=ParseProvenance)


class ResolvedQuery(BaseModel):
    parsed_query: ParsedQuery
    resolved_entities: list[EntityMention] = Field(default_factory=list)


class GroundedConcept(BaseModel):
    raw_text: str
    category: ConceptCategory
    canonical_concept: SemanticValue | None = None
    status: GroundingStatus


class GroundedFilter(BaseModel):
    raw_filter: FilterSpec
    canonical_field: str | None = None
    canonical_value: SemanticValue | None = None
    canonical_values: list[SemanticValue] = Field(default_factory=list)
    status: GroundingStatus


class GroundedSort(BaseModel):
    raw_sort: SortSpec
    canonical_field: str | None = None
    status: GroundingStatus


class GroundedField(BaseModel):
    raw_text: str
    canonical_field: str | None = None
    ontology_uri: str | None = None
    mapping_version: str | None = None
    status: GroundingStatus


class GroundedRelation(BaseModel):
    raw_text: str
    canonical_relation: str | None = None
    ontology_uri: str | None = None
    direction: RelationDirection = RelationDirection.OUTGOING
    status: GroundingStatus
    constraint_id: str | None = None
    subject_type: str | None = None
    target_raw_text: str | None = None
    target_type: str | None = None
    target_value: str | None = None
    negated: bool = False
    chain_id: str | None = None
    path_position: int | None = Field(default=None, ge=0)


class GroundedQuery(BaseModel):
    parsed_query: ParsedQuery
    resolved_entities: list[EntityMention] = Field(default_factory=list)
    canonical_concepts: list[SemanticValue] = Field(default_factory=list)
    canonical_fields: dict[str, str] = Field(default_factory=dict)
    grounded_concepts: list[GroundedConcept] = Field(default_factory=list)
    grounded_filters: list[GroundedFilter] = Field(default_factory=list)
    grounded_sort: list[GroundedSort] = Field(default_factory=list)
    grounded_requested_fields: list[GroundedField] = Field(default_factory=list)
    grounded_relations: list[GroundedRelation] = Field(default_factory=list)
    unresolved_concepts: list[str] = Field(default_factory=list)
    semantic_constraints: list[SemanticConstraint] = Field(default_factory=list)
    ontology_uri: str | None = None
    ontology_version: str | None = None
    semantic_mapping_version: str | None = None
    dataset_snapshot: str | None = None


class SemanticStorageIdentity(BaseModel):
    """Temporary M10.7 bridge between semantic and physical entity grains."""

    storage_row_id: str
    ontology_entity_type: str
    ontology_uri: str
    parent_entity_id: str | None = None
    compatibility_product_type: str | None = None


class CanonicalEntity(BaseModel):
    canonical_id: str
    entity_type: str
    # Fund family names may be authoritatively unavailable in canonical_v2.
    # Keeping None is semantically different from synthesizing a class name.
    official_name: str | None
    aliases: list[str] = Field(default_factory=list)
    identifiers: dict[str, str] = Field(default_factory=dict)


class EntityLookupMatch(BaseModel):
    entity: CanonicalEntity
    matched_alias: str
    identifier_type: str


class RoutingDecision(BaseModel):
    route: PlannerType
    reasons: list[RoutingReason] = Field(default_factory=list)
    required_sources: list[RetrievalSource] = Field(default_factory=list)


class QueryStep(BaseModel):
    step_id: str
    source: RetrievalSource
    operation: QueryOperation
    inputs: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    covers_constraint_ids: list[str] = Field(default_factory=list)


class QueryPlan(BaseModel):
    planner: PlannerType
    steps: list[QueryStep] = Field(default_factory=list)
    routing_reasons: list[RoutingReason] = Field(default_factory=list)
    unsupported_constraint_ids: list[str] = Field(default_factory=list)
    constraint_coverage_required: bool = False


class RetrievalRecord(BaseModel):
    step_id: str | None = None
    source: str
    source_id: str
    entity_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalResult(BaseModel):
    """Records plus non-semantic transport cardinality metadata."""

    records: list[RetrievalRecord] = Field(default_factory=list)
    total_matches: int | None = Field(default=None, ge=0)
    returned_count: int = Field(default=0, ge=0)
    window_limit: int | None = Field(default=None, ge=1)
    counts: dict[str, int] = Field(default_factory=dict)
    ranked_candidate_ids: list[str] = Field(default_factory=list)
    filtered_total: int | None = Field(default=None, ge=0)
    rankable_total: int | None = Field(default=None, ge=0)
    missing_metric_total: int | None = Field(default=None, ge=0)
    requested_top_n: int | None = Field(default=None, ge=1)


class StepExecutionResult(BaseModel):
    step_id: str
    source: RetrievalSource
    status: StepExecutionStatus
    records: list[RetrievalRecord] = Field(default_factory=list)
    retrieval_metadata: dict[str, Any] = Field(default_factory=dict)
    error_code: ExecutionErrorCode | None = None
    error_message: str | None = None
    started_at: datetime
    finished_at: datetime
    duration_seconds: float = Field(ge=0.0)
    dependency_ids: list[str] = Field(default_factory=list)


class ExecutionResult(BaseModel):
    records: list[RetrievalRecord] = Field(default_factory=list)
    step_results: dict[str, StepExecutionResult] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class ExecutionContext(BaseModel):
    plan: QueryPlan
    step_results: dict[str, StepExecutionResult] = Field(default_factory=dict)


class Evidence(BaseModel):
    step_id: str | None = None
    source_type: str
    source_id: str
    entity_id: str | None = None
    field: str | None = None
    value: str | None = None
    text: str | None = None
    dataset_snapshot: str | None = None
    observed_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvidenceBundle(BaseModel):
    question: str
    resolved_entities: list[EntityMention] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    execution_result: ExecutionResult | None = None


class FieldQualityMetadata(BaseModel):
    canonical_field: str
    coverage_status: CoverageStatus = CoverageStatus.UNKNOWN
    coverage_fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    sentinel_values: list[ScalarValue] = Field(default_factory=list)
    nullable: bool = True
    ranking_safe: bool | None = None
    comparison_safe: bool | None = None
    snapshot_policy: SnapshotPolicy = (
        SnapshotPolicy.REQUIRE_CONSISTENT_FOR_COMPARISON
    )


class ValidationFinding(BaseModel):
    code: AnswerabilityReasonCode
    severity: FindingSeverity
    entity_id: str | None = None
    field: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    message: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ValidationResult(BaseModel):
    answerable: bool
    reason_codes: list[AnswerabilityReasonCode] = Field(default_factory=list)
    findings: list[ValidationFinding] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

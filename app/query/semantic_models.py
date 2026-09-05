from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.models import (
    AggregationOperator,
    BooleanNodeType,
    FilterOperator,
    QueryIntent,
    RelationDirection,
    ScalarUnit,
)


class UntrustedCandidateModel(BaseModel):
    """Strict base for data proposed by an external language model."""

    model_config = ConfigDict(extra="forbid")


class LLMCandidateSpan(UntrustedCandidateModel):
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    raw_text: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_order(self) -> "LLMCandidateSpan":
        if self.end <= self.start:
            raise ValueError("candidate span end must be greater than start")
        return self


class LLMSemanticTermCandidate(UntrustedCandidateModel):
    source_span: LLMCandidateSpan
    value: str = Field(min_length=1)


class LLMEntityCandidate(UntrustedCandidateModel):
    source_span: LLMCandidateSpan
    entity_type: Literal[
        "product",
        "financial_product",
        "fund",
        "fund_share_class",
        "sale_lot",
        "management_company",
        "asset_manager",
        "organization",
        "company",
        "issuer",
        "portfolio_company",
        "subsidiary",
        "institution",
        "index",
        "security",
        "holding",
    ]


class LLMTypedValueCandidate(UntrustedCandidateModel):
    raw: str = Field(min_length=1)
    unit: ScalarUnit
    normalized: int | float | None = None
    currency: str | None = None


class LLMFilterCandidate(UntrustedCandidateModel):
    source_span: LLMCandidateSpan
    field: str = Field(min_length=1)
    operator: FilterOperator
    value: str | list[str] | LLMTypedValueCandidate


class LLMSortCandidate(UntrustedCandidateModel):
    source_span: LLMCandidateSpan
    field: str = Field(min_length=1)
    direction: Literal["asc", "desc"]


class LLMRelationCandidate(UntrustedCandidateModel):
    source_span: LLMCandidateSpan
    raw_relation: str = Field(min_length=1)
    direction: RelationDirection = RelationDirection.OUTGOING
    subject_type: str | None = None
    target_raw_text: str | None = None
    target_type: str | None = None
    negated: bool = False
    chain_id: str | None = None
    path_position: int | None = Field(default=None, ge=0)


class LLMBooleanExpressionCandidate(UntrustedCandidateModel):
    node_type: BooleanNodeType
    predicate_span: LLMCandidateSpan | None = None
    children: list["LLMBooleanExpressionCandidate"] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_shape(self) -> "LLMBooleanExpressionCandidate":
        if self.node_type is BooleanNodeType.PREDICATE:
            if self.predicate_span is None or self.children:
                raise ValueError("predicate requires a span and no children")
        elif self.node_type is BooleanNodeType.NOT:
            if self.predicate_span is not None or len(self.children) != 1:
                raise ValueError("NOT requires exactly one child")
        elif self.predicate_span is not None or len(self.children) < 2:
            raise ValueError("AND/OR require at least two children")
        return self


class LLMResultLimitCandidate(UntrustedCandidateModel):
    source_span: LLMCandidateSpan
    value: int = Field(gt=0)


class LLMAggregationCandidate(UntrustedCandidateModel):
    source_span: LLMCandidateSpan
    operator: AggregationOperator


class LLMTemporalCandidate(UntrustedCandidateModel):
    source_span: LLMCandidateSpan
    requested_snapshot: str | None = None


class LLMSemanticParseCandidate(UntrustedCandidateModel):
    """Untrusted structured proposal; never passed directly to planning."""

    intent: QueryIntent
    product_types: list[LLMSemanticTermCandidate] = Field(default_factory=list)
    entities: list[LLMEntityCandidate] = Field(default_factory=list)
    filters: list[LLMFilterCandidate] = Field(default_factory=list)
    sorts: list[LLMSortCandidate] = Field(default_factory=list)
    requested_fields: list[LLMSemanticTermCandidate] = Field(default_factory=list)
    group_by: list[LLMSemanticTermCandidate] = Field(default_factory=list)
    semantic_texts: list[LLMSemanticTermCandidate] = Field(default_factory=list)
    subjective_conditions: list[LLMSemanticTermCandidate] = Field(
        default_factory=list
    )
    relations: list[LLMRelationCandidate] = Field(default_factory=list)
    boolean_expression: LLMBooleanExpressionCandidate | None = None
    result_limit: LLMResultLimitCandidate | None = None
    aggregation: LLMAggregationCandidate | None = None
    temporal_condition: LLMTemporalCandidate | None = None
    unresolved_material_phrases: list[LLMCandidateSpan] = Field(
        default_factory=list
    )


class SemanticParserRequest(UntrustedCandidateModel):
    original_question: str
    rule_parse: dict[str, object]
    compact_vocabulary: dict[str, list[str]]
    semantic_schema_version: str
    prompt_version: str

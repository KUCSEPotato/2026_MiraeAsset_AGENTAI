from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from app.domain.models import (
    AggregationSpec,
    BooleanExpression,
    BooleanNodeType,
    ConstraintSemanticType,
    ConstraintStatus,
    EntityMention,
    FilterOperator,
    FilterSpec,
    ParseProvenance,
    ParsedQuery,
    ParserSource,
    QueryIntent,
    RelationMention,
    ResultLimit,
    ScalarUnit,
    SemanticConstraint,
    SemanticCoverageStatus,
    SortSpec,
    SourceSpan,
    TemporalConstraint,
    TypedScalarValue,
    UnparsedMaterialSpan,
)
from app.ontology.index import normalize_ontology_text
from app.query.exceptions import SemanticCandidateValidationError
from app.query.semantic_models import (
    LLMCandidateSpan,
    LLMBooleanExpressionCandidate,
    LLMFilterCandidate,
    LLMSemanticParseCandidate,
    LLMSemanticTermCandidate,
    LLMTypedValueCandidate,
)


@dataclass(frozen=True)
class _Draft:
    span: LLMCandidateSpan
    semantic_type: ConstraintSemanticType
    payload: dict[str, Any]
    ref: tuple[str, int] | None = None
    status: ConstraintStatus = ConstraintStatus.PARSED
    reason: str | None = None


class LLMSemanticCandidateValidator:
    """Convert an untrusted full candidate into the existing ParsedQuery contract."""

    _subject_types = {
        "AssetManager", "Bond", "Currency", "ETF", "ETN",
        "ExchangeTradedProduct", "FinancialProduct", "Fund", "Index",
        "Issuer", "RiskGrade",
    }
    _target_types = {"AssetManager", "Currency", "Index", "Issuer", "RiskGrade"}

    def __init__(self, vocabulary: dict[str, list[str]]) -> None:
        self._vocabulary = {key: list(values) for key, values in vocabulary.items()}
        self._allowed_fields = {
            normalize_ontology_text(item) for item in vocabulary["fields"]
        }
        self._allowed_product_types = {
            normalize_ontology_text(item) for item in vocabulary["product_types"]
        }
        self._allowed_relations = {
            normalize_ontology_text(item) for item in vocabulary["relations"]
        }

    def validate(
        self,
        question: str,
        rule_result: ParsedQuery,
        candidate: LLMSemanticParseCandidate,
        *,
        model: str,
        rule_latency_ms: float,
        llm_latency_ms: float,
        prompt_version: str,
        schema_version: str,
    ) -> ParsedQuery:
        reasons = self._validate_candidate(question, rule_result, candidate)
        if reasons:
            raise SemanticCandidateValidationError(reasons)
        parsed = self._convert(question, candidate)
        return parsed.model_copy(
            update={
                "parser_source": ParserSource.LLM_FALLBACK,
                "parse_provenance": ParseProvenance(
                    parser_source=ParserSource.LLM_FALLBACK,
                    semantic_schema_version=schema_version,
                    prompt_version=prompt_version,
                    model=model,
                    rule_latency_ms=rule_latency_ms,
                    llm_latency_ms=llm_latency_ms,
                    validation_status="accepted",
                ),
            }
        )

    def _validate_candidate(
        self,
        question: str,
        rule_result: ParsedQuery,
        candidate: LLMSemanticParseCandidate,
    ) -> list[str]:
        reasons: list[str] = []
        spans = self._all_spans(candidate)
        for span in spans:
            if span.end > len(question) or question[span.start:span.end] != span.raw_text:
                reasons.append("invalid_source_span")

        for item in candidate.product_types:
            if normalize_ontology_text(item.value) not in self._allowed_product_types:
                reasons.append("unknown_product_type")
            self._require_value_in_span(item.value, item.source_span, reasons)
        for item in candidate.requested_fields:
            if normalize_ontology_text(item.value) not in self._allowed_fields:
                reasons.append("unknown_requested_field")
            self._require_value_in_span(item.value, item.source_span, reasons)
        for item in candidate.sorts:
            if normalize_ontology_text(item.field) not in self._allowed_fields:
                reasons.append("unknown_sort_field")
            self._require_value_in_span(item.field, item.source_span, reasons)
        for item in candidate.filters:
            if normalize_ontology_text(item.field) not in self._allowed_fields:
                reasons.append("unknown_filter_field")
            self._validate_filter_value(item, reasons)
        for item in candidate.semantic_texts:
            self._require_value_in_span(item.value, item.source_span, reasons)
        for item in candidate.subjective_conditions:
            self._require_value_in_span(item.value, item.source_span, reasons)
        for item in candidate.relations:
            if normalize_ontology_text(item.raw_relation) not in self._allowed_relations:
                reasons.append("unknown_relation")
            self._require_value_in_span(item.raw_relation, item.source_span, reasons)
            if item.target_raw_text:
                self._require_value_in_span(
                    item.target_raw_text, item.source_span, reasons
                )
            if item.subject_type and item.subject_type not in self._subject_types:
                reasons.append("unknown_relation_subject_type")
            if item.target_type and item.target_type not in self._target_types:
                reasons.append("unknown_relation_target_type")

        if not self._covers_rule_material(rule_result, candidate):
            reasons.append("candidate_omits_rule_material")
        return list(dict.fromkeys(reasons))

    @staticmethod
    def _require_value_in_span(
        value: str,
        span: LLMCandidateSpan,
        reasons: list[str],
    ) -> None:
        if normalize_ontology_text(value) not in normalize_ontology_text(span.raw_text):
            reasons.append("candidate_value_not_grounded_in_span")

    def _validate_filter_value(
        self,
        item: LLMFilterCandidate,
        reasons: list[str],
    ) -> None:
        value = item.value
        if isinstance(value, LLMTypedValueCandidate):
            if normalize_ontology_text(value.raw) not in normalize_ontology_text(
                item.source_span.raw_text
            ):
                reasons.append("numeric_raw_value_not_in_span")
                return
            try:
                deterministic = _normalize_typed_value(value)
            except ValueError:
                reasons.append("invalid_numeric_unit")
                return
            if value.normalized is not None and not math.isclose(
                float(value.normalized), float(deterministic.normalized), rel_tol=1e-9
            ):
                reasons.append("inconsistent_numeric_normalization")
            return
        values = value if isinstance(value, list) else [value]
        if item.operator in {FilterOperator.IN, FilterOperator.BETWEEN}:
            valid_collection = (
                len(values) == 2
                if item.operator is FilterOperator.BETWEEN
                else bool(values)
            )
            if not isinstance(value, list) or not valid_collection:
                reasons.append("invalid_collection_operator_value")
        elif isinstance(value, list):
            reasons.append("invalid_scalar_operator_value")
        for raw in values:
            self._require_value_in_span(raw, item.source_span, reasons)

    @staticmethod
    def _covers_rule_material(
        rule_result: ParsedQuery,
        candidate: LLMSemanticParseCandidate,
    ) -> bool:
        typed: dict[ConstraintSemanticType, list[LLMCandidateSpan]] = {
            ConstraintSemanticType.PRODUCT_TYPE: [
                item.source_span for item in candidate.product_types
            ] + [
                item.source_span for item in candidate.filters
                if normalize_ontology_text(item.field) == "product_type"
            ],
            ConstraintSemanticType.ENTITY: [
                item.source_span for item in candidate.entities
            ],
            ConstraintSemanticType.FILTER: [
                item.source_span for item in candidate.filters
            ],
            ConstraintSemanticType.SORT: [
                item.source_span for item in candidate.sorts
            ],
            ConstraintSemanticType.REQUESTED_FIELD: [
                item.source_span for item in candidate.requested_fields
            ],
            ConstraintSemanticType.RELATION: [
                item.source_span for item in candidate.relations
            ],
            ConstraintSemanticType.LIMIT: (
                [candidate.result_limit.source_span]
                if candidate.result_limit else []
            ),
            ConstraintSemanticType.AGGREGATION: (
                [candidate.aggregation.source_span]
                if candidate.aggregation else []
            ),
            ConstraintSemanticType.TEMPORAL: (
                [candidate.temporal_condition.source_span]
                if candidate.temporal_condition else []
            ),
        }
        semantic_spans = [
            item.source_span
            for item in (
                *candidate.semantic_texts,
                *candidate.subjective_conditions,
            )
        ] + candidate.unresolved_material_phrases
        typed[ConstraintSemanticType.SEMANTIC] = semantic_spans
        typed[ConstraintSemanticType.SUBJECTIVE] = semantic_spans

        def overlaps(start: int, end: int, spans: list[LLMCandidateSpan]) -> bool:
            return any(start < span.end and end > span.start for span in spans)

        for item in rule_result.semantic_constraints:
            if not item.required or item.semantic_type in {
                ConstraintSemanticType.INTENT,
                ConstraintSemanticType.BOOLEAN,
            }:
                continue
            candidates = typed.get(item.semantic_type)
            if (
                item.semantic_type is ConstraintSemanticType.SEMANTIC
                and item.status is ConstraintStatus.UNSUPPORTED
            ):
                candidates = LLMSemanticCandidateValidator._all_spans(candidate)
            if candidates is None:
                candidates = semantic_spans
            if not overlaps(item.source_span.start, item.source_span.end, candidates):
                return False
        material_candidates = LLMSemanticCandidateValidator._all_spans(candidate)
        if any(
            not overlaps(
                item.source_span.start,
                item.source_span.end,
                material_candidates,
            )
            for item in rule_result.unparsed_material_spans
        ):
            return False
        if rule_result.requires_semantic_search and not semantic_spans:
            return False
        return True

    def _convert(
        self,
        question: str,
        candidate: LLMSemanticParseCandidate,
    ) -> ParsedQuery:
        products = [item.value for item in candidate.product_types]
        entities = [
            EntityMention(
                raw_text=item.source_span.raw_text,
                entity_type=item.entity_type,
            )
            for item in candidate.entities
        ]
        filters = [
            FilterSpec(
                field=item.field,
                operator=item.operator,
                value=(
                    _normalize_typed_value(item.value)
                    if isinstance(item.value, LLMTypedValueCandidate)
                    else item.value
                ),
            )
            for item in candidate.filters
        ]
        sorts = [
            SortSpec(field=item.field, direction=item.direction)
            for item in candidate.sorts
        ]
        relations = [
            RelationMention(
                raw_text=item.raw_relation,
                direction=item.direction,
                subject_type=item.subject_type,
                target_raw_text=item.target_raw_text,
                target_type=item.target_type,
                target_value=item.target_raw_text,
                negated=item.negated,
                chain_id=item.chain_id,
                path_position=item.path_position,
            )
            for item in candidate.relations
        ]
        drafts: list[_Draft] = []
        drafts.extend(
            _Draft(item.source_span, ConstraintSemanticType.PRODUCT_TYPE,
                   {"value": item.value}, ("product", index))
            for index, item in enumerate(candidate.product_types)
        )
        drafts.extend(
            _Draft(item.source_span, ConstraintSemanticType.ENTITY,
                   {"entity_type": item.entity_type}, ("entity", index))
            for index, item in enumerate(candidate.entities)
        )
        for index, item in enumerate(candidate.filters):
            typed = isinstance(item.value, LLMTypedValueCandidate)
            drafts.append(_Draft(
                item.source_span,
                ConstraintSemanticType.FILTER,
                {
                    "field": item.field,
                    "operator": item.operator.value,
                    "value": filters[index].model_dump(mode="json")["value"],
                },
                ("filter", index),
                ConstraintStatus.UNSUPPORTED if typed and item.value.unit is not ScalarUnit.NONE else ConstraintStatus.PARSED,
                "dataset_unit_mapping_unverified" if typed and item.value.unit is not ScalarUnit.NONE else None,
            ))
        drafts.extend(
            _Draft(item.source_span, ConstraintSemanticType.SORT,
                   {"field": item.field, "direction": item.direction},
                   ("sort", index))
            for index, item in enumerate(candidate.sorts)
        )
        drafts.extend(
            _Draft(item.source_span, ConstraintSemanticType.REQUESTED_FIELD,
                   {"field": item.value}, ("requested", index))
            for index, item in enumerate(candidate.requested_fields)
        )
        drafts.extend(
            _Draft(item.source_span, ConstraintSemanticType.SEMANTIC,
                   {"text": item.value}, ("semantic", index))
            for index, item in enumerate(candidate.semantic_texts)
        )
        drafts.extend(
            _Draft(item.source_span, ConstraintSemanticType.SUBJECTIVE,
                   {"text": item.value}, status=ConstraintStatus.UNSUPPORTED,
                   reason="subjective_execution_unsupported")
            for item in candidate.subjective_conditions
        )
        for index, item in enumerate(candidate.relations):
            drafts.append(_Draft(
                item.source_span,
                ConstraintSemanticType.RELATION,
                {
                    "direction": item.direction.value,
                    "target": item.target_raw_text,
                    "chain_id": item.chain_id,
                    "path_position": item.path_position,
                },
                ("relation", index),
                ConstraintStatus.UNSUPPORTED if item.negated else ConstraintStatus.PARSED,
                "negated_relation_execution_unsupported" if item.negated else None,
            ))
        if candidate.result_limit:
            drafts.append(_Draft(
                candidate.result_limit.source_span,
                ConstraintSemanticType.LIMIT,
                {"value": candidate.result_limit.value},
                ("limit", 0),
            ))
        if candidate.aggregation:
            drafts.append(_Draft(
                candidate.aggregation.source_span,
                ConstraintSemanticType.AGGREGATION,
                {"operator": candidate.aggregation.operator.value},
                ("aggregation", 0), ConstraintStatus.UNSUPPORTED,
                "aggregation_execution_unsupported",
            ))
        if candidate.temporal_condition:
            drafts.append(_Draft(
                candidate.temporal_condition.source_span,
                ConstraintSemanticType.TEMPORAL,
                {"snapshot": candidate.temporal_condition.requested_snapshot},
                ("temporal", 0), ConstraintStatus.UNSUPPORTED,
                "temporal_snapshot_unsupported",
            ))
        if candidate.intent in {
            QueryIntent.COMPARE_PRODUCTS,
            QueryIntent.RECOMMEND_PRODUCT,
            QueryIntent.UNKNOWN,
        }:
            drafts.append(_Draft(
                _intent_span(question, candidate), ConstraintSemanticType.INTENT,
                {"intent": candidate.intent.value}, status=ConstraintStatus.UNSUPPORTED,
                reason="intent_execution_unsupported",
            ))
        if candidate.boolean_expression and _unsupported_boolean(
            candidate.boolean_expression, len(candidate.semantic_texts)
        ):
            drafts.append(_Draft(
                _boolean_span(question, candidate.boolean_expression),
                ConstraintSemanticType.BOOLEAN,
                {"node_type": candidate.boolean_expression.node_type.value},
                status=ConstraintStatus.UNSUPPORTED,
                reason="boolean_execution_unsupported",
            ))
        drafts.extend(
            _Draft(span, ConstraintSemanticType.SEMANTIC, {"unresolved": True},
                   status=ConstraintStatus.UNSUPPORTED,
                   reason="unresolved_material_phrase")
            for span in candidate.unresolved_material_phrases
        )

        ordered = sorted(
            enumerate(drafts),
            key=lambda pair: (
                pair[1].span.start, pair[1].span.end,
                pair[1].semantic_type.value, pair[0],
            ),
        )
        constraints: list[SemanticConstraint] = []
        ref_ids: dict[tuple[str, int], str] = {}
        span_ids: list[tuple[LLMCandidateSpan, str]] = []
        for number, (_, draft) in enumerate(ordered, start=1):
            constraint_id = f"C{number}"
            constraints.append(SemanticConstraint(
                constraint_id=constraint_id,
                source_span=SourceSpan(start=draft.span.start, end=draft.span.end),
                raw_text=draft.span.raw_text,
                semantic_type=draft.semantic_type,
                status=draft.status,
                payload=draft.payload,
                unsupported_reason=draft.reason,
            ))
            if draft.ref:
                ref_ids[draft.ref] = constraint_id
            span_ids.append((draft.span, constraint_id))

        products = list(products)
        entities = [item.model_copy(update={"constraint_id": ref_ids[("entity", i)]})
                    for i, item in enumerate(entities)]
        filters = [item.model_copy(update={"constraint_id": ref_ids[("filter", i)]})
                   for i, item in enumerate(filters)]
        sorts = [item.model_copy(update={"constraint_id": ref_ids[("sort", i)]})
                 for i, item in enumerate(sorts)]
        relations = [item.model_copy(update={"constraint_id": ref_ids[("relation", i)]})
                     for i, item in enumerate(relations)]
        boolean = _convert_boolean(candidate.boolean_expression, span_ids)
        result_limit = (
            ResultLimit(
                value=candidate.result_limit.value,
                raw_text=candidate.result_limit.source_span.raw_text,
                constraint_id=ref_ids[("limit", 0)],
            ) if candidate.result_limit else None
        )
        aggregation = (
            AggregationSpec(
                operator=candidate.aggregation.operator,
                raw_text=candidate.aggregation.source_span.raw_text,
                constraint_id=ref_ids[("aggregation", 0)],
            ) if candidate.aggregation else None
        )
        temporal = (
            TemporalConstraint(
                raw_text=candidate.temporal_condition.source_span.raw_text,
                requested_snapshot=candidate.temporal_condition.requested_snapshot,
                constraint_id=ref_ids[("temporal", 0)],
            ) if candidate.temporal_condition else None
        )
        unsupported = [
            item.constraint_id for item in constraints
            if item.status is ConstraintStatus.UNSUPPORTED
        ]
        unresolved = [
            UnparsedMaterialSpan(
                source_span=SourceSpan(start=span.start, end=span.end),
                raw_text=span.raw_text,
            )
            for span in candidate.unresolved_material_phrases
        ]
        return ParsedQuery(
            original_question=question,
            intent=candidate.intent,
            product_types=products,
            entities=entities,
            filters=filters,
            relations=relations,
            sort=sorts,
            requested_fields=[item.value for item in candidate.requested_fields],
            semantic_terms=[item.value for item in candidate.semantic_texts],
            requires_semantic_search=bool(candidate.semantic_texts),
            boolean_expression=boolean,
            result_limit=result_limit,
            aggregation=aggregation,
            temporal_constraint=temporal,
            semantic_constraints=constraints,
            semantic_coverage=(
                SemanticCoverageStatus.INCOMPLETE
                if unresolved else SemanticCoverageStatus.COMPLETE
            ),
            unparsed_material_spans=unresolved,
            unsupported_constraint_ids=unsupported,
        )

    @staticmethod
    def _all_spans(candidate: LLMSemanticParseCandidate) -> list[LLMCandidateSpan]:
        spans = [
            item.source_span
            for collection in (
                candidate.product_types, candidate.entities, candidate.filters,
                candidate.sorts, candidate.requested_fields,
                candidate.semantic_texts, candidate.subjective_conditions,
                candidate.relations,
            )
            for item in collection
        ]
        spans.extend(candidate.unresolved_material_phrases)
        for optional in (
            candidate.result_limit, candidate.aggregation,
            candidate.temporal_condition,
        ):
            if optional is not None:
                spans.append(optional.source_span)
        if candidate.boolean_expression:
            spans.extend(_boolean_predicate_spans(candidate.boolean_expression))
        return spans


def _normalize_typed_value(value: LLMTypedValueCandidate) -> TypedScalarValue:
    normalized_text = value.raw.replace(",", "").strip()
    number_match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(%|원|억|조|개)?", normalized_text)
    if number_match is None:
        raise ValueError("unsupported numeric form")
    number = float(number_match.group(1))
    suffix = number_match.group(2)
    if value.unit is ScalarUnit.RATIO and suffix == "%":
        normalized = number / 100.0
    elif value.unit is ScalarUnit.KRW and suffix in {"원", "억", "조"}:
        normalized = number * {"원": 1, "억": 100_000_000, "조": 1_000_000_000_000}[suffix]
    elif value.unit is ScalarUnit.COUNT and suffix in {None, "개"}:
        normalized = number
    elif value.unit is ScalarUnit.NONE and suffix is None:
        normalized = number
    else:
        raise ValueError("numeric unit does not match raw value")
    return TypedScalarValue(
        raw=value.raw,
        normalized=normalized,
        unit=value.unit,
        currency="KRW" if value.unit is ScalarUnit.KRW else value.currency,
    )


def _boolean_predicate_spans(
    expression: LLMBooleanExpressionCandidate,
) -> list[LLMCandidateSpan]:
    if expression.node_type is BooleanNodeType.PREDICATE:
        return [expression.predicate_span] if expression.predicate_span else []
    return [
        span for child in expression.children
        for span in _boolean_predicate_spans(child)
    ]


def _boolean_span(
    question: str,
    expression: LLMBooleanExpressionCandidate,
) -> LLMCandidateSpan:
    spans = _boolean_predicate_spans(expression)
    start = min(span.start for span in spans)
    end = max(span.end for span in spans)
    return LLMCandidateSpan(start=start, end=end, raw_text=question[start:end])


def _unsupported_boolean(
    expression: LLMBooleanExpressionCandidate,
    semantic_term_count: int,
) -> bool:
    if expression.node_type is BooleanNodeType.OR:
        return True
    if semantic_term_count > 1 and expression.node_type is BooleanNodeType.AND:
        return True
    return any(_unsupported_boolean(child, semantic_term_count)
               for child in expression.children)


def _convert_boolean(
    expression: LLMBooleanExpressionCandidate | None,
    span_ids: list[tuple[LLMCandidateSpan, str]],
) -> BooleanExpression | None:
    if expression is None:
        return None
    if expression.node_type is BooleanNodeType.PREDICATE:
        assert expression.predicate_span is not None
        constraint_id = next(
            constraint_id for span, constraint_id in span_ids
            if span.start == expression.predicate_span.start
            and span.end == expression.predicate_span.end
        )
        return BooleanExpression(node_type="predicate", constraint_id=constraint_id)
    return BooleanExpression(
        node_type=expression.node_type,
        children=[_convert_boolean(child, span_ids) for child in expression.children],
    )


def _intent_span(
    question: str,
    candidate: LLMSemanticParseCandidate,
) -> LLMCandidateSpan:
    tokens = {
        QueryIntent.COMPARE_PRODUCTS: ("비교", "더 낮은 쪽"),
        QueryIntent.RECOMMEND_PRODUCT: ("추천",),
        QueryIntent.UNKNOWN: (),
    }[candidate.intent]
    for token in tokens:
        start = question.find(token)
        if start >= 0:
            return LLMCandidateSpan(start=start, end=start + len(token), raw_text=token)
    return LLMCandidateSpan(start=0, end=len(question), raw_text=question)

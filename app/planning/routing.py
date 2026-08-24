from app.domain.models import (
    ConceptCategory,
    GroundedQuery,
    GroundingStatus,
    PlannerType,
    ResolutionStatus,
    RetrievalSource,
    RoutingDecision,
    RoutingReason,
)
from app.planning.metadata import FieldCapability, RoutingMetadataRegistry


class FastRoutingChecker:
    """Conservatively decide whether deterministic rule planning is safe."""

    def __init__(self, metadata: RoutingMetadataRegistry) -> None:
        self._metadata = metadata

    def check(self, query: GroundedQuery) -> RoutingDecision:
        reasons: list[RoutingReason] = []
        sources: list[RetrievalSource] = []

        structured = self._has_structured_criteria(query)
        if structured:
            self._append_unique(sources, RetrievalSource.RDB)

        if query.parsed_query.requires_semantic_search:
            self._append_unique(reasons, RoutingReason.REQUIRES_SEMANTIC_SEARCH)
            self._append_unique(sources, RetrievalSource.VECTOR)

        if query.parsed_query.relations:
            self._append_unique(reasons, RoutingReason.RELATION_TRAVERSAL)
            self._append_unique(sources, RetrievalSource.GRAPH)

        if query.unresolved_concepts:
            self._append_unique(reasons, RoutingReason.UNRESOLVED_SEMANTIC_TERM)
            self._append_unique(sources, RetrievalSource.VECTOR)

        for entity in query.resolved_entities:
            if entity.resolution_status is ResolutionStatus.AMBIGUOUS:
                self._append_unique(reasons, RoutingReason.AMBIGUOUS_ENTITY)
                self._append_unique(sources, RetrievalSource.BM25)
            elif entity.resolution_status is ResolutionStatus.UNRESOLVED:
                self._append_unique(reasons, RoutingReason.UNRESOLVED_ENTITY)
                self._append_unique(sources, RetrievalSource.BM25)

        if self._has_unsupported_grounding(query):
            self._append_unique(
                reasons,
                RoutingReason.UNSUPPORTED_STRUCTURED_CONDITION,
            )

        if not structured and not sources:
            self._append_unique(
                reasons,
                RoutingReason.UNSUPPORTED_STRUCTURED_CONDITION,
            )
            self._append_unique(sources, RetrievalSource.BM25)

        if len(sources) > 1:
            self._append_unique(reasons, RoutingReason.MULTI_SOURCE_QUERY)

        if reasons:
            return RoutingDecision(
                route=PlannerType.SUPERVISOR,
                reasons=reasons,
                required_sources=sources,
            )
        return RoutingDecision(
            route=PlannerType.RULE,
            reasons=[RoutingReason.DETERMINISTIC_STRUCTURED_QUERY],
            required_sources=[RetrievalSource.RDB],
        )

    def _has_structured_criteria(self, query: GroundedQuery) -> bool:
        grounded_product_type = any(
            concept.category is ConceptCategory.PRODUCT_TYPE
            and concept.status is GroundingStatus.RESOLVED
            for concept in query.grounded_concepts
        )
        resolved_entity = any(
            entity.resolution_status is ResolutionStatus.RESOLVED
            and entity.entity_type == "product"
            for entity in query.resolved_entities
        )
        other_structured = bool(
            grounded_product_type
            or query.grounded_filters
            or query.grounded_sort
            or query.grounded_requested_fields
        )
        if query.parsed_query.relations and not other_structured:
            resolved_entity = False
        return bool(
            other_structured or resolved_entity
        )

    def _has_unsupported_grounding(self, query: GroundedQuery) -> bool:
        for item in query.grounded_filters:
            if item.status is not GroundingStatus.RESOLVED:
                return True
            if item.canonical_field is None or not self._metadata.supports_field(
                item.canonical_field,
                FieldCapability.FILTER,
            ):
                return True
        for item in query.grounded_sort:
            if item.status is not GroundingStatus.RESOLVED:
                return True
            if item.canonical_field is None or not self._metadata.supports_field(
                item.canonical_field,
                FieldCapability.SORT,
            ):
                return True
        for item in query.grounded_requested_fields:
            if item.status is not GroundingStatus.RESOLVED:
                return True
            if item.canonical_field is None or not self._metadata.supports_field(
                item.canonical_field,
                FieldCapability.PROJECT,
            ):
                return True
        return False

    @staticmethod
    def _append_unique(items: list, item) -> None:
        if item not in items:
            items.append(item)
